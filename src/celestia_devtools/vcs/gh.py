#!/usr/bin/env python3
"""Transparent GitHub CLI proxy — validates commit subject on ``pr merge``, passes
everything else through to the real ``gh`` binary.

Usage (after adding to ~/.bashrc)::

    alias gh='celestia-devtools gh'

    # pr merge → subject validated before forwarding
    gh pr merge "🐛 Fix bug." --squash --repo owner/repo

    # everything else → forwarded to real gh unchanged
    gh pr list
    gh issue create
    gh repo view
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REAL_GH: str = ""

_GUESSES = [
    "/usr/bin/gh",
    "/usr/local/bin/gh",
    "/opt/homebrew/bin/gh",
    "/home/linuxbrew/.linuxbrew/bin/gh",
    str(Path.home() / ".local" / "bin" / "gh"),
]


def _find_gh() -> str:
    found = shutil.which("gh")
    if found:
        return found
    for guess in _GUESSES:
        if os.access(guess, os.X_OK):
            return guess
    return "gh"


def _real_gh() -> str:
    global _REAL_GH
    if not _REAL_GH:
        _REAL_GH = _find_gh()
    return _REAL_GH


def main() -> int:
    argv = sys.argv[1:]

    if not argv:
        subprocess.run([_real_gh()])
        return 0

    is_pr = argv[0] == "pr"
    is_pr_merge = argv[0] == "pr-merge"
    has_merge = is_pr and len(argv) > 1 and argv[1] == "merge"

    if is_pr_merge or has_merge:
        from celestia_devtools.vcs.commit_msg import lint

        # Extract --subject from args
        subject: str | None = None
        args = list(argv)
        # Remove the "pr" and "merge" prefix for arg parsing
        if is_pr:
            args = args[2:]  # skip "pr merge"
        else:
            args = args[1:]  # skip "pr-merge"

        # Find --subject value
        for i, a in enumerate(args):
            if a in ("--subject", "-s") and i + 1 < len(args):
                subject = args[i + 1]
                break
            if a == "--subject=":
                subject = a.split("=", 1)[1] if len(a) > 10 else args[i + 1] if i + 1 < len(args) else None
                break

        if subject:
            violations = lint(subject)
            if violations:
                print("\n  " + "\n  ".join(violations), file=sys.stderr)
                print(
                    "\n  Merge REJECTED. Format:  <gitmoji> <Capitalized summary.>\n"
                    "  Example:  🐛 Fix the parser crash.\n",
                    file=sys.stderr,
                )
                return 1

    # Pass through to real gh
    result = subprocess.run([_real_gh()] + argv)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
