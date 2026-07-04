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

`celestia-devtools` — общий набор Python-скриптов сборки и инструментов разработки для экосистемы Celestia. Выделен из `arona/scripts/`, он отделяет инструменты разработки от отдельных crate'ов и используется репозиториями `entelecheia`, `shittim-chest`, `evernight` и другими через justfile.

Предоставляет управление кэшем cargo (`cache-guard`), форматирование Markdown (`format-markdown`), предварительную подготовку зависимостей для офлайн-сборок (`prefetch`), проверку предварительных требований кросс-компиляции (`check-cross-deps`) и утилиты поиска crate'ов (`locate`).

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

Выполните `celestia-devtools init` в репозитории, чтобы добавить `common.just` как фиксируемый `celestia-devtools.just`, затем один раз импортируйте его в начале вашего justfile:

```just
import "./celestia-devtools.just"
```

При новом checkout'е `just ensure` устанавливает пакет, а рецепты `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check` и `locate` становятся доступны. Все рецепты можно переопределять — полный список см. во включённом файле.

## Лицензия

Распространяется по лицензии [Synthetic Source License (SySL), Version 1.0](../../LICENSE).
