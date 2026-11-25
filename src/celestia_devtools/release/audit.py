#!/usr/bin/env python3
"""Reconcile a repository's release tags against the registries it publishes to.

*Why this exists:* hikari's npm line drifted away from its own history. The
0.55 line ran 450 publishes in 70 days (up to 21 a day), 73 publishes never
got a git tag, 36 patch numbers were burned by dispatch typos, and seven
breaking changes — component removals and splits — shipped inside patch
releases. The registry knew 577 versions and the repository knew 543 tags, and
nothing compared the two, so from whichever side a human happened to inspect,
every number looked intentional.

The org rule (`docs/agent-workflow-convention.md` rule 4) is that the tag
and the published version are one release: numbered once, never reused,
published from a tag. This command is that rule's mechanical reader. It lists
the repository's tags with `git tag --list`, fetches the versions a
registry actually serves, and reports every disagreement. A lane that cannot
be read fails the command instead of passing it: a gate that cannot see both
sides must not report "clean" — that is how this drift accumulated in the
first place.

*Rules* (a tag carries the version after its lane prefix — `v0.55.12`
for npm, `cargo-v0.6.0` for crates):

`published-without-tag`
    the registry serves a version the repository never tagged. A version
    nobody can point at a commit is a version nobody can reproduce; the
    published bytes and the tree they were built from share no coordinate.
`tag-without-published`
    the repository tagged a version the registry does not serve. The release
    notes promise an artifact that cannot be installed, and the tag claims a
    lane the world never received.
`skipped-patch` (warning, never drift)
    a patch number missing inside the NEWEST published minor. The rule says do
    not back-fill: the number is burned, and re-publishing it would make one
    version mean two artifacts. The count is exact; the printed list is capped
    by `--skips-limit` so a badly drifted line stays readable.
`ignored`
    versions and tags BELOW `--ignore-below` are pre-publish history, not
    drift: hikari's `v0.1`-`v0.4` tags predate the npm line entirely, and a
    floor keeps them out of the judgement without deleting them.

Minor and patch numbers are compared numerically, so `0.10` is newer than
`0.9` and `0.55.100` newer than `0.55.9` — a string sort would decide
both backwards and silently pick the wrong "latest". A pre-release sorts below
its release (`1.0.0-rc.1 < 1.0.0`). The skip range starts at the lowest
patch the newest minor actually published, so a line that legitimately begins
at `X.Y.1` does not report `.0` as burned.

*Transport:* registries are plain `urllib.request` calls that honour the
:mod:`celestia_devtools.core.netproxy` cascade (the five-level detection is
read once per process), send a User-Agent — crates.io refuses a request
without one — and time out, so a hung socket fails the gate instead of hanging
CI. npm answers with a packument whose `versions` keys are the published
versions; crates.io answers with `versions[].num`. Yanked crates still
count as published: they existed, they were tagged, and a yanked version with
no tag is still a lane nobody can trace.

Usage::

    celestia-devtools release-audit --npm @celestia-island/hikari --repo ../hikari --ignore-below 0.30.0
    celestia-devtools release-audit --crates hikari-components --crate-tag-prefix cargo-v
    celestia-devtools release-audit --npm @celestia-island/hikari --json
    celestia-devtools release-audit --selftest

Exit: 0 clean | 1 drift | 2 cannot verify (not a git repository, unreadable
registry answer, timeout) or a usage error. `--selftest` proves the
comparison offline against fixed tag/published sets: 0 pass, 1 fail.
"""

from __future__ import annotations
import argparse
import http.client
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple
from celestia_devtools.core import envelope, netproxy

#: Sent on every registry call: crates.io rejects a request without one, and
#: npm's registry uses it to attribute traffic. Kept ASCII and versionless (a
#: user-agent that changes per release is a cache key that changes per release).
USER_AGENT = "celestia-devtools-release-audit (+https://github.com/celestia-island/celestia-devtools)"

#: Registries. A scoped npm name is escaped whole (`@scope/name` ->
#: `%40scope%2Fname`), which both registries accept and which keeps the name
#: from being mistaken for a path.
NPM_REGISTRY = "https://registry.npmjs.org/"
CRATES_REGISTRY = "https://crates.io/api/v1/crates/"

