# 구매자 골든셋 누출 감사

- 위반: **0건**
- 경고: **16건**
- dev/holdout: 103/24

## 경고와 사유

- `buy-dirc-0001` ↔ `buy-invc-0004`: query 3-gram Jaccard 0.625 (동일 split의 의도적 표현 변형)
- `buy-dirm-0001` ↔ `buy-dirm-0002`: query 3-gram Jaccard 1.000 (동일 split의 의도적 표현 변형)
- `buy-dirm-0001` ↔ `buy-invc-0001`: query 3-gram Jaccard 0.615 (동일 split의 의도적 표현 변형)
- `buy-dirm-0001` ↔ `buy-invc-0002`: query 3-gram Jaccard 0.667 (동일 split의 의도적 표현 변형)
- `buy-dirm-0002` ↔ `buy-invc-0001`: query 3-gram Jaccard 0.615 (동일 split의 의도적 표현 변형)
- `buy-dirm-0002` ↔ `buy-invc-0002`: query 3-gram Jaccard 0.667 (동일 split의 의도적 표현 변형)
- `buy-dirm-0003` ↔ `buy-dirm-0004`: query 3-gram Jaccard 1.000 (동일 split의 의도적 표현 변형)
- `buy-dirm-0005` ↔ `buy-dirm-0006`: query 3-gram Jaccard 1.000 (동일 split의 의도적 표현 변형)
- `buy-invc-0001` ↔ `buy-invc-0002`: query 3-gram Jaccard 0.615 (동일 split의 의도적 표현 변형)
- `buy-invc-0011` ↔ `buy-srch-0010`: query 3-gram Jaccard 0.636 (동일 split의 의도적 표현 변형)
- `buy-invw-0011` ↔ `buy-pers-0004`: query 3-gram Jaccard 1.000 (동일 split의 의도적 표현 변형)
- `buy-repu-0001` ↔ `buy-srch-0003`: query 3-gram Jaccard 0.636 (동일 split의 의도적 표현 변형)
- `buy-fail-1003` ↔ `buy-fail-1004`: query 3-gram Jaccard 0.882 (동일 split의 의도적 표현 변형)
- `buy-fail-1003` ↔ `buy-fail-1005`: query 3-gram Jaccard 0.882 (동일 split의 의도적 표현 변형)
- `buy-fail-1004` ↔ `buy-fail-1005`: query 3-gram Jaccard 0.882 (동일 split의 의도적 표현 변형)
- holdout 비중 0.189: 목표 0.300에서 허용 오차 0.050보다 크게 벗어남

후보가 전부 정답인 케이스들은 순위 지표(nDCG·MRR·Precision@k)의 분모에서 제외해야 한다. 노출·필터·하드제약 검증에는 계속 사용한다.

## 커버리지

```json
{
  "dev": {
    "budget": 12,
    "category_mapping_failure": 9,
    "cold_start": 2,
    "failure": 5,
    "guest": 68,
    "member": 35,
    "multi_constraint": 12,
    "personalization": 11,
    "personalization_overreach": 6,
    "repurchase": 8,
    "search": 62,
    "single_need": 71
  },
  "holdout": {
    "budget": 0,
    "category_mapping_failure": 6,
    "cold_start": 0,
    "failure": 5,
    "guest": 12,
    "member": 12,
    "multi_constraint": 0,
    "personalization": 7,
    "personalization_overreach": 0,
    "repurchase": 6,
    "search": 19,
    "single_need": 18
  }
}
```

## v1 커버리지 한계

- holdout cold_start slice 표본이 0건입니다
- holdout multi_constraint slice 표본이 0건입니다
- holdout personalization_overreach slice 표본이 0건입니다
