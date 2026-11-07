"""Check functions for the custom Python style checker.

One ``check_*`` function per catalogued rule, plus the private analysis
helpers they share. ``CHECK_FUNCS`` is the rule-id registry the engine
drives. The source model comes from py_style_model; ``fix_explicit_precedence``
physically lived here once (next to its check) but is a fixer and moved to
py_style_fixers with the split."""

from __future__ import annotations
import ast
import fnmatch
import re
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence
from celestia_devtools.lint.py_style_model import (_CLOSE_BRACKETS, _FSTRING_TYPES, _OPEN_BRACKETS, _camel_to_snake, _comma_token_positions_of_single_element_tuples, _definitions, _is_op, _issue, definition, file_context, is_compound_statement, is_docstring_statement, is_parenthesized, issue_record, operator_precedence)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, runtime import is unused
    from celestia_devtools.lint.py_style_config import style_config

def check_trailing_whitespace(context: file_context, config: "style_config") -> list[issue_record]:
    """Report trailing spaces/tabs outside multi-line string literals."""

    issues: list[issue_record] = []

    for row in range(1, context.line_count + 1):
        line = context.line(row)
        stripped = line.rstrip(" \t")

        if stripped == line:
            continue

        if context.in_literal(row, len(stripped)):
            continue

        issues.append(_issue("trailing-whitespace", row, len(stripped) + 1, "Line ends with trailing whitespace."))

    return issues

def check_final_blank_lines(context: file_context, config: "style_config") -> list[issue_record]:
    """Report blank lines after the last non-blank line."""

    last_content: int | None = None

    for row in range(context.line_count, 0, -1):
        if not context.is_blank_line(row):
            last_content = row
            break

    if (last_content is None) or (last_content >= context.line_count):
        return []

    blank_count = context.line_count - last_content
    return [_issue("final-blank-lines", last_content + 1, 1, f"File ends with {blank_count} blank line(s); remove them.")]

def check_final_newline(context: file_context, config: "style_config") -> list[issue_record]:
    """Report a missing or extra final newline."""

    if not context.text:
        return []

    ends_with_newline = context.text.endswith("\n")
    row = max(1, context.line_count)

    if config.final_newline == "none":
        if ends_with_newline:
            return [_issue("final-newline", row, 1, "File must not end with a newline (final_newline = none).")]

        return []

    if not ends_with_newline or context.text.endswith("\n\n"):
        return [_issue("final-newline", row, 1, "File must end with exactly one newline.")]

    return []

def check_indent_four_spaces(context: file_context, config: "style_config") -> list[issue_record]:
    """Report block indentation widths that are not a multiple of four.

    Only the first physical row of a logical line is checked: continuation
    lines inside brackets are aligned freely and are not block indentation.
    """

    issues: list[issue_record] = []

    for row in sorted(context.statement_start_rows()):
        if context.line_starts_in_literal(row):
            continue

        if not context.line(row).strip():
            continue

        width = context.indent_width(row)

        if width % 4:
            issues.append(_issue("indent-four-spaces", row, width + 1, f"Indentation is {width} columns; it must be a multiple of 4."))

    return issues

def check_no_tabs(context: file_context, config: "style_config") -> list[issue_record]:
    """Report tab characters outside string literals."""

    issues: list[issue_record] = []

    for row in range(1, context.line_count + 1):
        line = context.line(row)

        for column, character in enumerate(line):
            if character != "\t":
                continue

            if context.in_literal(row, column):
                continue

            issues.append(_issue("no-tabs", row, column + 1, "Tab character in code; indent with spaces instead."))
            break

    return issues

def check_no_consecutive_blank_lines(context: file_context, config: "style_config") -> list[issue_record]:
    """Report runs of two or more blank lines."""

    issues: list[issue_record] = []
    row = 1

    while row <= context.line_count:
        if not context.is_blank_line(row):
            row += 1
            continue

        start = row

        while (row <= context.line_count) and context.is_blank_line(row):
            row += 1

        length = row - start

        if length >= 2:
            issues.append(_issue("no-consecutive-blank-lines", start + 1, 1, f"{length} consecutive blank lines; at most one is allowed."))

    return issues

