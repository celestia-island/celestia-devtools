#!/usr/bin/env python3
"""Tests for the pre-squash-merge gate (``celestia_devtools.vcs.pr_gate``).

The gate is the prevention layer for the 2026-11-20 ``probe-x`` incident: a
debug probe renamed a PR and the squash merge carried the polluted title into
``master`` as the commit subject.  Every check here is either a positive
(compliant input accepted) or a negative (a polluted/malformed input rejected)
— the negatives are the mutation evidence: deleting the corresponding gate
branch must turn them red.
"""

import inspect
import json
import re
import pytest
from celestia_devtools.vcs.pr_gate import _gate, body_problems, check_pr, title_problems
from celestia_devtools.vcs import pr_gate as pr_gate_module
from celestia_devtools.vcs.pr_gate import comment_waiver as pr_gate_comment_waiver

GOOD_TITLE = "✨ Make the workspace alias a required localized name with a preferred report language."
GOOD_RECORD = "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no budget=ok post-cap=0 c3=3"
#: A body that is clean for EVERY gate in the chain: real prose (so the body
#: gate has something to scan and finds nothing unsourced) plus a clean record.
#: The ellipsis form also passes the body gate (loud-skip lane), so this shape
#: is a fixture choice, not a control.
#: Prose only. A body that also carries the verify record is `GOOD_BODY +
#: GOOD_RECORD`, so a caller that supplies a DIFFERENT record cannot end up
#: with two (the record check refuses that) — the reason this is split.
GOOD_BODY = "Add the thing and update the docs.\n"
STRUCTURAL_BODY = "verify: doc_lint=0 rounds=1 clean=1 gates=1 s0=0 s1=0 s2=0 s3=0 hardcap=no"

class test_title:
    def test_compliant_accepted(self):
        assert title_problems(GOOD_TITLE) == []

    def test_pin_org_extension_accepted(self):
        assert title_problems("📌 Pin @celestia-island/hikari 0.55.435 with a same-day release-age exemption.") == []

    def test_too_short_rejected(self):
        # A perfect sentence cut down to 19 chars — ONLY the length rule can red it.
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
            candidate = fixture.format(v = v)
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
            ("placeholder", "text for the release notes")
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
        body = "verify: rounds=5 clean=4 S0=0 S1=0 S2=0 S3=0 gates=2 hardcap=no budget=ok post-cap=0 c3=4"
        assert body_problems(body) == []

    def test_missing_fields_rejected(self):
        # The placeholder template shape — malformed-record family.
        assert body_problems("verify: rounds=4 clean=3 c3=3")

    def test_unknown_field_rejected(self):
        assert body_problems("verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=no gatez=1 c3=3")

    def test_duplicate_field_rejected(self):
        assert body_problems("verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=no c3=3 c3=3")

    def test_clean_gt_rounds_rejected(self):
        assert body_problems("verify: rounds=4 clean=9 S0=0 S1=0 S2=0 S3=0 hardcap=no c3=3")

    def test_hardcap_enum_rejected(self):
        assert body_problems("verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=0 hardcap=maybe c3=3")

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
        # Pin the DIAGNOSTIC, not just truthiness: the unclosed branch and the
        # hidden-record branch are one decision with two messages, and only the
        # text distinguishes them.
        problems = body_problems(f"```\n{GOOD_RECORD}")
        assert any("never closes" in p for p in problems), problems
        assert not any("hides inside" in p for p in problems), problems

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

        for hidden in ("verify:rounds=4 clean=3 c3=3", "**verify:** rounds=4 clean=3 c3=3", "1. verify: rounds=4 clean=3 c3=3"):
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
        payload = {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}
        payload.update(overrides)
        return payload

    def test_clean_pr_passes(self):
        assert check_pr(self._pr(), expect_head = "a" * 40) == []

    def test_head_mismatch_rejected(self):
        problems = check_pr(self._pr(head = {"sha": "b" * 40}), expect_head = "a" * 40)
        assert any("does not match the reviewed" in p for p in problems)

    def test_short_head_prefix_accepted(self):
        assert check_pr(self._pr(), expect_head = "a" * 12) == []

    def test_closed_rejected(self):
        problems = check_pr(self._pr(state = "closed"), expect_head = "a" * 40)
        assert any("not open" in p for p in problems)

    def test_mergeable_unknown_hint(self):
        problems = check_pr(self._pr(mergeable = None), expect_head = "a" * 40)
        assert any("retry" in p for p in problems)

    def test_conflicted_rejected(self):
        problems = check_pr(self._pr(mergeable = False), expect_head = "a" * 40)
        assert any("conflicts" in p for p in problems)

    def test_empty_title_rejected(self):
        problems = check_pr(self._pr(title = ""), expect_head = "a" * 40)
        assert problems

    def test_empty_body_rejected(self):
        problems = check_pr(self._pr(body = ""), expect_head = "a" * 40)
        assert any("verify" in p for p in problems)

#: What a ``gh api`` diff read answers for a PR with no test-file changes:
#: the comment gate must find "nothing to judge" and not skip silently.
#: A diff with no test-file changes: the comment gate must SKIP it out loud.
NEUTRAL_DIFF = "diff --git a/src/neutral.ts b/src/neutral.ts\n--- a/src/neutral.ts\n+++ b/src/neutral.ts\n@@ -1,0 +1,1 @@\n+// nothing behaviour-claiming here\n"

