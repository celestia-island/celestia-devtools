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

`celestia-devtools` 是一套面向 Celestia 生態的共享構建與開發工具 Python 工具集。將開發工具與各個 crate 解耦，並透過 justfile 被 `entelecheia`、`shittim-chest`、`evernight` 等倉庫複用。

它提供 cargo 快取管理（`cache-guard`）、Markdown 格式化（`format-markdown`）、離線建置相依性預取（`prefetch`）、交叉編譯前置檢查（`check-cross-deps`）以及通用同級 crate 定位工具（`locate`）。

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

共享 recipe 存放於 `common.just`，**按需** stage 到各倉庫 gitignore 的 `.just/` 目錄——絕不提交進 git，因此不會在各倉庫間漂移或重複（摒棄 `gradlew` 式的逐倉庫副本）。

在倉庫中執行 `celestia-devtools init`：它會 stage `.just/celestia-devtools.just`、向 `.gitignore` 追加 `/.just/`、安裝 commit-msg 鉤子，並列印匯入行。在 justfile 頂部加入：

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash；Windows 必需
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = 選用：stage 前也能解析
```

再加一個 `fetch` recipe，讓任何人都能（重新）stage 共享檔案：

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?`（選用匯入）讓全新檢出的倉庫在 stage 前也能解析 justfile，你自己的 recipe 始終可用。執行 `just fetch`（或 `celestia-devtools init`）來 stage 共享 recipe——`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate`、`pglite`、`wsl-ensure`、`dev-watch` 等。所有 recipe 皆可覆寫——在 `import?` 行之後重新定義即可。

**Windows 注意：** 若 `bash` 被解析到 WSL（`just windows-shell-check` 報告劫持），請將 Git 的 `usr/bin` 前置到 PATH：
```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## 授權條款

採用 [Synthetic Source License (SySL), Version 1.0](../../LICENSE) 授權。
