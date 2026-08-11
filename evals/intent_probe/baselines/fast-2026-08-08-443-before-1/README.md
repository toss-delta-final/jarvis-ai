# before 런 1/2 — fast-2026-08-08-443 (#443/#465 판정 근거, **정본**)

`decompose._SYSTEM` = **`865ed6fd771e`**(출고판, 프롬프트 **미변경**)의 독립 런. **이 디렉터리가
#443/#465 categoryQueries 축의 채택 판정 근거 정본**이다 — 요인 분리표, 전 축 전/후 대조표,
underspecified 재집계가 아래에 있다. **결론: 문면 후보(C5)는 기각됐다** — 프롬프트는 이 런이
잰 값 그대로 출고된다. 나머지 4개 디렉터리(`443-before-2`·`443-cand5-{1,2}`·
`../../underspecified_probe/baselines/fast-2026-08-08-465-{before-1,cand5-1-partial}`)는 이
README 를 가리킨다.

```bash
uv run python -m evals.intent_probe --out <dir> --tier fast
```

(N=8 · fixture `intent-probe-anchors-manifest-v7` · tier fast · cellCount 91 · callCount 848 ·
관측 비용 USD 0.166 · 종료 코드 0 · 못 채운 셀 0)

## 출고물 == 측정물

| | 값 |
|---|---|
| `prompt.source` | `repo:_SYSTEM` |
| `prompt.sha256` | `865ed6fd771e…` — before 두 런 모두. C5 두 런은 `6f64dcbd43d4…`(같은 방식으로
  `--dump-prompt` 대조 확인) |
| 모델 | `gpt-5-nano`(`fastReasoningEffort=minimal`) · provider `openai` |
| fixture | `intent-probe-anchors-manifest-v7`(`anchors_a.json`+`anchors_b.json`) |

## 0. 최종 판정 — **C5 기각**

사전 등록 기준: 「after 2런 모두 before 최댓값(36) 이상 **그리고** 평균 상승 ≥ +4/48」.

| `namedCategoryHasLeg` | before-1 | before-2 | cand5-1 | cand5-2 |
|---|---|---|---|---|
| /48 | 36 | 33 | **35** | 38 |

