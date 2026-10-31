#!/usr/bin/env python3
"""Self-hosted dependency radar — detect drift, never propose.

Replaces the retired dependabot (wowsp 2026-11-01 directive): instead of
auto-proposing update PRs, the radar *reports* dependency drift as a rolling
GitHub issue in this repository, and humans/agents port what matter through
normal, governed PRs.

Three probe classes, matching what dependabot covered for the fleet:

* ``actions`` — every pinned third-party GitHub Action in the target repo's
  workflows and composite-action directories, compared against the action's
  latest tag. Reusable-workflow ``uses:`` lines are excluded (they pin this
  repository's own workflows, not an action), sha-pins are reported as
  "compare manually" rows.
* ``cargo`` — registry dependencies declared in Cargo.toml manifests
  (``name = "1.2"`` or ``name = { version = "1.2", ... }``), compared against
  the latest crates.io release. Path and git dependencies are skipped.
* ``npm`` — dependencies / devDependencies from package.json manifests,
  compared against the latest npm registry release. Workspace protocols
  (``workspace:*``, ``link:``, ``file:``, ``github:``) are skipped.

Targets come from ``dependency-radar.yml`` at this repository's root — the
upgrade point: add a repo entry and the next run covers it. GitHub data goes
through the ``gh`` CLI; the crates.io and npm registries are fetched with
urllib. Registry egress is guarded: https only, the two registry hosts are a
fixed allowlist, resolved addresses are checked against private/loopback/
link-local/reserved ranges (IPv4-mapped IPv6 unwrapped first), and redirects
are refused — the registries answer 200 directly, anything else is an error
signal. Probe failures degrade to report notes instead of aborting the scan.

Exit codes:
    0  — the scan completed and the report was produced (drift is NOT an error)
    1  — the report write or issue upsert failed
    2  — usage, configuration or environment error (missing gh, bad targets)

Usage::

    python -m celestia_devtools.radar.radar --config dependency-radar.yml --stdout
    python -m celestia_devtools.radar.radar --self-test            # offline fixtures

    Fully local since 2026-10-31 (user ruling): the workflow and the rolling
    GitHub issue are GONE. --stdout is the only mode an operator needs; the
    report lands in the workspace ledger where agents read it.
"""

from __future__ import annotations
import argparse
import datetime
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
import yaml
from celestia_devtools.core import logger

EXIT_OK = 0
EXIT_REPORT = 1
EXIT_USAGE = 2

ISSUE_TITLE = "Dependency radar"
VERSION_PARTS = re.compile(r"^\D*?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
ACTION_USES = re.compile(r"uses:\s*(\S+)@(\S+)")
CARGO_DEP = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+)$")
SKIP_NPM_SPEC = re.compile(r"^(workspace:|link:|file:|github:|git\+)")
REGISTRY_HOSTS = frozenset({"crates.io", "registry.npmjs.org"})
WORKFLOW_DIRS = (".github/workflows", ".github/actions")

# ── plumbing ─────────────────────────────────────────────────────────────────

def eprint(*args: object) -> None:
    print(*args, file = sys.stderr)

def gh_run(*args: str) -> str:
    """Run one ``gh`` invocation and return stdout; LookupError on failure."""
    gh = shutil.which("gh")

    if gh is None:
        eprint("error: gh CLI not found on PATH — install https://cli.github.com")
        raise SystemExit(EXIT_USAGE)

    proc = subprocess.run(["gh", *args], capture_output = True, text = True, encoding = "utf-8", errors = "replace")

    if proc.returncode != 0:
        eprint(f"warning: gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
        raise LookupError(args[0] if args else "gh")

    return proc.stdout

def gh_api(endpoint: str, *extra: str) -> str:
    return gh_run("api", endpoint, *extra)

def _guard_registry_url(url: str) -> str:
    """https + fixed-host allowlist + public-address resolution check."""
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme != "https":
        eprint(f"error: refusing non-https registry URL {url!r}")
        raise SystemExit(EXIT_USAGE)

    host = parsed.hostname or ""

    if host not in REGISTRY_HOSTS:
        eprint(f"error: refusing non-registry host {host!r}")
        raise SystemExit(EXIT_USAGE)

    for info in socket.getaddrinfo(host, 443, proto = socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])

        if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None):
            ip = ip.ipv4_mapped

        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            eprint(f"error: {host} resolves to a non-public address ({ip}) — refusing")
            raise SystemExit(EXIT_USAGE)

    return url

class _refuse_redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise LookupError(f"registry redirected to {newurl!r} — refused")

