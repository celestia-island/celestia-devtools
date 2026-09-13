#!/usr/bin/env python3
"""Fail a repo that is covered by an unresolved P0 finding.

*Why this exists:* the 2026-09-10 workspace audit listed five P0s whose only
carrier was a markdown report (``_reports/project-group-evaluation-2026-09-10.md``)
that nobody was obliged to open — so "bypassing a P0" cost nothing and three
days of silence followed. This gate makes an unresolved P0 expensive: any repo
named in the ``scope`` of an entry whose ``status`` is still ``"open"`` exits
non-zero and prints the outstanding list.

*Ledger:* a TOML file shipped inside this package (``lint/p0_ledger.toml``),
resolved in this order: ``--ledger`` → ``$CELESTIA_P0_LEDGER`` →
``<repo-root>/p0-ledger.toml`` → the bundled copy.  Fields: ``id`` / ``title`` /
``severity`` / ``scope`` / ``status`` / ``opened_at`` / ``closed_by`` /
``evidence``.  ``scope`` entries are matched with :func:`fnmatch.fnmatchcase`, so
a family can be covered by ``easy-hydro-*`` without listing every repo.

*Semantics:*

- ``--repo NAME`` (repeatable; default: the current checkout's origin name) —
  exit ``1`` when an open entry covers ``NAME``, printing ``id`` + ``title`` +
  the one-line ``evidence`` on stderr.
- **Escape hatch** — the acknowledgment must be *written by someone*: ``--ack
  ID``, ``$CELESTIA_P0_ACK`` (comma/space separated), or a ``P0-ACK: <id>``
  marker in the PR body (``--pr-body`` / ``--pr-body-file`` /
  ``$CELESTIA_P0_PR_BODY``).  An acknowledged entry is echoed to stderr with its
  acknowledgement source and stops blocking — it is never a silent pass, and
  the gate itself never guesses.
- ``--list`` — print the whole ledger grouped by status.

*Fail-closed:* a missing, unreadable, unparsable, or schema-invalid ledger is an
error (exit ``2``), never a pass; so is an empty ledger or an unidentifiable
repo name.  Exit codes: ``0`` pass, ``1`` blocked by an open P0, ``2``
usage/ledger error.

Usage::

    celestia-devtools p0-gate --repo entelecheia
    celestia-devtools p0-gate --repo entelecheia --ack P0-A
    celestia-devtools p0-gate --repo arona --pr-body-file pr-body.md
    celestia-devtools p0-gate --list
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # Python 3.9/3.10
    try:
        import tomli as _toml
    except ImportError:  # pragma: no cover - no TOML parser available
        _toml = None

#: Bundled ledger filename (lives next to this module).
LEDGER_BASENAME = "p0_ledger.toml"
#: Optional per-repo ledger override, looked up at the repo root.
LOCAL_LEDGER_BASENAME = "p0-ledger.toml"

LEDGER_PATH_ENV = "CELESTIA_P0_LEDGER"
ACK_ENV = "CELESTIA_P0_ACK"
PR_BODY_ENV = "CELESTIA_P0_PR_BODY"

LEDGER_VERSION = 1
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_OPEN, STATUS_CLOSED)
SEVERITIES = ("p0", "p1")

#: Top-level keys the ledger may carry besides ``p0``.
_META_KEYS = frozenset({"version", "updated_at", "source"})
#: Keys every ``[[p0]]`` entry must carry — and may carry (unknown keys are a
#: typo, so they fail loudly instead of being ignored).
_ENTRY_KEYS = (
    "id",
    "title",
    "severity",
    "scope",
    "status",
    "opened_at",
    "closed_by",
    "evidence",
)
_ENTRY_KEY_SET = frozenset(_ENTRY_KEYS)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: ``P0-ACK: P0-A P0-B because the demo runs on RTU`` — the ids come first, the
#: rest of the line is the (optional) stated reason.
_ACK_RE = re.compile(r"P0-ACK\s*:\s*(?P<rest>[^\n]*)", re.IGNORECASE)


class LedgerError(Exception):
    """Raised for a missing / unreadable / invalid ledger (fail-closed)."""


def normalize_repo_name(name: str) -> str:
    """Canonical repo name for scope matching.

    Accepts what a human or CI actually passes — ``owner/repo``, a full URL,
    ``repo.git``, different casing, surrounding whitespace — and reduces it to
    the bare lowercase name, so a spelling variant can never silently turn a
    covered repo into a clear one.
    """
    value = (name or "").strip().rstrip("/")
    if not value:
        return ""
    value = value.rsplit("/", 1)[-1].lower()
    if value.endswith(".git"):
        value = value[:-4]
    return value.strip()


@dataclass(frozen=True)
class P0Entry:
    """One ledger row."""

    id: str
    title: str
    severity: str
    scope: Tuple[str, ...]
    status: str
    opened_at: str
    closed_by: str
    evidence: str

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def covers(self, repo: str) -> bool:
        """True when any ``scope`` pattern matches *repo* (names normalized)."""
        target = normalize_repo_name(repo)
        return any(
            fnmatch.fnmatchcase(target, normalize_repo_name(pattern))
            for pattern in self.scope
        )

    def summary(self) -> str:
        return "%s: %s" % (self.id, self.title)


@dataclass(frozen=True)
class Ack:
    """An explicit acknowledgment of one ledger entry."""

    id: str
    source: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Ledger loading
# ---------------------------------------------------------------------------


def bundled_ledger_path() -> Path:
    """Path of the ledger shipped with the package."""
    return Path(__file__).resolve().with_name(LEDGER_BASENAME)


def resolve_ledger_path(
    explicit: Optional[str] = None,
    repo_root: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Path:
    """Resolve the ledger to read: flag → env → repo-local → bundled copy."""
    if explicit:
        return Path(explicit).expanduser()
    environ = os.environ if env is None else env
    from_env = environ.get(LEDGER_PATH_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    if repo_root is not None:
        local = Path(repo_root) / LOCAL_LEDGER_BASENAME
        if local.is_file():
            return local
    return bundled_ledger_path()


def _validate_entry(raw: object, index: int) -> P0Entry:
    where = "p0[%d]" % index
    if not isinstance(raw, dict):
        raise LedgerError("%s must be a table, got %s" % (where, type(raw).__name__))

    unknown = sorted(set(raw) - _ENTRY_KEY_SET)
    if unknown:
        raise LedgerError(
            "%s has unknown key(s): %s (known: %s)"
            % (where, ", ".join(unknown), ", ".join(_ENTRY_KEYS))
        )
    missing = [key for key in _ENTRY_KEYS if key not in raw]
    if missing:
        raise LedgerError("%s is missing required key(s): %s" % (where, ", ".join(missing)))

    values: Dict[str, str] = {}
    for key in _ENTRY_KEYS:
        if key == "scope":
            continue
        value = raw[key]
        if not isinstance(value, str):
            raise LedgerError("%s.%s must be a string" % (where, key))
        values[key] = value

    scope_raw = raw["scope"]
    if not isinstance(scope_raw, list) or not scope_raw:
        raise LedgerError("%s.scope must be a non-empty list of repo names" % where)
    scope: List[str] = []
    for item in scope_raw:
        if not isinstance(item, str) or not item.strip():
            raise LedgerError("%s.scope entries must be non-empty strings" % where)
        scope.append(item.strip())

    entry_id = values["id"].strip()
    if not _ID_RE.match(entry_id):
        raise LedgerError("%s.id %r is not a valid identifier" % (where, values["id"]))
    if not values["title"].strip():
        raise LedgerError("%s.title must not be empty" % where)
    if values["severity"] not in SEVERITIES:
        raise LedgerError(
            "%s.severity %r is not one of %s" % (where, values["severity"], ", ".join(SEVERITIES))
        )
    if values["status"] not in STATUSES:
        raise LedgerError(
            "%s.status %r is not one of %s" % (where, values["status"], ", ".join(STATUSES))
        )
    if not _DATE_RE.match(values["opened_at"]):
        raise LedgerError("%s.opened_at %r is not YYYY-MM-DD" % (where, values["opened_at"]))
    if not values["evidence"].strip():
        raise LedgerError("%s.evidence must not be empty" % where)
    if values["status"] == STATUS_CLOSED and not values["closed_by"].strip():
        raise LedgerError(
            "%s.status is closed but closed_by is empty — name the PR that closed it" % where
        )
    if values["status"] == STATUS_OPEN and values["closed_by"].strip():
        raise LedgerError("%s.status is open but closed_by is set — close it or clear the field" % where)

    return P0Entry(
        id=entry_id,
        title=values["title"].strip(),
        severity=values["severity"],
        scope=tuple(scope),
        status=values["status"],
        opened_at=values["opened_at"],
        closed_by=values["closed_by"].strip(),
        evidence=values["evidence"].strip(),
    )


def parse_ledger_text(text: str, origin: str = "<ledger>") -> List[P0Entry]:
    """Parse and validate ledger *text*; raise :class:`LedgerError` otherwise."""
    if _toml is None:  # pragma: no cover - depends on the interpreter
        raise LedgerError(
            "no TOML parser available (need Python 3.11+ or the 'tomli' package) — "
            "refusing to treat an unreadable ledger as a pass"
        )
    try:
        data = _toml.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        raise LedgerError("%s is not valid TOML: %s" % (origin, exc)) from exc
    if not isinstance(data, dict):
        raise LedgerError("%s must contain a TOML table at the top level" % origin)

    unknown = sorted(set(data) - _META_KEYS - {"p0"})
    if unknown:
        raise LedgerError(
            "%s has unknown top-level key(s): %s (known: %s)"
            % (origin, ", ".join(unknown), ", ".join(sorted(_META_KEYS | {"p0"})))
        )
    version = data.get("version")
    if version != LEDGER_VERSION:
        raise LedgerError(
            "%s.version is %r but this tool understands %d" % (origin, version, LEDGER_VERSION)
        )

    raw_entries = data.get("p0")
    if raw_entries is None:
        raise LedgerError("%s has no [[p0]] entries — an empty ledger is not a pass" % origin)
    if not isinstance(raw_entries, list):
        raise LedgerError("%s.p0 must be an array of tables ([[p0]])" % origin)
    if not raw_entries:
        raise LedgerError("%s has no [[p0]] entries — an empty ledger is not a pass" % origin)

    entries = [_validate_entry(raw, i) for i, raw in enumerate(raw_entries)]
    seen: Dict[str, int] = {}
    for entry in entries:
        if entry.id in seen:
            raise LedgerError("%s declares duplicate id %r" % (origin, entry.id))
        seen[entry.id] = 1
    return entries


def load_ledger(path: Path) -> List[P0Entry]:
    """Read + validate the ledger at *path*; fail-closed on any problem."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise LedgerError("ledger not found: %s" % path) from exc
    except OSError as exc:
        raise LedgerError("ledger unreadable: %s: %s" % (path, exc)) from exc
    return parse_ledger_text(text, origin=str(path))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def entries_for_repo(entries: Iterable[P0Entry], repo: str) -> List[P0Entry]:
    """Every entry (open or closed) whose scope covers *repo*."""
    return [entry for entry in entries if entry.covers(repo)]


