# 提交訊息規範

所有 Celestia Island 倉庫均強制採用基於 [gitmoji](https://gitmoji.dev) 的提交訊息規範。本文檔定義了規則、豁免條件和執行機制。

## 規則

每條提交的主題行（提交訊息的第一行）必須遵循以下格式：

```
<gitmoji> <大寫英文摘要。>
```

| 要求 | 範例（通過） | 範例（失敗） |
|---|---|---|
| 以 gitmoji 開頭 | `🐛 Fix...` | `Fix...` |
| 無 Conventional Commits 前綴 | `🐛 Fix...` | `🐛 fix: ...` |
| emoji 後首字母大寫 | `🐛 Fix...` | `🐛 fix...` |
| 以句號結尾 | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| 僅限英文 | `🐛 Fix the parser crash.` | `🐛 修復解析器崩潰。` |
| 描述性內容（非僅版本號/填充文字） | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## 豁免

以下提交類型自動豁免：

- **合併提交**：`Merge branch 'foo'` / `Merge pull request #42`
- **還原提交**：`Revert "..."`

對單個提交跳過檢查：

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## 強制執行

該規範在三個層面強制執行：

1. **本地 commit-msg 鉤子** — 由 `celestia-devtools init` 自動安裝。在 `git commit` 時阻止無效訊息。重新安裝或重新整理：

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # 覆蓋自訂鉤子
   just commit-msg-hook-install             # 透過 just recipe
   ```

2. **CI 檢查（PR）** — 可複用工作流程 `commit-msg-lint.yml` 驗證拉取請求中的每條提交。將此作業新增到倉庫的 `checks.yml`：

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **分支保護** — 在 GitHub 倉庫設定中將 `commit-msg` 狀態檢查配置為 `master` 分支的必需檢查。

## 機器人倉庫

完全由自動化流程驅動的倉庫（例如 `provider-registry`）應透過執行 `celestia-devtools init --no-hooks` 跳過本地鉤子，並透過不包含 CI 檢查來跳過 CI。單個機器人提交也可透過 `CELESTIA_COMMIT_MSG_SKIP=1` 豁免。

## 遷移

倉庫歷史中的現有提交不會進行追溯驗證——此規範僅適用於新提交。如果您的倉庫的 master 分支目前使用 Conventional Commits 前綴（`feat:`、`fix:` 等），您可以逐步過渡，對所有新工作採用 gitmoji 格式。

## 快速參考

| Gitmoji | 含義 | 使用時機 |
|---|---|---|
| ✨ | Sparkles | 新功能 |
| 🐛 | Bug | 修復 Bug |
| 📝 | Memo | 文件 |
| ♻️ | Recycle | 重構 |
| 🚀 | Rocket | 部署/發佈 |
| 🔒 | Lock | 安全 |
| ⬆️ | Arrow up | 升級依賴 |
| ⬇️ | Arrow down | 降級依賴 |
| 🔧 | Wrench | 配置變更 |
| ✅ | Check mark | 測試 |
| 🚧 | Construction | 進行中 |
| 🎨 | Art | 格式化/結構 |
| 💚 | Green heart | CI 修復 |
| 🔥 | Fire | 刪除程式碼 |
| 🚑 | Ambulance | 熱修復 |
| 📄 | Page | 許可證 |
| 🔨 | Hammer | 開發腳本 |
| 🌐 | Globe | 國際化 |
| 💡 | Bulb | 註解 |

完整列表請參見 [gitmoji.dev](https://gitmoji.dev)。
