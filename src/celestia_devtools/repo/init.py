#!/usr/bin/env python3
"""Vendor common.just into a repo so it survives git push/clone.

``celestia-devtools init`` copies the bundled ``common.just`` into the repo
root as ``celestia-devtools.just`` — a **real file**, not a symlink.  Commit
it to git; every clone gets it for free.

Usage::

    celestia-devtools init             # copy (or refresh if drifted) + install hooks
    celestia-devtools init --force     # overwrite even if identical
    celestia-devtools init --check     # CI gate: exit 1 if drifted
    celestia-devtools init --no-hooks  # skip automatic commit-msg hook install
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from celestia_devtools import __version__
from celestia_devtools.core import logger

LINK_NAME = "celestia-devtools.just"
_VERSION_RE = re.compile(r"^#\s*Version:\s*(\S+)", re.MULTILINE)


def common_just_path() -> Path:
    """Return the filesystem path to the bundled ``common.just``."""
    try:
        from importlib.resources import files

        return Path(str(files("celestia_devtools") / "common.just"))
    except Exception:
        import celestia_devtools

        return Path(celestia_devtools.__file__).resolve().parent / "common.just"


def _read_version(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vendor celestia-devtools justfile recipes into a repo"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite even if the existing copy is identical",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="report drift without writing (exit 1 if update needed)",
    )
    parser.add_argument(
        "--name", default=LINK_NAME,
        help=f"output filename (default: {LINK_NAME})",
    )
    parser.add_argument(
        "--no-hooks", action="store_true",
        help="skip automatic commit-msg hook install",
    )
    args = parser.parse_args()

    src = common_just_path()
    if not src.is_file():
        logger.error(f"common.just not found at {src}")
        return 1

    bundled = src.read_text(encoding="utf-8")
    dest = Path.cwd() / args.name

    # ── --check: CI drift gate ────────────────────────────
    if args.check:
        if not dest.is_file():
            logger.error(f"{args.name} missing — run: celestia-devtools init")
            return 1
        current = dest.read_text(encoding="utf-8")
        if current == bundled:
            logger.ok(f"{args.name} is up to date (v{__version__})")
            return 0
        cur_ver = _read_version(current)
        logger.warn(
            f"{args.name} drifted"
            + (f" (local v{cur_ver}" if cur_ver else "")
            + f" ≠ bundled v{__version__}) — run: celestia-devtools init"
        )
        return 1

    # ── Copy / refresh ────────────────────────────────────
    if dest.is_file():
        current = dest.read_text(encoding="utf-8")
        if current == bundled and not args.force:
            logger.info(f"{args.name} already up to date (v{__version__})")
            _check_justfile_import(args.name)
            return 0
        if not args.force:
            cur_ver = _read_version(current)
            logger.warn(
                f"{args.name} is outdated"
                + (f" (local v{cur_ver}" if cur_ver else "")
                + f" ≠ v{__version__}) — updating"
            )
    else:
        logger.info(f"creating {args.name}")

    dest.write_text(bundled, encoding="utf-8")
    logger.ok(f"vendored {args.name} (v{__version__}) — commit this file to git")

    _check_justfile_import(args.name)

    # ── Auto-install commit-msg hook ────────────────────────────────
    if not args.no_hooks:
        from celestia_devtools.vcs.hook import install_hook

        hooks_dir = Path.cwd() / ".git" / "hooks"
        if not hooks_dir.is_dir():
            logger.info("not a git repository — skipping hook install")
        else:
            hook_path = hooks_dir / "commit-msg"
            try:
                install_hook(hook_path, devtools_bin=None, force=args.force)
                logger.ok(f"installed commit-msg hook → {hook_path}")
            except SystemExit:
                logger.warn("hook install skipped (use --force to overwrite, --no-hooks to suppress)")

    return 0


def _check_justfile_import(name: str) -> None:
    """Print a hint if the repo's justfile doesn't import the vendored file."""
    justfile = Path.cwd() / "justfile"
    import_line = f'import "./{name}"'
    if not justfile.is_file():
        logger.info("no justfile found — create one starting with:")
        print(f"\n    {import_line}\n")
        return
    content = justfile.read_text(errors="replace")
    if import_line in content:
        logger.info(f"justfile already imports {name}")
    else:
        logger.info("add this line near the top of your justfile:")
        print(f"\n    {import_line}\n")


if __name__ == "__main__":
    raise SystemExit(main())
