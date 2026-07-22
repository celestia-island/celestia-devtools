#!/usr/bin/env python3
"""Daemon lifecycle management for celestia dev services.

Provides systemd-based (Linux) service management with optional mock server
integration.  On Windows, falls back to WSL2 celestia dev container.

Usage:
    celestia-devtools daemon install [--mock] [repo]
    celestia-devtools daemon uninstall [repo]
    celestia-devtools daemon restart [--mock] [repo]
    celestia-devtools daemon status [repo]

Repo is auto-detected from cwd.  Supported repos: shittim-chest, entelecheia, arona.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

def _find_free_port(start: int) -> int:
    """Find the first free TCP port starting from *start*."""
    port = start
    for _ in range(100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            port += 1
        finally:
            s.close()
    raise RuntimeError(f"no free port found starting from {start}")


def _start_pglite() -> str:
    """Start PGlite and return the DATABASE_URL connection string."""
    import json as _json
    result = subprocess.run(
        [sys.executable, "-m", "celestia_devtools", "pglite", "start", "--force"],
        capture_output=True, text=True, timeout=30,
    )
    # Read the URL from the instance file
    instance_file = Path.home() / ".local" / "share" / "celestia" / "pglite" / "pglite-instance.json"
    if instance_file.exists():
        data = _json.loads(instance_file.read_text())
        url = data.get("url", "")
        if url:
            return url
    # Fallback: try parsing from output
    for line in result.stdout.split("\n") + result.stderr.split("\n"):
        if "postgres://" in line or "postgresql://" in line:
            return line.strip()
    return ""


# ── Service definitions ──────────────────────────────────────────────────────

# Each service knows what binary to run, what features to build, what ports,
# and which mock servers it depends on.

SERVICE_DEFS: dict[str, dict] = {
    "shittim-chest": {
        "bin": "chest",
        "package": "core",
        "features": ["mock-mode"],
        "build_cmd": ["cargo", "build", "-p", "core", "--bin", "chest",
                      "--features", "mock-mode", "--release"],
        "run_port": 8400,
        "health_port": 8400,
        "args": [],  # chest binary runs without subcommand
        "env_defaults": {
            "SHITTIM_CHEST_HOST": "127.0.0.1",
            "RUST_LOG": "warn,chest=info",
        },
        "mock_deps": ["arona", "entelecheia"],
    },
    "entelecheia": {
        "bin": "scepter",
        "package": "scepter",
        "features": ["embedded-db"],
        "build_cmd": ["cargo", "build", "-p", "scepter",
                      "--features", "embedded-db", "--release"],
        "run_port": 8410,
        "health_port": 8410,
        "args": [],
        "env_defaults": {
            "RUST_LOG": "warn,scepter=info",
        },
        "mock_deps": ["arona"],
    },
    "arona": {
        "bin": "_cli",
        "package": "_cli",
        "features": [],
        "build_cmd": ["cargo", "build", "-p", "_cli", "--release"],
        "run_port": 8405,
        "health_port": 8405,
        "args": ["serve"],  # arona binary requires `serve` subcommand
        "env_defaults": {
            "MOCK_MODE": "1",
            "RUST_LOG": "warn,arona=info",
        },
        "mock_deps": [],
    },
}

# Mock port assignments (must match scripts/mock/server.py in each repo)
MOCK_PORTS: dict[str, dict] = {
    "arona":        {"port": 8429, "protocol": "http"},
    "entelecheia":  {"port": 8424, "protocol": "ws"},
    "shittim-chest": {"port": 8428, "protocol": "ws"},
}

MOCK_ENV_MAP = {
    "arona": "ARONA_MOCK_URL",
    "entelecheia": "ENTE_MOCK_URL",
}


def detect_repo() -> Optional[str]:
    """Detect which celestia repo we are in from cwd."""
    from celestia_devtools.repo.register_patches import is_celestia_repo
    d = Path.cwd()
    while d != d.parent:
        if (d / "Cargo.toml").exists() or (d / ".git").exists():
            name = is_celestia_repo(d)
            if name in SERVICE_DEFS:
                return name
        d = d.parent
    return None


# ── Systemd helpers ──────────────────────────────────────────────────────────

def _is_user_session() -> bool:
    """True if running inside a systemd user session (XDG_RUNTIME_DIR + systemctl --user works)."""
    try:
        subprocess.run(
            ["systemctl", "--user", "list-units"],
            capture_output=True, timeout=5,
        ).check_returncode()
        return True
    except Exception:
        return False


def _systemd_user_dir() -> Path:
    """~/.config/systemd/user/ — parent dirs created if needed."""
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _systemd_unit_name(repo: str) -> str:
    return f"celestia-{repo}.service"


def _generate_unit(repo: str, work_dir: Path, env: dict[str, str], binary: str,
                   args: list[str], port: int) -> str:
    """Generate a systemd user unit."""
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in sorted(env.items()))
    return f"""[Unit]
