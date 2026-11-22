#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Dev-script process-tree guarding and dev-port refusal.

Promoted from WoWSP #2095 so every celestia repository's dev orchestrator
(`scripts/dev.py` and friends) inherits the same hard-won mechanisms
instead of re-deriving them:

  - **Tracked children that are tree-killed on every exit path.** A dev
    script spawns long-lived trees (pnpm -> Vite -> node, cargo -> app). When
    the script leaves through Ctrl-C, SIGTERM, an exception between spawn and
    join, or a plain return, those trees must die with it — one orphaned Vite
    holds the dev port and the NEXT run's guard refuses to start. `taskkill
    /PID <pid> /T /F` is the only primitive that reaches the grandchildren of
    a shell-spawned Windows child; on POSIX the child leads its own process
    group (see :func:`spawn_kwargs`) and the group takes SIGTERM, then
    SIGKILL.

  - **A poll-loop wait helper.** WHY this exists at all, measured on WoWSP
    #2095: `Popen.wait()` parks the main thread inside
    `WaitForSingleObject(INFINITE)` on Windows, and a console SIGINT cannot
    dispatch while the main thread sits there. A handler that is supposed to
    tree-kill a Ctrl-C-ignoring child therefore only runs AFTER that child
    exits (6.04 s), by which point the orphan owns the port; polling
    :func:`wait_poll` lets the pending handler run within one interval
    (1.02 s) while the child is still alive and killable.

  - **A UTF-8-safe process describer.** PowerShell writes its REDIRECTED
    stdout in the console codepage (GBK/cp936 on the maintainers' hosts), so a
    CJK repo path came back as `D:/Դ????/...` even though the pipe was read
    as UTF-8. Every PowerShell read must pin
    `[Console]::OutputEncoding=[System.Text.Encoding]::UTF8` (that is
    :data:`PS_UTF8_PREFIX`) before querying `Win32_Process`, and the pid
    interpolated into the `-Command` string must be validated as decimal
    digits first.

  - **A port guard helper** whose MECHANISM lives here (detect, attribute the
    pid, describe it, build a refusal with a copy-pasteable kill command) while
    the POLICY stays in the caller: which port matters, the sentence that
    explains the consequence, and where the refusal is logged.

Everything is stdlib-only: a downstream dev script must never need an extra
package to keep its children from leaking.

Typical wiring::

    from celestia_devtools import devguard

    devguard.install_exit_hooks(notify = lambda text: log.warn(text))
    devguard.require_port_free(5199, "This is NOT this app's dev server.")

    child = subprocess.Popen(argv, **devguard.spawn_kwargs())
    devguard.track(child.pid, child)
    devguard.wait_poll(child)
    devguard.tree_kill(child.pid)

The `atexit` + `SIGINT`/`SIGBREAK`/`SIGTERM`/`SIGHUP` handlers close
the paths that skip the explicit teardown. Known gaps are the same ones WoWSP
#2095 recorded rather than papered over: no Python runs on a hard kill of the
script itself, and a Ctrl-C that reaches the whole console group first can
leave `/T` with no tree root to walk.
"""
from __future__ import annotations
import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple
from celestia_devtools.env.ports import pid_alive

DEFAULT_GRACE = 10.0
POLL_INTERVAL = 0.25
IDENTITY_LIMIT = 160
PS_UTF8_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "

_LIVE_CHILDREN: dict[int, subprocess.Popen | None] = {}
_LIVE_LOCK = threading.RLock()
_CLEANUP_IN_PROGRESS = False
_EXIT_HOOKS_INSTALLED = False
_NOTIFY: Callable[[str], None] | None = None
_TASKKILL_PATH: str | None = None

# ── spawning and tracking ─────────────────────────────────────────────────────

def spawn_kwargs() -> dict:
    """Popen kwargs that keep the POSIX tree-kill able to reach the whole tree.

    Pass these to every `subprocess.Popen` whose children must die with the
    script. Windows needs nothing (`taskkill /T` walks the tree by parent
    pid); POSIX needs the child to LEAD its own session, otherwise a group kill
    would signal the script's own group — i.e. the script itself.
    """

    if os.name == "nt":
        return {}

    return {"start_new_session": True}

def track(pid: int, child: subprocess.Popen | None = None) -> None:
    """Remember a spawned process so the exit hooks can tree-kill it.

    `child` is the Popen handle when the process is OUR direct child: it lets
    the teardown reap the child (a POSIX zombie would otherwise answer
    `os.kill(pid, 0)`) and read the real exit code. An external pid may be
    tracked without a handle. Entry pruning is lazy: tracking sweeps handles
    that have already exited.
    """

    with _LIVE_LOCK:
        stale = [key for key, value in _LIVE_CHILDREN.items() if _exited(value)]

        for key in stale:
            _LIVE_CHILDREN.pop(key, None)

        if _exited(child):
            _LIVE_CHILDREN.pop(int(pid), None)
        else:
            _LIVE_CHILDREN[int(pid)] = child

def untrack(pid: int) -> None:
    """Stop tracking `pid` (called once a kill is confirmed, or by the caller)."""

    with _LIVE_LOCK:
        _LIVE_CHILDREN.pop(int(pid), None)

def tracked() -> list[int]:
    """The pids a later cleanup pass would still try to kill, oldest first."""

    with _LIVE_LOCK:
        return list(_LIVE_CHILDREN)

# ── tree kill ─────────────────────────────────────────────────────────────────

def tree_kill(pid: int, grace: float = DEFAULT_GRACE) -> bool:
    """Tree-terminate one process; idempotent, never raises.

    Windows: `taskkill /PID <pid> /T /F` immediately — no signal-based
    graceful stop reaches the grandchildren of a shell-spawned child there.
    POSIX: SIGTERM the process group, wait up to `grace`, then SIGKILL the
    group (a descendant can outlive a cleanly exiting leader, so a clean leader
    exit must not end the teardown).

    Returns True once the process is confirmed gone AND untracked. A kill that
    cannot be confirmed stays tracked on purpose: the next atexit/signal pass
    retries it instead of silently dropping the tree.
    """
    pid = int(pid)
    child = _registered_child(pid)

    if _gone(pid, child):
        untrack(pid)
        return True

    try:
        if os.name == "nt":
            _taskkill(pid, force = True)
        else:
            _signal_tree(pid, signal.SIGTERM)

            if not _await_exit(pid, child, grace):
                _signal_tree(pid, signal.SIGKILL)
    except Exception:
        _last_resort_kill(pid, child)
    finally:
        _reap(child)

    if _gone(pid, child):
        untrack(pid)
        return True

    return False

def cleanup_children() -> None:
    """Tree-kill every live child. Runs on atexit, on every handled signal, and
    from a caller's explicit teardown. Never raises; reentrant-safe.

    :func:`tree_kill` untracks only on a confirmed kill, so an aborted pass
    (second Ctrl-C, taskkill timeout) leaves the survivors registered for the
    next one.
    """
    global _CLEANUP_IN_PROGRESS

    with _LIVE_LOCK:
        if _CLEANUP_IN_PROGRESS:
            return

        _CLEANUP_IN_PROGRESS = True
        pending = list(_LIVE_CHILDREN)

    try:
        for pid in pending:
            tree_kill(pid)
    finally:
        with _LIVE_LOCK:
            _CLEANUP_IN_PROGRESS = False

def wait_poll(child: subprocess.Popen, interval: float = POLL_INTERVAL) -> int | None:
    """Wait for `child` in short polls — NEVER in one blocking `child.wait()`.

    This is what makes a Ctrl-C guarantee real. Measured on WoWSP #2095: on
    Windows `Popen.wait()` parks the main thread in
    `WaitForSingleObject(INFINITE)`, which a console SIGINT handler cannot
    interrupt, so the handler that should tree-kill a Ctrl-C-ignoring child only
    runs after that child exits — 6.04 s later, with the orphan still holding
    the dev port. Polling lets the pending handler dispatch within one interval
    (1.02 s) while the child is still alive. Returns the exit code once the
    child is reaped.
    """

    while child.poll() is None:
        time.sleep(interval)

    return child.returncode

# ── exit hooks ────────────────────────────────────────────────────────────────

def install_exit_hooks(notify: Callable[[str], None] | None = None) -> None:
    """Register the atexit + signal cleanup exactly once (idempotent).

    Install BEFORE the first spawn: a signal between spawn and an explicit join
    would otherwise leave an untracked orphan. `notify` lets the caller route
    the "received SIGINT" line through its own logger; without it the line goes
    to stderr. A signal the platform does not define (SIGBREAK on POSIX, SIGHUP
    on Windows) is skipped; a signal that cannot be installed (e.g. called from
    a non-main thread) is skipped too — atexit still covers us.
    """
    global _EXIT_HOOKS_INSTALLED, _NOTIFY

    if _EXIT_HOOKS_INSTALLED:
        return

    _EXIT_HOOKS_INSTALLED = True
    _NOTIFY = notify
    atexit.register(cleanup_children)

    for name in ("SIGINT", "SIGBREAK", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)

        if sig is None:
            continue

        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError, RuntimeError):
            pass

def _on_signal(signum: int, _frame) -> None:
    """Kill the spawned trees BEFORE unwinding, so Ctrl-C cannot orphan a child."""
    _notify("received " + signal_name(signum) + " — stopping child process trees")
    cleanup_children()

    if signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGBREAK", None)):
        raise KeyboardInterrupt

    raise SystemExit(128 + signum)

def signal_name(signum: int) -> str:
    """The signal's name (`SIGINT`), falling back to the number."""

    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)

def _notify(message: str) -> None:
    """Emit one teardown line; a broken reporter must never skip the cleanup."""
    callback = _NOTIFY

    if callback is None:
        try:
            print(message, file = sys.stderr, flush = True)
        except Exception:
            pass

        return

    try:
        callback(message)
    except Exception:
        pass

# ── process description (UTF-8-safe) ──────────────────────────────────────────

def kill_command(pid: str | int) -> str:
    """Copy-pasteable one-liner that stops `pid` (with its tree on Windows)."""

    if os.name == "nt":
        return f"taskkill /PID {pid} /T /F"

    return f"kill -TERM {pid}"

def validate_pid(value: str | int) -> str | None:
    """`value` as decimal digits, or None when it is not numeric.

    Every pid that reaches a shell (the PowerShell `-Command` string, the
    printed kill one-liner) goes through this first: a non-numeric token is
    REFUSED rather than escaped, so a crafted process name cannot become
    command text.
    """
    text = str(value).strip()
    return text if text.isdigit() else None

def describe_pid_command(pid: str) -> list[str]:
    """PowerShell argv that reads a pid's command line as UTF-8 text.

    :data:`PS_UTF8_PREFIX` is mandatory: `powershell.exe` writes its
    redirected stdout in the console codepage (GBK/cp936 on the maintainers'
    hosts), so a CJK repo path came back as mojibake even though the pipe was
    read with `encoding = "utf-8"`. Forcing `[Console]::OutputEncoding`
    makes the producer match the consumer. Raises ValueError for a non-numeric
    pid (it is interpolated into the command string).
    """
    validated = validate_pid(pid)

    if validated is None:
        raise ValueError("refusing a non-numeric pid: " + repr(pid))

    query = "(Get-CimInstance Win32_Process -Filter 'ProcessId=" + validated + "').CommandLine"
    return ["powershell", "-NoProfile", "-Command", PS_UTF8_PREFIX + query]

def describe_pid(pid: str) -> str:
    """Best-effort one-line identity for a pid (command line, else image name).

    Returns "" when the pid cannot be described — never raises, because this is
    only ever used to make an error message actionable. `tasklist` is the
    Windows fallback (CIM can be unavailable or slow); its "INFO: No tasks are
    running ..." line is an answer about a DEAD pid, not an identity.
    """
    validated = validate_pid(pid)

    if validated is None:
        return ""

    try:
        if os.name == "nt":
            return _describe_windows(validated)

        return _describe_posix(validated)
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""

def _describe_windows(pid: str) -> str:
    finished = subprocess.run(describe_pid_command(pid), capture_output = True, text = True, encoding = "utf-8", errors = "replace", timeout = 6)
    text = finished.stdout.strip()

    if text:
        return text[:IDENTITY_LIMIT]

    tasks = subprocess.run(["tasklist", "/FI", "PID eq " + pid, "/FO", "CSV", "/NH"], capture_output = True, text = True, encoding = "utf-8", errors = "replace", timeout = 5)
    lines = tasks.stdout.strip().splitlines()
    fallback = lines[0] if lines else ""

    if (not fallback) or fallback.startswith("INFO:"):
        return ""

    return fallback[:IDENTITY_LIMIT]

def _describe_posix(pid: str) -> str:
    finished = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output = True, text = True, encoding = "utf-8", errors = "replace", timeout = 5)
    text = finished.stdout.strip()
    return text[:IDENTITY_LIMIT] if text else ""

# ── port guard ────────────────────────────────────────────────────────────────

class PortOwner(NamedTuple):
    """Who holds a port: the resolved pid (None when it could not be resolved)
    plus a best-effort identity suffix (", <command line>"), empty when unknown."""

    pid: str | None
    detail: str

def port_in_use(port: int) -> bool:
    """Is anything LISTENING on 127.0.0.1:`port`? A refused connect means free."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0

def port_listener_pid(port: int) -> str | None:
    """The pid listening on `port`, or None when the lookup cannot attribute it.

    Windows: `netstat -ano`. POSIX: `lsof` first (macOS and most Linux
    images ship it), then `ss -ltnp` — CI's self-hosted Ubuntu runners may
    have neither, in which case the caller degrades to an unattributed refusal.
    """

    if os.name == "nt":
        return _windows_listener_pid(port)

    return _posix_listener_pid(port)

def _windows_listener_pid(port: int) -> str | None:
    listing = _run_quiet(["netstat", "-ano"])
    suffix = ":" + str(int(port))

    for line in listing.splitlines():
        parts = line.split()

        # Match ANY local address on the port (0.0.0.0 / 127.0.0.1 / [::]): a
        # dev server typically binds 0.0.0.0, so an exact 127.0.0.1 comparison
        # would detect the conflict but lose the pid from the message.
        if (len(parts) >= 5) and parts[1].endswith(suffix) and (parts[3] == "LISTENING"):
            return validate_pid(parts[4])

    return None

def _posix_listener_pid(port: int) -> str | None:
    listening = str(int(port))
    owners = _run_quiet(["lsof", "-nP", "-iTCP:" + listening, "-sTCP:LISTEN", "-t"]).split()

    if owners:
        digits = validate_pid(owners[0])

        if digits is not None:
            return digits

    listing = _run_quiet(["ss", "-ltnp", "sport = :" + listening])
    marker = "pid="

    if marker not in listing:
        return None

    return validate_pid(listing.split(marker, 1)[1].split(",", 1)[0])

def busy_port(port: int, owner_lookup: Callable[[int], PortOwner | None] | None = None) -> PortOwner | None:
    """Who holds `port`, or None when it is free.

    `owner_lookup` lets a caller keep its own attribution policy (WoWSP names
    the offending command line) while detection stays shared; the default is
    detect -> resolve pid -> describe it.
    """

    if owner_lookup is not None:
        return owner_lookup(port)

    if not port_in_use(port):
        return None

    pid = port_listener_pid(port)

    if pid is None:
        return PortOwner(None, "")

    identity = describe_pid(pid)
    return PortOwner(pid, (", " + identity) if identity else "")

def listener_lookup_command(port: int) -> str:
    """How to find an unattributable listener by hand, per platform."""

    if os.name == "nt":
        return "netstat -ano | findstr :" + str(int(port))

    return "ss -ltnp | grep :" + str(int(port))

def busy_port_message(port: int, owner: PortOwner, expected_owner_hint: str | None = None) -> str:
    """The refusal's headline: mechanism facts plus the caller's policy sentence.

    Ends in a COPY-PASTEABLE kill one-liner carrying the resolved pid — no
    placeholder the user has to fill in. Without a pid it names the platform's
    listener-lookup command instead.
    """
    message = "something is already listening on :" + str(int(port))

    if owner.pid:
        message += f" (pid {owner.pid}{owner.detail})"
        message += f". Stop that process tree with `{kill_command(owner.pid)}`"
    else:
        message += ". Find it with `" + listener_lookup_command(port) + "`"

    if expected_owner_hint:
        return _terminated(message + ". " + expected_owner_hint)

    return _terminated(message)

def require_port_free(port: int, expected_owner_hint: str | None = None, *, owner_lookup: Callable[[int], PortOwner | None] | None = None, report: Callable[[str], None] | None = None) -> None:
    """Refuse to start when `port` already has a listener — SystemExit(1).

    The mechanism is here; the policy stays in the caller. `port` is the
    caller's choice, `expected_owner_hint` is the sentence explaining the
    consequence (what a foreign listener would do to THIS repo's dev session),
    and `report` is where the refusal is logged — the default prints to
    stderr through :func:`console_safe`.

    Why a hard exit: a dev session that silently shared its port loaded a
    FOREIGN project's frontend into its own window (WoWSP, 2026-10-30). A guard
    that only warns would reproduce that.
    """
    owner = busy_port(port, owner_lookup = owner_lookup)

    if owner is None:
        return

    message = busy_port_message(port, owner, expected_owner_hint)

    if report is not None:
        report(message)
    else:
        print(console_safe(message), file = sys.stderr, flush = True)

    raise SystemExit(1)

def console_safe(text: str, stream = None) -> str:
    """Make `text` printable on `stream`'s console encoding.

    The identity line can carry a path from another project; Windows consoles
    are commonly GBK, and a logger that prints through them with no encoding
    fallback turns an unencodable character into a UnicodeEncodeError traceback
    — the port guard then reports a crash instead of the conflict. Encode and
    decode against the stream's own encoding, replacing what it cannot
    represent.
    """
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"

    try:
        return text.encode(encoding, errors = "replace").decode(encoding)
    except (LookupError, UnicodeError):
        return text.encode("ascii", errors = "replace").decode("ascii")

def _terminated(text: str) -> str:
    """`text` with exactly one trailing period."""
    return text.strip().rstrip(".") + "."

# ── internals ─────────────────────────────────────────────────────────────────

def taskkill_path() -> str:
    """`taskkill.exe` by absolute System32 path first: a PATH entry earlier in
    the search order must not be able to shadow a kill primitive."""
    global _TASKKILL_PATH

    if _TASKKILL_PATH is None:
        candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
        _TASKKILL_PATH = str(candidate) if candidate.is_file() else (shutil.which("taskkill") or "taskkill")

    return _TASKKILL_PATH

def _taskkill(pid: int, force: bool) -> int:
    command = [taskkill_path(), "/PID", str(int(pid)), "/T"]

    if force:
        command.append("/F")

    try:
        finished = subprocess.run(command, capture_output = True, timeout = 15)
    except (OSError, subprocess.TimeoutExpired):
        return -1

    return int(finished.returncode)

def _signal_tree(pid: int, signum: int) -> None:
    """Signal the child's process GROUP when it leads one, else the pid itself.

    The group is used only when it differs from OUR group: a child that shares
    the script's group (a Popen without :func:`spawn_kwargs`) must never be
    group-killed, because that signal would kill the script too.
    """

    if os.name != "nt":
        try:
            group = os.getpgid(int(pid))

            if group != os.getpgid(0):
                os.killpg(group, signum)
                return
        except OSError:
            pass

    try:
        os.kill(int(pid), signum)
    except OSError:
        pass

def _last_resort_kill(pid: int, child: subprocess.Popen | None) -> None:
    """Kill the direct process when the platform primitive itself raised."""

    try:
        if child is not None:
            child.kill()
        elif os.name != "nt":
            _signal_tree(pid, signal.SIGKILL)
    except OSError:
        pass

def _reap(child: subprocess.Popen | None) -> None:
    """Reap a tracked Popen so POSIX does not leave a zombie behind."""

    if child is None:
        return

    try:
        child.wait(timeout = 1)
    except (subprocess.TimeoutExpired, OSError):
        pass

def _await_exit(pid: int, child: subprocess.Popen | None, grace: float) -> bool:
    """Does the process disappear within `grace`? Polls, never a bare wait."""
    deadline = time.monotonic() + max(grace, 0.0)

    while time.monotonic() <= deadline:
        if _gone(pid, child):
            return True

        time.sleep(0.05)

    return _gone(pid, child)

def _gone(pid: int, child: subprocess.Popen | None) -> bool:
    """A Popen handle reaps (so a POSIX zombie reads as gone); a bare pid polls."""

    if child is not None:
        return child.poll() is not None

    return not pid_alive(pid)

def _exited(child: subprocess.Popen | None) -> bool:
    return (child is not None) and (child.poll() is not None)

def _registered_child(pid: int) -> subprocess.Popen | None:
    with _LIVE_LOCK:
        return _LIVE_CHILDREN.get(int(pid))

def _run_quiet(argv: list[str]) -> str:
    """stdout of a metadata command ("" when the tool is missing or fails)."""

    try:
        return subprocess.run(argv, capture_output = True, text = True, errors = "replace", timeout = 10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
