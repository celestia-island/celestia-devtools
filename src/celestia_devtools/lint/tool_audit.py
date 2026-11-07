#!/usr/bin/env python3
"""Audit tracked shell scripts against the workspace tool-exemption ledger.

*Why this exists:* AGENTS §7 requires every ``.sh`` / ``.ps1`` tool script to be
Python 3 unless a platform need is registered in ``_ledger/tool-exemptions.md``.
Nothing read that ledger, so the rule was prose only and unregistered scripts
accumulated — the 2026-10-25 structural audit found 22+ of them (evernight 7,
gateway 6, plana 5, noa 3, sites 1 each, ...). This command reads the ledger and
reports every tracked script that has no registered reason.

*Ledger:* the machine-readable block between ``<!-- tool-audit:begin -->`` and
``<!-- tool-audit:end -->``, holding one TOML fence::

    [exempt]
    "_hooks/pre-commit" = "git hook"
    "*.ps1" = "Windows-only platform scripts"

    [exempt_repos]
    kei = "frozen repository"

``[exempt]`` keys are workspace-relative paths matched with
:func:`fnmatch.fnmatchcase` (``*`` also crosses ``/``) and every reason must be
non-empty. ``[exempt_repos]`` exempts every script in a repository by name.

*Scan:* every immediate child of the workspace that is a git checkout is asked
for its tracked files (``git ls-files``), so untracked build output never
counts. The workspace-level ``_tools/`` and ``_hooks/`` directories are scanned
from the filesystem (they are not part of a checkout), which is where the
policy's own shims live. Only ``.sh`` and ``.ps1`` are audited; an extensionless
script (for example the office-render entry point) is out of scope by policy.

*Exit codes:* ``0`` clean (or ``--warn-only``), ``1`` unregistered scripts,
``2`` usage / ledger / scan error. The gate is fail-closed: a missing,
unparsable or empty ledger, and a workspace with no git checkout, are errors
rather than passes.

Usage::

    celestia-devtools tool-audit
    celestia-devtools tool-audit /mnt/codespace --json
    celestia-devtools tool-audit --warn-only
"""

from __future__ import annotations
import argparse
import fnmatch
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Sequence, Tuple
from celestia_devtools.core import envelope

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # Python 3.9/3.10
    try:
        import tomli as _toml
    except ImportError:  # pragma: no cover - no TOML parser available
        _toml = None

BEGIN_MARKER = "<!-- tool-audit:begin -->"
END_MARKER = "<!-- tool-audit:end -->"
AUDITED_SUFFIXES = (".sh", ".ps1")
LEDGER_RELATIVE = Path("_ledger") / "tool-exemptions.md"
WORKSPACE_SCRIPT_ROOTS = ("_tools", "_hooks")

class tool_audit_error(Exception):
    """A ledger or scan problem that must fail the gate closed."""

@dataclass
class finding:
    path: str
    repo: str
    kind: str

    def to_dict(self) -> dict:
        return {"path": self.path, "repo": self.repo, "kind": self.kind}

@dataclass
class report:
    workspace: str
    ledger: str
    repositories: int = 0
    checked: int = 0
    exempted: int = 0
    findings: List[finding] = field(default_factory = list)
    errors: List[str] = field(default_factory = list)

    @property
    def ok(self) -> bool:
        return not self.findings and not self.errors

    def to_dict(self) -> dict:
        return {"workspace": self.workspace, "ledger": self.ledger, "repositories": self.repositories, "checked": self.checked, "exempted": self.exempted, "findings": [finding.to_dict() for finding in self.findings], "errors": list(self.errors), "ok": self.ok}

def ledger_path_for(workspace: Path, override: str | None = None) -> Path:
    """Resolve the ledger: explicit override, then ``<workspace>/_ledger/...``."""

    if override:
        return Path(override).absolute()

    return (workspace / LEDGER_RELATIVE).absolute()

