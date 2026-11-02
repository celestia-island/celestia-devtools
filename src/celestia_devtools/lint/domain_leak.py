#!/usr/bin/env python3
# AI-generated with DeepSeek Harness (deepseek-flash); reviewed by a human maintainer.
"""domain-leak gate - host-core repositories must not name industry vocabulary.

*Why this exists:* the workspace strategy is "generic host + vertical instances"
(workspace roadmap). Degrading into a bespoke system happens incrementally:
industry vocabulary seeps into the host core until a second domain can no longer
be plugged in. It had already seeped in - a demo plant's register safety table was
hardcoded under ``shared/security_policy``, a plant title reached the TUI core, and
a shift-supervisor persona reached core tests. **A rule without a tool is only a
suggestion** (the same lesson as ``cred_scan``), so the criterion is executable
here.

*Boundary:* this tool covers vocabulary leakage only. Where the domain *data*
belongs (site data goes into the owning service's database or seed channel) is
not its business.

*Scope:* the host-core repositories in :data:`SCOPE`, whole repository; the
``domain_*`` packages are the legitimate domain zone. Product-layer repositories
are not host core and are not scanned. Hook modes scan *this change*: a changed
file that hits and is not in the baseline is rejected, while touching a file that
is already baselined stays allowed (that is tracked debt, cleared by
neutralization waves).

*Baseline:* ``lint/domain_leak_baseline.json`` ships inside this package and is
resolved as ``--baseline`` -> ``$CELESTIA_DOMAIN_LEAK_BASELINE`` -> the bundled
copy. **Baseline changes are pull requests against this repository** - review is
the gate, and no consumer may edit its own copy and expect that to hold.

*Workspace root:* ``--workspace`` -> ``$CELESTIA_WORKSPACE`` -> the nearest
ancestor of this checkout that looks like a Celestia workspace (``_tools/claim.py``
plus ``_ledger/``). The depth is deliberately *not* hardcoded: measured from this
file, the workspace is ``parents[4]`` in a main checkout
(``<ws>/celestia-devtools/src/celestia_devtools/lint/``) but ``parents[6]`` from a
workspace worktree (``<ws>/_worktree/celestia-devtools/<slug>/src/...``), so a
fixed index would silently scan the wrong tree. When neither a declared root nor a
marker walk succeeds (an installed wheel, an unfamiliar cwd) the checkout's parent is
returned as a last resort flagged as a *fallback*: the full scan then refuses with
``rc 2`` instead of reporting a green scan over zero reachable repositories. A
declared root (``--workspace`` / ``$CELESTIA_WORKSPACE``) keeps the old semantics -
the caller owns the risk of an empty scan.

Usage::

    domain-leak                      # full scan (human / weekly sweep)
    domain-leak --staged             # pre-commit: this repository's staged content
    domain-leak --pushed             # pre-push: ref lines read from stdin
    domain-leak --pushed-range B H   # CI / self-test: the diff of B..H
    domain-leak --quiet              # exit code only
    domain-leak --json               # machine-readable (hooks / CI)
    domain-leak --prune-baseline     # drop fixed baseline entries (fix PRs run it)
    domain-leak --selftest           # known-positive / known-negative self-proof

Exit codes: ``0`` clean or baseline-only; ``1`` a hit outside the baseline (or a
failing self-test); ``2`` configuration, usage or git error - fail closed: an
unusable git checkout, an unidentifiable repository, or a full scan with no declared
root and no workspace markers refuses to pass instead of silently skipping.
"""

from __future__ import annotations
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Sequence

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parents[2]
_PACKAGE_BASELINE = _PACKAGE_DIR / "domain_leak_baseline.json"
BASELINE_PATH_ENV = "CELESTIA_DOMAIN_LEAK_BASELINE"
WORKSPACE_PATH_ENV = "CELESTIA_WORKSPACE"

# ── Reach: host core is each repository **in full** (enumerating prefixes missed
# res/scripts/examples/benches/tests); ``domain_*`` is the legitimate domain zone.
# The empty include prefix matches everything. Product-layer repositories are not
# host core and are not scanned.
SCOPE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {"entelecheia": (("",), ("packages/domain_agents/", "res/prompts/domain_agents/")), "plana": (("",), ("packages/domain_skills/", "res/prompts/domain_skills/")), "kirino": (("",), ()), "hikari": (("",), ()), "evernight": (("",), ()), "malkuth": (("",), ()), "aoba": (("",), ()), "shun": (("",), ())}

# ── Industry vocabulary. CJK matches literally (the single character covers the
# compound words); ASCII matches case-insensitively. Localized forms are included
# because eight languages carried the word verbatim in docs. Add or remove a term
# together with the matching self-test positive, in its own pull request.
TERMS_CJK = ("氢", "氫", "电极", "電極", "电解槽", "電解槽", "电堆", "電堆", "水素", "수소")
# Localized forms, in order: de, es, fr, pt, ru (covers inflected forms), ar. The
# last entry is the product-repo reference: citing a product repository from host
# core is a leakage vector of its own. Bare "hydro" stays unlisted because it is
# too generic (hydrology is not the industry).
TERMS_ASCII_CI = ("hydrogen", "electrode", "comsol", "electrolyzer", "wasserstoff", "hidrógeno", "hydrogène", "hidrogênio", "водород", "الهيدروجين", "easy-hydro")

