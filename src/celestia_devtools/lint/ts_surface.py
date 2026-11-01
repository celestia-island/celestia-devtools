#!/usr/bin/env python3
"""Audit a package's *published* type surface, not just its source.

*Why this exists:* the 2026-11-01 review of the family found that the three
repositories which produce the types every other repository consumes are the
three whose TypeScript was never compiled in CI: hikari's Vue half (its type
entry point is `src/index.ts`, so consumers compile hikari's source under their
own config), tairitsu's WASM glue, and entelecheia's generated bindings. A
`any`-in-source grep cannot catch a surface that only materialises when the
compiler expands generics, and a consumer-side green build is exactly what a
too-loose upstream surface produces.

The cheap mechanical probe is: ask the compiler to emit the package's
declarations, then run the escape-hatch rules over the emitted `.d.ts`. That
works for plain TypeScript producers today (tairitsu's transport and glue
packages, entelecheia's bindings). For a Vue component library it does NOT work
as a hard gate: TypeScript synthesises `any` inside Vue's own `DefineComponent`
generics, so hikari's emitted surface carries 435 hits of which almost none are
authored — they are the library's own typing machinery. Honest answer for those
packages is a type-level assertion suite (`*.test-d.ts`), which lands with the
contract wave rather than here; this command says so in its output instead of
pretending the noise is debt.

Usage::

    celestia-devtools ts-surface emit-scan            # emit declarations, scan them
    celestia-devtools ts-surface emit-scan --json
    celestia-devtools ts-surface emit-scan --keep-noise   # show the Vue noise too
    celestia-devtools ts-surface --selftest

Exit: 0 clean, 1 findings (or a failed emit), 2 usage error.
"""

from __future__ import annotations
import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence
from celestia_devtools.core import envelope
from celestia_devtools.lint import type_escapes

#: Declarations the compiler synthesised from Vue's own typing machinery. They
#: are not authored: reporting them as debt would make the gate unpassable for
#: every component library, which is the surest way to get a gate switched off.
NOISE = re.compile(r"ComponentProvideOptions|ComponentOptionsBase|ComponentInternalInstance|SetupContext<|EmitFn<|ShallowUnwrapRef|__type:|__props")

#: What a declaration-emit run adds to the repository's own tsconfig.
EMIT_FLAGS = ("--emitDeclarationOnly", "--declaration", "--noEmit", "false")

def _run(command: str, cwd: Path, extra: Sequence[str]) -> tuple[int, str]:
    """Run the checker with extra flags; never raises."""

    argv = shlex.split(command) + list(extra)
    completed = subprocess.run(argv, cwd = str(cwd), capture_output = True, text = True, check = False)

    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

def emit_declarations(checker: str, cwd: Path, config: Path, out_dir: Path, root_dir: Path) -> tuple[int, str]:
    """Ask the checker to write the package's declarations into `out_dir`.

    `--declarationDir` is passed as well as `--outDir`: a tsconfig that sets
    `declarationDir` (the normal shape for a library that publishes types)
    overrides the CLI `--outDir`, so the first version emitted into the
    checkout and then read an empty temp directory — both a dirty worktree and
    a false "produced no files" failure on exactly the packages this targets.
    """

    flags = list(EMIT_FLAGS) + ["-p", str(config.resolve()), "--outDir", str(out_dir), "--declarationDir", str(out_dir), "--rootDir", str(root_dir)]

    return _run(checker, cwd, flags)

DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")

def _full_source(surface: Path, hit: dict) -> str:
    """The complete source line behind one finding.

    `type_escapes` truncates each finding's `source` to 200 characters; the Vue
    noise tokens sit far to the right of a realistic emitted `DefineComponent`
    line, so the noise filter has to look at the untruncated line.
    """

    try:
        lines = (surface / hit["file"]).read_text(encoding = "utf-8", errors = "replace").splitlines()
    except OSError:
        return hit["source"]

    index = hit["line"] - 1

    return lines[index] if 0 <= index < len(lines) else hit["source"]

def scan_surface(surface: Path, keep_noise: bool = False) -> tuple[list[dict], int]:
    """Scan emitted declarations with the escape rules; return (hits, suppressed noise)."""

    hits, _scanned = type_escapes.scan_paths([surface], surface, "core")
    kept: list[dict] = []
    noise = 0

    for hit in hits:
        if type_escapes.tier_of(hit["rule"]) == type_escapes.TIER_COUNT:
            continue

        if (not keep_noise) and NOISE.search(_full_source(surface, hit)):
            noise += 1
            continue

        kept.append(hit)

    return kept, noise

def default_checker(root: Path) -> str | None:
    """Prefer the repository's vue-tsc when installed, else its tsc."""

    bin_root = root / "node_modules" / ".bin"

    for name in ("vue-tsc", "tsc"):
        candidate = bin_root / name

        if candidate.exists():
            return str(candidate)

    return None

