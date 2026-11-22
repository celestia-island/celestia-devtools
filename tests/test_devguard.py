# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""Tests for devguard — the promoted WoWSP #2095 dev-script mechanism.

Two regressions travel with this module, and both are pinned here:

* an orphaned dev server holding the dev port (run N left its child tree
  alive, run N+1's guard refused) — the process-level tests spawn a REAL
  child tree (wrapper -> port-holding grandchild) and assert the tree kill
  frees the port;
* the guard's mojibake on CJK paths — every PowerShell read must force the
  UTF-8 console output encoding before querying Win32_Process, and the
  refusal must carry a copy-pasteable kill command.

Portability: the mechanism is cross-platform, so the constants, message
builders, pid validation and the poll helper are asserted on every runner.
The kill paths are marked per platform — taskkill semantics on Windows, the
process-group escalation on POSIX. Both self-hosted CI fleets (ubuntu,
windows) therefore cover one path each.

MANUAL CHECK (OS-level Ctrl-C delivery, not simulated here because sending
CTRL_C_EVENT to a console process group would also interrupt the test
runner): run a dev script in a codepage-936 console, press Ctrl-C once, then
confirm the child really died:

    netstat -ano | findstr :5199      # must print nothing
"""

from __future__ import annotations
import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import pytest
from celestia_devtools import devguard

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason = "the taskkill kill path is Windows-only")
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason = "the process-group kill path is POSIX-only")

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0

def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return True

        time.sleep(interval)

    return predicate()

def _sleeper(tmp_path) -> subprocess.Popen:
    script = tmp_path / "sleeper.py"
    script.write_text("import time; time.sleep(300)\n", encoding = "utf-8")
    child = subprocess.Popen([sys.executable, str(script)], **devguard.spawn_kwargs())
    devguard.track(child.pid, child)
    return child

# ── platform primitives and pids ──────────────────────────────────────────────

class test_kill_command:
    def test_matches_platform(self):
        if os.name == "nt":
            assert devguard.kill_command("44060") == "taskkill /PID 44060 /T /F"
        else:
            assert devguard.kill_command("44060") == "kill -TERM 44060"

    def test_accepts_an_int_pid(self):
        assert "44060" in devguard.kill_command(44060)

class test_pid_validation:
    def test_digits_pass_through(self):
        assert devguard.validate_pid("44060") == "44060"
        assert devguard.validate_pid(44060) == "44060"

    def test_non_numeric_is_refused(self):
        # The pid is interpolated into a PowerShell -Command string; a crafted
        # value must never become command text.
        assert devguard.validate_pid("44060; rm -rf /") is None
        assert devguard.validate_pid("") is None
        assert devguard.validate_pid("12 34") is None

    def test_describe_pid_command_refuses_non_numeric(self):
        with pytest.raises(ValueError):
            devguard.describe_pid_command("44060; whoami")

    def test_describe_pid_returns_empty_for_garbage(self):
        assert devguard.describe_pid("not-a-pid") == ""

class test_describe_pid_command:
    def test_forces_utf8_console_output(self):
        argv = devguard.describe_pid_command("44060")
        assert argv[:2] == ["powershell", "-NoProfile"]
        assert argv[-2] == "-Command"
        command = argv[-1]
        assert command.startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; ")
        assert "ProcessId=44060" in command
        # The prefix must end with '; ' so it cannot glue onto the expression.
        assert devguard.PS_UTF8_PREFIX.endswith("; ")

    @WINDOWS_ONLY
    def test_tasklist_info_line_is_not_an_identity(self, monkeypatch):
        calls = []

        class _result:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(argv, **kwargs):
            calls.append(argv)

            if argv[0] == "powershell":
                return _result("")

            return _result("INFO: No tasks are running which match the specified criteria.\n")

        monkeypatch.setattr(devguard.subprocess, "run", fake_run)
        assert devguard.describe_pid("999999") == ""
        assert [argv[0] for argv in calls] == ["powershell", "tasklist"]

# ── the port guard ────────────────────────────────────────────────────────────

class test_busy_port_message:
    def test_names_pid_and_copy_pasteable_command(self):
        owner = devguard.PortOwner("44060", ", node.exe vite")
        message = devguard.busy_port_message(5199, owner)
        assert "something is already listening on :5199" in message
        assert "(pid 44060, node.exe vite)" in message
        assert devguard.kill_command("44060") in message

    def test_appends_the_callers_policy_sentence(self):
        owner = devguard.PortOwner("44060", "")
        message = devguard.busy_port_message(5199, owner, "This is NOT this app's dev server.")
        assert message.endswith("This is NOT this app's dev server.")
        assert devguard.kill_command("44060") in message

    def test_falls_back_to_lookup_command_without_pid(self):
        message = devguard.busy_port_message(5199, devguard.PortOwner(None, ""))
        assert "something is already listening on :5199" in message
        assert devguard.listener_lookup_command(5199) in message
        assert "taskkill" not in message

    def test_ends_with_exactly_one_period(self):
        message = devguard.busy_port_message(5199, devguard.PortOwner(None, ""), "Stop it..")
        assert message.endswith("Stop it.")
        assert not message.endswith("..")

class test_busy_port:
    def test_free_port_has_no_owner(self, monkeypatch):
        monkeypatch.setattr(devguard, "port_in_use", lambda port: False)
        assert devguard.busy_port(5199) is None

    def test_owner_without_attribution(self, monkeypatch):
        monkeypatch.setattr(devguard, "port_in_use", lambda port: True)
        monkeypatch.setattr(devguard, "port_listener_pid", lambda port: None)
        assert devguard.busy_port(5199) == devguard.PortOwner(None, "")

    def test_owner_is_described(self, monkeypatch):
        monkeypatch.setattr(devguard, "port_in_use", lambda port: True)
        monkeypatch.setattr(devguard, "port_listener_pid", lambda port: "44060")
        monkeypatch.setattr(devguard, "describe_pid", lambda pid: "node.exe vite")
        assert devguard.busy_port(5199) == devguard.PortOwner("44060", ", node.exe vite")

    def test_caller_lookup_wins(self):
        assert devguard.busy_port(5199, owner_lookup = lambda port: devguard.PortOwner("7", ", x")) == devguard.PortOwner("7", ", x")

class test_port_in_use:
    def test_detects_a_real_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            assert devguard.port_in_use(listener.getsockname()[1])

    def test_free_port_reads_as_free(self):
        assert not devguard.port_in_use(_free_port())

class test_listener_pid_parsing:
    def test_windows_netstat_line(self, monkeypatch):
        listing = "  TCP    0.0.0.0:5199    0.0.0.0:0    LISTENING    44060\n"
        monkeypatch.setattr(devguard, "_run_quiet", lambda argv: listing)
        assert devguard._windows_listener_pid(5199) == "44060"

    def test_windows_netstat_ignores_a_non_listening_row(self, monkeypatch):
        listing = "  TCP    0.0.0.0:5199    1.2.3.4:50000    ESTABLISHED    44060\n"
        monkeypatch.setattr(devguard, "_run_quiet", lambda argv: listing)
        assert devguard._windows_listener_pid(5199) is None

    def test_windows_netstat_refuses_a_non_numeric_pid(self, monkeypatch):
        listing = "  TCP    0.0.0.0:5199    0.0.0.0:0    LISTENING    notapid\n"
        monkeypatch.setattr(devguard, "_run_quiet", lambda argv: listing)
        assert devguard._windows_listener_pid(5199) is None

    def test_posix_lsof(self, monkeypatch):
        monkeypatch.setattr(devguard, "_run_quiet", lambda argv: "44060\n")
        assert devguard._posix_listener_pid(5199) == "44060"

    def test_posix_ss_fallback(self, monkeypatch):
        def fake(argv):
            if argv[0] == "lsof":
                return ""

            return 'users:(("node",pid=44060,fd=21))'

        monkeypatch.setattr(devguard, "_run_quiet", fake)
        assert devguard._posix_listener_pid(5199) == "44060"

    def test_posix_without_any_tool(self, monkeypatch):
        monkeypatch.setattr(devguard, "_run_quiet", lambda argv: "")
        assert devguard._posix_listener_pid(5199) is None

class test_require_port_free:
    def test_exits_with_the_kill_command(self):
        reported = []

        with pytest.raises(SystemExit) as excinfo:
            devguard.require_port_free(5199, "This is NOT this app's dev server.", owner_lookup = lambda port: devguard.PortOwner("44060", ", node.exe vite"), report = reported.append)

        assert excinfo.value.code == 1
        assert len(reported) == 1
        assert devguard.kill_command("44060") in reported[0]
        assert "This is NOT this app's dev server." in reported[0]

    def test_returns_when_the_port_is_free(self):
        devguard.require_port_free(5199, owner_lookup = lambda port: None)

    def test_default_report_goes_to_stderr(self, capsys, monkeypatch):
        monkeypatch.setattr(devguard, "port_in_use", lambda port: True)
        monkeypatch.setattr(devguard, "port_listener_pid", lambda port: "44060")
        monkeypatch.setattr(devguard, "describe_pid", lambda pid: "node.exe vite")

        with pytest.raises(SystemExit):
            devguard.require_port_free(5199)

        assert "already listening on :5199" in capsys.readouterr().err

class test_console_safe:
    def test_preserves_cjk_on_a_gbk_console(self):
        # The maintainers' consoles are GBK: a CJK command line must survive.
        stream = types.SimpleNamespace(encoding = "gbk")
        assert devguard.console_safe("D:/源代码/工程项目/wowsp", stream) == "D:/源代码/工程项目/wowsp"

    def test_replaces_unencodable_characters(self):
        stream = types.SimpleNamespace(encoding = "ascii")
        assert devguard.console_safe("源", stream) == "?"

    def test_falls_back_without_an_encoding(self):
        assert devguard.console_safe("plain", types.SimpleNamespace()) == "plain"

# ── tracking ──────────────────────────────────────────────────────────────────

class test_tracking:
    def test_spawn_kwargs_match_the_platform(self):
        kwargs = devguard.spawn_kwargs()

        if os.name == "nt":
            assert kwargs == {}
        else:
            assert kwargs == {"start_new_session": True}

    def test_track_and_untrack(self, tmp_path):
        child = _sleeper(tmp_path)

        try:
            assert child.pid in devguard.tracked()
            devguard.untrack(child.pid)
            assert child.pid not in devguard.tracked()
        finally:
            devguard.untrack(child.pid)
            child.kill()
            child.wait(timeout = 10)

    def test_track_prunes_exited_handles(self, tmp_path):
        finished = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        finished.wait(timeout = 30)
        devguard.track(finished.pid, finished)
        assert finished.pid not in devguard.tracked()

    def test_untrack_is_idempotent(self):
        devguard.untrack(4_000_000)
        assert 4_000_000 not in devguard.tracked()

# ── tree kill and the poll loop ───────────────────────────────────────────────

_HOLDER = """\
import socket, sys, time

port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)
print("bound", flush=True)
time.sleep(300)
"""

_WRAPPER = """\
import subprocess, sys, time

