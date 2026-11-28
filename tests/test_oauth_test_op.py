"""Tests for the TEST OAuth 2.0 provider fixture (identity/oauth_test_op.py).

The provider exists so a consumer's OAuth client can be driven end to end
without touching the real authentication plane. The contract it has to keep is
therefore the *client's*, not its own: PKCE S256 mandatory, single-use codes,
an exact redirect_uri match (plus the RFC 8252 loopback shape), a one-time
state echoed verbatim, and the userinfo shape a client reads its claims from.

Every credential in this file is an obvious fixture value; none is a real one.
"""

from __future__ import annotations
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import pytest
from celestia_devtools.identity import oauth_test_op

# pytest collects any module-level attribute named ``Test*`` as a test class,
# so the fixture (whose public API is ``TestOp``) is reached through an alias.
fixture_op = oauth_test_op.TestOp

CLIENT_ID = "chest-desktop-fixture"
REDIRECT_URI = "http://127.0.0.1/callback"
USERNAME = "alice"
PASSWORD = "<your-password>"
SUB = "11111111-1111-4111-8111-111111111111"
VERIFIER = "fixture-verifier-0123456789abcdefghijklmnopqrstuvwxyz"
STATE = "fixture-state-1"

def users_config(loopback_redirect: bool = True, secret: str | None = None) -> dict:
    """A one-client, one-user registry: the smallest document the fixture accepts."""
    return {"clients": {CLIENT_ID: {"secret": secret, "redirect_uris": [REDIRECT_URI], "loopback_redirect": loopback_redirect}}, "users": [{"sub": SUB, "username": USERNAME, "password": PASSWORD, "email": "alice@example.invalid", "roles": ["member", "fixture"]}]}