def load_ledger(path: Path) -> Tuple[dict, dict]:
    """Parse ``[exempt]`` / ``[exempt_repos]`` from the marked TOML fence."""

    if not path.is_file():
        raise tool_audit_error("tool-exemptions ledger not found at %s (pass --ledger)" % path)

    text = path.read_bytes().decode("utf-8", errors = "replace")
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)

    if (start < 0) or (end < 0) or (end <= start):
        raise tool_audit_error("ledger has no %s ... %s block" % (BEGIN_MARKER, END_MARKER))

    block = text[start + len(BEGIN_MARKER):end]
    toml_text = _extract_toml_fence(block)

    if _toml is None:
        raise tool_audit_error("no TOML parser available (python < 3.11 without tomli)")

    try:
        data = _toml.loads(toml_text)
    except Exception as exc:
        raise tool_audit_error("ledger machine block is not valid TOML: %s: %s" % (type(exc).__name__, exc)) from exc

    exempt = _validate_table(data, "exempt")
    exempt_repos = _validate_table(data, "exempt_repos")

    if not exempt and not exempt_repos:
        raise tool_audit_error("ledger machine block is empty (no [exempt] or [exempt_repos] entries)")

    return exempt, exempt_repos

def _extract_toml_fence(block: str) -> str:
    """Return the text of the first fenced code block (``````toml`````` or `````````)."""
    lines = block.splitlines()
    fence_start = None

    for index, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("```"):
            fence_start = index
            break

    if fence_start is None:
        raise tool_audit_error("ledger machine block has no fenced TOML block")

    collected: List[str] = []

    for line in lines[fence_start + 1:]:
        if line.strip().startswith("```"):
            return "\n".join(collected)

        collected.append(line)

    raise tool_audit_error("ledger machine block fence is not closed")

def _validate_table(data: dict, key: str) -> dict:
    table = data.get(key, {})

    if table is None:
        return {}

    if not isinstance(table, dict):
        raise tool_audit_error("ledger [%s] must be a table" % key)

    for entry_key, reason in table.items():
        if not isinstance(reason, str) or not reason.strip():
            raise tool_audit_error("ledger [%s] entry %r has no reason" % (key, entry_key))

    return table

def discover_repositories(workspace: Path) -> List[Path]:
    """Immediate git checkouts under *workspace* (``_``-prefixed dirs skipped)."""
    repositories: List[Path] = []

    for child in sorted(workspace.iterdir(), key = lambda item: item.name):
        if child.name.startswith("_"):
            continue

        try:
            is_git = child.is_dir() and (child / ".git").exists()
        except OSError:
            continue

        if is_git:
            repositories.append(child)

    return repositories

def tracked_scripts(repo: Path) -> List[str]:
    """Tracked ``.sh`` / ``.ps1`` paths in *repo* (empty when ``git`` fails)."""

    try:
        result = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output = True, timeout = 120)
    except subprocess.TimeoutExpired as exc:
        raise tool_audit_error("git ls-files timed out in %s" % repo) from exc

    if result.returncode != 0:
        raise tool_audit_error("git ls-files failed in %s: %s" % (repo, result.stderr.decode("utf-8", errors = "replace").strip()))

    scripts: List[str] = []

    for raw in result.stdout.decode("utf-8", errors = "replace").split("\0"):
        if not raw:
            continue

        if raw.endswith(AUDITED_SUFFIXES):
            scripts.append(raw)

    return sorted(scripts)

def workspace_scripts(workspace: Path) -> List[Tuple[str, str]]:
    """``(label, relative_path)`` for scripts under the workspace-level roots.

    ``_tools/`` and ``_hooks/`` are not checkouts, so ``git ls-files`` cannot
    see them; the policy still applies to their scripts (the ledger's own
    ``_tools/*.sh`` rows are the evidence).
    """
    found: List[Tuple[str, str]] = []

    for label in WORKSPACE_SCRIPT_ROOTS:
        root = workspace / label

        if not root.is_dir():
            continue

        for path in sorted(root.rglob("*")):
            if path.is_file() and (path.suffix in AUDITED_SUFFIXES):
                found.append((label, path.relative_to(workspace).as_posix()))

    return found

