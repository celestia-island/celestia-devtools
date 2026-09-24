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
)

#: `(npm package, canonical version, why)` — informational: the workspace rules
#: currently mandate `^*` for family packages, so an unbounded range warns
#: (showing what the audit measured) instead of failing.
NPM_LAYERS: tuple[tuple[str, Version, str], ...] = (
    ("@celestia-island/hikari", (0, 55, 0), "UI component library (Layer 2)"),
)

UNBOUNDED = {"", "*", "^*", "x", "X", "latest", ">=0.0.0"}
#: `file:` is a sibling-directory dependency — retired family-wide (§3.5).
SKIP_PROTOCOLS = ("workspace:", "catalog:", "link:workspace", "portal:")
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
        elif minor:
            high = (0, minor + 1, 0)
        else:
            high = (1, 0, 0)  # ^0 / ^0.* admits every 0.x
    elif operator == "~":
        high = (major, (minor or 0) + 1, 0)
    elif operator == ">=":
        return low, (10**9, 0, 0)
    elif operator == ">":
        return (major, minor or 0, (patch or 0) + 1), (10**9, 0, 0)
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
    if text.startswith("||"):
        return None
    low: Version = (0, 0, 0)
    high: Version = (10**9, 0, 0)
    for part in text.split(","):
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
    for section in ("patch", "replace"):
        tables = document.get(section)
        if isinstance(tables, dict):
            for source, table in tables.items():
                if isinstance(table, dict):
                    yield from (
                        (key, spec, f"{section}.{source}") for key, spec in table.items()
                    )


def _crate_name(key: str, spec: object) -> str:
    if isinstance(spec, dict) and isinstance(spec.get("package"), str):
        return spec["package"]
    return key


def _check_cargo(path: Path, root: Path, findings: list[Finding], repo: str) -> None:
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
            findings.append(Finding(
                "warning", subject,
                f"tracks {spec.get('branch') or spec.get('rev') or 'a git source'}; "
                f"crates.io {canonical[0]}.{canonical[1]} is the family floor",
            ))
            continue
        requirement = spec.get("version")
        if not isinstance(requirement, str):
            findings.append(Finding("violation", subject, "no version requirement"))
            continue
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


def _npm_value(table: object, package: str) -> str | None:
    if not isinstance(table, dict):
        return None
    for key, value in table.items():
        if key == package:
            return value if isinstance(value, str) else None
        # npm:@scope/pkg@range aliases the package under another name.
        if isinstance(value, str) and value.startswith(f"npm:{package}@"):
            return value
    return None


def _check_npm(path: Path, root: Path, findings: list[Finding]) -> None:
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
            requirement = _npm_value(table, package)
            if requirement is None:
                continue
            subject = f"{package} @ {path.relative_to(root)} ({section})"
            if requirement.startswith("file:"):
                findings.append(Finding(
                    "violation", subject,
                    f"declares {requirement!r} — a sibling-directory dependency, retired "
                    "family-wide in favour of the published package",
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
            verdict = intersects(requirement, canonical, (canonical[0], canonical[1] + 1, 0))
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


def check(root: Path) -> list[Finding]:
    """Every family-consumption finding in ``root``, violations first."""
    root = Path(root).resolve()
    findings: list[Finding] = []
    repo = _repo_name(root)
    for path in _manifests(root, "Cargo.toml"):
        _check_cargo(path, root, findings, repo)
    for path in _manifests(root, "package.json"):
        _check_npm(path, root, findings)
    order = {"violation": 0, "warning": 1}
    return sorted(findings, key=lambda f: (order[f.level], f.subject, f.message))


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

    findings = check(root)
    violations = [f for f in findings if f.level == "violation"]
    warnings = [f for f in findings if f.level == "warning"]

    if args.json:
        print(json.dumps({
            "repo": str(root),
            "findings": [asdict(f) for f in findings],
            "violations": len(violations),
            "warnings": len(warnings),
        }, indent=2, ensure_ascii=False))
    else:
        for finding in findings:
            stream = sys.stderr if finding.level == "violation" else sys.stdout
            print(f"{finding.level:9s} {finding.subject}: {finding.message}", file=stream)
        if not findings:
            print("ok: every family layer is consumed as one identity")
        else:
            print(
                f"{len(violations)} violation(s), {len(warnings)} warning(s)",
                file=sys.stderr if violations else sys.stdout,
            )

    if violations or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