_BLOCK_HEADER_KEYWORDS = frozenset({"if", "elif", "else", "for", "while", "with", "try", "except", "finally", "def", "class", "match", "case"})

def check_one_statement_per_line(context: file_context, config: "style_config") -> list[issue_record]:
    """Report semicolon-joined statements and single-line block bodies."""

    issues: list[issue_record] = []

    for tok in context.sig:
        if (tok.type == tokenize.OP) and (tok.string == ";"):
            issues.append(_issue("one-statement-per-line", tok.start[0], tok.start[1] + 1, "Multiple statements on one line (semicolon)."))

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
            elif (tok.string == ":") and (depth == 0):
                colon_position = position
                break

        if colon_position is None:
            continue

        colon_tok = tokens[colon_position]

        for tok in tokens[colon_position + 1 :]:
            if tok.start[0] == colon_tok.start[0]:
                issues.append(_issue("one-statement-per-line", tok.start[0], tok.start[1] + 1, "Block body must start on its own line (single-line block)."))
                break

    return issues

def check_single_line_statement(context: file_context, config: "style_config") -> list[issue_record]:
    """Report logical lines that span several physical lines."""

    issues: list[issue_record] = []

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

        is_decorator = (first.type == tokenize.OP) and (first.string == "@") and (len(sig_ids) >= 2)
        decorator_name = ""

        if is_decorator:
            second = tokens[1]
            decorator_name = second.string if second.type == tokenize.NAME else "expression"

            if config.allow_multiline_decorators:
                continue

        # A logical line that contains a multi-line string literal is exempt: the
        # line breaks inside the literal are runtime data (P09/P43) and rewriting
        # them to one physical line would change the string. Python 3.11
        # tokenizes an f-string as one STRING token; 3.12+ splits it into
        # FSTRING_START/MIDDLE/END tokens (FSTRING_MIDDLE may span rows).
        contains_multiline_literal = any((tok.end[0] > tok.start[0]) and ((tok.type == tokenize.STRING) or (tok.type in _FSTRING_TYPES)) for tok in tokens)

        if contains_multiline_literal:
            continue

        # PEP 701 also allows an f-string expression itself to span lines without
        # any multi-row literal token; such a line is still a multi-line string
        # statement and stays exempt.
        only_fstring_span = True

        for tok in tokens:
            if tok.start[0] == start_row:
                continue

            if context.in_fstring_span(tok.start[0], tok.start[1]):
                continue

            only_fstring_span = False
            break

        if only_fstring_span:
            continue

        line_span = end_row - start_row + 1

        if is_decorator:
            message = f"Decorator {decorator_name} spans {line_span} physical lines; " "keep decorators on one line."
        else:
            message = f"Statement spans {line_span} physical lines; keep one statement per physical line."

        issues.append(_issue("single-line-statement", first.start[0], first.start[1] + 1, message))

    return issues

def check_imports_at_top(context: file_context, config: "style_config") -> list[issue_record]:
    """Report module-level imports that follow a non-import statement."""

    issues: list[issue_record] = []
    body = list(context.tree.body)

    if body and is_docstring_statement(body[0]):
        body = body[1:]

    seen_code = False

    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if seen_code:
                issues.append(_issue("imports-at-top", statement.lineno, context.char_col(statement.lineno, statement.col_offset) + 1, "Imports must be grouped at the top of the module."))
        else:
            seen_code = True

    return issues

def check_imports_contiguous(context: file_context, config: "style_config") -> list[issue_record]:
    """Report blank lines between consecutive module-level imports."""

    issues: list[issue_record] = []
    body = context.tree.body

    for previous, current in zip(body, body[1:], strict = False):
        if not isinstance(previous, (ast.Import, ast.ImportFrom)):
            continue

        if not isinstance(current, (ast.Import, ast.ImportFrom)):
            continue

        if current.lineno <= previous.end_lineno:
            continue

        blanks = context.blank_rows_between(previous.end_lineno, current.lineno)

        if blanks:
            issues.append(_issue("imports-contiguous", current.lineno, context.char_col(current.lineno, current.col_offset) + 1, "Imports must not be separated by blank lines."))

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

