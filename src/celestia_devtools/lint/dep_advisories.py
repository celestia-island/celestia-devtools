#!/usr/bin/env python3
"""Warn about dependencies the family must stop using (RUSTSEC unmaintained class).

*Why this exists:* RUSTSEC-2026-0543 (issued 2026-11-17) marks **chrono** as
unmaintained — every version is affected, no patched release exists, and the
advisory recommends **jiff** (or time). The family ruling (2026-11-20) is jiff
1.x, but replacing chrono across the workspace is a multi-repository program;
until it lands, every agent that reaches for a date/time type must see this
warning *at the moment it decides*, not months later in a weekly security run.

This module is deliberately a **warning channel, not a ban**:

* it scans ``Cargo.toml`` declarations (every dependency table, including
  ``[workspace.dependencies]`` and ``[target.<triple>.dependencies]``) and Rust
  sources for the crate name;
* the default run prints one warning block per affected crate and **exits 0**
  (advisory form, safe to wire into a trunk basic check before the tree is
  clean), exactly like ``depspec-lint``'s advisory mode;
* ``--strict`` turns findings into exit 1, for when the migration is done and
  the gate should keep chrono from creeping back in;
* ``--json`` prints one machine-readable report on stdout (the text form goes
  to stderr, so machine consumers never parse warnings).

*Word boundary:* the pattern is ``(?<![\\w-])crate(?![\\w-])``. Matching plain
``crate`` as a substring would flag two real shapes that are *not* the crate:
``chrono-tz`` (a different crate) and ``chrono_no_dep_timestamp`` (wishwell's
function that exists precisely to avoid chrono). Both are pinned by tests.

*Scope:* the tree is walked recursively; VCS, build output, JS dependency
trees, vendored snapshots and python envs are skipped. A tree that yields no
manifest is an error (exit 2) — "nothing scanned" must never read as green.

*Exit codes:* 0 clean or advisory findings, 1 findings under ``--strict``,
2 usage / scan errors.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

#: Directories never descended into: same class as depspec's skip list.
SKIP_DIRS = {".git", "node_modules", "target", "vendor", "patches", "dist", ".venv", "venv", "__pycache__", ".pnpm-store", "_worktree"}

MANIFEST = "Cargo.toml"
SOURCE_SUFFIX = ".rs"

#: Evidence is capped so a large tree prints a warning, not a directory dump.
MAX_EVIDENCE = 4

@dataclass(frozen = True)
class advisory:
    """One crate the family has decided to stop depending on."""

    crate: str
    id: str
    issued: str
    replacement: tuple[str, ...]
    why: str
    next: str

    def pattern(self) -> re.Pattern[str]:
        """Word-boundary matcher; excludes ``chrono-tz`` / ``chrono_x`` shapes."""

        return re.compile(r"(?<![\w-])" + re.escape(self.crate) + r"(?![\w-])")

    def sentence(self) -> str:
        options = " / ".join(self.replacement)
        return f"warning: {self.crate} is unmaintained ({self.id}, issued {self.issued}; no patched release) — migrate to {options} as soon as possible, and do not add new {self.crate} usage."

#: The policy table. chrono is the first entry (2026-11-20 ruling); future
#: unmaintained crates join here with their advisory id and replacement.
ADVISORIES: tuple[advisory, ...] = (advisory(crate = "chrono", id = "RUSTSEC-2026-0543", issued = "2026-11-17", replacement = ("jiff",), why = "all versions are affected; there is no patched release", next = 'jiff 1.x (`jiff = "1"`); if it must stay for now, record why in deny.toml with an expiry date'),)

@dataclass
class hit:
    """Every place one advisory was found in a tree."""

    advisory: advisory
    declarations: list[str] = field(default_factory = list)
    sources: list[str] = field(default_factory = list)

    def to_dict(self) -> dict[str, object]:
        return {"crate": self.advisory.crate, "id": self.advisory.id, "issued": self.advisory.issued, "replacement": list(self.advisory.replacement), "declarations": list(self.declarations), "sources": list(self.sources)}

@dataclass
class report:
    hits: list[hit] = field(default_factory = list)
    manifests: int = 0
    sources: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"manifests_scanned": self.manifests, "sources_scanned": self.sources, "advisories": [h.to_dict()for h in self.hits]}

def _display(root: Path, path: Path) -> str:
    """Path relative to the scan root, forward slashes, stable for --json."""

    return path.relative_to(root).as_posix()

def _dependency_tables(data: dict) -> list[dict]:
    """Every dependency table a Cargo manifest can declare a crate in."""

    tables: list[dict] = []

    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        sec = data.get(key)

        if isinstance(sec, dict):
            tables.append(sec)

    workspace = data.get("workspace")

    if isinstance(workspace, dict) and isinstance(workspace.get("dependencies"), dict):
        tables.append(workspace["dependencies"])

    targets = data.get("target")

    if isinstance(targets, dict):
        for tbl in targets.values():
            if not isinstance(tbl, dict):
                continue

            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps = tbl.get(key)

                if isinstance(deps, dict):
                    tables.append(deps)

    return tables

def _line_of(text: str, crate: str) -> int | None:
    """1-based line of the ``crate = …`` declaration, for the evidence string."""

    line_re = re.compile(r'^\s*["\']?' + re.escape(crate) + r'["\']?\s*=')

    for number, line in enumerate(text.splitlines(), 1):
        if line_re.match(line):
            return number

    return None

def _scan_manifest(root: Path, path: Path, text: str, hits: dict[str, hit]) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return  # malformed manifests are depspec-lint's problem, not ours

    if not isinstance(data, dict):
        return

    tables = _dependency_tables(data)

    for adv in ADVISORIES:
        declared = any(adv.crate in table for table in tables)

        if not declared:
            continue

        line = _line_of(text, adv.crate)
        where = f"{_display(root, path)}:{line}" if line else _display(root, path)
        hits.setdefault(adv.crate, hit(advisory = adv)).declarations.append(where)

def _scan_source(root: Path, path: Path, text: str, hits: dict[str, hit]) -> None:
    for adv in ADVISORIES:
        if not adv.pattern().search(text):
            continue

        bucket = hits.setdefault(adv.crate, hit(advisory = adv))
        bucket.sources.append(_display(root, path))

def scan(root: Path | str) -> report:
    """Collect every advisory hit under ``root`` (manifests + Rust sources)."""

    root = Path(root)
    rep = report()
    hits: dict[str, hit] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fn in filenames:
            path = Path(dirpath) / fn

            if fn == MANIFEST:
                try:
                    text = path.read_text(encoding = "utf-8")
                except (UnicodeDecodeError, OSError):
                    continue

                rep.manifests += 1
                _scan_manifest(root, path, text, hits)
            elif fn.endswith(SOURCE_SUFFIX):
                try:
                    text = path.read_text(encoding = "utf-8", errors = "replace")
                except OSError:
                    continue

                rep.sources += 1
                _scan_source(root, path, text, hits)

    rep.hits = [hits[key] for key in sorted(hits)]
    return rep

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output = True, text = True)

def scan_staged(root: Path | str) -> report:
    """Scan only the staged content (``--staged``: the pre-commit view).

    A commit that touches no manifest and no Rust source is a normal event, so
    an empty staged set is not an error here (unlike the full-tree scan).
    """

    root = Path(root)
    rep = report()
    hits: dict[str, hit] = {}
    proc = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR")

    if proc.returncode != 0:
        return rep

    for line in proc.stdout.splitlines():
        rel = line.strip()

        if not rel or not (rel.endswith(SOURCE_SUFFIX) or (rel.rsplit("/", 1)[-1] == MANIFEST)):
            continue

        blob = _git(root, "show", f":{rel}")

        if blob.returncode != 0:
            continue

        path = root / rel

        if rel.rsplit("/", 1)[-1] == MANIFEST:
            rep.manifests += 1
            _scan_manifest(root, path, blob.stdout, hits)
        else:
            rep.sources += 1
            _scan_source(root, path, blob.stdout, hits)

    rep.hits = [hits[key] for key in sorted(hits)]
    return rep

def render(hits: Sequence[hit]) -> str:
    """Text form: one warning block per affected crate (stderr channel)."""

    if not hits:
        return ""

    lines: list[str] = []

    for bucket in hits:
        adv = bucket.advisory
        decls = bucket.declarations[:MAX_EVIDENCE]
        decl_suffix = f", … (+{len(bucket.declarations) - MAX_EVIDENCE})" if len(bucket.declarations) > MAX_EVIDENCE else ""
        sources = bucket.sources[:MAX_EVIDENCE]
        source_suffix = f", … (+{len(bucket.sources) - MAX_EVIDENCE} more)" if len(bucket.sources) > MAX_EVIDENCE else ""
        lines.append(adv.sentence())
        lines.append(f"         found: {', '.join(decls) or 'no Cargo.toml declaration'} ({len(bucket.declarations)} direct declaration(s)){decl_suffix}")

        if bucket.sources:
            lines.append(f"                {len(bucket.sources)} source file(s) mention it: {', '.join(sources)}{source_suffix}")

        lines.append(f"         why:   {adv.why}")
        lines.append(f"         next:  {adv.next}")

    return "\n".join(lines)

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools dep-advisories", description = "Report dependencies marked unmaintained (RUSTSEC-2026-0543: chrono → jiff).")
    parser.add_argument("path", nargs = "?", default = ".", help = "repository tree to scan (default: .)")
    parser.add_argument("--staged", action = "store_true", help = "scan only the staged diff (pre-commit view)")
    parser.add_argument("--quiet", action = "store_true", help = "print warnings only; suppress the scan summary")
    parser.add_argument("--strict", action = "store_true", help = "exit 1 when an advisory is found (post-migration form)")
    parser.add_argument("--json", action = "store_true", help = "print one machine-readable report on stdout")
    args = parser.parse_args(argv)

    root = Path(args.path)

    if not root.is_dir():
        print(f"dep-advisories: {root}: not a directory", file = sys.stderr)
        return 2

    try:
        rep = scan_staged(root) if args.staged else scan(root)
    except OSError as exc:
        print(f"dep-advisories: {exc}", file = sys.stderr)
        return 2

    # A full scan that found nothing at all is a broken invocation; an empty
    # staged set is a normal commit, so only the full-tree form errors out.
    if (not args.staged) and (rep.manifests == 0) and (rep.sources == 0):
        print(f"dep-advisories: {root}: no Cargo.toml or .rs file found", file = sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii = False))
    else:
        if not args.quiet:
            print(f"dep-advisories: scanned {rep.manifests} manifest(s), {rep.sources} source file(s)", file = sys.stderr)

        text = render(rep.hits)

        if text:
            print(text, file = sys.stderr)
        elif not args.quiet:
            print("  no unmaintained dependency in use", file = sys.stderr)

    if args.strict and rep.hits:
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
