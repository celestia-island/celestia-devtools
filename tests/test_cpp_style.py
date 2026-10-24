#!/usr/bin/env python3
"""Tests for the cpp-style checker: tokenizer, structure, rules, config, CLI.

The tokenizer tests pin the lexical traps from the spec (comments, strings, raw
strings, character literals, digit separators, preprocessor continuations and
nesting, CRLF/BOM, invalid UTF-8). The rule tests each have a bad and a good
sample, and the CLI tests pin exit codes, the JSON schema, exemptions, config
fail-closed behaviour, the third-party skip and the read-only guarantee.
"""

from __future__ import annotations
import json
import textwrap
import time
from pathlib import Path
import pytest
from celestia_devtools.lint import cpp_style
from celestia_devtools.lint.cpp import tokenizer as cpp_tokenizer
from celestia_devtools.lint.cpp.config import (DEFAULT_ALLOW_SPACE_BEFORE_PAREN, ConfigError, load_config)
from celestia_devtools.lint.cpp.rules import RULE_IDS, RULES
from celestia_devtools.lint.cpp.structure import analyze_bytes
from celestia_devtools.lint.cpp.tokenizer import (CHAR, COMMENT, IDENT, NEWLINE, NUMBER, OP, PREPROC, STRING)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cpp_style"

# --------------------------------------------------------------------------- helpers

def write_files(root: Path, files: dict) -> None:
    """Write test sources into *root* (dedent so tests can stay readable)."""

    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding = "utf-8")

def run_cli(capsys, argv):
    code = cpp_style.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err

def run_json(capsys, argv):
    code, out, err = run_cli(capsys, argv)
    payload = json.loads(out) if out.strip() else None
    return code, payload, err

def rule_ids(payload):
    return [issue["rule"] for issue in payload["issues"]]

def issues_for(payload, rule):
    return [issue for issue in payload["issues"] if issue["rule"] == rule]

def make_repo_root(root: Path) -> None:
    """Give *root* a repository marker so config discovery stops there."""
    (root / ".git").mkdir(exist_ok = True)

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path

@pytest.fixture()
def check_style(workspace, capsys):
    """Write files and run the checker on them with defaults (or extra flags)."""

    def run(files: dict, extra = (), json_mode: bool = True, args = ("check", ".")):
        write_files(workspace, files)
        argv = [*args, "--no-config", *extra]

        if json_mode:
            argv.append("--json")
            return run_json(capsys, argv)

        return run_cli(capsys, argv)

    return run

# --------------------------------------------------------------------------- tokenizer

