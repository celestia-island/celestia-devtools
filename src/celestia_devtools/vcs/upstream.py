#!/usr/bin/env python3
"""Upstream fork sync — Python port of the former bash ``upstream-sync`` recipe.

Fetches the latest commits from the ``upstream`` remote so a human can merge
or rebase deliberately. This command never auto-merges.

Prerequisites: an ``upstream`` git remote. If absent, one is added from the
``UPSTREAM_URL`` environment variable; otherwise the command fails with
guidance.

Usage::

    celestia-devtools upstream-sync
"""

from __future__ import annotations

import os
import subprocess
import sys


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def main() -> int:
    remote = _git("remote", "get-url", "upstream")
    if remote.returncode != 0:
        upstream_url = os.environ.get("UPSTREAM_URL", "")
        if not upstream_url:
            print(
                "error: no 'upstream' remote and UPSTREAM_URL is unset.",
                file=sys.stderr,
            )
            print("  Add one with:  git remote add upstream <url>", file=sys.stderr)
            print("  or set UPSTREAM_URL in your .env and re-run.", file=sys.stderr)
            return 1
        print(f"[upstream-sync] adding 'upstream' -> {upstream_url}")
        added = _git("remote", "add", "upstream", upstream_url)
        if added.returncode != 0:
            print(added.stderr, file=sys.stderr)
            return 1

    print("[upstream-sync] fetching upstream…")
    fetched = subprocess.run(["git", "fetch", "upstream"])
    if fetched.returncode != 0:
        return fetched.returncode
    print("[upstream-sync] fetched. Inspect with:")
    print("  git log --oneline HEAD..upstream/<branch>")
    print("  then merge or rebase as you see fit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
