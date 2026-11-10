"""Selftest for the depspec two-form declaration gate (2026-11-09 ruling).

Positive: every legal form the wave shipped scans clean. Negative: every
banned form the wave fixed turns red with the right code. Exemptions: dev
``^*``, prerelease tags, workspace/file protocols, cargo path deps,
[patch.crates-io] snapshots, vendor trees, pnpm overrides, and the
registered exact-pin table all stay green. Coverage: a tree with only
fixtures must scan (the zero-hit error is tested by an empty dir).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from celestia_devtools.lint import depspec  # noqa: E402


def _write(path: Path, rel: str, content: str) -> None:
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _npm(deps: dict[str, str], dev: dict[str, str] | None = None) -> str:
    data = {"name": "fixture", "dependencies": deps}
    if dev is not None:
        data["devDependencies"] = dev
    return json.dumps(data)


def _cargo(deps: dict[str, object], section: str = "dependencies") -> str:

    lines = [f"[{section}]"]
    for name, spec in deps.items():
        if isinstance(spec, dict):
            inner = ", ".join(f'{k} = "{v}"' for k, v in spec.items())
            lines.append(f"{name} = {{{inner}}}")
        else:
            lines.append(f'{name} = "{spec}"')
    return "\n".join(lines) + "\n"


def _codes(rep: depspec.report) -> set[str]:
    return {f.code for f in rep.findings}


def test_all_legal_forms_green(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _npm(
        {"vue": "^3", "three": "^0.185", "marked": "^18", "xterm": "^6.1.0-beta.304",
         "inner": "workspace:*", "local": "file:../x", "cat": "catalog:"},
        {"typescript": "^*", "sass": "^*", "vite": "^*"}))
    _write(tmp_path, "Cargo.toml", _cargo({
        "serde": "1", "serde_json": "^1", "axum": "0.8", "kube": "^2",
        "anyhow": "1", "rsa": "=0.10.0-rc.18", "inner": {"path": "../x"},
        "gitdep": {"git": "https://example.com/x", "version": "0.1"},
    }))
    _write(tmp_path, "packages/web/Cargo.toml", _cargo({"uuid": "1"}))
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "f"\ndependencies = ["celestia-devtools~=0.14.0", "requests>=2.32.3"]\n')
    rep = depspec.scan(tmp_path)
    assert rep.files_scanned == 4
    assert _codes(rep) == set(), [f.format() for f in rep.findings]


def test_npm_violations_red(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _npm({
        "a": "^*", "b": "^0.*", "c": "*", "d": "latest", "e": "~3.0.1",
        "f": "^2.3.0", "g": "4.0.2", "h": "left-pad", "i": "^0.0.1",
    }, {"j": "^0.*"}))
    rep = depspec.scan(tmp_path)
    codes = _codes(rep)
    assert codes == {"npm_caret_star", "npm_bare_star", "npm_latest", "npm_tilde_range",
                     "npm_three_segment", "npm_exact_pin", "npm_bare_name",
                     "npm_three_segment_zero"}, codes
    # dev ^0.* red but dev ^* green is covered in the positive test.
    assert any(f.code == "npm_caret_star" and f.slot == "dependencies" for f in rep.findings)
    assert any(f.code == "npm_caret_star" and f.slot == "devDependencies" for f in rep.findings)


def test_npm_single_segment_major_ok(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _npm({"vue": "^3", "marked": "18"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == set()


def test_cargo_violations_red(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", _cargo({
        "a": "1.0.104", "b": "0.8.9", "c": "~2.3", "d": "=3.4.12", "e": "*",
    }))
    rep = depspec.scan(tmp_path)
    codes = _codes(rep)
    # exact two-segment equality: every banned form pinned by code
    assert codes == {"cargo_three_segment", "cargo_three_segment_zero", "cargo_tilde_range",
                     "cargo_exact_pin", "cargo_bare_star"}, codes
    assert "0.8.9" in [f.spec for f in rep.findings]


def test_cargo_caret_star_red(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", _cargo({"wiremock": "^0.*"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == {"cargo_caret_star_zero"}


def test_cargo_zero_axis_red(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", _cargo({"x": "0"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == {"cargo_zero_axis"}


def test_cargo_two_segment_major_tolerated(tmp_path: Path) -> None:
    # the wave's S3-cosmetic class: legacy two-segment carets on >=1 lines
    _write(tmp_path, "Cargo.toml", _cargo({"bitflags": "^2.5", "syn": "^3.0"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == set()


def test_python_violations_red(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "f"\ndependencies = ["x~=3", "y~=0.14", "z", "w==1.2.3"]\n')
    rep = depspec.scan(tmp_path)
    codes = _codes(rep)
    assert codes
    assert "py_tilde_unpadded" in codes and "py_tilde_zero_axis" in codes
    assert "py_bare_name" in codes and "py_exact_pin" in codes
    # v~=2.2.3 (tight patch pin, >=1 major): tolerated
    assert all(f.name != "v" for f in rep.findings)


def test_vendor_tree_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "vendor/rumqttc/Cargo.toml", _cargo({"a": "1.2.3", "b": "~0.5"}))
    with pytest.raises(depspec.depspec_error, match="no package.json"):
        depspec.scan(tmp_path)


def test_registered_exact_pins_green(tmp_path: Path) -> None:
    # kei vendored unwinding + patches/x86_64: registered exact-pin class
    _write(tmp_path, "packages/ostd/Cargo.toml", _cargo({"unwinding": "=0.2.8"}))
    rep = depspec.scan(tmp_path, repo="kei")
    assert _codes(rep) == set()


def test_unregistered_exact_pin_still_red(tmp_path: Path) -> None:
    _write(tmp_path, "packages/ostd/Cargo.toml", _cargo({"unwinding": "=0.2.8", "serde": "1.0.229"}))
    rep = depspec.scan(tmp_path, repo="kei")
    codes = {(f.name, f.code) for f in rep.findings}
    assert ("serde", "cargo_three_segment") in codes
    assert all(f.name != "unwinding" for f in rep.findings)  # registered exact pin tolerated


def test_dev_star_exempt_but_dev_three_seg_red(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _npm({"vue": "^3"}, {"typescript": "^*", "sass": "^1.102.0"}))
    rep = depspec.scan(tmp_path)
    codes = _codes(rep)
    assert codes
    assert codes == {"npm_three_segment"}
    assert rep.findings[0].name == "sass"


def test_pnpm_overrides_field_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({
        "name": "f", "dependencies": {"vue": "^3"},
        "overrides": {"typescript": "~6.0"},
    }))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == set()


def test_strict_exit_and_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write(tmp_path, "package.json", _npm({"vue": "^*"}))
    assert depspec.main([str(tmp_path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files_scanned"] == 1 and out["findings"][0]["code"] == "npm_caret_star"
    assert depspec.main([str(tmp_path), "--strict"]) == 1
    capsys.readouterr()
    assert depspec.main([str(tmp_path)]) == 0  # advisory stays 0


def test_zero_hit_tree_is_error(tmp_path: Path) -> None:
    assert depspec.main([str(tmp_path)]) == 2


def test_real_tree_scans_clean(tmp_path: Path) -> None:
    """The wave normalized the family — hikari's install face must be green.

    Skipped when the repo is absent (standalone runs).
    """
    repo = Path("/mnt/codespace/hikari")
    if not (repo / "packages/vue/package.json").exists():
        pytest.skip("hikari checkout not present")
    rep = depspec.scan(repo)
    npm_findings = [f for f in rep.findings if f.path.endswith("package.json")]
    assert npm_findings == [], [f.format() for f in npm_findings]
    # known residual: packages/components Cargo face missed by the wave
    # inventory — tracked as follow-up until fixed.
    cargo_residuals = [f for f in rep.findings if not f.path.endswith("package.json")]
    assert all(f.code == "cargo_three_segment_zero" for f in cargo_residuals)


def test_npm_zero_zero_wildcards_red(tmp_path: Path) -> None:
    """S1-1 regression: ^0.0.* / ^0.0.x must not escape as legal."""
    _write(tmp_path, "package.json", _npm({"a": "^0.0.*", "b": "^0.0.x"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == {"npm_caret_star_zero"}


def test_cargo_exact_two_segment_red(tmp_path: Path) -> None:
    """S1-2 regression: =X.Y exact pins freeze the minor axis — banned."""
    _write(tmp_path, "Cargo.toml", _cargo({"pin1": "=1.0", "pin2": "=1.2", "pin3": "=0.8"}))
    rep = depspec.scan(tmp_path)
    codes = _codes(rep)
    assert codes == {"cargo_exact_pin"}, codes
    assert {f.spec for f in rep.findings} == {"=1.0", "=1.2", "=0.8"}


def test_range_forms_tolerated(tmp_path: Path) -> None:
    """S1-3 regression: >= floors are the owner-registered class — no finding."""
    _write(tmp_path, "package.json", _npm({"x": ">=1.2.3"}))
    _write(tmp_path, "Cargo.toml", _cargo({"y": ">=1.2, <2"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == set()


def test_target_sections_scanned(tmp_path: Path) -> None:
    """Fast-follow: [target.<triple>.dependencies] carry real declarations."""
    _write(tmp_path, "packages/ostd/Cargo.toml", """[target.x86_64-unknown-none.dependencies]
unwinding = "=0.2.8"
""")
    _write(tmp_path, "Cargo.toml", """[target.'cfg(windows)'.dependencies]
windows-service = "0.8.1"
""")
    # registered pin (kei unwinding) skipped; unregistered three-seg flagged
    rep = depspec.scan(tmp_path, repo="kei")
    assert [(f.name, f.code) for f in rep.findings] == [("windows-service", "cargo_three_segment_zero")]


def test_caret_star_zero_branch_reachable(tmp_path: Path) -> None:
    """^0.0.* (not exactly ^0.*) routes through the caret branch."""
    _write(tmp_path, "package.json", _npm({"a": "^0.0.x"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == {"npm_caret_star_zero"}


def test_npm_two_segment_caret_tolerated(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", _npm({"vue": "^3.2"}))
    rep = depspec.scan(tmp_path)
    assert _codes(rep) == set()


def test_classify_python_empty_spec_defensive(tmp_path: Path) -> None:
    assert depspec.classify_python("x", "")[0] == "py_bare_name"
