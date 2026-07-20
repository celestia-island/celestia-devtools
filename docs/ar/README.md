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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # التحقق من رسالة commit
celestia-devtools hook install         # تثبيت خطّاف commit-msg الخاص بالمنظمة
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

## إدارة رسائل commit

تفرض `celestia-devtools` اتفاقية gitmoji الخاصة بالمنظمة — يجب أن يبدأ كل commit ودمج PR بـ gitmoji، ويستخدم الإنجليزية بالأحرف الكبيرة، وينتهي بنقطة (`.`). راجع `celestia-devtools commit-msg-lint check --help` لمجموعة القواعد الكاملة.

### لماذا؟

`gh pr merge --squash --subject "..."` يتجاوز جميع عمليات التحقق — subject الذي تكتبه يصبح مباشرة commit الدمج. بدون بوابة، تتسلل الرسائل السيئة.

### الحماية المحلية (موصى بها)

بعد `pip install celestia-devtools`، شغّل `celestia-devtools init` في مستودعك. هذا يثبّت خطّاف `commit-msg` الذي يرفض الرسائل السيئة عند `git commit`.

لحماية دمج PR، أضف دالة shell إلى `~/.bashrc`:

```bash
gh() { celestia-devtools gh "$@"; }
```

بعد `source ~/.bashrc`، يتحقق `gh pr merge` من subject قبل إعادة توجيهه إلى ثنائي `gh` الحقيقي. جميع الأوامر الأخرى (`gh pr list`، `gh issue`، `gh repo`، إلخ) تمر دون تغيير. الملف الثنائي الحقيقي في `/usr/bin/gh` لا يُعدّل أبدًا.

للـ CI أو الأصداف غير التفاعلية (حيث لا يُحمّل `.bashrc`)، استخدم الوكيل مباشرة:

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### حماية CI (اختيارية، تفعيل يدوي)

لإضافة تحقق تلقائي من PR عبر GitHub Actions:

```bash
celestia-devtools init --with-workflows
```

هذا ينشئ `.github/workflows/commit-msg-lint.yml`. قم بعمل commit و push له.

للتطبيق الإلزامي، فعّل حماية الفروع على الفرع الافتراضي عبر GitHub Settings → Branches → "Require status checks to pass before merging" → اختر `lint-commits / Lint commit messages`.

> **ملاحظة:** المستودعات الخاصة تحتاج GitHub Team ($4/شهريًا) لحماية الفروع. المستودعات العامة تحصل عليها مجانًا.

### جميع الأوامر

| الأمر | الغرض |
|---------|---------|
| `celestia-devtools init` | تجهيز justfiles + تثبيت خطّاف commit-msg |
| `celestia-devtools init --with-workflows` | أيضًا إنشاء CI workflow (تفعيل يدوي) |
| `celestia-devtools commit-msg-lint check --subject "..."` | التحقق من سلسلة رسالة |
| `celestia-devtools pr-merge --subject "..." --squash` | التحقق ثم الدمج (مستقل) |
| `celestia-devtools gh pr merge --subject "..."` | وكيل gh شفاف |

## الترخيص

مرخص بموجب [Synthetic Source License (SySL), Version 1.0](../../LICENSE).

</div>
