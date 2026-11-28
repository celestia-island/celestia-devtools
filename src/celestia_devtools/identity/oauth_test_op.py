#!/usr/bin/env python3
"""TEST OP — not an identity provider. A fixture, never a real authentication plane.

Every hosted face in the family authenticates against the ONE real plane
(``auth.celestia.world``). No demo face, and no integration test, should have to
touch that plane or the account behind it just to obtain a token for a
throwaway identity. This module is the substitute: a complete OAuth 2.0
authorization-code provider with mandatory PKCE (S256) that is **only** ever a
test fixture.

It is a fixture because of how it treats secrets, not because of what it
implements:

  - users and their passwords live in a plain JSON file you hand it (0600);
  - every code and token is in memory and dies with the process;
  - the CLI refuses to start without ``--test-op``, binds ``127.0.0.1`` unless
    told otherwise, and shouts on stderr when it is told otherwise.

The contract is what a consumer's OAuth client actually reads, so that client
can point at ``TestOp.base_url`` unchanged:

  - ``GET  /.well-known/openid-configuration`` — discovery document;
  - ``GET  /authorize``  — parameter validation, then a minimal login form;
  - ``POST /authorize``  — login; success 302s to ``redirect_uri?code=&state=``;
  - ``POST /token``      — code + PKCE verifier for an opaque bearer token;
  - ``GET  /userinfo``   — ``sub/username/email/roles/preferred_username``.

Errors use the OAuth error shapes (``error``, ``error_description``) and never
say which of ``code`` / ``client_id`` / ``redirect_uri`` / ``code_verifier``
was wrong beyond what RFC 6749 §5.2 requires.

Library use, from any repository's integration tests::

    from celestia_devtools.identity.oauth_test_op import TestOp

    with TestOp.start(Path("users.json")) as op:
        authorize_with_pkce(op.base_url, ...)   # your client, unchanged

CLI use::

    celestia-devtools oauth-test-op --test-op --users-file users.json --port 0

With ``--port 0`` the chosen port is printed as ``READY <base_url>`` on stdout
(flushed) so a harness can wait for it before driving the flow.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence

# Codes are short-lived by design: the browser hop between /authorize and
# /token is milliseconds, and a fixture code should never outlive a debugger
# pause. Tokens, by contrast, must outlive a whole integration run.
CODE_TTL_SECONDS = 60
TOKEN_TTL_SECONDS = 3600
DEFAULT_HOST = "127.0.0.1"
ALLOWED_RESPONSE_TYPES = ["code"]
ALLOWED_GRANT_TYPES = ["authorization_code"]
ALLOWED_CHALLENGE_METHODS = ["S256"]
ADVERTISED_SCOPES = "openid profile email"

# The loopback shapes RFC 8252 §7.3 requires a public client to be able to use
# for a dynamically chosen port. The host spellings are exact: no userinfo, no
# query, no fragment, no wildcard host.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# The authorization-request fields a login POST has to carry back unchanged;
# the form is the same request plus credentials.
CARRIED_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope")

TEST_OP_BANNER = "TEST OP — not an identity provider: users, codes and tokens are fixtures"

class test_op_error(Exception):
    """A request the spec says to refuse, carrying the OAuth error code."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

def b64url(data: bytes) -> str:
    """base64url without padding, the encoding PKCE S256 mandates (RFC 7636 §4.2)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def s256_challenge(verifier: str) -> str:
    """The S256 challenge for a verifier: base64url(sha256(ASCII(verifier)))."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())

def loopback_redirect_allowed(url: str, path: str) -> bool:
    """Accept the registered path on any loopback port (RFC 8252 §7.3).

    A native app cannot promise a fixed port, so the registry stores
    ``http://127.0.0.1/callback`` and this accepts ``http://127.0.0.1:53144/callback``
    while still refusing userinfo, a query, a fragment and any other host.
    """

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False

    if parts.scheme != "http":
        return False

    if (parts.username is not None) or (parts.password is not None):
        return False

    if parts.query or parts.fragment:
        return False

    try:
        hostname = parts.hostname
    except ValueError:
        return False

    if (hostname or "").lower() not in LOOPBACK_HOSTS:
        return False

    return parts.path == path

