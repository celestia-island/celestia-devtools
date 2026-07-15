# celestia-devtools common.just — shared build/devtool recipes.
#
# This file is NOT committed to your repo. It is staged on demand into .just/
# (gitignored) by `just fetch` in your justfile, then pulled in via an optional
# import: `import? "./.just/celestia-devtools.just"`. The `?` lets a fresh clone
# parse the justfile before fetch, so your own recipes always work.
#
# Stage or refresh it with:  just fetch
# Version: 0.4.0
#
# All recipes and variables are overridable — redefine anything after the
# `import?` line in your justfile to customize it.
#
# This file is intentionally free of `set` directives (shell, windows-shell,
# unstable) so it composes cleanly with each repo's own header.
#
# Windows note: the importing justfile MUST set `set windows-shell := ["bash.exe", "-c"]`
# and `set shell := ["bash", "-c"]`. Linewise recipes run through windows-shell;
# multi-line recipes below use the empty `[script]` attribute (stable since
# just 1.32.0), whose interpreter comes from `set script-interpreter` — supplied
# by the staged `.just/git-bash-interop.just` (run `celestia-devtools init`).
#
# WSL caveat: an explicit `[script('bash')]` would resolve `bash` via PATH, and
# if `C:\Windows\System32` precedes Git, that hits the WSL launcher — which can't
# see the Windows temp path just writes [script] bodies to. So these recipes use
# empty `[script]` instead, letting the interop file pin them to the resolved Git
# Bash absolute path. `celestia-devtools init` stages that file automatically.
# The `windows-shell-check` recipe below still detects a residual hijack.
#   [Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')

# ── Tool resolution ───────────────────────────────────────────────────────────

# On Windows prefer `python` — `python3` is often the broken WindowsApps Store
# stub that exits 49. On Unix, `python3` is the norm.
python_cmd := if os_family() == "windows" {
    if which("python") != "" { "python" } else { "python3" }
} else {
    if which("python3") != "" { "python3" } else { "python" }
}

# Resolve the celestia-devtools CLI: installed entry point, or module fallback.
_devtools := if which("celestia-devtools") != "" { "celestia-devtools" } else { python_cmd + " -m celestia_devtools" }

# ── Bootstrap ─────────────────────────────────────────────────────────────────

# Ensure celestia-devtools is installed. Run once on a fresh checkout.
# Override the install source with $CELESTIA_DEVTOOLS_INSTALL (e.g. a local path).
ensure:
    @command -v celestia-devtools >/dev/null 2>&1 || \
        {{python_cmd}} -c "import celestia_devtools" 2>/dev/null || \
        pip3 install "${CELESTIA_DEVTOOLS_INSTALL:-git+https://github.com/celestia-island/celestia-devtools.git}"

# Fail fast on Windows if the importing justfile forgot `set windows-shell`,
# OR if `bash` is shadowed by the WSL launcher (C:\Windows\System32\bash.exe).
#
# This recipe is LINewise (not [script]) on purpose: a [script] body is
# written to a Windows temp path that WSL bash cannot see, so under WSL
# shadowing the check itself would crash with "No such file or directory"
# before it could report the problem. Linewise recipes run via windows-shell
# (`bash.exe -c "<line>"`), which WSL bash can still execute, so the check
# survives and can diagnose the hijack. No-op on non-Windows hosts.
windows-shell-check:
    @if [ "{{os_family()}}" != "windows" ]; then echo "windows-shell-check: non-Windows host, OK."; exit 0; fi
    @_u="$(uname -o 2>/dev/null || echo unknown)"; if [ "$_u" = "GNU/Linux" ] || [ -z "${MSYSTEM:-}" ]; then echo "windows-shell-check: HIJACK — 'bash' resolves to WSL ($_u), not Git Bash." >&2; echo "  Every recipe using bash syntax or [script] will fail." >&2; echo "  FIX (PowerShell, one-time, then restart terminal):" >&2; echo "    [Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + \$env:PATH,'User')" >&2; exit 1; fi
    @echo "windows-shell-check: OK — Git Bash detected."

# ── Build helper ──────────────────────────────────────────────────────────────

