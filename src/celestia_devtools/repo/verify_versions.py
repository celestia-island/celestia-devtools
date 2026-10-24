#!/usr/bin/env python3
"""Verify version consistency across the cargo, npm and python tracks of a repository.

A celestia-island repo may carry **independent version lines** (e.g. hikari:
Rust crates at ``0.3.19`` and the Vue/npm port at ``0.4.5``; Python distributions
carry their own ``pyproject.toml`` line).  ``verify-versions`` gates against
*drift* — a package in one track silently diverging from that track's baseline.

(2026-09-26) The former CHANGELOG.md rule is gone: the org bans maintained
changelogs outright (squashed PRs are the changelog), so consulting one both
endorsed a banned file and could never fire on a compliant repository. A stale
``CHANGELOG.md`` is now simply ignored.

Rules
-----

1.  **cargo track** — if the repo root has a ``Cargo.toml`` with
    ``[workspace.package].version``, that value is the baseline; otherwise the
    root ``[package].version`` is the baseline (this fallback also applies
    when a ``[workspace]`` table exists without ``[workspace.package]`` —
    e.g. a vestigial ``[workspace.lints]`` on a single-crate repo, or a
    root-crate + members layout whose members track the root line).  Every crate
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
3.  **python track (2026-10-25, structural audit SR-21)** — Python
    distributions had no gate at all: ``verify-versions`` reported ``ok: true``
    with ``cargo_base=null`` / ``npm_base=null`` for the whole Python half of
    the fleet (``celestia-plugin``, ``electrode_recon``, ``celestia-integration``,
    ``official-plugins``), so a tag/lock/manifest drift could never fail.  The
    root ``pyproject.toml`` ``[project].version`` (or ``[tool.poetry].version``)
    is the baseline; every versioned ``pyproject.toml`` that is *distributable*
    is compared against it.  Helper projects under ``tests/`` (or ``test/``),
    ``examples/``, ``scripts/``, ``tools/``, ``fixtures/``, ``benchmarks/`` and
    ``vendor/`` are not distributions and are skipped (a test harness pinned to a
    release line is not drift); a distributable manifest that *declares* a
    version which is empty or not a string is an error, not a skip.  A root ``pyproject.toml`` that declares ``dynamic =
    ["version"]`` has no static identity to compare: the track is absent, not
    an error — pin ``[track] python`` to gate it explicitly.
4.  **Exemptions** — an optional ``.versions.toml`` overrides the baselines and
    skips whole packages::

        [track]
        cargo = "0.3.19"   # optional: override the detected cargo baseline
        npm = "0.4.5"      # optional: override the detected npm baseline
        python = "0.2.0"   # optional: override the detected python baseline

        [exempt.cargo]
        "packages/e2e" = "test-only crate"

        [exempt.npm]
        "packages/theme" = "vendored fork"

        [exempt.python]
        "packages/vendor/harness" = "not a distribution"

    Every exemption key is a path relative to the repository root, and since
    2026-10-25 it may be a shell glob (``fnmatch`` semantics; ``*`` crosses
    path separators): ``packages/vendor/*`` exempts a vendored tree with one
    line instead of one entry per manifest.  This is what lets the shared
    vendored-upstream rule (``patches/boa_*``, ``packages/vendor/``,
    test-fixture crates) be expressed without 65 hand-written keys.

5.  **Fail-closed baselines (2026-09-27, structural scan S1-1)** — a track
    that *exists* (any gated ``Cargo.toml`` declaring ``[package].version`` or
    ``[workspace.package].version``, any ``package.json`` declaring a
    ``version``, or a root ``pyproject.toml`` declaring a static version)
    must resolve to a baseline.  An unresolvable baseline is a
    hard error, not a silent pass: a repo whose gate cannot decide must not
    get a green gate.  Remediation: add ``[workspace.package].version`` +
    ``version.workspace = true``, or pin ``[track]`` in ``.versions.toml``.
6.  **Output** — a human-readable drift table (package → actual → expected →
    suggestion) plus baseline errors; exit 1 when drift is present **or** a
    baseline cannot be resolved, exit 0 otherwise.  ``--json`` emits the same
    report as machine-readable JSON (one block per track: ``cargo``, ``npm``,
    ``python``).

Usage::

    celestia-devtools verify-versions [REPO] [--json]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
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

# Directories never walked: build artefacts, virtualenvs and vendored trees whose
# manifests are not part of any version track.
_SKIP_DIRS = frozenset(
    {
        "node_modules", "target", "dist", ".git", "build", ".next", ".venv",
        "venv", ".tox", ".nox", "site-packages", "__pycache__",
    }
)

# A Cargo.toml living under an ``examples/`` directory is a demo/example crate,
# not a published member — its version is not gated (mirrors the common
# ``exclude = ["examples/*"]`` convention).
_EXAMPLE_SEGMENT = "examples"

#: Directories whose ``pyproject.toml`` is a helper project, not a
#: distribution: test harnesses, example apps, tooling shims and vendored
#: trees.  Their versions intentionally follow whatever they import and must
#: not be dragged into the package's release line (SR-21).
_PY_HELPER_SEGMENTS = frozenset(
    {"tests", "test", "examples", "scripts", "tools", "fixtures", "benchmarks", "vendor"}
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
    python_base: Optional[str] = None
    drifts: List[Drift] = field(default_factory=list)
    # Non-drift failures: a present track whose baseline could not be resolved.
    # These fail the gate exactly like drifts do (fail-closed, S1-1).
    errors: List[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.drifts)

    @property
    def has_error(self) -> bool:
        return bool(self.errors)

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
            "python": {"base": self.python_base, "drifts": _drift_dict("python")},
            "errors": list(self.errors),
        }


# ── Manifest helpers ─────────────────────────────────────────────────────────


def _load_toml(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Parse a TOML file.

    Returns ``(data, None)`` on success, ``(None, None)`` when the file is
    absent (absence is a structural answer the callers interpret), and
    ``(None, reason)`` when a PRESENT file cannot be parsed. The split
    matters: the old shape swallowed corruption into the same ``None`` as
    absence, so an unparseable manifest was silently skipped — a repo whose
    Cargo.toml carried duplicate keys (invalid TOML) sailed through the
    gate green (2026-10-03 master incident: two ``version =`` lines in
    [workspace.package] survived a squash merge).
    """
    if not path.is_file():
        return None, None
    if _toml is None:
        return None, "no TOML parser available (python < 3.11 without tomli)"
    try:
        return _toml.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:  # tomllib raises TOMLDecodeError on dup keys
        return None, f"{type(exc).__name__}: {exc}"


