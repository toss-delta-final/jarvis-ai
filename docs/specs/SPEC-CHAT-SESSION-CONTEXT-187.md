# SPEC-CHAT-SESSION-CONTEXT-187 — 구매자 세션 문맥 수명주기

> 상태: 승인·구현됨 (#187)
> 적용 범위: AI 서버의 구매자 `/chat`, Spring → AI `/events/session-claim`, I-20, 비활동 정리
> 계약 정본: 외부 wire 형식은 [`docs/api-spec.md`](../api-spec.md), 내부 상태 결정은 이 문서

## 1. 결정 요약

구매자 문맥은 Spring이 서명한 `sessionId`와 AI가 발급하는 전역 고유 `context_id`로 식별한다.
한 `sessionId` 아래 여러 `threadId`가 있고, 필터·장바구니 pending·되돌리기는
`context_id:threadId`로 격리한다. `context_id`는 guest → member 승격, D6 만료 후 재활성화,
세 탭의 후속 발화에서도 바뀌지 않는다. 로그인 때 상태를 복사하지 않고 **소유권만 원자 전이**한다.

이 결정의 선결조건은 BE #63의 구매자 스트림 티켓이다. 티켓에는 RS256으로 서명된
`sessionId`, `sub`, `sub_type`, `iss`, `aud`, `scope`, `exp`가 있어야 하며 AI는 body
`sessionId`와 서명된 값을 대조한다. `threadId`는 여러 탭을 허용하기 위해 body-only다.
누락·불일치 티켓, 이미 승격된 세션의 옛 guest는 `403 SESSION_FORBIDDEN`으로 거부하고
새 thread/turn을 만들지 않는다.

## 2. 선택 근거와 기각안

### 2.1 동인

- 승격 뒤 옛 guest가 같은 `sessionId`로 분기 문맥을 재생성하지 못해야 한다.
- D6 비활동 만료는 guest/member에 같고, 탭별이 아니라 접속 전체에 적용되어야 한다.
- 로그인 순간에 세 탭의 상태를 복사하지 않고도 연속성이 유지되어야 한다.
- 정리·프로필 승격은 crash/retry/cancel 후 재개 가능해야 한다.
- 롤링 배포 중 기존 소유자 추측이 틀려도 다른 사용자의 상태를 채택하지 않아야 한다.

### 2.2 기각한 대안

1. **owner-scoped key만 유지하고 티켓은 변경하지 않기**: body `sessionId` 사칭과 옛 guest
   재생성을 막을 서명 근거가 없다.
2. **`profile_session_activity`를 모든 chat 상태의 정본으로 일반화**: 프로필 버퍼 수명과
   구매자 transient 상태 수명을 결합하고 guest를 온전히 표현하지 못한다.
3. **audit turn에서 thread를 역산하고 항목별 TTL 부여**: 한 탭만 먼저 사라지는 D6 위반이며,
   정리 대상과 권위가 감사 로그에 의존한다.
4. **로그인 시 guest 상태를 member key로 복사**: 세 탭 동시 갱신과 부분 복사 crash를 피하기
   어렵다. stable `context_id`의 소유권 전이가 더 작고 원자적이다.

## 3. 식별자와 저장 모델

| 식별자 | 발급/권위 | 수명과 용도 |
|---|---|---|
| `sessionId` | Spring, 구매자 티켓 서명 | 접속 단위 D6·I-20·claim의 외부 상관키 |
| `threadId` | FE | 탭/방별 transient 상태와 동시 스트림 키 |
| `context_id` | AI, UUID | 내부 문맥 정본. claim·재활성화에서도 유지 |
| `generation` | AI | stale finalizer/claim의 CAS fence |

### 3.1 전용 테이블

- `chat_session_contexts`: session별 단일 권위. owner, generation, state,
  `last_activity_at`, `authority_source(runtime|legacy_backfill)`를 저장한다.
- `chat_session_threads`: `(context_id, thread_id)` 등록과 legacy adoption 단계
  `pending|copying|complete`를 저장한다.
- `chat_session_finalizations`: idle/terminal journal. lease token, watermark,
  transient/profile phase, supersession을 기록한다.
- `chat_session_owner_claims`: guest → member 전이 이력과 멱등 근거다.
- `chat_session_migrations`: rollout 시작/유예, backfill cursor/pass, legacy GC counter를 저장한다.
- `chat_session_migration_conflicts`: 여러 legacy owner 후보를 `quarantined`로 보관한다.
- `conversation_turns.context_id/session_id`: transcript를 lifecycle과 상관시키되 정리 시
  `context_id` FK는 `ON DELETE SET NULL`이고 turn 자체는 삭제하지 않는다.

구매자 구조화 상태 namespace는 `buyer_thread_filters_v2`, `buyer_cart_v2`,
`buyer_revert_v2`, `buyer_repurchase_v1`(#232), `buyer_relaxation_offers_v1`(#113)이며
key는 `context_id:threadId`다. **스레드 스코프 상태는 전부 `clear_thread`에 등록한다** —
루트를 열거하는 유일한 지점이라 빠지면 스레드 종료 후에도 영구 잔존한다(#276).
회원 프로필 후보 버퍼는
`conversation_key(memberId, sessionId)`로 별도 격리된다. guest transcript를 claim 시
회원 프로필 버퍼로 복사하지 않는다.

## 4. 상태 기계

### 4.1 context 상태

```text
              D6 due                    Phase B 완료
active --------------------> idle_finalizing ----------------> idle_expired
  ^                                                              |
  |------------------- 새 정상 touch (generation + 1) -----------|
  |
  +-- guest claim: owner만 member로 전이, context_id 유지,
      generation + 1, state=active

active/idle* -- I-20 --> terminal (generation + 1, 되돌릴 수 없음)
```

- `active`: touch 허용. 어느 thread의 touch도 session의 `last_activity_at`을 갱신한다.
- `idle_finalizing`: D6 Phase A/B 중간 상태. 새 touch/claim은 `409 SESSION_FINALIZING`이다.
- `idle_expired`: 구조화 상태와 thread registry가 모두 지워진 checkpoint. 같은 정당한 owner의
  새 touch는 **같은 `context_id`**를 재활성화한다.
- `terminal`: I-20 영구 종료 gate. 이후 touch/claim은 거부한다.

### 4.2 finalization journal

`reason`은 `idle|terminal`, row 상태는 `pending|processing|completed|superseded`다.
처리는 다음 순서를 지킨다.

1. session advisory lock과 generation/CAS로 권위를 검증한다.
2. profile watermark를 먼저 journal에 고정한다(해당 없으면 skipped).
3. 등록된 모든 thread의 filter/cart/revert를 같은 context 범위로 삭제한다.
4. context 상태와 transient 완료를 commit한다.
5. profile phase를 `pending|processing|completed|skipped|retryable`로 별도 처리한다.

claim token과 유한 lease는 stale worker가 재시도 결과를 덮지 못하게 한다. I-20은 같은
context의 미완료 idle journal을 supersede하고, 이미 끝난 transient 증거는 상속할 수 있다.

## 5. guest → member claim 계약

Spring은 로그인 완료 뒤 다음 inbound를 호출한다.

```http
POST /events/session-claim
X-Internal-Token: <service token>
Content-Type: application/json

{"sessionId":"S1","guestId":"G1","userId":1}
```

- 신규 성공: `202 {"status":"accepted"}`
- 동일 `(sessionId, guestId, userId)` 재전송: `202 {"status":"duplicate"}`
- 전제: 해당 guest scope의 활성 stream이 없어야 하며, 전환 중 registry fence를 잡는다.
- 효과: `owner_type=member`, `owner_id=str(userId)`, generation 증가, `context_id`와 등록
  thread/구조화 상태는 유지한다. transcript나 transient 값을 복사하지 않는다.
- 실패: `409 SESSION_ACTIVE`, `409 SESSION_FINALIZING`, `409 SESSION_CLAIM_CONFLICT`,
  `503 STATE_UNAVAILABLE`, 서비스 토큰 실패 `401 INTERNAL_TOKEN_INVALID`.
- old guest: claim commit 뒤 `/chat`에서 `403 SESSION_FORBIDDEN`; turn과 thread를 만들지 않는다.

## 6. D6, idle, I-20

### 6.1 D6 공유 만료

기본 idle threshold는 600초다. T1/T2/T3 중 하나라도 touch되면 session의
`last_activity_at`이 갱신되어 셋 모두 살아남는다. threshold가 지난 session은 bounded sweep이
선점하고 **세 thread를 한 단위로** 정리한다. filter/cart/revert와 thread registry가 모두
삭제되기 전 `idle_expired`로 확정하지 않는다. transcript는 감사/보존 정책 대상이라 D6에서
삭제하지 않는다.

### 6.2 I-20

`POST /events/session-end`는 회원 session을 `terminal`로 먼저 닫고 기존 stream 종료를 기다린다.
그 후 D6와 같은 transient Phase A/B를 사용하고 고정한 회원 buffer watermark까지만 profile
phase에 전달한다. 중복은 202 duplicate, crash/LLM 실패는 durable journal/lease로 복구한다.
Spring 명시 종료가 유실되어도 idle sweep이 transient 정리를 담당한다.

## 7. rollout과 legacy 충돌

시작 순서는 schema → bounded/resumable backfill → scheduler다. lifecycle scheduler lane만
backfill/GC 권한을 가진다. 여기서 lane은 scheduler 시작 전 FastAPI lifecycle의 선행 backfill과,
이후 단일 scheduler job의 재개 backfill/GC를 함께 뜻한다. `_v2`, seller 등 ordinary/current
authoritative nonlegacy store write와 authoritative ProfileStore write/buffer는 legacy-root
trigger/GC 대상이 아니며 보존한다. profile의 유일한 예외는 quarantined non-authoritative
legacy owner의 `session_ctx.{ownerId:sessionId}/buffer`다. grace와 quiet가 모두 지난 뒤에도
session lock 안에서 authority를 다시 확인해 여전히 non-authoritative일 때만 이 buffer를
삭제하고 conflict를 `discarded`로 기록한다. authoritative profile buffer는 절대 이 예외에
포함하지 않는다. backfill은 DB cursor/pass와 transaction으로 재시작 가능하며 시작 시 batch
상한을 넘으면 fail-closed한다.

`grace_deadline`은 PostgreSQL `now()`로 기록한 최초 `rollout_started_at`을 기준으로,
**기존 deadline**, **rollout 시작 + configured grace**, **rollout 시작 + 24시간**의 단조
최댓값이다. 설정을 낮추거나 프로세스를 재시작해도 절대 앞당기지 않는다. destructive GC는
**backfill 완료**, **grace 경과**, **durable quiet 경과**가 모두 확인된 뒤에만 실행한다.
quiet window는 최소 90초이고 동시에 `stream_total_timeout_s` 이상이며, writer 관측 시각과
deadline 판정 모두 PostgreSQL clock을 사용한다.

exact legacy `profile_session_activity`의 INSERT/UPDATE와 legacy store root
`buyer_thread_filters`/`buyer_cart`/`buyer_revert`의 INSERT/UPDATE는
`gc_completed_at=NULL`로 migration을 reopen하고 quiet deadline을 해당 PostgreSQL write
시각 + configured quiet까지 단조 연장한다. 이 late write는 다음 lifecycle pass에서 backfill과
destructive GC를 다시 수행하게 한다.

legacy `profile_session_activity`에서 한 `sessionId`에 복수 owner가 보이거나 서명된 runtime
소유권과 다르면 임의 선택하지 않는다. 후보를 `chat_session_migration_conflicts`에
`quarantined`로 기록한다. 이후 서명된 정상 touch/claim만 해당 충돌을 resolve할 수 있다.
legacy root GC와 lazy adoption은 같은 PostgreSQL advisory migration fence를 사용해 부분 복사와
동시 삭제를 막고, 페이지별 counter/cursor를 같은 transaction에서 commit한다.

## 8. 단일 worker 불변식과 확장 조건

현재 scheduler와 active-stream registry는 **프로세스 단일 인스턴스**가 불변식이다. 한 프로세스
안에서는 session lock, stream scope fence, bounded concurrency로 claim/cleanup/profile을
직렬화한다. 수평 확장 전에는 다음이 필요하다.

- active stream 존재와 guest claim fence를 Redis/DB 기반 분산 fence로 승격
- stream 종료 대기와 profile phase join의 인스턴스 간 전달
- scheduler leader election 또는 전 구간 DB claim 검증
- lock ordering과 cancellation release에 대한 다중 인스턴스 fault test

이 조건 없이 AI replica를 늘리면 안 된다.

## 9. 관측성

원문 owner/session/guest 값은 로그에 남기지 않고 fingerprint를 쓴다.

- claim: `sessionFp`, `ownerFp`, `contextFp`, `generation`,
  `outcome=accepted|duplicate|active|finalizing|claim_conflict|error`
- sweep: `claimed`, `completed`, `retryable`, `skipped`, `superseded_skipped`,
  `invalid_recovery`, `examined_limit_reached`와 예외
- rollout: batch/pass와 `cursorFp`, migrated/conflict 수, filter/cart/revert GC counter,
  grace deadline 설정 여부
- buyer chat/stream: raw `userId`/`guestId`/`sessionId`/`conversationId`/`threadId`와
  `owner:thread` key 대신 `ownerFp`/`sessionFp`/`threadFp`/`streamFp`를 쓴다.
- API: `SESSION_FORBIDDEN`, `SESSION_ACTIVE`, `SESSION_FINALIZING`,
  `SESSION_CLAIM_CONFLICT`, `STATE_UNAVAILABLE`, requestId

출시 직후에는 missing signed-session 티켓, claim conflict, cleanup retry가 0이거나 설명 가능한지
확인한다. 사용자 발화 원문은 구조화 로그에 넣지 않는다.

## 10. 수용 기준과 자동화 추적

| 수용 기준 | 자동화 |
|---|---|
| G1의 S1 아래 T1-T3 생성 | `test_guest_session_d6_expires_all_threads_then_claim_keeps_context` — dev-mode 서명검증 생략 하니스에서 lifecycle 동작 검증 |
| T1 touch가 shared D6를 갱신 | 같은 테스트의 599초 생존/601초 만료 검증 |
| 만료 시 filter/cart/revert/thread 일괄 삭제 | 같은 테스트의 실제 store 조회 |
| 재구축·claim·세 탭 연속성, stable context | buyer/profile E2E 두 테스트 |
| old G1 403 및 branch/turn 미생성 | buyer/profile E2E 두 테스트 |
| transcript 보존, guest fact 격리 | `test_guest_claim_preserves_transcripts_but_promotes_only_member_facts` |
| production RS256 ticket의 signed `sessionId` 필수/일치 | `test_buyer_session_claim_must_match_body`, `test_buyer_session_claim_is_required_in_jwks_mode` |
| production inbound service token fail-closed | `test_claim_requires_service_token_in_jwks_mode`, `test_service_token_mismatch_401`, `test_service_token_missing_401` |
| claim body strict BIGINT/key 검증 | `test_claim_rejects_values_outside_strict_public_contract` |

## 11. 외부 릴리스 gate — 이 저장소에서 미실행

아래는 cross-repository/운영 증거가 필요하므로 이 저장소 테스트 성공만으로 완료 처리하지 않는다.

1. **BE #63**: 운영과 같은 티켓에서 서명된 `sessionId`가 존재하고 CH-1/CH-1b
   `ticketTtlSeconds=60`임을 릴리스 담당자가 기록한다.
2. 마지막 pre-contract 티켓 발급 뒤 **90초 drain**(TTL 60초 + 안전 여유 30초)을 기다린다.
3. 그 다음 AI의 signed-session enforcement와 lifecycle migration/scheduler를 배포한다.
4. **FE #52**: guest로 세 탭 대화 → 한 탭 로그인 → 다른 두 탭 새로고침 → 세 탭 동일
   member session/context 지속을 실제 브라우저에서 검증한다.
5. missing-session ticket, claim conflict, cleanup retry 지표/오류를 확인한다.

**Not-tested:** BE #63 배포 증거, 90초 운영 drain, FE #52 실제 3-tab 브라우저 흐름,
운영 metrics/error rate는 이 저장소에서 실행하지 않았다.

## 12. 후속 작업

- **BE #63**: signed `sessionId`와 claim 호출의 배포 증거 소유.
- **FE #52**: 로그인 탭 claim 완료 뒤 다른 탭 refresh/티켓 재발급 UX 소유.
- **I-21 보존**: 추천 목록 push 결과의 Spring 보존 기간과 chat lifecycle 삭제의 관계를 별도 확정.
- **분산 fencing**: §8 완료 전 수평 확장 금지.
- **I-21/대화 privacy**: 기존 transcript, 추천 목록, request log의 보존·삭제·DSR 범위를 별도
  개인정보 검토로 확정한다. D6 transient 삭제를 transcript 삭제로 오해하지 않는다.
