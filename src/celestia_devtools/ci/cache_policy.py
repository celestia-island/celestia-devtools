#!/usr/bin/env python3
"""Fail a self-hosted workflow whose ``actions/cache`` step costs more than it saves.

*Why this exists:* the org's self-hosted runners sit on one SATA controller
(``sda`` = ``/``, ``sdb`` = ``/mnt/ci-cache``, both ``ROTA=1``). On 2026-09-16 a
load spike was traced to ``actions/cache`` archive steps (``tar`` + ``zstdmt``) that
pinned runners in ``D`` state for hours -- one ``tar`` was observed alive for 1h42m
with only 39s of CPU, i.e. 99.9% IO wait -- while 2-4 of the 4 runners were stuck at
once, starving the whole org's queue.

The caches causing that were not one repository's mistake. Measured on the remote
default branches on 2026-09-20, across seven repositories and 54 ``actions/cache``
steps, three shapes recur:

``target/``
    Cargo's *build* directory. Multi-gigabyte, keyed on ``hashFiles('**/Cargo.lock')``
    so a dependency bump rewrites the whole archive, and under GitHub's 10 GB-per-repo
    cache budget it evicts the small caches that actually hit. A self-hosted runner
    already keeps its workspace between jobs, so the archive is mostly pure cost.

``~/.cargo``
    The runner's home is persistent, so this caches what is already there. The
    provisioning script has warned about this specific path since 2026-09-15: a cache
    restore of ``~/.cargo`` silently reverts ``[net] git-fetch-with-cli``, which is why
    that setting needs a runner-level environment variable to survive.

``${{ env.CARGO_HOME }}``
    Expands to the empty string unless the workflow defines ``CARGO_HOME`` in an
    ``env:`` block, which turns the path into ``/registry`` and ``/git`` at the
    filesystem root. The step then caches nothing: a cache that looks configured and
    silently does nothing. Sixteen such steps existed in one repository alone.

*A rule that is deliberately absent:* the obvious "cache size" check. Nothing local can
measure a repository's ``target/`` size without building it, and a rule that guesses is
worse than no rule.

*Rules* (each finding carries ``path:line`` and a recomputable criterion):

``self-hosted-build-dir-cache``
    a self-hosted job caching ``target/`` (or ``*/target/``).
``self-hosted-cargo-home-cache``
    a self-hosted job caching ``~/.cargo``, ``$HOME/.cargo`` or ``${HOME}/.cargo``.
``undefined-env-cache-path``
    a cache ``path:`` using ``${{ env.X }}`` where ``X`` is defined nowhere in the file
    (no workflow-level or job-level ``env:`` entry). This is the ``/registry`` shape
    above: the step reports success and caches nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:  # PyYAML is a runtime dependency of this package; keep the failure loud.
    import yaml
except ImportError:  # pragma: no cover - exercised only without the dependency
    yaml = None  # type: ignore[assignment]

_SELF_HOSTED = "self-hosted"
_CACHE_USES_RE = re.compile(r"^actions/cache(@|$)")
# ``${{ env.NAME }}`` / ``${{env.NAME}}`` -- whitespace inside the braces is optional.
_ENV_REF_RE = re.compile(r"\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_CARGO_HOME_PATHS = ("~/.cargo", "$HOME/.cargo", "${HOME}/.cargo")


def _uses_is_cache(uses: object) -> bool:
    return isinstance(uses, str) and bool(_CACHE_USES_RE.match(uses))


def _runs_on_is_self_hosted(runs_on: object) -> bool:
    if isinstance(runs_on, str):
        return _SELF_HOSTED in runs_on
    if isinstance(runs_on, list):
        return any(isinstance(item, str) and _SELF_HOSTED in item for item in runs_on)
    return False


def _path_entries(path_value: object) -> List[str]:
    """Normalise a cache ``path:`` into individual entries.

    Accepts the scalar form and the block form (a list, or a newline-joined string,
    which is how YAML represents a ``|`` block).
    """
    if path_value is None:
        return []
    if isinstance(path_value, list):
        return [str(item).strip() for item in path_value]
    return [line.strip() for line in str(path_value).splitlines()]


def _workflow_env_names(doc: dict) -> set:
    """Names bound by any ``env:`` mapping in the file (workflow level and job level)."""
    names = set()

    def collect(env: object) -> None:
        if isinstance(env, dict):
            names.update(str(key) for key in env)

    collect(doc.get("env"))
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict):
                collect(job.get("env"))
    return names


def _iter_steps(doc: dict):
    """Yield ``(job_id, job, step_index, step)`` for every job step in the document."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if isinstance(step, dict):
                yield str(job_id), job, index, step


def _cache_uses_lines(text: str) -> List[int]:
    """1-based line numbers of every ``uses: actions/cache...`` line, in file order.

    Findings must carry an accurate ``path:line``, and the steps are yielded in the same
    order by :func:`_iter_steps`, so the two lists line up positionally. Falling back to
    "search for the literal again" would report the first cache step's line for all of
    them, which is exactly the kind of plausible-looking wrong number this scanner exists
    to avoid.
    """
    lines: List[int] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("- uses:") or stripped.startswith("uses:"):
            value = stripped.split("uses:", 1)[1].strip().split("#", 1)[0].strip()
            if _CACHE_USES_RE.match(value):
                lines.append(number)
    return lines


