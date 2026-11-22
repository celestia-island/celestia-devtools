"""Pin the fork-PR contract of the reusable required checks.

*Why this exists:* `466d207` (2026-08-08) guarded the reusable lint jobs with

    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository

so a pull request from a fork skipped the job. A skipped job reports no check
run, and a required context that never reports leaves the pull request
``BLOCKED`` forever — not "failed", not "pending", just unmergeable by anyone
except a maintainer willing to pass ``--admin`` (and the public repositories
also set ``enforce_admins``, so even that is refused). The intent was narrow
(keep untrusted content off the shared self-hosted farm) but the effect was a
blanket "we take no outside patches", a policy nobody had decided. It went
unnoticed until 2026-10-06, when seven outside patches to a public repository
turned out to have been sitting unmergeable for a month.

The fix keeps the reachability and bounds the exposure: fork PRs are served,
and they are served on whatever lane the repository resolves — the farm
included (owner decision, 2026-10-06). Pinning forks to GitHub-hosted runners
was tried first and rejected: this org's hosted lane went dark on 2026-09-24
over account payments, so jobs sit fifteen minutes with no runner assigned and
the pin would have kept outside contributions BLOCKED while looking fixed.

What makes serving a fork here acceptable is that neither job can touch the
pull request's code or secrets:

* ``commit-msg-lint`` reads commit messages; it checks out the tree but never
  executes it, and holds ``contents: read`` + ``pull-requests: read``.
* ``pr-title-check`` never checks out anything — the title comes from the event
  payload.

So the contract below is: a fork must reach the job, and the job must not
require a secret (a fork run gets none, so any required secret would turn every
outside PR red).

*Why the fork clause disappeared, measured rather than assumed:* a called
workflow inherits the **caller's** ``github`` context (GitHub docs, "reusing
workflow configurations": "the ``github`` context is always associated with the
caller workflow"). So ``workflow_call`` is NOT what the callee sees — a
``pull_request``-triggered caller DOES give the callee
``event_name == 'pull_request'``. The clause went dead because the org's
canonical CALLERS are dispatch-only: ``5fcb8db`` (#264, 2026-11-06) removed the
``pull_request`` trigger from both workflows, and ``a802e08`` (#294) then
deleted the resulting dead code. *Effect* right; *reason* not this file's own
trigger list.

*What this file therefore asserts:* the 466d207 property is "no condition may
filter a fork out of a required check". A condition that mentions ``fork`` or
the full-repo-name comparison, or that is a constant, IS such a filter —
whether or not a ``pull_request`` event can reach the job today, because a
caller can deliver one tomorrow.

*A note on scope:* the other reusable workflows (``p0-gate`` and
``rules-lint``-adjacent lanes aside) grew a ``runner`` lane override in #305:
``family-versions`` and ``verify-versions`` default to the self-hosted fleet
and may opt into hosted runners (outside-org consumers where org runners
cannot pick jobs up at all). Whether a fork may *use* the override on org
runners is a separate decision these jobs do not grant — the override only
ever moves work OFF the fleet. ``p0-gate`` still pins ``runs-on`` to the
self-hosted fleet outright; ``test_farm_pinned_workflows_keep_the_guard``
records that this is deliberate rather than forgotten.
"""

from __future__ import annotations
import re
from pathlib import Path
import pytest
import yaml
from celestia_devtools.ci import workflow_audit

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: The event names that can reach these workflows. A job-level ``if:`` that
#: only discriminates ``pull_request`` is dead weight when nothing in the
#: trigger list can produce that event — see :func:`_can_receive_a_pull_request`.
PR_ONLY = "pull_request"

#: Reusable workflows served to the fleet as required checks and reachable by
#: fork PRs after the fix: both jobs are sub-minute, stdlib-only, hold no
#: secrets, and never execute the pull request's code.
FORK_SERVED = {"commit-msg-lint.yml": "lint", "pr-title-check.yml": "title"}

#: Farm-pinned workflows that must stay guarded until they grow a lane
#: override — opening them would spend shared runner time on untrusted content.
FARM_PINNED = ("p0-gate.yml", "family-versions.yml", "verify-versions.yml")

FORK_MARKER = "head.repo.fork"

def _normalise(expr: object) -> str:
    return re.sub(r"\s+", "", str(expr))

#: ``on`` is YAML 1.1 boolean ``true`` when unquoted — the key every GitHub
#: workflow file uses.
_TRIGGER_KEYS = ("on", True)

def _doc(filename: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding = "utf-8"))

