# 챗봇 장바구니 삭제·찜 추가·찜 해제 internal 계약 초안

- **상태: 초안(제안) — 계약 아님. BE 협의 전.**
- **작성일:** 2026-08-04
- **관련 이슈:** [#285](https://github.com/toss-delta-final/jarvis-ai/issues/285)

> **이 문서의 지위**
> 이 문서는 정본(Notion 「📡 API 명세서」) 등재 전 협의안이다. 사본 `docs/api-spec.md`는 아직 이 내용을 담고 있지 않다. 합의 후 **정본 등재 → 사본 동기화** 순서로 진행하며, 이 문서만으로 endpoint·SSE 계약이 확정되거나 신설되지 않는다.

- **마커 범례:** `🔶 협의` = BE 확인 전 미확정 / `🟢` = 현행 I-2/I-18에서 상속해 확정으로 볼 수 있는 규약
- **번호 부여:** 정본 등재 시 I-24~ 번호 부여를 제안한다. 이 문서에서는 가칭 **A/B/C/D**로 부른다. **🔶 협의(Q14)**
- **작성 원칙:** FE↔BE C-4/M-4/M-5/M-6의 상세 필드·오류·멱등 동작은 미확보다. 아래 형태는 해당 계약의 실측 정합을 우선하며, I-2/I-18 규약을 이식한 미확정 값에는 `🔶 협의`를 붙였다.

## 1. 요약

| 가칭 | 신설 대상(internal) | 참조 FE 계약 | 상속할 규약 | 상태 |
|---|---|---|---|---|
| A | `DELETE /internal/cart/items/{cartItemId}` | C-4 | I-2/I-18 | 🔶 협의 |
| B | `POST /internal/wishlist` | M-5 | I-2 | 🔶 협의 |
| C | `DELETE /internal/wishlist/{productId}` | M-6 | I-2/I-18 | 🔶 협의 |
| D | `GET /internal/wishlist` | M-4 | I-18 | 🔶 협의(필요성은 §D에서 논증) |

## 2. 공통 규약 — I-2/I-18 상속분

아래 규약은 A~D에서 반복하지 않고 공통 참조한다.

| 항목 | 제안 규약 | 상태·근거 |
|---|---|---|
| 인증 | `X-Internal-Token` 서비스 토큰. 실패 시 `401 INTERNAL_TOKEN_INVALID` | 🟢 I-2/I-18과 동일(`docs/api-spec.md` §4.1·§4.9, 인증 레인 §2.3) |
| 신원 출처 | 신원을 사용자 요청 본문이나 발화에서 받지 않는다. AI가 검증한 JWT `sub`에서 도출한다. POST body/query는 이 검증된 값을 Spring에 전달하는 와이어 위치일 뿐이다. | 🟢 IDOR 방지 원칙(§2.3·§4.1) |
| 신원 전달 위치 | 본문 있는 요청(POST)은 body 필드, 본문 없는 요청(GET/DELETE)은 query parameter | 🔶 협의(Q5). I-2 body와 I-18 query에서 자연 파생한 제안이며 신설 대상 실측이 아니다(§4.1·§4.9). |
| 식별자 | `cartItemId`·`productId`·`userId`는 number(BIGINT), `guestId`는 UUID string | 🟢 §2.6. 단, 구매자 SSE의 기존 `cartItemId`는 string 표기(§3.1). |
| 와이어 표기 | JSON 필드는 camelCase | 🟢 `CLAUDE.md`, §2.6 |
| 타임아웃 | AI→Spring 호출 3s | 🟢 §2.9 |
| 성공 봉투 | `{ "success": true, "data": ... }` | 🟢 I-2/I-18 실측 형태(§4.1·§4.9) |
| 실패 봉투 | `{ "success": false, "error": { "code", "message", "detail"? } }`로 추정 | 🔶 협의(Q6). I-2 오류표가 `error.detail.options`·`error.detail.availableStock`을 참조한다(§4.1). 최상위 키와 `message` 존재 여부는 BE 실측이 필요하다. §2.5는 FE↔AI 스트림 전 오류 봉투이므로 internal 응답에 이식하지 않는다. |
| 권위 | 재고·판매상태·소유권은 Spring이 실행 시점에 재검증한다. AI의 선조회는 대상 해소·안내용이며 인가나 실행 권위가 아니다. | 🟢 I-2의 Spring 합산·검증 권위와 동형(§4.1·§4.9) |

> **잔여 위험 — 서비스 토큰**
> `X-Internal-Token` 발급·교환 방식은 I-2 단계부터 남은 유일한 C-3 미해소 항목이다(§4.1). A~D도 같은 미해소를 상속한다. **🔶 협의(Q17)**

## 3. A. 장바구니 삭제 — `DELETE /internal/cart/items/{cartItemId}` 🔶 협의

### A-a. 동기·발화 예시

- “그 세제 빼줘.”
- “아까 담은 이어폰은 장바구니에서 지워줘.”

C-4 `DELETE /api/cart/items/{id}`와 같은 사용자 결과를 internal 서비스 토큰 레인에서 제공하려는 제안이다. C-4의 상세 계약은 미확보이므로 요청·응답·오류 동작은 **🔶 BE 실측 확인(Q1)**이 우선한다.

### A-b. 대상 해소

1. 자연어는 상품을 가리키므로 AI는 I-18 `GET /internal/cart`로 현재 장바구니를 조회해 `cartItemId`를 해소한다(§4.9).
2. 같은 상품의 옵션이 여러 개라 대상이 모호하면 I-2 옵션 되물음과 동형으로 `token` 텍스트를 보내 멀티턴으로 해소한다(§3.1·§4.1).
3. 이 되물음 턴은 실행 실패가 아니므로 `CART_REMOVE_FAILED`를 내보내지 않고 `token`으로 끝난다(§3.1 “옵션 되물음은 `action` 실패가 아니다”와 동형).
4. AI가 I-18에서 얻은 `cartItemId`라도 소유권 증거가 아니다. 해소와 실행 사이 상태가 바뀔 수 있고 AI 버그로 다른 id가 전달될 수도 있으므로, Spring이 `userId`/`guestId`와 항목 소유자를 실행 시점에 반드시 재검증한다. `cartItemId`가 연속 BIGINT여서 열거 가능하다는 위협을 전제로 한다(§2.6). 🟢 원칙이며 구체 응답 코드는 **🔶 협의(Q3)**다.
5. A는 참조 계약 C-4와 같은 **단건 삭제 계약**이다. “장바구니 다 비워줘”·“이거랑 저거 둘 다 빼줘” 같은 복수 삭제 발화는 항목별로 A를 반복 호출하고 결과 `action`도 항목별로 emit한다(I-2 단건 계약의 묶음 담기 반복 호출과 동형, §4.1). 별도 벌크 삭제 endpoint는 이 초안에서 제안하지 않으며, BE에 기존 벌크/복수 삭제 계약이 있는지는 **🔶 BE 실측 확인(Q1)**에 포함한다.

### A-c. 요청

```http
DELETE {SPRING_BASE_URL}/internal/cart/items/55?userId=123
X-Internal-Token: {서비스 토큰}
```

게스트 예시:

```http
DELETE {SPRING_BASE_URL}/internal/cart/items/55?guestId=550e8400-e29b-41d4-a716-446655440000
X-Internal-Token: {서비스 토큰}
```

| 위치 | 필드 | 타입 | 필수 | 설명 | 근거 |
|---|---|---:|---|---|---|
| header | `X-Internal-Token` | string | O | 서비스 토큰 | 🟢 §4.1·§4.9 |
| path | `cartItemId` | number(BIGINT) | O | I-18로 해소한 장바구니 항목 id | 🟢 타입 §2.6 / 경로명은 🔶 협의(Q1) |
| query | `userId` | number(BIGINT) | 조건부 O | 회원 JWT `sub`에서 도출; `guestId`와 정확히 하나 | 🔶 협의(Q5·Q7), I-18 방식 §4.9 |
| query | `guestId` | string(UUID) | 조건부 O | 게스트 JWT `sub`에서 도출; `userId`와 정확히 하나 | 🔶 협의(Q4·Q5·Q7), I-2 게스트 허용 §4.1 |
| body | — | — | — | DELETE body 없음 | 🔶 협의(Q1) |

**게스트 허용 제안:** I-2는 게스트 담기를 허용한다(§4.1 v0.6.0, 결정 8 개정 §8 항목 7). 게스트 장바구니가 실재하는데 담기만 되고 빼기가 안 되는 계약은 성립하기 어려우므로 A도 허용하는 편이 자연스럽다. 다만 C-2/C-4 실측 정합이 우선이다. **🔶 협의(Q4)**

### A-d. 성공 응답

```json
{ "success": true, "data": { "cartItemId": 55 } }
```

| 필드 | 타입 | 설명 | 상태 |
|---|---|---|---|
| `success` | boolean | 성공 여부 | 🟢 공통 봉투(§4.1·§4.9) |
| `data.cartItemId` | number(BIGINT) | 삭제 결과와 SSE `CART_REMOVED`를 상관할 항목 id | 🔶 협의(Q1). C-4 응답 실측에 따라 `data: null`도 가능하다. |

이미 삭제된 항목은 C-4의 동작을 따른다. **🔶 협의(Q2)**

- C-4가 멱등 200이면 AI는 `CART_REMOVED`로 정상 종료한다. 사용자 관점에서 이미 없던 것과 지금 지운 것은 결과가 같다.
- C-4가 404이면 `CART_REMOVE_FAILED` + `reason: "CART_ITEM_NOT_FOUND"`로 낼지, “이미 빠져 있어요”라는 `token` 안내 후 성공으로 취급할지도 **🔶 협의(Q2·Q15)**다.

### A-e. 오류 표

| HTTP | code | 조건 | AI 동작(SSE) | 상태 |
|---:|---|---|---|---|
| 400 | `VALIDATION_ERROR` 또는 `CART_QUERY_INVALID` | `cartItemId` 형식 오류, `userId`/`guestId` 누락 또는 둘 다 존재 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"`; 안전 문구 | 🔶 협의(Q7). I-18은 신원 조합 오류에 `CART_QUERY_INVALID` 사용(§4.9). |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음·불일치 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"`; 서버 로그·알림 | 🟢 코드 §4.1·§4.9 / action 확장은 🔶 협의(Q15) |
| 404 | `CART_ITEM_NOT_FOUND` | 항목이 존재하지 않음 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ITEM_NOT_FOUND"`, 또는 이미 없음 안내 | 🔶 협의(Q2·Q15) |
| 403 또는 404 | 🔶 BE 실측 확인 | 소유자 불일치 | 사용자에게 타인의 항목 존재를 노출하지 않는 안전 문구 | 🔶 협의(Q3). 초안은 404 존재 은닉을 권고하되 C-4 실측 우선. |
| 5xx/timeout | 🔶 BE 실측 확인 | Spring 장애 또는 3s 초과 | `action` `CART_REMOVE_FAILED` + `reason: "CART_ERROR"` | 🔶 협의(Q1·Q15), 타임아웃 §2.9 |

403은 “그 id는 존재한다”는 정보를 주어 열거 공격에 도움을 줄 수 있으므로 초안은 404 존재 은닉을 권고한다. 단, FE 계약 C-4 실측과 다르게 갈 경우 그 이유와 소비자 차이를 별도로 합의해야 하므로 C-4 정합이 우선이다. **🔶 협의(Q3)**

### A-f. SSE `action` 연동

- 성공: `CART_REMOVED`, `cartItemId`는 기존 `CART_ADDED`와 같이 SSE에서 string으로 표기 제안(§3.1).
- 실패: `CART_REMOVE_FAILED`, `reason` 후보는 `CART_ITEM_NOT_FOUND`/`CART_ERROR`.
- 되물음: 실패 `action` 없이 `token` 텍스트(§3.1 동형).
- 전체 확장안은 §7에 모으며 CH-2 정본 개정과 함께 **🔶 협의(Q15)**한다.

### A-g. 미확정 목록

- C-4 요청·응답 필드와 전체 오류 코드: **🔶 협의(Q1)**
- 이미 삭제된 항목의 멱등 200/404 및 AI 성공 취급 여부: **🔶 협의(Q2)**
- 소유자 불일치 403/404: **🔶 협의(Q3)**
- 게스트 삭제 허용: **🔶 협의(Q4)**
- DELETE 신원 query와 검증 오류 코드: **🔶 협의(Q5·Q7)**
- `CART_REMOVED`/`CART_REMOVE_FAILED` 및 reason: **🔶 협의(Q15)**

## 4. B. 찜 추가 — `POST /internal/wishlist` 🔶 협의

### B-a. 동기·발화 예시

- “이거 찜해줘.”
- “방금 추천한 이어폰은 찜해 둘래.”

M-5 `POST /api/wishlist`와 같은 결과를 internal 레인에서 제공하려는 제안이다. “찜해줘”를 장바구니로 오분류하지 않도록 하는 intent/decompose 구현은 계약 밖이며 별도 구현 이슈 소관이다.

**회원 전용:** M-4/M-5가 인증 필요인 회원 자원이므로 게스트는 B를 호출하지 않고 로그인 안내로 degrade한다. 이때 BE endpoint를 호출하지 않으므로 게스트용 BE 오류 코드는 요구하지 않는다. 🟢 자원 경계는 M-4/M-5 행 정보 근거이며, 안내 채널은 **🔶 협의(Q15·Q18)**다.

- 초안 권고: 별도 `action` 없이 `token` 텍스트. 옵션 되물음·장바구니 조회가 별도 이벤트 없이 `token`인 전례가 있다(§3.1·§4.9).
- FE가 로그인 버튼을 띄우려면 기계 판독 신호가 필요할 수 있어 FE와 **🔶 협의(Q18)**한다.
- `GUEST_NOT_ALLOWED`를 제안하더라도 장바구니에서 폐기된 동명 reason(§3.1·§4.1)을 되살리는 것이 아니다. 서로 다른 회원 전용 자원의 새 신호가 되는 셈이므로 혼동 비용까지 함께 검토해야 하며, 초안은 사용하지 않는 쪽을 권고한다.

### B-b. 대상 해소

`productId`는 AI가 자기 추천 목록 또는 대화 문맥에서 해소한다. Spring에 별도 조회를 요구하지 않는다. 숫자 BIGINT 규약은 §2.6을 따른다. 해소가 모호하면 실행하지 않고 `token`으로 되묻는 것이 §3.1의 멀티턴 원칙과 동형이다.

### B-c. 요청

```http
POST {SPRING_BASE_URL}/internal/wishlist
X-Internal-Token: {서비스 토큰}
Content-Type: application/json
```

```json
{ "userId": 123, "productId": 1 }
```

| 위치 | 필드 | 타입 | 필수 | 설명 | 근거 |
|---|---|---:|---|---|---|
| header | `X-Internal-Token` | string | O | 서비스 토큰 | 🟢 §4.1 |
| body | `userId` | number(BIGINT) | O | 검증된 회원 JWT `sub`에서 도출 | 🔶 협의(Q5·Q8), I-2 body 방식 §4.1 |
| body | `productId` | number(BIGINT) | O | 추천 목록/대화 문맥에서 해소한 상품 id | 🟢 타입 §2.6 / M-5 필드 정합은 🔶 협의(Q8) |
| body | `guestId` | — | 금지 | 회원 전용이므로 보내지 않음 | M-4/M-5 인증 행 정보; 🔶 협의(Q8) |

### B-d. 성공 응답

```json
{ "success": true, "data": null }
```

| 필드 | 타입 | 설명 | 상태 |
|---|---|---|---|
| `success` | boolean | 성공 여부 | 🟢 공통 봉투(§4.1·§4.9) |
| `data` | object 또는 null | `wishlistId`/`productId`/`null` 중 무엇인지 미확보 | 🔶 전면 미확정(Q9) |

I-2는 후속 삭제에 `cartItemId`가 필요해 id를 반환하지만, M-6의 삭제 키는 `productId`다. 따라서 별도 `wishlistId`가 없어도 B/C 계약은 성립한다. 다만 M-5 실제 응답을 우선한다. **🔶 협의(Q9)**

이미 찜한 상품의 재찜은 M-5 동작을 따른다. **🔶 협의(Q8)**

- 멱등 200이면 AI는 “이미 찜해 두셨어요”라고 안내하고 정상 종료한다(초안 권고).
- 409이면 `WISHLIST_ADD_FAILED`로 낼지, 현재 상태가 목표와 같으므로 성공 안내할지 **🔶 협의(Q8·Q15)**한다.

### B-e. 오류 표

| HTTP | code | 조건 | AI 동작(SSE) | 상태 |
|---:|---|---|---|---|
| 400 | `VALIDATION_ERROR` | `userId`/`productId` 누락·형식 오류 | `action` `WISHLIST_ADD_FAILED` + `reason: "WISHLIST_ERROR"` | 🔶 협의(Q8·Q15) |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음·불일치 | 같은 실패 action, 서버 로그·알림 | 🟢 코드 §4.1 / action은 🔶 협의(Q15) |
| 404 | `PRODUCT_NOT_FOUND` | 없는 상품 | `action` `WISHLIST_ADD_FAILED` + `reason: "PRODUCT_NOT_FOUND"` | 🔶 협의(Q8·Q15) |
| 409 또는 200 | 🔶 BE 실측 확인 | 이미 찜한 상품 | 실패 action 또는 멱등 성공 안내 | 🔶 협의(Q8) |
| 4xx | 🔶 BE 실측 확인 | 판매중지·삭제 상품 | 안전 안내; reason은 실측 후 매핑 | 🔶 협의(Q13·Q15) |
| 5xx/timeout | 🔶 BE 실측 확인 | Spring 장애 또는 3s 초과 | `action` `WISHLIST_ADD_FAILED` + `reason: "WISHLIST_ERROR"` | 🔶 협의(Q8·Q15), §2.9 |

### B-f. SSE `action` 연동

- 성공: `WISHLIST_ADDED`; 경로 B 때문에 `productId`는 싣지 않는 안을 우선 제안한다(§2.6).
- 실패: `WISHLIST_ADD_FAILED`; `reason` 후보는 `PRODUCT_NOT_FOUND`/`WISHLIST_ERROR`.
- 게스트 로그인 안내는 초안상 `token`, 실패 action은 내지 않는다.
- 전체는 §7 및 CH-2 정본 개정과 함께 **🔶 협의(Q15·Q16)**한다.

### B-g. 미확정 목록

- M-5 요청·응답 필드, 재찜 멱등 여부: **🔶 협의(Q8)**
- 성공 `data`와 `wishlistId` 존재 여부: **🔶 협의(Q9)**
- 판매중지·삭제 상품 처리: **🔶 협의(Q13)**
- 게스트 안내 채널과 FE 로그인 UI 신호: **🔶 협의(Q18)**
- SSE 이벤트·reason·식별자 미탑재: **🔶 협의(Q15·Q16)**

## 5. C. 찜 해제 — `DELETE /internal/wishlist/{productId}` 🔶 협의

### C-a. 동기·발화 예시

- “찜한 거 빼줘.”
- “그 이어폰 찜 해제해줘.”

M-6과 동일하게 삭제 키를 `productId`로 제안한다. 찜에는 `cartItemId` 같은 별도 항목 id가 필요하지 않다는 전제는 M-6 경로 행 정보에 근거하며, 상세 응답은 **🔶 BE 실측 확인(Q10)** 대상이다.

### C-b. 대상 해소

직전 추천 목록에 있는 상품은 대화 문맥에서 `productId`를 해소할 수 있다. 그러나 “어제 찜한 이어폰”처럼 현재 문맥에 없는 상품은 C만으로 해소할 수 없다. D `GET /internal/wishlist`가 필요한 이유이며 자세한 논증은 §D에 둔다.

모호하면 A와 같이 실행 전 `token`으로 되묻고 실패 action은 내지 않는다(§3.1 동형).

### C-c. 요청

```http
DELETE {SPRING_BASE_URL}/internal/wishlist/1?userId=123
X-Internal-Token: {서비스 토큰}
```

| 위치 | 필드 | 타입 | 필수 | 설명 | 근거 |
|---|---|---:|---|---|---|
| header | `X-Internal-Token` | string | O | 서비스 토큰 | 🟢 §4.1·§4.9 |
| path | `productId` | number(BIGINT) | O | M-6과 동일한 해제 키 | 🟢 타입 §2.6 / 상세 정합은 🔶 협의(Q10) |
| query | `userId` | number(BIGINT) | O | 검증된 회원 JWT `sub`에서 도출 | 🔶 협의(Q5·Q10), I-18 query 방식 §4.9 |
| body | — | — | — | DELETE body 없음 | 🔶 협의(Q10) |

C는 회원 전용이다. 게스트는 Spring을 호출하지 않고 B와 같은 로그인 안내로 degrade한다. 안내 채널은 **🔶 협의(Q18)**다.

### C-d. 성공 응답

```json
{ "success": true, "data": null }
```

| 필드 | 타입 | 설명 | 상태 |
|---|---|---|---|
| `success` | boolean | 성공 여부 | 🟢 공통 봉투(§4.1·§4.9) |
| `data` | object 또는 null | M-6 실측 응답 미확보 | 🔶 협의(Q10) |

미찜 상품 해제는 M-6 동작을 따른다. **🔶 협의(Q10)**

- 멱등 200이면 `WISHLIST_REMOVED`로 정상 종료한다.
- 404이면 `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ITEM_NOT_FOUND"`로 낼지, “이미 찜 목록에 없어요”라는 안내로 성공 취급할지 **🔶 협의(Q10·Q15)**한다.

### C-e. 오류 표

| HTTP | code | 조건 | AI 동작(SSE) | 상태 |
|---:|---|---|---|---|
| 400 | `VALIDATION_ERROR` 또는 자원별 query 오류 | `productId`/`userId` 누락·형식 오류 | `action` `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ERROR"` | 🔶 협의(Q7·Q10·Q15) |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음·불일치 | 같은 실패 action, 서버 로그·알림 | 🟢 코드 §4.1·§4.9 / action은 🔶 협의(Q15) |
| 404 | `WISHLIST_ITEM_NOT_FOUND` | 미찜 상품 또는 상품 없음 | 실패 action 또는 이미 없음 성공 안내 | 🔶 협의(Q10·Q15) |
| 4xx | 🔶 BE 실측 확인 | 판매중지·삭제 상품 | 안전 안내; reason은 실측 후 매핑 | 🔶 협의(Q13·Q15) |
| 5xx/timeout | 🔶 BE 실측 확인 | Spring 장애 또는 3s 초과 | `action` `WISHLIST_REMOVE_FAILED` + `reason: "WISHLIST_ERROR"` | 🔶 협의(Q10·Q15), §2.9 |

### C-f. SSE `action` 연동

- 성공: `WISHLIST_REMOVED`; `productId`는 구매자 SSE 경로 B 때문에 미탑재 제안(§2.6).
- 실패: `WISHLIST_REMOVE_FAILED`; `reason` 후보는 `WISHLIST_ITEM_NOT_FOUND`/`WISHLIST_ERROR`.
- 전체는 §7 및 CH-2 정본 개정과 함께 **🔶 협의(Q15·Q16)**한다.

### C-g. 미확정 목록

- M-6 요청·응답 필드 및 미찜 해제의 멱등 200/404: **🔶 협의(Q10)**
- DELETE 신원 query와 검증 오류: **🔶 협의(Q5·Q7)**
- 판매중지·삭제 상품 처리: **🔶 협의(Q13)**
- SSE 이벤트·reason·식별자 미탑재: **🔶 협의(Q15·Q16)**

## 6. D. 찜 목록 internal 조회 — `GET /internal/wishlist` 🔶 협의

### D-a. 동기·발화 예시

- “내가 뭐 찜했지?”
- “어제 찜한 이어폰은 빼줘.”

### D-b. 대상 해소 및 필요성 논증 — 결론: 필요

1. **C는 지칭 해소가 필요하다.** “찜한 거 빼줘”는 `productId`를 직접 주지 않는다. 직전 추천 목록에 없는 과거 찜은 현재 대화 문맥만으로 해소할 수 없다.
2. **AI는 M-4를 대신 호출할 수 없다.** M-4는 FE↔BE 계약이며 회원 AT 기반이다. AI→Spring 레인은 `X-Internal-Token` 서비스 토큰이고, 사용자 JWT 포워딩 방식은 v0.6.0 I-2에서 폐기됐다(§2.3·§4.1). AI는 M-4를 호출할 자격 증명을 갖지 않으므로 별도 internal 경로가 필요하다.
3. **I-18이라는 정확한 전례가 있다.** 담기 I-2에 조회 I-18을 둔 이유가 지칭 해소와 질의 응답이다(§4.9). 찜도 쓰기 B/C만 있고 읽기가 없으면 같은 구멍이 생긴다.
4. **부수 효과가 있다.** “내가 뭐 찜했지?”에도 D로 조회한 뒤 별도 SSE 이벤트 없이 `token`으로 답할 수 있다. I-18 용도 1과 동형이다(§3.1·§4.9).

따라서 D 신설을 제안한다. 단 endpoint 수용과 실제 응답 필드는 **🔶 협의(Q11·Q12)**이며, 이 문서가 신설을 확정하지 않는다.

### D-c. 요청

```http
GET {SPRING_BASE_URL}/internal/wishlist?userId=123
X-Internal-Token: {서비스 토큰}
```

| 위치 | 필드 | 타입 | 필수 | 설명 | 근거 |
|---|---|---:|---|---|---|
| header | `X-Internal-Token` | string | O | 서비스 토큰 | 🟢 §4.9 |
| query | `userId` | number(BIGINT) | O | 검증된 회원 JWT `sub`에서 도출 | 🔶 협의(Q5·Q11), I-18 방식 §4.9 |
| query | paging | 🔶 미정 | 미정 | MVP는 전량 반환 권고; 찜 목록 규모를 고려해 BE 확인 | 🔶 협의(Q11), I-18 MVP 전량 반환 전례 §4.9 |

회원 전용이므로 `guestId`는 제안하지 않는다. 게스트는 Spring 호출 없이 로그인 안내로 degrade하며 채널은 **🔶 협의(Q18)**다.

### D-d. 성공 응답

```json
{
  "success": true,
  "data": {
    "items": [
      { "productId": 1, "productName": "무선 이어폰", "price": 12900 }
    ]
  }
}
```

| 필드 | 타입 | 설명 | 상태 |
|---|---|---|---|
| `success` | boolean | 성공 여부 | 🟢 공통 봉투(§4.9) |
| `data.items` | array | 찜 항목 목록; 빈 목록은 `[]` 정상 200 | 🔶 응답 형태 협의(Q11), 빈 목록 의미는 I-18 동형 §4.9 |
| `items[].productId` | number(BIGINT) | C 호출 대상 해소용 | 🟢 타입 §2.6 / 필드 반환은 🔶 협의(Q11) |
| `items[].productName` | string | 자연어 답변·지칭 해소에 필수 | 🔶 협의(Q12). I-18이 같은 이유로 `productName`/`optionName` 필수를 확정(§4.9, BE 2026-07-18). |
| `items[].price` | number 또는 없음 | 금액 안내가 필요할 때의 후보 필드 | 🔶 협의(Q11). M-4 실측 정합과 Spring 표시 권위 확인 필요. |

- 빈 찜 목록은 `items: []` 정상 200을 제안한다. I-18 빈 장바구니와 동형이다(§4.9). **🔶 협의(Q11)**
- 페이징은 MVP 전량 반환을 권고한다(I-18 동형). 다만 찜 목록은 장바구니보다 커질 수 있으므로 상한·페이징 필요성을 **🔶 협의(Q11)**한다.
- 필드명·포함 범위는 M-4 실측과 정합 확인이 필요하다. **🔶 협의(Q11·Q12)**

### D-e. 오류 표

| HTTP | code | 조건 | AI 동작(SSE) | 상태 |
|---:|---|---|---|---|
| 400 | `VALIDATION_ERROR` 또는 wishlist query 오류 | `userId` 누락·형식 오류 | 별도 action 없이 `token`으로 조회 실패 안내 | 🔶 협의(Q7·Q11). I-18 `CART_QUERY_INVALID`와 자원별 정합 필요(§4.9). |
| 401 | `INTERNAL_TOKEN_INVALID` | 서비스 토큰 없음·불일치 | 별도 action 없이 `token` 안전 안내, 서버 로그·알림 | 🟢 코드 §4.9 / 안내 문구는 🔶 협의(Q11) |
| 5xx/timeout | 🔶 BE 실측 확인 | Spring 장애 또는 3s 초과 | 별도 action 없이 `token`으로 일시 조회 불가 안내 | 🔶 협의(Q11), I-18 질의 응답 동형 §4.9·타임아웃 §2.9 |

D는 조회이므로 신규 성공/실패 `action`을 제안하지 않는다. I-18의 “장바구니에 뭐 있어?” 응답이 `token`인 전례를 따른다(§3.1·§4.9).

### D-f. SSE 연동

- “내가 뭐 찜했지?”: D 결과를 `token` 텍스트로 답한다.
- C의 지칭 해소: D가 후보를 제공하고, 모호하면 `token`으로 되묻는다. 실제 해제 성공/실패만 C의 action 후보를 emit한다.
- 조회 자체를 위한 별도 action은 제안하지 않는다(§3.1·§4.9 동형).

### D-g. 미확정 목록

- D 신설 수용, 요청·응답·오류·페이징, M-4 필드 정합: **🔶 협의(Q11)**
- `productName` 필수 반환: **🔶 협의(Q12)**
- query 신원 전달과 검증 오류 코드: **🔶 협의(Q5·Q7)**
- 게스트 로그인 안내의 FE 신호: **🔶 협의(Q18)**

## 7. SSE `action` 확장안 — 계약 확정 아님 🔶 협의

아래 6종을 기존 `CART_ADDED`/`CART_ADD_FAILED`와 대칭인 **확장안으로 제안한다**. 채택하려면 CH-2 정본 개정 동반 협의가 필요하며, 사본 `docs/api-spec.md` §3.1만 먼저 고쳐서는 안 된다. **🔶 협의(Q15)**

| `type` | 계기 | 추가 필드 | 상태 |
|---|---|---|---|
| `CART_REMOVED` | A 성공 | `cartItemId` | 🔶 협의(Q15) |
| `CART_REMOVE_FAILED` | A 실패 | `reason` | 🔶 협의(Q15) |
| `WISHLIST_ADDED` | B 성공 | 없음 | 🔶 협의(Q15·Q16) |
| `WISHLIST_ADD_FAILED` | B 실패 | `reason` | 🔶 협의(Q15) |
| `WISHLIST_REMOVED` | C 성공 | 없음 | 🔶 협의(Q15·Q16) |
| `WISHLIST_REMOVE_FAILED` | C 실패 | `reason` | 🔶 협의(Q15) |

공통 필드는 기존과 같이 `type`·`message`(사용자 노출 안전 문구), 실패 시 `reason`을 제안한다(§3.1). **🔶 협의(Q15)**

성공 예시:

```json
{
  "type": "action",
  "data": {
    "type": "CART_REMOVED",
    "message": "장바구니에서 세제를 뺐어요.",
    "cartItemId": "55"
  }
}
```

실패 예시:

```json
{
  "type": "action",
  "data": {
    "type": "WISHLIST_ADD_FAILED",
    "message": "해당 상품을 찾지 못했어요.",
    "reason": "PRODUCT_NOT_FOUND"
  }
}
```

### 식별자 표기와 경로 B

- `cartItemId`는 internal 계약에서 number(BIGINT)이지만 SSE에서는 string으로 표기한다. 기존 §3.1의 `CART_ADDED` 예시와 필드 표가 string이므로 `CART_REMOVED`도 그대로 따르는 제안이다. 이 숫자/문자열 비대칭은 기존 계약의 관찰이며 여기서 고치지 않는다.
- 찜 이벤트에는 `productId`를 싣지 않는 안을 제안한다. §2.6 경로 B는 “구매자 SSE는 상품 카드/productId를 싣지 않는다”고 정한다. 대칭만을 이유로 `productId`를 추가하면 경로 B를 깬다.
- 반면 FE가 찜 아이콘을 정확히 갱신하려면 식별자가 필요할 수 있다. 경로 B 예외를 만들지, FE가 현재 화면·요청 문맥으로 해결할지는 FE와 **🔶 협의(Q16)**한다. 초안 권고는 미탑재다.

### `reason` 허용값 제안

| action | `reason` 후보 | 조건 |
|---|---|---|
| `CART_REMOVE_FAILED` | `CART_ITEM_NOT_FOUND` / `CART_ERROR` | 전자는 C-4가 404인 경우에만 필요 |
| `WISHLIST_ADD_FAILED` | `PRODUCT_NOT_FOUND` / `WISHLIST_ERROR` | M-5 오류 실측과 정합 필요 |
| `WISHLIST_REMOVE_FAILED` | `WISHLIST_ITEM_NOT_FOUND` / `WISHLIST_ERROR` | 전자는 M-6가 404인 경우에만 필요 |

- 위 허용값 전체는 **🔶 협의(Q15)**다.
- 총칭 오류를 `WISHLIST_ERROR`로 분리할지 기존 `CART_ERROR` 하나로 뭉칠지도 **🔶 협의(Q15)**다. 초안은 자원 구분이 명확한 `WISHLIST_ERROR`를 권고한다.
- A/C가 멱등 200으로 정해지면 `CART_ITEM_NOT_FOUND`/`WISHLIST_ITEM_NOT_FOUND` reason 자체가 필요 없을 수 있다. 즉 reason 집합은 Q2/Q10 결정에 종속된다. **🔶 협의(Q2·Q10·Q15)**

### 발생 횟수·순서

- 담기와 같이 항목별 0회 이상을 제안한다(§3.1).
- 여러 건을 처리하면 단건 internal 호출 결과마다 항목별 action을 emit한다. I-2 단건 계약과 동형이다(§4.1).
- 모호성 해소·게스트 로그인 안내처럼 실행 전 대화로 끝난 턴은 실패 action을 내지 않는다(§3.1 동형).
- 이 규약 역시 CH-2 정본 개정 동반 **🔶 협의(Q15)** 대상이다.

## 8. 협의 안건 — BE/FE 답변표

| # | 대상 | 질문 | 초안 권고 | BE/FE 답변 |
|---|---|---|---|---|
| Q1 | C-4 | 요청/응답 필드·오류 코드 실측은? 기존 벌크/복수 삭제 계약도 있는가? | — | |
| Q2 | C-4 | 이미 삭제된 항목은 멱등 200인가 404인가? 404면 AI는 실패 action인가 성공 안내인가? | 멱등 200 | |
| Q3 | C-4 | 소유자 불일치는 403인가 404인가? | 404(존재 은닉), 단 C-4 실측 우선 | |
| Q4 | A | 게스트 삭제를 허용하는가(C-2/I-2 게스트 담기와 정합)? | 허용 | |
| Q5 | 공통 | 본문 없는 GET/DELETE의 신원을 I-18처럼 query parameter로 전달하는가? | query | |
| Q6 | 공통 | internal 실패 봉투 최상위 형태(`success`/`error.code`/`message`/`detail`)는? | I-2에서 추정한 형태 | |
| Q7 | 공통 | `userId`/`guestId` 누락·중복·형식 오류 코드는 `VALIDATION_ERROR`인가 I-18의 `CART_QUERY_INVALID`처럼 자원별 코드인가? | 자원별 기존 코드 | |
| Q8 | M-5 | 요청/응답 필드 실측은? 재찜은 멱등 200인가 409인가? | 멱등 200 | |
| Q9 | M-5 | 성공 `data`에는 무엇이 오는가? `wishlistId`가 존재하는가? | id 없어도 계약 성립 | |
| Q10 | M-6 | 요청/응답·오류 실측은? 미찜 상품 해제는 멱등 200인가 404인가? | 멱등 200 | |
| Q11 | D | `GET /internal/wishlist` 신설을 수용할 수 있는가? 요청·응답·오류·페이징과 M-4 정합 필드는? | 신설 필요(§D), MVP 전량 반환 | |
| Q12 | D | 자연어 응답용 `productName`을 필수 포함할 수 있는가(I-18 전례)? | 필수 | |
| Q13 | 찜 | 판매중지·삭제 상품의 찜 추가/해제 동작과 오류 코드는? | — | |
| Q14 | 번호 | 정본 등재 번호 I-24~ 부여 방식은? | I-24~I-27 | |
| Q15 | SSE | CH-2 `action` 6종 확장과 reason 집합을 수용할 수 있는가? 멱등 결정에 따른 NOT_FOUND reason 제거도 포함하는가? | §7 안 | |
| Q16 | SSE(FE) | 찜 이벤트에 식별자를 싣지 않는 경로 B로 찜 아이콘·화면 갱신이 가능한가? 불가하면 경로 B 예외가 필요한가? | `productId` 미탑재 | |
| Q17 | 공통 | I-2부터 남은 `X-Internal-Token` 발급·교환 방식은? | — | |
| Q18 | 게스트(FE) | 게스트의 찜 발화에 대한 로그인 안내를 별도 `action` 없이 `token`으로 내도 되는가? FE가 로그인 버튼을 띄우려면 기계 판독 신호가 필요한가? | `token`(별도 `action` 없음) | |

## 9. 후속 단계

진행 순서는 다음과 같다.

1. **계약 초안 작성** — 이 문서/이 PR이 수행하는 유일한 단계
2. BE 협의 — Q1~Q18 실측·결정 회수
3. 정본(Notion 「📡 API 명세서」) 등재 — I-24~ 번호는 Q14 결과에 따름
4. 사본 `docs/api-spec.md`와 CH-2 동기화
5. 구현 이슈 #116·#117의 `blocked:spring` 해제

> 이 PR은 첫 칸인 **초안 작성만** 수행한다. 정본 등재, 사본 개정, CH-2 개정, 구현 및 blocked 해제는 사람 간 합의 뒤의 후속 단계다.
