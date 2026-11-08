"""``judge-fleet act``: the executor's checklist, gate branches and zero-scepter contract.

Layers:

* pure preflight units (bundle shape, action support, justification, identity) — no I/O;
* CLI end-to-end against ``tests/fake_mcp_server.py`` action modes — one mode per gate
  answer and per identity failure, plus a call log that proves ``--dry-run`` writes nothing;
* the structural guard: the executor module is the only place allowed to name an action
  tool, and its names are exactly :data:`ACTION_TOOLS`.
"""

from __future__ import annotations
import ast
import io
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
import pytest
from celestia_devtools.judge import act, advice, cli, demo_fleet
from celestia_devtools.judge.mcp_stdio import ACTION_TOOLS, READ_ONLY_TOOLS, action_session, mcp_error, mcp_session

HERE = Path(__file__).resolve().parent
FAKE_SERVER = HERE / "fake_mcp_server.py"
JUDGE_SOURCES = Path(cli.__file__).resolve().parent
GATE = "0" * 32

def _bundle(**overrides) -> dict:
    bundle = {"proposal_id": "judge:0190f0f0-0000-7000-8000-000000000001", "gate_proposal_id": GATE, "run_id": "run-1", "worker_id": "app", "action": "restart", "verdict_code": "B1", "codes": ["B1"], "severity": "critical", "pid": 4242, "starttime": 99, "report_status": "failed", "expires_at": 2_000_000_000}
    bundle.update(overrides)
    return bundle

def _cmd(mode: str) -> str:
    return f"{sys.executable} {FAKE_SERVER} {mode}"

def _run(mode: str, capsys, *, bundle: dict | None = None, argv: list[str] | None = None) -> tuple[int, dict, str]:
    extra = list(argv or [])
    code = cli.main(["act", "--handoff", "-", "--json", "--mcp-cmd", _cmd(mode), *extra])
    out = capsys.readouterr().out
    document = json.loads(out) if out.strip() else {}
    return code, document, out

def _stdin(monkeypatch, bundle: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bundle)))

# ── pure preflight ─────────────────────────────────────────────────────────

def test_a_complete_bundle_parses() -> None:
    bundle = act.parse_handoff(_bundle())
    assert bundle.gate_proposal_id == GATE
    assert bundle.codes == ("B1",)
    assert (bundle.pid, bundle.starttime) == (4242, 99)

@pytest.mark.parametrize("missing", ["proposal_id", "gate_proposal_id", "run_id", "worker_id", "action", "verdict_code", "codes"])
def test_a_missing_field_is_bundle_invalid(missing: str) -> None:
    payload = _bundle()
    payload.pop(missing)

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(payload)

    assert caught.value.code == "bundle_invalid"
    assert missing in caught.value.message

def test_a_missing_severity_is_refused() -> None:
    payload = _bundle()
    payload.pop("severity")

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(payload)

    assert caught.value.code == "bundle_invalid"
    assert "severity" in caught.value.message

@pytest.mark.parametrize("token", ["A" * 32, "abc", "g" * 32, "0" * 31, "0" * 33, "judge:{}".format("0" * 32)])
def test_a_malformed_gate_token_is_refused(token: str) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(gate_proposal_id = token))

    assert caught.value.code == "bundle_invalid"
    assert ("gate_proposal_id" in caught.value.message)

@pytest.mark.parametrize("proposal_id", ["0190f0f0-0000-7000-8000-000000000001", "gate:abc"])
def test_a_proposal_id_without_its_prefix_is_refused(proposal_id: str) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(proposal_id = proposal_id))

    assert caught.value.code == "bundle_invalid"
    assert "judge:" in caught.value.message

def test_a_bundle_without_a_deadline_cannot_prove_its_approval_live() -> None:
    payload = _bundle()
    payload.pop("expires_at")

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(payload)

    assert caught.value.code == "window_expired"
    assert "expires_at" in caught.value.hint

@pytest.mark.parametrize("payload", [None, [], "text", 3])
def test_a_non_object_bundle_is_refused(payload) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(payload)

    assert caught.value.code == "bundle_invalid"

@pytest.mark.parametrize("codes", [[], "B1", [""], [1]])
def test_codes_must_be_a_non_empty_string_list(codes) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(codes = codes))

    assert caught.value.code == "bundle_invalid"

def test_expires_at_accepts_seconds_and_rfc3339() -> None:
    assert act.parse_handoff(_bundle(expires_at = 2_000_000_000)).expires_at == 2_000_000_000.0
    assert act.parse_handoff(_bundle(expires_at = "2030-01-01T00:00:00Z")).expires_at > 0

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(expires_at = "not a time"))

    assert caught.value.code == "bundle_invalid"

