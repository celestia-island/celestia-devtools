"""Tests for the deploy subpackage: cli dispatch, doctor report, probes."""

from __future__ import annotations

import json

from celestia_devtools.core import cli as core_cli
from celestia_devtools.deploy import cli as deploy_cli, probe


class TestRegistry:
    def test_deploy_registered_in_commands(self):
        assert core_cli.COMMANDS.get("deploy") == "celestia_devtools.deploy.cli"


class TestDispatch:
    def test_bare_deploy_exits_2_and_points_at_doctor(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["deploy"])
        assert deploy_cli.main() == 2
        assert "doctor" in capsys.readouterr().out

    def test_help_flags_exit_0(self, monkeypatch, capsys):
        for flag in ("-h", "--help", "help"):
            monkeypatch.setattr("sys.argv", ["deploy", flag])
            assert deploy_cli.main() == 0
            capsys.readouterr()

    def test_unimplemented_subcommand_fails_loudly(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["deploy", "bootstrap"])
        assert deploy_cli.main() == 2
        err = capsys.readouterr().err
        assert "not implemented" in err and "bootstrap" in err


class TestDoctor:
    """R1-F5: hermetic by injection — no real detect(), no subprocess probes,
    no WPAD web fetches on whatever machine runs the suite."""

    def _fixture(self, monkeypatch, *, useradd_ok=True):
        from celestia_devtools.core import deps, netproxy
        monkeypatch.setattr(
            netproxy, "detect",
            lambda **kw: netproxy.ProxyConfig(
                "http://user:pass@203.0.113.7:7890", "env:HTTPS_PROXY",
                ("localhost", "198.18.0.0/15")))
        monkeypatch.setattr(
            deps, "ensure",
            lambda *a, **k: [deps.EnsureResult("PyYAML", True, "present", "6.0.2")])
        probes = [
            probe.Probe("python", True, "3.11.2"),
            probe.Probe("systemd", True, "255"),
            probe.Probe("useradd", useradd_ok, "", hint="passwd package"),
            probe.Probe("acme.sh", False, "", hint="curl https://get.acme.sh"),
        ]
        monkeypatch.setattr(probe, "all_probes", lambda: probes)

    def test_json_output_shape_and_redaction(self, monkeypatch, capsys):
        self._fixture(monkeypatch)
        monkeypatch.setattr("sys.argv", ["deploy", "doctor", "--json"])
        rc = deploy_cli.main()
        assert rc == 0  # fixture has no hard miss
        data = json.loads(capsys.readouterr().out)
        assert set(data) >= {"proxy", "deps", "probes", "hard_missing"}
        assert data["proxy"]["display"] == "203.0.113.7:7890"  # creds redacted
        assert "proxy_url" not in data["proxy"]  # the full URL never rides along
        assert data["hard_missing"] == []
        soft = {p["name"] for p in data["probes"] if p["soft"]}
        assert soft == {"acme.sh"} and "acme.sh" in probe.SOFT

    def test_hard_missing_drives_exit_code(self, monkeypatch, capsys):
        self._fixture(monkeypatch, useradd_ok=False)
        monkeypatch.setattr("sys.argv", ["deploy", "doctor"])
        rc = deploy_cli.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "硬缺口" in out and "useradd" in out and "代理" in out


class TestProbes:
    def test_all_probes_shapes(self):
        probes = probe.all_probes()
        assert probes and all(isinstance(p, probe.Probe) for p in probes)
        for p in probes:
            if not p.ok:
                assert p.hint, "a failing probe owes the operator a hint: " + p.name

    def test_python_floor_ok_on_current_interpreter(self):
        assert probe.check_python_floor().ok  # CI runs 3.11+

    def test_unknown_probe_id_reported(self):
        p = probe.check(("no-such-probe",))[0]
        assert not p.ok and "unknown probe id" in p.hint
