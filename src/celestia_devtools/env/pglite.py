#!/usr/bin/env python3
"""Shared temporary-PGlite lifecycle facility for the celestia ecosystem.

Spawns a real embedded Postgres (PGlite WASM, served over the PG wire
protocol by @electric-sql/pglite-socket) with the pgvector extension
preloaded, and manages its lifecycle. entelecheia and shittim-chest both
consume this so their tests / mock backends get a throwaway Postgres with
zero Docker / system-postgres install, and with vector-column support
that real Postgres + pgvector would provide.

The Node packages (@electric-sql/pglite, @electric-sql/pglite-socket,
@electric-sql/pglite-pgvector) are installed into a shared module dir
under %LOCALAPPDATA%/celestia/pglite-node (Windows) or ~/.local/share/
celestia/pglite-node (elsewhere) so they're reused across projects —
exactly the caching pattern the wsl-ensure distro itself follows.

Lifecycle:
  start   → pick a free port, spawn the Node server, wait until it prints
            the ready URL, return it. Records the PID + port in a sidecar
            so `stop` can find it later.
  stop    → kill the recorded PID, clear the sidecar.
  status  → is a recorded instance still alive + listening?

Named instances:
  Multiple PGlite servers can run side by side (e.g. one database per
  service under test) by giving each a `--name`; each name gets its own
  sidecar file. The nameless invocation is the `default` instance and
  stays compatible with the pre-name sidecar location.

Usage::

    celestia-devtools pglite start                 # spawn, print DATABASE_URL
    celestia-devtools pglite start --port 5433     # pin a port
    celestia-devtools pglite start --persist /path # persistent data dir
    celestia-devtools pglite start --name arona    # a second, parallel instance
    celestia-devtools pglite stop
    celestia-devtools pglite stop --name arona
    celestia-devtools pglite stop --all
    celestia-devtools pglite status
    celestia-devtools pglite status --all

Runs inside the celestia-dev WSL2 distro (or any Linux/WSL with node+npm).
"""

from __future__ import annotations
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from celestia_devtools.core import logger as _logger
from celestia_devtools.env.preflight import require as _require_tools

# Node packages — (name, version range) so scoped names (@electric-sql/…)
# don't get mangled by naive split("@"). pglite-pgvector gives the `vector`
# extension. Versions verified against npm (2026-07).
PACKAGES = [("@electric-sql/pglite", "^0.5"), ("@electric-sql/pglite-socket", "^0.2"), ("@electric-sql/pglite-pgvector", "^0.0.5")]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0  # 0 = let the OS pick a free port (reported back via stdout)
# Sidecar layout: the default instance keeps the historical single sidecar
# `pglite-instance.json`; named instances get `pglite-instance-<name>.json`.
SIDECAR_NAME = "pglite-instance.json"
DEFAULT_INSTANCE = "default"
# Instance names become sidecar filename suffixes, so keep them tight and
# filesystem-safe (also keeps `pglite-instance-<name>.json` grep-friendly).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

def _log(level: str, msg: str) -> None:
    getattr(_logger, level, _logger.info)(msg)

def _data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")

    d = Path(base) / "celestia" / "pglite"
    d.mkdir(parents = True, exist_ok = True)
    return d

def _node_dir() -> Path:
    """Shared node_modules dir for the PGlite packages, reused across calls."""
    d = _data_dir() / "node"
    d.mkdir(parents = True, exist_ok = True)
    return d

def _sidecar_path(name: str = DEFAULT_INSTANCE) -> Path:
    if name == DEFAULT_INSTANCE:
        return _data_dir() / SIDECAR_NAME

    return _data_dir() / f"pglite-instance-{name}.json"

def _iter_sidecar_names() -> list[str]:
    """Names of every recorded instance (default + named), sorted."""
    names: list[str] = []

    for p in _data_dir().glob("pglite-instance*.json"):
        if p.name == SIDECAR_NAME:
            names.append(DEFAULT_INSTANCE)
        else:
            stem = p.name[: -len(".json")]
            prefix = "pglite-instance-"

            if stem.startswith(prefix):
                names.append(stem[len(prefix):])

    return sorted(set(names))

