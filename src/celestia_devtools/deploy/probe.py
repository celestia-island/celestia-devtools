#!/usr/bin/env python3
"""System-tool probes for production deploy targets.

Mirrors ``env/preflight.py`` (development face) with the **production** face:
``doctor`` reports what a single-host install needs — systemd, a SQL client,
a front proxy, TLS tooling, privileged helpers. Report-only by design;
``--install-missing`` (a later slice) may act, and only with explicit consent.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Callable, List, Sequence

from celestia_devtools.env.preflight import Probe, _run_version

PY_FLOOR = (3, 11)


def check_python_floor() -> Probe:
    ok = sys.version_info >= PY_FLOOR
    return Probe(
        "python", ok, platform.python_version(),
        detail="" if ok else "celestia-devtools needs >= 3.11",
        hint="re-run via pyshim (it installs a current Python), or upgrade the system Python",
    )


def check_systemd() -> Probe:
    ok, ver = _run_version(["systemctl", "--version"])
    return Probe("systemd", ok, ver,
                 hint="production installs are systemd units; a container target may run without it")


def check_psql() -> Probe:
    ok, ver = _run_version(["psql", "--version"])
    return Probe("psql", ok, ver, hint="postgresql-client (apt/dnf)")


def check_pg_isready() -> Probe:
    ok, ver = _run_version(["pg_isready", "--version"])
    return Probe("pg_isready", ok, ver, hint="postgresql-client (apt/dnf)")


def check_nginx() -> Probe:
    # nginx -v writes to stderr; _run_version takes the first line of either.
    ok, ver = _run_version(["nginx", "-v"])
    return Probe("nginx", ok, ver, hint="nginx (apt/dnf) — the front/TLS stage skips gracefully when missing")


def check_openssl() -> Probe:
    ok, ver = _run_version(["openssl", "version"])
    return Probe("openssl", ok, ver, hint="openssl (apt/dnf)")


def check_curl() -> Probe:
    ok, ver = _run_version(["curl", "--version"])
    return Probe("curl", ok, ver, hint="curl (apt/dnf)")


def check_tar() -> Probe:
    ok, ver = _run_version(["tar", "--version"])
    return Probe("tar", ok, ver, hint="tar (apt/dnf)")


def check_sudo() -> Probe:
    ok, ver = _run_version(["sudo", "--version"])
    return Probe("sudo", ok, ver, hint="sudo — privileged stages run through a narrow sudoers helper")


def check_useradd() -> Probe:
    if os.name != "posix":
        return Probe("useradd", False, "", hint="posix-only stage")
    ok, ver = _run_version(["useradd", "--version"])
    return Probe("useradd", ok, ver, hint="passwd package (apt/dnf)")


def check_install_cmd() -> Probe:
    ok, _ = _run_version(["install", "--version"])
    return Probe("install", ok, "coreutils", hint="coreutils")


def check_acme_sh() -> Probe:
    """acme.sh is a user-level script, not usually on PATH — check both."""
    candidates = [shutil.which("acme.sh"),
                  os.path.expanduser("~/.acme.sh/acme.sh")]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                r = subprocess.run([path, "--version"], capture_output=True,
                                   text=True, timeout=10)
                out = (r.stdout or r.stderr).strip().splitlines()
                return Probe("acme.sh", True, out[0] if out else path)
            except (OSError, subprocess.SubprocessError):
                continue
    return Probe("acme.sh", False, "",
                 hint="curl https://get.acme.sh | sh -s email=<you> — HTTP-01 only; front stage skips when missing")


def check_git() -> Probe:
    ok, ver = _run_version(["git", "--version"])
    return Probe("git", ok, ver, hint="git — artifact fallback channel")


# Registry: req id → checker (same convention as env.preflight).
CHECKERS: dict[str, Callable[[], Probe]] = {
    "python": check_python_floor,
    "systemd": check_systemd,
    "psql": check_psql,
    "pg_isready": check_pg_isready,
    "nginx": check_nginx,
    "openssl": check_openssl,
    "curl": check_curl,
    "tar": check_tar,
    "sudo": check_sudo,
    "useradd": check_useradd,
    "install": check_install_cmd,
    "acme.sh": check_acme_sh,
    "git": check_git,
}

# Probes whose absence must not fail `doctor` (stages degrade, documented).
SOFT = {"acme.sh", "nginx", "psql", "pg_isready", "sudo"}


def check(reqs: Sequence[str]) -> List[Probe]:
    out: List[Probe] = []
    for req in reqs:
        fn = CHECKERS.get(req)
        out.append(fn() if fn else Probe(req, False, "", hint="unknown probe id"))
    return out


def all_probes() -> List[Probe]:
    return check(list(CHECKERS))


def report() -> List[Probe]:
    """Print the one-line-per-tool table; return the unmet hard probes."""
    probes = all_probes()
    unmet: List[Probe] = []
    for p in probes:
        if p.ok:
            print("  {:<12} ✓ {}".format(p.name, p.version))
        else:
            print("  {:<12} ✗ {}".format(p.name, p.hint or p.detail))
            if p.name not in SOFT:
                unmet.append(p)
    return unmet
