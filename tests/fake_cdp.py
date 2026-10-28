# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Test doubles for the CDP client: a loopback HTTP+websocket CDP server.

No real desktop app is needed. The server answers /json/list and the CDP
methods app-drive uses, records every request for shape assertions, and can
run scripted responses (values, protocol errors, JS exceptions). The frame
codec here is written independently of the production codec so a bug in one
cannot make the other agree with it.
"""
from __future__ import annotations
import base64
import hashlib
import json
import socketserver
import struct
import threading

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CRLF = (chr(13) + chr(10)).encode("ascii")

def page_target(target_id = "page-1", url = "http://127.0.0.1:5173/", target_type = "page", title = "Fake Chest") -> dict:
    return {"id": target_id, "type": target_type, "title": title, "url": url, "webSocketDebuggerUrl": None}

def fake_png(width: int, height: int) -> bytes:
    signature = bytes.fromhex("89504e470d0a1a0a")
    ihdr = struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", int(width), int(height)) + bytes([8, 6, 0, 0, 0])
    return signature + ihdr + b"payload-not-a-real-png"

def read_client_frame(reader):
    header = reader.read(2)

    if len(header) < 2:
        return None

    first, second = header[0], header[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F

    if length == 126:
        length = struct.unpack("!H", reader.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", reader.read(8))[0]

    mask = reader.read(4) if masked else b""
    payload = reader.read(length) if length else b""

    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

    return fin, opcode, payload

def encode_server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """Server frames are unmasked (RFC 6455), with an optional FIN bit."""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)

    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)

    return bytes(header) + payload

def websocket_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")

def websocket_handshake_response(key: str) -> bytes:
    accept = websocket_accept(key)
    return b"HTTP/1.1 101 Switching Protocols" + CRLF + b"Upgrade: websocket" + CRLF + b"Connection: Upgrade" + CRLF + b"Sec-WebSocket-Accept: " + accept.encode("ascii") + CRLF + CRLF

def remote_object_reply(value) -> dict:
    """The top-level spec for a successful Runtime.evaluate reply."""
    remote = {"type": "object" if isinstance(value, dict) else "string", "value": value}
    return {"result": {"result": remote}}

DEFAULT_PAGE_STATE = {"ready_state": "complete", "title": "Fake Chest", "url": "http://127.0.0.1:5173/", "boot_chrome": "claimed", "has_bar": False, "bar_driver": None, "degraded": None, "body_text_len": 5, "body_text_head": "login", "tauri": True, "tauri_internals": True, "root_children": 1}

class _fake_handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self._serve()
        except (OSError, ValueError):
            pass  # a test client that hung up mid-reply is not a test failure

    def _serve(self) -> None:
        server = self.server.fake
        request_line = self.rfile.readline().decode("latin-1").strip()
        headers: dict[str, str] = {}

        while True:
            raw = self.rfile.readline()

            if (not raw) or (raw == CRLF):
                break

            name, separator, value = raw.decode("latin-1").partition(":")

            if separator:
                headers[name.strip().lower()] = value.strip()

        parts = request_line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"

        if path == "/json/version":
            self._json({"Browser": "FakeWebView2/1.0"})
            return

        if path == "/json/list":
            self._json(server.targets())
            return

        if headers.get("upgrade", "").lower() == "websocket":
            self._websocket(server, headers)
            return

        self._status(404, b"not found")

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        headers = b"HTTP/1.1 200 OK" + CRLF + b"Content-Type: application/json" + CRLF + b"Content-Length: " + str(len(body)).encode("ascii") + CRLF + CRLF
        self.wfile.write(headers + body)
        self.wfile.flush()

    def _status(self, code: int, body: bytes) -> None:
        self.wfile.write(("HTTP/1.1 " + str(code) + " X").encode("ascii") + CRLF + b"Content-Length: " + str(len(body)).encode("ascii") + CRLF + CRLF + body)
        self.wfile.flush()

    def _websocket(self, server, headers) -> None:
        self.wfile.write(websocket_handshake_response(headers.get("sec-websocket-key", "")))
        self.wfile.flush()

        while True:
            frame = read_client_frame(self.rfile)

            if frame is None:
                return

            _fin, opcode, payload = frame
            server.received_opcodes.append(opcode)

            if opcode == 0x8:
                return

            if opcode in (0x9, 0xA):
                continue

            message = json.loads(payload.decode("utf-8", "replace"))
            server.messages.append(message)

            if server.send_ping_first:
                server.send_ping_first = False
                self.wfile.write(encode_server_frame(b"fakeping", 0x9))
                self.wfile.flush()

            body = {"id": message.get("id")}
            body.update(server.next_response(str(message.get("method"))))
            self.wfile.write(encode_server_frame(json.dumps(body).encode("utf-8")))
            self.wfile.flush()

class fake_cdp_server:
    """Threaded loopback CDP endpoint; records requests, scripts responses."""

    def __init__(self, targets = None):
        self.page_state = dict(DEFAULT_PAGE_STATE)
        self.responses: dict[str, list] = {}
        self.messages: list[dict] = []
        self.received_opcodes: list[int] = []
        self.screenshot_bytes = fake_png(8, 4)
        self.send_ping_first = False
        self._targets = list(targets) if targets is not None else [page_target()]
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _fake_handler, bind_and_activate = True)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target = self.httpd.serve_forever, daemon = True)
        self.started = False

    def start(self):
        if not self.started:
            self.thread.start()
            self.started = True

        return self

    def stop(self) -> None:
        if self.started:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

    def targets(self) -> list[dict]:
        rendered: list[dict] = []

        for target in self._targets:
            item = dict(target)

            if item.get("webSocketDebuggerUrl") is None:
                item["webSocketDebuggerUrl"] = "ws://127.0.0.1:" + str(self.port) + "/devtools/page/" + str(item.get("id"))

            rendered.append(item)

        return rendered

    def queue_response(self, method: str, spec: dict) -> None:
        self.responses.setdefault(method, []).append(spec)

    def queue_evaluate(self, *values) -> None:
        for value in values:
            self.queue_response("Runtime.evaluate", remote_object_reply(value))

    def queue_exception(self, text: str) -> None:
        details = {"text": text, "exception": {"description": text}}
        self.queue_response("Runtime.evaluate", {"result": {"exceptionDetails": details}})

    def next_response(self, method: str) -> dict:
        queue = self.responses.get(method)

        if queue:
            return queue.pop(0)

        if method == "Runtime.evaluate":
            return remote_object_reply(dict(self.page_state))

        if method == "Page.captureScreenshot":
            return {"result": {"data": base64.b64encode(self.screenshot_bytes).decode("ascii")}}

        return {"result": {}}
