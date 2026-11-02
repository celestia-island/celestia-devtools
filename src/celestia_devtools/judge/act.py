"""``judge-fleet act`` — execute ONE already-approved judge action (wave-4 B-2).

This is the **executor** side of the action slice, and the only module in the package that
may call an action tool (:data:`celestia_devtools.judge.mcp_stdio.ACTION_TOOLS`). It has no
credential of its own: the handoff bundle arrives on stdin (scepter's ``Export`` output),
and everything it learns about the world comes from one ``malkuth_status`` read plus the
gate's own answers. The durable audit record stays in scepter — this module writes nothing
anywhere, it prints one JSON result and exits.

Why a bundle and not a lookup: letting the executor ask scepter "what is approved?" would
need a scepter credential *and* would let it pick targets on its own, which is exactly what
"the operator stays in the loop" forbids. The bundle is the operator's handoff.

Preflight (all of it happens before the first mutating call):

1. ``bundle_invalid``    — not JSON, missing field, wrong type;
2. ``action_not_supported`` — action is neither ``restart`` nor ``drain``;
3. ``action_not_justified`` — the report is not ``failed``/critical, no codes, the code that
   justifies the action is missing, or the action contradicts the judge's fixed priority
   table (a ``C2`` spawn failure is never restarted; a suspended drain is never restarted);
4. ``target_unknown`` / ``target_gone`` / ``identity_missing`` / ``target_changed`` — the
   target is not in the live snapshot, has stopped, has no identity to compare, or the live
   pid+starttime differ from the ones the operator approved (pid reuse / a restart since).

Then it registers the gate approval and executes exactly one action. A refusal from the
gate is reported with malkuth's own code and a hint; a transport failure exits 5.

Exit codes: 0 executed (or a clean dry run) | 2 usage | 3 refused | 4 execution failed
(the gate or the tool said no) | 5 malkuth unavailable.
"""

from __future__ import annotations
import argparse
import json
import shlex
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any
from celestia_devtools.judge import advice
from celestia_devtools.judge.mcp_stdio import ACTION_TOOLS, action_session, mcp_error, mcp_unavailable

#: Exit codes (stable; the platform branches on them).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_FAILED = 4
EXIT_UNAVAILABLE = 5

ACTION_RESTART = "restart"
ACTION_DRAIN = "drain"
SUPPORTED_ACTIONS = (ACTION_RESTART, ACTION_DRAIN)

#: Bundle keys the executor cannot work without. ``pid``/``starttime`` are checked in step 4
#: under their own code (``identity_missing``) so the two failures stay distinguishable.
REQUIRED_FIELDS = ("proposal_id", "gate_proposal_id", "run_id", "worker_id", "action", "verdict_code", "codes")

#: The judge report status that may be acted on at all. A report that is ``ok``,
#: ``indeterminate`` or ``unavailable`` has no finding to act on — ``unavailable`` especially:
#: it is the *absence* of a judgement and must never read as permission.
ACTIONABLE_REPORT_STATUS = "failed"

#: Codes that justify a mutating action (the notifiable ones — C2, suspicious signals — do not).
ACTIONABLE_CODES = frozenset({"B1", "C1", "B3"})

STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_REFUSED = "refused"
STATUS_DRY_RUN = "dry_run"

#: Gate answers that mean "the approval itself is not usable", with the operator's next move.
GATE_HINTS = {"approval_required": "no live approval for this (proposal, worker) pair: re-run act to register it, or check the bundle", "approval_expired": "the gate window (300 s) closed: approve the proposal again in scepter", "approval_consumed": "this approval was already spent on an action: a new approval is required", "approval_blocked": "an operator blocked this pair: only a fresh allow can unblock it"}
GATE_REFUSALS = frozenset(GATE_HINTS)