def match_exempt(relative_path: str, repo_name: str, exempt: dict, exempt_repos: dict) -> str | None:
    """The registered reason for *relative_path*, or ``None`` when unregistered."""

    if repo_name in exempt_repos:
        return exempt_repos[repo_name]

    for pattern, reason in exempt.items():
        if fnmatch.fnmatchcase(relative_path, pattern):
            return reason

    return None

def audit(workspace: Path, ledger: str | None = None) -> report:
    """Scan the workspace and return a :class:`report` (raises on ledger errors)."""
    workspace = workspace.resolve()
    ledger_file = ledger_path_for(workspace, ledger)
    exempt, exempt_repos = load_ledger(ledger_file)
    repositories = discover_repositories(workspace)
    tools = workspace_scripts(workspace)

    if not repositories and not tools:
        raise tool_audit_error("nothing to audit under %s (no git checkouts, no _tools/_hooks scripts)" % workspace)

    result = report(workspace = str(workspace), ledger = str(ledger_file), repositories = len(repositories))

    for repo in repositories:
        for script in tracked_scripts(repo):
            relative = "%s/%s" % (repo.name, script)
            result.checked += 1
            reason = match_exempt(relative, repo.name, exempt, exempt_repos)

            if reason is None:
                result.findings.append(finding(path = relative, repo = repo.name, kind = Path(script).suffix))
            else:
                result.exempted += 1

    for label, relative in tools:
        result.checked += 1
        reason = match_exempt(relative, label, exempt, exempt_repos)

        if reason is None:
            result.findings.append(finding(path = relative, repo = label, kind = Path(relative).suffix))
        else:
            result.exempted += 1

    return result

def _error_payload(workspace: Path, message: str, ledger: str | None = None) -> dict:
    """The same eight keys as the success payload, for a gate error."""
    return {"workspace": str(workspace.absolute()), "ledger": str(ledger_path_for(workspace, ledger)), "repositories": 0, "checked": 0, "exempted": 0, "findings": [], "errors": [message], "ok": False}

def _render_text(report: report) -> str:
    lines: List[str] = []

    for finding in report.findings:
        lines.append("%s: unregistered %s script — add a reason to %s or rewrite it in Python 3" % (finding.path, finding.kind, report.ledger))

    if lines:
        lines.append("")

    lines.append("tool-audit: %d script(s) across %d repositories, %d exempted, %d unregistered" % (report.checked, report.repositories, report.exempted, len(report.findings)))
    return "\n".join(lines)

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools tool-audit", description = "Report tracked .sh/.ps1 scripts without a registered tool-exemption reason.")
    parser.add_argument("workspace", nargs = "?", default = ".", help = "workspace root holding _ledger/tool-exemptions.md (default: current directory)")
    parser.add_argument("--ledger", default = None, help = "override the ledger path, relative to the current directory (default: <workspace>/_ledger/tool-exemptions.md)")
    parser.add_argument("--json", action = "store_true", help = "emit the machine-readable report on stdout")
    parser.add_argument("--warn-only", action = "store_true", help = "report findings without failing the run")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace)

    try:
        if not workspace.is_dir():
            raise tool_audit_error("not a directory: %s" % workspace)

        report = audit(workspace, args.ledger)
    except (tool_audit_error, OSError) as exc:
        print("tool-audit: %s" % exc, file = sys.stderr)

        if args.json:
            envelope.emit(_error_payload(workspace, str(exc), args.ledger), sort_keys = True)

        return 2

    if args.json:
        envelope.emit(report.to_dict(), sort_keys = True)
    else:
        print(_render_text(report), file = sys.stderr)

    if report.findings and not args.warn_only:
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
