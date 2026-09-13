#!/usr/bin/env python3
"""Local gate orchestration — one command to reproduce the CI checklist locally.

The workspace relies on CI checks scattered across repos; ``gate`` turns the CI
checklist into a single local command so that "local verification == CI" before
a PR is opened.  It encodes the three design elements from PLAN.md §5:

* **modes** — ``gate rust`` / ``gate web`` / ``gate python`` / ``gate all``.
  With no mode the repo is auto-detected (``Cargo.toml`` → rust,
  ``package.json``/``pnpm-workspace.yaml`` → web, ``pyproject.toml`` → python).
  ``--list`` prints the resolved step graph without executing anything.
* **dependency ordering** — each mode's steps form a DAG (fmt → clippy → test →
  deny → coverage → lint-commits, etc.).  A step runs only after its
  dependencies PASS; a failed step aborts its dependents.
* **concurrency budget** — ``--jobs N`` (default ``os.cpu_count()`` capped at
  4) bounds parallel execution of independent steps via a generic topological
  scheduler (``celestia_devtools.core.scheduler``).

``gate precheck`` is a separate safety subcommand implementing the workspace
postmortem follow-ups: NFS mount-point warnings (``findmnt`` scan for worktree
paths) and a large-download heuristic scan (``hf_hub_download`` /
``huggingface_hub`` / ``modelscope`` without ``HF_HUB_DISABLE_XET`` /
``no_proxy`` hints).  The credential scan runs as a *pre-step* of ``gate rust``
and ``gate web``.

``gate credential-scan`` extends the same credential classifier beyond the
changed-files pre-step: it sweeps a checkout's own ``.git/config`` (where a
remote can bake a token into its userinfo, e.g.
``https://oauth2:gho_…@github.com/...``) and the source trees of the
``langyo/*`` business repos (``easy-hydro-*``) that no org workflow visits.
Reports are advisory; literal secrets exit non-zero.  Paths the sweep cannot
read (a root-owned ``lost+found``, a broken symlink, a socket, …) are never
dropped silently: each is printed with its reason, and ``--fail-on-skip``
turns an incomplete sweep into a failure.

Usage::

    celestia-devtools gate              # auto-detect mode
    celestia-devtools gate rust --list  # dry-run: print the graph
    celestia-devtools gate web --jobs 2
    celestia-devtools gate all --coverage
    celestia-devtools gate precheck
    celestia-devtools gate credential-scan --repo arona --repo easy-hydro-erp
    celestia-devtools gate credential-scan --all-langyo --scan-root /mnt/codespace
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from celestia_devtools.core import logger
from celestia_devtools.core.scheduler import (
    FAIL,
    PASS,
    SKIP,
    SKIP_DEP,
    Step,
    run_dag,
)
from celestia_devtools.vcs.commit_msg import EASY_HYDRO_REPOS, lint as lint_subject

MODES = ("rust", "web", "python", "all")

# ── Credential scan heuristics ────────────────────────────────────────────────

# Broad "report" pattern: any credential-looking token in a line. The `pass` /
# `pwd` alternation is guarded by lookarounds so prose like "bypass" or "the
# test will pass" is not flagged, while `SSH_PASS` / `--target-pass` are.
_CRED_PATTERN = re.compile(
    r"password|passwd|passphrase|secret|token|api[_-]?key|"
    r"(?<![a-z0-9])(pass|pwd)(?![a-z0-9])|"
    r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY",
    re.IGNORECASE,
)

# Whitelisted placeholder markers (obvious dummy values + RFC 5737 doc IPs).
# The "your…" convention is anchored at both ends — it must start with ``your``
# plus a separator AND end with the credential noun — so real-world placeholders
# (``your-jwt-secret``, ``your_app_token``) stay whitelisted while a committed
# literal such as ``TOKEN="yoursecret-real-value"`` is not swallowed by an
# unanchored alternative.
_PLACEHOLDER_PATTERN = re.compile(
    r"CHANGE[_ ]?ME|<your[-_ ]?password>|<password>|test[-_ ]?password|"
    r"<your[-_ ]?(?:token|secret|api[-_ ]?key)>|<(?:token|secret|api[-_ ]?key)>|"
    r"your[-_ ][\w-]*(?:password|secret|token|api[-_ ]?key)\b|"
    r"sk-xxx|xxxxx|xxxx|xxx|example|placeholder|redacted|"
    r"192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+",
    re.IGNORECASE,
)

# RFC 5737 documentation addresses — AGENTS §10.1.2 requires them in examples,
# so a credential URL pointing at one cannot be a live credential.
_DOC_ADDRESS_PATTERN = re.compile(r"192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+")

# Reading the value from env/config — no literal secret lives in the tree.
_ENV_REF_PATTERN = re.compile(
    r"os\.environ|getenv|environ\s*\[|\$\{|process\.env|\benv\s*\(",
    re.IGNORECASE,
)

# A PEM private-key header (always suspicious; never a placeholder by default).
_PRIVATE_KEY_PATTERN = re.compile(r"BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY")

# An assignment whose left-hand key contains a credential word, e.g.
# ``SSH_PASS="..."`` / ``password = value`` / ``api_key: value``.
_ASSIGN_KEY_PATTERN = re.compile(
    r"(?:^|[\s(])"
    r"[A-Za-z0-9_.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|pass|pwd)"
    r"[A-Za-z0-9_.-]*\s*[=:]\s*",
    re.IGNORECASE,
)

# A flag carrying a credential value, e.g. ``--target-pass s3cr3t-value`` or
# ``--api-key=realvalue``.
_FLAG_PATTERN = re.compile(
    r"--?[a-zA-Z0-9_-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|pass|pwd)"
    r"[a-zA-Z0-9_-]*(?:\s|=)\s*",
    re.IGNORECASE,
)

# A credential embedded in a URL's userinfo — the ``.git/config`` remote form
# ``https://oauth2:gho_…@github.com/org/repo.git``. Carries no credential WORD,
# so the broad pattern above never sees it; the userinfo IS the secret.
_EMBEDDED_CREDENTIAL_URL_PATTERN = re.compile(
    r"[a-z][a-z0-9+.-]*://"          # scheme://
    r"[^/\s:@]+:"                    # user
    r"(?P<secret>[^/\s@]+)"          # secret (the URL password / token)
    r"@[^\s/]+",                     # @host
    re.IGNORECASE,
)

# Bare provider tokens (GitHub family) — never placeholders by construction.
_GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"
)

#: A URL userinfo secret shorter than this is treated as an example, not a
#: credential: ``user:pass@host`` (and RFC 5737 hosts) in docs must keep the
#: pre-extension "report" verdict, while a real token/secret in a remote URL
#: (``oauth2:gho_…``, a 32-char app secret, …) is escalated to a violation.
_MIN_EMBEDDED_SECRET_LEN = 16

# Directories never walked by the workspace credential sweep.
_SWEEP_SKIP_DIRS = frozenset({
    ".git", "node_modules", "target", "dist", "build", "vendor", "coverage",
    ".venv", "venv", "__pycache__", ".next", ".nuxt", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "site-packages",
})

# Files above this size are not credential-scanned (lockfiles, bundles, …).
_MAX_SWEEP_BYTES = 2 * 1024 * 1024


def _first_token(rest: str) -> Optional[str]:
    """Extract the leading value token from the text after an assignment."""
    rest = rest.strip()
    if not rest:
        return None
    if rest[0] in "\"'":
        end = rest.find(rest[0], 1)
        if end > 0:
            return rest[1:end]
        return rest
    return re.split(r"[\s;#]+", rest, maxsplit=1)[0] or None


def _extract_secret_value(line: str) -> Optional[str]:
    """Return the literal value assigned to a credential key, or ``None``."""
    match = _ASSIGN_KEY_PATTERN.search(line)
    if match:
        return _first_token(line[match.end():])
    match = _FLAG_PATTERN.search(line)
    if match:
        return _first_token(line[match.end():])
    return None


def classify_credential_line(line: str) -> str:
    """Classify one source line for the credential scan.

    Returns ``"clean"`` (no credential token), ``"report"`` (a hit that is
    whitelisted or env-sourced — reported but not fatal), or ``"violation"``
    (a literal non-placeholder secret — fatal).

    Besides the classic ``key = value`` / ``--flag value`` forms, two shapes
    carry a secret without ever naming one:

    * a URL with credentials in its userinfo — the ``.git/config`` remote form
      ``https://oauth2:gho_…@github.com/org/repo.git``;
    * a bare provider token (``gho_…`` / ``ghp_…`` / ``github_pat_…``).
    """
    embedded = _EMBEDDED_CREDENTIAL_URL_PATTERN.search(line)
    bare_token = _GITHUB_TOKEN_PATTERN.search(line)
    if not (_CRED_PATTERN.search(line) or embedded or bare_token):
        return "clean"
    if _PRIVATE_KEY_PATTERN.search(line):
        return "report" if _PLACEHOLDER_PATTERN.search(line) else "violation"

    value = _extract_secret_value(line)
    if value is None and embedded is not None:
        # The URL userinfo is the secret this line carries — but only escalate
        # it when it is credential-shaped, so doc examples such as
        # ``http://user:pass@192.0.2.1:3128`` keep their historical "report".
        secret = embedded.group("secret")
        if _DOC_ADDRESS_PATTERN.search(line) or len(secret) < _MIN_EMBEDDED_SECRET_LEN:
            return "report"
        value = secret

    if value is not None:
        if _PLACEHOLDER_PATTERN.search(value) or _ENV_REF_PATTERN.search(value):
            return "report"
        if _ENV_REF_PATTERN.search(line):
            return "report"
        return "violation"

    # No assignment and no embedded URL: only a bare provider token is fatal.
    if _PLACEHOLDER_PATTERN.search(line) or _ENV_REF_PATTERN.search(line):
        return "report"
    return "violation" if bare_token else "report"


# ── Workspace credential sweep ────────────────────────────────────────────────
#
# The per-PR pre-step further down only sees files changed vs origin/master
# inside one checkout, so two surfaces from the 2026-09-10 audit stayed
# invisible: tokens baked into a checkout's own `.git/config`
# (``https://oauth2:gho_…@github.com/...``) and the ``langyo/*`` business repos
# (the easy-hydro-* family) that no org workflow visits. Both are still the
# §10.1 red line, so `gate credential-scan` runs the same clean/report/
# violation classifier over them. It only reports — it never rewrites a
# checkout.

@dataclass
class CredentialFinding:
    """One non-clean line surfaced by the workspace sweep."""

    path: Path
    line: int
    verdict: str
    excerpt: str

    def render(self) -> str:
        return "%s:%d: [%s] %s" % (self.path, self.line, self.verdict, self.excerpt.strip())


@dataclass
class SkippedPath:
    """A path the sweep could not read, with the reason why."""

    path: Path
    reason: str

    def render(self) -> str:
        return "%s (%s)" % (self.path, self.reason)


def _oserror_reason(exc: OSError) -> str:
    return "%s: %s" % (type(exc).__name__, exc.strerror or exc)


def _record_skip(skipped: Optional[List[SkippedPath]], path: Path, reason: str) -> None:
    if skipped is not None:
        skipped.append(SkippedPath(path, reason))


def git_dir(root: Path) -> Optional[Path]:
    """The ``.git`` directory for *root*, following a worktree ``gitdir:`` file."""
    dot = Path(root) / ".git"
    try:
        if dot.is_dir():
            return dot
        if not dot.is_file():
            return None
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        target = Path(text[len("gitdir:"):].strip())
        if not target.is_absolute():
            target = (Path(root) / target).resolve()
        return target
    return None


def git_config_paths(
    root: Path, skipped: Optional[List[SkippedPath]] = None
) -> List[Path]:
    """Existing git config files carrying *root*'s remotes.

    A checkout's ``.git/config`` holds the credential-bearing remote URL; a
    linked worktree stores it in the common dir's config (plus an optional
    ``config.worktree`` beside its own gitdir). An unreadable ``.git`` entry
    (e.g. a root-owned directory whose mode forbids traversal) is recorded in
    *skipped* instead of raising.
    """
    dot = Path(root) / ".git"
    candidates: List[Path] = []
    try:
        is_dir = dot.is_dir()
        is_file = dot.is_file() if not is_dir else False
    except OSError as exc:
        _record_skip(skipped, dot, _oserror_reason(exc))
        return []
    if is_dir:
        candidates.append(dot / "config")
    elif is_file:
        own = git_dir(root)
        if own is None:
            _record_skip(skipped, dot, "unparsable gitdir pointer")
        else:
            candidates.append(own / "config.worktree")
            # <common>/worktrees/<name> → <common>/config
            candidates.append(own.parent.parent / "config")
    existing: List[Path] = []
    for path in candidates:
        try:
            if path.is_file():
                existing.append(path)
        except OSError as exc:
            _record_skip(skipped, path, _oserror_reason(exc))
    return existing


def repo_remote_urls(root: Path, skipped: Optional[List[SkippedPath]] = None) -> List[str]:
    """Remote URLs declared in *root*'s git config (text-level, no git call)."""
    urls: List[str] = []
    for config in git_config_paths(root, skipped=skipped):
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _record_skip(skipped, config, _oserror_reason(exc))
            continue
        for match in re.finditer(r"^\s*url\s*=\s*(\S+)", text, re.MULTILINE):
            urls.append(match.group(1))
    return urls


