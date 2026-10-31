"""Conservative fixers for the custom Python style checker.

Every fixer takes the current ``file_context`` and returns rewritten
text; the engine rebuilds the context between fixers and validates the
final result (parseable source, identical ast.dump, identical string
token multiset), so a fixer only has to produce a layout change it
believes is local and safe. Edit primitives come from py_style_model,
analysis helpers from py_style_checks."""

from __future__ import annotations
import ast
import io
import keyword
import re
import tokenize
from typing import TYPE_CHECKING, Callable, Sequence
from celestia_devtools.lint.py_style_checks import (_BINARY_KEYWORDS, _BINARY_OPERATOR_STRINGS, _BRANCH_KEYWORDS, _UNARY_CAPABLE_OPERATORS, _UNARY_PREVIOUS_KEYWORDS, _UNARY_PREVIOUS_OPERATORS, _blank_runs, _body_entries, _colon_indices, _default_equals_indices, _explicit_precedence_children, _is_section_comment, _is_unary_operator, _keyword_operator_is_binary, _redundant_paren_issues)
from celestia_devtools.lint.py_style_model import (_CLOSE_BRACKETS, _FSTRING_TYPES, _LITERAL_TYPES, _SKIP_TOKEN_TYPES, _apply_line_edits, _comma_token_positions_of_single_element_tuples, _delete_rows, file_context, is_compound_statement, line_edit)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, runtime import is unused
    from celestia_devtools.lint.py_style_config import style_config

# ---------------------------------------------------------------------------
# Fixers
#
# Every fixer takes the current FileContext (built from the current text) and
# returns the rewritten text. The engine rebuilds the context between fixers and
# validates the final result (parseable source, identical ast.dump, identical
# string token multiset), so a fixer only has to produce a layout change that it
# believes is local and safe.
# ---------------------------------------------------------------------------

def fix_explicit_precedence(context: file_context, config: "style_config") -> str:
    """Wrap mixed-precedence operands in parentheses."""

    edits: list[line_edit] = []

    for child in _explicit_precedence_children(context):
        start = context.ast_start(child)
        end = context.ast_end(child)
        edits.append(line_edit(start[0], start[1], start[1], "("))
        edits.append(line_edit(end[0], end[1], end[1], ")"))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_trailing_whitespace(context: file_context, config: "style_config") -> str:
    """Strip trailing spaces and tabs outside multi-line strings."""

    edits: list[line_edit] = []

    for row in range(1, context.line_count + 1):
        line = context.line(row)
        stripped = line.rstrip(" \t")

        if (stripped == line) or context.in_literal(row, len(stripped)):
            continue

        edits.append(line_edit(row, len(stripped), len(line), ""))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_no_tabs(context: file_context, config: "style_config") -> str:
    """Expand tabs outside string literals to spaces."""

    edits: list[line_edit] = []

    for row in range(1, context.line_count + 1):
        line = context.line(row)
        indent = context.indent_width(row)

        for column, character in enumerate(line):
            if character != "\t":
                continue

            if context.in_literal(row, column):
                continue

            replacement = "    " if column < indent else " "
            edits.append(line_edit(row, column, column + 1, replacement))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_indent_four_spaces(context: file_context, config: "style_config") -> str:
    """Round offending block indentation down to the nearest multiple of four."""

    edits: list[line_edit] = []

    for row in sorted(context.statement_start_rows()):
        if context.line_starts_in_literal(row):
            continue

        if not context.line(row).strip():
            continue

        width = context.indent_width(row)

        if (width % 4) == 0:
            continue

        edits.append(line_edit(row, 0, width, " " * (width - (width % 4))))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_no_consecutive_blank_lines(context: file_context, config: "style_config") -> str:
    """Collapse runs of blank lines to one blank line."""

    delete: set[int] = set()
    row = 1

    while row <= context.line_count:
        if not context.is_blank_line(row):
            row += 1
            continue

        start = row

        while (row <= context.line_count) and context.is_blank_line(row):
            row += 1

        for extra in range(start + 1, row):
            delete.add(extra)

    if not delete:
        return context.text

    return _delete_rows(context, delete)

def _last_content_row(context: file_context) -> int | None:
    for row in range(context.line_count, 0, -1):
        if not context.is_blank_line(row):
            return row

    return None

def fix_final_blank_lines(context: file_context, config: "style_config") -> str:
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

