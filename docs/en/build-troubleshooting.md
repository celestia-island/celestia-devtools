# Build Troubleshooting

Common build failures seen across the Celestia ecosystem, and how to work
around them.

## `aws-lc-sys` fails with "NASM failed" (Windows)

Several Rust crates (`aoba`, `noa`, `scriptum`, `tairitsu`, `arona`, and
anything else that pulls in `rustls` with the default TLS provider) fail on
Windows with:

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### Cause

`aws-lc-sys` (the default crypto backend for `rustls` since 0.23) builds
AWS-LC's assembly sources with NASM. The build is failing at the assembly
step — not because NASM is missing (it is installed and on `PATH`), but
because the NASM 3.x invocation against AWS-LC's generated `.asm` files does
not complete cleanly under the MSVC toolchain on this host.

### Workarounds

1. **Cross-compile to `*-linux-musl`** instead of building natively on
   Windows. Most of these crates target Linux servers anyway, and the musl
   targets build `aws-lc-sys` with a Linux assembler that does not hit this
   bug. See each crate's `justfile` / `.cargo/config.toml` for the configured
   cross target.

2. **Switch the TLS provider to `ring`** (per-crate, opt-in) by disabling
   `aws_lc_rs` and enabling `ring` on the `rustls` dependency:

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   Only do this if the crate does not rely on AWS-LC-specific behavior; this
   is a dependency change and must be validated per project.

3. **Build inside WSL** (Ubuntu), where `aws-lc-sys` assembles cleanly with
   the system toolchain.

### Status

This is an environment/toolchain issue, not a per-project code defect. It is
tracked here so contributors do not chase it as a project bug. If you fix the
native Windows build, please update this note.
