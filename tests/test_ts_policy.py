# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Tests for celestia_devtools.lint.ts_policy.

The selftest is the control CI relies on; these pin the contract the reusable
workflow depends on — exit codes (0 clean, 1 findings, 2 usage), the tier A/B
semantics in both directions, the `extends` walk, and the coverage split.
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

DECIDED_POLICY = """
version = 1

[tier_a]
required = ["strict", "noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode"]

[tier_b]
flags = ["exactOptionalPropertyTypes", "noUncheckedIndexedAccess"]
on = ["exactOptionalPropertyTypes"]

[tier_b.off]
noUncheckedIndexedAccess = "pilot: new files only"
"""

def write(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_text(body, encoding = "utf-8")

    return target

def policy_at(tmp_path: Path) -> dict:
    return ts_policy.load_policy(write(tmp_path, "policy.toml", DECIDED_POLICY))

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

def test_extends_chain_merges_parent_options(tmp_path):
    write(tmp_path, "base.json", '{\n  "compilerOptions": {"strict": true, "noImplicitReturns": true}\n}\n')
    config = write(tmp_path, "tsconfig.json", '{\n  // child overrides one key\n  "extends": "./base.json",\n  "compilerOptions": {"noImplicitReturns": false}\n}\n')
    merged = ts_policy.load_tsconfig(config)
    assert merged["options"]["strict"] is True
    assert merged["options"]["noImplicitReturns"] is False

def test_extends_cycle_is_reported_not_raised(tmp_path):
    config = write(tmp_path, "cycle.json", '{\n  "extends": "./cycle.json"\n}\n')
    merged = ts_policy.load_tsconfig(config)
    assert merged["warnings"]

def test_check_accepts_a_compliant_config(tmp_path):
    findings = ts_policy.check_tsconfig({"options": dict(COMPLIANT), "warnings": []}, policy_at(tmp_path))
    assert findings == []

def test_check_reports_every_missing_tier_a_flag(tmp_path):
    config = {"options": {"strict": True}, "warnings": []}
    missed = {item["flag"] for item in ts_policy.check_tsconfig(config, policy_at(tmp_path)) if item["kind"] == "tier-a-missing"}
    assert missed == {"noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode"}

def test_check_treats_allow_unreachable_code_as_inverted(tmp_path):
    config = {"options": dict(COMPLIANT, allowUnreachableCode = True), "warnings": []}
    assert "allowUnreachableCode" in {item["flag"] for item in ts_policy.check_tsconfig(config, policy_at(tmp_path)) if item["kind"] == "tier-a-missing"}

def test_check_refuses_an_undecided_tier_b_flag(tmp_path):
    policy = ts_policy.load_policy(write(tmp_path, "silent.toml", 'version = 1\n\n[tier_b]\nflags = ["exactOptionalPropertyTypes"]\n'))
    findings = ts_policy.check_tsconfig({"options": dict(COMPLIANT), "warnings": []}, policy)
    assert [item["flag"] for item in findings if item["kind"] == "tier-b-undecided"] == ["exactOptionalPropertyTypes"]

def test_check_refuses_a_ledger_that_contradicts_the_config(tmp_path):
    policy = policy_at(tmp_path)
    stale_on = dict(COMPLIANT)
    stale_on["exactOptionalPropertyTypes"] = False
    assert "tier-b-stale-on" in kinds(ts_policy.check_tsconfig({"options": stale_on, "warnings": []}, policy))
    stale_off = dict(COMPLIANT)
    stale_off["noUncheckedIndexedAccess"] = True
    assert "tier-b-stale-off" in kinds(ts_policy.check_tsconfig({"options": stale_off, "warnings": []}, policy))

def test_check_refuses_unknown_and_contradictory_decisions(tmp_path):
    policy = ts_policy.load_policy(write(tmp_path, "bad.toml", 'version = 1\n\n[tier_b]\nflags = ["noUnusedLocals"]\non = ["noUnusedLocals", "typoFlag"]\n\n[tier_b.off]\nnoUnusedLocals = "also off"\n'))
    found = kinds(ts_policy.check_tsconfig({"options": dict(COMPLIANT), "warnings": []}, policy))
    assert "tier-b-unknown" in found
    assert "tier-b-contradiction" in found

def test_check_warns_that_skip_lib_check_hides_local_shims(tmp_path):
    write(tmp_path, "src/env.d.ts", "declare const injected: string;\n")
    config = {"options": dict(COMPLIANT, skipLibCheck = True), "warnings": []}
    assert "advice-skip-libcheck" in kinds(ts_policy.check_tsconfig(config, policy_at(tmp_path), tmp_path))

def test_in_scope_files_honors_include_exclude_and_defaults(tmp_path):
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/deep/b.tsx", "export const b = 2;\n")
    write(tmp_path, "node_modules/dep/c.ts", "export const c = 3;\n")
    write(tmp_path, "dist/bundle.ts", "export const d = 4;\n")
    write(tmp_path, "README.md", "# not source\n")
    config = {"options": {}, "include": ["src/**/*.ts", "src/**/*.tsx"], "exclude": ["dist/**"], "files": None}
    names = [path.relative_to(tmp_path).as_posix() for path in ts_policy.in_scope_files(tmp_path, config)]
    assert names == ["src/a.ts", "src/deep/b.tsx"]

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
    assert ts_policy.main(["check", "--tsconfig", str(config), "--policy", str(write(tmp_path, "p.toml", DECIDED_POLICY))]) == 1
    assert "tier-a-missing" in capsys.readouterr().out

def test_list_prints_the_catalog_and_decisions(tmp_path, capsys):
    config = write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true, "exactOptionalPropertyTypes": true}\n}\n')
    assert ts_policy.main(["list", "--tsconfig", str(config), "--policy", str(write(tmp_path, "p.toml", DECIDED_POLICY))]) == 0
    out = capsys.readouterr().out
    assert "tier A (mandatory)" in out
    assert "noUncheckedIndexedAccess" in out

