#!/usr/bin/env python3
"""ci-orphan-janitor.py — reap orphaned build processes on self-hosted CI runner VMs.

Why this exists (2026-09-16, node-ci-3 incident):
  rustc spawned via the sccache server escapes the Runner.Worker process tree,
  so GitHub runner's end-of-job teardown never kills them. A failed/cancelled
  job can leave cargo/rustc running for many hours, eating RAM/swap until the
  watchdog flags the node OVERLOADED. ci-farm-watch (judge/restart) and
  ci-reaper (stale GitHub runs) do not cover this layer.

Orphan criterion (all must hold) — 2026-10-03 rewrite, see PR for the evidence:
  1. comm is a build tool (rustc / cargo / clippy-driver / rustdoc / sccache)
  2. cwd sits in *some* runner work dir (<runner_home>/_work/<repo>/<repo>);
     a VM runs several slots (`actions-runner`, `actions-runner-2`, ...)
  3. no ancestor process is still a Runner.* / RunnerService process
  4. elapsed time >= --grace seconds (default 900)
  5. it started before the oldest currently live Worker of *its own slot*

Criterion 5 replaced the old "repo of the active job" exemption, and criterion 2
replaced a single hard-coded runner home. Both were wrong on a two-slot VM:
  * the repo exemption saved every process of the repo that happened to be running,
    so an entelecheia job exempted entelecheia orphans *tens of hours old*
    (node-ci-4, 2026-10-03: 11 processes aged 21.7h-44.7h survived while the timer
    reported "clean (active_jobs=['entelecheia/entelecheia'])");
  * the hard-coded home made the second slot invisible (repo is None -> continue),
    so its orphans could never be reaped at all.
Criterion 3 already protected a live job's own build tree, and criterion 5 protects
processes the job deliberately detached (an sccache server that outlives its client):
if a slot has a live Worker, only processes *older* than that Worker can be orphans.

Usage: ci-orphan-janitor.py [--grace SEC] [--cancel-grace SEC] [--dry-run]
                            [--selftest] [--skip-zombie] [--skip-orphans]
Exit 0 normally (also when nothing matched); exit 3 on internal error.
"""
import glob
import json
import os
import re
import sys
import time
import signal
import socket
import argparse
import subprocess
from datetime import datetime, timezone

RUNNER_PARENT = "/home/lab"
RUNNER_HOME = os.path.join(RUNNER_PARENT, "actions-runner")  # default/primary slot
BUILD_COMMS = {"rustc", "cargo", "clippy-driver", "rustdoc", "sccache"}


class RealProc:
    """Thin /proc reader. The selftest swaps in a dict-backed fake."""

    def pids(self):
        out = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                out.append(int(entry))
        return out

    def comm(self, pid):
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except OSError:
            return ""

    def cmdline(self, pid):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            return ""

    def cwd(self, pid):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""

    def ppid(self, pid):
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        return int(line.split()[1])
        except (OSError, ValueError):
            pass
        return -1

    def starttime(self, pid):
        """Epoch seconds when the process started; 0.0 when unreadable."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            ticks = int(fields[19])
            with open("/proc/uptime") as f:
                uptime_s = float(f.read().split()[0])
            return (time.time() - uptime_s) + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError):
            return 0.0

    def read_text(self, path):
        with open(path, errors="replace") as f:
            return f.read()


PROC = RealProc()


def all_pids():
    return PROC.pids()


def read_cmdline(pid):
    return PROC.cmdline(pid)


def read_comm(pid):
    return PROC.comm(pid)


def read_cwd(pid):
    return PROC.cwd(pid)


def read_ppid(pid):
    return PROC.ppid(pid)


def starttime_epoch(pid):
    return PROC.starttime(pid)


def etimes_of(pid):
    st = PROC.starttime(pid)
    if not st:
        return -1
    return int(time.time() - st)


def is_runner_proc(comm):
    return comm.startswith("Runner.") or "RunnerService" in comm


# ---------------------------------------------------------------------------
# slot enumeration: a VM runs one runner install dir per slot
# ---------------------------------------------------------------------------

def runner_homes():
    """Every runner install dir with a work folder (…/actions-runner, -2, -3 …)."""
    homes = [h for h in sorted(glob.glob(os.path.join(RUNNER_PARENT, "actions-runner*")))
             if os.path.isdir(os.path.join(h, "_work"))]
    return homes or [RUNNER_HOME]


def slot_units(homes):
    """home -> systemd unit, from the runner's own .runner registration file."""
    units = {}
    for home in homes:
        name = None
        try:
            name = json.loads(PROC.read_text(os.path.join(home, ".runner"))).get("agentName")
        except (OSError, ValueError):
            name = None
        if name:
            units[home] = f"actions.runner.celestia-island.{name}.service"
    return units


