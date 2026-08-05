# 구매자 추천 품질 평가

- dataset: `2.1.0` / `d16eb0e98e28486e2e63a7218cb8b25a96f9ebe69dbe0a37c5b205a16f01efb1`
- algorithm/config: `buyer-metrics-v1` / `buyer-eval-config-v1`
- model config: `{"decompose":"expectedFilters","profileArm":"guest","profileSource":"caseDerivedFixtureArm","provider":"scripted","recentPurchaseWindowDays":90,"referenceDate":"2026-08-02","rerank":"deterministicScoringBaseline","weights":{"diversity_bonus":0.1,"popularity":0.15,"profile_match":0.6,"recency":0.05,"recent_purchase_penalty":0.2,"semantic":0.55}}`
- 기본 scripted adapter는 expectedFilters를 decompose 출력으로 사용하므로 Filter Accuracy 1.0이 구조적으로 기대됩니다(#144에서 실모델로 교체).
- PR gate constraints: priceMax, priceMin; 판단 의존 mustExclude·forbiddenCategory·forbiddenProductId는 전부 보고하되 #144 전까지 gate하지 않습니다.
- primary confirmatory metric은 `overall.ndcgAtK.10` 1개뿐이다 — 나머지 cutoff(3·5)와 슬라이스별 수치는 exploratory다(#328 다중비교 통제 규약).

## 전체

| metric | value |
|---|---:|
| cases | 96 |
| ranking cases | 55 |
| nDCG@10 (primary) | 0.461219 |
| filter accuracy | 1.000000 |
| hard constraint violation rate | 0.000000 |
| coverage | 0.322794 |
| diversity | 0.840278 |
| MRR | 0.592496 |
| candidate depth (min/median/max) | 30/30/30 |
| candidates ≤10 (count/ratio) | 0/0.0000 |

## 순위 지표 분모 제외

- count: 41
- caseId: buy-cmap-0005, buy-dirc-0001, buy-dirc-0002, buy-dirc-0003, buy-dirc-0004, buy-dirc-0005, buy-dirc-0006, buy-dirm-0001, buy-dirm-0002, buy-dirm-0003, buy-dirm-0004, buy-dirm-0005, buy-dirm-0006, buy-fail-0001, buy-fail-0002, buy-fail-0003, buy-invc-0001, buy-invc-0002, buy-invc-0003, buy-invc-0004, buy-invc-0005, buy-invc-0006, buy-invc-0007, buy-invc-0008, buy-invc-0009, buy-invc-0010, buy-invc-0011, buy-invc-0012, buy-invw-0001, buy-invw-0002, buy-invw-0003, buy-invw-0004, buy-invw-0005, buy-invw-0006, buy-invw-0007, buy-invw-0008, buy-invw-0009, buy-invw-0010, buy-invw-0011, buy-invw-0012, buy-repu-0003

## 위반 제약

- 없음

## no-op 기준선 비교

- 시스템이 실제로 노출한 상품 집합(중복 제거 후)을 productId 오름차순으로 재정렬한 것을 노출했다고 가정하는 기준선이다(#333 리뷰 F-4b — F-4의 fixture 후보 절단 정의를 대체). 목적은 순수 순서 효과 격리다: 같은 노출 집합 위에서 '시스템이 고른 순서' 대 '임의(오름차순) 순서'만 비교한다(#275의 no-op도 arm 간 동일 eligible 집합 위 순서 비교였다). fixture 후보 자체가 이미 productId 오름차순으로 기록되므로, 이는 '시스템 노출 집합에 fixture 순서를 적용한 것'과 동치다. 앱의 hard_filter·dedup을 거친 뒤의 실제 노출 집합을 그대로 쓰므로, F-4가 남겼던 '노출 집합 자체가 fixture 앞부분과 달라지는' 문제(#333 리뷰 F-4 스모크 실측)가 생기지 않는다.
- 서로 다른 `datasetHash` 점수와는 비교하지 않는다.

| metric | system | no-op | delta(system-noop) |
|---|---:|---:|---:|
| nDCG@10 | 0.461219 | 0.392246 | 0.068973 |
| MRR | 0.592496 | 0.471032 | 0.121465 |
| hard constraint violation rate | 0.000000 | 0.000000 | 0.000000 |

## Slice

| slice | N(ranking) | cases | HCV | label |
|---|---:|---:|---:|---|
| budget | 12 | 12 | 0.000000 | exploratory |
| category_mapping_failure | 8 | 9 | 0.000000 | exploratory |
| cold_start | 2 | 2 | 0.000000 | exploratory |
| failure | 0 | 5 | 0.000000 | exploratory |
| guest | 26 | 63 | 0.000000 | exploratory |
| member | 29 | 33 | 0.000000 | exploratory |
| multi_constraint | 10 | 12 | 0.000000 | exploratory |
| personalization | 11 | 11 | 0.000000 | exploratory |
| personalization_overreach | 6 | 6 | 0.000000 | exploratory |
| repurchase | 7 | 8 | 0.000000 | exploratory |
| search | 55 | 55 | 0.000000 | exploratory |
| single_need | 26 | 64 | 0.000000 | exploratory |
