#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Minimal Chrome DevTools Protocol client - stdlib only (RFC 6455 subset).

Why hand-roll the websocket instead of depending on a client library: this
package is consumed by many repositories through one install, and the only
CDP capability the desktop debug channel needs is "send one text frame, read
one text frame" against a loopback socket. A dependency would add an
install-time surface to every consumer for roughly 150 lines of protocol
that never changes. The subset is pinned by unit tests instead: the RFC 6455
handshake accept vector, client masking, the 7/16/64-bit length forms, and
ping/pong/fragmentation are all exercised without a real app.

Server frames are validated rather than trusted: an unmasked flag, reserved
bits, an unknown opcode, a fragmented or oversized control frame, a stray
continuation and a data frame inside an unfinished fragmented message are all
protocol errors. Fragmented data messages are reassembled with a running byte
total, so a peer cannot make the client buffer unbounded data.

wss:// is rejected on purpose: WebView2 CDP is plain ws:// on loopback, and
this client deliberately carries no TLS/certificate surface (a TLS-wrapped
websocket would need trust policy this tool has no business inventing).

What this module deliberately does NOT implement: permessage-deflate, TLS,
subprotocol negotiation, or a public websocket server.

Discovery uses http.client rather than urllib.request on purpose: urllib
honors HTTP_PROXY, and a proxy that swallows a loopback CDP request turns a
local debug session into a cryptic timeout.
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import time
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA
DATA_OPCODES = (OPCODE_CONTINUATION, OPCODE_TEXT, OPCODE_BINARY)
CONTROL_OPCODES = (OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG)
KNOWN_OPCODES = DATA_OPCODES + CONTROL_OPCODES
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_CONTROL_PAYLOAD = 125
DEFAULT_TIMEOUT = 15.0
CR = chr(13)
LF = chr(10)
CRLF = CR + LF

class cdp_error(Exception):
    """A runtime failure the CLI renders as an ok:false envelope.

    The code attribute is the stable machine-readable discriminant
    (cdp-unreachable, cdp-timeout, cdp-protocol, js-exception, ...); details
    are extra envelope fields (the raw CDP error object, the JS exception
    payload).
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

def websocket_key() -> str:
    """A fresh 16-byte client nonce, base64-encoded (RFC 6455 section 4.1)."""
    return base64.b64encode(os.urandom(16)).decode("ascii")

def websocket_accept(key: str) -> str:
    """The Sec-WebSocket-Accept value a correct server must echo back."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")

def encode_client_frame(payload: bytes, opcode: int = OPCODE_TEXT) -> bytes:
    """Frame a client-to-server frame: always masked, single fragment, FIN set."""
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    mask = os.urandom(4)

    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)

    header += mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked

def _read_exact(reader, size: int) -> bytes:
    """Read exactly size bytes from a buffered socket reader."""
    chunks: list[bytes] = []
    remaining = size

    while remaining > 0:
        chunk = reader.read(remaining)

        if not chunk:
            raise cdp_error("cdp-disconnected", "the CDP websocket closed mid-frame")

        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)

def read_server_frame(reader) -> tuple[bool, int, bytes]:
    """Read and validate one server-to-client frame as (fin, opcode, payload).

    Every rule here is an RFC 6455 MUST for a server frame; anything else is a
    protocol error, never a value we try to interpret.
    """
    first, second = _read_exact(reader, 2)

    if first & 0x70:
        raise cdp_error("cdp-protocol", "the CDP websocket sent a frame with reserved bits set")

    fin = bool(first & 0x80)
    opcode = first & 0x0F

    if opcode not in KNOWN_OPCODES:
        raise cdp_error("cdp-protocol", "the CDP websocket sent an unknown opcode " + str(opcode))

    if second & 0x80:
        raise cdp_error("cdp-protocol", "the CDP websocket sent a masked server frame (RFC 6455 forbids masking server-to-client frames)")

    length = second & 0x7F

    if length == 126:
        length = int.from_bytes(_read_exact(reader, 2), "big")
    elif length == 127:
        extended = _read_exact(reader, 8)

        if extended[0] & 0x80:
            raise cdp_error("cdp-protocol", "the CDP websocket sent a 64-bit frame length with the high bit set")

        length = int.from_bytes(extended, "big")

    if opcode in CONTROL_OPCODES:
        if not fin:
            raise cdp_error("cdp-protocol", "the CDP websocket sent a fragmented control frame")

        if length > MAX_CONTROL_PAYLOAD:
            raise cdp_error("cdp-protocol", "the CDP websocket sent a control frame larger than 125 bytes")

    if length > MAX_MESSAGE_BYTES:
        raise cdp_error("cdp-protocol", "the CDP websocket frame is too large (" + str(length) + " bytes)")

    payload = _read_exact(reader, length) if length else b""
    return fin, opcode, payload

