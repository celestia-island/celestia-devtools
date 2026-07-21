"""
Mock server orchestration — lifecycle management and service discovery.

Provides:
- per-repo mock server scripts (discovery via env vars or registry file)
- justfile recipes: `mock-start`, `mock-stop`, `mock-status`
- registry file at `.celestia/mocks.json` for cross-repo address discovery

Mock server hierarchy (bottom-up):
  arona (LLM) → entelecheia (scepter) → shittim-chest (webui agent activity)

Each mock registers itself in the registry on startup and reads peer
addresses from the same file.  celestia-devtools provides the `mock`
CLI group and common.just recipes.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ── Registry ────────────────────────────────────────────────────────────────

REGISTRY_FILE = ".celestia/mocks.json"


def registry_path(workspace_root: Optional[str] = None) -> Path:
    root = workspace_root or _find_workspace_root()
    return Path(root) / REGISTRY_FILE


def _find_workspace_root() -> str:
    """Walk up from cwd to find the workspace root (has .git or justfile)."""
    d = Path.cwd()
    while d != d.parent:
        if (d / "justfile").exists() or (d / ".git").exists():
            return str(d)
        d = d.parent
    return str(Path.cwd())


def read_registry() -> dict:
    path = registry_path()
    if path.exists():
        return json.loads(path.read_text())
    return {}


def write_registry(data: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def register_mock(name: str, port: int, protocol: str = "ws") -> None:
    """Register a mock server in the registry."""
    reg = read_registry()
    reg[name] = {
        "name": name,
        "port": port,
        "protocol": protocol,
        "url": f"{protocol}://127.0.0.1:{port}",
        "pid": os.getpid(),
    }
    write_registry(reg)


def unregister_mock(name: str) -> None:
    reg = read_registry()
    reg.pop(name, None)
    write_registry(reg)


def get_mock_url(name: str) -> Optional[str]:
    reg = read_registry()
    info = reg.get(name)
    return info["url"] if info else None


# ── Lifecycle ────────────────────────────────────────────────────────────────

MOCK_PORTS: dict[str, dict] = {
    "arona":       {"port": 8429, "protocol": "http", "repo": "../arona"},
    "entelecheia": {"port": 8424, "protocol": "ws",   "repo": "../entelecheia"},
    "shittim-chest": {"port": 8428, "protocol": "ws", "repo": "."},
}


def start_mock(workspace_root: str, name: str) -> subprocess.Popen:
    """Start a mock server by name. Returns the Popen handle."""
    info = MOCK_PORTS.get(name)
    if not info:
        raise ValueError(f"Unknown mock: {name}")

    script = Path(workspace_root) / info["repo"] / "scripts" / "mock" / "server.py"
    if not script.exists():
        raise FileNotFoundError(f"Mock script not found: {script}")

    env = os.environ.copy()
    env["ARONA_MOCK_URL"] = get_mock_url("arona") or "http://127.0.0.1:8429"
    env["ENTE_MOCK_URL"] = get_mock_url("entelecheia") or "ws://127.0.0.1:8424"

    return subprocess.Popen(
        [sys.executable, "-u", str(script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_all(workspace_root: str) -> list[subprocess.Popen]:
    """Start all mocks in dependency order. Stores PIDs in registry."""
    procs: list[subprocess.Popen] = []
    # Start bottom-up: arona → entelecheia → shittim-chest
    for name in ["arona", "entelecheia", "shittim-chest"]:
        try:
            proc = start_mock(workspace_root, name)
            procs.append(proc)
            register_mock(name, MOCK_PORTS[name]["port"], MOCK_PORTS[name]["protocol"])
            print(f"[mock] Started {name} (pid={proc.pid}) on port {MOCK_PORTS[name]['port']}")
            time.sleep(1)  # allow server to bind
        except (FileNotFoundError, ValueError) as e:
            print(f"[mock] Skipping {name}: {e}")
    return procs


def stop_all() -> None:
    """Kill all registered mocks by PID."""
    reg = read_registry()
    for name, info in reg.items():
        pid = info.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[mock] Stopped {name} (pid={pid})")
            except ProcessLookupError:
                pass
    # Clear registry
    path = registry_path()
    if path.exists():
        path.unlink()


def status() -> None:
    """Print status of all registered mocks."""
    reg = read_registry()
    if not reg:
        print("[mock] No mock servers registered")
        return
    for name, info in reg.items():
        pid = info.get("pid")
        alive = "running" if pid and _is_alive(pid) else "dead"
        print(f"[mock] {name}: {info['url']} ({alive}, pid={pid})")


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def main() -> int:
    cmd = sys.argv[0]
    root = _find_workspace_root()

    if cmd == "mock-start":
        procs = start_all(root)
        if not procs:
            print("[mock] No mock servers started. Ensure repos are checked out in sibling directories.")
            return 1
        print("[mock] All mock servers started. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_all()
    elif cmd == "mock-stop":
        stop_all()
    elif cmd == "mock-status":
        status()
    else:
        print(f"Unknown mock command: {cmd}", file=sys.stderr)
        return 1
    return 0
