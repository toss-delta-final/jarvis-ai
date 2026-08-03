# Benchmark report

- server join: 60 / 60 (1.0)
- sample size rationale: 기본 하한 사용

| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold:seller_analysis@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 0/3 | 0 | 3 | 0 | 0 | 10621.688362996792 | 11713.635058986256 | unknown | 11713.635058986256 | 0.09213085244596322 |

- `cold:seller_analysis@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:seller_analysis@1` throughput: 3 requests / 32.56238187701092 seconds
- `cold:seller_analysis@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:seller_analysis@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=34831, completionTokens=2148, models=['gpt-5.6-luna']
- `cold:seller_analysis@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| cold:seller_general@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 0/3 | 0 | 3 | 0 | 0 | 1789.8623580113053 | 1835.7154430123046 | unknown | 1835.7154430123046 | 0.3945655379985906 |

- `cold:seller_general@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:seller_general@1` throughput: 3 requests / 7.603299606998917 seconds
- `cold:seller_general@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:seller_general@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=8982, completionTokens=468, models=['gpt-5.6-luna']
- `cold:seller_general@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| measured:seller_analysis@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 0/30 | 0 | 30 | 0 | 0 | 10686.887485004263 | 11728.020682989154 | unknown | 12695.561644999543 | 0.09293993522340933 |

- `measured:seller_analysis@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:seller_analysis@1` throughput: 30 requests / 322.78912103699986 seconds
- `measured:seller_analysis@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:seller_analysis@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=350284, completionTokens=21946, models=['gpt-5.6-luna']
| measured:seller_general@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 0/30 | 0 | 30 | 0 | 0 | 1713.6194109916687 | 2517.001339001581 | unknown | 2554.597237991402 | 0.39925706645397774 |

- `measured:seller_general@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:seller_general@1` throughput: 30 requests / 75.13955924799666 seconds
- `measured:seller_general@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:seller_general@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=89820, completionTokens=4504, models=['gpt-5.6-luna']

> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.
> provider TTFT는 chat_request 로그에 없어 `unknown` (`not_in_chat_request_log`)으로 기록한다.
> reasoning/cache token은 서버가 내보내지 않아 `unknown` (`not_emitted_by_server`)으로 기록한다.
> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있다.
