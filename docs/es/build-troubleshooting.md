# Solución de problemas de compilación

Errores de compilación comunes en el ecosistema Celestia y cómo evitarlos.

## `aws-lc-sys` falla con "NASM failed" (Windows)

Varios crates de Rust (`aoba`, `noa`, `scriptum`, `tairitsu`, `arona` y cualquier otro que utilice `rustls` con el proveedor TLS predeterminado) fallan en Windows con:

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### Causa

`aws-lc-sys` (el backend criptográfico predeterminado para `rustls` desde 0.23) compila las fuentes de ensamblado de AWS-LC con NASM. La compilación falla en el paso de ensamblado, no porque falte NASM (está instalado y en `PATH`), sino porque la invocación de NASM 3.x contra los archivos `.asm` generados por AWS-LC no se completa correctamente bajo la cadena de herramientas MSVC en este host.

### Soluciones

1. **Compile de forma cruzada para `*-linux-musl`** en lugar de compilar de forma nativa en Windows. La mayoría de estos crates están destinados a servidores Linux de todos modos, y los objetivos musl compilan `aws-lc-sys` con un ensamblador de Linux que no encuentra este error. Consulte el `justfile` / `.cargo/config.toml` de cada crate para conocer el objetivo de compilación cruzada configurado.

2. **Cambie el proveedor TLS a `ring`** (por crate, opt-in) deshabilitando `aws_lc_rs` y habilitando `ring` en la dependencia de `rustls`:

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   Solo hágalo si el crate no depende de un comportamiento específico de AWS-LC; este es un cambio de dependencia y debe validarse por proyecto.

3. **Compile dentro de WSL** (Ubuntu), donde `aws-lc-sys` se ensambla correctamente con la cadena de herramientas del sistema.

### Estado

Este es un problema de entorno/cadena de herramientas, no un defecto de código específico del proyecto. Se registra aquí para que los colaboradores no lo persigan como un error del proyecto. Si soluciona la compilación nativa de Windows, actualice esta nota.
