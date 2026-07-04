#!/usr/bin/env python3
"""Per-repo cargo build-cache guard.

Manages the ``target/`` of the repo it is pointed at (one repo per
invocation). Two budgets, cheapest check first:

1. **Hard limit — free-disk floor (default 10 GiB).**
   ``shutil.disk_usage`` is O(1). Below the floor → ``cargo clean`` immediately.
   This is the safety net that originally forced disabling incremental
   compilation: a single workspace's target/ ballooned to ~300 GiB and
   exhausted the disk. With this floor, incremental can stay on.

2. **Soft threshold — target size (default 40 GiB).**
   At/above the threshold → ``cargo sweep --time <days>`` (default 7), which
   reclaims artifacts in target/ older than N days that are NOT part of the
   current build graph. This is the only safe way to trim a deps-heavy target
   (compiled .rlib/.rmeta in deps/ is typically 80%+ of the size and is the
   irreducible active build set — removing incremental/ alone barely dents it
   AND throws away the incremental speedup). Requires ``cargo install
   cargo-sweep``; if absent, the soft step is skipped with a warning
   (incremental is preserved).

Size measurement is the slow part (a 300 GiB target/ defeats a full ``du``), so
it is cached in a sidecar (``target/.cache_guard.json``) and recomputed only
every ``recheck_interval`` (default 6 h). The free-space check is instant and
runs every invocation. If ``du`` cannot finish within ``du_timeout``, the
target is assumed over threshold.

Usage::

    celestia-devtools cache-guard <repo_root> [options]
    cargo-cache-guard <repo_root> [options]
    cargo-cache-guard <repo_root> --clean-incremental   # manual
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time  # noqa: F401 — kept for parity/future use
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celestia_devtools.core import logger as _logger

GIB = 1024 ** 3

# Defaults — overridable via env (CARGO_CACHE_GUARD_*) or CLI flags.
DEF_MIN_FREE_GIB = float(os.environ.get("CARGO_CACHE_GUARD_MIN_FREE_GIB", "10"))
DEF_SOFT_THRESHOLD_GIB = float(os.environ.get("CARGO_CACHE_GUARD_SOFT_THRESHOLD_GIB", "40"))
DEF_RECHECK_INTERVAL_S = float(os.environ.get("CARGO_CACHE_GUARD_RECHECK_INTERVAL_S", str(6 * 3600)))
DEF_DU_TIMEOUT_S = float(os.environ.get("CARGO_CACHE_GUARD_DU_TIMEOUT_S", "120"))
DEF_SWEEP_DAYS = int(os.environ.get("CARGO_CACHE_GUARD_SWEEP_DAYS", "7"))

SIDECAR_NAME = ".cache_guard.json"


def _log(level: str, msg: str) -> None:
    getattr(_logger, level, _logger.info)(msg)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_sidecar(target: Path) -> dict[str, Any]:
    try:
        return json.loads((target / SIDECAR_NAME).read_text())
    except (OSError, ValueError):
        return {}


def _write_sidecar(target: Path, data: dict[str, Any]) -> None:
    try:
        (target / SIDECAR_NAME).write_text(json.dumps(data))
    except OSError:
        pass  # non-fatal — caching is best-effort


def _sidecar_fresh(checked_at: str, recheck_interval_s: float) -> bool:
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - checked).total_seconds() < recheck_interval_s


# ── actions ───────────────────────────────────────────────


def _cargo_clean(repo_root: Path) -> None:
    _log("info", f"running `cargo clean` in {repo_root}")
    subprocess.run(["cargo", "clean"], cwd=str(repo_root))
    try:
        (repo_root / "target" / SIDECAR_NAME).unlink()
    except OSError:
        pass


def _cargo_sweep(repo_root: Path, days: int) -> bool:
    """Reclaim target/ artifacts older than `days` not in the build graph.

    Returns True if cargo-sweep ran. Requires `cargo install cargo-sweep`.
    """
    if not shutil.which("cargo-sweep"):
        return False
    _log("info", f"running `cargo sweep --time {days}` in {repo_root}")
    subprocess.run(["cargo", "sweep", "--time", str(days)], cwd=str(repo_root))
    return True


def remove_incremental_caches(target: Path) -> int:
    """Delete `target/**/incremental/` dirs; return the count removed.

    Manual / `--clean-incremental` entry only — the automatic soft step uses
    cargo-sweep instead (see guard()), because incremental/ is usually <15%
    of a deps-heavy target and removing it sacrifices the incremental speedup.
    """
    removed = 0
    for inc_dir in sorted(target.rglob("incremental")):
        if not inc_dir.is_dir():
            continue
        try:
            shutil.rmtree(inc_dir)
            removed += 1
        except OSError as exc:
            _log("warn", f"failed to remove {inc_dir}: {exc}")
    return removed


def _measure_target_size(
    target: Path,
    *,
    recheck_interval_s: float,
    du_timeout_s: float,
) -> float:
    """Return target/ size in bytes, cached in the sidecar.

    A `du` walk of a huge target/ is too slow for every invocation, so the
    result is cached for `recheck_interval_s`. If `du` cannot finish in
    `du_timeout_s`, the target is so large it must be over any sane threshold —
    return infinity to force the soft step (the sidecar is NOT updated, so the
    next interval recomputes once cargo-sweep has trimmed things).
    """
    cached = _read_sidecar(target)
    cached_at = cached.get("checked_at")
    if cached_at and "size_bytes" in cached and _sidecar_fresh(cached_at, recheck_interval_s):
        return float(cached["size_bytes"])

    try:
        out = subprocess.run(
            ["du", "-sb", str(target)],
            capture_output=True,
            text=True,
            timeout=du_timeout_s,
            check=False,
        )
        if out.returncode == 0 and out.stdout.split():
            size = float(out.stdout.split()[0])
            _write_sidecar(target, {"size_bytes": size, "checked_at": _now_iso()})
            return size
    except subprocess.TimeoutExpired:
        _log("warn", f"`du` timed out after {du_timeout_s:.0f}s — target assumed over threshold")
    except (OSError, ValueError) as exc:
        _log("warn", f"`du` failed: {exc}")
    return float("inf")


# ── public API ────────────────────────────────────────────


def guard(
    repo_root: Path,
    *,
    min_free_gib: float = DEF_MIN_FREE_GIB,
    soft_threshold_gib: float = DEF_SOFT_THRESHOLD_GIB,
    recheck_interval_s: float = DEF_RECHECK_INTERVAL_S,
    du_timeout_s: float = DEF_DU_TIMEOUT_S,
    sweep_days: int = DEF_SWEEP_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enforce the cache budgets for `repo_root/target/`.

    Returns a summary dict. Safe when no target/ exists (no-op). Never raises
    on cleanup failures.
    """
    repo_root = repo_root.resolve()
    target = repo_root / "target"
    if not target.is_dir():
        return {"action": "skip", "reason": "no target/"}

    free_before = shutil.disk_usage(target).free

    # ── Hard limit: free-disk floor → cargo clean ─────────
    if free_before < min_free_gib * GIB:
        free_gib = free_before / GIB
        _log("warn",
             f"free space {free_gib:.1f} GiB < floor {min_free_gib:.1f} GiB — "
             f"running cargo clean")
        if dry_run:
            return {"action": "cargo-clean", "dry_run": True, "free_gib_before": free_gib}
        _cargo_clean(repo_root)
        free_after = shutil.disk_usage(target).free
        _log("ok", f"cargo clean done — free {free_gib:.1f} → {free_after / GIB:.1f} GiB")
        return {
            "action": "cargo-clean",
            "free_gib_before": free_gib,
            "free_gib_after": free_after / GIB,
        }

    # ── Soft threshold: target size → cargo sweep ─────────
    # Skip the (slow) size measurement entirely when cargo-sweep is absent —
    # the soft step can't reclaim anything without it, so a 60-120s `du` of a
    # huge target/ would be pure waste on every invocation. The hard floor
    # above still protects the disk.
    if not shutil.which("cargo-sweep"):
        return {
            "action": "none",
            "free_gib": free_before / GIB,
            "soft_skipped": "cargo-sweep not installed",
        }

    size = _measure_target_size(
        target, recheck_interval_s=recheck_interval_s, du_timeout_s=du_timeout_s,
    )
    if size >= soft_threshold_gib * GIB:
        size_gib = (size / GIB) if size != float("inf") else float("inf")
        shown = "inf" if size_gib == float("inf") else f"{size_gib:.1f}"
        if dry_run:
            return {"action": "soft-clean", "dry_run": True, "size_gib": size_gib}
        _log("info",
             f"target {shown} GiB >= threshold {soft_threshold_gib:.1f} GiB — "
             f"cargo sweep --time {sweep_days}")
        _cargo_sweep(repo_root, sweep_days)
        free_after = shutil.disk_usage(target).free
        sidecar = _read_sidecar(target)
        sidecar["soft_cleaned_at"] = _now_iso()
        _write_sidecar(target, sidecar)
        _log("ok", f"cargo sweep done — free {free_before / GIB:.1f} → {free_after / GIB:.1f} GiB")
        return {
            "action": "soft-clean",
            "size_gib": size_gib,
            "free_gib_before": free_before / GIB,
            "free_gib_after": free_after / GIB,
        }

    return {
        "action": "none",
        "free_gib": free_before / GIB,
        "target_size_gib": (size / GIB) if size != float("inf") else None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="cargo target/ cache-size guard")
    p.add_argument("repo_root", nargs="?", default=".",
                   help="repository root containing target/ (default: cwd)")
    p.add_argument("--min-free-gib", type=float, default=DEF_MIN_FREE_GIB)
    p.add_argument("--soft-threshold-gib", type=float, default=DEF_SOFT_THRESHOLD_GIB)
    p.add_argument("--sweep-days", type=int, default=DEF_SWEEP_DAYS)
    p.add_argument("--clean-incremental", action="store_true",
                   help="manually remove target/**/incremental/ and exit "
                        "(does NOT run the budget guard)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    target = Path(args.repo_root).resolve() / "target"
    if args.clean_incremental:
        if target.is_dir():
            n = remove_incremental_caches(target)
            _log("ok", f"removed {n} incremental cache director{'y' if n == 1 else 'ies'}")
        return 0

    guard(
        Path(args.repo_root),
        min_free_gib=args.min_free_gib,
        soft_threshold_gib=args.soft_threshold_gib,
        sweep_days=args.sweep_days,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
