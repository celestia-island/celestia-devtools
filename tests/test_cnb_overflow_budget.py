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
    raw: bytes | None = None      # served verbatim, for malformed/non-ASCII status lines

    def do_GET(self):                                                  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        if self.raw is not None:
            self.wfile.write(self.raw)
            return
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
    _Ledger.payload, _Ledger.status, _Ledger.seen_auth, _Ledger.raw = {}, 200, [], None
    yield f"http://127.0.0.1:{srv.server_address[1]}/charge"
    srv.shutdown()


def run_guard(tmp_path, ledger_url, *, used_core_h=None, pct="70", auth_token="tok-secret-value",
              force=None, extra_env=None, cap=None, outputs=True):
    """Execute the shipped guard; return (stdout+stderr, outputs-dict, exit code)."""
    env = {k: v for k, v in os.environ.items() if k not in ("BUDGET_PCT", "FORCE_CNB", "CNB_TOKEN")}
    env["CHARGE_URL"] = ledger_url
    env["BUDGET_PCT"] = pct
    env["BUILD_CAP_H"] = str(cap if cap is not None else CAP_H)  # cap may be a junk string
    if auth_token is not None:
        env["CNB_TOKEN"] = auth_token
    if force is not None:
        env["FORCE_CNB"] = force
    if used_core_h is not None:
        _Ledger.payload = {"ci_in_sec": int(used_core_h * 3600), "dev_in_sec": 0}
    out_file = tmp_path / "gh_output"
    out_file.write_text("")
    if outputs == "directory":
        out_file.unlink()
        out_file.mkdir()
        env["GITHUB_OUTPUT"] = str(out_file)
    elif outputs:
        env["GITHUB_OUTPUT"] = str(out_file)
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-"], input=_guard_script(), capture_output=True,
                       text=True, env=env, timeout=60)
    got = ({} if out_file.is_dir() else
           dict(line.split("=", 1) for line in out_file.read_text().splitlines() if "=" in line))
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

    def test_the_guard_step_is_the_only_writer_of_the_budget_output(self):
        """A second step emitting `budget=allow` would otherwise override the guard silently."""
        ids = [s.get("id") for s in _steps("route")]
        assert ids.count(GUARD_STEP_ID) == 1, f"duplicate guard step ids: {ids}"
        writers = [s.get("id") or s.get("name") for s in _steps("route")
                   if "budget=" in (s.get("run") or "")]
        assert writers == [GUARD_STEP_ID], f"more than one step can grant the budget: {writers}"
        # A grant has to reach GITHUB_OUTPUT, so a writer is any step that touches that file
        # *and* mentions budget — which catches one that builds the string without a literal
        # `budget=` (the decide step writes GITHUB_OUTPUT but never touches the budget).
        touchers = [s.get("id") or s.get("name") for s in _steps("route")
                    if "GITHUB_OUTPUT" in (s.get("run") or "")
                    and "budget" in (s.get("run") or "")]
        assert touchers == [GUARD_STEP_ID], f"another step writes a budget output: {touchers}"

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

    @pytest.mark.parametrize("payload", [
        {"ci_in_sec": -1},                       # a negative counter is not a fresh pool
        {"ci_in_sec": -1e12},
        {"ci_in_sec": float("-inf")},
        {"ci_in_sec": True},                     # float(True) == 1.0
        {"ci_in_sec": False},
        {"ci_in_sec": None},
        {"ci_in_sec": 1, "freeze_ci_in_sec": -1e12},
        {"ci_in_sec": 1, "freeze_ci_in_sec": {}},   # present but unreadable, not silently 0
        {"ci_in_sec": 1, "freeze_ci_in_sec": []},
        {"ci_in_sec": 1, "freeze_ci_in_sec": ""},
        {"ci_in_sec": 1, "freeze_ci_in_sec": "abc"},
    ], ids=["negative", "very-negative", "-inf", "true", "false", "null",
            "negative-freeze", "freeze-dict", "freeze-list", "freeze-empty", "freeze-text"])
    def test_parseable_but_nonsensical_counters_fail_closed(self, tmp_path, ledger, payload):
        _Ledger.payload = payload
        out, got, rc = run_guard(tmp_path, ledger, pct="70")
        assert (rc, got["budget"]) == (0, "skip"), f"{payload} must not read as a fresh pool"
        assert "failing closed" in out

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
        _, got, rc = run_guard(tmp_path, ledger, pct="70")
        assert rc == 0
        assert got["budget"] == "skip"          # ...and it still produced a verdict
        assert _Ledger.seen_auth                 # ...after really asking the ledger


