#!/usr/bin/env python3
"""body-gate - every number and behaviour claim in a PR body must name its source.

*Why this exists:* the recorded-exception PRs of this wave (hikari #1476 /
#1478 / #1482 / #1485 / #1503 / #1506, chest #2813 / #2815, tairitsu #154) were
merged with **correct production code** and prose in the body that review
rounds kept refuting. The comment gate (``celestia_devtools.lint.comment_gate``,
devtools #306) covers behaviour claims in *test-file comments*; PR-body prose —
where the figures and the causal sentences live — had no mechanical check at
all. The failure named by the operators themselves: *when a number does not add
up, invent a reason instead of writing "cause not established"*. A discipline
that depends on remembering is not a gate.

*Boundary - what this is NOT:* it cannot judge whether a claim is **true**. A
verdict needs the reference semantics, and a text scan has none (the blind spot
``doc_lint`` documents about itself; a guess would manufacture false greens).
It checks one mechanical proxy instead: **wherever the body states a quantity
or claims behaviour, that statement must be able to point at something a reader
can recompute**, or say out loud that it was not recomputed. Nothing here reads
meaning, so nothing here can be talked into agreeing with itself.

*What counts as a claim*

* **quantitative** — a number carrying a unit or a counter word, in either
  language: ``N passed``, ``N/N``, ``rc=N``, ``N files``, ``N failed``, ``N 处``,
  percentages, durations, counts. A bare integer with no unit is NOT a
  quantity; the next class is what covers the bare numbers that still need a
  source. A figure written inside backticks is still a claim: a quoted figure
  is the claim repeated, not its source.
* **a sentence that names a thing is not a claim** (:func:`names_a_thing`): a
  line carrying a code token (an inline-code span, a flag, a path, a filename)
  and no measured outcome describes the code — `` `format-markdown` exits 1 on
  frame glyphs`` is naming the tool; "the suite went from 12 failed to 0" is
  asserting a run, and is judged. Review round 2 found the difference deciding
  whether an honest body was refused.
* **behavioural** — the comment gate's own vocabulary, imported unmodified
  (:data:`BEHAVIOUR_TERMS`: ``measured`` / ``verified`` / ``reds`` /
  ``does NOT`` / ``catches`` / ``only when`` / ``never`` / ``抓住`` / ``抓不住`` /
  ``只在`` / ``只有…时才``). One list, one source: a term added there is in
  force here and the two gates cannot drift.
* **unquoted digit** — a number written as prose instead of quoted from the
  output that produced it (``3 of 4``, ``two reviewers``, ``三处``). It is its
  own class because that is the organically measured shape of the incidents:
  the waiver bodies state an arithmetic relation between run outputs and quote
  the output for every figure EXCEPT the one carrying the wrong claim. A scan
  that skips numbers-without-units skips exactly those. Excluded by
  construction (each measured on the corpus): a heading line, CJK pronoun and
  ordinal forms (``这一行`` / ``第一版`` / ``唯一``), a dimension (``220×220``), a
  component version (``0.55.463``), a figure glued to its unit or a ratio
  (``3.3 MB`` / ``8/310``), and a ``§`` reference — see
  :func:`unquoted_digits`.

*A statement is satisfied by* one of three things, each looked for on the
statement's own line and the line above or below it:

* **an anchor** — ``MUT:<id>`` / ``mutate:<slug>`` (the comment gate's own
  pattern, imported), an ``evidence:`` marker, a ``#PR`` reference, a URL, a
  commit SHA, a checksum word (``sha256`` / ``md5``), a ``file:line``
  reference, a path with a directory in it (:func:`path_is_source` rejects the
  prose connectors ``and/or`` / ``n/a`` and date pairs), or an inline-code span
  that names something (a filename, a two-word command, a flag, a call, a
  camelCase identifier — :func:`span_is_source`);
* **a disclaimer** — ``未复算`` / ``未验证`` / ``未测量`` / ``原因未查明`` /
  ``unverified`` / ``not established`` / ``not re-run`` / ``not measured`` /
  ``inference`` / ``inferred`` / ``not reproduced``. Saying "I did not
  recompute this" is a valid answer; writing a cause you cannot check is not,
  and that is the entire disease;
* **a captured table** — a statement INSIDE a markdown table is satisfied when
  that table holds a verdict row (its LAST cell is ``3 red`` / ``4 green``, see
  :func:`row_is_verdict`), a captured telemetry row, or the statement's own row
  is nothing but captured cells (a row of numbers, a cell that is only a
  reading such as ``9041 bytes``). The table is the evidence block when it is
  captured output; a prose line saying ``rc=0``, or a row whose last cell is a
  sentence, is not (see :func:`is_captured_verdict`), so a table cannot become
  a blanket exemption.

The window is one line up and one line down on purpose. This gate runs against
real bodies, and a correct body very often puts the sentence first and the
point last (``5. Restated from the raw counts: …`` / ``` `before` = 10 in round
1 ```). Every real false claim in the waiver corpus sits at most one line from
its narrative context, so a wider window buys no recall here and costs
precision; the window is the tightening that carries, and its reach is not
ours to widen on a hunch.

*Scope — deliberately narrow:*

* **PR body prose only.** Excluded from the scan: fenced blocks (a command or a
  captured output is the evidence itself — a claim BESIDE a fence is satisfied
  by it), indented code blocks, block quotes (the template shape the verify
  record already refuses), HTML comments, headings (a title is not a claim),
  the gate entry lines (``body-gate-waiver:`` / ``comment-gate-waiver:`` /
  ``artifact-gate-waiver:``) and the ``verify:`` record line (machine state,
  not prose). A body whose prose states nothing is a **loud skip**: it is
  announced on stdout, the verdict is rc=0, and the skip is a printed line
  rather than a silent green. The rationale is measured, not assumed: an
  ordinary description ("load the Mermaid component through the host client,
  tests updated") states no quantity and makes no behaviour claim, and a gate
  that reds it would be switched off within a week — the noise-death argument
  the artifact gate's design records.
* **A body with no prose at all is refused** (rc≠0). That is NOT a skip: an
  empty body is either a probe or a truncated payload, a body that is only its
  ``verify:`` record is not a description, and a body whose every line is
  fenced or quoted had nothing to scan. No reason to believe anything about
  the change appears anywhere in those. The refusal lives in
  :func:`check_body`, so the merge gate inherits it.

*Waiver:* ``--waiver`` does not cover this gate — that channel suppresses
exactly one refusal (``verify record c3 < 3``), and a channel whose purpose is
"the description was shaky" would be self-defeating here. The only exception is
``body-gate-waiver: <reason>`` in the PR body, read by the merge gate through
the SAME parser as ``comment-gate-waiver:`` (``pr_gate._gate_waiver``: one
entry, prose only, no fence / indent, a 20-character floor) and announced on
stdout. It is never a silent pass, and it never reaches this module.

*CLI:* ``lint-pr-body [--body FILE | --pr N --repo OWNER/NAME] [--quiet]``,
also reachable as ``celestia-devtools lint-pr-body``.
Exit codes: 0 clean or loud skip, 1 unsourced statements, 2 usage or an
unreadable body (a body that cannot be read must never read as clean).
"""

