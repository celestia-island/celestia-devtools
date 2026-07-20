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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # проверить сообщение коммита
celestia-devtools hook install         # установить хук commit-msg организации
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

## Управление сообщениями коммитов

`celestia-devtools` обеспечивает соблюдение соглашения организации по gitmoji — каждый коммит и слияние PR должны начинаться с gitmoji, использовать английский в верхнем регистре и заканчиваться точкой (`.`). Полный набор правил см. в `celestia-devtools commit-msg-lint check --help`.

### Зачем это нужно?

`gh pr merge --squash --subject "..."` обходит ВСЕ проверки — введённый вами subject напрямую становится коммитом слияния. Без контроля плохие сообщения проскальзывают.

### Локальная защита (рекомендуется)

После `pip install celestia-devtools` выполните `celestia-devtools init` в вашем репозитории. Это установит хук `commit-msg`, который отклоняет плохие сообщения при `git commit`.

Для защиты слияния PR добавьте shell-функцию в `~/.bashrc`:

```bash
gh() { celestia-devtools gh "$@"; }
```

После `source ~/.bashrc` команда `gh pr merge` проверяет subject перед передачей настоящему бинарному файлу `gh`. Все остальные команды (`gh pr list`, `gh issue`, `gh repo` и т.д.) проходят без изменений. Реальный бинарный файл в `/usr/bin/gh` никогда не изменяется.

Для CI или неинтерактивных оболочек (где `.bashrc` не загружается) используйте прокси напрямую:

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### CI-защита (опционально, требуется явное включение)

Чтобы добавить автоматическую проверку PR через GitHub Actions:

```bash
celestia-devtools init --with-workflows
```

Это создаст `.github/workflows/commit-msg-lint.yml`. Закоммитьте и запушьте его.

Для принудительного применения включите защиту ветки на вашей ветке по умолчанию: GitHub Settings → Branches → "Require status checks to pass before merging" → выберите `lint-commits / Lint commit messages`.

> **Примечание:** для защиты веток в приватных репозиториях требуется GitHub Team ($4/мес). Публичные репозитории получают её бесплатно.

### Все команды

| Команда | Назначение |
|---------|---------|
| `celestia-devtools init` | Размещение justfile + установка хука commit-msg |
| `celestia-devtools init --with-workflows` | Также генерирует CI workflow (opt-in) |
| `celestia-devtools commit-msg-lint check --subject "..."` | Проверка строки сообщения |
| `celestia-devtools pr-merge --subject "..." --squash` | Проверка и слияние (автономно) |
| `celestia-devtools gh pr merge --subject "..."` | Прозрачный gh-прокси |

## Лицензия

Распространяется по лицензии [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
