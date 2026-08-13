#!/usr/bin/env python3
"""Verify version consistency across the cargo and npm tracks of a repository.

A celestia-island repo may carry **two independent version lines** (e.g. hikari:
Rust crates at ``0.3.19`` and the Vue/npm port at ``0.4.5``).  ``verify-versions``
gates against *drift* — a package in one track silently diverging from that
track's baseline — and warns when the changelog top entry lags behind both.

Rules
-----

1.  **cargo track** — if the repo root has a ``Cargo.toml`` with
    ``[workspace.package].version``, that value is the baseline.  Every crate
    manifest in the repo (workspace members *and* adjacent standalone crates
    such as ``packages/e2e``) is checked: ``version.workspace = true`` passes,
    a hardcoded ``version`` equal to the baseline passes, anything else is DRIFT
    (suggestion: switch to ``version.workspace = true``).  For a single-crate
    repo (no ``[workspace]``) the root ``[package].version`` is the baseline.
2.  **npm track** — collect every ``package.json`` (excluding ``node_modules/``,
    ``target/``, ``dist/``); if a root ``package.json`` declares ``version`` it
    is the baseline, otherwise the majority version among non-``private``
    (publishable) packages wins.  Any ``package.json`` whose ``version`` differs
    is DRIFT — ``private: true`` packages are included in the comparison (their
    versions must still agree).
3.  **CHANGELOG.md** — the topmost version heading (e.g. ``## [v0.3.14]``) must
    match at least one track's baseline, otherwise WARN (not a failure by
    default).  ``--strict`` escalates that warning to a failure.
4.  **Exemptions** — an optional ``.versions.toml`` overrides the baselines and
    skips whole packages::

        [track]
        cargo = "0.3.19"   # optional: override the detected cargo baseline
        npm = "0.4.5"      # optional: override the detected npm baseline

        [exempt.cargo]
        "packages/e2e" = "test-only crate"

        [exempt.npm]
        "packages/theme" = "vendored fork"

5.  **Output** — a human-readable drift table (package → actual → expected →
    suggestion); exit 1 when drift is present, exit 0 otherwise.  ``--json``
    emits the same report as machine-readable JSON.

Usage::

    celestia-devtools verify-versions [REPO] [--json] [--strict]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # Python 3.9/3.10
    try:
        import tomli as _toml
    except ImportError:  # pragma: no cover - no TOML parser available
        _toml = None

# Directories never walked: build artefacts / vendored trees whose manifests are
# not part of either version track.
_SKIP_DIRS = frozenset(
    {"node_modules", "target", "dist", ".git", "build", ".next", ".venv", "__pycache__"}
)

# A Cargo.toml living under an ``examples/`` directory is a demo/example crate,
# not a published member — its version is not gated (mirrors the common
# ``exclude = ["examples/*"]`` convention).
_EXAMPLE_SEGMENT = "examples"

# Topmost version heading in a Keep-a-Changelog style file.  ``## [v0.3.14]``,
# ``## [0.2.10] - date`` and ``## 1.2.3`` are recognised; ``[Unreleased]`` and
# prose headings ("Version lines", "Changelog") are skipped because they carry
# no version number.
_CHANGELOG_VERSION_RE = re.compile(
    r"^\s*#{1,6}\s+\[?([vV]?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)"
)


@dataclass
class Drift:
    """A single package whose version diverged from its track baseline."""

    track: str
    package: str
    actual: str
    expected: str
    suggestion: str


@dataclass
class VerifyResult:
    """Detection outcome for one repo (rendering is a separate concern)."""

    repo: str
    cargo_base: Optional[str] = None
    npm_base: Optional[str] = None
    drifts: List[Drift] = field(default_factory=list)
    changelog_version: Optional[str] = None
    changelog_warning: bool = False

    @property
    def has_drift(self) -> bool:
        return bool(self.drifts)

    def to_dict(self) -> dict:
        def _drift_dict(track: str) -> List[dict]:
            return [
                {
                    "package": d.package,
                    "actual": d.actual,
                    "expected": d.expected,
                    "suggestion": d.suggestion,
                }
                for d in self.drifts
                if d.track == track
            ]

        return {
            "repo": self.repo,
            "cargo": {"base": self.cargo_base, "drifts": _drift_dict("cargo")},
            "npm": {"base": self.npm_base, "drifts": _drift_dict("npm")},
            "changelog": {
                "version": self.changelog_version,
                "warning": self.changelog_warning,
            },
        }


# ── Manifest helpers ─────────────────────────────────────────────────────────


def _load_toml(path: Path) -> Optional[dict]:
    """Parse a TOML file, returning ``None`` on any read/parse failure."""
    if _toml is None:
        return None
    try:
        return _toml.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_json(path: Path) -> Optional[dict]:
    """Parse a JSON file, returning ``None`` on any read/parse failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _norm_version(value: object) -> str:
    """Normalise a version token for comparison (strip a leading ``v``)."""
    text = str(value).strip().lstrip("=").strip()
    if text[:1].lower() == "v" and text[1:2].isdigit():
        text = text[1:]
    return text.strip()


