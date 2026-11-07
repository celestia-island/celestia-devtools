"""The reusable lint callees must cap their job runtime (SR-16 follow-up).

Caller jobs cannot set `timeout-minutes` (AGENTS [[ci]]), so the bound has to
live in the callee. Without it, a caller that pins the self-hosted farm through
`runner` runs with no upper bound at all — fuuka's pr-title-check did exactly
that.
"""

from __future__ import annotations
from pathlib import Path
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

def test_lint_callees_cap_their_job_runtime():
    for filename, job in (("commit-msg-lint.yml", "lint"), ("pr-title-check.yml", "title")):
        document = yaml.safe_load((WORKFLOWS / filename).read_text(encoding = "utf-8"))
        assert document["jobs"][job]["timeout-minutes"] == 60, filename

def test_lint_callees_keep_the_lane_fallback():
    for filename in ("commit-msg-lint.yml", "pr-title-check.yml"):
        text = (WORKFLOWS / filename).read_text(encoding = "utf-8")
        assert "vars.LINT_RUNNER || '[\"ubuntu-latest\"]'" in text, filename
