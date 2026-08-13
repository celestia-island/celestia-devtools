#!/usr/bin/env python3
"""Local gate orchestration — one command to reproduce the CI checklist locally.

The workspace relies on CI checks scattered across repos; ``gate`` turns the CI
checklist into a single local command so that "local verification == CI" before
a PR is opened.  It encodes the three design elements from PLAN.md §5:

* **modes** — ``gate rust`` / ``gate web`` / ``gate python`` / ``gate all``.
  With no mode the repo is auto-detected (``Cargo.toml`` → rust,
  ``package.json``/``pnpm-workspace.yaml`` → web, ``pyproject.toml`` → python).
  ``--list`` prints the resolved step graph without executing anything.
* **dependency ordering** — each mode's steps form a DAG (fmt → clippy → test →
  deny → coverage → lint-commits, etc.).  A step runs only after its
  dependencies PASS; a failed step aborts its dependents.
* **concurrency budget** — ``--jobs N`` (default ``os.cpu_count()`` capped at
  4) bounds parallel execution of independent steps via a generic topological
  scheduler (``celestia_devtools.core.scheduler``).

``gate precheck`` is a separate safety subcommand implementing the workspace
postmortem follow-ups: NFS mount-point warnings (``findmnt`` scan for worktree
paths) and a large-download heuristic scan (``hf_hub_download`` /
``huggingface_hub`` / ``modelscope`` without ``HF_HUB_DISABLE_XET`` /
``no_proxy`` hints).  The credential scan runs as a *pre-step* of ``gate rust``
and ``gate web``.

Usage::

    celestia-devtools gate              # auto-detect mode
    celestia-devtools gate rust --list  # dry-run: print the graph
    celestia-devtools gate web --jobs 2
    celestia-devtools gate all --coverage
    celestia-devtools gate precheck
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from celestia_devtools.core import logger
from celestia_devtools.core.scheduler import (
    FAIL,
    PASS,
    SKIP,
    SKIP_DEP,
    Step,
    run_dag,
)
from celestia_devtools.vcs.commit_msg import EASY_HYDRO_REPOS, lint as lint_subject

MODES = ("rust", "web", "python", "all")

# ── Credential scan heuristics ────────────────────────────────────────────────

# Broad "report" pattern: any credential-looking token in a line. The `pass` /
# `pwd` alternation is guarded by lookarounds so prose like "bypass" or "the
# test will pass" is not flagged, while `SSH_PASS` / `--target-pass` are.
_CRED_PATTERN = re.compile(
    r"password|passwd|passphrase|secret|token|api[_-]?key|"
    r"(?<![a-z0-9])(pass|pwd)(?![a-z0-9])|"
    r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY",
    re.IGNORECASE,
)

# Whitelisted placeholder markers (obvious dummy values + RFC 5737 doc IPs).
_PLACEHOLDER_PATTERN = re.compile(
    r"CHANGE[_ ]?ME|<your[-_ ]?password>|<password>|test[-_ ]?password|"
    r"sk-xxx|xxxxx|xxxx|xxx|example|placeholder|redacted|"
    r"192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+",
    re.IGNORECASE,
)

# Reading the value from env/config — no literal secret lives in the tree.
_ENV_REF_PATTERN = re.compile(
    r"os\.environ|getenv|environ\s*\[|\$\{|process\.env|\benv\s*\(",
    re.IGNORECASE,
)

# A PEM private-key header (always suspicious; never a placeholder by default).
_PRIVATE_KEY_PATTERN = re.compile(r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY")

# An assignment whose left-hand key contains a credential word, e.g.
# ``SSH_PASS="..."`` / ``password = value`` / ``api_key: value``.
_ASSIGN_KEY_PATTERN = re.compile(
    r"(?:^|[\s(])"
    r"[A-Za-z0-9_.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|pass|pwd)"
    r"[A-Za-z0-9_.-]*\s*[=:]\s*",
    re.IGNORECASE,
)

# A flag carrying a credential value, e.g. ``--target-pass s3cr3t-value`` or
# ``--api-key=realvalue``.
_FLAG_PATTERN = re.compile(
    r"--?[a-zA-Z0-9_-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|pass|pwd)"
    r"[a-zA-Z0-9_-]*(?:\s|=)\s*",
    re.IGNORECASE,
)


def _first_token(rest: str) -> Optional[str]:
    """Extract the leading value token from the text after an assignment."""
    rest = rest.strip()
    if not rest:
        return None
    if rest[0] in "\"'":
        end = rest.find(rest[0], 1)
        if end > 0:
            return rest[1:end]
        return rest
    return re.split(r"[\s;#]+", rest, maxsplit=1)[0] or None


def _extract_secret_value(line: str) -> Optional[str]:
    """Return the literal value assigned to a credential key, or ``None``."""
    match = _ASSIGN_KEY_PATTERN.search(line)
    if match:
        return _first_token(line[match.end():])
    match = _FLAG_PATTERN.search(line)
    if match:
        return _first_token(line[match.end():])
    return None


def classify_credential_line(line: str) -> str:
    """Classify one source line for the credential scan.

    Returns ``"clean"`` (no credential token), ``"report"`` (a hit that is
    whitelisted or env-sourced — reported but not fatal), or ``"violation"``
    (a literal non-placeholder secret — fatal).
    """
    if not _CRED_PATTERN.search(line):
        return "clean"
    if _PRIVATE_KEY_PATTERN.search(line):
        return "report" if _PLACEHOLDER_PATTERN.search(line) else "violation"
    value = _extract_secret_value(line)
    if value is None:
        return "report"
    if _PLACEHOLDER_PATTERN.search(value) or _ENV_REF_PATTERN.search(line):
        return "report"
    return "violation"


# ── Mode detection ────────────────────────────────────────────────────────────

def detect_modes(root: Path) -> List[str]:
    """Return the list of detectable modes for a repo directory."""
    modes: List[str] = []
    if (root / "Cargo.toml").is_file():
        modes.append("rust")
    if (root / "package.json").is_file() or (root / "pnpm-workspace.yaml").is_file():
        modes.append("web")
    if (root / "pyproject.toml").is_file():
        modes.append("python")
    return modes


class UsageError(Exception):
    """Raised for CLI usage problems (mapped to exit code 2)."""


def resolve_modes(root: Path, mode: Optional[str]) -> List[str]:
    """Resolve the CLI mode argument to a concrete list of modes."""
    if mode in ("rust", "web", "python"):
        return [mode]
    detected = detect_modes(root)
    if not detected:
        raise UsageError(
            "no project detected (no Cargo.toml / package.json / "
            "pnpm-workspace.yaml / pyproject.toml); pass an explicit mode"
        )
    if mode == "all":
        return detected
    # mode is None — auto-detect.
    return detected


def default_jobs() -> int:
    """Default concurrency budget: cpu count, capped at 4."""
    return max(1, min(os.cpu_count() or 1, 4))


# ── Git helpers (internal steps) ──────────────────────────────────────────────

def _git_output(args: List[str], root: Path) -> Optional[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _changed_files(root: Path) -> Optional[List[str]]:
    """Files changed vs origin/master, or ``None`` when unavailable."""
    out = _git_output(["diff", "--name-only", "origin/master...HEAD"], root)
    if out is None:
        return None
    return [f for f in out.splitlines() if f.strip()]


def _repo_name(root: Path) -> str:
    url = _git_output(["config", "--get", "remote.origin.url"], root)
    if url:
        name = url.strip().rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return Path(root).name


def _credential_scan(root: Path) -> Callable[[], str]:
    """Pre-step callable: scan changed files for non-placeholder secrets."""
    def run() -> str:
        files = _changed_files(root)
        if not files:
            logger.info("credential-scan: no changed files vs origin/master — skipping")
            return SKIP
        violations: List[str] = []
        reported = 0
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                verdict = classify_credential_line(line)
                if verdict == "clean":
                    continue
                reported += 1
                if verdict == "violation":
                    violations.append("%s:%d: %s" % (rel, lineno, line.strip()))
        if violations:
            logger.error("credential-scan: %d non-placeholder secret(s) found" % len(violations))
            for item in violations:
                print("  " + item, file=sys.stderr)
            return FAIL
        if reported:
            logger.warn("credential-scan: %d hit(s) — all placeholders/env-referenced" % reported)
        else:
            logger.ok("credential-scan: no credential-looking lines in changed files")
        return PASS
    return run


def _lint_commits(root: Path) -> Callable[[], str]:
    """Validate branch commits against origin/master via commit-msg lint."""
    def run() -> str:
        out = _git_output(["log", "--format=%s", "origin/master..HEAD"], root)
        if out is None:
            logger.warn("lint-commits: no origin/master remote — skipping")
            return SKIP
        subjects = [s for s in out.splitlines() if s.strip()]
        if not subjects:
            logger.info("lint-commits: no commits ahead of origin/master — skipping")
            return SKIP
        allow_cjk = _repo_name(root) in EASY_HYDRO_REPOS
        errors: List[str] = []
        bad = 0
        for subject in subjects:
            violations = lint_subject(subject, allow_cjk=allow_cjk)
            if violations:
                bad += 1
                errors.append(subject)
                errors.extend("  - " + v for v in violations)
        if errors:
            logger.error("lint-commits: %d invalid subject(s)" % bad)
            print("\n".join(errors), file=sys.stderr)
            return FAIL
        logger.ok("lint-commits: %d commit(s) OK" % len(subjects))
        return PASS
    return run


# ── Mode graph builders ───────────────────────────────────────────────────────

def build_rust_graph(root: Path, coverage: bool = False) -> List[Step]:
    """Canonical rust DAG: credential-scan → fmt → clippy → check → deny →
    (coverage) → lint-commits."""
    steps: List[Step] = [Step("credential-scan", _credential_scan(root), (), str(root))]
    prev = "credential-scan"

    def add(name: str, cmd: Optional[Union[List[str], Callable[[], str]]]) -> None:
        nonlocal prev
        steps.append(Step(name, cmd, (prev,), str(root)))
        prev = name

    add("fmt", ["cargo", "fmt", "--check"])
    add("clippy", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"])
    add("check", ["cargo", "test"])
    if shutil.which("cargo-deny"):
        add("deny", ["cargo", "deny", "check"])
    else:
        logger.warn("cargo-deny not installed — skipping 'cargo deny check'")
        add("deny", None)
    if coverage:
        add("coverage", ["cargo", "tarpaulin"])
    add("lint-commits", _lint_commits(root))
    return steps


def _needs_install(root: Path) -> bool:
    """True when node_modules is missing or a lockfile changed since install."""
    node_modules = root / "node_modules"
    if not node_modules.is_dir():
        return True
    for lock in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
        lock_path = root / lock
        if lock_path.is_file() and lock_path.stat().st_mtime > node_modules.stat().st_mtime:
            return True
    return False


def build_web_graph(root: Path) -> List[Step]:
    """Canonical web DAG: credential-scan → install* → lint → build → test →
    lint-commits (* install only when deps changed)."""
    steps: List[Step] = [Step("credential-scan", _credential_scan(root), (), str(root))]
    prev = "credential-scan"

    def add(name: str, cmd: Optional[Union[List[str], Callable[[], str]]]) -> None:
        nonlocal prev
        steps.append(Step(name, cmd, (prev,), str(root)))
        prev = name

    if _needs_install(root):
        add("install", ["pnpm", "install", "--frozen-lockfile"])
    else:
        logger.info("node_modules up to date and lockfile unchanged — skipping install")
        add("install", None)
    add("lint", ["pnpm", "lint"])
    add("build", ["pnpm", "build"])
    add("test", ["pnpm", "test"])
    add("lint-commits", _lint_commits(root))
    return steps


def build_python_graph(root: Path) -> List[Step]:
    """Canonical python DAG: ruff-check → ruff-format → pytest → lint-commits."""
    steps: List[Step] = [Step("ruff-check", ["ruff", "check", "."], (), str(root))]
    steps.append(Step("ruff-format", ["ruff", "format", "--check", "."], ("ruff-check",), str(root)))
    steps.append(Step("pytest", ["pytest"], ("ruff-format",), str(root)))
    steps.append(Step("lint-commits", _lint_commits(root), ("pytest",), str(root)))
    return steps


def build_graph(root: Path, mode: str, coverage: bool = False) -> List[Step]:
    if mode == "rust":
        return build_rust_graph(root, coverage=coverage)
    if mode == "web":
        return build_web_graph(root)
    if mode == "python":
        return build_python_graph(root)
    raise UsageError("unknown mode: %s" % mode)


def _prefix_steps(steps: List[Step], prefix: str) -> List[Step]:
    return [
        Step(
            "%s:%s" % (prefix, s.name),
            s.cmd,
            tuple("%s:%s" % (prefix, d) for d in s.deps),
            s.cwd,
        )
        for s in steps
    ]


# ── Execution ─────────────────────────────────────────────────────────────────

def _normalize_status(result: object) -> str:
    if result is True:
        return PASS
    if result is False:
        return FAIL
    if result in (PASS, FAIL, SKIP):
        return result
    return FAIL


def _execute_step(step: Step) -> str:
    cmd = step.cmd
    if callable(cmd):
        try:
            return _normalize_status(cmd())
        except Exception as exc:
            logger.error("%s: internal step raised %r" % (step.name, exc))
            return FAIL
    if cmd is None:
        return SKIP
    cwd = step.cwd or str(Path.cwd())
    logger.info("$ %s  (in %s)" % (" ".join(cmd), cwd))
    return PASS if subprocess.run(cmd, cwd=cwd).returncode == 0 else FAIL


_DISPLAY = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP", SKIP_DEP: "SKIP"}


def _make_runner(durations: Dict[str, float]) -> Callable[[Step], str]:
    def runner(step: Step) -> str:
        start = time.monotonic()
        status = _execute_step(step)
        durations[step.name] = time.monotonic() - start
        print("[%s] %-18s %6.2fs" % (_DISPLAY.get(status, status), step.name, durations[step.name]))
        return status
    return runner


def _print_summary(steps: List[Step], statuses: Dict[str, str], durations: Dict[str, float]) -> None:
    print()
    print("%-18s %-8s %8s" % ("STEP", "STATUS", "SECONDS"))
    print("-" * 36)
    for step in steps:
        status = statuses.get(step.name, SKIP)
        print("%-18s %-8s %8.2f" % (
            step.name, _DISPLAY.get(status, status), durations.get(step.name, 0.0),
        ))
    failed = sum(1 for s in statuses.values() if s == FAIL)
    print("-" * 36)
    print("%d step(s), %d failed" % (len(statuses), failed))


def _print_graph(root: Path, jobs: int, plans: List[Tuple[str, List[Step]]]) -> None:
    print("repo: %s" % root)
    print("jobs: %d" % jobs)
    for label, steps in plans:
        print()
        print("mode: %s" % label)
        width = max((len(s.name) for s in steps), default=0)
        for step in steps:
            deps = ", ".join(step.deps) if step.deps else "-"
            print("  %-*s  deps: [%s]" % (width, step.name, deps))


def _run(steps: List[Step], jobs: int) -> int:
    durations: Dict[str, float] = {}
    statuses = run_dag(steps, _make_runner(durations), jobs)
    _print_summary(steps, statuses, durations)
    return 1 if any(s == FAIL for s in statuses.values()) else 0


# ── precheck ──────────────────────────────────────────────────────────────────

_NFS_FSTYPES = {"nfs", "nfs3", "nfs4"}


def findmnt_mounts(findmnt_cmd: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """Return ``[(target, fstype), ...]`` from ``findmnt -rn -o TARGET,FSTYPE``.

    ``findmnt_cmd`` is injectable for tests. Returns ``[]`` when findmnt is
    unavailable or fails.
    """
    cmd = findmnt_cmd if findmnt_cmd is not None else ["findmnt", "-rn", "-o", "TARGET,FSTYPE"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    mounts: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        target = parts[0]
        fstype = parts[1].strip() if len(parts) > 1 else ""
        mounts.append((target, fstype))
    return mounts


def precheck_mounts(mounts: List[Tuple[str, str]], cwd: Path) -> List[str]:
    """Return warning strings for dangerous NFS mountpoints."""
    warnings: List[str] = []
    cwd_resolved = str(cwd.resolve()).rstrip("/")
    for target, fstype in mounts:
        if fstype.lower() not in _NFS_FSTYPES:
            continue
        target_stripped = target.rstrip("/")
        if "/_worktree/" in target_stripped + "/":
            warnings.append(
                "NFS mountpoint under a _worktree path: %s (%s) — rm -rf here "
                "deletes the mount source" % (target_stripped, fstype)
            )
        if target_stripped == cwd_resolved:
            warnings.append(
                "current directory is an NFS mountpoint: %s (%s)" % (target_stripped, fstype)
            )
    return warnings


def scan_large_downloads(root: Path) -> List[str]:
    """Warn about model-hub download scripts missing the safety hints.

    Heuristic: any ``.py`` / ``.sh`` file mentioning ``hf_hub_download`` /
    ``huggingface_hub`` / ``modelscope`` that does not also mention
    ``HF_HUB_DISABLE_XET`` or ``no_proxy``.  Warn only, never fail.
    """
    warnings: List[str] = []
    exclude = {".git", "node_modules", "target", "dist", "__pycache__"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if any(part in exclude for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"hf_hub_download|huggingface_hub|modelscope", text):
            if not re.search(r"HF_HUB_DISABLE_XET|no_proxy", text):
                warnings.append(
                    "%s: model-hub download without HF_HUB_DISABLE_XET=1 / no_proxy hint"
                    % path.relative_to(root)
                )
    return warnings


def precheck(root: Path) -> int:
    """Run the ``gate precheck`` safety diagnostics (advisory, exit 0)."""
    if Path("/mnt/codespace").exists():
        mounts = findmnt_mounts()
        for warning in precheck_mounts(mounts, root):
            logger.warn(warning)
    else:
        logger.info("not on the workspace host — skipping mount-point precheck")

    for warning in scan_large_downloads(root):
        logger.warn(warning)

    logger.ok("precheck complete")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools gate",
        description="Run the local CI gate: modes + DAG ordering + job budget.",
    )
    parser.add_argument(
        "mode", nargs="?", default=None,
        choices=list(MODES) + ["precheck"],
        help="gate mode (rust/web/python/all); omit to auto-detect; 'precheck' runs safety checks",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the resolved step graph without executing",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="max parallel steps (default: cpu count capped at 4)",
    )
    parser.add_argument(
        "--coverage", action="store_true",
        help="(rust/all) enable cargo tarpaulin coverage",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="repo root directory (default: current directory)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root or ".").resolve()

    if args.mode == "precheck":
        return precheck(root)

    jobs = args.jobs if args.jobs is not None else default_jobs()
    if jobs < 1:
        print("error: --jobs must be >= 1", file=sys.stderr)
        return 2

    try:
        modes = resolve_modes(root, args.mode)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    plans: List[Tuple[str, List[Step]]] = [
        (mode, build_graph(root, mode, coverage=args.coverage)) for mode in modes
    ]

    if args.list:
        _print_graph(root, jobs, plans)
        return 0

    if len(plans) == 1:
        steps = plans[0][1]
    else:
        merged: List[Step] = []
        for mode, graph in plans:
            merged.extend(_prefix_steps(graph, mode))
        steps = merged

    return _run(steps, jobs)


if __name__ == "__main__":
    raise SystemExit(main())
