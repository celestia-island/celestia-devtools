"""Tests for the verify-versions drift gate (repo/verify_versions.py)."""

import json

import pytest

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
    # The override mechanism itself: pinning the baseline to the line the
    # workspace already declares stays green (a pin that diverges from the
    # root's declared line is the F9 blind spot and must be red — see the
    # stale-pin tests below).
    _cargo_workspace(tmp_path, base="0.4.0")
    _hardcode_member(tmp_path, "crates/b", "0.4.0")
    _write(tmp_path / ".versions.toml", '[track]\ncargo = "0.4.0"\n')
    rc = main([str(tmp_path)])
    assert rc == 0


def test_changelog_file_is_ignored(tmp_path, capsys):
    """2026-09-26：CHANGELOG 规则已按 §4.2（禁维护 CHANGELOG）删除。

    组织规定 squash 后的 PR 历史本身就是 changelog——工具去咨询一个被禁的
    文件，既endorse 了违规范式、又永远不可能在合规仓上触发。遗留的陈旧
    CHANGELOG.md 现在必须被完全无视：不告警、不影响退出码。
    """
    _cargo_workspace(tmp_path)
    _changelog(tmp_path, "0.3.18")  # stale on purpose; must not matter
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN" not in out
    assert "0.3.18" not in out


def test_strict_flag_is_gone(tmp_path, capsys):
    """--strict 只服务于 CHANGELOG 告警；规则删了，旗子也删（未知参数必须报错）。"""
    _cargo_workspace(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), "--strict"])
    assert exc.value.code == 2


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

# ── fail-closed baselines & cargo fallback (2026-09-27, structural scan S1-1) ─


def _pure_workspace_shape(root):
    """Root [workspace] without [workspace.package] and without [package].

    The only shape left with an unresolvable cargo baseline: members carry
    hardcoded versions and no pin exists.  Must fail the gate closed.
    """
    _write(
        root / "Cargo.toml",
        '[workspace]\nmembers = ["crates/a", "crates/b"]\n',
    )
    _write(
        root / "crates/a/Cargo.toml",
        '[package]\nname = "a"\nversion = "0.1.0"\n',
    )
    _write(
        root / "crates/b/Cargo.toml",
        '[package]\nname = "b"\nversion = "0.1.0"\n',
    )


def _vestigial_workspace_shape(root, version="0.3.2"):
    """Single crate with a [workspace.lints]-style vestigial [workspace] table.

    Before 2026-09-27 the mere presence of a ``[workspace]`` dict blocked the
    root ``[package].version`` fallback, silently skipping the whole track.
    """
    _write(
        root / "Cargo.toml",
        f'[package]\nname = "solo"\nversion = "{version}"\n\n'
        '[workspace.lints.rust]\nwarnings = "deny"\n',
    )


def test_root_package_version_falls_through_vestigial_workspace(tmp_path, capsys):
    _vestigial_workspace_shape(tmp_path)
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cargo=0.3.2" in out


