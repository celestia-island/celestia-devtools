"""``judge-fleet``: the predicate table, the fail-open contract, zero actions.

Three test layers:

* pure predicate/parse units (no processes, no I/O);
* CLI end-to-end against ``tests/fake_mcp_server.py`` (one mode per predicate and per
  failure shape — a fixture server keeps these deterministic and fast);
* real-malkuth acceptance (healthy fleet + crash loop) when a binary with the wave-4
  read arms is available, skipped otherwise.
"""

from __future__ import annotations
import json
import os
import shutil
import subprocess
import socket
import sys
import threading
import time
from pathlib import Path
import pytest
from celestia_devtools.judge import cli, fleet
from celestia_devtools.judge.mcp_stdio import READ_ONLY_TOOLS, mcp_error, mcp_session

HERE = Path(__file__).resolve().parent
FAKE_SERVER = HERE / "fake_mcp_server.py"
JUDGE_SOURCES = Path(cli.__file__).resolve().parent

def _fake_cmd(mode: str) -> str:
    return f"{sys.executable} {FAKE_SERVER} {mode}"

def _run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err

def _worker(**overrides) -> fleet.worker_state:
    fields = dict(id = "app", kind = "backend", phase = "running", pid = 4242, uptime_secs = 120, restart_policy = "permanent", restart_count = 0, last_error = None)
    fields.update(overrides)
    return fleet.worker_state(**fields)

def _run(workers: tuple[fleet.worker_state, ...], **overrides) -> fleet.run_state:
    fields = dict(run_id = "run-1", finished = False, draining = False, drain_kind = None, elapsed_secs = 120)
    fields.update(overrides)
    return fleet.run_state(workers = workers, **fields)

def _observation(ticks: list[fleet.tick]) -> fleet.observation:
    observation = fleet.observation()

    for tick in ticks:
        observation.add(tick)

    return observation

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def _malkuth_bin() -> str | None:
    candidate = os.environ.get("JUDGE_MALKUTH_BIN") or shutil.which("malkuth")
    return candidate if candidate else None

def _test_app_bin(malkuth_bin: str) -> str | None:
    candidate = os.environ.get("JUDGE_TEST_APP_BIN")

    if candidate and Path(candidate).exists():
        return candidate

    sibling = Path(malkuth_bin).resolve().parent / "examples" / "test_app"
    return str(sibling) if sibling.exists() else None

def _supervise_as_fixture(session: mcp_session, arguments: dict) -> dict:
    """Fixture plumbing: create the fleet the judge will read.

    The judge's own contract forbids this (``READ_ONLY_TOOLS`` refuses the tool), so the
    test goes one layer down to JSON-RPC. Keeping the production path closed is the
    point; a test that could reach it through ``call_tool`` would mean the allowlist had
    a hole.
    """
    reply = session._request("tools/call", {"name": "malkuth_supervise", "arguments": arguments})
    content = reply.get("content")
    assert isinstance(content, list) and content, "supervise returned no content"
    return json.loads(content[0]["text"])

def _judge(mode: str, capsys, *extra: str) -> tuple[int, str, str]:
    argv = ["--mcp-cmd", _fake_cmd(mode), "--ticks", "3", "--interval", "0.01", "--logs-lines", "10", *extra]
    return _run_cli(argv, capsys)

def _open_real() -> tuple[mcp_session, str]:
    malkuth_bin = _malkuth_bin()
    assert malkuth_bin is not None
    session = mcp_session([malkuth_bin, "mcp"], timeout = 10.0)
    session.open()

    if not READ_ONLY_TOOLS <= session.tools:
        session.close()
        pytest.skip("installed malkuth predates the wave-4 read arms")

    return session, malkuth_bin

