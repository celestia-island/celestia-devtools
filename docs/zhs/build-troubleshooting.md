# 构建问题排查

Celestia 生态中常见的构建失败问题及其解决方法。

## `aws-lc-sys` 在 Windows 上因 "NASM failed" 而失败

多个 Rust crate（`aoba`、`noa`、`scriptum`、`tairitsu`、`arona` 以及其他依赖 `rustls` 默认 TLS 提供者的 crate）在 Windows 上构建失败，错误信息如下：

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### 原因

`aws-lc-sys`（自 0.23 版本起 `rustls` 的默认加密后端）使用 NASM 构建 AWS-LC 的汇编源文件。构建失败发生在汇编阶段——并非因为缺少 NASM（它已安装且在 `PATH` 中），而是因为在此主机上的 MSVC 工具链下，针对 AWS-LC 生成的 `.asm` 文件运行的 NASM 3.x 无法完整完成。

### 解决方案

1. **交叉编译至 `*-linux-musl`**，而非在 Windows 上原生构建。这些 crate 大多部署在 Linux 服务器上，musl 目标使用不会触发此问题的 Linux 汇编器构建 `aws-lc-sys`。请参阅各 crate 的 `justfile` / `.cargo/config.toml` 了解已配置的交叉编译目标。

2. **将 TLS 提供者切换为 `ring`**（按 crate 选择启用），在 `rustls` 依赖中禁用 `aws_lc_rs` 并启用 `ring`：

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   仅当 crate 不依赖 AWS-LC 特定行为时才可执行此操作；这是一个依赖项变更，需在每个项目中验证。

3. **在 WSL（Ubuntu）中构建**，`aws-lc-sys` 可使用系统工具链正常汇编。

### 状态

这是一个环境/工具链问题，而非特定项目的代码缺陷。在此记录以便贡献者不必将其作为项目 Bug 追查。如果你修复了原生 Windows 构建，请更新此说明。
