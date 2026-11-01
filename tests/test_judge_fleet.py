"""``judge-fleet``: the predicate table, the fail-open contract, zero actions.

Three test layers:

* pure predicate/parse units (no processes, no I/O);
* CLI end-to-end against ``tests/fake_mcp_server.py`` (one mode per predicate and per
  failure shape — a fixture server keeps these deterministic and fast);
* real-malkuth acceptance (healthy fleet + crash loop) when a binary with the wave-4
  read arms is available, skipped otherwise.
"""

from __future__ import annotations
import ast
import json
import os
import shutil
import subprocess
import socket
import sys
import time
from pathlib import Path
import pytest
from celestia_devtools.judge import advice, cli, demo_fleet, fleet
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

def _judge(mode: str, capsys, *extra: str) -> tuple[int, str, str]:
    argv = ["--mcp-cmd", _fake_cmd(mode), "--ticks", "3", "--interval", "0.01", "--logs-lines", "10", *extra]
    return _run_cli(argv, capsys)

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
    # An endpoint that claims ok but answered 5xx is a readiness failure, not health.
    assert fleet.probe_state([{"ok": True, "status": 500}]) == "down"
    assert fleet.probe_ok([{"ok": True, "status": 500}]) is False

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

def test_c2_evidence_has_no_phantom_threshold() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(restart_count = 0, last_error = "spawn failed: nope"),)),)), fleet.tick(index = 1, runs = (_run((_worker(restart_count = 1, last_error = "spawn failed: nope"),)),))]
    verdicts = [verdict for verdict in fleet.evaluate(_observation(ticks)) if verdict.code == "C2"]
    assert verdicts, "C2 must fire on a one-restart spawn failure"
    assert verdicts[0].evidence["restart_delta"] == 1
    assert "threshold_used" not in verdicts[0].evidence

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

# ── teardown identity (a status pid is a hint, not an identity) ────────────

def test_terminate_skips_a_recycled_pid(monkeypatch) -> None:
    """A pid that now belongs to something else must never be signalled."""
    killed = []
    monkeypatch.setattr(demo_fleet, "_proc_cmdline", lambda pid: ["/usr/bin/something-else"])
    monkeypatch.setattr(demo_fleet.os, "kill", lambda pid, signal: killed.append(pid))
    assert demo_fleet.terminate([4242], expect = ["/tmp/demo/test_app"]) == 0
    assert killed == []

def test_terminate_requires_identity_and_a_pid(monkeypatch) -> None:
    killed = []
    monkeypatch.setattr(demo_fleet.os, "kill", lambda pid, signal: killed.append(pid))
    assert demo_fleet.terminate([4242]) == 0, "no expect list: never signal"
    assert demo_fleet.terminate([], expect = ["/tmp/demo/test_app"]) == 0
    assert killed == []

def test_terminate_signals_a_matching_process(monkeypatch) -> None:
    killed = []
    monkeypatch.setattr(demo_fleet, "_proc_cmdline", lambda pid: ["/tmp/demo/test_app", "worker"])
    monkeypatch.setattr(demo_fleet.os, "kill", lambda pid, signal: killed.append(pid))
    assert demo_fleet.terminate([4242], expect = ["/tmp/demo/test_app"]) == 1
    assert killed[0] == 4242, "the first signal is TERM to the verified pid"
    assert set(killed) == {4242}, "a TERM-ignoring survivor is escalated to KILL"

