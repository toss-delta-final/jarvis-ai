# union 실측 — smart 티어 (#432)

> ⚠️ **smart 는 프로덕션 티어가 아니다.** `run_buyer_turn` 이 실제로 쓰는 decompose 티어는
> `fast` 다 — 이 판의 숫자를 `#431`(플래그 기본 on 전환) 판단 근거로 **직접 인용하지 마라**.
> 이 판의 목적은 "전개가 되물음을 얼마나 꺼뜨리는가"라는 #432 의 핵심 질문에 대한 **오늘
> 가능한 첫 실측**을 남기는 것이다 — fast 티어는 `missRate` 가 이미 100%에 가까워 판정 True
> 표본(전복될 수 있는 표본) 자체가 0~1건이라 이 질문에 답할 수 없다(`union-fast-…/README.md`
> 참조).

> ⚠️ **단일 실행은 채택 판정이 아니다.** 독립 2~3회 분포로 판정한다(`evals/intent_probe` 의
> 재현 함정, `#433` 이 굳힌 규약). 이 판은 union 모드의 **첫** 실측이다.

```bash
uv run python -m evals.underspecified_probe \
  --out evals/underspecified_probe/baselines/union-smart-2026-08-08-run1 \
  --tier smart --union --budget-usd 6.0
```

