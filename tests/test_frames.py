"""Tests for the box-frame glyph gate (doc/linter/frames.py).

Negative control: the same box-frame glyphs must PASS inside a Mermaid fence,
inside every terminal-output fence tag (console/terminal/ansi/output/text),
and as a plain directory-tree listing — the gate flags frame art only outside
those legal fences, and never flags the ``│ ├ └ ─`` tree glyph family.
"""

import sys
from celestia_devtools.doc.linter.frames import EXEMPT_FENCE_TAGS, check_box_frames, fence_tag
from celestia_devtools.doc.markdown import main as markdown_main

FRAME_ART = ["┌──────┐         ┌──────┐", "│  in  │ ──┴──> │ out  │", "└──────┘         └──────┘"]

class test_frame_detection:
    def test_frame_diagram_in_prose_fails(self, tmp_path):
        """Box-frame glyphs in plain prose are the violation the gate exists for."""
        f = tmp_path / "doc.md"
        f.write_text("# Pipeline\n\n" + "\n".join(FRAME_ART) + "\n", encoding = "utf-8")
        warnings = check_box_frames([f])
        assert len(warnings) == 3
        assert (str(f) in warnings[0]) and (":3" in warnings[0])

    def test_each_violation_reports_file_and_line(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("intro\n\n┌──┐\nprose\n└──┘\n", encoding = "utf-8")
        warnings = check_box_frames([f])
        assert warnings == [f"  {f}:3 contains box-frame glyph(s) — use a fenced Mermaid diagram (ASCII frame diagrams are forbidden)", f"  {f}:5 contains box-frame glyph(s) — use a fenced Mermaid diagram (ASCII frame diagrams are forbidden)"]

    def test_bare_fence_frame_art_fails(self, tmp_path):
        """Only mermaid and the terminal-output tags are exempt; a bare fence is not."""
        f = tmp_path / "doc.md"
        f.write_text("```\n" + "\n".join(FRAME_ART) + "\n```\n", encoding = "utf-8")
        assert len(check_box_frames([f])) == 3

    def test_other_language_fence_frame_art_fails(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("```python\nchart = '┌──┐'\n```\n", encoding = "utf-8")
        assert len(check_box_frames([f])) == 1

class test_negative_controls:
    def test_mermaid_fence_with_frame_glyphs_passes(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("```mermaid\nflowchart LR\n    A[\"┌──┐ box\"] --> B\n```\n", encoding = "utf-8")
        assert check_box_frames([f]) == []

    def test_terminal_output_fence_tags_pass(self, tmp_path):
        for tag in ("console", "terminal", "ansi", "output", "text"):
            f = tmp_path / f"{tag}.md"
            f.write_text(f"```{tag}\n" + "\n".join(FRAME_ART) + "\n```\n", encoding = "utf-8")
            assert check_box_frames([f]) == [], tag

    def test_exempt_tags_declared(self):
        assert EXEMPT_FENCE_TAGS == frozenset({"mermaid", "console", "terminal", "ansi", "output", "text"})

    def test_directory_tree_passes(self, tmp_path):
        """Directory-tree listings are explicitly allowed — plain tree glyphs only."""
        f = tmp_path / "doc.md"
        f.write_text("src/\n├── engine.meta\n├── asr_server.py\n└── ...  # remaining engine files\n", encoding = "utf-8")
        assert check_box_frames([f]) == []

    def test_tree_and_frame_mixed_flags_only_the_frame(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("layout:\n├── a.py\n┌──────┐\n└──────┘\n└── b.py\n", encoding = "utf-8")
        warnings = check_box_frames([f])
        assert [w.split(":", 1)[1].split()[0] for w in warnings] == ["3", "4"]

    def test_clean_markdown_passes(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Heading\n\n```mermaid\nflowchart TD\n    A --> B\n```\n\nDone.\n", encoding = "utf-8")
        assert check_box_frames([f]) == []

class test_fence_tag:
    def test_first_token_lowercased(self):
        assert fence_tag("Mermaid strict") == "mermaid"

    def test_empty_info_string(self):
        assert fence_tag("") == ""
        assert fence_tag("   ") == ""

    def test_attribute_forms(self):
        assert fence_tag("{.text}") == "text"
        assert fence_tag("{python}") == "python"

class test_cli_gate_wiring:
    def test_violation_fails_check_mode(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "doc.md"
        f.write_text("# Pipeline\n\n" + "\n".join(FRAME_ART) + "\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        assert markdown_main() == 1
        assert str(f) in capsys.readouterr().err

    def test_violation_fails_format_mode_too(self, tmp_path, monkeypatch, capsys):
        """A policy breach is not auto-fixable, so formatting still fails the run.

        Regression pin (R2 round 1): the fixture uses a BARE fence. In format
        mode the write loop re-tags bare fences via infer_fence_language
        (glyph content gets tagged ``text``, an exempt tag), so a gate that
        scans files from disk AFTER the write loop would see an exempt fence
        and silently pass — this test must assert the warning is actually
        printed, not just an exit code.
        """
        f = tmp_path / "doc.md"
        f.write_text("# Pipeline\n\n```\n" + "\n".join(FRAME_ART) + "\n```\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path)])
        assert markdown_main() == 1
        err = capsys.readouterr().err
        assert "Box-frame glyphs" in err
        assert str(f) in err

    def test_format_mode_text_tagged_frame_is_exempt(self, tmp_path, monkeypatch):
        """Steady state: a ``text``-tagged fence is a legal terminal depiction.

        The formatter itself tags glyph-bearing bare fences as ``text``; once
        tagged they are exempt in every mode. This pins the exemption so the
        format-mode failure above cannot be 'fixed' by simply never tagging.
        """
        f = tmp_path / "doc.md"
        f.write_text("```text\n" + "\n".join(FRAME_ART) + "\n```\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path)])
        assert markdown_main() == 0

    def test_tilde_mermaid_fence_passes(self, tmp_path):
        """``~~~mermaid`` fences are CommonMark fences too."""
        f = tmp_path / "doc.md"
        f.write_text("~~~mermaid\nflowchart LR\n    A[\"┌──┐\"] --> B\n~~~\n", encoding = "utf-8")
        assert check_box_frames([f]) == []

    def test_crlf_line_endings_still_flagged(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("intro\r\n\r\n┌──┐\r\n└──┘\r\n", encoding = "utf-8")
        warnings = check_box_frames([f])
        assert len(warnings) == 2

    def test_inline_code_span_glyph_is_not_exempt(self, tmp_path):
        """Inline spans are not fences; a glyph there is still flagged."""
        f = tmp_path / "doc.md"
        f.write_text("use `┌──┐` sparingly\n", encoding = "utf-8")
        assert len(check_box_frames([f])) == 1

    def test_unclosed_fence_runs_to_eof_commonmark(self, tmp_path):
        """CommonMark: an unclosed fence extends to end-of-file, so its content
        is fence content under the tag's exemption rules (pinned behavior)."""
        f = tmp_path / "doc.md"
        f.write_text("```text\n" + "\n".join(FRAME_ART) + "\n", encoding = "utf-8")
        assert check_box_frames([f]) == []
        g = tmp_path / "doc2.md"
        g.write_text("```python\n" + "\n".join(FRAME_ART) + "\n", encoding = "utf-8")
        assert len(check_box_frames([g])) == 3

    def test_clean_file_passes(self, tmp_path, monkeypatch):
        f = tmp_path / "doc.md"
        f.write_text("# Heading\n\n```mermaid\nflowchart TD\n    A --> B\n```\n", encoding = "utf-8")
        monkeypatch.setattr(sys, "argv", ["format-markdown", str(tmp_path), "--check"])
        assert markdown_main() == 0