def fix_final_newline(context: file_context, config: "style_config") -> str:
    """Rewrite the file tail to one newline (or none)."""

    if not context.text:
        return context.text

    stripped = context.text.rstrip("\n")

    if config.final_newline == "none":
        return stripped

    return stripped + "\n"

def fix_imports_contiguous(context: file_context, config: "style_config") -> str:
    """Remove blank lines between consecutive module-level imports."""

    delete: set[int] = set()
    body = context.tree.body

    for previous, current in zip(body, body[1:], strict = False):
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

def _branch_body_blank_rows(context: file_context) -> set[int]:
    """Return blank rows that sit before elif/else/except/finally branches."""

    delete: set[int] = set()

    for row in range(2, context.line_count + 1):
        stripped = context.line(row).lstrip(" \t")

        if not stripped:
            continue

        word = re.match(r"[A-Za-z_]+", stripped)

        if (word is None) or (word.group(0) not in _BRANCH_KEYWORDS):
            continue

        probe = row - 1

        while (probe >= 1) and context.is_full_comment(probe):
            probe -= 1

        while (probe >= 1) and context.is_blank_line(probe):
            delete.add(probe)
            probe -= 1

    return delete

def fix_branch_adjacency(context: file_context, config: "style_config") -> str:
    """Remove blank lines in front of elif/else/except/finally branches."""

    delete = _branch_body_blank_rows(context)

    if not delete:
        return context.text

    return _delete_rows(context, delete)

def fix_decorator_adjacency(context: file_context, config: "style_config") -> str:
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

def _blank_line_edits(context: file_context, config: "style_config") -> tuple[set[int], set[int]]:
    """Return (blank rows to delete, rows that need a blank line inserted before)."""

    delete: set[int] = set()
    insert: set[int] = set()

    for container, statements, header_check in _body_entries(context):
        if not statements:
            continue

        if isinstance(container, ast.Module) and (config.blank_line_scope == "nested"):
            continue

        if header_check:
            header_row = context.header_colon_row(container)
            first = statements[0]

            if (header_row is not None) and (first.lineno > header_row):
                for row in context.blank_rows_between(header_row, first.lineno):
                    delete.add(row)

        for previous, current in zip(statements, statements[1:], strict = False):
            if not (is_compound_statement(previous) or is_compound_statement(current)):
                continue

            previous_end = previous.end_lineno
            current_start = current.lineno

            if current_start <= previous_end:
                continue

            blanks = context.blank_rows_between(previous_end, current_start)

            if not blanks:
                # No blank line at all: insert one directly after the previous
                # statement, so a comment block attached to the next statement
                # keeps travelling with it.
                insert.add(previous_end + 1)
                continue

            for run in _blank_runs(blanks):
                if len(run) > 1:
                    delete.update(run[1:])

    return delete, insert

def _apply_blank_row_edits(context: file_context, delete: set[int], insert: set[int]) -> str:
    """Return the text with the given rows deleted and blank rows inserted.

    An inserted blank line goes before the row recorded in *insert*.
    """

    if not delete and not insert:
        return context.text

    rewritten: list[str] = []

    for row in range(1, context.line_count + 1):
        if row in insert:
            rewritten.append("")

        if row in delete:
            continue

        rewritten.append(context.lines[row - 1])

    if context.text.endswith("\n"):
        rewritten.append("")

    return "\n".join(rewritten)

def fix_blank_lines_around_blocks(context: file_context, config: "style_config") -> str:
    """Delete extra blank lines and insert the missing lines around blocks."""

    delete, insert = _blank_line_edits(context, config)
    return _apply_blank_row_edits(context, delete, insert)

def _comment_blank_rows(context: file_context, config: "style_config") -> set[int]:
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

            if (context.first_sig_row is None) or (row < context.first_sig_row):
                continue

            probe = row + 1

            while (probe <= context.line_count) and (context.is_blank_line(probe) or context.is_full_comment(probe)):
                probe += 1

            if probe > context.line_count:
                continue

            if context.indent_width(probe) < context.indent_width(row):
                continue

        probe = row + 1

        while (probe <= context.line_count) and context.is_blank_line(probe):
            delete.add(probe)
            probe += 1

    return delete

def fix_comment_blank_line(context: file_context, config: "style_config") -> str:
    """Delete blank lines inserted after a comment."""

    delete = _comment_blank_rows(context, config)

    if not delete:
        return context.text

    return _delete_rows(context, delete)

