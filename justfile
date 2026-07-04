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

# Verify all modules import and CLI responds.
test:
    {{python_cmd}} -c "from celestia_devtools import cli, logger, cache_guard, format_markdown, prefetch, check_cross_deps, locate, init_repo; print('imports ok')"
    {{ _devtools }} --help > /dev/null
    {{ _devtools }} --version
    {{ _devtools }} include-path

# Format Markdown + verify the bundled common.just is valid just syntax.
fmt:
    {{ _devtools }} format-markdown .
    just --evaluate _devtools > /dev/null

clean:
    rm -rf build/ dist/ *.egg-info src/*.egg-info
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
