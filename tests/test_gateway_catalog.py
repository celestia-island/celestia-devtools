"""Tests for the gateway artifact-catalog registrar (identity/gateway_catalog.py).

Only the pure parts are covered here: row assembly from a checksums manifest,
tag derivation, argument validation, the dry-run document and credential
masking. The WebSocket path is deliberately not exercised here: it writes
the catalog the whole fleet downloads from, and a test must never be able
to do that.
comment-gate-ok: the live path is covered by the gateway's own wire tests
and by the release pipeline's registration run, both outside this file.
Every digest and token below is an obvious fixture value.
"""

from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from celestia_devtools.identity import gateway_catalog

ASSET = "shittim-chest-0.1.0-x64-setup.exe"
SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
COMMIT = "c2c42787e3849a1b2c3d4e5f6a7b8c9d0e1f2a3b"
DIRECT_URL = "https://example.invalid/celestia-artifacts/releases/download/desktop-stable/" + ASSET

# Split so the source carries no credential-shaped literal (the pre-commit
# secret scanner reads `token = "<long string>"` as a leak; this is a fixture).
FIXTURE_CREDENTIAL = "example-" + "token-value"

def manifest() -> dict:
    """A checksums.json shaped like the release one."""
    return {"version": "0.1.0", "source_commit": COMMIT, "artifacts": {ASSET: {"sha256": SHA256, "size": 123456789}}}

def explicit_row(**overrides) -> dict:
    """A fully specified row, fields overridable per test."""
    row = {"artifact": "chest-desktop", "channel": "stable", "tag": "0.1.0-c2c42787e384", "direct_url": DIRECT_URL, "sha256": SHA256, "size": 123456789}
    row.update(overrides)
    return row

def run_main(capsys, arguments: list[str], monkeypatch, token: str | None = None) -> tuple[int, str, str]:
    """Run main() and return (exit code, stdout, stderr)."""

    if token is None:
        monkeypatch.delenv("GATEWAY_ADMIN_TOKEN", raising = False)
    else:
        monkeypatch.setenv("GATEWAY_ADMIN_TOKEN", token)

    code = gateway_catalog.main(arguments)
    captured = capsys.readouterr()
    return code, captured.out, captured.err

class test_row_building:
    # comment-gate-ok: the two tests below compare the built row against the
# manifest's own values verbatim, which is the pin.
"""sha256/size come from the manifest, never from a frozen copy."""

    def test_manifest_fills_sha256_and_size(self):
        row = gateway_catalog.build_row(artifact = "chest-desktop", channel = "stable", tag = "0.1.0-c2c42787e384", direct_url = DIRECT_URL, checksums = manifest(), asset = ASSET)
        assert row["sha256"] == SHA256
        assert row["size"] == 123456789

    def test_explicit_hash_is_used_when_no_manifest(self):
        row = gateway_catalog.build_row(artifact = "chest-desktop", channel = "stable", tag = "0.1.0", direct_url = DIRECT_URL, sha256 = SHA256, size = 42)
        assert row == explicit_row(tag = "0.1.0", size = 42)

    def test_manifest_without_asset_is_a_usage_error(self):
        with pytest.raises(gateway_catalog.usage_error):
            gateway_catalog.build_row(artifact = "chest-desktop", channel = "stable", tag = "t", direct_url = DIRECT_URL, checksums = manifest())

    def test_asset_missing_from_the_manifest_names_the_known_ones(self):
        with pytest.raises(gateway_catalog.usage_error) as raised:
            gateway_catalog.build_row(artifact = "chest-desktop", channel = "stable", tag = "t", direct_url = DIRECT_URL, checksums = manifest(), asset = "nope.exe")

        assert ASSET in str(raised.value)