def _triggers(filename: str) -> set:
    """The event names a workflow can be started by."""
    doc = _doc(filename)
    spec = next((doc[key] for key in _TRIGGER_KEYS if key in doc), None)

    if isinstance(spec, str):
        return {spec}

    if isinstance(spec, list):
        return set(spec)

    if isinstance(spec, dict):
        return set(spec)

    return set()

def _job(filename: str, job_id: str) -> dict:
    return _doc(filename)["jobs"][job_id]

def _can_receive_a_pull_request(filename: str) -> bool:
    """True when some trigger could deliver a ``pull_request`` event.

    Says which regime a workflow is in — NOT whether a fork guard would be
    dead: a called workflow sees its CALLER's event name, so this is a lower
    bound on reachability, never an upper one.
    """
    triggers = _triggers(filename)
    return any((trigger == PR_ONLY) or trigger.startswith(f"{PR_ONLY}_") for trigger in triggers)

#: The org's canonical fork guard, imported from the ONE place that already owns
#: the definition (`ci.workflow_audit`). Re-declaring it here is how two
#: contradictory "canonical guard" notions came to coexist (review of #307).
CANONICAL_GUARD = workflow_audit.FORK_GUARD
EQUIVALENT_GUARDS = workflow_audit.FORK_GUARD_MARKERS

def _condition_text(job: dict) -> str:
    """A job's ``if:`` as whitespace-free text (``""`` when absent).

    ``if: false`` parses as a YAML BOOLEAN, so the value is stringified before
    normalising — ``job.get("if") or ""`` turned it into the empty string and a
    constant detector saw "no condition" (measured while fixing #307).
    """
    raw = job.get("if")

    if raw is None:
        return ""

    if isinstance(raw, bool):
        return "true" if raw else "false"

    return _normalise(raw)

def _guard_is_admitting(condition: str) -> bool:
    """True when ``condition`` POSITIVELY admits everything except a fork PR.

    Judged by the expression's SHAPE, not by one remembered spelling (review of
    #307 measured five paraphrases that a literal search accepted):

    * it must be a disjunction whose first arm admits the no-pull-request case
      (``github.event.pull_request == null``);
    * its arm about the head repository must EXCLUDE a fork — ``fork != true`` or
      ``full_name == github.repository`` — never the inverse.

    A condition naming an event (``event_name == 'workflow_dispatch'``) is not
    admitting: it skips every pull request, fork or not.
    """

    if ("||" not in condition) or ("pull_request==null" not in condition.lower()):
        return False

    arms = [arm.lower() for arm in condition.split("||")]

    for arm in arms:
        if "head.repo.fork" in arm:
            return ("!=true" in arm) or ("==false" in arm)

        if "head.repo.full_name" in arm:
            return "==github.repository" in arm

    return False

def _carries_fork_guard(condition: str) -> bool:
    """Either accepted guard form: the admitting one, or the canonical one.

    One predicate for every caller, so the two cannot drift into disagreeing
    the way the served-job and farm tests did (review of #307, finding S2).
    """
    return _guard_is_admitting(condition) or _is_canonical_guard(condition)

def _is_canonical_guard(condition: str) -> bool:
    """True when the condition IS the org's canonical guard, in any marker spelling.

    The canonical form opens with ``event_name != 'pull_request'`` and then
    admits same-repo PRs — a valid guard, and the one `ci.workflow_audit`
    tells repositories to use. Recognising it by the single owner's markers is
    the point: a second, home-grown notion of "canonical" is how #307's first
    two cuts came to reject the guard they meant to require.
    """
    # Quotes are stripped, so compare quote-free needles too.
    compact = "".join(condition.split()).replace("'", "")
    # Both halves are required: the event arm admits pushes and dispatches, the
    # repository arm excludes forks. A repository arm ALONE filters every
    # non-PR run out (probe 3, review of #307).
    admits_non_pr = "event_name!=pull_request" in compact

    return admits_non_pr and any(marker.replace("'", "") in compact for marker in EQUIVALENT_GUARDS)

def _is_constant(condition: str) -> bool:
    """True when ``condition`` does not depend on any event or context."""

    if not condition:
        return False

    bare = condition.removeprefix("${{").removesuffix("}}").strip().lower()

    return not any(context in bare for context in _EVENT_CONTEXTS)

#: Contexts a condition may legitimately depend on.
_EVENT_CONTEXTS = ("github.", "inputs.", "vars.", "needs.", "env.", "steps.", "job.", "strategy.", "matrix.")

#: Event tests that deny ALL pull requests when they stand ALONE. They are only
#: a filter in a single-arm condition: the org's canonical guard opens with
#: ``event_name != 'pull_request' || …`` and then serves same-repo PRs, so the
#: arm must be judged with the rest of the disjunction (review of #307 caught
#: this rejecting the canonical guard).
_PR_DENYING_ARMS = ("event_name!='pull_request'", "event_name=='workflow_dispatch'", "event_name=='workflow_call'", "event_name=='push'")