# -- token level layout helpers --------------------------------------------
_SPACE_AROUND_OPERATORS = frozenset({"+", "-", "*", "/", "//", "%", "@", "**", "==", "!=", "<", "<=", ">", ">=", "&", "|", "^", "<<", ">>", ":=", "=", "->"})
_SPACE_AROUND_KEYWORDS = frozenset({"and", "or", "in", "is", "not"})
_UNARY_ONLY_OPERATORS = frozenset({"~"})
_ATTACHING_PREVIOUS_KEYWORDS = frozenset({"def", "class", "C", "return", "yield", "assert", "if", "elif", "while", "lambda"})

def _is_literal_token(tok: tokenize.TokenInfo) -> bool:
    return tok.type in _LITERAL_TYPES

def _wordish(tok: tokenize.TokenInfo) -> bool:
    return tok.type in {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}

def _token_is_unary_operator(tokens: Sequence[tokenize.TokenInfo], index: int) -> bool:
    if (index < 0) or (index >= len(tokens)):
        return False

    tok = tokens[index]

    if tok.type != tokenize.OP:
        return False

    if tok.string not in {"+", "-", "*", "**", "~", "/", "@"}:
        return False

    if index == 0:
        return True

    previous = tokens[index - 1]

    if (previous.type == tokenize.OP) and (previous.string in _UNARY_PREVIOUS_OPERATORS):
        return True

    if (previous.type == tokenize.NAME) and (previous.string in _UNARY_PREVIOUS_KEYWORDS):
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

    return (previous.type in {tokenize.STRING, tokenize.NUMBER}) or (previous.type in _FSTRING_TYPES)

def _is_keyword_name(value: str) -> bool:
    return keyword.iskeyword(value)

def _needs_space(tokens: Sequence[tokenize.TokenInfo], index: int) -> bool:
    """Return whether a single space must separate tokens[index-1] and tokens[index]."""

    previous = tokens[index - 1]
    current = tokens[index]

    if (previous.type in _FSTRING_TYPES) or (current.type in _FSTRING_TYPES):
        return False

    if (current.type == tokenize.OP) and (current.string in ")]},;:"):
        return False

    if (previous.type == tokenize.OP) and (previous.string in "([{."):
        return False

    if (previous.type == tokenize.OP) and (previous.string == "@") and (index == 1):
        return False

    if (current.type == tokenize.OP) and (current.string in "([{"):
        return not _bracket_attaches(tokens, index)

    if (previous.type == tokenize.OP) and (previous.string in _UNARY_ONLY_OPERATORS):
        return False

    if _token_is_unary_operator(tokens, index - 1):
        return False

    if (previous.type == tokenize.OP) and (previous.string in {",", ":", ";"}):
        return True

    if (previous.type == tokenize.OP) and (previous.string in _SPACE_AROUND_OPERATORS):
        return True

    if (current.type == tokenize.OP) and (current.string in _SPACE_AROUND_OPERATORS):
        return True

    if (previous.type == tokenize.NAME) and (previous.string in _SPACE_AROUND_KEYWORDS):
        return True

    if (current.type == tokenize.NAME) and (current.string in _SPACE_AROUND_KEYWORDS):
        return True

    if (current.type == tokenize.OP) and (current.string in _UNARY_ONLY_OPERATORS):
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

_FRAGMENT_OPERATOR_CHARS = "+-*/%@<>=&|^~"

def _char_needs_space(previous: str, current: str) -> bool:
    """Return whether a single space is needed when joining two source fragments."""

    if not previous or not current:
        return False

    last = previous[-1]
    first = current[0]

    if last in "([{.":
        return False

    if first in ")]},;:.":
        return False

    if (last in _FRAGMENT_OPERATOR_CHARS) or (first in _FRAGMENT_OPERATOR_CHARS):
        return True

    if last in ",;:":
        return True

    wordish_last = last.isalnum() or (last == "_") or (last in "\"'")
    wordish_first = first.isalnum() or (first == "_") or (first in "\"'")

    if (last in ")]}") and (first.isalnum() or (first == "_") or (first in "\"'")):
        return True

    if wordish_last and wordish_first:
        return True

    return False

def _group_has_multiline_fstring(context: file_context, start_row: int, end_row: int) -> bool:
    """Return whether a single f-string inside the logical line spans rows."""

    for span_start_row, _start_column, span_end_row, _end_column in context.fstring_spans:
        if ((span_start_row < span_end_row) and (start_row <= span_start_row) and (span_end_row <= end_row)):
            return True

    return False

