#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""app-drive - drive a Tauri desktop debug build over the WebView2 CDP channel.

The desktop debug loop used to be hand-written Node scripts per investigation
(launch the exe with --remote-debugging-port, poll /json/list, evaluate JS,
screenshot, click, capture the boot-chrome lifecycle). Every investigation
re-invented the same driver, and every driver leaked a process or a stray
window when something failed. This command is that driver, standardized:

  celestia-devtools app-drive launch <exe>     # CDP env + port handoff + wait
  celestia-devtools app-drive status           # page + boot-chrome state JSON
  celestia-devtools app-drive eval "JS"        # Runtime.evaluate, JSON result
  celestia-devtools app-drive shot out.png     # PNG screenshot
  celestia-devtools app-drive click CSS        # click a selector (DOM or mouse)
  celestia-devtools app-drive wait --selector CSS --timeout 10
  celestia-devtools app-drive watch --out DIR  # boot-chrome lifecycle capture
  celestia-devtools app-drive close            # close only what it launched
  celestia-devtools app-drive targets          # raw /json/list targets

Port handoff: launch takes its CDP port from the ports facility
(env/ports.py) under the app name (default: the exe stem), so two debug
shells never cross-wire; on success the port claim is re-pointed at the app
pid so it stays valid exactly as long as the app lives. Attach commands
resolve their port as: --port > the launch state file for --app > the ports
registry for --app > CELESTIA_CDP_PORT.

Process hygiene: launch records pid/port/exe in a per-app state file under
the celestia state directory. close only terminates the pid recorded there
(and refuses when that pid now belongs to a different executable), never a
user-started app. launch that fails to reach CDP kills its own child and
removes the state. A graceful close is attempted first (Windows taskkill
without /F posts WM_CLOSE; POSIX SIGTERM to the session group) and forced
after --grace seconds.

Platforms: WebView2 on Windows is the primary CDP transport. Linux
(WebKitGTK) and macOS (WKWebView) do not expose CDP, so on those hosts use
the frontend fallback: serve the built webui (vite-serve), open it in a
Chromium/Edge started with --remote-debugging-port=N, and attach app-drive
with --port N. Every subcommand except launch is platform-agnostic because
it only speaks CDP.

Protocol: see --help epilog; every command prints one JSON envelope.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence
from celestia_devtools.env import cdp
from celestia_devtools.env import ports

PROTOCOL_VERSION = 1
CDP_ENV_VAR = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
CDP_PORT_ENV = "CELESTIA_CDP_PORT"
VITE_PORT_ENV = "CELESTIA_VITE_PORT"
STATE_ENV = "CELESTIA_APP_DRIVE_STATE"
DEFAULT_TIMEOUT = 15.0
DEFAULT_LAUNCH_TIMEOUT = 30.0
DEFAULT_WAIT_TIMEOUT = 30.0
DEFAULT_WAIT_INTERVAL = 0.1
DEFAULT_WATCH_INTERVAL = 0.05
DEFAULT_WATCH_DURATION = 30.0
DEFAULT_CLOSE_GRACE = 5.0
WATCH_FIELDS = ("boot_chrome", "has_bar", "bar_driver", "degraded", "ready_state", "url", "title")
STATE_EXPRESSION = """(() => {
  const root = document.documentElement;
  const bar = document.getElementById("hk-boot-chrome");
  const body = document.body;
  const text = (body && body.innerText) || "";
  return {
    ready_state: document.readyState,
    title: document.title,
    url: location.href,
    boot_chrome: root ? root.getAttribute("data-hk-boot-chrome") : null,
    has_bar: !!bar,
    bar_driver: bar ? bar.getAttribute("data-hk-driver") : null,
    degraded: root ? root.getAttribute("data-hk-degraded") : null,
    body_text_len: text.length,
    body_text_head: text.slice(0, 200),
    tauri: !!(window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke),
    tauri_internals: !!window.__TAURI_INTERNALS__,
    root_children: body ? body.children.length : 0
  };
})()"""
PROTOCOL_EPILOG = """protocol (stable, version 1):
  Every command prints exactly one JSON object to stdout:
    {"ok": true, "protocol": 1, "cmd": "<name>", ...fields}
  A failure prints {"ok": false, "protocol": 1, "cmd": "<name>",
  "error": {"code": "<slug>", "message": "<text>", ...}} and exits 1.
  Usage errors follow argparse (stderr, exit 2). stdout is envelope-only;
  progress and diagnostics go to stderr, so scripts can pipe stdout to jq.
  --pretty indents the envelope for humans.

exit codes:
  0  command succeeded
  1  runtime failure (the envelope carries code/message)
  2  usage error (argparse)

process hygiene:
  launch writes <state-dir>/<app>.json (pid, exe, cdp port, vite port,
  log dir); close reads it and terminates only that pid. launch that never
  reaches CDP kills its own child and removes the state file.

state directory (override with CELESTIA_APP_DRIVE_STATE):
  Windows  %LOCALAPPDATA%/celestia/app-drive
  POSIX    $XDG_STATE_HOME/celestia/app-drive (or ~/.local/state/...)
"""

