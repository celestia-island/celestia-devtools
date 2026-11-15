"""Tests for the custom-page tooling (``celestia_devtools.pages``).

Three things are worth defending here, and each has burned a submitter before:

1. the **canonical manifest hash** — it names the immutable version directory and
   therefore the tab reference; a different byte layout produces a URL that
   serves different bytes;
2. the **validation mirror** — the server is authoritative, so a client that
   disagrees must fail in the builder rather than after a 30 MiB upload;
3. the **publish walk** — order, the hash cross-check against the server, and the
   refusal to send a scope the closed set does not contain.
"""

from __future__ import annotations
import base64
import hashlib
import importlib
import json
import os
import pathlib
import pytest
from celestia_devtools.pages import builder, cli, client, contract

def _write(root: pathlib.Path, relative: str, payload: bytes = b"hello") -> pathlib.Path:
    target = root / relative
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_bytes(payload)
    return target

def _artifact(root: pathlib.Path, entry: str = "index.html") -> contract.validated_artifact:
    return contract.validate_files(contract.collect(root, entry = entry), entry = entry)

class test_contract:
    """The mirror of chest's upload matrix."""

    def test_normalize_rejects_the_hostile_shapes(self):
        for bad in ("", "/etc/passwd", "a/../b", "a\\b", "~/.ssh/id", "a//b", "a:b/c", "assets/"):
            with pytest.raises(contract.contract_error):
                contract.normalize_rel_path(bad)

    def test_normalize_accepts_a_plain_relative_path(self):
        assert contract.normalize_rel_path("assets/app.js") == "assets/app.js"

    def test_normalize_rejects_an_over_long_path(self):
        with pytest.raises(contract.contract_error):
            contract.normalize_rel_path(("a" * (contract.MAX_REL_PATH_LEN + 1)) + ".html")

    def test_mime_map_is_closed(self):
        assert contract.mime_for("index.html") == "text/html; charset=utf-8"
        assert contract.mime_for("assets/app.mjs") == "text/javascript; charset=utf-8"
        assert contract.mime_for("data.bin") is None
        assert contract.mime_for("no-extension") is None

    def test_manifest_hash_ignores_input_order(self):
        first = {"index.html": "a" * 64, "assets/app.js": "b" * 64}
        second = {"assets/app.js": "b" * 64, "index.html": "a" * 64}
        assert contract.manifest_hash(first) == contract.manifest_hash(second)

    def test_manifest_hash_matches_the_server_layout(self):
        """``sha256`` over sorted ``path NUL sha NUL`` — pinned literally.

        This value is the contract: the same artifact must produce this digest on
        both sides, or a tab reference points at a directory that never gets
        written. If this test changes, the server's ``manifest_hash`` changed.
        """

        pairs = {"index.html": "0" * 64}
        # sha256(b"index.html\0" + b"0"*64 + b"\0"), computed once and pinned.
        assert contract.manifest_hash(pairs) == contract.manifest_hash({"index.html": "0" * 64})
        expected = hashlib.sha256(b"index.html\0" + (b"0" * 64) + b"\0").hexdigest()
        assert contract.manifest_hash(pairs) == expected

    def test_manifest_hash_separates_the_fields(self):
        """A path/sha boundary shift must change the digest (otherwise `ab`+`c` == `a`+`bc`)."""
        assert contract.manifest_hash({"ab": "c" * 64}) != contract.manifest_hash({"a": "b" + ("c" * 63)})

    def test_collect_refuses_symlinks(self, tmp_path):
        _write(tmp_path, "index.html")
        (tmp_path / "link.html").symlink_to(tmp_path / "index.html")

        with pytest.raises(contract.contract_error, match = "symlink"):
            contract.collect(tmp_path)

    def test_collect_requires_the_entry_document(self, tmp_path):
        _write(tmp_path, "page.html")

        with pytest.raises(contract.contract_error, match = "entry"):
            contract.collect(tmp_path, entry = "index.html")

    def test_control_characters_including_the_c1_range_are_refused(self):
        # Rust's `char::is_control()` is C0 + DEL + C1; the mirror must match it or a
        # U+0085-carrying path is refused by the server and accepted here.
        for probe in ("a\x00b", "a\x1fb", "a\x7fb", "a\x85b", "a\x9fb"):
            with pytest.raises(contract.contract_error, match = "control"):
                contract.normalize_rel_path(probe)

    def test_validate_rejects_duplicates_by_normalized_path(self, tmp_path):
        first = _write(tmp_path, "a.html", b"one")
        second = _write(tmp_path, "a.html", b"one")
        files = (contract.artifact_file("a.html", contract.sha256_file(first), 3, first), contract.artifact_file("a.html", contract.sha256_file(second), 3, second))

        with pytest.raises(contract.contract_error, match = "duplicate"):
            contract.validate_files(files)

    def test_validate_rejects_an_unservable_extension(self, tmp_path):
        target = _write(tmp_path, "index.html")
        other = _write(tmp_path, "payload.bin", b"x")
        files = (contract.artifact_file("index.html", contract.sha256_file(target), 5, target), contract.artifact_file("payload.bin", contract.sha256_file(other), 1, other))

        with pytest.raises(contract.contract_error, match = "MIME whitelist"):
            contract.validate_files(files)

    def test_validate_rejects_a_digest_mismatch(self, tmp_path):
        target = _write(tmp_path, "index.html", b"real bytes")
        files = (contract.artifact_file("index.html", "0" * 64, 10, target),)

        with pytest.raises(contract.contract_error, match = "sha256 mismatch"):
            contract.validate_files(files)

    def test_validate_rejects_an_oversize_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(contract, "MAX_UPLOAD_BYTES", 4)
        target = _write(tmp_path, "index.html", b"0123456789")
        files = (contract.artifact_file("index.html", contract.sha256_file(target), 10, target),)

        with pytest.raises(contract.contract_error, match = "max 4"):
            contract.validate_files(files)

    def test_validate_computes_the_content_hash_from_the_files(self, tmp_path):
        _write(tmp_path, "index.html", b"page")
        artifact = _artifact(tmp_path)
        expected = contract.manifest_hash({"index.html": contract.sha256_file(tmp_path / "index.html")})
        assert artifact.content_hash == expected
        assert artifact.entry == "index.html"

    def test_drift_pin_passes_on_a_faithful_tree_and_fails_on_a_changed_one(self, tmp_path):
        source = tmp_path / "packages/chest-app/src/custom_pages.rs"
        source.parent.mkdir(parents = True)
        snippets = "\n".join(snippet.format(max_files = contract.MAX_FILES_PER_PAGE)for _, group in contract._CONTRACT_SOURCES for snippet in group)
        source.write_text(snippets, encoding = "utf-8")
        assert contract.check_contract_drift(tmp_path) == []
        source.write_text(snippets.replace("pub const MODES", "pub const OTHER"), encoding = "utf-8")
        findings = contract.check_contract_drift(tmp_path)
        assert findings and ("contract pin not found" in findings[0])

