#!/usr/bin/env python3
"""Fail a workflow file that GitHub would silently refuse to run.

*Why this exists:* on 2026-09-15 a batch of PRs added ``timeout-minutes`` to
reusable-workflow **caller** jobs (``jobs.<id>.uses``). That key is not in the caller key
set — ``name`` / ``uses`` / ``with`` / ``secrets`` / ``strategy`` / ``needs`` / ``if`` /
``concurrency`` / ``permissions`` — so GitHub did not merely drop an upper bound: it
rejected the **whole file**, created **no job at all**, and left behind a run whose name is
the file path, whose ``jobs_total`` is ``0`` and whose ``created_at`` equals
``updated_at``. Ten workflows across eight repositories stopped running on ``master`` and
nothing went red, because a workflow that never creates a job has no check that can fail.
This CLI makes that shape loud.

*A second, related fact:* a caller job **cannot** express a timeout upper bound at all. The
only place an upper bound can live is the callee — the reusable workflow named by ``uses:``,
on its self-hosted job. Rules 2 and 3 therefore fail a self-hosted job that has none.

*Rules* (each finding carries ``path:line`` and a recomputable criterion; all four are
``error`` severity today):

``invalid-caller-key``
    a job that has ``uses:`` **and** a key outside the nine caller keys — the incident.
``callee-missing-timeout``
    a job whose ``runs-on`` contains ``self-hosted``, inside a workflow whose ``on``
    includes ``workflow_call``, with no ``timeout-minutes``.
``self-hosted-missing-timeout``
    the same shape in a workflow that is not a ``workflow_call`` callee.
``yaml-parse-error``
    the file cannot be parsed as YAML. This rule reports the **file** (with the first line
    quoted); it never skips it. ``sysl/.github/workflows/validate.yml`` is a real workflow
    flattened onto one line, and every auditor that starts by parsing has been blind to it.

*Structure:* the workflow AST is composed with PyYAML (a declared runtime dependency), so
line numbers come from node marks and validity comes from the composer itself — a file that
cannot be parsed cannot be walked, which is exactly why rule 4 exists as its own rule.

*Paths:* ``PATHS`` default to ``.github/workflows`` and may be files or directories. A
directory containing ``.github/workflows`` is read as a repository root; any other
directory is read as a workflows directory. Reported paths are relative to the current
directory when the file lives underneath it (a run from a repository root prints
repository-relative paths) and absolute otherwise — so an org-wide run from the workspace
root reports ``sysl/.github/workflows/validate.yml`` and can match ``--allow ...=sysl/*``.

*Allowlist:* ``--allow RULE=GLOB`` (repeatable) suppresses matching findings, where ``RULE``
is one of the four rule names or ``*`` and ``GLOB`` is matched with :func:`fnmatch.fnmatchcase`
against the reported path (``*`` crosses ``/``). Suppressions are counted and echoed on
stderr — an exemption is never silent — and an unknown rule name is a usage error rather
than a silent pass.

*Exit codes:* ``0`` clean | ``1`` at least one error (or a warning under ``--strict``)
| ``2`` usage or I/O error (a missing PATH, an unknown ``--allow`` rule).

Usage::

    celestia-ci-audit
    celestia-ci-audit .github/workflows/ci.yml --json
    celestia-ci-audit */ --strict
    celestia-ci-audit --allow self-hosted-missing-timeout=sysl/*
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import yaml
    from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
except ImportError as exc:  # pragma: no cover - PyYAML is a declared runtime dependency
    raise ImportError(
        "PyYAML is required by celestia-ci-audit: the `yaml-parse-error` rule must report a "
        "file that cannot be parsed instead of silently skipping it, and node line numbers "
        "come from the composer. Install the package itself (PyYAML>=6) rather than adding "
        "this module to an environment by hand."
    ) from exc


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

RULE_INVALID_CALLER_KEY = "invalid-caller-key"
RULE_CALLEE_MISSING_TIMEOUT = "callee-missing-timeout"
RULE_SELF_HOSTED_MISSING_TIMEOUT = "self-hosted-missing-timeout"
RULE_YAML_PARSE_ERROR = "yaml-parse-error"

RULE_NAMES: Tuple[str, ...] = (
    RULE_INVALID_CALLER_KEY,
    RULE_CALLEE_MISSING_TIMEOUT,
    RULE_SELF_HOSTED_MISSING_TIMEOUT,
    RULE_YAML_PARSE_ERROR,
)

# 今天四条规则全是 error；severity 与 --strict 是留给将来 warning 级规则的机制
# （判据在 has_failure 里，测试用合成 finding 覆盖），不是死代码。
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: The **complete** set of keys GitHub accepts on a ``jobs.<id>.uses`` caller job.
#: Anything else makes the whole workflow invalid and creates zero jobs.
CALLER_LEGAL_KEYS: Tuple[str, ...] = (
    "name",
    "uses",
    "with",
    "secrets",
    "strategy",
    "needs",
    "if",
    "concurrency",
    "permissions",
)

#: Label substring that marks a job as running on the org's self-hosted farm.
SELF_HOSTED_LABEL = "self-hosted"

#: The ``on`` member that turns a workflow into a reusable-workflow callee.
WORKFLOW_CALL_KEY = "workflow_call"

#: Legal-key list as it appears in messages (kept in one place so it cannot drift).
_CALLER_KEY_LIST = "/".join(CALLER_LEGAL_KEYS)

DEFAULT_PATHS: Tuple[str, ...] = (".github/workflows",)
WORKFLOW_SUFFIXES: Tuple[str, ...] = (".yml", ".yaml")

#: First-line summary length for the ``yaml-parse-error`` message and every excerpt.
SUMMARY_LIMIT = 120
EXCERPT_LIMIT = 100


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One audit finding.

    ``excerpt`` is the offending source line and belongs to the **human** output only:
    the ``--json`` contract is the fixed five keys in :meth:`to_json`.
    """

    path: str
    line: int
    rule: str
    severity: str
    message: str
    excerpt: str = ""

    def render(self) -> str:
        """Render as ``path:line: [rule] message`` plus an indented excerpt."""
        head = f"{self.path}:{self.line}: [{self.rule}] {self.message}"
        if self.excerpt:
            return f"{head}\n    {self.excerpt}"
        return head

    def to_json(self) -> Dict[str, object]:
        """The frozen JSON shape CI consumers read (no ``excerpt``, no extras)."""
        return {
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
        }


