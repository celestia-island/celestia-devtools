"""Tests for the family dependency-identity check (repo/family_versions.py)."""

import json

from celestia_devtools.repo.family_versions import admits, check, main


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cargo(root, body, path="Cargo.toml"):
    _write(root / path, body)


def _levels(findings):
    return [f.level for f in findings]


# ── requirement semantics: the judgement is "does it admit 0.7?" ─────────────


def test_admits_reads_ranges_not_floors():
    # A floor reader calls all three of the first group "0.6" and reports a
    # violation for `>=0.6, <1.0`, which does admit 0.7.0 — the false positive
    # the adversarial round caught on a real repository (`^0.*`).
    assert admits("^0.6", (0, 7, 0)) is False
    assert admits("^0.6.5", (0, 7, 0)) is False
    assert admits("=0.6.1", (0, 7, 0)) is False
    assert admits(">=0.6, <1.0", (0, 7, 0)) is True
    assert admits("^0.*", (0, 7, 0)) is True
    assert admits("^0", (0, 7, 0)) is True
    assert admits("^*", (0, 7, 0)) is True
    assert admits("^0.7", (0, 7, 0)) is True
    assert admits("~0.7.0", (0, 7, 0)) is True
    assert admits("^0.8", (0, 7, 0)) is False


# ── kirino: one major, or it is a different primitive ────────────────────────


