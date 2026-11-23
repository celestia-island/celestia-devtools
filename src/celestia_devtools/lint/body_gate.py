#!/usr/bin/env python3
"""body-gate - every number and behaviour claim in a PR body must name its source.

*Why this exists:* the recorded-exception PRs of this wave (hikari #1476 /
#1478 / #1482 / #1485 / #1503 / #1506, chest #2813 / #2815, tairitsu #154) were
merged with **correct production code** and prose in the body that review
rounds kept refuting. The comment gate (``celestia_devtools.lint.comment_gate``,
devtools #306) covers behaviour claims in *test-file comments*; PR-body prose —
where the figures and the causal sentences live — had no mechanical check at
all. The failure named by the operators themselves: *when a number does not add
up, invent a reason instead of writing "cause not established"*.

*Boundary - what this is NOT:* it cannot judge whether a claim is **true**. A
verdict needs the reference semantics, and a text scan has none. It checks one
mechanical proxy: **wherever the body states a quantity or claims behaviour,
that statement must point at something a reader can recompute**, or say out
loud that it was not recomputed.

*Scope - deliberately SMALL.* An earlier revision of this gate tried to decide,
for every sentence, whether a code token made the sentence "naming a thing"
rather than claiming it, whether a table was a capture, whether a hash was a
subject — and five independent review rounds found eleven free passes inside
that question, every one of them a new facet of the same wording. That is not a
gate that converges; it is a rule whose surface cannot be enumerated. This
version keeps the three judgements a reviewer can check by reading it:

1. **a quantity must carry a source** — a number with a unit / counter word, a
   ratio, an ``rc=``, a percentage, a duration, or a bare prose figure
   (``3 of 4``, ``2 → 0 → 2``);
2. **a behaviour claim must carry a source** — the comment gate's own
   vocabulary, imported unmodified (:data:`BEHAVIOUR_TERMS`);
3. **a captured table is a table of readings** — a row whose first cell is a
   label and whose remaining cells are numbers / ``#ref`` / empty
   (:func:`is_reading_row`).

and one escape hatch the author can use honestly. Every other prose shape —
a table that mixes prose into its rows, a sentence that names a commit or a
file, a bare figure next to a command on the previous line — is covered by the
**explicit directive** ``body-gate-ok: <reason>`` on the statement's own line,
whose reason is held to the same 20-character floor as the PR-body waiver and
whose use is printed by the gate. That is the honest trade this version makes:
the three rules above are mechanical, and the rest is the author stating, in
public, that they did it deliberately.

*A source* (:data:`ANCHORS`) is one of: ``MUT:<id>`` / ``mutate:<slug>`` (the
comment gate's own pattern, imported), an ``evidence:`` marker, a ``#PR``
reference, a URL, a commit hash (7+ hex with at least one digit) or the
checksum words ``sha256`` / ``sha1`` / ``md5``, a ``file:line`` reference to a
dotted filename, or an inline-code span shaped like a command / flag / call /
identifier / multi-segment path. The source may sit on the statement's own
line, the line above or the line below.

*A disclaimer* (:data:`DISCLAIMERS`) says out loud that the figure was not
recomputed (``未复算`` / ``unverified`` / ``not established`` / ``inference`` /
…). Saying "I did not recompute this" is a valid answer; writing a cause you
cannot check is not, and that is the entire disease.

*Out of scope:* fenced blocks, indented blocks, block quotes, HTML comments,
headings, the gate entry lines, the ``verify:`` record line, and lines carrying
the directive. A body whose prose states nothing is a **loud skip** (rc 0, the
skip printed); an empty body, a body that is only its record, and a body whose
every line was excluded are **refused** (rc 1).

*Printing:* every directive the gate honours is printed — by the CLI, and by
the merge chain (:func:`celestia_devtools.vcs.pr_gate.body_gate_problems`
forwards ``report.directives``), so an exemption is never reached silently.

*Waiver:* ``--waiver`` does not cover this gate. The only exception is
``body-gate-waiver: <reason>`` in the PR body, read by the merge gate through
the same parser as ``comment-gate-waiver:`` and announced on stdout.

*CLI:* ``lint-pr-body [--body FILE | --pr N --repo OWNER/NAME] [--quiet]``,
also reachable as ``celestia-devtools lint-pr-body``. The merge chain calls
:func:`check_body` directly and always prints the directive and skip lines; it
does not forward ``--quiet``, so nothing is ever silenced inside a merge.
Exit codes: 0 clean or loud skip, 1 unsourced statements, 2 usage or an
unreadable body.
"""

