"""Mechanical fleet predicates over malkuth's read-only MCP payloads.

Wave 4, PR-1: this module decides what a fleet's numbers *look like*; it never
decides what to do about them. Every verdict is evidence plus an advisory
``suggest`` string — the tool prints it, nothing executes.

The predicate table (kept in lockstep with the wave-4 design note):

========  =========================================================  ==========================
Code      Mechanical condition                                       Data
========  =========================================================  ==========================
A1        readiness endpoint non-2xx/3xx for N ticks while running   ``malkuth_probe``
A2        identical log tail for N ticks while running + uptime up   ``malkuth_logs``
A3        reserved (needs a platform-provided progress endpoint)     —
B1        ``restart_count`` rose by >= K inside the window           ``malkuth_status``
B2        ``running -> restarting`` transitions >= K inside window   ``malkuth_status``
B3        run draining for N ticks and a worker still running        ``malkuth_status``
C1        readiness down N ticks + long uptime + frozen log tail     ``probe`` + ``logs``
C2        ``last_error`` set while ``restart_count`` rises            ``malkuth_status``
========  =========================================================  ==========================

Two facts from the malkuth implementation shape the table and are stated here so a
reader does not expect more than the payload can give:

* ``last_error`` only covers spawn/wait failures — a normal non-zero exit leaves it
  null, so "crashed" is inferred from ``restart_count``/phase, never from an exit code.
* a worker's log ring has no monotonic counter, so "log moved" is decided by hashing
  the tail the tool returns (a saturated ring still changes when output continues).
"""

from __future__ import annotations
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

PHASES = frozenset({"starting", "running", "restarting", "draining", "stopped"})
SEVERITIES = ("ok", "suspicious", "critical")

#: A3 is deliberately unimplemented: judging "no progress" needs a progress signal the
#: platform owns (queue depth, processed counter). Kept visible instead of silently
#: dropped, because a missing row is easier to forget than a named gap.
A3_RESERVED = "A3 reserved: needs a platform-provided progress endpoint (PR-3)"

class schema_error(ValueError):
    """The payload does not match the documented malkuth shape (criterion: fail open)."""

@dataclass(frozen = True)
class worker_state:
    id: str
    kind: str
    phase: str
    pid: int | None
    uptime_secs: int
    restart_policy: str
    restart_count: int
    last_error: str | None

@dataclass(frozen = True)
class run_state:
    run_id: str
    finished: bool
    draining: bool
    drain_kind: str | None
    workers: tuple[worker_state, ...]
    #: Age of the run in seconds. Needed because a crash loop can saturate *before*
    #: the observation window opens: a worker that restarted 100 times in 4 seconds
    #: shows a constant restart_count across ticks, and a delta-only predicate would
    #: call it healthy.
    elapsed_secs: int = 0

@dataclass
class tick:
    """One sample: the status payload for that tick plus what the read tools returned."""

    index: int
    runs: tuple[run_state, ...] = ()
    log_digests: dict[tuple[str, str], str] = field(default_factory = dict)
    probe_ok: bool | None = None
    probe_detail: str = ""

@dataclass(frozen = True)
class unevaluated:
    """A predicate (or a set of them) that could not be evaluated, and why.

    The report distinguishes this from "evaluated and found nothing": an unjudged signal
    must never be reported as health.
    """

    codes: tuple[str, ...]
    scope: str
    reason: str

@dataclass(frozen = True)
class verdict:
    code: str
    severity: str
    run_id: str
    worker_id: str
    evidence: dict[str, Any]
    suggest: str

@dataclass(frozen = True)
class thresholds:
    crash_delta: int = 2
    #: Absolute restarts per run age: catches a storm that saturated before the
    #: window opened (delta alone sees a frozen counter and calls it quiet).
    crash_absolute: int = 5
    crash_rate_per_sec: float = 0.02
    oscillation: int = 2
    stall_ticks: int = 3
    #: A floor on the worker's age before "static log tail" means anything: three
    #: identical tails inside the first seconds of a process is a warm-up, not a stall.
    stall_min_uptime_secs: int = 30
    drain_ticks: int = 2
    readiness_ticks: int = 2
    hang_uptime_secs: int = 300

