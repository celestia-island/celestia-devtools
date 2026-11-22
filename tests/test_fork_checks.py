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

#: Substrings that make a job-level condition a FORK FILTER — about meaning, not
#: one spelling: the 466d207 guard compared ``head.repo.full_name``, and a test
#: that only looked for ``head.repo.fork`` passed straight over it (review of
#: #307, probe 6).
_FORK_FILTERS = ("head.repo.fork", "head.repo.full_name", "pull_request.head")

#: The 466d207 guard's FIRST clause, on its own a fork filter: it says "skip
#: every pull request that did not come from this repository". A test that only
#: looked for the fork fields missed it (review of #307, probe 7).
_PULL_REQUEST_EXCLUSION = "event_name!='pull_request'"

#: The admitting form: "when there is no pull request at all, run anyway".
_PULL_REQUEST_NULL = "pull_request==null"

#: The fork comparison inside an admitting guard — any spelling of the fields.
_FORK_FIELDS_IN_ADMITTING = "head.repo"

def _condition_text(job: dict) -> str:
    """A job's ``if:`` as whitespace-free text (``""`` when absent).

    ``if: false`` parses as a YAML BOOLEAN, not the string "false", so the value
    has to be stringified before normalising — ``job.get("if") or ""`` turned it
    into the empty string and the constant detector saw "no condition" (found
    while re-running the review probes; probe 5a survived because of it).
    """
    raw = job.get("if")

    if raw is None:
        return ""

    if isinstance(raw, bool):
        return "true" if raw else "false"

    return _normalise(raw)

def _serves_a_fork_guard(condition: str) -> bool:
    """True when ``condition`` POSITIVELY admits the non-PR case.

    The farm-pinned jobs live in a *called* workflow and see their caller's
    event, so the only safe shape is the admitting guard: run when there is no
    pull request, and when there is one, refuse forks. Its absence is what
    "runs untrusted fork content on the shared fleet" looks like.
    """
    lowered = condition.lower()
    return ("||" in condition) and (_PULL_REQUEST_NULL in lowered) and (_FORK_FIELDS_IN_ADMITTING in lowered)

def _is_fork_filter(condition: str) -> bool:
    """True when ``condition`` could exclude a fork pull request.

    The judgement is about SHAPE, in this order:

    1. a logical join (``||``) that admits the non-pull-request case
       (``build_pull_request == null || …``) is an ADMITTING guard, not a
       filter — that is the #160 fix and it must stay green;
    2. otherwise any mention of the fork fields, the full-repo-name
       comparison, or "skip anything that is not a local pull request" is a
       filter, because in a non-admitting condition each of those clauses can
       only exclude.

    Review of #307 showed why the second half cannot be a literal search alone:
    the original guard used ``head.repo.full_name``, and a test looking only for
    ``head.repo.fork`` passed straight over it (probe 6) — then the first fix
    over-corrected and rejected the admitting guard (inverse probe).
    """

    if not condition:
        return False

    lowered = condition.lower()

    if ("||" in condition) and (_PULL_REQUEST_NULL in lowered):
        return False

    if _PULL_REQUEST_EXCLUSION in lowered:
        return True

    return any(needle in lowered for needle in _FORK_FILTERS)

#: Contexts a condition may legitimately depend on. A condition naming NONE of
#: them is constant, and a constant ``false`` skips the job — hence reports no
#: check run — which is 466d207's effect without naming a fork (probes 5a/5b).
_EVENT_CONTEXTS = ("github.", "inputs.", "vars.", "needs.", "env.", "steps.", "job.", "strategy.", "matrix.")

def _is_constant(condition: str) -> bool:
    """True when ``condition`` does not depend on any event at all."""

    if not condition:
        return False

    bare = condition.removeprefix("${{").removesuffix("}}").strip().lower()

    return not any(context in bare for context in _EVENT_CONTEXTS)