class _FakeRunner:
    """Stand-in for ``subprocess.run`` returning canned ``gh api -i`` output.

    The comment gate reads the PR diff through the same ``gh_api``, so a canned
    response list alone would be shifted by that read. A diff request (the
    ``Accept: application/vnd.github.diff`` header) is answered with
    :data:`NEUTRAL_DIFF` and consumes NO canned response unless one is queued
    for it — the merge-path tests care about the JSON calls, and this keeps
    their response lists unchanged.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, cmd, **kwargs):
        argv = [str(part) for part in cmd]
        accepted = ""

        for index, part in enumerate(argv):
            if (part == "-H") and ((index + 1) < len(argv)):
                accepted = argv[index + 1]

        if "vnd.github.diff" in accepted:
            self.calls.append(tuple(argv))

            class _diff_response:
                returncode = 0
                stdout = "HTTP/2.0 200 OK\n\n" + NEUTRAL_DIFF
                stderr = ""

            return _diff_response()

        # The artifact gate asks `/pulls/<n>/files`; like the diff read it must
        # not consume a queued payload, or every existing response list shifts.
        if "/files" in " ".join(argv):
            self.calls.append(tuple(argv))

            class _files_response:
                returncode = 0
                stdout = 'HTTP/2.0 200 OK\n\n[{"filename": "docs/readme.md"}, {"filename": "packages/wasm/qr/wit/qr.wit"}]'
                stderr = ""

            return _files_response()

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
        return {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}

    def test_check_passes_on_a_clean_pr(self, capsys):
        runner = _FakeRunner([(200, self._pr())])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0
        assert "gate passed" in capsys.readouterr().out

    def test_merge_subject_match_is_rc0(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"commit": {"message": f"{GOOD_TITLE} (#1)\n\nbody"}})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 0
        out = capsys.readouterr().out
        assert "merged subject verified" in out
        # The squash must NOT carry an explicit subject (that would bypass the
        # gate).  The merge call is the second one; assert its argv is subject-free.
        merge_call = runner.calls[1]
        assert "--subject" not in " ".join(merge_call)

    def test_merge_subject_mismatch_is_rc2(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"commit": {"message": "probe-x (#1)"}})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        assert "≠ expected" in capsys.readouterr().out

    def test_unreadable_commit_is_rc2_without_pollution_claim(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (500, {}), (500, {})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        out = capsys.readouterr().out
        assert "COMMIT-GET-FAILED" in out
        assert "no pollution marker written" in out

    def test_refused_gate_never_issues_the_merge(self, capsys):
        pr = self._pr()
        pr["title"] = "probe-x (#2758)"
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 1
        assert _writes(runner) == [], "a refused gate must not reach the merge PUT"

    def test_head_moved_after_review_is_refused(self, capsys):
        pr = self._pr()
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "b" * 40, allow_cjk = False, do_merge = True, runner = runner) == 1
        assert "does not match the reviewed" in capsys.readouterr().err
        assert _writes(runner) == []

    def test_non_master_base_refused(self, capsys):
        pr = self._pr()
        pr["base"] = {"ref": "dev"}
        runner = _FakeRunner([(200, pr)])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        assert "only merges into" in capsys.readouterr().err

    def test_put_rejected_status_fails_closed(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (405, {"message": "Pull request is not mergeable"})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 1
        assert "merge request failed" in capsys.readouterr().err

class test_round2_regressions:
    def test_missing_base_is_refused(self):
        problems = check_pr({"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}}, expect_head = "a" * 40)
        assert any("no base branch" in p for p in problems)

    def test_decorated_record_matches_the_authority(self):
        # The authority strips emphasis/inline-code from EVERY record; a
        # conditional strip made the mirror diverge both ways.  Both directions:
        # a backticked VALUE must still parse (`rounds=`4`` → 4), and a
        # decorated value that parses differently must land on the same verdict
        # as the detector (`c3=3*4` → 34, i.e. rounds 4 < c3+1).
        ok = (
            "verify: rounds=`4` clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no "
            "budget=ok post-cap=0 **c3=3**"
        )
        assert body_problems(ok) == []
        looser = "verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no " "budget=ok post-cap=0 c3=3*4"
        assert any("max(4, c3+1)" in p for p in body_problems(looser))

    def _pr(self):
        return {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}

    def test_empty_commit_message_is_unreadable_not_a_crash(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"sha": "c" * 40, "commit": {}})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        out = capsys.readouterr().out
        assert "COMMIT-GET-FAILED" in out

    def test_merge_without_sha_is_an_anomaly_not_success(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        out = capsys.readouterr().out
        assert "MERGE-RESPONSE-ANOMALY" in out
        # And the bogus readback endpoint must never be called.
        assert not any(c[-1].endswith("/commits/") for c in runner.calls)

    def test_transport_failure_is_not_dressed_as_an_http_code(self, capsys):
        class _Boom:
            def __call__(self, cmd, **kwargs):
                class _P:
                    returncode = 1
                    stdout = ""
                    stderr = 'Get "https://api.github.com/": TLS handshake timeout'

                return _P()

        assert _gate("o/r", 1, None, allow_cjk = False, do_merge = False, runner = _Boom()) == 1
        err = capsys.readouterr().err
        assert "transport failure" in err
        assert "http=" not in err

def _writes(runner) -> list[tuple]:
    """The gh calls that WRITE (``-X`` verb or an ``--input`` payload).

    Counting raw calls would fail the moment the gate reads anything else —
    the comment gate's diff read is a GET — so the "must not merge" assertions
    count writes, which is what they actually mean.
    """
    return [call for call in runner.calls if ("--input" in call) or ("-X" in call)]

class _RecordingRunner(_FakeRunner):
    """FakeRunner that also records the stdin payload of each WRITE call.

    Only ``--input -`` calls carry a payload; the comment gate's diff read does
    not, and recording a ``None`` for it would shift every index the write-shape
    assertions use.
    """

    def __init__(self, responses):
        super().__init__(responses)
        self.inputs: list[str | None] = []

    def __call__(self, cmd, **kwargs):
        argv = [str(part) for part in cmd]

        if "--input" in argv:
            self.inputs.append(kwargs.get("input"))

        return super().__call__(cmd, **kwargs)

class test_merge_write_shape:
    """The gate's ONLY write action must be pinned (round-2 S2)."""

    def _pr(self):
        return {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}

    def test_squash_request_shape(self):
        runner = _RecordingRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"commit": {"message": f"{GOOD_TITLE} (#7)"}})])
        assert _gate("o/r", 7, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 0
        assert len(runner.inputs) == 1, runner.inputs
        merge_argv = [list(call) for call in runner.calls if "/merge" in " ".join(call)][0]
        assert merge_argv == ["gh", "api", "-i", "repos/o/r/pulls/7/merge", "-X", "PUT", "--input", "-"], merge_argv
        payload = json.loads(runner.inputs[0] or "{}")
        # `sha` pins the validated head server-side: a push between the GET and
        # the PUT must answer 409 instead of merging an unreviewed commit.
        assert payload == {"merge_method": "squash", "sha": "a" * 40}, payload

    def test_draft_pr_refused(self):
        problems = check_pr({"state": "open", "mergeable": True, "draft": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}, expect_head = "a" * 40)
        assert any("draft" in p for p in problems)

class test_round4_regressions:
    """Closure-round-A findings."""

    def _pr(self):
        return {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "b" * 40}, "base": {"ref": "master"}}

    def test_empty_expect_head_is_refused_not_ignored(self):
        # The exact failure the gate exists to prevent: an empty pin must not
        # silently switch the reviewed-head check off.
        problems = check_pr(self._pr(), "")
        assert any("no reviewed head" in p for p in problems), problems

    def test_too_short_expect_head_is_refused(self):
        problems = check_pr(self._pr(), "bb")
        assert any("shorter than" in p for p in problems), problems

    def test_merge_lane_never_puts_with_an_empty_pin(self):
        runner = _FakeRunner([(200, self._pr())])
        assert _gate("o/r", 1, "", allow_cjk = False, do_merge = True, runner = runner) == 1
        # The gate reads three things before it can act: the PR payload, the
        # comment gate's diff, and the artifact gate's file list. "One payload
        # read" is therefore about /pulls/<n> ALONE — MUT:payload-read-count,
        # where counting every call instead reads 3 (payload + diff + files).
        json_reads = [c for c in runner.calls if c[-1].endswith("/pulls/1")]
        assert len(json_reads) == 1, "an unverified head must never reach the merge PUT"
        assert not any("merge" in " ".join(c) for c in runner.calls)

    def test_launcher_codes_are_not_http(self):
        from celestia_devtools.vcs.pr_gate import _failure_detail

        for code in (126, 127):  # both shell launcher codes sit inside the HTTP range
            detail = _failure_detail(code, {}, "gh: launcher failure")
            assert "http=" not in detail, detail
            assert f"exit {code}" in detail, detail

        assert "http=404" in _failure_detail(404, {"message": "Not Found"}, "")
        assert "http=500" in _failure_detail(500, {}, "")

    def test_transport_retry_is_pinned(self, capsys):
        # Two failed reads must actually issue two GETs and say so.
        runner = _FakeRunner([
            (200, self._pr()),
            (200, {"merged": True, "sha": "c" * 40}),
            (500, {}),
            (500, {})
        ])
        assert _gate("o/r", 1, "b" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        commit_calls = [c for c in runner.calls if "commits" in " ".join(c)]
        assert len(commit_calls) == 2, commit_calls
        assert "(2 attempt(s))" in capsys.readouterr().out

    def test_one_attempt_is_reported_truthfully(self, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"commit": {}})])
        assert _gate("o/r", 1, "b" * 40, allow_cjk = False, do_merge = True, runner = runner) == 2
        assert "(1 attempt(s))" in capsys.readouterr().out

