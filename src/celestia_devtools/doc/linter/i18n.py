"""Internationalization lint — detect untranslated paragraphs.

Warns when a large paragraph is identical across two or more language-variant
directories (e.g. ``docs/en`` vs ``docs/zhs``), which usually means a document
was copied but never translated.
"""

from __future__ import annotations

from pathlib import Path

from celestia_devtools.doc.linter._common import safe_read


KNOWN_LANG_CODES = {
    "en", "es", "fr", "ja", "ko", "ru", "zhs", "zht",
    "de", "pt", "ar", "zh", "zh-CN",
}


def extract_paragraphs(content: str, min_len: int = 80) -> list[str]:
    """Extract normal paragraphs longer than min_len (skipping code fences)."""
    lines = content.replace("\r\n", "\n").split("\n")
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in lines:
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            if current:
                text = "\n".join(current).strip()
                if len(text) >= min_len:
                    paragraphs.append(text)
                current = []
            continue
        if in_fence:
            continue
        if line.strip():
            current.append(line.rstrip())
        else:
            if current:
                text = "\n".join(current).strip()
                if len(text) >= min_len:
                    paragraphs.append(text)
                current = []
    if current:
        text = "\n".join(current).strip()
        if len(text) >= min_len:
            paragraphs.append(text)
    return paragraphs


def check_duplicate_paragraphs(paths: list[Path], root: Path) -> list[str]:
    """Warn about identical large paragraphs across language variants.

    Detects the common mistake of copying an English/Chinese document to
    another language directory and forgetting to translate it.
    """
    groups: dict[str, list[tuple[str, Path]]] = {}

    for path in paths:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        lang = None
        logical_parts: list[str] = []
        for i, part in enumerate(parts):
            if part in KNOWN_LANG_CODES and i < 3:
                lang = part
                logical_parts = list(parts[i + 1:])
                break
        if lang is None:
            continue
        logical = "/".join(logical_parts)
        groups.setdefault(logical, []).append((lang, path))

    warnings: list[str] = []
    for logical, files in groups.items():
        if len(files) < 2:
            continue
        para_to_langs: dict[str, list[str]] = {}
        for lang, path in files:
            content = safe_read(path)
            if content is None:
                continue
            for para in extract_paragraphs(content):
                para_to_langs.setdefault(para, []).append(lang)

        for para, langs in para_to_langs.items():
            unique_langs = set(langs)
            if len(unique_langs) >= 2:
                preview = para[:100].replace("\n", " ")
                if len(para) > 100:
                    preview += "…"
                warnings.append(
                    f"  {logical}: {len(para)}-char paragraph identical in {', '.join(sorted(unique_langs))}"
                )
                warnings.append(f"    \"{preview}\"")

    return warnings