def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise SystemExit(f"invalid instance name {name!r}: expected [a-z0-9][a-z0-9-]* (max 32)")

    return name

# ── The embedded Node launcher script ─────────────────────────────────
#
# pglite-server CLI doesn't take an extensions argument, so we generate a
# tiny launcher that creates a PGlite instance with the pgvector extension
# registered, wraps it in PGLiteSocketServer, and prints the connection URL
# on a dedicated "READY:" line that the Python side parses. This keeps the
# Python↔Node contract to a single line of stdout.
LAUNCHER_JS = r"""
// pglite-socket uses `new CustomEvent(...)` in its listening/error dispatch,
// which is a global in Node 20+ but absent in Node 18 (Ubuntu 24.04's apt
// nodejs). Polyfill it BEFORE importing pglite-socket. ESM `import` is
// hoisted, so we can't polyfill then import statically — use dynamic
// import() after installing the shim.
if (typeof globalThis.CustomEvent === "undefined") {
  globalThis.CustomEvent = class CustomEvent extends Event {
    constructor(type, opts = {}) {
      super(type, opts);
      this.detail = opts.detail;
    }
  };
}

const { PGlite } = await import("@electric-sql/pglite");
const { vector } = await import("@electric-sql/pglite-pgvector");
const { PGLiteSocketServer } = await import("@electric-sql/pglite-socket");

const host = process.env.PGLITE_HOST || "127.0.0.1";
const wantPort = parseInt(process.env.PGLITE_PORT || "0", 10);
const dataDir = process.env.PGLITE_DATA_DIR || "memory://";

const db = await PGlite.create({
  dataDir,
  extensions: { vector },
});
await db.exec("CREATE EXTENSION IF NOT EXISTS vector;");

// PGLiteSocketServer.start() resolves once the underlying net server is
// listening (it awaits the 'listening' event internally), so we don't wire
// our own event listener. The actual bound port comes from the inner server.
const server = new PGLiteSocketServer({ db, port: wantPort, host });
await server.start();
const actualPort = server.server?.address?.()?.port ?? wantPort;
// Single machine-parseable readiness line. postgres user/pw are irrelevant
// to PGlite's wire server but the URL must carry them for standard clients.
console.log(`READY:postgresql://postgres:postgres@${host}:${actualPort}/postgres`);

const shutdown = async () => {
  try { await server.stop(); } catch {}
  try { await db.close(); } catch {}
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
// Guard against unhandled socket errors (e.g. ECONNRESET when a client
// drops the connection abruptly). Without this, the Node process crashes
// on the first client disconnect, taking the whole DB down with it.
process.on("uncaughtException", (err) => {
  if (err && (err.code === "ECONNRESET" || err.code === "EPIPE" || err.code === "ECONNABORTED")) {
    // A client socket was reset — this is normal under aggressive connection
    // pooling (sqlx opens/closes connections rapidly). Just log and continue.
    return;
  }
  console.error("uncaughtException:", err);
});
"""

def _ensure_node_packages() -> bool:
    """Install the PGlite npm packages into the shared node dir if absent.
    Idempotent — skips when package.json already lists all required packages."""
    nd = _node_dir()
    pkg_json = nd / "package.json"
    required_names = {name for name, _ in PACKAGES}
    need_install = True

    if pkg_json.exists():
        try:
            deps = json.loads(pkg_json.read_text()).get("dependencies", {})

            if required_names <= set(deps):
                need_install = False
        except Exception:
            pass

    if need_install:
        _log("info", f"installing PGlite npm packages into {nd} ...")
        deps = {name: ver for name, ver in PACKAGES}
        pkg_json.write_text(json.dumps({"name": "celestia-pglite-host", "private": True, "type": "module", "dependencies": deps}, indent = 2))
        r = subprocess.run(["npm", "install", "--prefix", str(nd)], capture_output = True, text = True, shell = os.name == "nt")

        if r.returncode != 0:
            _log("error", f"npm install failed:\n{r.stdout}\n{r.stderr}")
            return False

    return True

