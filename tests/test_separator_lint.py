"""Tests for lint.separator_lint — the one decision gate that shipped without any.

*Why this exists:* `separator_lint` had no test module at all. It runs on every
repository through `just lint-separators`, so its default mode is a gate the
fleet relies on, and nothing was pinning what that default does or does not
catch. The conflict-marker half in particular is opt-in (`--conflict-markers`)
and no consumer passes it, so the tests below pin the *actual* semantics —
notice, not failure — rather than the semantics the docstring implies.

Fixture text is built from `"=" * N` rather than written out, so this file does
not itself trip the separator lint it is testing.
"""

from __future__ import annotations
import sys
from pathlib import Path
from celestia_devtools.lint.separator_lint import _fix_line, lint_file, main

_PLAIN = "# " + ("=" * 16) + "\n"
_LABELLED = "// " + ("=" * 6) + " Public API " + ("=" * 6) + "\n"
_FIXED_LABELLED = "// ------ Public API ------"
_CONFLICT = ("<" * 7) + " HEAD\n"

def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding = "utf-8")
    return path

def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["lint-separators", *argv])
    return main()

class test_separator_detection:
    def test_long_comment_separator_is_reported(self, tmp_path):
        path = _write(tmp_path / "sample.py", _PLAIN)
        assert lint_file(path) == ([1], [])

    def test_labelled_separator_is_rewritten_to_six_dashes(self, tmp_path):
        path = _write(tmp_path / "sample.py", _LABELLED)
        assert lint_file(path) == ([1], [])
        assert _fix_line(path.read_text(encoding = "utf-8").splitlines()[0]) == _FIXED_LABELLED

    def test_clean_file_reports_nothing(self, tmp_path):
        path = _write(tmp_path / "sample.py", "# a normal comment\nprint(1)\n")
        assert lint_file(path) == ([], [])

    def test_binary_file_is_skipped(self, tmp_path):
        path = tmp_path / "blob.bin"
        path.write_bytes(b"\0" + _PLAIN.encode("utf-8"))
        assert lint_file(path) == ([], [])

class test_conflict_markers_are_opt_in:
    def test_flag_makes_conflict_markers_fail(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path / "sample.py", _CONFLICT)
        result = _run(monkeypatch, str(tmp_path), "--conflict-markers")
        captured = capsys.readouterr()
        assert result == 1
        assert "merge-conflict markers" in captured.err

    def test_default_run_warns_without_failing(self, tmp_path, monkeypatch, capsys):
        # The semantics every consumer gets: `just lint-separators` passes no
        # flags, so a merge-conflict marker must not fail the sweep -- but it
        # must not be silent either.
        _write(tmp_path / "sample.py", _CONFLICT)
        assert _run(monkeypatch, str(tmp_path)) == 0
        captured = capsys.readouterr()
        assert "merge-conflict marker(s) found" in captured.err
        assert "--conflict-markers" in captured.err

class test_cli_contract:
    def test_separator_violation_fails_a_directory(self, tmp_path, monkeypatch):
        _write(tmp_path / "sample.py", _PLAIN)
        assert _run(monkeypatch, str(tmp_path)) == 1

    def test_separator_violation_fails_a_single_file(self, tmp_path, monkeypatch):
        # The file branch of the argument loop is a separate path; a directory
        # walk alone would let it rot.
        path = _write(tmp_path / "single.py", _PLAIN)
        assert _run(monkeypatch, str(path)) == 1

    def test_missing_path_fails(self, tmp_path, monkeypatch):
        assert _run(monkeypatch, str(tmp_path / "nope")) == 1

    def test_fix_rewrites_to_the_exact_convention(self, tmp_path, monkeypatch):
        path = _write(tmp_path / "sample.py", _LABELLED)
        # `--fix` still exits 1: it reports the issues it found, then fixes them.
        assert _run(monkeypatch, str(tmp_path), "--fix") == 1
        assert path.read_text(encoding = "utf-8") == (_FIXED_LABELLED + "\n")
        assert _run(monkeypatch, str(tmp_path)) == 0