def open_entries_for_repo(entries: Iterable[P0Entry], repo: str) -> List[P0Entry]:
    """Open entries whose scope covers *repo*."""
    return [entry for entry in entries_for_repo(entries, repo) if entry.is_open]


def detect_repo_name(root: Path) -> Optional[str]:
    """Infer the repo name from ``origin``'s URL, or ``None``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return repo_name_from_url(proc.stdout.strip())


def repo_name_from_url(url: str) -> Optional[str]:
    """``https://github.com/org/repo.git`` → ``repo``."""
    if not url:
        return None
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or None


# ---------------------------------------------------------------------------
# Acknowledgments
# ---------------------------------------------------------------------------


def parse_ack_markers(text: str, source: str) -> List[Ack]:
    """Extract ``P0-ACK: <id> [<id>…] [reason]`` markers from a PR body."""
    acks: List[Ack] = []
    for match in _ACK_RE.finditer(text or ""):
        ids: List[str] = []
        reason_words: List[str] = []
        for token in match.group("rest").split():
            stripped = token.strip(",;")
            if stripped and _ID_RE.match(stripped) and stripped.upper().startswith("P0"):
                ids.append(stripped)
            elif ids:
                reason_words.append(token)
        reason = " ".join(reason_words).strip()
        for entry_id in ids:
            acks.append(Ack(id=entry_id, source=source, reason=reason))
    return acks