def redirect_uri_allowed(redirect_uri: str, registered: Sequence[str], loopback_redirect: bool) -> bool:
    """Is this redirect_uri one the client registry vouches for?

    Exact string match always; additionally, when the client is flagged
    ``loopback_redirect``, the loopback shape of every registered loopback URI
    (same host spelling and path, any port).
    """

    if not redirect_uri:
        return False

    if redirect_uri in registered:
        return True

    if not loopback_redirect:
        return False

    for candidate in registered:
        try:
            parts = urllib.parse.urlsplit(candidate)
        except ValueError:
            continue

        if parts.scheme != "http":
            continue

        if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
            continue

        if loopback_redirect_allowed(redirect_uri, parts.path):
            return True

    return False

class token_store:
    """In-memory single-use codes and bearer tokens.

    A fixture has no database; the point of keeping these in the process is that
    nothing issued by a test run can outlive the test run.
    """

    def __init__(self, code_ttl: int = CODE_TTL_SECONDS, token_ttl: int = TOKEN_TTL_SECONDS) -> None:
        self.code_ttl = code_ttl
        self.token_ttl = token_ttl
        self._codes: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()

    def issue_code(self, record: dict) -> str:
        code = secrets.token_urlsafe(32)

        with self._lock:
            self._sweep()
            self._codes[code] = dict(record, expires_at = time.time() + self.code_ttl)

        return code

    def redeem_code(self, code: str) -> dict | None:
        """Consume a code. A code is single-use whether or not the redemption succeeds."""

        with self._lock:
            record = self._codes.pop(code, None)

        if record is None:
            return None

        if record["expires_at"] < time.time():
            return None

        return record

    def issue_token(self, subject: dict, scope: str) -> str:
        token = secrets.token_urlsafe(32)

        with self._lock:
            self._sweep()
            self._tokens[token] = {"subject": subject, "scope": scope, "expires_at": time.time() + self.token_ttl}

        return token

    def lookup_token(self, token: str) -> dict | None:
        with self._lock:
            record = self._tokens.get(token)

        if record is None:
            return None

        if record["expires_at"] < time.time():
            return None

        return record

    def _sweep(self) -> None:
        now = time.time()

        for key in [k for k, v in self._codes.items() if v["expires_at"] < now]:
            del self._codes[key]

        for key in [k for k, v in self._tokens.items() if v["expires_at"] < now]:
            del self._tokens[key]