EXTS = {".rs", ".ts", ".tsx", ".vue", ".toml", ".json", ".yml", ".yaml", ".snap", ".sql", ".md", ".py", ".jsonl", ".txt"}
SKIP_DIRS = {".git", "node_modules", "target", "dist", "__pycache__", ".venv", "snapshots"}

# The origin fallback trusts family organizations only: a local bare clone or a
# same-named repository under another organization must not silently escape or be
# mis-scanned. A new family organization is a change here plus a self-test run.
FAMILY_OWNERS = ("celestia-island", "langyo")

_PATTERNS = [(t, re.compile(re.escape(t)))for t in TERMS_CJK] + [(t, re.compile(re.escape(t), re.IGNORECASE))for t in TERMS_ASCII_CI]

def looks_like_workspace(path: Path) -> bool:
    """True when ``path`` carries the workspace markers this tool scans against.

    ``_tools/claim.py`` plus ``_ledger/`` is the discriminant: a parent directory of
    sibling checkouts (``_worktree``) also holds directories named after repositories,
    so counting checkouts alone would pick the wrong level.
    """
    return (path / "_tools" / "claim.py").is_file() and (path / "_ledger").is_dir()

# How the workspace root was arrived at. The full scan needs this split: a caller
# that declared the root (flag or environment) owns the risk of an empty scan, and a
# marker walk proves the root is a workspace - but the last-resort parent fallback
# proves nothing, and scanning it to report a clean pass is a silent green.
WORKSPACE_SOURCE_FLAG = "flag"
WORKSPACE_SOURCE_ENV = "env"
WORKSPACE_SOURCE_MARKER = "marker"
WORKSPACE_SOURCE_FALLBACK = "fallback"

def resolve_workspace_source(explicit: str | None = None, env: Dict[str, str] | None = None) -> tuple[Path, str]:
    """Resolve the workspace root together with how it was found.

    The devtools checkout normally sits at ``<workspace>/celestia-devtools``, so the
    walking fallback finds the workspace without hardcoding an absolute mount path
    (worktrees and secondary clones resolve the same way). When neither a declaration
    nor a marker walk succeeds - an installed wheel, an unfamiliar cwd - the parent
    directory is returned as a last resort flagged as ``fallback``, so the caller can
    refuse instead of reporting a silent green over zero reachable repositories.
    """
    env = dict(os.environ) if env is None else env

    if explicit:
        return Path(explicit).expanduser().resolve(), WORKSPACE_SOURCE_FLAG

    from_env = env.get(WORKSPACE_PATH_ENV)

    if from_env:
        return Path(from_env).expanduser().resolve(), WORKSPACE_SOURCE_ENV

    for candidate in _REPO_ROOT.parents:
        if looks_like_workspace(candidate):
            return candidate, WORKSPACE_SOURCE_MARKER

    return _REPO_ROOT.parent, WORKSPACE_SOURCE_FALLBACK

def resolve_workspace(explicit: str | None = None, env: Dict[str, str] | None = None) -> Path:
    """Resolve the workspace root only (see :func:`resolve_workspace_source`)."""
    return resolve_workspace_source(explicit, env)[0]

def resolve_baseline(explicit: str | None = None, env: Dict[str, str] | None = None) -> Path:
    """Resolve the baseline file: flag, environment, then the copy bundled here."""
    env = dict(os.environ) if env is None else env

    for candidate in (explicit, env.get(BASELINE_PATH_ENV)):
        if candidate:
            return Path(candidate).expanduser().resolve()

    return _PACKAGE_BASELINE

WORKSPACE, WORKSPACE_SOURCE = resolve_workspace_source()
BASELINE = resolve_baseline()

def find_terms(text: str, extra: str = "") -> list[str]:
    """Match the content plus the optional path: a file NAME carrying a term leaks too."""
    hay = text + ("\n" + extra if extra else "")
    return sorted({t for t, p in _PATTERNS if p.search(hay)})

def scan_repo(repo: str, root: Path) -> list[dict]:
    inc, exc = SCOPE[repo]
    hits: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for name in filenames:
            if Path(name).suffix not in EXTS:
                continue

            p = Path(dirpath) / name
            rel = str(p.relative_to(root))

            if not any(rel.startswith(i) for i in inc):
                continue

            if any(rel.startswith(e) for e in exc):
                continue

            try:
                text = p.read_text(encoding = "utf-8", errors = "replace")
            except OSError:
                continue

            found = find_terms(text, rel)

            if found:
                hits.append({"repo": repo, "path": rel, "terms": found})

    return hits