def _judge_with_supervision(workers: list[dict], *, ticks: int = 4, interval: float = 0.35) -> tuple[list[fleet.verdict], fleet.observation]:
    session, _ = _open_real()
    outcome: dict = {}

    def drive() -> None:
        try:
            outcome["supervise"] = _supervise_as_fixture(session, {"workers": workers, "timeout_secs": 4, "max_restarts": 100, "window_secs": 60})
        except mcp_error as error:
            outcome["supervise_error"] = str(error)

    driver = threading.Thread(target = drive, name = "supervise-driver", daemon = True)
    driver.start()
    time.sleep(0.4)
    observation = fleet.observation()

    try:
        failure = cli._collect(session, observation, ticks = ticks, interval = interval, logs_lines = 20)
        assert failure is None, failure
    finally:
        # Let the supervision drain BEFORE the session goes away: a hard close orphans
        # the grandchildren (`test_app worker` pods behind `test_app supervise`) — the
        # same leak class malkuth #175 fixed for its own suite.
        driver.join(timeout = 10)
        session.close()
        _sweep_supervised_leftovers(workers)

    return fleet.evaluate(observation, fleet.thresholds()), observation

_SUPERVISED_PROGRAMS: set[str] = set()

@pytest.fixture(scope = "module", autouse = True)
def _sweep_the_module_fleet():
    """Module teardown: nothing of ours may outlive the suite.

    A supervision can still be restarting a pod when a single test's sweep window closes,
    so a second pass at module teardown (after every real-fleet test) is what actually
    guarantees zero leftovers. The same leak class malkuth #175 fixed for its own suite.
    """
    yield

    for _ in range(25):
        candidates = [pid for program in _SUPERVISED_PROGRAMS for pid in _pids_running(program)]

        if not candidates:
            return

        for pid in candidates:
            _kill_if_abandoned(pid)

        time.sleep(0.4)

    for pid in [pid for program in _SUPERVISED_PROGRAMS for pid in _pids_running(program)]:
        try:
            os.kill(pid, 9)
        except OSError:
            continue

def _sweep_supervised_leftovers(workers: list[dict]) -> None:
    """Kill leftover grandchildren of this test's supervision, scoped and leader-aware.

    A supervised pod can outlive the session close in two shapes: its group leader is gone
    (a plain orphan), or the leader is still draining while its own parent is already gone
    (the malkuth supervisor is the parent). Both are ours; a leader whose parent chain is
    intact belongs to an in-flight run and is never touched. The sweep keeps retrying while
    candidates exist, because malkuth's drain and the example supervisor's exit are not
    instantaneous.
    """
    programs = {worker.get("program") for worker in workers if isinstance(worker.get("program"), str)}

    for _ in range(20):
        candidates = [pid for program in programs if Path(program).exists() for pid in _pids_running(program)]

        if not candidates:
            return

        for pid in candidates:
            _kill_if_abandoned(pid)

        time.sleep(0.4)

def _pids_running(program: str) -> list[int]:
    listing = subprocess.run(["pgrep", "-f", program], capture_output = True, text = True)
    pids = []

    for pid_text in listing.stdout.split():
        if not pid_text.isdigit():
            continue

        cmdline = Path(f"/proc/{pid_text}/cmdline")

        if not cmdline.exists():
            continue

        argv = [part.decode("utf-8", "replace") for part in cmdline.read_bytes().split(b"\0") if part]

        if argv and (os.path.realpath(argv[0]) == os.path.realpath(program)):
            pids.append(int(pid_text))

    return pids