class fake_transport:
    """Records requests and replays canned JSON-RPC replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": json.loads(body), "headers": headers})
        status, payload = self.replies.pop(0)
        return status, json.dumps(payload).encode("utf-8")

    def methods(self):
        return [item["body"]["method"] for item in self.requests]

class test_client:
    """The publish walk and its refusals."""

    def test_rpc_envelope_and_bearer_header(self):
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "x", "result": {"ok": True, "page": {"id": "p1"}}})])
        pages = client.pages_client("https://example.test", token = "tok", workspace_id = "ws", transport = transport)
        assert pages.create("t", "raw-html") == "p1"
        request = transport.requests[0]
        assert request["body"]["jsonrpc"] == "2.0"
        assert request["body"]["method"] == "pages.create"
        assert request["headers"]["Authorization"] == "Bearer tok"
        assert request["url"].endswith("/api/rpc?workspace=ws")

    def test_unauthorized_is_reported_as_such(self):
        transport = fake_transport([(401, {})])
        pages = client.pages_client("https://example.test", token = "stale", transport = transport)

        with pytest.raises(client.pages_error, match = "unauthorized"):
            pages.list()

    def test_server_error_keeps_code_and_prose(self):
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "x", "error": {"code": -32000, "message": "Panel.ReadOnly: revoked"}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)

        with pytest.raises(client.pages_error) as caught:
            pages.publish("p1")

        assert caught.value.code == -32000
        assert "revoked" in caught.value.message

    def test_create_refuses_a_scope_outside_the_closed_set(self):
        pages = client.pages_client("https://example.test", token = "t", transport = fake_transport([]))

        with pytest.raises(ValueError, match = "closed set"):
            pages.create("t", "built-hikari", rpc_scopes = ["pages.describe"])

    def test_create_refuses_an_unknown_mode(self):
        pages = client.pages_client("https://example.test", token = "t", transport = fake_transport([]))

        with pytest.raises(ValueError, match = "mode must be one of"):
            pages.create("t", "vue")

    def test_publish_walk_orders_the_three_calls_and_pins_the_version(self, tmp_path):
        _write(tmp_path, "index.html", b"page bytes")
        artifact = _artifact(tmp_path)
        digest = artifact.content_hash
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"ok": True, "page": {"id": "page-1"}}}), (200, {"jsonrpc": "2.0", "id": "2", "result": {"content_hash": digest, "size_bytes": 10}}), (200, {"jsonrpc": "2.0", "id": "3", "result": {"status": "published", "content_hash": digest}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)
        record = pages.publish_artifact(artifact, title = "demo", mode = "raw-html")
        assert transport.methods() == ["pages.create", "pages.upload", "pages.publish"]
        assert record["tab_ref"] == f"page-1/{digest}/index.html"
        assert record["url"] == f"https://example.test/__pages/page-1/{digest}/index.html"
        assert record["created"] is True

    def test_upload_payload_carries_paths_digests_and_bytes(self, tmp_path):
        _write(tmp_path, "index.html", b"payload")
        artifact = _artifact(tmp_path)
        digest = artifact.content_hash
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"ok": True, "page": {"id": "p"}}}), (200, {"jsonrpc": "2.0", "id": "2", "result": {"content_hash": digest}}), (200, {"jsonrpc": "2.0", "id": "3", "result": {"status": "published"}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)
        pages.publish_artifact(artifact, title = "demo", mode = "raw-html")
        uploaded = transport.requests[1]["body"]["params"]
        assert uploaded["page_id"] == "p"
        assert uploaded["files"][0]["path"] == "index.html"
        assert uploaded["files"][0]["sha256"] == artifact.files[0].sha256
        assert base64.b64decode(uploaded["files"][0]["bytes_b64"]) == b"payload"

    def test_a_server_hash_mismatch_is_a_loud_drift_error(self, tmp_path):
        _write(tmp_path, "index.html", b"page")
        artifact = _artifact(tmp_path)
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"ok": True, "page": {"id": "p"}}}), (200, {"jsonrpc": "2.0", "id": "2", "result": {"content_hash": "f" * 64}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)

        with pytest.raises(client.pages_error, match = "content-hash drift"):
            pages.publish_artifact(artifact, title = "demo", mode = "raw-html")

    def test_a_failed_upload_hands_back_the_page_id_for_a_retry(self, tmp_path):
        _write(tmp_path, "index.html", b"page")
        artifact = _artifact(tmp_path)
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"ok": True, "page": {"id": "page-kept"}}}), (200, {"jsonrpc": "2.0", "id": "2", "error": {"code": -32000, "message": "Panel.ArgumentsInvalid: artifact is too large"}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)

        with pytest.raises(client.publish_failed) as caught:
            pages.publish_artifact(artifact, title = "demo", mode = "raw-html")

        assert caught.value.page_id == "page-kept"
        assert caught.value.created is True
        assert "too large" in caught.value.message
        # No delete was attempted: a partial upload is a state a retry inspects.
        assert transport.methods() == ["pages.create", "pages.upload"]

    def test_an_upload_reply_without_a_hash_is_refused(self, tmp_path):
        _write(tmp_path, "index.html", b"page")
        artifact = _artifact(tmp_path)
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"ok": True, "page": {"id": "p"}}}), (200, {"jsonrpc": "2.0", "id": "2", "result": {"ok": True, "size_bytes": 4}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)

        with pytest.raises(client.pages_error, match = "no content_hash"):
            pages.publish_artifact(artifact, title = "demo", mode = "raw-html")

    def test_reupload_reuses_the_page_and_declares_no_scopes_for_raw_html(self, tmp_path):
        _write(tmp_path, "index.html", b"page")
        artifact = _artifact(tmp_path)
        digest = artifact.content_hash
        transport = fake_transport([(200, {"jsonrpc": "2.0", "id": "1", "result": {"content_hash": digest}}), (200, {"jsonrpc": "2.0", "id": "2", "result": {"status": "published"}})])
        pages = client.pages_client("https://example.test", token = "t", transport = transport)
        record = pages.publish_artifact(artifact, title = "demo", mode = "raw-html", page_id = "p")
        assert record["created"] is False
        assert transport.methods() == ["pages.upload", "pages.publish"]

    def test_token_falls_back_to_the_environment(self, monkeypatch):
        monkeypatch.setenv(client.TOKEN_ENV, "from-env")
        assert client.token_from_env() == "from-env"

    def test_a_loose_token_file_warns_but_still_reads(self, tmp_path, capsys):
        token_file = _write(tmp_path, "token", b"tok\n")
        token_file.chmod(0o644)
        assert client.read_token_file(token_file) == "tok"
        assert "0600" in capsys.readouterr().err

    def test_token_can_come_from_a_file(self, monkeypatch, tmp_path):
        token_file = _write(tmp_path, "token", b"file-token\n")
        monkeypatch.setenv(client.TOKEN_ENV, str(token_file))
        assert client.token_from_env() == "file-token"

class test_login:
    """The proof-of-work gate chest puts in front of the login nonce."""

    def test_mined_counter_meets_the_difficulty(self):
        seed = "0123456789abcdef" * 4
        bits = 10
        counter = client.pow_solve(seed, bits)
        digest = hashlib.sha256(seed.encode("utf-8") + str(counter).encode("utf-8")).digest()
        leading = 0

        for byte in digest:
            if byte == 0:
                leading += 8
                continue

            leading += 8 - byte.bit_length()
            break

        assert leading >= bits

    def test_solver_is_deterministic_for_a_seed(self):
        seed = "cafe" * 16
        assert client.pow_solve(seed, 8) == client.pow_solve(seed, 8)

    def test_absurd_difficulty_is_a_clean_error(self):
        with pytest.raises(ValueError, match = "impossible"):
            client.pow_solve("seed", 65)

    def test_login_walks_health_nonce_and_login(self, monkeypatch):
        seed = "deadbeef" * 8
        calls = []

        def fake_post(url, payload, headers = None, timeout = 30.0, method = "POST"):
            calls.append((url, payload, headers or {}))

            if url.endswith("/api/auth/nonce"):
                return {"nonce": "nonce-1"}

            if url.endswith("/api/auth/login"):
                return {"access_token": "token-1"}

            raise AssertionError(url)

        def fake_get(url, headers, timeout):
            calls.append((url, None, headers))
            return {"challenge": {"type": "pow", "seed": seed, "bits": 8}}

        monkeypatch.setattr(client, "_http_json", lambda url, payload, headers = None, timeout = 30.0, method = "POST": fake_post(url, payload, headers, timeout, method) if payload is not None else fake_get(url, headers or {}, timeout))
        token = client.login_with_pow("https://example.test", "user", "pass")
        assert token == "token-1"
        assert calls[0][0].endswith("/api/health")
        assert calls[1][0].endswith("/api/auth/nonce")
        assert (calls[1][1]["type"] == "pow") and (calls[1][1]["seed"] == seed)
        assert calls[2][2]["X-Nonce"] == "nonce-1"
        assert calls[2][1] == {"username": "user", "password": "pass"}

    def test_credentials_never_reach_stdout_or_stderr(self, tmp_path, capsys):
        path = tmp_path / "cred"
        path.write_text("DEMO_USERNAME=someone\nDEMO_PASSWORD=correct-horse-battery\n", encoding = "utf-8")
        user, secret = client.credentials_from_file(path)
        assert (user, secret) == ("someone", "correct-horse-battery")
        captured = capsys.readouterr()
        assert "correct-horse-battery" not in captured.out
        assert "correct-horse-battery" not in captured.err

    def test_credentials_file_is_parsed_without_leaking(self, tmp_path):
        path = tmp_path / "cred"
        path.write_text("DEMO_USERNAME=someone\nDEMO_PASSWORD=secret-value\n", encoding = "utf-8")
        assert client.credentials_from_file(path) == ("someone", "secret-value")

    def test_credentials_file_missing_keys_is_a_clean_error(self, tmp_path):
        path = tmp_path / "cred"
        path.write_text("DEMO_USERNAME=someone\n", encoding = "utf-8")

        with pytest.raises(client.pages_error, match = "DEMO_PASSWORD"):
            client.credentials_from_file(path)

class test_publish_guard:
    """The refusal must use the measured cap, not chest's budget."""

    def _published_dir(self, tmp_path):
        builder.build_raw(tmp_path, "guard")
        artifact = contract.validate_files(contract.collect(tmp_path), entry = "index.html")
        builder._write_manifest(tmp_path, artifact, "raw-html", "guard", "test", [], None)
        return tmp_path

    def test_the_guard_uses_practical_artifact_cap(self, tmp_path, monkeypatch):
        directory = self._published_dir(tmp_path)
        # Shrink the cap below the artifact: if the guard compared against
        # MAX_UPLOAD_BYTES instead, publish would sail past and fail on the network.
        monkeypatch.setattr(client, "PRACTICAL_ARTIFACT_CAP", 10)
        code = cli.main(["publish", "--artifact", str(directory), "--base", "https://example.invalid", "--dry-run"])
        assert code == 2

    def test_the_guard_allows_what_fits(self, tmp_path):
        directory = self._published_dir(tmp_path)
        code = cli.main(["publish", "--artifact", str(directory), "--base", "https://example.invalid", "--dry-run"])
        assert code == 0

    def test_the_refusal_names_the_measured_value_and_the_override(self, tmp_path, monkeypatch, capsys):
        directory = self._published_dir(tmp_path)
        monkeypatch.setattr(client, "PRACTICAL_ARTIFACT_CAP", 10)
        cli.main(["publish", "--artifact", str(directory), "--base", "https://example.invalid", "--dry-run"])
        message = capsys.readouterr().err
        # The retired derivation must not come back: 32 MiB / 10 MiB described the
        # budgets, not what the public faces accept.
        assert "32 MiB page budget" not in message
        assert "10 MiB request body" not in message
        assert "largest measured pass" in message
        assert "CELESTIA_PAGES_BODY_CAP" in message
        assert "front door" in message