def load_users_file(users_file: Path | Mapping) -> dict:
    """Read the client registry + users document.

    A mapping is accepted as-is so a test can hand over a literal; a path is
    read as JSON. Problems name the field, never the value, so a bad file can be
    fixed without the error message leaking a password into a log.
    """

    if isinstance(users_file, Mapping):
        data: Any = dict(users_file)
    else:
        try:
            data = json.loads(Path(users_file).read_text(encoding = "utf-8"))
        except OSError as error:
            raise SystemExit(f"error: cannot read users file: {error}") from error
        except json.JSONDecodeError as error:
            raise SystemExit(f"error: users file is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise SystemExit("error: users file must be a JSON object")

    clients = data.get("clients")
    users = data.get("users")

    if not isinstance(clients, dict) or not clients:
        raise SystemExit("error: users file needs a non-empty 'clients' object")

    if not isinstance(users, list) or not users:
        raise SystemExit("error: users file needs a non-empty 'users' array")

    for client_id, client in clients.items():
        validate_client(client_id, client)

    for user in users:
        validate_user(user)

    return data

def validate_client(client_id: Any, client: Any) -> None:
    """Refuse a registry entry the endpoint could not honour unambiguously."""

    if not isinstance(client, dict):
        raise SystemExit(f"error: client {client_id!r} must be an object")

    uris = client.get("redirect_uris")

    if not isinstance(uris, list):
        raise SystemExit(f"error: client {client_id!r} needs a 'redirect_uris' array of strings")

    for uri in uris:
        if not isinstance(uri, str):
            raise SystemExit(f"error: client {client_id!r} needs a 'redirect_uris' array of strings")

    secret = client.get("secret")

    if (secret is not None) and not isinstance(secret, str):
        raise SystemExit(f"error: client {client_id!r} 'secret' must be a string or null")

def validate_user(user: Any) -> None:
    """Every user needs the three fields a login and a token claim depend on."""

    if not isinstance(user, dict):
        raise SystemExit("error: every user must be an object")

    for field in ("sub", "username", "password"):
        if not isinstance(user.get(field), str) or not user[field]:
            raise SystemExit(f"error: a user is missing the string field {field!r}")

def html_escape(text: str) -> str:
    """Escape a value for an HTML attribute or text node."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def error_page(error: str, description: str) -> str:
    """The browser-facing half of the OAuth error shape."""
    return "<!doctype html><html><head><meta charset='utf-8'><title>TEST OP — request refused</title></head>" "<body><h1>Request refused</h1>" f"<p><code>{html_escape(error)}</code>: {html_escape(description)}</p>" "<p>This is a TEST OP fixture, not an identity provider.</p></body></html>"

def login_page(request: dict, usernames: Sequence[str], error: str | None) -> str:
    """The minimal login form: a user picker and a password field.

    The hidden fields carry the authorization request through the POST, so the
    form submission is the *same* request with credentials added — which is what
    makes the one-time ``state`` echo trivially correct.
    """
    hidden_parts = []

    for name in CARRIED_FIELDS:
        if request.get(name):
            hidden_parts.append(f"<input type='hidden' name='{html_escape(name)}' value='{html_escape(str(request[name]))}'>")

    hidden = "".join(hidden_parts)
    options = "".join(f"<option value='{html_escape(name)}'>{html_escape(name)}</option>" for name in usernames)
    banner = "<p class='error'><strong>Login failed.</strong></p>" if error else ""
    detail = f"<p class='error'>{html_escape(error)}</p>" if error else ""
    return "<!doctype html><html><head><meta charset='utf-8'><title>TEST OP — sign in</title></head><body>" "<h1>TEST OP — not an identity provider</h1>" "<p>This login is a fixture. Any password it accepts is in a local JSON file.</p>" f"{banner}{detail}" f"<form method='post' action='/authorize'>{hidden}" f"<p><label for='username'>User</label> <select id='username' name='username'>{options}</select></p>" "<p><label for='password'>Password</label> <input id='password' type='password' name='password' autocomplete='off'></p>" "<p><button type='submit'>Sign in</button></p>" "</form></body></html>"

class TestOp:
    """A running TEST OP: ``TestOp.start(users_file)`` then ``op.base_url``.

    The server is a stdlib ``ThreadingHTTPServer`` on a daemon thread, so a test
    that forgets ``close()`` still exits — it just leaks a socket until then.
    """

    verbose = False

    def __init__(self, server: ThreadingHTTPServer, clients: dict, users: list, store: token_store) -> None:
        self._server = server
        self._thread = threading.Thread(target = server.serve_forever, name = "test-op", daemon = True)
        self.clients = clients
        self.users = users
        self.users_by_sub = {user["sub"]: user for user in users}
        self.store = store
        self.host = server.server_address[0]
        self.port = server.server_address[1]

    @classmethod
    def start(cls, users_file: Path | Mapping, host: str = DEFAULT_HOST, port: int = 0, code_ttl: int = CODE_TTL_SECONDS, token_ttl: int = TOKEN_TTL_SECONDS) -> TestOp:
        """Start the fixture and return it once it is listening.

        ``code_ttl`` / ``token_ttl`` exist so a test can prove the expiry
        behaviour without sleeping for a minute.
        """
        data = load_users_file(users_file)
        store = token_store(code_ttl = code_ttl, token_ttl = token_ttl)
        handler = type("_BoundHandler", (op_handler,), {"op": None})
        server = ThreadingHTTPServer((host, port), handler)
        op = cls(server, data["clients"], data["users"], store)
        handler.op = op
        op._thread.start()
        return op

    @property
    def base_url(self) -> str:
        """The URL a consumer's client should be configured with."""

        if ":" in self.host:
            return f"http://[{self.host}]:{self.port}"

        return f"http://{self.host}:{self.port}"

    def usernames(self) -> list[str]:
        """The user picker's options."""
        return [user["username"] for user in self.users]

    def discovery(self) -> dict:
        """The OpenID discovery document (the subset a code-flow client needs)."""
        return {"issuer": self.base_url, "authorization_endpoint": f"{self.base_url}/authorize", "token_endpoint": f"{self.base_url}/token", "userinfo_endpoint": f"{self.base_url}/userinfo", "response_types_supported": list(ALLOWED_RESPONSE_TYPES), "grant_types_supported": list(ALLOWED_GRANT_TYPES), "code_challenge_methods_supported": list(ALLOWED_CHALLENGE_METHODS)}

    def authenticate(self, username: str, password: str) -> dict | None:
        """Resolve a username/password pair, constant-time in both fields.

        Every known user is compared so the time taken does not depend on which
        user was tried; an unknown username still costs one full comparison.
        """
        matched = None

        for user in self.users:
            username_ok = hmac.compare_digest(user["username"], username)
            password_ok = hmac.compare_digest(user["password"], password)

            if username_ok and password_ok:
                matched = user

        return matched

    def validate_authorize(self, params: Mapping[str, Sequence[str]]) -> dict:
        """Validate an authorization request, raising ``test_op_error`` on refusal.

        The order matters: everything cheap and client-independent first, then
        the client and its redirect_uri — an error page may never redirect to a
        URI the registry has not vouched for (RFC 6749 §4.1.2.1).
        """
        single = {key: values[-1] for key, values in params.items() if values}
        response_type = single.get("response_type", "")
        client_id = single.get("client_id", "")
        redirect_uri = single.get("redirect_uri", "")
        state = single.get("state", "")
        challenge = single.get("code_challenge", "")
        method = single.get("code_challenge_method", "")

        if response_type != "code":
            raise test_op_error("unsupported_response_type", "only response_type=code is supported")

        if not client_id:
            raise test_op_error("invalid_request", "client_id is required")

        client = self.clients.get(client_id)

        if client is None:
            raise test_op_error("invalid_request", "unknown client_id")

        if not redirect_uri_allowed(redirect_uri, client.get("redirect_uris") or [], bool(client.get("loopback_redirect"))):
            raise test_op_error("invalid_request", "redirect_uri is not registered for this client")

        if not state:
            raise test_op_error("invalid_request", "state is required")

        if not challenge:
            raise test_op_error("invalid_request", "code_challenge is required (PKCE S256)")

        # `plain` is a valid PKCE method that this fixture deliberately does not
        # implement: the family client is S256-only and a fixture should refuse
        # what the real consumer cannot do.
        if method != "S256":
            raise test_op_error("invalid_request", "code_challenge_method must be S256")

        scope = single.get("scope") or ADVERTISED_SCOPES
        return {"response_type": response_type, "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": method, "scope": scope}

    def token_binding_matches(self, record: dict, client_id: str, redirect_uri: str) -> bool:
        """The code must be redeemed by the client it was issued to, same redirect."""
        return (record["client_id"] == client_id) and (record["redirect_uri"] == redirect_uri)

    def close(self) -> None:
        """Stop the thread and release the socket."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout = 5)

    def __enter__(self) -> TestOp:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

class op_handler(BaseHTTPRequestHandler):
    """The five endpoints, on one class, because the server has no routing table."""

    server_version = "TestOp/1"
    sys_version = ""

    # Set by TestOp.start on the subclass it creates for one server.
    op: Any = None

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the access log unless the CLI asked for it.

        A fixture runs inside test output; its access log is noise. The CLI
        enables it explicitly with ``--verbose``.
        """

        if getattr(self.op, "verbose", False):
            super().log_message(format, *args)

    def do_GET(self) -> None:
        """Route a GET."""
        self._dispatch("GET")

    def do_POST(self) -> None:
        """Route a POST."""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except test_op_error as error:
            # Every refusal is data, not a traceback: an uncaught exception here
            # makes http.server drop the connection, and a client then sees a
            # broken socket instead of the OAuth error it has to assert on.
            self.send_oauth_error(error)

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        route = (method, parsed.path)

        if route == ("GET", "/.well-known/openid-configuration"):
            self.send_json(200, self.op.discovery())
        elif route == ("GET", "/authorize"):
            self._authorize_form(urllib.parse.parse_qs(parsed.query, keep_blank_values = True))
        elif route == ("POST", "/authorize"):
            self._authorize_submit(self.read_form())
        elif route == ("POST", "/token"):
            self._token(self.read_form())
        elif route == ("GET", "/userinfo"):
            self._userinfo()
        elif route == ("GET", "/healthz"):
            self.send_json(200, {"status": "ok", "test_op": True})
        else:
            self.send_oauth_error(test_op_error("not_found", f"no route for {method} {parsed.path}", 404))

    def read_form(self) -> dict[str, str]:
        """Read an ``application/x-www-form-urlencoded`` body into a flat dict."""

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise test_op_error("invalid_request", "content-length is not a number") from None

        raw = self.rfile.read(length) if length > 0 else b""

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise test_op_error("invalid_request", "request body must be UTF-8") from None

        return {key: values[-1] for key, values in urllib.parse.parse_qs(text, keep_blank_values = True).items()}

    def send_json(self, status: int, body: dict) -> None:
        """Write a JSON answer with the no-store headers RFC 6749 §5.1 requires."""
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, status: int, html: str) -> None:
        """Write the login form or an error page."""
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_redirect(self, location: str) -> None:
        """Send the one 302 the flow has."""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_oauth_error(self, error: test_op_error) -> None:
        """Render a refusal for whoever is reading: a browser or a client library."""
        is_browser = (self.command == "GET") and (urllib.parse.urlsplit(self.path).path in ("/authorize", "/userinfo"))

        if is_browser:
            self.send_html(error.status, error_page(error.error, error.description))
        else:
            self.send_json(error.status, {"error": error.error, "error_description": error.description})

    def _userinfo(self) -> None:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")

        if (scheme.lower() != "bearer") or not token.strip():
            raise test_op_error("invalid_token", "a Bearer access token is required", 401)

        record = self.op.store.lookup_token(token.strip())

        if record is None:
            raise test_op_error("invalid_token", "the access token is unknown or expired", 401)

        subject = record["subject"]
        self.send_json(200, {"sub": subject["sub"], "username": subject["username"], "email": subject.get("email", ""), "roles": list(subject.get("roles", [])), "preferred_username": subject["username"]})

    def _authorize_form(self, params: dict[str, list[str]]) -> None:
        try:
            request = self.op.validate_authorize(params)
        except test_op_error as error:
            self.send_oauth_error(error)
            return

        self.send_html(200, login_page(request, self.op.usernames(), None))

    def _authorize_submit(self, form: dict[str, str]) -> None:
        request = {key: [value] for key, value in form.items() if key not in ("username", "password")}

        try:
            validated = self.op.validate_authorize(request)
        except test_op_error as error:
            self.send_oauth_error(error)
            return

        username = form.get("username", "")
        password = form.get("password", "")
        subject = self.op.authenticate(username, password)

        if subject is None:
            # A failed login is NOT a redirect: the browser stays on the form
            # with an error, exactly like a real authorization server.
            self.send_html(200, login_page(validated, self.op.usernames(), "Invalid username or password."))
            return

        code = self.op.store.issue_code({"client_id": validated["client_id"], "redirect_uri": validated["redirect_uri"], "code_challenge": validated["code_challenge"], "scope": validated["scope"], "sub": subject["sub"]})
        separator = "&" if urllib.parse.urlsplit(validated["redirect_uri"]).query else "?"
        query = urllib.parse.urlencode({"code": code, "state": validated["state"]})
        self.send_redirect(f"{validated['redirect_uri']}{separator}{query}")

    def _token(self, form: dict[str, str]) -> None:
        grant_type = form.get("grant_type", "")

        if grant_type != "authorization_code":
            raise test_op_error("unsupported_grant_type", "only grant_type=authorization_code is supported")

        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        client_id = form.get("client_id", "")
        verifier = form.get("code_verifier", "")

        if not code or not redirect_uri or not client_id:
            raise test_op_error("invalid_request", "code, redirect_uri and client_id are required")

        client = self.op.clients.get(client_id)

        if client is None:
            # Do not disclose whether the client exists: one message for every
            # credential-shaped failure (RFC 6749 §5.2).
            raise test_op_error("invalid_client", "client authentication failed", 401)

        expected_secret = client.get("secret")

        if (expected_secret is not None) and not hmac.compare_digest(str(form.get("client_secret") or ""), expected_secret):
            raise test_op_error("invalid_client", "client authentication failed", 401)

        record = self.op.store.redeem_code(code)

        if record is None:
            raise test_op_error("invalid_grant", "the authorization code is unknown, expired or already used")

        if not self.op.token_binding_matches(record, client_id, redirect_uri):
            raise test_op_error("invalid_grant", "the authorization code does not belong to this client or redirect_uri")

        if not verifier:
            raise test_op_error("invalid_request", "code_verifier is required (PKCE S256)")

        if not hmac.compare_digest(s256_challenge(verifier), record["code_challenge"]):
            raise test_op_error("invalid_grant", "code_verifier does not match the code_challenge")

        subject = self.op.users_by_sub.get(record["sub"])

        if subject is None:
            raise test_op_error("invalid_grant", "the user behind this code no longer exists")

        access_token = self.op.store.issue_token(subject, record["scope"])
        self.send_json(200, {"access_token": access_token, "token_type": "Bearer", "expires_in": self.op.store.token_ttl, "scope": record["scope"]})