from __future__ import annotations
import argparse
import html
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
# py-style-ok: no-block-imports: module scope beside the other constants; the
# comment gate is a sibling module by design and carries no heavy imports.
from celestia_devtools.lint import comment_gate

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

#: The behavioural vocabulary is the comment gate's list, imported rather than
#: copied: one list, one owner, and the two gates cannot drift.
BEHAVIOUR_TERMS: tuple[str, ...] = comment_gate.BEHAVIOUR_TERMS

#: Quantities: a number WITH its unit or counter word, in both languages.
QUANTITY_PATTERNS: tuple[str, ...] = (  # py-style-ok: single-line-statement: one data table, one entry per line
    # 120 passed / 12 failed / 3 green / 2 red
    r"\b\d[\d,]*\s*(?:passed|failed|skipped|green|red|checks?|assertions?|findings?|rows?|claims?)\b",
    # 16/16, 3/4, 46/46
    r"(?<![\d/§])\d[\d,]*\s*/\s*\d[\d,]*(?![\d/])",
    # rc=0 / exit 0 / exit code 1
    r"\b(?:rc|exit(?:\s*code)?)\s*[=:]\s*-?\d+",
    # 16 files / 2 crashed / 6 tests / 4 components
    r"\b\d[\d,]*\s*(?:files?|tests?|cases?|runs?|rounds?|pins?|calls?|lines?|crash(?:es)?|faults?|components?|screenshots?|probes?)\b",
    # 2.9s / 120 ms / 0.42 seconds / 3.3 MB / 174 bytes / 1.2 MiB
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:ms|µs|us|ns|s|sec|secs|seconds?|min|mins|minutes?|hours?|bytes?|B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\b",
    # 85% / 12.5 %
    r"\d[\d,]*(?:\.\d+)?\s*%",
    # `same sha256` / `md5 equal` — a checksum quoted as a claim
    r"\bsha-?(?:1|256|512)\b|\bmd5\b",
    # 4 处 / 3 个文件 / 6 行 / 2 次
    r"\d[\d,]*\s*(?:处|次|个|行|条|项|例|件|组|份|字|笔|步|轮|章|节)",
    # 三个测试 / 五轮
    r"(?:[一二两三四五六七八九]|十[一二三四五六七八九]?|二十|三十)(?:处|次|个|行|条|项|例|件|组|份|步|轮)"
)
QUANTITY = re.compile("|".join(f"(?:{pattern})" for pattern in QUANTITY_PATTERNS), re.IGNORECASE)

#: A bare prose figure: a number in a sentence (`3 of 4`, `10 in round 1`,
#: `2 → 0 → 2`). The exclusions live in :func:`prose_figures`.
PROSE_FIGURE = re.compile(r"(?<![A-Za-z0-9_.§/~-])\d+(?:[,.]\d+)*")

#: Figures that are not prose claims even bare: a version (`0.7.0`, `>= 1.36`),
#: a section or PR reference (`§3`, `#2813`), a dimension (`220×220`), a ratio
#: (`8/310`), a unit-glued figure (`3.3 MB`, `48400 px`), an object count
#: (`exits 1`, `line 12`) and a CJK pronoun / ordinal (`这一行`, `第一版`).
_VERSION = re.compile(r"^\d+(?:\.\d+)+$")
_CJK_PRONOUN = re.compile(r"[这那每某唯一第一二三四五六七八九十][一二三四五六七八九十]?")
_CJK_ORDINAL = re.compile(r"第[一二三四五六七八九十\d]+")
_HOST_WORD = re.compile(r"(?i)^(?:exits?|lines?|steps?|modes?|items?|index|pages?|issues?|pr|parts?|phases?|levels?|tiers?|versions?|no\.)$")

#: The disclaimer vocabulary: saying out loud that a figure was not recomputed
#: (or that its cause was not established) is a VALID answer.
DISCLAIMERS: tuple[str, ...] = (  # py-style-ok: single-line-statement: one data table, one entry per line
    "未复算",
    "未验证",
    "未测量",
    "未复现",
    "原因未查明",
    "unverified",
    "not established",
    "not re-run",
    "not rerun",
    "not verified",
    "not measured",
    "unmeasured",
    "not reproduced",
    "inference",
    "inferred",
    "unconfirmed"
)

#: The directive: a statement the author deliberately makes with no source. The
#: reason is part of the pattern, held to :data:`DIRECTIVE_REASON_MIN`, and the
#: gate prints every line it exempts.
DIRECTIVE_REASON_MIN = 20

