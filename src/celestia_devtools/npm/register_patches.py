#!/usr/bin/env python3
"""DEPRECATED — writing ``link:`` sibling overrides into repo files is retired.

Org decision (2026-09): celestia-island repo manifests must never reference
sibling checkouts. ``@celestia-island/*`` dependencies resolve from the npm
registry by default; local development overlays sibling checkouts via
``celestia-devtools link-npm-siblings`` (node_modules symlinks, no tracked
file is touched).

This module keeps the sibling-package discovery helpers shared with
``link_siblings`` and supports one operation: removing the overrides block
previous versions auto-generated in ``pnpm-workspace.yaml``::

    celestia-devtools register-npm-patches --remove

Running it without ``--remove`` prints the retirement notice and fails.
Note the historical generated block used a nested ``pnpm: overrides:`` shape
that pnpm 11 does not honor as overrides anyway — the linking effect came
from the (likewise retired) ``../`` workspace members.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple


class NpmPackage(NamedTuple):
    """An npm package discovered inside a sibling repo."""

    name: str       # full package name (e.g. "@celestia-island/hikari")
    path: Path      # absolute path to the directory containing package.json
    repo: str       # repo name (e.g. "hikari")
    version: str    # declared version (e.g. "0.4.0")


# ── discovery (shared with npm/link_siblings.py) ──────────────────────────


def _git_remote_origin(repo_path: Path) -> str | None:
    """Return the raw origin URL for *repo_path*, or ``None``.

    Reads ``remote.origin.url`` from config directly: ``git remote get-url``
    applies ``url.<x>.insteadOf`` rewrites (active on self-hosted runners,
    which map github.com/celestia-island/* onto local bare mirrors) and would
    hide the canonical URL this regex needs to match.
    """
    try:
        import subprocess as _subprocess

        r = _subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, _subprocess.SubprocessError):
        pass
    return None


def is_celestia_repo(repo_path: Path) -> str | None:
    """Check if *repo_path* is a celestia-island git repo."""
    url = _git_remote_origin(repo_path)
    if not url:
        return None
    m = re.search(r"celestia-island[:/]([^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def discover_npm_packages(repo_path: Path, repo_name: str) -> list[NpmPackage]:
    """Find all ``@celestia-island/*`` npm packages inside *repo_path*.

    Scans the ``packages/*/package.json`` level of the repo only.
    """
    packages: list[NpmPackage] = []
    try:
        for pkg_json in sorted(repo_path.glob("packages/*/package.json")):
            if not pkg_json.is_file():
                continue
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            name = data.get("name")
            version = data.get("version", "0.0.0")
            if isinstance(name, str) and name.startswith("@celestia-island/"):
                packages.append(NpmPackage(
                    name=name,
                    path=pkg_json.parent.resolve(),
                    repo=repo_name,
                    version=version,
                ))
    except OSError:
        pass
    return packages


def scan_celestia_npm_packages(scan_dir: Path, *, exclude_repo: str | None = None) -> list[NpmPackage]:
    """Scan *scan_dir* for celestia-island sibling repos with npm packages.

    If *exclude_repo* is provided, packages whose repo name matches are skipped
    (prevents self-references when scanning from within the target repo).
    """
    packages: list[NpmPackage] = []
    if not scan_dir.is_dir():
        return packages

    for entry in sorted(scan_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir() or not (entry / ".git").exists():
                continue
        except OSError:
            # Unreadable directories (permission, stale NFS entries) are
            # simply not candidate sibling repos.
            continue
        repo_name = is_celestia_repo(entry)
        if not repo_name:
            continue
        if exclude_repo and repo_name == exclude_repo:
            continue
        found = discover_npm_packages(entry, repo_name)
        packages.extend(found)

    return packages


def detect_package_manager(target_dir: Path) -> str:
    """Detect which package manager is in use.

    Returns one of: ``"pnpm"``, ``"npm"``, ``"yarn"``, ``"unknown"``.
    """
    files = sorted(os.listdir(target_dir)) if target_dir.is_dir() else []

    # pnpm
    if "pnpm-lock.yaml" in files or "pnpm-workspace.yaml" in files:
        return "pnpm"

    # yarn
    if "yarn.lock" in files:
        return "yarn"

    # npm
    if "package-lock.json" in files:
        return "npm"

    return "unknown"


# ── legacy overrides block removal ────────────────────────────────────────

START_MARKER = "# ── celestia-devtools auto-registered npm overrides ──"
END_MARKER = "# ── end celestia-devtools npm overrides ──"


def remove_overrides_block(text: str) -> tuple[str, bool]:
    """Strip the auto-generated overrides block from *text*.

    Returns ``(new_text, changed)``. Surrounding blank-line runs collapse to
    a single blank line so removal leaves tidy YAML behind. Text without the
    markers is returned unchanged.
    """
    if START_MARKER not in text:
        return text, False
    parts = re.split(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
        text, flags=re.S,
    )
    new_text = ("\n\n".join(p.strip("\n") for p in parts if p.strip("\n")) + "\n") \
        if any(p.strip("\n") for p in parts) else ""
    return new_text, True


def _resolve_scan_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_root = os.environ.get("CELESTIA_ROOT")
    if env_root:
        return Path(env_root)
    return Path.cwd().parent


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retired — remove legacy link: overrides (use link-npm-siblings)",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="remove the legacy auto-generated overrides block from pnpm-workspace.yaml",
    )
    args = parser.parse_args()

    if not args.remove:
        print(
            "\033[31m[register-npm-patches] RETIRED: repo files must not reference "
            "sibling checkouts.\033[0m",
            file=sys.stderr,
        )
        print(
            "Dependencies resolve from the npm registry; for local development "
            "run `celestia-devtools link-npm-siblings` instead (node_modules "
            "symlinks, no tracked file changes).",
            file=sys.stderr,
        )
        print(
            "To clean a legacy block out of this workspace run: "
            "`celestia-devtools register-npm-patches --remove`",
            file=sys.stderr,
        )
        return 1

    workspace_path = Path.cwd() / "pnpm-workspace.yaml"
    if not workspace_path.is_file():
        print(
            "[register-npm-patches] pnpm-workspace.yaml not found — run from the "
            "workspace root.",
            file=sys.stderr,
        )
        return 1

    text = workspace_path.read_text(encoding="utf-8", errors="ignore")
    new_text, changed = remove_overrides_block(text)
    if not changed:
        print("[register-npm-patches] no legacy overrides block present")
        return 0
    workspace_path.write_text(new_text, encoding="utf-8")
    print(f"[register-npm-patches] removed legacy overrides block from {workspace_path}")
    print(
        "Reminder: also drop any '../' workspace members so the workspace stays "
        "registry-only.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
