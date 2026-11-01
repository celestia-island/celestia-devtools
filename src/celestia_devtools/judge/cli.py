#!/usr/bin/env python3
"""``celestia-devtools judge-fleet`` — read-only fleet judgement over MCP.

Background
==========

The wave-4 judge reads a malkuth fleet through its MCP read tools and states what the
numbers mean. PR-1 ships **library primitives + a bring-your-own-fleet demo**: the
judgement path is read-only, and the demo starts one supervision so the tool has a fleet
to look at.

What it can see (state this, do not paper over it)
==================================================

A malkuth server's run registry is **in-process** (8 runs). An external client therefore
cannot observe a fleet that somebody else holds: the only fleet this CLI can judge is one
it started itself through ``--supervise``, or one whose MCP session it owns (the
platform-side embedding, PR-2). Against a server that is already running something else,
the honest answer is "no fleet" — not an error and not a guess.

Status contract (four values; `failed` > `indeterminate` > `ok`)
==============================================================

* ``ok`` — every applicable predicate was evaluated and nothing fired. An empty registry
  is also ``ok``: "no fleet" is a real observation, not a missing one;
* ``indeterminate`` — nothing fired, but at least one predicate could not be evaluated
  (no probe URL, an unreadable log, a probe that never answered);
* ``failed`` — at least one predicate fired; the severity travels per verdict
  (``critical`` / ``suspicious``). A finding never reads as ``ok`` — that is the property
  a consumer gating on ``status == "ok"`` relies on;
* ``unavailable`` — nothing could be read at all (transport failure, non-JSON framing, a
  server without the read tools). This is the *absence* of a judgement: no verdicts, no
  counts, just the reason.

Exit codes: ``0`` a report was produced (including "no fleet" and "cannot judge")
| ``2`` usage error.

The three read tools are the only tools the judgement path may call
(:data:`celestia_devtools.judge.mcp_stdio.READ_ONLY_TOOLS`); the demo supervision is the
single documented exception and lives in :mod:`celestia_devtools.judge.demo_fleet`.
Actions (restart/drain) are PR-2 and need a target-bound approval chain (L61).
"""

from __future__ import annotations
import argparse
import json
import os
import shlex
import sys
import threading
import time
from collections.abc import Sequence
from celestia_devtools.judge import advice, demo_fleet, fleet
from celestia_devtools.judge.mcp_stdio import READ_ONLY_TOOLS, mcp_error, mcp_session, mcp_unavailable

DEFAULT_MCP_COMMAND = "malkuth mcp"

#: Endpoints a ``kind="both"`` probe call touches (healthz + readyz).
PROBE_ENDPOINTS = 2

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "celestia-devtools judge-fleet", description = "Read-only judgement of a malkuth MCP fleet (evidence + conclusions; no actions).", epilog = "Predicates: A1 readiness-down, A2 log-stall, B1 crash-loop, B2 phase-oscillation, " "B3 drain-stalled, C1 silent-hang, C2 spawn-failure. A3 (no-progress) is reserved " "for the platform-provided progress endpoint. Malkuth's run registry is in-process: " "this CLI only sees a fleet it starts with --supervise (or a session it owns). " "Status values: ok (everything evaluated, nothing fired), indeterminate (something was " "not evaluated), failed (a predicate fired), unavailable (nothing could be read). " "Exit codes: 0 a report was produced, 2 usage error.")
    parser.add_argument("--mcp-cmd", default = DEFAULT_MCP_COMMAND, help = "command that starts an MCP stdio server (default: 'malkuth mcp')")
    parser.add_argument("--ticks", type = int, default = 3, help = "number of samples (default 3, min 2)")
    parser.add_argument("--interval", type = float, default = 15.0, help = "seconds between samples (default 15)")
    parser.add_argument("--call-timeout", type = float, default = 10.0, help = "per-call timeout in seconds (default 10)")
    parser.add_argument("--logs-lines", type = int, default = 40, help = "log tail lines per sample (default 40, cap 500)")
    parser.add_argument("--probe-url", default = "", help = "optional HTTP base for readiness probes (enables A1/C1)")
    parser.add_argument("--probe-timeout", type = float, default = 5.0, help = "per-endpoint probe timeout in seconds (default 5; the call budget is 2x this + 2)")
    parser.add_argument("--probe-wait", type = float, default = 8.0, help = "with --supervise: seconds to wait for the demo readiness surface to answer (default 8)")
    parser.add_argument("--supervise", action = "append", default = [], metavar = "CMD", help = "DEMO PATH: supervise this command through the server and judge that fleet (repeatable; shlex-split)")
    parser.add_argument("--supervise-timeout", type = float, default = 0.0, help = "how long the demo supervision lives (default: max(10s, ticks*interval + 8s))")
    parser.add_argument("--crash-delta", type = int, default = 2, help = "B1 restart-count delta threshold (default 2)")
    parser.add_argument("--crash-absolute", type = int, default = 5, help = "B1 absolute restart count for a saturated loop (default 5)")
    parser.add_argument("--crash-rate", type = float, default = 0.02, help = "B1 restarts-per-second floor for the saturated branch (default 0.02)")
    parser.add_argument("--oscillation", type = int, default = 2, help = "B2 running->restarting transitions (default 2)")
    parser.add_argument("--stall-ticks", type = int, default = 3, help = "A2 identical-log-tail ticks (default 3)")
    parser.add_argument("--drain-ticks", type = int, default = 2, help = "B3 draining ticks before calling it stalled (default 2)")
    parser.add_argument("--hang-uptime", type = int, default = 300, help = "C1 long-uptime floor in seconds (default 300)")
    parser.add_argument("--json", action = "store_true", help = "machine-readable report")
    parser.add_argument("--read-only", dest = "read_only", action = "store_true", default = True, help = "only read (PR-1 default and only implemented mode)")
    parser.add_argument("--no-read-only", dest = "read_only", action = "store_false", help = "refuse: the action mode is PR-2 and needs target-bound approval (L61)")
    return parser

