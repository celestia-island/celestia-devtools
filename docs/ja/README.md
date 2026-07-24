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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # コミットメッセージを検査
celestia-devtools hook install         # 組織の commit-msg フックをインストール
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

## コミットメッセージガバナンス

`celestia-devtools` は組織の gitmoji 規約を強制します——すべてのコミットと PR マージは gitmoji で始まり、英大文字を使用し、ピリオド（`.`）で終わる必要があります。完全なルールセットは `celestia-devtools commit-msg-lint check --help` を参照してください。

### なぜ必要か？

`gh pr merge --squash --subject "..."` はすべての検証をバイパスします——入力した subject がそのままマージコミットになります。ゲートがないと、不適切なメッセージがすり抜けてしまいます。

### ローカル保護（推奨）

`pip install celestia-devtools` の後、リポジトリで `celestia-devtools init` を実行します。これにより、`git commit` 時に不適切なメッセージを拒否する `commit-msg` フックがインストールされます。

PR マージ保護のために、`~/.bashrc` にシェル関数を追加します：

```bash
gh() { celestia-devtools gh "$@"; }
```

`source ~/.bashrc` の後、`gh pr merge` は実際の `gh` バイナリに転送する前に subject を検証します。その他のコマンド（`gh pr list`、`gh issue`、`gh repo` など）はそのまま通過します。`/usr/bin/gh` の実際のバイナリは決して変更されません。

CI や非インタラクティブシェル（`.bashrc` が読み込まれない環境）では、プロキシを直接使用します：

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### CI 保護（オプション、オプトイン）

GitHub Actions による自動 PR 検証を追加するには：

```bash
celestia-devtools init --with-workflows
```

これにより `.github/workflows/commit-msg-lint.yml` が生成されます。コミットしてプッシュしてください。

強制するには、デフォルトブランチでブランチ保護を有効にします：GitHub Settings → Branches → "Require status checks to pass before merging" → `lint-commits / Lint commit messages` を選択します。

> **注意：** プライベートリポジトリでブランチ保護を使用するには GitHub Team（$4/月）が必要です。パブリックリポジトリは無料です。

### すべてのコマンド

| コマンド | 目的 |
|---------|---------|
| `celestia-devtools init` | justfile のステージング + commit-msg フックのインストール |
| `celestia-devtools init --with-workflows` | CI ワークフローも生成（オプトイン） |
| `celestia-devtools commit-msg-lint check --subject "..."` | メッセージ文字列の検証 |
| `celestia-devtools pr-merge --subject "..." --squash` | 検証後にマージ（スタンドアロン） |
| `celestia-devtools gh pr merge --subject "..."` | 透過的 gh プロキシ |

## ライセンス

[Synthetic Source License (SySL), Version 1.0](../../LICENSE) の下でライセンスされます。