class test_round6_regressions:
    """Closure-round-C findings: CLI contract and status-path pins."""

    def test_check_without_expect_head_is_an_argparse_error(self):
        # The documented-but-impossible head-less form must fail at the CLI
        # boundary (rc 2), not silently reach the gate and fail there.
        from celestia_devtools.vcs.pr_gate import main

        try:
            main(["check", "--repo", "o/r", "--pr", "1"])
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover - the assertion is the point
            raise AssertionError("check without --expect-head must exit 2")

    def test_merge_lane_also_requires_the_pin(self):
        from celestia_devtools.vcs.pr_gate import main

        try:
            main(["merge", "--repo", "o/r", "--pr", "1"])
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover
            raise AssertionError("merge without --expect-head must exit 2")

    def test_non_2xx_status_surfaces_as_http(self, capsys):
        # The non-2xx half of the header parsing was unpinned: neutralising the
        # regex fell back to gh's exit code and reported a 404 as a transport
        # failure.
        runner = _FakeRunner([(404, {"message": "Not Found"})])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        err = capsys.readouterr().err
        assert "http=404" in err, err
        assert "transport failure" not in err, err

    def test_bare_one_digit_reference_is_accepted(self):
        # Documented carve-out: a bare "#1" reads as an ordinary reference,
        # while a bare two-digit reference is still refused.
        assert title_problems("✨ Fix the #1 crash in the parser.") == []
        assert title_problems("✨ Fix the #12 crash in the parser.")

