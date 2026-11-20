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
  ``[workspace.dependencies]``, ``[target.<triple>.dependencies]``, renamed
  ``package = "chrono"`` aliases, ``[replace]`` ``"name:version"`` keys and
  ``[patch.*]`` snapshots) and Rust sources for the crate name;
* a full-tree hit needs a **declaration**: a comment that merely mentions the
  crate ("migrated off chrono in 2026-11") must not nag forever, and it must not
  keep ``--strict`` red after the migration — source mentions only become
  evidence under a declaration, or a hit of their own in the ``--staged`` view
  (where the mention *is* the change under review). Trade-off, accepted: a
  repository that consumes chrono only through another crate's re-export (no
  local declaration) gets no full-tree hit; the staged view still catches it
  while the diff is authored;
* the default run prints one warning block per affected crate and **exits 0**
  (advisory form, safe to wire into a trunk basic check before the tree is
  clean), exactly like ``depspec-lint``'s advisory mode;
* ``--strict`` turns declaration findings into exit 1, for when the migration is
  done and the gate should keep chrono from creeping back in;
* ``--json`` prints one machine-readable report on stdout (the text form goes
  to stderr, so machine consumers never parse warnings).

*Word boundary:* the pattern is ``(?<![\\w-])crate(?![\\w-])``. Matching plain
``crate`` as a substring would flag two real shapes that are *not* the crate:
``chrono-tz`` (a different crate) and ``chrono_no_dep_timestamp`` (wishwell's
function that exists to avoid chrono). Both are pinned by tests.

*Scope:* the tree is walked recursively; VCS, build output, JS dependency
trees, vendored snapshots and python envs are skipped. A tree that yields no
manifest and no source file is an error (exit 2) — "nothing scanned" must never
read as green. A manifest that does not parse is counted separately and never
counts as "checked"; if every manifest is unparseable the run reports that
instead of claiming the tree is clean.

*Exit codes:* 0 clean or advisory findings, 1 declaration findings under
``--strict``, 2 usage / scan errors.
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

SKIP_DIRS = {".git", "node_modules", "target", "vendor", "patches", "dist", ".venv", "venv", "__pycache__", ".pnpm-store", "_worktree"}
MANIFEST = "Cargo.toml"
SOURCE_SUFFIX = ".rs"
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
    unparsed: int = 0
    sources: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"manifests_scanned": self.manifests, "manifests_unparsed": self.unparsed, "sources_scanned": self.sources, "advisories": [h.to_dict() for h in self.hits]}

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

    patch = data.get("patch")

    if isinstance(patch, dict):
        for registry in patch.values():
            if isinstance(registry, dict):
                tables.append(registry)

    replace = data.get("replace")

    if isinstance(replace, dict):
        # [replace] keys are "name:version", with no registry level in between.
        tables.append(replace)

    return tables

def _table_declares(table: dict, crate: str) -> bool:
    """True when the table depends on ``crate``: plain key, ``name:version``, or alias."""

    if crate in table:
        return True

    for key, entry in table.items():
        if isinstance(key, str) and (key.split(":")[0] == crate):
            return True

        if isinstance(entry, dict) and (entry.get("package") == crate):
            return True

    return False

def _is_dependency_section(section: str) -> bool:
    return (section.rsplit(".", 1)[-1] in ("dependencies", "dev-dependencies", "build-dependencies")) or section.startswith("patch.") or (section == "replace")

def _lines_of(text: str, crate: str) -> list[int]:
    """1-based evidence lines, section-aware (a feature table is not a dependency)."""

    numbers: list[int] = []
    section = ""
    key_re = re.compile(r'^["\']?([A-Za-z0-9_.:-]+)["\']?\s*[=.]')
    alias_re = re.compile(r'package\s*=\s*["\']' + re.escape(crate) + r'["\']')

    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            parts = section.split(".")

            if (len(parts) >= 2) and (parts[-2] in ("dependencies", "dev-dependencies", "build-dependencies")) and (parts[-1].strip("\"'") == crate):
                numbers.append(number)

            continue

        if not _is_dependency_section(section):
            continue

        match = key_re.match(stripped)

        if match and (match.group(1).split(":")[0] == crate):
            numbers.append(number)
        elif alias_re.search(stripped):
            numbers.append(number)

    return numbers

def _scan_manifest(root: Path, path: Path, text: str, hits: dict[str, hit]) -> bool:
    """Record declarations; returns True when the manifest parsed."""

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False

    if not isinstance(data, dict):
        return True

    tables = _dependency_tables(data)

    for adv in ADVISORIES:
        declared = sum(1 for table in tables if _table_declares(table, adv.crate))

        if not declared:
            continue

        lines = _lines_of(text, adv.crate)
        where = [f"{_display(root, path)}:{number}" for number in lines[:declared]] or [_display(root, path) for _ in range(declared)]
        bucket = hits.setdefault(adv.crate, hit(advisory = adv))
        bucket.declarations.extend(where)

    return True

