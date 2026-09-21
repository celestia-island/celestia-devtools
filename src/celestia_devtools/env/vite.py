#!/usr/bin/env python3
"""Vite frontend build/serve/watch — Python port of the former bash recipes.

celestia frontends (shittim-chest, hikari, tairitsu, …) use ``vite build`` +
a plain static file server: no vite dev server, no HMR WebSocket, no error
overlay. Rebuilds are triggered by malkuth (or any file watcher) so the
watcher owns the terminal output.

Commands (dispatched on ``sys.argv[0]``, mirroring the mock-* pattern)::

    celestia-devtools vite-build [-- <extra vite args>]
    celestia-devtools vite-serve [--port 5173]
    celestia-devtools vite-dev   [--port 5173]   # build → serve → watch src/
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = "5173"


def _dist_dir() -> Path:
    dist = Path("dist")
    if dist.is_dir():
        return dist
    return Path("../../dist/webui")


def _port_of(args: list[str]) -> str:
    it = iter(range(len(args)))
    for i in it:
        if args[i] == "--port" and i + 1 < len(args):
            return args[i + 1]
        if args[i].startswith("--port="):
            return args[i].split("=", 1)[1]
    return DEFAULT_PORT


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
    print(f"[vite-serve] serving {dist} on http://localhost:{port}")
    server = subprocess.run([sys.executable, "-m", "http.server", port], cwd=dist)
    return server.returncode


def vite_dev() -> int:
    port = _port_of(sys.argv[1:])
    if _build([]) != 0:
        return 1
    dist = _dist_dir()
    print(f"[vite-dev] starting static server on port {port}")
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", port], cwd=dist
    )
    try:
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
        return server.wait()
    finally:
        server.terminate()


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
