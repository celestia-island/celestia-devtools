"""Precheck and the CLI entry for the gate.

Owns the findmnt mount audit, the large-download scan and main(). Split out
of gate.py (2026-10-31); the public entry stays
celestia_devtools.build.gate.main (re-exported by the facade).
"""

from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence, Tuple
from celestia_devtools.core import logger
from celestia_devtools.core.scheduler import (Step)
from celestia_devtools.build.gate_graph import (MODES, _prefix_steps, _print_graph, _run, build_graph, default_jobs, resolve_modes, usage_error)
from celestia_devtools.build.gate_sweep import credential_scan_cli

# ── precheck ──────────────────────────────────────────────────────────────────

_NFS_FSTYPES = {"nfs", "nfs3", "nfs4"}

def findmnt_mounts(findmnt_cmd: List[str] | None = None) -> List[Tuple[str, str]]:
    """Return ``[(target, fstype), ...]`` from ``findmnt -rn -o TARGET,FSTYPE``.

    ``findmnt_cmd`` is injectable for tests. Returns ``[]`` when findmnt is
    unavailable or fails.
    """
    cmd = findmnt_cmd if findmnt_cmd is not None else ["findmnt", "-rn", "-o", "TARGET,FSTYPE"]

    try:
        proc = subprocess.run(cmd, capture_output = True, text = True)
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

        if "/_worktree/" in (target_stripped + "/"):
            warnings.append("NFS mountpoint under a _worktree path: %s (%s) — rm -rf here " "deletes the mount source" % (target_stripped, fstype))

        if target_stripped == cwd_resolved:
            warnings.append("current directory is an NFS mountpoint: %s (%s)" % (target_stripped, fstype))

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
        if not path.is_file() or (path.suffix not in (".py", ".sh")):
            continue

        if any(part in exclude for part in path.parts):
            continue

        try:
            text = path.read_text(encoding = "utf-8", errors = "replace")
        except OSError:
            continue

        if re.search(r"hf_hub_download|huggingface_hub|modelscope", text):
            if not re.search(r"HF_HUB_DISABLE_XET|no_proxy", text):
                warnings.append("%s: model-hub download without HF_HUB_DISABLE_XET=1 / no_proxy hint" % path.relative_to(root))

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

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools gate", description = "Run the local CI gate: modes + DAG ordering + job budget.")
    parser.add_argument("mode", nargs = "?", default = None, choices = list(MODES) + ["precheck", "credential-scan"], help = "gate mode (rust/web/python/all); omit to auto-detect; 'precheck' runs safety " "checks; 'credential-scan' sweeps .git/config + source trees for secrets")
    parser.add_argument("--list", action = "store_true", help = "print the resolved step graph without executing")
    parser.add_argument("--jobs", type = int, default = None, help = "max parallel steps (default: cpu count capped at 4)")
    parser.add_argument("--coverage", action = "store_true", help = "(rust/all) enable cargo tarpaulin coverage")
    parser.add_argument("--repo-root", default = None, help = "repo root directory (default: current directory)")
    # credential-scan options.
    parser.add_argument("--scan-root", default = None, help = "(credential-scan) directory holding the checkouts to sweep " "(default: the parent of the repo root)")
    parser.add_argument("--repo", action = "append", default = [], metavar = "NAME", help = "(credential-scan) sweep <scan-root>/NAME (repeatable)")
    parser.add_argument("--all-langyo", action = "store_true", help = "(credential-scan) sweep every langyo/* business checkout under --scan-root")
    parser.add_argument("--git-config-only", action = "store_true", help = "(credential-scan) only read .git/config files, skip source trees")
    parser.add_argument("--no-git-config", action = "store_true", help = "(credential-scan) only read source trees, skip .git/config files")
    parser.add_argument("--show-reports", action = "store_true", help = "(credential-scan) also print placeholder/env-referenced hits")
    parser.add_argument("--fail-on-skip", action = "store_true", help = "(credential-scan) exit 1 when any path could not be read " "(an incomplete sweep is not a pass)")
    args = parser.parse_args(argv)

    root = Path(args.repo_root or ".").resolve()

    if args.mode == "precheck":
        return precheck(root)

    if args.mode == "credential-scan":
        return credential_scan_cli(root, args)

    jobs = args.jobs if args.jobs is not None else default_jobs()

    if jobs < 1:
        print("error: --jobs must be >= 1", file = sys.stderr)
        return 2

    try:
        modes = resolve_modes(root, args.mode)
    except usage_error as exc:
        print("error: %s" % exc, file = sys.stderr)
        return 2

    plans: List[Tuple[str, List[Step]]] = [(mode, build_graph(root, mode, coverage = args.coverage))for mode in modes]

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
