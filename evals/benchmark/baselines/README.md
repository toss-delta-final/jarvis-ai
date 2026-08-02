# #151 로컬 no-Spring 성능 baseline 판독 안내

이 디렉터리의 `20260802T123439477798Z-local-nospring/`은 실제 OpenAI LLM과 로컬
PostgreSQL을 사용해 블랙박스 HTTP/SSE runner로 측정한 **로컬 기준선**이다. AWS staging이
아니며 Spring이 기동되지 않은 의존성 결손 조건이다. 따라서 이 수치를 staging 성능, 정상 의존성
상태의 degrade율, 또는 앱의 provider 독립적 동시성 한계로 읽어서는 안 된다. 원본
`report.md`·`metrics.csv`·`raw.jsonl`·`manifest.json`은 불변 측정 아티팩트다.

## 측정 조건

| 항목 | 값 |
|---|---|
| 측정 시각 | 2026-08-02 12:34:39~12:51:23 UTC |
| 타깃 | 로컬 uvicorn `http://127.0.0.1:8199` (`local-nospring`) |
| 모델 | 실 OpenAI LLM (`gpt-5-nano`, `gpt-5.6-luna`) |
| 데이터 저장소 | 로컬 `pg-catalog`·`pg-profile` 컨테이너(5433·5434) |
| Spring / staging | Spring 미기동(I-1 검색·I-21 push 불가), AWS staging 부재(`#135 blocked:spring`) |
| 표본 | measured 270건(3 시나리오 × 동시성 1·5·10 × 30건), 성공 223/270 |
| 타깃 레이트 리밋 | `RATE_LIMIT_PER_MIN=100000`, `RATE_LIMIT_PER_HOUR=100000` |
| 가격표 | 입력·출력 단가표 모두 비어 있음 — 비용 전부 `unknown` |

레이트 리밋 상향은 앱 코드 변경이 아니라 **측정 타깃의 환경변수 변경**이다. 기본값(분 10·시간
100)으로 한 첫 측정은 dev 게스트 신원을 모든 요청이 공유해 measured 270건 중 120건(44%)이
`429 RATE_LIMITED`였고, 세 번째 시나리오는 113건 전량이 429였다. 앱 자기 리밋을 측정한 이 실행은
성능 기준선으로 폐기했다. 아래 보존본에서는 앱 리밋 429가 0건이다.

실행 명령과 git SHA·lockfile SHA·클라이언트 런타임·정확한 시작/종료 시각은 해당
`manifest.json`에 함께 보존돼 있다.

## measured 결과

지연은 성공 요청의 `client_ttft_ms` 기준이다. p99는 그룹당 표본 30건이 계약 하한 100건보다
작아 전부 생략됐으며, 원본 리포트에 생략 사유가 기록돼 있다.

| group | p50 ms | p95 ms | max ms | success | degrade |
|---|---:|---:|---:|---|---|
| buyer_recommend@1 | 7064 | 9970 | 10359 | 30/30 | 29/30 |
| buyer_recommend@5 | 9542 | 12623 | 12789 | 30/30 | 26/30 |
| buyer_recommend@10 | 14355 | 16327 | 17826 | 29/30 | 28/30 |
| buyer_fallback@1 | 2100 | 11679 | 12130 | 30/30 | 3/30 |
| buyer_fallback@5 | 2450 | 3731 | 3731 | 13/30 | 0/30 |
| buyer_fallback@10 | 2498 | 2797 | 2797 | 2/30 | 0/30 |
| buyer_dependency_degrade@1 | 7541 | 8820 | 9626 | 30/30 | 28/30 |
| buyer_dependency_degrade@5 | 8461 | 10569 | 10657 | 30/30 | 30/30 |
| buyer_dependency_degrade@10 | 11665 | 14988 | 14989 | 29/30 | 27/30 |

## 해석 주의 — 숫자보다 먼저 읽을 것

### 1. `buyer_fallback` 동시성 실패는 로컬 API 키의 provider 쿼터다

실패 45건은 모두 HTTP 200 스트림 안의 `error` 이벤트이며 `error_type=LLM_UNAVAILABLE`이다.
서버 로그의 실제 원인은 OpenAI `POST /v1/chat/completions` 응답의 `429 Too Many Requests`다.
즉 OpenAI가 측정에 쓴 키를 스로틀한 것이며, 동시성 5·10의 성공률 저하와 지연에는 provider
스로틀이 섞였다. 이를 앱의 동시성 처리 능력으로 해석하면 안 된다. 이 조건에서는 동시성 1 수치가
가장 신뢰할 만하다.

### 2. Spring 부재 조건에서는 recommend degrade가 정상이다

`buyer_recommend`의 26~29/30 degrade는 I-1 검색과 I-21 push를 사용할 수 없어 발생한
`push_skipped`·`search_failed`다. staging에서는 이 비율이 달라진다. 같은 이유로 outcome mismatch가
높다. fixture의 `expect_degraded:false`는 정상 타깃의 기대이고, runner가 로컬 결손 조건과 기대가
다르다는 사실을 드러낸 것이므로 runner 오작동이 아니다.

