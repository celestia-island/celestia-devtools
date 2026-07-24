#!/usr/bin/env python3
"""Locate celestia-island crate checkouts.

Resolves the local checkout of any sibling crate in the org's dev layout
(env var → cargo ``[patch]`` → sibling dir → recursive scan → git clone).

Resolution priority (cheap first, so the common case never walks the tree):

    1. ``$<CRATE>_ROOT`` or ``$<CRATE>_REPO``
       (e.g. ``$ARONA_ROOT``, ``$ARIS_REPO``)
    2. a ``crate = { path = ".." }`` under any ``[patch.*]`` in
       ``~/.cargo/config.toml`` or the caller repo's top-level ``Cargo.toml``
    3. a sibling ``../<crate>`` checkout (the org dev layout)
    4. (fallback) the same ``[patch]`` search across nested ``Cargo.toml``
       files, pruning ``target/`` / ``node_modules/`` / ``.git`` so a
       30+ GiB ``target/`` doesn't stall every invocation
    5. shallow ``git clone`` into ``<repo_root>/target/<crate>-shared``

Usage::

    celestia-devtools locate --crate arona
    celestia-devtools locate --crate entelecheia --env ENTELECHEIA_ROOT
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ORG_GIT_BASE = "https://github.com/celestia-island"
DEFAULT_MARKER = "Cargo.toml"

# Last-resort clone destination lives INSIDE cargo's own ``target/`` dir
# (already gitignored everywhere), so no separate scratch dir or ignore rule
# is needed. Override with ``CELESTIA_DEV_TARGET_DIR`` if you really must.
TARGET_DIR = os.environ.get("CELESTIA_DEV_TARGET_DIR", "target")


def _stderr(level: str, msg: str) -> None:
    print(f"[locate] {level}: {msg}", file=sys.stderr)


def _crate_info(crate: str, env_var: str | None, git_url: str | None):
    """Derive (env_var, git_url, marker) for *crate* by convention."""
    return (
        env_var or f"{crate.upper()}_ROOT",
        git_url or f"{ORG_GIT_BASE}/{crate}.git",
        Path(DEFAULT_MARKER),
    )


def find_patched_crate(
    crate: str,
    env_var: str,
    git_url: str,
    marker: Path,
    *,
    repo_root: Path | None = None,
    clone_subdir: str | None = None,
) -> Path | None:
    """Resolve the checkout of a cargo path/git-patched crate.

    See module docstring for the resolution priority.  Returns the resolved
    root, or ``None`` if the crate could not be found or cloned.
    """
    repo_root = repo_root or Path.cwd()

    def _ok(cand: Path) -> bool:
        return bool(cand and (cand / marker).exists())

    # 1. explicit override — honour both <CRATE>_ROOT and <CRATE>_REPO
    #    (the latter is the convention kei/aris use, e.g. $ARIS_REPO).
    for var in (env_var, f"{crate.upper()}_REPO"):
        if env := os.environ.get(var):
            c = Path(env).expanduser()
            if _ok(c):
                return c.resolve()

    pat = re.compile(
        rf'\b{re.escape(crate)}\s*=\s*\{{\s*[^}}]*\bpath\s*=\s*"([^"]+)"', re.S,
    )

    def _check_cfg(text: str, base: Path) -> Path | None:
        for m in pat.finditer(text):
            p = Path(m.group(1))
            if not p.is_absolute():
                p = base / p
            if _ok(p):
                return p.resolve()
        return None

    # 2. the two config files that almost always carry the org-wide patch
    for cfg in (Path.home() / ".cargo" / "config.toml", repo_root / "Cargo.toml"):
        try:
            hit = _check_cfg(cfg.read_text(errors="ignore"), cfg.parent)
        except OSError:
            hit = None
        if hit:
            return hit

    # 3. sibling layout
    sib = repo_root.parent / crate
    if _ok(sib):
        return sib.resolve()

    # 4. fallback: scan nested Cargo.toml, pruning heavy dirs
    _SKIP_DIRS = {"target", "node_modules", ".git", ".next", "dist", "build"}
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if "Cargo.toml" not in files:
            continue
        cfg = Path(root) / "Cargo.toml"
        try:
            hit = _check_cfg(cfg.read_text(errors="ignore"), cfg.parent)
        except OSError:
            hit = None
        if hit:
            return hit

    # 5. last resort: clone into the repo's cargo target/ dir (already ignored)
    clone = repo_root / TARGET_DIR / (clone_subdir or f"{crate}-shared")
    if not _ok(clone):
        try:
            clone.parent.mkdir(parents=True, exist_ok=True)
            _stderr("warn", f"{crate} not found locally — cloning into {clone}")
            subprocess.run(
                ["git", "clone", "--depth", "1", git_url, str(clone)], check=False,
            )
        except OSError as exc:
            _stderr("error", f"git clone of {crate} failed: {exc}")
    if _ok(clone):
        return clone.resolve()
    return None


def find_crate(
    crate: str, *, repo_root: Path | None = None,
    env_var: str | None = None, git_url: str | None = None,
) -> Path | None:
    """Locate a celestia-island crate checkout by name."""
    ev, gu, marker = _crate_info(crate, env_var, git_url)
    return find_patched_crate(crate, ev, gu, marker, repo_root=repo_root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Locate a celestia-island crate checkout"
    )
    parser.add_argument(
        "--crate", required=True,
        help="crate name to locate",
    )
    parser.add_argument(
        "--env", default=None,
        help="env var name to check first (default: <CRATE>_ROOT)",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="repo root to search from (default: cwd)",
    )
    args = parser.parse_args()

    env_var, git_url, marker = _crate_info(args.crate, args.env, None)
    repo_root = Path(args.repo_root) if args.repo_root else None
    found = find_patched_crate(
        args.crate, env_var, git_url, marker, repo_root=repo_root,
    )
    if found is None:
        _stderr("error", f"could not locate {args.crate}; set {env_var}")
        return 127
    print(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
