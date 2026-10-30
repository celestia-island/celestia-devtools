#!/usr/bin/env python3
"""Check C and C++ sources against the org's custom layout rules (check only, never writes).

The catalogue is deliberately a small lexical / lightweight-structural set of
rules that a stdlib-only scanner can decide without a compiler. Positioning
(2026-10-31 ruling): the family writes Rust and this checker is secondary but
active -- the fallback layer for the native C/C++ pieces a service grows when
it needs a lower-level high-performance path, and it checks C sources too,
because the lexical rules apply to C as-is apart from C++-only sugar.
The authoritative contract for each rule (source clause, severity, judgment and
the exceptions that are deliberately not reported) is cpp-style list-rules.

Rules (ids are stable):

- no-space-before-paren -- a name or control keyword must touch its '(';
- for-semicolon-spacing -- for(init;condition;step) has no spaces at the semicolons;
- control-braces -- if/for/while/switch/catch/do/else need a braced body;
- one-statement-per-line -- one statement terminator per physical line;
- line-comments-only -- block comments only in the leading license header;
- include-block -- #include at the head, one blank line before the code;
- platform-header-allowlist -- platform headers only in allowlisted sources;
- platform-type-leak -- headers must not expose HANDLE/SOCKET/HRESULT/COM types.

Output and exit codes (shared with py-style):

- human-readable findings and the summary go to stderr, prefixed cpp-style:;
- --json writes a single JSON document to stdout instead;
- exit 0 = clean, 1 = findings, 2 = usage / configuration / unreadable input;
- --warn-only always exits 0 for findings (configuration errors still exit 2);
- --quiet suppresses each finding line (and each diagnostic line) and keeps only
  the closing summary line; --json output is unaffected.

JSON schema (stable keys; this structure is the shared contract with py-style,
which aligns to it; the tests pin it)::

    {
      "tool": "cpp-style",
      "config": "<origin of the effective config>",
      "summary": {
        "checked": <int>, "skipped": <int>, "findings": <int>,
        "errors": <int>, "warnings": <int>, "exempted": <int>,
        "diagnostics": <int>, "truncated": <bool>
      },
      "issues": [
        {"path": "<posix path>", "line": <int>, "column": <int>,
         "rule": "<id>", "severity": "error" | "warning", "message": "<text>"}
      ],
      "warnings": ["<tokenizer/inline-directive diagnostics>"]
    }

skipped counts paths excluded by the built-in directory list (third_party,
vendor, external, generated, build, dist, target, node_modules, ...) or by the
configured exclude globs; a pruned directory counts once. Every rule finding is
read-only; the checker never modifies a file.

Configuration is read from --config, then <repo root>/cpp-style.toml, then
[tool.cpp_style] in <repo root>/pyproject.toml, then built-in defaults (unknown
keys and rule ids fail closed with exit 2). Inline exemptions are line comments
of the form::

    // cpp-style-ok: <rule[,rule]|*>: <reason>

placed on the same line as the finding or on the comment-only line above it. A
missing reason does not exempt and is recorded under "warnings".

Interpretations worth knowing (the rules catalogue lists them per rule):

- no-space-before-paren only asks function names and the control keywords
  if/for/while/switch/catch to touch their parenthesis; keyword-like constructs
  that take a parenthesised operand (return, sizeof, alignof, decltype, alignas,
  static_assert, noexcept, constexpr including if constexpr, operator, new,
  delete, __declspec, __attribute__, co_await, co_return, co_yield) are exempt by
  default, and allow_space_before_paren adds more names on top of that list;
- control-braces emits a warning "cannot decide" when a comment or preprocessor
  directive separates the condition from a body that is not braced, instead of
  guessing across an #if boundary;
- line-comments-only treats the first contiguous comment group of the file as
  the license header when its text contains license, spdx or copyright;
- include-block ignores comment-only lines when counting the blank line after
  the last include.

Deliberately out of scope in this version (documented in cpp.md as P2/P3 or
manual): brace placement and indentation, blank-line families around blocks,
pointer/reference binding, binary-operator spacing, unsigned-literal style, case
body braces, class design rules, and any auto-fix. No clang/LLVM dependency.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple
from celestia_devtools.lint.cpp.checks import run_checks
from celestia_devtools.lint.cpp.config import (config_record, config_error, load_config, matches_globs, path_variants)
from celestia_devtools.lint.cpp.rules import RULE_BY_ID, RULE_IDS, RULES
from celestia_devtools.lint.cpp.structure import analyzed_file, finding_record, analyze
from celestia_devtools.lint.cpp.tokenizer import COMMENT, token_record

#: File suffixes the checker scans. `.c` is deliberate (2026-10-31 ruling):
#: the lexical rules apply to C as-is apart from C++-only sugar.
SOURCE_SUFFIXES = frozenset({".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx", ".inl"})

#: Directories that are never scanned (the org rule exempts third-party code).
DEFAULT_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "target", "build", "dist", "third_party", "vendor", "external", "generated", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})

_INLINE_DIRECTIVE = re.compile(r"//\s*cpp-style-ok\s*:\s*(?P<payload>.*)$")
_RULE_TOKEN_SPLIT = re.compile(r"[\s,]+")

class _usage_error(Exception):
    """Argument parsing problem; mapped to exit code 2 with a cpp-style: error."""

class _parser(argparse.ArgumentParser):
    """argparse parser that raises instead of calling sys.exit on usage errors."""

    def error(self, message: str) -> None:
        raise _usage_error(message)

@dataclass
class discovery:
    """Result of expanding CLI paths into C++ source files."""

    files: List[Path]
    skipped: int
    errors: List[str]

@dataclass
class inline_exemptions:
    """Parsed inline cpp-style-ok directives of one file."""

    applicable: Dict[int, Set[str]]
    warnings: List[str]

def display_path(path: Path, cwd: Path) -> str:
    """Stable human/JSON path: relative to cwd when possible, always posix."""

    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()

def discover_sources(paths: Sequence[str], config: config_record, cwd: Path) -> discovery:
    """Expand file/directory arguments into the source files to check."""
    files: List[Path] = []
    skipped = 0
    errors: List[str] = []

    for raw in paths:
        candidate = Path(raw)

        if not candidate.is_absolute():
            candidate = cwd / candidate

        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()

        if candidate.is_dir():
            walked, walked_skipped = _walk_directory(candidate, config)
            files.extend(walked)
            skipped += walked_skipped
        elif candidate.is_file():
            if candidate.suffix.lower() not in SOURCE_SUFFIXES:
                skipped += 1
                continue

            if _excluded(candidate, config):
                skipped += 1
                continue

            files.append(candidate)
        else:
            errors.append("path does not exist: %s" % raw)

    unique: List[Path] = []
    seen: Set[str] = set()

    for path in files:
        key = str(path).lower()

        if key in seen:
            continue

        seen.add(key)
        unique.append(path)

    unique.sort(key = lambda path: display_path(path, cwd))
    return discovery(files = unique, skipped = skipped, errors = errors)

def _excluded(path: Path, config: config_record) -> bool:
    if not config.exclude:
        return False

    return matches_globs(path_variants(path, config), config.exclude)

def _walk_directory(root: Path, config: config_record) -> Tuple[List[Path], int]:
    files: List[Path] = []
    skipped = 0

    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        kept: List[str] = []

        for name in sorted(dirnames):
            child = current / name

            if (name.lower() in DEFAULT_SKIP_DIRS) or _excluded(child, config):
                skipped += 1
                continue

            kept.append(name)

        dirnames[:] = kept

        for name in sorted(filenames):
            path = current / name

            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue

            if _excluded(path, config):
                skipped += 1
                continue

            files.append(path)

    return files, skipped

def _split_rule_values(values: Sequence[str] | None) -> Tuple[str, ...]:
    if not values:
        return ()

    names: List[str] = []

    for value in values:
        for part in value.split(","):
            name = part.strip()

            if name:
                names.append(name)

    return tuple(names)

def _require_rule(name: str, where: str) -> str:
    if name == "*":
        return name

    if name not in RULE_BY_ID:
        raise config_error("unknown rule id %r in %s (run 'cpp-style list-rules' for the catalogue)" % (name, where))

    return name

def resolve_selection(config: config_record, cli_select: Sequence[str] | None, cli_ignore: Sequence[str] | None) -> frozenset:
    """Effective rule set: CLI --select/--ignore override/extend the config."""
    requested = _split_rule_values(cli_select)

    if requested:
        if "*" in requested:
            selected = set(RULE_IDS)
        else:
            selected = {_require_rule(name, "--select") for name in requested}
    elif config.select is not None:
        if "*" in config.select:
            selected = set(RULE_IDS)
        else:
            selected = set(config.select)
    else:
        selected = set(RULE_IDS)

    for name in config.ignore + _split_rule_values(cli_ignore):
        if "*" in name:
            selected.clear()
            continue

        selected.discard(_require_rule(name, "ignore"))

    return frozenset(selected)

def _comment_starts_line(af: analyzed_file, token: token_record) -> bool:
    line = af.line_text(token.line)
    return not line[: max(token.col - 1, 0)].strip()

def parse_inline_exemptions(af: analyzed_file, shown: str) -> inline_exemptions:
    """Collect cpp-style-ok directives from line comments of one file."""
    applicable: Dict[int, Set[str]] = {}
    warnings: List[str] = []

    for token in af.tokens:
        if (token.kind != COMMENT) or not token.text.startswith("//"):
            continue

        match = _INLINE_DIRECTIVE.search(token.text)

        if match is None:
            continue

        payload = match.group("payload")
        rules_part, separator, reason = payload.partition(":")

        if not separator or not reason.strip():
            warnings.append("%s:%d: cpp-style-ok directive without a reason is ignored" % (shown, token.line))
            continue

        accepted: Set[str] = set()

        for name in [part for part in _RULE_TOKEN_SPLIT.split(rules_part.strip()) if part]:
            if name == "*":
                accepted.add("*")
            elif name in RULE_BY_ID:
                accepted.add(name)
            else:
                warnings.append("%s:%d: unknown rule id %r in cpp-style-ok directive" % (shown, token.line, name))

        if not accepted:
            continue

        if "*" in accepted:
            accepted = {"*"}

        applicable.setdefault(token.line, set()).update(accepted)

        if _comment_starts_line(af, token):
            applicable.setdefault(token.end_line + 1, set()).update(accepted)

    return inline_exemptions(applicable = applicable, warnings = warnings)

def _is_exempt(config: config_record, variants: Sequence[str], inline: inline_exemptions, finding: finding_record) -> bool:
    for exemption in config.exempt:
        if ("*" not in exemption.rules) and (finding.rule not in exemption.rules):
            continue

        if matches_globs(variants, (exemption.pattern,)):
            return True

    applied = inline.applicable.get(finding.line)

    if applied and (("*" in applied) or (finding.rule in applied)):
        return True

    return False

def _render(shown: str, finding: finding_record) -> str:
    return "cpp-style: %s:%d:%d: %s: %s" % (shown, finding.line, finding.col, finding.rule, finding.message)

def _issue_payload(shown: str, finding: finding_record) -> dict:
    return {"path": shown, "line": finding.line, "column": finding.col, "rule": finding.rule, "severity": finding.severity, "message": finding.message}

def _run_check(args: argparse.Namespace) -> int:
    cwd = Path.cwd()

    try:
        config = load_config(explicit = args.config, no_config = args.no_config, cwd = cwd)
        selected = resolve_selection(config, args.select, args.ignore)
    except config_error as exc:
        print("cpp-style: error: %s" % exc, file = sys.stderr)
        return 2

    if args.max_issues < 0:
        print("cpp-style: error: --max-issues must be zero or positive", file = sys.stderr)
        return 2

    paths = list(args.paths) if args.paths else ["."]
    discovery = discover_sources(paths, config, cwd)

    if discovery.errors:
        for error in discovery.errors:
            print("cpp-style: error: %s" % error, file = sys.stderr)

        return 2

    findings: List[Tuple[str, finding_record]] = []
    diagnostics: List[str] = []
    exempted = 0

    for path in discovery.files:
        try:
            analyzed = analyze(path)
        except OSError as exc:
            print("cpp-style: error: cannot read %s: %s" % (path, exc), file = sys.stderr)
            return 2

        shown = display_path(path, cwd)
        diagnostics.extend("%s: %s" % (shown, warning) for warning in analyzed.warnings)
        inline = parse_inline_exemptions(analyzed, shown)
        diagnostics.extend(inline.warnings)
        variants = path_variants(path, config)

        for finding in run_checks(analyzed, config, selected):
            if _is_exempt(config, variants, inline, finding):
                exempted += 1
                continue

            findings.append((shown, finding))

    findings.sort(key = lambda item: (item[0], item[1].line, item[1].col, item[1].rule))
    total = len(findings)
    errors = sum(1 for _shown, finding in findings if finding.severity == "error")
    warnings = total - errors
    limit = args.max_issues
    truncated = (limit > 0) and (total > limit)
    reported = findings[:limit] if truncated else findings

    if args.json:
        payload = {"tool": "cpp-style", "config": config.origin, "summary": {"checked": len(discovery.files), "skipped": discovery.skipped, "findings": total, "errors": errors, "warnings": warnings, "exempted": exempted, "diagnostics": len(diagnostics), "truncated": truncated}, "issues": [_issue_payload(shown, finding)for shown, finding in reported], "warnings": diagnostics}
        print(json.dumps(payload, indent = 2))
    else:
        if not args.quiet:
            for shown, finding in reported:
                print(_render(shown, finding), file = sys.stderr)

            for diagnostic in diagnostics:
                print("cpp-style: warning: %s" % diagnostic, file = sys.stderr)

        if total:
            print("cpp-style: %d file(s) checked, %d skipped, %d finding(s)" " (%d error(s), %d warning(s)), %d exempted" % (len(discovery.files), discovery.skipped, total, errors, warnings, exempted), file = sys.stderr)
        else:
            print("cpp-style: no issues found (%d file(s) checked, %d skipped)" % (len(discovery.files), discovery.skipped), file = sys.stderr)

        if truncated and not args.quiet:
            print("cpp-style: output truncated to %d finding(s) by --max-issues" % limit, file = sys.stderr)

    if args.warn_only:
        return 0

    return 1 if total else 0

def _run_list_rules(args: argparse.Namespace) -> int:
    if args.json:
        payload = {"tool": "cpp-style", "rules": [{"id": rule.id, "title": rule.title, "source": rule.source, "severity": rule.severity, "autofix": rule.autofix, "judgment": rule.judgment, "exceptions": list(rule.exceptions)}for rule in RULES]}
        print(json.dumps(payload, indent = 2))
        return 0

    print("cpp-style: %d rule(s), check-only (no auto-fix)" % len(RULES))
    print()

    for rule in RULES:
        print(rule.id)
        print("  title:      %s" % rule.title)
        print("  severity:   %s" % rule.severity)
        print("  source:     %s" % rule.source)
        print("  autofix:    %s" % ("yes" if rule.autofix else "no"))
        print("  judgment:   %s" % rule.judgment)
        print("  exceptions:")

        for exception in rule.exceptions:
            print("    - %s" % exception)

        print()

    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = _parser(prog = "cpp-style", description = "Check C++ sources against the org's custom layout rules.", formatter_class = argparse.RawDescriptionHelpFormatter, epilog = "examples:\n" "  cpp-style check src\n" "  cpp-style check src --json\n" "  cpp-style check . --select control-braces --ignore include-block\n" "  cpp-style list-rules\n")
    subparsers = parser.add_subparsers(dest = "command", metavar = "{check,list-rules}")
    subparsers.required = True
    check = subparsers.add_parser("check", help = "report violations (exit 1 when any are found)", description = "Check C++ sources against the custom layout rules.")
    check.add_argument("paths", nargs = "*", metavar = "PATH", default = ["."], help = "files or directories to scan (default: the current directory)")
    check.add_argument("--json", action = "store_true", help = "write the machine-readable report to stdout")
    check.add_argument("--select", action = "append", metavar = "RULE[,RULE]", help = "only run the named rule(s); comma-separated and repeatable; '*' = every rule")
    check.add_argument("--ignore", action = "append", metavar = "RULE[,RULE]", help = "skip the named rule(s); comma-separated and repeatable; '*' = every rule")
    check.add_argument("--warn-only", action = "store_true", help = "report findings but always exit 0 (configuration errors still exit 2)")
    config_group = check.add_mutually_exclusive_group()
    config_group.add_argument("--config", metavar = "PATH", help = "explicit configuration file (overrides repo config discovery)")
    config_group.add_argument("--no-config", action = "store_true", help = "ignore cpp-style.toml / pyproject.toml and use built-in defaults")
    check.add_argument("--max-issues", type = int, default = 0, metavar = "N", help = "report at most N findings (0 = no limit)")
    check.add_argument("--quiet", action = "store_true", help = "suppress per-finding lines and keep only the closing summary")
    list_rules = subparsers.add_parser("list-rules", help = "print the rule catalogue (the authoritative spec)", description = "Print the rule catalogue: id, source clause, severity and exceptions.")
    list_rules.add_argument("--json", action = "store_true", help = "write the catalogue as JSON to stdout")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not arguments:
        print("cpp-style: error: a subcommand is required (check or list-rules)", file = sys.stderr)
        parser.print_usage(sys.stderr)
        return 2

    try:
        args = parser.parse_args(arguments)
    except _usage_error as exc:
        print("cpp-style: error: %s" % exc, file = sys.stderr)
        return 2
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0

    if args.command == "list-rules":
        return _run_list_rules(args)

    return _run_check(args)

if __name__ == "__main__":
    raise SystemExit(main())