추가로 pgvector 임베딩 재정렬에서 `statement timeout`이 관측됐다. 이는 `SEARCH_FAILED`로
분류된 사건이 아니라 로컬 DB 튜닝 조건에서 나타난 환경 아티팩트다.

### 3. 비용은 `$0`이 아니라 전 그룹 `unknown`이다

서버 로그에서 `promptTokens`·`completionTokens`는 실측됐지만 타깃의
`model_price_in_per_1k`·`model_price_out_per_1k` 가격표가 모두 비어 있었다. 단가 근거가 없으므로
서버가 내보낸 0을 비용 표본으로 신뢰하지 않았고, runner는 가격표 미등록 사유와 함께 비용을
`unknown`으로 보존했다.

## #138에서 이 기준선을 사용하는 법

현재 `slo_first_token_ms`는 10초이고, 가장 신뢰 가능한 동시성 1에서도
`buyer_recommend` p95가 9970ms로 경계에 닿는다. 동시성이 올라가면 p95는 10초를 초과한다. 다만 이
실행은 Spring 부재와 개인 OpenAI 키 스로틀이 섞인 로컬 측정이므로 값을 그대로 새 임계나 타임아웃으로
옮기면 안 된다. #138은 staging 재측정(#152)과 이 로컬 기준선을 함께 비교해 판단해야 하며, 이
문서는 특정 타임아웃 값을 제안하지 않는다.

## 재현

먼저 측정 타깃 프로세스에 `RATE_LIMIT_PER_MIN=100000 RATE_LIMIT_PER_HOUR=100000`을 주입한다.
그 뒤 아래 명령을 실행한다(서버 로그 경로는 재현 환경의 실제 경로로 바꾼다).

```bash
uv run python -m evals.benchmark.runner --base-url http://127.0.0.1:8199 --target-label local-nospring --scenarios buyer_recommend,buyer_fallback,buyer_dependency_degrade --concurrency 1,5,10 --measured-requests 30 --server-log /path/to/server.log --out-dir evals/benchmark/baselines --client-region local-wsl2 --instance-type local-dev-wsl2 --dependency-note 'Spring 미기동 — I-1 검색·I-21 push 불가(degrade 경로)' --dependency-note 'pg-catalog/pg-profile 로컬 컨테이너(5433/5434)' --dependency-note 'AWS staging 부재(#135 blocked:spring) — 로컬 측정' --dependency-note '벤치마크용으로 타깃의 RATE_LIMIT_PER_MIN/HOUR 를 100000 으로 상향(앱 코드 무변경, 타깃 환경변수만)'
```

## #138 로컬 Spring 기동 역할별 타임아웃 근거

`20260802T135849104976Z-local-spring-buyer/`와
`20260802T140556535202Z-local-spring-seller/`는 #151 기준선과 조건이 다르다. Spring 백엔드를
기동하고 유효한 `X-Internal-Token`으로 I-1 검색을 **42/42 200 OK**로 확인했으며, #151에서 빠졌던
판매자 시나리오를 처음 측정했다. 두 실행 모두 로컬 WSL2, 동시성 1, 시나리오당 measured n=30
전용이다. 원본 `report.md`·`metrics.csv`·`raw.jsonl`·`manifest.json`은 불변 측정 아티팩트다.

### 클라이언트 측 측정

| group | first_event p95 | first_text_token p95 | total p50 | total p95 | total max | error |
|---|---:|---:|---:|---:|---:|---:|
| buyer_recommend@1 | **2971ms** | 8779ms | 7236ms | 8786ms | 12031ms | 0/30 |
| buyer_fallback@1 | **2306ms** | 10778ms | 1969ms | 10783ms | 12804ms | 0/30 |
| seller_general@1 | **1775ms** | 2517ms | 2443ms | 3255ms | 3313ms | 0/30 |
| seller_analysis@1 | **1434ms** | 11728ms | 10700ms | 11730ms | 12698ms | 0/30 |

### 서버 측 role·lane 롤업

아래 표는 #137 `scripts/aggregate_observability.py`로 154턴을 집계한 결과다.

| role/lane | n | first_token p50 | p95 | p99 | total p95 |
|---|---:|---:|---:|---:|---:|
| guest/fallback | 35 | 1956 | 2464 | 2465 | 2465 |
| guest/recommend | 42 | 7085 | 10761 | 12773 | 10779 |
| seller/general | 38 | 1711 | 2515 | 2553 | 3251 |
| seller/analysis | 39 | 10684 | 11725 | 12693 | 11726 |
| **role:guest 전체** | 77 | 6214 | **10501** | 12773 | **10522** |
| **role:seller 전체** | 77 | 9049 | **11710** | 12693 | **11710** |

30초를 넘긴 턴은 0/154, 90초를 넘긴 턴도 0/154였고 최대 total은 12.8초였다. I-1 검색
지연은 직전 로그줄 기준 상한 추정 p50 302ms·p95 517ms·max 781ms(42/42 200), LLM 단일 호출은
같은 방식의 상한 추정 p50 1295ms·p95 4279ms였다. I-19
`/internal/members/{id}/orders`는 게스트 신원에 memberId가 없어 호출 0건이므로 측정하지 못했다.

