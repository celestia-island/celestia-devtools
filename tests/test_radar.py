"""Tests for the dependency radar (offline: parsers, comparators, renderer)."""

import pytest
from celestia_devtools.radar import radar

class test_version_compare:
    def test_equal_is_not_newer(self):
        assert radar.is_newer("1.2.3", "1.2.3") is False

    def test_patch_minor_major_newer(self):
        assert radar.is_newer("1.2.4", "1.2.3")
        assert radar.is_newer("1.3.0", "1.2.9")
        assert radar.is_newer("2.0.0", "1.99.99")

    def test_spec_prefixes_are_ignored(self):
        assert radar.is_newer("1.9.0", "^1.2")
        assert radar.is_newer("2.1", "~2.0")
        assert radar.is_newer("v7", "v4")

    def test_two_component_and_garbage(self):
        assert radar.is_newer("15.10", "15.9")
        assert radar.is_newer("1.0.0", "garbage")

    def test_missing_parts_count_as_zero(self):
        assert radar.is_newer("1.2.1", "1.2")
        assert radar.is_newer("1.3", "1.2.9")

class test_parse_action_refs:
    def test_third_party_actions_are_kept(self):
        text = "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        assert radar.parse_action_refs(text) == [("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1")]

    def test_local_and_reusable_workflow_refs_are_excluded(self):
        text = "uses: ./local/action@main\n" "uses: celestia-island/celestia-devtools/.github/workflows/ci.yml@abc\n"
        assert radar.parse_action_refs(text) == []

    def test_subdirectory_actions_are_kept(self):
        text = "uses: github/codeql-action/init@v3\n"
        assert radar.parse_action_refs(text) == [("github/codeql-action/init", "v3")]

class test_parse_cargo_manifest:
    def test_workspace_and_own_sections(self):
        text = '[workspace.dependencies]\nanyhow = "1"\n' 'game-assets = { version = "^0.43", path = "packages/tools/game-assets" }\n' '[dependencies]\nserde = "1.0"\n'
        assert radar.parse_cargo_manifest(text) == {"anyhow": "1", "game-assets": "^0.43", "serde": "1.0"}

    def test_multiline_inline_table_keeps_first_line_version(self):
        text = '[workspace.dependencies]\n' 'vendored = { path = "../vendored" }\n' 'hikari = { git = "https://github.com/celestia-island/hikari" }\n' 'malkuth = { version = "0.1", features = ["a"],\n' '            optional = true }\n' '[dev-dependencies]\n' 'insta = "1"\n'
        assert radar.parse_cargo_manifest(text) == {"malkuth": "0.1"}

    def test_dev_dependencies_are_out_of_scope(self):
        text = '[dev-dependencies]\ninsta = "1"\n'
        assert radar.parse_cargo_manifest(text) == {}

class test_parse_package_json:
    def test_dependencies_and_dev_dependencies(self):
        text = '{"dependencies": {"vue": "^3.5.0"}, "devDependencies": {"vite": "^8", "linked": "workspace:*"}}'
        assert radar.parse_package_json(text) == {"vue": "^3.5.0", "vite": "^8"}

    def test_workspace_protocols_are_skipped(self):
        text = '{"dependencies": {"a": "file:../a", "b": "github:o/r", "c": "1.2.3"}}'
        assert radar.parse_package_json(text) == {"c": "1.2.3"}

    def test_missing_sections_are_empty(self):
        assert radar.parse_package_json("{}") == {}

class test_render_report:
    def test_drift_row_and_header(self):
        targets = [{"repo": "r/x"}]
        rows = {"r/x": [radar.row("cargo", "anyhow", "1", "1.1", "drift")]}
        report = radar.render_report(targets, rows, "2026-11-01")
        assert "| cargo | anyhow | 1 | 1.1 | drift |" in report
        assert "1 repos watched · 1 rows." in report
        assert "## r/x" in report

    def test_quiet_repo_renders_placeholder(self):
        report = radar.render_report([{"repo": "r/y"}], {"r/y": []}, "2026-11-01")
        assert ("## r/y" in report) and ("_no drift, no notes._" in report)

class test_load_targets:
    def test_valid_manifest(self, tmp_path):
        path = tmp_path / "targets.yml"
        path.write_text("repos:\n  - repo: a/b\n    actions: true\n", encoding = "utf-8")
        assert radar.load_targets(path) == [{"repo": "a/b", "actions": True}]

    def test_invalid_manifests_exit_usage(self, tmp_path):
        for content in ("repos: []\n", "other: 1\n", "repos:\n  - name: a/b\n"):
            path = tmp_path / "targets.yml"
            path.write_text(content, encoding = "utf-8")

            with pytest.raises(SystemExit) as exc:
                radar.load_targets(path)

            assert exc.value.code == 2

class test_self_test:
    def test_offline_fixtures_pass(self):
        assert radar.self_test() == 0
