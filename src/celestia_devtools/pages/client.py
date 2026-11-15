"""JSON-RPC client for chest's ``pages.*`` surface, plus the publish walk.

Transport. chest's panel RPC is a JSON-RPC 2.0 POST (the plana client's HTTP
lane, which every tier uses): ``POST {apiBase}/rpc?workspace=<uuid>`` with
``Authorization: Bearer <access token>`` and a ``Content-Type`` of
``application/json``; an error reply carries ``error.code``/``error.message`` and
a 401 means the token is gone. This module implements exactly that, so a
submitter does not need a browser.

Auth. The token is a *bearer access token*: pass it in, read it from
``$CELESTIA_PAGES_TOKEN``, or point at a file containing it. :func:`login` is a
convenience for a scratch environment only — production accounts go through
TOTP/OAuth, and nothing here stores a credential.

Publish walk. ``pages.create`` -> ``pages.upload`` -> ``pages.publish``, then the
version-pinned reference ``{page_id}/{content_hash}/{entry}`` that a tab pins.
The walk **verifies the server's content hash against the locally computed one**:
the two must agree, because the tab reference is built from the hash and a
mismatch would silently pin a URL that serves different bytes.
"""

from __future__ import annotations
import base64
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable
from . import contract

#: Where the access token is read from when not passed explicitly.
TOKEN_ENV = "CELESTIA_PAGES_TOKEN"

