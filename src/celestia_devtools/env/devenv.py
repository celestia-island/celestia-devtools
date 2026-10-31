#!/usr/bin/env python3
"""Dev-mode gateway environment — DEBUG_* computation and command injection.

Local iteration must not depend on the production domain shape. The fleet
fronts its services at ``*.celestia.world`` hosts (``.cw`` is only the
fleet's short alias for the same names, terminated at a remote nginx), and
cloud-built clients bake those gateways in — the shittim-chest desktop
defaults to ``https://auth.celestia.ac.cn``. Reproducing domains locally
(hosts files, reverse proxies, certificates) buys exactly nothing for a
single-machine loop and drags UAC/sudo along for the ride.

The debug-mode contract instead is: **a ``DEBUG_``-prefixed environment
variable set that participating binaries read only in debug builds** (Rust
``cfg!(debug_assertions)``), replacing the default gateway addresses so a
dev build talks to the local stack. Release builds never read these
variables, so shipped binaries cannot be redirected. The tradeoff is
inherent and accepted: environment injection is per-process and local —
it cannot serve public-internet traffic the way the production vhosts do.

This facility owns two jobs:

  1. compute the variable set for a run profile (which local ports the
     stack listens on);
  2. launch a command with the set injected — `cargo tauri dev`, a mock
     server, an e2e runner — so every process in the loop agrees on the
     gateways without anyone exporting anything by hand.

Three built-in profiles mirror the three ways the stack runs locally
(native dev ports, malkuth-daemon pod ports, mock-stack ports). Alongside
the DEBUG_* contract vars, the existing per-repo variables
(``SHITTIM_CHEST_URL``, ``ENTELECHEIA_SCEPTER_URL``, ``ARONA_MEMORY_URL``,
…) are injected with the same values, so unmodified checkouts pick the
stack up today; the DEBUG_* names are the debug-gated way forward.

Usage::

    celestia-devtools devenv print                     # export lines (POSIX)
    celestia-devtools devenv print --profile mock      # front the mock stack
    celestia-devtools devenv print --format json       # machine-readable
    celestia-devtools devenv run -- cargo tauri dev    # inject + launch
    celestia-devtools devenv run --profile daemon -- chest serve

Custom ports live in a ``devenv.toml`` (cwd, then the celestia config
dir)::

    profile = "native"

    [profiles.mine]
    chest = 3000
    scepter = "127.0.0.1:8424"   # int → port on 127.0.0.1; or host:port

Injection precedence: variables already present in the environment are
NEVER overwritten — the caller's explicit value wins; use
``devenv print`` to inspect, or unset first.
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence
from celestia_devtools.env.host import detect_host_kind
import tomllib
from celestia_devtools.core import envelope

# ── Profiles ─────────────────────────────────────────────────────────────────

SERVICES = ("chest", "scepter", "arona", "evernight")

DEFAULT_PROFILE = "native"

# just dev webui --native / just server / cargo run -p _cli -- serve /
# cargo run --bin evernight-server -- serve
# malkuth daemon pod ports (env/daemon.py SERVICE_DEFS)
# mock-stack ports. arona's mock listens on ARONA_MOCK_PORT default
# 8429 (arona/scripts/mock/server.py), not the 8420 that
# core/mock.py MOCK_PORTS still records.
PROFILE_PORTS: dict[str, dict[str, object]] = {"native": {"chest": 3000, "scepter": 8424, "arona": 8420, "evernight": 8443}, "daemon": {"chest": 8400, "scepter": 8410, "arona": 8405, "evernight": 8443}, "mock": {"chest": 8428, "scepter": 8424, "arona": 8429, "evernight": 8443}}

# ── Config discovery ─────────────────────────────────────────────────────────

_CONFIG_NAME = "devenv.toml"

def config_file_path(explicit: str | None = None) -> Path | None:
    """First existing ``devenv.toml``: explicit → cwd → celestia config dir."""

    if explicit:
        p = Path(explicit)
        return p if p.exists() else None

    candidates = [Path.cwd() / _CONFIG_NAME]

    if detect_host_kind() == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".config"

    candidates.append(base / "celestia" / _CONFIG_NAME)

    for c in candidates:
        if c.exists():
            return c

    return None

def load_profile_ports(config: str | None = None, profile: str | None = None) -> tuple[str, dict[str, tuple[str, int]], Path | None]:
    """Resolve the active profile into ``{service: (host, port)}``.

    A ``devenv.toml`` may override ports per profile (int → 127.0.0.1 port,
    ``"host:port"`` for anything else) and add new profiles; overrides merge
    over the built-ins per service. Returns ``(profile, ports, config_path)``.
    """
    ports: dict[str, dict[str, tuple[str, int]]] = {name: {svc: ("127.0.0.1", p)for svc, p in table.items()}for name, table in PROFILE_PORTS.items()}
    active = profile or DEFAULT_PROFILE
    cfg_path = config_file_path(config)

    if cfg_path is not None:
        data = tomllib.loads(cfg_path.read_text(encoding = "utf-8"))
        active = profile or data.get("profile", active)

        for name, table in (data.get("profiles") or {}).items():
            merged = dict(ports.get(name, {}))

            for svc, value in table.items():
                if svc not in SERVICES:
                    raise ValueError(f"profile {name}: unknown service {svc!r}")

                merged[svc] = _normalize_target(value, f"profile {name}.{svc}")

            ports[name] = merged

    if active not in ports:
        known = ", ".join(sorted(ports))
        raise ValueError(f"unknown profile {active!r} (known: {known})")

    return active, ports[active], cfg_path

def _normalize_target(value: object, where: str) -> tuple[str, int]:
    if isinstance(value, bool):
        raise ValueError(f"{where}: boolean is not a port")

    if isinstance(value, int):
        if not 1 <= value <= 65535:
            raise ValueError(f"{where}: port {value} out of range")

        return "127.0.0.1", value

    if isinstance(value, str):
        host, sep, port_s = value.rpartition(":")

        if not sep or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
            raise ValueError(f"{where}: expected \"host:port\", got {value!r}")

        return host or "127.0.0.1", int(port_s)

    raise ValueError(f"{where}: unsupported value type {type(value).__name__}")

# ── Variable set ──────────────────────────────────────────────────────────────

def build_env(ports: dict[str, tuple[str, int]]) -> dict[str, str]:
    """The gateway variable set for one profile.

    Two layers with identical addresses: the ``DEBUG_*`` contract (read only
    by debug builds) and the existing per-repo variables (read unconditionally
    — they predate the contract and keep unmodified checkouts working).
    """

    def u(svc: str, scheme: str, path: str = "") -> str:
        host, port = ports[svc]
        return f"{scheme}://{host}:{port}{path}"

    # ── DEBUG_* contract — debug builds only, never read in release ──
    # ── existing per-repo variables, same addresses ──
    # shittim-chest desktop (packages/app/tauri/src/main.rs)
    # shittim-chest core outbound (packages/core/src/config.rs)
    # arona outbound (.env.example: memory gateway = scepter WS)
    return {"DEBUG_CHEST_URL": u("chest", "http"), "DEBUG_SCEPTER_URL": u("scepter", "http"), "DEBUG_SCEPTER_WS_URL": u("scepter", "ws"), "DEBUG_ARONA_URL": u("arona", "http"), "DEBUG_EVERNIGHT_URL": u("evernight", "ws"), "SHITTIM_CHEST_URL": u("chest", "http"), "ENTELECHEIA_SCEPTER_URL": u("scepter", "http"), "ENTELECHEIA_SCEPTER_WS_URL": u("scepter", "ws"), "ARONA_MEMORY_URL": u("scepter", "ws", "/ws"), "ARONA_EVERNIGHT_URL": u("evernight", "ws")}

def merge_env(base: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """Caller-set variables always win — injection fills gaps, never clobbers."""
    env = dict(os.environ)

    for key, value in {**base, **(extra or {})}.items():
        env.setdefault(key, value)

    return env

# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_print(args: argparse.Namespace) -> int:
    try:
        profile, ports, cfg = load_profile_ports(args.config, args.profile)
    except ValueError as exc:
        print(f"[devenv] {exc}", file = sys.stderr)
        return 2

    variables = build_env(ports)

    if args.format == "json":
        envelope.emit({"profile": profile, "vars": variables, "config": str(cfg)if cfg else None})
        return 0

    if args.format == "plain":
        for key in sorted(variables):
            print(f"{key}={variables[key]}")

        return 0

    # export lines for POSIX shells (`eval "$(celestia-devtools devenv print)"`)
    for key in sorted(variables):
        print(f"export {key}='{variables[key]}'")

    return 0

def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        print("[devenv] usage: devenv run [--profile P] -- <command…>", file = sys.stderr)
        return 2

    try:
        profile, ports, cfg = load_profile_ports(args.config, args.profile)
    except ValueError as exc:
        print(f"[devenv] {exc}", file = sys.stderr)
        return 2

    extra: dict[str, str] = {}

    for assignment in args.set or []:
        key, sep, value = assignment.partition("=")

        if not sep or not key:
            print(f"[devenv] bad --set {assignment!r} (expected VAR=VALUE)", file = sys.stderr)
            return 2

        extra[key] = value

    env = merge_env(build_env(ports), extra)
    # report only what WE contributed (absent from the caller's environment)
    contributed = sorted(k for k in {**build_env(ports), **extra} if os.environ.get(k) is None)
    print(f"[devenv] profile {profile}" + (f" (config: {cfg})" if cfg else "") + f" — injecting {len(contributed)} var(s): {' '.join(contributed)}")
    argv = list(args.command)
    # bare names like `python` need PATHEXT resolution on Windows —
    # CreateProcess does not search extensions the way a shell does.
    executable = shutil.which(argv[0]) or argv[0]

    try:
        result = subprocess.run([executable, *argv[1:]], env = env)
    except FileNotFoundError:
        print(f"[devenv] command not found: {argv[0]}", file = sys.stderr)
        return 127

    return result.returncode

# ── CLI dispatch ─────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog = "devenv", description = __doc__.splitlines()[0])
    sub = p.add_subparsers(dest = "cmd", required = True)

    pr = sub.add_parser("print", help = "print the gateway variable set")
    pr.add_argument("--profile", default = None, help = "native | daemon | mock | <custom>")
    pr.add_argument("--config", default = None, help = "devenv.toml path")
    pr.add_argument("--format", choices = ("export", "plain", "json"), default = "export")
    pr.set_defaults(fn = cmd_print)

    run = sub.add_parser("run", help = "launch a command with the set injected")
    run.add_argument("--profile", default = None)
    run.add_argument("--config", default = None)
    run.add_argument("--set", action = "append", metavar = "VAR=VALUE", help = "additional variable to inject (repeatable)")
    run.add_argument("command", nargs = argparse.REMAINDER, help = "the command to run (after --)")
    run.set_defaults(fn = cmd_run)
    return p

def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = getattr(args, "command", [])

    if command[:1] == ["--"]:  # `devenv run -- <cmd>`: drop the separator
        args.command = command[1:]

    return int(args.fn(args) or 0)

if __name__ == "__main__":
    raise SystemExit(main())
