"""Every decision gate must be imported by a test.

*Why this exists:* `separator_lint` shipped, ran on every repository through
`just lint-separators`, and had **no test module at all**. Nothing noticed,
because nothing was checking the checkers. `cross_deps`, `toml_sort` and
`preflight` were in the same state and were found by writing this file.

The reference is read from the test files' **AST**, not from their text: a
mutation that only added a `# TODO: <module>` comment passed an earlier
text-matching version of this check, which is the same class of fail-open the
file exists to catch.

This is deliberately weaker than "every gate has a negative control" — it cannot
see whether a test asserts anything useful. It closes the empty case.
"""

from __future__ import annotations
import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

#: Modules whose job is to decide pass/fail for a repository.
CHECKERS = {"celestia_devtools.build.cross_deps": "cross-compilation prerequisites", "celestia_devtools.build.gate": "local gate steps and the credential classifier", "celestia_devtools.ci.cache_policy": "actions/cache policy audit", "celestia_devtools.ci.job_timeouts": "job timeout audit and filler", "celestia_devtools.ci.workflow_audit": "reusable-workflow AST audit", "celestia_devtools.doc.markdown": "markdown format/lint gate", "celestia_devtools.env.preflight": "dev-tool prerequisite probe", "celestia_devtools.lint.cpp_style": "C++ layout gate", "celestia_devtools.lint.nav_lint": "navigation call-site gate", "celestia_devtools.lint.p0_gate": "P0 ledger gate", "celestia_devtools.lint.py_style": "Python layout gate", "celestia_devtools.lint.rules_lint": "workspace rules-tree gate", "celestia_devtools.lint.separator_lint": "separator and conflict-marker gate", "celestia_devtools.lint.ts_policy": "TypeScript strictness and compiler-coverage gate", "celestia_devtools.lint.ts_surface": "published type-surface gate", "celestia_devtools.lint.type_escapes": "TypeScript type-escape gate", "celestia_devtools.lint.tool_audit": "tracked-script exemption ledger", "celestia_devtools.repo.family_versions": "family dependency identity gate", "celestia_devtools.repo.fleet_audit": "canonical-caller and pin audit", "celestia_devtools.repo.toml_sort": "Cargo.toml dependency ordering gate", "celestia_devtools.repo.verify_versions": "version drift gate", "celestia_devtools.vcs.commit_msg": "commit/PR subject gate"}

#: Snapshot of the gate set. Adding a gate means adding it here *and* giving it a
#: test; removing one means editing this tuple, which is the deliberate step.
GATE_SNAPSHOT = ("celestia_devtools.build.cross_deps", "celestia_devtools.build.gate", "celestia_devtools.ci.cache_policy", "celestia_devtools.ci.job_timeouts", "celestia_devtools.ci.workflow_audit", "celestia_devtools.doc.markdown", "celestia_devtools.env.preflight", "celestia_devtools.lint.cpp_style", "celestia_devtools.lint.nav_lint", "celestia_devtools.lint.p0_gate", "celestia_devtools.lint.py_style", "celestia_devtools.lint.rules_lint", "celestia_devtools.lint.separator_lint", "celestia_devtools.lint.ts_policy", "celestia_devtools.lint.ts_surface", "celestia_devtools.lint.type_escapes", "celestia_devtools.lint.tool_audit", "celestia_devtools.repo.family_versions", "celestia_devtools.repo.fleet_audit", "celestia_devtools.repo.toml_sort", "celestia_devtools.repo.verify_versions", "celestia_devtools.vcs.commit_msg")

def _imported_names() -> dict:
    """name → set of modules the test file imports (AST-parsed, comments ignored)."""

    imports: dict = {}

    for path in sorted(_TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            # This file names every gate; counting it would make the check true
            # by construction (that is how the first version passed a mutation).
            continue

        try:
            tree = ast.parse(path.read_text(encoding = "utf-8", errors = "replace"))
        except SyntaxError:
            continue

        found: set = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""

                if module:
                    found.add(module)

                for alias in node.names:
                    found.add(f"{module}.{alias.name}" if module else alias.name)

        imports[path.name] = found

    return imports

def test_the_scan_reads_the_suite():
    # Positive control: an empty or broken walk would make the assertion below
    # pass over nothing at all, which is the failure mode this file guards.
    imports = _imported_names()
    assert len(imports) > 40
    assert any("celestia_devtools" in name for names in imports.values() for name in names)

def test_the_gate_set_matches_its_snapshot():
    assert set(CHECKERS) == set(GATE_SNAPSHOT)

def test_every_decision_gate_is_imported_by_a_test():
    imports = _imported_names()
    missing = sorted(module for module in CHECKERS if not any(module in names for names in imports.values()))
    assert missing == [], f"decision gates with no test import: {missing}"
