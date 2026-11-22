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