#: Zero production must fail, not read as "no claims".
#:
#: The comment gate learned the same thing from ``MIN_CALLBACK_SITES``: a scan
#: that finds nothing is either a body with nothing to find or a broken
#: extractor, and a silent green cannot tell them apart. A body with fewer than
#: this many prose lines and no statement is a *loud skip* (an ordinary short
#: description — the four published devtools bodies run 1, 9 and 11 prose lines
#: with 0 or 1 statements); a body with at least this many prose lines and NO
#: statement at all is REFUSED, because real prose on that scale always states
#: something and a zero here means the scan did not run.
MIN_PROSE_LINES = 20
DIRECTIVE = re.compile(r"body-gate-ok:[ \t]*(?P<reason>.*)$", re.IGNORECASE)

#: A commit hash (7+ hex, at least one digit) or a named checksum. An all-letter
#: run is a word (`defaced`), not a hash.
_COMMIT = re.compile(r"(?<![\w])[0-9a-f]{7,40}(?![\w])", re.IGNORECASE)
_CHECKSUM_WORD = re.compile(r"\bsha-?(?:1|256|512)\b|\bmd5\b", re.IGNORECASE)
#: A `file:line` reference: a dotted filename, a colon, a line number.
#: A test pointer that names a CASE (`a.py::test_b`) or carries a line
#: (`x.test.ts:88`) is line-level evidence: it opens at a specific assertion.
#: A bare test FILE name (`x.test.ts`, `test.ts`, `y_test.rs`) is NOT a source
#: at any level — citing a file says the file exists, nothing about what the
#: sentence claims. That is refused EXPLICITLY by :func:`span_is_source`; the
#: span-shape regex alone would let `test.ts` through its other alternatives.
_TEST_REF = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|rs)::[\w:.-]+|[\w./-]+\.(?:test|spec)\.[a-z]+\s*:\s*\d+|[\w./-]+_test\.(?:py|rs)\s*:\s*\d+")

_FILE_LINE = re.compile(r"[\w./-]+\.(?:rs|ts|tsx|js|jsx|py|md|toml|yml|yaml|json|sql|wit|vue|sh|just|lock|txt)\s*:\s*\d+")
_EVIDENCE = re.compile(r"\bevidence\s*:", re.IGNORECASE)
_PR_REF = re.compile(r"(?<![\w#])#\d+\b")
_URL = re.compile(r"https?://\S+")

#: An inline-code span shaped like something a reader can act on: a flag, a
#: call, an identifier, a filename, a multi-segment path, a value assignment.
_SPAN = re.compile(r"`[^`\n]+`")
#: A span is a SOURCE when its contents look like a thing to run or open: a
#: flag, a call, an underscored or camelCase identifier, a path, a filename, a
#: value assignment, or a short all-lowercase word pair (`vite build`).
#: …plus a short multi-word COMMAND (`vite build`, `cargo test`) — two or three
#: lowercase words with no article. That is the line the lead drew: `a b` and
#: `the thing` are prose, and a span that cannot be told from prose is not a
#: source.
_SPAN_SOURCE = re.compile(r"--?[a-z][\w-]*|\w+\(|\w_\w|[a-z][A-Z]|\w{2,}/\w|(?<!_test)(?<!\.test)(?<!\.spec)\.\w{1,6}$|:\d|=\S|^(?!(?:the|a|an|of|to|and|or|is|was) )[a-z][a-z0-9_-]*(?:\s+[a-z][\w-]*){1,2}$")

#: The marker set, in the order the docstring lists it.
ANCHORS: tuple[str, ...] = ("MUT", "evidence", "#PR", "URL", "SHA", "file:line", "span")

#: A markdown table row and its delimiter row.
_ROW = re.compile(r"^\s*\|.*\|[ \t]*$")
_ROW_SEPARATOR = re.compile(r"^\s*\|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|\s*$")

#: The gate's own entry lines (an exception statement is not prose), anchored at
#: the line start so a sentence that merely mentions the token is still scanned.
_WAIVER_ENTRY = re.compile(r"^[\s*>#]*[*_`]*(?:body|comment|artifact)-gate-waiver[*_`]*\s*[:：]", re.IGNORECASE)
#: The verify record line: machine state owned by the record checker.
_RECORD_LINE = re.compile(r"^[ \t>#*_+.\-`]*(?:\d+[.)][ \t]+)?[*_ \t#>.`-]*(?:verify|合计)[ \t*_`]*\**[ \t]*[:：]", re.IGNORECASE)
#: A fenced block opener.
_FENCE = re.compile(r"^(`{3,}|~{3,})")
#: Zero-width characters a directive reason could hide behind.
_INVISIBLE = comment_gate._INVISIBLE

