# 카테고리 사전 시드 (이슈 #401)

발화→카테고리 매핑(임베딩 top-k + LLM 택일 하이브리드, `app/agents/buyer/recommendation/
category_mapping.py`)이 근거로 삼는 카테고리 leaf 사전의 정본과 적재 절차.

## 무엇이 정본인가

`categories.json` — leaf **1,007개 문자열 배열**("대분류 > 중분류" 형태). UTF-8,
`ensure_ascii=False`, `indent=2`, 끝에 개행 1개. 정렬은 **파이썬 codepoint 오름차순**
(`sorted()`) — locale/collation 에 좌우되지 않는 결정론적 순서다.

**임베딩 벡터는 커밋하지 않는다.** 여기 있는 건 문자열 목록뿐이다 — 임베딩은 별도 배치가
Gemini API 를 호출해 만든다(아래 §적재 절차 ③). 벡터를 정본에 넣으면 임베딩 모델이 바뀔 때마다
정본 자체를 재생성해야 하고, API 키 없이는 정본을 검증할 수도 없어진다.

## 파생 절차

정본은 손으로 고치지 않는다. MariaDB `category` 테이블 덤프(원본 크롤 카탈로그, repo 밖 —
`~/inte-final/_sql/mariadb/10_category.sql` 등)에서 `scripts/derive_category_seed.py` 로
재생성한다:

```bash
uv run python scripts/derive_category_seed.py <덤프 SQL 경로>
# 커밋본과 같은지만 확인(쓰지 않음, 비0 종료 시 실패):
uv run python scripts/derive_category_seed.py <덤프 SQL 경로> --check
```

덤프가 없어도 **라이브 MariaDB** 에 아래 질의로 leaf 목록을 직접 뽑을 수 있다(카테고리는
"도메인(부모 NULL) → leaf(부모 = 도메인)" 2단계 평평화 트리다):

```sql
SELECT name FROM category WHERE parent_id IS NOT NULL;
```

이 결과를 codepoint 정렬해 JSON 배열로 저장하면 `categories.json` 과 동등하다(스크립트는
추가로 leaf 이름 전역 유일성·헤더 행 수 등을 검증한다 — 자세한 파서 규약은
`scripts/derive_category_seed.py` docstring 참고).

## 적재 절차

① **fresh 볼륨** — `db/catalog/init/04_categories_seed.sql` 이
`docker-entrypoint-initdb.d`(`02_categories.sql` 뒤 번호)로 자동 실행돼 `categories` 행이
채워진다(embedding 은 아직 NULL).

② **기존 볼륨**(이미 뜬 컨테이너) — init 스크립트는 재실행되지 않으므로 수동 적용한다:

```bash
docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/04_categories_seed.sql
```

③ **임베딩** — 행 생성과 임베딩 구축은 2단계로 분리돼 있다(`②` 직후엔 embedding 전부 NULL).
`app.pipelines.category_seed.seed_from_file` 배치가 Gemini API 키(`GOOGLE_API_KEY` →
`app/core/config.py` `Settings.google_api_key`, `app/pipelines/embedding.py` 가 이 값을 읽는다)
로 leaf 를 임베딩해 채운다:

```python
from app.core.config import get_settings
from app.pipelines.category_seed import seed_from_file

settings = get_settings()
seed_from_file("db/catalog/seed/categories.json", dsn=settings.catalog_db_url)
```

행만 있고 embedding 이 채워지지 않으면 `search_categories_pg`(`WHERE embedding IS NOT NULL`)
입장에서는 사전이 0행인 것과 똑같이 죽는다 — ③ 을 빠뜨리면 ①·② 는 아무 의미가 없다. 기동/배치
가드(`app/pipelines/category_seed.check_category_dictionary`)가 이 두 상태(0행 vs 0임베딩)를
모두 구성 오류로 잡는다.

## 지문 2종

카테고리 사전 상태를 재는 sha256 지문이 **둘** 있고, 서로 다른 값이 나오는 게 정상이다 — 정렬
기준이 다르기 때문이다.

| 지문 | 정렬 기준 | 값(leaf 1,007) | 어디서 쓰나 |
|---|---|---|---|
| codepoint | 파이썬 `sorted()` | `db81e849616ec5782f9d1b4ecda1f6eb15f9dbc7a2ec939b40e33fa786d65089` | `categories.json` 정본, `scripts/derive_category_seed.py`, `evals/category_probe/manifest.py::fingerprint_rows`/`canonical_seed_fingerprint` |
| en_US.utf8(DB collation) | `ORDER BY category`(pg-catalog DB collation) | `fb9ca975af1ea86ce013caeb018b7adcefc80a96d529aad0dd0555e464f21fe6` | `evals/category_probe/manifest.py::dictionary_fingerprint` 의 기존 `rowCount`/`sha256`(`dictionaryHash`, 과거 런과 비교용이라 보존) |

**collation 차이가 핵심 제약이다** — en_US.utf8 정렬은 파이썬에서 재현할 수 없어서(DB 마다,
locale 데이터 버전마다 미묘하게 다를 수 있음) 기존 `dictionaryHash` 필드는 그대로 두고,
collation 무관한 codepoint 지문을 **추가**해 정본과 대조한다(`dictionary_fingerprint()` 의
`canonicalSha256`/`seed`/`matchesSeed`, 자세한 내용은 `evals/category_probe/README.md`).

## 사전이 갱신될 때 할 일 체크리스트

카테고리 taxonomy 자체가 바뀌면(예: 새 크롤 배치, leaf 이름 정정) 순서대로:

1. **재파생** — `uv run python scripts/derive_category_seed.py <새 덤프>` 로 `categories.json`·
   `04_categories_seed.sql` 을 다시 만든다.
2. **SQL 재생성 확인** — 위 명령이 두 파일을 같은 원천에서 함께 만드므로 별도 단계는 없다.
   diff 로 실제 바뀐 leaf 를 훑는다.
3. **테스트의 sha 상수 갱신** — codepoint sha256·행 수 리터럴이 **두 파일**에 있다. 둘 다
   고친다: `tests/unit/test_category_seed_data.py`(`EXPECTED_LEAF_COUNT`/
   `EXPECTED_CODEPOINT_SHA256`)와 `tests/unit/test_category_probe_manifest_fingerprint.py`
   (동명 상수). 한쪽만 고치면 다른 쪽 테스트가 새 정본을 옛 지문과 비교해 실패한다.
4. **`evals/category_probe` 재측정** — `uv run python -m evals.category_probe.sweep --run
   <hits.csv 있는 런 디렉터리>` (README §run manifest). 사전이 바뀌면 `category_distance_max`
   등 임계의 근거가 stale 이 된다.
5. **`category_distance_max` 재검토** — 재측정 결과로 임계를 다시 정할지 판단한다
   (`app/core/config.py` 해당 필드 주석에 재튜닝 조건이 적혀 있다). 이 이슈(#401) 자체는
   임계값을 바꾸지 않는다 — 근거 사전을 repo 로 편입할 뿐이다.