def is_langyo_repo(root: Path, skipped: Optional[List[SkippedPath]] = None) -> bool:
    """True for a business checkout of the ``langyo/*`` account.

    Detected from the origin URL (the remote may itself carry an embedded
    token), with the ``easy-hydro-*`` directory convention as a fallback.
    """
    if any("github.com/langyo/" in url or "github.com:langyo/" in url
           for url in repo_remote_urls(root, skipped=skipped)):
        return True
    return Path(root).name.startswith("easy-hydro-")


def discover_langyo_repos(
    scan_root: Path, skipped: Optional[List[SkippedPath]] = None
) -> List[Path]:
    """Sibling checkouts under *scan_root* belonging to the langyo account.

    A sibling that cannot be probed (``lost+found`` on the NFS export is
    root-owned mode 700, so ``stat``-ing ``<child>/.git`` raises
    ``PermissionError``) is recorded in *skipped* — a scanner that crashes
    mid-sweep reports nothing, which is worse than skipping a path out loud.
    """
    root = Path(scan_root)
    try:
        if not root.is_dir():
            return []
        children = sorted(root.iterdir())
    except OSError as exc:
        _record_skip(skipped, root, _oserror_reason(exc))
        return []
    repos: List[Path] = []
    for child in children:
        try:
            if not child.is_dir() or not (child / ".git").exists():
                continue
        except OSError as exc:
            _record_skip(skipped, child, _oserror_reason(exc))
            continue
        if is_langyo_repo(child, skipped=skipped):
            repos.append(child)
    return repos


