"""Tests for ci/cache_policy.py — the actions/cache cost gate for self-hosted runners.

Every rule is exercised against the shape that actually exists in the org's repositories,
and each one also has a negative case: a gate that fires on legitimate caches gets waived,
and then the real findings are waived with it.

One test is a regression for a withdrawn rule. An earlier revision flagged
``${{ env.CARGO_HOME }}`` as "defined nowhere, so this caches nothing" -- 50 steps across
seven repositories. Measurement proved it false: ``dtolnay/rust-toolchain`` injects
``CARGO_HOME`` via ``$GITHUB_ENV`` at runtime, so the path resolves to the real cargo home
and the archive is very much real. That revision is kept here as a test so the same wrong
rule cannot come back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

TOOLS_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(TOOLS_SRC))

from celestia_devtools.ci import cache_policy  # noqa: E402

WARN_TIMEOUT = "self-hosted-cache-without-step-timeout"


def rules(text: str) -> list:
    return [finding["rule"] for finding in cache_policy.scan_text(text, rel="r/w.yml")]


def errors(text: str) -> list:
    return [
        finding["rule"]
        for finding in cache_policy.scan_text(text, rel="r/w.yml")
        if finding["severity"] == "error"
    ]


def lines_for(text: str, rule: str) -> list:
    return [
        finding["line"]
        for finding in cache_policy.scan_text(text, rel="r/w.yml")
        if finding["rule"] == rule
    ]


def cache_step(path: str, runs_on: str = "self-hosted") -> str:
    return f"""
jobs:
  build:
    runs-on: {runs_on}
    steps:
      - uses: actions/cache@v6
        with:
          path: {path}
          key: k
"""


# ── rule: self-hosted-build-dir-cache ────────────────────────────────────────


def test_flags_target_on_self_hosted():
    text = """
name: CI
on: [push]
jobs:
  build:
    runs-on: [self-hosted, linux, x64, local]
    steps:
      - uses: actions/cache@v6
        with:
          path: |
            target/
          key: k
"""
    assert "self-hosted-build-dir-cache" in errors(text)
    assert lines_for(text, "self-hosted-build-dir-cache") == [8]


def test_flags_nested_target_and_scalar_path():
    # Quoted, because a bare `*` starts a YAML alias -- the scanner must still see through
    # the quoting, which is why normalisation strips quotes before matching.
    text = cache_step('"*/target/"')
    assert "self-hosted-build-dir-cache" in errors(text)


def test_hosted_target_cache_is_not_flagged():
    """GitHub-hosted runners are stateless, so caching target/ there is a real choice."""
    assert errors(cache_step("target/", runs_on="ubuntu-latest")) == []


def test_caching_a_narrow_subdirectory_is_not_flagged():
    assert errors(cache_step("target/debug/build")) == []


# ── rule: self-hosted-cargo-home-cache ───────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "~/.cargo",
        "$HOME/.cargo",
        "${HOME}/.cargo",
        "~/.cargo/registry",
        "~/.cargo/git",
        # The shape a static reader gets wrong: CARGO_HOME is injected at runtime by the
        # preceding dtolnay/rust-toolchain step, so this resolves to the real cargo home.
        "${{ env.CARGO_HOME }}/registry",
        "${{ env.CARGO_HOME }}/git",
        "${{ env.CARGO_HOME }}",
    ],
)
def test_flags_cargo_home_trees_on_self_hosted(path):
    assert "self-hosted-cargo-home-cache" in errors(cache_step(path))


def test_hosted_cargo_registry_is_not_flagged():
    assert errors(cache_step("~/.cargo/registry", runs_on="ubuntu-latest")) == []


def test_target_is_not_flagged_when_no_self_hosted_label_is_present():
    assert errors(cache_step("target/", runs_on="[linux, x64, local]")) == []


def test_regression_the_withdrawn_dead_path_rule_must_not_return():
    """`${{ env.CARGO_HOME }}` is resolved at runtime -- it is NOT a no-op step.

    An earlier revision reported these as "caches nothing", which implied the cargo archive
    was harmless. It is the opposite: that archive is the process measured pinned in D
    state. The rule set must flag the step, never dismiss it.
    """
    text = cache_step("${{ env.CARGO_HOME }}/registry")
    assert not any("undefined" in rule for rule in rules(text))
    assert "self-hosted-cargo-home-cache" in errors(text)


# ── rule: self-hosted-cache-without-step-timeout ─────────────────────────────


def test_warns_when_a_self_hosted_cache_step_has_no_timeout():
    text = cache_step("~/.cargo/registry")
    assert WARN_TIMEOUT in rules(text)
    assert all(
        finding["severity"] == "warning"
        for finding in cache_policy.scan_text(text, rel="r/w.yml")
        if finding["rule"] == WARN_TIMEOUT
    )


def test_no_timeout_warning_when_the_step_declares_one():
    text = """
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        timeout-minutes: 5
        with:
          path: ~/.cargo/registry
          key: k