class TestHygiene:
    def test_token_is_never_echoed(self, tmp_path, ledger):
        for used in (1.0, 140.0):
            out, got, _ = run_guard(tmp_path, ledger, used_core_h=used,
                                    auth_token="tok-secret-value")
            assert "tok-secret-value" not in out
            # ...and the guard really ran: an absence assertion alone also passes if it did not.
            assert got.get("budget") in ("allow", "skip")
        assert set(_Ledger.seen_auth) == {"Bearer tok-secret-value"}

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

    def test_an_unwritable_github_output_warns_and_still_exits_zero(self, tmp_path, ledger):
        """An empty output skips the cnb job (fail-closed); it must not redden the step."""
        out, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0, outputs="directory")
        assert rc == 0
        assert got == {}, "no output must be produced when it cannot be written"
        assert "could not write GITHUB_OUTPUT" in out

    def test_a_summary_path_that_is_a_directory_still_exits_zero(self, tmp_path, ledger):
        out, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0,
                                 extra_env={"GITHUB_STEP_SUMMARY": str(tmp_path)})
        assert (rc, got["budget"]) == (0, "allow")
        assert "could not write the step summary" in out

    def test_the_shipped_program_is_ascii_only(self):
        """Executed strings must be ASCII: they are printed to CI logs under whatever locale."""
        offenders = [(i + 1, line.strip()[:60])
                     for i, line in enumerate(_guard_script().splitlines())
                     if any(ord(ch) > 127 for ch in line)]
        assert not offenders, f"non-ASCII in the shipped guard program: {offenders}"

    def test_a_non_ascii_reason_cannot_redden_the_step(self, tmp_path, ledger):
        """UnicodeEncodeError is a ValueError, so `except OSError` alone would let it escape."""
        _Ledger.payload = {"ci_in_sec": "日本語"}
        out, got, rc = run_guard(tmp_path, ledger, pct="70", extra_env={
            "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"})
        assert (rc, got["budget"]) == (0, "skip")
        assert "failing closed" in got["budget_reason"] or "failing closed" in out

    def test_a_non_ascii_exception_text_cannot_redden_the_step(self, tmp_path, ledger):
        """The reason embeds the exception, so a non-ASCII one must be escaped before printing.

        A `/日本語` path is *not* enough: the UnicodeEncodeError it raises has ASCII text, so the
        assertion would hold trivially.  A raw status line carrying a latin-1 byte does make
        `HTTPError.str()` non-ASCII, and `print()` of it on an ASCII stdout raises.
        """
        _Ledger.raw = b"HTTP/1.0 503 caf\xe9\r\nContent-Length: 0\r\n\r\n"
        out, got, rc = run_guard(tmp_path, ledger, pct="70", extra_env={
            "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"})
        assert (rc, got["budget"]) == (0, "skip"), out[-300:]
        assert "caf" in got["budget_reason"], got["budget_reason"]
        assert all(ord(ch) < 128 for ch in out + got["budget_reason"]), "printed a non-ASCII byte"

    def test_an_unusable_cap_skips_instead_of_crashing(self, tmp_path, ledger):
        out, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0, cap="abc")
        assert (rc, got["budget"]) == (0, "skip")
        assert "BUILD_CAP_H" in out

    def test_a_nonpositive_cap_skips(self, tmp_path, ledger):
        _, got, rc = run_guard(tmp_path, ledger, used_core_h=1.0, cap="0")
        assert (rc, got["budget"]) == (0, "skip")
