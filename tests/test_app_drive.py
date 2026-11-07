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
import time
import types
from pathlib import Path
import pytest
from celestia_devtools.env import app_drive  # noqa: E402
from celestia_devtools.env import app_drive_cdp, app_drive_cli, app_drive_proc, app_drive_state  # noqa: E402
from celestia_devtools.env import cdp
from celestia_devtools.env import ports
from fake_cdp import fake_cdp_server, page_target

def _patch_everywhere(monkeypatch, name, value):
    """Set *name* on every split-module namespace that holds it.

    app_drive was one module until the 2026-10-31 split; tests used to
    monkeypatch its single namespace. With from-imports each consumer
    module carries its own binding, so a seam patch must land on all of
    them or a consumer would keep calling the real function."""

    for mod in (app_drive, app_drive_cli, app_drive_proc, app_drive_cdp, app_drive_state):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)

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

    def test_launch_splits_app_arguments_after_separator(self):
        head, extra = app_drive.split_launch_arguments(["launch", "app.exe", "--app", "demo", "--", "--window", "second", "--flag"])
        namespace = app_drive.build_parser().parse_args(head)
        assert extra == ["--window", "second", "--flag"]
        assert namespace.app == "demo"
        assert namespace.exe == "app.exe"
        assert app_drive.split_launch_arguments(["status", "--port", "9333"]) == (["status", "--port", "9333"], [])

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
        assert app_drive.build_launch_command("app.exe", ["--window", "two"]) == ["app.exe", "--window", "two"]
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
        resolved = os.path.realpath(sys.executable)
        first = app_drive.default_app_key(resolved)
        second = app_drive.default_app_key(str(Path(resolved).resolve()))
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
        _patch_everywhere(monkeypatch, "read_process_identity", lambda pid: None)

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

        _patch_everywhere(monkeypatch, "_spawn", fake_spawn)
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
        _patch_everywhere(monkeypatch, "_spawn", self._fake_spawn(captured))
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
        _patch_everywhere(monkeypatch, "_spawn", self._fake_spawn(captured))
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

        _patch_everywhere(monkeypatch, "_spawn", fake_spawn)
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

        _patch_everywhere(monkeypatch, "_spawn", spawn_child)
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

        _patch_everywhere(monkeypatch, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", refuse_wait)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "fail-demo", "--no-log", "--wait-timeout", "1", "--", "-c", "import time; time.sleep(60)")
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

        _patch_everywhere(monkeypatch, "_spawn", recording_spawn)
        _patch_everywhere(monkeypatch, "write_state", broken_write)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "reg-demo", "--no-log", "--no-wait", "--", "-c", "import time; time.sleep(60)")
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

        def refuse_kill(pid, grace = 0, **kwargs):
            raise cdp.cdp_error("close-failed", "cannot kill " + str(pid))

        _patch_everywhere(monkeypatch, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", refuse_wait)
        _patch_everywhere(monkeypatch, "terminate_tree", refuse_kill)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "keep-demo", "--no-log", "--", "-c", "import time; time.sleep(60)")

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

        _patch_everywhere(monkeypatch, "_spawn", recording_spawn)
        monkeypatch.setattr(app_drive.cdp, "wait_for_page", interrupt)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "int-demo", "--no-log", "--", "-c", "import time; time.sleep(60)")
        assert code == 130
        assert payload["error"]["code"] == "interrupted"
        assert app_drive.read_state("int-demo") is None
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None
        assert ports.list_entries() == {}

    def test_identity_unavailable_after_spawn_refuses_blind_kill(self, drive_state, capsys, monkeypatch):
        spawned: dict = {}
        original_spawn = app_drive._spawn
        real_identity = app_drive.read_process_identity

        def recording_spawn(command, environment, cwd, stdout_target, stderr_target):
            child = original_spawn(command, environment, cwd, stdout_target, stderr_target)
            spawned["child"] = child
            return child

        _patch_everywhere(monkeypatch, "_spawn", recording_spawn)
        _patch_everywhere(monkeypatch, "read_process_identity", lambda pid: None)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)

        try:
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "ident-demo", "--no-log", "--no-wait", "--", "-c", "import time; time.sleep(60)")
            assert code == 1
            assert payload["error"]["code"] == "identity-unavailable"
            assert payload["cleanup"]["blind_kill_refused"] is True
            assert payload["cleanup"]["terminated"] is False
            assert payload["cleanup"]["state_kept"] is True
            state = app_drive.read_state("ident-demo")
            assert state is not None
            assert state["unverified"] is True
            assert spawned["child"].poll() is None  # a bare pid must NOT be blind-killed
            # The unverified state is closable only through explicit force.
            _patch_everywhere(monkeypatch, "read_process_identity", real_identity)
            code, payload, _out = run_cli(capsys, "close", "--app", "ident-demo", "--force")
            assert code == 0
            assert payload["verified"] is False
            assert payload["explicit_force"] is True
            assert spawned["child"].poll() is not None
            assert app_drive.read_state("ident-demo") is None
        finally:
            stop_child(spawned["child"])
            app_drive.remove_state(app_drive.state_file_path("ident-demo"))
            ports.release("ident-demo", "cdp")

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
            _patch_everywhere(monkeypatch, "read_process_identity", lambda pid: None)
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

            def refuse_kill(pid, grace = 0, **kwargs):
                raise cdp.cdp_error("close-failed", "cannot kill " + str(pid))

            _patch_everywhere(monkeypatch, "terminate_tree", refuse_kill)
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

    def test_not_running_state_fails_closed_unless_forced(self, drive_state, capsys):
        child = spawn_sleeper()
        stop_child(child)
        record_state(state_for(child.pid))
        # pid_alive False + unreadable identity is ambiguous (EPERM on a live
        # process looks identical): fail closed and KEEP the state.
        code, payload, _out = run_cli(capsys, "close", "--app", "demo")
        assert code == 1
        assert payload["error"]["code"] == "process-unconfirmed"
        assert app_drive.read_state("demo") is not None
        # Explicit break-glass drops the handle without touching anything.
        code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--force")
        assert code == 0
        assert payload["reason"] == "state-discarded-unconfirmed"
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