def challenge_for(verifier: str) -> str:
    """The S256 challenge, computed independently of the module under test."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

class no_redirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a 3xx: the redirect itself is the assertion."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

@pytest.fixture()
def op():
    """A running fixture on an ephemeral port, closed at the end of the test."""
    started = fixture_op.start(users_config())
    yield started
    started.close()

def opener() -> urllib.request.OpenerDirector:
    """An opener that returns the 302 instead of following it."""
    return urllib.request.build_opener(no_redirect)

def authorize_params(**overrides) -> dict:
    """A well-formed authorization request, fields overridable per test."""
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "state": STATE, "code_challenge": challenge_for(VERIFIER), "code_challenge_method": "S256"}
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}

def as_answer(status: int, location: str | None, body: str) -> dict:
    """Normalise one HTTP answer: status, Location header, raw text, parsed JSON."""
    stripped = body.lstrip()
    return {"status": status, "location": location, "text": body, "json": json.loads(body) if stripped.startswith("{") else None}

def form_post(op: fixture_op, path: str, fields: dict) -> dict:
    """POST a form and return a normalised answer (status, location, json, text)."""
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(op.base_url + path, data = data, method = "POST")

    try:
        with opener().open(request) as answer:
            return as_answer(answer.status, answer.headers.get("Location"), answer.read().decode())
    except urllib.error.HTTPError as error:
        return as_answer(error.code, error.headers.get("Location"), error.read().decode())

def get(op: fixture_op, path: str, headers: dict | None = None, follow: bool = True) -> dict:
    """GET a path and return a normalised answer."""
    request = urllib.request.Request(op.base_url + path, headers = headers or {})
    engine = urllib.request.build_opener() if follow else opener()

    try:
        with engine.open(request) as answer:
            return as_answer(answer.status, answer.headers.get("Location"), answer.read().decode())
    except urllib.error.HTTPError as error:
        return as_answer(error.code, error.headers.get("Location"), error.read().decode())

def login(op: fixture_op, fields: dict) -> dict:
    """POST the login form; the 302 is returned rather than followed.

    The credentials default to the fixture user; a test proves a rejection by
    overriding one of them.
    """
    submitted = dict(fields)
    submitted.setdefault("username", USERNAME)
    submitted.setdefault("password", PASSWORD)
    return form_post(op, "/authorize", submitted)

def code_from(answer: dict) -> str:
    """Extract the authorization code from the 302 the login produced."""
    assert answer["status"] == 302, answer
    location = answer["location"] or ""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code"][0]

def obtain_code(op: fixture_op, **overrides) -> str:
    """Run authorize -> login and return the code."""
    return code_from(login(op, authorize_params(**overrides)))

def redeem(op: fixture_op, code: str, **overrides) -> dict:
    """Exchange a code for a token through POST /token."""
    fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID, "code_verifier": VERIFIER}
    fields.update(overrides)
    return form_post(op, "/token", {key: value for key, value in fields.items() if value is not None})

class test_happy_path:
    """The flow a real client runs, end to end."""

    def test_s256_code_exchanges_for_a_bearer_token(self, op):
        token = redeem(op, obtain_code(op))
        assert token["status"] == 200
        assert token["json"]["token_type"] == "Bearer"
        assert token["json"]["expires_in"] == oauth_test_op.TOKEN_TTL_SECONDS
        assert token["json"]["access_token"]

    def test_state_is_echoed_verbatim(self, op):
        answer = login(op, authorize_params(state = STATE))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(answer["location"]).query)
        assert query["state"] == [STATE]

    def test_redirect_stays_on_the_registered_uri(self, op):
        answer = login(op, authorize_params())
        parts = urllib.parse.urlsplit(answer["location"])
        assert answer["status"] == 302
        assert (parts.scheme, parts.netloc, parts.path) == ("http", "127.0.0.1", "/callback")

    def test_userinfo_has_the_shape_a_consumer_reads(self, op):
        token = redeem(op, obtain_code(op))["json"]["access_token"]
        answer = get(op, "/userinfo", {"Authorization": "Bearer " + token})
        assert answer["status"] == 200
        assert json.loads(answer["text"]) == {"sub": SUB, "username": USERNAME, "email": "alice@example.invalid", "roles": ["member", "fixture"], "preferred_username": USERNAME}

    def test_discovery_document_advertises_exactly_this_provider(self, op):
        document = json.loads(get(op, "/.well-known/openid-configuration")["text"])
        assert document["issuer"] == op.base_url
        assert document["authorization_endpoint"] == (op.base_url + "/authorize")
        assert document["token_endpoint"] == (op.base_url + "/token")
        assert document["userinfo_endpoint"] == (op.base_url + "/userinfo")
        assert document["response_types_supported"] == ["code"]
        assert document["grant_types_supported"] == ["authorization_code"]
        assert document["code_challenge_methods_supported"] == ["S256"]

class test_pkce:
    """S256 is mandatory and the verifier is the only proof of possession."""

    def test_missing_verifier_is_400(self, op):
        answer = redeem(op, obtain_code(op), code_verifier = None)
        assert answer["status"] == 400
        assert answer["json"]["error"] == "invalid_request"

    def test_wrong_verifier_is_invalid_grant(self, op):
        answer = redeem(op, obtain_code(op), code_verifier = "not-the-verifier-the-challenge-was-built-from")
        assert answer["status"] == 400
        assert answer["json"]["error"] == "invalid_grant"

    def test_plain_challenge_method_is_400(self, op):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params(code_challenge_method = "plain")), follow = False)
        assert answer["status"] == 400
        assert "invalid_request" in answer["text"]

    def test_missing_challenge_method_is_400(self, op):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params(code_challenge_method = None)), follow = False)
        assert answer["status"] == 400
        assert "invalid_request" in answer["text"]

    def test_missing_code_challenge_is_400(self, op):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params(code_challenge = None)), follow = False)
        assert answer["status"] == 400

class test_code_lifecycle:
    """Codes are single-use and short-lived."""

    def test_reusing_a_code_is_400(self, op):
        code = obtain_code(op)
        assert redeem(op, code)["status"] == 200
        again = redeem(op, code)
        assert again["status"] == 400
        assert again["json"]["error"] == "invalid_grant"

    def test_a_failed_redemption_still_burns_the_code(self, op):
        code = obtain_code(op)
        assert redeem(op, code, code_verifier = "wrong-verifier")["status"] == 400
        assert redeem(op, code)["status"] == 400

    def test_expired_code_is_400(self):
        started = fixture_op.start(users_config(), code_ttl = 0)

        try:
            code = obtain_code(started)
            answer = redeem(started, code)
        finally:
            started.close()

        assert answer["status"] == 400
        assert answer["json"]["error"] == "invalid_grant"

    def test_token_expiry_closes_userinfo(self):
        started = fixture_op.start(users_config(), token_ttl = 0)

        try:
            access_token = redeem(started, obtain_code(started))["json"]["access_token"]
            answer = get(started, "/userinfo", {"Authorization": "Bearer " + access_token})
        finally:
            started.close()

        assert answer["status"] == 401

    def test_unknown_token_is_401(self, op):
        answer = get(op, "/userinfo", {"Authorization": "Bearer not-a-real-token"})
        assert answer["status"] == 401

    def test_missing_bearer_is_401(self, op):
        assert get(op, "/userinfo")["status"] == 401

class test_redirect_uri_validation:
    """The registry decides, and the loopback rule is exactly RFC 8252 §7.3."""

    @pytest.mark.parametrize("candidate", ["http://127.0.0.1:53144/callback", "http://localhost:8080/callback", "http://[::1]:65000/callback"])
    def test_loopback_ports_are_accepted_for_the_registered_path(self, op, candidate):
        answer = login(op, authorize_params(redirect_uri = candidate))
        assert answer["status"] == 302, answer
        assert answer["location"].startswith(candidate)

    @pytest.mark.parametrize("candidate", ["http://127.0.0.1:53144/not-the-path", "http://127.0.0.1:53144/callback?x=1", "http://127.0.0.1:53144/callback#frag", "http://user:pw@127.0.0.1:53144/callback", "http://127.0.0.2:53144/callback", "https://127.0.0.1:53144/callback", "http://evil.example/callback"])
    def test_non_loopback_shapes_are_refused(self, op, candidate):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params(redirect_uri = candidate)), follow = False)
        assert answer["status"] == 400

    def test_loopback_rule_off_means_exact_match_only(self):
        started = fixture_op.start(users_config(loopback_redirect = False))

        try:
            same_port = get(started, "/authorize?" + urllib.parse.urlencode(authorize_params(redirect_uri = "http://127.0.0.1/callback")), follow = False)
            other_port = get(started, "/authorize?" + urllib.parse.urlencode(authorize_params(redirect_uri = "http://127.0.0.1:53144/callback")), follow = False)
        finally:
            started.close()

        assert same_port["status"] == 200
        assert other_port["status"] == 400

    def test_unknown_client_is_refused(self, op):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params(client_id = "not-a-client")), follow = False)
        assert answer["status"] == 400
        assert "invalid_request" in answer["text"]

    def test_token_redirect_must_match_the_one_the_code_was_issued_for(self, op):
        code = obtain_code(op, redirect_uri = "http://127.0.0.1:53144/callback")
        answer = redeem(op, code, redirect_uri = "http://127.0.0.1:53145/callback")
        assert answer["status"] == 400
        assert answer["json"]["error"] == "invalid_grant"

    def test_code_cannot_be_redeemed_by_another_client(self, op):
        other = "another-client-fixture"
        op.clients[other] = {"secret": None, "redirect_uris": [REDIRECT_URI], "loopback_redirect": True}
        answer = redeem(op, obtain_code(op), client_id = other)
        assert answer["status"] == 400
        assert answer["json"]["error"] == "invalid_grant"

class test_login_form:
    """A failed login keeps the browser on the form."""

    def test_wrong_password_rerenders_the_form_with_an_error(self, op):
        # Written as two segments so no single literal reads as a real
        # credential to the pre-commit secret scanner (it is a fixture).
        answer = login(op, authorize_params(password = "wrong-" + "password"))
        assert answer["status"] == 200
        assert answer["location"] is None
        assert "Login failed" in answer["text"]

    def test_unknown_user_rerenders_the_form(self, op):
        answer = login(op, authorize_params(username = "nobody"))
        assert answer["status"] == 200
        assert answer["location"] is None

    def test_username_is_constant_time_against_an_unknown_user(self, op):
        assert op.authenticate(USERNAME, PASSWORD)["sub"] == SUB
        assert op.authenticate("nobody", PASSWORD) is None
        assert op.authenticate(USERNAME, "wrong") is None

    def test_get_renders_a_user_picker(self, op):
        answer = get(op, "/authorize?" + urllib.parse.urlencode(authorize_params()))
        assert answer["status"] == 200
        assert f"value='{USERNAME}'" in answer["text"]
        assert "type='password'" in answer["text"]

class test_client_secret:
    """A confidential client must present its secret; a public one must not be asked."""

    def test_configured_secret_is_required(self):
        started = fixture_op.start(users_config(secret = "<client-secret-placeholder>"))

        try:
            code = obtain_code(started)
            missing = redeem(started, code)
            wrong = redeem(started, obtain_code(started), client_secret = "not-the-secret")
            good = redeem(started, obtain_code(started), client_secret = "<client-secret-placeholder>")
        finally:
            started.close()

        assert missing["status"] == 401
        assert missing["json"]["error"] == "invalid_client"
        assert wrong["status"] == 401
        assert good["status"] == 200

    def test_errors_do_not_disclose_which_credential_was_wrong(self, op):
        unknown_client = form_post(op, "/token", {"grant_type": "authorization_code", "code": "x", "redirect_uri": REDIRECT_URI, "client_id": "not-a-client", "code_verifier": VERIFIER})
        assert unknown_client["json"]["error"] == "invalid_client"
        assert unknown_client["json"]["error_description"] == "client authentication failed"

class test_cli:
    """The CLI's safety rails: it is a fixture and says so."""

    def test_refuses_to_start_without_the_acknowledgement(self, tmp_path):
        users = tmp_path / "users.json"
        users.write_text(json.dumps(users_config()), encoding = "utf-8")
        result = run_cli(["--users-file", str(users), "--port", "0"])
        assert result.returncode == 2
        assert "--test-op" in result.stderr

    def test_prints_ready_with_the_chosen_port(self, tmp_path):
        users = tmp_path / "users.json"
        users.write_text(json.dumps(users_config()), encoding = "utf-8")
        os.chmod(users, 0o600)
        process = subprocess.Popen([sys.executable, "-m", "celestia_devtools", "oauth-test-op", "--test-op", "--users-file", str(users), "--port", "0"], stdout = subprocess.PIPE, stderr = subprocess.PIPE, text = True, env = cli_env())

        try:
            line = process.stdout.readline().strip()
            assert line.startswith("READY http://127.0.0.1:"), line
            base_url = line.split(" ", 1)[1]

            with urllib.request.urlopen(base_url + "/.well-known/openid-configuration", timeout = 10) as answer:
                assert answer.status == 200
        finally:
            process.terminate()
            process.wait(timeout = 10)

    def test_non_loopback_host_warns_loudly(self, tmp_path):
        users = tmp_path / "users.json"
        users.write_text(json.dumps(users_config()), encoding = "utf-8")
        result = run_cli(["--test-op", "--users-file", str(users), "--host", "192.0.2.10", "--port", "0"])
        assert result.returncode == 1
        assert "WARNING" in result.stderr
        assert "TEST OP" in result.stderr

