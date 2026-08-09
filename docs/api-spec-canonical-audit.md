# API 명세 정본 대조 감사 — #472

> 정본 대조 최종 시점: 2026-08-09
>
> 대조 방법: Notion MCP 직접 조회
> 기준: /home/uuser/inte-final/_audit-472/canonical/00-INDEX-and-scope.md 및 각 발췌의 최종수정일

## 범위와 판정 기준

정본 인덱스는 2026-08-09 정정으로 총 98행을 범위 안 46건 + 범위 밖 52건으로 확정했다. 최초 95행/44건 표기는 산술 오류였으며, 누락 방지를 위해 아래 표는 정정된 46개 식별자를 전부 대조했다.

- ① 사본이 낡음(동기화): Notion 정본은 확정됐고 저장소 사본만 갱신하면 된다.
- ② 코드 변경까지 필요: 정본 계약과 현 코드/스키마가 달라 문서만 고치면 거짓 정합이 된다.
- ③ 정본이 우리에게 물어본 미해결(우리가 답할 것): 정본의 결정을 기다리거나 답변을 먼저 제공해야 한다.

| No. | 엔드포인트 | 사본 § | 정본 최종수정 | 판정 | 차이 요약 | 갈래 | 조치 |
|---:|---|---|---|---|---|---|---|
| 1 | I-1 GET /internal/products/search | §4.6 | 2026-08-04 | 드리프트 | review 0건 규약·실패 응답표·실패 아님 표가 사본에 누락; brandName 다중은 OR, 부분매칭만 정본 질문 | ① 사본이 낡음(동기화) + ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | §4.6 정본 표/문구 반영 후 부분매칭 답변 |
| 2 | I-2 POST /internal/cart/items | §4.1 | 2026-08-06 | 드리프트 | chatSessionId 및 quantity 응답, add_to_cart server-side 적재 미소비 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.1 반영, 요청/응답 스키마 후속 |
| 3 | I-3 GET /internal/products/popular | §4.17 | 2026-07-31 | 드리프트 | I-1 DTO를 참조하지만 정본 I-3 예시가 구형 | ② 코드 변경까지 필요 | Notion I-3 예시를 I-1 DTO와 통일 |
| 4 | I-4 GET /internal/members/{id}/orders/status | §4.10 | 2026-07-31 | 드리프트 | representativeStatus 한국어 문자열이 I-19 enum과 충돌 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | 경로/계약 정본 반영, enum 후속 |
| 5 | I-6 GET /internal/seller/{brandId}/sales | §4.4 | 2026-08-06 | 드리프트 | salesCount(판매 수량, SUM(oi.quantity)) 응답 누락 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| 6 | I-7 GET /internal/seller/funnel | §4.4 | 2026-08-06 | 정합 | API 형상은 사본과 정합 | — | 유지 |
| 7 | I-8 GET /internal/seller/{brandId}/account-events | §4.4 | 2026-08-06 | 드리프트 | brand scope·suspiciousMemberCount·scope=brand 신설, 구 필드 제거 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| 8 | I-9 GET /internal/seller/{brandId}/products | §4.5 | 2026-08-05 | 드리프트 | 목록 endpoint와 DELETED 상태가 사본과 다름 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.5 반영, 상태 enum 후속 |
| 9 | I-10 POST /internal/seller/{brandId}/products | §4.5 | 2026-08-05 | 드리프트 | 등록 endpoint와 DELETED 상태가 사본과 다름 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.5 반영, 상태 enum 후속 |
| 10 | I-11 PATCH /internal/seller/{brandId}/products/{productId} | §4.5 | 2026-08-05 | 드리프트 | 수정 endpoint와 DELETED 전환/409가 사본과 다름 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.5 반영, 상태 enum 후속 |
| 11 | I-12 DELETE /internal/seller/{brandId}/products/{productId} | §4.5 | 2026-08-05 | 드리프트 | 삭제 endpoint와 HIDDEN→DELETED 전환이 사본과 다름 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.5 반영, 상태 enum 후속 |
| 12 | I-13 GET /internal/seller/{brandId}/events | §4.4 | 2026-08-06 | 드리프트 | remove_from_cart·salesQuantity·dwell 4필드 누락 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| 13 | I-14 GET /internal/seller/{brandId}/order-events | §4.4 | 2026-08-06 | 드리프트 | customerLabel HMAC·orderItemId·brand scope 미반영 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| 14 | I-15 GET /internal/seller/{brandId}/product-changes | §4.4 | 2026-07-31 | 정합 | 변경 이력 계약 정합 | — | 유지 |
| 15 | I-16 GET /internal/seller/{brandId}/churn | §4.4 | 2026-08-06 | 드리프트 | customerLabel, lastLoginAt 제거, 빈 cohort churnRate=null | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| 16 | I-17 GET /internal/products/changes | §4.8 | 2026-08-08 | 드리프트 | description은 정합, 숫자 brandId 추가가 미반영 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.8 반영, ProductChange 후속 |
| 17 | I-18 GET /internal/cart | §4.9 | 2026-08-05 | 정합 | purchaseState 정합 | — | 유지 |
| 18 | I-19 GET /internal/orders | §4.7 | 2026-07-31 | 정합 | 주문 조회 형상 정합 | — | 유지 |
| 19 | I-20 POST /events/session-end | §3.5 | 2026-07-31 | 드리프트 | reason 알려진 값이 logout/inactivityTimeout이며 newConversation은 API 사유가 아님 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §3.5 반영, SessionEndEvent 후속 |
| 20 | I-21 POST /internal/recommendations | §4.2 | 2026-08-07 | 드리프트 | 400 필수값 표에 listType·recommendationRequestId 누락 | ① 사본이 낡음(동기화) | §4.2 반영 |
| 21 | I-22 POST /internal/recommendations/home | §3.7 | 2026-08-07 | 정합 | catalogVersion 관대 수신과 한도는 정합; 정본 DB 메모에는 P-5 구현 시 `recentlyViewedProductIds` 최신순 보장 관찰 항목이 남아 있음 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | P-5 구현 시 최신순 보장을 Spring과 확인(순서가 뒤집히면 recency decay가 역전됨) |
| 22 | I-23 POST /events/session-claim | §3.5 | 2026-07-31 | 정합 | 세션 귀속 계약 정합 | — | 유지 |
| 23 | I-24 DELETE /internal/cart/items/{cartItemId} | §4.12 | 2026-08-07 | 드리프트 | 성공 삭제의 remove_from_cart server-side 적재 미기록 | ① 사본이 낡음(동기화) | §4.12 반영 |
| 24 | I-25 PATCH /internal/cart/items/{cartItemId} | §4.13 | 2026-08-07 | 정합 | 수량 변경 계약 정합 | — | 유지 |
| 25 | I-26 POST /internal/wishlist | §4.14 | 2026-08-07 | 드리프트 | internal 레인에 403 행이 남음 | ① 사본이 낡음(동기화) | §4.14 반영 |
| 26 | I-27 DELETE /internal/wishlist/{productId} | §4.15 | 2026-08-07 | 드리프트 | internal 레인에 403 행이 남음 | ① 사본이 낡음(동기화) | §4.15 반영 |
| 27 | I-28 GET /internal/wishlist | §4.16 | 2026-08-07 | 드리프트 | internal 레인에 403 행이 남음 | ① 사본이 낡음(동기화) | §4.16 반영 |
| 28 | I-29 GET /internal/seller/{brandId}/orders | §4.18 | 2026-08-07 | 드리프트 | 초안/협의 전 표시가 구현 완료 정본과 불일치 | ① 사본이 낡음(동기화) | §4.18 확정 상태 반영 |
| 29 | I-30 PATCH /internal/seller/{brandId}/order-items/{orderItemId}/status | §4.19 | 2026-08-07 | 드리프트 | 초안/협의 전 표시가 구현 완료 정본과 불일치 | ① 사본이 낡음(동기화) | §4.19 확정 상태 반영 |
| 30 | I-31 GET /internal/seller/{brandId}/reviews | §4.20 | 2026-08-07 | 드리프트 | 초안/협의 전 표시가 구현 완료 정본과 불일치 | ① 사본이 낡음(동기화) | §4.20 확정 상태 반영 |
| 31 | I-32 GET /internal/profile/{userId}/graph | §3.8 | 2026-08-08 | 드리프트 | suppressed/suppressedAt/suppressedCount와 includeSuppressed가 정본에서 제거됨; draft 표시는 확인 대기 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | 와이어 필드 동기화, draft 답변 후 표시 정정 |
| 32 | I-33 PUT /internal/profile/{userId}/graph/edges/{edgeId} | §3.9.1 | 2026-08-08 | 드리프트 | 응답 edge의 suppressed/suppressedAt가 정본에서 제거됨; draft 표시는 확인 대기 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | 와이어 필드 동기화, draft 답변 후 표시 정정 |
| 33 | I-34 DELETE /internal/profile/{userId}/graph/edges/{edgeId} | §3.9.2 | 2026-08-08 | 드리프트 | 즉시 물리 삭제이며 undo/restore/tombstone이 없음 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | 응답·규약 동기화, draft 답변 후 표시 정정 |
| 34 | I-35 POST /internal/profile/.../restore | §3.9.3 | 2026-08-08 | 드리프트 | 정본에서 폐기, 사본에 live 계약 잔존 | ① 사본이 낡음(동기화) | §3.9.3 폐기 반영 |
| 35 | I-36 POST /internal/profile/{userId}/graph/reset | §3.9.4 | 2026-08-08 | 드리프트 | purged.suppressed가 정본에서 제거됨; draft 표시는 확인 대기 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | 와이어 필드 동기화, draft 답변 후 표시 정정 |
| 36 | I-37 PUT /internal/profile/{userId}/personalization | §3.9.5 | 2026-08-08 | 정합 | 정본은 AI에 draft 표시 해제를 확인 요청 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | 답변 후 표시만 정정 |
| 37 | CH-1 POST /api/chat/sessions | §2.3, §3.1 | 2026-08-07 | 정합 | AUTH_TOKEN_EXPIRED 정합 | — | 유지 |
| 38 | CH-1b POST /api/chat/tickets | §2.3 | 2026-08-07 | 정합 | AUTH_TOKEN_EXPIRED 정합 | — | 유지 |
| 39 | CH-2 구매자 SSE | §2.2, §3.1, §6.1 | 2026-08-06 | 드리프트 | §3.1의 post-MVP budget 예시와 §6.1 dispatch 목록이 정본 이벤트 집합과 자기모순 | ① 사본이 낡음(동기화) | budget을 미구현·post-MVP로 명시하고 dispatch에서 제외 |
| 40 | CH-5 GET /api/chat/lists/{listId} | §4.3 | 2026-08-07 | 정합 | 카드 소비 계약에 모순 없음 | — | 유지 |
| 41 | CH-6 POST /api/chat/seller/sessions | §2.3, §3.2 | 2026-08-07 | 정합 | AUTH_TOKEN_EXPIRED 정합 | — | 유지 |
| 42 | CH-7 POST /api/chat/sessions/{sessionId}/claim | §3.5 | 2026-08-07 | 정합 | 게스트 귀속 계약 정합 | — | 유지 |
| 43 | S-4 판매자 SSE draft | §3.2 | 2026-07-30 | 드리프트 | 정본 S-4에는 ship op가 없어 I-30 확정과 불일치 | ③ 정본이 우리에게 물어본 미해결(우리가 답할 것) | Notion S-4 갱신 요청 |
| 44 | P-4 GET /api/products/popular | (미등재) | 2026-08-06 | 사본 미등재 | P-4 독립 절이 없고 §4.11은 P-5만 등재 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | P-4 미등재를 명시하고 참조 보완 |
| 45 | P-5 GET /api/products/recommended | §4.11 | 2026-08-07 | 드리프트 | source와 NOT_PERSONALIZED 미렌더 규칙은 정본 기준으로 갱신 필요 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §4.11 전 구 값 제거, FE/BE 후속 |
| 46 | E-1 POST /api/events | (범위 밖 §5.1 아님) | 2026-08-07 | 드리프트 | FE 11종+server 3종=14종, add/remove producer·pageType 변경 | ① 사본이 낡음(동기화) + ② 코드 변경까지 필요 | §3.1/§5.1 노트와 producer 후속 |

