"""Custom Python style checker and fixer for the org layout conventions.

The conventions come from the style guide (keyword-argument '=' spacing,
single-line statements, one blank line around complete blocks, import layout,
lower_snake_case definitions, ...) and are deliberately incompatible with
ruff format / Black, so this module is the authority for layout.

Usage:

    py-style check [PATH ...] [--json] [--select RULE] [--ignore RULE]
                   [--warn-only] [--config PATH] [--no-config]
                   [--max-issues N] [--quiet]
    py-style fix [PATH ...] [--dry-run] [--select RULE[,RULE]] [--ignore RULE[,RULE]]
                 [--config PATH] [--no-config] [--quiet] [--allow-many]
    py-style list-rules [--json]

Human-readable findings and the summary go to stderr; --json writes the machine
contract to stdout. Exit codes: 0 clean (or --warn-only), 1 violations, 2 usage,
configuration or I/O error. The module entry point is main(); the rule catalog
lives in celestia_devtools.lint.py_style_rules.CHECK_RULES.

JSON schema shared with cpp-style (stable keys; tests pin them):

    {
      "tool": "py-style",
      "config": "<origin of the effective config>",
      "summary": {
        "checked": <int>, "skipped": <int>, "findings": <int>,
        "errors": <int>, "warnings": <int>, "exempted": <int>,
        "diagnostics": <int>, "truncated": <bool>
      },
      "issues": [
        {"path": "<posix path relative to cwd>", "line": <int>,
         "column": <int>, "rule": "<id>",
         "severity": "error" | "warning", "message": "<text>"}
      ],
      "warnings": ["<decode / inline-directive diagnostics>"]
    }

checked counts the analysed Python files; skipped counts the paths pruned by
the built-in directory list or by the configured exclude globs (a pruned
directory counts once, an explicitly named non-Python file counts once);
findings/errors/warnings are counted before --max-issues truncation; exempted
counts violations removed by inline directives or config exempt entries;
diagnostics counts the strings in warnings; truncated marks --max-issues
truncation. --quiet suppresses per-finding and diagnostic lines but always keeps
the closing summary line.

Inline exemptions are comments of the form::

    # py-style-ok: <rule[,rule]|*>: <reason>

placed on the same line as the finding or on the line above it. A missing reason
does not exempt and is recorded under "warnings". Unknown rule ids or config
keys fail closed with exit 2.

F-strings are opaque on every supported Python version: the whole literal token
span (one STRING token on 3.11; FSTRING_START..FSTRING_END on 3.12+) is treated
as a multi-line string, findings and fixer edits inside it are suppressed, and
multi-line f-strings never count as multi-line statements. This keeps 3.11 and
3.12/3.13 results identical.
"""

from __future__ import annotations
import argparse
import ast
import json
import os
import re
import sys
import textwrap
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from celestia_devtools.lint.py_style_config import config_error, style_config, load_config
from celestia_devtools.lint.py_style_rules import (BUILTIN_RULES, CHECK_FUNCS, CHECK_RULES, CHECK_RULE_IDS, FIX_FUNCS, FIX_ORDER, MANUAL_RULES, file_context, issue_record, module_info, parse_failure, check_import_cycles, check_snake_case_file, module_import_edges, module_name_for_path, rule_severity, string_token_texts)

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

BUILT_IN_CONFIG_ORIGIN = "built-in defaults"

# A single fix run never rewrites more than this many files unless the caller
# opts in with --allow-many: a mistyped path that points at a whole tree must
# not silently reformat it (2026-10-25 incident).
MAX_FIX_FILES = 50

# Directories that are never scanned. build/dist/target are skipped only when
# they are build artefacts: a directory with that name that belongs to a Python
# package (its own __init__.py, or inside a package) is real source (O1).
SKIP_DIRECTORY_NAMES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})

BUILD_ARTIFACT_DIRECTORY_NAMES = frozenset({"build", "dist", "target"})

_DIRECTIVE_RE = re.compile(r"#\s*py-style-ok:(?P<body>.*)$")
_FILE_DIRECTIVE_RE = re.compile(r"#\s*py-style-ok-file:(?P<body>.*)$")

_USAGE = """usage: py-style <command> [options]

commands:
  check       Report style violations (default: check).
  fix         Apply the safe auto-fixes in place.
  list-rules  Print the rule catalog (ids, P refs, severity, autofix).

Run 'py-style <command> --help' for the options of a command."""

class usage_error(Exception):
    """Raised for a usage problem that should exit with status 2."""

# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

