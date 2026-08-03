# DESIGN — 판매자 레인 타임아웃 정합 (이슈 미배정)

작성일: 2026-08-03 · 상태: **제안(코드 미변경)** · 소관: `api-spec §2.9(c)`, `SPEC-SELLER-001 §7`

전수조사 대상: `app/api/seller.py`, `app/agents/seller/*`, `app/core/{llm,stream,config}.py`,
`app/services/spring_client.py`

---

## 1. 목적

판매자 레인의 타임아웃이 **계약(§2.9 c)과 실제 동작이 어긋나는 5개 지점**을 정합시킨다.
목표는 "상한을 조이는 것"이 아니라 **같은 원인이 항상 같은 오류 코드로 나가게** 만드는 것이다.

이 문서는 설계만 담는다. 코드는 승인 후 착수한다.

---

## 2. 현황 — 타임아웃 3층

```
FE ──POST /seller/chat──> [3층] SSE 스트림 캡        stream.py:513-520
                             └> [2층] asyncio.wait_for  orchestrator.py, api/seller.py
                                   └> [1층] SDK timeout  seller/models.py:122-130
                                         └> Anthropic / OpenAI
```

### 2.1 값 지도 (2026-08-03 코드 실측)

| 계층 | 값 | 기본 | 범위 |
|---|---|---|---|
| 1층 | `llm_timeout_s` × (`llm_max_retries`+1) | 30s × 2 = **60s** | **구매자와 공유** |
| 2층 | `seller_route_timeout_s` | 10s | 판매자 |
| 2층 | `seller_worker_timeout_s` | 60s | 판매자 (worker·report·judge·recommend·graph·planner·product **7곳 공용**) |
| 2층 | `seller_analysis_judge_timeout_s` | 20s | 판매자 |
| 2층 | `seller_branch_deadline_s` | 120s | 판매자 |
| 2층 | general 레인 | **없음** | — |
| 3층 | `stream_first_token_timeout_s` | 10s | **구매자와 공유** |
| 3층 | `stream_total_timeout_s` | 90s | 판매자·미지정 |

### 2.2 실측 성능 (근거 — 상한 재설계의 기준선)

`evals/benchmark/baselines/20260802T140556535202Z-local-spring-seller/report.md`
(로컬 + Spring 기동, 동시성 1, `gpt-5.6-luna`)

| 시나리오 | n | p50 | p95 | max | timeout | degrade |
|---|---:|---:|---:|---:|---:|---:|
| `measured:seller_analysis@1` | 30 | 10.69s | **11.73s** | **12.70s** | 0/30 | 0/30 |
| `measured:seller_general@1` | 30 | 1.71s | **2.52s** | **2.55s** | 0/30 | 0/30 |

**현행 상한은 실측 대비 과도하게 느슨하다.** analysis 실측 max 12.7s에 워커 하나만 60s를
주고 있고, 이론 최악값은 **약 640s**(§3.4)로 90s 캡의 7배다. 즉 상한이 상한 역할을 못 한다.

---

## 3. 문제 (P1~P5)

### P1 — general 레인에 2층이 통째로 없다

`api/seller.py:296-329`의 producer가 `agent.astream(...)`을 `wait_for` 없이 돈다.
analysis·product·routing은 전부 감싸는데 general만 빠졌다.

**파생 — 도달 불가 분기.** 같은 함수 `:347-353`:

```python
except (TimeoutError, asyncio.TimeoutError):
    yield _error("LLM_TIMEOUT", ...)
```

- `asyncio.TimeoutError`는 `wait_for`가 던지는데 general엔 `wait_for`가 없다.
- SDK 타임아웃(`httpx.TimeoutException` → `TransportError → HTTPError → Exception`,
  `anthropic/openai.APITimeoutError`)은 내장 `TimeoutError`의 **서브클래스가 아니다.**

결과: LLM 타임아웃이 `except Exception`(`:354`)으로 떨어져 **`INTERNAL`**이 나간다.
계약이 요구하는 `LLM_TIMEOUT`이 아니다.

> ⚠ SDK 예외 계보는 라이브러리 버전 의존이다. 구현 전 §9-V1로 실측 확인한다.

### P2 — 라우팅 10s == first-token 10s

`api/seller.py:856-905`의 emit 순서:

```
scope 체크 → 스레드 조회 → route_question(≤10s) → 첫 이벤트 meta{lane}
```