Description=Celestia {repo} dev daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={work_dir}
{env_lines}
ExecStart={binary} {" ".join(args)}
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _enable_linger() -> None:
    """Ensure linger is enabled for the current user (keeps user services alive)."""
    try:
        subprocess.run(["loginctl", "enable-linger"], capture_output=True, timeout=10)
    except Exception:
        pass


# ── Mock lifecycle ────────────────────────────────────────────────────────────

def _start_mock_servers(repo: str) -> dict[str, int]:
    """Start mock servers that *repo* depends on. Returns {name: pid}."""
    svc = SERVICE_DEFS.get(repo, {})
    needed = svc.get("mock_deps", [])

    # Discover mock scripts
    from celestia_devtools.core.mock import discover_mocks
    mocks = discover_mocks()
    peer_urls: dict[str, str] = {name: m["url"] for name, m in mocks.items()}

    procs: dict[str, int] = {}
    # arona first (if needed), then entelecheia, then shittim-chest (if self-mock)
    for name in ["arona", "entelecheia", "shittim-chest"]:
        if name not in needed and name != repo:
            continue
        info = mocks.get(name)
        if not info:
            print(f"[daemon] mock {name} not found locally")
            continue
        script = Path(info["script"])
        if not script.exists():
            print(f"[daemon] mock script not found: {script}")
            continue

        env = os.environ.copy()
        env["ARONA_MOCK_URL"] = peer_urls.get("arona", "http://127.0.0.1:8429")
        env["ENTE_MOCK_URL"] = peer_urls.get("entelecheia", "ws://127.0.0.1:8424")
        env["CHEST_MOCK_URL"] = peer_urls.get("shittim-chest", "ws://127.0.0.1:8428")

        proc = subprocess.Popen(
            [sys.executable, "-u", str(script)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs[name] = proc.pid
        print(f"[daemon] started mock {name} (pid={proc.pid}) on port {info['port']}")
        time.sleep(1)
    return procs


def _stop_mock_servers() -> None:
    """Kill all mock server processes by port."""
    for name, info in MOCK_PORTS.items():
        try:
            subprocess.run(
                ["fuser", "-k", f"{info['port']}/tcp"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────────────

def cmd_install(repo: str, with_mock: bool = False, no_build: bool = False) -> int:
    """Install (build + create systemd unit + enable + start) the daemon."""
    svc = SERVICE_DEFS[repo]
    work_dir = Path.cwd()

    if platform.system() == "Windows":
        print("[daemon] Windows detected — using WSL2 celestia dev container")
        return _wsl2_install(repo, with_mock)

    if not _is_user_session():
        print("[daemon] error: systemd user session not available", file=sys.stderr)
        print("[daemon] On WSL, ensure systemd is enabled: [boot] systemd=true in /etc/wsl.conf", file=sys.stderr)
        return 1

    # 1. Build (skip if --no-build)
    binary = work_dir / "target" / "release" / svc["bin"]
    if no_build:
        if not binary.is_file():
            print(f"[daemon] --no-build: binary not found at {binary}", file=sys.stderr)
            return 1
        print(f"[daemon] skipping build (--no-build), using {binary}")
    else:
        print(f"[daemon] building {repo}...")
        build_cmd = svc["build_cmd"]
        result = subprocess.run(build_cmd, cwd=work_dir)
        if result.returncode != 0:
            print(f"[daemon] build failed (exit {result.returncode})", file=sys.stderr)
            return result.returncode
        if not binary.is_file():
            print(f"[daemon] binary not found after build: {binary}", file=sys.stderr)
            return 1

    # 3. Start PGlite (needed by chest/arona)
    db_url = _start_pglite()
    if db_url:
        print(f"[daemon] PGlite ready: {db_url}")

    # 4. Find free port
    port = _find_free_port(svc["run_port"])
    if port != svc["run_port"]:
        print(f"[daemon] port {svc['run_port']} occupied, using {port}")

    # 4. Build env dict: defaults + overrides from environment
    svc = SERVICE_DEFS[repo]
    env: dict[str, str] = dict(svc.get("env_defaults", {}))
    if db_url:
        env["DATABASE_URL"] = db_url
    if "JWT_SECRET" not in os.environ:
        env.setdefault("JWT_SECRET", "dev-secret-not-for-production-use-only-32chars")
    if repo == "shittim-chest":
        env.setdefault("SHITTIM_CHEST_ENCRYPTION_KEY", os.environ.get(
            "SHITTIM_CHEST_ENCRYPTION_KEY", "dev-encryption-key-not-for-prod-32ch"))
        env.setdefault("SHITTIM_CHEST_PORT", str(port))
    env.setdefault("RUST_LOG", "warn,chest=info" if repo == "shittim-chest" else "warn,arona=info")
    if with_mock:
        _start_mock_servers(repo)
        from celestia_devtools.core.mock import discover_mocks
        mocks = discover_mocks()
        for name, key in MOCK_ENV_MAP.items():
            if name in mocks:
                env[key] = mocks[name]["url"]

    # 5. Generate systemd unit
    unit_name = _systemd_unit_name(repo)
    unit_path = _systemd_user_dir() / unit_name
    unit_text = _generate_unit(repo, work_dir, env, str(binary),
                                svc.get("args", []), port)
    unit_path.write_text(unit_text)
    print(f"[daemon] wrote unit: {unit_path}")

    # 5. Enable linger
    _enable_linger()

    # 6. Reload, enable, start
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", unit_name], check=True)
    subprocess.run(["systemctl", "--user", "restart", unit_name], check=True)
    print(f"[daemon] service {unit_name} installed and started")
    return 0


def cmd_uninstall(repo: str) -> int:
    """Stop and remove the daemon."""
    unit_name = _systemd_unit_name(repo)
    unit_path = _systemd_user_dir() / unit_name

    subprocess.run(["systemctl", "--user", "stop", unit_name], capture_output=True)
    subprocess.run(["systemctl", "--user", "disable", unit_name], capture_output=True)
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    _stop_mock_servers()
    print(f"[daemon] service {unit_name} removed")
    return 0


def cmd_restart(repo: str, with_mock: bool = False) -> int:
    """Rebuild + restart."""
    svc = SERVICE_DEFS[repo]
    work_dir = Path.cwd()

    print(f"[daemon] rebuilding {repo}...")
    result = subprocess.run(svc["build_cmd"], cwd=work_dir)
    if result.returncode != 0:
        print(f"[daemon] build failed (exit {result.returncode})", file=sys.stderr)
        return result.returncode

    # Stop old mock servers, start new ones if needed
    if with_mock:
        _stop_mock_servers()
        time.sleep(1)
        _start_mock_servers(repo)

    unit_name = _systemd_unit_name(repo)
    subprocess.run(["systemctl", "--user", "restart", unit_name], check=True)
    print(f"[daemon] service {unit_name} restarted")
    return 0


def cmd_status(repo: str) -> int:
    """Show daemon + mock status."""
    unit_name = _systemd_unit_name(repo)
    print(f"── {repo} daemon ──")
    result = subprocess.run(
        ["systemctl", "--user", "status", unit_name],
        capture_output=True, text=True,
    )
    print(result.stdout or result.stderr)

    print(f"\n── mock servers ──")
    try:
        subprocess.run(
            [sys.executable, "-m", "celestia_devtools", "mock-status"],
            check=True,
        )
    except subprocess.CalledProcessError:
        pass
    return 0


def _wsl2_install(repo: str, with_mock: bool) -> int:
    """Windows fallback: use WSL2 celestia debug container."""
    # Check if WSL2 is available
    wsl_check = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, text=True)
    distros = [d.strip() for d in wsl_check.stdout.split("\n") if d.strip()]
    target = None
    for d in distros:
        if "celestia" in d.lower() or "ubuntu" in d.lower():
            target = d
            break
    if not target:
        print("[daemon] No WSL2 distro found. Install Ubuntu from Microsoft Store.", file=sys.stderr)
        return 1

    print(f"[daemon] using WSL2 distro: {target}")
    # Forward to linux daemon install inside WSL2
    wsl_cmd = ["wsl", "-d", target, "--", "bash", "-c",
               f"cd $(wslpath '{Path.cwd().as_posix()}') && "
               f"python3 -m celestia_devtools daemon install {repo}"
               + (" --mock" if with_mock else "")]
    return subprocess.run(wsl_cmd).returncode


# ── CLI dispatch ─────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print(__doc__)
        return 0

    cmd = args[0]
    rest = args[1:]

    with_mock = "--mock" in rest
    no_build = "--no-build" in rest
    rest = [a for a in rest if a not in ("--mock", "--no-build")]

    repo = rest[0] if rest else detect_repo()
    if not repo:
        print("[daemon] error: cannot detect repo from cwd", file=sys.stderr)
        print("[daemon] usage: celestia-devtools daemon <install|uninstall|restart|status> [repo]", file=sys.stderr)
        return 1

    if repo not in SERVICE_DEFS:
        print(f"[daemon] error: unsupported repo '{repo}'", file=sys.stderr)
        print(f"[daemon] supported: {', '.join(SERVICE_DEFS)}", file=sys.stderr)
        return 1

    if cmd == "install":
        return cmd_install(repo, with_mock, no_build)
    elif cmd == "uninstall":
        return cmd_uninstall(repo)
    elif cmd == "restart":
        return cmd_restart(repo, with_mock)
    elif cmd == "status":
        return cmd_status(repo)
    else:
        print(f"[daemon] unknown command: {cmd}", file=sys.stderr)
        print("[daemon] usage: daemon <install|uninstall|restart|status> [--mock] [repo]", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
