#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""app-drive - drive a Tauri desktop debug build over the WebView2 CDP channel.

The desktop debug loop used to be hand-written Node scripts per investigation
(launch the exe with --remote-debugging-port, poll /json/list, evaluate JS,
screenshot, click, capture the boot-chrome lifecycle). Every investigation
re-invented the same driver, and every driver leaked a process or a stray
window when something failed. This command is that driver, standardized:

  celestia-devtools app-drive launch <exe>     # CDP env + port + wait
  celestia-devtools app-drive status           # page + boot-chrome state JSON
  celestia-devtools app-drive eval "JS"        # Runtime.evaluate, JSON result
  celestia-devtools app-drive shot out.png     # PNG screenshot
  celestia-devtools app-drive click CSS        # click a selector (DOM or mouse)
  celestia-devtools app-drive wait --selector CSS --timeout 10
  celestia-devtools app-drive watch --out DIR  # boot-chrome lifecycle capture
  celestia-devtools app-drive close            # close only what it launched
  celestia-devtools app-drive targets          # raw /json/list targets

Scope: app-drive owns the CDP channel ONLY. It allocates/claims a CDP port
through env/ports.py, sets WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS, and drives
the page; it does NOT allocate a vite port and does NOT start or verify the
frontend server. A compiled debug build keeps the devUrl baked in at build
time, so exporting a CELESTIA_VITE_PORT it never reads would only pretend to
prevent cross-wiring. Serve the frontend with vite-serve/ports and point the
build at it at build time.

Port handoff: launch allocates its CDP port from the ports facility under the
app key (--app, or a hash of the resolved exe path), and re-points the claim
at the app pid so the claim stays valid exactly as long as the app lives.
Attach commands (everything except launch) NEVER allocate or claim a port:
they resolve --port > the launch state for --app > CELESTIA_CDP_PORT.

Process identity (the safety contract): launch records the resolved full exe
path, the process creation token (Windows GetProcessTimes FILETIME, Linux
/proc starttime) and the observable kind. close/--replace require ALL of it
to match the live process; when any piece is missing or unreadable they FAIL
CLOSED and refuse to touch the process. The token is re-checked immediately
before the kill. A state file written by an older schema (no token) is
refused as state-outdated. --pid N --force is the single explicit
break-glass; it is unverified by construction and documented as such.

Launch hygiene: registration failures (state write, port claim) terminate the
spawned child; a cleanup that cannot terminate the child KEEPS the state file
and port claim and reports the pid so nothing becomes unmanageable; Ctrl+C
goes through the same cleanup path and exits 130 with a JSON envelope.

Platforms: WebView2 on Windows is the primary CDP transport. Linux
(WebKitGTK) and macOS (WKWebView) do not expose CDP, so use the frontend
fallback: serve the built webui (vite-serve), open it in a Chromium/Edge
started with --remote-debugging-port=N, and attach app-drive with --port N.
Every subcommand except launch is platform-agnostic because it only speaks
CDP. Identity verification is unavailable on platforms without /proc
(macOS): close/--replace fail closed there.

Protocol: see --help epilog; every command prints exactly one ASCII JSON
envelope (ensure_ascii, so a gbk/cp936 stdout cannot crash on out-of-codepage
page text) to stdout.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
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

