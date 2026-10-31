#!/usr/bin/env python3
"""Fail a tree that reintroduces TypeScript type escape hatches.

*Why this exists:* twelve repositories were driven to zero occurrences of the
assertions that silently delete type information. Nothing enforced that
property afterwards, so the first `as unknown as T` merged after the cleanup
would be invisible — the same fail-open shape the workspace keeps re-learning.
`_tools/ts_escape_scan.py` proved the audit could be mechanical; that path is
not in any git tree, so the gate belongs in the shared CLI the runner farm
already installs (the same way `commit-msg-lint` and `rules-lint` do).

*What the first version could not see* (2026-11-01 review). A word-level `any`
ban is a painkiller, not a cure: nothing stopped `type Any = any` from
laundering every `any` in a file, nothing caught the `any` that arrives with no
`any` token at all (`JSON.parse(text)`, `res.json()`), and nothing watched the
lane the gate runs on. The redesign adds three axes.

*Rulesets.* ``core`` is the always-on set (the original seven rules plus the
laundering and ambient shapes). ``strict`` adds the shapes whose fix costs a
migration, so a repository opts in only after paying it down: ``any-source``
(untyped-by-construction APIs), ``function-type`` (an erased callable is an
``any`` with better manners), ``event-target-cast`` (the DOM's own
``EventTarget`` widening) and ``cast-never`` (a src-only lie; the same token is
legitimate inside tests as deliberate bad-input injection).

*Profiles.* A finding is not equally alarming everywhere, so the scan labels
each file ``src`` / ``test`` / ``generated`` / ``dts`` from its path and header
and rules declare the profiles they apply to. ``cast-never`` is the first
beneficiary: 116 of hikari's 125 occurrences are tests feeding deliberately
invalid values, which is good testing, not debt.

*Ratchet.* Some shapes are too large to ban outright (hikari alone carries
~1.8k plain ``as`` assertions and ~2.8k non-null assertions); banning them
would push the codebase into worse workarounds. They are counted instead, and
``--baseline`` makes the counts monotone: a file that already carries debt may
not grow, and by default a new file may not introduce any. ``--update-baseline``
rewrites the ledger; it is JSON so a reviewer can diff exactly what debt a PR
took on.

*Suppression* is explicit and reviewable, never silent::

    same line   // ts-escape-ok: <reason>
    whole file  // ts-escape-allow-file: <reason> ledger=<id>

The file-level marker has to name a ledger entry AND sit on a file whose header
says it is generated. The two largest exemptions in the family are generated
trees (tairitsu's 40k-line WASM glue); an exemption with no id is exactly the
"regression nobody can trace" failure this module exists to prevent, and a
hand-written file must not be able to paste itself out of the gate.

Plain ``unknown`` annotations are NOT flagged. `unknown` is the safe counterpart
of `any` (type-guard parameters, catch clauses, external input) and banning it
pushes code back toward `any`; this gate only removes the ways of *lying* to
the compiler. The unsafe use of ``unknown`` is the assertion out of it, which
``double-cast`` and ``any-source`` already cover.

*Usage*::

    celestia-devtools type-escapes                  # scan the repository root
    celestia-devtools type-escapes packages src     # explicit paths
    celestia-devtools type-escapes --ruleset strict
    celestia-devtools type-escapes --inventory      # per-rule/per-profile counts
    celestia-devtools type-escapes --baseline .type-escapes-baseline.json
    celestia-devtools type-escapes --update-baseline .type-escapes-baseline.json
    celestia-devtools type-escapes --fail-on-dts    # enforce declarations too
    celestia-devtools type-escapes --selftest

`*.d.ts`, `*.d.mts` and `*.d.cts` files are reported in a separate advisory
section by default: a hand-written shim sometimes has to model an untyped
upstream, but a facade typed `any` hides the checking of every caller, so the
section is never silently dropped — `--fail-on-dts` moves it into the exit code.

Exit: 0 clean, 1 violations (or ratchet growth), 2 usage error. A missing path,
and a target that holds no scannable file at all (`--allow-empty` to accept the
latter explicitly), are usage errors: a mistyped path must never read as clean.
"""

from __future__ import annotations
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Sequence
from celestia_devtools.core import envelope

#: File suffixes scanned (TypeScript family, incl. Vue SFC script blocks).
INCLUDE = ("*.ts", "*.tsx", "*.vue", "*.mts", "*.cts")

#: Path segments never scanned (build output, dependencies, VCS internals).
SKIP_SEGMENTS = ("/node_modules/", "/dist/", "/target/", "/.git/", "/coverage/", "/build/")

#: Every declaration-file suffix. The first version only knew `.d.ts`, so
#: hikari's hand-written `.d.mts` plugin shims silently counted as source.
DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")

#: Directory names whose files are tests (profile ``test``).
TEST_SEGMENTS = ("/test/", "/tests/", "/__tests__/", "/__mocks__/", "/e2e/")

#: File names that are tests regardless of directory.
TEST_NAMES = re.compile(r"\.(?:test|spec)\.[cm]?tsx?$|\.test-d\.[cm]?ts$")