def test_terminate_does_not_touch_a_live_foreign_process() -> None:
    """Real process, wrong identity: it keeps running."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    try:
        assert demo_fleet.terminate([sleeper.pid], expect = ["/nonexistent/demo-program"]) == 0
        time.sleep(0.2)
        assert sleeper.poll() is None, "a foreign process was terminated"
    finally:
        sleeper.kill()
        sleeper.wait(timeout = 10)

# ── sweep scope (checkout root, not the caller's cwd) ───────────────────────

def test_sweep_uses_the_checkout_root_and_rejects_prefix_siblings(monkeypatch, tmp_path) -> None:
    killed = []
    fake_pids = {111: "/mnt/work/r241-r2pp-srcX", 222: str(tmp_path / "inside")}
    monkeypatch.setattr(demo_fleet, "_iter_pids", lambda: sorted(fake_pids))
    monkeypatch.setattr(demo_fleet, "_proc_cmdline", lambda pid: ["/tmp/demo-r241/test_app"])
    monkeypatch.setattr(demo_fleet, "_proc_parent_comm", lambda pid: "systemd")
    monkeypatch.setattr(demo_fleet, "_proc_cwd", lambda pid: Path(fake_pids[pid]))
    monkeypatch.setattr(demo_fleet, "_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(demo_fleet.os, "kill", lambda pid, signal: killed.append(pid))
    assert demo_fleet.sweep_abandoned([["/tmp/demo-r241/test_app"]]) == 1
    assert killed == [222], "a prefix sibling directory must not be swept"

def test_checkout_root_does_not_follow_the_caller(monkeypatch) -> None:
    root = demo_fleet._checkout_root()
    monkeypatch.chdir("/")
    assert demo_fleet._checkout_root() == root

# ── advisory suggestions (phase A: mapping only, nothing executes) ─────────

def _suggestions(ticks: list[fleet.tick]) -> list[advice.suggestion]:
    observation = _observation(ticks)
    verdicts = fleet.evaluate(observation)
    return advice.suggest(verdicts, observation)

def test_ok_fleet_has_no_suggestion() -> None:
    assert _suggestions([fleet.tick(index = 0, runs = (_run((_worker(),)),))]) == []

def test_crash_loop_suggests_restart() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(restart_count = 0),)),)), fleet.tick(index = 1, runs = (_run((_worker(restart_count = 2),)),))]
    rows = _suggestions(ticks)
    assert [row.action for row in rows] == ["restart"]
    assert rows[0].confidence == advice.CONFIDENCE_HIGH
    assert "B1" in rows[0].reason
    assert rows[0].evidence["verdict"]["code"] == "B1"

def test_drain_stalled_suggests_drain() -> None:
    ticks = [fleet.tick(index = index, runs = (_run((_worker(),), draining = True, drain_kind = "graceful"),)) for index in range(2)]
    rows = _suggestions(ticks)
    assert [row.action for row in rows] == ["drain"]
    assert rows[0].evidence["worker"]["draining"] is True

def test_silent_hang_suggests_restart_and_drain_when_already_draining() -> None:
    def hang_ticks(*, draining: bool) -> list[fleet.tick]:
        ticks = []

        for index in range(3):
            tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 600),), draining = draining, drain_kind = "graceful" if draining else None),), probe_ok = False)
            tick.log_digests[("run-1", "app")] = "deadbeef"
            ticks.append(tick)

        return ticks

    assert [row.action for row in _suggestions(hang_ticks(draining = False))] == ["restart"]
    assert [row.action for row in _suggestions(hang_ticks(draining = True))] == ["drain"]

def test_spawn_failure_suggests_notify_not_restart() -> None:
    ticks = [fleet.tick(index = 0, runs = (_run((_worker(restart_count = 0, last_error = "spawn failed: nope"),)),)), fleet.tick(index = 1, runs = (_run((_worker(restart_count = 1, last_error = "spawn failed: nope"),)),))]
    assert [row.action for row in _suggestions(ticks)] == ["notify"]

def test_suspicious_results_suggest_notify() -> None:
    ticks = []

    for index in range(3):
        tick = fleet.tick(index = index, runs = (_run((_worker(uptime_secs = 100 + index),)),))
        tick.log_digests[("run-1", "app")] = "deadbeef"
        ticks.append(tick)

    rows = _suggestions(ticks)
    assert [row.action for row in rows] == ["notify"]
    assert rows[0].confidence == advice.CONFIDENCE_LOW

def test_unevaluated_observes_and_unavailable_observes() -> None:
    observation = _observation([fleet.tick(index = 0, runs = (_run((_worker(),)),))])
    observation.note_unevaluated(("A1", "C1"), "run-1/app", "no --probe-url")
    rows = advice.suggest(fleet.evaluate(observation), observation)
    assert [row.action for row in rows] == ["observe"]
    assert advice.unavailable("boom").action == "observe"

# ── zero actions (structural) ──────────────────────────────────────────────

def test_allowlist_is_exactly_the_three_read_tools() -> None:
    assert READ_ONLY_TOOLS == {"malkuth_status", "malkuth_logs", "malkuth_probe"}

def test_mutating_tool_names_are_refused_by_an_open_session(monkeypatch) -> None:
    """The guard must bite on a live session, before any byte reaches the server.

    A closed session would raise a *transport* error instead, which is why the old shape
    of this test passed even with the allowlist deleted. Here the session is open, the
    exception type is the allowlist error (not its `mcp_unavailable` subclass), and the
    write path is spied to prove nothing was sent.
    """

    with mcp_session([sys.executable, str(FAKE_SERVER), "healthy"]) as session:
        sent: list[dict] = []
        original = session._write
        monkeypatch.setattr(session, "_write", lambda message: sent.append(message) or original(message))

        with pytest.raises(mcp_error) as caught:
            session.call_tool("malkuth_" + "restart", {})

        assert type(caught.value) is mcp_error
        assert "allowlist" in str(caught.value)
        assert sent == [], "an action tool name must never reach the server"

def _literal_string(node: ast.AST) -> str | None:
    """Resolve a string the compiler would fold: a literal or its ``+`` concatenation."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)

        if (left is not None) and (right is not None):
            return left + right

    return None

