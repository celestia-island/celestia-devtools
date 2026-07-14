# celestia-devtools — 项目状态与计划 (PLAN)

> 刷新于 2026-07-14。跨仓共享开发任务（just 配方）。

## 1. 项目概述

- **名称**：`celestia-devtools`
- **简介**：celestia-island 各仓共享的 `just` 配方与脚本（import `./.just/celestia-devtools.just`），提供 `register-patches` / `fetch-siblings` / `upstream-sync` 等统一动作。
- **远程仓库**：https://github.com/celestia-island/celestia-devtools.git
- **技术栈**：just / pwsh / bash / cargo
- **类别**：devtools

## 2. 当前状态

- **当前分支**：`dev`
- **工作区**：干净
- **最近提交时间**：2026-07-13
- **最近提交**：`⬆️ Refresh the checkout and setup-python action pins.`
- **本地领先 `origin/dev`**：0

## 3. 未提交改动

无

## 4. 近期进展

- `⬆️ Refresh the checkout and setup-python action pins.`
- `Merge remote-tracking branch 'origin/master' into dev`
- `✨ Add shared upstream-sync and sibling-repo linking recipes.`（最近新功能）

## 5. 后续计划

1. **`register-patches` 实现**：把 sibling-crate `path = "../..."` 的 `[patch]` 写到用户 `~/.cargo/config.toml`，而**不**写入各仓的 `.cargo/config.toml`（这是 entelecheia 本轮 [entelecheia/PLAN.md §6](./../entelecheia/PLAN.md) 强约束）。
2. **`fetch-siblings`**：把相邻 sibling 仓以 `--depth 1` 拉取到约定目录。
3. **`upstream-sync`**：定时把 celestia 各仓对 upstream 同步基线，便于跨仓版本协调。
4. **`sibling-repo linking`**：跨仓 workspace 关联（已加，待稳定）。

## 6. 跨仓依赖

- 被 entelecheia / scriptum / shittim-chest / kou / 等 30 仓的 justfile 引用。
- 本仓不参与 `path = "../..."` 的 [patch] 链（自身是 devtools，不被 [patch]）。

---

## 既有详细计划（存档）

所有 `just` 配方在仓库根 `justfile`；本文件只承载"当前态 → 后续计划"两部分。
