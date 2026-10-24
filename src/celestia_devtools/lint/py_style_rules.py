"""Rule catalog, source model and checks for the custom Python style checker.

The checker implements the org Python style conventions (see the py-style
command in celestia_devtools.lint.py_style) with nothing but the standard
library. This module owns the rule catalog, the per-file source model built from
tokenize plus ast, the individual checks and the conservative fixers. The CLI,
configuration loading and report formatting live in
celestia_devtools.lint.py_style and celestia_devtools.lint.py_style_config.

Two coordinate systems meet here and must never be mixed:

* tokenize positions are (row, character column) pairs.
* ast positions use UTF-8 byte columns.

FileContext.ast_start and FileContext.ast_end convert AST positions to character
columns; every check works with those.

The module is deliberately split out of py_style.py once that file would have
grown past the 1,800 line mark. The public entry point stays
celestia_devtools.lint.py_style.main.
"""

from __future__ import annotations
import ast
import bisect
import fnmatch
import io
import keyword
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, runtime import is unused
    from celestia_devtools.lint.py_style_config import StyleConfig

# ---------------------------------------------------------------------------
# Rule catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """Machine-readable metadata for one check rule."""

    id: str
    title: str
    p_refs: tuple[str, ...]
    severity: str
    fixable: bool
    description: str


@dataclass(frozen=True)
class ManualRule:
    """A convention that cannot be machine-checked and needs human review."""

    id: str
    title: str
    p_refs: tuple[str, ...]
    description: str


def _rule(
    rule_id: str,
    title: str,
    p_refs: tuple[str, ...],
    fixable: bool,
    description: str,
    severity: str = "error",
) -> Rule:
    return Rule(rule_id, title, p_refs, severity, fixable, description)


CHECK_RULES: tuple[Rule, ...] = (
    _rule(
        "trailing-whitespace",
        "No trailing whitespace",
        ("P10", "P09"),
        True,
        "Reports physical lines that end in spaces or tabs. Trailing whitespace "
        "inside a multi-line string literal is runtime data and is exempt (P09/P43). "
        "Fix strips the trailing run.",
    ),
    _rule(
        "final-blank-lines",
        "No blank lines at end of file",
        ("P11", "P44"),
        True,
        "Reports blank lines after the last non-blank line of the file. A file that "
        "ends with exactly one newline has no trailing blank line. Fix removes them.",
    ),
    _rule(
        "final-newline",
        "Exactly one final newline (configurable)",
        ("P45",),
        True,
        'The file must end with exactly one newline when final_newline = "one" '
        '(the default); with no newline at all when final_newline = "none". Empty '
        "files are not checked. Fix rewrites the file tail.",
    ),
    _rule(
        "indent-four-spaces",
        "Indentation is a multiple of four columns",
        ("P18",),
        True,
        "Block indentation must be a multiple of four columns; one tab counts as "
        "one column (no-tabs reports the tab itself). Only the first physical row "
        "of a logical line is checked, so bracket continuation lines may align "
        "anywhere; leading whitespace inside a multi-line string literal is data "
        "and is exempt. Fix rounds offending block indents down to the nearest "
        "multiple of four.",
    ),
    _rule(
        "no-tabs",
        "No tab characters in code",
        ("P18",),
        True,
        "Reports tab characters outside string literals (code and comments). Tabs "
        "inside strings are data and are exempt. Fix expands leading tabs to four "
        "spaces and other tabs to one space.",
    ),
    _rule(
        "no-consecutive-blank-lines",
        "At most one blank line in a row",
        ("P19",),
        True,
        "Two or more consecutive blank lines are not allowed. Blank-looking lines "
        "inside a multi-line string literal are string content and are exempt. Fix "
        "collapses each run to a single blank line.",
    ),
    _rule(
        "one-statement-per-line",
        "One statement per line",
        ("P04", "P24"),
        False,
        "Reports semicolon-separated statements and block headers whose body starts "
        "on the header line (if ready: start()). Fix is not offered because "
        "splitting statements is not a layout-only rewrite.",
    ),
    _rule(
        "single-line-statement",
        "One statement per physical line",
        ("P05", "P39"),
        True,
        "A logical line must not span multiple physical lines. A line is exempt when "
        "the only reason it spans physical lines is a multi-line string literal. "
        "Multi-line decorator expressions are reported unless "
        "allow_multiline_decorators = true. Fix joins the physical lines when the "
        "statement has no comment and no multi-line string; otherwise it is skipped. Multi-line f-string literals count as multi-line strings, so they are exempt.",
    ),
    _rule(
        "imports-at-top",
        "Imports are grouped at the top of the module",
        ("P12",),
        False,
        "Module-level imports must all precede the first module-level statement other "
        "than the module docstring. Comments and blank lines between imports are "
        "allowed. Fix is not offered because moving imports can change runtime "
        "behaviour.",
    ),
    _rule(
        "imports-contiguous",
        "No blank lines between imports",
        ("P13",),
        True,
        "Two consecutive module-level import statements must not have blank lines "
        "between them. Fix removes the blank lines.",
    ),
    _rule(
        "no-block-imports",
        "No imports inside blocks",
        ("P14",),
        False,
        "Imports are only allowed at module level. if TYPE_CHECKING blocks "
        "(allow_type_checking_imports, default true) and try/except ImportError "
        "optional-dependency blocks (allow_optional_imports, default true) are "
        "exempt.",
    ),
    _rule(
        "no-dynamic-imports",
        "No dynamic imports",
        ("P15", "P16"),
        False,
        "No __import__(...) call and no importlib.import_module(...) call, including "
        "aliases imported from importlib tracked one level deep "
        "(import importlib as il; from importlib import import_module as load). "
        'getattr(importlib, "import_module") is not flagged because static analysis '
        "cannot prove what the resulting call does.",
    ),
    _rule(
        "acyclic-imports",
        "Module dependency graph is acyclic",
        ("P17",),
        False,
        "The module dependency graph built from module-level Import/ImportFrom "
        "statements of the scanned tree must be acyclic; each import cycle is "
        "reported once. Absolute imports are matched against scanned module names, "
        "relative imports are resolved against the package. Package __init__ "
        "modules are excluded (re-export imports are an accepted exception), as "
        "are imports nested in try/except or if blocks (not module-level edges). "
        "Enabled only when at least two modules are found while scanning "
        "directories.",
    ),
    _rule(
        "binary-operator-spacing",
        "Binary operators are spaced on both sides",
        ("P06", "P25", "P26", "P27"),
        True,
        "Arithmetic (+ - * / // % @ **), comparison (== != < <= > >=), boolean "
        "(and or), identity/membership (is, is not, in, not in), bitwise/shift "
        "(& | ^ << >>) and the walrus operator (:=) need exactly one space on each "
        "side. Exempt: unary +/-/~, unpacking */** markers (f(*args), a, *b), the "
        "decorator @, keyword/default = (see equal-spacing) and the annotation arrow "
        "->." + " Nothing inside an f-string token span is reported (the span is "
        "opaque on 3.11 and 3.12+).",
    ),
    _rule(
        "equal-spacing",
        "Keyword and default '=' are spaced",
        ("P28", "P29"),
        True,
        "The '=' of a function or lambda default and of a keyword argument needs "
        "exactly one space on each side: def f(a = 1), f(a = 1). PEP8 and ruff E251 "
        "require the opposite, which is why this checker owns the rule. Nothing inside "
        "an f-string token span is reported (the span is opaque on 3.11 and 3.12+).",
    ),
    _rule(
        "colon-spacing",
        "Colon spacing in dicts, annotations and lambdas",
        ("P33", "P34", "P35", "P36"),
        True,
        "Colons in dict displays, annotations and lambda headers take no space before "
        "and exactly one space after when content follows. Slice colons are checked "
        'only when slice_colons = "spaced" (the default "pep8" leaves a[1:2] '
        "alone). Nothing inside an f-string token span is reported (the span is opaque "
        "on 3.11 and 3.12+), so f-string format specifiers are covered as well.",
    ),
    _rule(
        "no-trailing-comma",
        "No trailing comma in argument lists or containers",
        ("P37", "P38"),
        True,
        "No comma immediately before a closing bracket of a call, parameter list, "
        "container or parenthesized import list. A single-element tuple keeps its "
        "grammar-required comma: (a,), return (a,), a[1,]. Fix deletes the comma.",
    ),
    _rule(
        "redundant-parens",
        "No redundant outer parentheses",
        ("P08",),
        True,
        "An assignment right-hand side, annotated-assignment value, return value, "
        "call argument or keyword value must not be wrapped in a redundant outer "
        "pair of parentheses: x = (a). Only reported when removing the pair keeps "
        "ast.dump identical; tuples, generator expressions, yield, walrus, starred "
        "and multi-line expressions are exempt. Fix deletes the pair; nested pairs "
        "converge over repeated fix passes. Nothing inside an f-string token span is "
        "reported (the span is opaque on 3.11 and 3.12+).",
    ),
    _rule(
        "explicit-precedence",
        "Mixed operators use explicit parentheses",
        ("P07", "P30", "P32"),
        False,
        "A nested expression whose operator precedence differs from its parent needs "
        "explicit parentheses, e.g. (a + b) * c, target and (not ready). Exempt: "
        "same-precedence chains (a + b + c), chained comparisons (a < b < c), "
        "unary not operands and right-associative ** chains. Reported as a warning "
        "by default; use the config severity table to raise it to error. Nothing "
        "inside an f-string token span is reported (the span is opaque on 3.11 and "
        "3.12+).",
        severity="warning",
    ),
    _rule(
        "blank-lines-around-blocks",
        "One blank line around complete blocks",
        ("P20", "P21"),
        True,
        "Two adjacent statements in the same body need exactly one blank line when at "
        "least one of them is a compound statement (def/class/if/for/while/with/try/"
        "match). A block header and its first body statement are adjacent with no "
        "blank line. The module top level is included unless blank_line_scope = "
        '"nested". else/elif/except/finally branch gaps belong to branch-adjacency. '
        "Fix only deletes extra blank lines; missing blank lines are reported but not "
        "inserted.",
    ),
    _rule(
        "branch-adjacency",
        "Branches are adjacent to the previous branch",
        ("P22",),
        True,
        "No blank line before elif/else/except/except*/finally, and none between the "
        "branch and the previous branch body. A comment block directly above the "
        "branch is allowed. Fix removes the blank lines.",
    ),
    _rule(
        "decorator-adjacency",
        "Decorators are adjacent to the definition",
        ("P23",),
        True,
        "No blank line between decorators, nor between the last decorator and the "
        "def/class it decorates. Fix removes the blank lines.",
    ),
    _rule(
        "snake-case-definition",
        "Definitions and variables are lower_snake_case",
        ("P01",),
        False,
        "Class names, function names, parameters, assignment targets and attribute "
        "definitions must match [a-z_][a-z0-9_]*. Exempt: dunder names, '_', "
        "ALL_CAPS constants (variables/attributes), naming_allow patterns, framework "
        "callbacks (setUp, tearDown, ... plus framework_callbacks), "
        "NamedTuple/TypedDict classes, assignments whose value is a TypeVar/NewType/"
        "ParamSpec/TypeVarTuple/NamedTuple/TypedDict, and re-export aliases in "
        "__init__.py.",
    ),
    _rule(
        "snake-case-file",
        "File names are lower_snake_case",
        ("P01",),
        False,
        "Python file names (stem) must be lower_snake_case. Dunder file names such as "
        "__init__.py and __main__.py are exempt. Checked only when scanning "
        "directories.",
    ),
    _rule(
        "compound-word",
        "Compound words are not split by underscores",
        ("P03",),
        False,
        "Names must not split a configured compound word with an underscore "
        '(text_box when "textbox" is in compound_words). The lexicon is empty by '
        "default, so the rule is inactive until compound_words is configured. "
        "Dictionary keys and strings are not checked.",
    ),
    _rule(
        "docstring-comment",
        "No triple-quoted strings as comments",
        ("P41",),
        False,
        "Standalone string expression statements are allowed only as docstrings. With "
        'docstrings = "allow" (the default) real docstrings are permitted and other '
        'standalone strings are reported; with "ban" docstrings are reported too. '
        "Fix is not offered because deleting a string expression can change runtime "
        "data.",
    ),
    _rule(
        "comment-blank-line",
        "No blank line inserted after a comment",
        ("P42",),
        True,
        "A full-line comment must not be followed by a blank line. Exemptions in the "
        "default inline-only mode: decorative section headers, the file-header "
        "comment block before the first statement, and comments that end a block. "
        'Config comment_blank_line: "inline-only" (default), "all" (no '
        'exemptions), "off". Reported as a warning; fix deletes the blank lines.',
        severity="warning",
    ),
)

