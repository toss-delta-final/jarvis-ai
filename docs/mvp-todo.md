# MVP TODO (주제별 체크리스트)

구현 계획은 [mvp-plan.md](mvp-plan.md). 범례: `[ ]` 미착수 · `[~]` 진행 중 · `[x]` 완료 · 🔴 Spring 협의 선행.

> **[2026-07-27 전수 대조]** 체크박스를 `app/`·`tests/` 실측과 대조해 일괄 정정했다. 직전까지
> 미체크(`[ ]`/`[~]`)로 남아 있던 55개 항목 중 **52개가 이미 구현·테스트 완료** 상태였다
> (구현 후 체크박스를 갱신하지 않아 누적된 drift — [lessons.md](lessons.md) 참조).
> 남은 미구현은 **시맨틱 캐시(#122) · 분석 기준서 RAG(#91) · 차트(SPEC §12 보류)** 3건뿐이다.
> **기능이 병합되면 이 파일의 체크박스를 같은 PR 에서 갱신한다** — CHANGELOG 와 동일 규칙.
>
> **[2026-07-27 계약 대조 — 위 "3건뿐" 정정]** 정본(기획 저장소 Notion "📡 API 명세서" DB) 75행을
> Spring·AI 코드와 1:1 대조해 **체크리스트에 항목조차 없던 미구현 2건**을 추가로 발견했다. 위
> 전수 대조는 "체크박스가 코드보다 뒤처진 것"(완료를 미착수로 오판)만 잡았고, 이번 건은 **반대
> 방향** — 계약에는 있는데 체크리스트에 없어 조용히 "완료"로 오인되던 것이다.
>
> - **#163** — 총액 예산(Case 3) 산출 + `budget` SSE emit. `app/` 에 `budget`·`knapsack`·
>   `verifiedSum`·`price_scope` **0건**(구매자 SSE 7종 중 이것만 미구현). #60 은 이 baseline 을
>   전제한 확장 이슈라 #163 이 선행 의존이다.
> - ~~**#164** — 주문상태 문의(I-4)~~ **구현 완료**. 구매자 intent를
>   `recommend`/`cart_add`/`cart_view`/`order_status`/`general` 5종으로 확장하고,
>   I-4 client·엄격 schema·결정적 formatter/stream handler·구매자 graph early return을 배선했다
>   (`agents/buyer/recommendation/decompose.py`, `schemas/spring.py`,
>   `services/spring_client.py`, `agents/buyer/order_status.py`, `agents/buyer/graph.py`;
>   api-spec §4.10).
>
> 또한 **#91 은 구현 없이 COMPLETED 로 닫혀 있어 재오픈**했다 — `search_analysis_guide` 는 여전히
> `NotImplementedError` 스텁이며 이 저장소에 남은 유일한 스텁이다.

---

## 🚦 착수 우선순위

MVP 골격(인프라·구매자·판매자·프로필·배치)은 배선 완료다. 남은 것은 **품질·확장**이라 우선순위를
다음으로 둔다.

1. **구매자 추천 품질** — ~~#100(I-1 계약 정합)~~ **완료(PR #127)** → #101(pgvector 2차 압축 복구). 데모 핵심.
2. **계약에만 있고 구현 없는 것** — #163(`budget` SSE)
3. **구매자 버그** — #119(취향 과반영) · #115(카테고리 추측 정확도) · #120(재추천 차단)
4. **판매자 확장** — #91(분석 기준서 RAG, 재오픈) · #122(시맨틱 캐시)
5. **Spring 협의 잔여** — 아래 표. 대부분 해소, 남은 것은 운영 정책 성격.

---

## 0. 공통 인프라

- [x] RS256+JWKS 인증 · dev 모드 · deps
- [x] config 주입 · 부팅 검증 · 스텁 스트림
- [x] SSE 동시 스트림 제한 — 세션당 1개, `409 STREAM_IN_PROGRESS`, in-memory 레지스트리 (§2.9a) — `core/stream.py` `ActiveStreamRegistry`
- [x] 요청 취소 — `is_disconnected()` 감지 → LLM 스트림 close + task cancel (§2.9b) — `core/stream.py`
- [x] 타임아웃 — first-token 10s / 상한 90s / AI→Spring 3s / LLM 30s+1재시도 (§2.9c) — `core/config.py` 주입
- [x] 레이트 리밋 미들웨어 — 토큰 스코프, 분당 10·시간당 100 (§2.8) — `core/ratelimit.py` + IP 백스톱
- [x] 스트림 전 오류 봉투 코드 전체 (400/401/403/409/429/504) (§2.5) — `core/errors.py`

## 1. 구매자 추천 그래프

- [x] 그래프 스캐폴드(intent router 분기) — `agents/buyer/graph.py` **9-way 라우팅** (`recommend` / `cart_add` / `cart_view` / `order_status` / `general` / `cart_remove` / `wishlist_add` / `wishlist_remove` / `wishlist_view`). 최초 5종에서 #116·#117 이 3종, #386 이 `wishlist_view` 를 더했다 — 정본은 `RouteDecision.intent` Literal 이다
- [x] **#164 주문 상태 문의(I-4)** — `recommendation/decompose.py` `order_status` 분류 → `spring_client.py:get_order_status` (`recent=3`) → `order_status.py` 엄격 검증·결정적 요약 → `graph.py` early return; 계약 api-spec §4.10, 회귀 `tests/unit/test_order_status.py`·`tests/integration/test_buyer_flow_e2e.py`
- [x] `search_products` Spring 연결 (§4.6) — I-1 배선. **잔여 계약 정합은 #100**
- [x] decompose 노드 (구조화 필터 + 키워드) — `recommendation/decompose.py`
- [x] rerank 노드 (프로필 반영 + 근거 생성) — `recommendation/rerank.py`
- [x] `push_recommendations` → `products.ready` emit (경로 B, §4.2) — I-21 배선
- [x] `conditions` 칩 emit + 규약 문자열 재분해 — `recommendation/state.py` `build_condition_chips`
- [x] degrade: SEARCH_FAILED / rerank 폴백 / push 실패 처리 — leg 단위 격리 포함
- [x] 폴백 서브그래프 (무관 질문 · 일반 대화) — `agents/buyer/fallback/`
- [ ] pgvector 2차 압축 복구 — Spring 전량 반환 → AI top-K (#101, 선행 #100)

## 2. 장바구니 (I-2 · I-18)

- [x] intent 추출 노드 (상품 · 옵션 · 수량) — decompose 통합 (`CartIntent`)
- [x] `add_to_cart` I-2 연결 (§4.1) — 재고부족 `STOCK_INSUFFICIENT` 포함(이슈 #74)
- [x] 게스트 담기 허용 (userId|guestId 분기) — `cart/graph.py` `cart_identity`
- [x] 옵션 되물음 멀티턴 (`CART_OPTION_REQUIRED` → token 재질문 → 재담기)
- [x] `get_cart` I-18 연결 — 장바구니 질의 응답 (§4.9)
- [x] 담기 전 기존 보유 조회 → 합산 안내 (조회 실패 시 degrade)
- [x] SSE `action` 매핑 (CART_ADDED / CART_ADD_FAILED reason)

## 3. 구매 이력 & dedup

- [x] `get_recent_purchases` 연결 (§4.7) — I-19 배선
- [x] dedup: exact 제외 — I-1 이 `excludeProductIds` 미지원이라 AI 사후필터로 적용
- [x] 소모품 카테고리 억제 + 되돌리기 `suggestions` 칩 — `RevertStore` 누적
- [x] 게스트 스킵 · 실패 시 degrade(dedup 없이 진행)

## 4. 판매자 그래프 (SPEC-SELLER-001)

- [x] `/seller/chat` 스캐폴드 (role=seller 403) — 스트림 수명주기는 공통 `open_stream` 사용
- [x] spring_client 판매자 함수군 — 집계 7종(I-6~I-16) + CRUD I-9/10/11/12 (§4.4·§4.5)
- [x] supervisor 구조화 출력 라우팅 (analysis / product / general) — confidence 미달 시 보수 재지정
- [x] general_agent — 읽기 도구 + 계산기 (쓰기 도구 배정 금지)
- [ ] 분석 기준서 문서 작성 → `seller_kb` 인제스트 → `search_analysis_guide` @tool (pg-catalog pgvector) — **#91**, 현재 스텁(degrade 문구 반환)
- [x] 분석 서브그래프 — planner(이력·기간 정규화) → 팬아웃 워커 5종 → report (SPEC §2·§5)
- [x] AI-측 고도화 계산 모듈 — 이동평균·이상 판정·전환율 비교, 임계값 config 주입 (`seller/calc.py`)
- [x] report_verifier 검증 루프 (결정론 D1~D3 → LLM 채점, ≤3회)
- [x] recommend_agent (구조화 RecommendationSet, 읽기 전용) + compose + 진행 상황 `token`
- [x] 분석 이력 저장·조회 — pg-profile PostgresStore (SPEC §9.1), planner 입력 주입
- [x] product_agent — **전 쓰기 HITL**: `draft{draftId}` emit → interrupt → 구조화 `confirm{draftId}` resume → I-10/11/12 (SPEC §6) — 소유권·TTL·멱등·stale 5중 검증
- [x] 추천 적용 흐름 — 저장된 recommendations 조회 → draft 경유 (코드 선판정, LLM 0회)
- [x] 가드레일 (scope/PII/출력 검사) — `seller/middleware.py` 3층
- [ ] 시맨틱 캐시 `question_cache` — **#122**
- [ ] 차트 — **보류** (전달 계약 🔴, SPEC §12) — `chart_data_hint` 필드만 보존

## 5. 프로필 파이프라인

- [x] reader/builder/gate — 스텁 해소, 전부 구현
- [x] reader — 압축 요약 로드 (LLM 0회, 게스트/신규는 None)
- [x] 승격 게이트 (반복성·현저성·명시성 3조건 + "기억해" hot-path)
- [x] transient 격리 (session_context) — `store.py` 네임스페이스 분리
- [x] 세션 종료 델타 → sleep-time consolidation 병합 — 공통 finalizer
- [x] PostgresStore 네임스페이스 (profile/facts — 이슈 #33, pgvector 시맨틱 인덱스는 facts 전용. episodes 는 MVP 미구현·고도화 범위)
- [x] `POST /events/session-end` 수신 (§3.5, 멱등 — 이슈 #62/#64, C-8 해소)
- [x] AI 내부 inactivity timeout 스케줄러 — 회원 발화 활동시각 DB upsert, 기본 10분/60초 bounded sweep, 재개 가능한 idle checkpoint·공통 finalizer·claim별 실패 복구 (이슈 #79)

## 6. AI 생성물 배치 (I-17)

- [x] `fetch_product_changes` 연결 + hasMore 루프 (§4.8)
- [x] 커서 영속화 (다음 주기 since) — 페이지 성공 후에만 전진
- [x] `HIDDEN` 처리 (생성물 삭제) — 이슈 #63
- [x] enrichment 노드 활성화 (Haiku 배치)
- [x] build_search_doc + embed_texts 활성화 — gemini 임베딩, 프로비넌스 기록(이슈 #65)
- [x] AI Postgres upsert + 초기 전체 구축(커서 0) — `INVALID_CURSOR` 시 자동 rebuild(원자 교체)

## 7. 대화 저장 & 모니터링

- [x] 대화 저장 (user 수신 즉시 / assistant 완료 후) — pg-profile `conversation_turns` 일반 테이블(이슈 #33, checkpointer 아님 — 감사·조회용)
- [x] 저장 상태 COMPLETED/FAILED/CANCELLED + 부분 텍스트 보존
- [x] 구조화 로그 필드 (requestId·latency 2종·model·tokens·errorType)
- [x] PII: message 원문 로깅 금지 (길이 + HMAC 지문)

---

## 🔴 Spring 협의 대기 (블로킹)

**[2026-07-27 갱신]** 대부분 해소됐다. AI 배선이 끝났고 실동작하는 항목은 ✅ 로 표시한다.

| 항목 | 계약 | 상태 |
|---|---|---|
| C-15 | I-1 후보 검색 (검색 위임) | ✅ 배선 완료. **잔여 필드 정합은 #100** (`rating`·`price` 반환 여부) |
| C-6 | 구매 이력 조회 | ✅ I-19 로 해소 |
| C-3 | I-2 잔여 (재고 코드·options 스키마·서비스 토큰) | ✅ 해소 (이슈 #74 `STOCK_INSUFFICIENT`) |
| C-16 | 장바구니 조회 응답 필드 | ✅ I-18 로 해소 |
| C-13 | 집계 7종(I-6~I-16) 응답 스키마 + 계산 경계표 | ✅ 배선 완료 (`seller/tools.py`·`calc.py`) |
| C-14 | CRUD 4종 응답 스키마 · HITL confirm 전송 형식 | ✅ 해소 (`seller/hitl.py`) |
| C-4 | 변경분 pull | ✅ I-17 로 대체·해소 (이슈 #63/#76) |
| C-9 | 목록 push 응답(listId) | ✅ I-21 로 해소 — `listId` 는 AI 생성 |
| C-12 | 목록 GET (Spring 소유) | ✅ CH-5 구현 확인 (BE `ChatController#getList`) |
| C-5 | productId 타입 통일 | ✅ 숫자(BIGINT) 확정 |
| C-1 | 인증 — role 값·TTL·판매자 클레임·CH-1b | ✅ **BE 실측 확정**(2026-07-27): `iss/aud/scope` = `jarvis-spring-auth`/`jarvis-fastapi-ai`/`chat:stream`, 티켓 TTL 60s, 판매자 `role="seller"`(소문자)+`brandId`(숫자), **CH-1b `POST /api/chat/tickets` 구현됨**. 🔴 잔여는 **서비스 토큰 회전·만료·mTLS 운영 정책**뿐 |