def test_an_empty_action_is_a_shape_error() -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(action = ""))

    assert caught.value.code == "bundle_invalid"

@pytest.mark.parametrize("action", ["supervise", "notify", "RESTART-ALL"])
def test_only_restart_and_drain_are_supported(action: str) -> None:
    bundle = act.parse_handoff(_bundle(action = action))

    with pytest.raises(act.act_error) as caught:
        act.check_action_supported(bundle)

    assert caught.value.code == "action_not_supported"

def test_an_unavailable_report_is_never_permission() -> None:
    bundle = act.parse_handoff(_bundle(report_status = "unavailable"))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "action_not_justified"
    assert "unavailable" in caught.value.hint

@pytest.mark.parametrize("status", ["ok", "indeterminate"])
def test_a_non_failed_report_is_refused(status: str) -> None:
    bundle = act.parse_handoff(_bundle(report_status = status))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "action_not_justified"

def test_a_suspicious_finding_is_not_actionable() -> None:
    bundle = act.parse_handoff(_bundle(severity = "suspicious"))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "action_not_justified"

def test_the_codes_must_contain_the_justifying_code() -> None:
    bundle = act.parse_handoff(_bundle(verdict_code = "C1", codes = ["B1"]))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "action_not_justified"
    assert "C1" in caught.value.message

def test_a_spawn_failure_is_notify_only() -> None:
    bundle = act.parse_handoff(_bundle(verdict_code = "C2", codes = ["C2"], action = "restart"))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "action_not_justified"

def test_a_closed_window_is_refused() -> None:
    bundle = act.parse_handoff(_bundle(expires_at = 1.0))

    with pytest.raises(act.act_error) as caught:
        act.check_justification(bundle)

    assert caught.value.code == "window_expired"

def test_the_priority_table_maps_codes_to_actions() -> None:
    assert act.expected_action(act.parse_handoff(_bundle())) == advice.ACTION_RESTART
    assert act.expected_action(act.parse_handoff(_bundle(verdict_code = "B3", codes = ["B3"], action = "drain"))) == advice.ACTION_DRAIN
    # C1 is a restart unless the run is already draining, where it escalates to drain.
    c1 = act.parse_handoff(_bundle(verdict_code = "C1", codes = ["C1"]))
    assert act.expected_action(c1) == advice.ACTION_RESTART
    assert act.expected_action(c1, draining = True) == advice.ACTION_DRAIN

# ── identity ───────────────────────────────────────────────────────────────

def test_identity_is_missing_when_the_bundle_has_no_pid_or_starttime() -> None:
    bundle = act.parse_handoff(_bundle(pid = None, starttime = None))
    target = act.live_target(pid = 4242, starttime = 99, phase = "running", draining = False)

    with pytest.raises(act.act_error) as caught:
        act.check_target(bundle, target)

    assert caught.value.code == "identity_missing"

def test_a_changed_identity_is_refused() -> None:
    bundle = act.parse_handoff(_bundle())
    target = act.live_target(pid = 4242, starttime = 100, phase = "running", draining = False)

    with pytest.raises(act.act_error) as caught:
        act.check_target(bundle, target)

    assert caught.value.code == "target_changed"

def test_a_gone_target_is_refused() -> None:
    bundle = act.parse_handoff(_bundle())
    target = act.live_target(pid = 4242, starttime = 99, phase = "stopped", draining = False)

    with pytest.raises(act.act_error) as caught:
        act.check_target(bundle, target)

    assert caught.value.code == "target_gone"

def test_an_action_that_contradicts_the_priority_table_is_refused() -> None:
    # B3 says drain; a restart of that worker must not slip through.
    bundle = act.parse_handoff(_bundle(verdict_code = "B3", codes = ["B3"], action = "restart"))
    target = act.live_target(pid = 4242, starttime = 99, phase = "running", draining = True)

    with pytest.raises(act.act_error) as caught:
        act.check_target(bundle, target)

    assert caught.value.code == "action_not_justified"

# ── CLI end-to-end against the fixture server ──────────────────────────────

