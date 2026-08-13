"""Tests for the verify-versions drift gate (repo/verify_versions.py)."""

import json

from celestia_devtools.repo.verify_versions import main, verify


# ── fixture helpers ──────────────────────────────────────────────────────────


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cargo_workspace(root, base="0.3.19"):
    """A two-member workspace both inheriting the baseline version."""
    _write(
        root / "Cargo.toml",
        '[workspace]\n'
        'members = ["crates/a", "crates/b"]\n\n'
        f'[workspace.package]\nversion = "{base}"\n',
    )
    for name in ("a", "b"):
        _write(
            root / f"crates/{name}/Cargo.toml",
            f'[package]\nname = "{name}"\nversion.workspace = true\n',
        )


def _hardcode_member(root, rel_dir, version):
    _write(
        root / rel_dir / "Cargo.toml",
        f'[package]\nname = "{rel_dir.split("/")[-1]}"\nversion = "{version}"\n',
    )


def _package(root, rel_dir, version, private=False):
    pkg = {"name": rel_dir.replace("/", "-"), "version": version}
    if private:
        pkg["private"] = True
    _write(root / rel_dir / "package.json", json.dumps(pkg))


def _changelog(root, top_version):
    _write(
        root / "CHANGELOG.md",
        "# Changelog\n\n## [Unreleased]\n\n"
        f"## [{top_version}] - 2026-01-01\n",
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_all_consistent_exit_0(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _changelog(tmp_path, "0.3.19")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No version drift" in out
    assert "cargo=0.3.19" in out


def test_hardcoded_member_drift_exit_1(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _hardcode_member(tmp_path, "crates/b", "0.3.0")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "crates/b" in out
    assert "0.3.0" in out
    assert "0.3.19" in out


def test_standalone_crate_drift_detected(tmp_path, capsys):
    """A crate outside [workspace].members (e.g. packages/e2e) is still gated."""
    _cargo_workspace(tmp_path)
    _hardcode_member(tmp_path, "packages/e2e", "0.1.0")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "packages/e2e" in out


def test_npm_drift_exit_1(tmp_path, capsys):
    _package(tmp_path, ".", "0.4.5")
    _package(tmp_path, "packages/theme", "0.4.6")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "packages/theme" in out
    assert "0.4.6" in out
    assert "0.4.5" in out


def test_private_package_still_checked(tmp_path, capsys):
    _package(tmp_path, ".", "0.4.5")
    _package(tmp_path, "packages/theme", "0.4.6", private=True)
    rc = main([str(tmp_path)])
    assert rc == 1
    assert "packages/theme" in capsys.readouterr().out


def test_dual_track_consistent_exit_0(tmp_path, capsys):
    _cargo_workspace(tmp_path, base="0.3.19")
    _package(tmp_path, ".", "0.4.5")
    _package(tmp_path, "packages/vue", "0.4.5")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cargo=0.3.19" in out
    assert "npm=0.4.5" in out


def test_exemption_skips_cargo_package(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _hardcode_member(tmp_path, "crates/b", "0.3.0")
    _write(
        tmp_path / ".versions.toml",
        '[exempt.cargo]\n"crates/b" = "test-only crate"\n',
    )
    rc = main([str(tmp_path)])
    assert rc == 0
    assert "No version drift" in capsys.readouterr().out


def test_track_override_in_config(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _hardcode_member(tmp_path, "crates/b", "0.4.0")
    _write(tmp_path / ".versions.toml", '[track]\ncargo = "0.4.0"\n')
    rc = main([str(tmp_path)])
    assert rc == 0


def test_changelog_warning_default_non_fatal(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _changelog(tmp_path, "0.3.18")  # mismatched against cargo 0.3.19
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN" in out
    assert "0.3.18" in out


def test_changelog_warning_strict_fails(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _changelog(tmp_path, "0.3.18")
    rc = main([str(tmp_path), "--strict"])
    assert rc == 1
    assert "WARN" in capsys.readouterr().out


def test_json_output_drift(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    _hardcode_member(tmp_path, "crates/b", "0.3.0")
    rc = main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["cargo"]["base"] == "0.3.19"
    drifts = payload["cargo"]["drifts"]
    assert len(drifts) == 1
    assert drifts[0]["package"] == "crates/b"
    assert drifts[0]["actual"] == "0.3.0"


def test_json_output_clean(tmp_path, capsys):
    _cargo_workspace(tmp_path)
    rc = main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["cargo"]["drifts"] == []


def test_verify_returns_report(tmp_path):
    _cargo_workspace(tmp_path)
    result = verify(tmp_path)
    assert result.cargo_base == "0.3.19"
    assert result.npm_base is None
    assert result.has_drift is False
