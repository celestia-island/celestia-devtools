#!/usr/bin/env python3
"""Tests for the PR-body prose gate (``celestia_devtools.lint.body_gate``).

The gate exists because the recorded-exception PRs of this wave shipped
**correct code and prose review kept refuting** (hikari #1476 / #1478 / #1482 /
#1485 / #1503 / #1506, chest #2813 / #2815, tairitsu #154). The comment gate
covers test-file comments; nothing covered body prose.

*The scope is deliberately small* (see the module docstring): three rules a
reviewer can check by reading them — a quantity needs a source, a behaviour
claim needs a source, and a captured table is a table of readings — plus two
honest ways out (a disclaimer, or the printed ``body-gate-ok:`` directive).
Earlier revisions tried to decide every prose shape and five review rounds
found eleven free passes inside that question; the tests below are the three
rules, the two exits, and the boundary.

Controls, each with the mutation that proves it bites:

* :func:`test_a_source_satisfies` / :func:`test_a_disclaimer_satisfies` — the
  rule fires on a known body and the source/disclaimer stops it;
* :func:`test_the_corpus_rows` — the published bodies, verbatim, with their
  counts pinned (MUT:corpus-pin — a change on either side reds it);
* :func:`test_a_table_of_readings_is_captured` / :func:`test_a_prose_table_is_
  judged` — the table boundary from both sides;
* :func:`test_the_directive_needs_a_reason` — the escape hatch is not a mute
  button;
* :func:`test_zero_production` — empty / record-only / all-excluded bodies are
  refused, and a body that states nothing is a loud skip.
"""

from __future__ import annotations
from pathlib import Path
import pytest
from celestia_devtools.lint import body_gate, comment_gate

#: The corpus lives outside the ``tests/`` tree on purpose: the comment gate
#: treats a ``tests`` path segment as a test tree and reads ``.md`` line starts
#: as comment blocks. It is corpus prose, not a comment block.
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "body_prose"

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding = "utf-8")

def _lines(result) -> list[int]:
    return [stmt.line for stmt in result.findings]

class test_the_three_rules:
    """A quantity needs a source; a behaviour claim needs a source."""

    def test_an_unsourced_quantity_is_refused(self):
        # MUT:no-source-rule — deleting `claim_kinds`' quantity arm makes it pass.
        assert not body_gate.check_body("The suite went from 12 failed to 0.\n").ok

    def test_an_unsourced_behaviour_claim_is_refused(self):
        assert not body_gate.check_body("The new pin catches the reload.\n").ok

    def test_a_source_satisfies(self):
        assert body_gate.check_body("The suite went from 12 failed to 0 (`vite build`).\n").ok
        assert body_gate.check_body("The new pin catches the reload (`a.test.ts:12`).\n").ok
        assert body_gate.check_body("The counts match (MUT:reload-dropped).\n").ok

    def test_a_disclaimer_satisfies(self):
        assert body_gate.check_body("The count moved from 10 to 12 — not re-run.\n").ok
        assert body_gate.check_body("The count moved from 10 to 12（未复算）。\n").ok

    def test_a_nearby_source_satisfies(self):
        # MUT:window-one-line — widening or shortening the window changes these.
        assert body_gate.check_body("`vite build` → rc=0\nIt printed 120 passed.\n").ok
        assert not body_gate.check_body("It printed 120 passed.\n\n\n`vite build` → rc=0\n").ok

    def test_a_line_that_names_code_is_not_a_claim(self):
        # MUT:naming — devtools #304 names `just`, `allow-duplicate-variables`
        # and `.just/` and asserts no outcome; refusing it costs an honest body.
        assert body_gate.check_body("`just` >= 1.36 makes a duplicate a hard parse error.\n").ok
        # A pasted figure beside the command that printed it is the run itself
        # (the quotation is the evidence) — see `test_a_source_satisfies`.
        assert body_gate.check_body("`vite build` printed `12 failed`.\n").ok

class test_tables:
    """A captured table is a table of readings, and nothing more."""

    def test_a_table_of_readings_is_captured(self):
        body = "| repo | PR | refused |\n|---|---|---|\n| devtools | #302 | 0 |\n"
        assert body_gate.check_body(body).ok

    def test_a_prose_table_is_judged(self):
        # MUT:reading-row — treating any row as a reading silences the prose.
        body = "| control | finding |\n|---|---|\n| login screen | renders with 9041 bytes |\n"
        assert not body_gate.check_body(body).ok

    def test_a_headerless_grid_is_not_a_reading_row(self):
        # The first cell is the row's label in a readings table; a grid with no
        # label column and no header is prose (MUT:reading-row-label).
        assert body_gate.is_reading_row("| before | 0 | 0 | 0 | 3 |")
        assert not body_gate.is_reading_row("| control | renders with 9041 bytes |")