def display_path(path: Path, cwd: Path) -> str:
    """Stable human/JSON path: relative to cwd when possible, always posix."""

    try:
        return Path(os.path.relpath(path, cwd)).as_posix()
    except ValueError:
        return path.as_posix()

def _path_variants(path: Path, config: style_config, cwd: Path) -> list[str]:
    """Return the spellings of *path* that path globs may match."""

    variants = [path.resolve().as_posix()]

    for base in (config.root, cwd):
        try:
            variants.append(path.resolve().relative_to(base.resolve()).as_posix())
        except ValueError:
            continue

    variants.append(path.name)
    return variants

def _glob_to_regex(pattern: str) -> str:
    """Translate a path glob (with ** support) into a regular expression."""

    result: list[str] = []
    index = 0

    while index < len(pattern):
        character = pattern[index]

        if pattern.startswith("**/", index):
            result.append("(?:.*/)?")
            index += 3
            continue

        if pattern.startswith("**", index):
            result.append(".*")
            index += 2
            continue

        if character == "*":
            result.append("[^/]*")
        elif character == "?":
            result.append("[^/]")
        elif character == "[":
            end = pattern.find("]", index + 1)

            if end == -1:
                result.append(re.escape(character))
            else:
                result.append(pattern[index : end + 1])
                index = end + 1
                continue
        else:
            result.append(re.escape(character))

        index += 1

    return "".join(result)

def _glob_matches(pattern: str, candidates: Sequence[str]) -> bool:
    simple = pattern.strip()

    if not simple:
        return False

    if simple.endswith("/"):
        simple = simple.rstrip("/")

    regex = _glob_to_regex(simple)
    return any(re.fullmatch(regex, candidate) for candidate in candidates)

def _excluded(path: Path, config: style_config, cwd: Path) -> bool:
    if not config.exclude:
        return False

    variants = _path_variants(path, config, cwd)
    return any(_glob_matches(pattern, variants) for pattern in config.exclude)

def _is_package_directory(path: Path) -> bool:
    """Return whether *path* belongs to a Python package tree."""

    if (path / "__init__.py").is_file():
        return True

    return (path.parent / "__init__.py").is_file()

def _skip_directory(name: str, path: Path) -> bool:
    """Return whether a directory must be pruned from the source walk."""

    if name in SKIP_DIRECTORY_NAMES:
        return True

    if name in BUILD_ARTIFACT_DIRECTORY_NAMES:
        return not _is_package_directory(path)

    return False

def _walk_python_files(root: Path, config: style_config, cwd: Path) -> tuple[list[Path], int]:
    """Walk *root* collecting Python files and counting skipped paths.

    Directory symlinks are never followed and file symlinks are never collected
    (the fixer must not write through a link outside the scan roots).
    """

    found: list[Path] = []
    skipped = 0

    for current, directories, filenames in os.walk(root):
        kept: list[str] = []

        for name in sorted(directories):
            child = Path(current) / name

            if child.is_symlink():
                skipped += 1
                continue

            if _skip_directory(name, child) or _excluded(child, config, cwd):
                skipped += 1
                continue

            kept.append(name)

        directories[:] = kept

        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue

            candidate = Path(current) / filename

            if candidate.is_symlink():
                skipped += 1
                continue

            if _excluded(candidate, config, cwd):
                skipped += 1
                continue

            found.append(candidate)

    return found, skipped

@dataclass
class discovery:
    """Expanded CLI paths of one run."""

    files: list[Path]
    skipped: int
    directory_scan: bool

def discover_files(paths: Sequence[str], config: style_config) -> discovery:
    """Resolve CLI paths into a sorted list of Python files.

    directory_scan enables the file-name rule and the import-graph rule.
    """

    cwd = Path.cwd()
    requested = [Path(item) for item in paths] if paths else [Path(".")]
    files: list[Path] = []
    seen: set[str] = set()
    skipped = 0
    directory_scan = False

    for entry in requested:
        if entry.is_symlink():
            skipped += 1
            continue

        if entry.is_file():
            if entry.suffix != ".py":
                skipped += 1
                continue

            candidates = [entry]
        elif entry.is_dir():
            directory_scan = True
            found, pruned = _walk_python_files(entry, config, cwd)
            skipped += pruned
            candidates = found
        else:
            raise usage_error(f"path not found: {entry}")

        for candidate in candidates:
            # Keep the lexical absolute path: resolving here would turn a
            # symlink into its target and the fixer would write the target.
            absolute = Path(os.path.abspath(candidate))
            key = os.path.normcase(str(absolute))

            if key in seen:
                continue

            seen.add(key)

            if _excluded(absolute, config, cwd):
                skipped += 1
                continue

            files.append(absolute)

    files.sort(key = lambda item: display_path(item, cwd))
    return discovery(files = files, skipped = skipped, directory_scan = directory_scan)

