# after 런 1/2 — fast-2026-08-07-430 (#430 채택 판정)

`decompose._SYSTEM` 프롬프트 변경 **후**(sha12 `81e3770e1340`)의 첫 독립 런.
**이 디렉터리가 #430 의 채택 판정 근거 정본**이다 — before 2런(`../fast-2026-08-07-430-before-1`,
`-before-2`)과 after 2런(이 디렉터리, `-after-2`)의 대조표, 탈락 후보 표, `intent_probe`
타축 대조표가 아래에 있다.

```bash
uv run python -m evals.underspecified_probe --out <dir> --tier fast
```

(N=8 · 30셀 · 240콜 · 종료 코드 0 · 못 채운 셀 0 · 실패 0 · 관측 부분합 USD 0.0548)

## 무엇을 바꿨나 — 프롬프트 두 줄

`_SYSTEM` 의 `- recommend:` 규칙 절 **끝에** 한 규칙을 덧붙였다. 그게 전부다:

> 찾는 상품의 의미(종류·용도·상황·목적)가 발화에도 PRIOR_FILTERS·LAST_RECOMMENDATIONS·SCREEN
> 맥락에도 없으면 semanticQuery 는 **빈 문자열("")** 로 두세요 — 지어내거나 발화를 옮겨 적지 마세요.

판정 코드(`underspecified.is_underspecified_turn`)·`no_condition.py` 는 **한 줄도 바뀌지 않았다**
— 실측이 판정 코드는 정상이라고 말한다(2026-08-06 기준선: `what_axis`·`blocking_rating`
슬라이스 판정 100% 정확).

## 출고물 == 측정물

| | 값 |
|---|---|
| `prompt.source` | `repo:_SYSTEM` |
| `prompt.sha256` | `81e3770e13402b92df917c3869441f5bab3404afa5aa273b672c48d043ba3c38` |
| `prompt.charCount` | 7280 (before 7137, 순증 143자) |
| 모델 | `gpt-5-nano`(`fastReasoningEffort=minimal`) · provider `openai` |
| 판정 설정 | `underspecifiedReaskEnabled=true`(§D6 고정값 — `.env` 기본값 False 와 무관) |

두 after 런 모두 `source=repo:_SYSTEM` 이고 sha256 이 출고되는 `_SYSTEM` 과 같다.

## 채택 판정 (사전 등록 기준)

| # | 기준 | 결과 |
|---|---|---|
| 1 | `missRate` 가 after **모든 런**에서 before **모든 런**보다 낮다 | ✅ 9.8% · 2.7% vs 99.1% · 99.1% |
| 2 | `falseAlarmRate` 점추정 ≤ 3.6%(before CI95 상한) | ✅ 1.9% · 0.0% |
| 3 | 의미신호 소실 가드 ≤ 5% | ✅ 0/32 · 0/32 (아래 「가드축」) |
| 4 | `intent_probe` 전/후 타축 무회귀 | ⚠️ **`screenExactPick` −1 잔여** (아래 「타축」) |
| 5 | `flagOffInvariant`·`priorGateInvariant` = 0 | ✅ 0/240 (4런 전부) |
| 6 | 출고물 == 측정물 | ✅ 위 표 |

기준 4만 완전히 충족하지 못했다. **그 잔여를 아래에 이득과 같은 표로 싣는다 — 받아들일지는
머지하는 사람의 판단이다.**

## 전 축 대조 (before 2런 vs after 2런)

