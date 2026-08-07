# after 런 2/2 — fast-2026-08-07-430 (#430)

프롬프트 변경 **후**(채택안 F, `_SYSTEM` sha12 `81e3770e1340`, `source=repo:_SYSTEM`)의 두 번째
독립 런. 채택 판정표·가드축 표·후보 선별표·`intent_probe` 타축 대조는 전부
`../fast-2026-08-07-430-after-1/README.md` 에 있다 — 여기 숫자를 그쪽 표 없이 인용하지 말 것.

(N=8 · 240콜 · 종료 코드 0 · 못 채운 셀 0 · 실패 0 · 관측 부분합 USD 0.0482)

| 축 | 값 |
|---|---|
| `missRate` | **3/112 (2.7%)** [0.9, 7.6] |
| `falseAlarmRate` | **0/104 (0.0%)** [0.0, 3.6] |
| `judgmentAccuracy` | 213/216 (98.6%) |
| `flagOffInvariant` · `priorGateInvariant` | 0/240 · 0/240 |
| 가드축(category·keyword 4앵커) | **0/32** |
| `nonRecommendIntentCount` | `{}` |
| `semanticQueryIsFallback` | 164/240 (`no_condition` 40 · `constraint_price` 38 · `constraint_budget_set` 31 · `blocking_rating` 26 · `multiturn_gate` 23 · `what_axis` 6) |

미탐 원인 축: `categoryQueries;semanticQueryIsFallback` 2 · `semanticQueryIsFallback` 1.

**독립 2런의 `missRate` 는 9.8% 와 2.7%** — 둘 다 before(99.1% × 2)보다 낮아 사전 등록 기준 1을
만족한다. 두 런의 차이(7.1pp)가 이 프롬프트에서의 런간 노이즈 폭이고, before 노이즈 폭(0pp)보다
넓다 — 단일 런으로 이 프롬프트를 판정하지 말 것.
