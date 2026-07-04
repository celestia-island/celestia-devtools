<p align="center"><img src="https://celestia.world/logos/celestia.webp" alt="celestia-devtools" width="200" /></p>

<h1 align="center">celestia-devtools</h1>

<p align="center"><strong>Celestia 생태계를 위한 공유 빌드 및 개발 도구 스크립트</strong></p>

<div align="center">

[![License: SySL](https://img.shields.io/badge/license-SySL%201.0-blue)](../../LICENSE)
[![GitHub](https://img.shields.io/badge/github-celestia--island%2Fcelestia--devtools-blue)](https://github.com/celestia-island/celestia-devtools)

</div>

<div align="center">

[English](../en/README.md) ·
[简体中文](../zhs/README.md) ·
[繁體中文](../zht/README.md) ·
[日本語](../ja/README.md) ·
**한국어** ·
[Français](../fr/README.md) ·
[Español](../es/README.md) ·
[Русский](../ru/README.md) ·
[العربية](../ar/README.md)

</div>

## 소개

`celestia-devtools`는 Celestia 생태계를 위한 공유 빌드 및 개발 도구 스크립트를 모은 Python 툴킷입니다. 개발 도구를 개별 crate로부터 분리하며, `entelecheia`, `shittim-chest`, `evernight` 등의 저장소에서 justfile을 통해 사용됩니다.

cargo 캐시 관리(`cache-guard`), Markdown 포맷팅(`format-markdown`), 오프라인 빌드용 의존성 사전 준비(`prefetch`), 교차 컴파일 선행 조건 검사(`check-cross-deps`), 범용 형제 crate 위치 탐색(`locate`)을 제공합니다.

> 여전히 개발 중이며, 명령과 recipe는 향후 변경될 수 있습니다.

## 빠른 시작

```bash
# 개발용으로 편집 가능 모드 설치
pip install -e .

# 또는 git에서 설치
pip install git+https://github.com/celestia-island/celestia-devtools.git

# 통합 CLI
celestia-devtools cache-guard .        # cargo target/ 디스크 사용량 관리
celestia-devtools format-markdown .    # Markdown 파일 포맷 및 린트
celestia-devtools prefetch .           # 오프라인 빌드를 위한 의존성 사전 준비
celestia-devtools check-cross-deps     # 교차 컴파일 선행 조건 확인
celestia-devtools locate               # celestia-island crate 체크아웃 위치 탐색
```

## justfile 연동

저장소에서 `celestia-devtools init`을 실행하면 `common.just`를 커밋 대상인 `celestia-devtools.just`로 가져옵니다. 그런 뒤 justfile 상단에서 한 번 임포트합니다:

```just
import "./celestia-devtools.just"
```

새로 체크아웃한 환경에서 `just ensure`가 패키지를 설치하고, `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate` 등의 recipe를 사용할 수 있게 합니다. 모든 recipe는 재정의 가능하며, 전체 목록은 가져온 파일을 참고하세요.

## 라이선스

[Synthetic Source License (SySL), Version 1.0](../../LICENSE)에 따라 라이선스됩니다.
