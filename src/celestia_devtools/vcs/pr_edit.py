#!/usr/bin/env python3
"""Guarded ``pr edit --body-file`` — a PR body edit that proves what it wrote.

*Why this exists:* the org has had **two recorded PR-body cross-writes**, and
neither was stopped by a convention that every session had already agreed to
(2026-11-15 ``cherino#42`` was overwritten by another repo's ``hikari`` perfsec
body; 2026-11-27 ``hikari#1586`` was overwritten by ``chest#2866``'s content
through a shared ``/tmp/pr-body.md``).  Both sessions *believed* they were
compliant, which is the point: **compliance is conventional, not mechanical.**
A rule that lives only in each session's memory is a rule with no enforcement
path.  This command turns the two incident shapes into refusals the shell can
see:

1. **Repo is mandatory and explicit** (``--repo OWNER/NAME``, no fallback to the
   cwd's ``origin``).  The 11-15 write happened because a scripted
   ``gh pr edit <N> --body-file …`` ran without ``--repo`` in a checkout of a
   *different* repository, so PR number ``42`` resolved to ``cherino#42``
   instead of the intended PR.  A missing ``--repo`` is a refusal, never a
   guess: there is no cwd in which "the PR the caller meant" is knowable.
2. **The body file is owned by this target alone** (11-27).  The file's
   basename must carry the repo's name and the PR number (``hikari-1586-pr-body.md``,
   ``pr-2988-celestia-devtools-pr-body.md`` — either order, any separator).  A
   generic scratch path (``/tmp/pr-body.md``, ``body.md``, ``/dev/stdin``) is a
   refusal, because two lanes sharing one path is exactly how the second
   incident happened, and because the name is the only thing that says
   "nobody else owns these bytes".
3. **The write is read back and judged.**  Snapshot the old body → edit →
   re-read through the same ``gh pr view --json`` channel the body gate uses →
   assert that the new body carries ``--expect-marker``, that it *differs* from
   the snapshot, and that ``title`` and ``headRefName`` did not move.  A
   refusal here is the incident itself, caught in the same command that caused
   it rather than the next morning; both bodies' first lines are printed so the
   operator can restore the right one.
4. **The head is pinned when the caller knows it** (``--expect-head``): the
   pre-edit ``headRefOid`` must match, and the post-edit one must be unchanged.
   A body edit never moves the head, so a moved head means something else is
   writing to this PR at the same time.

Break-glass: :data:`BYPASS_ENV` (``CELESTIA_PR_EDIT_BYPASS=1``) forwards the
edit to the real ``gh`` after printing a loud warning.  It exists so a genuine
emergency is never bricked by this gate; it does not make the unguarded shape
correct, and the warning is printed on every use (the ``gh`` proxy path honours
the same variable, so a bypass set once covers both entry points).

CLI::

    celestia-devtools pr-edit --repo OWNER/NAME --pr N --body-file PATH \\
        --expect-marker STR [--expect-head SHA] [--dry-run] [--selftest]

Exit codes: 0 written and verified, 1 a refusal or a failed verification,
2 usage, 3 a repair was needed and failed (rc 2 from ``--dry-run``'s twin).

The ``gh`` proxy (:mod:`celestia_devtools.vcs.gh`) routes ``gh pr edit …
--body-file`` here, so an aliased ``gh`` cannot bypass these checks by habit;
plain ``gh pr edit`` (title / reviewers / labels) keeps forwarding untouched.
"""

from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

#: Environment variable that forwards a ``pr edit --body-file`` unguarded.
BYPASS_ENV = "CELESTIA_PR_EDIT_BYPASS"

#: Exit codes (documented in the module docstring and in ``--help``).
EXIT_REFUSED = 1
EXIT_USAGE = 2

#: How much of each body a recovery line shows.
_PREVIEW_CHARS = 160

#: Owner/name as GitHub accepts it; a bare name is refused because the whole
#: point of the flag is that the destination is stated in full.
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")

