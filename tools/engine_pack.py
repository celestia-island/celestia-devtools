#!/usr/bin/env python3
"""Declarative installer/runner for celestia island local inference "engine packs".

An engine pack is a directory that describes how to install and run one local
LLM/ASR inference engine as a systemd service on a GPU node::

    <pack-dir>/
      engine.meta        TOML manifest (schema: tools/README.md)
      asr_server.py      engine entrypoint (whatever the engine needs)
      ...                remaining engine files (code, weights refs, ...)

Subcommands:

    plan     <pack-dir>              parse meta and print planned actions (offline)
    install  <pack-dir> [options]    conda env + systemd unit + health check
    verify   <pack-dir> [--timeout]  poll the pack health endpoint

Guarantees:

* Python stdlib only (``tomllib`` needs 3.11+; older interpreters get a clean
  actionable error from every CLI command instead of a traceback).
* Conda isolation via miniforge3/mamba with explicit prefixes; ``conda
  activate`` is never used -- everything runs through ``<prefix>/bin/...``.
* Persistent footprint after install is exactly: the systemd unit, the conda
  env prefix and the pack directory.  This tool may be deleted afterwards
  without breaking the running service.
* Idempotent: re-running ``install`` reuses a matching env, re-renders the
  unit, restarts the service and re-checks health.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:
    import tomllib
except ImportError:  # Python < 3.11: keep the module importable, fail in main()
    tomllib = None  # type: ignore[assignment]

DEFAULT_MINIFORGE_ROOT = Path("/mnt/work/miniforge3")
DEFAULT_ENV_ROOT = Path("/mnt/work/engine-envs")
DEFAULT_UNIT_DIR = Path("/etc/systemd/system")
# Domestic mirror, reachable directly from the island nodes (no proxy needed).
DEFAULT_MIRROR_BASE = (
    "https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease"
)
RECORD_FILENAME = ".engine-pack-installed.json"
DEFAULT_HEALTH_TIMEOUT = 120.0
POLL_INTERVAL = 1.0
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

# Loopback health checks must never touch a proxy, and the miniforge mirror is
# a direct-reachable domestic host: use a no-proxy opener for both.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EnginePackError(Exception):
    """Base class for actionable engine-pack errors."""


class MetaError(EnginePackError):
    """engine.meta is missing, unparsable or invalid."""


class CommandError(EnginePackError):
    """A subprocess exited non-zero (or could not be started)."""


class HealthCheckError(EnginePackError):
    """The service did not answer HTTP 200 within the timeout."""


# ── Pack manifest ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvSpec:
    """``[env]`` section: conda-isolated python environment."""

    python: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class ServiceSpec:
    """``[service]`` section: what to run and how to health-check it."""

    unit: str
    command: tuple[str, ...]
    port: int
    health_path: str


@dataclass(frozen=True)
class SystemdSpec:
    """``[systemd]`` section (all fields have defaults)."""

    user: str
    after: str
    description: str


@dataclass(frozen=True)
class PackSpec:
    """Validated view of one engine.meta manifest."""

    name: str
    engine: str
    version: str
    env: EnvSpec | None
    service: ServiceSpec
    systemd: SystemdSpec

    @property
    def unit_name(self) -> str:
        return f"{self.service.unit}.service"

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.service.port}{self.service.health_path}"

    def env_prefix(self, env_root: Path) -> Path:
        return env_root / self.name

    def resolved_command(self, env_prefix: Path | None) -> tuple[str, ...]:
        """ExecStart argv with the ``python`` token bound to the env python."""
        argv = list(self.service.command)
        if env_prefix is not None and argv and argv[0] == "python":
            argv[0] = str(env_prefix / "bin" / "python")
        # An absolute first token passes through verbatim (validated at parse time).
        return tuple(argv)


def load_meta(pack_dir: Path) -> dict:
    """Read and parse ``<pack-dir>/engine.meta`` into a plain dict."""
    if tomllib is None:
        raise EnginePackError(
            "engine-pack requires Python 3.11+ (stdlib tomllib); "
            f"running under {sys.version.split()[0]}. Re-run with a newer python3."
        )
    if not pack_dir.is_dir():
        raise MetaError(f"pack directory not found: {pack_dir}")
    meta_path = pack_dir / "engine.meta"
    if not meta_path.is_file():
        raise MetaError(
            f"engine.meta not found: {meta_path}\n"
            "  every pack directory needs an engine.meta TOML manifest (see tools/README.md)"
        )
    try:
        with meta_path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise MetaError(f"invalid TOML in {meta_path}:\n  {exc}") from exc


def _require_str(table: dict, key: str, where: str, errors: list[str]) -> str | None:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.{key}: missing or not a non-empty string")
        return None
    return value


def _optional_str(table: dict, key: str, where: str, errors: list[str]) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.{key}: must be a non-empty string when present")
        return None
    return value


def _section(meta: dict, key: str, *, required: bool, errors: list[str]) -> dict:
    value = meta.get(key)
    if value is None:
        if required:
            errors.append(f"missing section [{key}]")
        return {}
    if not isinstance(value, dict):
        errors.append(f"[{key}] must be a TOML table")
        return {}
    return value


def parse_spec(meta: dict, *, source: Path | str = "engine.meta") -> PackSpec:
    """Validate a parsed manifest; raise :class:`MetaError` listing all problems."""
    errors: list[str] = []

    pack_table = _section(meta, "pack", required=True, errors=errors)
    name = _require_str(pack_table, "name", "pack", errors)
    engine = _require_str(pack_table, "engine", "pack", errors)
    version = _require_str(pack_table, "version", "pack", errors)

    service_table = _section(meta, "service", required=True, errors=errors)
    unit = _require_str(service_table, "unit", "service", errors)
    health_path = _require_str(service_table, "health_path", "service", errors)
    if health_path is not None and not health_path.startswith("/"):
        errors.append("service.health_path: must start with '/'")
        health_path = None
    port = service_table.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errors.append("service.port: missing or not an integer in 1..65535")
        port = None
    command = service_table.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(token, str) and token for token in command)
    ):
        errors.append("service.command: missing or not a non-empty list of strings")
        command = None

    has_env = "env" in meta
    env: EnvSpec | None = None
    if has_env:
        env_table = _section(meta, "env", required=True, errors=errors)
        python_version = _require_str(env_table, "python", "env", errors)
        packages = env_table.get("packages")
        if not isinstance(packages, list) or not all(
            isinstance(pkg, str) and pkg for pkg in packages
        ):
            errors.append("env.packages: missing or not a list of non-empty strings")
            packages = None
        if python_version is not None and packages is not None:
            env = EnvSpec(python=python_version, packages=tuple(packages))

    # First ExecStart token: "python" (bound to the env python) or an absolute path.
    if command is not None:
        head = command[0]
        if has_env:
            if head != "python" and not os.path.isabs(head):
                errors.append(
                    f"service.command[0] ({head!r}): must be \"python\" "
                    "(resolved to the env python) or an absolute path"
                )
        elif not os.path.isabs(head):
            errors.append(
                f"service.command[0] ({head!r}): native-binary pack (no [env]) "
                "requires an absolute path"
            )

    systemd_table = _section(meta, "systemd", required=False, errors=errors)
    user = _optional_str(systemd_table, "user", "systemd", errors) or "root"
    after = _optional_str(systemd_table, "after", "systemd", errors) or "network-online.target"
    default_description = f"engine-pack {name or '?'} ({engine or '?'})"
    description = (
        _optional_str(systemd_table, "description", "systemd", errors) or default_description
    )

    if errors:
        raise MetaError(
            f"invalid engine.meta ({source}):\n  - " + "\n  - ".join(errors)
        )

    assert name and engine and version and unit and port and health_path and command
    return PackSpec(
        name=name,
        engine=engine,
        version=version,
        env=env,
        service=ServiceSpec(
            unit=unit, command=tuple(command), port=port, health_path=health_path
        ),
        systemd=SystemdSpec(user=user, after=after, description=description),
    )


# ── Unit rendering (pure; no filesystem, no subprocess, no tomllib) ──────────


def render_unit(spec: PackSpec, *, pack_dir: Path, env_prefix: Path | None) -> str:
    """Render the systemd unit for ``spec`` as text."""
    argv = shlex.join(spec.resolved_command(env_prefix))
    lines = [
        "# Generated by tools/engine_pack.py -- do not edit by hand;",
        "# edit engine.meta and re-run `engine_pack.py install <pack-dir>` instead.",
        "",
        "[Unit]",
        f"Description={spec.systemd.description}",
        f"After={spec.systemd.after}",
        "",
        "[Service]",
        "Type=simple",
        f"User={spec.systemd.user}",
        f"WorkingDirectory={pack_dir}",
        f"ExecStart={argv}",
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return "\n".join(lines) + "\n"


# ── Subprocess / download / conda helpers ────────────────────────────────────


def run_cmd(cmd: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``cmd``, echo it, and raise :class:`CommandError` on non-zero exit."""
    printable = shlex.join(cmd)
    print(f"  $ {printable}", flush=True)
    try:
        proc = subprocess.run(
            list(cmd), cwd=str(cwd) if cwd is not None else None, text=True, capture_output=True
        )
    except FileNotFoundError as exc:
        raise CommandError(f"command not found: {cmd[0]} (while running: {printable})") from exc
    if proc.returncode != 0:
        tail = [line for line in (proc.stderr or proc.stdout or "").strip().splitlines()][-5:]
        detail = "\n    ".join(tail)
        raise CommandError(
            f"command failed (exit {proc.returncode}): {printable}"
            + (f"\n    {detail}" if detail else "")
        )
    return proc