`--budget-usd 6.0` 을 고른 근거: `--dry-run --union` 은 pg 접근이 0 이라 union 단계 콜 수를
직접 관측할 수 없다(그 자체가 이 모드의 한계 — README §union 측정 모드 참조). smart 티어
decompose 단독 기준선(`smart-2026-08-08-run1`, #433)의 관측 비용이 $0.2165(240콜)이었고,
union 단계가 표본마다 매핑 임베딩 + 조건부 택일(최대 `category_select_max_calls`=2) + 조건부
전개 LLM 을 추가하므로 최악의 경우를 넉넉히 덮도록 기본값(5.0)보다 약간 높였다. 실측 비용은
$0.1842(부분합)로 여유가 있었다.

## 헤더

`prompt=e62fd0f6e03d (repo:_SYSTEM)` · `tier=smart` · `fixture=underspec-anchors-v1` · `N=8` ·
`underspecifiedReaskEnabled=true`(판정 고정값) · `union=true`. 종료 코드 0 · 못 채운 셀 0 ·
`unionStageErrorCount=0`(240 표본 전부 union 단계 성공 — pg-catalog·임베딩 실패 0건).

**비용·토큰은 부분합이다.** 관측 비용 **$0.1842**(105콜은 provider 가 usage 를 보고하지 않아
집계에서 빠졌다) — 이 숫자를 총 비용으로 인용하지 마라(실제 총액은 이보다 크다). **union 이
추가로 부른 임베딩 호출은 이 부분합에 안 잡힌다**(`run_manifest.json` 참조, README §union
측정 모드 항목 참조).

## decompose 직후 vs union(전개 후) — #432 체크리스트 3항

| 축 | decompose 직후 | union(전개 후) | 성격 |
|---|---|---|---|
| 미탐율 | `missRate` 56/104 (53.8%) CI95 [44.3%, 63.1%] | `missRateAfterExpansion` 59/104 (56.7%) CI95 [47.1%, 65.8%] | exploratory |
| 오탐율 | `falseAlarmRate` 0/104 (0.0%) CI95 [0.0%, 3.6%] | `falseAlarmRateAfterExpansion` 0/104 (0.0%) CI95 [0.0%, 3.6%] | exploratory |
| 전개 게이트 발동률 | `expansionGateWouldFireRate`(가정) 3/48 (6.2%) CI95 [2.1%, 16.8%] | `expansionGateFiredRate`(실측) 47/232 (20.3%) CI95 [15.6%, 25.9%] | exploratory |

`expansionSuppressionRate`(decompose 판정 True → union 판정 False) = **3/48 (6.2%)**
CI95 [2.1%, 16.8%].

**해석**: 이 데이터셋·이 시드에서는 `missRateAfterExpansion`(56.7%)이 `missRate`(53.8%)보다
**오히려 높다** — 직관과 반대로 보이지만 구조적으로 당연하다. union 단계는 decompose 산출에
카테고리 leg 만 **추가**할 뿐 기존 신호를 지우지 않으므로(`is_underspecified_turn` 의 AND
조건), union 판정은 decompose 판정을 **True→False 로만** 뒤집을 수 있고 반대 방향(False→True)
으로는 절대 뒤집지 않는다. 즉 `expectedReask=true` 앵커 부분집합에서 `missRateAfterExpansion
>= missRate` 는 이 하네스의 구조적 불변식이다 — 전개가 "정답대로 되물어야 했던" 표본 중
3건(48건의 decompose-True 표본 중 6.2%)에서 매핑이 카테고리를 채워 되물음을 **억제**했고,
그 억제가 `expectedReask=true` 라벨과는 어긋나 union 관점의 미탐 3건을 새로 만들었다(56→59).

`expansionGateFiredRate`(20.3%, 분모 232)가 `expansionGateWouldFireRate`(6.2%, 분모 48)보다
큰 것은 **분모가 다르기 때문**이다(전자는 판정 True 인 recommend 표본만 보고, 후자는 union
성공한 recommend 표본 전부를 본다) — 두 수치를 직접 비율로 대조하지 마라. 그래도 두 축이
보여주는 것은 분명하다: **실 `unresolved` 기반 게이트는 가정(`unresolved=[]`)보다 훨씬 자주
발동한다** — 좁은 분모(가정판)는 D2(`mapping_failed`) 규칙이 구조적으로 발동할 수 없어 전개
게이트의 실제 노출 규모를 과소평가하고 있었다.

**⚠️ 왜 `expansionSuppressionRate`(3/48)와 `expansionGateWouldFireRate`(3/48)가 똑같은
숫자인가(F-5) — 우연이 아니라 구조다, 중복 계산 버그가 아니다.** 억제(decompose True → union
False)는 게이트 발동의 **부분집합**이다 — 게이트가 안 걸리면 전개 LLM 이 안 돌아 새 leg 이
생기지 않고, leg 이 없으면 union 판정을 뒤집을 수 없다. 이 판에서는 게이트가 발동한 3건
(`expansionGateWouldFireRate`) **전부**가 재매핑에 성공해 판정을 뒤집었으므로(억제 3건,
`samples.csv` 로 확인: 셋 다 `under-nc-0003`, `unionExpansionReason=no_legs`,
`unionMappedLegCount` 1~2) 두 수치가 우연히 같은 값(3/48)으로 등호가 됐다 — 게이트가 발동했지만
재매핑에 실패한 표본이 있었다면 억제 건수가 게이트 발동 건수보다 **작았을 것**이다(억제 ≤
게이트 발동은 이 하네스의 구조적 부등식이다, `test_expansion_suppression_rate_numerator_is_subset_of_gate_would_fire_numerator`
가 임의 표본 집합에서 이를 고정한다).

## 시드·튜너블 재현성

`run_manifest.json.underspecifiedProbe.union` 참조:
- `preflight.categoriesWithEmbedding` = 1007 · `preflight.productDocumentCount` = 6559
- `embeddingModelId` = `gemini-embedding-001`
- 튜너블 9종 전부 `Settings` 기본값과 **일치**(`tunablesDifferFromDefault` = `[]`) — 로컬
  `.env` 가 측정 대상을 바꾸지 않았다.

**⚠️ F-2 캐비엇(리뷰 findings-432-r1)**: 이 판이 기록한 `run_manifest.json` 은 위 9종만 담고
있다 — `category_select_margin_max`·`embedding_task_query`·`needs_expansion_tier`·
`needs_expansion_min_items`(4종)는 이 판 **당시**의 코드가 `UNION_TUNABLE_FIELDS` 에 넣지
않아 manifest 에 기록되지 않았다(이 리뷰 반영 커밋에서 추가됨, 산출물은 개변하지 않는다). 이
런들과 같은 로컬 `.env`로 **지금** 관측한 실제 값은 전부 `Settings` 기본값과 일치한다
(`category_select_margin_max=0.02`·`embedding_task_query=RETRIEVAL_QUERY`·
`needs_expansion_tier=fast`·`needs_expansion_min_items=2`, 전부 기본값) — 이 판이 그 4종에서
드리프트를 겪었을 가능성은 낮지만, manifest 자체의 보증은 아니다.

## 부작용

`_prepare_recommendation` 끝부분이 `get_revert_store()` 를 불러 로컬 pg-profile 에 되돌리기
키가 쓰일 수 있다(판정에는 영향 없음).

## 재현 명령

위 §헤더 코드 블록 참조. 상세·`unionMappedLegCount`·`unionExpansionReason` 등 원인 분해는
`samples.csv` 에서 재집계할 수 있다(런 재실행 없이).