첫 SSE 이벤트가 라우팅 **뒤**다. 그런데 first-token 데드라인도 10s(`stream.py:520`).
라우팅이 상한을 채우면 `orchestrator.py:165-167`의 general 폴백이 살려내기 **직전에**
스트림 계층이 504 UPSTREAM_TIMEOUT을 던진다(`stream.py:545-560`). 앞단 스레드 조회
시간까지 더하면 확정적이다. `config.py:224`의 주석 "first-token 10s 목표 내"는 거짓이다.

**`meta`를 라우팅 앞으로 옮기는 대안은 기각한다** — `FE-CONTRACT-SELLER-CHAT.md:15, 100`이
`meta{lane}`을 첫 프레임으로 못박았고 `lane`은 라우팅 산출물이다. 라우팅 전에는 실을 값이 없다.

### P3 — worker 60s와 SDK 예산 60s가 정확히 동률

`seller_worker_timeout_s = 60.0`, SDK 최악 `30 × (1+1) = 60.0`.
어느 쪽이 먼저 터지는지가 지터로 갈린다.

| 먼저 터진 쪽 | 예외 | 나가는 코드 |
|---|---|---|
| `wait_for` | `asyncio.TimeoutError` | `LLM_TIMEOUT` |
| SDK | `APITimeoutError` | `INTERNAL` |

**같은 원인이 두 코드로 기록된다.** 이 저장소는 카탈로그 DB에 대해 정확히 같은 문제를
`_require_db_timeout_after_app_timeout`(`config.py:840-853`)로 막아뒀다:

> 앱쪽이 항상 먼저 포기해야 느린 쿼리가 **결정적으로 504**로 나간다. DB가 먼저 끊으면
> `QueryCanceled` → `except Exception` → 503이 되고, 같은 원인이 지터에 따라 다른 코드로 기록된다

같은 논리가 LLM 층에 없다. **`seller_*`와 `llm_timeout_s`의 관계를 검증하는 기동 불변식은 0건이다.**

### P4 — 파이프라인 이론 최악값이 캡의 7배

`orchestrator.py:635-717` 단계별 최악:

| 단계 | 최악 | 근거 |
|---|---:|---|
| planner | 60s | `:683` |
| 브랜치 1개 | 워커 60 + judge 20 + 재실행 60 + judge 20 = **160s** | `:298, 268, 328` |
| report 루프 | (report 60 + judge 60) × 3 = **360s** | `:486, 505` + `seller_report_max_retries=3` |
| recommend ∥ graph | 60s | `:558, 605` (gather 병렬) |
| **합** | **≈ 640s** | vs 캡 **90s** |

`seller_branch_deadline_s = 120`은 방어가 안 된다. `:320`이

```python
can_retry = attempt < max_attempts and time.monotonic() < deadline
```

**재실행 진입 여부만** 보고 진행 중 작업을 자르지 않는다. 게다가 120 > 90이라 브랜치 하나
예산이 전체 스트림 예산보다 크다.

**초과 시 사용자가 받는 것**: 에러가 아니라 `done(stop)` 절단(`stream.py:665-676`).
FE는 정상 종료로 처리하고, 관측에는 `COMPLETED`로 남는다. **조용한 실패다.**

### P5 — product 레인이 analysis 예산을 빌려 쓴다

`api/seller.py:504`가 `seller_worker_timeout_s`(60s)를 쓴다. product는 상품 조회·수정
초안이라 분석 워커와 성격이 다른데 이름도 의미도 안 맞는 값을 공용한다.

---

## 4. 설계 원칙

**원칙 1 — 앱 벽시계가 항상 먼저 터진다.**
모든 2층 `wait_for` < 1층 SDK 예산. 카탈로그 선례(`config.py:840-853`)의 LLM 판.
이걸 지켜야 타임아웃이 결정적으로 `LLM_TIMEOUT`이 된다.

**원칙 2 — 공유값은 건드리지 않는다. 딱 1건만 분리한다.**
`stream_first_token_timeout_s`·`spring_timeout_s`·`stream_disconnect_poll_s`는 그대로 두고,
판매자 전용 값 조정으로 푼다. **`llm_timeout_s`만 분리한다**(§5.1의 근거).

**원칙 3 — 상한은 이론 최악값이 아니라 실측 × 여유배수로 잡는다.**
근거는 §2.2 실측. 각 값에 배수를 명시한다(§6).

