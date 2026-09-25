#!/usr/bin/env python3
"""Check that the family's shared layers are consumed as ONE identity.

The family shares three upstream layers — `kirino` (auth primitives),
`plana` (platform/RPC), `hikari` (UI) — and until 2026-09-25 nothing compared
how the repositories consume them.  The workspace audit then found the result:

* **`kirino` at two incompatible majors in one product surface**: `^0.7` in
  plana/shittim-chest, `^0.6` in e.celestia.world / erp.celestia.world /
  evernight (`kirino` and `kirino-session`).  `^0.6` and `^0.7` do not overlap
  (0.x carets pin the minor), so the family ships two generations of the auth
  primitive whose tokens its services exchange.
* **`plana` consumed as `branch = "master"`** and resolved to seven different
  commits across repositories, so the version string `0.2.1` names seven
  different trees.
* **`@celestia-island/hikari` declared `^*`** in four consumers — a range
  node-semver reads as `*`, i.e. any future major, in a 0.x line where every
  minor is breaking by the project's own experience.

This command reports those shapes and fails on the ones the workspace rules
already require.  The judgement is "does this requirement ADMIT the family's
canonical version?", not "what is its floor": `>=0.6, <1.0` and `^0.*` both
admit `0.7`, and reading a floor out of them is how a checker starts lying.

Exit status: 0 clean, 1 violations (or warnings with ``--strict``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib

import yaml
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

Version = tuple[int, int, int]

PLANA_REPO = "https://github.com/celestia-island/plana.git"
#: Repositories the freeze list allows to pin a `plana` revision.  Everyone else
#: tracks master so the family at least shares one moving target instead of
#: seven stationary ones.
REV_PIN_ALLOWED = frozenset({"scriptum", "aris"})

#: `(crate-family prefix, canonical version, why)` for the Rust layers.  A crate
#: belongs to the family when its name — with `-` read as `_` — is the prefix
#: itself or starts with `prefix_`; that is what catches `kirino-session` and
#: `plana-jsonrpc`, which is where the real consumption lives.
RUST_LAYERS: tuple[tuple[str, Version, str], ...] = (
    ("kirino", (0, 7, 0), "auth primitives: JWT/RBAC/sessions (Layer 0)"),
    ("plana", (0, 2, 0), "platform: JSON-RPC, RPC server/client, shared types (Layer 1)"),
    # The Rust track of the UI layer is 0.3.x while the npm binding is 0.55.x;
    # without it a `[patch."…/hikari.git"] hikari-* = { path = "../hikari/…" }`
    # (a cross-repository path dependency the rules forbid) reads as unknown.
    ("hikari", (0, 3, 0), "UI components, Rust track (Layer 2)"),
)

#: `crate family → repository` for the git-source check.
FAMILY_REPOS = {"kirino": "kirino", "plana": "plana", "hikari": "hikari"}

#: `(npm package, canonical version, why)` — informational: the workspace rules
#: currently mandate `^*` for family packages, so an unbounded range warns
#: (showing what the audit measured) instead of failing.
NPM_LAYERS: tuple[tuple[str, Version, str], ...] = (
    # (package, the line that package is actually published on, why). The npm
    # binding has its own version track: `@celestia-island/kirino` is published
    # at 0.6.x while the Rust crate is 0.7.x, so comparing the npm declaration
    # against the CRATE's line would invent violations.
    ("@celestia-island/hikari", (0, 55, 0), "UI component library (Layer 2)"),
    ("@celestia-island/kirino", (0, 6, 0), "auth bindings (Layer 0, npm track)"),
    ("@celestia-island/plana-types", (0, 1, 0), "shared protocol types (Layer 1, npm track)"),
    ("@celestia-island/plana-rpc-client", (0, 1, 0), "RPC client (Layer 1, npm track)"),
)

#: `layer → (rust crate family, npm package)` for the cross-track check: the same
#: layer shipped on two tracks has to agree on which generation it is.
CROSS_TRACK = (
    ("kirino", "kirino", "@celestia-island/kirino"),
)

UNBOUNDED = {"", "*", "^*", "x", "X", "latest", ">=0.0.0"}
#: Sibling-directory dependencies — retired family-wide (§3.5). `link:` and
#: `portal:` are the same shape as `file:` (a path outside the package), so they
#: are judged the same way instead of one warning and one silence.
SIBLING_PROTOCOLS = ("file:", "link:", "portal:")
#: Resolution the package manager owns: not a family-version decision.
SKIP_PROTOCOLS = ("workspace:", "catalog:")
SKIP_DIRS = {"node_modules", "target", "dist", ".git", ".generated", "vendor"}

CARGO_DEP_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
NPM_DEP_SECTIONS = (
    "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
)


@dataclass(frozen=True)
class Finding:
    level: str  # "violation" | "warning"
    subject: str
    message: str


# ── requirement semantics ────────────────────────────────────────────────────


def _parse_version(text: str) -> tuple[int, int | None, int | None] | None:
    """`0.7`, `0.7.1`, `0.7.*`, `0` → (major, minor, patch); None = unparsable."""
    match = re.match(r"^v?(\d+)(?:\.(\d+|\*|x|X))?(?:\.(\d+|\*|x|X))?$", text.strip())
    if not match:
        return None
    parts: list[int | None] = []
    for group in match.groups():
        if group is None or group in ("*", "x", "X"):
            parts.append(None)
        else:
            parts.append(int(group))
    while len(parts) < 3:
        parts.append(None)
    return parts[0], parts[1], parts[2]  # type: ignore[return-value]


def _hyphen_bounds(first: str, last: str) -> tuple[Version, Version] | None:
    """`1.2.3 - 2.3.4` — both ends parse as versions, the upper end inclusive."""
    low = _parse_version(first)
    high = _parse_version(last)
    if low is None or high is None:
        return None
    lower = (low[0], low[1] or 0, low[2] or 0)
    if high[2] is not None:
        upper = (high[0], high[1] or 0, high[2] + 1)
    elif high[1] is not None:
        upper = (high[0], high[1] + 1, 0)
    else:
        upper = (high[0] + 1, 0, 0)
    return lower, upper


def _bounds(part: str) -> tuple[Version, Version] | None:
    """The `[lower, upper)` interval one comma-separated comparator admits."""
    part = part.strip()
    if part in UNBOUNDED:
        return (0, 0, 0), (10**9, 0, 0)
    operator = ""
    for candidate in (">=", "<=", "^", "~", ">", "<", "="):
        if part.startswith(candidate):
            operator, part = candidate, part[len(candidate):].strip()
            break
    parsed = _parse_version(part)
    if parsed is None:
        return None
    major, minor, patch = parsed
    low: Version = (major, minor or 0, patch or 0)
    if operator == "^":
        if major > 0:
            high = (major + 1, 0, 0)
        elif minor is not None and minor > 0:
            high = (0, minor + 1, 0)
        elif patch is not None:
            high = (0, 0, patch + 1)  # ^0.0.3 is >=0.0.3 <0.0.4
        elif minor is not None:
            high = (0, 1, 0)  # ^0.0 is >=0.0.0 <0.1.0
        else:
            high = (1, 0, 0)  # ^0 / ^0.x admits every 0.x
    elif operator == "~":
        # `~1` is `>=1.0.0 <2.0.0`; only `~1.2` pins the minor.
        high = (major, minor + 1, 0) if minor is not None else (major + 1, 0, 0)
    elif operator == ">=":
        return low, (10**9, 0, 0)
    elif operator == ">":
        # node-semver: `>1.2.3` is `>=1.2.4`, `>1.2` is `>=1.3.0`, `>1` is `>=2.0.0`
        if patch is not None:
            return (major, minor or 0, patch + 1), (10**9, 0, 0)
        if minor is not None:
            return (major, minor + 1, 0), (10**9, 0, 0)
        return (major + 1, 0, 0), (10**9, 0, 0)
    elif operator == "<":
        return (0, 0, 0), low
    elif operator == "<=":
        return (0, 0, 0), (major, minor or 0, (patch or 0) + 1)
    elif minor is None:
        high = (major + 1, 0, 0)
    elif patch is None:
        high = (major, minor + 1, 0)
    else:
        high = (major, minor, patch + 1)
    return low, high


def intersects(requirement: str, low: Version, high: Version) -> bool | None:
    """Whether ``requirement`` can resolve to SOME version in ``[low, high)``.

    This is the question that matters: `^0.55.71` is a perfectly good member of
    the 0.55 line even though it cannot resolve to `0.55.0` exactly.
    """
    bounds = _requirement_bounds(requirement)
    if bounds is None:
        return None
    return max(bounds[0], low) < min(bounds[1], high)


def admits(requirement: str, target: Version) -> bool | None:
    """Whether ``requirement`` can resolve to ``target``.  None = cannot tell."""
    bounds = _requirement_bounds(requirement)
    if bounds is None:
        return None
    return bounds[0] <= target < bounds[1]


def _requirement_bounds(requirement: str) -> tuple[Version, Version] | None:
    """The `[lower, upper)` interval a whole requirement admits; None = unknown."""
    text = requirement.strip().strip("'\"")
    if text.startswith(("file:", "workspace:", "catalog:", "link:", "portal:")):
        return None
    if text.startswith("npm:"):
        text = text[len("npm:"):].rpartition("@")[2] or "*"  # npm:alias@range → range
    # `a || b` and `a - b` are unions: the requirement admits anything either
    # alternative admits, so the intervals are merged rather than intersecting.
    if "||" in text or " - " in text:
        merged: tuple[Version, Version] | None = None
        alternatives = text.split("||") if "||" in text else [text]
        for alternative in alternatives:
            if " - " in alternative:
                first, _, last = alternative.partition(" - ")
                bounds = _hyphen_bounds(first, last)
            else:
                bounds = _requirement_bounds(alternative)
            if bounds is None:
                return None
            merged = bounds if merged is None else (
                min(merged[0], bounds[0]), max(merged[1], bounds[1])
            )
        return merged
    low: Version = (0, 0, 0)
    high: Version = (10**9, 0, 0)
    for part in re.split(r"[,\s]+", text):
        if "||" in part:
            return None
        if not part.strip():
            continue
        bounds = _bounds(part)
        if bounds is None:
            return None
        low = max(low, bounds[0])
        high = min(high, bounds[1])
    return low, high


# ── scanning ─────────────────────────────────────────────────────────────────


def _manifests(root: Path, name: str) -> Iterable[Path]:
    """Every manifest under ``root``, pruned WHILE walking.

    Pruning matters twice over: `node_modules` of a frontend checkout is tens of
    thousands of directories, and a `SKIP_DIRS` test against the absolute path
    silently skips a repository that merely *lives* under a directory called
    `target` or `vendor`.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                stack.append(entry)
            elif entry.name == name:
                yield entry