def home_of(path, homes):
    for home in homes:
        if path == home or path.startswith(home + "/"):
            return home
    return None


def repo_from_cwd(cwd, homes):
    """(home, '<repo>/<repo>') when cwd is inside a slot's work dir, else (None, None)."""
    home = home_of(cwd, homes)
    if home is None:
        return None, None
    work = os.path.join(home, "_work")
    if not cwd.startswith(work + "/"):
        return None, None
    parts = cwd[len(work) + 1:].split("/", 2)
    if len(parts) >= 2 and parts[0] and parts[1]:
        return home, f"{parts[0]}/{parts[1]}"
    return None, None


def live_worker_starts(homes):
    """home -> start time of the oldest currently live Runner.Worker in that slot."""
    out = {}
    for pid in PROC.pids():
        cmd = PROC.cmdline(pid)
        if "Runner.Worker spawnclient" not in cmd:
            continue
        home = next((h for h in homes if cmd.startswith(h + "/")), None)
        if home is None:
            continue
        st = PROC.starttime(pid)
        if st and (home not in out or st < out[home]):
            out[home] = st
    return out


def has_runner_ancestor(pid):
    """True while the ancestry still reaches the runner itself (live job's tree).

    An unreadable parent is treated as *live*: a real orphan always has a readable
    chain ending at PID 1, so the only case this covers is a race or a permission
    problem, and sparing a process is the safe side of that bet.
    """
    p, depth = pid, 0
    while p not in (1, -1) and depth < 24:
        p = PROC.ppid(p)
        if p == -1:
            return True
        if is_runner_proc(PROC.comm(p)):
            return True
        depth += 1
    return False


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
# cancellation -> restart the runner unit. Checked per slot (each has its own
# _diag and its own unit).
# ---------------------------------------------------------------------------

CANCEL_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z[^\]]*\] "
    r"Job cancellation request ([0-9a-f-]+) received"
)


def worker_pids():
    return [p for p in PROC.pids()
            if "Runner.Worker spawnclient" in PROC.cmdline(p)]


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
            lines = PROC.read_text(path).splitlines()[-500:]
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


# ---------------------------------------------------------------------------
# orphan classification
# ---------------------------------------------------------------------------

def orphan_candidates(grace, homes):
    """Victims as (pid, comm, etimes, home, repo, cmdline); pure over PROC."""
    live = live_worker_starts(homes)
    victims = []
    for pid in PROC.pids():
        comm = PROC.comm(pid)
        if comm not in BUILD_COMMS:
            continue
        if has_runner_ancestor(pid):
            continue
        home, repo = repo_from_cwd(PROC.cwd(pid), homes)
        if home is None:
            continue
        et = etimes_of(pid)
        if et < grace:
            continue
        oldest_worker = live.get(home)
        if oldest_worker:
            st = PROC.starttime(pid)
            if not st or st >= oldest_worker:
                # started during this slot's live job: it may belong to that job
                # (e.g. an sccache server outliving its client) -> leave it alone
                continue
        victims.append((pid, comm, et, home, repo, PROC.cmdline(pid)[:90]))
    return victims


def reap(grace, homes, dry_run):
    victims = orphan_candidates(grace, homes)
    live = live_worker_starts(homes)
    scope = ", ".join(f"{os.path.basename(h)}"
                      f"({'live job' if h in live else 'idle'})" for h in homes)
    if not victims:
        print(f"janitor: clean (slots: {scope})")
        return 0
    for pid, comm, et, home, repo, cmd in victims:
        action = "DRYRUN kill" if dry_run else "kill"
        print(f"janitor: {action} pid={pid} comm={comm} et={et}s "
              f"slot={os.path.basename(home)} repo={repo} cmd={cmd}")
        if not dry_run:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"janitor: WARN no permission for pid={pid}")
    return 0


