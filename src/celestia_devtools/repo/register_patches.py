#!/usr/bin/env python3
"""Auto-register local celestia-island checkouts as cargo ``[patch]`` entries.

Scans the sibling directory (the standard celestia dev layout) for git
repositories whose ``origin`` remote points at ``celestia-island/*.git``.
For each repo, parses its workspace ``Cargo.toml`` to discover member
crates (name + relative path), then writes/updates ``[patch]`` sections
in ``~/.cargo/config.toml`` so that every celestia-island crate resolves
to its local working copy.

Two kinds of patch sections are emitted:

* ``[patch."https://github.com/celestia-island/<repo>.git"]`` — for crates
  that other repos depend on as a **git dependency** (the typical case for
  internal-only crates like ``arona``, ``hikari-*``, ``tairitsu-*``).
* ``[patch.crates-io]`` — for crates that are **published to crates.io**
  and consumed via a version dependency (e.g. ``aoba``, ``libnoa``).

A crate is considered "published" if it has ``publish = true`` or no
``publish`` field *and* a non-empty ``[package] description`` in its
manifest (heuristic — the user can override with ``--crates-io``).

Usage::

    celestia-devtools register-patches
    celestia-devtools register-patches --scan-dir /path/to/celestia
    celestia-devtools register-patches --dry-run
    celestia-devtools register-patches --crates-io aoba,libnoa
    celestia-devtools register-patches --repo lagrange,hikari
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ORG_GIT_BASE = "https://github.com/celestia-island"
CARGO_CONFIG = Path.home() / ".cargo" / "config.toml"

# Crates known to be published to crates.io and consumed via version deps.
# Extended by the --crates-io flag. The defaults cover the current set.
DEFAULT_CRATES_IO = {"aoba", "libnoa"}


class CrateInfo(NamedTuple):
    """A single member crate discovered in a repo workspace."""

    name: str       # crates.io name (e.g. "hikari-components")
    path: Path      # absolute path to the crate directory (containing Cargo.toml)
    repo: str       # repo name (e.g. "hikari")


class RepoInfo(NamedTuple):
    """A discovered celestia-island repo with its member crates."""

    name: str           # repo name (e.g. "hikari")
    git_url: str        # full git URL
    local_path: Path    # absolute path to the repo root
    crates: list[CrateInfo]


# ── discovery ──────────────────────────────────────────────────────────────


def _git_remote_origin(repo_path: Path) -> str | None:
    """Return the origin URL for *repo_path*, or ``None``."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def is_celestia_repo(repo_path: Path) -> str | None:
    """Check if *repo_path* is a celestia-island repo.

    Returns the repo name (e.g. ``"hikari"``) if it is, or ``None``.
    """
    url = _git_remote_origin(repo_path)
    if not url:
        return None
    # Match both https and ssh URLs:
    #   https://github.com/celestia-island/hikari.git
    #   git@github.com:celestia-island/hikari.git
    m = re.search(r"celestia-island[:/]([^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def scan_celestia_repos(scan_dir: Path) -> list[RepoInfo]:
    """Scan *scan_dir* for celestia-island git repos.

    Each immediate subdirectory that is a git repo with a celestia-island
    origin is inspected for workspace member crates.
    """
    repos: list[RepoInfo] = []
    if not scan_dir.is_dir():
        return repos

    for entry in sorted(scan_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / ".git").exists():
            continue
        repo_name = is_celestia_repo(entry)
        if not repo_name:
            continue
        crates = parse_workspace_crates(entry, repo_name)
        if crates:
            repos.append(RepoInfo(
                name=repo_name,
                git_url=f"{ORG_GIT_BASE}/{repo_name}.git",
                local_path=entry,
                crates=crates,
            ))
    return repos


# ── Cargo.toml parsing ────────────────────────────────────────────────────


def _extract_package_name(manifest_text: str) -> str | None:
    """Extract the ``[package] name`` field from a Cargo.toml manifest."""
    # Simple regex — we don't want to pull in a full TOML parser dependency.
    # Match `name = "..."` within the [package] section.
    m = re.search(
        r'^\[package\]\s*(.*?)(?=\n\[|\Z)',
        manifest_text, re.S | re.M,
    )
    if not m:
        return None
    pkg_section = m.group(1)
    nm = re.search(r'^name\s*=\s*"([^"]+)"', pkg_section, re.M)
    return nm.group(1) if nm else None


def _is_published(manifest_text: str) -> bool:
    """Heuristic: is this crate likely published to crates.io?

    A crate is considered published if:
    * ``publish = true`` explicitly, OR
    * no ``publish`` field AND has a non-empty ``description``.
    Crates with ``publish = false`` are definitely not published.
    """
    m = re.search(
        r'^\[package\]\s*(.*?)(?=\n\[|\Z)',
        manifest_text, re.S | re.M,
    )
    if not m:
        return False
    pkg = m.group(1)
    pub_match = re.search(r'^publish\s*=\s*(true|false)', pkg, re.M)
    if pub_match:
        return pub_match.group(1) == "true"
    # No publish field: check for description (heuristic for published crates).
    desc_match = re.search(r'^description\s*=\s*"([^"]+)"', pkg, re.M)
    return bool(desc_match and desc_match.group(1).strip())


def parse_workspace_crates(
    repo_path: Path, repo_name: str,
) -> list[CrateInfo]:
    """Parse a repo's Cargo.toml to discover all member crates.

    Handles both workspace manifests (``[workspace] members = [...]``) and
    single-crate manifests (just ``[package]``). Returns a list of
    :class:`CrateInfo` with absolute paths.
    """
    root_manifest = repo_path / "Cargo.toml"
    if not root_manifest.is_file():
        return []

    text = root_manifest.read_text(encoding="utf-8", errors="ignore")
    crates: list[CrateInfo] = []

    # Case 1: single-crate manifest (has [package] but no [workspace]).
    if "[package]" in text and "[workspace]" not in text:
        name = _extract_package_name(text)
        if name:
            crates.append(CrateInfo(
                name=name, path=repo_path, repo=repo_name,
            ))
        return crates

    # Case 2: workspace manifest — parse members list.
    # Handle both inline arrays and multiline arrays:
    #   members = ["a", "b"]
    #   members = [
    #       "packages/a",
    #       "packages/b",
    #   ]
    ws_match = re.search(
        r'^\[workspace\]\s*(.*?)(?=\n\[|\Z)',
        text, re.S | re.M,
    )
    if not ws_match:
        return crates

    ws_section = ws_match.group(1)
    members_match = re.search(
        r'members\s*=\s*\[(.*?)\]',
        ws_section, re.S,
    )
    if not members_match:
        return crates

    # Extract quoted paths from the members array.
    member_paths = re.findall(r'"([^"]+)"', members_match.group(1))
    # Also check members.exclude — those should NOT be registered.
    exclude_match = re.search(
        r'exclude\s*=\s*\[(.*?)\]',
        ws_section, re.S,
    )
    excluded = set()
    if exclude_match:
        excluded = set(re.findall(r'"([^"]+)"', exclude_match.group(1)))

    # Directories that contain example/test/benchmark crates — these are
    # not library crates other repos depend on, and registering them as
    # patches can cause version conflicts in the dependency graph.
    _SKIP_DIRS = {"examples", "tests", "benches", "fuzz", "e2e"}

    for mp in member_paths:
        if mp in excluded:
            continue
        # Skip member paths under examples/, tests/, etc.
        parts = mp.replace("\\", "/").split("/")
        if any(p in _SKIP_DIRS for p in parts):
            continue
        crate_dir = repo_path / mp
        manifest = crate_dir / "Cargo.toml"
        if not manifest.is_file():
            continue
        mtext = manifest.read_text(encoding="utf-8", errors="ignore")
        name = _extract_package_name(mtext)
        if name:
            # Skip crates whose name ends with -e2e (end-to-end test crates).
            if name.endswith("-e2e"):
                continue
            crates.append(CrateInfo(
                name=name, path=crate_dir.resolve(), repo=repo_name,
            ))

    return crates


# ── config.toml generation ────────────────────────────────────────────────


def _path_to_toml(p: Path) -> str:
    """Convert a path to a TOML-safe string with forward slashes."""
    return str(p).replace("\\", "/")


def generate_patch_sections(
    repos: list[RepoInfo],
    crates_io_names: set[str],
) -> str:
    """Generate the ``[patch]`` TOML sections for all discovered repos.

    Crates whose name is in *crates_io_names* go into ``[patch.crates-io]``;
    the rest go into ``[patch."<git-url>"]`` per repo.
    """
    lines: list[str] = []
    lines.append("")
    lines.append(
        "# ── celestia-devtools auto-registered patches ──────────────────────"
    )
    lines.append(
        "# Generated by `celestia-devtools register-patches`. Do not edit"
    )
    lines.append(
        "# manually — re-run the command after cloning new repos."
    )
    lines.append("")

    # Group crates: crates.io vs per-repo git patches.
    crates_io_crates: list[CrateInfo] = []
    per_repo: dict[str, list[CrateInfo]] = {}

    for repo in repos:
        for crate in repo.crates:
            if crate.name in crates_io_names:
                crates_io_crates.append(crate)
            else:
                per_repo.setdefault(repo.name, []).append(crate)

    # Emit [patch.crates-io] section.
    if crates_io_crates:
        lines.append("[patch.crates-io]")
        for crate in sorted(crates_io_crates, key=lambda c: c.name):
            lines.append(
                f'{crate.name} = {{ path = "{_path_to_toml(crate.path)}" }}'
            )
        lines.append("")

    # Emit [patch."<git-url>"] sections.
    for repo in sorted(repos, key=lambda r: r.name):
        repo_crates = per_repo.get(repo.name, [])
        if not repo_crates:
            continue
        lines.append(f'[patch."{ORG_GIT_BASE}/{repo.name}.git"]')
        for crate in sorted(repo_crates, key=lambda c: c.name):
            lines.append(
                f'{crate.name} = {{ path = "{_path_to_toml(crate.path)}" }}'
            )
        lines.append("")

    return "\n".join(lines)


# ── config.toml merge ────────────────────────────────────────────────────


def _strip_all_patch_sections(text: str) -> str:
    """Remove every ``[patch.*]`` section from *text*.

    A patch section starts at a line like ``[patch.crates-io]`` or
    ``[patch."https://..."]`` and extends until the next top-level
    ``[section]`` header (``[foo]``, not ``[[foo]]`` which is an array
    table) or EOF. Consecutive comment lines immediately preceding a
    patch section header are also stripped (they document the patch
    block).
    """
    lines = text.split("\n")
    # First pass: identify line indices that belong to patch sections.
    in_patch = False
    skip_lines: set[int] = set()
    pending_comments: list[int] = []  # indices of comment lines

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Detect start of a patch section.
        if re.match(r'^\[patch\b', stripped):
            in_patch = True
            skip_lines.update(pending_comments)  # strip doc comments above
            pending_comments = []
            skip_lines.add(i)
            continue

        if in_patch:
            # A new non-patch top-level section header ends the patch block.
            if stripped.startswith("[") and not stripped.startswith("[["):
                in_patch = False
                pending_comments = []
                # Don't skip this line — it's the next section.
            else:
                skip_lines.add(i)
                # Trailing blank lines after the last patch entry are part
                # of the section and will be skipped.
                continue

        # Track comment lines that might document an upcoming patch section.
        if stripped.startswith("#"):
            pending_comments.append(i)
        elif stripped == "":
            # Blank line breaks the comment-run — they're standalone comments.
            pending_comments = []
        else:
            pending_comments = []

    # Second pass: keep lines not in skip_lines.
    result = [line for i, line in enumerate(lines) if i not in skip_lines]
    # Collapse multiple consecutive blank lines into one.
    cleaned: list[str] = []
    prev_blank = False
    for line in result:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank
    return "\n".join(cleaned).rstrip() + "\n"


def update_cargo_config(
    new_sections: str,
    config_path: Path = CARGO_CONFIG,
    *,
    dry_run: bool = False,
) -> bool:
    """Replace all ``[patch]`` sections in the cargo config with *new_sections*.

    This first strips every existing ``[patch.*]`` section (whether
    auto-managed or hand-written) to avoid duplicate section headers, then
    appends the freshly generated managed block. Returns ``True`` if the
    file was (or would be) modified.
    """
    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8", errors="ignore")

    # Remove all existing [patch.*] sections.
    stripped = _strip_all_patch_sections(existing)

    # Append the new managed block.
    new_text = stripped.rstrip() + "\n" + new_sections

    if new_text.rstrip() == existing.rstrip():
        return False

    if dry_run:
        print(f"[register-patches] dry-run: would update {config_path}")
        print("--- old tail ---")
        print(existing[-300:] if len(existing) > 300 else existing)
        print("--- new tail ---")
        print(new_text[-500:] if len(new_text) > 500 else new_text)
        return True

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_text, encoding="utf-8")
    print(f"[register-patches] updated {config_path}")
    return True


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-register celestia-island repos as cargo [patch] entries",
    )
    parser.add_argument(
        "--scan-dir", default=None,
        help="directory to scan for repos (default: parent of this repo)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without writing",
    )
    parser.add_argument(
        "--crates-io", default=",".join(sorted(DEFAULT_CRATES_IO)),
        help="comma-separated crate names to put in [patch.crates-io] "
             f"(default: {','.join(sorted(DEFAULT_CRATES_IO))})",
    )
    parser.add_argument(
        "--repo", default=None,
        help="comma-separated repo names to include (default: all discovered)",
    )
    parser.add_argument(
        "--config", default=str(CARGO_CONFIG),
        help=f"cargo config path (default: {CARGO_CONFIG})",
    )
    args = parser.parse_args()

    # Determine scan directory.
    if args.scan_dir:
        scan_dir = Path(args.scan_dir)
    else:
        # Default: the parent of the celestia dev layout. We find it by
        # looking for the common ancestor of sibling repos — but the
        # simplest heuristic is the parent of the current working dir.
        scan_dir = Path.cwd().parent

    print(f"[register-patches] scanning {scan_dir} …")
    repos = scan_celestia_repos(scan_dir)

    if not repos:
        print("[register-patches] no celestia-island repos found", file=sys.stderr)
        return 1

    # Filter by --repo if specified.
    if args.repo:
        wanted = {r.strip() for r in args.repo.split(",") if r.strip()}
        repos = [r for r in repos if r.name in wanted]

    crates_io_names = {
        n.strip() for n in args.crates_io.split(",") if n.strip()
    }

    # Report what was found.
    total_crates = sum(len(r.crates) for r in repos)
    print(f"[register-patches] found {len(repos)} repo(s), {total_crates} crate(s):")
    for repo in sorted(repos, key=lambda r: r.name):
        print(f"  {repo.name}:")
        for crate in repo.crates:
            target = "crates-io" if crate.name in crates_io_names else "git-patch"
            print(f"    {crate.name} → {target}")

    # Generate and merge.
    new_sections = generate_patch_sections(repos, crates_io_names)
    config_path = Path(args.config)
    changed = update_cargo_config(
        new_sections, config_path, dry_run=args.dry_run,
    )
    if not changed:
        print("[register-patches] config already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
