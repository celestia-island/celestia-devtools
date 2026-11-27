#!/usr/bin/env python3
"""Transparent GitHub CLI proxy — validates commit subject on ``pr merge``,
routes ``pr edit … --body-file`` through the guarded editor, and passes
everything else through to the real ``gh`` binary.

Usage (after adding to ~/.bashrc)::

    alias gh='celestia-devtools gh'

    # pr merge → subject validated before forwarding
    gh pr merge "🐛 Fix bug." --squash --repo owner/repo

    # pr edit --body-file → guarded: mandatory --repo, an owned body path, a
    # readback assertion (see `celestia-devtools pr-edit --help`)
    gh pr edit 1586 --repo celestia-island/hikari --body-file /tmp/hikari-1586-pr-body.md

    # everything else → forwarded to real gh unchanged
    gh pr list
    gh pr edit 1586 --repo owner/repo --add-reviewer someone
    gh issue create
    gh repo view

Break-glass: ``CELESTIA_PR_EDIT_BYPASS=1`` forwards ``pr edit --body-file``
unguarded (with a loud stderr warning) — for an emergency, not for daily use.
"""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
from celestia_devtools.vcs.commit_msg import lint, repo_allows_cjk, repo_name_from_git_remote, repo_name_from_gh_flag
from celestia_devtools.vcs.pr_edit import bypass_active, bypass_warning, main as guarded_edit_main

_REAL_GH: str = ""
_ENV_SKIP = "CELESTIA_DEVTOOLS_GH_SKIP"

# apt default
# manual install / brew
# macOS brew
# brew on Linux
_GUESSES = ["/usr/bin/gh", "/usr/local/bin/gh", "/opt/homebrew/bin/gh", "/home/linuxbrew/.linuxbrew/bin/gh"]

def _find_gh() -> str:
    if os.environ.get(_ENV_SKIP):
        return "gh"

    found = shutil.which("gh")

    if found:
        canonical = os.path.realpath(found)
        myself = os.path.realpath(sys.argv[0]) if sys.argv[0] else ""

        if canonical != myself:
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

def _is_guarded_pr_edit(argv: list[str]) -> bool:
    """True for ``pr edit <N> … --body-file …`` and nothing else.

    Only the body-writing shape is intercepted: a title / reviewer / label edit
    and a bare ``gh pr view`` / ``pr close`` keep forwarding untouched, because
    the guards (repo pin, owned body path, body readback) are about the body
    write — the two recorded cross-writes (``cherino#42`` 2026-11-15,
    ``hikari#1586`` 2026-11-27) both moved a *body*.
    """
    is_edit = (len(argv) >= 3) and (argv[0] == "pr") and (argv[1] == "edit")
    writes_body = is_edit and any((a == "--body-file") or a.startswith("--body-file=") for a in argv[2:])
    return bool(writes_body)

def _guard_argv(argv: list[str]) -> list[str]:
    """``[gh,] pr edit <N> …`` → the guarded subcommand's own argv.

    ``gh`` takes the PR number positionally and ``pr-edit`` takes ``--pr``, so
    the number is the one argument that has to move; ``--repo`` and the
    ``--body-file`` ferry over as-is.  ``--body-file=X`` becomes
    ``--body-file X`` because a single-flag spelling is worth normalising where
    the guard is about to read it.  ``--expect-marker`` / ``--expect-head``
    simply pass through when a caller supplies them (a raw ``gh`` never does —
    they reject unknown flags — so their presence is an explicit opt-in).
    """
    head = argv[1:] if (argv[:1] == ["gh"]) else list(argv)
    tail = head[2:]  # after "pr edit"
    number = None
    body_file: str | None = None
    forwarded: list[str] = []
    skip = False

    for index, item in enumerate(tail):
        if skip:
            skip = False
            continue

        if (number is None) and item.isdigit():
            number = item
            continue

        if item == "--body-file":
            body_file = tail[index + 1] if (index + 1) < len(tail) else ""
            skip = True
            continue

        if item.startswith("--body-file="):
            body_file = item.split("=", 1)[1]
            continue

        forwarded.append(item)

    return (["--pr", number] if number else []) + forwarded + ((["--body-file", body_file]) if body_file is not None else [])

def main() -> int:
    argv = sys.argv[1:]

    if _is_guarded_pr_edit(argv):
        if bypass_active():
            print(bypass_warning("`gh pr edit --body-file`"), file = sys.stderr)
        else:
            # Delegated to the guarded subcommand: it owns the refusals, so one
            # implementation serves both the proxy and a direct invocation, and
            # a refusal here is byte-identical to one there.
            return guarded_edit_main(_guard_argv(argv))

    if not argv:
        subprocess.run([_real_gh()])
        return 0

    is_pr = argv[0] == "pr"
    is_pr_merge = argv[0] == "pr-merge"
    has_merge = is_pr and (len(argv) > 1) and (argv[1] == "merge")

    if is_pr_merge or has_merge:
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
            if (a in ("--subject", "-s")) and ((i + 1) < len(args)):
                subject = args[i + 1]
                break

            if a == "--subject=":
                subject = a.split("=", 1)[1] if len(a) > 10 else args[i + 1] if (i + 1) < len(args) else None
                break

        if subject:
            # Repo-aware CJK exemption: the linter exempts easy-hydro repos
            # from the English-only rule, but that needs the repo name — take
            # it from an explicit --repo/-R flag, else from the origin remote
            # of the cwd (gh pr merge is typically run inside the checkout).
            repo = repo_name_from_gh_flag(argv) or repo_name_from_git_remote()
            violations = lint(subject, allow_cjk = repo_allows_cjk(repo))

            if violations:
                print("\n  " + "\n  ".join(violations), file = sys.stderr)
                print("\n  Merge REJECTED. Format:  <gitmoji> <Capitalized summary.>\n" "  Example:  🐛 Fix the parser crash.\n", file = sys.stderr)
                return 1

    # Pass through to real gh
    env = os.environ.copy()
    env[_ENV_SKIP] = "1"
    result = subprocess.run([_real_gh()] + argv, env = env)
    return result.returncode

if __name__ == "__main__":
    raise SystemExit(main())