def envelope(command: str, **fields) -> dict:
    payload = {"ok": True, "protocol": PROTOCOL_VERSION, "cmd": command}
    payload.update(fields)
    return payload

def failure(command: str, code: str, message: str, **fields) -> dict:
    payload = {"ok": False, "protocol": PROTOCOL_VERSION, "cmd": command, "error": {"code": code, "message": message}}
    payload.update(fields)
    return payload

def emit(payload: dict, pretty: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii = False, indent = 2 if pretty else None))

def timestamp_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def state_root() -> Path:
    override = os.environ.get(STATE_ENV, "").strip()

    if override:
        return Path(override)

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))

    return base / "celestia" / "app-drive"

def app_slug(app: str) -> str:
    cleaned = "".join(character if (character.isalnum() or (character in "._-")) else "_" for character in str(app))
    return cleaned.strip("._") or "app"

def state_file_path(app: Optional[str], explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)

    if not app:
        raise cdp.cdp_error("no-app", "no application name: pass --app or --state-file")

    return state_root() / (app_slug(str(app)) + ".json")

def read_state(app: Optional[str] = None, explicit: Optional[str] = None) -> Optional[dict]:
    path = state_file_path(app, explicit)

    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding = "utf-8"))
    except (OSError, ValueError) as exc:
        raise cdp.cdp_error("bad-state", "cannot read the app-drive state file " + str(path) + ": " + str(exc)) from exc

    if not isinstance(data, dict):
        raise cdp.cdp_error("bad-state", "the app-drive state file is not a JSON object: " + str(path))

    return data

def write_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent = 2, ensure_ascii = False), encoding = "utf-8")
    os.replace(temporary, path)

def remove_state(path: Optional[Path]) -> bool:
    if path is None:
        return False

    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False

def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False

    return ports.pid_alive(int(pid))

def process_image(pid: int) -> Optional[str]:
    if os.name == "nt":
        return _windows_process_image(int(pid))

    try:
        return os.readlink("/proc/" + str(int(pid)) + "/exe")
    except OSError:
        return None

def _windows_process_image(pid: int) -> Optional[str]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))

    if not handle:
        return None

    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))

        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value

        return None
    finally:
        kernel32.CloseHandle(handle)

def _taskkill(pid: int, force: bool) -> int:
    command = ["taskkill", "/PID", str(int(pid)), "/T"]

    if force:
        command.append("/F")

    try:
        completed = subprocess.run(command, capture_output = True, text = True, timeout = 15)
    except (OSError, subprocess.TimeoutExpired):
        return -1

    return int(completed.returncode)

def _signal_tree(pid: int, signal_number: int) -> None:
    if os.name != "nt":
        try:
            os.killpg(int(pid), signal_number)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass

    try:
        os.kill(int(pid), signal_number)
    except (ProcessLookupError, OSError):
        pass

def wait_pid_exit(pid: int, timeout: float) -> Optional[int]:
    started = time.monotonic()

    while (time.monotonic() - started) <= timeout:
        if not pid_alive(pid):
            return int((time.monotonic() - started) * 1000)

        time.sleep(0.05)

    return None

def terminate_tree(pid: int, grace: float = DEFAULT_CLOSE_GRACE) -> dict:
    pid = int(pid)

    if not pid_alive(pid):
        return {"closed": True, "forced": False, "waited_ms": 0, "reason": "not-running"}

    forced = False

    if os.name == "nt":
        _taskkill(pid, False)
        waited = wait_pid_exit(pid, grace)

        if waited is None:
            _taskkill(pid, True)
            forced = True
            waited = wait_pid_exit(pid, grace)
    else:
        _signal_tree(pid, signal.SIGTERM)
        waited = wait_pid_exit(pid, grace)

        if waited is None:
            _signal_tree(pid, signal.SIGKILL)
            forced = True
            waited = wait_pid_exit(pid, grace)

    if waited is None:
        raise cdp.cdp_error("close-failed", "the process " + str(pid) + " did not exit within " + format(grace, "g") + "s" + (" (forced kill sent)" if forced else ""))

    return {"closed": True, "forced": forced, "waited_ms": waited, "reason": "terminated"}

def merge_webview2_arguments(existing: str, cdp_port: int) -> str:
    tokens = [token for token in str(existing or "").split() if token and not token.startswith("--remote-debugging-port=")]
    tokens.append("--remote-debugging-port=" + str(int(cdp_port)))
    return " ".join(tokens)

