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

`celestia-devtools` is a shared Python toolkit of build and devtool scripts for the Celestia ecosystem. Extracted from `arona/scripts/`, it decouples devtooling from individual crates and is consumed by `entelecheia`, `shittim-chest`, `evernight`, and other repos via justfile.

It provides cargo cache management (`cache-guard`), Markdown formatting (`format-markdown`), offline-build dependency pre-staging (`prefetch`), cross-compilation prerequisite checks (`check-cross-deps`), and crate-location utilities (`locate`).

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
```

## justfile integration

Run `celestia-devtools init` in a repo to vendor `common.just` as a committed `celestia-devtools.just`, then import it once near the top of your justfile:

```just
import "./celestia-devtools.just"
```

On a fresh checkout, `just ensure` bootstraps the package and recipes such as `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, and `locate` become available. All recipes are overridable — see the vendored file for the full list.

## License

Licensed under the [Synthetic Source License (SySL), Version 1.0](./LICENSE).