# ---------------------------------------------------------------------------
# Inline exemption directives and configured exemptions
# ---------------------------------------------------------------------------

@dataclass(frozen = True)
class directive:
    """One parsed '# py-style-ok: rules[: reason]' comment."""

    rules: frozenset[str] | None
    has_reason: bool

def parse_directives(context: file_context, shown: str) -> tuple[dict[int, list[directive]], list[str]]:
    """Parse inline exemption directives of one file."""

    directives: dict[int, list[directive]] = {}
    diagnostics: list[str] = []

    for tok in context.tokens:
        if tok.type != tokenize.COMMENT:
            continue

        match = _DIRECTIVE_RE.search(tok.string)

        if match is None:
            continue

        row = tok.start[0]
        rules_text, separator, reason_text = match.group("body").partition(":")
        reason = reason_text.strip() if separator else ""
        names = {piece.strip() for piece in rules_text.split(",") if piece.strip()}
        unknown = names - CHECK_RULE_IDS - {"*"}

        if unknown:
            rendered = ", ".join(sorted(unknown))
            diagnostics.append(f"{shown}:{row}: unknown rule id(s) in py-style-ok directive: {rendered}")

        if not reason:
            diagnostics.append(f"{shown}:{row}: py-style-ok directive without a reason is ignored")
            directives.setdefault(row, []).append(directive(None, False))
            continue

        rules = None if "*" in names else frozenset(names & CHECK_RULE_IDS)
        directives.setdefault(row, []).append(directive(rules, True))

    return directives, diagnostics

@dataclass(frozen = True)
class file_directive_record:
    """Parsed '# py-style-ok-file:' exemption of one file."""

    active: bool
    rules: frozenset[str] | None

def parse_file_directive(context: file_context, shown: str) -> tuple[file_directive_record, list[str]]:
    """Parse file-level exemption directives from the header comment block.

    Only comment-only lines before the first code statement are considered. A
    directive without a reason never exempts, and unknown rule ids are
    diagnostics; both cases leave the rest of the directive ineffective.
    """

    diagnostics: list[str] = []
    selected: set[str] = set()
    all_rules = False
    active = False

    for row in sorted(context.comment_rows):
        if (context.first_sig_row is not None) and (row >= context.first_sig_row):
            continue

        match = _FILE_DIRECTIVE_RE.search(context.line(row))

        if match is None:
            continue

        rules_text, separator, reason_text = match.group("body").partition(":")
        reason = reason_text.strip() if separator else ""
        names = {piece.strip() for piece in rules_text.split(",") if piece.strip()}
        unknown = names - CHECK_RULE_IDS - {"*"}

        if unknown:
            rendered = ", ".join(sorted(unknown))
            diagnostics.append(f"{shown}:{row}: unknown rule id(s) in py-style-ok-file directive: {rendered}")

        if not reason:
            diagnostics.append(f"{shown}:{row}: py-style-ok-file directive without a reason is ignored")
            continue

        active = True

        if "*" in names:
            all_rules = True

        selected.update(names & CHECK_RULE_IDS)

    if not active:
        return file_directive_record(False, None), diagnostics

    return file_directive_record(True, None if all_rules else frozenset(selected)), diagnostics

def _issue_is_inline_exempted(issue: issue_record, directives: dict[int, list[directive]]) -> bool:
    for row in (issue.line, issue.line - 1):
        for directive in directives.get(row, ()):
            if not directive.has_reason:
                continue

            if (directive.rules is None) or (issue.rule in directive.rules):
                return True

    return False

def _issue_is_config_exempted(issue: issue_record, path: Path, config: style_config, cwd: Path) -> bool:
    if not config.exempt:
        return False

    variants = _path_variants(path, config, cwd)

    for entry in config.exempt:
        if not _glob_matches(entry.path, variants):
            continue

        if ("*" in entry.rules) or (issue.rule in entry.rules):
            return True

    return False

# ---------------------------------------------------------------------------
# Rule selection and severity
# ---------------------------------------------------------------------------

def _split_rule_options(values: Sequence[str]) -> tuple[str, ...]:
    """Split repeatable, comma-separated rule option values."""

    names: list[str] = []

    for value in values:
        for part in value.split(","):
            name = part.strip()

            if name:
                names.append(name)

    return tuple(names)

