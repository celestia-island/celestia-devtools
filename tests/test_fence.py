"""Tests for code-fence programming-language inference (doc/linter/fence.py)."""

import pytest

from celestia_devtools.doc.linter.fence import infer_fence_language


@pytest.mark.parametrize("lines,expected", [
    # Python
    (["def main():", "    pass"], "python"),
    (["async def fetch():", "    return 1"], "python"),
    (["class Foo(Bar):", "    pass"], "python"),
    (["from pathlib import Path"], "python"),
    (["import os", "raise ValueError()"], "python"),

    # TypeScript / JavaScript
    (["const x: number = 1;"], "typescript"),
    (["interface Foo {", "  bar: string;", "}"], "typescript"),
    (["type Result<T> = { ok: T }"], "typescript"),
    (["export default function() {}"], "typescript"),

    # SQL
    (["SELECT * FROM users;"], "sql"),
    (["INSERT INTO t VALUES (1);"], "sql"),
    (["WITH cte AS (SELECT 1) SELECT * FROM cte;"], "sql"),

    # Mermaid
    (["graph TD", "  A --> B"], "mermaid"),
    (["flowchart LR", "  X --> Y"], "mermaid"),
    (["sequenceDiagram", "  A->>B: Hi"], "mermaid"),

    # TOML
    (["[package]", 'name = "x"'], "toml"),
    (['name = "celestia"', 'version = "0.1"'], "toml"),

    # Bash / shell
    (["cargo build --release"], "bash"),
    (["$ pip install -e ."], "bash"),
    (["just cache-guard", "cargo build"], "bash"),

    # JSON
    (['{"key": "value", "n": 42}'], "json"),

    # Text / fallback
    (["┌─────────┐", "│  Box   │", "└─────────┘"], "text"),
    (["Just some prose without code markers."], "text"),
    ([], "text"),
])
def test_infer_fence_language(lines, expected):
    assert infer_fence_language(lines) == expected
