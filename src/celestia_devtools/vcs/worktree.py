#!/usr/bin/env python3
"""Git worktree management — Python port of the former bash recipes.

Linked working trees that share one ``.git`` directory. Each worktree is an
independent checkout at ``../<repo>-<name>`` with its own branch, index, and
files. Rationale: different PRs need different cargo ``[patch]`` graphs, and
switching a worktree is ``cd``, not a rebuild storm.

Commands (dispatched on ``sys.argv[0]``, mirroring the mock-* pattern)::

    celestia-devtools worktree-create <name> [base=dev]   # + cargo patch re-register
    celestia-devtools worktree-remove <name>              # delete worktree + prune
    git worktree list                                     # (linewise recipe)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=capture,
        text=True,
    )


def _repo_name() -> str | None:
    top = _git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    return Path(top.stdout.strip()).name


def _create(name: str, base: str) -> int:
    repo = _repo_name()
    if repo is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    target = f"../{repo}-{name}"
    if Path(target).is_dir():
        print(f"worktree already exists: {target}")
        return 0
    # Ensure the base branch exists locally (fetch if only on remote).
    if _git("rev-parse", "--verify", f"refs/heads/{base}").returncode != 0 and _git(
        "rev-parse", "--verify", f"refs/remotes/origin/{base}"
    ).returncode != 0:
        print(f"branch '{base}' not found locally or on origin", file=sys.stderr)
        return 1
    print(f"creating worktree {target} from {base}")
    added = _git("worktree", "add", "-b", name, target, f"origin/{base}")
    if added.returncode != 0:
        added = _git("worktree", "add", target, base)
    if added.returncode != 0:
        print(added.stderr, file=sys.stderr)
        return 1
    print(f"worktree ready: {target}")
    # Register cargo patches for the new directory, best-effort.
    subprocess.run(
        [sys.executable, "-m", "celestia_devtools", "register-patches"],
        cwd=Path(target),
    )
    return 0


def _remove(name: str) -> int:
    repo = _repo_name()
    if repo is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 1
    target = f"../{repo}-{name}"
    if not Path(target).is_dir():
        print(f"worktree not found: {target}", file=sys.stderr)
        return 1
    removed = _git("worktree", "remove", target, "--force")
    if removed.returncode != 0:
        print(removed.stderr, file=sys.stderr)
        return 1
    _git("worktree", "prune")
    print(f"removed worktree: {target}")
    return 0


def main() -> int:
    cmd = Path(sys.argv[0]).stem
    args = sys.argv[1:]
    if cmd == "worktree-create":
        if not args:
            print("usage: celestia-devtools worktree-create <name> [base=dev]", file=sys.stderr)
            return 2
        return _create(args[0], args[1] if len(args) > 1 else "dev")
    if cmd == "worktree-remove":
        if not args:
            print("usage: celestia-devtools worktree-remove <name>", file=sys.stderr)
            return 2
        return _remove(args[0])
    print(f"error: unknown worktree command '{cmd}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
