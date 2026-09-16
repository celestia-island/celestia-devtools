#!/usr/bin/env python3
"""ci-orphan-janitor.py — reap orphaned build processes on self-hosted CI runner VMs.

Why this exists (2026-09-16, node-ci-3 incident):
  rustc spawned via the sccache server escapes the Runner.Worker process tree,
  so GitHub runner's end-of-job teardown never kills them. A failed/cancelled
  job can leave cargo/rustc running for many hours, eating RAM/swap until the
  watchdog flags the node OVERLOADED. ci-farm-watch (judge/restart) and
  ci-reaper (stale GitHub runs) do not cover this layer.

Orphan criterion (all three must hold):
  1. comm is a build tool (rustc / cargo / clippy-driver / rustdoc / sccache client)
  2. cwd is inside the runner workdir (<runner_home>/_work/<repo>/<repo>)
  3. elapsed time >= --grace seconds (default 900)
     AND it does not belong to the repo of the currently executing job
     (single runner per VM: at most one active job; if the runner is idle,
     anything matching 1-3 past grace is an orphan).

Usage: ci-orphan-janitor.py [--grace SEC] [--dry-run]
Exit 0 normally; exit 3 on internal error.
"""
import os
import sys
import signal
import argparse

RUNNER_HOME = "/home/lab/actions-runner"
WORK_DIR = os.path.join(RUNNER_HOME, "_work")
BUILD_COMMS = {"rustc", "cargo", "clippy-driver", "rustdoc", "sccache"}


def read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def read_comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def read_ppid(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return -1


def etimes_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        return int(uptime_s - starttime_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, IndexError, ValueError):
        return -1


def all_pids():
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            yield int(entry)


def active_job_repos():
    """Repos of the currently executing job, inferred from job step scripts
    (bash <runner_home>/_work/_temp/<uuid>.sh) whose cwd sits in a work dir."""
    repos = set()
    for pid in all_pids():
        cmd = read_cmdline(pid)
        if comm_of(pid) == "bash" and f"{WORK_DIR}/_temp/" in cmd:
            cwd = read_cwd(pid)
            repo = repo_from_cwd(cwd)
            if repo:
                repos.add(repo)
    return repos


def comm_of(pid):
    # separate helper so active_job_repos stays readable
    return read_comm(pid)


def repo_from_cwd(cwd):
    """_work/<repo>/<repo>/... -> '<repo>/<repo>' prefix identity; None if outside."""
    if not cwd.startswith(WORK_DIR + "/"):
        return None
    rel = cwd[len(WORK_DIR) + 1:]
    parts = rel.split("/", 2)
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grace", type=int, default=900,
                    help="minimum seconds alive before a candidate is killed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    active = active_job_repos()
    victims = []
    for pid in all_pids():
        comm = read_comm(pid)
        if comm not in BUILD_COMMS:
            continue
        # never touch anything whose ancestry still reaches the runner itself
        p, ok, depth = pid, True, 0
        while p not in (1, -1) and depth < 24:
            p = read_ppid(p)
            if p == -1:
                break
            pc = read_comm(p)
            if pc.startswith("Runner.") or "RunnerService" in pc:
                ok = False
                break
            depth += 1
        if not ok:
            continue
        cwd = read_cwd(pid)
        repo = repo_from_cwd(cwd)
        if repo is None:
            continue
        if repo in active:
            continue
        et = etimes_of(pid)
        if et < args.grace:
            continue
        victims.append((pid, comm, et, repo, read_cmdline(pid)[:90]))

    if not victims:
        print(f"janitor: clean (active_jobs={sorted(active) or 'none'})")
        return 0

    for pid, comm, et, repo, cmd in victims:
        action = "DRYRUN kill" if args.dry_run else "kill"
        print(f"janitor: {action} pid={pid} comm={comm} et={et}s repo={repo} cmd={cmd}")
        if not args.dry_run:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"janitor: WARN no permission for pid={pid}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"janitor: ERROR {exc}", file=sys.stderr)
        sys.exit(3)