# Shared build dispatcher: runs a prerequisite, then picks dev vs release cmd.
#
#   just _build ":" "cargo build" "cargo build --release" {{FLAGS}}
#   just _build "just cache-guard" "cargo build" "cargo build --release" --dev
#
# Flags (consumed from *FLAGS):
#   --dev    use the dev command instead of release
#   --clean  cargo clean before building
[script]
_build always_pre dcmd rcmd *FLAGS='':
    set -euo pipefail
    profile=release
    for a in {{FLAGS}}; do
      case "$a" in
        --dev)   profile=dev ;;
        --clean) cargo clean ;;
      esac
    done
    [ "X{{always_pre}}" != "X:" ] && {{always_pre}}
    if [ "$profile" = dev ]; then {{dcmd}}; else {{rcmd}}; fi

# ── Cache management ──────────────────────────────────────────────────────────

# target/ cache guard:
#   free-disk floor < 10 GiB → cargo clean (instant check, every run)
#   target ≥ 40 GiB → cargo sweep --time 7 (needs cargo-sweep; cached du)
# Tune via flags or CARGO_CACHE_GUARD_* env vars. Run before `just build`.
cache-guard *ARGS='':
    {{ _devtools }} cache-guard . {{ARGS}}

# Backwards-compatible alias of cache-guard.
sweep-guard min_free="10":
    {{ _devtools }} cache-guard . --min-free-gib {{min_free}}

# Manually remove target/**/incremental/ (keeps compiled dep artifacts).
clean-incremental:
    {{ _devtools }} cache-guard . --clean-incremental

# ── Formatting ────────────────────────────────────────────────────────────────

# Format Markdown docs (stdlib-only; call inside your fmt/fmt-check recipe).
# Does NOT touch Rust or JS — compose with cargo fmt / pnpm lint yourself.
fmt-markdown *ARGS='':
    {{ _devtools }} format-markdown . {{ARGS}}

# ── Commit message lint ──────────────────────────────────────────────────────

# Validate a commit message against the org gitmoji convention.
# Pass the commit-msg file path (git passes $1 to the hook).
commit-msg-lint FILE:
    {{ _devtools }} commit-msg-lint check {{ FILE }}

# Install (or refresh) the org commit-msg hook into .git/hooks/.
# Normally triggered automatically by `celestia-devtools init`; call this
# directly to reinstall after a force-override or to install into a fresh
# checkout where init has already vendored the justfile.
commit-msg-hook-install *ARGS='':
    {{ _devtools }} hook install {{ ARGS }}

# ── Upstream fork sync ───────────────────────────────────────────────────────
#
# For repos that track an upstream (kei ← asterinas, aris ← servo, …). Fetches
# the latest upstream commits so a human can then merge or rebase deliberately
# — this recipe never auto-merges.
#
# Prereqs: an `upstream` git remote. If absent it is added from $UPSTREAM_URL
# (set it in your .env or shell); otherwise the recipe errors with guidance.

upstream-sync:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! git remote get-url upstream >/dev/null 2>&1; then
        if [ -z "${UPSTREAM_URL:-}" ]; then
            echo "error: no 'upstream' remote and UPSTREAM_URL is unset." >&2
            echo "  Add one with:  git remote add upstream <url>" >&2
            echo "  or set UPSTREAM_URL in your .env and re-run." >&2
            exit 1
        fi
        echo "[upstream-sync] adding 'upstream' -> ${UPSTREAM_URL}"
        git remote add upstream "${UPSTREAM_URL}"
    fi
    echo "[upstream-sync] fetching upstream…"
    git fetch upstream
    echo "[upstream-sync] fetched. Inspect with:"
    echo "  git log --oneline HEAD..upstream/<branch>"
    echo "  then merge or rebase as you see fit."

# ── Dependencies ──────────────────────────────────────────────────────────────

# Pre-stage all deps for offline builds (cargo fetch + pnpm/npm/yarn install).
# Run once after cloning (needs network); subsequent builds run fully offline.
prefetch:
    {{ _devtools }} prefetch .

# ── Cross-compilation ─────────────────────────────────────────────────────────

# Check and auto-install cross-compilation prerequisites (rustup target +
# ziglang + cargo-zigbuild). Mirror selection auto-detected (TUNA for CN).
cross-check target='aarch64-unknown-linux-gnu' features='full':
    {{ _devtools }} check-cross-deps --target {{target}} --features {{features}}