def _write_launcher() -> Path:
    p = _node_dir() / "launch-pglite.mjs"
    p.write_text(LAUNCHER_JS)
    return p

def _free_port() -> int:
    """Ask the OS for a free TCP port (bind port 0, read the assigned port,
    close). Tiny TOCTOU window is acceptable for a dev facility."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _read_sidecar(name: str = DEFAULT_INSTANCE) -> dict:
    try:
        return json.loads(_sidecar_path(name).read_text())
    except Exception:
        return {}

def _write_sidecar(data: dict, name: str = DEFAULT_INSTANCE) -> None:
    try:
        _sidecar_path(name).write_text(json.dumps(data))
    except OSError as e:
        _log("warn", f"could not write sidecar: {e}")

def _clear_sidecar(name: str = DEFAULT_INSTANCE) -> None:
    """Remove the sidecar outright so `--all` sweeps don't re-see dead ones."""

    try:
        _sidecar_path(name).unlink(missing_ok = True)
    except OSError as e:
        _log("warn", f"could not remove sidecar: {e}")

def _proc_alive(pid: int) -> bool:
    """Best-effort liveness check that works cross-platform."""

    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False

def start(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, persist: str | None = None, force: bool = False, name: str = DEFAULT_INSTANCE) -> int:
    _validate_name(name)
    # Fail fast with a clear hint if node/npm are missing — otherwise the
    # user gets an opaque FileNotFoundError from deep inside npm install.
    _require_tools("node", "npm")

    # If an instance of this name is already recorded and alive, reuse it
    # (unless --force).
    if not force:
        sc = _read_sidecar(name)

        if sc and _proc_alive(sc.get("pid", 0)):
            _log("info", f"PGlite[{name}] already running → {sc['url']}")
            print(sc["url"])
            return 0

    if not _ensure_node_packages():
        return 1

    chosen = port if port > 0 else _free_port()
    data_dir = persist or "memory://"
    launcher = _write_launcher()
    env = {**os.environ, "PGLITE_HOST": host, "PGLITE_PORT": str(chosen), "PGLITE_DATA_DIR": data_dir}
    _log("info", f"starting PGlite on {host}:{chosen} (data={data_dir}) ...")
    # Detach so the server outlives this CLI invocation. stdout piped so we can
    # capture the READY line; stderr inherited for visibility.
    log_path = _data_dir() / "pglite.log"
    log_f = log_path.open("w")
    # On Windows, shell=True is needed to find node/npm from PATH.
    # On Unix, start_new_session detaches from the parent.
    popen_kwargs = {"cwd": str(_node_dir()), "env": env, "stdout": subprocess.PIPE, "stderr": log_f}

    if os.name == "nt":
        popen_kwargs["shell"] = True
        proc = subprocess.Popen(f'node "{launcher}"', **popen_kwargs)
    else:
        popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(["node", str(launcher)], **popen_kwargs)

    # Wait up to 30s for the READY line.
    url = None
    deadline = time.time() + 30

    while time.time() < deadline:
        line = proc.stdout.readline()

        if not line:
            if proc.poll() is not None:
                break

            time.sleep(0.1)
            continue

        text = line.decode(errors = "replace").strip()

        if text.startswith("READY:"):
            url = text[len("READY:"):]
            break

        _log("info", f"[pglite] {text}")

    if not url:
        _log("error", f"PGlite did not become ready in 30s (see {log_path})")

        if proc.poll() is not None:
            _log("error", f"process exited with code {proc.returncode}")

        try:
            proc.kill()
        except Exception:
            pass

        return 1

    _write_sidecar({"pid": proc.pid, "port": chosen, "url": url, "host": host, "data_dir": data_dir, "name": name, "started_at": time.time()}, name)
    _log("info", f"✓ PGlite[{name}] ready → {url}  (pid {proc.pid}, log {log_path})")
    print(url)
    return 0

