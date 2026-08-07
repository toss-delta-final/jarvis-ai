# 과소지정 판정 축 실 LLM 프로브 (#380)

`is_underspecified_turn`(`app/agents/buyer/recommendation/underspecified.py`)이 실 발화에서
**decompose 직후·판정 직전의 형상**으로 얼마나 정확히 판정하는지를 실 LLM 반복 분포로 잰다.
SPEC-UNDERSPECIFIED-336 §7.3 이 남긴 게이트 잔여 항목 1("실 LLM decompose 가 판정 축을 실제
발화에서 얼마나 정확히 산출하는지는 실측하지 않았다")을 채운다.

## 무엇을 재는가

발화 1건 → `decompose` 1회 호출 → 그 `RouteDecision` 을 `is_underspecified_turn` 에 그대로 넣어
판정한다. 셀 = 앵커 1개(컨텍스트 행렬 없음 — `evals/legs_probe` 와 같은 구조). 30 앵커 × N=8 =
240콜. **판정식은 이 하네스에 옮겨 적지 않는다** — 프로덕션 함수(`is_underspecified_turn`·
`detect_expansion_need`)를 그대로 호출한다.

## 측정 범위와 한계

프로덕션은 `app/agents/buyer/graph.py` 에서 **decompose → `_prepare_recommendation`(카테고리
매핑 + `needs_expansion` #217) → `is_underspecified_turn`** 순서로 돈다. 이 하네스는 가운데
단계를 부르지 않는다(그 단계는 pgvector·임베딩·추가 LLM 호출을 요구하고, 그 실패 축은
#331/#332 의 몫이라 함께 재면 원인이 뒤섞인다 — `legs_probe` 가 `needs_expansion` 을 뺀 것과
같은 근거). 그래서:

1. **`category_legs` 는 이 하네스에서 항상 빈다.** 판정은 `category_legs or category_queries`
   를 보므로 `category_queries` 가 비어 있으면 프로덕션도 legs 를 만들지 못한다 — 이 축에서는
   등가다.
2. **`filters.category` 는 이 하네스에서 구조적으로 항상 빈다.** decompose 의 `_SYSTEM` JSON
   스키마(`filters`)에는 애초에 `category` 키가 없다 — decompose 는 이 필드를 절대 직접 채우지
   않는다(오직 `_prepare_recommendation` 의 카테고리 매핑만 채운다). 프로덕션에서는 매핑 결과가
   없으면 `_prepare_recommendation` 이 `decision.filters.category = None` 으로 **덮어써 지운다**
   (`app/agents/buyer/graph.py::_prepare_recommendation` — `category_legs` 가 있고
   `category_expanded` 가 아니면 대표 canonical 을 싣고, 그 외(매핑 결과 없음)는 미검증
   `filters.category` 를 비운다). 즉 decompose 가 `category_queries` 없이
   `filters.category` 만 에코한 표본이 이 하네스에서 what-축으로 차단되는 시나리오는 애초에
   재현되지 않는다(그 값 자체가 이 하네스에서 나올 수 없다). 진단 카운터
   **`categoryEchoWithoutQueriesCount`**(`filters.category` 가 비어 있지 않은데
   `categoryQueries` 는 빈 표본 수)를 산출물에 실어 노출 크기를 수치로 남긴다 — 위 이유로 이
   값은 이 하네스에서 항상 0 이다(구조적 공허). 이 하네스의 정규 축(`referenceAxes` 어휘)이
   `filters.category` 를 쓰지 않는 것도 같은 이유다(대신 `categoryQueries` 를 쓴다).
3. **`needs_expansion`(#217)의 게이트 판정 함수는 부르지만, 전개 자체(카테고리 매핑·전개 LLM
   생성)는 부르지 않는다** `[F-3, 2차 리뷰어 발견]`**.** `detect_expansion_need(legs, case=...,
   unresolved=[])` 는 이 하네스가 **매 표본 실제로 호출**하는 프로덕션 함수다 — `case==3` ∧
   신호 있는 leg 없음이면 `"no_legs"` 를 돌려준다. 부르지 않는 것은 그 뒤 단계(카테고리 매핑·
   전개 LLM 이 `category_queries` 를 실제로 채우는 것)뿐이다 — 그게 성공하면 **프로덕션 판정은
   False 가 된다**(카테고리 매핑까지 성공한다는 전제 아래). 즉 이 하네스가 "되물음 대상"으로
   판정한 표본 중 일부는 프로덕션에서 되물음이 발동하지 않을 수 있다. → 진단 축
   **`expansionGateWouldFireRate`**(분자: `detect_expansion_need(...)` 가 사유를 돌려준 표본,
   분모: 판정 True 표본)와 **`missRateUnderExpansionAssumption`**(전개가 항상 leg 을 낸다고
   가정한 **상한**, D8 참조)을 싣는다. `unresolved=[]` 는 이 하네스가 카테고리 매핑(2단계)을
   돌리지 않는다는 사실의 정직한 반영이다 — D2(`mapping_failed`) 규칙은 이 하네스에서 발동할
   수 없다. union(전개 후 판정) 실측은 **후속 이슈 후보**로 남긴다(`legs_probe` 의 union
   커버리지 선례와 동형).
4. **프로덕션은 `intent == "recommend"` 인 턴에서만 판정을 호출한다** `[F-1, 2차 리뷰어
   발견]`**.** `app/agents/buyer/graph.py::run_buyer_turn` 은 `decision.intent` 가
   `general`·`cart_view`·`order_status`·`cart_add`·`cart_remove`·`wishlist_add`·
   `wishlist_remove`·`wishlist_view`(#386) 인 분기에서 전부 `is_underspecified_turn` 호출
   이전에 return 한다.
   decompose 가 앵커를 그 intent 로 라우팅한 표본(예: "뭐 좋은 거 없어?"가
   fast 티어에서 `general` 로 라우팅되는 경우)은 프로덕션 판정 함수에 도달조차 하지 않으므로,
   confirmatory 축(`missRate`·`falseAlarmRate`·`falseAlarmRateWithGateSlice`·`judgmentAccuracy`·
   `judgmentAccuracyWithGateSlice`·`missRateUnderExpansionAssumption`·`expansionGateWouldFireRate`)
   의 분모를 `intent == "recommend"` 표본으로 좁힌다. 비-recommend 표본은
   **`nonRecommendIntentCount`**(앵커별·intent별)로만 드러나며, 그 실패는 intent 라우팅 축
   (`evals/intent_probe`)의 소관이다 — 여기서 미탐/오탐으로 세면 원인이 뒤섞인다. 포함판
   (`missRateWithNonRecommendIntent`·`falseAlarmRateWithNonRecommendIntent`, exploratory)을
   비교용으로 병기한다. `flagOffInvariant`·`priorGateInvariant` 는 판정 함수의 게이트 자체를
   보는 불변식이라 intent 와 무관하게 **전 표본 그대로** 쓴다.
5. **단일 턴만 잰다** — 컨텍스트 행렬 없음(`profile_summary`·`last_recommendations`·
   `pending_cart`·`screen` 전부 None).

## #372 와 축이 다르다(숫자 섞지 말 것)

| | #372(`test_underspecified_answer_turn.py`) | 이 하네스(#380) |
|---|---|---|
| 무엇을 재는가 | 되물음 **답변 턴**의 멀티턴 처리·완화칩 우선순위 | **첫 턴 판정** 정확도 |
| 평가 | 결정론 fixture 1회(CI) | 확률 분포(실 LLM 반복, 수동) |
| 입력 | 손으로 채운 "LLM 이 이렇게 낸다"는 가정 | 실 LLM decompose 산출 |

## 이건 골든셋이 아니다(숫자 섞지 말 것)

| | `evals/goldenset` | `evals/legs_probe` | 이 하네스 |
|---|---|---|---|
| 본체 | 추천 품질 | decompose case·legs 산출 분포 | **decompose 직후 과소지정 판정 분포** |
| 평가 | 결정론 1회 | 확률 분포(단일 턴) | 확률 분포(**단일 턴**) |
| 세션 상태 | 없음 | 없음 | priorExists 로 게이트만 별도 슬라이스에서 흉내(고정 빈 상태) |

## cases.json 8건 매핑표

`evals/underspecified_cases/cases.json`(#336, 8건)이 어디로 갔는지 전부 여기서 읽힌다.

| caseId | 이 하네스의 슬라이스 | 비고 |
|---|---|---|
| `buy-under-0001` | `constraint_budget_set` | 그대로 승계, `caseId`·`sourceCaseId` 동일 |
| `buy-under-0002` | `no_condition` | 그대로 승계 |
| `buy-under-0003` | `constraint_price` | 그대로 승계 |
| `buy-under-0004` | `what_axis`(category) | 그대로 승계 |
| `buy-under-0005` | `what_axis`(brand) | 그대로 승계 |
| `buy-under-0006` | `multiturn_gate` | 그대로 승계, `priorExists=true` |
| `buy-under-0007` | `blocking_rating` | 그대로 승계 |
| `buy-under-0008` | **셀 아님** | 발화가 `buy-under-0003` 과 글자 그대로 같아(앵커 발화 고유 검증자와 충돌) 셀로 만들 수 없다. 판정 첫 줄 `if not settings.underspecified_reask_enabled: return False` 는 LLM 산출과 무관해 실 LLM 콜을 쓰면 아무것도 재지 않으면서 INV 분모만 부풀린다. 대신 **`flagOffInvariant`**(D9)가 전 표본(240개)에 대해 같은 것을 훨씬 강하게 잰다 — `underspecified_reask_enabled=False` 로 재판정했을 때 True 인 표본 수가 0인지, LLM 콜 0으로. |

나머지 23 앵커는 신규(신규 발화, `under-<슬라이스약자>-NNNN`). `decompose._SYSTEM` 프롬프트에
예문으로 등장하는 발화는 배제해 골랐다(`_SYSTEM` 을 읽고 대조했다) — 이 하네스에는
`promptExample` 필드를 두지 않는다.

## 실행법

```bash
# 오늘의 기준선(fast 티어)
uv run python -m evals.underspecified_probe --out evals/underspecified_probe/baselines/fast-2026-08-06 --tier fast

# API 없이 배관만 확인(가짜 LLM)
uv run python -m evals.underspecified_probe --out /tmp/probe --dry-run

# 후보 decompose 프롬프트 재기(intent_probe.client 재사용)
uv run python -m evals.underspecified_probe --out artifacts/cand1 --prompt cand1.txt
```

기본 규모: 30셀 × N=8(`--n`) = 240콜, 45rpm 페이서 기준 약 6~8분, `fast` 티어 대략
USD 0.03~0.10(legs_probe 312콜 실측 부분합 $0.07 에서 환산). `--budget-usd` 기본 5.0 이면
충분하다.

## 축과 정의

정의는 `metrics.py` 의 각 `axis_*` 함수가 만드는 `AxisResult` 에 데이터로 있고 **산출물에
그대로 실린다**(`results.json.axes[*].definition`, `report.md` 축 표).

| axisId | 분자 | 분모 | 성격 |
|---|---|---|---|
| `missRate`(미탐율) | 판정 False | `expectedReask=true` 앵커의 **recommend** 표본(F-1) | **confirmatory-primary** |
| `missRateWithNonRecommendIntent` | 위와 같음 | `expectedReask=true` 앵커의 표본 전부(intent 무관, 포함판) | exploratory |
| `falseAlarmRate`(오탐율) | 판정 True | `expectedReask=false` 앵커의 **recommend** 표본 중 `multiturn_gate` 슬라이스 제외(F-1) | **confirmatory-secondary** |
| `falseAlarmRateWithGateSlice` | 위와 같음 | `expectedReask=false` 의 recommend 표본 전부(게이트 포함) | exploratory |
| `falseAlarmRateWithNonRecommendIntent` | 위와 같음 | `expectedReask=false` 표본(intent 무관, 게이트 제외, 포함판) | exploratory |
| `judgmentAccuracy`(판정 정확도) | 판정 == `expectedReask` | recommend 표본(게이트 제외, F-1) | exploratory |
| `judgmentAccuracyWithGateSlice` | 위와 같음 | recommend 표본(게이트 포함) | exploratory |
| `missRateUnderExpansionAssumption` | (판정 False) 또는 (판정 True ∧ `detect_expansion_need` 가 사유를 돌려줌) | `expectedReask=true` 앵커의 recommend 표본 | exploratory — **가정 기반 상한** |
| `expansionGateWouldFireRate` | `detect_expansion_need(...)` 가 사유를 돌려준 표본 | 판정 True 인 recommend 표본 | exploratory(진단) |
| `flagOffInvariant` | `underspecified_reask_enabled=False` 재판정 True 표본 수 | 전 표본(intent 무관 — 판정 게이트 자체를 보는 불변식) | invariant(0이어야 한다) |
| `priorGateInvariant` | `prior=ProductSearchFilters()` 재판정 True 표본 수 | 전 표본(intent 무관) | invariant(0이어야 한다) |

**primary 선정 사유**: `missRate` 는 "플래그를 켜도 되물음이 조용히 아무 일도 하지 않는가"를
직접 재는 축이고, 이슈가 지목한 두 위험 중 코드 테스트로 절대 못 잡는 쪽이다. `falseAlarmRate`
는 사용자 체감 하방이 더 나쁜 축이라 confirmatory-secondary 로 사전 등록해 α 보정 대상에 함께
넣는다. 나머지는 exploratory.

**사전 등록 슬라이스**: `missRate` 는 `no_condition`·`constraint_price`, `falseAlarmRate` 는
`what_axis`·`blocking_rating`(#328 규약 5 — 나머지 슬라이스는 병기하되 exploratory).

**게이트 슬라이스 제외 사유**: `multiturn_gate` 는 판정 두 번째 줄 `if prior is not None:
return False` 가 LLM 산출과 무관하게 항상 False 를 내므로, 오탐율 분모에 넣으면 구조적 성공이
오탐율을 희석한다(`legs_probe` 가 `promptExample` 을 confirmatory 에서 제외한 것과 같은 규약).
포함판을 exploratory 로 병기해 둘 다 읽히게 한다.

**포함판은 두 축만 둔다**(`missRateWithNonRecommendIntent`·`falseAlarmRateWithNonRecommendIntent`)
— `judgmentAccuracy`·`missRateUnderExpansionAssumption` 등 나머지 축까지 intent 포함판을 만들면
표가 읽히지 않는다(F-1). axisId 는 `AXIS_BUILDERS` 의 map key 와 정확히 일치한다(legs_probe
`[R2-3]` 교훈 승계).

**모든 축은 슬라이스별로 분자·분모·비율·CI95 를 병기**한다. `SLICE_SAMPLE_THRESHOLD = 40`
(legs_probe 와 같은 값) 미만이면 `belowSampleThreshold=true` 로 표시하고 `report.md` 가 스스로
`exploratory` 라벨을 단다. N=8 기준 슬라이스가 5앵커여야 40 표본이다 —
`constraint_budget_set`(4)·`multiturn_gate`(3)은 설계상 임계 미만이고 그 사실이 표에 드러나는
것이 정상이다. **분모가 0인 슬라이스는 `exploratory: N<40` 이 아니라 `해당 없음` 으로
표시한다**(F-5) — 예를 들어 `missRate` 는 `expectedReask=true` 앵커만 분모에 넣으므로
`what_axis`·`blocking_rating`·`multiturn_gate` 슬라이스(전부 `expectedReask=false`)는
"표본이 적어서"가 아니라 **구조적으로 잴 수 없는** 칸이다.

## trivial baseline

"항상 reask=false"(= 플래그 off 동작). LLM 없이 결정론 계산: `missRate`=1.0(구조적 — 항상
False 를 내므로 `expectedReask=true` 앵커를 전부 놓친다) · `falseAlarmRate`=0.0(정의상 — 항상
False 이므로 오탐이 성립하지 않는다) · `judgmentAccuracy` = `expectedReask=false` recommend
표본 수 / recommend 표본 수(F-1 — 실측과 같은 분모). `report.md` 에 LLM vs baseline 대조표가
실리고, 분모 정의가 다른 값을 대조하지 않는다(#234/#240 사고 — `legs_probe` README `[R4-2]`
참조).

## 원인 축 분해 (이슈 완료 조건 3)

판정이 기대와 다른 모든 표본에 원인 축을 귀속한다.

- **미탐**(기대 true, 판정 false): 단일 축 소거 재판정(ablation). D8 정규 축 목록의 각 축을
  하나씩 "빈 값"으로 바꾼 `RouteDecision` 사본을 만들어 `is_underspecified_turn` 을 다시
  호출하고, 판정이 True 로 뒤집히는 축을 원인으로 기록한다(복수 가능).
  `semanticQueryIsFallback` 의 "빈 값"은 `True` 다(폴백 = 신호 없음). 어느 단일 축으로도 안
  뒤집히면 `causeAxes=["multiple"]` 로 기록하고, 비어 있지 않은 차단 축 전부를 `blockingAxes`
  컬럼에 함께 남긴다.
- **오탐**(기대 false, 판정 true): 앵커의 `referenceAxes` 를 원인으로 싣는다("채워졌어야 할
  축이 안 채워짐").
- **`multiple` 은 서술로 풀지 않는다** `[F-2, 2차 리뷰어 발견]`**.** "두 축이 함께 막았다"는
  사실을 문서 산문으로 요약하면(예: "19건이 X 와 함께") 실제 조합과 어긋날 수 있다 — SPEC·
  baseline README 가 실측과 다른 원인 축을 적어 다음 사람의 판단을 잘못 이끈 사고가 났다.
  대신 `missBlockingAxisComboCounts`(`cause_axis_summary` 산출, `blockingAxes` 를 정렬해 `;`
  로 join 한 문자열별 집계)를 `report.md` 「원인 축 분해」절과 `results.json.causeAxisSummary`
  에 실어, 문서는 이 표를 **인용만** 하고 다음 사람은 산출물에서 바로 조합을 읽는다.
- 노출: `report.md` 의 「원인 축 분해」절(축별 집계표 + `blockingAxes` 조합별 집계표) **및**
  `samples.csv` 의 컬럼(`intent`·`case`·`semanticQueryIsFallback`·`semanticQuery`·`verdict`·
  `expectedReask`·`outcome`(`hit`/`miss`/`falseAlarm`/`correctReject`)·`causeAxes`·
  `blockingAxes`, F-1). **런 재실행 없이 재집계 가능하다.** `semanticQuery` 원문을 싣는 이유는
  실측의 핵심 발견("fast 가 무조건 발화에도 의미쿼리를 지어낸다")을 산출물만으로 재집계할 수
  있어야 하기 때문이다(F-1 — legs_probe 가 `legQueries` 원문을 싣는 것과 같은 규약).

## 불변 재판정 (LLM 콜 0 — 같은 표본 재사용)

수집한 모든 표본에 대해 판정을 다시 돌린다:

- `flagOffInvariant` — `underspecified_reask_enabled=False` 로 재판정. True 인 표본 수는
  0이어야 한다(`buy-under-0008` 의 실측판).
- `priorGateInvariant` — `prior=ProductSearchFilters()` 로 재판정. True 인 표본 수는 0이어야
  한다(`buy-under-0006` 의 일반화).

0 이 아니면 `report.md` 가 경고 줄을 낸다.

## 재현 함정

1. **전역 페이서 필수** — 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가
   거짓이 된다.
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은
   `results.json.unfilledCells` 에 드러나며 종료 코드 4가 된다.
3. **단일 실행으로 판정 금지** — 채택 판정은 독립 2~3회 분포로 한다.
4. **이건 골든셋이 아니다 / #372 와 숫자를 섞지 말 것** — 위 두 절 참조.

## 산출물과 종료 코드

`--out <dir>`(이미 있으면 덮지 않는다)

| 파일 | 내용 |
|---|---|
| `results.json` | 축·정의·슬라이스별 병기·진단·baseline·원인 축 분해·못 채운 셀·페이서·예산 |
| `report.md` | 헤더 + primary/secondary 지표 + 축 표 + 슬라이스 표 + baseline 대조 + 원인 축 분해 + 불변 재판정 |
| `samples.csv` | 표본 1행씩(caseId·n·slice·intent·case·semanticQueryIsFallback·semanticQuery·verdict·expectedReask·outcome·causeAxes·blockingAxes·expansionReason·latencyMs, F-1) |
| `cells.csv` | 셀 1행씩(표본·시도·실패·충족 여부) |
| `failures.csv` | 버린 시도 1행씩 |
| `run_manifest.json` | 커밋·dirty·앵커 해시·실제 보낸 프롬프트 해시·축 정의·measurementScope·티어·모델 |

| 코드 | 뜻 |
|---|---|
| 0 | 모든 셀을 채웠다 |
| 2 | 사전 거부(인자·`--out` 존재·앵커 해시/스키마 불일치·프롬프트 읽기 실패·LLM 미설정) |
| 3 | 예산 초과로 중단(부분 산출물 기록) |
| 4 | 못 채운 셀이 있다(부분 산출물 기록) |

## 앵커(정답지)

`fixtures/anchors.json`(30건) — `fixtures/manifest.json` 의 sha256 과 대조해 읽는다(불일치 →
종료 코드 2). 슬라이스 구성: `no_condition` 5(MFT) · `constraint_price` 5(MFT) ·
`constraint_budget_set` 4(MFT) · `what_axis` 8(INV, category 2·brand 2·color 2·keyword 2) ·
`blocking_rating` 5(INV) · `multiturn_gate` 3(INV, `priorExists=true`). 합 30(MFT 14 / INV 16).
cases.json 승계 7건(위 매핑표) + 신규 23건.

## 기준선

`baselines/README.md` — 기준선 색인. 여러 판(프롬프트 세대·티어별)이 쌓여 있으므로 **정본이
무엇인지는 그 색인에서 확인해라**(#433) — 요약하면 `#430` before·`#431` 전환 판단의 정본은
`fast-2026-08-08-run1~3`(현행 dev 프롬프트, n=3 분포)이고, `fast-2026-08-06`(+run2/run3)은
pre-#386 프롬프트 세대의 역사 기록이다.