# ── Crate location ────────────────────────────────────────────────────────────

# Locate a celestia-island crate checkout (env → cargo [patch] → sibling → clone).
locate crate:
    {{ _devtools }} locate --crate {{crate}}

# Print the local path of a sibling repo, honouring $<NAME>_ROOT or $<NAME>_REPO
# (the kei/aris convention, e.g. $ARIS_REPO) before falling back to a sibling
# checkout. Exits non-zero if the repo cannot be found, so it is safe to use as
#   ARIS := `just link-sibling aris`
# in your own recipes.
link-sibling crate:
    {{ _devtools }} locate --crate {{crate}}

# ── File-watch dev server (malkuth) ──────────────────────────────────────────
#
# malkuth (celestia-island/malkuth) wraps any command with file-watching +
# auto-restart: notify v8 recursive watch, 400 ms debounce, kill+respawn.
#
# RECOMMENDED PATTERN: do NOT call `just dev-watch` from your repo's `dev`
# recipe — that forks a child `just` process which breaks under WSL PATH
# shadowing (the child `just` isn't on WSL bash's PATH). Instead, make your
# `dev` a self-contained [script] that calls malkuth directly:
#
#   [script]
#   dev:
#       #!/usr/bin/env bash
#       set -euo pipefail
#       malkuth="{{malkuth_bin}}"
#       # ... (resolve malkuth as shown in the dev-watch body below) ...
#       exec "$malkuth" --watch src --watch docs --drain-secs 2 -- cargo run
#
# The `dev-watch` recipe below is kept as a reference implementation and for
# ad-hoc use from the command line, but repos should inline it into their own
# `dev` [script] recipe.
#
# Override the malkuth binary location with $MALKUTH_BIN or $MALKUTH_ROOT.

# Resolve the malkuth binary path (env → PATH → sibling layout).
malkuth_bin := if env("MALKUTH_BIN", "") != "" {
    env("MALKUTH_BIN", "")
} else if env("MALKUTH_ROOT", "") != "" {
    env("MALKUTH_ROOT", "") + if os_family() == "windows" { "/target/release/malkuth.exe" } else { "/target/release/malkuth" }
} else if which("malkuth") != "" {
    "malkuth"
} else {
    "../malkuth/target/release/" + if os_family() == "windows" { "malkuth.exe" } else { "malkuth" }
}

# Always-valid sibling-repo fallback path (used when malkuth_bin resolves to a
# bare name that isn't actually on the script's PATH at runtime).
malkuth_bin_rel := "../malkuth/target/release/" + if os_family() == "windows" { "malkuth.exe" } else { "malkuth" }

[script]
dev-watch *ARGS:
    set -eu
    # Ensure MSYS /usr/bin is on PATH (just spawns bash.exe without /etc/profile,
    # so /usr/bin may be absent — breaks uname/cargo/etc.). Bash builtin only.
    case ":$PATH:" in
      *":/usr/bin:"*) ;;
      *) PATH="/usr/bin:$PATH" ;;
    esac
    # Split args at `--`: everything before = watch paths, after = command.
    watch_paths=()
    cmd=()
    found_sep=0
    for arg in {{ARGS}}; do
      if [ "$arg" = "--" ]; then
        found_sep=1
        continue
      fi
      if [ "$found_sep" = "1" ]; then
        cmd+=("$arg")
      else
        watch_paths+=("$arg")
      fi
    done
    if [ "${#cmd[@]}" -eq 0 ]; then
      echo "usage: just dev-watch <watch-paths...> -- <command...>" >&2
      echo "  e.g.  just dev-watch src -- cargo run" >&2
      exit 1
    fi
    # Resolve malkuth: prefer {{malkuth_bin}} (just-resolved), verify with bash
    # builtins only (no uname/which), fall back to sibling-repo release path.
    malkuth="{{malkuth_bin}}"
    if ! command -v "$malkuth" >/dev/null 2>&1 && [ ! -f "$malkuth" ]; then
      malkuth="{{ malkuth_bin_rel }}"
    fi
    if [ ! -f "$malkuth" ]; then
      echo "[dev-watch] malkuth not found." >&2
      echo "[dev-watch] Build it: cd ../malkuth && cargo build --release --features cli" >&2
      echo "[dev-watch] Or set: export MALKUTH_BIN=/path/to/malkuth" >&2
      exit 1
    fi
    # Build --watch flags.
    watch_flags=()
    for p in "${watch_paths[@]}"; do
      watch_flags+=(--watch "$p")
    done
    echo "[dev-watch] supervising: ${cmd[*]}"
    echo "[dev-watch] watching: ${watch_paths[*]}"
    exec "$malkuth" "${watch_flags[@]}" --drain-secs 2 -- "${cmd[@]}"

