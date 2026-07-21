# コミットメッセージ規約

すべての Celestia Island リポジトリは [gitmoji](https://gitmoji.dev) に基づくコミットメッセージ規約を適用しています。このドキュメントは、ルール、例外、および実施メカニズムを定義します。

## ルール

すべてのコミットの件名（コミットメッセージの最初の行）は以下の形式に従う必要があります：

```
<gitmoji> <大文字で始まる英語の概要。>
```

| 要件 | 例（合格） | 例（不合格） |
|---|---|---|
| gitmoji で始まる | `🐛 Fix...` | `Fix...` |
| Conventional Commits プレフィックスなし | `🐛 Fix...` | `🐛 fix: ...` |
| emoji 後の最初の文字は大文字 | `🐛 Fix...` | `🐛 fix...` |
| ピリオドで終わる | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| 英語のみ | `🐛 Fix the parser crash.` | `🐛 パーサークラッシュを修正。` |
| 説明的であること（バージョンのみ/埋め草ではない） | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## 例外

以下のコミットタイプは自動的に除外されます：

- **マージコミット**：`Merge branch 'foo'` / `Merge pull request #42`
- **revert コミット**：`Revert "..."`

単一のコミットでチェックをスキップするには：

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## 実施

この規約は 3 つのレイヤーで実施されます：

1. **ローカルの commit-msg フック** — `celestia-devtools init` によって自動的にインストールされます。`git commit` 時に無効なメッセージをブロックします。再インストールまたは更新：

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # カスタムフックを上書き
   just commit-msg-hook-install             # just recipe 経由
   ```

2. **CI チェック（PR）** — 再利用可能なワークフロー `commit-msg-lint.yml` がプルリクエスト内のすべてのコミットを検証します。このジョブをリポジトリの `checks.yml` に追加：

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **ブランチ保護** — GitHub リポジトリ設定で、`commit-msg` ステータスチェックを `master` ブランチの必須チェックとして設定します。

## ボットリポジトリ

完全に自動化プロセスによって駆動されるリポジトリ（例：`provider-registry`）は、`celestia-devtools init --no-hooks` を実行してローカルフックをスキップし、CI チェックを含めないことで CI をスキップする必要があります。個別のボットコミットも `CELESTIA_COMMIT_MSG_SKIP=1` で除外できます。

## 移行

リポジトリ履歴内の既存のコミットは遡及的に検証されません — この規約は新しいコミットにのみ適用されます。リポジトリの master ブランチが現在 Conventional Commits プレフィックス（`feat:`、`fix:` など）を使用している場合、すべての新しい作業に gitmoji 形式を採用することで段階的に移行できます。

## クイックリファレンス

| Gitmoji | 意味 | 使用タイミング |
|---|---|---|
| ✨ | Sparkles | 新機能 |
| 🐛 | Bug | バグ修正 |
| 📝 | Memo | ドキュメント |
| ♻️ | Recycle | リファクタリング |
| 🚀 | Rocket | デプロイ/リリース |
| 🔒 | Lock | セキュリティ |
| ⬆️ | Arrow up | 依存関係のアップグレード |
| ⬇️ | Arrow down | 依存関係のダウングレード |
| 🔧 | Wrench | 設定変更 |
| ✅ | Check mark | テスト |
| 🚧 | Construction | 作業中 |
| 🎨 | Art | フォーマット/構造 |
| 💚 | Green heart | CI 修正 |
| 🔥 | Fire | コード削除 |
| 🚑 | Ambulance | ホットフィックス |
| 📄 | Page | ライセンス |
| 🔨 | Hammer | 開発スクリプト |
| 🌐 | Globe | 国際化 |
| 💡 | Bulb | コメント |

完全なリストは [gitmoji.dev](https://gitmoji.dev) を参照してください。
