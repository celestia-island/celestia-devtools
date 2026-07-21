# Convention de message de commit

Tous les dépôts Celestia Island appliquent une convention de message de commit basée sur [gitmoji](https://gitmoji.dev). Ce document définit les règles, les exemptions et les mécanismes d'application.

## Règle

Chaque sujet de commit (première ligne du message de commit) doit respecter le format suivant :

```
<gitmoji> <Résumé en anglais commençant par une majuscule.>
```

| Exigence | Exemple (réussi) | Exemple (échoué) |
|---|---|---|
| Commence par un gitmoji | `🐛 Fix...` | `Fix...` |
| Pas de préfixe Conventional Commits | `🐛 Fix...` | `🐛 fix: ...` |
| Première lettre après l'emoji en majuscule | `🐛 Fix...` | `🐛 fix...` |
| Se termine par un point | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| Anglais uniquement | `🐛 Fix the parser crash.` | `🐛 修复解析器崩溃。` |
| Descriptif (pas seulement une version/texte de remplissage) | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## Exemptions

Les types de commit suivants sont automatiquement exemptés :

- **Commits de fusion** : `Merge branch 'foo'` / `Merge pull request #42`
- **Commits de revert** : `Revert "..."`

Pour ignorer la vérification pour un seul commit :

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## Application

La convention est appliquée à trois niveaux :

1. **Hook commit-msg local** — installé automatiquement par `celestia-devtools init`. Bloque les messages invalides au moment de `git commit`. Pour réinstaller ou actualiser :

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # écraser un hook personnalisé
   just commit-msg-hook-install             # via la recette just
   ```

2. **Vérification CI (PR)** — le workflow réutilisable `commit-msg-lint.yml` valide chaque commit dans une pull request. Ajoutez ce job au `checks.yml` de votre dépôt :

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **Protection de branche** — configurez la vérification de statut `commit-msg` comme requise sur la branche `master` dans les paramètres de votre dépôt GitHub.

## Dépôts de bots

Les dépôts entièrement pilotés par des processus automatisés (par exemple, `provider-registry`) doivent ignorer le hook local en exécutant `celestia-devtools init --no-hooks` et ignorer la vérification CI en ne l'incluant pas. Les commits individuels de bots peuvent également être exemptés via `CELESTIA_COMMIT_MSG_SKIP=1`.

## Migration

Les commits existants dans l'historique du dépôt ne sont pas validés rétroactivement — cette convention s'applique uniquement aux nouveaux commits. Si la branche master de votre dépôt utilise actuellement des préfixes Conventional Commits (`feat:`, `fix:`, etc.), vous pouvez effectuer une transition progressive en adoptant le format gitmoji pour tout nouveau travail.

## Référence rapide

| Gitmoji | Signification | Quand l'utiliser |
|---|---|---|
| ✨ | Sparkles | Nouvelle fonctionnalité |
| 🐛 | Bug | Correction de bug |
| 📝 | Memo | Documentation |
| ♻️ | Recycle | Refactorisation |
| 🚀 | Rocket | Déploiement / publication |
| 🔒 | Lock | Sécurité |
| ⬆️ | Arrow up | Mise à jour des dépendances |
| ⬇️ | Arrow down | Rétrogradation des dépendances |
| 🔧 | Wrench | Modifications de configuration |
| ✅ | Check mark | Tests |
| 🚧 | Construction | En cours |
| 🎨 | Art | Format / structure |
| 💚 | Green heart | Correction CI |
| 🔥 | Fire | Suppression de code |
| 🚑 | Ambulance | Correctif urgent |
| 📄 | Page | Licence |
| 🔨 | Hammer | Scripts de développement |
| 🌐 | Globe | Internationalisation |
| 💡 | Bulb | Commentaires |

Voir [gitmoji.dev](https://gitmoji.dev) pour la liste complète.
