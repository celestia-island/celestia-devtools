# Dépannage de la construction

Échecs de construction courants dans l'écosystème Celestia et comment les contourner.

## `aws-lc-sys` échoue avec "NASM failed" (Windows)

Plusieurs crates Rust (`aoba`, `noa`, `scriptum`, `tairitsu`, `arona`, et tout autre crate qui utilise `rustls` avec le fournisseur TLS par défaut) échouent sous Windows avec :

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### Cause

`aws-lc-sys` (le backend cryptographique par défaut de `rustls` depuis 0.23) compile les sources d'assemblage d'AWS-LC avec NASM. La construction échoue à l'étape d'assemblage — non pas parce que NASM est manquant (il est installé et dans le `PATH`), mais parce que l'invocation de NASM 3.x sur les fichiers `.asm` générés par AWS-LC ne se termine pas proprement sous la chaîne d'outils MSVC sur cet hôte.

### Solutions de contournement

1. **Compilez de manière croisée vers `*-linux-musl`** au lieu de compiler nativement sous Windows. La plupart de ces crates ciblent de toute façon des serveurs Linux, et les cibles musl construisent `aws-lc-sys` avec un assembleur Linux qui ne rencontre pas ce bug. Consultez le `justfile` / `.cargo/config.toml` de chaque crate pour la cible de compilation croisée configurée.

2. **Basculez le fournisseur TLS vers `ring`** (par crate, optionnel) en désactivant `aws_lc_rs` et en activant `ring` sur la dépendance `rustls` :

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   Ne faites cela que si le crate ne dépend pas d'un comportement spécifique à AWS-LC ; il s'agit d'un changement de dépendance et doit être validé par projet.

3. **Construisez dans WSL** (Ubuntu), où `aws-lc-sys` s'assemble proprement avec la chaîne d'outils système.

### Statut

Il s'agit d'un problème d'environnement/chaîne d'outils, pas d'un défaut de code spécifique au projet. Il est suivi ici afin que les contributeurs ne le poursuivent pas comme un bug de projet. Si vous corrigez la construction native Windows, veuillez mettre à jour cette note.
