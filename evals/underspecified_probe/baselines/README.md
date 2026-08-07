# 기준선 색인 (#433)

`evals/underspecified_probe/baselines/` 아래 여러 판이 쌓인다. **인용 대상은 하나여야 한다**
(#433 체크리스트 4항) — 이 문서가 그 하나를 가리킨다.

## 정본 선언

> ⚠️ **G-1(2차 리뷰) — "현행"이라는 말은 해시 없이 쓰지 않는다.** 이 문서는 두 번 낡았다
> (#386 머지 때 한 번, #430 back-merge 때 또 한 번) — "현행 dev 프롬프트"라고만 적으면 다음
> 머지에서 또 거짓이 된다. 아래부터는 프롬프트를 가리킬 때 항상 해시를 함께 적는다.

| family | 디렉터리 | prompt sha12 | 정본 여부 |
|---|---|---|---|
| **A — pre-#430 프롬프트 세대** | `fast-2026-08-08-run1`·`run2`·`run3` | `e62fd0f6e03d` | ✅ **`#430` 의 before 기준선(n=3, 99.1~100.0%) · `#431` 전환 판단의 정본** |
| B — pre-#386 프롬프트(역사 기록) | `fast-2026-08-06`(run1, 커밋된 기준선)·`run2`·`run3` | `11c6fe3bfa0c` | ❌ 역사 기록. **`#430` after 와 대조하지 마라** |
| C — 티어 대조(pre-#430 프롬프트, smart) | `smart-2026-08-08-run1` | `e62fd0f6e03d` | 참고(원인 축 분해의 티어 대조 전용, 채택 판정 대상 아님) |
| D — union(전개 후 판정, #432) | `union-smart-2026-08-08-run1`(진단 티어, pre-#430)·`union-fast-2026-08-08-run1`(프로덕션 티어, pre-#430)·`union-fast-2026-08-08-post430-run1`(프로덕션 티어, **post-#430**, G-3) | `e62fd0f6e03d`(첫 2판)·`865ed6fd771e`(post430 판) | 참고 전용. smart 판은 **프로덕션 동작이 아니다**(#431 근거로 직접 인용 금지) — 상세는 각 디렉터리 README |
| E — **#430 채택 판정(다른 레인 소유, G-2)** | `fast-2026-08-07-430-{before-1,before-2,merged-1,merged-2,merged-3,after-1,after-2}`(7개, `ls` 로 직접 확인) | `11c6fe3bfa0c`(before)·`f99a98867e4a`(merged, ⚠️G-4)·`865ed6fd771e`(after) | 이 색인이 인용하지 않는다 — **`fast-2026-08-07-430-after-1/README.md` 가 그 레인의 정본**(#430/#431 소관). 이 문서는 존재만 가리킨다 |

(리뷰 findings-432-r2 는 이 family 를 10개로 셌다 — `evals/intent_probe/baselines/` 아래의
`fast-2026-08-07-430-after-3`·`-v6-adopted-{1,2}`(intent_probe 소관, **다른 하네스**)까지
합산한 수로 보인다. `evals/underspecified_probe/baselines/` 안에는 `ls` 로 확인한 7개뿐이다.)

> ⚠️ **G-4(2차 리뷰, 관측 사실만 — 판단은 #430 레인 소관)**: `fast-2026-08-07-430-merged-1~3`
> 의 `run_manifest.json.hashes.systemPrompt` 는 `f99a98867e4a` 인데, 이 브랜치에 머지된
> `dev` 의 프롬프트는 `865ed6fd771e` 다(직접 대조: 위 세 디렉터리 전부 `f99a98867e4a`, 현재
> HEAD dry-run 은 `865ed6fd771e`). 즉 `-merged-*` 세 판은 최종 머지본과 **다른 프롬프트
> 문면**에서 돌았을 가능성이 있다 — 인용 전에 #430 레인에 확인할 것. 이 해시 불일치를
> 우리가 판정하거나 그 산출물을 고치지 않는다(다른 레인의 소관).

**G-1 — 프롬프트 세대는 이제 셋이다**(back-merge 로 `#430` 이 들어왔다):

| 세대 | sha12 | 도입 커밋 | 이 색인에서의 family |
|---|---|---|---|
| 1 | `11c6fe3bfa0c` | #380 최초 커밋 | B (`fast-2026-08-06*`) |
| 2 | `e62fd0f6e03d` | #386(`3547e43`, `wishlist_view`) | A·C·D(smart·fast pre-#430) — 착수 당시 "현행"이었으나 지금은 **pre-#430** |
| 3 | `865ed6fd771e` | #430(`55d93bd`, PR #460) | **현재 dev/이 브랜치 HEAD** — D(`union-fast-…-post430-run1`, G-3)만 이 세대 |

`865ed6fd771e` 가 지금 이 브랜치의 실제 "현행"이다(`uv run python -m evals.underspecified_probe
--out <tmp> --dry-run` 로 직접 재확인 가능) — A family(세대 2)는 **더 이상 현행이 아니지만**,
`#430` 의 before 로서의 가치는 그대로다(그게 원래 목적이었다).

**정본은 A family 3판의 분포다 — 단일 판이 아니다.** 대표값 하나를 인용해야 하면
`missRate` **중앙값 판**을 써라 — {run1 99.1%, run2 99.1%, run3 100.0%}의 중앙값은 99.1%이고,
run1·run2 둘 다 중앙값이지만 run1 은 `nonRecommendIntentCount={"under-nc-0002": {"general":
1}}`(비-recommend 표본 1건 존재)인 반면 **run2 는 `nonRecommendIntentCount={}`**(전 표본
recommend 라우팅)이므로 `fast-2026-08-08-run2`(111/112, 99.1%)를 대표 판으로 쓴다. 분산이
있다는 사실(아래 §런 간 편차)을 함께 인용해라 — `nonRecommendIntentCount={}` 는 run2·run3
둘 다 만족해 run2 와 run3 를 가르지 못하므로, 그 기준만으로 천장값(run3, 100.0%)을 대표로
고르면 안 된다(그러면 이 값을 before 로 쓰는 #430 의 개선폭이 실제보다 커 보인다).

## 프롬프트 세대 분기

#433 착수 전 실측(sub-orchestrator)에서 이슈가 가정한 전제("세 판이 같은 `hashes.systemPrompt`
를 공유한다")가 깨져 있었다:

| 대조 | 커밋된 기준선(`fast-2026-08-06`) manifest | 현재 HEAD | 판정 |
|---|---|---|---|
| `hashes.anchorFixture` | `cdadb1ca…384b8c` | 동일 | ✅ |
| `hashes.underspecifiedProbeModules.metrics.py` | `ce1784fe…` | `f4ef7a8f…` | ⚠️ 다르지만 **주석 전용** |
| 나머지 9개 하네스 모듈 해시 | — | 전부 동일 | ✅ |
| `hashes.systemPrompt` | `11c6fe3bfa0c…` | `e62fd0f6e03d…` | ❌ **다르다** |

- **`metrics.py` 차이의 정체**: 커밋 `bb65ee3`("docs(eval): #380 하네스의 graph.py 인용을 줄
  번호에서 심볼로")가 `_recommend_pairs` 의 **docstring 만** 고쳤다
  (`git diff b1413dc bb65ee3 -- evals/underspecified_probe/metrics.py` 로 확인 — 코드 라인
  변경 0). 계측 동작은 동일하다 — #328 규약 8("계측기가 바뀌면 전 baseline 재실행")의 트리거가
  아니다.
- **`systemPrompt` 차이의 정체**: 커밋 `3547e43`(#386 `wishlist_view` intent 신설)이
  `app/agents/buyer/recommendation/decompose.py::_SYSTEM` 을 바꿨다 → **커밋된 기준선은
  pre-#386 프롬프트 세대다.**
- **옛 프롬프트는 재현 가능하다** — CLI 의 `--prompt-rev <git rev>` 로
  `798f0a965385bfdedbe20646c3e8a07ba73ea08b` 를 지정하면 산출물 `prompt.sha12` 가 정확히
  `11c6fe3bfa0c` 로 재현된다(아래 6판 전부 실측 확인, run_manifest.json 대조).
- **⚠️ 이 문서 자신도 같은 함정을 남긴다** — 6판이 기록한
  `hashes.underspecifiedProbeModules.metrics.py` 는 `f4ef7a8f82500075…` 인데, 이 커밋(#433)의
  `metrics.py` 는 `d8f48042ae964d0c…` 로 **다르다**. 원인은 6판을 전부 돌린 **뒤에**
  `_recommend_pairs` docstring 에 `wishlist_view`(#386) 한 단어를 추가했기 때문이다
  (`git diff 613ab50..c04dd05 -- evals/underspecified_probe/metrics.py` 로 확인 — 코드 라인
  변경 0). **계측 동작은 불변이므로 #328 규약 8 의 재실행 트리거가 아니다** — 다음 사람이
  오늘 커밋된 `metrics.py` 와 6판 manifest 를 해시로만 대조하면 "계측기가 바뀌었다"는 오경보를
  맞을 수 있으니, 재실행 전에 반드시 위 `git diff` 로 코드 라인 변경 여부부터 확인해라.

두 프롬프트 세대를 각각 굳혔다 — 다운스트림(#430 after 대조, #431 전환 판단)이 쓰는 before 는
당시(#433 착수 시점) 현행이던 `e62fd0f6e03d`(세대 2)여야 하지만, #433 이 문자 그대로 요구한
"커밋된 기준선(옛 프롬프트, 세대 1)의 n=3"도 채웠다. **`e62fd0f6e03d` 는 이후 `#430`
back-merge 로 더 이상 현행이 아니게 됐다**(현행은 세대 3 `865ed6fd771e`) — G-1 참조.

## 실행 6판

```bash
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run1 --tier fast
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run2 --tier fast
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-08-run3 --tier fast

uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06-run2 --tier fast --prompt-rev 798f0a965385bfdedbe20646c3e8a07ba73ea08b
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06-run3 --tier fast --prompt-rev 798f0a965385bfdedbe20646c3e8a07ba73ea08b

uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/smart-2026-08-08-run1 --tier smart
```

6판 전부 종료 코드 0 · `unfilledCells` 빈 배열 · `hashes.anchorFixture` ==
`cdadb1cac1d88c7d6fa2904ca2f5c29065ad4c6663e47d46cd7b8be2ea384b8c` · `prompt.sha12` 이
family 대로(A·C=`e62fd0f6e03d`, B=`11c6fe3bfa0c`) · 6판의 `hashes.underspecifiedProbeModules`
전부 동일(같은 계측기, sha256 축약 `8aa8fcb49940252d`) — 게이트 6항목 전부 통과, 폐기한 판 없음.

## 런 간 편차 — A family (`fast-2026-08-08-run1~3`, prompt `e62fd0f6e03d`, tier `fast`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| run1 | 110/111, 99.1%, [95.1%, 99.8%] | 0/104, 0.0%, [0.0%, 3.6%] | 105/215, 48.8%, [42.2%, 55.5%] | 0 | $0.0536(`unknownCostCallCount`=39) |
| run2 | 111/112, 99.1%, [95.1%, 99.8%] | 0/104, 0.0%, [0.0%, 3.6%] | 105/216, 48.6%, [42.0%, 55.2%] | 0 | $0.0533(`unknownCostCallCount`=40) |
| run3 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0544(`unknownCostCallCount`=36) |

run1·run3 의 `missRate` 분모가 다른 것(111 vs 112)은 intent 라우팅의 확률적 산출 때문이다 —
run1 은 `under-nc-0002` 앵커 표본 1건이 `general` 로 라우팅돼(`nonRecommendIntentCount=
{"under-nc-0002": {"general": 1}}`) F-1 필터에서 confirmatory 분모 밖으로 빠졌다. run2·run3 은
`nonRecommendIntentCount={}`(전 표본 recommend 라우팅). 이것이 바로 이슈 본문이 지목한 "정의가
같아도 판마다 흔들릴 수 있는" 확률적 요소다 — 계측기 정의는 세 판에서 동일하다(§실행 6판의
모듈 해시 대조).

`falseAlarmRate` 는 세 판 모두 0/104 로 완전히 일치.

## 런 간 편차 — B family (`fast-2026-08-06` + `run2`/`run3`, prompt `11c6fe3bfa0c`, tier `fast`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| run1(커밋된 기준선) | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0510(`unknownCostCallCount`=38) |
| run2 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0521(`unknownCostCallCount`=34) |
| run3 | 112/112, 100.0%, [96.7%, 100.0%] | 0/104, 0.0%, [0.0%, 3.6%] | 104/216, 48.1%, [41.6%, 54.8%] | 0 | $0.0541(`unknownCostCallCount`=26) |

세 판 모두 `missRate`·`falseAlarmRate`·`judgmentAccuracy` 가 소수점까지 완전히 일치한다
(`nonRecommendIntentCount={}` 도 세 판 동일) — pre-#386 프롬프트 세대는 이 데이터셋에서
관측 가능한 편차가 없었다.

## 티어 대조 — C (`smart-2026-08-08-run1`, prompt `e62fd0f6e03d`, tier `smart`)

| 런 | `missRate` (분자/분모, 비율, CI95) | `falseAlarmRate` | `judgmentAccuracy` | 종료코드 | 관측 비용(부분합) |
|---|---|---|---|---|---|
| smart-run1 | 56/104, 53.8%, [44.3%, 63.1%] | 0/104, 0.0%, [0.0%, 3.6%] | 152/208, 73.1%, [66.7%, 78.6%] | 0 | $0.2165(`unknownCostCallCount`=37) |

`missRate` 분모가 fast(111~112) 대신 104 인 것은 `under-cbs-0003` 앵커 표본 8건이
`cart_add` 로 라우팅돼(`nonRecommendIntentCount={"under-cbs-0003": {"cart_add": 8}}`) F-1
필터에서 제외됐기 때문이다.

## `blockingAxes` 조합 분포 표

### A family (fast, 신 프롬프트)

| 조합 | run1 | run2 | run3 |
|---|---|---|---|
| `semanticQueryIsFallback`(단독) | 97 | 100 | 102 |
| `categoryQueries;semanticQueryIsFallback` | 9 | 7 | 4 |
| `filters.attrConditions;semanticQueryIsFallback` | 4 | 4 | 6 |

### B family (fast, 구 프롬프트)

| 조합 | run1(커밋) | run2 | run3 |
|---|---|---|---|
| `semanticQueryIsFallback`(단독) | 92 | 94 | 94 |
| `categoryQueries;semanticQueryIsFallback` | 9 | 7 | 7 |
| `filters.attrConditions;semanticQueryIsFallback` | 11 | 10 | 9 |
| `filters.keyword;semanticQueryIsFallback` | 0 | 1 | 2 |

### C (smart, 신 프롬프트)

| 조합 | smart-run1 |
|---|---|
| `semanticQueryIsFallback`(단독) | 52 |
| `categoryQueries;semanticQueryIsFallback` | 4 |

조합이 판마다(그리고 프롬프트 세대 사이에) 흔들린다는 것을 위 표가 그대로 보인다 —
`filters.attrConditions` 조합은 A family 에서 4~6건, B family 에서 9~11건으로, 프롬프트
세대에 따라 크기가 뚜렷이 다르다(#380 README 의 F-2 규약대로 조합은 여기서 표로만 인용하고
산문으로 풀지 않는다).

## `semanticQueryIsFallback` 단독 비율의 티어·프롬프트 세대 대조

미탐 표본 중 원인이 `semanticQueryIsFallback` **단독**인 비율(다른 축과 조합이 아닌 경우):

| family/런 | 단독/전체 미탐 | 비율 |
|---|---|---|
| B run1(커밋된 기준선, 구 프롬프트) | 92/112 | 82.1% |
| B run2(구 프롬프트) | 94/112 | 83.9% |
| B run3(구 프롬프트) | 94/112 | 83.9% |
| A run1(신 프롬프트) | 97/110 | 88.2% |
| A run2(신 프롬프트) | 100/111 | 90.1% |
| A run3(신 프롬프트) | 102/112 | 91.1% |
| C smart-run1(신 프롬프트) | 52/56 | 92.9% |

이슈(#433 체크리스트 3항)가 지목한 **82.1%** 는 B family(커밋된 기준선, 구 프롬프트)의
값이다. **단독 비율은 티어보다 프롬프트 세대에 더 크게 반응한다** — 같은 fast 티어 안에서도
구 프롬프트(B, 82.1~83.9%)→신 프롬프트(A, 88.2~91.1%)로 6~9%p 뛰는 반면, 신 프롬프트
안에서는 티어를 fast(A, 88.2~91.1%)→smart(C, 92.9%)로 바꿔도 2~5%p 밖에 안 움직인다 —
즉 프롬프트 세대 축의 신호가 티어 축의 신호보다 크다.

그러나 **`missRate` 자체(미탐이 발생하는 빈도)는 티어에 따라 극적으로 다르다** — fast 는
99.1~100.0%(사실상 항상 미탐)인 반면 smart 는 53.8%(CI95 [44.3%, 63.1%])로 거의 절반 수준
낮다. 즉 smart 티어는 fast 보다 훨씬 자주 `semanticQuery` 를 올바르게 채우지만(미탐 자체가
적다), **일단 미탐이 나면 그 원인 축 분해는 fast 와 비슷한 모양**(대부분
`semanticQueryIsFallback` 단독)이라는 뜻이다. `judgmentAccuracy` 도 smart(73.1%)가
fast(48.1~48.8%)보다 높다 — smart 티어가 되물음 판정 자체의 품질을 끌어올린다.

## 편차 판정 (#433 체크리스트 5항)

A family(정본) 3판의 `missRate` 최대-최소 차 = 100.0% − 99.1% = **0.9%p** —
**10%p 미만**이므로 `--n` 상향·앵커 증설 검토는 이 PR 의 범위 밖으로 남긴다(#328 규약 4,
검토만 여기 기록하고 실행하지 않는다). `judgmentAccuracy` 편차도 0.7%p(48.8%→48.1%)로 작다.
B family 는 편차 0%p(세 판 완전 일치)로 A family 보다도 안정적이다. 현재 N=8(셀당) 로도
confirmatory 축의 결론(미탐율 fast ≈100%, smart ≈54%)은 판마다 뒤집히지 않는다.

## 폐기한 판

없음 — 6판(#433) + 3판(#432 union, G-3 의 post-#430 판 포함) 전부 §게이트 6항목을 첫
시도에 통과했다.

## D family — union(전개 후 판정) 실측 (#432, G-3 로 3판째 추가)

`--union` 으로 카테고리 매핑·`needs_expansion` 보정까지 실제로 태워 잰 3판(2판은 pre-#430,
1판은 **post-#430** — G-3). 상세 해석·시드 재현성은 각 디렉터리의 README 참조(경고 문구를
맨 위에 둔다 — smart 는 프로덕션 티어가 아니고, pre-#430 fast 판은 프롬프트 세대가 낡았다).

| 축 | union-smart-run1(pre-#430) | union-fast-run1(pre-#430) | union-fast-post430-run1(**post-#430**, G-3) |
|---|---|---|---|
| prompt sha12 | `e62fd0f6e03d` | `e62fd0f6e03d` | **`865ed6fd771e`** |
| `missRate` → `missRateAfterExpansion` | 56/104 (53.8%) → 59/104 (56.7%) | 112/112 (100.0%) → 112/112 (100.0%) | 8/112 (7.1%) → 62/112 (55.4%) |
| `falseAlarmRate` → `falseAlarmRateAfterExpansion` | 0/104 (0.0%) → 0/104 (0.0%) | 0/104 (0.0%) → 0/104 (0.0%) | 2/104 (1.9%) → 1/104 (1.0%) |
| `expansionSuppressionRate` | 3/48 (6.2%) | 0/0 — 해당 없음 | **55/106 (51.9%)** |
| `expansionGateWouldFireRate`(가정) → `expansionGateFiredRate`(실측) | 3/48 (6.2%) → 47/232 (20.3%) | 0/0 — 해당 없음 → 78/240 (32.5%) | 56/106 (52.8%) → 91/240 (37.9%) |
| `unionStageErrorCount` | 0 | 0 | 0 |
| 관측 비용(부분합) | $0.1842(`unknownCostCallCount`=105) | $0.0500(`unknownCostCallCount`=115) | $0.0536(`unknownCostCallCount`=122) |

세 판 다 `unionStageErrorCount=0`(240 표본 전부 union 단계 성공 — post430 판은 콘솔에
`category_embed_failed` 경고 2회가 찍혔지만 leg 단위 격리로 흡수돼 union 단계 자체는
실패하지 않았다) — pg-catalog 시드(`categoriesWithEmbedding`=1007·`productDocumentCount`
=6559)·임베딩 모델(`gemini-embedding-001`)이 세 판에서 동일하고, 튜너블(post430 판은
F-2 이후 14종, 앞 두 판은 9종만 기록)도 `Settings` 기본값과 전부 일치한다
(`tunablesDifferFromDefault`=`[]`, 각 `run_manifest.json` 참조).

**핵심 발견 1(pre-#430)**: `expansionGateFiredRate`(실측)가 `expansionGateWouldFireRate`
(가정)보다 뚜렷이 크다(smart 20.3% vs 6.2%, fast 32.5% vs 해당 없음) — 가정판
(`unresolved=[]`)은 D2(`mapping_failed`) 규칙이 구조적으로 발동할 수 없어 전개 게이트의
실제 노출을 과소평가하고 있었다.

**핵심 발견 2(post-#430, G-3 — `#431` 이 실제로 쓸 재료)**: `#430` 이후 decompose 단계
`missRate` 는 100%→7.1%로 크게 개선됐지만(그 판정 자체는 `#430`/`#431` 레인 소관), **union
(전개 후) 판정에서는 그 개선의 절반 이상이 다시 지워진다.** decompose 가 정확히 "되물어야
한다"고 판정한 106건 중 55건(51.9%)이 카테고리 매핑·전개 단계에서 카테고리를 실제로 채워
최종 판정이 False 로 뒤집힌다 — `missRateAfterExpansion`(55.4%)이 `missRate`(7.1%)보다
훨씬 높은 것이 그 증거다(union 은 판정을 True→False 로만 뒤집을 수 있다는 이 하네스의
구조적 불변식과 일치, 각 디렉터리 README §해석 참조). smart 판(pre-#430, 억제 6.2%)과
post-#430 fast 판(억제 51.9%)의 억제율 차이가 큰 것은 프롬프트 세대·티어·모델이 모두 달라
단일 요인으로 분해할 수 없다 — 이 판만으로 원인을 단정하지 않는다.

D family 는 **단일 실행**이다(각 1회) — 채택 판정 근거가 아니다. union 자체도 독립 2~3판이
필요하면 별도 후속으로 검토한다(이 PR 범위 밖).

## 재현 명령

§실행 6판 참조(#433). D family(union) 재현 명령은 각 디렉터리 README 참조.