def _merge_source_fragments(context: file_context, start_row: int, end_row: int, tokens: list[tokenize.TokenInfo]) -> str | None:
    """Join the source fragments of a logical line whose tokens include f-strings.

    The token text inside an f-string is runtime data, so this path never
    re-spaces tokens: it keeps every token's original source text, adds only the
    whitespace needed at the removed line breaks, and accepts the result only if
    re-tokenizing the merged line yields exactly the same significant tokens and
    the whole file still parses.
    """

    first = tokens[0]
    last = tokens[-1]
    first_by_row: dict[int, tokenize.TokenInfo] = {}
    last_by_row: dict[int, tokenize.TokenInfo] = {}

    for tok in tokens:
        first_by_row.setdefault(tok.start[0], tok)
        last_by_row[tok.end[0]] = tok

    pieces: list[str] = []

    for row in range(start_row, end_row + 1):
        line = context.line(row)
        left = first.start[1] if row == start_row else first_by_row[row].start[1]
        right = last.end[1] if row == end_row else len(line)
        pieces.append(line[left:right].strip())

    merged = pieces[0]

    for offset in range(1, len(pieces)):
        if _char_needs_space(merged, pieces[offset]):
            merged += " "

        merged += pieces[offset]

    prefix = context.line(start_row)[: first.start[1]]
    merged = prefix + merged

    try:
        merged_tokens = [tok for tok in tokenize.generate_tokens(io.StringIO(merged).readline)if tok.type not in _SKIP_TOKEN_TYPES]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None

    if tuple((tok.type, tok.string)for tok in merged_tokens) != tuple((tok.type, tok.string)for tok in tokens):
        return None

    candidate_lines = list(context.lines[: context.line_count])
    candidate_lines[start_row - 1 : end_row] = [merged]

    if context.text.endswith("\n"):
        candidate_lines.append("")

    try:
        ast.parse("\n".join(candidate_lines))
    except SyntaxError:
        return None

    return merged

def _mergeable_logical_lines(context: file_context, config: "style_config") -> list[tuple[int, int, str]]:
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
        is_decorator = (first.type == tokenize.OP) and (first.string == "@")

        if is_decorator and config.allow_multiline_decorators:
            continue

        if any(tok.type == tokenize.COMMENT for tok in [context.tokens[i]for i in logical.token_ids]):
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

        if any(tok.type in _FSTRING_TYPES for tok in tokens):
            if _group_has_multiline_fstring(context, start_row, end_row):
                continue

            merged = _merge_source_fragments(context, start_row, end_row, tokens)

            if merged is None:
                continue

            merges.append((start_row, end_row, merged))
            continue

        merged = _merge_statement_tokens(tokens)
        prefix = first_line[: first.start[1]]
        merges.append((start_row, end_row, prefix + merged))

    return merges

def fix_single_line_statement(context: file_context, config: "style_config") -> str:
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

def _trailing_comma_positions(context: file_context) -> list[tuple[int, tokenize.TokenInfo]]:
    """Return comma tokens that violate the no-trailing-comma rule."""

    positions: list[tuple[int, tokenize.TokenInfo]] = []
    tuple_commas = _comma_token_positions_of_single_element_tuples(context)

    for index, tok in enumerate(context.sig):
        if (tok.type != tokenize.OP) or (tok.string not in _CLOSE_BRACKETS):
            continue

        previous_index = index - 1

        if previous_index < 0:
            continue

        previous = context.sig[previous_index]

        if (previous.type != tokenize.OP) or (previous.string != ","):
            continue

        if previous.start in tuple_commas:
            continue

        if context.bracket.get(tok.start) is None:
            continue

        positions.append((index, previous))

    return positions

