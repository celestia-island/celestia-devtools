<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Scripts de build y herramientas de desarrollo compartidas para el ecosistema Celestia</strong></p>

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
[Français](../fr/README.md) ·
**Español** ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## Introducción

`celestia-devtools` es un conjunto de herramientas Python de scripts de build y herramientas de desarrollo compartidas para el ecosistema Celestia. Extraído de `arona/scripts/`, desacopla las herramientas de desarrollo de los crates individuales y es consumido por `entelecheia`, `shittim-chest`, `evernight` y otros repositorios mediante justfile.

Proporciona gestión de caché de cargo (`cache-guard`), formateo de Markdown (`format-markdown`), preconfiguración de dependencias para builds sin conexión (`prefetch`), comprobación de requisitos de cross-compilación (`check-cross-deps`) y utilidades de localización de crates (`locate`).

> Aún en desarrollo; los comandos y recetas pueden cambiar en el futuro.

## Inicio rápido

```bash
# Instalar en modo editable (para desarrollo)
pip install -e .

# O instalar desde git
pip install git+https://github.com/celestia-island/celestia-devtools.git

# CLI unificado
celestia-devtools cache-guard .        # gestionar el uso de disco de cargo target/
celestia-devtools format-markdown .    # formatear y revisar archivos Markdown
celestia-devtools prefetch .           # preconfigurar dependencias para builds sin conexión
celestia-devtools check-cross-deps     # comprobar requisitos de cross-compilación
celestia-devtools locate               # localizar un checkout de crate de celestia-island
```

## Integración con justfile

Ejecuta `celestia-devtools init` en un repositorio para incorporar `common.just` como un `celestia-devtools.just` versionado y luego impórtalo una vez al inicio de tu justfile:

```just
import "./celestia-devtools.just"
```

En un checkout nuevo, `just ensure` prepara el paquete y recetas como `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check` y `locate` quedan disponibles. Todas las recetas se pueden sobrescribir — consulta el archivo incorporado para la lista completa.

## Licencia

Bajo la licencia [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
