"""Credential-line heuristics for the build gate.

Owns the regex catalogue (assignment keys, flags, embedded-credential URLs,
provider token shapes, placeholders, RFC 5737 doc addresses) and the line
classifier. Split out of gate.py (2026-10-31).
"""

from __future__ import annotations
import re
from typing import List, Tuple

# ── Credential scan heuristics ────────────────────────────────────────────────

# Broad "report" pattern: any credential-looking token in a line. The `pass` /
# `pwd` alternation is guarded by lookarounds so prose like "bypass" or "the
# test will pass" is not flagged, while `SSH_PASS` / `--target-pass` are.
_CRED_PATTERN = re.compile(r"password|passwd|passphrase|secret|token|credential|api[_-]?key|" r"(?<![a-z0-9])(pass|pwd)(?![a-z0-9])|" r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY", re.IGNORECASE)

# Whitelisted placeholder markers (obvious dummy values + RFC 5737 doc IPs).
# The "your…" convention is anchored at both ends — it must start with ``your``
# plus a separator AND end with the credential noun — so real-world placeholders
# (``your-jwt-secret``, ``your_app_token``) stay whitelisted while a committed
# literal such as ``TOKEN="yoursecret-real-value"`` is not swallowed by an
# unanchored alternative.
_PLACEHOLDER_PATTERN = re.compile(r"CHANGE[_ ]?ME|<your[-_ ]?password>|<password>|test[-_ ]?password|" r"<your[-_ ]?(?:token|secret|api[-_ ]?key)>|<(?:token|secret|api[-_ ]?key)>|" r"(?:your|optional|dummy|sample|fake|placeholder|test)[-_ ][\w-]*" r"(?:password|secret|token|api[-_ ]?key)\b|" r"sk-xxx|xxxxx|xxxx|xxx|example|placeholder|redacted|" r"\b(?:REPLACE_ME|changethis|change_this|unspecified|bearer|dummy|fake)\b|" r"192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+", re.IGNORECASE)

# RFC 5737 documentation addresses — AGENTS §10.1.2 requires them in examples,
# so a credential URL pointing at one cannot be a live credential.
_DOC_ADDRESS_PATTERN = re.compile(r"192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+")

# Reading the value from env/config — no literal secret lives in the tree.
_ENV_REF_PATTERN = re.compile(r"os\.environ|getenv\s*\(|environ\s*\[|\$\{|process\.env", re.IGNORECASE)

# A PEM private-key header (always suspicious; never a placeholder by default).
_PRIVATE_KEY_PATTERN = re.compile(r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY")

# An assignment whose left-hand key contains a credential word, e.g.
# ``SSH_PASS="..."`` / ``password = value`` / ``api_key: value``.
_ASSIGN_KEY_PATTERN = re.compile(r"(?:^|[\s(\"'])" r"[A-Za-z0-9_.-]{0,256}(?:password|passwd|passphrase|secret|token|credential|api[_-]?key|pass|pwd)" r"[A-Za-z0-9_.-]{0,256}[\"']?\s*[=:]\s*", re.IGNORECASE)

# A flag carrying a credential value, e.g. ``--target-pass s3cr3t-value`` or
# ``--api-key=realvalue``.
# Two things keep this linear *and* complete: the optional prefix must start
# with an alphanumeric (so a long run of dashes fails at the first character
# instead of backtracking), and it is optional so a flag whose name IS the
# credential word (``--token``, ``--api-key``) still matches.
_FLAG_PATTERN = re.compile(r"--?(?:[a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?" r"(?:password|passwd|passphrase|secret|token|api[_-]?key|pass|pwd)" r"[a-zA-Z0-9_-]{0,63}(?:\s|=)\s*", re.IGNORECASE)

# A credential embedded in a URL's userinfo — the ``.git/config`` remote form
# ``https://oauth2:gho_…@github.com/org/repo.git``. Carries no credential WORD,
# so the broad pattern above never sees it; the userinfo IS the secret.
# scheme:// (bounded: schemes are short,
# and an unbounded run backtracks on
# long alphanumeric lines)
# user
# secret (the URL password / token)
# @host
_EMBEDDED_CREDENTIAL_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]{0,32}://" r"[^/\s:@]+:" r"(?P<secret>[^/\s@]+)" r"@(?P<host>[^\s/]+)", re.IGNORECASE)

# Provider-issued token shapes. These are high signal on their own — no
# credential WORD and no particular key name required — because a scanner that
# only looked at key names would miss ``const k = "AKIA…"``.
# GitHub
# GitLab
# OpenAI-style
# Slack
# AWS access key id
# Google API key
# Feishu app id
_PROVIDER_TOKEN_PATTERN = re.compile(r"\b(?:" r"gh[pousr]_[A-Za-z0-9]{16,}" r"|github_pat_[A-Za-z0-9_]{20,}" r"|glpat-[A-Za-z0-9_-]{16,}" r"|sk-[A-Za-z0-9_-]{16,}" r"|xox[baprs]-[A-Za-z0-9-]{10,}" r"|AKIA[0-9A-Z]{16}" r"|AIza[0-9A-Za-z_-]{35}" r"|cli_[a-z0-9]{16,}" r")\b")

#: Key segments that name a credential when they are the LAST segment.
_CRED_KEY_WORDS = frozenset({"password", "passwd", "passphrase", "pwd", "pass", "secret", "token", "apikey", "credential", "credentials"})

#: ``…key`` only counts when a qualifier segment precedes it (``api_key``,
#: ``secret_key``, …) — a bare ``key = …`` is far too common to be a signal.
_KEY_QUALIFIERS = frozenset({"api", "access", "secret", "private", "public", "ssh", "auth", "app", "signing", "encryption", "client", "server", "master", "session"})

#: Split key names on punctuation and camelCase boundaries: ``jsonwebtoken``
#: stays one segment (not a credential name) while ``JSONWebToken`` /
#: ``jwtSecret`` / ``MP_JWT_SECRET`` do segment.
_KEY_SEPARATOR_PATTERN = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: An unquoted literal value: one token, no whitespace and no structural
#: punctuation (quotes, parens, brackets, commas). Punctuation like ``!``,
#: ``&``, ``%`` or ``#`` is *allowed* — ``DB_PASSWORD=Str0ng!Passw0rd!2026`` is
#: exactly the shape this gate exists for.
_LITERAL_VALUE_PATTERN = re.compile(r"^[^\s\"'`,;(){}\[\]<>]{4,}$")

#: A quoted literal may carry arbitrary punctuation (``"P@ssw0rd!2026#Prod"``)
#: but not whitespace: text with spaces is prose or a sliced expression, not a
#: committed secret.
_QUOTED_LITERAL_PATTERN = re.compile(r"^\S{4,}$")

#: A quoted value shorter than this is a config keyword or a flag, not a
#: secret (``TOKEN = "x"`` in a declaration is not a credential).
_MIN_LITERAL_LEN = 6

#: Bare words that are control flow / config keywords, never secrets.
# web-platform option values (``credentials: "same-origin"``)
_CONTROL_WORDS = frozenset({"write", "read", "none", "null", "true", "false", "nil", "undefined", "deny", "allow", "required", "optional", "inherit", "default", "same-origin", "same-site", "include", "omit", "cors", "no-cors", "navigate"})

# Directories never walked by the workspace credential sweep.
#: Directory names excluded from the sweep by design (build artefacts, VCS and
#: dependency trees). ``build`` is deliberately NOT here: it is a legitimate
#: source directory name (this package's own ``celestia_devtools/build``), and a
#: name-based prune would make the gate blind to it. Excluded names are reported
#: by the CLI so a green run never reads as "everything was scanned".
_SWEEP_SKIP_DIRS = frozenset({".git", "node_modules", "target", "dist", "vendor", "coverage", ".venv", "venv", "__pycache__", ".next", ".nuxt", ".pytest_cache", ".ruff_cache", ".mypy_cache", "site-packages"})

# Files above this size are not credential-scanned (lockfiles, bundles, …).
_MAX_SWEEP_BYTES = 2 * 1024 * 1024

#: UTF-16 byte-order marks — NUL bytes there mean text, not binary.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

def _first_token(rest: str) -> str | None:
    """Extract the leading value token from the text after an assignment."""
    rest = rest.strip()

    if not rest:
        return None

    if rest[0] in "\"'":
        end = rest.find(rest[0], 1)

        if end > 0:
            return rest[1:end]

        return rest

    return re.split(r"[\s;#]+", rest, maxsplit = 1)[0] or None

def _extract_assignment(line: str) -> Tuple[str | None, str | None, bool]:
    """Return ``(key, value, key_was_quoted)``, else ``(None, None, False)``.

    ``key_was_quoted`` marks the JSON/JS form (``"password": "…"``) — the shape
    every i18n label file uses, where a translated string is not a secret.
    """
    match = _ASSIGN_KEY_PATTERN.search(line)

    if match:
        raw = match.group(0)
        key_part = re.split(r"[=:]", raw, maxsplit = 1)[0]
        quoted_key = key_part.rstrip().endswith(("\"", "'"))
        key = key_part.strip().strip("\"'").lstrip("-")
        return key, _first_token(line[match.end():]), quoted_key

    match = _FLAG_PATTERN.search(line)

    if match:
        raw = match.group(0).strip()
        key = re.split(r"[\s=]", raw, maxsplit = 1)[0].strip("-")
        return key, _first_token(line[match.end():]), False

    return None, None, False

def _key_segments(key: str) -> List[str]:
    """Split a key name into lowercase word segments."""
    segments: List[str] = []

    for chunk in _KEY_SEPARATOR_PATTERN.split(key):
        if chunk:
            segments.extend(_CAMEL_BOUNDARY_PATTERN.split(chunk))

    return [segment.lower() for segment in segments]

def is_credential_key(key: str | None) -> bool:
    """True when *key* names a credential (``MP_JWT_SECRET``, ``api_key``, …).

    Segmentation is what separates a key name from a *value* that merely
    contains the word: ``jsonwebtoken = "^10"`` is a dependency, not a secret.
    """
    segments = _key_segments(key or "")

    if not segments:
        return False

    if segments[-1] in _CRED_KEY_WORDS:
        return True

    if segments[-1] == "key":
        return (len(segments) > 1) and (segments[-2] in _KEY_QUALIFIERS)

    return False

def _is_quoted_value(line: str, value: str) -> bool:
    return any(('%s%s%s' % (quote, value, quote)) in line for quote in ('"', "'", "`"))

def looks_like_literal(line: str, value: str | None) -> bool:
    """True when *value* is a committed literal rather than a code reference.

    A credential is a *value*: ``token = path.strip()``, ``this.token = token``
    and ``id-token: write`` are identifiers or control words. Treating those as
    secrets is what makes a scanner ignorable, so an unquoted value must carry a
    secret-like signal (a digit, a provider token shape, or a long mixed blob).
    """

    if not value or (value.lower() in _CONTROL_WORDS):
        return False

    quoted = _is_quoted_value(line, value)

    if quoted:
        if not _QUOTED_LITERAL_PATTERN.match(value):
            return False

        return (len(value) >= _MIN_LITERAL_LEN) or bool(_PROVIDER_TOKEN_PATTERN.search(value))

    if not _LITERAL_VALUE_PATTERN.match(value):
        return False

    if _PROVIDER_TOKEN_PATTERN.search(value):
        return True

    has_digit = any(char.isdigit() for char in value)
    has_alpha = any(char.isalpha() for char in value)

    if has_digit and has_alpha:
        return True

    return (len(value) >= 24) and has_alpha and (len(set(value)) >= 8)

def _looks_like_secret_value(value: str) -> bool:
    """Strict shape test for a credential that sits behind a JSON-quoted key.

    A quoted key is the JSON/i18n shape, where a plain translated string
    (``"password": "Passwort"``) must never be fatal. Only a provider-token
    shape or a value mixing digits and letters qualifies; a long alphabetic
    token without digits is reported, not failed — noise in hundreds of locale
    files would make the gate ignorable.
    """

    if not value:
        return False

    if _PROVIDER_TOKEN_PATTERN.search(value):
        return True

    return any(char.isdigit() for char in value) and any(char.isalpha() for char in value)

def _value_is_excused(value: str) -> bool:
    """A value that is a placeholder or comes from the environment."""
    return bool(_PLACEHOLDER_PATTERN.search(value) or _ENV_REF_PATTERN.search(value))

def classify_credential_line(line: str) -> str:
    """Classify one source line for the credential scan.

    Returns ``"clean"`` (no credential token), ``"report"`` (a hit that is
    whitelisted, env-sourced, or merely a code reference — reported but not
    fatal), or ``"violation"`` (a literal non-placeholder secret — fatal).

    Three shapes carry a secret without ever naming one, and all three are
    matched here: a provider token (``gho_…`` / ``github_pat_…`` / ``sk-…`` /
    ``AKIA…``), a URL with credentials in its userinfo (the ``.git/config``
    remote form ``https://oauth2:gho_…@github.com/org/repo.git``), and a
    credential-keyed assignment whose value is a literal.
    """
    key, value, quoted_key = _extract_assignment(line)
    embedded = _EMBEDDED_CREDENTIAL_URL_PATTERN.search(line)
    provider = _PROVIDER_TOKEN_PATTERN.search(line)

    if not (_CRED_PATTERN.search(line) or embedded or provider):
        return "clean"

    if _PRIVATE_KEY_PATTERN.search(line):
        return "report" if _PLACEHOLDER_PATTERN.search(line) else "violation"

    # Provider tokens are literals by construction; placeholders and env refs
    # are judged on the token itself, so a doc IP elsewhere on the line cannot
    # excuse a real one.
    if (provider is not None) and not _value_is_excused(provider.group(0)):
        return "violation"

    if (embedded is not None) and (value is None):
        secret, host = embedded.group("secret"), embedded.group("host")

        if _DOC_ADDRESS_PATTERN.search(host) or _value_is_excused(secret):
            return "report"

        return "violation" if looks_like_literal("", secret) else "report"

    if (value is not None) and is_credential_key(key):
        # Judge the excuse on the VALUE: a comment elsewhere on the line
        # (``# fallback for process.env``) must not demote a real literal.
        if _value_is_excused(value):
            return "report"

        if quoted_key and not _looks_like_secret_value(value):
            # ``"password": "Passwort"`` — an i18n label, not a credential
            return "report"

        return "violation" if looks_like_literal(line, value) else "report"

    return "report"