# ---------------------------------------------------------------------------
# selftest: synthetic process tables, including controls proving the old rules
# were wrong on a two-slot VM.  `python3 ci_orphan_janitor.py --selftest`
# ---------------------------------------------------------------------------

H1 = "/home/lab/actions-runner"
H2 = "/home/lab/actions-runner-2"
NOW = 1_700_000_000.0


class FakeProc:
    def __init__(self, table):
        self.table = table

    def pids(self):
        return sorted(self.table)

    def _f(self, pid, key, default=""):
        return self.table.get(pid, {}).get(key, default)

    def comm(self, pid):
        return self._f(pid, "comm")

    def cmdline(self, pid):
        return self._f(pid, "cmdline")

    def cwd(self, pid):
        return self._f(pid, "cwd")

    def ppid(self, pid):
        return self._f(pid, "ppid", -1)

    def starttime(self, pid):
        return self._f(pid, "start", 0.0)

    def read_text(self, path):
        return self.table.get(path, {}).get("text", "")


def _case(name, table, homes, expect, grace=900):
    """Run orphan_candidates against a synthetic table; return (ok, detail)."""
    global PROC
    saved = PROC
    PROC = FakeProc(table)
    try:
        got = sorted(v[0] for v in orphan_candidates(grace, homes))
    finally:
        PROC = saved
    return (got == sorted(expect), f"{name}: victims={got} expected={sorted(expect)}")


