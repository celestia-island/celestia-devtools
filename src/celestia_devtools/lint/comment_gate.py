#!/usr/bin/env python3
"""comment-gate - a behaviour claim in a test comment must name its mutation.

*Why this exists:* three consecutive seam PRs (hikari #1476 / #1478 / #1482)
shipped **correct production code** and **wrong prose**: the tests pinned the
behaviour, and the comments beside them asserted what the pin "catches", "does
NOT catch", "only reds when", or what "measured" showed. Those sentences were
written from memory, not copied from a probe run, and four of them survived a
round of self-review only to be refuted in the next one — four review rounds
spent on description, none on code. "Every factual claim in a comment must be
recomputable" is a *discipline*, and a discipline depends on remembering; this
gate is the mechanical probe that turns it into "cannot forget".

*Boundary - what this is NOT:* it cannot judge whether a claim is **true** (a
verdict needs the reference semantics, which no text scan has — the same blind
spot ``doc_lint`` has, and a guess would manufacture false greens). It checks
one mechanical proxy instead: **wherever a comment claims behaviour, the same
comment block must be able to point at the mutation that proved it.** With a
mutation id the next reader can re-run the proof; without one the claim is
unverified by construction, and is reported as such.

*Scope — deliberately narrow:*

* **Test files only** (``*.test.*`` / ``*.spec.*`` / ``*_test.*`` /
  ``test_*.*`` / any path segment named ``test`` / ``tests``). Production
  comments are out of scope: the false claim this gate was built from started
  in ``useHighlight.ts``, and it was found by review, not by a scanner. Seam-pin
  comments in test files are where behaviour claims concentrate, and widening
  the scan trades a real signal for a flood of ``never`` / ``always`` in
  ordinary code comments.
* **Added or modified comment lines only** (a diff, not the tree). The gate is
  a pre-merge gate for *this change*; a pre-existing claim is somebody else's
  review round, not a reason to block this one.
* **Comment text only**: a line the scanner is not sure is a comment (a code
  line inside a context run, say) is never accused. The corpus proves why —
  ``as [never]`` in a code line hits the vocabulary twice per file.

*Anchor — what counts as a mutation reference* (the second half of the rule, so
it cannot be satisfied by "the word mutation appears somewhere"):

* an explicit annotation — ``MUT:<id>`` where ``<id>`` is ``[A-Za-z0-9._-]{1,64}``,
  or ``mutate:<slug>`` with the same body. Case-insensitive (``mut:`` /
  ``MUTATE:`` both bind, because a reader who is looking for the anchor is not
  helped by the gate refusing for decoration), and required to carry a non-empty
  id — a bare ``MUT:`` names nothing and anchors nothing.
* a **reference to the PR's mutation table** (``mutation table`` / ``变异表``),
  which is where the probe ids live when a PR pins them in its body rather than
  in the file;
* **a row of that table**: a markdown row that names the mutation and states the
  verdict (``| drop the guard | 3 green |``), or a bullet / numbered item with
  an arrow (``- drop the guard → 3 red``). A prose sentence that merely says
  "mutating X reds this" is not a row — it is the very shape of a claim written
  from memory, and accepting it would make the gate agree with itself.

*Waiver:* ``--waiver`` does not cover this gate (that channel suppresses exactly
one refusal, ``verify record c3 < 3``, and a waiver channel whose whole purpose
is "the description was shaky" would be self-defeating here). The only
exception is ``comment-gate-waiver: <reason>`` in the PR body, read by the merge
gate and announced on stdout — it is never a silent pass, and it never reaches
this module.
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

#: How far from a claim line its mutation anchor may sit, in either direction.
#: Ten lines is the design number: it spans a paragraph of prose plus the
#: ``expect(...)`` block it introduces, and stops before an unrelated claim
#: further down the file can excuse this one.
ANCHOR_WINDOW = 10

#: Behaviour-statement vocabulary, matched case-insensitively as SUBSTRINGS. The
#: English entries plus the design document's four CJK ones (seam-pin comments in
#: this org are written in both languages). Two entries are deliberately shorter
#: than the word a reader sees: ``catch`` covers ``catches``/``caught``, ``reds``
#: covers ``reddens``, and ``doesn't`` is listed beside ``does not`` because the
#: contraction is what people actually write. A review round flagged ``catches``
#: as redundant next to ``catch``; it is, and that is the intent — every entry
#: that can match is listed in full so a reader can see why a line was hit.
BEHAVIOUR_TERMS: tuple[str, ...] = ("measured", "verified", "reds", "reddens", "does not", "doesn't", "catches", "catch", "only when", "never", "always", "抓不住", "抓住", "只在", "只有.*时才")

#: A mutation id: ``MUT:<id>`` / ``mutate:<slug>``. The body must be non-empty —
#: a bare ``MUT:`` names nothing, so it anchors nothing. Case-insensitive because
#: a reader looking for the anchor gains nothing from the gate refusing over
#: capitalisation.
_EXPLICIT_ANCHOR = re.compile(r"(?:MUT|MUTATE):\s*(?P<id>[A-Za-z0-9._-]{1,64})", re.IGNORECASE)

#: A reference to the PR's mutation table (``mutation table``, ``变异表``). This
#: is the third anchor form the design names: the claim points at the table, and
#: the table is what a reviewer re-runs. It is a *reference*, not a verdict —
#: the row shape below is what a row inside the table must look like.
_TABLE_REFERENCE = re.compile(r"mutation\s+table|变异表|mutation\s+matrix", re.IGNORECASE)

#: Prose that removes a line from the gate's reach. A ``comment-gate-ok:``
#: directive is the escape hatch for a claim the author can defend without a
#: mutation (a reproducibility note about a toolchain quirk, say). The reason is
#: part of the pattern, not a nicety: review round 5 pointed out that a bare
#: ``comment-gate-ok:`` suppressed a claim with no argument at all, which makes
#: the escape hatch the one silent path in an otherwise announce-everywhere
#: design. :data:`DIRECTIVE_REASON_MIN` is the same argument standard the PR-body
#: waiver is held to. ``MUT:`` / ``mutate:`` are never treated as a directive.
DIRECTIVE_REASON_MIN = 20
DIRECTIVE = re.compile(r"comment-gate-ok:[ \t]*(?P<reason>.*)$", re.IGNORECASE)

#: A mutation-table row. TWO shapes, both mechanical enough that a statement
#: writer cannot satisfy them by accident, which is the whole point — a plain
#: prose sentence like "removing the guard reds this case" is exactly the shape
#: of a claim written from memory, and accepting it would let the gate agree
#: with itself.
#:
#: * **verdict row** — a markdown table row (``| drop the guard | 3 red |``):
#:   a mutation verb plus a verdict (red / green / pass / fail / kill / survive);
#: * **bullet row** — a bullet or numbered item with an arrow (``- drop the
#:   guard → 3 red``), optionally carrying its own id.
_ROW_VERDICT = re.compile(r"\b(?:red|reds|green|stays green|passes|pass|fails|fail|kill(?:s|ed)?|survive[ds]?|survival|crash(?:es)?)\b|红|绿", re.IGNORECASE)
_ROW_TABLE = re.compile(r"^\s*\|.*\|[ \t]*$")
_ROW_ACTION = re.compile(r"\b(?:drop(?:s|ped|ping)?|delet(?:e|es|ed|ing)|remov(?:e|es|ed|ing)|" r"replac(?:e|es|ed|ing)|invert(?:s|ed|ing)?|bypass(?:es|ed|ing)?|" r"swap(?:s|ped|ping)?|mutat(?:e|es|ed|ing)|stub(?:s|bed|bing)?|" r"short-?circuit(?:s|ed|ing)?|reorder(?:s|ed|ing)?|" r"return(?:s|ed|ing)? earlier|" r"删(?:掉|除)?|替换|反转|绕过|变异)", re.IGNORECASE)
_ROW_ARROW = re.compile(r"→|->|=>|⇒")

#: ``//`` and ``/* */`` comment openers (JS/TS, Rust, Go, Swift, Kotlin, C-family).
_SLASH_COMMENT = re.compile(r"^\s*(?://|/\*|\*|\*/)")
#: ``#`` comments (Python, shell, TOML, YAML, Ruby). ``#!`` shebangs included —
#: harmless, they never carry behaviour vocabulary.
_HASH_COMMENT = re.compile(r"^\s*#(?!\[)")
#: A Python docstring (and its continuations), opened or closed by a triple
#: quote. Docstrings are how Python states what a test pins, so leaving them out
#: of the scanner's reach meant a docstring could claim behaviour with no anchor
#: at all — measured on PR #306's own successor: a docstring saying "reverting
#: `_header_text` reds this test and nothing else" was invisible to the gate
#: that exists to catch exactly that sentence.
_DOCSTRING = re.compile(r'^\s*(?:[rRuUbBfF]{0,2}"""(?!")|\'\'\'(?!\'))')
#: ``--`` comments (Lua, SQL, Haskell).
_DASH_COMMENT = re.compile(r"^\s*--")
#: ``;`` comments (Lisp family).
_SEMI_COMMENT = re.compile(r"^\s*;")
#: ``<!-- -->`` HTML/Markdown.
_HTML_COMMENT = re.compile(r"^\s*<!--")

_TEST_NAME = re.compile(r"(?:\.(?:test|spec)\.[^/]+$)|(?:_test\.[^/]+$)|(?:^test_[^/]+$)", re.IGNORECASE)
_TEST_SEGMENTS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_FILE_HEADER = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")
_NEW_FILE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")

@dataclass(frozen = True)
class claim:
    """One behaviour claim with no anchor in reach."""

    path: str
    line: int
    text: str
    terms: tuple[str, ...]

    def render(self) -> str:
        # The terms ride along when they include a marker the report must SHOW
        # (a thin `comment-gate-ok:` reason): a refusal that does not say why is
        # the silence the gate exists to remove (review round 6).
        marker = f"  [{', '.join(self.terms)}]" if any(term.startswith("comment-gate-ok") for term in self.terms) else ""
        text = self.text.strip().lstrip(DOC_CONTINUATION).strip()
        return f"{self.path}:{self.line}: {text}{marker}"

@dataclass(frozen = True)
class report:
    """The gate's whole verdict: scans, claims, misses, and the refusals."""

    scanned_lines: int
    hits: int
    claims: tuple[claim, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

def behaviour_terms(text: str) -> tuple[str, ...]:
    """The vocabulary entries ``text`` hits (empty = not a behaviour claim)."""
    hits: list[str] = []
    lowered = text.lower()

    for term in BEHAVIOUR_TERMS:
        if ".*" in term:
            if re.search(term, text):
                hits.append(term)
        elif term.lower() in lowered:
            hits.append(term)

    return tuple(hits)

def is_test_path(path: str) -> bool:
    """True when ``path`` names a test file (by name or by containing directory)."""
    normalised = path.replace("\\", "/")

    if _TEST_NAME.search(normalised):
        return True

    return any(part.lower() in _TEST_SEGMENTS for part in normalised.split("/")[:-1])

def comment_body(line: str) -> str | None:
    """The text of ``line`` when it is a comment or a doc line, else ``None``.

    A doc comment's continuation lines start with ``*`` and carry the sentence,
    so they count as comment text. Blank lines inside a block comment are
    comments too (they carry nothing, and returning ``None`` keeps the scan
    statistics about *text*).
    """

    if _SLASH_COMMENT.match(line) or _HASH_COMMENT.match(line):
        return line

    if line.startswith(DOC_CONTINUATION):
        return line[len(DOC_CONTINUATION):]

    if _DASH_COMMENT.match(line) or _SEMI_COMMENT.match(line) or _HTML_COMMENT.match(line):
        return line

    # `https://` inside code is not a comment: the triple-quote opener is what
    # decides, and a continuation line of a docstring is plain prose with no
    # marker at all — the block state that makes those visible lives in
    # `_docstring_blocks`, which the diff walker calls.
    if _DOCSTRING.match(line):
        return line

    return None

#: Zero-width characters a "reason" could hide behind: ``str.strip()`` removes
#: whitespace and nothing else, so twenty U+200B satisfied the floor before
#: review round 6. Mirrors ``pr_gate._visible_len``'s filter.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)

def _visible(text: str) -> str:
    """``text`` without leading/trailing space or zero-width characters."""
    return text.translate(_INVISIBLE).strip()

def _strip_comment_prefix(line: str) -> str:
    """The line without its comment opener (and, on doc rows, the leading ``*``)."""
    return re.sub(r"^\s*(?://|/\*|\*|#|--|;)\s?", "", line).rstrip()

def _is_row(line: str) -> bool:
    """True when ``line`` is one row of a mutation table (either row shape)."""
    stripped = _strip_comment_prefix(line).strip()

    if not stripped:
        return False

    if _ROW_TABLE.match(stripped):
        return bool(_ROW_ACTION.search(stripped)) and bool(_ROW_VERDICT.search(stripped))

    is_item = bool(re.match(r"^(?:[-*+]|\d+[.)])\s+\S", stripped))

    if not is_item:
        return False

    if not (_ROW_ACTION.search(stripped) or re.search(r"mutate|mutation|变异", stripped, re.IGNORECASE)):
        return False

    return bool(_ROW_VERDICT.search(stripped) or _ROW_ARROW.search(stripped))

def is_anchor(line: str) -> bool:
    """True when ``line`` can prove a claim (an id, a table reference, a row)."""
    line = line.removeprefix(CONTEXT_MARK)
    return _anchor_text(line)

def _anchor_text(line: str) -> bool:
    """The anchor test itself (callers strip the context marker first).

    Only COMMENT text can anchor, mirroring the claim rule: a code line that
    happens to contain `MUT: x` inside a string literal — or a line whose
    content is `+// MUT: x`, which is content, not a comment — must not certify
    a neighbouring claim. Review round 4 found the earlier fallback
    (`probe = body if body is not None else line`) let exactly that through.
    """
    body = comment_body(line)

    if body is None:
        return False

    if _EXPLICIT_ANCHOR.search(body) or _TABLE_REFERENCE.search(body):
        return True

    return _is_row(body)

def anchored(window: Iterable[str]) -> bool:
    """True when any line of ``window`` carries a mutation anchor."""
    return any(is_anchor(line.removeprefix(CONTEXT_MARK)) for line in window)

def scan_comment_block(lines: Sequence[str], path: str, line_numbers: Sequence[int]) -> tuple[list[claim], int]:
    """Claims without an anchor and the number of claim lines in ``lines``.

    ``lines`` are the lines of one section (blank lines allowed inside a doc
    comment) and ``line_numbers`` their positions in the FILE. The anchor window
    is :data:`ANCHOR_WINDOW` **file lines** on either side, clamped to the
    section: slicing by index instead made a patch that SKIPS lines (``-U0``
    hunks, an elided gap) look adjacent — a claim at line 1 and an anchor at
    line 17 sat ten indices apart and bound, which is the whole defect the
    round-2 review found. Widening past the section on both sides would let a
    neighbouring function's table excuse an unrelated claim — the section is the
    unit a reader actually reads.
    """
    claims: list[claim] = []
    hits = 0

    for index, text in enumerate(lines):
        # Only COMMENT text can carry a claim. A code line in the window
        # matches the vocabulary often enough (`as [never]` in the corpus) and
        # reported as an accusation against a code line — measured twice.
        if text.startswith(CONTEXT_MARK):
            continue

        if comment_body(text) is None:
            continue

        terms = behaviour_terms(text)

        if not terms:
            continue

        hits += 1

        directive = DIRECTIVE.search(text)

        if directive is not None:
            reason = _visible(directive.group("reason"))

            if len(reason) >= DIRECTIVE_REASON_MIN:
                continue

            # A directive without an argument suppresses nothing: the claim is
            # reported, and the reader sees why (the reason was too thin).
            terms = (*(term for term in terms), "comment-gate-ok with a thin reason")

        # Distance is measured in FILE LINE NUMBERS, not list indices: a diff
        # that skips lines would otherwise make a distant anchor look adjacent.
        here = line_numbers[index]
        low = max(0, here - ANCHOR_WINDOW)
        high = here + ANCHOR_WINDOW
        window = [text for text, number in zip(lines, line_numbers, strict = True) if low <= number <= high]

        if anchored(window):
            continue

        claims.append(claim(path = path, line = line_numbers[index], text = text.strip(), terms = terms))

    return claims, hits

def _comment_blocks(lines: Sequence[str], numbers: Sequence[int]) -> list[tuple[list[str], list[int]]]:
    """Group the comment lines of one file into contiguous blocks."""
    blocks: list[tuple[list[str], list[int]]] = []
    current: list[str] = []
    current_numbers: list[int] = []

    for line, number in zip(lines, numbers, strict = True):
        if comment_body(line) is None:
            if current:
                blocks.append((current, current_numbers))
                current, current_numbers = [], []

            continue

        # A blank-looking comment continuation (`` *``) is still a comment.
        current.append(line)
        current_numbers.append(number)

    if current:
        blocks.append((current, current_numbers))

    return blocks

def scan_file(path: str, text: str) -> tuple[list[claim], int]:
    """Scan one whole file's comments (the ``--file`` lane, for corpus probes)."""
    lines = text.splitlines()
    claims: list[claim] = []
    hits = 0

    for block_lines, block_numbers in _comment_blocks(lines, list(range(1, len(lines) + 1))):
        block_claims, block_hits = scan_comment_block(block_lines, path, block_numbers)
        claims.extend(block_claims)
        hits += block_hits

    return claims, hits

def scan_diff(diff: str) -> tuple[list[claim], int, int, list[str]]:
    """Scan a unified diff; return ``(claims, hits, scanned_comment_lines, notes)``.

    Only ``+`` lines of test files are candidates — the gate judges what *this
    change adds*, and complaining about a file a PR merely touches would block
    work on evidence the author did not write.

    The unit of scanning is **one section of one file**, and a section is a run
    of lines the patch actually shows, laid out by **file line number**. The
    section CLOSES (it never splices) when the file changes, when a hunk starts
    after a gap of more than ``2 * ANCHOR_WINDOW`` file lines, when a hunk's
    start goes backwards (a concatenation, not a continuation), and at a blank
    comment line (``//`` on its own) — the spacer a reader reads as a section
    break, and exactly where a mutation table at the top of a heading block
    would otherwise leak into the next one.

    Every distance compared here is a FILE line number, never a count of patch
    lines: found the hard way (round 2), a hunk declared as one line but
    carrying ten made ``start - new_line`` look like a two-line gap while the
    file distance was sixteen, and an anchor sixteen lines away bound.
    """
    claims: list[claim] = []
    hits = 0
    scanned = 0
    notes: list[str] = []
    scanned_numbers: dict[str, set[int]] = {}
    per_file: list[tuple[str, list[list[tuple[int, str]]]]] = []
    section: list[tuple[int, str]] = []
    sections: list[list[tuple[int, str]]] = []
    current_path: str | None = None
    last_file_line: int | None = None
    cursor = 1
    in_hunk = False

    def close_section() -> None:
        nonlocal section

        if section:
            sections.append(section)

        section = []

    # Python docstrings: a continuation line carries no marker, so the walker
    # tracks the block state itself. Reset whenever the file changes or a hunk
    # jumps backwards (a concatenation is not a continuation).
    doc_open = ""
    doc_quote = ""

    def close_file() -> None:
        nonlocal sections, current_path, last_file_line, in_hunk

        close_section()

        if sections:
            per_file.append((current_path or "", sections))

        sections = []
        current_path = None
        last_file_line = None
        in_hunk = False

    def add(file_line: int, text: str) -> None:
        nonlocal last_file_line

        # A line that goes BACKWARDS or jumps over a gap wider than the window
        # cannot be in the same section; splicing it would silently redefine
        # every distance measured inside that section.
        if (last_file_line is not None) and (file_line <= last_file_line):
            close_section()

        section.append((file_line, text))
        last_file_line = file_line

    for raw in diff.splitlines():
        header = _FILE_HEADER.match(raw)

        if header:
            close_file()
            current_path = header.group("new")
            continue

        hunk = _HUNK_HEADER.match(raw)

        if hunk:
            start = int(hunk.group("start"))

            # Hunk-start comparison is a cheap BOUND only (a hunk's declared
            # body can be smaller than the lines it carries); the binding test
            # that matters is the per-line one in `add()`.
            gap_is_wide = (last_file_line is not None) and (start > (last_file_line + (2 * ANCHOR_WINDOW)))

            if gap_is_wide:
                close_section()

            cursor = start
            in_hunk = True
            continue

        if not in_hunk:
            # Header block only: the per-file label, which for a new file is
            # the only place the real path appears (`a/` is ``/dev/null``).
            label = _NEW_FILE.match(raw)

            if label and (current_path is not None):
                current_path = label.group("path")

            continue

        # A hunk line is classified by its FIRST character ONLY, and the rest
        # of the line is the CONTENT that follows it. Matching the header
        # prefixes instead (`+++ ` / `--- `) ate a real added line whose content
        # itself began with `++` — found in round 2 with a live `git diff`.
        prefix, body = raw[:1], raw[1:]

        if prefix == "+":
            if current_path and is_test_path(current_path):
                content = _content(body)
                doc_open, doc_quote = _doc_state(content, doc_open, doc_quote)

                if (comment_body(content) is None) and doc_open:
                    # A docstring continuation is COMMENT TEXT that happens to
                    # carry no marker — not context. It keeps claim candidacy.
                    content = DOC_CONTINUATION + content

                if (comment_body(content) is not None) and content.strip():
                    # Count each LINE once: an extended header can repeat one.
                    seen = scanned_numbers.setdefault(current_path, set())

                    if cursor not in seen:
                        seen.add(cursor)
                        scanned += 1

                add(cursor, content)

            cursor += 1
            continue

        if prefix == " ":
            # Context feeds the geometry but is never a claim candidate: the
            # gate judges what this change ADDS. It is prefixed so a context
            # line that is CODE can never be read as a comment (`as [never]`
            # hit the vocabulary from inside a context line — measured).
            if (current_path is not None) and is_test_path(current_path):
                # A context line is GEOMETRY for the window and nothing else:
                # it can anchor a claim (it is a line of the file the reader
                # sees) but it can never BE a claim, because the gate judges
                # what this change adds. Prefixing it with an explicit context
                # marker is what makes that structural rather than incidental.
                add(cursor, CONTEXT_MARK + body)

            cursor += 1
            continue

        if prefix in ("-", "\\"):
            # A removal shifts nothing in the NEW file: `-` lines do not advance
            # its line numbers, and `\\ No newline` is not a line at all.
            continue

        if raw == "":
            continue

    close_file()

    for path, file_sections in per_file:
        for section_lines in file_sections:
            for block in _split_on_spacers(section_lines):
                numbers = [number for number, _ in block]
                texts = [text for _, text in block]
                block_claims, block_hits = scan_comment_block(texts, path, numbers)
                claims.extend(block_claims)
                hits += block_hits

    if scanned == 0:
        notes.append("no added/modified test-file comment lines in the diff")

    return claims, hits, scanned, notes

#: A context line's marker. NUL cannot appear in a source line, so a context
#: line can never accidentally look like a comment — the previous "prepend a
#: space" trick FAILED for a context line whose text started with ``//`` (the
#: space is stripped by every comment matcher).
CONTEXT_MARK = "\x00"
#: A docstring continuation line: comment text with no marker of its own. It is
#: DELIBERATELY a different sentinel from :data:`CONTEXT_MARK` — a context line
#: is never a claim (the gate judges what the change adds), while a docstring
#: line very much can be one.
DOC_CONTINUATION = "\x01"

def _content(body: str) -> str:
    """The FILE content of an added hunk line.

    ``body`` is everything after the one-character hunk marker, and git does NOT
    quote the content further — measured against real ``git diff -U0``: file
    content ``+// x`` is emitted as the hunk line ``++// x`` (marker plus
    content), and content ``++counter;`` as ``+++counter;``. So the content is
    exactly ``body``. An earlier revision stripped a leading ``+`` "to undo
    quoting", which mangled genuine ``++``-content lines (review round 4); this
    function stays as the single named place where that reading is stated.
    """
    return body

_DOC_OPEN = re.compile(r'^\s*[rRuUbBfF]{0,2}("""|\'\'\')')

def _doc_state(line: str, opened: str, quote: str) -> tuple[str, str]:
    """Advance the docstring block state by one line; ``(opened, quote)``.

    ``opened`` is empty, or ``"continuing"`` meaning the NEXT line is already
    inside the block (a one-line docstring opens and closes without continuing).
    """

    if not opened:
        opener = _DOC_OPEN.match(line)

        if opener is None:
            return "", ""

        chosen = opener.group(1)
        rest = line[opener.end():]

        if chosen in rest:
            return "", ""

        return "continuing", chosen

    if quote and (quote in line):
        return "", ""

    return "continuing", quote

_DOC_OPEN = re.compile(r'^\s*[rRuUbBfF]{0,2}("""|\'\'\')')
_DOC_CLOSE = re.compile(r'("""|\'\'\')\s*$')

def _docstring_lines(lines, numbers):
    """Continuation lines of Python docstrings, as ``{index: True}``.

    A docstring's FIRST line carries the opener and `comment_body` already sees
    it; its CONTINUATION lines carry no marker whatsoever, so without block
    state they read as code. That is the blind spot measured on this gate's own
    first follow-up PR: a docstring claiming a mutation's scope sat inside one.
    """
    inside = False
    quote = ""
    found = {}

    for index, text in enumerate(lines):
        stripped = text.strip()

        if not inside:
            opener = _DOC_OPEN.match(text)

            if opener:
                quote = opener.group(1)
                # A one-line docstring opens and closes on the same line.
                rest = text[opener.end():]

                if not _DOC_CLOSE.search(rest) or (quote not in rest):
                    inside = True

                continue

            continue

        found[index] = True

        if quote in stripped:
            inside = False

    return found

def _split_on_spacers(entries: Sequence[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Split one section at blank comment lines (``//`` on its own).

    A spacer run is the boundary a reader actually sees: it is what separates
    two sections, and it is the shape that would otherwise let a mutation table
    at the top of a heading block excuse a vague claim in the next one.
    """
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []

    for number, text in entries:
        # A spacer is a commented line whose text is empty (``//``, `` *``).
        # ``void`` is what ``comment_body`` returns for a non-comment, so
        # ``commented is not None`` already excludes code — testing the stripper
        # instead labelled real claims as spacers and the scanner saw nothing.
        commented = comment_body(text)
        spacer = (commented is not None) and (not commented.strip())

        if spacer:
            if current:
                blocks.append(current)

            current = []
            continue

        current.append((number, text))

    if current:
        blocks.append(current)

    return blocks

def _git_env() -> dict[str, str]:
    """The environment for the gate's own ``git`` calls.

    ``GIT_OPTIONAL_LOCKS=0`` documents that this is a read-only caller (git may
    otherwise take an optional index lock). The caller's environment is kept:
    a repository reached through a proxy or a custom ``GIT_DIR`` must behave
    exactly as it would for the operator.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env

def _collect_diff(workspace: str | Path, base: str | None, rev_range: str | None, staged: bool, git_root: str | Path | None) -> tuple[str | None, list[str]]:
    """Run ``git diff``; ``(diff_text, problems)``. Never raises."""
    cmd = ["git"]

    if git_root is not None:
        cmd.extend(["-C", str(git_root)])

    cmd.append("diff")
    cmd.append("--no-color")
    cmd.append("-U0")

    if staged:
        cmd.append("--cached")
    elif rev_range:
        cmd.append(rev_range)
    else:
        base = base or "origin/master"
        cmd.append(f"{base}...HEAD")

    try:
        # ``encoding`` / ``text`` are UTF-8 explicitly: the locale default on a
        # CI node is often ASCII, and a diff of CJK comments would then die on
        # a decode error that reads as "git failed".
        probe = subprocess.run(cmd, capture_output = True, encoding = "utf-8", errors = "replace", check = False, env = _git_env())
    except OSError as exc:
        return None, [f"cannot run git: {exc}"]

    if probe.returncode != 0:
        detail = (probe.stderr or "").strip() or f"exit {probe.returncode}"
        return None, [f"git diff failed ({detail}) — refusing to report a green scan over an unread diff"]

    return probe.stdout, []

def check_diff(diff: str) -> report:
    """The gate's verdict over one unified diff."""
    claims, hits, scanned, notes = scan_diff(diff)
    problems = list(notes)

    if scanned == 0:
        # Zero production must fail: a diff with no candidate lines is either a
        # non-test change (nothing to prove here) or a broken extractor, and a
        # silent green cannot tell the two apart (verification-loop: "zero
        # output must be an error").
        problems.append("zero comment lines scanned — no candidate lines to judge; a green verdict over zero scanned input is indistinguishable from a broken extractor, so this refuses instead")

    if (hits == 0) and (scanned > 0):
        problems.append(f"zero behaviour-claim hits across {scanned} comment line(s) — the " "vocabulary (BEHAVIOUR_TERMS) matched nothing, which is the shape of " "a mis-typed pattern, not of a clean change; refusing")

    if claims:
        rendered = "\n".join(f"  {claim.render()}" for claim in claims)
        problems.append(f"{len(claims)} behaviour claim(s) without a mutation anchor within " f"±{ANCHOR_WINDOW} lines (add `MUT:<id>` / `mutate:<slug>`, a " f"mutation-table row, or `comment-gate-ok: <reason>`):\n{rendered}")

    return report(scanned_lines = scanned, hits = hits, claims = tuple(claims), problems = tuple(problems))

def check_files(paths: Sequence[str]) -> report:
    """The gate's verdict over whole files (corpus probes / a single-file lint)."""
    claims: list[claim] = []
    hits = 0
    scanned = 0
    problems: list[str] = []

    for path in paths:
        candidate = Path(path)

        try:
            text = candidate.read_text(encoding = "utf-8")
        except OSError as exc:
            problems.append(f"{path}: unreadable ({exc})")
            continue

        if not is_test_path(path):
            problems.append(f"{path}: not a test file — the gate only covers test comments")
            continue

        file_claims, file_hits = scan_file(path, text)
        scanned += sum(1 for line in text.splitlines() if comment_body(line) is not None)
        claims.extend(file_claims)
        hits += file_hits

    if (scanned == 0) and not problems:
        problems.append("zero comment lines scanned across the given files — refusing")

    if (hits == 0) and (scanned > 0):
        problems.append(f"zero behaviour-claim hits across {scanned} comment line(s) — refusing")

    if claims:
        rendered = "\n".join(f"  {claim.render()}" for claim in claims)
        problems.append(f"{len(claims)} behaviour claim(s) without a mutation anchor within " f"±{ANCHOR_WINDOW} lines:\n{rendered}")

    return report(scanned_lines = scanned, hits = hits, claims = tuple(claims), problems = tuple(problems))

def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``lint-comments [--base REF | --range A..B | --staged | --file PATH]``."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools lint-comments", description = "Fail behaviour claims in test-file comments that name no mutation.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base", default = None, help = "diff against <base>...HEAD (default origin/master)")
    source.add_argument("--range", default = None, metavar = "A..B", help = "diff a revision range")
    source.add_argument("--staged", action = "store_true", help = "diff the index (pre-commit lane)")
    source.add_argument("--file", action = "append", default = None, metavar = "PATH", help = "scan a whole test file (repeatable)")
    parser.add_argument("--diff", default = None, metavar = "PATH", help = "read a unified diff from a file instead of git")
    parser.add_argument("--git-root", default = None, metavar = "DIR", help = "repository to run git in (default: cwd)")
    parser.add_argument("--quiet", action = "store_true", help = "only the closing verdict line")
    args = parser.parse_args(argv)

    if args.diff:
        try:
            diff_text = Path(args.diff).read_text(encoding = "utf-8")
        except OSError as exc:
            print(f"❌ comment-gate: unreadable diff {args.diff} ({exc})", file = sys.stderr)
            return EXIT_USAGE

        report = check_diff(diff_text)
        label = args.diff
    elif args.file:
        report = check_files(args.file)
        label = ", ".join(args.file)
    else:
        if args.range and args.base:
            print("❌ comment-gate: --base and --range are mutually exclusive", file = sys.stderr)
            return EXIT_USAGE

        diff_text, problems = _collect_diff(args.git_root or ".", args.base, args.range, args.staged, args.git_root)

        if diff_text is None:
            for problem in problems:
                print(f"❌ {problem}", file = sys.stderr)

            return EXIT_USAGE

        report = check_diff(diff_text)
        label = args.range or ("index" if args.staged else (args.base or "origin/master") + "...HEAD")

    for claim in report.claims:
        print(f"❌ {claim.render()}")

    for problem in report.problems:
        print(f"❌ {problem}", file = sys.stderr)

    if not report.ok:
        print(f"COMMENT-GATE REFUSED — {len(report.claims)} anchorless claim(s), " f"{len(report.problems)} problem(s) over {label}", file = sys.stderr)
        return EXIT_FINDINGS

    if not args.quiet:
        print(f"✅ comment-gate: {report.hits} claim line(s) over {report.scanned_lines} comment line(s), all anchored")

    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