#: The artifact size a caller is allowed to attempt, measured rather than derived.
#:
#: chest's own budget is 32 MiB ([`contract.MAX_UPLOAD_BYTES`]) and the proxy front
#: caps request bodies at 16 MiB on the demo face, but the *public* faces refuse
#: far earlier: measured 2026-11-16 through `demo.dev.celestia.world`, a 1.2 MB
#: artifact (1.6 MB base64 body) publishes, while 2 MB (2.7 MB body) is answered
#: with nginx's `413 Failed to buffer the request body: length limit exceeded`.
#: The hop that decides this has not been identified (plausibly the FRP edge), and
#: the ruling is to document the measurement instead of chasing it.
#:
#: So the default refuses locally at the measured ceiling — a clear error before
#: streaming megabytes that die at the front door — and `$CELESTIA_PAGES_BODY_CAP`
#: (bytes, request body) lifts it for a deployment whose faces are known to admit
#: more. 32 MiB is what chest would accept, not what a submitter can reach.
MEASURED_REQUEST_BODY_CAP = 1600 * 1024
HTTP_BODY_CAP = int(os.environ.get("CELESTIA_PAGES_BODY_CAP", str(MEASURED_REQUEST_BODY_CAP)))
PRACTICAL_ARTIFACT_CAP = min(contract.MAX_UPLOAD_BYTES, (HTTP_BODY_CAP * 3 // 4) - (64 * 1024))

def artifact_cap_label() -> str:
    """One phrase for the refusal message: what the number is and where it binds.

    Deliberately says *largest measured pass*, not "ceiling": the artifact boundary
    sits between the 1.2 MB that publishes and the 2 MB that returns a 413, and the
    binding hop is the public front door (see ``pages/README.md``'s three-layer
    note), not chest's internal budget.
    """
    budget = "the 32 MiB chest budget" if PRACTICAL_ARTIFACT_CAP >= contract.MAX_UPLOAD_BYTES else "the public front door"
    override = "default" if HTTP_BODY_CAP == MEASURED_REQUEST_BODY_CAP else f"$CELESTIA_PAGES_BODY_CAP={HTTP_BODY_CAP}"
    return f"{override}; binds at {budget}"

class pages_error(Exception):
    """A refused RPC: the server's JSON-RPC error, kept intact.

    chest answers panel refusals with an application error code and a message of
    the form ``<Panel.ErrorPath>: <prose>``, so the message is the only place the
    real reason lives (a quota refusal and an oversize refusal share a code).
    """

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method}: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message

class publish_failed(pages_error):
    """The walk stopped after the page row existed — no silent orphan.

    ``pages.create`` runs before ``pages.upload``, so an upload refusal (quota,
    oversize, a front that cuts the body) leaves a ``draft`` row behind. The caller
    needs that id to retry with ``--page-id`` instead of leaking a quota row per
    attempt — which is the expected path for the tairitsu lane while the front's
    body limit is being raised.
    """

    def __init__(self, page_id: str, created: bool, cause: BaseException) -> None:
        super().__init__("pages.publish", getattr(cause, "code", None), str(cause))
        self.page_id = page_id
        self.created = created
        self.cause = cause

def read_token_file(path: pathlib.Path) -> str:
    """Read a bearer token, complaining loudly when the file is not private."""
    mode = path.stat().st_mode & 0o777

    if mode & 0o077:
        print(f"warning: {path} is mode {oct(mode)}; a bearer token file should be 0600", file = sys.stderr)

    return path.read_text(encoding = "utf-8").strip()

def token_from_env() -> str | None:
    """The bearer token, from ``$CELESTIA_PAGES_TOKEN`` or a file it names."""
    raw = os.environ.get(TOKEN_ENV, "").strip()

    if not raw:
        return None

    candidate = pathlib.Path(raw)

    if candidate.is_file():
        return read_token_file(candidate)

    return raw

def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """One HTTP POST. Returns ``(status, body)``; transport faults raise."""
    request = urllib.request.Request(url, data = body, headers = headers, method = "POST")

    try:
        with urllib.request.urlopen(request, timeout = timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()

class pages_client:
    """The seven ``pages.*`` methods over one JSON-RPC endpoint."""

    def __init__(self, base_url: str, token: str | None = None, workspace_id: str | None = None, timeout: float = 60.0, transport: Callable[..., tuple[int, bytes]] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else token_from_env()
        self.workspace_id = workspace_id
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._counter = 0
        self._last_response: dict[str, Any] | None = None

    # ── transport ───────────────────────────────────────────────────────────
    def rpc_path(self) -> str:
        """``/api/rpc`` plus the workspace binding chest's session may need."""
        path = "/api/rpc"

        if self.workspace_id:
            path = f"{path}?workspace={self.workspace_id}"

        return path

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._counter += 1
        payload = {"jsonrpc": "2.0", "id": f"pages-{self._counter}", "method": method, "params": params}
        headers = {"Content-Type": "application/json", "X-Session-Id": f"pages-cli-{uuid.uuid4().hex[:12]}"}

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        status, raw = self._transport(self.base_url + self.rpc_path(), json.dumps(payload).encode("utf-8"), headers, self.timeout)

        if status == 401:
            raise pages_error(method, 401, "unauthorized (the access token is missing or expired)")

        if status >= 400:
            raise pages_error(method, status, raw.decode("utf-8", "replace")[:400])

        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise pages_error(method, None, f"malformed JSON-RPC reply: {error}") from error

        if not isinstance(body, dict):
            raise pages_error(method, None, f"malformed JSON-RPC reply: {type(body).__name__}")

        self._last_response = body
        error = body.get("error")

        if error:
            raise pages_error(method, error.get("code"), str(error.get("message", "unknown rpc error")))

        return body.get("result")

    # ── the seven methods ───────────────────────────────────────────────────
    def create(self, title: str, mode: str, expires_at: str | None = None, rpc_scopes: list[str] | None = None) -> str:
        """Create a ``draft`` row and return its page id."""

        if mode not in contract.MODES:
            raise ValueError(f"mode must be one of {contract.MODES}, got {mode!r}")

        scopes = list(rpc_scopes or [])
        unknown = [scope for scope in scopes if scope not in contract.CLOSED_RPC_SCOPES]

        if unknown:
            # The server clamps silently; refusing here is the whole point of a
            # pre-flight — a page that *thinks* it can call something else is a
            # debugging session nobody enjoys.
            raise ValueError(f"rpc scope(s) outside the closed set {contract.CLOSED_RPC_SCOPES}: {unknown}")

        params: dict[str, Any] = {"title": title, "mode": mode, "rpc_scopes": scopes}

        if expires_at:
            params["expires_at"] = expires_at

        result = self._rpc("pages.create", params) or {}
        # The host answers `{ok, page}` where `page_json` carries `id` — an early
        # draft of this client expected a flat `page_id`, which never arrives.
        page = result.get("page") or {}
        page_id = page.get("id") or result.get("page_id")

        if not page_id:
            raise pages_error("pages.create", None, f"no page id in the reply: {sorted(result)}")

        return str(page_id)

    def upload(self, page_id: str, artifact: contract.validated_artifact) -> dict[str, Any]:
        """Upload one complete version; returns the server's view of it."""
        files = []

        for item in artifact.files:
            encoded = base64.b64encode(item.source.read_bytes()).decode("ascii")
            files.append({"path": item.path, "sha256": item.sha256, "bytes_b64": encoded})

        result = self._rpc("pages.upload", {"page_id": page_id, "files": files}) or {}
        server_hash = result.get("content_hash")

        if not server_hash:
            # A reply without the field would let a mismatch pass silently, and the
            # tab reference is built from that hash.
            raise pages_error("pages.upload", None, f"the upload reply carried no content_hash; refusing to publish an unverified version ({sorted(result)})")

        if server_hash != artifact.content_hash:
            raise pages_error("pages.upload", None, f"content-hash drift: server {server_hash} != local {artifact.content_hash}; " "contract.py's manifest_hash no longer matches the host's")

        return result

    def publish(self, page_id: str) -> dict[str, Any]:
        return self._rpc("pages.publish", {"page_id": page_id}) or {}

    def get(self, page_id: str) -> dict[str, Any]:
        """One page row, flattened: the host nests it under ``page``."""
        result = self._rpc("pages.get", {"page_id": page_id}) or {}
        page = result.get("page")
        return page if isinstance(page, dict) else result

    def list(self) -> dict[str, Any]:
        """``{pages: [...], quota: {pages_used, bytes_used, max_pages, max_bytes}}``."""
        return self._rpc("pages.list", {}) or {}

    def revoke(self, page_id: str) -> dict[str, Any]:
        return self._rpc("pages.revoke", {"page_id": page_id}) or {}

    def delete(self, page_id: str) -> dict[str, Any]:
        return self._rpc("pages.delete", {"page_id": page_id}) or {}

    # ── the walk the tooling uses ───────────────────────────────────────────
    def document_url(self, page_id: str, content_hash: str, entry: str) -> str:
        """The capability URL of one immutable version."""
        return f"{self.base_url}/__pages/{page_id}/{content_hash}/{entry}"

    def publish_artifact(self, artifact: contract.validated_artifact, title: str, mode: str, expires_at: str | None = None, page_id: str | None = None) -> dict[str, Any]:
        """Upload + publish one artifact and return its version-pinned record.

        Passing ``page_id`` re-uploads an existing page (which the server always
        lands back in ``draft`` — publishing confirms one version), so a re-run of
        the tool replaces content instead of leaking a page per attempt.
        """
        created = page_id is None
        target = self.create(title, mode, expires_at = expires_at, rpc_scopes = list(contract.CLOSED_RPC_SCOPES) if mode != "raw-html" else []) if created else str(page_id)

        try:
            uploaded = self.upload(target, artifact)
            published = self.publish(target)
        except Exception as error:
            # Deliberately NOT deleting the row: a partial upload is a state a retry
            # must be able to inspect. Hand the id back instead.
            raise publish_failed(target, created, error) from error

        content_hash = uploaded.get("content_hash") or (published.get("page") or {}).get("content_hash") or published.get("content_hash") or artifact.content_hash
        return {"page_id": target, "created": created, "status": published.get("status") or (published.get("page") or {}).get("status") or "published", "content_hash": content_hash, "entry": artifact.entry, "size_bytes": artifact.size_bytes, "files": len(artifact.files), "tab_ref": f"{target}/{content_hash}/{artifact.entry}", "url": self.document_url(target, content_hash, artifact.entry)}

def pow_solve(seed: str, bits: int, limit: int = 1 << 32) -> int:
    """Mine the counter whose ``sha256(seed || counter)`` has ``bits`` zero bits.

    chest gates the login nonce behind a proof of work (``challenge.rs``:
    ``SHA-256(seed || counter)`` with at least ``bits`` leading zero bits, counter
    as decimal ASCII). At the demo's 12 bits this is a few thousand hashes — under
    a millisecond in Python — so it is mined inline rather than cached.
    """

    if (bits < 0) or (bits > 64):
        raise ValueError(f"impossible proof-of-work difficulty: {bits} bits")

    target = bits

    for counter in range(limit):
        digest = hashlib.sha256(seed.encode("utf-8") + str(counter).encode("utf-8")).digest()
        leading = 0

        for byte in digest:
            if byte == 0:
                leading += 8
                continue

            leading += 8 - byte.bit_length()
            break

        if leading >= target:
            return counter

    raise pages_error("auth.pow", None, f"no counter under {limit} meets {bits} bits")

def _http_json(url: str, payload: dict[str, Any] | None, headers: dict[str, str] | None = None, timeout: float = 30.0, method: str = "POST") -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    merged = {"Content-Type": "application/json"} if payload is not None else {}
    merged.update(headers or {})
    status, raw = _default_transport(url, body, merged, timeout) if payload is not None else _default_get(url, merged, timeout)

    if status >= 400:
        raise pages_error(url.rsplit("/", 1)[-1], status, raw.decode("utf-8", "replace")[:300])

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise pages_error(url, None, f"malformed JSON reply: {error}") from error

def _default_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers = headers, method = "GET")

    try:
        with urllib.request.urlopen(request, timeout = timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()

def login_with_pow(base_url: str, username: str, password: str, timeout: float = 30.0) -> str:
    """Mine the challenge, exchange it for a nonce, log in, return the token.

    The three-step flow is chest's own: ``GET /api/health`` publishes the
    challenge, ``POST /api/auth/nonce`` spends it (single-use, 60 s, IP-bound),
    and ``POST /api/auth/login`` carries the minted nonce in ``X-Nonce``. Neither
    the password nor the token is logged or written anywhere by this function.
    """
    base = base_url.rstrip("/")
    health = _http_json(f"{base}/api/health", None, timeout = timeout)
    challenge = health.get("challenge") or {}

    if (challenge.get("type") != "pow") or not challenge.get("seed"):
        raise pages_error("auth.health", None, f"no pow challenge on {base} (type={challenge.get('type')!r})")

    counter = pow_solve(str(challenge["seed"]), int(challenge.get("bits", 12)))
    nonce_reply = _http_json(f"{base}/api/auth/nonce", {"type": "pow", "seed": challenge["seed"], "counter": counter}, timeout = timeout)
    nonce = nonce_reply.get("nonce")

    if not nonce:
        raise pages_error("auth.nonce", None, "no nonce in the reply")

    tokens = _http_json(f"{base}/api/auth/login", {"username": username, "password": password}, headers = {"X-Nonce": str(nonce)}, timeout = timeout)
    token = tokens.get("access_token")

    if not token:
        raise pages_error("auth.login", None, f"no access_token in the reply: {sorted(tokens)}")

    return str(token)

def credentials_from_file(path: pathlib.Path) -> tuple[str, str]:
    """Read ``DEMO_USERNAME`` / ``DEMO_PASSWORD`` from a ``KEY=value`` file.

    The file lives outside every repo (``0600``); this function never prints,
    logs or returns it anywhere except as the pair the login call needs.
    """
    values: dict[str, str] = {}

    for line in path.read_text(encoding = "utf-8").splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#") or ("=" not in stripped):
            continue

        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()

    missing = [key for key in ("DEMO_USERNAME", "DEMO_PASSWORD") if not values.get(key)]

    if missing:
        raise pages_error("auth.credentials", None, f"{path} is missing {missing}")

    return values["DEMO_USERNAME"], values["DEMO_PASSWORD"]