def _require(mapping: dict, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if key not in mapping:
        raise schema_error(f"{where}: missing field {key!r}")

    value = mapping[key]

    if (kind is not None) and not isinstance(value, kind):
        raise schema_error(f"{where}: field {key!r} has type {type(value).__name__}")

    return value

def _optional(mapping: dict, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    value = _require(mapping, key, (kind, type(None)) if isinstance(kind, type) else kind + (type(None),), where)
    return value

def parse_worker(raw: Any, where: str) -> worker_state:
    if not isinstance(raw, dict):
        raise schema_error(f"{where}: worker is not an object")

    phase = _require(raw, "phase", str, where)

    if phase not in PHASES:
        raise schema_error(f"{where}: unknown phase {phase!r}")

    return worker_state(id = _require(raw, "id", str, where), kind = _require(raw, "kind", str, where), phase = phase, pid = _optional(raw, "pid", int, where), uptime_secs = _require(raw, "uptime_secs", int, where), restart_policy = _require(raw, "restart_policy", str, where), restart_count = _require(raw, "restart_count", int, where), last_error = _optional(raw, "last_error", str, where))

def parse_status(payload: Any) -> tuple[run_state, ...]:
    """Parse a ``malkuth_status`` payload; any deviation is a :class:`schema_error`."""

    if not isinstance(payload, dict):
        raise schema_error("status payload is not an object")

    if payload.get("ok") is not True:
        raise schema_error(f"status reported not-ok: {payload.get('code') or payload}")

    runs_raw = payload.get("runs")

    if not isinstance(runs_raw, list):
        raise schema_error("status payload has no runs array")

    runs: list[run_state] = []

    for run_index, run_raw in enumerate(runs_raw):
        where = f"runs[{run_index}]"

        if not isinstance(run_raw, dict):
            raise schema_error(f"{where}: run is not an object")

        workers_raw = run_raw.get("workers")

        if not isinstance(workers_raw, list):
            raise schema_error(f"{where}: no workers array")

        workers = tuple(parse_worker(worker, f"{where}.workers[{worker_index}]")for worker_index, worker in enumerate(workers_raw))
        runs.append(run_state(run_id = _require(run_raw, "run_id", str, where), finished = _require(run_raw, "finished", bool, where), draining = _require(run_raw, "draining", bool, where), drain_kind = _optional(run_raw, "drain_kind", str, where), workers = workers, elapsed_secs = _require(run_raw, "elapsed_secs", int, where)))

    return tuple(runs)

def log_digest(text: Any) -> str:
    """Stable digest of the tail text a ``malkuth_logs`` call returned."""

    if not isinstance(text, str):
        raise schema_error("logs payload text is not a string")

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def probe_state(payload: Any) -> str:
    """Classify one ``malkuth_probe`` payload as ``ok`` | ``down`` | ``unreachable``.

    The three outcomes are not the same thing and must not collapse into one boolean: a
    black-holed endpoint (transport error, no HTTP status) is not evidence about readiness,
    while a 503 *is*. ``unreachable`` therefore feeds the "not evaluated" state instead of
    a readiness verdict.
    """

    if not isinstance(payload, list) or not payload:
        raise schema_error("probe payload is not a non-empty array")

    states = set()

    for entry in payload:
        if not isinstance(entry, dict):
            raise schema_error("probe entry is not an object")

        status = entry.get("status")

        if (entry.get("ok") is True) and isinstance(status, int) and (200 <= status < 400):
            states.add("ok")
        elif isinstance(entry.get("status"), int):
            states.add("down")
        else:
            states.add("unreachable")

    if "unreachable" in states:
        return "unreachable"

    return "ok" if states == {"ok"} else "down"

def probe_ok(payload: Any) -> bool:
    """A probe counts as ok when every endpoint answered 2xx/3xx."""
    return probe_state(payload) == "ok"

class observation:
    """The tick sequence one judgement run collected."""

    def __init__(self) -> None:
        self.ticks: list[tick] = []
        self.unevaluated: list[unevaluated] = []

    def add(self, tick: tick) -> None:
        self.ticks.append(tick)

    def note_unevaluated(self, codes: Sequence[str], scope: str, reason: str) -> None:
        item = unevaluated(tuple(codes), scope, reason)

        if item not in self.unevaluated:
            self.unevaluated.append(item)

    def latest(self) -> tick | None:
        return self.ticks[-1] if self.ticks else None

    def worker_history(self, run_id: str, worker_id: str) -> list[worker_state]:
        history: list[worker_state] = []

        for tick in self.ticks:
            for run in tick.runs:
                if run.run_id != run_id:
                    continue

                for worker in run.workers:
                    if worker.id == worker_id:
                        history.append(worker)

        return history

    def log_digest_history(self, run_id: str, worker_id: str) -> list[str]:
        return [tick.log_digests[(run_id, worker_id)]for tick in self.ticks if (run_id, worker_id) in tick.log_digests]

    def probe_history(self) -> list[bool]:
        return [tick.probe_ok for tick in self.ticks if tick.probe_ok is not None]

def _advisory(action: str) -> str:
    return f"suggest: {action} (advisory only; no action executed — actions need target-bound approval, L61)"

def b1_crash_loop(run: run_state, worker_id: str, history: list[worker_state], limits: thresholds) -> verdict | None:
    """A crash loop, judged either by movement inside the window or by rate.

    Two shapes must both be caught:

    * the loop is ongoing — ``restart_count`` climbs between samples (delta);
    * the loop already saturated — a 4-second run with 100 restarts and a frozen
      counter in cooldown. A delta-only predicate calls that healthy, which is why
      the absolute/rate branch exists. The rate keeps a worker that legitimately
      restarted a handful of times over days from being escalated.
    """

    if not history:
        return None

    counts = [worker.restart_count for worker in history]
    delta = max(counts) - min(counts)
    latest = history[-1]
    elapsed = max(run.elapsed_secs, 1)
    rate = latest.restart_count / elapsed
    moved = delta >= limits.crash_delta
    saturated = (latest.restart_count >= limits.crash_absolute) and (rate >= limits.crash_rate_per_sec)

    if not (moved or saturated):
        return None

    return verdict(code = "B1", severity = "critical", run_id = run.run_id, worker_id = worker_id, evidence = {"restart_counts": counts, "delta": delta, "delta_threshold": limits.crash_delta, "restart_count": latest.restart_count, "run_age_secs": run.elapsed_secs, "restarts_per_sec": round(rate, 3), "phase": latest.phase}, suggest = _advisory("restart"))

def b2_phase_oscillation(run_id: str, worker_id: str, history: list[worker_state], limits: thresholds) -> verdict | None:
    transitions = 0

    for previous, current in zip(history, history[1:], strict = False):
        if (previous.phase == "running") and (current.phase == "restarting"):
            transitions += 1

    if transitions < limits.oscillation:
        return None

    return verdict(code = "B2", severity = "suspicious", run_id = run_id, worker_id = worker_id, evidence = {"running_to_restarting": transitions, "phases": [worker.phase for worker in history]}, suggest = _advisory("restart"))

def b3_drain_stalled(run: run_state, draining_ticks: int, limits: thresholds) -> verdict | None:
    if not run.draining or (draining_ticks < limits.drain_ticks):
        return None

    stuck = [worker.id for worker in run.workers if worker.phase in ("running", "draining")]

    if not stuck:
        return None

    return verdict(code = "B3", severity = "critical", run_id = run.run_id, worker_id = ",".join(stuck), evidence = {"drain_kind": run.drain_kind, "draining_ticks": draining_ticks, "stuck_workers": stuck}, suggest = _advisory("drain"))

def a1_readiness_down(run: run_state, worker: worker_state, failing_ticks: int, limits: thresholds) -> verdict | None:
    if (worker.phase != "running") or (failing_ticks < limits.readiness_ticks):
        return None

    return verdict(code = "A1", severity = "suspicious", run_id = run.run_id, worker_id = worker.id, evidence = {"failing_readiness_ticks": failing_ticks, "threshold": limits.readiness_ticks}, suggest = _advisory("observe"))

def a2_log_stall(run_id: str, worker_id: str, digests: list[str], uptimes: list[int], limits: thresholds) -> verdict | None:
    if len(digests) < limits.stall_ticks:
        return None

    if not uptimes or (uptimes[-1] < limits.stall_min_uptime_secs):
        return None

    tail = digests[-limits.stall_ticks :]

    if len(set(tail)) != 1:
        return None

    if (len(uptimes) < 2) or (uptimes[-1] <= uptimes[0]):
        return None

    return verdict(code = "A2", severity = "suspicious", run_id = run_id, worker_id = worker_id, evidence = {"identical_ticks": len(tail), "digest": tail[-1], "uptime_secs": uptimes[-1]}, suggest = _advisory("observe"))

def c1_silent_hang(run: run_state, worker: worker_state, failing_ticks: int, digests: list[str], limits: thresholds) -> verdict | None:
    if worker.phase != "running":
        return None

    if worker.uptime_secs < limits.hang_uptime_secs:
        return None

    if failing_ticks < limits.readiness_ticks:
        return None

    if (len(digests) < limits.stall_ticks) or (len(set(digests[-limits.stall_ticks :])) != 1):
        return None

    return verdict(code = "C1", severity = "critical", run_id = run.run_id, worker_id = worker.id, evidence = {"failing_readiness_ticks": failing_ticks, "uptime_secs": worker.uptime_secs, "frozen_log_ticks": limits.stall_ticks}, suggest = _advisory("restart"))

def c2_spawn_failure(run_id: str, worker_id: str, history: list[worker_state], limits: thresholds) -> verdict | None:
    if len(history) < 2:
        return None

    latest = history[-1]

    if latest.last_error is None:
        return None

    if latest.restart_count <= history[0].restart_count:
        return None

    return verdict(code = "C2", severity = "critical", run_id = run_id, worker_id = worker_id, evidence = {"last_error": latest.last_error, "restart_count": latest.restart_count, "restart_delta": latest.restart_count - history[0].restart_count}, suggest = _advisory("observe"))

def _draining_ticks(observation: observation, run_id: str) -> int:
    return sum(1 for tick in observation.ticks if any((run.run_id == run_id) and run.draining for run in tick.runs))

def evaluate(observation: observation, limits: thresholds | None = None) -> list[verdict]:
    """Run every predicate over the observation; returns findings only."""
    limits = limits or thresholds()
    latest = observation.latest()

    if latest is None:
        return []

    verdicts: list[verdict] = []
    probe_history = observation.probe_history()
    failing_ticks = 0

    for ok in reversed(probe_history):
        if ok:
            break

        failing_ticks += 1

    for run in latest.runs:
        drained = _draining_ticks(observation, run.run_id)
        stalled = b3_drain_stalled(run, drained, limits)

        if stalled is not None:
            verdicts.append(stalled)

        for worker in run.workers:
            history = observation.worker_history(run.run_id, worker.id)
            digests = observation.log_digest_history(run.run_id, worker.id)
            uptimes = [entry.uptime_secs for entry in history]
            candidates = [b1_crash_loop(run, worker.id, history, limits), b2_phase_oscillation(run.run_id, worker.id, history, limits), c2_spawn_failure(run.run_id, worker.id, history, limits), a1_readiness_down(run, worker, failing_ticks, limits), a2_log_stall(run.run_id, worker.id, digests, uptimes, limits), c1_silent_hang(run, worker, failing_ticks, digests, limits)]
            verdicts.extend(verdict for verdict in candidates if verdict is not None)

    verdicts.sort(key = lambda verdict: (SEVERITIES.index(verdict.severity) * -1, verdict.code, verdict.run_id, verdict.worker_id))
    return verdicts

def worker_line(run: run_state, worker: worker_state) -> str:
    pid = "none" if worker.pid is None else str(worker.pid)
    error = "" if worker.last_error is None else f" last_error={worker.last_error!r}"
    return f"{run.run_id}/{worker.id} kind={worker.kind} phase={worker.phase} pid={pid} " f"uptime={worker.uptime_secs}s restarts={worker.restart_count} policy={worker.restart_policy}{error}"
