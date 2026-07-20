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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # 检查提交信息
celestia-devtools hook install         # 安装组织的 commit-msg 钩子
```

## justfile 集成

共享 recipe 存放在 `common.just`，**按需** staged 到各仓库 gitignore 的 `.just/` 目录——绝不提交进 git，因此不会在各仓库间漂移或重复（摒弃 `gradlew` 式的逐仓库副本）。

在仓库中运行 `celestia-devtools init`：它会 stage `.just/celestia-devtools.just`、向 `.gitignore` 追加 `/.just/`、安装 commit-msg 钩子，并打印导入行。在 justfile 顶部添加：

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash；Windows 必需
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = 可选：stage 前也能解析
```

再加一个 `fetch` recipe，让任何人都能（重新）stage 共享文件：

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?`（可选导入）让全新检出的仓库在 stage 前也能解析 justfile，你自己的 recipe 始终可用。运行 `just fetch`（或 `celestia-devtools init`）来 stage 共享 recipe——`cache-guard`、`fmt-markdown`、`prefetch`、`cross-check`、`locate`、`pglite`、`wsl-ensure`、`dev-watch` 等。所有 recipe 均可覆盖——在 `import?` 行之后重新定义即可。

**Windows 注意：** 若 `bash` 被解析到 WSL（`just windows-shell-check` 报告劫持），请将 Git 的 `usr/bin` 前置到 PATH：

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## 提交信息治理

`celestia-devtools` 强制遵循组织的 gitmoji 规范——每个提交和 PR 合并必须以 gitmoji 开头，使用英文大写，并以句号（`.`）结尾。完整规则集请参见 `celestia-devtools commit-msg-lint check --help`。

### 为什么？

`gh pr merge --squash --subject "..."` 绕过了所有校验——你输入的主题直接成为合并提交。没有把关，糟糕的信息就会溜进去。

### 本地防护（推荐）

`pip install celestia-devtools` 之后，在你的仓库中运行 `celestia-devtools init`。这会安装一个 `commit-msg` 钩子，在 `git commit` 时拒绝不合格的提交信息。

对于 PR 合并防护，向 `~/.bashrc` 添加一个 shell 函数：

```bash
gh() { celestia-devtools gh "$@"; }
```

执行 `source ~/.bashrc` 后，`gh pr merge` 会在转发到真正的 `gh` 二进制文件之前校验主题。所有其他命令（`gh pr list`、`gh issue`、`gh repo` 等）直接透传。`/usr/bin/gh` 的实际二进制文件不会被修改。

对于 CI 或非交互式 shell（不会加载 `.bashrc`），直接使用代理：

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### CI 防护（可选，需主动启用）

通过 GitHub Actions 添加自动 PR 校验：

```bash
celestia-devtools init --with-workflows
```

这会生成 `.github/workflows/commit-msg-lint.yml`。提交并推送它。

要强制执行，在默认分支上启用分支保护：GitHub Settings → Branches → "Require status checks to pass before merging" → 选择 `lint-commits / Lint commit messages`。

> **注意：** 私有仓库需要 GitHub Team（$4/月）才能使用分支保护。公开仓库免费。

### 所有命令

| 命令 | 用途 |
|---------|---------|
| `celestia-devtools init` | 部署 justfile + 安装 commit-msg 钩子 |
| `celestia-devtools init --with-workflows` | 同时生成 CI 工作流（需主动启用） |
| `celestia-devtools commit-msg-lint check --subject "..."` | 校验消息字符串 |
| `celestia-devtools pr-merge --subject "..." --squash` | 校验后合并（独立使用） |
| `celestia-devtools gh pr merge --subject "..."` | 透明 gh 代理 |

## 许可证

采用 [Synthetic Source License (SySL), Version 1.0](../../LICENSE) 授权。