def collect_acks(
    cli_acks: Sequence[str] = (),
    pr_bodies: Sequence[Tuple[str, str]] = (),
    env: Optional[Dict[str, str]] = None,
) -> List[Ack]:
    """Merge the three acknowledgment channels (CLI, env, PR body)."""
    environ = os.environ if env is None else env
    acks: List[Ack] = []
    for raw in cli_acks:
        for token in re.split(r"[\s,]+", raw or ""):
            if token:
                acks.append(Ack(id=token, source="--ack"))
    for token in re.split(r"[\s,]+", environ.get(ACK_ENV, "") or ""):
        if token:
            acks.append(Ack(id=token, source="%s" % ACK_ENV))
    for body, source in pr_bodies:
        acks.extend(parse_ack_markers(body, source))
    return acks


def _format_ack(ack: Ack) -> str:
    reason = " — %s" % ack.reason if ack.reason else ""
    return "%s (via %s)%s" % (ack.id, ack.source, reason)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_list(entries: Sequence[P0Entry]) -> str:
    """Whole ledger, grouped open/closed, oldest first within a group."""
    lines: List[str] = []
    for status in STATUSES:
        group = sorted(
            (entry for entry in entries if entry.status == status),
            key=lambda entry: (entry.opened_at, entry.id),
        )
        lines.append("%s (%d):" % (status, len(group)))
        if not group:
            lines.append("  (none)")
        for entry in group:
            closing = " closed by %s" % entry.closed_by if entry.closed_by else ""
            lines.append(
                "  %-6s [%s] %s  (opened %s%s)"
                % (entry.id, ", ".join(entry.scope), entry.title, entry.opened_at, closing)
            )
            lines.append("         evidence: %s" % entry.evidence)
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def render_blocked(repo: str, blocked: Sequence[P0Entry]) -> str:
    lines = [
        "p0-gate: %s is covered by %d unresolved P0 finding(s):" % (repo, len(blocked)),
    ]
    for entry in blocked:
        lines.append("  %s: %s" % (entry.id, entry.title))
        lines.append("      evidence: %s" % entry.evidence)
    lines.append(
        "p0-gate: close the ledger entry (closed_by = the PR that did it) or acknowledge it "
        "explicitly with `--ack <id>`, $%s, or a `P0-ACK: <id>` line in the PR body."
        % ACK_ENV
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_pr_bodies(args: argparse.Namespace, env: Dict[str, str]) -> List[Tuple[str, str]]:
    bodies: List[Tuple[str, str]] = []
    if args.pr_body:
        bodies.append((args.pr_body, "--pr-body"))
    if args.pr_body_file:
        try:
            text = Path(args.pr_body_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError("--pr-body-file unreadable: %s: %s" % (args.pr_body_file, exc)) from exc
        bodies.append((text, "--pr-body-file"))
    from_env = env.get(PR_BODY_ENV, "")
    if from_env:
        bodies.append((from_env, "$%s" % PR_BODY_ENV))
    return bodies


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools p0-gate",
        description="Fail a repo covered by an unresolved P0 finding in the org ledger.",
    )
    parser.add_argument(
        "--repo", action="append", default=[], metavar="NAME",
        help="repo name to check (repeatable); accepts owner/repo, URLs and a trailing "
             ".git; default: the current checkout's origin name",
    )
    parser.add_argument(
        "--ack", action="append", default=[], metavar="ID",
        help="explicitly acknowledge an open P0 id (repeatable); acknowledged ids may pass",
    )
    parser.add_argument("--pr-body", default=None, help="PR body text to scan for `P0-ACK: <id>`")
    parser.add_argument("--pr-body-file", default=None, help="file holding the PR body text")
    parser.add_argument("--ledger", default=None, help="ledger path (default: bundled p0_ledger.toml)")
    parser.add_argument("--repo-root", default=None, help="repo root (default: current directory)")
    parser.add_argument("--list", action="store_true", help="print the ledger grouped by status")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    root = Path(args.repo_root or ".").resolve()

    try:
        ledger_path = resolve_ledger_path(args.ledger, repo_root=root, env=env)
        entries = load_ledger(ledger_path)
    except LedgerError as exc:
        print("p0-gate: %s" % exc, file=sys.stderr)
        print("p0-gate: refusing to pass on an unusable ledger (fail-closed)", file=sys.stderr)
        return 2

    if args.list:
        print("ledger: %s" % ledger_path)
        print(render_list(entries))
        return 0

    # Which ledger is in force is part of the record: an override must never be
    # invisible on a green run.
    print(
        "p0-gate: ledger %s (%d entries, %d open)"
        % (ledger_path, len(entries), sum(1 for entry in entries if entry.is_open)),
        file=sys.stderr,
    )

    repos = [normalize_repo_name(name) for name in args.repo if name.strip()]
    if not repos:
        detected = detect_repo_name(root)
        if not detected:
            print(
                "p0-gate: cannot determine the repo name (no origin remote under %s); "
                "pass --repo <name>" % root,
                file=sys.stderr,
            )
            return 2
        repos = [detected]

    try:
        acks = collect_acks(args.ack, _read_pr_bodies(args, env), env=env)
    except LedgerError as exc:
        print("p0-gate: %s" % exc, file=sys.stderr)
        return 2

    acked: Dict[str, Ack] = {}
    for ack in acks:
        acked.setdefault(ack.id, ack)

    blocked: List[P0Entry] = []
    acknowledged: List[Tuple[P0Entry, Ack]] = []
    covered_ids: set = set()
    blocked_ids: set = set()
    ack_seen: set = set()
    for repo in repos:
        for entry in open_entries_for_repo(entries, repo):
            ack = acked.get(entry.id)
            if ack is None:
                if entry.id not in blocked_ids:
                    blocked_ids.add(entry.id)
                    blocked.append(entry)
            elif (entry.id, ack.source) not in ack_seen:
                ack_seen.add((entry.id, ack.source))
                covered_ids.add(entry.id)
                acknowledged.append((entry, ack))

    for entry, ack in acknowledged:
        print(
            "p0-gate: acknowledged %s — %s" % (_format_ack(ack), entry.summary()),
            file=sys.stderr,
        )
    known_ids = {entry.id for entry in entries}
    for ack in acked.values():
        if ack.id not in known_ids:
            print("p0-gate: note: ack %r matches no ledger entry" % ack.id, file=sys.stderr)
        elif ack.id not in covered_ids:
            print(
                "p0-gate: note: ack %s did not cover an open P0 for %s"
                % (_format_ack(ack), ", ".join(repos)),
                file=sys.stderr,
            )

    if blocked:
        print(render_blocked(", ".join(repos), blocked), file=sys.stderr)
        return 1

    print("p0-gate: %s clear (%d open P0(s) in ledger, none unacknowledged for this repo)"
          % (", ".join(repos), sum(1 for entry in entries if entry.is_open)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
