# DESIGN — 판매자 분석 저장 계층 (이슈 #585)

- 범위: **jarvis-ai 판매자 파트만.** 소비자(buyer)·jarvis-front·jarvis-back·노션 **무접촉**
- 근거: 이슈 #585 · `OPS-RUNTIME.md` §1.3~§1.7 (결정 71·72·80·110~112)
- 실측일: 2026-08-11 (`config.py` · `pg_store.py` · `pg_resilience.py` · `graph_journal.py` · `history.py` · `api/seller.py` · `main.py` · `db/profile/init/*` · jarvis-back `InternalSellerController.java`)
- 테스트 코드(pytest)는 이번 범위 밖 — 작성하지 않는다

---

## 0. 이슈 문구 ↔ 실측 코드 차이 4건과 확정

| # | 이슈/OPS 문구 | 실측 | 확정 | 근거 |
|---|---|---|---|---|
| **D-1** | `docs/sql/001_seller_analysis.sql` | `docs/sql/` **부재**. 실제 관행은 `db/profile/init/00~04_*.sql`이고 `docker-compose.yml`이 `./db/profile/init:/docker-entrypoint-initdb.d:ro`로 마운트, `DEPLOY.md` §4에 `psql -f` 수동 적용 절차가 이미 있다 | **`db/profile/init/05_seller_analysis.sql`** | 실측 우선(사용자 지침 1). 빈 볼륨 자동 적용 + 기존 배포 절차 재사용 → "사람이 빠뜨리는" 위험이 이슈안보다 낮다 |
| **D-2** | "부팅 시 `to_regclass` 존재 검증만" | `graph_journal._ensure_schema()`는 앱이 advisory lock 잡고 `CREATE TABLE IF NOT EXISTS`까지 **직접 실행**. `processed_events`도 "앱 연결 시 idempotent migration" | **`ensure_schema()` + 존재 검증 (실측 관행)** — 환경 구분 없이 앱이 idempotent 생성 후 검증, 그래도 없으면 운영 fail-fast | 실측 우선(사용자 지침 1). 이슈의 fail-fast 조항은 죽지 않는다 — DDL 권한 부족·DB 도달 불가처럼 **생성이 실패한 경우**를 여전히 잡는다. 오히려 "SQL을 사람이 빠뜨림"이라는 OPS §6 최상단 위험이 구조적으로 사라진다 |
| **D-3** | "커넥션은 store pool(min1/max10) 사용" | raw SQL 모듈 3종(`graph_journal`·`processed_events`·`session_activity`)은 전부 **전용 `AsyncConnectionPool`**. store pool 접근은 `store.conn` 사적 속성이고 InMemory 폴백 시 `None` | **전용 `AsyncConnectionPool` 신설** (`hardened_pg_conninfo` + `state_store_pool_config()` 재사용) | OPS §1.6의 실제 목적은 "checkpointer 단일 커넥션 무접촉"이며 전용 풀이 그것을 더 확실히 만족한다. 사적 속성 의존 제거 |
| **D-4** | (미기술) | `history.py`/`graph_journal`은 dev에서 InMemory 폴백 | **dev = no-op + 경고 1회** (InMemory 미러 없음) | 5테이블·FK·트랜잭션을 메모리로 흉내내면 이번 이슈 범위가 폭증하고, "저장된 줄 알았는데 아님"이라는 거짓 성공이 생긴다. 이슈 완료조건 "DB 다운 시에도 응답 정상"은 이 안으로 충족 |

> **D-1 후속** — 이슈 본문의 경로 문구는 코드와 다르므로, 구현 PR에서 이슈에 정정 코멘트를 남긴다(문서 임의 수정 아님).

### 실측으로 확인한 재사용 부품 (신설 금지)