#: Paths whose basename carries no ownership even when it happens to be shaped
#: right: stdin / device nodes are shared by definition.
_SHARED_PATH_PREFIXES = ("/dev/",)

#: Repo names that carry typical host suffixes; a file that names the full
#: ``celestia-island-hikari`` still identifies the PR, so both spellings count.
_OWNER_SEPARATORS = ("-", "_", ".")

def bypass_active() -> bool:
    """True when the break-glass variable is set to anything but ``0``/empty."""
    value = os.environ.get(BYPASS_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")

def bypass_warning(target: str) -> str:
    """The loud line printed by every bypass path (proxy and subcommand)."""
    detail = f"⚠️  PR-EDIT GUARD BYPASSED via {BYPASS_ENV}={os.environ.get(BYPASS_ENV, '')} — forwarding {target} to the real gh with NO repo/body-path/readback checks."
    advice = "This is the exact shape of the 2026-11-15 and 2026-11-27 body cross-writes; unset the variable and use `celestia-devtools pr-edit` as soon as the emergency is over."
    return f"{detail} {advice}"

# ── refusal 1: the destination must be stated ────────────────────────────────

def repo_problems(repo: str | None) -> list[str]:
    """Why *repo* cannot identify a destination (empty = it can)."""
    mandatory = "--repo OWNER/NAME is mandatory — there is no cwd or origin fallback, because resolving a bare PR number against a checkout of a different repository is how cherino#42 was overwritten on 2026-11-15"
    shaped = f"--repo {repo!r} is not OWNER/NAME — pass the repo the PR lives in, e.g. --repo celestia-island/hikari"

    if not repo:
        return [mandatory]

    return [] if _REPO_RE.match(repo) else [shaped]

def repo_name(repo: str) -> str:
    """The ``NAME`` half of ``OWNER/NAME`` (lower case, as GitHub resolves it)."""
    return repo.split("/", 1)[-1].removesuffix(".git").lower()

# ── refusal 2: the body file belongs to this target ─────────────────────────

def _flatten(path: str) -> str:
    """Basename lowered, with the characters that separate words folded to ``-``.

    ``hikari_1586.md`` / ``hikari-1586.md`` / ``PR-1586-hikari.md`` all flatten
    to segments a name check can look at without caring about the author's
    separator choice.
    """
    base = Path(path.rstrip("/")).name.lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")

def body_path_problems(path: str, repo: str, pr: int) -> list[str]:
    """Why *path* does not name this ``<repo>-<pr>`` target (empty = it does).

    The rule kills the 2026-11-27 collision: a generic scratch name
    (``/tmp/pr-body.md``) is shared state in practice, so the edit refuses
    before it writes.  Both orderings are accepted (``hikari-1586-…`` and
    ``pr-1586-hikari-…``) because either one identifies the target
    unambiguously; a file naming a *different* repo and PR is refused too — it
    is the 11-15 content, not just a sloppy name.
    """
    resolved = os.path.abspath(path)

    if not path:
        return ["--body-file is mandatory"]

    if any(resolved.startswith(prefix) for prefix in _SHARED_PATH_PREFIXES):
        return [f"--body-file {path} is a shared device, not an owned file — write " f"<repo-name>-{pr}-pr-body.md to a private path first"]

    name = repo_name(repo)
    flat = _flatten(path)

    if not flat:
        return [f"--body-file {path!r} has no usable basename"]

    if ("-".join(filter(None, ("celestia-island", name))) not in flat) and (name not in flat):
        return [f"--body-file basename {Path(resolved).name!r} does not name the repo {name!r} — " f"the file must identify its target: <repo>-<pr>-pr-body.md (e.g. {name}-{pr}-pr-body.md). " "A generic scratch name is how hikari#1586 was overwritten by chest#2866's " "body on 2026-11-27"]

    if not re.search(rf"(?<![0-9]){pr}(?![0-9])", flat):
        return [f"--body-file basename {Path(resolved).name!r} does not name PR {pr} — " f"expected {name}-{pr}-pr-body.md; a path shared between lanes is " "how the 2026-11-27 cross-write happened"]

    return []

# ── gh plumbing (subprocess — gh carries its own auth) ───────────────────────

def _gh(argv: list[str], runner = None) -> tuple[int, str, str]:
    """Run ``gh <argv>``; returns ``(rc, stdout, stderr)`` and never raises."""
    call = runner or subprocess.run

    try:
        probe = call(["gh", *argv], capture_output = True, text = True, encoding = "utf-8", errors = "replace", check = False)
    except OSError as exc:
        return 127, "", str(exc)

    return int(probe.returncode), probe.stdout or "", probe.stderr or ""

def read_pr_state(repo: str, pr: int, runner = None) -> tuple[dict | None, list[str]]:
    """``title`` / ``body`` / ``headRefName`` / ``headRefOid`` of one PR.

    The same read the body gate does (``gh pr view --json …``) with the fields
    this guard compares; ``None`` + reasons when the PR cannot be read, because
    "cannot read" must never be judged as "unchanged".
    """
    rc, stdout, stderr = _gh(["pr", "view", str(pr), "--repo", repo, "--json", "title,body,headRefName,headRefOid"], runner = runner)

    if rc != 0:
        detail = (stderr or "").strip() or f"exit {rc}"
        return None, [f"gh pr view {pr} --repo {repo} failed ({detail}) — refusing to certify a PR state that could not be read"]

    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, [f"gh pr view {pr} --repo {repo} returned non-JSON ({exc})"]

    if not isinstance(parsed, dict):
        return None, [f"gh pr view {pr} --repo {repo} returned {type(parsed).__name__}, not an object"]

    return parsed, []

def _same_head(a: str | None, b: str | None) -> bool:
    """Head comparison: a short prefix is accepted on either side."""
    left = (a or "").strip().lower()
    right = (b or "").strip().lower()

    if not left or not right:
        return False

    return left.startswith(right) or right.startswith(left)

def _preview(text: str | None) -> str:
    first = (text or "").splitlines()[0] if (text or "").strip() else "<empty body>"
    return first[:_PREVIEW_CHARS] + ("…" if len(first) > _PREVIEW_CHARS else "")

# ── the guarded edit ────────────────────────────────────────────────────────

def _refuse(kind: str, problems: Sequence[str], file = None) -> int:
    """Print one named refusal and return its exit code."""
    lines = [f"❌ {kind}: {problem}" for problem in problems]

    for line in lines:
        print(line, file = file or sys.stderr)

    return EXIT_REFUSED

def guarded_edit(repo: str, pr: int, body_path: str, expect_marker: str, *, expect_head: str | None = None, dry_run: bool = False, runner = None) -> int:
    """Snapshot → edit → read back → assert.  Returns the exit code.

    Every refusal names itself (:data:`REFUSAL_REPO`, :data:`REFUSAL_BODY_PATH`,
    :data:`REFUSAL_HEAD_MOVED`, :data:`REFUSAL_MARKER`, :data:`REFUSAL_READBACK`)
    so a caller can tell which guard fired without parsing prose.
    """
    # The pre-checks are cheap and local: run them before any gh call so a
    # scripted caller gets its refusal without depending on network or auth.
    problems = repo_problems(repo)

    if problems:
        return _refuse(REFUSAL_REPO, problems)

    if pr <= 0:
        return _refuse(REFUSAL_REPO, [f"--pr {pr} is not a pull-request number"])

    problems = body_path_problems(body_path, repo, pr)

    if problems:
        return _refuse(REFUSAL_BODY_PATH, problems)

    if not expect_marker:
        return _refuse(REFUSAL_MARKER, ["--expect-marker is mandatory — an edit that does not say what the new body must contain cannot be verified"])

    try:
        new_body = Path(body_path).read_text(encoding = "utf-8")
    except OSError as exc:
        return _refuse(REFUSAL_BODY_PATH, [f"--body-file {body_path} is unreadable ({exc})"])

    if expect_marker not in new_body:
        # Refusing HERE too (not only after the write) keeps the bad body off
        # the PR: the file the caller is about to push does not carry the
        # marker, so the post-edit assertion below would fail anyway — but only
        # after an unverified overwrite had already landed.
        return _refuse(REFUSAL_MARKER, [f"--expect-marker {expect_marker!r} is absent from {body_path} — refusing to write a body that cannot carry it"])

    before, problems = read_pr_state(repo, pr, runner = runner)

    if before is None:
        return _refuse(REFUSAL_READBACK, problems)

    if expect_head and not _same_head(str(before.get("headRefOid") or ""), expect_head):
        return _refuse(REFUSAL_HEAD_MOVED, [f"--expect-head {expect_head} ≠ the current head {before.get('headRefOid')} of {repo}#{pr} — " "a push landed after the review this edit was prepared against"])

    old_body = str(before.get("body") or "")
    old_title = before.get("title")
    old_head = before.get("headRefOid")
    old_branch = before.get("headRefName")

    if dry_run:
        print(f"✅ pr-edit preflight passed for {repo}#{pr} (head={str(old_head)[:12]}, body-file={body_path}); --dry-run wrote nothing")
        return 0

    rc, stdout, stderr = _gh(["pr", "edit", str(pr), "--repo", repo, "--body-file", body_path], runner = runner)

    if rc != 0:
        detail = (stderr or stdout or "").strip() or f"exit {rc}"
        return _refuse(REFUSAL_EDIT_FAILED, [f"gh pr edit {pr} --repo {repo} --body-file {body_path} failed ({detail})"])

    after, problems = read_pr_state(repo, pr, runner = runner)

    if after is None:
        print(f"❌ {REFUSAL_READBACK}: the edit went through but the readback failed — " f"verify {repo}#{pr} by hand before trusting it", file = sys.stderr)
        return _refuse(REFUSAL_READBACK, problems)

    new_remote = str(after.get("body") or "")
    mismatches: list[str] = []

    if expect_marker not in new_remote:
        mismatches.append(f"the body read back from {repo}#{pr} does not contain --expect-marker {expect_marker!r}")

    if new_remote == old_body:
        mismatches.append(f"the body read back from {repo}#{pr} is identical to the pre-edit snapshot — the edit was a no-op or silently dropped")

    if after.get("title") != old_title:
        mismatches.append(f"title moved during a body edit: {old_title!r} → {after.get('title')!r}")

    if (after.get("headRefName") or "") != (old_branch or ""):
        mismatches.append(f"headRefName moved during a body edit: {old_branch!r} → {after.get('headRefName')!r}")

    if not _same_head(str(after.get("headRefOid") or ""), str(old_head or "")):
        mismatches.append(f"head moved during a body edit: {str(old_head)[:12]} → {str(after.get('headRefOid'))[:12]} — " "a body edit never moves the head, so something else wrote to this PR")

    if mismatches:
        for problem in mismatches:
            print(f"❌ {REFUSAL_MARKER}: {problem}", file = sys.stderr)

        # Recovery material: the operator has to see which body is on the PR now
        # and which one was there before, without re-reading the whole thing.
        print(f"   ↳ {repo}#{pr} first line NOW: {_preview(new_remote)!r}", file = sys.stderr)
        print(f"   ↳ {repo}#{pr} first line BEFORE this edit: {_preview(old_body)!r}", file = sys.stderr)
        print(f"   ↳ local --body-file first line: {_preview(new_body)!r}", file = sys.stderr)
        return EXIT_REFUSED

    print(f"✅ pr-edit verified: {repo}#{pr} head={str(old_head)[:12]} marker={expect_marker!r} " f"({len(new_remote)} characters read back)")
    return 0

#: Refusal identifiers — stable strings a wrapper can match on.
REFUSAL_REPO = "PR-EDIT REFUSED (repo)"
REFUSAL_BODY_PATH = "PR-EDIT REFUSED (body-file)"
REFUSAL_MARKER = "PR-EDIT REFUSED (readback)"
REFUSAL_HEAD_MOVED = "PR-EDIT REFUSED (head)"
REFUSAL_READBACK = "PR-EDIT REFUSED (unreadable)"
REFUSAL_EDIT_FAILED = "PR-EDIT REFUSED (gh edit)"

# ── --selftest ──────────────────────────────────────────────────────────────

class _fake_gh:
    """A stand-in for the ``gh`` binary: scripted per argv, records every call.

    ``views`` is the queue of ``pr view`` payloads (one per read); ``edit_rc``
    decides what the edit answers; ``mutate`` is applied to the state after the
    edit so a control can simulate a title change, a dropped body or a moved
    head — the three ways a write can come back wrong.
    """

    def __init__(self, state: dict, *, edit_rc: int = 0, after: dict | None = None, view_rc: int = 0):
        self.state = dict(state)
        self.edit_rc = edit_rc
        #: The state the guard must observe AFTER the edit; ``run`` populates it
        #: with the file it just wrote unless the control staged its own.
        self.after = after
        self.view_rc = view_rc
        self.calls: list[list[str]] = []
        self.views = 0

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))

        class _probe:
            returncode = 0
            stdout = ""
            stderr = ""

        probe = _probe()
        action = argv[2] if len(argv) > 2 else ""

        if (argv[:2] == ["gh", "pr"]) and (action == "view"):
            self.views += 1

            if self.view_rc:
                probe.returncode = self.view_rc
                probe.stderr = "simulated gh failure"
                return probe

            probe.stdout = json.dumps(self.state if self.views == 1 else (self.after if self.after is not None else self.state))
            return probe

        if (argv[:2] == ["gh", "pr"]) and (action == "edit"):
            probe.returncode = self.edit_rc

            if self.edit_rc:
                probe.stderr = "simulated edit failure"
                return probe

            written = Path(argv[argv.index("--body-file") + 1]).read_text(encoding = "utf-8")

            if self.after is None:
                self.after = {**self.state, "body": written}

            return probe

        probe.returncode = 127
        probe.stderr = "fake gh: unexpected argv"
        return probe

