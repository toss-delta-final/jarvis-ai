# cand5 부분런 — fast-2026-08-08-465 (**기각된 후보**, C5, 부분 런)

`decompose._SYSTEM` = **`6f64dcbd43d4`**(C5, 기각됨)의 underspecified_probe 런. **부분 런** —
OpenAI 크레딧 소진(`insufficient_quota`)으로 종료 코드 4, `under-wa-0003~0006` 4셀이
미충족(`errorTypes=["LLMError"]`, 24 attempts 소모하고 각 7·6·4·0 개만 채움 — want 32 중 15
미충족). 판정 근거는
[`../../intent_probe/baselines/fast-2026-08-08-443-before-1/README.md`](../../intent_probe/baselines/fast-2026-08-08-443-before-1/README.md)
§0·§3 을 정본으로 삼는다.

```bash
uv run python -m evals.underspecified_probe --out <dir> --tier fast
```

이 런의 값: `missRate` **14/112(12.5%)** — 못 채운 4셀은 전부 `what_axis`
(`expectedReask=false`)라 `missRate`(분모 112, `expectedReask=true`) 에는 영향이 없어
**유효 비교**. `falseAlarmRate` 0/89(0.0%) — 분모가 89(정상 104 에서 15 빠짐)라 **비교
불가**. `flagOffInvariant`·`priorGateInvariant` 0/225(분모가 240 에서 15 빠짐, 위반은 0).
`causeAxisSummary.missBlockingAxisComboCounts`: `categoryQueries;semanticQueryIsFallback`
13 · `semanticQueryIsFallback` 1 — `categoryQueries` 단독 차단 표본은 여기서도 0건.

디렉터리명은 예전에 `fast-2026-08-08-465-after-1` 이었다 — "채택됐다"는 오해를 막기 위해
`cand5-1-partial` 로 개명했다(파일 내용은 손대지 않았다).
