#!/usr/bin/env python3
"""Rule implementations for the cpp-style checker (check only, never writes).

*Why one function per rule:* each rule is a small read-only query over
AnalyzedFile, registered in RULES order and guarded by the selected-rule set, so
--select/--ignore, config select/ignore and list-rules all agree with the code.

The judgments below are the implementation; the human-readable contract for each
rule (including the deliberate exceptions) lives in the rules catalogue, which is
what cpp-style list-rules prints.
"""

from __future__ import annotations
import re
from typing import Iterator, List, Optional, Sequence, Tuple
from celestia_devtools.lint.cpp.config import Config, matches_globs, path_variants
from celestia_devtools.lint.cpp.rules import RULES
from celestia_devtools.lint.cpp.structure import AnalyzedFile, Finding
from celestia_devtools.lint.cpp.tokenizer import COMMENT, IDENT, NEWLINE, OP, PREPROC, Token

_CONTROL_BLOCK = frozenset({"if", "for", "while", "switch", "catch"})
_HEADER_SUFFIXES = frozenset({".h", ".hpp", ".hh", ".hxx"})

# An all-caps identifier that is not defined in this file can still be a macro
# from an included header; that is the case the warning-severity hint covers.
_MACRO_NAME = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_LICENSE_MARKER = re.compile(r"license|spdx|copyright", re.IGNORECASE)

_INCLUDE_DIRECTIVE = re.compile(r"^\s*#\s*include\b", re.IGNORECASE)
_INCLUDE_TARGET = re.compile(r'^\s*#\s*include\s*(?:<([^>\n]*)>|"([^"\n]*)")', re.IGNORECASE)

def iter_includes(af: AnalyzedFile) -> Iterator[Tuple[Token, Optional[str]]]:
    """Yield each #include directive token and its header name (None for macro includes)."""

    for token in af.tokens:
        if token.kind != PREPROC:
            continue

        if not _INCLUDE_DIRECTIVE.match(token.text):
            continue

        match = _INCLUDE_TARGET.match(token.text)
        header = None

        if match is not None:
            header = match.group(1) if match.group(1) is not None else match.group(2)

        yield token, header

def _paren_after(af: AnalyzedFile, position: int) -> Optional[int]:
    following = af.next_code(position)

    if following is None:
        return None

    index, token = following

    if (token.kind == OP) and (token.text == "("):
        return index

    return None

def _body_findings(af: AnalyzedFile, after_position: int, anchor: Token, rule: str, description: str) -> List[Finding]:
    """Classify the token after an anchor as braced, missing or undecidable."""
    following = af.next_code(after_position)

    if following is None:
        return []

    index, token = following

    if (token.kind == OP) and (token.text == "{"):
        return []

    if af.trivia_between(after_position, index):
        return [Finding(af.path, anchor.line, anchor.col, rule, "cannot decide whether %s has a braced body: a comment or preprocessor" " directive sits between the statement and its body; check skipped" % description, severity = "warning")]

    return [Finding(af.path, anchor.line, anchor.col, rule, "expected '{' after %s; single-statement bodies are not allowed" % description)]

def _do_while_conditions(af: AnalyzedFile) -> set:
    """Code positions of the while tokens that close a do-while statement.

    The body may be a braced block (the next token after the matching closing
    brace is the while) or a single unbraced statement (the next token after its
    terminating semicolon is the while). Both must be recognised, otherwise the
    trailing condition of a do-while is reported as a control statement without a
    brace.
    """
    positions: set = set()

    for position, token in enumerate(af.code):
        if (token.kind != IDENT) or (token.text != "do"):
            continue

        following = af.next_code(position)

        if following is None:
            continue

        next_position, next_token = following

        if (next_token.kind == OP) and (next_token.text == "{"):
            closing = af.brace_match.get(next_position)

            if closing is None:
                continue

            after = af.next_code(closing)
        else:
            after = None
            depth = af.brace_depth[position]

            for scan in range(next_position, len(af.code)):
                if af.brace_depth[scan] < depth:
                    break

                candidate = af.code[scan]

                if (af.brace_depth[scan] == depth) and (candidate.kind == OP) and (candidate.text == ";"):
                    after = af.next_code(scan)
                    break

        if after is None:
            continue

        if (after[1].kind == IDENT) and (after[1].text == "while"):
            positions.add(after[0])

    return positions

def _semicolon_adjacency(af: AnalyzedFile, position: int) -> Optional[bool]:
    """True when a for-header semicolon touches both neighbours; None if undecidable."""
    semicolon = af.code[position]
    previous = af.prev_code(position)
    following = af.next_code(position)

    if (previous is None) or (following is None):
        return False

    prev_index, prev_token = previous
    next_index, next_token = following

    if af.trivia_between(prev_index, position) or af.trivia_between(position, next_index):
        return None

    left = (prev_token.line == semicolon.line) and (prev_token.end_col == semicolon.col)
    right = (next_token.line == semicolon.line) and (next_token.col == semicolon.end_col)
    return left and right

