#!/usr/bin/env python3
"""WSL2 distro enumeration, tool probing, and interactive selection.

Enumerates installed WSL2 distros, probes each for build tools (docker,
podman, qemu, cargo, …), scores them, and interactively prompts when
multiple viable candidates exist. The shared ``celestia-dev`` distro
(provisioned by ``celestia-devtools wsl-ensure``) is strongly preferred.

Usage::

    from celestia_devtools.env import wsl_select
    sel = wsl_select.select_distro()
    if sel is None:
        sys.exit(1)
    distro_name, tools = sel
"""
from __future__ import annotations

import json
import sys

from celestia_devtools.core import logger
from celestia_devtools.env import host

# The shared distro provisioned by `celestia-devtools wsl-ensure`.
# Strongly preferred over ad-hoc distros so the global cargo registry
# cache is reused across projects.
PREFERRED_DISTRO = "celestia-dev"

_selected_distro: tuple[str, dict] | None = None  # (name, tools_dict)


def list_wsl_distros() -> list[dict]:
    """Return ``[{name, state, version}]`` for installed WSL2 distros.

    Only version-2 distros (version 1 cannot run docker/qemu meaningfully).
    """
    text = host.run_wsl(["-l", "-v"])
    if not text.strip():
        return []
    distros: list[dict] = []
    header_seen = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not header_seen:
            if line.upper().startswith("NAME"):
                header_seen = True
            continue
        parts = [p for p in line.split() if p and p != "*"]
        if len(parts) < 3:
            continue
        name, state, version = parts[0], parts[1], parts[2]
        if version != "2":
            continue
        distros.append({"name": name, "state": state, "version": version})
    return distros


def probe_distro_tools(distro: str) -> dict:
    """Probe a single WSL distro for build tools.

    Uses a login shell via stdin so ``~/.cargo/bin`` / ``~/.profile`` are
    sourced. Returns a dict of ``tool_name → path`` plus scoring metadata.
    """
    probe = r'''set +e
for t in docker podman qemu-system-aarch64 cargo rustc python3 just; do
  p=$(command -v "$t" 2>/dev/null) && echo "tool:$t=$p"
done
echo "---docker-info---"
if command -v docker >/dev/null 2>&1; then
  timeout 3 docker info --format '{{.ServerVersion}}' 2>/dev/null
fi
echo "---podman-info---"
if command -v podman >/dev/null 2>&1; then
  timeout 5 podman info --format '{{.Host.Os}}' 2>/dev/null
fi
echo "---end---"
'''
    text = host.run_wsl_shell(distro, probe, timeout=30)
    tools: dict[str, str] = {}
    phase = "tools"
    for line in text.splitlines():
        s = line.strip()
        if s == "---docker-info---":
            phase = "docker"; continue
        if s == "---podman-info---":
            phase = "podman"; continue
        if s == "---end---":
            break
        if phase == "tools" and s.startswith("tool:") and "=" in s:
            kv = s[len("tool:"):]
            k, v = kv.split("=", 1)
            tools[k] = v
        elif phase == "docker" and s:
            tools["__docker_alive__"] = s
        elif phase == "podman" and s:
            tools["__podman_alive__"] = s
    score = 0
    if tools.get("__docker_alive__"):
        score = 30
    elif tools.get("podman"):
        score = 20
    elif tools.get("docker"):
        score = 15
    elif any(tools.get(k) for k in
             ("cargo", "qemu-system-aarch64", "python3", "rustc", "just")):
        score = 5
    tools["__score__"] = str(score)
    return tools


def _rank_distros() -> list[tuple[str, dict]]:
    """Return all WSL2 distros with a non-zero score, highest first.

    Preference: ``celestia-dev`` (+100), Running distros (+2), higher score.
    """
    raw = list_wsl_distros()
    if not raw:
        return []
    scored: list[tuple[str, dict, int]] = []
    for d in raw:
        tools = probe_distro_tools(d["name"])
        score = int(tools.get("__score__", "0"))
        if score == 0:
            continue
        if d["state"].lower() == "running":
            score += 2
        if d["name"] == PREFERRED_DISTRO:
            score += 100
        scored.append((d["name"], tools, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return [(name, tools) for name, tools, _ in scored]


def select_distro() -> tuple[str, dict] | None:
    """Pick the best WSL2 distro for building (cached per process).

    Returns ``(distro_name, tools_dict)`` or ``None`` if nothing usable.
    Single candidate → silent; multiple → interactive prompt.
    """
    global _selected_distro
    if _selected_distro is not None:
        return _selected_distro
    if host.detect_host_kind() != "windows":
        return None

    ranked = _rank_distros()

    if not ranked:
        logger.error("没有可用的 WSL2 构建环境")
        logger.info("扫描了以下发行版，均缺少构建工具：")
        for d in list_wsl_distros() or [{"name": "(无 WSL2 发行版)"}]:
            logger.info(f"  - {d['name']}")
        logger.info("请运行: celestia-devtools wsl-ensure")
        return None

    if len(ranked) == 1:
        name, tools = ranked[0]
        _selected_distro = (name, tools)
        _log_distro_choice(name, tools)
        return _selected_distro

    # Multiple candidates — interactive prompt.
    logger.section("选择 WSL2 构建环境")
    logger.info("检测到多个可用发行版：")
    for i, (name, tools) in enumerate(ranked, 1):
        tag = " (推荐)" if i == 1 else ""
        logger.info(f"  [{i}] {name}{tag}  — {summarise_tools(tools)}")
    try:
        choice_raw = input("输入编号 [1]: ").strip()
    except EOFError:
        choice_raw = ""
    choice = int(choice_raw) if choice_raw.isdigit() else 1
    choice = max(1, min(choice, len(ranked)))

    name, tools = ranked[choice - 1]
    _selected_distro = (name, tools)
    return _selected_distro


def summarise_tools(tools: dict) -> str:
    """One-line summary of a distro's tool inventory."""
    bits: list[str] = []
    if tools.get("__docker_alive__"):
        bits.append(f"docker {tools['__docker_alive__']}")
    elif tools.get("docker"):
        bits.append("docker(守护进程未启动)")
    if tools.get("podman"):
        if tools.get("__podman_alive__"):
            bits.append("podman")
        else:
            bits.append("podman(socket 待启)")
    for k in ("qemu-system-aarch64", "cargo", "python3", "just"):
        if tools.get(k):
            bits.append(k)
    return ", ".join(bits) if bits else "无"


def summarise_container(tools: dict) -> str:
    """One-line description of which container backend will be used."""
    if tools.get("__docker_alive__"):
        return f"docker (server {tools['__docker_alive__']})"
    if tools.get("podman"):
        if tools.get("__podman_alive__"):
            return "podman (已就绪)"
        return "podman (socket 待启，将自动拉起)"
    if tools.get("docker"):
        return "docker (守护进程未启动 — 需 sudo systemctl start docker)"
    return "无容器后端（docker/podman 均未安装）"


def _log_distro_choice(name: str, tools: dict) -> None:
    logger.ok(f"使用 WSL2 发行版：{name}")
    logger.info(f"  工具：{summarise_tools(tools)}")
    if not tools.get("__docker_alive__") and tools.get("podman") \
            and not tools.get("__podman_alive__"):
        logger.info("  podman socket 未启动，将在首次容器调用时尝试拉起")
