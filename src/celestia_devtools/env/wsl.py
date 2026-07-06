#!/usr/bin/env python3
"""Ensure a shared WSL2 dev distro exists for the celestia ecosystem.

Creates and provisions a single persistent WSL2 Ubuntu 24.04 distro named
``celestia-dev`` (overridable via ``--distro``) — the same pattern Docker
Desktop uses. All celestia repos (aris, kei, entelecheia, shittim-chest)
share ONE distro so the global cargo registry cache (~/.cargo/registry) is
reused across projects instead of re-fetching every dependency per repo.

The distro is created from the official Ubuntu WSL rootfs tarball via
``wsl --import`` (clean, reproducible, no interactive user setup), then
provisioned non-interactively with:

  * build essentials (build-essential, pkg-config, libssl-dev, curl, …)
  * rustup + stable toolchain
  * just (via cargo)
  * docker (via get.docker.com, skippable with --no-docker)

Idempotent: ``wsl-ensure`` run repeatedly is safe — if the distro exists
and all tools are present, it exits immediately. ``--reset`` destroys and
rebuilds it (nuclear option for a corrupted environment).

Usage::

    celestia-devtools wsl-ensure                 # create/verify (idempotent)
    celestia-devtools wsl-ensure --reset         # destroy + recreate
    celestia-devtools wsl-ensure --no-docker     # skip docker
    celestia-devtools wsl-ensure --force         # re-provision even if present
    celestia-devtools wsl-ensure --distro my-distro
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from celestia_devtools.core import logger as _logger

DEFAULT_DISTRO = "celestia-dev"
DEFAULT_ROOTFS_URL = (
    "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/"
    "ubuntu-noble-wsl-amd64-24.04lts.rootfs.tar.gz"
)

# Build-time packages installed via apt before rust/docker. libssl-dev covers
# the most common native dep (openssl); pkg-config + curl are universally
# needed. ca-certificates ensures HTTPS for rustup / get.docker.com.
# nodejs + npm back the shared PGlite facility (env/pglite.py) — entelecheia
# and shittim-chest both use it for temporary embedded-Postgres instances.
APT_PACKAGES = [
    "build-essential",
    "pkg-config",
    "libssl-dev",
    "curl",
    "ca-certificates",
    "git",
    "nodejs",
    "npm",
]


def _log(level: str, msg: str) -> None:
    getattr(_logger, level, _logger.info)(msg)


def _is_windows() -> bool:
    return os.name == "nt"


def _local_data_dir() -> Path:
    """%LOCALAPPDATA%\\celestia on Windows, ~/.local/share/celestia elsewhere."""
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "celestia"


def _rootfs_cache_path(url: str) -> Path:
    """Cache the downloaded tarball by URL hash so different rootfs versions
    coexist and a re-import (e.g. after --reset) doesn't re-download."""
    cache_dir = _local_data_dir() / "wsl-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return cache_dir / f"rootfs-{digest}.tar.gz"


def _vhd_dir(distro: str) -> Path:
    d = _local_data_dir() / f"wsl-{distro}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── WSL invocation helpers ────────────────────────────────────────────

# Reuse the battle-tested decoder from the shared host module instead of
# maintaining a second heuristic here.
from celestia_devtools.env.host import decode_wsl_output as _decode_wsl_output  # noqa: E402


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing bytes and decoding defensively (wsl.exe is
    UTF-16LE on Windows). The returned CompletedProcess has .stdout/.stderr
    as str, decoded via _decode_wsl_output."""
    _log("info", f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, **kw)
    proc.stdout = _decode_wsl_output(proc.stdout)
    proc.stderr = _decode_wsl_output(proc.stderr)
    return proc


def _wsl_available() -> bool:
    """True if the wsl.exe binary responds to --status (WSL2 installed)."""
    if not _is_windows():
        return False
    r = _run(["wsl", "--status"])
    if r.returncode != 0:
        _log("error", f"WSL not available: {r.stderr.strip()}")
        return False
    return True


def _distro_exists(distro: str) -> bool:
    """True if `distro` is already registered with WSL."""
    if not _is_windows():
        return False
    r = _run(["wsl", "--list", "--quiet"])
    if r.returncode != 0:
        return False
    return distro in r.stdout


def _run_in_distro(distro: str, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bash command inside the distro as root (wsl --import defaults
    to root). Uses -lc so /etc/profile (which sources cargo env) applies."""
    full = ["wsl", "-d", distro, "--", "bash", "-lc", cmd]
    r = _run(full)
    if check and r.returncode != 0:
        _log("error", f"command failed in {distro}: {cmd}\n{r.stdout}\n{r.stderr}")
    return r


# ── Provisioning steps ────────────────────────────────────────────────


