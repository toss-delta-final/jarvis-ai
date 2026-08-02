# 구매자 골든셋 누출 감사

- 위반: **0건**
- 경고: **2건**
- dev/holdout: 31/12

## 경고와 사유

- `buy-srch-0003` ↔ `buy-repu-0001`: query 3-gram Jaccard 0.636 (동일 split의 의도적 표현 변형)
- 순위 판별 불가 후보/정답: `buy-srch-0001` (1/1), `buy-srch-0003` (2/2), `buy-gust-0001` (1/1), `buy-cmap-0002` (5/5), `buy-mult-0002` (5/5), `buy-pers-0003` (1/1), `buy-srch-0004` (1/1), `buy-srch-0006` (1/1)

후보가 전부 정답인 케이스들은 순위 지표(nDCG·MRR·Precision@k)의 분모에서 제외해야 한다. 노출·필터·하드제약 검증에는 계속 사용한다.

## 커버리지

```json
{
  "dev": {
    "category_mapping_failure": 9,
    "cold_start": 2,
    "failure": 5,
    "guest": 20,
    "multi_constraint": 4,
    "personalization": 8,
    "personalization_overreach": 3,
    "repurchase": 3,
    "search": 26
  },
  "holdout": {
    "category_mapping_failure": 2,
    "cold_start": 0,
    "failure": 2,
    "guest": 10,
    "multi_constraint": 0,
    "personalization": 2,
    "personalization_overreach": 0,
    "repurchase": 1,
    "search": 10
  }
}
```

## v1 커버리지 한계

- holdout cold_start slice 표본이 0건입니다
- holdout multi_constraint slice 표본이 0건입니다
- holdout personalization_overreach slice 표본이 0건입니다