#: Header marker that makes a file generated (profile ``generated``). Only the
#: leading lines are inspected: a marker further down is prose.
GENERATED_HEADER = re.compile(r"auto-?generated|@generated|do not edit|this file was generated", re.IGNORECASE)

#: How many leading lines the generated-header probe reads.
GENERATED_HEADER_WINDOW = 12

#: Rule tiers: enforced always (``core``), enforced only under
#: ``--ruleset strict``, or counted for the ratchet (``count``).
TIER_CORE = "core"
TIER_STRICT = "strict"
TIER_COUNT = "count"

#: Profiles a rule can apply to. A rule declaring ``None`` applies everywhere.
PROFILE_SRC = "src"
PROFILE_TEST = "test"
PROFILE_GENERATED = "generated"
PROFILE_DTS = "dts"

#: `(name, pattern, tier, profiles)`; profiles is a frozenset or None for
#: "every profile". Every literal stays on one physical line: the org's
#: py-style checker forbids multi-line logical lines.
RULES: Sequence[tuple[str, re.Pattern[str], str, frozenset[str] | None]] = [("double-cast", re.compile(r"\bas\s+unknown\s+as\b"), TIER_CORE, None), ("paren-dbl-cast", re.compile(r"\bas\s+unknown\s*\)\s*as\b"), TIER_CORE, None), ("cast-any", re.compile(r"\bas\s+any\b|\bas\s+any\s*\[\s*\]"), TIER_CORE, None), ("any-annot", re.compile(r":\s*any\b"), TIER_CORE, None), ("any-generic", re.compile(r"(?:[<\[(,]\s*any\s*[>\]),;]|Promise\s*<\s*any\s*>|Record\s*<[^<>]*,?\s*any\s*>|Array\s*<\s*any\s*>|\bany\s*\[\s*\])"), TIER_CORE, None), ("any-return", re.compile(r"=>\s*any\b"), TIER_CORE, None), ("type-alias-any", re.compile(r"\btype\s+[A-Za-z_$][\w$]*(?:\s*<[^<>]*>)?\s*=\s*(?:any\b|[^\n]*?\bany\b)"), TIER_CORE, None), ("eslint-type-disable", re.compile(r"eslint-disable(?:-next-line|-line)?\b[^\n]*(?:@typescript-eslint/no-explicit-any|@typescript-eslint/no-unsafe-)"), TIER_CORE, None), ("emits-array-form", re.compile(r"\bemits\s*:\s*\["), TIER_CORE, frozenset({PROFILE_SRC})), ("cast-never", re.compile(r"\bas\s+never\b"), TIER_CORE, frozenset({PROFILE_SRC, PROFILE_GENERATED})), ("any-source", re.compile(r"\bJSON\.parse\s*\(|\.json\s*\(\s*\)|\bnew\s+Function\s*\(|\beval\s*\(|\bObject\.create\s*\(|\bReflect\.get\s*\("), TIER_STRICT, frozenset({PROFILE_SRC, PROFILE_GENERATED})), ("function-type", re.compile(r":\s*Function\b|=>\s*Function\b"), TIER_STRICT, frozenset({PROFILE_SRC, PROFILE_GENERATED})), ("event-target-cast", re.compile(r"\.target\s+as\s+[\w<(]"), TIER_STRICT, frozenset({PROFILE_SRC, PROFILE_GENERATED})), ("suppress", re.compile(r"@ts-(?:nocheck|ignore|expect-error)\b"), TIER_CORE, None), ("as-assert", re.compile(r"\bas\s+(?!const\b)(?!never\b)[A-Z]"), TIER_COUNT, None), ("nonnull-assert", re.compile(r"[\w\)\]]!(?=[.\)\[,;\s])"), TIER_COUNT, None)]

#: Rules that live in a comment by construction: they must be matched before
#: the comment-line filter, or they would never fire at all.
COMMENT_RULES = ("suppress", "eslint-type-disable")

#: Same-line allowlist: the marker must carry a reason (anything non-empty).
OK_LINE = re.compile(r"ts-escape-ok\s*:\s*\S")

#: Whole-file allowlist. It has to name a reason AND a ledger id, because an
#: unnamed exemption is exactly how a generated tree re-acquires hand-edited
#: escapes with nobody able to trace the decision back to a finding.
# Horizontal whitespace only between the colon and the reason: `\s*` would
# swallow the newline and let a BARE marker (no reason on its own line) mute
# the first violation on the following line.
OK_FILE = re.compile(r"ts-escape-allow-file[ \t]*:[ \t]*\S")

#: The ledger id inside a file-level marker (`ledger=L40`, `ledger=P0-A`).
OK_FILE_LEDGER = re.compile(r"\bledger\s*=\s*[A-Za-z0-9][A-Za-z0-9._/-]*")

#: String literals are stripped before rule matching: `"cast as any is fine"`
#: is prose, not an assertion.
STRING_LIT = re.compile(r"""(?:"[^"\n]*"|'[^'\n]*'|`[^`\n]*`)""")

