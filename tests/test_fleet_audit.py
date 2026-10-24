"""Tests for the workspace canonical-caller audit (repo/fleet_audit.py, SR-01)."""

from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
from celestia_devtools.repo.fleet_audit import GATE_FILES, main
from celestia_devtools.repo.init import _ensure_workflows

def make_repo(workspace: Path, name: str) -> Path:
    repo = workspace / name
    repo.mkdir(parents = True, exist_ok = True)
    subprocess.run(["git", "init", "-q"], cwd = repo, check = True)
    return repo

def write_workflow(repo: Path, filename: str, text: str) -> None:
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents = True, exist_ok = True)
    (workflows / filename).write_text(text, encoding = "utf-8")

def canonical_repo(workspace: Path, name: str) -> Path:
    repo = make_repo(workspace, name)
    _ensure_workflows(repo)
    return repo

def test_gate_table_covers_the_nine_workflows():
    assert set(GATE_FILES) == {"commit-msg-lint", "ci-cache", "pr-title-check", "p0-gate", "family-versions", "verify-versions", "rules-lint", "release-notes", "cnb-overflow"}

def test_canonical_repository_passes(tmp_path, capsys):
    canonical_repo(tmp_path, "app")
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "1 repositories, 1 canonical, 0 with problems" in captured.err
    assert "commit-msg-lint=1" in captured.err