class TestTokenizer:
    def test_basic_tokens_and_positions(self):
        result = cpp_tokenizer.tokenize("int value = 1;\n")
        assert [(token.kind, token.text)for token in result.code] == [(IDENT, "int"), (IDENT, "value"), (OP, "="), (NUMBER, "1"), (OP, ";")]
        first = result.code[0]
        assert (first.line, first.col, first.end_line, first.end_col) == (1, 1, 1, 4)
        assert result.code[1].col == 5
        assert result.significant[-1].kind == NEWLINE

    def test_line_comment_is_trivia(self):
        result = cpp_tokenizer.tokenize("int x; // note\nint y;\n")
        comments = [token for token in result.tokens if token.kind == COMMENT]
        assert len(comments) == 1
        assert comments[0].text == "// note"
        assert all(token.kind != COMMENT for token in result.significant)
        assert [token.text for token in result.code] == ["int", "x", ";", "int", "y", ";"]

    def test_line_comment_backslash_continues(self):
        result = cpp_tokenizer.tokenize("// first \\\n// second\nint x;\n")
        comments = [token for token in result.tokens if token.kind == COMMENT]
        assert len(comments) == 1
        assert comments[0].end_line == 2
        assert "second" in comments[0].text
        assert result.code[0].line == 3

    def test_block_comment_spans_lines(self):
        result = cpp_tokenizer.tokenize("/* one\ntwo */\nint x;\n")
        comments = [token for token in result.tokens if token.kind == COMMENT]
        assert len(comments) == 1
        assert (comments[0].line, comments[0].end_line) == (1, 2)
        assert result.code[0].line == 3

    def test_block_comment_does_not_nest(self):
        result = cpp_tokenizer.tokenize("/* outer /* inner */ int x;\n")
        comments = [token for token in result.tokens if token.kind == COMMENT]
        assert comments[0].text == "/* outer /* inner */"
        assert [token.text for token in result.code] == ["int", "x", ";"]

    def test_string_ignores_comment_markers(self):
        result = cpp_tokenizer.tokenize('auto a = "// not"; auto b = "/* nope */";\n')
        strings = [token.text for token in result.code if token.kind == STRING]
        assert strings == ['"// not"', '"/* nope */"']

    def test_string_with_escaped_quote(self):
        result = cpp_tokenizer.tokenize('auto s = "a\\"b";\n')
        strings = [token.text for token in result.code if token.kind == STRING]
        assert strings == ['"a\\"b"']

    def test_adjacent_string_literals(self):
        result = cpp_tokenizer.tokenize('auto s = "a" "b";\n')
        assert [token.text for token in result.code if token.kind == STRING] == ['"a"', '"b"']

    def test_string_prefixes(self):
        source = 'auto a = u8"x"; auto b = L"x"; auto c = u"x"; auto d = U"x";\n'
        result = cpp_tokenizer.tokenize(source)
        strings = [token.text for token in result.code if token.kind == STRING]
        assert strings == ['u8"x"', 'L"x"', 'u"x"', 'U"x"']

    def test_char_literals(self):
        source = r"char a = 'a'; char b = '\''; char c = '\\'; char d = '\n';" + "\n"
        result = cpp_tokenizer.tokenize(source)
        chars = [token.text for token in result.code if token.kind == CHAR]
        assert chars == [r"'a'", r"'\''", r"'\\'", r"'\n'"]

    def test_digit_separators_are_numbers(self):
        result = cpp_tokenizer.tokenize("int a = 1'000; int b = 0xFF'FF; char c = '0';\n")
        numbers = [token.text for token in result.code if token.kind == NUMBER]
        chars = [token.text for token in result.code if token.kind == CHAR]
        assert numbers == ["1'000", "0xFF'FF"]
        assert chars == ["'0'"]

    def test_number_forms(self):
        source = "auto a = 1u; auto b = 1.5f; auto c = 0x1p-3; auto d = .5; auto e = 1e+5;\n"
        result = cpp_tokenizer.tokenize(source)
        numbers = [token.text for token in result.code if token.kind == NUMBER]
        assert numbers == ["1u", "1.5f", "0x1p-3", ".5", "1e+5"]

    def test_raw_string(self):
        result = cpp_tokenizer.tokenize('auto s = R"(a"b)c)";\n')
        strings = [token.text for token in result.code if token.kind == STRING]
        assert strings == ['R"(a"b)c)"']

    def test_raw_string_custom_delimiter(self):
        result = cpp_tokenizer.tokenize('auto s = R"xy(a )" b)xy";\n')
        strings = [token.text for token in result.code if token.kind == STRING]
        assert strings == ['R"xy(a )" b)xy"']

    def test_raw_string_multiline(self):
        source = 'auto s = R"(line one\nline two)";\nint x;\n'
        result = cpp_tokenizer.tokenize(source)
        strings = [token for token in result.code if token.kind == STRING]
        assert len(strings) == 1
        assert (strings[0].line, strings[0].end_line) == (1, 2)
        identifiers = [token.line for token in result.code if token.kind == IDENT]
        assert identifiers[-1] == 3

    def test_invalid_raw_delimiter_falls_back_to_string(self):
        result = cpp_tokenizer.tokenize('auto s = R"abcdefghijklmnopq(x)y";\n')
        kinds = [token.kind for token in result.code]
        assert IDENT in kinds
        assert STRING in kinds

    def test_preprocessor_directive(self):
        result = cpp_tokenizer.tokenize("#include <vector>\nint x;\n")
        preproc = [token for token in result.tokens if token.kind == PREPROC]
        assert len(preproc) == 1
        assert preproc[0].text == "#include <vector>"
        assert [token.text for token in result.code] == ["int", "x", ";"]

    def test_preprocessor_continuation(self):
        source = "#define SUM(a, b) \\\n    ((a) + (b))\nint x;\n"
        result = cpp_tokenizer.tokenize(source)
        preproc = [token for token in result.tokens if token.kind == PREPROC]
        assert len(preproc) == 1
        assert preproc[0].end_line == 2
        assert result.code[0].line == 3

    def test_preprocessor_nesting_depths(self):
        source = "#if A\n#ifdef B\n#else\n#endif\n#endif\nint x;\n"
        result = cpp_tokenizer.tokenize(source)
        assert [(directive.name, directive.depth)for directive in result.directives] == [("if", 1), ("ifdef", 2), ("else", 2), ("endif", 2), ("endif", 1)]

    def test_unterminated_conditional_warns(self):
        result = cpp_tokenizer.tokenize("#if A\nint x;\n")
        assert any("unterminated preprocessor conditional" in warning for warning in result.warnings)

    def test_stray_endif_warns(self):
        result = cpp_tokenizer.tokenize("#endif\nint x;\n")
        assert any("stray #endif" in warning for warning in result.warnings)

    def test_template_angles_do_not_break_delimiters(self):
        source = "std::vector<std::pair<int, int>> values;\nvoid f(std::pair<int, int> p);\n"
        analyzed = analyze_bytes(Path("sample.cpp"), source.encode("utf-8"))
        assert not analyzed.warnings
        assert ">>" in [token.text for token in analyzed.code]
        assert len(analyzed.paren_match) == 2

    def test_crlf_and_bom(self):
        result = cpp_tokenizer.tokenize("\ufeffint x;\r\nint y;\r\n")
        lines = [token.line for token in result.code if token.kind == IDENT]
        assert lines == [1, 1, 2, 2]
        assert all("\r" not in token.text for token in result.tokens)

    def test_unterminated_block_comment_warns(self):
        result = cpp_tokenizer.tokenize("/* nope\nint x;\n")
        assert any("unterminated block comment" in warning for warning in result.warnings)
        assert result.code == []

    def test_unterminated_string_warns(self):
        result = cpp_tokenizer.tokenize('auto s = "nope;\nint x;\n')
        assert any("unterminated string literal" in warning for warning in result.warnings)
        assert any((token.kind == IDENT) and (token.text == "int") for token in result.code)

    def test_unterminated_char_warns(self):
        result = cpp_tokenizer.tokenize("char c = 'a;\nint x;\n")
        assert any("unterminated character literal" in warning for warning in result.warnings)

    def test_invalid_utf8_bytes_warn_and_replace(self):
        result = cpp_tokenizer.tokenize_bytes(b"int x; // \xff\n")
        assert any("invalid UTF-8" in warning for warning in result.warnings)
        assert "\ufffd" in result.text

    def test_significant_keeps_newlines_but_not_trivia(self):
        result = cpp_tokenizer.tokenize("#include <x>\nint a; // c\n")
        assert all(token.kind not in (COMMENT, PREPROC) for token in result.significant)
        assert any(token.kind == NEWLINE for token in result.significant)
        assert result.code == [token for token in result.significant if token.kind != NEWLINE]

    def test_operators_longest_match(self):
        source = "a->b; c >>= 1; d <=> e; template<int... Ns> void f();\n"
        result = cpp_tokenizer.tokenize(source)
        operators = [token.text for token in result.code if token.kind == OP]
        assert "->" in operators
        assert ">>=" in operators
        assert "<=>" in operators
        assert "..." in operators

    def test_line_comment_at_eof_is_not_unterminated(self):
        result = cpp_tokenizer.tokenize("int x; // trailing")
        assert not result.warnings
        assert result.code[-1].text == ";"

