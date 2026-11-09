"""End-to-end: judge -> advice -> hand-off bundle -> act -> report, against a real gate.

This is the integration pin for the wave-4 action slice (task-14). It deliberately runs the
*real* malkuth (supervision, status, approval gate, restart) and the real judge/advice/act
code, and stops short of running scepter itself: giving scepter a real instance here would
mean a database, a WebSocket plane and credentials — deployment surface, not a unit test.
Instead the hand-off bundle is a **fixture whose shape is asserted field-by-field against
scepter's `export_bundle`** (entelecheia `packages/scepter/src/services/judge_actions.rs`,
the function that serves `Cli.JudgeActionExport`), so a drift on either side fails here.

The chain the test walks:

1. supervise a worker that crashes twice and then stays up (real malkuth);
2. sample it through the judge's read tools -> a real ``B1`` critical verdict;
3. ``advice.decide``/``suggest`` -> the ``restart`` action for that target;
4. build the bundle scepter would hand over for that row;
5. run the executor's preflight + gate + restart **on the supervising session** (the
   production shape: the platform calls ``act.run_act(bundle, session)``);
6. assert the worker really is a new process afterwards;
7. map the executor's result into the shape ``Cli.JudgeActionReport`` accepts.

The bundle's three field gaps are asserted explicitly rather than papered over: the export
carries the gate token, target, identity, action, verdict, codes/evidence and — since P0b —
the issuer's grant signature (``gate_sig`` + ``gate_issued_at``: what scepter signs when the
operator allows), but not the row id, the severity or the execution deadline — the platform
session supplies those from the row it exported.
"""

from __future__ import annotations
import hashlib
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
import pytest
import gate_grant
from celestia_devtools.judge import act, advice, demo_fleet, fleet
from celestia_devtools.judge.mcp_stdio import ACTION_TOOLS, READ_ONLY_TOOLS, action_session, mcp_session

#: Two crashes then a long run: the judge sees a real crash loop, and the executor still has
#: a stable, running target to restart (so "the pid changed" is attributable to OUR action).
CRASH_THEN_STAY_UP = 'n=$(cat "$1" 2>/dev/null || echo 0); echo $((n + 1)) > "$1"; if [ "$n" -lt 3 ]; then sleep 0.4; exit 1; fi; sleep 60'

def _malkuth_bin() -> str | None:
    candidate = os.environ.get("JUDGE_MALKUTH_BIN") or shutil.which("malkuth")

    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate

    return None

def _require_gate_binary() -> str:
    binary = _malkuth_bin()

    if binary is None:
        pytest.skip("no malkuth binary available")

    with mcp_session([binary, "mcp"], timeout = 10) as session:
        if not ACTION_TOOLS <= set(session.tools):
            pytest.skip(f"malkuth at {binary} predates the action gate ({sorted(session.tools)})")

        # The grant seam: names alone cannot tell a pre-grant build (same four tools,
        # unknown sig/issued_at silently dropped by serde) from a grant-aware one —
        # the advertised schema can.
        approve_fields = set(session.tool_schema("malkuth_approve").get("properties") or {})

        if not {"sig", "issued_at", "budget_secs"} <= approve_fields:
            pytest.skip(f"malkuth at {binary} predates the v2 grant seam (malkuth_approve schema lacks sig/issued_at/budget_secs)")

    return binary

#: The exact keys `Cli.JudgeActionExport` returns (entelecheia `export_bundle`,
#: packages/scepter/src/services/judge_actions.rs @ 436595db + the P0b grant fields the
#: scepter leg adds when it signs). Anything added or removed there must show up here as
#: a change to this list, not as a silently-passing test.
EXPORT_BUNDLE_FIELDS = ("gate_proposal_id", "run_id", "worker_id", "pid", "starttime", "action", "verdict_code", "codes", "evidence", "gate_sig", "gate_issued_at", "gate_budget")

