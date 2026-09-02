"""Tests for the link-npm-siblings command (npm/link_siblings.py)."""

import json
import os
import sys
from pathlib import Path

import pytest

from celestia_devtools.npm import link_siblings as ls
from celestia_devtools.npm.link_siblings import (
    LinkPlan,
    apply_link,
    collect_declared_family_deps,
    load_state,
    plan_links,
    remove_links,
)
from celestia_devtools.npm.register_patches import (
    END_MARKER,
    NpmPackage,
    START_MARKER,
    main as register_main,
    remove_overrides_block,
)

SIBLING_VER = "0.29.0"


def _argv(monkeypatch, *args: str) -> None:
    """Module mains read sys.argv (no argv parameter) — pin it for argparse."""
    monkeypatch.setattr(sys, "argv", ["prog", *args])


def _make_sibling(root: Path, repo: str, pkg: str, version: str = SIBLING_VER) -> NpmPackage:
    pkg_dir = root / repo / "packages" / pkg
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": f"@celestia-island/{pkg}", "version": version}),
        encoding="utf-8",
    )
    return NpmPackage(
        name=f"@celestia-island/{pkg}",
        path=pkg_dir.resolve(),
        repo=repo,
        version=version,
    )


def _make_target(root: Path, *, root_deps: dict | None = None,
                 webui_deps: dict | None = None) -> Path:
    repo = root / "target"
    (repo / "packages" / "webui").mkdir(parents=True)
    (repo / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/webui'\n")
    (repo / "package.json").write_text(
        json.dumps({"name": "@celestia-island/target", "dependencies": root_deps or {}}),
        encoding="utf-8",
    )
    (repo / "packages" / "webui" / "package.json").write_text(
        json.dumps({"name": "@celestia-island/webui", "dependencies": webui_deps or {}}),
        encoding="utf-8",
    )
    return repo


@pytest.fixture()
def hikari_sibling(tmp_path: Path) -> NpmPackage:
    return _make_sibling(tmp_path, "hikari", "hikari")


class TestCollectDeclaredFamilyDeps:
    def test_root_and_package_level(self, tmp_path: Path):
        repo = _make_target(
            tmp_path,
            root_deps={"@celestia-island/kirino": "^*"},
            webui_deps={"@celestia-island/hikari": "workspace:*"},
        )
        declared = collect_declared_family_deps(repo)
        assert set(declared) == {"@celestia-island/kirino", "@celestia-island/hikari"}
        assert declared["@celestia-island/kirino"] == [repo]
        assert declared["@celestia-island/hikari"] == [repo / "packages" / "webui"]

    def test_ignores_non_family_and_malformed(self, tmp_path: Path):
        repo = _make_target(tmp_path, root_deps={"vue": "^*"})
        (repo / "packages" / "broken" / "package.json").parent.mkdir()
        (repo / "packages" / "broken" / "package.json").write_text("{not json")
        assert collect_declared_family_deps(repo) == {}

    def test_dedupes_repeated_declaration(self, tmp_path: Path):
        repo = _make_target(
            tmp_path,
            root_deps={"@celestia-island/hikari": "^*"},
            webui_deps={"@celestia-island/hikari": "^*"},
        )
        declared = collect_declared_family_deps(repo)
        assert declared["@celestia-island/hikari"] == [repo, repo / "packages" / "webui"]


class TestPlanLinks:
    def test_matches_sibling_and_skips_missing(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(
            tmp_path,
            webui_deps={"@celestia-island/hikari": "^*", "@celestia-island/ghost": "^*"},
        )
        plans, notices = plan_links(repo, [hikari_sibling], collect_declared_family_deps(repo))
        assert len(plans) == 1
        assert plans[0].name == "@celestia-island/hikari"
        assert plans[0].link_path == repo / "packages" / "webui" / "node_modules" / "@celestia-island/hikari"
        assert any("ghost" in n for n in notices)

    def test_package_level_links_own_node_modules_only(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path, webui_deps={"@celestia-island/hikari": "^*"})
        plans, _ = plan_links(repo, [hikari_sibling], collect_declared_family_deps(repo))
        assert [p.link_path for p in plans] == [
            repo / "packages" / "webui" / "node_modules" / "@celestia-island/hikari",
        ]


class TestApplyLink:
    def _plan(self, repo: Path, sibling: NpmPackage) -> LinkPlan:
        return LinkPlan(
            name=sibling.name, sibling=sibling,
            link_path=repo / "packages" / "webui" / "node_modules" / sibling.name,
            consumer="packages/webui",
        )

    def test_creates_link_and_state(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = self._plan(repo, hikari_sibling)
        state = {"links": {}}
        result = apply_link(plan, repo, state)
        assert result.action == "created"
        assert plan.link_path.is_symlink()
        assert Path(os.readlink(plan.link_path)) == hikari_sibling.path
        assert state["links"][str(plan.link_path)]["target"] == str(hikari_sibling.path)
        assert load_state(repo) == {"links": {}}  # save_state is the caller's job

    def test_idempotent_second_run(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = self._plan(repo, hikari_sibling)
        state = {"links": {}}
        apply_link(plan, repo, state)
        result = apply_link(plan, repo, state)
        assert result.action == "ok"

    def test_replaces_foreign_symlink_recording_original(
        self, tmp_path: Path, hikari_sibling: NpmPackage,
    ):
        repo = _make_target(tmp_path)
        plan = self._plan(repo, hikari_sibling)
        plan.link_path.parent.mkdir(parents=True)
        original_dir = tmp_path / "pnpm-store-thing"
        original_dir.mkdir()
        os.symlink(original_dir, plan.link_path)
        state = {"links": {}}
        result = apply_link(plan, repo, state)
        assert result.action == "updated"
        assert Path(os.readlink(plan.link_path)) == hikari_sibling.path
        assert state["links"][str(plan.link_path)]["original"] == str(original_dir)

    def test_real_dir_skipped_unless_forced(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = self._plan(repo, hikari_sibling)
        plan.link_path.parent.mkdir(parents=True)
        plan.link_path.mkdir()
        state = {"links": {}}
        assert apply_link(plan, repo, state).action == "skipped-dir"
        assert plan.link_path.is_dir() and not plan.link_path.is_symlink()
        forced = apply_link(plan, repo, state, force=True)
        assert forced.action == "forced-dir"
        assert plan.link_path.is_symlink()
        assert state["links"][str(plan.link_path)]["was_dir"] is True

    def test_dry_run_touches_nothing(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = self._plan(repo, hikari_sibling)
        result = apply_link(plan, repo, {"links": {}}, dry_run=True)
        assert result.action == "dry-run"
        assert not plan.link_path.exists()


class TestRemoveLinks:
    def test_restores_original_and_clears_state(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = LinkPlan(
            name=hikari_sibling.name, sibling=hikari_sibling,
            link_path=repo / "packages" / "webui" / "node_modules" / hikari_sibling.name,
            consumer="packages/webui",
        )
        state = {"links": {}}
        apply_link(plan, repo, state)
        ls.save_state(repo, state)
        assert remove_links(repo) == 0
        assert not plan.link_path.exists()
        assert not ls._state_path(repo).exists()

    def test_restore_relinks_original_target(self, tmp_path: Path, hikari_sibling: NpmPackage):
        repo = _make_target(tmp_path)
        plan = LinkPlan(
            name=hikari_sibling.name, sibling=hikari_sibling,
            link_path=repo / "packages" / "webui" / "node_modules" / hikari_sibling.name,
            consumer="packages/webui",
        )
        original_dir = tmp_path / "pnpm-orig"
        original_dir.mkdir()
        plan.link_path.parent.mkdir(parents=True)
        os.symlink(original_dir, plan.link_path)
        state = {"links": {}}
        apply_link(plan, repo, state)
        ls.save_state(repo, state)
        remove_links(repo)
        assert plan.link_path.is_symlink()
        assert Path(os.readlink(plan.link_path)) == original_dir

    def test_noop_without_state(self, tmp_path: Path, capsys):
        repo = _make_target(tmp_path)
        assert remove_links(repo) == 0
        assert "nothing to remove" in capsys.readouterr().out


class TestRemoveOverridesBlock:
    @staticmethod
    def _sample() -> str:
        return (
            "packages:\n  - 'packages/webui'\n"
            "overrides:\n  typescript: ~6.0\n\n"
            f"{START_MARKER}\n"
            "# Generated by `celestia-devtools register-npm-patches`.\n"
            "pnpm:\n  overrides:\n"
            '    "@celestia-island/hikari": "link:../hikari/packages/vue"\n'
            f"{END_MARKER}\n"
        )

    def test_strips_block_keeps_rest(self):
        new_text, changed = remove_overrides_block(self._sample())
        assert changed
        assert "packages:" in new_text
        assert "typescript" in new_text
        assert "link:../hikari" not in new_text
        assert START_MARKER not in new_text

    def test_noop_without_markers(self):
        text = "packages:\n  - 'packages/webui'\n"
        new_text, changed = remove_overrides_block(text)
        assert not changed
        assert new_text == text

    def test_survives_duplicated_markers(self):
        text = self._sample() + self._sample()
        new_text, changed = remove_overrides_block(text)
        assert changed
        assert START_MARKER not in new_text
        assert "typescript" in new_text


class TestRegisterNpmPatchesRetired:
    def test_without_remove_fails_with_notice(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _argv(monkeypatch)
        assert register_main() == 1
        captured = capsys.readouterr()
        assert "RETIRED" in captured.err
        assert "link-npm-siblings" in captured.err

    def test_remove_strips_workspace_block(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pnpm-workspace.yaml").write_text(
            TestRemoveOverridesBlock._sample(), encoding="utf-8",
        )
        _argv(monkeypatch, "--remove")
        assert register_main() == 0
        out = capsys.readouterr().out
        assert "removed legacy overrides block" in out
        final = (tmp_path / "pnpm-workspace.yaml").read_text()
        assert "link:../hikari" not in final
        assert "typescript" in final

    def test_remove_without_block_is_noop(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pnpm-workspace.yaml").write_text("packages: []\n")
        _argv(monkeypatch, "--remove")
        assert register_main() == 0
        assert "no legacy overrides block" in capsys.readouterr().out


class TestLinkSiblingsMain:
    def _git_repo(self, path: Path, origin: str) -> None:
        import subprocess

        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", origin], check=True,
        )

    def test_end_to_end_link(self, tmp_path: Path, monkeypatch, capsys):
        scan_dir = tmp_path / "celestia"
        sibling = _make_sibling(scan_dir, "hikari", "hikari")
        self._git_repo(scan_dir / "hikari", "https://github.com/celestia-island/hikari.git")
        repo = _make_target(
            scan_dir, webui_deps={"@celestia-island/hikari": "workspace:*"},
        )
        monkeypatch.chdir(repo)
        _argv(monkeypatch, "--scan-dir", str(scan_dir))
        assert ls.main() == 0
        out = capsys.readouterr().out
        assert "created" in out
        link = repo / "packages" / "webui" / "node_modules" / "@celestia-island/hikari"
        assert link.is_symlink()
        assert Path(os.readlink(link)) == sibling.path
        assert ls._state_path(repo).is_file()

        capsys.readouterr()
        assert ls.main() == 0
        assert "already linked" in capsys.readouterr().out

    def test_status_and_remove_roundtrip(self, tmp_path: Path, monkeypatch, capsys):
        scan_dir = tmp_path / "celestia"
        _make_sibling(scan_dir, "hikari", "hikari")
        self._git_repo(scan_dir / "hikari", "https://github.com/celestia-island/hikari.git")
        repo = _make_target(scan_dir, webui_deps={"@celestia-island/hikari": "^*"})
        monkeypatch.chdir(repo)
        _argv(monkeypatch, "--scan-dir", str(scan_dir))
        ls.main()
        capsys.readouterr()

        _argv(monkeypatch, "--scan-dir", str(scan_dir), "--status")
        assert ls.main() == 0
        assert "->" in capsys.readouterr().out

        _argv(monkeypatch, "--remove")
        assert ls.main() == 0
        link = repo / "packages" / "webui" / "node_modules" / "@celestia-island/hikari"
        assert not link.exists()
        assert not ls._state_path(repo).exists()

    def test_missing_sibling_reports_registry_fallback(self, tmp_path: Path, monkeypatch, capsys):
        scan_dir = tmp_path / "celestia"
        scan_dir.mkdir()
        repo = _make_target(scan_dir, webui_deps={"@celestia-island/hikari": "^*"})
        monkeypatch.chdir(repo)
        _argv(monkeypatch, "--scan-dir", str(scan_dir))
        assert ls.main() == 0
        out = capsys.readouterr().out
        assert "no sibling checkouts provide the declared deps" in out
        assert "@celestia-island/hikari" in out

    def test_requires_pnpm_workspace(self, tmp_path: Path, monkeypatch):
        (tmp_path / "plain").mkdir()
        monkeypatch.chdir(tmp_path / "plain")
        _argv(monkeypatch)
        assert ls.main() == 1

    def test_legacy_refs_warn(self, tmp_path: Path, monkeypatch, capsys):
        scan_dir = tmp_path / "celestia"
        _make_sibling(scan_dir, "hikari", "hikari")
        self._git_repo(scan_dir / "hikari", "https://github.com/celestia-island/hikari.git")
        repo = _make_target(scan_dir, webui_deps={"@celestia-island/hikari": "workspace:*"})
        (repo / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'packages/webui'\n  - '../hikari/packages/vue'\n",
        )
        monkeypatch.chdir(repo)
        _argv(monkeypatch, "--scan-dir", str(scan_dir))
        assert ls.main() == 0
        err = capsys.readouterr().err
        assert "legacy sibling references" in err
