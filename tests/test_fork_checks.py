"""Pin the fork-PR contract of the reusable required checks.

*Why this exists:* `466d207` (2026-08-08) guarded the reusable lint jobs with

    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository

so a pull request from a fork skipped the job. A skipped job reports no check
run, and a required context that never reports leaves the pull request
``BLOCKED`` forever — not "failed", not "pending", just unmergeable by anyone
except a maintainer willing to pass ``--admin``. The intent was narrow (keep
untrusted content off the shared self-hosted farm) but the effect was a blanket
"we take no outside patches", a policy nobody had decided. It went unnoticed
until 2026-10-06, when seven outside patches to a public repository turned out
to have been sitting unmergeable for a month.

The fix keeps the original intent and drops the side effect: fork PRs are
served, and they are pinned to the GitHub-hosted lane, which is free and
unlimited for public repositories. The two assertions below are the contract —
if either regresses, outside contributions silently become unmergeable again,
which is exactly the failure mode that produced this test.

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
#: (``== false``, a dropped clause, a reordered lane) still does.
FORK_CLAUSE = "head.repo.fork==true"


def _normalise(expr: object) -> str:
    return re.sub(r"\s+", "", str(expr))

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

    def test_fork_runs_are_pinned_to_hosted(self, filename: str, job_id: str) -> None:
        """Untrusted fork content must never land on the shared farm."""
        runs_on = _normalise(_job(filename, job_id)["runs-on"])
        assert FORK_CLAUSE in runs_on, (
            f"{filename}:{job_id} does not pin a lane for forks; a caller input or an "
            "org variable could then route untrusted content onto the farm"
        )
        assert "ubuntu-latest" in runs_on, (
            f"{filename}:{job_id} must fall back to GitHub-hosted runners for forks"
        )
        assert runs_on.index(FORK_CLAUSE) < runs_on.index("inputs.runner"), (
            f"{filename}:{job_id} consults the caller's runner input before the fork "
            "check, so a caller can re-lane a fork onto the farm"
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