#: `expect.any(String)` / `arr.any()` are member calls, not the any type.
METHOD_ANY = re.compile(r"\.\s*any\b")

#: An ambient module/global block header; the block body is scanned as a unit
#: because a shim can erase an entire dependency in one line.
AMBIENT_HEADER = re.compile(r"\bdeclare\s+(?:module|global)\b")

#: The token whose presence inside an ambient block is the finding.
ANY_WORD = re.compile(r"\bany\b")

def _default_root() -> Path:
    """The repository root when no path is given (falls back to the cwd).

    CI checks out the repository and runs the command from its root, but a
    developer typing it from a subdirectory must still get the whole tree: a
    scan of one subdirectory that happens to be clean is exactly the silent
    pass this gate exists to prevent.
    """

    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output = True, text = True, check = False)
    except OSError:
        return Path.cwd()

    top = probe.stdout.strip()

    if (probe.returncode == 0) and top:
        return Path(top)

    return Path.cwd()

def _relative(path: Path, base: Path) -> str:
    """Render `path` relative to `base` so findings are repo-relative in CI."""

    try:
        return str(path.resolve().relative_to(base.resolve()))
    except (OSError, ValueError):
        return str(path)

def is_declaration_file(path: Path | str) -> bool:
    """True for `*.d.ts`, `*.d.mts` and `*.d.cts` (all three are shims)."""

    return str(path).endswith(DECLARATION_SUFFIXES)

def is_generated(text: str) -> bool:
    """True when the leading lines carry a generator header."""

    return bool(GENERATED_HEADER.search("\n".join(text.splitlines()[:GENERATED_HEADER_WINDOW])))

def profile_of(path: Path | str, text: str = "") -> str:
    """Classify a file as dts / test / generated / src, in that order.

    Declaration wins because the `--fail-on-dts` partition depends on it, then
    tests (a fixture may legitimately carry `as never`), then the generator
    header, and everything else is source.
    """

    name = str(path)

    if is_declaration_file(name):
        return PROFILE_DTS

    normalised = "/" + name.replace("\\", "/").lstrip("/")

    if TEST_NAMES.search(name) or any(segment in normalised for segment in TEST_SEGMENTS):
        return PROFILE_TEST

    if text and is_generated(text):
        return PROFILE_GENERATED

    return PROFILE_SRC

def is_comment_line(line: str) -> bool:
    """True for lines that are entirely comment (prose is never a violation)."""

    text = line.lstrip()

    return text.startswith("//") or text.startswith("*") or text.startswith("/*")

def iter_files(root: Path) -> Iterable[Path]:
    """Yield candidate files under `root`, skipping build output and deps.

    `*.d.ts` is not filtered here: the caller decides whether declaration files
    are enforced (see `--fail-on-dts`), and they are always reported.
    """

    seen: set[str] = set()

    for include in INCLUDE:
        for path in root.rglob(include):
            shown = str(path)

            if (shown in seen) or any(segment in shown for segment in SKIP_SEGMENTS):
                continue

            seen.add(shown)
            yield path

def _strip_strings(line: str) -> str:
    """Replace string literals with empty pairs so prose cannot match a rule."""

    return STRING_LIT.sub('""', line)

def _brace_delta(line: str) -> int:
    """Open minus close braces on one already string-stripped line."""

    return line.count("{") - line.count("}")

def ambient_findings(text: str) -> list[tuple[int, str]]:
    """Return `(line, body)` for each ambient block that carries an `any`.

    The block is delimited by brace counting over string-stripped lines, which
    is enough for the shapes that matter (a wildcard shim is a few lines); a
    block whose depth never returns to zero is reported at its header, because
    an unterminated shim is itself suspicious.
    """

    found: list[tuple[int, str]] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        header = lines[index]

        if (not AMBIENT_HEADER.search(header)) or is_comment_line(header):
            index += 1
            continue

        depth = _brace_delta(_strip_strings(header))
        body = [header]
        cursor = index + 1

        while (cursor < len(lines)) and (depth > 0):
            stripped = _strip_strings(lines[cursor])
            depth += _brace_delta(stripped)
            body.append(stripped)
            cursor += 1

        if ANY_WORD.search(" ".join(body)):
            found.append((index + 1, header.strip()[:200]))

        index = max(cursor, index + 1)

    return found

def _allowed_file(text: str) -> bool:
    """Whether the file-level marker exempts this file.

    The marker only works on a file that a generator header owns: a
    hand-written source file cannot paste itself out of the gate.
    """

    return bool(OK_FILE.search(text)) and bool(OK_FILE_LEDGER.search(text)) and is_generated(text)

def _hit(relpath: str, lineno: int, rule: str, match: str, source: str, profile: str) -> dict:
    """Build one finding record (the JSON shape is part of the CLI contract)."""

    return {"file": relpath, "line": lineno, "rule": rule, "match": match, "source": source.strip()[:200], "profile": profile}

def tier_of(rule_name: str) -> str:
    """Look up a rule's tier by name (used by the ratchet and the report)."""

    for name, _pattern, tier, _profiles in RULES:
        if name == rule_name:
            return tier

    return TIER_COUNT

