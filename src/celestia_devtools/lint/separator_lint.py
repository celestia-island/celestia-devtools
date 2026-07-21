#!/usr/bin/env python3
"""Lint and fix decorative separator patterns and merge-conflict markers.

*Convention:*

- Decorative comment separators such as  ``// =========...``  are replaced
  with exactly six dashes on each side::

      // ============================================================
      // ==================== Public API ====================

  becomes::

      // ------
      // ------ Public API ------

- The same rule applies to  ``/* ... */``,  ``#``, and  ``<!-- ... -->``
  comments, as well as to decorative ``=====`` inside string/byte-string
  literals (e.g.  ``eprint!("========= Expected: …")``).

- Stray Git merge-conflict markers (``<<<<<<< HEAD``, ``=======``,
  ``>>>>>>> …``) are also detected.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Regex for comments containing ==== separators.
# Group 1: prefix before the ==== (comment chars + optional space/comment-start)
# Group 2: the ==== content, optionally with a label in the middle.
# Minimum 5 sequential = to avoid matching JS === operators.
_COMMENT_SEP_RE = re.compile(
    r"^(\s*(?://|#|/\*|<!--)\s*)"                        # comment prefix
    r"(={5,}(?:\s+.+?\s+={5,})?)\s*"                     # ===...=== or ===... label ...===
    r"(\s*(?:\*/|-->)?)$",                                # optional closing
)

# Regex for pure ==== lines inside string/byte literals.
_STRING_SEP_RE = re.compile(
    r'(")(={5,})(")',                                     # "...===..."
)

# Regex for ===== label ===== inside string literals.
_STRING_LABEL_SEP_RE = re.compile(
    r'(")(={5,})\s+(.+?)\s+(={5,})(")',                   # "=== label ==="
)

# Regex for the inner label part when already inside quotes.
_STRING_LABEL_INNER_RE = re.compile(
    r"(={5,})\s+(.+?)\s+(={5,})",
)

# Merge-conflict markers.
_CONFLICT_MARKER_RE = re.compile(
    r"^(<{7}\s|={7}$|>{7}\s)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DASH_LINE = "------"
_DASH_WRAP = lambda text: f"------ {text} ------"


def _replace_comment_sep(m: re.Match) -> str:
    prefix = m.group(1)
    content = m.group(2)
    suffix = m.group(3) or ""
    # Check if there is a label between ===...===
    inner = re.match(r"^={5,}\s+(.+?)\s+={5,}$", content)
    if inner:
        return f"{prefix}{_DASH_WRAP(inner.group(1))}{suffix}"
    return f"{prefix}{_DASH_LINE}{suffix}"


def _replace_string_sep(m: re.Match) -> str:
    return f'{m.group(1)}{_DASH_LINE}{m.group(3)}'


def _replace_string_label_sep(m: re.Match) -> str:
    return f'{m.group(1)}{_DASH_WRAP(m.group(3))}{m.group(5)}'


def _fix_line(line: str) -> str:
    line, n1 = _COMMENT_SEP_RE.subn(_replace_comment_sep, line)
    if n1:
        return line
    line, n2 = _STRING_LABEL_SEP_RE.subn(_replace_string_label_sep, line)
    if n2:
        return line
    line, n3 = _STRING_SEP_RE.subn(_replace_string_sep, line)
    if n3:
        return line
    # Also handle bare ===== inside strings (not wrapped in quotes).
    # This catches e.g. eprintln!("========= Expected:\n{scenario}");
    # which already has ===== inside a quoted string matched by _STRING_SEP_RE
    # or _STRING_LABEL_SEP_RE.  For strings that span multiple lines or use
    # raw strings, try the inner-label pattern.
    line, n4 = _STRING_LABEL_INNER_RE.subn(
        lambda m: _DASH_WRAP(m.group(2)), line,
    )
    return line


def _has_conflict_marker(line: str) -> bool:
    return bool(_CONFLICT_MARKER_RE.match(line))


# ---------------------------------------------------------------------------
# File-level processing
# ---------------------------------------------------------------------------

def lint_file(path: Path) -> Tuple[List[int], List[int]]:
    """Check *path* for issues.

    Returns ``(separator_lines, conflict_lines)`` — two lists of 1‑based line
    numbers where each kind of problem occurs.
    """
    sep_lines: List[int] = []
    conflict_lines: List[int] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return sep_lines, conflict_lines

    # Skip binary files quickly.
    if "\0" in text:
        return sep_lines, conflict_lines

    for i, line in enumerate(text.splitlines(), 1):
        fixed = _fix_line(line)
        if fixed != line:
            # Only report if the original line actually contains ====
            if "===" in line:
                sep_lines.append(i)
        if _has_conflict_marker(line):
            conflict_lines.append(i)
    return sep_lines, conflict_lines


def fix_file(path: Path, dry_run: bool = False) -> bool:
    """Rewrite *path* in-place, fixing all separator issues.

    Returns ``True`` if the file was (or would be) changed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if "\0" in text:
        return False

    new_lines: List[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n\r")
        fixed = _fix_line(stripped)
        if fixed != stripped and "===" in stripped:
            changed = True
            new_lines.append(fixed + line[len(stripped):])
        else:
            new_lines.append(line)

    if not changed:
        return False

    if not dry_run:
        path.write_text("".join(new_lines), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Repository scanner
# ---------------------------------------------------------------------------

def _is_binary_path(path: Path) -> bool:
    BINARY_EXTENSIONS = frozenset({
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".gz", ".bz2", ".xz", ".zst",
        ".o", ".a", ".so", ".dylib", ".dll", ".exe",
        ".pyc", ".pyo",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".zip", ".tar",
    })
    return path.suffix.lower() in BINARY_EXTENSIONS


def _iter_source_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        parts = set(rel.parts)
        if parts & {".git", "node_modules", "target", "vendor", ".pytest_cache",
                     ".ruff_cache", ".just", "__pycache__"}:
            continue
        if _is_binary_path(path):
            continue
        yield path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools lint-separators",
        description="Lint/fix decorative ==== separators and merge-conflict markers.",
    )
    parser.add_argument(
        "paths", nargs="+",
        help="File or directory paths to scan",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Apply fixes in-place (default: dry-run / check only)",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Alias for the default: report issues without modifying files",
    )
    parser.add_argument(
        "--conflict-markers", action="store_true",
        help="Also flag (but cannot auto-fix) merge-conflict markers",
    )

    args = parser.parse_args()

    all_sep_lines: List[str] = []
    all_conflict_lines: List[str] = []
    returncode = 0

    for raw in args.paths:
        p = Path(raw).resolve()
        if not p.exists():
            print(f"error: {raw} does not exist", file=sys.stderr)
            returncode = 1
            continue

        targets: List[Path] = []
        if p.is_dir():
            targets.extend(_iter_source_files(p))
        else:
            targets.append(p)

        for tgt in targets:
            sep_lines, conflict_lines = lint_file(tgt)
            if sep_lines:
                for ln in sep_lines:
                    all_sep_lines.append(f"{tgt}:{ln}")
            if conflict_lines and args.conflict_markers:
                for ln in conflict_lines:
                    all_conflict_lines.append(f"{tgt}:{ln}")

    if all_sep_lines:
        print("--- decorative ==== separators ---", file=sys.stderr)
        for item in all_sep_lines:
            print(item, file=sys.stderr)
        returncode = 1

    if all_conflict_lines:
        print("--- merge-conflict markers ---", file=sys.stderr)
        for item in all_conflict_lines:
            print(item, file=sys.stderr)
        returncode = 1

    if args.fix and all_sep_lines:
        fixed_count = 0
        for raw in args.paths:
            p = Path(raw).resolve()
            if not p.exists():
                continue
            targets: List[Path] = []
            if p.is_dir():
                targets.extend(_iter_source_files(p))
            else:
                targets.append(p)
            for tgt in targets:
                if fix_file(tgt, dry_run=False):
                    fixed_count += 1
        print(f"Fixed {fixed_count} file(s).", file=sys.stderr)

    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
