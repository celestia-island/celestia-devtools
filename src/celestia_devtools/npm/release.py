#!/usr/bin/env python3
"""Publish staged npm packages — Python port of the former bash
``[script] npm-release`` recipe in common.just.

Publishes from ``./dist`` (the :mod:`celestia_devtools.npm.dist` output
directory). Intended for CI; the npm auth token comes from the environment
(``NPM_TOKEN`` / pre-configured ``.npmrc``).

Usage::

    celestia-devtools npm-release [PACKAGE.tar.gz ...]

With no arguments the root package in ``--dist-dir`` is published; with
arguments each staged tarball is published in order. The dist-tag defaults
to ``latest`` and is overridden with ``$NPM_DIST_TAG``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="npm-release",
        description="Publish staged npm packages from ./dist (NPM_TOKEN required).",
    )
    parser.add_argument(
        "packages", nargs="*",
        help="staged tarballs to publish (default: the root package in --dist-dir)",
    )
    parser.add_argument(
        "--dist-dir", default="dist",
        help="staging directory (default: ./dist)",
    )
    parser.add_argument(
        "--access", default="public",
        help="npm --access value (default: public)",
    )
    args = parser.parse_args()

    dist = Path(args.dist_dir)
    if not dist.is_dir():
        print(f"error: dist dir not found: {dist}", file=sys.stderr)
        return 1

    tag = os.environ.get("NPM_DIST_TAG", "latest")
    if not args.packages:
        targets: list[list[str]] = [["--access", args.access, "--tag", tag]]
    else:
        targets = [[pkg, "--access", args.access, "--tag", tag] for pkg in args.packages]

    for target in targets:
        completed = subprocess.run(["npm", "publish", *target], cwd=dist)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