class act_error(RuntimeError):
    """A refusal with a stable code (the message is for humans; callers branch on code)."""

    def __init__(self, code: str, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

@dataclass(frozen = True)
class handoff:
    """One approved action, as the operator handed it over (never mutated)."""

    proposal_id: str
    gate_proposal_id: str
    run_id: str
    worker_id: str
    action: str
    verdict_code: str
    codes: tuple[str, ...]
    severity: str
    pid: int | None
    starttime: int | None
    report_status: str | None
    expires_at: float | None
    drain_kind: str
    received_at: float = field(default_factory = time.time)

def _string(payload: dict, key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)

    if value is None:
        if required:
            raise act_error("bundle_invalid", f"the handoff bundle is missing `{key}`")

        return None

    if not isinstance(value, str) or not value.strip():
        raise act_error("bundle_invalid", f"`{key}` must be a non-empty string")

    return value.strip()

def _optional_int(payload: dict, key: str) -> int | None:
    value = payload.get(key)

    if value is None:
        return None

    if isinstance(value, bool) or not isinstance(value, int):
        raise act_error("bundle_invalid", f"`{key}` must be an integer (or null)")

    return value

def _timestamp(value: Any) -> float:
    """A deadline as unix seconds or an RFC 3339 string (scepter serializes timestamps both ways)."""

    if isinstance(value, bool):
        raise act_error("bundle_invalid", "`expires_at` must be a timestamp, not a boolean")

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")

        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError as e:
            raise act_error("bundle_invalid", f"`expires_at` is not RFC 3339: {e}") from e

    raise act_error("bundle_invalid", "`expires_at` must be a unix timestamp or an RFC 3339 string")

def _codes(payload: dict) -> tuple[str, ...]:
    value = payload.get("codes")

    if not isinstance(value, list) or not value:
        raise act_error("bundle_invalid", "`codes` must be a non-empty list")

    codes = []

    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise act_error("bundle_invalid", "every entry of `codes` must be a non-empty string")

        codes.append(item.strip().upper())

    return tuple(dict.fromkeys(codes))

def parse_handoff(payload: Any) -> handoff:
    """Validate the bundle's shape; every failure is ``bundle_invalid``."""

    if not isinstance(payload, dict):
        raise act_error("bundle_invalid", "the handoff bundle must be a JSON object")

    missing = [key for key in REQUIRED_FIELDS if key not in payload]

    if missing:
        raise act_error("bundle_invalid", f"the handoff bundle is missing {missing}")

    gate = _string(payload, "gate_proposal_id")
    severity = _string(payload, "severity", required = False) or ""
    report_status = _string(payload, "report_status", required = False) or _string(payload, "status", required = False)
    expires_at = payload.get("expires_at")
    horizon: float | None = None

    if expires_at is not None:
        horizon = _timestamp(expires_at)

    drain_kind = _string(payload, "drain_kind", required = False) or "graceful"

    if drain_kind not in ("graceful", "immediate"):
        raise act_error("bundle_invalid", "`drain_kind` must be `graceful` or `immediate`")

    parsed = handoff(proposal_id = _string(payload, "proposal_id") or "", gate_proposal_id = gate or "", run_id = _string(payload, "run_id") or "", worker_id = _string(payload, "worker_id") or "", action = (_string(payload, "action") or "").lower(), verdict_code = (_string(payload, "verdict_code") or "").upper(), codes = _codes(payload), severity = severity.lower(), pid = _optional_int(payload, "pid"), starttime = _optional_int(payload, "starttime"), report_status = (report_status or "").lower() or None, expires_at = horizon, drain_kind = drain_kind)
    return parsed

def check_action_supported(bundle: handoff) -> None:
    if bundle.action not in SUPPORTED_ACTIONS:
        raise act_error("action_not_supported", f"action {bundle.action!r} is not one of {list(SUPPORTED_ACTIONS)}")

class _drain_flag:
    """The one field :func:`advice.decide` reads from a run, without inventing a fleet."""

    def __init__(self, draining: bool) -> None:
        self.draining = draining

def expected_action(bundle: handoff, *, draining: bool = False) -> str:
    """The judge's fixed priority answer for these codes (the single source: ``advice``)."""

    return advice.decide(set(bundle.codes), _drain_flag(draining))[0]

def check_justification(bundle: handoff, *, now: float | None = None) -> None:
    """Step 3: is there a finding here that a mutating action is allowed to answer?"""

    if (bundle.report_status is not None) and (bundle.report_status != ACTIONABLE_REPORT_STATUS):
        raise act_error("action_not_justified", f"the report status is {bundle.report_status!r}: only a {ACTIONABLE_REPORT_STATUS!r} report may be acted on", hint = "an `unavailable` report is the absence of a judgement, not permission")

    if bundle.severity and (bundle.severity != "critical"):
        raise act_error("action_not_justified", f"severity is {bundle.severity!r}: only critical findings are actionable")

    if bundle.verdict_code not in bundle.codes:
        raise act_error("action_not_justified", f"`codes` {list(bundle.codes)} does not contain the justifying code {bundle.verdict_code!r}")

    actionable = ACTIONABLE_CODES & set(bundle.codes)

    if not actionable:
        raise act_error("action_not_justified", f"codes {list(bundle.codes)} contain nothing an action may answer", hint = "C2 (never got up) and suspicious signals are notify-only")

    now = time.time() if now is None else now

    if (bundle.expires_at is not None) and (now >= bundle.expires_at):
        raise act_error("window_expired", "the approval's execution window has closed", hint = "a closed window means the approval is stale: re-approve in scepter if this is still wanted")

    # Coarse priority check with the drain flag unknown; step 4 refines it once the live
    # snapshot is in hand (whether the run is already draining flips C1 from restart to drain).
    coarse = expected_action(bundle)

    if coarse not in SUPPORTED_ACTIONS:
        raise act_error("action_not_justified", f"the judge's priority table answers {coarse!r} for codes {list(bundle.codes)}, not an execution", hint = "notify/observe are not actions")

@dataclass
class live_target:
    pid: int | None
    starttime: int | None
    phase: str
    draining: bool

def _find_target(status: Any, run_id: str, worker_id: str) -> live_target:
    if (not isinstance(status, dict)) or (status.get("ok") is not True):
        raise act_error("target_unknown", "the live status payload is not a successful snapshot")

    runs = status.get("runs")

    if not isinstance(runs, list):
        raise act_error("target_unknown", "the live status payload has no runs array")

    for run in runs:
        if (not isinstance(run, dict)) or (run.get("run_id") != run_id):
            continue

        for worker in run.get("workers") or []:
            if (not isinstance(worker, dict)) or (worker.get("id") != worker_id):
                continue

            phase = worker.get("phase")
            return live_target(pid = worker.get("pid"), starttime = worker.get("starttime"), phase = str(phase), draining = bool(run.get("draining")))

        raise act_error("target_unknown", f"run {run_id!r} has no worker {worker_id!r}")

    raise act_error("target_unknown", f"no live run {run_id!r} (malkuth's registry is per-process: the supervising session must own it)")

def check_target(bundle: handoff, target: live_target) -> None:
    """Step 4: the target must be live, the identity must be whole AND unchanged."""

    if target.phase in ("stopped", "starting"):
        raise act_error("target_gone", f"worker {bundle.worker_id!r} is {target.phase!r}: there is nothing to act on")

    if (bundle.pid is None) or (bundle.starttime is None):
        raise act_error("identity_missing", "the bundle carries no pid/starttime: an action without a target identity is refused")

    if (target.pid is None) or (target.starttime is None):
        raise act_error("target_changed", "the live worker has no pid/starttime right now (it is between children): the approved identity cannot be matched")

    if (target.pid, target.starttime) != (bundle.pid, bundle.starttime):
        raise act_error("target_changed", f"the live identity ({target.pid},{target.starttime}) is not the approved one ({bundle.pid},{bundle.starttime})", hint = "pid reuse or a restart since the proposal: a new proposal is required")

    expected = expected_action(bundle, draining = target.draining)

    if expected != bundle.action:
        raise act_error("action_not_justified", f"codes {list(bundle.codes)} call for {expected!r} (run draining={target.draining}), not {bundle.action!r}")

def _execute_arguments(bundle: handoff) -> dict:
    if bundle.action == ACTION_RESTART:
        return {"run_id": bundle.run_id, "worker_id": bundle.worker_id, "proposal_id": bundle.gate_proposal_id}

    return {"run_id": bundle.run_id, "worker_id": bundle.worker_id, "proposal_id": bundle.gate_proposal_id, "kind": bundle.drain_kind}

def run_act(bundle: handoff, session: action_session, *, dry_run: bool = False) -> dict:
    """Walk the checklist; returns the result document (raises :class:`act_error` on refusal)."""

    checks: list[dict] = []
    status = session.call_tool("malkuth_status")

    try:
        target = _find_target(status, bundle.run_id, bundle.worker_id)
        check_target(bundle, target)
    except act_error as error:
        checks.append({"step": "target", "ok": False, "code": error.code, "detail": error.message})
        raise

    checks.append({"step": "target", "ok": True, "detail": f"phase={target.phase} pid={target.pid} starttime={target.starttime}"})

    if dry_run:
        return {"status": STATUS_DRY_RUN, "checks": checks, "would_approve": bundle.gate_proposal_id, "would_call": _execute_arguments(bundle)}

    approval = session.call_tool("malkuth_approve", {"proposal_id": bundle.gate_proposal_id, "worker_id": bundle.worker_id, "decision": "allow"})

    if (not isinstance(approval, dict)) or (approval.get("ok") is not True):
        code = approval.get("code") if isinstance(approval, dict) else None
        raise act_error("gate_register_failed", f"the gate refused to register the approval: {code or approval}", hint = GATE_HINTS.get(str(code), "check the proposal_id/worker_id pair"))

    checks.append({"step": "approve", "ok": True, "detail": "registered"})
    result = session.call_tool("malkuth_restart" if bundle.action == ACTION_RESTART else "malkuth_drain", _execute_arguments(bundle))

    if not isinstance(result, dict):
        raise act_error("execution_failed", f"the {bundle.action} tool returned a non-object payload")

    if result.get("ok") is not True:
        code = str(result.get("code") or "")

        if code in GATE_REFUSALS:
            raise act_error(code, str(result.get("message") or f"the gate refused: {code}"), hint = GATE_HINTS[code])

        raise act_error("execution_failed", f"{bundle.action} failed: {code or result.get('message')}")

    checks.append({"step": "execute", "ok": True, "detail": code_of(result)})
    return {"status": STATUS_EXECUTED, "checks": checks, "malkuth_code": code_of(result)}

def code_of(result: dict) -> str:
    """A stable result code for the audit row (never invented: the tool's own answer)."""

    if ("code" in result) and isinstance(result["code"], str):
        return result["code"]

    if result.get("accepted") is True:
        return "accepted"

    return "ok"

def _result_document(bundle: handoff | None, *, status: str, error: act_error | None = None, outcome: dict | None = None, age_secs: float | None = None) -> dict:
    document: dict[str, Any] = {"status": status}

    if bundle is not None:
        document["proposal_id"] = bundle.proposal_id
        document["action"] = bundle.action
        document["target"] = {"run_id": bundle.run_id, "worker_id": bundle.worker_id}

    if error is not None:
        document["code"] = error.code
        document["message"] = error.message

        if error.hint:
            document["hint"] = error.hint

    if outcome is not None:
        document["checks"] = outcome.get("checks", [])

        if outcome.get("malkuth_code"):
            document["malkuth_code"] = outcome["malkuth_code"]

        if status == STATUS_EXECUTED:
            document["executed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if status == STATUS_DRY_RUN:
            document["would_call"] = outcome.get("would_call")

    if age_secs is not None:
        document["bundle_age_secs"] = round(age_secs, 1)

    document["writes"] = "none"
    return document

def _exit_for(status: str, code: str = "") -> int:
    if status in (STATUS_EXECUTED, STATUS_DRY_RUN):
        return EXIT_OK

    if status == STATUS_REFUSED:
        return EXIT_REFUSED

    return EXIT_FAILED

def main(argv: list[str] | None = None, *, stream_in: Any = None) -> int:
    args = _build_parser().parse_args(argv)
    raw = sys.stdin.read() if args.handoff == "-" else _read_file(args.handoff)

    if raw is None:
        return EXIT_USAGE

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return _fail(args, None, act_error("bundle_invalid", f"the handoff is not JSON: {e}"))

    bundle: handoff | None = None

    try:
        bundle = parse_handoff(payload)
        check_action_supported(bundle)
        check_justification(bundle)
    except act_error as error:
        return _fail(args, bundle, error)

    session = action_session([*_split(args.mcp_cmd)], timeout = args.call_timeout)

    try:
        with session:
            missing = sorted(ACTION_TOOLS - session.tools)

            if missing:
                return _fail(args, bundle, act_error("malkuth_unavailable", f"the MCP server does not advertise {missing}: it predates the action gate"))

            outcome = run_act(bundle, session, dry_run = args.dry_run)
    except act_error as error:
        return _fail(args, bundle, error)
    except (mcp_error, mcp_unavailable) as error:
        return _fail(args, bundle, act_error("malkuth_unavailable", str(error)))

    document = _result_document(bundle, status = outcome["status"], outcome = outcome, age_secs = time.time() - bundle.received_at)

    if args.json:
        print(json.dumps(document, ensure_ascii = False))
    else:
        _print_human(document)

    return _exit_for(outcome["status"])

#: Refusals that are really "the executor could not run", not "the platform said no".
FAILED_CODES = frozenset({"execution_failed", "gate_register_failed", "malkuth_unavailable", "malkuth_timeout"})
UNAVAILABLE_CODES = frozenset({"malkuth_unavailable", "malkuth_timeout"})

def _fail(args: argparse.Namespace, bundle: handoff | None, error: act_error) -> int:
    status = STATUS_FAILED if error.code in FAILED_CODES else STATUS_REFUSED
    document = _result_document(bundle, status = status, error = error, age_secs = None if bundle is None else time.time() - bundle.received_at)

    if args.json:
        print(json.dumps(document, ensure_ascii = False))
    else:
        print(f"拒绝 ({error.code}): {error.message}")

        if error.hint:
            print(f"  下一步: {error.hint}")

    if error.code in UNAVAILABLE_CODES:
        return EXIT_UNAVAILABLE

    return _exit_for(document["status"])

def _print_human(document: dict) -> None:
    target = document.get("target") or {}
    print(f"{document['status'].upper()} {document.get('action', '')} {target.get('run_id', '')}/{target.get('worker_id', '')}")

    if document.get("malkuth_code"):
        print(f"  malkuth: {document['malkuth_code']}")

    for check in document.get("checks", []):
        print(f"  [{check['step']}] ok={check['ok']} {check.get('detail', '')}")

    print("  写入: 无（审计由平台会话回填 scepter）")

def _split(command: str) -> list[str]:
    return shlex.split(command)

def _read_file(path: str) -> str | None:
    try:
        with open(path, encoding = "utf-8") as handle:
            return handle.read()
    except OSError as e:
        print(f"error: cannot read handoff {path!r}: {e}", file = sys.stderr)
        return None

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "celestia-devtools judge-fleet act", description = "Execute ONE already-approved judge action from a handoff bundle on stdin (or --handoff FILE). Registers the gate approval, runs exactly one restart/drain, prints one JSON result. Holds no scepter credential and writes no state.", epilog = "Exit codes: 0 executed (or a clean --dry-run) | 2 usage | 3 refused (preflight or gate) | 4 execution failed | 5 malkuth unavailable. The bundle is the operator's handoff (scepter Export output): it must carry proposal_id, gate_proposal_id, run_id, worker_id, action, verdict_code, codes, pid, starttime.")
    parser.add_argument("--handoff", default = "-", metavar = "FILE", help = "handoff bundle path; '-' (default) reads stdin, which keeps the gate token out of argv")
    parser.add_argument("--dry-run", action = "store_true", help = "stop after the preflight (no approval registered, nothing executed)")
    parser.add_argument("--json", action = "store_true", help = "machine-readable result")
    parser.add_argument("--call-timeout", type = float, default = 10.0, help = "per-call timeout in seconds (default 10)")
    parser.add_argument("--mcp-cmd", default = "malkuth mcp", help = "command that starts an MCP stdio server (default: 'malkuth mcp')")
    return parser
