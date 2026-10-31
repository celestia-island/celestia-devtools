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
  - ``claim`` re-points a claim at a LAUNCHED app's pid: ``acquire``
    records the calling CLI process, which exits immediately, so a launcher
    (``app-drive launch``) records the app pid instead and the claim stays
    valid exactly as long as the app lives;
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
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence
from celestia_devtools.env.host import detect_host_kind
import ctypes
from celestia_devtools.core import envelope

# kind → (base port, exclusive ceiling of the scan window)
KINDS: dict[str, int] = {"vite": 5173, "cdp": 9222}
SCAN_WINDOW = 64  # ports tried forward from the preferred one

REGISTRY_ENV_KEYS = ("CELESTIA_PORTS_REGISTRY",)

def registry_path() -> Path:
    for key in REGISTRY_ENV_KEYS:
        if os.environ.get(key):
            return Path(os.environ[key])

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

def _posix_process_state(pid: int) -> str | None:
    """One-letter /proc/<pid>/stat state (Z = zombie), or None when unreadable."""

    try:
        stat_text = Path("/proc/" + str(int(pid)) + "/stat").read_text(encoding = "utf-8", errors = "replace")
    except OSError:
        return None

    closing = stat_text.rfind(")")

    if closing < 0:
        return None

    fields = stat_text[closing + 2:].split()
    return fields[0] if fields else None

def pid_alive(pid: int | None) -> bool:
    """Is the process running (not merely present as an unreaped zombie)?

    POSIX: os.kill(pid, 0) succeeds for a ZOMBIE too, so an exited-but-unreaped
    direct child would look alive forever and make termination loops wait for
    the whole grace budget. Zombies are therefore reported as not alive.
    """

    if not pid or (pid <= 0):
        return False

    if sys.platform == "win32":
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
    except OSError:
        return False

    state = _posix_process_state(pid)

    if state is None:
        return True  # no /proc entry despite a successful signal: stay conservative

    return state not in ("Z", "X", "x")

def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0

def _load(path: Path) -> dict:
    """Load and sanitize the registry.

    A hand-edited/corrupt registry must never crash a caller with a bare
    ValueError/TypeError: bad entries are dropped, valid ones normalized to
    int port/pid, so every later int() use is safe.
    """

    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    cleaned: dict = {}

    for key, value in data.items():
        if (not isinstance(key, str)) or (not isinstance(value, dict)):
            continue

        try:
            port = int(value.get("port"))
            pid = int(value.get("pid"))
            stamp = int(value.get("ts") or 0)
        except (TypeError, ValueError):
            continue

        if (port < 1) or (port > 65535) or (pid <= 0):
            continue

        cleaned[key] = {"port": port, "pid": pid, "ts": stamp}

    return cleaned

def _gc(entries: dict) -> int:
    """Drop claims whose owning pid is gone. Returns the number reclaimed."""
    dead = [k for k, v in entries.items()if isinstance(v, dict) and not pid_alive(v.get("pid"))]

    for k in dead:
        entries.pop(k, None)

    return len(dead)

def _save(path: Path, entries: dict) -> None:
    """Write the registry atomically (same-directory tmp file + os.replace)."""
    path.parent.mkdir(parents = True, exist_ok = True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))

    try:
        temporary.write_text(json.dumps(entries, indent = 2), encoding = "utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass

        raise SystemExit(f"error: cannot write the ports registry {path}: {exc}") from exc

LOCK_TIMEOUT = 30.0
LOCK_STALE = 60.0

@contextmanager
def registry_lock(path: Path):
    """Exclusive cross-process lock around a registry read-modify-write.

    An O_EXCL lock file works on every platform without optional imports; a
    lock older than LOCK_STALE seconds is treated as a crash leftover (the
    holder records its pid for diagnosis) and reclaimed. Waiting longer than
    LOCK_TIMEOUT is an error, never a silent unlocked write.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents = True, exist_ok = True)
    started = time.monotonic()
    handle = None

    while handle is None:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            handle = os.fdopen(descriptor, "w", encoding = "utf-8")
            handle.write(str(os.getpid()))
            handle.flush()
        except FileExistsError:
            # Reclaim only a lock whose holder is provably gone; a LIVE holder
            # that merely ran longer than LOCK_STALE must never be unlinked.
            try:
                holder = int(lock_path.read_text(encoding = "utf-8").strip() or 0)
            except (OSError, ValueError):
                holder = 0

            holder_alive = bool(holder) and pid_alive(holder)

            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0

            if (age > LOCK_STALE) and (not holder_alive):
                try:
                    lock_path.unlink()
                except OSError:
                    pass

                continue

            if (time.monotonic() - started) > LOCK_TIMEOUT:
                raise SystemExit(f"error: ports registry lock at {lock_path} is still held after {LOCK_TIMEOUT:g}s") from None

            time.sleep(0.02)

    try:
        yield
    finally:
        # Close first: Windows cannot unlink a file that is still open (the
        # lock would leak and deadlock every later registry command).
        try:
            handle.close()
        except OSError:
            pass

        try:
            lock_path.unlink()
        except OSError:
            pass

def acquire(app: str, kind: str) -> int:
    if kind not in KINDS:
        raise SystemExit(f"error: unknown kind {kind!r} (known: {', '.join(KINDS)})")

    path = registry_path()

    with registry_lock(path):
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

def claim(app: str, kind: str, port: int, pid: int) -> None:
    """Record that pid - a LAUNCHED app, not this CLI process - owns the port.

    acquire writes the calling process's pid, so a launcher that spawns a
    long-lived app and exits leaves a claim the next command's GC drops (the
    port then looks free while the app still listens on it). Recording the app
    pid keeps the claim live for exactly as long as the app is.
    """

    if kind not in KINDS:
        raise SystemExit(f"error: unknown kind {kind!r} (known: {', '.join(KINDS)})")

    path = registry_path()

    with registry_lock(path):
        entries = _load(path)
        _gc(entries)
        entries[f"{app}:{kind}"] = {"port": int(port), "pid": int(pid), "ts": int(time.time())}
        _save(path, entries)

def release(app: str, kind: str | None) -> int:
    path = registry_path()

    with registry_lock(path):
        entries = _load(path)
        _gc(entries)
        keys = []

        for key in entries:
            owner, separator, key_kind = key.rpartition(":")

            if (not separator) or (owner != str(app)):
                continue

            if (kind is not None) and (key_kind != kind):
                continue

            keys.append(key)

        for k in keys:
            entries.pop(k, None)

        _save(path, entries)

    return 0

def list_entries() -> dict:
    path = registry_path()

    with registry_lock(path):
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

def main(argv: Sequence[str] | None = None) -> int:
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
            envelope.emit(exports)
        else:
            for key in sorted(exports):
                print(f"export {key}='{exports[key]}'")

        return 0

    if args.cmd == "list":
        entries = list_entries()

        if args.json:
            envelope.emit(entries)
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
