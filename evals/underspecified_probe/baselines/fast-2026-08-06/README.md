# 기준선 — fast-2026-08-06

첫 실 LLM 실측(#380 이슈 완료 조건 6, acceptance run). **리뷰 라운드 1(F-1~F-6) 수정을 모두
반영한 뒤 재실행한 표다** — 이전 판(intent 필터 없이 전 표본을 confirmatory 분모에 넣던 표,
원인 축 분해를 `multiple` 로 뭉뚱그려 `filters.attrConditions` 를 감췄던 표, `measurementScope`
가 `detect_expansion_need` 호출과 모순됐던 표, 표본 0인 슬라이스가 "exploratory: N<40" 으로
잘못 인쇄되던 표)은 이 표로 교체됐다(legs_probe 기준선이 리뷰 라운드 뒤 재실행해 교체한 선례와
동형, `evals/legs_probe/baselines/fast-2026-08-06/README.md` 참조).

```bash
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06 --tier fast
```

(기본 N=8, 종료 코드 0, 못 채운 셀 0, 실패 0건)

## 헤더

`prompt=11c6fe3bfa0c (repo:_SYSTEM)` · `tier=fast` · `model=gpt-5-nano` ·
`fixture=underspec-anchors-v1` · `N=8` · `underspecifiedReaskEnabled=true`(판정 고정값, §D6 —
`.env` 의 실제 기본값 False 와 무관). 240콜 · 페이싱 대기 96회(45rpm) · 못 채운 셀 0.

**비용·토큰은 부분합이다.** `results.json.budget` 은 `unknownCostCallCount=38`·
`unknownTokenCallCount=38`(240콜 중 38콜은 provider 가 usage 를 보고하지 않아 집계에서 빠졌다).
**관측된 부분합은 $0.0510 · 758,860 tokens(202콜 기준)** 이며, 이 숫자를 "이 런의 총 비용"으로
인용하면 안 된다(실제 총 비용은 이보다 크다).

## Primary confirmatory 지표 — missRate

> ⚠️ **아래 수치는 decompose 직후·판정 직전 형상의 측정이다.** 프로덕션은 decompose 뒤에
> 카테고리 매핑·`needs_expansion` 보정을 거친다 — 상세는
> `evals/underspecified_probe/README.md` §측정 범위와 한계.

`missRate`(§F-1 — `intent=="recommend"` 표본으로 분모를 좁힌 뒤) = **112/112 (100.0%)**,
CI95 [96.7%, 100.0%]. 이번 런은 `nonRecommendIntentCount` 가 **`{}`(빈 dict)** 다 — 30 앵커
× N=8 표본 전부가 `intent=="recommend"` 로 라우팅됐으므로, F-1 의 intent 필터는 이번 런의
분모를 실제로는 바꾸지 않았다(포함판 `missRateWithNonRecommendIntent` 도 동일하게 112/112 —
아래 「LLM vs baseline」참조). 필터가 이번 런에서 무언가를 제외하지 않았다는 것이 F-1 수정이
불필요했다는 뜻은 아니다 — intent 라우팅은 발화·티어·시드에 따라 달라지는 확률적 산출이라,
다른 런에서는 비-recommend 표본이 나올 수 있다(§D14 항목 7 유닛테스트가 그 배관을 합성
표본으로 고정한다).

슬라이스별(사전 등록):

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 40/40 (100.0%) | [91.2%, 100.0%] |
| constraint_price | 40/40 (100.0%) | [91.2%, 100.0%] |
| constraint_budget_set(N<40, exploratory) | 32/32 (100.0%) | [89.3%, 100.0%] |

**해석**: `fast`(gpt-5-nano) 는 "무엇을 찾는지"를 지정하지 않은 발화("아무거나 추천해줘"·
"5만원 이하로 아무거나")에서 **단 한 건도** `semanticQuery` 를 비워두지 않았다(missRate
100.0%, outcome 분포에 `hit` 0건) — 원인 축 분해(아래)가 미탐 112건을 `blockingAxes` 조합
셋으로 정확히 나눈다: `semanticQueryIsFallback` 단독 92건(82.1%) ·
`categoryQueries;semanticQueryIsFallback` 9건(8.0%) ·
`filters.attrConditions;semanticQueryIsFallback` 11건(9.8%). `_SYSTEM` 프롬프트는
semanticQuery 를 "찾는 상품의 의미"로 정의할 뿐 "지정할 게 없으면 비워라"는 지시를 두지
않으므로, 모델이 "인기 상품"·"아무 상품" 류의 의미쿼리나(9건은 거기 더해 `categoryQueries`
까지) 무조건 발화에도 속성 조건을 스스로 지어내는 것으로 보인다(11건, **`filters.
attrConditions` 가 두 번째 원인 축이라는 사실은 F-2 수정 전에는 `multiple` 로 뭉개져 문서에서
드러나지 않았다**). **이것이 이번 실측의 핵심 발견이다** — 이슈 제목이 지목한 "기본 on
전환의 남은 전제"는 이 축에서 아직 충족되지 않았다.

## 축 전체

| 축 | 점수 | CI95 | 성격 |
|---|---|---|---|
| `missRate` | 112/112 (100.0%) | [96.7%, 100.0%] | confirmatory-primary |
| `missRateWithNonRecommendIntent` | 112/112 (100.0%) | [96.7%, 100.0%] | exploratory(F-1 포함판) |
| `falseAlarmRate`(게이트 제외) | 0/104 (0.0%) | [0.0%, 3.6%] | confirmatory-secondary |
| `falseAlarmRateWithGateSlice` | 0/128 (0.0%) | [0.0%, 2.9%] | exploratory |
| `falseAlarmRateWithNonRecommendIntent` | 0/104 (0.0%) | [0.0%, 3.6%] | exploratory(F-1 포함판) |
| `judgmentAccuracy`(게이트 제외) | 104/216 (48.1%) | [41.6%, 54.8%] | exploratory |
| `judgmentAccuracyWithGateSlice` | 128/240 (53.3%) | [47.0%, 59.5%] | exploratory |
| `missRateUnderExpansionAssumption` | 112/112 (100.0%) | [96.7%, 100.0%] | exploratory(상한) |
| `expansionGateWouldFireRate` | 0/0 | N/A | exploratory(진단) — **해당 없음**(F-5): 이번 런은 판정 True 표본이 0건이라 분모 자체가 없다 |
| `flagOffInvariant` | 0/240 (0.0%) | [0.0%, 1.6%] | invariant — **위반 없음** |
| `priorGateInvariant` | 0/240 (0.0%) | [0.0%, 1.6%] | invariant — **위반 없음** |

전체 정의는 `../../README.md` §「축과 정의」참조 — 분자·분모 문장은
`results.json.axes[*].definition` 에도 그대로 실린다.

## 슬라이스 표

`falseAlarmRate` 는 사전 등록 슬라이스(`what_axis`·`blocking_rating`) 둘 다 **0.0%** —
`what_axis`(브랜드·카테고리·색상·키워드 신호가 있는 발화)·`blocking_rating`(평점 하한이 있는
발화)에서는 fast 티어가 그 신호를 what/차단-축으로 안정적으로 채워, 판정이 한 번도 오탐하지
않았다. `judgmentAccuracy` 는 `what_axis`·`blocking_rating` 에서 100.0% 인 반면
`no_condition`·`constraint_price`·`constraint_budget_set` 는 전부 **0.0%** — **판정 자체
(코드)는 정상이지만, 그 판정이 소비하는 decompose 산출이 "무조건" 계열 발화 전 슬라이스에서
체계적으로 어긋난다**는 뜻이다(원인 축 분해 참조). `multiturn_gate` 는 `missRate`·
`falseAlarmRate` 양쪽에서 "해당 없음"(F-5) — 이 슬라이스는 `expectedReask=false` 전용(멀티턴
게이트)이라 애초에 `missRate` 분모에, 또 `priorExists=true` 라 `falseAlarmRate`(제외판) 분모에
들어갈 수 없다.

## LLM vs baseline (trivial baseline: 항상 reask=false)

| | LLM judgmentAccuracy | baseline judgmentAccuracy |
|---|---|---|
| **전체(게이트 제외)** | 48.1% | 48.1% |
| no_condition | 0.0% | 0.0% |
| constraint_price | 0.0% | 0.0% |
| constraint_budget_set | 0.0%(N<40) | 0.0% |
| what_axis | 100.0% | 100.0% |
| blocking_rating | 100.0% | 100.0% |
| multiturn_gate | 해당 없음 | 100.0% |

baseline `missRate` = 1.0(구조적) · baseline `falseAlarmRate` = 0.0(정의상). **전체 판정
정확도가 baseline 과 정확히 동률(48.1% = 48.1%)이다** — `no_condition`·`constraint_price`·
`constraint_budget_set` 세 슬라이스 전부 LLM 이 baseline 과 완전히 같고(0.0% = 0.0%),
`what_axis`·`blocking_rating` 도 둘 다 100.0% 로 같다. 이번 런에서는 **판정이 baseline("항상
reask=false")과 구별되지 않는다** — #275 가 랭킹 축에서 밟은 것과 같은 모양이다.

## 원인 축 분해 (이슈 완료 조건 3)

outcome 분포: `{correctReject: 128, miss: 112}`(hit·오탐 0건).

### `blockingAxes` 조합별 분포 (F-2 — 서술이 아니라 실측 조합)

| blockingAxes 조합 | 미탐 표본 수 | 비중 |
|---|---|---|
| `semanticQueryIsFallback`(단독) | 92 | 82.1% |
| `filters.attrConditions;semanticQueryIsFallback` | 11 | 9.8% |
| `categoryQueries;semanticQueryIsFallback` | 9 | 8.0% |

(단일 축 기준 `missCauseAxisCounts` 는 `semanticQueryIsFallback` 92건·`multiple` 20건으로
집계되며, 위 표가 그 `multiple` 20건을 두 조합으로 정확히 나눈다 — **`filters.
attrConditions` 가 `categoryQueries` 보다 더 잦은 두 번째 원인 축**이라는 사실은 이 조합 표
없이는 알 수 없었다, F-2.)

오탐은 0건이라 표가 비어 있다. 표본별 상세(`intent`·`case`·`semanticQueryIsFallback`·
`semanticQuery` 원문 포함, F-1)는 `samples.csv` 에 있다 — 런 재실행 없이 재집계할 수 있다.

## 불변 재판정 결과 (§D9)

`flagOffInvariant` = 0/240 · `priorGateInvariant` = 0/240 — **둘 다 위반 없음**. 240개 표본
전부에 대해 `underspecified_reask_enabled=False` 로 재판정해도, `prior=ProductSearchFilters()`
로 재판정해도 True 가 한 건도 안 나왔다(LLM 콜 0, intent 무관 — F-1). `buy-under-0008`(플래그
off 롤백 경로, 셀 제외)·`buy-under-0006`(멀티턴 게이트)의 실측 일반화다.

## ⚠️ D10/F-3 측정 범위와 한계 경고

1. **전개 게이트**: `expansionGateWouldFireRate` 는 이번 런에서 **분모 자체가 0**(판정 True
   표본이 0건이라 F-5 규약대로 "해당 없음"으로 인쇄된다). `needs_expansion` 효과의 상한
   (`missRateUnderExpansionAssumption`)도 `missRate` 와 완전히 같은 값(112/112)이다 — 이 런에서
   판정이 사실상 항상 False(=되물음 안 함)이므로, 전개 게이트가 그 판정을 뒤집을 기회 자체가
   없었다. 이는 §측정 범위와 한계 항목 3(전개 게이트 판정 함수는 부르지만 전개 자체는 안 부름,
   F-3)의 상한 가정이 이 런에서는 거의 작동하지 않았다는 뜻이지, 전개 게이트의 실제 효과가
   작다는 근거가 아니다.
2. **`filters.category` 덮어쓰기**: `categoryEchoWithoutQueriesCount` = 0 — 이 데이터셋에서는
   이 괴리가 공허하다(구조적으로 decompose 가 `filters.category` 를 채울 수 없어서다, §D10
   항목 2).
3. **intent 라우팅**: `nonRecommendIntentCount` = `{}` — 이번 런은 confirmatory 분모에서
   제외된 표본이 0건이었다(F-1). 다른 런에서는 0이 아닐 수 있다.
4. `category_legs` 는 이 런에서도 항상 비었다(카테고리 매핑을 부르지 않는다).
5. 단일 턴만 잰다(컨텍스트 행렬 없음).

## ⚠️ 단일 실행은 채택 판정이 아니다

이 런은 **1회 실행**이다. `missRate` 100.0%(CI95 [96.7%, 100.0%])는 리뷰 라운드 1 이전 실측
(111/112, 99.1%)과 같은 방향·비슷한 크기의 결과다 — 우연이 아님을 시사하지만, 프롬프트
개선이나 티어 비교 판단의 근거로 쓰려면 독립 2~3회 분포를 봐야 한다(`evals/intent_probe` 의
재현 함정 4). 이 런의 목적은 하네스 acceptance(D15)와 리뷰 라운드 1 수정 검증, 그리고
SPEC-UNDERSPECIFIED-336 §7.3 게이트 잔여 항목 1을 고정 데이터셋 위에서 수치화하는 것이다 —
**`underspecified_reask_enabled` 기본값 전환은 이 실측만으로 결정하지 않는다**(그 결정은 별도
이슈의 몫이다).

## 추기 (#433) — 이제 n=3 이다, 그리고 이 family 는 #430 after 의 대조 상대가 아니다

위 경고("단일 실행은 채택 판정이 아니다")를 지우지 않고 이 절만 덧붙인다 — 이 런의 원본
수치·표는 개변하지 않는다.

`run2`(`../fast-2026-08-06-run2/`)·`run3`(`../fast-2026-08-06-run3/`) 을
`--prompt-rev 798f0a965385bfdedbe20646c3e8a07ba73ea08b` 로 재현해 이 family 를 n=3 으로
채웠다. 세 판 모두 같은 `prompt.sha12`(`11c6fe3bfa0c`) · 같은 `hashes.anchorFixture`.

| 런 | `missRate` | `falseAlarmRate` | `judgmentAccuracy` |
|---|---|---|---|
| run1(이 문서, 커밋된 기준선) | 112/112 (100.0%) | 0/104 (0.0%) | 104/216 (48.1%) |
| run2 | 112/112 (100.0%) | 0/104 (0.0%) | 104/216 (48.1%) |
| run3 | 112/112 (100.0%) | 0/104 (0.0%) | 104/216 (48.1%) |

세 판이 소수점까지 완전히 일치한다 — 편차 0%p. 상세·`blockingAxes` 조합 분포는
`../README.md`(기준선 색인) 참조.

**⚠️ 이 family(prompt `11c6fe3bfa0c`)는 pre-#386 프롬프트 세대(세대 1/3)다.** `app/agents/
buyer/recommendation/decompose.py::_SYSTEM` 은 커밋 `3547e43`(#386 `wishlist_view` intent
신설)로 세대 2(`e62fd0f6e03d`)가, 커밋 `55d93bd`(#430, PR #460)로 세대 3(`865ed6fd771e`,
**현행**)이 바뀌었다 — 이 family 를 `#430`(프롬프트 수정)의 after 판과 대조하면 안 된다.
`#430` before·`#431` 전환 판단의 정본은 `../fast-2026-08-08-run1~3`(세대 2 `e62fd0f6e03d`,
착수 당시 현행이었으나 #430 back-merge 로 지금은 아니다) 이다. 자세한 내용·세대 계보는
`../README.md`(기준선 색인)의 G-1 을 봐라.