def limits_from_args(args: argparse.Namespace) -> fleet.thresholds:
    """Thresholds for a parsed CLI namespace (kept public for the test suite)."""
    return fleet.thresholds(crash_delta = args.crash_delta, crash_absolute = args.crash_absolute, crash_rate_per_sec = args.crash_rate, oscillation = args.oscillation, stall_ticks = args.stall_ticks, drain_ticks = args.drain_ticks, readiness_ticks = 2, hang_uptime_secs = args.hang_uptime)

def report_status(verdicts: Sequence[fleet.verdict], observation: fleet.observation) -> str:
    """The report's status. Findings escalate; missing signals never read as health.

    Order matters and is the contract PR-2 will gate on:

    1. ``failed`` — at least one predicate fired (any severity), because the definition of
       ``ok`` is "everything was evaluated and nothing fired";
    2. ``indeterminate`` — nothing fired, but at least one predicate could not be evaluated;
    3. ``ok`` — everything evaluated and nothing fired.

    ``unavailable`` is the separate "nothing could be read at all" outcome produced by
    :func:`_cannot_judge`; it is not a judgement, so it never contains verdicts.
    """

    if verdicts:
        return "failed"

    if observation.unevaluated:
        return "indeterminate"

    return "ok"

def _cannot_judge(reason: str, *, as_json: bool) -> int:
    """Fail-open report: no guess, just the reason (status ``unavailable``)."""

    if as_json:
        observe = advice.unavailable(reason)
        print(json.dumps({"status": "unavailable", "read_only": True, "cannot_judge": reason, "ticks": 0, "interval_secs": 0, "runs": 0, "workers": 0, "verdicts": [], "unevaluated": [], "suggested_actions": [observe.as_json()], "reserved_predicates": [fleet.A3_RESERVED]}, ensure_ascii = False))
    else:
        print(f"无法判定: {reason}")
        print("建议动作: observe (confidence none) — 无判定，不动作，也不当空 observation")

    return 0

def _probe_budget(args: argparse.Namespace) -> float:
    """Probe calls need more than the default call budget or a black-hole endpoint always times out."""
    return max(args.call_timeout, (PROBE_ENDPOINTS * max(args.probe_timeout, 0.1)) + 2.0)

def _collect(session: mcp_session, observation: fleet.observation, *, ticks: int, interval: float, logs_lines: int, probe_url: str = "", probe_timeout: float = 5.0, probe_budget: float = 12.0) -> str | None:
    """Sample the fleet; returns a fail-open reason on a fatal problem."""

    for index in range(max(ticks, 2)):
        if index > 0:
            time.sleep(max(interval, 0.0))

        try:
            payload = session.call_tool("malkuth_status")
        except (mcp_error, mcp_unavailable) as e:
            return f"tick {index}: status unavailable: {e}"

        try:
            runs = fleet.parse_status(payload)
        except fleet.schema_error as e:
            return f"tick {index}: status payload is not the documented shape: {e}"

        tick = fleet.tick(index = index, runs = runs)

        for run in runs:
            for worker in run.workers:
                if worker.phase != "running":
                    continue

                try:
                    logs = session.call_tool("malkuth_logs", {"run_id": run.run_id, "worker_id": worker.id, "lines": max(1, min(logs_lines, 500))})
                    tick.log_digests[(run.run_id, worker.id)] = fleet.log_digest(logs.get("text") if isinstance(logs, dict) else None)
                except (mcp_error, mcp_unavailable, fleet.schema_error) as e:
                    observation.note_unevaluated(("A2", "C1"), f"{run.run_id}/{worker.id}", f"logs unavailable: {e}")

        if probe_url:
            try:
                probe = session.call_tool("malkuth_probe", {"url": probe_url, "kind": "both", "timeout_secs": max(int(probe_timeout), 1)}, timeout = probe_budget)
                state = fleet.probe_state(probe)

                if state == "unreachable":
                    observation.note_unevaluated(("A1", "C1"), f"probe {probe_url}", "probe never answered (transport error): readiness was not evaluated")
                else:
                    tick.probe_ok = state == "ok"
            except (mcp_error, mcp_unavailable, fleet.schema_error) as e:
                observation.note_unevaluated(("A1", "C1"), f"probe {probe_url}", f"probe unavailable: {e}")
        else:
            for run in runs:
                for worker in run.workers:
                    observation.note_unevaluated(("A1", "C1"), f"{run.run_id}/{worker.id}", "no --probe-url: readiness predicates were not evaluated")

        observation.add(tick)

    return None

