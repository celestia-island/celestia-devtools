#!/usr/bin/env python3
"""Decide and enforce the family's TypeScript strictness, and audit the blind spots.

*Why this exists:* the 2026-11-01 type-uncertainty review found the family's
semantic gate coverage was inverted. Every application repository ran
`pnpm -r typecheck` on pull requests, while the three repositories that *produce*
the types everybody consumes were the unchecked ones: hikari's Vue half ran only
on GitHub-hosted runners (dead org-wide since 2026-09-24), tairitsu's 40k lines
of WASM glue are 100% `@ts-nocheck`, and entelecheia's generated bindings were
never compiled at all. A word-level `any` gate cannot see any of that, and no
gate asked the other half of the question: *which strictness flags are actually
on, and where does the compiler not look?*

The command answers three questions, one subcommand each::

    celestia-devtools ts-policy check    # is the repo's tsconfig at policy?
    celestia-devtools ts-policy probe    # what would a flag cost today?
    celestia-devtools ts-policy coverage # which source files never enter the program?
    celestia-devtools ts-policy list     # print the policy and this repo's decisions

*check* splits the policy in two tiers (see the bundled ``ts_policy.toml``):
tier A is cheap and mechanical, so a repository that has not turned it on is
simply missing checks and ``check`` fails; tier B moves real code, so the
repository must decide — in its own ``ts-policy.toml`` — and ``check`` fails on
an *undecided* flag. A decision whose claim contradicts the tsconfig ("on" but
the flag is off, or "off" but the flag is on) fails too: a stale ledger that
reads as compliance is worse than no ledger.

*probe* turns the migration argument into a number: it runs the repository's own
checker once as a baseline and once per candidate flag, and reports the error
delta. Nothing is decided automatically; the point is that "turning on
`noUncheckedIndexedAccess` costs 1072 errors" becomes a measurement instead of a
guess.

*coverage* is the anti-void gate. ``tsc --listFilesOnly`` names the files the
compiler will actually read; the tsconfig's own include/exclude globs name the
files it *claims* to cover. Anything in the second set and not the first never
gets checked — an "unchecked island" — and ``@ts-nocheck`` files are reported
separately because they enter the program only to be silenced. Both lists are
what the design's "no silent blindness" invariant means in practice; a
repository may exempt a glob, but the exemption is named in the invocation.

Exit: 0 clean, 1 findings, 2 usage error. A missing tsconfig, an unparsable
policy and an unresolvable ``extends`` are usage/configuration errors, never a
quiet pass: the same fail-closed shape every other gate in this package uses.
"""

from __future__ import annotations
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Sequence
from celestia_devtools.core import envelope

#: The bundled policy (catalog + the all-undecided default decisions).
BUNDLED_POLICY = Path(__file__).with_name("ts_policy.toml")

#: Flags whose strict value is `false` rather than `true`.
INVERTED_FLAGS = frozenset({"allowUnreachableCode"})

#: TypeScript's own default excludes, applied before any tsconfig exclude.
DEFAULT_EXCLUDED_DIRS = ("node_modules", "bower_components", "jspm_packages")

#: Suffixes that count as in-scope source for the coverage audit.
SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".vue", ".mjs", ".cjs")

#: One compiler diagnostic line ("file(1,2): error TS1234: ...").
TS_ERROR = re.compile(r"error TS\d+")

#: The diagnostic-suppression directive the coverage audit counts.
NOCHECK = re.compile(r"@ts-nocheck")

#: How deep an `extends` chain may go before it is treated as a cycle.
MAX_EXTENDS_DEPTH = 8