def is_loopback_host(host: str) -> bool:
    """Is this bind address confined to this machine?"""
    return (host in LOOPBACK_HOSTS) or host.startswith("127.")

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: refuse without ``--test-op``, then serve until Ctrl-C."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools oauth-test-op", description = "Run the TEST OAuth 2.0 provider fixture (never a real identity plane).")
    parser.add_argument("--test-op", action = "store_true", help = "required acknowledgement that this is a fixture, not an identity provider")
    parser.add_argument("--users-file", required = True, type = Path, help = "JSON file with the client registry and users (keep it 0600)")
    parser.add_argument("--host", default = DEFAULT_HOST, help = "bind address (default 127.0.0.1; anything non-loopback is a loud warning)")
    parser.add_argument("--port", type = int, default = 0, help = "bind port (0 picks a free one and prints it)")
    parser.add_argument("--code-ttl", type = int, default = CODE_TTL_SECONDS, help = f"authorization-code lifetime in seconds (default {CODE_TTL_SECONDS})")
    parser.add_argument("--token-ttl", type = int, default = TOKEN_TTL_SECONDS, help = f"access-token lifetime in seconds (default {TOKEN_TTL_SECONDS})")
    parser.add_argument("--verbose", action = "store_true", help = "log every request to stderr")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.test_op:
        print("error: this is a TEST fixture; pass --test-op to confirm you know that", file = sys.stderr)
        return 2

    print(TEST_OP_BANNER, file = sys.stderr, flush = True)

    if not is_loopback_host(args.host):
        print(f"WARNING: binding {args.host} exposes a TEST OP (password file included) beyond this machine.", file = sys.stderr, flush = True)
        print("WARNING: it is a fixture with a plaintext user file. Anyone who reaches it can sign in as anyone.", file = sys.stderr, flush = True)

    try:
        op = TestOp.start(args.users_file, host = args.host, port = args.port, code_ttl = args.code_ttl, token_ttl = args.token_ttl)
    except OSError as error:
        print(f"error: cannot bind {args.host}:{args.port}: {error}", file = sys.stderr)
        return 1

    TestOp.verbose = args.verbose

    try:
        print(f"READY {op.base_url}", flush = True)
        print("Ctrl-C to stop.", file = sys.stderr, flush = True)

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("stopping", file = sys.stderr)
    finally:
        op.close()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
