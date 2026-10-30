"""Tests for repo.toml_sort — the Cargo.toml dependency ordering gate.

*Why this exists:* `toml-sort --check` decides pass/fail for a repository and had
no direct test. The ordering itself is asserted through the public function, and
the `--check` exit codes are asserted through `main`, because that is what the
fleet's recipes actually call.
"""

from __future__ import annotations
from pathlib import Path
from celestia_devtools.repo.toml_sort import main, sort_toml_deps

UNSORTED = '[dependencies]\nzeta = "1"\nalpha = "1"\n'

def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding = "utf-8")
    return path

def _order(text: str) -> list[str]:
    return [line.split(" =")[0] for line in text.splitlines() if (line and (not line.startswith("[")))]

def test_entries_are_sorted():
    assert _order(sort_toml_deps(UNSORTED)) == ["alpha", "zeta"]

def test_sorting_is_idempotent():
    once = sort_toml_deps(UNSORTED)
    assert sort_toml_deps(once) == once

def test_check_mode_fails_on_an_unsorted_file(tmp_path, capsys):
    path = _write(tmp_path / "Cargo.toml", UNSORTED)
    assert main(["--check", str(path)]) == 1
    assert path.read_text(encoding = "utf-8") == UNSORTED  # check never writes

def test_check_mode_passes_once_sorted(tmp_path):
    path = _write(tmp_path / "Cargo.toml", sort_toml_deps(UNSORTED))
    assert main(["--check", str(path)]) == 0

def test_write_mode_sorts_in_place_and_succeeds(tmp_path):
    # Write mode reports success (the file it was asked to sort is now sorted);
    # only --check converts "needs sorting" into a failure.
    path = _write(tmp_path / "Cargo.toml", UNSORTED)
    assert main([str(path)]) == 0
    assert _order(path.read_text(encoding = "utf-8")) == ["alpha", "zeta"]
