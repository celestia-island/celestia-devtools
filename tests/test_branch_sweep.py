"""Tests for vcs.branch_sweep (git-sweep)."""

import subprocess
import sys
from pathlib import Path

import pytest

from celestia_devtools.vcs.branch_sweep import sweep_repo


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def _write(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "master")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "tester")
    _write(r, "base.txt", "base\n")
    _commit(r, "base")
    return r


def _verdicts(report: dict) -> dict:
    return {v["branch"]: v["verdict"] for v in report["verdicts"]}


def test_ancestor_branch_swept(repo: Path):
    _git(repo, "switch", "-c", "feat/merged")
    _write(repo, "f.txt", "1\n")
    _commit(repo, "feat work")
    _git(repo, "switch", "master")
    _git(repo, "merge", "feat/merged")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/merged"] == "DELETE"


def test_squash_merged_identical_content_swept(repo: Path):
    _git(repo, "switch", "-c", "feat/squash")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "work on branch")
    _git(repo, "switch", "master")
    _write(repo, "g.txt", "payload\n")  # squash landed the same content
    _commit(repo, "land the squash")
    report = sweep_repo(repo)
    v = _verdicts(report)["feat/squash"]
    assert v == "DELETE"
    assert any("identical" in x["reason"] for x in report["verdicts"])


def test_unique_content_kept(repo: Path):
    _git(repo, "switch", "-c", "feat/diverged")
    _write(repo, "g.txt", "branch\n")
    _commit(repo, "branch work")
    _git(repo, "switch", "master")
    _write(repo, "g.txt", "master\n")
    _commit(repo, "master moved on")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/diverged"] == "KEEP"


def test_alive_upstream_kept(repo: Path):
    _git(repo, "switch", "-c", "feat/live")
    _write(repo, "h.txt", "x\n")
    _commit(repo, "live work")
    sha = _git(repo, "rev-parse", "feat/live")
    _git(repo, "update-ref", "refs/remotes/origin/feat/live", sha)
    # no real remote exists: wire the tracking config directly
    _git(repo, "config", "branch.feat/live.remote", "origin")
    _git(repo, "config", "branch.feat/live.merge", "refs/heads/feat/live")
    _git(repo, "switch", "master")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/live"] == "KEEP"


def test_worktree_checked_out_kept(repo: Path, tmp_path: Path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "feat/wt", str(wt))
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/wt"] == "KEEP"


def test_dry_run_default_keeps_branch(repo: Path):
    _git(repo, "switch", "-c", "feat/squash")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "work on branch")
    _git(repo, "switch", "master")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "land the squash")
    report = sweep_repo(repo, apply=False)
    assert _verdicts(report)["feat/squash"] == "DELETE"
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "feat/squash"],
                              capture_output=True, text=True).stdout
    assert "feat/squash" in branches  # dry run touched nothing


def test_apply_deletes_and_backs_up(repo: Path, tmp_path: Path):
    _git(repo, "switch", "-c", "feat/squash")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "work on branch")
    _git(repo, "switch", "master")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "land the squash")
    bk = tmp_path / "bk"
    report = sweep_repo(repo, apply=True, backup_dir=bk)
    assert report["deleted"] == ["feat/squash"]
    patches = list(bk.glob("*.patch"))
    assert len(patches) == 1 and "tip " in patches[0].read_text(encoding="utf-8")
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "feat/squash"],
                              capture_output=True, text=True).stdout
    assert "feat/squash" not in branches


def test_main_dry_run_via_cli(repo: Path):
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "celestia_devtools.core.cli", "git-sweep", "--repo", str(repo)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "dry run" in r.stdout