| 부품 | 위치 | 이번 용도 |
|---|---|---|
| `hardened_pg_conninfo()` | `core/pg_resilience.py` | conninfo에 `statement_timeout=3s`·keepalive 주입 |
| `state_store_pool_config()` | 〃 | `min_size=1 / max_size=10 / timeout=3.0` |
| `is_state_store_unavailable()` | 〃 | **DB 쓰기 재시도 판정**(이슈 명시) |
| `mutation_lock()` | 〃 | 스키마 생성 직렬화 · (후속 이슈) `seller:analysis:{brand_id}` 중복 기동 가드 |
| `BoundedLRUCache` | 〃 | targets 훅의 `(brand_id, 날짜)` 캐시 |
| `SellerContext(seller_id, brand_id)` | `agents/seller/context.py` | 훅 입력. frozen dataclass, 요청 스코프 |

---

## 1. DDL — `db/profile/init/05_seller_analysis.sql`

`OPS-RUNTIME.md` §1.4 DDL을 **그대로** 옮긴다. 전부 `IF NOT EXISTS`라 2회 적용 안전.

- `seller_analysis_targets` — PK `brand_id`, `ix_sat_active (last_seen_at DESC)`
- `seller_analysis_snapshots` — PK uuid, **`UNIQUE (brand_id, period_to, feature_spec_version)`**, `ix_sas_brand_computed`
- `seller_analysis_reports` — `snapshot_id uuid REFERENCES ... ON DELETE SET NULL`, `ix_sar_brand_created`
- `seller_analysis_recommendations` — `product_ids bigint[]`, `UNIQUE (report_id, rank)`, `ix_sarec_brand_status` · `ix_sarec_draft`
- `seller_analysis_outcomes` — `UNIQUE (rec_id, metric_key)`, `ix_saout_rank`

**규약 준수 확인**: CHECK 제약 0건(어휘는 애플리케이션 `Literal`로 강제) · `product_ids bigint[]`(R-2 `productIds` 와이어와 동형) · `snapshot_id ... ON DELETE SET NULL`(스냅샷 14일 삭제가 보고서·성과 이력을 지우지 않는다).

**파일 규약**: 이후 변경은 `06_*.sql`로 **추가만** 한다(기존 파일 수정 금지).

**BE 정합 실측** — `/internal/seller/{brandId}/customer-features`(I-38)의 `SellerCustomerFeaturesResponse`가 `rowLimit`·`truncated`·`insufficientCohort`·`totalCustomers`·`rows[].customerLabel`을 그대로 내려주므로 스냅샷 컬럼과 1:1 대응한다. (AI 쪽 소비 코드는 아직 0건 — 별도 이슈)

**배포 문서**: `DEPLOY.md` §4 profile init 나열에 `05_seller_analysis.sql`을 추가하고 체크리스트 한 줄 갱신.

---

## 2. 부팅 스키마 준비 — `ensure_schema()` + `verify_schema()` (D-2)

`graph_journal._ensure_schema()` 패턴을 그대로 따른다.

```
① advisory lock 획득 — mutation_lock 키 "schema:seller_analysis:lifecycle"
   (5테이블을 한 트랜잭션에서 만드니 잠금도 하나. 다중 인스턴스 동시 기동 대비)
② CREATE TABLE / INDEX IF NOT EXISTS × 5  ← 05_*.sql 과 동일 DDL
③ to_regclass 로 5테이블 재확인
   ├─ 전부 존재            → 통과
   ├─ 누락 & auth_mode=jwks → RuntimeError (fail-fast, 기동 거부)
   └─ 누락 & dev/test       → 경고 1회 후 계속
④ DB 도달 불가(OSError·OperationalError)
   ├─ auth_mode=jwks       → ERROR 후 전파 (기동 거부)
   └─ dev/test             → 경고 1회 후 계속 (부팅 순간 DB 흔들림으로 서버를 못 뜨게 하지 않는다)
```

