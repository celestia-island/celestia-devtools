"""Advisory action suggestions (wave-4 action slice, phase A: nothing executes).

This module is the **single source** of suggested actions: `fleet` produces verdicts and
nothing else. It answers one question: *if* a human or an approved actor were to act on this
report, what is the smallest sensible action and why? It answers with data only — no
transport, no MCP session, no import of the supervision module, so a suggestion cannot turn
into a call by construction.

One action per target, chosen by this fixed priority
====================================================

1. ``C2`` (spawn/wait failure with the worker never getting up) → ``notify``. A spawn failure is usually
   configuration; restarting reproduces it, draining ends the run — so escalate to a human.
2. ``B3`` (run draining, worker still running) → ``drain``. The run is already on its way
   out and stuck: the next rung is an escalated drain, not a restart.
3. ``C1`` while the run is already draining → ``drain`` (restarting a worker on its way out
   is busy-work).
4. ``B1`` / ``C1`` → ``restart``: a fresh child is the smallest action that can leave a
   crash loop or a silent hang.
5. any ``suspicious`` verdict → ``notify``.
6. **unevaluated signals → ``observe``, always** (even alongside a verdict): an unjudged
   signal cannot be acted on, and dropping it would hide that the report is partial.
7. ``unavailable`` → ``observe``: there is no judgement, and it is not an empty observation.

Priority matters: a worker can trip several predicates at once (B1+C2, C1+B3), and two
mutually exclusive actions for one target would be worse than none.

Confidence is a mechanical tier, not a probability: ``high`` for a critical finding,
``low`` for a suspicion, ``none`` for "could not judge". Phase A stops here — executing
anything requires the target-bound approval chain and the pid+starttime check that phase B
adds.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from celestia_devtools.judge import fleet

ACTION_OBSERVE = "observe"
ACTION_NOTIFY = "notify"
ACTION_RESTART = "restart"
ACTION_DRAIN = "drain"

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

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

def decide(codes: set[str], run: fleet.run_state | None) -> tuple[str, str, str]:
    """Priority table: ``(action, confidence, why)`` for one target's codes."""
    draining = (run is not None) and run.draining

    if "C2" in codes:
        return ACTION_NOTIFY, CONFIDENCE_HIGH, "spawn failure loop (C2): configuration, not a crashed child — restarting would reproduce it, so escalate to a human"

    if "B3" in codes:
        return ACTION_DRAIN, CONFIDENCE_HIGH, "drain is not completing (B3): the next rung is an escalated drain, not a restart"

    if ("C1" in codes) and draining:
        return ACTION_DRAIN, CONFIDENCE_HIGH, "silent hang (C1) while the run is already draining: escalate the drain instead of restarting a worker on its way out"

    if codes & {"B1", "C1"}:
        return ACTION_RESTART, CONFIDENCE_HIGH, "crash loop (B1) or silent hang (C1): a fresh child is the smallest action"

    if not codes:
        return ACTION_OBSERVE, CONFIDENCE_NONE, "no predicate fired for this target: nothing to suggest"

    return ACTION_NOTIFY, CONFIDENCE_LOW, "suspicious signal: worth a human look, not an automated action"

def suggest(verdicts: list[fleet.verdict], observation: fleet.observation) -> list[suggestion]:
    """Map the report to advisory suggestions; never performs anything."""
    latest = observation.latest()
    runs = {run.run_id: run for run in latest.runs} if latest is not None else {}
    workers = {(run.run_id, worker.id): worker for run in runs.values() for worker in run.workers}
    grouped: dict[tuple[str, str], list[fleet.verdict]] = {}

    for verdict in verdicts:
        grouped.setdefault((verdict.run_id, verdict.worker_id), []).append(verdict)

    rows: list[suggestion] = []

    for (run_id, worker_id), items in grouped.items():
        run = runs.get(run_id)
        worker = workers.get((run_id, worker_id))
        codes = {item.code for item in items}
        action, confidence, reason = decide(codes, run)
        evidence = {"codes": sorted(codes), "verdicts": [{"code": item.code, "severity": item.severity, "evidence": item.evidence} for item in items], "worker": _worker_evidence(run, worker) if (run is not None) and (worker is not None) else {}}
        rows.append(suggestion(run_id = run_id, worker_id = worker_id, action = action, confidence = confidence, reason = reason, evidence = evidence))

    # An unjudged signal is reported even when a verdict exists: it must never be swallowed
    # (the JSON report lists it, and the advice must not read as "fully evaluated").
    for item in observation.unevaluated:
        rows.append(suggestion(run_id = "", worker_id = item.scope, action = ACTION_OBSERVE, confidence = CONFIDENCE_NONE, reason = f"not evaluated: {item.reason} — no action is possible on an unjudged signal", evidence = {"codes": list(item.codes)}))

    return rows

def unavailable(reason: str) -> suggestion:
    """The ``unavailable`` report: no judgement exists, so nothing may be acted on."""
    return suggestion(run_id = "", worker_id = "", action = ACTION_OBSERVE, confidence = CONFIDENCE_NONE, reason = f"no judgement was produced ({reason}); an unavailable report is not an empty observation and never triggers an action", evidence = {"cannot_judge": reason})
