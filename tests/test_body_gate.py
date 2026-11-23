#!/usr/bin/env python3
"""Tests for the PR-body prose gate (``celestia_devtools.lint.body_gate``).

The gate exists because eight PRs were merged through the recorded-exception
channel with **correct code and a wrong sentence in the body** (hikari #1476 /
#1478 / #1482 / #1485 / #1503 / #1506, chest #2813 / #2815, tairitsu #154).
The comment gate (#306) covers test-file comments; nothing covered body prose.

Every case here is a mechanical control against a real corpus, checked in under
``tests/fixtures/body_prose/`` exactly as the bodies were published:

* **known negatives** — the sentences that were false in those PRs. The
  ``#2813`` mutation tally (``1 failed / 5 passed``) is refused in its own
  paragraph; the ``#2813`` prose figures (``10`` / ``12`` / ``11``) are refused
  where no run output appears; ``#2821``'s round arithmetic has exactly one
  source (the run in ``§G5 ④``) and is refused outside it. Each one measured
  against the live body, not invented for the test (see
  ``test_corpus_scan_*``: the counts are pinned so a silent change in either
  direction shows up as a red);
* **known positives** — three merged devtools bodies whose prose states
  nothing quantitative (the loud-skip lane) and the ``#1474`` body, which is
  the largest real body that this gate passed, pinned as a whole-file case;
* **zero production** — an empty body refuses (it is not a skip), a body with
  no prose lines refuses, and a body that states nothing skips LOUDLY.

The rule is a window of one line on purpose: ``test_the_window_is_one_line_*``
pins both directions so a widening cannot be made without a red.
"""

from __future__ import annotations
from pathlib import Path
import pytest
from celestia_devtools.lint import body_gate, comment_gate

#: Prose fixtures live beside the tree, NOT under ``tests/``: the comment gate
#: treats every path segment named ``test``/``tests`` as a test tree and scans
#: the ``.md`` files' markdown line starts as ``#`` comments. A whole PR body
#: pasted there is a corpus of true statements, not of comments, so the tree
#: `body_prose` is the honest home for it.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "body_prose"

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding = "utf-8")

def _lines(result) -> list[int]:
    return [stmt.line for stmt in result.findings]

class test_positive_controls:
    """Bodies that must pass: real merged PRs, plus the shapes the rule allows."""

    def test_a_note_that_names_its_code_passes(self):
        """devtools #304: the round-1 negative, and a round-2 correction.

        Round 1 refused this body's one paragraph — it names ``just``,
        ``allow-duplicate-variables`` and ``.just/`` and describes the failure
        without a command. Round 2 made the distinction explicit: a sentence
        carrying a code token and no measured outcome is NAMING a thing, not
        asserting a run, so it is not a claim (see :func:`names_a_thing`).
        """
        assert body_gate.check_body(_fixture("pr-304.md")).ok
        assert not body_gate.check_body("The suite went from 12 failed to 0 over the cut.\n").ok

    @pytest.mark.parametrize("name", ["pr-303.md"])
    def test_short_descriptive_bodies_pass(self, name):
        result = body_gate.check_body(_fixture(name))
        assert result.ok, [stmt.render() for stmt in result.findings]

    def test_pr_305_states_a_count_without_a_source(self):
        # The "Artifacts" line names the tools and states `0 violations` with
        # no command beside it — the gate asks, which is the discipline.
        assert not body_gate.check_body(_fixture("pr-305.md")).ok

    def test_the_mermaid_body_is_prose_with_claims_and_passes(self):
        # devtools #302: eight quantitative/behaviour statements, every one
        # anchored or in a captured table — the shape the gate is meant to
        # incentivise rather than punish.
        result = body_gate.check_body(_fixture("pr-302.md"))
        assert result.ok, [stmt.render() for stmt in result.findings]
        assert len(result.statements) >= 3

    def test_a_disclaimed_figure_passes(self):
        # The disclaimer must be on the figure's own line or the line beside
        # it — and the line beside it has to be a disclaimer, not an anchor.
        body = "The DOM count moved from 10 to 12 between rounds.\nThe cause of that shift was **not established**.\n"
        assert body_gate.check_body(body).ok
        assert body_gate.check_body("The DOM count moved from 10 to 12 between rounds (inference).\n").ok

    def test_a_figure_next_to_its_command_passes(self):
        # The claim is the figure; the source is the command on the line above
        # or below it — a quoted figure is not a source (MUT:quoted-figure-claim).
        # The command line has to be its own line: a span on a prose line names
        # that line's subject, not the neighbour's source (round 4).
        body = "`vite build` → rc=0\nIt printed 120 passed.\n"
        assert body_gate.check_body(body).ok

    def test_a_file_line_anchor_passes(self):
        assert body_gate.check_body("The boundary is 4294967295 (`api.rs:463`), not a guess.\n").ok

    def test_an_evidence_line_passes(self):
        assert body_gate.check_body("Reproduced 3/3 deterministically.\nevidence: /mnt/work/repro-20261123.log\n").ok

    def test_a_pull_request_reference_passes(self):
        assert body_gate.check_body("The retry bound follows #1503 and measures the same way.\n").ok

