# LLM 기반 서비스 평가·개선 방법론 조사 — 이슈 #330 · 작성일 2026-08-05 · 상태(APO `no-go`(현시점) / 비결정성 회귀 게이트 `go` / judge 확대 `조건부`)

## 요약

**판정 3건.**

- **자동 프롬프트 최적화(APO): `no-go`(현시점).** 최적화 대상 metric 이 지금 판별력이 없다 — `teacher − no-op` 이 `inconclusive`(N=18, 95% CI [−0.122337, 0.231914])인 계측기 위에서 자동 루프를 돌리면 노이즈를 최적화한다.
- **비결정성 하의 회귀·운영 게이트: `go`.** 문헌(Ouyang 2023·Miller 2024·Madaan 2024)이 우리가 이미 채택한 규약(provider seed 강제 불가, temperature 0 도 "결정론"이라 안 부름, 실 LLM 하네스 CI 미포함)과 정확히 같은 결론을 낸다 — 판정 규칙을 사전 등록으로 승격하는 것만 새로 한다.
- **LLM-as-judge 확대: `조건부`.** 문헌의 position bias(Wang 2024: 80개 중 66개 역전)가 우리 teacher 에서 그대로 실측됐다(#275 E3, tau 0.3654) — 위치 무작위화·순열 자기일관성과 #153 인간 표본 보정을 전제로만 확대한다.
- **이슈 전제 정정 1건**: 이슈 본문이 KDD 서베이(Mohammadi et al., arXiv:2507.21504)에 "generation→execution→evaluation→compliance 4단계 프레임과 4대 결함"이 있다고 적었으나, 2026-08-05 본문 확인 결과 그런 구조는 없다 — 실제는 평가 대상 4종×평가 과정 5요소의 2차원 분류다(§2).

관련 판단: 랭킹 학습 자체의 no-go 는 [RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md)(이 조사가 인용하는 실측 원천), 평가 커버리지 지도·공통 규약은 [이슈 #328](https://github.com/toss-delta-final/jarvis-ai/issues/328)을 본다. 이 문서는 그 지도 위에서 "우리가 아직 안 하는 것"의 문헌 근거만 채운다 — 새 하네스·새 코드는 만들지 않는다.

## 1. 이 리포의 실측 현황

### 1.1 평가 자산 지도

에픽 #328 이 확정한 커버리지 지도(단계 축)에 이 조사가 채우는 축(Reliability, §2)을 더하면:

| 단계 | 자산 | 상태 |
|---|---|---|
| intent 라우팅 | `evals/intent_probe`(발화 46×컨텍스트 9=유효 74셀×N=8=712콜, 런당 $0.11·16~18분) | 있음 |
| 개인화 주입·과반영 | `evals/personalization`(5-arm×weight 5=25셀) | 있음 |
| 지연·예산 | `evals/first_event_budget`, `evals/benchmark` | 있음 |
| e2e 추천 품질(결정론) | `evals/metrics`+`evals/goldenset`(dev 31+holdout 12=43건, 순위 판별력 있는 건 18) | 있음(판별력 부족) |
| e2e 실모델 | `evals/model_eval`(예산 상한 800콜/3천만 token/$20, config 주입) | 있음 |
| 경로 비교 | `evals/ablation`(#146 사전 등록) | 있음 |
| 카테고리 매핑·선택 | 없음 | 자식 #331 |
| 니즈 전개(legs) | 없음(#198 지표가 로그로만 관측) | 자식 #332 |
| 필터 추출 단독 | e2e Filter Accuracy 뿐(합집합 분모 한계) | 자식 #334 |
| 기능 조합 커버리지 | 없음 | 자식 #335 |
| **Reliability(시간·시나리오에 걸친 신뢰성)** | **지도에 없음** — 이 조사가 지적하는 갭 | §5 가 다룸(자식 이슈 미배정) |

### 1.2 사고 이력 6건 요약

| 사고 | 무슨 일이 났나 | 이 문서 어느 절 |
|---|---|---|
| #234 / #240 | 프롬프트 채택 판정에 쓴 측정 스크립트를 커밋하지 않아 유실, 서로 다른 정답지로 채택 판정이 뒤집힘(되물음 위치 1번↔2번만으로 정답률 8/8 ↔ 3/8) | §5 (측정 하네스 커밋, 앵커 고정) |
| #260 | `FakeLLM` 유닛 테스트는 프롬프트를 어떻게 바꿔도 통과 — 라우팅 정확도는 실 LLM 반복 분포로만 측정(53셀×N=8, 축당 ±2) | §5 (결정론 CI / 확률 수동 이원화) |
| #198 | 프롬프트에 지시를 얹었다가 기존 성공 케이스가 3/3 → 1/3 로 희석 | §4 (metric 없이 문면만 바꾼 회귀의 원형, #84 실측 선례와 같은 급) |
| #119 | 프로필 주입이 하드필터로 승격되며 단계 간 의도치 않은 결합 — 9턴 중 9턴 유출 실측 | §2 (e2e 만으로는 단계 귀속이 안 됐던 사례), §6 (DIR 예시) |
| #275 | teacher 후보 제시 순서만 바꿔도 top-1 이 47.2% 만 유지(같은 순서 반복은 63.9%, tau 0.3654 대 0.6463) | §3 (judge/teacher 위치 편향 실측) |

## 2. 에이전트·파이프라인 평가 프레임워크 (범위 ①)

### 이슈 전제의 정정

이슈 #330 본문은 Mohammadi et al.(KDD 2025, arXiv:2507.21504)에 "generation → execution → evaluation → compliance" 4단계 프레임과 "과제 커버리지·지표 파편화·재현 비용·인간 가치 정렬"이라는 4대 결함 명명이 있다고 적었다. **2026-08-05 본문 확인 결과 그런 4단계 프레임·4대 결함 명명은 존재하지 않는다.** 실제 구조는 2차원 분류다 — 평가 **대상**(Agent Behavior / Capabilities / Reliability / Safety & Alignment) × 평가 **과정**(Interaction Mode / Evaluation Data / Metrics Computation / Tooling / Contexts). [RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §4(c)가 이슈 전제를 정정한 선례를 따라 같은 톤으로 정정한다 — KDD 2025 게재 자체는 DOI(10.1145/3711896.3736570)로 확인됐다.

### 커버리지 지도에 대응

이 2차원 분류를 §1.1 지도에 대응시키면, 우리 지도는 평가 **대상**(단계) 축은 채워가는 중이다(자식 #331~#335 가 카테고리 매핑·니즈 전개·필터·기능 조합을 각각 메운다). 그러나 **Reliability(시간·시나리오에 걸친 신뢰성)** 축은 지도에 아예 없다 — 이것이 §5(비결정성 하의 회귀·운영)가 다루는 갭이다. Mohammadi 서베이가 엔터프라이즈 갭으로 꼽는 항목(RBAC, 시간·시나리오에 걸친 신뢰성, 동적 long-horizon 상호작용, 도메인 정책·컴플라이언스)중 우리가 지금 근접한 것은 신뢰성 축뿐이며, 나머지 셋은 이 조사의 비범위다.

### 개발자용 평가 프레임워크 관점

Yehudai et al.(arXiv:2503.16416)의 5관점(핵심 역량/응용별 벤치마크/범용 에이전트 평가/벤치마크 차원 분석/개발자용 평가 프레임워크) 중 마지막 관점에서 보면, 우리 `evals/` 6종 하네스(`intent_probe`·`personalization`·`first_event_budget`·`benchmark`·`model_eval`·`ablation`)가 이미 그 형태다. 서베이가 미해결로 꼽는 세 축과 우리 자산을 대조하면:

| 서베이가 미해결로 꼽는 축 | 우리 자산 |
|---|---|
| 비용-효율성 평가 | `model_eval` 예산 상한(800콜/3천만 token/$20, config 주입), `chat_request.costUsd` 관측 |
| 안전성 평가 | HCV 0 게이트(`personalization.hardFilterViolationMax`·`ablation`·release 게이트), `hard_filter.py` 결정론 컷 |
| 견고성 평가 | 반복 N=8(intent_probe)·N=5(ablation·model_eval), 반복 분산 실측(#275 E1-b, teacher nDCG sd 0.0678) |

셋 다 "없음"이 아니라 "이미 부분적으로 있음"이라는 점이 서베이 대비 우리 위치다.

### 사고 대응

#119(프로필 주입이 하드필터로 승격되며 단계 간 의도치 않은 결합, 9/9 유출)는 e2e 평가만으로는 이 결합이 어느 단계에서 났는지 귀속되지 않았던 사례다 — 결국 `personalization` 5-arm 이 rerank 단계만 분리한 뒤에야 "프로필 유출이 아니라 확률적 LLM 지터"로 재해석됐다(clean_rerank_only, 단건 CI 0 포함). 단계별+e2e 병행 평가와 척추 `caseId` 공유(에픽 공통 규약 ②)는 이 실패 모드에 대한 정확한 처방이다 — §5 가 인용하는 2026 년 layer-isolated 사례 연구(Zhang et al., arXiv:2606.11686)의 "집계 지표가 계층 결함을 가린다"는 관측과 같은 실패 형태다.

## 3. LLM-as-judge 신뢰성 (범위 ②)

### 문헌

Zheng et al.(NeurIPS 2023, arXiv:2306.05685)은 강한 judge(GPT-4)가 인간 선호와 80% 이상 일치("인간 간 일치와 같은 수준")한다고 보고하면서도, position bias·verbosity bias·self-enhancement bias·제한된 추론 능력을 편향으로 꼽는다. Wang et al.(ACL 2024, arXiv:2305.17926)은 제시 순서만 바꿔도 80개 질의 중 66개에서 LLM 심판의 품질 순위가 역전됨을 보인다. Tang et al.(NAACL 2024, arXiv:2310.07712)은 순열 자기일관성(여러 번 순서를 섞어 중심 순위를 취함)으로 GPT-3.5 7~18% 개선을 보고한다. Panickssery et al.(arXiv:2404.13076)은 LLM 평가자가 자기 출력에 높은 점수를 주고(인간 평가자는 동등 평가), 자기인식과 자기선호가 선형 상관을 보인다고 밝힌다. G-Eval(Liu et al., arXiv:2303.16634, 게재처 미확인)은 요약 과제에서 인간 Spearman 0.514 를 보고하면서 "LLM 기반 평가기가 LLM 생성 텍스트를 선호할 수 있다"는 문제도 예비 분석한다. Gu et al.(arXiv:2411.15594)의 서베이는 신뢰성 전략을 일관성 개선·편향 완화·시나리오 적응으로 정리한다. Shankar et al.(arXiv:2404.12272, 게재처 미확인)은 **criteria drift**를 발견한다 — "출력을 채점하려면 기준이 필요하지만, 채점하는 과정에서 기준을 정의하게 된다." 2026 년의 체계 평가(Soumik, arXiv:2604.23178, 게재처 미확인)는 완화 전략 9종을 비교하며 **그들 셋업에서는 style 편향(마크다운 선호 0.10–0.76)이 지배적이고 position 편향은 미미(≤0.04)**였다고 보고한다 — 편향의 크기·순위는 셋업 의존이라는 뜻이며, 그래서 이 문서가 문헌 일반론이 아니라 **우리 실측(#275 tau 0.3654)**을 구속력 있는 증거로 삼는다(이 논문을 position 편향 반박 근거로 쓰지 않는다).

### 우리 실측과의 대조가 핵심

[RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §5 의 E3 순서 민감도(Kendall tau-b **0.3654**, top-1 일치 **47.2%**, 같은 순서 반복은 tau 0.6463·top-1 63.9%)는 문헌의 position bias 가 우리 teacher(smart 티어 rerank)에서 그대로 실측된 것이다. teacher 반복 분산(E1-b, 케이스별 nDCG@10 표준편차 평균 **0.0678**)이 teacher−no-op 격차(**0.044734**)보다 크다는 것도 같은 함의다 — **teacher 를 라벨로 쓰면, 그 라벨 자체가 teacher 가 임의 순서를 이긴다는 신호보다 더 크게 흔들린다.**

### #153 보정 절차

Shankar 의 criteria drift 발견은 "judge 기준을 사전 확정하고 끝"이 성립하지 않음을 뜻한다 — 채점 기준은 출력을 보면서 계속 갱신된다. 이 조사는 그래서 **사람 채점 표본과의 주기적 재보정**을 권고한다: #153(blind pairwise human evaluation, 미실행)이 그 표본을 만들면, judge 판정과의 일치율을 주기적으로 재는 절차가 필요하다. Chatbot Arena(Chiang et al., arXiv:2403.04132)는 크라우드 질문의 다양성·판별력이 전문가 평가와 일치함을 검증한 대표 사례로, 라이브 인간 선호 평가 축의 문헌 참조점이다.

### 델타

- **이미 하는 것**: judge 를 골든 라벨 원천으로 쓰지 않음(#275 no-go 가 이미 막음), 순서 고정·앵커 고정(`intent_probe` `reaskProductListPosition: 2`).
- **새로 할 것**: judge 류 판정을 쓰는 실험은 위치 무작위화 또는 순열 자기일관성(비용 k배, [RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §6 비용 환산 준용) + #153 인간 표본과의 일치율 보고.
- **기각할 것**: judge 단독을 CI 게이트·릴리스 게이트로 쓰는 것 — HCV·결정론 metric 이 그 자리다(에픽 공통 규약 ③). judge 확대가 이 규약과 긴장하는 지점은 §7 에서 명시한다.

## 4. 자동 프롬프트 최적화 (범위 ③) — 판정 `no-go`(현시점)

### 문헌: 다섯 계열의 공통 구조

| 기법 | 핵심 | 전제 |
|---|---|---|
| APO 서베이(Ramnath et al., EMNLP 2025, arXiv:2502.16923) | 5부 통합 프레임워크로 기법 분류 | — |
| DSPy(Khattab et al., arXiv:2310.03714, 게재처 미확인) | 컴파일러가 주어진 metric 을 최대화하도록 파이프라인 최적화 | **프로그램적 metric** |
| MIPRO(Opsahl-Ong et al., EMNLP 2024, arXiv:2406.11695) | 모듈 라벨·그래디언트 없이 다운스트림 metric 을 surrogate 기반으로 최대화 | **다운스트림 metric 평가 반복** |
| OPRO(Yang et al., ICLR 2024, arXiv:2309.03409) | 이전 해+점수를 프롬프트에 넣어 매 스텝 후보를 채점·제안 | **매 스텝 채점** |
| APE(Zhou et al., arXiv:2211.01910, 게재처 미확인) | 지시문 후보 풀을 score function 으로 채점·선택 | **score function** |
| GEPA(Agrawal et al., ICLR 2026 Oral, arXiv:2507.19457) | 궤적을 자연어로 반성해 프롬프트 진화 + Pareto 선택 | 후보 궤적의 반복 평가 |

다섯 계열 전부 **"프로그램적 metric + 반복 평가"가 전제**다. 이 전제가 성립하지 않으면 최적화 대상이 없다.

GEPA 는 GRPO(RL) 대비 평균 +6%·최대 +20%, **롤아웃 35배 감소**, MIPROv2 대비 +10% 이상을 보고한다 — 롤아웃을 35배 줄여 이 절의 "실비용은 평가 캠페인" 논증의 비용 항을 크게 낮추는 방향의 최신 발전이다. 그러나 낮아지는 것은 평가 **횟수**이지 평가 **품질 요구**가 아니다. GEPA 초록에 "프로그램적 metric 필수"라는 명시 문구는 없지만, 후보 궤적을 제안·테스트하는 반복 구조인 이상 후보별 평가 신호는 여전히 전제이며, 판별력 없는 metric(CI 가 0 을 포함) 위에서는 반성(reflection)이 읽는 성공/실패 신호 자체가 노이즈라 같은 함정이 남는다 — 판정(`no-go`(현시점))은 불변이다. `dspy.GEPA` 로 DSPy 에 통합된다는 사실은 §9.1 조건 ④(신규 의존성 사람 승인)와 같은 게이트를 통과해야 한다.

### 핵심 논증 — "계측기 먼저"의 문헌 확인

최적화 대상 metric 이 (a) 판별력이 없거나(#328: `teacher−no-op` inconclusive, 순위 유효 18건) (b) 노이즈 대역이 효과 크기보다 크면(`intent_probe` 축당 ±2), 자동 최적화는 노이즈를 최적화한다. Miller(arXiv:2411.00640)는 "평가는 실험이다"라는 틀로 오차 막대·power 분석을 요구한다 — 후보 간 차이가 신뢰구간 안에 있으면 그 선택은 우연이다. 우리 primary metric(`nDCG@10`)의 `teacher−no-op` 95% CI [−0.122337, 0.231914]는 정확히 이 상태다. 프롬프트 패러프레이즈 간 성능이 크게 흔들린다는 관측(Mizrahi et al., TACL, arXiv:2401.00595)도 단일 프롬프트 점수의 취약성을 뒷받침한다.

### 우리 실측 선례 — #84·#198

`docs/lessons.md`(#84)가 기록한 문면 후보 6종의 **수동** 탐색조차 전 축 회귀 전/후 각 2회를 요구했고("1회는 노이즈와 구분되지 않는다, 축당 ±2"), 채택 기준은 "내 축이 좋아졌다"가 아니라 **"다른 축이 안 깎였다"**였다(인라인 필드를 지운 뒤 전환 축이 32/56 → 37/56 으로 되돌아온 것으로 인과를 닫았다). 이 비용(런당 ~$0.11·16~18분 × 후보 6종 × 2회)이 자동 최적화 루프에서는 후보 수가 수십 배로 늘어난다 — **APO 도입의 실비용은 옵티마이저가 아니라 평가 캠페인이다.**

#198(프롬프트에 지시 한 줄을 얹었다가 기존 성공 케이스가 3/3 → 1/3 로 희석)이 이 실패 모드의 원형이다 — metric 없이 문면만 바꾼 변경이 다른 축을 조용히 깎았고, 당시 처방도 문면 재수정이 아니라 **전용 호출로 분리**하는 것이었다(#84 가 같은 처방을 카테고리 판정에서 반복 확인했다). metric 없이 문면만 바꾸는 것의 위험은 사람이 한 번 바꿀 때도 이 정도인데, APO 문맥에서는 이 위험이 **자동으로 대량 반복된다**는 것이 이 절의 핵심 우려다.

### 부수 확인

Shumailov et al.(Nature 631, 2024)와 Gerstgrasser et al.(arXiv:2404.01413)의 model collapse 논의는 옵티마이저 LLM 이 만든 프롬프트를 같은 LLM 이 채점하는 루프의 위험을 각주 수준으로 환기한다 — 자기 출력을 자기가 채점하면 편향이 누적될 수 있다는 것이 이 조사의 §3(self-enhancement bias)와 겹친다.

### 판정: `no-go`(현시점)

전제 조건(§9 재평가 트리거)을 모두 충족해야 재검토한다: ① #333 골든셋 v2 로 primary metric 의 판별력 확보 ② 단계별 결정론 metric(에픽 자식 #331/#332/#334) 중 최소 2축이 커밋되어 목적함수로 쓸 수 있을 것 ③ 후보 수×반복 수×런 비용의 평가 예산을 사전 등록 ④ DSPy 등 신규 의존성은 사람 승인 게이트(이슈 비범위 재확인). 에픽 공통 규약과의 정합: trivial baseline(규약 ①) 위에서만 개선을 주장할 수 있고, 측정 하네스는 커밋한다(규약 ⑦).

## 5. 비결정성 하의 회귀·운영 (범위 ④) — 판정 `go`(권고 규약 채택)

이슈가 "검증된 문헌 앵커가 없다"고 명시한 절이다 — 이번 조사로 채웠다.

### 문헌

Ouyang et al.(arXiv:2308.02828)은 동일 요청 반복 간 test-output 완전 일치율이 3개 벤치마크에서 각 **75.76%/51.00%/47.56%**이며, **temperature 0 은 비결정성을 줄이지만 결정성을 보장하지 않는다**고 보고한다. Miller(arXiv:2411.00640)는 평가를 실험으로 다뤄 paired 분석·power 분석을 요구한다. Madaan et al.(arXiv:2406.10229)은 seed 분산 등 벤치마크 분산을 정량화하고, 단순 처방(선택형→완성형 재구성)이 ~7B 급에서 유효함을 보인다. Luo et al.(FSE 2014)의 flaky test 원전은 비결정 테스트를 SE 관점에서 다루는 원류다(재실행 전략 등). Mizrahi et al.(TACL, arXiv:2401.00595)은 650만 인스턴스·20모델·39과제 분석에서 단일 지시문 템플릿 평가가 프롬프트 변경에 따라 크게 흔들림을 보이고, 다양한 프롬프트 세트로 평가할 것을 권고한다.

### 우리 자산과의 대조

provider seed 는 강제할 수 없다(`model_eval`·`intent_probe` README 명시), 판매자 `temperature 0.0` 도 `app/core/config.py` 주석에서 "일관성 장치"라고만 부를 뿐 결정론이라 하지 않는다 — **문헌과 정확히 같은 결론을 리포가 이미 채택**하고 있다. 2026 년 production 에이전트 사례 연구(Zhang et al., arXiv:2606.11686, 게재처 미확인)는 **LLM 없는 결정론 하네스**(238케이스·23슬라이스, 2.39초)만 CI 회귀 게이트로 두는 같은 구조를 보고하고, 회귀 주입 실험에서 전체 지표는 −1.7~−5.9% 로 마스킹되지만 해당 계층 슬라이스는 −25~−91% 급락함을 보였다 — 에픽 공통 규약 ③(결정론 CI/확률 수동)과 ②(단계별 귀속, e2e 집계가 계층 결함을 가림)의 2026 년 독립 실증이다.

### 구체 권고

1. **CI 포함 여부: 실 LLM 하네스는 CI 에 넣지 않는다(현행 유지, 에픽 공통 규약 ③ 정합).** 근거는 비용(콜당 $0.0008~0.0010, 런당 ~$0.11)·시간(16~18분)·flaky 화(Luo 2014 의 재실행 비용·신뢰 저하). 결정론 오프라인 eval(`tests/eval` 3파일)은 현행대로 PR pytest 유지. 이 결정론 CI / 실 LLM 수동 이원화 자체의 기원은 #260(`FakeLLM` 유닛 테스트는 프롬프트를 어떻게 바꿔도 통과하고, 라우팅 정확도는 실 LLM 반복 분포로만 측정됨을 실측한 사고)이다.
2. **반복 수: `intent_probe` N=8/셀 유지 + 채택 판정은 독립 런 전/후 각 2회.** #84 가 이미 실측한 규약을 명문화한다. 1회는 축당 ±2 노이즈와 구분되지 않는다. 이항 축(정답률)은 이 규칙, 연속 축(`nDCG`)은 paired bootstrap 95% CI(resamples 2000, #146 규약)로 이원화한다.
3. **판정 규칙의 사전 등록 승격.** 현재 `intent_probe` 는 ±2 를 "경고"만 한다 — 프롬프트를 바꾸는 PR 의 채택 규칙을 사전 등록으로 승격한다: "대상 축 개선 확인 + **모든 보호 축**의 전/후 델타가 노이즈 대역(±2/N=8) 이내"(#84 의 "다른 축이 안 깎였다" 기준). 산출물(`report.md`·전/후 표)을 PR 에 첨부한다(에픽 공통 규약 ⑦과 정합).
4. **단계별 실패 귀속·버전 해시·비용 관측은 이미 규약이 있다 — 새로 만들 것이 아니라 승계할 것이다.** prompt 는 명명 버전 대신 전송 텍스트 SHA-256(`intent_probe` `manifest.py:77-80`, #260 요구), 비용·degrade 는 `chat_request`(`costUsd`·`degraded`·`lane`), 누락은 0 이 아니라 null/unknown(benchmark 정직성 규약). 이 규약들의 기원은 #234/#240(채택 판정에 쓴 측정 스크립트를 커밋하지 않아 유실됐고, 되물음 앵커 위치가 1번이냐 2번이냐만으로 정답률이 8/8 ↔ 3/8 로 반전됐던 사고)이다 — `intent_probe` 의 `reaskProductListPosition: 2` 고정과 앵커를 데이터 파일로 커밋하는 규약이 이 사고에서 나왔다.

### 온라인/오프라인 격차

오프라인(골든셋·fixture) → 라이브(실 LLM·실 DB) → production 관측의 3단 사다리가 이미 있다. 각 단은 자기 한계를 스스로 밝힌다 — `first_event_budget`(#277)은 "ScriptedLLM 이라 라우팅·decompose 헤드가 제외돼, 결과는 staging 성능 수치가 아니다", `personalization` live(#119)는 "지터와 유출의 구분은 repeats>1 필요"라고 명시한다. Chatbot Arena(Chiang et al.)는 온라인 평가 축의 문헌 대표 사례다. production A/B 는 에픽 비범위이며 #140 소관이다.

## 6. CheckList 원전과 규약 ⑥

에픽 공통 규약 ⑥(MFT/INV/DIR 구분)은 발명이 아니라 Ribeiro et al.(ACL 2020, arXiv:2005.04118)의 CheckList 가 정의한 세 테스트 유형의 채택이다 — MFT(Minimum Functionality Test, capability 안의 행동을 검사하는 단순 예제·라벨 모음)·INV(Invariance, 라벨 보존 섭동에 예측 불변을 기대)·DIR(Directional Expectation, 라벨이 특정 방향으로 변할 것을 기대). 우리 선례를 이 세 유형에 대응시키면: #119 의 "회원 recall ≥ 게스트" 회귀 테스트는 **DIR**(라벨 없이 방향만 기대), `intent_probe` 셀은 **MFT**(발화-컨텍스트별 정답 라벨), #223 의 "회원·게스트 decompose 프롬프트 바이트 동일" 불변식은 **INV**(라벨 보존 섭동에 예측 불변)다. INV·DIR 은 라벨링 공수 없이 커버리지를 늘리는 수단이라는 점이 CheckList 원전의 핵심 주장이며, 이것이 에픽 규약 ⑥이 그 형식을 그대로 가져온 이유다.

## 7. 우리 규약과의 델타 종합

에픽 공통 규약 8항(요지): ①trivial baseline 의무 ②척추 `caseId` 공유 ③결정론은 CI·확률은 수동 ④슬라이스 쿼터·표본 산정(분산 먼저) ⑤다중 비교 통제(primary 1개) ⑥MFT/INV/DIR 구분 ⑦측정 하네스 커밋 ⑧`datasetVersion`/`Hash`·baseline 재실행.

| 범위 | 이미 하는 것 | 새로 할 것 | 기각할 것 |
|---|---|---|---|
| ① 평가 프레임워크 | `evals/` 6종 하네스가 개발자용 평가 프레임워크 관점에 이미 대응(②⑦) | 커버리지 지도에 Reliability 축을 명시적으로 추가(④가 §5 표본 산정과 연결) | 이슈가 전제한 KDD "4단계+4대 결함" 프레임 채택(§2 정정 — 채택할 대상 자체가 없음) |
| ② judge 신뢰성 | judge 를 골든 라벨 원천으로 안 씀(#275 no-go), 순서·앵커 고정(`intent_probe`) | judge 판정 실험에 위치 무작위화/순열 자기일관성(④⑤) + #153 인간 표본 일치율 보고 | judge 단독을 CI/release 게이트로(③ 정합 — **judge 확대 규모가 커지면 ③과 긴장**, §8 판정에서 조건부로 제어) |
| ③ APO | 없음(도입 자체가 없음) — #84 의 수동 회귀 게이트(①⑦)가 사실상의 대체 | 없음(no-go 유지) — 착수 조건 충족 시 offline arm([RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §12 형식, 사람 승인 게이트) | 자동 최적화 루프 지금 도입, DSPy 등 신규 의존성을 사람 승인 없이 도입 |
| ④ 비결정성 회귀 | N=8+±2 경고, provider seed 강제 불가 인지, prompt SHA-256 해시, cost/degrade 관측, 실 LLM CI 미포함(③) | 판정 규칙 사전 등록 승격(⑤), 전/후 각 2회 명문화(④), 온라인/오프라인 3단 사다리 명시 | 실 LLM 하네스를 CI 로 편입, provider seed 강제를 시도 |

**충돌은 현재 설계 기준으로는 없다** — 예상대로 judge 확대(②)가 규약 ③(결정론은 CI, 확률은 수동)과 긴장하는 지점이 있지만, judge 판정을 CI/release 게이트로 쓰지 않고 exploratory metric 으로만 쓰는 한 이 긴장은 표면화되지 않는다. 실제로 쓰다 이 경계를 넘는 판단이 나오면(예: judge 점수를 회귀 게이트에 편입하려는 시도) 이 표를 갱신한다.

## 8. 판정

| 범위 | 판정 | 근거(한 줄) |
|---|---|---|
| ③ 자동 프롬프트 최적화 | `no-go`(현시점) | `teacher−no-op` CI 가 0 을 배제하지 못해(inconclusive) 최적화 대상 metric 이 없다 |
| ④ 비결정성 하의 회귀·운영 | `go`(권고 규약 채택) | 문헌이 이미 우리 규약과 같은 결론 — 판정 규칙 사전 등록 승격만 신규 |
| ② LLM-as-judge 확대 | `조건부` | 위치 편향이 우리 teacher 에서 실측(tau 0.3654) — 위치 무작위화·인간 표본 보정이 전제 |

## 9. 재평가 트리거

### 9.1 APO 착수 조건 (모두 충족해야 offline arm([RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §12 형식)을 시작한다)

1. **계측기 먼저** — #333 골든셋 v2 로 `teacher−no-op` 급 비교의 paired bootstrap 95% CI 가 0 을 배제할 것.
2. **목적함수 존재** — 단계별 결정론 metric(에픽 자식 #331/#332/#334) 중 최소 2축이 커밋되어 APO 의 목적함수로 쓸 수 있을 것.
3. **예산 사전 등록** — 후보 수×반복 수×런 비용의 평가 예산을 시작 전에 사전 등록. 예산 산정 시 GEPA(35배 저롤아웃)류를 우선 검토한다.
4. **의존성 승인** — DSPy 등 신규 의존성은 사람 승인 게이트(이슈 비범위 재확인).

**도입 금지 조건**(하나라도 참이면 시작하지 않는다):
- 조건 1 의 CI 가 0 을 배제하지 못한 채로 시작하려는 경우.
- 조건 2 의 목적함수 없이 대리(proxy) metric 으로 대체해 건너뛰려는 경우.
- 조건 3 의 예산 미등록 상태로 후보 탐색 규모를 사후 확장하려는 경우.
- 신규 ML/외부 의존성을 사람 승인 없이 도입하려는 경우.

### 9.2 judge 확대 조건부의 조건

- judge 판정을 골든 라벨이나 CI/release 게이트로 승격하지 않고 exploratory metric 으로만 쓸 것.
- 위치 무작위화 또는 순열 자기일관성을 적용하고, 비용 배수를 [RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md) §6(순열 자기일관성 k회 = 콜 수 k배 선형 환산) 방식으로 사전 산정할 것.
- #153 인간 표본과의 일치율을 주기적으로 보고할 것(Shankar criteria drift 대응).

**도입 금지**: judge 단독을 CI/release 게이트로 쓰려는 경우, 위치 고정 없이 단일 순서 결과만으로 채택 판정을 내리려는 경우.

### 9.3 비결정성 회귀 게이트 재검토 트리거

- 실 LLM 콜당 비용이 현재(≈$0.0008~0.0010) 대비 유의하게 하락해 CI 포함의 비용 정당성이 바뀌는 경우.
- provider 가 seed 강제를 지원하게 되는 경우(현재 미지원, `app/core/config.py` 근거).
- #333 골든셋 v2 로 판별력 있는 케이스가 늘어 §5 의 표본 요구치가 바뀌는 경우.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- 서지 검증: 참고 문헌 전 항목을 sub-orchestrator 가 2026-08-05 WebFetch/WebSearch 로 원문·초록·본문에서 직접 확인했다. 게재처를 확인하지 못한 항목은 "게재처 미확인"으로 표기하고 arXiv 로만 인용했다.
- 리포 실측: `evals/` 각 하네스의 README·baseline·config 와 `app/core/` 소스에서 직접 확인했다(경로·수치는 본문에 인용). 이 조사에서 별도 스크립트 실행·DB 조회·LLM 호출은 하지 않았다.
- 사고 이력: 이슈 #330 배경 절, `docs/lessons.md` 상단 항목(#84 포함), [RESEARCH-TEACHER-275.md](./RESEARCH-TEACHER-275.md).
- 에픽 공통 규약·커버리지 지도: 이슈 #328 본문.

### 검증하지 못한 것

- G-Eval·APE·DSPy·Shankar 의 정확한 게재처는 미확인이다(arXiv 로만 인용).
- MIPRO 의 'Bayesian optimization' 이라는 표현은 초록에서 확인되지 않아 "surrogate 기반"으로만 서술했다.
- KDD 서베이(Mohammadi et al.)의 "4단계 프레임·4대 결함" 명명은 이슈 서술과 달리 본문에 없다 — §2 에서 정정으로 처리했다.
- Yehudai et al.(arXiv:2503.16416)은 arXiv 로만 확인됐고 게재처는 미확인이다.
- 이 조사 과정에서 확인한 "CLAUDE.md 의 'Anthropic 2-tier' 서술과 구매자 경로 실측(OpenAI 2-tier)의 불일치"는 이 조사의 범위(평가·개선 방법론) 밖이라 다루지 않았다 — 별도 이슈 대상일 수 있다.
- GEPA 초록에는 프로그램적 metric 요구가 명시돼 있지 않다 — 후보 궤적을 제안·테스트하는 반복 구조라는 사실로부터 평가 신호 전제를 서술했다. Soumik(arXiv:2604.23178)·Zhang et al.(arXiv:2606.11686)의 게재처는 미확인이다(arXiv 로만 인용).
- 2025–2026 신간 스윕(GEPA·judge 편향 완화 체계 평가·layer-isolated CI 게이트)을 2026-08-05 같은 날 추가 수행했다(라운드 3, recency 보강).

## 참고 문헌

- Mahmoud Mohammadi, Yipeng Li, Jane Lo, Wendy Yip, "Evaluation and Benchmarking of LLM Agents: A Survey", KDD 2025 (arXiv:2507.21504).
- Asaf Yehudai, Lilach Eden, Alan Li, Guy Uziel, Yilun Zhao, Roy Bar-Haim, Arman Cohan, Michal Shmueli-Scheuer, "Survey on Evaluation of LLM-based Agents", arXiv:2503.16416 (2025).
- Lianmin Zheng, Wei-Lin Chiang, Ying Sheng, Siyuan Zhuang, Zhanghao Wu, Yonghao Zhuang, Zi Lin, Zhuohan Li, Dacheng Li, Eric P. Xing, Hao Zhang, Joseph E. Gonzalez, Ion Stoica, "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena", NeurIPS 2023 Datasets and Benchmarks (arXiv:2306.05685).
- Jiawei Gu et al., "A Survey on LLM-as-a-Judge", arXiv:2411.15594 (2024).
- Peiyi Wang, Lei Li, Liang Chen, Zefan Cai, Dawei Zhu, Binghuai Lin, Yunbo Cao, Qi Liu, Tianyu Liu, Zhifang Sui, "Large Language Models are not Fair Evaluators", ACL 2024 (arXiv:2305.17926).
- Raphael Tang, Xinyu Zhang, Xueguang Ma, Jimmy Lin, Ferhan Ture, "Found in the Middle: Permutation Self-Consistency Improves Listwise Ranking in Large Language Models", NAACL 2024 (arXiv:2310.07712).
- Arjun Panickssery, Samuel R. Bowman, Shi Feng, "LLM Evaluators Recognize and Favor Their Own Generations", arXiv:2404.13076 (2024).
- Yang Liu, Dan Iter, Yichong Xu, Shuohang Wang, Ruochen Xu, Chenguang Zhu, "G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment", arXiv:2303.16634 (2023, 게재처 미확인).
- Shreya Shankar, J.D. Zamfirescu-Pereira, Björn Hartmann, Aditya G. Parameswaran, Ian Arawjo, "Who Validates the Validators? Aligning LLM-Assisted Evaluation of LLM Outputs with Human Preferences", arXiv:2404.12272 (2024, 게재처 미확인).
- Kiran Ramnath et al., "A Systematic Survey of Automatic Prompt Optimization Techniques", EMNLP 2025 (arXiv:2502.16923).
- Omar Khattab et al., "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines", arXiv:2310.03714 (2023, 게재처 미확인).
- Krista Opsahl-Ong et al., "Optimizing Instructions and Demonstrations for Multi-Stage Language Model Programs" (MIPRO), EMNLP 2024 (arXiv:2406.11695).
- Chengrun Yang et al., "Large Language Models as Optimizers" (OPRO), ICLR 2024 (arXiv:2309.03409).
- Yongchao Zhou et al., "Large Language Models Are Human-Level Prompt Engineers" (APE), arXiv:2211.01910 (2022, 게재처 미확인).
- Evan Miller, "Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations", arXiv:2411.00640 (2024).
- Lovish Madaan et al., "Quantifying Variance in Evaluation Benchmarks", arXiv:2406.10229 (2024).
- Shuyin Ouyang, Jie M. Zhang, Mark Harman, Meng Wang, "An Empirical Study of the Non-determinism of ChatGPT in Code Generation", arXiv:2308.02828 (2023, v3 2024).
- Qingzhou Luo, Farah Hariri, Lamyaa Eloussi, Darko Marinov, "An Empirical Analysis of Flaky Tests", FSE 2014, pp. 643–653 (DOI 10.1145/2635868.2635920).
- Wei-Lin Chiang et al., "Chatbot Arena: An Open Platform for Evaluating LLMs by Human Preference", arXiv:2403.04132 (2024).
- Ilia Shumailov, Zakhar Shumaylov, Yiren Zhao, Nicolas Papernot, Ross Anderson, Yarin Gal, "AI models collapse when trained on recursively generated data", Nature 631, 755–759 (2024).
- Matthias Gerstgrasser et al., "Is Model Collapse Inevitable? Breaking the Curse of Recursion by Accumulating Real and Synthetic Data", arXiv:2404.01413 (2024).
- Marco Tulio Ribeiro, Tongshuang Wu, Carlos Guestrin, Sameer Singh, "Beyond Accuracy: Behavioral Testing of NLP Models with CheckList", ACL 2020 (arXiv:2005.04118).
- Moran Mizrahi, Guy Kaplan, Dan Malkin, Rotem Dror, Dafna Shahaf, Gabriel Stanovsky, "State of What Art? A Call for Multi-Prompt LLM Evaluation", TACL (arXiv:2401.00595).
- Lakshya A Agrawal et al., "GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning", ICLR 2026 Oral (arXiv:2507.19457).
- Sadman Kabir Soumik, "Judging the Judges: A Systematic Evaluation of Bias Mitigation Strategies in LLM-as-a-Judge Pipelines", arXiv:2604.23178 (2026, 게재처 미확인).
- Sawyer Zhang, Alexander Wang, Sophie Lei, "Layer-Isolated Evaluation: Gating the Deterministic Scaffold of a Production LLM Agent with a No-LLM, Regression-Locked Test Harness", arXiv:2606.11686 (2026, 게재처 미확인).
