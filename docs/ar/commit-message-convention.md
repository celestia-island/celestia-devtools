# اتفاقية رسائل الالتزام

جميع مستودعات Celestia Island تفرض اتفاقية رسائل الالتزام القائمة على [gitmoji](https://gitmoji.dev). يحدد هذا المستند القواعد والإعفاءات وآليات التنفيذ.

## القاعدة

يجب أن يتبع موضوع كل التزام (السطر الأول من رسالة الالتزام) هذا التنسيق:

```
<gitmoji> <ملخص باللغة الإنجليزية يبدأ بحرف كبير.>
```

| المتطلب | مثال (ناجح) | مثال (فاشل) |
|---|---|---|
| يبدأ بـ gitmoji | `🐛 Fix...` | `Fix...` |
| لا بادئة Conventional Commits | `🐛 Fix...` | `🐛 fix: ...` |
| الحرف الأول بعد الرمز التعبيري كبير | `🐛 Fix...` | `🐛 fix...` |
| ينتهي بنقطة | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| باللغة الإنجليزية فقط | `🐛 Fix the parser crash.` | `🐛 修复解析器崩溃。` |
| وصفي (ليس مجرد إصدار/حشو) | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## الإعفاءات

أنواع الالتزامات التالية معفاة تلقائياً:

- **التزامات الدمج**: `Merge branch 'foo'` / `Merge pull request #42`
- **التزامات الإرجاع**: `Revert "..."`

لتخطي الفحص لالتزام واحد:

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## التنفيذ

يتم تطبيق الاتفاقية على ثلاث طبقات:

1. **خطاف commit-msg المحلي** — يتم تثبيته تلقائياً بواسطة `celestia-devtools init`. يمنع الرسائل غير الصالحة عند `git commit`. لإعادة التثبيت أو التحديث:

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # استبدال خطاف مخصص
   just commit-msg-hook-install             # عبر just recipe
   ```

2. **فحص CI (طلبات السحب)** — سير العمل القابل لإعادة الاستخدام `commit-msg-lint.yml` يتحقق من صحة كل التزام في طلب السحب. أضف هذه المهمة إلى `checks.yml` لمستودعك:

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **حماية الفرع** — قم بتكوين فحص الحالة `commit-msg` كشرط مطلوب على فرع `master` في إعدادات مستودع GitHub الخاص بك.

## مستودعات البوتات

المستودعات التي تعتمد بالكامل على العمليات الآلية (مثل `provider-registry`) يجب أن تتخطى الخطاف المحلي عن طريق تشغيل `celestia-devtools init --no-hooks` وتتخطى فحص CI بعدم تضمينه. يمكن أيضاً إعفاء التزامات البوتات الفردية عبر `CELESTIA_COMMIT_MSG_SKIP=1`.

## الترحيل

الالتزامات الموجودة في تاريخ المستودع لا يتم التحقق من صحتها بأثر رجعي — هذه الاتفاقية تنطبق فقط على الالتزامات الجديدة. إذا كان فرع master لمستودعك يستخدم حالياً بادئات Conventional Commits (`feat:`، `fix:`، إلخ)، يمكنك الانتقال تدريجياً عن طريق اعتماد تنسيق gitmoji لجميع الأعمال الجديدة.

## مرجع سريع

| Gitmoji | المعنى | متى تستخدم |
|---|---|---|
| ✨ | Sparkles | ميزة جديدة |
| 🐛 | Bug | إصلاح خطأ |
| 📝 | Memo | توثيق |
| ♻️ | Recycle | إعادة هيكلة |
| 🚀 | Rocket | نشر / إصدار |
| 🔒 | Lock | أمان |
| ⬆️ | Arrow up | ترقية التبعيات |
| ⬇️ | Arrow down | خفض التبعيات |
| 🔧 | Wrench | تغييرات التكوين |
| ✅ | Check mark | اختبارات |
| 🚧 | Construction | قيد العمل |
| 🎨 | Art | تنسيق / هيكل |
| 💚 | Green heart | إصلاح CI |
| 🔥 | Fire | إزالة كود |
| 🚑 | Ambulance | إصلاح عاجل |
| 📄 | Page | ترخيص |
| 🔨 | Hammer | نصوص تطويرية |
| 🌐 | Globe | تدويل |
| 💡 | Bulb | تعليقات |

انظر [gitmoji.dev](https://gitmoji.dev) للقائمة الكاملة.