# --------------------------------------------------------------------------- structure

class TestStructure:
    def test_delimiter_matching_and_depth(self):
        source = "void f()\n{\n    if(x)\n    {\n        g();\n    }\n}\n"
        analyzed = analyze_bytes(Path("sample.cpp"), source.encode("utf-8"))
        assert not analyzed.warnings
        assert len(analyzed.paren_match) == 6
        assert len(analyzed.brace_match) == 4
        depths = [analyzed.brace_depth[position]for position, token in enumerate(analyzed.code)if token.text == "g"]
        assert depths == [2]

    def test_unmatched_delimiters_warn(self):
        analyzed = analyze_bytes(Path("sample.cpp"), b"int f());\n")
        assert any("unmatched" in warning for warning in analyzed.warnings)

    def test_blank_line_helpers(self):
        analyzed = analyze_bytes(Path("sample.cpp"), b"a\n\n\nb\n")
        assert analyzed.lines == ["a", "", "", "b"]
        assert analyzed.blank_lines_between(1, 4) == 2
        assert analyzed.is_blank_line(2)

    def test_defined_macros(self):
        source = "#define ASSERT(x) x\nvoid f()\n{\n    ASSERT (1);\n}\n"
        analyzed = analyze_bytes(Path("sample.cpp"), source.encode("utf-8"))
        assert analyzed.defined_macros == frozenset({"ASSERT"})

    def test_analysis_reports_invalid_utf8(self):
        analyzed = analyze_bytes(Path("sample.cpp"), b"int x; // \xff\n")
        assert any("invalid UTF-8" in warning for warning in analyzed.warnings)

    def test_code_token_indices_align_with_token_stream(self):
        analyzed = analyze_bytes(Path("sample.cpp"), b"/* c */ int x;\n")
        assert analyzed.code_token_indices == [1, 2, 3]

        for position, index in enumerate(analyzed.code_token_indices):
            assert analyzed.tokens[index].text == analyzed.code[position].text

# --------------------------------------------------------------------------- rules: A

class TestNoSpaceBeforeParen:
    RULE = "no-space-before-paren"

    def test_bad_function_signature(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "void update (int value);\n"}, extra = ("--select", self.RULE))
        assert code == 1
        assert rule_ids(payload) == [self.RULE]
        assert payload["issues"][0]["severity"] == "error"
        assert payload["issues"][0]["line"] == 1
        assert payload["issues"][0]["column"] == 6

    def test_bad_control_keyword(self, check_style):
        source = "void f()\n{\n    if (value)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert rule_ids(payload) == [self.RULE]

    def test_good_calls(self, check_style):
        source = "void update(int value);\n\nvoid f()\n{\n    if(value)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_builtin_exemptions(self, check_style):
        source = "void f()\n" "{\n" "    auto size = sizeof (int);\n" "    auto alignment = alignof (int);\n" "    auto copy = decltype (size)();\n" "    alignas (16) int value = 0;\n" "    static_assert (true);\n" "    auto pointer = new (std::nothrow) int;\n" "    delete pointer;\n" "    co_yield (value);\n" "    value = co_await (task);\n" "    if constexpr (true)\n" "    {\n" "    }\n" "    return (value);\n" "}\n" "void g() noexcept (true);\n" "void operator ()();\n" "__declspec (dllexport) void h();\n" "__attribute__ ((noreturn)) void k();\n" "void coroutine()\n" "{\n" "    co_return;\n" "}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_default_exemption_contract(self):
        assert set(DEFAULT_ALLOW_SPACE_BEFORE_PAREN) == {"return", "sizeof", "alignof", "decltype", "alignas", "static_assert", "noexcept", "constexpr", "operator", "new", "delete", "__declspec", "__attribute__", "co_await", "co_return", "co_yield"}

    def test_uppercase_macro_is_a_warning(self, check_style):
        source = "void f()\n{\n    ASSERT (1);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        issue = payload["issues"][0]
        assert issue["severity"] == "warning"
        assert "macro" in issue["message"]

    def test_defined_macro_is_a_warning(self, check_style):
        source = "#define assert(x) x\nvoid f()\n{\n    assert (1);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert payload["issues"][0]["severity"] == "warning"

    def test_macro_style_ignore_silences_macro_warnings(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'macro_style = "ignore"\n', "sample.cpp": "void f()\n{\n    ASSERT (1);\n    update (2);\n}\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 1
        messages = [issue["message"] for issue in payload["issues"]]
        assert len(messages) == 1
        assert "update" in messages[0]

    def test_custom_allow_space_before_paren(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'allow_space_before_paren = ["update"]\n', "sample.cpp": ("void update (int value);\nauto size = sizeof (int);\n")})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 0
        assert payload["issues"] == []

    def test_comment_between_name_and_paren_is_skipped(self, check_style):
        source = "void f()\n{\n    update /* note */ (1);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_preprocessor_between_name_and_paren_is_skipped(self, check_style):
        source = "void f()\n{\n    update\n#if 1\n    (1);\n#endif\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_line_break_before_paren_is_reported(self, check_style):
        source = "void f()\n{\n    update\n    (1);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert rule_ids(payload) == [self.RULE]

