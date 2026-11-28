"""Regression tests for the semver gate's library-target detection.

Why this file exists
--------------------
``.github/workflows/semver-checks.yml`` skips a crate that has "no library
target", because cargo-semver-checks refuses proc-macro-only crates and exits
non-zero — which the publish loop would misread as "needs a bigger bump".

The detector used to test for the literal target kind ``"lib"``. Cargo
reports a crate that declares ``[lib] crate-type = ["rlib"]`` as
``kind: ["rlib"]``, not ``["lib"]`` — so kirino, an ordinary library, was
classified as proc-macro-only. Every kirino release logged ``no library
target … skipping`` and its public API was never diffed. A public field
added in 0.9.7 shipped as a patch and broke every external struct literal;
the gate that exists to refuse exactly that never ran (2026-11-28).

This test extracts the real detector out of the YAML, runs it against the
target kinds Cargo actually emits, and asserts the classification the loop
keys on. A mutation that narrows the kind set back to ``{"lib"}`` — or
that drops ``rlib`` alone — fails here.
"""

from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CALLEE = WORKFLOWS / "semver-checks.yml"

# The target kinds `cargo metadata --format-version 1` emits for the
# `[lib] crate-type` values a publishable crate can declare, plus the
# non-library kinds that must keep a crate out of the diff.
LIBRARY_KINDS = ["lib", "rlib", "dylib", "cdylib", "staticlib"]
NON_LIBRARY_KINDS = ["proc-macro", "bin", "test", "bench", "example", "custom-build"]

def _detector_source() -> str:
    """The python the workflow pipes `cargo metadata` into, verbatim."""
    doc = yaml.safe_load(CALLEE.read_text(encoding = "utf-8"))
    steps = doc["jobs"]["semver"]["steps"]
    run = next(s["run"] for s in steps if "HAS_LIB" in s.get("run", ""))
    # The detector is the `python3 -c '…'` the HAS_LIB process substitution
    # pipes into: the first one after the `HAS_LIB=()` declaration, and only
    # that one (the step holds several other python -c blocks).
    head = run.index("declare -A HAS_LIB=()")
    tail = run[head:]
    match = re.search(r"python3 -c '(.*?)'\s*\)", tail, re.DOTALL)
    assert match, "the HAS_LIB detector's python -c block was not found"
    return match.group(1)

def _classify(source: str, targets: list[dict]) -> dict[str, bool]:
    """Run the real detector over a fake `cargo metadata` payload."""
    payload = {"packages": [{"name": "pkg0", "targets": targets}]}
    proc = subprocess.run([sys.executable, "-c", source], input = json.dumps(payload), text = True, capture_output = True)
    # Not `check=True`: a detector that dies on a payload should fail the test
    # with its traceback in the failure message, not a bare CalledProcessError.
    assert proc.returncode == 0, f"the detector exited {proc.returncode}:\n{proc.stderr}"
    pairs = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    return {k: v == "true" for k, v in pairs.items()}

def test_every_library_kind_counts_as_a_library_target():
    """`lib`, `rlib`, `dylib`, `cdylib`, `staticlib` all keep a crate in the diff."""
    source = _detector_source()

    for kind in LIBRARY_KINDS:
        got = _classify(source, [{"kind": [kind]}])["pkg0"]
        assert got is True, f"a crate whose lib target is {kind!r} must be diffed"

def test_rlib_alone_is_a_library_target():
    """The kirino shape: one target, kind `["rlib"]`, nothing else.

    Pinned on its own because it is the exact case the old detector got
    wrong, and the one a future "simplification" is likeliest to lose.
    """
    source = _detector_source()
    got = _classify(source, [{"kind": ["rlib"], "name": "kirino", "src_path": "src/lib.rs"}])
    assert got["pkg0"] is True

def test_one_target_with_several_library_kinds_is_a_library_target():
    """`[lib] crate-type = ["lib", "dylib"]`: one target, several kinds.

    A detector that insists on exactly one kind per target would skip this
    real library, so the mixed shape is pinned on its own.
    """
    source = _detector_source()

    for kinds in (["lib", "dylib"], ["rlib", "cdylib"], ["rlib", "staticlib"]):
        got = _classify(source, [{"kind": kinds}])
        assert got["pkg0"] is True, f"a target of kinds {kinds} must be diffed"

def test_proc_macro_only_is_still_skipped():
    """A proc-macro crate has no API surface to diff; the skip stays."""
    source = _detector_source()
    got = _classify(source, [{"kind": ["proc-macro"]}])
    assert got["pkg0"] is False

def test_non_library_targets_do_not_count():
    """Bins, tests, benches and examples are not library targets."""
    source = _detector_source()

    for kind in NON_LIBRARY_KINDS:
        got = _classify(source, [{"kind": [kind]}])
        assert got["pkg0"] is False, f"{kind!r} must not be taken for a library"

def test_a_crate_with_a_lib_and_a_bin_is_a_library_target():
    """Mixed targets: one library among them is enough (the common bin+lib crate)."""
    source = _detector_source()
    got = _classify(source, [{"kind": ["bin"]}, {"kind": ["lib"]}])
    assert got["pkg0"] is True

def test_a_crate_with_no_targets_is_not_a_library_target():
    """Defensive: an empty target list classifies as skipped, never crashes."""
    source = _detector_source()
    got = _classify(source, [])
    assert got["pkg0"] is False

def test_a_target_without_a_kind_is_not_a_library_target():
    """A target whose `kind` is absent or null: skipped, never a crash.

    `cargo metadata` always fills `kind` for real targets, so this is the
    guard against a malformed payload rather than a live shape — which is
    exactly why it needs its own test: dropping the `or []` leaves every
    other test green (measured, devtools R4) and the detector then dies on
    `intersection(None)` instead of classifying.
    """
    source = _detector_source()

    for target in ({"name": "odd"}, {"kind": None}, {}):
        got = _classify(source, [target])
        assert got["pkg0"] is False, f"a target of {target} must classify as skipped"

def test_a_malformed_kind_alongside_a_library_still_counts_the_library():
    """One well-formed library target is enough even when a sibling lacks a kind.

    The malformed sibling comes first on purpose: `any` short-circuits, so a
    sibling placed after the library would never be looked at and the test
    would pass against a detector that dies on it. Three shapes — no `kind`
    key, a null `kind`, an empty target — because a detector may tolerate one
    and not another (the R5 mutant that broke on the missing key passed the
    null case).
    """
    source = _detector_source()

    for sibling in ({"name": "odd"}, {"kind": None}, {}):
        got = _classify(source, [sibling, {"kind": ["rlib"]}])
        assert got["pkg0"] is True, f"a malformed sibling {sibling} must not sink the library target"

def test_the_detector_does_not_test_for_the_literal_string_lib():
    """The shape of the bug, refused directly.

    The old line was `any("lib" in t.get("kind", []) …)` — a membership test
    for one exact string. The replacement names a set of library kinds; if
    someone narrows that set back to a single member, the tests above fail,
    and this one makes the reason legible in the diff.
    """
    source = _detector_source()
    assert "rlib" in source, "the library-kind set must name rlib explicitly"
    assert not re.search(r'"\s*lib\s*"\s+in\s+t', source), ("the detector is back to testing for the literal string 'lib'")
