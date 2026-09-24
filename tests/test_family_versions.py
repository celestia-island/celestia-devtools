"""Tests for the family dependency-identity check (repo/family_versions.py)."""

import json

from celestia_devtools.repo.family_versions import check, main


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cargo(root, body, path="Cargo.toml"):
    _write(root / path, body)


def _names(findings):
    return [(f.level, f.subject) for f in findings]


# ── kirino: one major, or it is a different primitive ────────────────────────


def test_kirino_below_the_family_floor_is_a_violation(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    findings = check(tmp_path)
    assert [f.level for f in findings] == ["violation"], _names(findings)
    assert "below the family's ^0.7" in findings[0].message


def test_kirino_at_the_family_floor_passes(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = { version = "^0.7", features = ["auth-jwt"] }\n')
    assert check(tmp_path) == []


def test_kirino_from_git_master_warns_but_does_not_fail(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nkirino = { git = "https://github.com/celestia-island/kirino.git",'
        ' branch = "master" }\n',
    )
    findings = check(tmp_path)
    assert [f.level for f in findings] == ["warning"], _names(findings)


def test_workspace_dependencies_are_checked_too(tmp_path):
    _cargo(tmp_path, '[workspace.dependencies]\nkirino = "^0.6"\n')
    assert [f.level for f in check(tmp_path)] == ["violation"]


def test_a_dev_dependency_declaration_is_checked(tmp_path):
    _cargo(tmp_path, '[dev-dependencies]\nkirino = "^0.6"\n')
    assert [f.level for f in check(tmp_path)] == ["violation"]


# ── plana: master, not a station ─────────────────────────────────────────────


def test_plana_git_master_passes(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nplana = { git = "https://github.com/celestia-island/plana.git",'
        ' branch = "master" }\n',
    )
    assert check(tmp_path) == []


def test_plana_subcrates_are_checked(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nplana_config = { git = "https://github.com/celestia-island/plana.git",'
        ' rev = "b4e7de35" }\n',
    )
    assert [f.level for f in check(tmp_path)] == ["violation"]


def test_plana_rev_pin_is_allowed_only_for_frozen_repositories(tmp_path):
    _cargo(tmp_path, '[dependencies]\nplana = { package = "plana", git = "x", rev = "abc" }\n')
    # No origin remote in the fixture → the repository name is the directory name.
    assert [f.level for f in check(tmp_path)] == ["violation"]

    frozen = tmp_path / "scriptum"
    _cargo(frozen, '[dependencies]\nplana = { package = "plana", git = "x", rev = "abc" }\n')
    assert check(frozen) == []


def test_plana_from_the_registry_is_a_violation(tmp_path):
    _cargo(tmp_path, '[dependencies]\nplana = "0.2"\n')
    findings = check(tmp_path)
    assert [f.level for f in findings] == ["violation"], _names(findings)
    assert "crates.io" in findings[0].message


def test_a_git_dependency_without_a_ref_warns(tmp_path):
    _cargo(tmp_path, '[dependencies]\nplana = { git = "https://example.invalid/plana.git" }\n')
    assert [f.level for f in check(tmp_path)] == ["warning"]


# ── hikari: unbounded ranges are the audit finding ───────────────────────────


def test_hikari_unbounded_range_warns(tmp_path):
    _write(
        tmp_path / "packages/webui/package.json",
        json.dumps({"dependencies": {"@celestia-island/hikari": "^*"}}),
    )
    findings = check(tmp_path)
    assert [f.level for f in findings] == ["warning"], _names(findings)
    assert "any future major" in findings[0].message


def test_hikari_bounded_range_passes(tmp_path):
    _write(
        tmp_path / "packages/webui/package.json",
        json.dumps({"dependencies": {"@celestia-island/hikari": "^0.55.71"}}),
    )
    assert check(tmp_path) == []


def test_hikari_below_the_family_line_warns(tmp_path):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {
        "@celestia-island/hikari": "^0.40.27"}}))
    findings = check(tmp_path)
    assert [f.level for f in findings] == ["warning"], _names(findings)
    assert "0.55" in findings[0].message


# ── scanning rules ───────────────────────────────────────────────────────────


def test_dependencies_under_skipped_directories_are_ignored(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n', path="node_modules/dep/Cargo.toml")
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n', path="target/debug/Cargo.toml")
    _write(tmp_path / "node_modules/x/package.json",
           json.dumps({"dependencies": {"@celestia-island/hikari": "^*"}}))
    assert check(tmp_path) == []


def test_a_clean_repository_reports_nothing(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.7"\n')
    _write(tmp_path / "packages/webui/package.json",
           json.dumps({"dependencies": {"@celestia-island/hikari": "^0.55.71"}}))
    assert check(tmp_path) == []


# ── CLI contract ─────────────────────────────────────────────────────────────


def test_cli_fails_on_a_violation(tmp_path, capsys):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    assert main([str(tmp_path)]) == 1
    assert "violation" in capsys.readouterr().err


def test_cli_passes_with_warnings_unless_strict(tmp_path, capsys):
    _write(tmp_path / "package.json", json.dumps({"dependencies": {
        "@celestia-island/hikari": "^*"}}))
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--strict"]) == 1
    assert "warning" in capsys.readouterr().out


def test_cli_json_report_shape(tmp_path, capsys):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    assert main([str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["violations"] == 1 and report["warnings"] == 0
    assert report["findings"][0]["subject"] == "kirino @ Cargo.toml"


def test_cli_says_ok_on_a_clean_tree(tmp_path, capsys):
    assert main([str(tmp_path)]) == 0
    assert "ok:" in capsys.readouterr().out


# ── shapes that must NOT be reported ─────────────────────────────────────────


def test_workspace_inheritance_is_not_reported(tmp_path):
    # The floor is declared (and checked) at the workspace root; flagging the
    # inheritance site would report the same requirement twice.
    _cargo(tmp_path, '[workspace.dependencies]\nkirino = "^0.7"\nplana = { git = "x", branch = "master" }\n')
    _cargo(tmp_path, '[dependencies]\nkirino = { workspace = true }\nplana = { workspace = true }\n',
           path="packages/core/Cargo.toml")
    assert check(tmp_path) == []


def test_intra_repository_path_dependencies_are_not_reported(tmp_path):
    _cargo(tmp_path, '[workspace]\nmembers = ["packages/plana", "packages/core"]\n')
    _cargo(tmp_path, '[package]\nname = "plana"\n', path="packages/plana/Cargo.toml")
    _cargo(tmp_path, '[dependencies]\nplana = { path = "../plana" }\n',
           path="packages/core/Cargo.toml")
    assert check(tmp_path) == []


def test_a_cross_repository_path_dependency_is_a_violation(tmp_path):
    repo = tmp_path / "consumer"
    _cargo(repo, '[dependencies]\nplana = { path = "../../plana" }\n')
    findings = check(repo)
    assert [f.level for f in findings] == ["violation"], _names(findings)
    assert "outside this repository" in findings[0].message


def test_non_family_crates_are_left_alone(tmp_path):
    _cargo(tmp_path, '[dependencies]\nserde = "1"\nanyhow = "^1"\nplana_extra = { path = "x" }\n')
    assert check(tmp_path) == []
