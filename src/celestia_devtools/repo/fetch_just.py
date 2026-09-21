#!/usr/bin/env python3
"""Stage the bundled ``common.just`` into ``.just/`` — the ``just fetch`` core.

The org-wide ``fetch`` recipe is linewise and shell-neutral (valid under both
POSIX sh and Windows PowerShell). Its "local bundle" branch used to be a bash
``command -v``/``cp`` pipeline; that logic lives here now so no recipe in the
chain needs bash, a ``.sh`` or a ``.ps1`` helper.

Usage::

    celestia-devtools fetch-just [--name celestia-devtools.just] [--force]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from celestia_devtools.core import logger
from celestia_devtools.repo.init import DEST_DIR, LINK_NAME, common_just_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage the bundled common.just into .just/ (fetch core)"
    )
    parser.add_argument(
        "--name", default=LINK_NAME,
        help=f"output filename inside {DEST_DIR}/ (default: {LINK_NAME})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite even if the staged copy is already current",
    )
    args = parser.parse_args()

    src = common_just_path()
    if not src.is_file():
        logger.error(f"common.just not found at {src}")
        return 1

    dest_dir = Path.cwd() / DEST_DIR
    dest = dest_dir / args.name
    dest_dir.mkdir(parents=True, exist_ok=True)

    if dest.is_file() and not args.force:
        current = dest.read_text(encoding="utf-8")
        bundled = src.read_text(encoding="utf-8")
        if current == bundled:
            logger.info(f"{DEST_DIR}/{args.name} already up to date")
            return 0
        logger.warn(f"{DEST_DIR}/{args.name} drifted — refreshing")
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    logger.ok(f"staged {src} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