def _excludes_a_pull_request(condition: str) -> bool:
    """True when a NON-admitting condition skips pull requests entirely.

    Only single-arm event tests qualify. ``pull_request_target`` is deliberately
    NOT a filter: it is a pull-request event, and a fork reaches it — treating it
    as an exclusion left that probe green (review of #307).
    """
    lowered = condition.lower()

    if "||" in condition:
        return False

    return any(arm in lowered for arm in _PR_DENYING_ARMS)

def _is_served_job_filtered(job: dict) -> str | None:
    """The reason a job would filter a fork (or skip PRs entirely), else ``None``."""
    condition = _condition_text(job)

    if _is_constant(condition):
        return f"a CONSTANT condition ({job.get('if')!r}) — a constant false skips the job, and a skipped job reports no check run (466d207)"

    if _guard_is_admitting(condition) or _is_canonical_guard(condition):
        return None

    if _excludes_a_pull_request(condition):
        return f"{job.get('if')!r} skips pull requests entirely — the 466d207 blank check run, whatever the fork fields say"

    if condition and ("head.repo" in condition.lower()):
        return f"{job.get('if')!r} names the head repository but does not exclude a fork — the guard's INVERSE admits one"

    return None

@pytest.mark.parametrize("filename,job_id", sorted(FORK_SERVED.items()))
class test_fork_pull_requests_are_served:
    def test_job_is_not_skipped_for_forks(self, filename: str, job_id: str) -> None:
        """No job-level condition may filter a fork pull request out.

        The 466d207 property. Judged by what the condition DOES, not by a
        spelling: a constant, a PR-excluding event test, an inverted fork
        comparison and a bare ``head.repo`` mention are all caught, and the
        admitting guard in every accepted paraphrase passes (review of #307
        measured five paraphrases a literal search accepted and one it wrongly
        rejected).
        """
        job = _job(filename, job_id)
        reason = _is_served_job_filtered(job)

        assert reason is None, f"{filename}:{job_id} would not serve a fork pull request: {reason}. A skipped job reports no check run, so every outside contribution stays BLOCKED - the regression 466d207 caused and this test exists to prevent"

    def test_job_requires_no_secret(self, filename: str, job_id: str) -> None:
        """A fork run gets no secrets, so a required one would fail every outside PR."""
        job = _job(filename, job_id)
        assert "secrets" not in job, (f"{filename}:{job_id} now consumes secrets, which are withheld from fork " "runs; outside contributions would go red instead of green")
        doc = _doc(filename)

        for name, other in doc["jobs"].items():
            assert "secrets" not in other, (f"{filename}:{name} now consumes secrets, which are withheld from " "fork runs")

@pytest.mark.parametrize("filename", FARM_PINNED)
def test_farm_pinned_workflows_keep_the_guard(filename: str) -> None:
    """Farm-pinned jobs must exclude forks PRECISELY, not approximately.

    Rewritten twice after review: the original selected the jobs WITHOUT a fork
    marker and asserted that list was non-empty (green only while UNGUARDED),
    then a substring test accepted the guard's inverse. A farm job must carry a
    guard that EXCLUDES a fork — discharging to closed, never open.
    """
    doc = _doc(filename)
    unguarded = [job_id for job_id, job in doc["jobs"].items() if not _carries_fork_guard(_condition_text(job))]

    assert not unguarded, f"{filename}: jobs {unguarded} run on the self-hosted fleet without a fork-EXCLUDING guard. Use `if: {CANONICAL_GUARD}` (or the `sits: fork == false` equivalent), or move the job off the farm and update FARM_PINNED with the fix note"

def test_runner_override_falls_back_to_the_fleet_labels():
    """#305: an empty ``runner`` input must resolve to exactly the fleet
    labels both workflows hardcoded before the override existed."""
    import json

    workflows = Path(".github/workflows")
    fallback = re.compile(r"runs-on: \$\{\{ fromJSON\(inputs\.runner \|\| '([^']+)'\) \}\}")

    for name in ("verify-versions.yml", "family-versions.yml"):
        text = (workflows / name).read_text(encoding = "utf-8")
        match = fallback.search(text)
        assert match is not None, f"{name} lost the runner override expression"
        assert json.loads(match.group(1)) == ["self-hosted", "linux", "x64", "local"], (f"{name} fallback literal drifted")
        assert "runner:" in text, f"{name} lost the runner input declaration"
