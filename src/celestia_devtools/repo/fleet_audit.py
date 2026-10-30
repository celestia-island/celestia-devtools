#!/usr/bin/env python3
"""Report canonical-caller adoption across a workspace of repositories.

*Why this exists:* ``init --check`` answers "is THIS checkout canonical?".
Nothing answered "which repositories in the workspace are canonical?" — the
2026-10-25 structural audit (SR-01) had to rebuild that picture by hand and
found commit-msg 46/51, ci-cache 14/51, pr-title 27/51, with 44/51 repositories
having at least one canonical problem. This command gives the fleet one surface: it walks
the workspace's immediate git checkouts, applies the SAME canonical comparison
as ``init --check`` (the templates stay a single source in
``celestia_devtools.repo.init``), and reports adoption of the nine reusable
gates.

*Per repository it reports:*

- ``problems`` — missing/drifted mandatory callers, from
  :func:`celestia_devtools.repo.init.workflow_drift_problems`; a repository
  without ``.github/workflows`` is reported rather than skipped;
- ``gates`` — whether each reusable workflow is referenced from a ``uses:``
  line of any workflow file (``commit-msg-lint``, ``ci-cache``,
  ``pr-title-check``, ``p0-gate``, ``family-versions``, ``verify-versions``,
  ``rules-lint``, ``release-notes``, ``cnb-overflow``); a commented-out
  reference does not count;
- ``devtools_refs`` / ``ref_state`` — the unique refs those ``uses:`` lines
  invoke and a per-repository verdict (``pinned`` at the current baseline,
  ``master`` floating, ``stale`` older pin, ``none``); ``@master`` callers are
  reported as warnings (P2-3) and counted in the summary census.

All reads are **origin-first**: the report describes the remote default branch,
not uncommitted local state, and falls back to the worktree (``"ref":
"worktree"``) only when the checkout has no usable origin ref.

This is a reporting command, not a policy engine: which opt-in gates a
repository *should* adopt depends on its manifests and tags, so ``ok`` tracks
only the mandatory trio (plus scan errors). Adoption counts are what a migration
wave schedules against.

Exit codes: ``0`` every checkout is canonical, ``1`` at least one mandatory
caller problem, ``2`` usage/scan error. ``--warn-only`` reports without failing.

*Pins:* every caller names a devtools ref, and a **branch** ref (``master`` /
``main``) means one commit there redefines the gate semantics of every consumer
at once -- the 2026-10 regression that made the CalVer baseline pin necessary
(PR #208). A tag or a SHA is a pin: GitHub accepts both and ``init.py``'s own
docstring calls a hand-written SHA pin legal, so a repository on an older *or
newer* pin is not a failure here (``unpinned_caller_warnings`` still reports it,
with advice that never tells a repository that is ahead to regenerate
downwards). Floating stays a **warning** while the fleet migrates, so the audit
is runnable today; ``--strict-pins`` promotes it to a failure, which is what a
migration wave (or a future required check) runs. A repository with no devtools
caller at all counts as ``none``: it has no gate to pin, and the
mandatory-caller problems already report it.

*Unreadable is not absent.* A caller line the extractor cannot read, or a file
whose YAML says it invokes a gate while yielding no ref, is a **problem** -- and
``pins_ok`` follows it, because an unfinished read must never report "pins
fine". The reverse skew is accepted knowingly: a ``run:`` heredoc whose line
begins with ``uses:`` is picked up by the line extractor but not by the
YAML-aware gate detection, so under ``--strict-pins`` it can fail *loudly*
rather than silently -- over-reported, never under-reported.

Usage::

    celestia-devtools fleet-audit
    celestia-devtools fleet-audit /mnt/codespace --json
    celestia-devtools fleet-audit --warn-only
"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import yaml
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence
from celestia_devtools.repo.init import OPT_IN_GATES, PINNED_DEVTOOLS_REF, REQUIRED_WORKFLOWS, alias_warnings, devtools_ref_inputs_in, devtools_refs_in, opt_in_drift_warnings, unparsed_devtools_uses, unpinned_caller_warnings, workflow_drift_problems

#: ``gate name → caller filename`` for the nine reusable workflows. The first
#: three are the mandatory trio; the rest are opt-in (scope decides adoption).
GATE_FILES: Dict[str, str] = {"commit-msg-lint": REQUIRED_WORKFLOWS[0][0], "ci-cache": REQUIRED_WORKFLOWS[1][0], "pr-title-check": REQUIRED_WORKFLOWS[2][0], "p0-gate": OPT_IN_GATES["p0"][0], "family-versions": OPT_IN_GATES["family"][0], "verify-versions": OPT_IN_GATES["verify"][0], "rules-lint": OPT_IN_GATES["rules"][0], "release-notes": OPT_IN_GATES["release-notes"][0], "cnb-overflow": OPT_IN_GATES["cnb"][0]}

#: Gate name → CALLEE filename as it appears in a caller's ``uses:``. Only
#: ``cnb`` differs (caller ``cnb-overflow-lane.yml`` invokes ``cnb-overflow.yml``).
GATE_CALLEE_FILES: Dict[str, str] = dict(GATE_FILES)
GATE_CALLEE_FILES["cnb-overflow"] = "cnb-overflow.yml"

class fleet_audit_error(Exception):
    """A workspace scan problem that must fail the gate closed."""

@dataclass
class repo_report:
    name: str
    ref: str = "worktree"
    problems: List[str] = field(default_factory = list)
    warnings: List[str] = field(default_factory = list)
    gates: Dict[str, bool] = field(default_factory = dict)
    #: Unique devtools refs this repository's callers invoke (P2-3 census).
    devtools_refs: List[str] = field(default_factory = list)
    #: True when a caller exists but its ref could not be read. `pins_ok` must
    #: follow this too, or an unfinished read would report "pins fine".
    pin_unreadable: bool = False
    #: Callers whose `uses:` ref is pinned but whose `devtools-ref` input floats.
    #: The README's two-layer commitment: pinning only the file is cosmetic.
    floating_inputs: List[str] = field(default_factory = list)

    @property
    def canonical(self) -> bool:
        return not self.problems

    @property
    def ref_state(self) -> str:
        """``pinned`` | ``master`` | ``stale`` | ``none`` (no devtools callers).

        ``master`` wins over ``pinned`` for a mixed repository: one floating
        caller is exactly the exposure the census exists to surface.
        """

        if not self.devtools_refs:
            return "none"

        if set(self.devtools_refs) & FLOATING_REFS:
            return "master"

        if set(self.devtools_refs) == {PINNED_DEVTOOLS_REF}:
            return "pinned"

        return "stale"

    def to_dict(self) -> dict:
        return {"name": self.name, "ref": self.ref, "problems": list(self.problems), "warnings": list(self.warnings), "gates": dict(self.gates), "devtools_refs": list(self.devtools_refs), "ref_state": self.ref_state, "pin_unreadable": self.pin_unreadable, "floating_inputs": list(self.floating_inputs), "canonical": self.canonical}

@dataclass
class fleet_report:
    workspace: str
    repositories: List[repo_report] = field(default_factory = list)
    errors: List[str] = field(default_factory = list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(repo.canonical for repo in self.repositories)

    def to_dict(self) -> dict:
        adoption = {gate: {"adopted": sum(1 for repo in self.repositories if repo.gates.get(gate)), "missing": sum(1 for repo in self.repositories if not repo.gates.get(gate))} for gate in GATE_FILES}
        census = {state: sum(1 for repo in self.repositories if repo.ref_state == state) for state in ("pinned", "master", "stale", "none")}
        floating = floating_repositories(self)
        unreadable = any(repo.pin_unreadable for repo in self.repositories)
        return {"workspace": self.workspace, "repositories": [repo.to_dict() for repo in self.repositories], "summary": {"repositories": len(self.repositories), "clean": sum(1 for repo in self.repositories if repo.canonical), "with_problems": sum(1 for repo in self.repositories if not repo.canonical), "gates": adoption, "devtools_ref_census": census, "current_pin": PINNED_DEVTOOLS_REF, "floating_repositories": len(floating), "pins_ok": (not floating) and (not unreadable)}, "errors": list(self.errors), "ok": self.ok}

#: Refs that float. A tag or a SHA is a pin by definition (init.py's contract),
#: so an older or newer pin is not a failure -- only a branch reference is, and
#: that is the exact hazard the baseline pin was cut for.
FLOATING_REFS = frozenset({"master", "main"})

def floating_repositories(report: fleet_report) -> List[repo_report]:
    """Repositories whose devtools callers float on a branch instead of a pin.

    ``stale`` and ``none`` are deliberately excluded: an older pin is still a
    pin (regenerating it at the baseline is an upgrade, not a repair), a SHA is
    legal, and a checkout that never adopted a gate has no ref to pin at all.
    """

    return [repo for repo in report.repositories if (set(repo.devtools_refs) & FLOATING_REFS) or repo.floating_inputs]

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

def resolve_origin_ref(repo: Path) -> str | None:
    """The remote default-branch ref for *repo*, or ``None`` without a remote.

    Stale checkouts are the norm in this workspace (the 2026-10-24 audit found
    17 repositories behind origin), so the fleet view must read the remote ref
    whenever it exists. ``origin/HEAD`` is missing in nine checkouts, so
    ``origin/master`` / ``origin/main`` are probed by name.
    """

    try:
        head = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], capture_output = True, timeout = 60)
    except (OSError, subprocess.SubprocessError):
        return None

    if head.returncode == 0:
        text = head.stdout.decode("utf-8", errors = "replace").strip()

        if text.startswith("origin/"):
            return text

    for branch in ("master", "main"):
        try:
            probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "origin/%s" % branch], capture_output = True, timeout = 60)
        except (OSError, subprocess.SubprocessError):
            return None

        if (probe.returncode == 0) and probe.stdout.strip():
            return "origin/%s" % branch

    return None

def workflow_texts(repo: Path, ref: str) -> Dict[str, str]:
    """``filename → text`` for a repository's workflow files at *ref*."""
    result = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref, "--", ".github/workflows/"], capture_output = True, timeout = 120)

    if result.returncode != 0:
        raise subprocess.SubprocessError(result.stderr.decode("utf-8", errors = "replace").strip() or "git ls-tree failed")

    texts: Dict[str, str] = {}

    for raw in result.stdout.decode("utf-8", errors = "replace").splitlines():
        if not raw:
            continue

        shown = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (ref, raw)], capture_output = True, timeout = 120)

        if shown.returncode == 0:
            texts[Path(raw).name] = shown.stdout.decode("utf-8", errors = "replace")

    return texts

