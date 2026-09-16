"""Tests for the zombie-worker cancellation detector in ci_orphan_janitor."""

import importlib.util
import os
import sys
import time
from datetime import datetime, timezone


TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ci_orphan_janitor.py")
spec = importlib.util.spec_from_file_location("ci_orphan_janitor", TOOL)
jan = importlib.util.module_from_spec(spec)
sys.modules["ci_orphan_janitor"] = jan
spec.loader.exec_module(jan)


CANCEL_LINE = ("[2026-09-16 03:05:20Z INFO JobDispatcher] Job cancellation request "
               "78964727-4f2f-506a-b92c-a2971cecf940 received, cancellation timeout 5 minutes.")


def test_cancel_re_parses_official_line():
    m = jan.CANCEL_RE.search(CANCEL_LINE)
    assert m is not None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()
    # 2026-09-16 03:05:20Z
    assert abs(ts - datetime(2026, 9, 16, 3, 5, 20, tzinfo=timezone.utc)
               .timestamp()) < 1
    assert m.group(2) == "78964727-4f2f-506a-b92c-a2971cecf940"


def test_cancel_re_ignores_other_lines():
    assert jan.CANCEL_RE.search(
        "[2026-09-16 03:05:20Z INFO JobDispatcher] Successfully renew job") is None
    assert jan.CANCEL_RE.search(
        "haven't exit within cancellation timout, kill running worker.") is None


def _write_log(tmp_path, body, name="Runner_20260916-030000-utc.log"):
    d = tmp_path / "_diag"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    return str(d)


def test_last_cancel_request_reads_recent(tmp_path):
    diag = _write_log(tmp_path, "noise\n" + CANCEL_LINE + "\nmore noise\n")
    got = jan.last_cancel_request(diag)
    assert got > 0
    assert abs(got - datetime(2026, 9, 16, 3, 5, 20, tzinfo=timezone.utc)
               .timestamp()) < 1


def test_last_cancel_request_ignores_old_logs(tmp_path):
    old = CANCEL_LINE.replace("2026-09-16", "2025-01-01")
    diag = _write_log(tmp_path, old + "\n")
    assert jan.last_cancel_request(diag) == 0.0


def test_last_cancel_request_missing_dir(tmp_path):
    assert jan.last_cancel_request(str(tmp_path / "nope")) == 0.0


def test_zombie_restart_when_worker_predates_cancel(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    cancel_ts = jan.last_cancel_request(diag)
    assert cancel_ts > 0

    started_before = cancel_ts - 100
    started_after = cancel_ts + 100
    monkeypatch.setattr(jan, "worker_pids", lambda: [4242])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: started_before)
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())

    fired = jan.zombie_worker_check(diag, cancel_grace=600, dry_run=False,
                                    unit="actions.runner.celestia-island.node-ci-1.service")
    assert fired is True
    assert calls == [["systemctl", "restart",
                      "actions.runner.celestia-island.node-ci-1.service"]]

    # a worker started after the cancellation is a live job, not a zombie
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: started_after)
    calls.clear()
    fired = jan.zombie_worker_check(diag, cancel_grace=600, dry_run=False, unit="u")
    assert fired is False
    assert calls == []


def test_zombie_respects_grace(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda: [1])
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
    monkeypatch.setattr(jan, "worker_pids", lambda: [])
    assert jan.zombie_worker_check(diag, cancel_grace=0, dry_run=True,
                                   unit="u") is False


def test_dry_run_does_not_restart(tmp_path, monkeypatch):
    diag = _write_log(tmp_path, CANCEL_LINE + "\n")
    monkeypatch.setattr(jan, "worker_pids", lambda: [7])
    monkeypatch.setattr(jan, "starttime_epoch", lambda pid: 1.0)
    calls = []
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    assert jan.zombie_worker_check(diag, cancel_grace=0, dry_run=True, unit="u") is True
    assert calls == []
