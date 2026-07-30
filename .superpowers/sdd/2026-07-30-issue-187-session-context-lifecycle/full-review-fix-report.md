# Issue #187 전체 리뷰 수정 보고서

## 범위

전체 리뷰의 Important 3건(JWKS buyer fail-open, PostgreSQL 장애 wire 계약, raw 식별자 로그)과 Minor 1건(sweep/backfill 관측성)을 TDD로 수정했다.

## RED

- JWKS 구매자 티켓에서 `sub_type`을 제거하고 `role=GUEST`, `role=USER`, `role=UNKNOWN`을 넣은 실제 RS256/JWKS `/chat` 요청이 모두 잘못된 `200`을 반환함을 확인했다: **3 failed, 1 passed**.
- buyer 구조화 로그가 raw owner/session/thread를 포함해 fingerprint-only assertion이 실패함을 확인했다: **1 failed**.
- scheduler 로그에 `claimed`, `superseded_skipped`, `invalid_recovery`, `examined_limit_reached`가 없어 assertion이 실패함을 확인했다: **1 failed**.
- `/chat` atomic turn과 `/events/session-claim`에 `TimeoutError`/`psycopg.OperationalError`를 주입했을 때 `SessionStateUnavailable`로 변환되지 않고 예외가 전파됨을 확인했다. ProgrammingError도 같은 경로에서 별도 분류되지 않는 현재 상태를 기록했다.

## GREEN

- JWKS 모드는 정확한 `role="seller"`만 seller 예외로 허용하고, 그 외 buyer는 정확한 `sub_type=guest|member`를 필수화했다. dev 모드는 legacy role 호환을 유지한다.
- `TimeoutError`, `PoolTimeout`, `psycopg.OperationalError`만 state-store unavailable로 분류한다. buyer conversation initialization/atomic turn 및 claim API 경계에서만 `SessionStateUnavailable`로 변환하며 programming/constraint/domain 오류와 `CancelledError`는 변환하지 않는다.
- 실제 PostgreSQL transaction에서 touch/claim mutation 뒤 OperationalError를 발생시켜 공개 endpoint가 `503 STATE_UNAVAILABLE`을 내고 context/thread/turn/owner history가 rollback되는 것을 확인했다.
- buyer chat/lifecycle/stream 로그의 raw owner/session/thread/stream key를 peppered HMAC 기반 `ownerFp`, `sessionFp`, `threadFp`, `streamFp`, `contextFp`로 교체했다.
- scheduler에 모든 lifecycle 집계 필드를 추가하고 backfill의 batch/pass/cursorFp/completed/grace 설정 상태를 raw cursor 없이 기록한다.
- `docs/api-spec.md`와 `SPEC-CHAT-SESSION-CONTEXT-187.md`를 실제 인증·로그 정책에 정렬했다.

## 검증

- focused auth/chat/claim/observability/scheduler: **146 passed**
- 관련 actual PostgreSQL focused: **4 passed**
- 전체 PostgreSQL integration: **134 passed, 1313 deselected**
- 전체 pytest: **1311 passed, 136 deselected**
- `uv run ruff check .`: 통과
- `uv run ruff format --check .`: **180 files already formatted**
- `git diff --check`: 통과

## 잔여 위험

- 실제 운영에서 PostgreSQL 프로세스를 외부에서 강제 중단하는 chaos test는 수행하지 않았다. 대신 실제 PostgreSQL transaction과 통제된 psycopg OperationalError를 사용해 rollback/wire 계약을 검증했다.
- TestClient/httpx 조합의 기존 deprecation warning 1건은 이번 변경과 무관하다.
