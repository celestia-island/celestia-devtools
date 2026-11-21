#!/usr/bin/env python3
"""Pre-squash-merge gate — mechanical title / head / verify-record checks that
must pass before a pull request may be squash-merged.

Background (2026-11-20 ``probe-x`` incident): a debugging probe renamed a PR
("probe-x") and the rename was never reverted.  The squash merge carried the
polluted title straight into the trunk as the commit subject — unfixable
without a force push, which the org forbids.  Two existing nets missed it:
``git log -1`` self-checks happen *after* the fact, and ``pr-merge`` / ``gh``
validate the subject the CALLER passes to ``gh pr merge`` — a bare REST merge
bypasses both.  This gate validates the PR's OWN title, refuses a head that
moved after review, and issues the merge itself from the same process, so no
human step sits between "checked" and "merged".  The GET→PUT race is closed
SERVER-SIDE as well: the merge request carries the validated head as its
``sha``, so a push landing inside those milliseconds makes the merge answer 409
instead of merging an unreviewed commit.

Checks (any failure → rc 1, merge refused):

1.  PR state == ``open``, ``mergeable`` is true, and the base branch is the
    expected one (``--base``, default ``master``).
2.  Head SHA matches ``--expect-head`` (the SHA the review rounds verified;
    a short prefix is accepted).  A push after the last review voids the
    closing verification.
3.  Title passes :func:`celestia_devtools.vcs.commit_msg.lint` — the single
    source for the org gitmoji rules (emoji, uppercase, period, English-only,
    no colon prefix, no version/filler start) — plus PR-specific extras:
    minimum length, bare probe/placeholder words, an in-title pull-request
    reference — a parenthesised ``(#N)`` in either width, or a bare ``#N`` of
    two or more digits (a bare ``#1`` is left alone: it reads as an ordinary
    cross-reference) — and a mid-title version word (zero-width characters and
    Eastern-Arabic digits are normalised first).  Two lint exemptions come
    along by design: ``Revert …`` (git revert) and the ``🔄`` period
    relaxation.  A title carrying an invisible private-use character is
    refused outright (it would land verbatim in the squash subject and has no
    legitimate place there).  Note two things about the emoji set and the
    zero-width strip: the set is lint's full gitmoji whitelist rather than the
    narrower one the workspace predecessor hard-coded (so a rarer canonical
    gitmoji passes here), and whitelist ZWJ sequences survive the zero-width
    strip while every other character **in the strip set**
    (U+200B/200C/200D/FEFF) is normalised away — including a ZWJ used to split
    a version word, a banned word or a colon prefix.  Characters outside that
    set (U+2060 word-joiner, U+00AD soft hyphen, variation selectors, …) are
    not touched: widening the set is a separate decision, not a side effect.
4.  Body carries exactly one ``verify:`` record, parsed by a faithful mirror of
    the workspace scanner (``_tools/verify_account.py``): fenced blocks are not
    records, an unclosed fence reds only when it *contains* a record, indented
    and quoted shapes are templates, the field set must be complete, duplicate
    and unknown keys are refused, counters must be integers, ``clean <=
    rounds``, hardcap/budget enums, ``post-cap`` requires ``hardcap=yes``, and
    a record hidden in a fence while another sits in prose is an ambiguous set.
    The mirror is a LOOSER-NEVER-FAIL rule: it may not accept a body the
    scanner reds (the scanner runs next, pre-merge and every 6 hours).
    On the ``c3`` lane: ``c3 >= 3`` is a floor (``c3=4`` is stronger) with
    ``rounds >= max(4, c3+1)``, and the S0–S3 counters are totals across all
    rounds (already-fixed findings included), so the clean streak — not a zero
    counter — is what proves closure.  Without ``c3`` the structural-exemption
    lane applies (``clean >= 1``, ``gates >= 1``, ``S0 == S1 == 0``; no rounds
    floor).

Merge (``merge`` subcommand): checks run and, only if they pass, the squash is
issued **without an explicit subject** so the server derives ``<title> (#N)``
from the validated title.  The merge commit's subject is then read back:

*  subject matches → rc 0;
*  mismatch → rc 2 (the caller owns the sticky pollution alert);
*  commit unreadable (transport, after one retry) → rc 2 as
   ``COMMIT-GET-FAILED`` *without* claiming pollution (cannot read ≠ polluted).

This module stays repo-agnostic and only prints; turning rc 2 into a sticky
``_alerts`` marker is the caller's job.  The workspace-side
``_tools/pr_merge_gate.py`` is a shim that forwards its argv here (see
:func:`celestia_devtools.vcs.pr_gate.main`) and owns the
``pr-title-pollution.failed`` marker; ``--waiver`` passes through it unchanged.

Recorded-exception waivers (2026-11-21 user ruling): ``--waiver PATH`` accepts a
signed file that suppresses **exactly one** refusal — ``verify record c3=<n> <
3`` — for the deadlock where every review finding is fixed and the gates are
green, but the record's history carries an S1 (so the structural-exemption
lane's ``S0 == S1 == 0`` fails) and a fresh c3 streak cannot be bought without
re-running rounds the budget already rejected.  The file is
``_alerts/pr-merge-waiver-<repo-name>-<pr>.md`` and must carry ``pr:``,
``head:`` (the covered commit), ``basis:`` (≥40 characters of rationale),
``c3-not-fabricated: yes`` and ``signed: lead <YYYY-MM-DD>``; see
:func:`read_waiver`.  Everything else — title, head pin, base, state, and the
record's own well-formedness — is checked exactly as without a waiver, so a
waiver can neither excuse a polluted title nor a head that moved after review.

CLI::

    pr-merge-gate check --repo OWNER/NAME --pr 123 --expect-head SHA
    pr-merge-gate merge --repo OWNER/NAME --pr 123 --expect-head SHA
    pr-merge-gate merge … --waiver _alerts/pr-merge-waiver-<repo>-<pr>.md

``--expect-head`` is required by BOTH lanes: a check that does not say which
commit was reviewed is not evidence, so there is no "head-less check" mode.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── PR-specific title extensions (on top of commit_msg.lint) ────────────────

MIN_TITLE_LEN = 20

# Bare probe / placeholder words — any standalone occurrence rejects the
# title.  Tokenised with [A-Za-z]+ so "testing" / "prober" / "testability"
# never false-positive.
BANNED_TITLE_WORDS = {
    "wip", "draft", "probe", "test", "tmp", "todo", "fixme",
    "t", "xxx", "placeholder",
}

# Zero-width characters stripped before matching; Eastern-Arabic digits
# normalised to ASCII so "v١2" cannot smuggle a version word.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# An in-title pull-request reference: the squash appends its own " (#N)", so a
# title that already carries one would end up duplicated.  A parenthesised form
# counts at any width; a bare form only at 2+ digits (bare "#1" reads as an
# ordinary cross-reference, see the module docstring).
_PR_REF_IN_TITLE = re.compile(r"(?:\(\s*#\s*\d+\s*\)|（\s*#\s*\d+\s*）)|#\s*\d{2,}")

# Mid-title version word: "Add v2 support." — lint's rule 4 only covers a
# version-number START, this catches the smuggled mid-sentence form.
_VERSION_MID_RE = re.compile(r"\bv\s?\d+\b", re.IGNORECASE)

# A reviewed head must be pinned to a real SHA: an empty value would silently
# switch the check off (the exact failure mode this gate exists to prevent),
# and a one-or-two character prefix matches thousands of commits, so it is not
# evidence.  Git's own abbreviation floor is 7.
MIN_HEAD_PREFIX = 7

DEFAULT_BASE = "master"

# ZWJ-sequence gitmojis from the lint whitelist (e.g. 🧑‍💻): the joiner is
# load-bearing inside them, so the zero-width strip must NOT eat it —
# stripping made lint report "must start with a gitmoji" for a fully compliant
# title.  The sequences are protected by SPLITTING on them and stripping only
# the surrounding pieces, so no placeholder character is ever written into the
# title: nothing can collide with a real input, nothing 1:1 with an emoji can
# be forged, and every character IN the strip set (U+200B/200C/200D/FEFF) is
# still normalised away — including a ZWJ outside a whitelist sequence ("v‍2",
# "Pr‍obe", "feat‍:").  Zero-width characters outside the set (U+2060, U+00AD,
# variation selectors) are deliberately untouched; see the module docstring.
_ZWJ_EMOJIS: tuple[str, ...] = ()  # filled lazily (needs commit_msg's whitelist)
_ZWJ_SPLIT_RE: "re.Pattern[str] | None" = None

# Distinctness is structural for whitelist entries that neither equal nor
# contain one another; an entry that *overlaps* another without being a prefix
# of it (e.g. 🧑‍💻 plus 💻‍🧑) could lose protection for the overlapped
# occurrence.  The real whitelist has exactly one ZWJ sequence, so this is a
# documented bound on the claim, not a live gap — a future entry needs the
# matcher revisited, not just the list extended.

# Private-use characters are invisible in every UI but land verbatim in the
# squash subject, and a title has no legitimate reason to carry one.
_PRIVATE_USE_RE = re.compile(
    "[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]"
)


def _zwj_splitter() -> "re.Pattern[str] | None":
    """Regex that splits a title on whitelist ZWJ sequences (odd parts kept)."""
    global _ZWJ_EMOJIS, _ZWJ_SPLIT_RE
    # Both globals are the cache; checking one alone lets a caller clear the
    # other and keep using a stale regex (the trap the sentinel-era code left
    # behind).  Clearing `_ZWJ_EMOJIS` therefore really forces a rebuild —
    # which is what a test monkeypatching the whitelist expects.
    if _ZWJ_SPLIT_RE is None or not _ZWJ_EMOJIS:
        from celestia_devtools.vcs.commit_msg import GITMOJI_WHITELIST

        _ZWJ_EMOJIS = tuple(
            sorted((e for e in GITMOJI_WHITELIST if "\u200d" in e), key=len, reverse=True)
        )
        if _ZWJ_EMOJIS:
            alternation = "|".join(re.escape(e) for e in _ZWJ_EMOJIS)
            _ZWJ_SPLIT_RE = re.compile(f"({alternation})")
    return _ZWJ_SPLIT_RE


def _normalise_title(title: str) -> str:
    """Strip zero-width noise, keeping whitelist ZWJ sequences intact.

    Splitting on the whitelist sequences means the function needs no
    placeholder character, so it cannot be talked into fabricating an emoji
    from something already in the title — the preconditions that bit the
    earlier sentinel design are gone rather than merely documented.
    """
    splitter = _zwj_splitter()
    if splitter is None:
        return title.translate(_ZERO_WIDTH).translate(_ARABIC_INDIC).strip()
    pieces = splitter.split(title)
    return "".join(
        piece if index % 2 else piece.translate(_ZERO_WIDTH).translate(_ARABIC_INDIC)
        for index, piece in enumerate(pieces)
    ).strip()


def title_problems(title: str, *, allow_cjk: bool = False) -> list[str]:
    """PR-title problems beyond :func:`commit_msg.lint` (empty = compliant)."""
    from celestia_devtools.vcs.commit_msg import lint

    problems: list[str] = []
    # Invisible private-use characters land verbatim in the squash subject and
    # have no legitimate place in a title — refused on sight, at any position.
    if _PRIVATE_USE_RE.search(title):
        problems.append(
            "title contains an invisible private-use character — refused on sight"
        )
    title = _normalise_title(title)

    if len(title) < MIN_TITLE_LEN:
        problems.append(
            f"title too short ({len(title)} chars) — reads as a probe/placeholder: {title!r}"
        )

    problems.extend(lint(title, allow_cjk=allow_cjk))

    for word in re.findall(r"[A-Za-z]+", title):
        if word.lower() in BANNED_TITLE_WORDS:
            problems.append(f"title contains bare probe/placeholder word {word!r}")

    if _PR_REF_IN_TITLE.search(title):
        problems.append("title carries its own PR reference — the squash appends ' (#N)' itself")
    if _VERSION_MID_RE.search(title):
        problems.append("title contains a version word (v2-style); describe the change instead")
    return problems


# ── verify-record body check (workspace-scanner mirror) ─────────────────────
#
# Authoritative origin: the workspace scanner (`_tools/verify_account.py`),
# which runs pre-merge (skill step 6) and post-merge every 6 hours.  Keep the
# two in step when the record format evolves; this mirror must never be
# LOOSER than that detector.

_REQUIRED_FIELDS = ("rounds", "clean", "s0", "s1", "s2", "s3", "hardcap")
_RECORD_RE = re.compile(
    r"^[ \t>#*_+.\-`]*(?:\d+[.)][ \t]+)?[*_ \t#>.`-]*(?:verify|合计)[ \t*_`]*\**[ \t]*[:：][ \t\u3000]*"
    r"(?=\W*[A-Za-z][\w-]*\s*=)(\S.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)[ \t]*=[ \t]*([^\s`*（(，,；;。+]+)")
_KNOWN_FIELDS = {
    "rounds", "clean", "s0", "s1", "s2", "s3", "hardcap",
    "gates", "post-cap", "c3", "doc-lint", "budget", "hardware",
    "gates-behavior", "gates-static",
}
_COUNTER_FIELDS = ("rounds", "clean", "s0", "s1", "s2", "s3", "gates", "post-cap", "c3", "doc-lint")
_HARD_ROUND_CAP = 6
_HARDCAP_VALUES = {"yes", "no"}
_BUDGET_VALUES = {"ok", "exhausted@3"}

# ── recorded-exception waivers (2026-11-21 user ruling) ─────────────────────
#
# The hard ``c3 >= 3`` floor can deadlock a PR whose code and gates are green
# and whose every review finding is fixed, when its HISTORY contains an S1 (so
# the structural-exemption lane's ``S0 == S1 == 0`` requirement fails) while a
# fresh streak cannot be bought without re-running rounds the budget already
# rejected.  The waiver is the auditable escape hatch: a signed file under
# ``_alerts/`` that names the PR and the head it covers, states the basis, and
# declares that the ``verify:`` line still carries the honest ``c3`` (no
# back-filled ``c3=3``).
#
# It suppresses EXACTLY ONE refusal — ``verify record c3=<n> < 3`` — and
# nothing else: the title, the head pin, the base branch, the state and the
# record's own well-formedness are checked exactly as before, so a waiver
# cannot smuggle in a polluted title or an unreviewed head.

WAIVER_FILENAME_TEMPLATE = "pr-merge-waiver-{repo}-{pr}.md"
# ``owner/name`` → the file is keyed by the REPOSITORY NAME, not the owner: one
# directory (``_alerts/``) holds every repo's waivers, and the owner is always
# ``celestia-island``.
_WAIVER_FILENAME_RE = re.compile(
    r"^pr-merge-waiver-(?P<repo>[A-Za-z0-9._-]+)-(?P<pr>\d+)\.md$"
)
_WAIVER_SIGNED_RE = re.compile(
    r"^signed:[ \t]*lead[ \t]+(?P<date>\d{4}-\d{2}-\d{2})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_WAIVER_C3_DECL_RE = re.compile(
    r"^c3-not-fabricated:[ \t]*(?P<value>yes|no)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_WAIVER_BASIS_MIN = 40
# The audit home is enforced rather than assumed: a waiver consumed from /tmp
# leaves no sticky artefact, and a channel whose whole justification is
# auditability cannot rely on the operator remembering to copy the file.  The
# check is on the directory NAME (not an absolute path) so it holds from any
# workspace root — and tests create one.
WAIVER_DIR_NAME = "_alerts"
# The single refusal a waiver may suppress.
_WAIVABLE_C3_RE = re.compile(r"^verify record c3=(?P<c3>\d+) < 3$")


@dataclass(frozen=True)
class Waiver:
    """A validated recorded-exception grant (one PR, one head SHA)."""

    path: Path
    repo: str
    pr: int
    head: str
    signed: str


# Zero-width characters a rationale could hide behind.  ``str.strip()`` only
# removes whitespace, so 40 U+200B characters would otherwise pass the floor.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)


def _visible_len(text: str) -> int:
    """Characters that are neither whitespace nor zero-width."""
    return sum(1 for char in text.translate(_INVISIBLE) if not char.isspace())


def _is_iso_date(value: str) -> bool:
    """A real calendar date (``datetime.date.fromisoformat`` rejects 2026-02-30)."""
    import datetime

    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _waiver_field(text: str, key: str) -> str | None:
    """The value of a ``key: value`` line, or ``None`` when the line is absent."""
    match = re.search(
        rf"^{re.escape(key)}:[ \t]*(?P<value>.*)$", text, re.IGNORECASE | re.MULTILINE
    )
    return match.group("value").strip() if match else None


def read_waiver(
    path: str | Path, *, repo: str, pr: int, head: str
) -> tuple[Waiver | None, list[str]]:
    """Validate a recorded-exception file; return ``(grant, problems)``.

    The file must be ``_alerts/pr-merge-waiver-<repo-name>-<pr>.md`` and carry:

    * ``pr: <N>``                 — matching ``--pr``;
    * ``head: <sha>``             — the head this waiver covers (a prefix of the
      payload head is accepted, the same rule ``--expect-head`` uses);
    * ``basis: <text>``           — at least :data:`_WAIVER_BASIS_MIN` characters
      of rationale (which rounds ran, whether findings were cleared, that the
      gates are green on that SHA);
    * ``c3-not-fabricated: yes``  — the declaration that the ``verify:`` line
      still records the real ``c3``;
    * ``signed: lead <YYYY-MM-DD>`` — the sign-off.

    Anything missing or mismatched is a refusal; a waiver that does not apply is
    never treated as "no waiver needed".
    """
    waiver_path = Path(path)
    problems: list[str] = []
    repo_name = repo.rsplit("/", 1)[-1]

    if not waiver_path.is_file():
        return None, [f"waiver {waiver_path}: not a readable file"]

    if waiver_path.parent.name != WAIVER_DIR_NAME:
        return None, [
            f"waiver {waiver_path}: must live in a {WAIVER_DIR_NAME!r} directory — "
            "a waiver consumed from elsewhere leaves no sticky audit artefact"
        ]

    name_match = _WAIVER_FILENAME_RE.match(waiver_path.name)
    if not name_match:
        return None, [
            f"waiver {waiver_path.name}: name must be "
            f"{WAIVER_FILENAME_TEMPLATE.format(repo='<repo>', pr='<pr>')}"
        ]
    if name_match.group("repo").lower() != repo_name.lower():
        problems.append(
            f"waiver {waiver_path.name}: names repo {name_match.group('repo')!r}, "
            f"this merge is {repo_name!r}"
        )
    if int(name_match.group("pr")) != pr:
        problems.append(
            f"waiver {waiver_path.name}: names PR {name_match.group('pr')}, this merge is #{pr}"
        )
    if problems:
        return None, problems

    try:
        text = waiver_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError: catching only
        # OSError let a non-UTF-8 waiver crash the gate with a traceback (still
        # fail-closed, but the operator gets no "this file is unusable" line).
        return None, [f"waiver {waiver_path}: unreadable ({exc})"]

    field_pr = _waiver_field(text, "pr")
    if field_pr is None:
        problems.append(f"waiver {waiver_path.name}: missing a `pr:` line")
    elif field_pr != str(pr):
        problems.append(
            f"waiver {waiver_path.name}: `pr: {field_pr}` does not match #{pr}"
        )

    field_head = _waiver_field(text, "head")
    wanted_head = (head or "").strip()
    if field_head is None:
        problems.append(f"waiver {waiver_path.name}: missing a `head:` line")
    elif not wanted_head:
        problems.append(
            f"waiver {waiver_path.name}: the PR payload carries no head SHA to match"
        )
    elif not re.fullmatch(r"[0-9a-f]{7,40}", field_head.lower()):
        problems.append(
            f"waiver {waiver_path.name}: `head: {field_head}` is not a commit SHA"
        )
    elif not (
        wanted_head == field_head or wanted_head.startswith(field_head.lower())
    ):
        problems.append(
            f"waiver {waiver_path.name}: `head: {field_head[:12]}` does not match the "
            f"PR head {wanted_head[:12]}"
        )

    basis = _waiver_field(text, "basis") or ""
    if len(basis) < _WAIVER_BASIS_MIN or not _visible_len(basis) >= _WAIVER_BASIS_MIN:
        problems.append(
            f"waiver {waiver_path.name}: `basis:` needs at least "
            f"{_WAIVER_BASIS_MIN} characters of rationale, got {len(basis)}"
        )

    decl = _WAIVER_C3_DECL_RE.search(text)
    if decl is None:
        problems.append(
            f"waiver {waiver_path.name}: missing `c3-not-fabricated: yes` "
            "(the verify line must keep the recorded c3, never a back-filled 3)"
        )
    elif decl.group("value").lower() != "yes":
        problems.append(
            f"waiver {waiver_path.name}: `c3-not-fabricated: {decl.group('value')}` — "
            "a waiver may not bless a fabricated c3"
        )

    signed = _WAIVER_SIGNED_RE.search(text)
    if signed is None:
        problems.append(
            f"waiver {waiver_path.name}: missing the sign-off line "
            "`signed: lead <YYYY-MM-DD>`"
        )
    elif not _is_iso_date(signed.group("date")):
        problems.append(
            f"waiver {waiver_path.name}: `signed: … {signed.group('date')}` is not a real date"
        )

    if problems:
        return None, problems
    assert signed is not None  # narrowing for type checkers; problems handles None
    return (
        Waiver(
            path=waiver_path,
            repo=repo,
            pr=pr,
            head=field_head.lower(),
            signed=signed.group("date"),
        ),
        [],
    )


def split_waivable(problems: list[str]) -> tuple[list[str], list[str]]:
    """Split ``(blocking, waivable)``; the only waivable refusal is ``c3<3``."""
    blocking: list[str] = []
    waivable: list[str] = []
    for problem in problems:
        if _WAIVABLE_C3_RE.match(problem):
            waivable.append(problem)
        else:
            blocking.append(problem)
    return blocking, waivable


def _split_fences(text: str) -> tuple[list[str], list[str], bool]:
    """Split fenced lines from prose; ``(fenced, prose, unclosed)``."""
    fenced: list[str] = []
    prose: list[str] = []
    fence_char: str | None = None
    fence_len = 0
    for line in text.splitlines():
        stripped = line.strip()
        core = re.sub(r"^(?:>\s*)+|^[-*+]\s+", "", stripped)
        if fence_char is None:
            m = re.match(r"(`{3,}|~{3,})", core)
            if m:
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                fenced.append(line)
                continue
            prose.append(line)
        else:
            fenced.append(line)
            if core and set(core) == {fence_char} and len(core) >= fence_len:
                fence_char = None
    return fenced, prose, fence_char is not None


def _strip_decoration(text: str) -> str:
    """Drop markdown emphasis / inline-code markers before tokenising."""
    return text.replace("*", "").replace("`", "").strip()


def body_problems(body: str) -> list[str]:
    """Verify-record problems in a PR body (empty = exactly one clean record)."""
    problems: list[str] = []
    fenced, prose, unclosed = _split_fences(body)

    record_lines: list[tuple[str, str]] = []
    for raw_line in prose:
        m = _RECORD_RE.search(raw_line)
        if not m:
            continue
        # The authority strips emphasis / inline-code markers from EVERY
        # record (verify_account.parse_text), not just the whole-line-backtick
        # form — applying it conditionally made the mirror diverge both ways
        # (`c3=3*4` accepted here, `h*ardcap=no` refused here).
        raw = _strip_decoration(m.group(1))
        lead = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        shape = ""
        if "\t" in lead or len(lead) >= 4:
            shape = "indented code block"
        elif raw_line.lstrip().startswith(">") or re.match(r"^[-*+]\s*>", raw_line.lstrip()):
            shape = "block quote"
        record_lines.append((raw, shape))

    # A record inside a fence is never a record: with prose alongside it the set
    # is ambiguous, and an unclosed fence that swallowed one is an unfinished
    # template.  Scan with the SAME matcher the authority uses (a prefix test
    # misses `verify:rounds=…`, `**verify:** …` and numbered forms); an
    # unclosed fence that swallows nothing is harmless and stays accepted.
    if any(_RECORD_RE.search(line) for line in fenced):
        if unclosed:
            problems.append(
                "code fence never closes — an unfinished template must not become a record"
            )
        else:
            problems.append(
                "a verify record hides inside a fenced block — keep exactly the prose record"
            )

    if len(record_lines) == 0:
        if not problems:
            problems.append("body carries no `verify:` record (the clean-streak merge record)")
        return problems
    if len(record_lines) > 1:
        problems.append(f"body carries {len(record_lines)} verify records (exactly one required)")
        return problems

    raw, shape = record_lines[0]
    if shape:
        problems.append(
            f"verify record sits in a {shape} — it reads as a format template, "
            "not a record; move it into the prose"
        )
        return problems

    fields: dict[str, str] = {}
    for tok in _TOKEN_RE.finditer(raw):
        key = tok.group(1).lower().replace("_", "-")
        key = "post-cap" if key in {"postcap", "post-cap"} else key
        if key in fields:
            problems.append(f"verify record has a duplicate field {key!r}")
            return problems
        value = tok.group(2).rstrip(".,;)")
        fields[key] = value.lower() if key in {"hardcap", "budget"} else value

    missing = [k for k in _REQUIRED_FIELDS if k not in fields]
    if missing:
        return problems + [f"verify record is missing fields {missing} (placeholder template?)"]
    unknown = [k for k in fields if k not in _KNOWN_FIELDS]
    if unknown:
        return problems + [f"verify record has unknown fields {unknown} (typo or policy change?)"]

    # Counters must be integers BEFORE anything converts them — the authority
    # returns on the first non-integer, so a body like `c3=3）` is a clean
    # malformed_record there and must never raise here.
    numbers: dict[str, int] = {}
    for key in _COUNTER_FIELDS:
        if key not in fields:
            continue
        if not re.fullmatch(r"\d+", fields[key]):
            return problems + [
                f"verify record field {key}={fields[key]!r} is not a non-negative integer"
            ]
        numbers[key] = int(fields[key])

    rounds = numbers["rounds"]
    clean = numbers["clean"]
    if rounds < 1:
        problems.append("verify record rounds must be >= 1")
    if rounds > _HARD_ROUND_CAP:
        problems.append(
            f"verify record rounds={rounds} exceeds the hard cap {_HARD_ROUND_CAP} "
            "(post-cap rounds are listed separately, not folded in)"
        )
    if clean > rounds:
        problems.append(f"verify record clean={clean} > rounds={rounds}")
    if fields["hardcap"] not in _HARDCAP_VALUES:
        problems.append(f"verify record hardcap={fields['hardcap']!r} must be yes or no")
    if "budget" in fields and fields["budget"] not in _BUDGET_VALUES:
        problems.append(f"verify record budget={fields['budget']!r} must be ok or exhausted@3")
    if fields["hardcap"] == "yes" and rounds != _HARD_ROUND_CAP:
        problems.append(
            f"verify record hardcap=yes requires rounds={_HARD_ROUND_CAP}, got {rounds}"
        )
    if numbers.get("post-cap", 0) > 0 and fields["hardcap"] != "yes":
        problems.append("verify record post-cap>0 requires hardcap=yes (post-cap only follows the cap)")
    if "hardware" in fields and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", fields["hardware"]):
        problems.append(f"verify record hardware={fields['hardware']!r} is not a registry hw-id")
    if problems:
        return problems

    if "c3" not in fields:
        # Structural-exemption lane (doc tooling): no streak proves closure, so
        # the record itself must be clean and show the gates really ran.
        if clean < 1 or numbers.get("gates", 0) < 1:
            problems.append("structural exemption needs clean>=1 and gates>=1")
        if numbers.get("s0", 1) != 0 or numbers.get("s1", 1) != 0:
            problems.append("structural exemption needs S0=S1=0 (unfixed reds never exempt)")
        return problems

    c3 = numbers["c3"]
    if c3 < 3:
        problems.append(f"verify record c3={c3} < 3")
    if rounds < max(4, c3 + 1):
        problems.append(f"verify record rounds={rounds} < max(4, c3+1)")
    return problems


# ── PR-level checks ──────────────────────────────────────────────────────────


def check_pr(
    pr: dict,
    expect_head: str | None = None,
    *,
    allow_cjk: bool = False,
    base: str = DEFAULT_BASE,
) -> list[str]:
    """All merge-blocking problems of one PR payload (empty = merge allowed)."""
    problems: list[str] = []
    title = pr.get("title") or ""
    head = (pr.get("head") or {}).get("sha") or ""
    base_ref = (pr.get("base") or {}).get("ref") or ""

    if pr.get("state") != "open":
        problems.append(f"PR state={pr.get('state')!r}, not open")
    if pr.get("draft") is True:
        problems.append("PR is a draft — publish it before merging")
    mergeable = pr.get("mergeable")
    if mergeable is None:
        problems.append("mergeable is unknown (GitHub still computing) — retry in a few seconds")
    elif mergeable is not True:
        problems.append(f"mergeable={mergeable} — conflicts or branch protection refuse the merge")
    if not base_ref:
        problems.append("PR payload carries no base branch — refusing to merge blind")
    elif base_ref != base:
        problems.append(f"PR targets {base_ref!r}, the gate only merges into {base!r}")

    if not expect_head:
        # Fail closed: an empty pin means "no evidence supplied", never "no
        # check needed" — every other empty input here is refused for the same
        # reason, and treating "" as "not supplied" is precisely how the
        # reviewed-head check would switch itself off.
        problems.append("no reviewed head SHA supplied — the closing verification is unverified")
    elif len(expect_head) < MIN_HEAD_PREFIX:
        problems.append(
            f"reviewed head {expect_head!r} is shorter than {MIN_HEAD_PREFIX} characters — "
            "too little evidence to pin a commit"
        )
    elif not (head == expect_head or head.startswith(expect_head)):
        problems.append(
            f"head SHA {head[:12]} does not match the reviewed {expect_head[:12]} — "
            "pushed after the last review, closing verification void"
        )

    problems.extend(title_problems(title, allow_cjk=allow_cjk))
    problems.extend(body_problems(pr.get("body") or ""))
    return problems


# ── gh plumbing (subprocess — gh carries its own auth) ───────────────────────

_STATUS_LINE = re.compile(r"^HTTP/\S+\s+(\d{3})\b")


def gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    runner=None,
) -> tuple[int, dict, str]:
    """``gh api`` call returning ``(http_status, parsed_body, stderr)``.

    ``gh api`` exits 0 for every 2xx and does NOT print the numeric status, so
    the status is read from the ``-i`` header block (``HTTP/2.0 200 OK``).
    A non-zero exit means the request failed and the exit code is reported
    when no status line exists.  Never raises (rc 127 = gh unusable).
    """
    cmd = ["gh", "api", "-i", endpoint]
    if payload is not None or method != "GET":
        cmd.extend(["-X", method])
    if payload is not None:
        cmd.extend(["--input", "-"])
    call = runner or subprocess.run
    try:
        probe = call(
            cmd,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return 127, {}, str(exc)

    stdout = probe.stdout or ""
    status = 0
    body_text = stdout
    if stdout.startswith("HTTP/"):
        head, _, rest = stdout.partition("\n\n")
        m = _STATUS_LINE.match(head)
        if m:
            status = int(m.group(1))
        body_text = rest
    if not status:
        # No header block (old gh / a stub): a clean exit means "it worked".
        status = 200 if probe.returncode == 0 else probe.returncode
    try:
        parsed = json.loads(body_text) if body_text.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"_raw": parsed}
    return status, parsed, (probe.stderr or "").strip()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _failure_detail(status: int, body: dict, stderr: str) -> str:
    """Human detail for a failed call; never dresses an exit code as HTTP."""
    # 126/127 are the shell's "not executable" / "not found" codes and collide
    # with the HTTP range, so the launcher codes are excluded explicitly.
    looks_http = 100 <= status <= 599 and status not in (126, 127)
    if looks_http:
        return f"http={status} {json.dumps(body)[:200] if body else ''}".rstrip()
    return f"transport failure (gh exit {status}) {stderr or 'no stderr'}"


def _gate(
    repo: str,
    pr: int,
    expect_head: str | None,
    *,
    allow_cjk: bool,
    do_merge: bool,
    base: str = DEFAULT_BASE,
    runner=None,
    waiver: str | Path | None = None,
) -> int:
    status, data, stderr = gh_api(f"repos/{repo}/pulls/{pr}", runner=runner)
    if status != 200:
        print(f"❌ GET pull request failed: {_failure_detail(status, data, stderr)}", file=sys.stderr)
        return 1

    # The waiver is validated against the head the payload actually carries —
    # not against ``--expect-head``, which is the caller's claim.  A waiver for
    # a different commit must never authorise this one.
    grant: Waiver | None = None
    if waiver is not None:
        payload_head = (data.get("head") or {}).get("sha") or ""
        grant, waiver_problems = read_waiver(waiver, repo=repo, pr=pr, head=payload_head)
        if waiver_problems:
            for problem in waiver_problems:
                print(f"❌ {problem}", file=sys.stderr)
            print(
                "MERGE REFUSED — the waiver does not cover this merge",
                file=sys.stderr,
            )
            return 1

    problems = check_pr(data, expect_head, allow_cjk=allow_cjk, base=base)
    if grant is not None:
        problems, suppressed = split_waivable(problems)
        detail = "; ".join(suppressed) if suppressed else "nothing (waiver unused)"
        print(f"🔓 waiver: {grant.path} (signed {grant.signed}) — suppressed: {detail}")
    if problems:
        for p in problems:
            print(f"❌ {p}", file=sys.stderr)
        print("MERGE REFUSED — fix every item above, then re-run", file=sys.stderr)
        return 1
    title = data.get("title") or ""
    head = (data.get("head") or {}).get("sha") or ""
    print(f"✅ gate passed: head={head[:12]} title={title!r}")

    if not do_merge:
        print("(check mode only; re-run with `merge` to squash from this state)")
        return 0

    status, merged, stderr = gh_api(
        f"repos/{repo}/pulls/{pr}/merge",
        method="PUT",
        # `sha` pins the head the gate just validated: GitHub answers 409 when
        # the head has moved, so a push landing between the GET above and this
        # PUT can no longer be merged unvalidated.  (Without it the same-process
        # design merely removes the human step, not the race.)
        payload={"merge_method": "squash", "sha": head},
        runner=runner,
    )
    if status not in (200, 201):
        print(f"❌ merge request failed: {_failure_detail(status, merged, stderr)}", file=sys.stderr)
        return 1
    merged_sha = str(merged.get("sha") or "")
    if merged.get("merged") is not True or not merged_sha:
        # The merge may or may not have landed; do not claim either way, and do
        # not build a readback endpoint out of an empty SHA (it would hit the
        # list/latest-commit endpoint instead of the squash commit).
        print(
            "🚨 MERGE-RESPONSE-ANOMALY: the API answered without a merged=true + sha "
            f"pair ({json.dumps(merged)[:160]}) — check the PR state by hand before retrying "
            "(a retry of an already-merged PR is harmless, but do not assume success here)"
        )
        return 2
    print(f"✅ squash merged → {merged_sha[:12]}")

    expected_subject = f"{title} (#{pr})"
    subject: str | None = None
    attempts = 0
    for attempt in (1, 2):  # cannot read ≠ polluted — one transport retry
        attempts = attempt
        status, commit, _stderr = gh_api(f"repos/{repo}/commits/{merged_sha}", runner=runner)
        if status == 200:
            # An empty/absent message is "cannot read", not "polluted" — and an
            # unguarded splitlines()[0] would raise IndexError AFTER the merge
            # already landed, exiting rc 1 ("nothing merged") and inviting a
            # retry of an irreversible action.
            message = ((commit.get("commit") or {}).get("message") or "").strip()
            if message:
                subject = message.splitlines()[0]
            break
        if attempt == 1:
            time.sleep(3)
    if subject is None:
        print(
            f"🚨 COMMIT-GET-FAILED: the merge happened but the commit subject is unreadable "
            f"({attempts} attempt(s)) — verify the log by hand; no pollution marker written "
            "(cannot read ≠ polluted)"
        )
        return 2
    if subject != expected_subject:
        print(
            f"🚨 squash subject {subject!r} ≠ expected {expected_subject!r} — the caller "
            "(workspace wrapper) owns the sticky pollution alert for this outcome"
        )
        return 2
    print(f"✅ merged subject verified: {subject}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools pr-merge-gate",
        description="Pre-squash-merge gate: title / head / verify-record checks + guarded squash.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "merge"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True, help="OWNER/NAME")
        sp.add_argument("--pr", required=True, type=int)
        sp.add_argument(
            "--expect-head",
            required=True,
            default=None,
            help="head SHA verified by the review rounds (prefix ok)",
        )
        sp.add_argument(
            "--allow-cjk",
            action="store_true",
            help="repo allows CJK summaries (easy-hydro family)",
        )
        sp.add_argument("--base", default=DEFAULT_BASE, help="branch the gate may merge into")
        sp.add_argument(
            "--waiver",
            default=None,
            metavar="PATH",
            help=(
                "signed recorded-exception file "
                "(_alerts/pr-merge-waiver-<repo>-<pr>.md); suppresses ONLY the "
                "`verify record c3=<n> < 3` refusal, every other check still runs"
            ),
        )
    args = parser.parse_args(argv)
    return _gate(
        args.repo,
        args.pr,
        args.expect_head,
        allow_cjk=args.allow_cjk,
        do_merge=(args.cmd == "merge"),
        base=args.base,
        waiver=args.waiver,
    )


if __name__ == "__main__":
    raise SystemExit(main())
