"""Tests for the Markdown formatting pipeline (doc/markdown.py)."""

import sys
from celestia_devtools.doc.markdown import (_URL_RE, apply_fence_languages, format_markdown, normalize_table_separator, postprocess_lines, split_frontmatter, wrap_code_like_tokens)
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
    non-zero exit as the failure signal. Until this test existed, `main()`
    returned 0 on every path but a missing target, so the job could not fail and
    the repository's own 32 unformatted files were invisible. It was carried as
    an `xfail(strict=True)` debt latch for one revision; turning `--check` into a
    real gate is what made it green.
    """

    def test_unformatted_file_fails_the_check(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "doc.md").write_text("#Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        assert markdown_main() == 1
        captured = capsys.readouterr()
        assert "WOULD REFORMAT" in captured.err
        assert "needs" not in captured.out

    def test_formatted_tree_passes_the_check(self, tmp_path, monkeypatch):
        (tmp_path / "doc.md").write_text("# Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        assert markdown_main() == 0

    def test_check_mode_does_not_write(self, tmp_path, monkeypatch):
        path = tmp_path / "doc.md"
        path.write_text("#Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        markdown_main()
        assert path.read_text(encoding = "utf-8") == "#Heading\n"

class test_list_continuation:
    """A wrapped list item is one item, not two paragraphs.

    The formatter used to strip the continuation's indentation and insert a blank
    line before it, so `- ... a safety interlock,` / `  or a write whitelist ...`
    came out as an item followed by a stray paragraph. That is why `--check`
    could not be turned on: it flagged the repository's own README.
    """

    def test_wrapped_item_stays_one_item(self):
        result = postprocess_lines(["- a safety interlock,", "  or a write whitelist;", "- next"])
        assert result == ["- a safety interlock,", "  or a write whitelist;", "- next"]

    def test_continuation_keeps_its_indentation(self):
        result = postprocess_lines(["- item", "    deeply indented rest"])
        assert result == ["- item", "    deeply indented rest"]

    def test_nested_item_continuation(self):
        result = postprocess_lines(["- outer", "  - inner", "    rest of inner", "- next outer"])
        assert result == ["- outer", "  - inner", "    rest of inner", "- next outer"]

    def test_outer_item_continuation_after_a_nested_list(self):
        # The stack keeps the outer indent alive, so text continuing the OUTER
        # item after a nested list is not mistaken for a new paragraph.
        result = postprocess_lines(["- outer", "  - inner", "  rest of outer"])
        assert result == ["- outer", "  - inner", "  rest of outer"]

    def test_unindented_line_still_ends_the_list(self):
        result = postprocess_lines(["- item", "paragraph"])
        assert result == ["- item", "", "paragraph"]

    def test_ordered_item_continuation(self):
        result = postprocess_lines(["1. first,", "   more of first", "2. second"])
        assert result == ["1. first,", "   more of first", "1. second"]

class test_urls_are_left_alone:
    """Code-token wrapping must never rewrite a link.

    `.../version/2/1/code_of_conduct/` became `.../version/2/1/`code_of_conduct`/`
    -- a broken URL written back into the file by an "auto-format".
    """

    def test_bare_url_is_untouched(self):
        text = "see https://example.com/some_token_name/x for details"
        assert wrap_code_like_tokens(text) == text

    def test_autolink_is_untouched(self):
        text = "available at <https://example.com/code_of_conduct/>."
        assert wrap_code_like_tokens(text) == text

    def test_reference_definition_is_untouched(self):
        text = "[cc]: https://example.com/version/2/1/code_of_conduct/"
        assert wrap_code_like_tokens(text) == text

    def test_tokens_outside_a_url_are_still_wrapped(self):
        # Positive control: the URL guard must not disable the feature itself.
        assert wrap_code_like_tokens("the some_token_name here") == "the `some_token_name` here"
        assert wrap_code_like_tokens("a VirtualMachinePlatform call") == "a `VirtualMachinePlatform` call"

class test_addresses_are_left_alone:
    """The URL guard covers addresses too, not just http links.

    `mailto:some_one@example.com` was rewritten to
    `mailto:`some_one`@example.com` -- the same corruption, one scheme over.
    """

    def test_mailto_is_untouched(self):
        text = "write to mailto:some_one@example.com"
        assert wrap_code_like_tokens(text) == text

    def test_bare_email_is_untouched(self):
        text = "a bare mail some_one@example.com here"
        assert wrap_code_like_tokens(text) == text

    def test_other_schemes_are_untouched(self):
        text = "see file:///tmp/some_path and ftp://host/some_path/x"
        assert wrap_code_like_tokens(text) == text

    def test_markdown_link_destination_is_untouched(self):
        # Relative destinations already survive via the regex lookarounds; pin
        # it so a future relaxation cannot quietly start rewriting them.
        for text in ("[x](docs/some_file_name.md)", "[ref]: docs/some_file_name.md"):
            assert wrap_code_like_tokens(text) == text

class test_fenced_block_inside_an_item:
    """A fence indented past a list marker belongs to the item.

    Blanking around it (what a top-level fence needs) turned a tight list into a
    loose one, which renders the item's text as a paragraph -- a structure change
    an "auto-format" has no business making.
    """

    def test_tight_list_with_a_fence_stays_tight(self):
        lines = ["- item", "  ```", "  code_snake_name", "  ```", "- next"]
        assert postprocess_lines(lines) == lines

    def test_top_level_fence_still_gets_blank_lines(self):
        result = postprocess_lines(["para", "```", "x", "```", "after"])
        assert result == ["para", "", "```", "x", "```", "", "after"]

    def test_unindented_fence_ends_the_list(self):
        result = postprocess_lines(["- item", "```", "x", "```", "after"])
        assert result == ["- item", "", "```", "x", "```", "", "after"]

class test_link_targets_are_left_alone:
    """Relative targets were the gap the URL guard left open.

    `[guide](/docs/getting_started)` became `[guide](/docs/`getting_started`)`
    -- no scheme, so the earlier guard never saw it, and `--check` would have
    written that broken link back as the "formatted" form.
    """

    def test_root_relative_link_target(self):
        text = "[guide](/docs/getting_started)"
        assert wrap_code_like_tokens(text) == text

    def test_bare_path_in_prose(self):
        text = "see /docs/get_started_page for details"
        assert wrap_code_like_tokens(text) == text

    def test_image_target(self):
        text = "![img](/assets/logo_image.png)"
        assert wrap_code_like_tokens(text) == text

    def test_reference_definition_target(self):
        text = "[ref]: /docs/get_started"
        assert wrap_code_like_tokens(text) == text

    def test_url_pattern_has_no_capture_group(self):
        # `_wrap_outside_urls` splits and findalls with this pattern; a capture
        # group would make every match appear twice and duplicate the URL.
        assert _URL_RE.groups == 0

class test_tab_indentation:
    def test_tab_indented_continuation(self):
        assert postprocess_lines(["- item", "\tmore text"]) == ["- item", "\tmore text"]

    def test_tab_indented_nested_item(self):
        result = postprocess_lines(["\t- inner", "\t\trest"])
        assert result == ["\t- inner", "\t\trest"]

class test_indented_table_and_quote_in_item:
    def test_indented_table_row_keeps_its_column(self):
        assert postprocess_lines(["- a", "  | h | i |"]) == ["- a", "  | h | i |"]

    def test_indented_blockquote_keeps_its_column(self):
        assert postprocess_lines(["- a", "  > note"]) == ["- a", "  > note"]

class test_cli_input_handling:
    def test_non_markdown_target_is_refused(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "note.txt"
        path.write_text("#Heading\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(path)])
        assert markdown_main() == 1
        assert "not a Markdown file" in capsys.readouterr().err
        assert path.read_text(encoding = "utf-8") == "#Heading\n"  # untouched

    def test_unreadable_file_fails_the_check_without_a_traceback(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "locked.md"
        path.write_text("# Heading\n", encoding = "utf-8")
        path.chmod(0o000)

        try:
            monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
            assert markdown_main() == 1
            assert "UNREADABLE" in capsys.readouterr().err
        finally:
            path.chmod(0o644)

class test_link_target_branch_is_guarded:
    """The `](target)` protection has to be its own test, not a side effect.

    A mutation removing that branch from `_URL_RE` left the whole suite green,
    because the root-relative cases survive on the `/` lookaround alone. These
    two inputs only pass when the branch itself is present.
    """

    def test_relative_target_without_a_slash(self):
        text = "[x](getting_started)"
        assert wrap_code_like_tokens(text) == text

    def test_anchor_target(self):
        text = "[x](#some_anchor)"
        assert wrap_code_like_tokens(text) == text

    def test_nested_item_fence_stays_tight(self):
        result = postprocess_lines(["- outer", "  - inner", "  ```", "  code_x", "  ```"])
        assert result == ["- outer", "  - inner", "  ```", "  code_x", "  ```"]
