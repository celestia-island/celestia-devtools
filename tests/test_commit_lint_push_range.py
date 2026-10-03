"""Pin the commit-lint workflow's push-step degradation ranges.

The "Lint all new commits on push" step degrades to a single-commit range
in two cases (a brand-new branch, and a force-push whose ``before`` sha the
clone does not carry). Both degradations MUST use ``${AFTER}^!`` — "exactly
this commit, none of its parents".

Why this is load-bearing (2026-10-03 incident, recorded in the org ledger):
the original implementation used a BARE ``$AFTER``, and ``git log <sha>``
walks the FULL ancestor history — so every force-pushed PR in every caller
repo failed the lint on whatever pre-rule subject happened to sit in its
history (observed: chest PRs tripping over the pre-2026-09 ``#682`` squash
subject). The only safe escape was closing the PR and reopening from a
clean branch (done twice: chest #1321→#1327 and #1335→#1340).

This test pins BOTH branches of the YAML (a revert of either turns it red)
and reproduces the underlying git semantics so the operator's meaning
("just the new head") is executable documentation.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "commit-msg-lint.yml"
)


def _push_step_source() -> str:
    """Extract the 'Lint all new commits on push' step's run block."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"name: Lint all new commits on push.*?run: \|\n(.*?)(?=\n {6,}- name:|\Z)",
        text,
        re.DOTALL,
    )
    assert match, "the push-step must keep its name and inline run block"
    return match.group(1)


def test_new_branch_degradation_is_single_commit() -> None:
    """The zero-``before`` branch (brand-new branch) must use ``^!``."""
    source = _push_step_source()
    segment = re.search(
        r'if \[ "\$BEFORE" = "0+" \]; then\n(.*?)\n\s*else',
        source,
        re.DOTALL,
    )
    assert segment, "the new-branch branch must stay a separate arm"
    body = segment.group(1)
    assert 'RANGE="${AFTER}^!"' in body, (
        "a bare $AFTER makes git log walk the FULL ancestor history — "
        "pre-rule subjects then fail the lint on every new branch "
        "(2026-10-03 incident)"
    )
    assert 'RANGE="$AFTER"' not in body.replace('RANGE="${AFTER}^!"', "")


def test_force_push_degradation_is_single_commit() -> None:
    """The unreachable-``before`` branch (force-push) must use ``^!``."""
    source = _push_step_source()
    segment = re.search(
        r'if ! git rev-parse --verify -q "\$BEFORE\^\{commit\}".*?; then\n(.*?)\n\s*else',
        source,
        re.DOTALL,
    )
    assert segment, "the force-push degradation branch must stay a separate arm"
    body = segment.group(1)
    assert 'RANGE="${AFTER}^!"' in body, (
        "a bare $AFTER makes git log walk the FULL ancestor history — "
        "every force-pushed PR would then fail on pre-rule subjects "
        "(observed on chest #1321 and #1335)"
    )
    assert 'RANGE="$AFTER"' not in body.replace('RANGE="${AFTER}^!"', "")


def test_branch_deletion_push_exits_cleanly() -> None:
    """A deletion push (after = zero sha) must exit 0, not feed a
    ``$BEFORE..0000…`` range to git log (exit 128)."""
    source = _push_step_source()
    assert re.search(
        r'if \[ "\$AFTER" = "0+" \]; then\n'
        r'\s*echo "branch deletion — nothing to lint"\n'
        r"\s*exit 0\n"
        r"\s*fi",
        source,
    ), "the deletion guard must stay the first check in the step"


def test_git_log_caret_bang_means_exactly_one_commit() -> None:
    """Executable documentation: ``<sha>^!`` selects the commit alone.

    Builds a real repository whose history carries a pre-rule (non-
    compliant) subject under a compliant head, and proves the operator
    the workflow relies on: the ``^!`` range yields EXACTLY the head's
    subject, where a bare sha would yield the whole history (and fail).
    """
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", tmp, *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        subprocess.run(["git", "init", "-q", tmp], check=True)
        # --no-verify: this machine's GLOBAL core.hooksPath points at the
        # devtools commit-msg hook, which would reject the deliberately
        # pre-rule "ancient" subject before the fixture could exist.
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "-C", tmp, "commit", "--allow-empty", "--no-verify",
             "-q", "-m", "old style without a gitmoji"],
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", tmp, "commit", "--allow-empty", "--no-verify",
             "-q", "-m", "✨ Compliant head."],
            env=env,
            check=True,
            capture_output=True,
        )
        head = git("rev-parse", "HEAD")

        bare = git("log", "--format=%s", head).splitlines()
        fixed = git("log", "--format=%s", f"{head}^!").splitlines()

        # The bare form is exactly the trap: it drags the ancestor in.
        assert bare == ["✨ Compliant head.", "old style without a gitmoji"]
        # The ^! form is the honest degradation: just the new head.
        assert fixed == ["✨ Compliant head."]