### 판독 주의

1. `client_first_event_ms`와 `client_ttft_ms`는 다른 값이다. 런타임 first-token 상한이 강제하는
   대상은 **첫 SSE 이벤트인 전자**이며, 첫 텍스트 토큰인 후자가 아니다.
2. degrade 26.0%는 전량 I-21 `POST /internal/recommendations`의 400 응답에 따른
   `push_skipped`다. BE 데이터/계약 조건이며 앱 성능 문제가 아니다. error율은 0.0%다.
3. 이 결과는 로컬 WSL2 측정이며 AWS staging 결과가 아니다. #152가 같은 runner로 staging에서
   다시 측정한다.
4. `buyer_dependency_degrade`는 Spring 중단 조건을 전제하므로 Spring 기동 상태인 이번 실행에서
   제외했다.
5. p99는 그룹당 30표본이라 runner가 생략했다. 위 p99는 그룹별 runner 결과가 아니라
   **#137의 154턴 서버 집계**에서 나온 값이다.

### 재현

아래 명령은 각 `manifest.json`의 `command`를 그대로 옮긴 것이다. 판매자 인증은 토큰 값을
명령에 넣지 않고 `--auth-token-env BENCH_SELLER_TOKEN`으로만 주입한다.

구매자:

```bash
/home/uuser/orca/workspaces/jarvis-ai/chore-138-timeout-retune-from-metrics/.venv/bin/python3 -m evals.benchmark.runner --base-url http://127.0.0.1:8199 --target-label local-spring-buyer --scenarios buyer_recommend,buyer_fallback --concurrency 1 --measured-requests 30 --server-log /tmp/claude-1000/-home-uuser-orca-workspaces-jarvis-ai-chore-138-timeout-retune-from-metrics/83c4453a-837b-42a9-8ff0-f9270fa1fee3/scratchpad/bench/server.log --out-dir /tmp/claude-1000/-home-uuser-orca-workspaces-jarvis-ai-chore-138-timeout-retune-from-metrics/83c4453a-837b-42a9-8ff0-f9270fa1fee3/scratchpad/bench/out --client-region local-wsl2 --instance-type local-dev-wsl2 --dependency-note 'Spring 기동 — I-1 GET /internal/products/search 200 (X-Internal-Token 유효). #151 baseline 과 결정적으로 다른 조건' --dependency-note 'I-21 POST /internal/recommendations 는 400 응답 — degradeReason=push_skipped 상시 발생(BE 데이터/계약 조건, AI 코드 무관)' --dependency-note 'pg-catalog/pg-profile 로컬 컨테이너(5433/5434), catalog.products 7220건 임베딩 전량' --dependency-note 'AWS staging 부재(#135 blocked:spring) — 로컬 WSL2 측정' --dependency-note '타깃 RATE_LIMIT_PER_MIN/HOUR=100000 상향(앱 코드 무변경, 타깃 환경변수만)' --dependency-note '동시성 1 전용 — #151 에서 동시성 5·10 은 개인 OpenAI 키 provider 429 로 오염됨'
```

판매자:

```bash
/home/uuser/orca/workspaces/jarvis-ai/chore-138-timeout-retune-from-metrics/.venv/bin/python3 -m evals.benchmark.runner --base-url http://127.0.0.1:8199 --target-label local-spring-seller --scenarios seller_general,seller_analysis --concurrency 1 --measured-requests 30 --auth-token-env BENCH_SELLER_TOKEN --server-log /tmp/claude-1000/-home-uuser-orca-workspaces-jarvis-ai-chore-138-timeout-retune-from-metrics/83c4453a-837b-42a9-8ff0-f9270fa1fee3/scratchpad/bench/server.log --out-dir /tmp/claude-1000/-home-uuser-orca-workspaces-jarvis-ai-chore-138-timeout-retune-from-metrics/83c4453a-837b-42a9-8ff0-f9270fa1fee3/scratchpad/bench/out --client-region local-wsl2 --instance-type local-dev-wsl2 --dependency-note 'Spring 기동 — 판매자 집계 GET /internal/seller/{brandId}/sales·products 는 404(brandId=1 데이터 없음). 도구 수준에서 처리되며 lane=analysis degraded=false 로 완주' --dependency-note 'dev auth_mode 무서명 JWT(role=seller, sub=1, brandId=1) — 산출물에 토큰 미포함' --dependency-note 'pg-catalog/pg-profile 로컬 컨테이너(5433/5434)' --dependency-note 'AWS staging 부재(#135 blocked:spring) — 로컬 WSL2 측정' --dependency-note '타깃 RATE_LIMIT_PER_MIN/HOUR=100000 상향(앱 코드 무변경, 타깃 환경변수만)' --dependency-note '동시성 1 전용 — #151 에서 동시성 5·10 은 개인 OpenAI 키 provider 429 로 오염됨'
```
