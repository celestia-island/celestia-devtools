"""Tests for the Markdown formatting pipeline (doc/markdown.py)."""

import sys
import pytest
from celestia_devtools.doc.markdown import (apply_fence_languages, format_markdown, normalize_table_separator, postprocess_lines, split_frontmatter, wrap_code_like_tokens)
from celestia_devtools.doc.markdown import main as markdown_main

class test_heading_spacing:
    def test_missing_space_after_hash(self):
        assert postprocess_lines(["#Heading"]) == ["# Heading"]

    def test_heading_gets_blank_lines(self):
        result = postprocess_lines(["text", "# Heading", "text"])
        assert result == ["text", "", "# Heading", "", "text"]

    def test_multiple_hashes(self):
        assert postprocess_lines(["###Deep"]) == ["### Deep"]

class test_list_normalization:
    def test_ordered_marker_normalized(self):
        result = postprocess_lines(["3. item"])
        assert result == ["1. item"]

    def test_unordered_preserved(self):
        result = postprocess_lines(["- item"])
        assert result == ["- item"]

    def test_list_blank_before(self):
        result = postprocess_lines(["paragraph", "- item"])
        assert result == ["paragraph", "", "- item"]

class test_table_separator:
    def test_simple_separator(self):
        assert normalize_table_separator("|---|---|") == "| --- | --- |"

    def test_alignment_colons(self):
        result = normalize_table_separator("|:---|---:|")
        # Left-align (:---) → leading space; right-align (---:) → trailing space
        assert result == "|  --- | ---  |"

class test_frontmatter:
    def test_valid_frontmatter(self):
        lines = ["+++", 'title = "x"', "+++", "# Body"]
        fm, body = split_frontmatter(lines)
        assert fm == ['title = "x"']
        assert body == ["# Body"]

    def test_no_frontmatter(self):
        fm, body = split_frontmatter(["# Just a heading"])
        assert fm == []
        assert body == ["# Just a heading"]

    def test_leading_blanks_skipped(self):
        lines = ["", "", "+++", 'title = "x"', "+++", "# Body"]
        fm, body = split_frontmatter(lines)
        assert fm == ['title = "x"']

    def test_frontmatter_in_format(self):
        content = '+++\ntitle = "Test"\n+++\n# Hello\n'
        result = format_markdown(content)
        assert result.startswith("+++\ntitle = \"Test\"\n+++")
        assert "# Hello" in result

class test_fence_language_application:
    def test_bare_fence_gets_language(self):
        lines = ["```", "def foo():", "    pass", "```"]
        result = apply_fence_languages(lines)
        assert result[0] == "```python"

    def test_existing_language_preserved(self):
        lines = ["```bash", "echo hi", "```"]
        result = apply_fence_languages(lines)
        assert result[0] == "```bash"

class test_code_like_tokens:
    def test_long_camelcase_wrapped(self):
        result = wrap_code_like_tokens("cacheGuardDiskThreshold")
        assert "`cacheGuardDiskThreshold`" in result

    def test_short_camelcase_not_wrapped(self):
        result = wrap_code_like_tokens("fooBar")
        assert result == "fooBar"

    def test_snake_case_wrapped(self):
        result = wrap_code_like_tokens("cargo_target_dir")
        assert "`cargo_target_dir`" in result

    def test_inside_backticks_not_wrapped(self):
        result = wrap_code_like_tokens("see `cache_guard` here")
        assert "`cache_guard`" in result

class test_full_format:
    def test_idempotent(self):
        """Running format_markdown twice should not change the result."""
        content = "# Title\n\n- item one\n- item two\n"
        once = format_markdown(content)
        twice = format_markdown(once)
        assert once == twice

    def test_trailing_newline(self):
        result = format_markdown("# Hello")
        assert result.endswith("\n")

    def test_strip_trailing_blanks(self):
        result = format_markdown("# Hello\n\n\n")
        assert not result.endswith("\n\n\n")

class test_check_mode:
    """`--check` is the CI gate; its exit code is the whole contract.

    The `markdown-check` job runs `format-markdown . --check` and treats a
    non-zero exit as the failure signal. `main()` only returns non-zero for a
    missing target, so a file that would be rewritten never fails the job -- and
    the tab and untranslated-paragraph warnings do not affect the exit code
    either. The test below is marked `xfail(strict=True)` to keep that gap
    executable instead of prose: whoever makes `--check` report its result will
    turn this test green, and the strict marker will fail until it is removed.
    """

    @pytest.mark.xfail(strict = True, reason = "main() returns 0 unconditionally; --check cannot fail (see the PR that added this test)")
    def test_unformatted_file_fails_the_check(self, tmp_path, monkeypatch):
        (tmp_path / "doc.md").write_text("#Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        assert markdown_main() == 1

    def test_check_mode_does_not_write(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.md"
        path.write_text("#Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        markdown_main()
        assert path.read_text(encoding = "utf-8") == "#Heading\n"