def _parent_of(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[-1].split()
    except OSError:
        return None

    return int(fields[1]) if len(fields) > 1 else None

def _is_init(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() in ("systemd", "init")
    except OSError:
        return False

def _kill_if_abandoned(pid: int) -> bool:
    """Return True when this pid was killed because nothing supervises it any more.

    A supervised pod is easy to spot from the outside: it was reparented to init/systemd
    when the supervision died. The process-group id is NOT usable here — malkuth spawns its
    children without a new session, so the pod inherits the *caller's* group (the test
    process group stays alive and would protect an orphan forever). Ancestry is the only
    honest signal, and it is the same conclusion malkuth #175 reached from the other side.
    """
    parent = _parent_of(pid)

    if (parent is None) or not _is_init(parent):
        return False

    try:
        cwd = Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return False

    if not str(cwd).startswith(str(Path.cwd())):
        return False  # not this checkout's program instance

    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False

# ── parsing ────────────────────────────────────────────────────────────────

def test_parses_the_documented_shape() -> None:
    payload = {"ok": True, "runs": [{"run_id": "run-1", "started_unix_secs": 1, "elapsed_secs": 2, "finished": False, "finished_unix_secs": None, "draining": False, "drain_kind": None, "worker_count": 1, "workers": [{"id": "app", "kind": "backend", "phase": "running", "pid": 7, "uptime_secs": 30, "restart_policy": "permanent", "restart_count": 1, "last_error": None}]}]}
    runs = fleet.parse_status(payload)
    assert len(runs) == 1
    assert runs[0].workers[0].phase == "running"
    assert runs[0].workers[0].restart_count == 1
    assert runs[0].workers[0].last_error is None
    assert runs[0].elapsed_secs == 2

def test_rejects_a_missing_field() -> None:
    with pytest.raises(fleet.schema_error):
        fleet.parse_status({"ok": True, "runs": [{"workers": []}]})

def test_rejects_an_unknown_phase() -> None:
    payload = {"ok": True, "runs": [{"run_id": "run-1", "finished": False, "draining": False, "drain_kind": None, "elapsed_secs": 1, "workers": [{"id": "app", "kind": "k", "phase": "exploded", "pid": 1, "uptime_secs": 0, "restart_policy": "permanent", "restart_count": 0, "last_error": None}]}]}

    with pytest.raises(fleet.schema_error):
        fleet.parse_status(payload)

def test_rejects_a_not_ok_payload() -> None:
    with pytest.raises(fleet.schema_error):
        fleet.parse_status({"ok": False, "code": "unknown_run"})

def test_probe_semantics() -> None:
    assert fleet.probe_ok([{"ok": True, "status": 200}, {"ok": True, "status": 204}]) is True
    assert fleet.probe_ok([{"ok": True, "status": 200}, {"ok": False, "status": 503}]) is False

    with pytest.raises(fleet.schema_error):
        fleet.probe_ok([])

def test_log_digest_is_stable_and_sensitive() -> None:
    assert fleet.log_digest("a\nb") == fleet.log_digest("a\nb")
    assert fleet.log_digest("a\nb") != fleet.log_digest("a\nc")

    with pytest.raises(fleet.schema_error):
        fleet.log_digest(None)

# ── predicates ─────────────────────────────────────────────────────────────

def test_healthy_fleet_has_no_verdict() -> None:
    observation = _observation([fleet.tick(index = 0, runs = (_run((_worker(),)),))])
    assert fleet.evaluate(observation) == []

def test_b1_crash_loop_by_delta() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(restart_count = 0),)),)), fleet.tick(index = 1, runs = (_run((_worker(restart_count = 2),)),))]
    assert "B1" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_b1_catches_a_loop_that_saturated_before_the_window() -> None:
    ticks = [fleet.tick(index = index, runs = (_run((_worker(restart_count = 101, phase = "restarting", uptime_secs = 0),), elapsed_secs = 4),)) for index in range(3)]
    verdicts = [verdict for verdict in fleet.evaluate(_observation(ticks)) if verdict.code == "B1"]
    assert verdicts, "a saturated crash loop must not read as healthy"
    assert verdicts[0].evidence["restarts_per_sec"] > 0

def test_b1_ignores_slow_historic_restarts() -> None:
    ticks = [fleet.tick(index = index, runs = (_run((_worker(restart_count = 7),), elapsed_secs = 1_200_000),)) for index in range(3)]
    assert "B1" not in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_b1_needs_the_threshold() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(restart_count = 0),)),)), fleet.tick(index = 1, runs = (_run((_worker(restart_count = 1),)),))]
    assert "B1" not in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_b2_phase_oscillation() -> None:
    phases = ["running", "restarting", "running", "restarting"]
    ticks = [fleet.tick(index = index, runs = (_run((_worker(phase = phase),)),)) for index, phase in enumerate(phases)]
    assert "B2" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_b3_drain_stalled() -> None:
    ticks = [fleet.tick(index = index, runs = (_run((_worker(),), draining = True, drain_kind = "graceful"),)) for index in range(2)]
    verdicts = [verdict for verdict in fleet.evaluate(_observation(ticks)) if verdict.code == "B3"]
    assert verdicts and (verdicts[0].evidence["stuck_workers"] == ["app"])