**원칙 4 — 예산 초과는 절단이 아니라 degrade로 표면화한다.**
`done(stop)`은 관측에서 성공으로 보인다. 파이프라인이 스스로 예산을 인지하고 부분 결과를
`_mark_degraded`로 내보내야 알람에 잡힌다.

---

## 5. 변경안

### C1 — 판매자 LLM 예산 분리 (유일한 공유값 분리)

#### 왜 분리가 불가피한가

구매자와 판매자가 이 값에 원하는 방향이 **반대**다.

- 구매자: 캡 30s인데 LLM 예산 60s → `LLM_TIMEOUT`이 나올 수 없다(항상 `done(stop)`이 먼저).
  재시도 2회가 30s에 들어오려면 12s 안팎이어야 한다(실측 단일 호출 p95 4.3s, `config.py:578`).
- 판매자: smart tier + 도구 호출이라 단일 호출이 구조적으로 길다. 12s로 조이면 정상 분석이 죽는다.

한 값으로 둘을 만족시킬 수 없다. **나머지 문제는 전부 `seller_*` 조정으로 풀리므로 분리는 여기 하나뿐이다.**

#### 구조적 결합도 = 0 (확인 완료)

```
구매자 →  core/llm.py:get_llm()          →  AnthropicLLM / OpenAILLM
판매자 →  seller/models.py:init_seller_model() → _cached_model(...)
```

- `get_llm()` 호출처 전수: `buyer/graph.py:316`, `profile/finalizer.py:144,456`,
  `pipelines/artifacts_batch.py:135`. **판매자 코드는 한 번도 부르지 않는다**
  (`api/seller.py:61`·`orchestrator.py:88`은 `LLMNotConfigured` 예외 타입만 import).
- `_cached_model`(`models.py:78-87`)은 이미 `timeout`·`max_retries`를 **위치 인자로 받고
  `lru_cache` 키에 포함**한다 → 값만 바꾸면 판매자 전용 인스턴스가 자동으로 따로 캐시된다.

#### 변경

```python
# app/core/config.py — 신규
seller_llm_timeout_s: float = 40.0
seller_llm_max_retries: int = 0

# app/agents/seller/models.py:128-129
- settings.llm_timeout_s,
- settings.llm_max_retries,
+ settings.seller_llm_timeout_s,
+ settings.seller_llm_max_retries,
```

**`seller_llm_max_retries = 0`인 이유**: 판매자는 앱 레벨 `wait_for`가 권위 시계다.
SDK가 몰래 예산을 2배로 늘리면 `wait_for` 값이 실제 상한이 아니게 된다(P3의 근본 원인).
재시도가 필요한 곳은 이미 앱 레벨에 있다(`seller_worker_max_retries`,
`seller_report_max_retries`) — 그쪽이 피드백까지 실어 재실행하므로 SDK 맹목 재시도보다 낫다.

**영향 없음 확인**: `config.py:972`의 `session_end_claim_ttl_s > llm_timeout_s × (retries+1) × 2`는
프로필 세션 종료 경로(구매자 계열)라 기존 값을 계속 쓴다. 판매자 분리와 무관하다.

### C2 — general 레인에 2층 추가 + 예외 매핑 교정

`api/seller.py`:

1. `produce()` 전체를 `asyncio.wait_for(..., timeout=settings.seller_general_timeout_s)`로 감싼다.
   스트리밍 제너레이터라 **`wait_for`가 아니라 `async with asyncio.timeout(...)`을 producer 안에
   두는 형태**가 맞다(청크 루프 전체를 덮어야 하고, 중간 yield가 있어 `wait_for`로는 못 감싼다).
2. `except (TimeoutError, asyncio.TimeoutError)` 분기에 **SDK 타임아웃 예외를 함께 넣는다.**

SDK 예외를 `api/seller.py`가 직접 알게 하지 않기 위해, **`core/llm.py`에 판정 헬퍼를 신설**한다:

```python
# app/core/llm.py — 신규 (구매자 _is_timeout 도 이걸로 교체, C6)
def is_timeout_error(exc: BaseException) -> bool:
    """SDK/전송 계층 타임아웃을 타입으로 판정한다(문자열 매칭 금지)."""
```

general 레인은 `except Exception as exc: if is_timeout_error(exc): → LLM_TIMEOUT`으로 분기한다.

