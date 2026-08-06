# ビルドトラブルシューティング

Celestia エコシステムでよく見られるビルド障害とその回避方法について説明します。

## `aws-lc-sys` が Windows で "NASM failed" により失敗する

複数の Rust クレート（`aoba`、`noa`、`scriptum`、`tairitsu`、`arona`、および `rustls` をデフォルトの TLS プロバイダで利用するその他すべてのクレート）が Windows で以下のエラーによりビルドに失敗します：

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### 原因

`aws-lc-sys`（0.23 以降の `rustls` のデフォルト暗号バックエンド）は、NASM を使用して AWS-LC のアセンブリソースをビルドします。ビルドはアセンブリ段階で失敗します。これは NASM がないからではなく（インストールされ PATH に含まれています）、このホストの MSVC ツールチェーンでは、AWS-LC が生成した `.asm` ファイルに対する NASM 3.x の呼び出しが正常に完了しないためです。

### 回避策

1. **Windows 上でのネイティブビルドではなく、`*-linux-musl` へのクロスコンパイル**を行ってください。これらのクレートのほとんどは Linux サーバーをターゲットとしており、musl ターゲットではこの問題が発生しない Linux アセンブラで `aws-lc-sys` をビルドします。各クレートの `justfile` / `.cargo/config.toml` で設定されているクロスコンパイルターゲットを参照してください。

2. **TLS プロバイダを `ring` に切り替えます**（クレート単位、オプトイン）。`rustls` 依存関係で `aws_lc_rs` を無効にし、`ring` を有効にします：

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   これはクレートが AWS-LC 固有の動作に依存していない場合にのみ行ってください。依存関係の変更であり、プロジェクトごとに検証する必要があります。

3. **WSL（Ubuntu）内でビルドします**。`aws-lc-sys` はシステムツールチェーンで正常にアセンブルされます。

### ステータス

これは環境/ツールチェーンの問題であり、特定プロジェクトのコード欠陥ではありません。ここに記録することで、 contributors がプロジェクトのバグとして追跡することを防ぎます。ネイティブ Windows ビルドを修正した場合は、この注意書きを更新してください。