from __future__ import annotations
import argparse
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
#: copied. A copy would be a second place to edit and a guaranteed drift.
BEHAVIOUR_TERMS: tuple[str, ...] = comment_gate.BEHAVIOUR_TERMS

#: Quantities: a number WITH its unit or counter word, in both languages. Each
#: alternative is independent; the tuple exists so a reader can see the whole
#: surface in one place and tests can pin it.
QUANTITY_PATTERNS: tuple[str, ...] = (  # py-style-ok: single-line-statement: one data table, one entry per line
    # 120 passed / 149 passed (16) / 12 failed / 3 green / 2 red
    r"\b\d[\d,]*\s*(?:passed|failed|skipped|green|red|checks?|assertions?|findings?|rows?|claims?)\b",
    # 16/16, 3/4, 46/46
    r"(?<![\d/§])\d[\d,]*\s*/\s*\d[\d,]*(?![\d/])",
    # rc=0 / exit 0 / exit code 1
    r"\b(?:rc|exit(?:\s*code)?)\s*[=:]\s*-?\d+",
    # 16 files / 2 crashes / 6 tests / 4 components
    r"\b\d[\d,]*\s*(?:files?|tests?|cases?|runs?|rounds?|pins?|calls?|lines?|chars?|crash(?:es)?|faults?|components?|screenshots?|probes?)\b",
    # 2.9s / 120 ms / 0.42 seconds / 119 s
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:ms|µs|us|ns|s|sec|secs|seconds?|min|mins|minutes?|hours?)\b",
    # 85% / 12.5 %
    r"\d[\d,]*(?:\.\d+)?\s*%",
    # checksums quoted as a claim ("same sha256", "md5 equal")
    r"\bsha-?(?:1|256|512)\b|\bmd5\b",
    # 4 处 / 3 个文件 / 6 行 / 2 次 / 5 个 crate / 16 个
    r"\d[\d,]*\s*(?:处|次|个|行|条|项|例|件|组|份|字|笔|步|轮|个文件|章|节)",
    # 三个测试: a CJK numeral before a counter word is the same claim.
    # Two characters minimum: a lone 一/二/三/十 is a pronoun or an ordinal far
    # more often than a count (一是…/二是…/最后一项), measured on the corpus.
    r"(?:[一二两三四五六七八九]|十[一二三四五六七八九]?|二十|三十)(?:处|次|个|行|条|项|例|件|组|份|步|轮)",
    # 2 crashed / 6 failed — one ASCII digit beside a CJK counter.
    r"\d[\d,]*\s*(?:crash|fail)"
)
QUANTITY = re.compile("|".join(f"(?:{pattern})" for pattern in QUANTITY_PATTERNS), re.IGNORECASE)

#: A number written as prose. See the module docstring: this class is what
#: covers the waiver corpus' prose figures, so it is the scan's recall centre.
#: A candidate is a space-delimited number token outside a word, a path, a
#: version or a section reference. Exclusions that need look-ahead (a figure
#: glued to its unit, a dimension, a ratio) live in :func:`unquoted_digits`.
UNQUOTED_DIGIT = re.compile(r"(?<![A-Za-z0-9_.§/~-])\d+(?:[,.]\d+)*")

#: Words that take a bare number as an OBJECT (`exits 1`, `line 12`, `version
#: 0.7`): a count of code objects, not a measured quantity.
_HOST_WORD = re.compile(r"(?i)^(?:exits?|lines?|steps?|modes?|items?|index|pages?|issues?|pr|parts?|phases?|levels?|tiers?|versions?|no\.)$")

#: CJK numeral forms that are quantifiers, pronouns or ordinals rather than
#: counts: 这一行 / 那一处 / 第一版 / 唯一 / 一番 / 一下 / 每三个.
_CJK_PRONOUN = re.compile(r"[这那每某唯一第一二三四五六七八九十][一二三四五六七八九十]?")
_CJK_ORDINAL = re.compile(r"第[一二三四五六七八九十\d]+")

#: The disclaimer vocabulary: saying out loud that a figure was not recomputed
#: (or that its cause was not established) is a VALID answer — precisely the
#: discipline the waiver corpus was missing.
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

#: Where a claim can point for its source. One list, used by one test.
MARKERS: tuple[re.Pattern[str], ...] = (  # py-style-ok: single-line-statement: one data table, one entry per line
    # MUT:<id> / mutate:<slug> — the comment gate's own pattern, imported so
    # the two gates cannot disagree about what a mutation reference looks like.
    comment_gate._EXPLICIT_ANCHOR,
    re.compile(r"\bevidence\s*:", re.IGNORECASE),
    re.compile(r"(?<![\w#])#\d+\b"),
    re.compile(r"https?://\S+"),
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),
    re.compile(r"\bsha-?(?:1|256|512)\b|\bmd5\b", re.IGNORECASE),
    # file:line / file:line-line — the reader opens that line.
    re.compile(r"[\w./-]+\.(?:rs|ts|tsx|js|jsx|py|md|toml|yml|yaml|json|sql|wit|vue|sh|just|lock)\s*:\s*\d+"),
    # A path with a directory. The marker is the exact SHAPE; which shapes
    # count as provenance is `path_is_source`, because prose connectors
    # (`and/or`, `before/after`, `n/a`) and date pairs (`2026-11-15/16`) also
    # look like `a/b` and certified whole lines until review round 2.
    re.compile(r"(?<![\w./~-])(?:[\w-]+/)+[\w.-]*[\w-]|(?<![\w./~-])[\w-]+/[\w.-]*[\w-]")
)

