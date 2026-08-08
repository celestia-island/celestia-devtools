#!/usr/bin/env python3
"""Ed25519 signing toolkit for Layer-3 agent repositories.

Implements the ``agent.toml.sig`` convention enforced by
``plana_custom_agent`` (see PLAN §11.3): a detached Ed25519 signature over the
raw bytes of ``agent.toml``, base64-encoded, committed next to the manifest.

Commands::

    sign-agent keygen [--out-dir DIR]      Generate key.pem + pubkey.pem
    sign-agent sign agent.toml [opts]      Write agent.toml.sig
    sign-agent verify agent.toml [opts]    Verify an existing signature

Key material is standard PEM handled by the system ``openssl`` CLI (OpenSSL 3+
Ed25519 support), so the tool has no Python dependencies.  The raw 64-byte
signature is byte-compatible with ``ed25519-dalek`` used on the Rust side.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import tempfile
from pathlib import Path

# DER prefix of an Ed25519 SPKI (SubjectPublicKeyInfo): the 12 bytes before
# the 32 raw key bytes.  Matches what ed25519-dalek VerifyingKey::from_bytes
# expects when we strip it for the base64 registration format.
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _openssl(args: list[str]) -> bytes:
    try:
        proc = subprocess.run(
            ["openssl", *args], check=True, capture_output=True
        )
    except FileNotFoundError as exc:
        raise SystemExit("openssl CLI not found; install openssl 3+ first") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "openssl failed: " + exc.stderr.decode(errors="replace").strip()
        ) from exc
    return proc.stdout


def _spki_to_raw(pub_pem: bytes) -> bytes:
    """Strip the PEM envelope and SPKI prefix, returning the raw 32-byte key."""
    body = b"".join(
        line for line in pub_pem.splitlines() if not line.startswith(b"-----")
    )
    der = base64.b64decode(body)
    if not der.startswith(ED25519_SPKI_PREFIX):
        raise SystemExit("unexpected SPKI structure; expected an Ed25519 public key")
    raw = der[len(ED25519_SPKI_PREFIX):]
    if len(raw) != 32:
        raise SystemExit(f"unexpected public key length: {len(raw)}")
    return raw


def _raw_to_spki_pem(raw: bytes) -> bytes:
    if len(raw) != 32:
        raise SystemExit("public key must be 32 raw bytes")
    der = ED25519_SPKI_PREFIX + raw
    b64 = base64.b64encode(der).decode()
    return f"-----BEGIN PUBLIC KEY-----\n{b64}\n-----END PUBLIC KEY-----\n".encode()


def cmd_keygen(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / "key.pem"
    pub_path = out_dir / "pubkey.pem"

    _openssl(["genpkey", "-algorithm", "ED25519", "-out", str(key_path)])
    pub_pem = _openssl(["pkey", "-in", str(key_path), "-pubout"])
    pub_path.write_bytes(pub_pem)

    pub_b64 = base64.b64encode(_spki_to_raw(pub_pem)).decode()
    print(f"wrote {key_path}")
    print(f"wrote {pub_path}")
    print(f"public key (base64, for platform registration): {pub_b64}")
    print(
        "register on the platform with: "
        f"CELESTIA_AGENT_SIGNING_PUBKEY_<SOURCE>={pub_b64}"
    )
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    key = Path(args.key)
    if not manifest.exists():
        raise SystemExit(f"manifest not found: {manifest}")
    if not key.exists():
        raise SystemExit(f"key not found: {key}")

    sig = _openssl(
        ["pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(manifest)]
    )
    sig_b64 = base64.b64encode(sig).decode()
    out = Path(args.out)
    out.write_text(sig_b64 + "\n")
    print(f"wrote {out} ({len(sig)} bytes, base64)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    sig_path = Path(args.sig)
    if not manifest.exists():
        raise SystemExit(f"manifest not found: {manifest}")
    if not sig_path.exists():
        raise SystemExit(f"signature not found: {sig_path}")

    if args.pubkey:
        pub_path = Path(args.pubkey)
        if not pub_path.exists():
            raise SystemExit(f"public key not found: {pub_path}")
        pub_pem = pub_path.read_bytes()
    elif args.pubkey_b64:
        raw = base64.b64decode(args.pubkey_b64)
        pub_pem = _raw_to_spki_pem(raw)
    else:
        raise SystemExit("either --pubkey or --pubkey-b64 is required")

    sig_raw = base64.b64decode(sig_path.read_text().strip())

    with tempfile.NamedTemporaryFile() as pub_file, tempfile.NamedTemporaryFile() as sig_file:
        pub_file.write(pub_pem)
        pub_file.flush()
        sig_file.write(sig_raw)
        sig_file.flush()
        _openssl(
            [
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey", pub_file.name,
                "-sigfile", sig_file.name,
                "-in", str(manifest),
            ]
        )
    print("signature OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign-agent",
        description="Ed25519 signing toolkit for Layer-3 agent repositories",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    kg = sub.add_parser("keygen", help="generate key.pem + pubkey.pem")
    kg.add_argument("--out-dir", default=".", help="output directory (default: .)")
    kg.set_defaults(func=cmd_keygen)

    sg = sub.add_parser("sign", help="sign agent.toml -> agent.toml.sig")
    sg.add_argument("manifest", help="path to agent.toml")
    sg.add_argument("--key", default="key.pem", help="Ed25519 private key PEM")
    sg.add_argument("--out", default="agent.toml.sig", help="output signature file")
    sg.set_defaults(func=cmd_sign)

    vf = sub.add_parser("verify", help="verify agent.toml against agent.toml.sig")
    vf.add_argument("manifest", help="path to agent.toml")
    vf.add_argument("--sig", default="agent.toml.sig", help="signature file")
    vf.add_argument("--pubkey", default=None, help="public key PEM file")
    vf.add_argument("--pubkey-b64", default=None, help="raw 32-byte public key, base64")
    vf.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
