#!/usr/bin/env python3
"""Generate categorized GitHub release notes from squash-merged PR subjects.

---

Every org repo squashes its PRs, so each commit on master is one PR whose
subject is ``<gitmoji> <Summary.> (#N)`` — enforced by ``commit-msg-lint``.
That makes the git history itself a changelog: this script walks the commits
reachable from a release tag, stops at the previous release tag, extracts the
PR reference from every subject, groups the PRs by their leading gitmoji, and
renders (or uploads) a standard release body::

    ## What's Changed

    ### ✨ Features
    - [#41](https://github.com/owner/repo/pull/41) ✨ Add invitation pages.

    ### 🐛 Fixes
    - [#40](https://github.com/owner/repo/pull/40) 🐛 Fix nonce endpoint.

    **Full Changelog**: https://github.com/owner/repo/compare/v0.1.0...v0.2.0

The previous tag is auto-resolved as the highest semver tag below ``--tag``
(any ``prefix-vX.Y.Z`` shape counts; floating tags such as ``res-latest``
never match). Releases that bundle many versions may pass ``--previous-tag``
explicitly; ``--no-previous`` lists the full history (first release).
Commits without a ``(#N)`` reference (direct pushes, reverts) are skipped —
the release story is the merged PRs.

Usage as a CLI::

    celestia-devtools release-notes --repo owner/repo --tag v0.2.0          # print
    celestia-devtools release-notes --tag v0.2.0 --apply                    # upload
    celestia-devtools release-notes --tag v0.2.0 --apply --publish          # + publish

Usage as a library::

    from celestia_devtools.publish.release_notes import build_body, collect_entries
    body = build_body("owner/repo", "v0.2.0", "v0.1.0", fetch=fake_fetch)

Authentication: ``GH_TOKEN`` / ``GITHUB_TOKEN`` in the environment, falling
back to ``gh auth token`` for local runs. Read-only calls on public repos work
unauthenticated (rate-limited); ``--apply`` requires a write-capable token.

The shared CI entry point is the reusable workflow
``celestia-island/celestia-devtools/.github/workflows/release-notes.yml`` —
repos gain the capability by adding one ``uses:`` job to their release
workflow, exactly like ``p0-gate.yml``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from celestia_devtools.vcs.commit_msg import GITMOJI_WHITELIST

API_ROOT = "https://api.github.com"

# ── gitmoji → release section mapping ────────────────────────────────────────
# Section headings keep a representative emoji so the categorized list reads
# like the org's hand-written releases (malkuth/evernight style). Lookup keys
# are normalized (VS16 stripped) forms of the whitelist emoji; any whitelist
# emoji without a mapping falls through to "Other Changes".

_SECTIONS: List[Tuple[str, Tuple[str, ...]]] = [
    ("✨ Features", ("\u2728", "\U0001f389")),                          # ✨ 🎉
    ("🐛 Fixes", ("\U0001f41b", "\U0001f691", "\U0001fa79",
                  "\u270f")),                                           # 🐛 🚑 🩹 ✏️
    ("⚡ Performance", ("\u26a1", "\U0001f4c8")),                       # ⚡ 📈
    ("♻️ Refactoring", ("\u267b", "\U0001f3a8",
                        "\U0001f3d7", "\U0001f69a")),                   # ♻️ 🎨 🏗️ 🚚
    ("💄 UI", ("\U0001f484", "\U0001f4f1", "\u267f")),                  # 💄 📱 ♿
    ("🌐 Localization", ("\U0001f310",)),                               # 🌐
    ("📝 Documentation", ("\U0001f4dd", "\U0001f4a1",
                          "\U0001f4ac")),                               # 📝 💡 💬
    ("✅ Tests", ("\u2705", "\U0001f9ea")),                             # ✅ 🧪
    ("⬆️ Dependencies", ("\u2b06", "\u2b07", "\U0001f4cc",
                         "\u2795", "\u2796")),                          # ⬆️ ⬇️ 📌 ➕ ➖
    ("🔒 Security", ("\U0001f512", "\U0001f6e1")),                      # 🔒 🛡️
    ("🔥 Removals", ("\U0001f525",)),                                   # 🔥
    ("🔧 Maintenance", ("\U0001f527", "\U0001f528", "\U0001f477",
                        "\U0001f4e6", "\U0001f680", "\U0001f371",
                        "\U0001f5c3", "\U0001f9f1", "\U0001f9ba",
                        "\U0001fa7a", "\U0001f6a8", "\U0001f331",
                        "\U0001f4f8", "\U0001f516",
                        "\U0001f9d1\u200d\U0001f4bb")),
    # 🔧 🔨 👷 📦 🚀 🍱 🗃️ 🧱 🦺 🩺 🚨 🌱 📸 🔖(release chores) 🧑‍💻
    ("📜 License", ("\U0001f4dc", "\U0001f4c4")),                       # 📜 📄
    ("🔄 Sync", ("\U0001f517", "\U0001f504")),                          # 🔗 🔄 (org additions)
]
OTHER_HEADING = "Other Changes"
_DEPENDABOT_HEADING = "⬆️ Dependencies"  # "Bump x from y to z" titles

_EMOJI_TO_SECTION: Dict[str, str] = {}
for _heading, _emojis in _SECTIONS:
    for _emoji in _emojis:
        _EMOJI_TO_SECTION.setdefault(_emoji, _heading)

# ── subject parsing ──────────────────────────────────────────────────────────

# Squash subject suffix: "<title> (#123)". Titles never end in ")" + "#N"
# before the reference, so a plain anchored regex is safe. A legacy variant
# (aoba) appends the closing period after the reference — "… (#123)." — so
# strip a trailing "." from the match tail when present.
_PR_SUFFIX_RE = re.compile(r"^(.*?)\s*\(#(\d+)\)\s*(?:\.\s*)?$", re.DOTALL)
_DEPENDABOT_RE = re.compile(r"^Bump\s")


def _normalize(emoji: str) -> str:
    """Drop the variation selector so both ⚡ and ⚡️ map to one key."""
    return emoji.replace("\ufe0f", "")


def match_gitmoji(subject: str) -> Optional[str]:
    """Return the longest whitelisted gitmoji prefixing *subject*, if any.

    The whitelist pins the canonical VS16 spellings, but subjects in the wild
    sometimes drop the variation selector — match both forms so classification
    never hinges on an invisible codepoint.
    """
    best: Optional[str] = None
    for emoji in GITMOJI_WHITELIST:
        for candidate in (emoji, _normalize(emoji)):
            if subject.startswith(candidate) and (
                best is None or len(candidate) > len(best)
            ):
                best = candidate
    return best


def parse_subject(subject: str) -> Tuple[Optional[int], str]:
    """Split a squash subject into ``(pr_number_or_None, title)``."""
    m = _PR_SUFFIX_RE.match(subject.strip())
    if m is None:
        return None, subject.strip()
    return int(m.group(2)), m.group(1).strip()


def classify(subject: str) -> str:
    """Map a squash subject to its release-section heading."""
    emoji = match_gitmoji(subject)
    if emoji is None:
        if _DEPENDABOT_RE.match(subject):
            return _DEPENDABOT_HEADING
        return OTHER_HEADING
    return _EMOJI_TO_SECTION.get(_normalize(emoji), OTHER_HEADING)


# ── GitHub API access (stdlib only, fetch injectable for tests) ──────────────

Fetch = Callable[[str], Any]


class GitHubError(RuntimeError):
    """A non-404 GitHub API failure."""


class NotFoundError(GitHubError):
    """The requested resource does not exist (HTTP 404)."""


def resolve_token() -> Optional[str]:
    """Token from the environment, falling back to the local ``gh`` login."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0:
        token = out.stdout.strip()
        if token:
            return token
    return None


