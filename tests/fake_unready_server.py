#!/usr/bin/env python3
"""A supervised stand-in for the "alive but not ready" case.

It answers ``/healthz`` and ``/readyz`` with 503 and never writes another line after its
startup banner, so the judge sees down readiness, a long uptime and a frozen log tail at the
same time (the three signals C1 requires). Used by the real-malkuth acceptance test.
"""

from __future__ import annotations
import http.server
import sys

class handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(503 if self.path in ("/healthz", "/readyz") else 404)
        self.end_headers()

    def log_message(self, *args) -> None:
        """Silence per-request logging: the log tail must stay frozen for the test."""
        return

def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 0
    print(f"unready server on {port}", flush = True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
