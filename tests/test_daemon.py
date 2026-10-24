"""Tests for the malkuth daemon service definitions (env/daemon.py).

The defs encode how the local daemon pods are built and configured; two
defect classes slipped through before (2026-10-16): ghost cargo features
that make `daemon build` fail outright (chest "mock-mode", scepter
"embedded-db" — neither exists in those Cargo.tomls) and missing listen
port envs, so every pod came up on its default port instead of run_port
(chest :80, scepter :8424, arona :8420). These tests pin the corrected
contract: no ghost features, every pod pins its real port variable to
run_port, and generate-config emits Windows-correct binary paths.
"""

from __future__ import annotations
import sys
from pathlib import Path
from celestia_devtools.env import daemon

# Each service's actual listen knob (verified against each repo's source):
# chest → SHITTIM_CHEST_PORT (config.rs), scepter → SERVER_BIND_ADDRESS
# (setup.rs), arona → ARONA_PORT (config.rs).
EXPECTED_PORT_ENV: dict[str, tuple[str, str]] = {
    "shittim-chest": ("SHITTIM_CHEST_PORT", "8400"),
    "entelecheia": ("SERVER_BIND_ADDRESS", "127.0.0.1:8410"),
    "arona": ("ARONA_PORT", "8405")
}

class test_service_defs:
    def test_no_ghost_cargo_features(self):
        """Both features removed on 2026-10-16 do not exist in the target
        Cargo.tomls — any feature flag here must be re-verified by hand."""
        ghosts = {"mock-mode", "embedded-db"}

        for repo, svc in daemon.SERVICE_DEFS.items():
            for flag in svc["build_cmd"]:
                assert flag not in ghosts, f"{repo}: ghost feature {flag}"

    def test_every_pod_pins_its_listen_port_to_run_port(self):
        for repo, (env_key, expected) in EXPECTED_PORT_ENV.items():
            svc = daemon.SERVICE_DEFS[repo]
            assert svc["env_defaults"].get(env_key) == expected, (f"{repo}: {env_key} must be pinned to run_port " f"{svc['run_port']} or the pod lands on its default port")

    def test_expected_services_present(self):
        assert set(daemon.SERVICE_DEFS) >= {"shittim-chest", "entelecheia", "arona"}

class test_generate_config:
    def _fake_layout(self, tmp_path: Path) -> Path:
        """Sibling checkouts with fake release binaries; returns the cwd to
        run from (a directory whose parent holds the fakes). _find_sibling_dir
        only accepts dirs carrying a Cargo.toml, so each fake gets one."""
        suffix = ".exe" if sys.platform == "win32" else ""

        for repo, svc in daemon.SERVICE_DEFS.items():
            (tmp_path / repo).mkdir(parents = True)
            (tmp_path / repo / "Cargo.toml").write_text("[package]\n", encoding = "utf-8")
            bin_dir = tmp_path / repo / "target" / "release"
            bin_dir.mkdir(parents = True)
            (bin_dir / (svc["bin"] + suffix)).write_bytes(b"fake")

        cwd = tmp_path / "launcher"
        cwd.mkdir()
        return cwd

    def test_emits_pinned_ports_and_real_binary_names(self, tmp_path, monkeypatch, capsys):
        cwd = self._fake_layout(tmp_path)
        monkeypatch.chdir(cwd)
        out = tmp_path / "services.toml"
        assert daemon.cmd_generate_config(["shittim-chest", "entelecheia", "arona"], str(out)) == 0
        toml = out.read_text(encoding = "utf-8")
        suffix = ".exe" if sys.platform == "win32" else ""

        for repo, svc in daemon.SERVICE_DEFS.items():
            assert f'id = "{repo}"' in toml
            assert (svc["bin"] + suffix) in toml  # cargo's real output name

        for env_key, expected in EXPECTED_PORT_ENV.values():
            assert f'{env_key} = "{expected}"' in toml

        assert ("mock-mode" not in toml) and ("embedded-db" not in toml)
        assert toml.count("[[services]]") == 3

    def test_missing_binary_is_skipped_with_hint(self, tmp_path, monkeypatch, capsys):
        cwd = self._fake_layout(tmp_path)
        (cwd.parent / "arona" / "target" / "release" / ("_cli" + (".exe" if sys.platform == "win32" else ""))).unlink()
        monkeypatch.chdir(cwd)
        out = tmp_path / "services.toml"
        assert daemon.cmd_generate_config(["arona"], str(out)) == 0
        captured = capsys.readouterr()
        assert "binary not found" in captured.err
        assert out.exists()                     # header is still written
        assert "[[services]]" not in out.read_text(encoding = "utf-8")  # no pod
