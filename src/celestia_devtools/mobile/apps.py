"""Per-app mobile build adapters (the registry the CLI drives).

v1 posture (2026-10-31):

* ``wowsp-android`` — WRAPS the repo's own ``just _build-android-apk`` flow
  (the one working Android pipeline in the family); the facility's value is
  the shared ASCII ``CARGO_TARGET_DIR`` resolution, the NDK-trap check, the
  toolchain audit, and artifact collection — not a reimplementation of the
  justfile.
* ``chest-android`` — EXPERIMENTAL: the tauri_mobile package is an
  unscaffolded stub (no ``gen/android``); the adapter runs the frontend
  prebuild then ``cargo tauri android build`` and fails with a scaffold
  hint when the gen tree is missing.
* ``chest-harmony`` — EXPERIMENTAL: pure ArkTS/hvigor pipeline, no Rust;
  the adapter locates ``hvigorw`` and runs ``assembleHap``; the
  command-line-tools version is unpinned upstream (recorded in the ledger).

Signing is deliberately OUT of scope for v1: every repo's release signing
material is absent by design, and provisioning keystores/certs is a
credential-handling decision (workspace red line) that must not be
smuggled into a build tool.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ascii_path import resolve_ascii_cargo_target
from .toolchain import doctor, format_report, ndk_trap_lines


@dataclass(frozen=True)
class MobileApp:
    """One buildable mobile target and how to drive it."""

    key: str
    repo: str
    workspace: str
    kind: str  # "tauri-android" | "harmony"
    experimental: bool
    frontend_prebuild: tuple[str, ...] = ()
    build_command: tuple[str, ...] = ()
    artifact_globs: tuple[str, ...] = ()


APPS: dict[str, MobileApp] = {
    "wowsp-android": MobileApp(
        key="wowsp-android",
        repo="wowsp",
        workspace="/mnt/codespace/wowsp",
        kind="tauri-android",
        experimental=False,
        # The justfile's _build-android-apk recipe already runs
        # fetch_models/extract_gameparams, sets WOWSP_MOBILE_BUNDLE and the
        # ASCII CARGO_TARGET_DIR (its own resolver — same candidate order
        # as ours); the adapter adds the shared audit + env on top.
        build_command=("just", "build", "android-apk"),
        artifact_globs=("packages/app/tauri/gen/android/app/build/outputs/apk/**/*.apk",),
    ),
    "chest-android": MobileApp(
        key="chest-android",
        repo="shittim-chest",
        workspace="/mnt/codespace/shittim-chest",
        kind="tauri-android",
        experimental=True,
        # No beforeBuildCommand in tauri_mobile's conf: the webui dist must
        # be built the way the desktop flow does it.
        frontend_prebuild=("just", "_build-webui"),
        build_command=(
            "cargo",
            "tauri",
            "android",
            "build",
            "--apk",
            "--target",
            "aarch64",
        ),
        artifact_globs=("packages/app/tauri_mobile/gen/android/app/build/outputs/apk/**/*.apk",),
    ),
    "chest-harmony": MobileApp(
        key="chest-harmony",
        repo="shittim-chest",
        workspace="/mnt/codespace/shittim-chest",
        kind="harmony",
        experimental=True,
        build_command=("hvigorw", "assembleHap", "--mode", "module", "-p", "product=default"),
        artifact_globs=("packages/app/harmony/entry/build/**/*.hap",),
    ),
}


def _scaffold_hint(app: MobileApp) -> str:
    if app.kind == "tauri-android":
        return (
            f"{app.repo}'s mobile package has no gen/android scaffold — run "
            "`cargo tauri android init` inside packages/app/tauri_mobile "
            "(or adopt the wowsp scaffold as the family template) first"
        )
    return f"{app.key}: toolchain scaffolding not present"


def _run(cmd: tuple[str, ...], cwd: Path, env: dict[str, str]) -> int:
    printable = " ".join(cmd)
    print(f"[mobile] {printable}  (cwd={cwd})")
    return subprocess.call(cmd, cwd=str(cwd), env=env)


def build(app_key: str, repo_root: Path | None = None, dry_run: bool = False) -> int:
    """Drive one app's mobile build. Returns the process exit code."""
    app = APPS.get(app_key)
    if app is None:
        print(f"unknown app {app_key!r}; known: {', '.join(sorted(APPS))}")
        return 2

    workspace = Path(repo_root or app.workspace)
    if not workspace.is_dir():
        print(f"workspace not found: {workspace}")
        return 2

    if app.experimental:
        print(f"[mobile] {app.key} is EXPERIMENTAL — failures here are expected to be loud")

    if app.kind == "tauri-android":
        report = doctor(workspace)
        print(format_report(report))
        for line in ndk_trap_lines(report):
            print(f"[mobile] refusing to continue: {line}")
            return 1
        if not report.ok_for_android():
            print("[mobile] toolchain not ready (see doctor output above)")
            return 1

    if app.kind == "harmony" and not shutil.which("hvigorw"):
        print(_scaffold_hint(app))
        return 1

    if app.kind == "tauri-android":
        gen = (
            workspace / "packages/app/tauri_mobile/gen/android"
            if app.repo == "shittim-chest"
            else workspace / "packages/app/tauri/gen/android"
        )
        if not gen.is_dir():
            print(_scaffold_hint(app))
            return 1

    env = dict(os.environ)
    if app.kind == "tauri-android":
        target_dir = resolve_ascii_cargo_target()
        env["CARGO_TARGET_DIR"] = str(target_dir)
        print(f"[mobile] CARGO_TARGET_DIR={target_dir} (ASCII-verified)")

    if dry_run:
        print("[mobile] dry run — would run:")
        if app.frontend_prebuild:
            print("  pre: " + " ".join(app.frontend_prebuild))
        print("  build: " + " ".join(app.build_command))
        return 0

    if app.frontend_prebuild:
        code = _run(app.frontend_prebuild, workspace, env)
        if code != 0:
            return code
    return _run(app.build_command, workspace, env)