class test_post_check_push:
    """A push landing between the GET and the PUT must not merge."""

    def _pr(self):
        return {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}

    def test_moved_head_is_a_409_not_a_silent_merge(self, capsys):
        runner = _RecordingRunner([(200, self._pr()), (409, {"message": "Head branch was modified. Review and try the merge again."})])
        assert _gate("o/r", 7, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 1
        assert "http=409" in capsys.readouterr().err

class test_zwj_gitmoji:
    """The zero-width strip must not break ZWJ-sequence gitmojis — but a ZWJ
    outside a whitelist sequence is still stripped (no smuggling)."""

    def test_zwj_sequence_gitmoji_accepted(self):
        fixture = "\U0001f9d1\u200d\U0001f4bb Add the pair-programming banner to the console."
        assert title_problems(fixture) == []

    def test_zwj_inside_version_word_still_caught(self):
        fixture = "✨ Extend the support matrix for v\u200d2 clients before the cut."
        assert title_problems(fixture), fixture

    def test_zwj_inside_banned_word_still_caught(self):
        fixture = "✨ Pr\u200dobe the parser crash handling before the cut."
        assert any("probe/placeholder" in p for p in title_problems(fixture)), fixture

    def test_zwj_inside_colon_prefix_still_caught(self):
        fixture = "✨ Fix\u200d: the parser crash handling before the cut."
        assert title_problems(fixture), fixture

    def test_protection_is_scoped_to_the_sequence(self):
        # Protection covers the whitelist sequence only: the SAME title with a
        # lone ZWJ inside a version word must still be stripped and caught.
        fixture = (
            "\U0001f9d1\u200d\U0001f4bb Extend the support matrix for v\u200d2 "
            "clients before the cut."
        )
        assert any("version word" in p for p in title_problems(fixture)), fixture

    def test_leading_zwsp_is_stripped_not_fatal(self):
        # A stray leading ZWSP is noise; after the strip the title is compliant.
        fixture = "\u200b\U0001f9d1\u200d\U0001f4bb Add the pair-programming banner."
        assert title_problems(fixture) == []

class test_l910_residue:
    """The three latent items recorded as L910 — closed."""

    def test_two_zwj_emojis_stay_distinct(self, monkeypatch):
        # L910①: the sentinel scheme mapped every parked sequence back onto the
        # LONGEST one, so a second whitelist entry would have collapsed them.
        # Splitting needs no placeholder, so distinctness is structural — this
        # pins it with a synthetic two-sequence whitelist.
        from celestia_devtools.vcs import pr_gate

        first = "\U0001f9d1\u200d\U0001f4bb"   # 🧑‍💻
        second = "\U0001f468\u200d\U0001f4bb"  # 👨‍💻 (not in the real whitelist)
        monkeypatch.setattr(pr_gate, "_ZWJ_EMOJIS", ())
        monkeypatch.setattr(pr_gate, "_ZWJ_SPLIT_RE", None)
        monkeypatch.setattr("celestia_devtools.vcs.commit_msg.GITMOJI_WHITELIST", frozenset({first, second}), raising = True)
        normalised = pr_gate._normalise_title(f"{first} and {second} both work.")
        assert (first in normalised) and (second in normalised), normalised
        assert normalised.count("\u200d") == 2, normalised

    def test_no_placeholder_character_is_ever_written(self):
        # L910②: the old design rewrote a pre-existing U+E000 into a gitmoji,
        # so the guard lived only in the caller.  There is no placeholder any
        # more; assert a U+E000 survives normalisation untouched (the refusal
        # happens in title_problems, on the raw title).
        from celestia_devtools.vcs.pr_gate import _normalise_title

        assert "\ue000" in _normalise_title("\ue000Add the banner for the console users now.")

    def test_private_use_refused_at_any_position(self):
        # L910②(behaviour): the refusal is what protects the subject line.
        for fixture in (
            "\ue000\u2728 Add the pair-programming banner to the console.",
            "\ue000Add the pair-programming banner to the console.",
            "\u2728 Add the \ue000 banner for the console users now.",
            "\u2728\ue000 Add the pair-programming banner to the console."
        ):
            problems = title_problems(fixture)
            assert any("private-use" in p for p in problems), (fixture, problems)

    def test_supplementary_private_use_planes_refused(self):
        # U+F0000 (supplementary PUA-A) is invisible too.
        problems = title_problems("\U000f0000\u2728 Add the banner for the console users now.")
        assert any("private-use" in p for p in problems), problems

    def test_docstring_documents_the_strip(self):
        # L910③: the module docstring must name the private-use refusal and the
        # ZWJ survival, i.e. the two behaviours a reader of check 3 needs.
        from celestia_devtools.vcs import pr_gate

        doc = pr_gate.__doc__ or ""
        assert "private-use" in doc, doc[:200]
        assert "ZWJ" in doc

    def test_clearing_the_emoji_cache_forces_a_rebuild(self, monkeypatch):
        # L910④: the two globals are the cache, so clearing one alone must not
        # leave a stale regex in place (the sentinel-era trap).
        from celestia_devtools.vcs import pr_gate

        monkeypatch.setattr(pr_gate, "_ZWJ_EMOJIS", ())
        monkeypatch.setattr(pr_gate, "_ZWJ_SPLIT_RE", None)
        assert pr_gate._zwj_splitter() is not None
        assert pr_gate._ZWJ_EMOJIS, "first call must fill the cache"
        # Clear ONLY the tuple (the regex stays warm): the old guard keyed on
        # the regex alone and would have kept serving the stale pair.
        monkeypatch.setattr(pr_gate, "_ZWJ_EMOJIS", ())
        pr_gate._zwj_splitter()
        assert pr_gate._ZWJ_EMOJIS, "clearing the tuple must force a rebuild"

    def test_strip_set_is_documented_precisely(self):
        # L910③: the docstring must not claim every zero-width character is
        # stripped — only the four in the strip set.
        from celestia_devtools.vcs import pr_gate

        doc = pr_gate.__doc__ or ""
        assert "U+200B/200C/200D/FEFF" in doc, doc[:300]

# ── recorded-exception waivers (2026-11-21 user ruling) ────────────────────
#
# The waiver exists for one deadlock: code and gates green, every review finding
# fixed, but the record's HISTORY carries an S1 (so the structural lane's
# S0=S1=0 fails) and the c3 streak cannot be rebuilt without re-running rounds
# the budget already rejected.  Its whole value is that it is AUDITABLE and
# NARROW: a signed, PR-and-head-scoped file that suppresses exactly one refusal.
# These tests are therefore mostly negatives — a waiver that does not match must
# never authorise a merge.
C3_ZERO_RECORD = (
    "verify: rounds=6 clean=0 S0=0 S1=1 S2=6 S3=26 gates=6 hardcap=yes "
    "budget=exhausted@3 c3=0"
)

def _waiver_text(head = "a" * 40, pr = 1, repo = "r", *, basis = None, signed = "signed: lead 2026-11-21", decl = "c3-not-fabricated: yes"):
    return f"# pr-merge waiver — celestia-island/{repo} #{pr}\n" f"pr: {pr}\n" f"head: {head}\n" f"basis: {basis if basis is not None else 'eleven review rounds, every finding cleared, gates green on this SHA'}\n" f"{decl}\n" f"{signed}\n"

class test_waiver_validation:
    def _write(self, tmp_path, name, text):
        alerts = tmp_path / "_alerts"
        alerts.mkdir(exist_ok = True)
        path = alerts / name
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def test_matching_waiver_is_accepted(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text())
        grant, problems = read_waiver(path, repo = "celestia-island/r", pr = 1, head = "a" * 40)
        assert problems == []
        assert grant is not None
        assert grant.signed == "2026-11-21"
        assert grant.pr == 1

    def test_head_prefix_is_accepted(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(head = "a" * 12))
        grant, problems = read_waiver(path, repo = "celestia-island/r", pr = 1, head = "a" * 40)
        assert (problems == []) and (grant is not None)

    def test_waiver_outside_the_alerts_directory_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = tmp_path / "pr-merge-waiver-r-1.md"
        path.write_text(_waiver_text(), encoding = "utf-8")
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("'_alerts' directory" in p for p in problems)

    def test_non_utf8_waiver_is_refused_not_crashed(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", b"\xff\xfe\x80 not utf-8")
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("unreadable" in p for p in problems)

    def test_zero_width_basis_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(basis = "\u200b" * 60))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("at least" in p for p in problems)

    def test_impossible_signoff_date_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(signed = "signed: lead 2026-02-30"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("not a real date" in p for p in problems)

    def test_decorated_pr_number_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text().replace("pr: 1", "pr: #1"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("does not match #1" in p for p in problems)

    def test_missing_file_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        grant, problems = read_waiver(tmp_path / "pr-merge-waiver-r-1.md", repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("not a readable file" in p for p in problems)

    def test_wrong_filename_shape_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "waiver.md", _waiver_text())
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("name must be" in p for p in problems)

    def test_wrong_repo_in_filename_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-other-1.md", _waiver_text())
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("names repo" in p for p in problems)

    def test_wrong_pr_in_filename_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-2.md", _waiver_text(pr = 2))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("names PR 2" in p for p in problems)

    def test_pr_field_mismatch_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        text = _waiver_text().replace("pr: 1", "pr: 7")
        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", text)
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("does not match #1" in p for p in problems)

    def test_missing_pr_line_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        text = _waiver_text().replace("pr: 1\n", "")
        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", text)
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("missing a `pr:`" in p for p in problems)

    def test_head_mismatch_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(head = "b" * 40))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("does not match the PR head" in p for p in problems)

    def test_head_that_is_not_a_sha_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(head = "HEAD"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("is not a commit SHA" in p for p in problems)

    def test_payload_without_head_cannot_be_covered(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text())
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "")
        assert (grant is None) and any("no head SHA to match" in p for p in problems)

    def test_thin_basis_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(basis = "trust me"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any(("at least" in p) and ("characters" in p) for p in problems)

    def test_missing_c3_declaration_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        text = _waiver_text().replace("c3-not-fabricated: yes\n", "")
        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", text)
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("c3-not-fabricated" in p for p in problems)

    def test_declaring_a_fabricated_c3_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(decl = "c3-not-fabricated: no"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("may not bless a fabricated c3" in p for p in problems)

    def test_missing_signoff_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        text = _waiver_text(signed = "")
        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", text)
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("sign-off line" in p for p in problems)

    def test_signoff_by_someone_else_is_refused(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(signed = "signed: alice 2026-11-21"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("sign-off line" in p for p in problems)

    def test_signoff_needs_a_date(self, tmp_path):
        from celestia_devtools.vcs.pr_gate import read_waiver

        path = self._write(tmp_path, "pr-merge-waiver-r-1.md", _waiver_text(signed = "signed: lead"))
        grant, problems = read_waiver(path, repo = "o/r", pr = 1, head = "a" * 40)
        assert (grant is None) and any("sign-off line" in p for p in problems)

class test_waivable_split:
    def test_only_the_c3_floor_is_waivable(self):
        from celestia_devtools.vcs.pr_gate import split_waivable

        blocking, waivable = split_waivable(["verify record c3=0 < 3", "no reviewed head SHA supplied — the closing verification is unverified", "verify record rounds=6 < max(4, c3+1)", "PR title reads as a probe/placeholder"])
        assert waivable == ["verify record c3=0 < 3"]
        assert len(blocking) == 3

    def test_other_verify_refusals_are_not_waivable(self):
        from celestia_devtools.vcs.pr_gate import split_waivable

        blocking, waivable = split_waivable(["structural exemption needs S0=S1=0 (unfixed reds never exempt)"])
        assert (waivable == []) and (len(blocking) == 1)

class test_waiver_merge_path:
    def _pr(self, *, title = GOOD_TITLE, head = "a" * 40, body = GOOD_BODY + "\n" + C3_ZERO_RECORD):
        return {"state": "open", "mergeable": True, "title": title, "body": body, "head": {"sha": head}, "base": {"ref": "master"}}

    def _waiver(self, tmp_path, *, head = None, name = "pr-merge-waiver-r-1.md", **kwargs):
        alerts = tmp_path / "_alerts"
        alerts.mkdir(exist_ok = True)
        path = alerts / name
        path.write_text(_waiver_text(head = head or ("a" * 40), **kwargs), encoding = "utf-8")
        return path

    def test_c3_zero_is_refused_without_a_waiver(self, capsys):
        runner = _FakeRunner([(200, self._pr())])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner) == 1
        assert "c3=0 < 3" in capsys.readouterr().err
        assert _writes(runner) == [], "a refused gate must not reach the merge PUT"

    def test_matching_waiver_unblocks_check_mode(self, tmp_path, capsys):
        runner = _FakeRunner([(200, self._pr())])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner, waiver = self._waiver(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert ("🔓 waiver" in out) and ("c3=0 < 3" in out)

    def test_matching_waiver_unblocks_the_merge(self, tmp_path, capsys):
        runner = _FakeRunner([(200, self._pr()), (200, {"merged": True, "sha": "c" * 40}), (200, {"commit": {"message": f"{GOOD_TITLE} (#1)\n\nbody"}})])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path))
        assert rc == 0
        assert "merged subject verified" in capsys.readouterr().out
        assert len(_writes(runner)) == 1

    def test_waiver_does_not_excuse_a_bad_title(self, tmp_path, capsys):
        runner = _FakeRunner([(200, self._pr(title = "probe-x (#2758)"))])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path))
        assert rc == 1
        assert _writes(runner) == []
        assert "probe" in capsys.readouterr().err

    def test_waiver_does_not_excuse_a_moved_head(self, tmp_path):
        runner = _FakeRunner([(200, self._pr(head = "b" * 40))])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path))
        assert rc == 1
        assert _writes(runner) == []

    def test_waiver_for_another_commit_is_refused(self, tmp_path, capsys):
        runner = _FakeRunner([(200, self._pr())])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path, head = "b" * 40))
        assert rc == 1
        assert "does not cover this merge" in capsys.readouterr().err
        assert _writes(runner) == []

    def test_unsigned_waiver_is_refused(self, tmp_path):
        runner = _FakeRunner([(200, self._pr())])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path, signed = ""))
        assert (rc == 1) and (len(runner.calls) == 1)

    def test_waiver_for_another_pr_is_refused(self, tmp_path):
        runner = _FakeRunner([(200, self._pr())])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = True, runner = runner, waiver = self._waiver(tmp_path, name = "pr-merge-waiver-r-2.md", pr = 2))
        assert (rc == 1) and (len(runner.calls) == 1)

    def test_a_clean_record_still_passes_without_a_waiver(self):
        runner = _FakeRunner([(200, self._pr(body = GOOD_BODY + GOOD_RECORD))])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0

    def test_cli_accepts_the_waiver_flag(self, tmp_path, monkeypatch, capsys):
        from celestia_devtools.vcs import pr_gate

        seen: dict = {}

        def fake_gate(repo, pr, expect_head, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(pr_gate, "_gate", fake_gate)
        waiver = self._waiver(tmp_path)
        rc = pr_gate.main(["merge", "--repo", "o/r", "--pr", "1", "--expect-head", "a" * 40, "--waiver", str(waiver)])
        assert rc == 0
        assert seen["waiver"] == str(waiver)
        assert seen["do_merge"] is True

# ── comment gate (phase 4, first cut) ────────────────────────────────────────
#
# The behaviour-claim scanner lives in `celestia_devtools.lint.comment_gate`;
# what these tests pin is the PLUMBING and the waiver reach:
#
#   * a claim in an added test-comment line refuses the merge;
#   * `--waiver` does NOT reach it (the signed file's one waivable refusal
#     stays `verify record c3 < 3`) — the mutation control for that claim is
#     "remove the category guard and the --waiver run goes green";
#   * the PR-body `comment-gate-waiver: <reason>` entry is the only exception,
#     it is announced, and a malformed entry is refused rather than ignored;
#   * a PR that touches no test file skips the check (announced, not silent) and
#     a PR whose file list cannot be read refuses BLIND.
COMMENT_CLAIM_PATCH = "@@ -1,0 +1,2 @@\n+// measured: this pin catches the reload\n+expect(1).toBe(1);\n"
COMMENT_ANCHORED_PATCH = "@@ -1,0 +1,3 @@\n+// measured: this pin catches the reload\n+expect(1).toBe(1);\n+// MUT: reload-dropped\n"
COMMENT_ROW_PATCH = "@@ -1,0 +1,2 @@\n+// | mutation | drop the reload | 1 green |\n+// measured: this catches it\n"

class diff_runner(_FakeRunner):
    """A gh stub that answers the PR payload and the PR unified diff.

    The comment gate reads ``/pulls/<n>`` twice: once as JSON (title, body,
    head, state) and once as ``application/vnd.github.diff``. The stub keys on
    the Accept header, because a gate that dropped the media type would get the
    JSON body back as a "diff", find no test path in it, and pass — the exact
    silent-green shape this harness exists to catch.
    """

    def __init__(self, pr, diff, *, diff_status = 200, files = None):
        self.pr = pr
        self.diff = diff
        self.diff_status = diff_status
        #: What ``/pulls/<n>/files`` answers. Defaulting it to the diff's own
        #: path keeps a caller that does not care consistent — MUT:files-default.
        self.files = files if files is not None else [line.split(" b/")[-1] for line in diff.splitlines() if line.startswith("diff --git")]
        super().__init__([])

    def __call__(self, cmd, **kwargs):
        argv = [str(part) for part in cmd]
        accepted = ""

        for index, part in enumerate(argv):
            if (part == "-H") and ((index + 1) < len(argv)):
                accepted = argv[index + 1]

        self.calls.append(tuple(argv))
        wants_diff = "vnd.github.diff" in accepted

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        joined = " ".join(argv)

        if "/files" in joined:
            # The artifact gate asks which paths a PR touches, NOT which patch
            # lines it changes: a PR can regenerate bindings without editing the
            # WIT, and that is exactly the drift this gate looks for.
            status, body = 200, [{"filename": path} for path in self.files]
        elif wants_diff and ("/pulls/" in joined):
            status, body = self.diff_status, self.diff
        elif "/pulls/" in joined:
            status, body = 200, self.pr
        else:
            return super().__call__(cmd, **kwargs)

        probe = _P()
        probe.returncode = 0 if status < 400 else 1
        rendered = body if isinstance(body, str) else json.dumps(body)
        probe.stdout = f"HTTP/2.0 {status} OK\n\n" + rendered
        return probe

def _diff_for(patch: str, filename: str = "packages/vue/src/useHighlight.test.ts") -> str:
    return f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n+++ b/{filename}\n{patch}"

def _comment_pr(patch: str, filename: str = "packages/vue/src/useHighlight.test.ts", body: str | None = None, **overrides):
    pr = {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": body if body is not None else GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}
    pr.update(overrides)
    return pr, _diff_for(patch, filename)

class test_comment_gate_plumbing:
    def test_claim_in_an_added_comment_refuses(self, capsys):
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH)
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        err = capsys.readouterr().err
        assert "comment-gate: packages/vue/src/useHighlight.test.ts:1" in err
        assert "MERGE REFUSED" in err

    def test_anchored_claim_passes(self, capsys):
        pr, diff = _comment_pr(COMMENT_ANCHORED_PATCH)
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0
        assert "gate passed" in capsys.readouterr().out

    def test_mutation_row_in_the_block_passes(self, capsys):
        pr, diff = _comment_pr(COMMENT_ROW_PATCH)
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0
        capsys.readouterr()

    def test_the_same_claim_in_production_code_is_out_of_scope(self, capsys):
        # Production comments are deliberately out of scope (see the scanner's
        # docstring): the gate judges test files only.
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH, filename = "packages/vue/src/useHighlight.ts")
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0
        assert "nothing to judge" in capsys.readouterr().out

    def test_unreadable_diff_refuses_blind(self, capsys):
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH)
        runner = diff_runner(pr, diff, diff_status = 500)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        assert "refusing blind" in capsys.readouterr().err

    def test_truncated_diff_refuses_instead_of_certifying_part(self, capsys):
        # GitHub cuts a big diff and appends a marker line. A gate that scanned
        # the visible part would report green over evidence it never saw.
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH)
        truncated = "diff --git a/src/only.ts b/src/only.ts\n--- a/src/only.ts\n+++ b/src/only.ts\n@@ -1,0 +1,1 @@\n+// neutral\n[truncated 7 lines]"
        runner = diff_runner(pr, truncated)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        assert "truncated" in capsys.readouterr().err

    def test_empty_diff_refuses_blind(self, capsys):
        pr, _diff = _comment_pr(COMMENT_CLAIM_PATCH)
        runner = diff_runner(pr, "   \n")
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        assert "refusing blind" in capsys.readouterr().err

    def test_the_scanner_refusals_reach_the_merge_chain(self, capsys):
        """Zero-hit / zero-scanned refusals must refuse the MERGE, not just the module.

        Round 3's mutation 2 deleted the `report.problems` forwarding and every
        plumbing test stayed green: the scanner refused a comment-only neutral
        diff while the gate printed "gate passed". This is that case at the
        gate's own level.
        """
        pr, diff = _comment_pr("@@ -1,0 +1,1 @@\n+// a neutral comment\n", filename = "packages/vue/src/neutral.test.ts")
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        err = capsys.readouterr().err
        assert "zero behaviour-claim hits" in err
        assert "MERGE REFUSED" in err

    def test_break_glass_flag_skips_the_gate(self, capsys):
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH)
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner, comment_gate = False) == 0
        capsys.readouterr()