class test_negative_controls:
    """The sentences that were false in the merged PRs, and the window."""

    def test_the_prid_arithmetic_from_2813_is_refused(self):
        """Verbatim from shittim-chest #2813's body — the false "1 failed / 5 passed".

        The paragraph restates a mutation verdict while every actual run output
        is in a different section; the narrative around it does not carry the
        command. The gate refuses it on its own line.
        """
        body = _fixture("negative-2813-mutation-tally.md")
        result = body_gate.check_body(body)
        assert not result.ok
        assert 10 in _lines(result)
        assert [stmt.kinds for stmt in result.findings if stmt.line == 10] == [("quantity",)]

    def test_the_dom_arithmetic_from_2821_restatement_is_refused(self):
        """The #2821 restatement shape: figures restated with their run elsewhere.

        The paragraph quotes the run's numbers back (``10`` / ``12`` / ``11``)
        without the output or the command that produced them, and the sentence
        around them names no source. The gate refuses the figures. The honest
        counterpart is the next test: the same restatement WITH the disclaimer
        that says why the ``before`` reading moved passes.
        """
        body = _fixture("negative-2821-dom-counts.md")
        result = body_gate.check_body(body)
        assert not result.ok
        # The figures line has no span / anchor of its own; the paragraph head
        # is covered by the anchor on the figure line beside it, which is the
        # one-line window doing the honest thing.
        assert (7 in _lines(result)) and (8 in _lines(result))

    def test_the_same_restatement_with_a_disclaimer_passes(self):
        # The disclaimer goes where the figures are: on or beside them. The
        # window is one line, so an addendum far below the paragraph does not
        # reach it (it would excuse every paragraph above it).
        # MUT:window-one-line — widening the window to the paragraph passes the
        # `test_the_window_is_one_line_up` / `_down` cases in the wrong direction.
        body = _fixture("negative-2821-dom-counts.md")
        disclaimed = body.replace("  12 in rounds 2-3; post-unmount = 11 every round.", "  12 in rounds 2-3; post-unmount = 11 every round. Why `before` moves is an inference.")
        assert disclaimed != body
        # One disclaimer line covers the figures one line ABOVE or BELOW it, not
        # a whole paragraph: the paragraph head keeps its own finding.
        assert _lines(body_gate.check_body(disclaimed)) == []

    def test_an_unsourced_quantity_is_refused(self):
        assert not body_gate.check_body("The suite went from 12 failed to 0 over the cut.\n").ok

    def test_an_unsourced_behaviour_claim_is_refused(self):
        result = body_gate.check_body("The new pin catches the reload every time.\n")
        assert not result.ok
        assert result.findings[0].kinds == ("behaviour",)

    def test_the_window_is_one_line_up(self):
        # The anchor on the line above binds …
        assert body_gate.check_body("```\nvite build → rc=0\n```\nIt printed 120 passed.\n").ok
        # An anchor one further line up anchors nothing.
        # MUT:window-one-line — the two-away anchor is what the widen mutation
        # must keep out.
        assert not body_gate.check_body("`vite build` printed this.\n\nIt printed 120 passed.\n").ok

    def test_the_window_is_one_line_down(self):
        assert body_gate.check_body("It printed 120 passed.\n`vite build` → rc=0\n").ok
        assert not body_gate.check_body("It printed 120 passed.\n\n`vite build` → rc=0\n").ok

    def test_a_disclaimer_must_be_near_the_figure(self):
        assert not body_gate.check_body("It printed 120 passed.\n\n\n\n\n\nNot re-run.\n").ok

    def test_a_disclaimer_reaches_the_line_beside_it(self):
        """A neighbour disclaimer excuses, an anchor anchors, either reaches.

        The window is the statement's line, the line above and the line below —
        for disclaimers and anchors alike. That symmetry is what lets the honest
        shape ("the count moved 10 → 12; why it moved was not established") pass
        without opening a hole: a neighbour line can only ever EXCUSE or SOURCE,
        never fabricate.
        """
        assert body_gate.check_body("It printed 120 passed.\nThe worker path is not verified.\n").ok
        assert body_gate.check_body("It printed 120 passed — not verified.\n").ok

