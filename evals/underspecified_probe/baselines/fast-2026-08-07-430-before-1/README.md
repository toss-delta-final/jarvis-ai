# before 런 1/2 — fast-2026-08-07-430 (#430)

프롬프트 변경 **전**(`_SYSTEM` sha12 `11c6fe3bfa0c`)의 독립 재현 런. 2026-08-06 기준선
(`../fast-2026-08-06/`)이 단일 실행이라 채택 판정의 근거가 될 수 없어서, #430 이 채택 판정을
하기 전에 **before 분포를 2런으로 굳혔다**(#260 규약: 단일 실행 금지).

```bash
uv run python -m evals.underspecified_probe --out <dir> --tier fast
```

(N=8 · 30셀 · 240콜 · 종료 코드 0 · 못 채운 셀 0 · 실패 0 · 관측 부분합 USD 0.0514)

산출물은 스크래치패드로 `--out` 한 뒤 이 경로로 옮겼다 — `run_manifest.json.run.command` 의
`--out` 경로가 그래서 임시 경로다. 프롬프트 신원(`prompt.sha256` · `source=repo:_SYSTEM`)과
표본은 그대로다.

| 축 | 이 런 | before 런 2 | 2026-08-06 기준선 |
|---|---|---|---|
| `missRate` | **111/112 (99.1%)** [95.1, 99.8] | 111/112 (99.1%) | 112/112 (100.0%) |
| `falseAlarmRate` | 0/104 (0.0%) [0.0, 3.6] | 0/104 (0.0%) | 0/104 (0.0%) |
| `judgmentAccuracy` | 105/216 (48.6%) | 105/216 (48.6%) | 104/216 (48.1%) |
| `flagOffInvariant` | 0/240 | 0/240 | 0/240 |
| `priorGateInvariant` | 0/240 | 0/240 | 0/240 |

**before 노이즈 폭은 0pp 다** — 두 독립 런이 소수점까지 같은 값(111/112)을 냈고, 기준선과도
1표본 차이다. 출고판 after 런의 하락(**9.8% · 6.2%**)이 노이즈일 수 없다는 근거가 여기 있다.

`nonRecommendIntentCount` 는 `{}` — 240표본 전부 `intent=="recommend"` 로 라우팅됐다(F-1 의
intent 필터가 이 런에서는 분모를 바꾸지 않았다).

## 미탐 원인 축(`causeAxisSummary.missBlockingAxisComboCounts`)

| blockingAxes 조합 | 건수 |
|---|---|
| `semanticQueryIsFallback` | 91 |
| `filters.attrConditions;semanticQueryIsFallback` | 9 |
| `categoryQueries;semanticQueryIsFallback` | 8 |
| `filters.keyword;semanticQueryIsFallback` | 2 |
| `categoryQueries;filters.attrConditions;semanticQueryIsFallback` | 1 |

차단축은 **논리곱**이다 — `semanticQuery` 만 고쳐서는 91건만 뒤집힌다. after 런이 그보다
더 내려간 것은 `attrConditions` 규칙(할 일 ③)을 함께 고쳤기 때문이다.

채택 판정표 전문은 `../fast-2026-08-07-430-after-1/README.md`.
