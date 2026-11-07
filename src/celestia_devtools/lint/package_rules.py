#!/usr/bin/env python3
"""Check Cargo package names and lockfile sources against the W0 package rules.

*Why this exists:* the 2026-11-06 plana package-surface plan (W0) makes two
rules mechanical before the rename waves land:

1. **A publishable package name is kebab-case.** A crate that can reach
   crates.io must not be named ``plana_sync``: the underscore would ship as a
   permanent coordinate, and a rename after the first publish means a new
   package plus an old one that never goes away. ``publish = false`` members
   are exempt for now — they carry no crates.io coordinate yet, and their
   rename is W2's work. Only the literal ``publish = false`` exempts: an empty
   ``publish = []`` allow-list is still reported, so the exemption cannot grow
   by accident.
2. **One lockfile, one source per package name.** A ``Cargo.lock`` that lists
   the same package name from more than one source (crates.io registry, a git
   branch, a different git rev, or a path/workspace entry with no ``source``
   at all) means two builds of "the same" crate can disagree. W4 drives the
   family's floating git edges to zero; this check makes the count visible per
   repository instead of waiting for a release to discover it.

*Scope:* ``PATH`` (default: the current directory) is walked recursively for
``Cargo.toml`` and ``Cargo.lock``; ``target/``, ``.git/`` and ``node_modules/``
are skipped. ``[workspace.package]`` is not a package and is never reported;
``publish.workspace = true`` is resolved against ``[workspace.package]``. A
directory that yields neither a manifest nor a lockfile is an error (exit 2),
not a clean pass: "nothing scanned" must never read as green.

*Mode:* advisory by default — violations are printed as warnings and the run
still exits 0, because the W0 rules are declared before W4 enforces them, and
wiring a failing gate into every repository today would block unrelated work.
``--strict`` makes violations exit 1; that is the switch a CI job can adopt
later. ``--json`` prints exactly one machine-readable report on stdout.

*Exit codes:* 0 clean (or advisory with violations), 1 violations under
``--strict``, 2 usage / parse / scan errors (including a zero-hit scan).

Usage::

    celestia-devtools package-rules
    celestia-devtools package-rules /mnt/codespace/plana
    celestia-devtools package-rules . --strict
    celestia-devtools package-rules . --json
"""

from __future__ import annotations
import argparse
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from celestia_devtools.core import envelope

#: Manifest and lockfile names discovered by the recursive walk.
MANIFEST_NAME = "Cargo.toml"
LOCK_NAME = "Cargo.lock"

#: Directories never descended into (build output, VCS metadata, JS deps).
SKIP_DIRS = {"target", ".git", "node_modules"}

#: The character the kebab-case rule forbids in a publishable package name.
UNDERSCORE = "_"

class package_rules_error(Exception):
    """A usage, parse or scan problem that must fail closed (exit 2)."""

@dataclass
class source_use:
    """One distinct ``source`` value a package name resolves from.

    ``None`` is the lockfile spelling of a path dependency or a workspace
    member: it counts as a source of its own when the same name also resolves
    from a registry or git.
    """

    source: str | None
    versions: list[str]

    def to_dict(self) -> dict:
        return {"source": self.source, "versions": list(self.versions)}

@dataclass
class violation:
    """One rule hit: a package name or a lockfile package that must change."""

    check: str
    path: str
    package: str
    detail: str
    sources: list[source_use] = field(default_factory = list)

    def to_dict(self) -> dict:
        payload = {"check": self.check, "path": self.path, "package": self.package, "detail": self.detail}

        if self.sources:
            payload["sources"] = [item.to_dict() for item in self.sources]

        return payload

@dataclass
class report:
    """The scan result: what was read, what was flagged, what failed."""

    root: str
    manifests: int = 0
    locks: int = 0
    violations: list[violation] = field(default_factory = list)
    errors: list[str] = field(default_factory = list)
    strict: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations and not self.errors

    def to_dict(self) -> dict:
        return {"root": self.root, "manifests": self.manifests, "locks": self.locks, "violations": [item.to_dict() for item in self.violations], "errors": list(self.errors), "strict": self.strict, "advisory": not self.strict, "ok": self.ok}

