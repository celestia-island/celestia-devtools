#!/usr/bin/env python3
"""Register a release in the gateway's artifact catalog without a browser session.

The gateway's artifact catalog (``gateway.artifacts.*``) is supply-chain
authority: one row decides where the whole fleet downloads a desktop build
from, so writing it needs an ``ARTIFACTS_MANAGE`` credential. Until now the only
path was a human in a browser; this command is the same operation, so a release
pipeline can perform it as an explicit, auditable step.

Verification is the point, not an extra: the command does not call the row
written until it has proved the catalog serves it.

  1. connect to ``<base>/api/rpc`` (a JSON-RPC WebSocket) with the token in the
     **Authorization: Bearer** header, never in the URL — a 500+ character
     token in the request line is refused by the front nginx (measured);
  2. ``gateway.artifacts.upsert`` with the row;
  3. ``gateway.artifacts.list`` and assert ``tag/direct_url/sha256/size`` read
     back exactly as written;
  4. fetch the public URL **anonymously** and require HTTP 200 — the row is
     useless if the fleet cannot download from it.

The token comes from ``GATEWAY_ADMIN_TOKEN`` and is never printed; the log
carries ``<N chars, sha256:8hex>`` instead.

Usage::

    celestia-devtools gateway-catalog register --artifact chest-desktop \\
        --channel stable --direct-url https://.../asset.exe \\
        --checksums https://.../checksums.json --asset asset.exe [--dry-run]
    celestia-devtools gateway-catalog register --artifact chest-desktop \\
        --channel stable --tag 0.1.0-c2c42787e384 --direct-url https://... \\
        --sha256 <64 hex> --size <bytes>
    celestia-devtools gateway-catalog list [--json]

Exit codes: 0 ok, 1 the server refused or the public URL is not reachable,
2 usage or missing token, 3 the post-write readback disagreed.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence
from celestia_devtools.core import envelope

DEFAULT_BASE = "https://gateway.celestia.world"
RPC_PATH = "/api/rpc"
TOKEN_ENV = "GATEWAY_ADMIN_TOKEN"
CHECKS_FETCH_ATTEMPTS = 3
READBACK_KEYS = ("tag", "direct_url", "sha256", "size")
ROW_KEYS = ("artifact", "channel", "tag", "direct_url", "sha256", "size")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_MISMATCH = 3

class usage_error(Exception):
    """A bad invocation: exit 2 without touching the network."""

class gateway_error(Exception):
    """The gateway refused the call or the catalog cannot be reached: exit 1."""

class readback_error(Exception):
    """The gateways answered but the catalog disagrees with what we wrote: exit 3."""

def mask(token: str) -> str:
    """A shape that identifies the credential without disclosing it."""

    if not token:
        return "<empty>"

    digest = hashlib.sha256(token.encode()).hexdigest()[:8]
    return f"<{len(token)} chars, sha256:{digest}>"

def public_url(base: str, artifact: str, channel: str) -> str:
    """The URL the catalog row must be reachable at (what the fleet downloads)."""
    return f"{base.rstrip('/')}/downloads/{urllib.parse.quote(artifact, safe = '')}/{urllib.parse.quote(channel, safe = '')}"

def build_row(artifact: str, channel: str, tag: str, direct_url: str, checksums: dict | None = None, asset: str | None = None, sha256: str | None = None, size: int | None = None) -> dict:
    """Assemble the catalog row, taking sha256/size from the manifest when given.

    ``tag`` is left empty when it must be derived: a caller that passed a
    manifest without ``--tag`` gets ``<version>-<commit12>`` (see
    ``derive_tag``), which is the stamp style the live rows use.
    """
    row: dict[str, Any] = {"artifact": artifact, "channel": channel}

    if tag:
        row["tag"] = tag

    row["direct_url"] = direct_url

    if checksums is not None:
        if not asset:
            raise usage_error("--asset is required with --checksums (the manifest names many assets)")

        entry = (checksums.get("artifacts") or {}).get(asset)

        if not isinstance(entry, dict):
            known = ", ".join(sorted((checksums.get("artifacts") or {}).keys())) or "none"
            raise usage_error(f"asset {asset!r} is not in the checksums manifest (known: {known})")

        row["sha256"] = entry.get("sha256")
        row["size"] = entry.get("size")
    else:
        row["sha256"] = sha256
        row["size"] = size

    return row

def derive_tag(version: str, source_commit: str, explicit: str | None = None) -> str:
    """The row's stamp: an explicit tag wins, else ``<version>-<commit12>``."""

    if explicit:
        return explicit

    if not version:
        raise usage_error("cannot derive a tag without a version (pass --tag)")

    if not source_commit:
        raise usage_error("cannot derive a tag without source_commit in the checksums manifest (pass --tag)")

    return f"{version}-{source_commit[:12]}"