def _await_probe(session: mcp_session, url: str, *, timeout: float, probe_timeout: float, budget: float) -> bool:
    """Wait for the demo's readiness surface to answer before the first timed tick."""
    deadline = time.time() + max(timeout, 0.0)

    while time.time() < deadline:
        try:
            state = fleet.probe_state(session.call_tool("malkuth_probe", {"url": url, "kind": "healthz", "timeout_secs": max(int(probe_timeout), 1)}, timeout = budget))

            if state != "unreachable":
                return True
        except (mcp_error, mcp_unavailable, fleet.schema_error):
            time.sleep(0.2)
            continue

        time.sleep(0.2)

    return False

def _start_demo(session: mcp_session, args: argparse.Namespace) -> dict:
    """Start the demo supervision in a background thread; it blocks until malkuth drains."""
    timeout = args.supervise_timeout if args.supervise_timeout > 0 else max(10.0, (args.ticks * args.interval) + 8.0)
    state: dict = {}

    def drive() -> None:
        try:
            state["result"] = demo_fleet.start_supervision(session, [shlex.split(command) for command in args.supervise], timeout_secs = timeout)
        except (mcp_error, mcp_unavailable, ValueError) as error:
            state["error"] = str(error)

    thread = threading.Thread(target = drive, name = "demo-supervision", daemon = True)
    thread.start()
    state["thread"] = thread
    seen, workers = demo_fleet.wait_for_run(session)
    state["run_seen"] = seen
    state["workers"] = workers
    return state

def _render(args: argparse.Namespace, observation: fleet.observation, latest: fleet.tick) -> int:
    verdicts = fleet.evaluate(observation, limits_from_args(args))
    suggestions = advice.suggest(verdicts, observation)
    workers = [(run, worker) for run in latest.runs for worker in run.workers]
    status = report_status(verdicts, observation)

    if args.json:
        print(json.dumps({"status": status, "read_only": True, "ticks": len(observation.ticks), "interval_secs": args.interval, "runs": len(latest.runs), "workers": len(workers), "verdicts": [{"code": verdict.code, "severity": verdict.severity, "run_id": verdict.run_id, "worker_id": verdict.worker_id, "evidence": verdict.evidence} for verdict in verdicts], "unevaluated": [{"codes": list(item.codes), "scope": item.scope, "reason": item.reason} for item in observation.unevaluated], "suggested_actions": [item.as_json() for item in suggestions], "reserved_predicates": [fleet.A3_RESERVED]}, ensure_ascii = False))
        return 0

    if not latest.runs:
        print("无 fleet: the MCP server supervises no runs (its registry is per-process; pass --supervise to bring one)")

        if suggestions:
            for item in suggestions:
                print(f"  建议动作: {item.action} confidence={item.confidence}: {item.reason}")
        else:
            print("  建议动作: 无 (nothing fired, nothing to act on)")

        _print_unevaluated(observation)
        return 0

    print(f"fleet: {len(latest.runs)} run(s), {len(workers)} worker(s), {len(observation.ticks)} tick(s) over {args.interval * (len(observation.ticks) - 1):g}s")

    for run, worker in workers:
        print("  " + fleet.worker_line(run, worker))

    if verdicts:
        print(f"结论: 失败 ({len(verdicts)} 组命中 — status failed)")

        for verdict in verdicts:
            print(f"  [{verdict.severity}] {verdict.code} {verdict.run_id}/{verdict.worker_id} {json.dumps(verdict.evidence, ensure_ascii = False)}")
    elif observation.unevaluated:
        print(f"结论: 未判定 ({len(observation.unevaluated)} 组谓词未评估 — 未评估不等于健康)")
    else:
        print("结论: 健康 (every applicable predicate was evaluated and none fired)")

    _print_unevaluated(observation)

    if suggestions:
        print("建议动作 (advisory only; nothing executed):")

        for item in suggestions:
            target = f"{item.run_id}/{item.worker_id}" if item.run_id or item.worker_id else "(fleet)"
            print(f"  - {item.action} [{target}] confidence={item.confidence}: {item.reason}")
    else:
        print("建议动作: 无 (nothing fired)")

    print(f"  保留未实现: {fleet.A3_RESERVED}")
    return 0