def _require_rule_ids(values: Sequence[str], option: str) -> tuple[str, ...]:
    for value in values:
        if value == "*":
            continue

        if value not in CHECK_RULE_IDS:
            raise usage_error(f"unknown rule id '{value}' for {option}; run 'py-style list-rules' for the catalog")

    return tuple(values)

def active_rule_ids(config: style_config, select: Sequence[str], ignore: Sequence[str]) -> tuple[str, ...]:
    """Resolve the enabled rule ids.

    Contract: effective = (CLI --select when given, else the config select when
    given, else every rule) minus (CLI --ignore plus the config ignore). The
    ignore step always runs last, so "--select X --ignore X" disables X, a CLI
    --select replaces the config select list, and a CLI select never lifts the
    config ignore list. Values may be comma-separated and repeated; "*" means
    every rule (also accepted in the config select/ignore lists).
    """

    cli_select = _split_rule_options(select)
    cli_ignore = _split_rule_options(ignore)

    if cli_select:
        selected = set(CHECK_RULE_IDS)if "*" in cli_select else set(_require_rule_ids(cli_select, "--select"))
    elif config.select:
        selected = set(CHECK_RULE_IDS) if "*" in config.select else set(config.select)
    else:
        selected = set(CHECK_RULE_IDS)

    ignored = set(_require_rule_ids(cli_ignore, "--ignore")) | set(config.ignore)

    if "*" in ignored:
        ignored = set(CHECK_RULE_IDS)

    return tuple(rule.id for rule in CHECK_RULES if (rule.id in selected) and (rule.id not in ignored))

def _severities(config: style_config) -> dict[str, str]:
    overrides = dict(config.severity)
    return {rule_id: rule_severity(rule_id, overrides) for rule_id in CHECK_RULE_IDS}

def config_origin(config: style_config) -> str:
    """Return the human-readable origin of the effective configuration."""

    if config.source is None:
        return BUILT_IN_CONFIG_ORIGIN

    return config.source.as_posix()

# ---------------------------------------------------------------------------
# Check engine
# ---------------------------------------------------------------------------

@dataclass
class file_report:
    """Collected result for one scanned file."""

    path: Path
    shown: str
    issues: list[issue_record] = field(default_factory = list)

@dataclass
class check_result:
    """Aggregated result of one check run."""

    checked: int
    skipped: int
    files: list[str]
    issues: list[tuple[str, issue_record]]
    warnings: list[str]
    exempted: int
    rule_count: int
    severities: dict[str, str]
    config_origin: str = BUILT_IN_CONFIG_ORIGIN

def _read_source(path: Path) -> tuple[str, bool, bool]:
    """Read *path* and return (normalized text, used_crlf, decode_error)."""

    data = path.read_bytes()
    used_crlf = b"\r\n" in data
    decode_error = False

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors = "replace")
        decode_error = True

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, used_crlf, decode_error

