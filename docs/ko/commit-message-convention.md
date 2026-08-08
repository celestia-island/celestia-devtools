# 커밋 메시지 규칙

모든 Celestia Island 저장소는 [gitmoji](https://gitmoji.dev) 기반 커밋 메시지 규칙을 적용합니다. 이 문서는 규칙, 예외 및 적용 메커니즘을 정의합니다.

## 규칙

모든 커밋의 제목(커밋 메시지의 첫 번째 줄)은 다음 형식을 따라야 합니다:

```
<gitmoji> <대문자로 시작하는 영어 요약.>
```

| 요구 사항 | 예시(통과) | 예시(실패) |
|---|---|---|
| gitmoji로 시작 | `🐛 Fix...` | `Fix...` |
| Conventional Commits 접두사 없음 | `🐛 Fix...` | `🐛 fix: ...` |
| 이모지 후 첫 글자는 대문자 | `🐛 Fix...` | `🐛 fix...` |
| 마침표로 끝남 | `🐛 Fix the parser crash.` | `🐛 Fix the parser crash` |
| 영어만 사용 | `🐛 Fix the parser crash.` | `🐛 파서 충돌 수정.` |
| 설명적(버전만/채우기 아님) | `⬆️ Upgrade the HTTP client to v2.` | `⬆️ 0.3.0` |

## 예외

다음 커밋 유형은 자동으로 제외됩니다:

- **병합 커밋**: `Merge branch 'foo'` / `Merge pull request #42`
- **되돌리기 커밋**: `Revert "..."`

단일 커밋에 대해 검사를 건너뛰려면:

```bash
CELESTIA_COMMIT_MSG_SKIP=1 git commit
```

## 적용

이 규칙은 세 가지 계층에서 적용됩니다:

1. **로컬 commit-msg 훅** — `celestia-devtools init`에 의해 자동으로 설치됩니다. `git commit` 시 무효한 메시지를 차단합니다. 재설치 또는 새로고침:

   ```bash
   celestia-devtools hook install
   celestia-devtools hook install --force   # 사용자 정의 훅 덮어쓰기
   just commit-msg-hook-install             # just recipe를 통해
   ```

2. **CI 검사(PR)** — 재사용 가능한 워크플로 `commit-msg-lint.yml`이 풀 리퀘스트의 모든 커밋을 검증합니다. 이 작업을 저장소의 `checks.yml`에 추가:

   ```yaml
   commit-msg:
     uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
   ```

3. **브랜치 보호** — GitHub 저장소 설정에서 `commit-msg` 상태 검사를 `master` 브랜치의 필수 검사로 구성합니다.

## 봇 저장소

완전히 자동화된 프로세스로 구동되는 저장소(예: `provider-registry`)는 `celestia-devtools init --no-hooks`를 실행하여 로컬 훅을 건너뛰고 CI 검사를 포함하지 않음으로써 CI를 건너뛰어야 합니다. 개별 봇 커밋도 `CELESTIA_COMMIT_MSG_SKIP=1`을 통해 제외할 수 있습니다.

## 마이그레이션

저장소 기록의 기존 커밋은 소급하여 검증되지 않습니다. 이 규칙은 새 커밋에만 적용됩니다. 저장소의 master 브랜치가 현재 Conventional Commits 접두사(`feat:`, `fix:` 등)를 사용하는 경우 모든 새 작업에 gitmoji 형식을 채택하여 점진적으로 전환할 수 있습니다.

## 빠른 참조

| Gitmoji | 의미 | 사용 시기 |
|---|---|---|
| ✨ | Sparkles | 새 기능 |
| 🐛 | Bug | 버그 수정 |
| 📝 | Memo | 문서 |
| ♻️ | Recycle | 리팩터링 |
| 🚀 | Rocket | 배포/릴리스 |
| 🔒 | Lock | 보안 |
| ⬆️ | Arrow up | 의존성 업그레이드 |
| ⬇️ | Arrow down | 의존성 다운그레이드 |
| 🔧 | Wrench | 설정 변경 |
| ✅ | Check mark | 테스트 |
| 🚧 | Construction | 작업 중 |
| 🎨 | Art | 포맷/구조 |
| 💚 | Green heart | CI 수정 |
| 🔥 | Fire | 코드 제거 |
| 🚑 | Ambulance | 핫픽스 |
| 📄 | Page | 라이선스 |
| 🔨 | Hammer | 개발 스크립트 |
| 🌐 | Globe | 국제화 |
| 💡 | Bulb | 주석 |

전체 목록은 [gitmoji.dev](https://gitmoji.dev)를 참조하세요.
