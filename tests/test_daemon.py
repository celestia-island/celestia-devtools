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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest
from celestia_devtools.env import daemon

# Each service's actual listen knob (verified against each repo's source):
# chest → SHITTIM_CHEST_PORT (config.rs), scepter → SERVER_BIND_ADDRESS
# (setup.rs), arona → ARONA_PORT (config.rs).
EXPECTED_PORT_ENV: dict[str, tuple[str, str]] = {"shittim-chest": ("SHITTIM_CHEST_PORT", "8400"), "entelecheia": ("SERVER_BIND_ADDRESS", "127.0.0.1:8410"), "arona": ("ARONA_PORT", "8405")}

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

    def test_build_cmd_names_a_package_and_a_bin(self):
        """Every `build_cmd` is `cargo build -p <package> [--bin <bin>] --release`,
        the `-p` value is not the pre-rename vocabulary, and `--bin` equals the
        bin the daemon runs. The structural half; `test_build_cmd_packages_resolve`
        asks cargo when a checkout is there."""

        for repo, svc in daemon.SERVICE_DEFS.items():
            cmd = svc["build_cmd"]
            assert cmd[:3] == ["cargo", "build", "-p"], (repo, cmd)
            pkg = cmd[3]
            assert pkg and not pkg.startswith("-"), (repo, cmd)
            assert pkg not in {"core", "shittim_chest"}, (repo, f"stale package name {pkg!r}")

            if "--bin" in cmd:
                assert cmd[cmd.index("--bin") + 1] == svc["bin"], (repo, "--bin must equal the bin the daemon runs")

            assert "--release" in cmd, (repo, cmd)

    def test_build_cmd_packages_resolve(self):
        """Every `-p` package must resolve in its repo and own the bin the daemon
        runs — asking cargo, not a string table. A rename that misses this file
        (shittim-chest#2514 did) fails here with cargo's own message. The
        checkout is found from this file's own path, so it runs in the
        `_worktree/<repo>/<slug>` layout the workspace gates use; skips only
        when no checkout exists (CI)."""

        if shutil.which("cargo") is None:
            pytest.skip("cargo not on PATH")

        checked = 0

        for repo, svc in daemon.SERVICE_DEFS.items():
            d = _sibling_checkout(repo)

            if d is None:
                continue

            pkg = svc["build_cmd"][3]
            env = {**os.environ, "CARGO_TERM_COLOR": "never"}
            r = subprocess.run(["cargo", "pkgid", "-p", pkg, "--offline"], cwd = d, capture_output = True, text = True, env = env, timeout = 120)
            assert r.returncode == 0, f"{repo}: `cargo build -p {pkg}` would fail ({d}):\n{r.stderr.strip()}"

            m = subprocess.run(["cargo", "metadata", "--no-deps", "--offline", "--format-version", "1"], cwd = d, capture_output = True, text = True, env = env, timeout = 120)
            assert m.returncode == 0, f"{repo}: cargo metadata failed ({d}):\n{m.stderr.strip()}"

            bins = {t["name"] for p in json.loads(m.stdout)["packages"] if p["name"] == pkg for t in p["targets"] if "bin" in t["kind"]}
            assert svc["bin"] in bins, f"{repo}: package {pkg!r} has no bin target {svc['bin']!r} (has {sorted(bins)})"
            checked += 1

        if checked == 0:
            pytest.skip("no sibling checkouts next to this repo")

def _sibling_checkout(repo: str) -> Path | None:
    """The checkout of `repo` as a sibling, found from this file rather than the
    cwd: inside `_worktree/<this-repo>/<slug>` the siblings live two levels up;
    otherwise the CLI's own rule (`cwd.parent / repo`) applies."""

    for anc in Path(__file__).resolve().parents:
        if anc.parent.name == "_worktree":
            d = anc.parent.parent / repo

            if d.is_dir() and (d / "Cargo.toml").exists():
                return d

    return daemon._find_sibling_dir(repo)

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
