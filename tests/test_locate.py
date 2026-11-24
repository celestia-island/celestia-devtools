from __future__ import annotations
from pathlib import Path
from celestia_devtools.repo import locate

MARKER = Path("Cargo.toml")

def _checkout(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents = True)
    (d / MARKER).write_text("[package]\n", encoding = "utf-8")
    return d

class test_sibling_candidates:
    def test_plain_checkout_finds_the_sibling_next_door(self, tmp_path):
        me = _checkout(tmp_path, "shittim-chest")
        arona = _checkout(tmp_path, "arona")
        assert locate._sibling_candidates(me, "arona") == [tmp_path / "arona"]
        assert locate.find_patched_crate("arona", "ARONA_ROOT_TEST", "https://invalid.example/arona.git", MARKER, repo_root = me) == arona.resolve()

    def test_worktree_checkout_finds_the_sibling_three_levels_up(self, tmp_path):
        """`_worktree/<repo>/<slug>` → siblings live under `<codespace>`, not
        under `_worktree/<repo>`."""
        arona = _checkout(tmp_path, "arona")
        me = tmp_path / "_worktree" / "shittim-chest" / "some-slug"
        me.mkdir(parents = True)
        (me / MARKER).write_text("[package]\n", encoding = "utf-8")
        assert locate._sibling_candidates(me, "arona") == [me.parent / "arona", tmp_path / "arona"]
        assert locate.find_patched_crate("arona", "ARONA_ROOT_TEST", "https://invalid.example/arona.git", MARKER, repo_root = me) == arona.resolve()

    def test_repo_dir_under_worktree_finds_siblings_two_levels_up(self, tmp_path):
        arona = _checkout(tmp_path, "arona")
        me = tmp_path / "_worktree" / "shittim-chest"
        me.mkdir(parents = True)
        (me / MARKER).write_text("[package]\n", encoding = "utf-8")
        assert locate.find_patched_crate("arona", "ARONA_ROOT_TEST", "https://invalid.example/arona.git", MARKER, repo_root = me) == arona.resolve()

    def test_no_sibling_falls_through_to_the_clone_last_resort(self, tmp_path, monkeypatch):
        """With no sibling anywhere the locator reaches step 5 (clone). The
        clone is stubbed to fail, so the result is None and nothing touches
        the network — and the stub proves the sibling probes really missed."""
        me = _checkout(tmp_path, "shittim-chest")
        monkeypatch.delenv("ARONA_ROOT_TEST", raising = False)
        calls = []

        def _fake_run(cmd, **_kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 128})()

        monkeypatch.setattr(locate.subprocess, "run", _fake_run)
        got = locate.find_patched_crate("arona", "ARONA_ROOT_TEST", "https://invalid.example/arona.git", MARKER, repo_root = me)
        assert got is None
        assert (len(calls) == 1) and (calls[0][:2] == ["git", "clone"])
