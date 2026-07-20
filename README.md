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

`import?` (optional import) lets a fresh clone parse the justfile before staging, so your own recipes always work. Run `just fetch` (or `celestia-devtools init`) to stage the shared recipes — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch`, etc. All recipes are overridable — redefine any after the `import?` line.

**Windows note:** if `bash` resolves to WSL (`just windows-shell-check` reports a hijack), prepend Git's `usr/bin` to PATH:

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

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

### All commands

| Command | Purpose |
|---------|---------|
| `celestia-devtools init` | Stage justfiles + install commit-msg hook |
| `celestia-devtools init --with-workflows` | Also generate CI workflow (opt-in) |
| `celestia-devtools commit-msg-lint check --subject "..."` | Validate a message string |
| `celestia-devtools pr-merge --subject "..." --squash` | Validate then merge (standalone) |
| `celestia-devtools gh pr merge --subject "..."` | Transparent gh proxy |

## License

Licensed under the [Synthetic Source License (SySL), Version 1.0](./LICENSE).