def http_json(url: str) -> object:
    """Fetch one JSON document from an allowlisted registry (SSRF-guarded)."""
    request = urllib.request.Request(_guard_registry_url(url), headers = {"User-Agent": "celestia-devtools-radar"})
    opener = urllib.request.build_opener(_refuse_redirects)

    with opener.open(request, timeout = 30) as response:
        if response.status != 200:
            raise LookupError(f"{url} answered HTTP {response.status}")

        return json.loads(response.read().decode("utf-8"))

def raw_file(repo: str, path: str) -> str | None:
    """One file's raw text from a repo's default branch, None when absent."""

    try:
        return gh_api(f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw")
    except LookupError:
        return None

# ── parsing ──────────────────────────────────────────────────────────────────

def parse_version(spec: str) -> tuple[int, int, int]:
    """Leading numeric triple of a version-ish string; missing parts are 0."""
    match = VERSION_PARTS.match(spec.strip())

    if match is None:
        return (0, 0, 0)

    return (int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0))

def is_newer(latest: str, declared: str) -> bool:
    """True when ``latest`` is strictly greater than ``declared`` (semver-ish)."""
    return parse_version(latest) > parse_version(declared)

def parse_action_refs(text: str) -> list[tuple[str, str]]:
    """(action, ref) pairs for third-party ``uses:`` pins in one workflow file.

    Local composite actions (``./...``) and reusable-workflow references
    (``owner/repo/.github/workflows/x.yml@ref``) are excluded: the latter pin
    this repository's own workflows, not an action.
    """
    pairs: list[tuple[str, str]] = []

    for match in ACTION_USES.finditer(text):
        target, ref = match.group(1), match.group(2)

        if target.startswith("./") or ref.startswith("./"):
            continue

        if (target.count("/") > 1) and ("/.github/workflows/" in f"{target}@{ref}"):
            continue

        pairs.append((target, ref))

    return pairs

def parse_cargo_manifest(text: str) -> dict[str, str]:
    """Registry dependency specs from a Cargo.toml (workspace + own sections).

    Only single-line ``name = "spec"`` / ``name = { version = "spec", ... }``
    forms are read; path/git/workspace-inherited deps are skipped. A multi-line
    inline table still contributes the ``version = "..."`` written on its first
    line; its continuation lines are consumed until the table closes.
    """
    deps: dict[str, str] = {}
    in_scope = False
    in_continuation = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("["):
            section = stripped.strip("[]").strip()
            in_scope = section in ("workspace.dependencies", "dependencies")
            in_continuation = False
            continue

        if in_continuation:
            in_continuation = "}" not in stripped
            continue

        if not in_scope or ("=" not in stripped) or stripped.startswith("#"):
            continue

        match = CARGO_DEP.match(stripped)

        if match is None:
            continue

        name, spec = match.group(1), match.group(2).strip()
        inline = re.match(r'^\{\s*version\s*=\s*"([^"]+)"', spec)

        if inline is not None:
            version = inline.group(1)
        elif spec.startswith("{"):
            continue
        else:
            version = spec.strip('"').strip()

        if re.match(r"^(path|git|workspace)\s*=", spec):
            continue

        if version == "":
            continue

        if spec.startswith("{") and (not spec.endswith("}")):
            in_continuation = True

        deps[name] = version

    return deps

def parse_package_json(text: str) -> dict[str, str]:
    """dependencies + devDependencies from a package.json; workspace specs skipped."""
    manifest = json.loads(text)
    deps: dict[str, str] = {}

    for section in ("dependencies", "devDependencies"):
        for name, spec in (manifest.get(section) or {}).items():
            if not SKIP_NPM_SPEC.match(spec):
                deps[name] = str(spec)

    return deps

# ── probes ───────────────────────────────────────────────────────────────────

def probe_actions(repo: str, tag_cache: dict[str, str]) -> list[dict[str, str]]:
    """Drift rows for the repo's pinned third-party actions."""
    refs: dict[str, str] = {}

    for directory in WORKFLOW_DIRS:
        try:
            listing = json.loads(gh_api(f"repos/{repo}/contents/{directory}"))
        except LookupError:
            continue

        if not isinstance(listing, list):
            continue

        for entry in listing:
            if not str(entry.get("name", "")).endswith((".yml", ".yaml")):
                continue

            text = raw_file(repo, str(entry.get("path", "")))

            if text is None:
                continue

            for action, ref in parse_action_refs(text):
                refs.setdefault(action, ref)

    rows: list[dict[str, str]] = []

    for action, ref in sorted(refs.items()):
        if action not in tag_cache:
            tag_cache[action] = _latest_action_tag(action)

        latest = tag_cache[action]

        if latest is None:
            rows.append(row("actions", action, ref, "?", "no releases/tags found"))
        elif (len(ref) >= 40) and not ref.startswith("v"):
            rows.append(row("actions", action, ref[:12] + "…", latest, "sha-pinned; compare manually"))
        elif is_newer(latest, ref):
            rows.append(row("actions", action, ref, latest, "drift"))

    return rows

