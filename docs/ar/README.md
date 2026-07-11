<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>برامج بناء وأدوات تطوير مشتركة لمنظومة Celestia</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div dir="rtl" align="center">

[English](../en/README.md) ·
[简体中文](../zhs/README.md) ·
[繁體中文](../zht/README.md) ·
[日本語](../ja/README.md) ·
[한국어](../ko/README.md) ·
[Français](../fr/README.md) ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
**العربية**

</div>

<div dir="rtl">

## مقدمة

`celestia-devtools` هي مجموعة أدوات Python مشتركة لبرامج البناء وأدوات التطوير داخل منظومة Celestia. وتفصل أدوات التطوير عن crate الفردية، وتُستخدم عبر justfile من قبل `entelecheia` و`shittim-chest` و`evernight` ومستودعات أخرى.

توفر إدارة ذاكرة cargo المؤقتة (`cache-guard`)، وتنسيق Markdown (`format-markdown`)، وتجهيز الاعتماديّات مسبقًا لبناءات دون اتصال (`prefetch`)، والتحقق من متطلبات الترجمة المتقاطعة (`check-cross-deps`)، وأدوات عامة لتحديد مواقع crate الشقيقة (`locate`).

> لا تزال قيد التطوير؛ قد تتغير الأوامر والوصفات مستقبلًا.

## البداية السريعة

```bash
# تثبيت في وضع قابل للتحرير (للتطوير)
pip install -e .

# أو التثبيت من git
pip install git+https://github.com/celestia-island/celestia-devtools.git

# واجهة سطر أوامر موحدة
celestia-devtools cache-guard .        # إدارة استخدام قرص cargo target/
celestia-devtools format-markdown .    # تنسيق وفحص ملفات Markdown
celestia-devtools prefetch .           # تجهيز الاعتماديّات لبناءات دون اتصال
celestia-devtools check-cross-deps     # التحقق من متطلبات الترجمة المتقاطعة
celestia-devtools locate               # تحديد موقع نسخة crate من celestia-island
```

## التكامل مع justfile

تتواجد الوصفات المشتركة في `common.just` وتُجهَّز **عند الطلب** داخل دليل `.just/` المتجاهَل من git في كل مستودع — لا تُلتزَم أبدًا، وبالتالي لا تنحرف ولا تتكرر بين المستودعات (ألغينا نموذج النسخ لكل مستودع على غرار `gradlew`).

شغّل `celestia-devtools init` داخل مستودع. سيُجهِّز `.just/celestia-devtools.just`، ويضيف `/.just/` إلى `.gitignore`، ويثبّت خطّاف commit-msg، ويطبع سطر الاستيراد. أضف قرب أعلى ملف justfile لديك:

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash؛ مطلوب على Windows
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = اختياري: يُحلَّل قبل التجهيز
```

ثم أضف وصفة `fetch` ليتمكن أي شخص من (إعادة) تجهيز الملف المشترك:

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

يتيح `import?` (الاستيراد الاختياري) للنسخة الجديدة تحليل ملف justfile قبل التجهيز، لذا فإن وصفاتك الخاصة تعمل دائمًا. شغّل `just fetch` (أو `celestia-devtools init`) لتجهيز الوصفات المشتركة — `cache-guard` و`fmt-markdown` و`prefetch` و`cross-check` و`locate` و`pglite` و`wsl-ensure` و`dev-watch` وغيرها. جميع الوصفات قابلة لإعادة التعريف — أعد تعريف أي منها بعد سطر `import?`.

**ملاحظة لـ Windows:** إذا كان `bash` يحلّ إلى WSL (`just windows-shell-check` يُبلغ عن اختطاف)، فألحق `usr/bin` الخاص بـ Git بمقدمة PATH:
```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## الترخيص

مرخص بموجب [Synthetic Source License (SySL), Version 1.0](../../LICENSE).

</div>
