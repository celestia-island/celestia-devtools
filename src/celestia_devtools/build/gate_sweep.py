"""Workspace credential sweep for the build gate.

Owns the finding/skip records, langyo-repo discovery, git-config surface,
file iteration with binary/size guards, per-file scanning and the sweep CLI.
Split out of gate.py (2026-10-31).
"""

from __future__ import annotations
import argparse
import errno
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Set
from celestia_devtools.core import logger
from celestia_devtools.build.gate_cred import (_MAX_SWEEP_BYTES, _SWEEP_SKIP_DIRS, _UTF16_BOMS, classify_credential_line)

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
class credential_finding:
    """One non-clean line surfaced by the workspace sweep."""

    path: Path
    line: int
    verdict: str
    excerpt: str

    def render(self) -> str:
        return "%s:%d: [%s] %s" % (self.path, self.line, self.verdict, self.excerpt.strip())

@dataclass
class skipped_path:
    """A path the sweep could not read, with the reason why."""

    path: Path
    reason: str

    def render(self) -> str:
        return "%s (%s)" % (self.path, self.reason)

def _oserror_reason(exc: OSError) -> str:
    return "%s: %s" % (type(exc).__name__, exc.strerror or exc)

def _record_skip(skipped: List[skipped_path] | None, path: Path, reason: str) -> None:
    if skipped is not None:
        skipped.append(skipped_path(path, reason))

def git_dir(root: Path) -> Path | None:
    """The ``.git`` directory for *root*, following a worktree ``gitdir:`` file."""
    dot = Path(root) / ".git"

    try:
        if dot.is_dir():
            return dot

        if not dot.is_file():
            return None

        text = dot.read_text(encoding = "utf-8", errors = "replace").strip()
    except OSError:
        return None

    if text.startswith("gitdir:"):
        target = Path(text[len("gitdir:"):].strip())

        if not target.is_absolute():
            target = (Path(root) / target).resolve()

        return target

    return None

def git_config_paths(root: Path, skipped: List[skipped_path] | None = None) -> List[Path]:
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

def repo_remote_urls(root: Path, skipped: List[skipped_path] | None = None) -> List[str]:
    """Remote URLs declared in *root*'s git config (text-level, no git call)."""
    urls: List[str] = []

    for config in git_config_paths(root, skipped = skipped):
        try:
            text = config.read_text(encoding = "utf-8", errors = "replace")
        except OSError as exc:
            _record_skip(skipped, config, _oserror_reason(exc))
            continue

        for match in re.finditer(r"^\s*url\s*=\s*(\S+)", text, re.MULTILINE):
            urls.append(match.group(1))

    return urls

def is_langyo_repo(root: Path, skipped: List[skipped_path] | None = None) -> bool:
    """True for a business checkout of the ``langyo/*`` account.

    Detected from the origin URL (the remote may itself carry an embedded
    token), with the ``easy-hydro-*`` directory convention as a fallback.
    """

    if any(("github.com/langyo/" in url) or ("github.com:langyo/" in url) for url in repo_remote_urls(root, skipped = skipped)):
        return True

    return Path(root).name.startswith("easy-hydro-")

def discover_langyo_repos(scan_root: Path, skipped: List[skipped_path] | None = None) -> List[Path]:
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

        if is_langyo_repo(child, skipped = skipped):
            repos.append(child)

    return repos

def _sweep_entry_verdict(path: Path) -> str | None:
    """``None`` when *path* should be scanned, else the skip reason.

    Order matters: ``Path.exists()`` swallows ELOOP, so a symlink loop has to be
    diagnosed from ``stat`` before the "broken symlink" shortcut can claim it.
    """

    try:
        stat_result = path.stat()
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return "unresolvable symlink"

        if (getattr(exc, "errno", None) == errno.ENOENT) and path.is_symlink():
            return "broken symlink"

        return _oserror_reason(exc)

    if not stat.S_ISREG(stat_result.st_mode):
        return "not a regular file"

    if stat_result.st_size > _MAX_SWEEP_BYTES:
        return "larger than %d bytes" % _MAX_SWEEP_BYTES

    return None

def _head_is_binary(path: Path) -> bool:
    """True for a binary file (NUL bytes that are not a UTF-16 BOM marker)."""

    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False

    if b"\0" not in head:
        return False

    return not any(head.startswith(bom) for bom in _UTF16_BOMS)

