#!/usr/bin/env python3
"""Tests for the PR-body prose gate (``celestia_devtools.lint.body_gate``).

The gate exists because eight PRs were merged through the recorded-exception
channel with **correct code and a wrong sentence in the body** (hikari #1476 /
#1478 / #1482 / #1485 / #1503 / #1506, chest #2813 / #2815, tairitsu #154).
The comment gate (#306) covers test-file comments; nothing covered body prose.

Every case here is a mechanical control against a real corpus, checked in under
``tests/fixtures/body_gate/`` exactly as the bodies were published:

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

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "body_gate"

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding = "utf-8")

def _lines(result) -> list[int]:
    return [stmt.line for stmt in result.findings]

class test_positive_controls:
    """Bodies that must pass: real merged PRs, plus the shapes the rule allows."""

    def test_an_unanchored_behaviour_claim_is_refused(self):
        """devtools #304 is a NEGATIVE, and a useful one.

        It states the observed failure in prose (``just >= 1.36 makes duplicate
        definitions a hard parse error … the moment ``.just/`` was staged
        (observed on wowsp: just dev refused to start)``) with no command, no
        ``file:line`` and no ``MUT:``. A maintainer can read that as history,
        but this gate cannot check it — and saying so is the point. The
        positive controls are #303 / #305 (which state nothing) and #302 (which
        anchors every claim).
        """
        result = body_gate.check_body(_fixture("pr-304.md"))
        assert not result.ok
        assert result.findings and (result.findings[0].kinds == ("behaviour",))

    @pytest.mark.parametrize("name", ["pr-303.md", "pr-305.md"])
    def test_short_descriptive_bodies_pass_on_adjacent_evidence(self, name):
        # #303 / #305 each make one claim and carry its command on the next
        # line: a pass, with the claim visible in `statements` (not a skip).
        result = body_gate.check_body(_fixture(name))
        assert result.ok, [stmt.render() for stmt in result.findings]
        assert result.statements

    def test_the_mermaid_body_is_prose_with_claims_and_passes(self):
        # devtools #302: eight quantitative/behaviour statements, every one
        # anchored or in a captured table — the shape the gate is meant to
        # incentivise rather than punish.
        result = body_gate.check_body(_fixture("pr-302.md"))
        assert result.ok, [stmt.render() for stmt in result.findings]
        assert len(result.statements) >= 5

    def test_a_disclaimed_figure_passes(self):
        # The disclaimer must be on the figure's own line or the line beside
        # it — and the line beside it has to be a disclaimer, not an anchor.
        body = "The DOM count moved from 10 to 12 between rounds.\nThe cause of that shift was **not established**.\n"
        assert body_gate.check_body(body).ok
        assert body_gate.check_body("The DOM count moved from 10 to 12 between rounds (inference).\n").ok

    def test_a_figure_next_to_its_command_passes(self):
        body = "Gates:\n\n```\nvite build → rc=0\n```\n\nThe build printed `120 passed` and the exit code was `rc=0`.\n"
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
        assert _lines(result) == [6, 7, 8]

    def test_the_same_restatement_with_a_disclaimer_passes(self):
        # The disclaimer goes where the figures are: on or beside them — the
        # window is one line, so an addendum far below the paragraph does not
        # reach it (and must not: it would excuse every paragraph above it).
        body = _fixture("negative-2821-dom-counts.md")
        disclaimed = body.replace("  12 in rounds 2-3; post-unmount = 11 every round.", "  12 in rounds 2-3; post-unmount = 11 every round. Why `before` moves is an inference.")
        assert disclaimed != body
        # One disclaimer line covers the figures one line ABOVE or BELOW it, not
        # a whole paragraph: the paragraph head keeps its own finding.
        assert _lines(body_gate.check_body(disclaimed)) == [6]

    def test_an_unsourced_quantity_is_refused(self):
        assert not body_gate.check_body("The suite went from 12 failed to 0 over the cut.\n").ok

    def test_an_unsourced_behaviour_claim_is_refused(self):
        result = body_gate.check_body("The new pin catches the reload every time.\n")
        assert not result.ok
        assert result.findings[0].kinds == ("behaviour",)

    def test_the_window_is_one_line_up(self):
        # The anchor on the line above binds …
        assert body_gate.check_body("```\nvite build → rc=0\n```\nIt printed 120 passed.\n").ok
        # … and the line above THAT does not.
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
        lines, _notes, _evidence = body_gate.prosaic("Intro.\n\n> Tests 120 passed in the template\n")
        assert "Tests 120" not in " ".join(row.text for row in lines)

    def test_an_indented_block_is_a_template(self):
        lines, _notes, _evidence = body_gate.prosaic("Intro.\n\n    Tests 120 passed in the template\n")
        assert "Tests 120" not in " ".join(row.text for row in lines)

    def test_the_gate_entry_lines_are_not_claims(self):
        # The exemption statement is excluded, and the surrounding prose still
        # scans: the reason's own numbers do not need sources of their own.
        body = "Why: the ids live in the linked issue.\nbody-gate-waiver: 3 of the 4 ids live in issue page two\n"
        lines, _notes, _evidence = body_gate.prosaic(body)
        assert [row.text for row in lines if row.text.strip()] == ["Why: the ids live in the linked issue."]
        assert body_gate.check_body(body).findings == ()

    def test_the_verify_record_is_not_a_claim(self):
        assert body_gate.check_body("verify: rounds=6 clean=3 S0=0 S1=0 S2=0 S3=9 gates=5 hardcap=no c3=3\nText.\n").ok

class test_zero_production:
    """Zero production is an error, never a silent pass."""

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
        assert body_gate.unquoted_digits("220×220 = 48400 px") == ("220", "48400")

class test_corpus_scan:
    """The real bodies, pinned: the gate's behaviour on them cannot drift silently."""

    def test_corpus_1474_passes(self):
        result = body_gate.check_body(_fixture("corpus-1474.md"))
        assert result.ok, [stmt.render() for stmt in result.findings]
        assert result.statements, "the 1474 body states plenty; an empty scan would mean a broken extractor"

    def test_corpus_2813_refusals_include_the_arithmetic_paragraph(self):
        result = body_gate.check_body(_fixture("corpus-2813.md"))
        lines = _lines(result)
        # The narrative restatement of the mutation tally (its only run output
        # lives in the Evidence block above) and the prose figures elsewhere.
        assert 189 in lines
        assert len(lines) >= 5

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
        target.write_text("It printed `120 passed`.\n", encoding = "utf-8")
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