@pytest.mark.parametrize("filename,job_id", sorted(FORK_SERVED.items()))
class test_fork_pull_requests_are_served:
    def test_job_is_not_skipped_for_forks(self, filename: str, job_id: str) -> None:
        """No job-level condition may filter a fork pull request out.

        This is the 466d207 property, and it is asserted ABOUT THE CONDITION —
        not about whether a ``pull_request`` event reaches the job today. A
        called workflow sees its CALLER's event name, so "the workflow is
        dispatch-only" bounds nothing: the next ``pull_request`` caller makes
        any surviving filter live again.

        The two shapes that filter a fork are both covered: a condition naming
        the fork fields (``head.repo.fork`` / ``head.repo.full_name`` — the
        466d207 guard used the second), and a constant, because
        ``if: false`` skips the job and a skipped job reports no check run,
        which is the same user-visible outcome with no fork mentioned at all.
        """
        job = _job(filename, job_id)
        condition = _condition_text(job)

        assert not _is_constant(condition), f"{filename}:{job_id} has a CONSTANT job condition ({job.get('if')!r}) — a constant false skips the job, and a skipped job reports no check run (466d207)"
        assert not _is_fork_filter(condition), f"{filename}:{job_id} filters fork pull requests ({job.get('if')!r}); a skipped job reports no check run, so every outside contribution stays BLOCKED - the regression 466d207 caused and this test exists to prevent"

    def test_the_caller_regime_is_recorded(self, filename: str, job_id: str) -> None:
        """Name the regime instead of hiding it, so the next reader knows the risk.

        The fork clause was deleted because the org's canonical CALLERS are
        dispatch-only (#264), not because this workflow's own trigger list
        settles the question — a ``pull_request`` caller delivers
        ``event_name == 'pull_request'`` to the callee. This test records which
        regime the workflow is in and asserts the consequence that matters
        either way: no filter, and no secret (asserted in the sibling test).
        """
        regime = "pull_request-capable" if _can_receive_a_pull_request(filename) else "called/dispatched-into"
        # The assertion is the invariant; the regime is the explanation. A
        # failure above reads alongside this line.
        assert not _is_fork_filter(_condition_text(_job(filename, job_id))), f"{filename}:{job_id} ({regime}) must not filter forks"

    def test_job_requires_no_secret(self, filename: str, job_id: str) -> None:
        """A fork run gets no secrets, so a required one would fail every outside PR."""
        job = _job(filename, job_id)
        assert "secrets" not in job, (f"{filename}:{job_id} now consumes secrets, which are withheld from fork " "runs; outside contributions would go red instead of green")
        doc = _doc(filename)

        for name, other in doc["jobs"].items():
            assert "secrets" not in other, (f"{filename}:{name} now consumes secrets, which are withheld from " "fork runs")

@pytest.mark.parametrize("filename", FARM_PINNED)
def test_farm_pinned_workflows_keep_the_guard(filename: str) -> None:
    """Farm-pinned jobs must not be OPENED to forks while they run untrusted code.

    Rewritten in the #307 review: the previous version selected the jobs WITHOUT
    a fork marker and asserted that list was non-empty — i.e. it passed only
    when the job was UNGUARDED, the exact opposite of its docstring. All three
    files currently carry no condition, so it was green by accident and would
    stay green while a guard was removed.

    The invariant: a job on the self-hosted fleet must not be reachable by a
    fork pull request. Since a called workflow sees its caller's event, a
    condition that names fork fields is the guard; its absence means the job
    runs for forks on the farm.
    """
    doc = _doc(filename)
    unguarded = [job_id for job_id, job in doc["jobs"].items() if not _serves_a_fork_guard(_condition_text(job))]

    assert not unguarded, f"{filename}: jobs {unguarded} run on the self-hosted fleet with no fork guard. Add the admitting clause (`if: ${{ github.event.pull_request == null || github.event.pull_request.head.repo.fork != true }}`), or move the job off the farm and update FARM_PINNED together with the fix note"

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