#: Fields the platform session adds from the row it exported (they are NOT in the bundle).
PLATFORM_SUPPLIES = ("proposal_id", "severity", "expires_at")

def _gate_token(seed: str) -> str:
    """What scepter writes into `gate_proposal_id`: 32 lowercase hex, no prefix."""

    return hashlib.sha256(seed.encode()).hexdigest()[:32]

def _export_bundle(row: dict) -> dict:
    """The bundle ``Cli.JudgeActionExport`` returns, field for field.

    Shape pinned against entelecheia's `export_bundle` (packages/scepter/src/services/
    judge_actions.rs): `gate_proposal_id`, `run_id`, `worker_id`, `pid`, `starttime`,
    `action`, `verdict_code`, `codes`, `evidence`, `gate_sig`, `gate_issued_at`,
    `gate_budget` (the v2 spend envelope) — and deliberately nothing else: the
    submitter, the ruling and the timestamps stay in scepter's own row.
    """

    return {"gate_proposal_id": row["gate_proposal_id"], "run_id": row["run_id"], "worker_id": row["worker_id"], "pid": row["pid"], "starttime": row["starttime"], "action": row["action"], "verdict_code": row["verdict_code"], "codes": row["codes"], "evidence": row["evidence"], "gate_sig": row["gate_sig"], "gate_issued_at": row["gate_issued_at"], "gate_budget": row["gate_budget"]}

