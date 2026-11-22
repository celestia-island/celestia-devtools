#!/usr/bin/env python3
"""artifact-drift - a WIT contract and its generated bindings must agree.

*Why this exists:* hikari #1485 gave the four wasm components versioned WIT
contracts (``package celestia:hikari-<c>@<major>.<minor>;``). The generated
bindings under ``packages/vue/src/wasm/<c>/`` carry that version in every
interface module header. Nothing, however, forces them to move together: the
PR that bumped ``qr.wit`` to ``@0.2.0`` and forgot to re-run the generator left
**571 files / 6769 tests and ``vue-tsc`` green** — the old ``@0.1.0`` bindings
shipped beside a contract that no longer matched them. A consumer pinning
``celestia:hikari-qr/<world>@0.2.0`` then talks to a module that only ever
declares ``@0.1.0``. This gate is the only thing that can stop that.

*What it compares (phase one, the two cheapest layers):*

1. **Package version.** The WIT ``package`` line names one version; every
   ``celestia:hikari-<c>/<interface>@<v>`` module header in the bindings must
   carry THAT version. A bump without regeneration leaves the old version in
   the headers and is reported by component, both versions included.
2. **Interface list.** The WIT ``interface`` declarations (kebab-cased, prefixed
   ``celestia-hikari-<c>-``) must match the ``interfaces/*.d.ts`` basenames for
   that component. Adding or removing an interface without regenerating is
   therefore caught even when the version did not move.

*What it deliberately does NOT do:*

* **No byte comparison** of ``packages/vue/src/wasm/**``. A jco/toolchain bump
  rewrites every byte of every binding while the contract is unchanged, and a
  gate that goes red for that is a gate somebody turns off — the noise death.
* **No signature comparison** (record/enum field sets). It would catch
  "added a field, forgot to regenerate", but it needs a WIT parser and is a
  phase-two cost; layers 1 and 2 already cover version drift and interface
  add/remove, the two highest-frequency shapes.

*Scope:* components are discovered from ``packages/wasm/*/wit/*.wit`` — not
hardcoded, so a new component joins the gate by existing. ``palette.wit`` lives
outside ``packages/wasm`` and is not a wasm component contract (no version, no
bindings directory); widening the scan there is a decision, not an oversight.
"""

from __future__ import annotations
import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

#: Where a component's contract and its generated bindings live, relative to a
#: checkout root. Kept in one place so the merge gate's "is this PR relevant?"
#: filter and the scan itself can never disagree.
WIT_GLOB = "packages/wasm/*/wit/*.wit"
BINDINGS_GLOB = "packages/vue/src/wasm/{component}"

#: The paths a PR must touch for this gate to have anything to say. Same two
#: roots the design names, expressed as prefixes so a merge-gate filter can test
#: them without globbing a whole checkout.
#: A WIT contract lives under ``<root>/wit/``; the bindings under
#: ``packages/vue/src/wasm/``. Rust sources and build scripts inside a wasm
#: package are NOT drift surface — a contract edit always touches the ``.wit``.
RELEVANT_PATTERNS = (re.compile(r"^packages/wasm/[^/]+/wit/"), re.compile(r"^packages/vue/src/wasm/"))

_PACKAGE = re.compile(r"^package\s+(?P<id>[A-Za-z0-9][A-Za-z0-9._:-]*)(?:@(?P<version>[0-9][0-9A-Za-z.\-]*))?\s*;", re.MULTILINE)
_INTERFACE = re.compile(r"^\s*interface\s+(?P<name>[A-Za-z0-9][A-Za-z0-9-]*)\s*\{", re.MULTILINE)
#: A generated module header, e.g. ``/** @module Interface celestia:hikari-qr/types@0.1.0 **/``.
_MODULE = re.compile(r"celestia:hikari-(?P<component>[A-Za-z0-9-]+)/(?P<interface>[A-Za-z0-9-]+)@(?P<version>[0-9][0-9A-Za-z.\-]*)")

@dataclass(frozen = True)
class contract:
    """One component's contract as written in its WIT."""

    component: str
    wit: Path
    package: str
    version: str | None
    interfaces: tuple[str, ...]

    def binding_filename(self, interface: str) -> str:
        """The ``interfaces/`` basename this interface generates."""
        return f"{self.package.replace(':', '-')}-{interface}.d.ts"

@dataclass(frozen = True)
class finding:
    component: str
    detail: str

    def render(self) -> str:
        return f"{self.component}: {self.detail}"

def read_contract(wit_path: str | Path) -> contract:
    """Parse one ``.wit``; ``(package, version, interfaces)``."""
    path = Path(wit_path)
    text = path.read_text(encoding = "utf-8")
    package = _PACKAGE.search(text)

    if package is None:
        return contract(component = path.stem, wit = path, package = "", version = None, interfaces = ())

    return contract(component = path.stem, wit = path, package = package.group("id"), version = package.group("version"), interfaces = tuple(dict.fromkeys(match.group("name")for match in _INTERFACE.finditer(text))))

def discover(root: str | Path) -> list[contract]:
    """Every component contract under the workspace's ``packages/wasm``."""
    return sorted((read_contract(path) for path in Path(root).glob(WIT_GLOB)), key = lambda item: item.component)