def _tier_active(tier: str, ruleset: str) -> bool:
    """Whether an enforced tier runs under `ruleset` (`core` or `strict`)."""

    if tier == TIER_CORE:
        return True

    return ruleset == TIER_STRICT

def scan_text(text: str, relpath: str, ruleset: str = "core") -> list[dict]:
    """Return the violations in one file's text.

    `ruleset` selects the enforced tiers: ``core`` runs core + count rules,
    ``strict`` adds the strict tier. Count-tier findings are always returned
    (they feed `--inventory` and `--baseline`); `main()` decides whether they
    can affect the exit code.
    """

    found: list[dict] = []

    if _allowed_file(text):
        return found

    profile = profile_of(relpath, text)

    for lineno, line in enumerate(text.splitlines(), 1):
        if OK_LINE.search(line):
            continue

        probe = METHOD_ANY.sub("._any_", _strip_strings(line))
        comment_rule_hit = _comment_rule_hit(probe, profile)

        if comment_rule_hit is not None:
            found.append(_hit(relpath, lineno, comment_rule_hit["rule"], comment_rule_hit["match"], line, profile))
            continue

        if is_comment_line(line):
            continue

        if _scan_enforced_line(probe, relpath, lineno, line, profile, ruleset, found):
            continue

        _scan_counted_line(probe, relpath, lineno, line, profile, found)

    for lineno, body in ambient_findings(text):
        found.append(_hit(relpath, lineno, "ambient-any", "declare", body, profile))

    return found

def _comment_rule_hit(probe: str, profile: str) -> dict | None:
    """Match the rules that are themselves comments (suppression directives)."""

    for name in COMMENT_RULES:
        for rule_name, pattern, _tier, profiles in RULES:
            if (rule_name != name) or ((profiles is not None) and (profile not in profiles)):
                continue

            hit = pattern.search(probe)

            if hit:
                return {"rule": rule_name, "match": hit.group(0)}

    return None

def _scan_enforced_line(probe: str, relpath: str, lineno: int, line: str, profile: str, ruleset: str, found: list[dict]) -> bool:
    """Append the first enforced-tier finding on this line; report whether one fired."""

    for name, pattern, tier, profiles in RULES:
        if (tier == TIER_COUNT) or (name in COMMENT_RULES) or (not _tier_active(tier, ruleset)):
            continue

        if (profiles is not None) and (profile not in profiles):
            continue

        hit = pattern.search(probe)

        if hit:
            found.append(_hit(relpath, lineno, name, hit.group(0), line, profile))
            return True

    return False

def _scan_counted_line(probe: str, relpath: str, lineno: int, line: str, profile: str, found: list[dict]) -> None:
    """Append every counted-tier finding on this line (they always count)."""

    for name, pattern, tier, profiles in RULES:
        if tier != TIER_COUNT:
            continue

        if (profiles is not None) and (profile not in profiles):
            continue

        hit = pattern.search(probe)

        if hit:
            found.append(_hit(relpath, lineno, name, hit.group(0), line, profile))

def scan_paths(paths: Sequence[Path], base: Path, ruleset: str = "core") -> tuple[list[dict], int]:
    """Scan `paths`, returning (findings, number of candidate files read)."""

    hits: list[dict] = []
    scanned = 0

    for root in paths:
        files = [root] if root.is_file() else list(iter_files(root))

        for path in files:
            try:
                text = path.read_text(encoding = "utf-8", errors = "replace")
            except OSError:
                continue

            scanned += 1

            # No token pre-filter: the first version skipped files without an
            # "any"/"as " token, which silently exempted every rule whose token
            # is something else (emits arrays, `!` assertions, declaration
            # headers). A skipped file reads as clean, so the filter's tiny
            # speed-up is not worth the false negatives.
            for hit in scan_text(text, _relative(path, base), ruleset):
                hit["dts"] = is_declaration_file(path)
                hits.append(hit)

    return hits, scanned

def enforced_hits(hits: Iterable[dict], fail_on_dts: bool = False) -> tuple[list[dict], list[dict]]:
    """Split findings into (enforced, declaration-advisory) by tier and profile."""

    enforced: list[dict] = []
    advisory: list[dict] = []

    for hit in hits:
        if tier_of(hit["rule"]) == TIER_COUNT:
            continue

        if hit.get("dts") and not fail_on_dts:
            advisory.append(hit)
            continue

        enforced.append(hit)

    return enforced, advisory

def count_by_rule(hits: Iterable[dict]) -> dict[str, int]:
    """Count findings per rule (the ratchet's unit of account)."""

    counts: dict[str, int] = {}

    for hit in hits:
        counts[hit["rule"]] = counts.get(hit["rule"], 0) + 1

    return counts

def counted_by_rule(hits: Iterable[dict]) -> dict[str, int]:
    """Count only the counted tier (what the ratchet compares)."""

    return count_by_rule(hit for hit in hits if tier_of(hit["rule"]) == TIER_COUNT)