def test_a_restart_bundle_executes(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_OK
    assert document["status"] == "executed"
    assert document["malkuth_code"] == "accepted"
    assert document["writes"] == "none"
    assert document["executed_at"].endswith("Z")

def test_a_drain_bundle_executes(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle(action = "drain", verdict_code = "B3", codes = ["B3"]))
    code, document, _ = _run("act-drain-ok", capsys)
    assert code == act.EXIT_OK
    assert document["status"] == "executed"

def test_an_unregistered_approval_is_refused_with_a_re_run_hint(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-required", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "approval_required"
    assert "re-run act" in document["hint"]

def test_a_spent_approval_needs_a_new_ruling(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-consumed", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "approval_consumed"
    assert "new approval" in document["hint"]

def test_an_approval_for_the_other_action_is_a_gate_refusal(monkeypatch, capsys) -> None:
    """The action-bound refusal must classify as a gate refusal (exit 3), not an
    execution failure — the gate refused, the executor did not break."""

    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-mismatch", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "approval_action_mismatch"
    assert "different action" in document["hint"]

def test_a_blocked_pair_can_only_be_unblocked_by_an_operator(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-blocked", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "approval_blocked"
    assert "allow" in document["hint"]

def test_an_expired_gate_window_needs_re_approval(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-expired", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "approval_expired"

def test_a_recycled_pid_is_refused_before_any_action(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-changed", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "target_changed"

def test_a_live_worker_without_starttime_cannot_be_matched(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-no-starttime", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "target_changed"

def test_an_identity_less_bundle_is_refused(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle(pid = None, starttime = None))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "identity_missing"

def test_a_dry_run_registers_and_executes_nothing(monkeypatch, capsys, tmp_path) -> None:
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_MCP_CALL_LOG", str(log))
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-ok", capsys, argv = ["--dry-run"])
    assert code == act.EXIT_OK
    assert document["status"] == "dry_run"
    assert document["would_call"]["run_id"] == "run-1"
    calls = [json.loads(line)["name"] for line in log.read_text().splitlines()]
    assert calls == ["malkuth_status"], f"a dry run must not approve or execute: {calls}"

def test_a_real_run_calls_status_approve_execute(monkeypatch, capsys, tmp_path) -> None:
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_MCP_CALL_LOG", str(log))
    _stdin(monkeypatch, _bundle())
    code, _, _ = _run("act-ok", capsys)
    assert code == act.EXIT_OK
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == ["malkuth_status", "malkuth_approve", "malkuth_restart"]
    # The gate registration must name the pair AND the action it authorizes —
    # neither the decision nor the action may be defaulted (a stale executor
    # that omits them is refused by the gate, not silently waved through).
    approve_args = entries[1]["arguments"]
    assert approve_args["decision"] == "allow"
    assert approve_args["action"] == "restart", "the approval must be bound to the executed action"

def test_a_stale_approve_without_action_or_decision_is_refused(monkeypatch, capsys, tmp_path) -> None:
    """Seam control: the fake gate must refuse an approval that defaults the
    required fields, exactly like the real malkuth gate does."""

    from celestia_devtools.judge.mcp_stdio import action_session

    with action_session(["python3", "-m", "tests.fake_mcp_server", "act-ok"]) as session:
        missing_action = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "allow"})
        assert missing_action["code"] == "approval_action_required"
        missing_decision = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "action": "restart"})
        assert missing_decision["code"] == "approval_decision_required"
        unknown_action = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "allow", "action": "hibernate"})
        assert unknown_action["code"] == "approval_action_required"
        bound = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "allow", "action": "drain"})
        assert bound["ok"] is True
        mismatch = session.call_tool("malkuth_restart", {"run_id": "run-1", "worker_id": "w", "proposal_id": "p"})
        assert mismatch["code"] == "approval_action_mismatch"

def test_a_file_handoff_works_without_stdin(monkeypatch, capsys, tmp_path) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle()), encoding = "utf-8")
    code = cli.main(["act", "--handoff", str(path), "--json", "--mcp-cmd", _cmd("act-ok")])
    assert code == act.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "executed"