#: Upper bound for one registry call. A wedged socket must fail the gate, not
#: hold the CI job open until the runner's own timeout fires.
HTTP_TIMEOUT = 30.0

#: Default cap on the printed skipped-patch list (the counts are always exact).
DEFAULT_SKIPS_LIMIT = 40

#: `X.Y.Z`, optionally pre-release/build-suffixed (registries accept both;
#: they are not "a different version shape", they are a different point in the
#: same lane).
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

# The proxy-aware opener is built once per process: netproxy.detect() probes the
# host (env vars, loopback listeners, gateway, DNS guesses, WPAD), which is the
# right cost for one run and the wrong cost for one HTTP call per lane.
_OPENER: urllib.request.OpenerDirector | None = None

class verify_error(Exception):
    """A source that cannot be judged: not a repo, registry down, bad answer.

    Deliberately not a finding: a finding is a fact the gate can prove about
    the tree, an error is the absence of a judgement. `main()` maps this to
    exit 2 (fail closed) so "could not check" can never read as "clean".
    """

# ── Versions ─────────────────────────────────────────────────────────────────

def parse_version(text: str) -> Tuple[int, int, int, int] | None:
    """`(major, minor, patch, rank)` for a version string, or `None`.

    `rank` is 0 for a pre-release and 1 for the release itself, so plain
    tuple comparison orders `1.0.0-rc.1` before `1.0.0`. Two different
    pre-releases of one patch compare equal here — they are distinct strings
    and the tag/registry comparison matches by string, so only the ordering is
    coarse.
    """
    match = _VERSION_RE.match(text.strip())

    if match is None:
        return None

    rank = 0 if match.group(4) else 1
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), rank)

def version_sort_key(text: str) -> Tuple[int, int, int, int]:
    """`parse_version` as a sort key, with unparseable text sorting last.

    Every list this module sorts has already been filtered by
    :func:`parse_version`; the sentinel exists so a future caller cannot
    turn one odd registry string into a `TypeError` mid-report.
    """
    key = parse_version(text)

    if key is None:
        return (sys.maxsize, sys.maxsize, sys.maxsize, sys.maxsize)

    return key

def latest_version(versions: Sequence[str]) -> str | None:
    """The highest version in *versions* (`None` when the lane is empty)."""
    best: str | None = None

    for version in versions:
        if (best is None) or (version_sort_key(version) > version_sort_key(best)):
            best = version

    return best

def skipped_patches(versions: Sequence[str], limit: int = DEFAULT_SKIPS_LIMIT) -> Tuple[List[int], int]:
    """Patch numbers missing inside the NEWEST published minor.

    Only the newest minor is judged: older lines are closed, and their gaps are
    history the release notes already describe rather than a slot a release can
    still land on. The range starts at the lowest patch that minor published —
    a line may legitimately open at `X.Y.1` — and ends at its highest, so
    the answer is the set of numbers the line skipped *between* its own
    releases. The returned pair is (first `limit` gaps, exact total): the
    display is capped, the count is not.
    """
    by_minor: Dict[Tuple[int, int], set] = {}

    for raw in versions:
        key = parse_version(raw)

        if key is None:
            continue

        by_minor.setdefault(key[:2], set()).add(key[2])

    if not by_minor:
        return [], 0

    newest = max(by_minor)
    patches = sorted(by_minor[newest])
    present = set(patches)
    low, high = patches[0], patches[-1]
    # The total is arithmetic: materializing `range(low, high + 1)` first would
    # let one hostile registry version (0.55.1000000000) allocate the whole gap
    # before --skips-limit could cap the display. The list below stops after
    # `limit` gaps, so the work is bounded by the published count plus the cap.
    total = (high - low + 1) - len(present)
    cap = max(0, limit)
    shown: List[int] = []
    cursor = low

    for value in patches:
        while (cursor < value) and (len(shown) < cap):
            shown.append(cursor)
            cursor += 1

        if len(shown) >= cap:
            break

        cursor = value + 1

    if len(shown) < cap:
        while (cursor <= high) and (len(shown) < cap):
            shown.append(cursor)
            cursor += 1

    return shown, total

