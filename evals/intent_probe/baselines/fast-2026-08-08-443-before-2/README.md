# before 런 2/2 — fast-2026-08-08-443

`decompose._SYSTEM` = **`865ed6fd771e`**(출고판, 프롬프트 미변경)의 두 번째 독립 런. 정본은
[`../fast-2026-08-08-443-before-1/README.md`](../fast-2026-08-08-443-before-1/README.md) —
요인 분리표·전 축 대조표·판정 근거가 모두 거기 있다.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

이 런의 값: `namedCategoryHasLeg` 33/48 · `conditionOnlyNoCategoryQuery` 40/40 ·
`categoryClear` 29/32 · `categoryMixedReplace` 26/32 · `switchLegacy2` 9/16 ·
`screenExactPick` 29/32 · `screenNoHallucination` 8/8 · `screenReask` 8/8 ·
`screenOutOfListConfirmCount` 3 · `mainIntent` 237/240.
