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
import fake_mcp_server
import gate_grant
from celestia_devtools.judge import act, advice, cli, demo_fleet
from celestia_devtools.judge.mcp_stdio import ACTION_TOOLS, READ_ONLY_TOOLS, action_session, mcp_error, mcp_session, open_session

HERE = Path(__file__).resolve().parent
FAKE_SERVER = HERE / "fake_mcp_server.py"
JUDGE_SOURCES = Path(cli.__file__).resolve().parent
GATE = "0" * 32

def _bundle(**overrides) -> dict:
    bundle = {"proposal_id": "judge:0190f0f0-0000-7000-8000-000000000001", "gate_proposal_id": GATE, "run_id": "run-1", "worker_id": "app", "action": "restart", "verdict_code": "B1", "codes": ["B1"], "severity": "critical", "pid": 4242, "starttime": 99, "report_status": "failed", "expires_at": 2_000_000_000}
    bundle.update(overrides)
    # P0b: the bundle carries the issuer's grant, signed over its own final (gate token,
    # worker, action, identity) at build time — so every fixture stays attested against
    # the fake gate by default. Tests that want an unattested or stale grant pass
    # `gate_sig`/`gate_issued_at` explicitly and thereby opt out of this default.
    bundle.setdefault("gate_issued_at", int(time.time()))
    budget = 30 if bundle["action"] == "drain" else 0  # scepter's v2 mint: the service default envelope, or the restart 0 shape
    bundle.setdefault("gate_budget", budget)
    bundle.setdefault("gate_sig", gate_grant.sign_grant(bundle["gate_proposal_id"], bundle["worker_id"], bundle["action"], bundle.get("pid") or 0, bundle.get("starttime") or 0, bundle["gate_issued_at"], budget))
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

@pytest.mark.parametrize("missing", ["proposal_id", "gate_proposal_id", "run_id", "worker_id", "action", "verdict_code", "codes", "gate_sig", "gate_issued_at", "gate_budget"])
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

@pytest.mark.parametrize("sig", ["A" * 128, "0" * 127, "0" * 129, "z" * 128, "0x" + ("0" * 126)])
def test_a_malformed_grant_signature_is_refused(sig: str) -> None:
    """The grant signature is shape-checked at parse time (128 lowercase hex, exactly
    like the real gate) so a garbage bundle answers ``bundle_invalid`` here, with the
    clearer code, instead of bouncing off the gate as ``approval_unattested``."""

    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(gate_sig = sig))

    assert caught.value.code == "bundle_invalid"
    assert "gate_sig" in caught.value.message

@pytest.mark.parametrize("issued_at", ["1762610000", 1762610000.5, -1, True, None])
def test_a_bad_gate_issued_at_is_refused(issued_at) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(gate_issued_at = issued_at))

    assert caught.value.code == "bundle_invalid"
    assert "gate_issued_at" in caught.value.message

@pytest.mark.parametrize("budget", [True, "30", 1.5, -1, 301, None])
def test_a_bad_gate_budget_is_refused(budget) -> None:
    with pytest.raises(act.act_error) as caught:
        act.parse_handoff(_bundle(gate_budget = budget))

    assert caught.value.code == "bundle_invalid"
    assert "gate_budget" in caught.value.message

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

def test_an_unverifiable_grant_is_refused_unattested(monkeypatch, capsys) -> None:
    """P0b seam: a bundle whose signature does not verify against the gate's issuer key
    is the GATE's refusal, surfaced as ``gate_register_failed`` naming the gate's own
    code (``approval_unattested``) with a hint that says what to do — get a fresh
    bundle, not a new approval for this one."""

    _stdin(monkeypatch, _bundle(gate_sig = "0" * 128))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_FAILED
    assert document["code"] == "gate_register_failed"
    assert "approval_unattested" in document["message"]
    assert "fresh bundle" in document["hint"]

def test_a_stale_grant_is_refused_by_epoch(monkeypatch, capsys) -> None:
    """A correctly-signed grant older than the 300 s TTL answers ``approval_epoch_stale``
    (the fake mirror implements the same strict window as malkuth: 60 s future skew,
    300 s past TTL) — the signature being valid does not resurrect a stale grant."""

    issued = int(time.time()) - 400
    sig = gate_grant.sign_grant(GATE, "app", "restart", 4242, 99, issued)
    _stdin(monkeypatch, _bundle(gate_sig = sig, gate_issued_at = issued))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_FAILED
    assert document["code"] == "gate_register_failed"
    assert "approval_epoch_stale" in document["message"]
    assert "stale" in document["hint"]

