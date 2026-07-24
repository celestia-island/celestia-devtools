#!/usr/bin/env python3
"""npx precompiled-package generator for celestia-island Rust crates.

Given a cross-compiled binary for one Rust target, stage a complete two-layer
npm package layout under ``--out-dir``:

    <out>/
      package.json              ← root (@scope/name)
      install.js                ← generated host-selector + launcher
      <name>-<platform>/        ← one subpackage per generated invocation
        package.json
        <binary>[.exe]

A full publishable root (with ``optionalDependencies`` on *every* platform) is
assembled incrementally: each call with ``--rust-target <T>`` writes that
target's subpackage and *regenerates* the root ``package.json`` listing every
subpackage directory it finds under ``<out>``. So the typical CI flow is:

    # one job per target:
    celestia-devtools npm-dist --name shirabe --rust-target x86_64-unknown-linux-gnu \
        --binary path/to/shirabe --out-dir dist
    # ... (macos, windows jobs likewise) ...

    # final job (no binary — just (re)assemble the root across all subpkgs):
    celestia-devtools npm-dist --name shirabe --out-dir dist

Local dry-run: ``just npm-dist`` runs the same generator with no publish.

Exit codes:
    0 — package(s) staged successfully
    1 — invalid arguments or staging failure
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from celestia_devtools.core import logger
from celestia_devtools.npm.packager import (
    SHIM_FILENAME,
    generate_postinstall_shim,
    generate_root_package,
    generate_subpackage,
)
from celestia_devtools.npm.platforms import PLATFORMS, find_platform

#: Default npm scope for celestia-island (matches @celestia-island/tairitsu-web-glue).
DEFAULT_SCOPE = "celestia-island"


# ── Logging helpers ──────────────────────────────────────────────────────────


def _ok(msg: str) -> None:
    logger.ok(msg)


def _info(msg: str) -> None:
    logger.info(msg)


def _warn(msg: str) -> None:
    logger.warn(msg)


def _err(msg: str) -> None:
    logger.error(msg)


# ── Staging ──────────────────────────────────────────────────────────────────


def _chmod_exec(path: Path) -> None:
    """Make a file executable (POSIX only; no-op on Windows)."""
    if os.name == "nt":
        return
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _discover_platforms(out_dir: Path, scope: str, name: str) -> list:
    """Reconstruct the platform set from already-staged subpackage dirs.

    The root manifest must list *every* platform that will be published, but
    each per-target CI job only knows about its own binary. To assemble the
    full set incrementally we look at which subpackage directories already
    exist under ``out_dir`` and map their suffix back to a Platform.
    """
    discovered = []
    seen = set()
    # Subpackage dirs are named <name><npm_suffix>. Scan out_dir's children.
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        cname = child.name
        if cname == name or not cname.startswith(name):
            continue
        suffix = cname[len(name):]
        plat = next((p for p in PLATFORMS if p.npm_suffix == suffix), None)
        if plat is None or plat.rust_target in seen:
            continue
        seen.add(plat.rust_target)
        discovered.append(plat)
    # Stable order for reproducible output.
    discovered.sort(key=lambda p: p.rust_target)
    return discovered


def stage_subpackage(
    *,
    out_dir: Path,
    scope: str,
    name: str,
    version: str,
    binary_name: str,
    binary_path: Path,
    platform,
    description: str,
    license: str,
    repository: str | None,
) -> Path:
    """Stage one platform subpackage: ``<out>/<name><suffix>/{package.json,binary}``."""
    sub_dir = out_dir / f"{name}{platform.npm_suffix}"
    sub_dir.mkdir(parents=True, exist_ok=True)

    leaf = f"{binary_name}.exe" if platform.node_os == "win32" else binary_name
    dest = sub_dir / leaf
    shutil.copy2(binary_path, dest)
    _chmod_exec(dest)

    pkg = generate_subpackage(
        scope=scope,
        name=name,
        version=version,
        platform=platform,
        binary=binary_name,
        description=description,
        license=license,
        repository=repository,
    )
    _write_json(sub_dir / "package.json", pkg)
    return sub_dir


def write_root(
    *,
    out_dir: Path,
    scope: str,
    name: str,
    version: str,
    binary_name: str,
    platforms,
    description: str,
    license: str,
    repository: str | None,
    homepage: str | None,
) -> Path:
    """(Re)write the root ``package.json`` + selector shim covering all platforms."""
    root_pkg = generate_root_package(
        scope=scope,
        name=name,
        version=version,
        platforms=platforms,
        binary=binary_name,
        description=description,
        license=license,
        repository=repository,
        homepage=homepage,
    )
    _write_json(out_dir / "package.json", root_pkg)

    shim = generate_postinstall_shim(
        scope=scope, name=name, binary=binary_name, platforms=platforms,
    )
    shim_path = out_dir / SHIM_FILENAME
    shim_path.write_text(shim, encoding="utf-8")
    _chmod_exec(shim_path)
    return out_dir / "package.json"


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="npm-dist",
        description="Stage npx precompiled npm packages for a Rust binary.",
    )
    parser.add_argument("--name", required=True, help="Package leaf name (e.g. shirabe)")
    parser.add_argument(
        "--version", default=None,
        help="Package version (default: read from package.json in --out-dir, else '0.0.0-dev')",
    )
    parser.add_argument(
        "--binary", default=None,
        help="Path to the cross-compiled binary to stage for --rust-target. "
             "Omit (with --out-dir) to only (re)assemble the root package.",
    )
    parser.add_argument(
        "--binary-name", default=None,
        help="Leaf filename of the binary inside subpackages (default: same as --name)",
    )
    parser.add_argument(
        "--rust-target", default=None,
        help="Rust target triple of --binary (e.g. x86_64-unknown-linux-gnu). "
             "Required when --binary is given.",
    )
    parser.add_argument(
        "--out-dir", default="dist",
        help="Output directory (default: ./dist)",
    )
    parser.add_argument(
        "--scope", default=DEFAULT_SCOPE,
        help=f"npm scope without @ (default: {DEFAULT_SCOPE})",
    )
    parser.add_argument("--description", default="", help="Package description")
    parser.add_argument("--license", default="SySL-1.0", help="SPDX license (default: SySL-1.0)")
    parser.add_argument("--repository", default=None, help="Repository URL")
    parser.add_argument("--homepage", default=None, help="Homepage URL")
    args = parser.parse_args()

    if args.binary and not args.rust_target:
        _err("--rust-target is required when --binary is given")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve version: explicit > existing root package.json > dev placeholder.
    version = args.version
    if version is None:
        existing = out_dir / "package.json"
        if existing.exists():
            try:
                version = json.loads(existing.read_text(encoding="utf-8")).get("version")
            except (json.JSONDecodeError, OSError):
                version = None
        version = version or "0.0.0-dev"

    binary_name = args.binary_name or args.name

    # ── Stage a platform subpackage (if a binary was provided) ───────────────
    if args.binary:
        platform = find_platform(args.rust_target)
        if platform is None:
            _err(
                f"unknown --rust-target '{args.rust_target}'. Known targets: "
                + ", ".join(p.rust_target for p in PLATFORMS)
            )
            return 1
        binary_path = Path(args.binary)
        if not binary_path.is_file():
            _err(f"binary not found: {binary_path}")
            return 1
        sub_dir = stage_subpackage(
            out_dir=out_dir,
            scope=args.scope,
            name=args.name,
            version=version,
            binary_name=binary_name,
            binary_path=binary_path,
            platform=platform,
            description=args.description,
            license=args.license,
            repository=args.repository,
        )
        _ok(f"staged {args.scope}/{args.name}{platform.npm_suffix} @ {version} → {sub_dir}")

    # ── (Re)assemble the root package across all discovered subpackages ──────
    platforms = _discover_platforms(out_dir, args.scope, args.name)
    write_root(
        out_dir=out_dir,
        scope=args.scope,
        name=args.name,
        version=version,
        binary_name=binary_name,
        platforms=platforms,
        description=args.description,
        license=args.license,
        repository=args.repository,
        homepage=args.homepage,
    )
    if platforms:
        _ok(
            f"root {args.scope}/{args.name} @ {version} → {out_dir} "
            f"(platforms: {', '.join(p.rust_target for p in platforms)})"
        )
    else:
        _warn(
            f"root {args.scope}/{args.name} @ {version} written with no platform "
            f"subpackages yet — re-run with --binary for each target."
        )
    _info("next: cd %s && npm pack --dry-run   # verify; publish from CI" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