class test_launch_cleanup_safety:
    def test_abort_skips_the_kill_when_the_child_already_exited(self, drive_state, monkeypatch):
        state_path = app_drive.state_file_path("demo")
        killed = []

        def bomb(*args, **kwargs):
            killed.append(args)
            raise AssertionError("an already-reaped child must not be killed")

        child = types.SimpleNamespace(pid = os.getpid(), poll = lambda: 7)
        _patch_everywhere(monkeypatch, "terminate_tree", bomb)
        result = app_drive._abort_launch(child, state_path, "demo", None, 1, 9444)
        assert result["already_exited"] is True
        assert result["exit_code"] == 7
        assert result["terminated"] is False
        assert killed == []

    def test_abort_refuses_to_blind_kill_without_identity(self, drive_state, monkeypatch):
        state_path = app_drive.state_file_path("demo")
        killed = []

        def bomb(*args, **kwargs):
            killed.append(args)
            raise AssertionError("a bare pid must never be blind-killed")

        _patch_everywhere(monkeypatch, "terminate_tree", bomb)
        _patch_everywhere(monkeypatch, "read_process_identity", lambda pid: None)
        child = types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)
        result = app_drive._abort_launch(child, state_path, "demo", None, 1, 9444)
        kept = app_drive.read_state("demo")

        try:
            assert result["blind_kill_refused"] is True
            assert result["terminated"] is False
            assert result["state_kept"] is True
            assert killed == []
            assert kept is not None
            assert kept["unverified"] is True
            assert kept["cdp_port"] == 9444
        finally:
            app_drive.remove_state(state_path)
            ports.release("demo", "cdp")

    def test_abort_uses_verified_termination_when_identity_exists(self, drive_state, monkeypatch):
        child = spawn_sleeper()
        state_path = app_drive.state_file_path("demo")
        record_state(state_for(child.pid))
        state = app_drive.read_state("demo")
        calls = []

        def fake_terminate_verified(record, pid, grace, child = None):
            calls.append((record, pid, grace))
            return {"closed": True, "forced": False, "waited_ms": 1, "reason": "terminated"}

        _patch_everywhere(monkeypatch, "terminate_verified", fake_terminate_verified)

        try:
            result = app_drive._abort_launch(types.SimpleNamespace(pid = child.pid, poll = lambda: None), state_path, "demo", state, 1, 9444)
            assert result["terminated"] is True
            assert result["state_verified"] is True
            assert calls and (calls[0][1] == child.pid)
            assert app_drive.read_state("demo") is None
        finally:
            stop_child(child)
            app_drive.remove_state(state_path)
            ports.release("demo", "cdp")

    def test_launch_port_range_is_validated_before_acquiring(self, drive_state, capsys, monkeypatch):
        def bomb(app, kind):
            raise AssertionError("an invalid --port must be rejected before ports.acquire")

        monkeypatch.setattr(ports, "acquire", bomb)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--port", "70000")
        assert code == 1
        assert payload["error"]["code"] == "bad-port"
        assert not (drive_state.parent / "ports.json").exists()

    def test_reserved_env_keys_are_rejected_before_acquiring(self, drive_state, capsys, monkeypatch):
        def bomb(app, kind):
            raise AssertionError("a rejected --env must not allocate a port")

        monkeypatch.setattr(ports, "acquire", bomb)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--env", "CELESTIA_CDP_PORT=1")
        assert code == 1
        assert payload["error"]["code"] == "bad-env"
        assert not (drive_state.parent / "ports.json").exists()

    def test_bad_log_dir_is_rejected_before_acquiring(self, drive_state, capsys, monkeypatch, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding = "utf-8")

        def bomb(app, kind):
            raise AssertionError("a bad --log-dir must not allocate a port")

        monkeypatch.setattr(ports, "acquire", bomb)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--log-dir", str(blocker))
        assert code == 1
        assert payload["error"]["code"] == "log-dir-failed"
        assert not (drive_state.parent / "ports.json").exists()

    def test_immediate_exit_is_detected_before_identity_read(self, drive_state, capsys, monkeypatch):
        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: 3)

        def identity_bomb(pid):
            raise AssertionError("identity must not be read after the child already exited")

        _patch_everywhere(monkeypatch, "_spawn", fake_spawn)
        _patch_everywhere(monkeypatch, "read_process_identity", identity_bomb)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "launch-failed"
        assert "exited before" in payload["error"]["message"]
        assert app_drive.read_state("demo") is None

