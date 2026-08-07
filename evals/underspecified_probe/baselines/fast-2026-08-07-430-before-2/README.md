# before 런 2/2 — fast-2026-08-07-430 (#430)

프롬프트 변경 **전**(`_SYSTEM` sha12 `11c6fe3bfa0c`)의 두 번째 독립 런. 목적·명령·한계는
`../fast-2026-08-07-430-before-1/README.md` 와 같다(그쪽에 before 2런 대조표가 있다).

(N=8 · 240콜 · 종료 코드 0 · 못 채운 셀 0 · 실패 0 · 관측 부분합 USD 0.0538)

| 축 | 값 |
|---|---|
| `missRate` | **111/112 (99.1%)** [95.1, 99.8] |
| `falseAlarmRate` | 0/104 (0.0%) [0.0, 3.6] |
| `judgmentAccuracy` | 105/216 (48.6%) |
| `flagOffInvariant` · `priorGateInvariant` | 0/240 · 0/240 |

미탐 원인 축(`missBlockingAxisComboCounts`): `semanticQueryIsFallback` 91 ·
`categoryQueries;semanticQueryIsFallback` 13 · `filters.attrConditions;semanticQueryIsFallback` 6 ·
`categoryQueries;filters.keyword;semanticQueryIsFallback` 1.

**run 1 과 `missRate` 가 정확히 같다(111/112).** 조합 분포는 run 1 과 다르다 — 어느 축이
함께 막았는지는 흔들려도 `semanticQueryIsFallback` 이 **모든 미탐의 필요조건**이라는 사실은
두 런에서 같다(91+13+6+1 = 111, 조합 전부에 이 축이 들어 있다).

채택 판정표 전문은 `../fast-2026-08-07-430-after-1/README.md`.
