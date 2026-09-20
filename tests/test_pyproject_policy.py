"""Dependency-specifier policy gate for pyproject.toml (floor-only versions).

House rule (AGENTS §3.3.3 adapted to the Python ecosystem): dependency
declarations use a lower bound only — `>=N`. Upper bounds, exact pins,
compatible-release (`~=`), wildcards and caret ranges are forbidden here;
narrowing is allowed only with an inline justification comment and a removal
condition, which this gate makes impossible to sneak in silently.

Environment markers (e.g. `python_version < '3.11'`) are legitimate and stay
allowed. Per the zero-hit rule, the checker is first proven against known
good and bad samples before it is trusted on the real file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib  # Python >= 3.11 (our floor)
except ImportError:  # pragma: no cover
    tomllib = None

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# `name >= 1.2` — everything else in the spec slot is a violation.
_SPEC_OK = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*>=\s*[0-9A-Za-z.]+\s*$")
_NAME_ONLY = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*$")


def spec_violations(requirement: str) -> list:
    """Return a list of problems for one requirement string (empty = clean)."""
    problems = []
    spec, _, marker = requirement.partition(";")
    if _SPEC_OK.match(spec):
        return problems
    if _NAME_ONLY.match(spec) and marker.strip():
        # bare name with an environment marker (e.g. an extra-conditional) —
        # no version constraint to police.
        return problems
    problems.append(
        "spec {!r} must be `<name>>=<version>` (floor-only); markers after `;` are fine".format(
            requirement.strip()))
    return problems


class TestCheckerHasTeeth:
    """Self-proof: the checker accepts known-good and rejects known-bad."""

    @pytest.mark.parametrize("good", [
        "PyYAML>=6",
        "tomli-w>=1",
        "questionary>=2",
        "pytest>=7",
        "ruff>=0.4",
        "tomli>=2; python_version < '3.11'",   # marker allowed
        "some-extra; sys_platform == 'win32'",  # bare name + marker
    ])
    def test_accepts_floor_only(self, good):
        assert spec_violations(good) == []

    @pytest.mark.parametrize("bad", [
        "rich==15.0.0",            # exact pin
        "rich>=13,<16",            # upper bound
        "rich~=13.0",              # compatible release
        "rich==15.*",              # wildcard
        "rich^13",                 # caret (npm-ism)
        "rich >13",                # strict greater (excludes the floor itself)
        "rich!=15.0.0",            # exclusion
    ])
    def test_rejects_non_floor(self, bad):
        assert spec_violations(bad), "checker must reject " + bad


class TestPolicyOnRealFile:
    def test_all_dependency_declarations_floor_only(self):
        if tomllib is None:  # pragma: no cover
            pytest.skip("tomllib unavailable below 3.11")
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        project = data["project"]
        specs = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            specs.extend(extra)
        assert specs, "extraction self-check: found dependency specs to police"
        violations = [(s, v) for s in specs for v in spec_violations(s)]
        assert violations == []

    def test_requires_python_floor(self):
        if tomllib is None:  # pragma: no cover
            pytest.skip("tomllib unavailable below 3.11")
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        assert data["project"]["requires-python"] == ">=3.11"

    def test_ruff_target_matches_floor(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        assert 'target-version = "py311"' in text
        assert 'target-version = "py39"' not in text
