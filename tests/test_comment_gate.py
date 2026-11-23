#!/usr/bin/env python3
"""Tests for the comment gate (``celestia_devtools.lint.comment_gate``).

The gate exists because three consecutive seam PRs shipped correct code and
wrong prose (hikari #1476 / #1478 / #1482). Its design fixes three negative
controls, and all three are pinned here:

1. **a known positive must pass** — the real corpus of the four merged
   seam-pin files (27 documented vocabulary hits; the checked-in fixtures are
   those files verbatim, so the case is recomputable without hikari checked
   out);
2. **a known negative must red** — the ``@vite-ignore`` sentence from #1482
   (with its mutation name removed), exactly on its line;
3. **zero production must fail** — a scan that finds nothing refuses instead of
   reporting green, so a mis-typed pattern cannot pass silently.

The corpus control is deliberately split into two cases. ``test_file_lane_*``
measures the scanner on the raw files (the shape the design document probed:
how many lines even hit the vocabulary). ``test_diff_lane_*`` measures the gate
on a diff of those files with the mutation-table marker the corpus needs added,
and THAT one is the "known positive passes" control — because the unmodified
corpus does not pass, and pretending otherwise would be the exact
prose-vs-fact failure the gate was built to catch. The measured numbers in both
cases are pinned verbatim so a silent drop shows up as a red, not as a quieter
report.
"""

from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path
import pytest
from celestia_devtools.lint import comment_gate

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "comment_gate"

#: The four merged seam-pin files, checked in verbatim (hikari @ f9e22fd4).
CORPUS = ("useHighlight.coldPath.test.ts", "useHighlight.hostSurface.test.ts", "qrHostSurface.test.ts", "scene3dHostSurface.test.ts")

#: The measured corpus baseline (see the module docstring). A change here means
#: either the vocabulary moved or the fixtures were edited — both need a reason.
CORPUS_HITS = 38
CORPUS_NON_COMMENT_HITS = 2

#: The #1482 sentence that was false in production and true only after the fix:
#: the claim about what ``@vite-ignore`` did to ``vi.mock``. With its mutation
#: name stripped it is a pure claim from memory — the known negative.
VITE_IGNORE_CLAIM = "A `/* @vite-ignore */` here would silence the bundler's static analysis"

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding = "utf-8")

def _as_added_diff(name: str, text: str | None = None, extra_rows: str = "") -> str:
    """A diff that adds the whole file (every line is ``+``)."""
    body = (text if text is not None else _fixture(name)).splitlines()

    if extra_rows:
        body = extra_rows.splitlines() + body

    header = f"diff --git a/{name} b/{name}\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(body)} @@"
    return "\n".join([header, *[("+" + line) for line in body]]) + "\n"

#: The anchor a PR's body carries: a reference to the mutation table plus one
#: of its rows. Applied to every corpus section that holds a claim.
_TABLE_ANCHOR = ("// Mutation table (PR body):", "// | mutation | drop the guard, or invert the matrix | 2 green |")

def _add_claim_diff(comment_line: str, anchor: str | None = None, path: str = "packages/vue/src/a.test.ts") -> str:
    """A minimal diff with one comment line and an optional anchor line."""
    lines = [comment_line]

    if anchor is not None:
        lines.append(anchor)

    body = "\n".join(("+" + line) for line in lines)
    header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,{len(lines)} @@"
    return header + "\n" + body + "\n"

class test_vocabulary:
    """The word list is data, and its whole surface is pinned here."""

    def test_design_terms_present(self):
        # The design document's list, verbatim. Deleting any one of these is a
        # rule change and must red this assertion.
        for term in ("measured", "verified", "reds", "does not", "catches", "only when", "never", "抓住", "抓不住", "只在"):
            assert any((term == entry.lower()) or (term in entry.lower()) for entry in comment_gate.BEHAVIOUR_TERMS), term

    def test_cjk_time_form_is_a_regex_fragment(self):
        # `只有 … 时才` cannot be a literal (the subject sits in the middle).
        assert any(".*" in entry for entry in comment_gate.BEHAVIOUR_TERMS)
        assert comment_gate.behaviour_terms("只有 load 失败时才重试")

    @pytest.mark.parametrize("line", ["// this pin catches a later reload", "// does NOT produce a second build", "// Scope, precisely (measured):", " * reds this case plus the two below", "# Measured on the CI lane", "-- only when the flag is set", "<!-- verified here -->", ";; always"])
    def test_comment_forms_are_seen(self, line):
        assert comment_gate.behaviour_terms(line)
        assert comment_gate.comment_body(line) is not None

    @pytest.mark.parametrize("line", ["// a well-named helper", "expect(x).toBe(1);", "#[allow(dead_code)]", "const moved = true;"])
    def test_ordinary_text_is_not_a_claim(self, line):
        assert comment_gate.behaviour_terms(line) == ()

    def test_code_lines_are_never_claims(self):
        # The corpus has two real code lines that match the vocabulary (`as
        # [never]`). They are not comments, so they are never claims — that is
        # the whole reason the scanner looks at comment text only.
        code = 'if (name !== EXPORT) return rawBuild(...(args as [never]));'
        assert comment_gate.behaviour_terms(code)
        assert comment_gate.comment_body(code) is None

    def test_comment_forms_are_not_code(self):
        assert comment_gate.comment_body("expect(measured).toBe(1)") is None
        assert comment_gate.comment_body("  const verified = true;") is None

#: Shapes that must NOT anchor a claim. The prose sentence is the important one:
#: "this pin catches X" is the very claim the gate exists to challenge.
NON_ANCHOR_LINES = ("// MUT:", "// MUT:   ", "// mutate:", "// Scope, precisely: this pin catches the argument being dropped.", "// measured: deleting the version bump kept the warmed pins green", "// nothing to see here", "expect(x).toBe(1);")

