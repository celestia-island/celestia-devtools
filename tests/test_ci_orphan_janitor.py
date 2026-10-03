"""Tests for the zombie-worker cancellation detector in ci_orphan_janitor."""

import importlib.util
import re
import subprocess
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
    # cancel_grace=0 so the check reaches the refusal instead of stopping at the grace
    # comparison (with a 300s-old line and grace 600 this test passed even with the
    # refusal removed)
    assert jan.zombie_worker_check(str(d), cancel_grace=0, dry_run=False,
                                   unit="u") is False
    assert calls == []


def test_slot_units_tolerates_the_runner_bom(tmp_path, monkeypatch):
    """The runner writes .runner with a UTF-8 BOM; a bare json.loads silently gives none."""
    home = tmp_path / "actions-runner"
    (home / "_work").mkdir(parents=True)
    (home / ".runner").write_bytes(
        b"\xef\xbb\xbf" + b'{"agentName": "node-ci-9"}')
    monkeypatch.setattr(jan, "unit_exists", lambda unit: True)
    monkeypatch.setattr(jan, "unit_serves_home", lambda unit, h: True)
    assert jan.slot_units([str(home)]) == {
        str(home): "actions.runner.celestia-island.node-ci-9.service"}


def test_slot_units_rejects_a_unit_that_serves_another_home(tmp_path, monkeypatch):
    """A .runner naming another slot's agent must not map this home to that unit.

    LoadState alone is not enough: restarting a unit that runs a different runner home
    would kill a live job in the other slot.
    """
    home = tmp_path / "actions-runner"
    (home / "_work").mkdir(parents=True)
    (home / ".runner").write_text('{"agentName": "node-ci-9b"}')
    monkeypatch.setattr(jan, "unit_exists", lambda unit: True)
    monkeypatch.setattr(jan, "unit_serves_home", lambda unit, h: False)
    monkeypatch.setattr(jan, "units_by_home", lambda homes: {str(home): "u-real"})
    assert jan.slot_units([str(home)]) == {str(home): "u-real"}


def test_slot_units_falls_back_to_systemd(tmp_path, monkeypatch):
    """A .runner that cannot be parsed must not leave the slot without a unit."""
    home = tmp_path / "actions-runner-2"
    (home / "_work").mkdir(parents=True)  # no .runner at all
    monkeypatch.setattr(jan, "unit_exists", lambda unit: False)
    monkeypatch.setattr(jan, "units_by_home", lambda homes: {str(home): "u-discovered"})
    assert jan.slot_units([str(home)]) == {str(home): "u-discovered"}


def test_worker_pids_attributes_a_relative_argv_by_cwd(tmp_path, monkeypatch):
    """A Worker whose argv carries no path must still be found for its own slot."""
    home_a = tmp_path / "actions-runner"
    home_b = tmp_path / "actions-runner-2"
    for h in (home_a, home_b):
        (h / "_work").mkdir(parents=True)

    class _P:
        def pids(self):
            return [700, 701]

        def cmdline(self, pid):
            # 700: relative argv, cwd identifies the slot; 701: absolute argv for slot B
            return ("Runner.Worker spawnclient 1 2" if pid == 700
                    else f"{home_b}/bin/Runner.Worker spawnclient 3 4")

        def comm(self, pid):
            return "Runner.Worker"

        def cwd(self, pid):
            return str(home_a) if pid == 700 else str(home_b)

    monkeypatch.setattr(jan, "PROC", _P())
    assert jan.worker_pids(str(home_a)) == [700]
    assert jan.worker_pids(str(home_b)) == [701]


def test_selftest_panel_passes():
    """CI runs pytest only, so the tool's own panel must be exercised here.

    Several orphan criteria (the criterion-5 tie, the renamed-Worker comm fallback) are
    pinned by --selftest and by nothing else; without this test they could rot unnoticed
    because no workflow invokes the flag. The case count is asserted as well: a panel
    that reports "0/0 passed" is empty, not green.
    """
    r = subprocess.run([sys.executable, TOOL, "--selftest"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout, r.stdout
    m = re.search(r"selftest: (\d+)/(\d+) passed", r.stdout)
    assert m, r.stdout
    passed, total = int(m.group(1)), int(m.group(2))
    assert total >= 30 and passed == total, r.stdout


class _Result:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_unit_serves_home_rejects_a_unit_for_another_home(monkeypatch):
    """LoadState alone is not enough - the unit must serve THIS home."""
    monkeypatch.setattr(jan.subprocess, "run",
                        lambda *a, **k: _Result("/home/lab/actions-runner-2\n"))
    assert jan.unit_serves_home("u", "/home/lab/actions-runner") is False


def test_unit_serves_home_accepts_slashes_and_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "actions-runner"
    home.mkdir()
    monkeypatch.setattr(jan.subprocess, "run", lambda *a, **k: _Result(f"{home}/\n"))
    assert jan.unit_serves_home("u", str(home)) is True
    link = tmp_path / "linked-runner"
    link.symlink_to(home)
    assert jan.unit_serves_home("u", str(link)) is True


def test_unit_serves_home_rejects_an_empty_working_directory(tmp_path, monkeypatch):
    """A nonexistent unit answers rc=0 with empty stdout; that must not read as a match.

    The cwd is set to the home on purpose: realpath("") is the cwd, so without the empty
    guard this would compare equal and restart the wrong unit.
    """
    home = tmp_path / "actions-runner"
    home.mkdir()
    monkeypatch.chdir(home)
    monkeypatch.setattr(jan.subprocess, "run", lambda *a, **k: _Result("\n"))
    assert jan.unit_serves_home("u", str(home)) is False


def test_units_by_home_normalises_the_home(tmp_path, monkeypatch):
    """The fallback must resolve the same paths unit_serves_home accepts."""
    home = tmp_path / "actions-runner"
    home.mkdir()

    def fake(cmd, **kwargs):
        if cmd[1] == "list-units":
            return _Result("actions.runner.o.n.service loaded active running GitHub\n")
        return _Result(f"{home}/\n")  # trailing slash in WorkingDirectory

    monkeypatch.setattr(jan.subprocess, "run", fake)
    assert jan.units_by_home([str(home)]) == {str(home): "actions.runner.o.n.service"}


def test_unit_without_diag_dir_is_rejected():
    """--unit only means something together with --diag-dir; say so instead of ignoring it."""
    r = subprocess.run([sys.executable, TOOL, "--unit", "u", "--dry-run"],
                       capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--diag-dir" in (r.stdout + r.stderr)


def test_diag_dir_with_a_unit_serving_another_home_is_refused(tmp_path):
    """The cancellation is read from this home's diag, so only its own unit may restart."""
    home = tmp_path / "actions-runner-2"
    (home / "_work").mkdir(parents=True)
    diag = home / "_diag"
    diag.mkdir()
    (diag / "Runner_20260101-000000-utc.log").write_text(CANCEL_LINE + "\n")
    r = subprocess.run([sys.executable, TOOL, "--diag-dir", str(diag),
                        "--unit", "actions.runner.celestia-island.nope.service",
                        "--dry-run"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "does not serve" in r.stdout, r.stdout