class test_comment_gate_waiver:
    """`--waiver` must NOT reach the comment gate; the body entry must."""

    def _waiver(self, tmp_path):
        alerts = tmp_path / "_alerts"
        alerts.mkdir()
        path = alerts / "pr-merge-waiver-r-1.md"
        path.write_text("pr: 1\nhead: " + ("a" * 40) + "\n" "basis: " + ("x" * 60) + "\n" "c3-not-fabricated: yes\nsigned: lead 2026-11-22\n", encoding = "utf-8")
        return path

    def test_signed_waiver_does_not_excuse_a_comment_claim(self, tmp_path, capsys):
        """A CLEAN ``c3=3`` body, so the unanchored claim is the ONLY refusal.

        Round 3 wrote this test with a ``c3=1`` fixture, which fails the record's
        own ``rounds >= max(4, c3+1)`` rule — an independent refusal — so
        ``assert rc == 1`` passed for the wrong reason and both review rounds
        found the "mutation control" it advertised to be a no-op. ``GOOD_RECORD``
        is clean: if this run ever goes green, the signed waiver reached the
        comment gate.
        """
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH)
        runner = diff_runner(pr, diff)
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner, waiver = self._waiver(tmp_path))
        assert rc == 1
        out, err = capsys.readouterr()
        # The signed channel still runs, and suppresses nothing here.
        assert "nothing (waiver unused)" in out
        assert "comment-gate: packages/vue/src/useHighlight.test.ts:1" in err

    def test_split_waivable_never_waives_a_comment_gate_refusal(self, monkeypatch):
        """The exclusion lives in the CALLEE, so a caller refactor cannot open it.

        Rounds 3 and 4 each broke the single-layer version by editing the CALL
        SITE: widen `_WAIVABLE_C3_RE`, move the merge above the split, and the
        signed waiver suppressed an unanchored claim — with a green suite both
        times. This reproduces that attack and asserts the classifier refuses.
        """
        from celestia_devtools.vcs import pr_gate as module

        monkeypatch.setattr(module, "_WAIVABLE_C3_RE", re.compile(r"^(?:verify record c3=(?P<c3>\d+) < 3|comment-gate: )"))
        blocking, waivable = module.split_waivable(["comment-gate: a.test.ts:1: // claim", "verify record c3=0 < 3"])
        assert blocking == ["comment-gate: a.test.ts:1: // claim"]
        assert waivable == ["verify record c3=0 < 3"]

    def test_the_comment_gate_category_is_never_waivable(self):
        from celestia_devtools.vcs import pr_gate as module

        assert module.split_waivable([f"{module.COMMENT_GATE_CATEGORY}: x"])[1] == []

    def test_gate_keeps_the_comment_gate_list_out_of_the_split(self):
        from celestia_devtools.vcs import pr_gate as module

        payload = inspect.getsource(module._gate)
        assert "split_waivable(problems)" in payload
        assert "split_waivable(gated)" not in payload

    def test_body_entry_excuses_and_is_announced(self, capsys):
        body = GOOD_BODY + GOOD_RECORD + "\ncomment-gate-waiver: the probe ids live in the linked issue, not the file\n"
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH, body = body)
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 0
        out = capsys.readouterr().out
        assert "comment-gate-waiver (PR body)" in out
        assert "suppressed 2 comment-gate refusal(s)" in out

    def test_body_entry_does_not_excuse_a_bad_title(self, capsys):
        body = GOOD_BODY + GOOD_RECORD + "\ncomment-gate-waiver: the probe ids live in the linked issue, not the file\n"
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH, body = body, title = "probe-x (#2758)")
        runner = diff_runner(pr, diff)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner) == 1
        capsys.readouterr()

