# 과소지정 판정 축 실 LLM 프로브 리포트

prompt=6f64dcbd43d4 (repo:_SYSTEM) · tier=fast · model=gpt-5-nano · fixture=underspec-anchors-v1 · N=8 · underspecifiedReaskEnabled=True

> **이건 골든셋이 아니다.** 추천 품질이 아니라 `is_underspecified_turn` 판정의 실 LLM 반복 분포를 잰 표다. 프로덕션은 decompose 뒤에 카테고리 매핑·`needs_expansion` 보정을 거치므로(§측정 범위와 한계, README 참조) 이 표의 수치를 사용자 체감 되물음율로 곧바로 읽으면 오독이다.

## Primary confirmatory 지표 — missRate

`missRate`: 14/112 (12.5%)
CI95 [7.6%, 19.9%]

슬라이스별(사전 등록: no_condition · constraint_price):

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_price | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_budget_set | 2/32 (6.2%) (exploratory: N<40) | [1.7%, 20.1%] |
| what_axis | 0/0 (해당 없음) | N/A |
| blocking_rating | 0/0 (해당 없음) | N/A |
| multiturn_gate | 0/0 (해당 없음) | N/A |

## Confirmatory-secondary 지표 — falseAlarmRate

`falseAlarmRate`(multiturn_gate 제외): 0/89 (0.0%)
CI95 [0.0%, 4.1%]

슬라이스별(사전 등록: what_axis · blocking_rating):

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/0 (해당 없음) | N/A |
| constraint_price | 0/0 (해당 없음) | N/A |
| constraint_budget_set | 0/0 (해당 없음) | N/A |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/0 (해당 없음) | N/A |

## 축 전체

| 축 | 점수 | CI95 | 성격 | 분자 정의 | 분모 정의 |
|---|---|---|---|---|---|
| `expansionGateWouldFireRate` 전개 게이트 발동률(판정 True 표본 중) | 49/98 (50.0%) | CI95 [40.3%, 59.7%] | exploratory | detect_expansion_need(...) 가 사유를 돌려준 표본 | 판정 True 인 recommend 표본(F-1) |
| `falseAlarmRate` 오탐율 | 0/89 (0.0%) | CI95 [0.0%, 4.1%] | confirmatory-secondary | 판정 True | expectedReask=false 앵커의 recommend 표본(multiturn_gate 슬라이스 제외)(F-1) |
| `falseAlarmRateWithGateSlice` 오탐율 | 0/113 (0.0%) | CI95 [0.0%, 3.3%] | exploratory | 판정 True | expectedReask=false 앵커의 recommend 표본(게이트 포함)(F-1) |
| `falseAlarmRateWithNonRecommendIntent` 오탐율(비-recommend 포함, 참고용) | 0/89 (0.0%) | CI95 [0.0%, 4.1%] | exploratory | 판정 True | expectedReask=false 표본(intent 무관, multiturn_gate 제외, 포함판) |
| `flagOffInvariant` flag off 불변식 | 0/225 (0.0%) | CI95 [0.0%, 1.7%] | invariant | underspecified_reask_enabled=False 로 재판정했을 때 True 인 표본 수 | 전 표본(intent 무관 — 판정 함수 게이트 자체를 보는 불변식, F-1) |
| `judgmentAccuracy` 판정 정확도 | 187/201 (93.0%) | CI95 [88.6%, 95.8%] | exploratory | 판정 == expectedReask | recommend 표본(게이트 제외)(F-1) |
| `judgmentAccuracyWithGateSlice` 판정 정확도 | 211/225 (93.8%) | CI95 [89.8%, 96.3%] | exploratory | 판정 == expectedReask | recommend 표본(게이트 포함)(F-1) |
| `missRate` 미탐율 | 14/112 (12.5%) | CI95 [7.6%, 19.9%] | confirmatory-primary | 판정 False | expectedReask=true 앵커의 recommend 표본 — 프로덕션은 intent==recommend 인 턴에서만 is_underspecified_turn 을 호출한다(F-1) |
| `missRateUnderExpansionAssumption` 미탐율(전개 가정 상한) | 63/112 (56.2%) | CI95 [47.0%, 65.1%] | exploratory | (판정 False) 또는 (판정 True ∧ detect_expansion_need 가 사유를 돌려줌) | expectedReask=true 앵커의 recommend 표본(F-1) |
| `missRateWithNonRecommendIntent` 미탐율(비-recommend 포함, 참고용) | 14/112 (12.5%) | CI95 [7.6%, 19.9%] | exploratory | 판정 False | expectedReask=true 앵커의 표본 전부(intent 무관, 포함판) |
| `priorGateInvariant` prior 게이트 불변식 | 0/225 (0.0%) | CI95 [0.0%, 1.7%] | invariant | prior=ProductSearchFilters() 로 재판정했을 때 True 인 표본 수 | 전 표본(intent 무관 — 판정 함수 게이트 자체를 보는 불변식, F-1) |