def strip_jsonc(text: str) -> str:
    """Remove `//` and `/* */` comments and trailing commas from JSONC.

    tsconfig files are JSON with comments; `json.loads` rejects both, and a
    config the checker refuses to read is a blind spot, so the parse has to
    succeed on the files TypeScript itself accepts.
    """

    out: list[str] = []
    index = 0
    in_string = False

    while index < len(text):
        char = text[index]

        if in_string:
            out.append(char)

            if char == "\\":
                if (index + 1) < len(text):
                    out.append(text[index + 1])
                    index += 2
                    continue
            elif char == '"':
                in_string = False

            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue

        if text.startswith("//", index):
            while (index < len(text)) and (text[index] != "\n"):
                index += 1

            continue

        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue

        out.append(char)
        index += 1

    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))

def translate_glob(pattern: str) -> re.Pattern[str]:
    """Compile a tsconfig include/exclude glob into a regex over a posix path.

    The three constructs that matter are `**/` (any directory prefix, possibly
    none), `**` (anything) and `*` (anything but a separator). Everything else
    is literal, which is why the rest is `re.escape`.
    """

    translated: list[str] = []
    normalised = pattern.replace("\\", "/")
    index = 0

    while index < len(normalised):
        if normalised.startswith("**/", index):
            translated.append("(?:.*/)?")
            index += 3
            continue

        if normalised.startswith("**", index):
            translated.append(".*")
            index += 2
            continue

        char = normalised[index]

        if char == "*":
            translated.append("[^/]*")
        elif char == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(char))

        index += 1

    return re.compile("^" + "".join(translated) + "$")

def matches_any(relative: str, patterns: Sequence[str]) -> bool:
    """Whether `relative` (a posix path) matches any of the globs."""

    for pattern in patterns:
        if translate_glob(pattern).match(relative):
            return True

    return False

def _extends_list(value: object) -> list[str]:
    """The `extends` field as a list (TypeScript 5 allows an array)."""

    if isinstance(value, str):
        return [value]

    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]

    return []

def resolve_extend(base: Path, reference: str) -> Path | None:
    """Resolve one `extends` reference the way TypeScript does, cheaply.

    Relative references resolve against the containing file; a bare package
    name resolves against the nearest `node_modules` walking up from it. The
    three candidate suffixes (exact, `.json`, `/tsconfig.json`) cover every
    shape the family uses; an unresolvable reference is reported, never guessed.
    """

    raw = reference.replace("\\", "/")

    if raw.startswith(".") or raw.startswith("/"):
        roots = [base.parent]
    else:
        roots = [directory / "node_modules" for directory in (base.parent, *base.parent.parents)]

    for root in roots:
        for suffix in ("", ".json", "/tsconfig.json"):
            candidate = Path(str(root) + "/" + raw + suffix)

            if candidate.is_file():
                return candidate

    return None

def load_tsconfig(path: Path, chain: tuple[Path, ...] = ()) -> dict:
    """Parse a tsconfig and merge its `extends` chain into one document.

    Returns ``{"path", "options", "include", "exclude", "files", "warnings",
    "sources"}``. Warnings (unresolvable extends, cycles, unparsable parents)
    are returned rather than raised: a config the checker cannot fully read is
    itself a finding, and swallowing it would be the fail-open shape this tool
    exists to catch.
    """

    document: dict = {"path": path, "options": {}, "include": None, "exclude": None, "files": None, "warnings": [], "sources": []}
    resolved = path.resolve()

    if resolved in chain:
        document["warnings"].append(f"extends cycle at {path}")
        return document

    try:
        raw = path.read_text(encoding = "utf-8")
    except OSError as exc:
        document["warnings"].append(f"cannot read {path}: {exc}")
        return document

    try:
        parsed = json.loads(strip_jsonc(raw))
    except json.JSONDecodeError as exc:
        document["warnings"].append(f"{path} is not valid JSONC: {exc}")
        return document

    if not isinstance(parsed, dict):
        document["warnings"].append(f"{path} is not a JSON object")
        return document

    if len(chain) >= MAX_EXTENDS_DEPTH:
        document["warnings"].append(f"extends chain deeper than {MAX_EXTENDS_DEPTH} at {path}")
        return document

    for reference in _extends_list(parsed.get("extends")):
        parent_path = resolve_extend(path, reference)

        if parent_path is None:
            document["warnings"].append(f"{path}: cannot resolve extends {reference!r}")
            continue

        parent = load_tsconfig(parent_path, chain + (resolved,))
        document = _merge_parent(document, parent)

    options = parsed.get("compilerOptions")

    if isinstance(options, dict):
        document["options"].update(options)

    for key in ("include", "exclude", "files"):
        if key in parsed:
            document[key] = parsed[key]

    document["sources"].append(str(path))

    return document

