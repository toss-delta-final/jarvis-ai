# before 런 — fast-2026-08-08-465

`decompose._SYSTEM` = **`865ed6fd771e`**(출고판, 프롬프트 미변경)의 underspecified_probe 런.
정본은
[`../../intent_probe/baselines/fast-2026-08-08-443-before-1/README.md`](../../intent_probe/baselines/fast-2026-08-08-443-before-1/README.md)
§3·§4 — underspecified 표·`categoryQueries` 재집계가 모두 거기 있다.

```bash
uv run python -m evals.underspecified_probe --out <dir> --tier fast
```

(N=8 · tier fast · cellCount 30 · callCount 240 · 종료 코드 0 · 못 채운 셀 0)

이 런의 값: `missRate` 9/112(8.0%) · `falseAlarmRate` 4/104(3.8%) · `flagOffInvariant` 0/240 ·
`priorGateInvariant` 0/240. `causeAxisSummary.missBlockingAxisComboCounts`:
`filters.attrConditions` 5 · `categoryQueries;semanticQueryIsFallback` 3 ·
`semanticQueryIsFallback` 1 — `categoryQueries` 단독 차단 표본은 0건.
