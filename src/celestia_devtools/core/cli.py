"""Unified CLI dispatch for celestia-devtools.

Usage::

    celestia-devtools <command> [args...]
    python -m celestia_devtools <command> [args...]

Commands::

    cache-guard       Manage cargo target/ disk usage (hard floor + soft sweep)
    format-markdown   Format and lint Markdown files
    prefetch          Pre-stage cargo + node dependencies for offline builds
    check-cross-deps  Check/install cross-compilation prerequisites (zigbuild)
    npm-dist          Stage npx precompiled npm packages for a Rust binary
    preflight         Check that required dev tools are present (node/rust/...)
    wsl-ensure        Ensure a shared WSL2 dev distro exists (rust/just/docker)
    pglite            Start/stop a shared temporary PGlite server (pgvector)
    serve             Shared process-supervision (ProcessManager) for dev scripts
    locate            Locate a celestia-island crate checkout
    init              Symlink common.just into a repo for justfile import
    include-path      Print the path to the bundled common.just

Each command has its own argparse interface; this dispatcher simply forwards
``argv`` so the individual ``main()`` entry points stay self-contained and
usable as standalone scripts.
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import Sequence

COMMANDS: dict[str, str] = {
    "cache-guard": "celestia_devtools.build.cache_guard",
    "format-markdown": "celestia_devtools.doc.markdown",
    "prefetch": "celestia_devtools.build.prefetch",
    "check-cross-deps": "celestia_devtools.build.cross_deps",
    "npm-dist": "celestia_devtools.npm.dist",
    "preflight": "celestia_devtools.env.preflight",
    "wsl-ensure": "celestia_devtools.env.wsl",
    "pglite": "celestia_devtools.env.pglite",
    "serve": "celestia_devtools.env.serve",
    "locate": "celestia_devtools.repo.locate",
    "init": "celestia_devtools.repo.init",
}


def _print_help(file=None) -> None:
    print(__doc__, file=file or sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    cmd, rest = args[0], args[1:]

    if cmd in ("-V", "--version"):
        from celestia_devtools import __version__

        print(f"celestia-devtools {__version__}")
        return 0

    if cmd == "include-path":
        from celestia_devtools.repo.init import common_just_path

        print(common_just_path())
        return 0

    module_path = COMMANDS.get(cmd)
    if module_path is None:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        _print_help(file=sys.stderr)
        return 2

    mod = import_module(module_path)
    entry = getattr(mod, "main", None)
    if not callable(entry):
        print(f"error: module '{cmd}' has no main()", file=sys.stderr)
        return 2

    sys.argv = [cmd, *rest]
    return int(entry() or 0)
