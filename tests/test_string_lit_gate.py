# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for ``celestia_devtools.lint.string_lit_gate``.

The gate exists because a 2026-11-24 review round told an implementer to
"collapse the runs of spaces" in four user-facing RPC error strings; the
implementer inverted the operation, so the runtime strings gained a bare
newline plus indentation, and ``cargo fmt`` / ``cargo clippy`` / 1176 tests all
passed. The tests below pin three things and nothing else:

1. **the two defect shapes are caught** — a real newline in the middle of a
   sentence, and a trailing space/tab at end of line, both asserted by
   ``file:line:column``;
2. **legitimate multi-line Rust is not** — escaped continuations, raw strings,
   whole-line template bodies, embedded documents and the plain-format floors
   that the org corpus is full of;
3. **the checker refuses to look clean when it saw nothing** — zero files, or a
   scan whose lanes swallowed everything, is a usage error rather than a green.

Fixture text is built from ``chr(92)`` / ``<sp>`` markers where a literal
backslash or trailing space would be invisible or ambiguous in this file's own
source — the same reason ``test_separator_lint`` builds its separators from
``"=" * N``.
"""

from __future__ import annotations
import sys
from pathlib import Path

# Allow running the tests against a source checkout without a pip install.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.core import cli  # noqa: E402
from celestia_devtools.lint import string_lit_gate as gate  # noqa: E402

BS = chr(92)
NL = chr(10)
SP = chr(32)

#: A plain string literal whose text is interrupted by a real newline — the
#: exact defect shape. ``\\\\`` (two backslashes) before the newline is the
#: ``\n`` ESCAPE written literally, which renders as one.
MIXED_ESCAPE = '            "the enquiry was not admitted ' + BS + BS + NL
MIXED_ESCAPE_TAIL = '             so the pick can be retried";' + NL

#: The same sentence with NO escape: a real newline reaches the runtime value.
BARE_NEWLINE = '    "line one' + NL + '    line two";' + NL

#: A second bare-newline shape: three lines, so it cannot be read as a fragment
#: that merely ends on punctuation.
BARE_NEWLINE_MULTI = '    "first' + NL + '    second' + NL + '    third";' + NL

#: A trailing space at end of a code line, and a trailing tab inside a string.
TRAILING_SPACE = "let x = 1;" + SP + SP + SP + NL
TRAILING_TAB = "let s = " + '"text   ' + chr(9) + NL + '   more";' + NL

#: Legitimate multi-line Rust. Every shape here appears in the org corpus, or is
#: the honest continuation form: an escaped continuation, a raw string, a hashed
#: raw string, a byte string, SQL, a markdown front matter, a format call split
#: across lines, a char literal, and a comment that merely names the hazard.
LEGAL_TEMPLATE = """let one = "a {bs}
    b {bs}
    c";
let raw = r"a
b";
let hashed = r#"say "hi"
and bye"#;
let byte_str = b"x
y";
let sql = "SELECT a
  FROM t";
let md = "# title

body";
let l = ["a",
"b"];
// a comment that mentions newline and trailing space but is code-adjacent
let quoted = '{quote}';
"""

LEGAL = LEGAL_TEMPLATE.replace("{bs}", "\\").replace("{quote}", "'")

def _scan(text: str, name: str = "fixture.rs") -> list:
    return gate.scan_text(text, name)

def _positions(findings: list) -> list:
    return [(item.line, item.column, item.kind) for item in findings]

class test_selftest:
    def test_selftest_passes(self, capsys):
        assert gate.main(["--selftest"]) == 0
        assert "selftest OK" in capsys.readouterr().out

class test_positive_control_reports_both_classes:
    """Every defect shape, with its position, in both classes."""

    def test_bare_newline_is_reported(self):
        findings = _scan(BARE_NEWLINE)
        assert _positions(findings) == [(1, 5, gate.NEWLINE)]

    def test_multi_line_bare_newline_is_reported(self):
        # Three lines is still a sentence split by real newlines, not a document:
        # the whole rendered value is the four concatenated words.
        findings = _scan(BARE_NEWLINE_MULTI)
        assert [(item.line, item.kind) for item in findings] == [(1, gate.NEWLINE)], findings

    def test_escape_then_bare_newline_is_the_half_converted_shape(self):
        # One escaped continuation and one bare newline in the same literal: the
        # 2026-11-24 mixture, reported even though the first line is a sentence.
        findings = _scan(MIXED_ESCAPE + MIXED_ESCAPE_TAIL)
        assert [(item.line, item.kind) for item in findings] == [(1, gate.NEWLINE)], findings

    def test_trailing_space_is_reported(self):
        findings = _scan(TRAILING_SPACE)
        assert _positions(findings) == [(1, 11, gate.TRAILING)]

    def test_trailing_tab_is_reported(self):
        # A tab at end of a line INSIDE a plain literal: the literal's own
        # trailing byte, which rustfmt never normalises. The same text also
        # carries the literal's bare newline, so BOTH classes fire — asserted as
        # a set because the two findings share one line.
        findings = _scan(TRAILING_TAB)
        assert {(item.line, item.kind) for item in findings} == {(1, gate.TRAILING), (1, gate.NEWLINE)}, findings

    def test_each_finding_carries_path_line_and_column(self):
        for text in (BARE_NEWLINE, TRAILING_SPACE):
            for item in _scan(text):
                assert item.path == "fixture.rs"
                assert item.line >= 1
                assert item.column >= 1
                assert item.kind in (gate.NEWLINE, gate.TRAILING)
                assert f"fixture.rs:{item.line}:{item.column}" in item.render()

class test_negative_control_stays_green:
    """Legal multi-line Rust must produce nothing — the corpus's whole point."""

    def test_legal_shapes_produce_no_findings(self):
        assert _scan(LEGAL) == []

    def test_escaped_continuation_alone_is_legal(self):
        text = 'let msg = "a " ' + BS + NL + "    \"b\";" + NL
        assert _scan(text) == []

    def test_raw_string_newlines_are_legal(self):
        text = 'let r = r"' + NL + 'two lines";' + NL
        assert _scan(text) == []

    def test_template_body_is_legal(self):
        text = 'let js = "(function() {' + NL + '    var x = 1;' + NL + '})";' + NL
        assert _scan(text) == []

    def test_trailing_space_inside_a_raw_string_is_data(self):
        text = 'let r = r"a' + SP + SP + NL + 'b";' + NL
        assert _scan(text) == []