@dataclass(frozen = True)
class statement:
    """One line of body prose that states a quantity or claims behaviour."""

    line: int
    text: str
    kinds: tuple[str, ...]
    terms: tuple[str, ...]

    def render(self) -> str:
        kinds = ", ".join(self.kinds)
        terms = f" [{', '.join(self.terms)}]" if self.terms else ""
        return f"body:{self.line} ({kinds}){terms}: {self.text.strip()[:220]}"

@dataclass(frozen = True)
class report:
    """The gate's whole verdict over one PR body."""

    scanned_lines: int
    statements: tuple[statement, ...]
    findings: tuple[statement, ...]
    problems: tuple[str, ...]
    directives: tuple[tuple[int, str], ...] = ()
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and not self.findings

@dataclass(frozen = True)
class _line:
    """One line of the body that is subject to the claim scan."""

    number: int
    text: str
    section: int = 0

def prosaic(body: str) -> tuple[list[_line], list[str], list[_line], tuple[str, ...]]:
    """The lines of ``body`` subject to the claim scan, plus notes + fence lines.

    Excluded, each for a shape that reads as a template or a capture: fenced
    blocks (an unclosed fence swallows the rest — the conservative reading),
    indented blocks, block quotes, HTML comments (a closed one hides only
    itself), headings, the gate entry lines and the verify record line.
    """
    lines: list[_line] = []
    notes: list[str] = []
    evidence: list[_line] = []
    dropped: list[int] = []
    hidden: list[tuple[int, str]] = []
    fences_notes: list[str] = []
    fence: str | None = None
    in_comment = False

    for number, raw in enumerate(body.splitlines(), start = 1):
        opener = _FENCE.match(raw.strip())

        if fence is not None:
            evidence.append(_line(number = number, text = raw))

            if (opener is not None) and (opener.group(1)[0] == fence) and (set(raw.strip()) == {fence}):
                fence = None

            continue

        if opener is not None:
            fence = opener.group(1)[0]
            evidence.append(_line(number = number, text = raw))
            continue

        text = raw

        if in_comment:
            _before, closed, trailing = text.partition("-->")

            if not closed:
                continue

            in_comment = False
            text = trailing

        if "<!--" in text:
            before, _, after = text.partition("<!--")
            span, closed, trailing = after.partition("-->")
            # A directive inside the comment is REPORTED (so it is never a
            # silent pass) but does not exempt: an invisible position must not
            # grant an invisible exemption (round 2 of the narrowed build).
            comment = DIRECTIVE.search(span)
            inline = comment.group(0) if comment is not None else ""

            if comment is not None:
                hidden.append((number, inline))

            text = before + ("<!-- " + inline + " -->" if inline else "") + trailing

            if not closed:
                in_comment = True

        if not text.strip():
            lines.append(_line(number = number, text = ""))
            continue

        lead = text[: len(text) - len(text.lstrip())]
        stripped = text.strip()
        in_table_row = _ROW.match(stripped) is not None

        if (not in_table_row) and (("\t" in lead) or (len(lead) >= 4)):
            dropped.append(number)
            continue

        if stripped.startswith(">") or stripped.startswith("#"):
            dropped.append(number)
            continue

        if _WAIVER_ENTRY.match(stripped) or _RECORD_LINE.match(raw):
            continue

        lines.append(_line(number = number, text = text))

    if fence is not None:
        notes.append("the body ends inside an unclosed code fence — everything after the opener was treated as code (the conservative reading of a truncated body)")

    # Paragraph labelling: a blank line, a heading or a table block starts the
    # next one. The table block is one unit so its rows are judged together.
    section = 0
    previous_blank = True
    index = 0

    while index < len(lines):
        row = lines[index]
        stripped = row.text.strip()

        if _ROW.match(stripped):
            while (index < len(lines)) and _ROW.match(lines[index].text.strip()):
                index += 1

            section += 1
            previous_blank = False
            continue

        if not stripped:
            section += 1
            previous_blank = True
            index += 1
            continue

        if previous_blank:
            section += 1

        previous_blank = False
        index += 1

    hidden_note = (f"note: an inline HTML comment on line {hidden[0][0]} carries a body-gate-ok directive — it is reported here and does not exempt anything, because a hidden directive must not grant a hidden pass",) if hidden else ()

    omitted = ("the body's only prose was indented or quoted, so nothing could be scanned — an indented block is a template, not a description",) if (not lines) and dropped else ()
    notes = [*notes, *hidden_note]

    return lines, notes, evidence, omitted

