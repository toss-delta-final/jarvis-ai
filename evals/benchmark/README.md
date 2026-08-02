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