def _merge_parent(document: dict, parent: dict) -> dict:
    """Fold a parent config in underneath the child (child keys win)."""

    merged_options = dict(parent["options"])
    merged_options.update(document["options"])
    document["options"] = merged_options
    document["warnings"].extend(parent["warnings"])
    document["sources"].extend(parent["sources"])

    for key in ("include", "exclude", "files"):
        if document[key] is None:
            document[key] = parent[key]

    return document

def load_policy(path: Path) -> dict | None:
    """Read one policy file; the catalog keys only count when they are present."""

    try:
        document = tomllib.loads(path.read_text(encoding = "utf-8"))
    except OSError as exc:
        print(f"ts-policy: cannot read policy {path}: {exc}", file = sys.stderr)
        return None
    except tomllib.TOMLDecodeError as exc:
        print(f"ts-policy: policy {path} is not valid TOML: {exc}", file = sys.stderr)
        return None

    if not isinstance(document, dict):
        print(f"ts-policy: policy {path} is not a table", file = sys.stderr)
        return None

    return document

def resolve_policy(explicit: str | None, root: Path) -> tuple[Path, dict] | None:
    """Resolve the policy file: flag, environment, repository, bundled."""

    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))
    elif Path(str(root) + "/ts-policy.toml").is_file():
        candidates.append(Path(str(root) + "/ts-policy.toml"))
    else:
        candidates.append(BUNDLED_POLICY)

    for candidate in candidates:
        policy = load_policy(candidate)

        if policy is not None:
            return candidate, policy

    return None

def catalog_of(policy: dict) -> tuple[list[str], list[str]]:
    """The (tier A, tier B) flag catalog declared by a policy document."""

    tier_a = policy.get("tier_a") or {}
    tier_b = policy.get("tier_b") or {}

    return list(tier_a.get("required") or []), list(tier_b.get("flags") or [])

def decisions_of(policy: dict) -> tuple[list[str], dict[str, str]]:
    """The (on, off) tier B decisions declared by a policy document."""

    tier_b = policy.get("tier_b") or {}

    return list(tier_b.get("on") or []), dict(tier_b.get("off") or {})

def _expected_value(flag: str) -> bool:
    """The value a tier A flag must hold."""

    return flag not in INVERTED_FLAGS

