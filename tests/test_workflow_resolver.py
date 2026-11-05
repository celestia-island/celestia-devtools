"""Regression tests for the commit-lint callee's checkout-ref resolver.

Why this file exists
--------------------
The resolver lives inside `.github/workflows/commit-msg-lint.yml` as an inline
`run:` block, so nothing in the Python package exercises it. That makes the
branch that actually fixes the 2026-11-05 incident — "merge ref gone, fall
back to the head SHA" — reachable only on GitHub, i.e. untested. A mutation
that deletes the fallback would leave the whole suite green.

So this test extracts the real `run:` script out of the YAML, puts a fake
`gh` on PATH, and asserts the four outcomes the workflow depends on:

  1. open PR, merge ref present          → refs/pull/<n>/merge
  2. merged PR, merge ref 404            → head SHA
  3. workflow_dispatch with a head SHA   → that head SHA
  4. no PR payload (merge_group)         → github.ref

It also pins the two guard rails that keep the resolver honest: the `gh` probe
is wrapped in `timeout`, and the script writes to `$GITHUB_ENV` (not stdout)
so the next step can read it.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CALLEE = WORKFLOWS / "commit-msg-lint.yml"
STEP_NAME = "Resolve the checkout ref"


def _resolver_script() -> str:
    doc = yaml.safe_load(CALLEE.read_text(encoding = "utf-8"))
    for step in doc["jobs"]["lint"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"step {STEP_NAME!r} not found in {CALLEE}")


def _run_resolver(
    tmp_path: Path,
    *,
    event_name: str,
    gh_rc: int,
    pr_number: str = "",
    head_sha: str = "",
    github_ref: str = "refs/heads/master",
) -> tuple[str, str]:
    """Execute the real resolver script in a sandbox; return (CHECKOUT_REF, log)."""
    script = _resolver_script()
    # GitHub substitutes `${{ ... }}` before the shell sees the step; do the
    # same for the only expression the script uses.
    script = script.replace("${{ github.event_name }}", event_name)
    assert "${{" not in script, "the resolver gained an expression this test cannot render"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nexit {gh_rc}\n", encoding = "utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding = "utf-8")
    script_path = tmp_path / "resolver.sh"
    script_path.write_text(script, encoding = "utf-8")

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "GITHUB_ENV": str(env_file),
        "GITHUB_REPOSITORY": "celestia-island/hikari",
        "GITHUB_REF": github_ref,
        "PR_NUMBER": pr_number,
        "HEAD_SHA": head_sha,
        "GH_TOKEN": "fake-token",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        ["/bin/bash", str(script_path)],
        capture_output = True,
        text = True,
        env = env,
        timeout = 30,
    )
    assert proc.returncode == 0, f"resolver failed: {proc.stdout}\n{proc.stderr}"
    written = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding = "utf-8").splitlines()
        if "=" in line
    )
    return written.get("CHECKOUT_REF", ""), proc.stdout


def test_open_pr_keeps_the_merge_ref(tmp_path: Path) -> None:
    """The merge ref is the tree GitHub would merge — keep it while it exists."""
    ref, log = _run_resolver(
        tmp_path, event_name = "pull_request", gh_rc = 0, pr_number = "42", head_sha = "a" * 40
    )
    assert ref == "refs/pull/42/merge"
    assert "merge ref present" in log


def test_merged_pr_falls_back_to_the_head_sha(tmp_path: Path) -> None:
    """The 2026-11-05 incident: after a squash merge the merge ref is gone.

    Pre-fix this shape checked out `refs/pull/<n>/merge` and died in Checkout
    with `fatal: couldn't find remote ref` — a red required check on an already
    green PR. The fallback branch is the fix; deleting it must red this test.
    """
    ref, log = _run_resolver(
        tmp_path, event_name = "pull_request", gh_rc = 1, pr_number = "42", head_sha = "b" * 40
    )
    assert ref == "b" * 40
    assert "falling back to head SHA" in log


def test_dispatch_uses_the_resolved_head_sha(tmp_path: Path) -> None:
    """The CNB lane has no PR payload; it passes the SHA it resolved earlier."""
    ref, _ = _run_resolver(
        tmp_path, event_name = "workflow_dispatch", gh_rc = 1, head_sha = "c" * 40
    )
    assert ref == "c" * 40


def test_merge_group_keeps_github_ref(tmp_path: Path) -> None:
    """No PR payload at all: the queue ref exists for the run's lifetime."""
    queue_ref = "refs/heads/gh-readonly-queue/master/pr-42-abc"
    ref, _ = _run_resolver(
        tmp_path, event_name = "merge_group", gh_rc = 1, github_ref = queue_ref
    )
    assert ref == queue_ref


def test_resolver_writes_github_env_not_stdout(tmp_path: Path) -> None:
    """The next step reads `env.CHECKOUT_REF`, so the value must go through $GITHUB_ENV."""
    ref, log = _run_resolver(
        tmp_path, event_name = "pull_request", gh_rc = 0, pr_number = "7", head_sha = "d" * 40
    )
    assert ref == "refs/pull/7/merge"
    # The value must travel through $GITHUB_ENV — echoing it to stdout would
    # leave `env.CHECKOUT_REF` empty in the Checkout step.
    assert "CHECKOUT_REF=" not in log, "the resolver leaked the value to stdout instead of $GITHUB_ENV"


def test_probe_cannot_hang_the_required_check() -> None:
    """A hung `gh api` would hold the required check pending for the job timeout."""
    script = _resolver_script()
    probe = [line for line in script.splitlines() if "gh api" in line]
    assert probe, "resolver no longer probes the merge ref"
    joined = " ".join(probe)
    assert re.search(r"timeout\s+\d+", joined), (
        "the merge-ref probe must be wrapped in `timeout`; without it a hung "
        f"connection holds the required check pending up to timeout-minutes: {joined!r}"
    )


def test_checkout_consumes_the_resolved_ref() -> None:
    """`Checkout` must read the resolver's output, not a hard-coded ref."""
    doc = yaml.safe_load(CALLEE.read_text(encoding = "utf-8"))
    steps = {step.get("name"): step for step in doc["jobs"]["lint"]["steps"]}
    checkout = steps["Checkout"]
    assert checkout["with"]["ref"] == "${{ env.CHECKOUT_REF }}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
