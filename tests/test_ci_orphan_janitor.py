"""Tests for the zombie-worker cancellation detector in ci_orphan_janitor."""

import importlib.util
import time as _time
import os
import sys
import time
from datetime import datetime, timezone


TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ci_orphan_janitor.py")
spec = importlib.util.spec_from_file_location("ci_orphan_janitor", TOOL)
jan = importlib.util.module_from_spec(spec)
sys.modules["ci_orphan_janitor"] = jan
spec.loader.exec_module(jan)


# Round-18's M-2: the fixtures were born with a 24h fuse — the
# hardcoded 2026-09-16 line aged past the tool's 86400s staleness
# filter a day after landing, turning the suite red while the old
# dispatcher behavior masked every completing run. All fixtures are
# now generated RELATIVE to the current time: "recent" means five
# minutes ago, "old" means three days ago — the suite can never age
# red again.

_NOW = _time.time()
_RECENT_DT = datetime.fromtimestamp(_NOW - 300, tz=timezone.utc)
_OLD_DT = datetime.fromtimestamp(_NOW - 3 * 86400, tz=timezone.utc)
_RECENT_LINE_TS = _RECENT_DT.strftime("%Y-%m-%d %H:%M:%S")
_OLD_LINE_TS = _OLD_DT.strftime("%Y-%m-%d %H:%M:%S")


def _cancel_line(ts_str):
    return (f"[{ts_str}Z INFO JobDispatcher] Job cancellation request "
            "78964727-4f2f-506a-b92c-a2971cecf940 received, "
            "cancellation timeout 5 minutes.")


CANCEL_LINE = _cancel_line(_RECENT_LINE_TS)


def test_cancel_re_parses_official_line():
    m = jan.CANCEL_RE.search(CANCEL_LINE)
    assert m is not None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()
    assert abs(ts - _RECENT_DT.timestamp()) < 1
    assert m.group(2) == "78964727-4f2f-506a-b92c-a2971cecf940"


def test_cancel_re_ignores_other_lines():
    assert jan.CANCEL_RE.search(
        f"[{_RECENT_LINE_TS}Z INFO JobDispatcher] Successfully renew job") is None
    assert jan.CANCEL_RE.search(
        "haven't exit within cancellation timout, kill running worker.") is None


def _write_log(tmp_path, body, name=None, slot="actions-runner"):
    # a real _diag lives inside a runner home (…/actions-runner/_work exists), which is
    # how the tool attributes a cancellation to a slot before it restarts anything
    home = tmp_path / slot
    (home / "_work").mkdir(parents=True, exist_ok=True)
    d = home / "_diag"
    d.mkdir(exist_ok=True)
    if name is None:
        name = "Runner_" + _RECENT_DT.strftime("%Y%m%d-%H%M%S") + "-utc.log"
    (d / name).write_text(body)
    return str(d)


def test_last_cancel_request_reads_recent(tmp_path):
    diag = _write_log(tmp_path, "noise\n" + CANCEL_LINE + "\nmore noise\n")
    got = jan.last_cancel_request(diag)
    assert got > 0
    assert abs(got - _RECENT_DT.timestamp()) < 1


def test_last_cancel_request_ignores_old_logs(tmp_path):
    old = _cancel_line(_OLD_LINE_TS)
    diag = _write_log(tmp_path, old + "\n")
    assert jan.last_cancel_request(diag) == 0.0


def test_last_cancel_request_missing_dir(tmp_path):
    assert jan.last_cancel_request(str(tmp_path / "nope")) == 0.0