def _repo_name(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=30,
        )
        url = out.stdout.strip()
        if url:
            return Path(url).name.removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return root.name


def _family_crate(name: str) -> tuple[str, Version, str] | None:
    normalized = name.replace("-", "_")
    for prefix, canonical, why in RUST_LAYERS:
        if normalized == prefix or normalized.startswith(f"{prefix}_"):
            return prefix, canonical, why
    return None


def _cargo_deps(document: dict) -> Iterable[tuple[str, object, str]]:
    """`(key, spec, section-label)` for every dependency table we can see."""
    for section in CARGO_DEP_SECTIONS:
        table = document.get(section)
        if isinstance(table, dict):
            yield from ((key, spec, section) for key, spec in table.items())
    workspace = document.get("workspace")
    if isinstance(workspace, dict):
        table = workspace.get("dependencies")
        if isinstance(table, dict):
            yield from ((key, spec, "workspace.dependencies") for key, spec in table.items())
    targets = document.get("target")
    if isinstance(targets, dict):
        for cfg, tables in targets.items():
            if isinstance(tables, dict):
                for section in CARGO_DEP_SECTIONS:
                    table = tables.get(section)
                    if isinstance(table, dict):
                        yield from (
                            (key, spec, f"target.{cfg}.{section}")
                            for key, spec in table.items()
                        )
    patches = document.get("patch")
    if isinstance(patches, dict):
        for source, table in patches.items():
            if isinstance(table, dict):
                yield from ((key, spec, f"patch.{source}") for key, spec in table.items())
    # `[replace]` is flat: "name:version" = { path = … } (Cargo's older spelling
    # of a patch). Reading it as patch-shaped made the whole section dead code.
    replaces = document.get("replace")
    if isinstance(replaces, dict):
        for key, spec in replaces.items():
            yield key.split(":")[0], spec, "replace"