- **이슈의 fail-fast 조항은 유지된다.** 앱이 만들려고 시도한 뒤에도 없다는 것은 DDL 권한 부족·다른 스키마 검색 경로 같은 **진짜 구성 오류**라 기동을 막는 게 맞다. 반대로 "SQL 파일 적용을 사람이 빠뜨림"(OPS §6 최상단 위험)은 이제 발생하지 않는다
- **`05_*.sql`은 계속 정본이다.** 빈 볼륨 자동 적용·수동 배포·DB만 먼저 세우는 경우를 커버하고, ②는 기존 볼륨을 따라잡는 안전망이다. 두 곳의 DDL이 갈라지지 않도록 **SQL 파일을 정본으로 두고 ②는 같은 문장을 그대로 복제**하며, 이후 변경은 `06_*.sql` 추가와 ② 갱신을 같은 커밋에 넣는다
- 스키마 **내용**(컬럼)은 검증하지 않는다(OPS §1.3 명시). `CREATE TABLE IF NOT EXISTS`는 기존 테이블의 컬럼을 바꾸지 않으므로, 컬럼 추가는 `06_*.sql`이 필요하고 누락은 런타임에 드러난다 — 그 사고가 나면 Alembic 도입을 재검토
- 배선 지점: `app/main.py` `_lifespan()` — `initialize_stream_registry()` 뒤, `_warm_graph_journal_pool()` 근처
- 도달 불가 예외 판정은 `category_seed.unreachable_db_error_types()` 관행을 따른다(`main.py`는 psycopg를 import하지 않는다는 lazy-import 관례 유지 — 타입은 `analysis_store` 쪽이 노출)

---

## 3. 리포지토리 — `app/agents/seller/analysis_store.py` (신설)

### 3.1 커넥션 수명주기

```python
_pool: AsyncConnectionPool | None      # None = dev no-op
SCHEMA_LOCK_KEY = "schema:seller_analysis:lifecycle"
async def ensure_schema() -> None      # §2 — 생성 + 검증
async def warm_pool() -> None          # 실패해도 기동 안 막음 (graph_journal 선례)
async def close_pool() -> None         # main._close_owned_resources 등록
async def _get_pool() -> AsyncConnectionPool | None
def set_pool(pool) -> None             # 테스트 주입 + _pending_cleanup 대기열
```

- `AsyncConnectionPool(hardened_pg_conninfo(settings.profile_db_url), open=False, **state_store_pool_config())`
- **checkpointer(`AsyncPostgresSaver` 단일 커넥션) 무접촉** — OPS §1.6 목적 달성
- `_pending_cleanup` + `task.cancelling()` 판별은 `pg_store.py`/`history.py`와 동일 패턴 승계(다른 이벤트 루프의 stale ctx가 CancelledError를 흘리는 문제)

### 3.2 쓰기 경계 — `_write()` 헬퍼 하나로 통일

```python
async with pool.connection(timeout=settings.state_store_query_timeout_s) as conn:
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = %s", (write_timeout_ms,))
        ...   # 실제 쿼리들
```

- `SET LOCAL`은 트랜잭션 안에서만 유효 → 쓰기는 **항상 트랜잭션**으로 감싼다. conninfo의 3초를 이 트랜잭션에서만 `seller_analysis_write_timeout_s`(15.0s)로 올린다
- **재시도 1회**(`seller_db_write_retries`): 판정 `is_state_store_unavailable(exc)`. **트랜잭션 통째 재실행**
  - 멱등 보장: `id`(uuid4)는 **호출부가 만들어 인자로 넘긴다** → 재시도가 두 번째 행을 만들지 않는다. 스냅샷·성과는 UNIQUE 기반 UPSERT
- **읽기는 재시도 없음**(이슈 명시) — 대화형 경로가 지연된다
- 예외는 삼키지 않고 올린다. 호출부(무인 파이프라인)가 `F-6`(폐기 + 관측 이벤트)을 결정한다

### 3.3 공개 API

