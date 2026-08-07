# union 실측 — fast 티어 (#432)

> ⚠️ **#430(프롬프트 수정)이 아직 dev 에 머지되지 않은 상태의 판이다.** fast(프로덕션 티어)는
> `missRate` 가 이미 100.0%에 가까워(`#433` 6판 실측: 99.1~100.0%) 판정 True 표본(전복될 수
> 있는 표본) 자체가 0~1건뿐이다 — 그래서 이 판의 `expansionSuppressionRate`(전개가 되물음을
> 얼마나 꺼뜨리는가)는 분모가 0 이라 사실상 **해당 없음**이다. **fast 티어의 판정 전복 축은
> `#430` 머지 후 재실행해야 값이 생긴다** — `#431` 이 읽어야 할 판은 이 판이 아니라 그 이후의
> 재실행 판이다.

**추기(2차 리뷰 G-3, `#430` back-merge 후)** — 이 판은 pre-#430(`e62fd0f6e03d`) 세대라
전복 축이 `해당 없음` 이었다. post-#430(`865ed6fd771e`) 실측은
`../union-fast-2026-08-08-post430-run1/` 참조 — 그 판에서 `expansionSuppressionRate` 가
처음으로 실제 분모(106)를 갖는다. 이 판의 원본 수치·표는 개변하지 않았다.

> ⚠️ **단일 실행은 채택 판정이 아니다.** 독립 2~3회 분포로 판정한다.

```bash
uv run python -m evals.underspecified_probe \
  --out evals/underspecified_probe/baselines/union-fast-2026-08-08-run1 \
  --tier fast --union --budget-usd 2.0
```

`--budget-usd 2.0` 을 고른 근거: fast 티어 decompose 단독 기준선(#433 3판)의 관측 비용이
$0.053~0.054(240콜)이었다 — union 단계가 추가하는 콜(매핑 임베딩은 예산에 안 잡히고, LLM 콜은
조건부 택일·조건부 전개뿐)이 fast 모델 단가 기준으로는 크지 않을 것으로 보고 기본값(5.0)보다
낮춰 상한을 걸었다. 실측 비용은 $0.0500(부분합)로 사실상 decompose 단독 판과 같은 수준이었다
(fast 티어는 카테고리 신호를 잘 못 내 매핑·전개가 자주 "신호 없음"으로 조기 종료된다는 뜻).

## 헤더

`prompt=e62fd0f6e03d (repo:_SYSTEM)` · `tier=fast` · `fixture=underspec-anchors-v1` · `N=8` ·
`underspecifiedReaskEnabled=true`(판정 고정값) · `union=true`. 종료 코드 0 · 못 채운 셀 0 ·
`unionStageErrorCount=0`(240 표본 전부 union 단계 성공).

**비용·토큰은 부분합이다.** 관측 비용 **$0.0500**(115콜은 provider 가 usage 를 보고하지 않아
집계에서 빠졌다).

## decompose 직후 vs union(전개 후)

| 축 | decompose 직후 | union(전개 후) | 성격 |
|---|---|---|---|
| 미탐율 | `missRate` 112/112 (100.0%) CI95 [96.7%, 100.0%] | `missRateAfterExpansion` 112/112 (100.0%) CI95 [96.7%, 100.0%] | exploratory |
| 오탐율 | `falseAlarmRate` 0/104 (0.0%) CI95 [0.0%, 3.6%] | `falseAlarmRateAfterExpansion` 0/104 (0.0%) CI95 [0.0%, 3.6%] | exploratory |
| 전개 게이트 발동률 | `expansionGateWouldFireRate`(가정) 0/0 — 해당 없음 | `expansionGateFiredRate`(실측) 78/240 (32.5%) CI95 [26.9%, 38.7%] | exploratory |

`expansionSuppressionRate`(decompose 판정 True → union 판정 False) = **0/0 — 해당 없음**
(분모가 되는 decompose 판정 True 표본이 이 판에 0건이다).

**해석**: `missRateAfterExpansion` 이 `missRate` 와 완전히 같다(112/112) — union 단계가 전복시킬
"판정 True" 표본 자체가 없기 때문이다(위 경고 참조). 그러나 `expansionGateFiredRate`(실측
32.5%, 분모 240)는 **가정판(0/0)이 구조적으로 못 재던 것을 처음으로 실측한다** — fast 티어도
전개 게이트 자체는 표본의 약 1/3에서 실제로 발동한다(`unresolved` 가 실제로 채워진다). 다만
그 발동이 최종 판정을 뒤집을 기회(판정이 이미 True 인 표본)를 아직 못 만나고 있을 뿐이다 —
`#430` 이 `missRate` 를 낮추면 이 게이트가 판정을 뒤집을 기회도 함께 늘어날 것으로 예상된다.

## 시드·튜너블 재현성

`run_manifest.json.underspecifiedProbe.union` 참조 — smart 판과 같은 pg-catalog 시드
(`categoriesWithEmbedding`=1007 · `productDocumentCount`=6559), 같은 임베딩 모델
(`gemini-embedding-001`), 튜너블 9종 전부 `Settings` 기본값과 일치
(`tunablesDifferFromDefault`=`[]`).

**⚠️ F-2 캐비엇(리뷰 findings-432-r1)**: 이 판이 기록한 `run_manifest.json` 도 위 9종만 담고
있다 — `category_select_margin_max`·`embedding_task_query`·`needs_expansion_tier`·
`needs_expansion_min_items`(4종)는 이 판 당시 `UNION_TUNABLE_FIELDS` 에 없어 manifest 에
기록되지 않았다(이 리뷰 반영 커밋에서 추가됨, 산출물은 개변하지 않는다). 같은 로컬 `.env`로
**지금** 관측한 실제 값은 전부 `Settings` 기본값과 일치한다(smart 판 README §시드·튜너블
재현성의 캐비엇과 동일한 값).

## 부작용

`_prepare_recommendation` 끝부분이 `get_revert_store()` 를 불러 로컬 pg-profile 에 되돌리기
키가 쓰일 수 있다(판정에는 영향 없음).

## 재현 명령

위 §헤더 코드 블록 참조.