def has_failure(findings: Sequence[Finding], strict: bool = False) -> bool:
    """Whether the run must exit non-zero: any error, or (with *strict*) any warning.

    Every rule is an ``error`` today, so ``--strict`` is inert in practice; the predicate is
    still the single place the exit-code rule lives, and is covered with a synthetic warning.
    """
    for finding in findings:
        if finding.severity == SEVERITY_ERROR:
            return True
        if strict and finding.severity == SEVERITY_WARNING:
            return True
    return False


# ---------------------------------------------------------------------------
# YAML 结构（compose 出来的节点树，带真实行号）
# ---------------------------------------------------------------------------


def _scalar_text(node: Optional[Node]) -> Optional[str]:
    """The text of a scalar node, or ``None`` for anything else.

    ``compose`` keeps the **raw** text (``on`` stays ``"on"`` rather than becoming the
    boolean ``True`` the way :func:`yaml.safe_load` would resolve it), which is why the
    top-level ``on:`` lookup below can compare strings.
    """
    if isinstance(node, ScalarNode):
        return node.value
    return None


def _mapping_value(node: Optional[Node], name: str) -> Optional[Node]:
    """First value under mapping key *name*, or ``None``."""
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if _scalar_text(key_node) == name:
            return value_node
    return None


def _scalar_labels(node: Optional[Node]) -> List[str]:
    """Read a ``runs-on`` value as a label list (scalar, flow list and block list alike)."""
    if isinstance(node, ScalarNode):
        return [node.value]
    if isinstance(node, SequenceNode):
        return [child.value for child in node.value if isinstance(child, ScalarNode)]
    return []


def _declares_workflow_call(on_node: Optional[Node]) -> bool:
    """Whether the top-level ``on`` includes ``workflow_call``.

    All three shapes GitHub accepts are covered::

        on: workflow_call
        on: [push, workflow_call]
        on:
          workflow_call:
    """
    if isinstance(on_node, ScalarNode):
        return on_node.value == WORKFLOW_CALL_KEY
    if isinstance(on_node, SequenceNode):
        return any(
            _scalar_text(child) == WORKFLOW_CALL_KEY for child in on_node.value
        )
    if isinstance(on_node, MappingNode):
        return any(
            _scalar_text(key_node) == WORKFLOW_CALL_KEY for key_node, _ in on_node.value
        )
    return False


def _job_entries(body: MappingNode) -> Dict[str, Tuple[Node, Node]]:
    """Direct keys of one ``jobs.<id>`` mapping as ``{key: (key_node, value_node)}``.

    Document order is preserved and the first occurrence wins. Only job-level keys are
    returned: a ``steps:`` entry such as ``- uses: actions/checkout`` is a child of the job
    body and is never mistaken for a caller job. Both nodes are kept because the key node
    carries the line to report and the value node carries the data to judge.
    """
    entries: Dict[str, Tuple[Node, Node]] = {}
    for key_node, value_node in body.value:
        name = _scalar_text(key_node)
        if name is not None and name not in entries:
            entries[name] = (key_node, value_node)
    return entries


