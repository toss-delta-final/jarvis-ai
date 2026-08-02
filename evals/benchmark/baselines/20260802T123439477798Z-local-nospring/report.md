# Benchmark report

- server join: 270 / 270 (1.0)
- sample size rationale: 기본 하한 사용

| group | reliability denominator | latency denominator | success | error | timeout | degrade (known denominator) | degrade unknown | outcome match | outcome mismatch | outcome unknown | p50 ms | p95 ms | p99 ms | max ms | throughput req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cold:buyer_dependency_degrade@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 3/3 | 0 | 3 | 0 | 0 | 6836.416085017845 | 7812.6749189978 | unknown | 7812.6749189978 | 0.14042250615166754 |

- `cold:buyer_dependency_degrade@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_dependency_degrade@1` throughput: 3 requests / 21.36409669800196 seconds
- `cold:buyer_dependency_degrade@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_dependency_degrade@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=16479, completionTokens=1374, models=['gpt-5-nano', 'gpt-5.6-luna']
- `cold:buyer_dependency_degrade@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| cold:buyer_fallback@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 1/3 | 0 | 2 | 1 | 0 | 2334.1323409986217 | 11654.95229899534 | unknown | 11654.95229899534 | 0.1846315421817933 |

- `cold:buyer_fallback@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_fallback@1` throughput: 3 requests / 16.24857792200055 seconds
- `cold:buyer_fallback@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_fallback@1` outcome reasons: lane_mismatch(expected=[fallback],actual=recommend)=1, unexpected_degrade=1
- `cold:buyer_fallback@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=10459, completionTokens=974, models=['gpt-5-nano', 'gpt-5.6-luna']
- `cold:buyer_fallback@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| cold:buyer_recommend@1 | 3 | 3 | 3/3 | 0/3 | 0/3 | 3/3 | 0 | 0 | 3 | 0 | 7495.494164992124 | 9749.8928210116 | unknown | 9749.8928210116 | 0.12195524175971198 |

- `cold:buyer_recommend@1` p99 omitted: `insufficient_samples(n=3 < 100)`
- `cold:buyer_recommend@1` throughput: 3 requests / 24.599188658990897 seconds
- `cold:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `cold:buyer_recommend@1` outcome reasons: unexpected_degrade=3
- `cold:buyer_recommend@1` server metrics: joined=3, cost=$unknown (samples=0, unknown=3, price_missing=3), promptTokens=16272, completionTokens=872, models=['gpt-5-nano', 'gpt-5.6-luna']
- `cold:buyer_recommend@1` cold 표본은 n=3로 작아 CI·p95를 강한 성능 주장에 사용하지 않는다.
| measured:buyer_dependency_degrade@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 28/30 | 0 | 28 | 2 | 0 | 7541.273790993728 | 8820.029105991125 | unknown | 9626.437266997527 | 0.13612774908779865 |

- `measured:buyer_dependency_degrade@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_dependency_degrade@1` throughput: 30 requests / 220.38122426200425 seconds
- `measured:buyer_dependency_degrade@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_dependency_degrade@1` outcome reasons: degrade_not_observed=2
- `measured:buyer_dependency_degrade@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=158708, completionTokens=14241, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_dependency_degrade@10 | 30 | 29 | 29/30 | 1/30 | 0/30 | 27/30 | 0 | 27 | 3 | 0 | 11665.226230979897 | 14988.394037995022 | unknown | 14989.12157700397 | 0.7782933096239408 |

- `measured:buyer_dependency_degrade@10` p99 omitted: `insufficient_samples(n=29 < 100)`
- `measured:buyer_dependency_degrade@10` throughput: 30 requests / 38.54587933499715 seconds
- `measured:buyer_dependency_degrade@10` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 1건
- `measured:buyer_dependency_degrade@10` outcome reasons: degrade_not_observed=3, lane_not_observed=1, terminal_mismatch(expected=done,actual=unknown)=1, token_not_observed=1
- `measured:buyer_dependency_degrade@10` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=29), promptTokens=153326, completionTokens=13432, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_dependency_degrade@5 | 30 | 30 | 30/30 | 0/30 | 0/30 | 30/30 | 0 | 30 | 0 | 0 | 8461.32337700692 | 10568.654987990158 | unknown | 10657.496386993444 | 0.5386444046143762 |

- `measured:buyer_dependency_degrade@5` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_dependency_degrade@5` throughput: 30 requests / 55.6953710889793 seconds
- `measured:buyer_dependency_degrade@5` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_dependency_degrade@5` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=164698, completionTokens=15456, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_fallback@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 3/30 | 0 | 26 | 4 | 0 | 2099.6096860035323 | 11678.795432992047 | unknown | 12129.947634006385 | 0.3231374035727805 |

