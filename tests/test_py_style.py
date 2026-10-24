# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for celestia_devtools.lint.py_style.

Every rule has at least one bad sample (the rule must fire) and one good sample
(the rule must stay silent). The remaining tests cover the source-model edge
cases (strings, encodings, CRLF, parse errors), configuration, inline
exemptions, the fixer safety invariants and the CLI/JSON contract.
"""

from __future__ import annotations
import ast
import json
import os
import sys
import tokenize
from pathlib import Path
import pytest
import inspect

# Allow running the tests against a source checkout without a pip
# install (PYTHONPATH-style); the CI venv install makes this a no-op.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.lint import py_style, py_style_rules  # noqa: E402
from celestia_devtools.lint.py_style_config import (config_error, style_config, load_config)  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write(tmp_path: Path, body: str, name: str = "sample.py") -> Path:
    target = tmp_path / name
    target.write_text(body, encoding = "utf-8")
    return target

def issues_for(tmp_path: Path, body: str, *, name: str = "sample.py", select = None, cfg: style_config | None = None, directory: bool = False):
    target = write(tmp_path, body, name)
    style = cfg if cfg is not None else style_config(root = tmp_path)
    paths = [str(tmp_path)] if directory else [str(target)]
    discovery = py_style.discover_files(paths, style)
    active = py_style.active_rule_ids(style, select or (), ())
    return py_style.run_checks(discovery, active, style)

def rule_hits(tmp_path: Path, body: str, rule: str, **kwargs) -> list[str]:
    result = issues_for(tmp_path, body, select = [rule], **kwargs)
    return [issue.rule for _shown, issue in result.issues]

def cli(capsys, *arguments: str) -> tuple[int, str, str]:
    code = py_style.main(list(arguments))
    captured = capsys.readouterr()
    return code, captured.out, captured.err

def make_symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink, or skip the test where the platform forbids it."""

    try:
        os.symlink(target, link, target_is_directory = target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform
        pytest.skip(f"symbolic links are not available here: {exc}")

# ---------------------------------------------------------------------------
# Rule: trailing-whitespace
# ---------------------------------------------------------------------------

def test_trailing_whitespace_bad(tmp_path):
    hits = rule_hits(tmp_path, "x = 1   \n", "trailing-whitespace")
    assert hits == ["trailing-whitespace"]

def test_trailing_whitespace_good(tmp_path):
    assert rule_hits(tmp_path, "x = 1\n", "trailing-whitespace") == []

def test_trailing_whitespace_multiline_string_exempt(tmp_path):
    body = 'x = """line with trailing spaces   \nsecond line\n"""\n'
    assert rule_hits(tmp_path, body, "trailing-whitespace") == []

# ---------------------------------------------------------------------------
# Rule: final-blank-lines
# ---------------------------------------------------------------------------

def test_final_blank_lines_bad(tmp_path):
    hits = rule_hits(tmp_path, "x = 1\n\n", "final-blank-lines")
    assert hits == ["final-blank-lines"]

def test_final_blank_lines_good(tmp_path):
    assert rule_hits(tmp_path, "x = 1\n", "final-blank-lines") == []

# ---------------------------------------------------------------------------
# Rule: final-newline
# ---------------------------------------------------------------------------

def test_final_newline_bad(tmp_path):
    assert rule_hits(tmp_path, "x = 1", "final-newline") == ["final-newline"]

def test_final_newline_good(tmp_path):
    assert rule_hits(tmp_path, "x = 1\n", "final-newline") == []

def test_final_newline_none_mode_bad(tmp_path):
    cfg = style_config(root = tmp_path, final_newline = "none")
    assert rule_hits(tmp_path, "x = 1\n", "final-newline", cfg = cfg) == ["final-newline"]

def test_final_newline_none_mode_good(tmp_path):
    cfg = style_config(root = tmp_path, final_newline = "none")
    assert rule_hits(tmp_path, "x = 1", "final-newline", cfg = cfg) == []

# ---------------------------------------------------------------------------
# Rule: indent-four-spaces
# ---------------------------------------------------------------------------

def test_indent_four_spaces_bad(tmp_path):
    body = "def f():\n   return 1\n"
    assert rule_hits(tmp_path, body, "indent-four-spaces") == ["indent-four-spaces"]

def test_indent_four_spaces_good(tmp_path):
    body = "def f():\n    if True:\n        return 1\n    return 2\n"
    assert rule_hits(tmp_path, body, "indent-four-spaces") == []

def test_indent_four_spaces_tab_counts_one_column(tmp_path):
    body = "def f():\n\treturn 1\n"
    assert rule_hits(tmp_path, body, "indent-four-spaces") == ["indent-four-spaces"]

def test_indent_four_spaces_multiline_string_exempt(tmp_path):
    assert rule_hits(tmp_path, 'x = """a\n b\n"""\n', "indent-four-spaces") == []

# ---------------------------------------------------------------------------
# Rule: no-tabs
# ---------------------------------------------------------------------------

def test_no_tabs_bad(tmp_path):
    assert rule_hits(tmp_path, "x\t= 1\n", "no-tabs") == ["no-tabs"]

def test_no_tabs_good(tmp_path):
    assert rule_hits(tmp_path, 'text = "a\tb"\n', "no-tabs") == []

# ---------------------------------------------------------------------------
# Rule: no-consecutive-blank-lines
# ---------------------------------------------------------------------------

def test_no_consecutive_blank_lines_bad(tmp_path):
    hits = rule_hits(tmp_path, "x = 1\n\n\ny = 2\n", "no-consecutive-blank-lines")
    assert hits == ["no-consecutive-blank-lines"]

def test_no_consecutive_blank_lines_good(tmp_path):
    assert rule_hits(tmp_path, "x = 1\n\ny = 2\n", "no-consecutive-blank-lines") == []

def test_no_consecutive_blank_lines_multiline_string_exempt(tmp_path):
    body = 'x = """a\n\n\nb"""\n'
    assert rule_hits(tmp_path, body, "no-consecutive-blank-lines") == []

# ---------------------------------------------------------------------------
# Rule: one-statement-per-line
# ---------------------------------------------------------------------------

def test_one_statement_per_line_semicolon_bad(tmp_path):
    body = "x = 1; y = 2\n"
    assert rule_hits(tmp_path, body, "one-statement-per-line") == ["one-statement-per-line"]

def test_one_statement_per_line_inline_block_bad(tmp_path):
    body = "if ready: start()\n"
    assert rule_hits(tmp_path, body, "one-statement-per-line") == ["one-statement-per-line"]

def test_one_statement_per_line_good(tmp_path):
    body = "x = 1\ny = 2\nif x:\n    y = 3\n"
    assert rule_hits(tmp_path, body, "one-statement-per-line") == []

# ---------------------------------------------------------------------------
# Rule: single-line-statement
# ---------------------------------------------------------------------------

def test_single_line_statement_bad(tmp_path):
    body = "value = (\n    1 + 2\n)\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == ["single-line-statement"]

def test_single_line_statement_good(tmp_path):
    body = "value = 1 + 2\nother = call(1, 2)\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_single_line_statement_multiline_string_exempt(tmp_path):
    body = 'value = """first\nsecond"""\n'
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_single_line_statement_multiline_decorator_bad(tmp_path):
    body = "@decorator(\n    option\n)\ndef f():\n    return 1\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == ["single-line-statement"]

def test_single_line_statement_multiline_decorator_allowed(tmp_path):
    cfg = style_config(root = tmp_path, allow_multiline_decorators = True)
    body = "@decorator(\n    option\n)\ndef f():\n    return 1\n"
    assert rule_hits(tmp_path, body, "single-line-statement", cfg = cfg) == []

def test_single_line_statement_multiline_string_argument_exempt(tmp_path):
    body = "def write(path, text):\n    return (path, text)\n\n\nvalue = write(\n    first,\n    \"\"\"line one\nline two\"\"\",\n    last,\n)\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_single_line_statement_multiline_fstring_argument_exempt(tmp_path):
    body = "value = call(\n    first,\n    f\"\"\"value {a + b}\nnext line\"\"\",\n    last,\n)\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_single_line_statement_plain_multiline_call_still_reported(tmp_path):
    body = "value = call(\n    first,\n    second,\n    last,\n)\n"
    assert rule_hits(tmp_path, body, "single-line-statement") == ["single-line-statement"]

# ---------------------------------------------------------------------------
# Rule: imports-at-top
# ---------------------------------------------------------------------------

def test_imports_at_top_bad(tmp_path):
    body = "x = 1\nimport os\n"
    assert rule_hits(tmp_path, body, "imports-at-top") == ["imports-at-top"]

def test_imports_at_top_good(tmp_path):
    body = '"""Docstring."""\n\nimport os\nimport sys\n\nx = os.sep\n'
    assert rule_hits(tmp_path, body, "imports-at-top") == []

# ---------------------------------------------------------------------------
# Rule: imports-contiguous
# ---------------------------------------------------------------------------

def test_imports_contiguous_bad(tmp_path):
    body = "import os\n\nimport sys\n"
    assert rule_hits(tmp_path, body, "imports-contiguous") == ["imports-contiguous"]

def test_imports_contiguous_good(tmp_path):
    body = "import os\nimport sys\n"
    assert rule_hits(tmp_path, body, "imports-contiguous") == []

# ---------------------------------------------------------------------------
# Rule: no-block-imports
# ---------------------------------------------------------------------------

def test_no_block_imports_bad(tmp_path):
    body = "def load():\n    import os\n    return os\n"
    assert rule_hits(tmp_path, body, "no-block-imports") == ["no-block-imports"]

def test_no_block_imports_good(tmp_path):
    body = "from typing import TYPE_CHECKING\n" "\n" "if TYPE_CHECKING:\n" "    from pathlib import Path\n" "\n" "try:\n" "    import json\n" "except ImportError:\n" "    json = None\n"
    assert rule_hits(tmp_path, body, "no-block-imports") == []

def test_no_block_imports_exemptions_can_be_disabled(tmp_path):
    cfg = style_config(root = tmp_path, allow_type_checking_imports = False, allow_optional_imports = False)
    body = "from typing import TYPE_CHECKING\n" "\n" "if TYPE_CHECKING:\n" "    from pathlib import Path\n" "\n" "try:\n" "    import json\n" "except ImportError:\n" "    json = None\n"
    assert rule_hits(tmp_path, body, "no-block-imports", cfg = cfg) == ["no-block-imports", "no-block-imports"]

# ---------------------------------------------------------------------------
# Rule: no-dynamic-imports
# ---------------------------------------------------------------------------

def test_no_dynamic_imports_bad(tmp_path):
    body = 'import importlib\n\nmodule = importlib.import_module("os")\n'
    assert rule_hits(tmp_path, body, "no-dynamic-imports") == ["no-dynamic-imports"]

def test_no_dynamic_imports_aliases_bad(tmp_path):
    body = 'from importlib import import_module as load\n\nmodule = load("os")\n'
    assert rule_hits(tmp_path, body, "no-dynamic-imports") == ["no-dynamic-imports"]

def test_no_dynamic_imports_good(tmp_path):
    body = 'import importlib\n\nname = "os"\nmodule = __builtins__\n'
    assert rule_hits(tmp_path, body, "no-dynamic-imports") == []

def test_no_dynamic_imports_getattr_not_flagged(tmp_path):
    body = 'import importlib\n\nloader = getattr(importlib, "import_module")\n'
    assert rule_hits(tmp_path, body, "no-dynamic-imports") == []

# ---------------------------------------------------------------------------
# Rule: acyclic-imports (engine-level)
# ---------------------------------------------------------------------------

def _package(tmp_path: Path, files: dict[str, str], name: str = "pkg") -> Path:
    root = tmp_path / name
    root.mkdir()

    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents = True, exist_ok = True)
        target.write_text(body, encoding = "utf-8")

    return root