def test_kirino_below_the_family_floor_is_a_violation(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "cannot resolve to the family's 0.7 line" in findings[0].message


def test_a_hyphenated_family_crate_is_checked_too(tmp_path):
    # `kirino-session` is where the real split lives: evernight and
    # erp.celestia.world declare it at 0.6 while the rest of the family is 0.7.
    _cargo(tmp_path, '[dependencies]\nkirino-session = "0.6"\n')
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert findings[0].subject.startswith("kirino-session @")


def test_kirino_at_the_family_floor_passes(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = { version = "^0.7", features = ["auth-jwt"] }\n')
    assert check(tmp_path) == []


def test_kirino_from_git_master_warns_but_does_not_fail(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nkirino = { git = "https://github.com/celestia-island/kirino.git",'
        ' branch = "master" }\n',
    )
    assert _levels(check(tmp_path)) == ["warning"]


def test_a_range_that_admits_the_family_version_passes(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = ">=0.6, <1.0"\n')
    assert check(tmp_path) == []


def test_workspace_and_dev_and_target_sections_are_all_checked(tmp_path):
    _cargo(tmp_path, '[workspace.dependencies]\nkirino = "^0.6"\n')
    assert _levels(check(tmp_path)) == ["violation"]
    _cargo(tmp_path, '[dev-dependencies]\nkirino = "^0.6"\n', path="a/Cargo.toml")
    assert _levels(check(tmp_path)) == ["violation", "violation"]
    _cargo(tmp_path, "[target.'cfg(unix)'.dependencies]\nkirino = \"^0.6\"\n", path="b/Cargo.toml")
    assert _levels(check(tmp_path)) == ["violation"] * 3


# ── plana: master, not a station ─────────────────────────────────────────────

_PLANA_OK = (
    '[dependencies]\nplana = { git = "https://github.com/celestia-island/plana.git",'
    ' branch = "master" }\n'
)


def test_plana_git_master_passes(tmp_path):
    _cargo(tmp_path, _PLANA_OK)
    assert check(tmp_path) == []


def test_plana_subcrates_are_checked(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nplana-jsonrpc = { git ='
        ' "https://github.com/celestia-island/plana.git", rev = "b4e7de35" }\n',
    )
    assert _levels(check(tmp_path)) == ["violation"]


def test_plana_rev_pin_is_allowed_only_for_frozen_repositories(tmp_path):
    pinned = (
        '[dependencies]\nplana = { package = "plana", git ='
        ' "https://github.com/celestia-island/plana.git", rev = "abc" }\n'
    )
    _cargo(tmp_path, pinned)
    assert _levels(check(tmp_path)) == ["violation"]

    frozen = tmp_path / "scriptum"
    _cargo(frozen, pinned)
    assert check(frozen) == []


def test_plana_from_the_registry_is_a_violation(tmp_path):
    _cargo(tmp_path, '[dependencies]\nplana = "0.2"\n')
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "crates.io" in findings[0].message


def test_plana_from_a_foreign_git_source_is_a_violation(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nplana = { git = "https://gitlab.example/evil/plana.git",'
        ' branch = "master" }\n',
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "is not the family's" in findings[0].message


def test_plana_on_a_feature_branch_is_a_violation(tmp_path):
    _cargo(
        tmp_path,
        '[dependencies]\nplana = { git = "https://github.com/celestia-island/plana.git",'
        ' branch = "feat/x" }\n',
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "feat/x" in findings[0].message


# ── hikari: unbounded ranges are the audit finding ───────────────────────────


def _webui(root, spec):
    _write(
        root / "packages/webui/package.json",
        json.dumps({"dependencies": {"@celestia-island/hikari": spec}}),
    )


def test_hikari_unbounded_range_warns(tmp_path):
    _webui(tmp_path, "^*")
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "any future major" in findings[0].message


def test_hikari_bounded_range_passes(tmp_path):
    _webui(tmp_path, "^0.55.71")
    assert check(tmp_path) == []


def test_hikari_below_the_family_line_warns(tmp_path):
    _webui(tmp_path, "^0.40.27")
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "cannot resolve to the family's 0.55" in findings[0].message


def test_a_wildcard_zero_range_is_not_below_the_line(tmp_path):
    # `^0.*` is what the workspace rules prescribe for a 0.x family package, and
    # node-semver reads it as >=0.0.0 <1.0.0 — it admits 0.55.
    _webui(tmp_path, "^0.*")
    assert check(tmp_path) == []


def test_a_sibling_file_dependency_is_a_violation(tmp_path):
    _webui(tmp_path, "file:../hikari")
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "retired" in findings[0].message


def test_workspace_protocols_are_left_alone(tmp_path):
    _webui(tmp_path, "workspace:*")
    assert check(tmp_path) == []


def test_an_npm_alias_is_judged_by_its_range(tmp_path):
    _write(
        tmp_path / "package.json",
        json.dumps({"dependencies": {"hk": "npm:@celestia-island/hikari@^0.40.1"}}),
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "0.55" in findings[0].message


def test_overrides_are_reported_too(tmp_path):
    _write(
        tmp_path / "package.json",
        json.dumps({"pnpm": {"overrides": {"@celestia-island/hikari": "^*"}}}),
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "pnpm.overrides" in findings[0].subject


# ── path dependencies and scanning rules ─────────────────────────────────────


def test_workspace_inheritance_is_not_reported(tmp_path):
    _cargo(tmp_path, '[workspace.dependencies]\nkirino = "^0.7"\n')
    _cargo(tmp_path, '[dependencies]\nkirino = { workspace = true }\n',
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
    assert _levels(findings) == ["violation"], findings
    assert "outside this repository" in findings[0].message


def test_dependencies_under_skipped_directories_are_ignored(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n', path="node_modules/dep/Cargo.toml")
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n', path="target/debug/Cargo.toml")
    _webui(tmp_path, "^0.55.71")
    _write(tmp_path / "node_modules/x/package.json",
           json.dumps({"dependencies": {"@celestia-island/hikari": "^*"}}))
    assert check(tmp_path) == []


def test_a_repository_living_under_a_skipped_name_is_still_scanned(tmp_path):
    # The skipped names belong to directories INSIDE the repository: matching
    # them against the absolute path silently skipped a checkout that merely
    # lived under `…/target/…`.
    repo = tmp_path / "target" / "vendor" / "checkout"
    _cargo(repo, '[dependencies]\nkirino = "^0.6"\n')
    assert _levels(check(repo)) == ["violation"]


def test_non_family_crates_are_left_alone(tmp_path):
    _cargo(tmp_path, '[dependencies]\nserde = "1"\nanyhow = "^1"\nserde_json = { workspace = true }\n')
    assert check(tmp_path) == []


def test_a_clean_repository_reports_nothing(tmp_path):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.7"\n')
    _webui(tmp_path, "^0.55.71")
    assert check(tmp_path) == []


# ── CLI contract ─────────────────────────────────────────────────────────────


def test_cli_fails_on_a_violation(tmp_path, capsys):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    assert main([str(tmp_path)]) == 1
    assert "violation" in capsys.readouterr().err


def test_cli_passes_with_warnings_unless_strict(tmp_path, capsys):
    _webui(tmp_path, "^*")
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--strict"]) == 1
    assert "warning" in capsys.readouterr().out


def test_cli_json_report_shape(tmp_path, capsys):
    _cargo(tmp_path, '[dependencies]\nkirino = "^0.6"\n')
    assert main([str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["violations"] == 1 and report["warnings"] == 0
    assert report["findings"][0]["subject"].startswith("kirino @ Cargo.toml")


def test_cli_says_ok_on_a_clean_tree(tmp_path, capsys):
    assert main([str(tmp_path)]) == 0
    assert "ok:" in capsys.readouterr().out


# ── surfaces the mutation round found unpinned ───────────────────────────────


def test_an_alias_does_not_hide_a_direct_declaration(tmp_path):
    # Returning the first match hid the `file:` violation behind an alias of the
    # same package in the same table.
    _write(
        tmp_path / "package.json",
        json.dumps({"dependencies": {
            "hk": "npm:@celestia-island/hikari@^0.55.71",
            "@celestia-island/hikari": "file:../hikari",
        }}),
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["violation"], findings
    assert "retired" in findings[0].message


def test_cargo_replace_section_is_flat(tmp_path):
    # `[replace]` is keyed by "name:version", not by source like `[patch]`;
    # walking it as patch-shaped made the section dead code.
    _cargo(tmp_path, '[replace]\n"kirino:0.6.0" = { version = "^0.6" }\n')
    assert _levels(check(tmp_path)) == ["violation"]


def test_cargo_patch_section_is_checked(tmp_path):
    _cargo(
        tmp_path,
        '[patch.crates-io]\nkirino = { git = "https://github.com/celestia-island/kirino.git",'
        ' branch = "master" }\n',
    )
    assert _levels(check(tmp_path)) == ["warning"]


def test_plain_overrides_are_reported(tmp_path):
    _write(
        tmp_path / "package.json",
        json.dumps({"overrides": {"@celestia-island/hikari": "^0.40.1"}}),
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "(overrides)" in findings[0].subject


def test_resolutions_are_reported(tmp_path):
    _write(
        tmp_path / "package.json",
        json.dumps({"resolutions": {"@celestia-island/hikari": "^*"}}),
    )
    findings = check(tmp_path)
    assert _levels(findings) == ["warning"], findings
    assert "(resolutions)" in findings[0].subject


def test_build_dependencies_are_checked(tmp_path):
    _cargo(tmp_path, '[build-dependencies]\nkirino = "^0.6"\n')
    assert _levels(check(tmp_path)) == ["violation"]


def test_peer_and_optional_dependencies_are_checked(tmp_path):
    _write(tmp_path / "a/package.json",
           json.dumps({"peerDependencies": {"@celestia-island/hikari": "^0.40.1"}}))
    _write(tmp_path / "b/package.json",
           json.dumps({"optionalDependencies": {"@celestia-island/hikari": "^0.40.1"}}))
    findings = check(tmp_path)
    assert _levels(findings) == ["warning", "warning"], findings
    assert any("(peerDependencies)" in f.subject for f in findings)
    assert any("(optionalDependencies)" in f.subject for f in findings)


def test_catalog_and_portal_protocols_are_left_alone(tmp_path):
    _write(tmp_path / "a/package.json",
           json.dumps({"dependencies": {"@celestia-island/hikari": "catalog:"}}))
    _write(tmp_path / "b/package.json",
           json.dumps({"dependencies": {"@celestia-island/hikari": "portal:../hikari"}}))
    assert check(tmp_path) == []
