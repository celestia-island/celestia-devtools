"""Box-frame glyph lint — ASCII/box-drawing frame diagrams are forbidden.

Structural diagrams are written as fenced Mermaid blocks (the policy lives in
``docs/<locale>/diagram-policy.md``). A box-frame corner/junction glyph outside
a legal fence means an ASCII frame diagram. The plain tree glyphs
(``│ ├ └ ─``, as in ``├── name`` directory listings) are deliberately not in
the glyph set, and fences tagged ``console`` / ``terminal`` / ``ansi`` /
``output`` / ``text`` are exempt so terminal-output depictions stay legal
plain text.
"""

from __future__ import annotations
import re
from pathlib import Path
from celestia_devtools.doc.linter._common import safe_read

#: Corner/junction glyphs of box-drawing frames (single and double line, plain
#: and heavy). The plain vertical/horizontal/branch family (``│ ─ ├ └ ┤ ┬``…)
#: that directory trees are built from stays out of this set on purpose.
FRAME_GLYPHS = "┌┐┘┬┴┼╔╗╝╚╠╣╦╩╬┏┓┗┛┣┫"

_FRAME_RE = re.compile(f"[{FRAME_GLYPHS}]")
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)([^`]*)$")

#: Fence tags whose content is never scanned: ``mermaid`` renders as a diagram,
#: the rest are legal plain-text terminal-output depictions.
EXEMPT_FENCE_TAGS = frozenset({"mermaid", "console", "terminal", "ansi", "output", "text"})

def fence_tag(info_string: str) -> str:
    """First token of a fence info string, lowercased and attribute-stripped."""
    parts = info_string.strip().split()

    if not parts:
        return ""

    tag = parts[0].strip("{}").lower()
    return tag[1:] if tag.startswith(".") else tag

def check_box_frames(paths: list[Path]) -> list[str]:
    """Warn about box-frame glyphs outside Mermaid and terminal-output fences."""
    warnings: list[str] = []

    for path in paths:
        text = safe_read(path)

        if text is None:
            continue

        in_fence = False
        exempt_fence = False

        for lineno, line in enumerate(text.splitlines(), 1):
            match = _FENCE_RE.match(line)

            if match:
                in_fence = not in_fence
                exempt_fence = in_fence and (fence_tag(match.group(3)) in EXEMPT_FENCE_TAGS)
                continue

            # Inside an exempt fence the content is a diagram or legal plain
            # text; everywhere else (prose, bare fences, other languages) a
            # frame glyph is an ASCII frame diagram.
            if in_fence and exempt_fence:
                continue

            if _FRAME_RE.search(line):
                warnings.append(f"  {path}:{lineno} contains box-frame glyph(s) — use a fenced Mermaid diagram " "(ASCII frame diagrams are forbidden)")

    return warnings
