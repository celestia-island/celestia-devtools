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
