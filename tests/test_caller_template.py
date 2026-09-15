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
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from celestia_devtools.repo.init import WORKFLOW_COMMIT_LINT, _ensure_workflows

#: sha256 of the canonical caller shipped in every celestia-island repository.
CANONICAL_SHA256 = "106a93d0ca2bb1db223ef0fc82b72a53a58ea0ebcbd44bbd2eb8539be8b27342"
CANONICAL_BYTES = 273


def _on(doc):
    """``on`` parses as the boolean ``True`` under YAML 1.1 loaders."""
    return doc.get("on") or doc.get(True)


class TestCanonicalBytes:
    def test_template_hashes_to_the_canonical_file(self):
        assert hashlib.sha256(WORKFLOW_COMMIT_LINT.encode()).hexdigest() == CANONICAL_SHA256

    def test_template_length_matches_the_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT.encode()) == CANONICAL_BYTES

    def test_template_ends_with_a_single_newline(self):
        assert WORKFLOW_COMMIT_LINT.endswith("master\n")
        assert not WORKFLOW_COMMIT_LINT.endswith("\n\n")


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
    def test_job_carries_only_uses(self):
        """A caller job accepts nine keys; anything else makes the whole file invalid."""
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert list(job) == ["uses"]

    def test_job_has_no_timeout_minutes(self):
        """The key is illegal on ``uses:`` jobs — GitHub then runs zero jobs (§8.1)."""
        text = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert "timeout-minutes" not in text

    def test_job_calls_the_shared_reusable_workflow(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["uses"] == (
            "celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master"
        )


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