def selftest():
    base = {
        # live job in slot 1: Runner.Worker -> bash -> cargo(10) -> rustc(11)
        100: {"comm": "Runner.Listener", "cmdline": f"{H1}/bin/Runner.Listener", "ppid": 1},
        101: {"comm": "Runner.Worker", "cmdline": f"{H1}/bin/Runner.Worker spawnclient 1 2",
              "ppid": 100, "start": NOW - 3600},
        10: {"comm": "cargo", "cmdline": "cargo test", "ppid": 102,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 3000},
        102: {"comm": "bash", "cmdline": f"{H1}/_work/_temp/abc.sh", "ppid": 101,
              "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 3100},
        11: {"comm": "rustc", "cmdline": "rustc --crate-name x", "ppid": 10,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 2900},
        # idle slot 2 with no worker at all
        200: {"comm": "Runner.Listener", "cmdline": f"{H2}/bin/Runner.Listener", "ppid": 1},
    }
    cases = []

    t = dict(base)
    cases.append(_case("live job's own tree is untouched", t, [H1, H2], []))

    t = dict(base)
    t[20] = {"comm": "cargo", "cmdline": "cargo bench", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("orphan older than the live worker is reaped", t, [H1, H2], [20]))

    t = dict(base)
    t[21] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H2}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("orphan in the second slot is reaped", t, [H1, H2], [21]))

    t = dict(base)
    t[101]["start"] = NOW - 3600
    t[22] = {"comm": "sccache", "cmdline": "sccache rustc", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 600}
    cases.append(_case("detached helper of the live job is spared", t, [H1, H2], []))

    t = dict(base)
    t[23] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 100}
    cases.append(_case("young process is spared", t, [H1, H2], []))

    t = dict(base)
    t[24] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": "/home/lab/elsewhere", "start": NOW - 20 * 3600}
    cases.append(_case("build tool outside any work dir is ignored", t, [H1, H2], []))

    t = dict(base)
    t[25] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": 0.0}
    cases.append(_case("unknown start time with a live job is spared", t, [H1, H2], []))

    t = dict(base)
    t[101]["comm"] = "bash"
    t[25] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("idle slot1: old process is reaped", t, [H1, H2], [25]))

    t = dict(base)
    t[29] = {"comm": "rustc", "cmdline": "rustc", "ppid": 999, "cwd": f"{H1}/_work/plana/plana",
             "start": NOW - 20 * 3600}  # 999 is absent from the table -> unreadable chain
    cases.append(_case("unreadable ancestry is spared", t, [H1, H2], []))

    t = dict(base)
    # both slots busy with the *same* repo, and one stale same-repo orphan in each:
    # the pre-fix repo exemption would have spared both
    t[101]["start"] = NOW - 1800
    t[400] = {"comm": "Runner.Worker", "cmdline": f"{H2}/bin/Runner.Worker spawnclient 3 4",
              "ppid": 200, "start": NOW - 1800}
    t[26] = {"comm": "cargo", "cmdline": "cargo test", "ppid": 1,
             "cwd": f"{H2}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    t[27] = {"comm": "cargo", "cmdline": "cargo test", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 21 * 3600}
    cases.append(_case("same-repo orphans are reaped while the repo runs in both slots",
                       t, [H1, H2], [26, 27]))

    t = dict(base)
    t[400] = {"comm": "RunnerService", "cmdline": "RunnerService", "ppid": 1}
    t[31] = {"comm": "rustc", "cmdline": "rustc", "ppid": 400,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 20 * 3600}
    cases.append(_case("RunnerService ancestry still protects", t, [H1, H2], []))

    t = dict(base)
    t[101] = {"comm": "Runner.Worker", "cmdline": "Runner.Worker spawnclient 1 2",
              "ppid": 100, "start": NOW - 1800}
    t[28] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/hikari/hikari", "start": NOW - 22 * 3600}
    cases.append(_case("a live job in slot1 cannot hide a stale slot1 orphan", t,
                       [H1, H2], [28]))

    # control: the pre-2026-10-03 rules (repo exemption + single hard-coded home)
    # on the same tables must get the two multi-slot cases WRONG.
    def old_rule(table, homes, grace=900):
        saved = PROC
        globals()["PROC"] = FakeProc(table)
        try:
            active = set()
            for pid in table:
                if table[pid]["comm"] == "bash" and f"{H1}/_work/_temp/" in table[pid].get("cmdline", ""):
                    _, r = repo_from_cwd(table[pid].get("cwd", ""), [H1])
                    if r:
                        active.add(r)
            out = []
            for pid in table:
                c = table[pid]["comm"]
                if c not in BUILD_COMMS:
                    continue
                p, ok, depth = pid, True, 0
                while p not in (1, -1) and depth < 24:
                    p = table.get(p, {}).get("ppid", -1)
                    if p == -1:
                        break
                    if is_runner_proc(table.get(p, {}).get("comm", "")):
                        ok = False
                        break
                    depth += 1
                if not ok:
                    continue
                _, repo = repo_from_cwd(table[pid].get("cwd", ""), [H1])
                if repo is None or repo in active:
                    continue
                if etimes_of(pid) < grace:
                    continue
                out.append(pid)
            return sorted(out)
        finally:
            globals()["PROC"] = saved

    t = dict(base)
    t[20] = {"comm": "cargo", "cmdline": "cargo bench", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    t[21] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H2}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    old = old_rule(t, [H1, H2])
    controls = [
        (old == [], f"old rules saw {old} (repo exemption + single home hide both)"),
    ]

    rc = 0
    for ok, detail in cases:
        print(f"selftest {'ok  ' if ok else 'FAIL'} {detail}")
        rc |= 0 if ok else 1
    for ok, detail in controls:
        print(f"selftest {'ok  ' if ok else 'FAIL'} control: {detail}")
        rc |= 0 if ok else 1
    print(f"selftest: {sum(1 for ok, _ in cases + controls if ok)}/{len(cases + controls)} passed")
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grace", type=int, default=900,
                    help="minimum seconds alive before a candidate is killed")
    ap.add_argument("--cancel-grace", type=int, default=600,
                    help="seconds after an unacknowledged job cancellation "
                         "before the runner unit is restarted")
    ap.add_argument("--unit", default=None,
                    help="runner systemd unit to restart (default: per slot, from "
                         "<runner_home>/.runner agentName)")
    ap.add_argument("--diag-dir", default=None,
                    help="override: use this single _diag dir and --unit only")
    ap.add_argument("--skip-zombie", action="store_true")
    ap.add_argument("--skip-orphans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    homes = runner_homes()
    if not args.skip_zombie:
        if args.diag_dir:
            slots = {args.unit or ("actions.runner.celestia-island.%s.service"
                                   % socket.gethostname()): args.diag_dir}
        else:
            slots = {unit: os.path.join(home, "_diag")
                     for home, unit in slot_units(homes).items()}
        for unit, diag in slots.items():
            if zombie_worker_check(diag, args.cancel_grace, args.dry_run, unit):
                return 0

    if args.skip_orphans:
        return 0
    return reap(args.grace, homes, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"janitor: ERROR {exc}", file=sys.stderr)
        sys.exit(3)