def check_no_block_imports(context: file_context, config: "style_config") -> list[issue_record]:
    """Report imports that are not at module level."""

    issues: list[issue_record] = []

    for node in context.nodes():
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        parent = context.parent(node)

        if (parent is None) or isinstance(parent, ast.Module):
            continue

        exempt = False

        for ancestor in context.ancestors(node):
            if (config.allow_type_checking_imports and isinstance(ancestor, ast.If) and _is_type_checking_test(ancestor.test)):
                exempt = True
                break

            if (config.allow_optional_imports and isinstance(ancestor, ast.Try) and any(_is_import_error_handler(handler)for handler in ancestor.handlers)):
                exempt = True
                break

        if exempt:
            continue

        issues.append(_issue("no-block-imports", node.lineno, context.char_col(node.lineno, node.col_offset) + 1, "Imports are only allowed at module level."))

    return issues

def check_no_dynamic_imports(context: file_context, config: "style_config") -> list[issue_record]:
    """Report __import__ and importlib.import_module calls."""

    issues: list[issue_record] = []
    importlib_aliases: set[str] = {"importlib"}
    import_module_aliases: set[str] = set()

    for node in context.nodes():
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and (node.module == "importlib"):
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
        elif isinstance(function, ast.Attribute) and (function.attr == "import_module"):
            base = function.value

            if isinstance(base, ast.Name) and (base.id in importlib_aliases):
                dynamic = True

        if not dynamic:
            continue

        row, column = context.ast_start(node)
        issues.append(_issue("no-dynamic-imports", row, column + 1, "Dynamic imports are not allowed; use a static import."))

    return issues

_BINARY_OPERATOR_STRINGS = frozenset({"+", "-", "*", "/", "//", "%", "@", "**", "==", "!=", "<", "<=", ">", ">=", "&", "|", "^", "<<", ">>", ":="})
_BINARY_KEYWORDS = frozenset({"and", "or", "in", "is", "not"})
_UNARY_CAPABLE_OPERATORS = frozenset({"+", "-", "*", "**", "/", "@"})
_UNARY_PREVIOUS_OPERATORS = frozenset({"(", "[", "{", ",", ":", ";", ".", "+", "-", "*", "**", "/", "//", "%", "@", "~", "=", "==", "!=", "<", "<=", ">", ">=", "&", "|", "^", "<<", ">>", "->", ":="})
_UNARY_PREVIOUS_KEYWORDS = frozenset({"return", "yield", "if", "elif", "while", "assert", "lambda", "else", "not", "and", "or", "in", "is", "for", "del", "from", "import", "with", "as", "raise", "await", "global", "nonlocal", "case"})

def _is_unary_operator(context: file_context, index: int) -> bool:
    tok = context.sig[index]
    previous = context.prev_token(index)

    if previous is None:
        return True

    if previous.end[0] != tok.start[0]:
        return True

    if (previous.type == tokenize.OP) and (previous.string in _UNARY_PREVIOUS_OPERATORS):
        return True

    if (previous.type == tokenize.NAME) and (previous.string in _UNARY_PREVIOUS_KEYWORDS):
        return True

    return False

def _keyword_operator_is_binary(context: file_context, index: int) -> bool:
    tok = context.sig[index]

    if tok.string in {"and", "or", "in", "is"}:
        return True

    previous = context.prev_token(index)
    following = context.next_token(index)

    if (previous is not None) and (previous.string == "is"):
        return True

    return (following is not None) and (following.string == "in")

def check_binary_operator_spacing(context: file_context, config: "style_config") -> list[issue_record]:
    """Report binary operators without exactly one space on each side."""

    issues: list[issue_record] = []

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

        if (previous is None) or (following is None):
            continue

        if (previous.end[0] != tok.start[0]) or (following.start[0] != tok.start[0]):
            continue

        line = context.line(tok.start[0])
        left = line[previous.end[1] : tok.start[1]]
        right = line[tok.end[1] : following.start[1]]

        if (left == " ") and (right == " "):
            continue

        issues.append(_issue("binary-operator-spacing", tok.start[0], tok.start[1] + 1, f"Operator '{tok.string}' must be surrounded by exactly one space."))

    return issues

