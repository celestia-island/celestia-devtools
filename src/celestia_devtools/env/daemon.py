#!/usr/bin/env python3
"""Daemon lifecycle — malkuth-based service supervision for celestia dev services.

Usage:
    celestia-devtools daemon build [repo...]
    celestia-devtools daemon generate-config <repo> [repo...] [--output path]
    celestia-devtools daemon start [--mock]
    celestia-devtools daemon status
    celestia-devtools daemon stop

No systemd required — services are supervised by `malkuth daemon --config`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# ── Service definitions ──────────────────────────────────────────────────────

SERVICE_DEFS: dict[str, dict] = {
    "shittim-chest": {
        "bin": "chest",
        "build_cmd": ["cargo", "build", "-p", "core", "--bin", "chest",
                      "--features", "mock-mode", "--release"],
        "run_port": 8400,
        "args": [],
        "env_defaults": {
            "SHITTIM_CHEST_HOST": "127.0.0.1",
            "RUST_LOG": "warn,chest=info",
        },
    },
    "entelecheia": {
        "bin": "scepter",
        "build_cmd": ["cargo", "build", "-p", "scepter",
                      "--features", "embedded-db", "--release"],
        "run_port": 8410,
        "args": [],
        "env_defaults": {
            "RUST_LOG": "warn,scepter=info",
        },
    },
    "arona": {
        "bin": "_cli",
        "build_cmd": ["cargo", "build", "-p", "_cli", "--release"],
        "run_port": 8405,
        "args": ["serve"],
        "env_defaults": {
            "MOCK_MODE": "1",
            "RUST_LOG": "warn,arona=info",
        },
    },
}

DEFAULT_CONFIG_PATH = "/etc/malkuth/services.toml"
DAEMON_PID_FILE = "/tmp/malkuth-daemon.pid"


def detect_repo() -> Optional[str]:
    from celestia_devtools.repo.register_patches import is_celestia_repo
    d = Path.cwd()
    while d != d.parent:
        if (d / "Cargo.toml").exists() or (d / ".git").exists():
            name = is_celestia_repo(d)
            if name in SERVICE_DEFS:
                return name
        d = d.parent
    return None


def _find_sibling_dir(repo: str) -> Optional[Path]:
    d = Path.cwd().parent / repo
    if d.is_dir() and (d / "Cargo.toml").exists():
        return d
    return None


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_build(repos: list[str]) -> int:
    if not repos:
        r = detect_repo()
        repos = [r] if r else []
    if not repos:
        print("[daemon] no repos specified", file=sys.stderr)
        return 1

    for repo in repos:
        if repo not in SERVICE_DEFS:
            print(f"[daemon] unsupported: {repo}", file=sys.stderr)
            continue
        svc = SERVICE_DEFS[repo]
        work_dir = _find_sibling_dir(repo) or Path.cwd()
        if not (work_dir / "Cargo.toml").exists():
            print(f"[daemon] {repo} not found at {work_dir}", file=sys.stderr)
            continue
        print(f"[daemon] building {repo}...")
        result = subprocess.run(svc["build_cmd"], cwd=work_dir)
        if result.returncode != 0:
            print(f"[daemon] {repo} build failed", file=sys.stderr)
            return result.returncode
        print(f"[daemon] {repo} built")
    return 0


def cmd_generate_config(repos: list[str], output_path: str = "") -> int:
    if not repos:
        print("[daemon] usage: generate-config <repo> [repo...] [--output path]", file=sys.stderr)
        return 1

    scan_dir = Path.cwd().parent
    services_toml = []
    header = """[daemon]
host = "127.0.0.1"
rate_limit_window_secs = 60
rate_limit_max_restarts = 5
cooldown_secs = 30
"""

    for repo in repos:
        if repo not in SERVICE_DEFS:
            print(f"[daemon] unsupported: {repo}", file=sys.stderr)
            continue
        svc = SERVICE_DEFS[repo]
        work_dir = _find_sibling_dir(repo) or Path.cwd()
        binary = work_dir / "target" / "release" / svc["bin"]
        if not binary.is_file():
            print(f"[daemon] binary not found: {binary} — run 'daemon build' first", file=sys.stderr)
            continue

        env = dict(svc.get("env_defaults", {}))
        args = svc.get("args", [])
        args_str = ", ".join(f'"{a}"' for a in args) if args else ""

        services_toml.append(f"""
[[services]]
id = "{repo}"
kind = "backend"
program = "{binary}"
{f"args = [{args_str}]" if args_str else ""}
restart_policy = "permanent"

[services.env]
{chr(10).join(f'{k} = "{v}"' for k, v in sorted(env.items()))}
""")

    full = header + "\n".join(services_toml)
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(full)
        print(f"[daemon] wrote {output_path}")
    else:
        print(full)
    return 0


def cmd_start(with_mock: bool = False, config_path: str = "") -> int:
    cfg = Path(config_path or DEFAULT_CONFIG_PATH)
    if not cfg.exists():
        print(f"[daemon] config not found: {cfg}", file=sys.stderr)
        print("[daemon] run 'generate-config' first", file=sys.stderr)
        return 1

    # Check if already running
    if _daemon_running():
        print("[daemon] already running")
        return 0

    print(f"[daemon] starting: malkuth daemon --config {cfg}")
    with open(DAEMON_PID_FILE, "w") as f:
        proc = subprocess.Popen(
            ["malkuth", "daemon", "--config", str(cfg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        f.write(str(proc.pid))
    time.sleep(2)
    if _daemon_running():
        print("[daemon] started")
    else:
        print("[daemon] failed to start — check malkuth logs", file=sys.stderr)
    return 0


def cmd_stop() -> int:
    if not _daemon_running():
        print("[daemon] not running")
        return 0
    pid = _read_pid()
    if pid:
        try:
            os.kill(pid, 15)  # SIGTERM → graceful drain
            time.sleep(2)
            if _daemon_running():
                os.kill(pid, 9)
        except ProcessLookupError:
            pass
    Path(DAEMON_PID_FILE).unlink(missing_ok=True)
    print("[daemon] stopped")
    return 0


def cmd_status() -> int:
    pid = _read_pid()
    if pid and _daemon_running():
        print(f"[daemon] running (pid={pid})")
    else:
        print("[daemon] not running")
    return 0


def _daemon_running() -> bool:
    pid = _read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> Optional[int]:
    try:
        return int(Path(DAEMON_PID_FILE).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


# ── CLI dispatch ─────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "build":
        return cmd_build(rest)
    elif cmd == "generate-config":
        output = ""
        repos = list(rest)
        if "--output" in rest:
            idx = rest.index("--output")
            if idx + 1 < len(rest):
                output = rest[idx + 1]
                repos = [r for r in rest if r not in ("--output", output)]
        return cmd_generate_config(repos, output)
    elif cmd == "start":
        with_mock = "--mock" in rest
        config = ""
        return cmd_start(with_mock, config)
    elif cmd == "stop":
        return cmd_stop()
    elif cmd == "status":
        return cmd_status()
    else:
        print(f"[daemon] unknown: {cmd}", file=sys.stderr)
        print("[daemon] commands: build | generate-config | start | stop | status", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
