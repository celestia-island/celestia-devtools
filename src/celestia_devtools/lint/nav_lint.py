#!/usr/bin/env python3
"""Lint navigation call sites for poisoned-target producers.

*Why this exists:* the family field bug class where a navigation target
stringifies without a leading ``/`` (an interpolated ``undefined`` is
the classic) makes vue-router compose ``origin + base + to`` into a
cross-origin URL; pushState throws SecurityError and the router's catch
branch performs a FULL-PAGE ``location.assign`` onto the broken host —
observed as ``https://<host>undefined/``. Runtime layers now fold this
class (hikari ``installNavigationSafetyNet``), but every producer that
bypasses the History API — direct ``window.location`` writes — can still
leave the origin. This scanner makes the audit mechanical: any
navigation call whose target is not provably in-app must carry an
explicit ``nav-ok`` annotation naming its validation, or the gate
fails.

*Convention:* the following are considered provably safe without
annotation:

- ``router.push("/literal")`` / ``router.replace`` with a string or
  template literal starting with ``/`` or ``#``;
- router object locations with literal ``path``/``name`` members, or
  hash/query-only updates (``{ hash, query }`` — no path change);
- ``location.assign("/literal")`` / ``location.href = "/literal"`` /
  ``location.replace("/literal")`` with ``/``-rooted literals;
- ``history.pushState/replaceState`` whose URL argument is a
  ``/``/``#``/``?``-rooted literal, a template starting with one of
  those, or a ``location.pathname``-prefixed composition.

Everything else must be annotated on the same line or the line above
with ``nav-ok`` (JS) or ``<!-- nav-ok -->`` (Vue templates), e.g.::

    window.location.href = url; // nav-ok: validated by startLoginMethod

The annotation must state WHY the target cannot leave the origin; a
bare ``nav-ok`` without a reason fails with ``--require-reason``
(default on).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: File suffixes scanned (JS/TS family, incl. Vue SFCs).
SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".vue", ".mjs", ".cjs"}

#: Directory names never scanned.
SKIP_DIRS = {"node_modules", "dist", ".generated", ".git", "coverage", "target"}

#: Test files are skipped by default: they deliberately feed hostile
#: targets to the APIs under test. Scan them with --include-tests.
TEST_PAT = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$|(^|/)__tests__/")

# router/$router.push(...) / .replace(...)
_ROUTER_CALL_RE = re.compile(
    r"(?<![\w$])(\$?\brouter)\.(push|replace)\s*\("
)

# location.assign(...) / location.replace(...) — the WRITE side of the
# Location API (full-page navigations that bypass the History net).
_LOCATION_CALL_RE = re.compile(
    r"(?<![\w$])(?:window\.)?location\.(assign|replace)\s*\("
)

# location.href = <expr> (and window.location.href)
_LOCATION_HREF_RE = re.compile(
    r"(?<![\w$])(?:window\.)?location\.href\s*=\s*([^;]+)"
)

# history.pushState/replaceState(state, title, url)
_HISTORY_CALL_RE = re.compile(
    r"(?<![\w$])(?:window\.)?history\.(pushState|replaceState)\s*\("
)

#: nav-ok escape hatch: same line or the line above. JS ``// nav-ok`` /
#: ``/* nav-ok */`` and Vue ``<!-- nav-ok -->``.
_NAV_OK_RE = re.compile(r"nav-ok\b")

#: The annotation must carry a reason (anything non-empty after the
#: marker, e.g. ``nav-ok: validated upstream``).
_NAV_OK_REASON_RE = re.compile(r"nav-ok\s*[:：]\s*\S+")


@dataclass
class Finding:
    path: Path
    line: int
    code: str
    excerpt: str
    detail: str

    def render(self) -> str:
        loc = f"{self.path}:{self.line}"
        return f"{loc}: [{self.code}] {self.detail}\n    {self.excerpt.strip()}"


# ---------------------------------------------------------------------------
# Argument classification
# ---------------------------------------------------------------------------


def _first_argument(source: str, start: int) -> str:
    """Extract the first argument text (up to a top-level , or ))."""
    depth = 0
    out: List[str] = []
    for i in range(start, min(start + 400, len(source))):
        ch = source[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def _literal_root(arg: str) -> str | None:
    """Return the leading character if arg is a quoted/template literal.

    ``"/x"`` -> ``"/"``; `` `/@${id}` `` -> ``"/"``; ``"#h"`` -> ``"#"``
    (first char inside the quotes). Returns None for non-literals.
    """
    a = arg.lstrip()
    for q in ('"', "'", "`"):
        if a.startswith(q):
            inner = a[1:]
            return inner[:1] if inner else None
    return None


def _is_router_object_safe(arg: str) -> bool:
    """Object locations: literal path/name, or hash/query-only updates."""
    a = arg.strip()
    if not a.startswith("{"):
        return False
    if re.search(r"\bpath\s*:\s*['\"`]/", a):
        return True
    if re.search(r"\bname\s*:\s*['\"`]", a):
        return True
    # hash-only / query-only / params-only updates keep the current path.
    if a and not re.search(r"\bpath\b", a):
        if re.search(r"\b(hash|query|params|force)\s*:", a):
            return True
    return False


def _classify_router_arg(arg: str) -> str | None:
    root = _literal_root(arg)
    if root in ("/", "#"):
        return None
    flat = _flatten_object(arg)
    if flat.startswith("{") and _is_router_object_safe(flat):
        return None
    # Ternary object locations (`cond ? { path: "/x" } : { path: "/y" }`):
    # safe only when EVERY branch is provably safe.
    if "?" in flat and re.search(r"\?\s*\{", flat):
        branches = _split_ternary(flat)
        if branches and all(
            (lambda b: _literal_root(b) in ("/", "#")
             or (b.strip().startswith("{") and _is_router_object_safe(b)))(b)
            for b in branches
        ):
            return None
    return (
        "router-target"
        if root is None
        else f"router-target-rooted-{root!r}"
    )


def _split_ternary(flat: str) -> List[str]:
    """Split a flattened ternary into its branch expressions."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    seen_q = False
    for ch in flat:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0:
            seen_q = True
            cur = []
            continue
        elif ch == ":" and depth == 0 and seen_q:
            parts.append("".join(cur))
            cur = []
            continue
        if seen_q:
            cur.append(ch)
    if seen_q and cur:
        parts.append("".join(cur))
    return parts


