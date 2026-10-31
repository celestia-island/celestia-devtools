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
CLOSED and refuse to touch the process. The token is re-checked before the
graceful signal and AGAIN after the graceful attempt times out, immediately
before any forced signal. A state file written by an older schema (no token) is refused as
state-outdated; recover it with close --discard-state (or pass
--discard-state to launch). --force is the explicit unverified break-glass for
close: --pid N --force kills by pid, --app <key> --force kills the recorded
pid without verification or drops an unconfirmable handle, and
close --force --discard-state drops a current-schema handle without touching
the process. An app that exited normally leaves its state behind; close then
fails closed (process-unconfirmed) and needs --force to clear the handle.

Launch hygiene: registration failures (state write, port claim) terminate the
spawned child through its recorded identity; an already-exited child is never
signalled and a child whose identity was never recorded is never blind-killed
(state+claim kept, pid reported). Ctrl+C before the spawn leaves nothing to
clean; after it, the same cleanup path runs and exits 130 with an envelope.
A direct Popen child is observed through wait()/poll() (POSIX zombies reaped)
rather than pid liveness.

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
import os  # noqa: F401 - facade re-export (tests patch app_drive.os)
from celestia_devtools.env import cdp  # noqa: F401 - facade re-export (tests patch app_drive.cdp)
from celestia_devtools.env import ports  # noqa: F401 - facade re-export
from celestia_devtools.env.app_drive_state import (PROTOCOL_VERSION, STATE_SCHEMA, CDP_ENV_VAR, CDP_PORT_ENV, RESERVED_ENV_KEYS, STATE_ENV, DEFAULT_TIMEOUT, DEFAULT_LAUNCH_TIMEOUT, DEFAULT_WAIT_TIMEOUT, DEFAULT_WAIT_INTERVAL, DEFAULT_WATCH_INTERVAL, DEFAULT_WATCH_DURATION, DEFAULT_CLOSE_GRACE, APP_KEY_PREFIX, WATCH_FIELDS, _TASKKILL_PATH, STATE_EXPRESSION, PROTOCOL_EPILOG, envelope, failure, emit, timestamp_utc, state_root, app_slug, default_app_key, state_file_path, read_state, write_state, remove_state, state_cdp_port)  # noqa: F401 - facade re-export
from celestia_devtools.env.app_drive_proc import (pid_alive, _windows_process_identity, _posix_process_identity, read_process_identity, identity_probe_reason, _normalized_image_path, same_image_path, verify_process_identity, taskkill_path, _taskkill, _signal_tree, wait_pid_exit, wait_child_exit, terminate_tree, terminate_verified, merge_webview2_arguments, parse_env_overrides, build_launch_env, build_launch_command, resolve_executable, state_port_for_attach)  # noqa: F401 - facade re-export
from celestia_devtools.env.app_drive_cdp import (acquire_cdp_port, claim_cdp_port, release_ports, resolve_attach_port, read_page_state, public_target, select_target, connect_page, element_probe_expression, element_click_expression, boot_chrome_matches, condition_description, check_condition, image_dimensions, state_signature, state_changes, watch_stop_reason, launched_summary)  # noqa: F401 - facade re-export
from celestia_devtools.env.app_drive_cli import (cmd_targets, cmd_status, _resolve_expression, cmd_eval, cmd_shot, cmd_click, cmd_wait, cmd_watch, _validate_log_dir, _new_default_log_dir, _open_launch_logs, _close_handles, _spawn, _cleanup_note, _launch_state_from_identity, _state_has_identity, _abort_launch, _replace_existing, _build_launch_state, _validate_launch_options, cmd_launch, _close_app_key, cmd_close, HANDLERS, _add_attach_arguments, build_parser, split_launch_arguments, main)  # noqa: F401 - facade re-export

if __name__ == "__main__":
    # The split moved the implementation into app_drive_cli; direct module
    # invocation (python -m celestia_devtools.env.app_drive) must still run
    # the CLI, not silently exit 0 (R2 S2-1 of the split).
    raise SystemExit(main())