def iter_sweep_files(root: Path, skipped: List[skipped_path] | None = None, excluded: Set[str] | None = None) -> Iterator[Path]:
    """Text files under *root* worth scanning, streaming as they are found.

    ``os.walk`` with in-place directory pruning and an ``onerror`` hook, so an
    unreadable directory is reported through *skipped* instead of aborting the
    sweep or vanishing. Anything that is *not* scanned is accounted for:
    directories behind a symlink, unreadable paths, broken symlinks, loops,
    sockets/FIFOs and oversized files are all listed with a reason, and the
    names pruned by :data:`_SWEEP_SKIP_DIRS` are collected into *excluded* so the
    caller can say which trees it deliberately skipped. Binary files are the one
    silent category — NUL bytes cannot be a credential, except in UTF-16 text,
    which is decoded and scanned.
    """

    def onerror(exc: OSError) -> None:
        _record_skip(skipped, Path(getattr(exc, "filename", None) or root), _oserror_reason(exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror = onerror, followlinks = False):
        kept: List[str] = []

        for name in dirnames:
            if name in _SWEEP_SKIP_DIRS:
                # excluded by design, not a read failure — collected so the CLI
                # can state it instead of reporting an unqualified "clean"
                if excluded is not None:
                    excluded.add(name)

                continue

            child = Path(dirpath) / name

            try:
                if child.is_symlink():
                    _record_skip(skipped, child, "symlinked directory (not followed)")
                    continue
            except OSError as exc:
                _record_skip(skipped, child, _oserror_reason(exc))
                continue

            kept.append(name)

        dirnames[:] = kept

        for name in filenames:
            path = Path(dirpath) / name
            reason = _sweep_entry_verdict(path)

            if reason is not None:
                _record_skip(skipped, path, reason)
                continue

            if _head_is_binary(path):
                continue

            yield path

def _decode_text(data: bytes) -> str:
    """Decode file bytes as UTF-8, falling back to BOM-marked UTF-16."""

    for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                return data[len(bom):].decode(encoding, errors = "replace")
            except (UnicodeDecodeError, LookupError):  # pragma: no cover - defensive
                break

    return data.decode("utf-8", errors = "replace")

def scan_file_credentials(path: Path, skipped: List[skipped_path] | None = None) -> List[credential_finding]:
    """Classify every line of *path*, returning the non-clean findings.

    UTF-16 text is decoded rather than mistaken for binary; a file that is
    binary after all is recorded in *skipped* instead of quietly returning an
    empty result. Raises ``OSError`` when the file cannot be read: the caller
    decides whether that is a reported skip or a hard error — never a silent
    "clean".
    """
    text = _decode_text(path.read_bytes())

    if "\0" in text:
        _record_skip(skipped, path, "binary content")
        return []

    findings: List[credential_finding] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        verdict = classify_credential_line(line)

        if verdict != "clean":
            findings.append(credential_finding(path, lineno, verdict, line))

    return findings

def credential_sweep(roots: Sequence[Path], include_git_config: bool = True, include_sources: bool = True, skipped: List[skipped_path] | None = None, excluded: Set[str] | None = None, stats: Dict[str, int] | None = None) -> List[credential_finding]:
    """Sweep *.git/config* and source trees of *roots* for literal secrets.

    Unreadable paths land in *skipped* (with a reason) instead of raising or
    being dropped silently.
    """

    if skipped is None:
        skipped = []

    if stats is not None:
        stats.setdefault("files", 0)

    findings: List[credential_finding] = []
    seen: set = set()

    for root in roots:
        root = Path(root)
        configs = git_config_paths(root, skipped = skipped) if include_git_config else []

        for config in configs:
            if config in seen:
                continue

            seen.add(config)

            try:
                findings.extend(scan_file_credentials(config, skipped = skipped))
            except OSError as exc:
                _record_skip(skipped, config, _oserror_reason(exc))

        if not include_sources:
            continue

        for path in iter_sweep_files(root, skipped = skipped, excluded = excluded):
            if path in seen:
                continue

            seen.add(path)

            try:
                before = len(skipped)
                findings.extend(scan_file_credentials(path, skipped = skipped))

                # A file that turned out to be binary was not scanned, so it
                # must not be counted as one (never both "skipped" and "scanned").
                if (stats is not None) and (len(skipped) == before):
                    stats["files"] = stats.get("files", 0) + 1
            except OSError as exc:
                _record_skip(skipped, path, _oserror_reason(exc))

    return findings

def credential_scan_cli(root: Path, args: argparse.Namespace) -> int:
    """``gate credential-scan``: sweep .git/config + source trees for secrets."""

    if args.git_config_only and args.no_git_config:
        print("error: --git-config-only and --no-git-config are mutually exclusive " "(together they scan nothing)", file = sys.stderr)
        return 2

    scan_root = Path(args.scan_root).resolve() if args.scan_root else root.parent
    skipped: List[skipped_path] = []
    excluded: Set[str] = set()
    stats: Dict[str, int] = {}
    targets: List[Path] = []

    if args.repo:
        for name in args.repo:
            candidate = scan_root / name

            try:
                is_checkout = candidate.is_dir()
            except OSError as exc:
                print("error: cannot read %s: %s" % (candidate, _oserror_reason(exc)), file = sys.stderr)
                return 2

            if not is_checkout:
                print("error: no checkout at %s" % candidate, file = sys.stderr)
                return 2

            targets.append(candidate)
    elif args.all_langyo:
        targets = discover_langyo_repos(scan_root, skipped = skipped)

        if not targets:
            for entry in skipped:
                print("  skipped: " + entry.render(), file = sys.stderr)

            print("error: no langyo/* checkout under %s — refusing to report a clean sweep " "of nothing" % scan_root, file = sys.stderr)
            return 2
    else:
        targets = [root]

    findings = credential_sweep(targets, include_git_config = not args.no_git_config, include_sources = not args.git_config_only, skipped = skipped, excluded = excluded, stats = stats)
    violations = [f for f in findings if f.verdict == "violation"]
    reports = [f for f in findings if f.verdict == "report"]

    print("credential-scan: %d path(s) under %s" % (len(targets), scan_root))

    for finding in violations:
        print("  " + finding.render(), file = sys.stderr)

    if args.show_reports:
        for finding in reports:
            print("  " + finding.render(), file = sys.stderr)

    for entry in skipped:
        print("  skipped: " + entry.render(), file = sys.stderr)

    if skipped:
        logger.warn("credential-scan: %d path(s) could not be read (listed above)" % len(skipped))

    if excluded:
        logger.info("credential-scan: excluded by name (build artefacts/VCS/deps): %s" % ", ".join(sorted(excluded)))

    if not stats.get("files"):
        logger.warn("credential-scan: no source file was scanned — an empty scan is not evidence of a " "clean tree")

    # A checkout can be months behind its remote, and a scan of such a tree reports
    # leaks that are already fixed upstream — which reads as "master still has live
    # secrets". This workspace has main checkouts stranded on commits the retired
    # history-rewriting workflow orphaned (AGENTS §8.3.7), and it has others that are
    # simply behind — 2026-09-13: all 20 hits this sweep reported came from two
    # checkouts that were behind `origin/master`, where the same files contain zero
    # secrets, because the cleanups had already merged. Say it out loud instead of
    # leaving the reader to infer it from the paths.
    stale = [(p, note) for p in targets if (note := _checkout_staleness(p))]

    if stale:
        logger.warn("credential-scan: %d checkout(s) are not identical to their remote default branch " "— findings in those trees may already be fixed upstream:" % len(stale))

        for path, note in sorted(stale, key = lambda item: item[0].name):
            logger.info("  %-24s %s" % (path.name, note))

    stale_violations = [finding for finding in violations if any(str(finding.path).startswith(str(path) + os.sep)for path, _ in stale)]

    if violations:
        message = "credential-scan: %d literal non-placeholder secret(s) in %d path(s)" % (len(violations), len(targets))

        if stale_violations:
            message+= ("; %d of them sit inside %d checkout(s) that differ from origin/<default> — " "check the remote before treating them as live" % (len(stale_violations), len(stale)))

        logger.error(message)
        return 1

    if skipped and args.fail_on_skip:
        logger.error("credential-scan: --fail-on-skip and %d unreadable path(s) — refusing an " "incomplete sweep" % len(skipped))
        return 1

    logger.ok("credential-scan: no literal secrets (%d placeholder/env hit(s) ignored, " "%d path(s) skipped)" % (len(reports), len(skipped)))
    return 0

def _git_output(args: List[str], root: Path) -> str | None:
    proc = subprocess.run(["git", *args], cwd = str(root), capture_output = True, text = True)

    if proc.returncode != 0:
        return None

    return proc.stdout

def _checkout_staleness(path: Path) -> str | None:
    """``None`` when *path* is identical to its remote default branch, else a description.

    Returns ``"ahead X / behind Y (origin/<ref>)"`` — note that a checkout can be BOTH
    (a stranded local commit plus real upstream progress), which ``--count`` reports and
    a plain ``merge-base --is-ancestor`` test would miss.

    Deliberately silent when there is no remote ref to compare against: "cannot tell"
    must never be rendered as "behind", or the warning becomes noise.
    """

    for ref in ("origin/master", "origin/main"):
        if _git_output(["rev-parse", "--verify", "-q", ref], path) is None:
            continue

        counts = _git_output(["rev-list", "--left-right", "--count", "HEAD...%s" % ref], path)

        if counts is None:
            continue

        parts = counts.split()

        if len(parts) == 2:
            ahead, behind = parts

            if (ahead == "0") and (behind == "0"):
                return None

            return "ahead %s / behind %s (%s)" % (ahead, behind, ref)

    return None

def _changed_files(root: Path) -> List[str] | None:
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