# Auto-register celestia-island git deps as cargo [patch] entries in
# ~/.cargo/config.toml. Runs `cargo metadata` to discover which celestia crates
# the current repo ACTUALLY depends on, then patches only those to their local
# checkouts. Scan directory: --scan-dir > $CELESTIA_ROOT > parent of cwd.
#
#   just register-patches                        scan + write
#   just register-patches --dry-run              preview only
#   just register-patches --scan-dir ../celestia pin scan directory
register-patches *ARGS='':
    {{ _devtools }} register-patches {{ARGS}}

# ── Git worktree management ───────────────────────────────────────────────────
#
# Linked working trees that share one .git directory. Each worktree is an
# independent checkout at ../<repo>-<name> with its own branch, index, and
# files — but shares objects/refs with the main repo. Rationale:
#
# 1. Different PRs need different Cargo.toml [patch] entries (A→B#feature vs
#    A→B#master), and you cannot express that in a single working directory.
# 2. In a monorepo-with-many-sibling-repos setup (celestia-island), each
#    worktree gets its own topological patch graph.
# 3. No rebuild storms: switching a worktree is `cd`, not `git checkout`.
#
# Recipes:
#   just worktree-create pr-42 dev     checkout ../repo-pr-42 from dev
#   just worktree-list                  list all worktrees for this repo
#   just worktree-remove pr-42          delete worktree + prune stale refs

# Create a new worktree at ../<repo>-<name> from <base> (branch or commit).
# After creation registers cargo patches for the new directory.
worktree-create name base='dev':
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$(basename "$(git rev-parse --show-toplevel)")
    target="../${repo}-${name}"
    if [ -d "$target" ]; then
      echo "worktree already exists: $target"
      exit 0
    fi
    # Ensure the base branch exists locally (fetch if only on remote).
    git rev-parse --verify "refs/heads/${base}" >/dev/null 2>&1 || \
      git rev-parse --verify "refs/remotes/origin/${base}" >/dev/null 2>&1 || \
      { echo "branch '${base}' not found locally or on origin"; exit 1; }
    echo "creating worktree ${target} from ${base}"
    git worktree add -b "${name}" "${target}" "origin/${base}" 2>/dev/null || \
      git worktree add "${target}" "${base}"
    echo "worktree ready: ${target}"

# List all worktrees for this repo.
worktree-list:
    git worktree list

# Remove a worktree and prune its metadata.
worktree-remove name:
    #!/usr/bin/env bash
    set -euo pipefail
    repo=$(basename "$(git rev-parse --show-toplevel)")
    target="../${repo}-${name}"
    if [ ! -d "$target" ]; then
      echo "worktree not found: $target"
      exit 1
    fi
    git worktree remove "$target" --force
    git worktree prune
    echo "removed worktree: $target"

# ── WSL2 dev environment ──────────────────────────────────────────────────────
#
# The celestia ecosystem shares ONE persistent WSL2 distro (default
# "celestia-dev") provisioned with rust + just + docker, so the global cargo
# registry cache is reused across aris / kei / entelecheia / shittim-chest
# instead of re-fetching every dependency per repo. Same model as Docker
# Desktop's managed WSL2 backend.
#
#   just wsl-ensure              create/verify (idempotent — safe to re-run)
#   just wsl-ensure --reset      destroy + recreate (clears ALL caches)
#   just wsl-ensure --no-docker  skip docker (use Docker Desktop integration)
#   just wsl-ensure --force      re-provision even if distro already exists
wsl-ensure *ARGS='':
    {{ _devtools }} wsl-ensure {{ARGS}}

