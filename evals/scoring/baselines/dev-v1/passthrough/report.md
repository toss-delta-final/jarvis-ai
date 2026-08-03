# 구매자 추천 품질 평가

- dataset: `1.0.0` / `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`
- algorithm/config: `buyer-metrics-v1` / `buyer-eval-config-v1`
- model config: `{"decompose":"expectedFilters","provider":"scripted","rerank":"searchOrderPassthrough"}`
- 기본 scripted adapter는 expectedFilters를 decompose 출력으로 사용하므로 Filter Accuracy 1.0이 구조적으로 기대됩니다(#144에서 실모델로 교체).
- PR gate constraints: priceMax, priceMin; 판단 의존 mustExclude·forbiddenCategory·forbiddenProductId는 전부 보고하되 #144 전까지 gate하지 않습니다.

## 전체

| metric | value |
|---|---:|
| cases | 31 |
| ranking cases | 18 |
| filter accuracy | 1.000000 |
| hard constraint violation rate | 0.032258 |
| coverage | 0.556034 |
| diversity | 0.659140 |
| MRR | 0.794974 |

## 순위 지표 분모 제외

- count: 13
- caseId: buy-cmap-0002, buy-cmap-0005, buy-fail-0001, buy-fail-0002, buy-fail-0003, buy-gust-0001, buy-mult-0002, buy-pers-0003, buy-repu-0003, buy-srch-0001, buy-srch-0003, buy-srch-0004, buy-srch-0006

## 위반 제약

| caseId | productId | constraint |
|---|---:|---|
| buy-cmap-0004 | 1679183612 | mustExclude |

## Slice

| slice | cases | HCV |
|---|---:|---:|
| category_mapping_failure | 9 | 0.111111 |
| cold_start | 2 | 0.000000 |
| failure | 5 | 0.000000 |
| guest | 20 | 0.050000 |
| multi_constraint | 4 | 0.000000 |
| personalization | 8 | 0.000000 |
| personalization_overreach | 3 | 0.000000 |
| repurchase | 3 | 0.000000 |
| search | 26 | 0.038462 |
