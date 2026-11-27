#!/usr/bin/env python3
"""Tests for the guarded PR body editor (``celestia_devtools.vcs.pr_edit``) and
the ``gh`` proxy interception that routes ``pr edit --body-file`` into it.

Every refusal the design names has a red here (``--repo`` missing, a generic
body path, a marker absent after the edit, a body that came back identical, a
title/head that moved, an unreadable PR, a failed ``gh pr edit``) and the
happy path has a green driven through the same fake transport.  The proxy tests
pin the *scoping*: only ``pr edit … --body-file`` is intercepted, every other
shape still reaches the real ``gh``, and ``CELESTIA_PR_EDIT_BYPASS=1`` forwards
with the warning printed.
"""

import json
import os
import re
import shutil
import subprocess
import pytest
from celestia_devtools.vcs import gh as gh_proxy
from celestia_devtools.vcs import pr_edit
from celestia_devtools.vcs.pr_edit import BYPASS_ENV, EXIT_REFUSED, EXIT_USAGE, body_path_problems, bypass_active, guarded_edit, main, read_pr_state, repo_problems

REPO = "celestia-island/hikari"
PR = 1586
HEAD = "a" * 40
MARKER = "verify: rounds=1"
BASE = {"title": "✨ Add the standalone error landing page asset.", "body": "old body\n", "headRefName": "feat-landing", "headRefOid": HEAD}
NEW = "# Body\n\nverify: rounds=1 clean=0\n"

