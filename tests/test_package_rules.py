"""Tests for the W0 package rules (lint/package_rules.py)."""

from __future__ import annotations
import json
from pathlib import Path
from celestia_devtools.lint.package_rules import main

#: One name from two versions of the SAME registry source (allowed) plus a
#: path/workspace member (no ``source`` key) and an unrelated crate.
CLEAN_LOCK = """version = 3

[[package]]
name = "plana"
version = "0.2.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaaa"

[[package]]
name = "plana"
version = "0.2.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bbbb"

[[package]]
name = "plana_res"
version = "0.2.0"

[[package]]
name = "serde"
version = "1.0.200"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cccc"
"""

#: The W0 failure shape: ``plana`` from crates.io AND from a master-branch git
#: edge at the same time.
DUAL_LOCK = """version = 3

[[package]]
name = "plana"
version = "0.2.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaaa"

[[package]]
name = "plana"
version = "0.2.4"
source = "git+https://github.com/celestia-island/plana?branch=master#0123456789abcdef0123456789abcdef01234567"
"""

def write_file(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents = True, exist_ok = True)
    path.write_text(body, encoding = "utf-8")
    return path

def write_package(root: Path, body: str) -> Path:
    return write_file(root, "Cargo.toml", "[package]\n" + body)

# ── check A: publishable package names are kebab-case ────────────────────────