def worktree_texts(repo: Path) -> Dict[str, str]:
    """``filename → text`` for a repository's workflow files on disk."""
    workflows = repo / ".github" / "workflows"
    texts: Dict[str, str] = {}

    if workflows.is_dir():
        for path in sorted(workflows.glob("*.y*ml")):
            try:
                texts[path.name] = path.read_bytes().decode("utf-8", errors = "replace")
            except OSError:
                continue

    return texts

def write_texts(texts: Dict[str, str], target: Path) -> None:
    """Materialise *texts* as a workflow directory for the canonical checker."""
    target.mkdir(parents = True, exist_ok = True)

    for filename, text in texts.items():
        (target / filename).write_text(text, encoding = "utf-8")

def references_callee(texts: Dict[str, str], callee: str) -> bool:
    """Whether a workflow *job* invokes *callee* (comments and run: text do not count)."""
    remote = "celestia-island/celestia-devtools/.github/workflows/%s" % callee

    for text in texts.values():
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            document = None

        if isinstance(document, dict) and isinstance(document.get("jobs"), dict):
            for job in document["jobs"].values():
                if isinstance(job, dict) and isinstance(job.get("uses"), str) and job["uses"].startswith(remote + "@"):
                    return True

            continue

        # Unparseable workflow: fall back to non-comment `uses:` lines.
        for line in text.splitlines():
            stripped = line.lstrip()

            if stripped.startswith("#"):
                continue

            if re.search(r"uses:\s*[\"']?%s@" % re.escape(remote), line):
                return True

    return False

