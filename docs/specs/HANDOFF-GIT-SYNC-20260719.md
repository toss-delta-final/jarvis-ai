# HANDOFF — Seller 작업 Git 동기화 (2026-07-19)

다른 세션에서 이 작업을 이어받기 위한 문서. **아직 commit/push 하지 않음. 사용자 확인 전 절대 commit/push 금지.**

## 목표

로컬 Seller 작업(전부 미커밋)을 기능별로 커밋하고, origin/main의 팀원 변경 14커밋을 유실 없이 통합한 뒤 push. force push·reset·히스토리 파괴 절대 금지.

## 진단 결과 (2026-07-19 확인)

- 브랜치: `feat/seller-tools` (로컬 전용, 원격에 없음). 커밋 0개 — **모든 Seller 작업이 미커밋 워킹트리에만 존재.**
- `origin/main`이 로컬보다 **14커밋 앞** (cart #16, recommend #15, api-spec v0.15.7 #14, obs #8, SSE infra #1, CI 다수). fetch는 이미 완료됨.
- ⚠️ modified 75개 중 **56개는 CRLF/LF 줄바꿈 노이즈** (LICENSE, docs/api-spec.md 등 전체 줄 재작성으로 표시). `git diff --ignore-cr-at-eol --stat` 기준 실제 변경은 19개 파일 + untracked 33개.
- `core.autocrlf` 미설정 상태.
- 실제 내 변경:
  - 신규: `app/agents/seller/*` 11개, `tests/unit/test_seller_*` 13개, `docs/specs/*SELLER*` 9개
  - 수정: `app/api/seller.py·deps.py·chat.py`, `app/core/auth.py·config.py`, `app/schemas/spring.py(신규)·chat.py`, `app/services/spring_client.py(+320줄)·order_seed.py`, `docs/lessons.md·mvp-plan.md·mvp-todo.md`, `CHANGELOG.md`, `pyproject.toml`, `uv.lock`

## 충돌 해결 정책 (사용자 지정)

- Seller 관련 파일 → **내 코드 우선** (필요시 팀원 변경과 수동 병합)
- Seller 무관 파일 → **팀원(origin/main) 우선**, 불필요한 변경 금지
- ⚠️ rebase 중 ours/theirs 반전: `--ours`=origin/main(팀원), `--theirs`=내 커밋

## 실행 계획 (순서 엄수)

### Step 0 — 물리 백업
```powershell
Copy-Item -Recurse C:\Users\vssea\jarvis-ai C:\Users\vssea\jarvis-ai-backup-20260719
```

### Step 1 — 줄바꿈 노이즈 제거 (커밋 전 필수)
```powershell
git config core.autocrlf true
git status   # modified가 ~19개로 줄어드는지 확인. 안 줄면 중단
```
이유: 노이즈 채로 커밋하면 팀원 파일 56개가 "변경"으로 기록 → 대규모 충돌 + 의도치 않은 변경.

### Step 2 — 기능별 커밋 (Conventional Commits, diff 확인 후 확정)

| # | 커밋 | 파일 |
|---|---|---|
| 1 | `feat(seller): 판매자 에이전트 코어 구현` | `app/agents/seller/*` 11개 |
| 2 | `feat(seller): 판매자 API·인증·설정 연동` | `app/api/seller.py`, `deps.py`, `chat.py`, `core/auth.py`, `core/config.py`, `schemas/chat.py` |
| 3 | `feat(spring): Spring 클라이언트·스키마 확장` | `services/spring_client.py`, `schemas/spring.py`, `order_seed.py` |
| 4 | `test(seller): 판매자 단위 테스트 추가` | `tests/unit/test_seller_*` 13개, `test_config_seller.py`, `test_health.py`, `test_schemas_camel.py` |
| 5 | `docs(seller): 설계·핸드오프 문서` | `docs/specs/*SELLER*`, `lessons.md`, `mvp-*`, `CHANGELOG.md` |
| 6 | `chore: 의존성 갱신` | `pyproject.toml`, `uv.lock` |

### Step 3 — 백업 브랜치
```powershell
git branch backup/seller-20260719
```

### Step 4~5 — 통합: rebase 선택 (근거: 내 커밋은 로컬 전용 → 히스토리 안 깨짐, force push 불필요, 실패 시 `git rebase --abort`로 완전 복구)
```powershell
git fetch origin
git rebase origin/main
```

### Step 6 — 충돌 해결
```powershell
git checkout --theirs <Seller 파일>   # 내 코드
git checkout --ours <무관 파일>       # 팀원 코드
git add <파일> ; git rebase --continue
```
예상 충돌: `CHANGELOG.md`, `docs/mvp-todo.md`, `docs/mvp-plan.md`, `app/api/deps.py`, `app/api/chat.py`, `app/core/config.py`, `pyproject.toml`, `uv.lock`(팀원 것 채택 후 `uv lock` 재생성 권장). Seller 신규 파일은 충돌 없을 것.

### Step 7 — 검증 (CLAUDE.md 규칙: 테스트 없이 완료 보고 금지)
```powershell
uv run ruff check --fix ; uv run ruff format ; uv run pytest
```

### Step 8 — Push & PR (main 보호 브랜치 — 직접 push 금지)
```powershell
git push -u origin feat/seller-tools
# GitHub에서 main 대상 PR 생성 (리뷰 1인 + CI)
git checkout main ; git pull --ff-only
```

## 결정 사항 / Q&A 기록

- "pull부터 해야 하나?" → 아니오. pull = fetch + merge. fetch는 완료됨. 미커밋 75개 파일 상태에서 pull하면 되돌릴 스냅샷 없는 충돌로 유일하게 코드 유실 가능. **커밋 먼저 → 그 다음 rebase(=pull의 통합 단계)**.
- rebase vs merge → rebase. 로컬 전용 커밋이라 안전하고 히스토리 깔끔.
- Claude 권한: 로컬 git 작업은 셸로 직접 실행 가능(승인 시). push는 샌드박스에 GitHub 인증 없어 사용자가 직접 실행 권장.

## 다음 세션 시작점

1. `git status`로 현재 어느 Step까지 진행됐는지 확인 (이 문서 작성 시점: Step 0 이전, 아무것도 실행 안 함).
2. Step 1 실행 → 실제 변경 파일 확정 → Step 2 커밋 분리안 diff로 검증 → **사용자 확인 후에만** 커밋 진행.