def miniforge_installer_name() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "arm64"}.get(
        machine
    )
    if arch is None:
        raise EnginePackError(f"unsupported architecture for miniforge bootstrap: {machine!r}")
    return f"Miniforge3-Linux-{arch}.sh"


def download_file(url: str, dest: Path) -> Path:
    """Stream ``url`` to ``dest`` (single bounded attempt, direct connection)."""
    print(f"  downloading {url}", flush=True)
    try:
        with _DIRECT_OPENER.open(url, timeout=60) as resp, dest.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise EnginePackError(
            f"download failed: {url}\n  {exc}\n  check connectivity or pass --mirror-base"
        ) from exc
    dest.chmod(0o755)
    return dest


def ensure_miniforge(root: Path, mirror_base: str) -> bool:
    """Ensure ``<root>/bin/mamba`` exists, bootstrapping miniforge3 if needed."""
    mamba = root / "bin" / "mamba"
    if mamba.is_file():
        return False
    installer_url = f"{mirror_base.rstrip('/')}/{miniforge_installer_name()}"
    with tempfile.TemporaryDirectory(prefix="engine-pack-miniforge-") as tmp:
        installer = download_file(installer_url, Path(tmp) / "miniforge.sh")
        run_cmd(["bash", str(installer), "-b", "-p", str(root)])
    if not mamba.is_file():
        raise EnginePackError(f"miniforge bootstrap finished but {mamba} is missing")
    return True


