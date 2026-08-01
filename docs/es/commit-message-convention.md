# Convención de mensajes de commit

Todos los repositorios de Celestia Island aplican una convención de mensajes de commit basada en [gitmoji](https://gitmoji.dev). Este documento define las reglas, exenciones y mecanismos de aplicación.

## Regla

Cada asunto del commit (primera línea del mensaje de commit) debe seguir este formato:

```
<gitmoji> <Resumen en inglés con mayúscula inicial.>
```

| Requisito | Ejemplo (aprobado) | Ejemplo (fallo) |
|---|---|---|
| Comienza con un gitmoji | `🐛 Fix...` | `Fix...` |
| Sin prefijo Conventional Commits | `🐛 Fix...` | `🐛 fix: ...` |
| Primera letra después del emoji en mayúscula | `🐛 Fix...` | `🐛 fix...` |
| Termina con un punto | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| Solo inglés | `🐛 Fix the parser crash.` | `🐛 修复解析器崩溃。` |
| Descriptivo (sin solo versión/relleno) | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## Exenciones

Los siguientes tipos de commit están automáticamente exentos:

- **Commits de fusión**: `Merge branch 'foo'` / `Merge pull request #42`
- **Commits de reversión**: `Revert "..."`

Para omitir la verificación de un solo commit:

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## Aplicación

La convención se aplica en tres niveles:

1. **Hook local commit-msg** — instalado automáticamente por `celestia-devtools init`. Bloquea mensajes no válidos al hacer `git commit`. Para reinstalar o actualizar:

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # sobrescribir un hook personalizado
   just commit-msg-hook-install             # a través de just recipe
   ```

2. **Verificación CI (PR)** — el flujo de trabajo reutilizable `commit-msg-lint.yml` valida cada commit en una pull request. Agregue este trabajo al `checks.yml` de su repositorio:

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **Protección de rama** — configure la verificación de estado `commit-msg` como requerida en la rama `master` en la configuración de su repositorio de GitHub.

## Repositorios de bots

Los repositorios impulsados enteramente por procesos automatizados (por ejemplo, `provider-registry`) deben omitir el hook local ejecutando `celestia-devtools init --no-hooks` y omitir la verificación CI no incluyéndola. Los commits individuales de bots también pueden eximirse mediante `CELESTIA_COMMIT_MSG_SKIP=1`.

## Migración

Los commits existentes en el historial del repositorio no se validan de forma retroactiva: esta convención se aplica solo a los commits nuevos. Si la rama master de su repositorio utiliza actualmente prefijos Conventional Commits (`feat:`, `fix:`, etc.), puede realizar una transición gradual adoptando el formato gitmoji para todo el trabajo nuevo.

## Referencia rápida

| Gitmoji | Significado | Cuándo usarlo |
|---|---|---|
| ✨ | Sparkles | Nueva funcionalidad |
| 🐛 | Bug | Corrección de error |
| 📝 | Memo | Documentación |
| ♻️ | Recycle | Refactorización |
| 🚀 | Rocket | Despliegue / publicación |
| 🔒 | Lock | Seguridad |
| ⬆️ | Arrow up | Actualizar dependencias |
| ⬇️ | Arrow down | Degradar dependencias |
| 🔧 | Wrench | Cambios de configuración |
| ✅ | Check mark | Pruebas |
| 🚧 | Construction | En progreso |
| 🎨 | Art | Formato / estructura |
| 💚 | Green heart | Corrección de CI |
| 🔥 | Fire | Eliminar código |
| 🚑 | Ambulance | Corrección urgente |
| 📄 | Page | Licencia |
| 🔨 | Hammer | Scripts de desarrollo |
| 🌐 | Globe | Internacionalización |
| 💡 | Bulb | Comentarios |

Consulte [gitmoji.dev](https://gitmoji.dev) para ver la lista completa.
