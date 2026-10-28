# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Tests for the stdlib CDP transport (env/cdp.py) - no real app required.

The frame codec is pinned against RFC 6455 vectors, an independently written
server-side codec and deliberately malformed server frames (masked, reserved
bits, oversized control frames, bad fragmentation). Request shaping is pinned
against the JSON the fake server records.
"""
from __future__ import annotations
import io
import json
import socket
import threading
import time
import pytest
from celestia_devtools.env import cdp
from fake_cdp import CRLF, encode_server_frame, fake_cdp_server, fake_png, page_target, websocket_handshake_response

class test_handshake_math:
    def test_rfc6455_accept_vector(self):
        assert cdp.websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_client_key_is_16_random_bytes_base64(self):
        first = cdp.websocket_key()
        second = cdp.websocket_key()
        assert first != second
        assert len(first) == 24

class test_client_frame_encoding:
    def test_small_payload_is_masked_and_marked_text(self):
        frame = cdp.encode_client_frame(b"hello", cdp.OPCODE_TEXT)
        assert frame[0] == 0x81
        assert (frame[1] & 0x80) == 0x80
        assert (frame[1] & 0x7F) == 5
        mask = frame[2:6]
        payload = frame[6:]
        assert bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)) == b"hello"

    def test_medium_payload_uses_16_bit_length(self):
        frame = cdp.encode_client_frame(b"x" * 300)
        assert (frame[1] & 0x7F) == 126
        assert int.from_bytes(frame[2:4], "big") == 300
        assert len(frame) == (2 + 2 + 4 + 300)

    def test_large_payload_uses_64_bit_length(self):
        frame = cdp.encode_client_frame(b"y" * 70000)
        assert (frame[1] & 0x7F) == 127
        assert int.from_bytes(frame[2:10], "big") == 70000
        assert len(frame) == (2 + 8 + 4 + 70000)

class test_read_server_frame:
    def test_unmasked_text_frame(self):
        fin, opcode, payload = cdp.read_server_frame(io.BytesIO(bytes([0x81, 0x05]) + b"hello"))
        assert fin is True
        assert opcode == cdp.OPCODE_TEXT
        assert payload == b"hello"

    def test_masked_server_frame_is_rejected(self):
        mask = bytes([1, 2, 3, 4])
        masked_payload = bytes([0x81, 0x80 | 5]) + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(b"hello"))

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(masked_payload))

        assert caught.value.code == "cdp-protocol"
        assert "masked" in str(caught.value)

    def test_reserved_bits_are_rejected(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(bytes([0xC1, 0x01]) + b"x"))

        assert "reserved" in str(caught.value)

    def test_unknown_opcode_is_rejected(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(bytes([0x83, 0x02]) + b"{}"))

        assert "opcode" in str(caught.value)

    def test_fragmented_control_frame_is_rejected(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(bytes([0x09, 0x00])))

        assert "control" in str(caught.value)

    def test_oversized_control_frame_is_rejected(self):
        frame = bytes([0x89, 126]) + int.to_bytes(200, 2, "big") + (b"p" * 200)

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(frame))

        assert "control" in str(caught.value)

    def test_64_bit_length_with_high_bit_is_rejected(self):
        frame = bytes([0x81, 127]) + bytes([0x80, 0, 0, 0, 0, 0, 0, 1])

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(frame))

        assert "64-bit" in str(caught.value)

    def test_16_bit_length_frame(self):
        payload = b"z" * 200
        data = bytes([0x81, 126]) + int.to_bytes(200, 2, "big") + payload
        _fin, opcode, value = cdp.read_server_frame(io.BytesIO(data))
        assert opcode == cdp.OPCODE_TEXT
        assert value == payload

class test_message_reassembly:
    def test_fragmented_text_message_is_joined(self):
        server_port = _scripted_ws_server(encode_server_frame(b"frag-", cdp.OPCODE_TEXT, fin = False) + encode_server_frame(b"mented", cdp.OPCODE_CONTINUATION, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            assert connection.recv_text() == "frag-mented"
        finally:
            connection.close()

    def test_stray_continuation_is_rejected(self):
        server_port = _scripted_ws_server(encode_server_frame(b"orphan", cdp.OPCODE_CONTINUATION, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                connection.recv_text()

            assert "continuation" in str(caught.value)
        finally:
            connection.close()

    def test_data_frame_inside_fragmented_message_is_rejected(self):
        server_port = _scripted_ws_server(encode_server_frame(b"start", cdp.OPCODE_TEXT, fin = False) + encode_server_frame(b"again", cdp.OPCODE_TEXT, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                connection.recv_text()

            assert "fragmented" in str(caught.value)
        finally:
            connection.close()

    def test_binary_message_is_rejected(self):
        server_port = _scripted_ws_server(encode_server_frame(b"{}", cdp.OPCODE_BINARY, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                connection.recv_text()

            assert "binary" in str(caught.value)
        finally:
            connection.close()

    def test_large_unmasked_server_frame_roundtrip(self):
        payload = ("x" * 70000).encode("utf-8")
        server_port = _scripted_ws_server(encode_server_frame(payload, cdp.OPCODE_TEXT, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            assert connection.recv_text() == ("x" * 70000)
        finally:
            connection.close()

class test_websocket_connection:
    def test_ping_is_answered_with_pong(self):
        with fake_cdp_server() as server:
            server.send_ping_first = True
            connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server.port) + "/devtools/page/page-1")

            try:
                connection.send_text(json.dumps({"id": 7, "method": "Page.enable", "params": {}}))
                response = json.loads(connection.recv_text())
                assert response["id"] == 7
                deadline = time.monotonic() + 2

                while (cdp.OPCODE_PONG not in server.received_opcodes) and (time.monotonic() < deadline):
                    time.sleep(0.01)

                assert cdp.OPCODE_PONG in server.received_opcodes
            finally:
                connection.close()

    def test_bad_accept_header_is_rejected(self):
        port = _raw_server(b"HTTP/1.1 101 Switching Protocols" + CRLF + b"Upgrade: websocket" + CRLF + b"Sec-WebSocket-Accept: not-the-accept" + CRLF + CRLF)

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.websocket_connection.connect("ws://127.0.0.1:" + str(port) + "/x", timeout = 2)

        assert caught.value.code == "cdp-handshake"

    def test_non_101_status_is_rejected(self):
        port = _raw_server(b"HTTP/1.1 403 Forbidden" + CRLF + CRLF)

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.websocket_connection.connect("ws://127.0.0.1:" + str(port) + "/x", timeout = 2)

        assert caught.value.code == "cdp-handshake"

    def test_wss_is_rejected_without_tls_trust(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.websocket_connection.connect("wss://127.0.0.1:1/x", timeout = 2)

        assert caught.value.code == "cdp-unsupported"

    def test_unreachable_websocket_is_reported(self):
        port = _free_port()

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.websocket_connection.connect("ws://127.0.0.1:" + str(port) + "/x", timeout = 0.5)

        assert caught.value.code in ("cdp-unreachable", "cdp-timeout")

class test_discovery:
    def test_json_list_and_target_selection(self):
        targets = [page_target("devtools-1", "devtools://devtools/bundled/inspector.html", "page", "DevTools"), page_target("page-7", "http://localhost:5173/@app")]

        with fake_cdp_server(targets) as server:
            listed = cdp.list_targets(server.port)
            assert [item["id"] for item in listed] == ["devtools-1", "page-7"]
            assert cdp.select_page_target(listed)["id"] == "page-7"

    def test_target_id_selection_wins_even_for_devtools(self):
        targets = [page_target("devtools-1", "devtools://x", "page", "DevTools"), page_target("page-7", "http://localhost:5173/")]
        assert cdp.select_page_target(targets, target_id = "devtools-1")["id"] == "devtools-1"
        assert cdp.select_page_target(targets, target_id = "missing") is None

    def test_prefer_url_wins_over_target_order(self):
        targets = [page_target("page-a", "http://localhost:5173/@first"), page_target("page-b", "http://localhost:5173/@second")]
        assert cdp.select_page_target(targets, prefer_url = "http://localhost:5173/@second")["id"] == "page-b"

    def test_wait_for_page_returns_page_and_elapsed(self):
        with fake_cdp_server() as server:
            page, elapsed = cdp.wait_for_page(server.port, timeout = 5, interval = 0.02)
            assert page["id"] == "page-1"
            assert elapsed >= 0

    def test_wait_for_page_honours_target_id(self):
        targets = [page_target("page-a", "http://localhost:5173/a"), page_target("page-b", "http://localhost:5173/b")]

        with fake_cdp_server(targets) as server:
            page, _elapsed = cdp.wait_for_page(server.port, timeout = 2, interval = 0.02, target_id = "page-b")
            assert page["id"] == "page-b"

    def test_wait_for_page_times_out_without_a_page(self):
        with fake_cdp_server(targets = []) as server:
            with pytest.raises(cdp.cdp_error) as caught:
                cdp.wait_for_page(server.port, timeout = 0.3, interval = 0.02)

            assert caught.value.code == "cdp-timeout"

    def test_unreachable_http_endpoint_is_reported(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.list_targets(_free_port(), timeout = 0.5)

        assert caught.value.code == "cdp-unreachable"

    def test_non_list_json_is_rejected(self):
        port = _raw_server(b"HTTP/1.1 200 OK" + CRLF + b"Content-Type: application/json" + CRLF + b"Content-Length: 2" + CRLF + CRLF + b"{}")

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.list_targets(port, timeout = 2)

        assert caught.value.code == "cdp-protocol"

    def test_truncated_http_response_is_wrapped(self):
        port = _raw_server(b"HTTP/1.1 200 OK" + CRLF + b"Content-Length: 100" + CRLF + CRLF + b"short")

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.list_targets(port, timeout = 2)

        assert caught.value.code == "cdp-unreachable"

    def test_out_of_range_port_is_rejected(self):
        with pytest.raises(cdp.cdp_error) as caught:
            cdp.list_targets(70000, timeout = 0.2)

        assert caught.value.code == "bad-port"

class test_cdp_client:
    def test_evaluate_request_shaping_and_defaults(self):
        with fake_cdp_server() as server:
            client = cdp.cdp_client.connect(server.port)

            try:
                value = client.evaluate_value("1 + 1", await_promise = False)
                assert value == server.page_state
                message = server.messages[-1]
                assert message["method"] == "Runtime.evaluate"
                assert message["params"] == {"expression": "1 + 1", "awaitPromise": False, "returnByValue": True, "userGesture": True}
            finally:
                client.close()

    def test_screenshot_returns_raw_bytes(self):
        with fake_cdp_server() as server:
            client = cdp.cdp_client.connect(server.port)

            try:
                data = client.screenshot()
                assert data == fake_png(8, 4)
                assert data[:8] == bytes.fromhex("89504e470d0a1a0a")
                assert server.messages[-1] == {"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png"}}
            finally:
                client.close()

    def test_js_exception_carries_details(self):
        with fake_cdp_server() as server:
            server.queue_exception("boom")
            client = cdp.cdp_client.connect(server.port)

            try:
                with pytest.raises(cdp.cdp_error) as caught:
                    client.evaluate("throw new Error('boom')")

                assert caught.value.code == "js-exception"
                assert "boom" in caught.value.details["exception"]["text"]
            finally:
                client.close()

    def test_protocol_error_is_surfaced(self):
        with fake_cdp_server() as server:
            server.queue_response("Runtime.evaluate", {"error": {"code": -32000, "message": "not allowed"}})
            client = cdp.cdp_client.connect(server.port)

            try:
                with pytest.raises(cdp.cdp_error) as caught:
                    client.evaluate("1")

                assert caught.value.code == "cdp-error"
                assert "not allowed" in str(caught.value)
            finally:
                client.close()

class test_resource_limits:
    def test_oversized_single_frame_is_rejected(self, monkeypatch):
        monkeypatch.setattr(cdp, "MAX_MESSAGE_BYTES", 10)
        frame = bytes([0x81, 20]) + (b"x" * 20)

        with pytest.raises(cdp.cdp_error) as caught:
            cdp.read_server_frame(io.BytesIO(frame))

        assert "too large" in str(caught.value)

    def test_cumulative_fragments_are_capped(self, monkeypatch):
        monkeypatch.setattr(cdp, "MAX_MESSAGE_BYTES", 10)
        server_port = _scripted_ws_server(encode_server_frame(b"123456", cdp.OPCODE_TEXT, fin = False) + encode_server_frame(b"789012", cdp.OPCODE_CONTINUATION, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                connection.recv_text()

            assert "size limit" in str(caught.value)
        finally:
            connection.close()

class test_close_frame:
    def test_server_close_frame_reports_disconnect(self):
        server_port = _scripted_ws_server(encode_server_frame(b"", cdp.OPCODE_CLOSE, fin = True))
        connection = cdp.websocket_connection.connect("ws://127.0.0.1:" + str(server_port) + "/x", timeout = 2)

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                connection.recv_text()

            assert caught.value.code == "cdp-disconnected"
        finally:
            connection.close()

def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port

def _raw_server(response: bytes) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        connection, _address = listener.accept()
        connection.recv(65536)
        connection.sendall(response)
        connection.close()
        listener.close()

    threading.Thread(target = serve, daemon = True).start()
    return port

def _scripted_ws_server(frames: bytes) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        connection, _address = listener.accept()
        connection.settimeout(5)
        request = b""

        while b"Sec-WebSocket-Key:" not in request:
            chunk = connection.recv(4096)

            if not chunk:
                break

            request += chunk

        key = ""

        for line in request.decode("latin-1").split(chr(13) + chr(10)):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()

        connection.sendall(websocket_handshake_response(key))
        connection.sendall(frames)

        try:
            connection.recv(65536)
        except OSError:
            pass

        connection.close()
        listener.close()

    threading.Thread(target = serve, daemon = True).start()
    return port