def run_checks(discovery: discovery, active: Sequence[str], config: style_config) -> check_result:
    """Run all selected rules over the discovered files and filter exemptions."""

    cwd = Path.cwd()
    active_set = set(active)
    reports: dict[Path, file_report] = {}
    warnings: list[str] = []
    contexts: dict[Path, tuple[file_context, dict[int, list[directive]]]] = {}
    file_directives: dict[Path, file_directive_record] = {}
    modules: list[module_info] = []

    for path in discovery.files:
        shown = display_path(path, cwd)
        report = file_report(path, shown)
        reports[path] = report

        try:
            text, _used_crlf, decode_error = _read_source(path)
        except OSError as exc:
            raise usage_error(f"cannot read {shown}: {exc}") from exc

        if decode_error:
            warnings.append(f"{shown}: not valid UTF-8; decoded with replacement characters")

        try:
            context = file_context.build(shown, text)
        except parse_failure as failure:
            report.issues.append(issue_record("parse-error", failure.line, failure.column, f"could not parse source: {failure.message}"))
            continue

        directives, directive_diagnostics = parse_directives(context, shown)
        warnings.extend(directive_diagnostics)
        contexts[path] = (context, directives)
        file_directive, file_diagnostics = parse_file_directive(context, shown)
        warnings.extend(file_diagnostics)
        file_directives[path] = file_directive

        for rule_id in active:
            checker = CHECK_FUNCS.get(rule_id)

            if checker is not None:
                report.issues.extend(checker(context, config))

        if ("snake-case-file" in active_set) and discovery.directory_scan:
            report.issues.extend(check_snake_case_file(path))

        # An f-string token span is opaque on every Python version: 3.11 gives
        # one STRING token, 3.12+ gives FSTRING_START/MIDDLE/END plus the
        # expression tokens. Suppress findings inside the span so both
        # tokenizers agree and a finding can never be unfixable by design.
        if context.fstring_spans:
            report.issues = [issue for issue in report.issues if not context.in_fstring_span(issue.line, issue.column - 1)]

        if ("acyclic-imports" in active_set) and discovery.directory_scan:
            module_name, _package, is_package = module_name_for_path(path)

            if not is_package:
                # Package __init__ modules are excluded: their re-export imports are
                # a documented exception (mapping Q5) and would create false
                # parent/child cycles.
                modules.append(module_info(module_name, path, is_package, tuple(module_import_edges(path, context.tree, module_name, is_package))))

    if ("acyclic-imports" in active_set) and discovery.directory_scan:
        for graph_issue in check_import_cycles(modules):
            shown = display_path(graph_issue.path, cwd)
            report = reports.get(graph_issue.path)

            if report is None:
                report = file_report(graph_issue.path, shown)
                reports[graph_issue.path] = report

            report.issues.append(issue_record("acyclic-imports", graph_issue.line, graph_issue.column, graph_issue.message))

    raw: list[tuple[Path, str, issue_record]] = []

    for path in discovery.files:
        report = reports.get(path)

        if report is None:
            continue

        for issue in report.issues:
            raw.append((path, report.shown, issue))

    for path, report in reports.items():
        if path in discovery.files:
            continue

        for issue in report.issues:
            raw.append((path, report.shown, issue))

    exempted = 0
    flattened: list[tuple[str, issue_record]] = []

    for path, shown, issue in raw:
        file_directive = file_directives.get(path)

        if ((file_directive is not None) and file_directive.active and ((file_directive.rules is None) or (issue.rule in file_directive.rules))):
            exempted += 1
            continue

        if _issue_is_config_exempted(issue, path, config, cwd):
            exempted += 1
            continue

        entry = contexts.get(path)

        if (entry is not None) and _issue_is_inline_exempted(issue, entry[1]):
            exempted += 1
            continue

        flattened.append((shown, issue))

    flattened.sort(key = lambda item: (item[0], item[1].line, item[1].column, item[1].rule))
    return check_result(checked = len(discovery.files), skipped = discovery.skipped, files = sorted(display_path(path, cwd)for path in discovery.files), issues = flattened, warnings = warnings, exempted = exempted, rule_count = len(active), severities = _severities(config), config_origin = config_origin(config))

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _issue_payload(shown: str, issue: issue_record, severities: dict[str, str]) -> dict:
    return {"path": shown, "line": issue.line, "column": issue.column, "rule": issue.rule, "severity": severities.get(issue.rule, "error"), "message": issue.message}

def _summary_line(result: check_result, total: int, errors: int, warning_count: int) -> str:
    return f"py-style: checked {result.checked} file(s), {total} issue(s) " f"({errors} error(s), {warning_count} warning(s)) - {result.rule_count} rule(s)"

def emit_check_report(result: check_result, *, json_output: bool, quiet: bool, max_issues: int, stream_out: object, stream_err: object) -> tuple[int, int]:
    """Write the check report; return (total findings, error count)."""

    severities = result.severities
    total = len(result.issues)
    errors = sum(1 for _shown, issue in result.issues if severities.get(issue.rule, "error") == "error")
    warning_count = total - errors
    truncated = (max_issues > 0) and (total > max_issues)
    reported = result.issues[:max_issues] if truncated else result.issues

    if json_output:
        payload = {"tool": "py-style", "config": result.config_origin, "summary": {"checked": result.checked, "skipped": result.skipped, "findings": total, "errors": errors, "warnings": warning_count, "exempted": result.exempted, "diagnostics": len(result.warnings), "truncated": truncated}, "issues": [_issue_payload(shown, issue, severities)for shown, issue in reported], "warnings": list(result.warnings)}
        json.dump(payload, stream_out, ensure_ascii = False, indent = 2)
        print("", file = stream_out)
        return total, errors

    if not quiet:
        for shown, issue in reported:
            print(f"py-style: {shown}:{issue.line}:{issue.column}: {issue.rule}: {issue.message}", file = stream_err)

        for diagnostic in result.warnings:
            print(f"py-style: warning: {diagnostic}", file = stream_err)

        if truncated:
            print(f"py-style: warning: output truncated to {max_issues} issue(s) by --max-issues", file = stream_err)

    print(_summary_line(result, total, errors, warning_count), file = stream_err)
    return total, errors

