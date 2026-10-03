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

Known limitation (evidence in the PR): a detached build helper that predates the
slot's live Worker and whose cwd sits inside a work dir is reaped, even if a live job
depends on it. The sccache daemon actually observed on the CI VMs keeps cwd=/ and is
therefore excluded by criterion 2, so this is latent rather than live; keeping a helper
across jobs should be done with a systemd unit whose cwd is outside _work.

Known limitation (deliberate, follow-up): a slot whose Worker hangs without any
cancellation request produces no restart trigger, and that hung Worker also counts as
"live" for criterion 5, so new processes behind it are spared. The farm watchdog and
ci-reaper are likewise slot-1-only for that class. Sparing is the intended direction
of every ambiguous case here: a wrong SIGKILL or a wrong runner restart costs a live
job, while a missed orphan is reaped on the next cycle once the slot goes idle.

Usage: ci-orphan-janitor.py [--grace SEC] [--cancel-grace SEC] [--dry-run]
                            [--selftest] [--skip-zombie] [--skip-orphans]
Exit 0 normally (also when nothing matched); exit 3 on internal error.
"""
import copy
import glob
import json
import os
import re
import sys
import time
import signal
import tempfile
import argparse
import subprocess
from datetime import datetime, timezone

RUNNER_PARENT = "/home/lab"
RUNNER_HOME = os.path.join(RUNNER_PARENT, "actions-runner")  # default/primary slot
BUILD_COMMS = {"rustc", "cargo", "clippy-driver", "rustdoc", "sccache"}


def read_text_file(path):
    with open(path, errors="replace") as f:
        return f.read()


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
        return read_text_file(path)


PROC = RealProc()


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


def read_agent_name(home):
    """agentName from the runner's .runner registration file, BOM tolerated.

    The runner writes that file with a UTF-8 BOM on these VMs, and json.loads rejects
    it - so a bare parse silently yields no unit at all and the whole zombie check
    becomes inert (field-found 2026-10-03: both nodes' .runner start with \ufeff).
    """
    try:
        raw = PROC.read_text(os.path.join(home, ".runner"))
    except OSError:
        return None
    try:
        name = json.loads(raw.lstrip("\ufeff")).get("agentName")
    except ValueError:
        return None
    return name or None


def unit_exists(unit):
    """Is that unit actually loaded? A wrong name would make a restart a silent no-op."""
    try:
        r = subprocess.run(["systemctl", "show", "-p", "LoadState", "--value", unit],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "loaded"


def unit_serves_home(unit, home):
    """Does that unit actually run this runner home?

    LoadState alone is not enough: a home whose .runner names another slot's agent would
    otherwise map to that other slot's unit, and a zombie here would restart - and kill a
    live job in - the wrong slot. Read-only check, capped like the other systemctl calls.
    """
    try:
        r = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", unit],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    w = r.stdout.strip()
    return r.returncode == 0 and bool(w) and os.path.realpath(w) == os.path.realpath(home)


def units_by_home(homes):
    """home -> unit, asked of systemd directly (the naming convention is not guessable)."""
    out = {}
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", "--all",
                            "--no-legend", "--plain", "actions.runner.*"],
                           capture_output=True, text=True, timeout=30)
        listing = r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in listing.splitlines():
        parts = line.split()
        if not parts or not parts[0].endswith(".service"):
            continue
        unit = parts[0]
        try:
            w = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", unit],
                               capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if w in homes:
            out[w] = unit
    return out


def slot_units(homes):
    """home -> systemd unit, from .runner when it checks out and from systemd otherwise."""
    units = {}
    discovered = None
    for home in homes:
        name = read_agent_name(home)
        unit = f"actions.runner.celestia-island.{name}.service" if name else None
        if unit and unit_exists(unit) and unit_serves_home(unit, home):
            units[home] = unit
            continue
        if discovered is None:
            discovered = units_by_home(homes)  # one scan covers every home
        if discovered.get(home):
            units[home] = discovered[home]
        else:
            print(f"janitor: WARN no runner unit serving {os.path.basename(home)}; "
                  f"its zombie check is skipped")
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
        if not is_worker_proc(pid):
            continue
        home = worker_home(pid, homes)
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


def worker_home(pid, homes):
    """Runner home a Worker belongs to, from its argv or failing that its cwd."""
    cmd = PROC.cmdline(pid)
    home = next((h for h in homes if cmd.startswith(h + "/")), None)
    if home is None:
        home = home_of(PROC.cwd(pid), homes)
    return home


def is_worker_proc(pid, home=None):
    """A Runner.Worker process, optionally restricted to one slot.

    Recognition falls back to the comm name because criterion 5 is the only guard for
    a live job's *detached* build processes: if a runner release renames the argv (the
    literal `Runner.Worker spawnclient`), criterion 5 would go silently vacuous and the
    tool would start killing live compiles - it has to fail toward sparing, not toward
    killing. The home filter is not cosmetic either: the cancellation timestamp comes
    from one slot's _diag, and pairing it with a machine-wide Worker list lets an old
    Worker in slot A justify restarting slot B, killing a healthy job there on every
    timer tick while the cancel line and that Worker both survive.
    """
    cmd = PROC.cmdline(pid)
    if "Runner.Worker spawnclient" not in cmd and PROC.comm(pid) != "Runner.Worker":
        return False
    return home is None or worker_home(pid, [home]) == home


def worker_pids(home=None):
    """Live Runner.Worker pids, optionally only those belonging to one slot."""
    return [p for p in PROC.pids() if is_worker_proc(p, home)]


def last_cancel_request(diag_dir):
    """Newest 'Job cancellation request' epoch across the latest Runner logs."""
    now = time.time()
    best = 0.0
    try:
        logs = [os.path.join(diag_dir, n) for n in os.listdir(diag_dir)
                if n.startswith("Runner_") and n.endswith(".log")]
    except OSError:
        return 0.0
    try:
        logs = sorted(logs, key=os.path.getmtime, reverse=True)[:2]
    except OSError:  # a log rotated away between listdir and stat
        return 0.0
    for path in logs:
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


def slot_home_for_diag(diag_dir):
    """Runner home a _diag dir belongs to, or None when it cannot be attributed."""
    home = os.path.dirname(os.path.normpath(diag_dir))
    return home if os.path.isdir(os.path.join(home, "_work")) else None


def zombie_worker_check(diag_dir, cancel_grace, dry_run, unit, home=None):
    """Restart `unit` when a Worker in *its own* slot ignored a cancellation.

    The slot is derived from the _diag dir when the caller does not supply it, and
    a cancellation that cannot be attributed to a slot is never acted on: pairing one
    slot's cancellation with a machine-wide Worker list is how an old Worker in slot A
    could justify restarting slot B, killing a healthy job there on every timer tick.
    """
    if home is None:
        home = slot_home_for_diag(diag_dir)
    if home is None:
        print(f"janitor: WARN cannot attribute {diag_dir} to a runner slot; "
              f"refusing to restart {unit}")
        return False
    last_cancel = last_cancel_request(diag_dir)
    if not last_cancel:
        return False
    age = time.time() - last_cancel
    if age < cancel_grace:
        return False
    for pid in worker_pids(home):
        st = starttime_epoch(pid)
        if st and st < last_cancel:
            print(f"janitor: zombie worker pid={pid} in "
                  f"{os.path.basename(home)} predates the "
                  f"cancellation request from {int(age)}s ago -> "
                  f"{'DRYRUN ' if dry_run else ''}restart {unit}")
            if not dry_run:
                # try-restart, not restart: a slot the slot governor deliberately
                # stopped must not be started again by the janitor.
                try:
                    rc = subprocess.run(["systemctl", "try-restart", unit],
                                        timeout=60).returncode
                except subprocess.TimeoutExpired:
                    rc = 124
                print(f"janitor: systemctl try-restart {unit} rc={rc}")
                if rc:
                    print(f"janitor: WARN cannot restart {unit} (rc={rc}) - check the "
                          f"unit name derived from .runner agentName")
            return True
    return False


# ---------------------------------------------------------------------------
# orphan classification
# ---------------------------------------------------------------------------

def orphan_candidates(grace, homes):
    """Victims as (pid, comm, etimes, home, repo, cmdline); pure over PROC."""
    live = live_worker_starts(homes)
    tentative = []
    spared_recent = set()
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
                spared_recent.add(pid)
                continue
        tentative.append((pid, comm, et, home, repo, PROC.cmdline(pid)[:90]))

    # Deliberately NOT protecting the ancestors of the spared set. It would save a
    # detached helper that predates the live job, but it cannot tell that helper apart
    # from a real orphan that is still spawning children: an hours-old `cargo bench`
    # keeps starting rustc that is "recent" by criterion 5, so its own ancestor rule
    # would shield the very process this tool exists to reap. The observed sccache
    # daemon is caught by criterion 2 instead (its cwd is /, not a work dir); a future
    # long-lived helper must be a systemd unit with a cwd outside _work.
    del spared_recent
    return tentative


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
NOW = time.time()  # real clock: --grace must actually bite in the fixtures


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
        # real files (a temp _diag in the zombie cases) must stay readable while the
        # process table is faked, otherwise the cancellation line is never parsed
        entry = self.table.get(path)
        if isinstance(entry, dict) and "text" in entry:
            return entry["text"]
        return read_text_file(path)


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

    t = copy.deepcopy(base)
    cases.append(_case("live job's own tree is untouched", t, [H1, H2], []))

    t = copy.deepcopy(base)
    t[20] = {"comm": "cargo", "cmdline": "cargo bench", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("orphan older than the live worker is reaped", t, [H1, H2], [20]))

    t = copy.deepcopy(base)
    t[21] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H2}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("orphan in the second slot is reaped", t, [H1, H2], [21]))

    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 3600
    t[22] = {"comm": "sccache", "cmdline": "sccache rustc", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 600}
    cases.append(_case("detached helper of the live job is spared", t, [H1, H2], []))

    t = copy.deepcopy(base)
    t[23] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 100}
    cases.append(_case("young process is spared", t, [H1, H2], []))

    t = copy.deepcopy(base)
    t[24] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": "/home/lab/elsewhere", "start": NOW - 20 * 3600}
    cases.append(_case("build tool outside any work dir is ignored", t, [H1, H2], []))

    t = copy.deepcopy(base)
    t[25] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": 0.0}
    cases.append(_case("unknown start time with a live job is spared", t, [H1, H2], []))

    t = copy.deepcopy(base)
    t[101]["comm"] = "bash"
    t[25] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    cases.append(_case("idle slot1: old process is reaped", t, [H1, H2], [25]))

    t = copy.deepcopy(base)
    t[29] = {"comm": "rustc", "cmdline": "rustc", "ppid": 999, "cwd": f"{H1}/_work/plana/plana",
             "start": NOW - 20 * 3600}  # 999 is absent from the table -> unreadable chain
    cases.append(_case("unreadable ancestry is spared", t, [H1, H2], []))

    # grace boundary in an IDLE slot: the fixture clock is real now, so these two
    # discriminate the --grace rule (a fixed clock made every age ~1053 days)
    t = copy.deepcopy(base)
    for pid in (101, 10, 11, 102):
        del t[pid]  # no live job in slot 1
    t[40] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 100}
    cases.append(_case("idle slot: process inside --grace is spared", t, [H1, H2], []))

    t = copy.deepcopy(base)
    for pid in (101, 10, 11, 102):
        del t[pid]
    t[41] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 1200}
    cases.append(_case("idle slot: process past --grace is reaped", t, [H1, H2], [41]))

    # the cancellation timestamp comes from one slot's _diag, so the Worker that
    # justifies a restart must belong to that same slot
    with tempfile.TemporaryDirectory() as tmp:
        home_a = os.path.join(tmp, "actions-runner")
        home_b = os.path.join(tmp, "actions-runner-2")
        diag = os.path.join(home_a, "_diag")
        os.makedirs(diag)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 1200))
        with open(os.path.join(diag, "Runner_20260101-000000-utc.log"), "w") as fh:
            fh.write(f"[{ts}Z INFO HostContext] Job cancellation request "
                     f"11111111-2222-3333-4444-555555555555 received\n")
        # only slot B has a Worker, and it predates the cancellation
        table = {500: {"comm": "Runner.Worker",
                       "cmdline": f"{home_b}/bin/Runner.Worker spawnclient 1 2",
                       "ppid": 1, "start": time.time() - 5000}}
        saved = PROC
        globals()["PROC"] = FakeProc(table)
        try:
            fired_wrong = zombie_worker_check(diag, 600, True,
                                              "actions.runner.x.node-b.service", home_a)
            fired_right = zombie_worker_check(diag, 600, True,
                                              "actions.runner.x.node-b.service", home_b)
        finally:
            globals()["PROC"] = saved
        cases.append((not fired_wrong,
                      f"zombie check ignores another slot's Worker (fired={fired_wrong})"))
        cases.append((fired_right,
                      f"zombie check fires for its own slot's Worker (fired={fired_right})"))

    # criterion 5 coverage: spared ONLY because it started after the live Worker
    # (age is past grace, so criterion 4 cannot spare it)
    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 600
    t[50] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 400}
    cases.append(_case("detached process started with the live job is spared", t,
                       [H1, H2], [], grace=300))

    # two Workers in one slot: the OLDEST decides, so a process that started after it
    # is still part of a live job even though the newer Worker is younger than it
    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 7200
    t[103] = {"comm": "Runner.Worker", "cmdline": f"{H1}/bin/Runner.Worker spawnclient 3 4",
              "ppid": 100, "start": NOW - 600}
    t[51] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 3600}
    cases.append(_case("the oldest Worker of the slot decides", t, [H1, H2], []))

    # actions-runner vs actions-runner-2 must not be confused by prefix
    t = copy.deepcopy(base)
    for pid in (101, 10, 11, 102):
        del t[pid]
    t[103] = {"comm": "Runner.Worker", "cmdline": f"{H2}/bin/Runner.Worker spawnclient 3 4",
              "ppid": 200, "start": NOW - 600}
    t[52] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 400}
    cases.append(_case("a Worker in the other slot does not spare this slot", t,
                       [H1, H2], [52], grace=300))

    # exactly at the grace boundary: the rule reaps at >= grace
    t = copy.deepcopy(base)
    for pid in (101, 10, 11, 102):
        del t[pid]
    t[53] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 900}
    cases.append(_case("process exactly at --grace is reaped", t, [H1, H2], [53]))

    # a candidate that starts at the same instant as the slot's Worker is spared
    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 600
    t[55] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 600}
    cases.append(_case("candidate starting with the Worker is spared", t, [H1, H2], []))

    # ... and criterion 5 must consult THIS slot's Worker, not the first slot's
    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 600
    t[56] = {"comm": "cargo", "cmdline": "cargo check", "ppid": 1,
             "cwd": f"{H2}/_work/plana/plana", "start": NOW - 400}
    cases.append(_case("slot 2 uses its own Worker, not slot 1's", t, [H1, H2], [56],
                       grace=300))

    # the tie must be tested with a fixture where criterion 4 cannot spare it, or the
    # case passes for the wrong reason (this is how the >= boundary stayed invisible)
    t = copy.deepcopy(base)
    t[101]["start"] = NOW - 3600
    t[57] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 3600}
    cases.append(_case("candidate starting exactly with the Worker is spared", t,
                       [H1, H2], [], grace=300))

    # criterion 5 must stay armed when the runner renames its argv: comm is the fallback
    t = copy.deepcopy(base)
    t[101] = {"comm": "Runner.Worker", "cmdline": f"{H1}/bin/Runner.Worker",
              "ppid": 100, "start": NOW - 3600}
    t[58] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 1000}
    cases.append(_case("a renamed Worker still protects its slot", t, [H1, H2], [],
                       grace=300))

    # ... and the slot must still be attributable when the argv carries no path
    t = copy.deepcopy(base)
    t[101] = {"comm": "Runner.Worker", "cmdline": "Runner.Worker spawnclient 1 2",
              "ppid": 100, "cwd": H1, "start": NOW - 3600}
    t[59] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 1000}
    cases.append(_case("a Worker without an absolute argv is attributed by cwd", t,
                       [H1, H2], [], grace=300))

    # the runner writes .runner with a UTF-8 BOM; a bare json.loads fails on it and the
    # whole zombie check goes inert
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "actions-runner")
        os.makedirs(os.path.join(home, "_work"))
        runner = os.path.join(home, ".runner")
        saved = PROC
        globals()["PROC"] = FakeProc(
            {runner: {"text": "\ufeff" + json.dumps({"agentName": "node-ci-9"})}})
        try:
            name = read_agent_name(home)
        finally:
            globals()["PROC"] = saved
        cases.append((name == "node-ci-9",
                      f"read_agent_name tolerates the .runner BOM (got {name!r})"))

    # sccache is a build tool too
    t = copy.deepcopy(base)
    for pid in (101, 10, 11, 102):
        del t[pid]
    t[54] = {"comm": "sccache", "cmdline": "sccache rustc", "ppid": 1,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 7200}
    cases.append(_case("a stale sccache wrapper is reaped", t, [H1, H2], [54]))

    # the observed sccache daemon lives with cwd=/ (probed on node-ci-2 and node-ci-4,
    # 2026-10-03), which criterion 2 excludes no matter how old it is
    t = copy.deepcopy(base)
    t[60] = {"comm": "sccache", "cmdline": "sccache --start-server",
             "ppid": 1, "cwd": "/", "start": NOW - 6 * 3600}
    cases.append(_case("the sccache daemon outside a work dir is never a candidate",
                       t, [H1, H2], []))

    t = copy.deepcopy(base)
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

    t = copy.deepcopy(base)
    t[400] = {"comm": "RunnerService", "cmdline": "RunnerService", "ppid": 1}
    t[31] = {"comm": "rustc", "cmdline": "rustc", "ppid": 400,
             "cwd": f"{H1}/_work/plana/plana", "start": NOW - 20 * 3600}
    cases.append(_case("RunnerService ancestry still protects", t, [H1, H2], []))

    t = copy.deepcopy(base)
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

    t = copy.deepcopy(base)
    t[20] = {"comm": "cargo", "cmdline": "cargo bench", "ppid": 1,
             "cwd": f"{H1}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    t[21] = {"comm": "rustc", "cmdline": "rustc", "ppid": 1,
             "cwd": f"{H2}/_work/entelecheia/entelecheia", "start": NOW - 20 * 3600}
    old = old_rule(t, [H1, H2])
    controls = [
        (old == [], f"old rules saw {old} (repo exemption + single home hide both)"),
    ]

    # fixtures must not alias each other (shallow copies once leaked a mutation)
    cases.append((base[101]["comm"] == "Runner.Worker" and base[10]["ppid"] == 102,
                  "selftest fixtures do not leak into each other"))

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
                    help="with --diag-dir: the unit to restart. Requires --diag-dir, and "
                         "is refused if it does not serve that diag's runner home.")
    ap.add_argument("--diag-dir", default=None,
                    help="override: use this single _diag dir and --unit only")
    ap.add_argument("--skip-zombie", action="store_true")
    ap.add_argument("--skip-orphans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.unit and not args.diag_dir:
        print("janitor: --unit needs --diag-dir; the per-slot units are resolved "
              "automatically otherwise", file=sys.stderr)
        return 2

    homes = runner_homes()
    if not args.skip_zombie:
        if args.diag_dir:
            # an unattributable override is refused inside zombie_worker_check, and the
            # unit is derived from the diag's own slot - defaulting to
            # <hostname>.service would target the first slot for a second-slot diag
            home = slot_home_for_diag(args.diag_dir)
            unit = args.unit
            if unit is not None:
                # the cancellation was read from this home's diag, so the unit that gets
                # restarted must be the one serving it
                if home is None or not unit_serves_home(unit, home):
                    print(f"janitor: WARN --unit {unit} does not serve {args.diag_dir}; "
                          f"refusing to restart it")
                    unit = None
            elif home is not None:
                unit = slot_units([home]).get(home)
            slots = [(home, unit, args.diag_dir)]
        else:
            slots = [(home, unit, os.path.join(home, "_diag"))
                     for home, unit in slot_units(homes).items()]
        for home, unit, diag in slots:
            if unit is None:
                print(f"janitor: WARN no unit for {diag}; skipping its zombie check")
                continue
            # a restart must not swallow this cycle's orphan reaping
            zombie_worker_check(diag, args.cancel_grace, args.dry_run, unit, home)

    if args.skip_orphans:
        return 0
    return reap(args.grace, homes, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"janitor: ERROR {exc}", file=sys.stderr)
        sys.exit(3)