def _read_http_headers(reader) -> dict[str, str]:
    headers: dict[str, str] = {}

    while True:
        raw = reader.readline()

        if not raw:
            raise cdp_error("cdp-handshake", "the websocket handshake ended before the headers were complete")

        line = raw.decode("latin-1").strip()

        if not line:
            return headers

        name, separator, value = line.partition(":")

        if not separator:
            continue

        headers[name.strip().lower()] = value.strip()

class websocket_connection:
    """A client-side websocket connection: text frames plus ping/pong/close.

    Read and write share the buffered reader made from the socket, so no
    server byte can be lost between the HTTP upgrade response and the first
    frame.
    """

    def __init__(self, sock: socket.socket, reader) -> None:
        self.sock = sock
        self.reader = reader
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = DEFAULT_TIMEOUT) -> "websocket_connection":
        parts = urlsplit(str(url))

        if parts.scheme == "wss":
            raise cdp_error("cdp-unsupported", "wss:// CDP endpoints are not supported: WebView2 CDP is loopback ws:// and this client carries no TLS trust policy")

        if parts.scheme != "ws":
            raise cdp_error("cdp-protocol", "not a websocket URL: " + repr(url))

        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"

        if parts.query:
            path = path + "?" + parts.query

        key = websocket_key()

        try:
            sock = socket.create_connection((host, port), timeout = timeout)
            sock.settimeout(timeout)
            reader = sock.makefile("rb")
            request = "GET " + path + " HTTP/1.1" + CRLF + "Host: " + host + ":" + str(port) + CRLF + "Upgrade: websocket" + CRLF + "Connection: Upgrade" + CRLF + "Sec-WebSocket-Key: " + key + CRLF + "Sec-WebSocket-Version: 13" + CRLF + CRLF
            sock.sendall(request.encode("ascii"))
            status_line = reader.readline().decode("latin-1").strip()
            headers = _read_http_headers(reader)
        except OSError as exc:
            raise cdp_error("cdp-unreachable", "cannot open the CDP websocket " + str(url) + ": " + str(exc)) from exc

        status_tokens = status_line.split()

        if (len(status_tokens) < 2) or (status_tokens[1] != "101"):
            raise cdp_error("cdp-handshake", "the CDP websocket handshake was rejected: " + repr(status_line))

        received = headers.get("sec-websocket-accept", "")
        expected = websocket_accept(key)

        if received != expected:
            raise cdp_error("cdp-handshake", "the CDP websocket handshake returned a bad Sec-WebSocket-Accept (expected " + repr(expected) + ", got " + repr(received) + ")")

        return cls(sock, reader)

    def send_frame(self, payload: bytes, opcode: int) -> None:
        if self.closed:
            raise cdp_error("cdp-disconnected", "the CDP websocket is already closed")

        try:
            self.sock.sendall(encode_client_frame(payload, opcode))
        except OSError as exc:
            self.closed = True
            raise cdp_error("cdp-disconnected", "the CDP websocket send failed: " + str(exc)) from exc

    def send_text(self, text: str) -> None:
        self.send_frame(text.encode("utf-8"), OPCODE_TEXT)

    def recv_text(self) -> str:
        """Read one complete text message, answering pings on the way."""
        fragments: list[bytes] = []
        total = 0
        in_message = False

        while True:
            try:
                fin, opcode, payload = read_server_frame(self.reader)
            except socket.timeout as exc:
                raise cdp_error("cdp-timeout", "timed out waiting for a CDP websocket message") from exc
            except OSError as exc:
                self.closed = True
                raise cdp_error("cdp-disconnected", "the CDP websocket read failed: " + str(exc)) from exc

            if opcode == OPCODE_PING:
                self.send_frame(payload, OPCODE_PONG)
                continue

            if opcode == OPCODE_PONG:
                continue

            if opcode == OPCODE_CLOSE:
                self.closed = True
                raise cdp_error("cdp-disconnected", "the app closed the CDP websocket")

            if opcode == OPCODE_BINARY:
                raise cdp_error("cdp-protocol", "the CDP websocket sent a binary frame; CDP messages must be text")

            if opcode == OPCODE_CONTINUATION:
                if not in_message:
                    raise cdp_error("cdp-protocol", "the CDP websocket sent a continuation frame without a started message")

                fragments.append(payload)
                total += len(payload)

                if total > MAX_MESSAGE_BYTES:
                    raise cdp_error("cdp-protocol", "the CDP websocket message exceeds the size limit")

                if not fin:
                    continue

                message = b"".join(fragments)
                return message.decode("utf-8", "replace")

            if opcode == OPCODE_TEXT:
                if in_message:
                    raise cdp_error("cdp-protocol", "the CDP websocket started a new data frame inside a fragmented message")

                if fin:
                    return payload.decode("utf-8", "replace")

                in_message = True
                fragments = [payload]
                total = len(payload)
                continue

            raise cdp_error("cdp-protocol", "the CDP websocket sent an unsupported opcode " + str(opcode))

    def close(self) -> None:
        if not self.closed:
            self.closed = True

            try:
                self.sock.sendall(encode_client_frame(b"", OPCODE_CLOSE))
            except OSError:
                pass

        try:
            self.reader.close()
        except OSError:
            pass

        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "websocket_connection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

