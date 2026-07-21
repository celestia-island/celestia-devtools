# 빌드 문제 해결

Celestia 생태계에서 자주 발생하는 빌드 실패와 이를 해결하는 방법입니다.

## `aws-lc-sys`가 Windows에서 "NASM failed" 오류로 실패

여러 Rust 크레이트(`aoba`, `noa`, `scriptum`, `tairitsu`, `arona` 및 기본 TLS 제공자와 함께 `rustls`를 사용하는 기타 모든 크레이트)가 Windows에서 다음 오류와 함께 빌드에 실패합니다:

```
error: failed to run custom build command for `aws-lc-sys v0.42.0`
  thread 'main' panicked at ...nasm_builder.rs:107:17:
  NASM failed for .../aws-lc/generated-src/win-x86_64/crypto/chacha/chacha-x86_64.asm:
```

### 원인

`aws-lc-sys`(0.23부터 `rustls`의 기본 암호화 백엔드)는 NASM을 사용하여 AWS-LC의 어셈블리 소스를 빌드합니다. 빌드는 어셈블리 단계에서 실패합니다. NASM이 누락되었기 때문이 아니라(설치되어 있고 PATH에 있음) 이 호스트의 MSVC 도구 체인에서 AWS-LC가 생성한 `.asm` 파일에 대한 NASM 3.x 호출이 완전히 완료되지 않기 때문입니다.

### 해결 방법

1. **Windows에서 네이티브 빌드하는 대신 `*-linux-musl`로 크로스 컴파일**하십시오. 이러한 크레이트는 대부분 Linux 서버를 대상으로 하며, musl 대상은 이 버그가 발생하지 않는 Linux 어셈블러로 `aws-lc-sys`를 빌드합니다. 각 크레이트의 `justfile` / `.cargo/config.toml`에서 구성된 크로스 컴파일 대상을 확인하세요.

2. **TLS 제공자를 `ring`으로 전환**하십시오(크레이트별 옵트인). `rustls` 종속성에서 `aws_lc_rs`를 비활성화하고 `ring`을 활성화하십시오:

   ```toml
   rustls = { version = "0.23", default-features = false, features = ["ring", "logging", "std", "tls12"] }
   ```

   크레이트가 AWS-LC 특정 동작에 의존하지 않는 경우에만 수행하십시오. 이는 종속성 변경이며 프로젝트별로 검증해야 합니다.

3. **WSL(Ubuntu) 내에서 빌드**하십시오. `aws-lc-sys`가 시스템 도구 체인으로 정상적으로 어셈블됩니다.

### 상태

이는 환경/도구 체인 문제이지 특정 프로젝트의 코드 결함이 아닙니다. 기여자가 프로젝트 버그로 추적하지 않도록 여기에 기록합니다. 네이티브 Windows 빌드를 수정한 경우 이 참고 사항을 업데이트하십시오.