def test_c2_spawn_failure() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(phase = "starting", pid = None, restart_count = 0, last_error = "spawn failed: nope"),)),)), fleet.tick(index = 1, runs = (_run((_worker(phase = "starting", pid = None, restart_count = 2, last_error = "spawn failed: nope"),)),))]
    assert "C2" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_a1_readiness_down() -> None:
    ticks = [fleet.tick(index = index, runs = (_run((_worker(),)),), probe_ok = False) for index in range(2)]
    assert "A1" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_a2_log_stall() -> None:
    ticks = []

    for index in range(3):
        tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 100 + index),)),))
        tick.log_digests[("run-1", "app")] = "deadbeef"
        ticks.append(tick)

    assert "A2" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_a2_ignores_a_warming_up_worker() -> None:
    ticks = []

    for index in range(3):
        tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 1 + index),)),))
        tick.log_digests[("run-1", "app")] = "deadbeef"
        ticks.append(tick)

    assert "A2" not in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_c1_silent_hang_needs_all_three_signals() -> None:
    ticks = []

    for index in range(3):
        tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 600),)),), probe_ok = False)
        tick.log_digests[("run-1", "app")] = "deadbeef"
        ticks.append(tick)

    assert "C1" in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

def test_c1_does_not_fire_without_the_frozen_tail() -> None:
    ticks = []

    for index in range(3):
        tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 600),)),), probe_ok = False)
        tick.log_digests[("run-1", "app")] = f"digest-{index}"
        ticks.append(tick)

    assert "C1" not in [verdict.code for verdict in fleet.evaluate(_observation(ticks))]

# ── zero actions (structural) ──────────────────────────────────────────────

def test_allowlist_is_exactly_the_three_read_tools() -> None:
    assert READ_ONLY_TOOLS == {"malkuth_status", "malkuth_logs", "malkuth_probe"}

def test_mutating_tool_names_are_refused() -> None:
    session = mcp_session([sys.executable, str(FAKE_SERVER), "healthy"])

    with pytest.raises(mcp_error):
        session.call_tool("malkuth_" + "restart", {})

def test_the_judge_package_contains_no_action_call() -> None:
    forbidden = ("malkuth_" + "restart", "malkuth_" + "drain", "malkuth_" + "supervise")
    offenders = []

    for path in JUDGE_SOURCES.glob("*.py"):
        text = path.read_text()
        offenders.extend(f"{path.name}: {needle}" for needle in forbidden if needle in text)

    assert offenders == [], f"the judge package must not contain action tools: {offenders}"

# ── CLI end-to-end against the fixture server ──────────────────────────────

def test_healthy_fleet_reports_health(capsys) -> None:
    code, out, _ = _judge("healthy", capsys)
    assert code == 0
    assert "结论: 健康" in out
    assert ("run-1/app" in out) and ("phase=running" in out)

def test_crash_loop_is_reported(capsys) -> None:
    code, out, _ = _judge("crashloop", capsys)
    assert code == 0
    assert ("B1" in out) and ("critical" in out)

def test_log_stall_is_reported(capsys) -> None:
    code, out, _ = _judge("logstall", capsys)
    assert code == 0
    assert "A2" in out

def test_drain_stalled_is_reported(capsys) -> None:
    code, out, _ = _judge("drainstalled", capsys)
    assert code == 0
    assert "B3" in out

def test_phase_oscillation_is_reported(capsys) -> None:
    code, out, _ = _judge("oscillate", capsys, "--ticks", "4")
    assert code == 0
    assert "B2" in out