def test_invalid_json_is_a_usage_clear_refusal(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_REFUSED
    assert document["code"] == "bundle_invalid"

def test_human_output_names_the_three_phases(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code = cli.main(["act", "--handoff", "-", "--mcp-cmd", _cmd("act-ok")])
    out = capsys.readouterr().out
    assert code == act.EXIT_OK
    assert "EXECUTED restart run-1/app" in out
    assert ("[approve]" in out) and ("[execute]" in out)
    assert "写入: 无" in out

def test_the_executor_refuses_a_server_without_the_gate(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("healthy", capsys)
    assert code == act.EXIT_UNAVAILABLE, "a server without the gate is unavailable, not a refusal"
    assert document["code"] == "malkuth_unavailable"
    assert document["status"] == "failed"

# ── demo switch: --supervise ───────────────────────────────────────────────

def test_the_demo_switch_acts_on_the_fleet_it_brings(monkeypatch, capsys) -> None:
    """`--supervise` starts a fleet, binds the bundle to it and executes there."""

    # The demo bundle authorises the action only: the target identity comes from the
    # fleet this CLI just started.
    payload = _bundle()

    for field in ("run_id", "worker_id", "pid", "starttime"):
        payload.pop(field)

    _stdin(monkeypatch, payload)
    code, document, _ = _run("act-ok", capsys, argv = ["--supervise", "sleep 30", "--supervise-timeout", "1"])

    assert code == act.EXIT_OK, document
    assert document["status"] == "executed"
    assert document["target"] == {"run_id": "run-1", "worker_id": "demo-0"}

def test_without_the_demo_switch_a_missing_fleet_is_still_target_unknown(monkeypatch, capsys) -> None:
    """No `--supervise` means no fleet: the existing refusal is unchanged."""

    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-empty", capsys)

    assert code == act.EXIT_REFUSED
    assert document["code"] == "target_unknown"
    message = document["message"]
    assert ("supervising session" in message) or ("per-process" in message)

def test_the_demo_switch_reports_a_supervise_failure(monkeypatch, capsys) -> None:
    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("healthy", capsys, argv = ["--supervise", "sleep 30"])

    assert code == act.EXIT_UNAVAILABLE
    assert document["code"] == "malkuth_unavailable"

# ── structural contract ────────────────────────────────────────────────────

def test_the_action_allowlist_is_exactly_the_four_gate_tools() -> None:
    assert ACTION_TOOLS == {"malkuth_status", "malkuth_approve", "malkuth_restart", "malkuth_drain"}
    assert READ_ONLY_TOOLS == {"malkuth_status", "malkuth_logs", "malkuth_probe"}

def test_the_judgement_session_still_refuses_an_action_tool() -> None:
    with mcp_session([sys.executable, str(FAKE_SERVER), "healthy"], timeout = 10) as session:
        with pytest.raises(mcp_error) as caught:
            session.call_tool("malkuth_" + "restart", {})

    assert "allowlist" in str(caught.value)

def test_the_action_session_refuses_a_read_only_tool_it_does_not_hold() -> None:
    with action_session([sys.executable, str(FAKE_SERVER), "act-ok"], timeout = 10) as session:
        with pytest.raises(mcp_error) as caught:
            session.call_tool("malkuth_logs", {})

    assert "allowlist" in str(caught.value)

def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)

        if (left is not None) and (right is not None):
            return left + right

    return None

def _tool_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding = "utf-8"))
    names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")

            if callee not in ("call_tool", "_request"):
                continue

            if callee == "call_tool":
                # The tool name is the first positional argument; later arguments are its
                # payload (a `{"decision": "allow"}` value is not a tool name).
                candidates = node.args[:1]
            else:
                # `_request(method, params)` carries the tool name under "name".
                candidates = []

                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        candidates.extend(value for key, value in zip(arg.keys, arg.values, strict = False) if _literal_string(key) == "name")

            for candidate in candidates:
                literal = _literal_string(candidate)

                if literal is not None:
                    names.add(literal)

    return names

def test_the_executor_names_only_its_own_tool_allowlist() -> None:
    literals = _tool_literals(JUDGE_SOURCES / "act.py")
    assert literals, "the executor must call tools through an allowlist, and this pin must see them"
    assert literals <= ACTION_TOOLS, f"the executor named a tool outside ACTION_TOOLS: {sorted(literals - ACTION_TOOLS)}"

def test_the_judgement_modules_name_no_action_tool() -> None:
    forbidden = ACTION_TOOLS - {"malkuth_status"}

    for name in ("cli.py", "fleet.py", "advice.py", "mcp_stdio.py"):
        literals = _tool_literals(JUDGE_SOURCES / name)
        assert not (literals & forbidden), f"{name} must not call {sorted(literals & forbidden)}"