class test_anchor_shapes:
    """A claim is anchored by an id, a table reference, or a real table row."""

    @pytest.mark.parametrize("line", ["// MUT: drop-the-capabilities-argument", "// mut: guard-removed", "// MUTATE: replace-the-surface-call", "// mutate: invert-the-matrix", "// see the mutation table in the PR body", "// 见 PR 正文的变异表", "// | drop the guard | 3 red |", "// - drop the guard → 3 red", "// 2. invert the door order: the pin stays green"])
    def test_anchor_forms(self, line):
        assert comment_gate.is_anchor(line), line

    @pytest.mark.parametrize("line", NON_ANCHOR_LINES)
    def test_non_anchors(self, line):
        assert not comment_gate.is_anchor(line), line

    def test_only_comment_text_can_anchor(self):
        """A code line carrying `MUT:` inside a string is not a mutation record.

        Review round 4 found the escape: `_anchor_text` fell back to the RAW
        line when the line was not a comment, so a content line `+// MUT: x`
        (a code line, in a test file) anchored a neighbouring claim.
        """

        for probe in ("+// MUT: planted", '  const note = "MUT: in a string";', "  setup({ MUT: true });"):
            assert not comment_gate.is_anchor(probe), probe

    def test_genuine_double_plus_content_is_not_rewritten(self):
        """Content `++counter;` arrives as `+++counter;` and must stay `++counter;`.

        Round 4 caught an earlier `_content` stripping a leading `+` from every
        `++…` body "to undo quoting", which mangled exactly this shape. Real
        `git diff -U0` confirms the rule: the hunk line is marker + content.
        """
        diff = _single_hunk("+++counter;\n+// measured: the claim\n", count = 2)
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 1
        assert [claim.line for claim in report.claims] == [2]
        assert comment_gate._content("+counter;") == "+counter;"

    def test_a_code_line_cannot_anchor_a_claim_in_a_real_diff(self):
        """The round-4 escape, end to end.

        The escape needed an added line whose FILE CONTENT is `+// MUT: x` — a
        code line, which git spells `++// MUT: x`. (`+// MUT: x` in the diff is
        a real comment, so it is not the shape.) With the fallback gone, that
        code line no longer certifies the claim below it.
        """
        diff = _single_hunk("++// MUT: planted inside a code line\n+// measured: the claim\n", count = 2)
        assert len(comment_gate.check_diff(diff).claims) == 1

    def test_bare_mut_prefix_does_not_anchor(self):
        # `MUT:` with no id names nothing. The next line is a claim, so the
        # whole block must report rather than pass on a dangling prefix.
        diff = _add_claim_diff("// catches the reload", anchor = "// MUT:")
        report = comment_gate.check_diff(diff)
        assert len(report.claims) == 1

class test_scope:
    """Only test files, only comments, only added or modified lines."""

    @pytest.mark.parametrize("path", ["packages/vue/src/useHighlight.test.ts", "packages/vue/src/useHighlight.spec.tsx", "src/repository_test.py", "tests/test_thing.py", "pkg/test/helpers.ts", "pkg/tests/deep/helpers.ts", "pkg/__tests__/helpers.js"])
    def test_test_paths(self, path):
        assert comment_gate.is_test_path(path)

    @pytest.mark.parametrize("path", ["packages/vue/src/useHighlight.ts", "src/celestia_devtools/vcs/pr_gate.py", "docs/measured.md", "tests/fixtures/helper.ts"])
    def test_non_test_paths(self, path):
        # `tests/fixtures/...` is a data tree, not a test module: the segment
        # rule looks at DIRECTORY segments, so `tests` does match — this case
        # documents the deliberate over-inclusion rather than pretending.
        assert comment_gate.is_test_path(path) == path.startswith("tests/")

    def test_production_comments_are_out_of_scope(self):
        # The false claim this gate was built from lived in PRODUCTION code
        # (`useHighlight.ts`). The gate does not cover it: production comments
        # are where `never` / `always` appear in ordinary prose, and a scanner
        # over them would trade one real signal for a flood of noise.
        diff = _add_claim_diff("// measured: it did NOT actually break vi.mock", path = "packages/vue/src/useHighlight.ts")
        report = comment_gate.check_diff(diff)
        assert report.claims == ()
        assert report.scanned_lines == 0

    def test_a_context_comment_is_not_a_claim_but_can_anchor(self):
        """Context lines are geometry and anchors — never claims.

        Round 3 measured the opposite: a context comment was reported as a
        claim while `scanned_lines` stayed 0, so one diff produced "1 claim"
        and "zero comment lines scanned" at the same time.
        """
        body = " // measured: this catches it\n-x\n+y\n"
        diff = _single_hunk(body, start = 1, count = 2, old_count = 2)
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 0
        assert report.claims == ()

    def test_a_context_anchor_still_binds_an_added_claim(self):
        body = " // MUT: context-anchor\n-x\n+// measured: this catches it\n"
        diff = _single_hunk(body, start = 1, count = 2, old_count = 2)
        assert comment_gate.check_diff(diff).claims == ()

    def test_removed_lines_are_not_scanned(self):
        # A claim the PR DELETES cannot block the PR.
        diff = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n@@ -1,2 +1,2 @@\n-// measured: this pin catches it\n+// a neutral comment\n expect(1).toBe(1);\n"
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 1
        assert report.claims == ()

    def test_anchor_in_a_distant_hunk_does_not_bind(self):
        # Hunks of one file are context for each other (the window is measured
        # in FILE lines), but a table 200 lines further down is outside ±10 and
        # must not excuse the claim.
        far = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n@@ -1,0 +1,1 @@\n+// measured: the pin catches it\n@@ -200,0 +200,1 @@\n+// MUT: somewhere-else\n"
        report = comment_gate.check_diff(far)
        assert len(report.claims) == 1

    def test_anchor_in_a_nearby_hunk_binds(self):
        # Same file, two hunks, anchor five file lines below the claim: hunks
        # are not block boundaries (measured while writing these tests: they are
        # three lines apart in the patch and a dozen apart in the file).
        near = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n@@ -1,0 +1,1 @@\n+// measured: the pin catches it\n@@ -6,0 +6,1 @@\n+// MUT: nearby-hunk\n"
        assert comment_gate.check_diff(near).claims == ()