def check_no_space_before_paren(af: AnalyzedFile, config: Config) -> List[Finding]:
    """Identifiers and control keywords must be followed directly by an opening parenthesis."""
    findings: List[Finding] = []
    allowed = set(config.effective_allow_space_before_paren)

    for position, token in enumerate(af.code):
        if (token.kind != IDENT) or (token.text in allowed):
            continue

        following = af.next_code(position)

        if following is None:
            continue

        next_position, next_token = following

        if (next_token.kind != OP) or (next_token.text != "("):
            continue

        if af.trivia_between(position, next_position):
            continue

        if (next_token.line == token.line) and (next_token.col == token.end_col):
            continue

        is_macro = (token.text in af.defined_macros) or (_MACRO_NAME.match(token.text) is not None)

        if is_macro:
            if config.macro_style == "ignore":
                continue

            findings.append(Finding(af.path, token.line, token.col, "no-space-before-paren", "space before '(' after %r, which looks like a macro; macro" " invocations are written as NAME(...) by this convention" " (macro_style = 'ignore' silences such findings)" % token.text, severity = "warning"))
            continue

        findings.append(Finding(af.path, token.line, token.col, "no-space-before-paren", "expected no whitespace between %r and its '('" % token.text))

    return findings

def check_for_semicolon_spacing(af: AnalyzedFile, config: Config) -> List[Finding]:
    """for headers must have exactly two semicolons, each touching both neighbours."""
    findings: List[Finding] = []

    for position, token in enumerate(af.code):
        if (token.kind != IDENT) or (token.text != "for"):
            continue

        opening = _paren_after(af, position)

        if opening is None:
            continue

        closing = af.paren_match.get(opening)

        if closing is None:
            continue

        header_depth = af.paren_depth[opening] + 1
        semicolons = [index for index in range(opening + 1, closing)if (af.paren_depth[index] == header_depth) and (af.code[index].kind == OP) and (af.code[index].text == ";")]

        if len(semicolons) != 2:
            continue

        for index in semicolons:
            adjacency = _semicolon_adjacency(af, index)

            if (adjacency is None) or adjacency:
                continue

            semicolon = af.code[index]
            findings.append(Finding(af.path, semicolon.line, semicolon.col, "for-semicolon-spacing", "for-header ';' must have no whitespace on either side:" " for(init;condition;step)"))

    return findings

def check_control_braces(af: AnalyzedFile, config: Config) -> List[Finding]:
    """if/for/while/switch/catch/do/else bodies must be braced blocks."""
    findings: List[Finding] = []
    do_while_conditions = _do_while_conditions(af)

    for position, token in enumerate(af.code):
        if token.kind != IDENT:
            continue

        text = token.text

        if text in _CONTROL_BLOCK:
            candidate = position
            following = af.next_code(position)

            if ((text == "if") and (following is not None) and (following[1].kind == IDENT) and (following[1].text == "constexpr")):
                candidate = following[0]

            opening = _paren_after(af, candidate)

            if opening is None:
                continue

            closing = af.paren_match.get(opening)

            if closing is None:
                continue

            if (text == "while") and (position in do_while_conditions):
                continue

            findings.extend(_body_findings(af, closing, token, "control-braces", "the condition of %r" % text))
        elif text == "do":
            findings.extend(_body_findings(af, position, token, "control-braces", "'do'"))
        elif text == "else":
            following = af.next_code(position)

            if (following is not None) and (following[1].kind == IDENT) and (following[1].text == "if"):
                continue

            findings.extend(_body_findings(af, position, token, "control-braces", "'else'"))

    return findings

def check_one_statement_per_line(af: AnalyzedFile, config: Config) -> List[Finding]:
    """At most one statement terminator per physical line and brace depth."""
    groups: dict = {}

    for position, token in enumerate(af.code):
        if (token.kind != OP) or (token.text != ";"):
            continue

        if af.paren_depth[position] != 0:
            continue

        key = (token.line, af.brace_depth[position])
        groups.setdefault(key, []).append(position)

    findings: List[Finding] = []

    for positions in groups.values():
        if len(positions) < 2:
            continue

        second = af.code[positions[1]]
        findings.append(Finding(af.path, second.line, second.col, "one-statement-per-line", "multiple statements on one physical line (%d statement terminators);" " put each statement on its own line" % len(positions), severity = "warning"))

    return findings

def _leading_comment_group(af: AnalyzedFile) -> List[Token]:
    """The first contiguous run of comments at the very top of the file."""
    group: List[Token] = []

    for token in af.tokens:
        if token.kind == NEWLINE:
            continue

        if token.kind == COMMENT:
            group.append(token)
            continue

        break

    return group

