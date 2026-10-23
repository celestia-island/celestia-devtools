"""brand-stamp — wire the family icon into a Rust project's Windows build.

Usage::

    celestia-devtools brand-stamp --project <path> [--icon <name>]

The shared brand-icon facility (user direction 2026-10-22): ONE icon
asset lives here (`brand/icons/`); downstream repos consume it at BUILD
time — this command writes the three artifacts a Rust project needs so
its ``.exe`` carries the icon when cross-compiled for Windows:

1. ``<project>/build.rs``   — the ``winresource`` compile hook (only
   when the file does not already exist; an existing build script is
   never overwritten — the command reports and exits non-zero so the
   merge stays deliberate).
2. ``<project>/Cargo.toml`` — a ``[build-dependencies]`` entry for
   ``winresource`` (appended once; idempotent).
3. ``<project>/brand/icon.ico`` — the asset copied from this package.

Everything is idempotent: running the stamp twice changes nothing the
second time. Linux/macOS targets are untouched (winresource no-ops off
Windows targets), so one wiring serves every platform.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ICON_NAMES = ("icon", "evernight")
BUILD_RS = Path(__file__).resolve().parent / "build.rs.tmpl"

EXIT_USAGE = 2
EXIT_CONFLICT = 3


def _stamp(project: Path, icon: str) -> int:
    if not (project / "Cargo.toml").is_file():
        print(f"[brand-stamp] no Cargo.toml under {project}", file=sys.stderr)
        return EXIT_USAGE
    icons_dir = Path(__file__).resolve().parent / "icons"
    src_icon = icons_dir / f"{icon}.ico"
    if not src_icon.is_file():
        print(f"[brand-stamp] unknown icon '{icon}' (have: {ICON_NAMES})", file=sys.stderr)
        return EXIT_USAGE

    # 1. build.rs — never overwrite; OUR OWN template counts as done.
    build_rs = project / "build.rs"
    tmpl = BUILD_RS.read_text(encoding="utf-8")
    if build_rs.exists():
        if build_rs.read_text(encoding="utf-8") != tmpl:
            print(f"[brand-stamp] {build_rs} already exists — merge the winresource hook by hand")
            return EXIT_CONFLICT
        print(f"[brand-stamp] {build_rs} already stamped — left as is")
    else:
        shutil.copyfile(BUILD_RS, build_rs)
        print(f"[brand-stamp] wrote {build_rs}")

    # 2. Cargo.toml [build-dependencies] — append once.
    manifest = project / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    if "winresource" not in text:
        block = "\n[build-dependencies]\nwinresource = \"^0.1\"\n"
        text = text.rstrip("\n") + "\n" if text.endswith("\n") else text
        text += block if text.endswith("\n\n") else "\n" + block
        manifest.write_text(text, encoding="utf-8")
        print(f"[brand-stamp] added [build-dependencies] winresource to {manifest}")
    else:
        print(f"[brand-stamp] {manifest} already carries winresource — left as is")

    # 3. The icon asset next to the build hook.
    dst_dir = project / "brand"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"{icon}.ico"
    shutil.copyfile(src_icon, dst)
    print(f"[brand-stamp] copied {icon}.ico -> {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brand-stamp",
        description="Wire the family icon into a Rust project's Windows build (idempotent).",
    )
    parser.add_argument("--project", required=True, help="path to the crate root (Cargo.toml)")
    parser.add_argument("--icon", default="icon", help=f"icon name ({'/'.join(ICON_NAMES)})")
    args = parser.parse_args(argv)
    return _stamp(Path(args.project).resolve(), args.icon)


if __name__ == "__main__":
    raise SystemExit(main())
