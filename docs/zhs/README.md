<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>面向 Celestia 生态的共享构建与开发工具脚本</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

[English](../en/README.md) ·
**简体中文** ·
[繁體中文](../zht/README.md) ·
[日本語](../ja/README.md) ·
[한국어](../ko/README.md) ·
[Français](../fr/README.md) ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## 简介

`celestia-devtools` 是一套面向 Celestia 生态的共享构建与开发工具 Python 工具集。将开发工具与各个 crate 解耦，并通过 justfile 被 `entelecheia`、`shittim-chest`、`evernight` 等仓库复用。

它提供 cargo 缓存管理（`cache-guard`）、Markdown 格式化（`format-markdown`）、离线构建依赖预取（`prefetch`）、交叉编译前置检查（`check-cross-deps`）以及通用同级 crate 定位工具（`locate`）。

> 目前仍在开发中，命令与 recipe 未来可能发生变化。

## 快速开始

```bash
# 以可编辑模式安装（用于开发）
pip install -e .

# 或从 git 安装
pip install git+https://github.com/celestia-island/celestia-devtools.git

# 统一 CLI
celestia-devtools cache-guard .        # 管理 cargo target/ 磁盘占用
celestia-devtools format-markdown .    # 格式化并检查 Markdown 文件
celestia-devtools prefetch .           # 为离线构建预取依赖
celestia-devtools check-cross-deps     # 检查交叉编译前置条件
celestia-devtools locate               # 定位 celestia-island crate 检出路径
```

## justfile 集成

在仓库根目录运行 `celestia-devtools init`，即可将 `common.just` 作为 `celestia-devtools.just` 提交到仓库中；随后在 justfile 顶部导入一次：

```just
import "./celestia-devtools.just"
```

在新检出的仓库中，`just ensure` 会引导安装本包，`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate` 等 recipe 随即可用。所有 recipe 均可被覆盖——完整列表见所引入的文件。

## 许可证

采用 [Synthetic Source License (SySL), Version 1.0](../../LICENSE) 授权。