class test_identity_matrix:
    def test_token_kind_mismatch_is_refused_and_claim_kept(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            state = state_for(child.pid)
            state["create_kind"] = "proc-starttime" if state["create_kind"] == "windows-filetime" else "windows-filetime"
            record_state(state)
            ports.claim("demo", "cdp", 9444, child.pid)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "pid-mismatch"
            assert child.poll() is None
            assert ports.list_entries()["demo:cdp"]["pid"] == child.pid
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", "cdp")

    def test_replace_token_mismatch_is_refused(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid, token = "0"))
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--replace", "--no-log", "--no-wait")
            assert code == 1
            assert payload["error"]["code"] == "pid-mismatch"
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_same_image_path_follows_symlinks(self, tmp_path):
        target = tmp_path / "real.exe"
        target.write_text("x", encoding = "utf-8")
        link = tmp_path / "link.exe"

        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable on this host")

        assert app_drive.same_image_path(str(target), str(link))
        assert not app_drive.same_image_path(str(target), str(tmp_path / "other.exe"))

    def test_state_app_mismatch_is_refused(self, drive_state, capsys):
        state = state_for(os.getpid())
        state["app"] = "someone-else"
        app_drive.write_state(state, app_drive.state_file_path("demo"))
        code, payload, _out = run_cli(capsys, "close", "--app", "demo")
        assert code == 1
        assert payload["error"]["code"] == "state-app-mismatch"

    def test_discard_outdated_state(self, drive_state, capsys):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": 1, "port": 9333}), encoding = "utf-8")
        ports.claim("demo", "cdp", 9444, os.getpid())
        code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--discard-state")
        assert code == 0
        assert payload["discarded_state"] is True
        assert not path.exists()
        assert "demo:cdp" not in ports.list_entries()

    def test_launch_discards_an_outdated_state(self, drive_state, capsys, monkeypatch):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": 1, "port": 9333}), encoding = "utf-8")

        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        _patch_everywhere(monkeypatch, "_spawn", fake_spawn)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)

        try:
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--discard-state", "--no-log", "--no-wait")
            assert code == 0
            assert payload["discarded_outdated_state"] is True
            assert app_drive.read_state("demo")["schema"] == app_drive.STATE_SCHEMA
        finally:
            app_drive.remove_state(path)
            ports.release("demo", "cdp")

    def test_status_with_state_file_reports_launched(self, drive_state, capsys, tmp_path):
        child = spawn_sleeper()
        custom = tmp_path / "custom-state.json"

        try:
            with fake_cdp_server() as server:
                app_drive.write_state(state_for(child.pid, port = server.port), custom)
                code, payload, _out = run_cli(capsys, "status", "--state-file", str(custom), "--port", str(server.port))

            assert code == 0
            assert payload["launched"] is not None
            assert payload["launched"]["app"] == "demo"
            assert payload["launched"]["cdp_port"] == server.port
        finally:
            stop_child(child)