# ── The lane ─────────────────────────────────────────────────────────────────

@dataclass
class lane:
    """One registry lane (an npm package or a crate) and its reconciliation.

    `published` and `tags` hold only the versions at or above the floor;
    the ignored counts keep the pre-publish history visible without judging
    it. `skipped` is the capped display list and `skipped_total` the exact
    count, so a reader can tell "no gaps" from "gaps beyond the cap".
    """

    kind: str
    name: str
    tag_prefix: str
    floor: str | None = None
    published: List[str] = field(default_factory = list)
    tags: List[str] = field(default_factory = list)
    published_untagged: List[str] = field(default_factory = list)
    tagged_unpublished: List[str] = field(default_factory = list)
    skipped: List[int] = field(default_factory = list)
    skipped_total: int = 0
    ignored_published: int = 0
    ignored_tags: int = 0
    unparsed_tags: int = 0
    latest_published: str | None = None
    latest_tag: str | None = None

    @property
    def has_drift(self) -> bool:
        """True when the tag set and the published set disagree in either direction."""
        return bool(self.published_untagged or self.tagged_unpublished)

    def to_dict(self) -> dict:
        """The frozen machine shape of one lane (flat keys, stable names)."""
        return {"kind": self.kind, "name": self.name, "tag_prefix": self.tag_prefix, "floor": self.floor, "published_count": len(self.published), "tagged_count": len(self.tags), "published_untagged": list(self.published_untagged), "tagged_unpublished": list(self.tagged_unpublished), "skipped": list(self.skipped), "skipped_total": self.skipped_total, "ignored_published": self.ignored_published, "ignored_tags": self.ignored_tags, "unparsed_tags": self.unparsed_tags, "latest_published": self.latest_published, "latest_tag": self.latest_tag, "drift": self.has_drift}

def lane_versions(repo_tags: Sequence[str], prefix: str) -> Tuple[List[str], int]:
    """Versions carried by *repo_tags* under *prefix*, plus the unparseable count.

    The prefix is the lane's whole identity: `v` and `cargo-v` tags live
    in the same repository and must never be compared against each other, and a
    tag that carries the prefix but no version (`v` alone, a hand-made
    `v-latest`) is counted rather than silently dropped: it is a release-shaped
    tag the audit cannot place.
    """
    versions: List[str] = []
    unparsed = 0

    for tag in repo_tags:
        if not tag.startswith(prefix):
            continue

        text = tag[len(prefix):]

        if parse_version(text) is None:
            unparsed += 1
            continue

        versions.append(text)

    return versions, unparsed

def _parseable(versions: Sequence[str]) -> List[str]:
    """Only the strings that name a version (dist-tags such as `latest` are not)."""
    return [version for version in versions if parse_version(version) is not None]

def _split_floor(versions: Sequence[str], floor_key: Tuple[int, int, int, int] | None) -> Tuple[List[str], int]:
    """`(versions at or above the floor, count below it)`; no floor keeps all."""

    if floor_key is None:
        return list(versions), 0

    kept: List[str] = []
    ignored = 0

    for version in versions:
        key = parse_version(version)

        if key is None:
            continue

        if key < floor_key:
            ignored += 1
            continue

        kept.append(version)

    return kept, ignored

