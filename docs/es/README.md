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

`celestia-devtools` es un conjunto de herramientas Python de scripts de build y herramientas de desarrollo compartidas para el ecosistema Celestia. Desacopla las herramientas de desarrollo de los crates individuales y es consumido por `entelecheia`, `shittim-chest`, `evernight` y otros repositorios mediante justfile.

Proporciona gestión de caché de cargo (`cache-guard`), formateo de Markdown (`format-markdown`), preconfiguración de dependencias para builds sin conexión (`prefetch`), comprobación de requisitos de cross-compilación (`check-cross-deps`) y utilidades genéricas de localización de crates hermanos (`locate`).

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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # revisar un mensaje de commit
celestia-devtools hook install         # instalar el hook commit-msg de la organización
```

## Integración con justfile

Las recetas compartidas viven en `common.just` y se **ubican bajo demanda** en el directorio `.just/` (gitignored) de cada repositorio — nunca se commitan, por lo que nunca se desincronizan ni se duplican entre repositorios (se elimina la copia por repositorio estilo `gradlew`).

Ejecuta `celestia-devtools init` en un repositorio. Ubica `.just/celestia-devtools.just`, añade `/.just/` a `.gitignore`, instala el hook commit-msg e imprime la línea de import. Añade cerca del inicio de tu justfile:

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash; obligatorio en Windows
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = opcional: se analiza antes de la ubicación
```

Luego añade una receta `fetch` para que cualquiera pueda (re)ubicar el archivo compartido:

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?` (import opcional) permite que un checkout nuevo analice la justfile antes de la ubicación, de modo que tus propias recetas siempre funcionen. Ejecuta `just fetch` (o `celestia-devtools init`) para ubicar las recetas compartidas — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch`, etc. Todas las recetas se pueden sobrescribir — redefine cualquiera tras la línea `import?`.

**Nota para Windows:** si `bash` resuelve a WSL (`just windows-shell-check` reporta un secuestro), antepone el `usr/bin` de Git al PATH:

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## Gobernanza de mensajes de commit

`celestia-devtools` aplica la convención gitmoji de la organización — cada commit y fusión de PR debe comenzar con un gitmoji, usar inglés en mayúsculas y terminar con un punto (`.`). Consulta `celestia-devtools commit-msg-lint check --help` para el conjunto completo de reglas.

### ¿Por qué?

`gh pr merge --squash --subject "..."` omite TODA validación — el asunto que escribes se convierte directamente en el commit de fusión. Sin un control, los mensajes incorrectos se cuelan.

### Protección local (recomendada)

Después de `pip install celestia-devtools`, ejecuta `celestia-devtools init` en tu repositorio. Esto instala un hook `commit-msg` que rechaza mensajes incorrectos al hacer `git commit`.

Para la protección de fusión de PR, añade una función shell a `~/.bashrc`:

```bash
gh() { celestia-devtools gh "$@"; }
```

Después de `source ~/.bashrc`, `gh pr merge` valida el asunto antes de reenviarlo al binario `gh` real. Todos los demás comandos (`gh pr list`, `gh issue`, `gh repo`, etc.) pasan sin modificaciones. El binario real en `/usr/bin/gh` nunca se modifica.

Para CI o shells no interactivos (donde `.bashrc` no se carga), usa el proxy directamente:

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### Protección CI (opcional, opt-in)

Para añadir validación automática de PR mediante GitHub Actions:

```bash
celestia-devtools init --with-workflows
```

Esto genera `.github/workflows/commit-msg-lint.yml`. Haz commit y push.

Para hacerlo obligatorio, activa la protección de rama en tu rama por defecto mediante GitHub Settings → Branches → "Require status checks to pass before merging" → elige `lint-commits / Lint commit messages`.

> **Nota:** los repositorios privados necesitan GitHub Team ($4/mes) para la protección de rama. Los repositorios públicos la obtienen gratis.

### Todos los comandos

| Comando | Propósito |
|---------|---------|
| `celestia-devtools init` | Desplegar justfiles + instalar hook commit-msg |
| `celestia-devtools init --with-workflows` | También genera workflow CI (opt-in) |
| `celestia-devtools commit-msg-lint check --subject "..."` | Validar una cadena de mensaje |
| `celestia-devtools pr-merge --subject "..." --squash` | Validar y luego fusionar (independiente) |
| `celestia-devtools gh pr merge --subject "..."` | Proxy gh transparente |

## Licencia

Bajo la licencia [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