- `measured:buyer_fallback@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_fallback@1` throughput: 30 requests / 92.83976311099832 seconds
- `measured:buyer_fallback@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_fallback@1` outcome reasons: lane_mismatch(expected=[fallback],actual=recommend)=4, unexpected_degrade=3
- `measured:buyer_fallback@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=82903, completionTokens=7322, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_fallback@10 | 30 | 2 | 2/30 | 28/30 | 0/30 | 0/30 | 0 | 2 | 28 | 0 | 2497.524020000128 | 2796.864473988535 | unknown | 2796.864473988535 | 5.854485355562252 |

- `measured:buyer_fallback@10` p99 omitted: `insufficient_samples(n=2 < 100)`
- `measured:buyer_fallback@10` throughput: 30 requests / 5.1242762049951125 seconds
- `measured:buyer_fallback@10` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 28건
- `measured:buyer_fallback@10` outcome reasons: lane_not_observed=28, terminal_mismatch(expected=done,actual=error)=28, token_not_observed=28
- `measured:buyer_fallback@10` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=4906, completionTokens=356, models=['gpt-5-nano']
| measured:buyer_fallback@5 | 30 | 13 | 13/30 | 17/30 | 0/30 | 0/30 | 0 | 11 | 19 | 0 | 2450.483569991775 | 3731.3176630123053 | unknown | 3731.3176630123053 | 2.598695287659124 |

- `measured:buyer_fallback@5` p99 omitted: `insufficient_samples(n=13 < 100)`
- `measured:buyer_fallback@5` throughput: 30 requests / 11.544254589010961 seconds
- `measured:buyer_fallback@5` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 17건
- `measured:buyer_fallback@5` outcome reasons: lane_mismatch(expected=[fallback],actual=recommend)=2, lane_not_observed=17, terminal_mismatch(expected=done,actual=error)=17, token_not_observed=17
- `measured:buyer_fallback@5` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=31889, completionTokens=2692, models=['gpt-5-nano']
| measured:buyer_recommend@1 | 30 | 30 | 30/30 | 0/30 | 0/30 | 29/30 | 0 | 1 | 29 | 0 | 7064.374487003079 | 9969.979304994922 | unknown | 10359.331281011691 | 0.1400755621448779 |

- `measured:buyer_recommend@1` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@1` throughput: 30 requests / 214.1701203309931 seconds
- `measured:buyer_recommend@1` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@1` outcome reasons: unexpected_degrade=29
- `measured:buyer_recommend@1` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=159757, completionTokens=10110, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_recommend@10 | 30 | 29 | 29/30 | 1/30 | 0/30 | 28/30 | 0 | 2 | 28 | 0 | 14355.36454297835 | 16326.81445000344 | unknown | 17826.067904010415 | 0.6647630477758486 |

- `measured:buyer_recommend@10` p99 omitted: `insufficient_samples(n=29 < 100)`
- `measured:buyer_recommend@10` throughput: 30 requests / 45.12886223199894 seconds
- `measured:buyer_recommend@10` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 1건
- `measured:buyer_recommend@10` outcome reasons: terminal_mismatch(expected=done,actual=error)=1, token_not_observed=1, unexpected_degrade=28
- `measured:buyer_recommend@10` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=156001, completionTokens=8844, models=['gpt-5-nano', 'gpt-5.6-luna']
| measured:buyer_recommend@5 | 30 | 30 | 30/30 | 0/30 | 0/30 | 26/30 | 0 | 4 | 26 | 0 | 9542.034041020088 | 12623.457107983995 | unknown | 12788.776144006988 | 0.5043858209999966 |

- `measured:buyer_recommend@5` p99 omitted: `insufficient_samples(n=30 < 100)`
- `measured:buyer_recommend@5` throughput: 30 requests / 59.47827784001129 seconds
- `measured:buyer_recommend@5` latency 제외 규칙: 성공(non-empty token 수신 + terminal `done`) 요청만 포함. 제외 0건
- `measured:buyer_recommend@5` outcome reasons: unexpected_degrade=26
- `measured:buyer_recommend@5` server metrics: joined=30, cost=$unknown (samples=0, unknown=30, price_missing=30), promptTokens=151385, completionTokens=9121, models=['gpt-5-nano', 'gpt-5.6-luna']

> p50/p95는 #137 `scripts/aggregate_observability.py`와 동일한 최근접 순위 정의다.
> provider TTFT는 chat_request 로그에 없어 `unknown` (`not_in_chat_request_log`)으로 기록한다.
> reasoning/cache token은 서버가 내보내지 않아 `unknown` (`not_emitted_by_server`)으로 기록한다.
> client TTFT는 커넥션 재사용 시 request-byte send 기준에 수렴한다. 풀에 유휴 연결이 없으면 httpx가 정확한 byte-send hook을 제공하지 않아 DNS·TCP·TLS 연결 수립 및 풀 대기 시간이 포함될 수 있다.
