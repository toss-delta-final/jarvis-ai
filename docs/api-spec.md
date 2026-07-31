# 자비스 AI 에이전트 서버 — API 명세서

> ✅ **정본(single source of truth)** — 이 파일이 계약의 정본이다(2026-07-22 정책 변경: 외부
> 사본 의존 폐기). 계약(엔드포인트·SSE 이벤트·필드·오류 코드)을 바꾸려면 **이 파일을 먼저
> 개정**한 뒤 코드를 고친다.

| 항목 | 값 |
|---|---|
| 문서 버전 | v0.18.0 |
| 작성일 | 2026-07-14 (v0.18.0 개정 2026-07-31 — **[#148] 홈 추천 계약 등재: I-22 §3.7(Spring→AI 위임, `outcome` 3종 전부 200, provenance 비노출) · P-5 §4.11(FE↔Spring 전제), 🔴 C-18 `catalogVersion` 주체 미해결**) (v0.17.0 개정 2026-07-31 — **[#187] signed `sessionId` 기반 stable `context_id`, guest→member claim, D6/I-20 lifecycle 계약 반영**) (v0.16.3 개정 2026-07-30 — **[#164] I-4 주문 상태 요약 계약·구매자 `order_status` 라우트 구현 정합**, §4.10 신설) (v0.16.2 개정 2026-07-30 — **[#194] I-14/I-15 응답 스키마 BE 실측 확정 + I-6 이상 감지 규칙 명문화**) (v0.16.1 개정 2026-07-30 — **I-21 `listId`를 UUID급 무작위(≥128bit)로 확정**, 순번·타임스탬프 등 추측 가능한 형식 금지) (v0.16.0 개정 2026-07-30 — **`sessionId`(접속)·`threadId`(방) 축 분리**: 동시 스트림 락을 방 단위로, I-20 사유 `logout` 1종, CH-1 멱등(D5), 맥락 TTL 접속 단위(D6)) (v0.15.27 개정 2026-07-30 — 사본 drift 정정: 담기 이벤트 적재 주체(BE→FE)·`budget` 이벤트 제외·`search.query` PII 기준) (v0.15.26 개정 2026-07-28 — 사본 동기화: §3.1 `conditionActions`(칩 제거, #84)·`screen`(화면 맥락, #118) 신설, `conditions` 칩 `field` 6종 확정, in-stream `error`에 `requestId`·`retryable` 추가) (v0.15.25 개정 2026-07-28 — #171: I-1 응답에 reviewCount 추가(AI 계산용·비표시), rating=0 의미 판별(리뷰 부재 vs 저평점). #100 "reviewCount 표시전용·미반환" 부분 개정. / v0.15.24 개정 2026-07-27 — 사본 동기화: S-5 폐기 반영, 상품 수정은 챗봇 HITL(I-11) 유일 경로) |
| 상태 | draft |
| 대상 독자 | Spring 백엔드 팀, React 프론트엔드(FE) 팀 |
| 소유 | AI 에이전트 서버 팀 |

> 본 문서는 **인터페이스 계약(interface contract)** 이다. 사용자 대면 엔드포인트의 동작·불변식은 소유 SPEC(`SPEC-RECOMMEND-001`, `SPEC-PROFILE-001`)에서 확정되며, 본 문서는 그 계약을 외부(Spring/FE) 관점에서 정리한다. 이벤트 채널(`/events/*`)의 HTTP 계약은 **본 문서가 단일 소스(single source of truth)** 로 소유한다(product.md 결정 21).
>
> **[v0.5.0 개정 — 2026-07-15 사용자 최종 확정]** 본 개정은 v0.4.0 Batch 2(카탈로그 미러 + 배치 동기화)를 **되돌려**, **후보 검색 = 질의 시점 Spring 위임(`POST /products/search`)** 을 **프로젝트 전 범위의 유일·영구 후보 확보 경로**로 확정한다. **[v0.5.1 정정 — 용어 확정]** 채택하지 않는 것은 **상품 원본 컬럼의 AI측 사본**(가격·재고·상품명 등 필터 컬럼 복제)이다. **AI 생성물 — extras(추론 태그)·search_doc·임베딩 벡터 — 은 AI Postgres에 저장·유지**하며(결정 3 Layer 2/3·결정 6 존속), 상품 변경 반영은 **AI가 요청하는 pull 배치**(§4.8)로 갱신한다. 이는 v0.4.0의 provenance 노트가 폐기했던 검색 위임 노선을 **최종 채택**하는 것이며, 이미 boot-verified 구현 스캐폴드(`~/projet/hk-final`, jarvis-ai, FastAPI+LangGraph)가 이 노선 위에 존재하고 사용자가 이를 구현 기준으로 비준했다.
> - **핵심 변경**: 후보 확보가 "AI 자기 검색 인덱스(미러)"에서 "질의 시점 Spring `POST /products/search` 위임"(신규 §4.6)으로 **영구 전환**된다. 상품 원본 컬럼의 사본(미러)은 두지 않는다. **[v0.5.1 정정]** AI 생성물(extras·search_doc·임베딩)은 유지하며 bulk export pull 배치(§4.8, C-4 부활)로 갱신한다. 질의 시점 후보 흐름에서 AI 임베딩과 Spring 검색의 결합 방식은 **OPEN**(§4.8 말미 — 두 방식 병행 검토).
> - **[이벤트 최종 — #187 개정]** `POST /events/session-end`(세션 종료)와 `POST /events/session-claim`(로그인 승격)을 **MVP에 유지**한다. **주문 알림(구 `POST /events/order`)·주문 미러는 채택하지 않는다** — 검색이 질의 시점 위임으로 확정되면서 구매 이력도 **추천 직전 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 확보한다(결정 14-F 동작 요구는 불변, 데이터 획득 방식만 교체). **병행 PRD 초안 라인은 모든 이벤트를 고도화로 옮겼으나, 본 계약은 session-end 유지 한 지점에서 PRD와 갈라진다** — PRD의 events-scope를 **바로잡아야 하며**(§8 항목 6), 본 문서는 PRD를 조용히 따르지 않는다.
> - **Batch 1(판매자 확장)은 v0.4.0 그대로 유지**: `POST /seller/chat` = 통계 Q&A(원천 = Spring 집계 I-6 질의 시점 콜백, C-7 해소) + 상세 수정 draft 흐름(I-7 읽기 → LLM 개정안 → SSE `draft` → FE diff 카드 → FE가 Spring `S-3` PATCH로 반영, FE↔Spring 전제).
> - **[v0.6.0 개정 — 2026-07-15 사용자 확정, BE "챗봇 장바구니 담기(I-2)" 문서 채택]** 장바구니 계약을 BE 팀 I-2 문서 기준으로 재작성한다(§4.1) — **게스트 담기 허용**(02 D30, 결정 8 개정 필요 §8 항목 7), **`POST /internal/cart/items` + `X-Internal-Token` 서비스 토큰 + 본문 신원(userId/guestId, AI-검증 JWT `sub` 유래)**, **`optionId` 필수 옵션 되물음 멀티턴**(400 `CART_OPTION_REQUIRED` + options 목록 → LLM 재질문), 동일 상품·옵션 기존 존재 시 **Spring이 quantity 합산**. **장바구니 조회(§4.9, C-16 신설)** 추가 — "장바구니에 뭐 있어?" 질의 응답 + 담기 시 기존 보유 안내.
> - **[v0.7.0 개정 — 2026-07-15 사용자 확정, 스트림 운영 규약]** SSE 스트림 수명주기 규약 신설(§2.9) — **동시 스트림 제한(세션당 1개, `409 STREAM_IN_PROGRESS`)**, **취소 = 클라이언트 연결 종료**(FE `AbortController` → AI가 disconnect 감지 시 LLM 스트림 즉시 중단), **타임아웃 기준표**(first-token 10s / 스트림 상한 90s / AI→Spring 3s / LLM 30s+1재시도), **레이트 리밋 값·소유 확정**(FastAPI 미들웨어 + in-memory, 분당·시간당 상한 config). 대화 저장(COMPLETED/FAILED/CANCELLED)·로그/모니터링 필드는 운영 요구로 부록 §6.3에 등재.
>
> **[v0.3.0 명명 기준 — 유지]** FE/BE 팀의 챗 API 문서("추천 챗봇 CH-2")를 **명명 기준(naming baseline)** 으로 채택한다. 구매자 SSE 이벤트명은 `token`/`conditions`/`action`/`products.ready`/`done`/`error`를 쓰고(구 `text.delta`/`products` 폐기), 모든 페이로드 필드는 **camelCase**로 표기한다. 이 변경으로 `SPEC-RECOMMEND-001` §5.3과의 정렬이 깨지므로 해당 SPEC의 **동기화 개정(sync amendment)** 이 후속으로 필요하다(§7). 본 문서는 SPEC을 편집하지 않고 후속 항목으로만 등록한다.
>
> **[provenance — 노선 확정]** v0.4.0은 검색 위임 노선을 "미비준 병렬 초안"으로 폐기하고 미러+배치를 채택했으나, v0.5.0은 사용자 최종 확정으로 **검색 위임 노선을 유일·영구 비준 노선으로 채택**한다(구현 기준 = `~/projet/hk-final` 스캐폴드). v0.4.0 Batch 2(미러+배치·카탈로그 벡터 검색)는 **채택하지 않기로 확정**되어 문서에서 제거된다. 병행 PRD 라인 대비 유일하게 다른 점은 session-end가 MVP에 남는다는 것이다(주문 알림은 §4.7 질의 시점 조회로 대체). 상세는 §6.2 변경 이력 참고.
>
> **표기 규약**
> - 🔴 **협의 필요**: Spring/FE 팀과 계약 확정이 필요한 미해결 항목. 이 표시가 붙은 스키마는 본 문서에서 **제안(초안)** 으로만 제시한다.
> - **제안(초안)**: 어느 계약에서도 아직 확정하지 않은 형태(상관관계 키·목록 push 스키마·bulk export API·I-6/I-7 계약 등)를 본 문서가 초안으로 제안하는 것. 최종 확정 전까지 변경될 수 있다.
> - **확정안 반영**: 소유 SPEC 또는 팀 세션에서 확정된 결정을 본 문서에 반영한 것. Spring/FE 수용 전까지 🔴가 병기될 수 있다.

---

## 1. 개요 (Overview)

### 1.1 목적

자비스 AI 에이전트 서버가 제공/요구하는 HTTP API 표면을 Spring 백엔드 팀·React FE 팀과 공유하기 위한 계약 문서다. AI 서버는 자연어 상품 추천·프로필·판매자 통계/상세 수정 보조 응답을 담당하고, 커머스 트랜잭션(장바구니 저장·결제·회원)과 **상품 표시 UI(우측 상품 패널)·상품 원본 데이터**는 Spring이 소유한다.

### 1.2 호출 방향 원칙 (Call Direction)

FE가 사용자 대면 API에 대해 **AI 서버를 직접 호출**하고(결정 19), AI 서버는 후보 검색·구매 이력·주문 상태·장바구니·추천 목록·판매자 집계/이력·판매자 상품 CRUD를 위해 Spring을 역호출하며, AI 생성물 갱신은 Spring 변경분을 pull한다. Spring → AI 레인은 **`/events/session-end`와 로그인 승격용 `/events/session-claim`**(§3.5)에 더해 **홈 추천 랭킹 위임 I-22 `POST /internal/recommendations/home`**(§3.7)을 갖는다 — 주문 알림은 채택하지 않는다(§3.6·§4.7). 상품 원본 컬럼의 AI측 사본은 두지 않으며 후보 확보는 **질의 시점 I-1 `GET /internal/products/search`**(§4.6), AI 생성물 갱신은 I-17 pull 배치(§4.8)로 분리한다.

| 레인 | 방향 | 호출 | 인증 | 근거 |
|---|---|---|---|---|
| (a) 사용자 대면 | **FE → AI (직접)** | `POST /chat`, `POST /seller/chat`, `GET /profile/me` | 사용자 JWT (§2.3 a) | 결정 19 |
| (b) Spring → AI | **Spring → AI** | 이벤트 `POST /events/session-end`·`POST /events/session-claim`(§3.5) + **위임 호출 I-22 `POST /internal/recommendations/home`**(§3.7) | 서비스 간 토큰 (§2.3 b) | 결정 12/16/21, #187, I-22(2026-07-28) |
| (c) 역방향 | **AI → Spring** | **17건** `{I-1,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}` — 후보 검색(§4.6), 구매 이력(§4.7), 주문 상태 요약(§4.10), 장바구니 담기/조회(§4.1/§4.9), 추천 목록 push(§4.2), 판매자 집계·이력(§4.4), 판매자 상품 CRUD(§4.5), AI 생성물 변경분 pull(§4.8) | **전부 서비스 토큰(internal, `X-Internal-Token`)**. 사용자/판매자 스코프 신원은 AI가 검증 JWT 클레임에서만 도출 | 결정 7 / 경로 B / BE DB 정합 |
| (d) 전제 계약 | **FE → Spring** | 세션+스트림 티켓 발급(CH-1)·티켓 재발급(CH-1b)·판매자 세션(CH-6), 채팅 추천 목록 GET(CH-5, §4.3), **홈 추천 목록 GET(P-5, §4.11)**, (판매자 FE 직접 상품편집 — AI 표면 밖) | Spring 소관 | 결정 19 / 경로 B / v0.15.20 / P-5(2026-07-28) |

- 레인 (a): 사용자(회원·게스트·판매자)의 요청. 신원은 **토큰 클레임**에서 추출한다(§2.3, §2.6). AI는 사용자 요청 본문의 식별자를 신뢰하지 않는다.
- 레인 (b): Spring → AI는 **(1) 이벤트 2건** — 세션 종료 통지(`/events/session-end`)와 로그인 소유권 승격(`/events/session-claim`) — **과 (2) 위임 호출 1건 I-22**(홈 추천 랭킹, §3.7)다. 주문 알림은 채택하지 않는다 — 구매 이력은 질의 시점 조회(§4.7)로 확보하며, 카탈로그 변경 이벤트도 존재하지 않는다(사본 없음). **[v0.18.0] 이 레인은 더 이상 "이벤트 채널"만이 아니다** — I-22는 통지가 아니라 응답 본문에 결과가 실려 오는 **동기 요청/응답**이고, 따라서 §2.7의 `/events/*` 멱등 규약이 적용되지 않는다(재시도 = 새 추천 실행).
- 레인 (c): AI → Spring 역방향은 **정확히 17건**이다 — `{I-1,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`. 이름과 순서는 **후보 검색**, **구매 이력 조회**, **주문 상태 요약(I-4, §4.10)**, **장바구니 담기**, **장바구니 조회**, **추천 목록 push**, **매출 시계열**, **구매전환 퍼널**, **행동 이벤트 집계**, **주문 상태 전이/조회**, **상품 변경 이력**, **이탈 코호트**, **자사 상품 목록 조회**, **상품 등록**, **상품 수정**, **상품 삭제**, **AI 생성물 변경분 pull**이다. I-1/I-19/I-4/I-2/I-18/I-21과 판매자 API는 요청 시점 호출이고, I-17은 배치 pull이다.
- 레인 (d): FE ↔ Spring 전제 계약(Spring 소유). **[v0.18.0] P-5 `GET /api/products/recommended`(§4.11) 등재** — 홈 "OO님을 위한 추천". Spring이 이를 서빙하려고 내부에서 I-22(§3.7)를 호출하므로 **P-5 ↔ I-22가 서브 관계**다. 게스트는 P-5 대신 P-4(인기 상품)를 직접 호출한다. **[v0.15.20] BE 구현 실측으로 경로·응답 확정.** (1) **세션+스트림 티켓 발급(CH-1, `POST /api/chat/sessions`)** — 응답 `{sessionId, ttlSeconds, streamTicket, ticketTtlSeconds, llmSseUrl}`. 세션 TTL 10분 sliding, 티켓 TTL 60s(RS256). `llmSseUrl`은 FE가 AI 서버에 직결할 SSE 주소로, Spring이 내려준다. (2) **스트림 티켓 재발급(CH-1b, `POST /api/chat/tickets`)** — 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO. 세션 유지한 채 새 티켓만 발급(2번째 메시지·`401` 시)하며 세션 TTL도 함께 갱신한다. **CH-1 재호출은 새 세션(맥락 단절)이라 티켓 재발급에 쓸 수 없다.** (3) **판매자 세션 발급(CH-6, `POST /api/chat/seller/sessions`)** — 판매자 챗 입구. `brandId`는 **BE가 JWT 검증 후 DB에서 도출해** 티켓 클레임에 박는다(클라이언트·LLM 주장 무시). (4) 추천 목록 GET(§4.3). (5) 판매자가 FE에서 직접 상품을 편집하는 경로(AI 표면 밖). ※ 구 "draft 적용 = FE가 S-3 PATCH"는 **폐기** — 채팅 경로 쓰기는 AI 직접(§3.2), `S-3`은 자사 상품 목록 조회(=I-9)다.

> **[HARD] 후보 확보 = 질의 시점 Spring 검색(v0.5.0, 유일·영구)**: 구매자 추천 후보는 **질의 시점에 Spring `POST /products/search`(§4.6)를 위임 호출**하여 확보한다. 상품 원본 컬럼의 AI측 사본은 두지 않는다. **[v0.5.1]** AI 생성물(extras·search_doc·임베딩)은 AI Postgres에 저장하며(§4.8), 질의 시점에 AI 임베딩과 Spring 검색을 어떻게 결합할지는 OPEN(§4.8 말미)이다. rerank(profile_summary 반영)는 여전히 AI 경계에서 수행한다.
>
> **[HARD] 표시 경로 = 경로 B(불변)**: 상품 목록은 SSE에 싣지 않는다. AI가 최종 랭크 목록을 Spring에 push(§4.2)하면 Spring이 표시 필드를 enrich하여 저장하고(§4.3), FE가 이를 GET한다. **표시 권위 = Spring**(결정 9-B, AI는 표시 필드 미보유). §4.6 검색 응답의 price는 rerank·예산 검증(AI-side)용이며 우측 패널 표시가는 여전히 경로 B로 채운다. 단방향 원칙의 AI→Spring 역방향 예외 증가는 product.md 신규 결정 레코드가 명문화한다(§8 항목 3).

### 1.3 MVP 범위 요약

MVP(개발 가동 목표 2026-07-19)에 포함되는 API 표면:

- **아키텍처**: 사용자 대면 API(`/chat`·`/seller/chat`·`GET /profile/me`)는 **FE → AI 직접 호출**(사용자 JWT), **후보 확보는 질의 시점 Spring 검색 위임(`POST /products/search`, §4.6)**, **상품 목록 표시는 경로 B**(AI → Spring push → FE ← Spring GET)로 분리된다(§1.2). 상품 원본 컬럼 사본은 없음, AI 생성물(extras·search_doc·임베딩)은 pull 배치로 유지(§4.8, v0.5.1 정정).
- **추천 agent** — `POST /chat`(SSE 스트리밍, 상품 추천 서브그래프 포함). 소유: `SPEC-RECOMMEND-001`. 후보는 §4.6 Spring 검색으로 확보, rerank는 AI-side. SSE 스트림은 상품 카드를 싣지 않고 `products.ready` 상관관계 키만 emit한다.
- **후보 검색 위임** — `POST /products/search`(§4.6, AI → Spring 질의 시점). decompose 산출 구조화 필터로 Spring 카탈로그를 검색하고, rerank·예산 검증에 필요한 후보 필드(price 포함)를 돌려받는다. **가장 중요한 신규 Spring 계약**.
- **장바구니 서브그래프** — `POST /chat` 내부 흐름. 실제 담기는 AI → Spring 장바구니 API 호출(I-2, §4.1, 단건 — 묶음은 반복 호출). **게스트도 담기 가능**(v0.6.0). 옵션 필수 상품은 `CART_OPTION_REQUIRED` 응답의 options 목록으로 **되물음 멀티턴**을 수행하고, 담기 전/질의 시 장바구니 **조회**(§4.9)로 기존 보유·수량 합산을 안내한다. 결과는 SSE `action` 이벤트로 반영.
- **프로필 조회** — `GET /profile/me`(마이페이지, 토큰 소유자 본인). 소유: `SPEC-PROFILE-001`.
- **판매자 agent** — `POST /seller/chat`. (a) **매출/판매 통계 Q&A**(원천 = Spring 집계 I-6 콜백, C-7 해소) **+ (b) 상세 수정 draft 흐름**(I-7 읽기 → `draft` 이벤트 → FE 반영). 리뷰 인사이트는 **비범위(MVP 제외)**.
- **이벤트 채널** — `POST /events/session-end`(세션 종료)와 `POST /events/session-claim`(guest→member 승격)을 유지. 주문 알림은 채택하지 않음 — 구매 이력은 **질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 대체(사용자 명시 결정 — 병행 PRD 라인과는 session-end 유지 지점에서 갈라짐, §8 항목 6).

> **판매자 agent 범위(Batch 1)**: 판매자 agent는 원래 고도화(~7/31) 범위였으나 2026-07-14 세션에서 최소 범위(통계 Q&A)로 MVP에 편입되었고(product.md 결정 20), 2026-07-15 세션에서 **상세 수정 draft 흐름까지 MVP로 확대**되었다(§8 결정 20 개정 항목). 리뷰 인사이트(측면별 감성)는 계속 고도화.
>
> **[v0.5.0] 시맨틱 검색 caveat(정직 명시)**: Case 3(상황 기반) 추천 품질은 **Spring 검색(`POST /products/search`) + LLM decompose(쇼핑리스트 분해)** 로 달성한다. **AI 측 시맨틱(임베딩) 인덱스는 도입하지 않는다** — 따라서 상황 태그·의미 유사도 기반 검색은 Spring 카탈로그의 키워드/필터 검색 능력 한도 안에서만 동작한다. 이 caveat로 `SPEC-RECOMMEND-001`의 검색 도구(search-tool) 절이 개정 대상이 된다(§7).

---

## 2. 공통 규약 (Common Conventions)

### 2.1 Base URL

```
{AI_SERVER_BASE_URL}
```

- 배포 환경별 실제 값은 인프라 설정으로 주입한다(플레이스홀더). 예: `https://ai.jarvis.internal`.
- 모든 §3 경로는 이 base URL에 상대적이다.

### 2.2 명명 규약 (Naming Convention)

**[HARD] 본 문서의 모든 JSON 필드명·SSE 이벤트명은 FE/BE 팀 챗 API 문서("추천 챗봇 CH-2")를 명명 기준으로 채택한다.**

- **구매자 SSE 이벤트명**: `token` / `conditions` / `action` / `products.ready` / `done` / `error`(§3.1). 구 v0.2.0/`SPEC-RECOMMEND-001` §5.3의 `text.delta`·`products`는 **폐기**한다.
- **판매자 SSE 이벤트명**: `token` / `draft` / `done` / `error`(§3.2). `products.ready`·`conditions`·`suggestions`·`budget`·`action`은 판매자 스트림에서 **emit하지 않는다**.
- **필드명**: 모든 페이로드는 **camelCase**(`sessionId`·`threadId`·`productId`·`finishReason`·`relaxationNotice`·`verifiedSum`·`withinBudget`·`droppedItems`·`feasibilityNotice`·`cartItemId`…). 구 snake_case는 전 계약에서 폐기한다.
- **일관성**: `/events/*`(§3.5~3.6)와 `GET /profile/me`(§3.4)도 **camelCase로 통일**할 것을 제안한다(제안(초안)). `SPEC-PROFILE-001` §5.4의 `ProfileViewResponse` 필드 역시 camelCase 정렬 대상이다(§7 후속 개정).

> **SPEC 정렬 깨짐 명시 🔴**: 이 명명 채택으로 `SPEC-RECOMMEND-001` §5.3(snake_case + `text.delta`/`products` 이벤트)과 정렬이 깨진다. 의미론은 보존하되 이름만 바뀌므로, **SPEC §5.3의 동기화 개정**이 후속으로 필요하다(§7).

### 2.3 인증 (Authentication) — 확정안 반영(RS256/JWKS·401 규약), Spring 수용 전 🔴

인증은 **호출자 유형에 따라 2종**으로 나뉜다.

#### (a) 사용자 대면 API — 사용자 JWT (레인 a)

`POST /chat`, `POST /seller/chat`, `GET /profile/me`에 적용한다.

```
Authorization: Bearer {STREAM_TICKET}   ← Spring이 스트림 단위로 발급한 단명 JWT (로그인 AT가 아님)
```

- **[개정 v0.10.0] SSE에 쓰는 토큰 = 스트림 단명 티켓** — 로그인 AT(전권 토큰)를 SSE에 직접 싣지 않는다. Spring이 **채팅 진입 시 신원을 확인하고 스트림 단위로 단명 JWT(RS256, TTL 30~60초)를 발급**하며, FE는 이 티켓으로 AI 서버에 SSE 연결한다. ("JWKS 검토 후 제안" 최종안 채택.)
  - **발급 흐름**: `FE → Spring`(회원=AT / 게스트=`guest_id` 쿠키) → `Spring`(신원 확인 후 스트림 티켓 발급, RS256) → `FE → AI`(티켓으로 SSE) → `AI`(JWKS 검증 후 스트리밍). **첫 티켓**은 **CH-1**(세션 발급, `POST /api/chat/sessions`) 응답에 얹어 추가 왕복이 없다(응답에 `sessionId` + `streamTicket`).
  - **[확정 v0.15.20] 티켓 재발급 경로 = CH-1b `POST /api/chat/tickets`** — 스트림 티켓 TTL(60초)이 세션 TTL(10분 sliding)보다 **훨씬 짧아**, CH-1 1회로는 첫 스트림만 커버된다. 2번째 메시지부터는 **세션을 유지한 채 티켓만 재발급**한다. BE 구현 실측: 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO.
  - **[개정 v0.16.0 — D5] CH-1은 멱등이다. 구 "CH-1 재호출 = 새 세션(맥락 단절)" 경고를 폐기한다.** Spring이 세션 생성 시 Redis `SETNX`로 **"이 사용자의 세션이 이미 있으면 그것을 그대로 반환"** 하므로, CH-1을 몇 번 부르든 한 사용자에게는 세션이 하나다. 이 멱등이 **정확성의 책임자**다 — FE의 Web Locks(D1)는 한 브라우저 안에서만 통하므로 폰과 PC 동시 접속은 막지 못하고, 축출을 없앤 뒤에는 기록에서 밀린 세션이 CH-1b로 TTL을 계속 연장하며 **유령 세션으로 남아 I-20도 나가지 않는다**. Web Locks는 쓸데없는 CH-1 중복 호출을 줄이는 **최적화**이며 유지 여부는 FE 판단이다.
    - 다만 **CH-1b가 여전히 정규 경로**다 — 티켓만 갱신하는 편이 싸고, 세션 소유자 검증이 붙는다. CH-1 재호출은 이제 "틀린 것"이 아니라 "불필요한 것"이다.
    - **예외 — 게스트 첫 방문 멀티탭**: 쿠키가 아직 없는 상태에서 탭을 여러 개 동시에 열면 Spring이 게스트를 **두 명** 만든다. 쿠키는 하나만 남으므로 밀린 탭은 자기 세션의 주인과 쿠키가 달라져 **CH-1b에서 `403`** 을 맞는다(CH-1을 다시 부르면 복구). **신원 자체가 갈라지는 것이라 `SETNX`로 막을 수 없다** — Web Locks가 실질적으로 방어하는 유일한 케이스다.
    - 🔴 **BE 확인 필요**: `SETNX` 멱등 키의 스코프(회원 `sub` 단위인지 `sub_type`+`sub` 단위인지)와, 멱등 반환 시 세션 TTL을 sliding 갱신하는지 여부. 정본 SPEC-CHAT-SESSION §5 D5는 스코프를 "이 사용자"로만 적었다. **호출자 신원이 세션 소유자와 다르면 거부**하며(BE가 세션에 보관한 `sub_type`+`sub` 대조), 재발급과 함께 세션 TTL도 sliding 갱신한다. 판매자 세션은 보관된 `brandId`를 복원해 SELLER 스코프 티켓으로 재발급한다. Spring 소유 전제 계약(§1.2 레인 d).
  - **채택 이유**: (1) **게스트 커버** — 게스트는 로그인 AT가 없으므로 Spring이 `guest_id` 쿠키를 확인해 동일 경로로 티켓 발급(`sub_type: guest`). (2) **전권 AT 비노출** — SSE 쿼리스트링/헤더에는 30~60초짜리 읽기 전용 티켓만 나가 유출 시 피해가 "스트림 1회 연결"로 한정. (3) **aud 규율** — 로그인 AT는 Spring 전용, FastAPI용 `aud`는 티켓에만. (4) **발급 = 인증 관문** — 모든 SSE 연결이 스트림마다 Spring 신원 검증을 1회 통과.
- **[확정] 서명·검증 = RS256 + JWKS** — Spring이 **JWKS 엔드포인트**(`GET /.well-known/jwks.json`)를 노출하고, AI 서버가 JWKS 공개키를 **fetch·캐시하여 로컬 검증**한다(RS256, `kid`로 키 선택). **`kid` miss 시에만 refetch**하며, 요청마다 Spring에 왕복하지 않는다(FastAPI 기동 시 Spring이 잠깐 죽어 있어도 캐시로 동작).
- **[확정] 스트림 티켓 필수 클레임**:
  - `sub` — 사용자/판매자/게스트 식별자(숫자 id를 문자열로, §2.5·§2.6).
  - `sub_type` — `member` | `guest`. 구매자 티켓의 **유일한 신원 유형 정본**이며,
    JWKS 모드에서 누락·그 외 값·legacy `role=GUEST|USER`·미지 role 대체는 모두
    `401 TOKEN_INVALID`로 fail-closed 한다. dev 모드만 로컬 호환을 유지한다.
  - `iss` — 발급자 **`"jarvis-spring-auth"` [확정 v0.15.20]** (BE `StreamTicketProvider.ISSUER` 실측).
  - `aud` — 대상 **`"jarvis-fastapi-ai"` [확정 v0.15.20]** (BE `StreamTicketProvider.AUDIENCE` 실측). **AI는 `aud`를 검증**한다(토큰 혼용 방지 — 로그인 AT는 이 aud가 없어 SSE에 못 씀).
  - `scope` — **`"chat:stream"` [확정 v0.15.20]** (BE `StreamTicketProvider.SCOPE_CHAT_STREAM` 실측). AI는 이 **단일 문자열 exact 값**을 항상 검증한다. 누락·빈 값·다른 값·공백 구분 복합 문자열·배열·비문자 값은 모두 `401 TOKEN_INVALID`이며 설정 누락으로 검증을 끌 수 없다.
  - `exp` — 발급 후 **60초 [확정 v0.15.20]** (BE `app.stream-ticket.ttl-seconds: 60`. 구 "30~60초" 범위의 상단값). 완전 1회용은 아니며 짧은 TTL로 근사 — Redis는 Spring 전용 결정 유지, stateless 검증. CH-1/CH-1b 응답이 `ticketTtlSeconds`로 실값을 함께 반환한다.
  - **구매자(`/chat`)**: 위 공통 클레임에 서명된 **`sessionId`**를 추가한다. AI는 이 값을 요청 body의 `sessionId`와 대조하고, 누락·불일치하면 `403 SESSION_FORBIDDEN`으로 거부한다. **`threadId`는 body-only**다 — 한 접속 티켓으로 여러 탭/방을 동시에 열 수 있어야 하므로 티켓에 바인딩하지 않는다.
  - **판매자(`/seller/chat`)**: **`role == "seller"`(소문자) + `brandId`(JSON 정수) — [확정 v0.15.20]** (BE `StreamTicketProvider.buildTicket` 실측). seller `sub`는 양의 BIGINT 숫자 문자열, `brandId`는 bool을 제외한 JSON 정수 `1..2^63-1`만 허용한다. null/string/float/bool/list/object/범위 밖 값은 seller route나 Spring backend에 닿기 전에 `401 TOKEN_INVALID`다. 집계·CRUD 역호출(§4.4·§4.5)의 `{brandId}` path에 이 값을 쓴다. AI는 `brandId`를 **요청 본문에서 받지 않고 검증된 티켓 클레임에서만** 얻는다(userId와 동일 원칙 — IDOR 방지, 판매자가 남의 brandId로 조회 불가, §2.6). **판매자 티켓에는 구매자용 `sessionId` claim을 요구하지 않는다.** `role` 클레임은 판매자 티켓에만 실리고 구매자·게스트 티켓에는 `role`이 없고 `sub_type`만 있다. 두 discriminator가 함께 있거나 역할별 필수 discriminator가 없으면 `401 TOKEN_INVALID`로 fail-closed 한다. 올바른 판매자 티켓도 구매자 `/chat`에서는 buyer state를 만들기 전에 `403 FORBIDDEN`으로 거부한다. AI는 신원을 **오직 토큰 클레임에서만** 추출한다(요청 본문 금지, §2.5·§3.1·§3.2).
  - 검증 항목: **signature / exp / iss / aud / scope**.
- **[확정] 401 통일 규약**: 토큰이 **없음/무효/만료**이면 AI 서버는 항상 **`401`** 을 반환한다.
  - `code == "TOKEN_EXPIRED"` — `exp` 경과.
  - `code == "TOKEN_INVALID"` — 서명 불일치·형식 오류·누락.
  - **FE 반응**: `401` 수신 → Spring **티켓 재발급 경로(CH-1b, §1.2 레인 d)** 에서 새 스트림 티켓 발급 → 새 티켓으로 원 요청을 **1회 재시도**(§2.5·§6.1). (티켓 TTL이 짧지만, 스트림 시작 전 만료 시의 재발급 흐름이며 — 이미 열린 스트림은 티켓 만료로 끊지 않는다, §2.5.)
- **[확정] `403` 규약**: `/seller/chat`는 `role == "seller"`를 요구한다. 판매자 스코프가 없는 토큰의 호출은 **`403 FORBIDDEN`**.
- **[폐기] `CHAT_SESSION_EXPIRED`(FE/BE 문서의 `400`) 폐기**: `sessionId`에는 만료 의미가 **없다**(§2.6). 인증 실패는 모두 `401`(TOKEN_*)로 통일한다.

#### (b) Spring → AI — 서비스 간 토큰 (레인 b)

`POST /events/session-end`와 `POST /events/session-claim`(Spring → AI, §3.5), **그리고 I-22 `POST /internal/recommendations/home`(§3.7)** 에 적용한다. (v0.5.0에서 주문 알림·카탈로그 배치는 채택하지 않으므로 해당 인증 항목은 없다.) I-22의 토큰 불일치 오류 코드는 `INTERNAL_TOKEN_INVALID`(401)다.

```
X-Internal-Token: {SERVICE_TOKEN}
```

- **[v0.15.17 확정]** Spring PR #24와 AI 수신 구현이 사용하는 서비스 토큰 헤더는 `X-Internal-Token`이다. 발급·회전 주체, 만료 정책, mTLS 병용 여부는 🔴 협의(§5 C-1).
- 사용자 JWT와 **별개의 자격 증명**이다 — 이벤트 채널은 사용자 신원이 아니라 서비스 신원을 검증한다.
- **[개정 v0.13.0] AI → Spring 역호출은 전 구간 동일 레인** — BE DB 실측(`internal` 그룹 전부 `서비스 토큰`)에 맞춰 **`X-Internal-Token` 서비스 토큰 + 본문/쿼리에 신원**(AI가 검증한 JWT `sub`에서 도출)으로 통일한다. 구 "사용자/판매자 JWT 포워딩" 제안(후보 검색·판매자·구매 이력)은 **폐기** — 장바구니 I-2 패턴이 표준. **IDOR 안전**: 본문 신원은 사용자 입력이 아니라 AI가 검증 토큰에서 도출한 값이다(§2.6).

### 2.4 Content-Type

| 방향 | Content-Type |
|---|---|
| 요청 본문(JSON) | `application/json; charset=utf-8` |
| 일반 JSON 응답 | `application/json; charset=utf-8` |
| SSE 스트리밍 응답(`/chat`, `/seller/chat`) | `text/event-stream; charset=utf-8` |

- SSE 응답 시 **FastAPI 앞단 리버스 프록시**는 **응답 버퍼링을 비활성화**해야 토큰 단위 스트리밍이 유지된다. FE가 AI 서버를 직접 호출하므로 chat 스트림에 대한 **Spring 중계 버퍼링 이슈는 해당하지 않는다**(§1.2).

### 2.5 스트림 전(前) 오류 봉투 (Pre-stream Error Envelope) — 확정안 반영, Spring 수용 전 🔴

비스트리밍 응답 및 **SSE 스트림이 시작되기 전** 거부(인증·요청 검증 등)의 오류 봉투다. (스트림 **내부** 오류는 §3.1/§3.2의 `error` 이벤트로 별도 전달되며 아래 봉투와 다르다.)

```json
{
  "error": {
    "code": "string",
    "message": "string",
    "requestId": "string"
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `error.code` | string | 기계 판독용 오류 코드(아래 상태 매핑) |
| `error.message` | string | 사람이 읽는 안전한 메시지(내부 스택/PII 미포함) |
| `error.requestId` | string | 추적용 요청 식별자(로그 상관관계) |

**[확정] 스트림 전 상태 코드 매핑**:

| HTTP | `code` | 의미 |
|---|---|---|
| `400` | `BAD_REQUEST` | 요청 본문/파라미터 오류 |
| `401` | `TOKEN_EXPIRED` / `TOKEN_INVALID` | 인증 실패(§2.3 a) |
| `403` | `SESSION_FORBIDDEN` | 구매자 티켓의 서명된 `sessionId` 누락/불일치, 또는 claim 뒤 옛 owner 접근 |
| `409` | `SESSION_ACTIVE` | owner claim 대상 guest session에 활성 스트림이 있음 |
| `409` | `SESSION_FINALIZING` | D6/I-20 정리 중이라 touch/claim을 받을 수 없음 |
| `409` | `SESSION_CLAIM_CONFLICT` | 이미 다른 소유권 이력이 있거나 terminal/owner가 충돌 |
| `503` | `STATE_UNAVAILABLE` | lifecycle 정본 저장소를 사용할 수 없어 fail-closed |
| `403` | `FORBIDDEN` | 권한 없음(예: 판매자 스코프 없이 `/seller/chat`) |
| `409` | `STREAM_IN_PROGRESS` | **[v0.7.0 · 개정 v0.16.0]** 동일 **`threadId`** 에 활성 스트림 존재(§2.9 a) — FE는 진행 중 스트림 종료 후 재시도. 같은 `sessionId`의 **다른 방은 막지 않는다** |
| `429` | `RATE_LIMITED` | 레이트 리밋 초과(§2.8) |
| `504` | `UPSTREAM_TIMEOUT` | **[v0.7.0]** 스트림 시작 전 상류(LLM/Spring) 타임아웃(§2.9 기준표) |

- `404`/`503`/`500` 등 그 외 상태는 필요 시 동일 봉투로 확장한다(제안). 정확한 코드 목록은 Spring 협의로 조정될 수 있다. 🔴
- 이 표가 **스트림 전 오류 코드의 통합 목록**이다(v0.7.0 — 4번 항목). 스트림 **내부** 오류는 §3.1 in-stream `error` 4종(+타임아웃, §2.9)으로 별도.

#### 공통 헤더 규약 — `X-Request-Id` · `traceparent` **[v0.15.27]**

정본은 Notion「🗂️ 프로젝트 자료실」의 **「공통 헤더 규약 — X-Request-Id · traceparent (전 API 공통)」** 페이지다. API 명세서 DB는 엔드포인트 행 단위라 전 API 공통 규약을 놓을 자리가 없어 DB 바깥에 두었다. 아래는 **AI 서버 소관 요약**이다.

| 항목 | 규칙 | AI 현재 |
|---|---|---|
| `X-Request-Id` 생성 | 최초 진입 서비스가 만든다 | — |
| 형식 | `[A-Za-z0-9_-]{16,64}` | — |
| **inbound 수용** | 형식에 맞으면 **유지**, 아니면 버리고 새로 만든다(400 아님 — 추적 편의가 요청을 막으면 안 된다) | 🔴 **미구현** — `app/core/errors.py` `request_context_middleware`가 `new_request_id()`를 **조건 없이** 호출한다 |
| **outbound 전파** | Spring 역호출에 그대로 실어 보낸다 | 🔴 **미구현** — `app/services/spring_client.py`가 `X-Internal-Token` **하나만** 붙인다 |
| 응답 echo | 응답 헤더로 되돌린다 | ✅ 구현됨(미들웨어 + 오류 핸들러 전부) |
| in-stream `error` | `requestId` 포함 | ✅ 계약 반영(§3.1·§3.2) · 구현 대기 |
| `traceparent`/`tracestate` | W3C Trace Context, tracing 켜졌을 때 전파. **MVP 범위 밖** | 🔴 코드베이스에 없음 |

> **왜 형식 검증을 하나** — 들어온 값을 무조건 믿으면 **로그 주입**이 된다(개행·제어문자로 로그 한 줄을 쪼개거나 수백 KB로 부풀리기). 화이트리스트 형식에 맞을 때만 유지한다.
>
> **`X-Request-Id`는 인증·인가에 쓰지 않는다** — 클라이언트가 정하는 값이라 신뢰 근거가 될 수 없다. 신원은 종전대로 JWT `sub`(§2.3)·`X-Internal-Token`이다.
>
> **`traceparent`와 역할이 겹치지 않는다** — `X-Request-Id`는 사람이 로그를 grep 하는 키, `traceparent`는 APM 도구가 스팬 트리를 그리는 키다. 하나로 합치지 않는다.
>
> **지금 상태의 대가**: FE → Spring → FastAPI 로그를 같은 키로 이을 수 없어, 오류 하나를 추적하려면 세 시스템 로그를 시간으로 눈대중해야 한다(#141·#134·#151).

#### 401 토큰 만료 재발급 흐름

- 사용자 JWT가 없음/무효/만료이면 AI 서버는 `401`(TOKEN_EXPIRED 또는 TOKEN_INVALID)을 반환한다(레인 a).
- FE 재시도 흐름: `401` 수신 → FE가 **Spring에 토큰 재발급 요청** → 새 토큰으로 원 요청 **1회 재시도**. AI 서버는 재발급에 관여하지 않는다(§6.1).
- **SSE 인증은 연결 시작 시점에 검증**한다(제안). 스트림 진행 중 스트림 티켓(TTL 30~60초)이 만료되어도 **활성 스트림을 끊지 않는다**(스트림 자체가 LLM 응답 1회 분량) — 만료는 다음 연결에서 `401`로 나타나며, FE가 그 시점에 새 티켓 발급·재연결한다(§2.3). 확정 전 제안(초안)이다. 🔴

### 2.6 식별자 규약 (Identifiers) — 확정, 양팀 통보 필요

**[HARD, 개정 v0.15.3] `productId` = 숫자(BIGINT), DB 스키마 기준** — **[사용자 확정 2026-07-18]** 상품/옵션/장바구니/주문 원본 id는 전부 **`BIGINT` 숫자**다(product/product_option/cart_item/order 테이블). AI 계약(§4 internal + SSE `draft`)은 **숫자 id를 그대로** 쓴다. 구 "경계별 문자열 정규화·전 구간 문자열" 규칙은 **폐기** — 그냥 스키마 타입을 따른다. **게스트 id(`guestId`)만 UUID 문자열**(guest.id CHAR(36)). 구매자 SSE는 상품 카드/productId를 싣지 않으므로(경로 B, `products.ready`=`listIds`만) 경계 변환 이슈 자체가 없다.

> **[✅ 정렬 완료 v0.15.3]** 코드(`schemas/spring.py`·`chat.py`)의 상품/옵션/장바구니/주문 id를 `int`(BIGINT)로, `guest_id`를 `str`(UUID)로 반영. CLAUDE.md "전 구간 string" 규칙도 개정. BE I-17 예시가 문자열 productId를 보였으나 **BE가 2026-07-18 숫자 BIGINT로 정정**(§4.8) — 표기 불일치 해소.

**사용자/게스트/판매자 식별자 = 숫자 id(numeric)** — Spring이 발급하며(게스트도 Spring이 숫자 id 부여), JWT `sub` 클레임에 **문자열화하여** 담는다. `role`(§2.3 a)로 회원/게스트/판매자를 구분한다.

**`sellerId` = JWT `sub`(role=seller)에서 도출 · `brandId` = JWT `brandId` 클레임에서 도출** — AI는 판매자 역호출(§4.4·§4.5)에 필요한 `sellerId`·`brandId`를 **모두 검증된 판매자 JWT 클레임에서만** 얻는다. seller `sub`는 양의 BIGINT 숫자 문자열, `brandId`는 bool 제외 JSON 정수 `1..2^63-1`로 decode 경계에서 검증한다. **`brandId`를 요청 본문·사용자 발화에서 받지 않는다**(IDOR 방지 — 판매자가 남의 `brandId`로 조회 불가). RS256 서명이라 클레임 위조 불가. **[개정 v0.8.0]** 구 "AI는 brandId를 알지 못한다(Spring 내부 해소)"에서 "JWT 클레임에서만 획득"으로 완화 — BE 집계 API가 `{brandId}` path를 요구함에 따름.

#### `sessionId`(접속) vs `threadId`(방) **[개정 v0.16.0 — SPEC-CHAT-SESSION Option B]**

한 사용자의 한 **접속**(`sessionId`) 아래 여러 **방**(`threadId`)이 **동시에** 존재한다(멀티탭 동시 대화). MVP의 `sessionId == threadId` 전제는 폐기한다. 두 축은 담당하는 상태가 다르며, **어느 축으로 키잉하는지가 곧 계약**이다.

| 축 | 발급 | 수명 | 담당 상태 |
|---|---|---|---|
| `sessionId` | **Spring** CH-1(`POST /api/chat/sessions`) | BE Redis TTL 10분 sliding | 프로필 세션버퍼 · 세션 종료 통지(I-20, §3.5) · `conversation_turns.conversation_id`(primary) |
| `threadId` | **FE**(탭·방별, 서버 왕복 없음) | 소속 세션과 함께 만료(아래) | 멀티턴 필터 누적 · 장바구니 pending · 되돌리기 · **동시 스트림 락**(§2.9 a) · `conversation_turns.thread_id` |

- **AI는 `sessionId`의 만료를 판정하지 않는다** — 세션 TTL은 Spring Redis 소유이고 AI가 검증하는 것은 스트림 티켓(§2.3 a)뿐이다. 따라서 AI가 `CHAT_SESSION_EXPIRED`를 반환하는 경우는 없다(§2.5). 만료 의미가 **없어서**가 아니라 **판정 주체가 Spring이라서**다. 다만 프로필 파이프라인은 자체 DB에 기록한 마지막 회원 발화 시각을 기준으로 **프로필 버퍼의 10분 비활동 종료**를 독립적으로 판정한다(§3.5).
- **새 대화는 CH-1을 부르지 않는다** — FE가 `threadId`만 새로 생성하고 세션은 유지된다. 따라서 "새 대화"는 세션 종료 사유가 아니다(§3.5).
- **맥락 TTL은 방이 아니라 접속 단위** — 어느 방에서든 활동이 있으면 그 `sessionId`에 속한 **모든 방**의 맥락 TTL을 함께 연장하고, 세션이 끝나면 그 아래 방을 **한꺼번에** 정리한다. 방마다 생사가 갈리면 탭을 옮겼을 때 한쪽 맥락만 사라져 사용자가 이해할 수 없다.
- **구매자 스트림 티켓은 `sessionId`를 담고 `threadId`는 담지 않는다.** AI는 서명된 `sessionId`를 body와 대조해 다른 접속의 세션 상태 접근을 막는다. `threadId`는 body-only라 **티켓 1장이 한 접속의 여러 방 스트림을 동시에 커버**한다. 세션 수명·만료의 정본은 계속 Spring Redis에 있다.
- **AI 내부 문맥 정본은 전역 고유 `context_id`다(#187).** 최초 정상 touch에서 한 번 생성하며 guest→member claim, D6 만료 후 같은 owner의 재활성화, 여러 `threadId`의 후속 발화에서도 유지한다. 구조화 상태는 `context_id:threadId`로 키잉하고 claim 때 복사하지 않는다. 상세 상태 기계·rollout은 `docs/specs/SPEC-CHAT-SESSION-CONTEXT-187.md`.
- 최대 길이는 둘 다 config `chat_key_max_chars`(§3.1) — 초과 시 `400`.

> **`sessionId`는 "불투명 스레드 키"가 아니다.** v0.15.x까지 이 문서는 `sessionId`를 *"만료 의미 없는 불투명 스레드 키"* 로 정의했다. 축이 갈린 뒤 "스레드 키"는 `threadId`의 것이므로 그 표현을 전면 폐기한다. `sessionId`는 여전히 AI에게 **불투명**하지만(형식 검증 없음, UUID 수용), **접속 식별자**다.

> 사용자/판매자 식별자 타입(숫자)·클레임 키는 Spring 회원 스키마 소유다 — 세부는 🔴 협의(§5 C-10).

### 2.7 이벤트 엔드포인트 멱등성 규약 (Idempotency for Event Endpoints)

`/events/*` 엔드포인트(§3.5~3.6)는 통지 채널이므로 **멱등(idempotent)** 이어야 한다.

- **[v0.15.19, 이슈 #79]** session-end(§3.5)은 별도 `eventId` 필드가 없다 — Spring I-20 멱등은 **`(userId, sessionId)` 고정 파생키**(`session-end:{userId}:{sessionId}`)로 판정한다. AI 내부 비활동 처리는 같은 키의 `PROCESSING` claim으로 I-20과 경합을 직렬화하되, 성공 뒤 claim을 영구 완료하지 않고 해제하는 **재개 가능한 checkpoint**다. 같은 `sessionId`의 새 회원 발화가 실제 저장되면 이전 `PROCESSING`/`COMPLETED` 키를 activity 갱신과 같은 transaction에서 무효화하므로 새 버퍼를 다음 timeout/I-20이 처리할 수 있다.
- 통지는 **best-effort** 이며, 유실되어도 AI 서버의 정합성은 통지에 의존하지 않는다(세션 종료: AI 내부 비활동 sweep이 저장 버퍼를 회수). 상세는 각 엔드포인트 항목 참고.
- **[v0.15.17 확정]** 정상 신규·중복 통지는 처리 완료 여부와 무관하게 `202 Accepted`로 수신 확인한다(§3.5).

### 2.8 CORS 및 레이트 리밋 (CORS & Rate Limiting) — 🔴 협의 필요

FE가 AI 서버(FastAPI)를 **다른 오리진에서 직접 호출**하므로 브라우저 CORS·남용 방어가 AI 서버 앞단으로 이동한다.

- **CORS**: AI 서버는 FE 오리진에 대해 CORS 헤더를 서빙해야 한다. 허용 오리진 목록은 🔴 협의(§5 C-11). `Authorization` 헤더를 사용하므로 브라우저 **preflight(OPTIONS)** 가 발생한다 — AI 서버는 preflight에 `Access-Control-Allow-Headers: Authorization` 등으로 응답해야 한다.
- **레이트 리밋(레인 a)**: 게스트도 토큰(익명 JWT)을 지참하므로 **토큰 스코프 기반 레이트 리밋**이 가능하다(§2.5). 초과 시 `429 RATE_LIMITED`(§2.5).
- **[v0.7.0 확정] 목적·소유·값**: 목적은 정밀 과금 통제가 아니라 **무분별한 남용 차단**(2026-07-15 사용자). **MVP 소유 = FastAPI 미들웨어 + in-memory 카운터**(단일 인스턴스 전제 — 다중 인스턴스 확장 시 Redis 이관, §2.9 동시 스트림 레지스트리와 동일 단서). 상한은 **config 기본값 제안**: 채팅 메시지(POST /chat·/seller/chat) **분당 10회 / 시간당 100회**(토큰 `sub` 스코프, 게스트 동일). 값 자체는 운영 조정 대상이며 계약 사항은 "429 + 토큰 스코프"뿐이다.
- **잔여 🔴(C-11)**: 허용 오리진 목록만 남음.

### 2.9 SSE 스트림 수명주기 — 동시 스트림·취소·타임아웃 [v0.7.0 신설]

`POST /chat`·`POST /seller/chat` 공통 규약이다.

#### (a) 동시 스트림 제한 — **방(`threadId`)당 1개** **[개정 v0.16.0]**

- 동일 `threadId`에 활성 스트림이 있는 상태에서 새 요청이 오면 **`409 STREAM_IN_PROGRESS`**(§2.5 봉투)로 거절한다(기존 스트림은 유지 — last-wins 아님, 2026-07-15 확정).
- **같은 `sessionId`의 다른 방은 서로 막지 않는다** — 락 키가 `threadId`라 탭 A가 스트리밍 중이어도 탭 B는 정상 스트리밍된다. 멀티탭 동시 대화가 이 축 분리의 목적이므로, **세션 단위로 잠그면 그 목적이 정면으로 무효화된다**(§2.6).
- **FE 1차 방어**: 스트리밍 중 **해당 방의** 입력창 비활성화. 409는 서버 측 백스톱(같은 방 재전송·중복 제출 대비).
- 구현: 인프로세스 활성 스트림 레지스트리(**MVP 단일 인스턴스 전제** — 결정 8의 무상태 원칙과의 긴장은 "요청 간 사용자 상태 없음" 의미로 한정 해석하고, 다중 인스턴스 확장 시 Redis로 이관).

#### (b) 요청 취소 — 취소 신호 = 연결 종료 (별도 취소 엔드포인트 없음)

- FE: `AbortController.abort()` → fetch 연결 종료. 이것이 유일한 취소 인터페이스다.
- AI 서버: SSE 제너레이터가 이벤트 전송 사이마다 disconnect를 감지(`request.is_disconnected()` 폴링)하고, 감지 즉시 **진행 중인 LLM 스트림을 close**(토큰 비용 차단)하며 LangGraph 실행 task를 취소한다.
- 취소된 턴의 대화 저장은 `CANCELLED` 상태 + **부분 생성 텍스트 보존**(§6.3) — 다음 턴 컨텍스트·프로필 스캔에 포함된다.

#### (c) 타임아웃 기준표 — 제한값 확정(config 기본값)

| 구간 | 기준값 | 초과 시 동작 |
|---|---|---|
| FE→AI **first-token**(스트림 첫 이벤트까지) | **10s** | 스트림 시작 전이면 `504 UPSTREAM_TIMEOUT`(§2.5), 시작 후면 in-stream `error` 후 종료 |
| FE→AI **스트림 전체 상한** | **90s** | `done`(finishReason `stop`) 강제 종료 + 저장 상태 `FAILED` 아님(정상 절단) |
| AI→Spring 콜백(§4.1/§4.4~4.7/§4.9) | **3s**(BE I-2 문서 기준으로 통일) | 각 계약의 degrade 규칙(조회 생략·담기 `CART_ERROR`·dedup 생략 등) |
| AI→LLM 단일 호출 | **30s + 1회 재시도** | 재시도 실패 시 in-stream `error`(`LLM_UNAVAILABLE` 계열) |

- 값은 config 기본값이며 운영 조정 가능. **계약 사항은 초과 시 동작**(어떤 오류가 어느 채널로 오는가)이다.

---

## 3. AI 서버 제공 API

> **호출자 구분**: §3.1~3.4(사용자 대면)은 **FE → AI 직접 호출**(사용자 JWT, 레인 a). §3.5~3.6(`/events/*`)과 **§3.7(I-22 홈 추천 랭킹)** 은 **Spring → AI 서버 간 호출**(서비스 토큰, 레인 b). §1.2 참고.

### 3.1 `POST /ai/chat` — 구매자 챗봇 (SSE 스트리밍, FE 직접)

구매자의 자연어 질의를 받아 상품 추천/장바구니/상품 질문/**주문상태 문의** 등을 SSE로 스트리밍 응답한다. **[v0.16.3, #164 구현] 주문상태 Q&A(I-4)를 `order_status` intent로 CH-2에 흡수**했으며 세부 계약은 §4.10을 따른다 — 별도 CS 챗봇 없음. 관리자 CS 문의(CH-3·I-5·AD-1/2·M-9)는 **post-MVP**. 소유: `SPEC-RECOMMEND-001`(추천 서브그래프), 상위 구매자 그래프 SPEC(라우팅).

> **[경로 정합 v0.15.0]** FE-대면 경로는 **`{AI_SERVER}/chat`**(BE DB 07/17 실측 — 구 `/ai/chat` 표기 정정, `{AI_SERVER}` 접두어로 AI 서버 직접 호출임을 명시, 인증=스트림 티켓 필요). 본 문서 다른 위치의 `POST /chat`·`POST /ai/chat` 표기는 이 경로로 읽는다. (판매자는 `{AI_SERVER}/seller/chat`.)

#### 요청 (Request)

```json
{
  "sessionId": "string",
  "threadId": "string",
  "message": "string",
  "conditionActions": [{ "op": "remove", "field": "category" }],
  "screen": {
    "pageType": "chat",
    "columns": 3,
    "products": [{ "productId": 101, "name": "무선 이어폰" }]
  }
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | **Spring 발급 접속 식별자**(§2.6). **프로필 세션버퍼·세션 종료 통지(§3.5)의 키** — 여러 방의 발화가 이 하나의 버퍼로 모인다. **[v0.15.7] 최대 길이 = config `chat_key_max_chars`(기본 200자)** — 초과 시 `400`(불투명 키 남용 방어) |
| `threadId` | string | 예 | **FE 생성 방 식별자**(§2.6). **멀티턴 필터 누적·장바구니 pending·되돌리기·동시 스트림 락(§2.9 a)의 키.** **[v0.15.7] 길이 상한 동일**(`chat_key_max_chars`) |
| `message` | string | 예 | 현재 턴 사용자 원문 질의. **[v0.15.6] 최대 길이 = config `chat_message_max_chars`(기본 4000자)** — 초과 시 `400 BAD_REQUEST`(§2.5). PII·메모리 방어(`/seller/chat` 동일). **[v0.15.26] `conditionActions`가 1건 이상이면 빈 문자열을 허용**한다 — 둘 다 비면 `400`. |
| `conditionActions` | array | 아니오 | **[v0.15.26 신설]** FE 조건 칩 제거 액션. 미지정 기본값은 빈 배열. **구매자 전용** — 아래 상세. |
| `screen` | object | 아니오 | **[v0.15.26 신설]** 사용자가 지금 보고 있는 화면. 지시어("이거") 해석·담기 대상 확정에 쓴다 — 아래 상세. |

> **[보안] `userId`는 요청 본문에 없다** — 사용자 식별자·역할은 `Authorization` 헤더의 JWT 클레임(`sub`/`role`)에서만 추출한다(사칭 방지, §2.3 a·§2.5). `conditionActions`·`screen` 도 신원을 싣지 않는다.

##### `conditionActions` — FE 조건 칩 제거 (선택) **[v0.15.26]**

`conditions` 칩(아래 (2))의 X 버튼을 눌렀을 때 FE가 보내는 **구조화 신호**다. 어떤 칩을 지웠는지는 UI만 아는 사실이라 서버가 발화만으로 복원할 수 없다.

- `op` — **`remove`만 허용**한다. add·replace 범용화는 필요해질 때 확장.
- `field` — `conditions` 칩의 계약 필드 **6종만** 허용(아래 (2) 참조). 그 밖의 값은 `400`.
- **멱등하다** — 이미 없는 필드를 지워도 성공 no-op, 같은 `field` 중복은 dedup. 재전송에 안전해야 한다.
- **구매자 전용 계약** — 판매자 요청(§3.2)에는 없다. AI는 `BuyerChatRequest`를 따로 두어 공통 `ChatRequest`를 오염시키지 않는다.

> **[폐기 v0.15.26]** 구 규약 — *"칩 제거는 왕복(round-trip), 다음 턴 `message`에 규약 문자열(예: `"[조건 제거] priceMax"`)로 실어 재분해를 트리거"* — 는 **폐기**한다(이슈 #84). FE는 이 방식으로 구현돼 있으나 AI에 수신부가 없어 **현재 칩 제거가 무동작**이다. 전환 시 FE는 제어 메시지를 사용자 말풍선으로 남기지 않는다.

##### `screen` — 현재 보고 있는 화면 (선택) **[v0.15.26]**

채팅은 좌(대화)/우(패널) 분할이고, 사용자는 우측 패널을 보며 **"이거"** 라고 말한다. 그 지시어는 발화만으로 확정할 수 없다 — 무엇이 어떻게 보이고 있었는지는 FE만 아는 사실이다(이슈 #118, 07-17 FE 제안).

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `pageType` | string | 예 | **우측 패널에 뜬 내용**의 종류. 라우트가 아니다(아래 참조). E-1 `page_view.pageType`과 **같은 enum을 공유**한다 |
| `filters` | object | 아니오 | 패널에 걸린 필터. **값은 사람이 읽는 표시값** — LLM 프롬프트에 그대로 들어간다. 키는 `status`·`sort`·`page` |
| `products` | array | 아니오 | **서버가 모르는 목록**만 `[{productId, name}]`(아래 참조). 최대 `screen_products_max`(config, 기본 20)건 — 초과분은 FE가 화면 순서대로 자른다 |
| `columns` | number | `products` 있으면 | **전송 시점 그리드 열 수.** 반응형이라 창 크기에 따라 바뀌며 서버가 알 수 없다. 목록형은 `1` |

**`filters` 키 (3종)** — 값은 **enum 코드가 아니라 화면에 보이는 한글 표시값**을 싣는다. LLM 프롬프트에 그대로 들어가므로 코드값(`ORDERED`)은 의미 전달이 안 된다.

| 키 | 쓰는 `pageType` | 값 예시(표시값) |
|---|---|---|
| `status` | `seller_orders` | `전체` · `신규주문` · `배송중` · `배송완료` · `취소·반품` |
| `status` | `seller_products` | `전체` · `판매중` · `품절` · `숨김` |
| `sort` | `seller_products` | `최신순` · `판매순` · `재고순` · `가격순` |
| `page` | 목록 전부 | `"1"` (**사람이 보는 1-base**. API의 0-base가 아니다) |

> **[v0.15.26] 목록 필터 키는 `status` 하나로 통일한다.** 주문 목록(주문 상태)과 상품 목록(상품 상태)은 값 집합이 다르지만 **어느 쪽인지는 `pageType`이 이미 말해주므로** 키를 나눌 이유가 없다. FE는 현재 URL 쿼리에서 주문은 `?status=`, 상품은 `?tab=`으로 다르게 쓰는데, **`screen.filters`에는 둘 다 `status`로 싣는다** — `tab`은 UI 용어라 LLM에 의미가 없다.
>
> `category`는 브랜드 페이지 필터인데 **그 화면엔 채팅이 없어** `screen`으로 올 경로가 없다. 사이드 채팅이 붙으면 그때 추가한다.

> **[v0.15.26] `pageType`은 라우트가 아니라 패널 내용을 가리킨다** — 채팅은 전용 페이지(`/chat`·`/seller/chat`)에만 있고 사이드 채팅 위젯이 없다. 라우트를 실으면 항상 `chat`·`seller_chat`이라 정보가 0이다. 판매자가 `/seller/chat`에서 우측 워크스페이스의 "주문 관리" 탭을 보고 있으면 `seller_orders`를 싣는다.
>
> **현재 UI로 실제 오는 값은 3종**이다 — `chat`(구매자 인기상품 패널) · `seller_orders` · `seller_products`. 나머지 11종은 **E-1 `page_view` 전용**이며, `screen`에 실리는 건 사이드 채팅이 그 화면에 붙은 뒤다. **FE는 3종만 매핑하면 되고, AI는 나머지 분기를 만들지 않는다.**

> **[v0.15.26] `products`는 "서버가 모르는 목록"에만 싣는다** — 판단 기준은 *화면에 보이나*가 아니라 *서버가 이미 아나*다.
>
> | 패널 내용 | 만든 주체 | `products` |
> |---|---|---|
> | 구매자 — 대화 시작 전 **P-4 인기상품** | Spring (FE 직접 조회) | ✅ **싣는다** |
> | 구매자 — 추천 카드(CH-5) | AI (I-21 → `listId`) | ❌ 서버가 `listId`로 안다 |
> | 판매자 — 자사 상품 목록 | Spring (I-9) | ✅ **싣는다** |
> | 판매자 — 주문 목록 | Spring | ❌ 채팅이 주문에 하는 쓰기가 없다 |
>
> 추천 카드를 되돌려주면 **위조 경로**가 된다(E-1도 같은 이유로 FE가 보낸 추천 문맥을 신뢰하지 않는다). 페이로드도 작아진다.

> **[v0.15.26] 지시어 해소** — `columns`가 있으면 좌표 지시가 풀린다: `index = (row-1) × columns + (col-1)`. **배열 순서가 화면 순서**(좌→우, 위→아래)임이 전제다. `rows`와 항목별 `row`/`col`은 **싣지 않는다** — `products.length`와 `columns`에서 계산된다.
>
> | 발화 | 필요한 것 |
> |---|---|
> | "이거 담아줘" | 후보가 1건일 때만 확정, 여러 건이면 **되물음** |
> | "3번째 거 담아줘" | 배열 순서 |
> | "3번째 줄 2번째 담아줘" | **`columns`** |
> | "무선 이어폰 담아줘" | `name` 매칭 |

> **[v0.15.26] 유효성 — `screen`은 관대하게, `conditionActions`는 엄격하게** 처리한다. `screen`은 **맥락 힌트**라 잘못돼도 사용자 플로우를 막지 않는다(E-1의 *"개별 이벤트가 이상해도 배치를 실패시키지 않는다"* 와 같은 철학). `conditionActions`는 **사용자 의도의 직접 표현**이라 조용히 무시하면 "왜 안 지워지지"가 된다.
>
> | 상황 | 처리 |
> |---|---|
> | `pageType` 누락·미지의 값 | `screen` **전체를 무시**하고 200 진행 |
> | `products` 상한 초과 | 앞 `screen_products_max`건만 취하고 버림 |
> | `products[].name` 누락 | 그 항목의 이름만 버림(`productId`는 allowed 에 남긴다) |
> | `products` 있고 `columns` 누락 | 좌표 지시만 불가, 나머지 진행 |
> | `conditionActions`의 알 수 없는 `op`/`field` | **400** |
> | `message` 키 생략·공백만 | `""` 와 동일 취급(trim 후 판정) — `conditionActions`가 있으면 정상, 없으면 **400** |

**`pageType` 어휘 (14종 — 구매자 10 · 판매자 4)** — **정본은 E-1**(Notion「📡 API 명세서」E-1 · 「이벤트 수집 확정 명세 v2」§1-2). `page_view` 이벤트와 `screen.pageType`이 **같은 값을 쓴다**.

| 구분 | 값 | 라우트 예 |
|---|---|---|
| 구매자 | `home` | `/` |
| | `category` | `/brands/:brandId` (브랜드·카테고리 목록) |
| | `search` | 검색 결과 |
| | `product_detail` | `/products/:productId` |
| | `cart` | `/cart` |
| | `checkout` | `/checkout` |
| | `order_complete` | `/checkout/complete` |
| | `my` | `/mypage/*`·`/wishlist` |
| | `chat` | `/chat`·`/inquiry` |
| | `auth` | `/login`·`/signup` |
| 판매자 | `seller_dashboard` | `/seller` |
| | `seller_orders` | `/seller/orders` |
| | `seller_products` | `/seller/products` |
| | `seller_chat` | `/seller/chat` |

> **[v0.15.26] 어휘 확장** — 기존 8종은 구매자 화면만 커버해 판매자·채팅·인증 화면이 갈 곳이 없었다(`page_view`가 전 라우트에서 발화하므로 미분류 값이 생긴다). `chat`·`auth` + 판매자 4종을 추가해 **8종 → 14종**(구매자 10 · 판매자 4)으로 넓힌다. E-1 정본과 함께 개정한다. **`screen.pageType`에 실리는 건 이 14종의 부분집합**이다(현재 3종).

> **[v0.15.26] `path`·`label`을 두지 않는 이유** — 화면을 가리키는 어휘를 새로 만들지 않는다. `pageType`이 E-1에 이미 있으므로 그대로 쓰고, 라우트 경로(`path`)는 **쿼리스트링에 검색어 등 PII가 실릴 수 있어** 계약에서 뺀다(필요한 조건은 `filters`로 정제해 나른다). 한글 화면명(`label`)은 **AI가 `pageType`→표시명 매핑을 config로 갖는다** — 프롬프트 문구는 튜닝 대상이지 계약이 아니고, FE가 관리하면 화면마다 표현이 흔들린다.

> **[보안] `screen.products`는 담기 허용 목록을 넓히는 입력이지 프리패스가 아니다.** 담기 가드는 **(누적 추천 목록 ∪ `screen.products`의 productId)** 를 allowed로 취급하며, **두 목록 밖의 id는 여전히 차단**한다. "이거"가 모호하면 되물음한다 — LLM이 발화 속 임의 숫자를 오추출해 담는 것을 막는 기존 가드는 유지된다. 실제 담기는 I-2(§4.1)가 재고·판매상태를 다시 검증한다.

> **판매자 스트림(§3.2)도 동일하다** — `screen`은 **구매자·판매자 공용 요청 필드**다(`conditionActions`가 구매자 전용인 것과 다르다). 스키마는 공용 `ChatRequest`에 두고 `BuyerChatRequest`/`SellerChatRequest`가 상속한다. 판매자 대시보드는 이미 `meta.lane`·`done.panel`로 **AI→FE 방향의 화면 조작**을 계약에 두고 있는데 그 반대 방향(FE→AI, 지금 무엇을 보고 있나)이 비어 있었다 — 서버가 `panel="refresh"`로 우측 재조회를 지시하면서 그 패널에 무엇이 떠 있는지는 모르는 상태였다.

#### 응답 (Response) — `text/event-stream`

SSE로 스트리밍한다. 표준 `EventSource`는 GET 전용이므로 FE는 **fetch 스트리밍(ReadableStream)** 으로 소비한다(§6.1). 이벤트명은 `token`/`conditions`/`action`/`products.ready`/`done`/`error`를 쓴다. **상품 카드는 SSE로 오지 않는다**(경로 B, §3.3·§4.2·§4.3).

**(1) `token`** — 근거/코멘트 토큰 증분 (0회 이상).

```json
{ "type": "token", "data": { "text": "이 케이스는 방수라서" } }
```

**(2) `conditions`** — 추출된 필터 조건을 FE 제거 가능한 칩으로 전달 (0~1회)

```json
{
  "type": "conditions",
  "data": {
    "chips": [
      { "field": "priceMax", "label": "5만원 이하", "value": 50000 },
      { "field": "category", "label": "여행용품", "value": "여행용품/보안용품" }
    ]
  }
}
```

- FE는 각 칩을 제거 가능한 형태로 노출한다. **[v0.15.26] 칩 제거는 위 `conditionActions`로 보낸다** — 구 규약 문자열 왕복 방식은 폐기됐다(이슈 #84).
- **[v0.15.26] `field` 허용값 6종** — `category`(카테고리 · 이어폰) / `priceMax`(50,000원 이하) / `priceMin`(30,000원 이상) / `brand`(삼성 · LG) / `ratingMin`(평점 4.5+) / `keyword`(방수). **`conditions`가 내보내는 집합과 `conditionActions`가 지우는 집합은 동일하다.** 종전에는 예시 둘만 있어 허용 집합이 계약에 없었다.
- 칩은 LLM 출력이 아니라 **확정된 병합 필터에서 결정론적으로 파생**된다(`build_condition_chips`) — 같은 필터면 항상 같은 칩이 나온다.

**(3) `action`** — 장바구니 담기 결과 (0회 이상). §4.1(I-2)과 연동.

```json
{
  "type": "action",
  "data": { "type": "CART_ADDED", "message": "여행용 방수 파우치를 장바구니에 담았어요.", "cartItemId": "55" }
}
```

실패 예:

```json
{
  "type": "action",
  "data": { "type": "CART_ADD_FAILED", "message": "해당 상품을 찾지 못했어요.", "reason": "PRODUCT_NOT_FOUND" }
}
```

재고 부족 예(남은 재고 수 노출):

```json
{
  "type": "action",
  "data": { "type": "CART_ADD_FAILED", "message": "재고가 3개뿐이에요.", "reason": "STOCK_INSUFFICIENT" }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `type` | `"CART_ADDED"` \| `"CART_ADD_FAILED"` | 담기 성공/실패 |
| `message` | string | 사용자 노출 안전 문구 |
| `cartItemId` | string \| 없음 | 성공 시 담긴 항목 식별자(I-2 응답 `data.cartItemId`) |
| `reason` | string \| 없음 | 실패 시 사유 코드(§4.1) |

- **`reason` 허용값(v0.15.16 재편)**: `PRODUCT_NOT_FOUND` / `STOCK_INSUFFICIENT` / `CART_ERROR` **3종**. `STOCK_INSUFFICIENT` = 합산 수량 > 재고(BE `400 CART_STOCK_INSUFFICIENT` + `error.detail.availableStock`, 2026-07-22 신설, 재고는 상품 단위) → AI가 message에 남은 재고 수를 실어 안내("재고가 N개뿐이에요"; **재고 0=품절이면 "품절된 상품이에요"**, §4.1). ~~`OUT_OF_STOCK`~~은 **폐기 유지** — 품절(stock 0)도 `STOCK_INSUFFICIENT`(availableStock:0)로 통합. 수량 상한(합산 > 99)은 BE `VALIDATION_ERROR`로 별개 — AI는 `CART_ERROR` + BE 동일 문구 "수량은 최대 99개까지 담을 수 있습니다."로 안내. ~~`GUEST_NOT_ALLOWED`~~는 **폐기** — 게스트도 담기 허용(v0.6.0, 결정 8 개정 필요 §8 항목 7).
- **옵션 되물음은 `action` 실패가 아니다** — I-2가 `400 CART_OPTION_REQUIRED`(options 목록 포함)를 반환하면 AI는 실패 `action`을 emit하지 않고 **`token` 텍스트로 옵션을 되묻는 멀티턴**으로 이어간다(§4.1). 사용자가 옵션을 답하면 `optionId`를 해석해 재담기한다.
- **장바구니 조회 응답("장바구니에 뭐 있어?")도 별도 이벤트 없이 `token` 텍스트**로 답한다(§4.9).

**(4) `products.ready`** — AI가 추천 목록을 Spring에 push한 뒤 emit (정확히 1회, 성공 시).

```json
{ "type": "products.ready", "data": { "sessionId": "550e8400-e29b-41d4-a716-446655440000", "listIds": ["3f9a2c1e7b8d4e5fa0c6d1e97b3f8a24"] } }
```

여러 묶음(세트형·니즈별) — I-21이 `lists`를 2개 보냈으면:

```json
{ "type": "products.ready", "data": { "sessionId": "550e8400-…", "listIds": ["9f2c1a7e4b8d43f5a0c6e1d97b3f8a24", "4b8d43f5a0c6e1d97b3f8a249f2c1a7e"] } }
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `sessionId` | string | 상관관계 키(요청과 동일) |
| `listIds` | string[] | FastAPI 생성 목록 식별자 배열(§4.2 I-21의 `lists` 순서) — reason 포함 카드는 CH-5로 조회(§4.3) |

- **[v0.15.26] `listIds`는 항상 배열이다.** 목록이 1개여도 길이 1 배열로 보낸다 — 단일/복수로 형식이 갈리면 FE에 분기가 생긴다. **이벤트 자체는 여전히 정확히 1회**이며, 목록이 여러 개여도 한 번에 실어 보낸다. 개수 상한은 10(§4.2 `lists` 상한과 동일).
- FE는 **각 `listId`로 CH-5를 개별 호출**한다. `listType`·`label`·`totalBudget`은 싣지 않는다 — CH-5 응답에 있다.
- **[v0.15.26 정정] 구 단일 `listId` 필드는 폐기한다.** I-21이 `lists`를 1~10개 보내므로(§4.2) 단일 필드로는 세트형·니즈별 추천을 나를 수 없었다. 예시의 `"list-4471"`도 §4.2가 금지한 추측 가능 형식(순번)이라 ≥128bit 무작위로 교정했다. **구현도 단일**이라(`ProductsReadyData.list_id: str`) 코드 변경이 따라야 한다.
- FE는 `products.ready` 수신 시 §4.3 목록 GET으로 Spring이 표시 필드를 채운 목록을 조회해 우측 상품 패널을 렌더한다(§6.1).
- **push 실패 시**: `products.ready`는 emit되지 **않는다.** 챗 텍스트는 정상 완료되고 지연 안내가 포함되며, 스트림은 `error`가 아니라 **`done`** 으로 종료한다(§3.3).

**(5) `done`** — 정상 종료

```json
{ "type": "done", "data": { "finishReason": "stop" } }
```

- `finishReason`: `"stop"`(정상 완료) \| `"zero_result"`(0건). **0건은 오류가 아니라 정상 종료**이며 FE는 우측 패널을 빈 상태(empty state)로 전환한다.
- 재랭킹(rerank) 실패 또는 목록 push 실패도 `error`가 아니라 `done`으로 종료한다(degrade 정책, §3.3).

**(6) `error`** — 오류 종료 (스트림 내부)

```json
{
  "type": "error",
  "data": {
    "code": "LLM_TIMEOUT",
    "message": "일시적으로 응답이 지연됐어요.",
    "requestId": "9f2c1a7e4b8d43f5a0c6e1d97b3f8a24",
    "retryable": true
  }
}
```

- **스트림 내부 `error.code` 허용값(4종)**: `LLM_TIMEOUT` / `LLM_UNAVAILABLE` / `SEARCH_FAILED` / `INTERNAL`.
- **[v0.15.26] `requestId`** — 이 요청의 추적 id. 스트림 시작 **전** 실패는 §2.5 오류 봉투에 이미 `requestId`를 싣는데 스트림 **내부** 실패에는 없어, 정작 사용자가 "오류 났어요"라고 신고하는 쪽을 로그에서 찾을 수 없었다. 봉투와 같은 값을 쓴다.
- **[v0.15.26] `retryable`** (boolean) — FE가 재시도를 권할지 판단하는 근거. **`code`로는 복원할 수 없다** — 같은 `LLM_UNAVAILABLE`이라도 "LLM 미구성"(재시도 무의미)과 "모델 일시 불가"(재시도 유효)가 섞이므로 **emit 지점이 값을 정한다**.
- **판매자 스트림(§3.2)도 동일** — `error` 페이로드는 구매자·판매자 공용 스키마다(`ErrorData`).
- **단계별 상세는 서버 로그 전용** — decompose/rerank 등 스테이지 단위 실패 코드는 사용자 스트림에 노출하지 않는다. rerank 실패는 검색 상위로 degrade 후 `done`으로 종료한다(하드 제약 유지).

#### MVP 추가 페이로드 — SSE 측 탑재 (구매자)

FE/BE 문서에 없으나 MVP에 필요한 아래 3종은 **모두 구매자 SSE 측에 실린다**(표시 필드는 Spring, 추천 로직 산출물은 AI 경계 유지):

- **`suggestions`(제안 칩)** — 0건 완화 제안 + 구매 이력 되돌리기(결정 14-D/14-F). 전용 이벤트 `suggestions`로 emit(제안(초안)).

```json
{
  "type": "suggestions",
  "data": {
    "chips": [
      { "label": "6만원대까지 볼까요?", "relaxation": { "field": "priceMax", "value": 65000 }, "estCount": 12 },
      { "label": "소금은 최근 구매 — 다시 추천받기", "revert": { "category": "조미료" }, "estCount": 8 }
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `label` | string | 칩 문구 |
| `relaxation.field` | string | 완화 대상 필드 |
| `relaxation.value` | any | 제안 값 |
| `revert.category` | string | 구매 이력 억제 되돌리기 대상 카테고리(결정 14-F) |
| `estCount` | int | 완화/재포함 적용 시 예상 결과 수(COUNT). `estCount == 0`인 칩은 제외 |

- **`relaxationNotice`(자동 완화 투명 안내)** — 0건 자동 완화 적용 시 안내(결정 14-D). 안내 산문은 `token`으로 스트리밍하고, 기계 판독 플래그가 필요하면 `done.data.relaxationNotice: string | null`로 병기(제안(초안)).
- **총액 예산 요약(BudgetSummary)** — Case 3 총액 예산(결정 14-A). 전용 SSE 이벤트 `budget`. **⚠️ [제외 v0.15.26] 정본(Notion CH-2)은 이 이벤트를 명세에서 제외했다** — *"현재 코드에 미구현 → 필요 시 post-MVP"*. 아래 스키마는 **post-MVP 참고용**으로만 남긴다. 구현 전까지 FE는 이 이벤트를 기다리지 않으며, 이벤트 순서 계약에서도 빠진다(이슈 #163):

```json
{
  "type": "budget",
  "data": { "totalBudget": 50000, "verifiedSum": 47800, "withinBudget": true, "droppedItems": [], "feasibilityNotice": null }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `totalBudget` | int | 묶음 총액 상한 |
| `verifiedSum` | int | 코드가 **인덱스 price**로 결정론 합산한 값(LLM 산수 아님) |
| `withinBudget` | bool | `verifiedSum <= totalBudget` |
| `droppedItems` | string[] | 예산 초과 제외 아이템 label |
| `feasibilityNotice` | string \| null | 부분 충족 안내 |

> **[주의] `verifiedSum`은 검색 응답(§4.6) 가격 기준**이다(질의 시점 Spring 가격이라 신선함). 다만 경로 B에서 표시 가격은 Spring이 목록 GET 시점에 다시 채우므로(§4.3), 검색~표시 사이 가격 변경 시 SSE `budget`과 우측 패널 표시가가 순간 괴리할 수 있다(SPEC-RECOMMEND-001 OPEN-11). 예산 표시 정책은 🔴 기획·Spring 협의(§8 항목 2).

#### 이벤트 순서 계약

정상 흐름(추천): `conditions`(0~1회) → `token`(0회 이상) + `suggestions`(해당 시) → `products.ready`(성공 시 정확히 1회) → `done`(1회). **[v0.15.26] `budget`은 순서 계약에서 제외**했다(정본이 명세에서 제외 — 미구현, post-MVP). 장바구니 흐름: `token`/`action` → `done`. `products.ready`는 목록 push 성공 이후에만 나타난다.

#### 오류/degrade 동작 (참고)

- `search` 실패: `SEARCH_FAILED` `error` 이벤트, 후보 날조 없음.
- `rerank` 실패 또는 출력 검증 실패: 검색 상위 5~8개로 degrade, 하드 제약(예: `priceMax`) 유지, `error`가 아닌 `done` 종료.
- 목록 push(§4.2) 실패: 챗 텍스트 정상 완료 + 지연 안내 + `done`(no `products.ready`)(§3.3).
- LLM 타임아웃/불가용: `LLM_TIMEOUT`/`LLM_UNAVAILABLE` `error` (기준값·재시도는 §2.9 c).
- 스트림 중 소비자 abort(HTTP 연결 종료): 진행 중 LLM 호출 취소 — 취소 의미론·부분 텍스트 저장은 §2.9 b·§6.3.
- 동시 스트림·타임아웃 수명주기 전반은 **§2.9**(v0.7.0, `/seller/chat` 공통).

### 3.2 `POST /seller/chat` — 판매자 챗봇 (SSE 스트리밍, FE 직접) — [v0.4.0 확대, Batch 1]

입점 판매자의 (a) 매출/판매 통계 자연어 질문과 (b) 상품 상세 수정 요청을 처리한다. **MVP 범위 확대**: 통계 Q&A **+ 상세 수정 draft 흐름**. 소유 SPEC은 별도(판매자 그래프 SPEC, 미작성).

> **인증**: `role == "seller"` 필수. 판매자 스코프가 없는 토큰의 호출은 `403 FORBIDDEN`(§2.3 a).
>
> **응답 형식**: `/chat`과 일관성을 위해 **SSE 스트리밍**. 이벤트는 `token`/`draft`/`done`/`error`만 쓴다 — `products.ready`·`conditions`·`suggestions`·`budget`·`action`은 판매자 스트림에서 **emit하지 않는다**(§2.2). `done.finishReason`은 `"stop"` **하나뿐**이다(`zero_result` 없음).

#### 요청 (Request) — 제안(초안)

**(a) 일반/제안 요청**

```json
{
  "sessionId": "string",
  "threadId": "string",
  "message": "string"
}
```

**(b) 승인 요청(confirm) — [확정 2026-07-22, A-2]**

```json
{
  "sessionId": "string",
  "threadId": "string",
  "action": "confirm",
  "draftId": "string"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | **접속 식별자**(Spring CH-6 발급, §2.6). 프로필 세션버퍼·세션 종료 통지의 키 |
| `threadId` | string | 예 | **방 식별자**(FE 생성, §2.6). 멀티턴 맥락·draft·**동시 스트림 락**(§2.9 a)의 키 — 판매자도 워크스페이스 탭을 여러 개 열 수 있어 축 분리가 동일하게 적용된다 |
| `message` | string | 예¹ | 통계 질문("이번 주 매출 어때?") 또는 상세 수정 요청("이 상품 설명 더 매력적으로 바꿔줘") |
| `action` | `"confirm"` | 아니오 | **[확정 v0.14.1]** HITL 승인 신호. draft에 대한 `[적용]`. 지정 시 `draftId` 필수 |
| `draftId` | string | 조건부 | `action == "confirm"` 일 때 실행할 draft 식별자(스트림1 `draft.draftId`). 누락 시 `400 BAD_REQUEST` |
| `screen` | object | 아니오 | **[v0.15.26 신설]** 현재 보고 있는 화면. **구매자와 공용 필드로 정의는 §3.1을 따른다**(`pageType`·`filters`·`products`·`columns`). (a)·(b) 어느 요청에도 실을 수 있다 |

> **[v0.15.26] `screen`이 판매자에도 필요한 이유** — 판매자 대시보드는 좌(채팅)/우(패널) 분할이라 **화면이 이미 계약의 일부**인데, 그동안 방향이 한쪽뿐이었다. **AI→FE**는 `meta.lane`(레이아웃 준비)·`done.panel`(`replace`/`keep`/`refresh`)로 화면을 조작하는데 **FE→AI**(지금 무엇을 보고 있나)가 없었다 — 서버가 `panel="refresh"`로 우측 재조회를 지시하면서 그 패널에 무엇이 떠 있는지는 모르는 상태다. `pageType`·`filters`가 있으면 "이 목록 왜 비어?" 같은 지시어 질문에 답할 수 있고, 지시가 타당한지도 판단할 수 있다. **`conditionActions`는 구매자 전용**이라 판매자 요청에 싣지 않는다 — `conditions` 이벤트 자체가 판매자 스트림에 없다(§2.2).

> ¹ `message`는 일반 발화에서 필수다. 승인 요청(`action == "confirm"`)에서는 비워도 된다 — 승인은 발화가 아니라 구조화 신호이기 때문(HITL 안전장치 ②, 발화 ≠ 동의).

> **[보안] `sellerId`·`brandId`는 요청 본문에 없다** — 판매자 식별자는 JWT `sub`·`brandId` 클레임(+`role == "seller"`)에서만 추출한다(사칭 방지, §2.3 a). AI는 `brandId`를 **검증된 토큰에서** 얻어 집계 역호출(§4.4)의 `{brandId}` path에 쓴다 — 사용자 입력 brandId는 신뢰하지 않는다(§2.6).

#### 응답 (Response) — `text/event-stream`

**[확정 v0.14.1, 2026-07-22 — 화면 전환 신호]** 판매자 스트림은 이벤트 6종을 쓴다: `meta`·`progress`·`token`·`draft`·`done`·`error`. FE 대시보드가 좌(채팅)/우(패널) 분할이라, 서버가 우측 패널 조치를 명시한다.

- **`meta`** (매 스트림 첫 프레임): `{ "type":"meta", "data":{ "lane": "analysis"|"product"|"general"|"confirm"|"apply"|"refused" } }`. FE 가 레인을 즉시 알아 레이아웃 전환·로딩을 준비한다.
- **`progress`** (analysis 진행, 0회 이상): `{ "type":"progress", "data":{ "text": "…" } }`. 로딩 표시 — 최종 답변이 아니다(`token` 과 분리).
- **`done`** (종료): `{ "type":"done", "data":{ "finishReason":"stop", "panel":"replace"|"keep"|"refresh" } }`. `panel` 이 우측 패널 조치를 확정한다 — `replace`(리포트·diff 카드로 교체)·`keep`(유지)·`refresh`(쓰기 반영 → 재조회). `error` 로 끝나면 `done` 이 없고 패널은 유지한다.
- 구현: `app/api/seller.py`. 구매자 `done`(§3.1)에는 `panel` 이 없다 — 판매자 전용 필드다.

**(1) 통계 Q&A 흐름**: `meta{analysis}` → `progress`×N → `token`(리포트) → `done{panel:"replace"}`. 되묻기(기간 불명 등)는 `token` → `done{panel:"keep"}`. 통계 수치는 MVP에서 `token` 산문으로 응답한다. 원천은 **I-6 집계 콜백**(§4.4·아래 데이터 소스).

**(2) 상품 수정/등록/삭제 흐름 — [확정 v0.11.0]**: 판매자 `product_agent`가 상품 쓰기를 **AI가 Spring internal API로 직접 수행**하되, **모든 쓰기는 HITL 승인 게이트를 통과**한다. 채팅 경로 쓰기는 AI가, FE에서 직접 편집하는 경로는 FE↔Spring(AI 표면 밖)이 담당한다.

**2-스트림(interrupt/resume) 흐름** — SSE 1스트림 = 응답 1회(§2.9)라 승인 대기를 한 연결에 물지 않고 끊고-재개한다:
```
[스트림 1 · 제안]  meta{product} → draft{draftId, op, changes} → (LangGraph interrupt, 상태 checkpointer 저장) → done{panel:"replace"}
                   FE: diff 카드 + [적용]/[취소]  (product 레인은 근거 token 없음 — draft.summary 가 요약)
[스트림 2 · 승인·실행]  FE가 {action:"confirm", draftId} 전송 → meta{confirm} → 그래프 resume → AI가 I-10/I-11/I-12 호출(§4.5) → token(결과) → done{panel:"refresh"(실행)|"keep"(변경없음)}
```
- **읽기(before)**: 대상 확인은 **I-9 자사 상품 목록**(§4.5)으로 조회.
- **HITL 안전장치 [HARD]**:
  1. **승인은 `draftId`에 바인딩** — confirm은 그 draft를 참조해 **보여준 diff == 실행하는 쓰기**를 보장(다중 draft·불일치 방지). 상태 checkpointer가 실제 변경분을 보유.
  2. **명시 액션만 승인** — confirm은 **구조화 신호**(`{action:"confirm", draftId}`)여야 하며 자유 텍스트는 승인 아님(발화 ≠ 동의).
  3. **멱등성** — 동일 `draftId` 재전송은 1회만 실행(더블클릭 방지).
  4. **Spring 소유권이 하드 게이트** — HITL 우회해도 Spring이 `brandId`(JWT)로 상품 귀속 검증(§4.5). HITL은 사람-안전, Spring authz는 최종 방어.
  5. **대기 TTL** — 미승인 draft는 N분 후 만료(checkpoint TTL) — 지연 승인 방지.
- **삭제(I-12)는 필수 HITL** — soft delete(`status=HIDDEN`, 물리 삭제 없음). HITL(그래프) + soft delete(데이터) 이중 방어. (MVP는 전 쓰기 단순 `[적용]` 확인, 삭제만 문구 강조 권장.)
- **`draftId`는 선택적 권장** — 제안이 항상 하나·즉시 승인이면 checkpointer만으로도 동작하나, 다중 draft·멱등 대비로 부여를 권장.
- **confirm 전송 형식 = [확정 v0.14.1, 2026-07-22]** 요청 본문 **최상위 `action`/`draftId` 필드**(위 요청 (b)). 구 "message 문자열에 JSON 을 실어 파싱" 방식은 폐기 — FE 가 message 를 이스케이프하지 않는다. AI 코드 정합 완료(`app/schemas/seller.py::SellerChatRequest`, `app/api/seller.py`). HITL 승인은 별도 이벤트명 없이 스트림2가 `token`(결과)+`done` 으로 응답한다.
- **confirm 결과는 전부 HTTP 200 [확정 v0.14.1]** — 실행/만료/미존재/소유불일치/중복(멱등)/stale 모두 SSE `token`(안내)+`done` 으로 온다(HTTP 오류 아님). 실제 쓰기만 `done{panel:"refresh"}`, 나머지는 `done{panel:"keep"}`. 소유 불일치는 미존재와 동일 문구(존재 비노출). Spring 장애만 `token`+`error{INTERNAL}`(초안 유지, 재confirm 가능). 구 "409 `DRAFT_EXPIRED`/`DRAFT_NOT_FOUND`" 표기는 폐기.
- **스트림 시작 전 거부(HTTP 오류 봉투 §2.5)**: `400 BAD_REQUEST`(필드 누락·`action=="confirm"`인데 `draftId` 없음, `RequestValidationError`→400)·`401 TOKEN_EXPIRED`/`TOKEN_INVALID`(누락/형식·seller `sub`/`brandId` 오류 포함)·`403 FORBIDDEN`(role≠seller)·`409 STREAM_IN_PROGRESS`(동일 threadId 동시 스트림)·`429 RATE_LIMITED`(config 상한·`/seller/chat` 적용)·`504 UPSTREAM_TIMEOUT`.

**`draft`** — 상세 수정 개정안 (정확히 1회)

```json
{
  "type": "draft",
  "data": {
    "draftId": "draft-8f21",
    "op": "update",
    "productId": 10293,
    "changes": [
      { "field": "description", "before": "여행용 방수 파우치입니다.", "after": "우천·수영장에도 안심인 IPX8 방수 파우치. 여권·전자기기를 완벽 보호합니다." },
      { "field": "name", "before": "방수 파우치", "after": "여행용 IPX8 방수 파우치" }
    ],
    "summary": "상품명·설명을 방수 성능 중심으로 개선"
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `draftId` | string | 이 제안의 식별자. 승인(confirm)이 이 값을 참조해 **보여준 것 == 실행하는 것**을 보장하고 다중 draft·중복 승인을 구분한다(아래 HITL). 서버 발급 UUID |
| `op` | `"update"` \| `"create"` \| `"delete"` | 실행할 쓰기 종류(I-11/I-10/I-12 매핑) |
| `productId` | number | 대상 상품 식별자(숫자 BIGINT, §2.6). `create`는 없을 수 있음(`null`) |
| `changes` | array | 필드별 변경 제안 배열 |
| `changes[].field` | string | 수정 대상 필드 — **camelCase** 8종: `name`·`price`·`originalPrice`·`description`·`category`·`imageUrl`·`status`·`stockQuantity` (와이어 규약 §2.2, [C-1 2026-07-22]) |
| `changes[].before` | string | I-9(목록)로 읽은 현재 값. `create`는 `""` |
| `changes[].after` | string | LLM 생성 개정안 |
| `summary` | string | diff 카드 부제용 한 줄 요약 |

- **FE 렌더링**: FE는 `draft`를 **diff 카드**로 렌더하고 `[적용]`/`[취소]` 버튼을 노출한다(승인 UI).
- **[HARD, 개정 v0.9.0] 반영은 AI가 Spring internal API로 직접 수행** — 판매자 승인(HITL) 후 AI가 **I-11 PATCH(수정)/I-10 POST(등록)/I-12 DELETE(삭제)**(§4.5, `X-Internal-Token`+`{brandId}`)를 호출한다. **구 "FE가 본인 JWT로 S-3 PATCH" 모델은 폐기** — [BE 실측 정정 v0.13.0] **S-3 = `GET /api/seller/products`(SELLER, FE→Spring)** 로 **판매자 본인 FE 대시보드용** 목록이고, AI가 쓰는 목록은 **I-9 `GET /internal/seller/{brandId}/products`(서비스 토큰, AI→Spring)** 로 **별개**다(둘 다 조회, 레인만 다름). 즉 S-3는 PATCH가 아니며 I-9와 동일 엔드포인트도 아니다. 채팅 경로의 쓰기는 AI가 internal API(I-11 등)로 수행한다. (판매자가 **FE에서 직접** 상품을 편집하는 경로는 FE↔Spring 별개, AI 표면 밖.) 반영 결과는 `token`으로 안내.
- **[HARD] 대화 발화는 동의로 취급하지 않는다** — 채팅의 모호한 발화("응 바꿔")는 승인이 아니다. 반영의 유일한 경로는 **HITL 명시 승인**(아래).

#### 데이터 소스 계약 — [v0.4.0 해소, C-7 RESOLVED, Batch 1]

**[확정]** 판매자 통계 답변의 원천은 **Spring의 판매자 집계 API(I-6)를 질의 시점에 콜백**하는 것이다(§4.4). AI는 **원시 로그를 제공받지 않고 집계값만** 조회한다. 이로써 **C-7이 해소**되며, **구 결정 20 기본안(주문 미러의 `sellerId`·금액 확장)은 폐기**된다.

- **원천 = 집계 API 콜백**: AI가 JWT 클레임의 `brandId`로 `GET /internal/seller/{brandId}/sales`(매출 시계열) 등 집계 API를 호출해 집계값을 받고 LLM으로 자연어 답변한다. **[개정 v0.8.0]** 구 "sellerId만 넘기고 Spring이 내부 해소"에서 "brandId 클레임으로 `{brandId}` path 호출"로 변경(§2.6·§4.4). 판매자 집계는 단일 API가 아니라 **5종**(매출·퍼널·행동·이탈·계정, §4.4)이다.
- **주문 데이터 접근은 조회로 통일(C-6)**: 주문 미러는 존재하지 않는다(§3.6 삭제) — 추천 dedup(결정 14-F)·프로필(결정 16)은 **질의 시점 구매 이력 조회(§4.7)** 를 사용하고, 판매자 통계는 I-6 콜백(§4.4)을 사용한다.

> **MVP 비범위(명시)**: 리뷰 인사이트(측면별 감성)는 판매자 agent MVP에 **포함하지 않는다**(고도화). 본 엔드포인트는 판매 통계 Q&A + 상세 수정 draft만 다룬다.

### 3.3 상품 목록 경로 B (Product List — Path B)

**[HARD] 구매자 SSE 스트림은 상품 카드를 싣지 않는다.** 상품 목록은 아래 경로 B로 전달된다(후보는 질의 시점 Spring 검색 §4.6에서 확보):

```
[0] AI: decompose → Spring POST /products/search 위임 조회(§4.6) → 후보 목록(price 포함) → rerank(profile_summary)
[1] AI: rerank 완료 → listId 생성 → 최종 id 목록 push (AI → Spring, I-21 §4.2)
        POST {SPRING_BASE_URL}/internal/recommendations { sessionId, listId, productIds:[Top5 숫자] }
[2] Spring: productIds를 Redis에 listId 키로 TTL 저장 + 표시 필드(price·imageUrl·reviewCount 등) enrich
[3] AI: 콜백 성공 → SSE `products.ready`({ sessionId, listIds:[listId] }) emit (reason은 콜백에 포함돼 CH-5로 전달)
[4] FE: `products.ready` 수신 → 카드 GET (FE → Spring, CH-5 §4.3) → 우측 상품 패널 렌더
```

**설계 근거(rationale)**: 우측 상품 패널은 **Spring이 서빙하는 UI**다. **표시 권위는 Spring**에 남고, AI는 표시 필드(가격·이미지·리뷰수·재고)를 보유·전달하지 않는다(결정 9-B 유지·강화). AI가 전달하는 것은 **추천 로직의 산출물**(어떤 상품을, 어떤 순위로, 왜)뿐이다. §4.6 검색이 돌려주는 price는 **rerank·예산 검증(AI-side)용 질의 시점 값**이며, 우측 패널 표시가는 여전히 Spring이 목록 GET(§4.3)에서 채운다.

**push 실패 처리**: 목록 push(§4.2)가 실패하면 — 챗 텍스트는 정상 완료되고 지연 안내를 포함하며, 스트림은 `error`가 아니라 **`done`** 으로 종료하고 `products.ready`는 emit하지 않는다.

> **[point 조회 폐기]** 구 v0.2.0 "상품 point 조회 API"는 **완전 삭제**된다. 표시 필드는 소비자 point 조회가 아니라 **Spring이 목록 enrich 시점에 채운다**(§4.3). product.md 신규 결정 레코드 필요(§8 항목 1).

### 3.4 `GET /profile/me` — 마이페이지 프로필 조회 (FE 직접)

마이페이지 표시용으로 **토큰 소유자 본인의** 사람이 읽는 프로필 마크다운을 반환한다(자연어 마크다운 passthrough). 소유: `SPEC-PROFILE-001` §5.4/§6.9. MVP는 **조회(GET)만** 제공하며 편집(PUT)은 고도화 범위다.

> **[보안] 경로에서 `{userId}` 제거 — `GET /profile/me`**: `GET /profile/{userId}`는 **IDOR** 위험이 있어, 조회 대상 신원을 **토큰 클레임(`sub`)에서 도출**하는 `GET /profile/me`를 채택한다(결정 19).
> - **SPEC 동기화 필요 🔴**: `SPEC-PROFILE-001` §5.4/§6.9는 `GET /profile/{user_id}`로 정의되어 있으므로 `/me` 채택 및 camelCase 정렬(§2.2)에 맞춘 **동기화 개정**이 필요하다(§7).

#### 요청 (Request)

```
GET /profile/me
```

- 경로 파라미터 없음. 조회 대상은 `Authorization` 헤더 JWT의 `sub` 클레임에서 도출한다.
- 게스트 토큰(`role == "guest"`): 프로필이 없으므로 `exists = false` 정상 응답.

#### 응답 (Response) — `application/json` (camelCase 정렬 제안)

```json
{
  "userId": "string",
  "exists": true,
  "markdown": "# 취향 요약\n- 3~5만원대 무선 이어폰 선호\n...",
  "generatedAt": "2026-07-13T21:04:00Z"
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `userId` | string | 요청 대상 식별자(토큰 `sub` 도출) |
| `exists` | bool | 프로필 존재 여부. 게스트·신규 회원 `false` |
| `markdown` | string \| null | 사람이 읽는 프로필 마크다운. 미존재 시 `null` |
| `generatedAt` | string \| null | 요약 생성 시각(ISO-8601). 미존재 시 `null` |

- 게스트/프로필 미보유: `exists = false`, `markdown = null`을 **오류가 아닌 정상 응답(200)** 으로 반환한다(SPEC-PROFILE-001 REQ-PROF-081).
- **PUT 미제공**: 프로필 편집은 고도화 범위(SPEC-PROFILE-001 EX-P3).

### 3.5 `POST {AI_SERVER}/events/session-end` (I-20) — 세션 종료 통지 (Spring → AI, best-effort, 멱등, 본 문서 소유)

Spring이 세션 종료를 감지해 프로필 파이프라인 **조기 트리거**로 전달한다(결정 12/16). **[개정 v0.16.0]** I-20에서 Spring이 보내는 알려진 `reason`은 **`logout` 1종**이다 — 축 분리 후 "새 대화"는 FE가 `threadId`만 새로 만들어 세션을 유지하므로(§2.6) `newConversation`은 더 이상 발화되지 않는다. `reason`은 wire enum을 강제하지 않지만 최대 64자로 제한한다. **`tabClose` 신호는 사용하지 않으며**, 비활동 종료(`inactivityTimeout`)는 HTTP 통지나 자기 호출 없이 AI 내부 스케줄러가 판정한다. HTTP 계약은 본 문서 소유(결정 21), 수신·내부 timeout 동작은 `SPEC-PROFILE-001`.

> **[경로/방향 정합 v0.17.0]** I-20은 **AI 서버가 호스팅하는 inbound 엔드포인트**(Spring→AI)다. `app/api/events.py`가 먼저 회원 lifecycle을 `terminal`로 닫고, 등록된 모든 thread의 filter/cart/revert transient를 일괄 정리한 뒤 고정된 watermark까지 프로필 phase를 처리한다. transcript는 삭제하지 않는다. AI가 Spring을 호출하는 역방향(§4)이 아니다.

#### 요청 (Request) — **[v0.15.17 확정, 이슈 #62]** BE 실측 페이로드 정렬

```json
{
  "userId": 123,
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "reason": "logout"
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `userId` | number(BIGINT) | 예 | 세션 소유 회원 식별자. 양의 정수만 허용하며 string/float/bool coercion은 거부(JWT `sub`와 동종 숫자 id, 프로필 스코프) |
| `sessionId` | string | 예 | 종료된 **접속** 식별자(UUID 포함 불투명 문자열, §2.6). **방(`threadId`) 단위 종료 통지는 없다** — 종료는 접속 단위다. 최대 길이 = config `chat_key_max_chars`(§2.6) |
| `reason` | string | 아니오 | Spring이 관측한 종료 사유(**[v0.16.0]** 알려진 값 `logout` 1종) — **enum 미강제·최대 64자**. 구 `newConversation` 은 발화되지 않는다(§2.6) |

> **[v0.15.17 변경 — 이슈 #62]** 구 초안의 `eventId`·`endedAt`를 **제거**하고 `userId`를 **string → number(BIGINT 정수)**로 정정했다(BE 실측 payload 정합). 멱등 키는 별도 필드 대신 **`(userId, sessionId)` 고정 파생키**(§2.7)로 전환한다. 종전 스키마와 불일치해 `POST /events/session-end`가 상시 `400`을 반환하던 문제를 해소한다.

- **Spring 명시적 종료 [개정 v0.16.0]**: **`LOGOUT` 하나만** I-20을 발화한다. 이 경로는 세션을 삭제하므로 한 `sessionId`에는 하나의 논리적 종료만 존재한다. 구 `NEW_CONVERSATION`은 **제거** — 새 대화가 `threadId`만 갱신하게 되어(§2.6) CH-1도 I-20도 호출되지 않는다. 결과적으로 **Spring이 I-20을 쏘는 경우는 로그아웃뿐**이고, 나머지(세션 TTL 만료)는 Redis 만료 + AI 내부 비활동 sweep이 담당한다.
- **AI 내부 비활동 종료(D6, #187)**: guest/member 구매자 turn과 lifecycle touch를 같은 transaction에서 commit하고 `chat_session_contexts.last_activity_at`을 DB 시각으로 갱신한다. 단일 인스턴스 scheduler가 기본 60초마다 기본 10분 이상 비활성인 `active` context를 bounded batch로 선점한다. 어느 `threadId`의 touch든 접속 전체 deadline을 연장하고, 만료되면 등록된 모든 thread의 filter/cart/revert와 thread registry를 같은 context phase로 정리한다. transcript는 보존한다. AI가 자기 `/events/session-end`를 HTTP 호출하지 않는다.
- **탭 닫기**: 별도 종료 신호를 두지 않는다. 사용자가 threshold 전에 어느 탭에서든 돌아오면 세 탭이 함께 살아남는다. `idle_expired` 뒤 정당한 같은 owner가 돌아오면 generation을 올리고 **같은 `context_id`**를 재활성화한다. `idle_finalizing` 중 touch는 `409 SESSION_FINALIZING`이다.
- **best-effort**: Spring I-20이 유실돼도 D6 sweep이 guest/member transient를 회수하고 회원 profile watermark를 후속 phase에서 처리한다.
- **멱등·직렬화**: I-20은 session advisory lock에서 `terminal` gate와 generation을 먼저 commit한 뒤 활성 member stream 종료를 기다린다. `chat_session_finalizations`의 유한 lease/claim token, watermark, transient/profile phase가 crash·retry를 재개한다. 동일 I-20은 `duplicate`; 실패한 profile phase는 `retryable`이며 transcript와 미처리 buffer를 보존한다.
- event inbound는 camelCase alias만 허용한다. unknown field, snake_case field, camelCase+snake_case collision은 `400 BAD_REQUEST`다.
- 응답: `202 Accepted`(신규 `{"status":"accepted"}` / 중복 `{"status":"duplicate"}`). `userId`·`sessionId` 누락·타입 오류 또는 `reason` 64자 초과는 `400`(§2.5 봉투). lifecycle PostgreSQL의 timeout/pool/connection 장애는 `503 STATE_UNAVAILABLE`이며 programming/integrity/domain/cancellation 오류를 503으로 마스킹하지 않는다.


#### 3.5.1 `POST {AI_SERVER}/events/session-claim` — guest → member 소유권 승격 (#187)

Spring은 로그인 완료 후 guest 접속 전체를 회원에게 넘기기 위해 이 inbound를 호출한다.
`X-Internal-Token`이 필수이며 사용자 JWT나 body의 임의 신원을 대신 신뢰하지 않는다.

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "guestId": "guest-uuid-or-id",
  "userId": 123
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sessionId` | string | 예 | BE가 로그인 전후 이어 쓸 접속 id. 빈 문자열 금지, 최대 `chat_key_max_chars` |
| `guestId` | string | 예 | 로그인 직전 서명 티켓의 guest `sub`와 같은 소유자. 빈 문자열 금지, 최대 `chat_key_max_chars` |
| `userId` | number(BIGINT) | 예 | 로그인 완료 회원 id. **strict 양의 정수 `1..2^63-1`**이며 string/float/bool coercion 금지 |

**응답**

- 최초 원자 전이: `202 {"status":"accepted"}`
- 동일 `(sessionId, guestId, userId)` 재전송: `202 {"status":"duplicate"}`

전이는 `chat_session_contexts`의 owner와 generation만 갱신한다. `context_id`, 등록된
`threadId`, filter/cart/revert 상태는 유지하며 로그인 시점 복사는 하지 않는다. 기존 guest
transcript도 보존하지만 member profile buffer로 복사하지 않는다. 전이 중에는 해당
`(guestId, sessionId)` active-stream scope에 fence를 걸며 활성 stream이 있으면 받지 않는다.

**오류**

| HTTP | `code` | 조건 |
|---|---|---|
| `401` | `INTERNAL_TOKEN_INVALID` | 운영에서 서비스 토큰 누락/불일치 |
| `400` | `BAD_REQUEST` | 필수 필드/strict 타입 오류, 빈/초과 길이 id, `userId`가 `1..2^63-1` 밖이거나 bool |
| `409` | `SESSION_ACTIVE` | guest scope 활성 stream 존재 |
| `409` | `SESSION_FINALIZING` | idle finalization 진행 중 |
| `409` | `SESSION_CLAIM_CONFLICT` | 다른 claim 이력, terminal, owner 불일치 |
| `503` | `STATE_UNAVAILABLE` | lifecycle PostgreSQL 정본 사용 불가 |

두 event 모델은 camelCase alias만 허용한다. unknown field, snake_case field,
camelCase+snake_case collision은 `400 BAD_REQUEST`다.

claim commit 뒤 옛 guest 티켓의 `/chat`은 `403 SESSION_FORBIDDEN`이고, 새 turn/thread를
만들지 않는다. member 티켓은 같은 signed `sessionId`와 기존 `context_id`로 모든 탭을 계속한다.

**배포 순서(필수)**: BE #63이 signed `sessionId`와 `ticketTtlSeconds=60` 증거를 먼저 남긴다 → 마지막 구 계약 티켓 뒤 90초(60초 TTL + 30초 여유) drain → AI enforcement/schema/backfill/scheduler 배포 → FE #52 실제 3-tab 로그인/refresh → missing-session·claim-conflict·cleanup-retry 지표 확인. 이 저장소는 BE/FE/운영 gate를 실행할 수 없으며 완료했다고 주장하지 않는다.

### 3.6 (삭제) 주문 이벤트 — 채택하지 않음 [v0.5.0]

**[v0.5.0 삭제]** 구 `POST /events/order`(주문 이벤트 미러)는 **채택하지 않는다**(2026-07-15 사용자 확정). 검색이 질의 시점 Spring 위임(§4.6)으로 확정되면서 구매 이력도 **추천 직전 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 확보한다 — 알림 수신도, 미러 테이블도 없다. 결정 14-F의 동작 요구(exact `productId` 제외·소모품 카테고리 억제·되돌리기 칩)는 **불변**이며 데이터 획득 방식만 교체된다. 프로필 파이프라인의 구매 소스도 sleep-time 배치가 동일 API(§4.7)를 조회한다(SPEC-PROFILE-001 개정 필요, §7.2).

> **[v0.5.0] 카탈로그 동기화 채널 없음**: AI 카탈로그 사본(미러)을 채택하지 않으므로 카탈로그 변경 이벤트 채널도, 배치 폴링도 **존재하지 않는다**(2026-07-15 확정, §4.6 말미). Spring → AI 이벤트는 §3.5의 `/events/session-end`와 `/events/session-claim`만 남는다.

### 3.7 `POST {AI_SERVER}/internal/recommendations/home` (I-22) — 메인 화면 추천 목록 (Spring → AI) [v0.18.0 신설]

홈 화면 "OO님을 위한 추천"(P-5, §4.11)의 개인화 랭킹을 Spring이 AI에 위임한다. **Spring이 호출 주체**이므로 채팅 경로(CH-2 → I-21 → CH-5)와 달리 **왕복 1회로 끝난다** — 응답 본문에 목록이 담겨 오고 **I-21 콜백(§4.2)을 타지 않는다**. 정본 확정 2026-07-28.

```
POST {AI_SERVER}/internal/recommendations/home
X-Internal-Token: {서비스 토큰}   ← internal 그룹 (레인 b)
```

- **인증**: `X-Internal-Token` 서비스 토큰(§2.3 b). 사용자 JWT를 받지 않는다.
- **예산**: **P-5 예산 내 — 연결 2s / 응답 3s.** 채팅 스트림 상한(90s, §2.9 c)과 무관하며 **메인 렌더 블로킹 방지**가 목적이다.

#### Spring → AI 요청 (I-22)

```json
{
  "memberId": 123,
  "limit": 12,
  "catalogVersion": "catalog-20260728T0300Z",
  "signals": {
    "recentlyViewedProductIds": [552, 88, 101],
    "cartProductIds": [205],
    "recentPurchasedProductIds": [77, 91]
  }
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `memberId` | number | 예 | 회원 BIGINT(§2.6). **게스트는 이 API를 호출하지 않는다** — 개인화 대상이 아니라 Spring이 P-4(인기 상품)로 처리한다 |
| `limit` | number | 예 | **최종 노출 목표 개수.** AI는 Spring의 품절 드롭에 대비해 **이보다 넉넉히 반환**하고, Spring이 판매 불가 상품을 뺀 뒤 `limit`만큼 자른다 |
| `catalogVersion` | string | 예 | 후보 산출 시점 식별자. 캐시 키·재현에 쓴다. **🔴 값 생성 주체 미해결 — C-18** |
| `signals` | object | 예 | 아래 3종. 비어 있으면 개인화 근거가 없어 `NO_PROFILE`로 답한다 |

| `signals` 필드 | 출처(Spring) | AI가 쓰는 방식 |
|---|---|---|
| `recentlyViewedProductIds` | `product_view` 이벤트 | 해당 상품 임베딩을 프로필 벡터와 **가중 혼합**(최신일수록 높게) |
| `cartProductIds` | 현재 장바구니(미결제) | 같은 방식, **가중치 더 높게** — 담기까지 갔다는 건 강한 신호다 |
| `recentPurchasedProductIds` | 최근 주문 | **결과에서 제외**한다(이미 샀으므로). **가중치가 아니라 필터다** |

> **`sessionId`가 없다** — 홈에는 채팅 세션이 없다. 신원은 `memberId`만으로 충분하다. 따라서 §2.6의 `sessionId`/`threadId` 축(§2.9 동시 스트림 락 포함)은 이 API에 적용되지 않는다.

#### 성공 응답 — 200

```json
{
  "outcome": "PERSONALIZED",
  "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
  "listId": "7c1e9f2a4b8d43f5a0c6d1e97b3f8a24",
  "items": [
    { "productId": 101, "reason": "최근 선호한 가격대와 카테고리에 맞아요" },
    { "productId": 552, "reason": null }
  ]
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `outcome` | string | `PERSONALIZED` \| `NO_PROFILE` \| `INSUFFICIENT_CANDIDATES` — 아래 표 |
| `recommendationRequestId` | string | 추천 실행 1회를 가리키는 id. **AI가 생성**(I-21의 상관키와 같은 역할) |
| `listId` | string | **[HARD] AI가 생성하는 UUID급 무작위 식별자(≥128bit)** — I-21(§4.2)과 **동일 규칙**. 순번·타임스탬프 등 추측 가능한 형식 금지. Spring이 저장하고 P-5 응답에 실어 FE에 전달하며, 노출·클릭 이벤트가 이 키로 추천에 귀속된다 |
| `items` | array | `{productId(숫자), reason}`. **배열 순서가 곧 순위다** — `position`을 싣지 않는다 |
| `items[].reason` | string\|null | 카드에 표시할 이유. 없으면 `null`(I-21의 `reasons` 생략 규약과 달리 **필드 자체는 유지**) |

**`outcome` 처리**

| 값 | 뜻 | Spring 동작 |
|---|---|---|
| `PERSONALIZED` | 개인화 성공 | 그대로 사용, P-5 `source=AI_RECOMMENDED` |
| `NO_PROFILE` | 프로필 없음(신규 회원) 또는 `signals`가 비어 개인화 근거 없음 | **P-4 인기상품으로 대체**, `source=POPULAR_FALLBACK` · `fallbackReason=PROFILE_MISSING` |
| `INSUFFICIENT_CANDIDATES` | 후보가 부족해 랭킹이 무의미 | 동일하게 P-4 대체, `fallbackReason=INSUFFICIENT_CANDIDATES` |

> **[HARD] cold start는 오류가 아니다.** 프로필이 없어도 **200 + `outcome`** 으로 답하며, **fallback 판단은 Spring이 한다.** 프로필 부재·후보 부족을 이유로 AI가 4xx/5xx를 내서는 안 된다.

#### 실패 응답

| HTTP | `code` | 조건 |
|---|---|---|
| 400 | `BAD_REQUEST` | 필수 필드 누락·타입 오류 |
| 401 | `INTERNAL_TOKEN_INVALID` | 토큰 없음/불일치 |
| 503 | `UPSTREAM_UNAVAILABLE` | 내부 의존성(프로필 저장소·LLM) 일시 장애 |
| 504 | `UPSTREAM_TIMEOUT` | 예산 초과 |

> 위 4종은 **입력·인프라 실패에만** 쓴다 — 앞의 [HARD]와 충돌하지 않는다. cold start(`NO_PROFILE`)·후보 부족(`INSUFFICIENT_CANDIDATES`)은 **정상 200**이다.
>
> 4xx/5xx가 나가면 Spring이 P-4로 대체하고 `fallbackReason=AI_ERROR` 또는 `AI_TIMEOUT`으로 기록한다. **어느 경우에도 홈 렌더는 막히지 않는다.**

#### 규약

- **멱등이 아니다** — 재시도하면 **새 `recommendationRequestId`·`listId`가 발급**되며 Spring이 새 추천 실행으로 센다. §2.7의 `/events/*` 멱등 규약은 이 API에 적용되지 않는다.
- **지면(`surface`)은 요청·응답에 싣지 않는다** — 이 API로 만들어진 목록은 `HOME`, I-21로 온 것은 `CHAT`이라 Spring이 **경로로 판단**해 저장한다.
- **`recommendation_generated`는 Spring이 server-side로 기록한다** — 목록 저장 시점에 `behavior_events`에 직접 적재(I-21과 동일 규칙). P-4 fallback으로 대체한 경우에도 `source=POPULAR_FALLBACK`으로 구분해 기록한다. **AI는 이 이벤트를 적재하지 않는다.**
- **[HARD] 프로필 원문·prompt·모델 식별자는 응답·로그·trace에 포함하지 않는다.** 알고리즘·모델 버전은 **AI가 자체 테이블에 보관**하며 **와이어에 싣지 않는다**(평가 산출물 전용). → `algorithmVersion`·`modelVersion`을 응답에 넣는 구현은 계약 위반이다. §6.3 관측 경계와 동일한 기준이다.
- **후보 확보 경로가 채팅과 다르다** — AI는 **자체 카탈로그 인덱스(I-17로 동기화된 임베딩, §4.8)** 로 순위를 매긴다. Spring이 후보 목록을 요청에 싣지 않으며, **재고·판매상태 반영은 Spring이 카드 조립 시점**에 한다(CH-5와 동일 패턴). ※ §1.2의 "[HARD] 후보 확보 = 질의 시점 Spring 검색(I-1)"은 **채팅 경로의 규약**이고, 홈은 이 절이 정한 자체 인덱스 경로를 쓴다.
- **캐시·TTL은 P-5 소관**(§4.11) — 개인화 결과 캐시 10분, `listId` 귀속 유효기간 24시간(2026-07-30 확정). 채팅 CH-5의 10분(조회 만료)과는 성격이 다르다.

#### 구현 노트 (v0.18.0, #148) — 계약과 다른 지점

와이어 계약(필드·`outcome`·오류 코드)은 위와 **완전히 일치**한다. 아래는 계약이 규정하지 않았거나, 규정했으나 현 구현이 다르게 채운 내부 동작이다. BE는 읽을 필요가 없고, AI측 후속 작업자가 알아야 한다.

- **프로필 벡터를 질의에 섞지 않는다.** 위 signals 표는 *"해당 상품 임베딩을 **프로필 벡터**와 가중 혼합"* 이라 쓰지만, 현재 프로필은 자연어 markdown 요약이라(`SPEC-PROFILE-001`) 벡터가 없다. 요청 경로에서 프로필을 임베딩하면 Google 임베딩 API 왕복이 하나 더 붙어 **연결 2s/응답 3s 예산을 위협**한다. 그래서 **질의 벡터는 signals만으로** 만들고 프로필은 `reason` 문장의 취향 근거로만 쓴다. 프로필 벡터가 생기면 `build_query_vector`에 항을 더하는 것이 전부다(주입형 seam).
- **`NO_PROFILE`의 판정 기준 = 질의 벡터를 만들 수 없음.** 정본은 사유를 *"프로필 없음(신규 회원)"* 과 *"signals 비어있음"* 두 갈래로 적었으나, 위 이유로 랭킹이 signals에서만 나오므로 **시그널 임베딩 0건**이 실질 판정이다(시그널이 아직 I-17로 인덱싱되지 않은 경우 포함). Spring 입장에선 셋 다 `PROFILE_MISSING`으로 P-4 대체라 구분 실익이 없다.
- **503의 범위** — 실패 응답표의 *"내부 의존성(프로필 저장소·LLM) 일시 장애"* 중 **프로필 저장소·LLM 장애는 503을 내지 않는다.** 둘 다 이 구현에선 `reason` 품질에만 영향을 주고 랭킹을 막지 않아 degrade가 옳다(홈 렌더 비차단이 §4.11 취지). **503은 카탈로그 인덱스 장애에만** 쓴다 — 랭킹의 유일한 입력이라 여기가 죽으면 시도조차 못 한다.
- **`catalogVersion`을 랭킹에 쓰지 않는다**(C-18 미해결) — 받아서 검증만 하고 버린다. AI는 자체 인덱스 현재 상태로 랭킹하므로 이 값에 의존하지 않는다. C-18이 "AI가 값을 만든다"로 확정되면 응답에 싣는 개정이 따른다.
- **🟡 성능 미검증** — 랭킹이 `store.all()` 전량을 파이썬으로 코사인 계산한다. 카탈로그가 커지면 3s 예산을 넘길 수 있다. pgvector `ORDER BY embedding <=> :q LIMIT k`로 밀어야 하나, 현재 `PgCatalogArtifactStore`에 해당 메서드가 없고 **라이브 환경(임베딩 API·Spring 자격증명) 부재로 실측을 못 했다.** 인덱스 적재 후 측정이 선행 과제다.

---

## 4. AI 서버 ↔ Spring 역방향/전제 계약

AI → Spring 역방향은 **정확히 17건**이다:
`{I-1,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`.
각 이름과 계약 위치는 §1.2 레인 (c)를 따르며, FE ↔ Spring 전제 계약(목록 GET §4.3)은
이 집합에 포함하지 않는다. 모든 internal 호출은 `X-Internal-Token`을 사용하고, 사용자·판매자
스코프 신원은 AI가 검증 JWT 클레임에서만 도출한다.

### 4.1 장바구니 담기 API (I-2, 결정 7) — BE 문서 채택 [v0.6.0]

**BE 팀 "챗봇 장바구니 담기"(No. I-2) 문서를 계약 기준으로 채택**한다. AI 서버는 "담아줘" 자연어에서 (상품, 옵션, 수량) 의도만 확정하고, 담기 실행·검증은 Spring에 위임한다(결정 7 유지). 구 v0.3.0 제안(JWT 포워딩 + `items[]` 다건)은 **폐기**한다.

#### AI → Spring 요청 (I-2 확정)

```
POST {SPRING_BASE_URL}/internal/cart/items
X-Internal-Token: {서비스 토큰}   ← internal 그룹, 타임아웃 권장 3s
```

```json
{ "userId": 123, "guestId": null, "productId": 1, "optionId": null, "quantity": 1 }
```

| 요청 필드 | 타입 | 설명 |
|---|---|---|
| `userId` / `guestId` | number / string \| null | **둘 중 하나** — 챗 요청의 메아리(`userId`=숫자, `guestId`=UUID 문자열, §2.6). AI가 신원을 만들지 않고 **AI-검증 JWT `sub`에서 도출**해 전달한다(FE 본문 값 사용 금지, §2.3) |
| `productId` | number | 담을 상품 식별자(숫자 BIGINT, §2.6) |
| `optionId` | number \| null | 상품 옵션. 옵션 필수 상품인데 null이면 `400 CART_OPTION_REQUIRED`(아래) |
| `quantity` | int | **1~99, 합산 포함** — 동일 상품·옵션이 이미 있으면 **Spring이 수량 합산**(입구가 달라도 같은 CartService 검증) |

- **단건 계약** — Case 3 묶음 담기는 상품별로 **반복 호출**한다(항목별 성공/실패가 자연 분리되므로 SSE `action`도 항목별 emit).
- **게스트 담기 허용** — `role == "guest"`여도 `guestId`로 담기 성공(BE 02 D30, 2026-07-10 개정 — 기존 403 유도 폐기). **로그인 유도는 결제 시점 FE 몫.** 구 AI-side 차단(`GUEST_NOT_ALLOWED`)은 폐기 — **결정 8 개정 필요(§8 항목 7)**.
- **합산 안내(v0.6.0)**: 담기 전 §4.9 조회로 동일 상품·옵션 기존 보유를 확인하면 "이미 담겨 있어 N개로 늘렸어요"처럼 안내할 수 있다. **합산의 권위는 Spring**(조회는 안내용 — 조회 실패 시에도 담기는 진행).
- **부수효과 없음 — 담기 이벤트는 서버가 적재하지 않는다** **[정정 v0.15.26]**. 구 서술("`CART_ADD(via: chat)` 이벤트는 BE가 적재")은 **폐기**한다. E-1 정본에서 `add_to_cart`는 **FE가 쏘는 12종 중 하나**이며, 서버가 직접 적재하는 이벤트는 `recommendation_generated` **하나뿐**이다(그것도 E-1 HTTP로 들어오면 드롭). 챗봇 경로도 FE가 SSE `action`(`CART_ADDED`)을 받은 시점에 `add_to_cart`를 쏜다.

#### 성공 응답 — 200

```json
{ "success": true, "data": { "cartItemId": 55 } }
```

`cartItemId`는 SSE `action`(`CART_ADDED`)에 사용한다(§3.1).

#### 실패 응답 — I-2 오류 코드와 AI 동작 매핑

| HTTP | I-2 `code` | 조건 | AI 동작 |
|---|---|---|---|
| 400 | `CART_OPTION_REQUIRED` | 옵션 필수인데 `optionId` 없음 — **`error.detail.options`에 `[{optionId, name, extraPrice}]` 포함**(BE 확정 2026-07-18) | **되물음 멀티턴**: 실패 `action` 없이 `token`으로 "어떤 색상으로 담을까요?" 재질문 → 다음 턴에서 사용자 답을 `optionId`로 해석해 재담기 |
| 400 | `CART_OPTION_INVALID` | 옵션이 해당 상품 소속 아님 | AI가 `optionId` 해석 오류 — options 목록 재확인 후 **되물음 재시도**(1회), 반복 실패 시 `action` `CART_ERROR` |
| 404 | `PRODUCT_NOT_FOUND` | 없는 상품 | `action` `CART_ADD_FAILED` + `reason: "PRODUCT_NOT_FOUND"` |
| 400 | `VALIDATION_ERROR` | 합산 수량 > 99(수량 상한, **재고검사보다 먼저** 걸림) | `action` `CART_ADD_FAILED` + `reason: "CART_ERROR"` + message "수량은 최대 99개까지 담을 수 있습니다."(BE 문구와 동일; 99=BE `CartItem.MAX_QUANTITY`) |
| 400 | `CART_STOCK_INSUFFICIENT` | 합산 수량 > 재고(재고는 상품 단위) — **`error.detail.availableStock`에 남은 재고 수 포함**(2026-07-22 신설) | `action` `CART_ADD_FAILED` + `reason: "STOCK_INSUFFICIENT"` + message에 남은 재고 수 노출("재고가 N개뿐이에요"; **재고 0=품절이면 "품절된 상품이에요"**, `availableStock` 미상 시 일반 안내) |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음/불일치 | 운영 오류 — 사용자에게는 `action` `CART_ERROR`로 안내, 서버 로그/알림 |

> **잔여 협의(C-3)** — 대부분 해소, 서비스 토큰만 남음: (1) ~~재고 오류~~ **해소(v0.15.16)** — 담기 재고검증 **있음**: BE `400 CART_STOCK_INSUFFICIENT` + `availableStock`(2026-07-22) → `reason STOCK_INSUFFICIENT`(구 `OUT_OF_STOCK` 폐기 유지, 품절≠재고부족). (2) ~~`CART_OPTION_REQUIRED` options 스키마~~ **해소(BE 2026-07-18)** — `error.detail.options: [{optionId, name, extraPrice}]`. (3) ~~`productId` 타입~~ **해소(v0.15.3)** — 숫자 BIGINT(§2.6). (4) 🔴 **서비스 토큰(`X-Internal-Token`) 발급·교환 방식** — 유일 잔여.

### 4.2 추천 목록 전달 API (I-21 `POST /internal/recommendations`) — [BE DB 등재 v0.15.0, reasons 확정 v0.15.15 🟢]

rerank 완료 후 AI가 **최종 랭크 상품 id(Top5)만** Spring에 POST한다. Spring이 Redis에 TTL 저장하고 표시 필드를 enrich하며, FE가 **CH-5**(§4.3)로 카드를 조회한다. **[07/17 BE 신설]** 합의된 추천 흐름 6번("FastAPI가 최종 추천 상품 ID만 Spring에 전달")의 실제 API.

#### AI → Spring 요청 (I-21)

```
POST {SPRING_BASE_URL}/internal/recommendations
X-Internal-Token: {서비스 토큰}   ← internal 그룹, 3s
```

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "listId": "9f2c1a7e4b8d43f5a0c6e1d97b3f8a24",
  "productIds": [101, 205, 552, 88, 13],
  "reasons": [
    { "productId": 101, "reason": "방수 등급이 높아 우천 시에도 안전합니다." },
    { "productId": 205, "reason": "가벼워 휴대가 편합니다." }
  ]
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `sessionId` | string(UUID) | 상관관계 키(`products.ready`와 상관) |
| `listId` | string | **[HARD] FastAPI가 생성하는 UUID급 무작위 식별자(≥128bit)** — 순번·타임스탬프 등 추측 가능한 형식 금지. 현재 구현은 `uuid4().hex` 32자리 lowercase hex. Spring이 Redis에 이 키로 TTL 저장하고 FE가 CH-5로 조회 |
| `productIds` | number[] | 최종 랭크 상품 id(Top5). **순서 유지 = 렌더 순서**(리랭킹 순서). 숫자 id(§2.6 internal) |
| `reasons` | array | **[확정 v0.15.15, BE 구현 2026-07-18] 상품별 추천 근거** `{productId(숫자), reason}` — productId로 키잉(순서 권위는 `productIds`, 부분집합/순서무관). Spring이 Redis 저장 → **CH-5 카드에 `reason` echo**(§4.3). 선택 필드 — 근거 없는 상품은 생략(🟢). **`reason` 생성 목표 = 한글 ≤40자 1문장**(rerank 프롬프트). AI가 push 전 개행 제거·안전 상한(config `reason_max_len`) 방어 정제하고, **표시 오버플로(줄임/더보기)는 FE 소관**(경로 B, 표시 권위=FE) |

- **[변경 07/17] payload = id 배열만** — 구 §4.2 `groups[{title,category,items[{productId,rank,reason}]}]` 구조는 **폐기**. 묶음 제목·순위·근거는 콜백에 싣지 않는다.
- **`listId`는 FastAPI가 UUID급 무작위(≥128bit)로 생성한다.** 구 "Spring이 listId 발급" 가정과 `list-4471` 같은 순번형 예시는 폐기한다. CH-5가 인증 불필요 공개 조회라 `listId`가 사실상 bearer 키이므로 **순번·타임스탬프 등 추측 가능한 형식은 금지**한다. 여기서 `≥128bit`는 **식별자 표현 폭** 기준이며, 현재 UUIDv4 구현은 128bit UUID 중 version·variant 고정 비트를 제외한 122bit를 무작위로 생성한다. **TTL = 10분(config, 세션 TTL 이하) 제안** — FE가 products.ready 직후 CH-5 조회하므로 짧아도 됨, 🔴 미확정.
- **[확정 v0.15.15] `reason`은 이 콜백에 포함**(🟢, BE 구현 2026-07-18) — `reasons[{productId, reason}]`를 Spring이 Redis 저장 → **CH-5 카드에 echo**(§4.3)해 FE에 전달. 구 BE 07/17 제안(reason=SSE·콜백 불포함)은 폐기 — SSE(`products.ready`)는 상관키만 유지, 경로 B 일관·FE join 불필요. AI→Spring 전송분은 jarvis-ai 이슈 #61에서 구현.
- **규약**: FastAPI는 이 콜백이 **성공한 뒤에만** SSE `products.ready`({sessionId, listIds:[listId]})를 발행한다 — 콜백 실패 시 미발행(FE가 빈 목록 조회 방지, §3.3).
- **인증**: `X-Internal-Token` 서비스 토큰.
- 🔴 협의(C-9): `listId` TTL·재조회 정책. (`listId` 형식은 v0.16.1에서 UUID급 무작위 ≥128bit로, `reason` 전달 방식은 v0.15.15에서 콜백 포함으로 확정 🟢.)

### 4.3 추천 목록/카드 조회 (CH-5 `GET /api/chat/lists/{listId}`, FE ↔ Spring 전제 계약) — [BE DB 등재 v0.15.0, 스키마 OPEN]

FE가 `products.ready` 수신 후 Spring에서 **표시 필드가 채워진** 추천 카드를 조회한다. **[07/17 BE 신설] 구 P-7을 대체**하는 CH-5. **이 계약은 FE ↔ Spring 간이며 AI 서버는 관여하지 않는다**(레인 d). AI는 I-21(§4.2)로 **id만** 넘기고, 카드 표시 필드는 Spring이 채운다.

#### FE → Spring 요청 (CH-5)

```
GET {SPRING_BASE_URL}/api/chat/lists/{listId}   ← BE DB(CH-5). listId = I-21에서 FastAPI가 넘긴 값
```

- Spring이 I-21로 받은 `productIds`를 자기 DB에서 enrich(name·price·image·reviewCount 등)해 **순서 유지 카드**로 반환.
- **표시 권위 = Spring**: `price`·`originalPrice`·`imageUrl`·`reviewCount`·`availability`를 Spring이 채운다(결정 9-B).
- **카드 응답 스키마는 FE ↔ Spring이 확정**(LLM 사안 아님). **[확정 v0.15.15] 카드 항목에 `reason` 포함** — Spring이 I-21에서 받은 값을 echo(§4.2)해 FE는 카드+이유를 한 번에 받는다(BE 구현 07-18). 나머지 카드 표시 필드 스키마는 OPEN 🔴(§5 C-12).

### 4.4 판매자 집계 조회 API (I-6, query-time) — 🔴 제안(초안) [v0.4.0 신설, Batch 1]

판매자 통계 답변의 원천. AI가 질의 시점에 이 API를 호출해 **집계값만** 받는다(원시 로그 미제공). C-7 해소의 핵심 계약이다.

#### AI → Spring 요청 (제안)

**[개정 v0.8.0]** BE 문서 기준으로 판매자 집계는 **단일 API가 아니라 `brandId` 스코프 집계 5종**이다(전부 `internal`·`X-Internal-Token`·3s). `brandId`는 검증된 판매자 JWT 클레임에서 얻는다(§2.3·§2.6). **전체 5종 반영·I-number 정합은 #9로 진행** — 아래는 대표(매출 시계열).

```
GET {SPRING_BASE_URL}/internal/seller/{brandId}/sales?from={d}&to={d}&granularity={g}
X-Internal-Token: {서비스 토큰}
```

**판매자 조회/집계 7종 (BE 실제 No., 전부 GET·internal·`X-Internal-Token`)**:

| BE No. | 경로 | 내용 · 쿼리 | 소비 서브에이전트 |
|---|---|---|---|
| I-6 | `/internal/seller/{brandId}/sales` | 매출 시계열 · `from`/`to`(필수)·`granularity`(daily/weekly/monthly/summary). `series[{date,sales,orderCount,isAnomaly,deviationPct}]` | sales_anomaly·conversion·general·recommend·chart |
| I-7 | `/internal/seller/{brandId}/funnel` | 구매전환 퍼널 4단(view→cart→checkout→purchase) · `from`/`to` | conversion·behavior·chart |
| I-13 | `/internal/seller/{brandId}/events` | 행동 이벤트 집계(`behavior_events`) · `from`/`to`·`eventType`(product_view/add_to_cart/checkout_start/purchase_complete)·`productId`·`groupBy`(product/eventType/date). `rows[{productId,counts{4종},viewToCartRate,uniqueVisitors}]` — **LLM팀 본문 재작성 반영(v0.15.1)** | behavior·conversion |
| I-16 | `/internal/seller/{brandId}/churn` | 이탈 코호트 · `inactiveDays`. `churnRate`·`preChurnSignals` | churn |
| I-14 | `/internal/seller/{brandId}/order-events` | 주문 상태 전이/조회(`order_status_logs`) · `toStatus`(8종 복수)·`actorType`·`from`/`to`·`stats`·`groupBy`·`limit`(기본 100). **응답(BE 실측 확정 v0.16.2, shape 상호 배제)**: 목록 = `rows[{orderId,fromStatus,toStatus,actorType,reason,buyerMemberId,createdAt}]`+`total` / `stats=true` = `byStatus`+`cancelReasonsTop[{reason,count}]` / `groupBy=memberId` = `rows[{buyerMemberId,orderCount,cancelCount,cancelRatio,maxOrdersPerHour,isSuspicious}]`+`total` | sales_anomaly·conversion·churn·abuse·general |
| I-15 | `/internal/seller/{brandId}/product-changes` | 상품 변경 이력(`product_change_logs`) · `changeType`(PRICE/STOCK/STATUS)·`productId`·`from`/`to`·`limit`(기본 100). **응답(BE 실측 확정 v0.16.2)**: `rows[{productId,productName,changeType,oldValue,newValue,createdAt}]`+`total` — `oldValue`/`newValue`는 문자열(숫자도 문자열, 품절 신호 = STOCK `newValue` "0") | sales_anomaly·churn·recommend |
| I-8 | `/internal/account-events` | 계정/보안 이벤트 집계 **(전역·브랜드 스코프 아님, admin 소유 🔴)** · `eventType`·`from`/`to`·`groupBy` | abuse·churn |

- **`brandId` = JWT 클레임** — AI는 사용자 입력이 아니라 검증 토큰에서 얻어 `{brandId}` path에 쓴다(IDOR 방지, §2.6). 전역 I-8은 brandId 무관.
- **집계/이력값만** 반환(원시 로그 아님). AI는 LLM으로 자연어 답변.
- ⚠️ **혼동 주의**: BE I-15 `product-changes`(판매자 감사 로그)는 C-4 `products/changes`(AI 생성물 배치 pull, §4.8)와 **다르다**. BE I-14 `order-events`(판매자 주문 이벤트)는 C-6 `orders/recent`(구매자 이력, §4.7)와 **다르다**.
- **[v0.16.2] I-14/I-15 응답 스키마는 BE 실측(SellerOrderEventsResponse/SellerProductChangesResponse)으로 확정** — 잔여 🔴 는 전역 I-8 소유(admin)·I-number 정합(§5 C-13, #9). I-6 이상 감지 로직도 BE 실측 확정: 직전 최소 3·최대 7포인트 평균 대비 ±30%, 기준선 0+매출 발생 = 이상(`deviationPct` null), 매출 0 포인트는 이상 아님.

### 4.5 판매자 상품 CRUD API (I-9/I-10/I-11/I-12) — [개정 v0.9.0, BE 문서 채택]

**[개정]** 구 §4.5(I-7 상세 읽기 + FE S-3 PATCH 반영)를 폐기하고, BE 판매자 상품 관리 4종을 채택한다. **AI(`product_agent`)가 Spring internal API로 읽기·쓰기를 직접 수행**하며, 쓰기는 §3.2 HITL 승인 게이트를 거친다. 전부 `internal`·`X-Internal-Token`·`{brandId}`(JWT 클레임)·3s.

| BE No. | Method · 경로 | 용도 | 비고 |
|---|---|---|---|
| I-9 | GET `/internal/seller/{brandId}/products` | 자사 상품 목록 조회 · `status`(ON_SALE/HIDDEN)·`q`·`limit`/`offset` | draft의 `before` 소스(구 I-7 상세 읽기 대체). `rows[{productId,name,price,originalPrice,stockQuantity,status,displayedSalesCount,category,description,imageUrl}]` |
| I-10 | POST `/internal/seller/{brandId}/products` | 상품 등록 · Body `name`·`price`(≤`originalPrice`)·`stockQuantity`(≥0) 필수 | 201 `{productId,status:"ON_SALE"}`. 신규 등록은 변경 이력 미기록 |
| I-11 | PATCH `/internal/seller/{brandId}/products/{productId}` | 상품 수정(가격·설명·상태·재고 통합) · Body 바꿀 필드만 | 재고도 이 API(별도 재고 API 없음). 변경 시 `product_change_logs`(PRICE/STOCK/STATUS) |
| I-12 | DELETE `/internal/seller/{brandId}/products/{productId}` | 상품 삭제(soft) · Body 없음 | **HITL 승인 후에만 실행**. 물리 삭제 없음 — `status=HIDDEN` 전환. 200 `{productId,status:"HIDDEN"}` |

- **쓰기는 `product_agent` 전용** — 쓰기 도구는 이 서브에이전트에만 배정(다른 서브에이전트는 읽기만).
- **소유권 검증은 Spring**: `brandId`(JWT 클레임 유래)로 판매자가 자기 상품만 다루도록 검증. AI는 신원을 요청 본문에서 받지 않는다.
- **status는 `ON_SALE`|`HIDDEN` 2종** (물리 삭제 없음).
- 정확한 응답 스키마·`categoryId`/`attributes` 스키마·HITL 이벤트 계약은 🔴 협의(§5 C-14, #9). "DB 논의 필요"(삭제 PDF)는 BE 내부 사안.

### 4.6 후보 검색 위임 API (I-1 `GET /internal/products/search`, query-time) — [BE 실측 정합 v0.13.0, 착수 전 최우선]

**[v0.5.0 — 가장 중요한 신규 Spring 계약]** 구매자 추천 후보를 **질의 시점에 Spring에 위임 검색**한다. AI는 사용자 원문을 decompose하여 구조화 필터를 만들고, 이 API로 Spring 카탈로그를 검색해 rerank·예산 검증에 필요한 후보를 돌려받는다. **검색 품질이 곧 추천 품질을 좌우**하므로 착수 전 최우선 협의 대상(C-15)이다. AI 카탈로그 사본·벡터 인덱스가 없으므로 **이 API가 유일·영구 후보 확보 경로**다.

#### AI → Spring 요청 (제안)

```
GET {SPRING_BASE_URL}/internal/products/search   ← BE 실측(I-1). 인증 서비스 토큰
X-Internal-Token: {서비스 토큰}
```

> **[확정 v0.15.5] I-1 = GET 그대로 수용**(사용자 지시 2026-07-19). 구 POST 역제안 폐기. **BE Notion I-1 파라미터를 기준으로 채택**(판정규칙: API 표면=Notion, 타입=DDL). Query string 스칼라 파라미터. 신원(userId/guestId)은 서비스 토큰 레인이라 AI가 도출해 쿼리로 전달(§2.6).

```
GET {SPRING_BASE_URL}/internal/products/search?keyword=방수파우치&categoryName=여행용품&maxPrice=50000&brandName=샘소나이트&brandName=레스포삭
X-Internal-Token: {서비스 토큰}
```

| 요청 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `keyword` | string \| null | 아니오 | 상품명+summary+attributes LIKE(BE I-1). FULLTEXT 없음·LIKE 2단(DDL D7) |
| `categoryName` | string \| null | 아니오 | 대분류명이면 하위 소분류 전체 포함, 소분류명이면 해당만(BE I-1·02 D20). LLM은 대분류명이 기본 |
| `minPrice` / `maxPrice` | int \| null | 아니오 | 가격 필터. 질의 시점이라 항상 최신(freshness) |
| `brandName` | string[] \| null | 아니오 | **다중 브랜드**(#100 P1, 방법 D). 반복 파라미터(`brandName=A&brandName=B`)로 전량 전송 → BE `WHERE brand IN (...)` OR 매칭. 단일은 값 1개(기존과 동일), 미전송 시 브랜드 필터 없음. **BE 배열 수용 협의 대상** |
| `color` | string \| null | 아니오 | 색상 조건. BE I-1이 `attributes` LIKE로 필터(#100 P1). decompose가 색상 언급 시 추출·전송 |
| ~~`size`~~ | — | — | **[2026-07-23 개정, BE 합의] 제거됨** — 아래 노트 참조 |

- **[2026-07-23, BE 합의] `size` 제거 — 라운드1 전량 반환**: I-1(라운드1)은 고정필터(category·price·brand) 매칭 상품을 **전량 반환**한다(반환 상한 없음). 결과 수 제한(top-K)은 **AI 쪽**에서 적용한다 — 후보를 받아 pgvector 임베딩 유사도로 재정렬 후 `limit`(config, AI 후보 상한)만큼 압축해 rerank 입력을 만든다(§4.8 방식2). 즉 "몇 개로 줄일지"는 Spring 요청 파라미터가 아니라 AI 파이프라인 소관이다.
- **[해소 v0.15.5, C-15] dedup·평점·정렬 = AI 사후필터(post-filter)**: BE I-1엔 `excludeProductIds`·`ratingMin`·`sort` 파라미터가 **없다**. 따라서 정확 제외 dedup(결정 14-F)은 **응답 수신 후 AI가 최근 구매 productId(I-19) 집합으로 제외**하고, 평점 필터·정렬도 rerank 단계에서 AI가 처리한다. 구 "요청 파라미터 제외" 기본안 폐기.

#### AI가 받는 응답 (BE Notion I-1 기준, 타입=DDL)

```json
{
  "success": true,
  "data": [
    {
      "productId": 1,
      "name": "린넨 셔츠",
      "summary": "시원한 여름 린넨 셔츠",
      "attributes": { "소재": "린넨", "핏": "오버핏" },
      "categoryName": "여성의류",
      "brandName": "더센트",
      "price": 29900,
      "rating": 4.8,
      "reviewCount": 128
    }
  ]
}
```

| 응답 필드 | 타입 | 설명 |
|---|---|---|
| `data[]` | array | 후보 배열(rerank 입력). envelope = `{success, data:[...]}`(BE 실측, ApiResponse<List>) — 파서는 구 `data:{items:[...]}` 도 호환 수용 |
| `[].productId` | number | 후보 식별자(숫자 BIGINT, DDL) |
| `[].name` | string | 상품명(rerank·근거 생성용) |
| `[].summary` | string \| null | 요약(#100 P0 — rerank/세부조건용, 소비는 #101 2차 압축) |
| `[].attributes` | object \| null | **[해소 v0.15.5, C-5] 축 = `category.attribute_schema`(키 배열, 예 `["소재","핏"]`), 값 자유텍스트**(DDL D7·D11) — 2차 압축 속성 매칭 대상 |
| `[].categoryName` / `brandName` | string | rerank 신호·필터 검증용 |
| `[].price` | int | 질의 시점 판매가 — **AI 계산용(비표시, #100 P1)**: 예산 검증(`verifiedSum`, §3.1 budget)·`maxPrice` 판정·rerank 신호. 표시가는 CH-5(§4.3) |
| `[].rating` | number | **조회 시 집계**(저장 avg 없음, DDL D9) — **AI 계산용(비표시, #100 P0)**: 평점 사후필터·rerank 신호. 표시는 CH-5 |
| `[].reviewCount` | int | **조회 시 집계**(리뷰 개수) — **AI 계산용(비표시, #171)**: `rating` 과 짝지어 **"리뷰가 없어 rating=0"(reviewCount=0, 데이터 부재)** 와 **"리뷰가 있고 하한 미달"(reviewCount>0)** 를 가른다. `null`/미전송이면 rating 이 지배(구 동작 폴백). 표시용 리뷰수는 여전히 경로 B(§4.3) |

- **[#100 결정, #171 부분 개정] 표시 전용 필드는 I-1 미반환 → CH-5(§4.3) 하이드레이션**: `imageUrl`·`originalPrice`·`options`는 카드 표시·장바구니 되물음 몫이라 I-1이 반환하지 않는다(BE 2026-07-18 재설계). AI 추천 경로(rerank·예산·평점필터)는 이들을 쓰지 않으며, 표시는 경로 B(§4.3)·옵션 되물음은 I-2(§4.1) 소관이다. **[#171 개정] `reviewCount`는 이 목록에서 제외** — BE가 LLM팀에 함께 제공하기로 합의(2026-07-28)하여 **I-1이 AI 계산용(비표시)으로 반환**한다(위 응답표). rating=0의 의미 판별(리뷰 부재 vs 저평점)에 필요하기 때문이며, 표시용 리뷰수는 여전히 경로 B가 채운다. 구 §4.6 응답표의 `imageUrl`·`originalPrice`·`options` 나열은 폐기.
- **[#100 P0/P1, #171] `price`·`rating`·`reviewCount` = 계산용(비표시) 반환**: display 로 오분류해 CH-5로만 넘기면 안 된다 — rerank·예산검증·평점필터는 CH-5(표시 시점)보다 앞선 질의 시점에 이 값이 필요하다(후보와 함께 받아야 함). `reviewCount`는 `rating` 판별자로 함께 받는다(#171). BE 협의 완료(2026-07-28), 반환 구현 대기.
- **[주의] BE I-1 응답에 `stock`·`totalCount` 없음**: (1) 재고는 후보에 안 실림 → 예산검증은 `price`만, 재고/품절 판정은 담기·주문 시점(§4.1). (2) `totalCount`는 **[#100 P2 결정] 별도 필드 불필요** — `size` 제거(전량 반환)로 AI `search_catalog`가 **top-K 절단 전에** 사후필터 통과 매칭 수를 `total_count`로 확정하므로(절단값 min(매칭, limit)이 아님) 현재 필터의 매칭 수를 안다. 완화 칩 `estCount`(§3.1 suggestions)는 '완화된 다른 필터'의 count 라 이 값으로 못 구하고(완화 칩 미구현·별도 이슈 — 재쿼리/BE count 필요), 되돌리기 칩은 **top-K 절단된 응답 후보 내** 억제 수라 page-local 근사다(전량 기준 진짜 억제 수보다 작을 수 있음).
- **freshness(신선도) — 트레이드오프 없음**: 질의 시점 검색이므로 **가격·재고 필터는 항상 최신**이다. v0.4.0 미러 배치의 "필터 경계 오류(stale price로 인한 오포함/오제외)" 트레이드오프는 **본 구조에서 소멸**한다. (표시가는 여전히 경로 B로 Spring이 목록 GET에서 채운다, §4.3.)
- **rerank는 AI-side**: 이 API는 후보만 반환하고, profile_summary 반영 rerank·근거 생성은 AI 경계에서 수행한다(하드 제약 유지).
- **[방식1 대비 — id 제약 조회 신규 요청, C-17 🔴]** §4.8 결합 **방식1**(AI 벡터검색 → Spring hydrate)에는 벡터가 뽑은 `productId` 집합의 **가용성(재고·활성)·상세를 id로 재조회**하는 변형이 필요하다(원본 컬럼 사본 금지 원칙상 AI가 재고를 저장 못 하므로 Spring 권위 확인 필수). **요청**: I-1에 `productIds`(숫자 배열) 필터 추가 또는 별도 by-id 조회 엔드포인트. **방식2는 불요**(기존 I-1 재사용). BE 협의 대상(C-17) — 오프라인 골든셋 비교는 이 변형에 무의존.

#### [v0.5.0 확정] 채택하지 않는 것 (Not Adopted)

**[v0.5.1 정정] 채택하지 않는 것은 상품 원본 컬럼의 AI측 사본(미러)이다** — 가격·재고·상품명 등 필터/표시 컬럼을 AI DB에 복제하지 않으며, 카탈로그 소유는 Spring/MySQL 단일 원본이다. 반면 **AI 생성물(extras·search_doc·임베딩 벡터)은 AI Postgres에 저장·유지**한다(결정 3 Layer 2/3·결정 6 존속) — 갱신은 §4.8 pull 배치. 질의 시점 후보 확보에서 AI 임베딩과 §4.6 Spring 검색의 결합 방식은 §4.8 말미 OPEN.

### 4.7 구매 이력 조회 API (I-19 `GET /internal/members/{id}/orders`, query-time) — [BE 본문 재작성 v0.15.0]

구 주문 이벤트 미러(§3.6 삭제)를 대체한다. 추천 흐름이 **search 직전**(decompose와 병렬 가능)에 호출해 최근 구매를 확보하고, **결정 14-F 판단은 AI-side**에서 수행한다 — exact `productId` 제외 + 소모품 카테고리 억제 + 되돌리기 제안 칩(suggestions) 생성 → **[v0.15.5] 제외는 §4.6 검색 응답을 받은 뒤 AI 사후필터**(I-1엔 제외 파라미터 없음). 프로필 sleep-time 배치도 동일 API를 구매 소스로 조회한다. **게스트는 호출을 스킵**한다(이력 없음, 결정 8).

#### AI → Spring 요청 (I-19, BE 본문 재작성 07/17)

```
GET {SPRING_BASE_URL}/internal/members/{id}/orders?status={enum}   ← {id}=memberId(AI가 JWT sub 도출)
X-Internal-Token: {서비스 토큰}
```

- `status`(선택, **단일 값만** — 다중 미지원) enum: `ORDERED | SHIPPING | DELIVERED | CONFIRMED | CANCELLED | RETURNED`(아이템 상태, **교환 없음 — 07/17 제거**). 없으면 전체.

#### AI가 받는 응답 (I-19, BE 본문)

```json
{
  "success": true,
  "data": {
    "orders": [
      {
        "orderId": 1023,
        "orderedAt": "2026-07-10T14:23:00",
        "status": "DELIVERED",
        "items": [
          { "orderItemId": 2001, "productId": 552, "productName": "무선 키보드", "optionName": "블랙", "quantity": 1, "price": 29000, "status": "DELIVERED", "categoryName": "키보드" }
        ],
        "itemsTotal": 29000,
        "shippingFee": 0,
        "totalAmount": 29000
      }
    ]
  }
}
```

- **id는 전부 숫자(BIGINT), 필드 camelCase** — 다른 internal API(I-2·I-18)와 동일 규칙(§2.6).
- **`shippingFee`는 항상 0**(DDL D36 배송비 항 자체 없음) — `totalAmount` = 상품 스냅샷 합. **[통보 대상] BE Notion I-19 페이지는 `shipping_fee: 3000`으로 stale** — 타입/데이터는 DDL 기준(배송비 없음)이라 0.
- **[정정 v0.15.5] `status` = 주문 상태 enum 6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, BE Notion I-19). 구 `representativeStatus` 8종은 **O-3 `GET /api/orders`(FE 대면, 대표 상태)** 것을 잘못 갖다 쓴 것 — I-19와 별개라 폐기. 표시 문구는 FE 매핑.
- **[통보 대상] BE Notion I-19 페이지가 stale**: snake_case(`order_id`·`unit_price`)·문자열 id(`"P552"`)로 표기됨 — 타입/케이스는 **DDL·프로젝트 규약 기준**(숫자 BIGINT·camelCase)이 우선(판정규칙). BE에 페이지 갱신 통보.
- **✅ [dedup 갭 해소 — BE 확정 2026-07-19] items에 `categoryName`(string) 포함** — 결정 14-F의 소모품 **카테고리 억제**·되돌리기 `suggestions.revert.category` 칩(§3.1)의 소스 확보. BE가 I-19 items[]에 `categoryName`을 추가(I-1과 동일 필드). 소모품 판정은 AI-side(MVP config, 정본 catalog 속성사전 SPEC-CATALOG-DATA-001). exact `productId` 제외 + 카테고리 억제 모두 구현 완료.
- **지연 가드**: §4.6 검색과 병렬 호출 가능. 실패/타임아웃 시 **dedup 없이 추천 진행**(degrade).
- 실패: `400 ORDER_INVALID_PARAM`(status enum 위반) / `401 INTERNAL_TOKEN_INVALID` / `404 MEMBER_NOT_FOUND`.
- 경로·파라미터 수용은 🔴 협의(§5 C-6) — 07/17 BE 확인질문("이대로 가도 되는지").

### 4.8 AI 생성물 갱신 배치 (bulk export pull) — 🟡 골격 BE 확정(2026-07-18)·잔여 3건 저영향 [v0.5.1 신설, v0.15.18 갱신]

AI Postgres의 **AI 생성물(extras·search_doc·임베딩 벡터, `productId` 키)** 을 상품 변경에 맞춰 갱신하는 배치. **AI가 요청하는 pull 방식**으로 확정 — Spring 주기 push는 기각(스케줄러·재시도·버퍼링 부담이 Spring으로 넘어가고, 유실 시 결국 pull 보정이 또 필요).

```
GET {SPRING_BASE_URL}/internal/products/changes?since={cursor}&limit={n}   ← BE 실측(I-17)
X-Internal-Token: {SERVICE_TOKEN}   ← BE 확정(2026-07-18): 다른 internal API와 동일 관례(§1.2 (c)), Bearer 아님
```

#### AI가 받는 응답 (BE 확정 2026-07-18)

> 실제 응답은 다른 internal API와 동일하게 **공통 envelope `{"success": true, "data": {…}}`** 로 감싸진다(BE 2026-07-18 정정). 아래는 `data` 본문. `productId`는 **숫자 BIGINT**(I-19 규칙 — BE가 구 문자열 `"P-10293"` 예시를 숫자로 정정).

```json
{
  "items": [
    { "productId": 10293, "status": "ON_SALE", "updatedAt": "2026-07-15T10:00:00Z", "name": "여행용 방수 파우치", "description": "…", "category": "여행용품/보안용품", "brand": "트래블메이트", "attributes": { "방수": true, "용량": "2L" } },
    { "productId": 10877, "status": "HIDDEN", "updatedAt": "2026-07-15T10:01:00Z" }
  ],
  "nextCursor": "opaque-cursor-123",
  "hasMore": true
}
```

| 필드 | 설명 |
|---|---|
| `items[].status` | `ON_SALE` \| **`HIDDEN`** — Spring `ProductStatus` 값을 별도 매핑 없이 그대로 반환. 두 값 외에는 응답 계약 위반. `HIDDEN` 누락 시 AI 생성물(임베딩)이 유령 상품을 계속 추천 후보로 유지 |
| `items[]` 콘텐츠 필드 | enrichment·search_doc 조립 입력(name/description/category/**brand**/attributes). **AI는 이 값을 저장하지 않고 산출물 생성에만 사용** |
| `nextCursor` | 다음 페이지 시작점(불투명 커서). AI가 저장했다가 다음 주기의 `since`로 사용 |
| `hasMore` | `true`면 같은 주기 안에서 `nextCursor`로 즉시 재요청(따라잡기), `false`면 이번 주기 종료 |

**오류(BE 확정 2026-07-18)**: 400 `INVALID_CURSOR`(커서 형식 오류/만료 → AI는 `since="0"` 전체 재구축 폴백) · 401 `INTERNAL_TOKEN_INVALID`(서비스 토큰 없음/무효) · 403 `FORBIDDEN`(내부 API 권한 없음).

- **흐름**: AI 배치 잡이 주기적으로 변경분 조회(커서 기반, `hasMore` 루프로 페이지 소진) → `HIDDEN`은 AI 생성물 삭제/비활성 → `ON_SALE`은 enrichment(Haiku, Layer 2 속성·상황 태그 추출) → `search_doc` 조립 → 임베딩(**Google `gemini-embedding-001` API**, 결정 6 개정 2026-07-20 — 셀프호스트 torch 폐기) → AI Postgres upsert. **상품 원본 컬럼은 저장하지 않는다** — 산출물만 저장.
- **계약 위반(fail-closed)**: `items[].status`가 `ON_SALE`/`HIDDEN` 외 값이면 해당 항목만 건너뛰지 않고 페이지 전체를 실패 처리한다. artifact와 커서를 전진시키지 않으며, Spring이 원천 데이터를 수정한 뒤 같은 `since`부터 다시 처리한다. I-17에는 항목별 ack/DLQ 계약이 없어 skip 후 커서를 전진시키면 특히 `HIDDEN` 삭제 이벤트가 영구 유실될 수 있다.
- **복구·초기 구축**: 일시 실패 시 다음 주기에 동일 커서부터 재개(자연 회복). 계약 위반은 Spring 원천 수정 후 같은 커서부터 재처리한다. 초기 전체 구축도 커서 0부터 같은 API로 처리.
- **hk-final 매핑**: `app/pipelines/enrichment.py`·`embedding.py` 스텁을 활성화. 임베딩은 **Google `gemini-embedding-001` API 호출**(셀프호스트 torch·sentence-transformers·`--group embedding` 폐기) — 쿼리 시점 임베딩도 동일 API. `embedding_dim` = **1536**(gemini-embedding-001 MRL 절단, 1024→1536; pgvector 표준 hnsw/ivfflat ≤2000 적합). ⚠️ MRL 1536은 **수동 L2 정규화** 필요(3072만 사전 정규화). ⚠️ search_doc·쿼리 텍스트가 Google로 전송됨(외부 의존·API 비용·데이터 전송 신규) — 단 "AI Postgres엔 생성물만, 상품 원본 사본 금지" 원칙과는 무관(임베딩 벡터만 저장).
- **[BE 확정 2026-07-18 / 잔여 3건 저영향]** BE가 골격 확정(인증 `X-Internal-Token`·envelope `{success,data}`·숫자 `productId`·오류코드·`since="0"` 초기구축·`hasMore` 루프). BE "I-17 미정 3개"(07/17 확인질문 Part 2 중 **유일하게 미해소인 항목**)는 **① 커서 값 형식**(BE 제안 "수정시각+id" — AI는 불투명 취급이라 무영향) **② `attributes` JSON 스키마**(AI는 카테고리별 자유 dict로 방어 파싱) **③ 리뷰 텍스트 포함 여부**(MVP 제외, search_doc 리뷰 결합은 고도화) — **세 항목 모두 저영향**(opaque·방어 파싱·MVP 제외)이라 AI 소비 구현을 막지 않는다. 페이지 크기(`limit` 기본 500)·주기는 AI config. **🔴 선결(스키마 아님): 이 배치(enrichment·임베딩)의 MVP/post-MVP 스코프** — config.py(post-MVP) vs 파이프라인 주석(MVP) 모순 미해소.

> **[결정 2026-07-20 — 두 방식 모두 구현·골든셋 확정]** #7(§4.8 배치)이 **MVP로 편입**되어 AI 임베딩이 검색에 실사용된다. §4.6 결합 방식은 **방식1·방식2를 둘 다 `SearchBackend`로 구현**해 골든셋/실측으로 확정한다(2026-07-15 계획 유지): **방식 1** AI 벡터 검색으로 상위 N개 `productId` 확보 → Spring에 **id 제약 조회**로 가격·재고 가용성 필터+상세(§4.6에 id 필터 변형 신규 필요 = **C-17 🔴**) / **방식 2** Spring 검색(I-1)이 후보 확보 → AI 임베딩은 시맨틱 재정렬 보조(**BE 계약 변경 없음, 라이브 구현 가능**). **[착수 방침 2026-07-20]** 방식2 라이브 + 방식1 오프라인 랭킹(가용성 필터 스텁)으로 골든셋 비교를 먼저 하고, 방식1 라이브 가용성 조회는 **C-17 확정 후 승격**한다(BE 무대기 착수). config.py의 "enrichment·임베딩 = post-MVP" 주석은 이 결정으로 **정정 대상**(→ MVP).
>
> **[✅ 해소 #101 — 방식2 채택, MVP hot path 확정]** 위 OPEN을 #101이 해소한다. **방식2(`EmbeddingRerankBackend`)를 MVP hot path 기본으로 채택**한다 — config `search_backend = "embedding_rerank"`(기본값). 흐름: Spring I-1 전량 반환 → AI가 `semanticQuery` 임베딩으로 pgvector 코사인 재정렬 → 최근구매 dedup 이후 `embedding_rerank_limit`(기본 30)로 압축 → Sonnet rerank. 임베딩/pgvector 장애는 `SEARCH_FAILED` 아니라 **Spring 순서 degrade**, Spring I-1 자체 실패만 `SEARCH_FAILED`(#7). **방식1(`VectorSearchBackend`)은 C-17(id 제약 조회) 미착수라 hydrate 미주입 시 미착수 신호**만 내고 hot path 미편입(골든셋 오프라인 비교용으로만 존치, 승격은 C-17 확정 후). **`[].attributes` "2차 압축 속성 매칭 대상"(§4.6)도 #101(PR②)이 구현** — 사용자 명시 속성조건을 `SpringProduct.attributes`와 관대 하드 매칭(문자열 부분·숫자 완전일치, 축 부재 보존). 이로써 본 문서 곳곳의 "결합 방식 = OPEN(§4.8 말미)" 표기(§0 v0.5.1·§4.6 말미 등)는 **방식2 채택으로 해소**된다.

### 4.9 장바구니 조회 API (I-18 `GET /internal/cart`) — [BE 실측 정합 v0.13.0]

담기(I-2, §4.1)의 짝이 되는 **조회 계약**. 두 용도로 사용한다(2026-07-15 사용자 확정):

1. **장바구니 질의 응답** — "장바구니에 뭐 있어?" 발화 시 조회 후 `token` 텍스트로 답변한다(별도 SSE 이벤트 없음, §3.1).
2. **담기 시 기존 보유 안내** — 담기 전 동일 상품·옵션 보유를 확인해 "이미 담겨 있어 N개로 늘렸어요"류 안내를 생성한다. **수량 합산의 실행 권위는 Spring**(I-2가 합산 처리) — 조회는 안내용이며, **조회 실패 시에도 담기는 진행**한다(degrade).

#### AI → Spring 요청 (제안)

```
GET {SPRING_BASE_URL}/internal/cart?userId={id}   또는 ?guestId={id}
X-Internal-Token: {서비스 토큰}   ← I-2와 동일 인증 레인
```

#### AI가 받는 응답 (제안)

```json
{
  "success": true,
  "data": {
    "items": [
      { "cartItemId": 55, "productId": 1, "productName": "여행용 방수 파우치", "optionId": 3, "optionName": "블루", "quantity": 2, "price": 12900 }
    ]
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `items[].cartItemId` | number | 장바구니 항목 식별자(I-2 응답과 동일 체계) |
| `items[].productId` | number | 상품 식별자(숫자 BIGINT, §2.6) |
| `items[].productName` / `optionName` | string | **필수 포함**(BE I-18 확정 2026-07-18) — 챗 답변 문장 생성에 필수(id만으로는 자연어 답변 불가) |
| `items[].optionId` | string \| null | 옵션 |
| `items[].quantity` | int | 현재 수량 |
| `items[].price` | number \| 없음 | 표시가(선택 — 총액 안내용, Spring 표시 권위 유지) |

- **빈 장바구니는 `items: []` 정상 200**(오류 아님). 실패 코드(BE I-18 확정): `400 CART_QUERY_INVALID`(userId/guestId 둘 다 없거나 둘 다 존재) / `401 INTERNAL_TOKEN_INVALID`(I-2와 동일).

> **[해소 C-16 — BE I-18 확정 2026-07-18]**: 경로 `GET /internal/cart`·쿼리(userId/guestId)·`X-Internal-Token` 인증·응답 필드(`productName`/`optionName` **필수 포함**)·`CART_QUERY_INVALID`(400) 모두 BE "챗봇 장바구니 조회" 문서로 확정. 페이징은 MVP 전량 반환.

### 4.10 주문 상태 요약 API (I-4)

구매자 챗의 `order_status` intent가 최근 주문 진행 상태를 조회하는 query-time 계약이다. I-19
구매 이력(§4.7)은 추천 dedup/프로필 구매 소스이고, I-4는 사용자에게 표시할 상태 요약이므로
endpoint·모델·실패 의미를 공유하지 않는다. 주문 사실은 두 번째 LLM 호출 없이 결정적으로
plain text로 렌더링한다.

#### AI → Spring 요청

```http
GET {SPRING_BASE_URL}/internal/members/{userId}/orders/status?recent=3
X-Internal-Token: {서비스 토큰}
```

- `recent`는 런타임 설정이 아닌 고정 계약값 `3`이다.
- 공통 Spring client의 **3초 timeout**과 `X-Internal-Token` 주입 경로를 사용한다.
- `{userId}`는 검증된 회원 스트림 티켓의 JWT `sub`에서 도출한 양의 Java `Long`
  (`1..9_223_372_036_854_775_807`)만 허용한다. 메시지·request body·LLM 출력의 숫자,
  `identity.subject` fallback은 사용하지 않는다.
- guest·seller·missing/invalid member identity는 Spring을 호출하지 않고 로그인/재인증 안내로
  정상 스트림 종료한다. 이 선차단은 I-4 path를 이용한 IDOR와 회원 존재 여부 탐색을 막는다.

#### AI가 받는 성공 응답

```json
{
  "success": true,
  "data": {
    "orders": [
      {
        "orderId": 1023,
        "orderedAt": "2026-07-30T09:15:00+09:00",
        "representativeStatus": "배송중",
        "items": [
          {
            "productName": "무선 키보드",
            "status": "SHIPPING",
            "statusText": "배송중"
          }
        ]
      }
    ]
  }
}
```

정상 envelope는 top-level object, **literal boolean `success is true`**, 존재하는 object `data`,
존재하는 array `data.orders`를 모두 만족해야 한다. `success` 누락/`false`/`null`/`1`/`"true"`,
`data` 또는 `orders` 누락·`null`·타입 불일치는 malformed response다. `orders or []`처럼
계약 위반을 빈 결과로 축소하지 않는다.

| 필드 | 엄격 계약 |
|---|---|
| `orders` | 필수 array, 요청 `recent=3`에 맞춰 **0~3건**. 4건 이상은 전체 malformed |
| `orders[].orderId` | coercion 없는 정수 BIGINT `1..9_223_372_036_854_775_807`; bool/string/float/null/0/음수/overflow 금지 |
| `orders[].orderedAt` | **timezone-aware datetime 필수**. naive/invalid 값이 한 건이라도 있으면 전체 malformed이며 부분 표시하지 않음 |
| `orders[].representativeStatus` | `결제 대기` / `결제 실패` / `주문 완료` / `배송중` / `배송 완료` / `구매 확정` / `취소/반품 진행중` / `처리 완료` 중 정확히 하나 |
| `orders[].items` | 필수 array. 수신 항목 전부를 검증하고, 표시 단계에서만 처음 3개로 제한 |
| `items[].productName` | strict string, 최대 200자. 유효 길이를 자르지 않으며 출력 전에 control/CR/LF/bidi/zero-width를 공백 정제 |
| `items[].status` | `PENDING` / `ORDERED` / `SHIPPING` / `DELIVERED` / `CONFIRMED` / `CANCEL_REQUESTED` / `CANCELLED` / `RETURN_REQUESTED` / `RETURNED` 중 정확히 하나 |
| `items[].statusText` | `결제 대기` / `주문 완료` / `배송중` / `배송 완료` / `구매 확정` / `취소 접수` / `취소 완료` / `반품 접수` / `반품 완료` 중 정확히 하나 |

`status`와 `statusText`는 각각 허용 어휘에 속하는 것만으로 부족하며 아래 canonical pair가
**정확히 일치**해야 한다. 서로 다른 pair의 유효 값을 섞거나 status/statusText에 제어문자가
있으면 전체 payload를 거부한다.

| `status` | `statusText` |
|---|---|
| `PENDING` | `결제 대기` |
| `ORDERED` | `주문 완료` |
| `SHIPPING` | `배송중` |
| `DELIVERED` | `배송 완료` |
| `CONFIRMED` | `구매 확정` |
| `CANCEL_REQUESTED` | `취소 접수` |
| `CANCELLED` | `취소 완료` |
| `RETURN_REQUESTED` | `반품 접수` |
| `RETURNED` | `반품 완료` |

#### 결정적 출력과 실패 의미

- Spring의 newest-first 주문 순서를 보존하고, aware `orderedAt`을 Asia/Seoul 기준 `M월 D일`로
  표시한다. 최대 **3개 주문 × 주문당 3개 상품**만 표시한다.
- 상품은 `productName — statusText` 형식이다. 한 주문에 상품이 4개 이상이면 앞의 3개 뒤에
  정확한 잔여 개수 `외 N개`를 붙인다. 수신한 나머지 항목도 모두 schema 검증한다.
- 정확한 `data.orders: []`만 정상 empty다. 이때 고정 문구 **`최근 주문 내역이 없어요.`** 를
  보낸다.
- guest/seller/invalid identity, HTTP 404/5xx, network/timeout, invalid JSON/envelope/schema,
  naive timestamp는 모두 사용자별 고정 안내 `token` **1개** 뒤 `done`
  `{"finishReason":"stop"}` **1개**로 끝난다. recoverable dependency degradation에는 SSE
  `error`를 emit하지 않으며, 404와 5xx 문구를 같게 해 회원 존재 여부 oracle을 만들지 않는다.

#### 관측·저장 경계

- 모든 분기는 첫 SSE frame 전에 privacy-safe JSON 로그를 정확히 1건 남긴다. 고정 key set은
  `event,requestId,outcome,errorCategory,orderCount,elapsedMs`다.
- `event=order_status_route`; `outcome`은
  `success|empty|identity_blocked|upstream_degraded`; `errorCategory`는
  `none|guest|seller|missing_user_id|invalid_user_id|upstream_unavailable|malformed_response`만
  허용한다. `requestId`는 바깥 `chat_request` 로그와 같은 correlation ID이고 count/time은
  숫자다.
- 로그에는 member/order/product ID, 상품명, 상태 문구, raw utterance/response, exception 문자열,
  URL/path, internal token을 기록하지 않는다.
- 방출한 assistant `token`은 일반 구매자 대화와 동일하게 §6.3의 대화 보존·삭제 정책을 적용받아
  주문번호·상품명·날짜·상태가 대화 이력에 저장될 수 있다. 반면 I-4 response-derived
  order/date/product/status 필드를 profile memory, recommendation filter, cart/pending state,
  별도 application cache에 추출·복제하지 않는다. 기존 사용자 입력 기반 profile/session 처리와
  non-cart 전환 시 stale pending-cart 정리는 그대로 유지한다.

### 4.11 홈 추천 목록 조회 (P-5 `GET /api/products/recommended`, FE ↔ Spring 전제 계약) — [v0.18.0 신설]

메인 화면 "OO님을 위한 추천" 영역. **이 계약은 FE ↔ Spring 간이며 AI 서버는 관여하지 않는다**(레인 d) — §4.3의 CH-5와 같은 위치다. 다만 **Spring이 내부적으로 I-22(§3.7)를 호출**해 랭킹을 얻으므로, I-22의 `outcome`·상관키가 여기로 어떻게 흘러나가는지를 알아야 §3.7 계약을 정확히 구현할 수 있어 등재한다.

```
GET {SPRING_BASE_URL}/api/products/recommended
Authorization: Bearer {AT}   ← 로그인 필요. 파라미터 없음(사용자는 AT에서 식별)
```

#### 성공 응답 — 200

카드 필드는 P-4(인기 상품)와 동일하고, 여기에 추천 상관키 3종(`source`·`recommendationRequestId`·`listId`)과 카드별 `reason`이 붙는다.

```json
{
  "success": true,
  "data": {
    "source": "AI_RECOMMENDED",
    "recommendationRequestId": "a63be350-ec96-4f44-b3f9-c962b6673a68",
    "listId": "7c1e9f2a4b8d43f5a0c6d1e97b3f8a24",
    "items": [
      {
        "productId": 1, "name": "린넨 셔츠", "brandName": "더센트",
        "price": 29900, "originalPrice": 39000, "imageUrl": "https://.../1.jpg",
        "rating": 4.8, "reviewCount": 2847,
        "reason": "최근 선호한 가격대와 카테고리에 맞아요"
      }
    ]
  }
}
```

| 필드 | 값 | FE 사용 |
|---|---|---|
| `source` | `AI_RECOMMENDED` \| `POPULAR_FALLBACK` | **표시 분기의 축** — `POPULAR_FALLBACK`이면 "OO님을 위한 추천" 제목과 `reason`을 **띄우지 않는다** |
| `recommendationRequestId` | 추천 실행 1회를 가리키는 id | 노출·클릭 이벤트(E-1)에 실어 보낸다 |
| `listId` | 전달된 목록 id(≥128bit 무작위) | 위와 동일 |
| `reason` | 카드별 추천 이유. 없으면 `null` | `source=AI_RECOMMENDED`일 때만 표시 |

- `items` — **배열 순서가 곧 순위다.** FE는 재정렬하지 않으며 `position`을 싣지 않는다(I-22와 동일).
- **표시 권위 = Spring**: `name`·`price`·`imageUrl`·`rating`·`reviewCount`를 Spring이 채운다. AI는 I-22로 **id와 `reason`만** 넘긴다(경로 B 일관, 결정 9-B).
- **와이어에 싣지 않는 것** — `fallbackReason`(`PROFILE_MISSING`·`INSUFFICIENT_CANDIDATES`·`AI_ERROR`·`AI_TIMEOUT`) · `cacheStatus` · `algorithmVersion` · `modelVersion`. FE 동작이 달라지지 않는 값이다. `fallbackReason`은 **`recommendation_generated` 이벤트에 저장**해 장애 관측에 쓴다 — 신규 회원이라 대체된 건지 AI가 죽어서 대체된 건지는 서버만 알면 된다.

#### Fallback 규약 — AI 구현이 지켜야 할 지점

다음 경우 **Spring이** P-4(인기 상품) 결과로 대체해 **항상 200 + items**로 응답하며 `source="POPULAR_FALLBACK"`이 된다:

- I-22가 `outcome=NO_PROFILE` — 프로필 없음(신규 회원) 또는 시그널이 비어 개인화 근거 없음
- I-22가 `outcome=INSUFFICIENT_CANDIDATES` — 후보 부족으로 랭킹이 무의미
- I-22 실패·타임아웃(연결 2s / 응답 3s)

> **fallback에도 `recommendationRequestId`·`listId`가 실리며 이때는 Spring이 발급한다.** 상관키가 없으면 FE가 인기상품 카드에 대해 쏘는 노출·클릭 이벤트가 **부모 없는 고아**가 되어 E-1 server-side 검증에서 버려진다. → **AI는 `outcome`만 정확히 내면 되고, fallback 목록·상관키를 대신 만들지 않는다.**

**FastAPI 실패는 P-5의 실패 응답이 아니다** — 타임아웃·에러·프로필 없음은 전부 위 규약으로 200이 된다. P-5가 5xx를 내는 건 Spring 자체 장애뿐이라 FE는 "추천 실패" 화면을 따로 만들지 않는다. P-5 자체의 실패 응답은 인증뿐이다: `401 AUTH_REQUIRED`(게스트는 이 API를 쓰지 않고 P-4를 직접 호출) · `401 AUTH_TOKEN_EXPIRED`(A-4 재발급 후 1회 재시도) · `403 AUTH_FORBIDDEN`(`SELLER`·`ADMIN` 계정 — 이 경로는 `USER` 전용).

#### 캐시 키 규칙 — [확정 2026-07-30]

- 개인화 결과의 캐시 키는 **회원 id + 카탈로그 버전 + 알고리즘 버전**이며 **타 회원 재사용 금지**(키가 새면 남의 추천이 보인다). fallback(P-4) 결과는 공용 캐시를 써도 되나 `recommendationRequestId`·`listId`는 요청마다 새로 발급해 귀속이 뭉개지지 않게 한다.
- **TTL** — 개인화 결과 캐시는 **10분** 후 재계산. 단 **`listId`의 이벤트 귀속 유효기간은 캐시 TTL과 별개로 24시간**이다 — 목록이 캐시에서 사라져도 `recommendation_generated`가 DB에 남아 귀속이 가능하다.
- **채팅 CH-5의 10분과 성격이 다르다** — CH-5는 *조회* 만료(Redis에서 목록이 사라지면 404)이고, 홈은 **조회 API가 없고** 카드가 P-5 응답에 바로 실려 오므로 **조회 만료라는 개념 자체가 없다**. 홈은 탭을 열어둔 채 한참 뒤 클릭하는 경우가 흔해 귀속 기간을 길게 잡는다.

> ※ §3.7 I-22 정본 페이지에는 아직 *"홈 목록의 TTL·캐시 정책은 … BE 결정 필요"* 로 남아 있으나, **P-5 정본이 2026-07-30에 10분/24시간으로 확정**했다. 본 사본은 확정본을 따르며 정본 I-22 페이지의 stale 문구는 BE 통보 대상이다.

---

## 5. 협의 필요 항목 요약표 (🔴 Consolidated Open Items)

Spring/FE 팀과 확정이 필요한 항목을 통합한다. 각 항목은 본 문서에서 **제안(초안)** 또는 **확정안 반영(수용 전 🔴)** 으로 제시된다.

**[v0.6.0] 착수 전 필수(최우선)**: **C-15(`POST /products/search` 후보 검색 — 최우선, 유일 후보 경로)** · C-6(구매 이력 조회) · C-3(장바구니 담기 I-2 잔여) + C-16(장바구니 조회) · C-13(I-6 집계) · C-1 잔여(role 값·TTL).

| # | 항목 | 현재 상태 | 소유/근거 | 상태 |
|---|---|---|---|---|
| C-1 | **인증(auth)** | **확정**: RS256 + JWKS(Spring JWKS 노출, AI 로컬 검증·kid miss refetch), `401` 통일, `/seller/chat` role=seller(403)(§2.3). **[v0.10.0] SSE = 스트림 단명 티켓**(로그인 AT 아님, CH-1 발급) — 클레임 `sub`+`sub_type`(member/guest)+`iss`+`aud`+`scope`+`exp`, 검증 signature/exp/iss/aud/scope. **[v0.8.0·확정] `brandId` = 판매자 티켓 클레임**(body 금지, `{brandId}` path용 — userId와 동일 IDOR 원칙, §2.6). **[v0.15.20·BE 코드 실측 확정]** `iss`=`jarvis-spring-auth` · `aud`=`jarvis-fastapi-ai` · `scope`=`chat:stream` · 티켓 TTL **60초** · 판매자 `role="seller"`(**소문자**)+`brandId`(숫자, 판매자 티켓에만) · **CH-1b `POST /api/chat/tickets` 구현 완료**(요청 `{sessionId}`, 세션 소유자 검증, 세션 TTL 동시 갱신) · CH-6 `POST /api/chat/seller/sessions` 신설 | 결정 19 / BE 실측(2026-07-27) | ✅ 해소 — 🔴 잔여는 **서비스 토큰(`X-Internal-Token`) 회전 주체·만료 정책·mTLS 병용 여부**(운영 정책, 현재 단일 공유 시크릿) |
| C-2 | **스트림 전 오류 봉투** | **확정안 반영**: `error.{code,message,requestId}` + 상태 매핑(`400`/`401`/`403`/`429`)(§2.5) | 본 문서 제안 | 🔴 Spring 수용 전 |
| C-3 | **[v0.6.0 재작성] 장바구니 담기 API(I-2)** | **BE 문서 채택**: `POST /internal/cart/items` 단건 + `X-Internal-Token` + 본문 신원(JWT `sub` 유래) + `optionId` + quantity 1~99 합산 + **게스트 담기 허용**. 옵션 되물음 멀티턴(`CART_OPTION_REQUIRED` options 목록)(§4.1) | BE I-2 문서 / 결정 7 / 결정 8 개정(§8 항목 7) | 🟢 **[재개정 v0.15.16] 담기 재고검증 있음** — BE `CART_STOCK_INSUFFICIENT`+`availableStock`(2026-07-22) → `STOCK_INSUFFICIENT`(`OUT_OF_STOCK` 폐기 유지). 🟢 **[해소 v0.15.8] options 스키마**(BE 2026-07-18) — `error.detail.options:[{optionId,name,extraPrice}]`. 🔴 잔여 — 서비스 토큰 발급만 |
| C-4 | **[BE 확정 2026-07-18] AI 생성물 갱신 배치 = I-17** | `GET /internal/products/changes`(§4.8, BE "상품 정보 Batch") — 골격 확정: `X-Internal-Token`·envelope `{success,data}`·숫자 `productId`·오류(INVALID_CURSOR/INTERNAL_TOKEN_INVALID/FORBIDDEN)·`since="0"` 초기구축·`hasMore` 루프. 상품 원본 사본 없음 | I-17 / BE Notion | 🟡 잔여 3건 저영향(커서 형식=opaque·`attributes` 스키마·리뷰 포함). 주기=AI config·페이지=`limit`(기본 500). **스코프(MVP?)는 스키마 아님 — 별건** |
| C-5 | **[해소 v0.15.5] `productId` 타입 & `attributes`** | 원본 id = 숫자(BIGINT). **[해소] `attributes` 구조 확정** — DDL `product.attributes` JSON, **축 = `category.attribute_schema`(키 배열), 값 자유텍스트**(D7·D11). 2차 압축 속성 매칭 대상 | 결정 9-B / DDL D7·D11 | ✅ **해소**(타입·attributes 구조 모두 확정) |
| C-6 | **[정정 v0.15.5] 구매 이력 = I-19** | `GET /internal/members/{id}/orders`(§4.7). camelCase·숫자 id(DDL)·`shippingFee` 0. **`status` = 6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, Notion I-19). **`categoryName` 포함**(BE 확정 2026-07-19 — 카테고리 억제·productId dedup 모두 가능) | I-19 / Notion·DDL | 🟢 확정(status·타입·**categoryName BE 확정 2026-07-19**). 🔴 잔여 — Notion 페이지 stale BE 통보 |
| C-7 | **판매자 판매 데이터 소스** | **[해소]** 원천 = **Spring 집계 API(I-6) 질의 시점 콜백**(§3.2·§4.4). 구 기본안(주문 미러 sellerId·금액 확장) 폐기 | 결정 20 개정/Batch 1 | ✅ **해소** — 계약 세부는 C-13으로 이관 |
| C-8 | **[해소 v0.15.19] 세션 종료 통지 = I-20** | `POST /events/session-end` `{sessionId,userId(number BIGINT),reason?}` + `X-Internal-Token`. UUID 포함 불투명 sessionId, reason 최대 64자, 파생 멱등키, 202 `accepted`/`duplicate`(§3.5) | 이슈 #62/#79 / Spring PR #24 | 🟢 계약 확정 — **[v0.16.0]** Spring 알려진 reason=`logout` **1종**(`newConversation` 제거 — 새 대화는 threadId만 갱신, §2.6), AI 내부 10분 비활동 종료, enum 미강제 |
| C-9 | **[BE 신설 07/17] 추천 push = I-21** | `POST /internal/recommendations` `{sessionId, listId, productIds[Top5 숫자], reasons[{productId,reason}]}`(§4.2). **listId=FastAPI 생성(UUID급 무작위 ≥128bit)**, **reason=콜백 포함(v0.15.15 확정, BE 구현 07-18)**, 콜백 성공 후 products.ready. 구 groups 구조·추측 가능한 listId 폐기 | I-21 / BE DB | 🟢 reason·listId 형식 확정. 🔴 잔여: listId TTL |
| C-10 | **식별자 = 토큰 클레임** | **확정(숫자 사용자 id)**: 사용자/게스트/판매자 = 숫자 id, JWT `sub`에 문자열화. `role` enum 구분(§2.6). **양팀 통보 필요** | 결정 8/19 / 2026-07-14 세션 확정 | 🔴 미확정 — 클레임 키·id 타입 세부 |
| C-11 | **[v0.7.0 축소] CORS 허용 오리진** | 레이트 리밋은 **확정**(FastAPI 미들웨어 + in-memory, 분당 10/시간당 100 config, §2.8) — 협의 잔여는 **FE 허용 오리진 목록**뿐 | 결정 19 / v0.7.0 확정 | 🔴 잔여 — 허용 오리진(FE 통보) |
| C-12 | **[BE 신설 07/17] 카드 조회 = CH-5** | `GET /api/chat/lists/{listId}`(§4.3, 구 P-7 대체) — Spring이 표시 필드 enrich·서빙, FE↔Spring. AI 미관여 | CH-5 / BE DB | 🔴 카드 응답 스키마(FE↔Spring 소유, LLM 사안 아님) |
| C-13 | **[재정의 v0.8.0] 판매자 집계 API 5종** | BE 문서 채택 — `GET /internal/seller/{brandId}/{sales\|funnel\|events\|churn}` + 전역 `/internal/account-events`. `X-Internal-Token`, `brandId`=JWT 클레임(§2.6). 구 단일 `/seller/aggregates` 제안 폐기. 통계 답변 원천(§4.4) | BE 문서 / #9 | 🔴 **최우선** — 응답 스키마(**I-13은 LLM팀 재작성 반영 완료**, 나머지 4종 잔여)·전역 I-8 admin 소유·I-number 정합 |
| C-14 | **[재정의 v0.9.0] 판매자 상품 CRUD API 4종** | BE 문서 채택 — I-9 목록/I-10 등록/I-11 수정/I-12 삭제(soft,HITL). `internal`·`X-Internal-Token`·`{brandId}`. **AI 직접 쓰기**(구 "I-7 읽기 + FE S-3 PATCH" 폐기), 쓰기는 HITL 승인(§3.2·§4.5) | BE 문서 / #9 | 🔴 미확정 — 응답 스키마·attributes 스키마·HITL 이벤트 계약 |
| C-15 | **후보 검색 위임 = I-1** | **[확정 v0.15.5] GET 그대로 수용**(사용자 지시). BE Notion 파라미터 채택(`keyword·categoryName·minPrice·maxPrice·brandName·size≤30`). **dedup·평점·정렬은 AI 사후필터**(BE I-1에 해당 파라미터 없음). 응답 = `{success,data:{items[...]}}`(§4.6) | I-1 / BE Notion·DDL | 🟢 **확정**(GET·파라미터·응답). 🔴 잔여 — `totalCount` 미제공 → estCount 완화칩 소스 |
| C-16 | **장바구니 조회 = I-18** | BE 실측 `GET /internal/cart`(§4.9, "챗봇 장바구니 조회"), 서비스 토큰. 질의 응답 + 담기 시 기존 보유·합산 안내. `productName`/`optionName` 포함 필요(챗 답변 생성용) | I-18 / BE DB | 🟢 **[해소 2026-07-18] `productName`/`optionName` 필수 포함 · `CART_QUERY_INVALID`(400) BE I-18 확정** |
| C-17 | **[신규 2026-07-20] 방식1용 id 제약 조회** | §4.8 결합 방식1(AI 벡터→Spring hydrate)용 — I-1(§4.6)에 `productIds`(숫자 배열) 필터 추가 또는 by-id 조회 엔드포인트. 벡터 후보의 가용성(재고·활성)·상세를 Spring 권위로 확인(원본 사본 금지). **방식2는 불요** | I-1 / BE 요청 | 🔴 BE 협의 — **방식1 라이브 전제**(방식2·오프라인 골든셋 비교는 무의존, 착수 무대기) |
| C-18 | **[신규 v0.18.0] I-22 `catalogVersion` 값 생성 주체** | 정본 I-22(§3.7)는 `catalogVersion`을 **Spring이 요청에 실어 보내는** 필수 필드로 규정하는데, 같은 문서가 *"FastAPI는 **자체** 카탈로그 인덱스(I-17로 동기화된 임베딩)로 순위를 매긴다"* 고 한다. **Spring은 AI의 인덱스 버전을 알 수 없다** — 보낼 수 있는 건 Spring 자기 카탈로그의 버전뿐이라, 그 값으로 캐시를 키잉하면 **AI 인덱스가 갱신돼도 캐시가 무효화되지 않는다**(P-5 캐시 키가 "회원 id + 카탈로그 버전 + 알고리즘 버전"이라 직격, §4.11). 필드 자체는 캐시 키·재현에 필요하니 유지하되, **값을 AI가 만들어 응답에 실어 보내고 Spring이 그것을 캐시 키에 쓰는 방향**을 제안한다 | I-22 / 이슈 #148 | 🔴 **미해결 — 정본 개정 + BE 합의 선행.** #148 착수 전 필수 |

> 참고(v0.5.0): **C-15 신설**(후보 검색 — 유일 경로·최우선). **C-4 폐기**(카탈로그 동기화 자체 없음). **C-6 재정의**(주문 알림/미러 → 질의 시점 구매 이력 조회 §4.7). **C-7 해소**(I-6 콜백, 세부는 C-13). C-1/C-2/C-3은 확정안 반영이나 Spring 수용 전까지 🔴 잔여를 유지한다.
> 참고(v0.6.0): **C-3 재작성**(BE I-2 문서 채택 — 구 JWT 포워딩/`items[]` 제안 폐기, 게스트 담기 허용). **C-16 신설**(장바구니 조회).

### 5.1 07/17 BE 확인질문 — LLM팀 답변 필요 (Q1~Q9)

BE "API·ERD 변경 정리(07/17)" Part 2가 **우리(LLM팀)에게 확정을 요청**한 9건. `우리 답(제안)`은 대부분 **기존 계약과 일치**해 즉답 가능, 일부만 팀 결정 필요.

| Q | 질문 | 우리 답(제안) | 상태 |
|---|---|---|---|
| Q1 | 세션 종료 sessionId **UUID 수용**? | ✅ **수용 완료** — AI는 정규식 없이 config 길이 상한만 적용해 UUID 포함 불투명 문자열을 받음(v0.15.17) | 확정 |
| Q2 | **I-21** 스키마·`listId` TTL·`reason` 전달 | ✅ 스키마 수용. listId=우리 생성(**UUID급 무작위 ≥128bit**)·TTL 10분(config). **reason을 I-21 콜백에 포함→CH-5로 전달**(§4.2·§4.3, BE의 SSE안 대신) — **BE 구현 확정 2026-07-18 🟢**(v0.15.15) | 형식·reason 확정, TTL 제안 |
| Q3 | **[적용]** = `{action:"confirm", draftId}` 확정? | ✅ **예** — §3.2 HITL 설계와 동일 | 즉답 |
| Q4 | **I-17** 커서·`attributes`·리뷰 텍스트 | 🟡 BE 골격 확정(2026-07-18): 인증·envelope·숫자 id·오류코드. 잔여 3건 저영향(커서 opaque·attributes 자유 dict·리뷰 MVP 제외, §4.8). 🔴 선결: I-17 배치 MVP/post-MVP 스코프 | BE 확정 |
| Q5 | **I-13** 본문 재작성(I-5 내용 복붙이던 것) | ✅ **LLM팀이 직접 재작성해 Notion 반영**(§4.4 I-13, v0.15.1) — BE 검토만 | 해소 |
| Q6 | **CH-3**(CS 챗) 라우팅 | ✅ **관리자 CS 문의(CH-3·I-5·AD-1/2·M-9) 전부 post-MVP**. **주문상태 Q&A(I-4)는 구매자 챗(CH-2)의 `order_status`로 구현**(§4.10) — 별도 CS챗 없음 | 해소 |
| Q7 | 게스트 담기 실패 **3종·차단 없음** 최종? | ✅ GUEST_NOT_ALLOWED 폐기(§3.1·§4.1). **[갱신 v0.15.16] 실패 3종**(PRODUCT_NOT_FOUND/STOCK_INSUFFICIENT/CART_ERROR) — 담기 재고검증 부활(`CART_STOCK_INSUFFICIENT`, 2026-07-22), `OUT_OF_STOCK` 폐기 유지 | 갱신 |
| Q8 | 판매자 챗 주소 `{AI_SERVER}/seller/chat`(별도) vs `/chat` 채널 | `{AI_SERVER}/seller/chat`(S-4, 별도 주소) 최종 — 채널 구분 아님(§3.2) | 즉답 |
| Q9 | 챗봇 담기 `add_to_cart` 이벤트 누가 쏘나 | **[정정 v0.15.26] FE가 쏜다** — E-1 정본에서 `add_to_cart`는 FE 12종 중 하나이고, 서버 직접 적재는 `recommendation_generated` 하나뿐이다. 구 답변("BE가 `CART_ADD(via:chat)` 적재")은 폐기 | 갱신 |

> **[v0.15.17 갱신]** Q1(I-20 UUID)·Q2(I-21 reason)·Q5(I-13 재작성)·Q6(관리자 문의 MVP 제외)은 **해소**. **Q4 I-17 스키마 골격은 BE 확정(2026-07-18)** — 잔여는 스키마가 아닌 🔴 선결 1건 **배치 MVP/post-MVP 스코프**(config vs 파이프라인 모순). 나머지(Q3·Q7·Q8·Q9)는 확정 회신 가능.

---

## 6. 부록 (Appendix)

### 6.1 FE 소비 노트 — fetch 스트리밍 + 경로 B (FE 직접 호출)

표준 `EventSource` API는 **GET 전용**이므로, `POST /chat`·`POST /seller/chat`의 SSE 응답은 FE에서 **`fetch` + `ReadableStream`** 으로 소비한다. FE는 **AI 서버(FastAPI)를 직접 호출**하며 `Authorization` 헤더에 사용자 JWT를 실어 보낸다(§2.3 a).

**구매자(`/chat`) 흐름**

1. `fetch(url, { method: "POST", headers: { Authorization: "Bearer {USER_JWT}", "Content-Type": "application/json" }, body })` 후 `response.body.getReader()`로 스트림을 읽고 `data:` 라인을 직접 파싱한다.
2. 각 이벤트(`token`/`conditions`/`action`/`suggestions`/`budget`/`products.ready`/`done`/`error`)로 디스패치한다.
3. **`token` → 챗 렌더**, **`products.ready` → 우측 패널**(§4.3 Spring 목록 GET), **`action` → 담기 결과 토스트**, **`conditions` → 제거 가능 조건 칩**, **`done.finishReason == "zero_result"` → 빈 상태**.
4. **`401` 재발급 흐름**: 요청 시작 시 `401`(TOKEN_EXPIRED/TOKEN_INVALID)을 받으면 → Spring 토큰 재발급 → 새 JWT로 원 요청 **1회 재시도**(§2.5).

**판매자(`/seller/chat`) 흐름**

5. 이벤트는 `token`/`draft`/`done`/`error`만 디스패치한다.
6. **`draft` → diff 카드**: `changes[]`의 필드별 before/after를 diff 카드로 렌더하고 `[적용]`/`[취소]`를 노출한다. **`[적용]` 시 FE는 `{action:"confirm", draftId}`를 판매자 챗(S-4)으로 보내고, AI가 HITL resume으로 Spring internal API(I-11 등)를 호출해 반영**한다(§3.2). 채팅 발화만으로는 반영되지 않는다. (**[v0.15.24] S-5 `PATCH /api/seller/products/{id}` 폐기 — 2026-07-21 미채택.** 07/17 신설했던 판매자 화면 직접 수정 경로는 채택되지 않았고, **상품 수정은 챗봇 HITL(I-11)이 유일 경로**다. Spring 코드에도 해당 엔드포인트가 없다.)

- **버퍼링 주의**: FE 직접 호출이므로 Spring 중계 버퍼링 이슈는 없다. 남는 주의점은 **FastAPI 앞단 리버스 프록시**의 응답 버퍼링 비활성화뿐이다(§2.4).
- **[v0.7.0] 취소·동시 스트림**: 사용자가 응답 중단 시 `AbortController.abort()`로 연결을 끊는다(별도 취소 API 없음, §2.9 b). 스트리밍 중에는 입력창을 비활성화한다 — 중복 전송 시 서버가 `409 STREAM_IN_PROGRESS`를 반환한다(§2.9 a). `504 UPSTREAM_TIMEOUT`(스트림 전)·in-stream `error`(스트림 중) 구분은 §2.9 c.

### 6.2 버전 관리 / 변경 이력 규약

본 문서는 **semver 유사(major.minor.patch)** 로 버전을 매긴다.

- **major**: 하위 호환을 깨는 계약 변경(필드 제거·의미 변경·엔드포인트 삭제).
- **minor**: 하위 호환 유지 추가(신규 엔드포인트·선택 필드·🔴 항목 확정).
- **patch**: 오탈자·설명 보강.
- 소유 SPEC(`SPEC-RECOMMEND-001`, `SPEC-PROFILE-001`)의 계약이 개정되면 본 문서를 동기화하고 변경 이력을 남긴다. `/events/*` HTTP 계약은 본 문서가 소유하므로(결정 21), 그 개정은 본 문서 버전 증가로 반영한다.

#### 변경 이력 (Change Log)

| 버전 | 날짜 | 변경 |
|---|---|---|
| v0.18.0 | 2026-07-31 | **[#148] 홈 추천 계약 등재 — I-22(§3.7)·P-5(§4.11)가 사본에 통째로 누락돼 있던 drift를 해소했다.** 정본(Notion「📡 API 명세서」) 2026-07-28 확정본을 대조해 옮겼다. (1) **§3.7 I-22 `POST {AI_SERVER}/internal/recommendations/home` 신설** — Spring → AI 위임 호출, `X-Internal-Token`, **연결 2s/응답 3s**(채팅 90s와 무관, 메인 렌더 블로킹 방지). 요청 `{memberId, limit, catalogVersion, signals{recentlyViewed·cart·recentPurchased}}`에서 `limit`은 **최종 노출 목표치**(AI는 품절 드롭 대비해 넉넉히 반환, Spring이 자름)이고 `recentPurchasedProductIds`는 **가중치가 아니라 제외 필터**다. 응답 `{outcome, recommendationRequestId, listId, items[{productId, reason}]}` — **배열 순서가 곧 순위**(`position` 없음), `listId`는 AI 생성 **≥128bit 무작위**(I-21 §4.2와 동일 규칙). **왕복 1회로 끝나며 I-21 콜백을 타지 않는다.** (2) **`outcome` 3종과 cold start 규약** — `PERSONALIZED`/`NO_PROFILE`/`INSUFFICIENT_CANDIDATES`를 **전부 200**으로 답하고 **fallback 판단은 Spring이** 한다. 프로필 부재·후보 부족으로 AI가 4xx/5xx를 내면 계약 위반이다. 단 **입력·인프라 실패용 4종은 존재한다**(`BAD_REQUEST`/`INTERNAL_TOKEN_INVALID`/`UPSTREAM_UNAVAILABLE`/`UPSTREAM_TIMEOUT`) — 이슈 #148 본문의 *"FastAPI가 4xx/5xx를 내지 않는다"* 는 **cold start에만 걸리는 서술**이라 여기서 범위를 명확히 했다. (3) **provenance 비노출 [HARD]** — 프로필 원문·prompt·모델 식별자를 응답·로그·trace에 싣지 않고, 알고리즘·모델 버전은 AI 자체 테이블 보관(평가 산출물 전용)이라 **`algorithmVersion`을 응답에 넣는 구현은 계약 위반**이다. (4) **§4.11 P-5 `GET /api/products/recommended` 신설**(레인 d, FE↔Spring) — Spring이 이를 서빙하려 I-22를 호출하는 **서브 관계**라 등재해야 의존이 드러난다. `source=AI_RECOMMENDED|POPULAR_FALLBACK`, fallback 시 상관키는 **Spring이 발급**(AI가 대신 만들지 않는다), `fallbackReason`·`cacheStatus`·`algorithmVersion`·`modelVersion`은 와이어 비노출. **캐시 TTL 10분 · `listId` 귀속 유효기간 24시간 확정(2026-07-30)** — 홈은 조회 API가 없어 CH-5의 *조회* 만료와 성격이 다르다. 정본 I-22 페이지에 남은 *"TTL은 BE 결정 필요"* 는 stale이며 BE 통보 대상. (5) **§1.2 레인 (b) 재정의** — 더 이상 "이벤트 채널"만이 아니다. I-22는 통지가 아닌 **동기 요청/응답**이라 §2.7 `/events/*` 멱등 규약이 적용되지 않으며 **재시도 = 새 `recommendationRequestId`·`listId` 발급**이다. 레인 (d)에 P-5, §2.3 (b)에 I-22 인증을 등재했다. (6) **🔴 C-18 신설 — `catalogVersion` 값 생성 주체 미해결.** 정본은 Spring이 실어 보내게 규정하는데 랭킹은 AI 자체 인덱스로 매긴다 — **Spring은 그 버전을 알 수 없어** 그 값으로 캐시를 키잉하면 AI 인덱스가 갱신돼도 캐시가 무효화되지 않는다. **정본 개정 + BE 합의가 #148 착수 전 필수**다. ※ 본 개정은 문서 등재만이며 구현은 #148, 재사용할 scoring baseline은 #145다. |
| v0.17.0 | 2026-07-31 | **[#187] signed `sessionId` 기반 stable `context_id`와 guest→member claim, D6/I-20 lifecycle을 확정했다.** `/events/session-claim`의 strict BIGINT/서비스 토큰 계약, claim 뒤 old guest의 turn/thread 미생성, guest transcript와 member profile 입력 격리, 재시작 가능한 backfill과 단조 grace(최소 24시간), PostgreSQL clock 기준 durable quiet(최소 90초이자 stream timeout 이상), exact legacy late-write reopen 및 destructive GC gate를 명시했다. 외부 출시는 BE #63 signed-ticket 증거, 90초 drain, FE #52 실제 3-tab 검증, 운영 지표 확인 뒤에만 완료로 본다. |
| v0.16.3 | 2026-07-30 | **[#164] I-4 주문 상태 문의 구현 계약 정합.** 구매자 라우팅을 `recommend`/`cart_add`/`cart_view`/`order_status`/`general` 5-way로 확장하고 §4.10을 신설했다. JWT-derived member identity, `GET /internal/members/{userId}/orders/status?recent=3`, internal token/3초 timeout, literal-success envelope, aware timestamp와 Spring 어휘/canonical pair의 전체 payload 검증, 최대 3개 주문·주문당 3개 상품 및 `외 N개`, empty와 dependency degradation 구분, `token`→`done(stop)` 정상 종료, privacy-safe correlated route log, 일반 대화 보존과 response-derived state non-copy 경계를 확정했다. §1.2·§4의 reverse-call 현황도 정확한 17건 `{I-1,I-19,I-4,I-2,I-18,I-21,I-6,I-7,I-13,I-14,I-15,I-16,I-9,I-10,I-11,I-12,I-17}`로 정정했다. |
| v0.16.2 | 2026-07-30 | **[#194] I-14/I-15 응답 스키마 BE 실측 확정 + I-6 이상 감지 규칙 명문화.** (1) **I-14 `order-events` 응답 확정** — `rows`/`total`/`byStatus`/`cancelReasonsTop` (shape 상호 배제: 목록/stats/groupBy=memberId). 구 추정 스키마(`events`/`stats`)는 BE에 없는 필드라 AI 도구가 **항상 0건**을 반환하던 버그의 원인(`extra="allow"`가 검증 실패를 은폐). (2) **I-15 `product-changes` 응답 확정** — `rows[{productId,productName,changeType,oldValue,newValue,createdAt}]`+`total` (구 `logs` 폐기, 동일 패턴 버그). (3) **I-6 이상 감지 규칙 명문화(BE `SellerSalesService.withAnomaly` 실측)** — 직전 최소 3(MIN_WINDOW)·최대 7(MOVING_WINDOW)포인트 평균 대비 ±30%, 기준선 0 + 매출 발생 = 이상(`deviationPct` null), **매출 0 포인트는 이상 아님**(저볼륨 무판매일 -100% 노이즈 방지). AI `calc.detect_sales_anomalies`를 동일 규칙으로 정렬. (4) I-14/I-15 `limit`(기본 100) 쿼리 등재 — `rows`는 절단본, 전수는 `total`. |
| v0.16.1 | 2026-07-30 | **[#167] I-21 `listId` 보안 규약 복원.** CH-5가 인증 불필요 공개 조회라 `listId`가 사실상 bearer 키인 점을 명시하고, FastAPI가 **UUID급 무작위(≥128bit)** 로 생성하며 순번·타임스탬프 등 추측 가능한 형식을 금지하도록 §4.2를 확정했다. 실제 I-21 예시의 `list-4471`을 32자리 무작위 hex로 교체하고 C-9·Q2의 형식 미확정 표기를 해소했다. 현재 `uuid4().hex` 구현을 형식·고유성·I-21/SSE 동일성 회귀 테스트로 고정했다. |
| v0.16.0 | 2026-07-30 | **[정본 SPEC-CHAT-SESSION 반영] `sessionId`(접속) · `threadId`(방) 축 분리 — MVP의 `sessionId == threadId` 전제 폐기.** 한 접속 아래 여러 방이 **동시에** 존재하는 멀티탭 대화를 지원하기 위해 두 식별자의 역할을 갈랐다. (1) **§2.6 식별자 모델 신설** — 축별 발급 주체·수명·담당 상태를 표로 확정. `sessionId`=Spring CH-1 발급(Redis TTL 10분 sliding)·프로필 세션버퍼·I-20·`conversation_turns.conversation_id`(primary), `threadId`=**FE 생성**(서버 왕복 없음)·필터 누적·장바구니 pending·되돌리기·동시 스트림 락·`conversation_turns.thread_id`. 구 정의 *"만료 의미 없는 불투명 스레드 키"* 를 **폐기** — "스레드 키"는 이제 `threadId`의 것이고, AI가 만료를 판정하지 않는 이유는 만료가 **없어서**가 아니라 **판정 주체가 Spring이라서**다. (2) **§2.9 a 동시 스트림 락을 세션→방 단위로 개정** — `409 STREAM_IN_PROGRESS`의 판정 키가 `sessionId`에서 **`threadId`** 로 바뀐다. 세션 단위로 잠그면 탭 B가 탭 A의 스트리밍 때문에 409를 맞아 **축 분리의 목적이 정면으로 무효화**된다. §2.5 오류표도 동기화. (3) **§3.5 I-20 사유를 `logout` 1종으로 축소** — 새 대화가 CH-1을 부르지 않고 `threadId`만 갱신하게 되어 `newConversation`이 발화되지 않는다. Spring이 I-20을 쏘는 경우는 로그아웃뿐이고 나머지는 Redis TTL 만료 + AI 내부 비활동 sweep이 담당한다(C-8 행 동기화). (4) **[D5] CH-1 멱등 등재 + 구 "CH-1 재호출 = 새 세션(맥락 단절)" 경고 폐기**(§1.2 레인 d) — Spring이 Redis `SETNX`로 기존 세션을 그대로 반환하므로 CH-1을 몇 번 불러도 세션은 하나다. **정확성은 `SETNX`가 책임지고 FE Web Locks(D1)는 최적화**다(한 브라우저 안에서만 통해 폰·PC 동시 접속을 막지 못한다). 축출을 없앤 뒤에는 밀린 세션이 CH-1b로 TTL을 연장하며 유령으로 남아 I-20이 안 나가는 문제가 생기는데 이를 `SETNX`가 막는다. **예외 = 게스트 첫 방문 멀티탭**(쿠키 부재 → 게스트 2명 생성 → 밀린 탭이 CH-1b `403`)은 신원이 갈라지는 것이라 `SETNX`로 막을 수 없어 Web Locks가 방어한다. (5) **[D6] 맥락 TTL을 방→접속 단위로** — 어느 방에서든 활동이 있으면 그 `sessionId`의 **모든 방** TTL을 함께 연장하고 세션 종료 시 일괄 정리한다. 방마다 생사가 갈리면 탭을 옮겼을 때 한쪽 맥락만 사라져 사용자가 이해할 수 없다. (6) **§6.3 저장·로그 축 정합** — checkpointer thread 키를 `sessionId`→**`threadId`** 로 정정하고, `conversation_turns`를 **session-primary + `thread_id` 병기**로 명시(세션 종료 스캔은 세션 축, 방별 조회·정리는 방 축이라 어느 한쪽만으로는 불가). 구조화 로그에 **`threadId` 필드 신설** — 멀티탭이면 한 `conversationId` 아래 여러 방 로그가 섞여 방을 못 가리면 동시 스트림을 분리해 읽을 수 없다. **🔴 잔여**: `SETNX` 멱등 키 스코프(`sub` vs `sub_type`+`sub`)와 멱등 반환 시 세션 TTL sliding 갱신 여부 — BE 확인 대기. |
| v0.15.27 | 2026-07-30 | **[사본 drift 정정] 정본 대조로 틀린 서술 3건 교체.** (1) **담기 이벤트 적재 주체** — §4.1 I-2의 *"`CART_ADD(via: chat)` 이벤트는 BE가 적재(AI 무관)"* 와 §5.1 Q9의 같은 답변을 **폐기**했다. E-1 정본에서 `add_to_cart`는 **FE가 쏘는 12종 중 하나**이고, 서버가 직접 적재하는 이벤트는 `recommendation_generated` 하나뿐이다(E-1 HTTP로 들어오면 드롭). 챗봇 경로도 FE가 SSE `action`(`CART_ADDED`) 수신 시점에 쏜다. (2) **`budget` 이벤트 제외** — 정본(Notion CH-2)이 *"현재 코드에 미구현 → 명세에서 제외(필요 시 post-MVP)"* 로 정리했는데 사본은 §3.1에 스키마를 그대로 두고 이벤트 순서 계약에도 넣어두고 있었다. 스키마는 post-MVP 참고용으로 남기고 순서 계약에서 뺐다(이슈 #163). (3) **공통 헤더 규약 §2.5 신설** — `X-Request-Id`·`traceparent` 는 전 API 공통이라 엔드포인트 행 단위인 정본 DB에 놓을 자리가 없었다. Notion「프로젝트 자료실」에 **공통 규약 페이지를 신설**하고 본 사본 §2.5에 AI 소관 요약을 넣었다(#141·#134·#151). 실측: inbound `X-Request-Id` **수용 미구현**(`request_context_middleware`가 `new_request_id()`를 조건 없이 호출) · Spring 역호출 **전파 미구현**(`X-Internal-Token` 하나만) · 응답 echo 는 구현됨 · `traceparent` 는 코드베이스에 없음. (4) **`search.query` PII 기준** — 정본 E-1이 *"개인정보를 properties에 넣지 않는다"* 와 *"`search` 필수 = `query`"* 를 동시에 말해 **자기모순**이었고, FE가 그 금지 조항을 근거로 `queryLength`만 보내 `searchTopics` 워커가 돌 수 없었다. 금지 대상은 **FE가 굳이 끌어다 넣는 이름·주소·연락처·이메일**이며 사용자가 직접 입력해 이미 서버로 보낸 검색어는 원문을 싣고 **보존기간으로 관리**한다 — 정본 E-1에 「개인정보 기준」 절로 명확화했다. |
| v0.15.26 | 2026-07-28 | **[사본 동기화] §3.1 요청 계약 확장 + in-stream `error` 추적 필드 — 정본(Notion "📡 API 명세서" CH-2) 2026-07-28 개정 반영.** (1) **`conditionActions` 신설**(이슈 #84) — 조건 칩 제거를 `[{op:"remove", field}]` 구조화 배열로 받는다. 구 규약 문자열(`"[조건 제거] priceMax"`) 왕복 방식은 **폐기** — FE는 그 방식으로 구현돼 있으나 AI에 수신부가 없어 현재 칩 제거가 무동작이다. `conditionActions`가 있으면 `message` 빈 문자열 허용, 둘 다 비면 `400`. 구매자 전용(`BuyerChatRequest`). (2) **`conditions` 칩 `field` 허용값 6종 확정** — `category`/`priceMax`/`priceMin`/`brand`/`ratingMin`/`keyword`. 종전에는 예시 둘만 있어 허용 집합이 계약에 없었는데, `conditionActions.field` 검증의 전제라 등재했다(코드 `build_condition_chips` 실측과 일치). (3) **`screen` 신설**(이슈 #118) — `{pageType, filters?, products?, columns?}`. `pageType`은 **라우트가 아니라 우측 패널 내용**을 가리킨다(채팅이 전용 페이지에만 있어 라우트를 실으면 정보가 0). `products`는 **서버가 모르는 목록만**(P-4 인기상품·판매자 자사 상품) — 추천 카드는 `listId`로 서버가 알고, 되돌려주면 위조 경로가 된다. `columns`는 반응형 그리드 열 수로 "3번째 줄 2번째" 좌표 지시 해소에 쓴다(`rows`·항목별 좌표는 파생값이라 제외). `pageType`은 **E-1 `page_view`와 같은 enum을 공유**한다 — 화면 어휘를 새로 만들지 않기 위해서다. 라우트 `path`는 쿼리스트링 PII 위험으로, 한글 `label`은 AI config 매핑으로 대체해 **계약에서 뺐다**. 07-17 FE 제안(`ChatScreenContext`)과 #118의 "노출 상품 목록" 요구를 **한 필드로 통합**했다(같은 사실의 두 측면). `products`는 담기 허용 목록을 넓히는 입력이며 **두 목록 밖 id 차단 가드는 유지**한다. **구매자·판매자 공용 필드**라 §3.2에도 등재 — 판매자 대시보드는 `meta.lane`·`done.panel`로 AI→FE 화면 조작만 있고 반대 방향이 비어 있었다. (4) **`products.ready`의 `listId`(단일) → `listIds`(배열, 항상)** — I-21이 `lists`를 1~10개 보내므로(§4.2) 단일 필드로는 세트형·니즈별 추천을 나를 수 없었다. 정본 I-21·CH-5는 이미 복수 전제인데 CH-2와 본 사본만 단일로 남아 있던 **3자 불일치**다. 목록이 1개여도 길이 1 배열로 보내 FE 분기를 없애고, 이벤트는 여전히 정확히 1회다. 예시의 `"list-4471"`도 §4.2가 금지한 추측 가능 형식이라 교정했다. **구현(`ProductsReadyData.list_id: str`)도 단일이라 코드 변경이 따라야 한다.** (5) **in-stream `error`에 `requestId`·`retryable` 추가** — 스트림 전 실패(§2.5 봉투)에는 `requestId`가 있는데 스트림 내부 실패에는 없어 추적이 끊겼다. `retryable`은 `code`로 복원 불가(같은 `LLM_UNAVAILABLE`이 미구성/일시불가에 겸용)라 emit 지점이 정한다. §3.2 판매자 스트림도 동일(`ErrorData` 공용). |
| v0.15.25 | 2026-07-28 | **[#171] I-1 응답에 `reviewCount` 추가 — `rating=0`의 의미 판별.** `rating`만으로는 **"리뷰가 없어 0"(데이터 부재)** 와 **"리뷰가 있고 하한 미달"(진짜 저평점)** 을 가를 수 없어, BE가 `reviewCount`를 함께 제공하기로 합의했다(2026-07-28). `reviewCount=0`이면 데이터 부재, `>0`이면 실제 저평점이다. `null`/미전송이면 `rating`이 지배(구 동작 폴백). **AI 계산용(비표시)** 이며 표시용 리뷰수는 여전히 경로 B(§4.3)가 채운다 — 이에 따라 #100의 *"`reviewCount`는 표시전용·I-1 미반환"* 을 **부분 개정**해 미반환 목록에서 제외했다(§4.6). ※ 이 행은 v0.16.0 병합 시 소급 등재했다 — 원 커밋(de9f34f)이 버전 헤더만 올리고 이력 행을 남기지 않아 §6.2 규약(개정은 이력에 남긴다)에 어긋나 있었다. |
| v0.15.24 | 2026-07-27 | **[사본 동기화] S-5 폐기 반영 — 정본(기획 저장소 Notion "📡 API 명세서" DB) 2026-07-21 결정이 본 사본에 미반영이었다.** S-5 `PATCH /api/seller/products/{id}`(판매자 화면 직접 수정, 07/17 신설)는 **미채택**이며 **상품 수정은 챗봇 HITL(I-11)이 유일 경로**다. §3.2 draft 절의 "챗봇 수정(I-11)과 병존" 서술을 폐기 표기로 교체. Spring 코드 실측에서도 `/api/seller/**`에 PATCH 엔드포인트가 없어 정본·코드 모두와 일치시켰다. 계약 변경이 아니라 **사본 drift 정정**이다. |
| v0.15.23 | 2026-07-27 | **[#100 P0/P1/P2] I-1 §4.6 실측 정합.** 표시 전용 필드(`imageUrl`·`originalPrice`·`reviewCount`·`options`)를 응답표에서 제거하고 CH-5(§4.3) 하이드레이션 이관 명시(AI 추천 경로 미사용), `price`·`rating`을 "AI 계산용(비표시 — 예산검증 `verifiedSum`·평점 사후필터·rerank 신호, 질의 시점 필요)"으로 명기해 display 오분류 재발 차단, envelope 예시를 실측 `{success, data:[...]}`(bare array)로 정정, 요청 `brandName` 단일→다중(반복 파라미터 → `WHERE brand IN`, 방법 D), `totalCount` 필드 불필요 결정 반영. |
| v0.15.22 | 2026-07-26 | **[#100 P1] I-1 `color` 요청 파라미터 연결.** decompose가 색상 조건("빨간"·"검정")을 `filters.color`로 추출·전송하고, BE I-1이 `attributes` LIKE로 필터. 요청 모델·쿼리 변환에 `color`가 없어 Spring 색상 검색을 못 쓰던 것을 해소. |
| v0.15.21 | 2026-07-24 | **[#100 P2] I-1 `size` 제거 → 라운드1 전량 반환 + AI top-K.** BE 합의로 Spring 요청에서 `size`를 제거(반환 상한 없음)하고, 결과 수 제한(top-K)은 AI `search_catalog`가 사후필터(dedup·평점) 뒤 `filters.limit`로 절단. `ProductSearchFilters.limit`은 Spring `size`가 아니라 AI 후보 상한(rerank 입력)이다. |
| v0.15.20 | 2026-07-27 | **[C-1 해소] 인증 계약을 Spring 코드 실측으로 확정.** 명세가 🔴 협의 대기로 남겨둔 항목이 BE에는 이미 구현돼 있어 역반영한다. (1) **스트림 티켓 클레임 실값 확정** — `iss`=`jarvis-spring-auth`, `aud`=`jarvis-fastapi-ai`, `scope`=`chat:stream`, TTL **60초**(구 "30~60초" 범위 → 실값). CH-1/CH-1b 응답이 `ticketTtlSeconds`로 실값을 함께 반환한다. (2) **판매자 티켓 형식 확정** — `role="seller"`(**소문자**) + `brandId`(**숫자**). `role` 클레임은 **판매자 티켓에만** 실리고 구매자·게스트는 `sub_type`만 갖는다. (3) **CH-1b `POST /api/chat/tickets` 구현 확인** — 구 "가칭·신설 필요"를 확정으로 전환. 요청 `{sessionId}`, 응답은 CH-1과 동일 DTO. 세션에 보관된 `sub_type`+`sub`으로 **소유자를 검증**하고(불일치 거부), 재발급과 함께 세션 TTL을 sliding 갱신하며, 판매자 세션은 보관된 `brandId`로 SELLER 스코프 티켓을 재발급한다. (4) **CH-6 `POST /api/chat/seller/sessions` 등재**(레인 d) — 판매자 챗 입구. `brandId`는 BE가 JWT 검증 후 DB에서 도출해 클레임에 박는다(클라이언트·LLM 주장 무시). (5) CH-1 응답 필드에 `llmSseUrl`(FE→AI SSE 직결 주소) 명시. **AI 측 와이어 동작 변경 없음** — `_norm_role`이 대소문자 무관 비교라 소문자 `seller`도 기존대로 매칭된다. C-1 🔴 잔여는 **서비스 토큰 회전·만료·mTLS 운영 정책**만 남는다. |
| v0.15.19 | 2026-07-23 | **[이슈 #79] 프로필 세션 종료 트리거의 MVP 소유권 확정.** Spring I-20의 알려진 사유는 `logout`/`newConversation` 2종으로 한정하고 탭 닫기 신호는 제거한다. AI는 회원 발화 저장 시 DB 서버 시각의 `lastActivityAt`을 갱신하며, 단일 인스턴스 스케줄러가 기본 60초마다 10분 이상 비활성 세션을 bounded batch로 선점한다. 내부 timeout은 HTTP 자기 호출 없이 I-20과 같은 finalizer 및 고정키 claim으로 직렬화하되, idle 성공은 영구 멱등 완료가 아닌 재개 가능한 checkpoint로 claim을 해제한다. 새 활동은 completed activity를 active로 되돌리고 이전 processing/completed 종료 generation을 같은 저장 transaction에서 무효화한다. terminal finalizer는 처리 중 새 activity를 영구 완료로 덮지 않으며 scheduler는 라이브 스트림 슬롯을 점유하지 않는다. 처리 전 활동·활성 스트림을 재확인하며 claim별 실패·crash는 해제/lease로 재시도한다. 외부 요청 스키마에는 변경이 없다. |
| v0.15.18 | 2026-07-23 | **[C-4/I-17 상태 계약 정합] `items[].status`를 Spring `ProductStatus`와 동일한 `ON_SALE`/`HIDDEN`으로 확정.** Spring은 별도 매핑 없이 enum 값을 그대로 반환하고, AI 배치는 `ON_SALE`을 생성·갱신하며 `HIDDEN`의 기존 artifact를 삭제한다. 구 `ACTIVE`/`DELISTED`를 포함한 미정의 값은 응답 계약 위반으로 페이지 전체를 fail-closed 처리한다. 해당 항목만 skip하지 않고 커서를 유지해 Spring 수정 뒤 같은 `since`부터 재처리한다. §4.8 응답 예시·필드 설명·배치 흐름·복구 규약 갱신. |
| v0.15.17 | 2026-07-22 | **[이슈 #62/#64] I-20 실측 계약과 실패 안전 멱등 lifecycle 확정.** 요청을 `{sessionId,userId(number BIGINT),reason?}`로 정렬하고 `eventId`·`endedAt` 제거. `userId`는 양의 정수만 엄격히 허용하고 enum 미강제 `reason`은 최대 64자로 방어한다. 검증 → `(userId,sessionId)` `PROCESSING` claim(token+lease) → 버퍼 처리 → 성공 시 `COMPLETED` 순서다. 첫 빈 버퍼 통지는 `202 accepted`, 활성/완료 동일 통지는 `202 duplicate`. delta/consolidation 실패·취소는 버퍼 보존+claim 해제, crash/해제 실패 claim은 lease 만료 후 재선점한다. Spring PR #24 송신 계약과 정합하며 C-8/Q1 해소. |
| v0.15.16 | 2026-07-22 | **[C-3 재개정] 담기 재고검증 부활 — BE I-2 `CART_STOCK_INSUFFICIENT` 신설(2026-07-22).** 합산 수량 > 재고 시 `400 CART_STOCK_INSUFFICIENT` + `error.detail.availableStock`(남은 재고, 재고는 상품 단위). AI는 `action` `CART_ADD_FAILED` + `reason: "STOCK_INSUFFICIENT"` + message에 남은 재고 수 노출("재고가 N개뿐이에요"; 재고 0=품절은 "품절된 상품이에요"). 담기 실패 **3종**(`PRODUCT_NOT_FOUND`/`STOCK_INSUFFICIENT`/`CART_ERROR`) — v0.15.5 "담기 재고검증 없음·OUT_OF_STOCK 폐기"를 뒤집음(품절=stock 0이 아니라 "재고 부족=N개 남음"이라 신규 코드 채택, `OUT_OF_STOCK`은 폐기 유지). 수량 상한(합산 > 99)은 `VALIDATION_ERROR`로 별개 → `CART_ERROR` + BE 동일 문구 "수량은 최대 99개까지 담을 수 있습니다.". **이 파일을 계약 정본으로 승격**(외부 사본 의존 폐기). |
| v0.15.15 | 2026-07-22 | **[C-9/Q2 확정] I-21 `reasons` 콜백 포함 확정(🔴 역제안→🟢).** BE가 §4.2 명세대로 구현(2026-07-18) — 추천 `reason`을 SSE 직접이 아니라 **I-21 콜백 `reasons[{productId, reason}]`에 포함**해 Spring이 Redis 저장 후 CH-5 카드에 echo. 구 BE 07/17 안(reason=SSE·콜백 불포함) 폐기. `reasons`는 선택 필드·productId 키잉(부분집합/순서무관). §4.2 필드표·주석·C-9·Q2 마커 🟢 갱신. AI→Spring 전송분은 jarvis-ai 이슈 #61에서 구현. 정본(기획 repo) 동기화 완료(2026-07-22). 잔여 🔴: `listId` TTL·형식(C-9). |
| v0.15.14 | 2026-07-20 | **[임베딩 모델 확정] 셀프호스트 torch → Google `gemini-embedding-001` API.** dim 1024→1536(MRL; pgvector 표준 인덱스 ≤2000 적합·1536 L2 정규화), $0.15/1M(배치 $0.075), 결정 6 개정. dragonkue·torch·`--group embedding` 폐기. 임베딩 단계 외부 API 호출 전환(search_doc·쿼리 텍스트 Google 전송). text-embedding-004 폐기(2026-01-14). |
| v0.15.13 | 2026-07-20 | **[#7 결정] I-17 배치 MVP 편입 + 임베딩 검색 방식 확정.** §4.8 OPEN 해소: 방식1·2를 `SearchBackend`로 둘 다 구현해 골든셋 확정(착수=방식2 라이브+방식1 오프라인 랭킹, BE 무대기). **C-17 신설** — 방식1 라이브용 I-1 id 제약 조회 BE 요청 🔴. config.py "post-MVP"→MVP 정정. |
| v0.15.12 | 2026-07-20 | **[C-4 골격 확정] I-17 상품 변경 배치 — BE "상품 정보 Batch" Notion 대조(2026-07-18).** 인증 `X-Internal-Token`(Bearer 아님)·envelope `{success,data}`·`productId` 숫자 BIGINT·오류(`INVALID_CURSOR`/`INTERNAL_TOKEN_INVALID`/`FORBIDDEN`)·`since="0"`·`hasMore` 루프 확정. C-4 🔴→🟡: 주기=AI config·페이지=`limit`(500)·커서=opaque라 무영향. 잔여 3건(커서 형식·`attributes`·리뷰) 저영향 → 소비 언블록. 스코프(MVP?)는 별건. |
| v0.15.11 | 2026-07-20 | **[C-16 해소] I-18 장바구니 조회 확정 — BE "챗봇 장바구니 조회" 문서(2026-07-18).** `productName`/`optionName` 필수 포함·`CART_QUERY_INVALID`(400)·경로·쿼리·인증 확정 → C-16 🟢. §4.9 필드표·협의 반영. |
| v0.15.10 | 2026-07-19 | **[C-6 해소] I-19 `categoryName` 추가 — BE 확정(라이브 Notion 2026-07-19).** I-19 items[]에 `categoryName`(string) 포함. 소모품 카테고리 억제·되돌리기 칩(결정 14-F) 구현 언블록(jarvis-ai 완료). 소모품 판정은 AI-side(MVP config·catalog 속성사전). |
| v0.15.9 | 2026-07-19 | **[C-6] I-19 `categoryName` 추가 요청 공식화(LLM팀 → BE).** 결정 14-F 소모품 카테고리 억제·`suggestions.revert.category` 칩(§3.1)은 구매 상품 category 가 유일 소스인데 I-19 items 에 없어(§4.7 갭) 구현 불가. **요청: I-19 items[]에 `categoryName`(string) 추가**(I-1과 동일 필드). exact productId dedup(#4) 구현 완료, 카테고리 억제만 이 확정에 의존. 계약 변경 아님. |
| v0.15.8 | 2026-07-19 | **잔재 청소 + 옵션 스키마 확정 반영(BE Notion 대조).** (1) **OUT_OF_STOCK 잔재 제거** — §3.1 reason·§4.1 C-3 잔여·Q7의 `OUT_OF_STOCK`을 **폐기(v0.15.5 결정)**로 정리 → 담기 실패 **2종**(`PRODUCT_NOT_FOUND`/`CART_ERROR`). (2) **[C-3 해소] 옵션 스키마 확정**(BE 2026-07-18) — `error.detail.options:[{optionId,name,extraPrice}]`, OPEN-CART-2 해소. (3) C-3 잔여 정리 후 서비스 토큰 발급만 🔴. 계약 자체 변경 없음. |
| v0.15.7 | 2026-07-19 | **[§3.1] `sessionId`/`threadId` 길이 상한 명시** — config `chat_key_max_chars`(기본 200자), 초과 시 400. 불투명 키가 registry·대화저장소·로그에 쌓이는 남용 방어(#8 리뷰). |
| v0.15.6 | 2026-07-19 | **[§3.1] `message` 길이 상한 명시** — 최대 = config `chat_message_max_chars`(기본 4000자), 초과 시 `400 BAD_REQUEST`. PII·메모리 방어(`/chat`·`/seller/chat`). 이슈 #8(대화 저장) 리뷰에서 코드가 상한을 도입하며 계약이 실질 변경됨 → "명세 개정 먼저" 규칙에 따라 정본에 반영(코드는 config 주입, 하드코딩 금지). |
| v0.15.5 | 2026-07-19 | **BE DB DDL(MariaDB) 대조 + 판정규칙 확정(사용자).** 판정규칙 = **API 표면(method·경로·파라미터·status enum·error code)은 Notion, 데이터 타입(id 숫자 BIGINT·guest UUID·배송비 없음·camelCase)은 DDL**. (1) **[C-15 확정] I-1 = GET 그대로 수용**(POST 역제안 폐기) — Notion 파라미터 `keyword·categoryName·minPrice·maxPrice·brandName·size≤30`. **`excludeProductIds`·`ratingMin`·`sort` 파라미터 없음** → dedup·평점·정렬은 **AI 사후필터**로 이동. 응답 = `{success,data:{items[...]}}`(BE I-1), `stock`·`totalCount` 미제공(estCount 소스 🔴). (2) **[C-5 해소] `attributes` 구조 확정** — 축 = `category.attribute_schema`(키 배열), 값 자유텍스트(DDL D7·D11). (3) **[C-3 해소] 담기 재고검증 없음** — DDL상 재고 차감=주문 시점, SOLD_OUT 미도입(품절=stock 0), `OUT_OF_STOCK` 담기 오류 폐기. (4) **[C-6 정정] I-19 `status`=6종**(`PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED`, Notion) — 구 `representativeStatus` 8종은 O-3(FE `/api/orders`) 오용이라 폐기. (5) **Notion stale 통보 대상**: I-19 페이지 snake_case·문자열 id·`shipping_fee:3000` (DDL 기준 숫자·camelCase·배송비 0 우선). 타입 확정(모든 PK BIGINT·guest CHAR(36))은 v0.15.3/4와 일치. |
| v0.15.4 | 2026-07-18 | **v0.15.3 stale 참조 정리(팀원 PRD/SPEC-CART 검토 중 발견).** (1) 필드표 `productId`/`optionId` `string`→`number`, `guestId`=UUID 문자열 명시(§4.1·§4.5·§4.6·§4.7) — §2.6 숫자 개정을 하위 표까지 전파. (2) **C-5** "SSE/FE 경계 문자열 정규화" 서술 폐기 — 구매자 SSE는 productId 미탑재(경로 B), 코드 int 정렬 완료(v0.15.3). (3) **I-19 구매이력 경로 표기 통일**: 구 별칭 `GET /orders/recent`(§1.2 등) → `GET /internal/members/{id}/orders`(§4.7 BE 실측) 5곳. (4) **CLAUDE.md(정본·hk-final 미러)**: 정본 버전 v0.7.0→v0.15.3, 인증레인 정정("장바구니만 서비스토큰·나머지 JWT 포워딩" → AI→Spring internal은 전부 `X-Internal-Token`), cart 조회 `I-9`→`I-18`. 이슈#3 제목 동기화. (5) **§4.6 I-1 `excludeProductIds` `string[]`→`number[]` + JSON 예시 문자열 id 교정**(§4.1 I-2·§4.5 draft/I-9·§4.6 검색 req/resp·§4.9 I-18의 `productId`/`optionId`) — I-19가 숫자 productId를 반환하므로 **dedup 제외 목록도 숫자라야 exact 제외가 성립**(문자열대로 구현 시 BIGINT와 불일치로 조용히 실패). 계약 자체 변경 없음(참조 정합만). |
| v0.15.3 | 2026-07-18 | **productId·id 타입 = DB 스키마 기준 BIGINT 확정(사용자).** §2.6 개정: 상품/옵션/장바구니/주문 id = **숫자(BIGINT)**, **게스트 id만 UUID 문자열**(guest.id CHAR(36)). 구 "경계별 문자열 정규화·전 구간 문자열" 규칙 폐기. **CLAUDE.md(정본·hk-final) "productId 전 구간 string" 규칙도 개정.** 코드 반영: `schemas/spring.py`·`chat.py`의 상품/옵션/장바구니/주문 id → `int`, `guest_id`·`get_cart` → `str`(UUID), dedup 비교 타입 정합(SpringProduct·excludeProductIds → int). BE I-17 예시가 문자열 productId를 보이나 DDL은 BIGINT — 스키마 기준 int(BE 표기 불일치 통보 대상). (Claude PR 리뷰가 CLAUDE.md 불일치를 지적해 해소.) |
| v0.15.2 | 2026-07-18 | **BE 확인질문 Q2·Q4·Q6 초안 반영(결정 대기).** (1) **Q6 해소**: 관리자 CS 문의(CH-3·I-5·AD-1/2·M-9) 전부 **post-MVP**. **주문상태 Q&A(I-4)는 구매자 챗(CH-2)에 흡수** — 별도 CS챗 없음(§1.2 레인 c·§3.1). (2) **Q2 초안(역제안)**: 추천 `reason`을 **I-21 콜백에 포함**(`reasons[{productId, reason}]`)해 Spring이 CH-5 카드에 echo(§4.2·§4.3) — BE 07/17 제안(reason=SSE)에 대한 역제안. 경로 B 일관·FE join 불필요, SSE(`products.ready`)는 상관키만. `listId` TTL 10분(config). BE 확정 🔴. (3) **Q4 초안**(§4.8): 커서 "수정시각+id" 수용(불투명 취급)·`attributes` 자유 dict·리뷰 텍스트 MVP 제외. 🔴 선결: I-17 배치 MVP/post-MVP 스코프(config vs 파이프라인 모순). §5.1 Q2/Q4/Q6 갱신. |
| v0.15.1 | 2026-07-18 | **I-13 행동 이벤트 조회/집계 본문 확정 — LLM팀 재작성 + BE Notion 반영.** BE 확인질문 Q5 해소: I-13(`GET /internal/seller/{brandId}/events`) 페이지에 I-5(문의 접수) 내용이 복붙돼 있던 것을 LLM팀이 `behavior_events` 기반 스펙으로 재작성해 **Notion 페이지에 직접 반영**. 요청(`from`/`to`·`eventType` 4종·`productId`·`groupBy`) + 응답 3형태(product/eventType/date, `counts`·`viewToCartRate`·`uniqueVisitors`) + 집계 규칙(판매자 스코프=`product→brand.seller_id`, camelCase, `client_event_id` 중복 배제, purchaseComplete는 행동 맥락용·매출 권위는 I-6/I-14). §4.4 I-13 행·§5.1 Q5·C-13 갱신. 판매자 집계 나머지 4종 응답 스키마는 C-13 잔여. |
| v0.15.0 | 2026-07-17 | **BE 07/17 API·ERD 개정 반영(확정분) — Notion "📡 API 명세서" 실측 + "API·ERD 변경 정리(07/17)".** (1) **I-21 `POST /internal/recommendations` 신설**(§4.2 재작성, C-9 확정) — 추천 목록 push가 `{sessionId, listId, productIds[Top5]}`로 확정. **`listId`는 FastAPI 생성**(구 Spring 생성 가정 폐기), `reason`은 SSE 직접(콜백 불포함), **콜백 성공 후에만 `products.ready`**. 구 groups/items/reason 구조 폐기. (2) **CH-5 `GET /api/chat/lists/{listId}` 신설**(§4.3 재작성, C-12) — 추천 카드 조회(구 P-7 대체), FE↔Spring, 카드 스키마 FE/LLM OPEN. (3) **I-19 본문 재작성**(§4.7) — camelCase·숫자 id·`shippingFee` 항상 0·`representativeStatus` enum 8종·item `status` 6종(교환 제거)·**`category` 필드 없음**(dedup은 `productId` 기준). (4) **CH-2 경로 = `{AI_SERVER}/chat`**(오타 수정), **I-20 = `{AI_SERVER}/events/session-end`**(Spring→AI inbound — 우리가 호스팅하는 엔드포인트임을 명확화). (5) **[BE DB] `productId` = 숫자(BIGINT) 확정** — internal(AI↔Spring) 계약은 숫자, SSE/FE 경계서 문자열 정규화(§2.6 개정). (6) **[BE DB/ERD] `product` +`stock_quantity`(시드 100)** — I-2 `OUT_OF_STOCK` 실재화(§4.1), `user_event`→`behavior_events`(+guest_id +client_event_id), `order_status_logs`·`product_change_logs`·`account_event_logs` 신설, **배송비 0원·교환 제거(주문상태 11→9종)**. (7) 신규 FE/Spring 엔드포인트 **E-1**(`POST /api/events` 행동 수집)·**CH-6**(`POST /api/chat/seller/sessions`)·**S-5**(`PATCH /api/seller/products/{id}` 판매자 직접 수정 — 챗봇 I-11과 병존) 등재. (8) **LLM팀 확인질문 9개**(§5 말미 표) — 세션 UUID 수용·I-21 확정·[적용] 형식·I-17 커서·I-13 재작성·CS챗 라우팅·게스트 담기·판매자챗 주소·담기 이벤트. |
| v0.14.0 | 2026-07-16 | **구매 이력 = I-19, 세션 종료 = I-20 — BE DB No. 채번 확정(사용자 승인 후 Notion 수정).** (1) **구매 이력 조회 = I-19 `GET /internal/members/{id}/orders`**(BE DB "구매 이력 목록" 행에 No.·그룹·Method·경로 채움) — §4.7·C-6·레인 c 정합, 구 `/orders/recent` 제안 폐기. `{id}`=userId(AI 도출), 서비스 토큰. I-4(주문 상태 요약)와 별개. (2) **세션 종료 = I-20 `POST /events/session-end`**(BE DB 행에 No. 채움) — §3.5·C-8 정합. Notion BE DB 실제 수정(사용자 승인). |
| v0.13.0 | 2026-07-16 | **BE API 명세 DB(Notion) 실측 정합 — 인증 레인 서비스 토큰 통일 + 실제 I-number/경로 — 사용자 확정.** (1) **[BREAKING] AI→Spring 역호출 전 구간 `X-Internal-Token` 서비스 토큰 + 본문/쿼리 신원**(AI가 JWT `sub` 도출)으로 통일 — 구 "사용자/판매자 JWT 포워딩"(후보 검색·구매 이력) 폐기. BE `internal` 그룹이 전부 서비스 토큰이라 정합(IDOR는 AI 도출 신원으로 유지). (2) **실제 BE 번호/경로 반영**: 후보 검색 = **I-1 `GET /internal/products/search`**(구 `POST /products/search`), 생성물 배치 = **I-17 `GET /internal/products/changes`**, 장바구니 조회 = **I-18 `GET /internal/cart`**. (3) **구매자 챗 경로 = `POST /ai/chat`**(구 `/chat`, BE DB 실측·인증 필요). (4) **S-3 정정**: `GET /api/seller/products`(SELLER·FE용) ∥ I-9(internal·AI용) **별개** — 구 "S-3=I-9" 오기 정정. (5) **후보 검색 GET vs POST 역제안 🔴**: 복잡 필터(배열·중첩)라 POST 바디 역제안. (6) **C-6 구매 이력**: BE DB 미등재 → AI팀 신규 요청 필요(I-4는 주문 상태 요약). C-4/C-15/C-16 = I-17/I-1/I-18로 확정. |
| v0.12.0 | 2026-07-16 | **CH-1 스트림 티켓 발급 + 재발급 경로 명시 — 사용자 확정.** 스트림 티켓 발급을 전제 계약(§1.2 레인 d)에 구체화: (1) **CH-1**(`POST /api/chat/sessions`) 응답에 `sessionId`(10분 sliding) + **첫 `streamTicket`**(RS256, TTL 30~60s) 반환. 신원은 회원 AT/게스트 쿠키로 확인(`sub_type`). (2) **[중요] 티켓 재발급 경로 신설 필요(CH-1b `POST /api/chat/tickets` 제안)** — 티켓 TTL(30~60s) ≪ 세션 TTL(10분)이라 CH-1 1회로는 첫 스트림만 커버, 2번째 메시지부터는 세션 유지한 채 티켓만 재발급해야 하며 **CH-1 재호출은 새 세션(맥락 단절)이라 못 씀**. (3) `401` 재발급 흐름을 CH-1b로 명확화. (4) 레인 d에서 구 "draft 적용=FE S-3 PATCH" 제거(v0.11.0 정합). CH-1/CH-1b는 Spring 소유 — 경로·응답 🔴 C-1. |
| v0.11.0 | 2026-07-16 | **판매자 쓰기 모델·HITL 계약 확정(쟁점 B) — 사용자 확정.** (1) **채팅 경로 쓰기 = AI가 internal API(I-10/11/12) 직접 수행 + HITL 승인 게이트**(v0.9.0 확정). 판매자가 FE에서 직접 편집하는 경로는 FE↔Spring 별개(AI 표면 밖). (2) **`S-3` = 자사 상품 목록 조회(=I-9, 읽기)** 로 명확화 — 구 S-4 문서의 "S-3 PATCH"는 오표기, brandId=JWT 클레임(userId와 동일 IDOR 원칙, 쟁점 A 확정). (3) **HITL 2-스트림 계약**: 스트림1 `draft{draftId,op,changes}` → LangGraph interrupt → done / 스트림2 `confirm{draftId}` → resume → I-11 등 실행 → done. (4) **HITL 안전장치 5종**: draftId 바인딩·명시 액션만·멱등성·Spring 소유권 하드게이트·대기 TTL. 삭제 필수 HITL + soft delete(HIDDEN). (5) `draft` 이벤트에 `draftId`·`op` 추가. 승인 이벤트명·confirm 형식은 🔴 판매자 SPEC. |
| v0.10.0 | 2026-07-16 | **SSE 인증 = 스트림 단명 티켓("JWKS 검토 후 제안" 채택) — 사용자 확정.** SSE에 로그인 AT를 직접 싣지 않고, Spring이 채팅 진입 시 신원 확인 후 **스트림 단위 단명 JWT(RS256, TTL 30~60초)** 를 발급(CH-1에 얹음). 게스트는 `guest_id` 쿠키로 동일 발급(`sub_type: guest`). 클레임 재편: `sub`+`sub_type`(member/guest)+`iss`(jarvis-spring-auth)+`aud`(jarvis-fastapi-ai)+`scope`(chat:stream)+`exp`. 검증에 **`aud`·`scope` 추가**(토큰 혼용 방지). JWKS는 `kid` miss 시 refetch. 판매자 티켓의 role/brandId 표현은 🔴 확인. §2.3·§2.5·C-1 개정. (JWKS 코어(RS256/kid/엔드포인트)는 기존 반영분 유지.) |
| v0.9.0 | 2026-07-16 | **판매자 BE internal API 배치 전면 반영(11종 PDF) — 사용자 확정.** (1) **판매자 조회/집계 7종**(§4.4): I-6 sales·I-7 funnel·I-13 events·I-16 churn·I-14 order-events·I-15 product-changes(brandId path) + I-8 account-events(전역). (2) **판매자 상품 CRUD 4종**(§4.5, C-14 재정의): I-9 목록·I-10 등록·I-11 수정·I-12 삭제(soft=HIDDEN). 구 "I-7 상세 읽기 + FE S-3 PATCH" **폐기**. (3) **[BREAKING] 판매자 쓰기 모델 전환**(§3.2): "FE가 본인 JWT로 S-3 PATCH 반영(AI 표면 밖)" → **"AI(`product_agent`)가 Spring internal API로 직접 쓰기 + 파괴적 작업은 HITL interrupt/resume 승인 게이트"**. "대화 발화 ≠ 동의" 원칙은 유지(HITL로 구현). soft delete(status=HIDDEN). (4) 전부 `internal`·`X-Internal-Token`·`{brandId}`(JWT 클레임). (5) **혼동 주의**: BE I-15 product-changes ≠ C-4 products/changes(생성물 배치), BE I-14 order-events ≠ C-6 orders/recent(구매자 이력). (6) 판매자 서브에이전트 다수화(sales_anomaly·conversion·behavior·churn·abuse·general·recommend·chart·product). **결정 20 개정 필요**(§8 항목 8). 응답 스키마·I-number 정합·HITL 이벤트는 #9. |
| v0.8.0 | 2026-07-16 | **판매자 `brandId` = JWT 클레임 + 집계 API 5종(BE 문서) — 사용자 확정.** (1) **§2.3 클레임에 `brandId` 추가**(role=seller 시 필수). (2) **§2.6 `brandId 미보유` 원칙 개정** — "AI는 brandId를 알지 못한다(Spring 내부 해소)"에서 **"brandId를 요청 본문에서 받지 않는다 — 검증된 판매자 JWT 클레임에서만 획득"** 으로 완화(IDOR 방지 취지 유지, RS256 위조 불가). BE 집계 API가 `{brandId}` path를 요구함에 따른 정합. (3) **§4.4 재정의** — 판매자 집계는 단일 `/seller/aggregates`(폐기)가 아니라 **brandId 스코프 집계 5종**: I-6 `sales`·I-7 `funnel`·I-13 `events`·I-16 `churn`(brandId path) + I-8 `account-events`(전역·admin). 전부 `internal`·`X-Internal-Token`. (4) **C-13 재정의**(5종), **C-1**에 brandId 클레임 발급 협의 추가. (5) **BE I-number ≠ 기존 임의 I-number**(BE I-7=funnel vs 기존 I-7=상세) — 정합은 #9. 상품 CRUD·주문 PDF 반영은 후속. |
| v0.7.0 | 2026-07-15 | **스트림 운영 규약 신설 — 사용자 확정(7개 항목).** (1) **§2.9 신설**: 동시 스트림 세션당 1개(`409 STREAM_IN_PROGRESS`, 기존 스트림 유지 — 409 거절안 채택), 취소 = 연결 종료(FE `AbortController` → disconnect 감지 → **LLM 스트림 즉시 close**·LangGraph task 취소), 타임아웃 기준표(first-token 10s / 스트림 상한 90s / AI→Spring 3s 통일 / LLM 30s+1재시도 — config 기본값, 계약은 초과 시 동작). (2) **§2.5 확장**: `409`·`504 UPSTREAM_TIMEOUT` 추가 — 스트림 전 오류 통합표化. (3) **§2.8 레이트 리밋 확정**: 목적 = 무분별 남용 차단, FastAPI 미들웨어 + in-memory(다중 인스턴스 시 Redis 이관 단서), 분당 10/시간당 100(config), **C-11 축소**(잔여 = 허용 오리진만). (4) **§6.3 신설(운영 요구)**: 대화 저장(수신 즉시 user 저장 / 완료 후 assistant 저장 / `COMPLETED`·`FAILED`·`CANCELLED`, 부분 텍스트 보존), 로그 필드(requestId·userId·conversationId·first-token/total latency 분리·model·tokens·errorType, message 원문 로깅 금지). FE 히스토리 복원 API는 미결로 등재. |
| v0.6.0 | 2026-07-15 | **[BREAKING] 장바구니 계약 BE I-2 문서 채택 + 조회 신설 — 사용자 확정.** (1) **§4.1 재작성**: `POST /internal/cart/items` 단건 + `X-Internal-Token` 서비스 토큰 + 본문 신원(AI-검증 JWT `sub` 유래) — 구 v0.3.0 제안(사용자 JWT 포워딩·`items[]` 다건) **폐기**, 묶음은 반복 호출. (2) **게스트 담기 허용**(BE 02 D30) — AI-side 차단·`GUEST_NOT_ALLOWED` 폐기, 로그인 유도는 결제 시점 FE 몫. **결정 8 개정 필요(§8 항목 7)**. (3) **옵션 되물음 멀티턴**: `400 CART_OPTION_REQUIRED`(options 목록 포함) → 실패 `action` 없이 `token` 재질문 → `optionId` 해석 후 재담기; `CART_OPTION_INVALID`는 1회 재시도 후 `CART_ERROR`. (4) **`action.reason` 재편**: `PRODUCT_NOT_FOUND`/`CART_ERROR`/`OUT_OF_STOCK`(I-2에 재고 코드 부재 — 🔴 협의). (5) **장바구니 조회 신설(§4.9, C-16)**: `GET /internal/cart` — 장바구니 질의 응답(`token` 텍스트) + 담기 시 기존 보유·수량 합산 안내(합산 권위는 Spring, 조회 실패 시 담기 진행). (6) 레인 (c) 6건→7건. C-3 재작성. |
| v0.5.1 | 2026-07-15 | **[정정] AI 생성물 저장 존속 + pull 배치 부활 — 용어 오해 정정.** v0.5.0의 "enrichment/임베딩 채택 안 함"은 오독이었음 — 채택하지 않는 것은 **상품 원본 컬럼의 AI측 사본**뿐. **AI 생성물(extras·search_doc·임베딩 벡터)은 AI Postgres에 저장·유지**(결정 3 Layer 2/3·결정 6 존속), 갱신은 **pull 배치**(`GET /products/changes?since={cursor}`, §4.8 신설, **C-4 부활**; Spring 주기 push 기각). **질의 시점 후보 흐름은 OPEN**(§4.8 말미) — 방식 1(AI 벡터 → Spring id 제약 조회) vs 방식 2(Spring 검색 → 임베딩 보조) 병행 검토, hk-final `SearchBackend`로 교체 가능 구현 후 골든셋/실측 확정. §8 항목 4 정정(결정 3/6 효력 유지). |
| v0.5.0 | 2026-07-15 | **[BREAKING] 검색 위임 영구 확정 + 주문 알림 폐기 — 사용자 최종 확정.** (1) **후보 검색 = 질의 시점 Spring 위임(`POST /products/search`, §4.6, C-15 신설)** 을 유일·영구 경로로 확정 — AI 카탈로그 사본(미러)·pgvector 카탈로그 벡터 검색·enrichment/임베딩·bulk export 배치(이원 주기)는 **채택하지 않음**(고도화 유예 아님, C-4 폐기). 구현 기준 = `~/projet/hk-final`(jarvis-ai) 스캐폴드. (2) **주문 알림(구 `POST /events/order`)·주문 미러 폐기** → **질의 시점 구매 이력 조회(`GET /internal/members/{id}/orders`, §4.7, C-6 재정의)** — dedup(14-F 동작 불변: exact 제외·소모품 억제·되돌리기 칩)·프로필 구매 소스 공용, 게스트 스킵, 실패 시 dedup 없이 degrade. (3) Spring → AI 이벤트는 **`/events/session-end` 1종만** MVP 유지(병행 PRD 라인과의 유일한 차이 — PRD 정정 필요, §8 항목 6). (4) §1.2 레인 재편 — AI→Spring 질의 시점 6건. 가격 신선도 트레이드오프 소멸(질의 시점 검색·조회). SPEC 후속: RECOMMEND-001 검색 tool·CATALOG-DATA-001 재범위·PROFILE-001 구매 소스(§7). |
| v0.4.0 | 2026-07-15 | **판매자 확대(Batch 1) + 카탈로그 배치 전환(Batch 2), 2026-07-15 사용자 확정.** **(Batch 1)** `POST /seller/chat` 범위 확대 — 통계 Q&A **+ 상세 수정 draft 흐름**(§3.2). 판매자 SSE = `token`/`draft`/`done`/`error`만, `finishReason`=`stop` 단일. 통계 원천 = **Spring 집계 I-6 질의 시점 콜백**(§4.4) → **C-7 해소**, 구 결정 20 기본안(주문 미러 sellerId·금액 확장) **폐기**. draft = **I-7 상세 읽기**(§4.5) → LLM 개정안 → SSE `draft`{productId(string), changes:[{field,before,after}]} → FE diff 카드 → FE가 Spring **S-3 PATCH**로 반영(FE↔Spring 전제, AI 표면 밖). 대화 발화는 동의 아님. `brandId`는 AI 미보유(Spring이 sellerId→brandId 해소). 신규 역호출 I-6/I-7 인증 = 판매자 JWT 포워딩 제안(🔴). §1.2 레인 갱신(AI→Spring 질의 시점 = 장바구니·목록 push·I-6·I-7). **(Batch 2)** `POST /events/catalog` **완전 폐기** → **`GET /products/changes?since={cursor}` bulk export 배치 폴링**(§4.6, 제안 🔴). **이원 주기**(가격·재고 짧은 주기 미러 UPDATE / 콘텐츠 긴 주기 재임베딩, contentHash 비교) — 결정 9-A 경량/전체 분기를 이벤트→배치로 이식. 배치가 곧 동기화(일 1회 보정이 백업 아님). 신선도 트레이드오프(필터 경계 오류) 수용 명시. **`/events/order`·`/events/session-end`는 MVP 유지**(이벤트 폐기는 카탈로그 한정). **(C-table)** C-4 재정의(bulk export 🔴), C-6 4필드 유지 확정, C-7 해소(I-6), C-13 I-6 신설(🔴), C-14 I-7 신설(🔴), 나머지 v0.3.0 유지. **[provenance]** 본 버전은 **미비준 병렬 초안**(no-mirror + 질의 시점 `POST /products/search` + `/events/*` 고도화 유예 + 판매자 AI DB 시드; 가칭 결정 22/23/24)을 **폐기·대체**한다 — 비준 노선은 **미러 + 배치 동기화**(본 세션). SPEC 동기화 개정 목록은 §7. |
| v0.3.0 | 2026-07-15 | **[BREAKING] FE/BE 팀 챗 API 문서("추천 챗봇 CH-2")를 명명 기준으로 채택 + 상품 목록 경로 B 도입.** (1) SSE 이벤트 집합 재편(`text.delta`→`token`, `products` 카드 삭제, `conditions`/`action`/`products.ready` 신설, `done.finishReason`=`stop`/`zero_result`, in-stream `error` 4종). 전 페이로드 **camelCase**. (2) 경로 B: SSE 상품 카드 제거, AI→Spring 목록 push(C-9) + FE←Spring 목록 GET(C-12), point 조회 삭제. (3) 인증 확정(RS256+JWKS, `401` 통일, `role`, `CHAT_SESSION_EXPIRED` 폐기). (4) `productId` 문자열 전면 통일(숫자 예시와 상충 → 양팀 통보). (5) 장바구니 `action` + JWT 포워딩 + 실패 4종. (6) suggestions/relaxationNotice/budget SSE 측 탑재. SPEC-RECOMMEND-001 §5.3 / SPEC-PROFILE-001 §5.4 동기화 개정 필요(§7). |
| v0.2.0 | 2026-07-14 | [BREAKING] FE 직접 호출 아키텍처 반영. 사용자 대면 API를 FE → AI 직접 호출로 전환. 요청 본문에서 `user_id`·`seller_id` 제거 — 토큰 클레임 추출. 인증 2종 분리, 401 만료 재발급·SSE 연결 시점 인증, CORS·레이트 리밋 신설, `GET /profile/{user_id}` → `GET /profile/me` IDOR 방지. |
| v0.1.0 | 2026-07-14 | 최초 작성. `/chat`(SSE)·`/seller/chat`(최소판)·`GET /profile/{user_id}`·`/events/{catalog,session-end,order}` 제공 API와 장바구니·point 조회 요구 계약 정의. 🔴 항목 10건(C-1~C-10) 등록. |

### 6.3 운영 요구 — 대화 저장·로그/모니터링 (AI 서버 내부) [v0.7.0 신설]

외부 계약이 아닌 **AI 서버 내부 운영 요구**다(FE/Spring 협의 불필요). 2026-07-15 사용자 확정. PRD·소유 SPEC에 비기능 요구로 편입한다.

#### (a) 대화 저장 규약

저장소 = LangGraph checkpointer(AI Postgres, **`threadId` = checkpointer thread 키** — **[개정 v0.16.0]** 축 분리 전에는 `sessionId`가 thread 키였다).

**[v0.16.0] `conversation_turns`는 session-primary + thread 병기** — 턴 행은 `conversation_id = sessionId`(primary)로 적재하고 `thread_id = threadId`를 **함께** 남긴다. 프로필 파이프라인의 세션 종료 스캔은 `conversation_id` 축이라야 한 접속의 여러 방 발화를 한 버퍼로 모을 수 있고(§2.6), 방별 조회·정리는 `thread_id` 축이 필요하다. 어느 한쪽만으로는 두 요구를 동시에 만족할 수 없다.

| 시점 | 저장 대상 | 상태 |
|---|---|---|
| 사용자 메시지 **수신 즉시** | user 메시지 원문 | — |
| 스트리밍 **완료 후** | assistant 응답 전문 | `COMPLETED` |
| 스트림 실패(in-stream `error`·LLM 재시도 소진) | 부분 생성 텍스트 | `FAILED` |
| 클라이언트 취소(§2.9 b) | **부분 생성 텍스트 보존** | `CANCELLED` |

- `FAILED`/`CANCELLED`의 부분 텍스트도 다음 턴 컨텍스트·프로필 스캔(결정 4-A sleep-time)에 포함한다.
- FE 채팅 히스토리 복원(`GET /chat/history` 류)은 **미결** — 지원 결정 시 이 저장소를 원천으로 계약 신설.

#### (b) 로그/모니터링 필드 (요청 단위 구조화 로그)

| 필드 | 비고 |
|---|---|
| `requestId` | §2.5 오류 봉투와 동일 키 — 전 구간 상관관계 |
| `ownerFp` / `role` | JWT `sub`의 peppered HMAC 지문과 역할. raw `userId`/`guestId` 금지 |
| `sessionFp` | `sessionId`(접속)의 peppered HMAC 지문 |
| `threadFp` | `threadId`(방)의 peppered HMAC 지문 |
| `streamFp` | 내부 `owner:thread` stream key의 peppered HMAC 지문(수명주기 로그) |
| `scopeFp` / `scopeType` / `ipFp` | 429 스코프와 IP의 peppered HMAC 지문 및 비민감 유형(`sub`/`ip`). raw scope/IP 금지 |
| `latencyFirstToken` / `latencyTotal` | SSE 2분할 — 체감 응답성 vs 전체 시간(§2.9 c 기준 대비) |
| `model` | 호출 모델 id(Haiku/Sonnet, 노드별 다중 기록) |
| `promptTokens` / `completionTokens` | LLM 호출별 합산 |
| `errorType` | in-stream `error` 코드·`FAILED` 사유·타임아웃 구간 |
| `streamStatus` | `COMPLETED` / `FAILED` / `CANCELLED` (a와 동일 enum) |

- **PII/식별자 정책**: 사용자 message 원문과 raw owner/session/thread/stream 식별자는
  **로그에 남기지 않는다**. message는 길이·peppered HMAC, 식별자는 위 `*Fp`만 기록한다.
  rejection 로그의 추가 필드는 명시 allowlist만 허용하며 Authorization/token/exception과
  사용자 입력 원문은 폐기한다. 원문은 (a) 대화 저장소에만 존재한다.
- 레이트 리밋(§2.8)·409(§2.9 a) 발동도 `errorType`으로 집계해 상한값 튜닝 근거로 쓴다.

---

## 7. 후속 SPEC 동기화 개정 목록 (Follow-up SPEC Amendments)

본 개정의 명명 기준 채택·경로 B·판매자 확대로 아래 SPEC들이 **정렬이 깨졌다.** 본 문서는 SPEC을 편집하지 않으며, 아래를 후속 동기화 개정(sync amendment) 대상으로 등록한다.

### 7.1 `SPEC-RECOMMEND-001` §5.3 (SSE 페이로드 스키마) — 개정 범위

- **이벤트명 교체**: `text.delta` → `token`; `products`(SSE 카드) → **삭제**(경로 B로 이관, `products.ready` 신호만 SSE).
- **필드 camelCase 전환**: `finish_reason`→`finishReason`, `product_id`→`productId`, `verified_sum`/`within_budget`/`dropped_items`/`feasibility_notice`→camelCase, `est_count`→`estCount` 등 전부.
- **`done.finishReason` 값**: `completed`→`stop`, `zero_result` 유지.
- **`error.code` 집합 교체**: `DECOMPOSE_FAILED`/`RERANK_FAILED` 등 스테이지 코드 → `LLM_TIMEOUT`/`LLM_UNAVAILABLE`/`SEARCH_FAILED`/`INTERNAL`(rerank 실패는 여전히 `done` degrade).
- **`ProductPayload` 이관**: `products` 카드 스키마는 SSE에서 제거되고, `productId`+`rank`+`reason`만 목록 push(§4.2)로 이관, 표시 필드는 Spring enrich(§4.3). EX-5/AC-REC-10 정신 유지·강화.
- **AC 갱신**: 이벤트 순서 `text.delta→products→done` → `token→products.ready→done`; "`products` 정확히 1회" → "`products.ready` 정확히 1회".
- **주의**: 서브그래프 동작·불변식(decompose 1회, rerank 상한, 하드 제약, degrade 정책)은 불변.

### 7.2 `SPEC-PROFILE-001` §5.4/§6.9 (`GET /profile/me`) — 개정 범위

- **경로**: `GET /profile/{user_id}` → `GET /profile/me`(IDOR 방지, 결정 19).
- **필드 camelCase**: `ProfileViewResponse` `user_id`/`generated_at` → `userId`/`generatedAt`. `exists`/`markdown` 유지.
- **구매 소스 개정 [v0.5.0]**: write 소스 "주문 이벤트 미러 스캔"(결정 16) → **질의 시점 구매 이력 조회(`GET /internal/members/{id}/orders`, §4.7)** 호출로 교체(sleep-time 배치). 게이트·델타 동작은 불변.
- **응답 구조·동작 불변**: 위 항목 외 스키마·REQ(PROF-081 등) 변경 없음.

### 7.3 이벤트 채널 SPEC 정합

- `/events/session-end` HTTP 계약은 본 문서 소유(결정 21). 개정 시 소비 SPEC의 필드명(camelCase) 전제와 정합을 확인한다. **수신 후 동작**은 소비 SPEC 소관 불변. (주문 알림은 v0.5.0에서 미채택 — §3.6·§4.7.)
- **카탈로그 동기화 참조 정합 [v0.5.0]**: 카탈로그 변경 이벤트·배치 동기화·AI 사본이 모두 채택되지 않으므로(§4.6 말미), 카탈로그 동기화·3계층 메타데이터·임베딩을 참조하는 SPEC-RECOMMEND-001(검색 tool을 pgvector 단일 SQL로 규정한 조항)·SPEC-CATALOG-DATA-001(enrichment→임베딩→적재 단계) 문구는 **질의 시점 Spring 위임(§4.6)에 맞춰 후속 개정/재범위**가 필요하다(§8 항목 4 연계).

---

## 8. product.md §12-A 결정과의 정합 — 사용자 확인 필요 항목 (결정 개정 필요 목록)

본 개정은 product.md의 여러 binding 결정과 긴장/상충한다. **product.md는 편집하지 않으며**, 아래를 **결정 개정 필요 항목(신규/개정 결정 레코드 대상)** 으로 등록한다. product.md 결정 로그는 현재 **결정 21**까지 있으며, 본 문서가 한때 참조한 결정 22/23/24는 **미비준 병렬 초안 소산으로 폐기**되었다(§6.2). 아래 8항목이 실제 필요한 개정이다.

### 항목 1 (상충 — 신규 결정 레코드 필요) — 경로 B: point 조회 폐기 + AI→Spring 역방향 예외 증가

결정 9-B(binding)의 "Spring 유일 접촉 = point 조회" 구체 문구와 경로 B(목록 push + 목록 GET)가 상충한다. 원칙(표시 권위 = Spring, AI 인덱스 표시 필드 미보유)은 유지·강화되나, 단방향 원칙의 AI→Spring 역방향 예외가 **장바구니 1건 → 장바구니·목록 push·I-6·I-7 4건**으로 증가한다. **경로 B + 판매자 역호출에 대한 product.md 신규 결정 레코드가 필요**하다. 사용자 확인·PRD 반영 요망.

### 항목 2 (긴장 — 정책 확인) — verifiedSum(검색 응답가) vs Spring enrich 표시가 괴리

BudgetSummary `verifiedSum`은 §4.6 검색 응답 가격 기준 결정론 합산(결정 14-A 원칙 유지)인데, 경로 B에서 실제 표시가는 Spring이 목록 GET 시점에 다시 채운다(§4.3). 검색~표시 사이 가격 변경 시 SSE `budget`과 우측 패널 표시가가 순간 괴리할 수 있다. **예산 표시 UX 정책**은 🔴 기획·Spring 협의 필요. (기존 OPEN-11 연장 — 질의 시점 검색이라 괴리 창은 크게 축소됨.)

### 항목 3 (개정 — 결정 20 확대) — 판매자 agent 상세 수정 draft 흐름 MVP 편입 + 데이터 소스 I-6 전환

결정 20(binding)은 판매자 MVP를 "매출/판매 통계 Q&A만"으로 한정하고, 데이터 소스 기본안을 **주문 미러 확장(`sellerId`·금액)** 으로 두었다(product.md line 134·695). 본 개정은 (1) **상세 수정 draft 흐름을 MVP로 확대**하고, (2) 데이터 소스를 **주문 미러 확장에서 I-6 집계 콜백으로 전환**한다(주문 미러 자체가 폐기됨 — 항목 5). **결정 20 개정 레코드가 필요**하다. C-7은 이로써 해소되나 I-6 계약 세부(C-13)는 협의 잔존.

### 항목 4 (개정 — 결정 9/9-A/9-B) — 상품 컬럼 사본·이벤트 동기화 폐기, AI 생성물 pull 배치로 대체 [v0.5.1 정정]

결정 9/9-A/9-B(binding)는 "필터 컬럼 최소 미러 + 이벤트 기반 준실시간 동기화"를 확정했으나(product.md line 132·310·325·340), 2026-07-15 사용자 최종 확정으로 **상품 원본 컬럼의 AI측 사본과 이벤트(웹훅) 동기화를 폐기**한다. **AI 생성물(extras·search_doc·임베딩)은 존속** — 결정 3의 Layer 2/3·결정 6(임베딩 모델)은 **유효**하며(2026-07-20 모델을 **Google `gemini-embedding-001`**(dim 1536, MRL) 로 개정 — 셀프호스트 torch 폐기), 갱신만 **pull 배치(bulk export, §4.8)** 로 바뀐다. 질의 시점 후보 흐름(AI 벡터 검색 ↔ Spring 검색 결합)은 OPEN(§4.8 말미) — 확정 시 결정 레코드에 포함. **결정 9/9-A/9-B 개정(사본·이벤트 폐기 + pull 배치) 신규 결정 레코드가 필요**하다. SPEC-CATALOG-DATA-001의 enrichment→임베딩 단계는 §4.8 배치와 통합 재범위.

### 항목 5 (개정 — 결정 14-F/16 구현 방식) — 주문 이벤트 미러 → 질의 시점 구매 이력 조회

결정 14-F(dedup)·결정 16(프로필 구매 소스)은 "주문 이벤트 → AI 경량 미러" 구현을 전제했으나, 주문 알림·미러를 폐기하고 **추천 직전/sleep-time의 질의 시점 조회(`GET /internal/members/{id}/orders`, §4.7)** 로 대체한다(2026-07-15 확정). **동작 요구(exact 제외·소모품 억제·되돌리기 칩·구매 신호 델타)는 불변** — 데이터 획득 방식 개정 레코드 필요. SPEC-PROFILE-001 구매 소스 문구 개정(§7.2).

### 항목 6 (정정 — 병행 PRD) — events scope

병행 PRD 초안(docs/PRD.md v1.1.0)은 이벤트 채널 전부를 고도화로 옮겼으나, 확정안은 **`/events/session-end`와 `/events/session-claim`을 MVP에 유지**한다(주문 알림은 미채택으로 정리됨). PRD의 events-scope와 일정표(7/15 행의 "하이브리드 통합" 표현 포함)를 본 문서 v0.5.0 기준으로 정정해야 한다.

### 항목 7 (개정 — 결정 8) — 게스트 장바구니 담기 허용 [v0.6.0]

결정 8(binding)은 "장바구니 담기·구매는 회원 전용"으로 확정했으나, BE 팀 I-2 문서(02 D30, 2026-07-10 개정)가 **게스트(guestId) 담기 성공**을 확정했고 2026-07-15 사용자가 이를 채택했다(§4.1). **개정 범위**: (1) 장바구니 담기는 게스트 허용(로그인 유도는 결제 시점 FE 몫), (2) 구매는 계속 회원 전용, (3) 검색/추천 무제한·개인화 미적용·AI 서버 무상태 원칙은 불변. 구 AI-side 게스트 차단(`GUEST_NOT_ALLOWED`)은 폐기. **결정 8 개정 레코드가 필요**하다. 아울러 결정 7의 구현 세부(인증)는 "사용자 JWT 포워딩"이 아닌 **I-2 서비스 토큰 + 본문 신원(AI-검증 JWT `sub` 유래)** 으로 확정됨 — 결정 7 자체(경로: AI→Spring API 위임)는 불변이므로 별도 개정 불요, C-3 세부로 처리.

### 항목 8 (개정 — 결정 20) — 판매자 모델 전면 확대 + 쓰기 모델 전환 [v0.9.0]

BE internal API 배치(11종 PDF, 2026-07-16) 반영으로 결정 20(판매자 MVP = 통계 Q&A + draft)이 크게 확대된다. **개정 범위**: (1) 판매자 그래프가 **서브에이전트 다수**(sales_anomaly·conversion·behavior·churn·abuse·general·recommend·chart·`product_agent`)로 구성되고 조회/집계 7종 + 상품 CRUD 4종을 소비. (2) **쓰기 모델 전환** — 구 "AI는 제안만, 반영은 FE S-3 PATCH(AI 표면 밖)"에서 **"AI가 Spring internal API로 직접 쓰기(등록/수정/삭제) + 파괴적 작업은 HITL interrupt/resume 승인"**. "대화 발화 ≠ 동의" 원칙은 HITL로 유지. (3) `brandId`=JWT 클레임(항목 없던 신규). (4) 전역 I-8(account-events)은 admin 소유 협의. **결정 20 개정 + 신규 결정 레코드(판매자 쓰기·HITL) 필요**. 판매자 SPEC 신규 작성 필요.

> 위 8항목 외 인증(결정 19)·식별자(결정 19)는 결정과 정합하며 별도 사용자 확인 불필요. ~~장바구니 JWT 포워딩(결정 7·19) 정합~~은 v0.6.0에서 I-2 서비스 토큰 방식으로 대체되었다(항목 7 말미).

---

*문서 끝.*
