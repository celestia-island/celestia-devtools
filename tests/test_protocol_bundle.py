"""Tests for the protocol-bundle vendor command (doc/protocol_bundle.py)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from celestia_devtools.doc import protocol_bundle as pb
from celestia_devtools.doc.protocol_bundle import (
    TYPE_FILES,
    collect_languages,
    generate,
    resolve_docs_root,
)


@pytest.fixture
def fake_docs(tmp_path):
    """A minimal docs.celestia.world checkout with two languages."""
    root = tmp_path / "docs.celestia.world"
    (root / "docs").mkdir(parents=True)
    (root / "justfile").write_text("default:\n\t@echo hi\n", encoding="utf-8")
    langs = {"en", "zh-Hans"}
    for lang in langs:
        meta = root / "docs" / lang / "meta"
        meta.mkdir(parents=True)
        for doc_type, rel in TYPE_FILES.items():
            (meta / rel.split("/")[-1]).write_text(
                f"# {doc_type} ({lang})\n\nbody\n", encoding="utf-8",
            )
    # one language missing the security doc on purpose
    (root / "docs" / "zh-Hans" / "meta" / "security.md").unlink()
    return root


class TestResolveDocsRoot:
    def test_explicit_docs_root(self, fake_docs):
        repo = fake_docs.parent / "repo"
        repo.mkdir()
        got = resolve_docs_root(repo, docs_root=fake_docs)
        assert got == fake_docs.resolve()

    def test_sibling_layout(self, fake_docs, monkeypatch):
        repo = fake_docs.parent / "repo"
        repo.mkdir()
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_ROOT", raising=False)
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_REPO", raising=False)
        # sibling = <parent>/docs.celestia.world — fake_docs sits right there
        got = resolve_docs_root(repo, docs_root=None)
        assert got == fake_docs.resolve()

    def test_env_override_wins_over_sibling(self, fake_docs, monkeypatch, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "justfile").write_text("x\n", encoding="utf-8")
        (other / "docs").mkdir()
        repo = fake_docs.parent / "repo"
        repo.mkdir()
        monkeypatch.setenv("DOCS_CELESTIA_WORLD_ROOT", str(other))
        got = resolve_docs_root(repo, docs_root=None)
        assert got == other.resolve()

    def test_missing_checkout_returns_none(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_ROOT", raising=False)
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_REPO", raising=False)
        # make the fallback clone fail fast instead of hitting the network
        import celestia_devtools.doc.protocol_bundle as pb

        def _fail_clone(*_a, **_k):
            raise OSError("no network in tests")

        monkeypatch.setattr(pb.subprocess, "run", _fail_clone)
        got = resolve_docs_root(repo, docs_root=None)
        assert got is None


class TestGenerate:
    def test_generates_vendor_files_and_manifest(self, fake_docs, tmp_path):
        repo = fake_docs.parent / "repo"
        out = tmp_path / "out" / "protocols"
        count = generate(repo, fake_docs, out_dir=out, verbose=False)

        # 5 types × en (5 docs) + zh-Hans (4 docs, security missing) = 9
        assert count == 9

        vendor = out / "vendor"
        assert (vendor / "license.en.md").is_file()
        assert (vendor / "license.zh-Hans.md").is_file()
        assert (vendor / "security.en.md").is_file()
        assert not (vendor / "security.zh-Hans.md").exists()
        assert (vendor / "license.en.md").read_text(encoding="utf-8").startswith("# license (en)")

        meta_ts = (out / "meta.ts").read_text(encoding="utf-8")
        assert "PROTOCOL_VENDOR_META" in meta_ts
        assert "AUTO-GENERATED" in meta_ts
        # meta.ts must be valid JSON payload after the assignment marker
        payload = meta_ts.split("= ", 1)[1].rsplit(";", 1)[0]
        meta = json.loads(payload)
        assert meta["license"]["en"]["file"] == "license.en.md"
        assert "zh-Hans" not in meta["security"]
        assert "lastRevised" in meta["license"]["en"]

    def test_empty_docs_yields_zero(self, tmp_path):
        root = tmp_path / "docs.celestia.world"
        (root / "docs").mkdir(parents=True)
        (root / "justfile").write_text("x\n", encoding="utf-8")
        count = generate(tmp_path / "repo", root, out_dir=tmp_path / "out")
        assert count == 0

    def test_collect_languages_orders_by_canonical_list(self, fake_docs):
        langs = collect_languages(fake_docs)
        assert langs == ["en", "zh-Hans"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


class TestOwnedCloneSelfRefresh:
    """The target/docs-celestia-shared clone must track the remote head."""

    def _origin_with_docs(self, tmp_path: Path) -> Path:
        origin = tmp_path / "docs.celestia.world.git"
        _git(tmp_path, "init", "--bare", "-q", "-b", "master", str(origin))
        work = tmp_path / "seed"
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                       capture_output=True)
        (work / "justfile").write_text("x\n", encoding="utf-8")
        meta = work / "docs" / "en" / "meta"
        meta.mkdir(parents=True)
        (meta / "license.md").write_text("# license v1\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
        _git(work, "push", "-q", "origin", "master")
        return origin


    def _clone_of(self, origin: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(dest.parent, "clone", "-q", "--depth", "1", str(origin), str(dest))

    def _advance_upstream(self, origin: Path, tmp_path: Path) -> str:
        work = tmp_path / "advance"
        _git(tmp_path, "clone", "-q", str(origin), str(work))
        meta = work / "docs" / "en" / "meta"
        (meta / "license.md").write_text("# license v2 — reflowed\n", encoding="utf-8")
        _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-aqm", "v2")
        _git(work, "push", "-q", "origin", "master")
        return subprocess.run(
            ["git", "-C", str(work), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
        ).stdout.strip()

    def test_existing_clone_refreshes_to_upstream_head(self, tmp_path, monkeypatch):
        origin = self._origin_with_docs(tmp_path)
        clone = tmp_path / "repo" / "target" / "docs-celestia-shared"
        self._clone_of(origin, clone)
        head2 = self._advance_upstream(origin, tmp_path)

        repo = tmp_path / "repo"
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_ROOT", raising=False)
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_REPO", raising=False)
        got = pb.resolve_docs_root(repo, docs_root=None)
        assert got == clone.resolve()
        assert (got / "docs" / "en" / "meta" / "license.md").read_text(
            encoding="utf-8") == "# license v2 — reflowed\n"
        out = tmp_path / "out" / "protocols"
        count = pb.generate(repo, got, out_dir=out)
        assert count == 1  # fake origin carries only license.en.md
        meta_ts = (out / "meta.ts").read_text(encoding="utf-8")
        assert head2[:12] in meta_ts
        assert (out / "vendor" / "license.en.md").read_text(
            encoding="utf-8") == "# license v2 — reflowed\n"

    def test_failed_refresh_keeps_existing_clone(self, tmp_path, monkeypatch, capsys):
        origin = self._origin_with_docs(tmp_path)
        clone = tmp_path / "repo" / "target" / "docs-celestia-shared"
        self._clone_of(origin, clone)
        # Simulate offline: destroy the origin (URL still names the docs repo).
        import shutil
        shutil.rmtree(origin)
        repo = tmp_path / "repo"
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_ROOT", raising=False)
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_REPO", raising=False)
        got = pb.resolve_docs_root(repo, docs_root=None)
        # Graceful: the existing (stale) clone is still used, loudly.
        assert got == clone.resolve()
        captured = capsys.readouterr()
        assert "docs clone refresh fetch failed" in captured.err
        assert "possibly stale" in captured.err

    def test_non_owned_clone_is_never_reset(self, tmp_path, monkeypatch, capsys):
        origin = self._origin_with_docs(tmp_path)
        clone = tmp_path / "repo" / "target" / "docs-celestia-shared"
        self._clone_of(origin, clone)
        # A user checkout parked at the owned-clone path must be left alone.
        _git(clone, "remote", "set-url", "origin", str(tmp_path / "my-work.git"))
        repo = tmp_path / "repo"
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_ROOT", raising=False)
        monkeypatch.delenv("DOCS_CELESTIA_WORLD_REPO", raising=False)
        got = pb.resolve_docs_root(repo, docs_root=None)
        assert got == clone.resolve()
        assert "refusing to refresh non-owned clone" in capsys.readouterr().err
