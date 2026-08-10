# #285 — 장바구니 삭제 · 찜 추가 · 찜 해제 · 찜 목록 internal 계약 검증 기록

> 이 문서는 계약 정본이 아니다 — 정본은 Notion 「📡 API 명세서」, 사본은 `docs/api-spec.md`
> §4.12~4.16. 여기 있는 것은 그 계약이 AI 코드·BE 와 실제로 맞는지 대조한 검증 기록이다.

이슈 #285 는 "장바구니 삭제·찜 추가·찜 해제 internal 계약을 만들어라"였다. 그 계약은 이미
정본에 I-24~I-28 로 확정·등재됐다(2026-08-05). 이 PR(#285 마무리 레인)은 **계약을 고치지
않는다** — ① BE `jarvis-backend` 실측으로 계약·AI 코드·BE 3자가 실제로 일치하는지 검증하고,
② 코드에 남아 있던 낡은 상태 표기(주석·docstring)를 스윕하고, ③ 계약 실패표 커버리지 빈칸을
채운 것이 산출물이다. **[2026-08-10 갱신]** 이후 사람의 지시로 이 레인이 **I-25(수량 변경)
AI 구현까지** 함께 진행했다 — 애초 이슈 #285 본문·초기 검증 범위 밖이었던 항목이지만, 계약이
이미 확정돼 있었고(§4.13) Spring·FE 도 준비돼 있어 이 레인에서 마저 구현했다(§1 A 아래·§5
"I-25 수량 변경" 절 참조).

## 1. A·B·C·D 계약 요약 — 확정값

참조 FE 계약: C-4(삭제)·C-3(수량 변경)·M-5(찜 추가)·M-6(찜 해제)·M-4(찜 목록). 상속한 internal
공통 규약(I-2 §4.1 · I-18 §4.9): `X-Internal-Token` 서비스 토큰, 신원은 **AI-검증 JWT `sub`
에서 도출**(본문/쿼리에 그대로 받지 않음, §2.3), AI→Spring 타임아웃 3s, 응답 envelope
`{success, data}`/`{success:false, error:{code,...}}`.

### A. 장바구니 삭제 (I-24, api-spec §4.12)

| | 값 |
|---|---|
| 요청 | `DELETE /internal/cart/items/{cartItemId}?userId=` 또는 `?guestId=`(정확히 하나, 게스트 허용) |
| 성공 200 | `{success:true, data:null}` — `cartItemId` 는 응답에 없다, AI 가 **요청에 쓴 값**을 그대로 `action`(`CART_REMOVED`)에 싣는다 |
| 실패 | 400 `VALIDATION_ERROR`(path 비숫자 / 신원 0·2개) · 403 `AUTH_FORBIDDEN`(소유자 불일치, BE 재검증) · 404 `CART_ITEM_NOT_FOUND`(**비멱등** → `action` `CART_REMOVED` 로 성공 안내) · 500 `INTERNAL_ERROR` |
| 비고 | 재고·상품 상태 무관(HIDDEN·품절도 삭제 성공), 복수 삭제는 항목별 반복 호출(bulk 없음) |

I-25(수량 변경, §4.13)는 애초 이 이슈 범위 밖이었으나, **사람의 지시로 이 레인이 마저
구현했다**(2026-08-10 — 결정론적 대상 해소·치환(I-25)/합산(I-2) 판정 라우팅 2경로, api-spec
§3.1·§4.13 v0.32.8). 요청/응답/실패 요약과 커버리지는 §5 "I-25 수량 변경" 절 참조.

### B. 찜 추가 (I-26, api-spec §4.14)

| | 값 |
|---|---|
| 요청 | `POST /internal/wishlist` `{userId, productId}`(회원 전용, 게스트 없음) |
| 성공 200 | `{success:true, data:{productId}}` — `wishlistId` 없음, 이벤트에도 `productId` 를 싣지 않는다(경로 B) |
| 실패 | 400 `VALIDATION_ERROR`(필드/타입/신원) · 404 `PRODUCT_NOT_FOUND`(없는 상품만, HIDDEN·품절은 찜 가능) · 409 `WISHLIST_DUPLICATE`/`RESOURCE_CONFLICT`(**비멱등** → `action` `WISHLIST_ADDED` 로 성공 안내) · 500 `INTERNAL_ERROR` |
| 비고 | **403 없음**(§2 참조) |

### C. 찜 해제 (I-27, api-spec §4.15)

| | 값 |
|---|---|
| 요청 | `DELETE /internal/wishlist/{productId}?userId=`(회원 전용, path 는 `productId` — `wishlistId` 아님, 게스트 없음) |
| 성공 200 | `{success:true, data:null}` |
| 실패 | 400 `VALIDATION_ERROR`(path 비숫자/신원) · 404 `WISHLIST_NOT_FOUND`(찜 안 함/없는 상품 구별 안 함, **비멱등** → `action` `WISHLIST_REMOVED` 로 성공 안내) · 500 `INTERNAL_ERROR` |
| 비고 | HIDDEN·품절 상품도 찜 행만 있으면 해제 성공. **403 없음** |

### D. 찜 목록 조회 (I-28, api-spec §4.16)

| | 값 |
|---|---|
| 요청 | `GET /internal/wishlist?userId=`(회원 전용) |
| 성공 200 | `{success:true, data:{items:[{productId, name, brandName, price, originalPrice, imageUrl, rating, reviewCount, purchaseState}]}}` — `name` ≠ `productName`(I-18 과 다름), 0건도 200 |
| 실패 | 400 `VALIDATION_ERROR`(신원) · 401 `INTERNAL_TOKEN_INVALID` · 500 `INTERNAL_ERROR` |
| 비고 | AI 실사용 필드는 `productId`·`name`·`purchaseState` 뿐(나머지는 SSE 로 내보내지 않음, 경로 B). 페이징 없음 — MVP 전량 반환. **403 없음** |

공통: 신원 query 검증 오류 code 는 I-24~I-28 전부 자원별 신규 code 를 신설하지 않고 기존
`VALIDATION_ERROR` 를 재사용한다.

## 2. 실측 대조표

BE 실측 근거: `/home/uuser/inte-final/jarvis-backend` **`origin/main` `39b1ba7`**(2026-08-08).
로컬 체크아웃 HEAD 는 74커밋 뒤처져 있어 인용하지 않았다 — `git show origin/main:<path>` 로
직접 봤다.

| 항목 | 계약 `docs/api-spec.md` | AI 코드 | BE `origin/main` |
|---|---|---|---|
| I-24 삭제 | §4.12 `DELETE /internal/cart/items/{cartItemId}?userId\|guestId`(정확히 하나) | `spring_client.py::delete_cart_item` | `InternalCartController#removeItem` + `requireExactlyOneIdentity(..., VALIDATION_ERROR)` |
| I-24 403 | 소유자 불일치 `AUTH_FORBIDDEN` | `CartError` → `CART_REMOVE_FAILED`/`CART_ERROR` | `CartService#findOwnedItem` 이 `AUTH_FORBIDDEN` throw |
| I-24 404 | `CART_ITEM_NOT_FOUND` 비멱등 → **`CART_REMOVED`**(성공 안내) | `remove.py` `except CartItemNotFound` | 두 번째 삭제 404 |
| I-26 추가 | §4.14 `POST /internal/wishlist` `{userId, productId}` → `data:{productId}` | `AddWishlistRequest` / `add_wishlist` | `InternalWishlistController#add` |
| I-26 409 | `WISHLIST_DUPLICATE`·`RESOURCE_CONFLICT` → **`WISHLIST_ADDED`** | `wishlist.py` `except WishlistDuplicate` | `WishlistService#add` |
| I-26 404 | `PRODUCT_NOT_FOUND`(없는 상품만, HIDDEN·품절은 찜 가능) | `WishlistProductNotFound` | `WishlistService#add` 의 `productRepository.findById` |
| I-27 해제 | §4.15 path=`productId`, query=`userId` 만(게스트 없음) | `remove_wishlist(product_id, *, user_id)` | `remove(@PathVariable productId, @RequestParam userId)` |
| I-27 404 | `WISHLIST_NOT_FOUND` — 없는 상품/안 찜한 상품 구별 안 함 | `WishlistNotFound` → `WISHLIST_REMOVED` | `WishlistService#remove` 가 `(memberId, productId)` 로만 조회 |
| I-28 목록 | §4.16 `items[].name`(≠`productName`)·`purchaseState`, 0건도 200 | `WishlistItem`(3필드) | `InternalWishlistListResponse` |
| 검증 code | I-24~I-28 공통 **`VALIDATION_ERROR` 재사용** | 동일 | BE 주석: "2026-08-05에 '자원별 query 검증 code 신설' 안이 **채택되지 않았다**(노션 I-27·I-28)" |
| 찜 403 | §4.14~4.16 "403은 이 API에 존재하지 않는다" | — | `InternalWishlistController` 에 역할 검사 **없음** |
| 200+false | (계약에 없음) | fail-closed 방어 | `ApiResponse` + `GlobalExceptionHandler` 가 `success:false` 를 **항상 non-200** 으로 냄 |
| §3.1 `action` | 10종 전부 emit(I-25 2종 포함, 2026-08-10 구현 완료) | `app/schemas/chat.py::ActionData.type` Literal 10종 | FE 10종 수신(FE PR #79) |

## 3. Q항목 처분표

| Q | 안건 | 처분 | 근거 |
|---|---|---|---|
| Q5 | body 없는 GET/DELETE 신원을 query 로? | **해소** — query 채택 | BE `@RequestParam` 실측 |
| Q7 | 자원별 신규 검증 code 신설? | **해소 — 미채택**, `VALIDATION_ERROR` 재사용 | BE 코드 주석("2026-08-05 채택 안 됨", 노션 I-27·I-28) |
| Q11 | `GET /internal/wishlist` 신설? 전량 반환 상한은? | **부분 해소** — 신설 수용, **상한 미결** | api-spec §4.16 "페이징 없음 — MVP 전량 반환", 상한 수치는 코멘트에 답 없음 |
| Q13 | HIDDEN·품절 상품 해제 동작 | **해소** — 찜 행만 있으면 성공 | api-spec §4.15 "[확정 2026-08-05]" |
| Q14 | I-24~I-28 번호 확정 | **해소** | 정본 등재 2026-08-05 |
| Q15 | CH-2 `action` 확장 8종 수용? | **해소** — 10종 등재 | api-spec §3.1 |
| Q16 | 찜 `action` 에 식별자 없이 화면 갱신? | **해소** — `productId` 미탑재(경로 B) | api-spec §4.14 "이벤트에 productId 를 싣지 않는다" |
| Q17 | `X-Internal-Token` 발급·교환 | **미결 — 단 #285 범위 밖** | "I-2부터 잔여"(§4.1 C-3·§5 C-1) |
| Q18 | 게스트 찜을 `token` 로그인 안내로? | **해소** — internal 호출 없이 degrade | api-spec §4.14 "게스트 발화는 internal 호출 없이 token 로그인 안내로 degrade" |

**초안이 실측으로 뒤집힌 것**:
- 소유자 불일치는 404 은닉이 아니라 **403** `AUTH_FORBIDDEN`.
- 삭제 성공 응답은 `data.cartItemId` 가 아니라 **`data: null`**(AI 가 요청 값을 그대로 되싣는다).
- `action` 의 `cartItemId` 는 string 이 아니라 **number**(BIGINT).
- 찜 API 에 **403 없음**(`/internal/**` 은 서비스 토큰 필터 하나로만 지키며 역할 개념이 없다).
- **200+`success:false` 조합은 존재하지 않는다** — `ApiResponse`+`GlobalExceptionHandler` 가
  `success:false` 를 항상 non-200 으로 낸다. AI 어댑터의 200+`success:false` 방어 분기는
  계약상 오지 않는 조합에 대한 fail-closed 방어라 유지한다.

## 4. 비멱등 3종과 "성공 안내로 종료" 규약

| 비멱등 실패 | → 성공 안내 |
|---|---|
| 404 `CART_ITEM_NOT_FOUND`(I-24) | `CART_REMOVED` |
| 409 `WISHLIST_DUPLICATE`/`RESOURCE_CONFLICT`(I-26) | `WISHLIST_ADDED` |
| 404 `WISHLIST_NOT_FOUND`(I-27) | `WISHLIST_REMOVED` |

세 어댑터(`delete_cart_item`·`add_wishlist`·`remove_wishlist`) 모두 **`error.code` 가 계약과
정확히 일치할 때만** 이 낙성을 한다 — HTTP status 만 보고 낙성하지 않는다. 이유: 엔드포인트가
아직 배포되지 않은 구간에서도 라우트가 없으면 404/409 가 나는데, 그 응답은 `error.code` 가
계약과 다르거나 본문 자체가 없다. status 만으로 판정하면 "미배포라 실패한 호출"을 "이미
빠져 있어요"/"이미 찜해 두셨어요" 같은 **거짓 성공 안내**로 오인한다. code 정확 일치 검사가
그 오인을 막는다(`tests/unit/test_cart.py`·`tests/unit/test_wishlist.py` 의 "wrong_code"·
"empty_body" 테스트가 이 구분을 고정한다).

## 5. T4 커버리지 매트릭스

`docs/api-spec.md` §4.12·§4.13·§4.14·§4.15·§4.16 실패표 각 행 + 성공 응답 대 `tests/unit/test_cart.py`
/ `tests/unit/test_wishlist.py` / `tests/unit/test_cart_quantity.py` / `tests/unit/test_cart_intent_guard.py`
대조.

### I-24 삭제 (`test_cart.py` "spring_client 배선 (I-24 삭제…)")

| 행 | 상태 |
|---|---|
| 성공(userId) | 기존 커버 — `test_delete_cart_item_success_returns_none` |
| 성공(guestId) | 기존 커버 — `test_delete_cart_item_uses_guest_id_only` |
| 200 success:false(방어) | 기존 커버 — `test_delete_cart_item_200_success_false_raises_cart_error` |
| 200 success 키 없음(양성 대조) | 기존 커버 — `test_delete_cart_item_200_missing_success_key_is_not_failure` |
| 404 `CART_ITEM_NOT_FOUND`(비멱등) | 기존 커버 — `test_delete_cart_item_not_found_raises` |
| 404 code 불일치 | 기존 커버 — `test_delete_cart_item_404_with_wrong_code_raises_cart_error_not_not_found` |
| 404 본문 없음 | 기존 커버 — `test_delete_cart_item_404_empty_body_raises_cart_error_not_not_found` |
| 403 `AUTH_FORBIDDEN` | 기존 커버 — `test_delete_cart_item_forbidden_maps_to_cart_error` |
| 500 `INTERNAL_ERROR` | 기존 커버 — `test_delete_cart_item_500_maps_to_cart_error` |
| 신원 query 0개 | 기존 커버 — `test_delete_cart_item_rejects_zero_identity_queries` |
| 신원 query 2개 | 기존 커버 — `test_delete_cart_item_rejects_two_identity_queries` |
| 400(path 비숫자) | N/A — `cart_item_id: int` 타입 주석이라 이 어댑터 계층에서 비숫자 값을 구성할 수 없다(라우트 계층 관심사) |

I-24 는 빈 칸 없음 — 새 테스트 추가하지 않았다.

### I-26 찜 추가 (`test_wishlist.py` "I-26 찜 추가")

| 행 | 상태 |
|---|---|
| 성공 + `{userId, productId}` camelCase 배선 단정 | 기존 커버 — `test_add_wishlist_success_parses_product_id`(`client.calls` 로 정확한 dict 단정, ③번 의심 빈칸 해소 확인) |
| 404 `PRODUCT_NOT_FOUND` | 기존 커버 — `test_add_wishlist_product_not_found_raises` |
| 404 code 불일치 | 기존 커버 — `test_add_wishlist_404_with_wrong_code_raises_wishlist_error_not_product_not_found` |
| 409 `WISHLIST_DUPLICATE`(비멱등) | 기존 커버 — `test_add_wishlist_duplicate_raises` |
| 409 `RESOURCE_CONFLICT`(비멱등) | 기존 커버 — `test_add_wishlist_resource_conflict_also_raises_duplicate` |
| 409 code 불일치 | 기존 커버 — `test_add_wishlist_409_with_wrong_code_raises_wishlist_error_not_duplicate` |
| 403(계약엔 없음, 미상 코드 낙성 확인) | 기존 커버 — `test_add_wishlist_forbidden_maps_to_wishlist_error` |
| 500 `INTERNAL_ERROR` | 기존 커버 — `test_add_wishlist_500_maps_to_wishlist_error` |
| 200 success:false(방어) | 기존 커버 — `test_add_wishlist_200_success_false_raises_wishlist_error` |
| 응답 `productId` 파싱 불가 | 기존 커버 — `test_add_wishlist_unparseable_product_id_raises_wishlist_error` |
| 400(필드 누락 등, 미상 코드 일반 낙성) | N/A — 403/409-불일치/500 테스트가 같은 범용 낙성 분기(`raise WishlistError(f"add_wishlist 실패: {status} {code}")`)를 이미 exercise — 400 전용 테스트는 같은 코드 경로 중복 |

### I-27 찜 해제 (`test_wishlist.py` "I-27 찜 해제")

| 행 | 상태 |
|---|---|
| 성공 + `userId` query만(guestId 미탑재) 배선 단정 | 기존 커버 — `test_remove_wishlist_success_returns_none`(`client.calls == [("DELETE", ..., {"userId": 1})]`, ②번 의심 빈칸 해소 확인 — `remove_wishlist` 시그니처 자체에 `guest_id` 파라미터가 없어 구조적으로도 실을 수 없다) |
| 404 `WISHLIST_NOT_FOUND`(비멱등) | 기존 커버 — `test_remove_wishlist_not_found_raises` |
| 404 code 불일치 | 기존 커버 — `test_remove_wishlist_404_with_wrong_code_raises_wishlist_error_not_not_found` |
| 404 본문 없음 | 기존 커버 — `test_remove_wishlist_404_empty_body_raises_wishlist_error_not_not_found` |
| 403(계약엔 없음, 미상 코드 낙성 확인) | 기존 커버 — `test_remove_wishlist_forbidden_maps_to_wishlist_error` |
| 200 success:false(방어) | 기존 커버 — `test_remove_wishlist_200_success_false_raises_wishlist_error` |
| **500 `INTERNAL_ERROR`** | **빈칸 — 신규 추가** `test_remove_wishlist_500_maps_to_wishlist_error`(①번 의심 빈칸) |

### I-28 찜 목록 (`test_wishlist.py` "I-28 찜 목록")

| 행 | 상태 |
|---|---|
| 성공 파싱(`userId` query 배선 포함) | 기존 커버 — `test_get_wishlist_parses_items` |
| `purchaseState` 키 없음 → `None` | 기존 커버 — `test_get_wishlist_missing_purchase_state_key_defaults_to_none` |
| `purchaseState` 비기본값(SOLD_OUT/HIDDEN) | 기존 커버 — `test_get_wishlist_parses_non_default_purchase_states` |
| 항목 단위 파싱 견고성(미지 값·unhashable·non-object) | 기존 커버 — `test_get_wishlist_unknown_purchase_state_skips_only_that_item` 외 3건 |
| 전 항목 드리프트 fail-closed | 기존 커버 — `test_get_wishlist_all_items_unknown_purchase_state_fails_closed_not_empty` |
| `items` 최상위 타입 붕괴 | 기존 커버 — `test_get_wishlist_items_not_a_list_still_unavailable` |
| 0건 정상(404 아님) | 기존 커버 — `test_get_wishlist_empty_is_not_an_error` |
| 200 success:false(빈 목록으로 위장 금지) | 기존 커버 — `test_get_wishlist_200_success_false_does_not_masquerade_as_empty` |
| 500/400/401(범용 실패 degrade) | 기존 커버 — `test_get_wishlist_unavailable_on_failure`(`raise_for_status()` 로 모든 4xx/5xx 가 같은 `except (httpx.HTTPError, ...)` 분기로 수렴 — 400/401 전용 테스트는 같은 코드 경로 중복) |

**새로 채운 행은 1건**: I-27 500 매핑. 나머지 "의심 빈칸" ②·③은 이미 기존 단정이 정확한
배선 값(`client.calls` 의 dict 리터럴)까지 검증하고 있어 빈칸이 아니었다.

### I-25 수량 변경 — 2026-08-10 사람 지시로 이 레인이 구현

애초 이 이슈 범위 밖이었으나(§1 A 아래 참조) 사람의 지시로 함께 구현했다. 1단계는 어댑터
(`spring_client.py::change_cart_quantity`), 2단계는 대상 해소·라우팅까지 포함한 전체 스트림
(`app/agents/buyer/cart/quantity.py::stream_cart_quantity_change` + `classify_cart_utterance`
사다리 4-a + `decompose` 직접 산출)이다 — I-24/26/27/28 과 달리 이 계약은 어댑터뿐 아니라
발화 판정부터 대상 해소까지 이 레인에서 전부 새로 만들었다.

**1단계 — 어댑터**(`test_cart.py` "spring_client 배선 (I-25 수량 변경…)", §4.13 실패표 대응):

| 행 | 테스트 |
|---|---|
| 성공(userId, 최종 quantity 반환) | `test_change_cart_quantity_success_returns_final_quantity` |
| 성공(guestId) | `test_change_cart_quantity_success_guest_id_only` |
| 400 `CART_STOCK_INSUFFICIENT`(availableStock 있음) | `test_change_cart_quantity_stock_insufficient_carries_available_stock` |
| 400 `CART_STOCK_INSUFFICIENT`(availableStock 없음) | `test_change_cart_quantity_stock_insufficient_without_available_stock_is_none` |
| 400 `VALIDATION_ERROR`(재고 부족과 구분) | `test_change_cart_quantity_validation_error_is_not_stock_insufficient` |
| 404 `CART_ITEM_NOT_FOUND` | `test_change_cart_quantity_not_found_raises` |
| 404 code 불일치 | `test_change_cart_quantity_404_with_wrong_code_raises_cart_error_not_not_found` |
| 404 본문 없음 | `test_change_cart_quantity_404_empty_body_raises_cart_error_not_not_found` |
| 403(계약엔 없음, 미상 코드 낙성 확인) | `test_change_cart_quantity_forbidden_maps_to_cart_error` |
| 500 `INTERNAL_ERROR` | `test_change_cart_quantity_500_maps_to_cart_error` |
| 200 success:false(방어) | `test_change_cart_quantity_200_success_false_raises_cart_error` |
| 신원 query 0개 | `test_change_cart_quantity_rejects_zero_identity_queries` |
| 신원 query 2개 | `test_change_cart_quantity_rejects_two_identity_queries` |

**2단계 — 스트림**(`test_cart_quantity.py`, `stream_cart_quantity_change` 대상 해소·함정 회귀):

| 행 | 테스트 |
|---|---|
| 성공(최종 quantity 실림) | `test_quantity_change_success_reports_final_quantity` |
| 응답에 quantity 없음 → 요청값 폴백 | `test_quantity_change_response_missing_quantity_falls_back_to_requested_value` |
| 게스트 신원 | `test_quantity_change_guest_identity_also_works` |
| 익명 신원(호출 0회) | `test_quantity_change_anon_identity_asks_login_without_any_call` |
| **함정 1**(404 → 실패, I-24 와 정반대) | `test_quantity_change_item_not_found_is_a_failure_not_success` |
| **함정 3**(목표 수량 미상 → 어댑터 미호출, 되물음) | `test_quantity_change_unresolved_target_quantity_asks_without_calling_adapter` |
| 재고 부족 3분기(None/0/N 문구) | `test_quantity_change_stock_insufficient_unknown_amount` 외 2건 |
| `CartError` → 실패 | `test_quantity_change_cart_error_maps_to_failed` |
| 장바구니 조회 실패 → 실패 | `test_quantity_change_get_cart_failure_maps_to_failed` |
| 빈 장바구니 | `test_quantity_change_empty_cart_says_empty` |
| 대상 2건 이상 → 되물음(임의 선택 금지) | `test_quantity_change_ambiguous_name_match_asks_instead_of_picking_one` |
| 단건 자동 해소 | `test_quantity_change_single_item_auto_resolves_without_name` |
| 다건 무신호 → 되물음 | `test_quantity_change_multiple_items_no_name_or_signal_asks` |
| 이름 매칭(동적 수량 표지 보강 — 이름·표지 사이 숫자 처리) | `test_resolve_quantity_target_name_match_picks_named_item` |
| "전체 삭제"류 규칙 미이식 확인 | `test_resolve_quantity_target_no_all_or_recent_rule_exists` |
| 부정 표지로 대상 배제 | `test_quantity_change_negation_suppresses_target_resolution` |

**2단계 — 판별·라우팅**(`classify_cart_utterance` 사다리 4-a·`decompose`·`buyer/graph.py`):

| 행 | 테스트 |
|---|---|
| **함정 2**(치환 vs 합산 사다리 판정, "하나 더 담아줘" 대조 포함) | `test_classify_cart_utterance_quantity_ladder` |
| 부정 표지로 표지 매칭 무효화 | `test_classify_cart_utterance_quantity_negation_suppresses_marker` |
| "수량 바꾸지 마" 리터럴 회귀 | `test_classify_cart_utterance_quantity_literal_negation_phrase_stays_cart_add` |
| 2선 방어 위임(`stream_cart_add` → `stream_cart_quantity_change`) | `test_stream_cart_add_quantity_intent_delegates_to_quantity_change` |
| **함정 3**(decompose 파싱 — target_quantity 기본값 미부여) | `test_parse_cart_target_quantity_does_not_default_to_one` |
| 범위(1~99) 밖 값 → 클램프 대신 되물음 | `test_parse_cart_target_quantity_out_of_range_asks_instead_of_clamping` |
| float/문자열 숫자 강제 변환 | `test_parse_cart_target_quantity_coerces_float_and_string` |
| 1차 라우팅(decompose 직접 산출 → `stream_cart_quantity_change`) | `test_route_cart_quantity` |

총 39건(1단계 13 + 2단계 26) — §4.13 실패표(성공·400 재고부족·400 검증오류·404·신원 방어)를
전부 덮고, 함정 1·2·3 을 각각 전용 회귀 테스트로 고정했다.

## 6. 이슈 완료 조건(Acceptance) 충족 근거

이슈 #285 본문 「진행 순서 (Acceptance)」의 실제 5개 항목(`gh issue view 285`, 확인일
2026-08-10)에 1:1 대응한다.

| # | 조건 | 상태 | 근거 |
|---|---|---|---|
| 1 | 초안 문서 작성(A·B·C±D — 요청/응답/오류 표 완성형, C-4/M-5/M-6·I-2/I-18 참조 명시) — `docs/proposals/` | ✅ **형태 상이** | 당시 팀이 "계약 정본은 Notion 이므로 리포에는 문서를 남기지 않습니다"라고 결정해(이슈 #285 코멘트 2026-08-04 마지막 줄 · `CHANGELOG.md` #285 항목) 초안 단계를 건너뛰고 정본에 직접 등재했다. **이 문서가 초안이 아니라 그 경로의 사후 검증 기록**으로 들어간다 |
| 2 | **BE 협의**(사람) — C-4/M-5/M-6 실측 정합 확인 포함, 결과를 이슈 코멘트로 | ✅ | 협의는 실제로 있었다(정본 확정 2026-08-05 — api-spec §4.12~4.16 "확정 2026-08-05" 표기). 다만 **그 결과가 이슈 코멘트에는 아직 기록되지 않았다** — 이 PR 과 함께 이슈 코멘트로 기록한다 |
| 3 | 합의안 **정본 등재**(Notion — 사람/BE), I-24~ 번호 부여 | ✅ | 이슈 #285 코멘트(2026-08-04, I-24~I-28 등재 표) + BE 소스 주석이 "노션 I-27·I-28"을 직접 인용(§3 Q7 처분 근거 — 사본이 스스로 정본을 주장하는 것보다 강한 제3자 방증) |
| 4 | 사본 `docs/api-spec.md` 동기화 + CH-2 `action` 확장분 동기화 | ✅ | api-spec §4.12~4.16 등재 · §3.1 `action` 10종 등재 |
| 5 | #116·#117 `blocked:spring` 해제 + 구현 착수 안내 | ✅ | 두 이슈 모두 `CLOSED`이고 라벨에 `blocked:spring` 없음(`gh issue view 116 117`, 확인일 2026-08-10) — 구현 코드는 `remove.py`·`wishlist.py`로 배포됨 |

**조건 2·3 은 이슈가 "(사람)"·"(Notion — 사람/BE)"라고 명시한 항목이다** — AI 가 대신
협의한 것처럼 쓰지 않는다. **이미 있었던 협의 결과를 이번 검증 레인이 실측으로 확인해
기록한 것**이 정확한 서술이다.

## 7. 남은 것

- **Q11 미결** — `GET /internal/wishlist` 전량 반환 응답의 크기 상한이 아직 정해지지 않았다.
  `spring_client.py::get_wishlist` 는 상한이 없다는 전제로 클라 측 절단을 두지 않는다.
- **Q17 범위 밖** — `X-Internal-Token` 발급·교환 절차는 I-2 때부터 남은 잔여 안건이며 #285
  범위가 아니다(api-spec §4.1 C-3·§5 C-1).
