"""Unified CLI dispatch for celestia-devtools.

Usage::

    celestia-devtools <command> [args...]
    python -m celestia_devtools <command> [args...]

Commands::

    cache-guard       Manage cargo target/ disk usage (hard floor + soft sweep)
    format-markdown   Format and lint Markdown files
    prefetch          Pre-stage cargo + node dependencies for offline builds
    check-cross-deps  Check/install cross-compilation prerequisites (zigbuild)
    npm-dist          Stage npx precompiled npm packages for a Rust binary
    preflight         Check that required dev tools are present (node/rust/...)
    wsl-ensure        Ensure a shared WSL2 dev distro exists (rust/just/docker)
    pglite            Start/stop a shared temporary PGlite server (pgvector)
    serve             Shared process-supervision (ProcessManager) for dev scripts
    locate            Locate a celestia-island crate checkout
    register-patches  Auto-register local repos as cargo [patch] entries
    register-npm-patches  RETIRED — remove legacy link: overrides (--remove)
    link-npm-siblings Symlink local @celestia-island sibling checkouts into node_modules
    init              Symlink common.just into a repo for justfile import
    include-path      Print the path to the bundled common.just
    commit-msg-lint   Validate commit messages against the org gitmoji convention
    hook              Manage the celestia-devtools commit-msg hook lifecycle
    pr-merge          Validate subject and merge via gh pr merge.
    nav-lint          Gate navigation call sites against poisoned/off-origin targets
    p0-gate           Fail a repo covered by an unresolved P0 finding (explicit ack to pass)
    rules-lint        Lint a split workspace-rules tree (core AGENTS.md + ledgers + skills)
    gh                Transparent gh proxy — validates subject on pr merge, forwards everything else.
    release-notes     Generate categorized GitHub release notes from squash PR subjects
    sign-agent        Keygen/sign/verify Ed25519 signatures for Layer-3 agents
    gate              Run the local CI gate (modes + DAG ordering + job budget)
    verify-versions   Check cargo/npm version drift across a repository
    protocol-bundle   Vendor the five org protocol docs from docs.celestia.world
                      into packages/webui/.generated/protocols as lazy assets
    fetch-just        Stage the bundled common.just into .just/ (just fetch core)
    build-dispatch    Run pre/dev/release build commands for _build-style recipes
    upstream-sync     Fetch the 'upstream' remote (adds it from $UPSTREAM_URL)
    worktree-create   Create ../<repo>-<name> worktree + register cargo patches
    worktree-remove   Remove a worktree and prune stale refs
    dev-watch         Supervise a command with malkuth file watching
    vite-build        One-shot production vite build
    vite-serve        Serve the built dist/ with python http.server
    vite-dev          Build → serve → watch src/ (malkuth) rebuild loop
    npm-release       Publish staged npm packages from ./dist
    e2e-sandbox       Run e2e/browser jobs in an isolated, always-cleaned TMPDIR
                      sandbox; sweep stale sandbox dirs and orphan chromium
                      profiles from /tmp (run | sweep | sweep-tmp)
Each command has its own argparse interface; this dispatcher simply forwards
``argv`` so the individual ``main()`` entry points stay self-contained and
usable as standalone scripts.
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import Sequence

COMMANDS: dict[str, str] = {
    "cache-guard": "celestia_devtools.build.cache_guard",
    "format-markdown": "celestia_devtools.doc.markdown",
    "prefetch": "celestia_devtools.build.prefetch",
    "check-cross-deps": "celestia_devtools.build.cross_deps",
    "npm-dist": "celestia_devtools.npm.dist",
    "preflight": "celestia_devtools.env.preflight",
    "wsl-ensure": "celestia_devtools.env.wsl",
    "qemu-ensure": "celestia_devtools.env.qemu",
    "pglite": "celestia_devtools.env.pglite",
    "serve": "celestia_devtools.env.serve",
    "locate": "celestia_devtools.repo.locate",
    "register-patches": "celestia_devtools.repo.register_patches",
    "register-npm-patches": "celestia_devtools.npm.register_patches",
    "link-npm-siblings": "celestia_devtools.npm.link_siblings",
    "init": "celestia_devtools.repo.init",
    "commit-msg-lint": "celestia_devtools.vcs.commit_msg",
    "hook": "celestia_devtools.vcs.hook",
    "pr-merge": "celestia_devtools.vcs.pr_merge",
    "nav-lint": "celestia_devtools.lint.nav_lint",
    "p0-gate": "celestia_devtools.lint.p0_gate",
    "rules-lint": "celestia_devtools.lint.rules_lint",
    "gh": "celestia_devtools.vcs.gh",
    "publish-crates": "celestia_devtools.publish.crates",
    "release-notes": "celestia_devtools.publish.release_notes",
    "toml-sort": "celestia_devtools.repo.toml_sort",
    "daemon": "celestia_devtools.env.daemon",
    "deploy": "celestia_devtools.deploy.cli",
    "mock-start": "celestia_devtools.core.mock",
    "mock-stop": "celestia_devtools.core.mock",
    "mock-status": "celestia_devtools.core.mock",
    "registry": "celestia_devtools.core.mock",
    "sign-agent": "celestia_devtools.agent.sign",
    "gate": "celestia_devtools.build.gate",
    "verify-versions": "celestia_devtools.repo.verify_versions",
    "protocol-bundle": "celestia_devtools.doc.protocol_bundle",
    "fetch-just": "celestia_devtools.repo.fetch_just",
    "build-dispatch": "celestia_devtools.build.dispatch",
    "upstream-sync": "celestia_devtools.vcs.upstream",
    "worktree-create": "celestia_devtools.vcs.worktree",
    "worktree-remove": "celestia_devtools.vcs.worktree",
    "dev-watch": "celestia_devtools.env.dev_watch",
    "vite-build": "celestia_devtools.env.vite",
    "vite-serve": "celestia_devtools.env.vite",
    "vite-dev": "celestia_devtools.env.vite",
    "npm-release": "celestia_devtools.npm.release",
    # Registered entry points that had no dispatcher command until 2026-09-20. The gap went
    # unnoticed because the only test that notices (`test_all_commands_registered`) was
    # already failing on master, so its output was not read: four tools installed by this
    # package could not be reached through the unified CLI, while both `justfile` and the
    # reusable workflows invoke commands as `celestia-devtools <cmd>`.
    "cargo-cache-guard": "celestia_devtools.build.cache_guard",
    "lint-separators": "celestia_devtools.lint.separator_lint",
    "job-timeouts": "celestia_devtools.ci.job_timeouts",
    "ci-audit": "celestia_devtools.ci.workflow_audit",
    "ci-cache": "celestia_devtools.ci.cache_policy",
    "e2e-sandbox": "celestia_devtools.env.e2e_sandbox",
}


def _print_help(file=None) -> None:
    print(__doc__, file=file or sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    cmd, rest = args[0], args[1:]

    if cmd in ("-V", "--version"):
        from celestia_devtools import __version__

        print(f"celestia-devtools {__version__}")
        return 0

    if cmd == "include-path":
        from celestia_devtools.repo.init import common_just_path

        print(common_just_path())
        return 0

    module_path = COMMANDS.get(cmd)
    if module_path is None:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        _print_help(file=sys.stderr)
        return 2

    mod = import_module(module_path)
    entry = getattr(mod, "main", None)
    if not callable(entry):
        print(f"error: module '{cmd}' has no main()", file=sys.stderr)
        return 2

    sys.argv = [cmd, *rest]
    return int(entry() or 0)


if __name__ == "__main__":
    # Without this guard `python3 -m celestia_devtools.core.cli …` imports the
    # module, exits 0, and does nothing — the silent no-op that made chest's
    # protocols codegen step produce nothing while staying green
    # (see chest #1028 and the known-env-issues ledger).
    sys.exit(main())
