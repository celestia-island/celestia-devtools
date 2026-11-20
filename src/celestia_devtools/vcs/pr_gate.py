#!/usr/bin/env python3
"""Pre-squash-merge gate — mechanical title / head / verify-record checks that
must pass before a pull request may be squash-merged into ``master``.

Background (2026-11-20 ``probe-x`` incident): a debugging probe renamed a PR
("probe-x") and the rename was never reverted.  The squash merge carried the
polluted title straight into ``master`` as the commit subject — unfixable
without a force push, which the org forbids.  The pre-existing ``git log -1``
self-check is *after the fact*; this gate moves the check *before* the merge,
and issues the merge itself so checks and merge share one process (no TOCTOU
gap).

Checks (any failure → rc 1, merge refused):

1.  PR state == ``open`` and ``mergeable`` is true.
2.  Head SHA matches ``--expect-head`` (the SHA the review rounds verified;
    a short prefix is accepted).  A push after the last review invalidates
    the closing verification.
3.  Title passes :func:`celestia_devtools.vcs.commit_msg.lint` (single source
    for the gitmoji / uppercase / period / CJK / colon rules) plus PR-specific
    extras: minimum length, bare probe/placeholder words, any spelling of an
    in-title pull-request reference (squash appends its own `` (#N)``), and a
    mid-title version word (zero-width characters and Eastern-Arabic digits
    are normalised first).
4.  Body carries exactly one ``verify:`` record, parsed by the
    verify-account semantics delegated here from the workspace scanner:
    fenced blocks (both markers, unclosed), indented / quoted template
    shapes, the full required field set, and — on the ``c3`` lane —
    ``c3 >= 3`` with ``rounds >= max(4, c3 + 1)``.  Without ``c3`` the
    structural-exemption lane applies (``clean >= 1``, ``gates >= 1``,
    ``S0 == S1 == 0``; no rounds floor).  Records hidden in a fenced block
    are rejected too: a prose record plus a fenced record is an ambiguous
    record set.

Merge (``merge`` subcommand): the checks run and, only if they pass, the
squash merge is issued **without an explicit subject** so GitHub generates
``<title> (#N)`` from the validated title.  Immediately afterwards the merge
commit's subject is fetched and compared; three outcomes:

*  subject matches → rc 0;
*  mismatch → the sticky pollution alert is expected from the caller, rc 2;
*  commit unreadable (transport) → rc 2 as ``COMMIT-GET-FAILED`` *without*
   writing a pollution marker (cannot read ≠ polluted).

The workspace wrapper turns rc 2 into the sticky ``_alerts`` marker; this
module stays repo-agnostic and only prints.

CLI::

    pr-merge-gate check --repo OWNER/NAME --pr 123 [--expect-head SHA]
    pr-merge-gate merge --repo OWNER/NAME --pr 123 --expect-head SHA
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# ── PR-specific title extensions (on top of commit_msg.lint) ────────────────

MIN_TITLE_LEN = 20

# Bare probe / placeholder words — any standalone occurrence rejects the
# title.  Tokenised with [A-Za-z]+ so "testing"/"prober" never false-positive.
BANNED_TITLE_WORDS = {
    "wip", "draft", "probe", "test", "tmp", "todo", "fixme",
    "t", "xxx", "placeholder",
}

# Zero-width characters stripped before matching; Eastern-Arabic digits
# normalised to ASCII so "v١2" cannot smuggle a version word.
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Any spelling of an in-title pull-request reference: squash appends its own
# " (#N)", so a title that already carries one would end up duplicated.
_PR_REF_IN_TITLE = re.compile(
    r"(?:\(\s*#\s*\d+\s*\)|（\s*#\s*\d+\s*）)|#\s*\d{2,}"
)

# Mid-title version word: "Add v2 support." — Rule 4 only covers version
# *starts*, this catches the smuggled mid-sentence form.
_VERSION_MID_RE = re.compile(r"\bv\s?\d+\b", re.IGNORECASE)


def title_problems(title: str, *, allow_cjk: bool = False) -> list[str]:
    """PR-title problems beyond :func:`commit_msg.lint` (empty = compliant)."""
    from celestia_devtools.vcs.commit_msg import lint

    problems: list[str] = []
    title = title.translate(_ZERO_WIDTH).translate(_ARABIC_INDIC).strip()

    if len(title) < MIN_TITLE_LEN:
        problems.append(
            f"title too short ({len(title)} chars) — reads as a probe/placeholder: {title!r}"
        )

    problems.extend(lint(title, allow_cjk=allow_cjk))

    for word in re.findall(r"[A-Za-z]+", title):
        if word.lower() in BANNED_TITLE_WORDS:
            problems.append(f"title contains bare probe/placeholder word {word!r}")

    if _PR_REF_IN_TITLE.search(title):
        problems.append(
            "title carries its own PR reference — squash appends ' (#N)' itself"
        )
    if _VERSION_MID_RE.search(title):
        problems.append("title contains a version word (v2-style); describe the change instead")
    return problems


# ── verify-record body check (verify-account semantics) ──────────────────────

_REQUIRED_FIELDS = ("rounds", "clean", "s0", "s1", "s2", "s3", "hardcap")
# ── verify-account mirror constants ──
# Authoritative origin: the workspace scanner (`_tools/verify_account.py`),
# which runs pre-merge (skill step 6) and post-merge (6h watch).  Keep both
# in the same change when the record format evolves; the gate must never be
# looser than that scanner.
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


def body_problems(body: str) -> list[str]:
    """Verify-record problems in a PR body (empty = exactly one clean record).

    This is a faithful mirror of the workspace scanner's parser
    (``_tools/verify_account.py`` — ``RECORD_RE`` / ``TOKEN_RE`` /
    ``split_fences`` / ``strip_decoration`` / ``validate`` / template shapes).
    That scanner is the authoritative origin (it runs pre-merge as the skill's
    step 6 and post-merge every 6 hours); this mirror exists so the merge
    gate refuses exactly the bodies the scanner would red — the gate must
    never be looser than its detector.  When the workspace format evolves,
    update both in the same change (the format has been policy-stable since
    2026-11-09).
    """
    problems: list[str] = []
    fenced: list[str] = []
    prose: list[str] = []
    fence_char: str | None = None
    fence_len = 0

    for line in body.splitlines():
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
    if fence_char is not None:
        problems.append("code fence never closes — an unfinished template must not silently become a record")

    for ln in fenced:
        stripped = ln.strip()
        if stripped.startswith("verify: ") or (
            stripped.startswith("`verify: ") and stripped.endswith("`")
        ):
            problems.append(
                "a verify record hides inside a fenced block — keep exactly the prose record"
            )

    records: list[dict[str, str]] = []
    record_lines: list[tuple[str, str]] = []
    for raw_line in prose:
        m = _RECORD_RE.search(raw_line)
        if not m:
            continue
        raw = m.group(1)
        stripped = raw_line.strip()
        if stripped.startswith("`verify: ") and stripped.endswith("`"):
            raw = _strip_decoration(raw)
        lead = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        shape = ""
        if "\t" in lead or len(lead) >= 4:
            shape = "indented code block"
        elif raw_line.lstrip().startswith(">") or re.match(r"^[-*+]\s*>", raw_line.lstrip()):
            shape = "block quote"
        record_lines.append((raw, shape))

    if len(record_lines) == 0 and not problems:
        problems.append("body carries no `verify:` record (the clean-streak merge record)")
        return problems
    if len(record_lines) > 1:
        problems.append(f"body carries {len(record_lines)} verify records (exactly one required)")
        return problems

    for raw, shape in record_lines:
        if shape:
            problems.append(
                f"verify record sits in a {shape} — it reads as a format template, not a record; move it into the prose"
            )
            continue
        fields: dict[str, str] = {}
        dup = False
        for tok in _TOKEN_RE.finditer(raw):
            k = tok.group(1).lower().replace("_", "-")
            k = "post-cap" if k in {"postcap", "post-cap"} else k
            if k in fields:
                problems.append(f"verify record has a duplicate field {k!r}")
                dup = True
                continue
            fields[k] = tok.group(2).rstrip(".,;)").lower() if k in {"hardcap", "budget"} else tok.group(2).rstrip(".,;)")
        if dup:
            continue
        missing = [k for k in _REQUIRED_FIELDS if k not in fields]
        if missing:
            problems.append(f"verify record is missing fields {missing} (placeholder template?)")
            continue
        unknown = [k for k in fields if k not in _KNOWN_FIELDS]
        if unknown:
            problems.append(f"verify record has unknown fields {unknown} (typo or policy change?)")
            continue
        for k in _COUNTER_FIELDS:
            if k in fields and not re.fullmatch(r"\d+", fields[k]):
                problems.append(f"verify record field {k}={fields[k]!r} is not a non-negative integer")
        rounds = int(fields["rounds"])
        clean = int(fields["clean"])
        if rounds < 1:
            problems.append("verify record rounds must be >= 1")
        if rounds > _HARD_ROUND_CAP:
            problems.append(
                f"verify record rounds={rounds} exceeds the hard cap {_HARD_ROUND_CAP} "
                "(post-cap rounds are listed separately, not folded in)"
            )
        if clean > rounds:
            problems.append(f"verify record clean={clean} > rounds={rounds}")
        if fields["hardcap"] not in {"yes", "no"}:
            problems.append(f"verify record hardcap={fields['hardcap']!r} must be yes or no")
        if "budget" in fields and fields["budget"] not in {"ok", "exhausted@3"}:
            problems.append(f"verify record budget={fields['budget']!r} must be ok or exhausted@3")
        if fields["hardcap"] == "yes" and rounds != _HARD_ROUND_CAP:
            problems.append(f"verify record hardcap=yes requires rounds={_HARD_ROUND_CAP}, got {rounds}")
        if int(fields.get("post-cap", "0")) > 0 and fields["hardcap"] != "yes":
            problems.append("verify record post-cap>0 requires hardcap=yes (post-cap only follows the cap)")
        if "hardware" in fields and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", fields["hardware"]):
            problems.append(f"verify record hardware={fields['hardware']!r} is not a registry hw-id")

        if "c3" not in fields:
            # Structural-exemption lane (doc tooling): no streak to prove
            # closure, so the record itself must be clean and show the gates
            # really ran.
            if clean < 1 or int(fields.get("gates", "0")) < 1:
                problems.append("structural exemption needs clean>=1 and gates>=1")
            if int(fields.get("s0", "1")) != 0 or int(fields.get("s1", "1")) != 0:
                problems.append("structural exemption needs S0=S1=0 (unfixed reds never exempt)")
            continue
        # c3 lane: c3 >= 3 is a floor (c3=4 is a stronger result) and the
        # S0/S1/S2/S3 counters are *totals across all rounds* including
        # already-fixed findings — the closing clean streak is what proves
        # everything landed, so no zero-counter gate on this lane.
        c3 = int(fields["c3"])
        if c3 < 3:
            problems.append(f"verify record c3={c3} < 3")
        if rounds < max(4, c3 + 1):
            problems.append(f"verify record rounds={rounds} < max(4, c3+1)")
    return problems


def _strip_decoration(text: str) -> str:
    """Drop markdown emphasis / inline-code markers before tokenising."""
    return text.replace("*", "").replace("`", "").strip()


def _strip_decoration(text: str) -> str:
    """Drop markdown emphasis / inline-code markers before tokenising."""
    return text.replace("*", "").replace("`", "").strip()


def _parse_fields(line: str) -> dict[str, str]:
    """Lower-cased k=v fields of one verify record (S0=0 parses as s0=0)."""
    pairs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", line)
    return {k.lower(): v for k, v in pairs}


# ── PR-level checks ──────────────────────────────────────────────────────────


def check_pr(pr: dict, expect_head: str | None = None, *, allow_cjk: bool = False) -> list[str]:
    """All merge-blocking problems of one PR payload (empty = merge allowed)."""
    problems: list[str] = []
    title = pr.get("title") or ""
    head = (pr.get("head") or {}).get("sha") or ""

    if pr.get("state") != "open":
        problems.append(f"PR state={pr.get('state')!r}, not open")
    mergeable = pr.get("mergeable")
    if mergeable is None:
        problems.append("mergeable is unknown (GitHub still computing) — retry in a few seconds")
    elif mergeable is not True:
        problems.append(f"mergeable={mergeable} — conflicts or branch protection refuse the merge")

    if expect_head:
        if not (head == expect_head or head.startswith(expect_head)):
            problems.append(
                f"head SHA {head[:12]} does not match the reviewed "
                f"{expect_head[:12]} — pushed after the last review, closing verification void"
            )

    problems.extend(title_problems(title, allow_cjk=allow_cjk))
    problems.extend(body_problems(pr.get("body") or ""))
    return problems


# ── gh plumbing (subprocess — gh carries its own auth) ───────────────────────


def gh_api(endpoint: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    """``gh api`` call; never raises (rc 127 = gh unusable)."""
    cmd = ["gh", "api", endpoint]
    if payload is not None or method != "GET":
        cmd.extend(["-X", method])
    if payload is not None:
        cmd.extend(["--input", "-"])
    try:
        probe = subprocess.run(
            cmd,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return 127, {}
    try:
        parsed = json.loads(probe.stdout) if probe.stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"_raw": parsed}
    return probe.returncode, parsed


# ── CLI ──────────────────────────────────────────────────────────────────────


def _gate(repo: str, pr: int, expect_head: str | None, *, allow_cjk: bool, do_merge: bool) -> int:
    code, data = gh_api(f"repos/{repo}/pulls/{pr}")
    if code != 200:
        print(f"❌ GET pull request failed http={code}: {json.dumps(data)[:200]}", file=sys.stderr)
        return 1
    problems = check_pr(data, expect_head, allow_cjk=allow_cjk)
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

    code, m = gh_api(f"repos/{repo}/pulls/{pr}/merge", method="PUT", payload={"merge_method": "squash"})
    if code not in (200, 201):
        # Fail closed: anything that is not an explicit success is treated as
        # "not merged" and printed verbatim.
        print(f"❌ merge request failed http={code}: {json.dumps(m)[:200]}", file=sys.stderr)
        return 1
    merged_sha = m.get("sha", "")
    print(f"✅ squash merged → {merged_sha[:12]}")

    expected_subject = f"{title} (#{pr})"
    subject: str | None = None
    for attempt in (1, 2):  # cannot read ≠ polluted — one transport retry
        code, c = gh_api(f"repos/{repo}/commits/{merged_sha}")
        if code == 200:
            subject = (c.get("commit", {}).get("message") or "").splitlines()[0]
            break
        if attempt == 1:
            import time

            time.sleep(3)
    if subject is None:
        print(
            "🚨 COMMIT-GET-FAILED: the merge happened but the commit subject is unreadable "
            "(two attempts) — verify `git log` by hand; no pollution marker written (cannot read ≠ polluted)"
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


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools pr-merge-gate",
        description="Pre-squash-merge gate: title / head / verify-record checks + guarded squash.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "merge"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True, help="OWNER/NAME")
        sp.add_argument("--pr", required=True, type=int)
        sp.add_argument("--expect-head", required=(name == "merge"), default=None,
                        help="head SHA verified by the review rounds (prefix ok)")
        sp.add_argument("--allow-cjk", action="store_true",
                        help="repo allows CJK summaries (easy-hydro family)")
    args = parser.parse_args()
    return _gate(args.repo, args.pr, args.expect_head, allow_cjk=args.allow_cjk,
                 do_merge=(args.cmd == "merge"))


if __name__ == "__main__":
    raise SystemExit(main())
