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
class toolchain_report:
    """What ``mobile doctor`` found — every field is reported, never guessed."""

    java_home: str | None = None
    android_home: str | None = None
    ndk_home: str | None = None
    ndk_versions: list[str] = field(default_factory = list)
    newest_ndk: str | None = None
    gradle_on_path: bool = False
    hvigor_on_path: bool = False
    ascii_problems: list[str] = field(default_factory = list)

    def ok_for_android(self) -> bool:
        return bool(self.java_home and self.android_home and self.newest_ndk and not self.ascii_problems)

def _first_env(*names: str) -> str | None:
    for name in names:
        raw = os.environ.get(name)

        if raw:
            return raw

    return None

def doctor(repo_root: Path | None = None) -> toolchain_report:
    """Probe the mobile toolchain surface and audit every path for ASCII.

    ``repo_root`` participates in the audit: a non-ASCII checkout is the
    primary real-world break (the maintainer's own ``D:\\源代码`` tree).
    """
    report = toolchain_report()

    report.java_home = _first_env("JAVA_HOME")
    report.android_home = _first_env("ANDROID_HOME", "ANDROID_SDK_ROOT")
    report.ndk_home = _first_env("NDK_HOME", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT")

    if report.android_home:
        ndk_root = Path(report.android_home) / "ndk"

        if ndk_root.is_dir():
            def version_key(name: str) -> tuple[int, ...]:
                return tuple(int(part) for part in name.split("."))

            versions = sorted((p.name for p in ndk_root.iterdir()if _NDK_VERSION_DIR.match(p.name)), key = version_key)
            report.ndk_versions = versions
            report.newest_ndk = versions[-1] if versions else None

    report.gradle_on_path = shutil.which("gradle") is not None
    report.hvigor_on_path = (shutil.which("hvigorw") is not None) or (shutil.which("hvigor") is not None)

    report.ascii_problems = audit_ascii({"JAVA_HOME": report.java_home, "ANDROID_HOME": report.android_home, "NDK_HOME": report.ndk_home, "repo root": repo_root})
    return report

def ndk_trap_lines(report: toolchain_report) -> list[str]:
    """Operator-facing warnings derived from the report.

    Only the REAL trap warns: a pinned NDK that is not the newest (a stray
    newer NDK silently changes the build — wowsp's documented trap). A pin
    that already selects the newest is deterministic even with older
    leftovers installed, so multi-NDK alone is not a warning.
    """
    lines: list[str] = []

    if report.newest_ndk and report.ndk_home:
        pinned = Path(report.ndk_home).name

        if pinned != report.newest_ndk:
            lines.append(f"NDK trap: NDK_HOME pins {pinned} but tauri-cli picks the NEWEST " f"NDK under $ANDROID_HOME/ndk (found {report.newest_ndk}) — remove " "the newer NDK or repin NDK_HOME, or your build silently changes")

    return lines

def format_report(report: toolchain_report) -> str:
    lines = ["mobile toolchain doctor", f"  JAVA_HOME:        {report.java_home or '<unset>'}", f"  ANDROID_HOME:     {report.android_home or '<unset>'}", f"  NDK_HOME:         {report.ndk_home or '<unset>'}", f"  NDKs installed:   {', '.join(report.ndk_versions) or '<none>'}", f"  gradle on PATH:   {report.gradle_on_path}", f"  hvigor on PATH:   {report.hvigor_on_path}"]
    lines.extend(f"  ⚠ {line}" for line in ndk_trap_lines(report))
    lines.extend(f"  ⚠ {line}" for line in report.ascii_problems)

    if report.ok_for_android():
        lines.append("  android builds: ready")
    else:
        lines.append("  android builds: NOT ready (see above)")

    return "\n".join(lines)