## 슬라이스별 병기

### `missRate`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_price | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_budget_set | 2/32 (6.2%) (exploratory: N<40) | [1.7%, 20.1%] |
| what_axis | 0/0 (해당 없음) | N/A |
| blocking_rating | 0/0 (해당 없음) | N/A |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `missRateWithNonRecommendIntent`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_price | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_budget_set | 2/32 (6.2%) (exploratory: N<40) | [1.7%, 20.1%] |
| what_axis | 0/0 (해당 없음) | N/A |
| blocking_rating | 0/0 (해당 없음) | N/A |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `falseAlarmRate`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/0 (해당 없음) | N/A |
| constraint_price | 0/0 (해당 없음) | N/A |
| constraint_budget_set | 0/0 (해당 없음) | N/A |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `falseAlarmRateWithGateSlice`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/0 (해당 없음) | N/A |
| constraint_price | 0/0 (해당 없음) | N/A |
| constraint_budget_set | 0/0 (해당 없음) | N/A |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/24 (0.0%) (exploratory: N<40) | [0.0%, 13.8%] |

### `falseAlarmRateWithNonRecommendIntent`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/0 (해당 없음) | N/A |
| constraint_price | 0/0 (해당 없음) | N/A |
| constraint_budget_set | 0/0 (해당 없음) | N/A |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `judgmentAccuracy`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 34/40 (85.0%) | [70.9%, 92.9%] |
| constraint_price | 34/40 (85.0%) | [70.9%, 92.9%] |
| constraint_budget_set | 30/32 (93.8%) (exploratory: N<40) | [79.9%, 98.3%] |
| what_axis | 49/49 (100.0%) | [92.7%, 100.0%] |
| blocking_rating | 40/40 (100.0%) | [91.2%, 100.0%] |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `judgmentAccuracyWithGateSlice`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 34/40 (85.0%) | [70.9%, 92.9%] |
| constraint_price | 34/40 (85.0%) | [70.9%, 92.9%] |
| constraint_budget_set | 30/32 (93.8%) (exploratory: N<40) | [79.9%, 98.3%] |
| what_axis | 49/49 (100.0%) | [92.7%, 100.0%] |
| blocking_rating | 40/40 (100.0%) | [91.2%, 100.0%] |
| multiturn_gate | 24/24 (100.0%) (exploratory: N<40) | [86.2%, 100.0%] |

### `missRateUnderExpansionAssumption`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 38/40 (95.0%) | [83.5%, 98.6%] |
| constraint_price | 6/40 (15.0%) | [7.1%, 29.1%] |
| constraint_budget_set | 19/32 (59.4%) (exploratory: N<40) | [42.3%, 74.5%] |
| what_axis | 0/0 (해당 없음) | N/A |
| blocking_rating | 0/0 (해당 없음) | N/A |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `expansionGateWouldFireRate`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 32/34 (94.1%) (exploratory: N<40) | [80.9%, 98.4%] |
| constraint_price | 0/34 (0.0%) (exploratory: N<40) | [0.0%, 10.2%] |
| constraint_budget_set | 17/30 (56.7%) (exploratory: N<40) | [39.2%, 72.6%] |
| what_axis | 0/0 (해당 없음) | N/A |
| blocking_rating | 0/0 (해당 없음) | N/A |
| multiturn_gate | 0/0 (해당 없음) | N/A |