def _action_call_sites(path: Path, forbidden: set[str]) -> list[tuple[str, int, str]]:
    """AST-level scan: which calls pass an action tool name as a literal argument.

    A text scan would flag docstrings that *explain* the exception; the contract is about
    call sites, and this covers a future `judge/actions.py` calling
    ``call_tool("malkuth_restart")`` just as well.
    """
    tree = ast.parse(path.read_text(encoding = "utf-8"))
    hits = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        else:
            continue

        if callee not in ("call_tool", "_request"):
            continue

        for arg in node.args:
            if isinstance(arg, ast.Dict):
                values = list(arg.values)
            else:
                values = [arg]

            for value in values:
                literal = _literal_string(value)

                if (literal is not None) and (literal in forbidden):
                    hits.append((path.name, node.lineno, literal))

    return hits

def test_the_judgement_path_contains_no_action_call() -> None:
    forbidden = {"malkuth_" + "restart", "malkuth_" + "drain", "malkuth_" + "supervise"}
    modules = [path for path in JUDGE_SOURCES.rglob("*.py") if path.name != "demo_fleet.py"]
    assert len(modules) >= 4, "the glob must actually see the package"
    offenders = [hit for path in modules for hit in _action_call_sites(path, forbidden)]
    assert offenders == [], f"the judgement path must not call action tools: {offenders}"

def test_every_module_outside_the_demo_holds_no_action_literal() -> None:
    """Text-level backstop: a mutating action name anywhere but the demo is a smell."""
    forbidden = ("malkuth_" + "restart", "malkuth_" + "drain")

    for path in [path for path in JUDGE_SOURCES.rglob("*.py") if path.name != "demo_fleet.py"]:
        text = path.read_text()
        assert not any(needle in text for needle in forbidden), f"{path.name} mentions an action tool"

def test_the_demo_module_holds_exactly_one_action() -> None:
    """The bring-your-own-fleet demo is the single exception, and it is supervision only."""
    text = (JUDGE_SOURCES / "demo_fleet.py").read_text()
    assert ("malkuth_" + "supervise") in text
    assert ("malkuth_" + "restart") not in text
    assert ("malkuth_" + "drain") not in text

# ── CLI end-to-end against the fixture server ──────────────────────────────