#: A markdown table row: the cells decide whether the row is a verdict row.
_ROW = re.compile(r"^\s*\|.*\|[ \t]*$")
#: A table's delimiter row (`|---|---|`), which is structure rather than a row.
_ROW_SEPARATOR = re.compile(r"^\s*\|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|\s*$")
_ROW_VERDICT = comment_gate._ROW_VERDICT

#: The gate's own entry lines: an exception statement is not a claim, and the
#: number in its reason must not need a source of its own. Anchored at the line
#: start (the exemption's own shape) so a sentence that merely mentions the
#: token is still scanned.
_WAIVER_ENTRY = re.compile(r"^[\s*>#]*[*_`]*(?:body|comment|artifact)-gate-waiver[*_`]*\s*[:：]", re.IGNORECASE)

#: The verify record line. Its fields are machine state owned by the record
#: checker (`rounds=6` / `c3=3`), and the trailing prose on that line is part
#: of the record's shape, not a claim about the change.
_RECORD_LINE = re.compile(r"^[ \t>#*_+.\-`]*(?:\d+[.)][ \t]+)?[*_ \t#>.`-]*(?:verify|合计)[ \t*_`]*\**[ \t]*[:：]", re.IGNORECASE)

#: A fenced block opener: ``` or ~~~ with an optional info string.
_FENCE = re.compile(r"^(`{3,}|~{3,})")

#: Zero-width characters a disclaimer could hide behind (the comment gate's
#: filter, reused rather than re-derived).
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
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and not self.findings

@dataclass(frozen = True)
class _line:
    """One line of the body that is subject to the claim scan.

    ``section`` is the index of the "prose paragraph" the line belongs to (a
    blank line or a heading starts a new one) and ``tabular`` marks a line that
    sits inside a markdown table (a table's opening row and its delimiter).
    """

    number: int
    text: str
    section: int = 0
    tabular: bool = False

    def with_section(self, section: int, *, tabular: bool) -> "_line":
        """This line, re-labelled by the paragraph walker."""
        return _line(number = self.number, text = self.text, section = section, tabular = tabular)

def prosaic(body: str) -> tuple[list[_line], list[str], list[_line], tuple[str, ...]]:
    """The lines of ``body`` subject to the claim scan, plus notes + fence lines.

    Excluded in order: fenced blocks (an UNCLOSED fence swallows the rest — the
    conservative reading the record checker takes), indented code blocks, block
    quotes, HTML comments, headings, gate entry lines and the verify record
    line. Every exclusion is a *shape that reads as a template or a capture*,
    never a judgement about content.
    """
    lines: list[_line] = []
    notes: list[str] = []
    fence: str | None = None
    in_comment = False
    # FENCE lines are EVIDENCE, not prose: never scanned as a claim, but their
    # PRESENCE beside a claim is what "the output below" means for a reader, so
    # they are tracked as evidence positions (see `sourced`).
    evidence: list[_line] = []
    #: Lines excluded as indented / quoted / heading. They are not scanned, and
    #: a body whose ONLY content was excluded must not read as "no statements"
    #: (that lane is a skip): `check_body` refuses when every prose line that is
    #: not a heading was dropped (review round 2, finding 5).
    dropped: list[int] = []

    for number, raw in enumerate(body.splitlines(), start = 1):
        stripped = raw.strip()
        opener = _FENCE.match(stripped)

        if fence is not None:
            evidence.append(_line(number = number, text = raw, section = -1, tabular = False))

            if (opener is not None) and (opener.group(1)[0] == fence) and (set(stripped) == {fence}):
                fence = None

            continue

        if opener is not None:
            fence = opener.group(1)[0]
            evidence.append(_line(number = number, text = raw, section = -1, tabular = False))
            continue

        if in_comment:
            _, closed, trailing = stripped.partition("-->")

            if not closed:
                continue

            in_comment = False
            stripped = trailing.strip()

        if "<!--" in stripped:
            before, _, after = stripped.partition("<!--")
            _span, closed, trailing = after.partition("-->")
            stripped = (before + trailing).strip()

            if not closed:
                in_comment = True

            if not stripped:
                continue

        lead = raw[: len(raw) - len(raw.lstrip())]
        in_table_row = _ROW.match(stripped) is not None

        # A row's leading spaces are markdown alignment inside the cell, not an
        # indented code block: only a line that is NOT a table row can be one.
        indented = (not in_table_row) and (("\t" in lead) or (len(lead) >= 4))

        if indented or stripped.startswith(">") or stripped.startswith("#"):
            dropped.append(number)
            continue

        if _WAIVER_ENTRY.match(stripped) or _RECORD_LINE.match(raw):
            # An exemption ENTRY is not prose. A sentence that merely MENTIONS
            # the token stays in the scan — the old "any line containing the
            # token" test was a one-line, unannounced hole (review round 2).
            continue

        lines.append(_line(number = number, text = raw))

    if fence is not None:
        notes.append("the body ends inside an unclosed code fence — everything after the opener was treated as code (the conservative reading of a truncated body)")

    # Paragraph and table labelling. A "section" is a run of prose lines not
    # interrupted by a blank line, a heading or a table block; a "table block"
    # is a run of `|` lines that contains a delimiter row. Measured: a captured
    # table is an evidence block (its cells are the output), so its rows are
    # re-labelled as one table and never judged as prose statements; a run of
    # pipe lines WITHOUT a delimiter row is not markdown table syntax, and stays
    # prose — that is the negative control for this rule.
    labelled: list[_line] = []
    section = 0
    previous_blank = True
    index = 0

    while index < len(lines):
        row = lines[index]
        stripped = row.text.strip()

        if _ROW.match(stripped):
            start = index

            while (index < len(lines)) and _ROW.match(lines[index].text.strip()):
                index += 1

            block = lines[start: index]
            is_table = any(_ROW_SEPARATOR.match(part.text.strip()) for part in block)

            if is_table:
                section += 1
                labelled.extend(part.with_section(section, tabular = True) for part in block)
                previous_blank = False
                continue

            labelled.extend(part.with_section(section, tabular = False) for part in block)
            previous_blank = False
            continue

        if not stripped:
            section += 1
            previous_blank = True
            labelled.append(row.with_section(section, tabular = False))
            index += 1
            continue

        if previous_blank:
            section += 1

        previous_blank = False
        labelled.append(row.with_section(section, tabular = False))
        index += 1

    omitted = ("the body's only prose was indented or quoted, so nothing could be scanned — an indented block is a template, not a description",) if (not lines) and dropped else ()

    return labelled, notes, evidence, omitted

