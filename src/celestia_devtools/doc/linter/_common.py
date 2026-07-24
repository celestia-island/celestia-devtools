"""Shared helpers for the linter subpackage."""

from __future__ import annotations

from pathlib import Path


def safe_read(path: Path) -> str | None:
    """Read file text, returning None for broken symlinks or read errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
