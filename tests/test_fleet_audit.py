"""Tests for the workspace canonical-caller audit (repo/fleet_audit.py, SR-01)."""

from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
import pytest
import celestia_devtools.repo.fleet_audit as fleet_audit_mod
import celestia_devtools.repo.init as init_mod
from celestia_devtools.repo.fleet_audit import GATE_FILES, main
from celestia_devtools.repo.init import PINNED_DEVTOOLS_REF, TEMPLATE_SOURCES, _ensure_workflows, render_caller

#: A commit identity for fixtures. `git commit` refuses to run without one, and
#: a CI runner has no global `user.name`/`user.email` -- two tests here went red
#: on the farm for months on `Author identity unknown` (ledger: ci-state, runner
#: `git commit` exit 128). Pinning it per command keeps the fixture hermetic.
_GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]

def git(*args: str, **kwargs) -> None:
    """Run git with the fixture identity, whatever the host has configured."""
    subprocess.run(["git", *_GIT_IDENTITY, *args], check = True, **kwargs)

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
    git("add", "-A", cwd = source)
    git("commit", "-q", "-m", "✨ Add canonical callers.", cwd = source)
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
    git("add", "-A", cwd = source)
    git("commit", "-q", "-m", "✨ Add canonical callers.", cwd = source)
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

def _repin(repo: Path, ref: str) -> None:
    """Rewrite a canonical repo's mandatory trio at another devtools ref."""

    for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml"):
        (repo / ".github" / "workflows" / filename).write_text(render_caller(TEMPLATE_SOURCES[filename], ref), encoding = "utf-8")

def test_ref_census_counts_pinned_master_and_stale(tmp_path, capsys):
    """P2-3: the census is the migration's schedule — it must see all four states."""
    canonical_repo(tmp_path, "pinned")
    _repin(canonical_repo(tmp_path, "floating"), "master")
    _repin(canonical_repo(tmp_path, "stale"), "v2026.9")
    callee_only = make_repo(tmp_path, "devtools-like")
    callee = "name: Callee\non:\n  workflow_call: {}\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"

    for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml"):
        write_workflow(callee_only, filename, callee)

    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["devtools_ref_census"] == {"pinned": 1, "master": 1, "stale": 1, "none": 1}
    assert payload["summary"]["current_pin"] == PINNED_DEVTOOLS_REF
    states = {item["name"]: item["ref_state"] for item in payload["repositories"]}
    assert states == {"devtools-like": "none", "floating": "master", "pinned": "pinned", "stale": "stale"}

def test_master_stock_warns_and_names_the_regeneration_path(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "floating")
    _repin(repo, "master")
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "pins nothing" in captured.err
    assert "init --with-workflows --force" in captured.err
    assert f"devtools refs (pin @{PINNED_DEVTOOLS_REF}): pinned=0 master=1" in captured.err

def test_mixed_repo_counts_as_master(tmp_path, capsys):
    """One floating caller is the exposure; it must not hide behind pinned siblings."""
    repo = canonical_repo(tmp_path, "mixed")
    (repo / ".github" / "workflows" / "ci-cache.yml").write_text(render_caller(TEMPLATE_SOURCES["ci-cache.yml"], "master"), encoding = "utf-8")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    (item,) = payload["repositories"]
    assert item["ref_state"] == "master"

def floating_repo(workspace: Path, name: str) -> Path:
    """A canonical repository whose callers float on ``@master``.

    That is the fleet's current stock: generator-owned bytes, only the ref is
    wrong, which is why it reports as a warning instead of a drift problem.
    """
    repo = make_repo(workspace, name)
    _ensure_workflows(repo, force = True, ref = "master")
    return repo

def extra_lane(repo: Path, uses_line: str) -> None:
    """Add a non-template caller so a hand-written ``uses:`` form can be probed.

    Job-level `uses:` with a pull_request trigger: a real reusable-workflow call
    that no template owns (a `workflow_call` header would make it a callee
    definition and the audit would skip it, which is how an earlier revision of
    these tests ended up not exercising the path they claimed to).
    """
    body = "name: Extra lane\non:\n  pull_request:\n    branches: [master]\njobs:\n  call:\n    " + uses_line + "\n"
    write_workflow(repo, "extra-lane.yml", body)

CALLEE = "celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml"

def test_floating_callers_warn_without_failing(tmp_path, capsys):
    floating_repo(tmp_path, "app")
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "floating callers in 1 repositories" in captured.err
    assert "init --with-workflows --force" in captured.err
    assert "  app" in captured.err.splitlines()

