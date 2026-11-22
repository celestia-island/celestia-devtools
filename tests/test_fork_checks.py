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

*How "must reach the job" is spelled since 2026-11-20:* both workflows became
``workflow_dispatch`` + ``workflow_call`` only (the 2026-11-06 dispatch-only
trigger policy), so they can no longer RECEIVE a ``pull_request`` event. At
that point the ``event_name != 'pull_request' || ...`` guard is constant true
for every reachable event, and #294 deleted it — correctly. This test therefore
asserts the property twice over: a workflow that cannot receive a PR event must
carry no fork clause (a dead ``false`` is the very 466d207 breakage), and one
that can must carry the clause that serves forks. Adding a ``pull_request``
trigger back makes the second duty live again, and the suite says so.

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
    """True when some trigger could deliver a ``pull_request`` event."""
    triggers = _triggers(filename)
    return any((trigger == PR_ONLY) or trigger.startswith(f"{PR_ONLY}_") for trigger in triggers)

@pytest.mark.parametrize("filename,job_id", sorted(FORK_SERVED.items()))
class test_fork_pull_requests_are_served:
    def test_job_is_not_skipped_for_forks(self, filename: str, job_id: str) -> None:
        """A fork PR must reach the job, or the required check never reports.

        The duty is: **no job-level condition may filter a fork out**. 466d207
        broke it with a guard that skipped fork PRs; #160/#294 made the same
        workflows dispatch-only, at which point a guard mentioning
        ``head.repo.fork`` can no longer evaluate — the event name is
        ``workflow_dispatch``/``workflow_call``, never ``pull_request``, so the
        old ``event_name != 'pull_request' || ...`` form was constant true and
        #294 correctly deleted it.

        So this asserts the PROPERTY, not the deleted literal: either the
        workflow cannot receive a ``pull_request`` event at all (nothing can
        skip a fork), or the job's condition must not name the fork fields. The
        old assertion was inverted for the first case — it demanded the very
        clause whose evaluation is impossible — which is how #294 and this test
        came to disagree with both of them green.
        """
        condition = _job(filename, job_id).get("if")

        if not _can_receive_a_pull_request(filename):
            # Unreachable by a PR event: a fork clause here would be dead code,
            # and a *false* dead clause is exactly the 466d207 breakage.
            dead_guard = f"{filename}:{job_id} carries a fork condition, but no trigger can deliver a pull_request event - the clause is dead, and a dead `false` is the 466d207 bug"
            assert (condition is None) or (FORK_MARKER not in _normalise(condition)), dead_guard
            return

        assert condition is not None, (f"{filename}:{job_id} can receive pull_request events and has no condition; the " "callees rely on the job to decide whether a run is served")
        assert "fork==true" in _normalise(condition), (f"{filename}:{job_id} no longer admits fork pull requests. A skipped job reports no " "check run, so every outside contribution stays BLOCKED - the regression 466d207 " "caused and this test exists to prevent")

    def test_the_dispatch_only_premise_is_what_the_guard_rests_on(self, filename: str, job_id: str) -> None:
        """The justification for any missing fork guard, stated as a test.

        #294 removed the guards because no trigger can produce a
        ``pull_request`` event. If a future change adds such a trigger, the
        fork duty is live again and this test says so in the same breath as the
        one above: the pair cannot both pass on a workflow that serves PRs
        without a fork clause.
        """

        if _can_receive_a_pull_request(filename):
            pytest.skip(f"{filename} can receive pull_request events; the fork clause must exist")

        assert (_job(filename, job_id).get("if") is None) or (FORK_MARKER not in _normalise(_job(filename, job_id).get("if"))), (f"{filename}:{job_id} guards on fork fields it can never receive")

    def test_job_requires_no_secret(self, filename: str, job_id: str) -> None:
        """A fork run gets no secrets, so a required one would fail every outside PR."""
        job = _job(filename, job_id)
        assert "secrets" not in job, (f"{filename}:{job_id} now consumes secrets, which are withheld from fork " "runs; outside contributions would go red instead of green")
        doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding = "utf-8"))

        for name, other in doc["jobs"].items():
            assert "secrets" not in other, (f"{filename}:{name} now consumes secrets, which are withheld from " "fork runs")

@pytest.mark.parametrize("filename", FARM_PINNED)
def test_farm_pinned_workflows_keep_the_guard(filename: str) -> None:
    """Documented scope: farm-pinned jobs stay fork-guarded until they get a lane."""
    doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding = "utf-8"))
    guarded = [job_id for job_id, job in doc["jobs"].items()if FORK_MARKER not in str(job.get("if", ""))]
    assert guarded, (f"{filename} now serves fork PRs on a self-hosted runner. That is fine only " "once the job resolves its lane from the event rather than from a caller " "input or org variable - update this test and the fix note together.")

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
