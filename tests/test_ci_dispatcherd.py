"""Tests for the ci-dispatcherd spill merge commit identity.

The ws-spill path creates a synthetic merge commit in the gitcache bare repo.
That repo (and the service env) carries no git identity, so commit-tree must
have one passed explicitly — otherwise it exits 128 ("Author identity unknown")
and every dev-quota spill silently falls back to the build lane (#132 area).
"""

import importlib.util
import os
import subprocess
import sys

import pytest

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ci_dispatcherd.py")
spec = importlib.util.spec_from_file_location("ci_dispatcherd", TOOL)
dsp = importlib.util.module_from_spec(spec)
sys.modules["ci_dispatcherd"] = dsp
spec.loader.exec_module(dsp)

FETCH_TOKEN = "github-pat-1111111111111111111111111"
CNB_TOKEN = "cnb-token-2222222222222222222222222"


def _called_process_error_with_cred_urls():
    """Mimic the real production failure argv shape (tokens inline in URLs)."""
    return subprocess.CalledProcessError(128, [
        "git", "-C", "/var/lib/ci-dispatcher/git/shittim-chest.git", "fetch", "-q",
        f"https://langyo:{FETCH_TOKEN}@github.com/celestia-island/shittim-chest.git",
        "+refs/heads/master:refs/heads/master", "a" * 40,
    ])


def test_redact_masks_url_credentials():
    out = dsp.redact(str(_called_process_error_with_cred_urls()))
    assert FETCH_TOKEN not in out
    assert "https://langyo:***@github.com/celestia-island/shittim-chest.git" in out
    assert "exit status 128" in out


def test_redact_masks_bare_secret_values(monkeypatch):
    monkeypatch.setattr(dsp, "GH", "gh-bearer-333")
    monkeypatch.setattr(dsp, "FETCH", FETCH_TOKEN)
    monkeypatch.setattr(dsp, "CNB", CNB_TOKEN)
    monkeypatch.setattr(dsp, "CNB_WS", "")
    out = dsp.redact(f"bearer gh-bearer-333 blew up near {FETCH_TOKEN} and {CNB_TOKEN}")
    assert "gh-bearer-333" not in out and FETCH_TOKEN not in out and CNB_TOKEN not in out
    assert out.count("***") == 3


def test_spill_failure_log_path_redacts_token(monkeypatch):
    """The ws-spill fallback log line must never carry the fetch/push tokens."""
    monkeypatch.setattr(dsp, "CNB_WS", "ws-present")
    err = _called_process_error_with_cred_urls()
    monkeypatch.setattr(dsp, "ensure_sha_on_mirror",
                        lambda repo, sha: (_ for _ in ()).throw(err))
    seen = {}

    def fake_cnb_api(path, data=None):
        seen["path"] = path
        return {"sn": "sn-1"}

    monkeypatch.setattr(dsp, "cnb_api", fake_cnb_api)
    captured = []
    monkeypatch.setattr(dsp, "log", captured.append)
    state = {}
    dsp.spill({"repo": "shittim-chest", "run_id": 4242, "sha": "b" * 40}, state)
    assert state["4242"]["mode"] == "build"  # fell back to the build lane
    joined = "\n".join(captured)
    assert FETCH_TOKEN not in joined
    assert "langyo:***@github.com" in joined