def discover(root: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(manifests, locks)`` under *root*, skipping cache directories."""

    manifests: list[Path] = []
    locks: list[Path] = []

    for directory, dirnames, filenames in os.walk(root, followlinks = False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)

        for filename in sorted(filenames):
            if filename == MANIFEST_NAME:
                manifests.append(Path(directory) / filename)
            elif filename == LOCK_NAME:
                locks.append(Path(directory) / filename)

    return manifests, locks

def _display(root: Path, path: Path) -> str:
    """Report paths relative to the scanned root when they live under it."""

    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()

def _read_toml(path: Path, label: str) -> dict:
    """Parse one TOML file, turning read/parse failures into scan errors.

    *label* is the root-relative spelling used in messages, so a parse error
    and a violation of the same file are reported the same way.
    """

    try:
        return tomllib.loads(path.read_text(encoding = "utf-8"))
    except UnicodeDecodeError as exc:
        raise package_rules_error("%s: not UTF-8: %s" % (label, exc)) from exc
    except OSError as exc:
        raise package_rules_error("%s: cannot read: %s" % (label, exc)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise package_rules_error("%s: not valid TOML: %s" % (label, exc)) from exc

def _publish_disabled(package: dict, data: dict, workspace_data: dict | None) -> bool:
    """True when the package carries no crates.io coordinate (``publish = false``).

    ``publish.workspace = true`` inherits the value from ``[workspace.package]``
    in the **workspace root manifest** — a member manifest never carries that
    table, so resolving against ``data`` alone silently misclassifies every
    inherited member as publishable (found on arona: root ``publish = false``,
    members ``publish.workspace = true`` still flagged). ``workspace_data`` is
    the parsed root manifest; when the scan root is a bare member (no root
    manifest under it) the inheritance cannot be resolved and the package is
    treated as publishable — fail closed.
    """

    publish = package.get("publish")

    if isinstance(publish, dict) and (publish.get("workspace") is True):
        source = workspace_data if workspace_data is not None else data
        workspace = source.get("workspace")
        inherited = workspace.get("package") if isinstance(workspace, dict) else None
        publish = inherited.get("publish") if isinstance(inherited, dict) else None

    return publish is False

def _manifest_violations(root: Path, path: Path, data: dict, workspace_data: dict | None) -> list[violation]:
    """Check A: a publishable ``[package].name`` must be kebab-case."""

    package = data.get("package")

    if not isinstance(package, dict):
        return []

    name = package.get("name")

    if (not isinstance(name, str)) or (not name):
        raise package_rules_error("%s: [package] has no string 'name'" % _display(root, path))

    if (UNDERSCORE not in name) or _publish_disabled(package, data, workspace_data):
        return []

    detail = "package name '%s' contains '_'; a publishable Cargo package name must be kebab-case" % name
    return [violation(check = "package-name", path = _display(root, path), package = name, detail = detail)]

def _source_order(source: str | None) -> tuple[bool, str]:
    """Sort ``None`` (path/workspace) before the textual sources."""

    return (source is not None, source or "")

def _source_label(source: str | None) -> str:
    """Human label for one source; ``None`` is a path or workspace member."""

    return source if source is not None else "path/workspace (no source)"

def _lock_violations(root: Path, path: Path, data: dict) -> list[violation]:
    """Check B: one package name must resolve from exactly one source.

    Different versions from the *same* source are normal (cargo resolves two
    semver ranges independently); only a second source makes the graph
    ambiguous, no matter whether it is a registry, a git rev or a path.
    """

    entries = data.get("package")

    if entries is None:
        return []

    if not isinstance(entries, list):
        raise package_rules_error("%s: 'package' is not an array of tables" % _display(root, path))

    grouped: dict[str, dict[str | None, set[str]]] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")

        if (not isinstance(name, str)) or (not name):
            continue

        raw_source = entry.get("source")
        source = raw_source if isinstance(raw_source, str) else None
        raw_version = entry.get("version")
        version = raw_version if isinstance(raw_version, str) else "?"
        grouped.setdefault(name, {}).setdefault(source, set()).add(version)

    violations: list[violation] = []

    for name in sorted(grouped):
        by_source = grouped[name]

        if len(by_source) < 2:
            continue

        uses = [source_use(source = source, versions = sorted(by_source[source])) for source in sorted(by_source, key = _source_order)]
        detail = "package '%s' resolves from %d different sources in one lockfile" % (name, len(by_source))
        violations.append(violation(check = "lock-source", path = _display(root, path), package = name, detail = detail, sources = uses))

    return violations

def scan(root: Path, strict: bool = False) -> report:
    """Scan *root* and return a :class:`report` (advisory unless *strict*).

    Raises :class:`package_rules_error` when *root* holds no manifest and no
    lockfile: a zero-hit scan is an error, never a clean pass.
    """

    root = root.resolve()
    manifests, locks = discover(root)

    if (not manifests) and (not locks):
        raise package_rules_error("nothing scanned under %s (no Cargo.toml or Cargo.lock)" % root)

    result = report(root = root.as_posix(), manifests = len(manifests), locks = len(locks), strict = strict)

    # The workspace root manifest (``<root>/Cargo.toml``) is the only place
    # ``[workspace.package]`` lives; load it once for ``publish.workspace``
    # inheritance. Absent (scan root is a bare member) -> None, fail closed.
    root_manifest = root / "Cargo.toml"
    workspace_data: dict | None = None
    if root_manifest.is_file():
        try:
            parsed = _read_toml(root_manifest, _display(root, root_manifest))
            if isinstance(parsed.get("workspace"), dict):
                workspace_data = parsed
        except package_rules_error:
            workspace_data = None

    for manifest in manifests:
        try:
            result.violations.extend(_manifest_violations(root, manifest, _read_toml(manifest, _display(root, manifest)), workspace_data))
        except package_rules_error as exc:
            result.errors.append(str(exc))

    for lock in locks:
        try:
            result.violations.extend(_lock_violations(root, lock, _read_toml(lock, _display(root, lock))))
        except package_rules_error as exc:
            result.errors.append(str(exc))

    result.violations.sort(key = lambda item: (item.path, item.check, item.package))
    return result

def _render_text(result: report) -> str:
    """Human-readable report: warnings first, then the summary and errors."""

    lines: list[str] = []

    for item in result.violations:
        lines.append("warning: %s: [%s] %s" % (item.path, item.check, item.detail))

        for use in item.sources:
            lines.append("    %s (%s)" % (_source_label(use.source), ", ".join(use.versions)))

    if lines:
        lines.append("")

    lines.append("package-rules: %d violation(s) in %d Cargo.toml and %d Cargo.lock under %s" % (len(result.violations), result.manifests, result.locks, result.root))

    if result.violations and not result.strict:
        lines.append("package-rules: advisory mode; pass --strict to exit 1 on violations")

    for error in result.errors:
        lines.append("error: %s" % error)

    return "\n".join(lines)

def _error_payload(root: Path, message: str, strict: bool) -> dict:
    """The full report shape for a scan that could not start (exit 2)."""

    return {"root": root.absolute().as_posix(), "manifests": 0, "locks": 0, "violations": [], "errors": [message], "strict": strict, "advisory": not strict, "ok": False}

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog = "celestia-devtools package-rules", description = "Check Cargo package names (kebab-case when publishable) and lockfile sources (one source per package name).")
    parser.add_argument("path", nargs = "?", default = ".", metavar = "PATH", help = "directory to scan recursively for Cargo.toml / Cargo.lock (default: current directory)")
    parser.add_argument("--strict", action = "store_true", help = "exit 1 when violations are found (default: advisory, exit 0)")
    parser.add_argument("--json", action = "store_true", help = "emit the machine-readable report on stdout")
    args = parser.parse_args(argv)
    root = Path(args.path)

    try:
        if not root.is_dir():
            raise package_rules_error("not a directory: %s" % args.path)

        result = scan(root, strict = args.strict)
    except (package_rules_error, OSError) as exc:
        print("package-rules: %s" % exc, file = sys.stderr)

        if args.json:
            envelope.emit(_error_payload(root, str(exc), args.strict), sort_keys = True)

        return 2

    if args.json:
        envelope.emit(result.to_dict(), sort_keys = True)
    else:
        print(_render_text(result), file = sys.stderr)

    if result.errors:
        return 2

    if result.violations and args.strict:
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