child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
print(child.pid, flush=True)
time.sleep(300)
"""

class test_tree_kill:
    def test_signal_handler_tree_kills_the_port_holder(self, tmp_path):
        # The regression: a wrapper spawns a grandchild that holds the port; the
        # SIGINT path must tree-kill BOTH and free the port (an orphan here is
        # the every-other-run "port already in use" block).
        holder = tmp_path / "holder.py"
        holder.write_text(_HOLDER, encoding = "utf-8")
        wrapper = tmp_path / "wrapper.py"
        wrapper.write_text(_WRAPPER, encoding = "utf-8")
        port = _free_port()
        child = subprocess.Popen([sys.executable, str(wrapper), str(holder), str(port)], **devguard.spawn_kwargs())
        devguard.track(child.pid, child)

        try:
            assert child.pid in devguard.tracked()
            assert _wait_until(lambda: _port_open(port)), "grandchild never bound the port"
            assert child.poll() is None

            with pytest.raises(KeyboardInterrupt):
                devguard._on_signal(signal.SIGINT, None)

            assert _wait_until(lambda: not _port_open(port)), "the SIGINT path left an orphan on the port"
            assert _wait_until(lambda: child.poll() is not None), "the SIGINT path left the direct child alive"
            assert child.pid not in devguard.tracked()
        finally:
            devguard.tree_kill(child.pid)
            devguard.cleanup_children()

    def test_cleanup_children_is_idempotent(self, tmp_path):
        child = _sleeper(tmp_path)

        try:
            devguard.cleanup_children()
            devguard.cleanup_children()
            assert _wait_until(lambda: child.poll() is not None), "cleanup did not kill the long sleep"
            assert child.pid not in devguard.tracked()
        finally:
            devguard.tree_kill(child.pid)
            devguard.cleanup_children()

    def test_unkillable_child_stays_tracked(self, tmp_path, monkeypatch):
        # A kill that cannot be confirmed must stay registered so the next
        # atexit/signal pass retries it instead of dropping the tree.
        child = _sleeper(tmp_path)
        monkeypatch.setattr(devguard, "_taskkill", lambda pid, force: -1)
        monkeypatch.setattr(devguard, "_signal_tree", lambda pid, signum: None)

        try:
            assert devguard.tree_kill(child.pid, grace = 0.1) is False
            assert child.pid in devguard.tracked()
        finally:
            monkeypatch.undo()
            devguard.tree_kill(child.pid)

    def test_exited_child_is_untracked(self, tmp_path):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        devguard.track(child.pid, child)
        child.wait(timeout = 30)
        assert devguard.tree_kill(child.pid) is True
        assert child.pid not in devguard.tracked()

    @WINDOWS_ONLY
    def test_windows_kill_uses_taskkill_on_the_tree(self, monkeypatch):
        calls = []
        monkeypatch.setattr(devguard, "_taskkill", lambda pid, force: calls.append((pid, force)) or 0)
        monkeypatch.setattr(devguard, "_gone", lambda pid, child: bool(calls))
        devguard.track(4_000_000)
        assert devguard.tree_kill(4_000_000) is True
        assert calls == [(4_000_000, True)]

    @POSIX_ONLY
    def test_posix_kill_escalates_to_sigkill(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(devguard, "_signal_tree", lambda pid, signum: sent.append(signum))
        child = _sleeper(tmp_path)

        try:
            # The sleeper outlives SIGTERM only if it ignores it; force the
            # escalation by reporting it alive until SIGKILL was sent.
            monkeypatch.setattr(devguard, "_await_exit", lambda pid, handle, grace: False)
            monkeypatch.setattr(devguard, "_gone", lambda pid, handle: signal.SIGKILL in sent)
            assert devguard.tree_kill(child.pid) is True
            assert sent == [signal.SIGTERM, signal.SIGKILL]
        finally:
            monkeypatch.undo()
            devguard.tree_kill(child.pid)

class test_wait_poll:
    def test_returns_the_exit_code(self, tmp_path):
        script = tmp_path / "exit3.py"
        script.write_text("raise SystemExit(3)\n", encoding = "utf-8")
        child = subprocess.Popen([sys.executable, str(script)])
        devguard.track(child.pid, child)

        try:
            assert devguard.wait_poll(child) == 3
        finally:
            devguard.tree_kill(child.pid)

    def test_dispatches_a_signal_while_the_child_is_alive(self, tmp_path):
        # The poll loop is what lets a signal handler tree-kill a child that
        # ignores Ctrl-C; a blocking Popen.wait() would only run the handler
        # after the child exits.
        child = _sleeper(tmp_path)

        def raise_interrupt(signum, frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGINT, raise_interrupt)
        timer = threading.Timer(1.0, signal.raise_signal, args = (signal.SIGINT,))
        timer.start()

        try:
            started = time.monotonic()

            with pytest.raises(KeyboardInterrupt):
                devguard.wait_poll(child)

            assert (time.monotonic() - started) < 5.0, "the wait must not block until the child exits"
            assert child.poll() is None, "the handler must run while the child is still alive"
        finally:
            timer.cancel()
            timer.join()
            signal.signal(signal.SIGINT, previous)
            devguard.tree_kill(child.pid)

class test_exit_hooks:
    def test_install_is_idempotent(self, monkeypatch):
        saved = {}

        for name in ("SIGINT", "SIGBREAK", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)

            if sig is not None:
                saved[sig] = signal.getsignal(sig)

        monkeypatch.setattr(devguard, "_EXIT_HOOKS_INSTALLED", False)

        try:
            devguard.install_exit_hooks()
            devguard.install_exit_hooks()

            for sig in saved:
                assert signal.getsignal(sig) is devguard._on_signal
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

            atexit.unregister(devguard.cleanup_children)

    def test_terminating_signal_exits_with_128_plus_signum(self, monkeypatch):
        monkeypatch.setattr(devguard, "cleanup_children", lambda: None)
        signum = signal.SIGTERM

        with pytest.raises(SystemExit) as excinfo:
            devguard._on_signal(signum, None)

        assert excinfo.value.code == (128 + int(signum))

    def test_signal_name_falls_back_to_the_number(self):
        assert devguard.signal_name(9999) == "9999"