class test_ports_hardening:
    def test_registry_sanitizes_corrupt_entries(self, drive_state):
        registry = drive_state.parent / "ports.json"
        registry.parent.mkdir(parents = True, exist_ok = True)
        payload = {"bad:cdp": {"port": "x", "pid": 1}, "ghost:cdp": {"port": 9444, "pid": 4_000_000}, "ok:cdp": {"port": 9445, "pid": os.getpid()}, "notdict": 3}
        registry.write_text(json.dumps(payload), encoding = "utf-8")
        entries = ports.list_entries()
        assert "bad:cdp" not in entries
        assert "ghost:cdp" not in entries
        assert entries["ok:cdp"]["port"] == 9445
        assert ports.acquire("fresh", "cdp") >= 9222

    def test_release_only_drops_the_exact_app_and_kind(self, drive_state):
        ports.claim("demo", "cdp", 9444, os.getpid())
        ports.claim("demo", "vite", 5444, os.getpid())
        ports.claim("demo2", "cdp", 9446, os.getpid())
        ports.release("demo", "cdp")
        entries = ports.list_entries()
        assert "demo:cdp" not in entries
        assert "demo:vite" in entries
        assert "demo2:cdp" in entries
        ports.release("demo", None)
        assert "demo2:cdp" in ports.list_entries()

    def test_stale_lock_with_a_live_holder_is_not_stolen(self, drive_state, monkeypatch):
        registry = drive_state.parent / "ports.json"
        registry.parent.mkdir(parents = True, exist_ok = True)
        lock = registry.with_name("ports.json.lock")
        lock.write_text(str(os.getpid()), encoding = "utf-8")
        os.utime(lock, (time.time() - 999, time.time() - 999))
        monkeypatch.setattr(ports, "LOCK_TIMEOUT", 0.3)
        monkeypatch.setattr(ports, "LOCK_STALE", 0.0)

        with pytest.raises(SystemExit):
            ports.acquire("demo", "cdp")

        assert lock.exists()

    def test_stale_lock_with_a_dead_holder_is_reclaimed(self, drive_state, monkeypatch):
        registry = drive_state.parent / "ports.json"
        registry.parent.mkdir(parents = True, exist_ok = True)
        lock = registry.with_name("ports.json.lock")
        lock.write_text("4000000", encoding = "utf-8")
        os.utime(lock, (time.time() - 999, time.time() - 999))
        monkeypatch.setattr(ports, "LOCK_TIMEOUT", 2.0)
        monkeypatch.setattr(ports, "LOCK_STALE", 0.0)
        assert ports.acquire("demo", "cdp") >= 9222
        assert not lock.exists()