class _JsonDuplicateKey(ValueError):
    """Raised by the object_pairs hook — json.loads would silently
    last-win a duplicate key, hiding exactly the corruption this gate
    exists to catch."""


def _no_duplicate_keys(pairs):
    seen: set = set()
    for key, _ in pairs:
        if key in seen:
            raise _JsonDuplicateKey(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _load_json(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Parse a JSON object file; see :func:`_load_toml` for the contract
    (absent → ``(None, None)``; corrupt → ``(None, reason)``, with
    duplicate keys detected explicitly)."""
    if not path.is_file():
        return None, None
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except _JsonDuplicateKey as exc:
        return None, f"invalid JSON: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "invalid JSON: top-level value is not an object"
    return data, None


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


# ── Track presence (fail-closed baselines, S1-1) ─────────────────────────────


def cargo_track_present(repo: Path, errors: Optional[List[str]] = None) -> bool:
    """True when any gated Cargo.toml in the repo declares a version at all.

    Presence is what turns "no baseline" from an acceptable "track absent"
    into a gate failure: versioned manifests exist, so a baseline must exist
    too.  Example crates are excluded exactly as in drift collection.

    A CORRUPT manifest counts as present (it may well be versioned — we
    cannot know) and is recorded into ``errors``: without this, a repo whose
    only versioned manifest is corrupt would resolve no baseline, skip the
    whole track, and sail green with zero diagnostics (R1 F2, 2026-10-03).
    """
    for manifest in _iter_manifests(repo, "Cargo.toml"):
        if _is_example_crate(repo, manifest):
            continue
        data, reason = _load_toml(manifest)
        if reason and errors is not None:
            errors.append(
                f"{_norm_path(str(manifest.relative_to(repo)))} unparseable, its version unknown: {reason}"
            )
        if not data:
            continue
        pkg = data.get("package")
        if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
            return True
        workspace = data.get("workspace")
        if isinstance(workspace, dict):
            wp = workspace.get("package")
            if isinstance(wp, dict) and isinstance(wp.get("version"), str):
                return True
    return False


def npm_track_present(repo: Path) -> bool:
    """True when a *publishable* package.json declares a version.

    `private: true` manifests are excluded (2026-10-24): a repository whose only
    package.json files are private webui shells has no npm identity to keep in
    line, and demanding a baseline for one turned five compliant repositories
    into immediate reds when they adopted this gate.  A private root package
    must likewise not define the baseline — `@celestia/tairitsu` is a private
    0.1.1 shell while the two packages it wraps publish on 0.3 / 0.5.
    """
    for manifest in _iter_manifests(repo, "package.json"):
        pkg, _reason = _load_json(manifest)
        if not isinstance(pkg, dict) or not isinstance(pkg.get("version"), str):
            continue
        if pkg.get("private") is True:
            continue
        return True
    return False


# ── Exemption config (.versions.toml) ────────────────────────────────────────


def load_versions_config(repo: Path, errors: Optional[List[str]] = None) -> dict:
    """Load ``.versions.toml`` if present, else an empty config dict.

    A PRESENT but unparseable config is recorded into ``errors`` when a
    sink is given: a corrupt file silently disabling exemptions/overrides
    is a gate malfunction, not a no-config repo."""
    data, reason = _load_toml(repo / ".versions.toml")
    if reason and errors is not None:
        errors.append(
            f".versions.toml unparseable, exemptions/overrides ignored: {reason}"
        )
    if not data:
        return {}
    result: dict = {"exempt": {"cargo": set(), "npm": set(), "python": set()}}
    track = data.get("track")
    if isinstance(track, dict):
        overrides = {
            k: v for k, v in track.items() if isinstance(v, str)
        }
        if overrides:
            result["track"] = overrides
    exempt = data.get("exempt")
    if isinstance(exempt, dict):
        for key in ("cargo", "npm", "python"):
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


def _is_exempt(package_id: str, patterns: set) -> bool:
    """True when *package_id* matches any exemption key.

    Keys are repository-relative paths.  A key without wildcards matches its own
    directory (the historical behaviour); a key with ``*`` is a shell glob where
    ``*`` also crosses ``/``, so ``packages/vendor/*`` exempts the whole vendored
    tree and ``patches/boa_*`` covers ``patches/boa_engine`` / ``_gc`` / ``_parser``
    (2026-10-25 structural audit SR-22 — the shared vendored-upstream rule must be
    expressible without one key per manifest).
    """
    return any(fnmatch.fnmatchcase(package_id, pattern) for pattern in patterns)


# ── cargo track ──────────────────────────────────────────────────────────────


def detect_cargo_base(
    repo: Path, override: Optional[str], errors: Optional[List[str]] = None
) -> Optional[str]:
    if override is not None:
        return _norm_version(override)
    root, reason = _load_toml(repo / "Cargo.toml")
    if reason and errors is not None:
        # The root manifest IS the baseline source; a corrupt one must not
        # read as "no cargo track" (which would skip the whole track
        # silently) — the 2026-10-03 master incident shape.
        errors.append(f"Cargo.toml unparseable, cargo track skipped: {reason}")
    if not root:
        return None
    workspace = root.get("workspace")
    if isinstance(workspace, dict):
        wp = workspace.get("package")
        if isinstance(wp, dict) and isinstance(wp.get("version"), str):
            return _norm_version(wp["version"])
        # A [workspace] table without [workspace.package].version may be a
        # real multi-crate workspace (baseline must come from an override,
        # see below) — but it may also be a single-crate repo carrying a
        # vestigial [workspace.lints] table.  In both cases the root
        # [package].version, when present, is a meaningful baseline: for the
        # vestigial case it is the only crate; for the mixed root-crate +
        # members case it is the flagship line members are expected to track
        # (or be exempted from).  Falling through here instead of returning
        # None keeps real coverage on shapes that previously skipped the
        # whole track silently (2026-09-26 structural scan S1-1).
        pkg = root.get("package")
        if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
            return _norm_version(pkg["version"])
        return None
    pkg = root.get("package")
    if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
        return _norm_version(pkg["version"])
    return None


def collect_cargo_drifts(
    repo: Path,
    base: Optional[str],
    exempt: set[str],
    errors: Optional[List[str]] = None,
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
        if _is_exempt(_package_id(repo, manifest), exempt):
            continue
        data, reason = _load_toml(manifest)
        if reason and errors is not None:
            errors.append(
                f"{_norm_path(str(manifest.relative_to(repo)))} unparseable, its version unchecked: {reason}"
            )
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


def detect_npm_base(
    repo: Path, override: Optional[str], errors: Optional[List[str]] = None
) -> Optional[str]:
    if override is not None:
        return _norm_version(override)
    root_pkg, reason = _load_json(repo / "package.json")
    if reason and errors is not None:
        errors.append(
            f"package.json unparseable, npm baseline fell back to majority vote: {reason}"
        )
    if (
        root_pkg is not None
        and isinstance(root_pkg.get("version"), str)
        and root_pkg.get("private") is not True
    ):
        return _norm_version(root_pkg["version"])
    # No (versioned) root package: majority version among publishable packages.
    publishable: List[str] = []
    for manifest in _iter_manifests(repo, "package.json"):
        pkg, reason = _load_json(manifest)
        if reason and errors is not None:
            errors.append(
                f"{_norm_path(str(manifest.relative_to(repo)))} unparseable, its version unchecked: {reason}"
            )
        if not pkg or not isinstance(pkg.get("version"), str):
            continue
        if pkg.get("private") is not True:
            publishable.append(_norm_version(pkg["version"]))
    if publishable:
        return Counter(publishable).most_common(1)[0][0]
    return None


def npm_baseline_source(repo: Path, override: Optional[str]) -> str:
    """Which declaration the npm baseline came from.

    ``"override"`` (a ``[track] npm`` pin), ``"root"`` (a public root
    package.json version) or ``"majority"`` (the majority version among
    publishable packages).  Only the last one implies "compare publishable
    packages only": both other sources name one identity on purpose, so every
    manifest is judged against it.
    """
    if override is not None:
        return "override"
    root_pkg, _reason = _load_json(repo / "package.json")
    if (
        isinstance(root_pkg, dict)
        and isinstance(root_pkg.get("version"), str)
        and root_pkg.get("private") is not True
    ):
        return "root"
    return "majority"


def collect_npm_drifts(
    repo: Path,
    base: Optional[str],
    exempt: set[str],
    errors: Optional[List[str]] = None,
    publishable_only: bool = False,
) -> List[Drift]:
    if base is None:
        return []
    drifts: List[Drift] = []
    for manifest in _iter_manifests(repo, "package.json"):
        if _is_exempt(_package_id(repo, manifest), exempt):
            continue
        pkg, reason = _load_json(manifest)
        if reason and errors is not None:
            errors.append(
                f"{_norm_path(str(manifest.relative_to(repo)))} unparseable, its version unchecked: {reason}"
            )
        if not pkg or not isinstance(pkg.get("version"), str):
            continue
        if publishable_only and pkg.get("private") is True:
            # The baseline was inferred from publishable packages, so a private
            # shell (a webui bundle, a Tauri dev package) is not drifting from a
            # line it never belonged to.  An explicit `[track] npm` pin still
            # compares everything — that is the escape hatch for repositories
            # that do want their private packages in line.
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


# ── python track ─────────────────────────────────────────────────────────────


def _is_python_helper(repo: Path, manifest: Path) -> bool:
    """True for a pyproject that is a helper project, not a distribution."""
    parts = manifest.parent.relative_to(repo).parts
    return any(part in _PY_HELPER_SEGMENTS for part in parts)


def _python_version_field(data: dict) -> tuple[Optional[str], object]:
    """Return ``(source, raw_version)`` for a pyproject-shaped table.

    ``source`` is ``"project"`` (PEP 621), ``"poetry"`` or ``"dynamic"`` when
    ``[project].dynamic`` names ``version`` (the identity is only known at build
    time, so there is nothing to compare).  ``(None, None)`` means the table
    declares no version at all.  The raw value is returned untyped so a
    malformed declaration (``version = 1``, ``version = ""``) still counts as
    present and trips the fail-closed baseline check instead of being silently
    skipped.
    """
    project = data.get("project")
    if isinstance(project, dict):
        dynamic = project.get("dynamic")
        if isinstance(dynamic, list) and "version" in [str(v) for v in dynamic]:
            return "dynamic", None
        if "version" in project:
            return "project", project["version"]
    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict) and "version" in poetry:
            return "poetry", poetry["version"]
    return None, None


def _python_declared_version(data: dict) -> Optional[str]:
    """Static, non-empty string version from ``[project]`` or ``[tool.poetry]``."""
    _source, raw = _python_version_field(data)
    if not isinstance(raw, str):
        return None
    normalized = _norm_version(raw)
    return normalized or None


def detect_python_base(
    repo: Path, override: Optional[str], errors: Optional[List[str]] = None
) -> Optional[str]:
    if override is not None:
        return _norm_version(override)
    root, reason = _load_toml(repo / "pyproject.toml")
    if reason and errors is not None:
        errors.append(f"pyproject.toml unparseable, python baseline unknown: {reason}")
    if not root:
        return None
    return _python_declared_version(root)


def python_track_present(repo: Path) -> bool:
    """True when the root pyproject declares a static version.

    Helper-only trees (``tests/python``) and dynamic-version projects do not
    create an identity the repository has to keep in line; an explicit
    ``[track] python`` pin does (see :func:`verify`).
    """
    root = repo / "pyproject.toml"
    if not root.is_file():
        return False
    data, _reason = _load_toml(root)
    if not isinstance(data, dict):
        # A corrupt root may well carry a version: count it as present so the
        # gate fails closed.  detect_python_base/_python_root_drifts are the
        # only places that report the parse failure (no duplicate errors).
        return True
    source, _raw = _python_version_field(data)
    return source is not None and source != "dynamic"


def collect_python_drifts(
    repo: Path,
    base: Optional[str],
    exempt: set,
    errors: Optional[List[str]] = None,
) -> List[Drift]:
    if base is None:
        return []
    drifts: List[Drift] = []
    root_manifest = repo / "pyproject.toml"
    for manifest in _iter_manifests(repo, "pyproject.toml"):
        if manifest == root_manifest:
            continue  # the root defines the baseline
        if _is_python_helper(repo, manifest):
            continue
        if _is_exempt(_package_id(repo, manifest), exempt):
            continue
        data, reason = _load_toml(manifest)
        if reason and errors is not None:
            errors.append(
                f"{_norm_path(str(manifest.relative_to(repo)))} unparseable, its version unchecked: {reason}"
            )
        if not isinstance(data, dict):
            continue
        source, raw = _python_version_field(data)
        actual = _python_declared_version(data)
        if source not in (None, "dynamic") and actual is None:
            if errors is not None:
                errors.append(
                    f"{_norm_path(str(manifest.relative_to(repo)))} declares an "
                    f"unusable version {raw!r}; expected a non-empty string"
                )
            continue
        if actual is not None and actual != base:
            drifts.append(
                Drift(
                    track="python",
                    package=_package_id(repo, manifest),
                    actual=actual,
                    expected=base,
                    suggestion=f"set version to {base}",
                )
            )
    return drifts


def _python_root_drifts(
    repo: Path, base: str, errors: Optional[List[str]] = None
) -> List[Drift]:
    """Compare the root pyproject against a ``[track] python`` pin.

    The parse failure is reported here because :func:`detect_python_base`
    returns the override before ever loading the root — the pinned-repo
    variant of the 2026-10-03 master incident (see :func:`_cargo_root_drifts`).
    """
    root, reason = _load_toml(repo / "pyproject.toml")
    if reason:
        if errors is not None:
            errors.append(
                f"pyproject.toml unparseable, root version unchecked: {reason}"
            )
        return []
    if not isinstance(root, dict):
        return []
    source, raw = _python_version_field(root)
    declared = _python_declared_version(root)
    if source not in (None, "dynamic") and declared is None:
        if errors is not None:
            errors.append(
                f"pyproject.toml declares an unusable version {raw!r}; expected a "
                "non-empty string"
            )
        return []
    if declared is None or declared == base:
        return []
    return [
        Drift(
            track="python",
            package=".",
            actual=declared,
            expected=base,
            suggestion=(
                "root pyproject.toml version diverged from the [track] python pin — "
                "bump the pin in .versions.toml in the same release commit"
            ),
        )
    ]


# ── Orchestration ────────────────────────────────────────────────────────────


def _cargo_root_drifts(
    repo: Path, base: str, errors: Optional[List[str]] = None
) -> List[Drift]:
    """Compare the root manifest's own declared version against a pinned base.

    Only meaningful when the baseline came from a ``[track]`` override: with
    inference (or fallback) the root *is* the baseline and would trivially
    agree with itself.
    """
    root, reason = _load_toml(repo / "Cargo.toml")
    if reason:
        # Record it HERE: under a [track] pin detect_cargo_base returns the
        # override before ever loading the root, so nobody else diagnoses a
        # corrupt root in that shape (R1 F1, 2026-10-03 — the pinned-repo
        # variant of the master incident shipped green).
        if errors is not None:
            errors.append(
                f"Cargo.toml unparseable, root version unchecked: {reason}"
            )
        return []
    if not root:
        return []
    declared: Optional[str] = None
    pkg = root.get("package")
    if isinstance(pkg, dict) and isinstance(pkg.get("version"), str):
        declared = _norm_version(pkg["version"])
    else:
        workspace = root.get("workspace")
        if isinstance(workspace, dict):
            wp = workspace.get("package")
            if isinstance(wp, dict) and isinstance(wp.get("version"), str):
                declared = _norm_version(wp["version"])
    if declared is None or declared == base:
        return []
    return [
        Drift(
            track="cargo",
            package=".",
            actual=declared,
            expected=base,
            suggestion=(
                "root manifest version diverged from the [track] cargo pin — "
                "bump the pin in .versions.toml in the same release commit"
            ),
        )
    ]


def verify(repo: Path) -> VerifyResult:
    """Run the whole detection and return a :class:`VerifyResult`."""
    repo = repo.resolve()
    # The parse-error sink comes FIRST so every detection stage can append
    # corruption findings into the same fail-closed list as the baseline
    # errors below (an unparseable manifest used to be silently skipped —
    # the 2026-10-03 master incident shipped duplicate version keys green).
    errors: List[str] = []
    config = load_versions_config(repo, errors)
    track = config.get("track", {})
    exempt = config.get("exempt", {})

    cargo_override = track.get("cargo")
    cargo_base = detect_cargo_base(repo, cargo_override, errors)
    npm_override = track.get("npm")
    npm_base = detect_npm_base(repo, npm_override, errors)
    python_override = track.get("python")
    python_base = detect_python_base(repo, python_override, errors)
    if cargo_track_present(repo, errors) and cargo_base is None:
        errors.append(
            "cargo track is present (versioned Cargo.toml manifests exist) but "
            "no baseline could be resolved: the root manifest declares neither "
            "[workspace.package].version nor [package].version, and "
            ".versions.toml pins no [track] cargo. Pin the baseline (add "
            "[workspace.package].version + version.workspace = true, or set "
            "[track] cargo in .versions.toml) — an unresolvable baseline must "
            "fail the gate, not pass it."
        )
    if npm_track_present(repo) and npm_base is None:
        errors.append(
            "npm track is present (a publishable package.json declares a "
            "version) but no baseline could be resolved: no public root "
            "package.json version and no majority among publishable packages. "
            "Pin [track] npm in .versions.toml."
        )
    if (python_track_present(repo) or python_override is not None) and python_base is None:
        if not any("pyproject.toml" in err for err in errors):
            errors.append(
                "python track is present (the root pyproject.toml declares a "
                "version, or [track] python is pinned) but no baseline could be "
                "resolved. Fix the declaration, or pin [track] python in "
                ".versions.toml."
            )

    drifts: List[Drift] = []
    drifts.extend(
        collect_cargo_drifts(repo, cargo_base, exempt.get("cargo", set()), errors)
    )
    # F9 (R2, 2026-09-27): collect_cargo_drifts skips the root manifest as the
    # "baseline definer" — but when the baseline actually comes from a
    # [track] pin, that skip lets a root-only bump escape all comparison
    # (the kirino S3 shape: root at 0.7.6, members and pin at 0.7.5, gate
    # green). The npm track compares the root package.json already; compare
    # the cargo root's own declared version against the pin for symmetry.
    if cargo_override is not None and cargo_base is not None:
        drifts.extend(_cargo_root_drifts(repo, cargo_base, errors))
    drifts.extend(
        collect_npm_drifts(
            repo,
            npm_base,
            exempt.get("npm", set()),
            errors,
            publishable_only=npm_baseline_source(repo, npm_override) == "majority",
        )
    )
    drifts.extend(
        collect_python_drifts(repo, python_base, exempt.get("python", set()), errors)
    )
    if python_override is not None and python_base is not None:
        drifts.extend(_python_root_drifts(repo, python_base, errors))
    drifts.sort(key=lambda d: (d.track, d.package))

    return VerifyResult(
        repo=str(repo),
        cargo_base=cargo_base,
        npm_base=npm_base,
        python_base=python_base,
        drifts=drifts,
        errors=errors,
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def _render_text(result: VerifyResult) -> str:
    lines: List[str] = []
    if result.errors:
        lines.append("Version baseline errors (gate fails closed):")
        lines.append("")
        for err in result.errors:
            lines.append(f"  error: {err}")
        lines.append("")
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
    elif not result.errors:
        summary = []
        if result.cargo_base is not None:
            summary.append(f"cargo={result.cargo_base}")
        if result.npm_base is not None:
            summary.append(f"npm={result.npm_base}")
        if result.python_base is not None:
            summary.append(f"python={result.python_base}")
        detail = f" ({', '.join(summary)})" if summary else ""
        lines.append(f"No version drift detected{detail}.")

    return "\n".join(lines)


def _render_json(result: VerifyResult, ok: bool) -> str:
    payload = result.to_dict()
    payload["ok"] = ok
    return json.dumps(payload, indent=2, sort_keys=True)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools verify-versions",
        description="Verify version consistency across cargo/npm/python tracks.",
    )
    parser.add_argument(
        "repo", nargs="?", default=".",
        help="repository root to verify (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit a machine-readable JSON report instead of the drift table",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2

    result = verify(repo)
    failed = result.has_drift or result.has_error

    if args.json:
        print(_render_json(result, ok=not failed))
    else:
        print(_render_text(result))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