| 축 | before 1 | before 2 | **after 1** | **after 2** |
|---|---|---|---|---|
| `missRate` (confirmatory-primary) | 111/112 (99.1%) [95.1, 99.8] | 111/112 (99.1%) [95.1, 99.8] | **11/112 (9.8%)** [5.6, 16.7] | **3/112 (2.7%)** [0.9, 7.6] |
| `falseAlarmRate` (confirmatory-secondary) | 0/104 (0.0%) [0.0, 3.6] | 0/104 (0.0%) [0.0, 3.6] | 2/104 (1.9%) [0.5, 6.7] | 0/104 (0.0%) [0.0, 3.6] |
| `judgmentAccuracy` | 105/216 (48.6%) | 105/216 (48.6%) | 203/216 (94.0%) | 213/216 (98.6%) |
| `missRateWithNonRecommendIntent` | 111/112 (99.1%) | 111/112 (99.1%) | 11/112 (9.8%) | 3/112 (2.7%) |
| `falseAlarmRateWithGateSlice` | 0/128 (0.0%) | 0/128 (0.0%) | 2/128 (1.6%) | 0/128 (0.0%) |
| `falseAlarmRateWithNonRecommendIntent` | 0/104 (0.0%) | 0/104 (0.0%) | 2/104 (1.9%) | 0/104 (0.0%) |
| `judgmentAccuracyWithGateSlice` | 129/240 (53.8%) | 129/240 (53.8%) | 227/240 (94.6%) | 237/240 (98.8%) |
| `missRateUnderExpansionAssumption` (상한 가정) | 111/112 (99.1%) | 112/112 (100.0%) | 59/112 (52.7%) | 58/112 (51.8%) |
| `expansionGateWouldFireRate` (진단) | 0/1 | 1/1 | 48/103 (46.6%) | 55/109 (50.5%) |
| `flagOffInvariant` | 0/240 | 0/240 | 0/240 | 0/240 |
| `priorGateInvariant` | 0/240 | 0/240 | 0/240 | 0/240 |

`missRate` 사전 등록 슬라이스:

| 슬라이스 | before 1 | before 2 | after 1 | after 2 |
|---|---|---|---|---|
| `no_condition` | 40/40 (100.0%) | 40/40 (100.0%) | 2/40 (5.0%) | 0/40 (0.0%) |
| `constraint_price` | 39/40 (97.5%) | 40/40 (100.0%) | 4/40 (10.0%) | 2/40 (5.0%) |
| `constraint_budget_set`(N<40, exploratory) | 32/32 (100.0%) | 31/32 (96.9%) | 5/32 (15.6%) | 1/32 (3.1%) |

`falseAlarmRate` 사전 등록 슬라이스:

| 슬라이스 | before 1 | before 2 | after 1 | after 2 |
|---|---|---|---|---|
| `what_axis` | 0/64 (0.0%) | 0/64 (0.0%) | 2/64 (3.1%) | 0/64 (0.0%) |
| `blocking_rating` | 0/40 (0.0%) | 0/40 (0.0%) | 0/40 (0.0%) | 0/40 (0.0%) |

**before 두 런은 소수점까지 같은 값(111/112)을 냈다** — 이 하락은 노이즈로 설명되지 않는다.