def test_a_stale_grant_with_a_bad_signature_is_unattested_not_epoch_stale(monkeypatch, capsys) -> None:
    """Order pin (P0b round-1 S2): the signature verifies BEFORE the epoch window,
    so a garbage sig on a stale grant answers ``approval_unattested``. Answering
    ``epoch_stale`` here would mean the epoch check moved ahead of the signature —
    the exact defect shape this seam must never regress into."""

    issued = int(time.time()) - 400
    _stdin(monkeypatch, _bundle(gate_sig = "0" * 128, gate_issued_at = issued))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_FAILED
    assert document["code"] == "gate_register_failed"
    assert "approval_unattested" in document["message"]

def test_a_gate_binary_predating_the_grant_seam_is_refused(monkeypatch, capsys) -> None:
    """Probe pin: tool NAMES prove nothing — a build advertising the same four
    tools but without sig/issued_at in malkuth_approve's schema would serde-drop
    the grant and register the approval unattested (fail-open). The executor
    must refuse it as unavailable BEFORE any action call is issued."""

    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-pre-grant", capsys)
    assert code == act.EXIT_UNAVAILABLE
    assert document["code"] == "malkuth_unavailable"
    assert "predates the grant seam" in document["message"]

def test_the_schema_store_fails_closed_for_missing_or_non_dict_schemas() -> None:
    """Unit pin for the store the staleness probe reads: an unadvertised name
    and a NON-dict inputSchema must both surface as ``{}`` — anything else
    (KeyError, a string leaking through) would crash or mislead the probe
    instead of failing closed."""

    with open_session([sys.executable, str(FAKE_SERVER), "act-pre-grant"]) as session:
        assert session.tool_schema("never-advertised") == {}
        assert session.tool_schema("malkuth_status") == {}, "non-dict inputSchema normalizes to {}"
        assert set(session.tool_schema("malkuth_approve").get("properties") or {}) == {"proposal_id", "worker_id", "decision", "action"}