def behaviour_terms(text: str) -> tuple[str, ...]:
    """The behavioural vocabulary ``text`` hits (the comment gate's own test)."""
    return comment_gate.behaviour_terms(text)

def unquoted_digits(text: str) -> tuple[str, ...]:
    """Bare prose numbers in ``text`` — the scan's recall centre.

    A candidate is a space-delimited number token outside a word, a version, a
    path or a section reference. Excluded, each for a measured reason (the
    exclusions were derived from the waiver corpus, not imagined):

    * ``§`` / ``#`` / ``-`` / ``/`` right before it — a section reference, a PR
      reference, a negative number or a ratio already matched as a quantity;
    * a CJK pronoun or ordinal word before it — ``这一行`` / ``第一版`` /
      ``每三个``;
    * a CJK word character directly before it with prose after it — the digit
      is part of a name, not a count;
    * a figure glued to its unit, a ratio, a dimension or a CJK counter
      (``3.3 MB`` / ``8/310`` / ``220×220`` / ``9行``) — those are the quantity
      patterns' job, and this class is about prose arithmetic.
    """
    found: list[str] = []

    for match in UNQUOTED_DIGIT.finditer(text):
        before = text[: match.start()]
        number = match.group(0)
        tail = before[-1:]
        following = text[match.end():]

        # A figure glued to a unit (`3.3 MB`), a ratio or a dimension (`8/310`,
        # `220×220`) or followed by a unit word (`48400 px`) belongs to the
        # quantity patterns, not to prose arithmetic.
        if re.match(r"^\s?(?:%|[×*]|\d+\s*[×*]|/\s*\d|[A-Za-z]{1,6}\b(?![\s,.;:)]|$))", following):
            continue

        if (following[:1] != " ") and (not re.match(r"^[\u4e00-\u9fff]", following)):
            continue

        if tail in ("§", "-", "—", "(", "[", "/", ">", "≥", "<", "≤", "="):
            continue

        word_before = before.rstrip().split(" ")[-1] if before.strip() else ""

        # A comparison (`>= 1.36`) is a version bound, not a measurement; a
        # version (`0.7.0`) and a unit word's object (`exits 1`) are not either.
        if before.rstrip().endswith((">=", "<=", ">", "<", "=")):
            continue

        if re.fullmatch(r"\d+(?:\.\d+)+", number) or _HOST_WORD.match(word_before):
            continue

        if _CJK_PRONOUN.match(tail) or _CJK_ORDINAL.match(before[-3:] + number):
            continue

        if ("\u4e00" <= tail <= "\u9fff") and not re.match(r"^\s*(?:处|次|个|行|条|项|例|件|组|份)", following):
            continue

        found.append(number)

    return tuple(found)

#: A code token: an identifier, a flag, a call, a path, a filename, or a
#: sentence that NAMES one. A line carrying one is naming a thing, not
#: asserting an outcome about it (`` `format-markdown` exits 1 on … `` describes
#: the tool; "The suite went from 12 failed to 0" asserts a run).
_CODE_TOKEN = re.compile(r"`[^`\n]+`|\b[a-z_]+(?:[_-][\w-]+)+\b|\b\w+\.[A-Za-z0-9]{1,6}\b\s*(?=[`(]|$)|\b\w+\s+--?[a-z][\w-]+")
#: A measured outcome: a counter word or a run word next to a figure or a flag.
_OUTCOME = re.compile(r"\b\d[\d,]*\s*(?:passed|failed|red|green|crash|error|warning|hits?)\b|\b(?:exit|rc)\b\s*[=:]?\s*-?\d", re.IGNORECASE)

#: What makes a span a code NAME rather than a word or a path: a call, an
#: underscore, a flag, a camelCase identifier, a dotted member (`a.b.c`).
_SPAN_CODE = re.compile(r"[\w.]+\(|_|--?[a-z][\w-]*|[a-z][A-Z]|^[a-z]+(?:\.[a-z][\w]*)+$")
#: A tool named WITH its version (``` `vite 8.2` ```): the tool is the subject.
_TOOL_VERSION = re.compile(r"[A-Za-z][\w-]*\s+\d+(?:\.\d+)*")

#: Figures written as arrows or ranges (`2 → 0 → 2`, `16→17`) are a COUNT
#: SEQUENCE — a change in a counter — not a measured quantity.
_SEQUENCE = re.compile(r"\d+(?:\s*(?:→|->|=>)\s*\d+)+")

