# GIT 동기화 실행 계획 v2 (2026-07-20) — 4-2·4-3·FINAL 문서 반영판

> HANDOFF-GIT-SYNC-20260719 의 갱신판. **아직 아무것도 실행하지 않음 — 사용자 확인 전
> commit/push 금지.** 실행 위치는 **Windows PowerShell**(샌드박스 git 은 CRLF 시야가 달라
> 부적합 + index 쓰기 권한 문제).

## 대원칙 (2026-07-20 사용자 지정 — 모든 단계에 우선)

1. **팀원 코드 수정·삭제·소실 금지.**
2. **충돌은 판매자 부분만 수정하여 통합** — 그 외 파일은 팀원(origin/main) 그대로.
3. **팀원 코드와 내 코드가 모두 정상 작동** — 최종 게이트는 전체 pytest(팀 테스트 포함).

금지: force push · reset --hard · 히스토리 파괴. 실패 시 `git rebase --abort` = 완전 복구.

## 0. 진단 요약 (2026-07-20 샌드박스 읽기 전용 재확인)

- 브랜치 `feat/seller-tools`, **선행 로컬 커밋 0개**(HEAD 8커밋 전부 origin/main 포함) — rebase 시 재생되는 것은 이번에 만들 새 커밋들뿐.
- origin/main 이 **14커밋 앞**: cart(#16)·recommend(#15)·api-spec v0.15.7(#14)·obs(#8)·SSE infra(#1)·CI.
- modified 75개 중 실변경(EOL 제외) **19개** + untracked 49개(전부 판매자 산출물 + `_to_delete/`).
- **양측 공통 변경(잠재 충돌) 11개** 중 내 쪽이 포맷 노이즈뿐인 3개는 복원 대상(아래 1-b) → 실질 충돌 후보 **8개**.

## 0.5 팀 커밋 14개 리뷰 결과 (2026-07-20 읽기 전용 전수 확인 — 플레이북 §4 를 이 결과가 확정)

내용 요약: CI 워크플로 8건(claude-review 등, 코드 무관) + 실질 6건 —
**#1 SSE infra**(stream/errors/ratelimit/llm), **#8 obs**(conversation/observability·PII),
**#15 recommend**·**#16 cart**(구매자 그래프 — 판매자 무관), **f37fc54 계약 정렬**, api-spec 미러 2건.

| 발견 | 내용 | 병합 지침 |
|---|---|---|
| R1 | **auth.py brandId 는 팀이 이미 반영**(f37fc54: `CLAIM_BRAND_ID`·`Identity.brand_id`) — 내 변경과 동일 취지 | `app/core/auth.py` 는 **팀 것 채택**(내 변경 폐기 — 커밋 3 대상에서 제외, restore) |
| R2 | **`spring_timeout_s` 중복** — 팀 config 에 동일 키·동일 값(3.0) 존재 | 내 config 추가분에서 이 키 **제거**(팀 정의 재사용) |
| R3 | **`internal_api_token` = AI→Spring 아웃바운드 토큰**(팀, spring_client 공통 헤더) — 내 `internal_token` 과 용도 동일 | **팀 키로 통일**: 내 `internal_token` 정의 삭제, `SellerSpringClient` 가 `internal_api_token` 을 읽도록 수정(판매자 코드만 수정). 내 `service_token`(Spring→AI 인바운드)은 용도가 달라 유지 |
| R4 | **seller.py 팀 버전 = 스텁 + 인프라 래퍼** — `start_observation` + `open_stream(http_request, registry_key(identity, session_id), factory, observer=…)` 패턴. open_stream 이 409 동시스트림·취소·first-token 504·전체 90s 절단을 담당(§2.9) | 내 `_seller_stream`(3분기+HITL+적용)을 내부 제너레이터로 유지하고, **엔드포인트를 팀 래퍼 패턴으로 감싼다**(`Request` 파라미터 추가). 내 코드의 "수명주기 TODO"가 이것으로 해소 |
| R5 | **팀 테스트 `test_seller_chat_requires_seller_scope` 는 구계약**(JWT 403) — F1/D1 전환(X-Internal-Token, 401/400) 후에는 실패가 정답 | 판매자 계약 테스트이므로 **새 계약(401)에 맞게 이 테스트만 수정**(대원칙 2 범위) — PR 본문에 계약 전환(REALIGN F1·D1) 근거 명시 |
| R6 | 팀 신설: `app/core/stream·errors·ratelimit·llm·conversation·observability.py`, `tests/conftest.py`·`tests/_fakes.py`·유닛 6파일 | 전부 무수정 수용. 내 `tests/unit/conftest.py` 는 경로가 달라 충돌 없음(공존) |
| R7 | pyproject 팀 변경 없음 / `.github/workflows/ci.yml` 내 변경은 EOL 노이즈 | 커밋 1 은 내 langchain 추가만. ci.yml 은 autocrlf 정리로 소멸 |

### 0.5-b 추가 리뷰 (2026-07-20 2차 — cart #16 이후 신규 11커밋, HEAD..origin/main = 25)

신규 구간: fix(cart) #18 · recommend I-19 #19·#23 · **profile #27** · docs/PRD 7건.
seller.py·auth.py 는 이 구간에서 무변경 — R1·R4·R5 결론 유지. 추가 발견:

| 발견 | 내용 | 병합 지침 |
|---|---|---|
| R8 | **`verify_service_token` 신설**(profile #27, deps.py) — Spring→AI **인바운드**도 `internal_api_token` + `X-Internal-Token` 으로 검증(dev 스킵, jwks fail-closed 401) | **R3 확장: 토큰 키 전면 단일화** — 내 `service_token`(인바운드)·`internal_token`(아웃바운드) 둘 다 폐기, `internal_api_token` 하나로 통일. `require_seller_internal` 은 유지(신원 헤더 400 로직은 팀에 없음)하되 검증 키만 교체 — 팀 `verify_service_token` 재사용 검토. 통합 커밋 9 범위 |
| R9 | config 에 profile_* 키 추가 — `profile_summary_max_chars` 는 내 베이스에도 존재(팀이 재배치) | config 충돌 해법 확정: **팀 최종본 전체 채택 + 내 seller_* 블록만 덧붙임**(그 외 일절 추가 금지 — 중복 정의 방지) |
| R10 | 팀 신규: profile 모듈·store, cart 옵션 정합, I-19, tests(test_profile 등), api-spec 미러 v0.15.8~11 | 전부 무수정 수용. 내 api-spec 사본(v0.14)은 EOL 노이즈라 autocrlf 정리로 소멸 — 팀 v0.15.11 유지 |

리뷰로 인한 커밋 계획 수정: **커밋 3 에서 auth.py 제외**(R1). 단 R2~R5 는 팀 모듈(core/stream 등)이
내 베이스(HEAD)에 없어 **커밋 시점에 선반영 불가** — 커밋 1~8 은 현재 코드 그대로(베이스에서 테스트
통과 상태 유지), rebase 충돌 해결에서 R2 처리 후, **rebase 완료 직후 통합 커밋 9
`fix(seller): 팀 SSE 인프라 합류 — open_stream 래퍼·internal_api_token 통일·계약 테스트 정렬`**
로 R3·R4·R5 를 한 번에 반영하고 전체 pytest 로 검증한다.

## 1. 준비 (커밋 전 필수)

```powershell
# (a) 물리 백업 — 워킹트리가 유일본인 상태의 마지막 안전망
Copy-Item -Recurse C:\Users\vssea\jarvis-ai C:\Users\vssea\jarvis-ai-backup-20260720

# (b) 줄바꿈 노이즈 제거 → modified 가 ~19개로 줄어야 함. 안 줄면 중단하고 재진단
git config core.autocrlf true
git status --short

# (c) 포맷-노이즈 3개 복원(내 실질 변경 없음 — 대원칙 2: 무관 파일 불변)
git restore app/api/chat.py app/schemas/chat.py app/services/order_seed.py

# (d) 커밋 제외 확인: _to_delete/ 는 스테이징 금지(미추적 방치), 백업 폴더는 repo 밖
```

## 2. 기능별 커밋 8개 (순서 = 의존 순서, 각 커밋 diff 확인 후 확정)

| # | 커밋 메시지 | 파일 |
|---|---|---|
| 1 | `chore(deps): langchain 1.3 등 판매자 스택 의존성` | `pyproject.toml`, `uv.lock` |
| 2 | `feat(spring): 판매자 Spring 스키마·클라이언트(I-6~I-16, CRUD 4종, 봉투 언랩)` | `app/schemas/spring.py`, `app/services/spring_client.py` |
| 3 | `feat(auth): Spring 패스스루 인증(require_seller_internal)·brandId 클레임·판매자 설정` | `app/api/deps.py`, `app/core/auth.py`, `app/core/config.py` |
| 4 | `feat(seller): 에이전트 코어 — 도구·워커 5종·검증·가드레일` | `app/agents/seller/` 중 `calc·context·middleware·models·prompts·schemas·tools·verifier·workers.py` |
| 5 | `feat(seller): 4단계 — supervisor 라우팅·분석 파이프라인·HITL 실행(4-2)·분석 이력(4-3)` | `orchestrator.py`, `pipeline.py`, `hitl.py`, `history.py`, `__init__.py` |
| 6 | `feat(seller): S-4 SSE 배선 — 입구 선판정 2종·3분기·draft/confirm/적용 레인` | `app/api/seller.py`, `scripts/smoke_seller_chat.py` |
| 7 | `test(seller): 유닛 293종 + InMemory 백엔드 격리 conftest` | `tests/unit/conftest.py`, `test_seller_*.py` 16개, `test_config_seller.py`, `test_health.py`, `test_schemas_camel.py` |
| 8 | `docs(seller): SPEC·REALIGN·핸드오프·FINAL 4종·CHANGELOG` | `docs/specs/*SELLER*`, `REALIGN-*`, `SMOKE-*`, `HANDOFF-*`, `GIT-SYNC-*`, `IMPL-PLAN-*`, `DESIGN-*`, `WORKFLOW-*.png`, `docs/specs/README.md`, `docs/lessons.md`, `docs/mvp-plan.md`, `docs/mvp-todo.md`, `CHANGELOG.md` |

- 근거: 파일 겹침 없음(충돌 시 해당 커밋에서 1회만 처리), 4→5→6 순으로 import 방향 유지.
- 각 커밋: Conventional Commit + 본문(왜) + Claude co-author 트레일러.

```powershell
git branch backup/seller-20260720   # 커밋 완료 직후 백업 브랜치
```

## 3. 통합 — rebase (근거: 새 커밋은 로컬 전용 → 히스토리 안전, abort 가능)

```powershell
git fetch origin
git rebase origin/main
# ⚠️ rebase 중 반전: --ours = origin/main(팀원) / --theirs = 내 커밋
```

## 4. 파일별 충돌 플레이북 (실측 기반 — 예상 충돌 8개)

내 변경이 대부분 **추가**라서 "한쪽 전체 채택"이 아니라 **양측 공존 병합**이 원칙이다(대원칙 3).

| 파일 | 팀 변경(HEAD→origin/main) | 내 변경 | 해법 |
|---|---|---|---|
| `app/services/spring_client.py` | 319줄 — 구매자 함수 개편 | +347 — `SellerSpringClient` 클래스·싱글턴 **추가** | **팀 버전 기반 + 내 Seller 클래스/헬퍼 블록 재적용**. 겹치는 곳은 import·모듈 docstring 정도 |
| `app/schemas/spring.py` | 148줄 — 구매자 모델 개편 | +199 — 판매자 모델 섹션 **추가** | 팀 버전 기반 + 내 판매자 섹션 재적용 |
| `app/api/seller.py` | 41줄 (SSE infra #1 추정) | +419 — 3분기+HITL 전면 재작성 | **판매자 소유 — 내 버전 채택 후**, 팀 41줄 diff 를 열어 인프라 훅(오류 봉투·레이트리밋·수명주기)이 있으면 내 버전에 이식 |
| `app/api/deps.py` | 12줄 | +71 — `require_seller_internal` 추가 | 양측 추가 모두 유지 |
| `app/core/auth.py` | brandId 이미 반영(R1) | +14 — 동일 취지 | 커밋 3 에 포함(제외 시 중간 커밋의 identity.brand_id 참조가 깨짐) — **rebase 충돌 시 팀 것(--ours) 채택**(동치) |
| `app/core/config.py` | +16키(stream·ratelimit 등) | +50 — seller_* 키 | 양측 유지하되 **중복 2키 정리**: 내 `spring_timeout_s` 제거(R2)·내 `internal_token` → 팀 `internal_api_token` 통일(R3) |
| `tests/unit/test_health.py` | 6종(OpenAPI 표면 고정 포함) | +35 — 400/스트림 검증 | 팀 6종 유지 + 내 판매자 검증 추가. 단 `test_seller_chat_requires_seller_scope` 는 **403→401 새 계약으로 수정**(R5) |
| `tests/unit/test_schemas_camel.py` | (팀 변경 있음) | +38 — 판매자 모델 camel 추가 | 양측 추가 모두 유지 |
| `uv.lock` | 다수 | 다수 | **팀 것 채택(--ours) → rebase 완료 후 `uv lock` 재생성 → 별도 커밋** `chore(deps): uv.lock 재생성` |
| `CHANGELOG.md`·`docs/mvp-*`·`lessons.md` | (팀 미변경 확인됨 — 충돌 없을 것) | 항목 추가 | 충돌 시 양측 항목 병합 |

주의: 커밋 2·3 rebase 시점에 팀의 신규 함수가 내 코드와 같은 위치에 있으면 **팀 코드를 절대 지우지 말 것** — 이해 안 되는 hunk 는 중단하고 diff 전체를 다시 읽는다(대원칙 1).

## 5. 검증 (양쪽 모두 작동 — 대원칙 3 게이트)

```powershell
uv sync                          # 팀 의존성 합류(cart/recommend/obs 신규 패키지)
uv run ruff check --fix ; uv run ruff format
uv run pytest                    # 전체 — 판매자 293종 + 팀원 테스트 전부 통과해야 함
uv run uvicorn app.main:app --reload   # 부팅 스모크(팀 SSE infra 와 seller 라우터 공존 확인)
```

팀 테스트 실패 시: 내 통합 실수다 — 팀 코드를 고치지 말고 **내 병합을 고친다**. 판매자 테스트 실패 시: 팀 인프라 변경(오류 봉투 등)에 내 코드를 맞춘다(판매자 파일만 수정).

## 6. Push & PR

```powershell
git push -u origin feat/seller-tools   # 인증 문제 시 사용자 직접 실행
# GitHub: main 대상 PR — 리뷰 1인 + CI. 본문에 SELLER-FINAL 4종 링크.
```

merge 후: `git checkout main ; git pull --ff-only`.

## 7. 이 계획이 v1(0719)과 다른 점

1. 4-2(hitl)·4-3(history)·conftest·FINAL 문서 4종·GIT-SYNC 문서 자체가 커밋 대상에 추가 — 6커밋 → 8커밋.
2. 포맷-노이즈 3파일(chat.py·schemas/chat.py·order_seed.py) 복원 발견 — 충돌 후보 11→8.
3. 충돌 해법을 "Seller=theirs / 무관=ours" 이분법에서 **파일별 공존 병합 플레이북**으로 구체화 — 팀도 나도 같은 파일에 '추가'를 했기 때문(전체 채택은 어느 쪽이든 코드 소실 = 대원칙 1 위반).
4. `_to_delete/` 커밋 금지 명시. 검증에 서버 부팅 스모크 추가.
