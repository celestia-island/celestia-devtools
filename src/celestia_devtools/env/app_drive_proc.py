"""Process identity and lifecycle for app-drive.

Owns pid liveness, cross-platform process-identity probing, image-path
equivalence, tree termination (taskkill / signal tree) and the launch
environment/command builders. Split out of app_drive.py (2026-10-31).
"""

from __future__ import annotations
import ctypes
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence
from celestia_devtools.env import cdp
from celestia_devtools.env import ports
from celestia_devtools.env.app_drive_state import (CDP_ENV_VAR, CDP_PORT_ENV, DEFAULT_CLOSE_GRACE, RESERVED_ENV_KEYS, state_cdp_port)

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False

    return ports.pid_alive(int(pid))

def _windows_process_identity(pid: int) -> dict | None:
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

def _posix_process_identity(pid: int) -> dict | None:
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

def read_process_identity(pid: int) -> dict | None:
    """Best-effort identity of a live process: full image path + creation token.

    Returns None when the platform or the process cannot be inspected; every
    caller treats None as "fail closed".
    """

    if not pid:
        return None

    if os.name == "nt":
        return _windows_process_identity(int(pid))

    return _posix_process_identity(int(pid))

def identity_probe_reason(pid: int) -> str:
    """Why an identity probe failed, distinguishing access-denied from gone.

    Windows: OpenProcess error 5 (ACCESS_DENIED) means the process may well be
    alive but unreadable (e.g. elevated); error 87 (INVALID_PARAMETER) means no
    such pid. POSIX: /proc missing -> no-such-process; EACCES -> access-denied;
    otherwise unreadable. The reason is surfaced in fail-closed messages so an
    operator can tell "gone" from "I cannot see it".
    """
    pid = int(pid)

    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)

        if handle:
            kernel32.CloseHandle(handle)
            return "readable"

        code = int(kernel32.GetLastError())

        if code == 5:
            return "access-denied"

        if code == 87:
            return "no-such-process"

        return "winerror-" + str(code)

    proc_dir = Path("/proc/" + str(pid))

    if not proc_dir.is_dir():
        return "no-such-process"

    try:
        os.readlink("/proc/" + str(pid) + "/exe")
    except PermissionError:
        return "access-denied"
    except FileNotFoundError:
        return "no-such-process"
    except OSError:
        return "unreadable"

    return "readable"

def _normalized_image_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))

def same_image_path(left: str, right: str) -> bool:
    """Compare two spellings of an executable path.

    The recorded path comes from Python's resolve() while the live path comes
    from QueryFullProcessImageNameW; a symlinked or 8.3-short launch path can
    therefore differ textually from the canonical form. exact/normalized match
    is tried first, then realpath on both sides. If they still differ the
    caller fails closed (safe): relaunch through the canonical path instead of
    guessing.
    """

    if _normalized_image_path(left) == _normalized_image_path(right):
        return True

    try:
        left_real = os.path.normcase(os.path.realpath(str(left)))
        right_real = os.path.normcase(os.path.realpath(str(right)))
    except (OSError, ValueError):
        return False

    return left_real == right_real

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

    current_kind = str(current.get("create_kind") or "")

    if current_kind != recorded_kind:
        raise cdp.cdp_error("pid-mismatch", "pid " + str(pid) + " reports its creation token in kind " + repr(current_kind) + ", but the launch recorded kind " + repr(recorded_kind) + "; refusing to touch it")

    current_token = str(current.get("create_token") or "")

    if current_token != recorded_token:
        raise cdp.cdp_error("pid-mismatch", "pid " + str(pid) + " was reused (creation token " + repr(current_token) + " != recorded " + repr(recorded_token) + "); refusing to touch it")

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
    """Signal a process group when the pid leads one, else the pid itself.

    A previously launched app normally runs in its own group (launch uses
    start_new_session / CREATE_NEW_PROCESS_GROUP), but the explicit
    --pid/--force break-glass can target any pid: killpg(pid) then raises
    ProcessLookupError for a non-group-leader and the direct os.kill(pid)
    fallback is what makes that documented path work.
    """
    pid = int(pid)

    if os.name != "nt":
        try:
            os.killpg(pid, signal_number)
            return
        except OSError:
            pass  # no such group / not a group leader: fall back to the pid

    try:
        os.kill(pid, signal_number)
    except OSError:
        pass

def wait_pid_exit(pid: int, timeout: float) -> int | None:
    """Watch an EXTERNAL pid (no Popen handle) via liveness; zombies count dead."""
    started = time.monotonic()

    while (time.monotonic() - started) <= timeout:
        if not pid_alive(pid):
            return int((time.monotonic() - started) * 1000)

        time.sleep(0.05)

    return None