def names_a_thing(line: str) -> bool:
    """True when ``line`` NAMES a code thing instead of claiming an outcome.

    Two shapes count, and both need the naming to be the line's subject rather
    than a citation:

    * an un-backticked code token — an underscored identifier or a ``--flag`` —
      anywhere on the line;
    * a code span whose content is an identifier, a call or a flag — an
      ``expect(…)``, a ``createSurface``, a ``settings.allow-scripts`` — anywhere
      on the line: a reader can open it, and it is the line's subject;
    * a span that OPENS the line with only punctuation or a result marker after
      it (``` `format-markdown` exits 1 on … ```), even when the span is a
      bare word.

    and none may sit beside a measured outcome (`` `vite build` printed 12
    failed `` is a claim about a run). A bare PATH citation mid-sentence
    (`` the harness, see `docs/en/x.md` ``) silences nothing: round 5 measured
    one path making any "verified" on the line pass.
    """

    if _OUTCOME.search(line):
        return False

    if re.search(r"\b[a-z_]+(?:[_-][\w-]+)+\b|\b\w+\s+--?[a-z][\w-]+", _SPAN.sub(" ", line)):
        return True

    # A span is a citation, not a subject, when it is a bare PATH
    # (`docs/en/x.md`): it is a reference to a place, not the thing described.
    for match in _SPAN.finditer(line):
        span = match.group(0).strip("`")
        before = line[: match.start()]
        after = _RESULT_MARKER.sub(" ", line[match.end():])
        subject = (re.search(r"[A-Za-z\u4e00-\u9fff]", before) is None) and (re.search(r"[A-Za-z\u4e00-\u9fff]", after) is None)

        # A span NAMES code when it is an identifier, a call or a flag — not a
        # bare path (a citation) and not a plain English phrase (emphasis).
        names_code = bool(_SPAN_CODE.search(span)) or bool(_TOOL_VERSION.fullmatch(span))

        if subject or names_code:
            return True

    return False