def _stop_one(name: str) -> bool:
    """Stop the named instance if recorded. Returns True if a live one was killed."""
    sc = _read_sidecar(name)
    pid = sc.get("pid", 0)

    if not pid or not _proc_alive(pid):
        _clear_sidecar(name)
        return False

    _log("info", f"stopping PGlite[{name}] pid {pid} ...")

    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pass
    except OSError as e:
        _log("warn", f"kill failed: {e}")

    # Give it a moment, then verify.
    for _ in range(20):
        if not _proc_alive(pid):
            break

        time.sleep(0.1)

    _clear_sidecar(name)
    _log("info", f"✓ PGlite[{name}] stopped.")
    return True

def stop(name: str = DEFAULT_INSTANCE, all_: bool = False) -> int:
    if all_:
        names = _iter_sidecar_names()

        if not names:
            _log("info", "no running PGlite instances recorded.")
            return 0

        stopped = [n for n in names if _stop_one(n)]
        _log("info", f"✓ stopped {len(stopped)}/{len(names)} instance(s): {', '.join(stopped) or 'none alive'}")
        return 0

    _validate_name(name)
    sc = _read_sidecar(name)
    pid = sc.get("pid", 0)

    if not pid or not _proc_alive(pid):
        _log("info", f"no running PGlite[{name}] instance recorded.")
        _clear_sidecar(name)
        return 0

    _stop_one(name)
    return 0

def status(name: str = DEFAULT_INSTANCE, all_: bool = False) -> int:
    if all_:
        live = []

        for n in _iter_sidecar_names():
            sc = _read_sidecar(n)

            if sc and _proc_alive(sc.get("pid", 0)):
                live.append({"name": n, **{k: v for k, v in sc.items() if k != "name"}})

        print(json.dumps({"running": bool(live), "instances": live}, indent = 2))
        return 0 if live else 1

    _validate_name(name)
    sc = _read_sidecar(name)
    pid = sc.get("pid", 0)
    alive = _proc_alive(pid) if pid else False

    if alive:
        print(json.dumps({"running": True, **sc}, indent = 2))
        return 0

    print(json.dumps({"running": False}))
    return 1 if pid else 0

def main() -> int:
    p = argparse.ArgumentParser(prog = "celestia-devtools pglite", description = "Shared temporary-PGlite server (pgvector-enabled) for the celestia ecosystem.")
    sub = p.add_subparsers(dest = "cmd", required = True)

    sp = sub.add_parser("start", help = "spawn a PGlite server, print its DATABASE_URL")
    sp.add_argument("--host", default = DEFAULT_HOST)
    sp.add_argument("--port", type = int, default = DEFAULT_PORT, help = "0 = auto-pick free port")
    sp.add_argument("--persist", default = None, help = "data dir path (default: in-memory)")
    sp.add_argument("--force", action = "store_true", help = "restart even if one is running")
    sp.add_argument("--name", default = DEFAULT_INSTANCE, help = "instance name (own sidecar + reuse slot; default: 'default')")

    sp = sub.add_parser("stop", help = "stop the recorded PGlite instance")
    sp.add_argument("--name", default = DEFAULT_INSTANCE, help = "instance name to stop")
    sp.add_argument("--all", dest = "all_", action = "store_true", help = "stop every recorded instance")
    sp = sub.add_parser("status", help = "show the recorded instance state")
    sp.add_argument("--name", default = DEFAULT_INSTANCE, help = "instance name to query")
    sp.add_argument("--all", dest = "all_", action = "store_true", help = "list every recorded instance")

    args = p.parse_args()

    if args.cmd == "start":
        return start(args.host, args.port, args.persist, args.force, args.name)

    if args.cmd == "stop":
        return stop(args.name, args.all_)

    if args.cmd == "status":
        return status(args.name, args.all_)

    return 2

if __name__ == "__main__":
    sys.exit(main())