def reconcile_lane(kind: str, name: str, tag_prefix: str, published: Sequence[str], repo_tags: Sequence[str], floor: str | None = None, skips_limit: int = DEFAULT_SKIPS_LIMIT) -> lane:
    """Compare one lane's published versions with the tags that carry them.

    Pure: no git, no network, no rendering — the selftest and the tests drive
    this directly, and that is the whole reason the comparison is a function
    and not a step inside `main()`. `floor` drops versions and tags below it
    into the ignored counters; `skips_limit` caps only the reported list,
    never the count. An unparseable floor is a programming error here (the CLI
    validates the flag first) and raises `ValueError` rather than judging with
    a floor nobody asked for.
    """
    floor_key = None

    if floor is not None:
        floor_key = parse_version(floor)

        if floor_key is None:
            raise ValueError(f"floor is not an X.Y.Z version: {floor!r}")

    kept_published, ignored_published = _split_floor(_parseable(published), floor_key)
    tagged_versions, unparsed_tags = lane_versions(repo_tags, tag_prefix)
    kept_tags, ignored_tags = _split_floor(tagged_versions, floor_key)
    published_set = set(kept_published)
    tagged_set = set(kept_tags)
    published_untagged = sorted([version for version in kept_published if version not in tagged_set], key = version_sort_key)
    tagged_unpublished = sorted([version for version in kept_tags if version not in published_set], key = version_sort_key)
    shown, skipped_total = skipped_patches(kept_published, skips_limit)
    return lane(kind = kind, name = name, tag_prefix = tag_prefix, floor = floor, published = sorted(kept_published, key = version_sort_key), tags = sorted(kept_tags, key = version_sort_key), published_untagged = published_untagged, tagged_unpublished = tagged_unpublished, skipped = shown, skipped_total = skipped_total, ignored_published = ignored_published, ignored_tags = ignored_tags, unparsed_tags = unparsed_tags, latest_published = latest_version(kept_published), latest_tag = latest_version(kept_tags))

# ── The run ──────────────────────────────────────────────────────────────────

@dataclass
class audit_report:
    """One audit run: the lanes that were judged, plus the sources that failed.

    `ok` is "judged and clean", never "nothing failed to load": an error makes
    the run unverified, and an unverified run is not a pass (fail closed).
    """

    repo: str
    floor: str | None
    skips_limit: int
    lanes: List[lane] = field(default_factory = list)
    errors: List[str] = field(default_factory = list)

    @property
    def has_drift(self) -> bool:
        """True when any judged lane disagrees with its registry."""
        return any(item.has_drift for item in self.lanes)

    @property
    def ok(self) -> bool:
        """Clean means "every source was judged and every lane agreed"."""
        return (not self.errors) and (not self.has_drift)

    def to_dict(self) -> dict:
        """The frozen machine shape of one run (`--json` prints exactly this)."""
        return {"ok": self.ok, "repo": self.repo, "floor": self.floor, "skips_limit": self.skips_limit, "lanes": [item.to_dict() for item in self.lanes], "errors": list(self.errors)}

# ── Git ──────────────────────────────────────────────────────────────────────

def git_tags(repo: Path) -> List[str]:
    """Every tag in *repo* via `git tag --list`; unreadable is an error.

    `-C` rather than a working directory keeps the audit pointed at the tree
    the caller named even when the process runs elsewhere, and the explicit
    `--list` avoids any locale-dependent decoration.
    """

    if not repo.is_dir():
        raise verify_error(f"not a directory: {repo}")

    try:
        probe = subprocess.run(["git", "-C", str(repo), "tag", "--list"], capture_output = True, text = True, encoding = "utf-8", errors = "replace", check = False)
    except OSError as exc:
        raise verify_error(f"git is not runnable: {exc}") from exc

    if probe.returncode != 0:
        detail = [line for line in (probe.stderr or probe.stdout).splitlines() if line.strip()]
        reason = detail[0].strip() if detail else f"exit {probe.returncode}"
        raise verify_error(f"not a git repository: {repo} ({reason})")

    return [line.strip() for line in probe.stdout.splitlines() if line.strip()]

# ── Registries ───────────────────────────────────────────────────────────────

def proxy_opener() -> urllib.request.OpenerDirector:
    """The process-wide opener: netproxy's cascade, then plain urllib.

    A direct result installs no `ProxyHandler` beyond urllib's default, so an
    environment with no proxy cannot route registry traffic through a phantom
    one. Detection runs once (see `_OPENER`) because the cascade probes the
    host and a per-lane re-probe would multiply that cost by the lane count.
    """
    global _OPENER

    if _OPENER is None:
        handlers: List[urllib.request.BaseHandler] = []
        config = netproxy.detect()

        if not config.direct:
            handlers.append(urllib.request.ProxyHandler({"http": config.proxy_url, "https": config.proxy_url}))

        _OPENER = urllib.request.build_opener(*handlers)

    return _OPENER