class _probe:
    def __init__(self, rc = 0, stdout = "", stderr = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr

class fake_gh:
    """A stand-in for the ``gh`` binary with scripted views and one recorded edit."""

    def __init__(self, *, edit_rc = 0, view_rc = 0, after: dict | None = None):
        self.edit_rc = edit_rc
        self.view_rc = view_rc
        self.after = after
        self.calls: list[list[str]] = []
        self.views = 0

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        action = argv[2] if len(argv) > 2 else ""

        if action == "view":
            self.views += 1

            if self.view_rc:
                return _probe(rc = self.view_rc, stderr = "gh: simulated failure")

            return _probe(stdout = json.dumps(BASE if self.views == 1 else (self.after or BASE)))

        if action == "edit":
            if self.edit_rc:
                return _probe(rc = self.edit_rc, stderr = "gh: simulated edit failure")

            written = open(argv[argv.index("--body-file") + 1], encoding = "utf-8").read()

            if self.after is None:
                self.after = {**BASE, "body": written}

            return _probe()

        return _probe(rc = 127, stderr = f"fake gh: unexpected {argv}")

    @property
    def edits(self) -> list[list[str]]:
        return [c for c in self.calls if (len(c) > 2) and (c[2] == "edit")]

@pytest.fixture
def body(tmp_path):
    def make(name = "hikari-1586-pr-body.md", text = NEW, root = None) -> str:
        path = (root or tmp_path) / name
        path.write_text(text, encoding = "utf-8")
        return str(path)

    return make

def test_missing_repo_refused():
    assert any("mandatory" in p for p in repo_problems(None))
    assert any("mandatory" in p for p in repo_problems(""))

def test_bare_name_refused():
    # A bare name is not a destination: it would resolve against whatever
    # owner the local gh config has, which is the ambiguity the flag exists
    # to remove.
    assert repo_problems("hikari")
    assert repo_problems("celestia-island/")

@pytest.mark.parametrize("repo", [REPO, "celestia-island/celestia-devtools", "org/repo.name", "a/b_c"])
def test_owner_name_accepted(repo):
    assert repo_problems(repo) == []

def test_generic_path_refused():
    # The 2026-11-27 face verbatim: a shared scratch name two lanes write.
    problems = body_path_problems("/tmp/pr-body.md", REPO, PR)
    assert problems and ("does not name the repo" in problems[0])

@pytest.mark.parametrize("path", ["hikari-pr-body.md", "chest-2866-pr-body.md", "hikari-9999-pr-body.md", "/dev/stdin", ""])
def test_unowned_or_foreign_path_refused(path):
    assert body_path_problems(path, REPO, PR)

@pytest.mark.parametrize("path", ["/tmp/hikari-1586-pr-body.md", "/tmp/pr-1586-hikari.md", "celestia-island-hikari-1586-pr-body.md", "HIKARI_1586_body.md", "hikari.1586.body.md"])
def test_owned_path_accepted(path):
    assert body_path_problems(path, REPO, PR) == []

def test_number_prefix_is_not_a_match():
    # `15861` contains `1586`; a substring test would accept a body meant
    # for a different PR of the same repo.
    assert body_path_problems("hikari-15861-pr-body.md", REPO, PR)

def test_other_numbers_and_repos_do_not_satisfy():
    assert body_path_problems("/tmp/hikari-42-pr-body.md", REPO, PR)
    assert body_path_problems("/tmp/chest-1586-pr-body.md", REPO, PR)

def test_transport_resolves_the_real_binary(body, monkeypatch):
    # Under the documented `gh() { celestia-devtools gh "$@"; }` alias a bare
    # "gh" would re-enter the proxy; the transport must ask the proxy module
    # for the real path instead. MUT:transport-bare-gh (a bare ["gh", ...]
    # argv) reds this.
    seen: list[list[str]] = []

    def recorder(argv, **_kwargs):
        seen.append(list(argv))

        class _p:
            returncode = 0
            stdout = json.dumps(BASE)
            stderr = ""

        return _p()

    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/opt/fake/gh-binary")
    read_pr_state(REPO, PR, runner = recorder)
    assert seen and (seen[0][0] == "/opt/fake/gh-binary"), seen

def test_positive_edit_verified(body):
    runner = fake_gh()
    assert guarded_edit(REPO, PR, body(), MARKER, expect_head = HEAD, runner = runner) == 0
    assert len(runner.edits) == 1
    # The edit names the repo itself — the proxy must not re-derive one.
    assert ("--repo" in runner.edits[0]) and (REPO in runner.edits[0])

def test_missing_repo_writes_nothing(body):
    runner = fake_gh()
    assert guarded_edit(None, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert runner.calls == []

def test_generic_body_path_writes_nothing(tmp_path):
    path = tmp_path / "pr-body.md"
    path.write_text(NEW, encoding = "utf-8")
    runner = fake_gh()
    assert guarded_edit(REPO, PR, str(path), MARKER, runner = runner) == EXIT_REFUSED
    assert runner.calls == []

def test_marker_absent_from_file_writes_nothing(body):
    runner = fake_gh()
    assert guarded_edit(REPO, PR, body(text = "no marker\n"), MARKER, runner = runner) == EXIT_REFUSED
    assert runner.edits == []

def test_marker_absent_after_edit_refused(body, capsys):
    runner = fake_gh(after = {**BASE, "body": "another lane's body\n"})
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "does not contain" in err
    # Recovery material: both bodies' first lines, so the operator can see
    # which one the PR carries now without re-reading either.
    assert ("BEFORE this edit" in err) and ("first line NOW" in err)

def test_unchanged_body_refused(body, capsys):
    runner = fake_gh(after = dict(BASE))
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert "identical to the pre-edit snapshot" in capsys.readouterr().err

def test_title_moved_refused(body, capsys):
    runner = fake_gh(after = {**BASE, "body": NEW, "title": "probe-x"})
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert "title moved" in capsys.readouterr().err

def test_branch_moved_refused(body, capsys):
    runner = fake_gh(after = {**BASE, "body": NEW, "headRefName": "other"})
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert "headRefName moved" in capsys.readouterr().err

def test_head_moved_after_edit_refused(body, capsys):
    runner = fake_gh(after = {**BASE, "body": NEW, "headRefOid": "c" * 40})
    assert guarded_edit(REPO, PR, body(), MARKER, expect_head = HEAD, runner = runner) == EXIT_REFUSED
    assert "head moved during a body edit" in capsys.readouterr().err

def test_head_moved_without_expect_head_still_refused(body, capsys):
    # Without --expect-head there is no PRE-edit pin, but a body edit still
    # cannot move the head: the post-edit comparison is unconditional.
    runner = fake_gh(after = {**BASE, "body": NEW, "headRefOid": "c" * 40})
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert "head moved during a body edit" in capsys.readouterr().err

def test_expect_head_mismatch_refused_before_the_write(body):
    runner = fake_gh()
    assert guarded_edit(REPO, PR, body(), MARKER, expect_head = "b" * 40, runner = runner) == EXIT_REFUSED
    assert runner.edits == []

def test_short_expect_head_prefix_accepted(body):
    runner = fake_gh()
    assert guarded_edit(REPO, PR, body(), MARKER, expect_head = "a" * 10, runner = runner) == 0

def test_unreadable_pr_refused_not_certified(body):
    runner = fake_gh(view_rc = 1)
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED
    assert runner.edits == []

def test_failed_edit_refused(body):
    runner = fake_gh(edit_rc = 1)
    assert guarded_edit(REPO, PR, body(), MARKER, runner = runner) == EXIT_REFUSED

def test_dry_run_writes_nothing(body):
    runner = fake_gh()
    assert guarded_edit(REPO, PR, body(), MARKER, dry_run = True, runner = runner) == 0
    assert runner.edits == []

def test_selftest_is_green():
    assert pr_edit.selftest() == 0

def test_missing_repo_refused_by_the_guard(body):
    assert main(["--pr", str(PR), "--body-file", body(), "--expect-marker", MARKER]) == EXIT_REFUSED

def test_missing_pr_is_usage(body):
    assert main(["--repo", REPO, "--body-file", body(), "--expect-marker", MARKER]) == EXIT_USAGE

def test_help_documents_the_bypass(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    assert BYPASS_ENV in capsys.readouterr().out

# ── proxy interception ──────────────────────────────────────────────────────

#: EVERY spelling `gh pr edit --help` documents for writing a body. This list
#: IS the contract: review R2 found the short `-F` form silently forwarded, and
#: `test_other_shapes_are_not_intercepted` could not catch it because it only
#: asserted the long spelling (MUT:narrow-proxy-to-long-flag reds the eight
#: parametrized spelling cases and the four short-flag proxy controls, twelve
#: in all; `test_other_shapes_are_not_intercepted` itself stays green, because
#: it asserts the shapes that only carry the long flag). A new alias must be
#: added here and to
#: `gh_proxy.BODY_FILE_FLAGS` / `BODY_INLINE_FLAGS` together; the
#: `gh pr edit --help` comparison below pins this list against the real binary.
BODY_WRITE_SPELLINGS = tuple((["--body-file", "/tmp/hikari-1586-pr-body.md"], ["--body-file=/tmp/hikari-1586-pr-body.md"], ["-F", "/tmp/hikari-1586-pr-body.md"], ["-F=/tmp/hikari-1586-pr-body.md"], ["-F/tmp/hikari-1586-pr-body.md"], ["-F", "-"], ["--body", "inline text"], ["--body=inline text"], ["-b", "inline text"], ["-binline text"]))

#: ``gh pr edit --help`` lists one flag per line as ``  -x, --long TYPE  text``.
#: Both halves are captured: a flag's SHORT alias is what a script actually
#: types, so a table that only machine-checks long names can be bypassed by a
#: future `-W, --body-file` (review R4's reproduction).
_GH_HELP_FLAG = re.compile(r"^\s+-(?P<short>[A-Za-z]),\s--(?P<long>[a-z-]+)(?:=|\s)", re.MULTILINE)

def _gh_pr_edit_body_flags(help_text: str) -> set[str]:
    """Both spellings (``-b`` and ``--body``) of every flag that writes the body.

    The body flags are the ones whose help text after the flag name says
    "body": ``-b, --body string Set the new body.`` and ``-F, --body-file file
    Read body text from file …``. Long AND short names are returned, because
    the short alias is the bypass surface review R4 demonstrated.
    """
    found: set[str] = set()

    for line in help_text.splitlines():
        match = _GH_HELP_FLAG.match(line)

        if not match:
            continue

        # Split on the FLAG TOKEN, not on the long name: a line whose
        # description merely says "alias" must still be recognised as a body
        # flag, and one that only names a different flag in its prose must not.
        description = line.split(match.group("long"), 1)[-1].lower()

        if "body" in description:
            found.add("-" + match.group("short"))
            found.add("--" + match.group("long"))

    return found

def _declared_body_flags() -> set[str]:
    """Every flag spelling the test table and the proxy's tables declare."""
    declared = set(gh_proxy.BODY_FILE_FLAGS) | set(gh_proxy.BODY_INLINE_FLAGS)

    for spelling in BODY_WRITE_SPELLINGS:
        token = spelling[0]

        if token.startswith("--"):
            declared.add(token.split("=", 1)[0])
        elif token.startswith("-") and (len(token) > 2) and not token.startswith("--"):
            declared.add(token[:2])
        elif token.startswith("-") and (len(token) == 2):
            declared.add(token)

    return declared

@pytest.mark.skipif(shutil.which("gh") is None, reason = "gh not installed — the --help comparison needs the real binary (skipped, not failed: a checkout without gh is still a valid dev environment)")
def test_the_spelling_table_matches_gh_pr_edit_help():
    """The declared spellings must equal what `gh pr edit --help` documents.

    Guard the guard: the table is a literal and the proxy tables are literals,
    so a gh release adding a body alias — long OR short — would leave them
    stale while the suite stayed green (review R4's `-W, --body-file`
    reproduction). Parsing the real help closes it; the parser is proven on a
    known-positive sample first, including a foreign short alias, so a broken
    capture cannot pass vacuously.
    """
    sample = "  -b, --body string             Set the new body.\n  -F, --body-file file          Read body text from file (use \"-\" to read from standard input)\n  -W, --body-file file          Also reads body text from a file.\n  -t, --title string            Set the new title.\n"
    assert _gh_pr_edit_body_flags(sample) == {"-b", "--body", "-F", "--body-file", "-W"}

    help_text = subprocess.run(["gh", "pr", "edit", "--help"], capture_output = True, text = True, check = False).stdout
    assert help_text, "gh pr edit --help produced no output"

    documented = _gh_pr_edit_body_flags(help_text)
    assert documented == {"-b", "--body", "-F", "--body-file"}, f"gh now documents other body flags: {sorted(documented)}"
    assert _declared_body_flags() == documented, f"declared body flags {sorted(_declared_body_flags())} != gh's {sorted(documented)} — a spelling the proxy does not intercept"

def test_the_flag_parser_sees_a_new_short_alias():
    """The R4 reproduction: a future short alias must change the parsed set.

    A help text where `--body-file` also answers to `-W` must come back with
    `-W` in the set; that is the signal `_declared_body_flags()` is compared
    against, so a new alias reds the comparison above instead of slipping past.
    """
    fake = "  -b, --body string             Set the new body.\n  -F, --body-file file          Read body text from file\n  -W, --body-file file          Also reads body text from a file\n"
    assert "-W" in _gh_pr_edit_body_flags(fake)
    assert "-W" not in _declared_body_flags(), "the proxy must not claim an alias it does not intercept"

@pytest.mark.parametrize("spelling", BODY_WRITE_SPELLINGS)
def test_every_body_write_spelling_is_intercepted(spelling):
    assert gh_proxy._is_guarded_pr_edit(["pr", "edit", "1586", *spelling]), spelling

@pytest.mark.parametrize("spelling", [["--body-file", "/tmp/x"], ["-F", "/tmp/x"], ["-F=/tmp/x"], ["-b", "x"], ["-bx"]])
def test_every_body_write_spelling_reaches_the_guard(spelling):
    # Not just "intercepted": the translated argv must carry the body to the
    # guard, in the flag the guard reads (so it refuses `-F /tmp/pr-body.md`
    # for the path, not for a missing flag).
    translated = gh_proxy._guard_argv(["gh", "pr", "edit", "1586", *spelling])
    assert ("--pr" in translated) and (translated[translated.index("--pr") + 1] == "1586")
    path_like = spelling[0].startswith("-F") or spelling[0].startswith("--body-file")
    assert (("--body-file" in translated) if path_like else ("--body" in translated))

def test_the_short_body_flag_is_not_forwarded_raw():
    # The R2 face verbatim: `gh pr edit 123 -F /tmp/pr-body.md` used to be
    # forwarded untouched, which is the 2026-11-27 shared-path write.
    assert gh_proxy._is_guarded_pr_edit(["pr", "edit", "123", "-F", "/tmp/pr-body.md"]) is True

@pytest.mark.parametrize("argv", [["pr", "edit", "1586", "--title", "x"], ["pr", "edit", "1586", "-l", "bug"], ["pr", "edit", "1586", "--add-label", "x"], ["pr", "edit", "1586", "-t", "x"], ["pr", "view", "1586", "--body-file", "/tmp/x.md"], ["pr", "close", "1586", "--body-file", "/tmp/x.md"], ["pr", "brief"], ["pr"], [], ["issue", "edit", "1", "--body-file", "/tmp/x.md"]])
def test_other_shapes_are_not_intercepted(argv):
    assert not gh_proxy._is_guarded_pr_edit(argv)

def test_proxy_refuses_the_incident_shape(monkeypatch, tmp_path, capsys):
    # 2026-11-15 verbatim: `gh pr edit <N> --body-file …` with no --repo.
    # The proxy must not forward it to the host gh binary.
    calls: list = []
    path = tmp_path / "cherino-42-pr-body.md"
    path.write_text(NEW, encoding = "utf-8")
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", "42", "--body-file", str(path), "--expect-marker", MARKER])

    assert gh_proxy.main() == EXIT_REFUSED
    assert [c for c in calls if (c and (c[0] == "/usr/bin/gh"))] == []
    assert "PR-EDIT REFUSED (repo)" in capsys.readouterr().err
    assert not any("gh pr view" in " ".join(c) for c in calls)

def test_proxy_with_repo_reaches_the_guard_not_gh(monkeypatch, tmp_path, capsys):
    calls: list = []
    path = tmp_path / "hikari-1586-pr-body.md"
    path.write_text(NEW, encoding = "utf-8")
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", str(PR), "--repo", REPO, "--body-file", str(path), "--expect-marker", MARKER])

    # The guard refuses the cross-repo-shaped invocation (no --expect-head is
    # fine, but the body path names hikari-1586 while REPO says hikari: this
    # passes the path guard) — the point of the control is that the gh
    # binary was NOT invoked and the refusal came from the guard.
    rc = gh_proxy.main()
    assert [c for c in calls if (c and (c[0] == "/usr/bin/gh"))] == []
    assert rc in (0, EXIT_REFUSED)
    assert "usage: celestia-devtools pr-edit" not in capsys.readouterr().err

@pytest.mark.parametrize("argv,expected", [(["gh", "pr", "edit", "1586", "--repo", "o/r", "--body-file", "p", "--expect-marker", "m"], ["--pr", "1586", "--repo", "o/r", "--expect-marker", "m", "--body-file", "p"]), (["gh", "pr", "edit", "1586", "--body-file=p"], ["--pr", "1586", "--body-file", "p"]), (["gh", "pr", "edit", "--repo", "o/r", "42", "--body-file", "p"], ["--pr", "42", "--repo", "o/r", "--body-file", "p"])])
def test_argv_translation(argv, expected):
    assert gh_proxy._guard_argv(argv) == expected

def test_proxy_refuses_the_short_flag_shared_path(monkeypatch, tmp_path, capsys):
    # Pin the working directory too: `pr-edit` resolves --body-file relative to
    # it, and a suite run that moved it would change which path the guard sees.
    monkeypatch.chdir(tmp_path)
    # `gh pr edit <N> -F /tmp/pr-body.md` — the 2026-11-27 shape through gh's
    # documented short flag. The host gh must never be spawned
    # (MUT:narrow-proxy-to-long-flag: the `-F` argv stops being intercepted).
    calls: list = []
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    # No --expect-marker here on purpose: a raw `gh` never passes one, and the
    # guard's incident refusals must fire ahead of the usage gate
    # (MUT:usage-gate-before-guard: the marker check answers first and this reds).
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", "42", "-F", "/tmp/pr-body.md"])

    # No --repo either — that is the 11-15 half of the shape. The guard names
    # the missing repo before it can complain about the missing marker, so this
    # stays a guard refusal (exit 1) rather than a usage error.
    rc = gh_proxy.main()
    err = capsys.readouterr().err
    assert rc == EXIT_REFUSED, err
    assert [c for c in calls if (c and (c[0] == "/usr/bin/gh"))] == []
    assert "PR-EDIT REFUSED (repo)" in err

def test_proxy_refuses_the_short_flag_shared_path_with_a_repo(monkeypatch, tmp_path, capsys):
    # Same shape WITH --repo: the body-path guard is the one that fires, so the
    # short flag cannot carry a shared scratch path past the ownership check.
    calls: list = []
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", "42", "-F", "/tmp/pr-body.md", "--repo", REPO, "--expect-marker", MARKER])

    rc = gh_proxy.main()
    err = capsys.readouterr().err
    assert rc == EXIT_REFUSED, err
    assert [c for c in calls if (c and (c[0] == "/usr/bin/gh"))] == []
    assert "PR-EDIT REFUSED (body-file)" in err

def test_proxy_refuses_an_inline_body(monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", str(PR), "--repo", REPO, "-b", "inline body", "--expect-marker", MARKER])

    assert gh_proxy.main() == EXIT_REFUSED
    assert [c for c in calls if (c and (c[0] == "/usr/bin/gh"))] == []
    assert "PR-EDIT REFUSED (inline body)" in capsys.readouterr().err

def test_bypass_forwards_with_a_warning(monkeypatch, capsys):
    calls: list = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return _probe()

    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", fake_run)
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", str(PR), "--body-file", "/tmp/pr-body.md"])
    monkeypatch.setenv(BYPASS_ENV, "1")

    assert gh_proxy.main() == 0
    assert calls and (calls[0][0] == "/usr/bin/gh") and ("edit" in calls[0])
    err = capsys.readouterr().err
    assert ("BYPASSED" in err) and (BYPASS_ENV in err)

def test_plain_edit_still_forwards(monkeypatch):
    calls: list = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return _probe()

    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", fake_run)
    monkeypatch.setattr(gh_proxy.sys, "argv", ["gh", "pr", "edit", str(PR), "--title", "x"])

    assert gh_proxy.main() == 0
    assert calls == [["/usr/bin/gh", "pr", "edit", str(PR), "--title", "x"]]

@pytest.mark.parametrize("value,expected", [("1", True), ("yes", True), ("true", True), ("TRUE", True), ("", False), ("0", False), ("no", False), ("false", False)])
def test_bypass_active_parsing(monkeypatch, value, expected):
    monkeypatch.setenv(BYPASS_ENV, value)
    assert bypass_active() is expected

def test_bypass_unset_by_default(monkeypatch):
    monkeypatch.delenv(BYPASS_ENV, raising = False)
    assert bypass_active() is False
    assert os.environ.get(BYPASS_ENV) is None
