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

---

# 색상 동의어 사전 시드 (이슈 #258)

I-1 색상 질의 확장(§4.6 제안, `docs/specs/PROPOSAL-I1-COLOR-ARRAY-258.md`)이 근거로 삼는
색상 표기 동의어 사전의 정본과 적재 절차. 오프라인 구축 파이프라인(`app.pipelines.
color_synonym_seed.build`, I-17 수확 → LLM 배정 → 검증)이 만든 검수 큐를 사람이 1·2차 검수한
결과를 repo 에 고정한다.

## 무엇이 정본인가

정본은 **두 파일**로 나뉜다 — 하나는 손으로 유지하고, 하나는 그로부터 파생된다.

- **`color_synonyms_review.json`**(사람이 유지) — 1·2차 검수 결과. `approved`(승인, `{term,
  canonical, note}`)와 `rejected`(반려, `{term, note}`) 두 배열만 담는다. 지금은 90행이
  승인됐고 40행은 반려됐다(`db/catalog/seed/color_synonyms.json` 의 789행 중). 수식어 결합,
  복합색, 데님 밝기 축, 판단이 갈린 재질·상품명 조각은 `pending_review`로 남긴다.
- **`color_synonyms.json`**(생성물, 789행) — `scripts/derive_color_synonym_seed.py` 가
  라이브 pg-catalog `color_synonyms` 테이블(기계 산출: term/canonical/provenance/doc_count)
  위에 `color_synonyms_review.json` 오버레이를 적용해 만든다. 각 행은 `term` / `canonical`
  (nullable) / `status`(`approved`\|`pending_review`\|`rejected`) / `provenance` / `doc_count`.
  **손으로 고치지 마라** — 재생성한다.

**임베딩 벡터는 커밋하지 않는다.** 카테고리 사전(위 §)과 같은 이유 — 임베딩 모델이 바뀌면
정본 자체를 재생성해야 하고, API 키 없이는 정본을 검증할 수도 없어진다.

**원천 I-17(Spring) 은 현재(2026-08-07 실측, `scripts/check_spring_connection.py`) 도달
불가**다. 재수확이 불가능하므로 라이브 pg-catalog `color_synonyms` 가 사실상 유일한 사본이고,
그래서 이 시드는 카테고리 사전(위 §, MariaDB 덤프 파일 파싱)과 달리 **라이브 DB 에서 직접**
파생한다.

## 파생 절차

```bash
uv run python scripts/derive_color_synonym_seed.py [--dsn postgresql://...]
# 커밋본과 같은지만 확인(쓰지 않음, 비0 종료 시 실패):
uv run python scripts/derive_color_synonym_seed.py --check
```

`--dsn` 미지정 시 `app.core.config.get_settings().catalog_db_url`(기본
`postgresql://jarvis:jarvis@localhost:5433/catalog`)을 쓴다. 스크립트는 오버레이 내부
정합성(중복·교집합) → 하네스트 대비 존재성(`term`·`canonical` 이 수확 집합에 있는지) → 의미
규칙(고아 승인 금지·2단계 체인/순환 금지·`_norm` 충돌 없음·오버레이가 DB 의 과거 사람 검수
산출물(`provenance='human'`)을 전부 덮는지) 순으로 검증하고, 위반 시 어디가 문제인지 사람이
읽을 수 있는 메시지로 실패한다(조용히 무시하지 않는다).

**DB 를 잃었을 때도 재파생할 수 있다.** 이 시드가 존재하는 이유 자체가 "라이브 DB 를 날리면
I-17 재수확이 불가능하다"는 것이므로, 재파생 절차가 라이브 DB 만 전제하면 정작 DB 를 잃은
순간 오버레이를 고쳐도 재파생할 방법이 없다는 모순이 생긴다. 복원 경로는:

```bash
docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/05_color_synonyms_seed.sql
uv run python scripts/derive_color_synonym_seed.py --check
```

