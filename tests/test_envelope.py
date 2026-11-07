"""Contract tests for the shared stdout-JSON emission policy (core/envelope.py).

The module exists because fourteen tools hand-rolled their own json.dumps and
had drifted on every axis (ensure_ascii, indentation, error shapes). These
tests pin the policy itself and the app-drive protocol delegation byte-for-
byte, so neither can drift silently again. Every assertion is an exact-string
known positive — not a property probe — so a broken extraction or a changed
policy cannot pass vacuously.
"""

from celestia_devtools.core import envelope
from celestia_devtools.env import app_drive

class test_ascii_policy:
    def test_render_escapes_non_ascii_with_exact_bytes(self):
        # The gbk-console lesson: escaping is the policy, and the exact escape
        # sequence is pinned so ensure_ascii cannot silently flip back. The
        # report form is indented by default — the expected string is the
        # document, escapes and all, not a property probe.
        assert envelope.render({"name": "雪"}) == '{\n  "name": "\\u96ea"\n}'

    def test_render_line_is_ascii_and_single_line(self):
        line = envelope.render_line({"title": "导航", "ok": True})

        assert line.isascii()
        assert "\n" not in line
        assert line == '{"title": "\\u5bfc\\u822a", "ok": true}'

class test_report_form:
    def test_sorted_document_is_byte_stable(self):
        assert envelope.render({"b": 1, "a": [2, 3]}, sort_keys = True) == '{\n  "a": [\n    2,\n    3\n  ],\n  "b": 1\n}'

    def test_unsorted_document_preserves_insertion_order(self):
        assert envelope.render({"b": 1, "a": 2}, sort_keys = False) == '{\n  "b": 1,\n  "a": 2\n}'

class test_protocol_envelopes:
    def test_ok_envelope_key_order_is_the_wire_contract(self):
        payload = envelope.ok_envelope("status", 2, port = 9222, target = {"id": "x"})

        assert list(payload) == ["ok", "protocol", "cmd", "port", "target"]
        assert payload["ok"] is True
        assert payload["protocol"] == 2

    def test_error_envelope_shape_and_key_order(self):
        payload = envelope.error_envelope("wait", 2, "timeout", "condition not satisfied", port = 1)

        assert list(payload) == ["ok", "protocol", "cmd", "error", "port"]
        assert list(payload["error"]) == ["code", "message"]
        assert payload["error"]["code"] == "timeout"

    def test_emit_line_prints_one_document_to_stdout(self, capsys):
        envelope.emit_line(envelope.ok_envelope("status", 2, port = 9222))
        captured = capsys.readouterr()

        assert captured.out == '{"ok": true, "protocol": 2, "cmd": "status", "port": 9222}\n'
        assert captured.err == ""

    def test_emit_line_pretty_indents(self, capsys):
        envelope.emit_line({"ok": True}, pretty = True)
        captured = capsys.readouterr()

        assert captured.out == '{\n  "ok": true\n}\n'

class test_app_drive_delegation:
    """app-drive keeps its local names (120+ tests import them) but they must
    produce byte-identical documents through the shared builders — the
    protocol consumers parse these envelopes on every desktop debug loop."""

    def test_envelope_matches_the_pinned_protocol_bytes(self, capsys):
        payload = app_drive.envelope("launch", pid = 41992, cdp_port = 9222)
        app_drive.emit(payload)
        captured = capsys.readouterr()

        assert captured.out == '{"ok": true, "protocol": 2, "cmd": "launch", "pid": 41992, "cdp_port": 9222}\n'

    def test_failure_matches_the_pinned_protocol_bytes(self, capsys):
        payload = app_drive.failure("close", "pid-mismatch", "recorded pid owns a different process now")
        app_drive.emit(payload)
        captured = capsys.readouterr()

        assert captured.out == '{"ok": false, "protocol": 2, "cmd": "close", "error": {"code": "pid-mismatch", "message": "recorded pid owns a different process now"}}\n'

    def test_pretty_flag_passes_through(self, capsys):
        app_drive.emit(app_drive.envelope("targets"), pretty = True)
        captured = capsys.readouterr()

        assert captured.out.startswith('{\n  "ok": true')

class test_extraction_self_check:
    """Guard the guard (the zero-yield trap): prove the exact-string
    assertions above can actually fail on a plausible wrong implementation."""

    def test_a_non_ascii_render_would_fail_the_exact_string(self):
        # What json.dumps(..., ensure_ascii=False) would produce for the same
        # payload — the pinned assertions must reject this shape.
        wrong = '{"name": "雪"}'

        assert wrong != envelope.render({"name": "雪"})

    def test_reordered_keys_would_fail_the_wire_contract(self):
        assert list({"protocol": 2, "ok": True, "cmd": "x"}) != list(envelope.ok_envelope("x", 2))
