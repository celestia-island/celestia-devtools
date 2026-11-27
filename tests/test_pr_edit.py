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
import pytest
from celestia_devtools.vcs import gh as gh_proxy
from celestia_devtools.vcs import pr_edit
from celestia_devtools.vcs.pr_edit import BYPASS_ENV, EXIT_REFUSED, EXIT_USAGE, body_path_problems, bypass_active, guarded_edit, main, repo_problems

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
#: asserted the long spelling. A new alias must be added here and to
#: `gh_proxy.BODY_FILE_FLAGS` / `BODY_INLINE_FLAGS` together.
BODY_WRITE_SPELLINGS = tuple((["--body-file", "/tmp/hikari-1586-pr-body.md"], ["--body-file=/tmp/hikari-1586-pr-body.md"], ["-F", "/tmp/hikari-1586-pr-body.md"], ["-F=/tmp/hikari-1586-pr-body.md"], ["-F/tmp/hikari-1586-pr-body.md"], ["-F", "-"], ["--body", "inline text"], ["--body=inline text"], ["-b", "inline text"], ["-binline text"]))

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
    # documented short flag. The host gh must never be spawned.
    calls: list = []
    monkeypatch.setattr(gh_proxy, "_real_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(gh_proxy.subprocess, "run", lambda *a, **k: calls.append(a) or _probe())
    # No --expect-marker here on purpose: a raw `gh` never passes one, and the
    # guard's incident refusals must fire ahead of the usage gate.
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
