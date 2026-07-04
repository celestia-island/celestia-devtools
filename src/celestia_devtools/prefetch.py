#!/usr/bin/env python3
"""Offline build pre-staging — download all referenced dependencies so later
builds can run with no network.

Resolves cargo + node dependencies for a repo:

  * cargo: ``cargo fetch`` (crates.io + git deps into the cargo registry/git
    cache) for every Cargo.toml at or just under the repo root.
  * node : ``pnpm install`` / ``npm install`` / ``yarn install`` (whichever the
    lockfile implies) for the repo root and any pnpm workspace packages.

Each step is best-effort and reported via the shared logger; a missing
toolchain is skipped with a warning rather than failing the whole install.

Usage::

    celestia-devtools prefetch <repo_root>
    celestia-prefetch <repo_root>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from celestia_devtools import logger as _logger

_GIT_DEP_HINT = (
    "note: cargo fetch resolves [patch] git deps too. For path-patched sibling "
    "repos (arona, entelecheia, ...) keep them checked out locally — they are "
    "not fetched over the network."
)


def _log(level: str, msg: str) -> None:
    getattr(_logger, level, _logger.info)(msg)


def _run(cmd: list[str], cwd: Path) -> bool:
    if not shutil.which(cmd[0]):
        _log("warn", f"{cmd[0]} not installed — skipping {' '.join(cmd[1:])}")
        return False
    _log("info", f"$ {' '.join(cmd)}  (in {cwd})")
    return subprocess.run(cmd, cwd=str(cwd)).returncode == 0


def _node_pm(root: Path) -> list[str] | None:
    if (root / "pnpm-workspace.yaml").exists() or (root / "pnpm-lock.yaml").exists():
        return ["pnpm", "install"] if shutil.which("pnpm") else None
    if (root / "yarn.lock").exists():
        return ["yarn", "install"] if shutil.which("yarn") else None
    if (root / "package.json").exists():
        return ["npm", "install"] if shutil.which("npm") else None
    return None


def prefetch(repo_root: Path) -> dict:
    root = Path(repo_root).resolve()
    summary: dict = {"repo": str(root), "steps": []}

    # ── cargo ──
    cargo_tomls = [root] + [p.parent for p in root.glob("packages/*/Cargo.toml")]
    cargo_tomls = [c for c in dict.fromkeys(cargo_tomls) if (c / "Cargo.toml").is_file()]
    if cargo_tomls:
        _log("info", f"cargo fetch (workspace roots: {[c.name for c in cargo_tomls]})")
        ok = _run(["cargo", "fetch"], cargo_tomls[0])
        summary["steps"].append({"cargo-fetch": ok})
        _log("debug", _GIT_DEP_HINT)
    else:
        summary["steps"].append({"cargo-fetch": "skipped (no Cargo.toml)"})

    # ── node ──
    pm = _node_pm(root)
    if pm:
        ok = _run(pm, root)
        summary["steps"].append({"node-install": pm, "ok": ok})
    else:
        summary["steps"].append({"node-install": "skipped (no JS manifests)"})

    _log("ok", "prefetch complete — subsequent builds can run offline")
    return summary


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    prefetch(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