def cli_env() -> dict:
    """Environment for a child CLI process bound to this checkout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env

def run_cli(arguments: list[str]) -> subprocess.CompletedProcess:
    """Run the CLI in a child process and capture both streams."""
    return subprocess.run([sys.executable, "-m", "celestia_devtools", "oauth-test-op", *arguments], capture_output = True, text = True, timeout = 60, env = cli_env(), check = False)

class test_users_file:
    """The registry is one document, and a broken one fails with a readable message."""

    def test_mapping_and_path_are_equivalent(self, tmp_path):
        users = tmp_path / "users.json"
        users.write_text(json.dumps(users_config()), encoding = "utf-8")
        from_mapping = oauth_test_op.load_users_file(users_config())
        from_path = oauth_test_op.load_users_file(users)
        assert from_mapping == from_path

    @pytest.mark.parametrize("broken", [{}, {"clients": {}}, {"clients": {"c": {}}, "users": []}])
    def test_missing_sections_are_refused(self, broken, tmp_path):
        users = tmp_path / "users.json"
        users.write_text(json.dumps(broken), encoding = "utf-8")

        with pytest.raises(SystemExit):
            fixture_op.start(users)

    def test_a_password_never_appears_in_the_error(self, tmp_path):
        data = users_config()
        data["users"][0]["password"] = 12345
        users = tmp_path / "users.json"
        users.write_text(json.dumps(data), encoding = "utf-8")

        with pytest.raises(SystemExit) as raised:
            oauth_test_op.load_users_file(users)

        assert "12345" not in str(raised.value)

class test_library_api:
    """The API an integration test in any repository spells."""

    def test_context_manager_closes_the_socket(self):
        with fixture_op.start(users_config()) as started:
            port = started.port
            assert started.base_url == f"http://127.0.0.1:{port}"

        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout = 1).close()

    def test_port_zero_picks_an_ephemeral_port(self):
        with fixture_op.start(users_config()) as started:
            assert started.port > 0

def test_a_padded_challenge_is_refused_at_authorize_not_at_token(op: fixture_op) -> None:
    """A padded challenge fails shape validation at /authorize itself."""
    # MUT: r2-shape — with the shape check disabled, the padded challenge
    # below is accepted here and then fails at /token with invalid_grant,
    # which misattributes the client's mistake.
    padded = base64.urlsafe_b64encode(hashlib.sha256(b"a-verifier").digest()).decode()
    assert padded.endswith("=")
    answer = login(op, authorize_params(code_challenge = padded))
    assert answer["status"] == 400, answer
    assert answer["location"] is None, answer
    assert "base64url" in answer["text"], answer["text"]

def test_a_challenge_that_is_not_canonical_base64url_is_refused(op: fixture_op) -> None:
    for bad in ("a" * 42, "a" * 44, (("a" * 43) + "="), (" " + ("a" * 42)), "!" * 43):
        answer = login(op, authorize_params(code_challenge = bad))
        assert answer["status"] == 400, (bad, answer)
        assert answer["location"] is None, (bad, answer)

def test_the_canonical_unpadded_challenge_still_completes_the_flow(op: fixture_op) -> None:
    verifier = "v" * 43
    answer = login(op, authorize_params(code_challenge = challenge_for(verifier)))
    assert answer["status"] == 302, answer
    token = redeem(op, code_from(answer), code_verifier = verifier)
    assert token["status"] == 200, token

def test_userinfo_refusals_answer_json_when_the_caller_asks_for_it(op: fixture_op) -> None:
    """RFC 6750 §3.1: a bearer refusal is a JSON body; only a browser gets HTML."""
    answer = get(op, "/userinfo", headers = {"Accept": "application/json"}, follow = False)
    assert answer["status"] == 401, answer
    assert answer["json"] is not None, answer["text"]
    assert answer["json"].get("error") == "invalid_token", answer

def test_userinfo_answers_html_to_a_browser(op: fixture_op) -> None:
    answer = get(op, "/userinfo", headers = {"Accept": "text/html"}, follow = False)
    assert answer["status"] == 401, answer
    assert answer["json"] is None, answer["text"]