class TestForSemicolonSpacing:
    RULE = "for-semicolon-spacing"

    def test_bad_classic_for_header(self, check_style):
        source = "void f()\n{\n    for (int i = 0; i < 3; i++)\n    {\n        g(i);\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 2

    def test_good_for_header(self, check_style):
        source = "void f()\n{\n    for(int i = 0;i < 3;i++)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_range_for_is_not_checked(self, check_style):
        source = "void f()\n{\n    for(const auto &item : items)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_empty_for_header(self, check_style):
        source = "void f()\n{\n    for(;;)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_nested_calls_in_header_are_fine(self, check_style):
        source = "void f()\n{\n    for(int i = g(1, 2);i < n;i++)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_multiline_header_reports(self, check_style):
        source = "void f()\n{\n    for(int i = 0;\n        i < n;i++)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_malformed_header_is_skipped(self, check_style):
        source = "void f()\n{\n    for(int i = 0;i < n)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

class TestControlBraces:
    RULE = "control-braces"

    def test_bad_unbraced_if(self, check_style):
        source = "void f()\n{\n    if(x)\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert rule_ids(payload) == [self.RULE]
        assert payload["issues"][0]["severity"] == "error"

    def test_nested_unbraced_statements_report_each(self, check_style):
        source = "void f()\n{\n    if(x)\n        for(;;)\n            g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 2

    def test_good_braced_blocks(self, check_style):
        source = "void f()\n{\n    if(x)\n    {\n    }\n\n    for(;;)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_bad_else(self, check_style):
        source = "void f()\n{\n    if(x)\n    {\n    }\n    else\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_else_if_chain_is_legal(self, check_style):
        source = "void f()\n{\n    if(x)\n    {\n    }\n    else if(y)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_do_while_with_braces_is_legal(self, check_style):
        source = "void f()\n{\n    do\n    {\n    }\n    while(x);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_bad_do(self, check_style):
        source = "void f()\n{\n    do\n        g();\n    while(x);\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_if_constexpr_with_braces_is_legal(self, check_style):
        source = "template<class T>\nvoid f()\n{\n    if constexpr (true)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_bad_if_constexpr(self, check_style):
        source = "template<class T>\nvoid f()\n{\n    if constexpr (true)\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_preprocessor_between_condition_and_body_is_a_warning(self, check_style):
        source = "void f()\n{\n    if(x)\n#if A\n        g();\n#endif\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        issue = payload["issues"][0]
        assert issue["severity"] == "warning"
        assert "cannot decide" in issue["message"]

    def test_preprocessor_between_condition_and_brace_is_not_reported(self, check_style):
        source = "void f()\n{\n    if(x)\n#if A\n    {\n    }\n#endif\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_comment_before_brace_is_not_reported(self, check_style):
        source = "void f()\n{\n    if(x) // reason\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_comment_before_unbraced_body_is_a_warning(self, check_style):
        source = "void f()\n{\n    if(x) // reason\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert payload["issues"][0]["severity"] == "warning"

    def test_bad_catch_and_switch(self, check_style):
        source = "void f()\n" "{\n" "    switch(x)\n" "        g();\n" "\n" "    try\n" "    {\n" "    }\n" "    catch(...)\n" "        g();\n" "}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 2

class TestOneStatementPerLine:
    RULE = "one-statement-per-line"

    def test_bad_two_calls(self, check_style):
        source = "void f()\n{\n    g(); h();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        issue = payload["issues"][0]
        assert issue["severity"] == "warning"
        assert issue["line"] == 3

    def test_bad_two_declarations(self, check_style):
        source = "int first = 0; int second = 1;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_good_one_statement_per_line(self, check_style):
        source = "int first = 0;\nint second = 1;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_for_header_semicolons_do_not_count(self, check_style):
        source = "void f()\n{\n    for(int i = 0;i < 3;i++)\n    {\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_case_body_counts(self, check_style):
        source = "void f(int x)\n{\n    switch(x)\n    {\n        case 1: g(); h();\n    }\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_declarator_list_is_one_statement(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "int first, second;\n"}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_struct_members_on_one_line(self, check_style):
        source = "struct point { int x; int y; };\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_closing_brace_semicolon_is_one_statement(self, check_style):
        source = "struct point\n{\n    int x;\n};\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

class TestLineCommentsOnly:
    RULE = "line-comments-only"

    def test_license_block_comment_is_allowed(self, check_style):
        source = "/*\n" " Copyright (c) 2026 Celestia Island.\n" " SPDX-License-Identifier: MIT\n" "*/\n" "\n" "int value = 0;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_block_comment_after_code_is_reported(self, check_style):
        source = "int value = 0;\n\n/* note */\nint other = 1;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        assert payload["issues"][0]["line"] == 3

    def test_first_comment_group_without_license_marker(self, check_style):
        source = "// helper notes\n/* detail */\nint value = 0;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_later_block_comment_after_license_header(self, check_style):
        source = "/* Copyright 2026 */\nint value = 0;\n\n/* later */\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        assert payload["issues"][0]["line"] == 4

    def test_doxygen_block_reported_by_default(self, check_style):
        source = "/** does something */\nint value = 0;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_doxygen_allowed_by_config(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": "allow_doxygen = true\n", "sample.cpp": "/** does something */\nint value = 0;\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 0
        assert payload["issues"] == []

    def test_markers_inside_strings_and_line_comments_are_ignored(self, check_style):
        source = 'auto text = "/* not a comment */"; // /* also not a comment\nint value = 0;\n'
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