# Run a command inside the celestia-dev distro (cwd = this repo's root).
# Example: just wsl-run cargo build --release
wsl-run *CMD='echo "usage: just wsl-run <command>"':
    wsl -d celestia-dev --cd "{{ justfile_directory() }}" -- bash -lc "{{ CMD }}"

# Drop into an interactive shell in the distro at the repo root.
wsl-shell:
    wsl -d celestia-dev --cd "{{ justfile_directory() }}"

# ── QEMU cross-architecture testing ──────────────────────────────────────────
#
# Ensure QEMU system emulators are installed for multi-arch testing
# (aarch64, x86_64, riscv64). Cross-platform: detects Windows QEMU, Linux
# apt/conda, macOS Homebrew — no root password required when possible.
#
#   just qemu-ensure              check/install all archs
#   just qemu-ensure --check      report status only (no install)
#   just qemu-ensure aarch64      ensure only aarch64
qemu-ensure *ARGS='':
    {{ _devtools }} qemu-ensure {{ARGS}}

# ── Environment preflight ────────────────────────────────────────────────────
#
# Verify required dev tools are present BEFORE a facility (pglite, build,
# cross-compile, …) actually needs them, so the user gets one actionable
# hint ("missing node → run just wsl-ensure") instead of a stack trace.
#
#   just preflight                 check ALL known tools
#   just preflight node npm        check only node + npm
#   just preflight -- --quiet      exit code only (for scripts)
preflight *REQS='':
    {{ _devtools }} preflight {{REQS}}

# ── Shared temporary PGlite ──────────────────────────────────────────────────
#
# Spawn a real embedded Postgres (PGlite WASM + pgvector) over the PG wire
# protocol, shared between entelecheia and shittim-chest for tests / mock
# backends. Zero Docker / system-postgres; data is in-memory unless --persist.
#
#   just pglite start              spawn, print DATABASE_URL (auto free port)
#   just pglite start --port 5433  pin a port
#   just pglite start --persist ./pgdata   keep data across restarts
#   just pglite stop               stop the recorded instance
#   just pglite status             is one running? prints JSON
pglite *ARGS='':
    {{ _devtools }} pglite {{ARGS}}

# ── npx precompiled distribution ─────────────────────────────────────────────
#
# Turn a cross-compiled Rust binary into a publishable npm package so users run
# it via `npx @celestia-island/<pkg>` with no Rust toolchain required. The
# generator writes a two-layer layout under ./dist: a root launcher
# (@scope/pkg) with optionalDependencies on per-platform subpackages, plus one
# subpackage per target carrying only that binary.
#
# Local dry-run (verify the staged packages without publishing):
#
#   just npm-dist shirabe                                        # reassemble root from existing dist/
#   just npm-dist shirabe 0.1.0 path/to/shirabe x86_64-unknown-linux-gnu
#
# Per-platform subpackages are staged from CI (one matrix job per target),
# then a final job reassembles the root. Actual `npm publish` runs in CI; this
# recipe only stages + `npm pack --dry-run` so you can inspect the tarball.
[script]
npm-dist name version='' binary='' target='':
    set -euo pipefail
    # Assemble the npm-dist CLI args. just params are not bash vars, so we test
    # the substituted strings directly rather than relying on ${var:+}.
    base=(--name "{{name}}" --out-dir dist)
    [ -n "{{version}}" ] && base+=(--version "{{version}}")
    if [ -n "{{binary}}" ]; then
      if [ -z "{{target}}" ]; then
        echo "npm-dist: --target required when --binary given" >&2
        exit 1
      fi
      base+=(--binary "{{binary}}" --rust-target "{{target}}")
    fi
    {{ _devtools }} npm-dist "${base[@]}"
    cd dist && npm pack --dry-run

# Publish the staged packages to npm (requires NPM_TOKEN in the environment).
# Intended for CI; prompts interactively for the OTP if 2FA is on.
# Override the tag with NPM_DIST_TAG (default: latest).
[script]
npm-release *PKGS='':
    set -euo pipefail
    cd dist
    tag="${NPM_DIST_TAG:-latest}"
    if [ $# -eq 0 ]; then
      npm publish --access public --tag "$tag"
    else
      for pkg in "$@"; do
        npm publish "$pkg" --access public --tag "$tag"
      done
    fi