## ① 사본 동기화 내역

| 정본 인용/요지 | 사본 당시 상태 | 이번 조치 |
|---|---|---|
| I-21: 400 필수 필드는 sessionId, recommendationRequestId, listId, listType, productIds | §4.2가 sessionId/listId/productIds만 열거 | 누락 두 필드를 §4.2 표에 추가 |
| I-1: 리뷰 0건은 `rating: 0.0`, `reviewCount: 0`; 실패 응답과 실패가 아닌 경우를 구분 | §4.6에 리뷰 0건 규약·실패 응답표·실패 아님 표가 없음 | §4.6 응답/실패 표와 규약을 정본대로 보완 |
| I-1: `brandName` 다중 값은 하나라도 일치하면 후보에 포함; 부분일치만 정본의 질문 | §4.6에 다중 `brandName`의 확정 의미와 부분일치 질문의 경계가 불명확 | §4.6에 BE `WHERE brand IN (...)` OR 매칭을 반영하고, 부분일치만 ③ 답변으로 분리 |
| I-6: `salesCount`는 판매 수량(`SUM(oi.quantity)`) | §4.4 응답에 `salesCount`가 없음 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| I-8: 브랜드 스코프 경로, `suspiciousMemberCount`·`scope=brand` 신설; `isSuspicious`·`failCount`·`nullMemberRatio` 제거 | §4.4가 전역 경로와 구 필드를 노출 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| I-13: `remove_from_cart`, `salesQuantity`, dwell 4필드와 rows 합산 규칙 | §4.4에 eventType 1종·판매 수량·체류시간 필드가 없음 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| I-14: `customerLabel` HMAC 문자열, `orderItemId`, 브랜드 스코프 | §4.4가 구 식별자와 스코프를 유지 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| I-16: `customerLabel`, `lastLoginAt` 제거, 빈 cohort의 `churnRate=null` | §4.4가 제거된 로그인 필드와 구 cohort 의미를 노출 | dev가 v0.29.0~v0.29.3(#481·#487·#488·#489)에서 이미 동기화 — 본 PR 범위 밖 |
| I-17: 숫자 `brandId` 추가 | §4.8 응답에 `brandId`가 없음 | §4.8 응답표에 `brandId`를 추가 |
| I-20: `reason`의 알려진 값은 `logout`·`inactivityTimeout` | §3.5가 `newConversation`을 API 사유로 서술 | §3.5 어휘를 정본 값으로 교체 |
| I-9~I-12: 삭제 상태는 `DELETED`, 삭제는 HIDDEN→DELETED 전환 | §4.5가 구 HIDDEN 삭제 상태를 사용 | 목록·등록·수정·삭제 규약을 `DELETED`로 정렬 |
| P-5: `source`는 `PERSONALIZED`/`NOT_PERSONALIZED`; 후자는 추천 컴포넌트를 렌더하지 않음 | §3.7·§4.11에 구 source 값과 fallback 렌더 규칙이 남음 | 구 값을 제거하고 명명·미렌더 규칙을 반영 |
| E-1: FE 11종+server 3종=14종, add/remove producer와 `pageType` 변경 | §3.1/§5.1에 이벤트 집합·생산자 요지가 낡음 | 정본 요지를 노트에 반영하고 생산자 구현은 ② 후속으로 분리 |
| CH-2: `budget`은 v0.15.26에서 정본 제외된 미구현 post-MVP 예시 | §3.1 예시와 §6.1 dispatch 목록이 서로 모순 | §3.1에 제외 단서를 붙이고 §6.1 dispatch 목록에서 제거 |
| I-24: 삭제 성공 뒤 Spring이 remove_from_cart를 server-side 적재 | §4.12에 producer가 없음 | §4.12 성공 규약에 적재 주체 추가 |
| I-26~I-28: internal 호출은 403을 반환하지 않음 | §4.14~4.16에 AUTH_FORBIDDEN 행/설명 존재 | 세 403 표기를 제거 |
| I-29~I-31: confirmed and implemented | §4.18~4.20이 초안·BE 협의 전 | 각 제목과 안내문을 확정·구현 완료로 갱신 |
| I-35: deprecated; I-34 is immediate physical delete and no undo | §3.9.3에 restore live 계약 | 참조 번호만 보존하고 폐기 안내로 교체 |
| I-32~I-34/I-36: suppressed 계열 응답 필드와 includeSuppressed는 제거 | §3.8~§3.9.4가 undo/suppress 계약을 노출 | 조회·수정·삭제·초기화 표본/필드표를 즉시 물리 삭제 계약으로 갱신 |

## ② Notion 갱신 요청과 코드 변경 필요

| 항목 | 요청/변경 | 저장소 증거 |
|---|---|---|
| I-2/E-1 | chatSessionId, quantity, server-side add_to_cart와 analytics 생산자를 함께 전환 | app/schemas/spring.py AddToCartRequest, AddToCartResult; app/clients/spring.py |
| I-3 | I-3 Notion 응답 예시를 I-1 DTO(price/rating/reviewCount/options/optionCount)와 통일 | docs/api-spec.md §4.17은 I-1 DTO를 참조 |
| I-4/I-19 | representativeStatus 한국어 문자열과 enum의 단일 권위 결정 | docs/api-spec.md §4.7, §4.10 |
| I-6/I-8/I-13 | 매출 salesCount, 브랜드 계정 이벤트, 행동 집계 필드 동기화 | app/schemas/spring.py SalesSeriesPoint, app/clients/spring.py get_account_events, app/agents/seller/tools.py get_account_events |
| I-9~I-12 | HIDDEN 대신 DELETED 삭제 상태로 상품 도구와 스키마 전환 | app/schemas/spring.py SellerProductRow 및 상품 생성/수정 모델 |
| I-14/I-16 | customerLabel HMAC, orderItemId, churn null 의미를 소비하도록 갱신 | app/schemas/spring.py OrderEventsResult, ChurnMember; app/agents/seller/tools.py get_order_events |
| I-17 | 응답 brandId 수용 | app/schemas/spring.py ProductChange |
| I-20 | session-end reason을 logout/inactivityTimeout으로 정렬 | app/schemas/profile.py SessionEndEvent |
| P-5 | source PERSONALIZED/NOT_PERSONALIZED 및 fallback 렌더 규칙을 FE/BE와 동시 갱신 | docs/api-spec.md §4.11; app/services/home_recommendation.py |
| E-1 | 14개 event type 및 add/remove producer/pageType를 검증·생산자에 동기화 | app/schemas 및 analytics event producer 전반 |

## ③ 정본 미해결 답변 초안

### I-1 brandName 부분일치

답변 초안: 부분일치만 BE가 정규화·이스케이프한 뒤 명시적으로 허용할지 확인 부탁드립니다. 정본 확정분으로 `brandName` 배열은 OR로 해석하고 unknown 값은 무시하며, 전부 unknown일 때만 0건입니다.

### I-32~I-37 개인화 그래프

답변 초안: I-35 폐기를 포함한 현재 Notion 계약을 기준으로 한다. AI 저장소 사본은 I-35를 폐기 처리했으며 I-32, I-33, I-34, I-36, I-37의 draft 표시는 Spring 프록시/구현 상태를 Notion에서 재확인한 뒤 일괄 해제한다.

### S-4 draft

답변 초안: I-30 확정 계약에 맞춰 S-4 draft.op에 ship과 orderItemId를 추가한다. 기존 product 레인을 유지하고 HITL 승인은 계속 필수이며, 정본 S-4가 갱신되기 전까지 사본의 구현 완료 표기는 S-4 자체의 확정 주장으로 확장하지 않는다.

## 범위 밖 52건

| 도메인 | 범위 | 제외 이유 |
|---|---|---|
| auth | A-1~A-5 (5) | FE↔Spring 로그인 레인. AI 는 Spring 발급 JWT 의 sub 만 소비하며 이 엔드포인트를 호출하지 않는다. 단 A-1·A-2 의 게스트 귀속·쿠키 반납 부수효과는 CH-7·I-23 발췌 안에 인용돼 있다 |
| cart | C-1~C-4 (4) | FE↔Spring. internal 판(I-2·I-18·I-24·I-25)이 이 실측을 상속하며, 상속 관계는 internal 발췌 안에 인용돼 있다 |
| orders | O-1~O-6 (6) | FE↔Spring. AI 는 I-19·I-4 로만 읽는다 |
| mypage | M-1~M-9, M-10(폐기) (13) | FE↔Spring. 찜(M-4·M-5·M-6)은 I-26~I-28 의 상속원이라 그 안에 인용돼 있다 |
| mypage 개인화 | M-11~M-16 (6) | I-32~I-37 의 FE 프록시 미러. 계약 실체는 internal 쪽이고 사본도 internal 만 등재한다. 다만 M-14(POST /api/profile/graph/edges/{edgeId}/restore)가 [폐기] 로 표시된 점은 I-35 폐기의 방증으로 발췌에 반영했다 |
| products | P-1, P-2, P-3, P-6 (4) | FE↔Spring 카탈로그. P-3 는 I-31 distribution·sort 의 참조원이라 그 안에 인용돼 있다 |
| seller | S-1, S-2, S-3, S-5(폐기) (4) | FE↔Spring(SELLER JWT). AI 는 호출 불가. internal 대응(I-6/I-29/I-9)이 파생 규칙을 상속하며 그 상속 관계는 각 발췌에 인용돼 있다 |
| admin | AD-1~AD-7 (7) | 전부 [폐기] |
| chat 폐기 | CH-3, CH-4 (2) | 전부 [폐기] |
| internal 폐기 | I-5 (1) | [폐기] 문의 접수 |

## #461 색상 동의어 확장 전/후 실측(2026-08-09)

재현 명령: `uv run python /tmp/measure_461_color_from_mariadb_dump.py` (커밋하지 않는 실측 전용 스크립트). Spring 원본인 `/home/uuser/inte-final/_sql/mariadb/30_product.sql`의 `product` 6,559건을 읽기 전용으로 파싱해 §4.6의 3갈래 판정(색상 축 부재 통과 / 축이 있으면 색상 값만 부분 일치)을 재현했다.

기존 로컬 `pg-catalog` 모수는 `products.extras.attributes` 보유가 6,310건 중 42건, 그중 색상 키가 5건뿐이었다. 따라서 그곳의 `색상 축 부재 통과 6,307`은 AI 생성물이 아직 없는 6,268건의 효과이며 §4.6 ② 판정의 대표값이 아니므로 폐기한다. 반면 원본 덤프의 색상 축 부재는 2,256건(34.4%)으로, 정본의 색상 미상 34%·2,445건과 비율상 대조 가능하다.

| 상태 | `expand_color("그레이", mapping)` | I-1 재현 매칭 상품 | 색상 축 부재 통과 | 명시 색상 부분일치 | 확장으로 새로 유입 |
|---|---|---:|---:|---:|---:|
| A. 확장 off·사전 비어 있음 | 1개: `그레이` | 3,023 | 2,256 | 767 | 0 |
| B. 승인 46행 반영·확장 on | 2개: `그레이`, `회색` | 3,030 | 2,256 | 774 | 7 |

승인군의 그레이 묶음은 `그레이`·`회색` 두 표기이며, 이 원본 스냅샷에서는 `회색` 계열 7건이 새로 유입됐다. 아래는 그중 앞선 5건이다(나머지 2건도 같은 재현 결과에 포함된다).

| 상품 ID | 색상 값 |
|---:|---|
| 3,093,790,437 | `회색` |
| 3,354,612,571 | `회색` |
| 8,824,201,427 | `짙은회색` |
| 9,262,001,162 | `회색`, `진회색` |
| 9,382,373,018 | `검회색` |

이 수치는 2026-08-09 원본 덤프 스냅샷에 한정한 관측이며, 검수 대기 743행이 승인되었을 때의 누적 이득을 단정하지 않는다.

AI 사후필터도 정본 ②와 정합이다. `app/services/spring_client.py::search_products`는 `filters.color`를 I-1 요청으로만 전송하고, `app/services/search_service.py::apply_ai_side_filters`는 `rating_min`·`attr_conditions`만 처리한다; 후자의 `_matches_attr_conditions`는 축이 없으면 `continue`로 후보를 보존한다. `tests/unit/test_recommendation.py::test_color_attr_conditions_preserve_axis_absent_and_exclude_mismatch`가 `attr_conditions={"색상": "그레이"}` 경로에서 색상 축 없는 상품은 보존하고, `빨강` 상품은 제외함을 고정한다.