def behaviour_terms(text: str) -> tuple[str, ...]:
    """The behavioural vocabulary ``text`` hits (the comment gate's own test)."""
    return comment_gate.behaviour_terms(text)

def prose_figures(text: str) -> tuple[str, ...]:
    """Bare prose figures in ``text`` — the numbers a sentence asserts.

    Excluded (each is a *label* rather than a measurement): a version
    (`0.7.0`, or any figure right after a comparison operator), a section or PR
    reference (`§3`, `#2813`), a dimension or ratio (`220×220`, `8/310`), a
    figure glued to its unit (`3.3 MB`, `48400 px`), an object count
    (`exits 1`, `line 12`) and a CJK pronoun / ordinal (`这一行`, `第一版`).
    """
    found: list[str] = []

    for match in PROSE_FIGURE.finditer(text):
        before = text[: match.start()]
        number = match.group(0)
        following = text[match.end():]
        word_before = before.rstrip().split(" ")[-1] if before.strip() else ""
        tail = before[-1:]

        if re.match(r"^\s?(?:%|[×*]|/\s*\d|[A-Za-z]{1,6}\b(?![\s,.;:)]|$))", following) and (not re.match(r"^\s?(?:→|->|=>)", following)):
            continue

        if (following[:1] != " ") and (not re.match(r"^[\u4e00-\u9fff]", following)) and (not re.match(r"^\s?(?:→|->|=>)", following)):
            continue

        if tail and (tail in "§-—([/<>≥≤="):
            continue

        if before.rstrip().endswith((">=", "<=", ">", "<", "=")):
            continue

        if _VERSION.match(number) or _HOST_WORD.match(word_before):
            continue

        if _CJK_PRONOUN.match(tail) or _CJK_ORDINAL.match(before[-3:] + number):
            continue

        if ("\u4e00" <= tail <= "\u9fff") and not re.match(r"^\s*(?:处|次|个|行|条|项|例|件|组|份)", following):
            continue

        found.append(number)

    return tuple(found)

def code_spans(text: str) -> tuple[str, ...]:
    """The inline-code spans of ``text``, without their backticks."""
    return tuple(match.group(0).strip("`") for match in _SPAN.finditer(text))

#: A BARE test file name: a module called `x.test.ts` / `test.ts` / `y_test.rs`
#: with no `::case` and no `:line`. Citing it names a file, not a place where an
#: assertion lives, so it evidences nothing.
_BARE_TEST_NAME = re.compile(r"^(?:[\w./-]*\.)?(?:test|spec)(?:\.\w+)?$|^[\w./-]*(?:\.(?:test|spec)|_test)\.\w+$")

def span_is_source(span: str) -> bool:
    """True when a span is shaped like a command, flag, call, identifier or path.

    A bare test file name is refused first and explicitly: the shape regex has
    alternatives (a flag-like word, an underscored identifier) that would
    otherwise let `test.ts` or `y_test.rs` through, and the point of the rule is
    that a file name cannot source a claim about behaviour.
    """
    text = span.strip()

    if _BARE_TEST_NAME.fullmatch(text):
        return False

    return bool(_SPAN_SOURCE.search(text)) or (bool(re.search(r"\.\w{1,6}$", text)) and (len(text) >= 4))

def has_quote(text: str) -> bool:
    """True when ``text`` carries an inline-code span at all."""
    return bool(_SPAN.search(text))