def _module_versions(bindings: Path, component: str) -> dict[str, set[str]]:
    """``{interface: {versions seen}}`` across a component's generated files."""
    seen: dict[str, set[str]] = {}

    for path in bindings.rglob("*.d.ts"):
        try:
            text = path.read_text(encoding = "utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for match in _MODULE.finditer(text):
            if match.group("component") != component:
                continue

            seen.setdefault(match.group("interface"), set()).add(match.group("version"))

    return seen

def check_component(root: str | Path, parsed: contract) -> list[finding]:
    """Every drift this component shows (empty = the bindings agree with the WIT)."""
    findings: list[finding] = []
    bindings_root = Path(root) / BINDINGS_GLOB.format(component = parsed.component)

    if parsed.version is None:
        # A component contract without a version cannot be drift-checked. Say so
        # rather than passing silently: an unversioned wasm contract is exactly
        # the state #1485 removed.
        return [finding(parsed.component, f"{parsed.wit.name} declares no @version — a wasm component contract is versioned (see hikari #1485)")]

    if not bindings_root.is_dir():
        return [finding(parsed.component, f"no generated bindings at {bindings_root} — the contract has never been generated")]

    canonical = sorted(path for path in bindings_root.rglob("*.d.ts"))

    if not canonical:
        return [finding(parsed.component, f"no *.d.ts under {bindings_root} — nothing was generated")]

    # Layer 1 — every module header must carry the WIT's version.
    versions = _module_versions(bindings_root, parsed.component)

    if not versions:
        findings.append(finding(parsed.component, f"no celestia:hikari-{parsed.component}/<interface>@<version> module header under {bindings_root} — the bindings do not name this package"))

    for interface, seen in sorted(versions.items()):
        stale = sorted(version for version in seen if version != parsed.version)

        if stale:
            findings.append(finding(parsed.component, f"{interface}: wit@{parsed.version} artifact {', '.join(stale)} — regenerate the bindings"))

    # Layer 2 — the interface files on disk are exactly the declared ones.
    declared = {parsed.binding_filename(name) for name in parsed.interfaces}
    present = {path.name for path in canonical if (path.parent.name == "interfaces") and path.name.startswith(f"{parsed.package.replace(':', '-')}-")}
    missing = sorted(declared - present)

    if missing:
        findings.append(finding(parsed.component, f"declared interface(s) with no generated file: {', '.join(missing)}"))

    extra = sorted(present - declared)

    if extra:
        findings.append(finding(parsed.component, f"generated interface file(s) the WIT no longer declares: {', '.join(extra)}"))

    return findings

@dataclass(frozen = True)
class report:
    contracts: tuple[contract, ...]
    findings: tuple[finding, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems and not self.findings

def check_repo(root: str | Path) -> report:
    """The whole verdict for one checkout root."""
    contracts = discover(root)
    problems: list[str] = []

    if not contracts:
        # Zero production must fail: no ``.wit`` under packages/wasm is either a
        # non-wasm checkout or a broken glob, and a green verdict cannot tell
        # the two apart.
        problems.append(f"zero component contracts found under {(Path(root) / WIT_GLOB).as_posix()} — refusing to report a green scan over nothing")

    findings: list[finding] = []

    for parsed in contracts:
        findings.extend(check_component(root, parsed))

    if contracts and not findings and not problems:
        pass

    return report(contracts = tuple(contracts), findings = tuple(findings), problems = tuple(problems))

def relevant_paths(paths: Sequence[str]) -> list[str]:
    """The paths that make a PR subject to this gate."""
    return [path for path in paths if any(pattern.match(path) for pattern in RELEVANT_PATTERNS)]

def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``lint-artifact-drift [--root DIR] [--component C ...] [--json]``."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools lint-artifact-drift", description = "Fail when a WIT contract and its generated bindings disagree (version / interface list).")
    parser.add_argument("--root", default = ".", help = "checkout to scan (the hikari repository)")
    parser.add_argument("--component", action = "append", default = None, help = "scan only this component (repeatable)")
    parser.add_argument("--quiet", action = "store_true", help = "only the closing verdict line")
    args = parser.parse_args(argv)

    if args.component:
        contracts = [parsed for parsed in discover(args.root) if parsed.component in set(args.component)]

        if not contracts:
            print(f"❌ artifact-drift: no contract matches --component {args.component} under {args.root}", file = sys.stderr)
            return EXIT_USAGE

        findings = [item for parsed in contracts for item in check_component(args.root, parsed)]
        result = report(contracts = tuple(contracts), findings = tuple(findings), problems = ())
    else:
        result = check_repo(args.root)

    for problem in result.problems:
        print(f"❌ {problem}", file = sys.stderr)

    for item in result.findings:
        print(f"❌ {item.render()}")

    if not result.ok:
        print(f"ARTIFACT-DRIFT REFUSED — {len(result.findings)} finding(s), {len(result.problems)} problem(s)", file = sys.stderr)
        return EXIT_FINDINGS

    if not args.quiet:
        names = ", ".join(f"{parsed.component}@{parsed.version}" for parsed in result.contracts)
        print(f"✅ artifact-drift: {len(result.contracts)} component(s) MATCH ({names})")

    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
