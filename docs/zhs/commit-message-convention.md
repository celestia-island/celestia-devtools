# 提交信息规范

所有 Celestia Island 仓库均强制采用基于 [gitmoji](https://gitmoji.dev) 的提交信息规范。本文档定义了规则、豁免条件和执行机制。

## 规则

每条提交的主题行（提交信息的第一行）必须遵循以下格式：

```
<gitmoji> <大写英文摘要。>
```

| 要求 | 示例（通过） | 示例（失败） |
|---|---|---|
| 以 gitmoji 开头 | `🐛 Fix...` | `Fix...` |
| 无 Conventional Commits 前缀 | `🐛 Fix...` | `🐛 fix: ...` |
| emoji 后首字母大写 | `🐛 Fix...` | `🐛 fix...` |
| 以句号结尾 | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| 仅限英文 | `🐛 Fix the parser crash.` | `🐛 修复解析器崩溃。` |
| 描述性内容（非仅版本号/填充文字） | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## 豁免

以下提交类型自动豁免：

- **合并提交**：`Merge branch 'foo'` / `Merge pull request #42`
- **还原提交**：`Revert "..."`

对单个提交跳过检查：

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## 强制执行

该规范在三个层面强制执行：

1. **本地 commit-msg 钩子** — 由 `celestia-devtools init` 自动安装。在 `git commit` 时阻止无效信息。重新安装或刷新：

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # 覆盖自定义钩子
   just commit-msg-hook-install             # 通过 just recipe
   ```

2. **CI 检查（PR）** — 可复用工作流 `commit-msg-lint.yml` 验证拉取请求中的每条提交。将此作业添加到仓库的 `checks.yml`：

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **分支保护** — 在 GitHub 仓库设置中将 `commit-msg` 状态检查配置为 `master` 分支的必需检查。

## 机器人仓库

完全由自动化流程驱动的仓库（例如 `provider-registry`）应通过运行 `celestia-devtools init --no-hooks` 跳过本地钩子，并通过不包含 CI 检查来跳过 CI。单个机器人提交也可通过 `CELESTIA_COMMIT_MSG_SKIP=1` 豁免。

## 迁移

仓库历史中的现有提交不会进行追溯验证——此规范仅适用于新提交。如果您的仓库的 master 分支当前使用 Conventional Commits 前缀（`feat:`、`fix:` 等），您可以逐步过渡，对所有新工作采用 gitmoji 格式。

## 快速参考

| Gitmoji | 含义 | 使用时机 |
|---|---|---|
| ✨ | Sparkles | 新功能 |
| 🐛 | Bug | 修复 Bug |
| 📝 | Memo | 文档 |
| ♻️ | Recycle | 重构 |
| 🚀 | Rocket | 部署/发布 |
| 🔒 | Lock | 安全 |
| ⬆️ | Arrow up | 升级依赖 |
| ⬇️ | Arrow down | 降级依赖 |
| 🔧 | Wrench | 配置变更 |
| ✅ | Check mark | 测试 |
| 🚧 | Construction | 进行中 |
| 🎨 | Art | 格式化/结构 |
| 💚 | Green heart | CI 修复 |
| 🔥 | Fire | 删除代码 |
| 🚑 | Ambulance | 热修复 |
| 📄 | Page | 许可证 |
| 🔨 | Hammer | 开发脚本 |
| 🌐 | Globe | 国际化 |
| 💡 | Bulb | 注释 |

完整列表请参见 [gitmoji.dev](https://gitmoji.dev)。