def validate_row(row: dict) -> None:
    """Refuse a row the gateway would accept but the fleet could never use."""

    for key in ("artifact", "channel"):
        value = row.get(key)

        if not isinstance(value, str) or not value:
            raise usage_error(f"--{key} is required")

    if not row.get("tag"):
        raise usage_error("--tag is required (or --checksums with a source_commit to derive it)")

    direct_url = row.get("direct_url")

    if not isinstance(direct_url, str) or not direct_url:
        raise usage_error("--direct-url is required")

    parts = urllib.parse.urlsplit(direct_url)

    if (parts.scheme not in ("http", "https")) or not parts.netloc:
        raise usage_error(f"--direct-url must be an absolute http(s) URL, got {direct_url!r}")

    digest = row.get("sha256")

    if not isinstance(digest, str):
        raise usage_error("sha256 is missing (pass --sha256 or --checksums + --asset)")

    if (len(digest) != 64) or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        raise usage_error(f"sha256 must be 64 hex characters, got {len(digest)} character(s)")

    size = row.get("size")

    if not isinstance(size, int) or isinstance(size, bool) or (size <= 0):
        raise usage_error(f"size must be a positive integer number of bytes, got {size!r}")

def readback_mismatch(row: dict, rows: list) -> str | None:
    """Compare the written row with the catalog's copy; None means they agree."""
    match = [r for r in rows if (r.get("artifact") == row["artifact"]) and (r.get("channel") == row["channel"])]

    if not match:
        return f"the write was accepted but list does not show {row['artifact']}/{row['channel']} ({len(rows)} row(s) total)"

    got = match[0]

    for key in READBACK_KEYS:
        if got.get(key) != row[key]:
            return f"{key} read back as {got.get(key)!r}, expected {row[key]!r}"

    return None

def _urlopen(url: str, timeout: int) -> Any:
    """Open a URL anonymously: no credentials are ever attached to these reads."""
    return urllib.request.urlopen(url, timeout = timeout)  # noqa: S310 — the URL comes from the operator's own command line

def fetch_checksums(location: str, attempts: int = CHECKS_FETCH_ATTEMPTS, timeout: int = 60) -> dict:
    """Read a checksums manifest from a URL or a local path, retrying empty reads.

    The release CDN sometimes answers an empty body from this host (measured:
    two empty bodies, then a good one), so a read is retried before it is
    allowed to fail, and it fails with a message rather than a traceback.
    """
    is_local = not location.startswith(("http://", "https://"))
    last_error = "empty body"

    for attempt in range(1, attempts + 1):
        try:
            if is_local:
                text = Path(location).read_text(encoding = "utf-8")
            else:
                with _urlopen(location, timeout) as answer:
                    text = answer.read().decode("utf-8")

            if text.lstrip().startswith("{"):
                parsed = json.loads(text)

                if isinstance(parsed, dict):
                    return parsed

                last_error = "not a JSON object"
        except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as error:
            last_error = f"{type(error).__name__}: {error}"

        if attempt < attempts:
            print(f"checksums: attempt {attempt} returned nothing usable ({last_error}), retrying", file = sys.stderr)

    raise gateway_error(f"could not read {location} after {attempts} attempts ({last_error})")

