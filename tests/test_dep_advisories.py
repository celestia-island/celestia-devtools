# py-style-ok-file: imports-at-top: tests run against a src checkout (sys.path.insert must precede the package import)
"""Selftest for the unmaintained-dependency warning channel (RUSTSEC-2026-0543).

Positive: a Cargo.toml declaration and Rust sources that name ``chrono`` are
reported with their evidence. Negative: ``chrono-tz`` (a different crate) and
wishwell's ``chrono_no_dep_timestamp`` (a function that exists *to avoid*
chrono) must not be reported — the word boundary is the whole point of the
pattern, so both shapes get a test rather than a comment.

Modes: advisory run exits 0, ``--strict`` exits 1, ``--json`` carries the same
fields on stdout (text stays on stderr), and a tree with neither a manifest nor
a source file is an error (2) — "nothing scanned" must never read as green.
"""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from celestia_devtools.lint import dep_advisories  # noqa: E402

def _write(path: Path, rel: str, content: str) -> None:
    target = path / rel
    target.parent.mkdir(parents = True, exist_ok = True)
    target.write_text(content, encoding = "utf-8")

def test_manifest_declaration_reported(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\n\n[dependencies]\nchrono = { version = "0.4", features = ["serde"] }\n')
    rep = dep_advisories.scan(tmp_path)
    assert len(rep.hits) == 1
    bucket = rep.hits[0]
    assert bucket.advisory.crate == "chrono"
    assert bucket.advisory.id == "RUSTSEC-2026-0543"
    assert bucket.declarations == ["Cargo.toml:5"]
    assert bucket.sources == []

def test_workspace_and_target_tables_reported(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[workspace.dependencies]\nchrono = { version = "^0.4", features = ["serde"] }\n')
    _write(tmp_path, "packages/k/Cargo.toml", '[target.riscv64imac-unknown-none-elf.dependencies]\nchrono = { version = "^0.4", default-features = false }\n')
    rep = dep_advisories.scan(tmp_path)
    declarations = rep.hits[0].declarations
    assert declarations == ["Cargo.toml:2", "packages/k/Cargo.toml:2"], declarations
    assert rep.manifests == 2

def test_hyphenated_and_underscored_shapes_are_not_chrono(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nchrono-tz = { version = "^0.10", features = ["serde"] }\n')
    _write(tmp_path, "src/store.rs", 'fn chrono_no_dep_timestamp() -> u64 { 1 }\nfn tz() -> chrono_tz::Tz { chrono_tz::Tz::UTC }\n')
    rep = dep_advisories.scan(tmp_path)
    assert rep.hits == [], [h.to_dict() for h in rep.hits]
    assert rep.manifests == 1
    assert rep.sources == 1

def test_source_mentions_reported(tmp_path: Path) -> None:
    _write(tmp_path, "src/lib.rs", "use chrono::{DateTime, Utc};\npub fn now() -> DateTime<Utc> { Utc::now() }\n")
    _write(tmp_path, "src/clean.rs", "pub fn answer() -> u8 { 42 }\n")
    rep = dep_advisories.scan(tmp_path)
    assert len(rep.hits) == 1
    assert rep.hits[0].sources == ["src/lib.rs"]
    assert rep.sources == 2

def test_clean_tree_has_no_hits(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\n\n[dependencies]\nserde = "1"\n')
    _write(tmp_path, "src/lib.rs", "pub fn f() {}\n")
    rep = dep_advisories.scan(tmp_path)
    assert rep.hits == []
    assert dep_advisories.render(rep.hits) == ""

def test_advisory_exit_zero_strict_exit_one(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nchrono = "0.4"\n')
    assert dep_advisories.main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "warning: chrono is unmaintained" in captured.err
    assert "migrate to jiff as soon as possible" in captured.err
    assert captured.out == ""
    assert dep_advisories.main([str(tmp_path), "--strict"]) == 1

def test_clean_tree_advisory_and_strict_both_zero(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nserde = "1"\n')
    assert dep_advisories.main([str(tmp_path)]) == 0
    assert dep_advisories.main([str(tmp_path), "--strict"]) == 0

def test_json_payload_shape(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nchrono = "0.4"\n')
    _write(tmp_path, "src/lib.rs", "use chrono::Utc;\n")
    assert dep_advisories.main([str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["manifests_scanned"] == 1
    assert payload["sources_scanned"] == 1
    entry = payload["advisories"][0]
    assert entry["crate"] == "chrono"
    assert entry["id"] == "RUSTSEC-2026-0543"
    assert entry["replacement"] == ["jiff"]
    assert entry["declarations"] == ["Cargo.toml:2"]
    assert entry["sources"] == ["src/lib.rs"]
    assert captured.err == ""

def test_missing_path_is_exit_two(tmp_path: Path) -> None:
    assert dep_advisories.main([str(tmp_path / "nope")]) == 2

def test_tree_without_manifest_or_source_is_exit_two(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("nothing to scan\n", encoding = "utf-8")
    assert dep_advisories.main([str(tmp_path)]) == 2

def test_render_caps_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nchrono = "0.4"\n')

    for index in range(dep_advisories.MAX_EVIDENCE + 3):
        _write(tmp_path, f"src/m{index}.rs", "use chrono::Utc;\n")

    rep = dep_advisories.scan(tmp_path)
    text = dep_advisories.render(rep.hits)
    assert "+3 more" in text
    assert "src/m0.rs" in text
    assert "src/m7.rs" not in text


def _stage(tmp_path: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check = True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.invalid"], check = True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check = True)
    for rel, content in files.items():
        _write(tmp_path, rel, content)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check = True)


def test_staged_view_sees_only_the_commit(tmp_path: Path) -> None:
    """The pre-commit channel: warn on what this commit touches, not the whole tree."""
    _stage(tmp_path, {"Cargo.toml": '[dependencies]\nchrono = "0.4"\n'})
    rep = dep_advisories.scan_staged(tmp_path)
    assert [h.advisory.id for h in rep.hits] == ["RUSTSEC-2026-0543"]
    assert rep.hits[0].declarations == ["Cargo.toml:2"]
    _write(tmp_path, "src/late.rs", "use chrono::Utc;\n")
    assert dep_advisories.scan_staged(tmp_path).sources == 0


def test_staged_view_quiet_and_advisory_exit(tmp_path: Path, capsys) -> None:
    _stage(tmp_path, {"src/lib.rs": "use chrono::Utc;\n"})
    assert dep_advisories.main([str(tmp_path), "--staged", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert "warning: chrono is unmaintained" in captured.err
    assert "scanned" not in captured.err


def test_staged_clean_commit_is_silent(tmp_path: Path, capsys) -> None:
    _stage(tmp_path, {"Cargo.toml": '[dependencies]\nserde = "1"\n'})
    assert dep_advisories.main([str(tmp_path), "--staged", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_staged_mode_outside_git_is_not_an_error(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "Cargo.toml", '[dependencies]\nchrono = "0.4"\n')
    assert dep_advisories.main([str(tmp_path), "--staged"]) == 0
    captured = capsys.readouterr()
    assert "warning: chrono" not in captured.err
