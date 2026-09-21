#!/usr/bin/env python3
"""Shared build dispatcher — the shell-neutral replacement for the old bash
``[script] _build`` recipe in common.just.

Usage::

    celestia-devtools build-dispatch <PRE> <DEV_CMD> <REL_CMD> [FLAGS...]

Positional arguments (quote multi-word commands):

    PRE      prerequisite command run first, or the literal ``:`` for none
    DEV_CMD  dev-profile command (used when ``--dev`` is passed)
    REL_CMD  release-profile command (default when ``--dev`` is absent)

Flags:

    --dev    use DEV_CMD instead of REL_CMD
    --clean  ``cargo clean`` before building

Runs the commands through :mod:`shlex` splitting and ``subprocess`` so no
shell (sh, bash, PowerShell) is involved at any point.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys


def _run(cmd: str) -> int:
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    if not argv:
        return 0
    if argv[0] == ":":
        return 0
    completed = subprocess.run(argv)
    return completed.returncode


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    pre, dev_cmd, rel_cmd = args[0], args[1], args[2]
    flags = args[3:]

    profile_release = True
    for flag in flags:
        if flag == "--dev":
            profile_release = False
        elif flag == "--clean":
            if subprocess.run(["cargo", "clean"]).returncode != 0:
                return 1

    if pre not in ("", ":"):
        if _run(pre) != 0:
            return 1
    return _run(rel_cmd if profile_release else dev_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
