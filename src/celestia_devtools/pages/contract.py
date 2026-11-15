"""Mirror of chest's custom-page upload contract.

The authority is chest's ``packages/chest-app/src/custom_pages.rs`` (P1-a). This
module exists because two things must agree *exactly* between submitter and
host, and neither is discoverable from an error message:

1. the **canonical manifest hash** — ``sha256`` over the sorted
   ``(path, sha256)`` pairs, each terminated by a NUL. It names the immutable
   version directory and therefore the version-pinned tab reference
   ``{page_id}/{content_hash}/{entry}``. A different byte layout here means the
   client cannot predict the URL it is about to publish to.
2. the **validation matrix** (path shape, MIME whitelist, caps). The server is
   authoritative and re-checks everything; this side is a pre-flight so a bad
   artifact fails in the builder instead of after a 30 MiB upload.

:func:`validate_files` returns errors in the same order as the server (structure
-> aggregate size -> digests) so a refusal reads the same on both sides, and
:func:`check_contract_drift` re-reads the Rust source to fail loudly if the
server's rules change under us.
"""

from __future__ import annotations
import hashlib
import pathlib
import re
from dataclasses import dataclass

# ── the closed sets (mirrors custom_pages.rs) ────────────────────────────────

MODES: tuple[str, ...] = ("built-hikari", "built-tairitsu", "raw-html")

#: v1 bridge scopes. The server clamps to this set at publish time
#: ([`clamp_rpc_scopes`]); a page that declares anything else silently loses it,
#: so the builder refuses to mint a manifest that asks for more.
CLOSED_RPC_SCOPES: tuple[str, ...] = ("view.fetch",)

MAX_FILES_PER_PAGE = 256
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_REL_PATH_LEN = 512
DEFAULT_ENTRY = "index.html"

#: Extension -> served content type. Closed: an unknown extension is not
#: servable at all, so the builder rejects it before the host has to.
MIME_BY_EXT: dict[str, str] = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "mjs": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "json": "application/json", "map": "application/json", "svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "ico": "image/x-icon", "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "wasm": "application/wasm"}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

class contract_error(ValueError):
    """An artifact the host would refuse, caught before the upload."""

@dataclass(frozen = True)
class artifact_file:
    """One file of an artifact, as the upload RPC wants it."""

    path: str
    sha256: str
    size: int
    source: pathlib.Path

@dataclass(frozen = True)
class validated_artifact:
    files: tuple[artifact_file, ...]
    size_bytes: int
    content_hash: str
    entry: str

    def entry_path(self) -> str:
        """``{page_id}/{content_hash}/{entry}`` once a page id is known."""
        return f"{{page_id}}/{self.content_hash}/{self.entry}"

def mime_for(path: str) -> str | None:
    """Served content type for a relative path, or ``None`` when not servable."""
    _, _, ext = path.rpartition(".")

    if not ext or ("." not in path):
        return None

    return MIME_BY_EXT.get(ext.lower())

def normalize_rel_path(raw: str) -> str:
    """Validate and return a relative artifact path (server rules, verbatim).

    Raises ``ValueError`` with the server's own wording where it is stable.
    """

    if not raw:
        raise contract_error("empty path")

    if len(raw.encode("utf-8")) > MAX_REL_PATH_LEN:
        raise contract_error(f"path longer than {MAX_REL_PATH_LEN} bytes")

    if raw.startswith("/") or raw.startswith("\\"):
        raise contract_error(f"absolute path not allowed: {raw!r}")

    if "\\" in raw:
        raise contract_error(f"backslash not allowed: {raw!r}")

    if raw.startswith("~"):
        raise contract_error(f"home-relative path not allowed: {raw!r}")

    # `char::is_control()` in Rust is C0 + DEL + C1 (Unicode Cc), so this mirror
    # must cover 0x80-0x9F too: a path carrying U+0085 has to be refused on both
    # sides, not only by the server.
    if any((ord(ch) < 0x20) or (0x7F <= ord(ch) <= 0x9F) for ch in raw):
        raise contract_error("control character in path")

    segments: list[str] = []

    for segment in raw.split("/"):
        if not segment:
            raise contract_error(f"empty path segment in {raw!r}")

        if segment in (".", ".."):
            raise contract_error(f"traversal segment in {raw!r}")

        if ":" in segment:
            raise contract_error(f"colon in path segment {segment!r}")

        segments.append(segment)

    return "/".join(segments)

