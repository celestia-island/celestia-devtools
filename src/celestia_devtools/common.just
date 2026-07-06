# celestia-devtools common.just — vendored build recipes.
#
# This file is COPIED into your repo by `celestia-devtools init`.
# Commit it to git so every clone gets it — no package install needed
# to parse the justfile, only to run the recipes.
#
# Refresh with:  celestia-devtools init --force
# Version: 0.1.0
#
# All recipes and variables are overridable — redefine anything after the
# `import` line in your justfile to customize it.
#
# This file is intentionally free of `set` directives (shell, windows-shell,
# unstable) so it composes cleanly with each repo's own header.

# ── Tool resolution ───────────────────────────────────────────────────────────

python_cmd := if which("python3") != "" { "python3" } else { "python" }

# Resolve the celestia-devtools CLI: installed entry point, or module fallback.
_devtools := if which("celestia-devtools") != "" { "celestia-devtools" } else { python_cmd + " -m celestia_devtools" }

# ── Bootstrap ─────────────────────────────────────────────────────────────────

# Ensure celestia-devtools is installed. Run once on a fresh checkout.
# Override the install source with $CELESTIA_DEVTOOLS_INSTALL (e.g. a local path).
ensure:
    @command -v celestia-devtools >/dev/null 2>&1 || \
        {{python_cmd}} -c "import celestia_devtools" 2>/dev/null || \
        pip3 install "${CELESTIA_DEVTOOLS_INSTALL:-git+https://github.com/celestia-island/celestia-devtools.git}"

# ── Build helper ──────────────────────────────────────────────────────────────

# Shared build dispatcher: runs a prerequisite, then picks dev vs release cmd.
#
#   just _build ":" "cargo build" "cargo build --release" {{FLAGS}}
#   just _build "just cache-guard" "cargo build" "cargo build --release" --dev
#
# Flags (consumed from *FLAGS):
#   --dev    use the dev command instead of release
#   --clean  cargo clean before building
_build always_pre dcmd rcmd *FLAGS='':
    #!/usr/bin/env bash
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
