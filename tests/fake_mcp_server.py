#!/usr/bin/env python3
"""A throwaway MCP stdio server with one behaviour per fixture mode.

Used by ``tests/test_judge_fleet.py`` to drive the judge without a real malkuth: every
mode answers the same JSON-RPC envelope, and only the payloads change. A mode is the
first CLI argument.

Modes: healthy, crashloop, spawnfail, logstall, drainstalled, empty, badshape,
missing-tools, slow, junkline, oscillate, hang, logfail, probefail, and the action-gate
modes (see ``ACTION_MODES``) that drive ``judge-fleet act``.

The action modes record every ``tools/call`` they receive to the file named by
``FAKE_MCP_CALL_LOG`` (one ``name {json}`` line each), which is how the tests prove a
``--dry-run`` registered and executed nothing.

A ``malkuth_supervise`` call always answers ``{"ok": true}`` and makes later ``status``
calls report one demo run, so the CLI's ``--supervise`` demo path is exercised without a
real malkuth.
"""

from __future__ import annotations
import json
import os
import sys
import time

#: Worker identity the action-gate modes report (the bundles in the tests quote it).
ACT_PID = 4242
ACT_STARTTIME = 99

#: Modes that drive the executor: status payload + gate answers per mode.
ACTION_MODES = frozenset({"act-ok", "act-required", "act-consumed", "act-blocked", "act-expired", "act-changed", "act-no-starttime", "act-drain-ok", "act-empty", "act-mismatch"})

def _content(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}

def _worker(**overrides) -> dict:
    worker = {"id": "app", "kind": "backend", "phase": "running", "pid": 1 << 30, "uptime_secs": 120, "restart_policy": "permanent", "restart_count": 0, "last_error": None}
    worker.update(overrides)
    return worker

def _run(workers, *, draining = False, drain_kind = None, finished = False) -> dict:
    return {"run_id": "run-1", "started_unix_secs": 1_750_000_000, "elapsed_secs": 120, "finished": finished, "finished_unix_secs": None, "draining": draining, "drain_kind": drain_kind, "worker_count": len(workers), "workers": workers}

class fake_server:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0
        # (proposal_id, worker_id) -> action, mirroring the real gate: an approval
        # is registered for one exact action, and the executor must match it.
        self.approved_actions: dict[tuple[str | None, str | None], str] = {}
        self.supervised = False

    def status(self) -> dict:
        self.calls += 1
        tick = self.calls

        if self.mode == "empty":
            return {"ok": True, "runs": []}

        if self.mode == "act-empty":
            return {"ok": True, "runs": []}

        if self.mode in ACTION_MODES:
            if self.supervised:
                # The demo switch supervises a command through this server; the run it
                # brought is what the executor acts on.
                return {"ok": True, "runs": [_run([_worker(id = "demo-0", pid = ACT_PID, starttime = ACT_STARTTIME)])]}

            pid, starttime = ACT_PID, ACT_STARTTIME

            if self.mode == "act-changed":
                starttime = ACT_STARTTIME + 1

            if self.mode == "act-no-starttime":
                starttime = None

            return {"ok": True, "runs": [_run([_worker(pid = pid, starttime = starttime)])]}

        if self.supervised:
            return {"ok": True, "runs": [_run([_worker(id = "demo-0", uptime_secs = 60 + tick)])]}

        if self.mode == "crashloop":
            return {"ok": True, "runs": [_run([_worker(restart_count = tick * 2, phase = "running")])]}

        if self.mode == "spawnfail":
            return {"ok": True, "runs": [_run([_worker(restart_count = tick * 2, phase = "starting", pid = None, uptime_secs = 0, last_error = "spawn failed: No such file or directory (os error 2)")])]}

        if self.mode == "oscillate":
            phase = "running" if tick % 2 else "restarting"
            return {"ok": True, "runs": [_run([_worker(phase = phase, pid = 4242 if phase == "running" else None)])]}

        if self.mode == "hang":
            return {"ok": True, "runs": [_run([_worker(uptime_secs = 600 + tick)])]}

        if self.mode == "drainstalled":
            return {"ok": True, "runs": [_run([_worker()], draining = True, drain_kind = "graceful")]}

        if self.mode == "badshape":
            return {"ok": True, "runs": [{"workers": []}]}

        if self.mode == "junkline":
            return {"ok": True, "runs": [_run([_worker()])]}

        return {"ok": True, "runs": [_run([_worker(restart_count = 0, uptime_secs = 120 + tick)])]}

    def logs(self) -> dict:
        if self.mode == "logfail":
            return {"ok": False, "code": "log_read_failed"}

        text = "boot\nsteady" if self.mode in ("logstall", "hang") else f"boot\nsteady {self.calls}"
        return {"ok": True, "text": text, "total_lines_in_ring": 2, "lines_returned": 2}

    def probe(self) -> list:
        if self.mode == "probefail":
            # A transport failure: an endpoint entry with no HTTP status, which the client
            # must classify as "unreachable" (not evaluated), never as a readiness verdict.
            return [{"url": "http://x/healthz", "ok": False, "error": "timed out"}, {"url": "http://x/readyz", "ok": False, "error": "timed out"}]

        if self.mode in ("silent-hang", "hang"):
            return [{"url": "http://x/healthz", "ok": False, "status": 503}]

        return [{"url": "http://x/healthz", "ok": True, "status": 200}, {"url": "http://x/readyz", "ok": True, "status": 200}]

