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

*Rules* (each finding carries ``path:line`` and a recomputable criterion; all eight are listed
here — six carry ``error`` severity, and ``self-hosted-pr-without-fork-guard`` and
``devtools-ref-not-pinned`` are ``warning`` so the fleet can adopt the guard and the ref pin
repository by repository instead of in one red wave):

``invalid-caller-key``
    a job that has ``uses:`` **and** a key outside the nine caller keys — the incident.
``callee-missing-timeout``
    a job whose ``runs-on`` contains ``self-hosted``, inside a workflow whose ``on``
    includes ``workflow_call``, with no ``timeout-minutes``.
``self-hosted-missing-timeout``
    the same shape in a workflow that is not a ``workflow_call`` callee.
``caller-not-canonical``
    ``.github/workflows/commit-msg-lint.yml`` — the shared commit-lint caller — must be
    **byte-identical** to the template ``celestia-devtools`` ships (``WORKFLOW_COMMIT_LINT``).
    The fleet is uniform so that one audit answers for all 42 repositories, and a
    hand-edited variant silently changes which check name a repository produces (a caller
    without ``ready_for_review`` or without ``synchronize`` never re-runs the required
    check, so the merge is refused — AGENTS §8.3.6). The comparison keys off the file name,
    so a shared caller parked under **any other name** is reported too: it sits outside the
    comparison and drifts unwatched.
``yaml-parse-error``
    the file cannot be parsed as YAML. This rule reports the **file** (with the first line
    quoted); it never skips it. ``sysl/.github/workflows/validate.yml`` is a real workflow
    flattened onto one line, and every auditor that starts by parsing has been blind to it.
``yaml-duplicate-key``
    a mapping defines the same key twice, at any level. PyYAML keeps the last value
    silently, so ``safe_load``-based auditors stay blind — GitHub's parser instead rejects
    the whole file, zero jobs, nothing red (the ``dev.celestia.world`` job with two ``if:``
    keys produced fourteen pseudo-runs).
``self-hosted-pr-without-fork-guard`` (warning)
    a job that runs **directly** on a self-hosted runner — ``runs-on`` naming
    ``self-hosted``, or an expression over ``vars.`` whose value cannot be resolved offline
    (``fromJSON(vars.CI_RUNNER)``) — inside a workflow whose ``on`` includes
    ``pull_request``, without an ``if:`` carrying the org's fork guard
    (``github.event.pull_request.head.repo.full_name == github.repository``, or the
    equivalent ``github.event.pull_request.head.repo.fork == false``). Such a workflow lets
    a pull request opened from a fork execute untrusted code inside the org network and
    spend shared runner capacity. A job that calls a reusable workflow (``uses:``) is **not**
    reported: its guard lives in the callee, so flagging callers would bury the real
    findings under every canonical caller in the fleet. Warning, not error, because the
    private half of the fleet still has to grow the guard one repository at a time; a
    repository that must stay unguarded records it with
    ``--allow self-hosted-pr-without-fork-guard=<glob>``.
``devtools-ref-not-pinned`` (warning)
    a job whose ``uses:`` invokes a celestia-devtools reusable workflow at ``@master``.
    A floating ref lets any single master commit redefine gate semantics for every
    caller still floating — the python-track verify-versions vacuous-pass incident ran
    on half the fleet exactly this way (arch-review 2026-10-26 P2-3). Any tag or SHA
    is a pin and is not reported; only ``master`` floats. The commit-lint caller under
    its canonical filename is classified by the byte comparison instead, which also
    knows stale pins (a caller pinned to an earlier baseline tag). Warning, not error,
    for as long as the fleet migration runs; a repository that must float records it
    with ``--allow devtools-ref-not-pinned=<glob>``.

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
is one of the rule names in :data:`RULE_NAMES` or ``*`` (a count spelled out in prose is what
drifted when the fifth rule landed) and ``GLOB`` is matched with :func:`fnmatch.fnmatchcase`
against the reported path (``*`` crosses ``/``). Suppressions are counted and echoed on
stderr — an exemption is never silent — and an unknown rule name is a usage error rather
than a silent pass.

*Exit codes:* ``0`` clean | ``1`` at least one error (or a warning under ``--strict``)
| ``2`` usage or I/O error (a missing PATH, an unknown ``--allow`` rule, or a
directory that exists but cannot be listed — that case is **not** reported as
"clean": unreadable means unknown, and unknown is not a pass).

