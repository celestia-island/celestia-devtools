#!/usr/bin/env python3
"""Rule catalogue for the cpp-style layout checker.

*Why the catalogue is code:* list-rules is the authoritative spec of what the
checker enforces, and the configuration loader validates every rule id written in
cpp-style.toml against this table. Keeping one table means a rule cannot exist in
the checker but be missing from list-rules (or vice versa), and that a typo in a
config file fails closed instead of silently disabling a check.

Each entry records the rule id, the clause in the org rules it implements, the
default severity, whether an auto-fix exists (the C++ checker is check-only in
this version), the judgment actually implemented, and the exceptions that are
deliberately not reported.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Rule:
    """One checkable rule: identity, provenance and the exact judgment."""

    id: str
    title: str
    source: str
    severity: str
    autofix: bool
    judgment: str
    exceptions: Tuple[str, ...]


RULES: Tuple[Rule, ...] = (
    Rule(
        id="no-space-before-paren",
        title="No whitespace between a name and its opening parenthesis",
        source="rules.md section 3.3 (functions, references and pointers)",
        severity="error",
        autofix=False,
        judgment="An identifier, or one of the control keywords if/for/while/switch/"
        "catch, must be followed immediately by an opening parenthesis;"
        " whitespace or a line break before it is reported. Keyword-like"
        " constructs that take a parenthesised operand but are not function"
        " calls are exempt by default (see the exceptions). A name written in"
        " upper case, or defined earlier by #define, is reported at warning"
        " severity with a macro hint, because macro invocations cannot be told"
        " from function calls lexically.",
        exceptions=(
            "built-in exemptions: return, sizeof, alignof, decltype, alignas, static_assert, noexcept, constexpr (including if constexpr), operator, new, delete, __declspec, __attribute__, co_await, co_return and co_yield",
            "additional names configured with allow_space_before_paren (additive: the built-in list stays)",
            "preprocessor lines are not scanned",
            "a comment between the name and the parenthesis makes the pair undecidable and is skipped",
            "a lowercase macro defined in another header is reported at error severity: macro detection is lexical (upper case or a #define in the same file)",
        ),
    ),
    Rule(
        id="for-semicolon-spacing",
        title="No whitespace around for-header semicolons",
        source="rules.md section 3.4 (operators and precedence)",
        severity="error",
        autofix=False,
        judgment="A for header must contain exactly two semicolons at parenthesis depth"
        " zero (directly inside the header), and each semicolon must touch its"
        " neighbouring tokens with no spaces or line breaks: for(size_t i = 0;i"
        " < count;i++).",
        exceptions=(
            "range-for headers (a top-level colon and no semicolons)",
            "headers with other than two top-level semicolons are skipped rather than guessed at",
            "a comment next to the semicolon makes that semicolon undecidable and is skipped",
        ),
    ),
    Rule(
        id="control-braces",
        title="Control statements must have a braced body",
        source="rules.md section 3.1 (braces)",
        severity="error",
        autofix=False,
        judgment="After the condition of if/for/while/switch/catch, after do, and after"
        " an else that does not start an else-if chain, the next code token must"
        " be an opening brace. Every unwrapped control statement is reported, so"
        " 'if (a) for (b) f();' reports twice. if constexpr is handled like if.",
        exceptions=(
            "else-if chains: 'else if' is a continuation, and the inner if is checked normally",
            "do-while: the trailing 'while (condition);' is a condition, not a control statement",
            "a comment or preprocessor directive between the condition and the body is reported as a warning 'cannot decide', not as a violation",
        ),
    ),
    Rule(
        id="one-statement-per-line",
        title="One statement per physical line",
        source="rules.md section 3.9 (comments and statements)",
        severity="warning",
        autofix=False,
        judgment="Two or more statement-terminating semicolons on the same physical line"
        " and at the same brace depth are reported; a statement terminator is a"
        " semicolon at parenthesis depth zero, so semicolons inside a for header"
        " or any other parentheses do not count.",
        exceptions=(
            "one declaration with several declarators (int a, b;) is a single statement",
            "preprocessor lines are not scanned",
        ),
    ),
    Rule(
        id="line-comments-only",
        title="Block comments are not allowed",
        source="rules.md section 3.9 (comments and statements)",
        severity="error",
        autofix=False,
        judgment="Only the leading license/SPDX/copyright comment group may use block"
        " comments; every other block comment is reported. The first contiguous"
        " comment group of the file is the license header when its text contains"
        " license, spdx or copyright (case-insensitive).",
        exceptions=(
            "the leading license/SPDX/copyright comment group",
            "Doxygen block comments when allow_doxygen = true",
        ),
    ),
    Rule(
        id="include-block",
        title="Include directives at the head of the file",
        source="rules.md section 3.7 (include)",
        severity="error",
        autofix=False,
        judgment="Every #include must appear before the first code token (license"
        " comments, #pragma once and include guards are not code), and the last"
        " include must be separated from the following code by exactly one blank"
        " line.",
        exceptions=(
            "license comments, #pragma once and include guards precede the includes",
            "comment-only lines between the last include and the code are ignored when counting blank lines",
            "a file without includes, or without code after the includes, is not checked for the blank line",
            "preprocessor lines that are not #include are not includes",
        ),
    ),
    Rule(
        id="platform-header-allowlist",
        title="Platform headers only in allowlisted sources",
        source="rules.md section 3.7 (include)",
        severity="error",
        autofix=False,
        judgment="#include of a configured platform header (windows.h, winsock2.h, the"
        " d3d/d2d/dwrite family, ...) is reported unless the file path matches"
        " one of the configured platform_paths globs.",
        exceptions=(
            "platform_paths globs, matched against paths relative to the config file and the working directory",
            "files under third_party/vendor/external/generated or matching config exclude are skipped entirely",
        ),
    ),
    Rule(
        id="platform-type-leak",
        title="Headers must not expose platform types",
        source="rules.md section 3.7 (include)",
        severity="error",
        autofix=False,
        judgment="In headers (.h/.hpp/.hh/.hxx) every code identifier that equals a"
        " configured platform type (HANDLE, SOCKET, HRESULT by default) or starts"
        " with a configured COM interface prefix (IUnknown, ID3D, ID2D, IDWrite,"
        " IWIC) is reported. Code inside platform conditional branches is scanned"
        " like any other code: a leak does not depend on the branch.",
        exceptions=(
            "comments and strings are not code tokens",
            "preprocessor lines are not scanned for type names",
            "implementation files (.cpp/.cc/.cxx/.inl) are not checked",
        ),
    ),
)

RULE_IDS: Tuple[str, ...] = tuple(rule.id for rule in RULES)
RULE_BY_ID: Dict[str, Rule] = {rule.id: rule for rule in RULES}