def test_acyclic_imports_cycle_detected(tmp_path):
    package = _package(tmp_path, {"__init__.py": "", "a.py": "from pkg import b\n", "b.py": "from pkg import a\n"})
    style = style_config(root = tmp_path)
    discovery = py_style.discover_files([str(package)], style)
    active = py_style.active_rule_ids(style, ["acyclic-imports"], ())
    result = py_style.run_checks(discovery, active, style)
    hits = [(issue.rule, issue.message) for _shown, issue in result.issues]
    assert len(hits) == 1
    assert hits[0][0] == "acyclic-imports"
    assert "cycle" in hits[0][1].lower()

def test_acyclic_imports_linear_graph_clean(tmp_path):
    package = _package(tmp_path, {"__init__.py": "", "a.py": "import os\n", "b.py": "from pkg import a\n"})
    style = style_config(root = tmp_path)
    discovery = py_style.discover_files([str(package)], style)
    active = py_style.active_rule_ids(style, ["acyclic-imports"], ())
    result = py_style.run_checks(discovery, active, style)
    assert result.issues == []

def test_acyclic_imports_relative_cycle_detected(tmp_path):
    package = _package(tmp_path, {"__init__.py": "", "a.py": "from . import b\n", "b.py": "from . import a\n"})
    style = style_config(root = tmp_path)
    discovery = py_style.discover_files([str(package)], style)
    active = py_style.active_rule_ids(style, ["acyclic-imports"], ())
    result = py_style.run_checks(discovery, active, style)
    assert [issue.rule for _shown, issue in result.issues] == ["acyclic-imports"]

# ---------------------------------------------------------------------------
# Rule: binary-operator-spacing
# ---------------------------------------------------------------------------