### C3 — 라우팅을 first-token 예산 안으로

```python
seller_route_timeout_s: 10.0 → 5.0
```

라우팅은 짧은 분류 태스크다. 실측 general 총 p50 1.71s 안에 라우팅이 포함되므로 라우팅
자체는 ~1s대다. 5s는 약 5배 여유이고, first-token 10s 안에서 스레드 조회 + 폴백 경로까지
여유가 남는다. **`orchestrator.py:165-167`의 general 폴백이 비로소 도달 가능해진다.**

`stream_first_token_timeout_s`(공유값)는 건드리지 않는다.

### C4 — 파이프라인 단일 예산(deadline) 도입

per-call 상한만으로는 합이 캡을 넘는다(P4). `home_recommendation.py:319-325, 361, 396, 439`가
이미 쓰는 **잔여 예산 패턴**을 판매자 파이프라인에 그대로 적용한다.

```python
# app/core/config.py — 신규
seller_pipeline_budget_s: float = 70.0   # 90s 캡 − 마무리·저장 몫 20s
```

`run_analysis_pipeline`(`orchestrator.py:635`) 진입 시 `deadline = monotonic() + budget`을 잡고,
각 단계가 `timeout=min(단계 상한, 남은 예산)`으로 기다린다. 예산 소진 시:

- report 이전 → 현재까지 finding으로 축약 보고서 경로
- report 이후 → recommend·graph 생략
- 어느 경우든 **`_mark_degraded("pipeline_budget_exhausted")`** 로 관측에 남긴다

이로써 P4의 "조용한 `done(stop)` 절단"이 **관측 가능한 degrade**로 바뀐다(원칙 4).

`seller_branch_deadline_s`(120s)는 이 예산에 흡수되므로 **폐기**한다 — 진행 중 작업을 못 자르는
반쪽 장치이고 캡보다 큰 값이라 의미가 없었다.

### C5 — 공용 60s를 역할별 상한으로 분해

`seller_worker_timeout_s` 하나가 7곳에서 쓰인다. 성격이 다른 단계를 쪼갠다.

| 신규 설정 | 대체 대상 |
|---|---|
| `seller_planner_timeout_s` | `orchestrator.py:683` |
| `seller_worker_timeout_s` (유지·값만 조정) | `:298, 328` |
| `seller_report_timeout_s` | `:486` |
| `seller_report_judge_timeout_s` | `:505` |
| `seller_synthesis_timeout_s` | `:558`(recommend), `:605`(graph) |
| `seller_product_timeout_s` | `api/seller.py:504` (P5) |
| `seller_general_timeout_s` | 신규 (C2) |

### C6 — `_is_timeout` 문자열 매칭 제거 (구매자 파생, 선택)

`buyer/graph.py:91-93`이 `"timeout" in str(exc).lower()`로 판정한다. SDK 메시지는
`"Request timed out."`(timed **out**)이라 매칭되지 않을 수 있고, `httpx.ReadTimeout`은 `str`이
빈 문자열인 경우가 있다. → `LLM_TIMEOUT`이어야 할 것이 `LLM_UNAVAILABLE`로 나간다.

테스트(`test_recommendation.py:483`, `test_degrade_e2e.py:69`)는 `"timeout"`이라는 단어를 직접
박은 가짜 예외를 써서 초록불이 유지된다 — `lessons.md:159-168`("소비처 없는 설정") 및
`:170-193`("검증식이 통과한다는 것은 근거가 옳다는 증거가 아니다")와 같은 계열이다.

C2에서 만든 `is_timeout_error`로 교체한다. **판매자 범위 밖이므로 별도 PR로 분리**한다.

---

## 6. 값 제안표 (실측 근거 포함)

