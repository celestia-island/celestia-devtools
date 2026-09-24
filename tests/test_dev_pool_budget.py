#!/usr/bin/env python3
"""Tests for the dev-pool monitor and budget gate in ``ci_dispatcherd``.

*Why this exists:* every dev-quota lane run is billed to the org's `云原生开发` pool (1600 free
core-hours/month), and that quota is a capability limit — the docs say the capability is
restricted once it is used up, and a pre-freeze with insufficient quota terminates the task. A
spill attempted at exhaustion therefore fails ``workspace/start`` and, after the retry budget,
**falls back to the build lane**: the failure spends the very pool the lane exists to protect.
The daemon now reads the org charge ledger (``dev_in_sec`` settled + ``freeze_dev_in_sec``
in-flight) and, at or above ``DISPATCH_DEV_BUDGET_PCT`` (default 80), defers the spill — the run
stays queued on the farm, exactly like a same-repo deferral, and no quota is spent.

These tests pin the three properties that matter:

* the gate refuses at the boundary and never spends quota above the line,
* it never fails *open*: an unreadable ledger with no usable reading defers, and nonsense values
  (`-1`, `true`, `inf`, a renamed field) are not read as a fresh pool,
* the monitor is honest — one log line per refresh carrying the number the gate acts on, read
  directly rather than through the proxy that crawls cnb.cool.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
import urllib.request

import pytest

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ci_dispatcherd.py")
spec = importlib.util.spec_from_file_location("ci_dispatcherd_devpool", TOOL)
dsp = importlib.util.module_from_spec(spec)
sys.modules["ci_dispatcherd_devpool"] = dsp
spec.loader.exec_module(dsp)

CAP_H = 1600.0


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    """No reading cached between tests, and no real HTTP."""
    dsp._pool_reading.update(t=0.0, attempt=0.0, dev=None, dev_pct=None,
                             build=None, build_pct=None, fresh=False)
    yield
    dsp._pool_reading.update(t=0.0, attempt=0.0, dev=None, dev_pct=None,
                             build=None, build_pct=None, fresh=False)


def ledger(dev_hours=0.0, freeze_hours=0.0, build_hours=0.0, freeze_build_hours=0.0):
    return {"dev_in_sec": int(dev_hours * 3600), "freeze_dev_in_sec": int(freeze_hours * 3600),
            "ci_in_sec": int(build_hours * 3600),
            "freeze_ci_in_sec": int(freeze_build_hours * 3600)}


@pytest.fixture
def ws_env(monkeypatch):
    """A spill that would take the dev-quota path, with the network stubbed out."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    monkeypatch.setattr(dsp, "log", lambda *_: None)
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror", lambda r, sha: "e" * 40)
    monkeypatch.setattr(dsp, "publish_lane_ref", lambda r, sha, host=None: (f"spill/{sha[:10]}", True))
    started = []
    monkeypatch.setattr(dsp, "ws_start", lambda r, ref: (started.append(r), {"sn": "s"})[1])
    monkeypatch.setattr(dsp, "cnb_api", lambda path, data=None: {"sn": "sn-build"})
    return started


def spill(repo="hikari", run_id=91):
    state = {}
    result = dsp.spill({"repo": repo, "run_id": run_id, "sha": "b" * 40}, state)
    return result, state