def test_strict_pins_fails_on_floating_callers(tmp_path, capsys):
    floating_repo(tmp_path, "app")
    assert main([str(tmp_path), "--strict-pins"]) == 1
    assert "floating callers in 1 repositories" in capsys.readouterr().err

def test_strict_pins_passes_when_every_caller_is_pinned(tmp_path):
    canonical_repo(tmp_path, "app")
    assert main([str(tmp_path), "--strict-pins"]) == 0

def test_strict_pins_accepts_an_older_or_a_sha_pin(tmp_path):
    # A tag or a SHA is a pin by definition (init.py's contract), so neither is a
    # failure: an older pin is an upgrade candidate, not a broken gate.
    older = make_repo(tmp_path, "older")
    _ensure_workflows(older, force = True, ref = "v2026.09")
    sha = make_repo(tmp_path, "sha-pinned")
    _ensure_workflows(sha, force = True, ref = "079e14090f7417a62cc4939b568f9eb88d97c1d0")
    assert main([str(tmp_path), "--strict-pins"]) == 0

def test_strict_pins_does_not_count_repositories_without_callers(tmp_path, capsys):
    # A checkout that never adopted a devtools gate has no ref to pin; the
    # mandatory-caller problems report it, the pin verdict must stay clean.
    canonical_repo(tmp_path, "app")
    make_repo(tmp_path, "bare")
    assert main([str(tmp_path), "--strict-pins", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["floating_repositories"] == 0
    assert payload["summary"]["pins_ok"] is True

def test_json_reports_the_pin_verdict(tmp_path, capsys):
    floating_repo(tmp_path, "app")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["current_pin"] == PINNED_DEVTOOLS_REF
    assert payload["summary"]["floating_repositories"] == 1
    assert payload["summary"]["pins_ok"] is False

def test_quoted_uses_line_still_counts_as_floating(tmp_path, capsys):
    # A quoting style change used to yield no ref at all, which read as "this
    # repository pins nothing" -- a fail-open on the check the pin exists for.
    repo = canonical_repo(tmp_path, "app")
    extra_lane(repo, 'uses: "' + CALLEE + '@master"')
    assert main([str(tmp_path), "--strict-pins"]) == 1
    payload = capsys.readouterr()
    assert "floating callers in 1 repositories" in payload.err

def test_uses_line_with_a_trailing_comment_still_counts(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    extra_lane(repo, "uses: " + CALLEE + "@master  # break-glass")
    assert main([str(tmp_path), "--strict-pins"]) == 1
    assert "floating callers in 1 repositories" in capsys.readouterr().err

def test_callee_line_without_a_ref_is_a_problem(tmp_path, capsys):
    # Unreadable is not the same as absent: it must fail closed, not read clean.
    repo = canonical_repo(tmp_path, "app")
    extra_lane(repo, "uses: " + CALLEE)
    assert main([str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert any("yields no ref" in problem for problem in payload["repositories"][0]["problems"])

def test_strict_pins_flags_a_main_branch(tmp_path, capsys):
    # `main` floats exactly like `master`; nothing else in the census knows it.
    repo = make_repo(tmp_path, "app")
    _ensure_workflows(repo, force = True, ref = "main")
    assert main([str(tmp_path), "--strict-pins"]) == 1
    assert "floating callers in 1 repositories" in capsys.readouterr().err

def test_quoted_uses_key_is_still_read(tmp_path, capsys):
    repo = canonical_repo(tmp_path, "app")
    extra_lane(repo, '"uses": "' + CALLEE + '@master"')
    assert main([str(tmp_path), "--strict-pins"]) == 1
    assert "floating callers in 1 repositories" in capsys.readouterr().err

def test_flow_map_caller_is_a_problem_not_a_silent_pass(tmp_path, capsys):
    # A spelling the line scan cannot see at all: the YAML-aware gate detection
    # still reports adoption, so extracting no ref anywhere must fail closed.
    repo = canonical_repo(tmp_path, "app")
    body = "name: Extra lane\non: {pull_request: {}}\njobs:\n  call: {uses: " + CALLEE + "@master}\n"
    write_workflow(repo, "extra-lane.yml", body)
    assert main([str(tmp_path), "--strict-pins", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    app = next(item for item in payload["repositories"] if item["name"] == "app")
    assert app["pin_unreadable"] is True
    assert payload["summary"]["pins_ok"] is False

def test_pinned_uses_with_a_floating_tool_input_is_flagged(tmp_path, capsys):
    # The README's two-layer commitment: pinning the workflow file while letting
    # the callee install the tool from master is a cosmetic pin.
    repo = canonical_repo(tmp_path, "app")
    path = repo / ".github" / "workflows" / "commit-msg-lint.yml"
    text = path.read_text(encoding = "utf-8")
    path.write_text(text.replace("devtools-ref: " + PINNED_DEVTOOLS_REF, "devtools-ref: master"), encoding = "utf-8")
    assert main([str(tmp_path), "--strict-pins"]) == 1
    assert "floating callers in 1 repositories" in capsys.readouterr().err

def test_warn_only_and_strict_pins_are_mutually_exclusive(tmp_path):
    canonical_repo(tmp_path, "app")

    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path), "--warn-only", "--strict-pins"])

    assert excinfo.value.code == 2

def test_error_payload_does_not_claim_pins_are_fine(tmp_path, capsys):
    # A scan that never finished cannot establish the property; a consumer
    # reading pins_ok must not see True for a run that did not look.
    assert main([str(tmp_path / "nope"), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["pins_ok"] is False
    assert payload["summary"]["floating_repositories"] is None

# ── SHA pins (scheme ruling 2026-10-31) ─────────────────────────────────────

def test_pin_position_orders_sha_pins_without_downgrade_advice(monkeypatch):
    """Exact-anchor SHA reads baseline; a different SHA reads custom with an
    advance-to-anchor hint — never a downgrade path two CalVer tags would take."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(init_mod, "PINNED_DEVTOOLS_REF", sha)
    assert init_mod.pin_position(sha) == "baseline"
    other = "fedcba9876543210fedcba9876543210fedcba98"
    assert init_mod.pin_position(other) == "custom"
    warning = init_mod._pin_warning("ci.yml", "pinned", other, "regen-cmd")
    assert "does not order a SHA" in warning
    assert "regenerate" in warning

def test_sha_pins_get_local_cohort_labels(tmp_path, capsys):
    """A 40-hex pin is labelled via the workspace's devtools checkout:
    nearest containing tag + commit date, so the census stays readable."""
    canonical_repo(tmp_path, "sha-pinned")

    # A local devtools checkout whose history contains a tag and the pinned SHA.
    devtools = make_repo(tmp_path, "celestia-devtools")
    (devtools / "marker").write_text("initial\n", encoding = "utf-8")
    subprocess.run(["git", "-C", str(devtools), "add", "marker"], check = True, capture_output = True)
    subprocess.run(["git", "-C", str(devtools), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "✨ Add marker one."], check = True, capture_output = True)
    subprocess.run(["git", "-C", str(devtools), "tag", "v2026.11"], check = True, capture_output = True)
    (devtools / "marker").write_text("second\n", encoding = "utf-8")
    subprocess.run(["git", "-C", str(devtools), "add", "marker"], check = True, capture_output = True)
    subprocess.run(["git", "-C", str(devtools), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "✨ Add marker two."], check = True, capture_output = True)
    real_sha = subprocess.run(["git", "-C", str(devtools), "rev-parse", "HEAD"], check = True, capture_output = True, text = True).stdout.strip()
    _repin(tmp_path / "sha-pinned", real_sha)

    assert main([str(tmp_path), "--json"]) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    labels = payload["summary"]["sha_pin_labels"]
    assert real_sha in labels, labels
    assert labels[real_sha].startswith("v2026.11-1-g"), labels
    assert labels[real_sha] != real_sha, "the label must carry the cohort tag, not just the sha"

def test_unknown_sha_pin_gets_an_honest_unresolved_label(tmp_path):
    """Both unresolvable paths say so explicitly — no invented label."""
    canonical_repo(tmp_path, "far-pinned")
    ghost = "9999999999999999999999999999999999999999"
    _repin(tmp_path / "far-pinned", ghost)
    # No devtools checkout in the workspace at all:
    report = fleet_audit_mod.audit_workspace(tmp_path)
    assert report.sha_labels.get(ghost) == "sha (no local devtools checkout to resolve)", report.sha_labels
    # A devtools checkout that simply lacks the object:
    make_repo(tmp_path, "celestia-devtools")
    report = fleet_audit_mod.audit_workspace(tmp_path)
    assert report.sha_labels.get(ghost) == "sha (not in local devtools object store)", report.sha_labels