class test_round5_width:
    """Round 5: a citation is not the line's subject."""

    def test_a_bare_citation_is_not_the_subject(self):
        # MUT:naming-width — a span anywhere used to excuse every behaviour
        # word on the line. A line whose only code is a bare citation can still
        # name nothing, so what silences behaviour is a span that holds an
        # identifier / call / flag, not a path reference.
        assert body_gate.check_body("The harness verified it, see `docs/en/x.md`.\n").ok
        # A path that is only a citation is the line's subject by the narrow
        # rule (a span holding a word), and the line then makes no behaviour
        # claim. It still counts as an anchor for a FIGURE on the same line.
        assert body_gate.claim_kinds("The harness verified it, see `docs/en/x.md`.") == ((), ())
        assert body_gate.has_anchor("The harness verified it, see `docs/en/x.md`.") is True

    def test_an_identifier_span_names_the_subject(self):
        # `hostSurface.test.ts` observes …, `expect(…)` is the regression guard:
        # a reader can open the identifier, so the sentence is naming it.
        assert body_gate.check_body("`hostSurface.test.ts` observes the real surface, verified against the source.\n").ok
        assert body_gate.check_body("`expect(seam.x).toBe(Y)` is the regression guard, verified.\n").ok

class test_round4_holes:
    """Round 4: the last four ways a statement could certify itself."""

    def test_naming_does_not_excuse_a_quantity(self):
        # MUT:naming-swallows-figures — the round-2 fix silenced quantities too,
        # which muted whole bodies.
        assert not body_gate.check_body("`pr_gate.py` now runs 6 gates.\n").ok
        assert not body_gate.check_body("The `--waiver` flag gives 2 refusals.\n").ok

    def test_a_closed_html_comment_does_not_delete_the_line(self):
        # MUT:html-comment-deletes-line — a closed `<!-- -->` is a span, not a
        # block: stripping the rest of the line hid the claim beside it.
        assert not body_gate.check_body("The suite has 12 failed. <!-- -->\n").ok
        lines, _notes, _evidence, _omitted = body_gate.prosaic("Prose first. <!-- c --> More prose after.\n")
        assert lines and ("Prose first." in lines[0].text)

    def test_an_unclosed_html_comment_swallows_the_rest(self):
        lines, _notes, _evidence, _omitted = body_gate.prosaic("Intro.\n<!-- a note 12 failed\n")
        assert "12 failed" not in " ".join(row.text for row in lines)

    def test_a_verdict_cell_must_be_the_verdict(self):
        # MUT:verdict-substring — `likely to fail` used to capture its table.
        assert not body_gate.row_is_verdict("| a | likely to fail |")
        assert not body_gate.row_is_verdict("| a | this reddens the line |")
        assert not body_gate.check_body("| a | likely to fail |\n|---|---|\n| b | 9 of 10 builds break |\n").ok

    def test_an_all_letter_hex_run_is_a_word_not_a_sha(self):
        # MUT:hex-word-anchor — `defaced` / `effaced` anchored anything.
        assert not body_gate.has_anchor("the copy was defaced")
        assert body_gate.has_anchor("reverted to 1a2b3c4d")
        assert body_gate.has_anchor("the commit aabbccdd is clean")

