# celestia-devtools — shared build/devtool scripts for celestia-island.
# This justfile self-hosts: it imports its own common.just to demonstrate
# the pattern that consumer repos follow.

set shell := ["bash", "-c"]
# Windows: PowerShell (the 5.1 floor ships with every Windows; pwsh 7 is
# NOT assumed). Linewise recipes must stay PS-5.1-safe: no `&&` chains,
# no `> /dev/null` (PowerShell would write a literal file), one command
# per line. No script-interpreter / [script] bodies — bash is banned.
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $PSDefaultParameterValues['*:Encoding']='utf8';"]
set unstable
set lists

# Repo definitions override the shared template's (imported above).
set allow-duplicate-recipes
set allow-duplicate-variables

import "./src/celestia_devtools/common.just"

default:
    @just --list

# Optional: install the CLI entry point (pip install -e .) for convenience.
install:
    {{python_cmd}} -m pip install -e .

# Verify all modules import and CLI responds, then run pytest.
test:
    {{python_cmd}} -c "from celestia_devtools.core import cli, logger, scheduler; from celestia_devtools.build import cache_guard, cross_deps, prefetch, gate, dispatch; from celestia_devtools.repo import locate, init, fetch_just; from celestia_devtools.doc import markdown; from celestia_devtools.doc.linter import fence, i18n, tabs, external; from celestia_devtools.lint import nav_lint, p0_gate, rules_lint; from celestia_devtools.vcs import upstream, worktree; from celestia_devtools.env import dev_watch, vite; from celestia_devtools.npm import release; print('imports ok')"
    {{ _devtools }} --help
    {{ _devtools }} --version
    {{ _devtools }} include-path
    {{ _devtools }} gate --list
    {{ _devtools }} p0-gate --list
    {{python_cmd}} -m pytest tests/ -v

# Lint with ruff.
lint:
    {{python_cmd}} -m ruff check src/ tests/

# Format Markdown + verify the bundled common.just is valid just syntax.
fmt:
    {{ _devtools }} format-markdown .
    just --evaluate _devtools

clean:
    {{python_cmd}} -c "import shutil, pathlib; targets = [pathlib.Path('build'), pathlib.Path('dist'), *pathlib.Path('.').glob('*.egg-info'), *pathlib.Path('src').glob('*.egg-info')]; [shutil.rmtree(p, ignore_errors=True) for p in targets]; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