#: Remote prefix of every devtools reusable workflow call.
_DEVTOOLS_REMOTE_PREFIX = "celestia-island/celestia-devtools/.github/workflows/"

def adopted_gates_from_lines(text: str) -> List[str]:
    """Gate names named by non-comment devtools callee lines (YAML-free fallback)."""

    found: set[str] = set()

    for line in text.splitlines():
        if line.strip().startswith("#") or (_DEVTOOLS_REMOTE_PREFIX not in line):
            continue

        tail = line.split(_DEVTOOLS_REMOTE_PREFIX, 1)[1]
        callee = re.split(r"""[@\s\"'}#]""", tail, maxsplit = 1)[0]

        for gate, filename in GATE_CALLEE_FILES.items():
            if filename == callee:
                found.add(gate)
                break

    return sorted(found)

def adopted_gates_in(text: str) -> List[str]:
    """Gate names a single workflow file invokes (YAML-aware).

    Unlike :func:`references_callee`, which answers for a set of files, this is
    per-file: it lets the pin audit tell "this file has no devtools caller" from
    "this file has one whose ref it cannot read".
    """

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        # Same fallback `references_callee` takes: an unparseable workflow still
        # gets its non-comment `uses:` lines read, because "cannot parse" must
        # not decay into "invokes nothing" -- that is how a flow-map caller in a
        # broken file would go invisible again.
        return adopted_gates_from_lines(text)

    if not isinstance(document, dict) or (not isinstance(document.get("jobs"), dict)):
        return []

    found: set[str] = set()

    for job in document["jobs"].values():
        if not isinstance(job, dict) or (not isinstance(job.get("uses"), str)):
            continue

        target = job["uses"]

        if not target.startswith(_DEVTOOLS_REMOTE_PREFIX):
            continue

        callee = target[len(_DEVTOOLS_REMOTE_PREFIX):].split("@")[0]

        for gate, filename in GATE_CALLEE_FILES.items():
            if filename == callee:
                found.add(gate)
                break

    return sorted(found)

