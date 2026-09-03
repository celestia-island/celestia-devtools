#!/usr/bin/env python3
"""Symlink local ``@celestia-island/*`` sibling checkouts into ``node_modules``.

Local-development overlay for celestia-island frontend workspaces. Repo
manifests (``package.json`` / ``pnpm-workspace.yaml``) stay registry-only:
every ``@celestia-island/*`` dependency resolves from the npm registry by
default. This command overlays selected dependencies with symlinks to
sibling checkouts so you can develop against upstream master without
touching any tracked file:

    pnpm install                            # registry resolution (lockfile truth)
    celestia-devtools link-npm-siblings     # overlay sibling checkouts
    celestia-devtools link-npm-siblings --status
    celestia-devtools link-npm-siblings --remove   # back to pure registry

Mechanism: for each ``@celestia-island/*`` dependency declared by the
workspace (root or ``packages/*/`` manifests) a sibling checkout providing
the same package name is discovered near the repo (see scan-dir below) and
linked in at ``node_modules/<name>`` (or ``packages/<pkg>/node_modules/<name>``
for per-package declarations). The original install state is recorded in
``node_modules/.celestia-linked-siblings.json`` so ``--remove`` can restore it.

Notes:

- ``pnpm install`` restores registry resolution; re-run this command after
  every install. The command is idempotent.
- This replaces the retired ``register-npm-patches`` mechanism: repo files
  must never reference sibling checkouts (no ``link:`` overrides, no
  ``../`` workspace members).

The scan directory for sibling repos is resolved in this order:

1. ``--scan-dir`` CLI argument (explicit)
2. ``CELESTIA_ROOT`` environment variable
3. The parent directory of the current working directory (fallback)

Usage::

    celestia-devtools link-npm-siblings
    celestia-devtools link-npm-siblings --status
    celestia-devtools link-npm-siblings --remove
    celestia-devtools link-npm-siblings --scan-dir /path/to/celestia
    celestia-devtools link-npm-siblings --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

from celestia_devtools.npm.register_patches import (
    NpmPackage,
    _resolve_scan_dir,
    detect_package_manager,
    is_celestia_repo,
    scan_celestia_npm_packages,
)

STATE_FILENAME = ".celestia-linked-siblings.json"

DEP_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)

# ../ sibling refs in pnpm-workspace.yaml — legacy mechanism this command
# replaces; their presence means the workspace still hard-requires checkouts.
_LEGACY_MEMBER_RE = re.compile(r"^\s*-\s*['\"]?\.\./", re.M)
_LEGACY_OVERRIDE_RE = re.compile(r"link:\.\./")


class LinkPlan(NamedTuple):
    """One dependency-to-sibling link to materialize."""

    name: str           # full package name (e.g. "@celestia-island/hikari")
    sibling: NpmPackage # discovered sibling checkout providing *name*
    link_path: Path     # absolute node_modules/<name> path to symlink
    consumer: str       # declaring manifest dir relative to repo root ("." = root)


class LinkResult(NamedTuple):
    """Outcome of applying one :class:`LinkPlan`."""

    plan: LinkPlan
    action: str         # "created" | "updated" | "ok" | "skipped-dir" | "forced-dir" | "dry-run"


# ── dependency discovery ──────────────────────────────────────────────────


def _manifest_dirs(repo_root: Path) -> list[Path]:
    """Directories of the target workspace that own a package.json."""
    dirs = [repo_root]
    try:
        for pkg_json in sorted(repo_root.glob("packages/*/package.json")):
            dirs.append(pkg_json.parent)
    except OSError:
        pass
    return dirs


def collect_declared_family_deps(repo_root: Path) -> dict[str, list[Path]]:
    """Map ``@celestia-island/*`` dependency names to declaring manifest dirs.

    The root ``package.json`` plus every ``packages/*/package.json`` is
    scanned across all dependency fields. Returns ``name -> [dirs]`` sorted
    for stable output.
    """
    declared: dict[str, list[Path]] = {}
    for manifest_dir in _manifest_dirs(repo_root):
        pkg_json = manifest_dir / "package.json"
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        for field in DEP_FIELDS:
            deps = data.get(field)
            if not isinstance(deps, dict):
                continue
            for name in deps:
                if isinstance(name, str) and name.startswith("@celestia-island/"):
                    bucket = declared.setdefault(name, [])
                    if manifest_dir not in bucket:
                        bucket.append(manifest_dir)
    return {name: sorted(dirs) for name, dirs in sorted(declared.items())}


def _link_locations(repo_root: Path, consumer_dir: Path) -> list[Path]:
    """node_modules candidates for a dep declared in *consumer_dir*.

    pnpm installs each workspace package's deps into that package's own
    ``node_modules`` (root declarations into the root ``node_modules``).
    Only the declaring level is linked — creating extra shadow links would
    open phantom resolution paths CI will not have.
    """
    if consumer_dir == repo_root:
        return [repo_root / "node_modules"]
    return [consumer_dir / "node_modules"]


def plan_links(
    repo_root: Path,
    siblings: list[NpmPackage],
    declared: dict[str, list[Path]],
) -> tuple[list[LinkPlan], list[str]]:
    """Match declared family deps against discovered sibling packages.

    Returns the concrete link plans plus human-readable notices for deps
    that stay on the registry (no sibling checkout providing that name).
    """
    by_name = {pkg.name: pkg for pkg in siblings}
    plans: list[LinkPlan] = []
    notices: list[str] = []
    for name, consumer_dirs in declared.items():
        sibling = by_name.get(name)
        if sibling is None:
            notices.append(f"{name}: no sibling checkout found — stays on the npm registry")
            continue
        seen: set[Path] = set()
        for consumer_dir in consumer_dirs:
            for node_modules in _link_locations(repo_root, consumer_dir):
                link_path = node_modules / name
                if link_path in seen:
                    continue
                seen.add(link_path)
                rel = (
                    "." if consumer_dir == repo_root
                    else consumer_dir.relative_to(repo_root).as_posix()
                )
                plans.append(LinkPlan(name, sibling, link_path, rel))
    return plans, notices


# ── state bookkeeping ─────────────────────────────────────────────────────


def _state_path(repo_root: Path) -> Path:
    return repo_root / "node_modules" / STATE_FILENAME


def load_state(repo_root: Path) -> dict:
    """Read the recorded link state, or an empty shell."""
    path = _state_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {"links": {}}
    if not isinstance(data, dict) or not isinstance(data.get("links"), dict):
        return {"links": {}}
    return data


def save_state(repo_root: Path, state: dict) -> None:
    path = _state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── link application ──────────────────────────────────────────────────────


def apply_link(
    plan: LinkPlan,
    repo_root: Path,
    state: dict,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> LinkResult:
    """Materialize one sibling symlink, recording prior state for --remove."""
    link_path = plan.link_path
    target = plan.sibling.path
    key = str(link_path)
    links: dict = state["links"]
    entry: dict = links.get(key, {})
    action = "created"

    if link_path.is_symlink():
        current_target: str | None = None
        try:
            current_target = os.readlink(link_path)
            resolved_target = Path(current_target)
            resolved = link_path.parent / resolved_target \
                if not resolved_target.is_absolute() else resolved_target
            action = "ok" if resolved.resolve() == target.resolve() else "updated"
        except OSError:
            action = "updated"
        if action == "updated":
            # Keep the first-seen pre-overlay target — repeated overlays must
            # not overwrite the restore point with our own previous link.
            # When nothing valid is recorded yet (fresh entry, or a first
            # overlay that found no pre-existing link at all) but an external
            # symlink now occupies the location — e.g. pnpm replaced our link
            # after a fresh install — capture that target instead, or --remove
            # would leave the dependency missing.
            if entry.get("original") is None and current_target is not None:
                entry["original"] = current_target
            entry["was_dir"] = False
            links[key] = entry
    elif link_path.exists():
        if not force:
            return LinkResult(plan, "skipped-dir")
        action = "forced-dir"
        entry["original"] = None
        entry["was_dir"] = True
        links[key] = entry
    else:
        entry["original"] = None
        entry["was_dir"] = False
        links[key] = entry

    if action == "ok":
        links[key] = {**links.get(key, {}), "target": str(target)}
        return LinkResult(plan, "ok")

    if dry_run:
        return LinkResult(plan, "dry-run")

    _remove_path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link_path)
    links[key] = {**entry, "target": str(target)}
    return LinkResult(plan, action)


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        import shutil

        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def remove_links(repo_root: Path, *, dry_run: bool = False) -> int:
    """Undo every link recorded in the state file (restore prior state).

    Only entries keyed under this repo root are touched. Worktree
    ``node_modules`` directories are symlinked to the shared main checkout
    (org convention), so one physical state file aggregates entries from
    every worktree that used it; entries belonging to other worktrees are
    kept so their own worktree can still ``--remove`` them, and the state
    file is only deleted once no foreign entries remain. Keys carry the
    absolute, resolved path of the declaring repo (root-level and
    ``packages/*/`` node_modules alike), so the repo-root prefix is the
    ownership boundary.
    """
    state = load_state(repo_root)
    links: dict = state.get("links", {})
    prefix = str(repo_root) + os.sep
    own = {key: entry for key, entry in links.items() if key.startswith(prefix)}
    foreign = {key: entry for key, entry in links.items() if not key.startswith(prefix)}

    if not own:
        if foreign:
            print(
                "[link-npm-siblings] no recorded sibling links for this repo "
                f"root — keeping {len(foreign)} foreign entries from other "
                "worktrees sharing this node_modules",
            )
        else:
            print("[link-npm-siblings] no recorded sibling links — nothing to remove")
        return 0

    restored = 0
    for key, entry in sorted(own.items()):
        link_path = Path(key)
        if dry_run:
            print(f"[link-npm-siblings] dry-run: would remove {link_path}")
            continue
        if link_path.is_symlink():
            link_path.unlink()
            original = entry.get("original")
            if original:
                os.symlink(original, link_path)
                print(f"[link-npm-siblings] restored {link_path} -> {original}")
            else:
                print(f"[link-npm-siblings] removed {link_path}")
            restored += 1
        elif link_path.exists():
            print(f"[link-npm-siblings] skipping {link_path} (not a symlink we manage)")
        else:
            restored += 1

    if not dry_run:
        if foreign:
            state["links"] = foreign
            save_state(repo_root, state)
            print(
                "[link-npm-siblings] kept foreign state entries — run --remove "
                "from their own worktree to clear them",
            )
        else:
            _state_path(repo_root).unlink(missing_ok=True)
        missing = [
            key for key, entry in own.items()
            if entry.get("was_dir") and not Path(key).exists()
        ]
        if missing:
            print(
                "[link-npm-siblings] note: previously-overwritten real directories "
                f"({len(missing)}) are gone — run `pnpm install` to restore them",
            )
        print("[link-npm-siblings] done — run `pnpm install` for pristine registry resolution")
    return 0


# ── legacy-mechanism detection ────────────────────────────────────────────


def warn_legacy_sibling_refs(repo_root: Path) -> None:
    """Warn when the workspace still hard-references sibling checkouts."""
    workspace = repo_root / "pnpm-workspace.yaml"
    try:
        text = workspace.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    problems = []
    if _LEGACY_MEMBER_RE.search(text):
        problems.append("'../' workspace members in pnpm-workspace.yaml")
    if _LEGACY_OVERRIDE_RE.search(text):
        problems.append("'link:../' overrides in pnpm-workspace.yaml")
    if problems:
        print(
            "\033[33m[link-npm-siblings] warning: legacy sibling references found "
            f"({'; '.join(problems)}). \033[0m",
            file=sys.stderr,
        )
        print(
            "\033[33mWorkspace manifests should stay registry-only; migrate with "
            "`register-npm-patches --remove` and drop the '../' members.\033[0m",
            file=sys.stderr,
        )


# ── reporting ─────────────────────────────────────────────────────────────


def _print_status(repo_root: Path, plans: list[LinkPlan], notices: list[str]) -> None:
    state = load_state(repo_root)
    print(f"[link-npm-siblings] status for {repo_root}")
    for plan in plans:
        link_path = plan.link_path
        if link_path.is_symlink():
            target = Path(os.readlink(link_path))
            print(f"  {plan.name} -> {target}")
        elif link_path.exists():
            print(f"  {plan.name}: real directory at {link_path} (not linked)")
        else:
            print(f"  {plan.name}: not linked — registry resolution applies")
    for name, consumers in _not_linked_but_recorded(state, plans):
        print(f"  {name}: recorded link state without a current plan ({consumers})")
    for notice in notices:
        print(f"  {notice}")


def _not_linked_but_recorded(state: dict, plans: list[LinkPlan]) -> list[tuple[str, str]]:
    planned = {str(plan.link_path) for plan in plans}
    out = []
    for key, entry in sorted(state.get("links", {}).items()):
        if key not in planned:
            out.append((Path(key).name, entry.get("target", "?")))
    return out


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Symlink local @celestia-island sibling checkouts into node_modules",
    )
    parser.add_argument(
        "--scan-dir", default=None,
        help="directory to scan for sibling repos "
             "(env: CELESTIA_ROOT; default: parent of cwd)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="show current link state without changing anything",
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="remove recorded sibling links (restore registry resolution)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace real directories found at link locations (recorded for --remove)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without touching node_modules",
    )
    args = parser.parse_args()

    repo_root = Path.cwd().resolve()
    if not (repo_root / "pnpm-workspace.yaml").is_file():
        print(
            "[link-npm-siblings] pnpm-workspace.yaml not found — run from the "
            "workspace root of a pnpm repo.",
            file=sys.stderr,
        )
        return 1

    own_repo = is_celestia_repo(repo_root)
    scan_dir = _resolve_scan_dir(args.scan_dir)

    if args.status and args.remove:
        parser.error("--status and --remove are mutually exclusive")

    # --remove must work even in a repo that no longer declares family deps
    # (stale recorded links still need clearing) and must not pay for the
    # sibling scan; --status likewise reports before any early-return.
    if args.remove:
        return remove_links(repo_root, dry_run=args.dry_run)

    siblings = scan_celestia_npm_packages(scan_dir, exclude_repo=own_repo)
    declared = collect_declared_family_deps(repo_root)

    if args.status:
        plans, notices = plan_links(repo_root, siblings, declared)
        _print_status(repo_root, plans, notices)
        return 0

    if not declared:
        print(
            "[link-npm-siblings] no @celestia-island dependencies declared — "
            "nothing to link",
        )
        return 0

    plans, notices = plan_links(repo_root, siblings, declared)

    if args.status:
        _print_status(repo_root, plans, notices)
        return 0

    if detect_package_manager(repo_root) != "pnpm":
        print(
            "[link-npm-siblings] only pnpm workspaces are supported",
            file=sys.stderr,
        )
        return 1

    warn_legacy_sibling_refs(repo_root)

    if not plans:
        print("[link-npm-siblings] no sibling checkouts provide the declared deps:")
        for notice in notices:
            print(f"  {notice}")
        return 0

    state = load_state(repo_root)
    changed = 0
    for plan in plans:
        result = apply_link(plan, repo_root, state, force=args.force, dry_run=args.dry_run)
        rel = os.path.relpath(str(plan.link_path), str(repo_root))
        if result.action == "ok":
            print(f"  = {plan.name} @{plan.sibling.version} already linked ({rel})")
        elif result.action == "skipped-dir":
            print(
                f"  ! {plan.name}: {rel} is a real directory — skipped "
                f"(use --force to replace)",
                file=sys.stderr,
            )
        elif result.action == "dry-run":
            print(f"  ~ {plan.name} @{plan.sibling.version} -> {rel} (dry-run)")
            changed += 1
        else:
            print(f"  {result.action} {plan.name} @{plan.sibling.version} -> {rel}")
            changed += 1

    for notice in notices:
        print(f"  - {notice}")

    if args.dry_run:
        print(f"[link-npm-siblings] dry-run: {changed} link(s) would change")
        return 0

    if changed:
        save_state(repo_root, state)
        print(
            f"[link-npm-siblings] done — {changed} link(s) applied. "
            "Re-run after every `pnpm install`.",
        )
    else:
        print("[link-npm-siblings] already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
