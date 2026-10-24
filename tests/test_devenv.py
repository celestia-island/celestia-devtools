"""Tests for the devenv facility (env/devenv.py).

Covers the three seams the module owns:

* profile resolution — built-in ports per profile, ``devenv.toml`` merge,
  validation of targets/services, unknown profiles;
* the variable set — DEBUG_* contract names, the per-repo pass-through
  variables, URL shapes per profile (incl. the arona mock port 8429);
* injection — ``run`` launches a child with the set applied, caller-set
  variables win, ``--set`` extras land, exit codes propagate.
"""

from __future__ import annotations
import json
import sys
import pytest
from celestia_devtools.env import devenv

# ── Profile resolution ───────────────────────────────────────────────────────

class TestProfiles:
    def test_builtin_profiles_present(self):
        assert set(devenv.PROFILE_PORTS) >= {"native", "daemon", "mock"}

    def test_default_profile_is_native(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile, ports, cfg = devenv.load_profile_ports()
        assert profile == "native"
        assert cfg is None
        assert ports["chest"] == ("127.0.0.1", 3000)

    def test_profile_selection(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile, ports, _ = devenv.load_profile_ports(profile = "daemon")
        assert profile == "daemon"
        assert ports["chest"] == ("127.0.0.1", 8400)      # malkuth pod port
        assert ports["arona"] == ("127.0.0.1", 8405)

    def test_mock_profile_uses_arona_mock_port(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _, ports, _ = devenv.load_profile_ports(profile = "mock")
        # arona's mock listens on ARONA_MOCK_PORT default 8429, not the
        # 8420 that core/mock.py MOCK_PORTS still records.
        assert ports["arona"] == ("127.0.0.1", 8429)

    def test_unknown_profile_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ValueError, match = "unknown profile"):
            devenv.load_profile_ports(profile = "nope")

    def test_config_overrides_and_extends(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "devenv.toml").write_text("[profiles.native]\nchest = 9999\n" '[profiles.lan]\nscepter = "192.0.2.10:8424"\n', encoding = "utf-8")
        profile, ports, cfg = devenv.load_profile_ports()
        assert cfg == (tmp_path / "devenv.toml")
        assert ports["chest"] == ("127.0.0.1", 9999)            # override
        assert ports["scepter"] == ("127.0.0.1", 8424)          # untouched
        _, lan, _ = devenv.load_profile_ports(profile = "lan")
        assert lan["scepter"] == ("192.0.2.10", 8424)           # host:port

    def test_config_default_profile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "devenv.toml").write_text('profile = "mock"\n[profiles.mock]\narona = 8429\n', encoding = "utf-8")
        profile, _, _ = devenv.load_profile_ports()
        assert profile == "mock"

    def test_bad_targets_rejected(self):
        for bad in (0, 65536, True, "no-port", "x:notaport", ["3000"]):
            with pytest.raises(ValueError):
                devenv._normalize_target(bad, "profile x.svc")

    def test_unknown_service_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "devenv.toml").write_text("[profiles.native]\nnot-a-service = 3000\n", encoding = "utf-8")

        with pytest.raises(ValueError, match = "unknown service"):
            devenv.load_profile_ports()

# ── Variable set ──────────────────────────────────────────────────────────────