def _download_rootfs(url: str) -> Path:
    """Download the rootfs tarball to the cache, skipping if already present
    and non-empty. Streams to disk to avoid loading 340MB into memory."""
    dest = _rootfs_cache_path(url)
    if dest.exists() and dest.stat().st_size > 1024 * 1024:  # >1MiB = likely complete
        _log("info", f"rootfs cache hit: {dest}")
        return dest

    _log("info", f"downloading rootfs (~340MB) from {url} ...")
    tmp = dest.with_suffix(".part")
    tmp.unlink(missing_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "celestia-devtools"})
        with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 1024)  # 1MiB chunks
        tmp.replace(dest)
        _log("info", f"rootfs cached: {dest} ({dest.stat().st_size // (1024*1024)} MiB)")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        _log("error", f"rootfs download failed: {e}")
        raise
    return dest


def _import_distro(distro: str, rootfs_tar: Path) -> bool:
    """wsl --import <name> <vhd_dir> <tar> --version 2. Returns True on success."""
    vhd = _vhd_dir(distro)
    r = _run(["wsl", "--import", distro, str(vhd), str(rootfs_tar), "--version", "2"])
    if r.returncode != 0:
        _log("error", f"wsl --import failed: {r.stderr.strip()}")
        return False
    _log("info", f"distro '{distro}' imported (VHD at {vhd})")
    return True


def _enable_systemd(distro: str) -> None:
    """Write /etc/wsl.conf with systemd=true so docker can run as a service.
    Requires a distro restart (wsl --terminate) to take effect — caller does that."""
    conf = (
        "[boot]\nsystemd=true\n\n"
        "[automount]\n options=metadata,umask=22,fmask=11\n\n"
        "[interop]\nenabled=true\nappendWindowsPath=true\n"
    )
    # Heredoc via tee to avoid quoting hell across wsl/bash layers.
    _run_in_distro(distro, f"tee /etc/wsl.conf > /dev/null <<'WSLCONF'\n{conf}WSLCONF")
    _log("info", "wsl.conf written (systemd enabled); will take effect after restart")


def _ensure_mirrored_networking() -> None:
    """Configure %UserProfile%\\.wslconfig with networkingMode=mirrored so the
    distro shares the host's network stack (no NAT isolation). Lets a service
    bound inside the distro (e.g. PGlite on 127.0.0.1:PORT) be reachable from
    Windows host code, and vice-versa — essential for the dev loop where the
    Tauri client (Windows) talks to a backend (WSL2) on localhost.

    Idempotent: merges the [wsl2] networkingMode key without clobbering other
    settings the user may have. Only runs on Windows."""
    if not _is_windows():
        return
    home = Path(os.path.expanduser("~"))
    wslconfig = home / ".wslconfig"
    desired = "networkingMode=mirrored"

    existing = ""
    if wslconfig.exists():
        try:
            existing = wslconfig.read_text()
        except OSError:
            pass

    if desired in existing:
        return  # already set — nothing to do

    # Append or merge into [wsl2]. If there's no [wsl2] section, add one.
    if "[wsl2]" in existing:
        # Insert the key right after the [wsl2] header (first occurrence).
        idx = existing.index("[wsl2]") + len("[wsl2]")
        updated = existing[:idx] + f"\n{desired}" + existing[idx:]
    else:
        sep = "\n\n" if existing.strip() and not existing.endswith("\n\n") else ""
        updated = (existing.rstrip("\n") + sep + "[wsl2]\n" + desired + "\n")
    try:
        wslconfig.write_text(updated)
        _log("info", f"set {desired} in {wslconfig} (host<->distro port sharing; restart WSL to apply)")
    except OSError as e:
        _log("warn", f"could not write .wslconfig: {e}")


def _provision_apt(distro: str) -> bool:
    """Install build essentials non-interactively."""
    _log("info", "installing apt packages (build-essential, pkg-config, libssl-dev, ...) ...")
    env = "DEBIAN_FRONTEND=noninteractive"
    r = _run_in_distro(
        distro,
        f"{env} apt-get update -qq && {env} apt-get install -y -qq {' '.join(APT_PACKAGES)}",
        check=False,
    )
    if r.returncode != 0:
        _log("error", f"apt install failed:\n{r.stdout}\n{r.stderr}")
        return False
    return True


def _provision_rust(distro: str) -> bool:
    """Install rustup + stable toolchain non-interactively."""
    _log("info", "installing rustup + stable toolchain ...")
    r = _run_in_distro(
        distro,
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal",
        check=False,
    )
    if r.returncode != 0:
        _log("error", f"rustup install failed:\n{r.stdout}\n{r.stderr}")
        return False
    return True


def _provision_just(distro: str) -> bool:
    """Install just via cargo (the version in apt is often outdated)."""
    _log("info", "installing just (via cargo) ...")
    r = _run_in_distro(distro, "source $HOME/.cargo/env && cargo install -q just", check=False)
    if r.returncode != 0:
        _log("warn", f"cargo install just failed:\n{r.stdout}\n{r.stderr}")
        return False
    return True


