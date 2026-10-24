#!/usr/bin/env python3
"""Per-app port allocation for tauri dev stacks — no more cross-wired apps.

Two Tauri debug apps on one machine fight over the SAME two sockets: the
WebView2 CDP port (9222 — whichever app boots first owns it, the other's
devtools silently go dead) and the vite devUrl server (5173 — a second
app's webview loads the FIRST app's frontend, the literal cross-wiring
seen live between WoWSP and shittim-chest, 2026-10-17). Neither port may
be a constant once more than one shell is in development.

This facility owns the coordination:

  - a per-user registry (``%LOCALAPPDATA%/celestia/ports.json`` / the
    XDG twin) maps ``(app, kind)`` → ``{port, pid, ts}``;
  - ``ports acquire`` answers a DETERMINISTIC preferred port per (app,
    kind) — stable across runs, so URLs and CDP targets feel permanent —
    and walks forward to the first port that is both un-listening and
    un-claimed by a live registrant;
  - stale claims (dead pid) are garbage-collected on every command;
  - ``ports env`` emits the coordination contract as shell exports:
    ``CELESTIA_<KIND>_PORT``, plus the derived pieces consumers need
    (``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` with the allocated CDP
    port for WebView2 apps that respect the environment variable).

Kinds: ``vite`` (base 5173) and ``cdp`` (base 9222). App names are free
strings — the repo name (``shittim-chest``, ``wowsp``) is the obvious
key.

Usage::

    celestia-devtools ports acquire --app shittim-chest --kind vite
    celestia-devtools ports acquire --app wowsp       --kind cdp
    celestia-devtools ports env    --app shittim-chest          # exports
    celestia-devtools ports list
    celestia-devtools ports release --app shittim-chest --kind vite
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# kind → (base port, exclusive ceiling of the scan window)
KINDS: dict[str, int] = {"vite": 5173, "cdp": 9222}
SCAN_WINDOW = 64  # ports tried forward from the preferred one

REGISTRY_ENV_KEYS = ("CELESTIA_PORTS_REGISTRY",)

def registry_path() -> Path:
    for key in REGISTRY_ENV_KEYS:
        if os.environ.get(key):
            return Path(os.environ[key])

    from celestia_devtools.env.host import detect_host_kind

    if detect_host_kind() == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))

    return base / "celestia" / "ports.json"

def _preferred(app: str, kind: str) -> int:
    """Deterministic preferred port for (app, kind): the kind's base plus a
    stable offset from the app name — SAME app → SAME port across runs and
    machines, DIFFERENT apps → different preferred ports in the common
    case (the scan resolves the rest)."""
    offset = int.from_bytes(hashlib.sha256(app.encode("utf-8")).digest()[: 2], "big") % (SCAN_WINDOW // 2)
    return KINDS[kind] + offset

def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or (pid <= 0):
        return False

    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION

        if not handle:
            return False

        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and (exit_code.value == 259)) # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0

def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _gc(entries: dict) -> int:
    """Drop claims whose owning pid is gone. Returns the number reclaimed."""
    dead = [k for k, v in entries.items()if isinstance(v, dict) and not _pid_alive(v.get("pid"))]

    for k in dead:
        entries.pop(k, None)

    return len(dead)

def _save(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    path.write_text(json.dumps(entries, indent = 2), encoding = "utf-8")

def acquire(app: str, kind: str) -> int:
    if kind not in KINDS:
        raise SystemExit(f"error: unknown kind {kind!r} (known: {', '.join(KINDS)})")

    path = registry_path()
    entries = _load(path)
    _gc(entries)
    key = f"{app}:{kind}"

    held = entries.get(key)

    if isinstance(held, dict) and held.get("port"):
        # Already ours and still valid (the GC dropped dead owners): keep
        # the port stable across re-acquires within a session.
        port = int(held["port"])
        entries[key] = {"port": port, "pid": os.getpid(), "ts": int(time.time())}
        _save(path, entries)
        return port

    taken = {int(v["port"])for v in entries.values()if isinstance(v, dict) and v.get("port")}
    start = _preferred(app, kind)

    for candidate in range(start, start + SCAN_WINDOW):
        if (candidate in taken) or _listening(candidate):
            continue

        entries[key] = {"port": candidate, "pid": os.getpid(), "ts": int(time.time())}
        _save(path, entries)
        return candidate

    raise SystemExit(f"error: no free {kind} port in [{start}, {start + SCAN_WINDOW})")

def release(app: str, kind: Optional[str]) -> int:
    path = registry_path()
    entries = _load(path)
    _gc(entries)
    keys = [k for k in entries if k.startswith(f"{app}:") and ((kind is None) or k.endswith(f":{kind}"))]

    for k in keys:
        entries.pop(k, None)

    _save(path, entries)
    return 0

def list_entries() -> dict:
    path = registry_path()
    entries = _load(path)
    _gc(entries)
    _save(path, entries)
    return entries

def env_exports(app: str) -> dict[str, str]:
    """The coordination contract for one app: every kind's port plus the
    derived WebView2 line. Acquires any kind not yet held (a consumer
    asking for its env is booting — the ports should exist)."""
    out: dict[str, str] = {}

    for kind in KINDS:
        out[f"CELESTIA_{kind.upper()}_PORT"] = str(acquire(app, kind))

    cdp = out["CELESTIA_CDP_PORT"]
    out["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={cdp}"
    return out

# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog = "ports", description = __doc__.splitlines()[0])
    sub = parser.add_subparsers(dest = "cmd", required = True)

    ac = sub.add_parser("acquire", help = "allocate (or re-confirm) the app's port for a kind")
    ac.add_argument("--app", required = True)
    ac.add_argument("--kind", required = True, choices = sorted(KINDS))

    rel = sub.add_parser("release", help = "drop the app's claims (one kind or all)")
    rel.add_argument("--app", required = True)
    rel.add_argument("--kind", default = None, choices = sorted(KINDS))

    env = sub.add_parser("env", help = "acquire all kinds and print export lines")
    env.add_argument("--app", required = True)
    env.add_argument("--format", choices = ("export", "json"), default = "export")

    ls = sub.add_parser("list", help = "list live claims")
    ls.add_argument("--json", action = "store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "acquire":
        print(acquire(args.app, args.kind))
        return 0

    if args.cmd == "release":
        return release(args.app, args.kind)

    if args.cmd == "env":
        exports = env_exports(args.app)

        if args.format == "json":
            print(json.dumps(exports, indent = 2))
        else:
            for key in sorted(exports):
                print(f"export {key}='{exports[key]}'")

        return 0

    if args.cmd == "list":
        entries = list_entries()

        if args.json:
            print(json.dumps(entries, indent = 2))
            return 0

        if not entries:
            print("[ports] no live claims")
            return 0

        print(f"{'app:kind':40s} {'port':>6s}  pid")

        for key, v in sorted(entries.items()):
            print(f"{key:40s} {v['port']:>6d}  {v.get('pid', '?')}")

        return 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())
