#!/usr/bin/env python3
"""Local domain hostmap — temporary production-style lanes onto the local stack.

The production fleet fronts its services with short ``*.cw`` vhosts
(``demo.dev.cw``, ``gateway.cw``, …) terminated at a remote nginx. Nothing
reproduces that shape locally: the dev stack is reached as bare
``localhost:<port>`` pairs, so origin-dependent client code (redirects,
OAuth callbacks, CSP checks, cookie scoping) can only be exercised against
the real servers.

This facility closes the gap on one machine, with zero extra dependencies:

  1. a *managed block* in the OS hosts file maps the lane domains to
     127.0.0.1 — added and removed by ``up`` / ``down``, never touching
     foreign lines, elevating itself once when the file needs admin;
  2. a small asyncio reverse proxy listens on :80 and routes each request
     by its ``Host`` header (``CONNECT`` and WebSocket upgrades included —
     after the routing decision it is a byte-for-byte pipe) to the local
     upstream of the active profile.

Three built-in profiles mirror the three ways the stack runs locally:

  native   chest :3000 / scepter :8424 / arona :8420 / evernight :8443
           (``just dev webui --native``, ``just server``, …)
  daemon   the malkuth-daemon pod ports (chest :8400 / scepter :8410 /
           arona :8405) from ``celestia-devtools daemon generate-config``
  mock     the mock-stack ports (arona :8420 / scepter :8424 / chest :8428)

Usage::

    celestia-devtools hostmap up                  # hosts + proxy, Ctrl-C stops
    celestia-devtools hostmap up --profile mock   # front the mock stack
    celestia-devtools hostmap up --port 8080      # unprivileged port
    celestia-devtools hostmap up --no-hosts       # proxy only (no admin)
    celestia-devtools hostmap down                # remove the managed block
    celestia-devtools hostmap proxy               # proxy only, CI-friendly
    celestia-devtools hostmap status              # lanes + hosts + ports
    celestia-devtools hostmap lanes [--json]      # resolved mapping table

Custom lanes live in a ``hostmap.toml`` (cwd, then the celestia config
dir)::

    profile = "native"

    [lanes.native]
    "dev.cw" = 3000            # int → 127.0.0.1:<port>
    "auth.cw" = "127.0.0.1:3000"   # or explicit host:port
    "*.lab.cw" = 8415          # wildcard lane: subdomains match the suffix

HTTP only in this revision: the local stacks are plain-HTTP, and TLS
termination (cert generation + SNI routing) is deliberately left out.

Inside WSL the *Windows* hosts file is the one that matters for browsers;
it is auto-detected (``/mnt/c/Windows/System32/drivers/etc/hosts``) and the
proxy should be started with ``--bind 0.0.0.0`` so WSL's localhost
forwarding can reach it from the Windows side.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# ── Built-in lane profiles ───────────────────────────────────────────────────

DEFAULT_PROFILE = "native"

DEFAULT_LANES: dict[str, dict[str, object]] = {
    # just dev webui --native / just server / cargo run -p _cli -- serve /
    # cargo run --bin evernight-server -- serve
    "native": {
        "dev.cw": 3000,
        "auth.cw": 3000,
        "scepter.cw": 8424,
        "arona.cw": 8420,
        "evernight.cw": 8443,
    },
    # malkuth daemon pod ports (env/daemon.py SERVICE_DEFS)
    "daemon": {
        "dev.cw": 8400,
        "auth.cw": 8400,
        "scepter.cw": 8410,
        "arona.cw": 8405,
        "evernight.cw": 8443,
    },
    # mock-stack ports (core/mock.py MOCK_PORTS). NOTE: arona's mock script
    # listens on ARONA_MOCK_PORT default 8429 (arona/scripts/mock/server.py),
    # not the 8420 that MOCK_PORTS still records.
    "mock": {
        "dev.cw": 8428,
        "auth.cw": 8428,
        "scepter.cw": 8424,
        "arona.cw": 8429,
        "evernight.cw": 8443,
    },
}

DEFAULT_PROXY_BIND = "127.0.0.1"
DEFAULT_PROXY_PORT = 80

# ── Managed hosts block ──────────────────────────────────────────────────────

HOSTS_BEGIN = "# BEGIN celestia-devtools hostmap — managed, do not edit"
HOSTS_END = "# END celestia-devtools hostmap"

_LANE_DOC = {
    "dev.cw": "panel (shittim-chest)",
    "auth.cw": "auth surface — served by chest, no separate service",
    "scepter.cw": "entelecheia scepter",
    "arona.cw": "arona gateway",
    "evernight.cw": "evernight broker",
}


def hosts_file_path(explicit: Optional[str] = None) -> Path:
    """Resolve the hosts file to edit — *explicit* wins, then the platform
    default. Inside WSL the Windows file is preferred when reachable, since
    that is the one a Windows browser resolves through."""
    if explicit:
        return Path(explicit)
    from celestia_devtools.env.host import detect_host_kind
    if detect_host_kind() == "wsl":
        win_hosts = Path("/mnt/c/Windows/System32/drivers/etc/hosts")
        if win_hosts.exists():
            return win_hosts
    if sys.platform == "win32":
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        return root / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def config_file_path(explicit: Optional[str] = None) -> Optional[Path]:
    """First existing ``hostmap.toml``: explicit → cwd → celestia config dir."""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    candidates = [Path.cwd() / "hostmap.toml"]
    from celestia_devtools.env.host import detect_host_kind
    if detect_host_kind() == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".config"
    candidates.append(base / "celestia" / "hostmap.toml")
    for c in candidates:
        if c.exists():
            return c
    return None


# ── Lane resolution ──────────────────────────────────────────────────────────

_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)


def _normalize_lane_value(value: object, domain: str) -> tuple[str, int]:
    """Accept ``8420`` (→ 127.0.0.1:8420) or ``"host:port"``; validate."""
    if isinstance(value, bool):
        raise ValueError(f"lane {domain}: boolean is not a port")
    if isinstance(value, int):
        if not 1 <= value <= 65535:
            raise ValueError(f"lane {domain}: port {value} out of range")
        return "127.0.0.1", value
    if isinstance(value, str):
        host, sep, port_s = value.rpartition(":")
        if not sep or not port_s.isdigit() or not 1 <= int(port_s) <= 65535:
            raise ValueError(f"lane {domain}: expected \"host:port\", got {value!r}")
        return host or "127.0.0.1", int(port_s)
    raise ValueError(f"lane {domain}: unsupported value type {type(value).__name__}")


def load_lanes(
    config: Optional[str] = None, profile: Optional[str] = None
) -> tuple[str, dict[str, tuple[str, int]], Optional[Path]]:
    """Resolve the active profile into ``{domain: (upstream_host, upstream_port)}``.

    File lanes deep-merge over the built-ins per profile (a profile named in
    the file keeps the built-in entries it does not override), and the file
    may add entirely new profiles. Returns ``(profile, lanes, config_path)``.
    """
    lanes: dict[str, dict[str, tuple[str, int]]] = {
        name: {d: _normalize_lane_value(v, d) for d, v in table.items()}
        for name, table in DEFAULT_LANES.items()
    }
    active = profile or DEFAULT_PROFILE
    cfg_path = config_file_path(config)
    if cfg_path is not None:
        import tomllib
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        active = profile or data.get("profile", active)
        for name, table in (data.get("lanes") or {}).items():
            merged = dict(lanes.get(name, {}))
            for domain, value in table.items():
                if not _DOMAIN_RE.match(domain):
                    raise ValueError(f"lane domain {domain!r} is not a domain name")
                merged[domain] = _normalize_lane_value(value, domain)
            lanes[name] = merged
    if active not in lanes:
        known = ", ".join(sorted(lanes))
        raise ValueError(f"unknown profile {active!r} (known: {known})")
    return active, lanes[active], cfg_path


def resolve(lanes: dict[str, tuple[str, int]], host: str) -> Optional[tuple[str, int]]:
    """Map one request ``Host`` to its upstream: exact lane first, then the
    closest ``*.suffix`` wildcard (``demo.dev.cw`` matching ``*.dev.cw``)."""
    if host in lanes:
        return lanes[host]
    best: Optional[tuple[str, tuple[str, int]]] = None
    for domain, upstream in lanes.items():
        if domain.startswith("*.") and host.endswith(domain[1:]):
            suffix = domain[1:]
            if best is None or len(suffix) > len(best[0]):
                best = (suffix, upstream)
    return best[1] if best else None


def _bare_domain(domain: str) -> str:
    """``*.dev.cw`` → ``dev.cw`` (hosts files have no wildcard syntax)."""
    return domain[2:] if domain.startswith("*.") else domain


def hosts_domains(lanes: dict[str, tuple[str, int]]) -> list[str]:
    """Domains to write into the hosts file — wildcards collapse to their
    bare suffix."""
    return sorted({_bare_domain(d) for d in lanes})


# ── Hosts block edit (pure file surgery — testable, elevation-free) ─────────

def render_block(domains: Sequence[str]) -> str:
    return f"{HOSTS_BEGIN}\n127.0.0.1 {' '.join(domains)}\n{HOSTS_END}\n"


def sync_hosts_block(hosts: Path, domains: Sequence[str]) -> bool:
    """Replace the managed block with *domains*; keep every foreign line.
    Returns True when the file content changed."""
    current = hosts.read_text(encoding="utf-8", errors="replace") if hosts.exists() else ""
    block = render_block(domains)
    updated = _splice_block(current, block)
    if updated == current:
        return False
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text(updated, encoding="utf-8")
    return True


def remove_hosts_block(hosts: Path) -> bool:
    current = hosts.read_text(encoding="utf-8", errors="replace") if hosts.exists() else ""
    updated = _splice_block(current, None)
    if updated == current:
        return False
    hosts.write_text(updated, encoding="utf-8")
    return True


def _splice_block(text: str, block: Optional[str]) -> str:
    """Cut any existing managed block out of *text*; append *block* if given."""
    begin_idx = text.find(HOSTS_BEGIN)
    if begin_idx != -1:
        end_idx = text.find(HOSTS_END, begin_idx)
        if end_idx != -1:
            after = text[end_idx + len(HOSTS_END):]
            text = text[:begin_idx] + after.lstrip("\r\n")
        else:
            # dangling BEGIN without END — drop to end of file
            text = text[:begin_idx]
    text = text.rstrip("\n") + "\n" if text.strip() else ""
    if block is None:
        return text
    if not text.endswith("\n"):
        text += "\n"
    return text + block


def read_managed_domains(hosts: Path) -> list[str]:
    """Domains currently recorded inside the managed block (empty if none)."""
    if not hosts.exists():
        return []
    text = hosts.read_text(encoding="utf-8", errors="replace")
    begin_idx = text.find(HOSTS_BEGIN)
    if begin_idx == -1:
        return []
    end_idx = text.find(HOSTS_END, begin_idx)
    body = text[begin_idx + len(HOSTS_BEGIN): end_idx if end_idx != -1 else len(text)]
    domains: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split()
        if parts and parts[0] == "127.0.0.1":
            domains.extend(parts[1:])
    return domains


# ── Elevation ────────────────────────────────────────────────────────────────

# Antivirus/UAC approvals can land after the first spawn already failed —
# stay in-process and retry instead of exiting (the Huorong lesson).
ELEVATE_RETRY_ATTEMPTS = 3
ELEVATE_RETRY_DELAY = 2.0


def is_elevated() -> bool:
    if sys.platform == "win32":
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0  # type: ignore[attr-defined]


def relaunch_elevated(args: Sequence[str]) -> int:
    """Re-run ``celestia-devtools hostmap <args…>`` elevated, waiting for it.

    UAC prompt on Windows (via PowerShell ``Start-Process -Verb RunAs``),
    ``sudo`` everywhere else. The subcommand re-run this way must be
    side-effect-complete on its own (``hosts-sync`` / ``hosts-remove``).
    The elevated copy's exit code propagates back through ``-PassThru``;
    its console output cannot cross the UAC boundary, so failures surface
    via the return code plus whatever PowerShell itself reports.

    Security software (Huorong, Defender SmartScreen, …) may hold the
    elevation request pending an out-of-band user decision, then report
    failure while the user is still approving it. One failed attempt must
    therefore NOT exit the tool: on Windows we retry in-process
    (ELEVATE_RETRY_ATTEMPTS times, ELEVATE_RETRY_DELAY apart) so an
    approval that lands late still succeeds. Exit code 1223
    (ERROR_CANCELLED) marks a declined prompt vs a failed elevated copy.
    """
    argv = ["-m", "celestia_devtools", "hostmap", *args]
    if sys.platform == "win32":
        result = None
        for attempt in range(1, ELEVATE_RETRY_ATTEMPTS + 1):
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 _elevation_script(sys.executable, argv)],
                capture_output=True,
            )
            if result.returncode == 0:
                return 0
            if attempt < ELEVATE_RETRY_ATTEMPTS:
                print(
                    f"[hostmap] elevation attempt {attempt}/{ELEVATE_RETRY_ATTEMPTS} "
                    "failed — if an antivirus prompt or UAC dialog is showing, "
                    f"approve it; retrying in {ELEVATE_RETRY_DELAY:.0f}s"
                )
                time.sleep(ELEVATE_RETRY_DELAY)
        # PowerShell 5.1 prints its own errors in the legacy console
        # codepage (GBK on zh-CN hosts) — capture bytes and decode lossily,
        # never let a decode error kill the subprocess reader thread.
        detail = ((result.stderr if result else None) or (result.stdout if result else None) or b"")
        detail_text = detail.decode("utf-8", errors="replace").strip()
        if detail_text:
            print(f"[hostmap] powershell: {detail_text}", file=sys.stderr)
        return (result.returncode if result else 1)
    # Plain `sudo` scrubs PYTHONPATH, so a checkout-based invocation would
    # lose sight of the package in the re-spawn; `sudo env VAR=… cmd` sets
    # it back without needing the sudoers SETENV privilege. Sudo prompts on
    # the inherited tty — run from an interactive terminal when a password
    # is required, or configure NOPASSWD for non-tty contexts.
    pkg_root = str(Path(__file__).resolve().parents[2])  # …/celestia-devtools/src
    pythonpath = pkg_root + os.pathsep + (os.environ.get("PYTHONPATH") or "")
    result = subprocess.run(
        ["sudo", "env", f"PYTHONPATH={pythonpath}", sys.executable, *argv]
    )
    return result.returncode


def _elevation_script(interpreter: str, argv: Sequence[str]) -> str:
    """The PowerShell body for one elevation attempt.

    The ArgumentList array literal MUST be ``@('a','b')`` — parentheses
    included. ``@'a', 'b'`` (bare @-quote) parses as a here-string opener
    and dies with UnexpectedCharactersAfterHereStringHeader before any UAC
    dialog can appear; the regression test pins this.
    """
    quoted = ", ".join("'" + a.replace("'", "''") + "'" for a in argv)
    return (
        "try { $p = Start-Process -Verb RunAs -Wait -PassThru "
        f"-FilePath '{interpreter}' -ArgumentList @({quoted}); "
        "exit $p.ExitCode } catch { exit 1223 }"
    )


def _sync_hosts_elevated_if_needed(
    domains: Sequence[str], hosts: Path, remove: bool,
    respawn_args: Sequence[str] = (),
) -> int:
    """Apply the block change in-process when the file is writable, else
    re-spawn the side-effect subcommand elevated once. *respawn_args* carries
    the lane-selection flags (``--profile``/``--config``) so the elevated
    copy resolves the same domains as the caller."""
    action = "hosts-remove" if remove else "hosts-sync"
    args = [action, *respawn_args, "--hosts-file", str(hosts)]
    try:
        if remove:
            changed = remove_hosts_block(hosts)
        else:
            changed = sync_hosts_block(hosts, domains)
    except PermissionError:
        if is_elevated():
            # Root/admin and still denied — a real ACL or filesystem issue
            # (e.g. DrvFs mapping the Windows hosts read-only to WSL root).
            print(f"[hostmap] cannot write {hosts} even elevated — check ACLs", file=sys.stderr)
            if str(hosts).startswith("/mnt/"):
                print(
                    "[hostmap] inside WSL, run hostmap from the Windows side, or pass "
                    "--hosts-file /etc/hosts for distro-local lanes", file=sys.stderr,
                )
            return 1
        print(f"[hostmap] needs elevation to edit {hosts}")
        rc = relaunch_elevated(args)
        if rc != 0:
            print(f"[hostmap] elevated {action} failed (rc={rc})", file=sys.stderr)
            if sys.platform == "win32":
                print("[hostmap] the UAC prompt must be approved", file=sys.stderr)
            return rc
        return 0
    if remove:
        print(f"[hostmap] {hosts}: {'block removed' if changed else 'nothing to remove'}")
    else:
        print(f"[hostmap] {hosts}: block {'updated' if changed else 'already current'}")
    return 0


# ── Host-routing reverse proxy (asyncio, stdlib only) ───────────────────────

_READ_CHUNK = 65536
_HEAD_LIMIT = 1 << 20  # 1 MiB of request head before giving up


def _host_from_head(head: bytes) -> tuple[str, str]:
    """Extract ``(method, host)`` from a raw request head — the Host header
    for origin-form requests, the absolute URI for proxy-form, the tunnel
    target for CONNECT."""
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1", "replace").split()
    method = parts[0].upper() if parts else ""
    if method == "CONNECT" and len(parts) > 1:
        return method, parts[1].rsplit(":", 1)[0].lower()
    host = ""
    if len(parts) > 1 and parts[1].startswith(("http://", "https://")):
        rest = parts[1].split("://", 1)[1]
        host = rest.split("/", 1)[0].rsplit(":", 1)[0]
    for line in lines[1:]:
        if line[:5].lower() == b"host:":
            value = line[5:].strip().decode("latin-1", "replace")
            maybe_port = value.rsplit(":", 1)
            host = maybe_port[0] if len(maybe_port) == 2 and maybe_port[1].isdigit() else value
            break
    return method, host.lower()


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(_READ_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.close()
        except (ConnectionError, OSError):
            pass


def _bad_gateway(writer: asyncio.StreamWriter, message: str, lanes: dict) -> None:
    known = "\n".join(
        f"  http://{d:<16} → {h}:{p}" for d, (h, p) in sorted(lanes.items())
    ) or "  (no lanes configured)"
    body = f"celestia-devtools hostmap: {message}\n\nKnown lanes:\n{known}\n".encode()
    head = (
        b"HTTP/1.1 502 Bad Gateway\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Connection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
    )
    try:
        writer.write(head + body)
    except (ConnectionError, OSError):
        pass


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    lanes: dict[str, tuple[str, int]],
) -> None:
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError,
                asyncio.LimitOverrunError):
            return
        method, host = _host_from_head(head)
        if not host:
            _bad_gateway(writer, "no Host header in request", lanes)
            return
        upstream = resolve(lanes, host)
        if upstream is None:
            _bad_gateway(writer, f"no lane for host {host!r}", lanes)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream[0], upstream[1]), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            _bad_gateway(writer, f"upstream {upstream[0]}:{upstream[1]} refused ({exc})", lanes)
            return
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(head)  # verbatim — upstream sees the real Host
            await up_writer.drain()
        await asyncio.gather(_pump(reader, up_writer), _pump(up_reader, writer))
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


async def run_proxy(
    bind: str, port: int, lanes: dict[str, tuple[str, int]]
) -> None:
    """Listen on *bind:port* and route by Host until cancelled/KeyboardInterrupt."""
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, lanes), bind, port, limit=_HEAD_LIMIT
    )
    sockets = server.sockets or []
    bound = ", ".join(f"{s.getsockname()[0]}:{s.getsockname()[1]}" for s in sockets) or bind
    print(f"[hostmap] proxy listening on {bound}")
    for domain, (host, port_) in sorted(lanes.items()):
        note = f" → {host}:{port_}"
        print(f"[hostmap]   http://{domain}{note}{'  (' + _LANE_DOC[domain] + ')' if domain in _LANE_DOC else ''}")
    async with server:
        await server.serve_forever()


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _selection_args(args: argparse.Namespace) -> list[str]:
    """Lane-selection flags to replay in the elevated copy (``--profile`` /
    ``--config``) — losing them would write the default profile's domains."""
    out: list[str] = []
    if getattr(args, "profile", None):
        out += ["--profile", args.profile]
    if getattr(args, "config", None):
        out += ["--config", args.config]
    return out


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_up(args: argparse.Namespace) -> int:
    try:
        profile, lanes, cfg = load_lanes(args.config, args.profile)
    except ValueError as exc:
        print(f"[hostmap] {exc}", file=sys.stderr)
        return 2
    domains = hosts_domains(lanes)
    hosts = hosts_file_path(args.hosts_file)
    if not args.no_hosts:
        rc = _sync_hosts_elevated_if_needed(
            domains, hosts, remove=False, respawn_args=_selection_args(args)
        )
        if rc != 0:
            return rc
        print(f"[hostmap] lanes: {', '.join(domains)} → 127.0.0.1 (profile {profile})")
    else:
        print("[hostmap] --no-hosts: leaving hosts file untouched")
    if cfg is not None:
        print(f"[hostmap] config: {cfg}")
    try:
        asyncio.run(run_proxy(args.bind, args.port, lanes))
    except KeyboardInterrupt:
        print("\n[hostmap] proxy stopped")
        if args.ephemeral:
            _sync_hosts_elevated_if_needed(domains, hosts, remove=True)
        else:
            print("[hostmap] hosts entries kept — `celestia-devtools hostmap down` removes them")
    except OSError as exc:
        print(f"[hostmap] cannot bind {args.bind}:{args.port} ({exc})", file=sys.stderr)
        if args.port < 1024 and sys.platform != "win32":
            print("[hostmap] ports <1024 need root on this OS — try --port 8080", file=sys.stderr)
        return 1
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    hosts = hosts_file_path(args.hosts_file)
    if not hosts.exists():
        print(f"[hostmap] {hosts}: nothing to remove")
        return 0
    return _sync_hosts_elevated_if_needed([], hosts, remove=True)


