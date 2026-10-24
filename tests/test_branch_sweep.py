"""Tests for vcs.branch_sweep (git-sweep)."""

import subprocess
import sys
from pathlib import Path
import pytest
from celestia_devtools.vcs.branch_sweep import sweep_repo

def _git(repo: Path, *args: str) -> str:
    # Same UTF-8 pin the tool itself uses: git speaks UTF-8 and the CJK
    # branch-name test would otherwise trip the test helper's own
    # locale-codec decode on a cp936 console.
    r = subprocess.run(["git", "-C", str(repo), *args], check = True, capture_output = True, text = True, encoding = "utf-8", errors = "replace")
    return r.stdout.strip()

def _commit(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)

def _write(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding = "utf-8")

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
    report = sweep_repo(repo, apply = False)
    assert _verdicts(report)["feat/squash"] == "DELETE"
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "feat/squash"], capture_output = True, text = True).stdout
    assert "feat/squash" in branches  # dry run touched nothing

def test_apply_deletes_and_backs_up(repo: Path, tmp_path: Path):
    _git(repo, "switch", "-c", "feat/squash")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "work on branch")
    _git(repo, "switch", "master")
    _write(repo, "g.txt", "payload\n")
    _commit(repo, "land the squash")
    bk = tmp_path / "bk"
    report = sweep_repo(repo, apply = True, backup_dir = bk)
    assert report["deleted"] == ["feat/squash"]
    patches = list(bk.glob("*.patch"))
    assert (len(patches) == 1) and ("tip " in patches[0].read_text(encoding = "utf-8"))
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "feat/squash"], capture_output = True, text = True).stdout
    assert "feat/squash" not in branches

def test_main_dry_run_via_cli(repo: Path):
    import subprocess
    r = subprocess.run([sys.executable, "-m", "celestia_devtools.core.cli", "git-sweep", "--repo", str(repo)], capture_output = True, text = True)
    assert r.returncode == 0
    assert "dry run" in r.stdout

def test_cjk_named_file_unique_content_kept(repo: Path):
    """A CJK-named file with unique content must keep its branch.

    Regression: `git diff --name-only` quoted non-ASCII paths as octal
    escapes ("\\346\\226\\207.txt" for 文件.txt); handing that quoted
    spelling back to `git diff -- <path>` matched nothing, so a
    uniquely-changed CJK file read as "identical on base" and the branch
    was swept (false DELETE - real content loss behind a backup patch
    nobody looks at).
    """
    _git(repo, "switch", "-c", "feat/cjk-file")
    _write(repo, "文件.txt", "branch\n")
    _commit(repo, "cjk file work")
    _git(repo, "switch", "master")
    _write(repo, "文件.txt", "master\n")
    _commit(repo, "master moved on")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/cjk-file"] == "KEEP"

def test_cjk_branch_name_decodes(repo: Path):
    """A non-ASCII branch name must survive git output decoding.

    On a cp936 (GBK) Windows console the tool's subprocess reader decoded
    git's UTF-8 output with the locale codec, the reader thread died and
    `stdout` became None - the sweep crashed with an AttributeError.
    """
    _git(repo, "switch", "-c", "feat/中文-分支")
    _write(repo, "m.txt", "1\n")
    _commit(repo, "work")
    _git(repo, "switch", "master")
    _git(repo, "merge", "feat/中文-分支")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/中文-分支"] == "DELETE"

def test_root_skips_repo_without_base(tmp_path: Path, capsys):
    """--root must not turn a base-less checkout into a sweep failure.

    A workspace walk hits stray/temporary git dirs (e.g. a `.pr-verify`
    scratch repo) that carry neither master nor main; those are not
    sweepable, and reporting them as failures made the whole run exit 1.
    """
    import subprocess as sp

    from celestia_devtools.vcs.branch_sweep import main as sweep_main

    def git_init(path: Path, branch: str) -> None:
        path.mkdir()

        for argv in (["init", "-b", branch], ["config", "user.email", "test@example.com"], ["config", "user.name", "tester"]):
            sp.run(["git", "-C", str(path), *argv], check = True, capture_output = True, text = True)

    ok = tmp_path / "ok"
    git_init(ok, "master")
    (ok / "a.txt").write_text("a\n", encoding = "utf-8")
    sp.run(["git", "-C", str(ok), "add", "-A"], check = True, capture_output = True)
    sp.run(["git", "-C", str(ok), "commit", "-m", "base"], check = True, capture_output = True)

    stray = tmp_path / "stray"
    git_init(stray, "trunk")

    assert sweep_main(["--root", str(tmp_path)]) == 0
    assert "no base branch" in capsys.readouterr().err