def selftest() -> int:
    """Verify the surface scan against known-positive and known-negative controls."""

    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix = "ts-surface-selftest-"))

    try:
        dirt = tmp / "dirty"
        clean = tmp / "clean"

        for directory in (dirt, clean):
            directory.mkdir()

        (dirt / "index.d.ts").write_text('export declare const payload: any;\nexport declare const handler: ((...args: any[]) => any) | undefined;\n', encoding = "utf-8")
        (clean / "index.d.ts").write_text('export declare const payload: unknown;\nexport declare const handler: ((value: string) => void) | undefined;\n', encoding = "utf-8")
        (dirt / "noise.d.ts").write_text('export declare const component: import("vue").DefineComponent<{}, {}, {}, {}, string, import("vue").ComponentProvideOptions, true, {}, any>;\n', encoding = "utf-8")
        (dirt / "long-noise.d.ts").write_text('export declare const wide: import("vue").DefineComponent<{' + ", ".join(f'"prop{index}": string' for index in range(40)) + '}, {}, {}, {}, string, import("vue").ComponentProvideOptions, true, {}, any>;\n', encoding = "utf-8")
        dirty_hits, dirty_noise = scan_surface(dirt)
        loud_hits, _loud_noise = scan_surface(dirt, keep_noise = True)
        clean_hits, _clean_noise = scan_surface(clean)

        if not dirty_hits:
            failures.append("known-positive surface produced no hit (the scan would read as clean)")

        if clean_hits:
            failures.append(f"clean surface produced {len(clean_hits)} false positive(s): {clean_hits}")

        if dirty_noise < 2:
            failures.append("the Vue noise filter suppressed nothing (a component library surface cannot pass)")

        if len(loud_hits) <= len(dirty_hits):
            failures.append("--keep-noise did not surface the suppressed declarations")

        if not [hit for hit in dirty_hits if hit["rule"] == "any-annot"]:
            failures.append("any-annot did not fire on an emitted `any`")

        if [hit for hit in dirty_hits if hit["file"].endswith("noise.d.ts") or hit["file"].endswith("long-noise.d.ts")]:
            failures.append("synthesised Vue declaration leaked past the noise filter")
    finally:
        shutil.rmtree(tmp, ignore_errors = True)

    if failures:
        for message in failures:
            print(f"SELFTEST FAIL: {message}", file = sys.stderr)

        return 1

    print("selftest OK: emitted-surface scan catches authored any, spares the clean surface, filters Vue synthesis without hiding it from --keep-noise")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 0 clean, 1 findings, 2 usage error."""

    parser = argparse.ArgumentParser(prog = "ts-surface", description = "Audit a package's published type surface (emit declarations, then scan them).")
    parser.add_argument("action", nargs = "?", default = "emit-scan", choices = ("emit-scan",), help = "emit-scan: emit declarations and scan the result.")
    parser.add_argument("--tsconfig", default = None, metavar = "PATH", help = "tsconfig to emit from (default: ./tsconfig.json).")
    parser.add_argument("--root", default = None, metavar = "PATH", help = "package root (default: the tsconfig's directory).")
    parser.add_argument("--command", default = None, metavar = "CMD", help = "checker command (default: node_modules/.bin/vue-tsc, then tsc).")
    parser.add_argument("--keep-noise", action = "store_true", help = "Also report `any` synthesised by Vue's own component generics (almost always noise).")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable output.")
    parser.add_argument("--selftest", action = "store_true", help = "Verify the scanner against its own controls.")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    config = Path(args.tsconfig or "tsconfig.json")

    if not config.is_file():
        print(f"ts-surface: no such tsconfig: {config}", file = sys.stderr)
        return 2

    root = Path(args.root) if args.root else config.parent.resolve()
    checker = args.command or default_checker(root)

    if checker is None:
        print("ts-surface: no checker found; pass --command (e.g. 'node_modules/.bin/tsc')", file = sys.stderr)
        return 2

    surface = Path(tempfile.mkdtemp(prefix = "ts-surface-"))

    try:
        status, output = emit_declarations(checker, root, config, surface, root)
        emitted = [item for suffix in DECLARATION_SUFFIXES for item in surface.rglob("*" + suffix)]

        if not emitted:
            message = f"declaration emit produced no files (checker exited {status}): {output.strip()[:400]}"

            if args.json:
                envelope.emit({"ok": False, "error": message})
            else:
                print(f"ts-surface: {message}", file = sys.stderr)

            return 1

        hits, noise = scan_surface(surface, args.keep_noise)
        documents = [dict(hit) for hit in hits]

        if args.json:
            envelope.emit({"ok": not documents, "emitted": len(emitted), "count": len(documents), "hits": documents, "noise_suppressed": noise, "checker_exit": status})
        else:
            for hit in documents:
                print(f"{hit['file']}:{hit['line']}  [{hit['rule']}]  {hit['source']}")

            print(f"\n{len(documents)} escape hatch(es) on the emitted surface; {len(emitted)} declaration file(s); {noise} synthesised Vue declaration(s) suppressed")

        return 1 if documents else 0
    finally:
        shutil.rmtree(surface, ignore_errors = True)

if __name__ == "__main__":
    raise SystemExit(main())
