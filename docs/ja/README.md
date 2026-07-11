<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Celestia エコシステム向けの共有ビルド・開発ツールスクリプト</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

[English](../en/README.md) ·
[简体中文](../zhs/README.md) ·
[繁體中文](../zht/README.md) ·
**日本語** ·
[한국어](../ko/README.md) ·
[Français](../fr/README.md) ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## 概要

`celestia-devtools` は、Celestia エコシステム向けの共有ビルド・開発ツールスクリプトをまとめた Python ツールキットです。開発ツールを個別の crate から切り離し、`entelecheia`、`shittim-chest`、`evernight` などのリポジトリから justfile 経由で利用されます。

cargo キャッシュ管理（`cache-guard`）、Markdown フォーマット（`format-markdown`）、オフラインビルド用依存関係の事前取得（`prefetch`）、クロスコンパイルの前提チェック（`check-cross-deps`）、汎用 sibling crate の位置特定（`locate`）を提供します。

> 現在も開発中であり、コマンドや recipe は今後変更される可能性があります。

## クイックスタート

```bash
# 開発用に編集可能モードでインストール
pip install -e .

# または git からインストール
pip install git+https://github.com/celestia-island/celestia-devtools.git

# 統合 CLI
celestia-devtools cache-guard .        # cargo target/ のディスク使用量を管理
celestia-devtools format-markdown .    # Markdown ファイルのフォーマットと検査
celestia-devtools prefetch .           # オフラインビルド用に依存関係を事前取得
celestia-devtools check-cross-deps     # クロスコンパイルの前提条件を確認
celestia-devtools locate               # celestia-island crate のチェックアウトを特定
```

## justfile 連携

共有 recipe は `common.just` にあり、各リポジトリの gitignore 済み `.just/` ディレクトリに**オンデマンドで** stage されます。コミット対象ではないため、リポジトリ間でのズレや重複が発生しません（`gradlew` 式のリポジトリごとのコピーは廃止しました）。

リポジトリで `celestia-devtools init` を実行すると、`.just/celestia-devtools.just` を stage し、`.gitignore` に `/.just/` を追記し、commit-msg フックをインストールして、import 行を表示します。justfile の先頭付近に以下を追加します：

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash。Windows では必須
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = オプション：stage 前でも解析可能
```

さらに `fetch` recipe を追加し、誰でも共有ファイルを（再）stage できるようにします：

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?`（オプション import）により、stage 前の新規チェックアウトでも justfile を解析でき、自身の recipe は常に動作します。`just fetch`（または `celestia-devtools init`）を実行して共有 recipe を stage してください——`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate`、`pglite`、`wsl-ensure`、`dev-watch` など。すべての recipe は上書き可能です——`import?` 行の後に再定義してください。

**Windows の注意：** `bash` が WSL に解決される場合（`just windows-shell-check` が乗っ取りを報告）、Git の `usr/bin` を PATH の先頭に追加します：

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## ライセンス

[Synthetic Source License (SySL), Version 1.0](../../LICENSE) の下でライセンスされます。
