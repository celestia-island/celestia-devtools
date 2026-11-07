#!/usr/bin/env python3
"""Lightweight structural analysis on top of the C++ token stream.

*Why this module exists:* the layout rules need a little more than a token list.
A control-brace check has to find the closing parenthesis that belongs to an
opening one, a do-while check has to know which block a closing brace closes, a
one-statement-per-line check has to know the brace and parenthesis depth of every
semicolon, and the include rules need physical lines. This module computes all of
that once per file and hands it to the rule functions.

AnalyzedFile exposes:

- the normalized source text and its physical lines (for blank-line counting);
- tokens, significant (comments and preprocessor directives dropped, newlines
  kept) and code (newlines dropped too);
- delimiter matching and depth arrays for parentheses, brackets and braces;
- the parsed preprocessor directives and the macro names defined with #define
  (used to recognise macro invocations such as ASSERT with a spaced parenthesis);
- tokenizer and structural warnings (unterminated literals, unmatched
  delimiters, ...).

The analysis is deliberately shallow: it never parses expressions, types or
template argument lists, and it does not evaluate preprocessor conditionals --
both branches of an #if are simply part of the token stream.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from celestia_devtools.lint.cpp.tokenizer import (NEWLINE, OP, PREPROC, TRIVIA, directive, token_record, tokenized, split_lines, tokenize_bytes)

_OPENERS = {"(": "(", "[": "[", "{": "{"}
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_PAIRS = {"(": ")", "[": "]", "{": "}"}

_MACRO_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)")

@dataclass
class finding_record:
    """One reported violation (or a warning from an undecidable check)."""

    path: Path
    line: int
    col: int
    rule: str
    message: str
    severity: str = "error"

@dataclass
class analyzed_file:
    """Everything the rule functions need to know about one source file."""

    path: Path
    text: str
    lines: List[str]
    tokens: List[token_record]
    significant: List[token_record]
    code: List[token_record]
    code_token_indices: List[int]
    directives: List[directive]
    warnings: List[str]
    defined_macros: frozenset
    paren_match: Dict[int, int] = field(default_factory = dict)
    bracket_match: Dict[int, int] = field(default_factory = dict)
    brace_match: Dict[int, int] = field(default_factory = dict)
    paren_depth: List[int] = field(default_factory = list)
    brace_depth: List[int] = field(default_factory = list)

    # -- token navigation -------------------------------------------------
    def next_code(self, position: int) -> Tuple[int, token_record] | None:
        """The code token after *position*, as (position, token)."""
        following = position + 1

        if following >= len(self.code):
            return None

        return following, self.code[following]

    def prev_code(self, position: int) -> Tuple[int, token_record] | None:
        """The code token before *position*, as (position, token)."""
        previous = position - 1

        if previous < 0:
            return None

        return previous, self.code[previous]

    def trivia_between(self, first: int, second: int) -> bool:
        """True when a comment or preprocessor directive sits between two code positions.

        Positions are indices into code, not into tokens; *first* must be lower
        than *second*. Rules use this to tell "nothing between these tokens" from
        "a comment or an #if branch sits between them", where the layout they are
        looking at cannot be decided locally.
        """
        start = self.code_token_indices[first] + 1
        end = self.code_token_indices[second]

        for token in self.tokens[start:end]:
            if token.kind in TRIVIA:
                return True

        return False

    # -- physical lines ---------------------------------------------------
    def line_text(self, line: int) -> str:
        """Text of a 1-based physical line (empty for out-of-range lines)."""

        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]

        return ""

    def is_blank_line(self, line: int) -> bool:
        return not self.line_text(line).strip()

    def blank_lines_between(self, first_line: int, second_line: int) -> int:
        """Number of blank physical lines strictly between the two 1-based lines."""
        return sum(1 for line in range(first_line + 1, second_line) if self.is_blank_line(line))

def _match_delimiters(code: Sequence[token_record]) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, int], List[int], List[int], List[str]]:
    """Match ()/[]/{} pairs and compute paren/brace depth before each code token."""
    paren_match: Dict[int, int] = {}
    bracket_match: Dict[int, int] = {}
    brace_match: Dict[int, int] = {}
    tables = {"(": paren_match, "[": bracket_match, "{": brace_match}
    stacks: Dict[str, List[int]] = {"(": [], "[": [], "{": []}
    paren_depth: List[int] = []
    brace_depth: List[int] = []
    warnings: List[str] = []

    for position, token in enumerate(code):
        paren_depth.append(len(stacks["("]))
        brace_depth.append(len(stacks["{"]))

        if token.kind != OP:
            continue

        text = token.text

        if text in _OPENERS:
            stacks[text].append(position)
        elif text in _CLOSERS:
            opener = _CLOSERS[text]
            stack = stacks[opener]

            if not stack:
                warnings.append("line %d: unmatched %r" % (token.line, text))
                continue

            open_position = stack.pop()
            table = tables[opener]
            table[open_position] = position
            table[position] = open_position

    for opener, stack in stacks.items():
        for open_position in stack:
            warnings.append("line %d: unterminated %r" % (code[open_position].line, opener))

    return paren_match, bracket_match, brace_match, paren_depth, brace_depth, warnings

def _defined_macros(tokenized: tokenized) -> frozenset:
    names = set()

    for token in tokenized.tokens:
        if token.kind != PREPROC:
            continue

        match = _MACRO_DEFINE.match(token.text)

        if match:
            names.add(match.group(1))

    return frozenset(names)

def analyze_bytes(path: Path, data: bytes) -> analyzed_file:
    """Analyze raw file bytes (decoded as UTF-8 with replacement)."""
    tokenized = tokenize_bytes(data)
    (paren_match, bracket_match, brace_match, paren_depth, brace_depth, structural_warnings) = _match_delimiters(tokenized.code)
    code_token_indices = [index for index, token in enumerate(tokenized.tokens)if (token.kind not in TRIVIA) and (token.kind != NEWLINE)]
    return analyzed_file(path = path, text = tokenized.text, lines = split_lines(tokenized.text), tokens = tokenized.tokens, significant = tokenized.significant, code = tokenized.code, code_token_indices = code_token_indices, directives = list(tokenized.directives), warnings = list(tokenized.warnings) + structural_warnings, defined_macros = _defined_macros(tokenized), paren_match = paren_match, bracket_match = bracket_match, brace_match = brace_match, paren_depth = paren_depth, brace_depth = brace_depth)

def analyze(path: Path) -> analyzed_file:
    """Read and analyze one source file."""
    return analyze_bytes(path, path.read_bytes())