class test_window:
    """The anchor window is ±10 lines, measured in file lines."""

    def test_anchor_at_the_window_edge_binds(self):
        # The claim is block index 0; NINE lines of code follow it, so the anchor
        # sits at block index 10 — exactly ANCHOR_WINDOW away, still binding.
        filler = "\n".join(["+" + line for line in ["expect(0).toBe(0);"] * 9])
        diff = _add_claim_diff("// catches the reload") + filler + "\n+// MUT: edge-anchor\n"
        assert comment_gate.ANCHOR_WINDOW == 10
        assert comment_gate.check_diff(diff).claims == ()

    def test_anchor_beyond_the_window_does_not_bind(self):
        # One line further (block index 11, eleven lines away) must NOT bind.
        filler = "\n".join(["+" + line for line in ["expect(0).toBe(0);"] * 10])
        diff = _add_claim_diff("// catches the reload") + filler + "\n+// MUT: too-far\n"
        assert len(comment_gate.check_diff(diff).claims) == 1

    def test_a_bare_directive_suppresses_nothing(self):
        """Round 5: `comment-gate-ok:` with no argument is a silent pass.

        The escape hatch must carry a reason — the same argument standard the
        PR-body waiver is held to — or it is a mute button on the gate.
        """
        diff = _single_hunk("+// measured: this catches it comment-gate-ok:\n", count = 1)
        report = comment_gate.check_diff(diff)
        assert len(report.claims) == 1
        assert any("comment-gate-ok with a thin reason" in term for term in report.claims[0].terms)

    def test_the_thin_reason_marker_reaches_the_report(self):
        """Round 6: the marker lived only in `claim.terms`, which no report renders."""
        diff = _single_hunk("+// measured: this catches it comment-gate-ok:\n", count = 1)
        claim = comment_gate.check_diff(diff).claims[0]
        assert "comment-gate-ok with a thin reason" in claim.render()

    def test_a_zero_width_directive_reason_suppresses_nothing(self):
        diff = _single_hunk("+// measured: this catches it comment-gate-ok: " + ("\u200b" * 25) + "\n", count = 1)
        assert len(comment_gate.check_diff(diff).claims) == 1

    def test_a_short_directive_reason_suppresses_nothing(self):
        diff = _single_hunk("+// measured: this catches it comment-gate-ok: flaky\n", count = 1)
        assert len(comment_gate.check_diff(diff).claims) == 1

    def test_directive_suppresses_one_line(self):
        diff = "diff --git a/a.test.ts b/a.test.ts\n" "--- a/a.test.ts\n" "+++ b/a.test.ts\n" "@@ -1,0 +1,2 @@\n" "+// measured on this node only — comment-gate-ok: a toolchain quirk, not a seam\n" "+expect(1).toBe(1);\n"
        report = comment_gate.check_diff(diff)
        assert report.claims == ()
        assert report.hits == 1

class test_negative_controls:
    """The three controls the design document requires, mechanically."""

    def test_positive_corpus_file_lane_hits_match_the_design(self):
        # The design document measured 27 vocabulary hits across the four
        # files. The scanner measures 38 *comment-line* hits on these exact
        # bytes — the difference is counting rule, not disagreement: the
        # document's 27 is its own narrower word list. Pinned so a silent
        # change in either direction is a red.
        total = 0
        per_file = {}

        for name in CORPUS:
            claims, hits = comment_gate.scan_file(name, _fixture(name))
            per_file[name] = hits
            total += hits
            assert len(claims) == hits  # nothing in the corpus carries an anchor

        assert per_file == {"useHighlight.coldPath.test.ts": 6, "useHighlight.hostSurface.test.ts": 14, "qrHostSurface.test.ts": 9, "scene3dHostSurface.test.ts": 9}
        assert total == CORPUS_HITS

    def test_positive_corpus_diff_lane_passes_with_a_mutation_table(self):
        """The real corpus, as a diff, anchored the way a maintainer would.

        The corpus pins carry no `MUT:` id and no table row — probe ids live in
        their PR bodies, not in the files — so the gate refuses them VERBATIM
        (next test). This case inserts one mutation-table marker (a reference
        plus a row, the third anchor form the design names) immediately before
        every claim. A correction pass on a merged file is exactly that edit, so
        the control is the real workflow rather than a synthetic positive: every
        claim then sits one line from its own anchor, the gate passes, and the
        vocabulary count is unchanged at CORPUS_HITS.
        """
        tables = 0
        diff = ""

        for name in CORPUS:
            out: list[str] = []

            for line in _fixture(name).splitlines():
                if (comment_gate.comment_body(line) is not None) and comment_gate.behaviour_terms(line):
                    tables += 1
                    out.extend(_TABLE_ANCHOR)

                out.append(line)

            header = f"diff --git a/{name} b/{name}\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(out)} @@"
            diff += header + "\n" + "\n".join(("+" + line) for line in out) + "\n"

        report = comment_gate.check_diff(diff)
        assert tables >= CORPUS_HITS, tables
        assert report.claims == (), [claim.render() for claim in report.claims]
        assert report.hits == CORPUS_HITS

    def test_negative_corpus_verbatim_is_refused(self):
        """Negative control #1 applied honestly: the corpus as-is does not pass."""
        diff = "".join(_as_added_diff(name) for name in CORPUS)
        report = comment_gate.check_diff(diff)
        assert len(report.claims) == CORPUS_HITS
        assert report.problems

    def test_negative_vite_ignore_claim_is_refused(self):
        """Negative control #2: the #1482 sentence, mutation name stripped."""
        diff = _add_claim_diff("// " + VITE_IGNORE_CLAIM, path = "packages/vue/src/useHighlight.hostSurface.test.ts")
        # `@vite-ignore` alone does not hit the vocabulary; the sentence does
        # in its full form, which is what the corpus carries.
        full = "diff --git a/p.test.ts b/p.test.ts\n--- a/p.test.ts\n+++ b/p.test.ts\n@@ -1,0 +1,4 @@\n+// Measured: a `/* @vite-ignore */` here did NOT actually break `vi.mock`\n+// under this Vite/vitest, so the reason to omit it is generality.\n+expect(1).toBe(1);\n"
        report = comment_gate.check_diff(full)
        assert [claim.line for claim in report.claims] == [1]
        assert report.problems
        assert comment_gate.check_diff(diff).scanned_lines == 1

    def test_negative_vite_ignore_claim_passes_when_anchored(self):
        full = "diff --git a/p.test.ts b/p.test.ts\n" "--- a/p.test.ts\n" "+++ b/p.test.ts\n" "@@ -1,0 +1,4 @@\n" "+// Measured: a `/* @vite-ignore */` here did NOT actually break `vi.mock`\n" "+// under this Vite/vitest, so the reason to omit it is generality.\n" "+// MUT: vite-ignore-removed\n" "+expect(1).toBe(1);\n"
        assert comment_gate.check_diff(full).claims == ()

    def test_zero_candidate_lines_refuses(self):
        """Negative control #3a: an empty diff must not read as green."""
        report = comment_gate.check_diff("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1,0 +1,1 @@\n+const x = 1;\n")
        assert report.scanned_lines == 0
        assert report.problems
        assert not report.ok

    def test_zero_vocabulary_hits_refuses(self):
        """Negative control #3b: no claim at all is a suspicious scan, not a pass."""
        report = comment_gate.check_diff("diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n@@ -1,0 +1,1 @@\n+// a neutral comment\n")
        assert report.scanned_lines == 1
        assert report.hits == 0
        assert report.problems
        assert not report.ok

