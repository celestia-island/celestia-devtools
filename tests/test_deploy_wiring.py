"""Wiring-slice tests: the remaining stages, verify/status/uninstall, and
the §1.8 rotation gate — all rootless via injected executors/HTTP."""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from unittest import mock
import pytest
from celestia_devtools.deploy import bootstrap, ops, stages_more
from celestia_devtools.deploy.artifact import build as artifact_build
from celestia_devtools.deploy.profile import (deploy_profile, front_section, host_section)
from celestia_devtools.deploy.artifact import write_index, scan_dir

_STAGES_SNAPSHOT = dict(bootstrap.STAGES)

@pytest.fixture(autouse = True)
def _register_and_restore_stages():
    """Complete the stage machine for this test, then restore the
    pending-slice contract so non-wiring tests are unaffected."""
    stages_more.register_stages()
    yield
    bootstrap.STAGES.clear()
    bootstrap.STAGES.update(_STAGES_SNAPSHOT)

def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")

def _profile(tmp_path: Path, front = False) -> deploy_profile:
    return deploy_profile(host = host_section(face = "chest", srv_base = str(tmp_path / "srv"), etc_base = str(tmp_path / "etc")), front = front_section(enabled = front, domain = "acme.example" if front else ""))

def _ctx(prof, executor = _ok, **kw):
    ctx = bootstrap.stage_context(profile = prof, executor = executor, **kw)
    ctx.getpwnam = lambda u: _fake_pw()
    return ctx

class _fake_check:
    def __init__(self, name):
        self.name = name
        self.ok = True
        self.detail = "fake ok"
        self.overridden = False

class _fake_pw:
    pw_uid = 1000
    pw_gid = 1000

class test_full_machine:
    def test_all_eleven_stages_now_land(self, tmp_path, monkeypatch):
        """The wiring promise: no stage stays PENDING — exit 0, not 2."""
        prof = _profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        _seed_artifact(tmp_path, prof)
        monkeypatch.setenv("CHEST_DATABASE_URL", "postgresql://u:p@198.51.100.9:5432/db")
        monkeypatch.setattr(stages_more, "_http_post", _fake_http_ok)
        monkeypatch.setattr("celestia_devtools.deploy.ops.verify", lambda p, **kw: [_fake_check("health"), _fake_check("epoch"), _fake_check("ledger"), _fake_check("backup-freshness")])
        ctx = _ctx(prof, assume_root = True)
        ctx.state = {}
        code, results = bootstrap.run_stages(ctx)
        by = {r.name: (r.status, r.detail) for r in results}
        assert code == 0, by
        assert by["first-admin"][0] == bootstrap.OK
        assert by["front"][0] == bootstrap.SKIPPED  # front disabled here
        assert by["verify"][0] == bootstrap.OK  # wired in stages_more

def _seed_artifact(tmp_path, prof):
    out = tmp_path / "channel"
    out.mkdir(parents = True, exist_ok = True)
    fake = tmp_path / "fakebin"
    fake.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(fake, 0o755)
    artifact_build(fake, "0.1.0", stages_more._target(), out)
    write_index(out, {"stable": scan_dir(out)})
    prof.artifact.source = str(out)
    assert (tmp_path / "channel" / "index.toml").exists(), "seed index missing"

class _fake_resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n = -1):
        data = self._body
        self._body = b""
        return data[:n] if n > 0 else data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

def _fake_http_ok(url, payload, headers = None, timeout = 30):
    if url.endswith("/api/auth/nonce"):
        return _http(200, json.dumps({"nonce": "n-1"}))

    return _http(201, json.dumps({"ok": True}))

class _http:
    """Matches stages_more.HttpResult's surface: .status, .body, .read()."""

    def __init__(self, status, text):
        self.status = status
        self.body = text

    def read(self, n = -1):
        return self.body.encode()[:n] if n > 0 else self.body.encode()

class test_first_admin:
    def test_register_failure_fails_stage_not_silently(self, tmp_path, monkeypatch, capsys):
        prof = _profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        monkeypatch.setattr(stages_more, "_http_post", lambda *a, **k: _http(403, '{"error":"denied"}'))
        ctx = _ctx(prof)
        result = stages_more.stage_first_admin(ctx)
        assert result.status == bootstrap.FAILED
        assert "403" in result.detail
        assert "口令" not in capsys.readouterr().out  # nothing printed on failure

    def test_password_printed_exactly_once_never_logged(self, tmp_path, monkeypatch, capsys):
        prof = _profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        monkeypatch.setattr(stages_more, "_http_post", _fake_http_ok)
        ctx = _ctx(prof)
        result = stages_more.stage_first_admin(ctx, http = _fake_http_ok, password_gen = lambda n: "pw-{}".format("x" * n))
        assert result.status == bootstrap.OK
        out = capsys.readouterr().out
        assert out.count("pw-xxxxxxxxxxxxxxxxxx") == 1
        assert "pw-" not in result.detail  # never in stage results (logs)

