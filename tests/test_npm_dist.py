"""Tests for the npm-dist generator (npm/packager.py + npm/dist.py)."""

import json
import subprocess
import sys

import pytest

from celestia_devtools.npm.packager import (
    SHIM_FILENAME,
    generate_postinstall_shim,
    generate_root_package,
    generate_subpackage,
)
from celestia_devtools.npm.platforms import PLATFORMS, find_platform
from celestia_devtools.npm.dist import main as dist_main

_HAS_NODE = pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node not available to validate generated JS syntax",
)


# ── platforms ────────────────────────────────────────────────────────────────


class TestPlatforms:
    def test_canonical_matrix_has_six_targets(self):
        targets = {p.rust_target for p in PLATFORMS}
        assert targets == {
            "x86_64-unknown-linux-gnu",
            "aarch64-unknown-linux-gnu",
            "x86_64-unknown-linux-musl",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "x86_64-pc-windows-msvc",
        }

    def test_suffixes_are_unique(self):
        suffixes = [p.npm_suffix for p in PLATFORMS]
        assert len(suffixes) == len(set(suffixes))

    def test_each_platform_names_a_leaf_node_pair(self):
        for p in PLATFORMS:
            assert p.node_os in {"linux", "darwin", "win32"}
            assert p.node_cpu in {"x64", "arm64"}

    def test_find_platform_round_trip(self):
        for p in PLATFORMS:
            assert find_platform(p.rust_target) is p
        assert find_platform("nope-not-a-target") is None


# ── packager (pure functions) ────────────────────────────────────────────────


class TestRootPackage:
    def test_has_scope_name_and_version(self):
        pkg = generate_root_package(
            scope="celestia-island", name="shirabe", version="1.2.3",
            platforms=PLATFORMS, binary="shirabe",
        )
        assert pkg["name"] == "@celestia-island/shirabe"
        assert pkg["version"] == "1.2.3"

    def test_optional_deps_cover_every_platform(self):
        pkg = generate_root_package(
            scope="celestia-island", name="shirabe", version="1.2.3",
            platforms=PLATFORMS, binary="shirabe",
        )
        deps = pkg["optionalDependencies"]
        assert set(deps.keys()) == {
            "@celestia-island/shirabe" + p.npm_suffix for p in PLATFORMS
        }
        assert all(v == "1.2.3" for v in deps.values())

    def test_bin_points_at_shim_and_postinstall_runs_it(self):
        pkg = generate_root_package(
            scope="celestia-island", name="shirabe", version="1.2.3",
            platforms=PLATFORMS, binary="shirabe",
        )
        assert pkg["bin"] == {"shirabe": SHIM_FILENAME}
        assert pkg["scripts"]["postinstall"] == f"node {SHIM_FILENAME}"

    def test_scope_leading_at_is_normalized(self):
        pkg = generate_root_package(
            scope="@celestia-island", name="x", version="0.0.1",
            platforms=PLATFORMS, binary="x",
        )
        assert pkg["name"] == "@celestia-island/x"


class TestSubpackage:
    def test_windows_leaf_gets_exe(self):
        win = find_platform("x86_64-pc-windows-msvc")
        pkg = generate_subpackage(
            scope="celestia-island", name="shirabe", version="1.0.0",
            platform=win, binary="shirabe",
        )
        assert pkg["os"] == ["win32"]
        assert pkg["cpu"] == ["x64"]
        assert pkg["files"] == ["shirabe.exe"]

    def test_unix_leaf_has_no_extension(self):
        linux = find_platform("x86_64-unknown-linux-gnu")
        pkg = generate_subpackage(
            scope="celestia-island", name="shirabe", version="1.0.0",
            platform=linux, binary="shirabe",
        )
        assert pkg["files"] == ["shirabe"]
        assert pkg["os"] == ["linux"]

    def test_subpackage_name_includes_suffix(self):
        mac = find_platform("aarch64-apple-darwin")
        pkg = generate_subpackage(
            scope="celestia-island", name="shirabe", version="1.0.0",
            platform=mac, binary="shirabe",
        )
        assert pkg["name"] == "@celestia-island/shirabe-darwin-arm64"