def scan_text(text: str, path: "Path | str" = "<memory>", rel: Optional[str] = None) -> List[dict]:
    """Return the findings for one workflow's text.

    Split out from :func:`scan_file` so tests can exercise the rules without touching
    the filesystem, and so the same text can be audited from a remote ref.
    """
    label = rel or str(path)
    if yaml is None:  # pragma: no cover - import guard
        raise RuntimeError("PyYAML is required for the cache policy scan")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - malformed input is a caller error
        return [
            {
                "rule": "unparseable-workflow",
                "severity": "error",
                "path": label,
                "line": 1,
                "detail": f"workflow is not valid YAML: {exc}",
            }
        ]
    if not isinstance(doc, dict):
        return []

    defined_env = _workflow_env_names(doc)
    uses_lines = _cache_uses_lines(text)
    findings: List[dict] = []
    cache_step_index = 0

    for job_id, job, _index, step in _iter_steps(doc):
        if not _uses_is_cache(step.get("uses")):
            continue
        with_block = step.get("with")
        line = uses_lines[cache_step_index] if cache_step_index < len(uses_lines) else 1
        cache_step_index += 1
        if not isinstance(with_block, dict):
            continue
        entries = _path_entries(with_block.get("path"))
        self_hosted = _runs_on_is_self_hosted(job.get("runs-on"))

        for entry in entries:
            entry_norm = entry.rstrip("/")

            if self_hosted and re.fullmatch(r"(\*/)?target", entry_norm):
                findings.append(
                    {
                        "rule": "self-hosted-build-dir-cache",
                        "severity": "error",
                        "path": label,
                        "line": line,
                        "detail": (
                            f"job '{job_id}' caches '{entry}' on a self-hosted runner: "
                            "cargo's build directory is multi-gigabyte and the runner's "
                            "workspace already persists between jobs"
                        ),
                    }
                )

            if self_hosted and entry_norm in _CARGO_HOME_PATHS:
                findings.append(
                    {
                        "rule": "self-hosted-cargo-home-cache",
                        "severity": "error",
                        "path": label,
                        "line": line,
                        "detail": (
                            f"job '{job_id}' caches '{entry}' on a self-hosted runner: "
                            "the runner home is persistent, and restoring this path "
                            "reverts cargo's [net] git-fetch-with-cli setting"
                        ),
                    }
                )

            for name in sorted(set(_ENV_REF_RE.findall(entry))):
                if name in defined_env:
                    continue
                findings.append(
                    {
                        "rule": "undefined-env-cache-path",
                        "severity": "error",
                        "path": label,
                        "line": line,
                        "detail": (
                            f"job '{job_id}' caches '{entry}' but env.{name} is defined "
                            "nowhere in this file, so the step caches nothing"
                        ),
                    }
                )

    return findings


def scan_file(path: Path, rel: Optional[str] = None) -> List[dict]:
    return scan_text(path.read_text(encoding="utf-8", errors="replace"), path, rel)


def workflow_files(root: Path) -> List[Path]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    return sorted(
        child
        for child in workflows.iterdir()
        if child.is_file() and child.suffix in (".yml", ".yaml")
    )


def collect(repos: Sequence[Path]) -> List[dict]:
    findings: List[dict] = []
    for repo in repos:
        for workflow in workflow_files(repo):
            rel = f"{repo.name}/{workflow.relative_to(repo)}"
            findings.extend(scan_file(workflow, rel))
    return findings


def _discover_repos(roots: Sequence[str]) -> List[Path]:
    """Treat each argument as either a repository root or a directory of checkouts."""
    repos: List[Path] = []
    for raw in roots:
        base = Path(raw).resolve()
        if (base / ".github" / "workflows").is_dir():
            repos.append(base)
            continue
        for child in sorted(base.iterdir()):
            if (child / ".github" / "workflows").is_dir():
                repos.append(child)
    return repos


def _summary(findings: Sequence[dict]) -> str:
    if not findings:
        return "✅ cache policy: no findings"
    by_rule: Dict[str, int] = {}
    for finding in findings:
        by_rule[finding["rule"]] = by_rule.get(finding["rule"], 0) + 1
    parts = ", ".join(f"{rule}={count}" for rule, count in sorted(by_rule.items()))
    return f"❌ cache policy: {len(findings)} finding(s) — {parts}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="celestia-ci-cache",
        description=(
            "Audit actions/cache steps that cost more than they save on self-hosted runners."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="repository roots, or a directory containing checkouts (default: .)",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    repos = _discover_repos(args.paths)
    findings = collect(repos)

    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(f"{finding['path']}:{finding['line']}: {finding['rule']}: {finding['detail']}")
    print(_summary(findings), file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
