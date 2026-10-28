<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Shared build and devtool scripts for the Celestia ecosystem</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](./LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

**English** ·
[简体中文](./docs/zhs/README.md) ·
[繁體中文](./docs/zht/README.md) ·
[日本語](./docs/ja/README.md) ·
[한국어](./docs/ko/README.md) ·
[Français](./docs/fr/README.md) ·
[Español](./docs/es/README.md) ·
[Русский](./docs/ru/README.md) ·
[العربية](./docs/ar/README.md)

</div>

## Introduction

`celestia-devtools` is a shared Python toolkit of build and devtool scripts for the Celestia ecosystem. It decouples devtooling from individual crates and is consumed by `entelecheia`, `shittim-chest`, `evernight`, and other repos via justfile.

It provides cargo cache management (`cache-guard`), Markdown formatting (`format-markdown`), offline-build dependency pre-staging (`prefetch`), cross-compilation prerequisite checks (`check-cross-deps`), and generic sibling-crate location utilities (`locate`).

> Still in development; commands and recipes may change in the future.

## Quick Start

```bash
# Install (editable, for development)
pip install -e .

# Or install from git
pip install git+https://github.com/celestia-island/celestia-devtools.git

# Unified CLI
celestia-devtools cache-guard .        # manage cargo target/ disk usage
celestia-devtools format-markdown .    # format + lint Markdown files
celestia-devtools prefetch .           # pre-stage deps for offline builds
celestia-devtools check-cross-deps     # check cross-compilation prerequisites
celestia-devtools locate               # locate a celestia-island crate checkout
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # lint a commit message
celestia-devtools hook install         # install the org commit-msg hook
```

## justfile integration

The shared recipes live in `common.just` and are **staged on demand** into each repo's gitignored `.just/` directory — never committed, so they never drift or duplicate across repos (no `gradlew`-style per-repo copy).

Run `celestia-devtools init` in a repo. It stages `.just/celestia-devtools.just`, adds `/.just/` to `.gitignore`, installs the commit-msg hook, and prints the import line. Add near the top of your justfile:

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash; required on Windows
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = optional: parses before staging
```

Then add a `fetch` recipe so anyone can (re)stage the shared file:

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?` (optional import) lets a fresh clone parse the justfile before staging, so your own recipes always work. Run `just fetch` (or `celestia-devtools init`) to stage the shared recipes — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch`, `devenv`, etc. All recipes are overridable — redefine any after the `import?` line.

**Windows note:** if `bash` resolves to WSL (`just windows-shell-check` reports a hijack), prepend Git's `usr/bin` to PATH:

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## Dev-mode gateway environment

Production fronts its services at `*.celestia.world` hosts (`.cw` is only the fleet's short alias), and cloud-built clients bake those gateways in — the shittim-chest desktop defaults to `https://auth.celestia.ac.cn`. Local iteration does **not** reproduce the domain shape (no hosts files, no reverse proxy, no certificates): instead, a `DEBUG_`-prefixed variable set replaces the gateway addresses, read **only in debug builds** (`cfg!(debug_assertions)` in Rust), so dev builds talk to the local stack while shipped binaries cannot be redirected. The tradeoff is inherent: environment injection is per-process and local — it cannot serve public-internet traffic the way the production vhosts do.

`devenv` computes the set for a run profile and launches commands with it injected (caller-set variables are never overwritten):

```bash
just devenv print                     # export lines, native profile
just devenv print --profile mock      # the mock-stack addresses
just devenv run -- cargo tauri dev    # desktop dev build → local chest
```

| Variable | Read by | `native` | `daemon` | `mock` |
| --- | --- | --- | --- | --- |
| `DEBUG_CHEST_URL` (`SHITTIM_CHEST_URL`) | chest desktop (debug builds) | `:3000` | `:8400` | `:8428` |
| `DEBUG_SCEPTER_URL` / `_WS_URL` (`ENTELECHEIA_SCEPTER_URL` / `_WS_URL`) | chest core outbound | `:8424` | `:8410` | `:8424` |
| `DEBUG_ARONA_URL` | arona-facing clients | `:8420` | `:8405` | `:8429` |
| `DEBUG_EVERNIGHT_URL` (`ARONA_EVERNIGHT_URL`) | evernight bridge clients | `:8443` | `:8443` | `:8443` |

The parenthesised names are the pre-existing per-repo variables, injected with identical values so unmodified checkouts work today. Custom ports live in a `devenv.toml` (cwd or the celestia config dir) with `[profiles.<name>]` tables.

## Per-app port allocation

Two tauri debug apps on one machine fight over the same two sockets: the WebView2 CDP port (`9222` — first app to boot owns it, the other's devtools silently die) and the vite devUrl server (`5173` — the second app's webview loads the **first** app's frontend; seen live between WoWSP and shittim-chest). `ports` gives every app deterministic-but-unique ports, coordinated through a per-user registry:

```bash
just ports acquire --app shittim-chest --kind vite   # bare port, script-friendly
just ports env --app shittim-chest                   # CELESTIA_VITE_PORT / CELESTIA_CDP_PORT + the WebView2 CDP line
just ports list                                      # live claims
just ports release --app shittim-chest
```

Same app → same ports across runs (URLs and CDP targets feel permanent); different apps → different ports; claims owned by dead pids are garbage-collected automatically. Consumers: pass the allocated port to `vite-serve --port`, and for WebView2 shells export `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` (the `env` output carries it) so each app's CDP lands on its own port.

## Driving a desktop debug build (`app-drive`)

The desktop debug loop used to be hand-written CDP scripts per investigation: launch the Tauri debug binary with `--remote-debugging-port`, poll `/json/list`, evaluate JavaScript, capture screenshots, click through settings, record the boot-chrome lifecycle, and hope the window and its child processes got closed. `app-drive` standardizes that channel: one command group, one JSON protocol, and a close path that only ever touches a process it launched and can prove it launched. `close` and `--replace` are the only subcommands that terminate anything, and `launch` never blind-kills a process it cannot verify.

The CDP port comes from the ports facility above, so a debug shell never collides with another app's:

```console
$ celestia-devtools app-drive launch target/debug/shittim_chest_tauri
{"ok": true, "protocol": 2, "cmd": "launch", "app": "exe-3f9a1c7d2e4b", "pid": 41992, "cdp_port": 9222, "page": {"id": "CC44...", "url": "http://localhost:5173/"}, ...}
```

`launch` sets `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<cdp port>` (pre-existing flags are preserved), waits for its target page, records the process identity, and writes a per-app state file under `%LOCALAPPDATA%/celestia/app-drive` (the XDG twin on POSIX). The app key is `--app` when given, otherwise a hash of the resolved executable path, so two same-stem executables never share a state file.

Scope: **CDP only.** `app-drive` allocates and claims the CDP port; it does not allocate a vite port and does not start or verify the frontend server. A compiled debug build keeps the `devUrl` baked in at build time, so exporting a vite port it never reads would only pretend to prevent cross-wiring. Serve the frontend with `vite-serve`/`ports` and point the build at it at build time. `--env` refuses to override `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` / `CELESTIA_CDP_PORT` (app-drive derives those from the allocated port).

### Safe close: the identity contract

`close` and `--replace` are the only subcommands that terminate a process, and they have to justify it. `launch` records the resolved full executable path, the process creation token (Windows `GetProcessTimes` FILETIME, Linux `/proc/<pid>` starttime) and the token kind. Both are re-read and compared before killing - once on entry and again immediately before the kill. When any piece is missing, unreadable or different, the command **fails closed**: exit 1, state file and port claim kept, and no signal sent to the process. A state file from an older schema is refused as `state-outdated`; recover that wedged handle with `close --app <key> --discard-state` (drops the state and CDP claim without touching the process - close the old process manually), and pass `--discard-state` to `launch` instead of refusing to start. When the recorded pid cannot be confirmed alive (exit status unavailable AND identity unreadable, e.g. access-denied), `close` fails closed and keeps the state; `--force` is the explicit unverified break-glass - it kills the recorded pid without verification, or drops an unconfirmable state without touching it. The Windows recorded path comes from Python's `resolve()` while the live path comes from `QueryFullProcessImageNameW`, so a symlinked or 8.3-short launch path can differ textually: comparison also tries `realpath` and still fails closed when equality cannot be proven (relaunch through the canonical path). Platforms without `/proc` (macOS) cannot expose the identity, so `close`/`--replace` fail closed there. `--replace` also refuses when the recorded executable differs from the one being launched (`replace-exe-mismatch`).

A launch state file (schema 2) looks like this:

```json
{"schema": 2, "protocol": 2, "app": "exe-3f9a1c7d2e4b", "pid": 41992, "exe": "C:/work/chest/target/debug/shittim_chest_tauri.exe", "create_token": "133670000000000000", "create_kind": "windows-filetime", "cdp_port": 9222, "cwd": "C:/work/chest", "launched_at": "2026-10-28T15:00:00Z", "log_dir": "...", "command": ["..."]}
```

Launch hygiene: `--env` and the log directory are validated before a port is acquired, so a bad value cannot leak a CLI-owned claim. If writing the state or the port claim fails, `launch` terminates the child it just spawned through its recorded identity; if the child had already exited it sends no kill at all, and if no identity was ever recorded it refuses to blind-kill a bare pid (a reused pid could belong to anything) - it keeps a closable state file and reports the pid instead. Ctrl+C before the spawn leaves nothing to clean; after it, the same cleanup path runs, prints the envelope and exits 130.

### Protocol

Every subcommand prints exactly one ASCII JSON object to stdout and exits `0` on success, `1` on a runtime failure, or `2` on a usage error; progress and diagnostics go to stderr, so `| jq` stays clean. Envelopes use `ensure_ascii`, so a gbk/cp936 stdout cannot crash on out-of-codepage page text:

```json
{"ok": true, "protocol": 2, "cmd": "status", "port": 9222, "target": {"id": "...", "url": "..."}, "state": {"boot_chrome": "claimed", "has_bar": false, "tauri": true}}
{"ok": false, "protocol": 2, "cmd": "wait", "error": {"code": "timeout", "message": "condition not satisfied within 10s"}}
```

`protocol` is the envelope version and `error.code` is the stable discriminant (`cdp-unreachable`, `cdp-protocol`, `js-exception`, `selector-not-found`, `identity-unavailable`, `pid-mismatch`, `timeout`, ...). `--pretty` indents the envelope for humans; `--raw` (eval only) prints the bare ASCII JSON value instead of the envelope. The `state` object behind `status` / `wait` / `watch` is the desktop debug contract: `boot_chrome` (the `data-hk-boot-chrome` attribute: `active` / `claimed` / absent), `has_bar` / `bar_driver` (the `#hk-boot-chrome` element), `degraded`, `ready_state`, `title`, `url`, `tauri` (whether `window.__TAURI__.core.invoke` exists), `body_text_head`, and friends.

| Command | What it does |
| --- | --- |
| `app-drive targets` | list the `/json/list` targets even when no page target exists (`--prefer-url` / `--target` select one) |
| `app-drive status` | selected target + boot-chrome/page state |
| `app-drive eval "JS"` | evaluate JS (promises awaited, result by value); `--raw` prints the bare value, `--out FILE` saves it |
| `app-drive shot out.png` | capture a PNG/JPEG screenshot |
| `app-drive click "CSS"` | click a selector (`element.click()`, or `--mouse` for real mouse events at the element center) |
| `app-drive wait --selector/--expr/--boot-chrome` | poll until a condition matches; `--timeout` is the condition deadline, `--shot FILE` captures on match |
| `app-drive watch --out DIR` | record state transitions and screenshots; `--reload` captures a fresh boot-chrome lifecycle |
| `app-drive launch <exe>` | allocate the CDP port, launch with the CDP environment, wait for the page |
| `app-drive close` | identity-verified graceful then forced close of the launched pid only; `--force` = explicit unverified break-glass, `--discard-state` drops a stale/outdated handle |

### Typical desktop verification loop

```console
$ celestia-devtools app-drive launch packages/app/tauri/target/debug/shittim_chest_tauri --app shittim-chest
$ celestia-devtools app-drive watch --app shittim-chest --reload --out _scratch/boot --stop-boot-chrome claimed
$ celestia-devtools app-drive shot --app shittim-chest _scratch/boot/final.png
$ celestia-devtools app-drive eval --app shittim-chest --out _scratch/report.json "window.__TAURI__.core.invoke('local_suite_report')"
$ celestia-devtools app-drive close --app shittim-chest
```

`watch` writes `timeline.jsonl` (one JSON sample per line), `watch.json` (transitions, screenshots, final state, stop reason) and `shot-NN-<ms>.png` on every change. It stops early with `--stop-boot-chrome claimed` or `--stop-selector CSS`, otherwise when `--duration` elapses.

### Attaching to a running app

`--port` attaches to any CDP endpoint directly (no launch state needed). Attach commands never allocate or claim a port, and `--app` without a launch state fails with `no-launch-state` instead of inventing one. `--prefer-url` picks between several page targets and `--target ID` pins an exact target on every attach command (status/eval/shot/click/wait/watch). `--timeout` bounds connect/response waits (and is the condition deadline for `wait`).

That is also the documented path outside Windows: WebView2 exposes CDP, but WebKitGTK on Linux and WKWebView on macOS do not, so serve the built webui (`vite-serve`) and drive it in a Chromium/Edge started with `--remote-debugging-port=N`. The client itself is stdlib-only (a minimal RFC 6455 implementation in `env/cdp.py` that validates server frames instead of trusting them, and refuses `wss://` rather than pretending to own a TLS trust policy): the channel needs text frames over loopback, so no new runtime dependency is added to a package consumed by the whole fleet, and the subset is pinned by unit tests plus a fake CDP endpoint. A second instance of the same WebView2 app needs `--env WEBVIEW2_USER_DATA_FOLDER=<dir>`; sharing the running instance's profile makes WebView2 fail to create the webview (`0x8007139F`).

## Commit Message Governance

`celestia-devtools` enforces the org gitmoji convention — every commit and PR merge must start with a gitmoji, use uppercase English, and end with a period (`.`). See `celestia-devtools commit-msg-lint check --help` for the full rule set.

### Why?

`gh pr merge --squash --subject "..."` bypasses ALL validation — the subject you type becomes the merge commit directly. Without a gate, bad messages slip through.

### Local protection (recommended)

After `pip install celestia-devtools`, run `celestia-devtools init` in your repo. This installs a `commit-msg` hook that rejects bad messages at `git commit` time.

For PR merge protection, add a shell function to `~/.bashrc`:

```bash
gh() { celestia-devtools gh "$@"; }
```

After `source ~/.bashrc`, `gh pr merge` validates the subject before forwarding to the real `gh` binary. All other commands (`gh pr list`, `gh issue`, `gh repo`, etc.) pass through untouched. The real binary at `/usr/bin/gh` is never modified.

For CI or non-interactive shells (where `.bashrc` is not sourced), use the proxy directly:

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### CI protection (optional, opt-in)

To add automatic PR validation via GitHub Actions:

```bash
celestia-devtools init --with-workflows
```

This generates `.github/workflows/commit-msg-lint.yml`. Commit and push it.

For enforcement, enable branch protection on your default branch via GitHub Settings → Branches → "Require status checks to pass before merging" → choose `lint-commits / Lint commit messages`.

> **Note:** private repos need GitHub Team ($4/mo) for branch protection. Public repos get it free.

### Pinned reusable-workflow refs

Callers generated by `init --with-workflows` reference the reusable workflows at
a **pinned baseline tag**, not `@master`:

```yaml
jobs:
  lint-commits:
    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@v2026.10
    with:
      devtools-ref: v2026.10
```

Why: ~46 repositories consume devtools gates through one ref. A floating
`@master` means a single bad commit redefines gate semantics for the whole
family at once — and it happened (a verify-versions regression reported
`ok:true` against a null baseline, running vacuously on half the fleet).

Semantics:

- **The pin freezes both layers.** The `uses:` ref fixes which workflow file
  GitHub loads, and `devtools-ref` tells the callee's runtime bootstrap which
  devtools snapshot to install — pinning the YAML while the tool floats from
  master would be cosmetic. `p0-gate` is the deliberate exception: its P0
  ledger is live policy state and stays on master even from a pinned caller.
- **The pin is a CalVer rolling baseline tag** (`v2026.10`). A new tag is cut
  on a verified-green master SHA each migration wave (or sooner when a gate
  change lands); tags are never moved. Advancing = regenerate the caller, not
  mutate the tag.
- **Upgrading is regeneration.** `celestia-devtools init --with-workflows
  --force` (add `--gate <name>` for opt-in callers) rewrites the callers at
  the current pin. Never hand-edit the ref.
- **Escape hatch.** `celestia-devtools init --with-workflows --ref master`
  writes the floating form for break-glass consumption of an unmerged fix.
- **Migration status is auditable.** `celestia-ci-audit` reports every
  `@master` caller as a `devtools-ref-not-pinned` warning (not an error, while
  the fleet migrates), and `celestia-devtools fleet-audit` prints a census:
  `devtools refs (pin @v2026.10): pinned=N master=M stale=K none=J`.

One honest limit: on the self-hosted farm the preinstalled CLI wins over the
bootstrap fetch (a forced reinstall there has proven unreliable), so the
`devtools-ref` input governs the git-fetch channels; the farm image remains an
ops-controlled supply line that the mirror sync advances.

### All commands

| Command | Purpose |
|---------|---------|
| `celestia-devtools init` | Stage justfiles + install commit-msg hook |
| `celestia-devtools init --with-workflows` | Also generate CI workflow (opt-in) |
| `celestia-devtools commit-msg-lint check --subject "..."` | Validate a message string |
| `celestia-devtools pr-merge --subject "..." --squash` | Validate then merge (standalone) |
| `celestia-devtools gh pr merge --subject "..."` | Transparent gh proxy |
| `celestia-devtools gate` | Run the local CI gate (see below) |
| `celestia-devtools gate credential-scan` | Sweep `.git/config` + source trees for literal secrets |
| `celestia-devtools p0-gate` | Fail a repo covered by an unresolved P0 (see below) |
| `celestia-devtools py-style check .` | Check Python sources against the custom style rules (see below) |
| `celestia-devtools py-style fix .` | Apply the safe auto-fixes in place (`--dry-run` previews) |
| `celestia-devtools cpp-style check .` | Check C++ sources against the custom style rules (see below) |
| `celestia-devtools release-notes` | Generate categorized GitHub release notes for a tag (see below) |

## Style Checkers

`py-style` and `cpp-style` enforce the org's custom Python/C++ layout rules
(keyword-argument spacing, single-line statements, block blank lines, import
layout, naming, ...). Each module ships its rule catalog as data —
`py-style list-rules` / `cpp-style list-rules` is the authoritative spec
(id, source clause, severity, auto-fixability, judgment and exceptions).

```bash
celestia-devtools py-style check src tests     # report violations (exit 1)
celestia-devtools py-style fix src tests       # safe auto-fixes, AST-verified
celestia-devtools py-style list-rules          # the rule catalog
celestia-devtools cpp-style check src          # C++ subset (8 lexical checks)
```

The division of labour is deliberate:

- **The handwritten stdlib checker is the authority for layout.** ruff's
  formatter and its preview-only whitespace rules (`E251`, `E3xx`, `W391`, ...)
  encode the opposite conventions, so `ruff format` is not part of a repo's
  style gate once it adopts these rules.
- **ruff keeps the stable semantic checks** (`F`, `E7`, `W1`, `E4`, ...) with an
  explicit, commented `ignore` list for anything that contradicts the style.
  Preview rules are never enabled: they drift between releases, and the one that
  covers keyword `=` spacing (`E251`) argues for the opposite of this style.
- **Auto-fix is conservative**: only `autofix` rules run, every rewrite is
  checked for AST equality and unchanged string tokens, and `check` never writes.

This repository itself is fully migrated to these conventions: `gate python`
runs `ruff check → py-style → pytest → lint-commits`, CI runs the same blocking
`py-style` step, and `ruff format` is deliberately absent (see the style-policy
block in `pyproject.toml`). File-level exemptions stay exceptional and every one
carries a reason: the CLI's lazy command dispatcher (`core/cli.py`) is the only
code exemption, and seven test modules exempt `imports-at-top` because
`sys.path.insert` must precede the package import when tests run against a
source checkout.

Repos that adopt the conventions opt in with the reusable workflow (sub-minute,
stdlib-only, safe on hosted runners):

```yaml
jobs:
  py-style:
    uses: celestia-island/celestia-devtools/.github/workflows/style-lint.yml@v2026.10
    with:
      tool: py-style          # or cpp-style
      paths: .
      devtools-ref: v2026.10  # pin the runtime fetch with the workflow file
```

## Local Gate

`celestia-devtools gate` reproduces the CI checklist in one local command, so
"local verification == CI" before opening a PR. It encodes three design
elements: **modes**, **dependency ordering**, and a **concurrency budget**.

```bash
celestia-devtools gate              # auto-detect the repo's mode
celestia-devtools gate rust         # cargo fmt/clippy/test/deny + lint-commits
celestia-devtools gate web          # pnpm install/lint/build/test + lint-commits
celestia-devtools gate python       # ruff check + py-style + pytest + lint-commits
celestia-devtools gate all          # every mode detectable in the repo
celestia-devtools gate rust --list  # print the resolved step graph (dry-run)
celestia-devtools gate web --jobs 2 # cap parallel execution at 2 workers
celestia-devtools gate rust --coverage  # enable cargo tarpaulin coverage
```

- **Modes** — no argument auto-detects from the repo (`Cargo.toml` → rust,
  `package.json`/`pnpm-workspace.yaml` → web, `pyproject.toml` → python).
- **Dependency ordering** — each mode is a DAG (e.g. rust:
  `fmt → clippy → test → deny → coverage → lint-commits`). A step runs only
  after its dependencies pass; a failed step aborts its dependents.
- **Concurrency budget** — `--jobs N` (default: CPU count capped at 4) bounds
  parallel execution of independent steps via a generic topological scheduler.

`gate rust` / `gate web` also run a **credential scan** pre-step: files changed
vs `origin/master` are grepped for `password|secret|token|api_key|BEGIN …
PRIVATE KEY`, failing only on literal non-placeholder values (`CHANGE_ME`,
`<your-password>`, `test-password`, `sk-xxx`, `192.0.2.x`, … are whitelisted).
Two secret shapes carry no credential word and are matched directly: a token
embedded in a URL's userinfo (the `.git/config` remote form
`https://oauth2:gho_…@github.com/org/repo.git`) and a bare provider token
(`gho_…` / `ghp_…` / `github_pat_…`).

`celestia-devtools gate credential-scan` runs that same classifier over the
surfaces no org workflow visits: each checkout's own `.git/config` and the
source trees of the `langyo/*` business repos (`easy-hydro-*`). It reports —
never rewrites a checkout.

```bash
celestia-devtools gate credential-scan --repo arona --repo easy-hydro-erp
celestia-devtools gate credential-scan --all-langyo --scan-root /mnt/codespace
celestia-devtools gate credential-scan --git-config-only   # remotes only
celestia-devtools gate credential-scan --fail-on-skip      # an unreadable path fails
```

Paths the sweep cannot read (a root-owned `lost+found`, a broken symlink, a
socket, a file the user cannot open) are never dropped silently: each is printed
with its reason, and `--fail-on-skip` turns an incomplete sweep into a failure.
`--git-config-only` and `--no-git-config` are mutually exclusive.

A hit is a **violation** only when the value is a literal secret: a provider
token shape (`gho_…`, `github_pat_…`, `glpat-…`, `sk-…`, `xox…`, `AKIA…`,
`AIza…`, `cli_…`), a URL userinfo that is credential-shaped, or a
credential-named key (`*_SECRET` / `*_TOKEN` / `*_PASSWORD` / `api_key` / …)
whose value is a quoted literal, carries a digit, or is a long mixed blob.
Identifiers, call expressions, control words (`write`), dependency names
(`jsonwebtoken = "^10"`) and placeholders are reported, not failed — a scanner
that flags `token = path.strip()` gets ignored, and an ignored scanner protects
nothing.

`celestia-devtools gate precheck` runs the safety diagnostics from the workspace
postmortem follow-ups: NFS mount-point warnings (a `findmnt` scan for `_worktree`
paths at `rm -rf` risk) and a large-download heuristic scan (`hf_hub_download` /
`huggingface_hub` / `modelscope` without `HF_HUB_DISABLE_XET=1` / `no_proxy`
hints). Both are advisory warnings, never failures.

Exit codes: `0` all steps passed, `1` any step failed, `2` usage error.

## P0 Ledger Gate

A finding that lives only in a report nobody is obliged to read costs nothing to
bypass. `celestia-devtools p0-gate` turns unresolved P0 findings into a red
check: the ledger (`src/celestia_devtools/lint/p0_ledger.toml`, shipped with the
package) lists each finding with an `id`, `title`, `severity`, `scope` (the
repos it covers), `status`, `opened_at`, `closed_by`, and a one-line `evidence`
for the current status.

```bash
celestia-devtools p0-gate --repo entelecheia   # exit 1 while an open P0 covers it
celestia-devtools p0-gate --list               # whole ledger, grouped by status
celestia-devtools p0-gate --repo entelecheia --ack P0-A
celestia-devtools p0-gate --repo arona --pr-body-file pr-body.md
```

- **Blocking** — any entry with `status = "open"` whose `scope` matches the repo
  (fnmatch patterns such as `easy-hydro-*` are supported) exits non-zero and
  prints `id` + `title` + `evidence`. Repo names are normalized, so
  `owner/repo`, a full URL, `repo.git` or different casing all resolve to the
  same check.
- **Escape hatch** — an acknowledgment has to be written by someone: `--ack ID`,
  `$CELESTIA_P0_ACK`, or a `P0-ACK: <id> [reason]` line in the PR body
  (`--pr-body` / `--pr-body-file` / `$CELESTIA_P0_PR_BODY`). Acknowledged entries
  are echoed to stderr with their source and stop blocking; unacknowledged ones
  still fail. The gate never guesses, and never passes silently.
- **Closing** — a `closed` entry must name the PR that closed it (`closed_by`);
  an open entry must leave it empty.
- **Fail-closed** — a missing, unreadable, unparsable, schema-invalid, or empty
  ledger exits `2` instead of passing. Exit codes: `0` clear, `1` blocked by an
  open P0, `2` usage/ledger error.

The ledger ships inside the package, so any repo can adopt the check by adding a
workflow step. Override it per checkout with `--ledger`, `$CELESTIA_P0_LEDGER`,
or a `p0-ledger.toml` at the repo root.

### Adopting the gate in a repo

Call the org reusable workflow (pinned like every caller — see "Pinned
reusable-workflow refs" above). The ledger read stays on `@master` **by
design**: the P0 ledger is live policy state, so a pin that froze it would gate
against an outdated list of open findings.

```yaml
name: P0 Ledger Gate
on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review]
concurrency:
  group: p0-gate-${{ github.ref }}
  cancel-in-progress: true
jobs:
  p0-gate:
    uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@v2026.10
```

`edited` is required: adding a `P0-ACK:` line to the PR body has to re-run the
check. An open P0 then blocks the repo's PRs until someone closes the ledger
entry or writes the acknowledgment.

## Release Notes

Every org repo squashes its PRs and lints their titles, so master history is
already a changelog — `celestia-devtools release-notes` turns it into the
standard release body: every merged PR since the previous tag, grouped by the
leading gitmoji of its subject.

```bash
celestia-devtools release-notes --repo celestia-island/shun --tag v0.2.16   # print
celestia-devtools release-notes --tag v0.2.16 --apply                       # upload (draft if new)
```

The previous tag auto-resolves as the highest semver tag below `--tag`
(`prefix-vX.Y.Z` shapes count; floating tags like `res-latest` never match).
Bundle several versions into one release with `--previous-tag v0.1.0`; list
the full history with `--no-previous`. Commits without a `(#N)` squash
reference (direct pushes, reverts) are skipped.

Repos get this automatically on tag push through the shared reusable workflow
— one job in the tag-triggered release workflow, after the artifacts upload:

```yaml
release-notes:
  needs: release              # whatever job creates/uploads to the release
  permissions:
    contents: write
  uses: celestia-island/celestia-devtools/.github/workflows/release-notes.yml@v2026.10
  with:
    devtools-ref: v2026.10
```

The job patches the body onto the release the build job created (keeping its
draft/published state); when no release exists it creates one as a draft so a
human can curate before publishing. Because the categorization keys off the
gitmoji, the notes are only as good as the PR titles — which is exactly the
discipline the commit-msg governance already enforces.

## Deploy (production targets)

`env/*` commands own **development** machines (foreground supervision,
embedded PG, WSL/QEMU bring-up). The `deploy` group owns **production
targets**: a fresh single-host box goes from zero to a systemd-resident
service, then gets verified, backed up, upgraded. Deploy commands never hold a
foreground process and never touch mock stacks; cross-host deployment scripts
live in `deploy/` and only here.

Current surface:

```console
$ celestia-devtools deploy doctor     # read-only: proxy / python deps / system tools
```

`doctor` reports, never installs (auto-install of missing system tools, when
it arrives, stays behind an explicit consent flag). Proxy detection is a
five-level cascade (env vars and git's own http.proxy -> loopback listeners ->
default gateway -> DNS suffixes -> WPAD) with credentials redacted in every report line, and `NO_PROXY`
always covers loopback plus RFC1918/ULA ranges so a same-LAN database never
routes through an egress proxy.

Boxes whose Python is older than 3.11 are rescued by `pyshim` (shipped in this
package): it warns, asks consent, installs the **newest** available Python
(uv -> python-build-standalone -> system packages, in that order), brings up
`/opt/celestia-devtools/venv`, and re-execs the original command line. The
shim itself is held to Python 3.6 syntax with zero third-party imports -- its
tests enforce that grammar floor.

The bare `deploy` wizard runs today and stops loudly (exit 2) at the first
stage that belongs to a later slice — `artifact`, `verify`, `backup`,
`restore`, `upgrade`, `rollback`, `status`, `secrets`, `uninstall` land with
their own slices and fail loudly until then.

## License


Licensed under the [Synthetic Source License (SySL), Version 1.0](./LICENSE).