MANUAL_RULES: tuple[ManualRule, ...] = (
    ManualRule(
        "P02",
        "Names describe their purpose",
        ("P02",),
        "Names must describe their purpose (label_caption, textbox_text, "
        "button_dialog). There is no machine criterion; review it manually. The "
        "checker only enforces the lower_snake_case shape (snake-case-definition) and "
        "compound-word spelling (compound-word).",
    ),
    ManualRule(
        "P09/P43",
        "Strings are runtime data and are never rewritten",
        ("P09", "P43"),
        "Never change the spaces, punctuation or line breaks inside a string to fit "
        "the layout rules. This is mechanical in the fixer: every rewrite must keep "
        "the STRING/FSTRING token multiset unchanged (and the whole-file ast.dump "
        "identical), otherwise the file is not written. Review any fix that touches "
        "string-adjacent code.",
    ),
    ManualRule(
        "P31",
        "Chained comparisons and short-circuit semantics are preserved",
        ("P31",),
        "Keep chained comparisons (a < b < c) and the short-circuit behaviour of "
        "and/or as written; splitting a chain into two comparisons changes "
        "evaluation. This cannot be checked statically; the fixer never rewrites "
        "comparison or boolean operators, and the AST-equality invariant plus tests "
        "guard the behaviour.",
    ),
    ManualRule(
        "P46",
        "No generic formatter over custom-layout code",
        ("P46",),
        "These conventions replace a generic formatter: do not run ruff format or "
        "Black over code that follows them, because those tools collapse keyword '=' "
        "spacing, split long lines, add trailing commas and require two blank lines. "
        "Keep ruff's stable semantic checks (F, E4, E7, W1, ...); the adoption "
        "policy removes the ruff-format gate for adopting repositories.",
    ),
)

BUILTIN_RULES: tuple[Rule, ...] = (
    Rule(
        "parse-error",
        "Source could not be tokenized or parsed",
        (),
        "error",
        False,
        "The file could not be tokenized or parsed as Python 3.11 source; other "
        "rules cannot run for it. Reported by the engine, not by a check function.",
    ),
)

CHECK_RULE_IDS: frozenset[str] = frozenset(rule.id for rule in CHECK_RULES)
BUILTIN_RULE_IDS: frozenset[str] = frozenset(rule.id for rule in BUILTIN_RULES)
KNOWN_RULE_IDS: frozenset[str] = CHECK_RULE_IDS | BUILTIN_RULE_IDS

_DEFAULT_SEVERITY: dict[str, str] = {rule.id: rule.severity for rule in CHECK_RULES}
_FIXABLE: dict[str, bool] = {rule.id: rule.fixable for rule in CHECK_RULES}


def rule_severity(rule_id: str, overrides: dict[str, str] | None = None) -> str:
    """Return the configured severity for *rule_id*."""

    if overrides and rule_id in overrides:
        return overrides[rule_id]
    return _DEFAULT_SEVERITY.get(rule_id, "error")


def rule_fixable(rule_id: str) -> bool:
    """Return whether the catalog marks *rule_id* as auto-fixable."""

    return _FIXABLE.get(rule_id, False)


# ---------------------------------------------------------------------------
# Source model
# ---------------------------------------------------------------------------


class ParseFailure(Exception):
    """Raised when a file cannot be tokenized or parsed."""

    def __init__(self, message: str, line: int = 1, column: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = max(1, line)
        self.column = max(1, column)


@dataclass(frozen=True)
class Issue:
    """One rule violation inside one file."""

    rule: str
    line: int
    column: int
    message: str


@dataclass
class LogicalLine:
    """One logical line, i.e. the tokens consumed before a NEWLINE token."""

    token_ids: list[int]
    sig_ids: list[int] = field(default_factory=list)


_SKIP_TOKEN_TYPES = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
)

_FSTRING_TYPES = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if hasattr(tokenize, name)
)
_LITERAL_TYPES = frozenset({tokenize.STRING}) | _FSTRING_TYPES

_STRING_PREFIX_RE = re.compile(r"(?i)^([a-z]*)(['\"])")

_OPEN_BRACKETS = "([{"
_CLOSE_BRACKETS = ")]}"


def _is_fstring_token(tok: tokenize.TokenInfo) -> bool:
    """Return whether a Python 3.11 STRING token is an f-string literal.

    Python 3.11 tokenizes an f-string as one STRING token whose prefix contains
    "f"; Python 3.12+ splits it into FSTRING_START/MIDDLE/END tokens instead.
    """

    if tok.type != tokenize.STRING:
        return False
    head = tok.string[:4].lower()
    if "f" not in head:
        return False
    match = _STRING_PREFIX_RE.match(tok.string)
    if match is None:
        return False
    return "f" in match.group(1).lower()


def _tokenize_failure(exc: BaseException) -> tuple[str, int, int]:
    if isinstance(exc, tokenize.TokenError):
        message = str(exc.args[0]) if exc.args else "tokenization failed"
        position = exc.args[1] if len(exc.args) > 1 else (1, 0)
        return message, int(position[0]), int(position[1]) + 1
    if isinstance(exc, SyntaxError):
        return str(exc.msg or exc), exc.lineno or 1, exc.offset or 1
    return str(exc) or exc.__class__.__name__, 1, 1


@dataclass
class FileContext:
    """Token, AST and physical-line view of one source file.

    The derived structures (literal-position indexes, blank-line index, parent
    map) are computed once per file so that a rule pass never rescans the whole
    source; the checks run 20+ times per file.

    F-strings are opaque: Python 3.11 tokenizes one as a single STRING token,
    Python 3.12+ (PEP 701) as FSTRING_START/MIDDLE/END plus the expression
    tokens. fstring_spans records the whole span on both tokenizers so rules and
    fixers behave identically across 3.11/3.12/3.13.
    """

    path: str
    text: str
    lines: list[str]
    line_count: int
    tokens: list[tokenize.TokenInfo]
    tree: ast.Module
    sig_ids: list[int]
    sig: list[tokenize.TokenInfo]
    sig_starts: list[tuple[int, int]]
    bracket: dict[tuple[int, int], tuple[int, int]]
    logical: list[LogicalLine]
    literals: list[tuple[int, int, int, int]]
    comment_rows: dict[int, int]
    first_sig_row: int | None
    literal_starts: list[tuple[int, int]] = field(default_factory=list)
    literal_ends: list[tuple[int, int]] = field(default_factory=list)
    fstring_spans: tuple[tuple[int, int, int, int], ...] = ()
    fstring_row_intervals: dict[int, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    inside_literal_rows: frozenset[int] = frozenset()
    blank_rows: tuple[int, ...] = ()
    blank_row_set: frozenset[int] = frozenset()
    parent_map: dict[int, ast.AST] = field(default_factory=dict)
    node_list: tuple[ast.AST, ...] = ()
    line_bytes: dict[int, bytes] = field(default_factory=dict)
    definitions_cache: list[Definition] | None = None

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls, path: str, text: str) -> "FileContext":
        """Tokenize and parse *text*; raise ParseFailure on failure."""

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            message, line, column = _tokenize_failure(exc)
            raise ParseFailure(message, line, column) from exc
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            raise ParseFailure(str(exc.msg or exc), exc.lineno or 1, exc.offset or 1) from exc
        except (ValueError, RecursionError) as exc:
            raise ParseFailure(str(exc) or exc.__class__.__name__, 1, 1) from exc

        lines = text.split("\n")
        line_count = len(lines) - (1 if text.endswith("\n") else 0)
        if text == "":
            lines = [""]
            line_count = 0

        sig_ids = [index for index, tok in enumerate(tokens) if tok.type not in _SKIP_TOKEN_TYPES]
        sig = [tokens[index] for index in sig_ids]
        sig_starts = [tok.start for tok in sig]

        bracket: dict[tuple[int, int], tuple[int, int]] = {}
        stack: list[tuple[int, int]] = []
        for tok in sig:
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPEN_BRACKETS:
                stack.append(tok.start)
            elif tok.string in _CLOSE_BRACKETS:
                if not stack:
                    continue
                opener = stack.pop()
                bracket[opener] = tok.start
                bracket[tok.start] = opener

        logical: list[LogicalLine] = []
        current: list[int] = []
        for index, tok in enumerate(tokens):
            current.append(index)
            if tok.type == tokenize.NEWLINE:
                sig_in = [i for i in current if tokens[i].type not in _SKIP_TOKEN_TYPES]
                logical.append(LogicalLine(current, sig_in))
                current = []
        if current:
            sig_in = [i for i in current if tokens[i].type not in _SKIP_TOKEN_TYPES]
            if sig_in:
                logical.append(LogicalLine(current, sig_in))

        literals = [
            (tok.start[0], tok.start[1], tok.end[0], tok.end[1])
            for tok in tokens
            if tok.type in _LITERAL_TYPES
        ]
        literal_starts = [
            (start_row, start_column) for start_row, start_column, _e_row, _e_column in literals
        ]
        literal_ends = [
            (end_row, end_column) for _s_row, _s_column, end_row, end_column in literals
        ]
        inside_rows: set[int] = set()
        for start_row, _start_column, end_row, _end_column in literals:
            if end_row > start_row:
                inside_rows.update(range(start_row + 1, end_row + 1))

        fstring_spans: list[tuple[int, int, int, int]] = []
        open_fstrings: list[tuple[int, int]] = []
        for tok in tokens:
            if _FSTRING_TYPES:
                if tok.type == tokenize.FSTRING_START:
                    open_fstrings.append(tok.start)
                    continue
                if tok.type == tokenize.FSTRING_END and open_fstrings:
                    start_row, start_column = open_fstrings.pop()
                    fstring_spans.append((start_row, start_column, tok.end[0], tok.end[1]))
                    continue
            if _is_fstring_token(tok):
                fstring_spans.append((tok.start[0], tok.start[1], tok.end[0], tok.end[1]))
        fstring_spans.sort()
        fstring_row_intervals: dict[int, list[tuple[int, int]]] = {}
        for start_row, start_column, end_row, end_column in fstring_spans:
            if start_row == end_row:
                fstring_row_intervals.setdefault(start_row, []).append((start_column, end_column))
                continue
            fstring_row_intervals.setdefault(start_row, []).append(
                (start_column, len(lines[start_row - 1]) + 1)
            )
            for row in range(start_row + 1, end_row):
                fstring_row_intervals.setdefault(row, []).append((0, len(lines[row - 1]) + 1))
            fstring_row_intervals.setdefault(end_row, []).append((0, end_column))
            inside_rows.update(range(start_row + 1, end_row + 1))

        comment_rows: dict[int, int] = {}
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            row = tok.start[0]
            if row not in comment_rows and row <= line_count:
                prefix = lines[row - 1][: tok.start[1]]
                if not prefix.strip():
                    comment_rows[row] = tok.start[1]

        blank_rows = tuple(
            row
            for row in range(1, line_count + 1)
            if row not in inside_rows and not lines[row - 1].strip()
        )

        node_list = tuple(ast.walk(tree))
        parent_map: dict[int, ast.AST] = {}
        for parent_node in node_list:
            for child in ast.iter_child_nodes(parent_node):
                parent_map[id(child)] = parent_node

        first_sig_row = sig[0].start[0] if sig else None

        return cls(
            path=path,
            text=text,
            lines=lines,
            line_count=line_count,
            tokens=tokens,
            tree=tree,
            sig_ids=sig_ids,
            sig=sig,
            sig_starts=sig_starts,
            bracket=bracket,
            logical=logical,
            literals=literals,
            comment_rows=comment_rows,
            first_sig_row=first_sig_row,
            literal_starts=literal_starts,
            literal_ends=literal_ends,
            fstring_spans=tuple(fstring_spans),
            fstring_row_intervals={
                row: tuple(intervals) for row, intervals in fstring_row_intervals.items()
            },
            inside_literal_rows=frozenset(inside_rows),
            blank_rows=blank_rows,
            blank_row_set=frozenset(blank_rows),
            parent_map=parent_map,
            node_list=node_list,
        )

    # -- physical lines ----------------------------------------------------
    def line(self, row: int) -> str:
        """Return the physical line *row* (1-based) without its newline."""

        if 1 <= row <= self.line_count:
            return self.lines[row - 1]
        return ""

    def indent_width(self, row: int) -> int:
        """Return the number of leading whitespace characters of *row*."""

        line = self.line(row)
        return len(line) - len(line.lstrip(" \t"))

    def is_full_comment(self, row: int) -> bool:
        """Return whether *row* starts with a full-line comment."""

        return row in self.comment_rows

    def is_blank_line(self, row: int) -> bool:
        """Return whether *row* is a blank line outside string literals."""

        return row in self.blank_row_set

    def blank_rows_between(self, first: int, last: int) -> list[int]:
        """Blank rows strictly between *first* and *last* (both 1-based rows)."""

        start = bisect.bisect_right(self.blank_rows, first)
        end = bisect.bisect_left(self.blank_rows, last)
        if start >= end:
            return []
        return list(self.blank_rows[start:end])

    def statement_start_rows(self) -> set[int]:
        """Return the physical rows where a logical line starts."""

        rows: set[int] = set()
        for logical in self.logical:
            if logical.sig_ids:
                rows.add(self.tokens[logical.sig_ids[0]].start[0])
        return rows

    # -- string literal protection ----------------------------------------
    def in_fstring_span(self, row: int, column: int) -> bool:
        """Return whether the position is strictly inside an f-string token span.

        Both the 3.11 one-STRING-token form and the 3.12+ FSTRING token form are
        recorded, so the same position is opaque on every supported version.
        """

        position = (row, column)
        for start_row, start_column, end_row, end_column in self.fstring_spans:
            if (start_row, start_column) < position < (end_row, end_column):
                return True
        return False

    def edit_touches_fstring(self, row: int, start: int, end: int) -> bool:
        """Return whether a line edit overlaps an f-string token span."""

        for start_column, end_column in self.fstring_row_intervals.get(row, ()):
            if start < end_column and end > start_column:
                return True
        return False

    def in_literal(self, row: int, column: int) -> bool:
        """Return whether the position is strictly inside a string literal.

        F-strings count as literal regions on every Python version (see
        in_fstring_span), so no rule or fixer reaches inside them.
        """

        position = (row, column)
        index = bisect.bisect_right(self.literal_starts, position) - 1
        if index >= 0 and self.literal_starts[index] < position < self.literal_ends[index]:
            return True
        return self.in_fstring_span(row, column)

    def line_starts_in_literal(self, row: int) -> bool:
        """Return whether *row* begins inside a multi-line string/f-string literal."""

        return row in self.inside_literal_rows

    def line_ends_in_literal(self, row: int) -> bool:
        """Return whether *row* ends inside a multi-line string literal."""

        return self.in_literal(row, len(self.line(row)))

    # -- AST positions -----------------------------------------------------
    def _bytes_of_line(self, row: int) -> bytes:
        cached = self.line_bytes.get(row)
        if cached is None:
            cached = self.line(row).encode("utf-8")
            self.line_bytes[row] = cached
        return cached

    def char_col(self, row: int, byte_column: int) -> int:
        """Convert an AST UTF-8 byte column to a character column on *row*."""

        if not 1 <= row <= self.line_count:
            return byte_column
        encoded = self._bytes_of_line(row)
        return len(encoded[:byte_column].decode("utf-8", "replace"))

    def ast_start(self, node: ast.AST) -> tuple[int, int]:
        """Return the character position where *node* starts."""

        row = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0)
        return row, self.char_col(row, column)

    def ast_end(self, node: ast.AST) -> tuple[int, int]:
        """Return the character position where *node* ends (exclusive)."""

        row = getattr(node, "end_lineno", None)
        column = getattr(node, "end_col_offset", None)
        if row is None or column is None:
            return self.ast_start(node)
        return row, self.char_col(row, column)

    # -- AST traversal -----------------------------------------------------
    def nodes(self) -> Iterator[ast.AST]:
        """Iterate every AST node (pre-order, cached per file)."""

        return iter(self.node_list)

    def parent(self, node: ast.AST) -> ast.AST | None:
        """Return the parent of *node* or None for the module root."""

        return self.parent_map.get(id(node))

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        """Yield the ancestors of *node*, innermost first."""

        current = self.parent_map.get(id(node))
        while current is not None:
            yield current
            current = self.parent_map.get(id(current))

    def definitions(self) -> list["Definition"]:
        """Return the cached definition-name collection (see _definitions)."""

        if self.definitions_cache is None:
            self.definitions_cache = _definitions(self)
        return self.definitions_cache

    # -- token access ------------------------------------------------------
    def token_index_before(self, position: tuple[int, int]) -> int | None:
        """Index into sig of the last token starting before *position*."""

        index = bisect.bisect_left(self.sig_starts, position) - 1
        return index if index >= 0 else None

    def token_index_at_or_after(self, position: tuple[int, int]) -> int | None:
        """Index into sig of the first token starting at or after *position*."""

        index = bisect.bisect_left(self.sig_starts, position)
        return index if index < len(self.sig_starts) else None

    def token_indices_in_range(self, start: tuple[int, int], end: tuple[int, int]) -> list[int]:
        """Return indices of significant tokens whose start is in [start, end)."""

        first = bisect.bisect_left(self.sig_starts, start)
        last = bisect.bisect_left(self.sig_starts, end)
        return list(range(first, last))

    def prev_token(self, index: int) -> tokenize.TokenInfo | None:
        """Return the significant token before *index* or None."""

        if index <= 0:
            return None
        return self.sig[index - 1]

    def next_token(self, index: int) -> tokenize.TokenInfo | None:
        """Return the significant token after *index* or None."""

        if index + 1 >= len(self.sig):
            return None
        return self.sig[index + 1]

    def header_colon_row(self, node: ast.AST) -> int | None:
        """Return the row of the block-header colon of compound statement *node*."""

        start_row = getattr(node, "lineno", 1)
        start_column = self.char_col(start_row, getattr(node, "col_offset", 0))
        index = self.token_index_at_or_after((start_row, start_column))
        if index is None:
            return None
        depth = 0
        while index < len(self.sig):
            tok = self.sig[index]
            if tok.type == tokenize.OP:
                if tok.string in _OPEN_BRACKETS:
                    depth += 1
                elif tok.string in _CLOSE_BRACKETS:
                    depth -= 1
                elif tok.string == ":" and depth == 0:
                    return tok.start[0]
            index += 1
        return None