# ---------------------------------------------------------------------------
# Fix engine
# ---------------------------------------------------------------------------

def _fix_one_file(path: Path, text: str, fixers: Sequence[str], config: style_config) -> tuple[str | None, list[str], str | None]:
    """Run fixer rounds; return (new text or None, applied rules, fatal reason)."""

    applied: list[str] = []
    current = text

    for _round in range(5):
        changed = False

        for rule_id in fixers:
            try:
                context = file_context.build(str(path), current)
            except parse_failure as failure:
                return None, applied, f"fix pass produced unparseable source: {failure.message}"

            rewritten = FIX_FUNCS[rule_id](context, config)

            if rewritten == current:
                continue

            current = rewritten
            changed = True

            if rule_id not in applied:
                applied.append(rule_id)

        if not changed:
            if current == text:
                return None, applied, None

            return current, applied, None

    return None, applied, "fixes did not converge after 5 rounds"

def _validate_rewrite(original: str, rewritten: str) -> str | None:
    """Check the fixer invariants; return a reason when the rewrite is unsafe."""

    try:
        original_tree = ast.parse(original)
    except SyntaxError:
        return "the original source does not parse"

    try:
        rewritten_tree = ast.parse(rewritten)
    except SyntaxError as exc:
        return f"the rewritten source does not parse: {exc}"

    if ast.dump(rewritten_tree, include_attributes = False) != ast.dump(original_tree, include_attributes = False):
        return "the rewrite would change the AST"

    if string_token_texts(rewritten) != string_token_texts(original):
        return "the rewrite would change string literal tokens"

    return None

def _write_source(path: Path, text: str, used_crlf: bool) -> None:
    payload = text.replace("\n", "\r\n") if used_crlf else text
    path.write_bytes(payload.encode("utf-8"))

# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _load_style_config(paths: Sequence[str], config_path: str | None, no_config: bool) -> style_config:
    try:
        return load_config(paths = paths, explicit = config_path, disabled = no_config)
    except config_error as exc:
        raise usage_error(str(exc)) from exc

def run_check_command(args: argparse.Namespace) -> int:
    """Implement 'py-style check'."""

    paths = args.paths or ["."]
    config = _load_style_config(paths, args.config, args.no_config)
    discovery = discover_files(paths, config)
    active = active_rule_ids(config, args.select or (), args.ignore or ())

    if args.max_issues < 0:
        raise usage_error("--max-issues must be zero or a positive integer")

    result = run_checks(discovery, active, config)
    _total, errors = emit_check_report(result, json_output = args.json_output, quiet = args.quiet, max_issues = args.max_issues, stream_out = sys.stdout, stream_err = sys.stderr)

    if args.warn_only:
        return EXIT_OK

    return EXIT_VIOLATIONS if errors else EXIT_OK

def _scan_roots(paths: Sequence[str]) -> list[Path]:
    """Return the lexical absolute scan roots of this run."""

    return [Path(os.path.abspath(item)) for item in paths]

def _write_refusal_reason(path: Path, scan_roots: Sequence[Path]) -> str | None:
    """Return why *path* must not be written, or None when it is safe.

    The fixer never follows a symbolic link and never writes through a symlinked
    directory component; a resolved path that leaves the scan roots is refused.
    """

    if path.is_symlink():
        return "path is a symbolic link"

    absolute = Path(os.path.abspath(path))
    resolved = absolute.resolve()

    # Compare case-insensitively on Windows so a differently cased drive letter
    # is not mistaken for a symlinked component.
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        return "path resolves through a symbolic link"

    resolved_key = os.path.normcase(str(resolved))

    for root in scan_roots:
        root_key = os.path.normcase(str(Path(os.path.abspath(root))))

        if (resolved_key == root_key) or resolved_key.startswith(root_key + os.sep):
            return None

    return "path is outside the scanned roots"