class TestMonitor:
    def test_logs_the_trend_line_with_the_number_the_gate_acts_on(self, monkeypatch):
        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=80.0))
        hours, pct, fresh = dsp.dev_pool(refresh=True)
        assert (hours, pct, fresh) == (80.0, 5.0, True)
        assert seen == [f"pools dev 80.0/{CAP_H:.0f} core-h = 5.0% "
                        f"(limit {dsp.DEV_BUDGET_PCT}%) | build 0.0/{dsp.BUILD_CAP_H} "
                        f"= 0.0% (limit {dsp.BUILD_BUDGET_PCT}%)"]

    def test_the_reading_is_cached_for_the_poll_window(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (calls.append(1), ledger(dev_hours=10.0))[1])
        dsp.dev_pool()
        dsp.dev_pool()
        assert len(calls) == 1, "a second read inside DEV_POLL_SEC must reuse the cached value"
        dsp.dev_pool(refresh=True)
        assert len(calls) == 2

    def test_the_inflight_freeze_counts_towards_the_budget(self, monkeypatch):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(0.0, freeze_hours=800.0))
        hours, pct, _ = dsp.dev_pool(refresh=True)
        assert (hours, pct) == (800.0, 50.0)

    def test_an_unreadable_ledger_keeps_the_last_good_reading_while_it_is_fresh_enough(
            self, monkeypatch):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=100.0))
        dsp.dev_pool(refresh=True)
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        hours, pct, fresh = dsp.dev_pool(refresh=True)
        assert (hours, pct) == (100.0, 6.25), "a stale reading is the best protection available"
        assert fresh is False

    def test_a_reading_older_than_the_stale_window_is_not_used(self, monkeypatch):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=100.0))
        dsp.dev_pool(refresh=True)
        dsp._pool_reading["t"] -= dsp.DEV_STALE_SEC + 1
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert dsp.dev_pool() == (None, None, False)

    def test_a_failed_read_is_not_retried_on_every_call(self, monkeypatch):
        """Both verifiers measured the same defect: with a dying ledger every deferring spill
        re-attempted a blocking read, which could push resolve() (the cancel-green step that
        frees farm slots) out by ~an hour on a deep queue."""
        attempts = []

        def dying():
            attempts.append(1)
            raise RuntimeError("blackhole")

        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "charge_volume", dying)
        for _ in range(50):
            dsp.dev_pool()
        assert len(attempts) == 1, f"50 calls must cost one attempt, got {len(attempts)}"
        assert sum("charge ledger unreadable" in line for line in seen) == 1

    def test_the_negative_cache_expires(self, monkeypatch):
        attempts = []

        def dying():
            attempts.append(1)
            raise RuntimeError("blackhole")

        monkeypatch.setattr(dsp, "log", lambda *_: None)
        monkeypatch.setattr(dsp, "charge_volume", dying)
        dsp.dev_pool()
        dsp._pool_reading["attempt"] -= dsp.DEV_POLL_SEC + 1
        dsp.dev_pool()
        assert len(attempts) == 2

    def test_a_failed_attempt_does_not_make_the_last_good_reading_look_fresh(self, monkeypatch):
        """`attempt` must stay separate from `t`: advancing `t` on failure would extend the
        usable window of a stale number, which is fail-open."""
        monkeypatch.setattr(dsp, "log", lambda *_: None)
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=100.0))
        dsp.pools(refresh=True)
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        dsp._pool_reading["t"] -= dsp.DEV_STALE_SEC + 1     # the last good read is too old
        dsp.pools(refresh=True)                             # the failure must not revive it
        assert dsp.dev_pool() == (None, None, False)

    def test_the_ledger_is_read_directly_not_through_the_proxy(self, monkeypatch):
        """cnb.cool must be reached directly from this node (AGENTS §8.1); through the proxy it
        crawls at ~13 KB/s and the guard would time out instead of protecting anything."""
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"dev_in_sec": 0}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                captured["url"] = req.full_url
                return FakeResp()

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return FakeOpener()

        # A proxy in the environment is what makes ProxyHandler() and ProxyHandler({}) differ;
        # without it this test passes either way (found by mutation).
        monkeypatch.setenv("HTTPS_PROXY", "http://daemon.node.local:7890")
        monkeypatch.setenv("HTTP_PROXY", "http://daemon.node.local:7890")
        monkeypatch.setattr(dsp.urllib.request, "build_opener", fake_build_opener)
        assert dsp.charge_volume() == {"dev_in_sec": 0}
        assert captured["url"].endswith("/celestia-island/-/charge/volume")
        assert isinstance(captured["handlers"][0], urllib.request.ProxyHandler)
        assert captured["handlers"][0].proxies == {}, "the proxy handler must be emptied"


