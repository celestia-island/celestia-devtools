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
celestia-devtools commit-msg-lint check .git/COMMIT_EDITMSG  # 커밋 메시지 검사
celestia-devtools hook install         # 조직 commit-msg 훅 설치
```

## justfile 연동

공유 recipe는 `common.just`에 있으며, 각 저장소의 gitignore된 `.just/` 디렉터리에 **온디맨드로** stage됩니다. 커밋하지 않으므로 저장소 간에 어긋남이나 중복이 발생하지 않습니다(`gradlew` 방식의 저장소별 복사는 폐지했습니다).

저장소에서 `celestia-devtools init`을 실행하면 `.just/celestia-devtools.just`를 stage하고, `.gitignore`에 `/.just/`를 추가하고, commit-msg 훅을 설치하며, import 줄을 출력합니다. justfile 상단에 다음을 추가합니다:

```just
set shell := ["bash", "-c"]
set windows-shell := ["bash.exe", "-c"]   # Git Bash. Windows에서 필수
set unstable
set lists

import? "./.just/celestia-devtools.just"   # `?` = 선택적: stage 전에도 파싱 가능
```

그리고 누구나 공유 파일을 (재)stage할 수 있도록 `fetch` recipe를 추가합니다:

```just
[script('bash')]
fetch URL='':
    #!/usr/bin/env bash
    set -euo pipefail
    out=.just/celestia-devtools.just; mkdir -p .just
    if command -v celestia-devtools >/dev/null 2>&1; then cp "$(celestia-devtools include-path)" "$out"
    else curl -fsSL "https://raw.githubusercontent.com/celestia-island/celestia-devtools/dev/src/celestia_devtools/common.just" -o "$out"; fi
```

`import?`(선택적 import) 덕분에 새로 체크아웃한 저장소도 stage 전에 justfile을 파싱할 수 있어, 자체 recipe는 항상 동작합니다. `just fetch`(또는 `celestia-devtools init`)를 실행하여 공유 recipe를 stage하세요 — `cache-guard`, `fmt-markdown`, `prefetch`, `cross-check`, `locate`, `pglite`, `wsl-ensure`, `dev-watch` 등. 모든 recipe는 재정의 가능합니다 — `import?` 줄 이후에 다시 정의하면 됩니다.

**Windows 참고:** `bash`가 WSL로 해석되는 경우(`just windows-shell-check`가 가로채기를 보고하면), Git의 `usr/bin`을 PATH 앞에 추가하세요:

```powershell
[Environment]::SetEnvironmentVariable('PATH','C:\Program Files\Git\usr\bin;' + $env:PATH,'User')
```

## 커밋 메시지 거버넌스

`celestia-devtools`는 조직의 gitmoji 규칙을 강제합니다——모든 커밋과 PR 병합은 gitmoji로 시작하고, 영문 대문자를 사용하며, 마침표(`.`)로 끝나야 합니다. 전체 규칙은 `celestia-devtools commit-msg-lint check --help`를 참조하세요.

### 왜 필요한가요?

`gh pr merge --squash --subject "..."` 는 모든 검증을 우회합니다——입력한 subject가 그대로 병합 커밋이 됩니다. 게이트가 없으면 잘못된 메시지가 그대로 통과됩니다.

### 로컬 보호 (권장)

`pip install celestia-devtools` 후, 저장소에서 `celestia-devtools init`을 실행하세요. `git commit` 시 잘못된 메시지를 거부하는 `commit-msg` 훅이 설치됩니다.

PR 병합 보호를 위해 `~/.bashrc`에 셸 함수를 추가하세요:

```bash
gh() { celestia-devtools gh "$@"; }
```

`source ~/.bashrc` 후, `gh pr merge`는 실제 `gh` 바이너리로 전달하기 전에 subject를 검증합니다. 다른 모든 명령(`gh pr list`, `gh issue`, `gh repo` 등)은 그대로 통과됩니다. `/usr/bin/gh`의 실제 바이너리는 절대 수정되지 않습니다.

CI 또는 비대화형 셸(`.bashrc`가 로드되지 않는 환경)에서는 프록시를 직접 사용하세요:

```bash
celestia-devtools gh pr merge --squash --subject "🐛 Fix the bug." --repo owner/repo
```

### CI 보호 (선택 사항, 옵트인)

GitHub Actions를 통해 자동 PR 검증을 추가하려면:

```bash
celestia-devtools init --with-workflows
```

`.github/workflows/commit-msg-lint.yml`이 생성됩니다. 커밋하고 푸시하세요.

강제하려면 기본 브랜치에서 브랜치 보호를 활성화하세요: GitHub Settings → Branches → "Require status checks to pass before merging" → `lint-commits / Lint commit messages`를 선택합니다.

> **참고:** 비공개 저장소에서 브랜치 보호를 사용하려면 GitHub Team($4/월)이 필요합니다. 공개 저장소는 무료입니다.

### 모든 명령

| 명령 | 목적 |
|---------|---------|
| `celestia-devtools init` | justfile 스테이징 + commit-msg 훅 설치 |
| `celestia-devtools init --with-workflows` | CI 워크플로도 생성 (옵트인) |
| `celestia-devtools commit-msg-lint check --subject "..."` | 메시지 문자열 검증 |
| `celestia-devtools pr-merge --subject "..." --squash` | 검증 후 병합 (독립 실행형) |
| `celestia-devtools gh pr merge --subject "..."` | 투명 gh 프록시 |

## 라이선스

[Synthetic Source License (SySL), Version 1.0](../../LICENSE)에 따라 라이선스됩니다.