def test_the_demo_switch_leaves_no_supervised_process_behind(tmp_path) -> None:
    """The demo switch owns the fleet it brings: after the act, nothing of it runs on."""

    binary = _require_gate_binary()
    counter = tmp_path / "crash-counter"
    # Two crashes then a long run: the demo is a real (short) crash loop that settles, and
    # the teardown must remove the settled child.
    command = [["sh", "-c", 'n=$(cat "$1" 2>/dev/null || echo 0); echo $((n + 1)) > "$1"; if [ "$n" -lt 2 ]; then exit 1; fi; sleep 60', "sh", str(counter)]]
    session = action_session([binary, "mcp"], timeout = 10)

    with session:
        with demo_fleet.supervision(session, command, timeout_secs = 5, max_restarts = 10) as demo:
            assert demo.run_seen, demo.error
            bound = act.bind_to_supervised_fleet(_bundle_missing_target(), session)
            bundle = act.parse_handoff(bound)
            act.check_action_supported(bundle)
            act.check_justification(bundle)
            outcome = act.run_act(bundle, session)
            assert outcome["status"] == act.STATUS_EXECUTED, outcome
            supervised_pids = list(demo.worker_pids)

    deadline = time.time() + 10

    while time.time() < deadline:
        survivors = [pid for pid in supervised_pids if _alive(pid)]

        if not survivors:
            break

        time.sleep(0.1)

    assert not [pid for pid in supervised_pids if _alive(pid)], "the demo fleet must not outlive act"

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True

def _bundle_missing_target() -> dict:
    payload = _bundle()

    for field in ("run_id", "worker_id", "pid", "starttime"):
        payload.pop(field)

    return payload

# ── real malkuth acceptance ────────────────────────────────────────────────

def _malkuth_bin() -> str | None:
    candidate = os.environ.get("JUDGE_MALKUTH_BIN") or shutil.which("malkuth")

    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate

    return None

def _require_gate_binary() -> str:
    """The real acceptance needs a malkuth that advertises the gate; older ones skip."""

    binary = _malkuth_bin()

    if binary is None:
        pytest.skip("no malkuth binary available")

    with mcp_session([binary, "mcp"], timeout = 10) as session:
        if not ACTION_TOOLS <= set(session.tools):
            pytest.skip(f"malkuth at {binary} predates the action gate ({sorted(session.tools)})")

    return binary

def _running_target(session: action_session, *, deadline: float = 15.0) -> act.live_target:
    """Wait for the supervised worker to be running with a whole identity."""

    end = time.time() + deadline

    while time.time() < end:
        status = session.call_tool("malkuth_status")
        runs = status.get("runs") or []

        for run in runs:
            for worker in run.get("workers") or []:
                if (worker.get("phase") == "running") and worker.get("pid") and worker.get("starttime"):
                    return act.live_target(pid = worker["pid"], starttime = worker["starttime"], phase = "running", draining = bool(run.get("draining")))

        time.sleep(0.05)

    raise AssertionError("the supervised worker never reached a running state with pid+starttime")

def test_a_real_malkuth_restart_executes_through_the_gate() -> None:
    """The executor against a real gate: supervise, act, and read the tool's own answer."""

    binary = _require_gate_binary()
    session = action_session([binary, "mcp"], timeout = 10)
    supervised: dict = {}

    with session:
        def drive() -> None:
            try:
                supervised["payload"] = demo_fleet.start_supervision(session, [["sleep", "60"]], timeout_secs = 8, max_restarts = 0)
            except Exception as error:  # noqa: BLE001 - surfaced by the assertion after join
                supervised["error"] = repr(error)

        thread = threading.Thread(target = drive, name = "act-real-supervise", daemon = True)
        thread.start()
        run_id = None

        for _ in range(100):
            status = session.call_tool("malkuth_status")

            if status.get("runs"):
                run_id = status["runs"][0]["run_id"]
                break

            time.sleep(0.05)

        assert run_id, "the supervision never appeared in the registry"
        target = _running_target(session)
        bundle = act.parse_handoff({"proposal_id": "judge:real-1", "gate_proposal_id": "a" * 32, "run_id": run_id, "worker_id": "demo-0", "action": "restart", "verdict_code": "B1", "codes": ["B1"], "severity": "critical", "pid": target.pid, "starttime": target.starttime, "report_status": "failed", "expires_at": 2_000_000_000})
        act.check_action_supported(bundle)
        act.check_justification(bundle)
        outcome = act.run_act(bundle, session)
        assert outcome["status"] == act.STATUS_EXECUTED, outcome
        assert outcome["malkuth_code"] == "accepted"

        # After the restart the reviewed identity is gone: replaying the same bundle is
        # refused by the identity check (a new pid), which is the practical replay guard.
        replay_deadline = time.time() + 15

        while time.time() < replay_deadline:
            try:
                act.run_act(bundle, session)
            except act.act_error as error:
                assert error.code in ("target_changed", "target_gone", "action_not_justified"), error.code
                break

            time.sleep(0.05)
        else:
            raise AssertionError("a replayed bundle was never refused after the restart")

        # Let the bounded supervision end on its own before the session closes, so the
        # worker thread finishes cleanly (closing first would surface a transport error).
        thread.join(timeout = 20)
        assert "error" not in supervised, supervised.get("error")
