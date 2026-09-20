"""CLI-level tests for bare `deploy` (run_bootstrap): the layer R1 found
unguarded (mutation f was green). Pins: --profile carries every field
including the ones the wizard never asks about (R1 P2-1), dry-run writes
nothing, --json stdout is pure JSON, exit codes match the stage machine."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from celestia_devtools.deploy import cli as deploy_cli
from celestia_devtools.deploy.profile import DeployProfile


def _profile_file(tmp_path: Path) -> Path:
    prof = DeployProfile()
    prof.host.face = "chest"
    prof.host.srv_base = str(tmp_path / "srv")
    prof.host.etc_base = str(tmp_path / "etc")
    prof.front.enabled = True
    prof.front.domain = "acme.example"
    prof.front.email = "ops@acme.example"
    prof.admin.email = "ops@acme.example"
    path = tmp_path / "chest.profile.toml"
    prof.write(path)
    return path


class TestProfilePassthrough:
    def test_etc_base_rides_through_dry_run(self, tmp_path, monkeypatch, capsys):
        """R1 P2-1 regression pin: an etc_base outside /etc must survive the
        seed→build round trip — a whitelist rebuild used to silently redirect
        privileged writes back to /etc/celestia."""
        path = _profile_file(tmp_path)
        monkeypatch.setattr("sys.stdin", _NoTty())
        monkeypatch.setattr("sys.argv", ["deploy", "--profile", str(path),
                                         "--non-interactive", "--dry-run"])
        assert deploy_cli.main() == 2  # pending slices, loudly
        out = capsys.readouterr().out
        assert str(tmp_path / "etc") in out
        assert "/etc/celestia" not in out

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        path = _profile_file(tmp_path)
        before = sorted(p.name for p in tmp_path.rglob("*"))
        monkeypatch.setattr("sys.stdin", _NoTty())
        monkeypatch.setattr("sys.argv", ["deploy", "--profile", str(path),
                                         "--non-interactive", "--dry-run"])
        deploy_cli.main()
        after = sorted(p.name for p in tmp_path.rglob("*"))
        assert before == after, "dry-run must not create files"

    @pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else False,
                        reason="as root, precheck passes and the stage would "
                               "really useradd — this test pins the non-root refusal")
    def test_real_run_without_root_fails_loudly(
            self, tmp_path, monkeypatch, capsys):
        """A non-root real run must refuse at precheck (privileged stages),
        never half-run: the profile lands under the configured etc_base, no
        secrets are generated, and the exit code is 1."""
        path = _profile_file(tmp_path)
        monkeypatch.setattr("sys.stdin", _NoTty())
        monkeypatch.setattr("sys.argv", ["deploy", "--profile", str(path),
                                         "--non-interactive"])
        rc = deploy_cli.main()
        assert rc == 1
        assert (tmp_path / "etc" / "chest.profile.toml").exists()
        assert not (tmp_path / "etc" / "chest.env").exists()
        assert "/etc/celestia" not in capsys.readouterr().out


class TestJsonPurity:
    def test_json_stdout_is_pure(self, tmp_path, monkeypatch, capsys):
        """R1 P3: with --json, stdout must parse as one JSON document —
        provenance and the human summary belong on stderr."""
        path = _profile_file(tmp_path)
        monkeypatch.setattr("sys.stdin", _NoTty())
        monkeypatch.setattr("sys.argv", ["deploy", "--profile", str(path),
                                         "--non-interactive", "--dry-run",
                                         "--json"])
        rc = deploy_cli.main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)  # must parse the WHOLE stdout
        assert data["exit"] == rc == 2
        assert {"precheck", "account", "secrets", "summary"} <= {
            s["name"] for s in data["stages"]}
        # R3 nit-2 pin: dry-run stages never report changed=True
        assert all(s["changed"] is False for s in data["stages"]), data["stages"]
        assert "── 部署进度" in captured.err  # human noise went to stderr


class TestUnknownTopLevel:
    def test_typo_table_rejected(self, tmp_path):
        prof = DeployProfile()
        prof.host.face = "chest"
        text = prof.to_toml() + "\n[frant]\nenabled = true\n"
        from celestia_devtools.deploy.profile import ProfileError
        import pytest
        with pytest.raises(ProfileError, match="frant"):
            DeployProfile.from_toml(text)


class _NoTty:
    def isatty(self):
        return False
