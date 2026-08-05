# 기준선 — fast-2026-08-06

첫 실 LLM 실측(#332 acceptance run). 리뷰 라운드 1(F-1~F-5)·라운드 2(R2-1~R2-5) 수정을 모두
반영한 뒤 **재실행**한 표다 — 이전 두 판(legCoverage 가 promptExample 을 포함해 계산되던 표,
gift-budget/school-budget pairKind 오라벨, 매칭 귀속이 문서 우선순위와 다르던 표, 직렬화
axisId 가 제외판·포함판에서 충돌하던 표)은 이 표로 교체됐다.
`uv run python -m evals.legs_probe --out evals/legs_probe/baselines/fast-2026-08-06 --tier fast`
(기본 N=8, 종료 코드 0).

## 헤더

`prompt=11c6fe3bfa0c (repo:_SYSTEM)` · `tier=fast` · `model=gpt-5-nano` ·
`fixture=legs-anchors-v1` · `N=8` · `categoryFanoutMax=5`. 312콜 · 페이싱 대기 96회(45rpm) ·
못 채운 셀 0.

**[R2-4] 비용·토큰은 부분합이다.** `results.json.budget` 은
`unknownCostCallCount=35`·`unknownTokenCallCount=35`(`costGateStatus`/`tokenGateStatus`
둘 다 `"unknown"`) — 312콜 중 35콜은 provider 가 usage 를 보고하지 않아 집계에서 빠졌다.
**관측된 부분합은 $0.0711 · 1,043,413 tokens(277콜 기준)** 이며, 이 숫자를 "이 런의 총 비용"
으로 인용하면 안 된다(실제 총 비용은 이보다 크다 — 정확히 얼마인지는 이 하네스가 모른다).

## Primary confirmatory 지표

> ⚠️ **[R4-1] 아래 94.0% 는 decompose 단계(2단계 전개 파이프라인 중 1단계, needs_expansion
> #217 보정 전) 형상의 측정이다 — 사용자 체감 실패율이 아니다.** 프로덕션 전개는 decompose
> 뒤에 needs_expansion 이 매핑 실패 leg 를 보정해 더한다(§0). 이 표 하나만으로 "사용자가
> 실제로 겪는 전개 실패율이 94%" 라고 읽으면 오독이다 — 상세는 아래 §「측정 범위와 한계」
> (하네스 README) 참조.

`case3UnderExpansionRate`(promptExample 5건 제외) = **142/151 (94.0%)**, CI95 [89.1%, 96.8%].

슬라이스별(사전 등록):

| 슬라이스 | 점수 | CI95 |
|---|---|---|
| situational | 66/67 (98.5%) | [92.0%, 99.7%] |
| purpose | 50/50 (100.0%) | [92.9%, 100.0%] |

**해석**: `fast`(gpt-5-nano) 는 상황·목적형 발화를 "case 3"로는 잘 인지하지만(슬라이스별
`caseAccuracy` situational 97.5%·purpose 91.5%), 인지 이후 **실제 leg 전개**는 거의 하지
않는다 — 발화 원문을 그대로 leg 하나로 되돌리거나(예: `"감자탕 재료 추천"` → leg
`"감자탕 재료"`) legs=0 을 낸다(진단 "case==3 ∧ legs==0" 35건). `needs_expansion.py`
docstring 이 이미 문서화한 실패 모양("`decompose` 프롬프트에 전개를 맡기는 방식은 실측
39회에서 1~2/3 확률로만 성립")과 정확히 같은 패턴이며, 이 런은 그 관측을 **39 앵커 × N=8
고정 데이터셋**으로 정량화한다.

## 축 전체

| 축 | 점수 | CI95 | 성격 |
|---|---|---|---|
| `caseAccuracy` | 217/304 (71.4%) | [66.1%, 76.2%] | exploratory |
| `case3UnderExpansionRate` | 142/151 (94.0%) | [89.1%, 96.8%] | confirmatory-primary |
| `case3UnderExpansionRateWithPromptExamples` | 167/186 (89.8%) | [84.6%, 93.4%] | exploratory |
| `expectedCase3UnderExpansion` | 159/192 (82.8%) | [76.8%, 87.5%] | exploratory |
| `legCoverage`(promptExample 제외) | 85/229 (37.1%) | [31.1%, 43.5%] | confirmatory-secondary |
| `legCoverageWithPromptExamples` | 93.17/264 (35.3%) | [29.8%, 41.2%] | exploratory |
| `legsInRangeRate` | 134/304 (44.1%) | [38.6%, 49.7%] | exploratory |
| `overExpansionRate` | 144/284 (50.7%) | [44.9%, 56.5%] | exploratory |
| `buyAllAccuracy` | 187/284 (65.8%) | [60.2%, 71.1%] | exploratory |
| `totalBudgetAccuracy` | 300/304 (98.7%) | [96.7%, 99.5%] | exploratory |

[R2-3] 직렬화 axisId 가 이제 map key 와 정확히 일치한다(`case3UnderExpansionRate` vs
`case3UnderExpansionRateWithPromptExamples`, `legCoverage` vs
`legCoverageWithPromptExamples`) — 이전 판은 포함판 아래에도 내부 axisId 가 제외판 이름으로
저장돼 두 표를 소비자가 구분할 수 없었다. 분모 정의 문자열에도 `(promptExample 제외)`/
`(promptExample 포함)` 이 명시된다.

전체 정의는 `../../README.md` §「축과 정의」참조 — 분자·분모 문장은 `results.json.axes[*].definition`
에도 그대로 실린다.

## 슬라이스 표

`caseAccuracy` 는 슬라이스마다 극단적으로 갈린다 — `single`(case1 기대) 22.2%, `multi`(복수
상품) 52.5% 로 낮고 `conditions`(case2) 90.0%, `situational`/`purpose`(case3) 97~92% 로
높다. `legCoverage`(promptExample 제외)는 `situational`·`purpose` 둘 다 **0.0%** — leg 이
나와도 앵커가 정의한 니즈 그룹을 하나도 맞히지 못했다. [R2-2] 이 숫자는 매칭 귀속을 2-pass로
고친 뒤의 값이다 — 이전 판은 그룹 단위(첫 매치에서 종료)로 규칙 1~3을 다 시도해, 앞 그룹이
raw_category 로 걸리면 뒤 그룹이 query(head-token)로 더 정확히 맞아도 앞 그룹으로 잘못
귀속될 수 있었다.

## LLM vs baseline (trivial baseline: 항상 leg 1개)

| 슬라이스 | LLM legCoverage(promptExample 제외) | baseline legCoverage(promptExample 제외) |
|---|---|---|
| **전체** | 37.1% | 37.4% |
| single | 84.7% | 100.0% |
| conditions | N/A | N/A |
| situational | 0.0% | 0.0% |
| purpose | 0.0% | 0.0% |
| multi | 75.0% | 45.8% |

[R4-2] **전체 기준으로 LLM 이 trivial baseline 을 넘지 못한다**(37.1% < 37.4%) — 슬라이스만
보면 `single`·`multi` 에서 LLM 이 이겨 놓쳤을 사실이다(#275 가 랭킹 축에서 밟은 것과 같은
과소 보고 모양: 슬라이스·부분 표만으로는 "전체적으로 아무것도 안 하는 것보다 못하다"는
결론이 표에서 빠진다). `legCoverage`(축 전체, results.json 의 confirmatory-secondary 값)
대 `baseline.legCoverageOverall` 대조이며 WithPromptExamples 판은 쓰지 않았다(정의가 다른
분모를 대조하면 #234/#240 사고가 재발한다).

baseline `legCoverage` 는 저자 라벨(`baselineGroupsHit`) 기반이다 — 발화 원문 하나를 그대로
검색어로 썼을 때 몇 개 그룹을 짚는지의 근사치이며, LLM 호출 없이 결정론으로 계산된다. baseline
도 `legCoverage`(confirmatory-secondary)와 같은 규칙으로 promptExample 을 제외해 대조가
사과-사과다. **situational·purpose 는 LLM 과 baseline 이 완전히 같다(둘 다 0.0%)** — 이 두
슬라이스에서는 fast 티어가 "아무것도 안 하는 것"과 구별되지 않는다. `single`·`multi` 는 LLM
이 여전히 baseline 보다 앞서지만, **전체 가중 평균으로는 그 우위가 situational·purpose 의
완전 무승부에 묻혀 baseline 을 못 넘는다.** baseline `case3UnderExpansionRate` = 1.0(구조적) ·
`overExpansionRate` = 0.0(정의상).

## pair 진단 (exploratory, 합불 아님)

| pairId | pairKind | caseId | 평균 legs | 평균 legCoverage |
|---|---|---|---|---|
| camp-budget | DIR-budget | `legs-situ-0003` | 1.00 | 0.0% |
| camp-paraphrase | INV-paraphrase | `legs-situ-0001` | 1.00 | 0.0% |
| camp-paraphrase | INV-paraphrase | `legs-situ-0002` | 0.88 | 0.0% |
| gift-budget | DIR-budget | `legs-purp-0003` | 0.50 | 0.0% |
| gift-budget | DIR-budget | `legs-purp-0004` | 1.00 | 0.0% |
| school-budget | DIR-budget | `legs-purp-0005` | 0.88 | 0.0% |
| school-budget | DIR-budget | `legs-purp-0006` | 0.38 | 0.0% |

[R2-5] 방향성 해석(표와 일치하도록 다시 씀):

- **gift pair**(무예산 `legs-purp-0003` 0.50 → 유예산 `legs-purp-0004` 1.00): legs 가
  **늘었다** — "예산 추가가 legs 를 줄이면 안 된다"는 방향을 위반하지 않았다.
- **school pair**(무예산 `legs-purp-0005` 0.88 → 유예산 `legs-purp-0006` 0.38): legs 가
  **줄었다** — 방향 위반 관측이다. 예산 조건("10만원으로 전부 챙겨줘")이 추가되면서 전개가
  오히려 더 위축된 모양이다.
- **camp-budget**(무예산 `legs-situ-0001` 1.00 → 유예산 `legs-situ-0003` 1.00): 변화 없음.

세 쌍 다 legs 값 자체가 1개 안팎으로 수렴해 있어(전개가 애초에 거의 안 됨) 신호가 약하고,
**단일 실행·표본 8개**라 이 방향 관측을 확정으로 읽으면 안 된다 — 특히 school pair 의
"감소"는 독립 재실행으로 재현되는지 확인 전에는 우연일 수 있다.

## promptExample 앵커 (confirmatory 집계에서 제외)

| caseId | 슬라이스 | recommend 표본 | case==3 ∧ legs<=1 | 평균 legCoverage |
|---|---|---|---|---|
| `legs-mult-0001` | multi | 8 | 2 | 75.0% |
| `legs-purp-0001` | purpose | 8 | 7 | 0.0% |
| `legs-purp-0002` | purpose | 7 | 7 | 0.0% |
| `legs-situ-0009` | situational | 4 | 4 | 12.5% |
| `legs-situ-0010` | situational | 8 | 5 | 20.8% |

`legs-purp-0001`(_SYSTEM 이 case 예문으로 직접 든 발화)는 7/8 이 과소전개다 — 프롬프트가 이
문장을 예시로 봤다고 해서 실제 전개 성공률이 올라가지는 않는다는 뜻이다.

## 진단 (합불 아님)

- intent!=recommend 표본(앵커별): `legs-purp-0002` 1건 · `legs-situ-0005` 1건 ·
  `legs-situ-0006` 1건 · `legs-situ-0008` 1건 · `legs-situ-0009` 4건.
- 발화 에코 leg(과전개 중 발화 복사): 68건.
- case==3 ∧ legs==0: 35건.

## R2-1 검증(예산 소진 시나리오)

이 런은 예산을 소진하지 않았다(`budget.budgetExceeded=false`). §12 마무리 절차가 요구한
"진행 중 예산 소진 시 셀 39개 전체가 unfilledCells 에 정확히 나오는지"는 실측 대신
`tests/unit/test_legs_probe_runner.py` 의 합성 시나리오(공유 카운터로 정확히 N회만 성공을
허용하는 가짜 LLM)로 검증했다 — 실 런에서 예산을 일부러 소진시키려면 `--budget-usd` 를
아주 작게 줘야 하는데, 그러면 이 기준선 자체가 무의미해진다.

## ⚠️ 단일 실행은 채택 판정이 아니다

이 런은 **1회 실행**이다. `evals/intent_probe` 의 재현 함정 4가 그대로 적용된다 — 같은
프롬프트 해시의 독립 실행에서도 축당 편차가 있을 수 있다(세 판 다 primary 값이 92.7% →
93.9% → 94.0% 로 좁은 범위 안에서 흔들렸다 — 같은 코드, 다른 API 호출 시드). 이 표를
근거로 "gpt-5-nano 는 니즈 전개를 못 한다"를 확정하려면 **독립 2~3회** 분포를 봐야 한다.
이 런의 목적은 하네스 acceptance(§7)와, #198 이 로그로만 관측하던 현상을 처음으로 고정
데이터셋 위에서 수치화하는 것이다 — 프롬프트 개선·티어 비교 판단의 근거는 후속 런의 몫이다.