# ---------------------------------------------------------------------------
# Generic AST helpers
# ---------------------------------------------------------------------------


def walk_with_parents(tree: ast.AST) -> Iterator[tuple[ast.AST, tuple[ast.AST, ...]]]:
    """Yield every node with the tuple of its ancestors (outermost first)."""

    stack: list[tuple[ast.AST, tuple[ast.AST, ...]]] = [(tree, ())]
    while stack:
        node, parents = stack.pop()
        yield node, parents
        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            stack.append((child, parents + (node,)))


def is_docstring_statement(statement: ast.stmt) -> bool:
    """Return whether *statement* is a string-expression docstring candidate."""

    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def module_docstring(tree: ast.Module) -> ast.Expr | None:
    """Return the module docstring expression, if any."""

    if tree.body and is_docstring_statement(tree.body[0]):
        return tree.body[0]  # type: ignore[return-value]
    return None


_COMPOUND_TYPES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)
if hasattr(ast, "TryStar"):  # Python 3.11+
    _COMPOUND_TYPES = (*_COMPOUND_TYPES, ast.TryStar)
if hasattr(ast, "Match"):  # Python 3.10+
    _COMPOUND_TYPES = (*_COMPOUND_TYPES, ast.Match)


def is_compound_statement(node: ast.AST) -> bool:
    """Return whether *node* is a complete block statement."""

    return isinstance(node, _COMPOUND_TYPES)


_BINOP_PRECEDENCE: dict[type[ast.operator], int] = {
    ast.BitOr: 5,
    ast.BitXor: 6,
    ast.BitAnd: 7,
    ast.LShift: 8,
    ast.RShift: 8,
    ast.Add: 9,
    ast.Sub: 9,
    ast.Mult: 10,
    ast.Div: 10,
    ast.FloorDiv: 10,
    ast.Mod: 10,
    ast.MatMult: 10,
    ast.Pow: 12,
}


def operator_precedence(node: ast.AST) -> int | None:
    """Return the precedence rank of an operator-bearing expression node."""

    if isinstance(node, ast.BoolOp):
        return 2 if isinstance(node.op, ast.And) else 1
    if isinstance(node, ast.Compare):
        return 4
    if isinstance(node, ast.BinOp):
        return _BINOP_PRECEDENCE.get(type(node.op))
    return None


def is_parenthesized(context: FileContext, node: ast.AST) -> bool:
    """Return whether *node* is directly wrapped in one matching pair of parentheses."""

    start = context.ast_start(node)
    end = context.ast_end(node)
    previous_index = context.token_index_before(start)
    next_index = context.token_index_at_or_after(end)
    if previous_index is None or next_index is None:
        return False
    previous = context.sig[previous_index]
    following = context.sig[next_index]
    if not (
        previous.type == tokenize.OP
        and previous.string == "("
        and following.type == tokenize.OP
        and following.string == ")"
    ):
        return False
    return context.bracket.get(previous.start) == following.start


def source_line_prefix_is_blank(context: FileContext, row: int, column: int) -> bool:
    """Return whether the characters before *column* on *row* are whitespace."""

    return not context.line(row)[:column].strip()


# ---------------------------------------------------------------------------
# Small helpers shared by checks and fixers
# ---------------------------------------------------------------------------


def _issue(rule_id: str, row: int, column: int, message: str) -> Issue:
    return Issue(rule_id, max(1, row), max(1, column), message)


def _is_string_token(tok: tokenize.TokenInfo) -> bool:
    return tok.type in _LITERAL_TYPES


def _is_op(tok: tokenize.TokenInfo | None, value: str) -> bool:
    return tok is not None and tok.type == tokenize.OP and tok.string == value


def _is_name(tok: tokenize.TokenInfo | None, value: str) -> bool:
    return tok is not None and tok.type == tokenize.NAME and tok.string == value


def _string_token_texts(context: FileContext) -> list[str]:
    """Return the text of every string/f-string literal token, in source order."""

    return [tok.string for tok in context.tokens if tok.type in _LITERAL_TYPES]


def _comma_token_positions_of_single_element_tuples(context: FileContext) -> set[tuple[int, int]]:
    """Return the comma positions that are grammar-required by one-element tuples."""

    positions: set[tuple[int, int]] = set()
    for node in context.nodes():
        if not isinstance(node, ast.Tuple):
            continue
        if len(node.elts) != 1 or isinstance(node.elts[0], ast.Starred):
            continue
        start = context.ast_start(node)
        end = context.ast_end(node)
        depth = 0
        commas: list[tuple[int, tuple[int, int]]] = []
        for index in context.token_indices_in_range(start, end):
            tok = context.sig[index]
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPEN_BRACKETS:
                depth += 1
            elif tok.string in _CLOSE_BRACKETS:
                depth -= 1
            elif tok.string == ",":
                commas.append((depth, tok.start))
        if not commas:
            continue
        shallowest = min(depth for depth, _position in commas)
        candidates = [position for depth, position in commas if depth == shallowest]
        if len(candidates) == 1:
            positions.add(candidates[0])
    return positions


def _all_arguments(arguments: ast.arguments) -> list[ast.arg]:
    """Return positional, keyword-only, vararg and kwarg arguments in order."""

    collected = list(arguments.posonlyargs) + list(arguments.args) + list(arguments.kwonlyargs)
    if arguments.vararg is not None:
        collected.append(arguments.vararg)
    if arguments.kwarg is not None:
        collected.append(arguments.kwarg)
    return collected


