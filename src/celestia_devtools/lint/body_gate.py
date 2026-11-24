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
reference (**on a line that states nothing** — on a statement line a ``#PR``
proves only that the PR exists, and the line needs another source), a URL, a
commit hash (7+ hex with at least one digit) or the checksum words ``sha256`` /
``sha1`` / ``md5``, a ``file:line`` reference to a dotted filename, or an
inline-code span shaped like a command / flag / call / identifier /
multi-segment path. The source may sit on the statement's own line, the line
above or the line below.

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
    # The same four phrasings with the other two Chinese negators. Measured
    # (debt ⑧): `没复算，输出 120 passed。` and `未经复算，输出 120 passed。` were
    # REFUSED — the disclaimer exists, the matcher just did not know the word —
    # while `尚未复算` already worked because it carries `未复算` as a substring.
    # These are listed as DISCLAIMER phrases, not added to `_NEGATION_TAIL`: a
    # negation entry would let `没` refute a disclaimer standing beside it
    # (`没复算，unverified` says the same thing twice), and a bare `没` also
    # matches inside `淹没` / `出没` / `吞没`.
    "没复算",
    "没验证",
    "没测量",
    "没复现",
    "未经复算",
    "未经验证",
    "未经测量",
    "未经复现",
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
#: …and a reason of dots, underscores or one repeated letter is not a reason:
#: at least one letter or CJK character has to appear in it.
DIRECTIVE_REASON_SUBSTANCE = re.compile(r"[A-Za-z\u4e00-\u9fff]")

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
#:
#: The pattern is not gated on the extension, so what it refuses is exactly the
#: spellings below and not "every language's convention". Measured (debt ⑤), on
#: the full trees of six workspace repos: every test filename family in use —
#: `test_*.py` (106), `*.test.ts`/`*.test.tsx` (24), `*_test.rs` (4),
#: `*_test.py` (4), plus the `spec` spellings — is refused as a bare name, and
#: the number of files that match the pattern while NOT being tests is 0. So the
#: fail-open direction is empty and the fail-closed direction is empty too; the
#: `check_*.py` gates (11 files) are outside this family by design, because a
#: gate is not a test and a bare gate name is the `#PR` case, not this one.
_BARE_TEST_NAME = re.compile(r"(?:^|/)(?:test|spec)(?:[_-][\w-]+)?\.[A-Za-z0-9]+$|(?:^|/)(?:[\w-]+\.)*(?:test|spec)\.[A-Za-z0-9]+$|(?:^|/)[\w-]+_test\.[A-Za-z0-9]+$")

def span_is_source(span: str) -> bool:
    """True when a span is shaped like a command, flag, call, identifier or path.

    A bare test file name is refused first and explicitly: the shape regex has
    alternatives (a flag-like word, an underscored identifier) that would
    otherwise let `test.ts` or `y_test.rs` through, and the point of the rule is
    that a file name cannot source a claim about behaviour.
    """
    text = span.strip()

    # The rule is about a FILE NAME; a case or line suffix opens a specific
    # assertion, so only the head is tested here (`x.test.ts:88` stays a source).
    # The test is `search` on a pattern anchored to a path boundary: a directory
    # prefix is allowed on either side (`tests/test_body_gate.py`), because a
    # test FILE is a test file wherever it lives.
    head = re.split(r"[:#]", text)[0]
    has_suffix = head != text.strip()

    if (not has_suffix) and _BARE_TEST_NAME.search(head):
        return False

    if has_suffix and _TEST_REF.search(text):
        return True

    return bool(_SPAN_SOURCE.search(text)) or (bool(re.search(r"\.\w{1,6}$", text)) and (len(text) >= 4))

def has_quote(text: str) -> bool:
    """True when ``text`` carries an inline-code span at all."""
    return bool(_SPAN.search(text))

