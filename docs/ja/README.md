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

`celestia-devtools` は、Celestia エコシステム向けの共有ビルド・開発ツールスクリプトをまとめた Python ツールキットです。`arona/scripts/` から切り出したもので、開発ツールを個別の crate から切り離し、`entelecheia`、`shittim-chest`、`evernight` などのリポジトリから justfile 経由で利用されます。

cargo キャッシュ管理（`cache-guard`）、Markdown フォーマット（`format-markdown`）、オフラインビルド用依存関係の事前取得（`prefetch`）、クロスコンパイルの前提チェック（`check-cross-deps`）、crate の位置特定（`locate`）を提供します。

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

リポジトリで `celestia-devtools init` を実行すると、`common.just` をコミット対象の `celestia-devtools.just` として取り込みます。その後、justfile の先頭付近で一度だけインポートします：

```just
import "./celestia-devtools.just"
```

新しくチェックアウトした環境では、`just ensure` がパッケージを導入し、`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate` などの recipe が使えるようになります。すべての recipe は上書き可能です。完全な一覧は取り込んだファイルを参照してください。

## ライセンス

[Synthetic Source License (SySL), Version 1.0](../../LICENSE) の下でライセンスされます。