def _is_doxygen_block(text: str) -> bool:
    return (text.startswith("/**") and (text != "/**/")) or text.startswith("/*!")

def check_line_comments_only(af: AnalyzedFile, config: Config) -> List[Finding]:
    """Block comments are only allowed in the leading license/SPDX/copyright group."""
    findings: List[Finding] = []
    leading = _leading_comment_group(af)
    license_header = bool(leading) and (_LICENSE_MARKER.search("\n".join(token.text for token in leading)) is not None)
    leading_ids = {id(token) for token in leading}

    for token in af.tokens:
        if token.kind != COMMENT:
            continue

        text = token.text

        if text.startswith("//"):
            continue

        doxygen = _is_doxygen_block(text)

        if doxygen and config.allow_doxygen:
            continue

        if license_header and (id(token) in leading_ids):
            continue

        if doxygen:
            message = "Doxygen block comment is not allowed; use line comments" " (allow_doxygen = true keeps them)"
        else:
            message = "block comments are not allowed outside the leading license/SPDX" " header; use line comments"

        findings.append(Finding(af.path, token.line, token.col, "line-comments-only", message))

    return findings

def check_include_block(af: AnalyzedFile, config: Config) -> List[Finding]:
    """Includes at the head of the file, one blank line before the following code."""
    findings: List[Finding] = []
    includes = list(iter_includes(af))

    if not includes:
        return findings

    code_lines = [token.line for token in af.code]
    first_code_line = min(code_lines) if code_lines else None

    for token, _header in includes:
        if (first_code_line is not None) and (token.line > first_code_line):
            findings.append(Finding(af.path, token.line, token.col, "include-block", "#include after the first code line (%d); keep every #include at" " the head of the file" % first_code_line))

    head_includes = [token for token, _header in includes if (first_code_line is None) or (token.line < first_code_line)]

    if not head_includes or (first_code_line is None):
        return findings

    last_include_line = max(token.end_line for token in head_includes)
    following_lines = [line for line in code_lines if line > last_include_line]

    if not following_lines:
        return findings

    blank_lines = af.blank_lines_between(last_include_line, min(following_lines))

    if blank_lines != 1:
        anchor = max(head_includes, key = lambda token: token.end_line)
        findings.append(Finding(af.path, anchor.line, anchor.col, "include-block", "expected exactly one blank line between the last #include and the" " following code (found %d)" % blank_lines))

    return findings

def check_platform_header_allowlist(af: AnalyzedFile, config: Config) -> List[Finding]:
    """Platform headers may only be included from allowlisted source paths."""

    if config.platform_paths and matches_globs(path_variants(af.path, config), config.platform_paths):
        return []

    restricted = {name.strip().lower() for name in config.platform_headers}
    findings: List[Finding] = []

    for token, header in iter_includes(af):
        if header is None:
            continue

        normalised = header.strip().replace("\\", "/")

        if normalised.lower() not in restricted:
            continue

        findings.append(Finding(af.path, token.line, token.col, "platform-header-allowlist", "platform header %r may only be included from allowlisted platform" " sources; configure platform_paths" % normalised))

    return findings

def check_platform_type_leak(af: AnalyzedFile, config: Config) -> List[Finding]:
    """Headers must not expose platform types or COM interfaces."""

    if af.path.suffix.lower() not in _HEADER_SUFFIXES:
        return []

    platform_types = set(config.platform_types)
    prefixes = config.com_interface_prefixes
    findings: List[Finding] = []

    for token in af.code:
        if token.kind != IDENT:
            continue

        if token.text in platform_types:
            findings.append(Finding(af.path, token.line, token.col, "platform-type-leak", "%r is a platform type and must not be exposed by a header; keep" " it behind the platform implementation" % token.text))
            continue

        if prefixes and token.text.startswith(prefixes):
            findings.append(Finding(af.path, token.line, token.col, "platform-type-leak", "%r looks like a COM interface and must not be exposed by a" " header; keep it behind the platform implementation" % token.text))

    return findings

RULE_FUNCTIONS = {"no-space-before-paren": check_no_space_before_paren, "for-semicolon-spacing": check_for_semicolon_spacing, "control-braces": check_control_braces, "one-statement-per-line": check_one_statement_per_line, "line-comments-only": check_line_comments_only, "include-block": check_include_block, "platform-header-allowlist": check_platform_header_allowlist, "platform-type-leak": check_platform_type_leak}

def run_checks(af: AnalyzedFile, config: Config, selected: Sequence[str]) -> List[Finding]:
    """Run the selected rules over one analyzed file in catalogue order."""
    findings: List[Finding] = []

    for rule in RULES:
        if rule.id not in selected:
            continue

        findings.extend(RULE_FUNCTIONS[rule.id](af, config))

    return findings