**오탐 2건은 숨기지 않는다.** after 1 의 오탐 2건은 같은 앵커 `buy-under-0005`("삼성 제품
아무거나", `n=2`·`n=6`)이고 after 2 는 0건이다. 근거는 판정식에서 나온다:
`is_underspecified_turn` 은 what-축이 **하나라도** 안 비면 False 를 내므로, 그 표본이
`verdict=True` 라는 사실 자체가 **`filters.brand` 가 비어 있었음을 증명한다** — 그 표본에서
모델은 "삼성"을 `filters.brand` 로 추출하지 못했다. 전에는 지어낸 `semanticQuery` 가 그 추출
실패를 가려주고 있었다. 이 변경은 오탐을 **만든** 게 아니라 원래 있던 brand 추출 실패를
**드러낸다.** 드러난 결과는 "의미 없는 검색"이 아니라 되물음이다(플래그를 켠 경우).

> ⚠️ `samples.csv` 의 오탐 행 `causeAxes`(여기서는 `filters.brand`)를 "brand 가 비었다는
> **측정값**"으로 인용하지 말 것 — 하네스 규약상 오탐 행의 `causeAxes` 는 앵커의 정적
> `referenceAxes`("채워졌어야 할 축")이지 그 표본의 관측이 아니다(`../../README.md`
> §원인 축 분해). 위 근거는 `causeAxes` 가 아니라 판정식에서 나온다.

## 가드축 — "정직하게 비운 것"과 "잘못 비운 것"을 가른다

`semanticQueryIsFallback` 비율을 `what_axis`+`blocking_rating` 104표본으로 뭉쳐 보면 after 런이
30~36% 로 보인다. **그 숫자는 회귀가 아니다.** 앵커별로 쪼개면 위반이 어디서 오는지 드러난다
(분모 각 8):

| 앵커 | 무엇이 what-신호인가 | before 1 | before 2 | after 1 | after 2 |
|---|---|---|---|---|---|
| 이어폰 추천해줘 | 상품명(category) | 0/8 | 0/8 | **0/8** | **0/8** |
| 노트북 하나 추천해줘 | 상품명(category) | 0/8 | 0/8 | **0/8** | **0/8** |
| 레트로 무드등 아무거나 보여줘 | 상품명(keyword) | 0/8 | 0/8 | **0/8** | **0/8** |
| 미니멀 텀블러 아무거나 있어? | 상품명(keyword) | 0/8 | 0/8 | **0/8** | **0/8** |
| 삼성 제품 아무거나 | 브랜드만 | 0/8 | 0/8 | 2/8 | 0/8 |
| LG 가전 아무거나 있어? | 브랜드만 | 0/8 | 0/8 | 0/8 | 0/8 |
| 노란색으로 아무거나 골라줘 | 색상만 | 0/8 | 0/8 | 5/8 | 5/8 |
| 초록색 제품 뭐 있어? | 색상만 | 0/8 | 0/8 | 2/8 | 1/8 |
| `blocking_rating` 5앵커 합 | 평점만 | 0/40 | 0/40 | 28/40 | 26/40 |

**증류할 상품명이 실제로 발화에 있는 앵커는 위 4개(32표본)뿐이고, 거기서는 after 런도 0/32 다.**
나머지는 "평점 4 이상 아무거나"·"노란색으로 아무거나 골라줘"처럼 **상품 의미가 애초에 발화에
없는** 발화다 — 색은 `filters.color`, 평점은 `filters.rating_min` 이 이미 싣고 있고,
`semanticQuery` 를 비우는 것은 회귀가 아니라 이 이슈가 원한 정직함 그 자체다. 그 슬라이스에서
`falseAlarmRate` 가 0/40 인 것이 이 해석을 뒷받침한다(`rating_min` 은 `_BLOCKING_FILTER_AXES`
라 판정이 그대로 False 다 — 비워도 되물음이 오발동하지 않는다).

그래서 가드축은 **category·keyword 앵커 4개(32표본) ≤ 5%** 로 판정했다. 넓은 판(104분모)은
exploratory 로 병기하되 이 표 없이 인용하면 "30% 회귀"로 오독된다.

## 이 변경은 되물음 플래그와 **무관하게** 오늘 운영 동작을 바꾼다 (#162)

`semantic_query_is_fallback` 의 소비자는 되물음만이 아니다. `grep` 으로 센 소비자는 둘이다:

| 소비자 | 게이트 | 오늘 운영에서 |
|---|---|---|
| `underspecified.is_underspecified_turn`(#336) | `underspecified_reask_enabled`(기본 **False**) | 무동작 |
| `no_condition.is_no_condition_turn`(#162, api-spec §4.17) | **플래그 없음** | **켜져 있다** |

`semanticQueryIsFallback=true` 표본 수(240 분모): before **1/240 · 1/240** → after
**163/240 · 164/240**(after 1: `no_condition` 39 · `constraint_price` 37 ·
`constraint_budget_set` 27 · `blocking_rating` 28 · `multiturn_gate` 23 · `what_axis` 9).

**그중 실제로 `is_no_condition_turn` 이 되는 것은 일부다.** 그 함수는 `_FILTER_AXES` 를
**가격 포함 전부** 비어 있으라고 요구하고 `_DECISION_CONDITION_AXES` 도 보므로
`is_underspecified_turn` 보다 엄격하다(`no_condition ⊂ underspecified` 불변식 —
`tests/unit/test_underspecified.py::test_no_condition_implies_underspecified_when_flag_on`):
`constraint_price`·`constraint_budget_set` 는 `price_*` 가, `blocking_rating` 은 `rating_min`
이, `multiturn_gate` 는 `prior is not None` 이, `what_axis` 대부분은 `color`/`brand`/`keyword`
가 막는다. → 남는 것은 **`no_condition` 슬라이스 39~40/40**.

즉 "아무거나 추천해줘"류가 **무필터 I-1**(실측 7,245건·13.33MB·1.112s,
`docs/specs/MEASURE-I1-RESPONSE-132.md`)로 새던 것이 멈추고 #162 설계대로 I-3 인기 경로 +
고지로 간다. `no_condition.py` 모듈 docstring 이 그 무필터 호출을 **"계약 위반"** 이라고 부른다
— 회귀가 아니라 **두 번째 죽은 기능(#162)이 비로소 발동하는 것**이며, `no_condition.py` 는 한
줄도 바뀌지 않았다. 가격 제약만 있는 턴이 여전히 `is_no_condition_turn=False` 로 남는 혈반경은
기존 `tests/unit/test_no_condition.py::test_any_single_condition_axis_blocks_trigger` 가
고정한다(`price_max`·`price_min` 파라미터, 그 파일 `_decision()` 기본값이
`semantic_query_is_fallback=True`).

## 타축 회귀 — `intent_probe` 전/후 (기준 4)

산출물: `evals/intent_probe/baselines/fast-2026-08-07-430-{before,after}-*`.

**깎인 축은 `screenExactPick` 하나다.** (`screenResolution` 은 `screenExactPick` +
`screenNoHallucination` + `screenReask` 의 **합**이므로 같은 사실의 재보고다 — 별도 회귀로
세지 말 것.)

| 축 | before(2런) | after(F, 3런) | 판정 |
|---|---|---|---|
| **`screenExactPick`** | **32 · 32** | **31 · 31 · 29** | **⚠ −1.7 잔여** |
| `screenOutOfListConfirmCount`(진단) | **0 · 0** | **1 · 1 · 3** | ⚠ +1.7 잔여 |
| `screenNoHallucination` | 8/8 · 8/8 | 8/8 (전 런) | = 무회귀 |
| `screenReask` | 8/8 · 8/8 | 8/8 (전 런) | = 무회귀 |
| `screenResolution`(위 셋의 합, 재보고) | 48 · 48 | 47 · 47 · 45 | (= `screenExactPick`) |
| `mainIntent` | 240 · 239 | 240 · 239 · 238 | = |
| `cartControl` · `orderStatus` | 144 · 48 (전 런) | 144 · 48 (전 런) | = |
| `categoryClear` · `categoryCarry` | 32 · 32 | 32 · 32 · 32 / 32 · 31 · 32 | = |
| `demonstrative` | 96 · 95 | 96 · 95 · 94 | = |
| `optionAnswer` | 29 · 29 | 27 · 26 · 26 | = (before 폭 25~29 — 아래 각주) |
| `categoryReplace` | 23 · 24 | 21 · 18 · 21 | = 노이즈 판정 (아래 각주) |
| `categoryAction3Way` | 105 · 109 | 105 · 106 · 107 | = |
| `switchAll7` | 33 · 36 | **35 · 40 · 40** | ↑ |
| `switchLegacy2` | 8 · 9 | **10 · 11 · 12** | ↑ |
| `cartAddProductIdLegacy2` | 15 · 15 | **16 · 16 · 16** | ↑ |
| `categoryMixedReplace` | 18 · 21 | **20 · 25 · 22** | ↑ |
| **`conditionOnlyNoCategoryQuery`** | 36 · 36 | **38 · 38 · 37** | ↑ (이 PR 이 노린 효과가 다른 하네스에서 독립 확인) |

**`optionAnswer` 각주**: clean before 는 29·29 지만, `--prompt` 로 돌린 before 런 2개도
**비-screen 축에서는 유효**하다(위 † 참조) — 그 값이 25·26 이라 before 실측 폭은 **25~29** 이고
after 26~27 은 그 안이다.

**`categoryReplace` 각주**: F 3런이 21·18·21 로 **3런 중 2런이 20 이상**이라 노이즈로 판정한다.
근거 — before 실측 폭 자체가 20~24 이고(clean 23·24, `--prompt` before 21·20), 후보별 대조에
**추세가 없다**(c3n 21·23 · S 23·23 · B 22·22). 좁힘 문면은 카테고리 교체와 인과가 없다.

**`screenExactPick` 3런째(29)를 숨기지 않는다.** F 는 2런(31·31) 시점에 채택 결정을 받았고,
`categoryReplace` 정리용으로 돌린 3런째가 29 로 나왔다. 그래서 **잔여는 −1 이 아니라
`screenExactPick` 32.0 → 30.33(−1.67) · `outOfList` 0.0 → 1.67(+1.67)** 이다(평균).

후보별 평균(`screenExactPick` / `outOfList`):

| 후보 | 런 수 | `screenExactPick` 평균 | `outOfList` 평균 |
|---|---|---|---|
| before | n=2 | **32.0** | 0.0 |
| c3n(sq + attrConditions) | n=2 | 28.5 | 3.5 |
| S(둘 다 짧게) | n=2 | 30.0 | 2.0 |
| B(sq 만, 트리거 안 좁힘) | n=2 | 30.0 | 2.0 |
| **F = 채택** | **n=3** | **30.33** | **1.67** |

읽히는 것 둘:

- **약 −2 의 screen 비용은 문면과 무관하게 `semanticQuery` 규칙 자체에 내재한다** — 후보 4종
  9런이 같은 말을 한다. F 의 screen 우위(31·31)는 작은 표본 효과가 컸다.
- **c3n 만 유독 나쁘다(28.5 / 3.5)** — S·B·F 대비 약 **−1.5** 가 `attrConditions` 추가분에
  귀속된다. 그 문면을 뺀 결정(할 일 ③ 반려)의 근거가 이 숫자다.

> ⚠️ **비교가 비대칭이다** — F 만 n=3 이고 나머지 후보는 n=2 다(F 3런째는 `categoryReplace`
> 정리용으로 돌린 것이 screen 표본으로도 쓰인 것이다). 평균끼리 비교할 때 이 사실을 함께 읽어야
> 한다: n=2 후보들의 평균은 F 보다 신뢰구간이 넓다.

F 를 고르는 근거는 그래도 남는다: primary 쌍이 가장 좋고(`missRate` 9.8·2.7%,
`falseAlarmRate` 1.9·0.0%, 가드 0/32·0/32), screen 은 최소 동률(30.33 vs 30.0)이며, 문면이
**코드 의미와 일치**한다(아래 「트리거를 맥락까지 좁힌 이유」).

## 트리거를 맥락까지 좁힌 이유 — 회귀 회피가 아니라 정확한 서술

`semantic_query_is_fallback = not (llm_sq or cat_signal or prior_sq)` 이라 **맥락(prior)이 있으면
플래그는 어차피 False** 이고, `is_underspecified_turn` 은 `prior is None` **첫 턴 한정**이다.
그러므로 "발화에도 맥락에도 없으면"이 코드가 실제로 하는 일이고, "발화에 없으면"은 코드보다
**넓게** 말한 것이었다. 되물음 표적(조건 없는 첫 턴)은 맥락이 전부 비어 있어 이 좁힘의 영향을
받지 않는다 — 실제로 `missRate` 는 B(8.9·8.9%)와 F(9.8·2.7%)가 같은 수준이다.

넓게 쓴 문면이 왜 screen 을 깎았는지에 대한 가설: `_SYSTEM` 에는 이미 "상품명 없는 지시대명사는
PRIOR_FILTERS.semanticQuery 또는 LAST_RECOMMENDATIONS 맥락의 **상품**을 가리킵니다"가 있는데,
screen 셀의 발화("이거 담아줘"·"3번째 거")는 **발화 자체에는 상품 의미가 없고 맥락에만 있다.**
모델이 "발화에 없으면 비워라·지어내지 마라"를 "맥락에서 끌어와 해소하는 것도 하지 마라"로
일반화하면 정확히 `screenExactPick` 하락 + `screenOutOfListConfirmCount` 상승으로 나타난다 —
관측과 부합한다. 좁힌 뒤 두 지표가 부분적으로 회복된 것이 이 가설의 근거이고,
**완전히 회복되지는 않았다는 것이 이 가설의 한계**다.

잔여 회귀에 대해 **쓸 수 있는 완화 근거**(전부 실측):

- `screenNoHallucination` 8/8 · `screenReask` 8/8 — 전 런 무회귀. **화면 밖 상품을 지어내지
  않고 되물음도 멀쩡하다.** 화면 **안에서 잘못 고르는** 빈도만 늘었다.
- 목록 밖 id 는 `docs/api-spec.md` §3.1 [보안] 담기 가드가 **여전히 차단**한다(방어 심층 유지).
- 완화를 **세 번 독립적으로 시도**했고 그 궤적이 아래 표에 남아 있다. 잔여는 시도를 안 해서
  남은 것이 아니다.

**쓰면 안 되는 근거**: "FE 는 아직 screen 을 보내지 않는다"(`decompose.py` 의 screen 주석).
`docs/api-spec.md` §3.1 은 `chat`·`seller_orders`·`seller_products` **3종이 실제로 온다**고
적는다 — 코드 주석이 정본보다 낡았다(이 PR 범위 밖, 후속 이슈 후보).

## 후보 선별 — 무엇을 커밋했고 무엇을 뺐나

후보 런 산출물은 **커밋하지 않는다**(후보 프롬프트 파일도 커밋하지 않는다 — 채택안은
`decompose._SYSTEM` 에 들어갔고, 탈락안은 아래 sha12 로 재생성·재현이 가능하다).
`--prompt` 열의 후보는 각 1회 실행이라 **선별일 뿐 채택 판정이 아니다.**

| 후보 | 무엇을 바꿨나 | prompt sha12 | 측정 방식 | `missRate` | `falseAlarm` | 가드/32 | `screenExactPick` | `outOfList` |
|---|---|---|---|---|---|---|---|---|
| before | — | `11c6fe3bfa0c` | repo (2런) | 99.1% · 99.1% | 0 · 0 | 0 · 0 | 32 · 32 | 0 · 0 |
| C1a | 상단 JSON 스키마 줄에만 "없으면 빈 문자열" | `90e2efb544af` | `--prompt` (1런) | 24.1% | 0 | 0 | 측정 불가† | — |
| C1b | recommend 규칙 절 끝에 불릿으로 | `f9b95c86df81` | `--prompt` (1런) | 17.0% | 0 | 0 | 측정 불가† | — |
| C2b | C1b + 수치제약 지시 재작성(장문) + 동의어 문장 조건화 | `4e5f621c6183` | `--prompt` (1런) | 48.2% | 0 | 0 | 측정 불가† | — |
| C2p | C1b + 수치제약 지시 재작성(최소) | `1ae861a03da0` | `--prompt` (1런) | 23.2% | 1 | 0 | 측정 불가† | — |
| C3p | C3n + 수치제약 지시 재작성(최소) | `8797ea048622` | `--prompt` (1런) | 13.4% | 0 | 0 | 측정 불가† | — |
| c3n | sq 규칙 + attrConditions 규칙(장문, +268자) | `2eeab1f8a6ac` | repo (2런) | 7.2% · 10.7% | 1 · 1 | 0 · 0 | **30 · 27** | 2 · 5 |
| S | 위 둘을 짧게(+161자) | `f2c711d279a8` | repo (2런) | 11.6% · 8.0% | 2 · 0 | 1 · 0 | **31 · 29** | 1 · 3 |
| B | sq 규칙만, 트리거 "발화에 없으면"(+110자) | `6ae48dfa3f5f` | repo (2런) | 8.9% · 8.9% | 1 · 2 | 0 · 0 | **30 · 30** | 2 · 2 |
| **F = 채택** | sq 규칙, 트리거를 **맥락까지 포함해 좁힘**(+143자) | `81e3770e1340` | repo (underspec 2런 / intent 3런) | **9.8% · 2.7%** | 2 · 0 | **0 · 0** | **31 · 31 · 29** | **1 · 1 · 3** |

† `--prompt` 오버라이드는 `SystemPromptOverrideLLM` 이 decompose 의 system 을 후보 텍스트로
갈아끼우는데, screen 이 실린 셀은 프로덕션에서 `_SYSTEM_WITH_SCREEN`(= `_SYSTEM` + 화면 규칙)을
쓴다. 오버라이드가 그 문면까지 덮으므로 **screen 축은 `--prompt` 런과 `repo:_SYSTEM` 런 사이에서
비교할 수 없다.** 이 사실을 발견한 뒤 screen 축이 걸린 후보는 전부 리포 `_SYSTEM` 에 넣고
`repo:_SYSTEM` 으로 다시 쟀다(위 표의 "repo" 행). `intent_probe` 의 before 팔도 같은 이유로
재실행했다(`--prompt` 로 돌린 before 런은 **비-screen 축에서는 여전히 유효**하다 —
`_SYSTEM_WITH_SCREEN` 은 screen 이 실린 턴에만 쓰이고 나머지 셀에서는 문자열이 같기 때문이다).

이 표에서 읽히는 세 가지:

1. **위치가 효과를 바꾼다** — 같은 취지를 스키마 줄에 적으면 24.1%, 규칙 절 불릿으로 적으면 17.0%.
2. **지시를 더 얹으면 먼저 얹은 지시가 희석된다** — C2b 는 C1b 대비 `missRate` 를 17.0% →
   48.2% 로 되돌렸다. 산출물이 원인을 말해준다: 새로 넣은 "상품 의미만"·"남는 상품 의미가
   없으면 비웁니다"가 또 하나의 **인용 가능한 문면**이 돼 모델이 빈 문자열 대신 그 말을 적었다
   (실측 산출 `'상품 의미'` · `'상품의 의미를 추출하지 못해 빈 문자열'`).
3. **screen 비용의 원인은 길이가 아니었다** — c3n(+268자) 평균 28.5 → S(+161자) 30 →
   B(+110자) 30 으로 **가장 짧은 판이 나아지지 않았다**(길이 가설 반증). 트리거를 맥락까지
   좁힌 F(+143자, B 보다 길다)가 31·31·29(평균 30.3)로 가장 나았지만 **완전히 회수하지는
   못했다** — 약 −2 의 screen 비용은 문면 변형과 대체로 무관하고 `semanticQuery` 규칙 자체에
   내재하는 것으로 보인다. 자세한 진단은 `docs/lessons.md` 2026-08-07 항목.

## 이슈 「할 일」 ②·③ 의 결론

- **② "수치 제약을 semanticQuery 로 근사하지 마라"가 왜 안 먹히는지 → 진단했고, 문구 수정은
  반려했다.** 진단: 그 금지형 문장은 같은 불릿의 **뒤쪽** 무조건 긍정 명령("semanticQuery 는
  동의어·상위어를 함께 담은 의미 중심 자연어로 쓰세요")에 진다. 직접 증거 — 모델이 그 문면을
  그대로 에코한 산출(`'의미 중심 자연어'` · `'무엇을 살지에 대한 의미 중심의 일반 추천'`).
  재작성 후보 2종(C2b 48.2%, C2p 23.2%·오탐 1)은 primary 를 각각 +31.2pp·+6.2pp 깎았고
  수치 에코를 줄이지도 못했다. 원인이 그 문장의 **어휘**가 아니라 "이 필드는 비울 수 없다"가
  유효 규칙이었던 데 있었기 때문이고, 비울 수 있게 하자 수치 에코가 **부수적으로** 줄었다.
- **③ attrConditions 조작 억제 → 이번 PR 에서는 반려(측정된 거래).** 그 규칙은 **효과가
  있었다** — c3n 에서 미탐의 `filters.attrConditions` 갈래가 **0건**이 됐다(before 6~9건,
  S 9·5건). 그러나 그 문면이 `screenExactPick` 을 30·27 로, `outOfList` 를 0 → 2·5 로 끌었다.
  그래서 별도 이슈로 분리할 것을 제안한다(전용 호출·더 짧은 문면·후처리 등 다른 수단이 남아 있다).
  **채택안 F 에는 `attrConditions` 규칙이 없다** — after 런의 미탐에 그 갈래가 다시 보이는
  이유다(after 1: 2건, after 2: 0건).

## 한계

- 이 하네스는 **decompose 직후·판정 직전 형상**만 잰다 — 카테고리 매핑·전개 LLM 생성은 부르지
  않는다(`../../README.md` §측정 범위와 한계).
- 비용·토큰은 **부분합**이다(`results.json.budget.unknownCostCallCount` — provider 가 usage 를
  보고하지 않은 콜은 집계에서 빠졌다). 이 런의 총 비용은 표기값보다 크다.
- 산출물은 스크래치패드로 `--out` 한 뒤 이 경로로 옮겼다 — `run_manifest.json.run.command` 의
  `--out` 경로가 임시 경로인 이유다. `dirty: true` 는 프롬프트 변경이 아직 커밋되지 않은 채
  측정했다는 사실의 정직한 기록이다(프롬프트 신원은 `prompt.sha256` 이 못박는다).
- 잔여 미탐의 주축은 `categoryQueries` 동반(after 1: 2건, after 2: 2건)이다 — 이슈 「할 일」
  목록 밖이라 이번 PR 에서 건드리지 않았다. 후속 이슈 후보.
