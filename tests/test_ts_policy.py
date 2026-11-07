# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for celestia_devtools.lint.ts_policy.

The selftest is the control CI relies on; these pin the contract the reusable
workflow depends on — exit codes (0 clean, 1 findings, 2 usage), the tier A/B
split in both directions, the catalog/decisions separation (a repository may
state its position but not delete a question), the glob handling real tsconfigs
use (`./` prefixes, bare directory names, `files` + `include`), and the coverage
split.
"""

from __future__ import annotations
import json
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from celestia_devtools.core import cli  # noqa: E402
from celestia_devtools.lint import ts_policy  # noqa: E402

COMPLIANT = {"strict": True, "noImplicitOverride": True, "noImplicitReturns": True, "noFallthroughCasesInSwitch": True, "allowUnreachableCode": False, "exactOptionalPropertyTypes": True}

CATALOG = """
version = 1

[tier_a]
required = ["strict", "noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode"]
strict_family = ["strictNullChecks", "noImplicitAny"]

[tier_b]
flags = ["exactOptionalPropertyTypes", "noUncheckedIndexedAccess"]
on = []

[tier_b.off]
"""

DECIDED = """
version = 1

[tier_b]
on = ["exactOptionalPropertyTypes"]

[tier_b.off]
noUncheckedIndexedAccess = "pilot: new files only"
"""

def write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_text(body, encoding = "utf-8")

    return target

def catalog_at(tmp_path: Path) -> dict:
    return ts_policy.load_policy(write(tmp_path, "catalog.toml", CATALOG))

def decided_at(tmp_path: Path) -> dict:
    return ts_policy.load_policy(write(tmp_path, "decisions.toml", DECIDED))

def findings_for(tmp_path: Path, options: dict) -> list[dict]:
    return ts_policy.check_tsconfig({"options": options, "warnings": []}, catalog_at(tmp_path), decided_at(tmp_path))

def kinds(findings) -> list[str]:
    return [item["kind"] for item in findings]

def test_selftest_passes(capsys):
    assert ts_policy.main(["--selftest"]) == 0
    assert "selftest OK" in capsys.readouterr().out

def test_dispatcher_reaches_the_command():
    assert cli.COMMANDS["ts-policy"] == "celestia_devtools.lint.ts_policy"
    assert cli.COMMANDS["ts-surface"] == "celestia_devtools.lint.ts_surface"

def test_strip_jsonc_keeps_strings_intact():
    body = '{\n  // comment\n  "url": "https://example.test/x", /* block */\n  "list": [1, 2,],\n}\n'
    parsed = json.loads(ts_policy.strip_jsonc(body))
    assert parsed["url"] == "https://example.test/x"
    assert parsed["list"] == [1, 2]

def test_translate_glob_anchors_and_crosses_directories():
    assert ts_policy.translate_glob("src/**/*.ts").match("src/a.ts")
    assert ts_policy.translate_glob("src/**/*.ts").match("src/deep/a.ts")
    assert not ts_policy.translate_glob("src/*.ts").match("src/deep/a.ts")
    assert not ts_policy.translate_glob("src/**/*.ts").match("other/src/a.ts")

def test_expand_globs_normalises_the_forms_tsconfigs_use(tmp_path):
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    assert ts_policy.expand_globs(["src"], tmp_path) == ["src/**/*"]
    assert ts_policy.expand_globs(["./src/**/*.ts"], tmp_path) == ["src/**/*.ts"]
    assert ts_policy.expand_globs(["src/**/*.ts"], tmp_path) == ["src/**/*.ts"]

def test_extends_chain_merges_parent_options(tmp_path):
    write(tmp_path, "base.json", '{\n  "compilerOptions": {"strict": true, "noImplicitReturns": true}\n}\n')
    config = write(tmp_path, "tsconfig.json", '{\n  // child overrides one key\n  "extends": "./base.json",\n  "compilerOptions": {"noImplicitReturns": false}\n}\n')
    merged = ts_policy.load_tsconfig(config)
    assert merged["options"]["strict"] is True
    assert merged["options"]["noImplicitReturns"] is False

def test_extends_cycle_is_reported_not_raised(tmp_path):
    config = write(tmp_path, "cycle.json", '{\n  "extends": "./cycle.json"\n}\n')
    assert ts_policy.load_tsconfig(config)["warnings"]

def test_check_accepts_a_compliant_config(tmp_path):
    assert findings_for(tmp_path, dict(COMPLIANT)) == []

def test_a_repository_file_cannot_delete_the_catalog(tmp_path):
    """The decisions file may state a position; it may not remove the questions."""

    empty = ts_policy.load_policy(write(tmp_path, "empty.toml", "version = 1\n"))
    findings = ts_policy.check_tsconfig({"options": {"strict": False}, "warnings": []}, catalog_at(tmp_path), empty)
    assert len([item for item in findings if item["kind"] == "tier-b-undecided"]) == 2
    assert "strict" in {item["flag"] for item in findings if item["kind"] == "tier-a-missing"}

def test_declaring_only_decisions_is_the_documented_shape(tmp_path):
    findings = findings_for(tmp_path, dict(COMPLIANT))
    assert "tier-b-unknown" not in kinds(findings)

def test_check_reports_every_missing_tier_a_flag(tmp_path):
    missed = {item["flag"] for item in findings_for(tmp_path, {"strict": True}) if item["kind"] == "tier-a-missing"}
    assert missed == {"noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode"}

def test_check_treats_allow_unreachable_code_as_inverted(tmp_path):
    assert "allowUnreachableCode" in {item["flag"] for item in findings_for(tmp_path, dict(COMPLIANT, allowUnreachableCode = True)) if item["kind"] == "tier-a-missing"}

def test_check_refuses_a_strict_family_flag_switched_back_off(tmp_path):
    """`strict: true` plus an explicit `strictNullChecks: false` is not compliance."""

    findings = findings_for(tmp_path, dict(COMPLIANT, strictNullChecks = False))
    assert [item["flag"] for item in findings if item["kind"] == "tier-a-strict-family"] == ["strictNullChecks"]

def test_check_refuses_an_undecided_tier_b_flag(tmp_path):
    policy = ts_policy.load_policy(write(tmp_path, "silent.toml", 'version = 1\n\n[tier_b]\nflags = ["exactOptionalPropertyTypes"]\n'))
    findings = ts_policy.check_tsconfig({"options": dict(COMPLIANT), "warnings": []}, policy, policy)
    assert [item["flag"] for item in findings if item["kind"] == "tier-b-undecided"] == ["exactOptionalPropertyTypes"]

def test_check_refuses_a_ledger_that_contradicts_the_config(tmp_path):
    assert "tier-b-stale-on" in kinds(findings_for(tmp_path, dict(COMPLIANT, exactOptionalPropertyTypes = False)))
    assert "tier-b-stale-off" in kinds(findings_for(tmp_path, dict(COMPLIANT, noUncheckedIndexedAccess = True)))

def test_check_refuses_unknown_and_contradictory_decisions(tmp_path):
    policy = ts_policy.load_policy(write(tmp_path, "bad.toml", 'version = 1\n\n[tier_b]\nflags = ["noUnusedLocals"]\non = ["noUnusedLocals", "typoFlag"]\n\n[tier_b.off]\nnoUnusedLocals = "also off"\n'))
    found = kinds(ts_policy.check_tsconfig({"options": dict(COMPLIANT), "warnings": []}, policy, policy))
    assert "tier-b-unknown" in found
    assert "tier-b-contradiction" in found

def test_check_warns_that_skip_lib_check_hides_local_shims(tmp_path):
    write(tmp_path, "src/env.d.ts", "declare const injected: string;\n")
    findings = ts_policy.check_tsconfig({"options": dict(COMPLIANT, skipLibCheck = True), "warnings": []}, catalog_at(tmp_path), decided_at(tmp_path), tmp_path)
    assert "advice-skip-libcheck" in kinds(findings)

def test_in_scope_files_honors_a_bare_directory_include(tmp_path):
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/deep/b.tsx", "export const b = 2;\n")
    config = {"options": {}, "include": ["./src"], "exclude": None, "files": None}
    names = [path.relative_to(tmp_path).as_posix() for path in ts_policy.in_scope_files(tmp_path, config)]
    assert names == ["src/a.ts", "src/deep/b.tsx"]

def test_in_scope_files_honors_a_bare_directory_exclude(tmp_path):
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "dist/bundle.ts", "export const b = 2;\n")
    write(tmp_path, "node_modules/dep/c.ts", "export const c = 3;\n")
    config = {"options": {}, "include": ["**/*.ts"], "exclude": ["dist"], "files": None}
    names = [path.relative_to(tmp_path).as_posix() for path in ts_policy.in_scope_files(tmp_path, config)]
    assert names == ["src/a.ts"]

def test_in_scope_files_unions_files_and_include(tmp_path):
    write(tmp_path, "env/globals.ts", "export const g = 1;\n")
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    config = {"options": {}, "include": ["src/**/*.ts"], "exclude": ["env"], "files": ["env/globals.ts"]}
    names = [path.relative_to(tmp_path).as_posix() for path in ts_policy.in_scope_files(tmp_path, config)]
    assert names == ["env/globals.ts", "src/a.ts"]

def test_coverage_splits_islands_from_silenced_files(tmp_path):
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/island.ts", "export const c = 3;\n")
    write(tmp_path, "src/muted.ts", "// @ts-nocheck\nexport const d = 4;\n")
    config = {"options": {}, "include": ["src/**/*.ts"], "exclude": None, "files": None}
    program = {str((tmp_path / "src" / "a.ts").resolve()), str((tmp_path / "src" / "muted.ts").resolve())}
    document = ts_policy.coverage_findings(tmp_path, config, program)
    assert document["islands"] == ["src/island.ts"]
    assert document["silenced"] == ["src/muted.ts"]

def test_program_from_listing_ignores_non_paths():
    listing = "Project: /w/tsconfig.json\n/w/src/a.ts\n/w/node_modules/x.d.ts\n\n"
    assert ts_policy.program_from_listing(listing) == {str(Path("/w/src/a.ts").resolve()), str(Path("/w/node_modules/x.d.ts").resolve())}

def test_probe_report_is_a_delta_against_the_baseline():
    rows = ts_policy.probe_report(5, [("exactOptionalPropertyTypes", 141), ("noUncheckedIndexedAccess", 1077)])
    assert [row["delta"] for row in rows] == [136, 1072]

def test_missing_tsconfig_is_a_usage_error(tmp_path, capsys):
    assert ts_policy.main(["--tsconfig", str(tmp_path / "absent.json")]) == 2
    assert "no such tsconfig" in capsys.readouterr().err

def test_check_exits_one_on_findings(tmp_path, capsys):
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true}\n}\n')
    write(tmp_path, "ts-policy.toml", DECIDED)
    assert ts_policy.main(["check", "--tsconfig", str(config)]) == 1
    assert "tier-a-missing" in capsys.readouterr().out

def test_list_prints_the_catalog_and_decisions(tmp_path, capsys):
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "exactOptionalPropertyTypes": true}\n}\n')
    write(tmp_path, "ts-policy.toml", DECIDED)
    assert ts_policy.main(["list", "--tsconfig", str(config)]) == 0
    out = capsys.readouterr().out
    assert "tier A (mandatory)" in out
    assert "noUncheckedIndexedAccess" in out

def fake_checker(tmp_path: Path, body: str) -> str:
    script = write(tmp_path, "fake_tsc.py", body)

    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

def coverage_tree(tmp_path: Path) -> Path:
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true},\n  "include": ["src/**/*.ts"]\n}\n')
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/island.ts", "export const b = 2;\n")

    return config

def test_coverage_exits_one_on_an_unchecked_island(tmp_path, capsys):
    config = coverage_tree(tmp_path)
    checker = fake_checker(tmp_path, 'import pathlib\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(config), "--command", checker]) == 1
    assert "unchecked islands" in capsys.readouterr().out

def test_coverage_exempts_a_named_island_glob(tmp_path):
    config = coverage_tree(tmp_path)
    checker = fake_checker(tmp_path, 'import pathlib\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(config), "--command", checker, "--allow-islands", "src/island.ts"]) == 0

def test_coverage_fails_on_a_silenced_file_unless_allowed(tmp_path):
    """A tree can silence every file with @ts-nocheck; that must not read as clean."""

    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true},\n  "include": ["src/**/*.ts"]\n}\n')
    write(tmp_path, "src/a.ts", "// @ts-nocheck\nexport const a = 1;\n")
    checker = fake_checker(tmp_path, 'import pathlib\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(config), "--command", checker]) == 1
    assert ts_policy.main(["coverage", "--tsconfig", str(config), "--command", checker, "--allow-nocheck"]) == 0

def test_coverage_reports_a_directory_exclude_as_excluded(tmp_path, capsys):
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true},\n  "include": ["**/*.ts"],\n  "exclude": ["dist"]\n}\n')
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "dist/bundle.ts", "export const b = 2;\n")
    checker = fake_checker(tmp_path, 'import pathlib\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(config), "--command", checker]) == 0
    assert "unchecked islands" not in capsys.readouterr().out

def test_probe_passes_the_tsconfig_it_was_given(tmp_path, capsys):
    """Probing must measure the named project, not whatever tsc finds in cwd."""

    config = write(tmp_path, "strict.json", '{\n  "compilerOptions": {"strict": true}\n}\n')
    write(tmp_path, "ts-policy.toml", DECIDED)
    checker = fake_checker(tmp_path, 'import sys\nprint("x.ts(1,1): error TS1000: baseline")\nprint("x.ts(2,1): error TS1000: extra" if "--exactOptionalPropertyTypes" in sys.argv else "")\n')
    assert ts_policy.main(["probe", "--tsconfig", str(config), "--command", checker, "--flag", "exactOptionalPropertyTypes"]) == 0
    out = capsys.readouterr().out
    assert "baseline: 1 error(s)" in out
    assert "delta +1" in out

def test_the_policy_environment_variable_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("CELESTIA_TS_POLICY", str(write(tmp_path, "env-policy.toml", DECIDED)))
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "exactOptionalPropertyTypes": true}\n}\n')
    assert ts_policy.main(["check", "--tsconfig", str(config)]) == 1
    assert ts_policy.main(["list", "--tsconfig", str(config)]) == 0

def test_decisions_are_found_from_a_workspace_leaf(tmp_path, monkeypatch, capsys):
    """A monorepo leaf tsconfig must find the repo-root decisions by walking up.

    The first version looked only beside the tsconfig, so every leaf read as
    "all tier B flags undecided" — found the hard way wiring entelecheia, whose
    only tsconfig lives at packages/webui_bindings.
    """

    monkeypatch.delenv("CELESTIA_TS_POLICY", raising = False)
    body = ", ".join(f'"{flag}": true' for flag in ("noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode", "strict", "exactOptionalPropertyTypes"))
    write(tmp_path, "ts-policy.toml", DECIDED)
    write(tmp_path, "packages/webui/tsconfig.json", '{\n  "compilerOptions": {' + body + '}\n}\n')
    resolved = ts_policy.resolve_decisions(None, tmp_path / "packages" / "webui")
    expected = tmp_path / "ts-policy.toml"
    assert (resolved is not None) and (resolved[0] == expected)
    assert ts_policy.main(["check", "--tsconfig", str(tmp_path / "packages" / "webui" / "tsconfig.json")]) == 1
    undecided = [line for line in capsys.readouterr().out.splitlines() if "tier-b-undecided" in line]
    # DECIDED covers two of the catalog's five flags; the proof of inheritance
    # is that THOSE two no longer read as undecided.
    decided_flags = ("exactOptionalPropertyTypes", "noUncheckedIndexedAccess")
    inherited = all(all(flag not in line for flag in decided_flags) for line in undecided)
    assert inherited and undecided, "leaf config did not inherit the root decisions"
