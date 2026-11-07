"""`init --check` fails on local copies that shadow the shared justfile (SR-24/25).

common.just provides `fetch`, and the Git-Bash interop import has been obsolete
since bash was banned in recipes. The fleet audit found 37 repositories with a
local `fetch` copy (28 on the retired `dev` fallback URL, 9 on `master`) and 32
still importing the interop file; nothing warned, so the copies kept spreading.
"""

from __future__ import annotations
import sys
from celestia_devtools.repo import init as init_mod
from celestia_devtools.repo.init import _justfile_shadow_problems

def _run_init(monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["celestia-devtools", *args])
    return init_mod.main()

def _staged_repo(monkeypatch, tmp_path):
    """A checkout with clean staging + canonical workflows."""
    assert _run_init(monkeypatch, tmp_path, "--with-workflows") == 0
    return tmp_path

def _write_justfile(tmp_path, text):
    (tmp_path / "justfile").write_text(text, encoding = "utf-8")

def test_clean_justfile_passes(monkeypatch, tmp_path):
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/celestia-devtools.just"\n\n' "default:\n    @just --list\n")
    assert _justfile_shadow_problems(tmp_path) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0

def test_local_fetch_recipe_fails(monkeypatch, tmp_path):
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/celestia-devtools.just"\n\n' "fetch URL='':\n    curl -fsSL https://example.invalid/common.just -o .just/x\n")
    problems = _justfile_shadow_problems(tmp_path)
    assert any("local `fetch` recipe" in p for p in problems), problems
    assert _run_init(monkeypatch, tmp_path, "--check") == 1

def test_fetch_prefixed_recipe_is_not_the_shared_name(monkeypatch, tmp_path):
    """`fetch-icons:` is a different recipe; it must not trip the check."""
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/celestia-devtools.just"\n\n' "fetch-icons:\n    @echo icons\n")
    assert _justfile_shadow_problems(tmp_path) == []
    assert _run_init(monkeypatch, tmp_path, "--check") == 0

def test_obsolete_interop_import_fails(monkeypatch, tmp_path):
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/git-bash-interop.just"\n' 'import? "./.just/celestia-devtools.just"\n\n' "default:\n    @just --list\n")
    problems = _justfile_shadow_problems(tmp_path)
    assert any("git-bash-interop.just" in p for p in problems), problems
    assert _run_init(monkeypatch, tmp_path, "--check") == 1

def test_no_justfile_is_not_a_shadow_problem(tmp_path):
    assert _justfile_shadow_problems(tmp_path) == []

def test_fetch_variable_is_not_the_recipe(monkeypatch, tmp_path):
    """`fetch := "..."` is a variable assignment; only the recipe shadows common.just."""
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/celestia-devtools.just"\n\n' 'fetch := "https://example.invalid/common.just"\n\n' "default:\n    @just --list\n")
    assert _justfile_shadow_problems(tmp_path) == []

def test_historical_comment_mentioning_interop_is_not_an_import(monkeypatch, tmp_path):
    """Deleting the import but leaving a note about it must not keep the repo red."""
    _staged_repo(monkeypatch, tmp_path)
    _write_justfile(tmp_path, 'import? "./.just/celestia-devtools.just"\n' '# was: import? "./.just/git-bash-interop.just" (removed 2026-10-25)\n\n' "default:\n    @just --list\n")
    assert _justfile_shadow_problems(tmp_path) == []