def is_source(line: str, *, statement: bool = False) -> bool:
    """True when ``line`` carries one of :data:`ANCHORS`.

    Two kinds of source, and the difference is the whole rule:

    * **line sources** — a ``file:line``, ``MUT:`` / ``mutate:``, an
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

    A ``#PR`` reference is a line source **only on a line that states
    nothing** (``statement = False``): ``#2813`` proves the PR exists, not what
    a sentence quantifies or claims about it, so on a statement line it
    satisfies nothing and the line needs another source — a reading table, a
    ``file:line``, a command result, a disclaimer or a directive. A pure
    reference line (``Filed upstream: hikari #1498.``) still counts: it is not
    judged, and it points the reader at where the claim's evidence lives.
    """
    text = line.lower()

    for word in DISCLAIMERS:
        text = text.replace(word, " " * len(word))

    # A pointer to a specific TEST CASE (`a.py::test_b`, `x.test.ts:88`) is
    # line-level: it opens at an assertion. A bare test FILE name is not — it
    # only says the file exists (see the span branch below).
    if _TEST_REF.search(text):
        return True

    for pattern in (_EVIDENCE, _URL, _FILE_LINE, comment_gate._EXPLICIT_ANCHOR):
        if pattern.search(text):
            return True

    # A `#PR` reference sources a line that asserts nothing. On a STATEMENT
    # line it is not a source: the reference proves the PR exists, while the
    # line asserts a quantity or a behaviour the reader cannot recompute from
    # that existence alone (the #315 review's live miss: `#2813` sourced the
    # whole of "… 32 refusals"). `statement` is judged by the gate's own
    # predicates in :func:`claim_kinds`, not by another word list.
    if (not statement) and _PR_REF.search(text):
        return True

    # A checksum WORD is a locator only when it is not itself negated, on
    # either side: "no sha256 anywhere" and "the sha256 is not shown" both name
    # the absence. Only the leading side was checked before, so the second one
    # sourced the line.
    for match in _CHECKSUM_WORD.finditer(text):
        before = _negation_state(text, match.start(), match.group(0)) != NONE
        after = _NEGATION_TAIL.search(text[match.end(): match.end() + 16]) is not None

        if (not before) and (not after):
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

#: A negation flips a disclaimer word only when it ATTACHES to it: `not an
#: inference`, `并非 inference`, `未未复算`. A negation in a neighbouring clause
#: does not travel — "but not round 1, so it was an inference" is a real
#: disclaimer, and a 40-character lookbehind read it as the opposite.
#:
#: The rule, in three parts: the negation must be among the two words directly
#: before the term, no sentence punctuation may sit between them, and English
#: negations only attach to an English term (a CJK negation in front of an
#: English word is a different clause).
_NEGATION = re.compile(r"(?:\bnot\b|n't\b|\bno\b|\bnothing\b|\bnone\b|\bnever\b|不是|并非|未$|无从)")
_NEGATION_TAIL = re.compile(r"\b(?:not|no|nothing|none|never)\b|n't\b|不是|并非|未$|无从")
_SCOPE_BREAK = re.compile(r"[,.!?;:—，。；：、（)）\n]")
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: What a negation can do to a disclaimer term.
ATTACHED = "attached"
DETACHED = "detached"
NONE = "none"

