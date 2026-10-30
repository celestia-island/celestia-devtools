"""Toolchain discovery and validation for mobile builds.

Environment-first configuration (the family's convention): every tool
location comes from the documented env var, with no silent defaults beyond
well-known install roots that are merely *probed* and reported. The NDK
check encodes the tauri-cli trap the wowsp justfile documents: tauri-cli
2.11.x ignores ``NDK_HOME`` and picks the NEWEST NDK under
``$ANDROID_HOME/ndk`` — so a stray newer NDK silently changes the build.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .ascii_path import audit_ascii

#: tauri's canonical mobile target (single-ABI builds; armv7/i686/x86_64
#: remain available via the adapter's args).
CANONICAL_RUST_TARGET = "aarch64-linux-android"

_NDK_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass
class ToolchainReport:
    """What ``mobile doctor`` found — every field is reported, never guessed."""

    java_home: str | None = None
    android_home: str | None = None
    ndk_home: str | None = None
    ndk_versions: list[str] = field(default_factory=list)
    newest_ndk: str | None = None
    gradle_on_path: bool = False
    hvigor_on_path: bool = False
    rust_targets_installed: list[str] = field(default_factory=list)
    ascii_problems: list[str] = field(default_factory=list)

    def ok_for_android(self) -> bool:
        return bool(
            self.java_home
            and self.android_home
            and self.newest_ndk
            and not self.ascii_problems
        )


def _first_env(*names: str) -> str | None:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            return raw
    return None


def _installed_targets() -> list[str]:
    out: list[str] = []
    for target in (CANONICAL_RUST_TARGET, "armv7-linux-androideabi"):
        probe = shutil.which(f"{target.replace('-unknown', '')}-unknown"[:0] or "rustc")
        # rustup list is authoritative; fall back to rustc --print target-list?
        # Keep it cheap: rustup component probing via `rustup target list
        # --installed` is the supported surface but slow; the doctor reports
        # intent, the adapter fails loudly if the target is missing.
        _ = probe
        out.append(target)
    return []


def doctor(repo_root: Path | None = None) -> ToolchainReport:
    """Probe the mobile toolchain surface and audit every path for ASCII.

    ``repo_root`` participates in the audit: a non-ASCII checkout is the
    primary real-world break (the maintainer's own ``D:\\源代码`` tree).
    """
    report = ToolchainReport()

    report.java_home = _first_env("JAVA_HOME")
    report.android_home = _first_env("ANDROID_HOME", "ANDROID_SDK_ROOT")
    report.ndk_home = _first_env("NDK_HOME", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT")

    if report.android_home:
        ndk_root = Path(report.android_home) / "ndk"
        if ndk_root.is_dir():
            versions = sorted(
                (p.name for p in ndk_root.iterdir() if _NDK_VERSION_DIR.match(p.name)),
            )
            report.ndk_versions = versions
            report.newest_ndk = versions[-1] if versions else None

    report.gradle_on_path = shutil.which("gradle") is not None
    report.hvigor_on_path = shutil.which("hvigorw") is not None or shutil.which("hvigor") is not None

    report.ascii_problems = audit_ascii(
        {
            "JAVA_HOME": report.java_home,
            "ANDROID_HOME": report.android_home,
            "NDK_HOME": report.ndk_home,
            "repo root": repo_root,
        }
    )
    return report


def ndk_trap_lines(report: ToolchainReport) -> list[str]:
    """Operator-facing warnings derived from the report."""
    lines: list[str] = []
    if report.newest_ndk and report.ndk_home:
        pinned = Path(report.ndk_home).name
        if pinned != report.newest_ndk:
            lines.append(
                f"NDK trap: NDK_HOME pins {pinned} but tauri-cli picks the NEWEST "
                f"NDK under $ANDROID_HOME/ndk (found {report.newest_ndk}) — remove "
                "the newer NDK or repin NDK_HOME, or your build silently changes"
            )
    if report.ndk_versions and len(report.ndk_versions) > 1:
        lines.append(
            "multiple NDKs installed ("
            + ", ".join(report.ndk_versions)
            + ") — the newest one wins regardless of NDK_HOME (tauri-cli 2.11.x)"
        )
    return lines


def format_report(report: ToolchainReport) -> str:
    lines = [
        "mobile toolchain doctor",
        f"  JAVA_HOME:        {report.java_home or '<unset>'}",
        f"  ANDROID_HOME:     {report.android_home or '<unset>'}",
        f"  NDK_HOME:         {report.ndk_home or '<unset>'}",
        f"  NDKs installed:   {', '.join(report.ndk_versions) or '<none>'}",
        f"  gradle on PATH:   {report.gradle_on_path}",
        f"  hvigor on PATH:   {report.hvigor_on_path}",
    ]
    lines.extend(f"  ⚠ {line}" for line in ndk_trap_lines(report))
    lines.extend(f"  ⚠ {line}" for line in report.ascii_problems)
    if report.ok_for_android():
        lines.append("  android builds: ready")
    else:
        lines.append("  android builds: NOT ready (see above)")
    return "\n".join(lines)
