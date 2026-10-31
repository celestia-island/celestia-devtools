#!/usr/bin/env python3
"""Fail a tree that reintroduces TypeScript type escape hatches.

*Why this exists:* twelve repositories were driven to zero occurrences of the
assertions that silently delete type information. Nothing enforced that
property afterwards, so the first `as unknown as T` merged after the cleanup
would be invisible — the same fail-open shape the workspace keeps re-learning.
`_tools/ts_escape_scan.py` proved the audit could be mechanical; that path is
not in any git tree, so the gate belongs in the shared CLI the runner farm
already installs (the same way `commit-msg-lint` and `rules-lint` do).

*Rule set* (identical to the workspace scanner, deliberately not weakened)::

    double-cast      `as unknown as T`      (bypasses every assignability check)
    paren-dbl-cast   `(x as unknown) as T`  (identical trick, parenthesised)
    cast-any         `as any`, `<any>`
    any-annot        `: any`, `any[]`
    any-generic      `<any>`, `Promise<any>`, `Record<K, any>`, `Array<any>`
    any-return       `=> any`
    suppress         `@ts-ignore`, `@ts-nocheck`, `@ts-expect-error`

Plain `unknown` annotations are NOT flagged. `unknown` is the safe counterpart
of `any` (type-guard parameters, catch clauses, external input) and banning it
pushes code back toward `any`; this gate only removes the ways of *lying* to
the compiler.

*Suppression* is explicit and reviewable, never silent::

    same line   // ts-escape-ok: <reason>
    whole file  // ts-escape-allow-file: <reason>

*Usage*::

    celestia-devtools type-escapes                  # scan the repository root
    celestia-devtools type-escapes packages src     # explicit paths
    celestia-devtools type-escapes --json
    celestia-devtools type-escapes --fail-on-dts    # enforce *.d.ts too
    celestia-devtools type-escapes --selftest

`*.d.ts` files are reported in a separate advisory section by default: a
hand-written shim sometimes has to model an untyped upstream, but a facade
typed `any` hides the checking of every caller, so the section is never
silently dropped — `--fail-on-dts` moves it into the exit code.

Exit: 0 clean, 1 violations found, 2 usage error. A missing path, and a target
that holds no scannable file at all (`--allow-empty` to accept the latter
explicitly), are usage errors: a mistyped path must never read as clean.
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

#: File suffixes scanned (TypeScript family, incl. Vue SFC script blocks).
INCLUDE = ("*.ts", "*.tsx", "*.vue", "*.mts", "*.cts")

#: Path segments never scanned (build output, dependencies, VCS internals).
SKIP_SEGMENTS = ("/node_modules/", "/dist/", "/target/", "/.git/", "/coverage/", "/build/")

#: name -> regex. All of these are type-position assertions/annotations; the
#: first match on a line wins, so `const h: any[]` is one finding, not two.
RULES: Sequence[tuple[str, re.Pattern[str]]] = [("double-cast", re.compile(r"\bas\s+unknown\s+as\b")), ("paren-dbl-cast", re.compile(r"\bas\s+unknown\s*\)\s*as\b")), ("cast-any", re.compile(r"\bas\s+any\b|\bas\s+any\s*\[\s*\]")), ("any-annot", re.compile(r":\s*any\b")), ("any-generic", re.compile(r"(?:[<\[(,]\s*any\s*[>\]),;]|Promise\s*<\s*any\s*>|Record\s*<[^<>]*,?\s*any\s*>|Array\s*<\s*any\s*>|\bany\s*\[\s*\])")), ("any-return", re.compile(r"=>\s*any\b"))]

#: Same-line allowlist: the marker must carry a reason (anything non-empty).
OK_LINE = re.compile(r"ts-escape-ok\s*:\s*\S")

#: Whole-file allowlist, for vendored/generated glue that is regenerated
#: upstream. It also has to name a reason.
# Horizontal whitespace only between the colon and the reason: `\s*` would
# swallow the newline and let a BARE marker (no reason on its own line) mute
# the first violation on the following line.
OK_FILE = re.compile(r"ts-escape-allow-file[ \t]*:[ \t]*\S")

#: TypeScript suppression directives, in all three spellings.
SUPPRESS = re.compile(r"@ts-(?:nocheck|ignore|expect-error)\b")

#: String literals are stripped before rule matching: `"cast as any is fine"`
#: is prose, not an assertion.
STRING_LIT = re.compile(r"""(?:"[^"\n]*"|'[^'\n]*'|`[^`\n]*`)""")