def _one_line(text: str, limit: int) -> str:
    """Collapse *text* to a single line, truncated to *limit* characters."""
    flattened = " ".join(text.split())
    if len(flattened) > limit:
        flattened = flattened[:limit] + "..."
    return flattened


def _first_line_summary(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """A short, single-line summary of the file's first line (rule 4 must quote the file)."""
    return _one_line(text.split("\n", 1)[0], limit)


def _line_excerpt(lines: Sequence[str], line: int, limit: int = EXCERPT_LIMIT) -> str:
    """The stripped source line (1-based), truncated; empty when out of range."""
    if 1 <= line <= len(lines):
        return _one_line(lines[line - 1].strip(), limit)
    return ""


def _line_of(node: Node) -> int:
    """1-based line of a node (PyYAML marks are 0-based)."""
    return node.start_mark.line + 1


def _parse_error_finding(exc: yaml.YAMLError, text: str, path: str) -> Finding:
    """Rule 4: report the file itself, with the composer's own words and its first line."""
    problem = getattr(exc, "problem", None) or "unparsable YAML"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        line = 1
        where = ""
    else:
        line = mark.line + 1
        where = f" at line {mark.line + 1}, column {mark.column + 1}"
    summary = _first_line_summary(text)
    message = (
        f"file is not valid YAML ({problem}{where}); first line: {summary!r}; "
        "a file that cannot be parsed is reported here rather than skipped - "
        "GitHub refuses such a workflow outright, so it creates no job that could fail"
    )
    return Finding(
        path=path,
        line=line,
        rule=RULE_YAML_PARSE_ERROR,
        severity=SEVERITY_ERROR,
        message=message,
        excerpt=_line_excerpt(text.split("\n"), line),
    )


def _caller_findings(
    job_id: str, entries: Dict[str, Tuple[Node, Node]], path: str, lines: Sequence[str],
) -> List[Finding]:
    """Rule 1: a caller job may only carry the nine legal keys; name every offender."""
    findings: List[Finding] = []
    for key, (key_node, _value_node) in entries.items():
        if key in CALLER_LEGAL_KEYS:
            continue
        line = _line_of(key_node)
        message = (
            f"job {job_id!r} calls a reusable workflow (uses:) and also sets {key!r}, "
            f"which is not one of the nine caller keys ({_CALLER_KEY_LIST}); GitHub then "
            "rejects the whole workflow - no job is created and the run degenerates to "
            "0 jobs without failing"
        )
        findings.append(
            Finding(
                path=path,
                line=line,
                rule=RULE_INVALID_CALLER_KEY,
                severity=SEVERITY_ERROR,
                message=message,
                excerpt=_line_excerpt(lines, line),
            )
        )
    return findings


def _missing_timeout_finding(
    job_id: str,
    runs_on_key: Node,
    runs_on_value: Node,
    is_callee: bool,
    path: str,
    lines: Sequence[str],
) -> Finding:
    """Rules 2 and 3: a self-hosted job without ``timeout-minutes``."""
    labels = ", ".join(_scalar_labels(runs_on_value))
    if is_callee:
        rule = RULE_CALLEE_MISSING_TIMEOUT
        message = (
            f"job {job_id!r} is self-hosted (runs-on: {labels}) in a workflow_call callee "
            "and has no timeout-minutes; a caller job cannot set one (it is not in the "
            "caller key set), so the upper bound can only live here"
        )
    else:
        rule = RULE_SELF_HOSTED_MISSING_TIMEOUT
        message = (
            f"job {job_id!r} is self-hosted (runs-on: {labels}) and has no timeout-minutes; "
            "GitHub's 360-minute default cannot stop a process stuck in the kernel D state, "
            "so a wedged job holds one of the four runners until a human intervenes"
        )
    line = _line_of(runs_on_key)
    return Finding(
        path=path,
        line=line,
        rule=rule,
        severity=SEVERITY_ERROR,
        message=message,
        excerpt=_line_excerpt(lines, line),
    )


def audit_text(text: str, path: str) -> List[Finding]:
    """Audit one workflow's text; *path* is the already-resolved display path."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        # 规则 4 的意义就在这一条 except：解析失败必须变成 finding，绝不能 continue 跳过。
        return [_parse_error_finding(exc, text, path)]

    if not isinstance(root, MappingNode):
        # 空文档（compose 返回 None）或非映射根：没有 jobs 可查，也没有解析错误。
        return []

    jobs = _mapping_value(root, "jobs")
    if not isinstance(jobs, MappingNode):
        return []

    is_callee = _declares_workflow_call(_mapping_value(root, "on"))
    lines = text.split("\n")
    findings: List[Finding] = []

    for key_node, body in jobs.value:
        job_id = _scalar_text(key_node) or "<unnamed>"
        if not isinstance(body, MappingNode):
            continue
        entries = _job_entries(body)

        if "uses" in entries:
            # caller 作业：只查合法键集。它的 runner 由 callee 决定，因此不再套用
            # 自托管规则（否则 `uses` + `runs-on` 会被重复计一次）。
            findings.extend(_caller_findings(job_id, entries, path, lines))
            continue

        runs_on = entries.get("runs-on")
        if runs_on is None or "timeout-minutes" in entries:
            continue
        runs_on_key, runs_on_value = runs_on
        labels = _scalar_labels(runs_on_value)
        if not any(SELF_HOSTED_LABEL in label.lower() for label in labels):
            continue
        findings.append(
            _missing_timeout_finding(job_id, runs_on_key, runs_on_value, is_callee, path, lines)
        )

    return findings


def audit_file(path: Path, display: str) -> List[Finding]:
    """Audit one file; a read failure is either a finding (bad encoding) or an ``OSError``."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        message = (
            f"file is not valid UTF-8 ({exc}); it is reported here rather than skipped, "
            "because a workflow GitHub cannot read creates no job that could fail"
        )
        return [
            Finding(
                path=display,
                line=1,
                rule=RULE_YAML_PARSE_ERROR,
                severity=SEVERITY_ERROR,
                message=message,
                excerpt="",
            )
        ]
    return audit_text(text, display)


