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

    def test_a_span_mid_sentence_sources_only_the_thing_it_names(self):
        # MUT:span-subject — review round 4 replayed hikari #1476's body: a
        # backticked `.toEqual(...)` appeared mid-sentence and sourced the whole
        # claim. A span sources a FIGURE beside it; it does not make a claim
        # about itself evidenced.
        claim = "The file header claimed the frozen golden asserted `toEqual(QR_CAPABILITIES)` — it never referenced it."
        result = body_gate.check_body(claim + "\n")
        assert not result.ok
        assert result.findings

    def test_a_span_sources_a_claim_it_does_not_exempt_it(self):
        # MUT:naming-exemption — the first cut let a span exempt the whole line.
        # A span is one way to SOURCE a claim (beside MUT:, evidence:, file:line);
        # the claim itself is still decided by the vocabulary and the figures.
        assert body_gate.check_body("The armed path was never exercised (`x.test.tsx:88`).\n").ok
        assert not body_gate.check_body("The armed path was never exercised (`a b`).\n").ok
        assert not body_gate.check_body("The mock inverted the armed path so it always failed.\n").ok
        # A line with no vocabulary and no figure is not a claim at all.
        assert body_gate.check_body("`just` >= 1.36 makes a duplicate a hard parse error.\n").ok

class test_span_evidence:
    """A span names its subject; MUT:span-attribution pins that it is no evidence."""

    def test_a_quoted_call_does_not_evidence_an_attribution(self):
        # MUT:span-attribution — hikari #1476 quotes `toEqual(...)` and then
        # asserts it was never called; the quotation proves the call exists.
        claim = "The header asserted `toEqual(QR_CAPABILITIES)` — it never referenced it."
        assert not body_gate.check_body(claim + "\n").ok

    def test_a_span_still_sources_a_figure_and_a_claim_about_its_thing(self):
        assert body_gate.check_body("The suite printed `120 passed`.\n").ok
        assert body_gate.check_body("The armed path ran (`x.test.tsx:88`).\n").ok
        assert body_gate.check_body("The armed path was never skipped (`x.test.tsx:88`).\n").ok

    def test_a_coverage_claim_without_a_source_is_refused(self):
        assert not body_gate.check_body("The mock was inverted, so the armed path was never exercised.\n").ok
        assert not body_gate.check_body("The fault fixture is always-failing, which is why the case passes.\n").ok

    def test_the_span_rule_uses_the_comment_gates_own_vocabulary(self):
        # MUT:span-attribution — a hand-transcribed subset of BEHAVIOUR_TERMS
        # (the first cut kept 9 of 15) must fail here. Each sentence carries an
        # attribution word AND a span that IS a source (`vite build` is a
        # command shape), so only the full list can refuse it; the first cut of
        # this test used ` `x` `, which is not a source, and passed either way.
        for text in ["The gate doesn't flag `vite build` output.", "这条规则抓不住 `setup.py` 那种写法。", "这个只在 `config.toml` 里出现。", "只有 `vite build` 时才红。"]:
            assert not body_gate.check_body(text + "\n").ok, text

    def test_the_vocabulary_is_reached_through_the_comment_gate(self, monkeypatch):
        # MUT:span-attribution — the delegation is what keeps the two gates on
        # one list; a local copy would ignore the sentinel.
        sentinel = ("does not",)

        def fake(_text):
            return sentinel

        monkeypatch.setattr(comment_gate, "behaviour_terms", fake)
        assert body_gate.behaviour_terms("anything") == sentinel

    def test_citing_a_test_file_does_not_evidence_a_behaviour_claim(self):
        # MUT:test-ref-span — a bare `(x.test.ts)` proves the file exists, not
        # that the path was never exercised. Only a case pointer does more.
        for text in ["The armed path was never exercised", "The armed path was never exercised (`x.test.ts`)", "The armed path was never exercised, per foo.test.z", "The armed path was never exercised (README.test.md)"]:
            assert not body_gate.check_body(text + "\n").ok, text

        assert body_gate.check_body("The armed path was never exercised (`x.test.ts:88`).\n").ok

    # MUT:bare-test-name — `span_is_source`'s shape regex lets a flag-like word
    # and an underscored identifier through, so a bare test FILE name has to be
    # refused before the shape test. Each case below is its own test so a
    # failure names the shape that broke.
    @pytest.mark.parametrize("name", ["test.ts", "spec.ts", "x.test.ts", "y_test.rs", "dir/test.ts", "a/test.py", "tests/test_body_gate.py", "src/a.user.test.ts", "api.user.test.ts", "user.component.test.tsx", "tests/fixtures/x/useHighlight.coldPath.test.ts", "test.tsx", "x.test.tsx", "dir/test.pyi", "test_util.ts"])
    def test_a_bare_test_file_name_is_not_a_source(self, name):
        assert not body_gate.span_is_source(name), name

    @pytest.mark.parametrize("name", ["x.test.ts:88", "a.py::test_b", "vite build", "api.rs:463", "contest.ts", "mytest.py", "tests/__tests__/x.ts"])
    def test_a_case_pointer_or_command_is_still_a_source(self, name):
        assert body_gate.span_is_source(name), name

    def test_citing_a_test_file_sources_nothing(self):
        # No behaviour word here: only the span branch can decide it.
        assert not body_gate.check_body("See `tests/test_body_gate.py` for coverage (12 checks).\n").ok
        assert not body_gate.check_body("See `x.test.ts` for coverage (12 checks).\n").ok
        assert body_gate.check_body("See `x.test.ts:88` for coverage (12 checks).\n").ok

    def test_a_version_still_names_its_line(self):
        assert body_gate.check_body("`just` >= 1.36 makes duplicate definitions a hard parse error.\n").ok

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