def is_source(line: str) -> bool:
    """True when ``line`` carries one of :data:`ANCHORS`.

    Two kinds of source, and the difference is the whole rule:

    * **line sources** — a ``#PR``, a ``file:line``, ``MUT:`` / ``mutate:``, an
      ``evidence:`` marker, a URL, a checksum word, or a commit hash with a
      digit. Each points at a thing a reader can open, so where it sits on the
      line does not matter.
    * **span sources** — an inline-code span names the thing it quotes. It is
      evidence for a FIGURE (``` `120 passed` ```) and for a claim about that
      thing (``` `x.py:12` shows it ```), but it is NOT evidence for a claim
      ABOUT the code it names: "… asserted `toEqual(QR_CAPABILITIES)` — it never
      referenced it" quotes the call and then asserts the opposite, and the
      quotation proves only that the call exists. So when the sentence carries
      an attribution word (``never`` / ``always`` / ``catches`` / …) and no line
      source, the spans do not count.
    """
    text = line.lower()

    for word in DISCLAIMERS:
        text = text.replace(word, " " * len(word))

    # A pointer to a specific TEST CASE (`a.py::test_b`, `x.test.ts:88`) is
    # line-level: it opens at an assertion. A bare test FILE name is not — it
    # only says the file exists (see the span branch below).
    if _TEST_REF.search(text):
        return True

    for pattern in (_EVIDENCE, _PR_REF, _URL, _CHECKSUM_WORD, _FILE_LINE, comment_gate._EXPLICIT_ANCHOR):
        if pattern.search(text):
            return True

    for match in _COMMIT.finditer(text):
        if re.search(r"[0-9]", match.group(0)):
            return True

    # A command line WITH its result (` `vite build` → rc=0 `) is a source in
    # its own right: the arrow and the exit code are output, not a quotation.
    if re.search(r"(?:\u2192|->|=>|\brc\s*=|\bexit\s+\d)", text):
        return True

    # A span names its subject; it does not evidence a claim ABOUT that subject.
    # The vocabulary is the comment gate's list, reached through this module's
    # two-hop delegation (`body_gate.behaviour_terms` returns
    # `comment_gate.behaviour_terms(text)`) rather than a copy: a
    # hand-transcribed 9-word regex dropped `doesn't`, `only when` and all four
    # CJK entries, and review caught the body claiming "the same list" while the
    # code held a subset.
    if behaviour_terms(text):
        return False

    for span in code_spans(text):
        if span_is_source(span):
            return True

    return False

def disclaims(line: str) -> bool:
    """True when ``line`` says out loud that the figure was not recomputed."""
    return any(word in line.translate(_INVISIBLE).lower() for word in DISCLAIMERS)

def directive_reason(line: str) -> str | None:
    """The reason a ``body-gate-ok:`` directive carries, or ``None``."""
    match = DIRECTIVE.search(line)

    if match is None:
        return None

    reason = line[match.start("reason"):].translate(_INVISIBLE).strip()

    # Only a directive that came out of an HTML comment carries the comment's
    # close (` -->`) in its reason; a visible directive's reason is taken as
    # written (a `>` at the floor is the author's character, not our noise).
    if _hidden_directive(line):
        return reason.rstrip("->").strip()

    return reason

def is_reading_row(line: str) -> bool:
    """True when a table row is a label followed by readings.

    The first cell may be a word (the row's label); every following cell must be
    empty, a number with an optional unit, or a `#ref`. A prose cell
    (``9 of 10 builds break``, ``waiver PR``) makes the row prose — which is the
    only table shape this gate certifies.
    """

    if not _ROW.match(line):
        return False

    cells = [cell.strip().strip("*`_ ") for cell in line.strip().strip("|").split("|")]

    if len(cells) < 2:
        return False

    return all((not cell) or bool(_READING_CELL.fullmatch(cell)) for cell in cells[1:])

#: A reading: a number with an optional unit, or a `#ref`, or an em dash.
_READING_CELL = re.compile(r"-?\d+(?:[.,]\d+)*\s*(?:%|ms|s|MiB|GiB|KB|MB|GB|px|B)?|#\d+|—|-")

def table_bounds(lines: Sequence[_line], index: int) -> tuple[int, int] | None:
    """The ``[first, last]`` envelope of the table block holding ``index``."""

    if not _ROW.match(lines[index].text.strip()):
        return None

    low = index
    high = index

    while (low > 0) and _ROW.match(lines[low - 1].text.strip()):
        low -= 1

    while ((high + 1) < len(lines)) and _ROW.match(lines[high + 1].text.strip()):
        high += 1

    return low, high

def scan_body(body: str) -> tuple[list[statement], int, list[str]]:
    """Every claim line of ``body``: ``(statements, scanned_lines, notes)``."""
    lines, notes, _evidence, _omitted = prosaic(body)
    statements: list[statement] = []

    for index, row in enumerate(lines):
        bounds = table_bounds(lines, index)

        if bounds is not None:
            low, high = bounds
            table = lines[low: high + 1]

            # A table made of reading rows is captured output: its cells are
            # the numbers a run printed.
            if all(is_reading_row(part.text) or _ROW_SEPARATOR.match(part.text.strip()) for part in table):
                continue

            if is_reading_row(row.text):
                continue

        kinds, terms = claim_kinds(row.text)

        if kinds:
            statements.append(statement(line = row.number, text = row.text, kinds = kinds, terms = terms))

    return statements, len(lines), notes