def test_a_grant_signed_over_another_target_is_refused_unattested(monkeypatch, capsys, tmp_path) -> None:
    """The grant binds the target identity: a signature over a different starttime
    (a recycled-pid attempt) must not verify against the gate's own scan."""

    issued = int(time.time())
    sig = gate_grant.sign_grant(GATE, "app", "restart", 4242, 100, issued)
    _stdin(monkeypatch, _bundle(gate_sig = sig, gate_issued_at = issued))
    code, document, _ = _run("act-ok", capsys)
    assert code == act.EXIT_FAILED
    assert document["code"] == "gate_register_failed"
    assert "approval_unattested" in document["message"]

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
    required fields, exactly like the real malkuth gate does — same parse
    order (action before decision) and same stable codes for missing and
    unknown values."""

    with action_session([sys.executable, str(FAKE_SERVER), "act-ok"]) as session:
        missing_action = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "allow"})
        assert missing_action["code"] == "approval_action_required"
        missing_decision = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "action": "restart"})
        assert missing_decision["code"] == "approval_decision_required"
        both_missing = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w"})
        assert both_missing["code"] == "approval_action_required", "the gate names the action first"
        unknown_action = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "allow", "action": "hibernate"})
        assert unknown_action["code"] == "unsupported_action"
        unknown_decision = session.call_tool("malkuth_approve", {"proposal_id": "p", "worker_id": "w", "decision": "maybe", "action": "restart"})
        assert unknown_decision["code"] == "unsupported_decision"
        # P0b seam: an allow without the issuer's grant is refused unattested; a grant
        # for a worker that is not live is refused the same way (the identity scan);
        # a signed grant for the live worker registers — exactly the malkuth wire
        # contract.
        grant_proposal = "b" * 32
        issued_at = int(time.time())
        unsigned = session.call_tool("malkuth_approve", {"proposal_id": grant_proposal, "worker_id": "app", "decision": "allow", "action": "drain"})
        assert unsigned["code"] == "approval_unattested"
        unknown_worker = session.call_tool("malkuth_approve", {"proposal_id": grant_proposal, "worker_id": "elsewhere", "decision": "allow", "action": "drain", "sig": gate_grant.sign_grant(grant_proposal, "elsewhere", "drain", 4242, 99, issued_at, 30), "issued_at": issued_at, "budget_secs": 30})
        assert unknown_worker["code"] == "approval_unattested", "a grant must name one live worker of this fleet"
        sig = gate_grant.sign_grant(grant_proposal, "app", "drain", 4242, 99, issued_at, 30)
        bound = session.call_tool("malkuth_approve", {"proposal_id": grant_proposal, "worker_id": "app", "decision": "allow", "action": "drain", "sig": sig, "issued_at": issued_at, "budget_secs": 30})
        assert bound["ok"] is True
        mismatch = session.call_tool("malkuth_restart", {"run_id": "run-1", "worker_id": "app", "proposal_id": grant_proposal})
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
    """`--supervise` starts a fleet, binds the bundle to it and executes there.

    The demo bundle authorises the fleet it will bring: the grant is signed over the
    identity the fake gate reports for its supervised run (worker ``demo-0``, the
    fixed ACT identity) — the same whole-identity binding malkuth's gate demands. A
    bundle minted for the pre-fleet ``app`` worker would be refused unattested, which
    is the point of the gate: the grant names who it blesses.
    """

    payload = _bundle()

    for field in ("run_id", "worker_id", "pid", "starttime"):
        payload.pop(field)

    issued = int(time.time())
    payload["gate_issued_at"] = issued
    payload["gate_sig"] = gate_grant.sign_grant(GATE, "demo-0", "restart", fake_mcp_server.ACT_PID, fake_mcp_server.ACT_STARTTIME, issued)

    _stdin(monkeypatch, payload)
    code, document, _ = _run("act-ok", capsys, argv = ["--supervise", "sleep 30", "--supervise-timeout", "1"])

    assert code == act.EXIT_OK, document
    assert document["status"] == "executed"
    assert document["target"] == {"run_id": "run-1", "worker_id": "demo-0"}

def test_the_demo_switch_refuses_a_bundle_minted_for_another_fleet(monkeypatch, capsys) -> None:
    """A bundle that arrives with the platform's identity baked in cannot be spent on
    a demo fleet: the rebind would rewrite the identity but only scepter re-signs, so
    the gate's unattested refusal (and its "mint a fresh bundle" hint) could never be
    satisfied. The executor names the real reason instead of looping the operator."""

    _stdin(monkeypatch, _bundle())
    code, document, _ = _run("act-ok", capsys, argv = ["--supervise", "sleep 30", "--supervise-timeout", "1"])

    assert code == act.EXIT_REFUSED, document
    assert document["status"] == "refused"
    assert document["code"] == "gate_bundle_foreign_fleet"
    assert "worker_id" in document["message"]
    assert "platform" in document["hint"]

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

def test_the_demo_switch_leaves_no_supervised_process_behind(monkeypatch, tmp_path) -> None:
    """The demo switch owns the fleet it brings: after the act, nothing of it runs on."""

    binary = _require_gate_binary()
    monkeypatch.setenv("MALKUTH_GATE_VERIFY_KEY", gate_grant.GATE_TEST_VERIFY_KEY_HEX)
    counter = tmp_path / "crash-counter"
    # Two crashes then a long run: the demo is a real (short) crash loop that settles, and
    # the teardown must remove the settled child.
    command = [["sh", "-c", 'n=$(cat "$1" 2>/dev/null || echo 0); echo $((n + 1)) > "$1"; if [ "$n" -lt 2 ]; then exit 1; fi; sleep 60', "sh", str(counter)]]
    session = action_session([binary, "mcp"], timeout = 10)

    with session:
        with demo_fleet.supervision(session, command, timeout_secs = 5, max_restarts = 10) as demo:
            assert demo.run_seen, demo.error
            bound = act.bind_to_supervised_fleet(_bundle_missing_target(), session)
            # The test plays scepter for the live identity: a grant minted anywhere
            # else could not cover the pid this crash loop settled on.
            issued = int(time.time())
            bound["gate_issued_at"] = issued
            bound["gate_sig"] = gate_grant.sign_grant(bound["gate_proposal_id"], bound["worker_id"], bound["action"], bound["pid"], bound["starttime"], issued)
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

def test_a_real_malkuth_restart_executes_through_the_gate(monkeypatch) -> None:
    """The executor against a real gate: supervise, act, and read the tool's own answer.

    The gate's verify key is the published RFC 8032 test key for this run (the env
    override malkuth documents for non-production deployments): the grant in the
    bundle is signed with the matching seed in ``tests/gate_grant.py``. Production
    malkuth embeds its own key and scepter signs with the issuer seed — the wire
    contract this test pins is identical either way.
    """

    binary = _require_gate_binary()
    monkeypatch.setenv("MALKUTH_GATE_VERIFY_KEY", gate_grant.GATE_TEST_VERIFY_KEY_HEX)
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
        issued_at = int(time.time())
        bundle = act.parse_handoff({"proposal_id": "judge:real-1", "gate_proposal_id": "a" * 32, "run_id": run_id, "worker_id": "demo-0", "action": "restart", "verdict_code": "B1", "codes": ["B1"], "severity": "critical", "pid": target.pid, "starttime": target.starttime, "report_status": "failed", "expires_at": 2_000_000_000, "gate_issued_at": issued_at, "gate_budget": 0, "gate_sig": gate_grant.sign_grant("a" * 32, "demo-0", "restart", target.pid, target.starttime, issued_at)})
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
