#!/usr/bin/env python3
"""Auto-register local celestia-island checkouts as cargo ``[patch]`` entries.

**On-demand mode (default, ``--per-repo``):** runs ``cargo metadata`` in the
current repo to discover which celestia-island crates it ACTUALLY depends on
(git/source deps), then writes ONLY those patches into the repo's own
``.cargo/config.toml``. This avoids the ``was not used in the crate graph``
warning storm that the legacy global mode caused by registering every crate
in every repo.

**Legacy global mode (``--global``):** the old behaviour — scan all sibling
repos, register ALL of their crates into ``~/.cargo/config.toml``. Kept for
back-compat but discouraged; use per-repo instead.

Two kinds of patch sections are emitted:

* ``[patch."https://github.com/celestia-island/<repo>.git"]`` — for crates
  consumed as a git dependency.
* ``[patch.crates-io]`` — for crates published to crates.io.

Usage::

    celestia-devtools register-patches              # per-repo (on-demand)
    celestia-devtools register-patches --global     # legacy global mode
    celestia-devtools register-patches --dry-run
    celestia-devtools register-patches --scan-dir /path/to/celestia
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ORG_GIT_BASE = "https://github.com/celestia-island"
CARGO_CONFIG = Path.home() / ".cargo" / "config.toml"

# Per-repo config: only patches the deps this repo actually uses.
PER_REPO_CONFIG = Path.cwd() / ".cargo" / "config.toml"

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


def discover_actual_celestia_deps() -> dict[str, str]:
    """Run ``cargo metadata`` in cwd and return the celestia-island git deps.

    Returns a ``{crate_name: source_url}`` dict where source_url is the
    canonical ``https://github.com/celestia-island/<repo>.git`` form (the
    ``?branch=dev`` query is stripped, since [patch] matches on the base URL).

    Only deps whose source contains ``celestia-island`` are returned — these
    are the ones that genuinely need a [patch] entry. Path deps and
    crates.io deps don't need patching.
    """
    import json
    try:
        r = subprocess.run(
            ["cargo", "metadata", "--format-version", "1"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return {}
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}

    deps: dict[str, str] = {}
    for pkg in data.get("packages", []):
        for dep in pkg.get("dependencies", []):
            src = dep.get("source") or ""
            if "celestia-island" not in src:
                continue
            # Normalize: strip git+ prefix and ?branch= query → base URL.
            # git+https://github.com/celestia-island/arona.git?branch=dev
            # → https://github.com/celestia-island/arona.git
            url = src
            if url.startswith("git+"):
                url = url[4:]
            url = url.split("?")[0]
            deps[dep["name"]] = url
    return deps


def generate_per_repo_patches(
    actual_deps: dict[str, str],
    repos: list[RepoInfo],
    crates_io_names: set[str],
) -> str:
    """Generate [patch] sections for ONLY the deps this repo actually uses.

    Filters the discovered sibling crates down to those in *actual_deps*,
    then emits a ``[patch."<url>"]`` section per distinct git URL. This is
    the on-demand mode that avoids the ``was not used in the crate graph``
    warning storm.
    """
    # Build a lookup: crate_name → CrateInfo (from scanned sibling repos).
    crate_index: dict[str, CrateInfo] = {}
    for repo in repos:
        for crate in repo.crates:
            crate_index[crate.name] = crate

    # Match actual deps to local crate paths, grouped by git URL.
    # patches: {git_url: [(crate_name, local_path), ...]}
    patches: dict[str, list[tuple[str, Path]]] = {}
    crates_io_patches: list[tuple[str, Path]] = []
    missing: list[str] = []

    for dep_name, git_url in sorted(actual_deps.items()):
        crate = crate_index.get(dep_name)
        if crate is None:
            missing.append(dep_name)
            continue
        if dep_name in crates_io_names or git_url == "crates-io":
            crates_io_patches.append((dep_name, crate.path))
        else:
            patches.setdefault(git_url, []).append((dep_name, crate.path))

    if missing:
        print(
            f"[register-patches] {len(missing)} dep(s) have no local checkout "
            f"(will stay as git dep): {', '.join(sorted(missing))}",
            file=sys.stderr,
        )

    if not patches and not crates_io_patches:
        return ""  # No patches needed — repo uses only path/crates.io deps.

    lines: list[str] = []
    lines.append("")
    lines.append("# ── celestia-devtools on-demand patches ───────────────────────────")
    lines.append("# Generated by `celestia-devtools register-patches` (per-repo mode).")
    lines.append("# Only patches deps this repo ACTUALLY uses — no crate-graph warnings.")
    lines.append("")

    if crates_io_patches:
        lines.append("[patch.crates-io]")
        for name, path in sorted(crates_io_patches):
            lines.append(f'{name} = {{ path = "{_path_to_toml(path)}" }}')
        lines.append("")

    for git_url in sorted(patches):
        lines.append(f'[patch."{git_url}"]')
        for name, path in sorted(patches[git_url]):
            lines.append(f'{name} = {{ path = "{_path_to_toml(path)}" }}')
        lines.append("")

    return "\n".join(lines)


def _path_to_toml(p: Path) -> str:
    """Convert a path to a TOML-safe string with forward slashes."""
    return str(p).replace("\\", "/")


def generate_patch_sections(
    repos: list[RepoInfo],
    crates_io_names: set[str],
) -> str:
    """Generate the ``[patch]`` TOML sections for all discovered repos.

    Every crate is registered in **both** ``[patch.crates-io]`` and its
    repo's ``[patch."<git-url>"]`` section. Cargo silently ignores patch
    entries that don't match any dependency in the crate graph, so
    dual-registering is harmless and ensures the patch works whether a
    crate is consumed as a version dep (crates.io) or a git dep.

    The *crates_io_names* parameter is kept for API compatibility but no
    longer affects the output — all crates go into both sections.
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

    all_crates: list[CrateInfo] = []
    per_repo: dict[str, list[CrateInfo]] = {}

    for repo in repos:
        for crate in repo.crates:
            all_crates.append(crate)
            per_repo.setdefault(repo.name, []).append(crate)

    # Emit [patch.crates-io] section with ALL crates.
    if all_crates:
        lines.append("[patch.crates-io]")
        for crate in sorted(all_crates, key=lambda c: c.name):
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--per-repo", action="store_true", default=True,
        help="on-demand mode (DEFAULT): patch only deps this repo uses, "
             "write to .cargo/config.toml",
    )
    mode.add_argument(
        "--global", dest="global_mode", action="store_true",
        help="legacy mode: register ALL sibling crates into ~/.cargo/config.toml",
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
        help="(global mode only) comma-separated repo names to include",
    )
    parser.add_argument(
        "--config", default=None,
        help="cargo config path (default: per-repo → .cargo/config.toml; "
             "global → ~/.cargo/config.toml)",
    )
    args = parser.parse_args()

    # Determine scan directory.
    if args.scan_dir:
        scan_dir = Path(args.scan_dir)
    else:
        scan_dir = Path.cwd().parent

    print(f"[register-patches] scanning {scan_dir} …")
    repos = scan_celestia_repos(scan_dir)
    if not repos:
        print("[register-patches] no celestia-island repos found", file=sys.stderr)
        return 1

    crates_io_names = {
        n.strip() for n in args.crates_io.split(",") if n.strip()
    }

    # ── per-repo (on-demand) mode ──────────────────────────────
    if not args.global_mode:
        print("[register-patches] per-repo mode: analyzing actual dependencies …")
        actual_deps = discover_actual_celestia_deps()
        print(
            f"[register-patches] {len(actual_deps)} celestia-island git dep(s) "
            f"found in dependency graph"
        )
        for name, url in sorted(actual_deps.items()):
            print(f"  {name} → {url}")

        new_sections = generate_per_repo_patches(actual_deps, repos, crates_io_names)
        config_path = Path(args.config) if args.config else PER_REPO_CONFIG

        if not new_sections:
            # No patches needed — but still clean any stale managed section.
            print(f"[register-patches] no patches needed → ensuring {config_path} is clean")
            if config_path.is_file():
                changed = update_cargo_config("", config_path, dry_run=args.dry_run)
                if not changed:
                    print("[register-patches] config already clean")
            else:
                print("[register-patches] no config file, nothing to do")
            return 0

        print(f"[register-patches] writing patches to {config_path}")
        changed = update_cargo_config(new_sections, config_path, dry_run=args.dry_run)
        if not changed:
            print("[register-patches] config already up to date")
        return 0

    # ── legacy global mode ─────────────────────────────────────
    print("[register-patches] GLOBAL MODE (legacy — consider --per-repo)")
    if args.repo:
        wanted = {r.strip() for r in args.repo.split(",") if r.strip()}
        repos = [r for r in repos if r.name in wanted]

    total_crates = sum(len(r.crates) for r in repos)
    print(f"[register-patches] found {len(repos)} repo(s), {total_crates} crate(s)")

    new_sections = generate_patch_sections(repos, crates_io_names)
    config_path = Path(args.config) if args.config else CARGO_CONFIG
    changed = update_cargo_config(new_sections, config_path, dry_run=args.dry_run)
    if not changed:
        print("[register-patches] config already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