### `flagOffInvariant`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/40 (0.0%) | [0.0%, 8.8%] |
| constraint_price | 0/40 (0.0%) | [0.0%, 8.8%] |
| constraint_budget_set | 0/32 (0.0%) (exploratory: N<40) | [0.0%, 10.7%] |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/24 (0.0%) (exploratory: N<40) | [0.0%, 13.8%] |

### `priorGateInvariant`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| no_condition | 0/40 (0.0%) | [0.0%, 8.8%] |
| constraint_price | 0/40 (0.0%) | [0.0%, 8.8%] |
| constraint_budget_set | 0/32 (0.0%) (exploratory: N<40) | [0.0%, 10.7%] |
| what_axis | 0/49 (0.0%) | [0.0%, 7.3%] |
| blocking_rating | 0/40 (0.0%) | [0.0%, 8.8%] |
| multiturn_gate | 0/24 (0.0%) (exploratory: N<40) | [0.0%, 13.8%] |

## LLM vs baseline (trivial baseline: 항상 reask=false)

baseline `missRate` = 1.0 (구조적 — 항상 False 를 내므로 expectedReask=true 앵커를 전부 놓친다). baseline `falseAlarmRate` = 0.0 (정의상 — 항상 False 이므로 오탐이 성립하지 않는다).

| | LLM judgmentAccuracy | baseline judgmentAccuracy |
|---|---|---|
| **전체(게이트 제외)** | 93.0% | 44.3% |
| no_condition | 85.0% | 0.0% |
| constraint_price | 85.0% | 0.0% |
| constraint_budget_set | 93.8% (exploratory: N<40) | 0.0% |
| what_axis | 100.0% | 100.0% |
| blocking_rating | 100.0% | 100.0% |
| multiturn_gate | N/A (해당 없음) | 100.0% |

전체 기준으로 LLM 판정이 trivial baseline 을 앞선다 — 단일 런이라 확정은 아니다.

## 원인 축 분해 (이슈 완료 조건 3)

outcome 분포: {'correctReject': 113, 'hit': 98, 'miss': 14}

### 미탐 원인 축(ablation 이 뒤집은 축 — 복수 가능, `multiple` = 단일 축으로 안 뒤집힘)

| 축 | 미탐 표본 수 |
|---|---|
| `multiple` | 13 |
| `semanticQueryIsFallback` | 1 |

### 미탐 `blockingAxes` 조합별 분포 (F-2 — 실측 조합, 서술 아님)

`multiple` 로 뭉뚱그리면 실제로 어떤 축들이 함께 막았는지가 감춰진다 — 이 표는 미탐
표본의 `blockingAxes`(비어 있지 않은 차단 축 전부) 조합을 그대로 집계한다.

| blockingAxes 조합 | 미탐 표본 수 |
|---|---|
| `categoryQueries;semanticQueryIsFallback` | 13 |
| `semanticQueryIsFallback` | 1 |

### 오탐 원인 축(앵커 referenceAxes — 채워졌어야 할 축)

(오탐 없음)

표본별 상세(`verdict`·`expectedReask`·`outcome`·`causeAxes`·`blockingAxes`)는 `samples.csv` 에 실려 있다 — 런 재실행 없이 재집계할 수 있다.

## 불변 재판정 (§D9, LLM 콜 0)

- `flagOffInvariant`: 0/225 (0.0%) (0/225 이어야 한다)
- `priorGateInvariant`: 0/225 (0.0%) (0/225 이어야 한다)

## 진단 (합불 아님)

- `categoryEchoWithoutQueriesCount`: 0 — filters.category 가 비어 있지 않은데 categoryQueries 는 빈 표본 수 — §D10 항목 2(프로덕션이 필터를 덮어쓰는 괴리)의 노출 크기
- `nonRecommendIntentCount`(앵커별·intent별, F-1): {} — intent != 'recommend' 표본 수(앵커별·intent별) — 프로덕션은 이 표본에서 is_underspecified_turn 에 도달하지 않는다(F-1). confirmatory 축 분모에서 제외된 표본의 노출 크기이며, 그 실패는 intent 라우팅 축(evals/intent_probe)의 소관이다.
- `expansionGateWouldFireRate`: 49/98 (50.0%)
- `missRateUnderExpansionAssumption`: 63/112 (56.2%)

