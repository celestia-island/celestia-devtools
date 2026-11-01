"""Bring-your-own-fleet demo: start one supervision through the MCP server.

The judgement path is read-only (:data:`celestia_devtools.judge.mcp_stdio.READ_ONLY_TOOLS`);
this module is its **single documented exception** and it exists for one reason: malkuth's
run registry is *in-process*, so an external CLI cannot observe a fleet that somebody else
holds. To demonstrate the judge at all, the CLI has to bring a fleet itself — spawn the
MCP server and supervise one command through it, then judge that run.

So the contract is: ``malkuth_supervise`` appears here and nowhere else in the package,
and the judgement entry points in :mod:`celestia_devtools.judge.cli`
(:func:`~celestia_devtools.judge.cli.main` / ``_render``) never import it. That is what
keeps "the judge never acts" honest while still giving the demo a supported path.

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

    # A started supervision reports ``{run_id, timed_out, workers}`` and has no ``ok`` key;
    # only an explicit ``ok: false`` (or a missing run id) is a refusal.
    if not isinstance(payload, dict) or (payload.get("ok") is False) or not payload.get("run_id"):
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

def _checkout_root() -> Path:
    """The source checkout this module was imported from (not the caller's cwd).

    Installed copies fall back to the caller's cwd, where a sweep cannot do better than
    "same directory as the CLI" — the conservative choice, since a wrong root only means
    we clean less.
    """
    candidate = Path(__file__).resolve().parents[3]

    if (candidate / "src").is_dir() and (candidate / "pyproject.toml").is_file():
        return candidate

    return Path.cwd()

def _within_checkout(path: Path, root: Path | None = None) -> bool:
    """True when ``path`` is the checkout root or below it (a real path boundary).

    ``str.startswith`` would accept a sibling like ``/mnt/work/r241-r2pp-srcX`` for a root
    of ``/mnt/work/r241-r2pp-src`` — the reason this uses path semantics instead.
    """

    try:
        return path.resolve().is_relative_to((root or _checkout_root()).resolve())
    except OSError:
        return False

def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None

    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

def _proc_parent_comm(pid: int) -> str:
    try:
        parent = int(Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[-1].split()[1])
        return Path(f"/proc/{parent}/comm").read_text().strip()
    except (OSError, IndexError, ValueError):
        return ""

def _proc_cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None

def _iter_pids() -> list[int]:
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]

def terminate(pids: Sequence[int], *, expect: Sequence[str] | None = None) -> int:
    """TERM the given pids, but only after the process still *is* what we started.

    A pid from a status payload is a hint, not an identity: by the time the demo tears down
    it can be dead and recycled onto an unrelated process. With ``expect`` (the demo's
    programs) the kill only lands when ``/proc/<pid>/cmdline`` still matches one of them;
    an unreadable or mismatched process is skipped. Without ``expect`` nothing is signalled
    — an unverified pid is never terminated.
    """

    if not pids or not expect:
        return 0

    programs = {os.path.realpath(program) for program in expect}
    delivered = 0

    for pid in pids:
        try:
            numeric = int(pid)
        except (TypeError, ValueError):
            continue

        argv = _proc_cmdline(numeric)

        if not argv or (os.path.realpath(argv[0]) not in programs):
            continue

        try:
            os.kill(numeric, 15)
            delivered += 1
        except OSError:
            continue

    # A demo child may ignore TERM (a legitimate thing to supervise). Give it a moment and
    # KILL the survivors we started — the identity is re-checked before the second signal.
    if delivered:
        time.sleep(0.3)

        for pid in pids:
            try:
                numeric = int(pid)
            except (TypeError, ValueError):
                continue

            argv = _proc_cmdline(numeric)

            if not argv or (os.path.realpath(argv[0]) not in programs):
                continue

            try:
                os.kill(numeric, 9)
            except OSError:
                continue

    return delivered

def sweep_abandoned(commands: Sequence[Sequence[str]]) -> int:
    """TERM orphaned leftovers of the demo commands that the drain could not reach.

    Only *project-local* programs are swept (a path with a separator outside the system bin
    and opt/home trees) **and** only when the process' working directory is under this
    checkout's root (derived from this module's location, not the caller's cwd). A process
    is a leftover when its parent was reparented to init/systemd, i.e. nothing supervises it
    any more — the same ancestry rule as the test suite.
    """
    programs = set()

    for argv in commands:
        if not argv:
            continue

        program = os.path.realpath(argv[0])

        if ("/" not in argv[0]) or program.startswith(("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/", "/usr/local/bin/", "/opt/", "/home/")):
            continue

        programs.add(program)

    if not programs:
        return 0

    killed = 0

    for pid in _iter_pids():
        argv = _proc_cmdline(pid)

        if not argv or (os.path.realpath(argv[0]) not in programs):
            continue

        cwd = _proc_cwd(pid)

        if (cwd is None) or not _within_checkout(cwd):
            continue

        if _proc_parent_comm(pid) not in ("systemd", "init"):
            continue

        try:
            os.kill(pid, 15)
            killed += 1
        except OSError:
            continue

    return killed