def http_json(port: int, path: str, timeout: float = 5.0) -> object:
    """GET a JSON document from the local CDP HTTP endpoint (proxy-free)."""
    port = int(port)

    if (port < 1) or (port > 65535):
        raise cdp_error("bad-port", "CDP port out of range: " + str(port))

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout = timeout)
    status = 0
    body = ""

    try:
        connection.request("GET", path, headers = {"Accept": "application/json"})
        response = connection.getresponse()
        status = response.status
        body = response.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        raise cdp_error("cdp-unreachable", "cannot reach the CDP endpoint on 127.0.0.1:" + str(port) + " (" + str(exc) + "); is the app running with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=" + str(port) + "?") from exc
    finally:
        connection.close()

    if status != 200:
        raise cdp_error("cdp-http", "the CDP endpoint " + str(path) + " returned HTTP " + str(status))

    try:
        return json.loads(body)
    except ValueError as exc:
        raise cdp_error("cdp-protocol", "the CDP endpoint " + str(path) + " returned non-JSON: " + repr(body[:120])) from exc

def list_targets(port: int, timeout: float = 5.0) -> list[dict]:
    """The CDP /json/list targets, filtered to JSON objects."""
    data = http_json(port, "/json/list", timeout = timeout)

    if not isinstance(data, list):
        raise cdp_error("cdp-protocol", "the CDP endpoint /json/list returned " + type(data).__name__ + ", not a list")

    return [item for item in data if isinstance(item, dict)]

def select_page_target(targets: list[dict], prefer_url: str = "", target_id: str = "") -> dict | None:
    """Pick the target to drive.

    An explicit target_id wins (whatever its type); otherwise devtools:// and
    non-page targets never win, a prefer_url prefix is honoured first, and an
    http(s)/tauri/file page is preferred over about:blank style placeholders.
    """

    if target_id:
        for item in targets:
            if str(item.get("id", "")) == str(target_id):
                return item

        return None

    pages = [item for item in targets if (item.get("type") == "page") and not str(item.get("url", "")).startswith("devtools://")]

    if prefer_url:
        for page in pages:
            if str(page.get("url", "")).startswith(prefer_url):
                return page

    for page in pages:
        if str(page.get("url", "")).startswith(("http://", "https://", "tauri://", "file://")):
            return page

    return pages[0] if pages else None