def _find_equal_index(context: file_context, start: tuple[int, int], end: tuple[int, int]) -> int | None:
    for index in context.token_indices_in_range(start, end):
        tok = context.sig[index]

        if (tok.type == tokenize.OP) and (tok.string == "="):
            return index

    return None

def _default_equals_indices(context: file_context) -> list[int]:
    """Return the token indices of '=' in defaults and keyword arguments."""

    found: list[int] = []

    def add_function_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        arguments = node.args
        positional = list(arguments.posonlyargs) + list(arguments.args)
        paired = positional[len(positional) - len(arguments.defaults) :]

        for argument, default in zip(paired, arguments.defaults, strict = True):
            index = _find_equal_index(context, context.ast_end(argument), context.ast_start(default))

            if index is not None:
                found.append(index)

        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict = True):
            if default is None:
                continue

            index = _find_equal_index(context, context.ast_end(argument), context.ast_start(default))

            if index is not None:
                found.append(index)

    for node in context.nodes():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            add_function_arguments(node)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue

                index = _find_equal_index(context, context.ast_start(keyword), context.ast_start(keyword.value))

                if index is not None:
                    found.append(index)

    return sorted(set(found))

def _spacing_problems(context: file_context, index: int, *, check_right: bool = True) -> list[str]:
    """Return human-readable spacing problems around token *index*."""

    tok = context.sig[index]
    line = context.line(tok.start[0])
    problems: list[str] = []
    previous = context.prev_token(index)

    if (previous is not None) and (previous.end[0] == tok.start[0]):
        if line[previous.end[1] : tok.start[1]] != " ":
            problems.append("no space before")

    following = context.next_token(index)

    if check_right and (following is not None) and (following.start[0] == tok.start[0]):
        if line[tok.end[1] : following.start[1]] != " ":
            problems.append("no space after")

    return problems

def check_equal_spacing(context: file_context, config: "style_config") -> list[issue_record]:
    """Report '=' in defaults and keyword arguments without exactly one space."""

    issues: list[issue_record] = []

    for index in _default_equals_indices(context):
        problems = _spacing_problems(context, index)

        if not problems:
            continue

        tok = context.sig[index]
        issues.append(_issue("equal-spacing", tok.start[0], tok.start[1] + 1, "The '=' of a default or keyword argument must be surrounded by exactly one space."))

    return issues

def _find_colon_index(context: file_context, start: tuple[int, int], end: tuple[int, int]) -> int | None:
    for index in context.token_indices_in_range(start, end):
        tok = context.sig[index]

        if (tok.type == tokenize.OP) and (tok.string == ":"):
            return index

    return None

def _lambda_colon_index(context: file_context, node: ast.Lambda) -> int | None:
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
        elif (tok.string == ":") and (depth == 0):
            candidate = index

    return candidate

def _slice_colon_indices(context: file_context) -> list[int]:
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

        if (tok.string != ":") or not stack or (stack[-1] != "["):
            continue

        previous = context.prev_token(index)
        following = context.next_token(index)

        if (previous is not None) and (previous.type == tokenize.OP) and (previous.string == ":"):
            continue

        if (following is not None) and (following.type == tokenize.OP) and (following.string == ":"):
            continue

        if (following is not None) and (following.type in _FSTRING_TYPES):
            continue

        found.append(index)

    return found

def _colon_indices(context: file_context, config: "style_config") -> list[int]:
    """Return the token indices of colons whose spacing the rule governs."""

    found: set[int] = set()

    for node in context.nodes():
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict = True):
                if key is None:
                    continue

                index = _find_colon_index(context, context.ast_end(key), context.ast_start(value))

                if index is not None:
                    found.add(index)
        elif isinstance(node, ast.DictComp):
            index = _find_colon_index(context, context.ast_end(node.key), context.ast_start(node.value))

            if index is not None:
                found.add(index)
        elif isinstance(node, ast.AnnAssign):
            index = _find_colon_index(context, context.ast_end(node.target), context.ast_start(node.annotation))

            if index is not None:
                found.add(index)
        elif isinstance(node, ast.arg) and (node.annotation is not None):
            index = _find_colon_index(context, context.ast_start(node), context.ast_start(node.annotation))

            if index is not None:
                found.add(index)
        elif isinstance(node, ast.Lambda):
            index = _lambda_colon_index(context, node)

            if index is not None:
                found.add(index)

    if config.slice_colons == "spaced":
        found.update(_slice_colon_indices(context))

    return sorted(found)