def test_kebab_case_publishable_package_passes(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana-sync"\nversion = "0.2.0"\n')
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "package-rules: 0 violation(s) in 1 Cargo.toml and 0 Cargo.lock" in captured.err
    assert "warning:" not in captured.err

def test_underscore_publishable_package_is_a_violation(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana_sync"\nversion = "0.2.0"\n')
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "warning: Cargo.toml: [package-name]" in captured.err
    assert "plana_sync" in captured.err
    assert "must be kebab-case" in captured.err
    assert "advisory mode" in captured.err

def test_underscore_package_with_publish_false_is_exempt(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana_sync"\nversion = "0.2.0"\npublish = false\n')
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "package-rules: 0 violation(s)" in captured.err
    assert "warning:" not in captured.err

def test_publish_false_inherited_from_the_workspace_package_is_exempt(tmp_path, capsys):
    # `publish.workspace = true` is cargo's inheritance spelling; the parent
    # package table has no local `publish` key of its own.
    body = '[workspace]\n\n[workspace.package]\npublish = false\n\n[package]\nname = "plana_timeline"\nversion = "0.2.0"\npublish.workspace = true\n'
    write_file(tmp_path, "Cargo.toml", body)
    assert main([str(tmp_path)]) == 0
    assert "package-rules: 0 violation(s)" in capsys.readouterr().err

def test_workspace_package_name_is_not_a_package_name(tmp_path, capsys):
    # W0 says `[workspace.package]` is not a package: only a real
    # `[package].name` can own a crates.io coordinate.
    body = '[workspace.package]\nname = "plana_workspace_name"\nversion = "0.2.0"\n'
    write_file(tmp_path, "Cargo.toml", body)
    assert main([str(tmp_path)]) == 0
    assert "package-rules: 0 violation(s)" in capsys.readouterr().err

# ── check B: one lockfile, one source per package name ───────────────────────

def test_lock_with_registry_and_git_sources_is_a_violation(tmp_path, capsys):
    write_file(tmp_path, "Cargo.lock", DUAL_LOCK)
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "warning: Cargo.lock: [lock-source]" in captured.err
    assert "resolves from 2 different sources" in captured.err
    assert "registry+https://github.com/rust-lang/crates.io-index (0.2.5)" in captured.err
    assert "git+https://github.com/celestia-island/plana?branch=master#0123456789abcdef0123456789abcdef01234567 (0.2.4)" in captured.err

def test_lock_with_path_and_registry_sources_is_a_violation(tmp_path, capsys):
    # A lock entry without `source` is a path dependency or workspace member;
    # it is a distinct source on its own, not "unknown".
    body = 'version = 3\n\n[[package]]\nname = "plana-infra-utils"\nversion = "0.2.0"\n\n[[package]]\nname = "plana-infra-utils"\nversion = "0.2.0"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "aaaa"\n'
    write_file(tmp_path, "Cargo.lock", body)
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "resolves from 2 different sources" in captured.err
    assert "path/workspace (no source) (0.2.0)" in captured.err

def test_lock_with_two_git_revisions_is_a_violation(tmp_path, capsys):
    # W4's remaining floating edges: the same git URL pinned at two revs is
    # two sources, even though the package name and version are identical.
    body = 'version = 3\n\n[[package]]\nname = "scriptum"\nversion = "0.1.0"\nsource = "git+https://github.com/celestia-island/scriptum?rev=aaa#1111111111111111111111111111111111111111"\n\n[[package]]\nname = "scriptum"\nversion = "0.1.0"\nsource = "git+https://github.com/celestia-island/scriptum?rev=bbb#2222222222222222222222222222222222222222"\n'
    write_file(tmp_path, "Cargo.lock", body)
    assert main([str(tmp_path), "--strict"]) == 1
    assert "resolves from 2 different sources" in capsys.readouterr().err

def test_clean_lock_with_one_source_per_name_passes(tmp_path, capsys):
    write_file(tmp_path, "Cargo.lock", CLEAN_LOCK)
    assert main([str(tmp_path), "--strict"]) == 0
    captured = capsys.readouterr()
    assert "package-rules: 0 violation(s) in 0 Cargo.toml and 1 Cargo.lock" in captured.err
    assert "warning:" not in captured.err

# ── mode: advisory by default, strict on request ─────────────────────────────

def test_strict_exits_one_and_advisory_exits_zero(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana_sync"\nversion = "0.2.0"\n')
    assert main([str(tmp_path)]) == 0
    assert "advisory mode" in capsys.readouterr().err
    assert main([str(tmp_path), "--strict"]) == 1
    assert "advisory mode" not in capsys.readouterr().err

# ── zero-hit self-proof and scan errors ──────────────────────────────────────

def test_empty_directory_exits_two_instead_of_passing(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "nothing scanned" in captured.err
    assert "0 violation(s)" not in captured.err

def test_a_manifest_under_target_is_not_scanned(tmp_path, capsys):
    write_file(tmp_path / "target", "Cargo.toml", '[package]\nname = "plana_sync"\n')
    assert main([str(tmp_path)]) == 2
    assert "nothing scanned" in capsys.readouterr().err

def test_invalid_manifest_toml_exits_two(tmp_path, capsys):
    write_file(tmp_path, "Cargo.toml", '[package\nname = "x"\n')
    assert main([str(tmp_path)]) == 2
    assert "not valid TOML" in capsys.readouterr().err

def test_package_table_without_a_name_exits_two(tmp_path, capsys):
    write_file(tmp_path, "Cargo.toml", '[package]\nversion = "0.2.0"\n')
    assert main([str(tmp_path)]) == 2
    assert "has no string 'name'" in capsys.readouterr().err

def test_missing_path_exits_two(tmp_path, capsys):
    assert main([str(tmp_path / "does-not-exist")]) == 2
    assert "not a directory" in capsys.readouterr().err

# ── machine mode ─────────────────────────────────────────────────────────────

def test_json_report_is_parseable_and_complete(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana_sync"\nversion = "0.2.0"\n')
    write_file(tmp_path, "Cargo.lock", DUAL_LOCK)
    assert main([str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["manifests"] == 1
    assert payload["locks"] == 1
    assert payload["strict"] is False
    assert payload["advisory"] is True
    assert {item["check"] for item in payload["violations"]} == {"package-name", "lock-source"}
    lock = [item for item in payload["violations"] if item["check"] == "lock-source"][0]
    assert lock["package"] == "plana"
    assert len(lock["sources"]) == 2
    # Sources are ordered None-first, then lexicographically: git before registry.
    assert [use["versions"] for use in lock["sources"]] == [["0.2.4"], ["0.2.5"]]
    assert captured.err == ""

def test_json_strict_exits_one(tmp_path, capsys):
    write_package(tmp_path, 'name = "plana_sync"\nversion = "0.2.0"\n')
    assert main([str(tmp_path), "--json", "--strict"]) == 1
    assert json.loads(capsys.readouterr().out)["strict"] is True

def test_json_error_document_is_emitted_for_a_parse_error(tmp_path, capsys):
    write_file(tmp_path, "Cargo.toml", '[package\nname = "x"\n')
    assert main([str(tmp_path), "--json"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert any("Cargo.toml: not valid TOML" in error for error in payload["errors"])
    # Machine mode carries the errors in the document; stdout stays one JSON
    # document and stderr stays quiet unless the scan cannot start at all.
    assert captured.err == ""