def run_fix_command(args: argparse.Namespace) -> int:
    """Implement 'py-style fix'."""

    paths = args.paths or ["."]
    config = _load_style_config(paths, args.config, args.no_config)
    discovery = discover_files(paths, config)
    active = active_rule_ids(config, args.select or (), args.ignore or ())
    fixers = [rule_id for rule_id in FIX_ORDER if rule_id in set(active)]
    cwd = Path.cwd()
    rewrites: list[tuple[Path, str, str, bool, list[str]]] = []
    skipped: list[str] = []

    for path in discovery.files:
        shown = display_path(path, cwd)

        try:
            text, used_crlf, decode_error = _read_source(path)
        except OSError as exc:
            raise usage_error(f"cannot read {shown}: {exc}") from exc

        if decode_error:
            skipped.append(f"{shown}: not valid UTF-8")
            continue

        if not text:
            continue

        try:
            context = file_context.build(shown, text)
        except parse_failure:
            skipped.append(f"{shown}: parse error")
            continue

        file_directive, file_diagnostics = parse_file_directive(context, shown)

        if not args.quiet:
            for diagnostic in file_diagnostics:
                print(f"py-style: warning: {diagnostic}", file = sys.stderr)

        active_fixers = fixers

        if file_directive.active:
            active_fixers = [rule_id for rule_id in fixers if (file_directive.rules is not None) and (rule_id not in file_directive.rules)]

            if not active_fixers:
                continue

        rewritten, applied, fatal = _fix_one_file(path, text, active_fixers, config)

        if fatal is not None:
            print(f"py-style: fatal: {shown}: {fatal}", file = sys.stderr)
            return EXIT_ERROR

        if rewritten is None:
            continue

        problem = _validate_rewrite(text, rewritten)

        if problem is not None:
            print(f"py-style: fatal: refusing to write {shown}: {problem}", file = sys.stderr)
            return EXIT_ERROR

        rewrites.append((path, shown, rewritten, used_crlf, applied))

    if not args.dry_run and (len(rewrites) > MAX_FIX_FILES) and not args.allow_many:
        print(f"py-style: error: refusing to rewrite {len(rewrites)} file(s) in one " f"run (limit {MAX_FIX_FILES}); review the change set and rerun with " "--allow-many", file = sys.stderr)
        return EXIT_ERROR

    scan_roots = _scan_roots(paths)
    refused: list[str] = []
    changed_files: list[str] = []

    for path, shown, rewritten, used_crlf, applied in rewrites:
        if not args.dry_run:
            reason = _write_refusal_reason(path, scan_roots)

            if reason is not None:
                refused.append(f"{shown}: {reason}")
                continue

            _write_source(path, rewritten, used_crlf)

        if not args.quiet:
            verb = "would fix" if args.dry_run else "fixed"
            print(f"py-style: {verb} {shown} ({len(applied)} rewrite(s): " + ", ".join(applied) + ")", file = sys.stderr)

        changed_files.append(shown)

    if not args.quiet:
        for note in skipped:
            print(f"py-style: warning: skipped {note}", file = sys.stderr)

        for note in refused:
            print(f"py-style: warning: refusing to write {note}", file = sys.stderr)

    verb = "would fix" if args.dry_run else "fixed"
    print(f"py-style: {verb} {len(changed_files)} file(s)", file = sys.stderr)
    refresh = run_checks(discovery, active, config)
    emit_check_report(refresh, json_output = False, quiet = True, max_issues = 0, stream_out = sys.stdout, stream_err = sys.stderr)
    return EXIT_OK

def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width = width) or [""]

def run_list_rules_command(args: argparse.Namespace) -> int:
    """Implement 'py-style list-rules'."""

    if args.json_output:
        payload = {"tool": "py-style", "rules": [{"id": rule.id, "title": rule.title, "p_refs": list(rule.p_refs), "severity": rule.severity, "fixable": rule.fixable, "description": rule.description}for rule in CHECK_RULES], "manual": [{"id": manual.id, "title": manual.title, "p_refs": list(manual.p_refs), "description": manual.description}for manual in MANUAL_RULES], "builtin": [{"id": rule.id, "title": rule.title, "severity": rule.severity, "fixable": rule.fixable, "description": rule.description}for rule in BUILTIN_RULES]}
        json.dump(payload, sys.stdout, ensure_ascii = False, indent = 2)
        print("", file = sys.stdout)
        return EXIT_OK

    count = len(CHECK_RULES)
    print(f"py-style rule catalog: {count} check rule(s), " f"{len(MANUAL_RULES)} manual rule set(s), {len(BUILTIN_RULES)} built-in", file = sys.stdout)
    print("", file = sys.stdout)
    print(f"{'id':<26} {'P refs':<16} {'severity':<9} {'autofix':<8} summary", file = sys.stdout)
    print("-" * 100, file = sys.stdout)

    for rule in CHECK_RULES:
        p_refs = ",".join(rule.p_refs) if rule.p_refs else "-"
        fixable = "yes" if rule.fixable else "no"
        print(f"{rule.id:<26} {p_refs:<16} {rule.severity:<9} {fixable:<8} {rule.title}", file = sys.stdout)

        for line in _wrap(rule.description, 92):
            print(f"    {line}", file = sys.stdout)

    print("", file = sys.stdout)
    print("Built-in", file = sys.stdout)
    print("-" * 100, file = sys.stdout)

    for rule in BUILTIN_RULES:
        print(f"{rule.id:<26} {'-':<16} {rule.severity:<9} {'no':<8} {rule.title}", file = sys.stdout)

        for line in _wrap(rule.description, 92):
            print(f"    {line}", file = sys.stdout)

    print("", file = sys.stdout)
    print("Manual rules (cannot be machine-checked)", file = sys.stdout)
    print("-" * 100, file = sys.stdout)

    for manual in MANUAL_RULES:
        p_refs = ",".join(manual.p_refs)
        print(f"{manual.id:<26} {p_refs:<16} {'review':<9} {'no':<8} {manual.title}", file = sys.stdout)

        for line in _wrap(manual.description, 92):
            print(f"    {line}", file = sys.stdout)

    return EXIT_OK