def test_missing_mandatory_caller_fails(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    (repo / ".github" / "workflows" / "ci-cache.yml").unlink()
    assert main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "ci-cache.yml missing" in captured.err

def test_repository_without_workflows_is_reported(tmp_path, capsys):
    make_repo(tmp_path, "bare")
    assert main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "no CI at all" in captured.err

def test_callee_definitions_are_not_drift(tmp_path):
    repo = make_repo(tmp_path, "devtools-like")
    callee = "name: Callee\non:\n  workflow_call:\n    inputs:\n      x:\n        type: string\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"

    for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml"):
        write_workflow(repo, filename, callee)

    assert main([str(tmp_path)]) == 0

def test_opt_in_gate_adoption_is_reported(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "checks.yml", "name: Checks\non:\n  pull_request:\njobs:\n  verify:\n    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["gates"]["verify-versions"] is True
    assert app["gates"]["p0-gate"] is False
    assert payload["summary"]["gates"]["verify-versions"] == {"adopted": 1, "missing": 0}

def test_json_schema_and_counts(tmp_path, capsys):
    canonical_repo(tmp_path, "ok")
    broken = canonical_repo(tmp_path, "broken")
    (broken / ".github" / "workflows" / "pr-title-check.yml").unlink()
    assert main([str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"workspace", "repositories", "summary", "errors", "ok"}
    assert payload["ok"] is False
    assert payload["summary"]["repositories"] == 2
    assert payload["summary"]["clean"] == 1
    assert payload["summary"]["with_problems"] == 1
    assert len(payload["summary"]["gates"]) == 9

def test_warn_only_exits_zero(tmp_path):
    canonical_repo(tmp_path, "broken")
    (tmp_path / "broken" / ".github" / "workflows" / "ci-cache.yml").unlink()
    assert main([str(tmp_path), "--warn-only"]) == 0

def test_empty_workspace_fails_closed(tmp_path, capsys):
    assert main([str(tmp_path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"workspace", "repositories", "summary", "errors", "ok"}
    assert payload["ok"] is False
    assert "no git checkouts" in payload["errors"][0]

def test_non_directory_fails_closed(tmp_path, capsys):
    assert main([str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err

def test_opt_in_drift_is_a_warning_not_a_failure(tmp_path, capsys):
    """Opt-in hand-edits stay warnings (and must not corrupt the JSON contract)."""
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "p0-gate.yml", "name: P0 Gate\non:\n  pull_request:\njobs:\n  gate:\n    uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@master\n    with:\n      local: manual\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["problems"] == []
    assert len(app["warnings"]) == 1
    assert "p0-gate.yml" in app["warnings"][0]
    assert app["gates"]["p0-gate"] is True

def test_origin_ref_wins_over_a_stale_worktree(tmp_path, capsys):
    """A checkout behind origin must be judged on origin (the fleet's evidence)."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd = source, check = True)
    _ensure_workflows(source)
    subprocess.run(["git", "add", "-A"], cwd = source, check = True)
    subprocess.run(["git", "commit", "-q", "-m", "✨ Add canonical callers."], cwd = source, check = True)
    clone = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", str(source), str(clone)], check = True)
    # The worktree loses the workflows (as if the checkout were stale behind a
    # change that only exists on the remote ref); origin/master still has them.
    shutil.rmtree(clone / ".github")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["ref"] == "origin/master"
    assert app["canonical"] is True, app

def test_content_equal_alias_is_canonical_with_a_rename_warning(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    workflows = repo / ".github" / "workflows"
    (workflows / "ci-cache.yml").rename(workflows / "cache-policy.yml")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["canonical"] is True
    assert any("cache-policy.yml" in warning for warning in app["warnings"])
    assert app["gates"]["ci-cache"] is True

def _clone_without_origin_head(tmp_path, branch: str):
    source = tmp_path / ("source-" + branch)
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch], cwd = source, check = True)
    _ensure_workflows(source)
    subprocess.run(["git", "add", "-A"], cwd = source, check = True)
    subprocess.run(["git", "commit", "-q", "-m", "✨ Add canonical callers."], cwd = source, check = True)
    clone = tmp_path / "app"
    subprocess.run(["git", "clone", "-q", str(source), str(clone)], check = True)
    subprocess.run(["git", "-C", str(clone), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], check = True)
    return clone

def test_origin_main_is_read_when_origin_head_is_missing(tmp_path, capsys):
    """R1: the origin/main probe must return origin/main, not a hardcoded master."""
    clone = _clone_without_origin_head(tmp_path, "main")
    shutil.rmtree(clone / ".github")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["ref"] == "origin/main"
    assert app["canonical"] is True

def test_commented_caller_does_not_count_as_adopted(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "checks.yml", "name: Checks\non:\n  pull_request:\njobs:\n  verify:\n    # uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo skipped\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["gates"]["verify-versions"] is False

def test_cnb_callee_name_is_detected(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "cnb.yml", "name: Spill\non:\n  pull_request:\njobs:\n  lane:\n    uses: celestia-island/celestia-devtools/.github/workflows/cnb-overflow.yml@master\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["gates"]["cnb-overflow"] is True
    assert payload["summary"]["gates"]["cnb-overflow"] == {"adopted": 1, "missing": 0}

def test_uses_inside_a_run_block_is_not_adopted(tmp_path, capsys):
    """R2 nit: a heredoc/run: scalar containing `uses:` is text, not a job call."""
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "checks.yml", "name: Checks\non:\n  pull_request:\njobs:\n  v:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          echo uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["gates"]["verify-versions"] is False

def test_trailing_comment_on_a_real_uses_line_still_counts(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    write_workflow(repo, "checks.yml", "name: Checks\non:\n  pull_request:\njobs:\n  v:\n    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master  # pinned\n")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["gates"]["verify-versions"] is True

def test_text_mode_shows_warnings_for_canonical_repositories(tmp_path, capsys):
    """R2 nit: a rename warning must not need --json to be seen."""
    repo = canonical_repo(tmp_path, "app")
    workflows = repo / ".github" / "workflows"
    (workflows / "ci-cache.yml").rename(workflows / "cache-policy.yml")
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "app:" in captured.err
    assert "cache-policy.yml" in captured.err
    assert "1 canonical" in captured.err

def test_error_json_carries_the_gate_summary(tmp_path, capsys):
    """R2 nit: consumers iterate summary.gates; the error path must not hand them {}."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["summary"]["gates"]) == {"commit-msg-lint", "ci-cache", "pr-title-check", "p0-gate", "family-versions", "verify-versions", "rules-lint", "release-notes", "cnb-overflow"}
    assert all(value == {"adopted": 0, "missing": 0} for value in payload["summary"]["gates"].values())
    assert payload["workspace"].startswith("/")