class test_cli:
    """The command line: exit codes, ``--diff``, and the zero-input lane."""

    def test_anchored_file_passes(self, tmp_path, capsys):
        target = tmp_path / "a.test.ts"
        target.write_text("// measured: this catches it\n// MUT: guard-dropped\nexpect(1).toBe(1);\n", encoding = "utf-8")
        assert comment_gate.main(["--file", str(target), "--quiet"]) == 0
        capsys.readouterr()

    def test_claim_file_refused(self, tmp_path, capsys):
        target = tmp_path / "a.test.ts"
        target.write_text("// measured: this catches it\n", encoding = "utf-8")
        assert comment_gate.main(["--file", str(target)]) == 1
        captured = capsys.readouterr()
        assert "behaviour claim" in captured.err

    def test_empty_file_refused(self, tmp_path, capsys):
        target = tmp_path / "a.test.ts"
        target.write_text("expect(1).toBe(1);\n", encoding = "utf-8")
        assert comment_gate.main(["--file", str(target)]) == 1
        capsys.readouterr()

    def test_non_test_file_refused(self, tmp_path, capsys):
        target = tmp_path / "a.ts"
        target.write_text("// measured\n", encoding = "utf-8")
        assert comment_gate.main(["--file", str(target)]) == 1
        assert "not a test file" in capsys.readouterr().err

    def test_diff_file_lane(self, tmp_path, capsys):
        diff = tmp_path / "p.diff"
        diff.write_text(_add_claim_diff("// catches it"), encoding = "utf-8")
        assert comment_gate.main(["--diff", str(diff)]) == 1
        capsys.readouterr()

    def test_base_and_range_are_exclusive(self, capsys):
        # argparse owns the mutual exclusion and exits 2 (usage error) before
        # any git call happens.
        with pytest.raises(SystemExit) as excinfo:
            comment_gate.main(["--base", "origin/master", "--range", "a..b"])

        assert excinfo.value.code == 2
        capsys.readouterr()

def _git(repo: Path, *args: str) -> str:
    # The workspace's global hooks reject commits outside a registered checkout,
    # so the sandbox repository clears them explicitly (`-c core.hooksPath=`).
    return subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd = repo, capture_output = True, text = True, check = True).stdout

class test_git_lane:
    """The default lane reads ``git diff`` — pinned end to end on a real repository."""

    def test_staged_claim_is_caught(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "master")
        _git(repo, "config", "user.email", "selftest@example.invalid")
        _git(repo, "config", "user.name", "selftest")
        (repo / "a.test.ts").write_text("expect(1).toBe(1);\n", encoding = "utf-8")
        _git(repo, "add", "a.test.ts")
        _git(repo, "commit", "-qm", "base")
        (repo / "a.test.ts").write_text("// measured: it catches the reload\nexpect(1).toBe(1);\n", encoding = "utf-8")
        _git(repo, "add", "a.test.ts")
        assert comment_gate.main(["--staged", "--git-root", str(repo)]) == 1
        captured = capsys.readouterr()
        assert "a.test.ts:1" in captured.out

    def test_staged_anchored_claim_passes(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "master")
        _git(repo, "config", "user.email", "selftest@example.invalid")
        _git(repo, "config", "user.name", "selftest")
        (repo / "a.test.ts").write_text("expect(1).toBe(1);\n", encoding = "utf-8")
        _git(repo, "add", "a.test.ts")
        _git(repo, "commit", "-qm", "base")
        (repo / "a.test.ts").write_text("// measured: it catches the reload\n// mutate: reload-dropped\nexpect(1).toBe(1);\n", encoding = "utf-8")
        _git(repo, "add", "a.test.ts")
        assert comment_gate.main(["--staged", "--git-root", str(repo), "--quiet"]) == 0
        capsys.readouterr()

    def test_unreadable_diff_fails_closed(self, tmp_path, capsys):
        # No git repository at all: refuse with the usage code rather than
        # reporting "nothing found, therefore green".
        empty = tmp_path / "not-a-repo"
        empty.mkdir()
        assert comment_gate.main(["--staged", "--git-root", str(empty)]) == 2
        assert "refusing" in capsys.readouterr().err

    def test_missing_diff_file_fails_closed(self, tmp_path, capsys):
        assert comment_gate.main(["--diff", str(tmp_path / "nope.diff")]) == 2
        capsys.readouterr()

class test_repo_self_proof:
    """A last cheap guard: the module's own CLI is reachable and its help is real."""

    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            comment_gate.main(["--help"])

        assert excinfo.value.code == 0
        assert "lint-comments" in capsys.readouterr().out

    def test_module_importable_from_the_checkout(self):
        assert Path(sys.modules["celestia_devtools.lint.comment_gate"].__file__).is_file()

def _hunk(body: str, *, start: int = 1, count: int = 1, old_count: int = 0) -> str:
    """A hunk whose body is written literally (``+``/``-``/space prefixed)."""
    return f"@@ -{start},{old_count} +{start},{count} @@\n" + body

def _diff(*hunks: str) -> str:
    """A one-file diff built from already-built hunks."""
    return "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n" + "".join(hunks)

def _single_hunk(body: str, *, start: int = 1, count: int = 1, old_count: int = 0) -> str:
    """One-hunk diff: the shape every simple regression uses."""
    return _diff(_hunk(body, start = start, count = count, old_count = old_count))

