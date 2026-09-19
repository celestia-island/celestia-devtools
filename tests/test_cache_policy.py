"""Tests for ci/cache_policy.py — the actions/cache cost gate for self-hosted runners.

Every rule is exercised against the *shape that actually exists* in the org's
repositories (see the module docstring for the measurements), and each rule also has a
negative case: the checks are worthless if they fire on legitimate caches, because a
noisy gate gets waived and then the real finding is waived with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

TOOLS_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(TOOLS_SRC))

from celestia_devtools.ci import cache_policy  # noqa: E402


def rules(text: str) -> list:
    return [finding["rule"] for finding in cache_policy.scan_text(text, rel="r/w.yml")]


def lines_for(text: str, rule: str) -> list:
    return [
        finding["line"]
        for finding in cache_policy.scan_text(text, rel="r/w.yml")
        if finding["rule"] == rule
    ]


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
    assert rules(text) == ["self-hosted-build-dir-cache"]
    assert lines_for(text, "self-hosted-build-dir-cache") == [8]


def test_flags_nested_target_and_scalar_path():
    text = """
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: "*/target/"
          key: k
"""
    assert rules(text) == ["self-hosted-build-dir-cache"]


def test_hosted_target_cache_is_not_flagged():
    """GitHub-hosted runners are stateless, so caching target/ there is a real choice."""
    text = """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v6
        with:
          path: |
            target/
          key: k
"""
    assert rules(text) == []


def test_caching_a_narrow_subdirectory_is_not_flagged():
    text = """
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: target/debug/build
          key: k
"""
    assert rules(text) == []


# ── rule: self-hosted-cargo-home-cache ───────────────────────────────────────


@pytest.mark.parametrize("path", ["~/.cargo", "$HOME/.cargo", "${HOME}/.cargo"])
def test_flags_persistent_cargo_home_on_self_hosted(path):
    text = f"""
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: {path}
          key: k
"""
    assert "self-hosted-cargo-home-cache" in rules(text)


def test_cargo_registry_subpath_on_hosted_is_not_flagged():
    text = """
jobs:
  build:
    runs-on: [self-hosted, linux]
    steps:
      - uses: actions/cache@v6
        with:
          path: ~/.cargo/registry
          key: k
"""
    assert rules(text) == []


# ── rule: undefined-env-cache-path ───────────────────────────────────────────


def test_flags_env_ref_defined_nowhere():
    text = """
jobs:
  test:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: |
            ${{ env.CARGO_HOME }}/registry
          key: k
"""
    assert rules(text) == ["undefined-env-cache-path"]


def test_env_ref_defined_at_workflow_level_is_not_flagged():
    text = """
env:
  CARGO_HOME: /mnt/ci-cache/cargo
jobs:
  test:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: |
            ${{ env.CARGO_HOME }}/registry
          key: k
"""
    assert rules(text) == []


def test_env_ref_defined_at_job_level_is_not_flagged():
    text = """
jobs:
  test:
    runs-on: self-hosted
    env:
      CARGO_HOME: /mnt/ci-cache/cargo
    steps:
      - uses: actions/cache@v6
        with:
          path: ${{ env.CARGO_HOME }}/git
          key: k
"""
    assert rules(text) == []


# ── negative cases shared by the undefined-env rule ──────────────────────────


def test_literal_absolute_path_is_not_flagged():
    """A literal absolute path is a legitimate choice, not a collapsed variable."""
    text = """
jobs:
  test:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: /opt/toolchain
          key: k
"""
    assert rules(text) == []


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
    (workflows / "ci.yml").write_text(
        """
jobs:
  a:
    runs-on: self-hosted
    steps:
      - uses: actions/cache@v6
        with:
          path: target/
          key: k
""",
        encoding="utf-8",
    )
    (workflows / "notes.md").write_text("not a workflow\n", encoding="utf-8")

    findings = cache_policy.collect([repo])
    assert len(findings) == 1
    assert findings[0]["path"] == "repo/.github/workflows/ci.yml"
    assert findings[0]["detail"].startswith("job 'a'")


def test_main_exits_nonzero_on_findings_and_zero_when_clean(tmp_path: Path, capsys):
    clean = tmp_path / "clean"
    (clean / ".github" / "workflows").mkdir(parents=True)
    (clean / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/cache@v6\n        with:\n          path: target/\n          key: k\n",
        encoding="utf-8",
    )
    assert cache_policy.main([str(clean)]) == 0
    assert "no findings" in capsys.readouterr().err

    dirty = tmp_path / "dirty"
    (dirty / ".github" / "workflows").mkdir(parents=True)
    (dirty / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  a:\n    runs-on: self-hosted\n    steps:\n"
        "      - uses: actions/cache@v6\n        with:\n          path: target/\n          key: k\n",
        encoding="utf-8",
    )
    assert cache_policy.main([str(dirty)]) == 1


def test_discover_repos_accepts_a_parent_directory(tmp_path: Path):
    for name in ("one", "two"):
        (tmp_path / name / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    found = {path.name for path in cache_policy._discover_repos([str(tmp_path)])}
    assert found == {"one", "two"}
