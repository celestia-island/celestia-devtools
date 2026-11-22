#!/usr/bin/env python3
"""Tests for the artifact-drift gate (``celestia_devtools.lint.artifact_drift``).

The gate exists because hikari #1485 found that a WIT version bump without
regeneration left 571 files / 6769 tests and ``vue-tsc`` green — the old
bindings could ship beside a contract that no longer matched them. The design
fixes three negative controls, and all three are pinned here:

1. **a known positive must pass** — the four real hikari components match;
2. **a known negative must red** — a version bump without regeneration is
   reported BY COMPONENT with both versions;
3. **zero production must fail** — no contract, or a contract with no bindings,
   refuses instead of reporting green.

The fixtures are a miniature workspace that mirrors hikari's real layout, so the
cases are recomputable without hikari checked out. The real-corpus control is a
separate test that SKIPS when hikari is absent, and it is the one the design
names as the acceptance criterion.
"""

from __future__ import annotations
import shutil
from pathlib import Path
import pytest
from celestia_devtools.core.cli import COMMANDS
from celestia_devtools.lint import artifact_drift

HIKARI = Path("/mnt/codespace/hikari")

#: What a component's contract and bindings look like, distilled from the real
#: ones. ``types`` is the interface every component declares; ``celestia-hikari-``
#: is the prefix the generator gives it.
WIT = """package celestia:hikari-{component}@{version};

interface types {{
    record thing {{
        size: u8,
    }}
}}

world {component} {{
    use types.{{thing}};
    export encode: func() -> thing;
}}
"""

INTERFACE_DTS = """/** @module Interface celestia:hikari-{component}/types@{version} **/
export interface Thing {{ size: number; }}
"""

MODULE_DTS = """// world root:component/root
export type * as {component}Types from './interfaces/celestia-hikari-{component}-types.js'; // import celestia:hikari-{component}/types@{version}
"""

def build_workspace(root: Path, component: str = "qr", *, version: str = "0.1.0", artifact_version: str | None = None, extra_interface: bool = False) -> Path:
    """Write one component's contract + bindings; return ``root``."""
    wit_dir = root / "packages" / "wasm" / component / "wit"
    wit_dir.mkdir(parents = True, exist_ok = True)
    (wit_dir / f"{component}.wit").write_text(WIT.format(component = component, version = version), encoding = "utf-8")

    bindings = root / "packages" / "vue" / "src" / "wasm" / component
    interfaces = bindings / "interfaces"
    interfaces.mkdir(parents = True, exist_ok = True)
    version = artifact_version or version
    (interfaces / f"celestia-hikari-{component}-types.d.ts").write_text(INTERFACE_DTS.format(component = component, version = version), encoding = "utf-8")
    (bindings / f"hikari_wasm_{component}.d.ts").write_text(MODULE_DTS.format(component = component, version = version), encoding = "utf-8")

    if extra_interface:
        (interfaces / f"celestia-hikari-{component}-legacy.d.ts").write_text(INTERFACE_DTS.format(component = component, version = version), encoding = "utf-8")

    return root

