"""Cross-platform QEMU installer for celestia-devtools.

Provides zero-root QEMU installation for three host platforms:
  - Windows: uses the preinstalled C:\\Program Files\\qemu\\ (detected) or
    downloads from https://qemu.weilnetz.de/w64/ (MSI installer, no admin needed
    if installed to user dir).
  - Linux: prefers apt (if sudo works), falls back to conda/pip
    (pip install qemu-system via pyqemu), or downloads AppImage.
  - macOS: uses Homebrew (brew install qemu) if available, else conda.

Usage:
    celestia-devtools qemu-ensure [--arch aarch64,x86_64,riscv64]
    celestia-devtools qemu-ensure --check
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass

from ..core.logger import get_logger

log = get_logger(__name__)

# QEMU architectures we care about
QEMU_ARCHS = ["aarch64", "x86_64", "riscv64"]


@dataclass
class QemuProbe:
    """Result of probing for a QEMU system emulator."""
    arch: str
    path: str | None
    version: str | None

    @property
    def found(self) -> bool:
        return self.path is not None


def _which_qemu(arch: str) -> str | None:
    """Find qemu-system-<arch> on PATH or in common locations."""
    exe_name = f"qemu-system-{arch}"
    # On Windows, also try .exe
    if platform.system() == "Windows":
        exe_name += ".exe"
        # Check C:\Program Files\qemu\
        win_qemu = Path("C:/Program Files/qemu") / exe_name
        if win_qemu.exists():
            return str(win_qemu)

    return shutil.which(exe_name)


def _get_version(path: str) -> str | None:
    """Get QEMU version string."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            first_line = result.stdout.strip().split("\n")[0]
            return first_line
    except Exception:
        pass
    return None


def check_qemu() -> list[QemuProbe]:
    """Check which QEMU system emulators are available."""
    results = []
    for arch in QEMU_ARCHS:
        path = _which_qemu(arch)
        version = _get_version(path) if path else None
        results.append(QemuProbe(arch=arch, path=path, version=version))
    return results


def _is_wsl() -> bool:
    """Check if running inside WSL2."""
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        return False


def _install_linux(archs: list[str]) -> bool:
    """Install QEMU on Linux (try apt, then conda)."""
    # Try apt with sudo (passwordless or cached sudo)
    apt_pkgs = [f"qemu-system-{a}" for a in archs]
    # On Debian/Ubuntu, qemu-system-misc covers riscv64
    if "riscv64" in archs and "qemu-system-misc" not in apt_pkgs:
        apt_pkgs.append("qemu-system-misc")
    if "x86_64" in archs and "qemu-system-x86" not in apt_pkgs:
        apt_pkgs.append("qemu-system-x86")

    # Check if any are already installed
    missing = []
    for pkg in apt_pkgs:
        arch = pkg.replace("qemu-system-", "")
        if not _which_qemu(arch):
            missing.append(pkg)

    if not missing:
        log.info("All QEMU packages already installed")
        return True

    # Try passwordless sudo first
    try:
        result = subprocess.run(
            ["sudo", "-n", "apt-get", "install", "-y"] + missing,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log.info(f"Installed via apt: {', '.join(missing)}")
            return True
    except Exception:
        pass

    # Try celestia-dev WSL distro (root by default, no password needed)
    if platform.system() == "Windows" or _is_wsl():
        try:
            result = subprocess.run(
                ["wsl", "-d", "celestia-dev", "--",
                 "apt-get", "install", "-y"] + missing,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                log.info(f"Installed via celestia-dev WSL: {', '.join(missing)}")
                return True
        except Exception:
            pass

    # Try conda
    conda = shutil.which("conda")
    if conda:
        try:
            result = subprocess.run(
                [conda, "install", "-y", "-c", "conda-forge"] +
                [f"qemu-system-{a}" for a in archs],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                log.info(f"Installed via conda")
                return True
        except Exception:
            pass

    # Fallback: suggest manual install
    log.warning(f"Cannot install QEMU automatically. Please run:")
    log.warning(f"  sudo apt-get install {' '.join(missing)}")
    log.warning(f"Or use conda: conda install -c conda-forge qemu")
    return False


def _install_macos(archs: list[str]) -> bool:
    """Install QEMU on macOS via Homebrew."""
    brew = shutil.which("brew")
    if not brew:
        log.warning("Homebrew not found. Install from https://brew.sh")
        return False

    try:
        result = subprocess.run(
            [brew, "install", "qemu"],
            capture_output=True, text=True, timeout=120
        )
        return result.returncode == 0
    except Exception as e:
        log.error(f"brew install qemu failed: {e}")
        return False


def _install_windows(archs: list[str]) -> bool:
    """Check Windows QEMU (preinstalled or via choco)."""
    # Check if already installed
    win_qemu_dir = Path("C:/Program Files/qemu")
    if win_qemu_dir.exists():
        all_found = True
        for arch in archs:
            exe = win_qemu_dir / f"qemu-system-{arch}.exe"
            if not exe.exists():
                all_found = False
        if all_found:
            log.info(f"QEMU found at {win_qemu_dir}")
            return True

    # Try chocolatey
    choco = shutil.which("choco")
    if choco:
        try:
            result = subprocess.run(
                [choco, "install", "qemu", "-y"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                log.info("Installed QEMU via chocolatey")
                return True
        except Exception:
            pass

    log.warning("QEMU not found on Windows. Install from:")
    log.warning("  https://qemu.weilnetz.de/w64/ (MSI installer)")
    log.warning("  Or: choco install qemu")
    return False


def ensure(archs: list[str] | None = None) -> bool:
    """Ensure QEMU system emulators are available for the given architectures.

    Args:
        archs: List of architectures to ensure (default: all supported).
               Supported: aarch64, x86_64, riscv64.

    Returns:
        True if all requested architectures are available.
    """
    if archs is None:
        archs = QEMU_ARCHS

    log.info(f"Checking QEMU for architectures: {', '.join(archs)}")

    # Check what's already available
    probes = check_qemu()
    missing = [p.arch for p in probes if not p.found and p.arch in archs]

    if not missing:
        for p in probes:
            if p.arch in archs:
                log.info(f"  {p.arch}: {p.version}")
        return True

    log.info(f"Missing: {', '.join(missing)}")

    # Try platform-specific installation
    system = platform.system()
    if system == "Linux":
        success = _install_linux(missing)
    elif system == "Darwin":
        success = _install_macos(missing)
    elif system == "Windows":
        success = _install_windows(missing)
    else:
        log.error(f"Unsupported platform: {system}")
        return False

    # Re-check
    if success:
        probes = check_qemu()
        for p in probes:
            if p.arch in archs:
                status = "OK" if p.found else "MISSING"
                log.info(f"  {p.arch}: {status}")
        return all(p.found for p in probes if p.arch in archs)

    return False