def cmd_proxy(args: argparse.Namespace) -> int:
    """Proxy only — never touches the hosts file (CI, or self-managed hosts)."""
    try:
        profile, lanes, cfg = load_lanes(args.config, args.profile)
    except ValueError as exc:
        print(f"[hostmap] {exc}", file=sys.stderr)
        return 2
    if cfg is not None:
        print(f"[hostmap] config: {cfg}")
    try:
        asyncio.run(run_proxy(args.bind, args.port, lanes))
    except KeyboardInterrupt:
        print("\n[hostmap] proxy stopped")
    except OSError as exc:
        print(f"[hostmap] cannot bind {args.bind}:{args.port} ({exc})", file=sys.stderr)
        if args.port < 1024 and sys.platform != "win32":
            print("[hostmap] ports <1024 need root on this OS — try --port 8080", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        profile, lanes, cfg = load_lanes(args.config, args.profile)
    except ValueError as exc:
        print(f"[hostmap] {exc}", file=sys.stderr)
        return 2
    hosts = hosts_file_path(args.hosts_file)
    managed = set(read_managed_domains(hosts))
    proxy = _port_listening(args.port, args.bind)
    print(f"[hostmap] profile: {profile}" + (f"  (config: {cfg})" if cfg else "  (built-in lanes)"))
    print(f"[hostmap] proxy {args.bind}:{args.port}: {'listening' if proxy else 'down'}")
    print(f"[hostmap] hosts file: {hosts} — {len(managed)} managed domain(s)")
    for domain in sorted(lanes):
        host, port = lanes[domain]
        in_hosts = _bare_domain(domain) in managed
        upstream = "up" if _port_listening(port, host) else "down"
        marks = f"hosts={'y' if in_hosts else '-'} upstream={upstream}"
        print(f"[hostmap]   {domain:<18} → {host}:{port:<5} {marks}")
    return 0


def cmd_lanes(args: argparse.Namespace) -> int:
    try:
        profile, lanes, cfg = load_lanes(args.config, args.profile)
    except ValueError as exc:
        print(f"[hostmap] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(
            {"profile": profile,
             "lanes": {d: f"{h}:{p}" for d, (h, p) in sorted(lanes.items())},
             "config": str(cfg) if cfg else None},
            indent=2))
        return 0
    print(f"profile: {profile}" + (f"  (config: {cfg})" if cfg else "  (built-in lanes)"))
    for domain in sorted(lanes):
        host, port = lanes[domain]
        print(f"  {domain:<18} → {host}:{port}")
    return 0