class TestGate:
    def test_a_healthy_pool_spills(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=1.0))
        result, state = spill()
        assert result is None and state["91"]["mode"] == "ws"
        assert ws_env == ["ci-infra-hikari"]

    def test_at_the_limit_it_defers(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "DEV_BUDGET_PCT", 80)
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=CAP_H * 0.8))
        result, state = spill()
        assert result is False, "a deferral must be reported as False so the batch is not spent"
        assert state == {} and ws_env == [], "no quota may be spent at the line"

    def test_above_the_limit_it_defers(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=CAP_H * 0.95))
        result, state = spill()
        assert result is False and state == {} and ws_env == []

    def test_it_does_not_even_reach_the_mirror_when_deferring(self, monkeypatch, ws_env):
        """The gate sits before the mirror work (~990 s of budget) so a refusal is cheap."""
        touched = []
        monkeypatch.setattr(dsp, "ensure_sha_on_mirror",
                            lambda r, sha: (touched.append(1), "e" * 40)[1])
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=CAP_H))
        assert spill()[0] is False
        assert touched == []

    def test_the_freeze_alone_can_trip_the_gate(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(0.0, freeze_hours=CAP_H))
        assert spill()[0] is False

    def test_an_unknown_usage_defers_instead_of_risking_a_build_lane_fallback(
            self, monkeypatch, ws_env):
        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("503")))
        result, state = spill()
        assert result is False and state == {} and ws_env == []
        assert any("usage unknown" in line for line in seen)

    def test_a_stale_reading_still_allows_the_spill_but_says_so(self, monkeypatch, ws_env):
        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=10.0))
        dsp.dev_pool(refresh=True)                       # cache a good reading...
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        dsp._pool_reading["t"] -= dsp.DEV_POLL_SEC + 1        # ...let it age past the poll window,
        dsp._pool_reading["attempt"] -= dsp.DEV_POLL_SEC + 1  # ...and let the retry be due again
        result, state = spill()
        assert result is None and state["91"]["mode"] == "ws"
        assert any("stale" in line for line in seen)

    def test_a_stale_reading_over_the_limit_still_defers(self, monkeypatch, ws_env):
        """Stale means "keep using the number", not "ignore it" — and the log must say so."""
        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=CAP_H * 0.99))
        dsp.dev_pool(refresh=True)
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        dsp._pool_reading["t"] -= dsp.DEV_POLL_SEC + 1
        dsp._pool_reading["attempt"] -= dsp.DEV_POLL_SEC + 1
        assert spill()[0] is False and ws_env == []
        assert any("stale reading" in line for line in seen), seen

    def test_the_dev_gate_only_applies_to_dev_quota_repos(self, monkeypatch, ws_env):
        """A repo outside DEV_QUOTA goes to the build lane on a healthy ledger."""
        monkeypatch.setattr(dsp, "DEV_QUOTA", set())
        started = []
        monkeypatch.setattr(dsp, "cnb_api",
                            lambda path, data=None: (started.append(path), {"sn": "sn-build"})[1])
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger())
        result, state = spill(repo="some-other-repo", run_id=55)
        assert result is None and state["55"]["mode"] == "build" and started


