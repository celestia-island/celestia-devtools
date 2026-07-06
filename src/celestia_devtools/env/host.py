#!/usr/bin/env python3
"""Host platform detection and low-level WSL subprocess primitives.

Shared across the entire celestia ecosystem (aris, kei, entelecheia, …).
This module has **no project-specific dependencies** — it answers two
questions:

  * ``detect_host_kind()`` — am I on Windows, inside WSL2, on native
    Linux, or on macOS?
  * ``host_machine()`` — what CPU architecture am I on (normalised)?

Plus the low-level ``wsl.exe`` invocation helpers that the other
``env/`` modules (``wsl_select``, ``wsl_exec``, ``wsl``) build on.

Usage::

    from celestia_devtools.env import host
    if host.detect_host_kind() == "windows":
        ...
    arch = host.host_machine()
"""
from __future__ import annotations

import platform
import subprocess
import sys
from typing import Literal

HostKind = Literal["windows", "wsl", "linux", "macos"]

_host_kind: HostKind | None = None


# ── Host detection ───────────────────────────────────────────────────────────

def detect_host_kind() -> HostKind:
    """Return the host kind, caching the result.

    Distinguishes WSL2 from native Linux via the ``microsoft-standard-WSL``
    signature in ``/proc/version``.
    """
    global _host_kind
    if _host_kind is not None:
        return _host_kind

    if sys.platform == "win32":
        _host_kind = "windows"
        return "windows"
    if sys.platform == "darwin":
        _host_kind = "macos"
        return "macos"
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/version", "r", encoding="utf-8", errors="replace") as f:
                version_line = f.read()
            if "microsoft-standard-WSL2" in version_line or \
               "microsoft-standard-WSL" in version_line:
                _host_kind = "wsl"
                return "wsl"
        except OSError:
            pass
        _host_kind = "linux"
        return "linux"
    _host_kind = "linux"
    return "linux"


def host_machine() -> str:
    """Cross-platform ``uname -m`` replacement.

    ``os.uname()`` is Linux/macOS only and raises on native Windows; this
    wraps ``platform.machine()`` which works everywhere. Normalises the
    common aliases (``amd64``→``x86_64``, ``arm64``→``aarch64``, …).
    """
    machine = platform.machine().lower()
    if machine in ("x86", "i386", "i486", "i586", "i686"):
        return "x86_64" if sys.maxsize > 2**32 else "i686"
    if machine in ("amd64", "x64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return machine


# ── WSL subprocess primitives ────────────────────────────────────────────────

def decode_wsl_output(raw: bytes) -> str:
    """Decode ``wsl.exe`` stdout, handling the UTF-16LE / UTF-8 split.

    WSL's own management commands (``-l``, ``--status``) emit UTF-16LE;
    commands that shell into a distro (``-d X -- ...``) emit plain UTF-8.
    We sniff for a UTF-16LE BOM or interleaved NULs and fall back to UTF-8.
    """
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16-le", errors="ignore")
    if len(raw) >= 8 and raw.count(b"\x00") > len(raw) // 4:
        text = raw.decode("utf-16-le", errors="ignore")
        if text.strip():
            return text
    return raw.decode("utf-8", errors="ignore")


def run_wsl(args: list[str], timeout: float = 15.0) -> str:
    """Run ``wsl.exe`` with *args* (management commands) and return decoded stdout."""
    try:
        r = subprocess.run(
            ["wsl.exe", *args],
            capture_output=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return decode_wsl_output(r.stdout or b"")


def run_wsl_shell(
    distro: str, script: str, *, timeout: float = 30.0, login: bool = True,
) -> str:
    """Run a bash *script* inside *distro*, returning decoded stdout.

    The script is fed via **stdin** (``bash -l`` with no ``-c``), which is
    the reliable channel across the Windows→WSL command-line boundary.
    Passing the script as ``bash -lc "<script>"`` is fragile: WSL's interop
    re-parses the command line and shell metacharacters like ``$var`` get
    eaten. stdin sidesteps that entirely and preserves CJK + quoting.

    ``login=True`` sources ``~/.profile`` / ``~/.bashrc`` so user-installed
    tools (e.g. ``~/.cargo/bin``) are on PATH.
    """
    cmd = ["wsl.exe", "-d", distro, "--", "bash"]
    if login:
        cmd.append("-l")
    try:
        r = subprocess.run(
            cmd, input=script.encode("utf-8"),
            capture_output=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return decode_wsl_output(r.stdout or b"")


def shq_for_bash(tok: str) -> str:
    """Single-quote a string for safe embedding in a bash script."""
    return "'" + tok.replace("'", "'\\''") + "'"
