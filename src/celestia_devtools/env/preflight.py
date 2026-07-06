#!/usr/bin/env python3
"""Shared environment preflight checks for the celestia-devtools facilities.

Any celestia-devtools subcommand that depends on external tooling (node,
cargo, docker, wsl, …) should run a focused `check()` before doing real
work, so the user gets a single, actionable message — "missing node; run
`just wsl-ensure`" — instead of a stack trace from deep inside npm.

Design:
  * Each tool has one atomic checker returning a `Probe` (found / missing +
    version / install hint). Checkers are cheap (a `--version` subprocess)
    and side-effect free.
  * `check(*reqs)` runs a named subset and prints a one-line-per-tool
    report. Returns the list of unmet reqs (empty = all good).
  * `require(*reqs)` is `check()` + `sys.exit(2)` on any miss, with a
    consolidated hint. Use it at the top of a command's `main()`.

Cross-platform: works on Windows (wsl.exe / where) and Linux/WSL (which).
The "linux-basics" req verifies a non-Windows kernel OR WSL availability so
Linux-targeted facilities (the mock stack) can decide whether to relaunch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Sequence

from celestia_devtools.core import logger as _logger

# WSL_HINT is the canonical "fix this" pointer — every miss that wsl-ensure
# can repair tells the user the same command.
WSL_HINT = "just wsl-ensure"


@dataclass
class Probe:
    """Result of probing one tool. `ok` False → caller should bail or warn."""
    name: str
    ok: bool
    version: str = ""
    detail: str = ""
    # Human hint shown in the report when ok is False (e.g. "run just wsl-ensure").
    hint: str = ""


def _run_version(args: Sequence[str]) -> tuple[bool, str]:
    """Run `<tool> --version`, returning (found, version_string). Swallows all
    errors — a missing tool is a normal preflight outcome, not an exception.

    On Windows, bare command names like `npm` resolve to `npm.cmd` / shim
    scripts that subprocess.run can't find without a shell, so we resolve via
    shutil.which first (cross-platform) and fall back to shell=True."""
    resolved = shutil.which(args[0])
    # Replace just the command name with its resolved path, keep the rest of
    # args (e.g. --version). If which() failed, fall back to shell resolution.
    run_args = ([resolved, *args[1:]] if resolved else list(args))
    try:
        # shell=True only when we couldn't resolve (lets cmd.exe find .cmd/.bat).
        r = subprocess.run(run_args, capture_output=True, text=True, timeout=10,
                           shell=(not resolved and os.name == "nt"))
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False, ""
    if r.returncode != 0:
        return False, ""
    out = (r.stdout or r.stderr).strip()
    line = out.splitlines()[0] if out else ""
    return True, line


# ── Atomic tool probes ────────────────────────────────────────────────
# Each returns a Probe. Naming convention: check_<req_id>. The req id passed
# to check()/require() is the function suffix (e.g. "node" → check_node).


def check_node() -> Probe:
    ok, ver = _run_version(["node", "--version"])
    return Probe("node", ok, ver,
                 hint=f"install via {WSL_HINT} (apt nodejs) or https://nodejs.org")


def check_npm() -> Probe:
    ok, ver = _run_version(["npm", "--version"])
    return Probe("npm", ok, ver, hint=f"ships with nodejs — {WSL_HINT}")


def check_rust() -> Probe:
    ok, ver = _run_version(["rustc", "--version"])
    return Probe("rust", ok, ver, hint="curl https://sh.rustup.rs | sh")


def check_cargo() -> Probe:
    ok, ver = _run_version(["cargo", "--version"])
    return Probe("cargo", ok, ver, hint="part of rustup — install rust first")


def check_just() -> Probe:
    ok, ver = _run_version(["just", "--version"])
    return Probe("just", ok, ver, hint="cargo install just  (or: just wsl-ensure)")


def check_git() -> Probe:
    ok, ver = _run_version(["git", "--version"])
    return Probe("git", ok, ver, hint="system package manager")


def check_docker() -> Probe:
    ok, ver = _run_version(["docker", "--version"])
    return Probe("docker", ok, ver,
                 hint="https://get.docker.com  (or use Docker Desktop + --no-docker)")


def _is_windows() -> bool:
    return os.name == "nt"


def check_wsl() -> Probe:
    """WSL2 availability (Windows only). On Linux this is trivially 'na'."""
    if not _is_windows():
        return Probe("wsl", True, "n/a (already on Linux)")
    if not shutil.which("wsl"):
        return Probe("wsl", False, hint="wsl --install --no-distribution")
    r = subprocess.run(["wsl", "--status"], capture_output=True, timeout=10)
    return Probe("wsl", r.returncode == 0, hint="wsl --install --no-distribution")


def check_linux() -> Probe:
    """A non-Windows kernel is available — either natively or via WSL2."""
    if not _is_windows():
        return Probe("linux", True, "native")
    wsl = check_wsl()
    if not wsl.ok:
        return Probe("linux", False, hint="needs WSL2 — " + wsl.hint)
    # Confirm at least one distro is registered.
    r = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, timeout=10)
    has_distro = bool(r.stdout.strip())
    return Probe("linux", has_distro,
                 hint="wsl --install -d Ubuntu  (then just wsl-ensure)" if not has_distro else "")


# Registry — req id → checker. Adding a new tool = one function + one entry.
CHECKERS = {
    "node": check_node,
    "npm": check_npm,
    "rust": check_rust,
    "cargo": check_cargo,
    "just": check_just,
    "git": check_git,
    "docker": check_docker,
    "wsl": check_wsl,
    "linux": check_linux,
}


def check(*reqs: str, quiet: bool = False) -> list[Probe]:
    """Run the named checks, print a compact report, return the failed ones.

    Unknown req ids are reported as failures with a clear message (typo guard).
    """
    results: list[Probe] = []
    for req in reqs:
        checker = CHECKERS.get(req)
        if checker is None:
            results.append(Probe(req, False, detail=f"unknown requirement '{req}'"))
            continue
        results.append(checker())

    if not quiet:
        _report(results)
    return [r for r in results if not r.ok]


def require(*reqs: str) -> None:
    """Check + exit(2) if any are missing. Use at the top of a command."""
    missing = check(*reqs)
    if missing:
        hints = " | ".join({r.hint for r in missing if r.hint})
        _logger.error(
            f"missing {len(missing)} requirement(s): {[r.name for r in missing]}. "
            + (hints + "." if hints else "Install them and re-run.")
        )
        sys.exit(2)


def _report(results: list[Probe]) -> None:
    """One line per probe: ✓/✗ name version (hint if missing)."""
    for r in results:
        mark = "✓" if r.ok else "✗"
        if r.ok:
            _logger.info(f"  {mark} {r.name:<10} {r.version}")
        else:
            extra = f"  → {r.hint}" if r.hint else ""
            detail = f" ({r.detail})" if r.detail else ""
            _logger.warn(f"  {mark} {r.name:<10} MISSING{detail}{extra}")


# ── CLI entry: `celestia-devtools preflight [reqs...]` ─────────────────


def main() -> int:
    import argparse  # local import keeps the module importable in odd envs

    p = argparse.ArgumentParser(
        prog="celestia-devtools preflight",
        description="Check that required dev tools are present. "
                    f"Available: {', '.join(CHECKERS)}",
    )
    p.add_argument("reqs", nargs="*", default=list(CHECKERS),
                   help=f"requirements to check (default: all = {', '.join(CHECKERS)})")
    p.add_argument("--quiet", action="store_true", help="suppress the report, just exit code")
    args = p.parse_args()
    missing = check(*args.reqs, quiet=args.quiet)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