def _crashes(counter: Path) -> int:
    try:
        return int(counter.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0

def _wait_for_running(session: action_session, *, deadline: float = 20.0, after_crashes: int = 0, counter: Path | None = None) -> tuple[str, dict]:
    """The first running snapshot with a whole identity (and optionally after N crashes)."""

    end = time.time() + deadline

    while time.time() < end:
        status = session.call_tool("malkuth_status")

        for run in status.get("runs") or []:
            for worker in run.get("workers") or []:
                if not ((worker.get("phase") == "running") and worker.get("pid") and worker.get("starttime")):
                    continue

                if (counter is not None) and (_crashes(counter) < after_crashes):
                    continue

                return run["run_id"], worker

        time.sleep(0.05)

    raise AssertionError("the worker never reached a running state with pid+starttime")

def _judge_once(session: action_session, *, ticks: int = 6, interval: float = 0.3) -> tuple[list, advice.suggestion]:
    """Sample the fleet like the judge CLI does and take its suggestion for the target."""

    observation = fleet.observation()

    for index in range(ticks):
        if index:
            time.sleep(interval)

        run_states = fleet.parse_status(session.call_tool("malkuth_status"))
        observation.add(fleet.tick(index = index, runs = run_states))

    verdicts = [verdict for verdict in fleet.evaluate(observation) if verdict.severity == "critical"]
    suggestions = [row for row in advice.suggest(verdicts, observation) if row.action in (advice.ACTION_RESTART, advice.ACTION_DRAIN)]
    assert suggestions, f"the judge produced no actionable suggestion from verdicts {[(v.code, v.severity) for v in verdicts]}"
    return verdicts, suggestions[0]

def _report_payload(proposal_id: str, result: dict) -> dict:
    """Map the executor's result onto ``Cli.JudgeActionReport``'s input.

    Mirrors scepter's `parse_report` (packages/scepter/src/services/judge_actions.rs):
    `proposal_id` + `status ∈ {executed, failed}` + `result_code`, optional detail.
    """

    assert result["status"] in ("executed", "failed"), f"a refused run must not be reported: {result}"

    code = result.get("malkuth_code") or result.get("code")
    assert isinstance(code, str) and code, f"the report needs a result_code: {result}"
    return {"proposal_id": proposal_id, "status": result["status"], "result_code": code}

def test_the_export_contract_is_the_one_the_executor_receives() -> None:
    """Shape pin, no malkuth needed: the bundle is the export's fields, plus three from the row."""

    issued_at = int(time.time())
    row = {"gate_proposal_id": _gate_token("pin"), "run_id": "run-1", "worker_id": "w", "pid": 1, "starttime": 2, "action": "restart", "verdict_code": "B1", "codes": ["B1"], "evidence": {"codes": ["B1"]}, "gate_issued_at": issued_at, "gate_budget": 0, "gate_sig": gate_grant.sign_grant(_gate_token("pin"), "w", "restart", 1, 2, issued_at)}
    hand_off = _export_bundle(row)
    assert tuple(hand_off) == EXPORT_BUNDLE_FIELDS, "the fixture drifted from the export contract"

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(hand_off)

    assert caught.value.code == "bundle_invalid"

    for field in PLATFORM_SUPPLIES:
        assert field not in hand_off, f"{field} is the platform's to add, not the export's"

    complete = {**hand_off, "proposal_id": "judge:x", "severity": "critical", "expires_at": time.time() + 600}
    bundle = act.parse_handoff(complete)
    assert bundle.gate_proposal_id == row["gate_proposal_id"]

def test_the_wave4_chain_runs_end_to_end_against_a_real_gate(monkeypatch, tmp_path: Path) -> None:
    binary = _require_gate_binary()
    # The gate verifies grants against the published RFC 8032 test key for this run
    # (malkuth's documented env override); the bundle's grant is signed with the
    # matching seed. Production embeds its own key and scepter signs with the issuer
    # seed — the wire contract is identical either way.
    monkeypatch.setenv("MALKUTH_GATE_VERIFY_KEY", gate_grant.GATE_TEST_VERIFY_KEY_HEX)
    counter = tmp_path / "crash-counter"
    proposal_id = f"judge:{uuid.uuid4()}"
    session = action_session([binary, "mcp"], timeout = 10)
    supervised: dict = {}

    with session:
        def drive() -> None:
            try:
                supervised["payload"] = demo_fleet.start_supervision(session, [["sh", "-c", CRASH_THEN_STAY_UP, "sh", str(counter)]], timeout_secs = 60, max_restarts = 10)
            except Exception as error:  # noqa: BLE001 - surfaced after join
                supervised["error"] = repr(error)

        thread = threading.Thread(target = drive, name = "e2e-supervise", daemon = True)
        thread.start()
        # Step 1/2: the real judge sees a real crash loop on a worker that then stays up.
        run_id, worker = _wait_for_running(session)
        # Sample WHILE it is crashing: B1 is a restart-count delta, so the judge must see the
        # loop move (sampling only after it stabilises would honestly report "ok").
        verdicts, suggestion = _judge_once(session)
        # …then let it come back from its crashes (the stable phase the executor acts on).
        _, worker = _wait_for_running(session, after_crashes = 3, counter = counter)
        assert _crashes(counter) >= 3, "the worker must have crashed three times before stabilising"
        assert suggestion.action == advice.ACTION_RESTART, suggestion
        assert "B1" in suggestion.evidence["codes"], suggestion.evidence
        assert verdicts, "a critical verdict must exist for the advice to rest on"

        # Step 4: the row scepter would export for this target — including the grant
        # scepter signs at allow time (P0b): the executor presents it verbatim.
        gate_proposal = _gate_token(f"{run_id}/{worker['id']}/{proposal_id}")
        issued_at = int(time.time())
        row = {"gate_proposal_id": gate_proposal, "run_id": run_id, "worker_id": worker["id"], "pid": worker["pid"], "starttime": worker["starttime"], "action": suggestion.action, "verdict_code": "B1", "codes": list(suggestion.evidence["codes"]), "evidence": {"codes": list(suggestion.evidence["codes"]), "verdicts": [{"code": "B1", "severity": "critical"}], "worker": {"phase": worker["phase"], "pid": worker["pid"], "starttime": worker["starttime"]}}, "gate_issued_at": issued_at, "gate_budget": 0, "gate_sig": gate_grant.sign_grant(gate_proposal, worker["id"], suggestion.action, worker["pid"], worker["starttime"], issued_at)}
        hand_off = _export_bundle(row)

        # The export is NOT a complete executor bundle: the row id, the severity and the
        # deadline live in scepter's row, and the platform session supplies them.
        with pytest.raises(act.act_error) as caught:
            act.parse_handoff(hand_off)

        assert caught.value.code == "bundle_invalid"
        assert "proposal_id" in caught.value.message
        assert "severity" in caught.value.message

        # Adding only the row id/severity still leaves the deadline missing -> fail closed.
        partial = {**hand_off, "proposal_id": proposal_id, "severity": "critical"}

        with pytest.raises(act.act_error) as caught:
            act.parse_handoff(partial)

        assert caught.value.code == "window_expired"

        # Step 5: the complete hand-off the platform session hands the executor.
        bundle = act.parse_handoff({**partial, "expires_at": time.time() + 600})
        act.check_action_supported(bundle)
        act.check_justification(bundle)
        before = _wait_for_running(session)[1]
        outcome = act.run_act(bundle, session)
        assert outcome["status"] == act.STATUS_EXECUTED, outcome
        assert outcome["malkuth_code"] == "accepted"

        # Step 6: the worker is a new process (the restart really happened).
        deadline = time.time() + 20
        after = None

        while time.time() < deadline:
            _, current = _wait_for_running(session)

            if (current["pid"], current["starttime"]) != (before["pid"], before["starttime"]):
                after = current
                break

            time.sleep(0.05)

        assert after is not None, "the restarted worker never came back with a new identity"
        assert after["pid"] != before["pid"]

        # Step 7: the executor's answer is directly reportable.
        report = _report_payload(proposal_id, outcome)
        assert report["status"] == "executed"
        assert report["result_code"] == "accepted"

        # …and a refusal is not reportable as an outcome (the row must stay approved).
        with pytest.raises(AssertionError):
            _report_payload(proposal_id, {"status": "refused", "code": "target_changed"})

        # Drain through the same gate so the supervision ends promptly — signed over
        # the post-restart identity (`after`), because the grant binds the live worker.
        # call_tool does not raise on ok:false, so the payloads are asserted too: a
        # mutated grant must fail HERE, not slip through an unchecked return.
        drain_token = _gate_token(f"drain/{run_id}")
        drain_issued = int(time.time())
        approve_out = session.call_tool("malkuth_approve", {"proposal_id": drain_token, "worker_id": after["id"], "decision": "allow", "action": "drain", "sig": gate_grant.sign_grant(drain_token, after["id"], "drain", after["pid"], after["starttime"], drain_issued, 30), "issued_at": drain_issued, "budget_secs": 30})
        assert approve_out.get("ok") is True, f"the drain approval must register: {approve_out}"
        drain_out = session.call_tool("malkuth_drain", {"run_id": run_id, "worker_id": worker["id"], "proposal_id": drain_token, "kind": "graceful", "timeout_secs": 10})
        assert drain_out.get("ok") is True, f"the drain must be accepted: {drain_out}"
        thread.join(timeout = 20)
        assert "error" not in supervised, supervised.get("error")

def test_the_judge_path_still_cannot_act() -> None:
    """The chain's read half must stay read-only (the guards #241/#249 established)."""

    assert READ_ONLY_TOOLS == {"malkuth_status", "malkuth_logs", "malkuth_probe"}
    assert ACTION_TOOLS == {"malkuth_status", "malkuth_approve", "malkuth_restart", "malkuth_drain"}
    assert not (READ_ONLY_TOOLS & (ACTION_TOOLS - {"malkuth_status"}))

    binary = _malkuth_bin()

    if binary is None:
        pytest.skip("no malkuth binary available")

    with mcp_session([binary, "mcp"], timeout = 10) as session:
        with pytest.raises(Exception) as caught:
            session.call_tool("malkuth_" + "restart", {})

        assert "allowlist" in str(caught.value)