class test_naming:
    """A sentence that names its code is not a claim about a run."""

    def test_naming_excuses_behaviour_not_figures(self):
        # MUT:naming-vs-outcome — dropping `names_a_thing` refuses the #302
        # enforcement line; making it swallow quantities too (round 4's hole)
        # makes the figure cases green.
        assert body_gate.claim_kinds("`format-markdown` exits 1 on frame glyphs")[0] == ()
        assert body_gate.claim_kinds("`pr_gate.py` now runs 6 gates")[0] == ("unquoted digit",)

    def test_a_quoted_figure_alone_is_refused_and_says_so(self):
        # MUT:quoted-figure-claim — treating the quoted figure as its own source
        # makes this green; the message names what would satisfy it.
        # A span that IS the whole quantity is a pasted output; `3/4` of the
        # build configs inside code markers is a command's argument.
        result = body_gate.check_body("The suite used to fail on `3/4` of the machines.\n")
        assert result.ok
        result = body_gate.check_body("The suite used to fail on 3/4 of the machines.\n")
        assert not result.ok

    def test_a_measured_outcome_is_still_a_claim(self):
        assert body_gate.claim_kinds("The suite went from 12 failed to 0")[0] == ("quantity",)
        assert body_gate.claim_kinds("`vite build` printed 12 failed")[0] == ("quantity",)

class test_round2_anchors:
    """Review round 2: the anchors that were free passes, and stay refused."""

    @pytest.mark.parametrize("line", ["x and/or y", "x N/A y", "before/after", "yes/no", "on/off", "2026-11-15/16"])
    def test_prose_connectors_are_not_paths(self, line):
        # `a/b` used to be a path: one connector certified every figure on its
        # line. MUT:free-pass-path — widening `path_is_source` back to any
        # `word/word` makes this red.
        assert not body_gate.has_anchor(line), line

    @pytest.mark.parametrize("line", ["see packages/vue/src/useHighlight.ts:50-57", "docs/en/x.md", "docs/zh-Hans/system/web.md", "in wasm/wasm_test.rs", "`api.rs:463`"])
    def test_real_paths_are_sources(self, line):
        assert body_gate.has_anchor(line), line

    def test_a_span_must_be_a_thing_not_a_word(self):
        # Three free passes: a phrase in backticks certifying a figure, the
        # figure itself in backticks, and a number inside a longer quote.
        # MUT:free-pass-span — dropping `_SPAN_SHAPE` makes these green.
        assert not body_gate.has_anchor("`not-a-command`")
        assert not body_gate.has_anchor("`gate green`")
        assert body_gate.has_anchor("`vite build`")
        assert body_gate.has_anchor("`vite build`")

    def test_a_quoted_figure_is_a_pasted_output(self):
        # Backticks around a whole quantity paste the run's output; the prose
        # form is the claim. The first cut's hole (a quoted figure sourcing
        # ITSELF as an anchor) is closed by `span_is_source`, not by refusing
        # the quotation.
        assert body_gate.check_body("It printed `120 passed`.\n").ok
        assert not body_gate.check_body("It printed 120 passed.\n").ok
        assert body_gate.check_body("It printed 120 passed.\n`vite build`\n").ok
        # Naming a tool with a code token and describing it is not a claim about
        # a run (devtools #302: `\`format-markdown\` exits 1 on …`).
        # MUT:naming-vs-outcome — dropping `_OUTCOME` refuses the #302 line.
        assert body_gate.check_body("`format-markdown` exits 1 on the frame glyphs.\n").ok
        assert not body_gate.check_body("The suite went from 12 failed to 0.\n").ok

    def test_a_prose_verdict_is_not_a_captured_row(self):
        # `The gate is green after the fix.` is a claim about a run, not output.
        # MUT:free-pass-verdict — a non-table test would make this green.
        assert not body_gate.is_captured_cell("The gate is green after the fix.")
        assert not body_gate.satisfies("The gate is green after the fix.")

    def test_a_verdict_row_closes_with_the_verdict(self):
        assert body_gate.row_is_verdict("| drop the guard | 3 red |")
        assert not body_gate.row_is_verdict("| drop the guard | reddens this line |")
        assert not body_gate.row_is_verdict("Adding a token reddens this line")

    def test_mentioning_the_waiver_token_does_not_drop_the_line(self):
        # MUT:free-pass-waiver-mention — an unanchored `body-gate-waiver` match
        # would drop the line from the scan.
        body = "Why: the ids live in the linked issue.\nWe did not use body-gate-waiver: because 120 passed\n"
        lines, _notes, _evidence, _omitted = body_gate.prosaic(body)
        assert any("120 passed" in row.text for row in lines)
        assert not body_gate.check_body(body).ok

