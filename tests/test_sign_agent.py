"""End-to-end tests for the sign-agent toolkit.

Each test drives the CLI through real openssl Ed25519 operations.  The
signature format must stay byte-compatible with ed25519-dalek on the Rust
side (plana_custom_agent), which the sign->verify round-trip plus the raw
base64 conventions assert.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from celestia_devtools.agent.sign import _raw_to_spki_pem, _spki_to_raw, main


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def test_keygen_sign_verify_roundtrip(workdir: Path) -> None:
    assert main(["keygen", "--out-dir", str(workdir)]) == 0
    assert (workdir / "key.pem").exists()
    assert (workdir / "pubkey.pem").exists()

    manifest = workdir / "agent.toml"
    manifest.write_text('[agent]\nid = "demo"\nlayer = 3\n')

    assert main(
        [
            "sign",
            str(manifest),
            "--key", str(workdir / "key.pem"),
            "--out", str(workdir / "agent.toml.sig"),
        ]
    ) == 0
    assert (workdir / "agent.toml.sig").exists()

    assert main(
        [
            "verify",
            str(manifest),
            "--sig", str(workdir / "agent.toml.sig"),
            "--pubkey", str(workdir / "pubkey.pem"),
        ]
    ) == 0


def test_verify_rejects_tampered_manifest(workdir: Path) -> None:
    main(["keygen", "--out-dir", str(workdir)])
    manifest = workdir / "agent.toml"
    manifest.write_text('[agent]\nid = "demo"\nlayer = 3\n')
    main(
        [
            "sign",
            str(manifest),
            "--key", str(workdir / "key.pem"),
            "--out", str(workdir / "agent.toml.sig"),
        ]
    )
    manifest.write_text('[agent]\nid = "evil"\nlayer = 3\n')
    with pytest.raises(SystemExit):
        main(
            [
                "verify",
                str(manifest),
                "--sig", str(workdir / "agent.toml.sig"),
                "--pubkey", str(workdir / "pubkey.pem"),
            ]
        )


def test_keygen_base64_key_verifies_manifest(workdir: Path, capsys) -> None:
    main(["keygen", "--out-dir", str(workdir)])
    captured = capsys.readouterr()
    line = next(line_ for line_ in captured.out.splitlines() if "public key (base64" in line_)
    pub_b64 = line.split("): ")[-1].strip()
    assert len(base64.b64decode(pub_b64)) == 32

    manifest = workdir / "agent.toml"
    manifest.write_text('[agent]\nid = "demo"\nlayer = 3\n')
    main(
        [
            "sign",
            str(manifest),
            "--key", str(workdir / "key.pem"),
            "--out", str(workdir / "agent.toml.sig"),
        ]
    )
    # The raw base64 key (the format registered on the platform) must verify.
    assert main(
        [
            "verify",
            str(manifest),
            "--sig", str(workdir / "agent.toml.sig"),
            "--pubkey-b64", pub_b64,
        ]
    ) == 0


def test_pem_helpers_roundtrip() -> None:
    raw = bytes(range(32))
    pem = _raw_to_spki_pem(raw)
    assert _spki_to_raw(pem) == raw