def _scan_source(root: Path, path: Path, text: str, hits: dict[str, hit]) -> None:
    for adv in ADVISORIES:
        if not adv.pattern().search(text):
            continue

        bucket = hits.setdefault(adv.crate, hit(advisory = adv))
        bucket.sources.append(_display(root, path))

def _finish(hits: dict[str, hit], *, declarations_only: bool) -> list[hit]:
    ordered = [hits[key] for key in sorted(hits)]

    if declarations_only:
        return [bucket for bucket in ordered if bucket.declarations]

    return ordered

def scan(root: Path | str) -> report:
    """Collect declaration-backed hits under ``root`` (manifests + Rust sources)."""

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
                    rep.unparsed += 1
                    continue

                if _scan_manifest(root, path, text, hits):
                    rep.manifests += 1
                else:
                    rep.unparsed += 1
            elif fn.endswith(SOURCE_SUFFIX):
                try:
                    text = path.read_text(encoding = "utf-8", errors = "replace")
                except OSError:
                    continue

                rep.sources += 1
                _scan_source(root, path, text, hits)

    # A comment that mentions the crate is not a dependency: full-tree mode only
    # reports declaration-backed hits, so a migrated tree goes quiet (and
    # --strict can actually go green).
    rep.hits = _finish(hits, declarations_only = True)

    return rep

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # errors="replace": a staged non-UTF-8 blob must warn or stay silent, never
    # raise UnicodeDecodeError inside a pre-commit hook.
    return subprocess.run(["git", "-C", str(root), *args], capture_output = True, text = True, errors = "replace")

def scan_staged(root: Path | str) -> report:
    """Scan only the staged content (``--staged``: the pre-commit view).

    ``-z`` disables git's C-quoting, so a non-ASCII path is not silently dropped;
    both declarations and source mentions count here, because the mention *is*
    the change under review. A commit that touches no manifest and no Rust
    source is a normal event, so an empty staged set is not an error.
    """

    root = Path(root)
    rep = report()
    hits: dict[str, hit] = {}
    proc = _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")

    if proc.returncode != 0:
        return rep

    for rel in proc.stdout.split("\0"):
        if not rel:
            continue

        name = rel.rsplit("/", 1)[-1]

        if not (rel.endswith(SOURCE_SUFFIX) or (name == MANIFEST)):
            continue

        blob = _git(root, "show", f":{rel}")

        if blob.returncode != 0:
            continue

        path = root / rel

        if name == MANIFEST:
            if _scan_manifest(root, path, blob.stdout, hits):
                rep.manifests += 1
            else:
                rep.unparsed += 1
        else:
            rep.sources += 1
            _scan_source(root, path, blob.stdout, hits)

    rep.hits = _finish(hits, declarations_only = False)

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
    parser.add_argument("--strict", action = "store_true", help = "exit 1 on a declaration finding (post-migration form)")
    parser.add_argument("--json", action = "store_true", help = "print one machine-readable report on stdout")
    args = parser.parse_args(argv)

    root = Path(args.path)

    if not root.is_dir():
        print(f"dep-advisories: {root}: not a directory", file = sys.stderr)
        return 2

    try:
        rep = scan_staged(root) if args.staged else scan(root)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"dep-advisories: {exc}", file = sys.stderr)
        return 2

    # A full scan that found nothing at all is a broken invocation; an empty
    # staged set is a normal commit, so only the full-tree form errors out.
    # A full scan that read no manifest is a broken invocation, not a clean
    # tree: "nothing scanned" must never read as green (an empty staged set is
    # a normal commit, so only the full-tree form errors out).
    if (not args.staged) and (rep.manifests == 0):
        if rep.unparsed:
            print(f"dep-advisories: {root}: no manifest could be parsed ({rep.unparsed} unparseable)", file = sys.stderr)
        else:
            print(f"dep-advisories: {root}: no Cargo.toml or .rs file found", file = sys.stderr)

        if args.json:
            print(json.dumps(rep.to_dict(), ensure_ascii = False))

        return 2

    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii = False))
    else:
        if not args.quiet:
            parsed = f"{rep.manifests} manifest(s)"

            if rep.unparsed:
                parsed += f" (+{rep.unparsed} unparseable)"

            print(f"dep-advisories: scanned {parsed}, {rep.sources} source file(s)", file = sys.stderr)

        text = render(rep.hits)

        if text:
            print(text, file = sys.stderr)
        elif not args.quiet:
            print("  no unmaintained dependency in use", file = sys.stderr)

    # --strict is the post-migration gate: only a real declaration fails it, so
    # a comment that says "we removed chrono" can never keep the gate red.
    if args.strict and any(bucket.declarations for bucket in rep.hits):
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
