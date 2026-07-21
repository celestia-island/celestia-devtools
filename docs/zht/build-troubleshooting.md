# 構建問題排查

Celestia 生態中常見的構建失敗問題及其解決方法。

## `aws-lc-sys` 在 Windows 上因 "NASM failed" 而失敗

多個 Rust crate（`aoba`、`noa`、`scriptum`、`tairitsu`、`arona` 以及其他依賴 `rustls` 預設 TLS 提供者的 crate）在 Windows 上構建失敗，錯誤訊息如下：

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### 原因

`aws-lc-sys`（自 0.23 版本起 `rustls` 的預設加密後端）使用 NASM 構建 AWS-LC 的組合語言原始檔。構建失敗發生在組合階段——並非因為缺少 NASM（它已安裝且在 `PATH` 中），而是因為在此主機上的 MSVC 工具鏈下，針對 AWS-LC 生成的 `.asm` 檔案執行的 NASM 3.x 無法完整完成。

### 解決方案

1. **交叉編譯至 `*-linux-musl`**，而非在 Windows 上原生構建。這些 crate 大多部署在 Linux 伺服器上，musl 目標使用不會觸發此問題的 Linux 組合器構建 `aws-lc-sys`。請參閱各 crate 的 `justfile` / `.cargo/config.toml` 了解已配置的交叉編譯目標。

2. **將 TLS 提供者切換為 `ring`**（按 crate 選擇啟用），在 `rustls` 相依項目中停用 `aws_lc_rs` 並啟用 `ring`：

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   僅當 crate 不依賴 AWS-LC 特定行為時才可執行此操作；這是一個相依項目變更，需在每個專案中驗證。

3. **在 WSL（Ubuntu）中構建**，`aws-lc-sys` 可使用系統工具鏈正常組合。

### 狀態

這是一個環境/工具鏈問題，而非特定專案的程式碼缺陷。在此記錄以便貢獻者不必將其作為專案 Bug 追查。如果你修復了原生 Windows 構建，請更新此說明。
