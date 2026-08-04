# intent 라우팅 프로브 리포트

prompt=e5e195822495 (git:3f1dec7) · tier=fast · model=gpt-5-nano · fixture=intent-probe-anchors-b-v1 · N=8

#240 축 순서 요약: `237/144/93/27/0/48/37/5`
(순서: mainIntent / cartControl / demonstrative / optionAnswer / switchLegacy2 / orderStatus / general / cartAddProductIdLegacy2)

> **이건 골든셋이 아니다.** 추천 품질이 아니라 intent 라우팅 분포를 잰 표다.

## 축

| 축 | 점수 | 분자 정의 | 분모 정의 |
|---|---|---|---|
| `cartAddProductIdLegacy2` cart_add productId(#234 정의) | 5/16 (31.2%) | intent 가 cart_add 이고 cart.productId 가 LAST_RECOMMENDATIONS 안에 있는 표본 수 — **되물음 상품을 그대로 에코해도 정답으로 센다**. #234 정의이며 switchLegacy2 와 같은 표본을 다른 정의로 다시 센 값이다 | switchLegacy2 와 동일 표본(전환 2발화 × PENDING_CART × N) |
| | | ⚠ 직접 비교 금지: switchLegacy2, #240 전환 축 | |
| `cartControl` 장바구니 대조군 | 144/144 (100.0%) | intent 가 기대 intent(cart_view 또는 cart_add)와 일치한 표본 수 | 장바구니 대조군 6발화 × 컨텍스트 3종 × N |
| `demonstrative` 지시대명사 | 93/96 (96.9%) | intent 가 recommend 인 표본 수 | 지시대명사 4발화 × 컨텍스트 3종 × N |
| `general` general | 37/48 (77.1%) | intent 가 general 인 표본 수 | general 2발화 × 컨텍스트 3종 × N |
| `mainIntent` 본 표 intent | 237/240 (98.8%) | intent 가 기대 intent 와 일치한 표본 수 (장바구니 대조군 + 지시대명사) | 장바구니 대조군 6발화 + 지시대명사 4발화 × 컨텍스트 3종 × N |
| `optionAnswer` 옵션 답변 | 27/32 (84.4%) | intent 가 cart_add 이고 cart.optionId 까지 기대값과 일치한 표본 수 (하나만 맞으면 오답 — 옵션을 잘못 담는 것은 실패다) | 옵션 답변 4발화 × PENDING_CART 컨텍스트 × N |
| `orderStatus` order_status | 48/48 (100.0%) | intent 가 order_status 인 표본 수 | order_status 2발화 × 컨텍스트 3종 × N |
| `switchAll7` 전환(#260 7발화) | 29/56 (51.8%) | switchLegacy2 와 같은 술어를 전환 7발화 전부에 적용한 표본 수 | 전환 7발화 × PENDING_CART 컨텍스트 × N |
| | | ⚠ 직접 비교 금지: switchLegacy2, #240 전환 축(분모 16) | |
| `switchLegacy2` 전환(#240 2발화) | 0/16 (0.0%) | intent 가 cart_add 이고 cart.productId 가 **되물음 상품이 아닌** LAST_RECOMMENDATIONS 안의 상품인 표본 수 — #240 정의 | #240 이 쓴 전환 2발화(`이어폰으로 할래`·`다른 거 담아줘`) × PENDING_CART × N |
| | | ⚠ 직접 비교 금지: cartAddProductIdLegacy2, switchAll7, #234 productId 표 | |

## 진단 (합불 아님)

- 되물음 상품 에코(위험한 실패): 8
- productId null(안전한 퇴화): 0

## 셀별 intent 분포

| 셀 | 표본 | 시도 | intent 분포 |
|---|---|---|---|
| `cart-control-001\|lastRecommendations` | 8 | 8 | cart_view 8 |
| `cart-control-001\|none` | 8 | 8 | cart_view 8 |
| `cart-control-001\|pendingCart` | 8 | 8 | cart_view 8 |
| `cart-control-002\|lastRecommendations` | 8 | 8 | cart_view 8 |
| `cart-control-002\|none` | 8 | 8 | cart_view 8 |
| `cart-control-002\|pendingCart` | 8 | 8 | cart_view 8 |
| `cart-control-003\|lastRecommendations` | 8 | 8 | cart_view 8 |
| `cart-control-003\|none` | 8 | 8 | cart_view 8 |
| `cart-control-003\|pendingCart` | 8 | 8 | cart_view 8 |
| `cart-control-004\|lastRecommendations` | 8 | 8 | cart_add 8 |
| `cart-control-004\|none` | 8 | 8 | cart_add 8 |
| `cart-control-004\|pendingCart` | 8 | 8 | cart_add 8 |
| `cart-control-005\|lastRecommendations` | 8 | 8 | cart_add 8 |
| `cart-control-005\|none` | 8 | 8 | cart_add 8 |
| `cart-control-005\|pendingCart` | 8 | 8 | cart_add 8 |
| `cart-control-006\|lastRecommendations` | 8 | 8 | cart_add 8 |
| `cart-control-006\|none` | 8 | 8 | cart_add 8 |
| `cart-control-006\|pendingCart` | 8 | 8 | cart_add 8 |
| `demonstrative-001\|lastRecommendations` | 8 | 8 | general 1, recommend 7 |
| `demonstrative-001\|none` | 8 | 8 | recommend 8 |
| `demonstrative-001\|pendingCart` | 8 | 8 | recommend 8 |
| `demonstrative-002\|lastRecommendations` | 8 | 8 | recommend 8 |
| `demonstrative-002\|none` | 8 | 8 | recommend 8 |
| `demonstrative-002\|pendingCart` | 8 | 8 | cart_add 1, recommend 7 |
| `demonstrative-003\|lastRecommendations` | 8 | 8 | recommend 8 |
| `demonstrative-003\|none` | 8 | 8 | recommend 8 |
| `demonstrative-003\|pendingCart` | 8 | 8 | recommend 8 |
| `demonstrative-004\|lastRecommendations` | 8 | 8 | general 1, recommend 7 |
| `demonstrative-004\|none` | 8 | 8 | recommend 8 |
| `demonstrative-004\|pendingCart` | 8 | 8 | recommend 8 |
| `general-001\|lastRecommendations` | 8 | 8 | general 8 |
| `general-001\|none` | 8 | 8 | general 8 |
| `general-001\|pendingCart` | 8 | 8 | general 8 |
| `general-002\|lastRecommendations` | 8 | 8 | recommend 8 |
| `general-002\|none` | 8 | 8 | general 8 |
| `general-002\|pendingCart` | 8 | 8 | general 5, recommend 3 |
| `option-answer-001\|pendingCart` | 8 | 8 | cart_add 8 |
| `option-answer-002\|pendingCart` | 8 | 8 | cart_add 8 |
| `option-answer-003\|pendingCart` | 8 | 8 | cart_add 8 |
| `option-answer-004\|pendingCart` | 8 | 8 | cart_add 8 |
| `order-status-001\|lastRecommendations` | 8 | 8 | order_status 8 |
| `order-status-001\|none` | 8 | 8 | order_status 8 |
| `order-status-001\|pendingCart` | 8 | 8 | order_status 8 |
| `order-status-002\|lastRecommendations` | 8 | 8 | order_status 8 |
| `order-status-002\|none` | 8 | 8 | order_status 8 |
| `order-status-002\|pendingCart` | 8 | 8 | order_status 8 |
| `switch-001\|pendingCart` | 8 | 8 | cart_add 1, recommend 7 |
| `switch-002\|pendingCart` | 8 | 8 | cart_add 4, cart_view 2, recommend 2 |
| `switch-003\|pendingCart` | 8 | 8 | cart_add 5, cart_view 3 |
| `switch-004\|pendingCart` | 8 | 8 | cart_add 8 |
| `switch-005\|pendingCart` | 8 | 8 | cart_add 3, recommend 5 |
| `switch-006\|pendingCart` | 8 | 8 | cart_add 8 |
| `switch-007\|pendingCart` | 8 | 8 | cart_add 8 |

## 채우지 못한 셀

(없음)

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.
3. 픽스처 문자열이 정답 신호와 겹치면 안 된다(옵션 이름이 상품명에 섞이면 옵션 축이 무너진다).
4. 단일 실행은 채택 판정이 아니다 — 축당 ±2, 특정 셀은 2/8~6/8 까지 흔들린다. 독립 2~3회로 판정.

페이싱 실측: 대기 202회 / 허용 45 rpm.
