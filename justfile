# celestia-devtools — shared build/devtool scripts for celestia-island.
# This justfile self-hosts: it imports its own common.just to demonstrate
# the pattern that consumer repos follow.

import "./src/celestia_devtools/common.just"

set shell := ["bash", "-c"]

default:
    @just --list

# Optional: install the CLI entry point (pip install -e .) for convenience.
install:
    {{python_cmd}} -m pip install -e .

# Verify all modules import and CLI responds, then run pytest.
test:
    {{python_cmd}} -c "from celestia_devtools.core import cli, logger; from celestia_devtools.build import cache_guard, cross_deps, prefetch; from celestia_devtools.repo import locate, init; from celestia_devtools.doc import markdown; from celestia_devtools.doc.linter import fence, i18n, tabs, external; print('imports ok')"
    {{ _devtools }} --help > /dev/null
    {{ _devtools }} --version
    {{ _devtools }} include-path
    {{python_cmd}} -m pytest tests/ -v

# Lint with ruff.
lint:
    {{python_cmd}} -m ruff check src/ tests/

# Format Markdown + verify the bundled common.just is valid just syntax.
fmt:
    {{ _devtools }} format-markdown .
    just --evaluate _devtools > /dev/null

clean:
    rm -rf build/ dist/ *.egg-info src/*.egg-info
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
