# Commit Message Convention

All Celestia Island repositories enforce a commit-message convention based on [gitmoji](https://gitmoji.dev). This document defines the rules, exemptions, and enforcement mechanisms.

## Rule

Every commit subject (first line of the commit message) must follow this format:

```
<gitmoji> <Capitalized English summary.>
```

| Requirement | Example (pass) | Example (fail) |
|---|---|---|
| Starts with a gitmoji | `🐛 Fix...` | `Fix...` |
| No Conventional Commits prefix | `🐛 Fix...` | `🐛 fix: ...` |
| First letter after emoji is uppercase | `🐛 Fix...` | `🐛 fix...` |
| Ends with a period | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| English only | `🐛 Fix the parser crash.` | `🐛 修复解析器崩溃。` |
| Descriptive (no version-only / filler) | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## Exemptions

The following commit types are automatically exempt:

- **Merge commits**: `Merge branch 'foo'` / `Merge pull request #42`
- **Revert commits**: `Revert "..."`

To skip the check for a single commit:

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## Enforcement

The convention is enforced at three layers:

1. **Local commit-msg hook** — installed automatically by `celestia-devtools init`. Blocks invalid messages at `git commit` time. To reinstall or refresh:

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # overwrite a custom hook
   just commit-msg-hook-install             # via just recipe
   ```

2. **CI check (PRs)** — the reusable workflow `commit-msg-lint.yml` validates every commit in a pull request. Add this job to your repo's `checks.yml`:

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **Branch protection** — configure the `commit-msg` status check as required on the `master` branch in your GitHub repository settings.

## Bot repositories

Repositories that are driven entirely by automated processes (e.g., `provider-registry`) should skip the local hook by running `celestia-devtools init --no-hooks` and the CI check by not including it. Individual bot commits can also be exempted via `CELESTIA_COMMIT_MSG_SKIP=1`.

## Migration

Existing commits in the repository history are not retroactively validated — this convention applies to new commits only. If your repository's master branch currently uses Conventional Commits prefixes (`feat:`, `fix:`, etc.), you can transition gradually by adopting the gitmoji format for all new work.

## Quick reference

| Gitmoji | Meaning | When to use |
|---|---|---|
| ✨ | Sparkles | New feature |
| 🐛 | Bug | Fix a bug |
| 📝 | Memo | Documentation |
| ♻️ | Recycle | Refactor |
| 🚀 | Rocket | Deploy / release |
| 🔒 | Lock | Security |
| ⬆️ | Arrow up | Upgrade deps |
| ⬇️ | Arrow down | Downgrade deps |
| 🔧 | Wrench | Config changes |
| ✅ | Check mark | Tests |
| 🚧 | Construction | WIP |
| 🎨 | Art | Format / structure |
| 💚 | Green heart | CI fix |
| 🔥 | Fire | Remove code |
| 🚑 | Ambulance | Hotfix |
| 📄 | Page | License |
| 🔨 | Hammer | Dev scripts |
| 🌐 | Globe | i18n |
| 💡 | Bulb | Comments |

See [gitmoji.dev](https://gitmoji.dev) for the full list.