def cmd_hosts_sync(args: argparse.Namespace) -> int:
    """Side-effect-complete hosts writer — the unit re-spawned elevated."""
    try:
        _, lanes, _ = load_lanes(args.config, args.profile)
    except ValueError as exc:
        print(f"[hostmap] {exc}", file=sys.stderr)
        return 2
    return _sync_hosts_elevated_if_needed(
        hosts_domains(lanes), hosts_file_path(args.hosts_file),
        remove=False, respawn_args=_selection_args(args),
    )


def cmd_hosts_remove(args: argparse.Namespace) -> int:
    """Side-effect-complete hosts cleaner — the unit re-spawned elevated."""
    return _sync_hosts_elevated_if_needed([], hosts_file_path(args.hosts_file), True)


# ── CLI dispatch ─────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hostmap", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="hosts block + foreground Host-routing proxy")
    up.add_argument("--profile", choices=None, help="native | daemon | mock | <custom>")
    up.add_argument("--config", default=None, help="hostmap.toml path")
    up.add_argument("--bind", default=DEFAULT_PROXY_BIND)
    up.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    up.add_argument("--hosts-file", default=None, help=argparse.SUPPRESS)
    up.add_argument("--no-hosts", action="store_true", help="proxy only, never touch hosts")
    up.add_argument("--ephemeral", action="store_true",
                    help="remove the hosts block again on Ctrl-C")
    up.set_defaults(fn=cmd_up)

    down = sub.add_parser("down", help="remove the managed hosts block")
    down.add_argument("--hosts-file", default=None, help=argparse.SUPPRESS)
    down.set_defaults(fn=cmd_down)

    proxy = sub.add_parser("proxy", help="run the proxy only (no hosts changes)")
    proxy.add_argument("--profile", default=None)
    proxy.add_argument("--config", default=None)
    proxy.add_argument("--bind", default=DEFAULT_PROXY_BIND)
    proxy.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    proxy.set_defaults(fn=cmd_proxy)

    status = sub.add_parser("status", help="lanes + hosts entries + port states")
    status.add_argument("--profile", default=None)
    status.add_argument("--config", default=None)
    status.add_argument("--bind", default=DEFAULT_PROXY_BIND)
    status.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    status.add_argument("--hosts-file", default=None, help=argparse.SUPPRESS)
    status.set_defaults(fn=cmd_status)

    lanes = sub.add_parser("lanes", help="print the resolved lane table")
    lanes.add_argument("--profile", default=None)
    lanes.add_argument("--config", default=None)
    lanes.add_argument("--json", action="store_true")
    lanes.set_defaults(fn=cmd_lanes)

    for name, fn, help_ in (
        ("hosts-sync", cmd_hosts_sync, "internal: write the managed hosts block"),
        ("hosts-remove", cmd_hosts_remove, "internal: remove the managed hosts block"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--profile", default=None)
        sp.add_argument("--config", default=None)
        sp.add_argument("--hosts-file", default=None, help=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