def scan_repository(repo: Path) -> repo_report:
    """Canonical problems + gate adoption for one checkout (origin-first)."""
    report = repo_report(name = repo.name)
    ref = resolve_origin_ref(repo)

    if ref is not None:
        try:
            texts = workflow_texts(repo, ref)
        except (OSError, subprocess.SubprocessError):
            ref = None
            texts = {}
    else:
        texts = {}

    if ref is not None:
        report.ref = ref
    else:
        texts = worktree_texts(repo)

    if not texts:
        report.problems.append(".github/workflows missing — the repository has no CI at all")
        report.gates = {gate: False for gate in GATE_FILES}
        return report

    with tempfile.TemporaryDirectory(prefix = "fleet-audit-") as scratch:
        write_texts(texts, Path(scratch) / "workflows")
        report.problems = workflow_drift_problems(Path(scratch) / "workflows")
        report.warnings = alias_warnings(Path(scratch) / "workflows") + opt_in_drift_warnings(Path(scratch) / "workflows") + unpinned_caller_warnings(Path(scratch) / "workflows")

    report.gates = {gate: references_callee(texts, GATE_CALLEE_FILES[gate]) for gate in GATE_FILES}
    refs = sorted({ref for text in texts.values() for ref in devtools_refs_in(text)})
    report.devtools_refs = refs

    # Fail closed on caller lines the extractor cannot read. Without this, a
    # quoted or otherwise reformatted `uses:` produced no ref, the repository
    # landed on `none`, and the pin verdict read "nothing to pin" -- clean.
    for name, text in sorted(texts.items()):
        for line in unparsed_devtools_uses(text):
            report.pin_unreadable = True
            report.problems.append(f".github/workflows/{name}: a devtools callee line yields no ref — {line}")

    # Second net, for spellings a line scan cannot see at all (a flow map, an
    # inline job): the YAML-aware detection says this FILE invokes a devtools
    # gate, so extracting no ref from it means the pin surface is unreadable --
    # not that the file pins nothing. Per-file, not per-repo: a repository with
    # one canonical caller and one unreadable one still extracts a ref.
    for name, text in sorted(texts.items()):
        pinned_uses = [ref for ref in devtools_refs_in(text) if ref not in FLOATING_REFS]
        floating_inputs = [ref for ref in devtools_ref_inputs_in(text) if ref in FLOATING_REFS]

        if pinned_uses and floating_inputs:
            report.floating_inputs.append(f".github/workflows/{name}: devtools-ref {', '.join(sorted(set(floating_inputs)))}")

        adopted = adopted_gates_in(text)

        if (not adopted) or devtools_refs_in(text):
            continue

        report.pin_unreadable = True
        report.problems.append(f".github/workflows/{name}: invokes devtools gate(s) {', '.join(adopted)} but no caller ref could be extracted — the pin audit cannot see them")

    return report