def claim_kinds(line: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(kinds, terms)`` for one line of prose; empty ``kinds`` = no claim.

    A line is a claim when it carries the comment gate's behaviour vocabulary or
    a figure. A code span does NOT make a sentence a description: ``` `x.test.tsx:88` ```
    is one way to SOURCE a claim (it sits beside `MUT:`, `evidence:` and
    `file:line` in :func:`is_source`), not a way to excuse it. That separation is
    what closes the free pass review round 4 replayed: with a span exempting the
    whole line, "the armed path was never exercised" stopped being a claim as
    soon as any identifier sat beside it.
    """
    kinds: list[str] = []
    terms = behaviour_terms(line)

    if QUANTITY.search(line) or prose_figures(line):
        kinds.append("quantity")

    if terms:
        kinds.append("behaviour")

    return tuple(kinds), terms

def sourced(lines: Sequence[_line], index: int, evidence: Sequence[_line] = ()) -> bool:
    """True when the statement at ``index`` may stand.

    The window is the statement's own line, the line above and the line below;
    inside it, any of these suffices:

    * a **source** (:func:`is_source`) — an anchor, a ``MUT:`` id, a command in
      backticks, a ``file:line``, a ``#PR``, a URL, a commit hash;
    * a **disclaimer** (:func:`disclaims`) — "not recomputed", said out loud;
    * the statement's own **directive** (``body-gate-ok: <reason>`` with a real
      reason) — the author's explicit, printed decision to make the claim
      without a source;
    * a **capture**: a pasted quoted figure on the same line, or a fenced block
      one line away (a figure beside the command that printed it).
    """
    row = lines[index]
    low = max(0, index - 1)
    high = min(len(lines) - 1, index + 1)

    for position in range(low, high + 1):
        part = lines[position]

        if (position != index) and not part.text.strip():
            continue

        if is_source(part.text) or disclaims(part.text):
            return True

        reason = directive_reason(part.text)

        if (reason is not None) and (len(reason) >= DIRECTIVE_REASON_MIN) and (not _hidden_directive(part.text)):
            return True

        if (position == index) and pastes_figure(part.text):
            return True

    return any(abs(part.number - row.number) <= 1 for part in evidence)

def pastes_figure(line: str) -> bool:
    """True when ``line`` carries a quoted figure (`` `120 passed` ``).

    A pasted figure is the run's output beside the prose talking about it; the
    quotation is the evidence, so the line may stand on it.
    """
    spans = [match.span() for match in _SPAN.finditer(line)]

    return any((start <= part.start()) and (part.end() <= end) for part in QUANTITY.finditer(line) for start, end in spans)

def _hidden_directive(line: str) -> bool:
    """True when ``line`` carries a directive that came from an HTML comment."""
    return bool(re.search(r"<!--[^>]*body-gate-ok:", line, re.IGNORECASE))

def check_body(body: str) -> report:
    """The gate's verdict over one PR body string."""
    stripped_body = (body or "").strip()

    if not stripped_body:
        return report(scanned_lines = 0, statements = (), findings = (), problems = ("the PR body is empty — an empty body is either a probe or a truncated payload, and no reason to believe anything about the change appears anywhere; write the body",))

    lines, notes, evidence, omitted = prosaic(stripped_body)

    if not lines:
        nothing_scan = "zero prose lines in the body — everything is the verify record, a fence, a quote, an indent or a heading; a green verdict over it would certify a scan that was empty"

        return report(scanned_lines = 0, statements = (), findings = (), problems = (*notes, *omitted, nothing_scan))

    statements, scanned, _scan_lines = scan_body(stripped_body)
    problems = list(notes)
    directives = tuple((row.number, directive_reason(row.text) or "") for row in lines if directive_reason(row.text) is not None)
    findings = [stmt for stmt in statements if not sourced(lines, _index_of(lines, stmt), evidence)]

    if not statements:
        # A body that states nothing is a skip — unless something else already
        # refuses it (a hidden directive's note, an unclosed fence), in which
        # case the merge log must not print "nothing to judge" beside a refusal.
        skipped = None if problems else f"no quantity or behaviour statement in {scanned} prose line(s) — nothing to judge"

        # MIN_PROSE_LINES: prose this long always states something; zero is the
        # shape of an extractor that stopped working, so it refuses.
        if (not problems) and (scanned >= MIN_PROSE_LINES):
            problems = [f"zero quantity or behaviour statement across {scanned} prose lines — prose this long always states one, so this is the shape of a broken extractor, not of a clean body; refusing to report green"]

        return report(scanned_lines = scanned, statements = (), findings = (), problems = tuple(problems), directives = directives, skipped = None if problems else skipped)

    return report(scanned_lines = scanned, statements = tuple(statements), findings = tuple(findings), problems = tuple(problems), directives = directives)