class TestShim:
    def test_shim_embeds_scope_name_binary(self):
        js = generate_postinstall_shim(
            scope="celestia-island", name="kou", binary="kou", platforms=PLATFORMS,
        )
        assert 'SCOPE = "@celestia-island"' in js
        assert 'NAME = "kou"' in js
        assert 'BINARY = "kou"' in js

    def test_shim_lists_every_platform(self):
        js = generate_postinstall_shim(
            scope="celestia-island", name="kou", binary="kou", platforms=PLATFORMS,
        )
        for p in PLATFORMS:
            assert repr(p.npm_suffix) in js
            assert repr(p.node_os) in js
            assert repr(p.node_cpu) in js

    @_HAS_NODE
    def test_generated_shim_is_syntactically_valid_js(self, tmp_path):
        js = generate_postinstall_shim(
            scope="celestia-island", name="kou", binary="kou", platforms=PLATFORMS,
        )
        f = tmp_path / "install.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# ── dist.main() (filesystem staging) ─────────────────────────────────────────


@pytest.fixture
def fake_binary(tmp_path):
    """A tiny fake native binary the generator can stage."""
    bin_path = tmp_path / "fakebin"
    bin_path.write_bytes(b"#!/bin/sh\necho hi\n")
    return bin_path


class TestStaging:
    def test_stages_one_subpackage_then_root(self, tmp_path, fake_binary, monkeypatch):
        out = tmp_path / "dist"
        monkeypatch.setattr(sys, "argv", [
            "npm-dist", "--name", "demo", "--version", "0.4.2",
            "--binary", str(fake_binary),
            "--rust-target", "x86_64-unknown-linux-gnu",
            "--out-dir", str(out),
        ])
        rc = dist_main()
        assert rc == 0

        # subpackage written with its binary + manifest
        sub = out / "demo-linux-x64"
        assert (sub / "demo").is_file()
        sub_pkg = json.loads((sub / "package.json").read_text(encoding="utf-8"))
        assert sub_pkg["name"] == "@celestia-island/demo-linux-x64"
        assert sub_pkg["files"] == ["demo"]

        # root manifest lists exactly that one platform
        root_pkg = json.loads((out / "package.json").read_text(encoding="utf-8"))
        assert root_pkg["name"] == "@celestia-island/demo"
        assert root_pkg["optionalDependencies"] == {"@celestia-island/demo-linux-x64": "0.4.2"}
        assert (out / SHIM_FILENAME).is_file()

    def test_incremental_assembly_grows_root_deps(self, tmp_path, fake_binary, monkeypatch):
        out = tmp_path / "dist"
        targets = [p.rust_target for p in PLATFORMS]
        for t in targets:
            monkeypatch.setattr(sys, "argv", [
                "npm-dist", "--name", "demo", "--version", "0.4.2",
                "--binary", str(fake_binary), "--rust-target", t,
                "--out-dir", str(out),
            ])
            assert dist_main() == 0

        root_pkg = json.loads((out / "package.json").read_text(encoding="utf-8"))
        assert set(root_pkg["optionalDependencies"]) == {
            "@celestia-island/demo" + p.npm_suffix for p in PLATFORMS
        }
        # one subpackage dir per platform
        sub_dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
        assert sub_dirs == sorted("demo" + p.npm_suffix for p in PLATFORMS)

    def test_root_only_reassembly_without_binary(self, tmp_path, fake_binary, monkeypatch):
        out = tmp_path / "dist"
        # stage two platforms first
        for t in ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin"]:
            monkeypatch.setattr(sys, "argv", [
                "npm-dist", "--name", "demo", "--version", "0.4.2",
                "--binary", str(fake_binary), "--rust-target", t,
                "--out-dir", str(out),
            ])
            assert dist_main() == 0

        # now reassemble with NO binary (final CI job pattern)
        monkeypatch.setattr(sys, "argv", [
            "npm-dist", "--name", "demo", "--out-dir", str(out),
        ])
        # version is inherited from the existing root package.json
        assert dist_main() == 0
        root_pkg = json.loads((out / "package.json").read_text(encoding="utf-8"))
        assert root_pkg["version"] == "0.4.2"
        assert set(root_pkg["optionalDependencies"]) == {
            "@celestia-island/demo-linux-x64",
            "@celestia-island/demo-darwin-arm64",
        }

    def test_unknown_rust_target_errors(self, tmp_path, fake_binary, monkeypatch):
        out = tmp_path / "dist"
        monkeypatch.setattr(sys, "argv", [
            "npm-dist", "--name", "demo", "--version", "0.1.0",
            "--binary", str(fake_binary),
            "--rust-target", "wasm32-unknown-unknown",
            "--out-dir", str(out),
        ])
        assert dist_main() == 1

    def test_binary_without_target_errors(self, tmp_path, fake_binary, monkeypatch):
        out = tmp_path / "dist"
        monkeypatch.setattr(sys, "argv", [
            "npm-dist", "--name", "demo", "--version", "0.1.0",
            "--binary", str(fake_binary), "--out-dir", str(out),
        ])
        assert dist_main() == 1
