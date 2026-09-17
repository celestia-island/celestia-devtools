#!/usr/bin/env python3
"""ci-farm-heartbeat-receiver.py — CI farm guest heartbeat receiver.

Every runner guest hits ``GET /beat/<name>`` once a minute (systemd timer);
this service records the last-beat time as the mtime of
``<beat-dir>/<name>``. The referee (ci_farm_heartbeat_referee.py) reads
those mtimes to decide liveness and triggers a hypervisor rollback on
stale beats. See the referee module for the architecture and safety rails.

Endpoints: GET /beat/<name> → 204 (record); GET /status → JSON summary;
anything else → 404. Guest names must match ^[a-zA-Z0-9_-]+$ (no path
traversal). No authentication: closed lab network, the payload only
influences a timestamp.

Configuration (environment): CI_HB_PORT (listen port),
CI_HB_BEAT_DIR (beat file directory).
"""

from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"ci-farm-heartbeat receiver: missing required env {name}")
    return value


PORT = int(_require("CI_HB_PORT"))
BEAT_DIR = _require("CI_HB_BEAT_DIR")
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def beat_path(beat_dir: str, name: str) -> str:
    return os.path.join(beat_dir, name)


def record_beat(beat_dir: str, name: str) -> None:
    os.makedirs(beat_dir, exist_ok=True)
    # 重写文件（而非 utime）保证 mtime 变化可见且崩溃安全。
    with open(beat_path(beat_dir, name), "w", encoding="utf-8") as fh:
        fh.write(str(int(time.time())))


def run_receiver(bind: str, beat_dir: str) -> None:
    """可测入口：在 bind 上起 HTTP 服务，beat 文件落 beat_dir。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.startswith("/beat/"):
                name = path[len("/beat/"):]
                if not NAME_RE.match(name):
                    self.send_response(400)
                    self.end_headers()
                    return
                try:
                    record_beat(beat_dir, name)
                except OSError as exc:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(exc).encode())
                    return
                self.send_response(204)
                self.end_headers()
                return
            if path == "/status":
                beats: dict[str, int] = {}
                entries = sorted(os.listdir(beat_dir)) if os.path.isdir(beat_dir) else []
                for entry in entries:
                    p = os.path.join(beat_dir, entry)
                    if os.path.isfile(p):
                        beats[entry] = int(os.path.getmtime(p))
                body = json.dumps({"beats": beats}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            pass

    host, _, port = bind.rpartition(":")
    os.makedirs(beat_dir, exist_ok=True)
    print(f"ci-farm-heartbeat receiver listening on {bind}, beats -> {beat_dir}", flush=True)
    ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler).serve_forever()


if __name__ == "__main__":
    run_receiver(f"0.0.0.0:{PORT}", BEAT_DIR)