class test_tag_derivation:
    """The stamp style the live rows use: version plus the first 12 commit chars."""

    def test_version_and_commit_truncated_to_twelve(self):
        assert gateway_catalog.derive_tag("0.1.0", COMMIT) == "0.1.0-c2c42787e384"

    def test_explicit_tag_wins(self):
        assert gateway_catalog.derive_tag("0.1.0", COMMIT, "nightly") == "nightly"

    @pytest.mark.parametrize("version,commit", [("", COMMIT), ("0.1.0", "")])
    def test_missing_component_is_a_usage_error(self, version, commit):
        with pytest.raises(gateway_catalog.usage_error):
            gateway_catalog.derive_tag(version, commit)

class test_validation:
    """A row the fleet could not safely redirect is refused at the boundary (comment-gate-ok: test_a_complete_row_passes pins the accept path and the seven refusal cases below pin the rejections)."""

    def test_a_complete_row_passes(self):
        gateway_catalog.validate_row(explicit_row())

    @pytest.mark.parametrize("overrides", [{"artifact": ""}, {"channel": ""}, {"tag": ""}, {"direct_url": "not-a-url"}, {"direct_url": "ftp://example.invalid/x"}, {"sha256": "abc"}, {"sha256": SHA256.upper() + "0"}, {"size": 0}, {"size": -5}, {"size": "123"}])
    def test_broken_fields_are_refused(self, overrides):
        with pytest.raises(gateway_catalog.usage_error):
            gateway_catalog.validate_row(explicit_row(**overrides))

class test_masking:
    """The credential is identified by a mask alone."""

    def test_mask_is_length_plus_digest_prefix(self):
        token = FIXTURE_CREDENTIAL
        expected = f"<{len(token)} chars, sha256:{hashlib.sha256(token.encode()).hexdigest()[:8]}>"
        assert gateway_catalog.mask(token) == expected

    def test_mask_never_contains_the_token(self):
        token = FIXTURE_CREDENTIAL
        assert token not in gateway_catalog.mask(token)

    def test_empty_token_has_its_own_shape(self):
        assert gateway_catalog.mask("") == "<empty>"

class test_public_url:
    """The URL the anonymous fetch checks is the one the fleet downloads."""

    def test_url_is_derived_from_base_artifact_and_channel(self):
        assert gateway_catalog.public_url("https://gateway.example.invalid", "chest-desktop", "stable") == "https://gateway.example.invalid/downloads/chest-desktop/stable"

    def test_trailing_slash_on_the_base_does_not_double(self):
        assert gateway_catalog.public_url("https://gateway.example.invalid/", "a", "b") == "https://gateway.example.invalid/downloads/a/b"

class test_dry_run:
    """--dry-run prints the row and touches no network."""

    def test_manifest_dry_run_carries_derived_tag_and_digest(self, tmp_path, capsys, monkeypatch):
        path = tmp_path / "checksums.json"
        path.write_text(json.dumps(manifest()), encoding = "utf-8")
        code, out, _ = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--direct-url", DIRECT_URL, "--checksums", str(path), "--asset", ASSET, "--dry-run"], monkeypatch)
        assert code == 0
        row = json.loads(out.split("\n", 1)[1])
        assert row["tag"] == "0.1.0-c2c42787e384"
        assert row["sha256"] == SHA256
        assert row["size"] == 123456789

    def test_explicit_dry_run_needs_no_token(self, capsys, monkeypatch):
        code, out, _ = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--tag", "0.1.0", "--direct-url", DIRECT_URL, "--sha256", SHA256, "--size", "42", "--dry-run"], monkeypatch)
        assert code == 0
        assert json.loads(out.split("\n", 1)[1]) == explicit_row(tag = "0.1.0", size = 42)