def check_tsconfig(config: dict, policy: dict, root: Path | None = None) -> list[dict]:
    """Return the findings for one repository's tsconfig against the policy."""

    tier_a, tier_b = catalog_of(policy)
    on, off = decisions_of(policy)
    options = config["options"]
    findings: list[dict] = []

    for warning in config["warnings"]:
        findings.append({"kind": "tsconfig-unreadable", "severity": "error", "flag": None, "detail": warning})

    for flag in tier_a:
        expected = _expected_value(flag)
        actual = options.get(flag)

        if actual is not expected:
            findings.append({"kind": "tier-a-missing", "severity": "error", "flag": flag, "detail": f"expected {flag} = {str(expected).lower()!r}, found {actual!r}"})

    catalog = set(tier_b)
    unknown = sorted((set(on) | set(off)) - catalog)

    for flag in unknown:
        findings.append({"kind": "tier-b-unknown", "severity": "error", "flag": flag, "detail": "not a tier B flag in the catalog (typo, or the policy moved on)"})

    for flag in sorted(set(on) & set(off)):
        findings.append({"kind": "tier-b-contradiction", "severity": "error", "flag": flag, "detail": "listed both on and off"})

    for flag in tier_b:
        if (flag not in on) and (flag not in off):
            findings.append({"kind": "tier-b-undecided", "severity": "error", "flag": flag, "detail": "no decision: add it to tier_b.on or tier_b.off with a reason in ts-policy.toml"})

    for flag in on:
        if options.get(flag) is not True:
            findings.append({"kind": "tier-b-stale-on", "severity": "error", "flag": flag, "detail": f"policy says on but the tsconfig has {options.get(flag)!r}"})

    for flag in off:
        if options.get(flag) is True:
            findings.append({"kind": "tier-b-stale-off", "severity": "error", "flag": flag, "detail": "policy says off but the tsconfig turns it on (update the ledger, it understates what runs)"})

    if (root is not None) and (options.get("skipLibCheck") is not False):
        shims = _in_tree_declarations(root)

        if shims:
            findings.append({"kind": "advice-skip-libcheck", "severity": "warning", "flag": "skipLibCheck", "detail": f"skipLibCheck hides type errors inside this repository's own {shims} declaration file(s); give them their own compile pass"})

    return findings

def _in_tree_declarations(root: Path) -> int:
    """Count declaration files under `root`, excluding dependencies."""

    count = 0

    for suffix in (".d.ts", ".d.mts", ".d.cts"):
        for path in root.rglob("*" + suffix):
            if any(part in DEFAULT_EXCLUDED_DIRS for part in path.parts):
                continue

            count += 1

    return count

def program_from_listing(text: str) -> set[str]:
    """Parse `tsc --listFilesOnly` output into a set of resolved paths."""

    program: set[str] = set()

    for line in text.splitlines():
        stripped = line.strip()

        if (not stripped) or (("/" not in stripped) and ("\\" not in stripped)):
            continue

        if stripped.startswith(("#", "Project:")):
            continue

        program.add(str(Path(stripped).resolve()))

    return program

def in_scope_files(root: Path, config: dict) -> list[Path]:
    """The files the repository's own globs claim the checker covers."""

    out_dir = config["options"].get("outDir")
    excluded = [str(item) for item in (config["exclude"] or [])]

    if out_dir:
        excluded.append(str(out_dir).rstrip("/") + "/**")

    includes = [str(item) for item in (config["include"] or ["**/*"])]
    candidates: list[Path] = []

    if config["files"]:
        candidates = [root / str(item) for item in config["files"]]
    else:
        for suffix in SOURCE_SUFFIXES:
            candidates.extend(root.rglob("*" + suffix))

    found: list[Path] = []

    for path in candidates:
        relative = path.relative_to(root).as_posix()

        if any(part in DEFAULT_EXCLUDED_DIRS for part in path.parts):
            continue

        if excluded and matches_any(relative, excluded):
            continue

        if not matches_any(relative, includes):
            continue

        found.append(path)

    return sorted(set(found))

def coverage_findings(root: Path, config: dict, program: set[str]) -> dict:
    """Compare the claimed include set with the compiler's program.

    `islands` are in-scope files the program never read; `silenced` are files
    that enter the program only to carry `@ts-nocheck`. Both are debt that a
    green build happily reports as fine.
    """

    claimed = in_scope_files(root, config)
    islands: list[str] = []
    silenced: list[str] = []

    for path in claimed:
        if str(path.resolve()) not in program:
            islands.append(path.relative_to(root).as_posix())

    for entry in sorted(program):
        path = Path(entry)

        try:
            path.relative_to(root)
        except ValueError:
            continue

        if path.suffix not in SOURCE_SUFFIXES:
            continue

        try:
            text = path.read_text(encoding = "utf-8", errors = "replace")
        except OSError:
            continue

        if NOCHECK.search(text):
            silenced.append(path.relative_to(root).as_posix())

    return {"islands": islands, "silenced": silenced, "claimed": len(claimed), "program": len(program)}