_BASE_STATE = {"title": "✨ Add the standalone error landing page asset.", "body": "old body\nsecond line\n", "headRefName": "feat-standalone-error-landing", "headRefOid": "a" * 40}
_NEW_BODY = "# Body\n\nverify: rounds=1 clean=0\n"

def selftest() -> int:
    """Positive + negative controls for every refusal (the gate-exit-0 discipline).

    Each negative asserts the *exit code* and the *refusal identifier*, so a
    guard whose condition is written too wide or too narrow reds here; the
    positive control drives the full path (snapshot → edit → readback) through
    the fake transport and asserts the edit actually happened exactly once.
    """
    failures: list[str] = []
    checks = 0

    def check(label: str, ok: bool, evidence: str = "") -> None:
        nonlocal checks
        checks += 1

        if not ok:
            failures.append(f"{label}{(': ' + evidence) if evidence else ''}")

    def body_file(root: str, name: str, text: str = _NEW_BODY) -> str:
        path = Path(root) / name
        path.write_text(text, encoding = "utf-8")
        return str(path)

    def run(repo: str, pr: int, path: str, marker: str = "verify: rounds=1", *, expect_head: str | None = None, runner, capture) -> int:
        """Run guarded_edit with stderr captured, returning (rc, stderr)."""
        return _captured(lambda: guarded_edit(repo, pr, path, marker, expect_head = expect_head, runner = runner), capture)

    # ── refusal 1: --repo is mandatory ─────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix = "devtools-edit-selftest-") as root:
        path = body_file(root, "hikari-1586-pr-body.md")
        capture = _capture()

        for missing in ("", None, "hikari", "celestia-island/"):
            runner = _fake_gh(_BASE_STATE)
            rc = run(missing, 1586, path, runner = runner, capture = capture)
            check(f"repo-missing refused for {missing!r}", (rc == EXIT_REFUSED) and (REFUSAL_REPO in capture.stderr), f"rc={rc} stderr={capture.text()[:120]}")
            check(f"repo-missing made no gh call for {missing!r}", runner.calls == [], f"calls={runner.calls}")

        # ── refusal 2: the body file must name <repo>-<pr> ─────────────────
        for bad in ("pr-body.md", "body.md", "hikari-pr-body.md", "hikari-9999-pr-body.md", "chest-2866-pr-body.md", "/dev/stdin"):
            runner = _fake_gh(_BASE_STATE)
            rc = run("celestia-island/hikari", 1586, bad, runner = runner, capture = capture)
            check(f"generic body path refused for {bad!r}", (rc == EXIT_REFUSED) and (REFUSAL_BODY_PATH in capture.stderr), f"rc={rc} stderr={capture.text()[:120]}")
            check(f"generic body path made no gh call for {bad!r}", runner.calls == [], f"calls={runner.calls}")

        # ── positive control: a real-shaped edit goes through ──────────────
        runner = _fake_gh(_BASE_STATE)
        path = body_file(root, "hikari-1586-pr-body.md")
        rc = run("celestia-island/hikari", 1586, path, expect_head = "a" * 40, runner = runner, capture = capture)
        edits = [c for c in runner.calls if (len(c) > 2) and (c[2] == "edit")]
        clean = not any(marker in capture.stderr for marker in (REFUSAL_REPO, REFUSAL_BODY_PATH, REFUSAL_MARKER))
        check("positive: edit verified", (rc == 0) and clean, f"rc={rc} stderr={capture.text()[:160]}")
        check("positive: exactly one gh pr edit", len(edits) == 1, f"edits={edits}")
        repo_on_edit = bool(edits) and ("--repo" in edits[0]) and ("celestia-island/hikari" in edits[0])
        check("positive: --repo on the edit", repo_on_edit, f"argv={edits[0] if edits else None}")

        # the other accepted orderings (a PR number first, the full repo name)
        for name in ("pr-1586-hikari.md", "celestia-island-hikari-1586-pr-body.md", "HIKARI_1586_body.md"):
            runner = _fake_gh(_BASE_STATE)
            rc = run("celestia-island/hikari", 1586, body_file(root, name), runner = runner, capture = capture)
            check(f"positive: ordering {name!r} accepted", rc == 0, f"rc={rc} stderr={capture.text()[:120]}")

        # a different PR of the same repo is still one target (1586 is not 15861)
        runner = _fake_gh(_BASE_STATE)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-15861-pr-body.md"), runner = runner, capture = capture)
        check("negative: 15861 does not satisfy PR 1586", (rc == EXIT_REFUSED) and (REFUSAL_BODY_PATH in capture.stderr), f"rc={rc}")

        # ── refusal 3a: marker absent from the local file ──────────────────
        runner = _fake_gh(_BASE_STATE)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-nomarker.md", "no marker here\n"), runner = runner, capture = capture)
        check("marker absent locally refused", (rc == EXIT_REFUSED) and (REFUSAL_MARKER in capture.stderr), f"rc={rc}")
        check("marker absent locally: no edit", not any(c[2] == "edit" for c in runner.calls if len(c) > 2), f"calls={runner.calls}")

        # ── refusal 3b: marker absent AFTER the edit (simulated) ───────────
        after = dict(_BASE_STATE)
        after["body"] = "some other lane's body\n"
        runner = _fake_gh(_BASE_STATE, after = after)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-marker-lost.md"), runner = runner, capture = capture)
        check("marker lost after edit refused", (rc == EXIT_REFUSED) and (REFUSAL_MARKER in capture.stderr) and ("does not contain" in capture.stderr), f"rc={rc} stderr={capture.text()[:200]}")
        check("marker lost: both bodies printed", ("BEFORE this edit" in capture.stderr) and ("first line NOW" in capture.stderr), "recovery lines missing")

        # ── refusal 3c: the body came back identical (dropped write) ───────
        runner = _fake_gh(_BASE_STATE, after = dict(_BASE_STATE))
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-noop.md", "verify: rounds=1\n"), runner = runner, capture = capture)
        check("unchanged body refused", (rc == EXIT_REFUSED) and ("identical to the pre-edit snapshot" in capture.stderr), f"rc={rc} stderr={capture.text()[:200]}")

        # ── refusal 3d: the title moved (someone else wrote) ───────────────
        after = dict(_BASE_STATE)
        after["body"] = _NEW_BODY
        after["title"] = "probe-x"
        runner = _fake_gh(_BASE_STATE, after = after)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-title.md"), runner = runner, capture = capture)
        check("title moved refused", (rc == EXIT_REFUSED) and ("title moved" in capture.stderr), f"rc={rc} stderr={capture.text()[:200]}")

        # ── refusal 4: --expect-head mismatch, and a head moved post-edit ──
        runner = _fake_gh(_BASE_STATE)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-head.md"), expect_head = "b" * 40, runner = runner, capture = capture)
        check("expect-head mismatch refused", (rc == EXIT_REFUSED) and (REFUSAL_HEAD_MOVED in capture.stderr), f"rc={rc} stderr={capture.text()[:160]}")
        check("expect-head mismatch: no edit", not any(c[2] == "edit" for c in runner.calls if len(c) > 2), f"calls={runner.calls}")

        after = dict(_BASE_STATE)
        after["body"] = _NEW_BODY
        after["headRefOid"] = "c" * 40
        runner = _fake_gh(_BASE_STATE, after = after)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-head-moved.md"), expect_head = "a" * 40, runner = runner, capture = capture)
        check("head moved during the edit refused", (rc == EXIT_REFUSED) and ("head moved during a body edit" in capture.stderr), f"rc={rc} stderr={capture.text()[:200]}")

        # a short --expect-head prefix is accepted (README / pr-merge-gate parity)
        runner = _fake_gh(_BASE_STATE)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-prefix.md"), expect_head = "a" * 12, runner = runner, capture = capture)
        check("positive: short --expect-head accepted", rc == 0, f"rc={rc} stderr={capture.text()[:160]}")

        # ── transport failures are refused, never read as "unchanged" ──────
        runner = _fake_gh(_BASE_STATE, view_rc = 1)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-viewfail.md"), runner = runner, capture = capture)
        check("unreadable PR refused", (rc == EXIT_REFUSED) and (REFUSAL_READBACK in capture.stderr), f"rc={rc} stderr={capture.text()[:160]}")

        runner = _fake_gh(_BASE_STATE, edit_rc = 1)
        rc = run("celestia-island/hikari", 1586, body_file(root, "hikari-1586-editfail.md"), runner = runner, capture = capture)
        check("failed gh edit refused", (rc == EXIT_REFUSED) and (REFUSAL_EDIT_FAILED in capture.stderr), f"rc={rc} stderr={capture.text()[:160]}")

        # ── dry run: preflight passes, nothing is written ──────────────────
        runner = _fake_gh(_BASE_STATE)
        rc = _captured(lambda: guarded_edit("celestia-island/hikari", 1586, body_file(root, "hikari-1586-dry.md"), "verify: rounds=1", dry_run = True, runner = runner), capture)
        check("dry-run rc 0 without an edit", (rc == 0) and not any(c[2] == "edit" for c in runner.calls if len(c) > 2), f"rc={rc} calls={runner.calls}")

    # ── the CLI refuses a missing --repo before argparse defaults it ────────
    capture = _capture()
    rc = _captured(lambda: main(["--repo", "celestia-island/hikari", "--pr", "0", "--body-file", "/tmp/x-hikari-0-pr-body.md", "--expect-marker", "x"]), capture)
    check("cli refuses --pr 0", (rc == EXIT_REFUSED) and (REFUSAL_REPO in capture.stderr), f"rc={rc} stderr={capture.text()[:120]}")

    rc = _captured(lambda: main(["--pr", "1586", "--body-file", "/tmp/hikari-1586-pr-body.md", "--expect-marker", "x"]), capture)
    check("cli refuses a missing --repo", (rc == EXIT_REFUSED) and (REFUSAL_REPO in capture.stderr), f"rc={rc} stderr={capture.text()[:120]}")

    rc = _captured(lambda: main(["--repo", "celestia-island/hikari", "--body-file", "/tmp/hikari-1586-pr-body.md", "--expect-marker", "x"]), capture)
    check("cli refuses a missing --pr as usage", (rc == EXIT_USAGE) and ("--pr is required" in capture.stderr), f"rc={rc} stderr={capture.text()[:120]}")

    if failures:
        for failure in failures:
            print(f"❌ selftest: {failure}", file = sys.stderr)

        print(f"SELFTEST FAILED — {len(failures)} control(s) red of {checks}", file = sys.stderr)
        return 1

    print(f"✅ pr-edit selftest: {checks} control(s) over {_SELFTEST_ROOTS} guard(s) — refusals red, positive green")
    return 0