cand5-1(35) < before 최댓값(36) → 즉시 실패. 평균 상승 +2.0 < +4 → 실패. 스크리닝(부분 셀 11개,
tmp, 소실)에서 관측됐다는 46/48 은 같은 sha(`6f64dcbd43d4`)의 전체 런 2회에서 35·38/48 로
재현되지 않았다 — 미변경 프롬프트(before, `865ed6fd771e`)도 38·33·36·33 으로 흔들려 이 축의
런간 폭은 하네스 문서가 말하는 "축당 ±2"보다 훨씬 크다(≈5 이상). 동시에 반대 방향 비용이
`conditionOnlyNoCategoryQuery`(−2.0)·`categoryClear`(−4.5, #463 축)·underspecified `missRate`
(8.0%→12.5%)에서 관측됐다. 이득이 노이즈와 구분 안 되고 비용은 세 축에서 같은 방향으로 나와
**기각**했다. 상세 근거·기각된 문면 4종은 `app/agents/buyer/recommendation/decompose.py` 의
`_SYSTEM` 바로 아래 주석(`# ── categoryQueries 불릿의 "넓은 상품군" 예시 한 줄 — 시도했으나
기각 (#443)`)에 있다.

**재시도 조건**: 이 축은 런간 폭이 ≈5 라 부분 셀 스크리닝 1회로 채택하지 마라. N=8×6셀(48표본)
로는 +2 크기의 효과를 노이즈와 가를 수 없다 — 재시도하려면 `--n` 을 키운 전체 런 2회 이상으로
사전 등록 문턱을 다시 재라.

## 1. #443 요인 분리표 — `named_category` 6앵커, before 2런

| 앵커 | 발화 | before-1 (has-leg/8) | before-2 (has-leg/8) |
|---|---|---|---|
| `named-category-001` | 과일 추천해줘 | 2 | 1 |
| `named-category-002` | 나 아기 키우는데 과일 추천해줘 | 6 | 4 |
| `named-category-003` | 과일 추천해줘, 나 아기 키우고 있어서 | 5 | 5 |
| `named-category-004` | 나 아기 키우는데 유아용 물티슈 추천해줘 | 8 | 8 |
| `named-category-005` | 나 캠핑 다니는데 텐트 추천해줘 | 8 | 7 |
| `named-category-006` | 가성비 좋은 텀블러 추천해줘 | 7 | 8 |

`case` 필드(before 두 런 동일): 001~005 는 모두 `case=3`, 006 만 `case=2` — 이 그룹에서 거의
상수다. `case=3 ∧ categoryLegs 공백` 11·15 대 `case=3 ∧ 비공백` 29·25 로, 공백 여부가 case 와
함께 움직이지 않는다.

**결론 3줄**:
1. 상황 설명이 **없는** 001 이 가장 나쁘다(2·1/8) — "상황 설명 없음이 원인" 가설은 기각. 상황
   설명이 있는 002·003 도 001 보다 나을 뿐 여전히 낮다(6·4, 5·5).
2. 상황 설명의 **위치**(002: 선행 "나 아기 키우는데 과일" vs 003: 후행 "과일, ~ 있어서")로는
   안 갈린다 — 6·4 대 5·5 로 큰 차이가 없다.
3. `case` 는 이 그룹에서 거의 상수(006 만 2, 나머지 3)이고 `case3 ∧ 공백` 11·15 대
   `case3 ∧ 비공백` 29·25 라 공백과 함께 움직이지 않는다.

→ 남는 요인은 **상품군의 추상도**다: 001~003(넓은 대분류 "과일")은 낮고, 004~006(구체 상품명
"유아용 물티슈"·"텐트"·"텀블러")은 거의 항상 leg 이 찬다.

## 2. 전 축 전/후 대조표 (before 2런 vs cand5 2런)

| 축 | before-1 | before-2 | cand5-1 | cand5-2 |
|---|---|---|---|---|
| `namedCategoryHasLeg` (/48, confirmatory-primary) | 36 | 33 | 35 | 38 |
| `conditionOnlyNoCategoryQuery` (/40) | 39 | 40 | 38 | 37 |
| `categoryClear` (/32) | 29 | 29 | 23 | 26 |
| `categoryMixedReplace` (/32) | 27 | 26 | 30 | 30 |
| `switchLegacy2` (/16) | 8 | 9 | 10 | 13 |
| `screenExactPick` (/32) | 30 | 29 | 32 | 32 |
| `screenNoHallucination` (/8) | 8 | 8 | 8 | 8 |
| `screenReask` (/8) | 8 | 8 | 8 | 8 |
| `screenOutOfListConfirmCount` (진단) | 2 | 3 | 0 | 0 |
| `mainIntent` (/240) | 240 | 237 | 239 | 240 |

산출물(`results.json` `axes`/`diagnostics`)에서 다시 뽑아 위 표와 대조: **전부 일치**. 무회귀
축(`screenNoHallucination`·`screenReask`)도 8·8→8·8 로 확인됐다. 하락 축
(`categoryClear` −4.5 평균, `conditionOnlyNoCategoryQuery` −2.0 평균)은 이미 등록된 **#463**
(`[fix] decompose 비움 규칙이 screenExactPick·categoryClear 를 깎는다 — 문면으로는 못 잡는다`)
의 소관이라 여기서 새 이슈를 만들지 않는다.

## 3. underspecified 표 (#465, `evals/underspecified_probe`)

| 런 | `missRate` | `falseAlarmRate` | 비고 |
|---|---|---|---|
| `fast-2026-08-07-430-after-1`(커밋됨, #430 채택판) | 11/112 (9.8%) | 2/104 (1.9%) | 프롬프트 `865ed6fd771e` |
| `fast-2026-08-07-430-after-2`(커밋됨, #430 채택판) | 7/112 (6.2%) | 3/104 (2.9%) | 프롬프트 `865ed6fd771e` |
| `465-before-1`(오늘) | 9/112 (8.0%) | 4/104 (3.8%) | 프롬프트 `865ed6fd771e` |
| `465-cand5-1-partial`(오늘, C5) | 14/112 (12.5%) | 0/89 (0.0%) | 프롬프트 `6f64dcbd43d4`, 부분런 |

`465-cand5-1-partial` 은 종료 코드 4(OpenAI 크레딧 소진, `insufficient_quota`)로
`under-wa-0003~0006` 4셀이 미충족(`errorTypes=["LLMError"]`, 24 attempts 씩 소모하고
7·6·4·0 개만 채움 — 32 중 15 미충족)이다. 못 채운 4셀은 전부 `what_axis`
(`expectedReask=false`)라 **`missRate`(분모 112, `expectedReask=true`)에는 영향이 없다** — 이
수치는 유효 비교. 반면 `falseAlarmRate` 는 분모가 89(정상 104에서 15 빠짐)라 **비교 불가**다.
불변식: `flagOffInvariant`·`priorGateInvariant` 는 before 0/240, cand5-1-partial 은 분모가
줄어 **0/225**(240 에서 미충족 15 만큼 빠짐 — 위반은 0 이지만 분모가 다르다는 점을 그대로
적는다).

## 4. #465 재집계 — `categoryQueries` 단독 차단 표본은 0건

`causeAxisSummary.missBlockingAxisComboCounts`(miss 표본을 판정 뒤집는 데 필요했던 축 조합):

| 런 | `filters.attrConditions` | `categoryQueries;semanticQueryIsFallback` | `semanticQueryIsFallback` |
|---|---|---|---|
| `430-after-1` | 5 | 4 | 2 |
| `430-after-2` | 2 | 2 | 3 |
| `465-before-1` | 5 | 3 | 1 |

**핵심**: `categoryQueries` 가 **단독으로** 차단한 표본은 세 런 모두 **0건**이다(위 표에
`categoryQueries` 단독 키가 없다 — 항상 `semanticQueryIsFallback` 과 묶여야 miss 판정이
뒤집힌다, 즉 `causeAxes=multiple`). 가장 큰 **단독** 차단 조합은 `filters.attrConditions`
(5·2·5)이고 그건 **이미 등록된 #464**(`[fix] attrConditions 조작 억제를 문면 아닌 수단으로`)
의 소관이다 — 새 이슈를 만들지 않는다. 즉 #465 이슈 본문의 "남은 미탐의 주축은
`categoryQueries`" 는 출고판 산출물에서 그대로는 재현되지 않는다.

## 5. 정직 경고

스크리닝(부분 셀 11개) 산출물은 **보존되지 않았다** — 리포 밖(tmp)에 썼고 런타임 재시작으로
소실됐다. 위 0절의 "46/48"·"−15~−18/48"·"46/48 vs 33/40" 등 스크리닝 수치는 **런 보고에서 옮겨
적은 값**이고 이 산출물로 검증할 수 없다. 검증 가능한 값은 이 디렉터리·`443-before-2`·
`443-cand5-{1,2}`·`465-before-1`·`465-cand5-1-partial` 6개 산출물뿐이며, 위 1~4절의 표는 전부
그 산출물에서 다시 뽑아 대조했다.