class test_the_escape_hatches_are_not_buttons:
    """A disclaimer and a directive are only valid in their honest form."""

    def test_a_negated_disclaimer_is_not_a_disclaimer(self):
        # MUT:negated-disclaimer — a substring test read "Nothing here is
        # unverified" and "未未复算" as disclaimers; both say the opposite.
        assert not body_gate.check_body("The suite printed 120 passed and 0 failed. Nothing here is unverified.\n").ok
        assert not body_gate.check_body("测试跑了 120 passed，未未复算。\n").ok
        assert body_gate.check_body("The suite printed 120 passed (unverified).\n").ok
        assert body_gate.check_body("测试跑了 120 passed，未复算。\n").ok

    def test_a_negation_in_the_previous_clause_does_not_reach(self):
        # MUT:negation-scope — a 40-character lookbehind turned the corpus row
        # `but not round 1, so it was an inference` into "no disclaimer", and
        # that false positive was pinned as a golden value (8 -> 9).
        assert body_gate.disclaims("rule fits rounds 2-3 but not round 1, so it was an inference fitted to one")
        assert body_gate.disclaims("rule fits rounds 2-3, so it was an inference fitted to one")

    def test_a_negation_that_attaches_does_flip_the_term(self):
        assert not body_gate.disclaims("Nothing here is unverified.")
        assert not body_gate.disclaims("This was not an inference.")
        assert not body_gate.disclaims("This is 并非 inference 结论。")
        assert not body_gate.disclaims("测试跑了 120 passed，未未复算。")

    def test_the_clause_break_is_what_scopes_the_negation(self):
        # MUT:negation-scope — drop the clause break and both of these flip,
        # because a negation two clauses away starts counting.
        assert not body_gate.disclaims("that was not inference")
        assert body_gate.disclaims("that was not round 1, so it was an inference")
        assert body_gate.disclaims("neither was it round one; the label is an inference")
        assert body_gate.disclaims("it was not round one, an inference was drawn")

    def test_a_comma_between_the_negation_and_the_term_scopes_it(self):
        # NOTE: this case does not distinguish the clause-break mutant — with or
        # without the stop the three-word walk never reaches across the comma
        # for these inputs. It pins the BEHAVIOUR (a comma scopes the negation)
        # but not that one line of the implementation. MUT:negated-disclaimer
        # above is the mutant that bites on this file; recorded, not dressed up.
        assert not body_gate.disclaims("that was not an inference")
        assert body_gate.disclaims("x not, y inference")
        assert body_gate.disclaims("not, y inference")
        assert body_gate.disclaims("that was not, an inference")

    def test_a_reason_without_substance_is_not_a_reason(self):
        # MUT:directive-substance — 20 dots printed a `🔓` and exempted.
        for filler in (".", "_"):
            filler_block = filler * 20

            assert not body_gate.check_body("It printed 120 passed. body-gate-ok: " + filler_block + "\n").ok

        assert body_gate.check_body("It printed 120 passed. body-gate-ok: the log lives in the linked issue\n").ok

    def test_a_negated_checksum_word_is_not_a_locator(self):
        assert not body_gate.check_body("We printed 120 passed and no sha256 is shown.\n").ok
        assert not body_gate.check_body("The sha256 is not shown; we printed 120 passed.\n").ok
        assert body_gate.check_body("We printed 120 passed; sha256 is in the log.\n").ok