def _latest_action_tag(action: str) -> str | None:
    for endpoint in (f"repos/{action}/releases/latest", f"repos/{action}/tags?per_page=1"):
        try:
            payload = json.loads(gh_api(endpoint))
        except LookupError:
            continue

        if isinstance(payload, dict):
            tag = payload.get("tag_name")

            if tag:
                return str(tag)

        if isinstance(payload, list) and payload:
            tag = payload[0].get("name")

            if tag:
                return str(tag)

    return None

def probe_cargo(repo: str, manifests: list[str], cache: dict[str, str]) -> list[dict[str, str]]:
    """Drift rows for the repo's Cargo.toml registry dependencies."""
    rows: list[dict[str, str]] = []

    for manifest_path in manifests:
        text = raw_file(repo, manifest_path)

        if text is None:
            rows.append(row("cargo", manifest_path, "-", "-", "manifest not found"))
            continue

        for name, spec in sorted(parse_cargo_manifest(text).items()):
            if name not in cache:
                cache[name] = _latest_crate(name)

            latest = cache[name]

            if latest is None:
                rows.append(row("cargo", name, spec, "?", "not on crates.io"))
            elif is_newer(latest, spec):
                rows.append(row("cargo", name, spec, latest, "drift"))

    return rows

def _latest_crate(name: str) -> str | None:
    try:
        payload = http_json(f"https://crates.io/api/v1/crates/{name}")
    except Exception:  # noqa: BLE001 — any registry failure degrades to a note
        return None

    crate = payload.get("crate") if isinstance(payload, dict) else None

    if not isinstance(crate, dict):
        return None

    version = crate.get("max_stable_version") or crate.get("max_version")
    return str(version) if version else None

def probe_npm(repo: str, manifests: list[str], cache: dict[str, str]) -> list[dict[str, str]]:
    """Drift rows for the repo's package.json dependencies."""
    rows: list[dict[str, str]] = []

    for manifest_path in manifests:
        text = raw_file(repo, manifest_path)

        if text is None:
            rows.append(row("npm", manifest_path, "-", "-", "manifest not found"))
            continue

        try:
            deps = parse_package_json(text)
        except json.JSONDecodeError as exc:
            rows.append(row("npm", manifest_path, "-", "-", f"unparseable: {exc}"))
            continue

        for name, spec in sorted(deps.items()):
            if name not in cache:
                cache[name] = _latest_npm(name)

            latest = cache[name]

            if latest is None:
                rows.append(row("npm", name, spec, "?", "not on the npm registry"))
            elif is_newer(latest, spec):
                rows.append(row("npm", name, spec, latest, "drift"))

    return rows

def _latest_npm(name: str) -> str | None:
    try:
        payload = http_json(f"https://registry.npmjs.org/{name}/latest")
    except Exception:  # noqa: BLE001 — any registry failure degrades to a note
        return None

    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version) if version else None

def row(kind: str, name: str, declared: str, latest: str, note: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "declared": declared, "latest": latest, "note": note}

# ── report ───────────────────────────────────────────────────────────────────

def render_report(targets: list[dict], rows_by_repo: dict[str, list[dict[str, str]]], date: str) -> str:
    """The markdown report body; per repo, drift rows and probe notes alike."""
    lines: list[str] = [f"# {ISSUE_TITLE} — {date}", "", "Detected, not proposed: every row below is drift the radar *saw* — act on it", "through normal governed PRs. Upgrade the watch-list in `dependency-radar.yml`.", ""]
    total = sum(len(rows) for rows in rows_by_repo.values())
    lines.append(f"**{len(targets)} repos watched · {total} rows.** Nothing here was opened or merged by the radar.")
    lines.append("")

    for target in targets:
        repo = target["repo"]
        rows = rows_by_repo.get(repo, [])
        lines.append(f"## {repo}")
        lines.append("")

        if not rows:
            lines.append("_no drift, no notes._")
            lines.append("")
            continue

        lines.append("| kind | name | declared | latest | note |")
        lines.append("|---|---|---|---|---|")

        for row_ in rows:
            lines.append(f"| {row_['kind']} | {row_['name']} | {row_['declared']} | {row_['latest']} | {row_['note']} |")

        lines.append("")

    return "\n".join(lines) + "\n"

