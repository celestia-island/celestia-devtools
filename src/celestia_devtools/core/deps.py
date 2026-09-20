#!/usr/bin/env python3
"""Ensure Python dependencies with a bounded budget, never blocking deploy.

Policy (user-settled):

* a missing enhancement library must **degrade**, not abort — the deploy is
  the lifeline, the polish is optional;
* auto-install has a hard budget: at most ``tries`` attempts and ``timeout``
  seconds total, so a hostile network fails fast instead of looking like a
  hung deploy (mirrors/indexes are respected, never overridden silently);
* everything is reported one line at a time — an operator must see *why* the
  wizard fell back to plain-text prompts.

Usage::

    from celestia_devtools.core import deps, netproxy
    cfg = netproxy.detect()
    results = deps.ensure(deps.DEFAULT_REQUIRED, proxy=cfg)
    for r in results:
        print(r.line())
"""

from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from dataclasses import dataclass

# dist-name → import name (importlib.metadata works on distribution names).
DIST_IMPORT = {
    "PyYAML": "yaml",
    "tomli-w": "tomli_w",
    "questionary": "questionary",
}

DEFAULT_REQUIRED = ("PyYAML",)

_TRIES = 2
_TIMEOUT = 90  # seconds, total, across attempts


@dataclass
class EnsureResult:
    name: str
    ok: bool
    action: str  # present | installed | failed | skipped
    detail: str = ""

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        note = self.detail or self.action
        return "{} {} — {}".format(mark, self.name, note)


def _installed(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _pip_argv(dist: str, index: str | None, proxy_url: str | None) -> list[str]:
    argv = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
    if proxy_url:
        argv.append("--proxy={}".format(proxy_url))
    if index:
        argv.append("--index-url={}".format(index))
    argv.append(dist)
    return argv


def ensure(
    requirements: tuple[str, ...] | list[str] = DEFAULT_REQUIRED,
    *,
    install: bool = True,
    index: str | None = None,
    proxy_url: str | None = None,
    tries: int = _TRIES,
    timeout: int = _TIMEOUT,
    runner=subprocess.run,
) -> list[EnsureResult]:
    """Check each distribution; install within budget when allowed.

    ``runner`` is injectable for tests. Never raises — failures come back as
    ``EnsureResult(action="failed")`` with a hint in ``detail``.
    """
    results: list[EnsureResult] = []
    budget = timeout
    for dist in requirements:
        ver = _installed(dist)
        if ver:
            results.append(EnsureResult(dist, True, "present", ver))
            continue
        if not install:
            results.append(
                EnsureResult(dist, False, "skipped",
                             "missing; install disabled (report-only)"))
            continue
        attempt = 0
        while attempt < max(tries, 1) and budget > 0:
            attempt += 1
            # Fair share: the total budget divided across the remaining
            # attempts, so the cap is reachable instead of attempt #1 eating
            # everything (and a single hung call cannot outlive the budget).
            spend = max(1, budget // max(1, max(tries, 1) - attempt + 1))
            try:
                r = runner(_pip_argv(dist, index, proxy_url),
                           capture_output=True, text=True, timeout=spend)
            except (OSError, subprocess.SubprocessError) as exc:
                results.append(EnsureResult(dist, False, "failed",
                                            "pip error: {}".format(exc)))
                break
            budget -= spend
            if r.returncode == 0:
                results.append(EnsureResult(
                    dist, True, "installed",
                    importlib.metadata.version(dist) if _installed(dist) else "installed"))
                break
            if attempt >= max(tries, 1) or budget <= 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()
                last = tail[-1] if tail else "rc={}".format(r.returncode)
                results.append(EnsureResult(
                    dist, False, "failed",
                    "install failed ({} attempt{}): {}".format(
                        attempt, "s" if attempt > 1 else "", last)))
                break
        else:  # pragma: no cover - budget exhausted before first attempt
            results.append(EnsureResult(dist, False, "failed", "budget exhausted"))
    return results
