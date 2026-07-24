"""Tab-character lint — warn about tabs that break Mermaid rendering."""

from __future__ import annotations

from pathlib import Path

from celestia_devtools.doc.linter._common import safe_read


def check_tabs(paths: list[Path]) -> list[str]:
    """Warn about tab characters in markdown files."""
    warnings: list[str] = []
    for path in paths:
        text = safe_read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "\t" in line:
                warnings.append(
                    f"  {path}:{lineno} contains tab character(s) — replace with spaces "
                    f"(tabs break Mermaid diagram rendering)"
                )
    return warnings
