"""Constants, protocol envelope and the state store for app-drive.

Split out of app_drive.py (2026-10-31); owns the wire envelope helpers,
the on-disk state file schema accessors and the module-level constants
the other split modules share.
"""

from __future__ import annotations
import hashlib
import json
import os
import time
from pathlib import Path
from celestia_devtools.core import envelope as core_envelope
from celestia_devtools.env import cdp

PROTOCOL_VERSION = 2
STATE_SCHEMA = 2
CDP_ENV_VAR = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
CDP_PORT_ENV = "CELESTIA_CDP_PORT"
RESERVED_ENV_KEYS = (CDP_ENV_VAR, CDP_PORT_ENV)
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
_TASKKILL_PATH: str | None = None
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

terminating commands:
  close and --replace are the only USER-FACING subcommands that terminate a
  process; launch's failure cleanup may also terminate the child it just
  spawned, but only through the identity it recorded (an already-exited child
  is never signalled, and a child with no recorded identity is never
  blind-killed). close re-checks the identity before the graceful signal and
  again after the graceful attempt times out, immediately before any forced
  signal. A direct
  Popen child is observed through wait()/poll() so POSIX zombies are reaped
  rather than mistaken for running processes. Ctrl+C before the spawn leaves
  nothing to clean; after it, the same cleanup path runs and the envelope is
  printed.

safe-close contract:
  launch records the resolved exe path + process creation token + token kind
  in <state-dir>/<app>.json (schema 2). close and --replace require all three
  to match the live process, re-check immediately before the kill, and fail
  closed (state kept, exit 1) when anything is missing, unreadable or
  different. A schema-1 state file is refused (state-outdated); recover the
  handle with close --discard-state (or --state-file ... --discard-state) and
  pass --discard-state to launch instead of refusing to start; the old process
  then has to be closed manually. Discarding a CURRENT-schema handle also
  needs --force. When the recorded pid cannot be confirmed alive (exit status
  unavailable AND identity unreadable, e.g. access-denied), close fails
  closed, keeps the state, and reports the probe reason (access-denied vs
  no-such-process); --force is the explicit unverified break-glass (--pid N
  --force kills by pid; --app <key> --force kills the recorded pid without
  verification, or drops an unconfirmable state). An app that exited normally
  also leaves its state behind: close reports process-unconfirmed and needs
  --force before the handle is removed. Windows path note: the recorded path comes from Python resolve() while the
  live path comes from QueryFullProcessImageNameW, so a symlinked or
  8.3-short launch path can differ textually; comparison also tries realpath
  and still fails closed when equality cannot be proven (relaunch through the
  canonical path).

scope:
  CDP only: launch allocates/claims the CDP port and sets
  WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS; no vite port is allocated, claimed
  or exported (a compiled debug build keeps its baked devUrl). --env refuses
  to override WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS / CELESTIA_CDP_PORT
  (case-insensitive). Attach commands never allocate or claim a port.
  launch app arguments: pass them after a bare -- separator, e.g.
    app-drive launch target/debug/my_app -- --window second

state directory (override with CELESTIA_APP_DRIVE_STATE):
  Windows  %LOCALAPPDATA%/celestia/app-drive
  POSIX    $XDG_STATE_HOME/celestia/app-drive (or ~/.local/state/...)
"""

def envelope(command: str, **fields) -> dict:
    return core_envelope.ok_envelope(command, PROTOCOL_VERSION, **fields)

def failure(command: str, code: str, message: str, **fields) -> dict:
    return core_envelope.error_envelope(command, PROTOCOL_VERSION, code, message, **fields)

def emit(payload: dict, pretty: bool = False) -> None:
    """Print the signed envelope as pure ASCII (gbk stdout cannot crash)."""
    core_envelope.emit_line(payload, pretty = pretty)

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

def state_file_path(app: str | None = None, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)

    if not app:
        raise cdp.cdp_error("no-app", "no application key: pass --app or --state-file")

    return state_root() / (app_slug(str(app)) + ".json")

def read_state(app: str | None = None, explicit: str | None = None, allow_outdated: bool = False) -> dict | None:
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

    if (not allow_outdated) and (schema != STATE_SCHEMA):
        recovery = ("app-drive close --state-file " + str(path) + " --discard-state") if explicit else ("app-drive close --app " + str(app or "<key>") + " --discard-state")

        raise cdp.cdp_error("state-outdated", "the launch state " + str(path) + " was written by an older app-drive (schema " + str(schema) + ", expected " + str(STATE_SCHEMA) + "); it carries no process creation token, so app-drive will not touch that process (fail closed). Recover the stale handle with " + recovery + ", or run launch --discard-state for the same app; the old process then has to be closed manually.")

    if (app is not None) and (str(data.get("app") or "") != str(app)):
        raise cdp.cdp_error("state-app-mismatch", "the launch state " + str(path) + " belongs to app " + repr(data.get("app")) + ", not " + repr(str(app)) + "; refusing to use it (state-file slug collision?)")

    return data

def write_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(json.dumps(state, indent = 2, ensure_ascii = True), encoding = "utf-8")
    os.replace(temporary, path)

def remove_state(path: Path | None) -> bool:
    if path is None:
        return False

    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False

def state_cdp_port(state: dict, path: Path | None = None) -> int:
    port = state.get("cdp_port")

    if (not isinstance(port, int)) or (port < 1) or (port > 65535):
        raise cdp.cdp_error("bad-state", "the launch state " + str(path) + " carries no usable cdp_port")

    return int(port)