def count_by_file(hits: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Count counted-tier findings per file, per rule (the per-file ratchet)."""

    counts: dict[str, dict[str, int]] = {}

    for hit in hits:
        if tier_of(hit["rule"]) != TIER_COUNT:
            continue

        per_file = counts.setdefault(hit["file"], {})
        per_file[hit["rule"]] = per_file.get(hit["rule"], 0) + 1

    return counts

def build_baseline(hits: Iterable[dict]) -> dict:
    """Render the baseline document for the current tree (sorted, diffable)."""

    hits = list(hits)
    files = {name: dict(sorted(per_file.items())) for name, per_file in sorted(count_by_file(hits).items())}

    return {"version": 1, "rules": dict(sorted(counted_by_rule(hits).items())), "files": files}

def load_baseline(path: Path) -> dict | None:
    """Read a ratchet baseline; a missing or malformed file is a usage error."""

    try:
        document = json.loads(path.read_text(encoding = "utf-8"))
    except OSError as exc:
        print(f"type-escapes: cannot read baseline {path}: {exc}", file = sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"type-escapes: baseline {path} is not JSON: {exc}", file = sys.stderr)
        return None

    if not isinstance(document, dict) or not isinstance(document.get("files", {}), dict):
        print(f"type-escapes: baseline {path} must be an object with a 'files' object", file = sys.stderr)
        return None

    return document

def ratchet_violations(baseline: dict, hits: Iterable[dict], allow_new_files: bool = False) -> list[str]:
    """Return the messages for counted-tier growth against `baseline`.

    Two independent ceilings: the per-rule totals (deleting one finding and
    adding one under a different rule cannot net out to zero) and the
    per-file/rule counts (a file cannot grow while another shrinks). Files
    absent from the baseline are new debt: refused unless the caller explicitly
    opted into `allow_new_files`.
    """

    hits = list(hits)
    current_rules = counted_by_rule(hits)
    current_files = count_by_file(hits)
    baseline_rules = baseline.get("rules") or {}
    baseline_files = baseline.get("files") or {}
    messages: list[str] = []

    # The per-rule ceiling is the strict mode. `--baseline-allow-new` exists for
    # repositories that are still adding components: new files may bring their
    # own assertions, but an EXISTING file stays frozen (checked below).
    if not allow_new_files:
        for rule_name, count in sorted(current_rules.items()):
            allowed = baseline_rules.get(rule_name)

            if allowed is None:
                messages.append(f"{rule_name}: baseline has no entry; the tree now carries {count} (record it with --update-baseline)")
            elif count > allowed:
                messages.append(f"{rule_name}: {count} > baseline {allowed} (the ratchet is monotone: lower the count, do not raise the budget)")

    for name, per_rule in sorted(current_files.items()):
        allowed_file = baseline_files.get(name)

        if allowed_file is None:
            if allow_new_files:
                continue

            detailed = ", ".join(f"{rule_name}={count}" for rule_name, count in sorted(per_rule.items()))
            messages.append(f"{name}: new file carries counted assertions ({detailed}); accept them with --update-baseline only when the review accepts them")
            continue

        for rule_name, count in sorted(per_rule.items()):
            allowed = allowed_file.get(rule_name, 0)

            if count > allowed:
                messages.append(f"{name}: {rule_name} {count} > baseline {allowed}")

    return messages

SELFTEST_POSITIVE = """\
const a = b as unknown as Widget;
const c = d as any;
function e(x: any): void {}
const f: Promise<any> = g();
const h: any[] = [];
// @ts-ignore
const i = j;
const k = <any>l;
const m: Record<string, any> = {};
const n = (o as unknown) as Widget;
const rf: () => any = q;
"""

SELFTEST_NEGATIVE = """\
// any prose here: any touch reveals it, as any CSS length would
/* as unknown as also appears in this block comment */
const s = "cast as any is fine inside a string";
const t = "any";
expect(el).toEqual(expect.any(String));
const u = v as unknown as Widget; // ts-escape-ok: marker lives on the same line
const w: unknown = x;
const y: Record<string, unknown> = {};
"""

SELFTEST_ALLOW_FILE = """\
// Auto-generated by build_npm_glue_packages.py
// ts-escape-allow-file: generated WASM-browser glue, retires with L40 ledger=L40
const a = b as unknown as Widget;
const c = d as any;
"""

SELFTEST_HANDWRITTEN_ALLOW = """\
// ts-escape-allow-file: hand-written file pasting the marker ledger=L40
const a = b as any;
"""

SELFTEST_ALLOW_NO_LEDGER = """\
// Auto-generated by build_npm_glue_packages.py
// ts-escape-allow-file: generated glue, reason only
const a = b as any;
"""

SELFTEST_SCOPING = """\
const a = b as any;
const c = d as any; // ts-escape-ok: only this line is excused
const e = f as any;
"""

SELFTEST_DTS = """\
declare const payload: any;
"""

SELFTEST_LAUNDER = """\
type Any = any;
type Widened = Array<any>;
declare module "*.vue" {
  const component: DefineComponent<Record<string, never>, Record<string, never>, Record<string, never>>;
  export default component;
}
declare module "*.untyped" {
  const value: any;
  export default value;
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const value: unknown = 1;
"""

SELFTEST_STRICT = """\
export const parsed = JSON.parse(raw);
export const fired: Function = handler;
export const name = (event.target as HTMLElement).dataset.name;
export const doomed = value as never;
"""

SELFTEST_TEST_PROFILE = """\
export const invalid = isTimeAware(null as never);
"""

SELFTEST_EMITS = """\
export const Widget = defineComponent({ emits: ["select"], setup() { return () => null; } });
"""

SELFTEST_RATCHET_TREE = """\
export const el = document.querySelector("#app")!;
export const card = body as HTMLElement;
"""

def selftest() -> int:
    """Verify the scanner against its own positive and negative controls.

    The controls are the ones the workspace scanner carried, plus the axes the
    redesign added: the laundering shapes, the strict tier, the test profile,
    the generated/ledger protocol, the declaration-file classification and the
    ratchet in both directions. A gate whose allowlist silently widened, or
    whose strict tier never fired, would pass every clean tree and catch
    nothing.
    """

    tmp = Path(tempfile.mkdtemp(prefix = "type-escapes-selftest-"))

    try:
        (tmp / "positive.ts").write_text(SELFTEST_POSITIVE, encoding = "utf-8")
        (tmp / "negative.ts").write_text(SELFTEST_NEGATIVE, encoding = "utf-8")
        (tmp / "allowfile.ts").write_text(SELFTEST_ALLOW_FILE, encoding = "utf-8")
        (tmp / "handwritten.ts").write_text(SELFTEST_HANDWRITTEN_ALLOW, encoding = "utf-8")
        (tmp / "noledger.ts").write_text(SELFTEST_ALLOW_NO_LEDGER, encoding = "utf-8")
        (tmp / "scoping.ts").write_text(SELFTEST_SCOPING, encoding = "utf-8")
        (tmp / "shim.d.ts").write_text(SELFTEST_DTS, encoding = "utf-8")
        (tmp / "launder.ts").write_text(SELFTEST_LAUNDER, encoding = "utf-8")
        (tmp / "strict.ts").write_text(SELFTEST_STRICT, encoding = "utf-8")
        (tmp / "tests").mkdir()
        (tmp / "tests" / "guard.test.ts").write_text(SELFTEST_TEST_PROFILE, encoding = "utf-8")
        (tmp / "widget.tsx").write_text(SELFTEST_EMITS, encoding = "utf-8")
        (tmp / "ratchet.ts").write_text(SELFTEST_RATCHET_TREE, encoding = "utf-8")
        positive = scan_text(SELFTEST_POSITIVE, "positive.ts")
        negative = scan_text(SELFTEST_NEGATIVE, "negative.ts")
        allowfile = scan_text(SELFTEST_ALLOW_FILE, "allowfile.ts")
        handwritten = scan_text(SELFTEST_HANDWRITTEN_ALLOW, "handwritten.ts")
        noledger = scan_text(SELFTEST_ALLOW_NO_LEDGER, "noledger.ts")
        scoping = scan_text(SELFTEST_SCOPING, "scoping.ts")
        launder = scan_text(SELFTEST_LAUNDER, "launder.ts")
        core_strict = scan_text(SELFTEST_STRICT, "strict.ts", "core")
        strict = scan_text(SELFTEST_STRICT, "strict.ts", "strict")
        test_profile = scan_text(SELFTEST_TEST_PROFILE, "tests/guard.test.ts")
        emits = scan_text(SELFTEST_EMITS, "widget.tsx")
        wanted_core = {"double-cast", "paren-dbl-cast", "cast-any", "any-annot", "any-generic", "any-return", "suppress"}
        seen_core = {hit["rule"] for hit in positive if tier_of(hit["rule"]) != TIER_COUNT}
        failures: list[str] = []

        if seen_core != wanted_core:
            failures.append(f"positive control missed rules: want={sorted(wanted_core)} got={sorted(seen_core)}")

        if len([hit for hit in positive if tier_of(hit["rule"]) != TIER_COUNT]) != 10:
            failures.append(f"positive control expected 10 enforced hits, got {len([hit for hit in positive if tier_of(hit['rule']) != TIER_COUNT])}")

        if [hit["rule"] for hit in negative if tier_of(hit["rule"]) != TIER_COUNT]:
            failures.append(f"negative control produced {len(negative)} hit(s): {negative}")

        if allowfile:
            failures.append(f"generated+ledger allowlist ignored: {len(allowfile)} hit(s)")

        if not handwritten:
            failures.append("hand-written file pasted the file marker and was excused (the marker must require a generator header)")

        if not noledger:
            failures.append("file-level marker without ledger= was accepted")

        if [hit["line"] for hit in scoping if tier_of(hit["rule"]) != TIER_COUNT] != [1, 3]:
            failures.append(f"line-scoped allowlist is not line-scoped: want lines [1, 3], got {[hit['line'] for hit in scoping]}")

        laundering = {hit["rule"] for hit in launder}

        if not {"type-alias-any", "ambient-any", "eslint-type-disable"}.issubset(laundering):
            failures.append(f"laundering shapes not all reported: got={sorted(laundering)}")

        if {hit["rule"] for hit in core_strict} & {"any-source", "function-type", "event-target-cast"}:
            failures.append("strict rules fired under --ruleset core")

        if not {"any-source", "function-type", "event-target-cast", "cast-never"}.issubset({hit["rule"] for hit in strict}):
            failures.append(f"strict ruleset missed shapes: got={sorted({hit['rule'] for hit in strict})}")

        if "cast-never" in {hit["rule"] for hit in test_profile}:
            failures.append("cast-never fired inside a test profile (deliberate bad-input injection is legitimate there)")

        if "emits-array-form" not in {hit["rule"] for hit in emits}:
            failures.append("emits array form was not reported on a component source")

        live, scanned = scan_paths([tmp], tmp)

        if scanned != 12:
            failures.append(f"file enumeration read {scanned} of 12 control files")

        if not [hit for hit in live if hit["file"].endswith("positive.ts")]:
            failures.append("known-positive file produced no hit (the scan would read as clean)")

        if not [hit for hit in live if hit.get("dts")]:
            failures.append("declaration-file hit was not marked dts (the advisory section would be empty)")

        baseline = build_baseline(live)
        extra_counted = {"file": "ratchet.ts", "line": 1, "rule": "as-assert", "match": "", "source": "", "profile": PROFILE_SRC}
        grown = ratchet_violations(baseline, live)
        stricter = ratchet_violations(baseline, live + [extra_counted])
        newfile = ratchet_violations(baseline, live + [dict(extra_counted, file = "brand-new.ts")])
        allowed_new = ratchet_violations(baseline, live + [dict(extra_counted, file = "brand-new.ts")], allow_new_files = True)

        if grown:
            failures.append(f"ratchet rejected its own baseline: {grown[:2]}")

        if not stricter:
            failures.append("ratchet accepted growth in an existing file")

        if not newfile:
            failures.append("ratchet accepted a new file's counted debt")

        if allowed_new:
            failures.append(f"ratchet refused a new file even with --baseline-allow-new: {allowed_new[:1]}")

        enforced, advisory = enforced_hits(live)

        if not advisory:
            failures.append("declaration-file findings were not split into the advisory section")

        if [hit for hit in enforced if hit.get("dts")]:
            failures.append("a declaration-file finding leaked into the enforced set without --fail-on-dts")

        shutil.rmtree(tmp)

        if failures:
            for message in failures:
                print(f"SELFTEST FAIL: {message}", file = sys.stderr)

            return 1

        print(f"selftest OK: {len([hit for hit in positive if tier_of(hit['rule']) != TIER_COUNT])}/10 enforced hits, 0 false positives, profiles honored, {len(strict)} strict hits, laundering shapes reported, dts advisory, ratchet monotone both ways")
        return 0
    except Exception as exc:  # noqa: BLE001 - the selftest must never mask its own crash
        shutil.rmtree(tmp, ignore_errors = True)
        print(f"SELFTEST ERROR: {exc}", file = sys.stderr)
        return 2

def inventory(hits: Sequence[dict], scanned: int) -> dict:
    """Summarise findings by rule, tier and profile (the measurement stick)."""

    by_rule: dict[str, dict] = {}

    for hit in hits:
        slot = by_rule.setdefault(hit["rule"], {"tier": tier_of(hit["rule"]), "total": 0, "profiles": {}})
        slot["total"] += 1
        slot["profiles"][hit["profile"]] = slot["profiles"].get(hit["profile"], 0) + 1

    rules = {name: {"tier": slot["tier"], "total": slot["total"], "profiles": dict(sorted(slot["profiles"].items()))} for name, slot in sorted(by_rule.items())}

    return {"rules": rules, "files": len({hit["file"] for hit in hits}), "scanned": scanned}

def _print_inventory(document: dict) -> None:
    """Print the inventory as a table a human can read in a terminal."""

    print(f"{'rule':22s} {'tier':7s} {'total':>6s}  profiles")

    for name, slot in document["rules"].items():
        profiles = ", ".join(f"{key}={value}" for key, value in slot["profiles"].items())
        print(f"{name:22s} {slot['tier']:7s} {slot['total']:6d}  {profiles}")

    print(f"\n{document['files']} file(s) with findings; {document['scanned']} file(s) scanned")

def _print_report(hits: list[dict], dts_hits: list[dict], scanned: int, ratchet: Sequence[str], ruleset: str, counted: dict[str, int]) -> None:
    """Human-readable report: findings, by-rule summary, advisory d.ts section."""

    per_rule: dict[str, int] = {}
    per_file: dict[str, int] = {}

    for hit in hits:
        per_rule[hit["rule"]] = per_rule.get(hit["rule"], 0) + 1
        per_file[hit["file"]] = per_file.get(hit["file"], 0) + 1

    for hit in hits:
        print(f"{hit['file']}:{hit['line']}  [{hit['rule']}/{hit['profile']}]  {hit['source']}")

    if hits:
        print()
        print(f"by rule ({ruleset}): " + ", ".join(f"{name}={count}" for name, count in sorted(per_rule.items())))
        print("top files:")

        for name, count in sorted(per_file.items(), key = lambda item: -item[1])[:10]:
            print(f"  {count:4d}  {name}")

    if dts_hits:
        print("\n-- declaration files (advisory; --fail-on-dts to enforce) --")

        for hit in dts_hits:
            print(f"{hit['file']}:{hit['line']}  [{hit['rule']}]  {hit['source']}")

        dts_by_rule: dict[str, int] = {}

        for hit in dts_hits:
            dts_by_rule[hit["rule"]] = dts_by_rule.get(hit["rule"], 0) + 1

        print(f"   {len(dts_hits)} hit(s) in declaration files: " + ", ".join(f"{name}={count}" for name, count in sorted(dts_by_rule.items())))

    if counted:
        print("\n-- counted assertions (ratcheted, not enforced: see --baseline) --")
        print("   " + ", ".join(f"{name}={count}" for name, count in sorted(counted.items())))

    if ratchet:
        print("\n-- ratchet (counted assertions may only shrink) --")

        for message in ratchet:
            print(f"  {message}")

    print(f"\n{len(hits)} escape hatch(es) across {len(per_file)} file(s); {scanned} file(s) scanned")

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 0 clean, 1 violations, 2 usage error."""

    parser = argparse.ArgumentParser(prog = "type-escapes", description = "Fail a tree that reintroduces TypeScript type escape hatches.")
    parser.add_argument("paths", nargs = "*", default = [], metavar = "PATH", help = "Files or directories to scan; defaults to the repository root.")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable output.")
    parser.add_argument("--ruleset", choices = ("core", "strict"), default = "core", help = "Rule tier to enforce: core (always-on) or strict (adds the shapes whose fix costs a migration).")
    parser.add_argument("--inventory", action = "store_true", help = "Print per-rule/per-profile counts instead of a findings list (never affects the exit code).")
    parser.add_argument("--baseline", default = None, metavar = "FILE", help = "Ratchet ledger: counted-tier assertions may not exceed the counts recorded there.")
    parser.add_argument("--update-baseline", default = None, metavar = "FILE", help = "Rewrite the ratchet ledger from the current tree (review the diff: it is debt).")
    parser.add_argument("--baseline-allow-new", action = "store_true", help = "Let files absent from the baseline carry counted assertions (migration aid; off by default).")
    parser.add_argument("--fail-on-dts", action = "store_true", help = "Count hits in declaration files (*.d.ts, *.d.mts, *.d.cts) toward the exit code; by default they are an advisory section.")
    parser.add_argument("--allow-empty", action = "store_true", help = "Accept a target that contains no scannable file (off by default: a mistyped path must not read as clean).")
    parser.add_argument("--selftest", action = "store_true", help = "Verify the scanner against its own controls.")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    targets = [Path(raw) for raw in args.paths] or [_default_root()]
    missing = [str(target) for target in targets if not target.exists()]

    if missing:
        print(f"type-escapes: no such path: {', '.join(missing)}", file = sys.stderr)
        return 2

    hits, scanned = scan_paths(targets, Path.cwd(), args.ruleset)

    if (scanned == 0) and not args.allow_empty:
        listed = ", ".join(str(target) for target in targets)
        print(f"type-escapes: no scannable file ({', '.join(INCLUDE)}) under: {listed} — check the path (--allow-empty to accept)", file = sys.stderr)
        return 2

    if args.inventory:
        document = inventory(hits, scanned)

        if args.json:
            envelope.emit(document)
        else:
            _print_inventory(document)

        return 0

    enforced, advisory = enforced_hits(hits, args.fail_on_dts)
    counted = counted_by_rule(hits)
    ratchet: list[str] = []
    status = 0

    # Baseline comparison happens BEFORE an update writes: `--update-baseline`
    # must not be able to launder growth it accepted a moment ago.
    if args.baseline:
        baseline = load_baseline(Path(args.baseline))

        if baseline is None:
            return 2

        ratchet = ratchet_violations(baseline, hits, args.baseline_allow_new)

    if args.update_baseline:
        Path(args.update_baseline).write_text(json.dumps(build_baseline(hits), indent = 1, ensure_ascii = True) + "\n", encoding = "utf-8")
        print(f"type-escapes: baseline written to {args.update_baseline}")

    if enforced or ratchet:
        status = 1

    if args.json:
        envelope.emit({"count": len(enforced), "hits": enforced, "dts_count": len(advisory), "dts_hits": advisory, "scanned": scanned, "ruleset": args.ruleset, "counted": counted, "ratchet": ratchet, "ratchet_ok": not ratchet})
    else:
        _print_report(enforced, advisory, scanned, ratchet, args.ruleset, counted)

    return status

if __name__ == "__main__":
    raise SystemExit(main())