## 셀별 표본 수

| 셀 | 슬라이스 | 표본 | 시도 |
|---|---|---|---|
| `buy-under-0001` | constraint_budget_set | 8 | 8 |
| `buy-under-0002` | no_condition | 8 | 8 |
| `buy-under-0003` | constraint_price | 8 | 8 |
| `buy-under-0004` | what_axis | 8 | 8 |
| `buy-under-0005` | what_axis | 8 | 8 |
| `buy-under-0006` | multiturn_gate | 8 | 8 |
| `buy-under-0007` | blocking_rating | 8 | 8 |
| `under-br-0001` | blocking_rating | 8 | 8 |
| `under-br-0002` | blocking_rating | 8 | 8 |
| `under-br-0003` | blocking_rating | 8 | 8 |
| `under-br-0004` | blocking_rating | 8 | 8 |
| `under-cbs-0001` | constraint_budget_set | 8 | 8 |
| `under-cbs-0002` | constraint_budget_set | 8 | 8 |
| `under-cbs-0003` | constraint_budget_set | 8 | 8 |
| `under-cp-0001` | constraint_price | 8 | 8 |
| `under-cp-0002` | constraint_price | 8 | 8 |
| `under-cp-0003` | constraint_price | 8 | 8 |
| `under-cp-0004` | constraint_price | 8 | 8 |
| `under-mg-0001` | multiturn_gate | 8 | 8 |
| `under-mg-0002` | multiturn_gate | 8 | 8 |
| `under-nc-0001` | no_condition | 8 | 8 |
| `under-nc-0002` | no_condition | 8 | 8 |
| `under-nc-0003` | no_condition | 8 | 8 |
| `under-nc-0004` | no_condition | 8 | 8 |
| `under-wa-0001` | what_axis | 8 | 8 |
| `under-wa-0002` | what_axis | 8 | 8 |
| `under-wa-0003` | what_axis | 7 | 24 |
| `under-wa-0004` | what_axis | 6 | 24 |
| `under-wa-0005` | what_axis | 4 | 24 |
| `under-wa-0006` | what_axis | 0 | 24 |

## 채우지 못한 셀

| 셀 | 채움/목표 | 시도 | 오류 |
|---|---|---|---|
| `under-wa-0003` | 7/8 | 24 | LLMError |
| `under-wa-0004` | 6/8 | 24 | LLMError |
| `under-wa-0005` | 4/8 | 24 | LLMError |
| `under-wa-0006` | 0/8 | 24 | LLMError |

## 측정 범위와 한계 (요약 — 전문은 README §측정 범위와 한계)

1. `category_legs` 는 이 하네스에서 항상 빈다(카테고리 매핑을 부르지 않는다).
2. `filters.category` 는 decompose 의 `filters` JSON 스키마에 키가 없어 구조적으로 항상 빈다 — `categoryEchoWithoutQueriesCount` 가 그 사실을 실측한다.
3. `_prepare_recommendation` 의 카테고리 매핑과 전개 LLM 생성(`needs_expansion` #217 의 전개 호출)은 부르지 않는다 — **다만 게이트 판정 함수 `detect_expansion_need` 자체는 진단 목적으로 매 표본 부른다**(F-3, `expansionGateWouldFireRate`·`missRateUnderExpansionAssumption` 의 근거).
4. 프로덕션은 `intent==recommend` 인 턴에서만 판정을 호출한다 — confirmatory 축은 recommend 표본으로 좁힌다(F-1). 비-recommend 표본은 `nonRecommendIntentCount` 로만 드러나며, 그 실패는 intent 라우팅 축(`evals/intent_probe`)의 소관이다.
5. 단일 턴만 잰다(컨텍스트 행렬 없음).

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.
3. 단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.
4. 이건 골든셋이 아니다 — decompose 직후·판정 직전의 형상만 잰다. #372 의 되물음 답변 턴 결정론 fixture 실측과 숫자를 섞지 말 것.

페이싱 실측: 대기 121회 / 허용 45 rpm.