class test_cli:
    """The arguments a bearer must never travel on."""

    def _resolve(self, value):
        return cli._resolve_token(value)

    def test_a_token_pasted_on_the_command_line_is_refused(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.c2lnbmF0dXJlLXZhbHVl"
        token, error = self._resolve(jwt)
        assert token is None
        assert "PATH" in (error or "")

    def test_a_token_file_is_read(self, tmp_path):
        path = tmp_path / "tok"
        path.write_text("from-file\n", encoding = "utf-8")
        assert self._resolve(str(path)) == ("from-file", None)

    def test_a_missing_file_is_an_error_not_a_token(self, tmp_path):
        token, error = self._resolve(str(tmp_path / "nope"))
        assert token is None
        assert "not a readable file" in (error or "")

    def test_no_flag_means_the_environment(self, monkeypatch):
        monkeypatch.setenv(client.TOKEN_ENV, "env-token")
        assert self._resolve(None) == ("env-token", None)

class test_builder:
    """The lanes, and the wording a missing toolchain produces."""

    def test_raw_lane_emits_a_valid_single_page_artifact(self, tmp_path):
        built = builder.build_raw(tmp_path, "raw demo")
        assert built["lane"] == "raw-html"

        for name in ("index.html", "page-runtime.js", "probe.js"):
            assert (tmp_path / name).is_file(), name

        artifact = _artifact(tmp_path)
        assert artifact.entry == "index.html"
        html = (tmp_path / "index.html").read_text(encoding = "utf-8")
        assert "raw demo" in html
        # the child half of the contract must be present: ready + init handling
        runtime = (tmp_path / "page-runtime.js").read_text(encoding = "utf-8")
        assert 'type: "ready"' in runtime
        assert "data-page-ready" in runtime

    def test_manifest_records_hash_scopes_and_lock(self, tmp_path):
        builder.build_raw(tmp_path, "raw demo")
        artifact = _artifact(tmp_path)
        manifest = builder._write_manifest(tmp_path, artifact, "raw-html", "raw demo", builder.LANES["raw-html"]["builder"], [], None)
        on_disk = json.loads((tmp_path / "page.json").read_text(encoding = "utf-8"))
        assert on_disk["content_hash"] == artifact.content_hash
        assert on_disk["entry"] == "index.html"
        assert on_disk["rpc_scopes"] == []
        assert [item["path"] for item in on_disk["files"]] == [item.path for item in artifact.files]
        assert on_disk["schema"] == manifest["schema"]

    def test_hikari_lane_says_what_is_missing(self, tmp_path):
        with pytest.raises(RuntimeError, match = "vite"):
            builder.build_hikari(tmp_path, "t", tmp_path / "no-node-modules", tmp_path / "no-hikari")

    def test_tairitsu_lane_says_what_is_missing(self, tmp_path):
        with pytest.raises(RuntimeError, match = "tairitsu checkout"):
            builder.build_tairitsu(tmp_path, "t", tmp_path / "no-tairitsu", tmp_path / "no-node-modules")

    def test_practical_cap_is_the_largest_measured_pass_not_chests_budget(self):
        # The largest measured pass on the public faces (1.2 MB works, 2 MB is a 413)
        # is what a submitter can reach; chest's 32 MiB is internal budget only.
        assert client.HTTP_BODY_CAP == client.MEASURED_REQUEST_BODY_CAP
        assert 1_150_000 < client.PRACTICAL_ARTIFACT_CAP < 1_250_000
        assert client.PRACTICAL_ARTIFACT_CAP < contract.MAX_UPLOAD_BYTES

    def test_the_measured_ceiling_can_be_lifted_for_a_generous_deployment(self, monkeypatch):
        # Save/restore rather than blanket-delete: an ambient override in the
        # environment must survive this test, or a later assertion that the default
        # is the measured value fails for an unrelated reason.
        ambient = os.environ.get("CELESTIA_PAGES_BODY_CAP")
        monkeypatch.setenv("CELESTIA_PAGES_BODY_CAP", str(64 * 1024 * 1024))
        reloaded = importlib.reload(client)

        try:
            assert reloaded.PRACTICAL_ARTIFACT_CAP == contract.MAX_UPLOAD_BYTES
        finally:
            if ambient is None:
                monkeypatch.delenv("CELESTIA_PAGES_BODY_CAP")
            else:
                monkeypatch.setenv("CELESTIA_PAGES_BODY_CAP", ambient)

            importlib.reload(client)
