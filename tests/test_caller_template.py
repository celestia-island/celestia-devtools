"""Pin the generated commit-lint caller to the org-wide canonical bytes.

*Why this exists:* on 2026-09-15 the org unified all 42 caller files byte-for-byte
(273 B, sha256 ``106a93d0…``). The generator here is the only other producer of that
file — ``celestia-devtools init --with-workflows`` (and ``--force``, which rewrites an
existing file unconditionally). Before this guard the template still emitted the older
variant without ``ready_for_review``, so running ``init --force --with-workflows`` over
a unified repository silently reverted it, and a draft PR flipped to "ready for review"
produced no lint run at all — the §8.3.6 deadlock: the required check never appears and
the merge is refused. A template nobody compares against the fleet is a regression vector,
so these tests fail the moment the constant drifts.

*2026-09-26 lane migration:* the canonical bytes became the lane-carrying form
(1170 B, sha256 ``620d511f…``, byte-identical to the shittim-chest production caller
that already serves the check through ``vars.LINT_RUNNER``/``vars.LINT_LANE``). The
pre-lane 273 B bytes live on as ``WORKFLOW_COMMIT_LINT_LEGACY`` — the audit reports
them as a *warning* so the fleet can regenerate repo by repo instead of going 38-red
in one wave. These tests pin both constants, and pin that they stay distinct.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from celestia_devtools.repo.init import (
    WORKFLOW_CI_CACHE,
    WORKFLOW_COMMIT_LINT,
    WORKFLOW_COMMIT_LINT_LEGACY,
    WORKFLOW_PR_TITLE_CHECK,
    _ensure_workflows,
)

#: sha256 of the canonical (lane-carrying) caller shipped in celestia-island repositories.
CANONICAL_SHA256 = "620d511f256c4b73522b75beecc33509aa1401d2ca808efacde866b03126f75f"
CANONICAL_BYTES = 1170

#: The pre-lane canonical (2026-09-15 → 2026-09-26): audit-only migration marker.
LEGACY_SHA256 = "106a93d0ca2bb1db223ef0fc82b72a53a58ea0ebcbd44bbd2eb8539be8b27342"
LEGACY_BYTES = 273


def _on(doc):
    """``on`` parses as the boolean ``True`` under YAML 1.1 loaders."""
    return doc.get("on") or doc.get(True)


class TestCanonicalBytes:
    def test_template_hashes_to_the_canonical_file(self):
        assert hashlib.sha256(WORKFLOW_COMMIT_LINT.encode()).hexdigest() == CANONICAL_SHA256

    def test_template_length_matches_the_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT.encode()) == CANONICAL_BYTES

    def test_template_ends_with_a_single_newline(self):
        assert WORKFLOW_COMMIT_LINT.endswith("pr: ${{ inputs.pr }}\n")
        assert not WORKFLOW_COMMIT_LINT.endswith("\n\n")


class TestLegacyBytes:
    """The pre-lane template: the audit's migration marker, frozen on purpose.

    ``_canonical_caller_findings`` reports exactly these bytes as a warning, so a
    stray edit to the legacy constant would silently re-classify the un-migrated
    fleet as hand-edited (error) — or worse, make legacy equal canonical and kill
    the migration signal entirely.
    """

    def test_legacy_hashes_to_the_historical_file(self):
        assert (
            hashlib.sha256(WORKFLOW_COMMIT_LINT_LEGACY.encode()).hexdigest()
            == LEGACY_SHA256
        )

    def test_legacy_length_is_the_pre_lane_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT_LEGACY.encode()) == LEGACY_BYTES

    def test_legacy_is_distinct_from_canonical(self):
        assert WORKFLOW_COMMIT_LINT != WORKFLOW_COMMIT_LINT_LEGACY

    def test_canonical_carries_the_lane_passthrough(self):
        """The whole point of the migration: canonical must express the lane vars."""
        assert "vars.LINT_RUNNER" in WORKFLOW_COMMIT_LINT
        assert "vars.LINT_LANE" in WORKFLOW_COMMIT_LINT
        assert "vars.LINT_RUNNER" not in WORKFLOW_COMMIT_LINT_LEGACY


class TestTriggerShape:
    def test_every_lifecycle_event_is_covered(self):
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert _on(doc)["pull_request"]["types"] == [
            "opened",
            "edited",
            "reopened",
            "ready_for_review",
            "synchronize",
        ]

    def test_ready_for_review_is_present(self):
        """Its absence is the §8.3.6 deadlock: a draft PR marked ready never lints."""
        types = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"]
        assert "ready_for_review" in types

    def test_synchronize_is_present(self):
        """Its absence means a re-pushed head never re-runs the required check."""
        types = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"]
        assert "synchronize" in types

    def test_merge_group_is_declared(self):
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert _on(doc)["merge_group"]["types"] == ["checks_requested"]

    def test_no_push_trigger(self):
        """Lint is a required check, not a master-post-processing step."""
        assert "push" not in _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))

    def test_no_workflow_level_concurrency(self):
        assert "concurrency" not in yaml.safe_load(WORKFLOW_COMMIT_LINT)


class TestCallerJobShape:
    def test_job_carries_only_uses_and_with(self):
        """A caller job accepts nine legal keys; this one uses ``uses`` + ``with``.

        The ``with`` block is the lane passthrough (2026-09-26): anything else —
        ``timeout-minits`` in particular — makes the whole file invalid.
        """
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert list(job) == ["uses", "with"]

    def test_job_has_no_timeout_minutes(self):
        """The key is illegal on ``uses:`` jobs — GitHub then runs zero jobs (§8.1)."""
        text = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert "timeout-minutes" not in text

    def test_job_calls_the_shared_reusable_workflow(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["uses"] == (
            "celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master"
        )


class TestLanePassthrough:
    """The 2026-09-26 canonical additions: dispatch fallback + lane variables.

    Without these, moving the single required check off the (broken) hosted lane
    required editing every repository's tree — the W-2 "one lane of rescue" defect.
    """

    def test_workflow_dispatch_declares_required_pr_input(self):
        dispatch = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["workflow_dispatch"]
        assert dispatch["inputs"]["pr"]["required"] is True

    def test_runner_defaults_to_hosted_and_reads_the_repo_variable(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["runner"] == "${{ vars.LINT_RUNNER || '[\"ubuntu-latest\"]' }}"

    def test_lane_defaults_to_hosted_and_reads_the_repo_variable(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["lane"] == "${{ vars.LINT_LANE || 'hosted' }}"

    def test_pr_passthrough_feeds_the_dispatch_fallback(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["pr"] == "${{ inputs.pr }}"


class TestGeneratedFile:
    def test_writer_emits_the_canonical_bytes(self, tmp_path):
        _ensure_workflows(tmp_path)
        written = (tmp_path / ".github" / "workflows" / "commit-msg-lint.yml").read_bytes()
        assert written == WORKFLOW_COMMIT_LINT.encode()
        assert hashlib.sha256(written).hexdigest() == CANONICAL_SHA256

    def test_force_rewrites_to_the_canonical_bytes(self, tmp_path):
        """``--force`` overwrites an existing file; that rewrite must land on canonical."""
        target = tmp_path / ".github" / "workflows" / "commit-msg-lint.yml"
        target.parent.mkdir(parents=True)
        target.write_text("name: stale\n", encoding="utf-8")
        _ensure_workflows(tmp_path, force=True)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == CANONICAL_SHA256

    def test_existing_file_is_left_alone_without_force(self, tmp_path):
        target = tmp_path / ".github" / "workflows" / "commit-msg-lint.yml"
        target.parent.mkdir(parents=True)
        target.write_text("name: mine\n", encoding="utf-8")
        _ensure_workflows(tmp_path)
        assert target.read_text(encoding="utf-8") == "name: mine\n"


#: The callee this repository hosts: the reusable workflow the template's ``uses:`` resolves to.
REPO_ROOT = Path(__file__).resolve().parents[1]
CALLEE_PATH = REPO_ROOT / ".github" / "workflows" / "commit-msg-lint.yml"


def _callee():
    return yaml.safe_load(CALLEE_PATH.read_text(encoding="utf-8"))


class TestCalleeMatchesTheTemplate:
    """Two files must agree on the trigger: the caller (copied into 42 repositories) and the
    callee this repository hosts.

    Nothing compared them. The caller's template could gain an event — or the callee could
    drop one — and the caller's jobs would simply never start, with the caller file still
    byte-identical to the canonical bytes and every other guard green. Only the trigger has
    to match: the callee additionally declares ``workflow_call`` and carries ``runs-on`` /
    ``steps`` the caller must never have.
    """

    def test_callee_file_is_present(self):
        assert CALLEE_PATH.is_file(), f"missing callee workflow: {CALLEE_PATH}"

    def test_pull_request_types_match_the_template(self):
        assert (
            _on(_callee())["pull_request"]["types"]
            == _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"]
        )

    def test_merge_group_types_match_the_template(self):
        assert (
            _on(_callee())["merge_group"]["types"]
            == _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["merge_group"]["types"]
        )

    def test_callee_declares_every_event_the_caller_waits_on(self):
        caller_events = set(_on(yaml.safe_load(WORKFLOW_COMMIT_LINT)))
        assert caller_events <= set(_on(_callee()))


class TestCachePolicyCallerTemplate:
    """The second generated caller: same discipline as the commit-lint one.

    It is written from the same generator (`init --with-workflows`), so it carries the same
    regression risk -- a template nobody compares against the fleet silently reverts the
    wiring on the next `--force` run. These pin the shape the gate needs, without pinning a
    hash: the file is new, so there is no fleet of byte-identical copies to match yet.
    """

    def test_uses_the_shared_workflow_at_master(self):
        doc = yaml.safe_load(WORKFLOW_CI_CACHE)
        assert doc["jobs"]["cache-policy"]["uses"] == (
            "celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@master"
        )

    def test_synchronize_is_present(self):
        """Without it a re-pushed head never re-runs the check: the §8.3.6 deadlock."""
        types = _on(yaml.safe_load(WORKFLOW_CI_CACHE))["pull_request"]["types"]
        assert "synchronize" in types

    def test_ready_for_review_is_present(self):
        types = _on(yaml.safe_load(WORKFLOW_CI_CACHE))["pull_request"]["types"]
        assert "ready_for_review" in types

    def test_merge_group_is_declared(self):
        doc = yaml.safe_load(WORKFLOW_CI_CACHE)
        assert _on(doc)["merge_group"]["types"] == ["checks_requested"]

    def test_no_push_trigger(self):
        assert "push" not in _on(yaml.safe_load(WORKFLOW_CI_CACHE))

    def test_caller_job_carries_only_uses(self):
        """A caller job accepts nine keys; anything else makes the whole file invalid.

        `timeout-minutes` is the specific trap here: it is not a caller key, and adding it
        makes GitHub reject the file without creating a job at all.
        """
        job = yaml.safe_load(WORKFLOW_CI_CACHE)["jobs"]["cache-policy"]
        assert set(job) == {"uses"}

    def test_generator_writes_both_callers(self, tmp_path: Path):
        _ensure_workflows(tmp_path)
        written = sorted(p.name for p in (tmp_path / ".github" / "workflows").iterdir())
        assert written == ["ci-cache.yml", "commit-msg-lint.yml", "pr-title-check.yml"]
        assert (tmp_path / ".github" / "workflows" / "ci-cache.yml").read_text(
            encoding="utf-8"
        ) == WORKFLOW_CI_CACHE

    def test_generator_does_not_overwrite_without_force(self, tmp_path: Path):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci-cache.yml").write_text("hand-edited\n", encoding="utf-8")
        _ensure_workflows(tmp_path)
        assert (workflows / "ci-cache.yml").read_text(encoding="utf-8") == "hand-edited\n"


class TestPrTitleCallerTemplate:
    """The third generated caller, added 2026-09-20.

    It had no template before that, which is why 21 repositories carried a hand-written copy
    whose `types` omitted `synchronize`: a push to an existing PR never re-ran the title
    check, and because it is a required check the merge then waited for a result that was
    never produced.
    """

    def test_uses_the_shared_workflow_at_master(self):
        doc = yaml.safe_load(WORKFLOW_PR_TITLE_CHECK)
        assert doc["jobs"]["pr-title-check"]["uses"] == (
            "celestia-island/celestia-devtools/.github/workflows/pr-title-check.yml@master"
        )

    def test_synchronize_is_present(self):
        types = _on(yaml.safe_load(WORKFLOW_PR_TITLE_CHECK))["pull_request"]["types"]
        assert "synchronize" in types

    def test_edited_is_present(self):
        """Editing the title must re-run the check that validates it."""
        types = _on(yaml.safe_load(WORKFLOW_PR_TITLE_CHECK))["pull_request"]["types"]
        assert "edited" in types

    def test_caller_job_carries_only_uses(self):
        job = yaml.safe_load(WORKFLOW_PR_TITLE_CHECK)["jobs"]["pr-title-check"]
        assert set(job) == {"uses"}

    def test_callee_also_re_runs_on_synchronize(self):
        """The reusable workflow's own trigger list must match, or the caller is useless."""
        callee = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / ".github/workflows/pr-title-check.yml")
            .read_text(encoding="utf-8")
        )
        types = _on(callee)["pull_request"]["types"]
        assert "synchronize" in types

    def test_generator_writes_all_three_callers(self, tmp_path: Path):
        _ensure_workflows(tmp_path)
        written = sorted(p.name for p in (tmp_path / ".github" / "workflows").iterdir())
        assert written == ["ci-cache.yml", "commit-msg-lint.yml", "pr-title-check.yml"]
