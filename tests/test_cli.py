"""Tests for CLI dispatch (core/cli.py)."""

import re
import subprocess
import sys
from importlib import import_module
import pytest
from celestia_devtools.core import cli
from celestia_devtools.core.cli import COMMANDS, main
import tomllib
from pathlib import Path
from celestia_devtools.repo.register_patches import _clean_stale_local_patches
import os
import tempfile

class test_command_registry:
    def test_all_commands_registered(self):
        # The full set of commands the dispatcher knows about. Update this set
        # whenever a command is added or removed in core/cli.py — keeping it an
        # explicit literal (rather than set(COMMANDS)) guards against accidental
        # removals of a command that downstream justfiles depend on.
        # Added 2026-09-20: these four shipped console scripts but had no
        # dispatcher command, so `celestia-devtools <cmd>` could not reach them.
        # release-notes is already listed above (line 29); the
        # reverse-direction test below pins the whole set either way.
        # Added 2026-09-20: production-target lifecycle group (deploy doctor;
        # the remaining subcommands land with their own slices and fail loudly).
        # Added 2026-10-22: local branch hygiene sweeper.
        # Added 2026-10-25: custom layout checkers for the org style rules.
        # Each ships a RULES catalog (check/list-rules); Python also has a
        # safe fix mode used by the migration follow-up.
        # Added 2026-09-25: family dependency-identity check. The audit found
        # kirino at two incompatible majors, plana on seven revisions and
        # hikari declared unbounded — nothing compared how the repositories
        # consume the shared layers.
        # Added 2026-10-16: dev-mode gateway environment — the DEBUG_* +
        # per-repo variable set for a run profile, with an injecting
        # launcher (replaces the abandoned hosts-file hostmap approach).
        # Added 2026-10-17: per-app vite/CDP port allocation — two
        # tauri debug shells on one machine must never cross-wire.
        # Added 2026-10-25: workspace tool-exemption + canonical-caller audits.
        # Added 2026-10-25: workspace tool-exemption audit (SR-32) — the ledger
        # had no reader, so unregistered .sh/.ps1 scripts accumulated.
        # Added 2026-10-28: standard desktop debug channel driver (WebView2 CDP).
        expected = {"cache-guard", "format-markdown", "prefetch", "check-cross-deps", "npm-dist", "preflight", "wsl-ensure", "qemu-ensure", "pglite", "serve", "locate", "register-patches", "init", "commit-msg-lint", "hook", "pr-merge", "gh", "publish-crates", "daemon", "mock-start", "mock-status", "mock-stop", "registry", "toml-sort", "sign-agent", "gate", "verify-versions", "link-npm-siblings", "protocol-bundle", "nav-lint", "p0-gate", "rules-lint", "fetch-just", "build-dispatch", "upstream-sync", "worktree-create", "worktree-remove", "dev-watch", "vite-build", "vite-serve", "vite-dev", "npm-release", "release-notes", "cargo-cache-guard", "lint-separators", "type-escapes", "ts-policy", "ts-surface", "job-timeouts", "ci-audit", "tool-audit", "fleet-audit", "ci-cache", "deploy", "git-sweep", "py-style", "cpp-style", "e2e-sandbox", "family-versions", "version-string", "devenv", "ports", "brand-stamp", "app-drive", "mobile", "dependency-radar"}
        assert set(COMMANDS.keys()) == expected

    def test_every_console_script_has_a_dispatcher_command(self):
        """The reverse direction: a console script nobody can reach through the CLI.

        `test_all_commands_registered` catches removals from `COMMANDS`, but nothing caught
        *omissions* — which is how four entry points sat unreachable. The convention below
        (strip the `celestia-` prefix the package adds) is what the missing four follow.
        """

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding = "utf-8"))
        scripts = data["project"]["scripts"]

        for name, target in scripts.items():
            if name in ("celestia-devtools",):
                continue

            short = name[len("celestia-"):] if name.startswith("celestia-") else name
            assert (short in COMMANDS) or (name in COMMANDS), (f"console script '{name}' ({target}) has no `celestia-devtools` command; " "add it to COMMANDS in core/cli.py or the unified CLI cannot run it")

    @pytest.mark.parametrize("cmd,module_path", list(COMMANDS.items()))
    def test_command_modules_importable(self, cmd, module_path):
        """Every registered command must resolve to an importable module with main().

        Some master-added commands are POSIX-only by design (deploy's bootstrap
        imports `pwd`, e2e-sandbox relies on `signal.SIGKILL`); importing them
        on Windows raises ModuleNotFoundError/AttributeError before main() is
        ever reached, so the importability contract only applies on POSIX.
        """

        if (sys.platform == "win32") and (cmd in ("deploy", "e2e-sandbox")):
            pytest.skip(f"{cmd} is POSIX-only (imports pwd / signal.SIGKILL)")

        # py-style-ok: no-dynamic-imports: exercises the documented lazy-dispatcher exception (see core/cli.py)
        mod = import_module(module_path)
        assert callable(getattr(mod, "main", None)), f"{cmd} -> {module_path} has no main()"

    _HELP_ENTRY = re.compile(r"^    ([a-z][a-z0-9-]*)\s{2,}\S")

    def _doc_commands(self) -> set:
        """Command names listed in the usage docstring.

        Continuation lines are indented past the first four spaces, so the
        anchored pattern only matches real `<name>  <description>` entries.
        """

        return {match.group(1) for match in (self._HELP_ENTRY.match(line) for line in (cli.__doc__ or "").splitlines()) if match}

    def test_help_covers_the_registry(self):
        """The usage docstring and COMMANDS must not drift apart.

        Both halves of that drift had already happened by 2026-10-31: sixteen
        registered commands (the whole mock/deploy group, every CI audit tool,
        the version-string/toml-sort pair, four console-script spellings) had
        no help entry, while `include-path` lived only in the docstring because
        it is dispatched inline. `--help` is the only map a human ever sees and
        no CI gate reads it, so the two lists are compared mechanically here:
        every command must have exactly one help entry and vice versa.
        """

        # include-path is dispatched inline in main() (lazy import, see the
        # file header) rather than through COMMANDS, so it belongs to the help
        # surface without being a registry key.
        expected = set(COMMANDS) | {"include-path"}
        documented = self._doc_commands()

        missing = expected - documented
        unknown = documented - expected
        assert not missing, f"registered commands with no --help entry: {sorted(missing)}"
        assert not unknown, f"--help entries that match no registered command: {sorted(unknown)}"

    def test_help_entries_are_single_line_names(self):
        """Guard the guard: the parser must see the docstring it claims to parse.

        An extraction that can never match anything passes vacuously (the
        zero-yield trap), so the same regex is proven against a known-positive
        snippet shaped exactly like a help entry.
        """

        assert self._HELP_ENTRY.match("    cache-guard        Manage cargo target/ disk usage")
        assert self._HELP_ENTRY.match("    app-drive          Drive a Tauri desktop debug build over WebView2 CDP:")
        assert not self._HELP_ENTRY.match("                       (check | fix | list-rules)")
        assert not self._HELP_ENTRY.match("    celestia-devtools <command> [args...]")

