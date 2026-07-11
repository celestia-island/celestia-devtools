<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Scripts de build et d'outils de développement partagés pour l'écosystème Celestia</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

[English](../en/README.md) ·
[简体中文](../zhs/README.md) ·
[繁體中文](../zht/README.md) ·
[日本語](../ja/README.md) ·
[한국어](../ko/README.md) ·
**Français** ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## Introduction

`celestia-devtools` est une boîte à outils Python de scripts de build et d'outils de développement partagés pour l'écosystème Celestia. Elle découple les outils de développement des crates individuels et est consommée par `entelecheia`, `shittim-chest`, `evernight` et d'autres dépôts via justfile.

Elle fournit la gestion du cache cargo (`cache-guard`), le formatage Markdown (`format-markdown`), la pré-stabilisation des dépendances pour les builds hors ligne (`prefetch`), la vérification des prérequis de cross-compilation (`check-cross-deps`) et des utilitaires génériques de localisation de crate sibling (`locate`).

> Toujours en développement ; les commandes et recettes peuvent changer à l'avenir.

## Démarrage rapide

```bash
# Installation en mode éditable (pour le développement)
pip install -e .

# Ou installation depuis git
pip install git+https://github.com/celestia-island/celestia-devtools.git

# CLI unifié
celestia-devtools cache-guard .        # gérer l'utilisation disque de cargo target/
celestia-devtools format-markdown .    # formater et vérifier les fichiers Markdown
celestia-devtools prefetch .           # pré-stabiliser les dépendances pour les builds hors ligne
celestia-devtools check-cross-deps     # vérifier les prérequis de cross-compilation
celestia-devtools locate               # localiser un checkout de crate celestia-island
```

## Intégration justfile

Les recettes partagées vivent dans `common.just` et sont **mises en place à la demande** dans le répertoire `.just/` ignoré par git de chaque dépôt — jamais commitées, donc elles ne dérivent ni ne se dupliquent entre dépôts (fini la copie par dépôt façon `gradlew`).

Exécutez `celestia-devtools init` dans un dépôt. Il met en place `.just/celestia-devtools.just`, ajoute `/.just/` à `.gitignore`, installe le hook commit-msg et affiche la ligne d'import. Ajoutez près du haut de votre justfile :

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash ; requis sur Windows
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = optionnel : parsable avant la mise en place
```

Ajoutez ensuite une recette `fetch` afin que chacun puisse (re)mettre en place le fichier partagé :

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?` (import optionnel) permet à un nouveau checkout d'analyser la justfile avant la mise en place, de sorte que vos propres recettes fonctionnent toujours. Exécutez `just fetch` (ou `celestia-devtools init`) pour mettre en place les recettes partagées — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch`, etc. Toutes les recettes sont surchargeables — redéfinissez-les après la ligne `import?`.

**Note Windows :** si `bash` pointe vers WSL (`just windows-shell-check` signale un détournement), ajoutez le `usr/bin` de Git en tête du PATH :
```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## Licence

Sous licence [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