#: Number of guard families the selftest exercises (repo / body path / readback
#: content / readback head / transport) — printed so a shrunken selftest is visible.
_SELFTEST_ROOTS = 5

class _capture:
    """Capture stderr around a call (the guards print there, not to a stream arg)."""

    def __init__(self) -> None:
        self.stderr = ""

    def text(self) -> str:
        return self.stderr.replace("\n", " | ")

def _captured(fn, capture: _capture) -> int:
    """Run *fn* with stderr redirected into *capture*, returning its exit code."""
    buffer = io.StringIO()

    with contextlib.redirect_stderr(buffer):
        rc = fn()

    capture.stderr = buffer.getvalue()
    return int(rc or 0)

# ── CLI ─────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    description = "Guarded `gh pr edit --body-file`: mandatory --repo, a body file that names <repo>-<pr>, and a readback assertion (marker present, body changed, title/head unchanged)."
    epilog = f"The `gh` proxy routes `gh pr edit … --body-file` here. Break-glass: {BYPASS_ENV}=1 forwards the edit unguarded with a loud warning (the same variable disables the proxy interception); it is meant for an emergency, not for daily use."
    parser = argparse.ArgumentParser(prog = "celestia-devtools pr-edit", description = description, epilog = epilog, formatter_class = argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default = None, help = "OWNER/NAME the PR lives in (mandatory — there is no cwd/origin fallback)")
    parser.add_argument("--pr", type = int, default = None, help = "pull-request number")
    parser.add_argument("--body-file", default = None, metavar = "PATH", help = "body to write; the basename must name <repo>-<pr> (e.g. hikari-1586-pr-body.md)")
    parser.add_argument("--expect-marker", default = None, metavar = "STR", help = "string the body must contain, before and after the edit")
    parser.add_argument("--expect-head", default = None, metavar = "SHA", help = "head SHA this edit was prepared against (prefix ok); the head must not move")
    parser.add_argument("--dry-run", action = "store_true", help = "run every guard and the snapshot, then stop without writing")
    parser.add_argument("--selftest", action = "store_true", help = "run the refusal controls (red) and the fixture edit (green)")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv if argv is not None else sys.argv[1:]))

    if args.selftest:
        return selftest()

    # argparse cannot express "required only without --selftest" without making
    # --selftest unusable, so the missing-argument cases are spelled out here.
    # A missing --pr / --body-file / --expect-marker is USAGE (2): the guard
    # never ran, so no guard refusal fired.  A missing --repo is NOT usage in
    # the same sense — it is the 2026-11-15 incident shape and it is refused by
    # the guard itself (exit 1, under its own identifier), so that a wrapper
    # greps one string whether the caller went through the proxy or the CLI.
    for name in ("pr", "body_file", "expect_marker"):
        if getattr(args, name) in (None, ""):
            print(f"❌ pr-edit: --{name.replace('_', '-')} is required", file = sys.stderr)
            print("   usage: celestia-devtools pr-edit --repo OWNER/NAME --pr N --body-file PATH --expect-marker STR [--expect-head SHA]", file = sys.stderr)
            return EXIT_USAGE

    return guarded_edit(args.repo, args.pr, args.body_file, args.expect_marker, expect_head = args.expect_head, dry_run = args.dry_run)

if __name__ == "__main__":
    raise SystemExit(main())
