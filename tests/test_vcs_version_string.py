"""Tests for the version-string facility (vcs/version_string.py)."""

import subprocess
from pathlib import Path
import pytest
from celestia_devtools.vcs.version_string import default_base, version_string

# ── fixture helpers ──────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> None:
    # core.hooksPath= disables the workspace-global org commit-msg hook —
    # the fixture's "init" subject would otherwise fail the gitmoji lint.
    subprocess.run(["git", "-c", "core.hooksPath=", "-C", str(repo), *args], check = True, capture_output = True)

def _git_repo(tmp_path: Path, workspace_version: str) -> Path:
    """A minimal git repo whose workspace Cargo.toml carries one version line."""
    repo = tmp_path / "repo"
    (repo / "packages" / "core").mkdir(parents = True)
    (repo / "Cargo.toml").write_text(f'[workspace.package]\nversion = "{workspace_version}"\n', encoding = "utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo

# ── tests ────────────────────────────────────────────────────────────────────

def test_default_base_is_the_full_workspace_version(tmp_path: Path) -> None:
    """The 2026-10-10 direction: the default base is the honest FULL version
    parsed from the workspace Cargo.toml — never a truncated major.minor.
    Restoring the pre-#166 `major.minor` collapse makes this red."""
    repo = _git_repo(tmp_path, "0.1.0")
    assert default_base(repo) == "0.1.0"

def test_default_base_keeps_prerelease_and_patch(tmp_path: Path) -> None:
    """Prerelease/patch tails ride through verbatim (`2.7.9-rc.3` stays whole)."""
    repo = _git_repo(tmp_path, "2.7.9-rc.3")
    assert default_base(repo) == "2.7.9-rc.3"

def test_default_base_falls_back_without_cargo_toml(tmp_path: Path) -> None:
    """No Cargo.toml → the family-baseline fallback, not a fabricated parse."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert default_base(repo) == "0.1.0"

def test_version_string_keeps_the_caller_base(tmp_path: Path) -> None:
    """version_string() renders the caller's base verbatim (plana's resolver
    keeps the caller's base the same way)."""
    repo = _git_repo(tmp_path, "0.1.0")
    line = version_string(repo, "2.7.9-rc.3")
    assert line.startswith("2.7.9-rc.3 "), f"line {line} keeps the caller base"
    assert ":" in line, f"line {line} carries branch:hash"

def test_version_string_emits_the_single_colon_shape(tmp_path: Path) -> None:
    """The 2026-10-11 narrowing: the identity token is `<branch>:<hash7>`
    with ONE colon. Restoring the 2026-10-08 double-colon emit makes this
    red — down-stream parsers accept both, but fresh lines speak the
    single-colon shape."""

    repo = _git_repo(tmp_path, "0.1.0")
    branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], check = True, capture_output = True, text = True).stdout.strip()
    line = version_string(repo, "0.1.0")
    token = line.split()[-1]
    name, sep, hash7 = token.rpartition(":")
    assert sep == ":", f"line {line} carries a colon separator"
    assert (name == branch) and not name.endswith(":"), (f"line {line} keeps a clean branch name")
    assert (len(hash7) == 7) and all(c in "0123456789abcdef" for c in hash7), (f"line {line} ends in the short hex hash")

# ── the 2026-10-11 mislabel incident ─────────────────────────────────────────

def _mislabel_repo(tmp_path: Path, advance_master: bool, default: str = "master") -> Path:
    """The incident's repo shape: the default branch (``default``) carries a
    same-tip feature branch, and HEAD sits detached on that tip (the deploy
    wave's checkout shape). With advance_master the default branch AND its
    remote-tracking ref move one commit ahead first — the honest off-tip
    case where the ladder must keep answering."""
    repo = tmp_path / "repo"
    repo.mkdir(parents = True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "tip")
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check = True, capture_output = True, text = True).stdout.strip()
    remote_ref = f"refs/remotes/origin/{default}"
    _git(repo, "update-ref", remote_ref, tip)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", remote_ref)
    _git(repo, "branch", "feat/stale", tip)

    if advance_master:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "ahead")
        ahead = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check = True, capture_output = True, text = True).stdout.strip()
        _git(repo, "update-ref", remote_ref, ahead)

    _git(repo, "checkout", "-q", "--detach", tip)
    return repo

def test_detached_default_tip_wins_over_same_tip_feature_branch(tmp_path: Path) -> None:
    """Incident shape: name-rev would answer feat/stale (and did, in
    production) — the default-tip rule must claim the identity."""
    repo = _mislabel_repo(tmp_path, advance_master = False)
    line = version_string(repo, "0.1.0")
    assert " master:" in f" {line}", (f"line {line} must name the default branch, not the stale feature tip")

def test_detached_off_tip_keeps_the_ladder_answer(tmp_path: Path) -> None:
    """master moved ahead and the fetch landed; HEAD is genuinely only the
    feature branch's tip — the ladder's honest answer stands."""
    repo = _mislabel_repo(tmp_path, advance_master = True)
    line = version_string(repo, "0.1.0")
    assert " feat/stale:" in f" {line}", (f"line {line} must keep the ladder's feature-branch answer")

def test_detached_slash_named_default_tip_also_wins(tmp_path: Path) -> None:
    """R2 2026-10-11: a slash-named default (release/2026-10) must keep its
    full name — last-component extraction would probe refs/heads/2026-10,
    find nothing, and silently disarm the guard (the incident shape would
    survive on such a repo)."""
    repo = _mislabel_repo(tmp_path, advance_master = False, default = "release/2026-10")
    line = version_string(repo, "0.1.0")
    assert " release/2026-10:" in f" {line}", (f"line {line} must name the slash-named default branch")

def test_env_passthrough_pins_the_stated_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine passthrough outranks the ladder: the branch a builder
    states is honored when (and only when) its hash pins to this HEAD."""
    repo = _mislabel_repo(tmp_path / "on-tip", advance_master = False)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check = True, capture_output = True, text = True).stdout.strip()
    monkeypatch.setenv("CELESTIA_BUILD_BRANCH", "release/2026-10")
    monkeypatch.setenv("CELESTIA_BUILD_HASH", head)
    assert " release/2026-10:" in f" {version_string(repo, '0.1.0')}"

    # The fall-through cases need a repo whose honest answer is NOT the
    # default branch (off-tip: the ladder names feat/stale).
    off_tip = _mislabel_repo(tmp_path / "off-tip", advance_master = True)
    monkeypatch.setenv("CELESTIA_BUILD_BRANCH", "release/2026-10")
    monkeypatch.setenv("CELESTIA_BUILD_HASH", "0" * 40)
    assert " feat/stale:" in f" {version_string(off_tip, '0.1.0')}", ("a stale pin (hash of some other checkout) must not relabel anything")
    monkeypatch.delenv("CELESTIA_BUILD_HASH")
    assert " feat/stale:" in f" {version_string(off_tip, '0.1.0')}", ("a branch alone is no statement")
