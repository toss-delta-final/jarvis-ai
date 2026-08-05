# 니즈 전개(legs) 평가 하네스 (#332)

`decompose` 가 **상황·목적형 발화를 몇 개의 구체 상품(leg)으로 전개하는지**를 실 LLM 반복
분포로 잰다. #198 의 핵심 지표("case==3 인데 legs<=1" — 전개가 필요하다고 인지했으면서도
전개에 실패한 턴)를 로그 관측(`decompose_case`)에서 고정 데이터셋(앵커 39건 × N=8)으로 옮긴다.
**[R4-1] 이 하네스가 재는 것은 2단계 전개 파이프라인 중 decompose(1단계) 직후, `needs_expansion`
(#217) 보정 전의 형상이다** — 아래 primary 지표를 "사용자 체감 전개 실패율"로 곧바로 읽으면
오독이다(상세는 §「측정 범위와 한계」).

## 무엇을 재는가

발화 1건 → `decompose` 1회 호출 → 산출된 `case`·`categoryQueries`(legs)·`buyAll`·`totalBudget`·
`intent` 를 정답지와 대조한다. 셀 = 앵커 1개(컨텍스트 행렬 없음 — `evals/intent_probe` 와 다른
점). 39 앵커 × N=8 = 312콜.

## 측정 범위와 한계

- **decompose 단계만 잰다.** 프로덕션 전개는 2단계다 — decompose 뒤에
  `needs_expansion`(#217, 매핑 실패 게이트)이 leg 를 **더한다**. 이 하네스 v1 은 그 2단계를
  부르지 않는다: `needs_expansion` 의 발동은 `category_mapping` 결과(카테고리 사전 매핑
  성공/실패)에 의존하는데, 그 축은 #331(카테고리 매핑·선택 하네스)의 몫이라 여기서 함께
  재면 두 이슈의 실패 원인이 뒤섞인다. **union 커버리지**(decompose leg ∪ needs_expansion leg
  의 합산 커버리지) 측정은 후속(follow-up) 이슈로 남긴다.
- **단일 턴만 잰다.** `intent_probe` 처럼 컨텍스트 행렬(`PRIOR_FILTERS`·`LAST_RECOMMENDATIONS`·
  `PENDING_CART`·`SCREEN`)을 조합하지 않는다 — decompose 호출 조건은 고정이다:
  `prior_filters=None`·`profile_summary=None`·`last_recommendations=None`·
  `pending_cart=None`·`screen=None`. 멀티턴에서 전개가 어떻게 달라지는지는 이 하네스의 범위
  밖이다.
- **coverage 매칭 규칙과 그 한계.** leg(`CategoryQuery`, 필드 `query`·`raw_category`)가
  coverage/acceptable 그룹의 synonym 과 매칭되는지는 아래 세 규칙을 순서대로 시도한다.
  1. synonym 에 공백이 **있으면**: 정규화한 synonym 이 정규화한 `query` 전체의 부분 문자열이면
     매칭.
  2. synonym 에 공백이 **없으면**: (a) 공백 제거한 synonym == 공백 제거한 `query` 전체, 또는
     (b) synonym 이 `query` 의 **마지막 공백 토큰(head token)** 의 부분 문자열이면 매칭.
  3. 1·2 로 안 잡히면 `raw_category` 에 대해 단순 부분 문자열(정규화 후)로 보조 매칭.

  head-token 규칙의 이유: 한국어 명사구의 head 는 마지막 토큰이다. 단순 부분 문자열이면
  `"이어폰"` synonym 이 `"이어폰 케이스"`(다른 상품) leg 를 커버로 오인하고, 발화 에코 leg
  (`"감자탕 재료"`)가 `"감자"` 그룹을 커버한 것으로 오인한다(lessons 2026-08-02 「부분 문자열
  매칭은 포함 방향마다 의미가 다르다」). **한계**: 이 규칙은 정확히 이 39 앵커의 synonym 표기
  (대부분 2글자 이상 명사)를 겨냥해 만들어졌다 — 다른 어휘 집합(예: 접두사·접미사가 많은
  카테고리)에 그대로 적용하면 오탐할 수 있다. 판정 근거(leg 원문·매칭 그룹)는 전부
  `samples.csv` 에 남아 **런 재실행 없이 재집계**할 수 있다.
- **baseline 커버리지가 저자 라벨 기반인 이유.** trivial baseline("항상 leg 1개")은 LLM 을
  부르지 않으므로 "그 1개 leg 이 실제로 몇 개 그룹을 커버하는가"를 코드가 계산할 수 없다 —
  기준 leg 이 무엇인지 자체가 정의돼 있지 않기 때문이다(baseline 정책은 "leg 을 안 만든다"가
  아니라 "발화 원문을 그대로 검색어로 쓴다"는 정책의 근사다). 그래서 앵커 저자가 직접 라벨링한
  `baselineGroupsHit`(0 또는 1 — "발화 원문 하나를 그대로 검색어로 쓰면 커버리지 그룹을 몇 개
  짚는가")로 대신한다. 이 값은 주관적 저자 판단이라 LLM 실측만큼 엄밀하지 않다 — trivial
  baseline 대조표는 "개선의 방향"을 보여줄 뿐, 정밀한 하한선은 아니다.
- **모든 비율에 Wilson 95% CI 를 병기한다** — `legCoverage` 처럼 numerator 가 정수 성공
  횟수가 아니라 [0,1] 로 유계인 값들의 합인 축에도 같은 공식을 적용한다. 엄밀히는 이항분포가
  아니라 근사다(`metrics.wilson_ci` docstring 참조).

## 이건 골든셋이 아니다(숫자 섞지 말 것)

| | `evals/goldenset` | `evals/intent_probe` | 이 하네스 |
|---|---|---|---|
| 본체 | 추천 품질 | intent 라우팅 안정성 | **decompose case·legs 산출 분포** |
| 평가 | 결정론 1회 | 확률 분포(컨텍스트 행렬) | 확률 분포(**단일 턴**) |
| 세션 상태 | 없음 | 멀티턴 | **없음**(고정 단일 턴) |

`caseId` 척추: `buy-*` 앵커 8건은 `evals/goldenset/cases/buyer_dev.jsonl`(v1)과 발화가
**글자 그대로 같다**(스키마 검증자가 강제) — e2e 실패를 단계로 귀속하기 위한 연결점이다.
**#333 이 골든셋 v2 를 작업 중**이므로 이 하네스는 v1 만 읽는다. v2 머지 후 caseId 연결 갱신은
후속 커밋이다.

## 실행법

```bash
# 오늘의 기준선(fast 티어)
uv run python -m evals.legs_probe --out artifacts/legs-run1 --tier fast

# API 없이 배관만 확인(가짜 LLM)
uv run python -m evals.legs_probe --out /tmp/probe --dry-run

# 후보 decompose 프롬프트 재기(intent_probe.client 재사용 — 리포를 더럽히지 않고 파일로 갈아끼운다)
uv run python -m evals.legs_probe --out artifacts/cand1 --prompt cand1.txt
uv run python -m evals.legs_probe --out artifacts/base --prompt-rev 3f1dec7

# 외부 앵커(해시 대조 생략, 산출물에 해시는 남는다)
uv run python -m evals.legs_probe --out artifacts/ext --fixture /path/to/anchors.json
```

기본 규모: 39셀 × N=8(`--n`) = 312콜, 45rpm 페이서라 런당 약 8~9분, `fast` 기준 대략
USD 0.05~0.10. [R2-4] `results.json.budget.totalCostUsd`/`totalTokens` 는 provider 가
usage 를 보고한 콜만의 **부분합**이다 — `unknownCostCallCount`/`unknownTokenCallCount` 가
0 이 아니면 그만큼 누락된 관측이라 총량으로 인용하지 말 것(`costGateStatus`/`tokenGateStatus`
가 `"unknown"` 이면 그 표시다).

## 축과 정의

정의는 `metrics.py` 의 각 `axis_*` 함수가 만드는 `AxisResult` 에 데이터로 있고 **산출물에
그대로 실린다**(`results.json.axes[*].definition`, `report.md` 축 표).

| axisId | 분자 | 분모 | 성격 |
|---|---|---|---|
| `caseAccuracy` | 산출 case == expected.case | recommend 표본 전부 | exploratory(슬라이스별 병기) |
| `case3UnderExpansionRate` | 산출 case==3 ∧ len(legs)<=1 | **산출** case==3 인 recommend 표본 | **PRIMARY confirmatory** — #198 로그 정의 그대로 |
| `expectedCase3UnderExpansion` | len(legs)<=1 | expected.case==3 앵커의 recommend 표본 | exploratory(산출 case 오판까지 포함한 보조 시야) |
| `legCoverage` | Σ min(1, 커버리지그룹 distinct 매칭 수 / coverageTarget) | coverageGroups 있는 앵커의 recommend 표본 수(**promptExample 제외**) | confirmatory 보조(슬라이스별) |
| `legCoverageWithPromptExamples` | 위와 동일 | 위와 동일(**promptExample 포함**) | exploratory — 포함판 대조용 |
| `legsInRangeRate` | legsMin<=len(legs)<=legsMax | recommend 표본 전부 | exploratory |
| `overExpansionRate` | 커버리지∪acceptable 어느 그룹에도 안 맞는 leg 수 | 산출 leg 총수(그룹 있는 앵커) | exploratory |
| `buyAllAccuracy` | 산출 buy_all == expected.buyAll | expected.buyAll≠null 앵커의 recommend 표본 | exploratory |
| `totalBudgetAccuracy` | 산출 total_budget == expected.totalBudget(null 포함 정확 일치) | recommend 표본 전부 | exploratory |

진단 카운터(합불 아님): `nonRecommendIntentCount`(앵커별) · `utteranceEchoLegCount`(과전개 leg
중 발화 복사) · `emptyLegsOnCase3Count`.

## confirmatory 사전 등록과 promptExample 제외

`evals/README.md` 규약 5(다중 비교 통제): primary confirmatory metric 은
`case3UnderExpansionRate` **전체** + 사전 등록 슬라이스 2개(`situational`·`purpose`) — 이
둘을 고른 이유는 상황·목적형 발화가 전개 실패가 가장 자주 나타나는 슬라이스이기 때문이다(α
보정). 나머지 슬라이스·축은 exploratory 다.

`promptExample==true` 앵커(5건 — `_SYSTEM` 프롬프트 안에 예시로 등장하는 발화, 예:
"발이 시려워")는 **confirmatory 집계에서 제외**하고 `report.md` 의 별도 표
("promptExample 앵커", 앵커별 slice·recommend 표본 수·case==3 ∧ legs<=1 표본 수·평균
legCoverage 실값)로만 노출한다 — 프롬프트가 그 발화를 예문으로 이미 봤으므로, 그 표본을
일반화 성능과 같은 자리에 놓으면 과대평가된다. **제외 규칙은 `case3UnderExpansionRate` 뿐
아니라 confirmatory-secondary 인 `legCoverage`(및 그 슬라이스 병기·baseline 대조)에도
동일하게 적용된다** — 두 축 다 포함판(`*WithPromptExamples`)을 exploratory 로 별도 남긴다.

슬라이스 표본이 임계(기본 40) 미만이면 `AxisResult.belowSampleThreshold` 가 `true` 가 되고
`report.md` 가 스스로 `exploratory` 라벨을 단다.

## trivial baseline

`evals/README.md` 규약 1(baseline 의무): "항상 leg 1개(전개 없음)" 정책을 LLM 없이 결정론
계산한다 — `case3UnderExpansionRate` 는 구조적으로 1.0(leg 이 항상 1개 이하이므로), leg
커버리지는 앵커 저자 라벨(`baselineGroupsHit`)로 근사, `overExpansionRate` 는 정의상 0.
`report.md` 에 "LLM vs baseline" 슬라이스별 legCoverage 대조표가 실린다 — baseline 의
per-slice/overall legCoverage 도 `legCoverage`(confirmatory-secondary) 축과 같은 규칙으로
promptExample 앵커를 제외해 대조가 사과-사과가 되게 한다(포함판은
`legCoveragePerSliceWithPromptExamples` 로 남는다). **e2e 유효성 최종 판정은 caseId 척추
(골든셋 연결)의 몫**이다 — 이 대조표는 decompose 단계 하나의 이득을 보여줄 뿐이다.

## pair 진단

`pairId` 가 같은 앵커끼리 평균 `len(legs)`·`legCoverage` 를 나란히 인쇄한다(exploratory,
합불 아님). `pairKind`: `INV-paraphrase`(같은 뜻 다른 표현 — legs·coverage 가 비슷해야 함) ·
`DIR-budget`(예산 추가가 legs 를 줄이면 안 됨). 일부 `DIR-budget` 쌍(`camp-budget`)은 비교
대상(`legs-situ-0001`)이 다른 pairId(`camp-paraphrase`)에 속해 있어 표에서 자동으로 짝지어
나오지 않는다 — 그 앵커의 `note` 필드에 비교 대상이 명시돼 있다.

## CI 에서 돌리지 않는다

실 LLM 호출이라 비용·비결정론이 붙는다. **수동 실행 도구**이며, 프롬프트를 바꾸는 PR 이
산출물(`report.md`)을 근거로 첨부한다. `tests/unit/test_legs_probe_*.py` 는 전부 가짜
LLM(`ScriptedDecomposeLLM`)이라 CI 에서 API 콜이 0이다.

## 재현 함정

intent_probe 와 같은 함정이 그대로 적용된다:

1. **전역 페이서 필수** — 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가
   거짓이 된다.
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은
   `results.json.unfilledCells` 에 드러나며 종료 코드 4가 된다.
3. **단일 실행으로 판정 금지** — 채택 판정은 독립 2~3회 분포로 한다.

## 산출물과 종료 코드

`--out <dir>`(이미 있으면 덮지 않는다)

| 파일 | 내용 |
|---|---|
| `results.json` | 축·정의·슬라이스별 병기·진단·baseline·pair 진단·못 채운 셀·페이서·예산 |
| `report.md` | 헤더 + primary 지표 + 축 표 + 슬라이스 표 + baseline 대조 + pair 진단 + promptExample 표 |
| `samples.csv` | 표본 1행씩(caseId·n·intent·case·legsCount·legQueries·legRawCategories·matchedGroups·coverage·overExpandedLegCount·echoLegCount·buyAll·totalBudget) |
| `cells.csv` | 셀 1행씩(표본·시도·실패·충족 여부) |
| `failures.csv` | 버린 시도 1행씩 |
| `run_manifest.json` | 커밋·dirty·앵커 해시·**실제 보낸 프롬프트 해시**·축 정의·category_fanout_max·티어·모델 |

| 코드 | 뜻 |
|---|---|
| 0 | 모든 셀을 채웠다 |
| 2 | 사전 거부(인자·`--out` 존재·앵커 해시/스키마 불일치·프롬프트 읽기 실패·LLM 미설정) |
| 3 | 예산 초과로 중단(부분 산출물 기록) |
| 4 | 못 채운 셀이 있다(부분 산출물 기록) |

## 앵커(정답지)

`fixtures/anchors.json`(39건) — `fixtures/manifest.json` 의 sha256 과 대조해 읽는다(불일치
→ 종료 코드 2). 슬라이스 구성: `single` 9(case1) · `conditions` 5(case2) · `situational` 11 ·
`purpose` 9 · `multi` 5(case3, 총 25건).

## 기준선

`baselines/fast-2026-08-06/` — 최초 실 LLM 기준선(fast 티어). 그 디렉터리의 README 가 표를
해석하고, **단일 실행이라 채택 판정 근거가 아니라는 경고**를 담는다.