def claim_kinds(line: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(kinds, terms)`` for one line of prose; empty ``kinds`` = no claim.

    Two rules, and they are independent:

    * **a figure is a claim** — a quantity carrying its unit or a counter word,
      unless EVERY quantity on the line is a code span (``` `16 passed (16)` ```
      is the run's output pasted, not prose), and any bare prose figure;
    * **naming a thing makes no BEHAVIOUR claim** (:func:`names_a_thing`) — a
      line carrying a code token and no measured outcome describes the code.
      It does not excuse a figure: `` \`pr_gate.py\` now runs 6 gates `` names
      the module and states a count, and only the count is judged.
    """
    kinds: list[str] = []
    terms = behaviour_terms(line)
    naming = names_a_thing(line)
    spans = [(match.start(), match.end()) for match in _SPAN.finditer(line)]
    quantities = list(QUANTITY.finditer(line))
    quoted = bool(quantities) and all(any((start <= part.start()) and (part.end() <= end) for start, end in spans) for part in quantities)

    if terms and naming:
        terms = ()

    if quantities and (not quoted):
        kinds.append("quantity")

    if terms:
        kinds.append("behaviour")

    if (not kinds) and unquoted_digits(_SEQUENCE.sub(" ", line)):
        kinds.append("unquoted digit")

    return tuple(kinds), terms

def has_disclaimer(line: str) -> bool:
    """True when ``line`` says out loud that the figure was not recomputed."""
    return any(word in line.translate(_INVISIBLE).lower() for word in DISCLAIMERS)

def is_table_row(line: str) -> bool:
    """True when ``line`` is a markdown table row."""
    return bool(_ROW.match(line))

def is_outcome_row(line: str) -> bool:
    """True when ``line`` is a full-width row holding a run OUTCOME word.

    ``pass`` / ``fail`` / ``red`` / ``green`` are the gate vocabulary, but only
    when the row is not a claimed-behaviour cell of a 2/3-column table: a
    distribution table (``| QR | ... | already caught ... |``) is describing
    the code, not quoting a run. A full-width row (5+ cells) is the telemetry
    shape (``| before | 0 | 0 | 0 | 3 |``), and that is treated as a captured
    output — its numbers are the evidence, so they are not prose claims.
    """

    if not is_table_row(line) or not _ROW_VERDICT.search(line):
        return False

    cells = len(line.strip().strip("|").split("|"))
    return cells >= 5

def is_captured_verdict(line: str) -> bool:
    """True when ``line`` is a TABLE row holding a run outcome or an ``rc=0``.

    Deliberately table-only: a prose line such as ``Status: rc=0 on the last
    run.`` is a claim about a run, not the run's output, so it must not be read
    as captured evidence (review round 2 measured the prose form being treated
    as one).
    """

    if not is_table_row(line):
        return False

    # The verdict has to CLOSE the row (`| run | 3 green |`); a word found
    # anywhere in a prose cell is not a captured outcome (round 6).
    return row_is_verdict(line) or bool(re.search(r"\brc\s*=\s*-?\d+\s*$", line.strip().rstrip("|").strip()))

def row_is_verdict(line: str) -> bool:
    """True when a table row CLOSES with a verdict cell (``| 3 red |``).

    The comment gate's row rule, applied to a body table: the verdict has to be
    the last cell, so a row that merely says the word ``green`` somewhere in its
    prose does not count (review round 2 measured a whole-line search turning
    ``Adding a token to an unchanged list reddens this line`` into a verdict).
    """

    if not is_table_row(line):
        return False

    cells = [cell.strip().strip("*`_ ") for cell in line.strip().strip("|").split("|")]

    if not cells:
        return False

    # The last cell must BE the verdict, not contain the word: `likely to fail`
    # is prose (round 4).
    return bool(re.fullmatch(r"(?i:(?:\d+\s*(?:red|green)|red|green|stays green|pass|passes|failed|fails|killed|survived))|(?:红|绿)", cells[-1]))

def is_captured_cell(line: str) -> bool:
    """True when a table row's cells are captured output rather than prose.

    Three shapes, each measured:

    * a run-outcome word (``pass`` / ``fail`` / ``red`` / ``green`` / ``rc=0``);
    * EVERY cell is nothing but a number (``| 0 | 0 | 0 | 3 |``): a numeric row
      is the reading itself. One numeric cell among prose ones (``| before | 0
      occurrences | yes |``) is NOT enough, and neither is a row whose first
      cell is a word (``| before | 0 | 0 | 0 | 3 |``) — that one is a prose
      row with a label, so its table must carry a verdict row to be trusted
      (that is how shittim-chest #2821's telemetry table passes, and why its
      out-of-table restatement does not);
    * a cell that is a bare reading with its unit (``9041 bytes`` /
      ``220×220``): a cell holding only the measurement, no sentence around it.

    A prose cell (``| wrapper evaluation fails | 0 bytes — blank page | 1 |``
    is mixed, so it stays prose by the first rule) still has to carry a source:
    this helper is deliberately narrow, and ``test_a_prose_table_still_needs_
    sources`` is the negative control that keeps it narrow.
    """

    if is_captured_verdict(line) or row_is_verdict(line):
        return True

    if not is_table_row(line):
        return False

    cells = [cell.strip().strip("*`_ ") for cell in line.strip().strip("|").split("|")]

    numeric = bool(cells) and all(re.fullmatch(r"-?\d[\d,.]*", cell) for cell in cells)

    # A row of label | reading | reading… is a metrics row: after the first
    # label cell, every cell is empty, a number (with an optional unit) or a
    # `#ref`. A prose cell (`9 of 10 builds break`, `waiver PR`) is not a
    # reading, so that row stays prose and its table cannot become a blanket
    # exemption.
    measured = bool(cells) and (len(cells) > 1) and all((not cell) or bool(_METRIC_CELL.fullmatch(cell)) for cell in cells[1:])

    # A cell that is only a reading with its unit (`9041 bytes`, `220×220`).
    reading = any(re.fullmatch(r"-?\d[\d,.]*\s*(?:bytes|px|KB|MB|GB|fps|ms)", cell, re.IGNORECASE) or re.search(r"\d+\s*[×x]\s*\d+", cell) for cell in cells)

    return numeric or measured or reading

def is_table_verdict(line: str) -> bool:
    """True when ``line`` is a table row that closes with a verdict cell."""
    return is_table_row(line) and bool(_ROW_VERDICT.search(line))

#: Prose that marks a command's OWN result (`→ rc=0`, `: 0`, `-> pass`): the
#: leftover after stripping a command span may be this and nothing else.
_RESULT_MARKER = re.compile(r"\brc\s*=\s*-?\d+|\bexit\s*\d+|→|->|=>|\d+|[:;,.=|`\s]")

#: A metrics-table cell: a bare number (with an optional unit) or a `#ref`.
_METRIC_CELL = re.compile(r"-?\d+(?:\.\d+)?\s*(?:%|ms|s|MiB|GiB|KB|MB|GB|px|B)?|#\d+|—|-")

#: A backtick span. Extraction runs FIRST and satisfaction is tested per span:
#: asking a regex to certify "this span holds a word" with lookaheads inside the
#: span match reached across the closing backtick into the next span (measured
#: against a real body: ``` `12→14→11` … `before` ``` certified the first span
#: because the second one held a letter). A two-pass read cannot do that.
_SPAN = re.compile(r"`[^`\n]+`")
#: A span is a SOURCE when it holds a word: an identifier, a command, a value
#: name. A span holding only figures or arrows is the claim itself quoted.
_SPAN_WORD = re.compile(r"[A-Za-z_]")
#: What a runnable / openable span looks like: a path, a filename, a command
#: line, a flag, or an identifier with a call. Anything else in backticks — a
#: word, a phrase, a phrase with an article — is emphasis, and emphasis is not
#: a source (review round 2: `not-a-command` certified a figure on its line).
_SPAN_SHAPE = re.compile(r"/|\.[A-Za-z0-9]{1,6}$|--?[a-z][\w-]*|\w\(|\w_\w|=|::|\b[a-z][a-z0-9]+(?:[A-Z][\w]*)+")

def code_spans(line: str) -> tuple[str, ...]:
    """The inline-code spans of ``line``, without their backticks."""
    return tuple(match.group(0).strip("`") for match in _SPAN.finditer(line))

#: A figure inside a span (``120 passed``, ``3/4``, ``rc=0``). A span carrying
#: one quotes the claim instead of naming its source: the source of a number is
#: the command or the line that produced it, never the number repeated in
#: backticks (review round 2 measured that self-certification).
_SPAN_FIGURE = re.compile(r"\d")

#: A filename with an extension (``api.rs:463``, ``useHighlight.ts``).
_FILENAME = re.compile(r"^[\w./:-]*[^\s/]\.[A-Za-z0-9][\w]*\s*(?::\s*\d+)?$")
#: Two English words in backticks (`gate green`): emphasis, not a command.
_EMPHASIS_WORDS = r"(?:stays?|gate|the|all|is|green|red|passes?|fails?|this|that|here)"
_ENGLISH_EMPHASIS = re.compile(r"(?i)" + _EMPHASIS_WORDS + r"\s+(?:green|red|honest|correct|true|false|done|good|bad|slow|fast)")
#: A two-word command (``vite build``, ``cargo check``).
_TWO_WORD_COMMAND = re.compile(r"^[a-z][a-z0-9_-]*\s+[a-z][\w-]*$")
#: Code markers that make a single token a name rather than a word.
_CODE_MARKER = re.compile(r"--|_|\(|::|/|=")

#: A bare path reference (``` `docs/en/x.md` ```): a CITATION, not the line's
#: subject and not its evidence (round 6).
_CITATION = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$|^[\w-]+\.[A-Za-z0-9]{1,6}$|^[A-Za-z][\w-]*\s+\d+(?:\.\d+)*$")

def is_citation(span: str) -> bool:
    """True when the span is a bare path / filename rather than a code name."""
    text = span.strip()

    return bool(_CITATION.fullmatch(text)) and (not _SPAN_CODE.search(text))

def span_is_source(span: str) -> bool:
    """True when a span names something a reader can look up or re-run.

    Four shapes, and nothing else: a filename, a two-word command, a token
    carrying a code marker (a flag, an underscore, a call, a path, an
    assignment), or a camelCase identifier. A word, a phrase, or a
    hyphenated English phrase (``` `before` ```, ``` `gate green` ```,
    ``` `not-a-command` ```) is emphasis — and a span holding a figure is the
    claim itself quoted.
    """
    text = span.strip()

    if not _SPAN_WORD.search(text) or _SPAN_FIGURE.search(text):
        return False

    if _FILENAME.match(text):
        return True

    if " " in text:
        # A phrase carrying a code marker is a command (`cargo check
        # --all-targets`); a bare two-word lowercase pair is one too (`vite
        # build`) unless it is two English words with no marker (`gate green`).
        if _CODE_MARKER.search(text):
            return True

        return bool(_TWO_WORD_COMMAND.match(text)) and (_ENGLISH_EMPHASIS.fullmatch(text) is None)

    if not _SPAN_SHAPE.search(text):
        return False

    if _CODE_MARKER.search(text):
        return True

    return (len(text) >= 4) and (re.search(r"[A-Z]", text[1:]) is not None)

#: A date pair inside a path shape (``2026-11-15/16``) and a measurement word.
_DATE_TAIL = re.compile(r"^(?:\d{2}|\d{1,2})$")
#: Single-slash shapes that are prose, not paths: a two-word connector / an
#: abbreviation / a unit ratio. Measured on the corpus (``and/or`` certified a
#: whole sentence and ``2026-11-15/16`` a whole paragraph).
_CONNECTOR = re.compile(r"^(?:and|or|n|w|no|yes|on|off|in|out|per|to|vs|w)$", re.IGNORECASE)
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-?\d{0,2}$")
#: Directory names that make a bare `dir/name` pair a path even without dots.
_SOURCE_DIRS = frozenset({"docs", "src", "tests", "test", "tools", "scripts", "packages", "package", "crates", "config", "conf", "fixtures", "res", "resources", "assets", "examples", "benches", "internal", "public", "static", "lib", "bin"})

def path_is_source(match: str) -> bool:
    """True when a `dir/name` shape names something a reader can open.

    A run of three or more segments is a path (``packages/vue/src``). A single
    pair is a path only when neither piece is a prose connector, an
    abbreviation or a date — those are `and/or`, `n/a`, `2026-11-15/16`.
    """
    segments = match.strip("/").split("/")

    # A run of three or more segments is a path (`packages/vue/src`): prose does
    # not spell one, and `and/or` has two pieces, not three.
    if len(segments) >= 3:
        return bool(re.search(r"[A-Za-z]", segments[-1])) and (len(segments[-1]) >= 2)

    if len(segments) != 2:
        return False

    left, right = segments

    if _CONNECTOR.match(left) or _CONNECTOR.match(right):
        return False

    if _DATE_PREFIX.match(left) and _DATE_TAIL.match(right):
        return False

    # BOTH pieces have to look like a name. `before/after` are two English
    # words; `wasm/wasm_test.rs` and `docs/en/x.md` are directory plus filename.
    def filename(piece: str) -> bool:
        return bool("." in piece) or bool(re.search(r"\d", piece)) or ("_" in piece)

    # The LEFT piece is a directory name: a common source directory, or a
    # dotted / numbered / underscored name. A bare English word (`before`) or a
    # bare camel word (`rustfmt`) is not a directory.
    named_left = (len(left) >= 3) and ((left in _SOURCE_DIRS) or filename(left))
    bare_left = (len(left) >= 3) and (not filename(left))
    # (`filename` covers dots, digits and underscores; a bare word still is not
    # a directory.)
    named_right = (len(right) >= 3) and (filename(right) or (right in _SOURCE_DIRS))

    return bool(named_left and named_right) or (bool(bare_left) and bool(named_right))

def _marker_hit(text: str) -> bool:
    """True when one of :data:`MARKERS` matches in a shape that is a source."""

    for pattern in MARKERS:
        for match in pattern.finditer(text):
            group = match.group(0)

            if re.fullmatch(r"[0-9a-fA-F]{7,40}", group) and (not re.search(r"\d", group)) and (not re.search(r"\bsha|\bcommit|\bhead\b", text, re.IGNORECASE)):
                continue

            if ("/" in group) and (group.count("/") >= 1):
                # The path marker is the only one whose matches need a verdict.
                if "\u4e00" <= group[0] <= "\u9fff":
                    return True

                if path_is_source(group) or (group.count("/") >= 2):
                    return True

                continue

            return True

    return False

def has_anchor(line: str) -> bool:
    """True when ``line`` names a recomputable source for a statement."""
    # A disclaimer is never a source: "not verified" must not certify anything.
    text = line

    for word in DISCLAIMERS:
        text = re.sub(re.escape(word), " " * len(word), text, flags = re.IGNORECASE)

    spans = tuple(span for span in code_spans(text) if not is_citation(span))
    rest = _SPAN.sub(" ", text)

    # A line's own code tokens name its SUBJECT — "`pr_gate.py` now runs 6
    # gates" names the module and states a count, and the module is not the
    # run. A span sources a line only when the leftover text is a result marker
    # (` → rc=0`), and a span carrying a figure is never a source.
    leftover = _RESULT_MARKER.sub(" ", rest)

    if (re.search(r"[A-Za-z\u4e00-\u9fff]", leftover) is None) and any(span_is_source(span) for span in spans):
        return True

    # A bare path in backticks is a CITATION (round 6): it is blanked so the
    # path marker cannot fire on it, while `file:line` and the other markers
    # keep working from the same text.
    cited = _SPAN.sub(lambda match: " " if is_citation(match.group(0).strip("`")) else match.group(0), text)

    return _marker_hit(cited)

def pastes_quoted_output(line: str) -> bool:
    """True when ``line`` carries a quoted quantity (``` `16 passed (16)` ```).

    A quotation on the same line is the run's output pasted beside the prose
    that talks about it: ``` kept the frozen 16/16 green … `16 passed (16)` ```
    is one sentence whose evidence is its own quotation.
    """
    spans = [match.span() for match in _SPAN.finditer(line)]

    return any((start <= part.start()) and (part.end() <= end) for part in QUANTITY.finditer(line) for start, end in spans)

def satisfies(line: str) -> bool:
    """The whole one-line test: a disclaimer, an anchor, an outcome row or a pasted output."""
    return has_disclaimer(line) or is_outcome_row(line) or has_anchor(line) or pastes_quoted_output(line)

def scan_body(body: str) -> tuple[list[statement], int, list[str]]:
    """Every claim line of ``body``: ``(statements, scanned_lines, notes)``.

    A line inside a markdown TABLE (``tabular``) that holds a run outcome word
    (``pass`` / ``fail`` / ``red`` / ``green`` / ``rc=0``) is captured output —
    the table is the evidence block — so it is not a prose statement. Every
    other line of a table is judged like any other prose, which is what keeps
    this rule from becoming "tables are exempt".
    """
    lines, notes, _evidence, _omitted = prosaic(body)
    statements: list[statement] = []

    for row in lines:
        if row.tabular and is_captured_cell(row.text):
            continue

        kinds, terms = claim_kinds(row.text)

        if kinds:
            statements.append(statement(line = row.number, text = row.text, kinds = kinds, terms = terms))

    return statements, len(lines), notes

def sourced(lines: Sequence[_line], index: int, evidence: Sequence[_line] = ()) -> bool:
    """True when the statement at ``index`` is satisfied by nearby evidence.

    Three windows, all of them local:

    * **the statement's own window** — the line above, itself, the line below;
    * **its paragraph** — inside the same prose section, the line above and the
      line below (a paragraph is what a reader perceives as one thought; the
      line window above already covers the common case and this covers the
      wrapped one);
    * **its table** — a statement inside a markdown table is satisfied when the
      table holds a verdict row (its LAST cell is ``3 red`` / ``4 green`` /
      ``passes``, see :func:`row_is_verdict`) or a captured telemetry row: that
      table is the run's output, and the row is a cell of it.

    A captured table's claim-looking cells are filtered out in
    :func:`scan_body`; this function is what keeps the prose tables honest.
    """
    candidates = [lines[position] for position in (index - 1, index, index + 1) if 0 <= position < len(lines)]
    row = lines[index]

    for position in (index - 1, index + 1):
        if (0 <= position < len(lines)) and (lines[position].section == row.section):
            candidate = lines[position]

            if candidate.tabular == row.tabular:
                candidates.append(candidate)

    if row.tabular:
        same_table = [part for part in lines if part.section == row.section]

        # A table is captured output only when it is CAPTURED: a row whose
        # last cell is a verdict (`| drop the guard | 3 red |`) or a wide
        # telemetry row. One verdict row does not license the prose rows beside
        # it (round 6: `| suite | 3 green |` excused `| login | 9041 bytes |`).
        if any(row_is_verdict(part.text) or is_outcome_row(part.text) for part in same_table):
            return any(is_captured_cell(part.text) for part in same_table if part.number == row.number) or bool(row_is_verdict(row.text) or is_outcome_row(row.text))

    # A fenced line one row away (the opener just above, the closer just below,
    # or the capture itself) is the evidence a figure is talking about — but a
    # BEHAVIOUR claim is not established by an unrelated command's output
    # (`node -v` does not prove "the retry never fires twice", round 6).
    nearby_fence = any(abs(part.number - row.number) <= 1 for part in evidence)

    if nearby_fence and (not behaviour_terms(row.text)):
        return True

    for part in candidates:
        # The statement's OWN line may end in a disclaimer ("… — not verified").
        # A neighbour line may be a disclaimer or an anchor. Both reach: writing
        # the sentence first and the point on the next line is ordinary PR
        # prose, and neither can fabricate a claim — a NEIGHBOUR's disclaimer
        # excuses the figure it is talking about, while an anchor is a source.
        if satisfies(part.text):
            return True

    return False

def check_body(body: str) -> report:
    """The gate's verdict over one PR body string."""
    stripped_body = (body or "").strip()

    if not stripped_body:
        return report(scanned_lines = 0, statements = (), findings = (), problems = ("the PR body is empty — an empty body is either a probe or a truncated payload, and no reason to believe anything about the change appears anywhere; write the body",))

    lines, notes, evidence, omitted = prosaic(stripped_body)

    if not lines:
        # Every line was structurally excluded (all fenced, all quoted, all
        # headings, only gate entries). That is not "nothing to judge": it is a
        # body whose prose could not be read at all, and a green verdict over it
        # would certify a scan that never happened.
        nothing_scan = "zero prose lines in the body — everything is the verify record, a fence, a quote, an indent or a heading; a green verdict over it would certify a scan that never happened"

        return report(scanned_lines = 0, statements = (), findings = (), problems = (*notes, *omitted, nothing_scan))

    statements, scanned, scan_notes = scan_body(stripped_body)
    problems = [*notes, *scan_notes]

    findings = [stmt for stmt in statements if not sourced(lines, _index_of(lines, stmt), evidence)]

    if not statements:
        return report(scanned_lines = scanned, statements = (), findings = tuple(findings), problems = tuple(problems), skipped = f"no quantity or behaviour statement in {scanned} prose line(s) — nothing to judge")

    return report(scanned_lines = scanned, statements = tuple(statements), findings = tuple(findings), problems = tuple(problems))

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
        parts.append("the command or output the number came from (`file:line`, `#PR`, `evidence:`, or the COMMAND in backticks)")

    if "behaviour" in stmt.kinds:
        parts.append("a mutation id or a probe result (`MUT:<id>`, a verification-table row)")

    if "unquoted digit" in stmt.kinds:
        parts.append("that figure quoted from the run that produced it")

    # A span holding the figure itself never certifies it (see `span_is_source`),
    # so the advice must name the shapes that DO count.
    parts.append("or the disclaimer 未复算 / unverified / not established / inference")

    if code_spans(stmt.text) and any(_SPAN_FIGURE.search(span) for span in code_spans(stmt.text)):
        parts.append("(a quoted figure is the claim, not its source: quote the COMMAND, or open the `file:line` it came from)")

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
        print(f"✅ body-gate: {len(result.statements)} statement(s) over {result.scanned_lines} prose line(s), all sourced or disclaimed ({label})")

    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
