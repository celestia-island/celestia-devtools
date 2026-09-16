"""Tests for celestia_devtools.lint.rules_lint."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow running the tests against a source checkout without a pip
# install (PYTHONPATH-style); the CI venv install makes this a no-op.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.lint import rules_lint  # noqa: E402


SKILL_BODY = "\n".join("rule body line %d" % i for i in range(6))
LONG_LINE = "This sentence is long enough to be treated as a rule statement."
OTHER_LONG_LINE = "A second sentence, long enough to be counted as a rule statement."

DEFAULT_CORE = """# Rules

## 1. Core

%s

### 1.1 Moved out

> **全文已移至技能** `demo`（2026-09-16）——动手前先装载。

## 2. Another

See §1 and §1.1 for the base rule.
""" % LONG_LINE

LEDGER_OK = """# Ledger of things

> This is a ledger, not rules. Read on demand.

## Entries

- one
"""


def skill_text(
    name: str = "demo",
    description: str = "A demo rule pack.",
    when: str = "When demoing.",
    body: str = SKILL_BODY,
) -> str:
    return (
        "---\n"
        "name: %s\n"
        "description: %s\n"
        "whenToUse: %s\n"
        "---\n\n"
        "# %s\n\n%s\n" % (name, description, when, name, body)
    )


def make_tree(
    tmp_path: Path,
    *,
    core: str = DEFAULT_CORE,
    skill_name: str = "demo",
    skill: str = None,
    ledgers: dict = None,
) -> Path:
    (tmp_path / "AGENTS.md").write_text(core, encoding="utf-8")
    target = tmp_path / ".agents" / "skills" / skill_name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        skill if skill is not None else skill_text(skill_name), encoding="utf-8"
    )
    for fname, text in (ledgers or {}).items():
        ledger_dir = tmp_path / "_ledger"
        ledger_dir.mkdir(exist_ok=True)
        (ledger_dir / fname).write_text(text, encoding="utf-8")
    return tmp_path


def run(tmp_path: Path, *extra: str) -> int:
    return rules_lint.main([str(tmp_path), *extra])


# ------------------------------------------------------------------ happy path


def test_clean_tree_passes(tmp_path):
    make_tree(tmp_path, ledgers={"things.md": LEDGER_OK})
    assert run(tmp_path) == 0


def test_root_is_discovered_upward(tmp_path):
    make_tree(tmp_path)
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    assert rules_lint.main([str(nested)]) == 0


# --------------------------------------------------- skill frontmatter (the real bug)


def test_unquoted_colon_in_description_is_an_error(tmp_path):
    """Regression: `": "` inside an unquoted YAML scalar made four real skills
    unparseable, and the catalog dropped them without a visible error."""
    make_tree(tmp_path, skill=skill_text(description="A rule pack: with a colon."))
    assert run(tmp_path) == 1


def test_missing_frontmatter_is_an_error(tmp_path):
    make_tree(tmp_path, skill="# demo\n\nno frontmatter at all\n")
    assert run(tmp_path) == 1


def test_empty_description_is_an_error(tmp_path):
    make_tree(tmp_path, skill=skill_text(description="''"))
    assert run(tmp_path) == 1


def test_name_directory_mismatch_is_an_error(tmp_path):
    make_tree(tmp_path, skill=skill_text(name="other"))
    assert run(tmp_path) == 1


def test_short_skill_body_is_an_error(tmp_path):
    make_tree(tmp_path, skill=skill_text(body="only one line"))
    assert run(tmp_path) == 1


# ------------------------------------------------------------------- pointers


def test_pointer_to_missing_skill_is_an_error(tmp_path):
    core = DEFAULT_CORE.replace("`demo`", "`not-there`")
    make_tree(tmp_path, core=core)
    assert run(tmp_path) == 1


def test_pointer_to_empty_skill_is_an_error(tmp_path):
    make_tree(tmp_path, skill=skill_text(body="one\ntwo"))
    assert run(tmp_path) == 1


def test_directory_pointer_must_resolve(tmp_path):
    core = DEFAULT_CORE.replace(
        "> **全文已移至技能** `demo`（2026-09-16）——动手前先装载。",
        "> **全文已移至目录层** `_tools/AGENTS.md`（2026-09-16）。",
    )
    make_tree(tmp_path, core=core)
    assert run(tmp_path) == 1  # target absent
    tools = tmp_path / "_tools"
    tools.mkdir()
    (tools / "AGENTS.md").write_text("# tools\n\nreal content here\n", encoding="utf-8")
    assert run(tmp_path) == 0


# ------------------------------------------------------------------ references


def test_dead_reference_warns_but_does_not_fail(tmp_path):
    core = DEFAULT_CORE + "\nSee §9.9 for details.\n"
    make_tree(tmp_path, core=core)
    assert run(tmp_path) == 0
    assert run(tmp_path, "--strict") == 1


def test_subsection_reference_resolves_via_prefix(tmp_path):
    core = DEFAULT_CORE + "\nSee §1.1.3 for the item-level rule.\n"
    make_tree(tmp_path, core=core)
    assert run(tmp_path, "--strict") == 0


# ------------------------------------------------------------------- ledgers


def test_ledger_without_provenance_is_an_error(tmp_path):
    make_tree(tmp_path, ledgers={"bad.md": "# Ledger\n\n## Section\n\n- item\n"})
    assert run(tmp_path) == 1


def test_ledger_without_sections_is_an_error(tmp_path):
    make_tree(tmp_path, ledgers={"bad.md": "# Ledger\n\n> ledger not rules\n"})
    assert run(tmp_path) == 1


# ------------------------------------------------------------------- secrets


# Fixture values for the private-address detector. They are synthetic (no host in any
# environment uses them); the detector can only be exercised with real private ranges,
# since RFC 5737 documentation addresses are deliberately *not* private.
@pytest.mark.parametrize("bad", ["192.168.7.7", "10.4.4.4", "172.20.5.5", "deadbeef" * 4])
def test_secret_shaped_value_in_injected_surface_is_an_error(tmp_path, bad):
    make_tree(tmp_path, core=DEFAULT_CORE + "\nvalue: %s\n" % bad)
    assert run(tmp_path) == 1


def test_documentation_addresses_are_allowed(tmp_path):
    make_tree(tmp_path, core=DEFAULT_CORE + "\nUse 192.0.2.x and 198.51.100.x placeholders.\n")
    assert run(tmp_path) == 0


@pytest.mark.parametrize("placeholder", ["<your-password>", "CHANGE_ME", "Passcode", "${MY_TOKEN}", "xxxxxxxx"])
def test_placeholder_values_are_not_credentials(tmp_path, placeholder):
    """A gate that fires on documentation examples stops being read."""
    make_tree(tmp_path, core=DEFAULT_CORE + '\npassword: "%s"\n' % placeholder)
    assert run(tmp_path) == 0


@pytest.mark.parametrize("real", ["hunter2xyz", "correcthorsebatterystaple", "aB3$kL9mNp2q"])
def test_entropy_bearing_inline_value_is_a_credential(tmp_path, real):
    make_tree(tmp_path, core=DEFAULT_CORE + '\npassword: "%s"\n' % real)
    assert run(tmp_path) == 1


def test_repo_local_agents_file_is_scanned(tmp_path):
    make_tree(tmp_path)
    repo = tmp_path / "somerepo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# repo\n\nhost = 10.9.9.9\n", encoding="utf-8")
    assert run(tmp_path) == 1


# --------------------------------------------------------------- duplication


def test_duplicate_rule_line_warns(tmp_path):
    make_tree(tmp_path, skill=skill_text(body=SKILL_BODY + "\n" + LONG_LINE))
    assert run(tmp_path) == 0
    assert run(tmp_path, "--strict") == 1


def test_allow_duplicate_suppresses_the_warning(tmp_path):
    make_tree(tmp_path, skill=skill_text(body=SKILL_BODY + "\n" + LONG_LINE))
    assert run(tmp_path, "--strict", "--allow-duplicate", LONG_LINE) == 0


def test_repo_local_files_are_excluded_from_duplication(tmp_path):
    """A repo-local AGENTS.md is a standalone artifact: someone who clones only that
    repository reads it without ever seeing the rules root, so restating a convention
    there is deliberate rather than drift."""
    # OTHER_LONG_LINE lives only in the skill, so the sole duplication is repo-local.
    make_tree(tmp_path, skill=skill_text(body=SKILL_BODY + "\n" + OTHER_LONG_LINE))
    repo = tmp_path / "somerepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# repo\n\n%s\n" % OTHER_LONG_LINE, encoding="utf-8")
    assert run(tmp_path, "--strict") == 0, "repo-local restatement must not fail --strict"
    assert run(tmp_path, "--strict", "--include-repo-local") == 1, "opt-in must expose it"


def test_repo_local_files_are_still_scanned_for_secrets(tmp_path):
    make_tree(tmp_path)
    repo = tmp_path / "somerepo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# repo\n\nhost = 10.9.9.9\n", encoding="utf-8")
    assert run(tmp_path) == 1


# ------------------------------------------------------------------- interface


def test_json_output(tmp_path, capsys):
    make_tree(tmp_path)
    assert run(tmp_path, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == []
    assert payload["root"] == str(tmp_path)


def test_disable_skips_a_rule(tmp_path, capsys):
    make_tree(tmp_path, skill=skill_text(description="A rule pack: with a colon."))
    assert run(tmp_path) == 1
    assert run(tmp_path, "--disable", "skill-frontmatter") == 0


def test_missing_root_reports_usage_error(tmp_path):
    assert rules_lint.main([str(tmp_path)]) == 2