def load_targets(path: Path) -> list[dict]:
    payload = yaml.safe_load(path.read_text(encoding = "utf-8"))

    if not isinstance(payload, dict) or not isinstance(payload.get("repos"), list) or (payload["repos"] == []):
        eprint(f"error: {path} must define a non-empty 'repos' list")
        raise SystemExit(EXIT_USAGE)

    for entry in payload["repos"]:
        if not isinstance(entry, dict) or ("repo" not in entry):
            eprint(f"error: {path}: every entry needs a 'repo' field")
            raise SystemExit(EXIT_USAGE)

    return payload["repos"]

def scan(targets: list[dict]) -> dict[str, list[dict[str, str]]]:
    """Run every enabled probe class over every target; failures degrade to notes."""
    actions_cache: dict[str, str] = {}
    crates_cache: dict[str, str] = {}
    npm_cache: dict[str, str] = {}
    rows_by_repo: dict[str, list[dict[str, str]]] = {}

    for target in targets:
        repo = str(target["repo"])
        logger.info(f"radar: scanning {repo}")
        rows: list[dict[str, str]] = []

        if target.get("actions", False):
            rows += probe_actions(repo, actions_cache)

        if target.get("cargo", False):
            rows += probe_cargo(repo, list(target.get("cargo_manifests", ["Cargo.toml"])), crates_cache)

        if target.get("npm", False):
            rows += probe_npm(repo, list(target.get("npm_manifests", ["package.json"])), npm_cache)

        rows_by_repo[repo] = rows

    return rows_by_repo

def self_test() -> int:
    fixtures = [("1.2.3", "1.2.3", False), ("0.43.0", "0.46.0", True), ("^1.2", "1.9.0", True), ("~2.0", "2.1", True), ("v4", "v7", True), ("v7", "v7", False), ("15.9", "15.10", True), ("garbage", "1.0.0", True)]

    for declared, latest, want in fixtures:
        got = is_newer(latest, declared)

        if got != want:
            print(f"self-test FAILED: is_newer({latest!r}, {declared!r}) = {got}, want {want}", file = sys.stderr)
            return 1

    actions = parse_action_refs("uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n" "uses: ./local/action@main\n" "uses: celestia-island/celestia-devtools/.github/workflows/ci.yml@abc\n" "uses: actions/upload-artifact@v4\n")

    if [a for a, _ in actions] != ["actions/checkout", "actions/upload-artifact"]:
        print(f"self-test FAILED: action refs parsed as {actions}", file = sys.stderr)
        return 1

    cargo = parse_cargo_manifest('[workspace.dependencies]\nanyhow = "1"\n' 'game-assets = { version = "^0.43", path = "packages/tools/game-assets" }\n' 'winnow = { version = "0.7", features = ["simd"] }\n' 'vendored = { path = "../vendored" }\n' '[dependencies]\nserde = "1.0"\n')

    if cargo != {"anyhow": "1", "game-assets": "^0.43", "winnow": "0.7", "serde": "1.0"}:
        print(f"self-test FAILED: cargo parse gave {cargo}", file = sys.stderr)
        return 1

    npm = parse_package_json('{"dependencies": {"vue": "^3.5.0"}, "devDependencies": {"vite": "^8", "linked": "workspace:*"}}')

    if npm != {"vue": "^3.5.0", "vite": "^8"}:
        print(f"self-test FAILED: npm parse gave {npm}", file = sys.stderr)
        return 1

    report = render_report([{"repo": "r/x"}], {"r/x": [row("cargo", "anyhow", "1", "1.1", "drift")]}, "2026-11-01")

    if ("| cargo | anyhow |" not in report) or ("1 repos watched · 1 rows." not in report):
        print("self-test FAILED: report render", file = sys.stderr)
        return 1

    print("self-test: 12 fixtures passed")
    return 0

def main(argv: Sequence[str] | None = None) -> int:
    # Keep emoji-bearing output intact on Windows consoles (cp936 etc.).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding = "utf-8", errors = "replace")
        sys.stderr.reconfigure(encoding = "utf-8", errors = "replace")

    parser = argparse.ArgumentParser(description = __doc__.splitlines()[0])
    parser.add_argument("--config", default = "dependency-radar.yml", help = "targets manifest path")
    parser.add_argument("--self-test", action = "store_true", help = "offline fixtures, no network")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    targets = load_targets(Path(args.config))
    rows_by_repo = scan(targets)
    today = datetime.date.today().isoformat()
    report = render_report(targets, rows_by_repo, today)

    print(report)
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