"""
    assert WARN_TIMEOUT not in rules(text)


def test_no_timeout_warning_on_hosted_runners():
    """A job-level timeout bounds hosted minutes; the post-step problem is self-hosted."""
    assert WARN_TIMEOUT not in rules(cache_step("~/.cargo/registry", runs_on="ubuntu-latest"))


def test_job_level_timeout_does_not_silence_the_warning():
    """A job-level bound does not bound a post-job step -- that is the whole point."""
    text = """
jobs:
  build:
    runs-on: self-hosted
    timeout-minutes: 60
    steps:
      - uses: actions/cache@v6
        with:
          path: target/
          key: k
"""
    assert "self-hosted-build-dir-cache" in errors(text)
    assert WARN_TIMEOUT in rules(text)


# ── scanner plumbing ─────────────────────────────────────────────────────────


def test_line_numbers_follow_each_cache_step():
    """Two cache steps must not report the same line (the first one's)."""
    text = """
jobs:
  a:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: target/
          key: k1
  b:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: target/
          key: k2
"""
    assert lines_for(text, "self-hosted-build-dir-cache") == [6, 13]


def test_non_cache_steps_are_ignored():
    text = """
jobs:
  a:
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@v7
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo test
"""
    assert rules(text) == []


def test_cache_step_without_with_block_is_ignored():
    text = """
jobs:
  a:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
"""
    assert rules(text) == []


def test_scan_file_and_collect_walk_the_workflow_directory(tmp_path: Path):
    repo = tmp_path / "repo"
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(cache_step("target/"), encoding="utf-8")
    (workflows / "notes.md").write_text("not a workflow\n", encoding="utf-8")

    findings = cache_policy.collect([repo])
    assert {finding["rule"] for finding in findings} == {
        "self-hosted-build-dir-cache",
        WARN_TIMEOUT,
    }
    assert all(finding["path"] == "repo/.github/workflows/ci.yml" for finding in findings)
    assert any(finding["detail"].startswith("job 'build'") for finding in findings)


def test_main_exits_nonzero_on_findings_and_zero_when_clean(tmp_path: Path, capsys):
    clean = tmp_path / "clean"
    (clean / ".github" / "workflows").mkdir(parents=True)
    (clean / ".github" / "workflows" / "ci.yml").write_text(
        cache_step("target/", runs_on="ubuntu-latest"), encoding="utf-8"
    )
    assert cache_policy.main([str(clean)]) == 0
    assert "no findings" in capsys.readouterr().err

    dirty = tmp_path / "dirty"
    (dirty / ".github" / "workflows").mkdir(parents=True)
    (dirty / ".github" / "workflows" / "ci.yml").write_text(
        cache_step("target/"), encoding="utf-8"
    )
    assert cache_policy.main([str(dirty)]) == 1


def test_discover_repos_accepts_a_parent_directory(tmp_path: Path):
    for name in ("one", "two"):
        (tmp_path / name / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    found = {path.name for path in cache_policy._discover_repos([str(tmp_path)])}
    assert found == {"one", "two"}


# ── 退出码语义：error 决定成败，warning 只报告（对齐 rules_lint / workflow_audit） ──
#
# 事故背景：第一版 `main()` 对**任何** finding 都返回 1。于是 hikari 一接上 caller 就常红
# ——它只剩 4 条 warning（`icons/mdi` 窄图标缓存的步骤级 timeout，本就不该删）。
# **一个无法变绿的门禁会停止被阅读，接着 error 级发现也一起停止被阅读。**


def test_warning_only_repo_exits_zero(tmp_path: Path, capsys):
    repo = tmp_path / "narrow-cache"
    (repo / ".github" / "workflows").mkdir(parents=True)
    # 自托管、缓存窄子目录（非 target/、非 cargo home）⇒ 只触 warning
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        """
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: icons/mdi
          key: k
""",
        encoding="utf-8",
    )
    assert cache_policy.main([str(repo)]) == 0
    err = capsys.readouterr().err
    assert "finding(s)" in err and "without-step-timeout" in err  # 仍然报告出来


def test_warning_only_repo_fails_under_strict(tmp_path: Path):
    repo = tmp_path / "narrow-cache-strict"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        """
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: icons/mdi
          key: k
""",
        encoding="utf-8",
    )
    assert cache_policy.main([str(repo), "--strict"]) == 1


def test_error_finding_still_fails_without_strict(tmp_path: Path):
    repo = tmp_path / "big-cache"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(cache_step("target/"), encoding="utf-8")
    assert cache_policy.main([str(repo)]) == 1