class test_round4_regressions:
    def test_targets_lists_an_endpoint_without_any_target(self, drive_state, capsys):
        with fake_cdp_server(targets = []) as server:
            code, payload, _out = run_cli(capsys, "targets", "--port", str(server.port))

        assert code == 0
        assert payload["count"] == 0
        assert payload["selected_target_id"] is None

    def test_release_ports_only_drops_the_cdp_claim(self, drive_state):
        ports.claim("demo", "cdp", 9444, os.getpid())
        ports.claim("demo", "vite", 5444, os.getpid())
        assert app_drive.release_ports("demo") is True
        entries = ports.list_entries()
        assert "demo:cdp" not in entries
        assert "demo:vite" in entries
        ports.release("demo", "vite")

    def test_unverified_state_requires_force(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            state = state_for(child.pid)
            state["create_token"] = ""
            state["create_kind"] = ""
            state["unverified"] = True
            record_state(state)
            ports.claim("demo", "cdp", 9444, child.pid)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo")
            assert code == 1
            assert payload["error"]["code"] == "identity-unavailable"
            assert app_drive.read_state("demo") is not None
            assert child.poll() is None
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", "cdp")

    def test_spawn_oserror_releases_the_claim(self, drive_state, capsys, monkeypatch):
        def broken_spawn(command, environment, cwd, stdout_target, stderr_target):
            raise OSError("spawn denied")

        _patch_everywhere(monkeypatch, "_spawn", broken_spawn)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "spawn-demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "launch-failed"
        assert app_drive.read_state("spawn-demo") is None
        assert ports.list_entries() == {}

    def test_cleanup_messages_cover_every_branch(self):
        unverified = {"app": "demo", "pid": 5, "state_file": "/tmp/demo.json", "state_kept": True, "state_verified": False}
        assert "close --app demo --force" in app_drive._cleanup_note(unverified)
        verified = {"app": "demo", "pid": 5, "state_file": "/tmp/demo.json", "state_kept": True, "state_verified": True}
        assert "retry with app-drive close" in app_drive._cleanup_note(verified)
        assert "already exited" in app_drive._cleanup_note({"already_exited": True, "exit_code": 3})
        assert "NOT killed" in app_drive._cleanup_note({"blind_kill_refused": True, "pid": 5})
        assert "terminated" in app_drive._cleanup_note({"terminated": True})

    def test_force_signal_rechecks_identity_first(self, drive_state):
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, start_new_session = os.name != "nt")
        checked = []
        time.sleep(0.4)  # let the child install its SIGTERM handler before we signal it

        def reject() -> dict:
            checked.append(True)
            raise cdp.cdp_error("pid-mismatch", "identity changed during the grace window")

        try:
            with pytest.raises(cdp.cdp_error) as caught:
                app_drive.terminate_tree(child.pid, 0.2, verify = reject)

            assert caught.value.code == "pid-mismatch"
            assert checked
            assert child.poll() is None  # the forced signal was never sent
        finally:
            stop_child(child)

    def test_identity_exit_race_is_reported_before_registering(self, drive_state, capsys, monkeypatch):
        identity = app_drive.read_process_identity(os.getpid())

        if identity is None:
            pytest.skip("this platform cannot expose process identity")

        class racing_child:
            pid = os.getpid()

            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls == 1 else 3

        _patch_everywhere(monkeypatch, "_spawn", lambda command, environment, cwd, stdout_target, stderr_target: racing_child())
        _patch_everywhere(monkeypatch, "read_process_identity", lambda pid: identity)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: 9444)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "race-demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "launch-failed"
        assert "reading its identity" in payload["error"]["message"]
        assert app_drive.read_state("race-demo") is None

    def test_reserved_env_keys_are_case_insensitive(self, drive_state, capsys, monkeypatch):
        def bomb(app, kind):
            raise AssertionError("a rejected --env must not allocate a port")

        monkeypatch.setattr(ports, "acquire", bomb)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--env", "celestia_cdp_port=1")
        assert code == 1
        assert payload["error"]["code"] == "bad-env"

    def test_discard_current_state_requires_force(self, drive_state, capsys):
        child = spawn_sleeper()

        try:
            record_state(state_for(child.pid))
            code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--discard-state")
            assert code == 1
            assert payload["error"]["code"] == "discard-needs-force"
            assert app_drive.read_state("demo") is not None
            assert child.poll() is None
            code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--discard-state", "--force")
            assert code == 0
            assert payload["reason"] == "state-discarded"
            assert app_drive.read_state("demo") is None
            assert child.poll() is None  # discarding never signals the process
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))

    def test_outdated_discard_survives_a_vanishing_file(self, drive_state, capsys, monkeypatch):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": 1, "port": 9333}), encoding = "utf-8")
        real_read = app_drive.read_state

        def vanishing(app = None, explicit = None, allow_outdated = False):
            if allow_outdated:
                return None

            return real_read(app, explicit, allow_outdated)

        _patch_everywhere(monkeypatch, "read_state", vanishing)
        code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--discard-state")
        assert code == 0
        assert payload["reason"] == "state-vanished"

    def test_no_timestamped_log_dir_when_acquire_fails(self, drive_state, capsys, monkeypatch):
        def no_port(app, kind):
            raise SystemExit("error: no free cdp port")

        monkeypatch.setattr(ports, "acquire", no_port)
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo")
        assert code == 1
        assert payload["error"]["code"] == "port-unavailable"
        assert not (drive_state / "logs").exists()

    def test_identity_probe_reason_distinguishes_gone_processes(self):
        assert app_drive.identity_probe_reason(4_000_000) in ("no-such-process", "access-denied")

    def test_pid_alive_treats_zombies_as_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
        deadline = time.monotonic() + 10

        while (child.poll() is None) and (time.monotonic() < deadline):
            time.sleep(0.02)

        # poll() above reaped the child; the zombie case itself is covered by
        # ports.pid_alive reading /proc and by the POSIX termination tests.
        assert child.returncode == 0
        assert ports.pid_alive(child.pid) is False