class test_comment_waiver_entry:
    """The body entry: one per PR, a real reason, prose only."""

    def test_absent_entry_is_legal(self):
        grant, problems = pr_gate_comment_waiver(GOOD_RECORD)
        assert grant is None
        assert problems == []

    def test_reason_is_read_verbatim(self):
        grant, problems = pr_gate_comment_waiver("comment-gate-waiver: the ids live in the linked issue")
        assert problems == []
        assert grant is not None
        assert grant.reason == "the ids live in the linked issue"

    def test_short_reason_refused(self):
        grant, problems = pr_gate_comment_waiver("comment-gate-waiver: ok")
        assert grant is None
        assert problems and ("floor" in problems[0])

    def test_two_entries_refused(self):
        text = "comment-gate-waiver: the ids live in the linked issue, page two\n" "comment-gate-waiver: and also in the archived probe log somewhere\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems and ("2 comment-gate-waiver entries" in problems[0])

    def test_fenced_entry_is_not_a_grant(self):
        text = "```\ncomment-gate-waiver: <reason goes here, never a real grant>\n```\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems == []

    def test_an_indented_entry_is_not_a_grant(self):
        """Round 5: a template copy-pasted into an indented block became a live grant.

        The verify record already refuses exactly this shape ("it reads as a
        template, not a record"); the waiver lane now agrees with it.
        """
        text = "verify: x\n\n    comment-gate-waiver: the ids live in the linked issue, page two\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems and ("indented code block" in problems[0])

    def test_a_tab_led_entry_is_indented_too(self):
        """Round 6: a narrower regex let `"\t  entry"` through while the record refused it."""
        text = "\t  comment-gate-waiver: the ids live in the linked issue, page two\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems and ("indented code block" in problems[0])

    def test_three_spaces_are_prose_no_four_are_a_block(self):
        # Parity with the verify record's own rule: lead of >=4 chars, or a tab.
        assert pr_gate_comment_waiver("   comment-gate-waiver: the ids live in the linked issue, page two")[0] is not None
        assert pr_gate_comment_waiver("    comment-gate-waiver: the ids live in the linked issue, page two")[0] is None

    def test_a_zero_width_reason_is_not_a_reason(self):
        # A floor a run of U+200B satisfies is a mute button (round 6).
        grant, problems = pr_gate_comment_waiver("comment-gate-waiver: " + ("\u200b" * 30))
        assert grant is None
        assert problems

    def test_an_entry_in_prose_and_indented_refuses(self):
        text = "comment-gate-waiver: the ids live in the linked issue, page two\n    comment-gate-waiver: a second copy in an indented block\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems and ("indented code block" in problems[0])

    def test_unclosed_fence_refuses(self):
        text = "```\ncomment-gate-waiver: the ids live in the linked issue, page two\n"
        grant, problems = pr_gate_comment_waiver(text)
        assert grant is None
        assert problems and ("unclosed" in problems[0])

    def test_body_problems_reports_a_malformed_entry(self):
        # A malformed exception must surface at merge time, not as "the gate
        # still says no" with no explanation.
        problems = body_problems(GOOD_RECORD + "\ncomment-gate-waiver: ok\n")
        assert any("floor" in p for p in problems)

    def test_body_problems_accepts_a_good_entry(self):
        assert body_problems(GOOD_RECORD + "\ncomment-gate-waiver: the ids live in the linked issue\n") == []

