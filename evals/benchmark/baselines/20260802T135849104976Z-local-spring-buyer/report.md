# Benchmark report

- server join: 60 / 60 (1.0)
- sample size rationale: 기본 하한 사용

| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold:buyer_fallback@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 0/3 | 0 | 3 | 0 | 0 | 1847.4010860081762 | 2466.3404340099078 | unknown | 2466.3404340099078 | 0.49674906852308504 |

- `cold:buyer_fallback@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_fallback@1` throughput: 3 requests / 6.039266483014217 seconds
- `cold:buyer_fallback@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_fallback@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=7359, completionTokens=536, models=['gpt-5-nano']
- `cold:buyer_fallback@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| cold:buyer_recommend@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 3/3 | 0 | 0 | 3 | 0 | 6685.813668009359 | 7019.055209006183 | unknown | 7019.055209006183 | 0.14904737881406102 |

- `cold:buyer_recommend@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_recommend@1` throughput: 3 requests / 20.127827969001373 seconds
- `cold:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_recommend@1` outcome reasons: unexpected_degrade=3
- `cold:buyer_recommend@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=16275, completionTokens=910, models=['gpt-5-nano', 'gpt-5.6-luna']
- `cold:buyer_recommend@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| measured:buyer_fallback@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 3/30 | 0 | 27 | 3 | 0 | 1965.2462720114272 | 10777.973404008662 | unknown | 12795.458765001968 | 0.34198810294738724 |

- `measured:buyer_fallback@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_fallback@1` throughput: 30 requests / 87.72234981699148 seconds
- `measured:buyer_fallback@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_fallback@1` outcome reasons: lane_mismatch(expected=[fallback],actual=recommend)=3, unexpected_degrade=3
- `measured:buyer_fallback@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=82945, completionTokens=7283, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_recommend@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 28/30 | 0 | 2 | 28 | 0 | 7231.076764001045 | 8778.849651978817 | unknown | 12021.906206005951 | 0.1392671871243619 |

- `measured:buyer_recommend@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@1` throughput: 30 requests / 215.41326869200566 seconds
- `measured:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@1` outcome reasons: unexpected_degrade=28
- `measured:buyer_recommend@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=156938, completionTokens=10096, models=['gpt-5-nano', 'gpt-5.6-luna']

> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.
> provider TTFT는 chat_request 로그에 없어 `unknown` (`not_in_chat_request_log`)으로 기록한다.
> reasoning/cache token은 서버가 내보내지 않아 `unknown` (`not_emitted_by_server`)으로 기록한다.
> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있다.
