"""Mode detection and the build-step graph for the gate.

Owns mode detection/resolution, checkout-staleness probes, the credential
and commit-lint step factories, the rust/web/python graph builders and the
parallel runner/summary. Split out of gate.py (2026-10-31).
"""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple
from celestia_devtools.core import logger
from celestia_devtools.core.scheduler import (FAIL, PASS, SKIP, SKIP_DEP, Step, run_dag)
from celestia_devtools.vcs.commit_msg import EASY_HYDRO_REPOS, lint as lint_subject
from celestia_devtools.build.gate_cred import classify_credential_line
from celestia_devtools.build.gate_sweep import _changed_files, _git_output, _repo_name

MODES = ("rust", "web", "python", "all")

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

class usage_error(Exception):
    """Raised for CLI usage problems (mapped to exit code 2)."""

def resolve_modes(root: Path, mode: str | None) -> List[str]:
    """Resolve the CLI mode argument to a concrete list of modes."""

    if mode in ("rust", "web", "python"):
        return [mode]

    detected = detect_modes(root)

    if not detected:
        raise usage_error("no project detected (no Cargo.toml / package.json / " "pnpm-workspace.yaml / pyproject.toml); pass an explicit mode")

    if mode == "all":
        return detected

    # mode is None — auto-detect.
    return detected

def default_jobs() -> int:
    """Default concurrency budget: cpu count, capped at 4."""
    return max(1, min(os.cpu_count() or 1, 4))

# ── Git helpers (internal steps) ──────────────────────────────────────────────

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
                text = path.read_text(encoding = "utf-8", errors = "replace")
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
                print("  " + item, file = sys.stderr)

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
            violations = lint_subject(subject, allow_cjk = allow_cjk)

            if violations:
                bad += 1
                errors.append(subject)
                errors.extend("  - " + v for v in violations)

        if errors:
            logger.error("lint-commits: %d invalid subject(s)" % bad)
            print("\n".join(errors), file = sys.stderr)
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

    def add(name: str, cmd: List[str] | Callable[[], str] | None) -> None:
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

        if lock_path.is_file() and (lock_path.stat().st_mtime > node_modules.stat().st_mtime):
            return True

    return False

def build_web_graph(root: Path) -> List[Step]:
    """Canonical web DAG: credential-scan → install* → lint → build → test →
    lint-commits (* install only when deps changed)."""
    steps: List[Step] = [Step("credential-scan", _credential_scan(root), (), str(root))]
    prev = "credential-scan"

    def add(name: str, cmd: List[str] | Callable[[], str] | None) -> None:
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
    """Canonical python DAG: ruff-check → py-style → pytest → lint-commits.

    `py-style` (the handwritten checker) owns the layout conventions; no
    formatter runs in this gate. See the style-policy block in pyproject.toml.
    """
    steps: List[Step] = [Step("ruff-check", ["ruff", "check", "."], (), str(root))]
    style_cmd = [sys.executable, "-m", "celestia_devtools", "py-style", "check", "."]
    steps.append(Step("py-style", style_cmd, ("ruff-check",), str(root)))
    steps.append(Step("pytest", ["pytest"], ("py-style",), str(root)))
    steps.append(Step("lint-commits", _lint_commits(root), ("pytest",), str(root)))
    return steps

def build_graph(root: Path, mode: str, coverage: bool = False) -> List[Step]:
    if mode == "rust":
        return build_rust_graph(root, coverage = coverage)

    if mode == "web":
        return build_web_graph(root)

    if mode == "python":
        return build_python_graph(root)

    raise usage_error("unknown mode: %s" % mode)

def _prefix_steps(steps: List[Step], prefix: str) -> List[Step]:
    return [Step("%s:%s" % (prefix, s.name), s.cmd, tuple("%s:%s" % (prefix, d)for d in s.deps), s.cwd)for s in steps]

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
    return PASS if subprocess.run(cmd, cwd = cwd).returncode == 0 else FAIL

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
        print("%-18s %-8s %8.2f" % (step.name, _DISPLAY.get(status, status), durations.get(step.name, 0.0)))

    failed = sum(1 for s in statuses.values() if s == FAIL)
    print("-" * 36)
    print("%d step(s), %d failed" % (len(statuses), failed))

def _print_graph(root: Path, jobs: int, plans: List[Tuple[str, List[Step]]]) -> None:
    print("repo: %s" % root)
    print("jobs: %d" % jobs)

    for label, steps in plans:
        print()
        print("mode: %s" % label)
        width = max((len(s.name) for s in steps), default = 0)

        for step in steps:
            deps = ", ".join(step.deps) if step.deps else "-"
            print("  %-*s  deps: [%s]" % (width, step.name, deps))

def _run(steps: List[Step], jobs: int) -> int:
    durations: Dict[str, float] = {}
    statuses = run_dag(steps, _make_runner(durations), jobs)
    _print_summary(steps, statuses, durations)
    return 1 if any(s == FAIL for s in statuses.values()) else 0
