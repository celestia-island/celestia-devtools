"""
Mock server orchestration — lifecycle management and service discovery.

Provides:
- per-repo mock server scripts (discovery via devtools subprocess)
- justfile recipes: `mock-start`, `mock-stop`, `mock-status`
- `celestia-devtools registry` — JSON output of all mock addresses

Mock servers query devtools for peer locations instead of using a shared
file.  devtools already knows every repo's location via its scan logic
(shared with `register-patches` and `locate`).

Mock server hierarchy (bottom-up):
  arona (LLM) → entelecheia (scepter) → shittim-chest (webui agent activity)
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from pathlib import Path
from celestia_devtools.repo.register_patches import is_celestia_repo
import socket
from celestia_devtools.core import envelope

# ── Repo discovery (shared with register_patches) ──────────────────────────

def find_repo_root() -> Path:
    """Walk up from cwd to find the workspace root."""
    d = Path.cwd()

    while d != d.parent:
        if (d / "justfile").exists() or (d / ".git").exists():
            return d

        d = d.parent

    return Path.cwd()

def discover_sibling_repos() -> dict[str, Path]:
    """Find all celestia-island repos in the parent directory.

    Returns ``{repo_name: absolute_path}``.
    """

    own_name = is_celestia_repo(find_repo_root())
    parent = find_repo_root().parent
    repos: dict[str, Path] = {}

    # Also include the repo we're running from
    if own_name:
        repos[own_name] = find_repo_root()

    if not parent.is_dir():
        return repos

    for entry in sorted(parent.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        try:
            if not (entry / ".git").exists():
                continue
        except PermissionError:
            continue

        name = is_celestia_repo(entry)

        if name:
            repos[name] = entry

    return repos

# ── Mock addresses ──────────────────────────────────────────────────────────

# arona's mock script listens on ARONA_MOCK_PORT, whose default is 8429
# (arona/scripts/mock/server.py) — NOT 8420, the stale value this table
# carried until 2026-10-16; mock-start's peer URLs, status and stop all
# derive from here.
MOCK_PORTS: dict[str, dict] = {"arona": {"port": 8429, "protocol": "http"}, "entelecheia": {"port": 8424, "protocol": "ws"}, "shittim-chest": {"port": 8428, "protocol": "ws"}}

def discover_mocks() -> dict:
    """Build the complete mock registry from sibling repo discovery.

    Returns a dict like:
    { "arona": {"path": "/path/to/arona", "port": 8429, "url": "http://127.0.0.1:8429", "script": "/path/to/arona/scripts/mock/server.py"}, ... }
    Only includes repos that actually have a mock script.
    """
    repos = discover_sibling_repos()
    result: dict = {}

    for name, info in MOCK_PORTS.items():
        path = repos.get(name)

        if not path:
            continue

        script = path / "scripts" / "mock" / "server.py"

        if script.exists():
            result[name] = {"name": name, "path": str(path), "port": info["port"], "protocol": info["protocol"], "url": f"{info['protocol']}://127.0.0.1:{info['port']}", "script": str(script)}

    return result

# ── Lifecycle ────────────────────────────────────────────────────────────────

def start_mock(info: dict, peer_urls: dict[str, str]) -> subprocess.Popen | None:
    """Start a single mock server. Returns Popen handle or None if skipped."""
    script = Path(info["script"])

    if not script.exists():
        print(f"[mock] Skipping {info['name']}: script not found at {script}")
        return None

    env = os.environ.copy()

    # Fallbacks derive from MOCK_PORTS so the table stays the single
    # source of truth — the 8420 arona fallback silently pointed peers at
    # a port the mock server never listens on.
    def _fallback(name: str) -> str:
        info = MOCK_PORTS[name]
        return f"{info['protocol']}://127.0.0.1:{info['port']}"

    env["ARONA_MOCK_URL"]  = peer_urls.get("arona", _fallback("arona"))
    env["ENTE_MOCK_URL"]   = peer_urls.get("entelecheia", _fallback("entelecheia"))
    env["CHEST_MOCK_URL"]  = peer_urls.get("shittim-chest", _fallback("shittim-chest"))

    proc = subprocess.Popen([sys.executable, "-u", str(script)], env = env, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
    print(f"[mock] Started {info['name']} (pid={proc.pid}) on port {info['port']}")
    return proc

def start_all() -> list[subprocess.Popen]:
    """Start all mocks in dependency order."""
    mocks = discover_mocks()

    if not mocks:
        print("[mock] No mock scripts found. Ensure repos are checked out.")
        return []

    # Build peer URL map for env vars
    peer_urls = {name: m["url"] for name, m in mocks.items()}

    procs: list[subprocess.Popen] = []

    for name in ["arona", "entelecheia", "shittim-chest"]:
        info = mocks.get(name)

        if info:
            proc = start_mock(info, peer_urls)

            if proc:
                procs.append(proc)
                time.sleep(1)  # allow server to bind

    return procs

def stop_all() -> None:
    """Kill all mock server processes found in the process list."""
    killed = 0

    for _, info in MOCK_PORTS.items():
        code = subprocess.run(["fuser", "-k", f"{info['port']}/tcp"], capture_output = True, timeout = 5).returncode

        if code == 0:
            killed += 1

    print(f"[mock] Stopped {killed} mock server(s)")

def status() -> None:
    """Print status of all mock servers."""
    mocks = discover_mocks()

    if not mocks:
        print("[mock] No mock scripts found")
        return

    for name, info in mocks.items():
        port = info["port"]
        alive = _port_listening(port)
        print(f"[mock] {name}: {info['url']} ({'running' if alive else 'stopped'})")

def _port_listening(port: int) -> bool:
    """Check if a TCP port is in LISTEN state."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)

    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False

def registry() -> str:
    """Return the full mock registry as a JSON string."""
    mocks = discover_mocks()

    # Add URL-only entries for mocks that aren't locally available but known
    for name, info in MOCK_PORTS.items():
        if name not in mocks:
            mocks[name] = {"name": name, "port": info["port"], "protocol": info["protocol"], "url": f"{info['protocol']}://127.0.0.1:{info['port']}"}

    return envelope.render(mocks)

# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    cmd = sys.argv[0]

    if cmd == "mock-start":
        procs = start_all()

        if not procs:
            print("[mock] No mock servers started.")
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
    elif cmd == "registry":
        print(registry(), end = "")
    else:
        print(f"Unknown mock command: {cmd}", file = sys.stderr)
        return 1

    return 0