def wait_child_exit(child: subprocess.Popen, timeout: float) -> int | None:
    """Watch OUR OWN child through its Popen handle and reap it on exit.

    A Popen child that has exited but is not reaped is a zombie: on POSIX it
    still answers os.kill(pid, 0), so a pid-liveness loop would wait for the
    whole grace budget even though the process is dead. wait()/poll() reaps and
    reports the real exit.
    """
    started = time.monotonic()

    try:
        child.wait(timeout = max(timeout, 0.0))
    except (subprocess.TimeoutExpired, OSError):
        pass

    if child.poll() is None:
        return None

    return int((time.monotonic() - started) * 1000)

def terminate_tree(pid: int, grace: float = DEFAULT_CLOSE_GRACE, child: subprocess.Popen | None = None, verify = None) -> dict:
    """Graceful-then-forced termination.

    child: the Popen handle when the target is OUR direct child (launch
    cleanup / in-process callers); observed via wait()/poll() so POSIX zombies
    are reaped and never mistaken for running processes.
    verify: re-validation callback invoked immediately BEFORE the forced signal
    (the identity could have been recycled during the grace window).
    """
    pid = int(pid)

    if child is not None:
        if child.poll() is not None:
            return {"closed": True, "forced": False, "waited_ms": 0, "reason": "not-running"}

        forced = False

        if os.name == "nt":
            _taskkill(pid, False)
            waited = wait_child_exit(child, grace)

            if waited is None:
                if verify is not None:
                    verify()

                _taskkill(pid, True)
                forced = True
                waited = wait_child_exit(child, grace)
        else:
            _signal_tree(pid, signal.SIGTERM)
            waited = wait_child_exit(child, grace)

            if waited is None:
                if verify is not None:
                    verify()

                _signal_tree(pid, signal.SIGKILL)
                forced = True
                waited = wait_child_exit(child, grace)

        if waited is None:
            raise cdp.cdp_error("close-failed", "the process " + str(pid) + " did not exit within " + format(grace, "g") + "s" + (" (forced kill sent)" if forced else ""))

        return {"closed": True, "forced": forced, "waited_ms": waited, "reason": "terminated"}

    if not pid_alive(pid):
        return {"closed": True, "forced": False, "waited_ms": 0, "reason": "not-running"}

    forced = False

    if os.name == "nt":
        _taskkill(pid, False)
        waited = wait_pid_exit(pid, grace)

        if waited is None:
            if verify is not None:
                verify()

            _taskkill(pid, True)
            forced = True
            waited = wait_pid_exit(pid, grace)
    else:
        _signal_tree(pid, signal.SIGTERM)
        waited = wait_pid_exit(pid, grace)

        if waited is None:
            if verify is not None:
                verify()

            _signal_tree(pid, signal.SIGKILL)
            forced = True
            waited = wait_pid_exit(pid, grace)

    if waited is None:
        raise cdp.cdp_error("close-failed", "the process " + str(pid) + " did not exit within " + format(grace, "g") + "s" + (" (forced kill sent)" if forced else ""))

    return {"closed": True, "forced": forced, "waited_ms": waited, "reason": "terminated"}

def terminate_verified(state: dict, pid: int, grace: float = DEFAULT_CLOSE_GRACE, child: subprocess.Popen | None = None) -> dict:
    """Verify identity, verify again, and re-verify immediately before force."""
    verify_process_identity(state, pid)
    verify_process_identity(state, pid)

    def recheck() -> dict:
        return verify_process_identity(state, pid)

    return terminate_tree(pid, grace, child = child, verify = recheck)

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

        if key.strip().upper() in RESERVED_ENV_KEYS:
            raise cdp.cdp_error("bad-env", "--env must not override " + key + ": app-drive owns that variable (the CDP port/WebView2 arguments are derived from the allocated port; Windows environment names are case-insensitive)")

        parsed[key] = value

    return parsed

def build_launch_env(base, cdp_port: int, overrides: Sequence[str]) -> dict[str, str]:
    environment = dict(base)
    environment[CDP_ENV_VAR] = merge_webview2_arguments(environment.get(CDP_ENV_VAR, ""), cdp_port)
    environment[CDP_PORT_ENV] = str(int(cdp_port))
    environment.update(parse_env_overrides(overrides))
    return environment

def build_launch_command(exe: str, extra: Sequence[str]) -> list[str]:
    return [str(exe), *[str(item) for item in extra]]

def resolve_executable(exe: str) -> str:
    candidate = Path(str(exe))

    if candidate.is_file():
        return str(candidate.resolve())

    found = shutil.which(str(exe))

    if found:
        return str(Path(found).resolve())

    raise cdp.cdp_error("exe-not-found", "executable not found: " + str(exe) + " (pass a path, or a name on PATH)")

def state_port_for_attach(state: dict, path: Path | None = None) -> int:
    if not pid_alive(state.get("pid")):
        raise cdp.cdp_error("app-not-running", "app " + repr(state.get("app")) + " from " + str(path) + " is not running; relaunch it with app-drive launch")

    return state_cdp_port(state, path)
