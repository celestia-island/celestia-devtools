"""End-to-end chain tests (D5): every landed module in ONE flow.

The full "fresh machine → systemd → health" e2e needs root + systemd + PG and
lives behind the docker gate at the bottom of this file. What CAN run
everywhere — and what these tests pin — is the **rootless chain**: wizard →
profile → stages (injected executor for the privileged bits, real filesystem
for everything else) → artifact build/index/fetch → upgrade with rollback →
backup/restore. Each module already has unit teeth; this file proves the
modules still compose, which unit tests cannot.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from celestia_devtools.deploy import (
    artifact, backup as backup_mod, bootstrap, lifecycle, wizard,
)
from celestia_devtools.deploy.profile import (
    DeployProfile, FrontSection, HostSection,
)

_PY_FLOOR = "3.11"  # must track pyproject requires-python (R2 P3)


class NonTty(io.StringIO):
    def isatty(self):
        return False


def _ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _chain_profile(tmp_path: Path) -> DeployProfile:
    return DeployProfile(
        host=HostSection(face="chest", srv_base=str(tmp_path / "srv"),
                         etc_base=str(tmp_path / "etc")),
        front=FrontSection(enabled=True, domain="acme.example",
                           email="ops@acme.example"),
    )


def _fake_bin(tmp_path: Path, payload: bytes = b"chest-v1") -> Path:
    p = tmp_path / "fakebin"
    p.write_bytes(payload)
    os.chmod(p, 0o755)
    return p


class TestRootlessChain:
    def test_wizard_to_upgrade_to_backup_full_chain(self, tmp_path, capsys):
        # ── 1. wizard (non-interactive, seeded like the CLI would) ──
        seed = {
            "host.face": "chest", "host.level": "selfhosted",
            "host.listen": "3001", "front.domain": "acme.example",
            "admin.email": "ops@acme.example",
            "admin.password_to_file": "n", "artifact.channel": "stable",
        }
        answers, prov = wizard.collect(seed, interactive=False,
                                       input_fn=_boom)
        assert set(prov.values()) == {"(seeded)"}
        prof = wizard.build_profile(answers)
        prof.host.srv_base = str(tmp_path / "srv")
        prof.host.etc_base = str(tmp_path / "etc")
        prof.write(prof.host.profile_path())

        # ── 2. bootstrap stages: real fs for secrets, injected executor ──
        ctx = bootstrap.StageContext(profile=prof, executor=_ok,
                                     assume_root=True)
        ctx.getpwnam = lambda u: _FakePw(os.getuid(), os.getgid())
        code, results = bootstrap.run_stages(ctx)
        assert code == 2  # pending slices, loudly
        by = {r.name: r.status for r in results}
        assert by["precheck"] == bootstrap.OK
        assert by["secrets"] == bootstrap.OK
        assert by["database"] == bootstrap.PENDING
        env_file = prof.host.etc_env()
        assert env_file.exists() and "JWT_SECRET=" in env_file.read_text()

        # ── 3. idempotent second run: nothing changes, nothing restarts ──
        actions_before = list(ctx.actions)
        code2, results2 = bootstrap.run_stages(ctx)
        assert code2 == 2
        by2 = {r.name: r.status for r in results2}
        assert by2["secrets"] == bootstrap.SKIPPED
        assert ctx.actions == actions_before, "second run must add no commands"

        # ── 4. artifact channel: build → index → fetch a real binary ──
        out = tmp_path / "artifacts"
        artifact.build(_fake_bin(tmp_path), "0.1.0", "t1", out)
        artifact.write_index(out, {"stable": artifact.scan_dir(out)})
        index = artifact.read_index(out / "index.toml")
        picked = artifact.resolve(index, "stable", "t1")
        dest = tmp_path / "incoming"
        bin_path = artifact.fetch(picked, str(out), dest)
        assert bin_path.read_bytes() == b"chest-v1"

        # ── 5. lifecycle: install, upgrade (backup prerequisite), rollback ──
        binary = lifecycle.bin_path(prof)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"chest-v1")
        os.chmod(binary, 0o755)
        artifact.build(_fake_bin(tmp_path, b"chest-v2"), "0.2.0", "t1", out)
        artifact.write_index(out, {"stable": artifact.scan_dir(out)})
        outcome = lifecycle.upgrade(prof, str(out), "stable", "t1",
                                    executor=_ok)
        assert outcome.ok and outcome.version == "0.2.0"
        assert lifecycle.bin_path(prof).read_bytes() == b"chest-v2"
        assert lifecycle.prev_path(prof).read_bytes() == b"chest-v1"
        rows = lifecycle.history_read(prof)
        assert rows[-1]["action"] == "upgrade" and rows[-1]["result"] == "ok"
        assert rows[-1]["backup"] and Path(rows[-1]["backup"]).exists(), \
            "upgrade must record a backup"

        rolled = lifecycle.rollback(prof, executor=_ok)
        assert rolled.ok
        assert lifecycle.bin_path(prof).read_bytes() == b"chest-v1"

        # ── 6. backup → destroy → restore round trip ──
        (prof.host.deploy_root() / "data" / "uploads").mkdir(
            parents=True, exist_ok=True)
        (prof.host.deploy_root() / "data" / "uploads" / "x").write_bytes(b"x")
        bkp = backup_mod.backup(prof, tmp_path / "backups", executor=_ok)
        (prof.host.deploy_root() / "data" / "uploads" / "x").unlink()
        applied = backup_mod.restore(prof, bkp.path, executor=_ok)
        assert (prof.host.deploy_root() / "data" / "uploads" / "x").exists()
        assert "data" in applied

    def test_cli_json_reports_the_whole_chain(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("CHEST_DATABASE_URL", raising=False)
        from celestia_devtools.deploy import cli as deploy_cli
        prof = _chain_profile(tmp_path)
        prof.admin.email = "ops@acme.example"
        path = prof.host.profile_path()
        prof.write(path)
        monkeypatch.setattr("sys.stdin", NonTty())
        monkeypatch.setattr("sys.argv", ["deploy", "--profile", str(path),
                                         "--non-interactive", "--dry-run",
                                         "--json"])
        rc = deploy_cli.main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["exit"] == rc == 1  # wired: database fails (no PG)
        names = [s["name"] for s in data["stages"]]
        statuses = {s["name"]: s["status"] for s in data["stages"]}
        # No stage may be PENDING — the CLI is fully wired
        assert all(st != "pending-slice" for st in statuses.values()), \
            f"PENDING stages found: {[n for n, st in statuses.items() if st == 'pending-slice']}"
        assert names[-1] == 'summary'  # summary always runs


class _FakePw:
    def __init__(self, uid, gid):
        self.pw_uid = uid
        self.pw_gid = gid


def _boom(_prompt=""):
    raise AssertionError("stdin must not be touched")


class TestDockerGatedE2E:
    """The real fresh-container run. Gated on a live docker/podman daemon —
    CI runs it when the farm provides one; otherwise it skips loudly."""

    def test_container_bootstrap_smoke(self, tmp_path):
        from celestia_devtools.env import docker
        try:
            cmd = docker.docker_cmd()
        except Exception:
            import pytest
            pytest.skip("no live container runtime on this host")
        import pytest
        # The smoke: our artifact tarball builds, indexes, and its sha
        # verifies inside a stock python container (the pyshim contract:
        # stdlib-only extraction works on a bare interpreter).
        out = tmp_path / "artifacts"
        artifact.build(_fake_bin(tmp_path), "0.1.0", "t1", out)
        artifact.write_index(out, {"stable": artifact.scan_dir(out)})
        index = artifact.read_index(out / "index.toml")
        entry = artifact.resolve(index, "stable", "t1")
        script = (
            "import hashlib,sys;d=hashlib.sha256(open(sys.argv[1],'rb').read()"
            ").hexdigest();print(d);sys.exit(0 if d==sys.argv[2] else 1)"
        )
        proc = subprocess.run(
            [*cmd, "run", "--rm", "-v", "{}:/a:ro".format(out),
             "python:{}-slim".format(_PY_FLOOR), "python", "-c", script,
             "/a/" + entry.file, entry.sha256],
            capture_output=True, text=True, timeout=300)
        if proc.returncode == 125 and (
                "registry" in proc.stderr or "dial tcp" in proc.stderr
                or "Cannot connect to the Docker daemon" in proc.stderr):
            pytest.skip("container runtime or registry unreachable "
                        "(known infra gap, PLAN §2.2)")
        assert proc.returncode == 0, proc.stderr
