#!/usr/bin/env python3
"""``celestia-devtools judge-fleet`` — read-only fleet judgement over MCP.

Background
==========

The wave-4 judge reads a malkuth fleet through its MCP read tools and states what the
numbers mean. PR-1 is deliberately **read-only**: it can observe, it cannot act. The
mechanical predicate table lives in :mod:`celestia_devtools.judge.fleet`; this module
is the tick loop, the fail-open plumbing and the report.

What it can see
===============

The fleet a ``malkuth mcp`` server exposes is the set of runs **that server** started
(its registry is in-process, 8 runs). It is not the machine's systemd units: those are
a different process, so the judge reports "no fleet" for them rather than pretending.

Fail-open contract
==================

The judge never guesses. A transport timeout, an unparsable payload, a missing field,
or a server that does not advertise the read tools all end the same way: a
``cannot judge`` report with the reason and a zero exit status. Failures inside one
auxiliary read (logs, probe) degrade that predicate to "not judged" and are listed with
the other indeterminate reasons.

Exit codes: ``0`` a report was produced (including "no fleet" and "cannot judge")
| ``2`` usage error.

The three read tools are the only calls in the code path (enforced by
``READ_ONLY_TOOLS`` in :mod:`celestia_devtools.judge.mcp_stdio`); actions are PR-2 and
need a target-bound approval chain (L61).
"""

from __future__ import annotations
import argparse
import json
import sys
import time
from collections.abc import Sequence
from celestia_devtools.judge import fleet
from celestia_devtools.judge.mcp_stdio import READ_ONLY_TOOLS, mcp_error, mcp_session, mcp_unavailable

DEFAULT_MCP_COMMAND = ("malkuth", "mcp")

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "celestia-devtools judge-fleet", description = "Read-only judgement of a malkuth MCP fleet (evidence + conclusions; no actions).", epilog = "Predicates: A1 readiness-down, A2 log-stall, B1 crash-loop, B2 phase-oscillation, " "B3 drain-stalled, C1 silent-hang, C2 spawn-failure. A3 (no-progress) is reserved " "for the platform-provided progress endpoint.")
    parser.add_argument("--mcp-cmd", default = " ".join(DEFAULT_MCP_COMMAND), help = "command that starts an MCP stdio server (default: 'malkuth mcp')")
    parser.add_argument("--ticks", type = int, default = 3, help = "number of samples (default 3, min 2)")
    parser.add_argument("--interval", type = float, default = 15.0, help = "seconds between samples (default 15)")
    parser.add_argument("--call-timeout", type = float, default = 10.0, help = "per-call timeout in seconds (default 10)")
    parser.add_argument("--logs-lines", type = int, default = 40, help = "log tail lines per sample (default 40, cap 500)")
    parser.add_argument("--probe-url", default = "", help = "optional HTTP base for readiness probes (enables A1/C1)")
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

def _cannot_judge(reason: str, *, as_json: bool) -> int:
    """Fail-open report: no guess, just the reason."""

    if as_json:
        print(json.dumps({"ok": False, "cannot_judge": reason}, ensure_ascii = False))
    else:
        print(f"无法判定: {reason}")

    return 0

def _collect(session: mcp_session, observation: fleet.observation, *, ticks: int, interval: float, logs_lines: int, probe_url: str = "") -> str | None:
    """Sample the fleet; returns a fail-open reason on a fatal problem.

    Kept free of argparse so the tick loop can be exercised directly (the real-malkuth
    test drives it against a session it owns, after starting a supervision).
    """

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
                except (mcp_error, mcp_unavailable) as e:
                    observation.note_indeterminate(f"logs {run.run_id}/{worker.id}: {e}")
                    continue

                try:
                    text = logs.get("text") if isinstance(logs, dict) else None
                    tick.log_digests[(run.run_id, worker.id)] = fleet.log_digest(text)
                except fleet.schema_error as e:
                    observation.note_indeterminate(f"logs {run.run_id}/{worker.id}: {e}")

        if probe_url:
            try:
                probe = session.call_tool("malkuth_probe", {"url": probe_url, "kind": "both", "timeout_secs": 5})
                tick.probe_ok = fleet.probe_ok(probe)
            except (mcp_error, mcp_unavailable) as e:
                observation.note_indeterminate(f"probe {probe_url}: {e}")
            except fleet.schema_error as e:
                observation.note_indeterminate(f"probe {probe_url}: {e}")

        observation.add(tick)

    return None

def _render(args: argparse.Namespace, observation: fleet.observation, latest: fleet.tick) -> int:
    verdicts = fleet.evaluate(observation, limits_from_args(args))
    workers = [(run, worker)for run in latest.runs for worker in run.workers]

    if args.json:
        print(json.dumps({"ok": True, "read_only": True, "ticks": len(observation.ticks), "interval_secs": args.interval, "runs": len(latest.runs), "workers": len(workers), "verdicts": [{"code": verdict.code, "severity": verdict.severity, "run_id": verdict.run_id, "worker_id": verdict.worker_id, "evidence": verdict.evidence, "suggest": verdict.suggest}for verdict in verdicts], "indeterminate": observation.indeterminate, "reserved_predicates": [fleet.A3_RESERVED]}, ensure_ascii = False))
        return 0

    if not latest.runs:
        print("无 fleet: the MCP server supervises no runs (it reports an empty registry)")

        if observation.indeterminate:
            print("  注意: " + "; ".join(observation.indeterminate))

        return 0

    print(f"fleet: {len(latest.runs)} run(s), {len(workers)} worker(s), {len(observation.ticks)} tick(s) over {args.interval * (len(observation.ticks) - 1):g}s")

    for run, worker in workers:
        print("  " + fleet.worker_line(run, worker))

    if not verdicts:
        print("结论: 健康 (no predicate fired)")
    else:
        print(f"结论: {len(verdicts)} 项命中")

        for verdict in verdicts:
            print(f"  [{verdict.severity}] {verdict.code} {verdict.run_id}/{verdict.worker_id} {json.dumps(verdict.evidence, ensure_ascii = False)}")
            print(f"    {verdict.suggest}")

    for reason in observation.indeterminate:
        print(f"  无法判定(局部): {reason}")

    print(f"  保留未实现: {fleet.A3_RESERVED}")
    return 0

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

    command = args.mcp_cmd.split()
    observation = fleet.observation()
    session = mcp_session(command, timeout = args.call_timeout)

    try:
        session.open()
        missing = sorted(READ_ONLY_TOOLS - session.tools)

        if missing:
            return _cannot_judge(f"MCP server {args.mcp_cmd!r} does not advertise {missing}; " "it predates the wave-4 read arms, so no judgement is possible", as_json = args.json)

        failure = _collect(session, observation, ticks = args.ticks, interval = args.interval, logs_lines = args.logs_lines, probe_url = args.probe_url)

        if failure is not None:
            return _cannot_judge(failure, as_json = args.json)
    except (mcp_error, mcp_unavailable) as e:
        return _cannot_judge(str(e), as_json = args.json)
    finally:
        session.close()

    latest = observation.latest()
    assert latest is not None  # _collect guarantees at least one tick
    return _render(args, observation, latest)

if __name__ == "__main__":
    raise SystemExit(main())
