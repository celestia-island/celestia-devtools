"""Tests for vcs.branch_sweep (git-sweep)."""

import subprocess
import sys
from pathlib import Path
import pytest
from celestia_devtools.vcs import branch_sweep
from celestia_devtools.vcs.branch_sweep import sweep_repo
import subprocess as sp
from celestia_devtools.vcs.branch_sweep import main as sweep_main

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

def test_alive_upstream_kept(repo: Path, tmp_path: Path):
    _git(repo, "switch", "-c", "feat/live")
    _write(repo, "h.txt", "x\n")
    _commit(repo, "live work")
    _wire_alive_upstream(repo, "feat/live", tmp_path)
    _git(repo, "switch", "master")
    report = sweep_repo(repo)
    assert _verdicts(report)["feat/live"] == "KEEP"
    assert any("upstream alive" in x["reason"] for x in report["verdicts"])

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

# --- for-each-ref record parsing (git-sweep --apply safety) -----------------
def _fake_git(stdout: str, returncode: int = 0, stderr: str = ""):
    """Build a stand-in for branch_sweep._git returning canned for-each-ref output."""

    def fake(repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return fake

def _wire_alive_upstream(repo: Path, branch: str, tmp_path: Path) -> None:
    # Real remote-tracking, not hand-written config: `%(upstream)` only
    # resolves when the remote's fetch refspec maps the branch, so a bare
    # origin plus `push -u` is the faithful hermetic way to simulate an
    # alive upstream (local-path remote, no network involved).
    origin = tmp_path / "origin.git"
    origin.mkdir()
    sp.run(["git", "init", "--bare", str(origin)], check = True, capture_output = True, text = True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", branch)

def test_parses_one_tuple_per_record(monkeypatch):
    """Every record must yield exactly (name, upstream, track).

    Reproduction: for-each-ref appends a newline after EACH record, so the
    old whole-buffer split on "@@\\n" shifted every field after the first
    record: branch one lost its upstream (read as branch-less), the upstream
    name became a phantom branch entry, and branch two's name was swallowed
    into the previous record's track field. NUL-separated fields, one line
    per record, are unambiguous because refnames and track text can contain
    neither a newline nor a NUL.
    """
    raw = "feat/one\x00origin/one\x00[ahead 1]\n" "feat/two\x00origin/two\x00[gone]\n" "feat/三\x00origin/三\x00\n" "solo\x00\x00\n"
    monkeypatch.setattr(branch_sweep, "_git", _fake_git(raw))
    assert branch_sweep._local_branches(Path("/anywhere")) == [("feat/one", "origin/one", "ahead 1"), ("feat/two", "origin/two", "gone"), ("feat/三", "origin/三", ""), ("solo", "", "")]

def test_parses_exotic_branch_names(monkeypatch):
    """Slash, CJK, @-bearing and dotted names must survive record splitting."""
    raw = "feat/slash\x00\x00\n" "feat/中文-两\x00origin/中文-两\x00[gone]\n" "weird@@name\x00origin/x\x00\n" "feat/a@b\x00\x00\n" "dot.name\x00origin/d\x00[ahead 2, behind 1]\n"
    monkeypatch.setattr(branch_sweep, "_git", _fake_git(raw))
    assert branch_sweep._local_branches(Path("/anywhere")) == [("feat/slash", "", ""), ("feat/中文-两", "origin/中文-两", "gone"), ("weird@@name", "origin/x", ""), ("feat/a@b", "", ""), ("dot.name", "origin/d", "ahead 2, behind 1")]

def test_parser_refuses_truncated_output(monkeypatch):
    """No final newline = truncated record: refuse, never guess."""
    monkeypatch.setattr(branch_sweep, "_git", _fake_git("only\x00partial\x00"))

    with pytest.raises(branch_sweep.sweep_parse_error):
        branch_sweep._local_branches(Path("/anywhere"))

def test_empty_output_means_zero_branches(monkeypatch):
    """Empty stdout from a SUCCESSFUL for-each-ref is the only rendering of
    "zero local branches" (every record is newline-terminated, so zero
    records means zero bytes): a repo whose branches were all swept in a
    previous run, or whose only "master" is a tag, must read as an empty
    list -- not as a truncated listing.
    """
    monkeypatch.setattr(branch_sweep, "_git", _fake_git(""))
    assert branch_sweep._local_branches(Path("/anywhere")) == []

def test_zero_branch_repo_is_a_clean_noop(tmp_path: Path, capsys):
    """A repo with no local branches must sweep nothing and exit 0 -- even
    when its only "master" is a TAG that satisfies the base lookup (the
    shape of the second run after a maximal sweep deleted every branch).
    """
    r = tmp_path / "tagbase"
    r.mkdir()
    _git(r, "init", "-b", "master")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "tester")
    _write(r, "base.txt", "base\n")
    _commit(r, "base")
    _git(r, "tag", "master")  # tag satisfies rev-parse --verify master
    _git(r, "checkout", "-q", "--detach")
    _git(r, "branch", "-D", "master")
    rc = sweep_main(["--repo", str(r)])
    out = capsys.readouterr()
    assert rc == 0
    assert "0 swept, 0 kept" in out.out
    assert "ERROR" not in out.err

def test_parser_refuses_wrong_field_count(monkeypatch):
    """A record without exactly two NULs has no trustworthy field alignment."""
    raw = "ok\x00\x00\nname-without-separators\n"
    monkeypatch.setattr(branch_sweep, "_git", _fake_git(raw))

    with pytest.raises(branch_sweep.sweep_parse_error):
        branch_sweep._local_branches(Path("/anywhere"))

def test_parser_refuses_empty_refname(monkeypatch):
    monkeypatch.setattr(branch_sweep, "_git", _fake_git("\x00origin/x\x00\n"))

    with pytest.raises(branch_sweep.sweep_parse_error):
        branch_sweep._local_branches(Path("/anywhere"))

def test_parser_refuses_git_failure(monkeypatch):
    """A failed for-each-ref must not read as 'no branches to sweep'."""
    monkeypatch.setattr(branch_sweep, "_git", _fake_git("", returncode = 128, stderr = "fatal: bad object"))

    with pytest.raises(branch_sweep.sweep_parse_error):
        branch_sweep._local_branches(Path("/anywhere"))

def test_multi_branch_records_all_present(repo: Path, tmp_path: Path):
    """With >=2 branches every real branch must get its own verdict, and no
    phantom branch (an upstream name misread as a branch) may appear.

    Reproduction: the old parser dropped records after the first, so
    feat/merged and feat/diverged vanished from the report entirely while
    phantom entries like "origin/feat/live" showed up as sweepable branches.
    """
    _git(repo, "switch", "-c", "feat/live")
    _write(repo, "live.txt", "payload\n")
    _commit(repo, "live work")
    _wire_alive_upstream(repo, "feat/live", tmp_path)
    _git(repo, "switch", "master")
    _git(repo, "branch", "feat/merged")  # tip == master HEAD: ancestor
    _git(repo, "switch", "-c", "feat/diverged")
    _write(repo, "u.txt", "diverged\n")
    _commit(repo, "diverged work")
    _git(repo, "switch", "master")
    _write(repo, "live.txt", "payload\n")  # squash landed the same content
    _commit(repo, "land the squash")
    report = sweep_repo(repo)
    assert _verdicts(report) == {"feat/live": "KEEP", "feat/merged": "DELETE", "feat/diverged": "KEEP"}

def test_apply_never_deletes_alive_upstream_branch(repo: Path, tmp_path: Path):
    """The wrong-deletion case behind the --apply ban: a branch whose
    upstream is alive AND whose content already landed via squash must be
    KEEP. The old parser corrupted the first record's upstream field, read
    the branch as upstream-less, judged its identical content sweepable and
    `--apply` deleted a branch that still had a living upstream.
    """
    _git(repo, "switch", "-c", "feat/live")
    _write(repo, "live.txt", "payload\n")
    _commit(repo, "live work")
    _wire_alive_upstream(repo, "feat/live", tmp_path)
    _git(repo, "switch", "master")
    _git(repo, "branch", "feat/merged")  # second record shifts the alignment
    _write(repo, "live.txt", "payload\n")
    _commit(repo, "land the squash")
    report = sweep_repo(repo, apply = True, backup_dir = tmp_path / "bk")
    v = _verdicts(report)
    assert v["feat/live"] == "KEEP"
    assert v["feat/merged"] == "DELETE"
    assert report["deleted"] == ["feat/merged"]
    assert report["failed"] == []
    assert "feat/live" in _git(repo, "branch", "--list", "feat/live")

def test_apply_refuses_when_parse_fails(repo: Path, tmp_path: Path, monkeypatch, capsys):
    """A malformed for-each-ref shape must abort the sweep with a non-zero
    exit -- --apply deletes nothing rather than guessing branch names.
    """
    real_git = branch_sweep._git

    def poisoned(repo_path: Path, *args: str):
        if args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 0, "refs/heads/broken-record\n", "")

        return real_git(repo_path, *args)

    monkeypatch.setattr(branch_sweep, "_git", poisoned)
    _git(repo, "branch", "feat/keepme")
    rc = sweep_main(["--repo", str(repo), "--apply", "--backup-dir", str(tmp_path / "bk")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR" in captured.err
    assert "for-each-ref" in captured.err  # the refusal names the parse, not a failed deletion
    assert "FAILED" in captured.out  # the summary must not read as a clean run
    assert "feat/keepme" in _git(repo, "branch", "--list", "feat/keepme")

def test_dry_run_and_apply_verdicts_identical(repo: Path, tmp_path: Path):
    """Dry run and --apply must surface the exact same verdict lines: both
    consume the same parse, so a fixed parser cannot show one branch list
    and delete another.
    """
    _git(repo, "switch", "-c", "feat/live")
    _write(repo, "live.txt", "payload\n")
    _commit(repo, "live work")
    _wire_alive_upstream(repo, "feat/live", tmp_path)
    _git(repo, "switch", "master")
    _git(repo, "branch", "feat/merged")
    _git(repo, "switch", "-c", "feat/squash")
    _write(repo, "s.txt", "payload\n")
    _commit(repo, "squash work")
    _git(repo, "switch", "master")
    _write(repo, "live.txt", "payload\n")
    _commit(repo, "land live squash")
    _write(repo, "s.txt", "payload\n")
    _commit(repo, "land s squash")
    dry = sweep_repo(repo, apply = False)
    applied = sweep_repo(repo, apply = True, backup_dir = tmp_path / "bk")
    assert applied["verdicts"] == dry["verdicts"]
    assert applied["deleted"] == [v["branch"] for v in dry["verdicts"] if v["verdict"] == "DELETE"]
    assert applied["failed"] == []
