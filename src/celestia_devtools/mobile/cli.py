"""``celestia-devtools mobile`` — the family's unified mobile-build facility.

Usage:
    celestia-devtools mobile doctor [--repo PATH]
    celestia-devtools mobile ascii-target
    celestia-devtools mobile build <app> [--repo PATH] [--dry-run]

Subcommands:
    doctor       Probe the mobile toolchain (JDK/SDK/NDK/gradle/hvigor),
                 surface the tauri-cli NDK newest-wins trap, and audit
                 every involved path for ASCII compliance (the NDK/AGP
                 toolchain family breaks on non-English checkout paths).
    ascii-target Resolve and print the shared ASCII CARGO_TARGET_DIR
                 (wowsp-compatible candidate order).
    build        Drive one app's mobile build through its adapter:
                 wowsp-android (wraps the justfile flow with the shared
                 audit + env), chest-android / chest-harmony
                 (experimental scaffold-aware lanes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .apps import APPS, build
from .ascii_path import resolve_ascii_cargo_target
from .toolchain import doctor, format_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools mobile",
        description="Unified mobile-build facility (ASCII path infra, toolchain doctor, adapters).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="probe the mobile toolchain surface")
    p_doctor.add_argument("--repo", type=Path, default=None, help="repo root to audit")

    sub.add_parser("ascii-target", help="print the resolved ASCII CARGO_TARGET_DIR")

    p_build = sub.add_parser("build", help="drive one app's mobile build")
    p_build.add_argument("app", choices=sorted(APPS), help="which app to build")
    p_build.add_argument("--repo", type=Path, default=None, help="override the workspace root")
    p_build.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        print(format_report(doctor(args.repo)))
        return 0
    if args.command == "ascii-target":
        print(resolve_ascii_cargo_target())
        return 0
    if args.command == "build":
        return build(args.app, repo_root=args.repo, dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