def test_root_crate_plus_members_uses_root_line_as_baseline(tmp_path, capsys):
    # kirino's real shape: root [package].version + real members; with the
    # fallback the track gains coverage — a member left behind (kirino-macro
    # at 0.7.0 while the root moved to 0.7.5) is now DRIFT, not silence.
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "root-crate"\nversion = "0.7.5"\n\n'
        '[workspace]\nmembers = ["packages/macro"]\n',
    )
    _write(
        tmp_path / "packages/macro/Cargo.toml",
        '[package]\nname = "macro"\nversion = "0.7.0"\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "packages/macro" in out
    assert "0.7.5" in out


def test_unresolvable_cargo_baseline_fails_closed(tmp_path, capsys):
    _pure_workspace_shape(tmp_path)
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "baseline" in out
    assert "workspace.package" in out


def test_track_override_rescues_unresolvable_baseline(tmp_path, capsys):
    _pure_workspace_shape(tmp_path)
    _write(
        tmp_path / ".versions.toml",
        '[track]\ncargo = "0.1.0"\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cargo=0.1.0" in out


def test_exempt_alone_does_not_rescue_baseline(tmp_path):
    _pure_workspace_shape(tmp_path)
    _write(
        tmp_path / ".versions.toml",
        '[exempt.cargo]\n"crates/a" = "parked"\n',
    )
    rc = main([str(tmp_path)])
    assert rc == 1


def test_no_cargo_track_at_all_still_passes(tmp_path, capsys):
    _write(tmp_path / "README.md", "no rust here\n")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No version drift" in out


def test_versionless_workspace_manifest_passes(tmp_path):
    # A bare [workspace] with no versioned manifests anywhere: nothing to gate.
    _write(tmp_path / "Cargo.toml", '[workspace]\nmembers = []\n')
    rc = main([str(tmp_path)])
    assert rc == 0


def test_unresolvable_npm_baseline_fails_closed(tmp_path, capsys):
    # All packages private → no publishable majority → baseline unresolvable,
    # yet versions exist, so the track is present and must be pinned.
    _package(tmp_path, "apps/web", "1.2.3", private=True)
    _package(tmp_path, "apps/cli", "1.2.3", private=True)
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[track] npm" in out


def test_json_output_includes_baseline_errors(tmp_path, capsys):
    _pure_workspace_shape(tmp_path)
    rc = main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["errors"]
    assert "cargo" in payload["errors"][0]


# ── F9 (R2): a stale [track] cargo pin must not hide a root-only bump ────────


def _root_crate_pin_shape(root, root_version, pin="0.7.5"):
    _write(
        root / "Cargo.toml",
        f'[package]\nname = "root-crate"\nversion = "{root_version}"\n\n'
        '[workspace]\nmembers = ["packages/macro"]\n',
    )
    _write(
        root / "packages/macro/Cargo.toml",
        '[package]\nname = "macro"\nversion = "0.7.5"\n',
    )
    _write(root / ".versions.toml", f'[track]\ncargo = "{pin}"\n')


def test_stale_pin_hides_root_only_bump_no_longer_green(tmp_path, capsys):
    # R2's S3 shape: root bumped to 0.7.6 while the pin and members stay at
    # 0.7.5 — the root used to escape all comparison; now it must be red.
    _root_crate_pin_shape(tmp_path, root_version="0.7.6")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "  cargo  ." in out
    assert "bump the pin" in out


def test_pin_matching_root_stays_green(tmp_path, capsys):
    _root_crate_pin_shape(tmp_path, root_version="0.7.5")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cargo=0.7.5" in out


def test_root_not_compared_without_pin(tmp_path, capsys):
    # Without an override the root IS the baseline (fallback), so a
    # root-only move must flag the members it left behind, not the root.
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "root-crate"\nversion = "0.7.6"\n\n'
        '[workspace]\nmembers = ["packages/macro"]\n',
    )
    _write(
        tmp_path / "packages/macro/Cargo.toml",
        '[package]\nname = "macro"\nversion = "0.7.5"\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "packages/macro" in out
    assert "  cargo  ." not in out


# ── corruption must fail loudly (2026-10-03 master incident) ─────────────────
# A squash merge shipped duplicate `version` keys: Cargo.toml carried two
# `version =` lines in [workspace.package] (invalid TOML) and the npm
# manifests two "version" keys (valid JSON that last-wins silently). The
# gate answered "no drift" and exit 0 — corruption used to be
# indistinguishable from absence at the loader boundary.


def test_duplicate_toml_version_keys_fail_loudly(tmp_path, capsys):
    """The exact master-incident shape: two version lines in
    [workspace.package]."""
    _write(
        tmp_path / "Cargo.toml",
        '[workspace]\nmembers = ["crates/a"]\n\n'
        '[workspace.package]\nversion = "0.1.334"\nversion = "0.1.331"\nedition = "2024"\n',
    )
    _write(
        tmp_path / "crates/a/Cargo.toml",
        '[package]\nname = "a"\nversion.workspace = true\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1, f"invalid TOML must fail the gate, got exit {rc}: {out}"
    assert "Cargo.toml unparseable" in out
    assert "Cannot overwrite a value" in out  # tomllib names the duplicate


def test_duplicate_json_version_keys_fail_loudly(tmp_path, capsys):
    """json.loads silently last-wins a duplicate key — the hook must turn
    that into a gate failure naming the file."""
    _write(
        tmp_path / "package.json",
        '{\n  "name": "x",\n  "version": "0.1.369",\n  "version": "0.1.367"\n}\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "duplicate key 'version'" in out


def test_malformed_json_manifest_fails_loudly(tmp_path, capsys):
    _write(tmp_path / "package.json", "{ not json")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "package.json unparseable" in out


def test_corrupt_member_manifest_is_named(tmp_path, capsys):
    """A corrupt NON-root manifest must be named and fail the gate too —
    skipping just its version check would under-report the track."""
    _package(tmp_path, "packages/webui", "0.3.19")
    _package(tmp_path, "packages/ide", "0.3.19")
    _write(tmp_path / "package.json", '{"name": "root", "version": "0.3.19"}\n')
    _write(tmp_path / "packages/broken/package.json", "{ oops")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "packages/broken/package.json unparseable" in out


def test_corrupt_versions_config_fails_loudly(tmp_path, capsys):
    """A present-but-corrupt .versions.toml must not silently disable
    exemptions/overrides."""
    _cargo_workspace(tmp_path)
    _write(tmp_path / ".versions.toml", "[exempt\n")
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert ".versions.toml unparseable" in out


def test_healthy_repo_still_green(tmp_path, capsys):
    """Positive control: absence of files stays fine; nothing new fires."""
    _cargo_workspace(tmp_path)
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "unparseable" not in out


def test_corrupt_root_under_track_pin_fails_loudly(tmp_path, capsys):
    """R1 F1: with a [track] cargo pin, detect_cargo_base returns the
    override without ever loading the root — a corrupt root must still
    fail the gate (the pinned-repo variant of the master incident)."""
    _write(
        tmp_path / "Cargo.toml",
        '[workspace.package]\nversion = "0.1.334"\nversion = "0.1.331"\n',
    )
    _write(
        tmp_path / ".versions.toml",
        '[track]\ncargo = "0.1.331"\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1, f"corrupt root under a pin must fail, got exit {rc}: {out}"
    assert "Cargo.toml unparseable" in out


def test_sole_corrupt_member_without_baseline_fails_loudly(tmp_path, capsys):
    """R1 F2: unversioned root + a sole CORRUPT versioned member — no
    baseline resolves and the collector never walks; the presence probe
    must both count it as present and name it."""
    _write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["crates/a"]\n')
    _write(
        tmp_path / "crates/a/Cargo.toml",
        '[package]\nname = "a"\nversion = "0.1.1"\nversion = "0.1.0"\n',
    )
    rc = main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "crates/a/Cargo.toml unparseable" in out
