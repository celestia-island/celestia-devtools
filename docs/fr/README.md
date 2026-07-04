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

Exécutez `celestia-devtools init` dans un dépôt pour intégrer `common.just` sous la forme d'un `celestia-devtools.just` versionné, puis importez-le une fois en haut de votre justfile :

```just
import "./celestia-devtools.just"
```

Sur un nouveau checkout, `just ensure` amorce le paquet et des recettes telles que `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check` et `locate` deviennent disponibles. Toutes les recettes sont surchargeables — consultez le fichier intégré pour la liste complète.

## Licence

Sous licence [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
