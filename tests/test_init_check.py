"""`init --check` also gates the canonical caller workflows (SR-01).

Before 2026-10-25 the check only verified the staged justfile and the
`.gitignore` rules, so a repository could run a hand-edited or missing
commit-lint/ci-cache/pr-title caller forever and `--check` stayed green. The
fleet-wide audit found 5/51 repositories missing the mandatory trio and 23/51
missing pr-title, all invisible to the gate that was supposed to catch drift.

The drift messages are asserted on `_workflow_drift_problems` (the pure gate)
rather than on captured stdout: the shared logger emits through a worker thread,
so `capfd.readouterr()` is racy by design.
"""

from __future__ import annotations

import sys

from celestia_devtools.repo import init as init_mod
from celestia_devtools.repo.init import (
    REQUIRED_WORKFLOWS,
    WORKFLOW_CI_CACHE,
    WORKFLOW_COMMIT_LINT,
    WORKFLOW_PR_TITLE_CHECK,
    _ensure_workflows,
    _workflow_drift_problems,
)

CALLEE = """\
name: Commit Message Lint

on:
  workflow_call:
    inputs:
      pr:
        required: true
        type: string

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: echo lint
"""


def _run_init(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["celestia-devtools", *args])
    return init_mod.main()


def _stage_clean_tree(monkeypatch, tmp_path):
    """A repository `init --check` considers clean: staging + three callers."""
    assert _run_init(monkeypatch, tmp_path, "--with-workflows") == 0
    return tmp_path / ".github" / "workflows"


def test_clean_tree_passes(monkeypatch, tmp_path):
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    for filename, template, _label in REQUIRED_WORKFLOWS:
        assert (workflows / filename).read_text() == template
    assert _workflow_drift_problems(workflows) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0


def test_missing_mandatory_caller_fails(monkeypatch, tmp_path):
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    (workflows / "pr-title-check.yml").unlink()
    problems = _workflow_drift_problems(workflows)
    assert any("pr-title-check.yml missing" in p for p in problems), problems
    assert _run_init(monkeypatch, tmp_path, "--check") == 1


def test_drifted_mandatory_caller_fails(monkeypatch, tmp_path):
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    (workflows / "commit-msg-lint.yml").write_text(WORKFLOW_COMMIT_LINT + "\n# hand edit\n")
    problems = _workflow_drift_problems(workflows)
    assert any("commit-msg-lint.yml drifted" in p for p in problems), problems
    assert _run_init(monkeypatch, tmp_path, "--check") == 1


def test_callee_definition_is_not_compared(monkeypatch, tmp_path):
    """celestia-devtools hosts the callees; its own tree is not drifted."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "commit-msg-lint.yml").write_text(CALLEE)
    (workflows / "ci-cache.yml").write_text(WORKFLOW_CI_CACHE)
    # Generate the missing caller and the staging files; existing files are kept.
    assert _run_init(monkeypatch, tmp_path, "--with-workflows") == 0
    assert (workflows / "commit-msg-lint.yml").read_text() == CALLEE
    assert _workflow_drift_problems(workflows) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0


def test_opt_in_drift_only_warns(monkeypatch, tmp_path):
    """Opt-in callers are hand-edited until the templates expose every input."""
    assert _run_init(monkeypatch, tmp_path, "--with-workflows", "--gate", "p0") == 0
    workflows = tmp_path / ".github" / "workflows"
    p0 = workflows / "p0-gate.yml"
    p0.write_text(p0.read_text() + "\n# local runner pin\n")
    # A drifted opt-in caller is a warning: the mandatory set still passes.
    assert _workflow_drift_problems(workflows) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0


def test_required_workflows_table_is_the_generator_source(tmp_path):
    _ensure_workflows(tmp_path)
    names = {p.name for p in (tmp_path / ".github" / "workflows").iterdir()}
    assert names == {filename for filename, _template, _label in REQUIRED_WORKFLOWS}


def test_pr_title_template_is_exact(tmp_path):
    _ensure_workflows(tmp_path)
    got = (tmp_path / ".github" / "workflows" / "pr-title-check.yml").read_text()
    assert got == WORKFLOW_PR_TITLE_CHECK


def test_callee_substring_in_a_comment_is_not_a_bypass(monkeypatch, tmp_path):
    """A drifted caller whose comment mentions `workflow_call:` must not be skipped.

    The original substring test exempted any file quoting the word — a one-line
    bypass of the whole gate (R1 finding, 2026-10-25).
    """
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    target = workflows / "commit-msg-lint.yml"
    target.write_text(
        "# the callee is invoked via workflow_call: and owns its own concurrency\n"
        + WORKFLOW_COMMIT_LINT
        + "\n# local tweak\n"
    )
    problems = _workflow_drift_problems(workflows)
    assert any("commit-msg-lint.yml drifted" in p for p in problems), problems


def test_scalar_workflow_call_callee_is_skipped(monkeypatch, tmp_path):
    """`on: workflow_call` (scalar form, no inputs) is a valid callee shape."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "commit-msg-lint.yml").write_text(
        "name: Lint\non: workflow_call\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo lint\n"
    )
    assert _run_init(monkeypatch, tmp_path, "--with-workflows") == 0
    problems = _workflow_drift_problems(workflows)
    assert problems == [], problems


def test_canonical_alias_counts_as_in_place(monkeypatch, tmp_path):
    """Eight repos landed the identical caller as `cache-policy.yml`."""
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    # The fleet shape: no ci-cache.yml, the same bytes under the old name.
    (workflows / "ci-cache.yml").unlink()
    (workflows / "cache-policy.yml").write_text(WORKFLOW_CI_CACHE)
    assert _workflow_drift_problems(workflows) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0


def test_alias_with_different_bytes_does_not_count(monkeypatch, tmp_path):
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    (workflows / "ci-cache.yml").unlink()
    (workflows / "cache-policy.yml").write_text(WORKFLOW_CI_CACHE + "# drift\n")
    problems = _workflow_drift_problems(workflows)
    assert any("ci-cache.yml missing" in p for p in problems), problems
    assert _run_init(monkeypatch, tmp_path, "--check") == 1


def test_generator_does_not_duplicate_the_alias(monkeypatch, tmp_path):
    """R2 F1: regenerating must not install a second caller beside the alias."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "cache-policy.yml").write_text(WORKFLOW_CI_CACHE)
    assert _run_init(monkeypatch, tmp_path, "--with-workflows") == 0
    assert (workflows / "cache-policy.yml").is_file()
    assert not (workflows / "ci-cache.yml").exists()
    # The canonical pr-title caller was still generated; the alias is not drift.
    assert (workflows / "pr-title-check.yml").is_file()
    assert _workflow_drift_problems(workflows) == []


def test_crlf_caller_is_drift(monkeypatch, tmp_path):
    """The gate claims byte identity: CRLF-normalised files must be drift (R3 N1)."""
    workflows = _stage_clean_tree(monkeypatch, tmp_path)
    target = workflows / "commit-msg-lint.yml"
    target.write_bytes(WORKFLOW_COMMIT_LINT.replace("\n", "\r\n").encode("utf-8"))
    problems = _workflow_drift_problems(workflows)
    assert any("commit-msg-lint.yml drifted" in p for p in problems), problems