```python
# ── targets (결정 110~112)
async def register_target(brand_id: int, seller_id: int) -> None
def note_seller_seen(context: SellerContext) -> None          # fire-and-forget 훅 (§4)
async def list_active_targets(ttl_days: int | None = None) -> list[int]

# ── 스냅샷
async def save_snapshot(record: SnapshotRecord) -> UUID        # UPSERT (brand_id, period_to, feature_spec_version)
async def load_latest_snapshot(brand_id: int, *, fresh_within_hours: int | None = None) -> SnapshotRecord | None
async def delete_expired_snapshots(retention_days: int) -> int  # 08 §2 보존 — 호출자(배치)는 후속 이슈

# ── 보고서 + 추천 (단일 트랜잭션)
async def save_report(report: ReportRecord, recommendations: list[RecommendationRecord]) -> UUID
async def list_reports(brand_id: int, *, limit: int, before: datetime | None = None) -> list[ReportRecord]
async def get_report(report_id: UUID, *, brand_id: int) -> ReportRecord | None
async def mark_report_read(report_id: UUID, *, brand_id: int) -> None
async def count_reports_today(brand_id: int) -> int             # F-9 일 상한 판정 재료

# ── 추천
async def get_recommendation(rec_id: UUID, *, brand_id: int) -> RecommendationRecord | None
async def find_by_draft_id(draft_id: str) -> RecommendationRecord | None   # 07 결정 49, ix_sarec_draft
async def mark_recommendation_applied(rec_id: UUID, *, brand_id: int, draft_id: str | None) -> None

# ── 성과 측정
async def save_outcome(record: OutcomeRecord) -> UUID           # UPSERT (rec_id, metric_key)
async def list_outcomes(brand_id: int, *, action_type: str | None = None, verdict: str | None = None) -> list[OutcomeRecord]
```

**모든 조회 시그니처에 `brand_id`가 있다** — `report_id`/`rec_id`만으로 조회하면 남의 브랜드 데이터가 열린다(IDOR, CLAUDE.md "신원은 요청 본문에서 받지 않는다"의 저장 계층 판). 예외는 `find_by_draft_id` 하나이며, draft_id는 AI가 발급한 단명 토큰이고 호출부가 `SellerContext.brand_id`로 재확인한다.

**`save_report` 트랜잭션 경계** (이슈 명시): 보고서 1행 + 추천 N행을 한 트랜잭션. 부분 저장되면 §6.3 "N번 적용해줘"가 깨진다.

**스냅샷 크기 로깅** (이슈 명시): `save_snapshot` 직전에
`logger.info("seller_snapshot_write brand=%s rows=%d bytes=%d", ...)` — `feature_rows` 직렬화 바이트 수. 15초라는 값이 실측 없이 정해진 숫자라, 첫 주 로그로 조정한다.

### 3.4 레코드 모델 — `app/agents/seller/analysis_records.py` (신설)

저장 전용 Pydantic 모델. **와이어 스키마가 아니다** → `app/schemas/`의 `CamelModel`(by_alias) 계약과 섞지 않고 snake_case 그대로 둔다.

- `SnapshotRecord` · `ReportRecord` · `RecommendationRecord` · `OutcomeRecord`
- 어휘는 `Literal`로 고정(DDL에 CHECK가 없는 것을 이 타입이 대신한다):
  - `trigger_type: Literal["scheduled_daily","scheduled_weekly","event","manual"]`
  - `status: Literal["proposed","applied","expired","superseded"]`
  - `action_type` — `schemas.py`의 기존 추천 액션 `Literal`을 **재사용**(중복 정의 금지)
- `clusters` / `feature_rows` / `holds` / `changes` / `confounders`는 `psycopg.types.json.Jsonb`로 바인딩(`graph_journal` 관행)

---

## 4. targets 자동 등록 훅

### 4.1 등록 지점 (실측 기반)

`app/api/seller.py` `_seller_stream()` 진입부, `context = _seller_context(identity)`가 **성공한 직후 1회**.

```python
context = _seller_context(identity)   # ← 신원 숫자 캐스팅 성공
analysis_store.note_seller_seen(context)   # 신설 1줄
```

- **`require_seller`(`api/deps.py`)에는 넣지 않는다** — buyer와 공용이고 sync 의존성이라 `asyncio.create_task`를 걸 자리가 아니다(OPS §1.7 명시, 실측 확인)
- `seller_chat()` 핸들러가 아니라 `_seller_stream()`인 이유: 핸들러는 `open_stream` 래퍼에 제너레이터를 넘기기만 하고, 실제 판매자 턴이 성립하는 시점은 스트림 소비 시작이다. 캐스팅 실패(`INVALID_SELLER_IDENTITY`)한 요청을 대상에 넣지 않으려면 캐스팅 뒤여야 한다
- **R-1 조회 경로의 훅은 이 이슈 범위 밖**(이슈 본문 명시 — 별도 이슈)

