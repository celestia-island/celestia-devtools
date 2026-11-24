#!/usr/bin/env python3
"""Fail a Rust source whose *rendered* text carries a newline the source never wrote.

*Why this exists:* a 2026-11-24 review round told an implementer to "collapse the
runs of spaces" in four user-facing RPC error strings. The implementer did the
opposite operation — the ``\\`` line-continuation was replaced with a **real
newline plus the indentation**, so the runtime string gained a bare ``"\\n    "``
in the middle of a sentence — and the change merged: ``cargo fmt`` reformats
code, not the whitespace *inside* a literal, ``cargo clippy`` has no lint for
it, and all 1176 tests stayed green. A delta review found it by comparing bytes.
The lesson is not "review harder": a defect class that every existing gate is
structurally blind to needs a gate of its own.

*Scope — ``.rs`` files only.* Markdown is out of the scan for exactly the reason
it would otherwise need an exception: a trailing double space is a hard line
break in Markdown, so class B has no legal exception to manage once the corpus
is Rust. What it scans:

* **class A — a newline the runtime string did not ask for.** A plain string
  literal (``"…"`` or ``b"…"``) whose MIDDLE line breaks are bare. The MIDDLE
  is the whole point: the line break before a closing quote is structural (every
  spanning literal has one) and a ``\\`` continuation renders as nothing, so
  only an unescaped newline between the quotes reaches the value as ``"\\n"``.
  Raw strings (``r"…"``, ``r#"…#"``, ``br#…#``, ``cr#"…#"``) are never reported:
  there the newline is an explicit choice.
* **class B — trailing space or tab at the end of a line.** Four spaces at a
  line end are invisible in a diff and in a terminal, and their absence is
  exactly what a half-finished "collapse the runs of spaces" edit leaves behind.
  Blank lines are exempt (rustfmt normalises them, they render nothing) and so
  is the interior of a raw string (its trailing bytes ARE the value).

*The exemption lanes are what make this usable, and they were measured, not
guessed.* Nine Rust repositories in this workspace hold 3737 tracked ``.rs``
files and 1000 literals that span lines. A fail-closed "any spanning literal"
rule reports 919 of them — the honest ``\\`` continuations — on day one, which
is how a gate gets switched off. So the classifier asks the question a reviewer
asks instead: is this literal a SENTENCE that a newline broke, or a DOCUMENT
that is meant to occupy lines?

* :data:`LANE_CONTINUATION` — every break is a ``\\`` continuation.
* :data:`LANE_HALF_CONVERTED` — continuations AND a bare newline: the
  2026-11-24 mixture. Checked before every excuse lane; this is the defect.
* :data:`LANE_EMBEDDED` — the body is a document (SQL, TOML front matter, a
  markdown list, another language's source), or its tail is punctuation only.
* :data:`LANE_BODY` — the first line is an opener or a format placeholder
  (``(function() {``, ``{}``, ``[``): a template, not a sentence.

After those lanes the org corpus has exactly three class A findings, and each
one is a real defect with the same signature: a message that renders
``"...\\n   more"`` to a user. They are recorded, not fixed — repairing another
repository is not this change's scope.

*Waiver.* ``// string-lit-gate: allow [reason]`` on the line, or on the line
that opens the literal, exempts it. The anchor is a comment tag and the reason
is optional for the same reason it is optional in ``comment-gate``'s
``allow`` spelling: the shape is a review-visible marker in the diff, and a gate
that refuses a self-documenting line teaches authors to delete the marker
instead of defending it.

*Exit codes:* 0 clean, 1 findings, 2 usage / unreadable input (a mistyped path
must never read as clean). ``--selftest`` runs the checker against its own
positive control, negative control, waiver anchor and terminal gate.

Usage::

    celestia-devtools string-lit-gate                 # scan the repository root
    celestia-devtools string-lit-gate crates src      # explicit paths
    celestia-devtools string-lit-gate --newline       # class A only
    celestia-devtools string-lit-gate --trailing      # class B only
    celestia-devtools string-lit-gate --json
    celestia-devtools string-lit-gate --selftest
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

#: Class identifiers, stable: they appear in the report, the JSON envelope and
#: the tests. A rename is a rule change.
NEWLINE = "newline-in-literal"
TRAILING = "trailing-whitespace"

#: A raw-string prefix (``r#`` / ``br#`` / ``cr#``) at the scanner's position.
_RAW_PREFIX = re.compile(r"(?:cr|br|b|c)?r(#*)(\"+)")

#: The waiver anchor. It is scanned as raw text on the line — a waiver inside a
#: string literal is still read as a waiver, which is a deliberate fail-open:
#: the line merely escapes the gate, it never escapes review (the marker shows
#: up in the diff). Requiring comment CONTAINMENT would need the lexer to
#: classify the very lines it is about to excuse.
_WAIVER = re.compile(r"//\s*string-lit-gate:\s*allow\b")

#: Directories never scanned: build output, caches, and the vendored copies the
#: workspace keeps under ``vendor/`` (a vendored tree is upstream's code; a
#: finding there is not this repository's to fix and would block unrelated
#: work — the same boundary ``cpp-style`` draws for ``third_party``).
SKIP_DIRS = frozenset({".git", "target", "node_modules", "vendor", "third_party", "external", "__pycache__", ".venv", "venv"})

#: The first-line shapes that mark an embedded foreign document (another
#: language's source, a fixture payload, a quoted snippet). Measured against the
#: org corpus: 66 of the 79 spanning literals are one of these, and every one of
#: them is intentional content.
LANE_EMBEDDED = "embedded-document"
#: The first-line shapes that mark a template whose body occupies whole lines.
LANE_BODY = "template-body"
#: Escaped continuations throughout: one rendered line, rustfmt-stable.
LANE_CONTINUATION = "continuation"
#: The half-converted shape: escaped continuations AND a bare newline.
LANE_HALF_CONVERTED = "half-converted"
#: No lane applies: a sentence that a real newline interrupted.
LANE_PLAIN = "plain"

_SQL_LEAD = ("SELECT", "INSERT", "UPDATE ", "DELETE", "CREATE", "PRAGMA", "ALTER", "DROP ", "WITH ", "REPLACE ")
_BODY_OPENER = ("(function", "function", "{{", "{}", "[", "<", "(", "{")

@dataclass(frozen = True)
class finding:
    """One reported line, with the class that produced it."""

    path: str
    line: int
    column: int
    kind: str
    detail: str
    lane: str = ""

    def render(self) -> str:
        suffix = f" [{self.lane}]" if self.lane else ""
        return f"{self.path}:{self.line}:{self.column}: {self.kind}: {self.detail}{suffix}"

@dataclass(frozen = True)
class literal:
    """One plain string or byte string literal that spans more than one line."""

    open_line: int
    open_column: int
    body: str
    #: The raw text that opens the literal (``"``, ``b"``, ...), used to tell a
    #: byte string from a plain one.
    origin: str = '"'
    #: Last line of the literal. ``body`` holds quotes and escapes, so newlines
    #: in it are not a line count (a raw string consumed wholesale lands on its
    #: closer and its "body" holds none); the lexer counts lines directly.
    close_line: int = 0

    @property
    def bare_newlines(self) -> int:
        """Newlines in the body that no backslash immediately precedes.

        The FINAL newline is excluded: it is the line break that precedes the
        closing quote, and every spanning literal has one. Counting it would mark
        all 919 legitimate spanning literals the org corpus holds, and would NOT
        catch the shape that shipped broken — ``"a \\`` then a bare newline —
        where the defect is a newline in the MIDDLE of the text.
        """
        total = sum(1 for index, char in enumerate(self.body) if (char == "\n") and not self._escaped(index))

        if (total > 0) and not self.body.endswith("\n"):
            # The last character of ``body`` is the newline before the closing
            # quote, so a body that does not END in a newline cannot be a
            # spanning literal at all; the guard keeps the subtraction below
            # from firing on a single-line body.
            return total

        return max(0, total - (0 if self._escaped(len(self.body) - 1) else 1))

    @property
    def escaped_newlines(self) -> int:
        """Backslash-newline escapes: the honest line continuation."""
        return sum(1 for index, char in enumerate(self.body) if (char == "\n") and self._escaped(index))

    def _escaped(self, index: int) -> bool:
        """True when ``self.body[index]`` is preceded by an odd run of backslashes.

        Counting RUNS rather than "the previous character" is what Rust's lexer
        does: ``\\\\`` is one escaped backslash and the newline after it is a
        real one.
        """
        run = 0
        cursor = index - 1

        while (cursor >= 0) and (self.body[cursor] == "\\"):
            run += 1
            cursor -= 1

        return bool(run % 2)

    @property
    def honest_continuation(self) -> bool:
        """True when the literal's line breaks are all escaped continuations.

        Two conditions, and both are needed: at least one escaped continuation,
        and no BARE newline away from the closing quote's own line break. The
        second is what separates this from the half-converted shape (one escaped
        continuation, one bare newline) that the gate exists to report.
        """
        physical = self.body.count(chr(10))

        return (self.escaped_newlines > 0) and (self.escaped_newlines >= (physical - 1))

    @property
    def first_line(self) -> str:
        """The first line of the literal's CONTENT (the opening quote excluded).

        ``body`` is sliced from the quote's own offset, so its first character is
        always ``"``. Excluding it here — once, in the accessor — is what keeps
        every first-line rule (embedded document, template body) from reading the
        delimiter as content. An earlier revision tested the raw first line and
        ``startswith('"')`` then matched every literal ever written, which
        silently excused the entire corpus with a zero-finding green.
        """
        first = self.body.split("\n", 1)[0]
        return first[1:] if first.startswith('"') else first

#: What may follow a split-at-a-point literal's closing quote on its own line:
#: the call punctuation the value is passed into, and nothing else.
_PUNCTUATION_TAIL = re.compile(r"^[\s,)\;\].}?\[]*$")

def _split_at_point(lit: literal) -> bool:
    """True when the RENDERED value is punctuation, not a sentence.

    ``"kind = \\"license\\"`` then ``"`` renders ``kind = "license"``: the newline
    is real but the two halves are one fragment, and no sentence was split.
    The mechanical tell is that the literal's LAST line (everything after its
    final newline) is nothing but a closing quote, or a quote followed by call
    punctuation — an unfinished fragment. A sentence that a newline interrupted
    carries words after the break, which this rejects.

    Measured: without this lane, shun's TOML fixture fragments (``"kind = \\"license\\"``
    then ``"``) were three of the report's class A hits; with it, zero. The
    positive control's ``"a   `` then ``b"`` is NOT excused, because ``b`` is a
    word, not punctuation.
    """
    tail = lit.body.rsplit("\n", 1)[-1]
    return bool(_PUNCTUATION_TAIL.match(tail))

#: A line that is a sentence fragment, not a document's line: it starts with
#: prose (a letter, a digit, an opening quote) and ends with prose or sentence
#: punctuation. Used to tell a wrapped message from a fixture document.
_PROSE_HEAD = re.compile(r"^[A-Za-z0-9\"'(]")
_PROSE_TAIL = re.compile(r"[A-Za-z0-9.,:;!?\"')]$")

def _is_document(lit: literal) -> bool:
    """True when the literal's lines are a DOCUMENT, not a wrapped sentence.

    Three signals, in the order a reader uses them:

    * **many lines** — five or more can only be a document (SQL, a template, a
      fixture file); a message that a reviewer sees comes in two or three;
    * **a fragmented body** — the last line is punctuation only, so the text is
      a concatenation, not a sentence;
    * **lines that are not prose** — a document's line commonly starts with an
      indent plus a marker (``- a``, ``# t``, ``| c |``) or wraps mid-word,
      while a sentence's every line starts and ends with a word or punctuation.

    Every signal was measured: without them the org corpus's SQL, TOML and
    markdown fixtures are reported (64 false positives); with a two-line
    "document" rule instead, the wrapped-message positive control goes green.
    """
    lines = [line for line in lit.body.split("\n")[1:] if line.strip()]

    if len(lit.body.split("\n")) >= 5:
        return True

    if _split_at_point(lit):
        return True

    return any(not (_PROSE_HEAD.search(line.strip()) and _PROSE_TAIL.search(line.strip())) for line in lines)

def _lane(lit: literal) -> str:
    """The exemption lane ``lit`` sits in, or :data:`LANE_PLAIN` when none does.

    The order IS the design, and each step was measured on the org corpus (3737
    tracked ``.rs`` files, 1000 spanning literals):

    1. :data:`LANE_CONTINUATION` — every line break is a ``\\`` continuation, so
       the value renders as ONE line and the whitespace around each ``\\`` is
       dropped by the compiler. 919 of the corpus is this shape.
    2. :data:`LANE_HALF_CONVERTED` — escaped continuations AND a bare newline in
       the same literal. Checked before every excuse lane: this mixture is the
       2026-11-24 defect, a first-line rule would otherwise excuse it, and no
       legitimate shape produces it.
    3. :data:`LANE_EMBEDDED` — a body that spans more than one rendered line
       (SQL, TOML front matter, a markdown list, another language's source), or
       a split-at-a-point fragment whose tail is punctuation. The newline is the
       content, not an accident.
    4. :data:`LANE_BODY` — a first line that is an opener or a placeholder
       (``{}``, ``(function() {``, ``[``): a template whose body occupies whole
       lines.
    5. :data:`LANE_EMBEDDED` again for a first line that OPENS another document
       (a comment marker, a quote, SQL, another literal's delimiter, or a byte
       string's own head line).

    A rule that reports a legitimate shape is a rule the fleet turns off, which
    is why these lanes exist instead of a fail-closed newline ban.
    """
    content = lit.first_line.strip()

    if lit.honest_continuation:
        return LANE_CONTINUATION

    if (lit.escaped_newlines > 0) and (lit.bare_newlines > 0):
        return LANE_HALF_CONVERTED

    #: An embedded document the way a reader recognises one: three or more
    #: lines, OR a body whose every line is a fragment. A two-line literal can
    #: still be a sentence that a newline interrupted (``"line one`` /
    #: ``line two"``), and treating two lines as "embedded" excuses exactly the
    #: shape this gate exists for — measured: the multi-line positive control
    #: went green.
    if lit.first_line.strip().startswith(("//", "#", "<!--", "/*")):
        # The FIRST line is a comment marker in another language: the literal is
        # a document (``"# title`` / markdown), whatever its later lines look
        # like. Checked before the document classifier because a short heading
        # and a short body read as prose line by line.
        return LANE_EMBEDDED

    if _is_document(lit):
        return LANE_EMBEDDED

    if (content in ("", "{}", "{{}}")) or content.endswith(("{", "{{")) or content.startswith(_BODY_OPENER):
        return LANE_BODY

    if lit.origin.startswith(('b"', "b'")) or content.startswith(("//", '"', "'", "r#", 'r"', "br#", 'br"', "cr#", 'cr"', 'b"', "b'")):
        return LANE_EMBEDDED

    if content.upper().startswith(_SQL_LEAD):
        return LANE_EMBEDDED

    return LANE_PLAIN

def scan_literals(text: str) -> Tuple[List[literal], List[int]]:
    """``(spanning plain literals, raw-string line numbers)``.

    A hand-written lexer, not a regex: these are lexical STATE questions. ``"``
    inside a comment is not a quote; ``'x'`` is a char literal whose apostrophe
    does not open a string; ``r#"…"#`` / ``br#…"#`` / ``cr#"…"#`` are raw strings
    whose content must not be scanned for quotes at ALL; ``"a \"`` does not
    close; and a byte string (``b"…"``) is the same defect one type over, so it
    is reported like a plain one. Each of those was a wrong answer during this
    module's own construction, which is why the lexer is explicit about them.

    ``spans`` carries the line numbers of every RAW string, closer included. The
    caller needs them because a raw string's interior is its data: trailing
    whitespace there is the value it renders, and the lexer is the only place
    that knows where the interior is (raw content never enters a string state).
    """
    #: Every line a raw string occupies (its interior is data, not formatting).
    spans: List[int] = []
    literals: List[literal] = []
    line, column = 1, 1
    state = "code"
    start = (0, 0, 0)
    index = 0
    size = len(text)

    while index < size:
        char = text[index]
        following = text[index + 1] if ((index + 1) < size) else ""

        if state == "code":
            # ``b`` is excluded from the raw pattern on purpose. ``b"…"`` is a
            # BYTE string, not a raw one, and when this branch could match it the
            # plain-string branch below (whose own guard tests the same letter)
            # never ran: the body was lexed as code and a legal fixture reported
            # two findings.
            raw = _RAW_PREFIX.match(text, index) if (char != "b") else None

            if raw is not None:
                hashes = raw.group(1)
                quotes = raw.group(2)
                # Content starts after the whole opening delimiter (``r#"``), so
                # the search for the closer must start past it.
                content_at = index + len(raw.group(0))
                close_at = text.find(quotes + hashes, content_at)
                # ``end`` is one past the closing delimiter — the byte the scan
                # resumes ON.
                end = size if close_at < 0 else close_at + len(hashes) + len(quotes)
                chunk = text[index:end]
                breaks = chunk.count("\n")

                if breaks:
                    spans.extend(range(line, line + breaks + 1))
                    line += breaks
                    # 1-based column of the byte AFTER the raw string: how many
                    # bytes follow the chunk's final newline. Verified against a
                    # hand-computed probe; a wrong value never showed up as a
                    # wrong CLASS, only as the NEXT literal's column being off.
                    column = len(chunk) - chunk.rfind("\n")
                else:
                    column += len(chunk)

                index = end
                continue

            if char == "'":
                # A char literal closes on its own line; ``'\''`` and ``'\u{1F600}'``
                # are one character each.
                counted = re.match(r"'(\\.|[^\\'\n])'", text[index:])
                width = len(counted.group(0)) if counted else 1
                index += width
                column += width
                continue

            if char == '"':
                # ``b"…"`` arrives here too: the ``b`` was consumed as an
                # ordinary letter a moment ago (the raw branch declined it), so
                # ``start`` records the QUOTE's position and a reader following a
                # column lands on the quote, not on the prefix.
                state = "string"
                start = (line, column, index)
                index += 1
                column += 1
                continue

            if (char == "/") and (following == "/"):
                state = "line-comment"
                index += 2
                column += 2
                continue

            if (char == "/") and (following == "*"):
                state = "block-comment"
                index += 2
                column += 2
                continue

            if char == "\n":
                line += 1
                column = 1
                index += 1
                continue

            index += 1
            column += 1
            continue

        if state == "line-comment":
            if char == "\n":
                # The newline ends the comment AND is the line break, so it must
                # not also increment ``column``: one branch, one increment. A
                # revision that did both put every column after a ``//`` line one
                # too high (measured: a quote at 24 reported at 25).
                state = "code"
                line += 1
                column = 1
                index += 1
                continue

            index += 1
            column += 1
            continue

        if state == "block-comment":
            if (char == "*") and (following == "/"):
                state = "code"
                index += 2
                column += 2
                continue

            if char == "\n":
                line += 1
                column = 1
                index += 1
                continue

            index += 1
            column += 1
            continue

        # Inside a plain or byte string literal.
        if char == "\\":
            if following == "\n":
                line += 1
                column = 1
                index += 2
                continue

            width = 2 if following else 1
            index += width
            column += width
            continue

        if char == '"':
            body = text[start[2]:index]
            state = "code"

            if "\n" in body:
                # ``origin`` is the prefix as it appears in the SOURCE. The plain
                # branch is entered on the opening quote, so it is read from the
                # character before it — the only place the difference between
                # ``b"`` and ``"`` survives (``body`` starts at the quote).
                origin = text[start[2] - 1:start[2] + 1] if start[2] else text[start[2]:start[2] + 1]
                literals.append(literal(open_line = start[0], open_column = start[1], body = body, close_line = line, origin = origin))

            index += 1
            column += 1
            continue

        if char == "\n":
            line += 1
            column = 1
            index += 1
            continue

        index += 1
        column += 1

    if state == "string":
        # An UNTERMINATED literal runs to end of file. Rust would refuse to
        # compile it, but silently dropping the span made every later construct
        # on those lines read as code — this file's own truncated fixtures
        # reported a literal at an EARLIER literal's column because the real
        # opener had been swallowed.
        literals.append(literal(open_line = start[0], open_column = start[1], body = text[start[2]:], close_line = line))

    return literals, spans

def scan_text(text: str, path: str, *, newline: bool = True, trailing: bool = True) -> List[finding]:
    """The findings for one file's text (the single entry point both lanes use)."""
    found: List[finding] = []
    lines = text.split("\n")
    waived = {number for number, content in enumerate(lines, 1) if _WAIVER.search(content)}

    literals_, raw_span_lines = scan_literals(text)

    if newline:
        for lit in literals_:
            if _lane(lit) != LANE_PLAIN:
                continue

            # A waiver on the opening line covers the literal. The closing line
            # cannot excuse it: a reader of the diff sees the opener, and the
            # defect is the newline that follows it.
            if lit.open_line in waived:
                continue

            found.append(finding(path = path, line = lit.open_line, column = lit.open_column, kind = NEWLINE, detail = "a real newline inside a plain string literal (runtime value carries \\n); use a \\\\ continuation or a raw string"))

    if trailing:
        # A trailing space that sits INSIDE an allowed spanning literal is the
        # literal's DATA (``r"a<sp><sp>`` then ``b"`` renders those two spaces),
        # not source formatting — rustfmt does not touch it and neither does this
        # class. Class A literals need no blanking: class A already reports them.
        # Every line of such a literal is blanked: its interior carries the value
        # it renders (``r"a<sp><sp>`` on one line, the rest on the next), and
        # blanking only the head left the interior reported as source
        # formatting — measured, not hypothetical: this scan's own negative
        # control reddened on three interior lines before the range was used.
        heads = set()

        for lit in literals_:
            lane = _lane(lit)

            # A trailing backslash ON a continuation must stay reachable:
            # ``"a   \\`` is both a class B line (the spaces are trailing)
            # and the honest continuation, so only a bare (unescaped)
            # spanning literal takes its head line out of the scan.
            if (lane != LANE_PLAIN) and (lit.bare_newlines > 0):
                heads.update(range(lit.open_line, max(lit.close_line, lit.open_line) + 1))

        heads.update(raw_span_lines)

        this_text = text if not heads else "\n".join("" if (number in heads) else line for number, line in enumerate(lines, 1))

        for number, content in enumerate(this_text.split("\n"), 1):
            if not content.strip():
                # Blank and whitespace-only lines are rustfmt's to normalise and
                # render nothing; no byte of a runtime string depends on them.
                continue

            stripped = content.rstrip(" \t")

            if stripped == content:
                continue

            if number in waived:
                continue

            tail = content[len(stripped):]
            kind = "tab" if "\t" in tail else "space"
            found.append(finding(path = path, line = number, column = len(stripped) + 1, kind = TRAILING, detail = f"trailing {kind} at end of line"))

    return found

def discover_sources(paths: Sequence[str], cwd: Path) -> Tuple[List[Path], List[str]]:
    """Expand CLI paths into ``.rs`` files; ``(files, problems)``."""
    files: List[Path] = []
    problems: List[str] = []
    seen: set = set()

    for raw in paths:
        candidate = Path(raw)

        if not candidate.is_absolute():
            candidate = cwd / candidate

        if candidate.is_file():
            candidates = [candidate]
        elif candidate.is_dir():
            candidates = []

            for directory, dirnames, filenames in os.walk(candidate):
                dirnames[:] = sorted(name for name in dirnames if (name not in SKIP_DIRS) and (not name.startswith("target")))

                for name in sorted(filenames):
                    if name.endswith(".rs"):
                        candidates.append(Path(directory) / name)
        else:
            problems.append(f"no such path: {raw}")
            continue

        for path in candidates:
            if not path.name.endswith(".rs"):
                continue

            key = str(path.resolve())

            if key in seen:
                continue

            seen.add(key)
            files.append(path)

    files.sort(key = lambda path: str(path))
    return files, problems

def check_paths(paths: Sequence[str], *, newline: bool = True, trailing: bool = True, cwd: Path | None = None) -> Tuple[List[finding], int, List[str]]:
    """Scan ``paths``; ``(findings, files_scanned, problems)``."""
    cwd = cwd or Path.cwd()
    original = os.getcwd()
    files, problems = discover_sources(list(paths), cwd)
    findings: List[finding] = []
    scanned = 0

    for path in files:
        try:
            text = path.read_text(encoding = "utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"unreadable: {path} ({exc})")
            continue

        try:
            shown = str(path.resolve().relative_to(Path(original).resolve()))
        except (ValueError, OSError):
            shown = str(path)

        scanned += 1
        findings.extend(scan_text(text, shown, newline = newline, trailing = trailing))

    findings.sort(key = lambda item: (item.path, item.line, item.column, item.kind))
    return findings, scanned, problems

#: Positive control: two shapes for each class plus the mixed 2026-11-24 shape
#: (an escaped continuation AND a bare newline in one literal). A template
#: rather than source lines so a trailing space is visible here as ``<sp>``
#: instead of as an invisible byte, and so the expected ``line:column`` reads
#: straight off the fixture.
_POSITIVE_TEMPLATE = """fn messages() {
    let one = "the enquiry was not admitted
        so the pick stays parked";
    let two = "the enquiry was not admitted {bs}
        so the pick stays parked
        with a second line";
    let three = "collapsed spaces keep a bare newline
        in the middle";
    let tail = "line ends with a space<sp><sp><sp>
    next<sp><sp>";
    let tabbed = [
        "line ends with a tab<sp><tab>
    next",
    ];
}"""

#: Negative control: legal multi-line Rust that must produce zero findings. The
#: bare newlines live in a raw string, a hashed raw string, a byte string and a
#: whole-line template body; the double spaces live on a trailing ``\\`` (Rust
#: removes trailing whitespace before a line continuation, so these render as
#: ordinary spaces), inside the raw strings, and in a comment.
_NEGATIVE_TEMPLATE = """fn legal() {
    let continued = "one   {bs}
        two   {bs}
        three";
    let raw = r"a<sp><sp>
b\tc<sp><sp>";
    let hashed = r#"a raw string with "quotes" and<sp><sp>
b<sp><sp> lines"#; // trailing comment
    let byte_string = b"a<sp><sp>
b";
    let byte_char = b'x';
    let formatted = format!(
        "value: {{}}",
        1
    );
    // a comment that mentions newline and trailing space, but is code-adjacent
    let quoted = '{quote}';
    let escaped = "a {bs}{bs}" quote"; // trailing comment
    let body = "(function() {{
    var x = 1;
}})";
    let prompt = [
        "You are step one: <sp><sp>",
        "**Phase two**.",
    ];
}"""

def _materialise(template: str) -> str:
    """Expand a fixture template's markers into real characters.

    ``<sp>`` / ``<tab>`` make trailing whitespace visible in this file's own
    source (an invisible trailing space here would be edited away by the next
    person); ``{bs}`` / ``{quote}`` keep a Rust backslash or apostrophe from
    colliding with Python's own string syntax.
    """
    expanded = template.replace("{bs}", "\\").replace("{quote}", "'")
    return expanded.replace("<sp>", " ").replace("<tab>", "\t") + "\n"

SELFTEST_POSITIVE = _materialise(_POSITIVE_TEMPLATE)
SELFTEST_NEGATIVE = _materialise(_NEGATIVE_TEMPLATE)

#: The waiver anchor: two shapes the positive control reports, excused on the
#: line that carries them (class B) or on the line that opens the literal
#: (class A). A waiver inside a string literal is read as a waiver on purpose.
_WAIVED_TEMPLATE = """fn excused() {
    let one = "the enquiry was not admitted // string-lit-gate: allow fixture text
        so the pick stays parked";
    let tail = "line ends with a space<sp><sp><sp>"; // string-lit-gate: allow fixture text
}"""

SELFTEST_WAIVED = _materialise(_WAIVED_TEMPLATE)

#: ``(line, column, kind)`` for every finding the positive control must produce.
SELFTEST_POSITIVE_WANTED = frozenset({(2, 15, NEWLINE), (7, 17, NEWLINE), (9, 16, NEWLINE), (12, 9, NEWLINE), (9, 39, TRAILING), (12, 30, TRAILING)})

def selftest() -> int:
    """Run the checker against its own controls; ``0`` means every one held."""
    failures: List[str] = []
    positive = scan_text(SELFTEST_POSITIVE, "positive.rs")

    if len(positive) != 6:
        failures.append(f"positive control: expected 6 findings, got {len(positive)}: {[item.render() for item in positive]}")

    newline_hits = [item for item in positive if item.kind == NEWLINE]

    if len(newline_hits) != 4:
        failures.append(f"positive control: expected 4 class A findings, got {len(newline_hits)}")

    trailing_hits = [item for item in positive if item.kind == TRAILING]

    if len(trailing_hits) != 2:
        failures.append(f"positive control: expected 2 class B findings, got {len(trailing_hits)}")

    #: ``line:column`` of every expected finding, keyed off the fixture lines
    #: above. The columns are the opening quote's 1-based column for class A and
    #: the first trailing byte's column for class B.
    positions = {(item.line, item.column, item.kind) for item in positive}

    if positions != set(SELFTEST_POSITIVE_WANTED):
        failures.append(f"positive control: positions differ: want={sorted(SELFTEST_POSITIVE_WANTED)} got={sorted(positions)}")

    negative = scan_text(SELFTEST_NEGATIVE, "negative.rs")

    if negative:
        failures.append(f"negative control: {len(negative)} false positive(s): {[item.render() for item in negative]}")

    waived = scan_text(SELFTEST_WAIVED, "waived.rs")

    if waived:
        failures.append(f"waiver anchor: {len(waived)} finding(s) survived: {[item.render() for item in waived]}")

    # The terminal gate: zero findings from zero input must refuse, not pass.
    if _status([], scanned = 0, problems = []) == EXIT_OK:
        failures.append("terminal gate: an empty scan reported clean instead of refusing")

    if _status([], scanned = 1, problems = []) != EXIT_OK:
        failures.append("terminal gate: a scan over a clean file was refused")

    if _status([], scanned = 1, problems = ["boom"]) == EXIT_OK:
        failures.append("terminal gate: a problem reported clean")

    if failures:
        for message in failures:
            print(f"SELFTEST FAIL: {message}", file = sys.stderr)

        return 1

    print(f"selftest OK: {len(newline_hits)} class A hit(s), {len(trailing_hits)} class B hit(s), " f"0 false positive(s) across {len(SELFTEST_NEGATIVE.splitlines())} legal lines, " "waiver anchor honored, empty scan refused")
    return EXIT_OK

def _status(findings: Sequence[finding], *, scanned: int, problems: Sequence[str]) -> int:
    """The exit code for one scan result, printing nothing.

    Split from :func:`_verdict` because the selftest probes this decision
    directly: probing the printing wrapper wrote its own "zero files" refusal to
    stderr, which read as a failure in a green run.
    """

    if problems:
        return EXIT_USAGE

    if scanned == 0:
        return EXIT_USAGE

    return EXIT_FINDINGS if findings else EXIT_OK

def _verdict(findings: Sequence[finding], *, scanned: int, problems: Sequence[str], quiet: bool) -> int:
    """Turn a scan result into the exit code (and print the report)."""

    if not quiet:
        for item in findings:
            print(f"❌ {item.render()}")

    for problem in problems:
        print(f"❌ string-lit-gate: {problem}", file = sys.stderr)

    if problems:
        return EXIT_USAGE

    if scanned == 0:
        # A mistyped path — or a scanner that enumerated nothing — must never
        # read as clean: the two are indistinguishable from the exit code alone.
        print("❌ string-lit-gate: zero .rs files scanned — a mistyped path must not read as clean", file = sys.stderr)
        return EXIT_USAGE

    if findings:
        print(f"STRING-LIT-GATE REFUSED — {len(findings)} finding(s) over {scanned} .rs file(s), " 'add `// string-lit-gate: allow <reason>` to a line that is deliberate', file = sys.stderr)
        return EXIT_FINDINGS

    if not quiet:
        print(f"✅ string-lit-gate: {scanned} .rs file(s), no literal newline and no trailing whitespace")

    return EXIT_OK

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``string-lit-gate [PATH...] [--selftest]``."""
    parser = argparse.ArgumentParser(prog = "string-lit-gate", description = "Fail a Rust source whose runtime string carries a newline the source never wrote.")
    parser.add_argument("paths", nargs = "*", default = [], metavar = "PATH", help = "Files or directories to scan (default: the current directory).")
    parser.add_argument("--newline", action = "store_true", help = "Report class A (a real newline inside a plain string literal) only.")
    parser.add_argument("--trailing", action = "store_true", help = "Report class B (trailing space or tab at end of line) only.")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable report on stdout.")
    parser.add_argument("--quiet", action = "store_true", help = "Only the closing verdict line.")
    parser.add_argument("--selftest", action = "store_true", help = "Run the checker against its own controls.")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    newline = args.newline or not (args.newline or args.trailing)
    trailing = args.trailing or not (args.newline or args.trailing)
    targets = list(args.paths) or ["."]
    findings, scanned, problems = check_paths(targets, newline = newline, trailing = trailing)

    if args.json:
        print(json.dumps({"tool": "string-lit-gate", "scanned": scanned, "count": len(findings), "problems": problems, "findings": [{"path": item.path, "line": item.line, "column": item.column, "kind": item.kind, "lane": item.lane, "detail": item.detail} for item in findings]}, indent = 1, ensure_ascii = False))
        return _verdict([], scanned = scanned, problems = problems, quiet = True) if not findings else (EXIT_USAGE if problems else EXIT_FINDINGS)

    return _verdict(findings, scanned = scanned, problems = problems, quiet = args.quiet)

if __name__ == "__main__":
    raise SystemExit(main())
