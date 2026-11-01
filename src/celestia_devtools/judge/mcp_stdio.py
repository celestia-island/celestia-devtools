"""Minimal read-only MCP client over stdio (JSON-RPC 2.0, one message per line).

Why hand-rolled instead of an SDK: this tool talks to exactly one server
(``malkuth mcp``), needs three read methods, and must stay stdlib-only — the same
reason the rest of this package keeps its dependency surface at zero.

The read-only contract is **structural for the judgement path**: :data:`READ_ONLY_TOOLS` is
an allowlist and :meth:`mcp_session.call_tool` refuses anything outside it, so a caller
cannot reach a mutating tool by accident or by copy-paste. The single documented bypass is
the demo supervision in :mod:`celestia_devtools.judge.demo_fleet`, which goes through the
JSON-RPC layer directly because widening this allowlist would weaken the guard itself.
"""

from __future__ import annotations
import json
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

#: The only MCP tools this package may call. Actions (restart/drain, supervision
#: itself) are outside the allowlist on purpose — see the package docstring.
READ_ONLY_TOOLS = frozenset({"malkuth_status", "malkuth_logs", "malkuth_probe"})

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "celestia-judge", "version": "0"}

class mcp_error(RuntimeError):
    """A protocol-level or tool-level failure (the server answered with an error)."""

class mcp_unavailable(mcp_error):
    """The transport failed: timeout, EOF, unparsable framing, missing capability."""

class mcp_session:
    """One stdio session with an MCP server, opened as a context manager.

    Every request is bounded by `timeout`. On any transport failure the session
    marks itself dead and every later call raises :class:`mcp_unavailable`, which is
    what lets the caller fail open (report "cannot judge") instead of guessing.
    """

    def __init__(self, command: Sequence[str], *, timeout: float = 10.0) -> None:
        self._command = list(command)
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._replies: dict[int, dict] = {}
        self._events: dict[int, threading.Event] = {}
        self._next_id = 1
        self._closed = False
        self._reader: threading.Thread | None = None
        self._tools: frozenset[str] = frozenset()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self) -> mcp_session:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if not self._command:
            raise mcp_unavailable("empty MCP command")

        try:
            # stderr is dropped so server logs cannot pollute the JSON-RPC stream.
            self._proc = subprocess.Popen(self._command, stdin = subprocess.PIPE, stdout = subprocess.PIPE, stderr = subprocess.DEVNULL, text = True, bufsize = 1)
        except OSError as e:
            raise mcp_unavailable(f"cannot start MCP server {self._command!r}: {e}") from e

        self._reader = threading.Thread(target = self._read_loop, name = "mcp-reader", daemon = True)
        self._reader.start()
        self._request("initialize", {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO})
        self._notify("notifications/initialized", {})
        listing = self._request("tools/list", {})
        tools = listing.get("tools")

        if not isinstance(tools, list):
            raise mcp_unavailable("tools/list returned no tool array")

        names = {t.get("name") for t in tools if isinstance(t, dict)}
        self._tools = frozenset(n for n in names if isinstance(n, str))

    def close(self) -> None:
        self._closed = True
        proc = self._proc

        if proc is None:
            return

        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

        try:
            proc.wait(timeout = 5)
        except subprocess.TimeoutExpired:
            proc.kill()

            try:
                proc.wait(timeout = 5)
            except subprocess.TimeoutExpired:
                pass

    # ── tools ────────────────────────────────────────────────────────────────
    @property
    def tools(self) -> frozenset[str]:
        """Tool names the server advertised at initialize time."""
        return self._tools

    def call_tool(self, name: str, arguments: dict | None = None, *, timeout: float | None = None) -> Any:
        """Call one read-only tool and return its parsed JSON payload.

        Raises :class:`mcp_error` (never a bare exception) for a tool-level error or
        an unparsable payload; the payload contract is the server's own JSON
        document inside the first text content block.
        """

        if name not in READ_ONLY_TOOLS:
            raise mcp_error(f"tool {name!r} is not in the read-only allowlist {sorted(READ_ONLY_TOOLS)}")

        if name not in self._tools:
            raise mcp_unavailable(f"MCP server does not advertise {name!r}; cannot judge")

        reply = self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout = timeout)
        content = reply.get("content")

        if not isinstance(content, list) or not content:
            raise mcp_error(f"{name} returned no content blocks")

        first = content[0]

        if not isinstance(first, dict) or not isinstance(first.get("text"), str):
            raise mcp_error(f"{name} returned a non-text content block")

        try:
            return json.loads(first["text"])
        except json.JSONDecodeError as e:
            raise mcp_error(f"{name} returned payload that is not JSON: {e}") from e

    # ── JSON-RPC plumbing ────────────────────────────────────────────────────
    def _next_request_id(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._events[request_id] = threading.Event()
            return request_id

    def _write(self, message: dict) -> None:
        proc = self._proc

        if (proc is None) or self._closed or (proc.stdin is None):
            raise mcp_unavailable("MCP session is closed")

        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            self._closed = True
            raise mcp_unavailable(f"cannot write to MCP server: {e}") from e

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        request_id = self._next_request_id()
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        event = self._events[request_id]
        budget = self._timeout if timeout is None else timeout

        if not event.wait(budget):
            raise mcp_unavailable(f"{method} timed out after {budget:g}s")

        reply = self._replies.pop(request_id, None)

        if reply is None:
            raise mcp_unavailable(f"{method} produced no reply")

        error = reply.get("error")

        if error is not None:
            raise mcp_error(f"{method} failed: {error}")

        result = reply.get("result")

        if not isinstance(result, dict):
            raise mcp_error(f"{method} returned no result object")

        return result

    def _read_loop(self) -> None:
        proc = self._proc

        if (proc is None) or (proc.stdout is None):
            return

        for line in proc.stdout:
            text = line.strip()

            if not text:
                continue

            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # A server that writes non-JSON on stdout is not something we can
                # reason about; fail every waiter rather than skip silently.
                self._fail_all("MCP server wrote a non-JSON line")
                return

            if not isinstance(message, dict):
                continue

            request_id = message.get("id")

            if not isinstance(request_id, int):
                continue  # notifications and server-initiated requests are ignored

            self._replies[request_id] = message
            event = self._events.get(request_id)

            if event is not None:
                event.set()

        self._fail_all("MCP server closed its output")

    def _fail_all(self, reason: str) -> None:
        self._closed = True

        for event in list(self._events.values()):
            event.set()

def open_session(command: Iterable[str], *, timeout: float = 10.0) -> mcp_session:
    """Convenience wrapper: build the session and let the caller open/close it."""
    return mcp_session(list(command), timeout = timeout)

def utc_now() -> float:
    """Wall clock helper kept here so tests can patch one symbol."""
    return time.time()