# ---------------------------------------------------------------------------
# 文件遍历
# ---------------------------------------------------------------------------


def _yml_files(directory: Path) -> List[Path]:
    """Sorted ``*.yml`` / ``*.yaml`` files directly inside *directory* (never recursive)."""
    if not directory.is_dir():
        return []
    found = [item for item in directory.glob("*.yml") if item.is_file()]
    found += [item for item in directory.glob("*.yaml") if item.is_file()]
    return sorted(found)


def _files_for_directory(directory: Path) -> List[Path]:
    """A directory holding ``.github/workflows`` is a repository root; anything else is one."""
    nested = directory / ".github" / "workflows"
    if nested.is_dir():
        return _yml_files(nested)
    return _yml_files(directory)


def _display_path(path: Path, cwd: Path) -> str:
    """Path relative to *cwd* when it lives underneath it, otherwise the resolved absolute path."""
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - resolve() rarely fails on a readable path
        return str(path)
    try:
        return resolved.relative_to(cwd).as_posix()
    except ValueError:
        return str(resolved)


def collect_workflow_files(
    raw_paths: Sequence[str], cwd: Optional[Path] = None,
) -> Tuple[List[Tuple[Path, str]], Optional[int]]:
    """Resolve PATHS into ``[(path, display_path)]``; a missing PATH is a usage error (exit 2).

    Duplicates (the same real file reached through two arguments) are collapsed, so a
    repository-root argument and an explicit file argument cannot double-report.
    """
    root = (cwd or Path.cwd()).resolve()
    if not raw_paths:
        raw_paths = list(DEFAULT_PATHS)

    collected: List[Tuple[Path, str]] = []
    seen: set = set()
    for raw in raw_paths:
        candidate = Path(raw)
        if not candidate.exists():
            print(f"error: {raw}: no such file or directory", file=sys.stderr)
            return [], 2
        try:
            if candidate.is_dir():
                files = _files_for_directory(candidate)
            else:
                files = [candidate]
        except OSError as exc:
            # 目录不可读时必须停：静默跳过等于把该目录下的 workflow 全部漏检，
            # 而本工具存在的理由就是"未创建 job 的失败没有 check 会变红"。
            print(f"error: {raw}: {exc}", file=sys.stderr)
            return [], 2
        for path in files:
            try:
                key = path.resolve()
            except OSError:  # pragma: no cover - resolve() rarely fails on an existing path
                key = path
            if key in seen:
                continue
            seen.add(key)
            collected.append((path, _display_path(path, root)))
    collected.sort(key=lambda item: item[1])
    return collected, None


# ---------------------------------------------------------------------------
# --allow 允许列表
# ---------------------------------------------------------------------------