def test_zombie_restart_when_worker_predates_cancel(tmp_path, monkeypatch):
    # The zombie check needs a cancel OLDER than the grace (600s) but
    # still inside the tool's 86400s staleness filter — 20 minutes ago
    # sits in that window forever.
    _cancel_20m = datetime.fromtimestamp(_NOW - 1200, tz=timezone.utc)
    line = _cancel_line(_cancel_20m.strftime("%Y-%m-%d %H:%M:%S"))
    diag = _write_log(tmp_path, line + "\n")
    cancel_ts = jan.last_cancel_request(diag)
    assert cancel_ts > 0

    started_before = cancel_ts - 100
    started_after = cancel_ts + 100
    monkeypatch.setattr(jan, "worker_pids", lambda home=None: [4242])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: started_before)
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd, timeout=None: calls.append(cmd) or type("R", (), {"returncode": 0})())

    fired = jan.zombie_worker_check(diag, cancel_grace=600, dry_run=False,
                                    unit="actions.runner.celestia-island.node-ci-1.service")
    assert fired is True
    assert calls == [["systemctl", "try-restart",
                      "actions.runner.celestia-island.node-ci-1.service"]]

    # a worker started after the cancellation is a live job, not a zombie
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: started_after)
    calls.clear()
    fired = jan.zombie_worker_check(diag, cancel_grace=600, dry_run=False, unit="u")
    assert fired is False
    assert calls == []


def test_zombie_respects_grace(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda home=None: [1])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: time.time() - 9999)
    # cancel timestamp is fixed in the log; force tiny grace and a worker
    # that predates it -> would fire, but only if age >= grace
    cancel_ts = jan.last_cancel_request(diag)
    age = time.time() - cancel_ts
    assert jan.zombie_worker_check(diag, cancel_grace=age + 10,
                                   dry_run=True, unit="u") is False
    assert jan.zombie_worker_check(diag, cancel_grace=0,
                                   dry_run=True, unit="u") is True


def test_zombie_no_worker_is_noop(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda home=None: [])
    assert jan.zombie_worker_check(diag, cancel_grace=0, dry_run=True,
                                   unit="u") is False


def test_dry_run_does_not_restart(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda home=None: [7])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: 1.0)
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd, timeout=None: calls.append(cmd) or type("R", (), {"returncode": 0})())
    assert jan.zombie_worker_check(diag, cancel_grace=0, dry_run=True, unit="u") is True
    assert calls == []


class _FakeProc:
    """Only cmdline/starttime matter for the worker scan."""

    def __init__(self, table):
        self.table = table

    def pids(self):
        return sorted(self.table)

    def cmdline(self, pid):
        return self.table[pid]["cmdline"]

    def comm(self, pid):
        return self.table[pid].get("comm", "")

    def cwd(self, pid):
        return self.table[pid].get("cwd", "")

    def starttime(self, pid):
        return self.table[pid]["start"]

    def read_text(self, path):
        return jan.read_text_file(path)


def test_zombie_never_uses_another_slots_worker(tmp_path, monkeypatch):
    """A cancellation in slot A must not be justified by a Worker in slot B."""
    diag_a = _write_log(tmp_path, CANCEL_LINE + "\n", slot="actions-runner")
    home_b = tmp_path / "actions-runner-2"
    (home_b / "_work").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jan, "PROC", _FakeProc({
        4242: {"cmdline": f"{home_b}/bin/Runner.Worker spawnclient 1 2",
               "start": jan.last_cancel_request(diag_a) - 100},
    }))
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd, timeout=None: calls.append(cmd)
                        or type("R", (), {"returncode": 0})())
    assert jan.zombie_worker_check(diag_a, cancel_grace=600, dry_run=False,
                                   unit="u-b") is False
    assert calls == []


def test_zombie_refuses_when_the_slot_is_unattributable(tmp_path, monkeypatch):
    """An unattributable cancellation is never acted on (no machine-wide fallback)."""
    d = tmp_path / "elsewhere" / "_diag"
    d.mkdir(parents=True)
    (d / "Runner_20260101-000000-utc.log").write_text(CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda home=None: [4242])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: 1.0)
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd, timeout=None: calls.append(cmd)
                        or type("R", (), {"returncode": 0})())
    assert jan.zombie_worker_check(str(d), cancel_grace=600, dry_run=False,
                                   unit="u") is False
    assert calls == []