#: `expect.any(String)` / `arr.any()` are member calls, not the any type.
METHOD_ANY = re.compile(r"\.\s*any\b")

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

def scan_text(text: str, relpath: str) -> list[dict]:
    """Return the violations in one file's text."""

    found: list[dict] = []

    if OK_FILE.search(text):
        return found

    for lineno, line in enumerate(text.splitlines(), 1):
        if OK_LINE.search(line):
            continue

        # Suppression directives ARE comments, so they must be matched before
        # the comment-line filter below.
        directive = SUPPRESS.search(line)

        if directive:
            found.append({"file": relpath, "line": lineno, "rule": "suppress", "match": directive.group(0), "source": line.strip()[:200]})
            continue

        if is_comment_line(line):
            continue

        probe = STRING_LIT.sub('""', line)
        probe = METHOD_ANY.sub("._any_", probe)

        for name, pattern in RULES:
            hit = pattern.search(probe)

            if hit:
                found.append({"file": relpath, "line": lineno, "rule": name, "match": hit.group(0), "source": line.strip()[:200]})
                break

    return found

def scan_paths(paths: Sequence[Path], base: Path) -> tuple[list[dict], int]:
    """Scan `paths`, returning (violations, number of candidate files read)."""

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

            # Cheap pre-filter: a file without any of these three tokens cannot
            # match a rule, so the regex pass is skipped entirely.
            if ("any" not in text) and ("unknown" not in text) and ("@ts-" not in text):
                continue

            for hit in scan_text(text, _relative(path, base)):
                hit["dts"] = path.name.endswith(".d.ts")
                hits.append(hit)

    return hits, scanned

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
// ts-escape-allow-file: vendored generated glue, regenerate upstream
const a = b as unknown as Widget;
const c = d as any;
"""

SELFTEST_SCOPING = """\
const a = b as any;
const c = d as any; // ts-escape-ok: only this line is excused
const e = f as any;
"""

SELFTEST_DTS = """\
declare const payload: any;
"""

def selftest() -> int:
    """Verify the scanner against its own positive and negative controls.

    The controls are the ones the workspace scanner carried, plus a line-scope
    case and a `*.d.ts` case: a gate whose allowlist silently widened to the
    whole file would pass every clean tree and catch nothing.
    """

    tmp = Path(tempfile.mkdtemp(prefix = "type-escapes-selftest-"))

    try:
        (tmp / "positive.ts").write_text(SELFTEST_POSITIVE, encoding = "utf-8")
        (tmp / "negative.ts").write_text(SELFTEST_NEGATIVE, encoding = "utf-8")
        (tmp / "allowfile.ts").write_text(SELFTEST_ALLOW_FILE, encoding = "utf-8")
        (tmp / "scoping.ts").write_text(SELFTEST_SCOPING, encoding = "utf-8")
        (tmp / "shim.d.ts").write_text(SELFTEST_DTS, encoding = "utf-8")
        positive = scan_text(SELFTEST_POSITIVE, "positive.ts")
        negative = scan_text(SELFTEST_NEGATIVE, "negative.ts")
        allowfile = scan_text(SELFTEST_ALLOW_FILE, "allowfile.ts")
        scoping = scan_text(SELFTEST_SCOPING, "scoping.ts")
        wanted = {"double-cast", "paren-dbl-cast", "cast-any", "any-annot", "any-generic", "any-return", "suppress"}
        seen = {hit["rule"] for hit in positive}
        failures: list[str] = []

        if seen != wanted:
            failures.append(f"positive control missed rules: want={sorted(wanted)} got={sorted(seen)}")

        if len(positive) != 10:
            failures.append(f"positive control expected 10 hits, got {len(positive)}")

        if negative:
            failures.append(f"negative control produced {len(negative)} hit(s): {negative}")

        if allowfile:
            failures.append(f"file-level allowlist ignored: {len(allowfile)} hit(s)")

        if [hit["line"] for hit in scoping] != [1, 3]:
            failures.append(f"line-scoped allowlist is not line-scoped: want lines [1, 3], got {[hit['line'] for hit in scoping]}")

        live, scanned = scan_paths([tmp], tmp)

        if scanned != 5:
            failures.append(f"file enumeration read {scanned} of 5 control files")

        if not [hit for hit in live if hit["file"].endswith("positive.ts")]:
            failures.append("known-positive file produced no hit (the scan would read as clean)")

        if not [hit for hit in live if hit.get("dts")]:
            failures.append("declaration-file hit was not marked dts (the advisory section would be empty)")

        shutil.rmtree(tmp)

        if failures:
            for message in failures:
                print(f"SELFTEST FAIL: {message}", file = sys.stderr)

            return 1

        print(f"selftest OK: {len(positive)}/10 positive hits, 0 false positives, line + file allowlists honored, known-positive file live, dts classified")
        return 0
    except Exception as exc:  # noqa: BLE001 - the selftest must never mask its own crash
        shutil.rmtree(tmp, ignore_errors = True)
        print(f"SELFTEST ERROR: {exc}", file = sys.stderr)
        return 2

def _print_report(hits: list[dict], dts_hits: list[dict], scanned: int) -> None:
    """Human-readable report: findings, by-rule summary, advisory d.ts section."""

    per_rule: dict[str, int] = {}
    per_file: dict[str, int] = {}

    for hit in hits:
        per_rule[hit["rule"]] = per_rule.get(hit["rule"], 0) + 1
        per_file[hit["file"]] = per_file.get(hit["file"], 0) + 1

    for hit in hits:
        print(f"{hit['file']}:{hit['line']}  [{hit['rule']}]  {hit['source']}")

    if hits:
        print()
        print("by rule: " + ", ".join(f"{name}={count}" for name, count in sorted(per_rule.items())))
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

        print(f"   {len(dts_hits)} hit(s) in *.d.ts: " + ", ".join(f"{name}={count}" for name, count in sorted(dts_by_rule.items())))

    print(f"\n{len(hits)} escape hatch(es) across {len(per_file)} file(s); {scanned} file(s) scanned")

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 0 clean, 1 violations, 2 usage error."""

    parser = argparse.ArgumentParser(prog = "type-escapes", description = "Fail a tree that reintroduces TypeScript type escape hatches.")
    parser.add_argument("paths", nargs = "*", default = [], metavar = "PATH", help = "Files or directories to scan; defaults to the repository root.")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable output.")
    parser.add_argument("--fail-on-dts", action = "store_true", help = "Count hits in declaration files (*.d.ts) toward the exit code; by default they are an advisory section.")
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

    hits, scanned = scan_paths(targets, Path.cwd())

    if (scanned == 0) and not args.allow_empty:
        listed = ", ".join(str(target) for target in targets)
        print(f"type-escapes: no scannable file ({', '.join(INCLUDE)}) under: {listed} — check the path (--allow-empty to accept)", file = sys.stderr)
        return 2

    enforced = [hit for hit in hits if args.fail_on_dts or not hit.get("dts")]
    dts_hits = [hit for hit in hits if hit.get("dts")]

    if args.json:
        print(json.dumps({"count": len(enforced), "hits": enforced, "dts_count": len(dts_hits), "dts_hits": dts_hits, "scanned": scanned}, indent = 1, ensure_ascii = False))
    else:
        _print_report(enforced, dts_hits, scanned)

    return 1 if enforced else 0

if __name__ == "__main__":
    raise SystemExit(main())
