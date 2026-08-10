# RELEASE-CLAIMS-139 — 1차 완료(발표) 핵심 주장·claim-evidence matrix·release gate

> **버전**: v1.0.0 · **상태**: 확정 — #139 에서 결정
> **작성**: 2026-08-10 · **동기**: 발표가 2026-08-14, 나흘 남았다. 이 이슈는 `Blocks: 모든 P0 신규 이슈`이고
> [jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)(staging 최종 benchmark) ·
> [jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)(blind pairwise) ·
> [jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)(최종 리포트)가 이 문서의
> claim-evidence matrix 를 기다린다.
> **관련**: [evals/README.md](../../evals/README.md)(#328 공통 규약 8항, 인용 규율의 정본) ·
> [EVAL-OBS-PLAN-001](EVAL-OBS-PLAN-001.md)(지표 정의) · [jarvis-ai#139](https://github.com/toss-delta-final/jarvis-ai/issues/139)

---

## §0 목적·범위·독법

이 문서는 발표(2026-08-14)에서 증명할 **핵심 주장 4개**, 그 근거인 **claim-evidence matrix**,
발표가 들고 나갈 **최종 산출물 목록**, 그리고 **1차 완료에서 제외하는 범위**를 고정한다.
`evals/` 아래 17개 하네스가 이미 쌓아둔 baseline 을 엮은 결과이며, 새 실행은 하지 않았다 — 이
문서에 있는 모든 수치는 저장소에 커밋된 baseline 산출물에서 **직접 읽은 값**이다.

이 문서는 상위 규약을 **대체하지 않고 인용한다**:
- [evals/README.md](../../evals/README.md) — #328 공통 규약 8항(trivial baseline·`caseId`·결정론/확률
  구분·슬라이스 쿼터·다중비교 통제·CheckList·하네스 커밋·분자분모+datasetHash 재실행)이 정본이다.
- [EVAL-OBS-PLAN-001](EVAL-OBS-PLAN-001.md) — 지표(`nDCG@k`·`Hard-Constraint-Violation` 등)의
  정의와 임계 제안(§1.4·§4)이 정본이다.

**인용 규율 3줄**:

1. **서로 다른 `datasetHash`·fixture 세대의 수치를 같은 표에서 빼지 않는다**(`evals/README.md`
   규약 8항). 특히 `intent_probe` v5(79셀) vs v6(85셀) — 이건 [jarvis-ai#463](https://github.com/toss-delta-final/jarvis-ai/issues/463)이
   명시한 함정이다.
2. **로컬 측정값을 운영 수치로 인용하지 않는다.**
3. **실측하지 않은 수치를 쓰지 않는다.** 비용이 `unknown`이면 `unknown`으로 보존한다.

---

## §1 핵심 주장(claim) 4개

| claim | 주장 | 대상 slice | 왜 이 주장인가 |
|---|---|---|---|
| **C1** | 에이전트 추천 경로는 "아무것도 하지 않는" 순서(no-op) 대비 추천 순위를 유의하게 개선한다 | buyer dev golden, guest·member | [jarvis-ai#275](https://github.com/toss-delta-final/jarvis-ai/issues/275)가 드러낸 "trivial baseline 미등록" 문제를 정면으로 해소한 결과 |
| **C2** | 컨텍스트(직전 화면·장바구니 상태)가 있어도 의도가 흔들리지 않게 라우팅되고, 화면 밖 상품 id 를 확정하지 않는다 | `intent_probe` v6 85셀 출고판 | 모드 선택을 사용자에게 떠넘기지 않는 설계의 정량 근거 |
| **C3** | 개인화는 후보를 줄이지 않고 순서에만 반영되며, 사용자 의도와 하드 제약을 침범하지 않는다 | buyer dev golden 전 arm + 라이브 [jarvis-ai#119](https://github.com/toss-delta-final/jarvis-ai/issues/119) 전후 | 안전성·정합성 지표(0 목표) — 값이 아니라 위반 0, 그리고 결함→수정의 before/after |
| **C4** | 지연과 비용을 공개할 수 있고 예산 안에 있다 | `buyer_recommend`, staging | 평가자가 명시 요구한 p50/p95·쿼리당 비용 공개 |

### C1 — 에이전트 경로 vs no-op

pipeline arm 은 같은 노출 상품 집합을 productId 오름차순으로 재정렬한 no-op 대비 nDCG@10 이
paired bootstrap 95% CI 하한에서도 0을 배제한다(§2 참조) — "에이전트가 정말 필요한가"라는
질문에 사전등록된 통계로 답한다.

**한계**: dev fixture 한정, 오프라인 하네스(결정론 fixture 검색 위 실 LLM decompose/rerank).
staging·실제 트래픽 분포에는 아직 일반화하지 않는다.

### C2 — 컨텍스트 강건 라우팅

직전 화면·장바구니 상태가 있는 상황에서도 `mainIntent`(장바구니 대조군+지시대명사 합성축)와
`screenNoHallucination`(화면 밖 `forbiddenProductId` 확정 금지)이 실 LLM 반복에서 안정적으로
높다(§2 참조).

**한계**: 약축 3개(`general`·`switchLegacy2`·`categoryMixedReplace`)가 낮다 — 아래 §2 수치 참조.

### C3 — 개인화의 안전성(과반영 금지)

개인화는 결정론 하네스에서 하드 제약 위반 0건을 유지하고, [jarvis-ai#119](https://github.com/toss-delta-final/jarvis-ai/issues/119)
전후 라이브 대조에서 필터 유출이 29/31건에서 1/31건으로 줄었다(§2 참조).

**한계**: 노이즈가 섞인 프로필이면 품질이 떨어진다(`cleanNoisyDrop = regression`, §2·§3 참조).

### C4 — 지연·비용 공개 가능성

로컬 WSL2 5종 baseline 이 p50/p95·success/degrade·비용(또는 unknown 사유)을 전부 기록한다.

**한계**: **현재 baseline 5종이 전부 로컬 WSL2다. staging 측정은 [jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)가 만든다**
— C4 는 그때까지 `pending`이다.

---

## §2 claim-evidence matrix

열: `claim ID` · `대상 slice` · `primary metric` · `baseline` · `candidate` · `통과 기준` ·
`필요 artifact` · `담당(이슈)` · `현재 상태`.

### C1 — 에이전트 경로 vs no-op

- **primary metric**: `overall.ndcgAtK.10`(paired case-level delta, bootstrap 95% CI) —
  `evals/ablation/config.py::PRIMARY_METRIC`, `evals/metrics/metrics.py::ndcg_at_k`
- **baseline**: `evals/ablation/baselines/20260805-dev-v2-full-n5/noop_comparison.json` 의
  `pipelineNoop` arm(dataset `2.1.0`, hash `904f90e93a1dbff797c7e8bc48f2a795f006d1e6b5405e753207c76adb8de273`,
  n=62, N=5, seed `20260805`)
- **candidate**: 같은 명령을 발표 시점 dev HEAD 에서 재실행한 산출물(§4 식별 규칙)
- **실측**(`noop_comparison.json.comparisons["pipeline-pipelineNoop"]`, 파일에서 직접 확인):
  meanDelta **+0.10836893756216259**, bootstrap 95% CI **[0.0632438392056272, 0.1556513247733518]**,
  `verdict=pipelineWins`, `label=confirmatory`, pairedCount **62**
  - 슬라이스(`slicePipelineNoopHolmBonferroni`, Holm-Bonferroni m=2): guest meanDelta
    **+0.14773096687328907** CI [0.05900286075241938, 0.2427796707898863] n=31 `significant=true`;
    member meanDelta **+0.06900690825103609** CI [0.018482932977177523, 0.12431922715717074] n=31
    `significant=true`
  - `requiredNRecalculation["pipeline-pipelineNoop"].requiredNCeil` = **26**
  - no-op 정의(`noopDefinition` 원문): "각 arm이 그 케이스·반복에서 실제로 노출한
    rankedProductIds(중복 제거)를 productId 오름차순으로 재정렬한 결정론 파생(F-4b,
    evals.metrics.runner._noop_output 재사용). 추가 LLM 호출 0."
- **통과 기준**: paired bootstrap 95% CI 하한 > 0(전체), 그리고 guest·member 슬라이스 모두
  Holm 보정 후 유의
- **artifact**: `comparison.json`, `noop_comparison.json`, arm 별 `results.json`·`calls.csv`,
  `run_manifest.json`
- **담당**: 선행 [jarvis-ai#146](https://github.com/toss-delta-final/jarvis-ai/issues/146)(닫힘) ·
  재실행/보고 [jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)
- **현재 상태**: `pass`

### C2 — 컨텍스트 강건 라우팅

> ⚠️ **출고판은 `adopted-*`다, `merged-*`가 아니다.** `evals/intent_probe/README.md`의 "기준선" 절 원문:
> "`merged-*`는 `#386`(PR #441) 병합 직후 판(`f99a98867e4a`), `adopted-*`는 **출고판**(`865ed6fd771e`)
> — 두 팔은 픽스처·모델·앵커·N이 전부 같고 `_SYSTEM`이 **10자만** 다르다." 같은 README는 "전 축
> 대조표는 `fast-2026-08-07-430-v6-adopted-1/README.md`가 정본이다"라고 한다. **발표는 출고판을
> 인용한다.**
>
> **`mainIntent`는 "전체 의도 라우팅 정확도"가 아니다.** `results.json`의 `axes.mainIntent.definition`
> 원문: numerator "intent 가 기대 intent 와 일치한 표본 수 (장바구니 대조군 + 지시대명사)",
> denominator "장바구니 대조군 6발화 + 지시대명사 4발화 × 컨텍스트 3종 × N", components
> `["cartControl", "demonstrative"]`. **합성축이다.** 또 `report.md` 원문: "**이건 골든셋이
> 아니다.** 추천 품질이 아니라 intent 라우팅 분포를 잰 표다." **정의를 붙이지 않은 "라우팅
> 정확도 xx%" 문장을 쓰지 않는다.**

- **primary metric**: `mainIntent` 축 ratio — 위 정의를 표에 그대로 병기(구현
  `evals/intent_probe/metrics.py::score_axis`)
- **baseline**: **출고판 2 run** — `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-1`
  (`run.timestamp=2026-08-07T13:28:00.996526+00:00`, `budget.totalCostUsd=0.15047944999999993`)과
  `…-adopted-2`(`run.timestamp=2026-08-07T13:46:06.530784+00:00`, `budget.totalCostUsd=0.15381354999999974`).
  둘 다 fixture `intent-probe-anchors-b-v6`, **85셀 × N=8**, tier fast, provider openai,
  fast `gpt-5-nano`/minimal · smart `gpt-5.6-luna`/medium
- **실측**(adopted-1 / adopted-2, 두 열로 병기 — 같은 세대라 허용):
  - `mainIntent` **236/240 = 0.9833** / **235/240 = 0.9792**
  - `screenNoHallucination` **8/8 = 1.0** / **8/8 = 1.0**(정의: "resolvedProductId(해소기 통과 후
    최종값)가 expected.forbiddenProductId 와 다른 표본 수" — 화면 밖 id 확정 금지)
  - `orderStatus` 48/48 / 48/48 · `cartControl` 144/144 / 144/144 ·
    `wishlistViewRouting` 48/48 / 48/48 · `wishlistViewNoSteal` 24/24 / 24/24 ·
    `categoryCarry` 32/32 / 32/32 · `screenReask` 8/8 / 8/8
  - `screenResolution` 47/48 = 0.9792 / 46/48 = 0.9583 · `demonstrative` 92/96 = 0.9583 / 91/96 = 0.9479
  - **약축(반드시 병기)**: `general` **30/48 = 0.625** / **31/48 = 0.6458**;
    `switchLegacy2` **11/16 = 0.6875** / **12/16 = 0.75**;
    `categoryMixedReplace` **26/32 = 0.8125** / **27/32 = 0.8438**;
    `categoryClear` **28/32 = 0.875** / **28/32 = 0.875**;
    `switchAll7` 46/56 = 0.8214 / 46/56 = 0.8214
    - **약축 후속 이슈**(둘 다 열려 있다, 2026-08-10T08:25:11Z `gh` 로 확인 —
      [jarvis-ai#240](https://github.com/toss-delta-final/jarvis-ai/issues/240)을 "닫힘"으로 적은
      초판·Codex 조사는 오류였다): [jarvis-ai#240](https://github.com/toss-delta-final/jarvis-ai/issues/240)이
      `switchLegacy2`·`general` 이 걸린 PENDING_CART 규칙군 재설계를, [jarvis-ai#259](https://github.com/toss-delta-final/jarvis-ai/issues/259)가
      라우팅 정확도 개선 3안(현행 유지/티어 상향/라우팅 분리) 비교를 다룬다. 둘 다 §11 에서
      "주장 밖"으로 판정했다 — C2 는 현 출고판 수치로 이미 성립한다.
      [jarvis-ai#463](https://github.com/toss-delta-final/jarvis-ai/issues/463)은
      `screenExactPick`·`categoryClear`·`missRate`를 다룬다.
- **통과 기준**: `mainIntent` ≥ **0.95** **and** `screenNoHallucination` = **1.0**, 동일 fixture
  세대(v6, 85셀)에서 2 run 이상 — 출고판 2 run 으로 충족됨을 명시
  - 임계 근거: `EVAL-OBS-PLAN-001` §4 의 `eval_regression_routing_acc_min = 0.85` 보다 높게 잡는다
    — 현 실측이 0.979~0.983 이라 0.85 는 회귀를 못 잡는다.
- **artifact**: 두 run 의 `results.json`·`run_manifest.json`·`README.md`, 전 축 대조표 정본
  `…-adopted-1/README.md`
- **담당**: 선행 [jarvis-ai#260](https://github.com/toss-delta-final/jarvis-ai/issues/260) ·
  [jarvis-ai#430](https://github.com/toss-delta-final/jarvis-ai/issues/430) ·
  [jarvis-ai#386](https://github.com/toss-delta-final/jarvis-ai/issues/386) · 약축 후속(1차 주장 밖)
- **현재 상태**: `pass`

### C3 — 개인화는 후보를 줄이지 않는다(안전성)

- **primary metric**: 위반 건수(0 목표 안전성 지표 — `EVAL-OBS-PLAN-001` §1.4 원문: "**Hard-Constraint-Violation**
  … (0 목표 — 값이 아니라 안전성 지표)") + 보조: [jarvis-ai#119](https://github.com/toss-delta-final/jarvis-ai/issues/119)
  전후 라이브 필터 유출 건수
- **근거 ① — #119 전후 라이브 대조(이 claim 의 서사 중심)**. `evals/personalization/baselines/live-v1/comparison.json`은
  같은 실행 안에 **수정 전 동작**(`clean_both`, 프로필이 decompose 하드 필터로 새던 경로)과
  **출고 동작**(`clean_rerank_only`, `profile_injection_scope` 기본값)을 나란히 갖는다:
  - `axisLeakage`: `clean_both` **29건** → `clean_rerank_only` **1건**(`guest` 0건), 전체 31 케이스
  - `intentContradictions`: `clean_both` **10건** → `clean_rerank_only` **3건**(`guest` 5건)
  - `pairedVsGuest.<arm>.overall.ndcgAtK.10`: `clean_both` meanDelta **−0.2879316720443962**
    CI95 [−0.4813819808647765, −0.11845955082203671] → `clean_rerank_only` meanDelta
    **−0.05644463392740816** CI95 **[−0.2021440745401869, 0.04957802400457049]**(**CI 가 0 을 포함**),
    양쪽 다 ranking includedCount **18** / pairedCount **31**, `live.repeats` **1**
  - `CHANGELOG.md`의 #147 항목 원문: "#119 수정 전후(`both` vs `rerank_only`) 실 LLM paired 회귀
    자료를 `baselines/live-v1`에 영속해 수정 전 29/31건 필터 유출·ΔNDCG -0.29에서 수정 후 유출
    무신호·CI 0 포함으로의 변화를 기록한다."
  - **주의**: 이건 "개인화가 품질을 높인다"의 근거가 아니다(수정 후도 CI 가 0 포함 = inconclusive).
    **"개인화가 후보를 줄이지 않는다"**의 근거다. 이 구분을 흐리지 않는다.
- **근거 ②~④ baseline**:
  - `evals/personalization/baselines/dev-v2/overreach.json`: `forbiddenOrRecentInclusion` count
    **0** `verdict=pass`; `intentContradiction` count **0** `verdict=pass`
  - `evals/personalization/baselines/dev-v2/comparison.json` `hardFilter`: 전 25개 (arm × weight)
    셀 `violationCount` **0**, top-level `violationCount` **0** `verdict=pass`
  - `evals/ablation/baselines/20260805-dev-v2-full-n5/comparison.json`: **정정 필요 항목** — 3-arm
    전부가 0.0 이 아니다. 실측: `pipeline` `hardConstraintViolationRate` **0.0**,
    `scoring` **0.029850746268656716**, `single_call` **0.014925373134328358**;
    `hardFailureCount`는 3-arm 전부 **0**. `hardConstraintViolationRateDenominator` 원문: "hardFailure
    행도 evaluated metric row 분모에 포함되어 위반율이 희석될 수 있음; hardFailureCount를 함께
    해석." **1차 주장이 인용하는 arm 은 production 대상인 `pipeline`(0.0)이다** — `scoring`·
    `single_call`은 이 ablation 사전 등록의 비교용 arm 이며 출고 경로가 아니다.
- **반드시 병기할 한계**: 같은 `overreach.json`의 `cleanNoisyDrop`은 meanDelta 없이 CI95
  **[−0.14890832467160464, −0.09266621063303049]**, margin 0.03, `verdict=regression`,
  includedCount 68, pairedCount 109 — **노이즈가 섞인 프로필에서는 품질이 떨어진다.**
  ([jarvis-ai#361](https://github.com/toss-delta-final/jarvis-ai/issues/361)이 dev-v2 baseline 을
  현행 골든셋(109건)으로 재생성해 이 수치가 바뀌었다 — pass/fail 판정 자체는 그대로 `regression`.)
- **통과 기준**: `forbiddenOrRecentInclusion` = 0 **and** `intentContradiction` = 0 **and**
  production arm(`pipeline`) `hardConstraintViolationRate` = 0.0(`hardFailureCount` 병기)
  **and** 라이브 `clean_rerank_only`의 `axisLeakage` ≤ 1건/31케이스
- **artifact**: 위 파일들 + `live-v1/{comparison.json,run_manifest.json}` + 각 `run_manifest.json`
- **담당**: 선행 [jarvis-ai#147](https://github.com/toss-delta-final/jarvis-ai/issues/147) ·
  [jarvis-ai#119](https://github.com/toss-delta-final/jarvis-ai/issues/119) · 집행 근거
  [jarvis-ai#359](https://github.com/toss-delta-final/jarvis-ai/issues/359) ·
  [jarvis-ai#360](https://github.com/toss-delta-final/jarvis-ai/issues/360)(pin 불변·개인화 중지) ·
  [jarvis-ai#361](https://github.com/toss-delta-final/jarvis-ai/issues/361) — 측정으로 보인 성질
  ("개인화가 후보를 줄이지 않는다")을 `INV-PGRAPH-ORDER`(SPEC-PROFILE-GRAPH-149 §6.12)로 코드
  불변식에 고정한 후속. 정적 검사(그래프 모듈이 필터 타입을 참조하지 않음)와 행동 검사
  (`tests/unit/test_profile_graph_filter_isolation.py::test_graph_modules_do_not_reach_the_search_filter_type`
  등)로 이중 집행하며 런타임 변경은 0줄이다.
- **현재 상태**: `pass`

### C4 — 지연·비용 공개 가능성

- **primary metric**: `client_ttft_ms` p95, `client_total_ms` p95, `success_rate`, 쿼리당
  `cost_usd`
- **현 baseline(로컬, 운영 아님)**: `evals/benchmark/baselines/20260809T021733612671Z-local-realllm-spring`
  — `measured:buyer_recommend@1` `client_ttft_ms.p50` **5456.403518997831**, `.p95`
  **7821.348602003127**; `client_total_ms.p50` **5480.237982999824**, `.p95` **7842.093801998999**;
  `success_rate` **1.0**, `error_rate` **0.0** — `@5`: ttft p50 **6507.476107995899**, p95
  **9042.410506001033**; total p50 **6527.294614999846**, p95 **9063.382024003658**
  - 짝 스텁 baseline `20260809T014442747650Z-local-stub-spring`(같은 Spring·시드·시나리오, LLM 만
    스텁): `@1` ttft p50 **903.3608000027016**, p95 **945.5652799952077** → **차이가 벤더 지연
    기여분**(약 4.6~6.9초)
  - **주의**: 두 baseline 모두 `degrade_rate` **1.0** 인데 이는 BE 조건(`I-21
    POST /internal/recommendations` 400 → `push_skipped` 상시)이며 AI 코드 결함이 아니다.
    manifest 의 `dependency_conditions`(원문 필드명 — "dependency_note"가 아니다) 원문: "Spring
    기동(로컬 8080) — I-1 200. I-21 은 400 이라 degradeReason=push_skipped 상시(BE 측 조건)."
  - **비용**: 이 하네스의 measured artifact 는 `cost_usd: null`,
    `cost_unknown_reason: price_missing(model=gpt-5.6-luna)`로 **unknown 을 보존**한다 — 이 규율을
    발표에서도 명시한다.
  - 참고 비용 실측: ablation `arms.pipeline.resources.perCaseCostUsd` = **0.0014335289552238806**
- **candidate**: **[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)가 만들
  AWS staging run**(동일 scenario·payload, immutable run id)
- **통과 기준(근거 병기)**:
  - `client_ttft_ms` p95 ≤ **10,000ms** — 근거 `EVAL-OBS-PLAN-001` §4 `slo_first_token_ms: int = 10000`,
    `docs/mvp-todo.md` "first-token 10s", [MEASURE-FIRST-TOKEN-363](MEASURE-FIRST-TOKEN-363.md)가
    10.0s 를 실제 차단 상한으로 고정
  - buyer `client_total_ms` p95 ≤ **30,000ms** — `slo_total_buyer_ms: int = 30000`
  - `success_rate` ≥ **0.99**, `timeout_rate` = 0
  - 쿼리당 비용이 산출물에 기록될 것(모르면 `cost_unknown_reason`과 함께 unknown 보존)
- **artifact**: `manifest.json`·`metrics.csv`·`report.md`·`raw.jsonl`
- **담당**: [jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152) →
  [jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)
- **현재 상태**: **`pending(#152)`**

> ⚠️ **C4 는 staging 실측 전까지 `pending`이며, 로컬 수치를 발표에서 운영 수치로 말하지 않는다.**

---

## §3 무엇을 증명하지 않는가 — 정직한 negative result

이 절은 **발표 자산**이다. 숨기지 말고 세운다.

### 1. 에이전트 파이프라인이 단일 LLM 호출보다 낫다 — 현재 근거로는 증명 불가

- v1 `evals/ablation/baselines/20260803-dev-full-n5/comparison.json`(dataset `1.0.0`, hash
  `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`): `pipeline-single_call`
  meanDelta **+0.086859652969224**, CI95 **[0.022035023096324616, 0.15995802608927143]**,
  `verdict=pipelineWins`, pairedCount **18**
- v2 `evals/ablation/baselines/20260805-dev-v2-full-n5/comparison.json`(dataset `2.1.0`, hash
  `904f90e93a1dbff797c7e8bc48f2a795f006d1e6b5405e753207c76adb8de273`): meanDelta
  **+0.031498116410906**, CI95 **[-0.004700..., 0.066303...]**, `verdict=inconclusive`,
  pairedCount **62**
- **두 세대는 datasetHash 가 달라 직접 비교 금지**(`evals/README.md` 규약 8항).
  `evals/ablation/DECISION.md` 원문: "`pipeline - single_call`가 v1(2026-08-03, `pipelineWins`)과
  부호는 같지만 이번엔 `inconclusive`다 — **다른 datasetHash 비교이므로 '품질 격차가
  좁혀졌다'고 해석하지 않는다**(데이터셋 자체가 다시 만들어졌다)." 그리고 "**다른 datasetHash와
  비교 금지** — 이번 v2 결과는 2026-08-03 v1 결과와 절대 나란히 '개선/퇴보'로 해석하지 않는다."
- v2 가 판별력이 더 높다는 근거도 `DECISION.md`에 있다(추정이 아니다): v1은 "순위 판별력이 있는
  18건", v2는 "**순위 판별력이 있는 62/67건**"이 순위 지표에 포함됐다. [jarvis-ai#333](https://github.com/toss-delta-final/jarvis-ai/issues/333)이
  candidate depth 를 서빙 상한 30에 맞추고 hard negative·slice quota를 넣었다
  (`evals/personalization/baselines/dev-v2/comparison.json`의 arm 별 자원 정보는 후보 깊이 조건을
  전제로 한다).
- **필요 N(80% power, α=.05)**: v2 관측치 meanDelta **0.031498116410906**, sd **0.1487880146**
  (`comparison.json.pairedComparisons["pipeline-single_call"].summary`)로
  `N = (z_{α/2}+z_β)² × SD² / meanDelta² = (1.959964+0.841621)² × 0.1487880146² / 0.031498116410906²
  ≈ 175.14` → **올림 176 paired cases**. 현 pairedCount 62 의 약 2.8배다. 이 계산은 **관측치에 대한
  산술이며 새 실행 결과가 아니다.**
- 이 하네스에는 `requiredNFor80PowerAlpha05`가 `pipeline-single_call`에 대해 **저장돼 있지
  않다**(`noop_comparison.json.requiredNRecalculation`은 `pipeline-pipelineNoop`·
  `pipeline-scoring`·`scoring-scoringNoop` 세 쌍만 갖는다).

### 2. 개인화가 추천 품질을 높인다 — 라이브에서 미확정(inconclusive)

- 결정론 `personalization/baselines/dev-v2`
  `pairedComparisons.clean_vs_member_no_profile.overall.ndcgAtK.10`: meanDelta
  **+0.2581424304041047**, CI95 [0.20742327911007297, 0.31120628348994267], includedCount **68**,
  pairedCount 109. arm 설정은 `provider: scripted`, `decompose: expectedFilters`,
  `rerank: deterministicScoringBaseline` — **결정론 스코어러 위의 측정**이다.
  ([jarvis-ai#361](https://github.com/toss-delta-final/jarvis-ai/issues/361)이 이 baseline 을
  현행 골든셋(dev 109건, `datasetHash=675520d9…`)으로 재생성했다 — 이전 판은
  `datasetHash=d16eb0e9…`(96건)이었다. `CHANGELOG.md`의 #361 항목 원문: "분모가 바뀐 것이지
  품질 저하가 아니다 — 서로 다른 케이스 집합의 nDCG 는 비교 대상이 아니다." 방향(양수, 결정론
  스코어러 위에서 개인화가 순위를 개선)과 라이브 대비 결론은 변하지 않는다.)
- 라이브 출고 설정 `personalization/baselines/live-v1`
  `pairedVsGuest.clean_rerank_only.overall.ndcgAtK.10`: meanDelta **−0.05644463392740816**,
  CI95 **[−0.2021440745401869, 0.04957802400457049]** → **CI 가 0 을 포함한다**. ranking
  includedCount **18**, pairedCount 31, `live.repeats` **1**.
- **다음 두 가지를 반드시 명시한다**:
  (a) 같은 파일의 `clean_both` 음수(−0.2879)는 **#119 수정 전 동작**이며 출고 설정이 아니다 —
      그 자료는 §2 C3 의 before/after 근거로 쓴다.
  (b) 결정론과 라이브는 **대조 arm 이 다르다**(`member_no_profile` 대비 vs `guest` 대비).
      `dev-v2`에는 `clean_vs_guest` 키가 없다(직접 확인 — `pairedComparisons` 키는
      `clean_vs_member_no_profile`·`member_no_profile_vs_guest`·`noisy_vs_clean`·
      `repeated_vs_clean` 뿐이다). 두 수치를 같은 표에서 빼지 않는다.
- **결론**: 근거가 한 방향으로 모이지 않으므로 1차 주장에서 뺀다. 개인화는 **C3**로만 주장한다.
- **재개 조건**: 출고 설정에서 repeats ≥ 5 의 라이브 재실측, ranking-eligible n 확대.

### 3. 과소지정 되물음 판정 정확도

`evals/underspecified_probe/baselines/fast-2026-08-08-run3/results.json`의 `judgmentAccuracy`가
낮다 — **104/216 = 0.4815**, CI95 **[0.41575543293176387, 0.5478547066528497]**. 오탐율은 낮다
(`falseAlarmRate` 실측: `fast-2026-08-08-run1/2/3` 각 0/104=0.0, `union-fast-2026-08-08-post430-run1`
2/104=0.0192). 주장으로 세우지 않는다.

### 4. 판매자 품질

§8 참조.

---

## §4 baseline / candidate 버전 식별 규칙

- **baseline id** = `evals/<harness>/baselines/<dirName>` + `run_manifest.json`의 `run.runId` ·
  `commitSha`(+`dirty`) · `run.timestamp` · dataset 세대(`datasetVersion`/`datasetHash` 또는
  `fixtureVersion`+셀 수).
- **candidate id** = 같은 규칙. 디렉터리명은 `<YYYYMMDD>-<split>-<label>` 또는
  `<tier>-<YYYY-MM-DD>-<label>` — 기존 관례를 그대로 따르고 새 규칙을 발명하지 않는다.
- **비교 가능성 규칙**: baseline 과 candidate 는 같은 `datasetHash`(또는 같은 fixtureVersion+셀
  수)일 때만 같은 표에서 뺀다. 다르면 세대를 표기하고 각각 따로 보고한다.
  `evals/README.md` 규약 8항과 `evals/goldenset/GUIDE.md`(hash 변경 시 전 baseline 재실행)를
  인용한다.
- **"최신 timestamp" ≠ "출고판" 규칙**. `intent_probe`가 실례다: `merged-*`는 병합 직후 판,
  `adopted-*`가 출고판이며 `_SYSTEM`이 10자 다르다(`evals/intent_probe/README.md` 원문 인용,
  §2 C2 참조). 발표가 인용하는 baseline 은 **출고판 계보**여야 하고, 대조 팔(`merged-*`)을 출고
  수치로 쓰지 않는다.

**현행 "추천 baseline" 지정표**:

| 하네스 | 현행 baseline 디렉터리 | 세대 식별자 | 계보(출고판/대조팔) |
|---|---|---|---|
| ablation | `evals/ablation/baselines/20260805-dev-v2-full-n5/` | `datasetVersion=2.1.0`, hash `904f90e9…` | 출고판(유일 arm 세트) |
| intent_probe | `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-{1,2}/` | `intent-probe-anchors-b-v6`, 85셀 | **출고판**(`_SYSTEM 865ed6fd771e`) — `merged-*`는 대조팔 |
| personalization(결정론) | `evals/personalization/baselines/dev-v2/` | `configVersion` dev-v2, dev split | 출고판(결정론 스코어러 위) |
| personalization(라이브) | `evals/personalization/baselines/live-v1/` | `--live`, repeats=1 | 출고판(#119 수정 후 `clean_rerank_only`) |
| benchmark | `evals/benchmark/baselines/20260809T021733612671Z-local-realllm-spring/` | commit `6381a1e1f04710688170294f383fb84efd3a0d0e`, dirty=false | 로컬 참고 — staging 은 #152 |
| underspecified_probe | `evals/underspecified_probe/baselines/fast-2026-08-08-run3/`(judgmentAccuracy 인용시) | `underspec-anchors-v1` | 참고(주장 밖) |

- `codex-A-harness-census.md`의 "최신 baseline" 열은 benchmark 최신 baseline 디렉터리를 "checkout
  에 없음"이라 적었는데 **그건 오류다** — `evals/benchmark/baselines/20260809T021733612671Z-local-realllm-spring/`은
  실제로 존재한다(`manifest.json`·`metrics.csv`·`report.md`·`raw.jsonl` 확인). 이 문서 표에서
  바로잡았다.

---

## §5 품질·성능·비용 metric 과 측정 환경 표

### 표 A — metric 정의

이 표는 각 metric 의 분자·분모와 구현 심볼을 고정한다. **줄 번호로 인용하지 않는다.**

| metric | 분자/분모 정의 | 출처(파일::심볼) | 쓰는 claim | 하네스 |
|---|---|---|---|---|
| `nDCG@k` | DCG@k / IDCG@k, DCG = Σ rel_i / log2(i+1). IDCG=0 케이스는 분모 제외 | `EVAL-OBS-PLAN-001` §1.4, `evals/metrics/metrics.py::ndcg_at_k` | C1, C3, §3 | ablation, personalization, scoring, goldenset |
| `Recall@k`·`Precision@k`·`MRR`·`Filter-Accuracy` | `EVAL-OBS-PLAN-001` §1.4 | 위 문서 | exploratory | ablation, model_eval |
| `Hard-Constraint-Violation` | 0 목표 안전성 지표(값이 아니라 위반 존재 여부) | `EVAL-OBS-PLAN-001` §1.4 | C3 | ablation, personalization |
| `mainIntent` ratio | 장바구니 대조군+지시대명사 합성축(정의는 §2 C2 원문 참조) | `evals/intent_probe/metrics.py::score_axis` | C2 | intent_probe |
| `screenNoHallucination` | resolvedProductId ≠ expected.forbiddenProductId 인 표본 수 | `evals/intent_probe/metrics.py::score_axis` | C2 | intent_probe |
| `missRate` | 판정 False / expectedReask=true 앵커의 recommend 표본 | `evals/underspecified_probe/metrics.py::axis_miss_rate` | §3 | underspecified_probe |
| `presence.precision/recall` | (match+valueMismatch)/(match+valueMismatch+spurious 또는 missing) | `evals/filter_axes/metrics.py::_precision_recall_f1` | 참고 | filter_axes |
| `caseAccuracy` | decompose_case == expectedCase 표본 수 / confirmatory 대상 표본 | `evals/legs_probe/metrics.py::axis_case_accuracy` | 참고 | legs_probe |
| `recall`(taste) | 최대 이분 매칭된 기대 triple 수 / noise 제외 세션의 기대 triple × N | `evals/taste_probe/metrics.py::score_recall` | 참고 | taste_probe |
| `client_ttft_ms`·`client_total_ms`·`success_rate`·`degrade_rate`·`cost_usd` | reliability 분모=measured 전체, latency 분모=성공(non-empty token+terminal done) | `evals/benchmark/` (`report.py`, `scenarios.py::evaluate_outcome`) | C4 | benchmark |

### 표 B — 측정 환경

이 표는 각 측정 환경이 무엇을 재고, 어떤 한계를 갖는지 고정한다.

| 환경 | 무엇을 재나 | 결정론/확률 | CI/수동 | 대표 baseline | 한계 |
|---|---|---|---|---|---|
| 결정론 fixture(ScriptedLLM) | filter/ranking/hard-constraint | 결정론 | CI | `evals/scoring/baselines/dev-v2.3/`, `evals/personalization/baselines/dev-v2/` | 실 LLM decompose/rerank 를 대역, 라이브 편차는 별도 확인 필요 |
| 실 LLM probe(수동) | intent 라우팅, decompose 필터, 개인화 유출 | 확률 | 수동 | `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-*/`, `evals/personalization/baselines/live-v1/` | 반복 수(N=8, live repeats=1) 제한, 비용 gate 하 실행 |
| 로컬 HTTP/SSE benchmark(로컬 WSL2) | TTFT·total·success/degrade·비용 | 확률(실 LLM 벤더) | 수동 | `evals/benchmark/baselines/20260809T021733612671Z-local-realllm-spring/` | **로컬 WSL2** — 네트워크·인프라 조건이 운영과 다르다. 이웃 워크트리와 pg 컨테이너 공유 |
| AWS staging | 동일 시나리오의 운영급 지연·비용 | 확률 | 수동 | **아직 없음 — [jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)** | C4 정본이 될 유일한 환경, 미실행 |
| 사람 평가 | blind pairwise 선호 | — | 수동 | **미착수 — [jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)** | optional evidence, 1차 통과 조건 아님 |

로컬 5종의 환경 조건(`codex-B-artifacts-survey.md` §3 근거, manifest 로 확인): 모두
`client_region=local-wsl2`, `instance_type=local-dev-wsl2`, `image` null, price table 빈 객체.
`local-realllm-spring`(commit `6381a1e1f04710688170294f383fb84efd3a0d0e`, dirty=false)은
`local-stub-spring`(commit `0dd53296495df8bb197746d73673d6f0f71190b3`, dirty=false)과 같은 Spring·
시드·시나리오에서 LLM 만 실제로 바꾼 짝이다.

---

## §6 공통 run manifest 항목(이슈 완료 조건)

이슈가 요구한 항목: commit SHA·dirty flag, lockfile/image digest, dataset·prompt·config hash,
model parameters, seed, 실행 명령.

**새로 발명하지 않고 이미 있는 것을 정본으로 승격한다** —
`evals/ablation/baselines/20260805-dev-v2-full-n5/run_manifest.json`이 사실상 전부를 갖고 있다
(직접 열어 최상위/중첩 키 전수 확인).

| 항목 | manifest 키 | 현행 기록 하네스 | 공백 |
|---|---|---|---|
| commit | `commitSha` | ablation, category_probe, intent_probe, legs_probe, model_eval, personalization, priority_probe, scoring, taste_probe, underspecified_probe | benchmark 는 `manifest.json`의 `git_sha`(별도 스키마) |
| dirty | `dirty` | 위와 동일 | benchmark 는 별도 필드 없음(`git_sha`만) |
| lockfile | `hashes.uvLock` | 위와 동일 | benchmark 는 `lockfile_sha`(별도 스키마) |
| image digest | `image` | 위와 동일(값은 **전 하네스에서 null**) | 컨테이너 실행이 아니라서다. staging(#152)에서는 채워야 한다 |
| dataset manifest | `hashes.datasetManifest` | ablation 등 | — |
| prompt hash | `hashes.prompts.decompose`·`hashes.prompts.rerank`·`hashes.singleCallPrompt` | ablation | 하네스별 prompt 키 이름이 다르다 |
| config hash | `hashes.config`·`hashes.evalConfig` | ablation | — |
| dataset 세대 | `<harness>.datasetVersion`/`datasetHash`(예: `ablation.datasetVersion`) 또는 `fixtureVersion`+셀 수 | ablation, scoring, personalization(`datasetHash`) | `datasetVersion`/`datasetHash`를 top-level 로 기록하는 하네스가 일부뿐(요약표 참조 — `filter_axes`는 `source.datasetHash`, `personalization`은 top-level `configVersion`) |
| model parameters | `modelEval.modelConfig.*`(ablation), `<harness>.modelConfig`(intent_probe 등) | 대다수 | — |
| seed | `seed`(top-level) | 대다수 | — |
| 실행 명령 | `run.command`·`run.runId`·`run.timestamp` | 대다수 | — |
| 플랫폼 | `platform`·`pythonVersion` | 대다수 | — |

**공백**(`codex-A-harness-census.md` 요약표 2 근거, 직접 확인):
- `benchmark`는 `run_manifest.json`이 아니라 `manifest.json`이며 스키마가 다르다(키:
  `bootstrap`·`client_region`·`client_runtime`·`command`·`dependency_conditions`·`dependency_ids`·
  `ended_at_utc`·`git_sha`·`image`·`instance_type`·`lockfile_sha`·`model_ids`·`price_table`·
  `sample_size_rationale`·`started_at_utc`·`target` — 직접 확인).
- `first_event_budget`·`metrics`·`goldenset`·`combo_matrix`·`underspecified_cases`는 커밋된
  manifest 가 없다(`baselines/` 디렉터리 자체가 없거나 결과 파일만 있음).
- `image`는 전 하네스에서 null — 컨테이너 실행이 아니라서다. staging(#152)에서는 채워야 한다.
- `datasetVersion`/`datasetHash`를 top-level 로 기록하는 하네스가 일부뿐이다.

**[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)·
[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)가 지켜야 할 최소 필수
6항**(이 문서의 결정): `commitSha`+`dirty` · `hashes.uvLock` · dataset 세대(`datasetVersion`+
`datasetHash` 또는 `fixtureVersion`+셀 수) · model parameters · `seed` ·
`run.command`+`run.runId`+`run.timestamp`. staging 은 여기에 `image` digest 를 추가한다.

---

## §7 최종 발표 산출물 목록과 재현 명령

| 산출물 | 경로 | 무엇을 보여주나 | 재현 명령 | 연결 claim | 상태 |
|---|---|---|---|---|---|
| 1. golden dataset | `evals/goldenset/{manifest.json,cases/,GUIDE.md,audit/leakage_report.md}` | buyer golden v2 — 라벨·adjudication·누출 감사(위반 0건, 경고 16건) | (자산 — 재현 명령 아님, [jarvis-ai#333](https://github.com/toss-delta-final/jarvis-ai/issues/333) 산출물) | C1 | 있음 |
| 2. ablation 표(no-op 포함) | `evals/ablation/baselines/20260805-dev-v2-full-n5/` | pipeline vs scoring vs single_call vs no-op | `uv run python -m evals.ablation --out evals/ablation/baselines/<label>` | C1, §3-1 | 있음 |
| 3. intent 라우팅 표 | `evals/intent_probe/baselines/fast-2026-08-07-430-v6-adopted-{1,2}/` | 컨텍스트 강건 라우팅·화면 밖 확정 금지 | `uv run python -m evals.intent_probe --out <dir> --tier fast` | C2 | 있음 |
| 4. 개인화 과반영 표 | `evals/personalization/baselines/dev-v2/{overreach.json,comparison.json}`, `live-v1/comparison.json` | 하드 제약 위반 0, #119 전후 유출 감소 | 결정론: `uv run python -m evals.personalization --out <dir> --seed 20260803`(`run_manifest.json.seed`로 확인 — 재생성 후에도 20260803 그대로) · 라이브: `uv run python -m evals.personalization --live --out <dir>`(`live-v1/run_manifest.json.run.command` 그대로 — `--repeats` 생략 시 기본값이 baseline 의 `live.repeats=1`과 일치) | C3 | 있음 |
| 5. latency·cost 표(로컬 5종) | `evals/benchmark/baselines/*/` | TTFT/total/success/degrade/비용(unknown 포함) | `uv run python -m evals.benchmark.runner --base-url http://localhost:8000 --target-label local --scenarios buyer_recommend,buyer_fallback --concurrency 1,5,10 --measured-requests 30 --out-dir evals/benchmark/baselines` | C4 참고 | 있음 |
| 6. latency·cost 표(staging) | **[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152) 산출** | staging 운영급 지연·비용 | #152 정의 | C4 정본 | `pending(#152)` |
| 7. trace 사례 | `app/core/reco_provenance.py`, `docs/api-spec.md` §4.2 | `recommendationRequestId` 상관키로 실행 추적 | (코드 자산, 재현 명령 아님) | 신뢰성 보조(C1~C4 공통) | 있음([jarvis-ai#140](https://github.com/toss-delta-final/jarvis-ai/issues/140)) |
| 8. negative result appendix | 본 문서 §3 + `evals/ablation/DECISION.md` + `docs/research/RESEARCH-TEACHER-275.md` | 증명하지 못한 것을 정직하게 세운 appendix | (문서 자산) | §3 | 있음 |
| 9. run manifest 색인 | 본 문서 §6 | 재현 필수 6항 정의와 공백 | (문서 자산) | G0(전 claim) | 있음 |

---

## §8 1차 완료에서 제외하는 범위

| 항목 | 제외 사유 | 재개 조건 |
|---|---|---|
| **판매자 품질** | `evals/`에 판매자 품질 하네스가 없다(성능 시나리오 `evals/benchmark/fixtures/seller.json`뿐). `docs/specs/SELLER-FINAL-RISKS.md` §2 원문: V1 "**provider별 실 LLM 검증 0회**"("라우팅 정확도·draft 품질·first-token 체감과 ToolStrategy 정합은 전부 미검증"), V3 "Spring 실연동 0회"("I-* 실측은 I-13 봉투뿐"), V4 "FE→Spring→AI E2E 패스스루 미실시". 참고 측정 1건은 부록으로만: `evals/benchmark/baselines/20260802T140556535202Z-local-spring-seller/report.md` `seller_analysis@1` total p50 **10686.887485ms** / p95 **11728.020683ms**, 30/30 성공, cost `unknown(price_missing)`; `seller_general@1` p50 **1713.619411ms** / p95 **2517.001339ms** — **로컬·brandId=1 집계 404 조건**, 품질을 잰 것이 아니다 | 판매자 골든셋 착수(`EVAL-OBS-PLAN-001` §1.5 설계 존재, 상태는 "제안") |
| **개인화 품질 향상 주장(B)** | §3-2 | 출고 설정 라이브 재실측(repeats ≥ 5), ranking-eligible n 확대 |
| **파이프라인 vs 단일 LLM 우위 주장(G)** | §3-1 | 필요 N ≈ 176 확보(현 62 의 2.8배) |
| **연구 트랙**(`RESEARCH-TEACHER-275`·`RESEARCH-CF-159`·`RESEARCH-LTR-160`·`RESEARCH-BANDIT-161`) | `RESEARCH-LLMEVAL-330` §9.1 APO 착수 조건("모두 충족해야 offline arm 을 시작한다") 원문 조건 ②: "목적함수 존재 — 단계별 결정론 metric(에픽 자식 #331/#332/#334) 중 최소 2축이 커밋되어 APO 의 목적함수로 쓸 수 있을 것" — 아직 조건 미충족 | 조건 전부 충족 시 |
| **운영 관측 인프라** | Prometheus/Grafana·`/internal/metrics`(`EVAL-OBS-PLAN-001` §3.3 확장분). api-spec 개정이 선행돼야 하므로 1차 밖 | api-spec 개정 후 |
| **배포·운영 준비**([jarvis-ai#155](https://github.com/toss-delta-final/jarvis-ai/issues/155)~[jarvis-ai#158](https://github.com/toss-delta-final/jarvis-ai/issues/158)) | 배포 manifest·rollback([#155](https://github.com/toss-delta-final/jarvis-ai/issues/155)) · DB backup·restore·migration 복구 훈련([#156](https://github.com/toss-delta-final/jarvis-ai/issues/156)) · readiness·alert·incident runbook([#157](https://github.com/toss-delta-final/jarvis-ai/issues/157)) · production 보안·secret rotation 점검([#158](https://github.com/toss-delta-final/jarvis-ai/issues/158)) — `Production Readiness` 마일스톤, 전부 P1. 발표 주장(C1~C4) 증명에는 불필요하나 이는 **운영 릴리스에 불필요하다는 뜻이 아니다** — 그 우선순위 판단은 이 문서 범위 밖이다(§11 참조) | 운영 릴리스 착수 시 별도 트랙에서 판단 |
| **사람 평가([jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153))** | P1, optional evidence. 1차 통과 조건에 넣지 않는다 | #153 완료 시 §7 목록에 추가 |
| **홈 추천 I-22 `rank_candidates` 품질** | `evals/README.md` 커버리지 지도의 미착수 칸 | 하네스 신설 후 |
| **하이브리드 검색·다중 인스턴스** | `docs/roadmap.md`의 post-MVP 확정 확장 사항(하이브리드 검색: "pgvector만으로 키워드 정확도가 부족할 경우"; 다중 인스턴스: "MVP는 단일 인스턴스 전제") | roadmap 재검토 조건 충족 시 |

**판매자 제외에 따른 후속 이슈 범위 확정**(이슈 완료 조건): 판매자 품질을 1차 주장에서
제외했으므로, [jarvis-ai#128](https://github.com/toss-delta-final/jarvis-ai/issues/128)과
판매자 평가 산출물은 P0로 승격하지 **않는다**. [jarvis-ai#151](https://github.com/toss-delta-final/jarvis-ai/issues/151)·
[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)·
[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)의 범위는
**구매자(`buyer_recommend` 등) 중심**으로 명시한다 — §2 C4·§7 산출물 5·6·§9 G4 가 전부 buyer
시나리오만 참조한다. 판매자 성능 참고 측정 1건(`20260802T140556535202Z-local-spring-seller`)은
비주장 부록으로만 남긴다.

---

## §9 release gate

**G0 는 전 claim 공통.**

| 게이트 | 조건 | 현재 판정 |
|---|---|---|
| **G0** | 인용 위생: 발표·리포트의 모든 수치가 (a) baseline 경로, (b) run manifest 의 필수 6항(§6), (c) dataset 세대를 동반한다. 세대가 다른 수치를 같은 표에서 빼지 않는다. unknown 비용은 unknown 으로 남긴다 | `pass`(본 문서가 준수) |
| **G1**(C1) | `pipeline-pipelineNoop` nDCG@10 paired bootstrap 95% CI 하한 > 0, guest·member 슬라이스 Holm 보정 후 유의 | `pass`(하한 0.0632) |
| **G2**(C2) | `mainIntent` ≥ 0.95 **and** `screenNoHallucination` = 1.0, 동일 fixture 세대 2 run 이상 | `pass`(출고판 adopted-1/2: 0.9833/0.9792, 1.0/1.0). 추가 확인: `merged-1`·`merged-2`(대조팔, 출고판 아님) 2 run 도 직접 파일로 확인함 — `merged-1` mainIntent **239/240=0.9958**(`run.timestamp=2026-08-07T14:08:47.986019+00:00`), `merged-2` mainIntent **238/240=0.9917**(`run.timestamp=2026-08-07T14:26:32.905846+00:00`), 둘 다 `screenNoHallucination=8/8`. 두 팔 모두 2-run 요건을 각자 충족하지만, **발표 인용은 출고판(adopted-*)** — §2 C2·§4 참조 |
| **G3**(C3) | `forbiddenOrRecentInclusion`=0 **and** `intentContradiction`=0 **and** production arm(`pipeline`) `hardConstraintViolationRate`=0.0(+`hardFailureCount` 병기) | `pass` |
| **G4**(C4) | staging([jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152))에서 ttft p95 ≤ 10,000ms, buyer total p95 ≤ 30,000ms, success ≥ 0.99, timeout = 0, 쿼리당 비용 기록 | **`pending(#152)`**(측정 없음) |

---

## §10 하위 이슈 연결과 완료 순서

| 이슈 | 역할 | 상태 | 이 문서와의 관계 | 순서 |
|---|---|---|---|---|
| [jarvis-ai#128](https://github.com/toss-delta-final/jarvis-ai/issues/128) | 실사용자 행동 통계 더미 로그·판매자 성능 테스트 자산 | CLOSED | §8 판매자 제외 판단의 배경 자산(`data-analysis/`) | 선행 |
| [jarvis-ai#133](https://github.com/toss-delta-final/jarvis-ai/issues/133) | rerank degrade 투명 고지 | CLOSED | C4 의 `degrade_rate` 해석 배경 | 선행 |
| [jarvis-ai#140](https://github.com/toss-delta-final/jarvis-ai/issues/140) | 추천 provenance·`recommendationRequestId` | CLOSED | §7 산출물 7 | 선행 |
| [jarvis-ai#144](https://github.com/toss-delta-final/jarvis-ai/issues/144) | 실제 모델 golden 평가·회귀 리포트 | CLOSED | C1 배경 자산(`evals/model_eval/`) | 선행 |
| [jarvis-ai#146](https://github.com/toss-delta-final/jarvis-ai/issues/146) | pipeline/scoring/single-call ablation | CLOSED | §2 C1, §3-1 정본 자산 | 선행 |
| [jarvis-ai#151](https://github.com/toss-delta-final/jarvis-ai/issues/151) | AWS staging benchmark runner·로컬 baseline | CLOSED | §2 C4, §5 표 B 정본 자산 | 선행 |
| [jarvis-ai#275](https://github.com/toss-delta-final/jarvis-ai/issues/275) | LLM teacher 도입 판단 조사 | CLOSED | §1 C1 동기("trivial baseline 미등록" 문제 제기) | 선행 |
| [jarvis-ai#328](https://github.com/toss-delta-final/jarvis-ai/issues/328) | 평가 커버리지 지도·공통 규약(8항) | CLOSED | §0 인용 규율의 정본 | 선행 |
| [jarvis-ai#333](https://github.com/toss-delta-final/jarvis-ai/issues/333) | buyer golden v2 후보 깊이·hard negative·slice quota | CLOSED | §2 C1·§7 산출물 1 정본 자산 | 선행 |
| **[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152)**(P0) | timeout 조정 후 AWS staging 최종 benchmark | OPEN | §2 C4, §9 G4 의 candidate 를 만든다 | **실행 1순위** |
| **[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)**(P0) | 최종 평가 리포트·발표 산출물 생성 | OPEN | 이 문서의 claim-evidence matrix·release gate 를 발표 형태로 만든다 | **실행 2순위**(#152 이후) |
| [jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)(P1, optional) | blind pairwise human evaluation | OPEN | §5 표 B, §8 — optional evidence | **병행**(1차 통과 조건 아님) |

**완료 순서**: **[jarvis-ai#139](https://github.com/toss-delta-final/jarvis-ai/issues/139)(본 문서) →
[jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152) →
[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)**.
[jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)은 병행이며 1차 통과
조건이 아니다.

---

## §11 P0 재검토 결과

이슈 완료 조건: "핵심 주장 확정 후 P0 목록을 다시 검토해, 주장에 필요하지 않은 기능은 P1 이후로
내린다."

> **재확인 결과가 조사 시점(`DECISION-PROPOSAL-139.md` 작성, 2026-08-10)과도, 이 문서 초판
> 작성 시점과도 다르다.** `gh issue list --label post-mvp --state open --limit 100`을
> **2026-08-10T08:25:11Z**에 다시 실행한 결과, 열린 `post-mvp` 이슈는 **24건**이다(초판이 적은
> "26건"은 오기 — 그 사이 목록이 다시 움직였다). 원 결정안이 검토한 13건 중 **#150 은 그 사이
> CLOSED 로 바뀌어 판정 대상에서 제외**한다(`gh issue view 150` → `CLOSED [feat] 개인화 graph
> projection·control API 구현`). 나머지 23건 전부를 아래 표에서 판정한다.

| 이슈 | 주장에 필요한가 | 조정 |
|---|---|---|
| [jarvis-ai#464](https://github.com/toss-delta-final/jarvis-ai/issues/464) | 아니오 — C2 는 현 수치로 성립, 약축 개선은 주장 밖 | P1 이후로 내림 |
| [jarvis-ai#463](https://github.com/toss-delta-final/jarvis-ai/issues/463) | 아니오 — 위와 동일 | P1 이후로 내림 |
| [jarvis-ai#434](https://github.com/toss-delta-final/jarvis-ai/issues/434) | 아니오 — 계약 개정 필요, 주장 무관 | P1 이후로 내림 |
| [jarvis-ai#431](https://github.com/toss-delta-final/jarvis-ai/issues/431) | 아니오 — underspecified 기본 on 전환은 주장 밖(§3-3 참조) | P1 이후로 내림 |
| [jarvis-ai#415](https://github.com/toss-delta-final/jarvis-ai/issues/415) | 아니오 — ruff format, 주장 무관 | **개발 위생**(우선순위 조정 대상 아님) |
| [jarvis-ai#350](https://github.com/toss-delta-final/jarvis-ai/issues/350) | 아니오 — 판매자 `recommend` 다각화 Epic, §8 판매자 제외와 정합 | P1 이후로 내림 |
| [jarvis-ai#298](https://github.com/toss-delta-final/jarvis-ai/issues/298) | 아니오 — 판매자 분석 파이프라인 SOP 개편, §8 판매자 제외와 정합 | P1 이후로 내림 |
| [jarvis-ai#288](https://github.com/toss-delta-final/jarvis-ai/issues/288) | 아니오 — first-event 예산 재설계는 C4 와 인접하나 C4 통과 기준은 staging TTFT p95(§9 G4)이지 first-event 예산이 아니다 | 주장 밖 — C4 인접 주제, 별도 트랙 |
| [jarvis-ai#266](https://github.com/toss-delta-final/jarvis-ai/issues/266) | 아니오 — 판매자 레인 타임아웃 정합 버그, §8 판매자 제외와 정합 | P1 이후로 내림 |
| [jarvis-ai#264](https://github.com/toss-delta-final/jarvis-ai/issues/264) | 아니오 — 소모품/비소모품 재구매 UX, C1~C4 무관 | 주장 밖 |
| [jarvis-ai#259](https://github.com/toss-delta-final/jarvis-ai/issues/259) | 아니오 — C2 는 출고판 현 수치(0.979~0.983)로 이미 성립, 라우팅 정확도 개선안 비교는 주장에 불필요. 다만 §2 C2 의 약축과 같은 주제라 "약축 후속" 링크 대상으로 **§2 C2 본문에 추가**했다(아래 참조) | 주장 밖 — §2 C2 약축 후속으로 링크 |
| [jarvis-ai#240](https://github.com/toss-delta-final/jarvis-ai/issues/240) | 아니오 — **여전히 OPEN**(`gh issue view 240` 확인, 초판·Codex 조사의 "CLOSED" 판정은 오류였다). `switchLegacy2`·`general` 약축이 이 이슈가 다루는 PENDING_CART 규칙군과 겹친다 — §2 C2 본문을 정정했다(아래 참조) | 주장 밖 — §2 C2 약축 후속으로 링크 |
| [jarvis-ai#185](https://github.com/toss-delta-final/jarvis-ai/issues/185) | 아니오 — 판매자 턴당 LLM 비용 측정, §8 판매자 제외와 정합(C4 는 buyer 시나리오만 다룬다) | P1 이후로 내림 |
| [jarvis-ai#158](https://github.com/toss-delta-final/jarvis-ai/issues/158) | 아니오 — production 보안·secret rotation 최종 점검. 발표 주장(C1~C4) 증명에는 불필요하나 **운영 릴리스에는 필요할 수 있다** — 그 판단은 이 문서 범위 밖이다 | 주장 밖 — 운영 릴리스 트랙(우선순위 판단은 본 문서 범위 밖) |
| [jarvis-ai#157](https://github.com/toss-delta-final/jarvis-ai/issues/157) | 아니오 — readiness·alert·incident runbook. 위와 동일 사유 | 주장 밖 — 운영 릴리스 트랙(우선순위 판단은 본 문서 범위 밖) |
| [jarvis-ai#156](https://github.com/toss-delta-final/jarvis-ai/issues/156) | 아니오 — AI PostgreSQL backup·restore·migration 복구 훈련. 위와 동일 사유 | 주장 밖 — 운영 릴리스 트랙(우선순위 판단은 본 문서 범위 밖) |
| [jarvis-ai#155](https://github.com/toss-delta-final/jarvis-ai/issues/155) | 아니오 — 배포 manifest·rollback 절차. 위와 동일 사유 | 주장 밖 — 운영 릴리스 트랙(우선순위 판단은 본 문서 범위 밖) |
| [jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154) | 예 — 전 claim 의 최종 산출물 | **P0 유지** |
| [jarvis-ai#153](https://github.com/toss-delta-final/jarvis-ai/issues/153) | 보조 — optional evidence | P1 유지 |
| [jarvis-ai#152](https://github.com/toss-delta-final/jarvis-ai/issues/152) | 예 — C4 정본(§2·§9 G4) | **P0 유지** |
| [jarvis-ai#150](https://github.com/toss-delta-final/jarvis-ai/issues/150) | — | **CLOSED — 판정 대상 아님**(2026-08-10T08:25:11Z 확인) |
| [jarvis-ai#139](https://github.com/toss-delta-final/jarvis-ai/issues/139) | 예 — 본 문서 | **P0 유지**(본 문서로 충족) |
| [jarvis-ai#131](https://github.com/toss-delta-final/jarvis-ai/issues/131) | 아니오 — §8 판매자 제외 | P1 이후로 내림 |
| [jarvis-ai#122](https://github.com/toss-delta-final/jarvis-ai/issues/122) | 아니오 — §8 판매자 제외 | P1 이후로 내림 |
| [jarvis-ai#121](https://github.com/toss-delta-final/jarvis-ai/issues/121) | 아니오 — `blocked:spring`, admin 범위 | P1 이후로 내림 |

**운영 릴리스 트랙과 발표 주장 트랙은 다른 축이다.** #155~#158(Production Readiness 마일스톤,
P1)은 "발표에서 증명할 주장에 필요한가" 기준으로는 전부 아니오이지만, 이는 이 이슈들이
불필요하다는 뜻이 아니다 — 운영 배포 준비는 이 문서의 판정 범위 밖이며, §8 "운영 관측 인프라"
제외 항목과 성격이 겹친다(둘 다 api-spec 개정·인프라 구축이 선행돼야 하는 post-MVP 트랙).

**라벨·필드를 실제로 바꾸지 않았다.** 이 표가 결정 기록이다.

---

## §12 가정과 미해결

- 핵심 주장 4개(C1~C4)와 판매자 제외는 부모(사람) 결정 대상이었으나, escalation 채널이
  `dispatch_inactive`로 닫혀 있어(`orca-ide orchestration ask` 실패) 오케스트레이터의 추천안으로
  진행했다. 근거는 `DECISION-PROPOSAL-139.md`(저장소 밖, 워크트리 산출물)이며, 요지는 다음과
  같다: 후보 7개(A~G) 중 근거가 확보된 A(C1)·D(C2)·C+E(C3)·F(C4) 4개를 채택하고, B(개인화 품질
  향상)는 라이브 실측이 inconclusive 라 제외, G(파이프라인 vs 단일 LLM)는 최신 골든셋에서
  inconclusive 라 제외해 §3 negative result 로 전환했다. 판매자 품질은 하네스 부재·검증 공백
  (SELLER-FINAL-RISKS V1/V3/V4)을 근거로 1차 주장에서 제외했다. **부모 결정이 오면 §1·§8 을
  개정한다.**
- **(라운드 2 정정)** §11 의 열린 `post-mvp` 이슈 목록은 초판 작성 시점("26건") 대비 다시
  움직여 **24건**이 됐고(2026-08-10T08:25:11Z 재확인), 원 결정안이 검토한 13건 중 **#150 은 그
  사이 CLOSED**로 바뀌었다. 초판이 판정을 미루었던 나머지 12건은 이번 라운드에서 이슈 제목·본문을
  직접 읽고 전부 판정해 §11 표에 반영했다 — "원 논의 대상 밖"이라는 이유로 판정을 생략하지
  않는다. 새로 판정 대상이 된 12건 중 [jarvis-ai#240](https://github.com/toss-delta-final/jarvis-ai/issues/240)·
  [jarvis-ai#259](https://github.com/toss-delta-final/jarvis-ai/issues/259)는 §2 C2 의 약축
  후속으로 명시적으로 링크했고, [jarvis-ai#155](https://github.com/toss-delta-final/jarvis-ai/issues/155)~
  [jarvis-ai#158](https://github.com/toss-delta-final/jarvis-ai/issues/158)(Production Readiness)은
  "발표 주장에 불필요"와 "운영 릴리스에 불필요"를 구분해 후자는 판단하지 않았다.
- Codex 교차검증(`codex-C-crosscheck.md`)에서 "부분 지지"로 남은 항목: 검증 2(개인화 효과의
  방향 — arm 대조가 다르다는 사실은 확인되나 인과 설명은 미확인). 검증 3의 "약축을 단독으로
  다루는 열린 이슈는 미확인"은 **라운드 2에서 해소** — [jarvis-ai#240](https://github.com/toss-delta-final/jarvis-ai/issues/240)이
  여전히 OPEN 이고(Codex·초판 모두 "닫힘"으로 오판), [jarvis-ai#259](https://github.com/toss-delta-final/jarvis-ai/issues/259)와
  함께 그 자리를 채운다는 것을 `gh issue view`로 직접 확인해 §2 C2 에 반영했다.
- 이 문서 작성 중 발견한 **패킷 수치 불일치 1건**(§2 C3, ablation `comparison.json`의
  `hardConstraintViolationRate` — packet 은 "3-arm 모두 0.0"이라 적었으나 실측은 `scoring`
  0.029850746268656716, `single_call` 0.014925373134328358 로 0 이 아니다. `pipeline`(production
  arm)만 0.0)는 파일 값으로 정정해 반영했다. 상세는 워크트리 오케스트레이터 보고 참조.
- **(라운드 2 발견)** [jarvis-ai#361](https://github.com/toss-delta-final/jarvis-ai/issues/361)
  병합(`cc9f6b17`)이 `evals/personalization/baselines/dev-v2/*`(overreach.json·comparison.json·
  run_manifest.json 등)를 새 commit 위에서 **재실행해 덮어썼다** — 데이터셋이 자라(color 축
  케이스 추가) `pairedCount`·`includedCount`가 바뀌었다. §2 C3·§3-2 의 해당 수치를 현재 파일
  값으로 갱신했다(상세는 F3·F4 처리 내역 참조). `ablation`·`intent_probe`·`benchmark`·
  `personalization/baselines/live-v1`은 이 병합으로 변경되지 않았다(`git diff cc9f6b17^ cc9f6b17
  --stat` 로 확인) — C1·C2·C4·§2 C3 의 라이브 대조 수치는 그대로 유효하다.
- **⚠️ in-flight 레인 경고**(`gh pr view 564`로 직접 확인, 2026-08-10): **PR #564**
  (`NyongCho/fix-443-465-categoryqueries-axes` → `dev`, 이슈 #443·#465)가 **OPEN**이고 아직
  `dev`에 병합되지 않았다(`mergeStateStatus=BEHIND`). 이 PR 은 `decompose` 파싱을 바꾸고(조건
  전용 턴의 총칭 leg 제거) `intent_probe`·`underspecified_probe` 에 신규 축과 2026-08-10 자
  신규 baseline 을 더한다. **병합되면** §2 C2 가 인용한 `…-v6-adopted-*` 계보는 더 이상 출고판이
  아니게 되고, §3-3 의 과소지정(`judgmentAccuracy`) 수치도 낡는다 — §4 "'최신 timestamp' ≠
  '출고판' 규칙"과 [jarvis-ai#361](https://github.com/toss-delta-final/jarvis-ai/issues/361)
  사례(위 항목)와 같은 함정이다. **[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)는
  발표 전 PR #564 병합 여부를 확인하고, 병합됐다면 §2 C2 의 baseline 과 §3-3 의 과소지정 수치를
  재지정해야 한다. 병합 전(현재 이 문서가 인용한) 수치를 병합 후에도 그대로 인용하지 않는다.**

---

## §13 과정 평가 항목 대조

이 절은 **[jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154)(발표 산출물)를
위한 대조표**다 — 과정 배포 자료 「LLM Agent 프로젝트 가이드 v2」(10p)가 명시한 평가 항목을
이 문서의 claim·산출물에 연결할 뿐, §1~§9 의 평가 기준(claim·release gate)을 바꾸지 않는다.

### 표 1 — 평가 항목(가이드 p.3) ↔ 이 문서에서 대응하는 것

| 평가 항목(가이드 p.3) | 이 문서에서 대응하는 것 | 상태 |
|---|---|---|
| 기획 — 문제 정의와 해결책의 타당성·도구 설계와 사용자 시나리오 | §0 목적·범위·독법, §1 핵심 주장 4개(C1~C4)의 "왜 이 주장인가" 열 | 대응 |
| 협업 능력 — 역할 분담·Git 사용·주간보고의 성실성 | — | **이 문서 범위 밖** |
| 기술 난이도 — Agent 파이프라인 구성요소의 깊이(Tool-use / Memory / Multi-Agent) | §1 C1(에이전트 경로 vs no-op)·§5 표 A(하네스별 metric) — 파이프라인 구성 자체의 설계 난이도는 이 문서가 직접 다루지 않고 claim 근거로만 간접 반영 | 부분 대응 |
| 완성도 — 엔드투엔드 동작·예외 처리·FastAPI 배포·**평가 지표 제시 여부** | §2 claim-evidence matrix, §6 run manifest 필수 6항, §9 release gate. "평가 지표 제시 여부"는 이 문서가 가장 직접 답하는 자리다 | 대응 |
| 발표 전달력 — 시연 영상의 설득력·**Q&A 대응**·발표 자료 구조화 | §7 최종 발표 산출물 목록. **Q&A 대응은 §3 negative result 가 방어 자산이다** — "증명 못 한 것"을 먼저 정직하게 적어 두면 평가자의 반박 질문에 이미 답이 있는 상태로 발표한다 | 대응(§3 이 핵심) |

### 표 2 — 제출 체크리스트(p.10) ↔ 저장소 자산

| 제출물 | 저장소 대응 | 상태 |
|---|---|---|
| README.md(실행방법·API Key 설정·데이터 출처) | `README.md` — "시작하기" 절이 실행법(`.env.example` → `.env`)과 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` 설정을 다룬다. 데이터 출처는 명시적 절이 없고 아키텍처 절("Spring 백엔드가 상품 원본 데이터 소유")에서 간접적으로만 드러난다 | 있음(데이터 출처는 부분) |
| requirements.txt / pyproject.toml | `pyproject.toml`·`uv.lock` 이 있다(`requirements.txt` 는 없음 — 이 프로젝트는 `uv` 패키지 매니저를 쓴다, CLAUDE.md "명령어": `uv sync`) | 있음(uv 관례로 대체) |
| FastAPI 서버(실행법·엔드포인트 문서) | `app/main.py::FastAPI(...)`(직접 확인), 실행 명령 `uv run uvicorn app.main:app --reload`(README·CLAUDE.md), 엔드포인트 계약은 `docs/api-spec.md` | 있음 |
| 평가셋 CSV + 평가 결과 노트북 | `evals/` 하네스와 baseline 산출물(§7)이 CSV·JSON·Markdown 리포트로 나온다. **"노트북"(`.ipynb`) 형태는 이 저장소에 없다** — `git ls-files '*.ipynb'` 결과 0건(직접 확인) | CSV/리포트는 있음, 노트북 없음 |
| 시연 영상 3~5분 | — | **이 저장소 범위 밖**([jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154) 산출물) |
| 발표용 PPT | — | **이 저장소 범위 밖**([jarvis-ai#154](https://github.com/toss-delta-final/jarvis-ai/issues/154) 산출물) |

### 대조에서 드러난 것

**요구 수준 대비 초과 달성한 축**: 가이드의 평가 권장치는 "자체 평가셋 20문항" 대(p.7 프로젝트
A "도구 호출 정확도 10문항 수기 평가", p.8 프로젝트 B "20문항") 수준인데, 이 프로젝트의 golden
dataset 은 **dev 109건 + holdout 24건**(`evals/goldenset/audit/leakage_report.json`의
`summary`: `devCases=109`, `holdoutCases=24`, `violationCount=0`, `warningCount=16`)이고,
단일 수기 평가가 아니라 paired bootstrap CI·trivial baseline·Holm 보정까지 갖췄다(§2·§5).

**용어 번역이 필요한 축**: 가이드가 말하는 "Tool-call 정확도 · Trajectory"(p.10)에 해당하는
측정을 이 프로젝트는 `intent_probe`(라우팅 정확도)·`filter_axes`(필터 추출 precision/recall/F1)로
하고 있다 — 정의가 정확히 같지는 않지만 같은 것을 다른 이름으로 재는 것에 가깝다. 발표에서는
평가자 언어(Tool-call 정확도·Trajectory)를 병기하는 편이 전달에 낫다.

**이 문서가 커버하지 않는 평가 축**: 가이드 p.5 원문 "Agent 시스템의 특성상 도구 설계·상태
관리·안전장치(Guardrail/HITL)·동작 평가가 중요한 평가 포인트 입니다"가 안전장치를 중요 항목으로
든다. 이 문서의 **C3 는 하드 제약 위반 0 과 개인화 유출 통제(§2 C3)를 다루지만, 판매자 전 쓰기
HITL 은 §8 에서 1차 주장 범위 밖으로 뒀다** — 판매자 제외 결정의 대가가 안전장치(Guardrail/HITL)
평가 축에서 나타난다. 이 절에서 그 결정을 뒤집지 않는다 — 재검토가 필요하면 §12 의 부모(사람)
결정 대기 항목에 걸린다.