def test_healthy_fleet_with_readiness_reports_health(capsys) -> None:
    code, out, _ = _judge("healthy", capsys, "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "结论: 健康" in out
    assert ("run-1/app" in out) and ("phase=running" in out)
    code, out, _ = _judge("healthy", capsys, "--probe-url", "http://127.0.0.1:9", "--json")
    assert json.loads(out)["status"] == "ok"

def test_missing_readiness_is_not_health(capsys) -> None:
    """No probe URL means A1/C1 were never evaluated — the report must say so."""
    code, out, _ = _judge("healthy", capsys)
    assert code == 0
    assert "结论: 未判定" in out
    assert "未评估 A1,C1" in out
    assert "结论: 健康" not in out
    code, out, _ = _judge("healthy", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "indeterminate", payload
    assert payload["verdicts"] == []
    assert any(entry["codes"] == ["A1", "C1"] for entry in payload["unevaluated"])

def test_probe_transport_failure_is_unevaluated(capsys) -> None:
    code, out, _ = _judge("probefail", capsys, "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "结论: 未判定" in out
    assert "未评估 A1,C1" in out

def test_log_read_failure_is_unevaluated(capsys) -> None:
    code, out, _ = _judge("logfail", capsys, "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "结论: 未判定" in out
    assert "未评估 A2,C1" in out

def test_crash_loop_is_reported(capsys) -> None:
    code, out, _ = _judge("crashloop", capsys)
    assert code == 0
    assert ("B1" in out) and ("critical" in out)
    assert "结论: 失败" in out

def test_a_finding_is_never_status_ok(capsys) -> None:
    """Both signals present: the crash loop is a finding, so `ok` would be a lie."""
    code, out, _ = _judge("crashloop", capsys, "--probe-url", "http://127.0.0.1:9", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["verdicts"], payload
    assert payload["status"] == "failed", payload

def test_crash_loop_report_carries_a_restart_suggestion(capsys) -> None:
    code, out, _ = _judge("crashloop", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert [row["action"] for row in payload["suggested_actions"]] == ["restart"]
    assert payload["suggested_actions"][0]["confidence"] == "high"
    assert payload["suggested_actions"][0]["evidence"]["verdict"]["code"] == "B1"

def test_drain_stalled_report_suggests_drain(capsys) -> None:
    code, out, _ = _judge("drainstalled", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert [row["action"] for row in payload["suggested_actions"]] == ["drain"]
    assert payload["suggested_actions"][0]["evidence"]["worker"]["draining"] is True

def test_healthy_report_suggests_nothing(capsys) -> None:
    code, out, _ = _judge("healthy", capsys, "--probe-url", "http://127.0.0.1:9", "--json")
    assert code == 0
    assert json.loads(out)["suggested_actions"] == []

def test_unavailable_report_suggests_observe(capsys) -> None:
    code, out, _ = _judge("badshape", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "unavailable"
    assert [row["action"] for row in payload["suggested_actions"]] == ["observe"]

def test_silent_hang_report_suggests_restart(capsys) -> None:
    code, out, _ = _judge("hang", capsys, "--probe-url", "http://127.0.0.1:9", "--hang-uptime", "1", "--json")
    assert code == 0
    payload = json.loads(out)
    assert [row["action"] for row in payload["suggested_actions"]] == ["restart"]
    assert payload["suggested_actions"][0]["evidence"]["verdict"]["code"] in ("C1", "A1")

def test_the_judge_never_calls_an_action_tool_at_runtime(monkeypatch) -> None:
    """Runtime companion to the AST guard: only the three read tools ever go out."""
    sent: list[str] = []
    original = mcp_session.call_tool

    def spy(session, name, arguments = None, *, timeout = None):
        sent.append(name)
        return original(session, name, arguments, timeout = timeout)

    monkeypatch.setattr(mcp_session, "call_tool", spy)

    with mcp_session([sys.executable, str(FAKE_SERVER), "crashloop"], timeout = 5.0) as session:
        observation = fleet.observation()
        failure = cli._collect(session, observation, ticks = 3, interval = 0.01, logs_lines = 10)
        assert failure is None
        rows = advice.suggest(fleet.evaluate(observation), observation)
        assert rows and (rows[0].action == "restart")

    assert set(sent) <= READ_ONLY_TOOLS, f"the judge called a non-read tool: {sent}"

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

def test_unavailable_report_has_the_same_schema(capsys) -> None:
    code, out, _ = _judge("badshape", capsys, "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "unavailable"

    for key in ("ticks", "interval_secs", "runs", "workers", "verdicts", "unevaluated", "cannot_judge"):
        assert key in payload, key

    assert (payload["verdicts"] == []) and (payload["unevaluated"] == [])

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
    assert (payload["status"] == "failed") and (payload["read_only"] is True)
    assert (payload["runs"] == 1) and (payload["workers"] == 1)
    assert any(verdict["code"] == "B1" for verdict in payload["verdicts"])
    assert any(entry["codes"] == ["A1", "C1"] for entry in payload["unevaluated"])
    assert payload["reserved_predicates"]

def test_supervise_demo_path_judges_the_fleet_it_brings(capsys) -> None:
    code, out, _ = _judge("healthy", capsys, "--supervise", "sleep 30", "--probe-url", "http://127.0.0.1:9")
    assert code == 0
    assert "demo-0" in out, out
    assert "结论: 健康" in out

def test_action_mode_is_refused(capsys) -> None:
    code, _, err = _run_cli(["--no-read-only"], capsys)
    assert code == 2
    assert "target-bound approval" in err

def test_single_tick_is_refused(capsys) -> None:
    code, _, err = _run_cli(["--ticks", "1"], capsys)
    assert code == 2
    assert "at least 2" in err

# ── real malkuth acceptance ────────────────────────────────────────────────

def _require_real_malkuth() -> tuple[str, str | None]:
    malkuth_bin = _malkuth_bin()

    if malkuth_bin is None:
        pytest.skip("no malkuth binary available")

    if not _advertises_read_arms(malkuth_bin):
        pytest.skip("installed malkuth predates the wave-4 read arms")

    return malkuth_bin, _test_app_bin(malkuth_bin)

def _live_pods(program: str) -> list[int]:
    """Pids whose executable is exactly ``program`` (the demo's own grandchildren)."""
    found = []

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue

        try:
            argv = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue

        if argv and (os.path.realpath(argv[0]) == os.path.realpath(program)):
            found.append(int(entry.name))

    return found

def _assert_port_closed(port: int, *, timeout: float = 5.0) -> None:
    """The demo's supervised server must be gone once the CLI returns."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return

        time.sleep(0.2)

    raise AssertionError(f"the demo left its supervised server listening on port {port}")

def _assert_no_pods_left(program: str, *, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout

    while (time.time() < deadline) and _live_pods(program):
        time.sleep(0.2)

    assert _live_pods(program) == [], f"the demo left supervised processes behind: {_live_pods(program)}"

def _advertises_read_arms(malkuth_bin: str) -> bool:
    with mcp_session([malkuth_bin, "mcp"], timeout = 10.0) as session:
        return READ_ONLY_TOOLS <= session.tools

def test_supervise_demo_healthy_fleet_has_no_critical_verdict(capsys) -> None:
    """Acceptance ① (healthy), through the PUBLIC --supervise path on a real malkuth."""
    malkuth_bin, test_app = _require_real_malkuth()

    if test_app is None:
        pytest.skip("test_app example binary not available next to the malkuth binary")

    port = _free_port()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", f"{test_app} supervise --pods 1 --port-base {port}", "--ticks", "3", "--interval", "0.4", "--json"], capsys)
    _assert_no_pods_left(test_app)
    assert code == 0, out
    payload = json.loads(out)
    assert (payload["runs"] >= 1) and (payload["workers"] >= 1), payload
    assert [verdict for verdict in payload["verdicts"] if verdict["severity"] == "critical"] == [], payload
    # No HTTP readiness surface on the example app: A1/C1 must be reported as unevaluated,
    # not silently counted as healthy.
    assert payload["status"] == "indeterminate"
    assert any(entry["codes"] == ["A1", "C1"] for entry in payload["unevaluated"])

def test_supervise_demo_with_readiness_reports_health(capsys, tmp_path) -> None:
    """Acceptance ① (healthy + readiness): every predicate evaluated, nothing fired."""
    malkuth_bin, _ = _require_real_malkuth()
    (tmp_path / "healthz").write_text("ok", encoding = "utf-8")
    (tmp_path / "readyz").write_text("ok", encoding = "utf-8")
    port = _free_port()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", f"{sys.executable} -m http.server {port} --directory {tmp_path}", "--probe-url", f"http://127.0.0.1:{port}", "--ticks", "3", "--interval", "0.4", "--json"], capsys)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["status"] == "ok", payload
    assert payload["verdicts"] == []
    assert payload["unevaluated"] == []
    _assert_port_closed(port)

def test_supervise_demo_crash_loop_is_reported(capsys) -> None:
    """Acceptance ① (crash loop), through the PUBLIC --supervise path on a real malkuth."""
    malkuth_bin, _ = _require_real_malkuth()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", "sh -c 'exit 7'", "--supervise-timeout", "6", "--ticks", "3", "--interval", "0.4", "--json"], capsys)
    assert code == 0, out
    payload = json.loads(out)
    codes = {verdict["code"] for verdict in payload["verdicts"]}
    assert ("B1" in codes) or ("C2" in codes), payload
    assert any(verdict["severity"] == "critical" for verdict in payload["verdicts"])

def test_supervise_crash_loop_suggests_restart_without_acting(capsys) -> None:
    """Acceptance ① (crash loop): the suggestion is advisory and the run is untouched."""
    malkuth_bin, _ = _require_real_malkuth()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", "sh -c 'exit 7'", "--supervise-timeout", "4", "--ticks", "3", "--interval", "0.4", "--json"], capsys)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["status"] == "failed"
    assert [row["action"] for row in payload["suggested_actions"]] == ["restart"]
    evidence = payload["suggested_actions"][0]["evidence"]
    assert evidence["verdict"]["code"] == "B1"
    assert evidence["worker"]["draining"] is False, "nothing drained it"

def test_supervise_silent_hang_suggests_restart_without_acting(capsys) -> None:
    """Acceptance ① (alive-but-not-ready): C1 fires, the worker keeps its identity."""
    malkuth_bin, _ = _require_real_malkuth()
    port = _free_port()
    unready = HERE / "fake_unready_server.py"
    assert unready.exists()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", f"{sys.executable} {unready} {port}", "--probe-url", f"http://127.0.0.1:{port}", "--probe-timeout", "2", "--probe-wait", "8", "--hang-uptime", "1", "--stall-ticks", "2", "--ticks", "3", "--interval", "0.5", "--json"], capsys)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["status"] == "failed"
    assert [row["action"] for row in payload["suggested_actions"]] == ["restart"]
    evidence = payload["suggested_actions"][0]["evidence"]
    assert evidence["verdict"]["code"] == "C1"
    assert evidence["worker"]["restart_count"] == 0, "nothing restarted it"
    assert evidence["worker"]["draining"] is False, "nothing drained it"

def test_supervise_drain_stall_is_not_producible_through_the_public_path(capsys) -> None:
    """Acceptance ① (drain stalled): documented boundary, not silently skipped.

    malkuth's supervise deadline does ``begin_drain(Graceful)`` and then
    ``fleet.stop_all()`` immediately, so a drain never *stays* stalled through this path:
    the status goes ``draining: true, finished: true`` inside one tick. B3's mapping is
    therefore covered by the fixture server and the unit tests, and this test pins the
    reason so a future change (a real graceful drain) is noticed.
    """
    malkuth_bin, _ = _require_real_malkuth()
    code, out, _ = _run_cli(["--mcp-cmd", f"{malkuth_bin} mcp", "--supervise", f"{sys.executable} -c 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'", "--supervise-timeout", "2", "--ticks", "6", "--interval", "0.8", "--drain-ticks", "2", "--json"], capsys)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["status"] in ("failed", "indeterminate"), payload
    codes = {verdict["code"] for verdict in payload["verdicts"]}
    assert "B3" not in codes, "a stalled drain should not be observable through the supervise deadline any more"

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