class test_round5_termination:
    def test_force_close_kills_a_non_group_leader_pid(self, drive_state, capsys):
        # Deliberately NO start_new_session: killpg(pid) cannot reach this pid
        # because it does not lead a process group. The POSIX fallback to
        # os.kill(pid) is what --pid --force relies on.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)

        try:
            code, payload, _out = run_cli(capsys, "close", "--pid", str(child.pid), "--force", "--grace", "2")
            assert code == 0
            assert payload["closed"] is True
            assert app_drive.wait_pid_exit(child.pid, 10) is not None
        finally:
            stop_child(child)

    def test_force_close_app_state_kills_a_non_group_leader_pid(self, drive_state, capsys):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)

        try:
            state = state_for(child.pid)
            state["create_token"] = ""
            state["create_kind"] = ""
            state["unverified"] = True
            record_state(state)
            ports.claim("demo", "cdp", 9444, child.pid)
            code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--force", "--grace", "2")
            assert code == 0
            assert payload["verified"] is False
            assert payload["closed"] is True
            assert app_drive.wait_pid_exit(child.pid, 10) is not None
            assert app_drive.read_state("demo") is None
            assert ports.list_entries() == {}
        finally:
            stop_child(child)
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", "cdp")

    def test_owned_child_force_kill_escalates_and_reaps(self, drive_state):
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, start_new_session = os.name != "nt")
        time.sleep(0.4)  # let the child install its SIGTERM handler

        try:
            result = app_drive.terminate_tree(child.pid, 0.2, child = child)
            assert result["closed"] is True
            assert result["forced"] is True
            assert child.poll() is not None
        finally:
            stop_child(child)

    def test_wait_child_exit_times_out_for_a_live_child(self, drive_state):
        child = spawn_sleeper()

        try:
            assert app_drive.wait_child_exit(child, 0.2) is None
            assert child.poll() is None
        finally:
            stop_child(child)

    def test_state_outdated_messages_name_the_recovery(self, drive_state):
        path = app_drive.state_file_path("demo")
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps({"app": "demo", "pid": 1, "port": 9333}), encoding = "utf-8")

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.read_state("demo")

        assert "close --app demo --discard-state" in str(caught.value)
        assert "launch --discard-state" in str(caught.value)

        with pytest.raises(cdp.cdp_error) as explicit:
            app_drive.read_state(None, str(path))

        assert ("close --state-file " + str(path) + " --discard-state") in str(explicit.value)

    def test_identity_probe_reason_reports_access_denied(self, monkeypatch):
        if os.name == "nt":
            pytest.skip("the POSIX access-denied probe path is Linux-specific")

        def denied(path):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(app_drive.os, "readlink", denied)
        assert app_drive.identity_probe_reason(os.getpid()) == "access-denied"

    def test_identity_probe_reason_reports_no_such_process(self):
        assert app_drive.identity_probe_reason(4_000_000) in ("no-such-process", "access-denied")

def test_direct_module_invocation_still_runs_the_cli():
    """R2 S2-1: `python -m celestia_devtools.env.app_drive` must run main.

    The split moved the implementation (and its __main__ guard) into
    app_drive_cli; without a facade guard the module invocation became a
    silent exit-0 no-op while keeping the shebang - fake success."""
    proc = subprocess.run([sys.executable, "-m", "celestia_devtools.env.app_drive", "--help"], capture_output = True, text = True, env = {**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "") or "src"})
    assert proc.returncode == 0
    assert proc.stdout.strip(), "module invocation produced no output - the __main__ guard is missing from the facade"