class test_round2_geometry:
    """Regressions from the independent review round — all four were real.

    Each test constructs the exact diff that failed, so the fix cannot silently
    regress: the assertions are about FILE-line distance, which is the property
    the design names ("上下 10 行内"), not about patch-line arithmetic.
    """

    def test_anchor_beyond_the_window_does_not_bind_across_a_hunk(self):
        # Review round 2: the gap was measured as `start - new_line`, a count of
        # PATCH lines. A hunk declared as one line but carrying ten made a
        # sixteen-file-line gap look like two, and the anchor bound.
        # The claim is file line 1; nine expects follow (2..10); the LAST line
        # of the next hunk is at 17, i.e. SIXTEEN lines below the claim — the
        # distance the pre-round-2 code wrongly bound as "two".
        body = "\n".join(["+expect(0).toBe(0);"] * 9)
        diff = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n" "@@ -1,0 +1,1 @@\n+// measured: this catches it\n" + body + "\n@@ -3,0 +17,1 @@\n+// MUT: far\n"
        report = comment_gate.check_diff(diff)
        assert [claim.line for claim in report.claims] == [1]

    def test_declared_body_smaller_than_actual_does_not_shorten_the_window(self):
        # Same defect, opposite direction: the hunk's declared body ignores how
        # many lines it actually carries.
        claim = _hunk("+// measured: this catches it\n", count = 6)
        filler = "\n".join(["+expect(0);"] * 5)
        anchor = _hunk("+// MUT: far\n", start = 20)
        diff = _diff(claim + filler + "\n" + anchor)
        assert [claim.line for claim in comment_gate.check_diff(diff).claims] == [1]

    def test_a_backwards_hunk_is_not_spliced_onto_the_previous_section(self):
        # A non-monotonic hunk start is a concatenation, not a continuation.
        first = _hunk("+expect(1).toBe(1);\n+expect(2).toBe(2);\n", start = 20, count = 2)
        second = _hunk("+// MUT: early\n")
        third = _hunk("+// measured: this catches it\n+expect(3);\n", start = 30, count = 2)
        diff = _diff(first, second, third)
        assert [claim.line for claim in comment_gate.check_diff(diff).claims] == [30]

    def test_a_content_line_that_starts_with_a_plus_is_not_a_comment(self):
        # Git emits content `+// x` as the hunk line `++// x`. The scanner must
        # read that as the FILE line `+// x` — which is not a comment — instead
        # of treating `// x` as one. The old round-2 fixture (`+++hello`) never
        # exercised this: `"+++hello".startswith("+++ ")` is False, and the
        # round-3 review caught the resulting inert test.
        diff = _single_hunk("++// measured: this pin catches it\n", count = 1)
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 0
        assert report.claims == ()

    def test_the_line_after_a_plus_content_line_is_still_seen(self):
        # The same diff, plus a REAL comment claim on the next line: the first
        # line must not swallow the second, and the claim must be reported at
        # the FILE line (2), not at the patch offset.
        diff = _single_hunk("++// measured: this pin catches it\n+// measured: the real claim\n", count = 2)
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 1
        assert [claim.line for claim in report.claims] == [2]

    def test_an_added_content_line_may_start_with_a_minus(self):
        diff = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n" "@@ -1,0 +1,2 @@\n+--keep this\n+// measured: this pin catches it\n"
        assert [claim.line for claim in comment_gate.check_diff(diff).claims] == [2]

    def test_file_line_numbers_survive_removals_and_context(self):
        # Removals do not advance the NEW file; context does. A claim after a
        # 3-line removal and a 2-line context must report its true file line.
        diff = _single_hunk("-a\n-b\n-c\n expect(1);\n expect(2);\n-x\n+// measured: this catches it\n+y\n", count = 8, old_count = 6)
        report = comment_gate.check_diff(diff)
        assert [claim.line for claim in report.claims] == [3]

    def test_anchor_inside_the_window_binds(self):
        # The mirror of the round-2 fix: a window that is too NARROW would red
        # every anchored claim, so the same geometry is pinned from both sides.
        body = "\n".join(["+expect(0).toBe(0);"] * 9)
        diff = "diff --git a/a.test.ts b/a.test.ts\n--- a/a.test.ts\n+++ b/a.test.ts\n" "@@ -1,0 +1,1 @@\n+// measured: this catches it\n" + body + "\n@@ -11,0 +11,1 @@\n+// MUT: near\n"
        assert comment_gate.check_diff(diff).claims == ()

def _py_diff(body: str, *, count: int, start: int = 1) -> str:
    """A one-hunk diff of a Python test module (``tests/test_x.py``)."""
    return f"diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -{start},0 +{start},{count} @@\n" + body

def _two_hunks(first: str, first_count: int, second_start: int, second: str, second_count: int) -> str:
    """One Python file, two hunks — where a block can open in one and close in the other."""
    return f"diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1,0 +1,{first_count} @@\n{first}@@ -{second_start},0 +{second_start},{second_count} @@\n{second}"

