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

*A note on scope:* the other reusable workflows (``p0-gate``,
``family-versions``, ``verify-versions``; ``rules-lint`` takes its lane as an
input) still pin ``runs-on`` to the self-hosted fleet, so opening them to forks
would put untrusted content on org runners. They keep the guard until each
grows a lane override; ``test_farm_pinned_workflows_keep_the_guard`` records
that this is deliberate rather than forgotten.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: The clause that makes a fork PR reachable, whitespace-normalised so that
#: reformatting the YAML does not break the contract while a semantic flip
#: (``== false``, a dropped clause) still does.
FORK_CLAUSE = "head.repo.fork==true"

#: Reusable workflows served to the fleet as required checks and reachable by
#: fork PRs after the fix: both jobs are sub-minute, stdlib-only, hold no
#: secrets, and never execute the pull request's code.
FORK_SERVED = {
    "commit-msg-lint.yml": "lint",
    "pr-title-check.yml": "title",
}

#: Farm-pinned workflows that must stay guarded until they grow a lane
#: override — opening them would spend shared runner time on untrusted content.
FARM_PINNED = ("p0-gate.yml", "family-versions.yml", "verify-versions.yml")

FORK_MARKER = "head.repo.fork"


def _normalise(expr: object) -> str:
    return re.sub(r"\s+", "", str(expr))


def _job(filename: str, job_id: str) -> dict:
    doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))
    return doc["jobs"][job_id]


@pytest.mark.parametrize("filename,job_id", sorted(FORK_SERVED.items()))
class TestForkPullRequestsAreServed:
    def test_job_is_not_skipped_for_forks(self, filename: str, job_id: str) -> None:
        """A fork PR must reach the job, or the required check never reports."""
        condition = _job(filename, job_id).get("if")
        assert condition is not None, (
            f"{filename}:{job_id} lost its condition entirely; the callers rely on "
            "the callee to decide whether a run is served"
        )
        assert FORK_CLAUSE in _normalise(condition), (
            f"{filename}:{job_id} no longer admits fork pull requests (expected the "
            f"clause {FORK_CLAUSE!r}). A skipped job reports no check run, so every "
            "outside contribution stays BLOCKED - the exact regression 466d207 caused "
            "and this test exists to prevent."
        )

    def test_job_requires_no_secret(self, filename: str, job_id: str) -> None:
        """A fork run gets no secrets, so a required one would fail every outside PR."""
        job = _job(filename, job_id)
        assert "secrets" not in job, (
            f"{filename}:{job_id} now consumes secrets, which are withheld from fork "
            "runs; outside contributions would go red instead of green"
        )
        doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))
        for name, other in doc["jobs"].items():
            assert "secrets" not in other, (
                f"{filename}:{name} now consumes secrets, which are withheld from "
                "fork runs"
            )


@pytest.mark.parametrize("filename", FARM_PINNED)
def test_farm_pinned_workflows_keep_the_guard(filename: str) -> None:
    """Documented scope: farm-pinned jobs stay fork-guarded until they get a lane."""
    doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))
    guarded = [
        job_id
        for job_id, job in doc["jobs"].items()
        if FORK_MARKER not in str(job.get("if", ""))
    ]
    assert guarded, (
        f"{filename} now serves fork PRs on a self-hosted runner. That is fine only "
        "once the job resolves its lane from the event rather than from a caller "
        "input or org variable - update this test and the fix note together."
    )
