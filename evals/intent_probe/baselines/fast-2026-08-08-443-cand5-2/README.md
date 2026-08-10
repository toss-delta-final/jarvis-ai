# cand5 런 2/2 — fast-2026-08-08-443 (**기각된 후보**, C5)

`decompose._SYSTEM` = **`6f64dcbd43d4`** — categoryQueries 불릿에 "사용자가 말한 상품군은
넓어도 그대로 담으세요" 예시 한 줄을 더한 판(C5). **이 후보는 기각됐다.** 판정 근거는
[`../fast-2026-08-08-443-before-1/README.md`](../fast-2026-08-08-443-before-1/README.md) §0 을
정본으로 삼는다.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

이 런의 값: `namedCategoryHasLeg` 38/48 · `conditionOnlyNoCategoryQuery` 37/40 ·
`categoryClear` 26/32 · `categoryMixedReplace` 30/32 · `switchLegacy2` 13/16 ·
`screenExactPick` 32/32 · `screenNoHallucination` 8/8 · `screenReask` 8/8 ·
`screenOutOfListConfirmCount` 0 · `mainIntent` 240/240.

디렉터리명은 예전에 `fast-2026-08-08-443-after-2` 였다 — "채택됐다"는 오해를 막기 위해
`cand5-2` 로 개명했다(파일 내용은 손대지 않았다).
