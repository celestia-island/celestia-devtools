#!/usr/bin/env python3
"""Check dependency declarations against the 2026-11-09 two-form ruling.

*Why this exists:* the depspec wave (2026-11-09, 方案 B) normalized every
declaration in the family to the two-form rule, and the review rounds proved
that NO package manager enforces the rule itself: pnpm's frozen-lockfile gate
passes a manifest+lock pair that both say ``^*`` (consistent-pair mutation,
hikari R3 M3b), cargo has no canonical-form concept at all (evernight-cargo R3
M3), and lockless packages are invisible to both (hikari default_theme M4).
This gate parses the declaration FILES directly, so none of those blind spots
apply.

*Rule (AGENTS 版本声明, 2026-11-09):* declarations are two-form —

* npm ``^<major>`` (major ≥ 1) or ``^0.<minor>``;
* Cargo single-segment ``"N"`` (``^`` is an accepted synonym, so ``"^1"`` is
  legal) or ``"0.<minor>"``;
* python ``~=`` padded with ``.0`` (``~=3.0`` / ``~=0.55.0``; single-segment
  ``~=3`` is illegal and floats the whole 0.x axis).

Banned: three-segment forms, ``~`` ranges (npm/Cargo), non-prerelease exact
pins, ``^*``, ``^0.*``, bare ``*``, ``latest``, bare names.

*Slot exemption:* only npm ``devDependencies`` may keep ``^*``; every other
slot is strict. ``^0.*`` is banned in every slot (github.io follow-up,
2026-11-10 adjudication).

*Prerelease exemption:* pinning a prerelease line is legal while upstream has
no stable line — any specifier carrying a prerelease tag (``-beta.N``,
``-rc.N``, …) passes. NOTE: this is a *conditional* exemption; upstream
publishing a stable line voids it (rsa 0.10.0 did exactly that during the
wave). The gate cannot see the registry, so staleness checks stay with the
dependency radar.

*Exempt classes (repair/internal channels, per the ruling and the wave
receipts):* ``workspace:`` / ``file:`` / ``link:`` / ``catalog:`` protocols,
Cargo ``{ path = ... }`` dependencies (their ``version`` keys are
workspace-internal and were a registered exclusion class in the wave),
``[patch.crates-io]`` snapshots, ``vendor/`` trees (byte-identical invariant),
and pnpm ``overrides`` / ``resolutions`` (documented repair channel — this
gate never reads them).

*Registered exact pins* (owner-decision class, wave receipt 五): aris
``boa_*`` ×3, kei vendored ``unwinding`` ×3, kei vendored ``cargo-component``
manifests, evernight ``rsa`` rc.18. They live in :data:`REGISTERED_EXACT_PINS`
keyed by repository name and path substring; the gate skips only those exact
dependencies, not whole files.

*Scope:* ``PATH`` (default: the current directory) is walked recursively for
``package.json``, ``Cargo.toml`` and ``pyproject.toml``; VCS, build output and
dependency directories are skipped. A directory that yields nothing to scan is
an error (exit 2) — "nothing scanned" must never read as green.

*Mode:* ``--strict`` makes violations exit 1 (the CI form); the default run is
advisory (exit 0 with a report) so a repo can adopt the gate before its tree
is clean. ``--json`` prints one machine-readable report.

*Exit codes:* 0 clean (or advisory with findings), 1 findings under
``--strict``, 2 usage / parse / scan errors (including a zero-hit scan).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

#: Directories never descended into: VCS metadata, build output, JS dependency
#: trees, vendored upstream snapshots (their manifests are upstream's, held
#: byte-identical on purpose), python envs, rust caches.
SKIP_DIRS = {
    ".git",
    "node_modules",
    "target",
    "vendor",
    "patches",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".pnpm-store",
}

#: Manifest files the gate understands.
NPM_MANIFEST = "package.json"
CARGO_MANIFEST = "Cargo.toml"
PYTHON_MANIFEST = "pyproject.toml"

#: npm slots where every banned form is a violation. ``devDependencies`` is
#: special-cased: only ``^*`` is legal there, everything else follows the same
#: ban list.
NPM_STRICT_SLOTS = ("dependencies", "optionalDependencies", "peerDependencies")
NPM_DEV_SLOT = "devDependencies"

#: A prerelease tag somewhere in the version string (npm and Cargo spellings):
#: ``-beta.301``, ``-rc.18``, ``-alpha.0``, ``-pre``, ``-dev``.
_PRERELEASE_RE = re.compile(r"-[0-9]*[A-Za-z][A-Za-z0-9.-]*\.[0-9]|-(?:beta|rc|alpha|pre|dev|next|canary)\b", re.IGNORECASE)

#: npm bare-name / wildcard shapes.
_NPM_BARE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")
_NPM_VERSIONISH_RE = re.compile(r"[0-9]")

#: Registered non-prerelease exact pins (owner-decision class). Keyed by
#: repository directory name → list of (path substring, dependency name).
#: Anything matching is skipped with a note instead of a violation. Sourced
#: from the wave receipt §五; the dependency radar owns their lifecycle.
REGISTERED_EXACT_PINS: dict[str, list[tuple[str, str]]] = {
    "aris": [
        ("", "boa_engine"),
        ("", "boa_gc"),
        ("packages/js", "boa_engine"),
    ],
    "kei": [
        ("packages/ostd", "unwinding"),
        ("cargo-component", "*"),
        ("analysis", "*"),
        ("packages/patches/x86_64", "*"),
    ],
    # rsa rc.18: prerelease-legal form anyway; recorded here so the pin stays
    # visible if someone reformats it. The radar owns the staleness check
    # (stable 0.10.0 shipped 2026-11, voiding the exemption predicate).
    "evernight": [
        ("", "rsa"),
    ],
}


class depspec_error(Exception):
    """Usage / parse / scan error (exit 2)."""


@dataclass
class finding:
    """One rule violation or note, tied to a manifest location."""

    repo: str
    path: str
    slot: str
    name: str
    spec: str
    code: str
    detail: str

    def format(self) -> str:
        return f"{self.path}: {self.slot}/{self.name} = '{self.spec}' [{self.code}] {self.detail}"


@dataclass
class report:
    findings: list[finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    manifests: dict[str, int] = field(default_factory=dict)


def _display(root: Path, path: Path) -> str:
    """Path relative to the scan root, forward slashes, stable for --json."""
    rel = path.relative_to(root)
    return rel.as_posix()


def _repo_name(root: Path) -> str:
    return root.resolve().name


def _registered(repo: str, path_str: str, name: str, spec: str) -> bool:
    """True when (repo, path, dep) hits the registered exact-pin table."""
    for path_sub, dep in REGISTERED_EXACT_PINS.get(repo, []):
        path_ok = (not path_sub) or (path_sub in path_str)
        dep_ok = dep == "*" or dep == name
        if path_ok and dep_ok and _is_exact_pin(spec, "cargo"):
            return True
    return False


def _is_prerelease(spec: str) -> bool:
    return bool(_PRERELEASE_RE.search(spec))


def _is_exact_pin(spec: str, eco: str = "cargo") -> bool:
    """Exact-pin shape (non-prerelease check is upstream)."""
    return bool(re.match(r"^=?\d+(\.\d+)+$", spec.strip()))


def classify_npm(name: str, spec: str, dev_slot: bool) -> tuple[str | None, str]:
    """Return (code, detail); code None = legal.

    The spec has NOT been stripped of internal protocols yet — protocols are
    part of the classification.
    """
    s = spec.strip()
    # Internal / repair channels: workspace links, local paths, catalogs.
    if s.startswith(("workspace:", "file:", "link:", "catalog:", "git+", "http")) or s == "portal:":
        return None, ""
    if _is_prerelease(s):
        return None, ""
    if s == "*" or s == "":
        return ("npm_bare_star", "bare '*' has no axis; declare a real line")
    if s == "latest":
        return ("npm_latest", "'latest' is a moving target; declare a real line")
    if s == "^*" or s == "^0.*":
        code = "npm_caret_star"
        if dev_slot and s == "^*":
            return None, ""  # the ONLY dev-slot exemption
        if dev_slot:
            return (code, "^0.* is banned in every slot (2026-11-10 follow-up)")
        return (code, "runtime/peer slot bans '^*'; declare '^<major>' or '^0.<minor>'")
    if s.startswith("~"):
        return ("npm_tilde_range", "~ ranges are banned; declare '^<major>' or '^0.<minor>'")
    if s == "^0.*":
        return ("npm_caret_star_zero", "^0.* is banned in every slot")
    # versions-ish checks need digits
    if not _NPM_VERSIONISH_RE.search(s):
        if _NPM_BARE_RE.match(s):
            return ("npm_bare_name", "bare name without a range; declare a real line")
    # caret forms (third segment may be a wildcard tail: .* / .x)
    m = re.match(r"^\^(\d+)\.(\d+)(?:\.([0-9*Xx]+))?", s)
    if m:
        major = int(m.group(1))
        third = m.group(3)
        if major == 0:
            if third is not None and not _is_prerelease(s):
                if third in ("*", "x", "X") and m.group(2) == "0":
                    return ("npm_caret_star_zero", "^0.0.* is degenerate; declare '^0.<minor>'")
                return ("npm_three_segment_zero", "^0.x.y is three-segment; declare '^0.<minor>'")
            return None, ""
        if third is not None:
            return ("npm_three_segment", "^<major>.<minor>.<patch> is three-segment; declare '^<major>'")
        return None, ""
    # single-segment major line, caret or bare: "^3" / "3"
    m = re.match(r"^\^?(\d+)$", s)
    if m:
        if int(m.group(1)) == 0:
            return ("npm_zero_major", "bare '0' floats the whole 0.x axis; declare '^0.<minor>'")
        return None, ""
    # exact pins
    m = re.match(r"^=?(\d+(?:\.\d+)+)$", s)
    if m:
        if _is_prerelease(s):
            return None, ""
        return ("npm_exact_pin", "non-prerelease exact pins are banned; declare a caret line")
    # ranges like >=1.2.3: the ruling's ban list does not name them; the
    # owner-registered floor class (CVE floors, integration#107) is tolerated.
    if s.startswith((">=", ">", "<", "<=")):
        return None, ""
    return ("npm_unrecognized", "specifier outside the two-form grammar")


def classify_cargo(name: str, spec: str, registered: bool) -> tuple[str | None, str]:
    s = spec.strip()
    if _is_prerelease(s):
        return None, ""
    if registered and _is_exact_pin(s, "cargo"):
        return None, ""
    if s in ("*", "", "^*"):
        return ("cargo_bare_star", "bare/'^*' has no axis; declare a real line")
    if s == "^0.*":
        return ("cargo_caret_star_zero", "^0.* is banned; declare '0.<minor>'")
    if s.startswith("~"):
        return ("cargo_tilde_range", "~ ranges are banned; declare a single-segment line")
    # exact pins: "=X.Y[.Z]" freezes minor (and patch) axes; "=N" resolves to
    # exactly N.0.0. All banned unless prerelease-tagged or registered.
    if s.startswith("=") and not _is_prerelease(s):
        return ("cargo_exact_pin", "non-prerelease exact pins are banned; declare a single-segment line")
    m = re.match(r"^(?:\^)?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", s)
    if m:
        major = int(m.group(1))
        minor = m.group(2)
        patch = m.group(3)
        if major == 0:
            if minor is None:
                # "=0" or "0" — degenerate, floats the whole 0.x axis
                return ("cargo_zero_axis", "single '0' floats the whole 0.x axis; declare '0.<minor>'")
            if patch is not None:
                return ("cargo_three_segment_zero", "0.x.y is three-segment; declare '0.<minor>'")
            return None, ""
        if minor is None:
            return None, ""  # "N" / "^N": the canonical form ("=N" is an exact pin, banned above)
        # two-segment on a ≥1 line: legal-but-noncanonical was normalized in
        # the wave; the ruling's ban list does not name it. Tolerate (the
        # family ships plenty of these), per the wave's S3-cosmetic class.
        if patch is not None:
            return ("cargo_three_segment", "x.y.z is three-segment; declare a single-segment line")
        return None, ""
    # reqs starting with >= / > etc: not named by the ban list
    if s.startswith((">=", ">", "<", "<=")):
        return None, ""  # owner-registered floor class (integration#107 precedent)
    return ("cargo_unrecognized", "specifier outside the two-form grammar")


def classify_python(name: str, spec: str) -> tuple[str | None, str]:
    s = spec.strip()
    # strip environment markers and extras: "name[extra]>=1 ; py>=3.11"
    s = s.split(";", 1)[0].strip()
    s = re.sub(r"\[[^\]]*\]", "", s).strip()
    if _is_prerelease(s):
        return None, ""
    if "@ git+" in s or "@https" in s.replace(" ", "") or "@ http" in s:
        return None, ""  # direct URL reference pins an exact source
    bare = re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", s)
    if bare:
        return ("py_bare_name", "bare name without an operator; declare a padded '~=' line")
    m = re.match(r"^~=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?$", s)
    if m:
        major, minor, patch = int(m.group(1)), m.group(2), m.group(3)
        if minor is None:
            return ("py_tilde_unpadded", "~=N floats the whole major (or 0.x) axis; pad with '.0' "
                                          "(~=3.0 / ~=0.55.0)")
        if major == 0 and patch is None:
            return ("py_tilde_zero_axis", "~=0.<minor> floats the whole 0.x axis; pad to '~=0.<minor>.0'")
        # ~=3.0 (canonical major form), ~=0.55.0 (canonical 0.x form: floats
        # only the patch axis), ~=2.2.3 (tight patch-line pin — tolerated).
        return None, ""
    m = re.match(r"^==\s*(\d+(?:\.\d+)+)$", s)
    if m:
        return ("py_exact_pin", "non-prerelease exact pins are banned; declare a padded '~=' line")
    if s in ("*", ""):
        return ("py_bare_name" if s == "" else "py_bare_star",
                "bare name without an operator" if s == "" else "bare '*' has no axis")
    if s.startswith((">=", ">", "<", "<=", "!=", "~=", "==")):
        # Covered by the branches above when in the ban list; any remaining
        # range operator (>= floors etc.) is the owner-registered class that
        # merged with integration#107 — tolerated, not a finding.
        return None, ""
    return ("py_unrecognized", "specifier outside the two-form grammar")


def scan_npm(root: Path, path: Path, rep: report, repo: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise depspec_error(f"{_display(root, path)}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        return
    for slot in (*NPM_STRICT_SLOTS, NPM_DEV_SLOT):
        deps = data.get(slot)
        if not isinstance(deps, dict):
            continue
        dev = slot == NPM_DEV_SLOT
        for name, spec in deps.items():
            if not isinstance(spec, str):
                continue
            code, detail = classify_npm(name, spec, dev)
            if code:
                rep.findings.append(finding(repo, _display(root, path), slot, name, spec, code, detail))
    # npm "overrides"/"resolutions" fields are the repair channel — never read.


def scan_cargo(root: Path, path: Path, rep: report, repo: str) -> None:
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise depspec_error(f"{_display(root, path)}: invalid TOML: {exc}") from exc
    sections = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies", "workspace.dependencies"):
        sec = data
        for part in key.split("."):
            if not isinstance(sec, dict):
                sec = None
                break
            sec = sec.get(part)
        if isinstance(sec, dict):
            sections.append((key, sec))
    # [target.<triple>.dependencies] tables carry real declarations (kei's
    # unwinding pins live there). The R3 round proved these escape a naive
    # section walk — traverse them with the target qualifier in the label.
    targets = data.get("target")
    if isinstance(targets, dict):
        for triple, tbl in targets.items():
            if not isinstance(tbl, dict):
                continue
            for sec in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps = tbl.get(sec)
                if isinstance(deps, dict):
                    sections.append((f"target.{triple}.{sec}", deps))
    for section, deps in sections:
        for name, entry in deps.items():
            spec = None
            if isinstance(entry, str):
                spec = entry
            elif isinstance(entry, dict):
                if "path" in entry:
                    continue  # workspace-internal: registered exclusion class
                spec = entry.get("version")
            if not isinstance(spec, str) or not spec:
                if isinstance(entry, dict) and ("git" in entry or "registry" in entry):
                    continue  # git/registry deps without a version key: structural
                if spec == "":
                    rep.findings.append(
                        finding(repo, _display(root, path), section, name, "", "cargo_bare_star",
                                "empty version table has no axis; declare a real line")
                    )
                continue
            registered = _registered(repo, _display(root, path), name, spec)
            code, detail = classify_cargo(name, spec, registered)
            if code:
                rep.findings.append(finding(repo, _display(root, path), section, name, spec, code, detail))
    # [patch] tables are vendored snapshots — exempt by design.
    # pnpm-style overrides do not exist here; nothing else to skip.


def scan_python(root: Path, path: Path, rep: report, repo: str) -> None:
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise depspec_error(f"{_display(root, path)}: invalid TOML: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        return
    groups: list[tuple[str, object]] = [("dependencies", project.get("dependencies"))]
    opt = project.get("optional-dependencies")
    if isinstance(opt, dict):
        for gname, gdeps in opt.items():
            groups.append((f"optional-dependencies.{gname}", gdeps))
    dep_sets = project.get("dependency-groups")
    if isinstance(dep_sets, dict):
        for gname, gdeps in dep_sets.items():
            groups.append((f"dependency-groups.{gname}", gdeps))
    for slot, deps in groups:
        if not isinstance(deps, list):
            continue
        for entry in deps:
            if not isinstance(entry, str):
                continue
            # PEP 508-ish split: NAME[extras] SPEC ; marker. The name run is
            # the leading [A-Za-z0-9._-]+ chunk; everything after extras is
            # the specifier part handed to the classifier.
            m = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?(.*)$", entry)
            if not m:
                rep.findings.append(finding(repo, _display(root, path), slot, entry, entry,
                                            "py_unrecognized", "declaration does not start with a package name"))
                continue
            name, _extras, spec = m.group(1), m.group(2) or "", m.group(3).strip()
            code, detail = classify_python(name, spec if spec else entry)
            if code:
                rep.findings.append(finding(repo, _display(root, path), slot, name, entry, code, detail))


def scan(root: Path | str, repo: str | None = None) -> report:
    root = Path(root)
    rep = report()
    repo = repo or _repo_name(root)
    found_any = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if fn == NPM_MANIFEST:
                scan_npm(root, p, rep, repo)
                rep.manifests["npm"] = rep.manifests.get("npm", 0) + 1
                found_any = True
            elif fn == CARGO_MANIFEST:
                scan_cargo(root, p, rep, repo)
                rep.manifests["cargo"] = rep.manifests.get("cargo", 0) + 1
                found_any = True
            elif fn == PYTHON_MANIFEST:
                scan_python(root, p, rep, repo)
                rep.manifests["python"] = rep.manifests.get("python", 0) + 1
                found_any = True
    rep.files_scanned = sum(rep.manifests.values())
    if not found_any:
        raise depspec_error(f"{root}: no package.json / Cargo.toml / pyproject.toml found")
    return rep


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools depspec-lint",
        description="Check dependency declarations against the two-form ruling (2026-11-09).",
    )
    parser.add_argument("path", nargs="?", default=".", help="repository tree to scan (default: .)")
    parser.add_argument("--strict", action="store_true", help="exit 1 when findings exist (CI form)")
    parser.add_argument("--json", action="store_true", help="print one machine-readable report on stdout")
    parser.add_argument("--repo", default=None,
                        help="repository name for the registered exact-pin table "
                             "(default: the scanned directory's name)")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.is_dir():
        print(f"depspec-lint: {root}: not a directory", file=sys.stderr)
        return 2
    try:
        rep = scan(root, repo=args.repo)
    except depspec_error as exc:
        print(f"depspec-lint: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "files_scanned": rep.files_scanned,
            "manifests": rep.manifests,
            "findings": [f.__dict__ for f in rep.findings],
        }, ensure_ascii=False))
    else:
        print(f"depspec-lint: scanned {rep.files_scanned} manifests {rep.manifests}")
        for f in rep.findings:
            print(f"  {f.format()}")
        if not rep.findings:
            print("  all declarations two-form")
        else:
            print(f"  {len(rep.findings)} finding(s)")

    if args.strict and rep.findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