def test_acyclic_imports_only_on_directory_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    write(package, "", "__init__.py")
    write(package, "from pkg import b\n", "a.py")
    write(package, "from pkg import a\n", "b.py")
    code, out, err = cli(capsys, "check", "pkg/a.py", "pkg/b.py", "--no-config", "--select", "acyclic-imports", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    code, out, err = cli(capsys, "check", "pkg", "--no-config", "--select", "acyclic-imports", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["acyclic-imports"]

def test_binary_operator_spacing_bad(tmp_path):
    body = "x = 1+2\ny = 2**8\n"
    assert rule_hits(tmp_path, body, "binary-operator-spacing") == ["binary-operator-spacing", "binary-operator-spacing"]

def test_binary_operator_spacing_good(tmp_path):
    body = "x = 1 + 2\n" "y = 2 ** 8\n" "z = -x\n" "w = call(*items, **mapping)\n" "v = x @ y\n" "u = x is not None\n" "t = x not in items\n"
    assert rule_hits(tmp_path, body, "binary-operator-spacing") == []

def test_binary_operator_spacing_double_spaces_bad(tmp_path):
    assert rule_hits(tmp_path, "x = 1  +  2\n", "binary-operator-spacing") == ["binary-operator-spacing"]

# ---------------------------------------------------------------------------
# Rule: equal-spacing
# ---------------------------------------------------------------------------

def test_equal_spacing_bad(tmp_path):
    body = "def f(a=1):\n    return a\n\nf(a=1)\n"
    hits = rule_hits(tmp_path, body, "equal-spacing")
    assert hits == ["equal-spacing", "equal-spacing"]

def test_equal_spacing_good(tmp_path):
    body = "def f(a = 1, b = 2):\n    return a + b\n\nf(a = 1, b = 2)\n"
    assert rule_hits(tmp_path, body, "equal-spacing") == []

def test_equal_spacing_lambda_defaults_bad(tmp_path):
    body = "fn = lambda value=1: value\n"
    assert rule_hits(tmp_path, body, "equal-spacing") == ["equal-spacing"]

# ---------------------------------------------------------------------------
# Rule: colon-spacing
# ---------------------------------------------------------------------------

def test_colon_spacing_bad(tmp_path):
    body = 'mapping = {"key":1}\nnumber :int = 1\n'
    assert rule_hits(tmp_path, body, "colon-spacing") == ["colon-spacing", "colon-spacing"]

def test_colon_spacing_good(tmp_path):
    body = 'mapping = {"key": 1}\nnumber: int = 1\nfn = lambda value: value\n'
    assert rule_hits(tmp_path, body, "colon-spacing") == []

def test_colon_spacing_fstring_format_spec_clean(tmp_path):
    body = 'a = f"{value:>10}"\nb = f"{value!r:>10}"\n'
    assert rule_hits(tmp_path, body, "colon-spacing") == []

def test_colon_spacing_slice_pep8_default_clean(tmp_path):
    assert rule_hits(tmp_path, "part = items[1:2]\n", "colon-spacing") == []

def test_colon_spacing_spaced_slice_mode(tmp_path):
    cfg = style_config(root = tmp_path, slice_colons = "spaced")
    body = "part = items[1:2]\nother = items[1: 2]\n"
    assert rule_hits(tmp_path, body, "colon-spacing", cfg = cfg) == ["colon-spacing"]

# ---------------------------------------------------------------------------
# Rule: no-trailing-comma
# ---------------------------------------------------------------------------

def test_no_trailing_comma_bad(tmp_path):
    body = "values = [1, 2,]\ncall(1,)\n"
    hits = rule_hits(tmp_path, body, "no-trailing-comma")
    assert hits == ["no-trailing-comma", "no-trailing-comma"]

def test_no_trailing_comma_good(tmp_path):
    body = "values = [1, 2]\ncall(1, 2)\n"
    assert rule_hits(tmp_path, body, "no-trailing-comma") == []

def test_no_trailing_comma_single_element_tuple_exempt(tmp_path):
    body = "pair = (1,)\nother = call((1,))\nitem = items[1,]\n"
    assert rule_hits(tmp_path, body, "no-trailing-comma") == []

def test_no_trailing_comma_parenthesized_import_bad(tmp_path):
    body = "from os import (sep,)\n"
    assert rule_hits(tmp_path, body, "no-trailing-comma") == ["no-trailing-comma"]

# ---------------------------------------------------------------------------
# Rule: redundant-parens
# ---------------------------------------------------------------------------

def test_redundant_parens_bad(tmp_path):
    body = "x = (1 + 2)\n"
    assert rule_hits(tmp_path, body, "redundant-parens") == ["redundant-parens"]

def test_redundant_parens_nested_bad(tmp_path):
    body = "x = ((1))\n"
    assert rule_hits(tmp_path, body, "redundant-parens") == ["redundant-parens"]

def test_redundant_parens_good(tmp_path):
    body = "x = (1, 2)\ny = (item for item in items)\nz = 1 + 2\nw = (1 + 2) * 3\n"
    assert rule_hits(tmp_path, body, "redundant-parens") == []

def test_redundant_parens_multiline_ignored(tmp_path):
    body = "x = (\n    1 + 2\n)\n"
    assert rule_hits(tmp_path, body, "redundant-parens") == []

# ---------------------------------------------------------------------------
# Rule: explicit-precedence
# ---------------------------------------------------------------------------

def test_explicit_precedence_bad(tmp_path):
    body = "x = 1 + 2 * 3\ny = a and b == c\n"
    hits = rule_hits(tmp_path, body, "explicit-precedence")
    assert hits == ["explicit-precedence", "explicit-precedence"]

def test_explicit_precedence_good(tmp_path):
    body = "x = (1 + 2) * 3\n" "y = (a and (b == c)) or d\n" "z = a < b < c\n" "w = 2 ** 3 ** 4\n" "v = not ready\n" "u = -value\n"
    assert rule_hits(tmp_path, body, "explicit-precedence") == []

# ---------------------------------------------------------------------------
# Rule: blank-lines-around-blocks
# ---------------------------------------------------------------------------

def test_blank_lines_around_blocks_missing_bad(tmp_path):
    body = "def f():\n    return 1\nx = 2\n"
    assert rule_hits(tmp_path, body, "blank-lines-around-blocks") == ["blank-lines-around-blocks"]

def test_blank_lines_around_blocks_extra_bad(tmp_path):
    body = "def f():\n    return 1\n\n\nx = 2\n"
    assert rule_hits(tmp_path, body, "blank-lines-around-blocks") == ["blank-lines-around-blocks"]

def test_blank_lines_around_blocks_header_blank_bad(tmp_path):
    body = "def f():\n\n    return 1\n"
    assert rule_hits(tmp_path, body, "blank-lines-around-blocks") == ["blank-lines-around-blocks"]

def test_blank_lines_around_blocks_good(tmp_path):
    body = "def f():\n    if True:\n        return 1\n\n    return 2\n\nx = 2\n"
    assert rule_hits(tmp_path, body, "blank-lines-around-blocks") == []

def test_blank_lines_around_blocks_nested_scope_skips_module(tmp_path):
    cfg = style_config(root = tmp_path, blank_line_scope = "nested")
    body = "def f():\n    return 1\n\n\nx = 2\n"
    assert rule_hits(tmp_path, body, "blank-lines-around-blocks", cfg = cfg) == []

# ---------------------------------------------------------------------------
# Rule: branch-adjacency
# ---------------------------------------------------------------------------

def test_branch_adjacency_bad(tmp_path):
    body = "if ready:\n    start()\n\nelse:\n    stop()\n"
    assert rule_hits(tmp_path, body, "branch-adjacency") == ["branch-adjacency"]

def test_branch_adjacency_good(tmp_path):
    body = "if ready:\n" "    start()\n" "else:\n" "    stop()\n" "\n" "try:\n" "    run()\n" "except ValueError:\n" "    pass\n" "finally:\n" "    cleanup()\n"
    assert rule_hits(tmp_path, body, "branch-adjacency") == []

# ---------------------------------------------------------------------------
# Rule: decorator-adjacency
# ---------------------------------------------------------------------------

def test_decorator_adjacency_bad(tmp_path):
    body = "@first\n\ndef f():\n    return 1\n"
    assert rule_hits(tmp_path, body, "decorator-adjacency") == ["decorator-adjacency"]

def test_decorator_adjacency_good(tmp_path):
    body = "@first\n@second\ndef f():\n    return 1\n"
    assert rule_hits(tmp_path, body, "decorator-adjacency") == []

# ---------------------------------------------------------------------------
# Rule: snake-case-definition
# ---------------------------------------------------------------------------

def test_snake_case_definition_bad(tmp_path):
    body = "class Worker:\n" "    def runTask(self, SomeArg = 1):\n" "        self.someAttr = SomeArg\n" "        return self.someAttr\n"
    hits = rule_hits(tmp_path, body, "snake-case-definition")
    assert hits == ["snake-case-definition", "snake-case-definition", "snake-case-definition", "snake-case-definition"]

def test_snake_case_definition_good(tmp_path):
    body = "class worker:\n" "    def run_task(self, some_arg = 1):\n" "        self.some_attr = some_arg\n" "        return self.some_attr\n"
    assert rule_hits(tmp_path, body, "snake-case-definition") == []

def test_snake_case_definition_exemptions_good(tmp_path):
    body = "from typing import NamedTuple, TYPE_CHECKING, TypeVar\n" "\n" "MAX_SIZE = 10\n" 'Item = TypeVar("Item")\n' "\n" "\n" "class Point(NamedTuple):\n" "    x: int\n" "    y: int\n" "\n" "\n" "class handler:\n" "    def setUp(self):\n" "        self._value = 1\n" "\n" "    def do_GET(self):\n" "        return self._value\n" "\n" "    def do_HEAD(self):\n" "        return self._value\n" "\n" "    def do_OPTIONS(self):\n" "        return self._value\n"
    assert rule_hits(tmp_path, body, "snake-case-definition") == []

def test_snake_case_definition_underscore_constants_exempt(tmp_path):
    body = "_FOO = 1\n" "MAX_SIZE = 1\n" "_MAX_SIZE = 1\n" "__X__ = 1\n" "_TYPE_NAME: int = 1\n" "\n" "\n" "class worker:\n" "    _CLASS_CONST = 1\n" "    __PRIVATE_CONST = 2\n" "\n" "    def run(self):\n" "        return self._CLASS_CONST\n"
    assert rule_hits(tmp_path, body, "snake-case-definition") == []

def test_snake_case_definition_underscore_camel_is_still_bad(tmp_path):
    body = "_maxSize = 1\n" "_mixed_CASE = 2\n" "lowerCase = 3\n" "\n" "\n" "class worker:\n" "    def run(self):\n" "        self._camelAttr = 1\n" "        return self._camelAttr\n"
    hits = rule_hits(tmp_path, body, "snake-case-definition")
    assert hits == ["snake-case-definition", "snake-case-definition", "snake-case-definition", "snake-case-definition"]

def test_snake_case_definition_named_tuple_classes_still_exempt(tmp_path):
    body = "from typing import NamedTuple, TypedDict\n" "\n" "\n" "class Step(NamedTuple):\n" "    label: str\n" "    index: int\n" "\n" "\n" "class StepMap(TypedDict):\n" "    label: str\n"
    assert rule_hits(tmp_path, body, "snake-case-definition") == []

def test_snake_case_definition_init_reexport_exempt(tmp_path):
    body = "from .thing import thing\n\nThing = thing\n"
    hits = rule_hits(tmp_path, body, "snake-case-definition", name = "__init__.py")
    assert hits == []

# ---------------------------------------------------------------------------
# Rule: snake-case-file
# ---------------------------------------------------------------------------

def test_snake_case_file_bad(tmp_path):
    result = issues_for(tmp_path, "x = 1\n", name = "BadName.py", select = ["snake-case-file"], directory = True)
    assert [issue.rule for _shown, issue in result.issues] == ["snake-case-file"]

def test_snake_case_file_good(tmp_path):
    result = issues_for(tmp_path, "x = 1\n", name = "_helpers.py", select = ["snake-case-file"], directory = True)
    assert result.issues == []

def test_snake_case_file_not_checked_for_explicit_file(tmp_path):
    result = issues_for(tmp_path, "x = 1\n", name = "BadName.py", select = ["snake-case-file"])
    assert result.issues == []

def test_snake_case_file_dunder_exempt(tmp_path):
    result = issues_for(tmp_path, "", name = "__init__.py", select = ["snake-case-file"], directory = True)
    assert result.issues == []

# ---------------------------------------------------------------------------
# Rule: compound-word
# ---------------------------------------------------------------------------

def test_naming_helper_defined_once():
    assert inspect.getsource(py_style_rules).count("def _naming_issues") == 1

def test_compound_word_bad(tmp_path):
    cfg = style_config(root = tmp_path, compound_words = ("textbox",))
    hits = rule_hits(tmp_path, "text_box = 1\n", "compound-word", cfg = cfg)
    assert hits == ["compound-word"]

def test_compound_word_good(tmp_path):
    cfg = style_config(root = tmp_path, compound_words = ("textbox",))
    body = "textbox_value = 1\nunrelated_name = 2\n"
    assert rule_hits(tmp_path, body, "compound-word", cfg = cfg) == []

def test_compound_word_inactive_without_lexicon(tmp_path):
    assert rule_hits(tmp_path, "text_box = 1\n", "compound-word") == []

# ---------------------------------------------------------------------------
# Rule: docstring-comment
# ---------------------------------------------------------------------------

def test_docstring_comment_bad(tmp_path):
    body = 'x = 1\n"""Not a docstring."""\n'
    assert rule_hits(tmp_path, body, "docstring-comment") == ["docstring-comment"]

def test_docstring_comment_good(tmp_path):
    body = '"""Module docstring."""\n\nx = 1\n'
    assert rule_hits(tmp_path, body, "docstring-comment") == []

def test_docstring_comment_ban_mode_flags_docstrings(tmp_path):
    cfg = style_config(root = tmp_path, docstrings = "ban")
    body = '"""Module docstring."""\n\nx = 1\n'
    assert rule_hits(tmp_path, body, "docstring-comment", cfg = cfg) == ["docstring-comment"]

# ---------------------------------------------------------------------------
# Rule: comment-blank-line
# ---------------------------------------------------------------------------

def test_comment_blank_line_bad(tmp_path):
    body = "x = 1\n\n# note\n\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line") == ["comment-blank-line"]

def test_comment_blank_line_good(tmp_path):
    body = "x = 1\n# note\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line") == []

