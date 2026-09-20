"""Wiring-slice tests: the remaining stages, verify/status/uninstall, and
the §1.8 rotation gate — all rootless via injected executors/HTTP."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from celestia_devtools.deploy import bootstrap, ops, stages_more
from celestia_devtools.deploy.artifact import build as artifact_build
from celestia_devtools.deploy.profile import (
    DeployProfile, FrontSection, HostSection,
)

_STAGES_SNAPSHOT = dict(bootstrap.STAGES)


@pytest.fixture(autouse=True)
def _register_and_restore_stages():
    """Complete the stage machine for this test, then restore the
    pending-slice contract so non-wiring tests are unaffected."""
    stages_more.register_stages()
    yield
    bootstrap.STAGES.clear()
    bootstrap.STAGES.update(_STAGES_SNAPSHOT)


def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _profile(tmp_path: Path, front=False) -> DeployProfile:
    return DeployProfile(
        host=HostSection(face="chest", srv_base=str(tmp_path / "srv"),
                         etc_base=str(tmp_path / "etc")),
        front=FrontSection(enabled=front, domain="acme.example" if front else ""),
    )


def _ctx(prof, executor=_ok, **kw):
    ctx = bootstrap.StageContext(profile=prof, executor=executor, **kw)
    ctx.getpwnam = lambda u: _FakePw()
    return ctx


class _FakeCheck:
    def __init__(self, name):
        self.name = name
        self.ok = True
        self.detail = "fake ok"


class _FakePw:
    pw_uid = 1000
    pw_gid = 1000


class TestFullMachine:
    def test_all_eleven_stages_now_land(self, tmp_path, monkeypatch):
        """The wiring promise: no stage stays PENDING — exit 0, not 2."""
        prof = _profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        _seed_artifact(tmp_path, prof)
        monkeypatch.setenv("CHEST_DATABASE_URL", "postgresql://u:p@198.51.100.9:5432/db")
        monkeypatch.setattr(stages_more, "_http_post", _fake_http_ok)
        monkeypatch.setattr("celestia_devtools.deploy.ops.verify",
                            lambda p: [_FakeCheck("health"), _FakeCheck("epoch"),
                                       _FakeCheck("ledger"), _FakeCheck("backup-freshness")])
        ctx = _ctx(prof, assume_root=True)
        ctx.state = {}
        code, results = bootstrap.run_stages(ctx)
        by = {r.name: (r.status, r.detail) for r in results}
        assert code == 0, by
        assert by["first-admin"][0] == bootstrap.OK
        assert by["front"][0] == bootstrap.SKIPPED  # front disabled here
        assert by["verify"][0] == bootstrap.OK  # wired in stages_more


def _seed_artifact(tmp_path, prof):
    out = tmp_path / "channel"
    out.mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "fakebin"
    fake.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(fake, 0o755)
    from celestia_devtools.deploy.artifact import write_index, scan_dir
    artifact_build(fake, "0.1.0", stages_more._target(), out)
    write_index(out, {"stable": scan_dir(out)})
    prof.artifact.source = str(out)
    assert (tmp_path / "channel" / "index.toml").exists(), "seed index missing"


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        data = self._body
        self._body = b""
        return data[:n] if n > 0 else data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_http_ok(url, payload, headers=None, timeout=30):
    if url.endswith("/api/auth/nonce"):
        return _Http(200, json.dumps({"nonce": "n-1"}))
    return _Http(201, json.dumps({"ok": True}))


class _Http:
    """Matches stages_more.HttpResult's surface: .status, .body, .read()."""
    def __init__(self, status, text):
        self.status = status
        self.body = text

    def read(self, n=-1):
        return self.body.encode()[:n] if n > 0 else self.body.encode()


class TestFirstAdmin:
    def test_register_failure_fails_stage_not_silently(self, tmp_path, monkeypatch, capsys):
        prof = _profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        monkeypatch.setattr(stages_more, "_http_post",
                            lambda *a, **k: _Http(403, '{"error":"denied"}'))
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
        result = stages_more.stage_first_admin(
            ctx, http=_fake_http_ok, password_gen=lambda n: "pw-{}".format("x" * n))
        assert result.status == bootstrap.OK
        out = capsys.readouterr().out
        assert out.count("pw-xxxxxxxxxxxxxxxxxx") == 1
        assert "pw-" not in result.detail  # never in stage results (logs)


class TestRotationGate:
    def _rpc(self, needs_setup):
        def rpc(base, method, params=None):
            return 200, {"result": {"needs_setup": needs_setup}}
        return rpc

    def test_fresh_face_allows_rotation(self, tmp_path):
        prof = _profile(tmp_path)
        allowed, why = ops.assert_rotation_allowed(prof, rpc=self._rpc(True))
        assert allowed and "fresh" in why

    def test_registered_face_refuses_fail_closed(self, tmp_path):
        prof = _profile(tmp_path)
        allowed, why = ops.assert_rotation_allowed(prof, rpc=self._rpc(False))
        assert not allowed and "real users" in why

    def test_unreachable_face_refuses(self, tmp_path):
        prof = _profile(tmp_path)

        def dead(base, method, params=None):
            return 0, {"error": "connection refused"}

        allowed, why = ops.assert_rotation_allowed(prof, rpc=dead)
        assert not allowed and "refusing" in why


class TestVerifyAndUninstall:
    def test_verify_reports_health_epoch_ledger(self, tmp_path):
        prof = _profile(tmp_path)

        def fake_get(url, timeout=15):
            if url.endswith("/api/health"):
                return 200, '{"status":"ok"}'
            return 200, '<html><script src="main-Ab12Cd34.js"></script>'

        checks = {c.name: c for c in ops.verify(prof, http_get=fake_get)}
        assert checks["health"].ok
        assert checks["epoch"].ok and checks["epoch"].detail == "main-Ab12Cd34.js"
        assert not checks["backup-freshness"].ok  # no backups yet

    def test_verify_health_down_is_a_red_check(self, tmp_path):
        prof = _profile(tmp_path)

        def dead(url, timeout=15):
            return 0, "connection refused"

        checks = {c.name: c for c in ops.verify(prof, http_get=dead)}
        assert not checks["health"].ok
        assert not checks["epoch"].ok

    def test_uninstall_keeps_data_without_purge(self, tmp_path):
        prof = _profile(tmp_path)
        data = prof.host.deploy_root() / "data" / "uploads"
        data.mkdir(parents=True)
        (data / "x").write_bytes(b"x")
        binroot = prof.host.deploy_root() / "bin"
        binroot.mkdir(parents=True)
        (binroot / "chest").write_bytes(b"bin")
        steps = ops.uninstall(prof, executor=_ok)
        assert any("stopped" in s for s in steps)
        assert not (prof.host.deploy_root() / "bin").exists()
        assert (data / "x").exists(), "uninstall must NOT touch data"
        purged = ops.uninstall(prof, purge_data=True, executor=_ok)
        assert any("purged" in s for s in purged)