def audit_workspace(workspace: Path) -> fleet_report:
    """Scan every immediate git checkout under *workspace*."""
    workspace = workspace.resolve()

    if not workspace.is_dir():
        raise fleet_audit_error("not a directory: %s" % workspace)

    repositories = discover_repositories(workspace)

    if not repositories:
        raise fleet_audit_error("no git checkouts found under %s" % workspace)

    result = fleet_report(workspace = str(workspace))

    for repo in repositories:
        result.repositories.append(scan_repository(repo))

    return result

def _render_text(report: fleet_report) -> str:
    lines: List[str] = []

    for repo in report.repositories:
        if repo.canonical and not repo.warnings:
            continue

        lines.append("%s:" % repo.name)

        for problem in repo.problems:
            lines.append("  - %s" % problem)

        for warning in repo.warnings:
            lines.append("  ! %s" % warning)

    if lines:
        lines.append("")

    adopted = {gate: sum(1 for repo in report.repositories if repo.gates.get(gate)) for gate in GATE_FILES}
    census = {state: sum(1 for repo in report.repositories if repo.ref_state == state) for state in ("pinned", "master", "stale", "none")}
    lines.append("fleet-audit: %d repositories, %d canonical, %d with problems" % (len(report.repositories), sum(1 for repo in report.repositories if repo.canonical), sum(1 for repo in report.repositories if not repo.canonical)))
    lines.append("gate adoption: " + ", ".join("%s=%d" % (gate, adopted[gate]) for gate in GATE_FILES))
    lines.append("devtools refs (pin @%s): pinned=%d master=%d stale=%d none=%d" % (PINNED_DEVTOOLS_REF, census["pinned"], census["master"], census["stale"], census["none"]))
    floating = floating_repositories(report)

    if floating:
        lines.append("floating callers in %d repositories: one commit on master redefines all of their gate semantics at once" % len(floating))
        lines.append("  migrate: celestia-devtools init --with-workflows --force   (in each repository below; add --gate <name> for every opt-in gate it carries — --force alone does not regenerate a gate that was not requested, and a hand-written caller has no generator)")
        lines.append("  " + " ".join(sorted(repo.name for repo in floating)))

    return "\n".join(lines)

def _render_error_json(workspace: Path, message: str) -> dict:
    gates = {gate: {"adopted": 0, "missing": 0} for gate in GATE_FILES}
    # ``pins_ok`` is False and the count is null, not 0: an unfinished scan cannot
    # establish the property, and a consumer that reads ``pins_ok`` must not see
    # "true" for a run that never looked.
    return {"workspace": str(workspace.absolute()), "repositories": [], "summary": {"repositories": 0, "clean": 0, "with_problems": 0, "gates": gates, "pins_ok": False, "floating_repositories": None}, "errors": [message], "ok": False}

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools fleet-audit", description = "Report canonical-caller adoption across the workspace's git checkouts.")
    parser.add_argument("workspace", nargs = "?", default = ".", help = "workspace root holding the repositories (default: current directory)")
    parser.add_argument("--json", action = "store_true", help = "emit the machine-readable report on stdout")
    # Mutually exclusive on purpose: `--warn-only --strict-pins` used to let the
    # softer flag win silently, so a caller could ask for enforcement and get 0.
    behaviour = parser.add_mutually_exclusive_group()
    behaviour.add_argument("--warn-only", action = "store_true", help = "report problems without failing the run")
    behaviour.add_argument("--strict-pins", action = "store_true", help = "also fail when a caller floats on a branch instead of a pinned ref")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace)

    try:
        report = audit_workspace(workspace)
    except (fleet_audit_error, OSError, subprocess.SubprocessError) as exc:
        print("fleet-audit: %s" % exc, file = sys.stderr)

        if args.json:
            print(json.dumps(_render_error_json(workspace, str(exc)), indent = 2, sort_keys = True))

        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent = 2, sort_keys = True))
    else:
        print(_render_text(report), file = sys.stderr)

    pinned = not floating_repositories(report)

    if (report.ok and (pinned or not args.strict_pins)) or args.warn_only:
        return 0

    return 1

if __name__ == "__main__":
    raise SystemExit(main())