def test_comment_blank_line_section_header_exempt(tmp_path):
    body = "x = 1\n\n# ------ section ------\n\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line") == []

def test_comment_blank_line_file_header_exempt(tmp_path):
    body = "# File header comment\n\nx = 1\n"
    assert rule_hits(tmp_path, body, "comment-blank-line") == []

def test_comment_blank_line_block_tail_exempt(tmp_path):
    body = "def f():\n    x = 1\n    # end of block\n\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line") == []

def test_comment_blank_line_all_mode_flags_sections(tmp_path):
    cfg = style_config(root = tmp_path, comment_blank_line = "all")
    body = "x = 1\n\n# ------ section ------\n\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line", cfg = cfg) == ["comment-blank-line"]

def test_comment_blank_line_off_mode_silent(tmp_path):
    cfg = style_config(root = tmp_path, comment_blank_line = "off")
    body = "x = 1\n\n# note\n\ny = 2\n"
    assert rule_hits(tmp_path, body, "comment-blank-line", cfg = cfg) == []

# ---------------------------------------------------------------------------
# Filesystem safety: symlinks are never followed and never written through
# ---------------------------------------------------------------------------

def test_discovery_skips_symlinked_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    outside = write(tmp_path, "x = 1   \n", "outside.py")
    scan = tmp_path / "scan"
    scan.mkdir()
    make_symlink_or_skip(outside, scan / "linked.py")
    write(scan, "y = 1\n", "normal.py")
    code, out, err = cli(capsys, "check", "scan", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["summary"]["checked"] == 1
    assert payload["summary"]["skipped"] == 1
    assert payload["issues"] == []
    code, out, err = cli(capsys, "fix", "scan", "--no-config")
    assert code == 0
    assert outside.read_text(encoding = "utf-8") == "x = 1   \n"

def test_fix_does_not_follow_symlinked_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    target = write(outside_dir, "x = 1+2\n", "bad.py")
    scan = tmp_path / "scan"
    scan.mkdir()
    make_symlink_or_skip(outside_dir, scan / "linked_dir")
    normal = write(scan, "y = 1+2\n", "bad_normal.py")
    code, out, err = cli(capsys, "fix", "scan", "--no-config")
    assert code == 0
    assert normal.read_text(encoding = "utf-8") == "y = 1 + 2\n"
    assert target.read_text(encoding = "utf-8") == "x = 1+2\n"

def test_symlinked_path_argument_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "x = 1+2\n", "target.py")
    link = tmp_path / "link.py"
    make_symlink_or_skip(target, link)
    code, out, err = cli(capsys, "check", "link.py", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["summary"]["checked"] == 0
    assert target.read_text(encoding = "utf-8") == "x = 1+2\n"

def test_write_refusal_reason_rejects_unsafe_targets(tmp_path):
    real = write(tmp_path, "x = 1\n", "real.py")
    assert py_style._write_refusal_reason(real, [tmp_path]) is None
    link = tmp_path / "link.py"
    make_symlink_or_skip(real, link)
    assert py_style._write_refusal_reason(link, [tmp_path]) == "path is a symbolic link"
    directory = tmp_path / "nested"
    directory.mkdir()
    make_symlink_or_skip(directory, tmp_path / "linked_dir")
    through = tmp_path / "linked_dir" / "file.py"
    through.write_text("x = 1\n", encoding = "utf-8")
    assert (py_style._write_refusal_reason(through, [tmp_path]) == "path resolves through a symbolic link")
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    assert py_style._write_refusal_reason(real, [other_root]) == "path is outside the scanned roots"

# ---------------------------------------------------------------------------
# Whole-file sample used by the cross-rule tests
# ---------------------------------------------------------------------------

FULL_STYLE_SAMPLE = '''"""Module docstring."""

import os
import sys
from pathlib import Path

class worker:
    """A worker."""

    def __init__(self, name = "x", count = 1):
        self.name = name
        self.count = count

    def run(self):
        with open(self.name) as handle:
            if handle:
                data = handle.read()

        return data

def helper(value = 1):
    """Helper."""

    if value:
        return value

    result = {"a": 1, "b": 2}
    numbers = [1, 2, 3]
    pair = (1, 2)
    single = (1,)
    text = "a  b"
    returned = helper(value = 2)
    total = value + (value * 2)
    valid = (value is not None) and (not isinstance(value, str) or (not 1 <= value <= 128))
    return result, numbers, pair, single, text, returned, total, valid
'''

def test_full_style_sample_is_clean(tmp_path):
    result = issues_for(tmp_path, FULL_STYLE_SAMPLE, name = "full_style.py", directory = True)
    assert [(issue.rule, issue.line) for _shown, issue in result.issues] == []

# ---------------------------------------------------------------------------
# f-strings are opaque on every supported Python version
#
# Python 3.11 tokenizes an f-string as one STRING token; Python 3.12+ (PEP 701)
# tokenizes FSTRING_START/FSTRING_MIDDLE/FSTRING_END plus the expression tokens.
# Every finding whose position falls inside that span is suppressed, so these
# tests must pass unchanged on 3.11 and 3.12/3.13.
# ---------------------------------------------------------------------------

def test_multiline_fstring_plain_not_a_multiline_statement(tmp_path):
    body = 'x = f"""line one\nline two\n"""\n'
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_multiline_fstring_with_expression_not_a_multiline_statement(tmp_path):
    body = 'x = f"""value {a + b}\nnext line\n"""\n'
    assert rule_hits(tmp_path, body, "single-line-statement") == []

def test_multiline_fstring_content_trailing_whitespace_exempt(tmp_path):
    body = 'x = f"""line one   \nline two"""\n'
    assert rule_hits(tmp_path, body, "trailing-whitespace") == []

def test_fstring_expression_binary_operator_not_reported(tmp_path):
    body = 'x = f"{a+b}"\n'
    assert rule_hits(tmp_path, body, "binary-operator-spacing") == []

def test_fstring_expression_binary_operator_in_multiline_fstring_not_reported(tmp_path):
    body = 'x = f"""total {a+b}\nnext line\n"""\n'
    assert rule_hits(tmp_path, body, "binary-operator-spacing") == []

def test_fstring_expression_dict_colon_not_reported(tmp_path):
    body = "x = f\"{ {'k':1} }\"\n"
    assert rule_hits(tmp_path, body, "colon-spacing") == []

def test_fstring_expression_keyword_equals_not_reported(tmp_path):
    body = 'x = f"{call(value=1)}"\n'
    assert rule_hits(tmp_path, body, "equal-spacing") == []

def test_fstring_expression_precedence_not_reported(tmp_path):
    body = 'x = f"{a + b * c}"\n'
    assert rule_hits(tmp_path, body, "explicit-precedence") == []

def test_fstring_expression_redundant_parens_not_reported(tmp_path):
    body = 'x = f"{call((a))}"\n'
    assert rule_hits(tmp_path, body, "redundant-parens") == []

def test_fstring_expression_tab_not_reported(tmp_path):
    body = 'x = f"{a\t+ b}"\n'
    assert rule_hits(tmp_path, body, "no-tabs") == []

def test_plain_code_same_constructs_still_reported(tmp_path):
    body = 'plain = a+b\nmapping = {"k":1}\ncaller = call(value=1)\nmixed = a + b * c\nwrapped = (a)\n'
    assert rule_hits(tmp_path, body, "binary-operator-spacing") == ["binary-operator-spacing"]
    assert rule_hits(tmp_path, body, "colon-spacing") == ["colon-spacing"]
    assert rule_hits(tmp_path, body, "equal-spacing") == ["equal-spacing"]
    assert rule_hits(tmp_path, body, "explicit-precedence") == ["explicit-precedence"]
    assert rule_hits(tmp_path, body, "redundant-parens") == ["redundant-parens"]

def test_fstring_string_token_prefix_detection():
    # The 3.11 tokenizer gives one STRING token for every f-string; the prefix
    # detector is what makes the opacity work on 3.11 (CI) as well as 3.12+.
    def string_token(text):
        return tokenize.TokenInfo(tokenize.STRING, text, (1, 0), (1, len(text)), "")

    assert py_style_rules._is_fstring_token(string_token('f"x"'))
    assert py_style_rules._is_fstring_token(string_token("f'x'"))
    assert py_style_rules._is_fstring_token(string_token('f"""x"""'))
    assert py_style_rules._is_fstring_token(string_token('fr"x"'))
    assert py_style_rules._is_fstring_token(string_token('Rf"x"'))
    assert not py_style_rules._is_fstring_token(string_token('"x"'))
    assert not py_style_rules._is_fstring_token(string_token('rb"x"'))
    assert not py_style_rules._is_fstring_token(string_token('u"x"'))

def test_check_and_fix_have_no_unclearable_fstring_finding(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, 'value = f"{a+b}"  \n', "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert target.read_text(encoding = "utf-8") == 'value = f"{a+b}"\n'
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

# ---------------------------------------------------------------------------
# Source model / formatting edge cases
# ---------------------------------------------------------------------------

def test_empty_file_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "", "empty.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["summary"]["checked"] == 1
    assert payload["issues"] == []

def test_crlf_source_is_clean(tmp_path):
    target = tmp_path / "crlf.py"
    target.write_bytes(b"x = 1\r\n\r\nif x:\r\n    y = 2\r\n")
    style = style_config(root = tmp_path)
    discovery = py_style.discover_files([str(target)], style)
    active = py_style.active_rule_ids(style, (), ())
    result = py_style.run_checks(discovery, active, style)
    assert [issue.rule for _shown, issue in result.issues] == []

def test_non_utf8_file_is_checked_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "latin.py").write_bytes(b"x = 1  # caf\xe9\n")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert "not valid UTF-8" in payload["warnings"][0]
    assert payload["summary"]["checked"] == 1

def test_parse_error_is_reported_as_single_issue(tmp_path):
    body = "def broken(:\n    pass\n"
    result = issues_for(tmp_path, body)
    rules = [issue.rule for _shown, issue in result.issues]
    assert rules == ["parse-error"]
    assert result.issues[0][1].line >= 1

def test_parse_error_does_not_stop_other_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "def broken(:\n", "broken.py")
    write(tmp_path, "x = 1   \n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    rules = {issue["rule"] for issue in payload["issues"]}
    assert code == 1
    assert "parse-error" in rules
    assert "trailing-whitespace" in rules

def test_unicode_line_keeps_ast_columns_aligned(tmp_path):
    body = 'def f(a = "\u4e2d\u6587", b=2):\n    return a\n'
    hits = rule_hits(tmp_path, body, "equal-spacing")
    assert hits == ["equal-spacing"]

def test_multiline_string_content_is_untouched_by_check(tmp_path):
    body = 'x = """first line   \nsecond line\n"""\n'
    result = issues_for(tmp_path, body)
    assert result.issues == []
    assert "first line   " in (tmp_path / "sample.py").read_text(encoding = "utf-8")

def test_ast_equivalent_rewrite_validation_accepts_whitespace_only():
    original = "x = 1+2\n"
    rewritten = "x = 1 + 2\n"
    assert ast.dump(ast.parse(original), include_attributes = False) == ast.dump(ast.parse(rewritten), include_attributes = False)
    assert py_style._validate_rewrite(original, rewritten) is None

def test_rewrite_validation_rejects_ast_change():
    reason = py_style._validate_rewrite("x = 1\n", "x = 2\n")
    assert reason == "the rewrite would change the AST"

def test_rewrite_validation_rejects_string_token_change():
    # "a" "b" and "ab" have the same AST (implicit concatenation) but different
    # string token multisets, which the fixer invariant forbids.
    reason = py_style._validate_rewrite('x = "a" "b"\n', 'x = "ab"\n')
    assert reason == "the rewrite would change string literal tokens"

def test_rewrite_validation_rejects_unparseable_result():
    reason = py_style._validate_rewrite("x = 1\n", "x = (\n")
    assert reason is not None
    assert "does not parse" in reason

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_config_select_from_standalone_toml(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'select = ["trailing-whitespace"]\n', "py-style.toml")
    write(tmp_path, "x = 1; y = 2\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

def test_config_ignore_from_standalone_toml(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'ignore = ["trailing-whitespace"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

def test_config_unknown_key_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "bogus = true\n", "py-style.toml")
    write(tmp_path, "x = 1\n", "code.py")
    code, out, err = cli(capsys, "check", ".")
    assert code == 2
    assert "unknown key" in err

def test_config_unknown_rule_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'select = ["no-such-rule"]\n', "py-style.toml")
    code, out, err = cli(capsys, "check", ".")
    assert code == 2
    assert "unknown rule id" in err

def test_config_invalid_enum_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'final_newline = "two"\n', "py-style.toml")
    code, out, err = cli(capsys, "check", ".")
    assert code == 2
    assert "final_newline" in err

