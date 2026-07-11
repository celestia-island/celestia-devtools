<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Общие скрипты сборки и инструменты разработки для экосистемы Celestia</strong></p>

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
[Español](../es/README.md) ·
**Русский** ·
[العربية](../ar/README.md)

</div>

## Введение

`celestia-devtools` — общий набор Python-скриптов сборки и инструментов разработки для экосистемы Celestia. Он отделяет инструменты разработки от отдельных crate'ов и используется репозиториями `entelecheia`, `shittim-chest`, `evernight` и другими через justfile.

Предоставляет управление кэшем cargo (`cache-guard`), форматирование Markdown (`format-markdown`), предварительную подготовку зависимостей для офлайн-сборок (`prefetch`), проверку предварительных требований кросс-компиляции (`check-cross-deps`) и универсальные утилиты поиска соседних crate'ов (`locate`).

> Всё ещё в разработке; команды и рецепты могут изменяться в будущем.

## Быстрый старт

```bash
# Установка в редактируемом режиме (для разработки)
pip install -e .

# Или установка из git
pip install git+https://github.com/celestia-island/celestia-devtools.git

# Единый CLI
celestia-devtools cache-guard .        # управление использованием диска cargo target/
celestia-devtools format-markdown .    # форматирование и проверка файлов Markdown
celestia-devtools prefetch .           # подготовка зависимостей для офлайн-сборок
celestia-devtools check-cross-deps     # проверка предварительных требований кросс-компиляции
celestia-devtools locate               # поиск checkout'а crate'а celestia-island
```

## Интеграция с justfile

Общие рецепты находятся в `common.just` и **по требованию** помещаются в gitignore-каталог `.just/` каждого репозитория — они никогда не коммитятся, поэтому не расходятся и не дублируются между репозиториями (копия на каждый репозиторий в стиле `gradlew` упразднена).

Выполните `celestia-devtools init` в репозитории. Он помещает `.just/celestia-devtools.just`, добавляет `/.just/` в `.gitignore`, устанавливает хук commit-msg и выводит строку импорта. Добавьте ближе к началу вашего justfile:

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash; обязательно на Windows
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = опционально: парсится до размещения
```

Затем добавьте рецепт `fetch`, чтобы любой мог (пере)разместить общий файл:

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?` (опциональный импорт) позволяет свежему checkout'у разобрать justfile до размещения, поэтому ваши собственные рецепты всегда работают. Выполните `just fetch` (или `celestia-devtools init`), чтобы разместить общие рецепты — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch` и т. д. Все рецепты можно переопределять — переопределите любой после строки `import?`.

**Замечание для Windows:** если `bash` разрешается в WSL (`just windows-shell-check` сообщает о перехвате), добавьте `usr/bin` Git'а в начало PATH:
```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## Лицензия

Распространяется по лицензии [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