def env_exists(prefix: Path) -> bool:
    return (prefix / "bin" / "python").is_file()


def ensure_env(mamba: Path, prefix: Path, python_version: str, *, force: bool) -> bool:
    """Create the env at ``prefix`` if missing; return True when created here."""
    if env_exists(prefix):
        if not force:
            return False
        run_cmd([str(mamba), "remove", "-y", "-p", str(prefix), "--all"])
    run_cmd([str(mamba), "create", "-y", "-p", str(prefix), f"python={python_version}"])
    if not env_exists(prefix):
        raise EnginePackError(f"mamba create finished but {prefix / 'bin' / 'python'} is missing")
    return True


def record_path(prefix: Path) -> Path:
    return prefix / RECORD_FILENAME


def read_installed_packages(prefix: Path) -> list[str] | None:
    """Return the sorted recorded package set, or None when missing/invalid."""
    path = record_path(prefix)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        packages = data["packages"]
        if isinstance(packages, list) and all(isinstance(pkg, str) for pkg in packages):
            return sorted(packages)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def write_installed_record(prefix: Path, pack_name: str, env: EnvSpec) -> None:
    record = {"pack": pack_name, "python": env.python, "packages": sorted(env.packages)}
    record_path(prefix).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def ensure_pip_packages(prefix: Path, env: EnvSpec, pack_name: str) -> bool:
    """Install pinned pip packages unless the recorded set matches exactly."""
    wanted = sorted(env.packages)
    if read_installed_packages(prefix) == wanted:
        return False
    if wanted:
        run_cmd([str(prefix / "bin" / "pip"), "install", "--no-input", *env.packages])
    write_installed_record(prefix, pack_name, env)
    return True


def write_unit(spec: PackSpec, unit_dir: Path, text: str) -> Path:
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / spec.unit_name
    unit_path.write_text(text, encoding="utf-8")
    return unit_path


