# Benchmark report

- server join: 60 / 60 (1.0)
- sample size rationale: 기본 하한 사용

| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold:buyer_recommend@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 3/3 | 0 | 0 | 3 | 0 | 7236.591635999503 | 7394.2394600017 | unknown | 7394.2394600017 | 0.1405523938243205 |

- `cold:buyer_recommend@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_recommend@1` throughput: 3 requests / 21.344353649001278 seconds
- `cold:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_recommend@1` outcome reasons: unexpected_degrade=3
- `cold:buyer_recommend@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=20607, completionTokens=1841, models=['gpt-5-nano', 'gpt-5.6-luna']
- `cold:buyer_recommend@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| measured:buyer_recommend@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 5456.403518997831 | 7821.348602003127 | unknown | 8025.626424998336 | 0.1688841083410769 |

- `measured:buyer_recommend@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@1` throughput: 30 requests / 177.6366071070006 seconds
- `measured:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@1` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=205282, completionTokens=13643, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_recommend@5 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 6507.476107995899 | 9042.410506001033 | unknown | 9630.771082003776 | 0.6919995858638411 |

- `measured:buyer_recommend@5` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@5` throughput: 30 requests / 43.35262710099778 seconds
- `measured:buyer_recommend@5` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@5` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@5` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=205412, completionTokens=15660, models=['gpt-5-nano', 'gpt-5.6-luna']

> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.
> provider TTFT는 chat_request 로그에 없어 `unknown` (`not_in_chat_request_log`)으로 기록한다.
> reasoning/cache token은 서버가 내보내지 않아 `unknown` (`not_emitted_by_server`)으로 기록한다.
> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있다.
