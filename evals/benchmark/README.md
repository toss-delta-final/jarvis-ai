# HTTP/SSE benchmark runner (#151)

실제 FastAPI→Spring/DB/LLM 경로를 블랙박스 HTTP/SSE로 측정한다. 타깃만 주입하므로 로컬과
staging에서 같은 runner를 쓰며, fixture와 산출물에는 토큰·키·실사용자 식별자를 넣지 않는다.

```bash
uv run python -m evals.benchmark.runner \
  --base-url http://localhost:8000 \
  --target-label local \
  --scenarios buyer_recommend,buyer_fallback \
  --concurrency 1,5,10 \
  --measured-requests 30 \
  --server-log /path/to/server.log \
  --out-dir evals/benchmark/baselines
```

staging 인증은 토큰 값이 아니라 환경변수 이름만 넘긴다.

```bash
BENCH_AUTH_TOKEN=... uv run python -m evals.benchmark.runner \
  --base-url https://staging.example --target-label staging \
  --auth-token-env BENCH_AUTH_TOKEN
```

`--dry-run`은 네트워크 호출 없이 시나리오·설정·예정 출력 경로를 검사한다. 기본 최소 30건보다
작은 measured 표본은 `--sample-size-rationale`이 없으면 실행 전에 exit 2로 거절한다. 기존 출력
디렉터리는 절대 덮어쓰지 않는다.

## 정의와 정직성 규약

- `client_ttft_ms`: 요청 직전 monotonic 시각부터 `type=token`이며 `data.text.strip()`이 비지 않은
  첫 프레임까지다. httpx는 request byte가 실제 전송되는 순간의 공개 hook을 제공하지 않으므로,
  그룹별 warm-up으로 연결을 먼저 채운 measured 구간에서는 request-byte send 기준에 수렴한다.
  풀에 유휴 연결이 없으면 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있으며 이를 숨기지
  않는다. 첫 이벤트·전체 종료 지연도 별도로 기록한다.
- cold와 warm-up은 raw에 남지만 measured 집계에서 제외한다.
  cold는 시나리오별 별도 그룹으로 리포트하며 작은 표본의 CI·p95를 강한 주장에 쓰지 않는다.
  warm-up은 각 scenario×concurrency 그룹에서 해당 동시성으로 실행하며, 설정 건수가 동시성보다
  작으면 연결 풀을 채우기 위해 동시성 수만큼 실행한다.
- reliability 분모는 성공·실패·타임아웃을 포함한 measured 전체다. latency 분모는 non-empty token과
  terminal `done`을 모두 받은 성공만이며 제외 건수를 표시한다.
- p50/p95는 #137과 동일한 최근접 순위다. bootstrap은 manifest의 고정 시드·횟수로 재현한다.
  p99는 설정된 최소 표본(기본 100) 미만이면 `null`과 생략 사유를 출력한다.
- 로그·TTFT·토큰·비용 누락은 0으로 추정하지 않고 `null`/`unknown`과 bounded 사유로 남긴다.
  `latencyFirstToken`은 #141의 `server_first_text_token_ms`이며 provider TTFT는 chat_request에 없다.
- `--server-log`가 있으면 응답 `X-Request-Id`와 `chat_request.requestId`를 조인한다. 없으면 보고서
  상단에 server-side 미수집을 명시한다.

산출물은 `report.md`, `metrics.csv`, `raw.jsonl`, `manifest.json`이다. CSV는
`section,group,metric,value` long-format이며 결측은 빈 문자열로 써 0과 구분한다.
`buyer_dependency_degrade`는 runner가 장애를 만들지 않는다. 이미 Spring이 중단된 승인된 타깃에서
실행하고 `induced_by=spring_unavailable`을 manifest의 dependency conditions에 기록한다.

## 무료 모드 — `LLM_PROVIDER=scripted` (#438)

매 요청이 실 LLM(decompose+rerank)을 호출하므로 시나리오×동시성×measured 격자는 금방 수백
호출이 되고, rerank는 후보 30~40건 JSON을 싣는 smart tier라 비용의 대부분을 차지한다. 부하
테스트가 알고 싶은 것(이벤트루프·SSE 스트리밍·pgvector·커넥션 풀·Spring 왕복)에는 LLM이 대부분
필요 없고, 오히려 벤더 지연이 다른 병목을 가린다. `LLM_PROVIDER=scripted`로 서버를 띄우면
결정론 스텁(`app/core/llm_scripted.py::LoadTestLLM`)이 LLM을 대신해 이 러너를 비용 없이 돌릴 수
있다.

지연 없는 `instant` 프로파일은 내부 처리량 상한을 잰다.

```bash
LLM_PROVIDER=scripted \
APP_ENVIRONMENT=local \
SCRIPTED_LLM_MODE=instant \
uv run uvicorn app.main:app --reload
```