class TestIncludeBlock:
    RULE = "include-block"

    def test_good_header(self, check_style):
        source = "// Copyright (c) 2026 Celestia Island.\n" "\n" "#pragma once\n" "\n" "#include <cstdint>\n" "#include <vector>\n" "\n" "int value();\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_include_after_code_is_reported(self, check_style):
        source = "int value = 0;\n\n#include <vector>\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        assert "after the first code line" in payload["issues"][0]["message"]

    def test_missing_blank_line_after_includes(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "#include <vector>\nint value = 0;\n"}, extra = ("--select", self.RULE))
        assert code == 1
        issues = issues_for(payload, self.RULE)
        assert len(issues) == 1
        assert "found 0" in issues[0]["message"]

    def test_two_blank_lines_after_includes(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "#include <vector>\n\n\nint value = 0;\n"}, extra = ("--select", self.RULE))
        assert code == 1
        issues = issues_for(payload, self.RULE)
        assert len(issues) == 1
        assert "found 2" in issues[0]["message"]

    def test_include_guard_header(self, check_style):
        source = "#ifndef SAMPLE_H\n#define SAMPLE_H\n\n#include <cstdint>\n\nint value();\n\n#endif\n"
        code, payload, _ = check_style({"sample.h": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_conditional_include_at_head(self, check_style):
        source = "#ifdef _WIN32\n#include <windows.h>\n#endif\n\nint value();\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_no_includes_is_fine(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "int value = 0;\n"}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_includes_without_following_code(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "#include <vector>\n"}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_comment_line_between_include_and_code(self, check_style):
        source = "#include <vector>\n\n// detail\nint value = 0;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_comment_without_blank_line_after_include(self, check_style):
        source = "#include <vector>\n// detail\nint value = 0;\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

class TestPlatformHeaderAllowlist:
    RULE = "platform-header-allowlist"

    def test_default_rejects_platform_header(self, check_style):
        source = "#include <windows.h>\n\nint main();\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        assert "windows.h" in payload["issues"][0]["message"]

    def test_allowlisted_path(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'platform_paths = ["driver/**"]\n', "driver/platform.cpp": "#include <windows.h>\n\nint create();\n"})
        code, payload, _ = run_json(capsys, ["check", "driver", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 0
        assert payload["issues"] == []

    def test_non_allowlisted_path(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'platform_paths = ["driver/**"]\n', "ui/main.cpp": "#include <windows.h>\n\nint main();\n"})
        code, payload, _ = run_json(capsys, ["check", "ui", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_quoted_include_is_checked(self, check_style):
        code, payload, _ = check_style({"sample.cpp": '#include "windows.h"\n\nint main();\n'}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_case_insensitive_header_match(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "#include <Windows.h>\n\nint main();\n"}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_standard_header_is_fine(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "#include <vector>\n\nint main();\n"}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_configured_header_list_replaces_defaults(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'platform_headers = ["my_platform.h"]\n', "sample.cpp": ("#include <windows.h>\n#include <my_platform.h>\n\nint main();\n")})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 1
        issues = issues_for(payload, self.RULE)
        assert len(issues) == 1
        assert "my_platform.h" in issues[0]["message"]

    def test_header_file_including_platform_header(self, check_style):
        code, payload, _ = check_style({"sample.h": "#include <winsock2.h>\n\nint create();\n"}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

class TestPlatformTypeLeak:
    RULE = "platform-type-leak"

    def test_header_exposes_handle(self, check_style):
        source = "struct handle_holder\n{\n    HANDLE handle;\n};\n"
        code, payload, _ = check_style({"sample.h": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1
        assert "HANDLE" in payload["issues"][0]["message"]

    def test_implementation_file_is_not_checked(self, check_style):
        source = "struct handle_holder\n{\n    HANDLE handle;\n};\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_header_exposes_com_interface(self, check_style):
        code, payload, _ = check_style({"sample.hpp": "class ID3D11Device;\n"}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_header_exposes_socket_and_hresult(self, check_style):
        code, payload, _ = check_style({"sample.hh": "SOCKET socket_value;\nHRESULT result;\n"}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 2

    def test_comments_and_strings_are_not_code(self, check_style):
        source = '// HANDLE\nconst char *type_name = "HANDLE";\n'
        code, payload, _ = check_style({"sample.h": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_platform_conditional_branch_still_leaks(self, check_style):
        source = "#ifdef _WIN32\nHANDLE handle;\n#endif\n"
        code, payload, _ = check_style({"sample.h": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(issues_for(payload, self.RULE)) == 1

    def test_configured_platform_types(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'platform_types = ["MYTYPE"]\n', "sample.h": "MYTYPE first;\nHANDLE second;\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json", "--select", self.RULE])
        assert code == 1
        issues = issues_for(payload, self.RULE)
        assert len(issues) == 1
        assert "MYTYPE" in issues[0]["message"]

    def test_inl_file_is_not_checked(self, check_style):
        code, payload, _ = check_style({"sample.inl": "HANDLE handle;\n"}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

# --------------------------------------------------------------------------- exemptions

class TestExemptions:
    RULE = "no-space-before-paren"

    def test_inline_same_line(self, check_style):
        source = "void update (int value); // cpp-style-ok: no-space-before-paren: legacy\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_inline_previous_line(self, check_style):
        source = "// cpp-style-ok: no-space-before-paren: legacy signature\nvoid update (int value);\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_inline_wildcard(self, check_style):
        source = "// cpp-style-ok: *: migrating later\nvoid update (int value);\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 0
        assert payload["issues"] == []

    def test_inline_without_reason_does_not_exempt(self, check_style):
        source = "void update (int value); // cpp-style-ok: no-space-before-paren\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert len(payload["issues"]) == 1
        assert any("without a reason" in warning for warning in payload["warnings"])

    def test_inline_unknown_rule_warns(self, check_style):
        source = "void update (int value); // cpp-style-ok: no-such-rule: why\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", self.RULE))
        assert code == 1
        assert any("unknown rule id" in warning for warning in payload["warnings"])

    def test_config_exempt_glob(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": ("[[exempt]]\n" 'path = "legacy/**"\n' 'rules = ["no-space-before-paren"]\n' 'reason = "imported from the old engine"\n'), "legacy/old.cpp": "void update (int value);\n", "other/new.cpp": "void update (int value);\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json"])
        assert code == 1
        paths = [issue["path"] for issue in payload["issues"]]
        assert len(paths) == 1
        assert paths[0].endswith("other/new.cpp")

    def test_config_exempt_missing_reason_is_a_config_error(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": ('[[exempt]]\npath = "legacy/**"\nrules = ["no-space-before-paren"]\n'), "sample.cpp": "void update (int value);\n"})
        code, out, err = run_cli(capsys, ["check", ".", "--config", "cpp-style.toml", "--json"])
        assert code == 2
        assert out.strip() == ""
        assert "reason" in err

    def test_config_exempt_unknown_field_is_a_config_error(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": ("[[exempt]]\n" 'path = "legacy/**"\n' 'rules = ["no-space-before-paren"]\n' 'reason = "old engine"\n' 'note = "extra"\n'), "sample.cpp": "void update (int value);\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--config", "cpp-style.toml", "--json"])
        assert code == 2
        assert "unknown field" in err

# --------------------------------------------------------------------------- config

class TestConfig:
    def test_cpp_style_toml_is_discovered(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'select = ["no-space-before-paren"]\n', "sample.cpp": "void f()\n{\n    if (x)\n    {\n    }\n}\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--json"])
        assert code == 1
        assert rule_ids(payload) == ["no-space-before-paren"]

    def test_pyproject_table_is_discovered(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"pyproject.toml": '[tool.cpp_style]\nselect = ["control-braces"]\n', "sample.cpp": "void f()\n{\n    if (x)\n        g();\n}\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--json"])
        assert code == 1
        assert rule_ids(payload) == ["control-braces"]

    def test_cpp_style_toml_tool_table_is_accepted(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": '[tool.cpp_style]\nselect = ["line-comments-only"]\n', "sample.cpp": "/* note */\nint value = 0;\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--json"])
        assert code == 1
        assert rule_ids(payload) == ["line-comments-only"]

    def test_explicit_config_overrides_discovered(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'select = ["line-comments-only"]\n', "other.toml": 'select = ["no-space-before-paren"]\n', "sample.cpp": "/* note */\nvoid update (int value);\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "other.toml", "--json"])
        assert code == 1
        assert rule_ids(payload) == ["no-space-before-paren"]

    def test_no_config_ignores_discovered(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'select = ["line-comments-only"]\n', "sample.cpp": "/* note */\nvoid update (int value);\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--no-config", "--json"])
        assert code == 1
        assert set(rule_ids(payload)) == {"line-comments-only", "no-space-before-paren"}

    def test_unknown_key_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": "platform_header = []\n", "sample.cpp": "int value = 0;\n"})
        code, out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert out.strip() == ""
        assert "unknown configuration key" in err

    def test_unknown_rule_in_select_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'select = ["no-such-rule"]\n', "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "unknown rule id" in err

    def test_unknown_rule_in_ignore_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'ignore = ["no-such-rule"]\n', "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "unknown rule id" in err

    def test_empty_select_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": "select = []\n", "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "select" in err

    def test_wrong_value_type_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": "platform_headers = 3\n", "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "list of strings" in err

    def test_malformed_toml_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": "select = [\n", "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "cannot parse" in err

    def test_missing_explicit_config_fails_closed(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--config", "missing.toml", "--json"])
        assert code == 2
        assert "not found" in err

    def test_invalid_macro_style_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'macro_style = "error"\n', "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "macro_style" in err

    def test_non_boolean_allow_doxygen_fails_closed(self, workspace, capsys):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'allow_doxygen = "yes"\n', "sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--json"])
        assert code == 2
        assert "allow_doxygen" in err

    def test_load_config_reports_origin_and_values(self, workspace):
        make_repo_root(workspace)
        write_files(workspace, {"cpp-style.toml": 'platform_types = ["MYTYPE"]\nexclude = ["ignored"]\n'})
        config = load_config(explicit = None, no_config = False, cwd = workspace)
        assert config.platform_types == ("MYTYPE",)
        assert config.exclude == ("ignored",)
        assert config.origin.endswith("cpp-style.toml")

    def test_load_config_rejects_unknown_key_directly(self, workspace):
        path = workspace / "bad.toml"
        path.write_text("unknown_key = 1\n", encoding = "utf-8")

        with pytest.raises(ConfigError):
            load_config(explicit = str(path), no_config = False, cwd = workspace)

# --------------------------------------------------------------------------- scanning

class TestScanning:
    def test_default_skip_directories(self, check_style):
        skipped_dirs = ["third_party", "vendor", "external", "generated", "build", "dist", "target", "node_modules"]
        files = {"main.cpp": "int main();\n"}

        for directory in skipped_dirs:
            files[directory + "/bad.cpp"] = "void update (int value);\n"

        code, payload, _ = check_style(files)
        assert code == 0
        assert payload["issues"] == []
        assert payload["summary"]["skipped"] >= len(skipped_dirs)
        assert payload["summary"]["checked"] == 1

    def test_configured_exclude(self, workspace, capsys):
        write_files(workspace, {"cpp-style.toml": 'exclude = ["custom/**"]\n', "custom/bad.cpp": "void update (int value);\n", "main.cpp": "int main();\n"})
        code, payload, _ = run_json(capsys, ["check", ".", "--config", "cpp-style.toml", "--json"])
        assert code == 0
        assert payload["issues"] == []
        assert payload["summary"]["skipped"] >= 1
        assert payload["summary"]["checked"] == 1

    def test_non_source_files_are_ignored(self, check_style):
        code, payload, _ = check_style({"notes.txt": "void update (int value);\n", "main.cpp": "int main();\n"})
        assert code == 0
        assert payload["summary"]["checked"] == 1

    def test_explicit_file_argument(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "void update (int value);\n"}, args = ("check", "sample.cpp"))
        assert code == 1
        assert payload["summary"]["checked"] == 1

    def test_nonexistent_path_is_a_usage_error(self, workspace, capsys):
        code, out, err = run_cli(capsys, ["check", "missing.cpp", "--no-config", "--json"])
        assert code == 2
        assert out.strip() == ""
        assert "does not exist" in err

    def test_json_paths_are_posix(self, check_style):
        code, payload, _ = check_style({"src/nested/sample.cpp": "void update (int value);\n"})
        assert code == 1
        path = payload["issues"][0]["path"]
        assert path == "src/nested/sample.cpp"
        assert "\\" not in path

# --------------------------------------------------------------------------- CLI

class TestCli:
    def test_no_subcommand_exits_two(self, capsys):
        code, out, err = run_cli(capsys, [])
        assert code == 2
        assert out == ""
        assert "subcommand is required" in err

    def test_unknown_subcommand_exits_two(self, capsys):
        code, _out, err = run_cli(capsys, ["frobnicate"])
        assert code == 2
        assert "invalid choice" in err

    def test_list_rules_completeness(self, capsys):
        code, out, err = run_cli(capsys, ["list-rules"])
        assert code == 0
        assert err == ""
        assert "8 rule(s)" in out

        for rule in RULES:
            assert rule.id in out
            assert rule.severity in out
            assert rule.judgment in out

            for exception in rule.exceptions:
                assert exception in out

        assert len(RULES) == 8
        assert RULE_IDS == ("no-space-before-paren", "for-semicolon-spacing", "control-braces", "one-statement-per-line", "line-comments-only", "include-block", "platform-header-allowlist", "platform-type-leak")

    def test_list_rules_json(self, capsys):
        code, payload, err = run_json(capsys, ["list-rules", "--json"])
        assert code == 0
        assert err == ""
        assert payload["tool"] == "cpp-style"
        assert [entry["id"] for entry in payload["rules"]] == list(RULE_IDS)

        for entry in payload["rules"]:
            assert set(entry) == {"id", "title", "source", "severity", "autofix", "judgment", "exceptions"}
            assert entry["autofix"] is False
            assert entry["judgment"]
            assert entry["exceptions"]

    def test_clean_run_exits_zero(self, check_style):
        code, payload, err = check_style({"sample.cpp": "int value = 0;\n"})
        assert code == 0
        assert payload["issues"] == []
        assert payload["summary"]["checked"] == 1
        assert payload["summary"]["errors"] == 0
        assert err == ""

    def test_clean_run_human_summary(self, check_style):
        code, _payload, err = check_style({"sample.cpp": "int value = 0;\n"}, json_mode = False)
        assert code == 0
        assert "no issues found" in err

    def test_violations_exit_one(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "void update (int value);\n"})
        assert code == 1
        assert payload["summary"]["findings"] == 1
        assert payload["summary"]["errors"] == 1

    def test_violations_human_output(self, check_style):
        code, _payload, err = check_style({"sample.cpp": "void update (int value);\n"}, json_mode = False)
        assert code == 1
        assert "cpp-style: sample.cpp:1:6: no-space-before-paren:" in err
        assert "1 finding(s)" in err

    def test_json_schema(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "void f()\n{\n    if (x)\n        g();\n}\n"})
        assert code == 1
        assert set(payload) == {"tool", "config", "summary", "issues", "warnings"}
        assert payload["tool"] == "cpp-style"
        assert set(payload["summary"]) == {"checked", "skipped", "findings", "errors", "warnings", "exempted", "diagnostics", "truncated"}
        issue = payload["issues"][0]
        assert set(issue) == {"path", "line", "column", "rule", "severity", "message"}

    def test_warn_only_exits_zero(self, check_style):
        code, payload, _ = check_style({"sample.cpp": "void update (int value);\n"}, extra = ("--warn-only",))
        assert code == 0
        assert len(payload["issues"]) == 1

    def test_select_comma_separated_matches_repeated(self, check_style):
        files = {"sample.cpp": "void update (int value);\n"}
        code_one, payload_one, _ = check_style(files, extra = ("--select", "no-space-before-paren,control-braces"))
        code_two, payload_two, _ = check_style(files, extra = ("--select", "no-space-before-paren", "--select", "control-braces"))
        assert code_one == code_two == 1
        assert rule_ids(payload_one) == rule_ids(payload_two)
        assert rule_ids(payload_one) == ["no-space-before-paren"]

    def test_select_and_ignore_star(self, check_style):
        source = "void update (int value);\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", "*"))
        assert code == 1
        assert "no-space-before-paren" in rule_ids(payload)
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--ignore", "*"))
        assert code == 0
        assert payload["issues"] == []
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--ignore", "no-space-before-paren,control-braces"))
        assert code == 0
        assert payload["issues"] == []

    def test_select_limits_rules(self, check_style):
        source = "void f()\n{\n    if (x)\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--select", "control-braces"))
        assert code == 1
        assert rule_ids(payload) == ["control-braces"]

    def test_ignore_skips_rules(self, check_style):
        source = "void f()\n{\n    if (x)\n        g();\n}\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--ignore", "control-braces"))
        assert code == 1
        assert "control-braces" not in rule_ids(payload)
        assert "no-space-before-paren" in rule_ids(payload)

    def test_max_issues_truncates_output(self, check_style):
        source = "void update (int first);\nvoid other (int second);\n"
        code, payload, _ = check_style({"sample.cpp": source}, extra = ("--max-issues", "1"))
        assert code == 1
        assert len(payload["issues"]) == 1
        assert payload["summary"]["findings"] == 2
        assert payload["summary"]["truncated"] is True

    def test_quiet_keeps_only_summary(self, check_style):
        code, _payload, err = check_style({"sample.cpp": "void update (int value);\n"}, extra = ("--quiet",), json_mode = False)
        assert code == 1
        assert "no-space-before-paren" not in err
        assert "cpp-style: 1 file(s) checked" in err

    def test_quiet_clean_run_keeps_summary(self, check_style):
        code, _payload, err = check_style({"sample.cpp": "int value = 0;\n"}, extra = ("--quiet",), json_mode = False)
        assert code == 0
        assert "no issues found" in err

    def test_quiet_suppresses_diagnostics(self, check_style):
        source = "void update (int value); // cpp-style-ok: no-space-before-paren\n"
        code, _payload, err = check_style({"sample.cpp": source}, extra = ("--quiet",), json_mode = False)
        assert code == 1
        assert "without a reason" not in err
        assert "finding(s)" in err

    def test_check_never_writes(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "void update (int value);\n"})
        before = (workspace / "sample.cpp").read_bytes()
        code, _out, _err = run_cli(capsys, ["check", ".", "--no-config"])
        assert code == 1
        assert (workspace / "sample.cpp").read_bytes() == before

    def test_check_without_path_defaults_to_cwd(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "void update (int value);\n"})
        code, payload, _ = run_json(capsys, ["check", "--no-config", "--json"])
        assert code == 1
        assert payload["summary"]["checked"] == 1

    def test_config_and_no_config_conflict(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--config", "x.toml", "--no-config"])
        assert code == 2
        assert "not allowed" in err

    def test_unknown_select_rule_is_usage_error(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--no-config", "--select", "no-such-rule"])
        assert code == 2
        assert "unknown rule id" in err

    def test_negative_max_issues_is_usage_error(self, workspace, capsys):
        write_files(workspace, {"sample.cpp": "int value = 0;\n"})
        code, _out, err = run_cli(capsys, ["check", ".", "--no-config", "--max-issues", "-1"])
        assert code == 2
        assert "max-issues" in err

# --------------------------------------------------------------------------- fixtures

class TestFixtures:
    def test_good_fixtures_are_clean(self, capsys):
        code, payload, _ = run_json(capsys, ["check", str(FIXTURES / "good"), "--no-config", "--json"])
        assert code == 0
        assert payload["issues"] == []
        assert payload["summary"]["checked"] == 2

    def test_bad_fixtures_report_expected_rules(self, capsys):
        code, payload, _ = run_json(capsys, ["check", str(FIXTURES / "bad"), "--no-config", "--json"])
        assert code == 1
        reported = set(rule_ids(payload))
        assert {"no-space-before-paren", "control-braces", "for-semicolon-spacing", "line-comments-only", "one-statement-per-line", "platform-header-allowlist", "platform-type-leak"} <= reported

    def test_third_party_fixture_is_skipped(self, capsys):
        code, payload, _ = run_json(capsys, ["check", str(FIXTURES), "--no-config", "--json"])
        assert code == 1
        assert payload["summary"]["skipped"] >= 1
        paths = [issue["path"] for issue in payload["issues"]]
        assert not any("third_party" in path for path in paths)

    def test_platform_config_allows_driver_only(self, capsys):
        root = FIXTURES / "platform_repo"
        code, payload, _ = run_json(capsys, ["check", str(root), "--config", str(root / "cpp-style.toml"), "--json", "--select", "platform-header-allowlist"])
        assert code == 1
        issues = issues_for(payload, "platform-header-allowlist")
        assert len(issues) == 1
        assert issues[0]["path"].endswith("ui/main.cpp")

# --------------------------------------------------------------------------- performance

class TestPerformance:
    def test_two_hundred_synthetic_files_under_three_seconds(self, workspace, capsys):
        source = "// Copyright (c) 2026 Celestia Island.\n" "\n" "int compute(int value)\n" "{\n" "    if(value > 0)\n" "    {\n" "        return value;\n" "    }\n" "\n" "    for(int i = 0;i < 3;i++)\n" "    {\n" "        value = value + i;\n" "    }\n" "\n" "    return value;\n" "}\n"

        for index in range(200):
            write_files(workspace, {"src/file_%03d.cpp" % index: source})

        started = time.perf_counter()
        code, out, _err = run_cli(capsys, ["check", ".", "--no-config", "--json"])
        elapsed = time.perf_counter() - started
        payload = json.loads(out)
        assert code == 0
        assert payload["summary"]["checked"] == 200
        assert payload["issues"] == []
        assert elapsed < 3.0

    def test_fixtures_check_under_three_seconds(self, capsys):
        started = time.perf_counter()
        code, _payload, _err = run_json(capsys, ["check", str(FIXTURES), "--no-config", "--json"])
        elapsed = time.perf_counter() - started
        assert code == 1
        assert elapsed < 3.0