def activate_service(unit_name: str) -> None:
    """daemon-reload, then enable, then restart.

    ``enable`` is idempotent and ``restart`` both starts a stopped unit and
    restarts a live one, so re-runs always pick up the re-rendered unit.
    """
    run_cmd(["systemctl", "daemon-reload"])
    run_cmd(["systemctl", "enable", unit_name])
    run_cmd(["systemctl", "restart", unit_name])


def wait_for_health(url: str, timeout: float, poll_interval: float = POLL_INTERVAL) -> float:
    """Poll ``url`` until HTTP 200; return elapsed seconds or raise."""
    started = time.monotonic()
    deadline = started + timeout
    last = "no attempt made"
    while True:
        try:
            with _DIRECT_OPENER.open(url, timeout=5.0) as resp:
                if resp.status == 200:
                    return time.monotonic() - started
                last = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            last = str(reason)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HealthCheckError(
                f"{url} did not answer HTTP 200 within {timeout:.0f}s (last: {last})"
            )
        time.sleep(min(poll_interval, remaining))


# ── CLI commands ──────────────────────────────────────────────────────────────


def plan_actions(spec: PackSpec, args: argparse.Namespace) -> list[str]:
    if spec.env is None:
        head = [
            "skip conda entirely (native-binary pack; ExecStart used verbatim)",
        ]
    else:
        prefix = spec.env_prefix(args.env_root)
        head = [
            f"ensure miniforge3 at {args.miniforge_root} "
            f"(bootstrap from {args.mirror_base} if {args.miniforge_root / 'bin' / 'mamba'} missing)",
            f"mamba create -p {prefix} python={spec.env.python} (skipped when already present)",
            f"pip install {len(spec.env.packages)} pinned package(s) into the env "
            "(skipped when the recorded set matches engine.meta)",
        ]
    return head + [
        f"render {args.unit_dir / spec.unit_name}, then systemctl daemon-reload + enable + restart",
        f"poll {spec.health_url} until HTTP 200 (timeout {args.timeout:.0f}s)",
    ]


