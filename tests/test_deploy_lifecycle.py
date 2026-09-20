"""Tests for backup/restore and the upgrade/rollback ledger.

Everything runs against tmp-path faces with an injected executor — no real
systemctl, no real pg_dump; the binding rule (backup-before-migrate) and the
rollback path are what must have teeth here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from celestia_devtools.deploy import backup as backup_mod, lifecycle
from celestia_devtools.deploy.artifact import build as artifact_build
from celestia_devtools.deploy.profile import (
    DeployProfile, FrontSection, HostSection,
)


def _profile(tmp_path: Path) -> DeployProfile:
    return DeployProfile(
        host=HostSection(face="chest", srv_base=str(tmp_path / "srv"),
                         etc_base=str(tmp_path / "etc")),
        front=FrontSection(enabled=True, domain="acme.example"),
    )


def _seed_face(prof: DeployProfile, payload: bytes = b"chest-v1") -> Path:
    root = prof.host.deploy_root()
    (root / "data" / "uploads").mkdir(parents=True, exist_ok=True)
    (root / "data" / "uploads" / "x.bin").write_bytes(b"upload")
    env = prof.host.etc_env()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("JWT_SECRET=j\nSHITTIM_CHEST_ENCRYPTION_KEY=k\n", encoding="utf-8")
    os.chmod(env, 0o600)
    binary = lifecycle.bin_path(prof)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(payload)
    os.chmod(binary, 0o755)
    return binary


def _ok_executor(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


class TestBackup:
    def test_backup_captures_data_env_and_warns(self, tmp_path, capsys):
        prof = _profile(tmp_path)
        _seed_face(prof)
        result = backup_mod.backup(prof, tmp_path / "backups",
                                   executor=_ok_executor)
        assert set(result.entries) >= {"data.tar.gz", "env"}
        manifest = json.loads((result.path / "manifest.json").read_text())
        assert manifest["entries"] == result.entries
        env_copy = result.path / "env"
        assert stat.S_IMODE(os.stat(env_copy).st_mode) == 0o600
        assert "密文永久解不开" in capsys.readouterr().out  # the warning must reach the operator

    def test_restore_round_trip_and_integrity_teeth(self, tmp_path):
        prof = _profile(tmp_path)
        _seed_face(prof)
        result = backup_mod.backup(prof, tmp_path / "backups",
                                   executor=_ok_executor)
        (prof.host.deploy_root() / "data" / "uploads" / "x.bin").unlink()
        prof.host.etc_env().unlink()
        applied = backup_mod.restore(prof, result.path, executor=_ok_executor)
        assert set(applied) >= {"data", "env"}
        assert (prof.host.deploy_root() / "data" / "uploads" / "x.bin").read_bytes() == b"upload"
        # tamper → refuse
        (result.path / "env").write_text("JWT_SECRET=tampered\n", encoding="utf-8")
        with pytest.raises(IOError, match="integrity"):
            backup_mod.restore(prof, result.path, executor=_ok_executor)


class TestLedger:
    def test_history_append_read(self, tmp_path):
        prof = _profile(tmp_path)
        assert lifecycle.history_read(prof) == []
        lifecycle.history_append(prof, action="upgrade", version="1.2.3", result="ok")
        lifecycle.history_append(prof, action="rollback", result="ok")
        rows = lifecycle.history_read(prof)
        assert [r["action"] for r in rows] == ["upgrade", "rollback"]
        assert all({"ts", "action"} <= set(r) for r in rows)


class TestUpgradeRollback:
    def _channel(self, tmp_path, version: str) -> Path:
        out = tmp_path / "artifacts"
        fake = tmp_path / "fakebin"
        fake.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        artifact_build(fake, version, "t1", out)
        from celestia_devtools.deploy.artifact import write_index, scan_dir
        write_index(out, {"stable": scan_dir(out)})
        return out

    def test_happy_path_keeps_prev_and_backups(self, tmp_path):
        prof = _profile(tmp_path)
        _seed_face(prof)
        out = self._channel(tmp_path, "0.2.0")
        outcome = lifecycle.upgrade(prof, str(out), "stable", "t1",
                                    executor=_ok_executor)
        assert outcome.ok and outcome.version == "0.2.0"
        assert lifecycle.prev_path(prof).read_bytes() == b"chest-v1"
        assert lifecycle.bin_path(prof).read_bytes() != b"chest-v1"
        rows = lifecycle.history_read(prof)
        assert rows[-1]["action"] == "upgrade" and rows[-1]["result"] == "ok"
        assert rows[-1]["backup"] and Path(rows[-1]["backup"]).exists()

    def test_migrate_failure_rolls_binary_back(self, tmp_path):
        prof = _profile(tmp_path)
        _seed_face(prof)
        out = self._channel(tmp_path, "0.3.0")

        def failing_migrate(cmd, **kw):
            if "db-migrate" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "boom: column missing")
            if cmd[0] == "pg_dump":
                return subprocess.CompletedProcess(cmd, 1, "", "no db")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        outcome = lifecycle.upgrade(prof, str(out), "stable", "t1",
                                    executor=failing_migrate)
        assert not outcome.ok and outcome.rolled_back
        assert lifecycle.bin_path(prof).read_bytes() == b"chest-v1"
        rows = lifecycle.history_read(prof)
        assert rows[-1]["result"] == "failed"
        assert "rolled back" in rows[-1]["detail"]

    def test_explicit_rollback_and_no_prev_case(self, tmp_path):
        prof = _profile(tmp_path)
        _seed_face(prof)
        out = self._channel(tmp_path, "0.2.0")
        lifecycle.upgrade(prof, str(out), "stable", "t1", executor=_ok_executor)
        outcome = lifecycle.rollback(prof, executor=_ok_executor)
        assert outcome.ok
        assert lifecycle.bin_path(prof).read_bytes() == b"chest-v1"
        # nothing left to roll back to
        again = lifecycle.rollback(prof, executor=_ok_executor)
        assert not again.ok and "no " in again.detail

    def test_backup_prerequisite_is_structural(self, tmp_path):
        """The binding rule: an upgrade without a recorded backup is a bug.
        The happy path above asserts rows[-1]['backup'] exists; here we pin
        that even the failed-migrate path records one (the rollback story
        depends on it)."""
        prof = _profile(tmp_path)
        _seed_face(prof)
        out = self._channel(tmp_path, "0.3.0")

        def failing_migrate(cmd, **kw):
            if "db-migrate" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "x")
            if cmd[0] == "pg_dump":
                return subprocess.CompletedProcess(cmd, 1, "", "no db")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        lifecycle.upgrade(prof, str(out), "stable", "t1", executor=failing_migrate)
        rows = lifecycle.history_read(prof)
        assert all(r.get("backup") for r in rows if r["action"] == "upgrade"), rows
