#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Tests for the domain-leak gate (``celestia_devtools.lint.domain_leak``).

The scanner ships its own 19-assertion self-proof, so these tests pin what that
self-proof cannot own: the bundled baseline, the workspace and baseline
resolution, and the scope/term data contracts. They then exercise one full scan
and one CLI run end to end against a throwaway workspace, because "the self-test
passed" only proves the code paths the self-test itself drives.
"""

import json
from celestia_devtools.lint import domain_leak

class test_selftest:
    """All twenty ported assertions must still hold."""

    def test_selftest_passes_both_halves(self, capsys):
        rc = domain_leak.main(["--selftest"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "7/7 assertions passed" in out
        assert "+21 assertions passed" in out

class test_baseline:
    """The shipped baseline is the single source of truth and stays well formed."""

    def test_bundled_baseline_is_well_formed(self):
        data = json.loads(domain_leak._PACKAGE_BASELINE.read_text(encoding = "utf-8"))
        entries = data["entries"]
        assert entries, "the shipped baseline must carry the tracked debt"

        for entry in entries:
            assert set(entry) >= {"repo", "path", "terms"}, entry
            assert entry["repo"] in domain_leak.SCOPE, entry
            assert entry["terms"], entry

    def test_resolution_order(self, tmp_path, monkeypatch):
        bundled = domain_leak.resolve_baseline()
        assert bundled == domain_leak._PACKAGE_BASELINE
        custom = tmp_path / "baseline.json"
        custom.write_text('{"entries": []}', encoding = "utf-8")
        assert domain_leak.resolve_baseline(str(custom)) == custom
        assert domain_leak.resolve_baseline(str(bundled)) == bundled
        monkeypatch.setenv(domain_leak.BASELINE_PATH_ENV, str(custom))
        assert domain_leak.resolve_baseline() == custom

class test_workspace_resolution:
    """The workspace root is found by markers, not by a hardcoded depth."""

    def test_marker_walk_from_a_worktree_style_checkout(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        (workspace / "_tools").mkdir(parents = True)
        (workspace / "_tools" / "claim.py").write_text("", encoding = "utf-8")
        (workspace / "_ledger").mkdir()
        nested = workspace / "_worktree" / "celestia-devtools" / "a-slug" / "src" / "celestia_devtools" / "lint"
        nested.mkdir(parents = True)
        monkeypatch.setattr(domain_leak, "_REPO_ROOT", nested)
        assert domain_leak.resolve_workspace() == workspace

    def test_explicit_and_environment_win(self, tmp_path, monkeypatch):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        assert domain_leak.resolve_workspace(str(one)) == one
        monkeypatch.setenv(domain_leak.WORKSPACE_PATH_ENV, str(two))
        assert domain_leak.resolve_workspace() == two
        assert domain_leak.resolve_workspace(str(one)) == one

class test_scope_and_terms:
    """Reach and word list are data contracts, so pin their shape."""

    def test_scope_covers_the_host_core_and_spares_the_domain_zones(self):
        expected = {"entelecheia", "plana", "kirino", "hikari", "evernight", "malkuth", "aoba", "shun"}
        assert set(domain_leak.SCOPE) == expected

        for repo in domain_leak.SCOPE:
            assert domain_leak.SCOPE[repo][0] == ("",), repo

        assert "packages/domain_agents/" in domain_leak.SCOPE["entelecheia"][1]
        assert "res/prompts/domain_agents/" in domain_leak.SCOPE["entelecheia"][1]
        assert "packages/domain_skills/" in domain_leak.SCOPE["plana"][1]
        assert domain_leak.SCOPE["kirino"][1] == ()

    def test_terms_cover_cjk_english_and_the_product_repo_name(self):
        cjk = ("氢", "氫", "电极", "電極", "电解槽", "電解槽", "电堆", "電堆", "水素", "수소")

        for term in cjk:
            assert term in domain_leak.TERMS_CJK, term

        for term in ("hydrogen", "electrode", "comsol", "electrolyzer", "easy-hydro"):
            assert term in domain_leak.TERMS_ASCII_CI, term

        assert "hydro" not in domain_leak.TERMS_ASCII_CI, "bare hydro would hit hydrology"

    def test_find_terms_is_case_insensitive_and_reads_the_path(self):
        assert domain_leak.find_terms("Hydrogen plant") == ["hydrogen"]
        assert "hydrogen" in domain_leak.find_terms("fn main() {}", "src/hydrogen_flow.rs")
        assert domain_leak.find_terms("fn main() -> u8 { 42 }") == []

class test_full_scan:
    """One scan against a throwaway workspace: new hit, baselined hit, then stale."""

    def _workspace(self, tmp_path):
        host = tmp_path / "ws" / "kirino" / "src"
        host.mkdir(parents = True)
        (host / "generic.rs").write_text("fn generic() -> u8 { 42 }\n", encoding = "utf-8")
        (host / "leak.rs").write_text('const S: &str = "hydrogen";\n', encoding = "utf-8")
        baseline = tmp_path / "baseline.json"
        baseline.write_text('{"entries": []}', encoding = "utf-8")
        return tmp_path / "ws", baseline

    def test_new_hit_fails_then_baseline_covers_it_then_prune_sees_it(self, tmp_path, monkeypatch):
        workspace, baseline = self._workspace(tmp_path)
        monkeypatch.setattr(domain_leak, "WORKSPACE", workspace)
        monkeypatch.setattr(domain_leak, "BASELINE", baseline)
        # The test declares its throwaway workspace, so it vouches for the root the way
        # a marker walk would; without this the full-scan fail-closed guard would (rightly)
        # refuse in a bare CI checkout that carries no workspace markers.
        monkeypatch.setattr(domain_leak, "WORKSPACE_SOURCE", domain_leak.WORKSPACE_SOURCE_MARKER)
        rc, new_hits, known_hits, stale = domain_leak.run_scan(quiet = True)
        assert rc == 1
        assert [hit["path"] for hit in new_hits] == ["src/leak.rs"]
        assert (known_hits == []) and (stale == [])
        entry = {"repo": "kirino", "path": "src/leak.rs", "terms": ["hydrogen"], "note": "test"}
        baseline.write_text(json.dumps({"entries": [entry]}), encoding = "utf-8")
        rc, new_hits, known_hits, stale = domain_leak.run_scan(quiet = True)
        assert (rc == 0) and (new_hits == [])
        assert [hit["path"] for hit in known_hits] == ["src/leak.rs"]
        assert stale == []
        (workspace / "kirino" / "src" / "leak.rs").unlink()
        rc, new_hits, known_hits, stale = domain_leak.run_scan(quiet = True)
        assert (rc == 0) and (known_hits == [])
        assert [entry["path"] for entry in stale] == ["src/leak.rs"]

class test_cli:
    """The dispatcher spelling reaches the same code path and prints the contract."""

    def test_json_mode_reports_the_hit(self, tmp_path, monkeypatch, capsys):
        workspace = tmp_path / "ws"
        host = workspace / "kirino" / "src"
        host.mkdir(parents = True)
        (host / "leak.rs").write_text('const S: &str = "electrolyzer";\n', encoding = "utf-8")
        baseline = tmp_path / "baseline.json"
        baseline.write_text('{"entries": []}', encoding = "utf-8")
        rc = domain_leak.main(["--json", "--workspace", str(workspace), "--baseline", str(baseline)])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["rc"] == 1
        assert payload["new"][0]["path"] == "src/leak.rs"
        assert payload["new"][0]["terms"] == ["electrolyzer"]

    def test_quiet_mode_keeps_the_exit_code(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        (workspace / "kirino").mkdir(parents = True)
        baseline = tmp_path / "baseline.json"
        baseline.write_text('{"entries": []}', encoding = "utf-8")
        rc = domain_leak.main(["--quiet", "--workspace", str(workspace), "--baseline", str(baseline)])
        assert rc == 0
