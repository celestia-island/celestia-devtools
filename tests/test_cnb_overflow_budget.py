#!/usr/bin/env python3
"""Tests for the caller-lane build-pool budget guard in ``.github/workflows/cnb-overflow.yml``.

*Why this exists:* the reusable CNB overflow router dispatches to the org's **build** pool, whose
160 core-hour/month free quota is a capability limit, not a soft bill — the docs say the capability
is restricted once it is exhausted and that a pre-freeze with insufficient quota terminates the
task. Measured on 2026-09-24, the caller lanes alone really dispatched **68 CNB builds in 24 h**
(~9-13 core-hours/day ≈ 280-390/month), i.e. on their own they overrun the cap — and the pool they
would exhaust is also what the mirror-sync pipelines run on, which is what the *dev-pool* lane
clones from. So the guard has to hold, and hold in the right direction:

* it must narrow dispatch (never widen it),
* a refusal must be a no-op (the farm still checks the SHA) and must never post a status,
* it must fail **closed** when the ledger cannot be read,
* it must never echo the token it authenticates with.

The guard lives as inline python inside the workflow (the router job has no checkout), so these
tests execute the **shipped text**: they extract the step's script from the YAML and run it in a
subprocess against a stub ledger HTTP server. Editing the workflow is what turns these red.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/cnb-overflow.yml"
GUARD_STEP_ID = "budget"
CAP_H = 160.0


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job: str) -> list:
    return _doc()["jobs"][job]["steps"]


def _guard_step() -> dict:
    return next(s for s in _steps("route") if s.get("id") == GUARD_STEP_ID)


def _guard_script() -> str:
    """The shipped guard program, heredoc wrapper stripped.

    The block scalar is already dedented by the YAML loader, so the captured text keeps exactly
    the relative indentation the runner feeds to python.
    """
    run = _guard_step()["run"]
    m = re.search(r"<<'PYEOF'\n(.*?)\n\s*PYEOF", run, re.S)
    assert m, "guard step no longer contains a PYEOF heredoc"
    return m.group(1)


class _Ledger(http.server.BaseHTTPRequestHandler):
    """Stub `/-/charge/volume`: serves `payload` or fails with `status`."""

    payload: dict = {}
    status = 200
    seen_auth: list = []

    def do_GET(self):                                                  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                                         # noqa: D102
        pass


@pytest.fixture
def ledger():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ledger)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Ledger.payload, _Ledger.status, _Ledger.seen_auth = {}, 200, []
    yield f"http://127.0.0.1:{srv.server_address[1]}/charge"
    srv.shutdown()


def run_guard(tmp_path, ledger_url, *, used_core_h=None, pct="70", auth_token="tok-secret-value",
              force=None, extra_env=None, cap=None, outputs=True):
    """Execute the shipped guard; return (stdout+stderr, outputs-dict, exit code)."""
    env = {k: v for k, v in os.environ.items() if k not in ("BUDGET_PCT", "FORCE_CNB", "CNB_TOKEN")}
    env["CHARGE_URL"] = ledger_url
    env["BUDGET_PCT"] = pct
    env["BUILD_CAP_H"] = str(cap if cap is not None else CAP_H)
    if auth_token is not None:
        env["CNB_TOKEN"] = auth_token
    if force is not None:
        env["FORCE_CNB"] = force
    if used_core_h is not None:
        _Ledger.payload = {"ci_in_sec": int(used_core_h * 3600), "dev_in_sec": 0}
    out_file = tmp_path / "gh_output"
    out_file.write_text("")
    if outputs:
        env["GITHUB_OUTPUT"] = str(out_file)
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-c", _guard_script()], capture_output=True, text=True,
                       env=env, timeout=60)
    got = dict(line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line)
    return r.stdout + r.stderr, got, r.returncode


class TestShippedShape:
    def test_guard_runs_before_the_two_non_budget_gates(self):
        ids = [s.get("id") for s in _steps("route")]
        assert ids.index("decide") < ids.index(GUARD_STEP_ID)

    def test_guard_only_runs_when_the_farm_queue_asked_for_cnb(self):
        assert _guard_step()["if"] == "steps.decide.outputs.decision == 'cnb'"

    def test_cnb_job_requires_both_the_decision_and_the_budget(self):
        cond = _doc()["jobs"]["cnb"]["if"]
        assert "needs.route.outputs.decision == 'cnb'" in cond
        assert "needs.route.outputs.budget == 'allow'" in cond

    def test_the_budget_condition_has_no_escape_hatch(self):
        """`|| budget == ''` would fail OPEN whenever the guard did not run (mutation M1)."""
        cond = _doc()["jobs"]["cnb"]["if"]
        assert "||" not in cond, f"the cnb gate must be a plain conjunction, got: {cond!r}"
        assert cond.strip() == ("needs.route.outputs.decision == 'cnb' "
                               "&& needs.route.outputs.budget == 'allow'")

    def test_the_guard_step_is_wired_to_the_token_the_input_and_the_cap(self):
        """Dropping any of these turns the guard into a no-op that the suite used to miss."""
        env = _guard_step()["env"]
        assert env["CNB_TOKEN"] == "${{ secrets.CNB_TOKEN }}"
        assert env["BUDGET_PCT"] == "${{ inputs.build_budget_pct }}"
        assert env["FORCE_CNB"] == "${{ inputs.force_cnb }}"
        assert env["CHARGE_URL"] == "https://api.cnb.cool/celestia-island/-/charge/volume"
        assert env["BUILD_CAP_H"] == "160", "the cap is the quota being protected: 160 core-h"

    def test_the_guard_cannot_be_switched_off_by_an_empty_step_env(self):
        for key, val in _guard_step()["env"].items():
            assert val != "", f"{key} is empty: the step would fall back to a default"

    def test_route_job_exports_the_budget_outputs(self):
        outs = _doc()["jobs"]["route"]["outputs"]
        assert outs["budget"] == "${{ steps.budget.outputs.budget }}"
        assert "budget_reason" in outs

    def test_budget_default_is_conservative_and_leaves_headroom(self):
        for on in ("workflow_call", "workflow_dispatch"):
            d = _doc()[True][on]["inputs"]["build_budget_pct"]["default"]
            assert 0 < d < 100, f"{on} default must stop before the hard cap"

    def test_threshold_input_still_governs_the_farm_queue(self):
        assert _doc()[True]["workflow_call"]["inputs"]["queue_threshold"]["default"] == 3


class TestAllowPath:
    def test_fresh_pool_allows_dispatch(self, tmp_path, ledger):
        out, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0)
        assert (rc, got["budget"]) == (0, "allow")
        assert "1.00/160" in out

    def test_just_below_the_threshold_allows(self, tmp_path, ledger):
        _, got, _ = run_guard(tmp_path, ledger, used_core_h=0.7 * CAP_H - 0.01, pct="70")
        assert got["budget"] == "allow"

    def test_missing_pct_env_falls_back_to_the_shipped_default(self, tmp_path, ledger):
        out, got, _ = run_guard(tmp_path, ledger, used_core_h=100.0, pct="")
        assert got["budget"] == "allow"
        assert "70%" in out

    def test_forced_dispatch_bypasses_the_guard_loudly(self, tmp_path, ledger):
        out, got, _ = run_guard(tmp_path, ledger, used_core_h=CAP_H, force="true")
        assert got["budget"] == "allow"
        assert "force_cnb" in out


class TestSkipPath:
    def test_a_fresh_month_reads_as_zero_used_and_allows(self, tmp_path, ledger):
        """`if not led.get(...)` would mistake a zero balance for an unreadable ledger (M2)."""
        out, got, _ = run_guard(tmp_path, ledger, used_core_h=0.0, pct="70")
        assert got["budget"] == "allow"
        assert "0.00/160" in out

    def test_inflight_prefreeze_counts_towards_the_budget(self, tmp_path, ledger):
        """A month whose quota is already pre-frozen for in-flight pipelines is not a fresh pool."""
        _Ledger.payload = {"ci_in_sec": 0, "freeze_ci_in_sec": int(0.9 * CAP_H * 3600)}
        _, got, _ = run_guard(tmp_path, ledger, pct="70")
        assert got["budget"] == "skip"

    def test_at_the_threshold_it_skips(self, tmp_path, ledger):
        out, got, _ = run_guard(tmp_path, ledger, used_core_h=0.7 * CAP_H)
        assert got["budget"] == "skip"
        assert "112.00/160" in out

    def test_over_the_hard_cap_it_skips_even_at_100_pct(self, tmp_path, ledger):
        _, got, _ = run_guard(tmp_path, ledger, used_core_h=CAP_H + 5, pct="100")
        assert got["budget"] == "skip"

    def test_skip_reason_names_the_measured_usage(self, tmp_path, ledger):
        out, got, _ = run_guard(tmp_path, ledger, used_core_h=140.0)
        assert "140.00/160" in got["budget_reason"]
        assert "87.5%" in got["budget_reason"]
        assert "skip" in out

    def test_lower_pct_is_honoured(self, tmp_path, ledger):
        _, got, _ = run_guard(tmp_path, ledger, used_core_h=20.0, pct="10")
        assert got["budget"] == "skip"


class TestFailsClosed:
    def test_ledger_500_skips(self, tmp_path, ledger):
        _Ledger.status = 500
        out, got, _ = run_guard(tmp_path, ledger, pct="70")
        assert got["budget"] == "skip"
        assert "failing closed" in out

    def test_unreachable_ledger_skips(self, tmp_path):
        out, got, _ = run_guard(tmp_path, "http://127.0.0.1:9/charge", pct="70")
        assert got["budget"] == "skip"
        assert "failing closed" in out

    def test_non_json_body_skips(self, tmp_path, ledger):
        _Ledger.payload = {"ci_in_sec": "not-a-number"}
        _, got, _ = run_guard(tmp_path, ledger, pct="70")
        assert got["budget"] == "skip"

    def test_renamed_ledger_field_must_not_read_as_zero_used(self, tmp_path, ledger):
        """A shape change in the charge API is the one way this guard could silently fail open."""
        _Ledger.payload = {"build_in_sec": 10}
        out, got, _ = run_guard(tmp_path, ledger, pct="70")
        assert got["budget"] == "skip"
        assert "no ci_in_sec" in out

    def test_absent_field_is_not_treated_as_a_fresh_pool(self, tmp_path, ledger):
        _Ledger.payload = {}
        _, got, _ = run_guard(tmp_path, ledger, pct="70")
        assert got["budget"] == "skip"

    def test_missing_token_skips_without_calling_out(self, tmp_path, ledger):
        out, got, _ = run_guard(tmp_path, ledger, auth_token=None, used_core_h=1.0)
        assert got["budget"] == "skip"
        assert "CNB_TOKEN missing" in out
        assert _Ledger.seen_auth == [], "must not call the ledger without a token"

    def test_unusable_pct_skips(self, tmp_path, ledger):
        _, got, _ = run_guard(tmp_path, ledger, pct="not-a-number")
        assert got["budget"] == "skip"

    @pytest.mark.parametrize("pct", ["1e308", "inf", "-inf", "nan", "0", "-1", "101", "1000"])
    def test_a_threshold_outside_the_sane_range_is_unusable(self, tmp_path, ledger, pct):
        """These are all valid YAML floats a caller could pass; none may widen the guard."""
        _, got, _ = run_guard(tmp_path, ledger, used_core_h=0.95 * CAP_H, pct=pct)
        assert got["budget"] == "skip", f"pct={pct} must not allow at 95% usage"

    def test_the_hard_cap_threshold_is_still_a_policy(self, tmp_path, ledger):
        """100 means "only refuse at the cap" — legal, and it must really allow below it."""
        _, got, _ = run_guard(tmp_path, ledger, used_core_h=0.99 * CAP_H, pct="100")
        assert got["budget"] == "allow"

    def test_guard_always_exits_zero_so_it_cannot_fail_the_route_job(self, tmp_path, ledger):
        _Ledger.status = 503
        _, _, rc = run_guard(tmp_path, ledger, pct="70")
        assert rc == 0


class TestHygiene:
    def test_token_is_never_echoed(self, tmp_path, ledger):
        for used in (1.0, 140.0):
            out, _, _ = run_guard(tmp_path, ledger, used_core_h=used, auth_token="tok-secret-value")
            assert "tok-secret-value" not in out

    def test_token_is_sent_as_a_bearer_header(self, tmp_path, ledger):
        run_guard(tmp_path, ledger, used_core_h=1.0, auth_token="tok-secret-value")
        assert _Ledger.seen_auth == ["Bearer tok-secret-value"]

    def test_guard_does_not_post_any_status(self):
        """The skip path must not reach for the statuses API: no green we did not earn."""
        script = _guard_script()
        assert "statuses" not in script
        assert "api.github.com" not in script

    def test_outputs_are_written_even_without_a_summary_file(self, tmp_path, ledger):
        _, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0,
                               extra_env={"GITHUB_STEP_SUMMARY": ""})
        assert (rc, got["budget"]) == (0, "allow")

    def test_guard_survives_a_missing_github_output(self, tmp_path, ledger):
        out, _, rc = run_guard(tmp_path, ledger, used_core_h=1.0, outputs=False)
        assert rc == 0
        assert "build-pool budget: allow" in out
