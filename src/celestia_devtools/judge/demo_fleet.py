"""Bring-your-own-fleet demo: start one supervision through the MCP server.

The judgement path is read-only (:data:`celestia_devtools.judge.mcp_stdio.READ_ONLY_TOOLS`);
this module is its **single documented exception** and it exists for one reason: malkuth's
run registry is *in-process*, so an external CLI cannot observe a fleet that somebody else
holds. To demonstrate the judge at all, the CLI has to bring a fleet itself — spawn the
MCP server and supervise one command through it, then judge that run.

So the contract is: ``malkuth_supervise`` appears here and nowhere else in the package,
and nothing in here is called by :func:`celestia_devtools.judge.cli.judge` (the judgement
entry point). That is what keeps "the judge never acts" honest while still giving the
demo a supported path.

The supervision is bounded by ``timeout_secs``: malkuth drains and returns on its own, and
the caller closes the session afterwards, which ends the server and (with it) the child it
supervised.
"""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
from collections.abc import Sequence
from celestia_devtools.judge.mcp_stdio import mcp_error, mcp_session

#: The one action tool this package may call, and only from this module.
SUPERVISION_TOOL = "malkuth_supervise"

def worker_specs(commands: Sequence[Sequence[str]]) -> list[dict]:
    """One supervised worker per command; argv is taken verbatim (already shlex-split)."""
    specs = []

    for index, argv in enumerate(commands):
        if not argv:
            raise ValueError("an empty --supervise command is not a command")

        specs.append({"id": f"demo-{index}", "kind": "demo", "program": argv[0], "args": list(argv[1:])})

    return specs

def start_supervision(session: mcp_session, commands: Sequence[Sequence[str]], *, timeout_secs: float = 10.0, max_restarts: int = 20, window_secs: int = 60) -> dict:
    """Ask the server to supervise the demo fleet; returns the tool's JSON payload.

    This is the only place the package sends a non-read call (see the module docstring).
    The request goes through the JSON-RPC layer on purpose: routing it through
    ``call_tool`` would mean widening the judgement allowlist.
    """
    specs = worker_specs(commands)
    reply = session._request("tools/call", {"name": SUPERVISION_TOOL, "arguments": {"workers": specs, "timeout_secs": max(int(timeout_secs), 1), "max_restarts": max_restarts, "window_secs": window_secs}})
    content = reply.get("content")

    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise mcp_error("supervise returned no content block")

    payload = json.loads(content[0].get("text") or "{}")

    if not isinstance(payload, dict) or (payload.get("ok") is not True):
        raise mcp_error(f"supervise did not start a run: {payload}")

    return payload

def wait_for_run(session: mcp_session, *, timeout: float = 10.0) -> tuple[bool, list[int]]:
    """Poll ``malkuth_status`` until the demo run appears.

    Returns ``(run_seen, worker_pids)``: a crash-looping worker is real but has no pid
    between restarts, so "a run exists" — not "a pid exists" — is the readiness signal.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            payload = session.call_tool("malkuth_status")
        except mcp_error:
            time.sleep(0.2)
            continue

        runs = payload.get("runs") if isinstance(payload, dict) else None

        if isinstance(runs, list) and runs:
            pids = []

            for run in runs:
                workers = run.get("workers") if isinstance(run, dict) else None

                for worker in workers if isinstance(workers, list) else []:
                    if isinstance(worker, dict) and isinstance(worker.get("pid"), int):
                        pids.append(worker["pid"])

            return True, pids

        time.sleep(0.2)

    return False, []

def descendants(pid: int) -> set[int]:
    """Every live descendant of ``pid`` (the demo's own leaves), read from /proc."""
    children: dict[int, list[int]] = {}

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue

        try:
            fields = (entry / "stat").read_text().split(") ", 1)[-1].split()
            parent = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue

        children.setdefault(parent, []).append(int(entry.name))

    found: set[int] = set()
    stack = [pid]

    while stack:
        current = stack.pop()

        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)

    return found

def sweep_abandoned(commands: Sequence[Sequence[str]]) -> int:
    """TERM orphaned leftovers of the demo commands that the drain could not reach.

    Only *project-local* programs are swept (a path with a separator outside the system
    bin directories): the demo never hunts system-wide copies of ``python3`` or ``sh``.
    A process is a leftover when its parent was reparented to init/systemd, i.e. nothing
    supervises it any more — the same ancestry rule as the test suite.
    """
    programs = set()

    for argv in commands:
        if not argv:
            continue

        program = os.path.realpath(argv[0])

        if ("/" not in argv[0]) or program.startswith(("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/")):
            continue

        programs.add(program)

    if not programs:
        return 0

    killed = 0

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue

        try:
            argv = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            parent = int((entry / "stat").read_text().split(") ", 1)[-1].split()[1])
        except (OSError, IndexError, ValueError):
            continue

        if not argv or (os.path.realpath(argv[0]) not in programs):
            continue

        try:
            parent_comm = (Path("/proc") / str(parent) / "comm").read_text().strip()
        except OSError:
            parent_comm = ""

        if parent_comm not in ("systemd", "init"):
            continue

        try:
            os.kill(int(entry.name), 15)
            killed += 1
        except OSError:
            continue

    return killed

def terminate(pids: Sequence[int]) -> int:
    """TERM the given pids; returns how many signals were delivered."""
    delivered = 0

    for pid in pids:
        try:
            os.kill(int(pid), 15)
            delivered += 1
        except (OSError, ValueError):
            continue

    return delivered