def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
    """GET one JSON document; every transport or shape failure is a `verify_error`.

    The error text names the URL so a "cannot verify" exit tells the reader
    which side of the audit went dark instead of only that something did.
    """
    request = urllib.request.Request(url, headers = {"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        with proxy_opener().open(request, timeout = timeout) as response:
            body = response.read().decode("utf-8", errors = "replace")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise verify_error(f"{url}: {type(exc).__name__}: {exc}") from exc

    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise verify_error(f"{url}: registry answered with something that is not JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise verify_error(f"{url}: registry answered with a {type(document).__name__}, not a JSON object")

    return document

def npm_versions(document: dict) -> List[str]:
    """The published version keys of an npm packument."""
    versions = document.get("versions")

    if not isinstance(versions, dict) or not versions:
        raise verify_error("the npm packument carries no versions object")

    return [str(key) for key in versions]

def crates_versions(document: dict) -> List[str]:
    """The published version numbers of a crates.io crate record.

    Yanked entries are kept: a yank withdraws an artifact from resolution, it
    does not un-publish the tag or rewrite the lane, so the comparison still
    owes that version an answer.
    """
    entries = document.get("versions")

    if not isinstance(entries, list) or not entries:
        raise verify_error("the crates.io record carries no versions array")

    numbers = [str(entry["num"]) for entry in entries if isinstance(entry, dict) and isinstance(entry.get("num"), str)]

    if not numbers:
        raise verify_error("the crates.io record lists no version numbers")

    return numbers

def registry_url(kind: str, name: str) -> str:
    """The metadata endpoint for one lane (scoped npm names escaped whole)."""

    if kind == "npm":
        return NPM_REGISTRY + urllib.parse.quote(name, safe = "")

    return CRATES_REGISTRY + urllib.parse.quote(name, safe = "")

def run_audit(repo: Path, npm_packages: Sequence[str] = (), crates: Sequence[str] = (), tag_prefix: str = "v", crate_tag_prefix: str = "cargo-v", ignore_below: str | None = None, skips_limit: int = DEFAULT_SKIPS_LIMIT, fetch: Callable[[str], dict] | None = None, listed_tags: Sequence[str] | None = None) -> audit_report:
    """Judge every requested lane; a lane that cannot be read becomes an error.

    `fetch` and `listed_tags` are injection points, not options: they let
    the tests and the selftest drive a whole run offline, which is the only way
    the comparison can be proven on a push gate with no registry reachable. A
    failed fetch skips that lane's `lane` record entirely (there is nothing to
    reconcile) and records why; it never fabricates an empty published set,
    which would read as "the registry serves nothing" and turn every tag into
    drift.
    """
    result = audit_report(repo = str(repo), floor = ignore_below, skips_limit = skips_limit)

    if (ignore_below is not None) and (parse_version(ignore_below) is None):
        result.errors.append(f"--ignore-below is not an X.Y.Z version: {ignore_below!r}")
        return result

    try:
        repo_tags = list(listed_tags) if listed_tags is not None else git_tags(repo)
    except verify_error as exc:
        result.errors.append(str(exc))
        return result

    fetcher = fetch if fetch is not None else fetch_json

    def judge(kind: str, name: str, prefix: str, published: Sequence[str]) -> None:
        """Reconcile one lane; a floor that leaves nothing visible is an error.

        A lane whose every published version fell below `--ignore-below` judged
        nothing, and printing it as clean is exactly the false green this gate
        exists to prevent (a typo like `--ignore-below 0.550.0` would pass).
        """
        item = reconcile_lane(kind = kind, name = name, tag_prefix = prefix, published = published, repo_tags = repo_tags, floor = ignore_below, skips_limit = skips_limit)

        if not item.published:
            reason = f"--ignore-below {ignore_below} hides every published version" if (ignore_below is not None) else "the registry answer carries no published version"
            result.errors.append(f"{kind} {name}: {reason}; nothing was judged")
            return

        result.lanes.append(item)

    for package in npm_packages:
        try:
            document = fetcher(registry_url("npm", package))
            published = npm_versions(document)
        except verify_error as exc:
            result.errors.append(f"npm {package}: {exc}")
            continue

        judge("npm", package, tag_prefix, published)

    for crate in crates:
        try:
            document = fetcher(registry_url("crates", crate))
            published = crates_versions(document)
        except verify_error as exc:
            result.errors.append(f"crates {crate}: {exc}")
            continue

        judge("crates", crate, crate_tag_prefix, published)

    return result

# ── Rendering ────────────────────────────────────────────────────────────────

def render_text(result: audit_report) -> str:
    """Human report: one line per lane, indented findings under it, summary last.

    Every list is printed in full for drift (an untagged publish is a concrete
    version the reader has to act on) and capped for skips (the rule is the
    same for all of them: leave the number burned). No colour: the output is
    read from CI logs as often as from a terminal.
    """
    lines: List[str] = [f"Release audit: {result.repo}", ""]

    for item in result.lanes:
        scope = f"floor {item.floor}" if item.floor else "no floor"
        lines.append(f"{item.kind} {item.name}  (tag prefix {item.tag_prefix!r}, {scope})")
        lines.append(f"  published {len(item.published)}, tagged {len(item.tags)} (latest published {item.latest_published or 'none'}, latest tag {item.latest_tag or 'none'})")

        if item.published_untagged:
            lines.append(f"  drift: {len(item.published_untagged)} published version(s) without a tag: {', '.join(item.published_untagged)}")

        if item.tagged_unpublished:
            lines.append(f"  drift: {len(item.tagged_unpublished)} tag(s) without a published version: {', '.join(item.tagged_unpublished)}")

        if item.skipped_total:
            shown = ", ".join(str(number) for number in item.skipped) or "(none shown)"
            hidden = f" (+{item.skipped_total - len(item.skipped)} more)" if item.skipped_total > len(item.skipped) else ""
            lines.append(f"  warn: {item.skipped_total} skipped patch number(s) inside the newest published minor, never back-fill: {shown}{hidden}")

        if item.unparsed_tags:
            lines.append(f"  warn: {item.unparsed_tags} tag(s) do not parse as <prefix>X.Y.Z and were not judged")

        if (item.ignored_published or item.ignored_tags) and item.floor:
            lines.append(f"  ignored: {item.ignored_published} published version(s) and {item.ignored_tags} tag(s) below the floor {item.floor}")

        if not item.has_drift:
            lines.append("  no drift")

        lines.append("")

    for message in result.errors:
        lines.append(f"  error: {message}")

    if result.errors:
        lines.append("")
        lines.append(f"release-audit: cannot verify — {len(result.errors)} source(s) unreadable, so no lane is reported clean (fail closed)")
        return "\n".join(lines)

    drift = sum(len(item.published_untagged) + len(item.tagged_unpublished) for item in result.lanes)
    skipped = sum(item.skipped_total for item in result.lanes)

    if drift:
        lines.append(f"release-audit: {drift} drift finding(s) across {len(result.lanes)} lane(s); {skipped} skipped patch number(s) warned, never back-filled")
    else:
        lines.append(f"release-audit: no drift across {len(result.lanes)} lane(s); {skipped} skipped patch number(s) warned, never back-filled")

    return "\n".join(lines)

# ── Selftest ─────────────────────────────────────────────────────────────────

def selftest() -> int:
    """Offline proof of the comparison: fixed tags/published sets, fixed answers.

    The command needs a git checkout and a reachable registry; the lint gate
    that runs on every push can have neither. These controls pin every branch
    of the reconciliation — both drift directions, the floor, the newest-minor
    skip rule, the display cap, the lane prefixes, numeric ordering — plus the
    injected whole-run path and the fail-closed error path, so the logic stays
    provable without the network it exists to audit.
    """
    failures: List[str] = []
    checks = [0]

    def check(name: str, got: object, want: object) -> None:
        checks[0] += 1

        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    clean = reconcile_lane("npm", "pkg", "v", ["0.5.0", "0.5.1"], ["v0.5.0", "v0.5.1"])
    check("clean: no drift", clean.has_drift, False)
    check("clean: published kept", clean.published, ["0.5.0", "0.5.1"])
    check("clean: tagged kept", clean.tags, ["0.5.0", "0.5.1"])
    check("clean: latest published", clean.latest_published, "0.5.1")
    check("clean: latest tag", clean.latest_tag, "0.5.1")
    untagged = reconcile_lane("npm", "pkg", "v", ["0.5.0", "0.5.1", "0.5.2"], ["v0.5.0", "v0.5.2"])
    check("published-untagged: listed", untagged.published_untagged, ["0.5.1"])
    check("published-untagged: no reverse drift", untagged.tagged_unpublished, [])
    check("published-untagged: has drift", untagged.has_drift, True)
    unpushed = reconcile_lane("npm", "pkg", "v", ["0.5.0"], ["v0.5.0", "v0.5.9"])
    check("tagged-unpublished: listed", unpushed.tagged_unpublished, ["0.5.9"])
    check("tagged-unpublished: has drift", unpushed.has_drift, True)
    floored = reconcile_lane("npm", "pkg", "v", ["0.4.4", "0.4.5", "0.30.0", "0.31.0"], ["v0.1.0", "v0.4.4", "v0.30.0"], floor = "0.30.0")
    check("floor: published ignored", floored.ignored_published, 2)
    check("floor: tags ignored", floored.ignored_tags, 2)
    check("floor: drift above the floor only", floored.published_untagged, ["0.31.0"])
    check("floor: no reverse drift", floored.tagged_unpublished, [])
    gappy = reconcile_lane("npm", "pkg", "v", ["0.4.0", "0.4.1", "0.4.3", "0.5.0", "0.5.2"], ["v0.5.0", "v0.5.2"])
    check("skips: newest minor only", gappy.skipped, [1])
    check("skips: total counts all", gappy.skipped_total, 1)
    capped = reconcile_lane("npm", "pkg", "v", ["0.6.0", "0.6.2", "0.6.3", "0.6.5"], [], skips_limit = 1)
    check("skips: display capped", capped.skipped, [1])
    check("skips: total stays exact", capped.skipped_total, 2)
    numeric = reconcile_lane("npm", "pkg", "v", ["0.9.0", "0.9.3", "0.10.0", "0.10.2"], [])
    check("ordering: 0.10 is newer than 0.9", numeric.skipped, [1])
    check("ordering: latest is numeric", numeric.latest_published, "0.10.2")
    prerelease = reconcile_lane("npm", "pkg", "v", ["1.0.0-rc.1", "1.0.0"], ["v1.0.0"])
    check("ordering: prerelease sorts below its release", prerelease.latest_published, "1.0.0")
    check("ordering: build metadata does not rank", version_sort_key("1.0.0+build.5"), version_sort_key("1.0.0"))
    big_gap = reconcile_lane("npm", "pkg", "v", ["0.55.0", "0.55.1000000"], [], skips_limit = 2)
    check("skips: hostile gap display capped", big_gap.skipped, [1, 2])
    check("skips: hostile gap total arithmetic", big_gap.skipped_total, 999999)
    separated = reconcile_lane("npm", "pkg", "v", ["0.5.0"], ["v0.5.0", "cargo-v0.6.0"])
    check("prefix: foreign lane tag ignored", separated.tags, ["0.5.0"])
    check("prefix: foreign lane tag not drift", separated.tagged_unpublished, [])
    crate_lane = reconcile_lane("crates", "crate-x", "cargo-v", ["0.6.0"], ["v0.5.0", "cargo-v0.6.0"])
    check("prefix: crate lane sees its own tags", crate_lane.tags, ["0.6.0"])
    check("prefix: crate lane clean", crate_lane.has_drift, False)
    tagged_noise = reconcile_lane("npm", "pkg", "v", ["0.5.0"], ["v0.5.0", "v-latest"])
    check("prefix: unparseable tag counted", tagged_noise.unparsed_tags, 1)

    def fake_registry(url: str) -> dict:
        if url.startswith(NPM_REGISTRY):
            return {"versions": {"1.0.0": {}, "1.0.1": {}}}

        return {"versions": [{"num": "2.0.0"}]}

    def broken_registry(url: str) -> dict:
        raise verify_error(f"{url}: simulated outage")

    both = run_audit(Path("."), npm_packages = ["@scope/pkg"], crates = ["crate-x"], listed_tags = ["v1.0.0", "cargo-v2.0.0"], fetch = fake_registry)
    check("run: two lanes judged", [item.kind for item in both.lanes], ["npm", "crates"])
    check("run: npm drift", both.lanes[0].published_untagged, ["1.0.1"])
    check("run: crates clean", both.lanes[1].has_drift, False)
    check("run: not ok with drift", both.ok, False)
    check("run: no errors", both.errors, [])
    payload = both.to_dict()
    check("json: run keys", sorted(payload), sorted(["ok", "repo", "floor", "skips_limit", "lanes", "errors"]))
    check("json: lane keys", sorted(payload["lanes"][0]), sorted(["kind", "name", "tag_prefix", "floor", "published_count", "tagged_count", "published_untagged", "tagged_unpublished", "skipped", "skipped_total", "ignored_published", "ignored_tags", "unparsed_tags", "latest_published", "latest_tag", "drift"]))
    broken = run_audit(Path("."), npm_packages = ["@scope/pkg"], listed_tags = [], fetch = broken_registry)
    check("broken registry: fail closed", broken.ok, False)
    check("broken registry: error recorded", len(broken.errors), 1)
    missing = run_audit(Path("selftest-no-such-repo-8f4c1d") / "nested", npm_packages = ["@scope/pkg"])
    check("unreadable repo: fail closed", missing.ok, False)
    check("unreadable repo: error recorded", len(missing.errors), 1)
    hidden = run_audit(Path("."), npm_packages = ["@scope/pkg"], ignore_below = "99.0.0", listed_tags = ["v99.1.0"], fetch = fake_registry)
    check("floor: hiding every version is an error", len(hidden.errors), 1)
    check("floor: hiding every version is not ok", hidden.ok, False)
    check("floor: hiding every version judges no lane", hidden.lanes, [])

    if failures:
        for message in failures:
            print(f"SELFTEST FAIL: {message}", file = sys.stderr)

        return 1

    print(f"selftest OK: {checks[0]} control(s) over both drift directions, the floor, the newest-minor skip rule, the display cap, numeric and prerelease ordering, the lane prefixes and the injected whole-run path")
    return 0

# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: 0 clean, 1 drift, 2 cannot verify or usage error."""
    parser = argparse.ArgumentParser(prog = "celestia-devtools release-audit", description = "Reconcile a repository's release tags against its npm/crates registries.")
    parser.add_argument("--repo", default = ".", metavar = "PATH", help = "git checkout to read tags from (default: the current directory)")
    parser.add_argument("--npm", action = "append", default = [], metavar = "PACKAGE", help = "npm package to audit (repeatable, scoped names allowed)")
    parser.add_argument("--crates", action = "append", default = [], metavar = "CRATE", help = "crate to audit (repeatable)")
    parser.add_argument("--tag-prefix", default = "v", metavar = "PREFIX", help = "npm release tag prefix (default: v)")
    parser.add_argument("--crate-tag-prefix", default = "cargo-v", metavar = "PREFIX", help = "crate release tag prefix (default: cargo-v)")
    parser.add_argument("--ignore-below", default = None, metavar = "X.Y.Z", help = "treat versions and tags below this as pre-publish history (per lane)")
    parser.add_argument("--skips-limit", type = int, default = DEFAULT_SKIPS_LIMIT, metavar = "N", help = f"cap the printed skipped-patch list (default: {DEFAULT_SKIPS_LIMIT}; the count stays exact)")
    parser.add_argument("--json", action = "store_true", help = "emit the machine-readable report instead of the lane table")
    parser.add_argument("--selftest", action = "store_true", help = "prove the comparison against offline fixtures and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if (not args.npm) and (not args.crates):
        print("release-audit: nothing to audit: pass --npm PACKAGE and/or --crates CRATE", file = sys.stderr)
        return 2

    if args.skips_limit < 0:
        print(f"release-audit: --skips-limit must be >= 0, got {args.skips_limit}", file = sys.stderr)
        return 2

    result = run_audit(Path(args.repo), npm_packages = args.npm, crates = args.crates, tag_prefix = args.tag_prefix, crate_tag_prefix = args.crate_tag_prefix, ignore_below = args.ignore_below, skips_limit = args.skips_limit)

    if args.json:
        envelope.emit(result.to_dict(), sort_keys = True)
    else:
        print(render_text(result))

    if result.errors:
        return 2

    return 1 if result.has_drift else 0

if __name__ == "__main__":
    raise SystemExit(main())