def parse_allow(raw: str) -> Tuple[str, str]:
    """Parse ``--allow RULE=GLOB``; an unknown RULE is rejected rather than silently ignored."""
    rule, separator, pattern = raw.partition("=")
    rule = rule.strip()
    pattern = pattern.strip()
    if not separator or not rule or not pattern:
        raise argparse.ArgumentTypeError(f"expected RULE=GLOB, got {raw!r}")
    if rule != "*" and rule not in RULE_NAMES:
        raise argparse.ArgumentTypeError(
            f"unknown rule {rule!r} (expected one of {', '.join(RULE_NAMES)}, or *)"
        )
    return rule, pattern


def _is_allowed(finding: Finding, rules: Sequence[Tuple[str, str]]) -> bool:
    """Whether any ``--allow`` entry covers this finding (``*`` matches every rule)."""
    return any(
        (rule == "*" or rule == finding.rule) and fnmatch.fnmatchcase(finding.path, pattern)
        for rule, pattern in rules
    )


def apply_allow(
    findings: Sequence[Finding], rules: Sequence[Tuple[str, str]],
) -> Tuple[List[Finding], int]:
    """Split findings into ``(reported, suppressed_count)``."""
    reported = [finding for finding in findings if not _is_allowed(finding, rules)]
    return reported, len(findings) - len(reported)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def build_report(
    findings: Sequence[Finding],
    files: int,
    suppressed: int,
    strict: bool,
) -> Dict[str, object]:
    """The frozen ``--json`` document: ``findings`` (five keys each) plus a summary."""
    rules = {name: 0 for name in RULE_NAMES}
    errors = 0
    warnings = 0
    for finding in findings:
        rules[finding.rule] = rules.get(finding.rule, 0) + 1
        if finding.severity == SEVERITY_ERROR:
            errors += 1
        elif finding.severity == SEVERITY_WARNING:
            warnings += 1
    return {
        "findings": [finding.to_json() for finding in findings],
        "summary": {
            "files": files,
            "findings": len(findings),
            "errors": errors,
            "warnings": warnings,
            "suppressed": suppressed,
            "failed": has_failure(findings, strict),
            "rules": rules,
        },
    }


def _summary_line(files: int, findings: Sequence[Finding], suppressed: int) -> str:
    """Human summary line (stderr under ``--json``)."""
    if not findings:
        text = f"clean ({files} workflow file(s) scanned"
        if suppressed:
            text += f", {suppressed} finding(s) suppressed by --allow"
        return f"celestia-ci-audit: {text})"
    errors = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
    warnings = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    return (
        f"celestia-ci-audit: {len(findings)} finding(s) in {files} workflow file(s) "
        f"(errors: {errors}, warnings: {warnings}, suppressed: {suppressed})"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(
        prog="celestia-ci-audit",
        description=(
            "Fail GitHub Actions workflows that GitHub would silently refuse to run: "
            "illegal keys on reusable-workflow caller jobs (jobs.<id>.uses), self-hosted "
            "jobs without timeout-minutes, and files that are not valid YAML. The caller "
            "side cannot express a timeout upper bound; it belongs on the callee."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="workflow files or directories (default: .github/workflows)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit the fixed machine-readable report on stdout",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warning-severity findings as failures too (every rule is an error today)",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        type=parse_allow,
        metavar="RULE=GLOB",
        help=(
            "suppress findings of RULE whose reported path matches GLOB "
            "(RULE is one of the four rule names or *; repeatable)"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns 0 clean / 1 findings / 2 usage or I/O error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    files, failure = collect_workflow_files(args.paths)
    if failure is not None:
        return failure

    findings: List[Finding] = []
    for path, display in files:
        try:
            findings.extend(audit_file(path, display))
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    findings.sort(key=lambda item: (item.path, item.line, item.rule))
    reported, suppressed = apply_allow(findings, args.allow)

    if args.as_json:
        report = build_report(reported, len(files), suppressed, args.strict)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for finding in reported:
            print(finding.render())
        if not files:
            print(
                f"note: no workflow file found under {', '.join(args.paths or DEFAULT_PATHS)}",
                file=sys.stderr,
            )

    if suppressed:
        # 豁免必须留痕：允许列表不能变成静默通过。
        rules_text = ", ".join(f"{rule}={pattern}" for rule, pattern in args.allow)
        print(
            f"note: suppressed {suppressed} finding(s) via --allow ({rules_text})",
            file=sys.stderr,
        )

    print(
        _summary_line(len(files), reported, suppressed),
        file=sys.stderr if args.as_json else sys.stdout,
    )
    return 1 if has_failure(reported, args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