def parse_env_overrides(overrides: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}

    for item in overrides:
        key, separator, value = str(item).partition("=")

        if (not separator) or (not key):
            raise cdp.cdp_error("bad-env", "--env expects KEY=VALUE, got " + repr(item))

        parsed[key] = value

    return parsed

def build_launch_env(base, cdp_port: int, vite_port: Optional[int], overrides: Sequence[str]) -> dict[str, str]:
    environment = dict(base)
    environment[CDP_ENV_VAR] = merge_webview2_arguments(environment.get(CDP_ENV_VAR, ""), cdp_port)
    environment[CDP_PORT_ENV] = str(int(cdp_port))

    if vite_port is not None:
        environment[VITE_PORT_ENV] = str(int(vite_port))

    environment.update(parse_env_overrides(overrides))
    return environment

def build_launch_command(exe: str, extra: Sequence[str]) -> list[str]:
    arguments = [str(item) for item in extra]

    if arguments and (arguments[0] == "--"):
        arguments = arguments[1:]

    return [str(exe), *arguments]

def resolve_executable(exe: str) -> str:
    candidate = Path(str(exe))

    if candidate.is_file():
        return str(candidate.resolve())

    found = shutil.which(str(exe))

    if found:
        return str(Path(found).resolve())

    raise cdp.cdp_error("exe-not-found", "executable not found: " + str(exe) + " (pass a path, or a name on PATH)")

def default_app_name(exe: str) -> str:
    return Path(str(exe)).stem or "app"

def _open_launch_logs(app: str, log_dir: Optional[str], no_log: bool) -> tuple[Optional[Path], object, object]:
    if no_log:
        return None, subprocess.DEVNULL, subprocess.DEVNULL

    directory = Path(log_dir) if log_dir else (state_root() / "logs" / (app_slug(app) + "-" + time.strftime("%Y%m%d-%H%M%S")))
    directory.mkdir(parents = True, exist_ok = True)
    return directory, open(directory / "stdout.log", "wb"), open(directory / "stderr.log", "wb")

def _close_handles(*handles) -> None:
    for handle in handles:
        closing = getattr(handle, "close", None)

        if callable(closing):
            try:
                closing()
            except (OSError, ValueError):
                pass

def _spawn(command: list[str], environment: dict[str, str], cwd: Optional[str], stdout_target, stderr_target) -> subprocess.Popen:
    kwargs: dict = {"env": environment, "cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": stdout_target, "stderr": stderr_target}

    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        if creation_flags:
            kwargs["creationflags"] = creation_flags
    else:
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise cdp.cdp_error("launch-failed", "cannot start " + str(command[0]) + ": " + str(exc)) from exc

def resolve_attach_port(args) -> int:
    explicit = getattr(args, "port", None)

    if explicit is not None:
        return int(explicit)

    app = getattr(args, "app", None)

    if app:
        state_file = getattr(args, "state_file", None)
        state = read_state(str(app), state_file)

        if state is not None:
            if not state.get("port"):
                raise cdp.cdp_error("bad-state", "the launch state for app '" + str(app) + "' carries no CDP port")

            if not pid_alive(state.get("pid")):
                raise cdp.cdp_error("app-not-running", "app '" + str(app) + "' from " + str(state_file_path(str(app), state_file)) + " is not running; relaunch it with app-drive launch")

            return int(state["port"])

        return int(ports.acquire(str(app), "cdp"))

    environment_port = os.environ.get(CDP_PORT_ENV, "").strip()

    if environment_port.isdigit():
        return int(environment_port)

    raise cdp.cdp_error("no-port", "no CDP port: pass --port, pass --app for a port recorded by app-drive launch, or set " + CDP_PORT_ENV)

def read_page_state(client: cdp.cdp_client) -> dict:
    value = client.evaluate_value(STATE_EXPRESSION)

    if not isinstance(value, dict):
        raise cdp.cdp_error("bad-state", "the page state probe returned " + type(value).__name__ + ", not an object")

    return value

def public_target(target: dict) -> dict:
    return {"id": target.get("id"), "type": target.get("type"), "title": target.get("title"), "url": target.get("url"), "websocket_url": target.get("webSocketDebuggerUrl")}

def element_probe_expression(selector: str) -> str:
    quoted = json.dumps(str(selector))
    return "(() => { const el = document.querySelector(" + quoted + "); if (!el) { return { matched: false }; } el.scrollIntoView({ block: 'center', inline: 'center' }); const rect = el.getBoundingClientRect(); return { matched: true, tag: el.tagName.toLowerCase(), id: el.id || null, text: (el.innerText || el.textContent || '').trim().slice(0, 120), rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, disabled: !!el.disabled }; })()"

