"""Per-file source model for the custom Python style checker.

Owns everything built from one source file: the tokenize/ast source
model (``file_context``, ``logical_line``), the shared token/AST
helpers, the line-edit primitives the fixers apply, and the
string-token multiset invariant (P09/P43). Checks live in
py_style_checks, fixers in py_style_fixers, the rule catalog and the
compatibility facade in py_style_rules, the cross-file import graph in
py_style_graph."""

from __future__ import annotations
import ast
import bisect
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Iterator, Sequence

class parse_failure(Exception):
    """Raised when a file cannot be tokenized or parsed."""

    def __init__(self, message: str, line: int = 1, column: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = max(1, line)
        self.column = max(1, column)

@dataclass(frozen = True)
class issue_record:
    """One rule violation inside one file."""

    rule: str
    line: int
    column: int
    message: str

@dataclass
class logical_line:
    """One logical line, i.e. the tokens consumed before a NEWLINE token."""

    token_ids: list[int]
    sig_ids: list[int] = field(default_factory = list)

_SKIP_TOKEN_TYPES = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})

_FSTRING_TYPES = frozenset(getattr(tokenize, name)for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")if hasattr(tokenize, name))
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
class file_context:
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
    logical: list[logical_line]
    literals: list[tuple[int, int, int, int]]
    comment_rows: dict[int, int]
    first_sig_row: int | None
    literal_starts: list[tuple[int, int]] = field(default_factory = list)
    literal_ends: list[tuple[int, int]] = field(default_factory = list)
    fstring_spans: tuple[tuple[int, int, int, int], ...] = ()
    fstring_row_intervals: dict[int, tuple[tuple[int, int], ...]] = field(default_factory = dict)
    inside_literal_rows: frozenset[int] = frozenset()
    blank_rows: tuple[int, ...] = ()
    blank_row_set: frozenset[int] = frozenset()
    parent_map: dict[int, ast.AST] = field(default_factory = dict)
    node_list: tuple[ast.AST, ...] = ()
    line_bytes: dict[int, bytes] = field(default_factory = dict)
    definitions_cache: list[definition] | None = None

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls, path: str, text: str) -> "file_context":
        """Tokenize and parse *text*; raise ParseFailure on failure."""

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            message, line, column = _tokenize_failure(exc)
            raise parse_failure(message, line, column) from exc

        try:
            tree = ast.parse(text, filename = path)
        except SyntaxError as exc:
            raise parse_failure(str(exc.msg or exc), exc.lineno or 1, exc.offset or 1) from exc
        except (ValueError, RecursionError) as exc:
            raise parse_failure(str(exc) or exc.__class__.__name__, 1, 1) from exc

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

        logical: list[logical_line] = []
        current: list[int] = []

        for index, tok in enumerate(tokens):
            current.append(index)

            if tok.type == tokenize.NEWLINE:
                sig_in = [i for i in current if tokens[i].type not in _SKIP_TOKEN_TYPES]
                logical.append(logical_line(current, sig_in))
                current = []

        if current:
            sig_in = [i for i in current if tokens[i].type not in _SKIP_TOKEN_TYPES]

            if sig_in:
                logical.append(logical_line(current, sig_in))

        literals = [(tok.start[0], tok.start[1], tok.end[0], tok.end[1])for tok in tokens if tok.type in _LITERAL_TYPES]
        literal_starts = [(start_row, start_column)for start_row, start_column, _e_row, _e_column in literals]
        literal_ends = [(end_row, end_column)for _s_row, _s_column, end_row, end_column in literals]
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

                if (tok.type == tokenize.FSTRING_END) and open_fstrings:
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

            fstring_row_intervals.setdefault(start_row, []).append((start_column, len(lines[start_row - 1]) + 1))

            for row in range(start_row + 1, end_row):
                fstring_row_intervals.setdefault(row, []).append((0, len(lines[row - 1]) + 1))

            fstring_row_intervals.setdefault(end_row, []).append((0, end_column))
            inside_rows.update(range(start_row + 1, end_row + 1))

        comment_rows: dict[int, int] = {}

        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue

            row = tok.start[0]

            if (row not in comment_rows) and (row <= line_count):
                prefix = lines[row - 1][: tok.start[1]]

                if not prefix.strip():
                    comment_rows[row] = tok.start[1]

        blank_rows = tuple(row for row in range(1, line_count + 1)if (row not in inside_rows) and not lines[row - 1].strip())

        node_list = tuple(ast.walk(tree))
        parent_map: dict[int, ast.AST] = {}

        for parent_node in node_list:
            for child in ast.iter_child_nodes(parent_node):
                parent_map[id(child)] = parent_node

        first_sig_row = sig[0].start[0] if sig else None

        return cls(path = path, text = text, lines = lines, line_count = line_count, tokens = tokens, tree = tree, sig_ids = sig_ids, sig = sig, sig_starts = sig_starts, bracket = bracket, logical = logical, literals = literals, comment_rows = comment_rows, first_sig_row = first_sig_row, literal_starts = literal_starts, literal_ends = literal_ends, fstring_spans = tuple(fstring_spans), fstring_row_intervals = {row: tuple(intervals)for row, intervals in fstring_row_intervals.items()}, inside_literal_rows = frozenset(inside_rows), blank_rows = blank_rows, blank_row_set = frozenset(blank_rows), parent_map = parent_map, node_list = node_list)

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
            if (start < end_column) and (end > start_column):
                return True

        return False

    def in_literal(self, row: int, column: int) -> bool:
        """Return whether the position is strictly inside a string literal.

        F-strings count as literal regions on every Python version (see
        in_fstring_span), so no rule or fixer reaches inside them.
        """

        position = (row, column)
        index = bisect.bisect_right(self.literal_starts, position) - 1

        if (index >= 0) and (self.literal_starts[index] < position < self.literal_ends[index]):
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

        if (row is None) or (column is None):
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

    def definitions(self) -> list["definition"]:
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

        if (index + 1) >= len(self.sig):
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
                elif (tok.string == ":") and (depth == 0):
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

    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)