def probe_report(baseline_errors: int, runs: Sequence[tuple[str, int]]) -> list[dict]:
    """Turn per-flag error counts into the migration-cost table.

    `runs` is `(flag, error_count)`; the delta is against the baseline run, so a
    repository whose check is already red still gets a usable answer.
    """

    return [{"flag": flag, "errors": errors, "delta": errors - baseline_errors} for flag, errors in runs]

def run_checker(command: str, cwd: Path, extra: Sequence[str] = ()) -> tuple[int, str]:
    """Run the repository's checker with `extra` flags; never raises."""

    argv = shlex.split(command) + list(extra)

    try:
        completed = subprocess.run(argv, cwd = str(cwd), capture_output = True, text = True, check = False)
    except OSError as exc:
        return 127, str(exc)

    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

def count_errors(output: str) -> int:
    """Count compiler diagnostics in one checker run."""

    return len(TS_ERROR.findall(output))

def _print_check(findings: Sequence[dict], config_path: Path) -> None:
    """Human-readable check report grouped by severity."""

    errors = [item for item in findings if item["severity"] == "error"]
    warnings = [item for item in findings if item["severity"] == "warning"]

    for item in errors + warnings:
        flag = f"[{item['flag']}] " if item["flag"] else ""
        print(f"{config_path}: {item['severity']}: {item['kind']}: {flag}{item['detail']}")

    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")

def _print_coverage(document: dict) -> None:
    """Human-readable coverage report."""

    print(f"claimed {document['claimed']} file(s); program holds {document['program']} file(s)")

    if document["islands"]:
        print(f"\n-- unchecked islands ({len(document['islands'])}) --")

        for name in document["islands"][:50]:
            print(f"  {name}")

    if document["silenced"]:
        print(f"\n-- @ts-nocheck files ({len(document['silenced'])}) --")

        for name in document["silenced"][:50]:
            print(f"  {name}")

    if not document["islands"] and not document["silenced"]:
        print("every claimed source file enters the program and none is silenced")

def _print_list(catalog: tuple[list[str], list[str]], decisions: tuple[list[str], dict[str, str]], options: dict) -> None:
    """Print the catalog plus the resolved decisions and their live config value."""

    tier_a, tier_b = catalog
    on, off = decisions

    print("tier A (mandatory):")

    for flag in tier_a:
        print(f"  {flag:34s} config={options.get(flag)!r} expected={_expected_value(flag)!r}")

    print("\ntier B (decided per repository):")

    for flag in tier_b:
        if flag in on:
            state = "on"
        elif flag in off:
            state = f"off ({off[flag]})"
        else:
            state = "UNDECIDED"

        print(f"  {flag:34s} {state:40s} config={options.get(flag)!r}")

SELFTEST_TSCONFIG = {"strict": True, "noImplicitOverride": True, "noImplicitReturns": True, "noFallthroughCasesInSwitch": True, "allowUnreachableCode": False, "exactOptionalPropertyTypes": True}

SELFTEST_POLICY = """
version = 1

[tier_a]
required = ["strict", "noImplicitOverride", "noImplicitReturns", "noFallthroughCasesInSwitch", "allowUnreachableCode"]

[tier_b]
flags = ["exactOptionalPropertyTypes", "noUncheckedIndexedAccess"]
on = ["exactOptionalPropertyTypes"]

[tier_b.off]
noUncheckedIndexedAccess = "selftest: decided off with a reason"
"""

