## 변경 요약

`color_synonyms` 2차 사람 검수로 독립 색명과 자명한 표기 상이만 확정했다. 승인 수는 46→93행
(앵커 15→22행), 반려는 40행이며, 수식어 결합·복합색·데님 밝기 축·애매한 재질/상품명 조각은
`pending_review`로 남긴다.

대표적으로 `카멜→브라운`, `버건디→와인`, `바이올렛→퍼플`을 승인했고, 기계가 `카라멜→베이지`로
배정한 것은 갈색 계열인 `브라운`으로 정정했다. `진청→다크그레이`, `다크모스→다크그레이`,
`청록→스카이블루`처럼 색상 축을 바꾸거나 인접색을 합치는 배정은 승인하지 않았다.

## 정본과 검증

- 사람이 유지하는 원본은 `db/catalog/seed/color_synonyms_review.json`이며, 각 2차 승인·반려에
  한국어 근거를 남겼다.
- `db/catalog/seed/color_synonyms.json` 및 `db/catalog/init/05_color_synonyms_seed.sql`은 오버레이에서
  재파생한 생성물이다.
- 시드 불변식의 승인 수·지문·앵커 집합은 새 생성물의 실제 값으로 갱신했으며, 단언은 약화하지
  않았다.

## 운영 반영 절차 (사람 게이트 — 이 PR에서는 실행하지 않음)

기존 pg-catalog 볼륨에는 init SQL이 자동 재실행되지 않는다. 운영 승인 후에만 다음 순서로 적용한다.

```bash
docker exec -i <pg-catalog-container> psql -U <user> -d <catalog-db> \
  < db/catalog/init/05_color_synonyms_seed.sql
uv run python -c '
from app.core.config import get_settings
from app.pipelines.color_synonym_seed import seed_from_file
seed_from_file("db/catalog/seed/color_synonyms.json", dsn=get_settings().catalog_db_url)
'
```

## 관련

**Part of #505**

이 변경은 #505를 닫지 않는다. 플래그 활성화와 운영 A/B 실측은 별도 사람·인프라 게이트다.