# ---------------------------------------------------------------------------
# Argument parser and entry point
# ---------------------------------------------------------------------------

def _add_common_rule_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--select", action = "append", metavar = "RULE[,RULE]", default = [], help = "only run these rules; comma-separated and repeatable; '*' = every rule")
    parser.add_argument("--ignore", action = "append", metavar = "RULE[,RULE]", default = [], help = "skip these rules; comma-separated and repeatable; '*' = every rule")
    parser.add_argument("--config", metavar = "PATH", default = None, help = "explicit config file (overrides repo py-style.toml / pyproject.toml)")
    parser.add_argument("--no-config", action = "store_true", help = "ignore configuration files and use the built-in defaults")

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the py-style command."""

    parser = argparse.ArgumentParser(prog = "py-style", description = "Check Python sources against the org's custom layout rules.", epilog = "Findings and summaries go to stderr; --json writes to stdout. " "Exit codes: 0 clean/warn-only, 1 violations, 2 usage/config/IO error.")
    subparsers = parser.add_subparsers(dest = "command", metavar = "COMMAND")

    check = subparsers.add_parser("check", help = "report style violations")
    check.add_argument("paths", nargs = "*", default = [], help = "files or directories")
    check.add_argument("--json", action = "store_true", dest = "json_output", help = "JSON to stdout")
    check.add_argument("--warn-only", action = "store_true", help = "always exit 0 unless fatal")
    check.add_argument("--max-issues", type = int, default = 0, metavar = "N", help = "stop after N issues (0 = no limit)")
    check.add_argument("--quiet", action = "store_true", help = "only print the summary")
    _add_common_rule_options(check)

    fix = subparsers.add_parser("fix", help = "apply the safe auto-fixes in place", description = "Apply the safe auto-fixes in place (never writes through symlinks).", epilog = "Exit code 0 after applying safe fixes; remaining findings are reported, " "use 'py-style check' for a failing gate.")
    fix.add_argument("paths", nargs = "*", default = [], help = "files or directories")
    fix.add_argument("--dry-run", action = "store_true", help = "report changes without writing")
    fix.add_argument("--quiet", action = "store_true", help = "only print the summary")
    fix.add_argument("--allow-many", action = "store_true", help = "allow rewriting more than %d files in one run" % MAX_FIX_FILES)
    _add_common_rule_options(fix)

    list_rules = subparsers.add_parser("list-rules", help = "print the rule catalog")
    list_rules.add_argument("--json", action = "store_true", dest = "json_output", help = "JSON to stdout")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""

    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and (arguments[0] in ("-h", "--help", "help")):
        print(_USAGE, file = sys.stdout)
        return EXIT_OK

    if not arguments:
        print(_USAGE, file = sys.stderr)
        return EXIT_ERROR

    parser = build_parser()

    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else EXIT_ERROR

    if parsed.command is None:
        print(_USAGE, file = sys.stderr)
        return EXIT_ERROR

    try:
        if parsed.command == "check":
            return run_check_command(parsed)

        if parsed.command == "fix":
            return run_fix_command(parsed)

        if parsed.command == "list-rules":
            return run_list_rules_command(parsed)
    except usage_error as exc:
        print(f"py-style: error: {exc}", file = sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"py-style: error: {exc}", file = sys.stderr)
        return EXIT_ERROR

    print(f"py-style: error: unknown command '{parsed.command}'", file = sys.stderr)
    return EXIT_ERROR

if __name__ == "__main__":
    raise SystemExit(main())