def element_click_expression(selector: str) -> str:
    quoted = json.dumps(str(selector))
    return "(() => { const el = document.querySelector(" + quoted + "); if (!el) { return { matched: false }; } el.click(); return { matched: true, clicked: true }; })()"

def boot_chrome_matches(target: str, current) -> bool:
    if target == "any":
        return current is not None

    if target == "none":
        return current is None

    return current == target

def condition_description(args) -> dict:
    if args.selector:
        return {"kind": "selector", "selector": args.selector}

    if args.expression:
        return {"kind": "expression", "expression": args.expression}

    return {"kind": "boot-chrome", "value": args.boot_chrome}

def check_condition(client: cdp.cdp_client, args) -> tuple[bool, dict]:
    if args.selector:
        value = client.evaluate_value(element_probe_expression(args.selector))
        matched = bool(isinstance(value, dict) and value.get("matched"))
        return matched, {"element": value}

    if args.expression:
        value = client.evaluate_value(args.expression)
        return bool(value), {"value": value}

    state = read_page_state(client)
    current = state.get("boot_chrome")
    return boot_chrome_matches(str(args.boot_chrome), current), {"boot_chrome": current, "state": state}

def image_dimensions(data: bytes) -> tuple[Optional[int], Optional[int]]:
    png_signature = bytes.fromhex("89504e470d0a1a0a")

    if data.startswith(png_signature) and (len(data) >= 24) and (data[12:16] == b"IHDR"):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    return None, None

def state_signature(state: dict) -> tuple:
    return tuple(str(state.get(field_name)) for field_name in WATCH_FIELDS)

def state_changes(previous: dict, current: dict) -> list[dict]:
    changes: list[dict] = []

    for field_name in WATCH_FIELDS:
        before = previous.get(field_name)
        after = current.get(field_name)

        if before != after:
            changes.append({"field": field_name, "from": before, "to": after})

    return changes

def watch_stop_reason(args, client: cdp.cdp_client, state: dict) -> Optional[str]:
    target = getattr(args, "stop_boot_chrome", None)

    if target is not None:
        if boot_chrome_matches(str(target), state.get("boot_chrome")):
            return "boot-chrome:" + str(target)

    selector = getattr(args, "stop_selector", None)

    if selector:
        value = client.evaluate_value(element_probe_expression(selector))

        if isinstance(value, dict) and value.get("matched"):
            return "selector:" + str(selector)

    return None

def _launched_summary(args) -> Optional[dict]:
    app = getattr(args, "app", None)

    if not app:
        return None

    state_file = getattr(args, "state_file", None)
    state = read_state(str(app), state_file)

    if state is None:
        return None

    return {"app": state.get("app", app), "pid": state.get("pid"), "running": pid_alive(state.get("pid")), "state_file": str(state_file_path(str(app), state_file)), "log_dir": state.get("log_dir")}

def cmd_targets(args) -> int:
    port = resolve_attach_port(args)
    targets = cdp.list_targets(port, timeout = args.timeout)
    page = cdp.select_page_target(targets)
    emit(envelope("targets", port = port, count = len(targets), targets = [public_target(item) for item in targets], selected_target_id = (page or {}).get("id")), args.pretty)
    return 0

def cmd_status(args) -> int:
    port = resolve_attach_port(args)
    targets = cdp.list_targets(port, timeout = args.timeout)
    page = cdp.select_page_target(targets, args.prefer_url)

    if page is None:
        raise cdp.cdp_error("no-page-target", "the CDP endpoint on 127.0.0.1:" + str(port) + " has no page target", {"targets": [public_target(item) for item in targets]})

    client = cdp.cdp_client.connect(port, target = page, timeout = args.timeout)

    try:
        state = read_page_state(client)
        events = list(client.events)
    finally:
        client.close()

    emit(envelope("status", port = port, target = public_target(page), target_count = len(targets), state = state, launched = _launched_summary(args), events = events[-20:]), args.pretty)
    return 0

def _resolve_expression(args) -> str:
    if args.file and args.expression:
        raise cdp.cdp_error("ambiguous-expression", "pass either a positional expression or --file, not both")

    if args.file:
        try:
            return Path(args.file).read_text(encoding = "utf-8")
        except OSError as exc:
            raise cdp.cdp_error("bad-expression", "cannot read the expression file " + str(args.file) + ": " + str(exc)) from exc

    if args.expression:
        return " ".join(str(item) for item in args.expression)

    raise cdp.cdp_error("no-expression", "no JavaScript expression: pass one positionally or with --file")