@pytest.fixture
def no_git_identity(monkeypatch, tmp_path):
    """Strip every identity source: env vars, global/system config, HOME."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _bare_repo_with_two_parents(path):
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    tree = subprocess.run(["git", "-C", str(path), "mktree"], input="",
                          text=True, capture_output=True, check=True).stdout.strip()
    ident = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
    parents = []
    for msg in ("parent-one", "parent-two"):
        sha = subprocess.run(["git", "-C", str(path), *ident, "commit-tree", tree],
                             input=msg + "\n", text=True, capture_output=True,
                             check=True).stdout.strip()
        parents.append(sha)
    return tree, parents[0], parents[1]


def test_spill_commit_identity_constant_is_declared():
    assert "user.name=ci-dispatcher" in dsp.SPILL_COMMIT_IDENTITY
    assert any(v.startswith("user.email=") for v in dsp.SPILL_COMMIT_IDENTITY)


def test_spill_merge_commit_works_without_ambient_identity(tmp_path, no_git_identity):
    repo = str(tmp_path / "fixture.git")
    tree, p1, p2 = _bare_repo_with_two_parents(repo)
    msha = dsp.spill_merge_commit(repo, tree, p1, p2)
    assert len(msha) == 40
    out = subprocess.run(
        ["git", "-C", repo, "show", "-s", "--format=%an%n%ae%n%P", msha],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()
    assert out[0] == "ci-dispatcher"
    assert out[1].endswith("@users.noreply.github.com")
    # parent order must stay (master-side parent first, spilled sha second)
    assert out[2].split() == [p1, p2]


def _fixture_repo_with_conflicting_branches(path):
    """bare repo: master modifies f.txt; 'clean' adds g.txt; 'pr' conflicts on f.txt."""
    def git(*args, input=None, ident=False):
        env = dict(os.environ)
        if ident:
            env.update(GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["git", "-C", str(path), *args], input=input, text=True,
                              capture_output=True, check=True, env=env).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    def blob(content):
        return git("hash-object", "-w", "--stdin", input=content)
    def tree(*entries):
        return git("mktree", input="".join(f"100644 blob {s}\t{n}\n" for s, n in entries))
    def commit(tree_sha, msg, parent=None):
        args = ["commit-tree", tree_sha] + (["-p", parent] if parent else [])
        return git(*args, input=msg + "\n", ident=True)
    f_base, f_master, f_pr, g_new = blob("one\n"), blob("two-master\n"), blob("two-pr\n"), blob("other\n")
    base = commit(tree((f_base, "f.txt")), "base")
    master = commit(tree((f_master, "f.txt")), "master", base)
    pr = commit(tree((f_pr, "f.txt")), "pr", base)
    clean = commit(tree((f_base, "f.txt"), (g_new, "g.txt")), "clean", base)
    git("update-ref", "refs/heads/master", master)
    return str(path), master, pr, clean


def test_spill_merge_tree_clean_merge(tmp_path, no_git_identity):
    repo, master, _pr, clean = _fixture_repo_with_conflicting_branches(tmp_path / "fx.git")
    tree_sha = dsp.spill_merge_tree(repo, master, clean)
    assert len(tree_sha) == 40


def test_spill_merge_tree_conflict_carries_detail(tmp_path, no_git_identity):
    repo, master, pr, _clean = _fixture_repo_with_conflicting_branches(tmp_path / "fx.git")
    with pytest.raises(RuntimeError) as ei:
        dsp.spill_merge_tree(repo, master, pr)
    assert "merge-tree conflict" in str(ei.value)
    assert "CONFLICT (content): Merge conflict in f.txt" in str(ei.value)


def test_redact_masks_token_containing_at(monkeypatch):
    """Bare-value pass must run before the URL pass or '@'-bearing tokens leak."""
    token = "abc@def"
    monkeypatch.setattr(dsp, "FETCH", token)
    out = dsp.redact(f"git fetch -q https://langyo:{token}@github.com/o/r.git")
    assert token not in out
    assert "https://langyo:***@github.com/o/r.git" in out


def test_redact_leaves_port_urls_alone():
    url = "see http://127.0.0.1:3080/notes@team for details"
    assert dsp.redact(url) == url


def test_all_exception_log_lines_are_redacted():
    """Guard every exception log site, not just the one covered behaviorally."""
    src = open(TOOL, encoding="utf-8").read().splitlines()
    through_redact = [ln for ln in src if "log(" in ln and "str(e)" in ln]
    # self-test the finder: the seven existing sites must be found
    assert len(through_redact) >= 7, f"finder pattern broke: matched {len(through_redact)}"
    for ln in through_redact:
        assert "redact(" in ln, f"str(e) logged without redact: {ln.strip()}"
    raw_e = [ln for ln in src if "log(" in ln and "{e}" in ln]
    assert raw_e == [], f"raw exception interpolation in log lines: {raw_e}"