class json_rpc_socket:
    """The few bytes of RFC 6455 this needs: text frames out, text frames back."""

    def __init__(self, base: str, token: str, timeout: int = 30) -> None:
        parsed = urllib.parse.urlsplit(base)
        secure = parsed.scheme == "https"
        host = parsed.hostname or ""

        if not host:
            raise gateway_error(f"--base has no host: {base!r}")

        port = parsed.port or (443 if secure else 80)
        path = f"{parsed.path.rstrip('/')}{RPC_PATH}"
        self._sock = socket.create_connection((host, port), timeout = timeout)

        if secure:
            self._sock = ssl.create_default_context().wrap_socket(self._sock, server_hostname = host)

        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if parsed.port is None else f"{host}:{parsed.port}"
        # The token travels in the Authorization header, never in the URL: a
        # 500+ character access token in the request line is refused by the
        # front nginx (measured: 400/404 where the header form succeeds).
        request = f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n"
        self._sock.sendall(request.encode())
        head = self._sock.recv(4096).decode("utf-8", "replace")
        status = head.split("\r\n", 1)[0]

        if " 101 " not in status:
            # The status line is echoed; it never carries the credential.
            self._sock.close()
            raise gateway_error(f"websocket upgrade refused: {status}")

    def send(self, text: str) -> None:
        """Send one masked text frame (a client MUST mask)."""
        payload = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)

        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)

        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read_exactly(self, count: int) -> bytes:
        """Read exactly ``count`` bytes, or raise: a short read is a broken frame."""
        chunks = b""

        while len(chunks) < count:
            chunk = self._sock.recv(count - len(chunks))

            if not chunk:
                raise gateway_error("connection closed before a reply arrived")

            chunks += chunk

        return chunks

    def recv(self) -> dict:
        """Read one text frame and parse it as a JSON-RPC reply."""
        first = self._read_exactly(2)
        length = first[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exactly(8))[0]

        body = self._read_exactly(length)
        return json.loads(body.decode("utf-8", "replace"))

    def call(self, rpc_id: int, method: str, params: dict) -> dict:
        """One JSON-RPC request/response pair."""
        self.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}))
        reply = self.recv()

        if "error" in reply:
            raise gateway_error(f"{method} refused: {reply['error']}")

        result = reply.get("result", {})
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        """Close the socket, ignoring an already-broken one."""

        try:
            self._sock.close()
        except OSError:
            pass

def resolve_explicit(args: argparse.Namespace) -> tuple[dict | None, str]:
    """Read the manifest (if asked) and work out the tag for an explicit-hash register."""
    checksums = None

    if args.checksums:
        checksums = fetch_checksums(args.checksums)

    version = ""

    if checksums is not None:
        version = str(checksums.get("version") or "")

    tag = derive_tag(version, str((checksums or {}).get("source_commit") or ""), args.tag)
    return checksums, tag

def prepare_register(args: argparse.Namespace) -> dict:
    """Turn the command line into a validated row (no network beyond the manifest)."""
    explicit = (args.sha256, args.size)

    if args.checksums and (any(value is not None for value in explicit)):
        raise usage_error("pass EITHER --checksums + --asset OR --sha256 + --size, not both")

    if not args.checksums and not args.sha256:
        raise usage_error("pass --checksums + --asset, or --sha256 + --size")

    checksums, tag = resolve_explicit(args)
    row = build_row(artifact = args.artifact, channel = args.channel, tag = tag, direct_url = args.direct_url, checksums = checksums, asset = args.asset, sha256 = args.sha256, size = args.size)
    validate_row(row)
    return row

