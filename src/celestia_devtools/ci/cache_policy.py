#!/usr/bin/env python3
"""Fail a self-hosted workflow whose ``actions/cache`` step costs more than it saves.

*Why this exists:* all four self-hosted runners are VMs on a single 8 TB spinning disk
(``RAID阵列`` = ST8000NM0055, ``Is SSD: false``; the NVMe datastore hosts none of them), and
a runner's cargo home, its ``target/`` tree and the cache archive it writes all live on the
same root VMDK. The dedicated ``/mnt/ci-cache`` is on that same spindle -- measured 0.8-1.6x
the root disk, so it is not a throughput fix -- and on one runner it is not even mounted.

The ``actions/cache`` post-job save then tars a tree that is *already on the disk*: caught
live, ``tar`` spent 45+ minutes at ~790 KB/s and ~1% CPU in ``folio_wait_bit_common`` while
blocking on 141,224 files (3.3 GiB, median 3.5 KB) -- with the job's own build steps already
finished. Burned that way, a runner holds its slot, and with 2-4 victims at once the whole
org's queue starves. A job-level ``timeout-minutes`` does not help: it bounds the job, not a
post-job step.

Because the runners are non-ephemeral (cargo registry accumulated Jul 5 -> Sep 19, a
``target/`` tree from Sep 12 was still present), caching these trees saves nothing that
persistence does not already provide. Measured on the remote default branches: 29 of 55
org-wide cache steps cache ``target/``.

*A rule deliberately absent:* "the cache is too big". Nothing local can measure a
repository's ``target/`` size without building it, and a guess produces exactly the kind of
finding that gets a gate waived -- along with the real ones.

*A rule that had to be withdrawn:* an earlier revision flagged ``${{ env.CARGO_HOME }}`` as
"defined nowhere, so the step caches nothing". That was **false** and misleading in the
dangerous direction: ``dtolnay/rust-toolchain``, which runs immediately before these cache
steps, injects ``CARGO_HOME`` through ``$GITHUB_ENV`` at runtime, so the expression resolves
to the real cargo home. See :func:`_resolves_to_cargo_home`.

*Rules* (each finding carries ``path:line`` and a recomputable criterion):

``self-hosted-build-dir-cache``
    a self-hosted job caching ``target/`` (or ``*/target/``) -- the largest tree, and the
    one whose *restore* also sits on the critical path before the build starts.
``self-hosted-cargo-home-cache``
    a self-hosted job caching a cargo-home registry/git tree, whether written as
    ``~/.cargo/...`` or through ``${{ env.CARGO_HOME }}``: the archive is duplicated work
    on a persistent runner.
``self-hosted-cache-without-step-timeout``
    a cache step on a self-hosted job with no ``timeout-minutes`` of its own. Severity
    ``warning``: this does not remove the cost, it bounds how long a runner can be held.
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

# Paths that end up at the cargo home's registry/git trees.
_CARGO_HOME_PATHS = ("~/.cargo", "$HOME/.cargo", "${HOME}/.cargo")
_CARGO_TREE_SUFFIXES = ("registry", "git")


def _timeout_minutes(step: dict):
    """The step's own ``timeout-minutes``, or None. Only a literal integer counts."""
    value = step.get("timeout-minutes")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _resolves_to_cargo_home(entry: str) -> bool:
    """Whether a cache ``path`` entry lands inside the cargo home.

    This is deliberately *not* a "the variable is undefined" check. An earlier version of
    this rule assumed ``${{ env.CARGO_HOME }}`` expands to nothing unless the workflow
    defines it in an ``env:`` block, and flagged 50 steps as caching nothing. That was
    wrong, and wrong in the most damaging direction -- it implied the cargo archive was
    harmless, when measurement showed the cargo archive *is* the process that pinned a
    runner in ``D`` state for 45 minutes.

    What actually happens: on these workflows the immediately preceding step is
    ``dtolnay/rust-toolchain``, which writes ``CARGO_HOME=${CARGO_HOME:-$HOME/.cargo}`` to
    ``$GITHUB_ENV``; GitHub merges those writes into the ``env`` context of subsequent
    steps, so the expression resolves at runtime to the real cargo home
    (``/home/lab/.cargo``, measured from the live archive's ``INPUT_PATH`` and from the
    generated cache manifest). A static reader cannot see that injection, which is exactly
    why the rule is written against the *resolved shape* instead of the variable's
    presence in the YAML.
    Normalisation is explicit rather than clever: strip expressions and quotes, map every
    cargo-home spelling onto one token, then match the tree underneath it.
    """
    normalized = entry.strip().strip("'\"")
    if not normalized:
        return False
    normalized = _ENV_REF_RE.sub("$CARGO_HOME", normalized)
    for spelling in ("${CARGO_HOME}", "$CARGO_HOME", "~", "$HOME", "${HOME}"):
        if normalized == spelling or normalized.startswith(f"{spelling}/"):
            normalized = "$CARGO_HOME" + normalized[len(spelling) :]
            break
    if normalized == "$CARGO_HOME/.cargo":
        normalized = "$CARGO_HOME"
    elif normalized.startswith("$CARGO_HOME/.cargo/"):
        normalized = "$CARGO_HOME/" + normalized[len("$CARGO_HOME/.cargo/") :]

    trimmed = normalized.rstrip("/")
    if trimmed == "$CARGO_HOME":
        return True
    return any(
        trimmed == f"$CARGO_HOME/{suffix}" or trimmed.startswith(f"$CARGO_HOME/{suffix}/")
        for suffix in _CARGO_TREE_SUFFIXES
    )


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

            if self_hosted and _resolves_to_cargo_home(entry):
                findings.append(
                    {
                        "rule": "self-hosted-cargo-home-cache",
                        "severity": "error",
                        "path": label,
                        "line": line,
                        "detail": (
                            f"job '{job_id}' caches '{entry}' on a self-hosted runner: "
                            "the runner home and workspace persist between jobs, so the "
                            "save tars a tree that is already on the disk -- 3.3 GiB across "
                            "141k small files in the measured case, which is the archive "
                            "that pinned a runner in D state for 45 minutes"
                        ),
                    }
                )

            if self_hosted and _timeout_minutes(step) is None:
                findings.append(
                    {
                        "rule": "self-hosted-cache-without-step-timeout",
                        "severity": "warning",
                        "path": label,
                        "line": line,
                        "detail": (
                            f"job '{job_id}' cache step has no 'timeout-minutes': a job-level "
                            "timeout does not bound a post-job step, so a slow archive can "
                            "outlive the job and hold the runner"
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