def test_config_exempt_table_suppresses_and_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config_body = "[[exempt]]\n" 'path = "generated.py"\n' 'rules = ["trailing-whitespace"]\n' 'reason = "generated file"\n'
    write(tmp_path, config_body, "py-style.toml")
    write(tmp_path, "x = 1   \n", "generated.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["summary"]["exempted"] == 1
    assert payload["summary"]["findings"] == 0

def test_config_exempt_requires_reason(tmp_path):
    write(tmp_path, '[[exempt]]\npath = "generated.py"\nrules = ["trailing-whitespace"]\n', "py-style.toml")

    with pytest.raises(config_error):
        load_config(paths = [str(tmp_path)])

def test_config_exclude_glob_counts_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'exclude = ["skip_me.py"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \n", "skip_me.py")
    write(tmp_path, "x = 1   \n", "kept.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["summary"]["checked"] == 1
    assert payload["summary"]["skipped"] == 1
    assert {issue["path"] for issue in payload["issues"]} == {"kept.py"}

def test_config_py_style_toml_beats_pyproject(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'ignore = ["no-tabs"]\n', "py-style.toml")
    write(tmp_path, '[tool.py_style]\nignore = ["trailing-whitespace"]\n', "pyproject.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    # py-style.toml wins: trailing-whitespace is active, no-tabs ignored.
    assert code == 1
    assert payload["config"].endswith("py-style.toml")
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]

def test_config_pyproject_section_is_used(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, '[tool.py_style]\nignore = ["trailing-whitespace"]\n', "pyproject.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["config"].endswith("pyproject.toml")

def test_config_no_config_uses_builtin_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'ignore = ["trailing-whitespace"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["config"] == "built-in defaults"
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]

def test_config_explicit_path_overrides_repo_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'ignore = ["trailing-whitespace"]\n', "py-style.toml")
    write(tmp_path, 'ignore = ["no-tabs"]\n', "custom.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--config", "custom.toml", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["config"].endswith("custom.toml")
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]

def test_config_severity_override_promotes_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, '[severity]\nexplicit-precedence = "error"\n', "py-style.toml")
    write(tmp_path, "x = 1 + 2 * 3\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--select", "explicit-precedence", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["issues"][0]["severity"] == "error"

def test_config_missing_file_fails(tmp_path):
    with pytest.raises(config_error):
        load_config(paths = [str(tmp_path)], explicit = str(tmp_path / "missing.toml"))

# ---------------------------------------------------------------------------
# Inline exemption directives
# ---------------------------------------------------------------------------

def test_inline_directive_same_line_exempts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2  # py-style-ok: binary-operator-spacing: matches the layout table\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "binary-operator-spacing", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["summary"]["exempted"] == 1
    assert payload["warnings"] == []

def test_inline_directive_previous_line_exempts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: trailing-whitespace: aligned table\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["warnings"] == []

def test_inline_directive_wildcard_exempts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: *: generated block\nx = 1+2\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "binary-operator-spacing", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["warnings"] == []

def test_inline_directive_without_reason_does_not_exempt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: trailing-whitespace:\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert len(payload["warnings"]) == 1
    assert "without a reason" in payload["warnings"][0]
    assert payload["summary"]["diagnostics"] == 1

def test_inline_directive_without_reason_no_colon_does_not_exempt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: trailing-whitespace\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert len(payload["warnings"]) == 1
    assert "without a reason" in payload["warnings"][0]
    assert payload["summary"]["diagnostics"] == 1

def test_inline_directive_missing_reason_prints_text_diagnostic(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: *\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace")
    assert code == 1
    assert "py-style: warning:" in err
    assert "without a reason" in err
    assert "py-style: code.py:2:6: trailing-whitespace:" in err

def test_inline_directive_with_reason_has_no_diagnostic(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: *: intentional here\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["warnings"] == []
    assert payload["summary"]["exempted"] == 1

def test_inline_directive_unknown_rule_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok: made-up-rule: because\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert len(payload["warnings"]) == 1
    assert "unknown rule id" in payload["warnings"][0]

# ---------------------------------------------------------------------------
# File-level exemption directives
# ---------------------------------------------------------------------------

def test_file_directive_with_reason_exempts_whole_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: *: generated lookup table\nx = 1   \n# note\there\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace,no-tabs", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["summary"]["exempted"] == 2

def test_file_directive_limits_rules(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: trailing-whitespace: generated table\nx = 1+2   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace,binary-operator-spacing", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["binary-operator-spacing"]
    assert payload["summary"]["exempted"] == 1

def test_file_directive_without_reason_does_not_exempt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: trailing-whitespace\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert payload["summary"]["exempted"] == 0
    assert payload["summary"]["diagnostics"] == 1
    assert "py-style-ok-file directive without a reason" in payload["warnings"][0]

def test_file_directive_unknown_rule_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: made-up-rule: because\nx = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert payload["summary"]["exempted"] == 0
    assert "unknown rule id" in payload["warnings"][0]

def test_file_directive_after_code_is_not_a_file_directive(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n# py-style-ok-file: *: late directive\nx = 2   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert payload["warnings"] == []

def test_file_directive_after_docstring_is_not_a_file_directive(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, '"""Module docstring."""\n\n# py-style-ok-file: *: generated\nx = 1   \n', "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert payload["warnings"] == []

def test_file_directive_covers_engine_rule(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: *: generated scaffold\nx = 1\n", "BadName.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "snake-case-file", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["summary"]["exempted"] == 1

def test_file_directive_merges_multiple_header_directives(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: trailing-whitespace: generated table\n" "# py-style-ok-file: no-tabs: generated table\n" "x = 1   \n" "# note\there\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace,no-tabs", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    assert payload["summary"]["exempted"] == 2

def test_file_directive_does_not_exempt_parse_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "# py-style-ok-file: *: generated\n", "broken.py")
    (tmp_path / "broken.py").write_text("# py-style-ok-file: *: generated\ndef broken(:\n    pass\n", encoding = "utf-8")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["parse-error"]
    assert payload["summary"]["exempted"] == 0

def test_fix_respects_file_directive(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "# py-style-ok-file: trailing-whitespace: generated table\nx = 1+2   \n", "code.py")
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert (target.read_text(encoding = "utf-8") == "# py-style-ok-file: trailing-whitespace: generated table\nx = 1 + 2   \n")

# ---------------------------------------------------------------------------
# Fix engine
# ---------------------------------------------------------------------------

def test_fix_applies_safe_rewrites(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "x = 1+2\n", "code.py")
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert target.read_text(encoding = "utf-8") == "x = 1 + 2\n"
    assert "fixed 1 file(s)" in err
    assert "checked 1 file(s)" in err

def test_fix_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "x = 1+2\n", "code.py")
    code, out, err = cli(capsys, "fix", ".", "--no-config", "--dry-run")
    assert code == 0
    assert target.read_text(encoding = "utf-8") == "x = 1+2\n"
    assert "would fix 1 file(s)" in err

def test_check_never_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "x = 1+2  \n"
    target = write(tmp_path, body, "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config")
    assert code == 1
    assert target.read_text(encoding = "utf-8") == body

def test_fix_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "def f( a=1 ):\n    return  [1, 2,]   \n\n\ndef g():\n    return (\n        3\n    )\n", "code.py")
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    first = target.read_bytes()
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert target.read_bytes() == first

def test_fix_single_line_statement_merges(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "value = (\n    1 + 2\n)\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "value = 1 + 2\n"

def test_fix_preserves_single_element_tuple(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "pair = (1,)\nindexed = items[1,]\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == body

def test_fix_final_newline_none_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'final_newline = "none"\n', "py-style.toml")
    target = write(tmp_path, "x = 1\n", "code.py")
    assert cli(capsys, "fix", ".")[0] == 0
    assert target.read_bytes() == b"x = 1"

def test_fix_preserves_crlf(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "code.py"
    target.write_bytes(b"x = 1  \r\n")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_bytes() == b"x = 1\r\n"

def test_fix_skips_parse_error_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    broken = write(tmp_path, "def broken(:\n    pass\n", "broken.py")
    original = broken.read_bytes()
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert broken.read_bytes() == original
    assert "skipped broken.py" in err

def test_fix_branch_and_decorator_adjacency(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "if ready:\n" "    start()\n" "\n" "else:\n" "    stop()\n" "\n" "\n" "@decorator\n" "\n" "def helper():\n" "    return 1\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    fixed = target.read_text(encoding = "utf-8")
    assert "    start()\nelse:\n" in fixed
    assert "@decorator\ndef helper():" in fixed

def test_fix_single_line_statement_spacing_preserved(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Regression: joining Black-style multi-line calls must keep the token gaps
    # that the line breaks provided (", False", '"s" if', "> 1 else", ", last").
    body = "results.append(\n" "    call(\n" "        value,\n" "        False,\n" '        "failed",\n' '        "install failed ({})".format(count, "s" if count > 1 else ""),\n' "    )\n" ")\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    fixed = target.read_text(encoding = "utf-8")
    assert fixed == ('results.append(call(value, False, "failed", "install failed ({})".format(' 'count, "s" if count > 1 else "")))\n')
    compile(fixed, "<fixed>", "exec")

def test_fix_merge_keeps_builtin_call_spacing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "result = max(1,2)\nother = min(\n    3,\n    4 \n)\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    fixed = target.read_text(encoding = "utf-8")
    assert "other = min(3, 4)" in fixed
    assert "result = max(1,2)" in fixed  # plain argument commas are not a rule

def test_fix_redundant_nested_parens_converges(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "x = ((1))\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "x = 1\n"

def test_fix_select_limits_rewrites(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "x = 1+2   \n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config", "--select", "trailing-whitespace")[0] == 0
    assert target.read_text(encoding = "utf-8") == "x = 1+2\n"

def test_fix_rewrite_keeps_string_data(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'text = """first   \nsecond"""  \n'
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    fixed = target.read_text(encoding = "utf-8")
    assert "first   " in fixed
    assert fixed.endswith('second"""\n')

def test_fix_refuses_too_many_files_without_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    targets = []

    for index in range(51):
        targets.append(write(tmp_path, "x = 1+2\n", "bad_%02d.py" % index))

    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 2
    assert "refusing to rewrite 51 file(s)" in err
    assert "--allow-many" in err
    assert all(target.read_text(encoding = "utf-8") == "x = 1+2\n" for target in targets)

def test_fix_allow_many_flag_applies_many_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    targets = []

    for index in range(51):
        targets.append(write(tmp_path, "x = 1+2\n", "bad_%02d.py" % index))

    code, out, err = cli(capsys, "fix", ".", "--no-config", "--allow-many")
    assert code == 0
    assert "fixed 51 file(s)" in err
    assert all(target.read_text(encoding = "utf-8") == "x = 1 + 2\n" for target in targets)

def test_fix_batch_guard_only_counts_rewritten_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    for index in range(51):
        write(tmp_path, "x = 1\n", "clean_%02d.py" % index)

    target = write(tmp_path, "x = 1+2\n", "bad.py")
    code, out, err = cli(capsys, "fix", ".", "--no-config")
    assert code == 0
    assert target.read_text(encoding = "utf-8") == "x = 1 + 2\n"

def test_fix_config_error_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "bogus = true\n", "py-style.toml")
    write(tmp_path, "x = 1\n", "code.py")
    code, out, err = cli(capsys, "fix", ".")
    assert code == 2

# ---------------------------------------------------------------------------
# single-line-statement: merge statements that contain single-line f-strings
# ---------------------------------------------------------------------------

def test_fix_single_line_merges_single_line_fstring_call(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'value = call(\n    f"{a + b}",\n    other,\n)\n'
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == 'value = call(f"{a + b}", other)\n'

def test_fix_single_line_merges_parenthesized_fstring(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'text = (\n    f"value {a + b}"\n)\n'
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == 'text = f"value {a + b}"\n'

def test_fix_single_line_preserves_fstring_expression_text(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'value = call(\n    f"{a+b}",\n)\n'
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == 'value = call(f"{a+b}")\n'

def test_fix_single_line_skips_multiline_fstring(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'value = call(\n    f"""line one\nline two""",\n    other,\n)\n'
    target = write(tmp_path, body, "code.py")
    assert (cli(capsys, "fix", ".", "--no-config", "--select", "single-line-statement")[0] == 0)
    assert target.read_text(encoding = "utf-8") == body

def test_fix_single_line_fstring_merge_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = 'first = call(\n    f"{a} {b}",\n    other,\n)\nsecond = other_call(\n    f"{c}",\n)\n'
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    first = target.read_bytes()
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_bytes() == first
    assert ast.dump(ast.parse(body), include_attributes = False) == ast.dump(ast.parse(first.decode("utf-8")), include_attributes = False)

# ---------------------------------------------------------------------------
# blank-lines-around-blocks: insert the missing blank line
# ---------------------------------------------------------------------------

def test_fix_blank_lines_inserts_missing_at_module_top(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "def f():\n    return 1\nx = 2\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "def f():\n    return 1\n\nx = 2\n"

def test_fix_blank_lines_inserts_in_nested_body(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "def outer():\n    x = 1\n    if x:\n        y = 2\n    return x\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == ("def outer():\n    x = 1\n\n    if x:\n        y = 2\n\n    return x\n")

def test_fix_blank_lines_inserts_before_attached_comment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "def f():\n    return 1\n# about x\nx = 2\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == ("def f():\n    return 1\n\n# about x\nx = 2\n")

def test_fix_blank_lines_does_not_insert_between_header_and_body(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "def f():\n\n    return 1\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "def f():\n    return 1\n"

def test_fix_blank_lines_does_not_pad_first_or_last_statement(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "x = 1\ny = 2\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == body

def test_fix_blank_lines_deletes_extra_and_inserts_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "def f():\n    return 1\n\n\nx = 2\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "def f():\n    return 1\n\nx = 2\n"

def test_fix_blank_lines_idempotent_and_ast_equal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "def outer():\n    x = 1\n    if x:\n        y = 2\n    return x\nz = 1\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    first = target.read_bytes()
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_bytes() == first
    assert ast.dump(ast.parse(body), include_attributes = False) == ast.dump(ast.parse(first.decode("utf-8")), include_attributes = False)

def test_fix_blank_lines_respects_nested_scope_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'blank_line_scope = "nested"\n', "py-style.toml")
    target = write(tmp_path, "def f():\n    return 1\nz = 1\n", "code.py")
    assert cli(capsys, "fix", ".")[0] == 0
    assert target.read_text(encoding = "utf-8") == "def f():\n    return 1\nz = 1\n"

def test_fix_blank_lines_then_check_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "def f():\n    return 1\nx = 2\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "blank-lines-around-blocks", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

# ---------------------------------------------------------------------------
# explicit-precedence: insert the missing parentheses
# ---------------------------------------------------------------------------

def test_fix_explicit_precedence_simple(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "value = 1 + 2 * 3\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "value = 1 + (2 * 3)\n"

def test_fix_explicit_precedence_boolop_compare(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "valid = a and b == c\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "valid = a and (b == c)\n"

def test_fix_explicit_precedence_nested_same_close(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = write(tmp_path, "value = a + b * c == d\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == "value = (a + (b * c)) == d\n"

def test_fix_explicit_precedence_exceptions_untouched(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "chained = a < b < c\n" "simple_not = not ready\n" "power = 2 ** 3 ** 4\n" "same = a + b + c\n" "wrapped = 1 + (2 * 3)\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_text(encoding = "utf-8") == body

def test_fix_explicit_precedence_idempotent_and_ast_equal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    body = "first = 1 + 2 * 3\nsecond = a and b == c\n"
    target = write(tmp_path, body, "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    first = target.read_bytes()
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    assert target.read_bytes() == first
    assert ast.dump(ast.parse(body), include_attributes = False) == ast.dump(ast.parse(first.decode("utf-8")), include_attributes = False)

def test_fix_explicit_precedence_removes_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "value = a + b * c\n", "code.py")
    assert cli(capsys, "fix", ".", "--no-config")[0] == 0
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "explicit-precedence", "--json")
    payload = json.loads(out)
    assert payload["issues"] == []

def test_list_rules_marks_explicit_precedence_fixable(capsys):
    code, out, err = cli(capsys, "list-rules", "--json")
    payload = json.loads(out)
    rule = next(entry for entry in payload["rules"] if entry["id"] == "explicit-precedence")
    assert rule["fixable"] is True

def test_fix_order_contains_explicit_precedence():
    order = list(py_style_rules.FIX_ORDER)
    assert "explicit-precedence" in order
    assert order.index("explicit-precedence") < order.index("redundant-parens")

# ---------------------------------------------------------------------------
# CLI: usage, exit codes and output routing
# ---------------------------------------------------------------------------

def test_cli_no_arguments_prints_usage_and_exits_2(capsys):
    code, out, err = cli(capsys)
    assert code == 2
    assert "usage:" in err
    assert out == ""

def test_cli_help_exits_0(capsys):
    code, out, err = cli(capsys, "-h")
    assert code == 0
    assert "usage:" in out

def test_cli_unknown_command_exits_2(capsys):
    code, out, err = cli(capsys, "nope")
    assert code == 2
    assert err

def test_cli_unknown_option_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, out, err = cli(capsys, "check", "--bogus")
    assert code == 2

def test_check_clean_exit_0_and_summary_format(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n", "good.py")
    code, out, err = cli(capsys, "check", ".", "--no-config")
    assert code == 0
    assert ("py-style: checked 1 file(s), 0 issue(s) (0 error(s), 0 warning(s)) - 27 rule(s)") in err

def test_check_violation_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2\n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config")
    assert code == 1
    assert "py-style: bad.py:1:6: binary-operator-spacing:" in err

def test_warn_only_exits_0_on_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2\n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--warn-only")
    assert code == 0
    assert "binary-operator-spacing" in err

def test_warn_only_still_exits_2_on_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "bogus = true\n", "py-style.toml")
    code, out, err = cli(capsys, "check", ".", "--warn-only")
    assert code == 2

def test_missing_path_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, out, err = cli(capsys, "check", "does-not-exist")
    assert code == 2
    assert "path not found" in err

def test_unknown_select_rule_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n", "good.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "made-up")
    assert code == 2
    assert "unknown rule id" in err

def test_negative_max_issues_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n", "good.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--max-issues", "-1")
    assert code == 2

def test_select_and_ignore_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2  \n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]
    assert payload["summary"]["warnings"] == 0
    code, out, err = cli(capsys, "check", ".", "--no-config", "--ignore", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert {issue["rule"] for issue in payload["issues"]} == {"binary-operator-spacing"}

def test_cli_ignore_wins_over_cli_select(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--ignore", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--ignore", "trailing-whitespace")
    assert code == 0
    assert " - 0 rule(s)" in err

def test_select_two_rules_ignore_one_keeps_the_other(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \nif True:\n\ty = 2\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--select", "no-tabs", "--ignore", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["no-tabs"]

def test_config_ignore_still_applies_with_cli_select(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'ignore = ["trailing-whitespace"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

def test_cli_select_overrides_config_select(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'select = ["no-tabs"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \nif True:\n\ty = 2\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]

def test_select_comma_separated_equals_repeated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \n# note\there\n", "code.py")
    code_one, out_one, _ = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace,no-tabs", "--json")
    code_two, out_two, _ = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--select", "no-tabs", "--json")
    payload_one = json.loads(out_one)
    payload_two = json.loads(out_two)
    assert code_one == code_two == 1
    assert payload_one["issues"] == payload_two["issues"]
    assert {issue["rule"]for issue in payload_one["issues"]} == {"trailing-whitespace", "no-tabs"}

def test_ignore_comma_separated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \n# note\there\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--ignore", "trailing-whitespace,no-tabs", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["issues"] == []

def test_select_star_runs_every_rule(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "*")
    assert code == 1
    assert " - 27 rule(s)" in err

def test_ignore_star_disables_every_rule(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--ignore", "*")
    assert code == 0
    assert " - 0 rule(s)" in err

def test_select_comma_with_unknown_rule_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace,made-up-rule")
    assert code == 2
    assert "unknown rule id" in err

def test_config_select_star_is_allowed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, 'select = ["*"]\n', "py-style.toml")
    write(tmp_path, "x = 1   \n", "code.py")
    code, out, err = cli(capsys, "check", ".", "--json")
    payload = json.loads(out)
    assert code == 1
    assert [issue["rule"] for issue in payload["issues"]] == ["trailing-whitespace"]

def test_json_contract_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2\n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 1
    assert set(payload) == {"tool", "config", "summary", "issues", "warnings"}
    assert payload["tool"] == "py-style"
    assert payload["config"] == "built-in defaults"
    assert set(payload["summary"]) == {"checked", "skipped", "findings", "errors", "warnings", "exempted", "diagnostics", "truncated"}
    assert payload["summary"]["checked"] == 1
    assert payload["summary"]["findings"] == 1
    assert payload["summary"]["errors"] == 1
    assert payload["summary"]["truncated"] is False
    issue = payload["issues"][0]
    assert set(issue) == {"path", "line", "column", "rule", "severity", "message"}
    assert issue["path"] == "bad.py"
    assert issue["severity"] == "error"
    assert err == ""

def test_json_issues_are_sorted(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "b = 1+2\nc = 3+4\n", "z_file.py")
    write(tmp_path, "a = 5+6\n", "a_file.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    keys = [(issue["path"], issue["line"], issue["column"], issue["rule"])for issue in payload["issues"]]
    assert keys == sorted(keys)

def test_json_truncated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "a = 1+2\nb = 3+4\nc = 5+6\n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--max-issues", "1", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["summary"]["findings"] == 3
    assert payload["summary"]["truncated"] is True
    assert len(payload["issues"]) == 1

def test_quiet_keeps_only_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "a = 1+2\nb = 3   \n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--quiet")
    assert code == 1
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("py-style: checked 1 file(s)")

def test_build_package_directory_is_scanned(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    write(package, "", "__init__.py")
    build_package = package / "build"
    build_package.mkdir()
    write(build_package, "", "__init__.py")
    write(build_package, "x = 1   \n", "bad.py")
    artifact = tmp_path / "repo" / "build"
    artifact.mkdir(parents = True)
    write(artifact, "y = 1   \n", "output.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--select", "trailing-whitespace", "--json")
    payload = json.loads(out)
    paths = {issue["path"] for issue in payload["issues"]}
    assert code == 1
    assert "pkg/build/bad.py" in paths
    assert all("repo/build" not in path for path in paths)
    assert payload["summary"]["skipped"] >= 1

def test_check_default_path_is_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1\n", "good.py")
    code, out, err = cli(capsys, "check", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["summary"]["checked"] == 1

def test_check_multiple_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    write(first, "x = 1\n", "a.py")
    write(second, "y = 2+3\n", "b.py")
    code, out, err = cli(capsys, "check", "one", "two", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["summary"]["checked"] == 2

def test_skipped_directory_counts_once(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    write(target, "x = 1+2\n", "ignored.py")
    write(tmp_path, "x = 1\n", "kept.py")
    code, out, err = cli(capsys, "check", ".", "--no-config", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["summary"]["checked"] == 1
    assert payload["summary"]["skipped"] == 1

def test_summary_output_is_ascii(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "x = 1+2\n", "bad.py")
    code, out, err = cli(capsys, "check", ".", "--no-config")
    assert err.isascii()
    assert out.isascii()

def test_fix_help_exits_0(capsys):
    code, out, err = cli(capsys, "fix", "--help")
    assert code == 0
    assert "--dry-run" in out
    assert "Exit code 0 after applying safe fixes" in out

# ---------------------------------------------------------------------------
# list-rules
# ---------------------------------------------------------------------------

def test_list_rules_text_contains_catalog(capsys):
    code, out, err = cli(capsys, "list-rules")
    assert code == 0
    assert out.isascii()
    assert "27 check rule(s)" in out

    for rule in py_style_rules.CHECK_RULES:
        assert rule.id in out

    assert "parse-error" in out
    assert "P02" in out
    assert "P09/P43" in out
    assert "P31" in out
    assert "P46" in out

def test_list_rules_json_catalog_complete(capsys):
    code, out, err = cli(capsys, "list-rules", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["tool"] == "py-style"
    listed = {rule["id"] for rule in payload["rules"]}
    assert listed == set(py_style_rules.CHECK_RULE_IDS)
    implemented = set(py_style_rules.CHECK_FUNCS) | set(py_style_rules.ENGINE_RULE_IDS)
    assert implemented == listed

    for rule in payload["rules"]:
        assert rule["p_refs"]
        assert rule["severity"] in {"error", "warning"}
        assert isinstance(rule["fixable"], bool)
        assert rule["description"]

def test_list_rules_json_manual_entries(capsys):
    code, out, err = cli(capsys, "list-rules", "--json")
    payload = json.loads(out)
    manual = {entry["id"]: entry for entry in payload["manual"]}
    assert set(manual) == {"P02", "P09/P43", "P31", "P46"}
    assert manual["P02"]["p_refs"] == ["P02"]
    assert manual["P09/P43"]["p_refs"] == ["P09", "P43"]
    assert all(entry["description"] for entry in manual.values())

def test_list_rules_json_builtin_parse_error(capsys):
    code, out, err = cli(capsys, "list-rules", "--json")
    payload = json.loads(out)
    assert [entry["id"] for entry in payload["builtin"]] == ["parse-error"]
    assert payload["builtin"][0]["severity"] == "error"
    assert payload["builtin"][0]["fixable"] is False

def test_list_rules_does_not_need_a_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "bogus = true\n", "py-style.toml")
    code, out, err = cli(capsys, "list-rules")
    assert code == 0