def _index_of(lines: Sequence[_line], stmt: statement) -> int:
    """The position of ``stmt`` in ``lines`` (built in the same order)."""

    for index, row in enumerate(lines):
        if row.number == stmt.line:
            return index

    raise LookupError(f"{stmt.line} is not a scanned line")

def missing_for(stmt: statement) -> str:
    """What the body would have to add, in the design's own words."""
    parts: list[str] = []

    if "quantity" in stmt.kinds:
        parts.append("the command or output the number came from (`file:line`, `#PR`, `evidence:`, `MUT:<id>`, or the COMMAND in backticks)")

    if "behaviour" in stmt.kinds:
        parts.append("a mutation id or a probe result (`MUT:<id>`, `evidence:`, a `file:line`)")

    parts.append("or the disclaimer 未复算 / unverified / not established / inference")
    parts.append(f"or the directive `body-gate-ok: <reason>` (≥{DIRECTIVE_REASON_MIN} characters, printed when used)")

    return "; ".join(parts)

def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``lint-pr-body [--body FILE | --pr N --repo OWNER/NAME] [--quiet]``."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools lint-pr-body", description = "Fail quantities and behaviour claims in a PR body that name no source.")
    source = parser.add_mutually_exclusive_group(required = True)
    source.add_argument("--body", default = None, metavar = "PATH", help = "read the body from a UTF-8 file (`-` = stdin)")
    source.add_argument("--pr", default = None, type = int, help = "read the PR body with `gh pr view`")
    parser.add_argument("--repo", default = None, metavar = "OWNER/NAME", help = "repository for --pr (default: the checkout's origin)")
    parser.add_argument("--quiet", action = "store_true", help = "only the closing verdict line")
    args = parser.parse_args(argv)

    if args.pr is not None:
        body, problems = read_pr_body(args.repo, args.pr)

        if body is None:
            for problem in problems:
                print(f"❌ {problem}", file = sys.stderr)

            return EXIT_USAGE

        label = f"PR #{args.pr}"
    else:
        body, problems = read_body_file(args.body)

        if body is None:
            for problem in problems:
                print(f"❌ {problem}", file = sys.stderr)

            return EXIT_USAGE

        label = args.body or "stdin"

    return report_and_exit(check_body(body), label, quiet = args.quiet)

def read_body_file(path: str | None) -> tuple[str | None, list[str]]:
    """The body text from ``path`` (``-`` = stdin); never raises."""

    if path in (None, "-"):
        try:
            return sys.stdin.read(), []
        except (OSError, UnicodeDecodeError) as exc:
            return None, [f"cannot read stdin ({exc})"]

    try:
        return Path(path).read_text(encoding = "utf-8"), []
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"unreadable body file {path} ({exc})"]

def read_pr_body(repo: str | None, pr: int) -> tuple[str | None, list[str]]:
    """The PR body via ``gh pr view --json body``; never raises."""
    cmd = ["gh", "pr", "view", str(pr), "--json", "body", "--jq", ".body"]

    if repo:
        cmd.extend(["--repo", repo])

    try:
        probe = subprocess.run(cmd, capture_output = True, encoding = "utf-8", errors = "replace", check = False)
    except OSError as exc:
        return None, [f"cannot run gh: {exc}"]

    if probe.returncode != 0:
        detail = (probe.stderr or "").strip() or f"exit {probe.returncode}"
        return None, [f"gh pr view {pr} failed ({detail}) — refusing to certify a body that could not be read"]

    return probe.stdout, []

def report_and_exit(result: report, label: str, *, quiet: bool) -> int:
    """Print one verdict and return its exit code (shared with the merge gate)."""

    for line, reason in result.directives:
        if len(reason) >= DIRECTIVE_REASON_MIN:
            print(f"🔓 body-gate-ok (body:{line}): {reason}")

    for finding in result.findings:
        print(f"❌ {finding.render()}")
        print(f"   body:{finding.line}: needs {missing_for(finding)}", file = sys.stderr)

    for problem in result.problems:
        print(f"❌ {problem}", file = sys.stderr)

    if not result.ok:
        print(f"BODY-GATE REFUSED — {len(result.findings)} unsourced statement(s), {len(result.problems)} problem(s) over {label}", file = sys.stderr)
        return EXIT_FINDINGS

    if not quiet and (result.skipped is not None):
        print(f"⏭️  body-gate: {result.skipped} ({label})")
    elif not quiet:
        print(f"✅ body-gate: {len(result.statements)} statement(s) over {result.scanned_lines} prose line(s), all sourced, disclaimed or directed ({label})")

    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
