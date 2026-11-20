#!/usr/bin/env python3
"""Tests for the pre-squash-merge gate (``celestia_devtools.vcs.pr_gate``).

The gate is the prevention layer for the 2026-11-20 ``probe-x`` incident: a
debug probe renamed a PR and the squash merge carried the polluted title into
``master`` as the commit subject.  Every check here is either a positive
(compliant input accepted) or a negative (a polluted/malformed input rejected)
— the negatives are the mutation evidence: deleting the corresponding gate
branch must turn them red.
"""

from celestia_devtools.vcs.pr_gate import body_problems, check_pr, title_problems

GOOD_TITLE = "✨ Make the workspace alias a required localized name with a preferred report language."
GOOD_RECORD = "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no budget=ok post-cap=0 c3=3"
GOOD_BODY = f"…\n\n{GOOD_RECORD}\n"
STRUCTURAL_BODY = "verify: doc_lint=0 rounds=1 clean=1 gates=1 s0=0 s1=0 s2=0 s3=0 hardcap=no"


class test_title:
    def test_compliant_accepted(self):
        assert title_problems(GOOD_TITLE) == []

    def test_pin_org_extension_accepted(self):
        assert title_problems("📌 Pin @celestia-island/hikari 0.55.435 with a same-day release-age exemption.") == []

    def test_too_short_rejected(self):
        assert title_problems("t")
        assert title_problems("probe-x")

    def test_incident_title_rejected(self):
        # The real incident face: squash carried "probe-x (#2758)".
        assert title_problems("probe-x (#2758)")

    def test_no_emoji_rejected(self):
        assert title_problems("Fix the thing.")

    def test_lowercase_rejected(self):
        assert title_problems("✨ fix the thing.")

    def test_no_period_rejected(self):
        assert title_problems("✨ Fix the thing")

    def test_colon_prefix_rejected(self):
        assert title_problems("✨ Fix: the thing.")

    def test_topic_colon_rejected(self):
        assert title_problems("✨ Phase 6: Theme bridge.")

    def test_cjk_rejected(self):
        assert title_problems("✨ 中文标题")

    def test_in_title_pr_ref_rejected(self):
        assert title_problems("✨ Add the thing (#123).")
        assert title_problems("✨ Add the thing (# 123).")
        assert title_problems("✨ Add the thing （#123）.")
        assert title_problems("✨ Add the thing #123.")

    def test_version_word_rejected(self):
        assert title_problems("✨ Add v2 support.")
        assert title_problems("✨ Add v 2 support.")
        assert title_problems("✨ Add v\u200b2 support.")
        assert title_problems("✨ Add v١2 support.")

    def test_bare_probe_words_rejected(self):
        for word in ("wip", "draft", "probe", "test", "tmp", "todo", "fixme", "placeholder"):
            assert title_problems(f"✨ {word} the thing.")

    def test_no_false_positive_on_substrings(self):
        # Tokenised matching: "testing"/"prober"/"testability" contain banned
        # literals but are real words — must pass (R-review-confirmed edge).
        assert not title_problems("✨ Improve testability of the scheduler.")
        assert not title_problems("🐛 Fix the prober startup race.")


class test_body:
    def test_compliant_accepted(self):
        assert body_problems(GOOD_RECORD) == []

    def test_whole_line_backtick_accepted(self):
        # verify_account L348: the whole-line-backtick form parses like prose.
        assert body_problems(f"`{GOOD_RECORD}`") == []

    def test_list_item_accepted(self):
        # A plain list item is a record (verify_account accepts it).
        assert body_problems(f"- {GOOD_RECORD}") == []

    def test_structural_lane_accepted(self):
        assert body_problems(STRUCTURAL_BODY) == []

    def test_c4_stronger_record_accepted(self):
        body = (
            "verify: rounds=5 clean=4 S0=0 S1=0 S2=0 S3=0 gates=2 hardcap=no budget=ok post-cap=0 c3=4"
        )
        assert body_problems(body) == []

    def test_missing_fields_rejected(self):
        # The placeholder template shape — malformed-record family.
        assert body_problems("verify: rounds=4 clean=3 c3=3")

    def test_unknown_field_rejected(self):
        assert body_problems(
            "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=no gatez=1 c3=3"
        )

    def test_duplicate_field_rejected(self):
        assert body_problems(
            "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=no c3=3 c3=3"
        )

    def test_clean_gt_rounds_rejected(self):
        assert body_problems(
            "verify: rounds=4 clean=9 S0=0 S1=0 S2=0 S3=0 hardcap=no c3=3"
        )

    def test_hardcap_enum_rejected(self):
        assert body_problems(
            "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=maybe c3=3"
        )

    def test_indented_record_rejected(self):
        # The RECORD LINE itself is indented (a template shape).
        assert body_problems(f"    {GOOD_RECORD}")

    def test_tab_indented_record_rejected(self):
        assert body_problems(f"\t{GOOD_RECORD}")

    def test_quote_record_rejected(self):
        assert body_problems(f"> {GOOD_RECORD}")

    def test_tilde_fenced_record_rejected(self):
        assert body_problems(f"~~~\n{GOOD_RECORD}\n~~~")

    def test_backtick_fenced_record_rejected(self):
        assert body_problems(f"```\n{GOOD_RECORD}\n```")

    def test_unclosed_fence_rejected(self):
        assert body_problems(f"```\n{GOOD_RECORD}")

    def test_fenced_hidden_second_record_rejected(self):
        # Prose record + a fenced "kept for provenance" record = ambiguous set.
        body = (
            f"{GOOD_RECORD}\n\n```text\n"
            "verify: rounds=3 clean=2 S0=0 S1=0 S2=0 S3=0 gates=2 hardcap=no budget=ok post-cap=0 c3=2\n"
            "```"
        )
        assert body_problems(body)

    def test_no_record_rejected(self):
        assert body_problems("no verify record here")


class test_check_pr:
    def _pr(self, **overrides):
        payload = {
            "state": "open",
            "mergeable": True,
            "title": GOOD_TITLE,
            "body": GOOD_BODY,
            "head": {"sha": "a" * 40},
        }
        payload.update(overrides)
        return payload

    def test_clean_pr_passes(self):
        assert check_pr(self._pr(), expect_head="a" * 40) == []

    def test_head_mismatch_rejected(self):
        problems = check_pr(self._pr(head={"sha": "b" * 40}), expect_head="a" * 40)
        assert any("does not match the reviewed" in p for p in problems)

    def test_short_head_prefix_accepted(self):
        assert check_pr(self._pr(), expect_head="a" * 12) == []

    def test_closed_rejected(self):
        problems = check_pr(self._pr(state="closed"), expect_head="a" * 40)
        assert any("not open" in p for p in problems)

    def test_mergeable_unknown_hint(self):
        problems = check_pr(self._pr(mergeable=None), expect_head="a" * 40)
        assert any("retry" in p for p in problems)

    def test_conflicted_rejected(self):
        problems = check_pr(self._pr(mergeable=False), expect_head="a" * 40)
        assert any("conflicts" in p for p in problems)

    def test_empty_title_rejected(self):
        problems = check_pr(self._pr(title=""), expect_head="a" * 40)
        assert problems

    def test_empty_body_rejected(self):
        problems = check_pr(self._pr(body=""), expect_head="a" * 40)
        assert any("verify" in p for p in problems)