class test_waiver_anchor:
    def test_allowed_line_is_not_reported(self):
        text = "let x = 1;" + SP + SP + " // string-lit-gate: allow fixture" + NL
        assert _scan(text) == []

    def test_allow_marker_on_the_literal_line_covers_class_a(self):
        text = '    "line one // string-lit-gate: allow cloned message' + NL + '    line two";' + NL
        assert _scan(text) == []

    def test_marker_must_name_the_gate(self):
        # A near-miss marker: ``string-lit:`` without ``-gate``. The line has no
        # trailing whitespace, so the ONLY finding this text can produce is the
        # newline inside the literal — which the near-miss must NOT excuse.
        text = '"line one // string-lit: allow' + NL + 'line two";' + NL
        assert [(item.line, item.kind) for item in _scan(text)] == [(1, gate.NEWLINE)]

class test_terminal_gate:
    """A scan that saw nothing must refuse rather than report clean."""

    def test_empty_directory_is_a_usage_error(self, tmp_path):
        assert gate.main([str(tmp_path)]) == gate.EXIT_USAGE

    def test_missing_path_is_a_usage_error(self, tmp_path):
        assert gate.main([str(tmp_path / "nope")]) == gate.EXIT_USAGE

    def test_clean_file_exits_zero(self, tmp_path):
        (tmp_path / "ok.rs").write_text('fn f() {' + NL + '    let x = 1;' + NL + '}' + NL, encoding = "utf-8")
        assert gate.main([str(tmp_path)]) == gate.EXIT_OK

    def test_defect_file_exits_one(self, tmp_path):
        (tmp_path / "bad.rs").write_text(BARE_NEWLINE, encoding = "utf-8")
        assert gate.main([str(tmp_path)]) == gate.EXIT_FINDINGS

class test_discovery_and_classes:
    def test_non_rust_files_are_ignored(self, tmp_path):
        (tmp_path / "notes.md").write_text("a line   " + NL, encoding = "utf-8")
        findings, scanned, problems = gate.check_paths([str(tmp_path)])
        assert (findings, scanned, problems) == ([], 0, [])

    def test_class_filters_are_independent(self, tmp_path):
        (tmp_path / "both.rs").write_text(BARE_NEWLINE + TRAILING_SPACE, encoding = "utf-8")
        only_newline, _, _ = gate.check_paths([str(tmp_path)], newline = True, trailing = False)
        only_trailing, _, _ = gate.check_paths([str(tmp_path)], newline = False, trailing = True)
        assert [item.kind for item in only_newline] == [gate.NEWLINE]
        assert [item.kind for item in only_trailing] == [gate.TRAILING]

    def test_vendored_trees_are_skipped(self, tmp_path):
        vendored = tmp_path / "vendor" / "upstream"
        vendored.mkdir(parents = True)
        (vendored / "x.rs").write_text(BARE_NEWLINE, encoding = "utf-8")
        findings, scanned, _ = gate.check_paths([str(tmp_path)])
        assert (findings, scanned) == ([], 0)

class test_cli_wiring:
    def test_dispatcher_reaches_the_command(self):
        assert cli.COMMANDS["string-lit-gate"] == "celestia_devtools.lint.string_lit_gate"
        assert callable(sys.modules["celestia_devtools.lint.string_lit_gate"].main)
