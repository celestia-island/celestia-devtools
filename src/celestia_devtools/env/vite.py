#!/usr/bin/env python3
"""Vite frontend build/serve/watch — Python port of the former bash recipes.

celestia frontends (shittim-chest, hikari, tairitsu, …) use ``vite build`` +
a plain static file server: no vite dev server, no HMR WebSocket, no error
overlay. Rebuilds are triggered by malkuth (or any file watcher) so the
watcher owns the terminal output.

The server is SPA-aware: the frontends use history-API routes (the chest
webui routes workspaces as ``/@<uuid>``), so a navigation GET whose path
maps to no file is served ``index.html`` instead of the bare
``http.server`` 404 — without the fallback, a page refresh on any routed
view replaces the whole app (custom title bar included) with the 404
error page. Extension-bearing misses stay 404 so broken asset links
still fail loudly.

Commands (dispatched on ``sys.argv[0]``, mirroring the mock-* pattern)::

    celestia-devtools vite-build [-- <extra vite args>]
    celestia-devtools vite-serve [--port 5173] [--no-spa]
    celestia-devtools vite-dev   [--port 5173]   # build → serve → watch src/
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_PORT = "5173"


def _dist_dir() -> Path:
    dist = Path("dist")
    if dist.is_dir():
        return dist
    return Path("../../dist/webui")


def _port_of(args: list[str]) -> str:
    """--port flag > CELESTIA_VITE_PORT env > 5173.

    The env rung is the ports-facility seam: a tauri shell launched with
    `ports env` exports carries CELESTIA_VITE_PORT, its beforeDevCommand
    (vite-serve) inherits it, and the shell navigates to the same port —
    one variable threads the loop, and two apps on one machine never
    land on the same devUrl."""
    it = iter(range(len(args)))
    for i in it:
        if args[i] == "--port" and i + 1 < len(args):
            return args[i + 1]
        if args[i].startswith("--port="):
            return args[i].split("=", 1)[1]
    env = os.environ.get("CELESTIA_VITE_PORT", "").strip()
    if env.isdigit():
        return env
    return DEFAULT_PORT


class _SpaFallbackHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler with history-API fallback (see the module docstring).

    Only navigation-shaped GETs fall back: no file extension in the last
    path segment, ``text/html`` accepted, and no such file on disk.
    """

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self._spa_fallback():
            self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 — http.server API
        if self._spa_fallback():
            self.path = "/index.html"
        super().do_HEAD()

    def _spa_fallback(self) -> bool:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        last = path.rsplit("/", 1)[-1]
        if not last or "." in last:
            return False  # "/" is native; file-shaped misses must 404
        if "text/html" not in self.headers.get("Accept", ""):
            return False  # XHR/fetch probes keep their honest status
        return not Path(self.translate_path(path)).exists()

    def log_message(self, fmt: str, *args: object) -> None:
        # Prefix keeps the watcher-owned terminal scannable.
        print("[vite-serve] " + (fmt % args), file=sys.stderr)


def _serve(dist: Path, port: str, spa: bool = True) -> int:
    handler = _SpaFallbackHandler if spa else http.server.SimpleHTTPRequestHandler
    # directory= is NOT optional: without it SimpleHTTPRequestHandler
    # resolves paths against the PROCESS cwd, and vite project roots carry
    # their own source index.html — the server then happily serves the
    # SOURCE page whose /assets/* references all 404 (the 2026-10-16 live
    # bite: app shell 200, every chunk dead).
    bound = functools.partial(handler, directory=str(dist))
    with http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), bound) as srv:
        srv.daemon_threads = True
        mode = " (SPA fallback)" if spa else ""
        print(f"[vite-serve] serving {dist} on http://localhost:{port}{mode}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _build(extra: list[str]) -> int:
    if extra and extra[0] == "--":
        extra = extra[1:]
    print("[vite-build] pnpm vite build " + " ".join(extra))
    return subprocess.run(["pnpm", "vite", "build", *extra]).returncode


def vite_build() -> int:
    return _build(sys.argv[1:])


def vite_serve() -> int:
    port = _port_of(sys.argv[1:])
    dist = _dist_dir()
    return _serve(dist, port, spa="--no-spa" not in sys.argv[1:])


def vite_dev() -> int:
    port = _port_of(sys.argv[1:])
    if _build([]) != 0:
        return 1
    dist = _dist_dir()
    print(f"[vite-dev] starting static server on port {port}")
    # Daemon thread: dies with the process, no explicit teardown needed.
    threading.Thread(target=_serve, args=(dist, port), daemon=True).start()
    time.sleep(1)
    exe = "malkuth.exe" if os.name == "nt" else "malkuth"
    malkuth = (
        os.environ.get("MALKUTH_BIN", "")
        or shutil.which("malkuth")
        or os.path.join("..", "malkuth", "target", "release", exe)
    )
    if os.path.isfile(malkuth) or shutil.which(malkuth):
        print("[vite-dev] watching src/ for changes (malkuth)...")
        watched = subprocess.run(
            [malkuth, "--watch", "src", "--drain-secs", "2", "--",
             "pnpm", "vite", "build"]
        )
        return watched.returncode
    print("[vite-dev] malkuth not found — install: cd ../malkuth && cargo build --release --features cli")
    print("[vite-dev] falling back to one-shot build; restart manually to pick up changes.")
    return 0


def main() -> int:
    cmd = Path(sys.argv[0]).stem
    handlers = {
        "vite-build": vite_build,
        "vite-serve": vite_serve,
        "vite-dev": vite_dev,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"error: unknown vite command '{cmd}'", file=sys.stderr)
        return 2
    return handler()


if __name__ == "__main__":
    raise SystemExit(main())