def selftest() -> int:
    """Verify the checker against known-positive and known-negative controls."""

    failures: list[str] = []

    try:
        json.loads(strip_jsonc('{\n  // note\n  "a": 1, /* block */\n  "b": [1, 2,],\n}\n'))
    except json.JSONDecodeError as exc:
        failures.append(f"strip_jsonc: JSONC was not normalised: {exc}")

    if not translate_glob("src/**/*.ts").match("src/deep/x.ts"):
        failures.append("translate_glob: **/ did not cross directories")

    if translate_glob("src/**/*.ts").match("src/x.ts") is None:
        failures.append("translate_glob: **/ did not match the zero-directory case")

    if translate_glob("src/*.ts").match("src/deep/x.ts"):
        failures.append("translate_glob: single * crossed a directory")

    tmp = Path(tempfile.mkdtemp(prefix = "ts-policy-selftest-"))

    try:
        policy_path = tmp / "policy.toml"
        policy_path.write_text(SELFTEST_POLICY, encoding = "utf-8")
        policy = load_policy(policy_path)
        clean = check_tsconfig({"options": dict(SELFTEST_TSCONFIG), "warnings": []}, policy, tmp)

        if clean:
            failures.append(f"check: a compliant config produced {clean}")

        missing = dict(SELFTEST_TSCONFIG)
        del missing["noImplicitOverride"]
        missing_findings = check_tsconfig({"options": missing, "warnings": []}, policy, tmp)

        if [item["kind"] for item in missing_findings] != ["tier-a-missing"]:
            failures.append(f"check: missing tier A flag not reported alone: {missing_findings}")

        wrong_strict = dict(SELFTEST_TSCONFIG)
        wrong_strict["allowUnreachableCode"] = True
        inverted = [item for item in check_tsconfig({"options": wrong_strict, "warnings": []}, policy, tmp) if item["kind"] == "tier-a-missing"]

        if not inverted:
            failures.append("check: the inverted flag (allowUnreachableCode) accepted its non-strict value")

        stale = dict(SELFTEST_TSCONFIG)
        stale["noUncheckedIndexedAccess"] = True

        if not [item for item in check_tsconfig({"options": stale, "warnings": []}, policy, tmp) if item["kind"] == "tier-b-stale-off"]:
            failures.append("check: a tier B 'off' decision that the tsconfig contradicts was accepted")

        undecided = """
version = 1

[tier_b]
flags = ["exactOptionalPropertyTypes"]
on = []

[tier_b.off]
"""
        undecided_path = tmp / "undecided.toml"
        undecided_path.write_text(undecided, encoding = "utf-8")

        if not [item for item in check_tsconfig({"options": dict(SELFTEST_TSCONFIG), "warnings": []}, load_policy(undecided_path), tmp) if item["kind"] == "tier-b-undecided"]:
            failures.append("check: an undecided tier B flag was accepted (silence must not read as a decision)")

        extend_root = tmp / "extends"
        extend_root.mkdir()
        (extend_root / "base.json").write_text('{\n  "compilerOptions": {"strict": true, "noImplicitReturns": true}\n}\n', encoding = "utf-8")
        (extend_root / "tsconfig.json").write_text('{\n  // child\n  "extends": "./base.json",\n  "compilerOptions": {"noUnusedLocals": true}\n}\n', encoding = "utf-8")
        merged = load_tsconfig(extend_root / "tsconfig.json")

        if (merged["options"].get("strict") is not True) or (merged["options"].get("noUnusedLocals") is not True):
            failures.append(f"extends: parent options were not merged: {merged['options']}")

        (extend_root / "cycle.json").write_text('{\n  "extends": "./cycle.json"\n}\n', encoding = "utf-8")

        if not load_tsconfig(extend_root / "cycle.json")["warnings"]:
            failures.append("extends: a cycle was not reported")

        scope_root = tmp / "scope"
        (scope_root / "src" / "deep").mkdir(parents = True)
        (scope_root / "src" / "a.ts").write_text("export const a = 1;\n", encoding = "utf-8")
        (scope_root / "src" / "deep" / "b.ts").write_text("// @ts-nocheck\nexport const b = 2;\n", encoding = "utf-8")
        (scope_root / "src" / "island.ts").write_text("export const c = 3;\n", encoding = "utf-8")
        (scope_root / "node_modules" / "dep").mkdir(parents = True)
        (scope_root / "node_modules" / "dep" / "d.ts").write_text("export const d = 4;\n", encoding = "utf-8")
        scope_config = {"options": {}, "include": ["src/**/*.ts"], "exclude": None, "files": None}
        program = {str((scope_root / "src" / "a.ts").resolve()), str((scope_root / "src" / "deep" / "b.ts").resolve())}
        document = coverage_findings(scope_root, scope_config, program)

        if document["islands"] != ["src/island.ts"]:
            failures.append(f"coverage: islands wrong: {document['islands']}")

        if document["silenced"] != ["src/deep/b.ts"]:
            failures.append(f"coverage: @ts-nocheck list wrong: {document['silenced']}")

        if document["claimed"] != 3:
            failures.append(f"coverage: claimed set wrong ({document['claimed']})")

        if program_from_listing("/a/b.ts\n\n/a/c.ts\n") != {str(Path("/a/b.ts").resolve()), str(Path("/a/c.ts").resolve())}:
            failures.append("coverage: --listFilesOnly parsing lost entries")

        rows = probe_report(3, [("exactOptionalPropertyTypes", 139), ("noUncheckedIndexedAccess", 1075)])

        if [row["delta"] for row in rows] != [136, 1072]:
            failures.append(f"probe: delta arithmetic wrong: {rows}")
    finally:
        shutil.rmtree(tmp, ignore_errors = True)

    if failures:
        for message in failures:
            print(f"SELFTEST FAIL: {message}", file = sys.stderr)

        return 1

    print("selftest OK: JSONC stripped, globs anchored, tier A/B decisions enforced both ways, extends merged, cycle reported, coverage islands + @ts-nocheck split, probe deltas computed")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 0 clean, 1 findings, 2 usage error."""

    parser = argparse.ArgumentParser(prog = "ts-policy", description = "Decide and enforce TypeScript strictness, and audit what the compiler never reads.")
    parser.add_argument("action", nargs = "?", default = "check", choices = ("check", "probe", "coverage", "list"), help = "check (policy conformance), probe (migration cost), coverage (unchecked islands), list (print the policy).")
    parser.add_argument("--tsconfig", default = None, metavar = "PATH", help = "tsconfig to inspect (default: ./tsconfig.json).")
    parser.add_argument("--policy", default = None, metavar = "PATH", help = "policy override (default: <repo>/ts-policy.toml, then the bundled catalog).")
    parser.add_argument("--root", default = None, metavar = "PATH", help = "repository root (default: the tsconfig's directory).")
    parser.add_argument("--command", default = None, metavar = "CMD", help = "checker command for probe/coverage (default: node_modules/.bin/vue-tsc when a .vue file exists, else node_modules/.bin/tsc).")
    parser.add_argument("--flag", action = "append", default = [], metavar = "NAME", help = "probe: a compiler flag to cost out (repeatable; default: every undecided tier B flag).")
    parser.add_argument("--allow-islands", action = "append", default = [], metavar = "GLOB", help = "coverage: exempt an unchecked-island glob (say why in the caller).")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable output.")
    parser.add_argument("--selftest", action = "store_true", help = "Verify the tool against its own controls.")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    config_path = Path(args.tsconfig or "tsconfig.json")

    if not config_path.is_file():
        print(f"ts-policy: no such tsconfig: {config_path}", file = sys.stderr)
        return 2

    config = load_tsconfig(config_path)
    root = Path(args.root) if args.root else config_path.parent.resolve()
    resolved = resolve_policy(args.policy, root)

    if resolved is None:
        return 2

    policy_path, policy = resolved

    if args.action == "list":
        document = {"policy": str(policy_path), "tier_a": catalog_of(policy)[0], "tier_b": catalog_of(policy)[1], "on": decisions_of(policy)[0], "off": decisions_of(policy)[1], "options": config["options"]}

        if args.json:
            envelope.emit(document)
        else:
            _print_list(catalog_of(policy), decisions_of(policy), config["options"])

        return 0

    if args.action == "check":
        findings = check_tsconfig(config, policy, root)
        errors = [item for item in findings if item["severity"] == "error"]

        if args.json:
            envelope.emit({"findings": findings, "errors": len(errors), "warnings": len(findings) - len(errors), "policy": str(policy_path), "tsconfig": str(config_path)})
        else:
            _print_check(findings, config_path)

        return 1 if errors else 0

    command = args.command or default_checker(root)

    if command is None:
        print("ts-policy: no checker found; pass --command (e.g. 'node_modules/.bin/tsc')", file = sys.stderr)
        return 2

    if args.action == "coverage":
        return _run_coverage(args, root, config, config_path, command)

    return _run_probe(args, root, policy, command)

def default_checker(root: Path) -> str | None:
    """Pick the repository's checker: vue-tsc when SFCs exist, else tsc."""

    bin_root = root / "node_modules" / ".bin"

    # A repository that installs vue-tsc is a Vue repository: preferring it is
    # what makes `.vue` sources enter the program instead of reading as islands.
    if (bin_root / "vue-tsc").exists():
        return str(bin_root / "vue-tsc")

    if (bin_root / "tsc").exists():
        return str(bin_root / "tsc")

    return None