def _iter_manifests(repo: Path, filename: str):
    """Yield manifest paths under *repo*, pruning vendored/build directories."""
    for root, dirs, files in os.walk(repo, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if filename in files:
            yield Path(root) / filename


def _package_id(repo: Path, manifest: Path) -> str:
    """Relative directory of *manifest* from *repo* (posix, ``.`` for root)."""
    rel = manifest.parent.relative_to(repo)
    return rel.as_posix() if str(rel) != "." else "."


def _is_example_crate(repo: Path, manifest: Path) -> bool:
    return _EXAMPLE_SEGMENT in manifest.parent.relative_to(repo).parts


# ── Exemption config (.versions.toml) ────────────────────────────────────────


def load_versions_config(repo: Path) -> dict:
    """Load ``.versions.toml`` if present, else an empty config dict."""
    data = _load_toml(repo / ".versions.toml")
    if not data:
        return {}
    result: dict = {"exempt": {"cargo": set(), "npm": set()}}
    track = data.get("track")
    if isinstance(track, dict):
        overrides = {
            k: v for k, v in track.items() if isinstance(v, str)
        }
        if overrides:
            result["track"] = overrides
    exempt = data.get("exempt")
    if isinstance(exempt, dict):
        for key in ("cargo", "npm"):
            table = exempt.get(key)
            if isinstance(table, dict):
                result["exempt"][key] = set(_norm_path(k) for k in table)
    return result


def _norm_path(path: str) -> str:
    """Normalise a relative path for exemption matching (forward slashes)."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") or "."


# ── cargo track ──────────────────────────────────────────────────────────────


def detect_cargo_base(repo: Path, override: Optional[str]) -> Optional[str]:
    if override is not None:
        return _norm_version(override)
    root = _load_toml(repo / "Cargo.toml")
    if not root:
        return None
    workspace = root.get("workspace")
    if isinstance(workspace, dict):
        wp = workspace.get("package")
        if isinstance(wp, dict) and isinstance(wp.get("version"), str):
            return _norm_version(wp["version"])
        return None
    pkg = root.get("package")
    if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
        return _norm_version(pkg["version"])
    return None


def collect_cargo_drifts(
    repo: Path, base: Optional[str], exempt: set[str]
) -> List[Drift]:
    if base is None:
        return []
    drifts: List[Drift] = []
    root_manifest = repo / "Cargo.toml"
    for manifest in _iter_manifests(repo, "Cargo.toml"):
        if manifest == root_manifest:
            continue  # the workspace / root manifest defines the baseline
        if _is_example_crate(repo, manifest):
            continue
        if _package_id(repo, manifest) in exempt:
            continue
        data = _load_toml(manifest)
        if not data:
            continue
        pkg = data.get("package")
        if not isinstance(pkg, dict):
            continue
        version = pkg.get("version")
        if isinstance(version, dict) and version.get("workspace") is True:
            continue  # version.workspace = true — inherits the baseline
        if isinstance(version, str):
            actual = _norm_version(version)
            if actual != base:
                drifts.append(
                    Drift(
                        track="cargo",
                        package=_package_id(repo, manifest),
                        actual=actual,
                        expected=base,
                        suggestion="set version.workspace = true",
                    )
                )
    return drifts


# ── npm track ────────────────────────────────────────────────────────────────


def detect_npm_base(repo: Path, override: Optional[str]) -> Optional[str]:
    if override is not None:
        return _norm_version(override)
    root_pkg = _load_json(repo / "package.json")
    if root_pkg is not None and isinstance(root_pkg.get("version"), str):
        return _norm_version(root_pkg["version"])
    # No (versioned) root package: majority version among publishable packages.
    publishable: List[str] = []
    for manifest in _iter_manifests(repo, "package.json"):
        pkg = _load_json(manifest)
        if not pkg or not isinstance(pkg.get("version"), str):
            continue
        if pkg.get("private") is not True:
            publishable.append(_norm_version(pkg["version"]))
    if publishable:
        return Counter(publishable).most_common(1)[0][0]
    return None


def collect_npm_drifts(
    repo: Path, base: Optional[str], exempt: set[str]
) -> List[Drift]:
    if base is None:
        return []
    drifts: List[Drift] = []
    for manifest in _iter_manifests(repo, "package.json"):
        if _package_id(repo, manifest) in exempt:
            continue
        pkg = _load_json(manifest)
        if not pkg or not isinstance(pkg.get("version"), str):
            continue
        actual = _norm_version(pkg["version"])
        if actual != base:
            drifts.append(
                Drift(
                    track="npm",
                    package=_package_id(repo, manifest),
                    actual=actual,
                    expected=base,
                    suggestion=f"set version to {base}",
                )
            )
    return drifts


# ── CHANGELOG track ──────────────────────────────────────────────────────────


def detect_changelog_version(repo: Path) -> Optional[str]:
    """Return the topmost version heading from ``CHANGELOG.md`` (normalised)."""
    changelog = repo / "CHANGELOG.md"
    try:
        lines = changelog.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = _CHANGELOG_VERSION_RE.match(line)
        if match:
            return _norm_version(match.group(1))
    return None


# ── Orchestration ────────────────────────────────────────────────────────────


def verify(repo: Path) -> VerifyResult:
    """Run the whole detection and return a :class:`VerifyResult`."""
    repo = repo.resolve()
    config = load_versions_config(repo)
    track = config.get("track", {})
    exempt = config.get("exempt", {})

    cargo_base = detect_cargo_base(repo, track.get("cargo"))
    npm_base = detect_npm_base(repo, track.get("npm"))

    drifts: List[Drift] = []
    drifts.extend(collect_cargo_drifts(repo, cargo_base, exempt.get("cargo", set())))
    drifts.extend(collect_npm_drifts(repo, npm_base, exempt.get("npm", set())))
    drifts.sort(key=lambda d: (d.track, d.package))

    changelog_version = detect_changelog_version(repo)
    changelog_warning = False
    if changelog_version is not None:
        baselines = {b for b in (cargo_base, npm_base) if b is not None}
        if baselines and changelog_version not in baselines:
            changelog_warning = True

    return VerifyResult(
        repo=str(repo),
        cargo_base=cargo_base,
        npm_base=npm_base,
        drifts=drifts,
        changelog_version=changelog_version,
        changelog_warning=changelog_warning,
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def _render_text(result: VerifyResult) -> str:
    lines: List[str] = []
    if result.drifts:
        lines.append("Version drift detected:")
        lines.append("")
        header = ("TRACK", "PACKAGE", "ACTUAL", "EXPECTED", "SUGGESTION")
        rows = [
            (d.track, d.package, d.actual, d.expected, d.suggestion)
            for d in result.drifts
        ]
        widths = [
            max(len(header[i]), *(len(r[i]) for r in rows))
            for i in range(len(header))
        ]
        fmt = "  " + "  ".join(
            f"{{{i}:<{widths[i]}}}" for i in range(len(header))
        )
        lines.append(fmt.format(*header))
        for row in rows:
            lines.append(fmt.format(*row))
    else:
        summary = []
        if result.cargo_base is not None:
            summary.append(f"cargo={result.cargo_base}")
        if result.npm_base is not None:
            summary.append(f"npm={result.npm_base}")
        detail = f" ({', '.join(summary)})" if summary else ""
        lines.append(f"No version drift detected{detail}.")

    if result.changelog_warning:
        tracks = []
        if result.cargo_base is not None:
            tracks.append(f"cargo ({result.cargo_base})")
        if result.npm_base is not None:
            tracks.append(f"npm ({result.npm_base})")
        lines.append(
            f"WARN: CHANGELOG.md top version {result.changelog_version} "
            f"matches neither {' nor '.join(tracks)}."
        )
    return "\n".join(lines)


def _render_json(result: VerifyResult, ok: bool) -> str:
    payload = result.to_dict()
    payload["ok"] = ok
    return json.dumps(payload, indent=2, sort_keys=True)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools verify-versions",
        description="Verify version consistency across cargo/npm tracks.",
    )
    parser.add_argument(
        "repo", nargs="?", default=".",
        help="repository root to verify (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit a machine-readable JSON report instead of the drift table",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="escalate a CHANGELOG version mismatch from a warning to a failure",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2

    result = verify(repo)
    failed = result.has_drift or (args.strict and result.changelog_warning)

    if args.json:
        print(_render_json(result, ok=not failed))
    else:
        print(_render_text(result))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
