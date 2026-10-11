"""Tests for the version-string facility (vcs/version_string.py)."""

from pathlib import Path

from celestia_devtools.vcs.version_string import default_base, version_string


# ── fixture helpers ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    import subprocess

    # core.hooksPath= disables the workspace-global org commit-msg hook —
    # the fixture's "init" subject would otherwise fail the gitmoji lint.
    subprocess.run(
        ["git", "-c", "core.hooksPath=", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _git_repo(tmp_path: Path, workspace_version: str) -> Path:
    """A minimal git repo whose workspace Cargo.toml carries one version line."""
    repo = tmp_path / "repo"
    (repo / "packages" / "core").mkdir(parents=True)
    (repo / "Cargo.toml").write_text(
        f'[workspace.package]\nversion = "{workspace_version}"\n', encoding="utf-8"
    )
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
    import subprocess

    repo = _git_repo(tmp_path, "0.1.0")
    branch = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )
    line = version_string(repo, "0.1.0")
    token = line.split()[-1]
    name, sep, hash7 = token.rpartition(":")
    assert sep == ":", f"line {line} carries a colon separator"
    assert name == branch and not name.endswith(":"), (
        f"line {line} keeps a clean branch name"
    )
    assert len(hash7) == 7 and all(c in "0123456789abcdef" for c in hash7), (
        f"line {line} ends in the short hex hash"
    )
