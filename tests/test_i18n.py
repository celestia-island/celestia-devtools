"""Tests for i18n duplicate-paragraph detection and tab linting."""

from celestia_devtools.doc.linter.i18n import (
    KNOWN_LANG_CODES,
    check_duplicate_paragraphs,
    extract_paragraphs,
)
from celestia_devtools.doc.linter.tabs import check_tabs


class TestExtractParagraphs:
    def test_simple_paragraph(self):
        text = "This is a long enough paragraph to exceed the minimum length threshold for detection."
        paras = extract_paragraphs(text)
        assert len(paras) == 1
        assert paras[0] == text.strip()

    def test_short_paragraph_ignored(self):
        text = "Short."
        assert extract_paragraphs(text) == []

    def test_code_blocks_skipped(self):
        text = (
            "```\n"
            "This is inside a code block and is long enough to be a paragraph but should be ignored."
            "\n```\n"
            "This is a real paragraph that is long enough to pass the minimum length threshold."
        )
        paras = extract_paragraphs(text)
        assert len(paras) == 1
        assert "real paragraph" in paras[0]

    def test_multiple_paragraphs(self):
        text = (
            "First paragraph that is long enough to exceed the minimum length threshold here.\n\n"
            "Second paragraph also long enough to exceed the minimum length threshold easily."
        )
        paras = extract_paragraphs(text)
        assert len(paras) == 2


class TestCheckDuplicateParagraphs:
    def test_identical_across_langs_warns(self, tmp_path):
        """Two language dirs with an identical long paragraph should warn."""
        long_text = "This is a sufficiently long paragraph that is identical across two language directories for testing."
        for lang in ("en", "zhs"):
            d = tmp_path / "docs" / lang
            d.mkdir(parents=True)
            (d / "intro.md").write_text(long_text, encoding="utf-8")

        warnings = check_duplicate_paragraphs(
            list((tmp_path / "docs").rglob("*.md")), tmp_path / "docs"
        )
        assert len(warnings) >= 2  # header line + preview line

    def test_different_across_langs_no_warn(self, tmp_path):
        """Different paragraphs should not warn."""
        (tmp_path / "docs" / "en").mkdir(parents=True)
        (tmp_path / "docs" / "zhs").mkdir(parents=True)
        (tmp_path / "docs" / "en" / "intro.md").write_text(
            "This is the English version of a sufficiently long paragraph.", encoding="utf-8"
        )
        (tmp_path / "docs" / "zhs" / "intro.md").write_text(
            "这是中文版本的一个足够长的段落，用于测试不会产生重复段落警告的功能。", encoding="utf-8"
        )

        warnings = check_duplicate_paragraphs(
            list((tmp_path / "docs").rglob("*.md")), tmp_path / "docs"
        )
        assert warnings == []

    def test_single_lang_no_warn(self, tmp_path):
        """A single language directory cannot have duplicates."""
        d = tmp_path / "docs" / "en"
        d.mkdir(parents=True)
        (d / "intro.md").write_text(
            "A standalone paragraph in only one language directory.", encoding="utf-8"
        )

        warnings = check_duplicate_paragraphs(
            list((tmp_path / "docs").rglob("*.md")), tmp_path / "docs"
        )
        assert warnings == []


class TestCheckTabs:
    def test_tab_found_warns(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("line one\n\tindented with tab\nline three", encoding="utf-8")
        warnings = check_tabs([f])
        assert len(warnings) == 1
        assert "tab character" in warnings[0]

    def test_no_tabs_clean(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("line one\nline two\nline three", encoding="utf-8")
        assert check_tabs([f]) == []


class TestKnownLangCodes:
    def test_common_codes_present(self):
        for code in ("en", "zhs", "zht", "ja", "ko", "fr", "es", "ru", "ar"):
            assert code in KNOWN_LANG_CODES