class test_the_directive:
    """``body-gate-ok:`` is the printed, argument-bearing escape hatch."""

    def test_a_visible_reason_at_the_floor_is_honoured(self):
        # MUT:directive-floor-edge — the comment-close hedge must apply only to
        # a hidden directive; a visible `>` at the floor is the author's.
        body = "It printed 120 passed. body-gate-ok: " + ("y" * 19) + ">"
        assert body_gate.check_body(body + "\n").ok

    def test_a_refusal_never_prints_a_skip(self):
        # MUT:skip-vs-refusal — `ok=False` and a skip line in one report makes
        # the merge log say both "nothing to judge" and "refused".
        result = body_gate.check_body("Add the thing. <!-- body-gate-ok: the log lives in the issue page two -->\n")
        assert not result.ok
        assert result.skipped is None

    def test_the_directive_needs_a_reason(self):
        # MUT:directive-floor — a thin reason must suppress nothing.
        assert not body_gate.check_body("It printed 120 passed. body-gate-ok: ok\n").ok
        assert body_gate.check_body("It printed 120 passed. body-gate-ok: the raw log lives in the linked issue\n").ok

    def test_the_directive_is_printed(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("It printed 120 passed. body-gate-ok: the raw log lives in the linked issue\n", encoding = "utf-8")
        assert body_gate.main(["--body", str(target)]) == 0
        assert "body-gate-ok" in capsys.readouterr().out

class test_the_directive_is_never_silent:
    def test_a_directive_in_an_html_comment_is_reported_not_honoured(self):
        # MUT:directive-printed — a hidden directive must be neither silent nor
        # an invisible exemption: it is printed AND the claim is still refused.
        body = "It printed 120 passed. <!-- body-gate-ok: the log is elsewhere, page two -->\n"
        result = body_gate.check_body(body)
        assert not result.ok
        assert result.directives and (len(result.directives[0][1]) >= body_gate.DIRECTIVE_REASON_MIN)
        assert any("does not exempt" in problem for problem in result.problems)

    def test_a_hidden_directive_does_not_reach_a_neighbour(self):
        body = "The suite went from 12 failed to 0.\n<!-- body-gate-ok: the log lives in the linked issue page two -->\n"
        assert not body_gate.check_body(body).ok

class test_scope_exclusions:
    """Fenced, quoted, indented, heading and record shapes are out of scope."""

    def test_a_fence_sources_a_figure(self):
        assert body_gate.check_body("Gates:\n\n```\nvite build → rc=0\n```\n").ok

    def test_a_heading_is_not_a_claim(self):
        # A body of headings only has no prose, which is a refusal (zero
        # production), not a skip. MUT:zero-production pins the refusal.
        assert body_gate.check_body("## §3 — three statements that were not true\nAdd the thing.\n").ok
        assert not body_gate.check_body("## §3 — three statements that were not true\n").ok

    def test_the_gate_entry_lines_are_not_claims(self):
        assert body_gate.check_body("Add the thing.\nbody-gate-waiver: the ids live in the linked issue, page two of it\n").ok

    def test_the_verify_record_is_not_a_claim(self):
        assert body_gate.check_body("Add the thing.\nverify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no c3=3\n").ok

class test_zero_production:
    """Zero production is an error; a body that states nothing is a loud skip."""

    def test_an_empty_body_refuses(self):
        # MUT:zero-production — deleting the guards makes these green.
        assert not body_gate.check_body("").ok

    def test_a_record_only_body_refuses(self):
        assert not body_gate.check_body("verify: rounds=4 clean=3 S0=0 S1=0 S2=0 S3=6 gates=5 hardcap=no c3=3\n").ok

    def test_a_body_with_no_claims_skips(self):
        result = body_gate.check_body("Add the thing and update the docs.\n")
        assert result.ok
        assert result.skipped is not None

#: MUT:naming — this pin catches the naming rule being dropped: the test
#: `test_a_line_that_names_code_is_not_a_claim` goes red without it.
class test_vocabulary:
    """The words and the units are data, and the surface is pinned."""

    def test_the_behaviour_list_is_the_comment_gates_list(self):
        assert body_gate.BEHAVIOUR_TERMS is comment_gate.BEHAVIOUR_TERMS

    @pytest.mark.parametrize("line", ["120 passed", "16/16", "rc=0", "6 files", "2.9s", "3.3 MB", "174 bytes", "85%", "same sha256", "4 处", "三个测试"])
    def test_quantity_shapes(self, line):
        assert body_gate.QUANTITY.search(line), line

    @pytest.mark.parametrize("line", ["3 of 4", "10 in round 1", "2 → 0 → 2", "counted 12 times"])
    def test_prose_figure_shapes(self, line):
        assert body_gate.prose_figures(line), line

    @pytest.mark.parametrize("line", ["220×220", "0.55.463", "8/310", "exits 1", "这一行", "§3"])
    def test_excluded_figure_shapes(self, line):
        assert body_gate.prose_figures(line) == (), line

class test_corpus:
    """The published bodies, pinned: the gate's behaviour cannot drift silently."""

    @pytest.mark.parametrize(("name", "sentences", "refused"), [("corpus-1474", 8, 0), ("corpus-2813", 36, 20), ("corpus-2821", 8, 4)])
    def test_the_corpus_rows(self, name, sentences, refused):
        result = body_gate.check_body(_fixture(name + ".md"))
        assert (len(result.statements), len(result.findings)) == (sentences, refused)

    def test_the_2813_tally_paragraph_is_refused(self):
        assert 189 in _lines(body_gate.check_body(_fixture("corpus-2813.md")))
        assert 73 in _lines(body_gate.check_body(_fixture("corpus-2813.md")))

    def test_a_clean_body_passes(self):
        assert body_gate.check_body(_fixture("pr-302.md")).ok

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

    def test_a_missing_file_exits_usage(self, tmp_path, capsys):
        assert body_gate.main(["--body", str(tmp_path / "nope.md")]) == 2
        assert "unreadable" in capsys.readouterr().err
