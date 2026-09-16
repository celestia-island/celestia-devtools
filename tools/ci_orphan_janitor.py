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
import re
import sys
import time
import signal
import socket
import argparse
import subprocess
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# zombie-worker detection (unhonoured job cancellation)
#
# Symptom (2026-09-15/16 incidents): ci-reaper cancels stale runs GitHub-side,
# the Listener logs "Job cancellation request ... received, cancellation
# timeout 5 minutes", its own kill fires ("haven't exit within cancellation
# timout, kill running worker") — but the Worker process survives, the runner
# stays busy forever and the whole farm queue stalls behind a dead job.
#
# Rule: if the newest cancellation request is older than CANCEL_GRACE seconds
# (2x the runner's own 5-minute timeout) and a Worker process alive right now
# already existed when that request arrived, the runner ignored the
# cancellation -> restart the runner unit.
# ---------------------------------------------------------------------------

CANCEL_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z[^\]]*\] "
    r"Job cancellation request ([0-9a-f-]+) received"
)


def worker_pids():
    return [p for p in all_pids()
            if "Runner.Worker spawnclient" in read_cmdline(p)]


def starttime_epoch(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])
        with open("/proc/uptime") as f:
            uptime_s = float(f.read().split()[0])
        boot = time.time() - uptime_s
        return boot + starttime_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return 0.0


def last_cancel_request(diag_dir):
    """Newest 'Job cancellation request' epoch across the latest Runner logs."""
    now = time.time()
    best = 0.0
    try:
        logs = [os.path.join(diag_dir, n) for n in os.listdir(diag_dir)
                if n.startswith("Runner_") and n.endswith(".log")]
    except OSError:
        return 0.0
    for path in sorted(logs, key=os.path.getmtime, reverse=True)[:2]:
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()[-500:]
        except OSError:
            continue
        for line in lines:
            m = CANCEL_RE.search(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"
                                       ).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if now - ts > 86400:  # ignore stale rotated logs
                continue
            best = max(best, ts)
    return best


def zombie_worker_check(diag_dir, cancel_grace, dry_run, unit):
    last_cancel = last_cancel_request(diag_dir)
    if not last_cancel:
        return False
    age = time.time() - last_cancel
    if age < cancel_grace:
        return False
    for pid in worker_pids():
        st = starttime_epoch(pid)
        if st and st < last_cancel:
            print(f"janitor: zombie worker pid={pid} predates cancellation "
                  f"request from {int(age)}s ago -> {'DRYRUN ' if dry_run else ''}"
                  f"restart {unit}")
            if not dry_run:
                rc = subprocess.run(["systemctl", "restart", unit]).returncode
                print(f"janitor: systemctl restart {unit} rc={rc}")
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grace", type=int, default=900,
                    help="minimum seconds alive before a candidate is killed")
    ap.add_argument("--cancel-grace", type=int, default=600,
                    help="seconds after an unacknowledged job cancellation "
                         "before the runner unit is restarted")
    ap.add_argument("--unit", default=None,
                    help="runner systemd unit to restart (default: "
                         "actions.runner.celestia-island.$(hostname).service)")
    ap.add_argument("--diag-dir", default=os.path.join(RUNNER_HOME, "_diag"))
    ap.add_argument("--skip-zombie", action="store_true")
    ap.add_argument("--skip-orphans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.skip_zombie:
        unit = args.unit or ("actions.runner.celestia-island.%s.service"
                             % socket.gethostname())
        if zombie_worker_check(args.diag_dir, args.cancel_grace,
                               args.dry_run, unit):
            return 0

    active = [] if args.skip_orphans else active_job_repos()
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
