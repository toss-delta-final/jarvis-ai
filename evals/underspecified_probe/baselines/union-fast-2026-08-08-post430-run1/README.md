# union 실측 — fast 티어, post-#430 프롬프트 (#432, 2차 리뷰 G-3)

> **이 판이 프로덕션 티어(fast) × post-#430 프롬프트(`865ed6fd771e`)의 첫 union 실측이다.**
> `#432` 는 원래 "`#430` 이 먼저다 — `missRate` 가 100%인 동안에는 이 측정이 잴 표본 자체가
> 없다"고 적었고, 그래서 pre-#430 판(`../union-fast-2026-08-08-run1/`)은
> `expansionSuppressionRate` 가 `0/0`(해당 없음)이었다. `#430` back-merge 로 그 선행 조건이
> 충족돼 이 판에서 처음으로 전복 축이 실제 분모를 갖는다.

> ⚠️ **단일 실행은 채택 판정이 아니다.** 독립 2~3회 분포로 판정한다(`evals/intent_probe` 의
> 재현 함정, `#433` 이 굳힌 규약). 이 판은 post-#430 union 모드의 **첫** 실측이다.

> **이 문서는 union 축만 말한다 — decompose 단계의 `missRate`(8/112, 7.1%)는 `#430` 의
> after 판정에 해당하는 값이지만 그 판정은 이 문서의 소관이 아니다.** `#430`/`#431` 레인의
> 정본은 `../fast-2026-08-07-430-after-1/README.md` 다.

```bash
uv run python -m evals.underspecified_probe \
  --out evals/underspecified_probe/baselines/union-fast-2026-08-08-post430-run1 \
  --tier fast --union --budget-usd 2.0
```

`--budget-usd 2.0` 을 고른 근거: 직전 fast union 판(pre-#430, `union-fast-2026-08-08-run1`)의
관측 비용이 $0.0500(부분합)이었다 — union 단계의 콜 구성(매핑 임베딩 + 조건부 택일 + 조건부
전개)은 프롬프트 세대와 무관하게 같은 산식(§union 측정 모드 참조)을 따르므로 그 판과 같은
상한을 그대로 썼다. 실측 비용은 $0.0536(부분합)로 직전 판과 같은 수준이었다.

## 헤더

`prompt=865ed6fd771e (repo:_SYSTEM)` · `tier=fast` · `fixture=underspec-anchors-v1` · `N=8` ·
`underspecifiedReaskEnabled=true`(판정 고정값) · `union=true`. 종료 코드 0 · 못 채운 셀 0 ·
`unionStageErrorCount=0`(240 표본 전부 union 단계 성공 — 콘솔에 `category_embed_failed`
경고 2회가 찍혔지만 `map_categories` 의 leg 단위 격리(canonical-or-null)로 흡수돼 union 단계
자체는 실패하지 않았다, `unionStageErrorCount` 가 그 사실을 그대로 보인다).

**비용·토큰은 부분합이다.** 관측 비용 **$0.0536**(122콜은 provider 가 usage 를 보고하지 않아
집계에서 빠졌다) — 이 숫자를 총 비용으로 인용하지 마라. **union 이 추가로 부른 임베딩 호출은
이 부분합에 안 잡힌다.**

## decompose 직후 vs union(전개 후)

| 축 | decompose 직후 | union(전개 후) |
|---|---|---|
| 미탐율 | `missRate` 8/112 (7.1%) CI95 [3.7%, 13.5%] | `missRateAfterExpansion` 62/112 (55.4%) CI95 [46.1%, 64.2%] |
| 오탐율 | `falseAlarmRate` 2/104 (1.9%) CI95 [0.5%, 6.7%] | `falseAlarmRateAfterExpansion` 1/104 (1.0%) CI95 [0.2%, 5.2%] |
| 전개 게이트 발동률(분모가 서로 다르다) | `expansionGateWouldFireRate` 56/106 (52.8%) CI95 [43.4%, 62.1%] | `expansionGateFiredRate` 91/240 (37.9%) CI95 [32.0%, 44.2%] |

`expansionSuppressionRate`(decompose 판정 True → union 판정 False) = **55/106 (51.9%)**
CI95 [42.5%, 61.2%].

`judgmentAccuracy`(decompose 단계) = 206/216 (95.4%) CI95 [91.7%, 97.5%].

## 해석 — union 축만(decompose 단계 판정은 #430/#431 레인 소관)

**전개가 되물음을 절반 이상 꺼뜨린다.** decompose 가 "되물어야 한다"고 정확히 판정한 표본
106건(전체 recommend 표본 중 decompose 판정 True) 중 **55건(51.9%)**이 union 단계(카테고리
매핑·`needs_expansion` 전개)에서 카테고리를 실제로 채워 최종 판정이 False 로 뒤집혔다 —
이 하네스가 앞서 굳힌 구조적 불변식대로(union 은 판정을 True→False 로만 뒤집을 수 있다)
`missRateAfterExpansion`(55.4%)이 `missRate`(7.1%)보다 훨씬 높아졌다. 이 갭이 `#432` 가
원래 재려던 것이다 — decompose 만 보면 `#430` 이후 되물음 판정이 크게 개선된 것처럼 보이지만
(`missRate` 100%→7.1%), 프로덕션 파이프라인 전체(전개 포함)로 보면 그 개선의 **절반 이상이
전개 단계에서 다시 지워진다.**

`expansionGateFiredRate`(37.9%, 분모 240)와 `expansionGateWouldFireRate`(52.8%, 분모 106)는
**분모가 다른 별개 질문**이다(가정판: 판정 True 표본 중 몇 %가 게이트에 걸리나 / 실측판: 전체
recommend 표본 중 몇 %에서 게이트가 실제로 발동하나) — 두 수치를 직접 비율로 대조하지 마라
(근거는 `../README.md` §union 측정 모드, F-5/G-1 참조).

`falseAlarmRateAfterExpansion`(1.0%)이 `falseAlarmRate`(1.9%)보다 낮은 것도 같은 구조적
이유다 — union 은 오탐(판정 True 인데 라벨은 False)도 True→False 로만 뒤집을 수 있어 오탐이
줄어들 수는 있어도 늘 수는 없다.

미탐 원인 축 분해(decompose 단계, 8건): `categoryQueries;semanticQueryIsFallback` 4건 ·
`filters.attrConditions` 3건 · `semanticQueryIsFallback`(단독) 1건 — `#430`/`#431` 레인이
해석할 몫이다.

## 시드·튜너블 재현성

`run_manifest.json.underspecifiedProbe.union` 참조:
- `preflight.categoriesWithEmbedding` = 1007 · `preflight.productDocumentCount` = 6559
  (앞선 두 union 판과 동일 시드)
- `embeddingModelId` = `gemini-embedding-001`
- 튜너블 14종(F-2 반영 후 전체 목록) 전부 `Settings` 기본값과 **일치**
  (`tunablesDifferFromDefault` = `[]`) — 로컬 `.env` 가 측정 대상을 조용히 바꾸지 않았다.
- `budgetCallFormula.maxCalls` = 1440(`expectedCalls`=240 · `attemptMultiplier`=3 ·
  `unionExtraCallsPerSample`=3) — F-3 산식대로.

## 부작용

`_prepare_recommendation` 끝부분이 `get_revert_store()` 를 불러 로컬 pg-profile 에 되돌리기
키가 쓰일 수 있다(판정에는 영향 없음).

## 재현 명령

위 §헤더 코드 블록 참조. 상세·`unionMappedLegCount`·`unionExpansionReason` 등 원인 분해는
`samples.csv` 에서 재집계할 수 있다(런 재실행 없이).