PROTOCOL_VERSION = 2
STATE_SCHEMA = 2
CDP_ENV_VAR = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
CDP_PORT_ENV = "CELESTIA_CDP_PORT"
STATE_ENV = "CELESTIA_APP_DRIVE_STATE"
DEFAULT_TIMEOUT = 15.0
DEFAULT_LAUNCH_TIMEOUT = 30.0
DEFAULT_WAIT_TIMEOUT = 30.0
DEFAULT_WAIT_INTERVAL = 0.1
DEFAULT_WATCH_INTERVAL = 0.05
DEFAULT_WATCH_DURATION = 30.0
DEFAULT_CLOSE_GRACE = 5.0
APP_KEY_PREFIX = "exe-"
WATCH_FIELDS = ("boot_chrome", "has_bar", "bar_driver", "degraded", "ready_state", "url", "title")
_TASKKILL_PATH: Optional[str] = None
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
PROTOCOL_EPILOG = """protocol (stable, version 2):
  Every command prints exactly one ASCII JSON object to stdout:
    {"ok": true, "protocol": 2, "cmd": "<name>", ...fields}
  A failure prints {"ok": false, "protocol": 2, "cmd": "<name>",
  "error": {"code": "<slug>", "message": "<text>", ...}} and exits 1.
  Usage errors follow argparse (stderr, exit 2). stdout is envelope-only and
  ASCII-safe (ensure_ascii): progress goes to stderr, so stdout pipes into
  jq. --pretty indents the envelope for humans. --raw (eval only) prints the
  bare ASCII JSON value instead of the envelope.

exit codes:
  0    command succeeded
  1    runtime failure (the envelope carries code/message)
  2    usage error (argparse)
  130  interrupted (Ctrl+C); cleanup ran and the envelope was printed

safe-close contract:
  launch records the resolved exe path + process creation token + token kind
  in <state-dir>/<app>.json (schema 2). close and --replace require all three
  to match the live process, re-check immediately before the kill, and fail
  closed (state kept, exit 1) when anything is missing, unreadable or
  different. A state file from an older schema is refused (state-outdated).
  --pid N --force is the explicit unverified break-glass.

scope:
  CDP only: launch allocates/claims the CDP port and sets
  WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS; no vite port is allocated, claimed
  or exported (a compiled debug build keeps its baked devUrl).
  Attach commands never allocate or claim a port.

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
    """Print the signed envelope as pure ASCII (gbk stdout cannot crash)."""
    print(json.dumps(payload, ensure_ascii = True, indent = 2 if pretty else None))

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

def default_app_key(executable: str) -> str:
    """Stable app key from the resolved exe path (same-stem exes never share)."""
    normalized = os.path.normcase(os.path.abspath(str(executable)))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return APP_KEY_PREFIX + digest

def state_file_path(app: Optional[str] = None, explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)

    if not app:
        raise cdp.cdp_error("no-app", "no application key: pass --app or --state-file")

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

    schema = data.get("schema")

    if schema != STATE_SCHEMA:
        raise cdp.cdp_error("state-outdated", "the launch state " + str(path) + " was written by an older app-drive (schema " + str(schema) + ", expected " + str(STATE_SCHEMA) + "); it carries no process creation token, so app-drive will not touch that process (fail closed). Relaunch with the current app-drive, and close the old process manually if needed.")

    return data

def write_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(state, indent = 2, ensure_ascii = True), encoding = "utf-8")
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

def state_cdp_port(state: dict, path: Optional[Path] = None) -> int:
    port = state.get("cdp_port")

    if (not isinstance(port, int)) or (port < 1) or (port > 65535):
        raise cdp.cdp_error("bad-state", "the launch state " + str(path) + " carries no usable cdp_port")

    return int(port)

def state_port_for_attach(state: dict, path: Optional[Path] = None) -> int:
    if not pid_alive(state.get("pid")):
        raise cdp.cdp_error("app-not-running", "app " + repr(state.get("app")) + " from " + str(path) + " is not running; relaunch it with app-drive launch")

    return state_cdp_port(state, path)

def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False

    return ports.pid_alive(int(pid))

def _windows_process_identity(pid: int) -> Optional[dict]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))

    if not handle:
        return None

    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))

        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None

        creation = ctypes.c_ulonglong(0)
        exit_time = ctypes.c_ulonglong(0)
        kernel_time = ctypes.c_ulonglong(0)
        user_time = ctypes.c_ulonglong(0)

        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None

        return {"image": buffer.value, "create_token": str(creation.value), "create_kind": "windows-filetime"}
    finally:
        kernel32.CloseHandle(handle)

def _posix_process_identity(pid: int) -> Optional[dict]:
    if not os.path.isdir("/proc"):
        return None

    try:
        image = os.readlink("/proc/" + str(int(pid)) + "/exe")
    except OSError:
        return None

    try:
        stat_text = Path("/proc/" + str(int(pid)) + "/stat").read_text(encoding = "utf-8", errors = "replace")
    except OSError:
        return None

    closing = stat_text.rfind(")")

    if closing < 0:
        return None

    fields = stat_text[closing + 2:].split()

    if len(fields) <= 19:
        return None

    return {"image": image, "create_token": fields[19], "create_kind": "proc-starttime"}

def read_process_identity(pid: int) -> Optional[dict]:
    """Best-effort identity of a live process: full image path + creation token.

    Returns None when the platform or the process cannot be inspected; every
    caller treats None as "fail closed".
    """

    if not pid:
        return None

    if os.name == "nt":
        return _windows_process_identity(int(pid))

    return _posix_process_identity(int(pid))

def same_image_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))

def verify_process_identity(state: dict, pid: int) -> dict:
    """Require the recorded path + creation token to match the live process.

    Raises identity-unavailable when anything needed is missing or
    unreadable, and pid-mismatch when the live process is not the recorded
    one. Never returns a "probably" answer: this is the S0 safety boundary.
    """
    recorded_exe = str(state.get("exe") or "")
    recorded_token = str(state.get("create_token") or "")
    recorded_kind = str(state.get("create_kind") or "")

    if (not recorded_exe) or (not recorded_token) or (not recorded_kind):
        raise cdp.cdp_error("identity-unavailable", "the launch state for pid " + str(pid) + " has no recorded executable path/creation token; refusing to touch it (fail closed)")

    current = read_process_identity(int(pid))

    if current is None:
        raise cdp.cdp_error("identity-unavailable", "cannot read the executable path and creation time of pid " + str(pid) + "; refusing to touch it (fail closed)")

    if not same_image_path(recorded_exe, str(current.get("image") or "")):
        raise cdp.cdp_error("pid-mismatch", "pid " + str(pid) + " now runs " + repr(current.get("image")) + ", not the launched " + repr(recorded_exe) + "; refusing to touch it")

    if (str(current.get("create_kind") or "") != recorded_kind) or (str(current.get("create_token") or "") != recorded_token):
        raise cdp.cdp_error("pid-mismatch", "pid " + str(pid) + " was reused (creation token " + repr(current.get("create_token")) + " != recorded " + repr(recorded_token) + "); refusing to touch it")

    return current

def taskkill_path() -> str:
    global _TASKKILL_PATH

    if _TASKKILL_PATH is None:
        candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"

        if candidate.is_file():
            _TASKKILL_PATH = str(candidate)
        else:
            _TASKKILL_PATH = shutil.which("taskkill") or "taskkill"

    return _TASKKILL_PATH

def _taskkill(pid: int, force: bool) -> int:
    command = [taskkill_path(), "/PID", str(int(pid)), "/T"]

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

def terminate_verified(state: dict, pid: int, grace: float = DEFAULT_CLOSE_GRACE) -> dict:
    """Verify identity, then verify AGAIN immediately before the kill."""
    verify_process_identity(state, pid)
    verify_process_identity(state, pid)
    return terminate_tree(pid, grace)

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

def build_launch_env(base, cdp_port: int, overrides: Sequence[str]) -> dict[str, str]:
    environment = dict(base)
    environment[CDP_ENV_VAR] = merge_webview2_arguments(environment.get(CDP_ENV_VAR, ""), cdp_port)
    environment[CDP_PORT_ENV] = str(int(cdp_port))
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

def acquire_cdp_port(app: str) -> int:
    try:
        port = int(ports.acquire(str(app), "cdp"))
    except SystemExit as exc:
        raise cdp.cdp_error("port-unavailable", "no free CDP port for app " + repr(app) + ": " + str(exc)) from None
    except OSError as exc:
        raise cdp.cdp_error("port-registry", "cannot read/write the ports registry: " + str(exc)) from exc

    if (port < 1) or (port > 65535):
        raise cdp.cdp_error("port-unavailable", "the ports registry handed out an invalid CDP port: " + str(port))

    return port

def claim_cdp_port(app: str, port: int, pid: int) -> None:
    try:
        ports.claim(str(app), "cdp", int(port), int(pid))
    except SystemExit as exc:
        raise cdp.cdp_error("port-unavailable", "cannot claim CDP port " + str(port) + ": " + str(exc)) from None
    except OSError as exc:
        raise cdp.cdp_error("port-registry", "cannot write the ports registry: " + str(exc)) from exc

def release_ports(app: Optional[str]) -> bool:
    if not app:
        return False

    try:
        ports.release(str(app), None)
        return True
    except (SystemExit, OSError):
        return False

def resolve_attach_port(args) -> int:
    explicit = getattr(args, "port", None)

    if explicit is not None:
        port = int(explicit)

        if (port < 1) or (port > 65535):
            raise cdp.cdp_error("bad-port", "CDP port out of range: " + str(port))

        return port

    state_file = getattr(args, "state_file", None)
    app = getattr(args, "app", None)

    if state_file:
        state = read_state(None, state_file)

        if state is None:
            raise cdp.cdp_error("no-launch-state", "no app-drive launch state at " + str(state_file))

        return state_port_for_attach(state, Path(state_file))

    if app:
        state = read_state(str(app))

        if state is None:
            raise cdp.cdp_error("no-launch-state", "no app-drive launch state for app " + repr(str(app)) + "; attach mode never allocates or claims a port (launch the app with app-drive, or pass --port)")

        return state_port_for_attach(state, state_file_path(str(app)))

    environment_port = os.environ.get(CDP_PORT_ENV, "").strip()

    if environment_port.isdigit():
        port = int(environment_port)

        if (port < 1) or (port > 65535):
            raise cdp.cdp_error("bad-port", "CELESTIA_CDP_PORT is out of range: " + str(port))

        return port

    raise cdp.cdp_error("no-port", "no CDP port: pass --port, pass --app for a launch recorded by app-drive, or set " + CDP_PORT_ENV)

def read_page_state(client: cdp.cdp_client) -> dict:
    value = client.evaluate_value(STATE_EXPRESSION)

    if not isinstance(value, dict):
        raise cdp.cdp_error("bad-state", "the page state probe returned " + type(value).__name__ + ", not an object")

    return value

def public_target(target: dict) -> dict:
    return {"id": target.get("id"), "type": target.get("type"), "title": target.get("title"), "url": target.get("url"), "websocket_url": target.get("webSocketDebuggerUrl")}

def select_target(port: int, args, timeout: float) -> tuple[list[dict], dict]:
    targets = cdp.list_targets(port, timeout = timeout)
    target_id = str(getattr(args, "target", None) or "")
    page = cdp.select_page_target(targets, getattr(args, "prefer_url", "") or "", target_id)

    if page is None:
        if target_id:
            raise cdp.cdp_error("target-not-found", "no CDP target with id " + repr(target_id), {"targets": [public_target(item) for item in targets]})

        raise cdp.cdp_error("no-page-target", "the CDP endpoint on 127.0.0.1:" + str(port) + " has no page target", {"targets": [public_target(item) for item in targets]})

    return targets, page

def connect_page(args) -> tuple[int, dict, cdp.cdp_client]:
    port = resolve_attach_port(args)
    _targets, page = select_target(port, args, args.timeout)
    client = cdp.cdp_client.connect(port, target = page, timeout = args.timeout)
    return port, page, client

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

def launched_summary(app: Optional[str], state_file: Optional[str] = None) -> Optional[dict]:
    if not app:
        return None

    state = read_state(str(app), state_file)

    if state is None:
        return None

    return {"app": state.get("app", app), "pid": state.get("pid"), "running": pid_alive(state.get("pid")), "cdp_port": state.get("cdp_port"), "exe": state.get("exe"), "state_file": str(state_file_path(str(app), state_file)), "log_dir": state.get("log_dir")}

def cmd_targets(args) -> int:
    port = resolve_attach_port(args)
    targets, page = select_target(port, args, args.timeout)
    emit(envelope("targets", port = port, count = len(targets), targets = [public_target(item) for item in targets], selected_target_id = page.get("id")), args.pretty)
    return 0

def cmd_status(args) -> int:
    port, page, client = connect_page(args)

    try:
        state = read_page_state(client)
        events = list(client.events)
    finally:
        client.close()

    emit(envelope("status", port = port, target = public_target(page), state = state, launched = launched_summary(args.app, args.state_file), events = events[-20:]), args.pretty)
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
    port, _page, client = connect_page(args)

    try:
        result = client.evaluate(expression, await_promise = not args.no_await, return_by_value = True, user_gesture = not args.no_gesture)
    finally:
        client.close()

    value = result.get("value")

    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents = True, exist_ok = True)
        output.write_text(json.dumps(value, indent = 2, ensure_ascii = True), encoding = "utf-8")

    if args.raw:
        print(json.dumps(value, ensure_ascii = True))
        return 0

    emit(envelope("eval", port = port, type = result.get("type"), value = value, out = str(Path(args.out).resolve()) if args.out else None), args.pretty)
    return 0

def cmd_shot(args) -> int:
    port, _page, client = connect_page(args)

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
    port, _page, client = connect_page(args)

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
    port, _page, client = connect_page(args)
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
    port, _page, client = connect_page(args)
    output_dir = Path(args.out)
    output_dir.mkdir(parents = True, exist_ok = True)
    timeline_path = output_dir / "timeline.jsonl"
    screenshots: list[str] = []
    transitions: list[dict] = []
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

                timeline.write(json.dumps(record, ensure_ascii = True) + chr(10))
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
    (output_dir / "watch.json").write_text(json.dumps(summary, indent = 2, ensure_ascii = True), encoding = "utf-8")
    emit(envelope("watch", **summary), args.pretty)
    return 0

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

def _cleanup_note(cleanup: dict) -> str:
    if cleanup.get("terminated"):
        return "; the spawned app was terminated and its state/claim removed"

    if cleanup.get("state_kept"):
        return "; WARNING: the spawned app could not be terminated - state kept at " + str(cleanup.get("state_file")) + " (retry with app-drive close --app " + str(cleanup.get("app")) + "; pid " + str(cleanup.get("pid")) + ")"

    return "; cleanup of the spawned app is incomplete (pid " + str(cleanup.get("pid")) + ")"

def _abort_launch(child: Optional[subprocess.Popen], state_path: Path, app: str, state: Optional[dict], grace: float) -> dict:
    result: dict = {"pid": None, "terminated": False, "state_kept": False, "claims_released": False, "app": app, "state_file": str(state_path.resolve())}

    if child is not None:
        result["pid"] = child.pid

        try:
            terminate_tree(child.pid, grace = grace)
            result["terminated"] = True
        except cdp.cdp_error as exc:
            result["terminate_error"] = str(exc)

    if result["terminated"] or (child is None):
        result["state_removed"] = remove_state(state_path)
        result["claims_released"] = release_ports(app)
        return result

    if state is not None:
        try:
            write_state(state, state_path)
            result["state_kept"] = True
        except (OSError, ValueError) as exc:
            result["state_write_error"] = str(exc)

        try:
            claim_cdp_port(app, int(state.get("cdp_port") or 0), int(state.get("pid") or 0))
        except cdp.cdp_error as exc:
            result["claim_error"] = str(exc)
    else:
        try:
            partial = {"schema": STATE_SCHEMA, "protocol": PROTOCOL_VERSION, "app": app, "pid": child.pid, "exe": "", "create_token": "", "create_kind": "", "cdp_port": 0, "cwd": str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": None, "command": []}
            write_state(partial, state_path)
            result["state_kept"] = True
        except (OSError, ValueError) as exc:
            result["state_write_error"] = str(exc)

    return result

def _replace_existing(app: str, existing: dict, requested_exe: str, state_path: Path, grace: float) -> None:
    recorded_exe = str(existing.get("exe") or "")

    if (not recorded_exe) or (not same_image_path(recorded_exe, requested_exe)):
        raise cdp.cdp_error("replace-exe-mismatch", "the live app-drive instance for " + repr(app) + " runs " + repr(recorded_exe) + ", but --replace was asked to launch " + repr(requested_exe) + "; refusing to kill it (run app-drive close --app " + app + " yourself if that is really the instance you mean)")

    pid = int(existing.get("pid") or 0)
    terminate_verified(existing, pid, grace)
    remove_state(state_path)
    release_ports(app)

def _build_launch_state(app: str, child_pid: int, executable: str, identity: dict, port: int, cwd: Optional[str], log_dir: Optional[Path], command: list[str]) -> dict:
    return {"schema": STATE_SCHEMA, "protocol": PROTOCOL_VERSION, "app": app, "pid": child_pid, "exe": executable, "create_token": str(identity.get("create_token") or ""), "create_kind": str(identity.get("create_kind") or ""), "cdp_port": int(port), "cwd": cwd or str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": str(log_dir.resolve()) if log_dir is not None else None, "command": command}

def cmd_launch(args) -> int:
    if (args.wait_timeout <= 0) or (args.grace < 0):
        raise cdp.cdp_error("bad-timeout", "--wait-timeout must be positive and --grace must not be negative")

    executable = resolve_executable(args.exe)
    app = str(args.app or default_app_key(executable))
    state_path = state_file_path(app, args.state_file)
    existing = read_state(app, args.state_file)

    if (existing is not None) and pid_alive(existing.get("pid")):
        if not args.replace:
            raise cdp.cdp_error("already-running", "app " + repr(app) + " was already launched (pid " + str(existing.get("pid")) + ", CDP port " + str(existing.get("cdp_port")) + "); run app-drive close --app " + app + " or pass --replace")

        _replace_existing(app, existing, executable, state_path, args.grace)

    port = int(args.port) if args.port is not None else acquire_cdp_port(app)
    environment = build_launch_env(os.environ, port, args.env)
    command = build_launch_command(executable, args.extra)
    log_dir, stdout_target, stderr_target = _open_launch_logs(app, args.log_dir, args.no_log)
    child: Optional[subprocess.Popen] = None
    state: Optional[dict] = None
    started = time.monotonic()

    try:
        try:
            child = _spawn(command, environment, args.cwd, stdout_target, stderr_target)
        finally:
            _close_handles(stdout_target, stderr_target)

        identity = read_process_identity(child.pid)

        if identity is None:
            exit_code = child.poll()
            suffix = (" (app exit code " + str(exit_code) + ")") if exit_code is not None else ""
            raise cdp.cdp_error("identity-unavailable", "cannot read the creation time and full path of the spawned process (pid " + str(child.pid) + ")" + suffix + "; refusing to manage a process it cannot verify (fail closed)")

        state = _build_launch_state(app, child.pid, executable, identity, port, args.cwd, log_dir, command)
        write_state(state, state_path)
        claim_cdp_port(app, port, child.pid)
    except KeyboardInterrupt:
        cleanup = _abort_launch(child, state_path, app, state, args.grace)
        emit(failure("launch", "interrupted", "launch interrupted" + _cleanup_note(cleanup), cleanup = cleanup), args.pretty)
        return 130
    except cdp.cdp_error as exc:
        cleanup = _abort_launch(child, state_path, app, state, args.grace)
        details = dict(exc.details)
        details.update({"cleanup": cleanup, "app": app, "pid": (child.pid if child is not None else None), "state_file": str(state_path.resolve())})
        raise cdp.cdp_error(exc.code, str(exc) + _cleanup_note(cleanup), details) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        cleanup = _abort_launch(child, state_path, app, state, args.grace)
        raise cdp.cdp_error("launch-failed", str(exc) + _cleanup_note(cleanup), {"cleanup": cleanup, "app": app, "pid": (child.pid if child is not None else None), "state_file": str(state_path.resolve())}) from exc

    launch_ms = int((time.monotonic() - started) * 1000)

    if args.no_wait:
        emit(envelope("launch", app = app, pid = child.pid, cdp_port = port, page = None, waited = False, launch_ms = launch_ms, state_file = str(state_path.resolve()), log_dir = state["log_dir"], exe = executable), args.pretty)
        return 0

    try:
        page, cdp_ms = cdp.wait_for_page(port, timeout = args.wait_timeout, prefer_url = args.prefer_url, target_id = args.target or "")
    except KeyboardInterrupt:
        cleanup = _abort_launch(child, state_path, app, state, args.grace)
        emit(failure("launch", "interrupted", "launch interrupted while waiting for CDP" + _cleanup_note(cleanup), cleanup = cleanup), args.pretty)
        return 130
    except cdp.cdp_error as exc:
        exit_code = child.poll()
        cleanup = _abort_launch(child, state_path, app, state, args.grace)
        suffix = (" (app exit code " + str(exit_code) + ")") if exit_code is not None else ""
        details = dict(exc.details)
        details.update({"cleanup": cleanup, "app": app, "pid": child.pid, "state_file": str(state_path.resolve())})
        raise cdp.cdp_error(exc.code, str(exc) + suffix + _cleanup_note(cleanup), details) from exc

    emit(envelope("launch", app = app, pid = child.pid, cdp_port = port, page = public_target(page), waited = True, launch_ms = launch_ms, cdp_ms = cdp_ms, state_file = str(state_path.resolve()), log_dir = state["log_dir"], exe = executable), args.pretty)
    return 0

def _close_app_key(args) -> Optional[str]:
    if args.app:
        return str(args.app)

    if args.exe:
        return default_app_key(resolve_executable(args.exe))

    return None

def cmd_close(args) -> int:
    if args.grace < 0:
        raise cdp.cdp_error("bad-timeout", "--grace must not be negative")

    if args.pid is not None:
        if not args.force:
            raise cdp.cdp_error("unsafe-close", "closing an arbitrary pid needs --force; prefer --app/--exe so app-drive only closes the process it launched and can verify")

        result = terminate_tree(int(args.pid), args.grace)
        payload = {"app": None, "pid": int(args.pid), "verified": False}
        payload.update(result)
        emit(envelope("close", **payload, state_file_removed = False, ports_released = False), args.pretty)
        return 0

    app = _close_app_key(args)
    state: Optional[dict] = None

    if (not app) and args.state_file:
        state = read_state(None, args.state_file)

        if state is not None:
            app = str(state.get("app") or "")

    if not app:
        raise cdp.cdp_error("no-app", "no application key: pass --app, --exe (the launched executable), or --state-file")

    state_path = state_file_path(app, args.state_file)

    if state is None:
        state = read_state(app, args.state_file)

    if state is None:
        raise cdp.cdp_error("no-state", "no launch state for app " + repr(app) + " at " + str(state_path) + "; app-drive only closes apps it launched (break-glass: --pid N --force)")

    recorded_pid = state.get("pid")

    if not recorded_pid:
        raise cdp.cdp_error("bad-state", "the launch state " + str(state_path) + " carries no pid")

    pid = int(recorded_pid)

    if not pid_alive(pid):
        removed = remove_state(state_path)
        released = release_ports(app)
        emit(envelope("close", app = app, pid = pid, closed = True, forced = False, waited_ms = 0, reason = "not-running", verified = True, state_file_removed = removed, ports_released = released), args.pretty)
        return 0

    try:
        result = terminate_verified(state, pid, args.grace)
    except cdp.cdp_error as exc:
        details = dict(exc.details)
        details.update({"app": app, "pid": pid, "state_file": str(state_path.resolve())})
        raise cdp.cdp_error(exc.code, str(exc) + "; the launch state and port claim were kept so this process stays retryable (pid " + str(pid) + ")", details) from exc

    removed = remove_state(state_path)
    released = release_ports(app)
    payload = {"app": app, "pid": pid, "verified": True}
    payload.update(result)
    emit(envelope("close", **payload, state_file_removed = removed, ports_released = released), args.pretty)
    return 0

HANDLERS = {"targets": cmd_targets, "status": cmd_status, "eval": cmd_eval, "shot": cmd_shot, "click": cmd_click, "wait": cmd_wait, "watch": cmd_watch, "launch": cmd_launch, "close": cmd_close}

def _add_attach_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", default = None, help = "application key from app-drive launch (reads its state file; attach never allocates a port)")
    parser.add_argument("--state-file", default = None, help = "override the launch state file")
    parser.add_argument("--port", type = int, default = None, help = "CDP port to attach to (overrides --app/--state-file/CELESTIA_CDP_PORT)")
    parser.add_argument("--prefer-url", default = "", help = "prefer the page target whose URL starts with this prefix")
    parser.add_argument("--target", default = None, help = "CDP target id to drive (default: the app page target)")
    parser.add_argument("--timeout", type = float, default = DEFAULT_TIMEOUT, metavar = "SECONDS", help = "connect/response budget; for wait it is also the condition deadline (default 15)")
    parser.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "app-drive", description = "Drive a Tauri desktop debug build over the WebView2 CDP channel (CDP only; no vite allocation).", epilog = PROTOCOL_EPILOG, formatter_class = argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest = "command", required = True, metavar = "COMMAND")
    targets = subparsers.add_parser("targets", help = "list the CDP targets (/json/list)")
    _add_attach_arguments(targets)
    status = subparsers.add_parser("status", help = "selected target + boot-chrome/page state as JSON")
    _add_attach_arguments(status)
    eval_parser = subparsers.add_parser("eval", help = "evaluate JavaScript and print the JSON value")
    _add_attach_arguments(eval_parser)
    eval_parser.add_argument("expression", nargs = "*", help = "JavaScript expression (awaitPromise/returnByValue are on by default)")
    eval_parser.add_argument("--file", default = None, help = "read the expression from a file instead")
    eval_parser.add_argument("--no-await", action = "store_true", help = "do not await a returned promise")
    eval_parser.add_argument("--no-gesture", action = "store_true", help = "do not mark the evaluation as user-gesture")
    eval_parser.add_argument("--out", default = None, help = "write the ASCII JSON value to this file")
    eval_parser.add_argument("--raw", action = "store_true", help = "print only the bare ASCII JSON value instead of the envelope")
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
    launch.add_argument("--app", default = None, help = "application key for the state/port keys (default: a hash of the resolved exe path)")
    launch.add_argument("--cwd", default = None, help = "working directory for the app process")
    launch.add_argument("--port", type = int, default = None, help = "force a specific CDP port instead of allocating one")
    launch.add_argument("--prefer-url", default = "", help = "prefer the page target whose URL starts with this prefix")
    launch.add_argument("--target", default = None, help = "wait for this CDP target id instead of the first page target")
    launch.add_argument("--wait-timeout", type = float, default = DEFAULT_LAUNCH_TIMEOUT, metavar = "SECONDS", help = "how long to wait for the CDP page target (default 30)")
    launch.add_argument("--no-wait", action = "store_true", help = "return right after spawning, do not wait for CDP")
    launch.add_argument("--replace", action = "store_true", help = "replace the live app-drive instance of this app (verifies its identity and exe first)")
    launch.add_argument("--env", action = "append", default = [], metavar = "KEY=VALUE", help = "extra environment variable for the app (repeatable; use WEBVIEW2_USER_DATA_FOLDER for a second instance of a WebView2 app)")
    launch.add_argument("--log-dir", default = None, help = "directory for stdout.log/stderr.log (default: state-dir/logs/<app>-<stamp>)")
    launch.add_argument("--no-log", action = "store_true", help = "discard the app stdout/stderr instead of writing log files")
    launch.add_argument("--state-file", default = None, help = "override the launch state file")
    launch.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-termination budget before a forced kill (default 5)")
    launch.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")
    launch.add_argument("extra", nargs = "*", help = "extra app arguments (pass them after --)")
    close = subparsers.add_parser("close", help = "close the app instance that app-drive launched (identity-verified)")
    close.add_argument("--app", default = None, help = "application key from app-drive launch")
    close.add_argument("--exe", default = None, help = "derive the application key from this executable path")
    close.add_argument("--state-file", default = None, help = "override the launch state file")
    close.add_argument("--pid", type = int, default = None, help = "break-glass: close this pid without verification (requires --force)")
    close.add_argument("--force", action = "store_true", help = "required with --pid; explicitly accepts an unverified kill")
    close.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-termination budget before a forced kill (default 5)")
    close.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")
    return parser

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = list(argv if argv is not None else sys.argv[1:])
    parsed = parser.parse_args(arguments)
    pretty = bool(getattr(parsed, "pretty", False))
    handler = HANDLERS.get(parsed.command)

    if handler is None:
        print("error: unknown command " + repr(parsed.command), file = sys.stderr)
        return 2

    try:
        return int(handler(parsed) or 0)
    except KeyboardInterrupt:
        emit(failure(parsed.command, "interrupted", "interrupted; a launched app keeps its state file and port claim so app-drive close can finish the job"), pretty)
        return 130
    except cdp.cdp_error as exc:
        emit(failure(parsed.command, exc.code, str(exc), **exc.details), pretty)
        return 1
    except SystemExit as exc:
        emit(failure(parsed.command, "error", str(exc) or "aborted"), pretty)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        emit(failure(parsed.command, "error", type(exc).__name__ + ": " + str(exc)), pretty)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