def check_colon_spacing(context: file_context, config: "style_config") -> list[issue_record]:
    """Report dict/annotation/lambda colon spacing problems."""

    issues: list[issue_record] = []

    for index in _colon_indices(context, config):
        tok = context.sig[index]
        line = context.line(tok.start[0])
        found: list[str] = []
        previous = context.prev_token(index)

        if (previous is not None) and (previous.end[0] == tok.start[0]):
            gap = line[previous.end[1] : tok.start[1]]

            if gap:
                found.append(f"{len(gap)} space(s) before the colon (expected none)")

        following = context.next_token(index)

        if (following is not None) and (following.start[0] == tok.start[0]):
            if not ((following.type == tokenize.OP) and (following.string in _CLOSE_BRACKETS)):
                gap = line[tok.end[1] : following.start[1]]

                if gap != " ":
                    found.append(f"{len(gap)} space(s) after the colon (expected exactly one)")

        if not found:
            continue

        issues.append(_issue("colon-spacing", tok.start[0], tok.start[1] + 1, "Colon spacing: found " + " and ".join(found) + "."))

    return issues

def check_no_trailing_comma(context: file_context, config: "style_config") -> list[issue_record]:
    """Report commas directly before a closing bracket, minus tuple exemptions."""

    issues: list[issue_record] = []
    position_to_index = {tok.start: index for index, tok in enumerate(context.sig)}
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

        opener = context.bracket.get(tok.start)

        if (opener is None) or (opener not in position_to_index):
            continue

        issues.append(_issue("no-trailing-comma", previous.start[0], previous.start[1] + 1, "Trailing comma before a closing bracket; remove it."))

    return issues

def _absolute_offset(context: file_context, position: tuple[int, int]) -> int:
    row, column = position
    offset = 0

    for line_index in range(row - 1):
        offset += len(context.lines[line_index]) + 1

    return offset + column

