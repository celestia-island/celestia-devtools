"""End-to-end tests for the sign-agent toolkit (v2 package manifest).

Each test drives the CLI through real openssl Ed25519 operations.  The
manifest format and signature must stay byte-compatible with the Rust side
(plana_custom_agent signature.rs), which the sign->verify round-trip plus the
raw base64 conventions assert.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from celestia_devtools.agent.sign import (
    SIGNATURE_FILE,
    _build_manifest,
    _raw_to_spki_pem,
    _spki_to_raw,
    main,
)


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def sample_package(root: Path) -> None:
    (root / "sub").mkdir()
    (root / "agent.toml").write_text('[agent]\nid = "demo"\nlayer = 3\n')
    (root / "plugin.ts").write_text("globalThis.handleRequest = null;\n")
    (root / "sub" / "util.js").write_text("export const x = 1;\n")


def test_keygen_sign_verify_roundtrip(workdir: Path) -> None:
    assert main(["keygen", "--out-dir", str(workdir)]) == 0
    assert (workdir / "key.pem").exists()
    assert (workdir / "pubkey.pem").exists()

    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)

    assert main(["sign", str(agent), "--key", str(workdir / "key.pem")]) == 0
    assert (agent / SIGNATURE_FILE).exists()

    assert main(
        ["verify", str(agent), "--pubkey", str(workdir / "pubkey.pem")]
    ) == 0


def test_verify_rejects_tampered_code_file(workdir: Path) -> None:
    main(["keygen", "--out-dir", str(workdir)])
    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)
    main(["sign", str(agent), "--key", str(workdir / "key.pem")])

    # Tamper with a code file (not agent.toml) — v2 must catch it.
    (agent / "plugin.ts").write_text("evil code\n")
    with pytest.raises(SystemExit):
        main(["verify", str(agent), "--pubkey", str(workdir / "pubkey.pem")])


def test_verify_rejects_added_file(workdir: Path) -> None:
    main(["keygen", "--out-dir", str(workdir)])
    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)
    main(["sign", str(agent), "--key", str(workdir / "key.pem")])
    (agent / "backdoor.sh").write_text("#!/bin/sh\n")
    with pytest.raises(SystemExit):
        main(["verify", str(agent), "--pubkey", str(workdir / "pubkey.pem")])


def test_keygen_base64_key_verifies_manifest(workdir: Path, capsys) -> None:
    main(["keygen", "--out-dir", str(workdir)])
    captured = capsys.readouterr()
    line = next(
        line_ for line_ in captured.out.splitlines() if "public key (base64" in line_
    )
    pub_b64 = line.split("): ")[-1].strip()
    assert len(base64.b64decode(pub_b64)) == 32

    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)
    main(["sign", str(agent), "--key", str(workdir / "key.pem")])
    # The raw base64 key (the format registered on the platform) must verify.
    assert main(
        ["verify", str(agent), "--pubkey-b64", pub_b64]
    ) == 0


def test_manifest_is_deterministic(workdir: Path) -> None:
    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)
    assert _build_manifest(agent) == _build_manifest(agent)
    assert b'"version":1' in _build_manifest(agent)


def test_manifest_matches_rust_ordering(workdir: Path) -> None:
    """Files must be sorted by path, entries path-then-sha256 (serde order)."""
    agent = workdir / "agent"
    agent.mkdir()
    sample_package(agent)
    manifest = _build_manifest(agent).decode()
    assert manifest.index("agent.toml") < manifest.index("plugin.ts")
    assert manifest.index("plugin.ts") < manifest.index("sub/util.js")
    assert '"path":"agent.toml","sha256":"' in manifest


def test_pem_helpers_roundtrip() -> None:
    raw = bytes(range(32))
    pem = _raw_to_spki_pem(raw)
    assert _spki_to_raw(pem) == raw


def test_manifest_keeps_raw_utf8(workdir: Path) -> None:
    """serde_json outputs raw UTF-8; the Python side must not escape it."""
    agent = workdir / "agent"
    (agent / "子目录").mkdir(parents=True)
    (agent / "agent.toml").write_text('[agent]\nid = "demo"\nlayer = 3\n')
    (agent / "子目录" / "说明.txt").write_text("你好\n")
    manifest = _build_manifest(agent)
    assert "子目录" in manifest.decode()
    assert "\\u" not in manifest.decode()


def test_manifest_excludes_git_anywhere(workdir: Path) -> None:
    agent = workdir / "agent"
    (agent / "sub" / ".git").mkdir(parents=True)
    (agent / "agent.toml").write_text('[agent]\nid = "demo"\nlayer = 3\n')
    (agent / "sub" / ".git" / "config").write_text("[core]\n")
    manifest = _build_manifest(agent).decode()
    assert ".git" not in manifest
