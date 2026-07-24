"""Tests for CLI dispatch (core/cli.py)."""

import subprocess
import sys

import pytest

from celestia_devtools.core.cli import COMMANDS, main


class TestCommandRegistry:
    def test_all_commands_registered(self):
        # The full set of commands the dispatcher knows about. Update this set
        # whenever a command is added or removed in core/cli.py — keeping it an
        # explicit literal (rather than set(COMMANDS)) guards against accidental
        # removals of a command that downstream justfiles depend on.
        expected = {
            "cache-guard", "format-markdown", "prefetch", "check-cross-deps",
            "npm-dist", "preflight", "wsl-ensure", "qemu-ensure", "pglite",
            "serve", "locate", "register-patches", "register-npm-patches", "init",
            "commit-msg-lint", "hook", "pr-merge", "gh", "publish-crates",
            "daemon", "mock-start", "mock-status", "mock-stop",
            "registry", "toml-sort",
        }
        assert set(COMMANDS.keys()) == expected

    @pytest.mark.parametrize("cmd,module_path", list(COMMANDS.items()))
    def test_command_modules_importable(self, cmd, module_path):
        """Every registered command must resolve to an importable module with main()."""
        from importlib import import_module

        mod = import_module(module_path)
        assert callable(getattr(mod, "main", None)), f"{cmd} -> {module_path} has no main()"


class TestVersionHelp:
    def test_version(self, capsys):
        rc = main(["--version"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "celestia-devtools" in captured.out

    def test_help(self, capsys):
        rc = main(["--help"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "cache-guard" in captured.out

    def test_no_args_shows_help(self, capsys):
        rc = main([])
        captured = capsys.readouterr()
        assert rc == 0
        assert "format-markdown" in captured.out


class TestUnknownCommand:
    def test_unknown_returns_error(self, capsys):
        rc = main(["nonexistent-command"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "unknown command" in captured.err


class TestIncludePath:
    def test_include_path_prints_common_just(self, capsys):
        rc = main(["include-path"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "common.just" in captured.out


class TestModuleRun:
    def test_python_m_invocation(self):
        """`python -m celestia_devtools --version` should work."""
        result = subprocess.run(
            [sys.executable, "-m", "celestia_devtools", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "celestia-devtools" in result.stdout


class TestStaleLocalPatchCleanup:
    def test_removes_stale_absolute_path(self):
        from celestia_devtools.repo.register_patches import _clean_stale_local_patches

        import os
        bad = os.path.abspath(os.path.join(os.sep, "nonexistent", "abcdef", "crate"))
        config = f"""[patch.crates-io]
kirino = {{ path = "{bad}" }}
aoba = {{ path = "{os.sep}tmp" }}
"""
        cleaned, warnings = _clean_stale_local_patches(config)
        assert len(warnings) == 1
        assert "nonexistent" in warnings[0]
        assert 'kirino' not in cleaned
        assert 'aoba' in cleaned

    def test_ignores_relative_and_git_patches(self):
        from celestia_devtools.repo.register_patches import _clean_stale_local_patches

        config = """[patch.crates-io]
lib = { path = "./packages/lib" }

[patch."https://github.com/org/repo.git"]
crate = { git = "https://github.com/org/repo" }
"""
        cleaned, warnings = _clean_stale_local_patches(config)
        assert len(warnings) == 0
        assert './packages/lib' in cleaned
        assert 'github.com/org/repo' in cleaned

    def test_leaves_intact_when_nothing_stale(self):
        from celestia_devtools.repo.register_patches import _clean_stale_local_patches

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            p = Path(td)
            (p / "Cargo.toml").write_text("[package]\nname = \"test\"\n")
            config = f"""[patch.crates-io]
test = {{ path = \"{td}\" }}
"""
            cleaned, warnings = _clean_stale_local_patches(config)
            assert len(warnings) == 0
            assert 'test' in cleaned