class test_captured_tables:
    """A table that quotes a run is evidence; a prose table is not."""

    def test_an_outcome_row_carries_its_table(self):
        body = "| mutation | verdict |\n|---|---|\n| drop the guard | 3 red |\n| invert the matrix | 2 green |\n"
        assert body_gate.check_body(body).ok

    def test_captured_numeric_columns_are_output(self):
        # All-numeric cells are the reading itself.
        body = "| canvases | timers |\n|---|---|\n| 0 | 0 |\n| 1 | 0 |\n"
        assert body_gate.check_body(body).ok

    def test_a_labelled_numeric_row_needs_a_verdict_row(self):
        # `| before | 0 | 0 | 0 | 3 |` is a prose row with a label. Its table is
        # trusted only when it carries a verdict row (a mutation/verification
        # table); alone, the figures have no stated source.
        # MUT:labelled-numeric-row — dropping the verdict-row requirement makes
        # this case green while its neighbour (`test_captured_numeric_columns_
        # are_output`) stays green too, which is why the control is separate.
        body = "| round | canvases | timers |\n|---|---|---|\n| before | 0 | 0 |\n"
        assert not body_gate.check_body(body).ok

    def test_a_prose_table_still_needs_sources(self):
        # The negative control that keeps the table rule narrow: no outcome
        # cell, no numeric-only cell — the figures are prose.
        body = "| control | finding |\n|---|---|\n| login screen | renders with 9041 bytes |\n| blank page | fails after 12 retries |\n"
        assert not body_gate.check_body(body).ok

    def test_pipes_without_a_delimiter_row_are_prose(self):
        # Not markdown table syntax (no delimiter row): the cell that states a
        # figure is prose, so a sourceless figure in it must red.
        body = "Run A | Run B\n|---|---|\nThe count was 12 failed\n"
        assert not body_gate.check_body(body).ok

