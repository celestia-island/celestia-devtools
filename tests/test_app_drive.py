# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Tests for the app-drive CLI (env/app_drive.py) - no real desktop app required.

Covered: argument parsing, CDP-only launch environment, non-mutating attach
port resolution, state schema/keys, the S0 process-identity contract
(fail-closed, creation-time mismatch, wrong path, replace-exe mismatch),
launch/cleanup behaviour (registration failure, kill failure keeps state,
Ctrl+C), ASCII envelopes under a gbk stdout, close safety, and the JSON
protocol of every subcommand against a fake CDP endpoint.
"""
from __future__ import annotations
import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
import pytest
from celestia_devtools.env import app_drive
from celestia_devtools.env import cdp
from celestia_devtools.env import ports
from fake_cdp import fake_cdp_server, page_target

@pytest.fixture()
def drive_state(tmp_path, monkeypatch):
    monkeypatch.setenv(app_drive.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("CELESTIA_PORTS_REGISTRY", str(tmp_path / "ports.json"))
    return tmp_path / "state"

def run_cli(capsys, *args):
    code = app_drive.main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured

def attach_args(**overrides):
    values = {"port": None, "app": None, "state_file": None, "prefer_url": "", "target": None, "timeout": 2.0, "pretty": False}
    values.update(overrides)
    return types.SimpleNamespace(**values)

def spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, start_new_session = os.name != "nt")

def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()

    try:
        child.wait(timeout = 10)
    except subprocess.TimeoutExpired:
        pass

def state_for(pid: int, app: str = "demo", port: int = 9444, exe = None, token = None, kind = None) -> dict:
    identity = app_drive.read_process_identity(pid)
    recorded = identity or {}
    return {"schema": app_drive.STATE_SCHEMA, "protocol": app_drive.PROTOCOL_VERSION, "app": app, "pid": int(pid), "exe": exe if exe is not None else str(recorded.get("image") or sys.executable), "create_token": token if token is not None else str(recorded.get("create_token") or ""), "create_kind": kind if kind is not None else str(recorded.get("create_kind") or ""), "cdp_port": port, "cwd": str(Path.cwd()), "launched_at": "test", "log_dir": None, "command": []}

def record_state(state: dict) -> None:
    app_drive.write_state(state, app_drive.state_file_path(state["app"]))

class test_argument_parsing:
    def test_help_exits_zero(self):
        with pytest.raises(SystemExit) as caught:
            app_drive.main(["--help"])

        assert caught.value.code == 0

    def test_no_command_is_usage_error(self):
        with pytest.raises(SystemExit) as caught:
            app_drive.main([])

        assert caught.value.code == 2

    def test_launch_collects_app_arguments_after_separator(self):
        parser = app_drive.build_parser()
        namespace = parser.parse_args(["launch", "app.exe", "--app", "demo", "--", "--window", "second", "--flag"])
        assert namespace.extra == ["--window", "second", "--flag"]
        assert namespace.app == "demo"
        assert namespace.exe == "app.exe"

    def test_wait_requires_exactly_one_condition(self):
        parser = app_drive.build_parser()

        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["wait", "--port", "1", "--selector", "x", "--boot-chrome", "claimed"])

        assert caught.value.code == 2

    def test_wait_accepts_each_condition(self):
        parser = app_drive.build_parser()
        assert parser.parse_args(["wait", "--port", "1", "--selector", "x"]).selector == "x"
        assert parser.parse_args(["wait", "--port", "1", "--expr", "flag"]).expression == "flag"
        assert parser.parse_args(["wait", "--port", "1", "--boot-chrome", "claimed"]).boot_chrome == "claimed"

    def test_eval_collects_expression_tokens(self):
        parser = app_drive.build_parser()
        namespace = parser.parse_args(["eval", "--port", "1", "1 +", "1"])
        assert namespace.expression == ["1 +", "1"]

    def test_every_subcommand_has_a_handler(self):
        assert set(app_drive.HANDLERS) == {"targets", "status", "eval", "shot", "click", "wait", "watch", "launch", "close"}

    def test_close_accepts_exe_for_key_derivation(self):
        parser = app_drive.build_parser()
        namespace = parser.parse_args(["close", "--exe", sys.executable])
        assert namespace.exe == sys.executable
        assert namespace.app is None

class test_launch_environment:
    def test_webview2_line_replaces_only_the_debugging_port(self):
        assert app_drive.merge_webview2_arguments("--disable-gpu --remote-debugging-port=1 --foo", 9333) == "--disable-gpu --foo --remote-debugging-port=9333"

    def test_webview2_line_is_created_when_absent(self):
        assert app_drive.merge_webview2_arguments("", 9222) == "--remote-debugging-port=9222"

    def test_launch_env_is_cdp_only(self):
        environment = app_drive.build_launch_env({"PATH": "x", app_drive.CDP_ENV_VAR: "--flag"}, 9333, ["EXTRA=1"])
        assert environment["PATH"] == "x"
        assert environment[app_drive.CDP_ENV_VAR] == "--flag --remote-debugging-port=9333"
        assert environment[app_drive.CDP_PORT_ENV] == "9333"
        assert "CELESTIA_VITE_PORT" not in environment
        assert environment["EXTRA"] == "1"

    def test_env_override_rejects_malformed_values(self):
        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.parse_env_overrides(["NO_EQUALS"])

        assert caught.value.code == "bad-env"

    def test_launch_command_passes_extra_arguments(self):
        assert app_drive.build_launch_command("app.exe", ["--", "--window", "two"]) == ["app.exe", "--window", "two"]
        assert app_drive.build_launch_command("app.exe", []) == ["app.exe"]

class test_state_files:
    def test_round_trip(self, drive_state):
        path = app_drive.state_file_path("shittim-chest")
        app_drive.write_state({"schema": app_drive.STATE_SCHEMA, "app": "shittim-chest", "pid": 1, "cdp_port": 2}, path)
        assert app_drive.read_state("shittim-chest")["cdp_port"] == 2
        assert app_drive.remove_state(path) is True
        assert app_drive.read_state("shittim-chest") is None

    def test_write_is_atomic(self, drive_state):
        path = app_drive.state_file_path("demo")
        app_drive.write_state({"schema": app_drive.STATE_SCHEMA, "app": "demo", "pid": 1, "cdp_port": 2}, path)
        leftovers = [item.name for item in path.parent.iterdir() if ".tmp" in item.name]
        assert leftovers == []

    def test_missing_state_is_none(self, drive_state):
        assert app_drive.read_state("nobody") is None

    def test_old_schema_is_refused(self, drive_state):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": 1, "port": 9333}), encoding = "utf-8")

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.read_state("demo")

        assert caught.value.code == "state-outdated"

    def test_app_slug_sanitizes_path_separators(self):
        assert app_drive.app_slug("shittim/chest") == "shittim_chest"
        assert app_drive.app_slug("") == "app"

    def test_state_root_honours_the_env_override(self, drive_state):
        assert app_drive.state_root() == drive_state

    def test_app_key_is_a_hash_of_the_exe_path(self):
        first = app_drive.default_app_key(sys.executable)
        second = app_drive.default_app_key(str(Path(sys.executable).resolve()))
        assert first == second
        assert first.startswith(app_drive.APP_KEY_PREFIX)
        assert len(first) == (len(app_drive.APP_KEY_PREFIX) + 12)
        assert app_drive.default_app_key("other-place/other.exe") != first

class test_process_identity:
    def test_own_process_identity_is_readable(self):
        identity = app_drive.read_process_identity(os.getpid())

        if identity is None:
            pytest.skip("this platform cannot expose process identity")

        assert identity["image"]
        assert identity["create_token"]
        assert identity["create_kind"] in ("windows-filetime", "proc-starttime")

    def test_verify_accepts_a_matching_process(self):
        identity = app_drive.read_process_identity(os.getpid())

        if identity is None:
            pytest.skip("this platform cannot expose process identity")

        state = state_for(os.getpid())
        assert app_drive.verify_process_identity(state, os.getpid())["create_token"] == identity["create_token"]

    def test_missing_token_fails_closed(self):
        state = state_for(os.getpid())
        state["create_token"] = ""

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.verify_process_identity(state, os.getpid())

        assert caught.value.code == "identity-unavailable"

    def test_missing_kind_fails_closed(self):
        state = state_for(os.getpid())
        state["create_kind"] = ""

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.verify_process_identity(state, os.getpid())

        assert caught.value.code == "identity-unavailable"

    def test_unreadable_identity_fails_closed(self, monkeypatch):
        state = state_for(os.getpid())
        monkeypatch.setattr(app_drive, "read_process_identity", lambda pid: None)

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.verify_process_identity(state, os.getpid())

        assert caught.value.code == "identity-unavailable"

    def test_creation_token_mismatch_is_refused(self):
        state = state_for(os.getpid())
        state["create_token"] = "not-the-recorded-token"

        if state["create_kind"] == "":
            pytest.skip("this platform cannot expose process identity")

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.verify_process_identity(state, os.getpid())

        assert caught.value.code == "pid-mismatch"

    def test_wrong_image_path_is_refused(self):
        state = state_for(os.getpid(), exe = "/definitely/not/this/process.exe")

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.verify_process_identity(state, os.getpid())

        assert caught.value.code == "pid-mismatch"

    def test_same_image_path_normalises_case_and_relative_parts(self):
        assert app_drive.same_image_path(sys.executable, os.path.abspath(sys.executable))
        assert not app_drive.same_image_path(sys.executable, "/definitely/other.exe")

class test_port_handoff:
    def test_explicit_port_wins_over_state(self, drive_state):
        record_state(state_for(os.getpid(), port = 9444))
        assert app_drive.resolve_attach_port(attach_args(port = 9333, app = "demo")) == 9333

    def test_app_reads_the_launch_state_port(self, drive_state):
        record_state(state_for(os.getpid(), port = 9444))
        assert app_drive.resolve_attach_port(attach_args(app = "demo")) == 9444

    def test_attach_without_state_never_allocates(self, drive_state, monkeypatch):
        def refuse(app, kind):
            raise AssertionError("attach mode must never call ports.acquire")

        monkeypatch.setattr(ports, "acquire", refuse)

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args(app = "ghost"))

        assert caught.value.code == "no-launch-state"
        assert not (drive_state.parent / "ports.json").exists()

    def test_attach_with_dead_pid_reports_not_running(self, drive_state):
        state = state_for(os.getpid(), port = 9444)
        state["pid"] = 4_000_000
        record_state(state)

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args(app = "demo"))

        assert caught.value.code == "app-not-running"

    def test_attach_port_by_env_never_touches_the_registry(self, drive_state, monkeypatch):
        def refuse(app, kind):
            raise AssertionError("attach mode must never call ports.acquire")

        monkeypatch.setattr(ports, "acquire", refuse)
        monkeypatch.setenv(app_drive.CDP_PORT_ENV, "9666")
        assert app_drive.resolve_attach_port(attach_args()) == 9666
        assert not (drive_state.parent / "ports.json").exists()

    def test_attach_port_out_of_range_is_rejected(self, drive_state):
        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args(port = 70000))

        assert caught.value.code == "bad-port"

    def test_no_port_is_an_error(self, drive_state, monkeypatch):
        monkeypatch.delenv(app_drive.CDP_PORT_ENV, raising = False)

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args())

        assert caught.value.code == "no-port"

class test_ascii_output:
    def test_emit_is_ascii_under_a_gbk_stdout(self, monkeypatch):
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(buffer, encoding = "gbk")
        monkeypatch.setattr(sys, "stdout", wrapper)
        app_drive.emit({"ok": True, "value": "中文标题"}, pretty = True)
        wrapper.flush()
        raw = buffer.getvalue()
        assert raw.isascii()
        assert json.loads(raw.decode("ascii"))["value"] == "中文标题"

    def test_status_envelope_stays_ascii_with_cjk_page_text(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.page_state["title"] = "什亭之匣"
            server.page_state["body_text_head"] = "登录"
            code, payload, captured = run_cli(capsys, "status", "--port", str(server.port))

        assert code == 0
        assert payload["state"]["title"] == "什亭之匣"
        assert captured.out.isascii()

class test_dropped_vite_scope:
    def test_launch_does_not_export_a_vite_port(self, drive_state, capsys, monkeypatch):
        captured: dict = {}

        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            captured["environment"] = environment
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        monkeypatch.setattr(app_drive, "_spawn", fake_spawn)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")

        try:
            assert code == 0
            assert "CELESTIA_VITE_PORT" not in captured["environment"]
            assert "vite_port" not in payload
            assert "vite_port" not in app_drive.read_state("demo")
        finally:
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

class test_protocol_commands:
    def test_targets_lists_the_endpoint(self, drive_state, capsys):
        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "targets", "--port", str(server.port))

        assert code == 0
        assert payload["ok"] is True
        assert payload["protocol"] == app_drive.PROTOCOL_VERSION
        assert payload["cmd"] == "targets"
        assert payload["count"] == 1
        assert payload["targets"][0]["id"] == "page-1"
        assert payload["targets"][0]["websocket_url"].startswith("ws://127.0.0.1:")

    def test_targets_can_select_a_target_by_id(self, drive_state, capsys):
        targets = [page_target("devtools-1", "devtools://x", "page", "DevTools"), page_target("page-7", "http://localhost:5173/")]

        with fake_cdp_server(targets) as server:
            code, payload, _captured = run_cli(capsys, "targets", "--port", str(server.port), "--target", "devtools-1")

        assert code == 0
        assert payload["selected_target_id"] == "devtools-1"

    def test_status_reports_page_and_boot_chrome(self, drive_state, capsys):
        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port))

        assert code == 0
        assert payload["target"]["id"] == "page-1"
        assert payload["state"]["title"] == "Fake Chest"
        assert payload["state"]["boot_chrome"] == "claimed"
        assert payload["state"]["tauri"] is True
        assert payload["launched"] is None

    def test_status_skips_devtools_targets(self, drive_state, capsys):
        targets = [page_target("devtools-1", "devtools://devtools/bundled/inspector.html", "page", "DevTools"), page_target("page-7", "http://localhost:5173/@app")]

        with fake_cdp_server(targets) as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port))

        assert code == 0
        assert payload["target"]["id"] == "page-7"

    def test_status_honours_prefer_url(self, drive_state, capsys):
        targets = [page_target("page-a", "http://localhost:5173/@first"), page_target("page-b", "http://localhost:5173/@second")]

        with fake_cdp_server(targets) as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port), "--prefer-url", "http://localhost:5173/@second")

        assert code == 0
        assert payload["target"]["id"] == "page-b"

    def test_eval_returns_the_json_value(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_evaluate({"answer": 42})
            code, payload, _captured = run_cli(capsys, "eval", "--port", str(server.port), "--no-await", "1 + 1")

        assert code == 0
        assert payload["value"] == {"answer": 42}
        assert payload["type"] == "object"
        assert server.messages[-1]["params"]["awaitPromise"] is False

    def test_eval_raw_prints_only_the_ascii_value(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_evaluate("hello")
            code, payload, captured = run_cli(capsys, "eval", "--port", str(server.port), "--raw", "x")

        assert code == 0
        assert payload == "hello"
        assert captured.out.strip() == json.dumps("hello")

    def test_eval_writes_the_value_to_a_file(self, drive_state, capsys, tmp_path):
        output = tmp_path / "value.json"

        with fake_cdp_server() as server:
            server.queue_evaluate([1, 2, 3])
            code, payload, _captured = run_cli(capsys, "eval", "--port", str(server.port), "--out", str(output), "x")

        assert code == 0
        assert json.loads(output.read_text(encoding = "utf-8")) == [1, 2, 3]
        assert payload["out"] == str(output.resolve())

    def test_js_exception_is_a_protocol_failure(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_exception("boom")
            code, payload, _captured = run_cli(capsys, "eval", "--port", str(server.port), "throw")

        assert code == 1
        assert payload["ok"] is False
        assert payload["error"]["code"] == "js-exception"

    def test_shot_writes_a_png_and_reports_its_size(self, drive_state, capsys, tmp_path):
        output = tmp_path / "shot.png"

        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "shot", "--port", str(server.port), str(output))

        assert code == 0
        assert output.exists()
        assert payload["bytes"] > 0
        assert (payload["width"], payload["height"]) == (8, 4)

    def test_click_dom_matches_and_clicks(self, drive_state, capsys):
        probe = {"matched": True, "tag": "button", "id": "settings", "text": "Settings", "rect": {"x": 10, "y": 20, "width": 100, "height": 30}, "disabled": False}

        with fake_cdp_server() as server:
            server.queue_evaluate(probe)
            server.queue_evaluate({"matched": True, "clicked": True})
            code, payload, _captured = run_cli(capsys, "click", "--port", str(server.port), "#settings")

        assert code == 0
        assert payload["mode"] == "dom"
        assert payload["element"]["id"] == "settings"
        expressions = [message["params"]["expression"] for message in server.messages if message["method"] == "Runtime.evaluate"]
        assert "getBoundingClientRect" in expressions[0]
        assert ".click()" in expressions[1]

    def test_click_mouse_dispatches_real_events(self, drive_state, capsys):
        probe = {"matched": True, "tag": "button", "id": "ok", "text": "OK", "rect": {"x": 10, "y": 20, "width": 100, "height": 30}, "disabled": False}

        with fake_cdp_server() as server:
            server.queue_evaluate(probe)
            code, payload, _captured = run_cli(capsys, "click", "--port", str(server.port), "--mouse", "#ok")

        assert code == 0
        assert payload["mode"] == "mouse"
        mouse_messages = [message for message in server.messages if message["method"] == "Input.dispatchMouseEvent"]
        assert len(mouse_messages) == 2
        assert mouse_messages[0]["params"]["x"] == 60
        assert mouse_messages[0]["params"]["y"] == 35
        assert mouse_messages[0]["params"]["type"] == "mousePressed"
        assert mouse_messages[1]["params"]["type"] == "mouseReleased"

    def test_click_missing_selector_fails(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_evaluate({"matched": False})
            code, payload, _captured = run_cli(capsys, "click", "--port", str(server.port), "#nope")

        assert code == 1
        assert payload["error"]["code"] == "selector-not-found"

    def test_wait_matches_after_polling(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_evaluate({"matched": False}, {"matched": False}, {"matched": True, "tag": "div"})
            code, payload, _captured = run_cli(capsys, "wait", "--port", str(server.port), "--selector", "div", "--interval", "0.01", "--timeout", "2")

        assert code == 0
        assert payload["matched"] is True
        assert payload["samples"] >= 3
        assert payload["condition"]["kind"] == "selector"

    def test_wait_timeout_is_a_protocol_failure(self, drive_state, capsys):
        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "wait", "--port", str(server.port), "--selector", "div", "--interval", "0.01", "--timeout", "0.15")

        assert code == 1
        assert payload["error"]["code"] == "timeout"

    def test_unreachable_endpoint_is_reported(self, drive_state, capsys):
        code, payload, _captured = run_cli(capsys, "status", "--port", "1", "--timeout", "0.3")
        assert code == 1
        assert payload["error"]["code"] in ("cdp-unreachable", "cdp-timeout")

    def test_target_not_found_is_reported(self, drive_state, capsys):
        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port), "--target", "missing")

        assert code == 1
        assert payload["error"]["code"] == "target-not-found"

class test_watch:
    def test_records_the_boot_chrome_lifecycle(self, drive_state, capsys, tmp_path):
        active = {"ready_state": "loading", "title": "Chest", "url": "http://localhost:5173/", "boot_chrome": "active", "has_bar": True, "bar_driver": "window", "degraded": None}
        claimed = {"ready_state": "complete", "title": "Chest", "url": "http://localhost:5173/", "boot_chrome": "claimed", "has_bar": False, "bar_driver": None, "degraded": None}
        output_dir = tmp_path / "watch"

        with fake_cdp_server() as server:
            server.queue_evaluate(active, claimed)
            code, payload, _captured = run_cli(capsys, "watch", "--port", str(server.port), "--out", str(output_dir), "--duration", "5", "--interval", "0.01", "--reload", "--stop-boot-chrome", "claimed")

        assert code == 0
        assert payload["reloaded"] is True
        assert payload["stopped_reason"] == "boot-chrome:claimed"
        assert payload["final_state"]["boot_chrome"] == "claimed"
        assert (output_dir / "timeline.jsonl").is_file()
        assert (output_dir / "watch.json").is_file()
        fields = {transition["field"] for transition in payload["transitions"]}
        assert "boot_chrome" in fields
        assert payload["screenshots"]

class test_launch:
    def _fake_spawn(self, captured: dict):
        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            captured["command"] = command
            captured["environment"] = environment
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        return fake_spawn

    def test_launch_writes_state_and_applies_the_env(self, drive_state, capsys, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(app_drive, "_spawn", self._fake_spawn(captured))
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")
        state = app_drive.read_state("demo")

        try:
            assert code == 0
            assert payload["pid"] == os.getpid()
            assert payload["cdp_port"] == 9444
            assert "vite_port" not in payload
            assert captured["command"] == [str(Path(sys.executable).resolve())]
            assert captured["environment"][app_drive.CDP_ENV_VAR] == "--remote-debugging-port=9444"
            assert "CELESTIA_VITE_PORT" not in captured["environment"]
            assert state is not None
            assert state["schema"] == app_drive.STATE_SCHEMA
            assert state["pid"] == os.getpid()
            assert state["cdp_port"] == 9444
            assert state["create_token"]
            assert state["create_kind"] in ("windows-filetime", "proc-starttime")
            registry = ports.list_entries()
            assert registry["demo:cdp"]["pid"] == os.getpid()
        finally:
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_launch_without_app_uses_the_exe_path_hash(self, drive_state, capsys, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(app_drive, "_spawn", self._fake_spawn(captured))
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--no-log", "--no-wait")
        expected = app_drive.default_app_key(str(Path(sys.executable).resolve()))

        try:
            assert code == 0
            assert payload["app"] == expected
            assert app_drive.read_state(expected) is not None
        finally:
            app_drive.remove_state(app_drive.state_file_path(expected))
            ports.release(expected, None)

    def test_forced_port_skips_allocation(self, drive_state, capsys, monkeypatch):
        captured: dict = {}

        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            captured["environment"] = environment
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        def refuse(app, kind):
            raise AssertionError("--port must skip ports.acquire")

        monkeypatch.setattr(app_drive, "_spawn", fake_spawn)
        monkeypatch.setattr(ports, "acquire", refuse)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--port", "9555", "--no-log", "--no-wait")

        try:
            assert code == 0
            assert payload["cdp_port"] == 9555
            assert captured["environment"][app_drive.CDP_ENV_VAR] == "--remote-debugging-port=9555"
        finally:
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_launch_refuses_to_replace_a_live_instance(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid))
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")
            assert code == 1
            assert payload["error"]["code"] == "already-running"
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_replace_refuses_a_different_exe(self, drive_state, capsys, tmp_path):
        child = spawn_sleeper()
        other = tmp_path / "other.exe"
        shutil.copyfile(sys.executable, other)

        try:
            record_state(state_for(child.pid))
            code, payload, _out = run_cli(capsys, "launch", str(other), "--app", "demo", "--replace", "--no-log", "--no-wait")
            assert code == 1
            assert payload["error"]["code"] == "replace-exe-mismatch"
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_replace_verifies_and_stops_the_previous_instance(self, drive_state, capsys, monkeypatch):
        previous = spawn_sleeper()
        spawned: list = []

        def spawn_child(command, environment, cwd, stdout_target, stderr_target):
            child = spawn_sleeper()
            spawned.append(child)
            return child

        monkeypatch.setattr(app_drive, "_spawn", spawn_child)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)

        try:
            record_state(state_for(previous.pid))
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--replace", "--no-log", "--no-wait")
            assert code == 0
            assert previous.poll() is not None
            assert payload["pid"] == spawned[0].pid
            assert app_drive.read_state("demo")["pid"] == spawned[0].pid
        finally:
            stop_child(previous)

            for child in spawned:
                stop_child(child)

            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_failed_wait_kills_its_child_and_removes_state(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        def refuse_wait(*args, **kwargs):
            raise cdp.cdp_error("cdp-timeout", "no page target")

        monkeypatch.setattr(app_drive, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", refuse_wait)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "fail-demo", "--no-log", "--wait-timeout", "1")
        assert code == 1
        assert payload["error"]["code"] == "cdp-timeout"
        assert payload["cleanup"]["terminated"] is True
        assert app_drive.read_state("fail-demo") is None
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None

    def test_registration_failure_reclaims_the_child(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        def broken_write(state, path):
            raise OSError("disk full")

        monkeypatch.setattr(app_drive, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive, "write_state", broken_write)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "reg-demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "launch-failed"
        assert app_drive.read_state("reg-demo") is None
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None
        assert ports.list_entries() == {}

    def test_cleanup_failure_keeps_state_and_claims(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        def refuse_wait(*args, **kwargs):
            raise cdp.cdp_error("cdp-timeout", "no page target")

        def refuse_kill(pid, grace = 0):
            raise cdp.cdp_error("close-failed", "cannot kill " + str(pid))

        monkeypatch.setattr(app_drive, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", refuse_wait)
        monkeypatch.setattr(app_drive, "terminate_tree", refuse_kill)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "keep-demo", "--no-log")

        try:
            assert code == 1
            assert payload["cleanup"]["terminated"] is False
            assert payload["cleanup"]["state_kept"] is True
            assert app_drive.read_state("keep-demo") is not None
            assert ports.list_entries()["keep-demo:cdp"]["pid"] == spawned["child"].pid
            assert "state kept" in payload["error"]["message"]
        finally:
            stop_child(spawned["child"])
            app_drive.remove_state(app_drive.state_file_path("keep-demo"))
            ports.release("keep-demo", None)

    def test_keyboard_interrupt_while_waiting_cleans_up(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr(app_drive, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", interrupt)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "int-demo", "--no-log")
        assert code == 130
        assert payload["error"]["code"] == "interrupted"
        assert app_drive.read_state("int-demo") is None
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None
        assert ports.list_entries() == {}

    def test_identity_unavailable_after_spawn_reclaims_the_child(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        monkeypatch.setattr(app_drive, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive, "read_process_identity", lambda pid: None)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "ident-demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "identity-unavailable"
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None
        assert app_drive.read_state("ident-demo") is None

class test_close:
    def test_closes_a_matching_identity(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid))
            ports.claim("demo", "cdp", 9444, child.pid)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--grace", "5")
            assert code == 0
            assert payload["closed"] is True
            assert payload["verified"] is True
            assert payload["state_file_removed"] is True
            assert app_drive.read_state("demo") is None
            assert ports.list_entries() == {}
            assert child.poll() is not None
        finally:
            stop_child(child)

    def test_close_derives_the_key_from_exe(self, drive_state, capsys):
        child = spawn_sleeper()
        key = app_drive.default_app_key(str(Path(sys.executable).resolve()))

        try:
            record_state(state_for(child.pid, app = key, exe = str(Path(sys.executable).resolve())))
            code, payload, _out = run_cli(capsys, "close", "--exe", sys.executable)
            assert code == 0
            assert payload["closed"] is True
            assert child.poll() is not None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path(key))

    def test_close_refuses_a_wrong_image_path(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid, exe = "/definitely/not/python.exe"))
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "pid-mismatch"
            assert app_drive.read_state("demo") is not None
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_close_refuses_a_reused_creation_token(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid, token = "stale-creation-token"))
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "pid-mismatch"
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_close_fails_closed_without_a_recorded_token(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid, token = "", kind = ""))
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "identity-unavailable"
            assert app_drive.read_state("demo") is not None
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_close_fails_closed_when_identity_is_unreadable(self, drive_state, capsys, monkeypatch):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid))
            monkeypatch.setattr(app_drive, "read_process_identity", lambda pid: None)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "identity-unavailable"
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_missing_state_is_an_error(self, drive_state, capsys):
        code, payload, _out = run_cli(capsys, "close", "--app", "ghost")
        assert code == 1
        assert payload["error"]["code"] == "no-state"

    def test_outdated_state_is_refused(self, drive_state, capsys):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": os.getpid(), "port": 9333}), encoding = "utf-8")
        code, payload, _out = run_cli(capsys, "close", "--app", "demo")
        assert code == 1
        assert payload["error"]["code"] == "state-outdated"

    def test_close_accepts_a_state_file_without_app(self, drive_state, capsys, tmp_path):
        child = spawn_sleeper()
        state_path = tmp_path / "custom-state.json"

        try:
            app_drive.write_state(state_for(child.pid, app = "demo"), state_path)
            code, payload, _out = run_cli(capsys, "close", "--state-file", str(state_path))
            assert code == 0
            assert payload["closed"] is True
            assert payload["verified"] is True
            assert not state_path.exists()
            assert child.poll() is not None
        finally:
            stop_child(child)

    def test_pid_requires_force(self, drive_state, capsys):
        code, payload, _out = run_cli(capsys, "close", "--pid", "1234")
        assert code == 1
        assert payload["error"]["code"] == "unsafe-close"

    def test_pid_force_is_the_explicit_break_glass(self, drive_state, capsys):
        child = spawn_sleeper()
        code, payload, _out = run_cli(capsys, "close", "--pid", str(child.pid), "--force")

        try:
            assert code == 0
            assert payload["verified"] is False
            assert child.poll() is not None
        finally:
            stop_child(child)

    def test_kill_failure_keeps_state_and_claims(self, drive_state, capsys, monkeypatch):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid))
            ports.claim("demo", "cdp", 9444, child.pid)

            def refuse_kill(pid, grace = 0):
                raise cdp.cdp_error("close-failed", "cannot kill " + str(pid))

            monkeypatch.setattr(app_drive, "terminate_tree", refuse_kill)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "close-failed"
            assert "state and port claim were kept" in payload["error"]["message"]
            assert app_drive.read_state("demo") is not None
            assert ports.list_entries()["demo:cdp"]["pid"] == child.pid
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_not_running_state_is_cleaned_up(self, drive_state, capsys):
        child = spawn_sleeper()
        stop_child(child)
        record_state(state_for(child.pid))
        code, payload, _out = run_cli(capsys, "close", "--app", "demo")
        assert code == 0
        assert payload["reason"] == "not-running"
        assert app_drive.read_state("demo") is None

class test_ports_registry:
    def test_acquire_is_atomic_and_leaves_no_temp_files(self, drive_state):
        port = ports.acquire("demo", "cdp")
        registry = drive_state.parent / "ports.json"
        assert registry.is_file()
        assert json.loads(registry.read_text(encoding = "utf-8"))
        assert [item.name for item in registry.parent.iterdir() if ".tmp" in item.name] == []
        assert port >= 9222

    def test_claim_is_visible_and_garbage_collected(self, drive_state):
        ports.claim("demo", "cdp", 9444, os.getpid())
        assert ports.list_entries()["demo:cdp"]["port"] == 9444
        ports.claim("ghost", "cdp", 9445, 4_000_000)
        assert "ghost:cdp" not in ports.list_entries()