def module_docstring(tree: ast.Module) -> ast.Expr | None:
    """Return the module docstring expression, if any."""

    if tree.body and is_docstring_statement(tree.body[0]):
        return tree.body[0]  # type: ignore[return-value]

    return None

_COMPOUND_TYPES: tuple[type[ast.AST], ...] = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)

if hasattr(ast, "TryStar"):  # Python 3.11+
    _COMPOUND_TYPES = (*_COMPOUND_TYPES, ast.TryStar)

if hasattr(ast, "Match"):  # Python 3.10+
    _COMPOUND_TYPES = (*_COMPOUND_TYPES, ast.Match)

def is_compound_statement(node: ast.AST) -> bool:
    """Return whether *node* is a complete block statement."""

    return isinstance(node, _COMPOUND_TYPES)

_BINOP_PRECEDENCE: dict[type[ast.operator], int] = {ast.BitOr: 5, ast.BitXor: 6, ast.BitAnd: 7, ast.LShift: 8, ast.RShift: 8, ast.Add: 9, ast.Sub: 9, ast.Mult: 10, ast.Div: 10, ast.FloorDiv: 10, ast.Mod: 10, ast.MatMult: 10, ast.Pow: 12}

def operator_precedence(node: ast.AST) -> int | None:
    """Return the precedence rank of an operator-bearing expression node."""

    if isinstance(node, ast.BoolOp):
        return 2 if isinstance(node.op, ast.And) else 1

    if isinstance(node, ast.Compare):
        return 4

    if isinstance(node, ast.BinOp):
        return _BINOP_PRECEDENCE.get(type(node.op))

    return None

def is_parenthesized(context: file_context, node: ast.AST) -> bool:
    """Return whether *node* is directly wrapped in one matching pair of parentheses."""

    start = context.ast_start(node)
    end = context.ast_end(node)
    previous_index = context.token_index_before(start)
    next_index = context.token_index_at_or_after(end)

    if (previous_index is None) or (next_index is None):
        return False

    previous = context.sig[previous_index]
    following = context.sig[next_index]

    if not ((previous.type == tokenize.OP) and (previous.string == "(") and (following.type == tokenize.OP) and (following.string == ")")):
        return False

    return context.bracket.get(previous.start) == following.start

def source_line_prefix_is_blank(context: file_context, row: int, column: int) -> bool:
    """Return whether the characters before *column* on *row* are whitespace."""

    return not context.line(row)[:column].strip()