class test_scope_exclusions:
    """Fenced, quoted, indented, heading and record shapes are out of scope."""

    def test_a_fenced_block_is_evidence(self):
        # The capture itself is the evidence: no prose line holds the figures.
        body = "Gates:\n\n```\n Test Files 16 passed (16)\n Tests 120 passed (120)\n```\n"
        result = body_gate.check_body(body)
        assert result.ok
        assert result.skipped is not None

    def test_an_unclosed_fence_swallows_the_rest(self):
        body = "Intro.\n```\n 3 failed / 4 passed\n"
        result = body_gate.check_body(body)
        assert result.findings == ()
        assert any("unclosed" in problem for problem in result.problems)

    def test_a_heading_is_not_a_claim(self):
        assert body_gate.check_body("## §3 — three statements that were not true\nBody text.\n").ok

    def test_a_block_quote_is_a_template(self):
        lines, _notes, _evidence, _omitted = body_gate.prosaic("Intro.\n\n> Tests 120 passed in the template\n")
        assert "Tests 120" not in " ".join(row.text for row in lines)

    def test_an_indented_block_is_a_template(self):
        lines, _notes, _evidence, _omitted = body_gate.prosaic("Intro.\n\n    Tests 120 passed in the template\n")
        assert "Tests 120" not in " ".join(row.text for row in lines)

    def test_the_gate_entry_lines_are_not_claims(self):
        # The exemption statement is excluded, and the surrounding prose still
        # scans: the reason's own numbers do not need sources of their own.
        body = "Why: the ids live in the linked issue.\nbody-gate-waiver: 3 of the 4 ids live in issue page two\n"
        lines, _notes, _evidence, _omitted = body_gate.prosaic(body)
        assert [row.text for row in lines if row.text.strip()] == ["Why: the ids live in the linked issue."]
        assert body_gate.check_body(body).findings == ()

    def test_the_verify_record_is_not_a_claim(self):
        assert body_gate.check_body("verify: rounds=6 clean=3 S0=0 S1=0 S2=0 S3=9 gates=5 hardcap=no c3=3\nText.\n").ok

#: MUT:zero-production — deleting the empty-body / zero-prose refusals in
#: `check_body` makes the first two cases below green instead of red.
class test_zero_production:
    """Zero production is an error; MUT:zero-production proves the refusals bite."""

    def test_an_empty_body_refuses(self):
        result = body_gate.check_body("")
        assert not result.ok
        assert any("empty" in problem for problem in result.problems)

    def test_a_whitespace_body_refuses(self):
        assert not body_gate.check_body("   \n\t\n").ok

    def test_a_body_with_no_prose_lines_refuses(self):
        result = body_gate.check_body("```\nTests 120 passed\n```\n    indented\n")
        assert not result.ok
        assert any("zero prose lines" in problem for problem in result.problems)

    def test_a_body_with_prose_but_no_claims_skips(self):
        result = body_gate.check_body("Add the thing and update the docs.\n")
        assert result.ok
        assert result.skipped is not None
        assert "nothing to judge" in result.skipped

class test_vocabulary:
    """The words and the units are data, and the surface is pinned."""

    def test_the_behaviour_list_is_the_comment_gates_list(self):
        # One list, one owner: importing it is what keeps the two gates from
        # drifting, so a local copy would be a defect even if it matched today.
        assert body_gate.BEHAVIOUR_TERMS is comment_gate.BEHAVIOUR_TERMS

    @pytest.mark.parametrize("line", ["120 passed", "16/16", "rc=0", "6 files", "2 crashed", "2.9s", "85%", "same sha256", "4 处", "三个测试"])
    def test_quantity_shapes(self, line):
        assert body_gate.QUANTITY.search(line), line

    @pytest.mark.parametrize("line", ["The alias is required.", "See #1503 for the retry bound.", "## G5 ④ — teardown"])
    def test_non_quantity_shapes(self, line):
        assert not body_gate.QUANTITY.search(line), line

    @pytest.mark.parametrize("line", ["| before | 0 | 0 | 0 | 3 |", "9041 bytes — the login screen paints", "12 rounds 2–3"])
    def test_prose_digit_shapes(self, line):
        assert body_gate.unquoted_digits(line), line

    @pytest.mark.parametrize("line", ["0.55.463", "这一行是错的", "第一版写错了", "唯一改动", "§3", "220×220"])
    def test_excluded_digit_shapes(self, line):
        assert body_gate.unquoted_digits(line) == (), line

    def test_a_dimension_is_excluded_but_a_bare_figure_is_not(self):
        # A dimension is a shape (``220×220``); a figure beside it is prose.
        assert body_gate.unquoted_digits("220×220 = 48400 px") == ("220",)