def _sweep_entry_verdict(path: Path) -> Optional[str]:
    """``None`` when *path* should be scanned, else the skip reason."""
    try:
        if path.is_symlink() and not path.exists():
            return "broken symlink"
        stat_result = path.stat()
    except OSError as exc:
        return _oserror_reason(exc)
    if not stat.S_ISREG(stat_result.st_mode):
        return "not a regular file"
    if stat_result.st_size > _MAX_SWEEP_BYTES:
        return "larger than %d bytes" % _MAX_SWEEP_BYTES
    return None


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return False


def iter_sweep_files(
    root: Path, skipped: Optional[List[SkippedPath]] = None
) -> Iterator[Path]:
    """Text files under *root* worth scanning, streaming as they are found.

    ``os.walk`` with in-place directory pruning (VCS/build trees are never
    descended into) and an ``onerror`` hook, so an unreadable directory is
    reported through *skipped* instead of aborting the sweep or vanishing.
    Binary files are skipped silently: NUL bytes cannot be a credential,
    whereas an *access* failure is always reported.
    """
    def onerror(exc: OSError) -> None:
        _record_skip(skipped, Path(getattr(exc, "filename", None) or root), _oserror_reason(exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SWEEP_SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            reason = _sweep_entry_verdict(path)
            if reason is not None:
                _record_skip(skipped, path, reason)
                continue
            if _is_binary(path):
                continue
            yield path


def scan_file_credentials(path: Path) -> List[CredentialFinding]:
    """Classify every line of *path*, returning the non-clean findings.

    Raises ``OSError`` when the file cannot be read: the caller decides whether
    that is a reported skip or a hard error — never a silent "clean".
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: List[CredentialFinding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        verdict = classify_credential_line(line)
        if verdict != "clean":
            findings.append(CredentialFinding(path, lineno, verdict, line))
    return findings


def credential_sweep(
    roots: Sequence[Path],
    include_git_config: bool = True,
    include_sources: bool = True,
    skipped: Optional[List[SkippedPath]] = None,
) -> List[CredentialFinding]:
    """Sweep *.git/config* and source trees of *roots* for literal secrets.

    Unreadable paths land in *skipped* (with a reason) instead of raising or
    being dropped silently.
    """
    if skipped is None:
        skipped = []
    findings: List[CredentialFinding] = []
    seen: set = set()
    for root in roots:
        root = Path(root)
        configs = git_config_paths(root, skipped=skipped) if include_git_config else []
        for config in configs:
            if config in seen:
                continue
            seen.add(config)
            try:
                findings.extend(scan_file_credentials(config))
            except OSError as exc:
                _record_skip(skipped, config, _oserror_reason(exc))
        if not include_sources:
            continue
        for path in iter_sweep_files(root, skipped=skipped):
            if path in seen:
                continue
            seen.add(path)
            try:
                findings.extend(scan_file_credentials(path))
            except OSError as exc:
                _record_skip(skipped, path, _oserror_reason(exc))
    return findings


def credential_scan_cli(root: Path, args: argparse.Namespace) -> int:
    """``gate credential-scan``: sweep .git/config + source trees for secrets."""
    if args.git_config_only and args.no_git_config:
        print(
            "error: --git-config-only and --no-git-config are mutually exclusive "
            "(together they scan nothing)",
            file=sys.stderr,
        )
        return 2

    scan_root = Path(args.scan_root).resolve() if args.scan_root else root.parent
    skipped: List[SkippedPath] = []
    targets: List[Path] = []
    if args.repo:
        for name in args.repo:
            candidate = scan_root / name
            try:
                is_checkout = candidate.is_dir()
            except OSError as exc:
                print("error: cannot read %s: %s" % (candidate, _oserror_reason(exc)), file=sys.stderr)
                return 2
            if not is_checkout:
                print("error: no checkout at %s" % candidate, file=sys.stderr)
                return 2
            targets.append(candidate)
    elif args.all_langyo:
        targets = discover_langyo_repos(scan_root, skipped=skipped)
        if not targets:
            print(
                "error: no langyo/* checkout under %s — refusing to report a clean sweep "
                "of nothing" % scan_root,
                file=sys.stderr,
            )
            return 2
    else:
        targets = [root]

    findings = credential_sweep(
        targets,
        include_git_config=not args.no_git_config,
        include_sources=not args.git_config_only,
        skipped=skipped,
    )
    violations = [f for f in findings if f.verdict == "violation"]
    reports = [f for f in findings if f.verdict == "report"]

    print("credential-scan: %d path(s) under %s" % (len(targets), scan_root))
    for finding in violations:
        print("  " + finding.render(), file=sys.stderr)
    if args.show_reports:
        for finding in reports:
            print("  " + finding.render(), file=sys.stderr)
    for entry in skipped:
        print("  skipped: " + entry.render(), file=sys.stderr)
    if skipped:
        logger.warn("credential-scan: %d path(s) could not be read (listed above)" % len(skipped))

    if violations:
        logger.error(
            "credential-scan: %d literal non-placeholder secret(s) in %d path(s)"
            % (len(violations), len(targets))
        )
        return 1
    if skipped and args.fail_on_skip:
        logger.error(
            "credential-scan: --fail-on-skip and %d unreadable path(s) — refusing an "
            "incomplete sweep" % len(skipped)
        )
        return 1
    logger.ok(
        "credential-scan: no literal secrets (%d placeholder/env hit(s) ignored, "
        "%d path(s) skipped)" % (len(reports), len(skipped))
    )
    return 0


# ── Mode detection ────────────────────────────────────────────────────────────

def detect_modes(root: Path) -> List[str]:
    """Return the list of detectable modes for a repo directory."""
    modes: List[str] = []
    if (root / "Cargo.toml").is_file():
        modes.append("rust")
    if (root / "package.json").is_file() or (root / "pnpm-workspace.yaml").is_file():
        modes.append("web")
    if (root / "pyproject.toml").is_file():
        modes.append("python")
    return modes


class UsageError(Exception):
    """Raised for CLI usage problems (mapped to exit code 2)."""


def resolve_modes(root: Path, mode: Optional[str]) -> List[str]:
    """Resolve the CLI mode argument to a concrete list of modes."""
    if mode in ("rust", "web", "python"):
        return [mode]
    detected = detect_modes(root)
    if not detected:
        raise UsageError(
            "no project detected (no Cargo.toml / package.json / "
            "pnpm-workspace.yaml / pyproject.toml); pass an explicit mode"
        )
    if mode == "all":
        return detected
    # mode is None — auto-detect.
    return detected


def default_jobs() -> int:
    """Default concurrency budget: cpu count, capped at 4."""
    return max(1, min(os.cpu_count() or 1, 4))


# ── Git helpers (internal steps) ──────────────────────────────────────────────

def _git_output(args: List[str], root: Path) -> Optional[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _changed_files(root: Path) -> Optional[List[str]]:
    """Files changed vs origin/master, or ``None`` when unavailable."""
    out = _git_output(["diff", "--name-only", "origin/master...HEAD"], root)
    if out is None:
        return None
    return [f for f in out.splitlines() if f.strip()]


def _repo_name(root: Path) -> str:
    url = _git_output(["config", "--get", "remote.origin.url"], root)
    if url:
        name = url.strip().rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return Path(root).name


def _credential_scan(root: Path) -> Callable[[], str]:
    """Pre-step callable: scan changed files for non-placeholder secrets."""
    def run() -> str:
        files = _changed_files(root)
        if not files:
            logger.info("credential-scan: no changed files vs origin/master — skipping")
            return SKIP
        violations: List[str] = []
        reported = 0
        for rel in files:
            path = root / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                verdict = classify_credential_line(line)
                if verdict == "clean":
                    continue
                reported += 1
                if verdict == "violation":
                    violations.append("%s:%d: %s" % (rel, lineno, line.strip()))
        if violations:
            logger.error("credential-scan: %d non-placeholder secret(s) found" % len(violations))
            for item in violations:
                print("  " + item, file=sys.stderr)
            return FAIL
        if reported:
            logger.warn("credential-scan: %d hit(s) — all placeholders/env-referenced" % reported)
        else:
            logger.ok("credential-scan: no credential-looking lines in changed files")
        return PASS
    return run


def _lint_commits(root: Path) -> Callable[[], str]:
    """Validate branch commits against origin/master via commit-msg lint."""
    def run() -> str:
        out = _git_output(["log", "--format=%s", "origin/master..HEAD"], root)
        if out is None:
            logger.warn("lint-commits: no origin/master remote — skipping")
            return SKIP
        subjects = [s for s in out.splitlines() if s.strip()]
        if not subjects:
            logger.info("lint-commits: no commits ahead of origin/master — skipping")
            return SKIP
        allow_cjk = _repo_name(root) in EASY_HYDRO_REPOS
        errors: List[str] = []
        bad = 0
        for subject in subjects:
            violations = lint_subject(subject, allow_cjk=allow_cjk)
            if violations:
                bad += 1
                errors.append(subject)
                errors.extend("  - " + v for v in violations)
        if errors:
            logger.error("lint-commits: %d invalid subject(s)" % bad)
            print("\n".join(errors), file=sys.stderr)
            return FAIL
        logger.ok("lint-commits: %d commit(s) OK" % len(subjects))
        return PASS
    return run


# ── Mode graph builders ───────────────────────────────────────────────────────

def build_rust_graph(root: Path, coverage: bool = False) -> List[Step]:
    """Canonical rust DAG: credential-scan → fmt → clippy → check → deny →
    (coverage) → lint-commits."""
    steps: List[Step] = [Step("credential-scan", _credential_scan(root), (), str(root))]
    prev = "credential-scan"

    def add(name: str, cmd: Optional[Union[List[str], Callable[[], str]]]) -> None:
        nonlocal prev
        steps.append(Step(name, cmd, (prev,), str(root)))
        prev = name

    add("fmt", ["cargo", "fmt", "--check"])
    add("clippy", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"])
    add("check", ["cargo", "test"])
    if shutil.which("cargo-deny"):
        add("deny", ["cargo", "deny", "check"])
    else:
        logger.warn("cargo-deny not installed — skipping 'cargo deny check'")
        add("deny", None)
    if coverage:
        add("coverage", ["cargo", "tarpaulin"])
    add("lint-commits", _lint_commits(root))
    return steps


def _needs_install(root: Path) -> bool:
    """True when node_modules is missing or a lockfile changed since install."""
    node_modules = root / "node_modules"
    if not node_modules.is_dir():
        return True
    for lock in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
        lock_path = root / lock
        if lock_path.is_file() and lock_path.stat().st_mtime > node_modules.stat().st_mtime:
            return True
    return False


def build_web_graph(root: Path) -> List[Step]:
    """Canonical web DAG: credential-scan → install* → lint → build → test →
    lint-commits (* install only when deps changed)."""
    steps: List[Step] = [Step("credential-scan", _credential_scan(root), (), str(root))]
    prev = "credential-scan"

    def add(name: str, cmd: Optional[Union[List[str], Callable[[], str]]]) -> None:
        nonlocal prev
        steps.append(Step(name, cmd, (prev,), str(root)))
        prev = name

    if _needs_install(root):
        add("install", ["pnpm", "install", "--frozen-lockfile"])
    else:
        logger.info("node_modules up to date and lockfile unchanged — skipping install")
        add("install", None)
    add("lint", ["pnpm", "lint"])
    add("build", ["pnpm", "build"])
    add("test", ["pnpm", "test"])
    add("lint-commits", _lint_commits(root))
    return steps


def build_python_graph(root: Path) -> List[Step]:
    """Canonical python DAG: ruff-check → ruff-format → pytest → lint-commits."""
    steps: List[Step] = [Step("ruff-check", ["ruff", "check", "."], (), str(root))]
    steps.append(Step("ruff-format", ["ruff", "format", "--check", "."], ("ruff-check",), str(root)))
    steps.append(Step("pytest", ["pytest"], ("ruff-format",), str(root)))
    steps.append(Step("lint-commits", _lint_commits(root), ("pytest",), str(root)))
    return steps


def build_graph(root: Path, mode: str, coverage: bool = False) -> List[Step]:
    if mode == "rust":
        return build_rust_graph(root, coverage=coverage)
    if mode == "web":
        return build_web_graph(root)
    if mode == "python":
        return build_python_graph(root)
    raise UsageError("unknown mode: %s" % mode)


def _prefix_steps(steps: List[Step], prefix: str) -> List[Step]:
    return [
        Step(
            "%s:%s" % (prefix, s.name),
            s.cmd,
            tuple("%s:%s" % (prefix, d) for d in s.deps),
            s.cwd,
        )
        for s in steps
    ]


# ── Execution ─────────────────────────────────────────────────────────────────

def _normalize_status(result: object) -> str:
    if result is True:
        return PASS
    if result is False:
        return FAIL
    if result in (PASS, FAIL, SKIP):
        return result
    return FAIL


def _execute_step(step: Step) -> str:
    cmd = step.cmd
    if callable(cmd):
        try:
            return _normalize_status(cmd())
        except Exception as exc:
            logger.error("%s: internal step raised %r" % (step.name, exc))
            return FAIL
    if cmd is None:
        return SKIP
    cwd = step.cwd or str(Path.cwd())
    logger.info("$ %s  (in %s)" % (" ".join(cmd), cwd))
    return PASS if subprocess.run(cmd, cwd=cwd).returncode == 0 else FAIL


_DISPLAY = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP", SKIP_DEP: "SKIP"}


def _make_runner(durations: Dict[str, float]) -> Callable[[Step], str]:
    def runner(step: Step) -> str:
        start = time.monotonic()
        status = _execute_step(step)
        durations[step.name] = time.monotonic() - start
        print("[%s] %-18s %6.2fs" % (_DISPLAY.get(status, status), step.name, durations[step.name]))
        return status
    return runner


def _print_summary(steps: List[Step], statuses: Dict[str, str], durations: Dict[str, float]) -> None:
    print()
    print("%-18s %-8s %8s" % ("STEP", "STATUS", "SECONDS"))
    print("-" * 36)
    for step in steps:
        status = statuses.get(step.name, SKIP)
        print("%-18s %-8s %8.2f" % (
            step.name, _DISPLAY.get(status, status), durations.get(step.name, 0.0),
        ))
    failed = sum(1 for s in statuses.values() if s == FAIL)
    print("-" * 36)
    print("%d step(s), %d failed" % (len(statuses), failed))


def _print_graph(root: Path, jobs: int, plans: List[Tuple[str, List[Step]]]) -> None:
    print("repo: %s" % root)
    print("jobs: %d" % jobs)
    for label, steps in plans:
        print()
        print("mode: %s" % label)
        width = max((len(s.name) for s in steps), default=0)
        for step in steps:
            deps = ", ".join(step.deps) if step.deps else "-"
            print("  %-*s  deps: [%s]" % (width, step.name, deps))


def _run(steps: List[Step], jobs: int) -> int:
    durations: Dict[str, float] = {}
    statuses = run_dag(steps, _make_runner(durations), jobs)
    _print_summary(steps, statuses, durations)
    return 1 if any(s == FAIL for s in statuses.values()) else 0


# ── precheck ──────────────────────────────────────────────────────────────────

_NFS_FSTYPES = {"nfs", "nfs3", "nfs4"}


def findmnt_mounts(findmnt_cmd: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """Return ``[(target, fstype), ...]`` from ``findmnt -rn -o TARGET,FSTYPE``.

    ``findmnt_cmd`` is injectable for tests. Returns ``[]`` when findmnt is
    unavailable or fails.
    """
    cmd = findmnt_cmd if findmnt_cmd is not None else ["findmnt", "-rn", "-o", "TARGET,FSTYPE"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    mounts: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        target = parts[0]
        fstype = parts[1].strip() if len(parts) > 1 else ""
        mounts.append((target, fstype))
    return mounts


def precheck_mounts(mounts: List[Tuple[str, str]], cwd: Path) -> List[str]:
    """Return warning strings for dangerous NFS mountpoints."""
    warnings: List[str] = []
    cwd_resolved = str(cwd.resolve()).rstrip("/")
    for target, fstype in mounts:
        if fstype.lower() not in _NFS_FSTYPES:
            continue
        target_stripped = target.rstrip("/")
        if "/_worktree/" in target_stripped + "/":
            warnings.append(
                "NFS mountpoint under a _worktree path: %s (%s) — rm -rf here "
                "deletes the mount source" % (target_stripped, fstype)
            )
        if target_stripped == cwd_resolved:
            warnings.append(
                "current directory is an NFS mountpoint: %s (%s)" % (target_stripped, fstype)
            )
    return warnings


def scan_large_downloads(root: Path) -> List[str]:
    """Warn about model-hub download scripts missing the safety hints.

    Heuristic: any ``.py`` / ``.sh`` file mentioning ``hf_hub_download`` /
    ``huggingface_hub`` / ``modelscope`` that does not also mention
    ``HF_HUB_DISABLE_XET`` or ``no_proxy``.  Warn only, never fail.
    """
    warnings: List[str] = []
    exclude = {".git", "node_modules", "target", "dist", "__pycache__"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if any(part in exclude for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"hf_hub_download|huggingface_hub|modelscope", text):
            if not re.search(r"HF_HUB_DISABLE_XET|no_proxy", text):
                warnings.append(
                    "%s: model-hub download without HF_HUB_DISABLE_XET=1 / no_proxy hint"
                    % path.relative_to(root)
                )
    return warnings


def precheck(root: Path) -> int:
    """Run the ``gate precheck`` safety diagnostics (advisory, exit 0)."""
    if Path("/mnt/codespace").exists():
        mounts = findmnt_mounts()
        for warning in precheck_mounts(mounts, root):
            logger.warn(warning)
    else:
        logger.info("not on the workspace host — skipping mount-point precheck")

    for warning in scan_large_downloads(root):
        logger.warn(warning)

    logger.ok("precheck complete")
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools gate",
        description="Run the local CI gate: modes + DAG ordering + job budget.",
    )
    parser.add_argument(
        "mode", nargs="?", default=None,
        choices=list(MODES) + ["precheck", "credential-scan"],
        help="gate mode (rust/web/python/all); omit to auto-detect; 'precheck' runs safety "
             "checks; 'credential-scan' sweeps .git/config + source trees for secrets",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the resolved step graph without executing",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="max parallel steps (default: cpu count capped at 4)",
    )
    parser.add_argument(
        "--coverage", action="store_true",
        help="(rust/all) enable cargo tarpaulin coverage",
    )
    parser.add_argument(
        "--repo-root", default=None,
        help="repo root directory (default: current directory)",
    )
    # credential-scan options.
    parser.add_argument(
        "--scan-root", default=None,
        help="(credential-scan) directory holding the checkouts to sweep "
             "(default: the parent of the repo root)",
    )
    parser.add_argument(
        "--repo", action="append", default=[], metavar="NAME",
        help="(credential-scan) sweep <scan-root>/NAME (repeatable)",
    )
    parser.add_argument(
        "--all-langyo", action="store_true",
        help="(credential-scan) sweep every langyo/* business checkout under --scan-root",
    )
    parser.add_argument(
        "--git-config-only", action="store_true",
        help="(credential-scan) only read .git/config files, skip source trees",
    )
    parser.add_argument(
        "--no-git-config", action="store_true",
        help="(credential-scan) only read source trees, skip .git/config files",
    )
    parser.add_argument(
        "--show-reports", action="store_true",
        help="(credential-scan) also print placeholder/env-referenced hits",
    )
    parser.add_argument(
        "--fail-on-skip", action="store_true",
        help="(credential-scan) exit 1 when any path could not be read "
             "(an incomplete sweep is not a pass)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root or ".").resolve()

    if args.mode == "precheck":
        return precheck(root)

    if args.mode == "credential-scan":
        return credential_scan_cli(root, args)

    jobs = args.jobs if args.jobs is not None else default_jobs()
    if jobs < 1:
        print("error: --jobs must be >= 1", file=sys.stderr)
        return 2

    try:
        modes = resolve_modes(root, args.mode)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    plans: List[Tuple[str, List[Step]]] = [
        (mode, build_graph(root, mode, coverage=args.coverage)) for mode in modes
    ]

    if args.list:
        _print_graph(root, jobs, plans)
        return 0

    if len(plans) == 1:
        steps = plans[0][1]
    else:
        merged: List[Step] = []
        for mode, graph in plans:
            merged.extend(_prefix_steps(graph, mode))
        steps = merged

    return _run(steps, jobs)


if __name__ == "__main__":
    raise SystemExit(main())
