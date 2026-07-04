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

`celestia-devtools` هي مجموعة أدوات Python مشتركة لبرامج البناء وأدوات التطوير داخل منظومة Celestia. استُخرجت من `arona/scripts/`، وتفصل أدوات التطوير عن crate الفردية، وتُستخدم عبر justfile من قبل `entelecheia` و`shittim-chest` و`evernight` ومستودعات أخرى.

توفر إدارة ذاكرة cargo المؤقتة (`cache-guard`)، وتنسيق Markdown (`format-markdown`)، وتجهيز الاعتماديّات مسبقًا لبناءات دون اتصال (`prefetch`)، والتحقق من متطلبات الترجمة المتقاطعة (`check-cross-deps`)، وأدوات تحديد مواقع crate (`locate`).

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

شغّل `celestia-devtools init` داخل مستودع لإضافة `common.just` كملف `celestia-devtools.just` مُتابَع، ثم استورده مرة واحدة قرب أعلى ملف justfile لديك:

```just
import "./celestia-devtools.just"
```

عند نسخة جديدة، يقوم `just ensure` بتثبيت الحزمة، وتصبح الوصفات مثل `cache-guard` و`fmt-markdown` و`prefetch` و`cross-check` و`locate` متاحة. يمكن إعادة تعريف جميع الوصفات — راجع الملف المُضمَّن للقائمة الكاملة.

## الترخيص

مرخص بموجب [Synthetic Source License (SySL), Version 1.0](../../LICENSE).

</div>