| 설정 | 현행 | 제안 | 근거 |
|---|---:|---:|---|
| `seller_llm_timeout_s` | (30 공유) | **40.0** | 모든 2층 상한(≤30)보다 커서 앱이 항상 먼저 터진다(원칙 1) |
| `seller_llm_max_retries` | (1 공유) | **0** | 앱 `wait_for`가 권위 시계 — SDK 이중 예산 제거(P3 근본) |
| `seller_route_timeout_s` | 10.0 | **5.0** | 라우팅 실측 ~1s대 × 5배, first-token 10s 안쪽(C3) |
| `seller_planner_timeout_s` | (60) | **15.0** | 구조화 단발 호출 |
| `seller_worker_timeout_s` | 60.0 | **30.0** | analysis 총 실측 p95 11.7s — 단일 워커에 30s는 충분 |
| `seller_analysis_judge_timeout_s` | 20.0 | **12.0** | 채점 단발 호출 |
| `seller_report_timeout_s` | (60) | **30.0** | 최장 생성 단계 |
| `seller_report_judge_timeout_s` | (60) | **15.0** | 채점 단발 호출 |
| `seller_synthesis_timeout_s` | (60) | **20.0** | recommend ∥ graph |
| `seller_product_timeout_s` | (60) | **20.0** | P5 |
| `seller_general_timeout_s` | (없음) | **20.0** | general 실측 max 2.55s × 약 8배 |
| `seller_pipeline_budget_s` | (없음) | **70.0** | analysis 실측 max 12.70s × 5.5배, 캡 90s − 20s |
| `seller_branch_deadline_s` | 120.0 | **폐기** | C4가 흡수 |

**여유 검증**: 제안값 전부에서 실측 max 대비 최소 5배 이상 여유가 있고, 실측 30/30에서
timeout 0건·degrade 0건이었다. 조이는 변경이지만 정상 요청을 자를 위험은 낮다.

**불변식 성립 확인**: 모든 2층 상한 최댓값 30.0 < 1층 예산 40.0 ✓

---

## 7. 기동 불변식 (신규 validator)

`config.py`에 카탈로그 선례(`_require_db_timeout_after_app_timeout`) 형식으로 추가한다.

```
V-1  max(seller 2층 상한 전부) < seller_llm_timeout_s × (seller_llm_max_retries + 1)
       └ 앱이 항상 먼저 → 결정적 LLM_TIMEOUT (P3)

V-2  seller_route_timeout_s < stream_first_token_timeout_s
       └ 라우팅 실패가 폴백에 닿기 전에 504가 되지 않게 (P2)

V-3  seller_pipeline_budget_s < stream_total_timeout_s
       └ 파이프라인이 캡보다 먼저 degrade로 마감 (P4)

V-4  seller_planner + seller_worker + seller_analysis_judge + seller_report
     + seller_report_judge + seller_synthesis  ≤  seller_pipeline_budget_s
       └ 무재시도 happy path 상한의 합이 예산 안 (= 15+30+12+30+15+20 = 122 > 70 ✗)
```

> ⚠ **V-4는 제안값으로 성립하지 않는다(122 > 70).** 이는 의도된 것이다 — 단계 상한은
> *개별 폭주 방어*이고 예산은 *총량 방어*라, 둘을 모두 만족시키려면 단계 상한을 실측에
> 지나치게 근접시켜야 한다. **V-4는 기동 검증으로 넣지 않고**, 대신 C4의 `min(상한, 잔여)`
> 배선이 런타임에 강제한다. 이 판단을 문서에 남기는 이유는, 검증을 "넣지 않기로 했다"와
> "빠뜨렸다"를 구분하기 위해서다.

---

## 8. 계약(api-spec) 개정안

CLAUDE.md 규약상 **명세 개정 커밋이 코드보다 먼저/함께** 나가야 한다.

### 8.1 §2.9(c) 기준표 — LLM 행 분리

현행 `:303`:

```
| AI→LLM 단일 호출 | 30s + 1회 재시도 | 재시도 실패 시 in-stream error(LLM_UNAVAILABLE 계열) |
```

개정안:

```
| AI→LLM 단일 호출 (구매자·프로필) | 30s + 1회 재시도 | 재시도 실패 시 in-stream error |
| AI→LLM 단일 호출 (판매자)        | 40s, SDK 재시도 없음 — 재시도는 앱 레벨(브랜치·보고서 루프) | 동일 |
| 판매자 분석 파이프라인 총예산     | 70s | 예산 소진 시 부분 결과 + degrade 관측(절단 아님) |
```

**FE·BE 영향 없음** — 이 행들은 내부 값이고, 외부 계약인 "초과 시 동작"(§2.9 `:305`)은 불변이다.
`:305`가 이미 *"값은 config 기본값이며 운영 조정 가능. 계약 사항은 초과 시 동작이다"* 로
못박아 뒀으므로 숫자 변경 자체는 계약 변경이 아니다. 행 분리만 개정 대상이다.

