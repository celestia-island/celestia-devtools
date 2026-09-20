#!/usr/bin/env python3
"""Network proxy auto-detection for commands that must reach the internet.

Why this exists: a deployer box frequently has a working egress proxy that is
**not** exported as ``HTTP(S)_PROXY`` — e.g. a LAN gateway listening on
7890/7891, or a DNS name like ``proxy.<domain>``. A script that only reads
environment variables is blind on exactly those machines, and the failure mode
("downloads hang, looks like the deploy is stuck") is the expensive one.

Detection is a five-level short-circuit cascade:

1. environment variables (both cases + tool-specific aliases, then git's
   own ``http.proxy`` config);
2. well-known ports on the loopback interface;
3. the default gateway / resolver addresses (same port set);
4. DNS-suffix guesses (``proxy.<domain>``, ``sing-box.<domain>``);
5. WPAD/PAC (first ``PROXY host:port`` only — no JS interpretation).

Failure is loud: when nothing is found the report says "direct", it never
pretends a proxy exists.

Security rules baked in:

* credentials in a proxy URL (``http://user:pass@host:port``) are preserved for
  the actual child processes but **never** for logging — ``ProxyConfig.display``
  and ``redact()`` return ``host:port`` only;
* ``NO_PROXY`` is composed independently and always includes loopback, this
  host's addresses, RFC1918/ULA ranges, ``.local`` and the detected internal
  DNS suffixes — a database on the same LAN must never be routed through an
  egress proxy;
* ``sudo`` strips proxy variables by default, so callers should detect in the
  current environment and pass the result via ``child_env()`` explicitly
  instead of relying on inheritance.

Usage::

    from celestia_devtools.core import netproxy
    cfg = netproxy.detect()
    print(cfg.summary())            # one human line, credentials redacted
    env = netproxy.child_env(cfg)   # for subprocess/Popen
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import urllib.parse

# Level 1 — environment variables, both cases, plus tool-specific aliases.
# Ordered: https wins over http wins over all (matches pip/curl conventions).
_ENV_ORDER = (
    "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy",
)
_TOOL_ENV = ("PIP_PROXY", "UV_HTTP_PROXY", "npm_config_proxy")

# Levels 2–4 — ports tried against candidate hosts.
COMMON_PORTS = (7890, 7891, 8080, 8888, 3128)

# Level 4 — suffixes combined with "proxy." and "sing-box.".
_DEFAULT_SUFFIXES = ("local", "lan", "internal", "node.local")

# Names appended to NO_PROXY unconditionally (loopback + link-local + RFC1918
# + ULA). Kept as strings so callers can extend without ipaddress churn.
_DIRECT_NETS = (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fd00::/8", "fe80::/10",
)

_DISABLE_ENV = "CELESTIA_NO_PROXY_DETECT"


def redact(url: str) -> str:
    """Return ``host:port`` (userinfo dropped) for safe logging.

    Never raises: a malformed URL is degraded to its post-``@`` tail so that
    credentials can never survive into a log line even when parsing fails.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname is None:
            return _strip_userinfo(url)
        netloc = parsed.hostname
        if parsed.port is not None:
            netloc = "{}:{}".format(netloc, parsed.port)
        return netloc
    except (ValueError, UnicodeError):
        return _strip_userinfo(url)


def _strip_userinfo(url: str) -> str:
    return url.partition("@")[-1] if "@" in url else url


