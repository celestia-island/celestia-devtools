#!/usr/bin/env python3
"""Check that the family's shared layers are consumed as ONE identity.

The family shares three upstream layers — `kirino` (auth primitives),
`plana` (platform/RPC), `hikari` (UI) — and until 2026-09-25 nothing compared
how the repositories consume them.  The workspace audit then found the result:

* **`kirino` at two incompatible majors in one product surface**: `^0.7` in
  plana/shittim-chest, `^0.6` in e.celestia.world / erp.celestia.world /
  evernight.  `^0.6` and `^0.7` do not overlap (0.x carets pin the minor), so
  the family ships two generations of the auth primitive whose tokens its
  services exchange.
* **`plana` consumed as `branch = "master"`** and resolved to seven different
  commits across repositories, so the version string `0.2.1` names seven
  different trees.
* **`@celestia-island/hikari` declared `^*`** in four consumers — a range
  node-semver reads as `*`, i.e. any future major, in a 0.x line where every
  minor is breaking by the project's own experience.

This command reports those shapes.  It FAILS on what the workspace rules already
require and merely WARNS about what they permit today (`^*` is currently the
mandated declaration for family packages, so flagging it as a violation would
contradict the rule — the warning exists because the audit showed what it
costs).

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

PLANA_REPO = "https://github.com/celestia-island/plana.git"

#: Repositories the freeze list allows to pin a `plana` revision.  Everyone else
#: tracks master so the family at least shares one moving target instead of
#: seven stationary ones.
REV_PIN_ALLOWED = frozenset({"scriptum", "aris"})

#: `(crate, canonical requirement, why)` for the Rust layers.  The canonical
#: value is the floor the family standardised on; a lower floor means a
#: different primitive, not an older build.
LAYERS: tuple[tuple[str, str, str], ...] = (
    ("kirino", "^0.7", "auth primitives: JWT/RBAC/sessions (Layer 0)"),
)

#: The npm layer.  `(package, floor, why)` — the floor is informational because
#: the declared range is allowed to be `^*` today.
NPM_LAYERS: tuple[tuple[str, tuple[int, int], str], ...] = (
    ("@celestia-island/hikari", (0, 55), "UI component library (Layer 2)"),
)

UNBOUNDED = {"", "*", "^*", "x", "latest", ">=0.0.0"}
SKIP_DIRS = {"node_modules", "target", "dist", ".git", ".generated", "vendor"}
_MANIFEST_GLOBS = ("Cargo.toml", "package.json")


@dataclass(frozen=True)
class Finding:
    level: str  # "violation" | "warning"
    subject: str
    message: str


def _floor(requirement: str) -> tuple[int, int] | None:
    """The lowest version a caret/tilde/range requirement admits, as (major, minor)."""
    text = requirement.strip().strip("'\"")
    if text in UNBOUNDED:
        return None
    match = re.match(r"^[\^~>=<\s]*(\d+)(?:\.(\d+))?", text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0))


def _manifests(root: Path, name: str) -> Iterable[Path]:
    for path in sorted(root.rglob(name)):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


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


def _cargo_deps(document: dict) -> Iterable[tuple[str, object]]:
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        table = document.get(section)
        if isinstance(table, dict):
            yield from table.items()
    workspace = document.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("dependencies"), dict):
        yield from workspace["dependencies"].items()


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
    for key, spec in _cargo_deps(document):
        name = _crate_name(key, spec)
        spec = spec if isinstance(spec, dict) else {"version": spec}
        subject = f"{name} @ {path.relative_to(root)}"
        if name not in ("plana",) and not name.startswith("plana_") and name not in {
            crate for crate, _canonical, _why in LAYERS
        }:
            continue
        if spec.get("workspace") is True:
            # The version lives in the workspace root, which this scan also reads;
            # checking the inheritance site would report the same floor twice.
            continue
        if "path" in spec:
            target = (path.parent / str(spec["path"])).resolve()
            if root in target.parents or target == root:
                continue  # inside this repository: not a cross-repo dependency
            findings.append(Finding(
                "violation", subject,
                f"local path dependency to {spec['path']!r} outside this repository; "
                "cross-repo Rust dependencies must be git references on master",
            ))
            continue
        if name == "plana" or name.startswith("plana_"):
            if "git" not in spec:
                findings.append(Finding(
                    "violation", subject,
                    "plana is consumed from crates.io; the family consumes it from "
                    f"{PLANA_REPO} (branch = \"master\")",
                ))
            elif "rev" in spec and repo not in REV_PIN_ALLOWED:
                findings.append(Finding(
                    "violation", subject,
                    f"pins plana to rev {spec['rev']!r}; only the frozen repositories "
                    f"({', '.join(sorted(REV_PIN_ALLOWED))}) may pin",
                ))
            elif not any(k in spec for k in ("rev", "tag", "branch")):
                findings.append(Finding(
                    "warning", subject, "git dependency with no branch/tag/rev",
                ))
        for crate, canonical, _why in LAYERS:
            if name != crate:
                continue
            if "git" in spec:
                findings.append(Finding(
                    "warning", subject,
                    f"tracks {spec.get('branch') or spec.get('rev') or 'a git source'}; "
                    f"crates.io {canonical} is the family floor",
                ))
                continue
            requirement = spec.get("version")
            if not isinstance(requirement, str):
                findings.append(Finding("violation", subject, "no version requirement"))
                continue
            floor = _floor(requirement)
            want = _floor(canonical)
            if floor is None or want is None:
                findings.append(Finding(
                    "violation", subject, f"unparsable requirement {requirement!r}",
                ))
            elif floor < want:
                findings.append(Finding(
                    "violation", subject,
                    f"declares {requirement} — floor {floor[0]}.{floor[1]} is below the "
                    f"family's {canonical}; 0.x carets do not overlap, so this is a "
                    f"different primitive, not an older build",
                ))


def _check_npm(path: Path, root: Path, findings: list[Finding]) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - unreadable manifest
        findings.append(Finding("warning", str(path.relative_to(root)), f"unreadable: {exc}"))
        return
    for section in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
        table = document.get(section)
        if not isinstance(table, dict):
            continue
        for package, floor, _why in NPM_LAYERS:
            requirement = table.get(package)
            if not isinstance(requirement, str):
                continue
            subject = f"{package} @ {path.relative_to(root)}"
            if requirement.strip() in UNBOUNDED:
                findings.append(Finding(
                    "warning", subject,
                    f"declares {requirement!r}, which node-semver reads as `*` — any "
                    "future major is admitted on a fresh resolve",
                ))
                continue
            parsed = _floor(requirement)
            if parsed is None:
                findings.append(Finding(
                    "warning", subject, f"unparsable requirement {requirement!r}",
                ))
            elif parsed < floor:
                findings.append(Finding(
                    "warning", subject,
                    f"declares {requirement} — below the family's {floor[0]}.{floor[1]} line",
                ))


def check(root: Path) -> list[Finding]:
    """Every family-consumption finding in ``root``, violations first."""
    findings: list[Finding] = []
    repo = _repo_name(root)
    for name in _MANIFEST_GLOBS:
        for path in _manifests(root, name):
            if name == "Cargo.toml":
                _check_cargo(path, root, findings, repo)
            else:
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
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable report",
    )
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures too",
    )
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