def load_baseline() -> dict:
    if not BASELINE.exists():
        return {"entries": []}

    try:
        data = json.loads(BASELINE.read_text(encoding = "utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"domain-leak: unreadable baseline ({e}); treating it as empty = every hit is new", file = sys.stderr)
        return {"entries": []}

    if not isinstance(data.get("entries"), list):
        print("domain-leak: baseline has no entries list; treating it as empty", file = sys.stderr)
        return {"entries": []}

    return data

def key(h: dict) -> tuple[str, str]:
    return (h["repo"], h["path"])

# ── Hook mode: block on the diff, not on the post-merge state of the base branch

def _git(root: Path, *args: str, check: bool = True) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output = True, text = True, errors = "replace", timeout = 60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"git call failed ({e})") from e

    if check and (r.returncode != 0):
        raise RuntimeError(f"git {' '.join(args[:2])}… rc={r.returncode}: {r.stderr.strip()[:200]}")

    return r.stdout

def repo_from_toplevel(top: Path) -> str | None:
    """Derive the repo name from the git toplevel: <root>/<repo> or <root>/_worktree/<repo>/<slug>."""
    s = str(top)

    if "/_worktree/" in s:
        part = s.split("/_worktree/")[-1]
        return part.split("/")[0] or None

    if top.parent == WORKSPACE:
        return top.name

    return None

def repo_from_origin(top: Path) -> str | None:
    """Fall back to the origin URL when the path cannot name the repository.

    A local bare-clone origin (/tmp/x/remote.git) or a same-named repository under
    another organization (gitlab.example.com/other/kirino) returns None, so hook
    mode exits 2 (fail-closed) instead of silently passing or mis-scanning.
    """
    url = _git(top, "remote", "get-url", "origin", check = False).strip()

    if not url:
        return None

    clean = url.replace(":", "/").rstrip("/")

    if clean.endswith(".git"):
        clean = clean[:-len(".git")]

    parts = [p for p in clean.split("/") if p]

    if len(parts) < 2:
        return None

    owner, name = parts[-2], parts[-1]

    if (owner not in FAMILY_OWNERS) or not name:
        return None

    return name

def resolve_repo(top: Path) -> str | None:
    """Identify the repository by path, then by origin; None means hook mode refuses (rc 2)."""
    return repo_from_toplevel(top) or repo_from_origin(top)

def _in_scope(repo: str, rel: str) -> bool:
    """Prefix and suffix filter, shared by hook mode and the full scan so they agree."""
    inc, exc = SCOPE[repo]

    if not any(rel.startswith(i) for i in inc):
        return False

    if any(rel.startswith(e) for e in exc):
        return False

    return Path(rel).suffix in EXTS

def scan_diff(repo: str, root: Path, files_terms: list[tuple[str, str]]) -> tuple[int, list[dict]]:
    """``files_terms`` is [(rel, blob_content)]; returns (rc, new hits)."""
    baseline = {key(e) for e in load_baseline()["entries"]}
    new_hits = []

    for rel, text in files_terms:
        if not _in_scope(repo, rel):
            continue

        found = find_terms(text, rel)

        if found and ((repo, rel) not in baseline):
            new_hits.append({"repo": repo, "path": rel, "terms": found})

    return (1 if new_hits else 0), new_hits

def _report_hook_hits(mode: str, new_hits: list[dict], quiet: bool) -> None:
    """Hook mode: a violation ALWAYS reaches stderr - a quiet rejection nobody can read is a trap."""

    for h in new_hits:
        print(f"  {mode} leak  {h['repo']}/{h['path']}  terms={','.join(h['terms'])}", file = sys.stderr)

    if new_hits:
        print("-> host core must not name industry vocabulary: use neutral wording, or move" " the data into a domain_* package or the seed channel. Baseline changes are a" " pull request against celestia-devtools; do not edit a local copy.", file = sys.stderr)

def run_staged(quiet: bool) -> int:
    """pre-commit: scan this repository's staged content (the index version)."""

    try:
        top = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        repo = resolve_repo(top)  # unidentifiable repo = rc 2, never a silent pass

        if repo is None:
            print(f"domain-leak(--staged): cannot identify the repository ({top}); refusing", file = sys.stderr)
            return 2

        if repo not in SCOPE:
            return 0

        out = _git(top, "diff", "--cached", "--name-only", "--diff-filter=ACMR")
        pairs = []

        for rel in (line.strip() for line in out.splitlines() if line.strip()):
            blob = _git(top, "show", f":{rel}", check = False)
            pairs.append((rel, blob))

        rc, new_hits = scan_diff(repo, top, pairs)
    except RuntimeError as e:
        print(f"domain-leak(--staged): {e}; refusing (fail-closed)", file = sys.stderr)
        return 2

    _report_hook_hits("staged", new_hits, quiet)
    return rc

def _diff_pairs(top: Path, base: str, head: str) -> list[tuple[str, str]]:
    out = _git(top, "diff", "--name-only", "--diff-filter=ACMR", base, head)
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return [(rel, _git(top, "show", f"{head}:{rel}", check = False)) for rel in files]

def _tree_pairs(top: Path, head: str) -> list[tuple[str, str]]:
    """Fail-closed retreat when no diff base is available: scan the whole head tree."""
    out = _git(top, "ls-tree", "-r", "--name-only", head)
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return [(rel, _git(top, "show", f"{head}:{rel}", check = False)) for rel in files]

def run_pushed_range(base: str, head: str, quiet: bool) -> int:
    """CI / self-test: scan the diff of base..head."""

    try:
        top = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        repo = resolve_repo(top)

        if repo is None:
            print(f"domain-leak(--pushed-range): cannot identify the repository ({top}); refusing", file = sys.stderr)
            return 2

        if repo not in SCOPE:
            return 0

        rc, new_hits = scan_diff(repo, top, _diff_pairs(top, base, head))
    except RuntimeError as e:
        print(f"domain-leak(--pushed-range): {e}; refusing (fail-closed)", file = sys.stderr)
        return 2

    _report_hook_hits("push", new_hits, quiet)
    return rc

def run_pushed(stdin_text: str, quiet: bool) -> int:
    """pre-push: stdin carries git ref lines `<lref> <lsha> <rref> <rsha>`."""

    try:
        top = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        repo = resolve_repo(top)

        if repo is None:
            print(f"domain-leak(--pushed): cannot identify the repository ({top}); refusing", file = sys.stderr)
            return 2

        if repo not in SCOPE:
            return 0

        pairs: list[tuple[str, str]] = []

        for line in stdin_text.splitlines():
            parts = line.split()

            if len(parts) != 4:
                continue

            _, lsha, _, rsha = parts

            if set(lsha) == {"0"}:
                continue  # deleting a remote branch/tag introduces no content; skip it

            if set(rsha) == {"0"}:
                sym = _git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check = False).strip()
                remote = sym or "origin/master"
                mb = _git(top, "merge-base", lsha, remote, check = False).strip()

                if mb:
                    pairs += _diff_pairs(top, mb, lsha)
                else:
                    pairs += _tree_pairs(top, lsha)  # degraded base -> scan the whole tree
            else:
                pairs += _diff_pairs(top, rsha, lsha)

        rc, new_hits = scan_diff(repo, top, pairs)
    except RuntimeError as e:
        print(f"domain-leak(--pushed): {e}; refusing (fail-closed)", file = sys.stderr)
        return 2

    _report_hook_hits("push", new_hits, quiet)
    return rc