class test_contract_parsing:
    def test_package_version_and_interfaces_are_read(self, tmp_path):
        build_workspace(tmp_path)
        parsed = artifact_drift.read_contract(tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit")
        assert parsed.package == "celestia:hikari-qr"
        assert parsed.version == "0.1.0"
        assert parsed.interfaces == ("types",)

    def test_the_bindings_filename_is_derived_from_the_package_id(self, tmp_path):
        build_workspace(tmp_path)
        parsed = artifact_drift.read_contract(tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit")
        assert parsed.binding_filename("types") == "celestia-hikari-qr-types.d.ts"

    def test_an_unversioned_contract_is_reported_not_skipped(self, tmp_path):
        """`palette.wit` is `package hikari:palette;` — no version at all."""
        wit = tmp_path / "packages" / "wasm" / "pal" / "wit" / "pal.wit"
        wit.parent.mkdir(parents = True)
        wit.write_text("package hikari:pal;\n\ninterface color {\n}\n", encoding = "utf-8")
        parsed = artifact_drift.read_contract(wit)
        assert parsed.version is None
        findings = artifact_drift.check_component(tmp_path, parsed)
        assert any("no @version" in item.detail for item in findings)

class test_negative_controls:
    """The three controls the design requires, mechanically."""

    def test_control_1_known_positive_passes(self, tmp_path):
        build_workspace(tmp_path)
        report = artifact_drift.check_repo(tmp_path)
        assert report.ok, [item.render() for item in report.findings]
        assert [parsed.version for parsed in report.contracts] == ["0.1.0"]

    def test_control_2_version_bump_without_regeneration_reds(self, tmp_path):
        """The #1485 shape: WIT moved to 0.2.0, the bindings did not."""
        build_workspace(tmp_path, version = "0.2.0", artifact_version = "0.1.0")
        report = artifact_drift.check_repo(tmp_path)
        assert not report.ok
        rendered = [item.render() for item in report.findings]
        assert any(("wit@0.2.0" in line) and ("artifact 0.1.0" in line) for line in rendered), rendered
        assert all(item.component == "qr" for item in report.findings)

    def test_control_2_names_the_component_not_the_tree(self, tmp_path):
        build_workspace(tmp_path, component = "qr", version = "0.2.0", artifact_version = "0.1.0")
        build_workspace(tmp_path, component = "mermaid")
        findings = [item for item in artifact_drift.check_repo(tmp_path).findings]
        assert {item.component for item in findings} == {"qr"}

    def test_control_3_no_contract_refuses(self, tmp_path):
        report = artifact_drift.check_repo(tmp_path)
        assert not report.ok
        assert report.problems and ("zero component contracts" in report.problems[0])

    def test_control_3_contract_without_bindings_refuses(self, tmp_path):
        wit = tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit"
        wit.parent.mkdir(parents = True)
        wit.write_text(WIT.format(component = "qr", version = "0.1.0"), encoding = "utf-8")
        report = artifact_drift.check_repo(tmp_path)
        assert not report.ok
        assert any("no generated bindings" in item.detail for item in report.findings)

    def test_control_3_bindings_without_any_module_header_refuses(self, tmp_path):
        build_workspace(tmp_path)
        # Strip the module header from both files: nothing names the package.
        bindings = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr"

        for path in bindings.rglob("*.d.ts"):
            path.write_text("export interface Thing { size: number; }\n", encoding = "utf-8")

        findings = artifact_drift.check_repo(tmp_path).findings
        assert any("do not name this package" in item.detail for item in findings)

    def test_the_cli_refuses_zero_production(self, tmp_path, capsys):
        assert artifact_drift.main(["--root", str(tmp_path)]) == 1
        assert "zero component contracts" in capsys.readouterr().err

    def test_the_cli_passes_a_matching_workspace(self, tmp_path, capsys):
        build_workspace(tmp_path)
        assert artifact_drift.main(["--root", str(tmp_path)]) == 0
        assert "MATCH" in capsys.readouterr().out

class test_interface_layer:
    """Layer two: the interface FILES are exactly the declared interfaces."""

    def test_a_declared_interface_with_no_file_reds(self, tmp_path):
        build_workspace(tmp_path)
        wit = tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit"
        wit.write_text(wit.read_text(encoding = "utf-8").replace("interface types {", "interface types {\n}\n\ninterface extra {", 1), encoding = "utf-8")
        findings = artifact_drift.check_repo(tmp_path).findings
        assert any(("no generated file" in item.detail) and ("extra" in item.detail) for item in findings)

    def test_a_stale_generated_interface_reds(self, tmp_path):
        build_workspace(tmp_path, extra_interface = True)
        findings = artifact_drift.check_repo(tmp_path).findings
        assert any("no longer declares" in item.detail for item in findings)

    def test_the_wasi_interfaces_are_not_counted(self, tmp_path):
        """wasi-* files are the runtime's, not this contract's."""
        build_workspace(tmp_path)
        interfaces = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "interfaces"
        (interfaces / "wasi-cli-stdin.d.ts").write_text("/** @module Interface wasi:cli/stdin@0.2.12 **/\n", encoding = "utf-8")
        assert artifact_drift.check_repo(tmp_path).ok

class test_scope:
    """Only a PR touching the contract or the bindings is subject to the gate."""

    @pytest.mark.parametrize("path", ["packages/wasm/qr/wit/qr.wit", "packages/vue/src/wasm/qr/interfaces/celestia-hikari-qr-types.d.ts", "packages/vue/src/wasm/qr/hikari_wasm_qr.d.ts"])
    def test_relevant_paths(self, path):
        assert artifact_drift.relevant_paths([path]) == [path]

    @pytest.mark.parametrize("path", ["README.md", "packages/vue/src/App.vue", "packages/wasm/qr/src/lib.rs", "tools/build.rs"])
    def test_irrelevant_paths(self, path):
        assert artifact_drift.relevant_paths([path]) == []

    def test_an_unrelated_change_is_not_relevant(self):
        assert artifact_drift.relevant_paths(["a.ts", "docs/x.md"]) == []

@pytest.mark.skipif(not (HIKARI / "packages" / "wasm").is_dir(), reason = "hikari checkout not present")
class test_real_corpus:
    """Control one against the ACTUAL four components, not a fixture."""

    def test_the_four_components_match(self):
        report = artifact_drift.check_repo(HIKARI)
        assert report.ok, [item.render() for item in report.findings]
        assert [parsed.component for parsed in report.contracts] == ["highlight", "mermaid", "qr", "scene3d"]

    def test_the_versions_are_the_ones_the_contracts_declare(self):
        report = artifact_drift.check_repo(HIKARI)
        assert {parsed.component: parsed.version for parsed in report.contracts} == {"highlight": "0.1.0", "mermaid": "0.1.0", "qr": "0.1.0", "scene3d": "0.1.0"}

    def test_a_bumped_contract_reds_against_the_real_bindings(self, tmp_path):
        """Control two against the real corpus: copy, bump one WIT, expect a named red."""
        copied = tmp_path / "hikari"
        shutil.copytree(HIKARI / "packages" / "wasm", copied / "packages" / "wasm")
        shutil.copytree(HIKARI / "packages" / "vue" / "src" / "wasm", copied / "packages" / "vue" / "src" / "wasm")
        wit = copied / "packages" / "wasm" / "qr" / "wit" / "qr.wit"
        wit.write_text(wit.read_text(encoding = "utf-8").replace("@0.1.0;", "@0.2.0;", 1), encoding = "utf-8")
        findings = artifact_drift.check_repo(copied).findings
        assert [item.component for item in findings] == ["qr"], [item.render() for item in findings]
        assert ("wit@0.2.0" in findings[0].detail) and ("0.1.0" in findings[0].detail)

class test_round2_regressions:
    """Defects the independent review of this PR found, each pinned."""

    def test_a_commented_out_package_line_does_not_win(self, tmp_path):
        """A commented-out older contract above the real one must not be parsed.

        `re.search` takes the first match, so without comment stripping the
        block comment's `@0.0.9` became "the" version and the gate reported
        drift that did not exist (MUT:block-comment-poison).
        """
        build_workspace(tmp_path)
        wit = tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit"
        text = wit.read_text(encoding = "utf-8")
        wit.write_text("/*\npackage celestia:hikari-qr@0.0.9;\n*/\n" + text, encoding = "utf-8")
        parsed = artifact_drift.read_contract(wit)
        assert parsed.version == "0.1.0"
        assert artifact_drift.check_repo(tmp_path).ok

    def test_a_trailing_line_comment_cannot_supply_the_version(self, tmp_path):
        """A `//` comment BEFORE a real declaration is inert for a different reason.

        `^package` cannot match text after `//`, so that shape passes with or
        without comment stripping — an unfalsifiable test (round 3 of this PR
        measured it green even with stripping disabled). What IS falsifiable is
        that a line carrying both a declaration and a trailing comment parses.
        """
        build_workspace(tmp_path)
        wit = tmp_path / "packages" / "wasm" / "qr" / "wit" / "qr.wit"
        wit.write_text("// a leading note\npackage celestia:hikari-qr@0.1.0; // trailing note\ninterface types {}\nworld qr {}\n", encoding = "utf-8")
        parsed = artifact_drift.read_contract(wit)
        assert parsed.version == "0.1.0"
        assert parsed.package == "celestia:hikari-qr"

    def test_a_prose_mention_in_a_binding_is_not_a_header(self, tmp_path):
        """A version quoted in a docstring must not red a correct tree."""
        build_workspace(tmp_path)
        header = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "interfaces" / "celestia-hikari-qr-types.d.ts"
        header.write_text(header.read_text(encoding = "utf-8") + "// migration note: pin `celestia:hikari-qr/types@0.0.9` when debugging\n", encoding = "utf-8")
        assert artifact_drift.check_repo(tmp_path).ok

    def test_a_real_stale_header_still_reds(self, tmp_path):
        """The mirror: a genuine @module header at the wrong version must red."""
        build_workspace(tmp_path, version = "0.2.0", artifact_version = "0.1.0")
        findings = artifact_drift.check_repo(tmp_path).findings
        assert findings and ("wit@0.2.0" in findings[0].detail)

    def test_a_nested_interfaces_directory_is_not_counted(self, tmp_path):
        """Only the component's OWN interfaces/ counts; a stale dist/ copy is not live."""
        build_workspace(tmp_path)
        ghost = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "dist" / "interfaces"
        ghost.mkdir(parents = True)
        (ghost / "celestia-hikari-qr-ghost.d.ts").write_text("/** @module Interface celestia:hikari-qr/types@0.1.0 **/\n", encoding = "utf-8")
        assert artifact_drift.check_repo(tmp_path).ok

    def test_the_cli_is_registered_and_runs(self, tmp_path, capsys):
        """The design names `lint-artifact-drift`; an unregistered module is unreachable."""
        assert "lint-artifact-drift" in COMMANDS
        assert COMMANDS["lint-artifact-drift"] == "celestia_devtools.lint.artifact_drift"
        build_workspace(tmp_path)
        rc = artifact_drift.main(["--root", str(tmp_path), "--quiet"])
        assert rc == 0
        capsys.readouterr()

    def test_a_stale_import_appendix_reds(self, tmp_path):
        """The generator's real version carrier is a TRAILING `// import` line.

        Round 4 found the round-3 fix had narrowed the version-source set past
        the real shape; round 5 found the first version of this test was not
        load-bearing. The barrel carries NO `@module` header of its own — only
        the import appendix — so this asserts the appendix alone is a source.
        """
        build_workspace(tmp_path)
        barrel = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "hikari_wasm_qr.d.ts"
        barrel.write_text("// world root:component/root\nexport type * as Q from './i.js'; // import celestia:hikari-qr/types@0.0.9\n", encoding = "utf-8")
        findings = artifact_drift.check_repo(tmp_path).findings
        assert findings, "a stale import appendix must red"
        assert "artifact 0.0.9" in findings[0].detail

    def test_a_matching_appendix_with_prose_mention_stays_green(self, tmp_path):
        """The shape that DISTINGUISHES round 4 from round 3.

        A correct appendix plus prose that names an old version: round 3's raw
        scan reddened this (a false red, the defect round 3 fixed), round 4's
        header filter leaves it green. Reverting `_header_text` reds this test
        and nothing else — pinned by MUT:header-filter-removed.
        """
        build_workspace(tmp_path)
        barrel = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "hikari_wasm_qr.d.ts"
        barrel.write_text("// world root:component/root\nexport type * as Q from './i.js'; // import celestia:hikari-qr/types@0.1.0\n// migration: the old pin was celestia:hikari-qr/types@0.0.9\n", encoding = "utf-8")
        assert artifact_drift.check_repo(tmp_path).ok

    def test_a_matching_import_appendix_passes(self, tmp_path):
        """The mirror, also end to end: the same line at the declared version."""
        build_workspace(tmp_path)
        barrel = tmp_path / "packages" / "vue" / "src" / "wasm" / "qr" / "hikari_wasm_qr.d.ts"
        barrel.write_text("// world root:component/root\nexport type * as QrTypes from './interfaces/celestia-hikari-qr-types.js'; // import celestia:hikari-qr/types@0.1.0\n", encoding = "utf-8")
        assert artifact_drift.check_repo(tmp_path).ok

    def test_matching_import_appendix_passes(self):
        """The mirror: the same line at the declared version is fine."""
        line = "export type * as X from './i.js'; // import celestia:hikari-qr/types@0.1.0"
        assert artifact_drift._header_text(line).strip()