class TestVariableSet:
    def test_debug_contract_names(self):
        env = devenv.build_env({"chest": ("127.0.0.1", 3000), "scepter": ("127.0.0.1", 8424), "arona": ("127.0.0.1", 8420), "evernight": ("127.0.0.1", 8443)})
        assert env["DEBUG_CHEST_URL"] == "http://127.0.0.1:3000"
        assert env["DEBUG_SCEPTER_URL"] == "http://127.0.0.1:8424"
        assert env["DEBUG_SCEPTER_WS_URL"] == "ws://127.0.0.1:8424"
        assert env["DEBUG_ARONA_URL"] == "http://127.0.0.1:8420"
        assert env["DEBUG_EVERNIGHT_URL"] == "ws://127.0.0.1:8443"

        # every DEBUG_* key has no whitespace / trailing slash surprises
        for key, value in env.items():
            assert (value == value.strip()) and not value.endswith("/"), key

    def test_per_repo_passthrough_matches_debug(self):
        ports = {"chest": ("127.0.0.1", 3000), "scepter": ("127.0.0.1", 8424), "arona": ("127.0.0.1", 8420), "evernight": ("127.0.0.1", 8443)}
        env = devenv.build_env(ports)
        assert env["SHITTIM_CHEST_URL"] == env["DEBUG_CHEST_URL"]
        assert env["ENTELECHEIA_SCEPTER_URL"] == env["DEBUG_SCEPTER_URL"]
        assert env["ARONA_MEMORY_URL"] == "ws://127.0.0.1:8424/ws"  # scepter WS + path
        assert env["ARONA_EVERNIGHT_URL"] == env["DEBUG_EVERNIGHT_URL"]

    def test_profiles_differ_where_the_stacks_do(self):
        native = devenv.build_env(devenv.load_profile_ports(profile = "native")[1])
        mock = devenv.build_env(devenv.load_profile_ports(profile = "mock")[1])
        assert native["DEBUG_CHEST_URL"] != mock["DEBUG_CHEST_URL"]
        assert mock["DEBUG_ARONA_URL"].endswith(":8429")

# ── Injection ────────────────────────────────────────────────────────────────

class TestInjection:
    """`devenv run -- <cmd>` spawns the command with the set applied."""

    def _child_env_dump(self, tmp_path, monkeypatch, *run_args, extra_env = None):
        dump = tmp_path / "child-env.json"
        code = "import json,os;" f"open({str(dump)!r},'w').write(json.dumps(dict(os.environ)))"
        argv = ["run", *run_args, "--", sys.executable, "-c", code]

        for key, value in (extra_env or {}).items():
            monkeypatch.setenv(key, value)

        rc = devenv.main(argv)
        assert rc == 0
        return json.loads(dump.read_text(encoding = "utf-8"))

    def test_run_injects_the_contract(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        child = self._child_env_dump(tmp_path, monkeypatch)
        assert child["DEBUG_CHEST_URL"] == "http://127.0.0.1:3000"
        assert child["SHITTIM_CHEST_URL"] == "http://127.0.0.1:3000"
        assert child["ARONA_MEMORY_URL"] == "ws://127.0.0.1:8424/ws"

    def test_run_profile_and_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        child = self._child_env_dump(tmp_path, monkeypatch, "--profile", "mock", "--set", "MY_FLAG=1")
        assert child["DEBUG_ARONA_URL"] == "http://127.0.0.1:8429"
        assert child["MY_FLAG"] == "1"

    def test_caller_set_variables_win(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        child = self._child_env_dump(tmp_path, monkeypatch, extra_env = {"DEBUG_CHEST_URL": "http://10.9.8.7:1"})
        assert child["DEBUG_CHEST_URL"] == "http://10.9.8.7:1"  # never clobbered

# ── CLI ──────────────────────────────────────────────────────────────────────

class TestCli:
    def test_print_export(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert devenv.main(["print"]) == 0
        out = capsys.readouterr().out
        assert "export DEBUG_CHEST_URL='http://127.0.0.1:3000'" in out
        assert "export ARONA_MEMORY_URL='ws://127.0.0.1:8424/ws'" in out

    def test_print_plain_and_json(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert devenv.main(["print", "--format", "plain"]) == 0
        assert "DEBUG_CHEST_URL=http://127.0.0.1:3000" in capsys.readouterr().out
        assert devenv.main(["print", "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["profile"] == "native"
        assert data["vars"]["DEBUG_ARONA_URL"] == "http://127.0.0.1:8420"

    def test_print_bad_profile_rc2(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert devenv.main(["print", "--profile", "nope"]) == 2
        assert "unknown profile" in capsys.readouterr().err

    def test_run_without_command_rc2(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert devenv.main(["run"]) == 2
        assert "usage:" in capsys.readouterr().err