class test_rotation_gate:
    def _rpc(self, needs_setup):
        def rpc(base, method, params = None):
            return 200, {"result": {"needs_setup": needs_setup}}

        return rpc

    def test_fresh_face_allows_rotation(self, tmp_path):
        prof = _profile(tmp_path)
        allowed, why = ops.assert_rotation_allowed(prof, rpc = self._rpc(True))
        assert allowed and ("fresh" in why)

    def test_registered_face_refuses_fail_closed(self, tmp_path):
        prof = _profile(tmp_path)
        allowed, why = ops.assert_rotation_allowed(prof, rpc = self._rpc(False))
        assert not allowed and ("real users" in why)

    def test_unreachable_face_refuses(self, tmp_path):
        prof = _profile(tmp_path)

        def dead(base, method, params = None):
            return 0, {"error": "connection refused"}

        allowed, why = ops.assert_rotation_allowed(prof, rpc = dead)
        assert not allowed and ("refusing" in why)

class test_verify_and_uninstall:
    def test_verify_reports_health_epoch_ledger(self, tmp_path):
        prof = _profile(tmp_path)

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, '{"status":"ok"}'

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get)}
        assert checks["health"].ok
        assert checks["epoch"].ok and (checks["epoch"].detail == "main-Ab12Cd34.js")
        assert not checks["backup-freshness"].ok  # no backups yet

    def test_verify_rejects_a_lying_ok_above_a_red_census(self, tmp_path):
        """The 2026-11-09 defect, at the gate: the face answered 200 with
        `status:"ok"` while three channels were red, and `deploy verify`
        called it healthy. The census is now part of the judgement."""
        prof = _profile(tmp_path)
        red = json.dumps({
            "status": "degraded",
            "channels": {
                "census_ok": True,
                "tracked": 6,
                "healthy": 3,
                "failing": 3,
                "channels": [
                    {"name": "scepter-live", "consecutive_failures": 12},
                    {"name": "plugin-agent-sync", "consecutive_failures": 1},
                    {"name": "world-sync", "consecutive_failures": 4},
                ],
            },
        })

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, red

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get, pause = lambda _s: None)}
        assert not checks["health"].ok, "a degraded face must fail the health check"
        assert "scepter-live" in checks["health"].detail
        assert "plugin-agent-sync" in checks["health"].detail
        assert "world-sync" in checks["health"].detail

    def test_verify_rejects_ok_beside_red_channels(self, tmp_path):
        """Belt and braces for faces built before the fix: they still report
        `status:"ok"` next to a red census, and the gate must not trust the
        top-level field alone."""
        prof = _profile(tmp_path)
        body = json.dumps({
            "status": "ok",
            "channels": {"census_ok": True, "tracked": 5, "healthy": 4, "failing": 1, "channels": [{"name": "world-sync", "consecutive_failures": 3}]},
        })

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, body

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get, pause = lambda _s: None)}
        assert not checks["health"].ok
        assert "world-sync" in checks["health"].detail

    def test_verify_accepts_a_green_census(self, tmp_path):
        prof = _profile(tmp_path)
        body = json.dumps({
            "status": "ok",
            "channels": {"census_ok": True, "tracked": 6, "healthy": 6, "failing": 0, "channels": []},
        })

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, body

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get)}
        assert checks["health"].ok, checks["health"].detail
        assert checks["health"].detail == "status=ok"

    def test_verify_rejects_an_unparseable_health_body(self, tmp_path):
        """200 plus a proxy's HTML error page is not health either."""
        prof = _profile(tmp_path)

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, "<html>Malkuth — service restarting</html>"

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get, pause = lambda _s: None)}
        assert not checks["health"].ok
        assert "not JSON" in checks["health"].detail

    def test_verify_resamples_a_blip_before_failing_the_deploy(self, tmp_path):
        """The verdict reads each channel's LAST outcome, so a three-second
        blip can fail a gate nobody retries. A red 200 is sampled again; a
        dead face still fails on the first answer."""
        prof = _profile(tmp_path)
        red = json.dumps({
            "status": "degraded",
            "channels": {"census_ok": True, "tracked": 6, "healthy": 5, "failing": 1, "channels": [{"name": "scepter-live", "consecutive_failures": 1}]},
        })
        green = json.dumps({
            "status": "ok",
            "channels": {"census_ok": True, "tracked": 6, "healthy": 6, "failing": 0, "channels": []},
        })
        answers = [red, green]
        seen = []

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                seen.append(url)
                return 200, answers[min(len(seen), 2) - 1]

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get = fake_get, pause = lambda _s: None)}
        assert checks["health"].ok, checks["health"].detail
        assert len(seen) == 2, "the red sample must be re-read, not trusted once"

        # A dead face is not a blip: one answer is enough.
        dead_seen = []

        def dead(url, timeout = 15):
            if url.endswith("/api/health"):
                dead_seen.append(url)
            return 0, "connection refused"

        dead_checks = {c.name: c for c in ops.verify(prof, http_get = dead, pause = lambda _s: None)}
        assert not dead_checks["health"].ok
        assert len(dead_seen) == 1, "a connection refusal must not be re-sampled"

    def test_verify_allows_degraded_only_when_explicitly_asked(self, tmp_path):
        """An operator must be able to ship the fix for the thing that is
        degraded — but only by saying so, and only for `degraded`."""
        prof = _profile(tmp_path)
        degraded = json.dumps({
            "status": "degraded",
            "channels": {"census_ok": True, "tracked": 6, "healthy": 3, "failing": 3, "channels": [{"name": "world-sync", "consecutive_failures": 9}]},
        })

        def fake_get(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, degraded

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        strict = {c.name: c for c in ops.verify(prof, http_get = fake_get, pause = lambda _s: None)}
        assert not strict["health"].ok

        allowed = {c.name: c for c in ops.verify(prof, http_get = fake_get, allow_degraded = True, pause = lambda _s: None)}
        assert allowed["health"].ok
        assert "allowed by --allow-degraded" in allowed["health"].detail
        assert "world-sync" in allowed["health"].detail, "the override must still record what it covered"
        # The flag is DERIVED from that marker text (`overridden = marker in
        # detail`), and the stage layer reads only the flag — so a reworded
        # marker would silently drop the audit line while this test stayed
        # green. Both directions are asserted here.
        assert allowed["health"].overridden is True
        assert strict["health"].overridden is False

        # `unhealthy` is not a state to ship over, override or not.
        def unhealthy(url, timeout = 15):
            if url.endswith("/api/health"):
                return 200, json.dumps({"status": "unhealthy", "channels": {"census_ok": True, "tracked": 1, "healthy": 0, "failing": 1, "channels": [{"name": "db", "consecutive_failures": 1}]}})

            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        still_red = {c.name: c for c in ops.verify(prof, http_get = unhealthy, allow_degraded = True, pause = lambda _s: None)}
        assert not still_red["health"].ok, "the override covers degradation, not a dying face"

    def test_the_override_stays_on_the_record_after_a_successful_verify(self, tmp_path):
        """The flag exists so a human's decision is auditable. The stage's OK
        branch used to print only `"{name} ok"`, dropping the detail — so the
        override was visible ONLY when the deployment had already failed."""
        prof = _profile(tmp_path)
        ctx = bootstrap.stage_context(profile = prof, allow_degraded = True)

        def fake_verify(p, **kw):
            assert kw.get("allow_degraded") is True, "the stage must pass the flag through"
            return [
                ops.check("health", True, 'status="degraded" (allowed by --allow-degraded)', overridden = True),
                ops.check("epoch", True, "main-Ab12Cd34.js"),
                ops.check("ledger", True, "3 entries"),
            ]

        with mock.patch.object(ops, "verify", fake_verify):
            result = stages_more.stage_verify(ctx)

        assert result.status == bootstrap.OK
        assert "allowed by --allow-degraded" in result.detail
        assert "world-sync" not in result.detail  # nothing invented
        # A plain green verify must not sprout the override marker.
        with mock.patch.object(ops, "verify", lambda p, **kw: [ops.check("health", True, "status=ok")]):
            plain = stages_more.stage_verify(ctx)

        assert plain.status == bootstrap.OK
        assert "allowed" not in plain.detail
        assert plain.detail == "health ok"

    def test_a_positional_caller_cannot_smuggle_the_override(self, tmp_path):
        """`allow_degraded` sits AFTER `actions` so the 5th positional argument
        (a dry-run transcript list) can never bind to it — a non-empty list is
        truthy and would silently open the override."""
        prof = _profile(tmp_path)
        transcript = ["would do things"]
        ctx = bootstrap.stage_context(prof, subprocess.run, False, False, transcript)
        assert ctx.actions == transcript
        assert ctx.allow_degraded is False

    def test_verify_health_down_is_a_red_check(self, tmp_path):
        prof = _profile(tmp_path)

        def dead(url, timeout = 15):
            return 0, "connection refused"

        checks = {c.name: c for c in ops.verify(prof, http_get = dead)}
        assert not checks["health"].ok
        assert not checks["epoch"].ok

    def test_uninstall_keeps_data_without_purge(self, tmp_path):
        prof = _profile(tmp_path)
        data = prof.host.deploy_root() / "data" / "uploads"
        data.mkdir(parents = True)
        (data / "x").write_bytes(b"x")
        binroot = prof.host.deploy_root() / "bin"
        binroot.mkdir(parents = True)
        (binroot / "chest").write_bytes(b"bin")
        steps = ops.uninstall(prof, executor = _ok)
        assert any("stopped" in s for s in steps)
        assert not (prof.host.deploy_root() / "bin").exists()
        assert (data / "x").exists(), "uninstall must NOT touch data"
        purged = ops.uninstall(prof, purge_data = True, executor = _ok)
        assert any("purged" in s for s in purged)