def cmd_eval(args) -> int:
    expression = _resolve_expression(args)
    port = resolve_attach_port(args)
    client = cdp.cdp_client.connect(port, timeout = args.timeout)

    try:
        result = client.evaluate(expression, await_promise = not args.no_await, return_by_value = True, user_gesture = not args.no_gesture)
    finally:
        client.close()

    value = result.get("value")

    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents = True, exist_ok = True)
        output.write_text(json.dumps(value, indent = 2, ensure_ascii = False), encoding = "utf-8")

    if args.raw:
        print(json.dumps(value, ensure_ascii = False))
        return 0

    emit(envelope("eval", port = port, type = result.get("type"), value = value, out = str(Path(args.out).resolve()) if args.out else None), args.pretty)
    return 0

def cmd_shot(args) -> int:
    port = resolve_attach_port(args)
    client = cdp.cdp_client.connect(port, timeout = args.timeout)

    try:
        data = client.screenshot(args.format, args.quality)
    finally:
        client.close()

    output = Path(args.out)
    output.parent.mkdir(parents = True, exist_ok = True)
    output.write_bytes(data)
    width, height = image_dimensions(data)
    emit(envelope("shot", port = port, path = str(output.resolve()), format = args.format, bytes = len(data), width = width, height = height), args.pretty)
    return 0