def fix_no_trailing_comma(context: file_context, config: "style_config") -> str:
    """Delete trailing commas before closing brackets."""

    edits: list[line_edit] = []

    for index, comma in _trailing_comma_positions(context):
        following = context.sig[index]

        if following.start[0] == comma.end[0]:
            edits.append(line_edit(comma.start[0], comma.start[1], following.start[1], ""))
        else:
            edits.append(line_edit(comma.start[0], comma.start[1], comma.end[1], ""))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def _operator_spacing_edits(context: file_context) -> list[line_edit]:
    edits: list[line_edit] = []

    for index, tok in enumerate(context.sig):
        is_binary_token = False

        if (tok.type == tokenize.OP) and (tok.string in _BINARY_OPERATOR_STRINGS):
            if (tok.string in _UNARY_CAPABLE_OPERATORS) and _is_unary_operator(context, index):
                continue

            is_binary_token = True
        elif (tok.type == tokenize.NAME) and (tok.string in _BINARY_KEYWORDS):
            if not _keyword_operator_is_binary(context, index):
                continue

            is_binary_token = True

        if not is_binary_token:
            continue

        previous = context.prev_token(index)
        following = context.next_token(index)

        if (previous is not None) and (previous.end[0] == tok.start[0]):
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != " ":
                edits.append(line_edit(tok.start[0], previous.end[1], tok.start[1], " "))

        if (following is not None) and (following.start[0] == tok.start[0]):
            if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                edits.append(line_edit(tok.start[0], tok.end[1], following.start[1], " "))

    return edits

def fix_binary_operator_spacing(context: file_context, config: "style_config") -> str:
    """Normalise spaces around binary operators."""

    edits = _operator_spacing_edits(context)

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_equal_spacing(context: file_context, config: "style_config") -> str:
    """Normalise spaces around default and keyword '='."""

    edits: list[line_edit] = []

    for index in _default_equals_indices(context):
        tok = context.sig[index]
        previous = context.prev_token(index)
        following = context.next_token(index)

        if (previous is not None) and (previous.end[0] == tok.start[0]):
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != " ":
                edits.append(line_edit(tok.start[0], previous.end[1], tok.start[1], " "))

        if (following is not None) and (following.start[0] == tok.start[0]):
            if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                edits.append(line_edit(tok.start[0], tok.end[1], following.start[1], " "))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_colon_spacing(context: file_context, config: "style_config") -> str:
    """Normalise spaces around governed colons."""

    edits: list[line_edit] = []

    for index in _colon_indices(context, config):
        tok = context.sig[index]
        previous = context.prev_token(index)
        following = context.next_token(index)

        if (previous is not None) and (previous.end[0] == tok.start[0]):
            if context.line(tok.start[0])[previous.end[1] : tok.start[1]] != "":
                edits.append(line_edit(tok.start[0], previous.end[1], tok.start[1], ""))

        if (following is not None) and (following.start[0] == tok.start[0]):
            if not ((following.type == tokenize.OP) and (following.string in _CLOSE_BRACKETS)):
                if context.line(tok.start[0])[tok.end[1] : following.start[1]] != " ":
                    edits.append(line_edit(tok.start[0], tok.end[1], following.start[1], " "))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

def fix_redundant_parens(context: file_context, config: "style_config") -> str:
    """Delete one redundant outer parenthesis pair per expression."""

    edits: list[line_edit] = []

    for open_position, close_position in _redundant_paren_issues(context):
        edits.append(line_edit(open_position[0], open_position[1], open_position[1] + 1, ""))
        edits.append(line_edit(close_position[0], close_position[1], close_position[1] + 1, ""))

    if not edits:
        return context.text

    return _apply_line_edits(context, edits)

FIX_FUNCS: dict[str, Callable[[file_context, "style_config"], str]] = {"trailing-whitespace": fix_trailing_whitespace, "no-tabs": fix_no_tabs, "indent-four-spaces": fix_indent_four_spaces, "no-consecutive-blank-lines": fix_no_consecutive_blank_lines, "final-blank-lines": fix_final_blank_lines, "final-newline": fix_final_newline, "imports-contiguous": fix_imports_contiguous, "branch-adjacency": fix_branch_adjacency, "decorator-adjacency": fix_decorator_adjacency, "blank-lines-around-blocks": fix_blank_lines_around_blocks, "comment-blank-line": fix_comment_blank_line, "single-line-statement": fix_single_line_statement, "no-trailing-comma": fix_no_trailing_comma, "binary-operator-spacing": fix_binary_operator_spacing, "equal-spacing": fix_equal_spacing, "colon-spacing": fix_colon_spacing, "explicit-precedence": fix_explicit_precedence, "redundant-parens": fix_redundant_parens}

FIX_ORDER: tuple[str, ...] = ("trailing-whitespace", "no-tabs", "indent-four-spaces", "no-consecutive-blank-lines", "final-blank-lines", "final-newline", "imports-contiguous", "branch-adjacency", "decorator-adjacency", "blank-lines-around-blocks", "comment-blank-line", "single-line-statement", "no-trailing-comma", "binary-operator-spacing", "equal-spacing", "colon-spacing", "explicit-precedence", "redundant-parens")
