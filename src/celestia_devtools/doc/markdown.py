#!/usr/bin/env python3
"""Repository Markdown formatter.

This script is stdlib-only so it can run as part of `just fmt`.
It fixes the repository's recurring Markdown problems, especially TOML
frontmatter fenced by ``+++`` and common markdownlint issues.

Language-aware checks (code-fence inference, i18n duplicate-paragraph
detection, tab linting, markdownlint bridge) live in :mod:`celestia_devtools.doc.linter`.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from celestia_devtools.doc.linter import (
    check_duplicate_paragraphs,
    check_tabs,
    infer_fence_language,
    maybe_run_markdownlint,
)


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "fixtures",
    "node_modules",
    "target",
}

HEADING_RE = re.compile(r"^#{1,6}\s+")
HEADING_NO_SPACE_RE = re.compile(r"^(#{1,6})([^\s#].*)$")
ORDERED_ITEM_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
UNORDERED_ITEM_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)([^`]*)$")
TABLE_RE = re.compile(r"^\|.*\|\s*$")
TABLE_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")
BLOCKQUOTE_RE = re.compile(r"^>\s?")
CAMEL_CODE_RE = re.compile(
    r"(?<![`\w])([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[a-z]+(?:[A-Z][a-z0-9]+)+)(?![`\w])")
SNAKE_CODE_RE = re.compile(r"(?<![`\w.])([a-z]+(?:_[a-z0-9]+)+)(?![`\w.])")


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def read_lines(content: str) -> List[str]:
    return content.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def split_frontmatter(lines: Sequence[str]) -> Tuple[List[str], List[str]]:
    if not lines:
        return [], []
    # Skip leading blank lines so a front-matter fence that follows blank
    # lines (common after doc migrations) is still recognized — otherwise the
    # whole file, including the front matter, gets formatted and the TOML is
    # corrupted (e.g. `[[related_tools]]` -> `[[`related_tools`]]`).
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "+++":
        return [], list(lines)

    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "+++":
            frontmatter = [line.rstrip() for line in lines[start + 1:index]]
            while frontmatter and not frontmatter[0].strip():
                frontmatter.pop(0)
            while frontmatter and not frontmatter[-1].strip():
                frontmatter.pop()
            body = list(lines[index + 1:])
            while body and not body[0].strip():
                body.pop(0)
            return frontmatter, body

    return [], list(lines)


def classify_list(line: str) -> str | None:
    if ORDERED_ITEM_RE.match(line):
        return "ordered"
    if UNORDERED_ITEM_RE.match(line):
        return "unordered"
    return None


def normalize_ordered_marker(line: str) -> str:
    match = ORDERED_ITEM_RE.match(line)
    if not match:
        return line
    indent, rest = match.groups()
    return f"{indent}1. {rest}"


def ensure_blank_line(output: List[str]) -> None:
    if output and output[-1] != "":
        output.append("")


def should_start_list_with_blank(previous: str | None) -> bool:
    if previous is None or previous == "":
        return False
    if HEADING_RE.match(previous):
        return False
    if classify_list(previous):
        return False
    if BLOCKQUOTE_RE.match(previous):
        return False
    return True


def wrap_code_like_tokens(text: str) -> str:
    def replace_camel_token(match: re.Match[str]) -> str:
        token = match.group(1)
        if len(token) <= 10:
            return token
        return f"`{token}`"

    def replace_snake_token(match: re.Match[str]) -> str:
        token = match.group(1)
        return f"`{token}`"

    parts = text.split("`")
    for index in range(0, len(parts), 2):
        parts[index] = SNAKE_CODE_RE.sub(replace_snake_token, parts[index])
        parts[index] = CAMEL_CODE_RE.sub(replace_camel_token, parts[index])
    return "`".join(parts)


def fix_heading_spacing(line: str) -> str:
    match = HEADING_NO_SPACE_RE.match(line)
    if match:
        hashes, rest = match.groups()
        return f"{hashes} {rest}"
    return line


def normalize_table_separator(line: str) -> str:
    if not TABLE_SEPARATOR_RE.match(line):
        return line
    cells = [cell.strip() for cell in line.strip().split("|")]
    cells = [c for c in cells if c]
    normalized_cells = []
    for cell in cells:
        is_left_align = cell.startswith(":")
        is_right_align = cell.endswith(":")
        normalized_cells.append(
            f"{' ' if is_left_align else ''}{'---'}{' ' if is_right_align else ''}")
    return "| " + " | ".join(normalized_cells) + " |"


def postprocess_lines(lines: Sequence[str]) -> List[str]:
    result: List[str] = []
    in_fence = False

    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        line = fix_heading_spacing(line)
        stripped = line.strip()
        previous = result[-1] if result else None

        if FENCE_RE.match(line):
            if not in_fence:
                ensure_blank_line(result)
                result.append(line)
                in_fence = True
            else:
                result.append(line)
                in_fence = False
                next_non_empty = next(
                    (candidate.strip()
                     for candidate in lines[index + 1:] if candidate.strip()),
                    None,
                )
                if next_non_empty is not None:
                    ensure_blank_line(result)
            continue

        if in_fence:
            result.append(line)
            continue

        if not stripped:
            next_non_empty = next(
                (candidate.rstrip()
                 for candidate in lines[index + 1:] if candidate.strip()),
                None,
            )
            if previous and TABLE_RE.match(previous) and next_non_empty and TABLE_RE.match(next_non_empty):
                continue
            if previous and BLOCKQUOTE_RE.match(previous) and next_non_empty and BLOCKQUOTE_RE.match(next_non_empty):
                continue
            if result and result[-1] != "":
                result.append("")
            continue

        if HEADING_RE.match(line):
            ensure_blank_line(result)
            result.append(stripped)
            ensure_blank_line(result)
            continue

        list_kind = classify_list(line)
        if list_kind:
            if should_start_list_with_blank(previous):
                ensure_blank_line(result)
            normalized = wrap_code_like_tokens(normalize_ordered_marker(line))
            if previous and classify_list(previous) and classify_list(previous) != list_kind:
                ensure_blank_line(result)
            result.append(normalized)
            continue

        if TABLE_RE.match(line):
            if not previous or not TABLE_RE.match(previous):
                ensure_blank_line(result)
            result.append(normalize_table_separator(line.strip()))
            continue

        if BLOCKQUOTE_RE.match(line):
            next_non_empty = next(
                (candidate.rstrip()
                 for candidate in lines[index + 1:] if candidate.strip()),
                None,
            )
            if stripped == ">" and previous and BLOCKQUOTE_RE.match(previous) and next_non_empty and BLOCKQUOTE_RE.match(next_non_empty):
                continue
            if not previous or not BLOCKQUOTE_RE.match(previous):
                ensure_blank_line(result)
            result.append(wrap_code_like_tokens(line.strip()))
            continue

        if previous and classify_list(previous):
            ensure_blank_line(result)
        result.append(wrap_code_like_tokens(line.strip()))

    while result and result[-1] == "":
        result.pop()
    return result


def apply_fence_languages(lines: Sequence[str]) -> List[str]:
    output: List[str] = []
    index = 0
    in_fence = False

    while index < len(lines):
        line = lines[index]
        match = FENCE_RE.match(line)
        if not match:
            output.append(line)
            index += 1
            continue

        indent, marker, suffix = match.groups()
        suffix = suffix.strip()

        if in_fence:
            output.append(f"{indent}{marker}")
            in_fence = False
            index += 1
            continue

        if suffix:
            output.append(line.rstrip())
            in_fence = True
            index += 1
            continue

        block: List[str] = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if FENCE_RE.match(candidate):
                break
            block.append(candidate)
            index += 1

        language = infer_fence_language(block)
        output.append(f"{indent}{marker}{language}")
        output.extend(block)
        if index < len(lines):
            closing = FENCE_RE.match(lines[index])
            if closing:
                closing_indent, closing_marker, _ = closing.groups()
                output.append(f"{closing_indent}{closing_marker}")
            else:
                output.append(lines[index].rstrip())
            index += 1

    return output


def format_markdown(content: str) -> str:
    lines = read_lines(content)
    frontmatter, body = split_frontmatter(lines)
    formatted_body = postprocess_lines(apply_fence_languages(body))

    result: List[str] = []
    if frontmatter:
        result.append("+++")
        result.extend(frontmatter)
        result.append("+++")
        if formatted_body:
            result.append("")

    result.extend(formatted_body)

    while result and result[-1] == "":
        result.pop()
    return "\n".join(result) + "\n"


def iter_markdown_files(target: Path, pattern: str) -> Iterable[Path]:
    if target.is_file():
        if target.suffix == ".md":
            yield target
        return

    for path in target.rglob(pattern):
        if path.is_file() and path.suffix == ".md" and not is_excluded(path):
            yield path


def process_file(file_path: Path, check_only: bool) -> bool:
    if file_path.is_symlink() and not file_path.exists():
        return False  # broken symlink — skip
    original = file_path.read_text(encoding="utf-8")
    formatted = format_markdown(original)
    if original == formatted:
        return False
    if not check_only:
        file_path.write_text(formatted, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Format Markdown files and run lint checks")
    parser.add_argument("target", help="file or directory to format")
    parser.add_argument("pattern", nargs="?", default="*.md",
                        help="glob pattern when target is a directory")
    parser.add_argument("--check", action="store_true",
                        help="report files that would change (no writes)")
    parser.add_argument("--use-markdownlint", action="store_true",
                        help="optionally run markdownlint-cli2 if it is installed")
    args = parser.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        return 1
    paths: list[Path] = []
    if target.is_dir():
        paths = [p for p in target.rglob("*.md") if not is_excluded(p)]
    else:
        paths = [target]

    # ── Format ──
    changed: list[Path] = []
    for path in paths:
        if process_file(path, args.check):
            changed.append(path)
    if not args.check:
        for path in changed:
            print(f"Formatted: {path}")
        print(f"\nFormatted {len(changed)} file(s)")

    if args.use_markdownlint:
        maybe_run_markdownlint(target, args.check)

    # ── Lint: tab characters ──
    tab_warnings = check_tabs(paths)
    if tab_warnings:
        print(f"\n⚠  Tab characters found in {len(tab_warnings)} location(s):", file=sys.stderr)
        for w in tab_warnings:
            print(w, file=sys.stderr)

    # ── Lint: duplicate paragraphs across languages ──
    root = target if target.is_dir() else target.parent
    dup_warnings = check_duplicate_paragraphs(paths, root)
    if dup_warnings:
        pairs = len(dup_warnings) // 2
        print(f"\n⚠  {pairs} untranslated paragraph(s) detected "
              f"(identical text across language variants):", file=sys.stderr)
        for w in dup_warnings:
            print(w, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