def cmd_plan(args: argparse.Namespace) -> int:
    """Offline: parse and print the planned actions; touch nothing."""
    spec = parse_spec(load_meta(args.pack_dir), source=args.pack_dir / "engine.meta")
    env_note = (
        f"{spec.env_prefix(args.env_root)} (python {spec.env.python})"
        if spec.env is not None
        else "none (native-binary pack)"
    )
    print(f"pack       : {spec.name} ({spec.engine} {spec.version})")
    print(f"pack dir   : {args.pack_dir}")
    print(f"conda env  : {env_note}")
    print(f"unit       : {args.unit_dir / spec.unit_name}")
    print(f"port       : {spec.service.port}")
    print(f"health URL : {spec.health_url}")
    print("planned actions:")
    for item in plan_actions(spec, args):
        print(f"  - {item}")
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    """Full install flow: conda env -> unit -> service -> health check."""
    spec = parse_spec(load_meta(args.pack_dir), source=args.pack_dir / "engine.meta")
    env_prefix = spec.env_prefix(args.env_root) if spec.env is not None else None
    mamba = args.miniforge_root / "bin" / "mamba"
    outcomes: list[tuple[str, str]] = []

    def step(status: str, name: str, detail: str) -> None:
        outcomes.append((status, name))
        print(f"[{status}] {name:<7} {detail}", flush=True)

    def do_conda() -> None:
        if spec.env is None:
            step("SKIP", "conda", "native-binary pack (no [env] section)")
            return
        bootstrapped = ensure_miniforge(args.miniforge_root, args.mirror_base)
        note = "bootstrapped from mirror" if bootstrapped else "already present"
        step("PASS", "conda", f"miniforge3 at {args.miniforge_root} ({note})")

    def do_env() -> None:
        if spec.env is None or env_prefix is None:
            step("SKIP", "env", "native-binary pack (no [env] section)")
            return
        created = ensure_env(mamba, env_prefix, spec.env.python, force=args.force_env)
        if created:
            step("PASS", "env", f"created {env_prefix} (python {spec.env.python})")
        else:
            step("SKIP", "env", f"env already present at {env_prefix}")

    def do_pip() -> None:
        if spec.env is None or env_prefix is None:
            step("SKIP", "pip", "native-binary pack (no [env] section)")
            return
        installed = ensure_pip_packages(env_prefix, spec.env, spec.name)
        record = record_path(env_prefix)
        if installed:
            step("PASS", "pip", f"installed {len(spec.env.packages)} package(s); record {record}")
        else:
            step("SKIP", "pip", f"recorded package set matches engine.meta ({record})")

    def do_unit() -> None:
        text = render_unit(
            spec,
            pack_dir=args.pack_dir.resolve(),
            env_prefix=env_prefix.resolve() if env_prefix is not None else None,
        )
        unit_path = write_unit(spec, args.unit_dir, text)
        step("PASS", "unit", f"rendered {unit_path}")

    def do_service() -> None:
        activate_service(spec.unit_name)
        step("PASS", "service", "systemctl daemon-reload + enable + restart")

    def do_health() -> None:
        print(f"  waiting for {spec.health_url} (timeout {args.timeout:.0f}s)", flush=True)
        elapsed = wait_for_health(spec.health_url, args.timeout)
        step("PASS", "health", f"{spec.health_url} -> 200 in {elapsed:.1f}s")

    actions: list[tuple[str, Callable[[], None]]] = [
        ("conda", do_conda),
        ("env", do_env),
        ("pip", do_pip),
        ("unit", do_unit),
        ("service", do_service),
        ("health", do_health),
    ]
    for index, (name, action) in enumerate(actions):
        try:
            action()
        except EnginePackError as exc:
            step("FAIL", name, str(exc))
            for pending, _ in actions[index + 1 :]:
                step("SKIP", pending, "not run (previous step failed)")
            break

    failed = any(status == "FAIL" for status, _ in outcomes)
    print(f"engine pack '{spec.name}': {'FAILED' if failed else 'OK'}")
    return EXIT_FAILURE if failed else EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Health check only: poll the pack endpoint until 200 or timeout."""
    spec = parse_spec(load_meta(args.pack_dir), source=args.pack_dir / "engine.meta")
    print(f"verifying {spec.health_url} (timeout {args.timeout:.0f}s)", flush=True)
    try:
        elapsed = wait_for_health(spec.health_url, args.timeout)
    except HealthCheckError as exc:
        print(f"[FAIL] health {exc}")
        return EXIT_FAILURE
    print(f"[PASS] health {spec.health_url} -> 200 in {elapsed:.1f}s")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engine_pack.py",
        description="Install/verify declarative local inference engine packs (stdlib only).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{plan,install,verify}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("pack_dir", type=Path, help="pack directory containing engine.meta")

    def add_roots(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--miniforge-root", type=Path, default=DEFAULT_MINIFORGE_ROOT,
            help="miniforge3 install root (default: %(default)s)",
        )
        p.add_argument(
            "--env-root", type=Path, default=DEFAULT_ENV_ROOT,
            help="root for conda env prefixes (default: %(default)s)",
        )
        p.add_argument(
            "--unit-dir", type=Path, default=DEFAULT_UNIT_DIR,
            help="systemd unit directory (default: %(default)s)",
        )
        p.add_argument(
            "--mirror-base", default=DEFAULT_MIRROR_BASE,
            help="miniforge download mirror base, must be direct-reachable (default: %(default)s)",
        )

    p_plan = sub.add_parser(
        "plan", parents=[common], help="parse engine.meta and print planned actions (offline)"
    )
    add_roots(p_plan)
    p_plan.add_argument(
        "--timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT, help=argparse.SUPPRESS
    )
    p_plan.set_defaults(func=cmd_plan)

    p_install = sub.add_parser(
        "install", parents=[common], help="conda env + systemd unit + service + health check"
    )
    add_roots(p_install)
    p_install.add_argument(
        "--force-env", action="store_true", help="recreate the conda env even if it exists"
    )
    p_install.add_argument(
        "--timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT,
        help="health check timeout in seconds (default: %(default)s)",
    )
    p_install.set_defaults(func=cmd_install)

    p_verify = sub.add_parser(
        "verify", parents=[common], help="poll the pack health endpoint until HTTP 200"
    )
    p_verify.add_argument(
        "--timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT,
        help="health check timeout in seconds (default: %(default)s)",
    )
    p_verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if tomllib is None:
        print(
            "engine-pack requires Python 3.11+ (stdlib tomllib); "
            f"running under {sys.version.split()[0]}. Re-run with a newer python3.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MetaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except EnginePackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