class test_docstrings:
    """Python docstrings are comment text, and the state that decides that lives in the FILE.

    Measured twice on the real chain, and the second measurement is why this
    class has this shape.

    #310 taught the walker to mark a docstring continuation with its own
    sentinel, but it advanced the block state over HUNK lines only. A patch
    shows the lines it changed, so a ``\"\"\"`` that CLOSES a docstring reads as
    one that OPENS it whenever the closing line is a line the walker never fed
    it — and every following line is then judged as prose. hikari's
    artifact-retire branch (``a7c20c3b`` against ``master``) is the standing corpus: its
    ``packages/wasm/component_descriptor_test.py`` opened a docstring in hunk 1
    and closed it in hunk 2 (``+start=42``), so hunk 3 (``+start=363``) ran with
    the state open and its code lines were prefixed as comment text. The
    direction is what makes this urgent: a CODE line read as a comment can be
    refused as an anchorless claim, so the gate fails OPEN on code it should
    ignore.

    #310's own three tests drove ``check_diff`` and were green — they were
    green because the one state they exercised (a block that opens and closes
    inside a single hunk) is exactly the case where hunk-local tracking happens
    to be right. None of them crossed a hunk boundary, which is the defect.
    """

    def test_a_docstring_claim_is_reported(self):
        diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n" "--- a/tests/test_x.py\n" "+++ b/tests/test_x.py\n" "@@ -1,0 +1,3 @@\n" '+        """The shape that distinguishes the two.\n' "+\n" "+        Reverting the filter reds this test.\n" '+        """\n'
        report = comment_gate.check_diff(diff)
        assert len(report.claims) == 1
        assert report.claims[0].line == 3
        assert report.claims[0].render() == "tests/test_x.py:3: Reverting the filter reds this test."

    def test_a_docstring_claim_with_an_anchor_passes(self):
        diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n" "--- a/tests/test_x.py\n" "+++ b/tests/test_x.py\n" "@@ -1,0 +1,4 @@\n" '+        """The shape.\n' "+        Reverting the filter reds this test.\n" "+        MUT: header-filter-removed\n" '+        """\n'
        assert comment_gate.check_diff(diff).claims == ()

    def test_a_single_line_docstring_does_not_leak_state(self):
        # One-line docstrings open and close; the following code must not be
        # read as prose.
        diff = _py_diff('+        """One line."""\n+        assert 1 == 1\n+        expect_that_never_reds_alone()\n', count = 3)
        report = comment_gate.check_diff(diff)
        assert report.scanned_lines == 1
        assert report.claims == ()

    def test_an_unclosed_docstring_refuses_instead_of_marking_code_as_prose(self):
        """A block that never closes is refused, not read as prose to EOF.

        An unterminated triple quote leaves the block open forever, so every
        line after it reads as prose - including ``x = "never"`` inside a
        function body, which the gate then refuses as an anchorless claim.
        Either half of that is a wrong answer: the line is code, and the honest
        verdict is that this gate cannot classify anything after the opener.

        Measured with the refusal removed: scanned=1, hits=0, claims=0 and no
        note (the payload line vanishes into "zero vocabulary hits"). With it:
        scanned=4, hits=1, claims=1 at line 4, plus the never-closed note.
        """
        quote = '"' * 3
        diff = _py_diff("+" + quote + "Header.\n+    prose\n+def f():\n+    x = \"never\"\n", count = 4)
        claims, hits, scanned, notes = comment_gate.scan_diff(diff)
        assert (scanned, hits) == (4, 1), (scanned, hits, claims)
        assert [claim.render() for claim in claims] == ['tests/test_x.py:4: x = "never"'], claims
        assert any("never closed" in note for note in notes), notes

    def test_a_hunk_only_diff_with_a_closed_block_still_judges_claims(self):
        """The other direction: refusing must not become "refuse everything".

        A one-hunk diff whose docstring opens and closes inside the hunk needs
        no more input at all — the state is complete without it. This is the
        control that keeps the new refusal from swallowing every Python diff.
        """
        diff = _py_diff('+"""One hunk, self-contained.\n+    and this one reds\n+"""\n', count = 3)
        report = comment_gate.check_diff(diff)
        assert [claim.line for claim in report.claims] == [2]

#: A throwaway repository whose two commits the gate reads with real ``git``.
#:
#: Every line number in the cases below comes from ``git diff`` — none is
#: written down here. The shape being pinned lives in the FILES (where a
#: docstring opens and closes), and the patch is whatever git says it is, which
#: is the only form that can tell "reads the file" from "walks the hunks".
def _repo_with_change(tmp_path: Path, before: str, after: str, *, path: str = "tests/test_x.py") -> tuple[Path, str]:
    """Commit ``before``, then ``after``; return ``(repo, diff of the two)``.

    ``core.hooksPath`` is pinned empty: a throwaway repository under ``/tmp`` is
    still inside the workspace's hook scope, and the commit is not what these
    cases are about.
    """
    repo = tmp_path / "repo"
    module = repo / path
    module.parent.mkdir(parents = True, exist_ok = True)
    base = subprocess.run(["git", "-C", str(repo), "init", "-q"], capture_output = True, text = True)

    if base.returncode != 0:
        pytest.skip(f"git init unavailable: {base.stderr.strip()}")

    for key, value in (("core.hooksPath", "/dev/null"), ("user.email", "gate@example.invalid"), ("user.name", "gate")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check = True, capture_output = True)

    for text, message in ((before, "base"), (after, "change")):
        module.write_text(text, encoding = "utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check = True, capture_output = True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check = True, capture_output = True)

    diff = subprocess.run(["git", "-C", str(repo), "diff", "-U0", "--no-color", "HEAD~1", "HEAD"], check = True, capture_output = True, encoding = "utf-8").stdout

    return repo, diff

#: A module docstring, a function, a run of constants the patch never needs to
#: show, and then the one comment it changes. The text is assembled from parts
#: rather than written as one literal so this module's own source does not
#: itself contain a Python file whose docstrings a reader would have to count:
#: the cases read the hunk header out of the diff git produced.
_CONSTANTS = "\n".join(f"LINE_{index:02d} = {index}" for index in range(1, 21))

def _module_docstring_file() -> str:
    """The fixture file text: one element per line."""
    parts = ['"""Module docstring."""', "", "import os", "", "", "def helper(name: str) -> str:", '    """Return the name."""', "    return name", "", "", _CONSTANTS, "", "", "def test_the_behaviour():", "    # MUT:helper-returns-name -- dropping the return leaves the helper None", '    assert helper("x") == "x"']

    return "\n".join(parts) + "\n"

def test_the_new_side_revision_is_taken_from_the_lane_that_names_it(tmp_path):
    """`--range A..B` must read B's text, not HEAD's.

    The walker computes the docstring state from the NEW side, so a wrong
    revision is worse than none: it certifies a classification taken from
    another commit. `--staged` describes the index (no commit); a range names
    its own NEW side; `--base` describes HEAD.
    """
    repo, _diff = _repo_with_change(tmp_path, '"""A."""\n', '"""B."""\n')

    def lane(**overrides):
        options = {"git_root": str(repo), "base": None, "range": None}
        options.update(overrides)

        return argparse.Namespace(**options)

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check = True, capture_output = True, encoding = "utf-8").stdout.strip()
    parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"], check = True, capture_output = True, encoding = "utf-8").stdout.strip()
    assert comment_gate._git_revision(lane(range = "HEAD~1..HEAD"), staged = False) == head
    assert comment_gate._git_revision(lane(range = f"{parent}...{head}"), staged = False) == head
    assert comment_gate._git_revision(lane(base = "origin/master"), staged = False) == head
    assert comment_gate._git_revision(lane(), staged = True) is None
    assert comment_gate._git_revision(lane(range = "HEAD..no-such-rev"), staged = False) is None

def test_a_range_reads_its_own_new_side_which_head_cannot_stand_in_for(tmp_path):
    """Reading HEAD's text for `--range A..B` produces a refusal the range does not have.

    The range's NEW side closes its docstring; HEAD (a later commit) leaves one
    open before the same region. Certified against HEAD, the gate reports "the
    docstring ... is never closed" for a diff whose own text has no such thing —
    a refusal about a commit the caller never asked about.

    Measured: the range's own side gives scanned=0 and no docstring note;
    HEAD's text gives a never-closed note. That is why the revision is taken
    from the lane (`--range` -> B, `--base` -> HEAD, `--staged` -> none).
    """
    closed = '"""Header.\nprose line.\n"""\n\n\ndef f():\n    # Reverting this reds the suite\n    return 1\n'
    opened = '"""Header.\n\n"""\n\nDOC = """opens and stays open\n\ndef f():\n    # Reverting this reds the suite\n    return 3\n'
    repo = tmp_path / "repo"
    module = repo / "tests" / "test_x.py"
    module.parent.mkdir(parents = True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check = True, capture_output = True)

    for key, value in (("core.hooksPath", "/dev/null"), ("user.email", "gate@example.invalid"), ("user.name", "gate")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check = True, capture_output = True)

    for text, message in ((closed, "base"), (closed.replace("return 1", "return 2"), "range-new-side"), (opened, "head")):
        module.write_text(text, encoding = "utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check = True, capture_output = True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check = True, capture_output = True)

    def revspec(spec: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", spec], check = True, capture_output = True, encoding = "utf-8").stdout.strip()

    diff = subprocess.run(["git", "-C", str(repo), "diff", "-U0", "--no-color", "HEAD~2..HEAD~1"], check = True, capture_output = True, encoding = "utf-8").stdout
    from_range = comment_gate.check_diff(diff, root = repo, rev = revspec("HEAD~1"))
    from_head = comment_gate.check_diff(diff, root = repo, rev = revspec("HEAD"))
    assert not any("never closed" in problem for problem in from_range.problems), from_range.problems
    assert any("never closed" in problem for problem in from_head.problems), from_head.problems

def test_a_claim_never_carries_the_walkers_own_markers():
    """A report line must be readable: the internal sentinels never reach it.

    The walker marks a context line with ``CONTEXT_MARK`` (NUL) and a prose line
    with ``DOC_CONTINUATION`` (SOH); a docstring continuation inside a context
    run carries BOTH, in that order. ``claim.text`` is what a reader sees through
    ``render()``, so a raw control character there is unreadable in a terminal
    and invisible in a log. Measured before the fix: ``tests/x.py:2: \x00...``.

    The line below is deliberately built to carry both markers, so deleting
    either strip reds this case (the round-2 mutation that stripped only the
    doc marker SURVIVED the whole file — this is what pins it).
    """
    marked = comment_gate.CONTEXT_MARK + comment_gate.DOC_CONTINUATION + "Reverting the filter reds this test."
    diff = "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n" "@@ -1,4 +1,4 @@\n" ' """Header.\n' " Reverting the filter reds this test.\n" ' """\n' " x = 1\n"
    claims, _hits, _scanned, _notes = comment_gate.scan_diff(diff)
    assert [entry.line for entry in claims] == [2], claims
    control = [ord(char) for char in marked if char in (comment_gate.CONTEXT_MARK, comment_gate.DOC_CONTINUATION)]
    assert control, "the fixture must carry both markers"
    assert comment_gate._claim_text(marked) == "Reverting the filter reds this test."
    assert comment_gate._claim_text(comment_gate.DOC_CONTINUATION + marked) == "Reverting the filter reds this test."
    # The other arrival order too: a context line whose text starts with the doc
    # marker. One pass in the wrong order leaves a marker behind (measured), so
    # this pins the loop rather than trusting "it is order-independent".
    assert comment_gate._claim_text(comment_gate.CONTEXT_MARK + comment_gate.DOC_CONTINUATION + "x") == "x"

    for entry in claims:
        assert not any(ord(char) < 32 for char in entry.text), entry.text
        assert not any(ord(char) < 32 for char in entry.render()), entry.render()

def _hunk_starts(diff: str) -> list[int]:
    """The NEW-side start line of every hunk in ``diff``."""
    starts: list[int] = []

    for raw in diff.splitlines():
        if raw.startswith("@@"):
            starts.append(int(raw.split("+")[1].split(",")[0].split(" ")[0]))

    return starts

class test_docstring_state_reads_the_whole_file:
    """The gate must judge a file by the FILE, not by the lines a patch shows.

    #310's walker advanced the docstring state over HUNK lines only. A patch
    shows what it changed, so a ``\"\"\"`` that CLOSES a docstring in a line the
    patch never shows reads as one that OPENS it, and every following line is
    judged as prose. hikari's artifact-retire branch is the standing corpus:
    the gate marked 8 more lines of ``packages/wasm/component_descriptor_test.py``
    as prose than the file has, and — because a code line read as prose can be
    refused as an anchorless claim — it refused to merge that PR at all.

    **No case here hand-computes a line number.** Each one writes two versions
    of a real file into a throwaway repository, asks git for the diff, and
    asserts on what the gate does with it. The earlier revision of this fix
    shipped fixtures with hand-written hunk headers and hand-counted payload
    lines; four of them were wrong in the same way the code was, so they
    agreed with each other and proved nothing.
    """

    def test_a_docstring_that_closes_between_hunks_keeps_the_file_in_scope(self, tmp_path):
        """A block opened ABOVE the patch and closed BELOW hunk 1 is prose — and ends there.

        The file opens a module docstring, and the patch edits one sentence
        INSIDE it (hunk 1) and one code line far below (hunk 2). Neither hunk
        shows the opening or the closing quote: they sit in lines the patch
        never touches, which is the ordinary shape of an edit.

        What the gate must do is judge the whole file: the edited sentence is
        prose, so it keeps claim candidacy, and it is reported (it names
        behaviour — ``reds`` — with no mutation anchor in reach). A walker that
        sees only the hunks never learns the docstring opened at line 1, so it
        reads hunk 1 as code and scans NOTHING — silently dropping the one
        comment the change adds.

        Measured: with the file text, scanned=1, hits=1, and the claim is the
        edited sentence; without it, scanned=0 and a note about line 1.
        """
        before = '"""Module docstring.\n\n\nA sentence that reds nothing at all.\n\nSpans several lines so the quotes are\nnowhere near what the patch touches.\n"""\n\n' + _CONSTANTS + "\n\n\ndef test_a():\n    assert 1 == 1\n    payload = never_reds_alone\n"
        after = before.replace("A sentence that reds nothing at all.", "A sentence that reds nothing at all, revised.").replace("    assert 1 == 1\n", "    assert 2 == 2\n")
        repo, diff = _repo_with_change(tmp_path, before, after)
        starts = _hunk_starts(diff)
        assert len(starts) == 2, diff
        read = comment_gate.check_diff(diff, root = repo, rev = "HEAD")
        blind = comment_gate.check_diff(diff)
        assert read.scanned_lines == 1, read
        # The claim is the edited SENTENCE, on the line git says hunk 1 starts
        # on — the file's own line numbers, never a count written down here.
        # ``.text`` carries the continuation marker the docstring walk puts on a
        # prose line with no opener of its own, so ``.strip()`` reads the claim.
        assert [(claim.line, claim.text.removeprefix(comment_gate.DOC_CONTINUATION).strip()) for claim in read.claims] == [(starts[0], "A sentence that reds nothing at all, revised.")], (starts, read.claims)
        assert blind.scanned_lines == 0, blind

    def test_a_first_hunk_that_starts_late_is_judged_not_refused(self, tmp_path):
        """THE anti-over-refusal pin: a file whose first hunk starts past line 1.

        A module docstring at the top of the file, twenty lines of constants,
        and then the only thing the patch changes. Almost every real edit has
        this shape, and a hunk-only walk refuses every one of them with "line 1
        is not shown by the patch" — which is how an earlier revision of this
        fix would have blocked nearly every PR.

        The gate must JUDGE this diff: the docstring claim inside the hunk is
        reported (it is prose with no mutation anchor), and nothing is refused
        for want of the lines above it.
        """
        after = _module_docstring_file().replace("    # MUT:helper-returns-name -- dropping the return leaves the helper None\n", "    # Reverting the return reds this test and nothing else would notice.\n")
        repo, diff = _repo_with_change(tmp_path, _module_docstring_file(), after)
        starts = _hunk_starts(diff)
        assert starts and (min(starts) > 1), f"the case needs a first hunk past line 1: {starts}"
        report = comment_gate.check_diff(diff, root = repo, rev = "HEAD")
        assert not any("not shown by the patch" in problem for problem in report.problems), report.problems
        # The claim's line comes from the patch itself: one comment line, at the
        # hunk start — never a number counted in this file.
        message = "Reverting the return reds this test and nothing else would notice."
        assert [(claim.line, claim.text) for claim in report.claims] == [(starts[0], f"# {message}")], (starts, report.claims)

    def test_a_triple_quote_inside_a_string_literal_opens_no_block(self, tmp_path):
        """The fail-open in the other direction: a QUOTED delimiter is data.

        A test file that asserts about docstring delimiters spells them inside
        a string — ``re.search(r"(\'\'\'|\"\"\")[^\n]*", SHIM)``. Reading that
        as an opener left a block open for the rest of the file, so every later
        line was judged as prose and the gate refused the file with "the
        docstring ... is never closed". Measured on this repository's own
        ``tests/test_pyshim.py`` (line 53), which is a valid Python module whose
        last line is code.

        Both delimiters, both quoting styles, and the escaped form the real
        file uses must all read as data; a genuine opener on the same line
        shape must still open.
        """
        before = "x = 1\n\n\ndef t():\n" + '    assert not re.search(r"(\'\'\'|\\"\\"\\")[^\\n]*", S)\n' + "    # placeholder\n"
        after = before.replace("# placeholder", "# Reverting this reds the suite")
        repo, diff = _repo_with_change(tmp_path, before, after)
        report = comment_gate.check_diff(diff, root = repo, rev = "HEAD")
        # The file really does close every quote, so nothing may be refused for
        # want of a closer — that refusal is the bug this pins.
        assert not any("never closed" in problem for problem in report.problems), report.problems
        # And the comment claim in the same file is still found and judged.
        assert [claim.line for claim in report.claims] == [len(before.splitlines())], (report.claims, before)

    def test_a_triple_quote_inside_a_string_literal_is_not_an_opener(self):
        """The unit shape of the same rule, both delimiters and both quote kinds."""
        triple_double = '"' * 3
        triple_single = "'" * 3
        # data, never an opener
        assert comment_gate._doc_open_at('assert not re.search(r"(' + triple_single + '|", S)') == ""
        assert comment_gate._doc_open_at("x = 'a " + triple_double + " b'") == ""
        assert comment_gate._doc_open_at('x = "' + triple_single + '"') == ""
        # a real opener of the same shape still opens
        assert comment_gate._doc_open_at('    ' + triple_double + 'Return the name.' + triple_double) == triple_double
        assert comment_gate._doc_open_at('PURE = ' + triple_double + 'pkg') == triple_double

    def test_an_unterminated_docstring_in_the_file_still_refuses(self, tmp_path):
        """The fail-closed half survives the whole-file read.

        The FILE really leaves its triple quote open. Reading the whole file
        does not make that knowable — it makes it certain — so the gate refuses
        and names the file, and the code line after the opener is judged on its
        own classification rather than silently becoming prose.
        """
        before = '"""Closed header."""\n\n\ndef f():\n    return 1\n'
        after = '"""Header that is never closed.\n\n\ndef f():\n    x = "never"\n    return 1\n'
        repo, diff = _repo_with_change(tmp_path, before, after)
        report = comment_gate.check_diff(diff, root = repo, rev = "HEAD")
        assert not report.ok, report
        # The note is a PROBLEM of its own, not the anchorless-claim listing —
        # which quotes the offender's text and so also contains "never closed"
        # (a filter on that phrase alone passed with the note deleted; measured).
        notes = [problem for problem in report.problems if "is never closed in the file" in problem]
        assert notes, report.problems
        assert notes[0].startswith("tests/test_x.py:"), notes