def register(args: argparse.Namespace) -> int:
    """Write the row, verify it came back, and prove the public URL answers."""

    try:
        row = prepare_register(args)
    except gateway_error as error:
        print(f"error: {error}", file = sys.stderr)
        return EXIT_REFUSED

    print("row to write:")

    if args.dry_run:
        envelope.emit(row, sort_keys = True)
        return EXIT_OK

    token = os.environ.get(TOKEN_ENV, "")

    if not token:
        print(f"error: {TOKEN_ENV} is not set", file = sys.stderr)
        return EXIT_USAGE

    print(f"credential: {mask(token)}")
    envelope.emit(row, sort_keys = True)

    try:
        ws = json_rpc_socket(args.base, token)
    except (gateway_error, OSError, ssl.SSLError) as error:
        print(f"error: could not authenticate to the gateway: {error}", file = sys.stderr)
        return EXIT_REFUSED

    try:
        ws.call(1, "gateway.artifacts.upsert", row)
        print("upsert: accepted")
        listing = ws.call(2, "gateway.artifacts.list", {})
        rows = listing.get("artifacts") or listing.get("rows") or []
        mismatch = readback_mismatch(row, rows)

        if mismatch is not None:
            print(f"error: {mismatch}", file = sys.stderr)
            return EXIT_MISMATCH

        print(f"readback: matches on {'/'.join(READBACK_KEYS)} ({len(rows)} row(s) in the catalog)")
    except gateway_error as error:
        print(f"error: {error}", file = sys.stderr)
        return EXIT_REFUSED
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: the connection failed mid-call: {type(error).__name__}: {error}", file = sys.stderr)
        return EXIT_REFUSED
    finally:
        ws.close()

    url = public_url(args.base, row["artifact"], row["channel"])

    try:
        with _urlopen(url, args.timeout) as answer:
            status = getattr(answer, "status", 200)
    except (urllib.error.URLError, OSError) as error:
        print(f"error: the public URL still fails: {type(error).__name__}: {error}", file = sys.stderr)
        return EXIT_REFUSED

    print(f"anonymous download: HTTP {status} from {url}")

    if status != 200:
        print("error: the public URL must answer HTTP 200", file = sys.stderr)
        return EXIT_REFUSED

    print(f"done: {row['artifact']}/{row['channel']} is registered and publicly reachable")
    return EXIT_OK

def list_rows(args: argparse.Namespace) -> int:
    """Print the catalog as JSON."""
    token = os.environ.get(TOKEN_ENV, "")

    if not token:
        print(f"error: {TOKEN_ENV} is not set", file = sys.stderr)
        return EXIT_USAGE

    print(f"credential: {mask(token)}", file = sys.stderr)

    try:
        ws = json_rpc_socket(args.base, token)
    except (gateway_error, OSError, ssl.SSLError) as error:
        print(f"error: could not authenticate to the gateway: {error}", file = sys.stderr)
        return EXIT_REFUSED

    try:
        listing = ws.call(1, "gateway.artifacts.list", {})
    except gateway_error as error:
        print(f"error: {error}", file = sys.stderr)
        return EXIT_REFUSED
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: the connection failed mid-call: {type(error).__name__}: {error}", file = sys.stderr)
        return EXIT_REFUSED
    finally:
        ws.close()

    envelope.emit(listing)
    return EXIT_OK

def build_parser() -> argparse.ArgumentParser:
    """The CLI surface: register | list."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools gateway-catalog", description = "Register a release in the gateway's artifact catalog (or print it).")
    parser.add_argument("--base", default = DEFAULT_BASE, help = f"gateway base URL (default {DEFAULT_BASE})")
    parser.add_argument("--timeout", type = int, default = 45, help = "per-request timeout in seconds")
    sub = parser.add_subparsers(dest = "cmd", required = True)
    reg = sub.add_parser("register", help = "upsert one row, then verify the readback and the public URL")
    reg.add_argument("--artifact", required = True, help = "artifact name, e.g. chest-desktop")
    reg.add_argument("--channel", required = True, help = "channel, e.g. stable")
    reg.add_argument("--tag", help = "release stamp; derived as <version>-<commit12> from --checksums when absent")
    reg.add_argument("--direct-url", required = True, help = "the asset's absolute download URL")
    reg.add_argument("--checksums", help = "checksums.json URL or local path (sha256/size come from it)")
    reg.add_argument("--asset", help = "asset name inside the checksums manifest")
    reg.add_argument("--sha256", help = "explicit sha256 (64 hex) instead of --checksums")
    reg.add_argument("--size", type = int, help = "explicit size in bytes instead of --checksums")
    reg.add_argument("--dry-run", action = "store_true", help = "print the row and exit 0 without any network")
    sub.add_parser("list", help = "print the catalog rows as JSON")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.cmd == "register":
            return register(args)

        return list_rows(args)
    except usage_error as error:
        print(f"error: {error}", file = sys.stderr)
        return EXIT_USAGE

if __name__ == "__main__":
    raise SystemExit(main())
