# 니즈 전개(legs) 평가 하네스 리포트

prompt=11c6fe3bfa0c (repo:_SYSTEM) · tier=fast · model=gpt-5-nano · fixture=legs-anchors-v1 · N=8 · categoryFanoutMax=5

> **이건 골든셋이 아니다.** 추천 품질이 아니라 decompose 단계의 case·legs 산출 분포를 잰 표다. e2e 유효성 최종 판정은 caseId 척추(골든셋 연결)의 몫이다.

## Primary confirmatory 지표

> ⚠️ decompose 단계(2단계 전개 파이프라인 중 **1단계, needs_expansion #217 보정 전**) 형상의 측정이다 — 사용자 체감 실패율이 아니다.

`case3UnderExpansionRate` (promptExample 제외): 142/151 (94.0%)
CI95 [89.1%, 96.8%]

슬라이스별(confirmatory 사전 등록: situational · purpose):

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| purpose | 50/50 (100.0%) | [92.9%, 100.0%] |
| situational | 66/67 (98.5%) | [92.0%, 99.7%] |

## 축 전체

| 축 | 점수 | CI95 | 성격 | 분자 정의 | 분모 정의 |
|---|---|---|---|---|---|
| `buyAllAccuracy` buyAll 정확도 | 187/284 (65.8%) | CI95 [60.2%, 71.1%] | exploratory | 산출 buy_all == expected.buyAll | expected.buyAll≠null 앵커의 recommend 표본 |
| `case3UnderExpansionRate` case3 과소전개율 | 142/151 (94.0%) | CI95 [89.1%, 96.8%] | confirmatory-primary | 산출 case==3 ∧ len(legs)<=1 | 산출 case==3 인 recommend 표본(promptExample 제외) |
| `case3UnderExpansionRateWithPromptExamples` case3 과소전개율 | 167/186 (89.8%) | CI95 [84.6%, 93.4%] | exploratory | 산출 case==3 ∧ len(legs)<=1 | 산출 case==3 인 recommend 표본(promptExample 포함) |
| `caseAccuracy` case 판정 정확도 | 217/304 (71.4%) | CI95 [66.1%, 76.2%] | exploratory | 산출 case == expected.case | recommend 표본 전부 |
| `expectedCase3UnderExpansion` 기대 case3 과소전개율(보조 시야) | 159/192 (82.8%) | CI95 [76.8%, 87.5%] | exploratory | len(legs)<=1 | expected.case==3 앵커의 recommend 표본 |
| `legCoverage` 니즈 커버리지 | 85/229 (37.1%) | CI95 [31.1%, 43.5%] | confirmatory-secondary | Σ min(1, 커버리지그룹 distinct 매칭 수 / coverageTarget) | coverageGroups 있는 앵커의 recommend 표본 수(promptExample 제외) |
| `legCoverageWithPromptExamples` 니즈 커버리지 | 93.17/264 (35.3%) | CI95 [29.8%, 41.2%] | exploratory | Σ min(1, 커버리지그룹 distinct 매칭 수 / coverageTarget) | coverageGroups 있는 앵커의 recommend 표본 수(promptExample 포함) |
| `legsInRangeRate` leg 수 범위 준수율 | 134/304 (44.1%) | CI95 [38.6%, 49.7%] | exploratory | legsMin<=len(legs)<=legsMax | recommend 표본 전부 |
| `overExpansionRate` 과전개율 | 144/284 (50.7%) | CI95 [44.9%, 56.5%] | exploratory | 커버리지∪acceptable 어느 그룹에도 안 맞는 leg 수 | 산출 leg 총수(그룹 있는 앵커) |
| `totalBudgetAccuracy` totalBudget 정확도 | 300/304 (98.7%) | CI95 [96.7%, 99.5%] | exploratory | 산출 total_budget == expected.totalBudget(null 포함 정확 일치) | recommend 표본 전부 |

## 슬라이스별 병기

### `case3UnderExpansionRate`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| situational | 66/67 (98.5%) | [92.0%, 99.7%] |
| purpose | 50/50 (100.0%) | [92.9%, 100.0%] |

### `caseAccuracy`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| single | 16/72 (22.2%) | [14.2%, 33.1%] |
| conditions | 36/40 (90.0%) | [76.9%, 96.0%] |
| situational | 79/81 (97.5%) | [91.4%, 99.3%] |
| purpose | 65/71 (91.5%) | [82.8%, 96.1%] |
| multi | 21/40 (52.5%) | [37.5%, 67.1%] |

### `legCoverage`

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| single | 61/72 (84.7%) | [74.7%, 91.2%] |
| situational | 0/69 (0.0%) | [0.0%, 5.3%] |
| purpose | 0/56 (0.0%) | [0.0%, 6.4%] |
| multi | 24/32 (75.0%) (exploratory: N<40) | [57.9%, 86.7%] |

## LLM vs baseline (trivial baseline: 항상 leg 1개)

baseline `legCoverage` 는 저자 라벨(`baselineGroupsHit`) 기반이다 — 발화 원문 하나를 그대로 검색어로 썼을 때 몇 개 그룹을 짚는지의 근사치이며, LLM 호출 없이 결정론으로 계산된다.

baseline `case3UnderExpansionRate` = 1.0 (구조적 — leg 이 항상 1개이므로 정의상 1.0). baseline `overExpansionRate` = 0.0 (정의상 0).

| 슬라이스 | LLM legCoverage | baseline legCoverage |
|---|---|---|
| **전체** | 37.1% | 37.4% |
| single | 84.7% | 100.0% |
| conditions | N/A | N/A |
| situational | 0.0% | 0.0% |
| purpose | 0.0% | 0.0% |
| multi | 75.0% (exploratory: N<40) | 45.8% |

**전체 기준으로 LLM 이 trivial baseline 을 넘지 못한다** — 단일 런이라 확정은 아니다(위 「단일 실행은 채택 판정이 아니다」 참조), 하지만 #275 가 랭킹에서 밟은 같은 형태의 결과다: 임의 순서 기준선 대조 없이는 몰랐을 사실이다.

## pair 진단 (exploratory, 합불 아님)

| pairId | pairKind | caseId | 표본 | 평균 legs | 평균 legCoverage |
|---|---|---|---|---|---|
| camp-budget | DIR-budget | `legs-situ-0003` | 8 | 1.00 | 0.0% |
| camp-paraphrase | INV-paraphrase | `legs-situ-0001` | 8 | 1.00 | 0.0% |
| camp-paraphrase | INV-paraphrase | `legs-situ-0002` | 8 | 0.88 | 0.0% |
| gift-budget | DIR-budget | `legs-purp-0003` | 8 | 0.50 | 0.0% |
| gift-budget | DIR-budget | `legs-purp-0004` | 8 | 1.00 | 0.0% |
| school-budget | DIR-budget | `legs-purp-0005` | 8 | 0.88 | 0.0% |
| school-budget | DIR-budget | `legs-purp-0006` | 8 | 0.38 | 0.0% |

## promptExample 앵커 (confirmatory 집계에서 제외, 별도 exploratory)

| caseId | 슬라이스 | recommend 표본 | case==3 ∧ legs<=1 | 평균 legCoverage |
|---|---|---|---|---|
| `legs-mult-0001` | multi | 8 | 2 | 75.0% |
| `legs-purp-0001` | purpose | 8 | 7 | 0.0% |
| `legs-purp-0002` | purpose | 7 | 7 | 0.0% |
| `legs-situ-0009` | situational | 4 | 4 | 12.5% |
| `legs-situ-0010` | situational | 8 | 5 | 20.8% |

## 진단 (합불 아님)

- intent!=recommend 표본(앵커별): {'legs-purp-0002': 1, 'legs-situ-0005': 1, 'legs-situ-0006': 1, 'legs-situ-0008': 1, 'legs-situ-0009': 4}
- 발화 에코 leg(과전개 중 발화 복사): 68
- case==3 ∧ legs==0: 35

## 셀별 intent 분포

| 셀 | 슬라이스 | 표본 | 시도 | intent 분포 |
|---|---|---|---|---|
| `buy-cold-0002` | single | 8 | 8 | recommend 8 |
| `buy-fail-0003` | single | 8 | 8 | recommend 8 |
| `buy-gust-0001` | single | 8 | 8 | recommend 8 |
| `buy-mult-0002` | single | 8 | 8 | recommend 8 |
| `buy-srch-0001` | situational | 8 | 8 | recommend 8 |
| `buy-srch-0002` | single | 8 | 8 | recommend 8 |
| `buy-srch-0003` | single | 8 | 8 | recommend 8 |
| `buy-srch-0004` | single | 8 | 8 | recommend 8 |
| `legs-cond-0001` | conditions | 8 | 8 | recommend 8 |
| `legs-cond-0002` | conditions | 8 | 8 | recommend 8 |
| `legs-cond-0003` | conditions | 8 | 8 | recommend 8 |
| `legs-cond-0004` | conditions | 8 | 8 | recommend 8 |
| `legs-cond-0005` | conditions | 8 | 8 | recommend 8 |
| `legs-mult-0001` | multi | 8 | 8 | recommend 8 |
| `legs-mult-0002` | multi | 8 | 8 | recommend 8 |
| `legs-mult-0003` | multi | 8 | 8 | recommend 8 |
| `legs-mult-0004` | multi | 8 | 8 | recommend 8 |
| `legs-mult-0005` | multi | 8 | 8 | recommend 8 |
| `legs-purp-0001` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0002` | purpose | 8 | 8 | general 1, recommend 7 |
| `legs-purp-0003` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0004` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0005` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0006` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0007` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0008` | purpose | 8 | 8 | recommend 8 |
| `legs-purp-0009` | purpose | 8 | 8 | recommend 8 |
| `legs-single-0001` | single | 8 | 8 | recommend 8 |
| `legs-single-0002` | single | 8 | 8 | recommend 8 |
| `legs-situ-0001` | situational | 8 | 8 | recommend 8 |
| `legs-situ-0002` | situational | 8 | 8 | recommend 8 |
| `legs-situ-0003` | situational | 8 | 8 | recommend 8 |
| `legs-situ-0004` | situational | 8 | 8 | recommend 8 |
| `legs-situ-0005` | situational | 8 | 8 | general 1, recommend 7 |
| `legs-situ-0006` | situational | 8 | 8 | general 1, recommend 7 |
| `legs-situ-0007` | situational | 8 | 8 | recommend 8 |
| `legs-situ-0008` | situational | 8 | 8 | general 1, recommend 7 |
| `legs-situ-0009` | situational | 8 | 8 | general 4, recommend 4 |
| `legs-situ-0010` | situational | 8 | 8 | recommend 8 |

## 채우지 못한 셀

(없음)

## 재현 함정

1. 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.
2. 실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.
3. 단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.
4. 이건 골든셋이 아니다 — decompose 단계 산출 분포이며, 2단계 needs_expansion 은 부르지 않는다.

페이싱 실측: 대기 96회 / 허용 45 rpm.
