# intent 라우팅 프로브 리포트

prompt=e5e7f9b8d844 (repo:_SYSTEM) · scope=6c54c2656063 · tier=fast · model=gpt-5-nano · fixture=intent-probe-anchors-b-v4 · N=8

#240 축 순서 요약: `0/0/0/0/0/0/0/0`
(순서: mainIntent / cartControl / demonstrative / optionAnswer / switchLegacy2 / orderStatus / general / cartAddProductIdLegacy2)

> **이건 골든셋이 아니다.** 추천 품질이 아니라 intent 라우팅 분포를 잰 표다.

## 축

| 축 | 점수 | 분자 정의 | 분모 정의 |
|---|---|---|---|
| `cartAddProductIdLegacy2` cart_add productId(#234 정의) | 0/0 | intent 가 cart_add 이고 cart.productId 가 LAST_RECOMMENDATIONS 안에 있는 표본 수 — **되물음 상품을 그대로 에코해도 정답으로 센다**. #234 정의이며 switchLegacy2 와 같은 표본을 다른 정의로 다시 센 값이다 | switchLegacy2 와 동일 표본(전환 2발화 × PENDING_CART × N) |
| | | ⚠ 직접 비교 금지: switchLegacy2, #240 전환 축 | |
| `cartControl` 장바구니 대조군 | 0/0 | intent 가 기대 intent(cart_view 또는 cart_add)와 일치한 표본 수 | 장바구니 대조군 6발화 × 컨텍스트 3종 × N |
| `categoryAction3Way` 카테고리 승계 3분기 | 0/0 | 확정값(resolvedCategoryAction)이 기대 carry·clear·replace 와 일치한 표본 수 (carry 기대는 확정값이 replace 라도 그 leg 이 **전부** 직전 카테고리 에코면 정답으로 센다 — 프롬프트가 리파인 턴에 PRIOR_FILTERS.category 를 categoryQueries 로 복사하라고 지시해서 나오는 모양이고, 결과적으로 카테고리가 유지되므로 사용자가 겪는 동작이 carry 와 같다. 에코 판정은 앵커 categoryPriorFilters(카테고리 전체·각 조각·semanticQuery)와 **정규화 후 정확 일치**다 — 부분 문자열이면 `이어폰 케이스` 같은 새 상품도 에코로 세어 카테고리가 바뀐 턴을 '유지됐다'로 읽는다) | 카테고리 15발화 × categoryPrior 컨텍스트 × N (N=8 이면 120) — 라운드 3 이 혼합 4발화를 더해 88 → 120 이 됐다 |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런), baselines/fast-2026-08-05-84 (분모 88 — 혼합 4발화 추가 전) | |
| `categoryCarry` 리파인(직전 카테고리 유지) | 0/0 | resolvedCategoryAction 이 carry 인 표본 수 (**또는** replace 이면서 leg 이 **전부** 직전 카테고리 에코인 표본 — 프롬프트가 리파인 턴에 직전 카테고리를 leg 로 복사하라고 지시해서 나오는 모양이고, 카테고리는 유지된다. 에코는 앵커 categoryPriorFilters 와 정규화 후 **정확 일치**일 때만 인정한다) | 리파인 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런) | |
| `categoryClear` 카테고리-무관 리셋 | 0/0 | resolvedCategoryAction 이 clear 인 표본 수 | 리셋 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런) | |
| `categoryMixedReplace` 혼합 발화(새 카테고리 + 아무거나) | 0/0 | resolvedCategoryAction 이 replace 인 표본 수 — 새 카테고리를 지목하면서 동시에 '아무거나'류 표현을 쓴 발화다. 초판(scopeFree 우선)에서는 사용자가 말한 카테고리가 통째로 버려져 무필터가 됐다(실 LLM 실측 32건 중 19건 clear) | 혼합 4발화 × categoryPrior 컨텍스트 × N (N=8 이면 32) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런) | |
| `categoryReplace` 카테고리 교체 | 0/0 | resolvedCategoryAction 이 replace 인 표본 수 | 교체 3발화 × categoryPrior 컨텍스트 × N (N=8 이면 24) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런) | |
| `demonstrative` 지시대명사 | 0/0 | intent 가 recommend 인 표본 수 | 지시대명사 4발화 × 컨텍스트 3종 × N |
| `general` general | 0/0 | intent 가 general 인 표본 수 | general 2발화 × 컨텍스트 3종 × N |
| `mainIntent` 본 표 intent | 0/0 | intent 가 기대 intent 와 일치한 표본 수 (장바구니 대조군 + 지시대명사) | 장바구니 대조군 6발화 + 지시대명사 4발화 × 컨텍스트 3종 × N |
| `optionAnswer` 옵션 답변 | 0/0 | intent 가 cart_add 이고 cart.optionId 까지 기대값과 일치한 표본 수 (하나만 맞으면 오답 — 옵션을 잘못 담는 것은 실패다) | 옵션 답변 4발화 × PENDING_CART 컨텍스트 × N |
| `orderStatus` order_status | 0/0 | intent 가 order_status 인 표본 수 | order_status 2발화 × 컨텍스트 3종 × N |
| `screenExactPick` 화면 지시어 확정 | 31/32 (96.9%) | resolvedProductId(해소기 통과 후 최종값) 가 expected.productId 와 일치한 표본 수 | 확정 4발화(screen-001·003·004·005) × 1 컨텍스트 × N (N=8 이면 32) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런), baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런), baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런), #118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표(48/48 · 프롬프트 층만 9/48) | |
| `screenNoHallucination` 화면 밖 id 확정 금지 | 8/8 (100.0%) | resolvedProductId(해소기 통과 후 최종값) 가 expected.forbiddenProductId 와 다른 표본 수 | 확정금지 1발화(screen-006) × 1 컨텍스트 × N (N=8 이면 8) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런), baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런), baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런), #118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표(48/48 · 프롬프트 층만 9/48) | |
| `screenReask` 화면 지시어 되물음(안전 셀) | 8/8 (100.0%) | resolvedProductId(해소기 통과 후 최종값) 가 None 인 표본 수(임의 확정하지 않고 되물음으로 흐른다) | 되물음 1발화(screen-002) × 1 컨텍스트 × N (N=8 이면 8) |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런), baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런), baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런), #118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표(48/48 · 프롬프트 층만 9/48) | |
| `screenResolution` 화면 지시어 해소 종합 | 47/48 (97.9%) | screenExactPick·screenReask·screenNoHallucination 셋의 합 — 각 표본은 자신의 productIdRule(screenExact\|screenReask\|screenNotHallucinated)이 가리키는 술어 하나로만 채점된다 | screen 6발화 × 1 컨텍스트 × N (N=8 이면 48) — #118 의 48/48 과 같은 분모 |
| | | ⚠ 직접 비교 금지: baselines/fast-2026-08-04 (이 축이 존재하지 않던 런), baselines/fast-2026-08-04-prompt-e5e19582 (이 축이 존재하지 않던 런), baselines/fast-2026-08-05-84 (이 축이 존재하지 않던 런), #118 원 프로브(이관 전, #300 흡수 후 삭제됨)의 adopted 변형 표(48/48 · 프롬프트 층만 9/48) | |
| `switchAll7` 전환(#260 7발화) | 0/0 | switchLegacy2 와 같은 술어를 전환 7발화 전부에 적용한 표본 수 | 전환 7발화 × PENDING_CART 컨텍스트 × N |
| | | ⚠ 직접 비교 금지: switchLegacy2, #240 전환 축(분모 16) | |
| `switchLegacy2` 전환(#240 2발화) | 0/0 | intent 가 cart_add 이고 cart.productId 가 **되물음 상품이 아닌** LAST_RECOMMENDATIONS 안의 상품인 표본 수 — #240 정의 | #240 이 쓴 전환 2발화(`이어폰으로 할래`·`다른 거 담아줘`) × PENDING_CART × N |
| | | ⚠ 직접 비교 금지: cartAddProductIdLegacy2, switchAll7, #234 productId 표 | |

## 진단 (합불 아님)

- 되물음 상품 에코(위험한 실패): 0
- productId null(안전한 퇴화): 0
- 범위 해제 분류기 무판정(None): 0
- 리파인이 clear 로 풀림(이 변경의 새 회귀 모양): 0
- 화면 지시어: 해소기 전 프롬프트 층만으로 규칙 충족: 31
- 화면 지시어: 해소기 발동(override) 표본 수: 39
- 화면 지시어: 두 목록 밖 productId 확정(위험한 실패, 0 이어야 함): 0

## 셀별 intent 분포

| 셀 | 표본 | 시도 | intent 분포 |
|---|---|---|---|
| `screen-001\|screenSingle` | 8 | 8 | cart_add 8 |
| `screen-002\|screenTriple` | 8 | 8 | cart_add 8 |
| `screen-003\|screenFive` | 8 | 8 | cart_add 7, recommend 1 |
| `screen-004\|screenNine` | 8 | 8 | cart_add 8 |
| `screen-005\|screenNamed` | 8 | 8 | cart_add 8 |
| `screen-006\|screenTriple` | 8 | 8 | cart_add 8 |

## 채우지 못한 셀

(없음)

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.
3. 픽스처 문자열이 정답 신호와 겹치면 안 된다(옵션 이름이 상품명에 섞이면 옵션 축이 무너진다).
4. 단일 실행은 채택 판정이 아니다 — 축당 ±2, 특정 셀은 2/8~6/8 까지 흔들린다. 독립 2~3회로 판정.

페이싱 실측: 대기 2회 / 허용 45 rpm.