class test_version_help:
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

class test_unknown_command:
    def test_unknown_returns_error(self, capsys):
        rc = main(["nonexistent-command"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "unknown command" in captured.err

class test_include_path:
    def test_include_path_prints_common_just(self, capsys):
        rc = main(["include-path"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "common.just" in captured.out

class test_module_run:
    def test_python_m_invocation(self):
        """`python -m celestia_devtools --version` should work."""
        result = subprocess.run([sys.executable, "-m", "celestia_devtools", "--version"], capture_output = True, text = True)
        assert result.returncode == 0
        assert "celestia-devtools" in result.stdout

class test_stale_local_patch_cleanup:
    def test_removes_stale_absolute_path(self):
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
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "Cargo.toml").write_text("[package]\nname = \"test\"\n")
            config = f"""[patch.crates-io]
test = {{ path = \"{td}\" }}
"""
            cleaned, warnings = _clean_stale_local_patches(config)
            assert len(warnings) == 0
            assert 'test' in cleaned

class test_module_main_guard:
    """`python3 -m celestia_devtools.core.cli …` must actually dispatch.

    Before the `__main__` guard, `-m` imported the module, exited 0, and ran
    nothing — the silent codegen no-op behind chest #1028 (see the
    known-env-issues ledger). These tests pin both directions: a real command
    produces output, and an unknown command fails loudly instead of exiting
    green with nothing done.
    """

    def _run_module(self, *args):
        # Resolve the module from THIS checkout (src/ layout), not whatever
        # celestia_devtools happens to be pip-installed on the host — otherwise
        # the test would exercise a stale copy and drift with it.
        src = Path(__file__).resolve().parents[1] / "src"
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{src}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src)
        return subprocess.run([sys.executable, "-m", "celestia_devtools.core.cli", *args], capture_output = True, text = True, timeout = 60, env = env)

    def test_module_run_dispatches_a_real_command(self):
        result = self._run_module("--version")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("celestia-devtools "), (f"-m run produced no version output: stdout={result.stdout!r}")

    def test_module_run_fails_loudly_on_unknown_command(self):
        result = self._run_module("definitely-not-a-command")
        assert result.returncode == 2, result.stderr
        assert "unknown command" in result.stderr, result.stderr

class test_lazy_dispatch:
    """Module scope must not import a command package before dispatch.

    `core/cli.py` opens with a file-level exemption stating that this module is
    a deliberate lazy plugin loader. Nothing asserted it, so it stopped being
    one: a module-scope ``from celestia_devtools.repo.init import
    common_just_path`` — needed by the single ``include-path`` command — pulled
    that package's PyYAML dependency into every invocation, 42.6 ms even for
    ``--version``. The comment stayed green while the behaviour drifted.
    """

    def test_module_scope_imports_no_command_package(self):
        src = Path(__file__).resolve().parents[1] / "src"
        # The probe reports whether the dispatcher itself loaded, so an import
        # failure cannot masquerade as "nothing extra was imported".
        # Guard derived from the registry, not a hand-typed prefix: COMMANDS is
        # the exact set the dispatcher promises not to import eagerly, so a new
        # command package is covered the day it is registered.
        guard = " or ".join("m == %r" % module for module in sorted(set(COMMANDS.values())))
        script = "\n".join(["import sys, celestia_devtools, celestia_devtools.core.cli", "cli = 'celestia_devtools.core.cli' in sys.modules", "extra = ','.join(sorted(m for m in sys.modules if %s or m == 'yaml'))" % guard, "print(cli, celestia_devtools.__file__, extra, sep = '|')"])
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{src}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src)
        result = subprocess.run([sys.executable, "-c", script], capture_output = True, text = True, timeout = 60, env = env)
        assert result.returncode == 0, result.stderr
        # Fields: the probe ran, WHICH COPY it imported, and what leaked. The
        # middle field keeps the last assertion from passing vacuously when the
        # probe silently resolves to the installed package instead of the tree.
        loaded, module_file, extra = result.stdout.strip().split("|", 2)
        assert loaded == "True", f"the probe never imported the dispatcher: stdout={result.stdout!r}"
        assert Path(module_file).resolve().is_relative_to(src.resolve()), f"the probe imported {module_file}, not this checkout"
        assert extra == "", f"module scope pulled command packages back in: {extra}"
