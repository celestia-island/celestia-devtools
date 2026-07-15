# celestia-devtools — 项目状态与计划 (PLAN)

> 刷新于 2026-07-15。跨仓共享开发任务（just 配方 + Python 工具）。

## 1. 项目概述

- **名称**：`celestia-devtools`
- **简介**：celestia-island 各仓共享的 `just` 配方与 Python CLI 脚本，提供 `register-patches` / `fetch-siblings` / `upstream-sync` 等统一动作。
- **远程仓库**：https://github.com/celestia-island/celestia-devtools.git
- **技术栈**：just / pwsh / bash / Python / cargo
- **类别**：devtools

## 2. 当前状态

- **当前分支**：`dev`
- **工作区**：有未提交改动（`register_patches.py` bugfix + PLAN.md 更新）
- **最近提交时间**：2026-07-13
- **本地领先 `origin/dev`**：0

## 3. 未提交改动

```
M PLAN.md
M src/celestia_devtools/repo/register_patches.py
```

## 4. 近期进展

- `✨ register-patches` Python 实现已落地（per-repo 模式 + legacy global 模式）
- `✨ upstream-sync` 和 `sibling-repo linking` 已加
- 当前 session：修复 `register-patches` bug（cargo metadata 0 deps 时误删 patch）

## 5. 后续计划

1. **`register-patches` 无网 fallback**：`cargo metadata` 失败时扫描兄弟目录直接生成 patch（不依赖网络缓存）
2. **`fetch-siblings`**：把相邻 sibling 仓以 `--depth 1` 拉取到约定目录
3. **`upstream-sync` 定时化**：CI cron job 定时对齐 upstream 基线
4. **克隆后自动化**：`init` 菜谱自动跑 `register-patches`（如果扫描到兄弟目录）

## 6. 跨仓依赖

- 被 entelecheia / scriptum / shittim-chest / kou 等 30 仓的 justfile 引用
- 本仓不参与 `path = "../..."` 的 `[patch]` 链
