"""Code-fence programming-language inference.

Heuristics that detect the language of a fenced code block from its content
so the formatter can attach the right info-string (```python, ```sql, …).
"""

from __future__ import annotations

from typing import Sequence


def infer_fence_language(block_lines: Sequence[str]) -> str:
    content = "\n".join(block_lines).strip()
    if not content:
        return "text"

    first = next((line.strip() for line in block_lines if line.strip()), "")
    upper = first.upper()

    if any(first.startswith(prefix) for prefix in (
        "graph ",
        "flowchart ",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "erDiagram",
        "journey",
        "gantt",
        "pie ",
    )):
        return "mermaid"
    if upper.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "WITH ")):
        return "sql"
    if first.startswith(("def ", "async def ", "class ", "import ", "from ")) or "raise " in content:
        return "python"
    if first.startswith(("type ", "interface ", "const ", "let ", "export ")) or "=>" in content:
        return "typescript"
    if first.startswith(("[", "name =", "description =", "agent =", "namespace =")) and "=" in content:
        return "toml"
    if first.startswith(("$ ", "cargo ", "just ", "docker ", "git ", "python ", "python3 ", "npm ", "pnpm ", "yarn ", "cp ", "mv ", "rm ", "export ")):
        return "bash"
    if any(char in content for char in "┌┐└┘├┤┬┴│─↓↑→←●○✓!"):
        return "text"
    if "{" in content and "}" in content and ":" in content:
        return "json"
    return "text"