def _headers(token: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "celestia-devtools",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(
    method: str,
    url: str,
    token: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFoundError(f"not found: {url}") from exc
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise GitHubError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc


def make_fetch(token: Optional[str]) -> Fetch:
    """Build a GET-and-parse-JSON function with all rate-limit handling inline."""
    def fetch(url: str) -> Any:
        _, data = _request("GET", url, token)
        return data
    return fetch


# ── tag resolution ───────────────────────────────────────────────────────────

# Anchored semver with an optional "<name>-" prefix and optional "v":
# v0.2.9, flasher-v1.2.3 … match; floating tags (fonts, res-latest) do not.
_SEMVER_RE = re.compile(r"^(?:[A-Za-z0-9]+-)?v?(\d+)\.(\d+)(?:\.(\d+))?$")


def parse_semver(tag: str) -> Optional[Tuple[int, int, int]]:
    m = _SEMVER_RE.match(tag.strip())
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def resolve_previous_tag(
    repo: str, tag: str, fetch: Fetch, max_pages: int = 50
) -> Optional[str]:
    """Highest semver tag below *tag*; ``None`` when *tag* is not semver-shaped."""
    current = parse_semver(tag)
    if current is None:
        return None
    names: List[str] = []
    for page in range(1, max_pages + 1):
        url = (f"{API_ROOT}/repos/{repo}/tags"
               f"?per_page=100&page={page}")
        batch = fetch(url)
        if not batch:
            break
        names.extend(entry["name"] for entry in batch)
        if len(batch) < 100:
            break
    best_name: Optional[str] = None
    best_version: Optional[Tuple[int, int, int]] = None
    for name in names:
        version = parse_semver(name)
        if version is None or version >= current:
            continue
        if best_version is None or version > best_version:
            best_name, best_version = name, version
    return best_name


def tag_commit_sha(repo: str, tag: str, fetch: Fetch) -> str:
    """Commit SHA behind *tag*, following the annotated-tag indirection."""
    ref = fetch(f"{API_ROOT}/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}")
    obj = ref["object"]
    if obj["type"] == "tag":
        annotated = fetch(f"{API_ROOT}/repos/{repo}/git/tags/{obj['sha']}")
        return annotated["object"]["sha"]
    return obj["sha"]


def find_release(
    repo: str, tag: str, fetch: Fetch, max_pages: int = 10
) -> Optional[Dict[str, Any]]:
    """The release object for *tag*, drafts included.

    ``GET /releases/tags/{tag}`` skips drafts, so fall back to paging the
    release list and matching ``tag_name`` — repos that keep releases as
    long-lived drafts (curate-then-publish) would otherwise be invisible.
    """
    try:
        return fetch(f"{API_ROOT}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}")
    except NotFoundError:
        for page in range(1, max_pages + 1):
            batch = fetch(
                f"{API_ROOT}/repos/{repo}/releases?per_page=100&page={page}"
            )
            if not batch:
                return None
            for release in batch:
                if release.get("tag_name") == tag:
                    return release
            if len(batch) < 100:
                return None
    return None


def walk_head(repo: str, tag: str, fetch: Fetch, on: str) -> str:
    """A ref ``commits?sha=`` accepts for walking history from *tag*.

    Normally the tag itself. Repos that lost their git tags (release objects
    surviving a history rewrite) still carry ``target_commitish`` on the
    release — fall back to it so the walk degrades to "everything reachable
    from that branch" instead of a hard 404.
    """
    try:
        return tag_commit_sha(repo, tag, fetch)
    except NotFoundError:
        release = find_release(repo, tag, fetch)
        target = (release or {}).get("target_commitish", "").strip()
        if not target:
            raise GitHubError(
                f"tag {tag!r} has no git ref and no release object with a "
                "target_commitish — cannot walk history"
            ) from None
        print(f"note: git ref for {tag!r} missing; walking {target!r} "
              f"({on})", file=sys.stderr)
        return target


# ── commit walking → categorized entries ─────────────────────────────────────

def collect_entries(
    repo: str,
    tag: str,
    previous_tag: Optional[str],
    fetch: Fetch,
    max_pages: int = 500,
) -> Tuple[List[Tuple[str, List[Tuple[int, str]]]], int]:
    """Walk commits from *tag* back to *previous_tag* and group PRs by section.

    Returns ``(sections, skipped)`` where *sections* preserves the fixed
    section order (empty sections dropped) and each item is ``(pr_number,
    title)`` in chronological order; *skipped* counts commits without a
    ``(#N)`` reference.
    """
    prev_sha: Optional[str] = None
    if previous_tag:
        try:
            prev_sha = tag_commit_sha(repo, previous_tag, fetch)
        except NotFoundError:
            print(f"warning: no git ref for previous tag {previous_tag!r} — "
                  "walking full history from the release tag instead",
                  file=sys.stderr)
    # An unresolvable base degenerates to a full walk rather than a hard fail.
    base_found = prev_sha is None
    head = walk_head(repo, tag, fetch, on="release target fallback")

    grouped: Dict[str, List[Tuple[int, str]]] = {}
    seen: set = set()
    skipped = 0

    for page in range(1, max_pages + 1):
        url = (f"{API_ROOT}/repos/{repo}/commits"
               f"?sha={urllib.parse.quote(head)}&per_page=100&page={page}")
        batch = fetch(url)
        if not batch:
            break
        for commit in batch:
            if prev_sha is not None and commit["sha"] == prev_sha:
                base_found = True
                break
            subject = commit["commit"]["message"].split("\n", 1)[0].rstrip()
            pr_number, title = parse_subject(subject)
            if pr_number is None:
                skipped += 1
                continue
            if pr_number in seen:
                continue
            seen.add(pr_number)
            grouped.setdefault(classify(subject), []).append((pr_number, title))
        if base_found or len(batch) < 100:
            break

    if not base_found:
        raise GitHubError(
            f"previous tag {previous_tag!r} was never reached walking history "
            f"from {tag!r} — it is probably not an ancestor; "
            "pass --previous-tag explicitly"
        )

    sections = [
        (heading, grouped[heading])
        for heading, _ in _SECTIONS
        if heading in grouped
    ]
    if OTHER_HEADING in grouped:
        sections.append((OTHER_HEADING, grouped[OTHER_HEADING]))
    return sections, skipped


def build_body(
    repo: str,
    tag: str,
    previous_tag: Optional[str],
    sections: List[Tuple[str, List[Tuple[int, str]]]],
) -> str:
    """Render the standard release body (matches GitHub's own voice)."""
    lines = ["## What's Changed", ""]
    if not sections:
        lines += ["_No pull requests were merged in this release._", ""]
    for heading, items in sections:
        lines.append(f"### {heading}")
        lines.append("")
        for pr_number, title in items:
            link = f"https://github.com/{repo}/pull/{pr_number}"
            lines.append(f"- [#{pr_number}]({link}) {title}")
        lines.append("")
    if previous_tag:
        compare = f"https://github.com/{repo}/compare/{previous_tag}...{tag}"
        lines.append(f"**Full Changelog**: {compare}")
    return "\n".join(lines).rstrip() + "\n"


# ── release upsert ───────────────────────────────────────────────────────────

def upsert_release(
    repo: str,
    tag: str,
    body: str,
    token: Optional[str],
    title: Optional[str] = None,
    publish: bool = False,
) -> str:
    """Set the body on the tag's release, creating a draft when missing.

    An existing release keeps its draft/published state (only the body — and
    the name, when *title* is given — is patched). Returns what happened.
    """
    base = f"{API_ROOT}/repos/{repo}"
    existing = find_release(repo, tag, make_fetch(token))

    if existing is not None:
        payload: Dict[str, Any] = {"body": body}
        if title:
            payload["name"] = title
        _request("PATCH", f"{base}/releases/{existing['id']}", token, payload)
        return f"updated release {tag} ({'draft' if existing.get('draft') else 'published'})"

    payload = {
        "tag_name": tag,
        "name": title or tag,
        "body": body,
        "draft": not publish,
    }
    _request("POST", f"{base}/releases", token, payload)
    state = "published" if publish else "draft"
    return f"created {state} release {tag}"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools release-notes",
        description=(
            "Generate categorized GitHub release notes from squash-merged "
            "PR subjects (grouped by leading gitmoji)."
        ),
    )
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--tag", default=os.environ.get("GITHUB_REF_NAME"),
        help="tag to release (default: $GITHUB_REF_NAME, as on a tag push)",
    )
    parser.add_argument(
        "--previous-tag", default=None,
        help="base tag for the change range (default: highest semver tag below --tag)",
    )
    parser.add_argument(
        "--no-previous", action="store_true",
        help="list the full history (first release) instead of resolving a base tag",
    )
    parser.add_argument(
        "--title", default=None,
        help="release name to set when creating (or patching) the release",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the body to the GitHub release instead of stdout",
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="with --apply: create the release published instead of as a draft",
    )
    parser.add_argument(
        "--out", default=None, help="write the body to FILE instead of stdout",
    )
    parser.add_argument(
        "--max-pages", type=int, default=500,
        help="safety cap on paginated commit walk (default: 500 = 50k commits)",
    )
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("--repo is required (or set GITHUB_REPOSITORY)")
    if not args.tag:
        parser.error("--tag is required (or set GITHUB_REF_NAME to a tag)")

    token = resolve_token()
    if token is None:
        if args.apply:
            print("error: --apply needs GH_TOKEN / GITHUB_TOKEN / gh auth",
                  file=sys.stderr)
            return 2
        print("warning: no token found — unauthenticated, rate-limited reads",
              file=sys.stderr)

    fetch = make_fetch(token)

    if args.no_previous:
        previous = None
    elif args.previous_tag:
        previous = args.previous_tag
    else:
        previous = resolve_previous_tag(args.repo, args.tag, fetch)
        if previous:
            print(f"note: previous tag resolved to {previous}", file=sys.stderr)

    sections, skipped = collect_entries(
        args.repo, args.tag, previous, fetch, max_pages=args.max_pages
    )
    if skipped:
        print(f"note: skipped {skipped} commit(s) without a \"(#PR)\" reference",
              file=sys.stderr)
    body = build_body(args.repo, args.tag, previous, sections)

    pr_count = sum(len(items) for _, items in sections)
    if args.apply:
        outcome = upsert_release(
            args.repo, args.tag, body, token,
            title=args.title, publish=args.publish,
        )
        print(f"note: {outcome} ({pr_count} PR(s) across "
              f"{len(sections)} section(s))", file=sys.stderr)
    elif args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print(f"note: wrote {args.out} ({pr_count} PR(s))", file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