def cmd_click(args) -> int:
    port = resolve_attach_port(args)
    client = cdp.cdp_client.connect(port, timeout = args.timeout)

    try:
        probe = client.evaluate_value(element_probe_expression(args.selector))

        if not isinstance(probe, dict) or not probe.get("matched"):
            raise cdp.cdp_error("selector-not-found", "no element matches selector " + repr(args.selector), {"selector": args.selector})

        mode = "dom"

        if args.mouse:
            rect = probe.get("rect") or {}
            x = float(rect.get("x", 0)) + (float(rect.get("width", 0)) / 2)
            y = float(rect.get("y", 0)) + (float(rect.get("height", 0)) / 2)
            client.request("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1})
            client.request("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1})
            mode = "mouse"
        else:
            clicked = client.evaluate_value(element_click_expression(args.selector))

            if not isinstance(clicked, dict) or not clicked.get("matched"):
                raise cdp.cdp_error("selector-not-found", "the element for selector " + repr(args.selector) + " disappeared before the click", {"selector": args.selector})
    finally:
        client.close()

    emit(envelope("click", port = port, selector = args.selector, mode = mode, element = probe), args.pretty)
    return 0

def cmd_wait(args) -> int:
    port = resolve_attach_port(args)
    client = cdp.cdp_client.connect(port, timeout = args.timeout)
    started = time.monotonic()
    samples = 0
    detail: dict = {}

    try:
        while True:
            samples += 1
            matched = False

            try:
                matched, detail = check_condition(client, args)
            except cdp.cdp_error as exc:
                if exc.code in ("js-exception", "cdp-error"):
                    raise

                detail = {"error": str(exc)}

            waited_ms = int((time.monotonic() - started) * 1000)

            if matched:
                if args.shot:
                    data = client.screenshot("png")
                    output = Path(args.shot)
                    output.parent.mkdir(parents = True, exist_ok = True)
                    output.write_bytes(data)
                    detail = dict(detail)
                    detail["screenshot"] = str(output.resolve())

                emit(envelope("wait", port = port, condition = condition_description(args), matched = True, waited_ms = waited_ms, samples = samples, detail = detail), args.pretty)
                return 0

            if waited_ms >= int(args.timeout * 1000):
                raise cdp.cdp_error("timeout", "condition not satisfied within " + format(args.timeout, "g") + "s", {"condition": condition_description(args), "waited_ms": waited_ms, "samples": samples, "detail": detail})

            time.sleep(args.interval)
    finally:
        client.close()

def cmd_watch(args) -> int:
    port = resolve_attach_port(args)
    output_dir = Path(args.out)
    output_dir.mkdir(parents = True, exist_ok = True)
    timeline_path = output_dir / "timeline.jsonl"
    screenshots: list[str] = []
    transitions: list[dict] = []
    client = cdp.cdp_client.connect(port, timeout = args.timeout)
    started = time.monotonic()
    samples = 0
    last_state: dict = {}
    final_state: dict = {}
    stopped_reason = "duration"
    reloaded = False

    try:
        if args.reload:
            client.request("Page.enable")
            client.request("Page.reload", {"ignoreCache": False})
            reloaded = True

        deadline = started + args.duration

        with timeline_path.open("w", encoding = "utf-8") as timeline:
            while time.monotonic() < deadline:
                samples += 1
                elapsed_ms = int((time.monotonic() - started) * 1000)
                error_text: Optional[str] = None

                try:
                    state = read_page_state(client)
                except cdp.cdp_error as exc:
                    state = {}
                    error_text = str(exc)

                final_state = state
                signature = state_signature(state)
                previous_signature = state_signature(last_state)
                changed = (signature != previous_signature) or (samples == 1)
                record: dict = {"t_ms": elapsed_ms, "changed": changed, "state": state}

                if error_text is not None:
                    record["error"] = error_text

                timeline.write(json.dumps(record, ensure_ascii = False) + chr(10))
                timeline.flush()

                if changed and last_state:
                    for change in state_changes(last_state, state):
                        transition = dict(change)
                        transition["t_ms"] = elapsed_ms
                        transitions.append(transition)

                if changed and args.shots and (samples > 1):
                    shot_name = "shot-" + str(len(screenshots) + 1).zfill(2) + "-" + str(elapsed_ms) + "ms.png"

                    try:
                        data = client.screenshot("png")
                        shot_path = output_dir / shot_name
                        shot_path.write_bytes(data)
                        screenshots.append(str(shot_path.resolve()))
                    except cdp.cdp_error as exc:
                        transitions.append({"t_ms": elapsed_ms, "field": "screenshot-error", "from": None, "to": str(exc)})

                last_state = state

                if error_text is None:
                    try:
                        stop_reason = watch_stop_reason(args, client, state)
                    except cdp.cdp_error as exc:
                        stop_reason = None
                        transitions.append({"t_ms": elapsed_ms, "field": "stop-probe-error", "from": None, "to": str(exc)})

                    if stop_reason is not None:
                        stopped_reason = stop_reason
                        break

                time.sleep(args.interval)
    finally:
        client.close()

    summary = {"port": port, "reloaded": reloaded, "duration_ms": int((time.monotonic() - started) * 1000), "interval_ms": int(args.interval * 1000), "samples": samples, "stopped_reason": stopped_reason, "transitions": transitions, "screenshots": screenshots, "final_state": final_state, "timeline": str(timeline_path.resolve()), "summary_file": str((output_dir / "watch.json").resolve())}
    (output_dir / "watch.json").write_text(json.dumps(summary, indent = 2, ensure_ascii = False), encoding = "utf-8")
    emit(envelope("watch", **summary), args.pretty)
    return 0

def cmd_launch(args) -> int:
    executable = resolve_executable(args.exe)
    app = str(args.app or default_app_name(executable))
    state_path = state_file_path(app, args.state_file)
    existing = read_state(app, args.state_file)

    if (existing is not None) and pid_alive(existing.get("pid")):
        if not args.replace:
            raise cdp.cdp_error("already-running", "app '" + app + "' was already launched (pid " + str(existing.get("pid")) + ", CDP port " + str(existing.get("port")) + "); run app-drive close --app " + app + " or pass --replace")

        _verify_launched_process(existing, int(existing.get("pid") or 0))
        terminate_tree(int(existing.get("pid") or 0), grace = args.grace)

    port = int(args.port) if args.port is not None else int(ports.acquire(app, "cdp"))
    vite_port = int(args.vite_port) if args.vite_port is not None else int(ports.acquire(app, "vite"))
    environment = build_launch_env(os.environ, port, vite_port, args.env)
    command = build_launch_command(executable, args.extra)
    log_dir, stdout_target, stderr_target = _open_launch_logs(app, args.log_dir, args.no_log)
    started = time.monotonic()
    child = _spawn(command, environment, args.cwd, stdout_target, stderr_target)
    _close_handles(stdout_target, stderr_target)
    launch_ms = int((time.monotonic() - started) * 1000)
    state = {"protocol": PROTOCOL_VERSION, "app": app, "pid": child.pid, "port": port, "vite_port": vite_port, "exe": executable, "cwd": args.cwd or str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": str(log_dir.resolve()) if log_dir is not None else None, "command": command}
    write_state(state, state_path)
    ports.claim(app, "cdp", port, child.pid)
    ports.claim(app, "vite", vite_port, child.pid)

    if args.no_wait:
        emit(envelope("launch", app = app, pid = child.pid, port = port, vite_port = vite_port, page = None, waited = False, launch_ms = launch_ms, state_file = str(state_path.resolve()), log_dir = state["log_dir"]), args.pretty)
        return 0

    try:
        page, cdp_ms = cdp.wait_for_page(port, timeout = args.wait_timeout, prefer_url = args.prefer_url)
    except cdp.cdp_error as exc:
        exit_code = child.poll()

        try:
            terminate_tree(child.pid, grace = args.grace)
        except cdp.cdp_error:
            pass

        remove_state(state_path)
        ports.release(app, None)
        suffix = (" (app exit code " + str(exit_code) + ")") if exit_code is not None else ""
        raise cdp.cdp_error(exc.code, str(exc) + suffix, exc.details) from exc

    emit(envelope("launch", app = app, pid = child.pid, port = port, vite_port = vite_port, page = public_target(page), waited = True, launch_ms = launch_ms, cdp_ms = cdp_ms, state_file = str(state_path.resolve()), log_dir = state["log_dir"]), args.pretty)
    return 0

def _verify_launched_process(state: dict, pid: int) -> None:
    recorded = str(state.get("exe") or "")
    current = process_image(pid)

    if (not recorded) or (not current):
        return

    if Path(recorded).name.lower() != Path(current).name.lower():
        raise cdp.cdp_error("pid-mismatch", "pid " + str(pid) + " now belongs to " + str(current) + ", not the launched " + str(recorded) + "; refusing to close it (use --pid " + str(pid) + " --force only if you are sure)")

def cmd_close(args) -> int:
    app = getattr(args, "app", None)
    state_path: Optional[Path] = None
    state: Optional[dict] = None

    if args.pid is not None:
        if not args.force:
            raise cdp.cdp_error("unsafe-close", "closing an arbitrary pid needs --force; prefer --app so app-drive only closes the process it launched")

        pid = int(args.pid)
    else:
        if not app:
            raise cdp.cdp_error("no-app", "no application name: pass --app (or --pid N --force)")

        state_path = state_file_path(str(app), args.state_file)
        state = read_state(str(app), args.state_file)

        if state is None:
            raise cdp.cdp_error("no-state", "no launch state for app '" + str(app) + "' at " + str(state_path) + "; app-drive only closes apps it launched (break-glass: --pid N --force)")

        recorded_pid = state.get("pid")

        if not recorded_pid:
            raise cdp.cdp_error("bad-state", "the launch state " + str(state_path) + " carries no pid")

        pid = int(recorded_pid)
        app = str(state.get("app") or app)

        if pid_alive(pid):
            _verify_launched_process(state, pid)

    result = terminate_tree(pid, args.grace)
    removed = remove_state(state_path) if state_path is not None else False
    released = False

    if (state is not None) and app:
        ports.release(str(app), None)
        released = True

    payload = {"app": app, "pid": pid}
    payload.update(result)
    emit(envelope("close", **payload, state_file_removed = removed, ports_released = released), args.pretty)
    return 0

HANDLERS = {"targets": cmd_targets, "status": cmd_status, "eval": cmd_eval, "shot": cmd_shot, "click": cmd_click, "wait": cmd_wait, "watch": cmd_watch, "launch": cmd_launch, "close": cmd_close}

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "app-drive", description = "Drive a Tauri desktop debug build over the WebView2 CDP channel.", epilog = PROTOCOL_EPILOG, formatter_class = argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest = "command", required = True, metavar = "COMMAND")
    targets = subparsers.add_parser("targets", help = "list the CDP targets (/json/list)")
    _add_attach_arguments(targets)
    status = subparsers.add_parser("status", help = "page target + boot-chrome/page state as JSON")
    _add_attach_arguments(status)
    status.add_argument("--prefer-url", default = "", help = "prefer the page target whose URL starts with this prefix")
    eval_parser = subparsers.add_parser("eval", help = "evaluate JavaScript and print the JSON value")
    _add_attach_arguments(eval_parser)
    eval_parser.add_argument("expression", nargs = "*", help = "JavaScript expression (awaitPromise/returnByValue are on by default)")
    eval_parser.add_argument("--file", default = None, help = "read the expression from a file instead")
    eval_parser.add_argument("--no-await", action = "store_true", help = "do not await a returned promise")
    eval_parser.add_argument("--no-gesture", action = "store_true", help = "do not mark the evaluation as user-gesture")
    eval_parser.add_argument("--out", default = None, help = "write the JSON value to this file")
    eval_parser.add_argument("--raw", action = "store_true", help = "print only the bare JSON value (for piping)")
    shot = subparsers.add_parser("shot", help = "capture a screenshot to a file")
    _add_attach_arguments(shot)
    shot.add_argument("out", help = "output image path")
    shot.add_argument("--format", choices = ("png", "jpeg"), default = "png", help = "image format (default png)")
    shot.add_argument("--quality", type = int, default = None, help = "JPEG quality 0-100")
    click = subparsers.add_parser("click", help = "click the element matching a CSS selector")
    _add_attach_arguments(click)
    click.add_argument("selector", help = "CSS selector")
    click.add_argument("--mouse", action = "store_true", help = "dispatch real mouse events at the element center instead of element.click()")
    wait = subparsers.add_parser("wait", help = "poll until a selector/expression/boot-chrome state matches")
    _add_attach_arguments(wait)
    wait_conditions = wait.add_mutually_exclusive_group(required = True)
    wait_conditions.add_argument("--selector", default = None, help = "wait until this CSS selector matches an element")
    wait_conditions.add_argument("--expr", dest = "expression", default = None, help = "wait until this JavaScript expression is truthy")
    wait_conditions.add_argument("--boot-chrome", default = None, help = "wait until data-hk-boot-chrome equals this value (any | none | literal)")
    wait.add_argument("--interval", type = float, default = DEFAULT_WAIT_INTERVAL, metavar = "SECONDS", help = "poll interval (default 0.1)")
    wait.add_argument("--shot", default = None, help = "capture a PNG screenshot when the condition matches")
    watch = subparsers.add_parser("watch", help = "record page/boot-chrome state transitions (and screenshots)")
    _add_attach_arguments(watch)
    watch.add_argument("--out", required = True, help = "directory for timeline.jsonl, watch.json and screenshots")
    watch.add_argument("--duration", type = float, default = DEFAULT_WATCH_DURATION, metavar = "SECONDS", help = "maximum recording time (default 30)")
    watch.add_argument("--interval", type = float, default = DEFAULT_WATCH_INTERVAL, metavar = "SECONDS", help = "sample interval (default 0.05)")
    watch.add_argument("--reload", action = "store_true", help = "reload the page first, to capture a fresh boot-chrome lifecycle")
    watch.add_argument("--shots", action = argparse.BooleanOptionalAction, default = True, help = "capture a screenshot on every state change (default on)")
    watch.add_argument("--stop-boot-chrome", default = None, help = "stop early once data-hk-boot-chrome equals this value (any | none | literal)")
    watch.add_argument("--stop-selector", default = None, help = "stop early once this CSS selector matches")
    launch = subparsers.add_parser("launch", help = "launch a debug build with the CDP environment and wait for its page")
    launch.add_argument("exe", help = "path to the desktop debug executable")
    _add_attach_arguments(launch, with_app = False)
    launch.add_argument("--app", default = None, help = "application name for the port/state keys (default: the exe stem)")
    launch.add_argument("--cwd", default = None, help = "working directory for the app process")
    launch.add_argument("--vite-port", type = int, default = None, help = "override the allocated vite port")
    launch.add_argument("--wait-timeout", type = float, default = DEFAULT_LAUNCH_TIMEOUT, metavar = "SECONDS", help = "how long to wait for the CDP page target (default 30)")
    launch.add_argument("--no-wait", action = "store_true", help = "return right after spawning, do not wait for CDP")
    launch.add_argument("--replace", action = "store_true", help = "replace a live app-drive-launched instance of this app")
    launch.add_argument("--env", action = "append", default = [], metavar = "KEY=VALUE", help = "extra environment variable for the app (repeatable; use WEBVIEW2_USER_DATA_FOLDER for a second instance of a WebView2 app)")
    launch.add_argument("--log-dir", default = None, help = "directory for stdout.log/stderr.log (default: state-dir/logs/<app>-<stamp>)")
    launch.add_argument("--no-log", action = "store_true", help = "discard the app stdout/stderr instead of writing log files")
    launch.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-close budget before a forced kill (default 5)")
    launch.add_argument("--prefer-url", default = "", help = "prefer the page target whose URL starts with this prefix")
    launch.add_argument("extra", nargs = "*", help = "extra app arguments (pass them after --)")
    close = subparsers.add_parser("close", help = "close the app instance that app-drive launched")
    close.add_argument("--app", default = None, help = "application name of a previous launch")
    close.add_argument("--state-file", default = None, help = "override the launch state file")
    close.add_argument("--pid", type = int, default = None, help = "break-glass: close this pid (requires --force)")
    close.add_argument("--force", action = "store_true", help = "required with --pid; also skips the executable identity check")
    close.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-close budget before a forced kill (default 5)")
    close.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")
    return parser

def _add_attach_arguments(parser: argparse.ArgumentParser, with_app: bool = True, with_state_file: bool = True) -> None:
    if with_app:
        parser.add_argument("--app", default = None, help = "application name recorded by app-drive launch")

    if with_state_file:
        parser.add_argument("--state-file", default = None, help = "override the launch state file for --app")

    parser.add_argument("--port", type = int, default = None, help = "attach to this CDP port directly (overrides --app and CELESTIA_CDP_PORT)")
    parser.add_argument("--timeout", type = float, default = DEFAULT_TIMEOUT, metavar = "SECONDS", help = "CDP connect/response timeout (default 15)")
    parser.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = list(argv if argv is not None else sys.argv[1:])
    parsed = parser.parse_args(arguments)
    handler = HANDLERS.get(parsed.command)

    if handler is None:
        print("error: unknown command " + repr(parsed.command), file = sys.stderr)
        return 2

    try:
        return int(handler(parsed) or 0)
    except cdp.cdp_error as exc:
        emit(failure(parsed.command, exc.code, str(exc), **exc.details), getattr(parsed, "pretty", False))
        return 1
    except KeyboardInterrupt:
        print("app-drive: interrupted", file = sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        emit(failure(parsed.command, "error", type(exc).__name__ + ": " + str(exc)), getattr(parsed, "pretty", False))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
