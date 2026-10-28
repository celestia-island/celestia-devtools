# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Tests for the app-drive CLI (env/app_drive.py) - no real app required.

Covered without a desktop app: argument parsing, launch environment
construction (the WebView2 CDP line and the vite handoff), port handoff
precedence, the launch state file, process cleanup on a failed launch, close
only killing the launched pid, and the one-JSON-envelope protocol of every
subcommand against a fake CDP endpoint.
"""
from __future__ import annotations
import json
import os
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
    values = {"port": None, "app": None, "state_file": None}
    values.update(overrides)
    return types.SimpleNamespace(**values)

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

class test_launch_environment:
    def test_webview2_line_replaces_only_the_debugging_port(self):
        assert app_drive.merge_webview2_arguments("--disable-gpu --remote-debugging-port=1 --foo", 9333) == "--disable-gpu --foo --remote-debugging-port=9333"

    def test_webview2_line_is_created_when_absent(self):
        assert app_drive.merge_webview2_arguments("", 9222) == "--remote-debugging-port=9222"

    def test_launch_env_sets_the_full_contract(self):
        environment = app_drive.build_launch_env({"PATH": "x", app_drive.CDP_ENV_VAR: "--flag"}, 9333, 5333, ["EXTRA=1"])
        assert environment["PATH"] == "x"
        assert environment[app_drive.CDP_ENV_VAR] == "--flag --remote-debugging-port=9333"
        assert environment[app_drive.CDP_PORT_ENV] == "9333"
        assert environment["CELESTIA_VITE_PORT"] == "5333"
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
        app_drive.write_state({"app": "shittim-chest", "pid": 1, "port": 2}, path)
        assert app_drive.read_state("shittim-chest")["port"] == 2
        assert app_drive.remove_state(path) is True
        assert app_drive.read_state("shittim-chest") is None

    def test_missing_state_is_none(self, drive_state):
        assert app_drive.read_state("nobody") is None

    def test_app_slug_sanitizes_path_separators(self):
        assert app_drive.app_slug("shittim/chest") == "shittim_chest"
        assert app_drive.app_slug("") == "app"

    def test_state_root_honours_the_env_override(self, drive_state):
        assert app_drive.state_root() == drive_state

class test_port_handoff:
    def test_explicit_port_wins_over_state(self, drive_state):
        app_drive.write_state({"app": "demo", "pid": os.getpid(), "port": 9444}, app_drive.state_file_path("demo"))
        assert app_drive.resolve_attach_port(attach_args(port = 9333, app = "demo")) == 9333

    def test_app_reads_the_launch_state_port(self, drive_state):
        app_drive.write_state({"app": "demo", "pid": os.getpid(), "port": 9444}, app_drive.state_file_path("demo"))
        assert app_drive.resolve_attach_port(attach_args(app = "demo")) == 9444

    def test_app_with_a_dead_pid_reports_not_running(self, drive_state):
        app_drive.write_state({"app": "demo", "pid": 4_000_000, "port": 9444}, app_drive.state_file_path("demo"))

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args(app = "demo"))

        assert caught.value.code == "app-not-running"

    def test_app_without_state_allocates_from_the_registry(self, drive_state, monkeypatch):
        calls = []
        monkeypatch.setattr(ports, "acquire", lambda app, kind: calls.append((app, kind)) or 9555)
        assert app_drive.resolve_attach_port(attach_args(app = "demo")) == 9555
        assert calls == [("demo", "cdp")]

    def test_environment_fallback(self, drive_state, monkeypatch):
        monkeypatch.setenv(app_drive.CDP_PORT_ENV, "9666")
        assert app_drive.resolve_attach_port(attach_args()) == 9666

    def test_no_port_is_an_error(self, drive_state, monkeypatch):
        monkeypatch.delenv(app_drive.CDP_PORT_ENV, raising = False)

        with pytest.raises(cdp.cdp_error) as caught:
            app_drive.resolve_attach_port(attach_args())

        assert caught.value.code == "no-port"

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

    def test_status_reports_page_and_boot_chrome(self, drive_state, capsys):
        with fake_cdp_server() as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port))

        assert code == 0
        assert payload["target"]["id"] == "page-1"
        assert payload["state"]["title"] == "Fake Chest"
        assert payload["state"]["boot_chrome"] == "claimed"
        assert payload["state"]["tauri"] is True

    def test_status_skips_devtools_targets(self, drive_state, capsys):
        targets = [page_target("devtools-1", "devtools://devtools/bundled/inspector.html", "page", "DevTools"), page_target("page-7", "http://localhost:5173/@app")]

        with fake_cdp_server(targets) as server:
            code, payload, _captured = run_cli(capsys, "status", "--port", str(server.port))

        assert code == 0
        assert payload["target"]["id"] == "page-7"
        assert payload["target_count"] == 2

    def test_eval_returns_the_json_value(self, drive_state, capsys):
        with fake_cdp_server() as server:
            server.queue_evaluate({"answer": 42})
            code, payload, _captured = run_cli(capsys, "eval", "--port", str(server.port), "--no-await", "1 + 1")

        assert code == 0
        assert payload["value"] == {"answer": 42}
        assert payload["type"] == "object"
        assert server.messages[-1]["params"]["awaitPromise"] is False

    def test_eval_raw_prints_only_the_value(self, drive_state, capsys):
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
        lines = [line for line in (output_dir / "timeline.jsonl").read_text(encoding = "utf-8").splitlines() if line.strip()]
        assert len(lines) >= 2
        fields = {transition["field"] for transition in payload["transitions"]}
        assert "boot_chrome" in fields
        assert payload["screenshots"]

class test_launch:
    def test_launch_writes_state_and_applies_the_env(self, drive_state, capsys, monkeypatch):
        captured: dict = {}

        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            captured["command"] = command
            captured["environment"] = environment
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        monkeypatch.setattr(app_drive, "_spawn", fake_spawn)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: {"cdp": 9444, "vite": 5444}[kind])
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")
        state = app_drive.read_state("demo")

        try:
            assert code == 0
            assert payload["pid"] == os.getpid()
            assert payload["port"] == 9444
            assert payload["vite_port"] == 5444
            assert captured["command"] == [str(Path(sys.executable).resolve())]
            assert captured["environment"][app_drive.CDP_ENV_VAR] == "--remote-debugging-port=9444"
            assert captured["environment"]["CELESTIA_VITE_PORT"] == "5444"
            assert state is not None
            assert state["pid"] == os.getpid()
            assert state["port"] == 9444
            registry = ports.list_entries()
            assert registry["demo:cdp"]["pid"] == os.getpid()
        finally:
            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_failed_launch_kills_its_child_and_removes_state(self, drive_state, capsys, monkeypatch):
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
        monkeypatch.setattr(ports, "acquire", lambda app, kind: {"cdp": 9444, "vite": 5444}[kind])
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "fail-demo", "--no-log", "--", "-c", "import time; time.sleep(30)")
        assert code == 1
        assert payload["error"]["code"] == "cdp-timeout"
        assert app_drive.read_state("fail-demo") is None
        assert app_drive.wait_pid_exit(spawned["child"].pid, 10) is not None

    def test_launch_refuses_to_replace_a_live_instance(self, drive_state, capsys):
        app_drive.write_state({"app": "demo", "pid": os.getpid(), "port": 9444, "exe": sys.executable}, app_drive.state_file_path("demo"))
        code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--no-log", "--no-wait")
        assert code == 1
        assert payload["error"]["code"] == "already-running"

    def test_launch_replace_terminates_the_previous_instance(self, drive_state, capsys, monkeypatch):
        previous = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, start_new_session = os.name != "nt")
        app_drive.write_state({"app": "demo", "pid": previous.pid, "port": 9333, "exe": str(Path(sys.executable).resolve())}, app_drive.state_file_path("demo"))

        def fake_spawn(command, environment, cwd, stdout_target, stderr_target):
            return types.SimpleNamespace(pid = os.getpid(), poll = lambda: None)

        monkeypatch.setattr(app_drive, "_spawn", fake_spawn)
        monkeypatch.setattr(ports, "acquire", lambda app, kind: {"cdp": 9444, "vite": 5444}[kind])

        try:
            code, payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "demo", "--replace", "--no-log", "--no-wait")
            assert code == 0
            assert payload["pid"] == os.getpid()
            assert app_drive.wait_pid_exit(previous.pid, 10) is not None
            assert app_drive.read_state("demo")["port"] == 9444
        finally:
            if previous.poll() is None:
                previous.kill()
                previous.wait(timeout = 10)

            app_drive.remove_state(app_drive.state_file_path("demo"))
            ports.release("demo", None)

    def test_launch_cleans_the_ports_claim_on_failure(self, drive_state, capsys, monkeypatch):
        monkeypatch.setattr(ports, "acquire", lambda app, kind: {"cdp": 9444, "vite": 5444}[kind])

        def refuse_wait(*args, **kwargs):
            raise cdp.cdp_error("cdp-timeout", "no page target")

        monkeypatch.setattr(app_drive.cdp, "wait_for_page", refuse_wait)
        code, _payload, _out = run_cli(capsys, "launch", sys.executable, "--app", "clean-fail", "--no-log", "--", "-c", "pass")
        assert code == 1
        assert app_drive.read_state("clean-fail") is None
        assert ports.list_entries() == {}

class test_close:
    def test_closes_the_process_recorded_in_the_state(self, drive_state, capsys):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin = subprocess.DEVNULL, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, start_new_session = os.name != "nt")
        state_path = app_drive.state_file_path("demo")
        app_drive.write_state({"app": "demo", "pid": child.pid, "port": 9444, "exe": str(Path(sys.executable).resolve())}, state_path)
        ports.claim("demo", "cdp", 9444, child.pid)

        try:
            code, payload, _out = run_cli(capsys, "close", "--app", "demo", "--grace", "5")
            assert code == 0
            assert payload["closed"] is True
            assert payload["state_file_removed"] is True
            assert app_drive.read_state("demo") is None
            assert ports.list_entries() == {}
            assert child.poll() is not None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout = 10)

    def test_missing_state_is_an_error(self, drive_state, capsys):
        code, payload, _out = run_cli(capsys, "close", "--app", "ghost")
        assert code == 1
        assert payload["error"]["code"] == "no-state"

    def test_pid_requires_force(self, drive_state, capsys):
        code, payload, _out = run_cli(capsys, "close", "--pid", "1234")
        assert code == 1
        assert payload["error"]["code"] == "unsafe-close"

    def test_executable_mismatch_is_refused(self, drive_state, capsys):
        app_drive.write_state({"app": "demo", "pid": os.getpid(), "port": 9444, "exe": "definitely-not-python.exe"}, app_drive.state_file_path("demo"))
        code, payload, _out = run_cli(capsys, "close", "--app", "demo")
        assert code == 1
        assert payload["error"]["code"] == "pid-mismatch"