class test_comment_gate_cli:
    def test_cli_flag_reaches_the_gate(self, monkeypatch, capsys):
        seen: dict = {}

        def fake_gate(repo, pr, expect_head, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(pr_gate_module, "_gate", fake_gate)
        rc = pr_gate_module.main(["check", "--repo", "o/r", "--pr", "1", "--expect-head", "a" * 40])
        assert rc == 0
        assert seen["comment_gate"] is True

    def test_cli_break_glass_flag(self, monkeypatch, capsys):
        seen: dict = {}

        def fake_gate(repo, pr, expect_head, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(pr_gate_module, "_gate", fake_gate)
        rc = pr_gate_module.main(["check", "--repo", "o/r", "--pr", "1", "--expect-head", "a" * 40, "--no-comment-gate"])
        assert rc == 0
        assert seen["comment_gate"] is False

# ── artifact gate (phase 4, second cut) ─────────────────────────────────────
#
# The WIT-vs-bindings comparison lives in `celestia_devtools.lint.artifact_drift`;
# what these tests pin is the PLUMBING and the reach:
#
#   * a PR that touches neither the WIT nor the wasm bindings is a LOUD SKIP,
#     with no comparison run at all;
#   * a PR that touches either root runs the comparison against the checkout,
#     and a drift refusal reaches the merge verdict;
#   * `--waiver` does NOT reach it (the refusals ride the same `gated` channel
#     the comment gate uses) — MUT:artifact-waiver-blocked; and the PR-body
#     `artifact-gate-waiver:` entry does;
#   * the two gates are independent: waiving one is not waiving the other.
ARTIFACT_WIT_PATH = "packages/wasm/qr/wit/qr.wit"

def _artifact_workspace(tmp_path, monkeypatch, *, artifact_version = "0.1.0", wit_version = "0.1.0"):
    """A miniature hikari checkout and the env var that points the gate at it."""
    from celestia_devtools.lint import artifact_drift

    wit_dir = tmp_path / "packages" / "wasm" / "qr" / "wit"
    wit_dir.mkdir(parents = True)
    wit = f"package celestia:hikari-qr@{wit_version};\n\ninterface types {{\n    record thing {{ size: u8, }}\n}}\n\nworld qr {{\n    use types.{{thing}};\n}}\n"
    (wit_dir / "qr.wit").write_text(wit, encoding = "utf-8")
    interfaces = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "interfaces"
    interfaces.mkdir(parents = True)
    (interfaces / "celestia-hikari-qr-types.d.ts").write_text(f"/** @module Interface celestia:hikari-qr/types@{artifact_version} **/\n", encoding = "utf-8")
    monkeypatch.setenv("CELESTIA_ARTIFACT_ROOT", str(tmp_path))
    assert artifact_drift is not None
    return tmp_path

def _artifact_pr(files, body: str | None = None, **overrides):
    """A PR payload whose file list is ``files`` (the diff is irrelevant here)."""
    pr = {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": body if body is not None else GOOD_BODY + GOOD_RECORD, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}
    pr.update(overrides)
    # A neutral claim-free patch: these tests are about the ARTIFACT gate, so the
    # comment gate must have nothing to say (a claim here would refuse first).
    neutral = _diff_for("@@ -1,0 +1,3 @@\n+// MUT: neutral-fixture\n+// measured: the fixture claim the MUT line above anchors\n+expect(1).toBe(1);\n", filename = "packages/vue/src/useHighlight.test.ts")
    return pr, neutral, files

# The PR-body prose gate rides the SAME family: refusals in `gated` (never in
# the waiver split), its own body token, and a loud skip when the body
# states nothing. MUT:body-gate-not-in-gated proves the split: moving the
# refusals into `problems` makes the signed-waiver case pass, not refuse. These pin the plumbing; the scan itself is tested in
# `tests/test_body_gate.py`.
BODY_UNSOURCED = "The suite went from 12 failed to 0 over the cut.\n"
BODY_ANCHORED = "The suite went from `12 failed` to `0` over the cut (`vite build` → rc=0).\n"

class test_body_gate_plumbing:
    def _gate(self, body, *, runner = None, **kwargs):
        pr = {"state": "open", "mergeable": True, "title": GOOD_TITLE, "body": body, "head": {"sha": "a" * 40}, "base": {"ref": "master"}}
        # An ANCHORED test-comment claim, so the comment gate is satisfied and
        # the body gate is the only thing under test here.
        diff = _diff_for(COMMENT_ANCHORED_PATCH)
        return _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = runner or diff_runner(pr, diff), **kwargs)

    def test_an_unsourced_quantity_refuses_the_merge(self, capsys):
        rc = self._gate(GOOD_BODY + GOOD_RECORD + "\n" + BODY_UNSOURCED)
        assert rc == 1
        err = capsys.readouterr().err
        assert f"{pr_gate_module.BODY_GATE_CATEGORY}: body:" in err
        assert "MERGE REFUSED" in err

    def test_an_anchored_body_passes(self, capsys):
        assert self._gate(GOOD_BODY + GOOD_RECORD + "\n" + BODY_ANCHORED) == 0
        assert "gate passed" in capsys.readouterr().out

    def test_a_body_with_no_statement_is_a_loud_skip(self, capsys):
        assert self._gate("Add the thing.\n" + GOOD_RECORD) == 0
        assert f"{pr_gate_module.BODY_GATE_CATEGORY}: no quantity or behaviour statement" in capsys.readouterr().out

    def test_an_empty_body_refuses_even_with_a_clean_record(self, capsys):
        # A record with no prose around it is not a body: the record check
        # accepts it and the body gate refuses it.
        assert self._gate(GOOD_RECORD) == 1
        assert "zero prose lines in the body" in capsys.readouterr().err

    def test_the_signed_waiver_does_not_excuse_a_body_claim(self, tmp_path, capsys):
        alerts = tmp_path / "_alerts"
        alerts.mkdir()
        waiver = alerts / "pr-merge-waiver-r-1.md"
        waiver.write_text("pr: 1\nhead: " + ("a" * 40) + "\n" + "basis: " + ("x" * 60) + "\n" + "c3-not-fabricated: yes\nsigned: lead 2026-11-23\n", encoding = "utf-8")
        rc = self._gate(GOOD_BODY + GOOD_RECORD + "\n" + BODY_UNSOURCED, waiver = waiver)
        assert rc == 1
        out, err = capsys.readouterr()
        assert "nothing (waiver unused)" in out
        assert f"{pr_gate_module.BODY_GATE_CATEGORY}: body:" in err

    def test_the_body_entry_excuses_and_is_announced(self, capsys):
        body = GOOD_BODY + GOOD_RECORD + "\n" + BODY_UNSOURCED + "body-gate-waiver: the figures live in the linked run log, page two\n"
        rc = self._gate(body)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"{pr_gate_module.BODY_GATE_CATEGORY}-waiver (PR body)" in out
        assert "suppressed 1 body-gate refusal(s)" in out

    def test_the_body_entry_needs_a_reason(self, capsys):
        body = GOOD_BODY + GOOD_RECORD + "\n" + BODY_UNSOURCED + "body-gate-waiver: ok\n"
        assert self._gate(body) == 1
        assert "below the 20-character floor" in capsys.readouterr().err

    def test_the_body_category_is_never_waivable(self):
        # The category clause is belt-and-braces (the generic `else` already
        # blocks it); what a caller can OBSERVE is the widened-regex attack, so
        # this case pins both halves: the plain classifier, and the classifier
        # with the regex widened to swallow the body-gate shape.
        assert pr_gate_module.split_waivable([f"{pr_gate_module.BODY_GATE_CATEGORY}: body:1 (quantity): x"])[1] == []
        doc = inspect.getsource(pr_gate_module.split_waivable)
        assert "BODY_GATE_CATEGORY" in doc

        original = pr_gate_module._WAIVABLE_C3_RE

        try:
            pr_gate_module._WAIVABLE_C3_RE = re.compile(r"^(?:verify record c3=(?P<c3>\d+) < 3|body-gate: )")
            blocking, waivable = pr_gate_module.split_waivable(
                [f"{pr_gate_module.BODY_GATE_CATEGORY}: body:1 (quantity): x", "verify record c3=0 < 3"]
            )
        finally:
            pr_gate_module._WAIVABLE_C3_RE = original

        # The body-gate refusal stays blocking; the c3 refusal is still the one
        # thing a widened regex legitimately classifies as waivable.
        assert blocking == [f"{pr_gate_module.BODY_GATE_CATEGORY}: body:1 (quantity): x"]
        assert waivable == ["verify record c3=0 < 3"]

    def test_a_refusal_carrying_the_category_stays_blocking_under_a_widened_regex(self, monkeypatch):
        # Same attack as the comment gate's: widen the regex, and the classifier
        # still refuses — the exclusion lives in the callee.
        monkeypatch.setattr(pr_gate_module, "_WAIVABLE_C3_RE", re.compile(r"^(?:verify record c3=(?P<c3>\d+) < 3|body-gate: )"))
        blocking, waivable = pr_gate_module.split_waivable(["body-gate: body:1 (quantity): x", "verify record c3=0 < 3"])
        assert blocking == ["body-gate: body:1 (quantity): x"]
        assert waivable == ["verify record c3=0 < 3"]

    def test_break_glass_flag_skips_the_gate_loudly(self, capsys):
        assert self._gate(GOOD_BODY + GOOD_RECORD + "\n" + BODY_UNSOURCED, body_gate = False) == 0
        assert f"{pr_gate_module.BODY_GATE_CATEGORY}: SKIPPED by --no-body-gate" in capsys.readouterr().out

    def test_the_cli_declares_the_flag(self):
        with pytest.raises(SystemExit) as info:
            pr_gate_module.main(["check", "--help"])

        assert info.value.code == 0

class test_artifact_gate_plumbing:
    def test_a_pr_touching_neither_root_is_a_loud_skip(self, tmp_path, monkeypatch, capsys):
        _artifact_workspace(tmp_path, monkeypatch)
        pr, diff, files = _artifact_pr(["packages/vue/src/App.vue"])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files)) == 0
        assert "artifact-drift: no WIT / wasm-binding changes" in capsys.readouterr().out

    def test_a_matching_tree_passes(self, tmp_path, monkeypatch, capsys):
        _artifact_workspace(tmp_path, monkeypatch)
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files)) == 0
        capsys.readouterr()

    def test_a_version_drift_refuses_the_merge(self, tmp_path, monkeypatch, capsys):
        """The #1485 shape, at the merge chain: WIT moved, bindings did not."""
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files)) == 1
        err = capsys.readouterr().err
        assert "artifact-drift: qr: types: wit@0.2.0 artifact 0.1.0" in err
        assert "MERGE REFUSED" in err

    def test_a_bindings_only_change_also_triggers_the_gate(self, tmp_path, monkeypatch, capsys):
        # Regenerating without moving the WIT is still drift surface.
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        pr, diff, files = _artifact_pr(["packages/vue/src/wasm/qr/interfaces/celestia-hikari-qr-types.d.ts"])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files)) == 1
        assert "artifact-drift:" in capsys.readouterr().err

    def test_the_break_glass_flag_skips_it(self, tmp_path, monkeypatch, capsys):
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files), artifact_drift = False) == 0
        capsys.readouterr()