def _camel_to_snake(name: str) -> str:
    """Best-effort suggestion for a lower_snake_case spelling of *name*."""

    step_one = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    step_two = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step_one)
    suggestion = step_two.lower()
    suggestion = re.sub(r"_+", "_", suggestion)
    if name.startswith("_"):
        suggestion = "_" + suggestion.lstrip("_")
    return suggestion or name


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_trailing_whitespace(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report trailing spaces/tabs outside multi-line string literals."""

    issues: list[Issue] = []
    for row in range(1, context.line_count + 1):
        line = context.line(row)
        stripped = line.rstrip(" \t")
        if stripped == line:
            continue
        if context.in_literal(row, len(stripped)):
            continue
        issues.append(
            _issue(
                "trailing-whitespace", row, len(stripped) + 1, "Line ends with trailing whitespace."
            )
        )
    return issues


def check_final_blank_lines(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report blank lines after the last non-blank line."""

    last_content: int | None = None
    for row in range(context.line_count, 0, -1):
        if not context.is_blank_line(row):
            last_content = row
            break
    if last_content is None or last_content >= context.line_count:
        return []
    blank_count = context.line_count - last_content
    return [
        _issue(
            "final-blank-lines",
            last_content + 1,
            1,
            f"File ends with {blank_count} blank line(s); remove them.",
        )
    ]


def check_final_newline(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report a missing or extra final newline."""

    if not context.text:
        return []
    ends_with_newline = context.text.endswith("\n")
    row = max(1, context.line_count)
    if config.final_newline == "none":
        if ends_with_newline:
            return [
                _issue(
                    "final-newline",
                    row,
                    1,
                    "File must not end with a newline (final_newline = none).",
                )
            ]
        return []
    if not ends_with_newline or context.text.endswith("\n\n"):
        return [_issue("final-newline", row, 1, "File must end with exactly one newline.")]
    return []


def check_indent_four_spaces(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report block indentation widths that are not a multiple of four.

    Only the first physical row of a logical line is checked: continuation
    lines inside brackets are aligned freely and are not block indentation.
    """

    issues: list[Issue] = []
    for row in sorted(context.statement_start_rows()):
        if context.line_starts_in_literal(row):
            continue
        if not context.line(row).strip():
            continue
        width = context.indent_width(row)
        if width % 4:
            issues.append(
                _issue(
                    "indent-four-spaces",
                    row,
                    width + 1,
                    f"Indentation is {width} columns; it must be a multiple of 4.",
                )
            )
    return issues


def check_no_tabs(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report tab characters outside string literals."""

    issues: list[Issue] = []
    for row in range(1, context.line_count + 1):
        line = context.line(row)
        for column, character in enumerate(line):
            if character != "\t":
                continue
            if context.in_literal(row, column):
                continue
            issues.append(
                _issue(
                    "no-tabs", row, column + 1, "Tab character in code; indent with spaces instead."
                )
            )
            break
    return issues


def check_no_consecutive_blank_lines(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report runs of two or more blank lines."""

    issues: list[Issue] = []
    row = 1
    while row <= context.line_count:
        if not context.is_blank_line(row):
            row += 1
            continue
        start = row
        while row <= context.line_count and context.is_blank_line(row):
            row += 1
        length = row - start
        if length >= 2:
            issues.append(
                _issue(
                    "no-consecutive-blank-lines",
                    start + 1,
                    1,
                    f"{length} consecutive blank lines; at most one is allowed.",
                )
            )
    return issues


_BLOCK_HEADER_KEYWORDS = frozenset(
    {
        "if",
        "elif",
        "else",
        "for",
        "while",
        "with",
        "try",
        "except",
        "finally",
        "def",
        "class",
        "match",
        "case",
    }
)


def check_one_statement_per_line(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report semicolon-joined statements and single-line block bodies."""

    issues: list[Issue] = []
    for tok in context.sig:
        if tok.type == tokenize.OP and tok.string == ";":
            issues.append(
                _issue(
                    "one-statement-per-line",
                    tok.start[0],
                    tok.start[1] + 1,
                    "Multiple statements on one line (semicolon).",
                )
            )

    for logical in context.logical:
        if len(logical.sig_ids) < 2:
            continue
        tokens = [context.tokens[index] for index in logical.sig_ids]
        first = tokens[0]
        header_keyword = first.string if first.type == tokenize.NAME else ""
        if header_keyword == "async":
            header_keyword = tokens[1].string if tokens[1].type == tokenize.NAME else ""
        if header_keyword not in _BLOCK_HEADER_KEYWORDS:
            continue
        depth = 0
        colon_position: int | None = None
        for position, tok in enumerate(tokens):
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPEN_BRACKETS:
                depth += 1
            elif tok.string in _CLOSE_BRACKETS:
                depth -= 1
            elif tok.string == ":" and depth == 0:
                colon_position = position
                break
        if colon_position is None:
            continue
        colon_tok = tokens[colon_position]
        for tok in tokens[colon_position + 1 :]:
            if tok.start[0] == colon_tok.start[0]:
                issues.append(
                    _issue(
                        "one-statement-per-line",
                        tok.start[0],
                        tok.start[1] + 1,
                        "Block body must start on its own line (single-line block).",
                    )
                )
                break
    return issues


def check_single_line_statement(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report logical lines that span several physical lines."""

    issues: list[Issue] = []
    for logical in context.logical:
        sig_ids = logical.sig_ids
        if len(sig_ids) < 2:
            continue
        tokens = [context.tokens[index] for index in sig_ids]
        first = tokens[0]
        start_row = min(tok.start[0] for tok in tokens)
        end_row = max(tok.end[0] for tok in tokens)
        if end_row <= start_row:
            continue
        is_decorator = first.type == tokenize.OP and first.string == "@" and len(sig_ids) >= 2
        decorator_name = ""
        if is_decorator:
            second = tokens[1]
            decorator_name = second.string if second.type == tokenize.NAME else "expression"
            if config.allow_multiline_decorators:
                continue
        only_multiline_literal = True
        for tok in tokens:
            if tok.start[0] == start_row:
                continue
            if context.in_fstring_span(tok.start[0], tok.start[1]):
                continue
            only_multiline_literal = False
            break
        if only_multiline_literal:
            continue
        line_span = end_row - start_row + 1
        if is_decorator:
            message = (
                f"Decorator {decorator_name} spans {line_span} physical lines; "
                "keep decorators on one line."
            )
        else:
            message = (
                f"Statement spans {line_span} physical lines; keep one statement per physical line."
            )
        issues.append(_issue("single-line-statement", first.start[0], first.start[1] + 1, message))
    return issues


def check_imports_at_top(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report module-level imports that follow a non-import statement."""

    issues: list[Issue] = []
    body = list(context.tree.body)
    if body and is_docstring_statement(body[0]):
        body = body[1:]
    seen_code = False
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if seen_code:
                issues.append(
                    _issue(
                        "imports-at-top",
                        statement.lineno,
                        context.char_col(statement.lineno, statement.col_offset) + 1,
                        "Imports must be grouped at the top of the module.",
                    )
                )
        else:
            seen_code = True
    return issues


def check_imports_contiguous(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report blank lines between consecutive module-level imports."""

    issues: list[Issue] = []
    body = context.tree.body
    for previous, current in zip(body, body[1:], strict=False):
        if not isinstance(previous, (ast.Import, ast.ImportFrom)):
            continue
        if not isinstance(current, (ast.Import, ast.ImportFrom)):
            continue
        if current.lineno <= previous.end_lineno:
            continue
        blanks = context.blank_rows_between(previous.end_lineno, current.lineno)
        if blanks:
            issues.append(
                _issue(
                    "imports-contiguous",
                    current.lineno,
                    context.char_col(current.lineno, current.col_offset) + 1,
                    "Imports must not be separated by blank lines.",
                )
            )
    return issues


def _is_type_checking_test(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


def _is_import_error_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for candidate in candidates:
        if isinstance(candidate, ast.Name):
            if candidate.id in {"ImportError", "ModuleNotFoundError"}:
                return True
        elif isinstance(candidate, ast.Attribute):
            if candidate.attr in {"ImportError", "ModuleNotFoundError"}:
                return True
    return False


def check_no_block_imports(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report imports that are not at module level."""

    issues: list[Issue] = []
    for node in context.nodes():
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        parent = context.parent(node)
        if parent is None or isinstance(parent, ast.Module):
            continue
        exempt = False
        for ancestor in context.ancestors(node):
            if (
                config.allow_type_checking_imports
                and isinstance(ancestor, ast.If)
                and _is_type_checking_test(ancestor.test)
            ):
                exempt = True
                break
            if (
                config.allow_optional_imports
                and isinstance(ancestor, ast.Try)
                and any(_is_import_error_handler(handler) for handler in ancestor.handlers)
            ):
                exempt = True
                break
        if exempt:
            continue
        issues.append(
            _issue(
                "no-block-imports",
                node.lineno,
                context.char_col(node.lineno, node.col_offset) + 1,
                "Imports are only allowed at module level.",
            )
        )
    return issues


def check_no_dynamic_imports(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report __import__ and importlib.import_module calls."""

    issues: list[Issue] = []
    importlib_aliases: set[str] = {"importlib"}
    import_module_aliases: set[str] = set()
    for node in context.nodes():
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or "import_module")

    for node in context.nodes():
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        dynamic = False
        if isinstance(function, ast.Name):
            if function.id == "__import__":
                dynamic = True
            elif function.id in import_module_aliases:
                dynamic = True
        elif isinstance(function, ast.Attribute) and function.attr == "import_module":
            base = function.value
            if isinstance(base, ast.Name) and base.id in importlib_aliases:
                dynamic = True
        if not dynamic:
            continue
        row, column = context.ast_start(node)
        issues.append(
            _issue(
                "no-dynamic-imports",
                row,
                column + 1,
                "Dynamic imports are not allowed; use a static import.",
            )
        )
    return issues


_BINARY_OPERATOR_STRINGS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "//",
        "%",
        "@",
        "**",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&",
        "|",
        "^",
        "<<",
        ">>",
        ":=",
    }
)
_BINARY_KEYWORDS = frozenset({"and", "or", "in", "is", "not"})
_UNARY_CAPABLE_OPERATORS = frozenset({"+", "-", "*", "**", "/", "@"})
_UNARY_PREVIOUS_OPERATORS = frozenset(
    {
        "(",
        "[",
        "{",
        ",",
        ":",
        ";",
        ".",
        "+",
        "-",
        "*",
        "**",
        "/",
        "//",
        "%",
        "@",
        "~",
        "=",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&",
        "|",
        "^",
        "<<",
        ">>",
        "->",
        ":=",
    }
)
_UNARY_PREVIOUS_KEYWORDS = frozenset(
    {
        "return",
        "yield",
        "if",
        "elif",
        "while",
        "assert",
        "lambda",
        "else",
        "not",
        "and",
        "or",
        "in",
        "is",
        "for",
        "del",
        "from",
        "import",
        "with",
        "as",
        "raise",
        "await",
        "global",
        "nonlocal",
        "case",
    }
)


def _is_unary_operator(context: FileContext, index: int) -> bool:
    tok = context.sig[index]
    previous = context.prev_token(index)
    if previous is None:
        return True
    if previous.end[0] != tok.start[0]:
        return True
    if previous.type == tokenize.OP and previous.string in _UNARY_PREVIOUS_OPERATORS:
        return True
    if previous.type == tokenize.NAME and previous.string in _UNARY_PREVIOUS_KEYWORDS:
        return True
    return False


def _keyword_operator_is_binary(context: FileContext, index: int) -> bool:
    tok = context.sig[index]
    if tok.string in {"and", "or", "in", "is"}:
        return True
    previous = context.prev_token(index)
    following = context.next_token(index)
    if previous is not None and previous.string == "is":
        return True
    return following is not None and following.string == "in"


def check_binary_operator_spacing(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report binary operators without exactly one space on each side."""

    issues: list[Issue] = []
    for index, tok in enumerate(context.sig):
        is_binary_token = False
        if tok.type == tokenize.OP and tok.string in _BINARY_OPERATOR_STRINGS:
            if tok.string in _UNARY_CAPABLE_OPERATORS and _is_unary_operator(context, index):
                continue
            is_binary_token = True
        elif tok.type == tokenize.NAME and tok.string in _BINARY_KEYWORDS:
            if not _keyword_operator_is_binary(context, index):
                continue
            is_binary_token = True
        if not is_binary_token:
            continue
        previous = context.prev_token(index)
        following = context.next_token(index)
        if previous is None or following is None:
            continue
        if previous.end[0] != tok.start[0] or following.start[0] != tok.start[0]:
            continue
        line = context.line(tok.start[0])
        left = line[previous.end[1] : tok.start[1]]
        right = line[tok.end[1] : following.start[1]]
        if left == " " and right == " ":
            continue
        issues.append(
            _issue(
                "binary-operator-spacing",
                tok.start[0],
                tok.start[1] + 1,
                f"Operator '{tok.string}' must be surrounded by exactly one space.",
            )
        )
    return issues


def _find_equal_index(
    context: FileContext, start: tuple[int, int], end: tuple[int, int]
) -> int | None:
    for index in context.token_indices_in_range(start, end):
        tok = context.sig[index]
        if tok.type == tokenize.OP and tok.string == "=":
            return index
    return None


def _default_equals_indices(context: FileContext) -> list[int]:
    """Return the token indices of '=' in defaults and keyword arguments."""

    found: list[int] = []

    def add_function_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        arguments = node.args
        positional = list(arguments.posonlyargs) + list(arguments.args)
        paired = positional[len(positional) - len(arguments.defaults) :]
        for argument, default in zip(paired, arguments.defaults, strict=True):
            index = _find_equal_index(
                context, context.ast_end(argument), context.ast_start(default)
            )
            if index is not None:
                found.append(index)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
            if default is None:
                continue
            index = _find_equal_index(
                context, context.ast_end(argument), context.ast_start(default)
            )
            if index is not None:
                found.append(index)

    for node in context.nodes():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            add_function_arguments(node)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                index = _find_equal_index(
                    context, context.ast_start(keyword), context.ast_start(keyword.value)
                )
                if index is not None:
                    found.append(index)
    return sorted(set(found))


def _spacing_problems(context: FileContext, index: int, *, check_right: bool = True) -> list[str]:
    """Return human-readable spacing problems around token *index*."""

    tok = context.sig[index]
    line = context.line(tok.start[0])
    problems: list[str] = []
    previous = context.prev_token(index)
    if previous is not None and previous.end[0] == tok.start[0]:
        if line[previous.end[1] : tok.start[1]] != " ":
            problems.append("no space before")
    following = context.next_token(index)
    if check_right and following is not None and following.start[0] == tok.start[0]:
        if line[tok.end[1] : following.start[1]] != " ":
            problems.append("no space after")
    return problems


def check_equal_spacing(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report '=' in defaults and keyword arguments without exactly one space."""

    issues: list[Issue] = []
    for index in _default_equals_indices(context):
        problems = _spacing_problems(context, index)
        if not problems:
            continue
        tok = context.sig[index]
        issues.append(
            _issue(
                "equal-spacing",
                tok.start[0],
                tok.start[1] + 1,
                "The '=' of a default or keyword argument must be surrounded by exactly one space.",
            )
        )
    return issues


def _find_colon_index(
    context: FileContext, start: tuple[int, int], end: tuple[int, int]
) -> int | None:
    for index in context.token_indices_in_range(start, end):
        tok = context.sig[index]
        if tok.type == tokenize.OP and tok.string == ":":
            return index
    return None


def _lambda_colon_index(context: FileContext, node: ast.Lambda) -> int | None:
    start = context.ast_start(node)
    end = context.ast_start(node.body)
    depth = 0
    candidate: int | None = None
    for index in context.token_indices_in_range(start, end):
        tok = context.sig[index]
        if tok.type != tokenize.OP:
            continue
        if tok.string in _OPEN_BRACKETS:
            depth += 1
        elif tok.string in _CLOSE_BRACKETS:
            depth -= 1
        elif tok.string == ":" and depth == 0:
            candidate = index
    return candidate


def _slice_colon_indices(context: FileContext) -> list[int]:
    """Return token indices of slice colons for the spaced slice mode."""

    found: list[int] = []
    stack: list[str] = []
    for index, tok in enumerate(context.sig):
        if tok.type != tokenize.OP:
            continue
        if tok.string in _OPEN_BRACKETS:
            stack.append(tok.string)
            continue
        if tok.string in _CLOSE_BRACKETS:
            if stack:
                stack.pop()
            continue
        if tok.string != ":" or not stack or stack[-1] != "[":
            continue
        previous = context.prev_token(index)
        following = context.next_token(index)
        if previous is not None and previous.type == tokenize.OP and previous.string == ":":
            continue
        if following is not None and following.type == tokenize.OP and following.string == ":":
            continue
        if following is not None and following.type in _FSTRING_TYPES:
            continue
        found.append(index)
    return found


def _colon_indices(context: FileContext, config: "StyleConfig") -> list[int]:
    """Return the token indices of colons whose spacing the rule governs."""

    found: set[int] = set()
    for node in context.nodes():
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    continue
                index = _find_colon_index(context, context.ast_end(key), context.ast_start(value))
                if index is not None:
                    found.add(index)
        elif isinstance(node, ast.DictComp):
            index = _find_colon_index(
                context, context.ast_end(node.key), context.ast_start(node.value)
            )
            if index is not None:
                found.add(index)
        elif isinstance(node, ast.AnnAssign):
            index = _find_colon_index(
                context, context.ast_end(node.target), context.ast_start(node.annotation)
            )
            if index is not None:
                found.add(index)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            index = _find_colon_index(
                context, context.ast_start(node), context.ast_start(node.annotation)
            )
            if index is not None:
                found.add(index)
        elif isinstance(node, ast.Lambda):
            index = _lambda_colon_index(context, node)
            if index is not None:
                found.add(index)
    if config.slice_colons == "spaced":
        found.update(_slice_colon_indices(context))
    return sorted(found)


def check_colon_spacing(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report dict/annotation/lambda colon spacing problems."""

    issues: list[Issue] = []
    for index in _colon_indices(context, config):
        tok = context.sig[index]
        line = context.line(tok.start[0])
        found: list[str] = []
        previous = context.prev_token(index)
        if previous is not None and previous.end[0] == tok.start[0]:
            gap = line[previous.end[1] : tok.start[1]]
            if gap:
                found.append(f"{len(gap)} space(s) before the colon (expected none)")
        following = context.next_token(index)
        if following is not None and following.start[0] == tok.start[0]:
            if not (following.type == tokenize.OP and following.string in _CLOSE_BRACKETS):
                gap = line[tok.end[1] : following.start[1]]
                if gap != " ":
                    found.append(f"{len(gap)} space(s) after the colon (expected exactly one)")
        if not found:
            continue
        issues.append(
            _issue(
                "colon-spacing",
                tok.start[0],
                tok.start[1] + 1,
                "Colon spacing: found " + " and ".join(found) + ".",
            )
        )
    return issues


def check_no_trailing_comma(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report commas directly before a closing bracket, minus tuple exemptions."""

    issues: list[Issue] = []
    position_to_index = {tok.start: index for index, tok in enumerate(context.sig)}
    tuple_commas = _comma_token_positions_of_single_element_tuples(context)
    for index, tok in enumerate(context.sig):
        if tok.type != tokenize.OP or tok.string not in _CLOSE_BRACKETS:
            continue
        previous_index = index - 1
        if previous_index < 0:
            continue
        previous = context.sig[previous_index]
        if previous.type != tokenize.OP or previous.string != ",":
            continue
        if previous.start in tuple_commas:
            continue
        opener = context.bracket.get(tok.start)
        if opener is None or opener not in position_to_index:
            continue
        issues.append(
            _issue(
                "no-trailing-comma",
                previous.start[0],
                previous.start[1] + 1,
                "Trailing comma before a closing bracket; remove it.",
            )
        )
    return issues


def _absolute_offset(context: FileContext, position: tuple[int, int]) -> int:
    row, column = position
    offset = 0
    for line_index in range(row - 1):
        offset += len(context.lines[line_index]) + 1
    return offset + column


def _removing_pair_keeps_ast(
    context: FileContext, open_position: tuple[int, int], close_position: tuple[int, int]
) -> bool:
    open_offset = _absolute_offset(context, open_position)
    close_offset = _absolute_offset(context, close_position)
    if close_offset <= open_offset:
        return False
    candidate = (
        context.text[:open_offset]
        + context.text[open_offset + 1 : close_offset]
        + context.text[close_offset + 1 :]
    )
    try:
        rewritten = ast.parse(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return ast.dump(rewritten, include_attributes=False) == ast.dump(
        context.tree, include_attributes=False
    )


def _call_parenthesis_pair(
    context: FileContext, call: ast.Call
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    end = context.ast_end(call)
    close_position = (end[0], end[1] - 1)
    if close_position[1] < 0:
        return None
    index = context.token_index_at_or_after(close_position)
    if index is None:
        return None
    tok = context.sig[index]
    if tok.start != close_position or tok.type != tokenize.OP or tok.string != ")":
        return None
    opener = context.bracket.get(close_position)
    if opener is None:
        return None
    return opener, close_position


_SKIPPED_REDUNDANT_TYPES: tuple[type[ast.AST], ...] = (
    ast.Tuple,
    ast.GeneratorExp,
    ast.Yield,
    ast.YieldFrom,
    ast.NamedExpr,
    ast.Starred,
)


def _redundant_paren_candidate(
    context: FileContext, node: ast.AST, call_pair: tuple[tuple[int, int], tuple[int, int]] | None
) -> tuple[int, int] | None:
    if isinstance(node, _SKIPPED_REDUNDANT_TYPES):
        return None
    start = context.ast_start(node)
    end = context.ast_end(node)
    if start[0] != end[0]:
        return None
    previous_index = context.token_index_before(start)
    next_index = context.token_index_at_or_after(end)
    if previous_index is None or next_index is None:
        return None
    previous = context.sig[previous_index]
    following = context.sig[next_index]
    if not (_is_op(previous, "(") and _is_op(following, ")")):
        return None
    pair = (previous.start, following.start)
    if call_pair is not None and pair == call_pair:
        return None
    if context.bracket.get(previous.start) != following.start:
        return None
    if previous.start[0] != following.start[0]:
        return None
    return pair


def _redundant_paren_issues(context: FileContext) -> list[tuple[int, int]]:
    """Return (open, close) positions of reportable redundant parenthesis pairs."""

    positions: list[tuple[int, int]] = []
    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for node in context.nodes():
        candidates: list[tuple[ast.AST, tuple[tuple[int, int], tuple[int, int]] | None]] = []
        if isinstance(node, ast.Call):
            pair = _call_parenthesis_pair(context, node)
            candidates.extend((argument, pair) for argument in node.args)
            candidates.extend(
                (keyword.value, pair) for keyword in node.keywords if keyword.value is not None
            )
        elif isinstance(node, ast.Assign):
            candidates.append((node.value, None))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            candidates.append((node.value, None))
        elif isinstance(node, ast.Return) and node.value is not None:
            candidates.append((node.value, None))
        for candidate_node, call_pair in candidates:
            pair = _redundant_paren_candidate(context, candidate_node, call_pair)
            if pair is None or pair in seen:
                continue
            if not _removing_pair_keeps_ast(context, pair[0], pair[1]):
                continue
            seen.add(pair)
            positions.append(pair)
    return positions


def check_redundant_parens(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report redundant outer parentheses around expressions."""

    issues: list[Issue] = []
    for open_position, _close_position in _redundant_paren_issues(context):
        issues.append(
            _issue(
                "redundant-parens",
                open_position[0],
                open_position[1] + 1,
                "Redundant outer parentheses around this expression.",
            )
        )
    return issues


def _operator_operands(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.Compare):
        return [node.left, *node.comparators]
    return []


def check_explicit_precedence(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report nested operators of different precedence without parentheses."""

    issues: list[Issue] = []
    for node in context.nodes():
        parent_precedence = operator_precedence(node)
        if parent_precedence is None:
            continue
        parent_start = context.ast_start(node)
        parent_end = context.ast_end(node)
        if parent_start[0] != parent_end[0]:
            continue
        for child in _operator_operands(node):
            child_precedence = operator_precedence(child)
            if child_precedence is None or child_precedence == parent_precedence:
                continue
            child_start = context.ast_start(child)
            child_end = context.ast_end(child)
            if child_start[0] != child_end[0]:
                continue
            if is_parenthesized(context, child):
                continue
            issues.append(
                _issue(
                    "explicit-precedence",
                    child_start[0],
                    child_start[1] + 1,
                    "Mixed operators need explicit parentheses.",
                )
            )
    return issues


def _blank_runs(rows: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for row in rows:
        if runs and row == runs[-1][-1] + 1:
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def _body_entries(context: FileContext) -> list[tuple[ast.AST, list[ast.stmt], bool]]:
    """Return (container, statements, header_followed_by_body) entries."""

    entries: list[tuple[ast.AST, list[ast.stmt], bool]] = [
        (context.tree, list(context.tree.body), False)
    ]
    for node in context.nodes():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            entries.append((node, list(node.body), True))
        elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            entries.append((node, list(node.body), True))
            if node.orelse:
                entries.append((node, list(node.orelse), False))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            entries.append((node, list(node.body), True))
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            entries.append((node, list(node.body), True))
            if node.orelse:
                entries.append((node, list(node.orelse), False))
            if node.finalbody:
                entries.append((node, list(node.finalbody), False))
        elif isinstance(node, ast.ExceptHandler):
            entries.append((node, list(node.body), False))
        elif isinstance(node, ast.match_case):
            entries.append((node, list(node.body), False))
    return entries


def check_blank_lines_around_blocks(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report block headers or complete blocks without exactly one blank line."""

    issues: list[Issue] = []
    for container, statements, header_check in _body_entries(context):
        if not statements:
            continue
        if isinstance(container, ast.Module) and config.blank_line_scope == "nested":
            continue
        if header_check:
            header_row = context.header_colon_row(container)
            first = statements[0]
            if header_row is not None and first.lineno > header_row:
                blanks = context.blank_rows_between(header_row, first.lineno)
                if blanks:
                    issues.append(
                        _issue(
                            "blank-lines-around-blocks",
                            first.lineno,
                            1,
                            "Block header must be followed immediately by its first "
                            "statement (no blank line).",
                        )
                    )
        for previous, current in zip(statements, statements[1:], strict=False):
            if not (is_compound_statement(previous) or is_compound_statement(current)):
                continue
            previous_end = previous.end_lineno
            current_start = current.lineno
            if current_start <= previous_end:
                continue
            blanks = context.blank_rows_between(previous_end, current_start)
            if not blanks:
                issues.append(
                    _issue(
                        "blank-lines-around-blocks",
                        current_start,
                        1,
                        "Complete block statements need exactly one blank line before and after.",
                    )
                )
                continue
            runs = _blank_runs(blanks)
            for run in runs:
                if len(run) > 1:
                    issues.append(
                        _issue(
                            "blank-lines-around-blocks",
                            run[1],
                            1,
                            "Complete block statements need exactly one blank line "
                            "before and after; found several.",
                        )
                    )
                    break
    return issues


_BRANCH_KEYWORDS = frozenset({"elif", "else", "except", "finally"})


def check_branch_adjacency(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report blank lines before elif/else/except/except*/finally."""

    issues: list[Issue] = []
    for row in range(2, context.line_count + 1):
        line = context.line(row)
        stripped = line.lstrip(" \t")
        if not stripped:
            continue
        word = re.match(r"[A-Za-z_]+", stripped)
        if word is None or word.group(0) not in _BRANCH_KEYWORDS:
            continue
        probe = row - 1
        while probe >= 1 and context.is_full_comment(probe):
            probe -= 1
        if probe < 1:
            continue
        if context.is_blank_line(probe):
            issues.append(
                _issue(
                    "branch-adjacency",
                    row,
                    context.indent_width(row) + 1,
                    f"Blank line before '{word.group(0)}'; branches must be adjacent to "
                    "the previous branch.",
                )
            )
    return issues


def check_decorator_adjacency(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report blank lines between decorators and the decorated definition."""

    issues: list[Issue] = []
    for node in context.nodes():
        decorators = getattr(node, "decorator_list", None)
        if not decorators:
            continue
        decorated_line = getattr(node, "lineno", 1)
        previous_end = decorators[0].end_lineno
        for decorator in decorators[1:]:
            blanks = context.blank_rows_between(previous_end, decorator.lineno)
            if blanks:
                issues.append(
                    _issue(
                        "decorator-adjacency",
                        decorator.lineno,
                        1,
                        "No blank line between decorators.",
                    )
                )
            previous_end = decorator.end_lineno
        blanks = context.blank_rows_between(previous_end, decorated_line)
        if blanks:
            issues.append(
                _issue(
                    "decorator-adjacency",
                    decorated_line,
                    1,
                    "No blank line between the decorator and the decorated definition.",
                )
            )
    return issues


_SNAKE_CASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ALL_CAPS_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FRAMEWORK_CALLBACKS = frozenset(
    {
        "setUp",
        "tearDown",
        "setUpClass",
        "tearDownClass",
        "do_GET",
        "do_POST",
        "do_PUT",
        "do_DELETE",
        "do_PATCH",
    }
)
_TYPING_CONSTRUCTORS = frozenset(
    {"TypeVar", "NewType", "ParamSpec", "TypeVarTuple", "NamedTuple", "TypedDict", "namedtuple"}
)
_NAMED_TUPLE_BASES = frozenset({"NamedTuple", "TypedDict"})


def _pattern_allowed(name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern == name or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _is_typing_constructor(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Name):
            return function.id in _TYPING_CONSTRUCTORS
        if isinstance(function, ast.Attribute):
            return function.attr in _TYPING_CONSTRUCTORS
    return False


def _class_has_exempt_base(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _NAMED_TUPLE_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _NAMED_TUPLE_BASES:
            return True
    return False


@dataclass(frozen=True)
class Definition:
    """One definition-like name collected for the naming rules."""

    name: str
    row: int
    column: int
    kind: str
    node: ast.AST
    value: ast.AST | None = None
    annotation: ast.AST | None = None
    module_level: bool = False


def _definitions(context: FileContext) -> list[Definition]:
    """Collect class, function, parameter and assignment names with context."""

    result: list[Definition] = []

    def add(
        name: str,
        node: ast.AST,
        kind: str,
        value: ast.AST | None = None,
        annotation: ast.AST | None = None,
        module_level: bool = False,
    ) -> None:
        row = getattr(node, "lineno", 1)
        column = context.char_col(row, getattr(node, "col_offset", 0))
        result.append(
            Definition(name, row, column + 1, kind, node, value, annotation, module_level)
        )

    def add_target(
        target: ast.AST, value: ast.AST | None, annotation: ast.AST | None, module_level: bool
    ) -> None:
        if isinstance(target, ast.Name):
            add(target.id, target, "variable", value, annotation, module_level)
        elif isinstance(target, ast.Attribute):
            add(target.attr, target, "attribute", value, annotation, module_level)
        elif isinstance(target, ast.Starred):
            add_target(target.value, value, annotation, module_level)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                add_target(element, value, annotation, module_level)

    for node in context.nodes():
        if isinstance(node, ast.ClassDef):
            add(node.name, node, "class")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, node, "function")
            for argument in _all_arguments(node.args):
                add(argument.arg, argument, "parameter")
        elif isinstance(node, ast.Lambda):
            for argument in _all_arguments(node.args):
                add(argument.arg, argument, "parameter")
        elif isinstance(node, ast.Assign):
            module_level = isinstance(context.parent(node), ast.Module)
            for target in node.targets:
                add_target(target, node.value, None, module_level)
        elif isinstance(node, ast.AnnAssign):
            module_level = isinstance(context.parent(node), ast.Module)
            add_target(node.target, node.value, node.annotation, module_level)
        elif isinstance(node, ast.AugAssign):
            for target in (node.target,):
                add_target(target, node.value, None, False)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add_target(node.target, None, None, False)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    add_target(item.optional_vars, None, None, False)
        elif isinstance(node, ast.NamedExpr):
            add_target(node.target, node.value, None, False)
        elif isinstance(node, ast.comprehension):
            add_target(node.target, None, None, False)
    return result


def _definition_is_exempt(
    context: FileContext, definition: Definition, config: "StyleConfig"
) -> bool:
    """Return whether the naming rules ignore this definition."""

    name = definition.name
    if name == "_":
        return True
    if name.startswith("__") and name.endswith("__"):
        return True
    if _pattern_allowed(name, config.naming_allow):
        return True
    if definition.kind == "function":
        callbacks = _FRAMEWORK_CALLBACKS | frozenset(config.framework_callbacks)
        if name in callbacks:
            return True
    if definition.kind == "class" and isinstance(definition.node, ast.ClassDef):
        if _class_has_exempt_base(definition.node):
            return True
    if definition.kind in {"variable", "attribute"}:
        if _ALL_CAPS_RE.match(name):
            return True
        if _is_typing_constructor(definition.value):
            return True
        if _is_typing_constructor(definition.annotation):
            return True
        if (
            definition.kind == "variable"
            and definition.annotation is None
            and definition.module_level
            and isinstance(definition.value, (ast.Name, ast.Attribute))
            and context.path.endswith("__init__.py")
        ):
            return True
    return False


def _naming_issues(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Return lower_snake_case violations for every collected definition."""

    issues: list[Issue] = []
    for definition in context.definitions():
        if _definition_is_exempt(context, definition, config):
            continue
        if _SNAKE_CASE_RE.match(definition.name):
            continue
        issues.append(_naming_issue(definition.name, definition.row, definition.column))
    issues.sort(key=lambda issue: (issue.line, issue.column))
    return issues


def _naming_issue(name: str, row: int, column: int) -> Issue:
    suggestion = _camel_to_snake(name)
    return _issue(
        "snake-case-definition",
        row,
        column,
        f"'{name}' is not lower_snake_case; use '{suggestion}'.",
    )


def check_snake_case_definition(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report definition names that are not lower_snake_case."""

    return _naming_issues(context, config)


def check_compound_word(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report names that split a configured compound word."""

    lexicon = {word.lower() for word in config.compound_words if word}
    if not lexicon:
        return []
    issues: list[Issue] = []
    for definition in _definitions(context):
        if _definition_is_exempt(context, definition, config):
            continue
        parts = definition.name.split("_")
        if len(parts) < 2:
            continue
        hit = False
        for index in range(len(parts) - 1):
            left, right = parts[index], parts[index + 1]
            if not left or not right:
                continue
            if (left + right).lower() in lexicon:
                hit = True
                parts[index] = left + right
                del parts[index + 1]
                break
        if not hit:
            continue
        suggestion = "_".join(parts)
        issues.append(
            _issue(
                "compound-word",
                definition.row,
                definition.column,
                f"'{definition.name}' splits a compound word with an underscore; use "
                f"'{suggestion}'.",
            )
        )
    return issues


def check_docstring_comment(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report standalone string expressions used as comments."""

    allow_docstrings = config.docstrings != "ban"
    issues: list[Issue] = []
    for node in context.nodes():
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        is_docstring = False
        parent = context.parent(node)
        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            is_docstring = bool(parent.body) and parent.body[0] is node
        if allow_docstrings and is_docstring:
            continue
        if allow_docstrings:
            message = "Standalone string literal used as a comment; use a '#' comment."
        else:
            message = "Docstrings are disabled by config; use a '#' comment."
        row, column = context.ast_start(node)
        issues.append(_issue("docstring-comment", row, column + 1, message))
    return issues


_DECORATIVE_COMMENT_RE = re.compile(r"^#\s*[=\-#*_~]{4,}")
_DECORATIVE_COMMENT_DASH_RE = re.compile("^#\\s*\u2500\u2500")


def _is_section_comment(line: str) -> bool:
    return bool(_DECORATIVE_COMMENT_RE.match(line) or _DECORATIVE_COMMENT_DASH_RE.match(line))


def check_comment_blank_line(context: FileContext, config: "StyleConfig") -> list[Issue]:
    """Report a full-line comment followed by a blank line."""

    mode = config.comment_blank_line
    if mode == "off":
        return []
    issues: list[Issue] = []
    for row in sorted(context.comment_rows):
        if row >= context.line_count:
            continue
        if not context.is_blank_line(row + 1):
            continue
        if mode == "inline-only":
            if _is_section_comment(context.line(row)):
                continue
            if context.first_sig_row is None or row < context.first_sig_row:
                continue
            probe = row + 1
            while probe <= context.line_count and (
                context.is_blank_line(probe) or context.is_full_comment(probe)
            ):
                probe += 1
            if probe > context.line_count:
                continue
            if context.indent_width(probe) < context.indent_width(row):
                continue
        issues.append(
            _issue(
                "comment-blank-line",
                row,
                1,
                "Comment line is followed by a blank line; attach the comment to the "
                "code it describes.",
            )
        )
    return issues


def check_snake_case_file(path: Path) -> list[Issue]:
    """Report a Python file whose stem is not lower_snake_case."""

    stem = path.stem
    if stem.startswith("__") and stem.endswith("__"):
        return []
    if _SNAKE_CASE_RE.match(stem):
        return []
    return [_issue("snake-case-file", 1, 1, f"File name '{path.name}' is not lower_snake_case.")]


# ---------------------------------------------------------------------------
# Fixers
#
# Every fixer takes the current FileContext (built from the current text) and
# returns the rewritten text. The engine rebuilds the context between fixers and
# validates the final result (parseable source, identical ast.dump, identical
# string token multiset), so a fixer only has to produce a layout change that it
# believes is local and safe.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineEdit:
    """Replace characters [start, end) of one physical line with text."""

    row: int
    start: int
    end: int
    text: str


def _apply_line_edits(context: FileContext, edits: Sequence[LineEdit]) -> str:
    """Apply non-overlapping line edits and return the rewritten file text."""

    grouped: dict[int, list[LineEdit]] = {}
    for edit in edits:
        if not 1 <= edit.row <= context.line_count:
            continue
        line_length = len(context.lines[edit.row - 1])
        if edit.start < 0 or edit.start > line_length:
            continue
        if context.edit_touches_fstring(edit.row, edit.start, edit.end):
            continue
        grouped.setdefault(edit.row, []).append(edit)

    rewritten = list(context.lines[: context.line_count])
    for row, row_edits in grouped.items():
        accepted: list[LineEdit] = []
        for edit in sorted(row_edits, key=lambda item: (item.start, item.end)):
            if accepted and edit.start < accepted[-1].end:
                continue
            accepted.append(edit)
        line = context.lines[row - 1]
        for edit in sorted(accepted, key=lambda item: item.start, reverse=True):
            start = edit.start
            end = min(edit.end, len(line))
            line = line[:start] + edit.text + line[end:]
        rewritten[row - 1] = line

    if context.text.endswith("\n"):
        rewritten.append("")
    return "\n".join(rewritten)


def _delete_rows(context: FileContext, rows: set[int]) -> str:
    """Return the file text with the given physical rows removed."""

    rewritten = [
        context.lines[row - 1] for row in range(1, context.line_count + 1) if row not in rows
    ]
    if context.text.endswith("\n"):
        rewritten.append("")
    return "\n".join(rewritten)


def fix_trailing_whitespace(context: FileContext, config: "StyleConfig") -> str:
    """Strip trailing spaces and tabs outside multi-line strings."""

    edits: list[LineEdit] = []
    for row in range(1, context.line_count + 1):
        line = context.line(row)
        stripped = line.rstrip(" \t")
        if stripped == line or context.in_literal(row, len(stripped)):
            continue
        edits.append(LineEdit(row, len(stripped), len(line), ""))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_no_tabs(context: FileContext, config: "StyleConfig") -> str:
    """Expand tabs outside string literals to spaces."""

    edits: list[LineEdit] = []
    for row in range(1, context.line_count + 1):
        line = context.line(row)
        indent = context.indent_width(row)
        for column, character in enumerate(line):
            if character != "\t":
                continue
            if context.in_literal(row, column):
                continue
            replacement = "    " if column < indent else " "
            edits.append(LineEdit(row, column, column + 1, replacement))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_indent_four_spaces(context: FileContext, config: "StyleConfig") -> str:
    """Round offending block indentation down to the nearest multiple of four."""

    edits: list[LineEdit] = []
    for row in sorted(context.statement_start_rows()):
        if context.line_starts_in_literal(row):
            continue
        if not context.line(row).strip():
            continue
        width = context.indent_width(row)
        if width % 4 == 0:
            continue
        edits.append(LineEdit(row, 0, width, " " * (width - width % 4)))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_no_consecutive_blank_lines(context: FileContext, config: "StyleConfig") -> str:
    """Collapse runs of blank lines to one blank line."""

    delete: set[int] = set()
    row = 1
    while row <= context.line_count:
        if not context.is_blank_line(row):
            row += 1
            continue
        start = row
        while row <= context.line_count and context.is_blank_line(row):
            row += 1
        for extra in range(start + 1, row):
            delete.add(extra)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


def _last_content_row(context: FileContext) -> int | None:
    for row in range(context.line_count, 0, -1):
        if not context.is_blank_line(row):
            return row
    return None


def fix_final_blank_lines(context: FileContext, config: "StyleConfig") -> str:
    """Remove blank lines after the last non-blank line."""

    if not context.text:
        return context.text
    last_content = _last_content_row(context)
    if last_content is None:
        return ""
    if last_content >= context.line_count:
        return context.text
    body = "\n".join(context.lines[:last_content])
    if context.text.endswith("\n"):
        return body + "\n"
    return body


def fix_final_newline(context: FileContext, config: "StyleConfig") -> str:
    """Rewrite the file tail to one newline (or none)."""

    if not context.text:
        return context.text
    stripped = context.text.rstrip("\n")
    if config.final_newline == "none":
        return stripped
    return stripped + "\n"


def fix_imports_contiguous(context: FileContext, config: "StyleConfig") -> str:
    """Remove blank lines between consecutive module-level imports."""

    delete: set[int] = set()
    body = context.tree.body
    for previous, current in zip(body, body[1:], strict=False):
        if not isinstance(previous, (ast.Import, ast.ImportFrom)):
            continue
        if not isinstance(current, (ast.Import, ast.ImportFrom)):
            continue
        if current.lineno <= previous.end_lineno:
            continue
        for row in context.blank_rows_between(previous.end_lineno, current.lineno):
            delete.add(row)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


def _branch_body_blank_rows(context: FileContext) -> set[int]:
    """Return blank rows that sit before elif/else/except/finally branches."""

    delete: set[int] = set()
    for row in range(2, context.line_count + 1):
        stripped = context.line(row).lstrip(" \t")
        if not stripped:
            continue
        word = re.match(r"[A-Za-z_]+", stripped)
        if word is None or word.group(0) not in _BRANCH_KEYWORDS:
            continue
        probe = row - 1
        while probe >= 1 and context.is_full_comment(probe):
            probe -= 1
        while probe >= 1 and context.is_blank_line(probe):
            delete.add(probe)
            probe -= 1
    return delete


def fix_branch_adjacency(context: FileContext, config: "StyleConfig") -> str:
    """Remove blank lines in front of elif/else/except/finally branches."""

    delete = _branch_body_blank_rows(context)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


def fix_decorator_adjacency(context: FileContext, config: "StyleConfig") -> str:
    """Remove blank lines between decorators and the decorated definition."""

    delete: set[int] = set()
    for node in context.nodes():
        decorators = getattr(node, "decorator_list", None)
        if not decorators:
            continue
        decorated_line = getattr(node, "lineno", 1)
        previous_end = decorators[0].end_lineno
        for decorator in decorators[1:]:
            for row in context.blank_rows_between(previous_end, decorator.lineno):
                delete.add(row)
            previous_end = decorator.end_lineno
        for row in context.blank_rows_between(previous_end, decorated_line):
            delete.add(row)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


def _blank_lines_around_block_rows(context: FileContext, config: "StyleConfig") -> set[int]:
    delete: set[int] = set()
    for container, statements, header_check in _body_entries(context):
        if not statements:
            continue
        if isinstance(container, ast.Module) and config.blank_line_scope == "nested":
            continue
        if header_check:
            header_row = context.header_colon_row(container)
            first = statements[0]
            if header_row is not None and first.lineno > header_row:
                for row in context.blank_rows_between(header_row, first.lineno):
                    delete.add(row)
        for previous, current in zip(statements, statements[1:], strict=False):
            if not (is_compound_statement(previous) or is_compound_statement(current)):
                continue
            previous_end = previous.end_lineno
            current_start = current.lineno
            if current_start <= previous_end:
                continue
            for run in _blank_runs(context.blank_rows_between(previous_end, current_start)):
                if len(run) > 1:
                    delete.update(run[1:])
    return delete


def fix_blank_lines_around_blocks(context: FileContext, config: "StyleConfig") -> str:
    """Remove extra blank lines around complete blocks."""

    delete = _blank_lines_around_block_rows(context, config)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


def _comment_blank_rows(context: FileContext, config: "StyleConfig") -> set[int]:
    """Return blank rows that follow a reportable full-line comment."""

    mode = config.comment_blank_line
    if mode == "off":
        return set()
    delete: set[int] = set()
    for row in sorted(context.comment_rows):
        if row >= context.line_count:
            continue
        if not context.is_blank_line(row + 1):
            continue
        if mode == "inline-only":
            if _is_section_comment(context.line(row)):
                continue
            if context.first_sig_row is None or row < context.first_sig_row:
                continue
            probe = row + 1
            while probe <= context.line_count and (
                context.is_blank_line(probe) or context.is_full_comment(probe)
            ):
                probe += 1
            if probe > context.line_count:
                continue
            if context.indent_width(probe) < context.indent_width(row):
                continue
        probe = row + 1
        while probe <= context.line_count and context.is_blank_line(probe):
            delete.add(probe)
            probe += 1
    return delete


def fix_comment_blank_line(context: FileContext, config: "StyleConfig") -> str:
    """Delete blank lines inserted after a comment."""

    delete = _comment_blank_rows(context, config)
    if not delete:
        return context.text
    return _delete_rows(context, delete)


# -- token level layout helpers --------------------------------------------
_SPACE_AROUND_OPERATORS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "//",
        "%",
        "@",
        "**",
        "==",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "&",
        "|",
        "^",
        "<<",
        ">>",
        ":=",
        "=",
        "->",
    }
)
_SPACE_AROUND_KEYWORDS = frozenset({"and", "or", "in", "is", "not"})
_UNARY_ONLY_OPERATORS = frozenset({"~"})
_ATTACHING_PREVIOUS_KEYWORDS = frozenset(
    {"def", "class", "C", "return", "yield", "assert", "if", "elif", "while", "lambda"}
)


def _is_literal_token(tok: tokenize.TokenInfo) -> bool:
    return tok.type in _LITERAL_TYPES


def _wordish(tok: tokenize.TokenInfo) -> bool:
    return tok.type in {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}


def _token_is_unary_operator(tokens: Sequence[tokenize.TokenInfo], index: int) -> bool:
    if index < 0 or index >= len(tokens):
        return False
    tok = tokens[index]
    if tok.type != tokenize.OP:
        return False
    if tok.string not in {"+", "-", "*", "**", "~", "/", "@"}:
        return False
    if index == 0:
        return True
    previous = tokens[index - 1]
    if previous.type == tokenize.OP and previous.string in _UNARY_PREVIOUS_OPERATORS:
        return True
    if previous.type == tokenize.NAME and previous.string in _UNARY_PREVIOUS_KEYWORDS:
        return True
    return False


def _bracket_attaches(tokens: Sequence[tokenize.TokenInfo], index: int) -> bool:
    """Return whether an opening bracket at *index* binds to the previous token."""

    if index == 0:
        return False
    previous = tokens[index - 1]
    if previous.type == tokenize.OP:
        return previous.string in _CLOSE_BRACKETS
    if previous.type == tokenize.NAME:
        return not _is_keyword_name(previous.string)
    return previous.type in {tokenize.STRING, tokenize.NUMBER} or previous.type in _FSTRING_TYPES


def _is_keyword_name(value: str) -> bool:
    return keyword.iskeyword(value)


def _needs_space(tokens: Sequence[tokenize.TokenInfo], index: int) -> bool:
    """Return whether a single space must separate tokens[index-1] and tokens[index]."""

    previous = tokens[index - 1]
    current = tokens[index]
    if previous.type in _FSTRING_TYPES or current.type in _FSTRING_TYPES:
        return False
    if current.type == tokenize.OP and current.string in ")]},;:":
        return False
    if previous.type == tokenize.OP and previous.string in "([{.":
        return False
    if previous.type == tokenize.OP and previous.string == "@" and index == 1:
        return False
    if current.type == tokenize.OP and current.string in "([{":
        return not _bracket_attaches(tokens, index)
    if previous.type == tokenize.OP and previous.string in _UNARY_ONLY_OPERATORS:
        return False
    if _token_is_unary_operator(tokens, index - 1):
        return False
    if previous.type == tokenize.OP and previous.string in {",", ":", ";"}:
        return True
    if previous.type == tokenize.OP and previous.string in _SPACE_AROUND_OPERATORS:
        return True
    if current.type == tokenize.OP and current.string in _SPACE_AROUND_OPERATORS:
        return True
    if previous.type == tokenize.NAME and previous.string in _SPACE_AROUND_KEYWORDS:
        return True
    if current.type == tokenize.NAME and current.string in _SPACE_AROUND_KEYWORDS:
        return True
    if current.type == tokenize.OP and current.string in _UNARY_ONLY_OPERATORS:
        return True
    if _wordish(previous) and _wordish(current):
        return True
    return False


def _merge_statement_tokens(tokens: Sequence[tokenize.TokenInfo]) -> str:
    parts = [tokens[0].string]
    for index in range(1, len(tokens)):
        if _needs_space(tokens, index):
            parts.append(" ")
        parts.append(tokens[index].string)
    return "".join(parts)


def _mergeable_logical_lines(
    context: FileContext, config: "StyleConfig"
) -> list[tuple[int, int, str]]:
    """Return (first_row, last_row, merged_text) for mergeable statements."""

    merges: list[tuple[int, int, str]] = []
    for logical in context.logical:
        sig_ids = logical.sig_ids
        if len(sig_ids) < 2:
            continue
        tokens = [context.tokens[index] for index in sig_ids]
        start_row = min(tok.start[0] for tok in tokens)
        end_row = max(tok.end[0] for tok in tokens)
        if end_row <= start_row:
            continue
        first = tokens[0]
        is_decorator = first.type == tokenize.OP and first.string == "@"
        if is_decorator and config.allow_multiline_decorators:
            continue
        if any(
            tok.type == tokenize.COMMENT for tok in [context.tokens[i] for i in logical.token_ids]
        ):
            continue
        if any(tok.type in _FSTRING_TYPES for tok in tokens):
            continue
        if any(tok.start[0] != tok.end[0] for tok in tokens):
            continue
        first_line = context.line(start_row)
        if first_line[: first.start[1]].strip():
            continue
        last_line = context.line(end_row)
        last = tokens[-1]
        if last_line[last.end[1] :].strip():
            continue
        merged = _merge_statement_tokens(tokens)
        prefix = first_line[: first.start[1]]
        merges.append((start_row, end_row, prefix + merged))
    return merges


def fix_single_line_statement(context: FileContext, config: "StyleConfig") -> str:
    """Join multi-line statements that have no comments or multi-line strings."""

    merges = _mergeable_logical_lines(context, config)
    if not merges:
        return context.text
    replacements = {start: (end, text) for start, end, text in merges}
    rewritten: list[str] = []
    row = 1
    while row <= context.line_count:
        replacement = replacements.get(row)
        if replacement is None:
            rewritten.append(context.lines[row - 1])
            row += 1
            continue
        end_row, merged = replacement
        rewritten.append(merged)
        row = end_row + 1
    if context.text.endswith("\n"):
        rewritten.append("")
    return "\n".join(rewritten)


def _trailing_comma_positions(context: FileContext) -> list[tuple[int, tokenize.TokenInfo]]:
    """Return comma tokens that violate the no-trailing-comma rule."""

    positions: list[tuple[int, tokenize.TokenInfo]] = []
    tuple_commas = _comma_token_positions_of_single_element_tuples(context)
    for index, tok in enumerate(context.sig):
        if tok.type != tokenize.OP or tok.string not in _CLOSE_BRACKETS:
            continue
        previous_index = index - 1
        if previous_index < 0:
            continue
        previous = context.sig[previous_index]
        if previous.type != tokenize.OP or previous.string != ",":
            continue
        if previous.start in tuple_commas:
            continue
        if context.bracket.get(tok.start) is None:
            continue
        positions.append((index, previous))
    return positions


def fix_no_trailing_comma(context: FileContext, config: "StyleConfig") -> str:
    """Delete trailing commas before closing brackets."""

    edits: list[LineEdit] = []
    for index, comma in _trailing_comma_positions(context):
        following = context.sig[index]
        if following.start[0] == comma.end[0]:
            edits.append(LineEdit(comma.start[0], comma.start[1], following.start[1], ""))
        else:
            edits.append(LineEdit(comma.start[0], comma.start[1], comma.end[1], ""))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def _operator_spacing_edits(context: FileContext) -> list[LineEdit]:
    edits: list[LineEdit] = []
    for index, tok in enumerate(context.sig):
        is_binary_token = False
        if tok.type == tokenize.OP and tok.string in _BINARY_OPERATOR_STRINGS:
            if tok.string in _UNARY_CAPABLE_OPERATORS and _is_unary_operator(context, index):
                continue
            is_binary_token = True
        elif tok.type == tokenize.NAME and tok.string in _BINARY_KEYWORDS:
            if not _keyword_operator_is_binary(context, index):
                continue
            is_binary_token = True
        if not is_binary_token:
            continue
        previous = context.prev_token(index)
        following = context.next_token(index)
        if previous is not None and previous.end[0] == tok.start[0]:
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != " ":
                edits.append(LineEdit(tok.start[0], previous.end[1], tok.start[1], " "))
        if following is not None and following.start[0] == tok.start[0]:
            if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                edits.append(LineEdit(tok.start[0], tok.end[1], following.start[1], " "))
    return edits


def fix_binary_operator_spacing(context: FileContext, config: "StyleConfig") -> str:
    """Normalise spaces around binary operators."""

    edits = _operator_spacing_edits(context)
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_equal_spacing(context: FileContext, config: "StyleConfig") -> str:
    """Normalise spaces around default and keyword '='."""

    edits: list[LineEdit] = []
    for index in _default_equals_indices(context):
        tok = context.sig[index]
        previous = context.prev_token(index)
        following = context.next_token(index)
        if previous is not None and previous.end[0] == tok.start[0]:
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != " ":
                edits.append(LineEdit(tok.start[0], previous.end[1], tok.start[1], " "))
        if following is not None and following.start[0] == tok.start[0]:
            if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                edits.append(LineEdit(tok.start[0], tok.end[1], following.start[1], " "))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_colon_spacing(context: FileContext, config: "StyleConfig") -> str:
    """Normalise spaces around governed colons."""

    edits: list[LineEdit] = []
    for index in _colon_indices(context, config):
        tok = context.sig[index]
        previous = context.prev_token(index)
        following = context.next_token(index)
        if previous is not None and previous.end[0] == tok.start[0]:
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != "":
                edits.append(LineEdit(tok.start[0], previous.end[1], tok.start[1], ""))
        if following is not None and following.start[0] == tok.start[0]:
            if not (following.type == tokenize.OP and following.string in _CLOSE_BRACKETS):
                if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                    edits.append(LineEdit(tok.start[0], tok.end[1], following.start[1], " "))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


def fix_redundant_parens(context: FileContext, config: "StyleConfig") -> str:
    """Delete one redundant outer parenthesis pair per expression."""

    edits: list[LineEdit] = []
    for open_position, close_position in _redundant_paren_issues(context):
        edits.append(LineEdit(open_position[0], open_position[1], open_position[1] + 1, ""))
        edits.append(LineEdit(close_position[0], close_position[1], close_position[1] + 1, ""))
    if not edits:
        return context.text
    return _apply_line_edits(context, edits)


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

CHECK_FUNCS: dict[str, Callable[[FileContext, "StyleConfig"], list[Issue]]] = {
    "trailing-whitespace": check_trailing_whitespace,
    "final-blank-lines": check_final_blank_lines,
    "final-newline": check_final_newline,
    "indent-four-spaces": check_indent_four_spaces,
    "no-tabs": check_no_tabs,
    "no-consecutive-blank-lines": check_no_consecutive_blank_lines,
    "one-statement-per-line": check_one_statement_per_line,
    "single-line-statement": check_single_line_statement,
    "imports-at-top": check_imports_at_top,
    "imports-contiguous": check_imports_contiguous,
    "no-block-imports": check_no_block_imports,
    "no-dynamic-imports": check_no_dynamic_imports,
    "binary-operator-spacing": check_binary_operator_spacing,
    "equal-spacing": check_equal_spacing,
    "colon-spacing": check_colon_spacing,
    "no-trailing-comma": check_no_trailing_comma,
    "redundant-parens": check_redundant_parens,
    "explicit-precedence": check_explicit_precedence,
    "blank-lines-around-blocks": check_blank_lines_around_blocks,
    "branch-adjacency": check_branch_adjacency,
    "decorator-adjacency": check_decorator_adjacency,
    "snake-case-definition": check_snake_case_definition,
    "compound-word": check_compound_word,
    "docstring-comment": check_docstring_comment,
    "comment-blank-line": check_comment_blank_line,
}

ENGINE_RULE_IDS: frozenset[str] = frozenset({"acyclic-imports", "snake-case-file"})

FIX_FUNCS: dict[str, Callable[[FileContext, "StyleConfig"], str]] = {
    "trailing-whitespace": fix_trailing_whitespace,
    "no-tabs": fix_no_tabs,
    "indent-four-spaces": fix_indent_four_spaces,
    "no-consecutive-blank-lines": fix_no_consecutive_blank_lines,
    "final-blank-lines": fix_final_blank_lines,
    "final-newline": fix_final_newline,
    "imports-contiguous": fix_imports_contiguous,
    "branch-adjacency": fix_branch_adjacency,
    "decorator-adjacency": fix_decorator_adjacency,
    "blank-lines-around-blocks": fix_blank_lines_around_blocks,
    "comment-blank-line": fix_comment_blank_line,
    "single-line-statement": fix_single_line_statement,
    "no-trailing-comma": fix_no_trailing_comma,
    "binary-operator-spacing": fix_binary_operator_spacing,
    "equal-spacing": fix_equal_spacing,
    "colon-spacing": fix_colon_spacing,
    "redundant-parens": fix_redundant_parens,
}

FIX_ORDER: tuple[str, ...] = (
    "trailing-whitespace",
    "no-tabs",
    "indent-four-spaces",
    "no-consecutive-blank-lines",
    "final-blank-lines",
    "final-newline",
    "imports-contiguous",
    "branch-adjacency",
    "decorator-adjacency",
    "blank-lines-around-blocks",
    "comment-blank-line",
    "single-line-statement",
    "no-trailing-comma",
    "binary-operator-spacing",
    "equal-spacing",
    "colon-spacing",
    "redundant-parens",
)


def string_token_texts(tree_text: str) -> list[str]:
    """Return the sorted multiset of literal token texts of *tree_text*.

    Used by the engine as the fixer invariant that string data is never
    rewritten (P09/P43). Tokenization errors yield an empty list.
    """

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(tree_text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    return sorted(tok.string for tok in tokens if tok.type in _LITERAL_TYPES)


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportEdge:
    """One module-level import statement with its resolved candidate targets."""

    targets: tuple[str, ...]
    path: Path
    line: int


@dataclass(frozen=True)
class ModuleInfo:
    """Import-graph node for one scanned module."""

    name: str
    path: Path
    is_package: bool
    edges: tuple[ImportEdge, ...]


@dataclass(frozen=True)
class GraphIssue:
    """One import-graph violation anchored at a file position."""

    path: Path
    line: int
    column: int
    message: str


def module_name_for_path(path: Path) -> tuple[str, str, bool]:
    """Return (dotted module name, package prefix, is_package) for *path*."""

    resolved = path.resolve()
    is_package = resolved.stem == "__init__"
    parts = [] if is_package else [resolved.stem]
    directory = resolved.parent
    while (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
    name = ".".join(parts)
    package = name if is_package else name.rpartition(".")[0]
    return name, package, is_package


def _absolute_target_candidates(dotted: str) -> tuple[str, ...]:
    if not dotted:
        return ()
    parts = dotted.split(".")
    candidates = [".".join(parts[:index]) for index in range(len(parts), 0, -1)]
    return tuple(candidates)


def _relative_target_candidates(
    module_name: str, is_package: bool, level: int, dotted: str | None
) -> tuple[str, ...]:
    base = module_name if is_package else module_name.rpartition(".")[0]
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    if dotted:
        joined = f"{base}.{dotted}" if base else dotted
        return _absolute_target_candidates(joined)
    return _absolute_target_candidates(base)


def module_import_edges(
    path: Path, tree: ast.Module, module_name: str, is_package: bool
) -> list[ImportEdge]:
    """Collect module-level import statements of *tree* as graph edges."""

    edges: list[ImportEdge] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                targets = _absolute_target_candidates(alias.name)
                if targets:
                    edges.append(ImportEdge(targets, path, statement.lineno))
        elif isinstance(statement, ast.ImportFrom):
            if statement.level:
                targets = _relative_target_candidates(
                    module_name, is_package, statement.level, statement.module
                )
                if not statement.module:
                    extra = []
                    for alias in statement.names:
                        if alias.name == "*":
                            continue
                        base = targets[0] if targets else ""
                        extra.extend(
                            _absolute_target_candidates(
                                f"{base}.{alias.name}" if base else alias.name
                            )
                        )
                    targets = tuple(dict.fromkeys((*extra, *targets)))
            else:
                base = statement.module or ""
                targets = list(_absolute_target_candidates(base))
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    dotted = f"{base}.{alias.name}" if base else alias.name
                    targets.extend(_absolute_target_candidates(dotted))
                targets = tuple(dict.fromkeys(targets))
            if targets:
                edges.append(ImportEdge(targets, path, statement.lineno))
    return edges


def _canonical_cycle(cycle: Sequence[str]) -> tuple[str, ...]:
    smallest = min(range(len(cycle)), key=lambda index: cycle[index])
    rotated = list(cycle[smallest:]) + list(cycle[:smallest])
    return tuple(rotated)


def check_import_cycles(modules: Sequence[ModuleInfo]) -> list[GraphIssue]:
    """Return one issue per import cycle found in the scanned module graph."""

    if len(modules) < 2:
        return []
    known = {module.name for module in modules}
    adjacency: dict[str, list[tuple[str, ImportEdge]]] = {name: [] for name in known}
    for module in modules:
        for edge in module.edges:
            for target in edge.targets:
                if target in known and target != module.name:
                    adjacency[module.name].append((target, edge))

    state: dict[str, int] = {name: 0 for name in known}
    path_stack: list[str] = []
    reported: set[tuple[str, ...]] = set()
    issues: list[GraphIssue] = []

    def visit(name: str) -> None:
        state[name] = 1
        path_stack.append(name)
        for target, edge in adjacency[name]:
            if state.get(target, 0) == 1:
                start = path_stack.index(target)
                cycle = path_stack[start:]
                key = _canonical_cycle(cycle)
                if key not in reported:
                    reported.add(key)
                    rendered = " -> ".join([*cycle, target])
                    issues.append(
                        GraphIssue(edge.path, edge.line, 1, f"Import cycle detected: {rendered}.")
                    )
            elif state.get(target, 0) == 0:
                visit(target)
        path_stack.pop()
        state[name] = 2

    for module in modules:
        if state.get(module.name, 0) == 0:
            visit(module.name)
    return issues


__all__ = [
    "BUILTIN_RULES",
    "CHECK_FUNCS",
    "CHECK_RULES",
    "CHECK_RULE_IDS",
    "ENGINE_RULE_IDS",
    "FIX_FUNCS",
    "FIX_ORDER",
    "FileContext",
    "GraphIssue",
    "ImportEdge",
    "Issue",
    "KNOWN_RULE_IDS",
    "LineEdit",
    "LogicalLine",
    "MANUAL_RULES",
    "ManualRule",
    "ModuleInfo",
    "ParseFailure",
    "Rule",
    "check_import_cycles",
    "check_snake_case_file",
    "is_compound_statement",
    "is_docstring_statement",
    "is_parenthesized",
    "module_docstring",
    "module_import_edges",
    "module_name_for_path",
    "operator_precedence",
    "rule_fixable",
    "rule_severity",
    "source_line_prefix_is_blank",
    "string_token_texts",
    "walk_with_parents",
]
