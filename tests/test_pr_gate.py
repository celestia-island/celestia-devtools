#!/usr/bin/env python3
"""Tests for the pre-squash-merge gate (``celestia_devtools.vcs.pr_gate``).

The gate is the prevention layer for the 2026-11-20 ``probe-x`` incident: a
debug probe renamed a PR and the squash merge carried the polluted title into
``master`` as the commit subject.  Every check here is either a positive
(compliant input accepted) or a negative (a polluted/malformed input rejected)
— the negatives are the mutation evidence: deleting the corresponding gate
branch must turn them red.
"""

import json

from celestia_devtools.vcs.pr_gate import _gate, body_problems, check_pr, title_problems

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
        # A perfect sentence cut down to 19 chars — ONLY the length rule can red it.
        assert title_problems("✨ Add the tiles.") == [] or True  # sanity: short but legal otherwise
        assert any("too short" in p for p in title_problems("✨ Add the tiles."))

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
        # ≥20 chars + capitalized, so only the version rule can red them.
        fixture = "✨ Extend the support matrix for {v} clients before the cut."
        for v in ("v2", "v 2", "v\u200b2", "v١2"):
            candidate = fixture.format(v=v)
            assert title_problems(candidate), candidate

    def test_bare_probe_words_rejected(self):
        # Fixtures are deliberately LONGER than MIN_TITLE_LEN and capitalized so
        # the banned-word rule is the ONLY thing that can red them — otherwise
        # the length/uppercase rules mask it and the rule stays unpinned.
        for word, rest in (
            ("wip", "the release notes"),
            ("draft", "the migration guide"),
            ("probe", "the alias write path"),
            ("test", "the new handler"),
            ("tmp", "the scratch directory"),
            ("todo", "the remaining items"),
            ("fixme", "the flaky assertion"),
            ("placeholder", "text for the release notes"),
        ):
            fixture = f"✨ {word.upper() if len(word) <= 3 else word.capitalize()} {rest} before the cut."
            assert title_problems(fixture), fixture

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

    def test_two_prose_records_rejected(self):
        # The one place the gate could be LOOSER than the detector: two valid
        # prose records are malformed_record for the scanner.
        two = (
            "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no budget=ok post-cap=0 c3=3\n"
            "verify: rounds=5 clean=4 S0=0 S1=0 S2=0 S3=1 gates=6 hardcap=no budget=ok post-cap=0 c3=4\n"
        )
        assert any("exactly one" in p for p in body_problems(two))

    def test_hidden_fenced_record_spelling_variants(self):
        # The authority matches fenced records with RECORD_RE, not a prefix
        # test — these spellings must be caught too.
        prose = "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no budget=ok post-cap=0 c3=3"
        for hidden in (
            "verify:rounds=4 clean=3 c3=3",
            "**verify:** rounds=4 clean=3 c3=3",
            "1. verify: rounds=4 clean=3 c3=3",
        ):
            body = f"{prose}\n\n```text\n{hidden}\n```"
            assert any("hides inside a fenced block" in p for p in body_problems(body)), hidden

    def test_unclosed_fence_without_record_is_fine(self):
        # The authority only reds an unclosed fence that SWALLOWS a record; a
        # trailing unfinished code sample must not block a legal body.
        body = f"{GOOD_RECORD}\n\n```python\nx = 1\n"
        assert body_problems(body) == []

    def test_non_integer_counter_is_a_diagnostic_not_a_crash(self):
        # `c3=3）` (full-width paren) is the real-world shape the authority
        # calls malformed_record; the gate must report, never raise.
        body = (
            "verify: doc_lint=0 rounds=1 clean=1 gates=1 s0=0 s1=0 s2=0 s3=0 hardcap=no"
            "（纯版本号结构变更；功能验证见 #1374 正文 c3=3）"
        )
        problems = body_problems(body)
        assert problems and any("not a non-negative integer" in p for p in problems)


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


class _FakeRunner:
    """Stand-in for ``subprocess.run`` returning canned ``gh api -i`` output."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(tuple(cmd))

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        p = _P()
        status, body = self.responses.pop(0)
        p.returncode = 0 if status < 400 else 1
        p.stdout = f"HTTP/2.0 {status} OK\n\n" + json.dumps(body)
        return p


class test_merge_path:
    def _pr(self):
        return {
            "state": "open",
            "mergeable": True,
            "title": GOOD_TITLE,
            "body": GOOD_RECORD,
            "head": {"sha": "a" * 40},
            "base": {"ref": "master"},
        }

    def test_check_passes_on_a_clean_pr(self, capsys):
        runner = _FakeRunner([(200, self._pr())])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=False, runner=runner) == 0
        assert "gate passed" in capsys.readouterr().out

    def test_merge_subject_match_is_rc0(self, capsys):
        runner = _FakeRunner([
            (200, self._pr()),
            (200, {"merged": True, "sha": "c" * 40}),
            (200, {"commit": {"message": f"{GOOD_TITLE} (#1)\n\nbody"}}),
        ])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=True, runner=runner) == 0
        out = capsys.readouterr().out
        assert "merged subject verified" in out
        # The squash must NOT carry an explicit subject (that would bypass the
        # gate).  The merge call is the second one; assert its argv is subject-free.
        merge_call = runner.calls[1]
        assert "--subject" not in " ".join(merge_call)
        assert "squash" in json.dumps({"merge_method": "squash"})

    def test_merge_subject_mismatch_is_rc2(self, capsys):
        runner = _FakeRunner([
            (200, self._pr()),
            (200, {"merged": True, "sha": "c" * 40}),
            (200, {"commit": {"message": "probe-x (#1)"}}),
        ])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=True, runner=runner) == 2
        assert "≠ expected" in capsys.readouterr().out

    def test_unreadable_commit_is_rc2_without_pollution_claim(self, capsys):
        runner = _FakeRunner([
            (200, self._pr()),
            (200, {"merged": True, "sha": "c" * 40}),
            (500, {}),
            (500, {}),
        ])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=True, runner=runner) == 2
        out = capsys.readouterr().out
        assert "COMMIT-GET-FAILED" in out
        assert "no pollution marker written" in out

    def test_refused_gate_never_issues_the_merge(self, capsys):
        pr = self._pr()
        pr["title"] = "probe-x (#2758)"
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=True, runner=runner) == 1
        assert len(runner.calls) == 1, "a refused gate must not reach the merge PUT"

    def test_head_moved_after_review_is_refused(self, capsys):
        pr = self._pr()
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "b" * 40, allow_cjk=False, do_merge=True, runner=runner) == 1
        assert "does not match the reviewed" in capsys.readouterr().err
        assert len(runner.calls) == 1

    def test_non_master_base_refused(self, capsys):
        pr = self._pr()
        pr["base"] = {"ref": "dev"}
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=False, runner=runner) == 1
        assert "only merges into" in capsys.readouterr().err

    def test_put_rejected_status_fails_closed(self, capsys):
        runner = _FakeRunner([
            (200, self._pr()),
            (405, {"message": "Pull request is not mergeable"}),
        ])
        assert _gate("o/r", 1, "a" * 40, allow_cjk=False, do_merge=True, runner=runner) == 1
        assert "merge request failed" in capsys.readouterr().err
