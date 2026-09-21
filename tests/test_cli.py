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
            "registry", "toml-sort", "sign-agent", "gate", "verify-versions",
            "link-npm-siblings", "protocol-bundle", "nav-lint", "p0-gate",
            "rules-lint",
            "fetch-just", "build-dispatch", "upstream-sync",
            "worktree-create", "worktree-remove", "dev-watch",
            "vite-build", "vite-serve", "vite-dev", "npm-release",
            "release-notes",
            # Added 2026-09-20: these four shipped console scripts but had no
            # dispatcher command, so `celestia-devtools <cmd>` could not reach them.
            "cargo-cache-guard", "lint-separators", "job-timeouts", "ci-audit",
            "ci-cache",
            # Also missing from this literal while present in COMMANDS: the release-notes
            # generator. The new reverse-direction test below is what pins the whole set.
            "release-notes",
            # Added 2026-09-20: production-target lifecycle group (deploy doctor;
            # the remaining subcommands land with their own slices and fail loudly).
            "deploy",
            "e2e-sandbox",
        }
        assert set(COMMANDS.keys()) == expected

    def test_every_console_script_has_a_dispatcher_command(self):
        """The reverse direction: a console script nobody can reach through the CLI.

        `test_all_commands_registered` catches removals from `COMMANDS`, but nothing caught
        *omissions* — which is how four entry points sat unreachable. The convention below
        (strip the `celestia-` prefix the package adds) is what the missing four follow.
        """
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        for name, target in scripts.items():
            if name in ("celestia-devtools",):
                continue
            short = name[len("celestia-"):] if name.startswith("celestia-") else name
            assert short in COMMANDS or name in COMMANDS, (
                f"console script '{name}' ({target}) has no `celestia-devtools` command; "
                "add it to COMMANDS in core/cli.py or the unified CLI cannot run it"
            )

    @pytest.mark.parametrize("cmd,module_path", list(COMMANDS.items()))
    def test_command_modules_importable(self, cmd, module_path):
        """Every registered command must resolve to an importable module with main().

        Some master-added commands are POSIX-only by design (deploy's bootstrap
        imports `pwd`, e2e-sandbox relies on `signal.SIGKILL`); importing them
        on Windows raises ModuleNotFoundError/AttributeError before main() is
        ever reached, so the importability contract only applies on POSIX.
        """
        import sys

        from importlib import import_module

        if sys.platform == "win32" and cmd in ("deploy", "e2e-sandbox"):
            pytest.skip(f"{cmd} is POSIX-only (imports pwd / signal.SIGKILL)")
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
        import tempfile
        from pathlib import Path
        bad = os.path.abspath(os.path.join(os.sep, "nonexistent", "abcdef", "crate"))
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "Cargo.toml").write_text("[package]\nname = \"ok\"\n")
            config = f"""[patch.crates-io]
kirino = {{ path = "{bad}" }}
ok = {{ path = "{td}" }}
"""
            cleaned, warnings = _clean_stale_local_patches(config)
            assert len(warnings) == 1
            assert "nonexistent" in warnings[0]
            assert 'kirino' not in cleaned
            assert 'ok' in cleaned

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


class TestModuleMainGuard:
    """`python3 -m celestia_devtools.core.cli …` must actually dispatch.

    Before the `__main__` guard, `-m` imported the module, exited 0, and ran
    nothing — the silent codegen no-op behind chest #1028 (see the
    known-env-issues ledger). These tests pin both directions: a real command
    produces output, and an unknown command fails loudly instead of exiting
    green with nothing done.
    """

    def _run_module(self, *args):
        import os
        import subprocess
        import sys
        from pathlib import Path
        # Resolve the module from THIS checkout (src/ layout), not whatever
        # celestia_devtools happens to be pip-installed on the host — otherwise
        # the test would exercise a stale copy and drift with it.
        src = Path(__file__).resolve().parents[1] / "src"
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            f"{src}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src)
        )
        return subprocess.run(
            [sys.executable, "-m", "celestia_devtools.core.cli", *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_module_run_dispatches_a_real_command(self):
        result = self._run_module("--version")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("celestia-devtools "), (
            f"-m run produced no version output: stdout={result.stdout!r}"
        )

    def test_module_run_fails_loudly_on_unknown_command(self):
        result = self._run_module("definitely-not-a-command")
        assert result.returncode == 2, result.stderr
        assert "unknown command" in result.stderr, result.stderr
