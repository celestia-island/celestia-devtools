"""Commands, launch orchestration and the parser for app-drive.

Split out of app_drive.py (2026-10-31); the public entry point stays
celestia_devtools.env.app_drive.main (re-exported by the facade).
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence
from celestia_devtools.env import cdp
from celestia_devtools.env.app_drive_proc import (build_launch_command, build_launch_env, identity_probe_reason, parse_env_overrides, pid_alive, read_process_identity, resolve_executable, same_image_path, terminate_tree, terminate_verified)
from celestia_devtools.env.app_drive_cdp import (acquire_cdp_port, check_condition, claim_cdp_port, condition_description, connect_page, element_click_expression, element_probe_expression, image_dimensions, launched_summary, public_target, read_page_state, release_ports, resolve_attach_port, state_changes, state_signature, watch_stop_reason)
from celestia_devtools.env.app_drive_state import (DEFAULT_CLOSE_GRACE, DEFAULT_LAUNCH_TIMEOUT, DEFAULT_TIMEOUT, DEFAULT_WAIT_INTERVAL, DEFAULT_WATCH_DURATION, DEFAULT_WATCH_INTERVAL, PROTOCOL_EPILOG, PROTOCOL_VERSION, STATE_SCHEMA, app_slug, default_app_key, emit, envelope, failure, read_state, remove_state, state_file_path, state_root, timestamp_utc, write_state)

def cmd_targets(args) -> int:
    """List every CDP target; a list command must not require a page target."""
    port = resolve_attach_port(args)
    targets = cdp.list_targets(port, timeout = args.timeout)
    page = cdp.select_page_target(targets, getattr(args, "prefer_url", "") or "", getattr(args, "target", None) or "")
    emit(envelope("targets", port = port, count = len(targets), targets = [public_target(item) for item in targets], selected_target_id = (page or {}).get("id")), args.pretty)
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
                error_text: str | None = None

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

def _validate_log_dir(log_dir: str | None) -> Path | None:
    """Validate an EXPLICIT --log-dir before a port is acquired (no leaked claim).

    The timestamped default directory is created only after a port has been
    acquired, so a failed launch cannot leave empty log directories behind.
    """

    if not log_dir:
        return None

    directory = Path(log_dir)

    try:
        directory.mkdir(parents = True, exist_ok = True)
    except OSError as exc:
        raise cdp.cdp_error("log-dir-failed", "cannot create the launch log directory " + str(directory) + ": " + str(exc)) from exc

    if not directory.is_dir():
        raise cdp.cdp_error("log-dir-failed", "the launch log path is not a directory: " + str(directory))

    return directory

def _new_default_log_dir(app: str) -> Path:
    directory = state_root() / "logs" / (app_slug(app) + "-" + time.strftime("%Y%m%d-%H%M%S"))

    try:
        directory.mkdir(parents = True, exist_ok = True)
    except OSError as exc:
        raise cdp.cdp_error("log-dir-failed", "cannot create the launch log directory " + str(directory) + ": " + str(exc)) from exc

    return directory

def _open_launch_logs(directory: Path | None) -> tuple[object, object]:
    if directory is None:
        return subprocess.DEVNULL, subprocess.DEVNULL

    stdout_handle = None

    try:
        stdout_handle = open(directory / "stdout.log", "wb")
        stderr_handle = open(directory / "stderr.log", "wb")
    except OSError as exc:
        _close_handles(stdout_handle)
        raise cdp.cdp_error("log-dir-failed", "cannot open the launch log files in " + str(directory) + ": " + str(exc)) from exc

    return stdout_handle, stderr_handle

def _close_handles(*handles) -> None:
    for handle in handles:
        closing = getattr(handle, "close", None)

        if callable(closing):
            try:
                closing()
            except (OSError, ValueError):
                pass

def _spawn(command: list[str], environment: dict[str, str], cwd: str | None, stdout_target, stderr_target) -> subprocess.Popen:
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

    if cleanup.get("already_exited"):
        return "; the spawned app had already exited (exit code " + str(cleanup.get("exit_code")) + "), no kill was sent"

    if cleanup.get("state_kept"):
        app = str(cleanup.get("app"))
        state_file = str(cleanup.get("state_file"))

        if cleanup.get("state_verified"):
            return "; WARNING: the spawned app could not be terminated - state kept at " + state_file + " (retry with app-drive close --app " + app + "; pid " + str(cleanup.get("pid")) + ")"

        return "; WARNING: the spawned app could not be terminated and its identity is unrecorded - state kept at " + state_file + "; finish explicitly with app-drive close --app " + app + " --force (unverified kill)"

    if cleanup.get("blind_kill_refused"):
        return "; the spawned app was NOT killed because no recorded identity could be verified (a blind pid kill could hit a reused pid); pid " + str(cleanup.get("pid")) + " was left for a manual check"

    return "; cleanup of the spawned app is incomplete (pid " + str(cleanup.get("pid")) + ")"

def _launch_state_from_identity(app: str, pid: int, identity: dict | None, port: int | None) -> dict:
    if identity is None:
        return {"schema": STATE_SCHEMA, "protocol": PROTOCOL_VERSION, "app": app, "pid": int(pid), "exe": "", "create_token": "", "create_kind": "", "cdp_port": int(port or 0), "cwd": str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": None, "command": [], "unverified": True}

    return {"schema": STATE_SCHEMA, "protocol": PROTOCOL_VERSION, "app": app, "pid": int(pid), "exe": str(identity.get("image") or ""), "create_token": str(identity.get("create_token") or ""), "create_kind": str(identity.get("create_kind") or ""), "cdp_port": int(port or 0), "cwd": str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": None, "command": [], "unverified": False}

def _state_has_identity(state: dict | None) -> bool:
    if not state:
        return False

    return bool(state.get("exe")) and bool(state.get("create_token")) and bool(state.get("create_kind"))

def _abort_launch(child: subprocess.Popen | None, state_path: Path, app: str, state: dict | None, grace: float, port: int | None = None) -> dict:
    """Clean up a failed launch WITHOUT ever blind-killing a bare pid.

    - already-exited child (poll() returned): skip the kill entirely;
    - recorded identity present: terminate through terminate_verified;
    - no identity recorded: refuse to kill a bare pid (it may have been
      reused); recover the identity now if possible and keep a closable state.
    """
    result: dict = {"pid": None, "terminated": False, "already_exited": False, "state_kept": False, "claims_released": False, "blind_kill_refused": False, "state_verified": False, "app": app, "state_file": str(state_path.resolve())}

    if child is None:
        result["state_removed"] = remove_state(state_path)
        result["claims_released"] = release_ports(app)
        return result

    result["pid"] = child.pid
    exit_code = child.poll()

    if exit_code is not None:
        result["already_exited"] = True
        result["exit_code"] = exit_code
        result["state_removed"] = remove_state(state_path)
        result["claims_released"] = release_ports(app)
        return result

    if _state_has_identity(state):
        try:
            terminate_verified(state, child.pid, grace, child = child)
            result["terminated"] = True
            result["state_verified"] = True
        except cdp.cdp_error as exc:
            result["terminate_error"] = str(exc)
    else:
        result["blind_kill_refused"] = True

    if result["terminated"]:
        result["state_removed"] = remove_state(state_path)
        result["claims_released"] = release_ports(app)
        return result

    keep = state

    if keep is None:
        identity = read_process_identity(child.pid)

        if child.poll() is not None:
            # Exited while we probed: never inspect/keep a possibly reused pid.
            result["already_exited"] = True
            result["exit_code"] = child.returncode
            result["state_removed"] = remove_state(state_path)
            result["claims_released"] = release_ports(app)
            return result

        keep = _launch_state_from_identity(app, child.pid, identity, port)
        result["state_verified"] = identity is not None
    else:
        result["state_verified"] = _state_has_identity(keep)

    try:
        write_state(keep, state_path)
        result["state_kept"] = True
    except (OSError, ValueError) as exc:
        result["state_write_error"] = str(exc)

    if port:
        try:
            claim_cdp_port(app, int(port), int(keep["pid"]))
        except cdp.cdp_error as exc:
            result["claim_error"] = str(exc)

    return result

def _replace_existing(app: str, existing: dict, requested_exe: str, state_path: Path, grace: float) -> None:
    recorded_exe = str(existing.get("exe") or "")

    if (not recorded_exe) or (not same_image_path(recorded_exe, requested_exe)):
        raise cdp.cdp_error("replace-exe-mismatch", "the live app-drive instance for " + repr(app) + " runs " + repr(recorded_exe) + ", but --replace was asked to launch " + repr(requested_exe) + "; refusing to kill it (run app-drive close --app " + app + " yourself if that is really the instance you mean)")

    pid = int(existing.get("pid") or 0)
    terminate_verified(existing, pid, grace)
    remove_state(state_path)
    release_ports(app)

def _build_launch_state(app: str, child_pid: int, executable: str, identity: dict, port: int, cwd: str | None, log_dir: Path | None, command: list[str]) -> dict:
    return {"schema": STATE_SCHEMA, "protocol": PROTOCOL_VERSION, "app": app, "pid": child_pid, "exe": executable, "create_token": str(identity.get("create_token") or ""), "create_kind": str(identity.get("create_kind") or ""), "cdp_port": int(port), "cwd": cwd or str(Path.cwd()), "launched_at": timestamp_utc(), "log_dir": str(log_dir.resolve()) if log_dir is not None else None, "command": command}

def _validate_launch_options(args) -> None:
    if (args.wait_timeout <= 0) or (args.grace < 0):
        raise cdp.cdp_error("bad-timeout", "--wait-timeout must be positive and --grace must not be negative")

    if args.port is not None:
        forced_port = int(args.port)

        if (forced_port < 1) or (forced_port > 65535):
            raise cdp.cdp_error("bad-port", "CDP port out of range: " + str(forced_port))

def cmd_launch(args) -> int:
    _validate_launch_options(args)
    # Validate --env before anything is created or acquired: a bad value must
    # not leave a CLI-owned port claim behind.
    parse_env_overrides(args.env)
    executable = resolve_executable(args.exe)
    app = str(args.app or default_app_key(executable))
    state_path = state_file_path(app, args.state_file)
    discarded_state = False

    try:
        existing = read_state(app, args.state_file)
    except cdp.cdp_error as exc:
        if (exc.code != "state-outdated") or (not args.discard_state):
            raise

        read_state(app, args.state_file, allow_outdated = True)
        remove_state(state_path)
        release_ports(app)
        existing = None
        discarded_state = True

    if (existing is not None) and pid_alive(existing.get("pid")):
        if not args.replace:
            raise cdp.cdp_error("already-running", "app " + repr(app) + " was already launched (pid " + str(existing.get("pid")) + ", CDP port " + str(existing.get("cdp_port")) + "); run app-drive close --app " + app + " or pass --replace")

        _replace_existing(app, existing, executable, state_path, args.grace)

    explicit_log_dir = _validate_log_dir(args.log_dir)
    port = int(args.port) if args.port is not None else acquire_cdp_port(app)

    try:
        # The timestamped default directory is created only now, so a failed
        # acquisition leaves no empty log directory behind.
        log_directory = None if args.no_log else (explicit_log_dir if explicit_log_dir is not None else _new_default_log_dir(app))
        environment = build_launch_env(os.environ, port, args.env)
        command = build_launch_command(executable, args.extra)
        stdout_target, stderr_target = _open_launch_logs(log_directory)
    except cdp.cdp_error:
        release_ports(app)
        raise

    child: subprocess.Popen | None = None
    state: dict | None = None
    started = time.monotonic()

    try:
        try:
            child = _spawn(command, environment, args.cwd, stdout_target, stderr_target)
        finally:
            _close_handles(stdout_target, stderr_target)

        exit_code = child.poll()

        if exit_code is not None:
            raise cdp.cdp_error("launch-failed", "the app exited before app-drive could record its identity (exit code " + str(exit_code) + ")")

        identity = read_process_identity(child.pid)
        exit_after_probe = child.poll()

        if exit_after_probe is not None:
            raise cdp.cdp_error("launch-failed", "the app exited while app-drive was reading its identity (exit code " + str(exit_after_probe) + ")")

        if identity is None:
            raise cdp.cdp_error("identity-unavailable", "cannot read the creation time and full path of the spawned process (pid " + str(child.pid) + "); refusing to manage a process it cannot verify (fail closed)")

        state = _build_launch_state(app, child.pid, executable, identity, port, args.cwd, log_directory, command)
        write_state(state, state_path)
        claim_cdp_port(app, port, child.pid)
    except KeyboardInterrupt:
        cleanup = _abort_launch(child, state_path, app, state, args.grace, port)
        emit(failure("launch", "interrupted", "launch interrupted" + _cleanup_note(cleanup), cleanup = cleanup), args.pretty)
        return 130
    except cdp.cdp_error as exc:
        cleanup = _abort_launch(child, state_path, app, state, args.grace, port)
        details = dict(exc.details)
        details.update({"cleanup": cleanup, "app": app, "pid": (child.pid if child is not None else None), "state_file": str(state_path.resolve()), "discarded_outdated_state": discarded_state})
        raise cdp.cdp_error(exc.code, str(exc) + _cleanup_note(cleanup), details) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        cleanup = _abort_launch(child, state_path, app, state, args.grace, port)
        raise cdp.cdp_error("launch-failed", str(exc) + _cleanup_note(cleanup), {"cleanup": cleanup, "app": app, "pid": (child.pid if child is not None else None), "state_file": str(state_path.resolve()), "discarded_outdated_state": discarded_state}) from exc

    launch_ms = int((time.monotonic() - started) * 1000)
    base_fields = {"app": app, "pid": child.pid, "cdp_port": port, "state_file": str(state_path.resolve()), "log_dir": state["log_dir"], "exe": executable, "discarded_outdated_state": discarded_state}

    if args.no_wait:
        emit(envelope("launch", **base_fields, page = None, waited = False, launch_ms = launch_ms), args.pretty)
        return 0

    try:
        page, cdp_ms = cdp.wait_for_page(port, timeout = args.wait_timeout, prefer_url = args.prefer_url, target_id = args.target or "")
    except KeyboardInterrupt:
        cleanup = _abort_launch(child, state_path, app, state, args.grace, port)
        emit(failure("launch", "interrupted", "launch interrupted while waiting for CDP" + _cleanup_note(cleanup), cleanup = cleanup), args.pretty)
        return 130
    except cdp.cdp_error as exc:
        cleanup = _abort_launch(child, state_path, app, state, args.grace, port)
        exit_code = child.poll()
        suffix = (" (app exit code " + str(exit_code) + ")") if exit_code is not None else ""
        details = dict(exc.details)
        details.update({"cleanup": cleanup, "app": app, "pid": child.pid, "state_file": str(state_path.resolve()), "discarded_outdated_state": discarded_state})
        raise cdp.cdp_error(exc.code, str(exc) + suffix + _cleanup_note(cleanup), details) from exc

    emit(envelope("launch", **base_fields, page = public_target(page), waited = True, launch_ms = launch_ms, cdp_ms = cdp_ms), args.pretty)
    return 0

def _close_app_key(args) -> str | None:
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
        payload = {"app": None, "pid": int(args.pid), "verified": False, "explicit_force": True, "discarded_state": False}
        payload.update(result)
        emit(envelope("close", **payload, state_file_removed = False, ports_released = False), args.pretty)
        return 0

    app = _close_app_key(args)
    state: dict | None = None

    try:
        state = read_state(app, args.state_file)
    except cdp.cdp_error as exc:
        if (exc.code != "state-outdated") or (not args.discard_state):
            raise

        raw = read_state(app, args.state_file, allow_outdated = True)

        if raw is None:
            # The file vanished between the two reads: report it, never crash.
            emit(envelope("close", app = app or None, pid = None, closed = False, verified = False, discarded_state = False, state_file_removed = False, ports_released = False, reason = "state-vanished"), args.pretty)
            return 0

        app = str(raw.get("app") or app or "")
        state_path = state_file_path(app or None, args.state_file)
        discarded = remove_state(state_path)
        released = release_ports(app)
        emit(envelope("close", app = app or None, pid = raw.get("pid"), closed = False, verified = False, discarded_state = True, state_file_removed = discarded, ports_released = released, reason = "outdated-state-discarded"), args.pretty)
        return 0

    if (not app) and (state is not None):
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

    if args.discard_state:
        if not args.force:
            raise cdp.cdp_error("discard-needs-force", "refusing to discard a current-schema launch state without --force: it records the live process identity (pid " + str(pid) + "). Use close --force --discard-state only if you accept dropping the handle without touching the process")

        removed = remove_state(state_path)
        released = release_ports(app)
        emit(envelope("close", app = app, pid = pid, closed = False, verified = False, discarded_state = True, state_file_removed = removed, ports_released = released, reason = "state-discarded"), args.pretty)
        return 0

    if args.force:
        # Explicit break-glass: never verify, but still never blind-kill a pid we
        # cannot even observe (when pids are unobservable we only drop the state).
        unconfirmable = (not pid_alive(pid)) and (read_process_identity(pid) is None)

        if unconfirmable:
            result = {"closed": False, "forced": False, "waited_ms": 0, "reason": "state-discarded-unconfirmed"}
        else:
            result = terminate_tree(pid, args.grace)

        removed = remove_state(state_path)
        released = release_ports(app)
        payload = {"app": app, "pid": pid, "verified": False, "explicit_force": True, "discarded_state": False}
        payload.update(result)
        emit(envelope("close", **payload, state_file_removed = removed, ports_released = released), args.pretty)
        return 0

    if not pid_alive(pid):
        # pid_alive() returns False for access-denied live processes too: only
        # drop the state when the process is gone BEYOND doubt (no identity).
        # A normally-exited app lands here as well (state left behind): that is
        # intentional - app-drive only drops a state through --force.
        if read_process_identity(pid) is None:
            reason = identity_probe_reason(pid)
            raise cdp.cdp_error("process-unconfirmed", "cannot confirm whether pid " + str(pid) + " is still running (probe: " + reason + "). Refusing to drop the launch state (fail closed); pass --force to discard the handle or to kill the recorded pid explicitly, or close the app manually. Note: an app that exited normally also needs --force before its leftover state is removed", {"app": app, "pid": pid, "state_file": str(state_path.resolve()), "probe": reason})

    try:
        result = terminate_verified(state, pid, args.grace)
    except cdp.cdp_error as exc:
        details = dict(exc.details)
        details.update({"app": app, "pid": pid, "state_file": str(state_path.resolve())})
        raise cdp.cdp_error(exc.code, str(exc) + "; the launch state and port claim were kept so this process stays retryable (pid " + str(pid) + "; --force is the explicit unverified path)", details) from exc

    removed = remove_state(state_path)
    released = release_ports(app)
    payload = {"app": app, "pid": pid, "verified": True, "explicit_force": False, "discarded_state": False}
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
    launch.add_argument("exe", help = "path to the desktop debug executable (extra app arguments go after a bare -- separator)")
    launch.add_argument("--app", default = None, help = "application key for the state/port keys (default: a hash of the resolved exe path)")
    launch.add_argument("--cwd", default = None, help = "working directory for the app process")
    launch.add_argument("--port", type = int, default = None, help = "force a specific CDP port instead of allocating one")
    launch.add_argument("--prefer-url", default = "", help = "prefer the page target whose URL starts with this prefix")
    launch.add_argument("--target", default = None, help = "wait for this CDP target id instead of the first page target")
    launch.add_argument("--wait-timeout", type = float, default = DEFAULT_LAUNCH_TIMEOUT, metavar = "SECONDS", help = "how long to wait for the CDP page target (default 30)")
    launch.add_argument("--no-wait", action = "store_true", help = "return right after spawning, do not wait for CDP")
    launch.add_argument("--replace", action = "store_true", help = "replace the live app-drive instance of this app (verifies its identity and exe first)")
    launch.add_argument("--discard-state", action = "store_true", help = "drop a schema-1 state file left by an older app-drive instead of refusing to launch")
    launch.add_argument("--env", action = "append", default = [], metavar = "KEY=VALUE", help = "extra environment variable for the app (repeatable; use WEBVIEW2_USER_DATA_FOLDER for a second instance of a WebView2 app)")
    launch.add_argument("--log-dir", default = None, help = "directory for stdout.log/stderr.log (default: state-dir/logs/<app>-<stamp>)")
    launch.add_argument("--no-log", action = "store_true", help = "discard the app stdout/stderr instead of writing log files")
    launch.add_argument("--state-file", default = None, help = "override the launch state file")
    launch.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-termination budget before a forced kill (default 5)")
    launch.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")
    close = subparsers.add_parser("close", help = "close the app instance that app-drive launched (identity-verified)")
    close.add_argument("--app", default = None, help = "application key from app-drive launch")
    close.add_argument("--exe", default = None, help = "derive the application key from this executable path")
    close.add_argument("--state-file", default = None, help = "override the launch state file")
    close.add_argument("--pid", type = int, default = None, help = "break-glass: signal this pid without verification (through its process group when it leads one, otherwise the pid itself); requires --force")
    close.add_argument("--force", action = "store_true", help = "explicit break-glass: kill the recorded pid without identity verification (also required with --pid, and to drop an unconfirmable state)")
    close.add_argument("--discard-state", action = "store_true", help = "drop the state file + CDP claim without touching the process (recovery for schema-1/outdated state)")
    close.add_argument("--grace", type = float, default = DEFAULT_CLOSE_GRACE, metavar = "SECONDS", help = "graceful-termination budget before a forced kill (default 5)")
    close.add_argument("--pretty", action = "store_true", help = "indent the JSON envelope")
    return parser

def split_launch_arguments(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split launch's trailing app arguments at the first bare -- separator.

    argparse's handling of "--" with a trailing nargs="*" positional differs
    between supported Python versions (3.12 rejects the segment that 3.13
    accepts), so the separator is consumed here instead of relying on it.
    """
    tokens = list(argv)

    if tokens and (tokens[0] == "launch") and ("--" in tokens):
        separator = tokens.index("--")
        return tokens[:separator], tokens[separator + 1:]

    return tokens, []

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv if argv is not None else sys.argv[1:])
    arguments, extra_arguments = split_launch_arguments(arguments)
    parsed = parser.parse_args(arguments)

    if extra_arguments and (parsed.command != "launch"):
        parser.error("-- app arguments are only supported by launch")

    parsed.extra = list(extra_arguments)
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