def _flatten_object(arg: str) -> str:
    """Object arguments may span lines; collapse to a single line."""
    return arg.replace("\n", " ")


def _classify_location_arg(arg: str) -> str | None:
    root = _literal_root(arg)
    if root == "/":
        return None
    # location writes are the open-redirect surface: anything not a
    # /-rooted literal needs an annotation (absolute URLs, origin
    # concatenations, variables — all flag).
    return "location-target"


def _classify_history_args(source: str, open_paren: int) -> str | None:
    """History calls: the URL is argument 3 (index 2)."""
    args: List[str] = []
    depth = 0
    cur: List[str] = []
    i = open_paren
    n = len(source)
    while i < n and len(args) <= 3:
        ch = source[i]
        if ch in "([{":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                if cur:
                    args.append("".join(cur).strip())
                break
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        if depth >= 1:
            cur.append(ch)
        i += 1
    if len(args) < 3:
        return None  # no URL argument — cannot leave the origin
    url = args[2]
    root = _literal_root(url)
    if root in ("/", "#", "?"):
        return None
    # pathname-prefixed compositions are same-origin by construction
    if re.search(r"^['\"`]?\s*\$\{?\s*(?:window\.)?location\.pathname", url):
        return None
    return "history-url"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_file(path: Path, require_reason: bool = True) -> List[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Blank // line comments AND /* block comments */ so call sites that
    # only exist inside prose (doc comments) never count.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    lines = text.splitlines()
    findings: List[Finding] = []

    def annotated_ok(idx: int) -> bool:
        window = lines[max(0, idx - 1) : idx + 1]
        if not any(_NAV_OK_RE.search(w) for w in window):
            return False
        if not require_reason:
            return True
        return any(_NAV_OK_REASON_RE.search(w) for w in window)

    for idx, raw in enumerate(lines):
        line = re.sub(r"//.*$", "", raw)
        # Calls may continue onto following lines; classify the first
        # argument against the joined window (single-line calls are the
        # common case and behave identically).
        joined = "\n".join(lines[idx : idx + 6])
        m = _ROUTER_CALL_RE.search(line)
        if m and not annotated_ok(idx):
            arg = _first_argument(joined, m.end())
            code = _classify_router_arg(arg)
            if code:
                findings.append(
                    Finding(path, idx + 1, code, raw,
                            f"router.{m.group(2)} target is not provably in-app "
                            f"(arg: {arg[:60].replace(chr(10), ' ')!r}); annotate with "
                            f"'nav-ok: <reason>' or navigate via a /-rooted literal")
                )
        m = _LOCATION_CALL_RE.search(line)
        if m and not annotated_ok(idx):
            arg = _first_argument(joined, m.end())
            if _classify_location_arg(arg):
                findings.append(
                    Finding(path, idx + 1, "location-target", raw,
                            f"location.{m.group(1)} bypasses the History net — "
                            f"target must be a /-rooted literal or carry "
                            f"'nav-ok: <reason>' (arg: {arg[:60].replace(chr(10), ' ')!r})")
                )
        m = _LOCATION_HREF_RE.search(line)
        if m and not annotated_ok(idx):
            if _classify_location_arg(m.group(1)):
                findings.append(
                    Finding(path, idx + 1, "location-target", raw,
                            f"location.href write bypasses the History net — "
                            f"target must be a /-rooted literal or carry "
                            f"'nav-ok: <reason>' (arg: {m.group(1)[:60]!r})")
                )
        m = _HISTORY_CALL_RE.search(line)
        if m and not annotated_ok(idx):
            code = _classify_history_args(joined, m.end() - 1)
            if code:
                findings.append(
                    Finding(path, idx + 1, code, raw,
                            f"history.{m.group(1)} URL argument is not provably "
                            "same-origin; use a /-#-?-rooted literal or a "
                            "location.pathname-prefixed composition")
                )
    return findings


def iter_targets(paths: Sequence[str | Path], include_tests: bool = False) -> Iterable[Path]:
    for p in map(Path, paths):
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in SUFFIXES:
                    continue
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                if not include_tests and TEST_PAT.search(str(f)):
                    continue
                yield f
        elif p.is_file() and p.suffix.lower() in SUFFIXES:
            yield p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nav-lint",
        description="Gate navigation call sites against poisoned/off-origin targets.",
    )
    parser.add_argument("paths", nargs="+", help="Files or directories to scan")
    parser.add_argument(
        "--allow-bare-nav-ok",
        action="store_true",
        help="Accept 'nav-ok' annotations without a stated reason.",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Also scan *.test.* / *.spec.* files (they intentionally feed hostile targets).",
    )
    args = parser.parse_args(argv)

    findings: List[Finding] = []
    for f in iter_targets(args.paths, include_tests=args.include_tests):
        findings.extend(scan_file(f, require_reason=not args.allow_bare_nav_ok))

    if not findings:
        print("nav-lint: clean")
        return 0
    for f in findings:
        print(f.render())
    print(f"nav-lint: {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