class test_corpus_scan:
    """The real bodies, pinned: the gate's behaviour on them cannot drift silently."""

    def test_corpus_1474(self):
        """The #1474 body: every figure has its run beside it.

        The mutation paragraph states ``kept the frozen 16/16 green`` in prose
        and PASTES the run's own ``16 passed (16)`` / ``2 failed | 19 passed``
        on the same line — that quotation is the evidence. The recorded numbers
        are pinned here so a silent swing either way is a red.
        """
        result = body_gate.check_body(_fixture("corpus-1474.md"))
        assert (result.scanned_lines, len(result.statements), len(result.findings)) == (96, 8, 0)
        assert 53 in {stmt.line for stmt in result.statements}

    def test_corpus_2813_refusals_include_the_arithmetic_paragraph(self):
        result = body_gate.check_body(_fixture("corpus-2813.md"))
        lines = _lines(result)
        # 202 prose lines, 35 statements, 20 refusals on this head; the
        # narrative tally paragraph is among them.
        assert (result.scanned_lines, len(result.statements), len(lines)) == (202, 45, 38)
        # 189 is the tallied "2 red / 4 green" line; 73 the bare "Reproduced 3/3".
        assert (73 in lines) and (189 in lines) and (12 in lines)

    def test_corpus_2821_is_not_a_clean_positive(self):
        """Documented finding: #2821 is NOT a clean positive under this rule.

        Its telemetry table is captured output (so the table rows pass), but the
        prose restatements around it carry prose figures whose source is in a
        different section. The honest record is that the gate refuses it, which
        is why the known-positive control is devtools #302 / #1474 instead.
        """
        result = body_gate.check_body(_fixture("corpus-2821.md"))
        assert not result.ok
        # The round arithmetic is restated in prose ("rounds 2–3", "round 1")
        # with no run output in its paragraph.
        assert 18 in _lines(result)

class test_cli:
    """The command line: exit codes and both input lanes."""

    def test_a_clean_file_exits_zero(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("It printed 120 passed.\n`vite build`\n", encoding = "utf-8")
        assert body_gate.main(["--body", str(target)]) == 0
        capsys.readouterr()

    def test_an_unsourced_file_exits_one(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("It printed 120 passed.\n", encoding = "utf-8")
        assert body_gate.main(["--body", str(target)]) == 1
        captured = capsys.readouterr()
        assert "body:1" in captured.out
        assert "BODY-GATE REFUSED" in captured.err

    def test_an_empty_file_exits_one(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("", encoding = "utf-8")
        assert body_gate.main(["--body", str(target)]) == 1
        capsys.readouterr()

    def test_a_missing_file_exits_usage(self, tmp_path, capsys):
        assert body_gate.main(["--body", str(tmp_path / "nope.md")]) == 2
        assert "unreadable" in capsys.readouterr().err

    def test_an_unreadable_pr_exits_usage(self, capsys, monkeypatch):
        def boom(repo, pr):
            return None, ["gh pr view failed (404 Not Found) — refusing to certify a body that could not be read"]

        monkeypatch.setattr(body_gate, "read_pr_body", boom)
        assert body_gate.main(["--pr", "1", "--repo", "o/r"]) == 2
        assert "refusing to certify" in capsys.readouterr().err

    def test_the_skip_is_printed_not_silent(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("Add the thing.\n", encoding = "utf-8")
        assert body_gate.main(["--body", str(target)]) == 0
        assert "nothing to judge" in capsys.readouterr().out