def _provision_docker(distro: str) -> bool:
    """Install docker via the official get.docker.com script + add root to the
    docker group so docker commands work without sudo."""
    _log("info", "installing docker (get.docker.com) ...")
    r = _run_in_distro(
        distro,
        "curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh",
        check=False,
    )
    if r.returncode != 0:
        _log("warn", f"docker install failed (use --no-docker or Docker Desktop):\n{r.stdout}\n{r.stderr}")
        return False
    # Enable the service (needs systemd, which _enable_systemd + restart set up).
    _run_in_distro(distro, "systemctl enable docker.socket 2>/dev/null || true", check=False)
    _run_in_distro(distro, "usermod -aG docker root 2>/dev/null || true", check=False)
    return True


def _verify_tools(distro: str, want_docker: bool) -> dict[str, bool]:
    """Check which tools are present in the distro. Returns {name: present}."""
    checks = {
        "cargo": "cargo --version",
        "just": "just --version",
    }
    if want_docker:
        checks["docker"] = "docker --version"
    result = {}
    for name, cmd in checks.items():
        r = _run_in_distro(distro, cmd, check=False)
        result[name] = r.returncode == 0
    return result


def _provision(distro: str, want_docker: bool) -> bool:
    """Full provisioning sequence. Returns True if all critical steps succeed
    (docker failure is non-fatal — warns and continues)."""
    ok = True
    ok &= _provision_apt(distro)
    ok &= _provision_rust(distro)
    if not _provision_just(distro):
        # just is critical for the `just dev --mock` flow; treat as failure.
        ok = False
    if want_docker:
        _provision_docker(distro)  # non-fatal on failure
    return ok


# ── Top-level orchestration ───────────────────────────────────────────


def ensure(
    distro: str = DEFAULT_DISTRO,
    rootfs_url: str = DEFAULT_ROOTFS_URL,
    *,
    force: bool = False,
    no_docker: bool = False,
    reset: bool = False,
) -> int:
    want_docker = not no_docker

    if not _is_windows():
        _log("error", "wsl-ensure is only supported on Windows (it drives wsl.exe).")
        return 2

    if not _wsl_available():
        _log("error", "WSL2 is not installed. Run 'wsl --install --no-distribution' first.")
        return 2

    if reset:
        if _distro_exists(distro):
            _log("info", f"--reset: unregistering '{distro}' (all caches lost) ...")
            _run(["wsl", "--unregister", distro])
        else:
            _log("info", f"--reset: '{distro}' not registered, nothing to remove.")

    if _distro_exists(distro) and not force:
        tools = _verify_tools(distro, want_docker)
        missing = [t for t, ok in tools.items() if not ok]
        if not missing:
            _log("info", f"✓ distro '{distro}' exists and all tools present — nothing to do.")
            return 0
        _log("info", f"distro '{distro}' exists but missing: {missing} — re-provisioning ...")
        if not _provision(distro, want_docker):
            _log("error", "re-provisioning failed.")
            return 1
        _log("info", "✓ re-provisioning complete.")
        return 0

    if force and _distro_exists(distro):
        _log("info", f"--force: re-provisioning '{distro}' ...")
        if not _provision(distro, want_docker):
            return 1
        _log("info", "✓ re-provisioning complete.")
        return 0

    # Fresh creation path.
    _log("info", f"creating new distro '{distro}' ...")
    try:
        rootfs = _download_rootfs(rootfs_url)
    except Exception:
        return 1

    if not _import_distro(distro, rootfs):
        return 1

    # Enable systemd before provisioning so docker's service can run.
    _enable_systemd(distro)
    _ensure_mirrored_networking()
    _log("info", "restarting distro to activate systemd + mirrored networking ...")
    _run(["wsl", "--terminate", distro])

    if not _provision(distro, want_docker):
        _log("error", "provisioning failed — distro was created but may be incomplete.")
        _log("error", f"re-run `celestia-devtools wsl-ensure --distro {distro}` to retry.")
        return 1

    tools = _verify_tools(distro, want_docker)
    _log("info", f"✓ distro '{distro}' ready. Tools: {tools}")
    _log("info", f"  enter with:  wsl -d {distro}")
    _log("info", "  build with:  just wsl-run cargo build  (or: just wsl-shell)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="celestia-devtools wsl-ensure",
        description=(
            "Ensure a shared WSL2 dev distro exists for the celestia ecosystem "
            "(creates + provisions rust/just/docker from the official Ubuntu "
            "rootfs if absent)."
        ),
    )
    p.add_argument("--distro", default=DEFAULT_DISTRO, help=f"distro name (default: {DEFAULT_DISTRO})")
    p.add_argument("--rootfs", default=DEFAULT_ROOTFS_URL, help="rootfs tarball URL (default: Ubuntu 24.04)")
    p.add_argument("--force", action="store_true", help="re-provision even if distro exists")
    p.add_argument("--no-docker", action="store_true", help="skip docker (use Docker Desktop integration)")
    p.add_argument("--reset", action="store_true", help="destroy + recreate the distro (clears all caches)")
    args = p.parse_args()

    return ensure(
        distro=args.distro,
        rootfs_url=args.rootfs,
        force=args.force,
        no_docker=args.no_docker,
        reset=args.reset,
    )


if __name__ == "__main__":
    sys.exit(main())