이 왕복은 **바이트 안정**이다 — 복원된 `color_synonyms` 는 승인·반려된 사람 검수 행이
`provenance='human'` 그대로 다시 들어오지만, 오버레이가 그 term 을 전부 덮고 있어
재파생 결과 정본은 복원 전과 완전히 동일하다(실측 확인: 임시 DB 에 적재 → 재파생 →
커밋본과 바이트 동일). 이 안정성은 **오버레이가 DB 의 human 행을 빠짐없이 덮는다**는 전제
위에 서 있고, 그 전제는 위 검증(오버레이 누락 시 실패)이 기동 시점이 아니라 파생 시점에
강제한다 — 검수를 철회하고 싶다면 오버레이에서 조용히 지우는 게 아니라 그 term 을
`rejected` 로 옮기거나 새 `approved` 값으로 명시해야 한다.

## 적재 절차

① **fresh 볼륨** — `db/catalog/init/05_color_synonyms_seed.sql` 이
`docker-entrypoint-initdb.d`(`03_color_synonyms.sql` 뒤 번호)로 자동 실행돼 `color_synonyms`
행이 채워진다(embedding 은 아직 NULL).

② **기존 볼륨**(이미 뜬 컨테이너) — init 스크립트는 재실행되지 않으므로 수동 적용한다:

```bash
docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/05_color_synonyms_seed.sql
```

이 재적재는 기존 행도 정본의 `status`/`canonical`/`provenance`/`doc_count`로 갱신하므로,
새 검수 회차에서 승인·반려로 승격된 결과가 기존 볼륨에도 반영된다. 임베딩과 임베딩 모델은
갱신하지 않아 이미 구축된 벡터는 보존된다.

③ **임베딩** — 행 생성과 임베딩 구축은 2단계로 분리돼 있다(`②` 직후엔 embedding 전부 NULL).
`app.pipelines.color_synonym_seed.seed_from_file` 배치가 Gemini API 키로 각 표기를 임베딩해
채운다. **`NON_COLOR_TERMS`(sentinel, 예: `혼합색상`·`기타`·`투명`) 는 임베딩하지 않는다.**

```python
from app.core.config import get_settings
from app.pipelines.color_synonym_seed import seed_from_file

settings = get_settings()
seed_from_file(
    "db/catalog/seed/color_synonyms.json",
    dsn=settings.catalog_db_url,
)
```

`seed_from_file` 은 파일의 `status`/`canonical`/`provenance`/`doc_count` 를 **항상 권위
있게(authoritative) 반영**한다 — 기존 배치 수확 upsert(`UPSERT_COLOR_TERM_SQL`, 사람 검수
결과를 보존하는 CASE 가드형)와는 다른 상수(`UPSERT_SEED_COLOR_TERM_SQL`)를 쓴다. 임베딩만
`COALESCE`로 기존 벡터를 보존한다.

## 검수 워크플로

**검수는 `color_synonyms_review.json` 에 적고 재파생·재커밋한다.** DB `color_synonyms` 테이블을
직접 `UPDATE` 해 `status`/`canonical` 을 바꾸면, 다음 `seed_from_file` 실행(또는 배포 재적재)
때 정본 파일 값으로 **되돌아간다** — 검수 결과의 유일한 보존처는 오버레이 파일이다.

새로운 표기를 승인하려면 `color_synonyms_review.json` 의 `approved` 배열에
`{"term": "...", "canonical": "...", "note": "..."}` 를 추가하고(반려면 `rejected` 배열에
`{"term": "...", "note": "..."}`), `derive_color_synonym_seed.py` 를 재실행해
`color_synonyms.json`·`05_color_synonyms_seed.sql` 을 다시 만든 뒤 커밋한다.

## 행 수·지문

- 총 **789행**, 그중 승인(`approved`) **90행**(앵커 19 + 동의어 71), 반려(`rejected`) **40행**,
  나머지 659행은 `pending_review`(2026-08-10 2차 검수 기준).
- codepoint sha256 지문(term·canonical·status·provenance·doc_count 전 필드,
  `scripts/derive_color_synonym_seed.py::row_fingerprint`):
  `37d4aeffe1b62e1163b89f17af6535256ac9a972070e8a853834a5df2607ab4e`
- 사전이 갱신되면(재수확 또는 검수 확대) **`tests/unit/test_color_synonym_seed_data.py`** 의
  `EXPECTED_ROW_COUNT`/`EXPECTED_CODEPOINT_SHA256`(및 관련 승인 수 상수)를 함께 갱신한다.
  한쪽만 고치면 다른 쪽 테스트가 새 정본을 옛 지문과 비교해 실패한다.
