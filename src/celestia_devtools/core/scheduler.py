"""Generic DAG scheduler with a worker cap.

The gate orchestrator (and any future pipeline) builds a list of :class:`Step`
tuples and hands them to :func:`run_dag` together with a *runner*.  The
scheduler is engine-agnostic: it never knows whether a step is cargo, pnpm,
ruff, or an in-process Python check — it only manages two concerns:

* **dependency ordering** — a step runs only after every step it depends on
  has completed with a satisfiable status (``PASS`` or soft-``SKIP``); a step
  whose dependency ``FAIL``ed is never started and is marked ``SKIP_DEP``.
* **concurrency budget** — at most *jobs* steps execute at the same time,
  enforced by a bounded thread pool.

Step statuses
-------------

``PASS``       completed successfully.
``FAIL``       completed with an error (downstream dependents are skipped).
``SKIP``       conditionally skipped (``cmd is None``, or the runner returned
               ``SKIP``); treated as satisfied by dependents.
``SKIP_DEP``   skipped because an upstream dependency ``FAIL``ed.

A :class:`Step` is a 4-tuple ``(name, cmd, deps, cwd)``:

* ``name`` — unique step id.
* ``cmd`` — one of: a command ``list[str]`` (external subprocess), a
  ``Callable[[], str]`` returning a status string (in-process check), or
  ``None`` (conditional skip).
* ``deps`` — tuple of step names that must complete first.
* ``cwd`` — working directory for the runner (ignored by the scheduler).
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple, Union

# A command is either an external argv list or an in-process callable that
# returns a status string ("PASS" / "FAIL" / "SKIP").
StepCommand = Union[List[str], Callable[[], str]]


class Step(NamedTuple):
    name: str
    cmd: Optional[StepCommand]
    deps: Tuple[str, ...] = ()
    cwd: Optional[str] = None


# A runner executes one step and returns its status string.
Runner = Callable[[Step], str]

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
SKIP_DEP = "SKIP_DEP"

_VALID_STATUSES = {PASS, FAIL, SKIP, SKIP_DEP}


def validate_steps(steps: List[Step]) -> List[str]:
    """Return a list of graph problems (duplicate names, unknown deps, cycles).

    An empty list means the graph is well-formed and safe to schedule.
    """
    problems: List[str] = []
    names = [s.name for s in steps]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        problems.append("duplicate step names: " + ", ".join(dupes))
    by_name = {s.name: s for s in steps}
    for s in steps:
        for d in s.deps:
            if d not in by_name:
                problems.append("step '%s' depends on unknown step '%s'" % (s.name, d))
    if not problems:
        cycle = _find_cycle(by_name)
        if cycle:
            problems.append("dependency cycle: " + " -> ".join(cycle))
    return problems


def _find_cycle(by_name: Dict[str, Step]) -> Optional[List[str]]:
    """Return one dependency cycle as a list of step names, or ``None``."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in by_name}
    path: List[str] = []

    def dfs(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)
        for dep in by_name[node].deps:
            if color[dep] == GRAY:
                return path[path.index(dep):] + [dep]
            if color[dep] == WHITE:
                found = dfs(dep)
                if found:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for name in by_name:
        if color[name] == WHITE:
            found = dfs(name)
            if found:
                return found
    return None


def run_dag(steps: List[Step], runner: Runner, jobs: int = 1) -> Dict[str, str]:
    """Run a DAG of steps with a worker cap and return ``{name: status}``.

    * *steps* — the step graph (validated before scheduling; a malformed graph
      raises ``ValueError``).
    * *runner* — callable ``runner(step) -> str`` returning ``PASS`` / ``FAIL``
      / ``SKIP``.  The scheduler never calls the runner for ``cmd is None``
      steps or for steps whose dependencies already failed.
    * *jobs* — maximum number of steps executing concurrently.

    Fail-fast propagation: when a step returns ``FAIL``, every step that
    (transitively) depends on it is marked ``SKIP_DEP`` and never started.
    Steps already running in parallel, and steps unrelated to the failure,
    are left to finish normally.
    """
    if jobs < 1:
        raise ValueError("jobs must be >= 1, got %d" % jobs)
    steps = list(steps)
    problems = validate_steps(steps)
    if problems:
        raise ValueError("invalid step graph: " + "; ".join(problems))

    by_name = {s.name: s for s in steps}
    statuses: Dict[str, str] = {}
    remaining = set(by_name)

    def ready(step: Step) -> Optional[bool]:
        """Return True (run), False (skip — dep failed), or None (wait)."""
        for dep in step.deps:
            status = statuses.get(dep)
            if status is None:
                return None
            if status in (FAIL, SKIP_DEP):
                return False
        return True

    futures: Dict[object, Step] = {}
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        while remaining or futures:
            # Dispatch every currently-ready step (the executor enforces the cap).
            for name in sorted(remaining):
                step = by_name[name]
                state = ready(step)
                if state is None:
                    continue
                remaining.discard(name)
                if state is False:
                    statuses[name] = SKIP_DEP
                elif step.cmd is None:
                    statuses[name] = SKIP
                else:
                    futures[executor.submit(runner, step)] = step

            if not futures:
                # No runnable work remains. Anything still pending is unreachable
                # (defensive; validate_steps already rejects cycles/unknown deps).
                for name in remaining:
                    statuses[name] = SKIP_DEP
                remaining.clear()
                break

            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                step = futures.pop(future)
                try:
                    status = future.result()
                except Exception:
                    status = FAIL
                statuses[step.name] = status if status in _VALID_STATUSES else FAIL

    return statuses
