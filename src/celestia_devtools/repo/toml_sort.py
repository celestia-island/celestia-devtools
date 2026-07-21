#!/usr/bin/env python3
"""Sort Cargo.toml [workspace.dependencies] entries.

Ordering mirrors Rust `use` statement conventions:
  1. Internal / path deps         (workspace crates)
  2. Celestia-island ecosystem     (git deps + known crates.io packages)
  3. External third-party          (everything else)

Within each group entries are sorted alphabetically by key.
Comments immediately preceding an entry stay attached to it.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

# ── Known celestia-island packages published to crates.io ──────────────────
CELESTIA_CRATES: frozenset[str] = frozenset(
    {"kirino", "kirino-session", "kirino-macro", "yuuka"}
)

# Section headers we recognise (order matters for rule matching).
SECTION_PATTERN: re.Pattern = re.compile(r"^\[(workspace\.)?(dependencies|dev-dependencies|build-dependencies)\]")
SECTION_END_PATTERN: re.Pattern = re.compile(r"^\[")


def _classify(key: str, value_str: str) -> int:
    """Return sort group: 0=internal, 1=celestia-ecosystem, 2=external."""
    if "path =" in value_str:
        return 0
    if "celestia-island" in value_str:
        return 1
    if key in CELESTIA_CRATES:
        return 1
    return 2


def _parse_section(lines: list[str], start: int) -> tuple[int, int]:
    """Return (header_line_index, end_line_index) for the section at *start*."""
    end = start + 1
    while end < len(lines):
        if SECTION_END_PATTERN.match(lines[end]):
            break
        end += 1
    return start, end


_KV_LINE: re.Pattern = re.compile(r"^(\S+)\s*=\s*")


def _is_multiline_value(first_line: str) -> str | None:
    """Return the opening delimiter if *first_line* starts a multi-line value."""
    stripped = first_line.rstrip()
    if stripped.endswith("{") and "}" not in stripped:
        return "{"
    if stripped.endswith("[") and "]" not in stripped:
        return "["
    # Triple-quoted string
    if '"""' in stripped:
        count = stripped.count('"""')
        # If count is 0, then it's a different line
        if count % 2 == 1:
            return '"""'
    return None


def _parse_entries(lines: list[str], body_start: int, body_end: int) -> list[tuple[int, int, str]]:
    """Parse key-value entries from a section body.

    Returns list of (class_group, sort_key_lower, entry_text) tuples,
    where entry_text includes any attached comment lines.
    """
    entries: list[tuple[int, int, str]] = []
    i = body_start
    while i < body_end:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue

        # Collect comment block preceding the entry
        comment_lines: list[str] = []
        while i < body_end and lines[i].strip().startswith("#"):
            comment_lines.append(lines[i])
            i += 1

        if i >= body_end:
            break
        if SECTION_END_PATTERN.match(lines[i]):
            break

        # Start of a key-value entry
        entry_lines: list[str] = list(comment_lines)
        entry_lines.append(lines[i])
        i += 1

        # Multi-line value detection
        delimiter = _is_multiline_value(lines[i - 1])
        if delimiter:
            closing = {"{": "}", "[": "]", '"""': '"""'}[delimiter]
            while i < body_end:
                entry_lines.append(lines[i])
                i += 1
                if lines[i - 1].rstrip().endswith(closing):
                    break
                if i >= body_end:
                    break
                # Safety: stop if we hit a top-level key (non-indented, non-continuation)
                next_line = lines[i] if i < body_end else ""
                if (
                    _KV_LINE.match(next_line)
                    and not next_line.startswith((" ", "\t"))
                    and not next_line.rstrip() == ""
                ):
                    break

        entry_text = "".join(entry_lines)
        # Extract key from first non-comment line
        for line in entry_lines:
            m = re.match(r"^(\S+)", line.lstrip())
            if m and not line.lstrip().startswith("#"):
                key = m.group(1)
                group = _classify(key, entry_text)
                entries.append((group, key.lower(), entry_text))
                break
    return entries


def sort_toml_deps(text: str) -> str:
    """Sort dependency sections in *text* and return the formatted result."""
    lines = io.StringIO(text).readlines()
    # Retain original line endings where possible
    original_line_endings = [l for l in lines if l.endswith("\n")]
    if not original_line_endings:
        return text  # empty file

    i = 0
    result_lines: list[str] = []
    while i < len(lines):
        line = lines[i]
        if SECTION_PATTERN.match(line):
            header, end = _parse_section(lines, i)
            header_line = lines[header]

            # Figure out where the body starts (skip blank line after header)
            body_start = header + 1
            while body_start < end and lines[body_start].strip() == "":
                body_start += 1

            entries = _parse_entries(lines, body_start, end)

            if entries:
                # Sort
                entries.sort(key=lambda e: (e[0], e[1]))

                # Rebuild: group by class, blank lines between groups only
                result_lines.append(header_line)
                result_lines.append("\n")
                prev_group = -1
                for idx, (group, _key, text) in enumerate(entries):
                     if prev_group >= 0 and group != prev_group:
                         result_lines.append("\n")
                     prev_group = group
                     # Strip trailing blank lines from entry text; we control spacing
                     text = text.rstrip("\n") + "\n"
                     result_lines.append(text)
            else:
                # Empty section — preserve as-is
                result_lines.extend(lines[header:end])

            i = end
        else:
            result_lines.append(line)
            i += 1

    return "".join(result_lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    check_only = False
    paths: list[str] = []
    for a in args:
        if a == "--check":
            check_only = True
        else:
            paths.append(a)

    if not paths:
        paths = ["."]

    modified = 0
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            candidates = list(p.rglob("Cargo.toml"))
        else:
            candidates = [p]

        for cargo_toml in candidates:
            # Skip target/ and .git/ directories
            if any(part in ("target", ".git") for part in cargo_toml.parts):
                continue
            try:
                original = cargo_toml.read_text(encoding="utf-8")
            except Exception:
                continue
            sorted_text = sort_toml_deps(original)
            if sorted_text == original:
                continue
            if check_only:
                print(f"CHECK FAIL: {cargo_toml}")
                modified += 1
                continue
            cargo_toml.write_text(sorted_text, encoding="utf-8")
            print(f"Sorted: {cargo_toml}")
            modified += 1

    if check_only and modified:
        print(f"\n{modified} Cargo.toml file(s) need sorting. Run `just fmt` to fix.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