Usage::

    celestia-ci-audit
    celestia-ci-audit .github/workflows/ci.yml --json
    celestia-ci-audit .github/workflows --strict
    celestia-ci-audit --allow self-hosted-missing-timeout=sysl/*
"""

from __future__ import annotations
import argparse
import fnmatch
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
# canonical caller 的**唯一来源**：生成器写出去的就是这个常量。
# （2026-09-15 的 P1 教训：模板在两处各写一份，必然漂移——审计必须比对同一份字节。）
# LEGACY = 2026-09-26 车道化之前的旧 canonical：只作迁移判据（warning），见
# _canonical_caller_findings。
# 钉版（P2-3）：caller 按 ref 分类——clean（当前 pin）/ unpinned（@master 浮动）/
# stale（旧 pin）——分类器与生成器同源（repo.init 的 render_caller/caller_render_state）。
from celestia_devtools.repo.init import (PINNED_DEVTOOLS_REF, TEMPLATE_SOURCES, WORKFLOW_COMMIT_LINT, WORKFLOW_COMMIT_LINT_LEGACY, WORKFLOW_COMMIT_LINT_NO_PERMS, caller_render_state, render_caller)
import yaml as _yaml

try:
    import yaml
    from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
except ImportError as exc:  # pragma: no cover - PyYAML is a declared runtime dependency
    raise ImportError("PyYAML is required by celestia-ci-audit: the `yaml-parse-error` rule must report a " "file that cannot be parsed instead of silently skipping it, and node line numbers " "come from the composer. Install the package itself (PyYAML>=6) rather than adding " "this module to an environment by hand.")from exc

# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

RULE_INVALID_CALLER_KEY = "invalid-caller-key"
RULE_CALLEE_MISSING_TIMEOUT = "callee-missing-timeout"
RULE_SELF_HOSTED_MISSING_TIMEOUT = "self-hosted-missing-timeout"
RULE_YAML_PARSE_ERROR = "yaml-parse-error"
RULE_CALLER_NOT_CANONICAL = "caller-not-canonical"
RULE_YAML_DUPLICATE_KEY = "yaml-duplicate-key"
RULE_SELF_HOSTED_PR_WITHOUT_FORK_GUARD = "self-hosted-pr-without-fork-guard"
RULE_DEVTOOLS_REF_NOT_PINNED = "devtools-ref-not-pinned"

RULE_NAMES: Tuple[str, ...] = (RULE_INVALID_CALLER_KEY, RULE_CALLEE_MISSING_TIMEOUT, RULE_SELF_HOSTED_MISSING_TIMEOUT, RULE_YAML_PARSE_ERROR, RULE_CALLER_NOT_CANONICAL, RULE_YAML_DUPLICATE_KEY, RULE_SELF_HOSTED_PR_WITHOUT_FORK_GUARD, RULE_DEVTOOLS_REF_NOT_PINNED)

#: The file every repository ships for the shared commit-lint caller.
CANONICAL_CALLER_NAME = "commit-msg-lint.yml"

#: Substring that identifies a job calling the shared commit-lint reusable workflow.
SHARED_COMMIT_LINT_REF = "celestia-devtools/.github/workflows/commit-msg-lint.yml"

#: Prefix identifying any job that calls a celestia-devtools reusable workflow
#: (rule 8 reads the ref off the ``uses:`` scalar itself, so any callee counts).
SHARED_DEVTOOLS_PREFIX = "celestia-island/celestia-devtools/.github/workflows/"

#: The ref that floats with master — the one ref that is not a pin (rule 8).
FLOATING_REF = "master"

# Two severities: ``error`` for what GitHub silently refuses to run (or what silently stops a
# required check), ``warning`` for a shape the fleet has to adopt incrementally —
# ``self-hosted-pr-without-fork-guard`` is the latter, and ``--strict`` turns warnings into a
# non-zero exit for a repository that has finished the migration and wants to stay finished.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: The **complete** set of keys GitHub accepts on a ``jobs.<id>.uses`` caller job.
#: Anything else makes the whole workflow invalid and creates zero jobs.
CALLER_LEGAL_KEYS: Tuple[str,...] = ("name", "uses", "with", "secrets", "strategy", "needs", "if", "concurrency", "permissions")

#: Label substring that marks a job as running on the org's self-hosted farm.
SELF_HOSTED_LABEL = "self-hosted"

#: The ``on`` member that turns a workflow into a reusable-workflow callee.
WORKFLOW_CALL_KEY = "workflow_call"

#: The event that carries a head repository, i.e. the one a fork pull request arrives on.
PULL_REQUEST_EVENT = "pull_request"

#: The org's canonical fork guard, verbatim from every workflow that has one. A fork PR
#: must neither execute code on the org's self-hosted runners nor spend their capacity.
FORK_GUARD = "github.event_name != 'pull_request' || " "github.event.pull_request.head.repo.full_name == github.repository"

#: Substrings that identify a guarded job. Whitespace-stripped, because YAML authors fold and
#: reflow ``if:`` freely (``>``/``>-`` blocks, ``||`` on its own line), and the guard must be
#: recognised in every spelling of the same expression.
#:
#: The second form (``head.repo.fork == false``) is the equivalent test — a fork has a head
#: repository that is not ``github.repository`` — and repositories already using it are clean.
FORK_GUARD_MARKERS: Tuple[str, ...] = ("github.event.pull_request.head.repo.full_name==github.repository", "github.event.pull_request.head.repo.fork==false")

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
class finding_record:
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
        return {"path": self.path, "line": self.line, "rule": self.rule, "severity": self.severity, "message": self.message}

def has_failure(findings: Sequence[finding_record], strict: bool = False) -> bool:
    """Whether the run must exit non-zero: any error, or (with *strict*) any warning.

    ``self-hosted-pr-without-fork-guard`` and ``devtools-ref-not-pinned`` are
    warnings so the fleet can grow the guard and the ref pin one repository at
    a time without a red wave, which makes ``--strict`` the switch a repository
    flips once its own tree is guarded and pinned to keep it that way.
    """

    for finding in findings:
        if finding.severity == SEVERITY_ERROR:
            return True

        if strict and (finding.severity == SEVERITY_WARNING):
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

def _triggers(on_node: Optional[Node], event: str) -> bool:
    """Whether the top-level ``on`` includes *event*.

    All three shapes GitHub accepts are covered::

        on: pull_request
        on: [push, pull_request]
        on:
          pull_request:
    """

    if isinstance(on_node, ScalarNode):
        return on_node.value == event

    if isinstance(on_node, SequenceNode):
        return any(_scalar_text(child) == event for child in on_node.value)

    if isinstance(on_node, MappingNode):
        return any(_scalar_text(key_node) == event for key_node, _ in on_node.value)

    return False

def _declares_workflow_call(on_node: Optional[Node]) -> bool:
    """Whether the top-level ``on`` includes ``workflow_call`` (a reusable-workflow callee)."""
    return _triggers(on_node, WORKFLOW_CALL_KEY)

def _is_self_hosted(runs_on: Optional[Node]) -> bool:
    """Whether a ``runs-on`` value puts the job on the org farm (rule 7's reading).

    Two things count, and the second is the interesting one:

    * a label naming ``self-hosted``;
    * an expression over ``vars.`` — ``runs-on: ${{ fromJSON(vars.CI_RUNNER) }}``. The
      repository variable is not in the tree, so the audit cannot know whether it resolves
      to the farm or to ``ubuntu-latest``. It reads as self-hosted, because the two mistakes
      are not symmetric: a false positive costs one warning a repository silences with
      ``--allow``, while a false negative means a fork pull request runs inside the org
      network.

    Rules 2 and 3 deliberately keep the literal reading (``self-hosted`` in a label): turning
    their existing ``error`` into "the expression might name the farm" would redden builds
    across the fleet, which is a different decision from adding this warning.
    """

    for label in _scalar_labels(runs_on):
        lowered = label.lower()

        if SELF_HOSTED_LABEL in lowered:
            return True

        if ("${{" in label) and ("vars." in lowered):
            return True

    return False

def _has_fork_guard(entries: Dict[str, Tuple[Node, Node]]) -> bool:
    """Whether the job's own ``if:`` carries the fork guard, in any accepted spelling."""
    entry = entries.get("if")

    if entry is None:
        return False

    text = _scalar_text(entry[1]) or ""
    compact = "".join(text.split())  # `if: >-` folds, `||` may sit on its own line
    return any(marker in compact for marker in FORK_GUARD_MARKERS)

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

        if (name is not None) and (name not in entries):
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

def _parse_error_finding(exc: yaml.YAMLError, text: str, path: str) -> finding_record:
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
    message = f"file is not valid YAML ({problem}{where}); first line: {summary!r}; " "a file that cannot be parsed is reported here rather than skipped - " "GitHub refuses such a workflow outright, so it creates no job that could fail"
    return finding_record(path = path, line = line, rule = RULE_YAML_PARSE_ERROR, severity = SEVERITY_ERROR, message = message, excerpt = _line_excerpt(text.split("\n"), line))

def _caller_findings(job_id: str, entries: Dict[str, Tuple[Node, Node]], path: str, lines: Sequence[str]) -> List[finding_record]:
    """Rule 1: a caller job may only carry the nine legal keys; name every offender."""
    findings: List[finding_record] = []

    for key, (key_node, _value_node) in entries.items():
        if key in CALLER_LEGAL_KEYS:
            continue

        line = _line_of(key_node)
        message = f"job {job_id!r} calls a reusable workflow (uses:) and also sets {key!r}, " f"which is not one of the nine caller keys ({_CALLER_KEY_LIST}); GitHub then " "rejects the whole workflow - no job is created and the run degenerates to " "0 jobs without failing"
        findings.append(finding_record(path = path, line = line, rule = RULE_INVALID_CALLER_KEY, severity = SEVERITY_ERROR, message = message, excerpt = _line_excerpt(lines, line)))

    return findings

def _missing_timeout_finding(job_id: str, runs_on_key: Node, runs_on_value: Node, is_callee: bool, path: str, lines: Sequence[str]) -> finding_record:
    """Rules 2 and 3: a self-hosted job without ``timeout-minutes``."""
    labels = ", ".join(_scalar_labels(runs_on_value))

    if is_callee:
        rule = RULE_CALLEE_MISSING_TIMEOUT
        message = f"job {job_id!r} is self-hosted (runs-on: {labels}) in a workflow_call callee " "and has no timeout-minutes; a caller job cannot set one (it is not in the " "caller key set), so the upper bound can only live here"
    else:
        rule = RULE_SELF_HOSTED_MISSING_TIMEOUT
        message = f"job {job_id!r} is self-hosted (runs-on: {labels}) and has no timeout-minutes; " "GitHub's 360-minute default cannot stop a process stuck in the kernel D state, " "so a wedged job holds one of the four runners until a human intervenes"

    line = _line_of(runs_on_key)
    return finding_record(path = path, line = line, rule = rule, severity = SEVERITY_ERROR, message = message, excerpt = _line_excerpt(lines, line))

def _fork_guard_finding(job_id: str, runs_on_key: Node, runs_on_value: Node, path: str, lines: Sequence[str]) -> finding_record:
    """Rule 7: a self-hosted job reachable by ``pull_request`` without the fork guard."""
    labels = ", ".join(_scalar_labels(runs_on_value)) or "an expression"
    message = f"job {job_id!r} has no fork guard: it runs directly on a self-hosted runner " f"(runs-on: {labels}) in a workflow triggered by pull_request, so a pull request " f"whose head branch lives in a fork would execute untrusted code inside the org " f"network and spend the shared runner capacity same-repo PRs need. Add the org's " f"canonical guard to the job — same-repo PRs, pushes and workflow_dispatch keep " f"running: if: {FORK_GUARD} (github.event.pull_request.head.repo.fork == false is " f"accepted as the equivalent form). A repository that must stay unguarded records " f"it with --allow self-hosted-pr-without-fork-guard=<glob>"
    line = _line_of(runs_on_key)
    return finding_record(path = path, line = line, rule = RULE_SELF_HOSTED_PR_WITHOUT_FORK_GUARD, severity = SEVERITY_WARNING, message = message, excerpt = _line_excerpt(lines, line))

def _calls_shared_commit_lint(jobs: MappingNode) -> bool:
    """True when any job is a caller of the shared commit-lint reusable workflow."""

    for _key_node, body in jobs.value:
        if not isinstance(body, MappingNode):
            continue

        uses = _mapping_value(body, "uses")

        if isinstance(uses, ScalarNode) and (SHARED_COMMIT_LINT_REF in (uses.value or "")):
            return True

    return False

def _same_caller_semantics(text: str, canonical: str) -> tuple[bool, str]:
    """Do two caller files *declare* the same thing, comments aside?

    Returns ``(equal, reason_when_different)``.  YAML drops comments, so parsing
    both and comparing the documents compares exactly what GitHub acts on:
    triggers (and their `types`), `permissions`, and every job's `uses`/`with`.
    A parse failure is reported as "not the same" with the parse error, never as
    a silent pass — an unparseable caller is a different (worse) problem.
    """

    try:
        mine = _yaml.safe_load(text)
        theirs = _yaml.safe_load(canonical)
    except Exception as exc:  # noqa: BLE001 - any parse error means "cannot compare"
        return False, f"cannot be parsed for comparison ({exc.__class__.__name__})"

    def shape(doc: object) -> dict:
        """The parts GitHub acts on, normalised for comparison."""

        if not isinstance(doc, dict):
            return {}

        triggers = doc.get(True, doc.get("on"))
        jobs = doc.get("jobs") or {}
        declared = {}

        for job_id, body in (jobs or {}).items():
            if not isinstance(body, dict):
                return {}

            declared[job_id] = {"uses": body.get("uses"), "with": body.get("with") or {}}

        return {"triggers": triggers, "permissions": doc.get("permissions"), "jobs": declared}

    # Compare the parsed documents whole: YAML keeps everything GitHub acts on
    # (triggers, permissions, jobs, `with`, `concurrency`, ...) and drops only
    # comments and formatting.  Comparing whole documents is the honest test —
    # an extra `concurrency:` block really does change how runs behave.
    if mine == theirs:
        return True, ""

    mine_shape, theirs_shape = shape(mine), shape(theirs)

    for key in ("triggers", "permissions", "jobs"):
        if mine_shape.get(key) != theirs_shape.get(key):
            return False, f"differs in `{key}`"

    return False, "differs outside triggers/permissions/jobs"

def _canonical_caller_findings(text: str, path: str) -> List[finding_record]:
    """Byte-compare the caller against the template the generator ships.

    Outcomes (2026-09-26 lane migration + 2026-10-26 P2-3 ref pinning):

    * byte-identical to the canonical template rendered at the current pin →
      clean;
    * byte-identical to the **legacy** pre-lane template → WARNING, migration;
    * the lane-carrying form without the `permissions` grant → WARNING;
    * byte-identical to the template rendered at ``@master`` → WARNING
      (``devtools-ref-not-pinned``): generator-owned bytes, floating ref — the
      fleet's migration backlog, never a red wave;
    * byte-identical to the template rendered at an older pin → WARNING
      (``devtools-ref-not-pinned``): advance by regeneration;
    * semantically equal to either form, comments aside → WARNING (do not
      delete comments to satisfy an audit);
    * anything else → ERROR: hand-edited or from an older generator, and a
      variant that drops ``ready_for_review``/``synchronize`` stops re-running
      the required check (the merge then cannot proceed).
    """
    _source = TEMPLATE_SOURCES["commit-msg-lint.yml"]

    if text == WORKFLOW_COMMIT_LINT:
        return []

    if text == WORKFLOW_COMMIT_LINT_NO_PERMS:
        return [finding_record(path = path, line = 1, rule = RULE_CALLER_NOT_CANONICAL, severity = SEVERITY_WARNING, message = f"this commit-lint caller carries the lane passthrough but not the " f"`permissions` grant the reusable lint declares " f"({len(WORKFLOW_COMMIT_LINT_NO_PERMS.encode())} bytes). The org default " f"token permissions were tightened on 2026-10-02; without an explicit " f"`pull-requests: read` the callee fails as `startup_failure` with zero " f"jobs, so the required check never reports and a protected repository " f"cannot be merged at all. Regenerate it with " f"`celestia-devtools init --with-workflows --force`", excerpt = "")]

    if text == WORKFLOW_COMMIT_LINT_LEGACY:
        return [finding_record(path = path, line = 1, rule = RULE_CALLER_NOT_CANONICAL, severity = SEVERITY_WARNING, message = f"this commit-lint caller is the legacy pre-lane template " f"({len(WORKFLOW_COMMIT_LINT_LEGACY.encode())} bytes); regenerate it with " f"`celestia-devtools init --with-workflows --force` to gain the " f"`vars.LINT_RUNNER`/`vars.LINT_LANE` passthrough, so the required check " f"can be served from the self-hosted farm by setting repository " f"variables instead of editing this tree", excerpt = "")]

    state, ref = caller_render_state(text, _source)

    if state in ("master", "stale"):
        floating = "pins nothing: it invokes the devtools callee at @master, so any master commit instantly redefines this gate's semantics" if state == "master" else f"is pinned to @{ref} while the current pin is @{PINNED_DEVTOOLS_REF}"
        return [finding_record(path = path, line = 1, rule = RULE_DEVTOOLS_REF_NOT_PINNED, severity = SEVERITY_WARNING, message = f"this commit-lint caller {floating} (P2-3). Regenerate it with " f"`celestia-devtools init --with-workflows --force` to pin @{PINNED_DEVTOOLS_REF}; a repository that must float records it with " f"--allow devtools-ref-not-pinned={Path(path).name}", excerpt = "")]

    semantically_same, why = _same_caller_semantics(text, WORKFLOW_COMMIT_LINT)

    if not semantically_same and (state == "drifted"):
        # A hand-edited @master variant may still declare what the pre-pin
        # canonical declared, comments aside: compare against the master
        # render before failing it, so a straggler with extra comments is a
        # warning (regenerate) and not an error that invites comment deletion.
        semantically_same, why = _same_caller_semantics(text, render_caller(_source, FLOATING_REF))

        if semantically_same:
            why = "matches the floating @master form, comments aside"

    if semantically_same:
        # 2026-10-24: a caller that differs from the canonical bytes only in its
        # comments is NOT a broken check. shittim-chest and noa carried exactly
        # this shape (1618 / 1667 bytes, identical triggers, permissions, `uses`
        # and `with`), and the check reported an ERROR — which teaches a
        # repository to delete its explanatory comments to turn the gate green,
        # i.e. to make the tree worse to satisfy the tool. Comments are where a
        # caller records why it grants what it grants, so they stay a warning.
        return [finding_record(path = path, line = 1, rule = RULE_CALLER_NOT_CANONICAL, severity = SEVERITY_WARNING, message = f"this commit-lint caller declares exactly what a canonical template " f"does (same triggers, permissions, `uses` and `with`) but differs in its " f"comments ({len(text.encode())} bytes; {why}). That is not a broken check; " f"regenerate with `celestia-devtools init --with-workflows --force` if you " f"want the bytes to match, but do not delete the comments to satisfy the " f"audit", excerpt = "")]

    return [finding_record(path = path, line = 1, rule = RULE_CALLER_NOT_CANONICAL, severity = SEVERITY_ERROR, message = f"the shared commit-lint caller must be byte-identical to the canonical " f"template ({len(WORKFLOW_COMMIT_LINT.encode())} bytes); this file is " f"{len(text.encode())} bytes and {why}. Regenerate it with " f"`celestia-devtools init --with-workflows --force` instead of editing it by " f"hand — a variant that drops `ready_for_review` or `synchronize` stops " f"re-running the required check, and the PR then cannot be merged", excerpt = "")]

def _devtools_unpinned_findings(jobs: MappingNode, path: str, lines: Sequence[str]) -> List[finding_record]:
    """Rule 8: a job calling a devtools reusable workflow at ``@master``.

    Any tag or SHA is a pin — only ``master`` floats, so only ``master`` is
    reported. The ``uses:`` value is read from the parsed document, so a
    commented-out example cannot count as consumption. The commit-lint caller
    under its canonical name is excluded here: the byte comparison in
    :func:`_canonical_caller_findings` already classifies its ref (and a file
    that hits both rules would report the same fact twice).
    """

    findings: List[finding_record] = []

    for key_node, body in jobs.value:
        if not isinstance(body, MappingNode):
            continue

        uses_key = _mapping_value(body, "uses")

        if not isinstance(uses_key, ScalarNode):
            continue

        value = uses_key.value or ""
        is_devtools_callee = value.startswith(SHARED_DEVTOOLS_PREFIX)
        is_floating = value.endswith("@" + FLOATING_REF)

        if (not is_devtools_callee) or (not is_floating):
            continue

        job_id = _scalar_text(key_node) or "<unnamed>"
        callee = value[len(SHARED_DEVTOOLS_PREFIX):].split("@", 1)[0]
        message = f"job {job_id!r} invokes the devtools reusable workflow {callee!r} at " f"@{FLOATING_REF}: any master commit instantly redefines this gate's " f"semantics for every caller still floating (P2-3; the python-track " f"verify-versions vacuous-pass incident ran exactly this way). Pin the " f"caller — regenerate it with `celestia-devtools init --with-workflows " f"--force` (add `--gate <name>` for opt-in callers) or reference a devtools " f"baseline tag directly. A repository that must float records it with " f"--allow devtools-ref-not-pinned=<glob>"
        findings.append(finding_record(path = path, line = _line_of(uses_key), rule = RULE_DEVTOOLS_REF_NOT_PINNED, severity = SEVERITY_WARNING, message = message, excerpt = _line_excerpt(lines, _line_of(uses_key))))

    return findings

def _mislocated_caller_findings(path: str) -> List[finding_record]:
    """Flag a shared caller that lives in a file other than :data:`CANONICAL_CALLER_NAME`.

    The byte comparison keys off the file name, so a copy under ``checks.yml`` is never
    compared: it can drop ``synchronize``/``ready_for_review`` and stay green forever. That
    is exactly how the fleet ended up with two shapes — the check name depends only on the
    job id, so the copy works well enough to survive, and the drift is invisible.
    """
    return [finding_record(path = path, line = 1, rule = RULE_CALLER_NOT_CANONICAL, severity = SEVERITY_ERROR, message = f"the shared commit-lint caller must live in `.github/workflows/" f"{CANONICAL_CALLER_NAME}`; this copy is under another name, which puts it " f"outside the byte comparison against the canonical template — it will drift " f"silently and stop re-running the required check. Move it to " f"`.github/workflows/{CANONICAL_CALLER_NAME}` (or regenerate that file with " f"`celestia-devtools init --with-workflows --force`) and delete this one", excerpt = "")]

def _duplicate_key_findings(root: Node, path: str, lines: Sequence[str]) -> List[finding_record]:
    """Flag a mapping that defines the same key twice, anywhere in the document.

    PyYAML's ``safe_load`` silently keeps the *last* value, so every "parse it and look"
    auditor stays blind — but GitHub's parser rejects the **whole file**, which means zero
    jobs, no checks, and nothing red (the ``dev.celestia.world`` incident: a job with two
    ``if:`` keys produced fourteen zero-job pseudo-runs). The composed tree keeps every
    key, duplicates included, so the check is a plain walk.
    """
    findings: List[finding_record] = []

    def walk(node: Node, seen: Set[int]) -> None:
        if id(node) in seen:  # recursive aliases must not recurse forever
            return

        seen.add(id(node))

        if isinstance(node, MappingNode):
            first_line: Dict[str, int] = {}

            for key_node, value_node in node.value:
                key = _scalar_text(key_node)
                line = _line_of(key_node)

                if key is not None:
                    if key in first_line:
                        findings.append(finding_record(path = path, line = line, rule = RULE_YAML_DUPLICATE_KEY, severity = SEVERITY_ERROR, message = f"the same mapping key {key!r} is defined twice " f"(this occurrence at line {line}, the first at line " f"{first_line[key]}); PyYAML silently keeps the last " f"value but GitHub's parser rejects the whole file and " f"runs zero jobs — deduplicate, keeping one value", excerpt = _line_excerpt(lines, line)))
                    else:
                        first_line[key] = line

                walk(key_node, seen)
                walk(value_node, seen)
        elif isinstance(node, SequenceNode):
            for item in node.value:
                walk(item, seen)

    if root is not None:
        walk(root, set())

    return findings

def audit_text(text: str, path: str) -> List[finding_record]:
    """Audit one workflow's text; *path* is the already-resolved display path."""

    try:
        root = yaml.compose(text, Loader = yaml.SafeLoader)
    except yaml.YAMLError as exc:
        # 规则 4 的意义就在这一条 except：解析失败必须变成 finding，绝不能 continue 跳过。
        return [_parse_error_finding(exc, text, path)]

    lines = text.split("\n")
    # 规则 6 在整棵树上走：重复键可以出现在任何层级（作业里、step 的 env 里），而且
    # 就算没有 jobs 可查（其他规则都会早退），这个文件对 GitHub 仍然是非法的。
    findings: List[finding_record] = list(_duplicate_key_findings(root, path, lines))

    if not isinstance(root, MappingNode):
        # 空文档（compose 返回 None）或非映射根：没有 jobs 可查，也没有解析错误。
        return findings

    jobs = _mapping_value(root, "jobs")

    if not isinstance(jobs, MappingNode):
        return findings

    on_node = _mapping_value(root, "on")
    is_callee = _declares_workflow_call(on_node)
    # 规则 7 只看 pull_request 触发的 workflow：push / workflow_dispatch 事件里
    # `github.event.pull_request` 是 null，守卫无从谈起。
    pr_triggered = _triggers(on_node, PULL_REQUEST_EVENT)

    if _calls_shared_commit_lint(jobs):
        if Path(path).name == CANONICAL_CALLER_NAME:
            findings.extend(_canonical_caller_findings(text, path))
        else:
            findings.extend(_mislocated_caller_findings(path))

    # Rule 8 reads the ref off every `uses:` job — but not the commit-lint
    # caller under its canonical name, which the byte comparison above already
    # classified (reporting the same fact under two rules is noise, and the
    # canonical comparator knows stale pins too, not just @master).
    is_canonical_lint_file = (Path(path).name == CANONICAL_CALLER_NAME) and _calls_shared_commit_lint(jobs)

    if not is_canonical_lint_file:
        findings.extend(_devtools_unpinned_findings(jobs, path, lines))

    for key_node, body in jobs.value:
        job_id = _scalar_text(key_node) or "<unnamed>"

        if not isinstance(body, MappingNode):
            continue

        entries = _job_entries(body)

        if "uses" in entries:
            # caller 作业：只查合法键集。它的 runner 由 callee 决定，因此不再套用
            # 自托管规则（否则 `uses` + `runs-on` 会被重复计一次）。fork guard 同理：
            # 守卫在 callee 里，在 caller 上要求一份会把 34 个私有仓的 canonical caller
            # 全部误报成 warning，真问题被噪音埋掉。
            findings.extend(_caller_findings(job_id, entries, path, lines))
            continue

        runs_on = entries.get("runs-on")

        if runs_on is None:
            continue

        runs_on_key, runs_on_value = runs_on

        if pr_triggered and _is_self_hosted(runs_on_value) and not _has_fork_guard(entries):
            findings.append(_fork_guard_finding(job_id, runs_on_key, runs_on_value, path, lines))

        if "timeout-minutes" in entries:
            continue

        labels = _scalar_labels(runs_on_value)

        if not any(SELF_HOSTED_LABEL in label.lower() for label in labels):
            continue

        findings.append(_missing_timeout_finding(job_id, runs_on_key, runs_on_value, is_callee, path, lines))

    return findings

def audit_file(path: Path, display: str) -> List[finding_record]:
    """Audit one file; a read failure is either a finding (bad encoding) or an ``OSError``."""

    try:
        text = path.read_text(encoding = "utf-8")
    except UnicodeDecodeError as exc:
        message = f"file is not valid UTF-8 ({exc}); it is reported here rather than skipped, " "because a workflow GitHub cannot read creates no job that could fail"
        return [finding_record(path = display, line = 1, rule = RULE_YAML_PARSE_ERROR, severity = SEVERITY_ERROR, message = message, excerpt = "")]

    return audit_text(text, display)

# ---------------------------------------------------------------------------
# 文件遍历
# ---------------------------------------------------------------------------

def _yml_files(directory: Path) -> List[Path]:
    """Sorted ``*.yml`` / ``*.yaml`` files directly inside *directory* (never recursive).

    ``os.scandir`` instead of ``Path.glob`` is deliberate: ``glob`` **swallows EACCES** and
    returns an empty list, which would turn "this directory cannot be listed" into "this
    directory holds no workflows" — precisely the silent skip this tool exists to prevent
    (verified counterexample 2026-09-15: ``.github/workflows`` readable-but-chmod-000
    reported ``clean (0 workflow file(s) scanned)`` / exit 0). Here the ``OSError``
    propagates and the caller turns it into exit 2.
    """

    if not directory.is_dir():          # EACCES on the path itself propagates too
        return []

    found: List[Path] = []

    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith((".yml", ".yaml")):
                continue

            try:
                if entry.is_file():
                    found.append(Path(entry.path))
            except OSError:             # 单个条目竞态消失：跳过它即可，不掩盖整个目录
                continue

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

def collect_workflow_files(raw_paths: Sequence[str], cwd: Optional[Path] = None) -> Tuple[List[Tuple[Path, str]], Optional[int]]:
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
            print(f"error: {raw}: no such file or directory", file = sys.stderr)
            return [], 2

        try:
            if candidate.is_dir():
                files = _files_for_directory(candidate)
            else:
                files = [candidate]
        except OSError as exc:
            # 目录不可读时必须停：静默跳过等于把该目录下的 workflow 全部漏检，
            # 而本工具存在的理由就是"未创建 job 的失败没有 check 会变红"。
            print(f"error: {raw}: {exc}", file = sys.stderr)
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

    collected.sort(key = lambda item: item[1])
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

    if (rule != "*") and (rule not in RULE_NAMES):
        raise argparse.ArgumentTypeError(f"unknown rule {rule!r} (expected one of {', '.join(RULE_NAMES)}, or *)")

    return rule, pattern

def _is_allowed(finding: finding_record, rules: Sequence[Tuple[str, str]]) -> bool:
    """Whether any ``--allow`` entry covers this finding (``*`` matches every rule)."""
    return any(((rule == "*") or (rule == finding.rule)) and fnmatch.fnmatchcase(finding.path, pattern)for rule, pattern in rules)

def apply_allow(findings: Sequence[finding_record], rules: Sequence[Tuple[str, str]]) -> Tuple[List[finding_record], int]:
    """Split findings into ``(reported, suppressed_count)``."""
    reported = [finding for finding in findings if not _is_allowed(finding, rules)]
    return reported, len(findings) - len(reported)

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def build_report(findings: Sequence[finding_record], files: int, suppressed: int, strict: bool) -> Dict[str, object]:
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

    return {"findings": [finding.to_json()for finding in findings], "summary": {"files": files, "findings": len(findings), "errors": errors, "warnings": warnings, "suppressed": suppressed, "failed": has_failure(findings, strict), "rules": rules}}

def _summary_line(files: int, findings: Sequence[finding_record], suppressed: int) -> str:
    """Human summary line (stderr under ``--json``)."""

    if not findings:
        text = f"clean ({files} workflow file(s) scanned"

        if suppressed:
            text += f", {suppressed} finding(s) suppressed by --allow"

        return f"celestia-ci-audit: {text})"

    errors = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
    warnings = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    return f"celestia-ci-audit: {len(findings)} finding(s) in {files} workflow file(s) " f"(errors: {errors}, warnings: {warnings}, suppressed: {suppressed})"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _whole_word_help_formatter(argparse.HelpFormatter):
    """Wrap option help without breaking words on hyphens.

    argparse wraps with ``break_on_hyphens=True``, so the ``--allow`` rule list renders as
    ``callee-missing-`` / ``timeout`` at the usual 80-column width — a reader copying a name
    out of ``--help`` gets half of one, and the rendered text no longer contains the rule
    name its own error message demands. The names come from :data:`RULE_NAMES`; this keeps
    them whole at any width (``break_long_words=False`` covers a terminal narrower than the
    longest name).

    The base class stays the wrapping ``HelpFormatter`` on purpose: the
    ``Raw*`` variants return the description unwrapped, which would trade a split rule name
    for a 292-character single-line description.
    """

    def _split_lines(self, text: str, width: int) -> List[str]:
        return textwrap.wrap(text, width, break_on_hyphens = False, break_long_words = False)

def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(prog = "celestia-ci-audit", formatter_class = _whole_word_help_formatter, description = "Fail GitHub Actions workflows that GitHub would silently refuse to run: " "illegal keys on reusable-workflow caller jobs (jobs.<id>.uses), self-hosted " "jobs without timeout-minutes, and files that are not valid YAML. The caller " "side cannot express a timeout upper bound; it belongs on the callee. " "Self-hosted jobs reachable by pull_request also have to keep fork PRs out " "(warning).")
    parser.add_argument("paths", nargs = "*", help = "workflow files or directories (default: .github/workflows)")
    parser.add_argument("--json", action = "store_true", dest = "as_json", help = "emit the fixed machine-readable report on stdout")
    parser.add_argument("--strict", action = "store_true", help = "treat warning-severity findings as failures too " "(self-hosted-pr-without-fork-guard and devtools-ref-not-pinned are the warning-severity rules)")
    parser.add_argument("--allow", action = "append", default = [], type = parse_allow, metavar = "RULE=GLOB", help = "suppress findings of RULE whose reported path matches GLOB " f"(RULE is one of {', '.join(RULE_NAMES)}, or *; repeatable)")
    return parser

def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns 0 clean / 1 findings / 2 usage or I/O error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    files, failure = collect_workflow_files(args.paths)

    if failure is not None:
        return failure

    findings: List[finding_record] = []

    for path, display in files:
        try:
            findings.extend(audit_file(path, display))
        except OSError as exc:
            print(f"error: {exc}", file = sys.stderr)
            return 2

    findings.sort(key = lambda item: (item.path, item.line, item.rule))
    reported, suppressed = apply_allow(findings, args.allow)

    if args.as_json:
        report = build_report(reported, len(files), suppressed, args.strict)
        print(json.dumps(report, indent = 2, ensure_ascii = False))
    else:
        for finding in reported:
            print(finding.render())

        if not files:
            print(f"note: no workflow file found under {', '.join(args.paths or DEFAULT_PATHS)}", file = sys.stderr)

    if suppressed:
        # 豁免必须留痕：允许列表不能变成静默通过。
        rules_text = ", ".join(f"{rule}={pattern}" for rule, pattern in args.allow)
        print(f"note: suppressed {suppressed} finding(s) via --allow ({rules_text})", file = sys.stderr)

    print(_summary_line(len(files), reported, suppressed), file = sys.stderr if args.as_json else sys.stdout)
    return 1 if has_failure(reported, args.strict) else 0

if __name__ == "__main__":
    raise SystemExit(main())