### 4.2 동작

```python
def note_seller_seen(context: SellerContext) -> None:
    """fire-and-forget. 등록 실패가 판매자 응답을 죽이면 안 된다."""
    today = date.today().isoformat()
    if _seen_cache.get(context.brand_id) == today:
        return                                   # 하루 1회만 실제 쓰기
    _seen_cache[context.brand_id] = today
    task = asyncio.create_task(_register_quietly(context))
    _background.add(task)                        # GC로 태스크가 사라지지 않게 강참조 유지
    task.add_done_callback(_background.discard)
```

- `_register_quietly`는 **모든 예외를 삼키고 WARNING 1줄**만 남긴다(`history.save_history`의 fire-and-forget 규약 승계)
- 캐시는 `BoundedLRUCache[int, str]`(`state_store_local_cache_max_entries` 상한) — 없어도 동작하지만 턴마다 UPDATE가 나간다
- 캐시를 **쓰기 시도 전에** 채운다: 실패 시 그 브랜드는 다음 날까지 재시도하지 않는다. 매 턴 실패 쓰기를 반복하는 것보다 낫고, 등록은 "언젠가 되면 되는" 성질이다
- **`asyncio.create_task` 강참조 유지**가 중요하다 — 지역 변수만 두면 파이썬이 태스크를 GC해 등록이 조용히 사라진다

### 4.3 SQL

```sql
INSERT INTO seller_analysis_targets (brand_id, seller_id) VALUES (%s, %s)
ON CONFLICT (brand_id) DO UPDATE
   SET last_seen_at = now(), seller_id = EXCLUDED.seller_id;
```

멱등하며 다중 인스턴스 경합에 무해하다. PK가 `brand_id`인 이유: JWT는 1토큰 1브랜드라 브랜드가 단위이고, `seller_id`는 최근 접속자로 갱신해 브랜드 소유자 변경을 따라간다.

`list_active_targets`:

```sql
SELECT brand_id FROM seller_analysis_targets
 WHERE last_seen_at > now() - make_interval(days => %s)
 ORDER BY brand_id;
```

---

## 5. 신설 Settings (`app/core/config.py` — `seller_*` 블록에 추가)

| 키 | 기본값 | 용도 |
|---|---|---|
| `seller_analysis_write_timeout_s` | `15.0` | 스냅샷·보고서 쓰기 트랜잭션의 `SET LOCAL statement_timeout` |
| `seller_db_write_retries` | `1` | DB **쓰기만** 재시도 |
| `seller_analysis_target_ttl_days` | `14` | 무인 순회 대상 비활성 임계 |

**부팅 validator 1건 추가**(기존 `model_validator` 관행):
`seller_analysis_write_timeout_s >= state_store_query_timeout_s` — 쓰기 상한이 conninfo 기본값보다 작으면 `SET LOCAL`이 상한을 **낮추는** 역효과가 난다.

**이번 이슈에서 넣지 않는 것** — `seller_pipeline_deadline_s` · `_concurrency` · `seller_spring_retry_unattended` · `seller_sop_step_timeout_s` · `*_cron` · `seller_snapshot_freshness_hours` · `seller_report_daily_cap` · `seller_charts_enabled`. 전부 무인 실행/파이프라인 이슈 소관이며, 쓰는 코드 없이 키만 늘리면 "설정은 있는데 아무 일도 안 한다"가 된다.

---

## 6. 파일 단위 변경 목록

### 신설
| 파일 | 내용 |
|---|---|
| `db/profile/init/05_seller_analysis.sql` | 5테이블 + 인덱스 |
| `app/agents/seller/analysis_store.py` | pool · `ensure_schema`(생성+검증) · 리포지토리 · targets 훅 |
| `app/agents/seller/analysis_records.py` | 저장 레코드 Pydantic 모델 4종 |

