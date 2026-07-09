#!/usr/bin/env python3
"""git filter-branch --msg-filter script: auto-fix commit subject lines.

Reads a commit message from stdin, fixes the subject line against the
celestia-island gitmoji convention, and writes the result to stdout.
Only the subject (first line) is touched; the body and trailers are
preserved exactly.
"""
import sys
import re

CONV_MAP = {
    "feat": "\u2728",
    "fix": "\U0001f41b",
    "docs": "\U0001f4dd",
    "refactor": "\u267b\ufe0f",
    "perf": "\u26a1\ufe0f",
    "test": "\u2705",
    "build": "\U0001f477",
    "ci": "\U0001f527",
    "chore": "\U0001f527",
    "style": "\U0001f3a8",
    "revert": "\u23ea",
    "net": "\u2728",
    "init": "\U0001f680",
}

GITMOJI_SET = {
    "\U0001f3a8", "\u26a1\ufe0f", "\U0001f525", "\U0001f41b", "\U0001f691",
    "\u2728", "\U0001f4dd", "\U0001f680", "\U0001f484", "\U0001f389",
    "\u2705", "\U0001f512", "\U0001f516", "\U0001f6a8", "\U0001f6a7",
    "\U0001f49a", "\u2b07\ufe0f", "\u2b06\ufe0f", "\U0001f4cc", "\U0001f477",
    "\U0001f4c8", "\u267b\ufe0f", "\u2795", "\u2796", "\U0001f527",
    "\U0001f528", "\U0001f310", "\u270f\ufe0f", "\U0001f4a9", "\u23ea",
    "\U0001f500", "\U0001f504", "\U0001f4e6", "\U0001f47d", "\U0001f69a",
    "\U0001f4c4", "\U0001f4a5", "\U0001f371", "\u267f\ufe0f", "\U0001f4a1",
    "\U0001f37b", "\U0001f4ac", "\U0001f5c3\ufe0f", "\U0001f50a", "\U0001f507",
    "\U0001f465", "\U0001f6b8", "\U0001f3d7\ufe0f", "\U0001f4f1", "\U0001f921",
    "\U0001f95a", "\U0001f648", "\U0001f4f8", "\u2697\ufe0f", "\U0001f50d",
    "\U0001f3f7\ufe0f", "\U0001f331", "\U0001f6a9", "\U0001f945", "\U0001f4ab",
    "\U0001f5d1\ufe0f", "\U0001f6c2", "\U0001fa79", "\U0001f9d0", "\u26b0\ufe0f",
    "\U0001f9ea", "\U0001f454", "\U0001fa7a", "\U0001f9f1",
    "\U0001f9d1\u200d\U0001f4bb", "\U0001f4b8", "\U0001f9f5", "\U0001f9ba",
    "\U0001f4dc",
}

CONV_PREFIX_RE = re.compile(
    r"^(feat|fix|docs|refactor|perf|test|build|ci|chore|style|revert|net|init)"
    r"(?:\([^)]*\))?\s*[:!]\s*",
)
FIRST_ALPHA_RE = re.compile(r"[A-Za-z]")
ENDS_PERIOD_RE = re.compile(r"\.\s*$")


def fix_subject(subject: str) -> str:
    fixed = subject
    has_gitmoji = False
    for g in sorted(GITMOJI_SET, key=len, reverse=True):
        if fixed.startswith(g):
            has_gitmoji = True
            break

    if not has_gitmoji:
        m = CONV_PREFIX_RE.match(fixed)
        if m:
            emoji = CONV_MAP.get(m.group(1), "\u2728")
            rest = fixed[m.end():]
            fixed = emoji + " " + rest

    tail = fixed
    for g in sorted(GITMOJI_SET, key=len, reverse=True):
        if fixed.startswith(g):
            tail = fixed[len(g):].lstrip()
            break
    alpha = FIRST_ALPHA_RE.search(tail)
    if alpha and not alpha.group()[0].isupper():
        idx = alpha.start()
        pos = len(fixed) - len(tail) + idx
        fixed = fixed[:pos] + alpha.group()[0].upper() + fixed[pos + 1:]

    if not fixed.startswith("\U0001f504") and not ENDS_PERIOD_RE.search(fixed):
        fixed = fixed.rstrip() + "."

    return fixed


def main() -> int:
    raw = sys.stdin.read()
    if "\n" in raw:
        subject, body = raw.split("\n", 1)
        if body.endswith("\n"):
            body = body.rstrip("\n") + "\n"
    else:
        subject = raw
        body = ""

    fixed_subject = fix_subject(subject.rstrip("\r"))

    if body:
        sys.stdout.write(fixed_subject + "\n" + body)
    else:
        sys.stdout.write(fixed_subject + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
