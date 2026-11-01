"""Advisory action suggestions (wave-4 action slice, phase A: nothing executes).

This module answers one question: *if* a human or an approved actor were to act on this
report, what is the smallest sensible action and why? It answers with data only — it has no
transport, no MCP session and no import of the supervision module, so a suggestion can never
turn into a call by construction.

Mapping (deliberately opinionated and small):

===========  =========================  ==========================================
Trigger      Suggested action            Why
===========  =========================  ==========================================
``ok``       *none*                      nothing fired; nothing to do
``B1`` crit  ``restart``                 a crash loop needs a fresh child, not a drain
``B3`` crit  ``drain``                   the run is already draining and stuck; escalate
``C1`` crit  ``restart`` (or ``drain``   a hung worker: restart unless the run is
             when already draining)      already on its way out
``C2`` crit  ``notify``                  spawn failure is usually configuration — a
                                         restart would loop the same failure
suspicious   ``notify``                  worth a human look, not an automated action
unevaluated  ``observe``                 no action possible on an unjudged signal
unavailable  ``observe``                 there is no judgement at all
===========  =========================  ==========================================

Confidence is a mechanical tier, not a probability: the gate for an automated action is
``status == "failed"`` **and** a ``critical`` verdict, which this module reports as
``high``; a suspicion is ``low``; "we could not judge" is ``none``. Phase A stops here —
executing anything requires the target-bound approval chain and the pid+starttime check
that phase B adds.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from celestia_devtools.judge import fleet

ACTION_OBSERVE = "observe"
ACTION_NOTIFY = "notify"
ACTION_RESTART = "restart"
ACTION_DRAIN = "drain"
ACTION_NONE = "none"

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

#: The fixed mapping for critical findings. C1 is resolved separately (drain state).
_CRITICAL_ACTIONS = {"B1": ACTION_RESTART, "B3": ACTION_DRAIN, "C2": ACTION_NOTIFY}

@dataclass(frozen = True)
class suggestion:
    """One advisory row: what to do, how sure, why, and the evidence it rests on."""

    run_id: str
    worker_id: str
    action: str
    confidence: str
    reason: str
    evidence: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "worker_id": self.worker_id, "action": self.action, "confidence": self.confidence, "reason": self.reason, "evidence": self.evidence}

def _worker_evidence(run: fleet.run_state, worker: fleet.worker_state) -> dict:
    """The key fields a reader needs to sanity-check a suggestion without a second call."""
    return {"phase": worker.phase, "pid": worker.pid, "uptime_secs": worker.uptime_secs, "restart_count": worker.restart_count, "last_error": worker.last_error, "draining": run.draining, "drain_kind": run.drain_kind}

def _critical_action(code: str, run: fleet.run_state) -> tuple[str, str]:
    if code == "C1":
        if run.draining:
            return ACTION_DRAIN, "silent hang while the run is already draining: escalate the drain instead of restarting a worker on its way out"

        return ACTION_RESTART, "silent hang (down readiness, long uptime, frozen log tail): a fresh child is the smallest action"

    action = _CRITICAL_ACTIONS.get(code)

    if action is None:
        return ACTION_NOTIFY, f"unmapped critical predicate {code}: escalate to a human instead of guessing an action"

    reason = {"B1": "crash loop: restart is the smallest action that can leave the loop (a drain would end the run)", "B3": "drain is not completing: the next rung is an escalated drain, not a restart", "C2": "spawn failure loop: configuration, not a crashed child — restarting would reproduce it"}[code]
    return action, reason

def suggest(verdicts: list[fleet.verdict], observation: fleet.observation) -> list[suggestion]:
    """Map the report to advisory suggestions; never performs anything."""
    latest = observation.latest()
    runs = {run.run_id: run for run in latest.runs} if latest is not None else {}
    workers = {(run.run_id, worker.id): worker for run in runs.values() for worker in run.workers}
    rows: list[suggestion] = []
    handled: set[tuple[str, str]] = set()

    for verdict in verdicts:
        key = (verdict.run_id, verdict.worker_id)
        run = runs.get(verdict.run_id)
        worker = workers.get(key)

        if verdict.severity == "critical":
            action, reason = _critical_action(verdict.code, run) if run is not None else (ACTION_NOTIFY, "run disappeared between sampling and reporting: escalate")
            rows.append(suggestion(run_id = verdict.run_id, worker_id = verdict.worker_id, action = action, confidence = CONFIDENCE_HIGH, reason = f"{reason} (predicate {verdict.code}), evidence={verdict.evidence}", evidence = {"verdict": {"code": verdict.code, "severity": verdict.severity, "evidence": verdict.evidence}, "worker": _worker_evidence(run, worker) if (run is not None) and (worker is not None) else {}}))
            handled.add(key)

    for verdict in verdicts:
        key = (verdict.run_id, verdict.worker_id)

        if (verdict.severity == "critical") or (key in handled):
            continue

        run = runs.get(verdict.run_id)
        worker = workers.get(key)
        rows.append(suggestion(run_id = verdict.run_id, worker_id = verdict.worker_id, action = ACTION_NOTIFY, confidence = CONFIDENCE_LOW, reason = f"suspicious predicate {verdict.code}: worth a human look, not an automated action; evidence={verdict.evidence}", evidence = {"verdict": {"code": verdict.code, "severity": verdict.severity, "evidence": verdict.evidence}, "worker": _worker_evidence(run, worker) if (run is not None) and (worker is not None) else {}}))
        handled.add(key)

    rows = _merge(rows)

    if not rows:
        for item in observation.unevaluated:
            rows.append(suggestion(run_id = "", worker_id = item.scope, action = ACTION_OBSERVE, confidence = CONFIDENCE_NONE, reason = f"not evaluated: {item.reason} — no action is possible on an unjudged signal", evidence = {"codes": list(item.codes)}))

    return rows

def _merge(rows: list[suggestion]) -> list[suggestion]:
    """Collapse duplicates that point at the same target with the same action.

    A single worker can trip more than one predicate (C1 and B3 both say "drain" for a
    draining hung worker); two rows would read as two actions.
    """
    merged: dict[tuple[str, str, str], suggestion] = {}

    for row in rows:
        key = (row.run_id, row.worker_id, row.action)
        current = merged.get(key)

        if current is None:
            merged[key] = row
            continue

        codes = {current.evidence.get("verdict", {}).get("code"), row.evidence.get("verdict", {}).get("code")}
        merged[key] = suggestion(run_id = current.run_id, worker_id = current.worker_id, action = current.action, confidence = current.confidence, reason = f"{current.reason} | also: {row.reason}", evidence = {"verdicts": [current.evidence.get("verdict"), row.evidence.get("verdict")], "codes": sorted(code for code in codes if code), "worker": current.evidence.get("worker", {})})

    return list(merged.values())

def unavailable(reason: str) -> suggestion:
    """The ``unavailable`` report: no judgement exists, so nothing may be acted on."""
    return suggestion(run_id = "", worker_id = "", action = ACTION_OBSERVE, confidence = CONFIDENCE_NONE, reason = f"no judgement was produced ({reason}); an unavailable report is not an empty observation and never triggers an action", evidence = {"cannot_judge": reason})