def run_scan(quiet: bool = False) -> tuple[int, list[dict], list[dict], list[dict]]:
    """Return (rc, new hits, baseline hits, stale baseline entries).

    Fails closed (``rc 2``) when nothing vouches for the root: with no declared root
    and no marker walk, every repository below is merely "missing", which used to
    print a green ``rc 0`` (the L68 follow-up). An explicit ``--workspace`` or
    ``$CELESTIA_WORKSPACE`` keeps the old semantics - the caller declared the root and
    owns the risk of an empty scan.
    """

    if WORKSPACE_SOURCE == WORKSPACE_SOURCE_FALLBACK:
        # Deliberately not silenced by --quiet: a configuration failure nobody can
        # read is the same trap as a quiet rejection in hook mode.
        print(f"domain-leak: no workspace markers found above {_REPO_ROOT} - pass --workspace or set {WORKSPACE_PATH_ENV} (refusing to report a green scan over zero reachable repositories)", file = sys.stderr)

        return 2, [], [], []

    baseline = {key(e): e for e in load_baseline()["entries"]}
    new_hits: list[dict] = []
    known_hits: list[dict] = []
    missing_repos: list[str] = []

    for repo in SCOPE:
        root = WORKSPACE / repo

        if not root.is_dir():
            missing_repos.append(repo)
            continue

        for h in scan_repo(repo, root):
            (known_hits if key(h) in baseline else new_hits).append(h)

    stale = [{"repo": r, "path": p, "terms": e.get("terms", []), "note": e.get("note", "")}for (r, p), e in baseline.items()if (r, p) not in {key(h)for h in known_hits}]

    if missing_repos and not quiet:
        print(f"domain-leak: missing repositories (treated as empty, not as hits): {', '.join(missing_repos)}", file = sys.stderr)

    return (1 if new_hits else 0), new_hits, known_hits, stale

def report(new_hits: list[dict], known_hits: list[dict], stale: list[dict], elapsed: float) -> None:
    for h in new_hits:
        print(f"  NEW   {h['repo']}/{h['path']}  terms={','.join(h['terms'])}")

    for h in known_hits:
        note = ""
        be = load_baseline()["entries"]

        for e in be:
            if key(e) == key(h):
                note = f"  [{e.get('note', '')}]"
                break

        print(f"  BASE  {h['repo']}/{h['path']}  terms={','.join(h['terms'])}{note}")

    if stale:
        print(f"  STALE {len(stale)} baseline entries (their files are clean now; --prune-baseline drops them):")

        for e in stale:
            print(f"    - {e['repo']}/{e['path']}")

    print(f"total: new {len(new_hits)} (rc {'1' if new_hits else '0'}) | baseline {len(known_hits)}" f" | stale {len(stale)} | {elapsed:.2f}s")

