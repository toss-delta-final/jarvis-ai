# Benchmark report

> ⚠️ **STUB LLM MODE — 이 수치는 벤더 지연을 포함하지 않는다. 실 LLM p95 로 인용 금지.**
> 관측된 스텁 모델 id: scripted-stub-fast, scripted-stub-smart

- server join: 120 / 120 (1.0)
- sample size rationale: 기본 하한 사용

| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold:buyer_recommend@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 3/3 | 0 | 0 | 3 | 0 | 945.4501769941999 | 1168.8908819996868 | unknown | 1168.8908819996868 | 0.9607810590467374 |

- `cold:buyer_recommend@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_recommend@1` throughput: 3 requests / 3.122459556994727 seconds
- `cold:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_recommend@1` outcome reasons: unexpected_degrade=3
- `cold:buyer_recommend@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=0, completionTokens=0, models=['scripted-stub-fast', 'scripted-stub-smart']
- `cold:buyer_recommend@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| measured:buyer_recommend@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 903.3608000027016 | 945.5652799952077 | unknown | 970.1532860053703 | 1.0757484876328214 |

- `measured:buyer_recommend@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@1` throughput: 30 requests / 27.887559540999064 seconds
- `measured:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@1` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=0, completionTokens=0, models=['scripted-stub-fast', 'scripted-stub-smart']
| measured:buyer_recommend@10 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 2152.703988002031 | 3279.213943002105 | unknown | 3510.959063998598 | 3.8372438741071715 |

- `measured:buyer_recommend@10` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@10` throughput: 30 requests / 7.8181113800019375 seconds
- `measured:buyer_recommend@10` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@10` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@10` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=0, completionTokens=0, models=['scripted-stub-fast', 'scripted-stub-smart']
| measured:buyer_recommend@20 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 3243.361927998194 | 7032.434213993838 | unknown | 7101.4291220053565 | 3.9879181457603483 |

- `measured:buyer_recommend@20` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@20` throughput: 30 requests / 7.522722107998561 seconds
- `measured:buyer_recommend@20` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@20` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@20` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=0, completionTokens=0, models=['scripted-stub-fast', 'scripted-stub-smart']
| measured:buyer_recommend@5 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 0 | 30 | 0 | 1393.2326230060426 | 1959.5124650004436 | unknown | 1960.0205570022808 | 3.1778080192118123 |

- `measured:buyer_recommend@5` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@5` throughput: 30 requests / 9.440469599998323 seconds
- `measured:buyer_recommend@5` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@5` outcome reasons: unexpected_degrade=30
- `measured:buyer_recommend@5` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=0, completionTokens=0, models=['scripted-stub-fast', 'scripted-stub-smart']

> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.
> provider TTFT는 chat_request 로그에 없어 `unknown` (`not_in_chat_request_log`)으로 기록한다.
> reasoning/cache token은 서버가 내보내지 않아 `unknown` (`not_emitted_by_server`)으로 기록한다.
> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있다.