사용자 첫 응답 실측 평균을 보수적으로 5초로 반올림한 `delayed` 프로파일은 요청당 한 번
비동기로 5초를 기다린다. 한 buyer 요청 안에서 decompose·rerank 등 LLM 호출이 여러 번 생겨도
5초가 호출마다 누적되지 않는다. 이 프로파일은 실제처럼 SSE 연결이 오래 열린 동안의 동시 연결,
메모리, ALB timeout을 재기 위한 것이다. 5초는 **추가 대기값**이므로 실제 종단 시간은 여기에
FastAPI·DB·Spring 처리시간을 더한 값이다.

```bash
LLM_PROVIDER=scripted \
APP_ENVIRONMENT=local \
SCRIPTED_LLM_MODE=delayed \
SCRIPTED_LLM_DELAY_S=5.0 \
uv run uvicorn app.main:app --reload
```

```bash
uv run python -m evals.benchmark.runner \
  --base-url http://localhost:8000 \
  --target-label local-scripted \
  --scenarios buyer_recommend \
  --concurrency 1,5,10 \
  --measured-requests 30 \
  --server-log /path/to/server.log \
  --out-dir evals/benchmark/baselines
```

**운영에서는 절대 켜지지 않는다** — `app_environment`가 `local`/`test`가 아니면 기동 자체가
`ValueError`로 실패한다(config.py `_forbid_scripted_outside_local`, G1). `deploy.yml`은
`LLM_PROVIDER`·`APP_ENVIRONMENT`와 scripted 프로파일·부하 테스트 rate limit을 GitHub Variables에서
EC2 env로 전달한다. 운영 환경에 실수로 `scripted`가 들어가면 가드가 컨테이너 기동을 막는다.
test 환경에서 scripted를 명시적으로 켠 동안에는 I-17 카탈로그 enrichment job도 등록하지 않아
결정론 가짜 생성물이 실 카탈로그와 cursor를 오염시키지 않는다(`app/pipelines/scheduler.py`).
기동 시 로그에 "STUB LLM MODE" 경고 배너가 남고(`app/main.py`), 서버 로그의
`chat_request.model_ids`에 `scripted-stub-fast`/`scripted-stub-smart`가 실려 `--server-log`로
조인한 보고서 최상단에 "STUB LLM MODE" 경고가 자동으로 붙는다(`report.py::render_markdown`).
`--server-log`가 없으면 스텁 여부를 추정하지 않고 "LLM 모드 미확인"만 남긴다.

이 모드가 재는 것 / 못 재는 것:

| 재는 것 | 못 재는 것 |
|---|---|
| FastAPI 이벤트루프 | **벤더 지연과 그 분산**(스텁은 네트워크 왕복이 없다) |
| SSE 스트리밍 | 토큰·비용 |
| pgvector 조회 | LLM 품질에 따른 fan-out 변동(#428의 8-leg degrade처럼 품질 결함이 rerank 비용을 2.50s → 8.80s로 밀어 올리는 효과) |
| 커넥션 풀(카테고리 매핑 앵커 조회 포함 — `category_search_pool_max_size`) | **판매자 레인**(`init_seller_model`이 scripted에서 명시적으로 거부한다 — 무료 모드는 구매자 레인 전용) |
| Spring 왕복(Spring이 떠 있으면) | Spring 미기동이면 검색 자체가 degrade라는 것 — LLM 무료화는 Spring을 대신해 주지 않는다 |
| | `color_synonym_seed`의 인라인 system 프롬프트 2곳(앵커 병합·꼬리 배정)은 스텁 마커 밖이라 `LLMError` → 호출부의 `except Exception`이 흡수해 항상 "미배정"으로 떨어진다(오프라인 배치라 부하 경로는 아니지만, 스텁 모드로 그 배치를 돌리면 안 된다는 뜻) |
| | 스텁은 다중 카테고리 leg 전개(`needs_expansion`)를 **트리거하지 않는다** — decompose 스텁이 항상 `case=1`(상품명이 발화에 있음)을 내므로, `case=3`(목적·상황형)에서만 도는 전개 경로는 이 모드로 재지 않는다 |
| | 스텁은 라우팅을 하지 않고 `LoadTestLLM._decompose_response`가 **항상 `intent=recommend`**를 낸다 — `buyer_fallback`처럼 다른 레인을 기대하는 시나리오(`expected_outcome.expect_lane`에 `recommend`가 없는 fixture)는 스텁 모드로 돌리면 매 요청 outcome mismatch로 집계된다. 코드 결함이 아니라 이 모드의 성질이며, 무료 모드에서는 `buyer_recommend` 계열 시나리오만 의미가 있다 |

**스텁 모드 p95를 실 LLM p95인 것처럼 인용하면 이 문서 상단의 정직성 규약 위반이다.** 실
LLM 비용 격자는 이 모드로 대신할 수 없다 — LLM 미포함 병목만 비용 없이 반복 측정하는 용도로만
쓴다. 특히 `delayed`는 대기시간과 SSE 연결 수명만 근사하며, 실제 provider 네트워크·connection
pool·429·토큰 생성 편차는 재현하지 않는다. 두 프로파일은 기동 배너의
`SCRIPTED_LLM_MODE=<mode> delay_s=<seconds>`로 구분한다.

동일 EC2를 쓸 때는 실제 사용자 트래픽을 먼저 차단하고, 테스트 전 GitHub Variables 값을 기록한다.
테스트가 끝나면 `APP_ENVIRONMENT`, `LLM_PROVIDER`, `SCRIPTED_LLM_*`, `RATE_LIMIT_*`를 모두 원복해
재배포한 뒤 `STUB LLM MODE` 배너가 사라지고 smoke 요청의 `chat_request.model_ids`가 실제 provider
모델인지 확인해야 한다. 상세 체크리스트는 `DEPLOY.md`의 "부하 테스트 전용 scripted 모드"를 따른다.

### 첫 무료 baseline (2026-08-09) — `baselines/20260809T014442747650Z-local-stub-spring`

Spring 기동 · pg 실물 · LLM만 스텁으로 `buyer_recommend` × 동시성 1/5/10/20 × measured 30을 돌린
결과다. 조건 전체는 그 디렉터리의 `manifest.json` `dependency_conditions`에 있다(요약: I-1 200,
I-21은 400이라 `degradeReason=push_skipped`가 상시, 레이트 리밋 상향, pg 컨테이너를 다른 레인과 공유).

| 동시성 | p50 ms | p95 ms | throughput req/s | success |
|---:|---:|---:|---:|---:|
| 1 | 903 | 946 | 1.08 | 30/30 |
| 5 | 1393 | 1960 | 3.18 | 30/30 |
| 10 | 2153 | 3279 | 3.84 | 30/30 |
| 20 | 3243 | 7032 | 3.99 | 30/30 |

읽는 법 — **처리량이 동시성 10 부근에서 포화**하고(3.84 → 3.99) 그 뒤로는 지연만 늘어난다.
LLM 지연이 빠졌으므로 이 포화점은 벤더가 아니라 **우리 코드·풀·pg 쪽 한계**를 가리킨다.
전 구간 error 0 · timeout 0이며, degrade 30/30은 위 I-21 400(BE 조건)이지 스텁 때문이 아니다.
서버 로그 조인율은 120/120이고 보고서 최상단에 STUB LLM MODE 배너가 자동으로 붙어 있다.

### 짝이 되는 실 LLM 좁은 격자 (2026-08-09) — `baselines/20260809T021733612671Z-local-realllm-spring`

**같은 조건에서 LLM만 실 벤더로 바꿔** 동시성 1/5 × 30건만 좁게 떴다(전 격자를 실 LLM으로 돌 이유가
없다 — 이슈 #438 본문). provider는 저장소 기본값 openai(fast=`gpt-5-nano` / smart=`gpt-5.6-luna`)다.
Anthropic 키는 401이라 이 격자는 openai로만 떴으므로 **벤더 간 비교가 아니다.**

**두 baseline의 차이가 곧 벤더 지연 기여분이다:**

| 동시성 | p50 스텁→실 | 벤더 기여분 | p95 스텁→실 | 벤더 기여분 | throughput 스텁→실 |
|---:|---|---:|---|---:|---|
| 1 | 903 → 5456 ms | **4553 ms (83%)** | 946 → 7821 ms | **6876 ms (88%)** | 1.08 → 0.17 req/s (6.4배) |
| 5 | 1393 → 6507 ms | **5114 ms (79%)** | 1960 → 9042 ms | **7083 ms (78%)** | 3.18 → 0.69 req/s (4.6배) |

읽는 법 — 실 LLM 턴에서 **지연의 8할 안팎이 벤더 구간**이다. 이슈가 "벤더 지연이 다른 병목을 전부
가려버린다"고 한 것이 그대로 실측됐다: 실 LLM으로만 재면 우리 코드의 포화점(무료 baseline이 보여준
동시성 10 부근)은 벤더 분산에 묻혀 보이지 않는다. 그래서 **용량 판단은 무료 모드로, 종단 예산 확인은
이 좁은 격자로** 나눠 쓴다.

**종단 예산 확인**: 관측 max는 c=1 8026ms · c=5 9631ms로, `stream_total_timeout_buyer_s=30`(30s)
대비 3배 이상의 여유가 있다. 이 격자의 용도가 정확히 이 확인이다.

토큰 사용량(리포트 집계 구간 63요청): prompt 431,301 · completion 31,144. `costUsd`는 단가표가
비어 있어 `price_missing`으로 `unknown`이다 — 비용 관측은 별도 이슈 #437 소관이며, 여기서는
0으로 추정하지 않고 토큰 수만 남긴다.