def _capture_cli(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run :func:`main` with both streams captured (self-test harness only)."""
    out, err = io.StringIO(), io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err

    try:
        return main(argv), out.getvalue(), err.getvalue()
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

def selftest() -> int:
    """Self-proof with known positives and negatives: the patterns hit, the baseline
    mechanism works, and the domain zone stays exempt.
    """
    global WORKSPACE, BASELINE, WORKSPACE_SOURCE
    tmp = Path(tempfile.mkdtemp(prefix = "dlscan-selftest-"))

    try:
        host = tmp / "kirino" / "src"
        host.mkdir(parents = True)
        (host / "a.rs").write_text('const X: &str = "hydrogen plant";\n', encoding = "utf-8")
        (host / "b.rs").write_text("fn main() { let 氢站 = 1; }\n", encoding = "utf-8")
        (host / "c.rs").write_text("// 电解槽 electrolyzer COMSOL 水素 Wasserstoff\n", encoding = "utf-8")
        (host / "clean.rs").write_text("fn generic() -> u8 { 42 }\n", encoding = "utf-8")
        dom = tmp / "entelecheia" / "packages" / "domain_agents" / "industrial_iot" / "src"
        dom.mkdir(parents = True)
        (dom / "ok.rs").write_text('// 氢能 demo\n', encoding = "utf-8")  # domain zone: allowed
        docs = tmp / "kirino" / "docs"
        docs.mkdir(parents = True)
        (docs / "d.md").write_text("hydrogen\n", encoding = "utf-8")  # .md is in reach

        real_ws = WORKSPACE

        try:
            WORKSPACE = tmp
            hits = []

            for repo in SCOPE:
                if (tmp / repo).is_dir():
                    hits += scan_repo(repo, tmp / repo)

            by_path = {h["path"]: h for h in hits}
            assert ("src/a.rs" in by_path) and ("hydrogen" in by_path["src/a.rs"]["terms"]), "ascii positive missed"
            assert ("src/b.rs" in by_path) and ("氢" in by_path["src/b.rs"]["terms"]), "CJK positive missed"
            assert "src/c.rs" in by_path, "electrolyzer/comsol positive missed"
            assert "src/clean.rs" not in by_path, "negative reported"
            assert not any("domain_agents" in p for p in by_path), "domain zone was not exempt"
            assert "docs/d.md" in by_path, ".md is in reach but was not scanned"
            assert {"electrolyzer", "comsol", "水素", "wasserstoff"}.issubset(by_path["src/c.rs"]["terms"]), (f"multi-term aggregation is wrong: {by_path['src/c.rs']['terms']}")
        finally:
            WORKSPACE = real_ws

        print("selftest: 7/7 assertions passed (3 positive, 1 negative, 1 exemption, 1 reach, 1 aggregation)")

        # ── Part two: full-scan fail-closed without a vouched-for root ──
        detached = tmp / "detached-no-markers"
        detached.mkdir(exist_ok = True)
        ws_backup_detached, source_backup_detached, base_backup_detached = WORKSPACE, WORKSPACE_SOURCE, BASELINE

        try:
            WORKSPACE = detached
            WORKSPACE_SOURCE = WORKSPACE_SOURCE_FALLBACK
            BASELINE = tmp / "detached-baseline.json"
            BASELINE.write_text('{"entries": []}', encoding = "utf-8")
            err_detached = io.StringIO()
            _sb_detached, sys.stderr = sys.stderr, err_detached

            try:
                rc_detached, _, _, _ = run_scan(quiet = True)
            finally:
                sys.stderr = _sb_detached

            assert rc_detached == 2, f"a full scan with no declared root and no workspace markers must fail closed (rc={rc_detached})"
            assert err_detached.getvalue().strip(), "the fail-closed full scan said nothing on stderr"
        finally:
            WORKSPACE, WORKSPACE_SOURCE, BASELINE = ws_backup_detached, source_backup_detached, base_backup_detached

        # ── Part three: the CLI contract of both refusals (quiet stdout, --quiet tight, --json keys) ──
        empty_root = tmp / "cli-empty-root"
        empty_root.mkdir(exist_ok = True)
        guarded_baseline = tmp / "cli-guarded-baseline.json"
        guarded_baseline.write_text('{"entries": [{"repo": "kirino", "path": "src/leak.rs", "terms": ["hydrogen"], "note": "guard"}]}', encoding = "utf-8")
        guarded_bytes = guarded_baseline.read_bytes()
        ws_backup_cli, source_backup_cli, base_backup_cli = WORKSPACE, WORKSPACE_SOURCE, BASELINE

        try:
            WORKSPACE, WORKSPACE_SOURCE, BASELINE = empty_root, WORKSPACE_SOURCE_FLAG, guarded_baseline
            rc_prune, out_prune, err_prune = _capture_cli(["--prune-baseline"])
            assert rc_prune == 2, f"prune with no reachable repository must refuse (rc={rc_prune})"
            assert out_prune == "", "the refused prune must keep stdout empty"
            assert err_prune.strip(), "the refused prune said nothing on stderr"
            assert guarded_baseline.read_bytes() == guarded_bytes, "the refused prune still rewrote the baseline"
            rc_prune_quiet, _, err_prune_quiet = _capture_cli(["--prune-baseline", "--quiet"])
            assert (rc_prune_quiet == 2) and err_prune_quiet.strip(), "--quiet swallowed the prune refusal"
            rc_prune_json, out_prune_json, _ = _capture_cli(["--prune-baseline", "--json"])
            payload_prune = json.loads(out_prune_json)
            assert (rc_prune_json == 2) and (payload_prune["rc"] == 2) and (set(payload_prune) == {"new", "baseline", "stale", "rc"}), (f"the refused prune broke the --json contract (rc={rc_prune_json}, keys={sorted(payload_prune)})")
            partial_root = tmp / "cli-partial-root"
            (partial_root / "kirino" / "src").mkdir(parents = True, exist_ok = True)
            WORKSPACE, WORKSPACE_SOURCE = partial_root, WORKSPACE_SOURCE_FLAG
            rc_partial, out_partial, err_partial = _capture_cli(["--prune-baseline"])
            assert rc_partial == 2, f"a prune with only part of the reach must refuse (rc={rc_partial})"
            assert (out_partial == "") and err_partial.strip(), "the partial-root prune refusal leaked or stayed silent"
            assert guarded_baseline.read_bytes() == guarded_bytes, "the partial-root prune still rewrote the baseline"
            WORKSPACE, WORKSPACE_SOURCE = detached, WORKSPACE_SOURCE_FALLBACK
            rc_scan, out_scan, err_scan = _capture_cli([])
            assert (rc_scan == 2) and (out_scan == "") and err_scan.strip(), "the CLI-level full-scan refusal leaked or stayed silent"
            rc_scan_json, out_scan_json, _ = _capture_cli(["--json"])
            assert (rc_scan_json == 2) and (json.loads(out_scan_json)["rc"] == 2), "--json lost the full-scan refusal code"
        finally:
            WORKSPACE, WORKSPACE_SOURCE, BASELINE = ws_backup_cli, source_backup_cli, base_backup_cli

        # ── Part four: hook mode against a real git repository (not a mock) ──
        ws_backup = WORKSPACE
        base_backup = BASELINE
        source_backup = WORKSPACE_SOURCE

        try:
            WORKSPACE = tmp
            # The self-test owns this throwaway workspace, so it vouches for the root
            # exactly like a marker walk would; without this the full-scan fail-closed
            # guard would (correctly) refuse in a bare CI checkout that has no markers.
            WORKSPACE_SOURCE = WORKSPACE_SOURCE_MARKER
            BASELINE = tmp / "baseline.json"
            BASELINE.write_text('{"entries": []}', encoding = "utf-8")
            grepo = tmp / "kirino"
            grepo.mkdir(exist_ok = True)
            _git(grepo, "init", "-q")
            _git(grepo, "config", "user.email", "dlscan@test")
            _git(grepo, "config", "user.name", "dlscan-selftest")
            (grepo / "src").mkdir(exist_ok = True)
            _git(grepo, "config", "core.hooksPath", "")  # isolate the family-wide hooks
            (grepo / "src" / "base.rs").write_text("fn a() {}\n", encoding = "utf-8")
            _git(grepo, "add", "-A")
            _git(grepo, "commit", "-qm", "base")
            # Staged mode: a newly leaking file must be caught
            (grepo / "src" / "leak.rs").write_text('const S: &str = "hydrogen";\n', encoding = "utf-8")
            _git(grepo, "add", "src/leak.rs")
            cwd_backup = os.getcwd()
            os.chdir(grepo)

            try:
                rc_staged = run_staged(True)
                # Baseline coverage: the same path passes once baselined (tracked debt)
                stale_entry = {"repo": "kirino", "path": "src/leak.rs", "terms": ["hydrogen"], "note": "t"}
                BASELINE.write_text(json.dumps({"entries": [stale_entry]}), encoding = "utf-8")
                rc_staged_baseline = run_staged(True)
                BASELINE.write_text('{"entries": []}', encoding = "utf-8")
                # Push mode: a leak inside base..head hits; out-of-reach files are not scanned
                (grepo / "docs").mkdir(exist_ok = True)
                (grepo / "docs" / "x.md").write_text("hydrogen\n", encoding = "utf-8")
                (grepo / "src" / "ok.rs").write_text("fn b() {}\n", encoding = "utf-8")
                _git(grepo, "add", "-A")
                _git(grepo, "commit", "-qm", "leak + md")
                head = _git(grepo, "rev-parse", "HEAD").strip()
                base = _git(grepo, "rev-parse", "HEAD~1").strip()
                rc_range = run_pushed_range(base, head, True)
                # A deleted remote ref (all-zero lsha) must be skipped, not crash
                zeros = "0" * 40
                rc_delete = run_pushed(f"refs/heads/topic {zeros} refs/heads/topic {head}", True)
                # A clone whose path cannot name the repository falls back to origin
                clone = tmp / "plain-place" / "k-clone"
                clone.parent.mkdir(parents = True, exist_ok = True)
                _git(clone.parent, "clone", "-q", str(grepo), str(clone))
                _git(clone, "remote", "set-url", "origin", "https://github.com/celestia-island/kirino.git")
                derived = resolve_repo(clone)
                # A local bare-clone or foreign-org origin resolves to None (fail-closed)
                _git(clone, "remote", "set-url", "origin", str(tmp / "bare.git"))
                derived_bare = resolve_repo(clone)
                _git(clone, "remote", "set-url", "origin", "https://github.com/celestia-island/kirino.git")
                # The origin fallback must hold end to end: a staged leak here is rc 1
                (clone / "src").mkdir(exist_ok = True)
                (clone / "src" / "leak.rs").write_text("// hydrogen\n", encoding = "utf-8")
                _git(clone, "add", "-A")
                cwd_clone = os.getcwd()
                os.chdir(clone)

                try:
                    rc_clone_e2e = run_staged(True)
                finally:
                    os.chdir(cwd_clone)

                # Binary staged content must not crash (errors=replace) and still hits
                (grepo / "src" / "blob.rs").write_bytes(b"\xff\xfe\x00binary\x00hydrogen")
                _git(grepo, "add", "src/blob.rs")
                rc_binary = run_staged(True)
                # Even under --quiet a violation must reach stderr
                err = io.StringIO()
                _sb, sys.stderr = sys.stderr, err

                try:
                    run_staged(True)
                finally:
                    sys.stderr = _sb

                quiet_stderr_nonempty = ("leak.rs" in err.getvalue()) or ("blob.rs" in err.getvalue())
                # Whole-repository reach: a root-level .md is in scope too
                (grepo / "README.md").write_text("# hydrogen demo\n", encoding = "utf-8")
                _git(grepo, "add", "README.md")
                rc_root_md = run_staged(True)
                _git(grepo, "reset", "-q", "--", "README.md", "src/blob.rs")
                (grepo / "README.md").unlink()
                (grepo / "src" / "blob.rs").unlink()
                # A new branch with no base falls back to the whole tree and still hits
                rc_fulltree = run_pushed(f"refs/heads/newbr {head} refs/heads/newbr {zeros}", True)

                for repo in SCOPE:
                    (tmp / repo).mkdir(exist_ok = True)

                # Concatenate the complete reach above first: prune keeps the _doc
                # metadata, but it refuses when any scanned repository is missing (Part
                # three covers that refusal on purpose). The remaining hit still gives rc 1.
                baseline_text = '{"_doc": "keepme", "entries": [{"repo": "kirino", "path": "nope/gone.rs", "terms": ["hydrogen"], "note": "stale"}]}'
                BASELINE.write_text(baseline_text, encoding = "utf-8")
                rc_prune = main(["--prune-baseline", "--quiet"])
                pruned = json.loads(BASELINE.read_text(encoding = "utf-8"))
                doc_kept = (pruned.get("_doc") == "keepme") and (pruned["entries"] == [])
            finally:
                os.chdir(cwd_backup)

            assert rc_staged == 1, f"--staged did not catch a staged leak (rc={rc_staged})"
            assert rc_staged_baseline == 0, "baseline coverage semantics broke"
            assert rc_range == 1, f"--pushed-range did not catch a pushed leak (rc={rc_range})"
            assert rc_delete == 0, f"a deleted ref was wrongly blocked (rc={rc_delete})"
            assert derived == "kirino", f"the origin fallback failed to identify the repo ({derived})"
            assert derived_bare is None, f"a local bare-clone origin must resolve to None ({derived_bare})"
            assert rc_clone_e2e == 1, f"the origin fallback did not block end to end (rc={rc_clone_e2e})"
            assert rc_binary == 1, f"binary staged content crashed or was missed (rc={rc_binary})"
            assert quiet_stderr_nonempty, "--quiet swallowed the violation report"
            assert rc_root_md == 1, f"a root-level .md was not caught (rc={rc_root_md})"
            assert rc_fulltree == 1, f"a new branch did not fall back to the whole tree (rc={rc_fulltree})"
            assert (rc_prune == 1) and doc_kept, (f"prune returned a false green or dropped _doc (rc={rc_prune}, doc_kept={doc_kept})")
        finally:
            WORKSPACE = ws_backup
            BASELINE = base_backup
            WORKSPACE_SOURCE = source_backup

        print("selftest: +24 assertions passed (detached full-scan fail-closed / cli refusal contract / prune-no-repository + partial-root guard / staged / baseline allow / pushed-range / deleted ref / " "origin fallback / bare None / origin end-to-end / binary / quiet stderr / root-level md / " "full-tree fallback / prune keeps _doc)")
        return 0
    except AssertionError as e:
        print(f"selftest: FAILED - {e}", file = sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors = True)

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog = "celestia-devtools domain-leak", description = "Fail when host-core code names industry vocabulary.")
    ap.add_argument("--quiet", action = "store_true", help = "exit code only")
    ap.add_argument("--json", action = "store_true", help = "machine-readable output (full scan)")
    ap.add_argument("--prune-baseline", action = "store_true", help = "drop fixed entries and rewrite the baseline")
    ap.add_argument("--selftest", action = "store_true", help = "known-positive / known-negative self-proof")
    ap.add_argument("--staged", action = "store_true", help = "pre-commit: this repository's staged content")
    ap.add_argument("--pushed", action = "store_true", help = "pre-push: ref lines from stdin")
    ap.add_argument("--pushed-range", nargs = 2, metavar = ("BASE", "HEAD"), help = "scan the diff of BASE..HEAD (CI / self-test)")
    ap.add_argument("--workspace", default = None, help = "workspace root (default: resolved from this checkout)")
    ap.add_argument("--baseline", default = None, help = "baseline path (default: the copy bundled here)")
    args = ap.parse_args(argv)

    # Overrides apply only when asked for: the self-test reassigns these module
    # globals itself, and an unconditional re-resolve would both defeat that and
    # point --prune-baseline at the bundled baseline file.
    global WORKSPACE, BASELINE, WORKSPACE_SOURCE

    if args.workspace:
        WORKSPACE, WORKSPACE_SOURCE = resolve_workspace_source(args.workspace)

    if args.baseline:
        BASELINE = resolve_baseline(args.baseline)

    if args.selftest:
        return selftest()

    if args.staged:
        return run_staged(args.quiet)

    if args.pushed:
        return run_pushed(sys.stdin.read(), args.quiet)

    if args.pushed_range:
        return run_pushed_range(args.pushed_range[0], args.pushed_range[1], args.quiet)

    t0 = time.monotonic()
    rc, new_hits, known_hits, stale = run_scan(quiet = args.quiet)

    if rc == 2:
        # Fail-closed: run_scan already said why on stderr. Return before the ordinary
        # report (a "0 new hits" summary next to a refusal reads like a pass) and before
        # any prune (a refused scan knows nothing about staleness). --json still emits
        # the same four keys so consumers keep one schema and read rc=2.
        if args.json:
            print(json.dumps({"new": new_hits, "baseline": known_hits, "stale": stale, "rc": rc}, ensure_ascii = False))

        return rc

    if args.prune_baseline:
        # A prune needs the whole reach, not part of it: entries belonging to a repository
        # that is not even checked out here would look stale and be deleted from a
        # committed file. Refuse like the other fail-closed paths: stdout stays empty,
        # --quiet does not swallow it, and --json keeps its four keys.
        missing_for_prune = [repo for repo in SCOPE if not (WORKSPACE / repo).is_dir()]

        if missing_for_prune:
            print(f"domain-leak: --prune-baseline refused - {len(missing_for_prune)} of {len(SCOPE)} scanned repositories are missing under {WORKSPACE} ({', '.join(missing_for_prune)}); their entries would look stale", file = sys.stderr)

            if args.json:
                print(json.dumps({"new": [], "baseline": [], "stale": [], "rc": 2}, ensure_ascii = False))

            return 2

        raw = load_baseline()

        if stale:
            stale_keys = {key(s) for s in stale}
            keep = [e for e in raw["entries"] if key(e) not in stale_keys]
            out = {k: v for k, v in raw.items() if k != "entries"}  # keep _doc-style metadata
            out["entries"] = keep
            BASELINE.write_text(json.dumps(out, ensure_ascii = False, indent = 2) + "\n", encoding = "utf-8")
            print(f"prune: dropped {len(stale)} stale entries, {len(keep)} left")
        else:
            print("prune: no stale entries")

        # prune is not a free pass: after dropping entries a new hit must still fail
        rc, new_hits, _, _ = run_scan(quiet = args.quiet)

        if not args.quiet:
            for h in new_hits:
                print(f"  NEW   {h['repo']}/{h['path']}  terms={','.join(h['terms'])}", file = sys.stderr)

        return rc

    if args.json:
        print(json.dumps({"new": new_hits, "baseline": known_hits, "stale": stale, "rc": rc}, ensure_ascii = False))
        return rc

    if not args.quiet:
        report(new_hits, known_hits, stale, time.monotonic() - t0)

        if new_hits:
            print("-> a new hit is host-core leakage: use neutral wording, or move the data into" " a domain_* package or the seed channel. Baseline changes go through a" " celestia-devtools pull request; do not edit a local copy.", file = sys.stderr)

    return rc

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
