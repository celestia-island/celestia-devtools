#!/usr/bin/env python3
"""WSL2 command re-execution and the main-guard entry point.

When a build script runs on Windows, the guard transparently re-execs it
inside the best available WSL2 distro (selected by ``wsl_select``) so the
rest of the script runs under Linux semantics — where docker, QEMU, and
``cargo osdk`` actually work.

The project-specific bits (which env vars to propagate, where the project
root is) are passed as parameters, so this module has zero coupling to
any particular repo.

Usage in a build script's ``main()``::

    from celestia_devtools.env import wsl_exec
    if wsl_exec.main_guard(
        project_root=Path(__file__).resolve().parent.parent,
        passthrough_env={"RUSTUP_HOME", "EVERNIGHT_ROOT", ...},
    ):
        return 0   # only reached on non-Windows; guard re-execs on Windows
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from celestia_devtools.core import logger
from celestia_devtools.env import host, wsl_select

# Default env vars propagated Windows → WSL (in addition to the
# project-supplied set). Path-like values (*_ROOT, CARGO_TARGET_DIR,
# ARCH_BUSYBOX) are translated via wslpath.
_BASE_PASSTHROUGH = {
    "RUSTUP_HOME", "CARGO_HOME", "CARGO_TARGET_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_COLOR", "TERM", "CELESTIA_DEVTOOLS_INSTALL",
    "QEMU_DISPLAY",
}

_PATH_LIKE_SUFFIXES = ("_ROOT",)
_PATH_LIKE_EXACT = {"CARGO_TARGET_DIR", "ARCH_BUSYBOX"}


def to_wsl_path(win_path: str | Path, distro: str | None = None) -> str:
    """Translate a Windows path to its /mnt/<x>/... WSL view.

    Falls back to manual ``/mnt/<drive_lower>/<path>`` if ``wslpath`` is
    unavailable. CJK characters are preserved.
    """
    s = str(win_path).replace("/", "\\")
    if distro is None:
        distros = wsl_select.list_wsl_distros()
        if not distros:
            return _wsl_path_fallback(s)
        distro = distros[0]["name"]
    script = f"wslpath -u {host.shq_for_bash(s)}\n"
    text = host.run_wsl_shell(distro, script, timeout=5)
    out_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if out_lines:
        return out_lines[-1]
    return _wsl_path_fallback(s)


def _wsl_path_fallback(win_path_backslashed: str) -> str:
    """Manual D:\\foo -> /mnt/d/foo when wslpath is unavailable."""
    s = win_path_backslashed
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:].lstrip("\\")
        return f"/mnt/{drive}/{rest.replace(chr(92), '/')}"
    return s.replace("\\", "/")


def _build_passthrough_env(
    distro: str, project_env: set[str] | None = None,
) -> dict[str, str]:
    """Build the env dict to hand to the re-exec'd WSL bash process.

    Merges the base set with ``project_env`` (project-specific vars like
    ``EVERNIGHT_ROOT``). Path-like values are translated to WSL paths.
    """
    names = _BASE_PASSTHROUGH | (project_env or set())
    env: dict[str, str] = {}
    for key in names:
        if key in os.environ and os.environ[key]:
            val = os.environ[key]
            if key.endswith(_PATH_LIKE_SUFFIXES) or key in _PATH_LIKE_EXACT:
                try:
                    val = to_wsl_path(val, distro)
                except RuntimeError:
                    pass
            env[key] = val
    return env


def reexec_in_wsl(
    argv: list[str] | None = None,
    distro: str | None = None,
    *,
    project_root: Path | None = None,
    passthrough_env: set[str] | None = None,
) -> int:
    """Re-exec the current script (or *argv*) inside a WSL2 distro.

    Returns the child process exit code via ``sys.exit``.

    ``project_root`` defaults to the parent of ``sys.argv[0]``'s directory
    (assumes the script lives under ``scripts/``). ``passthrough_env`` is
    the project-specific env var set to propagate.
    """
    if argv is None:
        script_win = Path(sys.argv[0]).resolve()
        try:
            script_wsl = to_wsl_path(script_win, distro)
        except RuntimeError:
            script_wsl = str(script_win).replace("\\", "/")
        argv = ["python3", script_wsl, *sys.argv[1:]]

    if distro is None:
        sel = wsl_select.select_distro()
        if sel is None:
            return 1
        distro = sel[0]

    passthrough = _build_passthrough_env(distro, passthrough_env)

    if project_root is None:
        project_root = Path(sys.argv[0]).resolve().parent.parent
    try:
        project_wsl = to_wsl_path(project_root, distro)
    except RuntimeError:
        project_wsl = str(project_root).replace("\\", "/")

    lines: list[str] = []
    for k, v in passthrough.items():
        lines.append(f"export {k}={host.shq_for_bash(v)}")
    lines.append(f"cd {host.shq_for_bash(project_wsl)}")
    quoted_argv = " ".join(host.shq_for_bash(t) for t in argv)
    lines.append(f"exec {quoted_argv}")
    script = "\n".join(lines) + "\n"

    logger.info(f"re-exec 进 WSL2 [{distro}]")
    logger.info(f"  工作目录：{project_wsl}")

    try:
        r = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "bash", "-l"],
            input=script.encode("utf-8"),
        )
    except FileNotFoundError:
        return 127
    return r.returncode


def main_guard(
    *,
    project_root: Path | None = None,
    passthrough_env: set[str] | None = None,
    wsl_hint: str = "",
) -> bool:
    """Entry-point guard: on Windows, re-exec into WSL and never return True.

    Call this at the top of ``main()``::

        if wsl_exec.main_guard(project_root=..., passthrough_env=...):
            return 0

    On Windows: selects a distro, re-execs, and the call never returns
    (``sys.exit`` with the child's code). Returns ``False`` on non-Windows.
    """
    if host.detect_host_kind() != "windows":
        return False

    if wsl_hint:
        logger.info(wsl_hint)
    else:
        logger.info("检测到 Windows，自动切换到 WSL2 构建环境")

    sel = wsl_select.select_distro()
    if sel is None:
        sys.exit(1)
    distro, _tools = sel
    rc = reexec_in_wsl(
        distro=distro,
        project_root=project_root,
        passthrough_env=passthrough_env,
    )
    sys.exit(rc)