**손대지 않는 행**: first-token `10s (역할 공통)` — 명시적으로 역할 공통이라 분리하면 FE 협의가
필요한데, C3로 해결되므로 유지한다. AI→Spring `3s (BE I-2 기준 통일)` — BE와의 교차 계약이라
분리 불가. (별건인 httpx 단계별 타임아웃 문제는 §11 비범위.)

### 8.2 `SPEC-SELLER-001 §7` — degrade 사유 추가

`pipeline_budget_exhausted`를 degrade 사유 목록에 추가한다.

### 8.3 `.env.example` 정정 (기존 버그)

`:78`이 이렇게 적혀 있다:

```
# SPRING_TIMEOUT_S × (이 값 + 1) 이 STREAM_FIRST_TOKEN_TIMEOUT_S 미만이어야 기동한다.
```

실제 코드(`config.py:898`)는 `stream_total_timeout_buyer_s`와 비교한다. `lessons.md:170-193`에
이 기준을 정정한 기록이 있는데 `.env.example`만 옛 문구다. **이번 PR에서 함께 고친다.**

---

## 9. 검증 계획

### 9.1 선행 실측 (구현 전 필수)

**V1 — SDK 타임아웃 예외 계보 확인.**
P1·C2·C6의 전제가 "SDK 타임아웃은 `TimeoutError` 서브클래스가 아니다"인데, 이건 라이브러리
버전 의존이다. `lessons.md:170-193`의 규칙("검증식이 통과한다는 것은 근거가 옳다는 증거가
아니다")에 따라, **코드를 고치기 전에** 현재 lock된 버전에서 실제 예외 타입·메시지를 찍어
확인한다. 예상과 다르면 C2·C6 설계를 수정한다.

```
확인 항목: httpx.ReadTimeout / anthropic.APITimeoutError / openai.APITimeoutError 의
           MRO, str(exc), isinstance(exc, TimeoutError)
```

### 9.2 회귀 테스트

| ID | 대상 | 단언 |
|---|---|---|
| T-1 | P1 | general 레인 LLM 지연 → `LLM_TIMEOUT` (현재 `INTERNAL`) |
| T-2 | P1 | general 레인에 SDK 타임아웃 주입 → `LLM_TIMEOUT` |
| T-3 | P3 | 2층 상한 초과 → 항상 `asyncio.TimeoutError` 경로 (SDK가 먼저 터지지 않음) |
| T-4 | P2 | 라우팅 지연 → 504가 아니라 general 폴백 + `meta{lane:"general"}` |
| T-5 | P4 | 예산 소진 → `done(stop)` 절단이 아니라 부분 결과 + degrade 기록 |
| T-6 | C1 | `seller_llm_timeout_s` 변경이 판매자 모델에만 반영, 구매자 `get_llm()` 불변 |
| T-7 | V-1~V-3 | 불변식 위반 설정으로 기동 시 `ValueError` |

**T-6이 중요하다.** `lessons.md:159-168`("튜너블을 추가하고 배선하지 않으면 초록불인데 동작은
안 바뀐다")에 따라, 값 유효성이 아니라 **값을 바꾸면 동작이 달라지는지**를 단언한다.

**T-3이 P3의 핵심이다.** 기존 테스트는 진입점별로 자기 코드만 단언해서 이 비대칭을 놓쳤다
(`lessons.md:16-30`과 같은 계열). "같은 원인 → 같은 코드" 교차 불변식으로 쓴다.

### 9.3 사후 실측

`evals/benchmark` 판매자 시나리오를 **변경 전후 동일 조건으로 재실행**하고 baseline을 갱신한다.
조이는 변경이므로 `timeout`·`degrade` 열이 0을 유지하는지가 합격 기준이다.

---

## 10. 구현 순서 (PR 분할)

CLAUDE.md의 "한 커밋 = 한 논리 단위" · "계약 변경은 명세 개정을 먼저/함께"를 따른다.
전부 `dev` 대상, 이슈 선등록 후 `Closes #N` 연결.

| # | PR | 내용 | 계약 |
|---|---|---|---|
| 0 | 실측 | §9.1 V1 SDK 예외 계보 확인 (코드 변경 없음, 결과를 이슈에 기록) | — |
| 1 | `docs(api-spec)` | §8.1·8.2 명세 개정 + `.env.example` 정정 | ✅ 선행 |
| 2 | `fix(seller)` | **C2** general 레인 상한 + 예외 매핑 (T-1·T-2) | — |
| 3 | `refactor(config)` | **C1·C5** LLM 예산 분리 + 역할별 상한 + **V-1·V-2** (T-3·T-6·T-7) | — |
| 4 | `fix(seller)` | **C3** 라우팅 상한 (T-4) | — |
| 5 | `feat(seller)` | **C4** 파이프라인 예산 + `branch_deadline` 폐기 + **V-3** (T-5) | — |
| 6 | `fix(buyer)` | **C6** `_is_timeout` 교체 (판매자 범위 밖 — 선택) | — |
| 7 | `chore(evals)` | §9.3 baseline 재측정·갱신 | — |

**PR 2를 먼저 내는 이유**: 단독으로 가치가 있고(현재 `INTERNAL` 오분류 즉시 해소),
다른 변경에 의존하지 않는다. PR 3이 값을 정리하기 전에 구조부터 맞춘다.

각 PR 병합 시 `CHANGELOG.md [Unreleased]` 갱신, 계약 변경분은 `(api-spec §2.9, vX.Y)` 병기.

---

## 11. 비범위

| 항목 | 이유 |
|---|---|
| `spring_timeout_s`의 httpx 단계별 타임아웃 문제 | `httpx.AsyncClient(timeout=3.0)`이 connect/read/write/pool에 **각각** 3s를 걸어 최악 ~9s가 된다(`spring_client.py:272, 770`). BE 교차 계약이라 별도 이슈 + BE 협의 필요 |
| `spring_timeout_s`의 JWKS 재사용 | `deps.py:46`, `ratelimit.py:127`이 Spring 콜백이 아닌 상류에 같은 값을 쓴다. 이름·의미 불일치이나 동작 문제는 아님 |
| `slo_total_seller_ms`(90s) == `stream_total_timeout_s`(90s) | SLO 목표와 강제 절단이 같은 값이라 "SLO를 못 지킨 상태"와 "잘린 상태"가 구분되지 않는다. EVAL-OBS 소관 |
| 구매자 레인 LLM 예산 역전 (60s > 캡 30s) | C1이 판매자를 떼어내면 구매자 값을 자유롭게 낮출 수 있게 되지만, 구매자 SLO 검토가 선행되어야 함. 별도 이슈 |
| `stream_first_token_timeout_s` 역할 분리 | C3로 해결되어 불필요. 분리는 명세상 "역할 공통" 개정 + FE 협의 비용이 큼 |

---

## 12. 미확정 — 착수 전 결정 필요

1. **§9.1 V1 실측 결과.** SDK 예외가 실제로 `TimeoutError` 서브클래스가 아닌지. 결과에 따라 C2·C6 수정.
2. **`seller_pipeline_budget_s = 70.0`이 적정한가.** 실측 max 12.70s 대비 5.5배지만, 실측이
   n=30·동시성 1·로컬이다. 운영 동시성에서 재측정 후 조정할 수 있다.
3. **`seller_llm_max_retries = 0`을 받아들일 것인가.** SDK 재시도를 없애면 일시적 네트워크
   오류에서 앱 레벨 재시도가 없는 단계(planner·recommend·graph)는 한 번에 실패한다.
   대안: 해당 단계에만 앱 레벨 재시도 1회를 추가. **비용·복잡도 대비 판단 필요.**
4. **이슈 번호.** mvp-todo 주제와 맞춰 등록 후 이 문서 제목에 반영.

---

## 13. 남길 lessons (구현 완료 후)

- 타임아웃 계층이 2개 이상이면 **어느 쪽이 먼저 터지는지를 기동 불변식으로 고정한다.**
  동률은 "둘 다 맞다"가 아니라 "같은 원인이 두 코드로 갈린다"이다.
- 예외를 타입이 아니라 **문자열로 판정하면 테스트는 통과하고 런타임만 틀린다.**
  가짜 예외로 쓴 테스트는 실제 SDK 예외를 한 번도 통과시켜 본 적이 없다.
- 상한을 설계할 때 **이론 최악값과 실측을 둘 다 적는다.** 이번엔 이론 640s / 실측 12.7s로
  50배 차이였고, 이 간극 자체가 "상한이 상한 역할을 못 한다"는 증거였다.