def _print_unevaluated(observation: fleet.observation) -> None:
    for item in observation.unevaluated:
        codes = ",".join(item.codes)
        print(f"  未评估 {codes} [{item.scope}]: {item.reason}")

def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.read_only:
        print("refusing: the action mode is not implemented; restart/drain need a target-bound " "approval chain first (L61). PR-1 is read-only by construction.", file = sys.stderr)
        return 2

    if args.ticks < 2:
        print("error: --ticks must be at least 2 (single-sample judges cannot see movement)", file = sys.stderr)
        return 2

    if args.interval < 0:
        print("error: --interval must not be negative", file = sys.stderr)
        return 2

    command = shlex.split(args.mcp_cmd)
    observation = fleet.observation()
    session = mcp_session(command, timeout = args.call_timeout)
    demo: dict = {}
    leftovers: set[int] = set()

    try:
        session.open()
        missing = sorted(READ_ONLY_TOOLS - session.tools)

        if missing:
            return _cannot_judge(f"MCP server {args.mcp_cmd!r} does not advertise {missing}; " "it predates the wave-4 read arms, so no judgement is possible", as_json = args.json)

        if args.supervise:
            demo = _start_demo(session, args)

            if demo.get("error"):
                return _cannot_judge(f"--supervise failed: {demo['error']}", as_json = args.json)

            if not demo.get("run_seen"):
                return _cannot_judge(f"--supervise started but no run appeared within the settle window ({args.supervise_timeout or 'auto'}s)", as_json = args.json)

            # Snapshot the descendants at TEARDOWN (below), not here: a supervisor-shaped demo
            # command spawns its own children a moment after the run appears, and an early
            # snapshot would miss exactly the grandchildren that leak.
            demo["worker_pids"] = list(demo["workers"])

        if args.probe_url:
            _await_probe(session, args.probe_url, timeout = args.probe_wait, probe_timeout = args.probe_timeout, budget = _probe_budget(args))

        failure = _collect(session, observation, ticks = args.ticks, interval = args.interval, logs_lines = args.logs_lines, probe_url = args.probe_url, probe_timeout = args.probe_timeout, probe_budget = _probe_budget(args))

        if failure is not None:
            return _cannot_judge(failure, as_json = args.json)
    except (mcp_error, mcp_unavailable) as e:
        return _cannot_judge(str(e), as_json = args.json)
    finally:
        # Snapshot BEFORE the join: the join waits for malkuth's drain, and by then the
        # demo supervisor is dead and its pods have been reparented to init — unreachable
        # by a ppid walk. Capturing while it is still alive is the only way to know them.
        for pid in demo.get("worker_pids", []):
            leftovers |= demo_fleet.descendants(pid)

        if demo.get("thread") is not None:
            demo["thread"].join(timeout = (args.supervise_timeout or max(10.0, (args.ticks * args.interval) + 8.0)) + 5.0)

        for pid in demo.get("worker_pids", []):
            leftovers |= demo_fleet.descendants(pid)

        session.close()
        # The supervised workers themselves too: malkuth reaps them on shutdown, but a slow
        # child can outlive the session close by a moment; the demo's own processes should
        # not be left waiting for that.
        leftovers |= {int(pid) for pid in demo.get("worker_pids", []) if isinstance(pid, int)}
        # A pid from the status payload is a hint, not an identity: the process may be gone
        # and the number recycled. Only a process whose cmdline still matches a demo command
        # is terminated (an empty pid list never reaches the killer).
        expected = [os.path.realpath(shlex.split(command)[0]) for command in args.supervise if shlex.split(command)]

        if leftovers:
            demo_fleet.terminate(sorted(leftovers), expect = expected)

        if args.supervise:
            # Not gated on `worker_pids`: malkuth registers the run before it spawns, so the
            # first status can legitimately show `pid: null` and the demo would skip cleanup
            # entirely. The sweep is identity- and cwd-scoped, so it is safe to always run.
            commands = [shlex.split(command) for command in args.supervise]

            for _ in range(4):
                demo_fleet.sweep_abandoned(commands)
                time.sleep(0.2)

    latest = observation.latest()
    assert latest is not None  # _collect guarantees at least one tick
    return _render(args, observation, latest)

if __name__ == "__main__":
    raise SystemExit(main())