def _tool_list(mode: str) -> list:
    if mode == "missing-tools":
        return [{"name": "malkuth_probe"}, {"name": "malkuth_supervise"}]

    if mode in ACTION_MODES:
        return [{"name": "malkuth_status"}, {"name": "malkuth_approve"}, {"name": "malkuth_restart"}, {"name": "malkuth_drain"}]

    return [{"name": "malkuth_status"}, {"name": "malkuth_logs"}, {"name": "malkuth_probe"}, {"name": "malkuth_supervise"}]

def _log_call(params: dict) -> None:
    """Append one ``tools/call`` line to $FAKE_MCP_CALL_LOG, if the test asked for it."""

    path = os.environ.get("FAKE_MCP_CALL_LOG")

    if not path:
        return

    with open(path, "a", encoding = "utf-8") as handle:
        handle.write(json.dumps({"name": params.get("name"), "arguments": params.get("arguments")}) + "\n")

def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "healthy"
    backend = fake_server(mode)

    for line in sys.stdin:
        text = line.strip()

        if not text:
            continue

        if mode == "junkline":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            return 1

        if mode == "slow":
            time.sleep(3600)
            return 0

        message = json.loads(text)
        method = message.get("method")

        if method == "tools/call":
            _log_call(message.get("params", {}))

        request_id = message.get("id")

        if method == "initialize":
            result: object = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-malkuth", "version": "0"}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result = {"tools": _tool_list(mode)}
        elif method == "tools/call":
            name = message.get("params", {}).get("name")
            args = message.get("params", {}).get("arguments") or {}

            if name == "malkuth_supervise":
                backend.supervised = True
                payload = {"ok": True, "run_id": "demo-run", "workers": []}
            elif name == "malkuth_status":
                payload = backend.status()
            elif name == "malkuth_logs":
                payload = backend.logs()
            elif name == "malkuth_probe":
                payload = backend.probe()
            elif name == "malkuth_approve":
                # Mirrors the real gate's parse order and stable codes exactly:
                # the action is validated before the decision, a missing/empty
                # field is refused as *_required, and an unknown non-empty value
                # as unsupported_*.
                action = str(args.get("action") or "").strip().lower()
                decision = str(args.get("decision") or "").strip().lower()
                if not action:
                    payload = {"ok": False, "code": "approval_action_required", "message": "the action field is required"}
                elif action not in ("restart", "drain"):
                    payload = {"ok": False, "code": "unsupported_action", "message": f"unknown action {action!r}"}
                elif not decision:
                    payload = {"ok": False, "code": "approval_decision_required", "message": "the decision field is required"}
                elif decision not in ("allow", "approved", "review", "block", "blocked"):
                    payload = {"ok": False, "code": "unsupported_decision", "message": f"unknown decision {decision!r}"}
                else:
                    backend.approved_actions[(args.get("proposal_id"), args.get("worker_id"))] = action
                    payload = {"ok": True, "registered": True, "action": action, "expires_in_secs": 300}
            elif name == "malkuth_restart":
                if mode == "act-required":
                    payload = {"ok": False, "code": "approval_required", "message": "no live approval"}
                elif mode == "act-consumed":
                    payload = {"ok": False, "code": "approval_consumed", "message": "already spent"}
                elif mode == "act-blocked":
                    payload = {"ok": False, "code": "approval_blocked", "message": "blocked by operator"}
                elif mode == "act-expired":
                    payload = {"ok": False, "code": "approval_expired", "message": "gate window closed"}
                elif mode == "act-mismatch":
                    payload = {"ok": False, "code": "approval_action_mismatch", "message": "the approval authorizes drain, not restart"}
                else:
                    bound = backend.approved_actions.get((args.get("proposal_id"), args.get("worker_id")))
                    if bound == "drain":
                        payload = {"ok": False, "code": "approval_action_mismatch", "message": "the approval authorizes drain, not restart"}
                    else:
                        payload = {"ok": True, "accepted": True, "phase": "restarting"}
            elif name == "malkuth_drain":
                bound = backend.approved_actions.get((args.get("proposal_id"), args.get("worker_id")))
                if bound == "restart":
                    payload = {"ok": False, "code": "approval_action_mismatch", "message": "the approval authorizes restart, not drain"}
                else:
                    payload = {"ok": True, "accepted": True, "already": False, "kind": "graceful", "workers": []}
            else:
                payload = {"ok": False, "code": "unknown_tool"}

            result = _content(payload)
        else:
            result = {}

        if request_id is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
            sys.stdout.flush()

    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