def _net_or_none(url: str | None) -> str | None:
    """Normalize a candidate URL; malformed input yields None (skipped level
    hit), never an exception — detect() must not crash on a bad env var."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname is None:
            return None
        _ = parsed.port  # range/shape errors mean the value is unusable
    except (ValueError, UnicodeError):
        return None
    return url.rstrip("/")


class ProxyConfig:
    """Immutable result of one detection pass."""

    __slots__ = ("proxy_url", "display", "source", "no_proxy")

    def __init__(self, proxy_url: str | None, source: str,
                 no_proxy: tuple[str, ...] = ()) -> None:
        self.proxy_url = proxy_url
        self.display = redact(proxy_url) if proxy_url else ""
        self.source = source  # "flag:none" | "disabled" | "direct" | "env:NAME" | ...
        self.no_proxy = tuple(no_proxy)

    @property
    def direct(self) -> bool:
        return self.proxy_url is None

    def summary(self) -> str:
        if self.direct:
            return "代理：未检测到（直连） [{}]".format(self.source)
        return "代理：{} [{}]".format(self.display, self.source)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "ProxyConfig(display={!r}, source={!r})".format(self.display, self.source)


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _from_env(env: dict[str, str]) -> tuple[str, str] | None:
    for name in _ENV_ORDER:
        url = _net_or_none(env.get(name))
        if url:
            return url, "env:{}".format(name)
    for name in _TOOL_ENV:
        url = _net_or_none(env.get(name))
        if url:
            return url, "env:{}".format(name)
    return None


def _git_config_proxy() -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        r = subprocess.run(
            [git, "config", "--global", "--get", "http.proxy"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return _net_or_none(r.stdout.strip())
    return None


def _gateway_hosts() -> tuple[str, ...]:
    """Resolver + default-gateway addresses (level 3 candidates)."""
    hosts: list[str] = []
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    ip = parts[1]
                    try:
                        ipaddress.ip_address(ip)
                        hosts.append(ip)
                    except ValueError:
                        continue
    except OSError:
        pass
    ip = shutil.which("ip")
    if ip:
        try:
            r = subprocess.run(
                [ip, "route", "show", "default"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if "via" in parts:
                        cand = parts[parts.index("via") + 1]
                        try:
                            ipaddress.ip_address(cand)
                            hosts.append(cand)
                        except (ValueError, IndexError):
                            continue
        except (OSError, subprocess.SubprocessError):
            pass
    # de-dup, keep order
    seen: set[str] = set()
    return tuple(h for h in hosts if not (h in seen or seen.add(h)))


def _dns_suffixes() -> tuple[str, ...]:
    suffixes: list[str] = list(_DEFAULT_SUFFIXES)
    fqdn = socket.getfqdn()
    if "." in fqdn:
        dom = fqdn.split(".", 1)[1]
        if dom and dom not in suffixes:
            suffixes.append(dom)
    return tuple(suffixes)


def _from_wpad(suffixes: tuple[str, ...], timeout: float) -> tuple[str, str] | None:
    """Fetch wpad.dat for each suffix; accept only a literal ``PROXY h:p``."""
    import re
    import urllib.request

    pat = re.compile(r"PROXY\s+([A-Za-z0-9._-]+:\d+)")
    for suffix in suffixes:
        url = "http://wpad.{}/wpad.dat".format(suffix)
        try:
            with urllib.request.urlopen(url, timeout=max(timeout, 2.0)) as resp:
                body = resp.read(65536).decode("utf-8", "replace")
        except OSError:
            continue
        m = pat.search(body)
        if m:
            return _net_or_none(m.group(1)), "wpad:{}".format(suffix)
    return None


def detect(
    *,
    explicit: str | None = None,
    no_proxy_flag: bool = False,
    disabled: bool | None = None,
    ports: tuple[int, ...] = COMMON_PORTS,
    listener_hosts: tuple[str, ...] = ("127.0.0.1",),
    gateway_hosts: tuple[str, ...] | None = None,
    dns_suffixes: tuple[str, ...] | None = None,
    timeout: float = 0.3,
    env: dict[str, str] | None = None,
) -> ProxyConfig:
    """Run the five-level cascade and compose the NO_PROXY list.

    ``explicit`` ("http://…" or "none") and ``no_proxy_flag`` are the
    command-line overrides; ``CELESTIA_NO_PROXY_DETECT=1`` disables detection
    entirely (audit / offline scenarios).
    """
    e = dict(os.environ if env is None else env)
    suffixes = _dns_suffixes() if dns_suffixes is None else dns_suffixes
    no_proxy = _compose_no_proxy(e, suffixes)

    if no_proxy_flag or (explicit and explicit.lower() in ("none", "direct")):
        return ProxyConfig(None, "flag:none", no_proxy)
    forced = _net_or_none(explicit)
    if forced:
        return ProxyConfig(forced, "flag:explicit", no_proxy)
    if disabled is None:
        disabled = e.get(_DISABLE_ENV, "") == "1"
    if disabled:
        return ProxyConfig(None, "disabled", no_proxy)

    hit = _from_env(e)
    if hit:
        return ProxyConfig(hit[0], hit[1], no_proxy)

    # A git http.proxy the user configured themselves outranks any guessing
    # (listeners / gateway / DNS / WPAD), so it belongs here at level 1.
    git_url = _git_config_proxy()
    if git_url:
        return ProxyConfig(git_url, "git:http.proxy", no_proxy)

    for host in listener_hosts:  # level 2
        for port in ports:
            if _tcp_reachable(host, port, timeout):
                url = "http://{}:{}".format(host, port)
                return ProxyConfig(url, "listen:{}:{}".format(host, port), no_proxy)

    gw = _gateway_hosts() if gateway_hosts is None else gateway_hosts
    for host in gw:  # level 3
        for port in ports:
            if _tcp_reachable(host, port, timeout):
                url = "http://{}:{}".format(host, port)
                return ProxyConfig(url, "gateway:{}:{}".format(host, port), no_proxy)

    for suffix in suffixes:  # level 4
        for stem in ("proxy", "sing-box"):
            host = "{}.{}".format(stem, suffix)
            for port in ports:
                if _tcp_reachable(host, port, timeout):
                    url = "http://{}:{}".format(host, port)
                    return ProxyConfig(url, "dns:{}".format(host), no_proxy)

    hit = _from_wpad(suffixes, timeout)  # level 5
    if hit:
        return ProxyConfig(hit[0], hit[1], no_proxy)

    return ProxyConfig(None, "direct", no_proxy)


def _compose_no_proxy(env: dict[str, str], suffixes: tuple[str, ...]) -> tuple[str, ...]:
    """Base direct list + caller's existing NO_PROXY, de-duplicated, in order."""
    entries: list[str] = ["localhost", "127.0.0.1", "::1"]
    entries.extend(_DIRECT_NETS)
    for suffix in suffixes:
        entries.append("." + suffix)
    for name in ("NO_PROXY", "no_proxy"):
        for piece in env.get(name, "").split(","):
            piece = piece.strip()
            if piece and piece not in entries:
                entries.append(piece)
    seen: set[str] = set()
    return tuple(x for x in entries if not (x in seen or seen.add(x)))


def child_env(cfg: ProxyConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes: proxy vars set/cleared + NO_PROXY.

    Never relies on inheritance — build the full dict so a ``sudo``-stripped
    environment still carries the detection result downstream.
    """
    out = dict(os.environ if base is None else base)
    clear = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
             "ALL_PROXY", "all_proxy") + _TOOL_ENV
    for name in clear:
        out.pop(name, None)
    if not cfg.direct:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            out[name] = cfg.proxy_url or ""
    joined = ",".join(cfg.no_proxy)
    out["NO_PROXY"] = joined
    out["no_proxy"] = joined
    return out