def test_readiness_down_is_reported(capsys) -> None:
    code, out, _ = _judge("silent-hang", capsys, "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "A1" in out

def test_silent_hang_is_reported(capsys) -> None:
    code, out, _ = _judge("hang", capsys, "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "C1" in out

def test_spawn_failure_is_reported(capsys) -> None:
    code, out, _ = _judge("spawnfail", capsys)
    assert code == 0
    assert "C2" in out

def test_empty_registry_reports_no_fleet(capsys) -> None:
    code, out, _ = _judge("empty", capsys)
    assert code == 0
    assert "无 fleet" in out

def test_server_without_the_read_arms_is_a_cannot_judge(capsys) -> None:
    code, out, _ = _judge("missing-tools", capsys)
    assert code == 0
    assert ("无法判定" in out) and ("does not advertise" in out)

def test_malformed_payload_is_a_cannot_judge(capsys) -> None:
    code, out, _ = _judge("badshape", capsys)
    assert code == 0
    assert "无法判定" in out

def test_non_json_transport_is_a_cannot_judge(capsys) -> None:
    code, out, _ = _judge("junkline", capsys)
    assert code == 0
    assert "无法判定" in out

def test_timeout_is_a_cannot_judge(capsys) -> None:
    code, out, _ = _judge("slow", capsys, "--call-timeout", "0.4")
    assert code == 0
    assert ("无法判定" in out) and ("timed out" in out)

def test_json_report_shape(capsys) -> None:
    code, out, _ = _judge("crashloop", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert (payload["ok"] is True) and (payload["read_only"] is True)
    assert (payload["runs"] == 1) and (payload["workers"] == 1)
    assert any(verdict["code"] == "B1" for verdict in payload["verdicts"])
    assert payload["reserved_predicates"]

def test_action_mode_is_refused(capsys) -> None:
    code, _, err = _run_cli(["--no-read-only"], capsys)
    assert code == 2
    assert "target-bound approval" in err

def test_single_tick_is_refused(capsys) -> None:
    code, _, err = _run_cli(["--ticks", "1"], capsys)
    assert code == 2
    assert "at least 2" in err

# ── real malkuth acceptance ────────────────────────────────────────────────

def test_healthy_test_app_fleet_is_not_called_critical() -> None:
    if _malkuth_bin() is None:
        pytest.skip("no malkuth binary available")

    _, malkuth_bin = _open_real()
    test_app = _test_app_bin(malkuth_bin)

    if test_app is None:
        pytest.skip("test_app example binary not available next to the malkuth binary")

    workers = [{"id": "app", "kind": "test", "program": test_app, "args": ["supervise", "--pods", "1", "--port-base", str(_free_port())]}]
    verdicts, observation = _judge_with_supervision(workers)
    assert observation.latest() is not None
    assert [run for run in observation.latest().runs], "no run observed"
    critical = [verdict for verdict in verdicts if verdict.severity == "critical"]
    assert critical == [], f"a healthy test_app fleet was escalated: {critical}"

def test_crash_loop_is_detected_on_real_malkuth() -> None:
    if _malkuth_bin() is None:
        pytest.skip("no malkuth binary available")

    _open_real()[0].close()
    workers = [{"id": "crasher", "kind": "test", "program": "/bin/sh", "args": ["-c", "exit 7"]}]
    verdicts, _ = _judge_with_supervision(workers)
    codes = {verdict.code for verdict in verdicts}
    assert ("B1" in codes) or ("C2" in codes), f"crash loop not detected: {verdicts}"

def test_installed_malkuth_without_arms_reports_cannot_judge(capsys) -> None:
    """The real-world version of acceptance ③: an old server is 'cannot judge', not a guess."""
    malkuth_bin = _malkuth_bin()

    if malkuth_bin is None:
        pytest.skip("no malkuth binary available")

    with mcp_session([malkuth_bin, "mcp"], timeout = 10.0) as session:
        if READ_ONLY_TOOLS <= session.tools:
            pytest.skip("this environment's malkuth already advertises the read arms")

    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--ticks", "2", "--interval", "0.01"], capsys)
    assert code == 0
    assert "无法判定" in out
