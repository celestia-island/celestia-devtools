#!/usr/bin/env python3
"""Container CLI resolution (docker / podman).

Shared across the celestia ecosystem. Prefers ``docker`` if a live daemon
is reachable; falls back silently to ``podman`` (which is docker-CLI
compatible). Both ``build_image.py`` and ``build_uboot.py`` use this to
avoid hard-coding ``"docker"``.

Usage::

    from celestia_devtools.env import docker
    cmd = docker.docker_cmd()  # ["docker"] or ["podman"]
    subprocess.run([*cmd, "run", "--rm", ...])
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_docker_cmd: list[str] | None = None


def docker_cmd() -> list[str]:
    """Return the container-CLI prefix to use (cached per process).

    Prefers ``docker`` if a live daemon is reachable; falls back silently
    to ``podman``. Returns ``["docker"]`` as a last resort so the caller's
    error message references docker.
    """
    global _docker_cmd
    if _docker_cmd is not None:
        return _docker_cmd

    docker_bin = shutil.which("docker")
    if docker_bin and _docker_daemon_alive(docker_bin):
        _docker_cmd = ["docker"]
        return _docker_cmd

    podman_bin = shutil.which("podman")
    if podman_bin and _podman_daemon_alive():
        _docker_cmd = ["podman"]
        return _docker_cmd

    if docker_bin:
        _docker_cmd = ["docker"]
    elif podman_bin:
        _docker_cmd = ["podman"]
    else:
        _docker_cmd = ["docker"]
    return _docker_cmd


def _docker_daemon_alive(docker_bin: str) -> bool:
    """True if ``docker info`` connects within ~3s."""
    try:
        r = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _podman_daemon_alive() -> bool:
    """True if podman can talk to its daemon/socket.

    Rootless podman uses a UNIX socket under ``$XDG_RUNTIME_DIR``; a bare
    ``podman info`` will start the service-on-demand if the socket unit is
    enabled, otherwise it errors.
    """
    try:
        r = subprocess.run(
            ["podman", "info", "--format", "{{.Host.Os}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def ensure_podman_socket() -> bool:
    """Best-effort start of a rootless podman socket (WSL/Linux).

    Returns True if podman is usable afterwards.
    """
    if not shutil.which("podman"):
        return False
    if _podman_daemon_alive():
        return True
    try:
        subprocess.run(
            ["systemctl", "--user", "start", "podman.socket"],
            capture_output=True, text=True, timeout=5,
        )
        socket_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "podman", "podman.sock",
        )
        if Path(socket_path).exists():
            os.environ["DOCKER_HOST"] = f"unix://{socket_path}"
            global _docker_cmd
            _docker_cmd = None  # invalidate cache
            return _podman_daemon_alive()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError):
        pass
    return False