def _negation_state(text: str, at: int, term: str) -> str:
    """How the clause around the disclaimer starting at ``at`` uses negation.

    ``ATTACHED`` — a same-script negation touches the term (`not an inference`,
    `未未复算`, `并非 inference`): the term is refuted. ``DETACHED`` — the same
    negation sits in the term's own punctuation-delimited stretch without
    touching it (`not, inference`): the author wrote both, and the reading is
    that the term is refuted. ``NONE`` — no negation shares the stretch.

    One scan, not two: an earlier version also walked back over the marks before
    the term, which duplicated this reach and meant either copy could be broken
    while the pins stayed green.

    An English negation does not reach a CJK term (`This was not 未复算` says the
    Chinese disclaimer out loud); a CJK negation does reach an English one,
    because `并非 inference` is how that refutation is written.
    """
    text = text.lower()

    if _NEGATION_TAIL.search(term) is not None:
        # The term carries its own negation (`not re-run`, `not established`):
        # that IS the disclaimer, whatever else the stretch says. (Deleting this
        # early return changes no verdict — the stretch scan finds the negation
        # on the term itself and would otherwise have to special-case it — so it
        # is an optimisation, not a rule, and no pin claims otherwise.)
        return NONE

    if term.startswith("未") and text[: at].rstrip().endswith("未"):
        return ATTACHED

    low = at
    high = at + len(term)

    while (low > 0) and (not _SCOPE_BREAK.match(text[low - 1])):
        low -= 1

    while (high < len(text)) and (not _SCOPE_BREAK.match(text[high])):
        high += 1

    # The reader's stretch is the term's own punctuation-delimited run plus the
    # run immediately before it: `120 passed, not, inference` puts the negation
    # one mark away, and the punctuation is what makes it detached rather than
    # attached — both are refutations.
    prior_end = max(0, low - 1)
    prior_start = prior_end

    while (prior_start > 0) and (not _SCOPE_BREAK.match(text[prior_start - 1])):
        prior_start -= 1

    prior = text[prior_start: prior_end].strip()
    # A lone negation one mark away (`..., not, inference`) belongs to the same
    # refutation; a whole clause does not (`but not round 1, so ...`).
    prior_words = prior.split()
    # The prior run merges into the refutation when its LAST word is a negation
    # — there is no length cap. What rejects `but not round 1` is exactly that
    # last-word check (its last word is `1`, not a negation), not any cap: a
    # run of any length that ends in a negation (`that was not`) is one
    # refutation just the same.
    lone = bool(prior_words) and (_NEGATION_TAIL.fullmatch(prior_words[-1]) is not None)

    own = text[low: high]
    own_before = own[: own.rfind(term)] if term in own else own
    merged = text[prior_start: high] if lone else own
    stretch = merged[: merged.rfind(term)] if term in merged else merged
    term_is_cjk = bool(_CJK.search(term))

    # ATTACHED only looks at the words that really touch the term — a negation
    # across a punctuation mark is DETACHED, never attached.
    words = own_before.split()

    for word in reversed(words[-2:]):
        match = _NEGATION_TAIL.search(word)
        word_is_cjk = bool(_CJK.search(word))

        if (match is not None) and ((term_is_cjk == word_is_cjk) or word_is_cjk):
            return ATTACHED

    # The same stretch, without touching: DETACHED.
    for match in _NEGATION_TAIL.finditer(stretch):
        if term_is_cjk == bool(_CJK.search(match.group(0))):
            return DETACHED

    return NONE

def disclaims(line: str) -> bool:
    """True when ``line`` says out loud that the figure was not recomputed.

    A disclaimer counts only where no negation shares its clause: attached
    (`not an inference`) and detached (`not, inference`) both refute the term,
    and refuting the disclaimer is not disclaiming.
    """
    text = line.translate(_INVISIBLE).lower()

    for word in DISCLAIMERS:
        start = 0

        while True:
            at = text.find(word, start)

            if at < 0:
                break

            if _negation_state(text, at, word) == NONE:
                return True

            start = at + len(word)

    return False

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
        reason = reason.rstrip("->").strip()

    # A reason of dots, underscores or one repeated character is not a reason:
    # printing `🔓 body-gate-ok: ....` would make the exemption silent again.
    if DIRECTIVE_REASON_SUBSTANCE.search(reason) is None:
        return None

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
_READING_NUMBER = r"-?\d+(?:[.,]\d+)*"
_READING_UNIT = r"%|ms|s|MiB|GiB|KiB|KB|MB|GB|px|B"
#: The count noun a runner prints after its number (`-3 passed`, `1,204 passed`).
#: Deliberately the runners' own vocabulary, not English: a cell saying
#: `3 passed and 5 failed` stays prose because the conjunction is left over.
_READING_TAIL = r"passed|failed|skipped|xfailed|xpassed|tests?|errors?|warnings?"
#: A reading: a number with an optional unit and optional count noun, an
#: approximate one (`about 9 KB`), a `#ref`, or an em dash. The approximation
#: prefix cannot stand alone — `about the build` is prose, so a number is
#: required right after it.
_READING_CELL = re.compile(rf"(?:(?:about|approximately|roughly|~|\u2248)\s*)?(?:{_READING_NUMBER}(?:\s*(?:{_READING_UNIT}))?(?:\s+(?:{_READING_TAIL}))?|#\d+)|\u2014|-")  # py-style-ok: single-line-statement: one alternation table

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

        if is_source(part.text, statement = bool(claim_kinds(part.text)[0])) or disclaims(part.text):
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