class test_a_number_only_row:
    def test_a_single_cell_number_row_is_a_statement(self):
        # A row that is nothing but a number has no label column, so it is not
        # a readings row: it states a figure like any other line.
        body = "Intro.\n\n| 42 |\n"
        result = body_gate.check_body(body)
        assert not result.ok
        assert any("quantity" in stmt.kinds for stmt in result.findings)

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
class test_zero_statement_floor:
    """MIN_PROSE_LINES — a long body with no statement is a broken scan."""

    def test_a_short_description_is_a_skip(self):
        result = body_gate.check_body("Add the thing and update the docs.\n")
        assert result.ok
        assert result.skipped is not None

    def test_the_floor_itself_is_the_boundary(self):
        # MUT:min-prose-lines — the floor binds AT `MIN_PROSE_LINES`, not above
        # it; a +5 slack let a future tightening of the floor pass silently.
        line = "This sentence describes the change with no figure."
        assert body_gate.check_body("\n".join([line] * (body_gate.MIN_PROSE_LINES - 1)) + "\n").ok
        assert not body_gate.check_body("\n".join([line] * body_gate.MIN_PROSE_LINES) + "\n").ok

    def test_a_long_body_with_no_statement_refuses(self):
        # MUT:min-prose-lines — deleting the floor makes this a silent skip.
        body = "\n".join(["This sentence describes the change with no figure."] * (body_gate.MIN_PROSE_LINES + 5))
        result = body_gate.check_body(body)
        assert not result.ok
        assert result.skipped is None

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

    @pytest.mark.parametrize(("name", "sentences", "refused"), [("corpus-1474", 25, 6), ("corpus-2813", 67, 32), ("corpus-2821", 20, 8)])
    def test_the_corpus_rows(self, name, sentences, refused):
        result = body_gate.check_body(_fixture(name + ".md"))
        assert (len(result.statements), len(result.findings)) == (sentences, refused)

    def test_the_2813_tally_paragraph_is_refused(self):
        refused = _lines(body_gate.check_body(_fixture("corpus-2813.md")))
        assert 73 in refused
        assert any(186 <= line <= 220 for line in refused)

    def test_a_restatement_with_a_table_beside_it_passes(self):
        # `dom-arithmetic.md`: three sentences restating the telemetry table
        # above them, and the table is one line away — a restatement is sourced
        # by the capture it restates. (The file used to be named
        # `negative-...` while passing; the name was the lie, not the body.)
        result = body_gate.check_body(_fixture("dom-arithmetic.md"))
        assert result.ok
        assert len(result.statements) == 3

    def test_a_clean_body_passes(self):
        assert body_gate.check_body(_fixture("pr-302.md")).ok

class test_cli:
    """The command line: exit codes and both input lanes."""

    def test_a_clean_file_exits_zero(self, tmp_path, capsys):
        target = tmp_path / "body.md"
        target.write_text("It printed 120 passed.\n`vite build` → rc=0\n", encoding = "utf-8")
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