# ---------------------------------------------------------------------------
# Small helpers shared by checks and fixers
# ---------------------------------------------------------------------------

def _issue(rule_id: str, row: int, column: int, message: str) -> issue_record:
    return issue_record(rule_id, max(1, row), max(1, column), message)

def _is_string_token(tok: tokenize.TokenInfo) -> bool:
    return tok.type in _LITERAL_TYPES

def _is_op(tok: tokenize.TokenInfo | None, value: str) -> bool:
    return (tok is not None) and (tok.type == tokenize.OP) and (tok.string == value)

def _is_name(tok: tokenize.TokenInfo | None, value: str) -> bool:
    return (tok is not None) and (tok.type == tokenize.NAME) and (tok.string == value)

def _string_token_texts(context: file_context) -> list[str]:
    """Return the text of every string/f-string literal token, in source order."""

    return [tok.string for tok in context.tokens if tok.type in _LITERAL_TYPES]

def _comma_token_positions_of_single_element_tuples(context: file_context) -> set[tuple[int, int]]:
    """Return the comma positions that are grammar-required by one-element tuples."""

    positions: set[tuple[int, int]] = set()

    for node in context.nodes():
        if not isinstance(node, ast.Tuple):
            continue

        if (len(node.elts) != 1) or isinstance(node.elts[0], ast.Starred):
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
# Definition collection (naming-rule source material)
# ---------------------------------------------------------------------------

@dataclass(frozen = True)
class definition:
    """One definition-like name collected for the naming rules."""

    name: str
    row: int
    column: int
    kind: str
    node: ast.AST
    value: ast.AST | None = None
    annotation: ast.AST | None = None
    module_level: bool = False

def _definitions(context: file_context) -> list[definition]:
    """Collect class, function, parameter and assignment names with context."""

    result: list[definition] = []

    def add(name: str, node: ast.AST, kind: str, value: ast.AST | None = None, annotation: ast.AST | None = None, module_level: bool = False) -> None:
        row = getattr(node, "lineno", 1)
        column = context.char_col(row, getattr(node, "col_offset", 0))
        result.append(definition(name, row, column + 1, kind, node, value, annotation, module_level))

    def add_target(target: ast.AST, value: ast.AST | None, annotation: ast.AST | None, module_level: bool) -> None:
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

# ---------------------------------------------------------------------------
# Line-edit primitives (shared by checks that re-check and fixers that apply)
# ---------------------------------------------------------------------------

@dataclass(frozen = True)
class line_edit:
    """Replace characters [start, end) of one physical line with text."""

    row: int
    start: int
    end: int
    text: str

def _apply_line_edits(context: file_context, edits: Sequence[line_edit]) -> str:
    """Apply non-overlapping line edits and return the rewritten file text."""

    grouped: dict[int, list[line_edit]] = {}

    for edit in edits:
        if not 1 <= edit.row <= context.line_count:
            continue

        line_length = len(context.lines[edit.row - 1])

        if (edit.start < 0) or (edit.start > line_length):
            continue

        if context.edit_touches_fstring(edit.row, edit.start, edit.end):
            continue

        grouped.setdefault(edit.row, []).append(edit)

    rewritten = list(context.lines[: context.line_count])

    for row, row_edits in grouped.items():
        accepted: list[line_edit] = []

        for edit in sorted(row_edits, key = lambda item: (item.start, item.end)):
            if accepted and (edit.start < accepted[-1].end):
                continue

            accepted.append(edit)

        line = context.lines[row - 1]

        for edit in sorted(accepted, key = lambda item: item.start, reverse = True):
            start = edit.start
            end = min(edit.end, len(line))
            line = line[:start] + edit.text + line[end:]

        rewritten[row - 1] = line

    if context.text.endswith("\n"):
        rewritten.append("")

    return "\n".join(rewritten)

def _delete_rows(context: file_context, rows: set[int]) -> str:
    """Return the file text with the given physical rows removed."""

    rewritten = [context.lines[row - 1]for row in range(1, context.line_count + 1)if row not in rows]

    if context.text.endswith("\n"):
        rewritten.append("")

    return "\n".join(rewritten)

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