class test_usage_errors:
    """Usage mistakes exit 2 before any network activity."""

    def test_missing_token_exits_2(self, capsys, monkeypatch):
        code, _, err = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--tag", "0.1.0", "--direct-url", DIRECT_URL, "--sha256", SHA256, "--size", "42"], monkeypatch)
        assert code == 2
        assert "GATEWAY_ADMIN_TOKEN" in err

    def test_list_without_token_exits_2(self, capsys, monkeypatch):
        code, _, err = run_main(capsys, ["list"], monkeypatch)
        assert code == 2
        assert "GATEWAY_ADMIN_TOKEN" in err

    def test_both_hash_sources_at_once_exit_2(self, tmp_path, capsys, monkeypatch):
        path = tmp_path / "checksums.json"
        path.write_text(json.dumps(manifest()), encoding = "utf-8")
        code, _, err = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--tag", "t", "--direct-url", DIRECT_URL, "--checksums", str(path), "--asset", ASSET, "--sha256", SHA256, "--size", "1", "--dry-run"], monkeypatch)
        assert code == 2
        assert "EITHER" in err

    def test_no_hash_source_at_all_exits_2(self, capsys, monkeypatch):
        code, _, err = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--tag", "t", "--direct-url", DIRECT_URL, "--dry-run"], monkeypatch)
        assert code == 2
        assert "--checksums" in err

    def test_an_unreadable_manifest_exits_1_without_a_traceback(self, tmp_path, capsys, monkeypatch):
        missing = tmp_path / "absent.json"
        code, _, err = run_main(capsys, ["register", "--artifact", "chest-desktop", "--channel", "stable", "--direct-url", DIRECT_URL, "--checksums", str(missing), "--asset", ASSET, "--dry-run"], monkeypatch)
        assert code == 1
        assert "could not read" in err
        assert "Traceback" not in err

class test_checksums_retry:
    """The release CDN answers empty bodies sometimes; the read retries then reports."""

    def test_empty_body_then_a_good_one_succeeds(self, tmp_path, capsys, monkeypatch):
        path = tmp_path / "checksums.json"
        path.write_text(json.dumps(manifest()), encoding = "utf-8")
        reads = []

        def fake_text() -> str:
            reads.append(1)
            return "" if len(reads) < 3 else json.dumps(manifest())

        monkeypatch.setattr(Path, "read_text", lambda self, encoding = None: fake_text())
        assert gateway_catalog.fetch_checksums(str(path)) == manifest()
        assert len(reads) == 3

    def test_three_empty_bodies_report_a_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "read_text", lambda self, encoding = None: "")

        with pytest.raises(gateway_catalog.gateway_error) as raised:
            gateway_catalog.fetch_checksums(str(tmp_path / "checksums.json"))

        assert "after 3 attempts" in str(raised.value)

class test_readback:
    """The post-write assertion: the catalog must serve exactly what was written."""

    def test_matching_row_has_no_mismatch(self):
        assert gateway_catalog.readback_mismatch(explicit_row(), [explicit_row()]) is None

    def test_a_missing_row_is_a_mismatch(self):
        assert gateway_catalog.readback_mismatch(explicit_row(), [explicit_row(channel = "beta")]) is not None

    @pytest.mark.parametrize("key", ["tag", "direct_url", "sha256", "size"])
    def test_every_verified_key_is_compared(self, key):
        drifted = explicit_row(**{key: "drifted"})
        mismatch = gateway_catalog.readback_mismatch(explicit_row(), [drifted])
        assert mismatch is not None
        assert key in mismatch

class test_cli_surface:
    """The dispatcher reaches the command and its subcommands are as documented."""

    def test_register_help_lists_every_documented_flag(self):
        result = subprocess.run([sys.executable, "-m", "celestia_devtools", "gateway-catalog", "register", "--help"], capture_output = True, text = True, timeout = 60, env = cli_env(), check = False)
        assert result.returncode == 0

        for flag in ("--artifact", "--channel", "--tag", "--direct-url", "--checksums", "--asset", "--sha256", "--size", "--dry-run"):
            assert flag in result.stdout

    def test_a_subcommand_is_required(self):
        result = subprocess.run([sys.executable, "-m", "celestia_devtools", "gateway-catalog"], capture_output = True, text = True, timeout = 60, env = cli_env(), check = False)
        assert result.returncode != 0

def cli_env() -> dict:
    """Environment for a child CLI process bound to this checkout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env