class test_artifact_gate_waiver:
    def _waiver(self, tmp_path):
        alerts = tmp_path / "_alerts"
        alerts.mkdir()
        path = alerts / "pr-merge-waiver-r-1.md"
        path.write_text("pr: 1\nhead: " + ("a" * 40) + "\n" "basis: " + ("x" * 60) + "\n" "c3-not-fabricated: yes\nsigned: lead 2026-11-22\n", encoding = "utf-8")
        return path

    def test_signed_waiver_does_not_excuse_a_drift(self, tmp_path, monkeypatch, capsys):
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH])
        rc = _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files), waiver = self._waiver(tmp_path))
        assert rc == 1
        assert "artifact-drift: qr:" in capsys.readouterr().err

    def test_body_entry_excuses_and_is_announced(self, tmp_path, monkeypatch, capsys):
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        body = GOOD_BODY + GOOD_RECORD + "\nartifact-gate-waiver: the bindings ship from a separate release train\n"
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH], body = body)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files)) == 0
        out = capsys.readouterr().out
        assert "artifact-drift-waiver (PR body)" in out
        assert "suppressed 1 artifact-drift refusal(s)" in out

    def test_the_two_gate_waivers_are_independent(self, tmp_path, monkeypatch, capsys):
        """Waiving the artifact gate must not waive the comment gate."""
        _artifact_workspace(tmp_path, monkeypatch)
        body = GOOD_BODY + GOOD_RECORD + "\nartifact-gate-waiver: the bindings ship from a separate release train\n"
        pr, diff = _comment_pr(COMMENT_CLAIM_PATCH, body = body)
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff)) == 1
        assert "comment-gate: packages/vue/src/useHighlight.test.ts:1" in capsys.readouterr().err

    def test_a_short_artifact_reason_is_refused(self):
        grant, problems = pr_gate_module.artifact_waiver("artifact-gate-waiver: ok")
        assert grant is None
        assert problems and ("floor" in problems[0])

    def test_a_malformed_artifact_entry_refuses_at_merge_time(self):
        problems = body_problems(GOOD_RECORD + "\nartifact-gate-waiver: ok\n")
        assert any("floor" in item for item in problems)

class test_artifact_gate_round2:
    """Defects the review of this PR found in the WIRING."""

    def test_a_bad_explicit_root_refuses_instead_of_falling_back(self, monkeypatch):
        """A typo'd CELESTIA_ARTIFACT_ROOT must not silently judge /mnt/codespace/hikari."""
        monkeypatch.setenv("CELESTIA_ARTIFACT_ROOT", "/nonexistent/typo")
        root, problems = pr_gate_module._artifact_root()
        assert root is None
        assert problems and ("refusing rather than judging a tree the caller did not name" in problems[0])

    def test_a_valid_explicit_root_wins_over_the_cwd(self, tmp_path, monkeypatch):
        _artifact_workspace(tmp_path, monkeypatch)
        root, problems = pr_gate_module._artifact_root()
        assert root == str(tmp_path)
        assert problems == []

    def test_the_break_glass_skip_is_announced(self, tmp_path, monkeypatch, capsys):
        """An unrecorded skip is a green line that certifies nothing."""
        _artifact_workspace(tmp_path, monkeypatch, wit_version = "0.2.0")
        pr, diff, files = _artifact_pr([ARTIFACT_WIT_PATH])
        assert _gate("o/r", 1, "a" * 40, allow_cjk = False, do_merge = False, runner = diff_runner(pr, diff, files = files), artifact_drift = False) == 0
        assert "SKIPPED by --no-artifact-gate" in capsys.readouterr().out

    def test_an_unidentifiable_cwd_refuses_instead_of_judging_hikari(self, tmp_path, monkeypatch):
        """A repo whose cwd is not a hikari checkout must not be judged against one.

        Round 3 found the earlier fix covering only the EXPLICIT branch: with no
        env var, a cwd without packages/wasm silently resolved to
        /mnt/codespace/hikari — a different repository entirely.
        """
        monkeypatch.delenv("CELESTIA_ARTIFACT_ROOT", raising = False)
        monkeypatch.chdir(tmp_path)
        root, problems = pr_gate_module._artifact_root()
        assert root is None
        assert problems and ("refusing blind rather than judging a tree nobody named" in problems[0])

    def test_a_cwd_that_is_a_checkout_is_accepted(self, tmp_path, monkeypatch):
        """The accepted-cwd branch needs its own test, not just the refusal."""
        stub = tmp_path / "hikari-like"
        (stub / "packages" / "wasm" / "qr" / "wit").mkdir(parents = True)
        (stub / "packages" / "wasm" / "qr" / "wit" / "qr.wit").write_text("package celestia:hikari-qr@0.1.0;\ninterface types {}\nworld qr {}\n", encoding = "utf-8")
        interfaces = stub / "packages" / "vue" / "src" / "wasm" / "qr" / "interfaces"
        interfaces.mkdir(parents = True)
        (interfaces / "celestia-hikari-qr-types.d.ts").write_text("/** @module Interface celestia:hikari-qr/types@0.1.0 **/\n", encoding = "utf-8")
        monkeypatch.delenv("CELESTIA_ARTIFACT_ROOT", raising = False)
        monkeypatch.chdir(stub)
        root, problems = pr_gate_module._artifact_root()
        assert root == str(stub)
        assert problems == []
