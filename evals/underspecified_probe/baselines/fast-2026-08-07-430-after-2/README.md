# after 런 2/2 — fast-2026-08-07-430 (#430, **출고판**)

`_SYSTEM` = **`865ed6fd771e`**(7838자, `source=repo:_SYSTEM`)의 두 번째 독립 런.
채택 판정표·가드축 해석·후보 이력·`intent_probe` 타축 대조는 전부
`../fast-2026-08-07-430-after-1/README.md` 가 정본이다 — 여기 숫자를 그 표 없이 인용하지 말 것.

(N=8 · 240콜 · 종료 코드 0 · 못 채운 셀 0 · 실패 0 · 관측 부분합 USD 0.0558)

| 축 | 값 |
|---|---|
| `missRate` | **7/112 (6.2%)** [3.1, 12.3] |
| `falseAlarmRate` | **3/104 (2.9%)** [1.0, 8.1] — 사전 등록 상한 3.6% 아래 |
| `judgmentAccuracy` | 206/216 (95.4%) |
| `flagOffInvariant` · `priorGateInvariant` | 0/240 · 0/240 |
| 가드축(category·keyword 4앵커) | **0/32** |
| `nonRecommendIntentCount` | `{}` |

오탐 표본(caseId·n): `buy-under-0005` n=0 · n=1(삼성 제품 아무거나) · `under-wa-0002` n=7
(LG 가전 아무거나 있어?) — 전부 `what_axis`, 브랜드 추출 실패가 드러난 것이다.
가드 위반 **없음**. 미탐 원인 축: `semanticQueryIsFallback` 3 ·
`categoryQueries;semanticQueryIsFallback` 2 · `filters.attrConditions` 2.

**독립 2런의 `missRate` 는 9.8% 와 6.2%** — 둘 다 before(99.1% × 2)보다 낮아 사전 등록 기준 1을
만족한다.
