"""CDP ports, targets and probe helpers for app-drive.

Owns port acquire/claim/release, attach-port resolution, target
selection, element/condition expressions and the watch-condition
evaluators. Split out of app_drive.py (2026-10-31).
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from celestia_devtools.env import cdp
from celestia_devtools.env import ports
from celestia_devtools.env.app_drive_proc import pid_alive, state_port_for_attach
from celestia_devtools.env.app_drive_state import (CDP_PORT_ENV, STATE_EXPRESSION, WATCH_FIELDS, read_state, state_file_path)

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

def release_ports(app: str | None) -> bool:
    """Release the CDP claim only (app-drive owns no other port kind)."""

    if not app:
        return False

    try:
        ports.release(str(app), "cdp")
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

def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
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

def watch_stop_reason(args, client: cdp.cdp_client, state: dict) -> str | None:
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

def launched_summary(app: str | None, state_file: str | None = None) -> dict | None:
    if (not app) and (not state_file):
        return None

    state = read_state(str(app) if app else None, state_file)

    if state is None:
        return None

    return {"app": state.get("app", app), "pid": state.get("pid"), "running": pid_alive(state.get("pid")), "cdp_port": state.get("cdp_port"), "exe": state.get("exe"), "state_file": str(state_file_path(str(app) if app else None, state_file)), "log_dir": state.get("log_dir")}