def _run_coverage(args: argparse.Namespace, root: Path, config: dict, config_path: Path, command: str) -> int:
    """Run the checker for its program listing and compare against the globs."""

    status, output = run_checker(command, root, ["--listFilesOnly", "-p", str(config_path.resolve())])

    if status not in (0, 1, 2):
        print(f"ts-policy: checker failed with {status}: {output.strip()[:400]}", file = sys.stderr)
        return 2

    document = coverage_findings(root, config, program_from_listing(output))
    allowed = [item for item in document["islands"] if matches_any(item, args.allow_islands)]
    islands = [item for item in document["islands"] if item not in allowed]

    if args.json:
        envelope.emit({"islands": islands, "exempted": allowed, "silenced": document["silenced"], "claimed": document["claimed"], "program": document["program"], "tsconfig": str(config_path)})
    else:
        _print_coverage({"islands": islands, "silenced": document["silenced"], "claimed": document["claimed"], "program": document["program"]})

    return 1 if islands else 0

def _run_probe(args: argparse.Namespace, root: Path, policy: dict, command: str) -> int:
    """Cost out candidate flags against the repository's own checker."""

    status, baseline_output = run_checker(command, root)

    if status not in (0, 1, 2):
        print(f"ts-policy: checker failed with {status}: {baseline_output.strip()[:400]}", file = sys.stderr)
        return 2

    baseline_errors = count_errors(baseline_output)
    tier_a, tier_b = catalog_of(policy)
    on, off = decisions_of(policy)
    flags = args.flag or [flag for flag in tier_b if flag not in on]
    runs: list[tuple[str, int]] = []

    for flag in flags:
        if flag in tier_a:
            print(f"ts-policy: {flag} is tier A (mandatory); probing it is meaningless", file = sys.stderr)
            return 2

        _status, output = run_checker(command, root, ["--" + flag])
        runs.append((flag, count_errors(output)))

    rows = probe_report(baseline_errors, runs)

    if args.json:
        envelope.emit({"baseline_errors": baseline_errors, "rows": rows, "command": command, "decided_off": sorted(off)})
    else:
        print(f"baseline: {baseline_errors} error(s) with the current config ({command})")

        for row in rows:
            print(f"  --{row['flag']:32s} {row['errors']:6d} error(s)   delta {row['delta']:+d}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
