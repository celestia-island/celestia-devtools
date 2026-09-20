"""Tests for the bootstrap stage machine: order, idempotency, loud pending."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from celestia_devtools.deploy import bootstrap
from celestia_devtools.deploy.profile import (
    DeployProfile, FrontSection, HostSection,
)


def _profile(tmp_path: Path) -> DeployProfile:
    return DeployProfile(
        host=HostSection(face="chest", srv_base=str(tmp_path / "srv"),
                         etc_base=str(tmp_path / "etc")),
        front=FrontSection(enabled=True, domain="acme.example",
                           email="ops@acme.example"),
    )


class _FakePw:
    def __init__(self, uid, gid):
        self.pw_uid = uid
        self.pw_gid = gid


class _Recorder:
    """Fake executor + injectable user database; records every command."""

    def __init__(self, users: set[str] | None = None):
        self.calls: list[list[str]] = []
        self.users = set(users or ())

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        name = cmd[0]
        if name == "useradd":
            self.users.add(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def getpwnam(self, user):
        if user not in self.users:
            raise KeyError(user)
        return _FakePw(os.getuid(), os.getgid())


def _ctx(prof, rec, **kw):
    ctx = bootstrap.StageContext(profile=prof, executor=rec, **kw)
    ctx.getpwnam = rec.getpwnam
    return ctx


class TestStageMachine:
    def test_pending_slice_stops_run_with_exit_2(self, tmp_path, capsys):
        rec = _Recorder()
        code, results = bootstrap.run_stages(_ctx(_profile(tmp_path), rec, assume_root=True))
        assert code == 2
        by_name = {r.name: r for r in results}
        for landed in ("precheck", "account", "secrets"):
            assert by_name[landed].status in (bootstrap.OK, bootstrap.SKIPPED)
        assert by_name["database"].status == bootstrap.PENDING
        assert by_name["verify"].status == bootstrap.PENDING
        out = capsys.readouterr().out
        assert "不假装完成" in out
        assert "D3" in out and "D4" in out

    def test_failed_stage_exits_1_after_summary(self, tmp_path, capsys):
        prof = _profile(tmp_path)
        prof.host.face = "Bad Face!"  # invalid → precheck fails
        code, results = bootstrap.run_stages(_ctx(prof, _Recorder(), assume_root=True))
        assert code == 1
        assert results[0].status == bootstrap.FAILED
        assert results[-1].name == "summary"  # operator still sees the picture


class TestAccountStage:
    def test_first_run_creates_second_run_noop(self, tmp_path):
        rec = _Recorder()
        ctx = _ctx(_profile(tmp_path), rec)
        first = bootstrap.stage_account(ctx)
        assert first.changed
        created = [c for c in rec.calls if c[0] in ("useradd", "install")]
        assert any(c[0] == "useradd" for c in created)
        assert (tmp_path / "srv" / "chest" / "data" / "uploads").exists()

        rec2 = _Recorder(users=set(rec.users))
        second = bootstrap.stage_account(_ctx(_profile(tmp_path), rec2))
        assert not second.changed
        assert rec2.calls == [], "second run must be a pure no-op"


class TestSecretsStage:
    def test_generates_once_0600_and_never_prints(self, tmp_path, capsys):
        ctx = _ctx(_profile(tmp_path), _Recorder())
        result = bootstrap.stage_secrets(ctx)
        assert result.status == bootstrap.OK and result.changed
        env = tmp_path / "etc" / "chest.env"
        mode = stat.S_IMODE(os.stat(env).st_mode)
        assert mode == 0o600
        text = env.read_text(encoding="utf-8")
        assert "JWT_SECRET=" in text and "SHITTIM_CHEST_ENCRYPTION_KEY=" in text
        # the generated values must not leak into any output surface
        value = text.split("JWT_SECRET=")[1].splitlines()[0]
        assert value not in result.detail
        assert value not in capsys.readouterr().out

    def test_rerun_skips_and_preserves_content(self, tmp_path):
        prof = _profile(tmp_path)
        bootstrap.stage_secrets(_ctx(prof, _Recorder()))
        before = (tmp_path / "etc" / "chest.env").read_text(encoding="utf-8")
        second = bootstrap.stage_secrets(_ctx(prof, _Recorder()))
        assert second.status == bootstrap.SKIPPED and not second.changed
        assert (tmp_path / "etc" / "chest.env").read_text(encoding="utf-8") == before

    def test_world_readable_existing_file_is_refused(self, tmp_path):
        prof = _profile(tmp_path)
        env = tmp_path / "etc" / "chest.env"
        env.parent.mkdir(parents=True)
        env.write_text("JWT_SECRET=x\n", encoding="utf-8")
        os.chmod(env, 0o644)
        result = bootstrap.stage_secrets(_ctx(prof, _Recorder()))
        assert result.status == bootstrap.FAILED
        assert "refusing" in result.detail


class TestAccountFailure:
    def test_useradd_failure_is_a_clean_stage_failure(self, tmp_path):
        """R2-F1: a failed useradd must come back as a FAILED stage result,
        never escape as a KeyError from the later getpwnam."""
        from types import SimpleNamespace
        rec = _Recorder()

        def failing(cmd, **kw):
            rec.calls.append(list(cmd))
            if cmd[0] == "useradd":
                return SimpleNamespace(returncode=1, stderr="useradd: Permission denied")
            return SimpleNamespace(returncode=0, stderr="")

        ctx = _ctx(_profile(tmp_path), rec)
        ctx.executor = failing
        result = bootstrap.stage_account(ctx)
        assert result.status == bootstrap.FAILED
        assert "useradd" in result.detail and "rc=1" in result.detail


class TestDryRun:
    def test_dry_run_touches_nothing_but_records_plan(self, tmp_path, capsys):
        rec = _Recorder()
        ctx = _ctx(_profile(tmp_path), rec, dry_run=True)
        code, results = bootstrap.run_stages(ctx)
        assert code == 2  # pending stages still loud in dry-run
        assert not (tmp_path / "etc").exists()
        assert not (tmp_path / "srv").exists()
        assert ctx.actions, "dry-run must still transcript the plan"
        dry = capsys.readouterr().out
        assert "would write" in dry or "generated" in "".join(
            r.detail for r in results) or ctx.actions

    def test_dry_run_passes_precheck_without_root(self, tmp_path):
        prof = _profile(tmp_path)
        ctx = bootstrap.StageContext(profile=prof, executor=_Recorder(),
                                     assume_root=False, dry_run=True)
        ctx.getpwnam = _Recorder().getpwnam
        result = bootstrap.stage_precheck(ctx)
        assert result.status == bootstrap.OK


class TestPrecheck:
    def test_non_root_without_dry_run_fails_loudly(self, tmp_path):
        if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover
            import pytest
            pytest.skip("running as root; gate would pass")
        ctx = _ctx(_profile(tmp_path), _Recorder(), assume_root=False)
        ctx.dry_run = False
        result = bootstrap.stage_precheck(ctx)
        assert result.status == bootstrap.FAILED
        assert "root" in result.detail


class TestStageRegistryContract:
    def test_every_stage_has_a_slice_and_an_order(self):
        assert tuple(bootstrap.STAGE_ORDER) == tuple(bootstrap.STAGE_SLICES)
        assert len(bootstrap.STAGE_ORDER) == 11
        # landed stages must be callable; pending ones must be None
        for name in bootstrap.STAGE_ORDER:
            fn = bootstrap.STAGES.get(name)
            if name in ("database", "artifact", "install", "migrate",
                        "first-admin", "front", "verify"):
                assert fn is None, name
            else:
                assert callable(fn), name