def _removing_pair_keeps_ast(context: file_context, open_position: tuple[int, int], close_position: tuple[int, int]) -> bool:
    open_offset = _absolute_offset(context, open_position)
    close_offset = _absolute_offset(context, close_position)

    if close_offset <= open_offset:
        return False

    candidate = context.text[: open_offset] + context.text[open_offset + 1: close_offset] + context.text[close_offset + 1:]

    try:
        rewritten = ast.parse(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return False

    return ast.dump(rewritten, include_attributes = False) == ast.dump(context.tree, include_attributes = False)

def _call_parenthesis_pair(context: file_context, call: ast.Call) -> tuple[tuple[int, int], tuple[int, int]] | None:
    end = context.ast_end(call)
    close_position = (end[0], end[1] - 1)

    if close_position[1] < 0:
        return None

    index = context.token_index_at_or_after(close_position)

    if index is None:
        return None

    tok = context.sig[index]

    if (tok.start != close_position) or (tok.type != tokenize.OP) or (tok.string != ")"):
        return None

    opener = context.bracket.get(close_position)

    if opener is None:
        return None

    return opener, close_position

_SKIPPED_REDUNDANT_TYPES: tuple[type[ast.AST], ...] = (ast.Tuple, ast.GeneratorExp, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Starred)

def _redundant_paren_candidate(context: file_context, node: ast.AST, call_pair: tuple[tuple[int, int], tuple[int, int]] | None) -> tuple[int, int] | None:
    if isinstance(node, _SKIPPED_REDUNDANT_TYPES):
        return None

    start = context.ast_start(node)
    end = context.ast_end(node)

    if start[0] != end[0]:
        return None

    previous_index = context.token_index_before(start)
    next_index = context.token_index_at_or_after(end)

    if (previous_index is None) or (next_index is None):
        return None

    previous = context.sig[previous_index]
    following = context.sig[next_index]

    if not (_is_op(previous, "(") and _is_op(following, ")")):
        return None

    pair = (previous.start, following.start)

    if (call_pair is not None) and (pair == call_pair):
        return None

    if context.bracket.get(previous.start) != following.start:
        return None

    if previous.start[0] != following.start[0]:
        return None

    return pair

def _redundant_paren_issues(context: file_context) -> list[tuple[int, int]]:
    """Return (open, close) positions of reportable redundant parenthesis pairs."""

    positions: list[tuple[int, int]] = []
    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    for node in context.nodes():
        candidates: list[tuple[ast.AST, tuple[tuple[int, int], tuple[int, int]] | None]] = []

        if isinstance(node, ast.Call):
            pair = _call_parenthesis_pair(context, node)
            candidates.extend((argument, pair) for argument in node.args)
            candidates.extend((keyword.value, pair)for keyword in node.keywords if keyword.value is not None)
        elif isinstance(node, ast.Assign):
            candidates.append((node.value, None))
        elif isinstance(node, ast.AnnAssign) and (node.value is not None):
            candidates.append((node.value, None))
        elif isinstance(node, ast.Return) and (node.value is not None):
            candidates.append((node.value, None))

        for candidate_node, call_pair in candidates:
            pair = _redundant_paren_candidate(context, candidate_node, call_pair)

            if (pair is None) or (pair in seen):
                continue

            if not _removing_pair_keeps_ast(context, pair[0], pair[1]):
                continue

            seen.add(pair)
            positions.append(pair)

    return positions

def check_redundant_parens(context: file_context, config: "style_config") -> list[issue_record]:
    """Report redundant outer parentheses around expressions."""

    issues: list[issue_record] = []

    for open_position, _close_position in _redundant_paren_issues(context):
        issues.append(_issue("redundant-parens", open_position[0], open_position[1] + 1, "Redundant outer parentheses around this expression."))

    return issues

def _operator_operands(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]

    if isinstance(node, ast.BoolOp):
        return list(node.values)

    if isinstance(node, ast.Compare):
        return [node.left, *node.comparators]

    return []

def _explicit_precedence_children(context: file_context) -> list[ast.AST]:
    """Return the operands that need explicit parentheses (shared by check/fix)."""

    children: list[ast.AST] = []

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

            if (child_precedence is None) or (child_precedence == parent_precedence):
                continue

            child_start = context.ast_start(child)
            child_end = context.ast_end(child)

            if child_start[0] != child_end[0]:
                continue

            if is_parenthesized(context, child):
                continue

            children.append(child)

    return children

def check_explicit_precedence(context: file_context, config: "style_config") -> list[issue_record]:
    """Report nested operators of different precedence without parentheses."""

    issues: list[issue_record] = []

    for child in _explicit_precedence_children(context):
        child_start = context.ast_start(child)
        issues.append(_issue("explicit-precedence", child_start[0], child_start[1] + 1, "Mixed operators need explicit parentheses."))

    return issues

def _blank_runs(rows: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []

    for row in rows:
        if runs and (row == (runs[-1][-1] + 1)):
            runs[-1].append(row)
        else:
            runs.append([row])

    return runs

def _body_entries(context: file_context) -> list[tuple[ast.AST, list[ast.stmt], bool]]:
    """Return (container, statements, header_followed_by_body) entries."""

    entries: list[tuple[ast.AST, list[ast.stmt], bool]] = [(context.tree, list(context.tree.body), False)]

    for node in context.nodes():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            entries.append((node, list(node.body), True))
        elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            entries.append((node, list(node.body), True))

            if node.orelse:
                entries.append((node, list(node.orelse), False))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            entries.append((node, list(node.body), True))
        elif isinstance(node, ast.Try) or (type(node).__name__ == "TryStar"):
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

def check_blank_lines_around_blocks(context: file_context, config: "style_config") -> list[issue_record]:
    """Report block headers or complete blocks without exactly one blank line."""

    issues: list[issue_record] = []

    for container, statements, header_check in _body_entries(context):
        if not statements:
            continue

        if isinstance(container, ast.Module) and (config.blank_line_scope == "nested"):
            continue

        if header_check:
            header_row = context.header_colon_row(container)
            first = statements[0]

            if (header_row is not None) and (first.lineno > header_row):
                blanks = context.blank_rows_between(header_row, first.lineno)

                if blanks:
                    issues.append(_issue("blank-lines-around-blocks", first.lineno, 1, "Block header must be followed immediately by its first " "statement (no blank line)."))

        for previous, current in zip(statements, statements[1:], strict = False):
            if not (is_compound_statement(previous) or is_compound_statement(current)):
                continue

            previous_end = previous.end_lineno
            current_start = current.lineno

            if current_start <= previous_end:
                continue

            blanks = context.blank_rows_between(previous_end, current_start)

            if not blanks:
                issues.append(_issue("blank-lines-around-blocks", current_start, 1, "Complete block statements need exactly one blank line before and after."))
                continue

            runs = _blank_runs(blanks)

            for run in runs:
                if len(run) > 1:
                    issues.append(_issue("blank-lines-around-blocks", run[1], 1, "Complete block statements need exactly one blank line " "before and after; found several."))
                    break

    return issues

_BRANCH_KEYWORDS = frozenset({"elif", "else", "except", "finally"})

def check_branch_adjacency(context: file_context, config: "style_config") -> list[issue_record]:
    """Report blank lines before elif/else/except/except*/finally."""

    issues: list[issue_record] = []

    for row in range(2, context.line_count + 1):
        line = context.line(row)
        stripped = line.lstrip(" \t")

        if not stripped:
            continue

        word = re.match(r"[A-Za-z_]+", stripped)

        if (word is None) or (word.group(0) not in _BRANCH_KEYWORDS):
            continue

        probe = row - 1

        while (probe >= 1) and context.is_full_comment(probe):
            probe -= 1

        if probe < 1:
            continue

        if context.is_blank_line(probe):
            issues.append(_issue("branch-adjacency", row, context.indent_width(row) + 1, f"Blank line before '{word.group(0)}'; branches must be adjacent to " "the previous branch."))

    return issues

def check_decorator_adjacency(context: file_context, config: "style_config") -> list[issue_record]:
    """Report blank lines between decorators and the decorated definition."""

    issues: list[issue_record] = []

    for node in context.nodes():
        decorators = getattr(node, "decorator_list", None)

        if not decorators:
            continue

        decorated_line = getattr(node, "lineno", 1)
        previous_end = decorators[0].end_lineno

        for decorator in decorators[1:]:
            blanks = context.blank_rows_between(previous_end, decorator.lineno)

            if blanks:
                issues.append(_issue("decorator-adjacency", decorator.lineno, 1, "No blank line between decorators."))

            previous_end = decorator.end_lineno

        blanks = context.blank_rows_between(previous_end, decorated_line)

        if blanks:
            issues.append(_issue("decorator-adjacency", decorated_line, 1, "No blank line between the decorator and the decorated definition."))

    return issues

_SNAKE_CASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ALL_CAPS_RE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_FRAMEWORK_CALLBACKS = frozenset({"setUp", "tearDown", "setUpClass", "tearDownClass", "do_GET", "do_HEAD", "do_OPTIONS", "do_POST", "do_PUT", "do_DELETE", "do_PATCH"})
_TYPING_CONSTRUCTORS = frozenset({"TypeVar", "NewType", "ParamSpec", "TypeVarTuple", "NamedTuple", "TypedDict", "namedtuple"})
_NAMED_TUPLE_BASES = frozenset({"NamedTuple", "TypedDict"})

def _pattern_allowed(name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if (pattern == name) or fnmatch.fnmatchcase(name, pattern):
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
        if isinstance(base, ast.Name) and (base.id in _NAMED_TUPLE_BASES):
            return True

        if isinstance(base, ast.Attribute) and (base.attr in _NAMED_TUPLE_BASES):
            return True

    return False

def _definition_is_exempt(context: file_context, definition: definition, config: "style_config") -> bool:
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

    if (definition.kind == "class") and isinstance(definition.node, ast.ClassDef):
        if _class_has_exempt_base(definition.node):
            return True

    if definition.kind in {"variable", "attribute"}:
        if _ALL_CAPS_RE.match(name):
            return True

        if _is_typing_constructor(definition.value):
            return True

        if _is_typing_constructor(definition.annotation):
            return True

        if ((definition.kind == "variable") and (definition.annotation is None) and definition.module_level and isinstance(definition.value, (ast.Name, ast.Attribute)) and context.path.endswith("__init__.py")):
            return True

    return False

def _naming_issues(context: file_context, config: "style_config") -> list[issue_record]:
    """Return lower_snake_case violations for every collected definition."""

    issues: list[issue_record] = []

    for defn in context.definitions():
        if _definition_is_exempt(context, defn, config):
            continue

        if _SNAKE_CASE_RE.match(defn.name):
            continue

        issues.append(_naming_issue(defn.name, defn.row, defn.column))

    issues.sort(key = lambda issue: (issue.line, issue.column))
    return issues

def _naming_issue(name: str, row: int, column: int) -> issue_record:
    suggestion = _camel_to_snake(name)
    return _issue("snake-case-definition", row, column, f"'{name}' is not lower_snake_case; use '{suggestion}'.")

def check_snake_case_definition(context: file_context, config: "style_config") -> list[issue_record]:
    """Report definition names that are not lower_snake_case."""

    return _naming_issues(context, config)

def check_compound_word(context: file_context, config: "style_config") -> list[issue_record]:
    """Report names that split a configured compound word."""

    lexicon = {word.lower() for word in config.compound_words if word}

    if not lexicon:
        return []

    issues: list[issue_record] = []

    for defn in _definitions(context):
        if _definition_is_exempt(context, defn, config):
            continue

        parts = defn.name.split("_")

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
        issues.append(_issue("compound-word", defn.row, defn.column, f"'{defn.name}' splits a compound word with an underscore; use " f"'{suggestion}'."))

    return issues

def check_docstring_comment(context: file_context, config: "style_config") -> list[issue_record]:
    """Report standalone string expressions used as comments."""

    allow_docstrings = config.docstrings != "ban"
    issues: list[issue_record] = []

    for node in context.nodes():
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue

        is_docstring = False
        parent = context.parent(node)

        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            is_docstring = bool(parent.body) and (parent.body[0] is node)

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

def check_comment_blank_line(context: file_context, config: "style_config") -> list[issue_record]:
    """Report a full-line comment followed by a blank line."""

    mode = config.comment_blank_line

    if mode == "off":
        return []

    issues: list[issue_record] = []

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

        issues.append(_issue("comment-blank-line", row, 1, "Comment line is followed by a blank line; attach the comment to the " "code it describes."))

    return issues

def check_snake_case_file(path: Path) -> list[issue_record]:
    """Report a Python file whose stem is not lower_snake_case."""

    stem = path.stem

    if stem.startswith("__") and stem.endswith("__"):
        return []

    if _SNAKE_CASE_RE.match(stem):
        return []

    return [_issue("snake-case-file", 1, 1, f"File name '{path.name}' is not lower_snake_case.")]

# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

CHECK_FUNCS: dict[str, Callable[[file_context, "style_config"], list[issue_record]]] = {"trailing-whitespace": check_trailing_whitespace, "final-blank-lines": check_final_blank_lines, "final-newline": check_final_newline, "indent-four-spaces": check_indent_four_spaces, "no-tabs": check_no_tabs, "no-consecutive-blank-lines": check_no_consecutive_blank_lines, "one-statement-per-line": check_one_statement_per_line, "single-line-statement": check_single_line_statement, "imports-at-top": check_imports_at_top, "imports-contiguous": check_imports_contiguous, "no-block-imports": check_no_block_imports, "no-dynamic-imports": check_no_dynamic_imports, "binary-operator-spacing": check_binary_operator_spacing, "equal-spacing": check_equal_spacing, "colon-spacing": check_colon_spacing, "no-trailing-comma": check_no_trailing_comma, "redundant-parens": check_redundant_parens, "explicit-precedence": check_explicit_precedence, "blank-lines-around-blocks": check_blank_lines_around_blocks, "branch-adjacency": check_branch_adjacency, "decorator-adjacency": check_decorator_adjacency, "snake-case-definition": check_snake_case_definition, "compound-word": check_compound_word, "docstring-comment": check_docstring_comment, "comment-blank-line": check_comment_blank_line}
