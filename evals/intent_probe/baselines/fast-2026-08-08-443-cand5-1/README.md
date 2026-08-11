# cand5 런 1/2 — fast-2026-08-08-443 (**기각된 후보**, C5)

`decompose._SYSTEM` = **`6f64dcbd43d4`** — categoryQueries 불릿에 "사용자가 말한 상품군은
넓어도 그대로 담으세요" 예시 한 줄을 더한 판(C5). **이 후보는 기각됐다** — 사전 등록 문턱
미달(`namedCategoryHasLeg` 35/48 < before 최댓값 36). 지금 출고되는 프롬프트는 이 변경을
포함하지 않는다(`865ed6fd771e`). 판정 근거·기각 사유는
[`../fast-2026-08-08-443-before-1/README.md`](../fast-2026-08-08-443-before-1/README.md) §0 을
정본으로 삼는다.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

이 런의 값: `namedCategoryHasLeg` **35/48** · `conditionOnlyNoCategoryQuery` 38/40 ·
`categoryClear` 23/32 · `categoryMixedReplace` 30/32 · `switchLegacy2` 10/16 ·
`screenExactPick` 32/32 · `screenNoHallucination` 8/8 · `screenReask` 8/8 ·
`screenOutOfListConfirmCount` 0 · `mainIntent` 239/240.

디렉터리명은 예전에 `fast-2026-08-08-443-after-1` 이었다 — "채택됐다"는 오해를 막기 위해
`cand5-1` 로 개명했다(파일 내용은 손대지 않았다).