def manifest_hash(pairs: dict[str, str]) -> str:
    """Canonical version hash: ``sha256`` over sorted ``path NUL sha NUL``.

    Ordering by path (not by input order) is what makes the same artifact land in
    the same immutable directory regardless of how it was walked.
    """
    digest = hashlib.sha256()

    for path in sorted(pairs):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(pairs[path].encode("utf-8"))
        digest.update(b"\0")

    return digest.hexdigest()

def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    return digest.hexdigest()

def collect(directory: pathlib.Path, entry: str = DEFAULT_ENTRY) -> tuple[artifact_file, ...]:
    """Walk an artifact directory into upload-ready file records.

    Symlinks are refused: the store must not be able to resolve a path outside
    the artifact, and the server-side matrix refuses them for the same reason.
    """
    root = directory.resolve()

    if not root.is_dir():
        raise contract_error(f"artifact directory not found: {directory}")

    files: list[artifact_file] = []

    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise contract_error(f"symlink not allowed in an artifact: {path}")

        if not path.is_file():
            continue

        rel = path.relative_to(root).as_posix()
        files.append(artifact_file(path = normalize_rel_path(rel), sha256 = sha256_file(path), size = path.stat().st_size, source = path))

    if not any(item.path == entry for item in files):
        raise contract_error(f"entry {entry} is not in the artifact")

    return tuple(files)

def validate_files(files: tuple[artifact_file, ...], entry: str = DEFAULT_ENTRY) -> validated_artifact:
    """Pre-flight the server's matrix and compute the version hash.

    Ordering mirrors ``validate_upload``: structure, then the aggregate budget,
    then per-file digests. The entry check runs after (the server does the same in
    a separate call, ``require_entry_in``).
    """

    if not files:
        raise contract_error("no files")

    if len(files) > MAX_FILES_PER_PAGE:
        raise contract_error(f"too many files: {len(files)} (max {MAX_FILES_PER_PAGE})")

    seen: set[str] = set()

    for item in files:
        normalized = normalize_rel_path(item.path)

        if normalized in seen:
            raise contract_error(f"duplicate path: {normalized}")

        seen.add(normalized)

        if mime_for(normalized) is None:
            raise contract_error(f"file {normalized}: extension not in the page MIME whitelist")

        if not _HEX64.match(item.sha256.lower()):
            raise contract_error(f"file {normalized}: sha256 must be 64 hex characters")

    size_bytes = sum(item.size for item in files)

    if size_bytes > MAX_UPLOAD_BYTES:
        raise contract_error(f"artifact is {size_bytes} bytes (max {MAX_UPLOAD_BYTES})")

    for item in files:
        actual = sha256_file(item.source)

        if actual.lower() != item.sha256.lower():
            raise contract_error(f"file {item.path}: sha256 mismatch (declared != bytes)")

    pairs = {item.path: item.sha256.lower() for item in files}
    normalized_entry = normalize_rel_path(entry)

    if normalized_entry not in pairs:
        raise contract_error(f"entry {normalized_entry} is not in the uploaded file set")

    return validated_artifact(files = files, size_bytes = size_bytes, content_hash = manifest_hash(pairs), entry = normalized_entry)

#: Files whose rules this module mirrors. The drift check reads them verbatim.
_CONTRACT_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = ( ( "packages/chest-app/src/custom_pages.rs", ( "pub const MAX_FILES_PER_PAGE: usize = {max_files};", "pub const MAX_UPLOAD_BYTES: usize = 32 * 1024 * 1024;", "pub const MODES: &[&str] = &[\"built-hikari\", \"built-tairitsu\", \"raw-html\"];", "pub const CLOSED_RPC_SCOPES: &[&str] = &[\"view.fetch\"];", "hasher.update([0u8]);" ) ), )

def check_contract_drift(chest_root: pathlib.Path) -> list[str]:
    """Re-read chest's source and report rules that no longer match this mirror.

    Returns a list of human-readable findings (empty = in sync). This is the same
    "pin the shape, not the value" technique P1-a uses for its quota lock: it
    cannot prove behavioural equality, but it makes silent drift impossible.
    """
    findings: list[str] = []

    for relative, snippets in _CONTRACT_SOURCES:
        source_path = chest_root / relative

        if not source_path.exists():
            findings.append(f"{relative}: missing (cannot verify the contract mirror)")
            continue

        text = source_path.read_text(encoding = "utf-8")

        for snippet in snippets:
            expected = snippet.format(max_files = MAX_FILES_PER_PAGE)

            if expected not in text:
                findings.append(f"{relative}: contract pin not found -> {expected}")

    return findings