def fake_checker(tmp_path: Path, body: str) -> str:
    script = write(tmp_path, "fake_tsc.py", body)

    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"

def test_coverage_exits_one_on_an_unchecked_island(tmp_path, capsys):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true},\n  "include": ["src/**/*.ts"]\n}\n')
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/island.ts", "export const b = 2;\n")
    checker = fake_checker(tmp_path, 'import pathlib, sys\nfor name in sys.argv[1:]:\n    if pathlib.Path(name).is_file():\n        print(pathlib.Path(name).resolve())\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker]) == 1
    assert "unchecked islands" in capsys.readouterr().out

def test_coverage_exempts_a_named_island_glob(tmp_path):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true},\n  "include": ["src/**/*.ts"]\n}\n')
    write(tmp_path, "src/a.ts", "export const a = 1;\n")
    write(tmp_path, "src/island.ts", "export const b = 2;\n")
    checker = fake_checker(tmp_path, 'import pathlib\nprint(pathlib.Path("src/a.ts").resolve())\n')
    assert ts_policy.main(["coverage", "--tsconfig", str(tmp_path / "tsconfig.json"), "--command", checker, "--allow-islands", "src/island.ts"]) == 0

def test_probe_reports_the_delta_and_never_fails_on_errors(tmp_path, capsys):
    write(tmp_path, "tsconfig.json", '{\n  "compilerOptions": {"strict": true}\n}\n')
    write(tmp_path, "p.toml", DECIDED_POLICY)
    checker = fake_checker(tmp_path, 'import sys\nprint("x.ts(1,1): error TS1000: baseline")\nprint("x.ts(2,1): error TS1000: extra" if "--exactOptionalPropertyTypes" in sys.argv else "")\n')
    assert ts_policy.main(["probe", "--tsconfig", str(tmp_path / "tsconfig.json"), "--policy", str(tmp_path / "p.toml"), "--command", checker, "--flag", "exactOptionalPropertyTypes"]) == 0
    out = capsys.readouterr().out
    assert "baseline: 1 error(s)" in out
    assert "delta +1" in out