def wait_for_page(port: int, timeout: float = DEFAULT_TIMEOUT, interval: float = 0.1, prefer_url: str = "", target_id: str = "") -> tuple[dict, int]:
    """Poll /json/list until a page target exists; return (page, elapsed_ms)."""
    started = time.monotonic()
    last_error: cdp_error | None = None

    while True:
        try:
            page = select_page_target(list_targets(port), prefer_url, target_id)
        except cdp_error as exc:
            page = None
            last_error = exc

        if page is not None:
            return page, int((time.monotonic() - started) * 1000)

        if (time.monotonic() - started) >= timeout:
            suffix = (" (last error: " + str(last_error) + ")") if last_error is not None else ""
            wanted = (" target " + repr(target_id)) if target_id else " page target"
            raise cdp_error("cdp-timeout", "no CDP" + wanted + " appeared on 127.0.0.1:" + str(int(port)) + " within " + format(timeout, "g") + "s" + suffix)

        time.sleep(interval)

def _exception_text(exception_details: dict) -> str:
    exception = exception_details.get("exception") or {}
    text = exception_details.get("text") or exception.get("description") or exception.get("value")
    return str(text if text is not None else "the evaluated JavaScript raised an exception")

class cdp_client:
    """One CDP session bound to a page target.

    Requests are matched by id; protocol events that arrive while waiting are
    buffered in events (bounded) instead of being misread as responses.
    """

    def __init__(self, connection: websocket_connection, page: dict, port: int) -> None:
        self.connection = connection
        self.page = page
        self.port = int(port)
        self.next_id = 0
        self.events: list[dict] = []

    @classmethod
    def connect(cls, port: int, target: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> "cdp_client":
        page = target if target is not None else wait_for_page(port, timeout = timeout)[0]
        url = str(page.get("webSocketDebuggerUrl", ""))

        if not url:
            raise cdp_error("cdp-protocol", "the selected CDP target has no webSocketDebuggerUrl")

        connection = websocket_connection.connect(url, timeout = timeout)
        return cls(connection, page, port)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.connection.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))

        while True:
            raw = self.connection.recv_text()

            try:
                message = json.loads(raw)
            except ValueError as exc:
                raise cdp_error("cdp-protocol", "the CDP websocket sent non-JSON: " + repr(raw[:200])) from exc

            if not isinstance(message, dict):
                raise cdp_error("cdp-protocol", "the CDP websocket sent a non-object message: " + repr(raw[:200]))

            if message.get("id") == message_id:
                error = message.get("error")

                if error:
                    message_text = error.get("message") if isinstance(error, dict) else error
                    raise cdp_error("cdp-error", "CDP " + method + " failed: " + str(message_text), {"method": method, "cdp_error": error})

                result = message.get("result")
                return result if isinstance(result, dict) else {}

            if "method" in message:
                self.events.append({"method": message.get("method"), "params": message.get("params") or {}})

                if len(self.events) > 1000:
                    del self.events[:500]

    def evaluate(self, expression: str, await_promise: bool = True, return_by_value: bool = True, user_gesture: bool = True) -> dict:
        """Runtime.evaluate with the desktop-debug defaults; returns the result object."""
        params = {"expression": expression, "awaitPromise": await_promise, "returnByValue": return_by_value, "userGesture": user_gesture}
        result = self.request("Runtime.evaluate", params)

        if result.get("exceptionDetails"):
            raise cdp_error("js-exception", _exception_text(result["exceptionDetails"]), {"exception": result["exceptionDetails"]})

        inner = result.get("result")
        return inner if isinstance(inner, dict) else {}

    def evaluate_value(self, expression: str, await_promise: bool = True, return_by_value: bool = True, user_gesture: bool = True) -> object:
        """Runtime.evaluate returning the JSON value only."""
        result = self.evaluate(expression, await_promise = await_promise, return_by_value = return_by_value, user_gesture = user_gesture)
        return result.get("value")

    def screenshot(self, image_format: str = "png", quality: int | None = None) -> bytes:
        """Page.captureScreenshot returned as raw image bytes."""
        self.request("Page.enable")
        params: dict = {"format": image_format}

        if quality is not None:
            params["quality"] = int(quality)

        result = self.request("Page.captureScreenshot", params)
        data = result.get("data")

        if not data:
            raise cdp_error("cdp-protocol", "Page.captureScreenshot returned no image data")

        return base64.b64decode(str(data))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "cdp_client":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