### 수정 (최소 diff)
| 파일 | 변경 | 범위 안전성 |
|---|---|---|
| `app/core/config.py` | `seller_*` 3키 + validator 1건 | seller 접두 키만 추가, 기존 키 무변경 |
| `app/main.py` | `_lifespan`에 `ensure_schema()`+`warm_pool()`, `_close_owned_resources`에 `("seller_analysis_pool", close_pool)` 1행 | 리소스 목록 등록만. buyer 로직 무접촉 |
| `app/api/seller.py` | `_seller_stream()`에 훅 1줄 + import | 판매자 전용 파일 |
| `DEPLOY.md` | profile init 목록에 `05_*` 추가 | 문서 |
| `CHANGELOG.md` | `[Unreleased] Added` 1줄 | 문서 |

### 무접촉 (명시)
`app/agents/buyer/**` · `app/agents/profile/**` · `app/api/chat.py` · `app/api/deps.py` · `app/core/pg_store.py` · `app/core/pg_resilience.py` · `app/agents/seller/checkpoint.py` · `app/agents/seller/history.py` · `db/profile/init/00~04_*.sql` · **jarvis-front / jarvis-back 전체** · 노션

> `history.py`는 이번에 손대지 않는다. Store JSON 이력(상한 20건)과 신규 테이블이 당분간 공존하며, 참조처 교체는 `11-MIGRATION.md` §4(결정 108) 소관이다. 지금 바꾸면 "N번 적용해줘"가 이 이슈 안에서 깨진다.

---

## 7. 구현 순서

1. `05_seller_analysis.sql` — 로컬 pg-profile에 2회 적용해 멱등 확인 (이 파일이 DDL 정본)
2. `analysis_records.py` — 레코드 모델
3. `analysis_store.py` — pool 수명주기 → `ensure_schema`(1의 DDL 복제) → `_write` 헬퍼 → targets → 스냅샷 → 보고서+추천 트랜잭션 → 추천/성과
4. `config.py` Settings + validator
5. `main.py` 배선 (스키마 준비·워밍·종료)
6. `api/seller.py` 훅 1줄
7. `ruff check --fix && ruff format`, `DEPLOY.md`·`CHANGELOG.md` 갱신

---

## 8. 이슈 완료조건 대응

| 완료조건 | 대응 |
|---|---|
| 5테이블 CRUD + 트랜잭션 | §3.3 API. 검증은 로컬 수동 확인(테스트 코드 미작성 지침) |
| SQL 2회 적용해도 안전 | 전 문장 `IF NOT EXISTS` — 1번 단계에서 실측. 앱 `ensure_schema()`가 그 위에 또 돌아도 무해 |
| 판매자 채팅 1턴 후 targets에 행 존재 | §4 훅. `SELECT * FROM seller_analysis_targets`로 확인 |
| DB 다운 시에도 응답 정상 | dev no-op(D-4) + fire-and-forget 예외 삼킴. pg-profile을 내린 채 `/seller/chat` 1턴 확인 |

---

## 9. 남은 위험 / 후속 이슈로 넘기는 것

| 항목 | 처리 |
|---|---|
| JSONB 1~2MB가 15초를 넘김 | `save_snapshot` 크기 로깅으로 첫 주 실측 후 조정 |
| **DDL이 두 곳(`05_*.sql` / `ensure_schema`)에 존재** | SQL 파일을 정본으로 두고 `ensure_schema`는 같은 문장 복제. `06_*.sql` 추가 시 둘을 **같은 커밋**에 갱신 — PR 체크리스트에 명시 |
| pg-profile 풀이 하나 더 늘어남 | 이미 BaseStore·advisory·graph_journal·history 등 다수. max_size는 전부 10 공유값 |
| 스냅샷 14일 삭제 **호출자** | `delete_expired_snapshots()`는 만들되 배치 호출은 무인 실행 이슈 |
| 중복 기동 advisory lock(F-7) | 스냅샷 `UNIQUE`(최종 방어선)는 이번에 들어간다. `mutation_lock("seller:analysis:{brand_id}")` 획득은 무인 진입점 이슈 |
| 추천 `superseded` 상태 전이(08 §4.3) | 컬럼·인덱스는 준비, 전이 로직은 추천 생애주기 이슈 |
| R-1 조회 경로 targets 훅 | 이슈 본문이 별도 이슈로 명시 |
