<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>為 Celestia 生態服務的共享構建與開發工具腳本</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

[English](../en/README.md) ·
[简体中文](../zhs/README.md) ·
**繁體中文** ·
[日本語](../ja/README.md) ·
[한국어](../ko/README.md) ·
[Français](../fr/README.md) ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## 簡介

`celestia-devtools` 是一套面向 Celestia 生態的共享構建與開發工具 Python 工具集。它從 `arona/scripts/` 中抽取而來，將開發工具與各個 crate 解耦，並透過 justfile 被 `entelecheia`、`shittim-chest`、`evernight` 等倉庫複用。

它提供 cargo 快取管理（`cache-guard`）、Markdown 格式化（`format-markdown`）、離線建置相依性預取（`prefetch`）、交叉編譯前置檢查（`check-cross-deps`）以及 crate 定位工具（`locate`）。

> 目前仍在開發中，指令與 recipe 未來可能發生變化。

## 快速開始

```bash
# 以可編輯模式安裝（用於開發）
pip install -e .

# 或從 git 安裝
pip install git+https://github.com/celestia-island/celestia-devtools.git

# 統一 CLI
celestia-devtools cache-guard .        # 管理 cargo target/ 磁碟佔用
celestia-devtools format-markdown .    # 格式化並檢查 Markdown 檔案
celestia-devtools prefetch .           # 為離線建置預取相依套件
celestia-devtools check-cross-deps     # 檢查交叉編譯前置條件
celestia-devtools locate               # 定位 celestia-island crate 檢出路徑
```

## justfile 整合

在倉庫根目錄執行 `celestia-devtools init`，即可將 `common.just` 作為 `celestia-devtools.just` 提交至倉庫中；隨後在 justfile 頂部匯入一次：

```just
import "./celestia-devtools.just"
```

在新檢出的倉庫中，`just ensure` 會引導安裝本套件，`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate` 等 recipe 隨即可用。所有 recipe 皆可被覆寫——完整列表見所引入的檔案。

## 授權條款

採用 [Synthetic Source License (SySL), Version 1.0](../../LICENSE) 授權。