class TestPolicyAndWiring:
    def test_the_shipped_defaults_are_the_agreed_policy_numbers(self):
        """These are policy: dev 1600 core-hours/month with a line at 80%, build 160 with the
        same 70% ceiling the caller-lane router uses, and a 5-minute reading cadence."""
        assert (dsp.DEV_CAP_H, dsp.DEV_BUDGET_PCT) == (1600, 80)
        assert (dsp.BUILD_CAP_H, dsp.BUILD_BUDGET_PCT) == (160, 70)
        assert dsp.DEV_POLL_SEC == 300
        assert dsp.DEV_STALE_SEC >= dsp.DEV_POLL_SEC

    def test_a_poll_window_longer_than_the_stale_window_is_clamped(self):
        """Otherwise a healthy ledger reads as unknown between refreshes and the lane goes dark."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,sys;"
             f"s=importlib.util.spec_from_file_location('x',{TOOL!r});"
             "m=importlib.util.module_from_spec(s);sys.modules['x']=m;s.loader.exec_module(m);"
             "print(m.DEV_POLL_SEC, m.DEV_STALE_SEC)"],
            capture_output=True, text=True,
            env={**os.environ, "DISPATCH_DEV_POLL_SEC": "86400", "DISPATCH_DEV_STALE_SEC": "900"},
            timeout=60)
        assert "WARN" in out.stderr and "DISPATCH_DEV_POLL_SEC" in out.stderr
        assert out.stdout.split() == ["900", "900"]

    def test_lane_host_extra_env_extends_the_pool(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,sys;"
             f"s=importlib.util.spec_from_file_location('x',{TOOL!r});"
             "m=importlib.util.module_from_spec(s);sys.modules['x']=m;s.loader.exec_module(m);"
             "print('|'.join(m.lane_hosts('hikari')))"],
            capture_output=True, text=True,
            env={**os.environ, "DISPATCH_LANE_HOST_EXTRA":
                 "ci-infra-{repo}-b, ci-infra-{repo}-c,,ci-infra-{repo}-2"}, timeout=60)
        assert out.stdout.split() == ["ci-infra-hikari|ci-infra-hikari-b|"
                                      "ci-infra-hikari-c|ci-infra-hikari-2"]

    def test_shipped_default_pool_is_three_hosts_per_repo(self):
        """User directive 2026-09-25: every watched repo gets at least three lane instances.

        The first instance keeps the plain name and every later one gets a `-<n>` suffix
        counting from 2; the default must ship all of them or a third same-repo run defers
        again even though its host exists on cnb.cool (the hosts are created out-of-band,
        so only this default turns them into concurrency).
        """
        out = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,sys;"
             f"s=importlib.util.spec_from_file_location('x',{TOOL!r});"
             "m=importlib.util.module_from_spec(s);sys.modules['x']=m;s.loader.exec_module(m);"
             "print('|'.join(m.lane_hosts('hikari')))"],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != "DISPATCH_LANE_HOST_EXTRA"},
            timeout=60)
        assert out.stdout.strip() == "ci-infra-hikari|ci-infra-hikari-2|ci-infra-hikari-3"

    def test_the_main_loop_logs_the_trend_line_even_when_nothing_spills(self, monkeypatch):
        """Driven, not grepped: a source check let `pass  # pools()` survive (found by mutation).

        The trend line is the whole monitor — without a read in the loop there is no visibility
        on a quiet night, which is exactly when a pool can drift toward its line unnoticed.
        """
        seen = []
        monkeypatch.setattr(dsp, "log", seen.append)
        monkeypatch.setattr(dsp, "GH", "gh")
        monkeypatch.setattr(dsp, "CNB", "cnb")
        monkeypatch.setattr(dsp, "FETCH", "fetch")
        monkeypatch.setattr(dsp, "CNB_WS", "ws")
        monkeypatch.setattr(dsp, "charge_volume", lambda: ledger(dev_hours=42.0, build_hours=99.0))
        monkeypatch.setattr(dsp, "load_state", lambda: {})
        monkeypatch.setattr(dsp, "census", lambda: [])
        monkeypatch.setattr(dsp, "save_state", lambda st: None)
        monkeypatch.setattr(dsp, "resolve", lambda st: None)

        class Stop(Exception):
            pass

        def one_iteration(_sec):
            raise Stop

        monkeypatch.setattr(dsp.time, "sleep", one_iteration)
        with pytest.raises(Stop):
            dsp.main()
        trend = [line for line in seen if line.startswith("pools dev ")]
        assert trend, f"the loop must log the pools line with an empty queue; got {seen}"
        assert "dev 42.0/1600" in trend[0] and "build 99.0/160" in trend[0]


class TestBusyHosts:
    def test_only_tracked_dev_quota_workspaces_block_a_host(self):
        """P3 (round-A verifier): without the mode/host filter, a build or legacy record keyed
        by repo could shadow a host name and make free_host defer on an idle pool."""
        state = {
            "_recent": {"a" * 40: 1.0},                                    # bookkeeping: never counts
            "1": {"mode": "ws", "host": "ci-infra-hikari", "repo": "hikari",
                  "sha": "a" * 40, "url": "", "since": 0.0},               # counts
            "2": {"mode": "build", "host": "ci-infra-hikari-2", "repo": "hikari",
                  "sha": "b" * 40, "url": "", "since": 0.0},               # build: never counts
            "3": {"mode": "ws", "repo": "hikari", "sha": "c" * 40,
                  "url": "", "since": 0.0},                                # legacy ws: no host
            "4": {"mode": "ws", "host": "ci-infra-plana", "repo": "plana",
                  "sha": "d" * 40, "url": "", "since": 0.0},               # another repo: counts
        }
        assert dsp.busy_hosts(state) == {"ci-infra-hikari", "ci-infra-plana"}


class TestPublishLaneRef:
    def test_the_ref_lands_on_the_selected_host(self, monkeypatch, tmp_path):
        """`publish-ignores-the-selected-host`: the host parameter is the whole point."""
        recorded = []
        monkeypatch.setattr(dsp, "GITCACHE", str(tmp_path))
        monkeypatch.setattr(dsp, "CNB", "cnb-token-fake")
        monkeypatch.setattr(dsp, "log", lambda *_: None)
        fake = types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        def fake_run(argv, **kwargs):
            recorded.append(list(argv))
            return fake

        monkeypatch.setattr(subprocess, "run", fake_run)
        branch, ok = dsp.publish_lane_ref("hikari", "b" * 40, host="ci-infra-hikari-2")
        assert (branch, ok) == ("spill/" + "b" * 10, True)
        pushes = [" ".join(argv) for argv in recorded if "push" in argv]
        assert pushes, recorded
        assert any("ci-infra-hikari-2" in p for p in pushes), pushes
        assert not any("celestia-island/hikari.git" in p for p in pushes), \
            "the spill ref must not be published to the target repo"


class TestLedgerParsing:
    @pytest.mark.parametrize("led,key,required", [
        ({}, "dev_in_sec", True),
        ({"dev_in_sec": "12"}, "dev_in_sec", True),
        ({"dev_in_sec": True}, "dev_in_sec", True),
        ({"dev_in_sec": -1}, "dev_in_sec", True),
        ({"dev_in_sec": float("inf")}, "dev_in_sec", True),
        ({"dev_in_sec": 1, "freeze_dev_in_sec": -1}, "freeze_dev_in_sec", False),
        ({"dev_in_sec": 1, "freeze_dev_in_sec": {}}, "freeze_dev_in_sec", False),
        ({"dev_in_sec": 1, "freeze_dev_in_sec": "x"}, "freeze_dev_in_sec", False),
    ], ids=["missing-required", "string", "bool", "negative", "inf", "negative-freeze",
            "freeze-dict", "freeze-string"])
    def test_nonsense_counters_are_refused(self, led, key, required):
        with pytest.raises(ValueError):
            dsp.core_seconds(led, key, required)

    def test_an_absent_optional_field_is_zero(self):
        assert dsp.core_seconds({"dev_in_sec": 5}, "freeze_dev_in_sec", False) == 0.0

    def test_a_renamed_field_does_not_read_as_a_fresh_pool(self, monkeypatch, ws_env):
        """Only the dev field may be renamed here — a missing `ci_in_sec` would fail the whole
        read first and mask what this test is about."""
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: {"dev_usage": 999, "ci_in_sec": 0, "freeze_ci_in_sec": 0})
        assert spill()[0] is False, "a renamed field must defer, not spill at 0%"


class TestBuildPoolFallbackGate:
    """The fallback path spends the scarce pool, so it is held to the caller-lane router's line."""

    def test_the_fallback_is_refused_above_the_build_line(self, monkeypatch, ws_env):
        built = []
        monkeypatch.setattr(dsp, "cnb_api",
                            lambda path, data=None: (built.append(path), {"sn": "sn-build"})[1])
        monkeypatch.setattr(dsp, "DEV_QUOTA", set())          # force the fallback path
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: ledger(build_hours=dsp.BUILD_CAP_H * 0.71))
        result, state = spill(repo="outside-quota", run_id=61)
        assert result is False and state == {} and built == [], "no build core-hours may be spent"

    def test_the_fallback_still_works_below_the_line(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "DEV_QUOTA", set())
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: ledger(build_hours=dsp.BUILD_CAP_H * 0.5))
        result, state = spill(repo="outside-quota", run_id=62)
        assert result is None and state["62"]["mode"] == "build"

    def test_the_real_fallback_from_a_dev_lane_repo_is_gated(self, monkeypatch, ws_env):
        """The production shape: the repo IS in DEV_QUOTA (WATCHED == DEV_QUOTA) and its
        workspace start fails, so the spill would fall through to the build lane. Scoping the
        build gate to repos *outside* DEV_QUOTA survived the suite and would spend build
        core-hours above the line (found by the round-A verifier)."""
        built = []
        monkeypatch.setattr(dsp, "cnb_api",
                            lambda path, data=None: (built.append(path), {"sn": "sn-build"})[1])

        def broken_start(host, ref):
            raise RuntimeError("workspace/start refused")

        monkeypatch.setattr(dsp, "ws_start", broken_start)
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: ledger(dev_hours=1.0, build_hours=dsp.BUILD_CAP_H * 0.71))
        result, state = spill(repo="hikari", run_id=77)
        assert result is False and state == {} and built == [], \
            "a dev-lane fallback must not spend build core-hours above the line"

    def test_the_real_fallback_from_a_dev_lane_repo_still_works_below_the_line(
            self, monkeypatch, ws_env):
        built = []
        monkeypatch.setattr(dsp, "cnb_api",
                            lambda path, data=None: (built.append(path), {"sn": "sn-build"})[1])
        monkeypatch.setattr(dsp, "ws_start",
                            lambda host, ref: (_ for _ in ()).throw(RuntimeError("nope")))
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: ledger(dev_hours=1.0, build_hours=dsp.BUILD_CAP_H * 0.5))
        result, state = spill(repo="hikari", run_id=78)
        assert result is None and state["78"]["mode"] == "build" and built

    def test_an_unreadable_ledger_defers_the_fallback_too(self, monkeypatch, ws_env):
        monkeypatch.setattr(dsp, "DEV_QUOTA", set())
        monkeypatch.setattr(dsp, "charge_volume",
                            lambda: (_ for _ in ()).throw(RuntimeError("503")))
        result, state = spill(repo="outside-quota", run_id=63)
        assert result is False and state == {}