def _crate_name(key: str, spec: object) -> str:
    if isinstance(spec, dict) and isinstance(spec.get("package"), str):
        return spec["package"]
    return key


def _check_cargo(path: Path, root: Path, findings: list[Finding], repo: str,
                 rust_lines: dict[str, set[tuple[int, int]]] | None = None) -> None:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover - unreadable manifest
        findings.append(Finding("warning", str(path.relative_to(root)), f"unreadable: {exc}"))
        return
    for key, raw_spec, section in _cargo_deps(document):
        name = _crate_name(key, raw_spec)
        family = _family_crate(name)
        if family is None:
            continue
        prefix, canonical, _why = family
        spec = raw_spec if isinstance(raw_spec, dict) else {"version": raw_spec}
        subject = f"{name} @ {path.relative_to(root)} ({section})"
        if spec.get("workspace") is True:
            # The requirement lives at the workspace root, which this scan also
            # reads; reporting the inheritance site would flag the same
            # declaration twice.
            continue
        if "path" in spec:
            target = (path.parent / str(spec["path"])).resolve()
            if target == root or root in target.parents:
                continue  # inside this repository: not a cross-repo dependency
            findings.append(Finding(
                "violation", subject,
                f"local path dependency to {spec['path']!r} outside this repository; "
                "cross-repo Rust dependencies must be git references on master",
            ))
            continue
        if prefix == "plana":
            if "git" not in spec:
                findings.append(Finding(
                    "violation", subject,
                    f"consumed from crates.io; the family consumes plana from {PLANA_REPO}",
                ))
                continue
            url = str(spec.get("git", ""))
            if "celestia-island/plana" not in url:
                findings.append(Finding(
                    "violation", subject,
                    f"git source {url!r} is not the family's {PLANA_REPO}",
                ))
                continue
            if "rev" in spec:
                if repo not in REV_PIN_ALLOWED:
                    findings.append(Finding(
                        "violation", subject,
                        f"pins plana to rev {spec['rev']!r}; only the frozen repositories "
                        f"({', '.join(sorted(REV_PIN_ALLOWED))}) may pin",
                    ))
            elif spec.get("branch") != "master":
                findings.append(Finding(
                    "violation", subject,
                    f"tracks {spec.get('branch') or spec.get('tag') or 'an unnamed ref'}; "
                    'the family tracks branch = "master"',
                ))
            continue
        # kirino and friends: one major, or it is a different primitive.
        if "git" in spec:
            # §3.3 prescribes git references on master for cross-repo Rust
            # dependencies, and entelecheia consumes kirino that way on purpose —
            # so the consumption MODE is not a finding. Which repository it
            # points at, and which ref it follows, still is: `plana` was checked
            # and `kirino` was not, which let `{ git = "…/other/kirino.git",
            # branch = "0.4-legacy" }` through.
            url = str(spec.get("git", ""))
            repository = FAMILY_REPOS.get(prefix, prefix)
            if f"celestia-island/{repository}" not in url:
                findings.append(Finding(
                    "violation", subject,
                    f"git source {url!r} is not the family's "
                    f"https://github.com/celestia-island/{repository}.git",
                ))
                continue
            if "rev" in spec and repo not in REV_PIN_ALLOWED:
                findings.append(Finding(
                    "violation", subject,
                    f"pins {prefix} to rev {spec['rev']!r}; only the frozen repositories "
                    f"({', '.join(sorted(REV_PIN_ALLOWED))}) may pin",
                ))
            elif "rev" not in spec and spec.get("branch") not in (None, "master"):
                findings.append(Finding(
                    "violation", subject,
                    f"tracks {spec.get('branch') or spec.get('tag')!r}; the family tracks "
                    'branch = "master"',
                ))
            continue
        requirement = spec.get("version")
        if not isinstance(requirement, str):
            findings.append(Finding("violation", subject, "no version requirement"))
            continue
        if rust_lines is not None and _parse_version(requirement.lstrip("^~>=< ")):
            parsed = _parse_version(requirement.lstrip("^~>=< "))
            if parsed and parsed[1] is not None:
                rust_lines.setdefault(prefix, set()).add((parsed[0], parsed[1]))
        line = (canonical[0], canonical[1] + 1, 0)
        verdict = intersects(requirement, canonical, line)
        if verdict is False:
            findings.append(Finding(
                "violation", subject,
                f"declares {requirement}, which cannot resolve to the family's "
                f"{canonical[0]}.{canonical[1]} line; 0.x carets do not overlap, so this "
                "is a different primitive, not an older build",
            ))
        elif verdict is None:
            findings.append(Finding(
                "warning", subject, f"cannot judge the requirement {requirement!r}",
            ))


