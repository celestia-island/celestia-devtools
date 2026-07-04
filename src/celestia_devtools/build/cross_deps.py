#!/usr/bin/env python3
"""Cross-compilation prerequisite checker.

Checks and auto-installs the tools needed for ``cargo zigbuild`` cross-compilation:
  - rustup target (e.g. aarch64-unknown-linux-gnu)
  - ziglang pip package (provides the zig C cross-compiler)
  - cargo-zigbuild pip package (cargo subcommand wrapping zig cc)

Missing components are installed automatically when possible.
Rustup mirror is auto-selected: TUNA for CN locale, official otherwise.
Override with ``RUSTUP_MIRROR`` env var.

Exit codes:
    0  — all prerequisites satisfied
    1  — one or more prerequisites could not be auto-installed

Usage::

    celestia-devtools check-cross-deps --target aarch64-unknown-linux-gnu
    check-cross-deps --target aarch64-unknown-linux-gnu --features full
"""

from __future__ import annotations

import argparse
import locale
import os
import shutil
import subprocess
import sys

from celestia_devtools.core import logger

# ── Mirror auto-detection ─────────────────────────────────────────────────────

_TUNA_DIST = "https://mirrors.tuna.tsinghua.edu.cn/rustup"
_TUNA_ROOT = "https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"


def _detect_rustup_mirror() -> tuple[str | None, str | None]:
    """Return (dist_server, update_root) — None for official mirrors."""
    if env := os.environ.get("RUSTUP_MIRROR", "").strip():
        if env.lower() in ("tuna", "cn", "china"):
            return _TUNA_DIST, _TUNA_ROOT
        # Custom: assume the env value is the dist server URL
        return env, env + "/rustup"

    # Auto-detect: CN locale → TUNA
    try:
        loc = locale.getdefaultlocale()[0] or ""
        if loc.startswith("zh_"):
            return _TUNA_DIST, _TUNA_ROOT
    except Exception:
        pass

    return None, None


# ── Logging helpers ──────────────────────────────────────────────────────────


def _ok(msg: str) -> None:
    logger.ok(msg)


def _warn(msg: str) -> None:
    logger.warn(msg)


def _err(msg: str) -> None:
    logger.error(msg)


def _info(msg: str) -> None:
    logger.info(msg)


# ── Checks ────────────────────────────────────────────────────────────────────


def check_rustup_target(target: str) -> bool:
    """Ensure the Rust target std is installed via rustup."""
    result = subprocess.run(
        ["rustup", "target", "list", "--installed"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _err("rustup not found. Install from https://rustup.rs")
        return False

    installed = result.stdout.strip().splitlines()
    if target in installed:
        _ok(f"Rust target '{target}' is installed")
        return True

    _info(f"Installing Rust target '{target}'...")
    env = os.environ.copy()
    dist, root = _detect_rustup_mirror()
    if dist:
        env.setdefault("RUSTUP_DIST_SERVER", dist)
        env.setdefault("RUSTUP_UPDATE_ROOT", root)

    result = subprocess.run(["rustup", "target", "add", target], env=env)
    if result.returncode == 0:
        _ok(f"Rust target '{target}' installed")
        return True

    _err(f"Failed to install Rust target '{target}'")
    return False


def _pip_install(package: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def check_pip_package(package: str, import_name: str | None = None) -> bool:
    """Check if a pip package is importable; install if missing."""
    import_name = import_name or package
    result = subprocess.run(
        [sys.executable, "-c", f"import {import_name}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        _ok(f"{package} is installed")
        return True

    _info(f"Installing {package} via pip...")
    if _pip_install(package):
        _ok(f"{package} installed")
        return True

    _err(f"Failed to install {package}")
    return False


def check_cargo_zigbuild() -> bool:
    """Ensure cargo-zigbuild CLI is available."""
    if shutil.which("cargo-zigbuild"):
        _ok("cargo-zigbuild is available")
        return True

    result = subprocess.run(
        [sys.executable, "-m", "cargo_zigbuild", "--help"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        _ok("cargo-zigbuild is available (via python -m)")
        return True

    _info("Installing cargo-zigbuild via pip...")
    if _pip_install("cargo-zigbuild"):
        _ok("cargo-zigbuild installed")
        return True

    _err("Failed to install cargo-zigbuild")
    return False


def check_openssl_needs(features: str) -> bool:
    """Warn if a feature set likely requires OpenSSL (libssl-dev)."""
    openssl_features = {"opcua"}
    active = set(features.replace(",", " ").split())
    overlap = active & openssl_features

    if not overlap:
        _ok("No OpenSSL dependency (rustls only)")
        return True

    _warn(f"Features {overlap} may require OpenSSL (libssl-dev).")
    _warn("For libssl-free builds, omit these features.")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check and auto-install cross-compilation prerequisites"
    )
    parser.add_argument(
        "--target",
        default="aarch64-unknown-linux-gnu",
        help="Rust target triple (default: aarch64-unknown-linux-gnu)",
    )
    parser.add_argument(
        "--features",
        default="full",
        help="Cargo features being built (default: full)",
    )
    args = parser.parse_args()

    all_ok = True

    all_ok = check_rustup_target(args.target) and all_ok
    all_ok = check_pip_package("ziglang", "ziglang") and all_ok
    all_ok = check_cargo_zigbuild() and all_ok
    check_openssl_needs(args.features)

    if all_ok:
        _ok("All prerequisites satisfied.")
        return 0

    _err("Some prerequisites could not be auto-installed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