def _npm_values(table: object, package: str) -> list[str]:
    """Every declaration of ``package`` in one table.

    All of them, not the first: `{"hk": "npm:<pkg>@^0.55", "<pkg>": "file:../x"}`
    declares the package twice, and returning on the alias hid the `file:`
    violation behind it.
    """
    if not isinstance(table, dict):
        return []
    found: list[str] = []
    for key, value in table.items():
        if isinstance(value, dict):
            # pnpm writes a nested override as `{"<pkg>": {".": "range"}}`.
            if key != package:
                continue
            for nested_key in (".", "", "*"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    found.append(nested)
            continue
        if not isinstance(value, str):
            continue
        if key == package or value.startswith(f"npm:{package}@"):
            found.append(value)
    return found


def _check_npm(path: Path, root: Path, findings: list[Finding],
               npm_lines: dict[str, set[tuple[int, int]]] | None = None) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - unreadable manifest
        findings.append(Finding("warning", str(path.relative_to(root)), f"unreadable: {exc}"))
        return
    tables: list[tuple[str, object]] = [
        (section, document.get(section)) for section in NPM_DEP_SECTIONS
    ]
    tables.append(("overrides", document.get("overrides")))
    pnpm = document.get("pnpm")
    if isinstance(pnpm, dict):
        tables.append(("pnpm.overrides", pnpm.get("overrides")))
    tables.append(("resolutions", document.get("resolutions")))
    for package, canonical, _why in NPM_LAYERS:
        for section, table in tables:
            for requirement in _npm_values(table, package):
                subject = f"{package} @ {path.relative_to(root)} ({section})"
                if requirement.startswith(SIBLING_PROTOCOLS):
                    findings.append(Finding(
                        "violation", subject,
                        f"declares {requirement!r} — a sibling-directory dependency, "
                        "retired family-wide in favour of the published package",
                    ))
                    continue
                if requirement.startswith(SKIP_PROTOCOLS):
                    continue
                if requirement.strip() in UNBOUNDED:
                    findings.append(Finding(
                        "warning", subject,
                        f"declares {requirement!r}, which node-semver reads as `*` — any "
                        "future major is admitted on a fresh resolve",
                    ))
                    continue
                if npm_lines is not None:
                    parsed = _parse_version(requirement.lstrip("^~>=< "))
                    if parsed and parsed[1] is not None:
                        npm_lines.setdefault(package, set()).add((parsed[0], parsed[1]))
                verdict = intersects(
                    requirement, canonical, (canonical[0], canonical[1] + 1, 0)
                )
                if verdict is False:
                    findings.append(Finding(
                        "warning", subject,
                        f"declares {requirement}, which cannot resolve to the family's "
                        f"{canonical[0]}.{canonical[1]} line",
                    ))
                elif verdict is None:
                    findings.append(Finding(
                        "warning", subject, f"cannot judge the requirement {requirement!r}",
                    ))


def _own_cargo_line(root: Path) -> tuple[int, int] | None:
    """This repository's own Rust version line, from its root manifest."""
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return None
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for table in (document.get("workspace", {}).get("package", {}),
                  document.get("package", {})):
        parsed = _parse_version(str(table.get("version", "")))
        if parsed is not None and parsed[1] is not None:
            return (parsed[0], parsed[1])
    return None


def _published_family_lines(root: Path) -> dict[str, set[tuple[int, int]]]:
    """The family npm packages this repository PUBLISHES, and on which line."""
    published: dict[str, set[tuple[int, int]]] = {}
    names = {package for _crate, _prefix, package in CROSS_TRACK}
    for path in _manifests(root, "package.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = document.get("name")
        parsed = _parse_version(str(document.get("version", "")))
        if name in names and parsed is not None and parsed[1] is not None:
            published.setdefault(name, set()).add((parsed[0], parsed[1]))
    return published


def _workspace_override_findings(root: Path) -> list[Finding]:
    """`pnpm-workspace.yaml` carries `overrides` for the pnpm 11 layout.

    The family moved `pnpm.overrides` there (see the frontend skill), so a check
    that only reads package.json cannot see a forced family version.
    """
    path = root / "pnpm-workspace.yaml"
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - unreadable manifest
        return [Finding("warning", path.name, f"unreadable: {exc}")]
    overrides = document.get("overrides") if isinstance(document, dict) else None
    findings: list[Finding] = []
    for package, canonical, _why in NPM_LAYERS:
        for requirement in _npm_values(overrides, package):
            subject = f"{package} @ pnpm-workspace.yaml (overrides)"
            if requirement.startswith(SIBLING_PROTOCOLS):
                findings.append(Finding(
                    "violation", subject,
                    f"declares {requirement!r} — a sibling-directory dependency, "
                    "retired family-wide in favour of the published package",
                ))
            elif requirement.strip() in UNBOUNDED:
                findings.append(Finding(
                    "warning", subject,
                    f"declares {requirement!r}, which node-semver reads as `*`",
                ))
            elif intersects(requirement, canonical, (canonical[0], canonical[1] + 1, 0)) is False:
                findings.append(Finding(
                    "warning", subject,
                    f"declares {requirement}, which cannot resolve to the family's "
                    f"{canonical[0]}.{canonical[1]} line",
                ))
    return findings


def _lock_findings(root: Path) -> list[Finding]:
    """What the LOCKFILES resolved, which is what actually gets built.

    A declaration can be satisfied by a version the auditor did not expect: four
    consumers declare `^*` and resolve to 0.40.27 / 0.41.3 / 0.41.9 / 0.55.0,
    and one `Cargo.lock` can link three generations of the auth crate while every
    declaration looks fine. These are warnings: a transitive dependency can pin
    the split, so they are evidence to act on rather than a gate.
    """
    findings: list[Finding] = []
    for cargo_lock in _manifests(root, "Cargo.lock"):
        where = cargo_lock.relative_to(root).as_posix()
        if where not in ("Cargo.lock",):
            # A nested lock is a second workspace. Reporting only the root one
            # let a repository whose root locked cleanly while `fuzz/Cargo.lock`
            # held an older generation print `ok` — and that output gets cited
            # as evidence, so it must not be able to miss one.
            findings.append(Finding(
                "info", where,
                "this is a separate workspace's lockfile, checked on its own below"))
        try:
            data = tomllib.loads(cargo_lock.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:  # pragma: no cover
            findings.append(Finding("warning", "Cargo.lock", f"unreadable: {exc}"))
            data = {}
        # Grouped by crate NAME, not by family prefix: `kirino-macro` and
        # `plana-celestia-types` keep their own version tracks, so lumping them
        # in reported a "second generation" that does not exist. What matters is
        # one name resolving to two lines (kirino 0.6.3 + 0.6.5 + 0.7.2 is the
        # real finding).
        lines: dict[str, set[tuple[int, int]]] = {}
        seen: dict[str, set[str]] = {}
        for package in data.get("package", []) or []:
            name = str(package.get("name", ""))
            parsed = _parse_version(str(package.get("version", "")))
            if _family_crate(name) is None or parsed is None or parsed[1] is None:
                continue
            lines.setdefault(name, set()).add((parsed[0], parsed[1]))
            seen.setdefault(name, set()).add(f"{name} {package.get('version')}")
        for crate, versions in sorted(lines.items()):
            if len(versions) > 1:
                findings.append(Finding(
                    "warning", f"{crate} @ {where}",
                    "the lock links "
                    + ", ".join(f"{m}.{n}" for m, n in sorted(versions))
                    + " of the same crate ("
                    + ", ".join(sorted(seen[crate]))
                    + ") — more than one generation in one binary",
                ))
    for lock in _manifests(root, "pnpm-lock.yaml"):
        where = lock.relative_to(root).as_posix()
        try:
            data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:  # pragma: no cover
            findings.append(Finding("warning", where, f"unreadable: {exc}"))
            data = {}
        importers = data.get("importers") if isinstance(data, dict) else None
        resolved: dict[str, dict[str, set[tuple[int, int]]]] = {}
        for importer, tables in (importers or {}).items():
            if not isinstance(tables, dict):
                continue
            for section in NPM_DEP_SECTIONS:
                table = tables.get(section)
                if not isinstance(table, dict):
                    continue
                for package, _canonical, _why in NPM_LAYERS:
                    entry = table.get(package)
                    if not isinstance(entry, dict):
                        continue
                    parsed = _parse_version(str(entry.get("version", "")).split("(")[0])
                    if parsed is None or parsed[1] is None:
                        continue
                    resolved.setdefault(package, {}).setdefault(importer, set()).add(
                        (parsed[0], parsed[1])
                    )
        for package, per_importer in sorted(resolved.items()):
            canonical = next(c for n, c, _why in NPM_LAYERS if n == package)
            lines = {line for found in per_importer.values() for line in found}
            below = sorted(line for line in lines if line < canonical[:2])
            if below:
                findings.append(Finding(
                    "warning", f"{package} @ {where}",
                    "the lock resolves "
                    + ", ".join(f"{m}.{n}" for m, n in below)
                    + f" while the family line is {canonical[0]}.{canonical[1]}",
                ))
            if len(lines) > 1:
                where = ", ".join(
                    f"{imp}: {', '.join(f'{m}.{n}' for m, n in sorted(found))}"
                    for imp, found in sorted(per_importer.items())
                )
                findings.append(Finding(
                    "warning", f"{package} @ {where}",
                    f"the lock resolves {len(lines)} different lines across importers ({where})",
                ))
    return findings


def _scanned_counts(root: Path) -> dict[str, int]:
    """How much was actually read — so `ok` cannot mean "found nothing"."""
    return {
        "Cargo.toml": len(list(_manifests(root, "Cargo.toml"))),
        "package.json": len(list(_manifests(root, "package.json"))),
        "Cargo.lock": len(list(_manifests(root, "Cargo.lock"))),
        "pnpm-lock.yaml": len(list(_manifests(root, "pnpm-lock.yaml"))),
    }


def check(root: Path) -> list[Finding]:
    """Every family-consumption finding in ``root``, violations first."""
    root = Path(root).resolve()
    findings: list[Finding] = []
    repo = _repo_name(root)
    rust_lines: dict[str, set[tuple[int, int]]] = {}
    npm_lines: dict[str, set[tuple[int, int]]] = {}
    for path in _manifests(root, "Cargo.toml"):
        _check_cargo(path, root, findings, repo, rust_lines)
    for path in _manifests(root, "package.json"):
        _check_npm(path, root, findings, npm_lines)
    published = _published_family_lines(root)
    for crate, _prefix, package in CROSS_TRACK:
        rust = rust_lines.get(crate, set())
        npm = npm_lines.get(package, set())
        if rust and npm and not (rust & npm):
            findings.append(Finding(
                "warning", f"{package} (npm) vs {crate} (cargo)",
                "the two tracks of "
                f"{crate} are on different generations (cargo "
                + ", ".join(f"{m}.{n}" for m, n in sorted(rust))
                + " vs npm "
                + ", ".join(f"{m}.{n}" for m, n in sorted(npm))
                + "); the binding lags the crate until it is republished, so a "
                "consumer cannot align on its own",
            ))
        # The actionable side: a repository that PUBLISHES the binding while its
        # own crates have moved on is the one that has to republish. Its own
        # crate line comes from its root manifest — kirino does not declare a
        # dependency on kirino, so the declaration scan sees nothing there.
        ours = rust or ({own} if (own := _own_cargo_line(root)) else set())
        for line in sorted(published.get(package, set())):
            if ours and line not in ours:
                findings.append(Finding(
                    "warning", f"{package} @ published here",
                    "this repository publishes the binding on "
                    + f"{line[0]}.{line[1]} while its Rust crates are on "
                    + ", ".join(f"{m}.{n}" for m, n in sorted(ours))
                    + " — the binding is behind the crates it wraps",
                ))
    findings.extend(_workspace_override_findings(root))
    findings.extend(_lock_findings(root))
    order = {"violation": 0, "warning": 1, "info": 2}
    unique: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.level, finding.subject, finding.message), finding)
    return sorted(unique.values(), key=lambda f: (order[f.level], f.subject, f.message))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools family-versions",
        description="Check that the family's shared layers are consumed as one identity.",
    )
    parser.add_argument(
        "repo", nargs="?", default=".",
        help="repository root to check (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures too")
    args = parser.parse_args(argv)
    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(
            f"::error::family-versions: {root} is not a directory — nothing was "
            "scanned, so there is nothing to report",
            file=sys.stderr,
        )
        return 2
    scanned = _scanned_counts(root)
    if not any(scanned.values()):
        print(
            f"::error::family-versions: no manifest was found under {root} — "
            "an empty scan is not a clean repository",
            file=sys.stderr,
        )
        return 2

    findings = check(root)
    violations = [f for f in findings if f.level == "violation"]
    warnings = [f for f in findings if f.level == "warning"]

    if args.json:
        print(json.dumps({
            "repo": str(root),
            "scanned": scanned,
            "findings": [asdict(f) for f in findings],
            "violations": len(violations),
            "warnings": len(warnings),
        }, indent=2, ensure_ascii=False))
    else:
        for finding in findings:
            stream = sys.stderr if finding.level == "violation" else sys.stdout
            print(f"{finding.level:9s} {finding.subject}: {finding.message}", file=stream)
        summary = ", ".join(f"{count} {name}" for name, count in scanned.items() if count)
        if not findings:
            print(f"ok: every family layer is consumed as one identity ({summary})")
        else:
            print(
                f"{len(violations)} violation(s), {len(warnings)} warning(s) ({summary})",
                file=sys.stderr if violations else sys.stdout,
            )

    if violations or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
