# 추천 시스템 평가 방법론 조사 — [이슈 #329](https://github.com/toss-delta-final/jarvis-ai/issues/329) · 작성일 2026-08-05 · 상태(`조건부 go`)

## 요약

**판정: `조건부 go`.** 판정 대상은 [#333](https://github.com/toss-delta-final/jarvis-ai/issues/333)이 계획하는 골든셋 v2 설계(후보 깊이 30·하드 네거티브 주입·슬라이스 쿼터·cutoff 유지·다중 비교 통제)다 — 문헌이 이 설계 방향을 지지하되, 전제와 재평가 트리거(§10)가 붙는다.

- **no-op이 튜닝된 현행 student를 이긴 사건(#275, teacher−no-op은 `inconclusive`)은 이 분야 전반의 현상과 동형이다** — Ferrari Dacrema, Cremonesi, Jannach(2019)가 보고한 "잘 튜닝된 고전 기법이 신경망 기법 대부분을 이긴다"는 재현성 위기와 같은 유형이다.
- **후보 ≤10인 9/18 케이스에서는 nDCG@10의 벌점이 구조적으로 발생할 수 없다** — Wang et al.(2013)의 NDCG 판별가능성 이론(할인함수 감쇠 속도·컷오프 의존)이 이 관측을 설명하며, Valcarce et al.(2018)의 실험은 컷오프를 낮추는 방향이 아니라 후보 깊이를 컷오프보다 깊게 만드는 방향이 문헌 정합임을 보인다.
- **표본추출 지표는 도입하지 않는다** — Krichene, Rendle(2020)이 표본추출 지표가 기대값 수준에서도 순서를 보존하지 않음을 보였고, 우리 metric runner는 이미 전수 평가라 이 위험에 노출돼 있지 않다.
- **오프라인 골든셋 v2는 필요조건 게이트일 뿐 충분조건이 아니다** — Garcin et al.(2014)·Rossetti et al.(2016)이 오프라인 최강 랭커가 온라인에서 최악이거나 순위가 모순되는 사례를 보고했으므로, 행동 로그([#140](https://github.com/toss-delta-final/jarvis-ai/issues/140)) 이후 상관 검증 전까지 온라인 성과를 주장하지 않는다.
- **표본 크기는 사전 설계 대상이지 사후 관찰 대상이 아니다** — Sakai(2018)의 topic set size design 원리로 현재 sd 0.402에서 필요 N(63/249/634)을 역산할 수 있으며, Carterette(2012)의 다중 비교 경고가 슬라이스별 사전 등록·알파 보정 요구를 뒷받침한다.

## 1. 이 리포의 실측 현황

### 3-arm nDCG (dev 31건×N=5, `evals/ablation/baselines/20260803-dev-full-n5/`, 순위 유효 18건)

| 랭커 | nDCG@10 |
|---|---:|
| 현행 결정론 student(6성분, `scoring` arm) | 0.616852 |
| **no-op(dev fixture 순서 = productId 오름차순 = 커밋된 `passthrough`)** | **0.738210** |
| teacher(`pipeline` arm) | 0.782943 |

출처: `evals/scoring/baselines/dev-v1/comparison.md`(passthrough·scoring), `evals/ablation/DECISION.md`(pipeline).

### paired bootstrap 95% CI(N=18, resamples 2000, `evals/ablation/DECISION.md`)

| 비교 | 델타 | 95% CI | 판정 |
|---|---:|---|---|
| no-op − 현행 student | +0.121358 | [0.039814, 0.206934] | no-op 우세 |
| **teacher − no-op** | **+0.044734** | **[−0.122337, 0.231914]** | **`inconclusive`** |

### 순위 유효 18건의 후보 깊이 분포

후보 수: `[2, 2, 4, 4, 5, 5, 6, 9, 9, 15, 15, 15, 16, 16, 26, 29, 30, 30]` — **9/18(50%)이 후보 ≤10**이라 primary metric `nDCG@10`이 가장 강하게 벌하는 실패 모드("정답이 컷오프 밖으로 탈락")가 구조적으로 발생 불가능하다. 서빙 후보 상한은 30인데 평가는 그보다 훨씬 얕은 리스트에서 이뤄져 분포가 어긋난다.

이 분포는 이 문서 작성 중 `evals/goldenset/cases/buyer_dev.jsonl` + `evals/goldenset/fixtures/search_responses.json`을 직접 대조해 재검증했다(read-only, 아래 §부록 "검증 결과" 참조) — **일치**.

### 정답 비율·표본 분산

- 후보 중 등급≥1(정답) 비율: 평균 0.389·중앙 0.267(최소 0.069, 최대 0.833), 정답 수 중앙 3개 — 후보가 골든 `expectedFilters` 검색 결과라 하드 네거티브가 없다.
- `teacher − no-op` 케이스별 델타의 표준편차: **sd 0.402**.

### 필요 표본 역산(현재 sd 0.402 기준)

| 목표 | 필요 N |
|---|---:|
| CI 반폭 ±0.10 | 약 63건 |
| CI 반폭 ±0.05 | 약 249건 |
| 현재 효과(+0.044734) 95%·80% power로 검출 | 약 634건 |

출처: 이슈 #328·#333 본문(리포 내부 관측 분산에서 직접 계산한 값이며, 표준 검정력 분석식의 재현은 §3에서 다룬다).

### 슬라이스 표본(순위 평가 18건 기준, 이슈 #333 본문)

| 슬라이스 | 케이스 수 |
|---|---:|
| `search` | 18(사실상 전체 포괄) |
| `guest` | 9 |
| `personalization` | 7 |
| `category_mapping_failure` | 6 |
| `personalization_overreach` | 3 |
| `cold_start` | 2 |
| `repurchase` | 2 |
| `multi_constraint` | **1** |

`evals/ablation/DECISION.md`도 이 슬라이스 표를 "표본이 작으므로 방향 탐색용이며 confirmatory 결론으로 쓰지 않는다"고 명시한다 — 슬라이스별 판정은 현재 불가능하다.

### 라벨 상태

`evals/goldenset/manifest.json`·`GUIDE.md`: `adjudicator` 전량 공란(구현자 자동 초안, 사람 2차 검수 미완), `provenance` 전량 `curated`(production-derived 0건). datasetVersion `1.0.0`, datasetHash `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`(dev 31 + sealed holdout 12 = 43건).

### 지금 가능한 것

- no-op을 1급 baseline으로 상설 등록하고 모든 랭킹 비교 산출물에 자동 병기하는 것.
- 현재 골든셋(v1)으로 방향 탐색용 slice 하이라이트를 exploratory로 보는 것.
- 후보 깊이·하드 네거티브 비율·슬라이스 쿼터 산정식을 문헌 근거로 사전 등록하는 것.

### 조건이 갖춰지면 가능한 것

- v2 재구축 후 `teacher − no-op` CI가 0을 배제하면 골든셋을 confirmatory 판별력 있는 계측기로 승격하는 것.
- 슬라이스별 sd가 재실측되면 슬라이스 쿼터를 pooled 0.402 가정에서 벗어나 재산정하는 것.
- 행동 로그([#140](https://github.com/toss-delta-final/jarvis-ai/issues/140))가 쌓이면 오프라인-온라인 상관을 검증하는 것.

## 2. nDCG 계열 지표의 성질과 한계 — cutoff·할인함수·짧은 리스트

Wang, Wang, Li, He, Liu(2013)는 표준 로그 할인 NDCG가 랭킹 함수와 무관하게 개체 수 n→∞에서 1로 수렴하고, **일관된 판별가능성**은 할인 함수의 감쇠 속도(1/r이 임계)에 의존하며 NDCG@k의 판별가능성이 k와 할인함수 선택에 따라 달라짐을 이론적으로 분석했다. 이는 **cutoff 선택이 결론을 바꾸는 조건**의 이론적 근거다.

우리 실측이 이 이론에 정확히 대응한다. 후보 ≤10인 9/18 케이스에서는 어떤 순열도 정답을 컷오프 밖으로 밀어낼 수 없다 — #333이 관측한 "지표가 가장 강하게 벌하는 실패 모드가 구조적으로 발생하지 않는다"는 현상은 nDCG@10 자체의 결함이 아니라 **후보 깊이 < 컷오프**라는 데이터 구조 문제다.

Valcarce, Bellogín, Parapar, Castells(2018)는 21개 추천기×3개 데이터셋 실험에서 **깊은 컷오프일수록 강건성·판별력이 높고**, precision은 강건성이 높은 반면 **nDCG가 최고 판별력**을 보인다고 보고했다(확장판 Information Retrieval Journal 2020은 존재만 확인, 세부는 미확인). 이는 컷오프를 낮추는(@3/@5로 도망가는) 방향이 아니라 **후보 깊이를 컷오프보다 깊게 만드는** 방향이 문헌과 정합함을 뜻한다.

Fuhr(2017)는 산술평균 상대 개선율·정밀도 과장 등 IR 평가의 흔한 실수를 경고했다(초록·목차 수준 확인; Sakai의 반론 "On Fuhr's guideline for IR evaluation", SIGIR Forum 2020 존재만 확인). 이는 이 리포가 이미 따르는 절대값·CI 병기 관행(#146 규약)과 정합한다.

Sakai(2006)는 판별력을 부트스트랩 유의 쌍 비율로 재는 방법을 제시했다 — v2 성공 판정(teacher−no-op CI의 0 배제)을 "판별력 확보"로 정식화하는 근거다.

## 3. IR 평가의 통계적 유의성·표본 설계·다중 비교

Sakai(2018)는 topic set size design과 power analysis까지 다루는 IR 실험 설계서로, 표본 크기를 목표 효과·검정력에서 **사전 설계**할 것을 요구한다(책 본문 공식의 정확한 표기는 미확인 — 공식 자체는 표준 검정력 분석식으로만 제시한다). 이는 #328 공통 규약 "슬라이스 쿼터와 표본 산정"의 "슬라이스별 목표 N을 관측 분산에서 역산해 사전 등록"과 정합한다.

paired 비교의 표준 검정력 분석식 `N ≥ ((z_{1−α/2}+z_{1−β})·sd/δ)²`으로 §1의 63/249/634를 재현할 수 있다. sd=0.402, δ(현재 효과)=0.044734, α=0.05(양측, z=1.96), β=0.20(power 80%, z=0.8416)일 때 N ≈ ((1.96+0.8416)×0.402/0.044734)² ≈ 634. CI 반폭 목표는 `N ≈ (z_{1−α/2}·sd/halfwidth)²` 형태로 halfwidth 0.10→63, 0.05→249가 산출된다.

Carterette(2012)와 Fuhr(2017)는 다중 비교 보정 없는 슬라이스별 유의성 남발을 경고한다 — #328 공통 규약 "다중 비교 통제"(confirmatory 슬라이스 2~3개 사전 등록 + 알파 보정, 나머지는 exploratory 자동 라벨)과 정합한다. 구체 처방은 사전 등록한 슬라이스 m개에 Holm–Bonferroni 보정을 적용하는 것이다.

**케이스 수보다 분산 축소가 먼저다.** sd 0.402에서 현재 효과(+0.0447)를 검출하려면 634건이 필요하다 — #328 공통 규약 "슬라이스 쿼터와 표본 산정"의 "분산을 줄이는 설계(후보 깊이·라벨 안정화)를 먼저 한다"를 검정력 설계라는 문헌 절차로 뒷받침한다.

## 4. 오프라인 지표와 온라인 성과의 상관

Garcin, Faltings, Donatsch, Alazzawi, Bruttin, Huber(2014)는 오프라인 최강(최다 인기 추천)이 라이브에서는 최악이었고, 실시간 context-tree 추천이 온라인 CTR을 최대 +35% 올렸다고 보고했다. Rossetti, Stella, Zanker(2016)는 **같은 참여자**의 within-users 설계에서도 오프라인 정확도 순위가 온라인 결과와 명백히 모순됨을 보였다. Gomez-Uribe, Hunt(2016)는 Netflix가 오프라인 실험을 후보 선별에, 온라인 A/B를 최종 판단에 쓰는 결합 프로세스 개요를 기술한다(세부 프로세스 수치는 미확인).

귀결: **v2 골든셋은 필요조건 게이트지 충분조건이 아니다.** 오프라인에서 진 랭커를 올리지 않는 gate로 쓰고, "오프라인에서 이겼으니 온라인에서도 이긴다"는 주장은 하지 않는다. 행동 로그([#140](https://github.com/toss-delta-final/jarvis-ai/issues/140)) 이후 상관 검증 전까지는 온라인 성과 주장을 금지한다 — PR #309의 RESEARCH-TEACHER-275 §13 조건 4(dev 미병합 — 부록 참조), [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)의 `no-go`와 정합한다.

Joachims, Swaminathan, Schnabel(2017)과 Li, Chu, Langford, Wang(2011)은 행동 로그가 쌓여도 position bias 보정(IPS)·replay 규약 없이는 로그가 곧 정답이 아님을 보인다 — [#140](https://github.com/toss-delta-final/jarvis-ai/issues/140) 설계 시 승계할 문헌이며 이미 [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md)·[RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)가 인용했다.

## 5. 재현성·베이스라인 문제 — "잘 튜닝된 단순 베이스라인"

Ferrari Dacrema, Cremonesi, Jannach(2019)는 신경망 추천 대부분이 잘 튜닝된 고전 기법에 패배하며, 하이퍼파라미터 최적화·평가·전처리·베이스라인 코드가 공유되지 않아 재현성이 낮다고 보고했다(RecSys 2019 Best Long Paper). **#275에서 no-op이 튜닝된 student를 이긴 사건은 이 분야 전반의 현상과 동형**이며 우리만의 사고가 아니다.

귀결: #328 공통 규약 "trivial baseline 의무"가 문헌 근거를 얻는다. no-op을 1급 baseline으로 상설 등록(모든 랭킹 비교 산출물에 자동 병기)하는 것은 논쟁의 여지가 없는 권고다.

Jeunen(2019)은 암묵 피드백 추천의 오프라인 평가 관행(무작위 split 등)이 실제 배포 상황과 어긋난다는 문제 제기를 했다(초록 수준 확인, 세부 실험 수치는 미확인) — 평가 설계 자체가 결론을 바꾼다는 같은 계열의 경고다.

우리 리포 특유의 함정도 실측됐다: dev fixture 중 항목 2개 이상인 32/32건이 productId 오름차순이라(§부록 검증 결과) `passthrough`가 "검색 순위" 기준선이 아니라 **임의 순서** 기준선이었다(#328) — 재현성 문헌이 경고하는 "평가 파이프라인의 숨은 가정"의 실례다.

## 6. 하드 네거티브·후보 풀 구성이 평가에 미치는 영향

Krichene, Rendle(2020)은 표본추출 지표가 기대값 수준에서도 순서를 보존하지 않고(A>B 순서 비보존), 표본이 작을수록 모든 지표가 AUC로 붕괴함을 보였다 — 권고는 보정항이 아니라 **표본추출 자체를 피하는 것**이다. 우리 metric runner는 이미 전수 평가라 이 위험에서 정합적이다. Cremonesi, Koren, Turrin(2010)의 "무작위 1000 풀" top-N 평가 관행은 이 비판의 대상이며 승계하지 않는다.

Cañamares, Castells(2020)는 타깃(후보) 풀 크기가 비교 평가의 정보량·일관성을 바꾸고, 축소 타깃 실험이 큰 타깃 실험과 판정이 뒤집히는 경우가 많음을 보였다 — 풀 구성 규칙을 데이터에 기록·고정(`datasetHash`와 함께)해야 비교 가능하다는 #328 공통 규약 "데이터셋 변경 시 datasetVersion·datasetHash 상향 + 전 baseline 재실행"과 정합한다.

Bellogín, Castells, Cantador(2017)는 IR 지표를 추천 평가에 적용할 때 생기는 sparsity·popularity 편향을 식별했다 — 하드 네거티브를 인기 상품 위주로만 채굴하면 popularity 편향을 평가에 주입하게 된다는 것이 채굴 채널 다양화의 근거다.

채굴 기법은 학습 문헌의 유추 적용임을 명시한다: Karpukhin, Oguz, Min, Lewis, Wu, Edunov, Chen, Yih(2020)는 BM25 상위이지만 정답을 포함하지 않는 지문을 hard negative로 채굴했고(+in-batch negative), Xiong, Xiong, Li, Tang, Liu, Bennett, Ahmed, Overwijk(2021, ANCE)는 학습 중인 검색기 자신의 ANN 인덱스에서 네거티브를 채굴했다. 우리 대응은 **pgvector 임베딩 최근접 중 오답**(질의 유사도 상위인데 등급 0)이 가장 정보량 높은 네거티브라는 것이다. 현재 골든 후보는 `expectedFilters` 검색 결과라 정답 비율 0.389로 하드 네거티브가 없다는 #333 진단을 이 문헌들의 용어로 재기술한 것이다.

## 7. 인간 평가 설계 — #153과의 맞물림

Carterette, Bennett, Chickering, Dumais(2008)는 "A가 B보다 관련 있다"는 pairwise 선호 판정이 절대 등급 판정보다 평가자에게 쉽다는 가설과 실증을 제시했다 — [#153](https://github.com/toss-delta-final/jarvis-ai/issues/153)의 blind pairwise 설계를 지지한다.

Voorhees(2000)는 평가자 간 관련성 판정이 상당히 달라도 **시스템 간 비교 순위는 매우 안정적**임을 보였다. 이는 라벨 검수(절대 등급)와 시스템 선호(pairwise)의 역할 분담 근거다 — **골든셋 라벨(등급)은 adjudicator 2차 검수로 안정화하고, 시스템 우열은 #153 pairwise로 외부 검증**한다.

Hayes, Krippendorff(2007)는 Krippendorff's alpha를 평가자 수·척도 수준·결측과 무관하게 적용 가능한 표준 신뢰도 지표로 제안했다 — #153 acceptance와 정합한다(α 임계 관행 ≥.800 양호/≥.667 잠정은 통용 관행 수준이며 원전 미확인).

오프라인-인간 맞물림 처방: #153 pairwise 승패와 오프라인 primary metric의 시스템 쌍 부호 일치를 보고한다(LLM judge를 쓰면 confusion matrix — #153 acceptance 그대로). 인간 평가는 exploratory(#153이 스스로 명시)이며, confirmatory는 오프라인 CI 규약을 유지한다.

Voorhees(2000)가 주는 안전망은 "검수 불일치가 있어도 비교는 안정적"이라는 것이지만, 그것은 **시스템 비교**에 한한 안정성이다. 케이스 단위 라벨 오류는 분산(sd 0.402)을 키우므로, adjudicator 검수는 그 자체로 분산 축소 수단으로 정당화된다(§9-8).

## 8. 최신 동향(2022~2026) — 정초 문헌 이후의 전개

§2~§7의 정초 문헌 이후 2022~2026년 문헌이 같은 결론을 강화하는지 뒤집는지 점검한다.

Castells, Moffat(2022)는 오프라인 실험이 온라인 성과 예측을 지향하지만 상관이 약한 경우가 많고, 실험 구성의 선택지 자체가 결과를 바꾼다는 open challenge를 정리한 서베이를 냈다 — §4의 결론(오프라인 골든셋은 필요조건 게이트일 뿐 충분조건이 아니다)을 재확인하는 최신 서베이다.

Hidasi, Czapp(2023)는 오프라인 평가에 만연한 결함 4가지를 지적했다(구체 내용은 미확인 — "만연한 결함 4가지를 지적" 수준까지만 인용한다). 우리 리포의 "dev fixture 32/32건 오름차순 → passthrough 오독" 사건(§5)과 같은 "평가 파이프라인의 숨은 가정" 계열이 분야 전반에서 계속 보고되고 있음을 보여준다.

Liu, Medlar, Glowacka(2023)는 표본추출 지표 논쟁이 여전히 진행형임을 보였다 — 52개 알고리즘×3개 데이터셋 벤치마크에서 표본 수·전략이 표본추출 지표의 일관성·판별력을 바꾼다. **정직하게 밝힌다**: "표본추출이 판별력을 높인다"는 반론도 존재하지만, 표본 수·전략에 따라 결과가 크게 흔들린다는 사실 자체가 사전 등록 없는 표본추출을 위험하게 만든다 — §9-7(전수 평가 유지) 권고는 이 논쟁의 존재로도 바뀌지 않는다.

Jeunen, Potapov, Ustimenko(2024)는 nDCG가 온라인 보상의 무편향 추정량이 되는 가정을 형식화하고, 대규모 추천 플랫폼의 온·오프라인 상관 분석에서 가정 일부가 위반돼도 무편향 DCG 추정치가 온라인 보상과 강한 상관을 보였다고 보고했다 — primary `nDCG@10` 유지(§9-1)에 최신 근거를 더하고, [#140](https://github.com/toss-delta-final/jarvis-ai/issues/140) 이후 상관 검증 설계(§10 재평가 트리거 ①)에서 쓸 문헌이다.

Wilm, Normann(2025)은 OTTO 이커머스의 대규모 온라인 실험에서 오프라인 지표와 실측 CTR·전환율·판매량 사이의 유의한 정렬을 식별했다 — §4의 오프라인-온라인 괴리 보고들이 "오프라인 무용론"이 아니라 "정렬은 검증 대상"임을 보여주는 반례이자, [#140](https://github.com/toss-delta-final/jarvis-ai/issues/140) 이후 우리가 할 일의 선례다.

Sato(2025)는 "미관측 = 오답"이라는 가정이 평가·학습을 왜곡하며, 표본추출 없이도 negative가 누락될 수 있음을 지적하고 inverse probability weighting 보정을 제안했다 — 하드 네거티브 주입(§9-3) 시 미관측 후보를 자동으로 등급 0 취급하지 말라는 경고다. 우리 골든셋은 `GUIDE.md` 절차상 사람이 등급을 명시 판정하므로 이 함정을 라벨링 절차로 이미 회피하지만, v2에서 자동 채굴한 하드 네거티브 후보도 **반드시 사람 판정을 거쳐 등급 0을 확정**해야 한다는 조건을 명시한다(자동 채굴은 후보 제안까지만 담당).

Parajuli, Vaez Barenji, Ekstrand(2026, **미심사 프리프린트**)는 데이터 필터링 임계·후보 집합 구성 등 오프라인 평가 설계 선택이 모델 비교 순위를 바꾼다고 보고했다 — Cañamares, Castells(2020, §6) 계열의 최신 확장이며, 풀 구성 규칙 고정(§9-7)의 근거를 보강한다.

최신 문헌은 §9 권고안·§10 판정을 뒤집지 않고 강화한다. 다만 Sato(2025)로 §9-3(하드 네거티브)에 라벨 확정 조건이 추가된다.

## 9. 골든셋 v2 권고안 — #333 v2 설계에 주는 시사점

| # | 권고 | 문헌 근거 | #328 규약 정합 | 관측 가능한 판정 |
|---|---|---|---|---|
| 1 | **cutoff**: primary `overall.ndcgAtK.10` 1개 유지. 후보 깊이가 채워지기 전까지 nDCG@5 병기는 exploratory 라벨로만 | Valcarce et al. 2018(컷오프를 낮추는 게 아니라 깊이를 올리는 것이 정방향) | #146·#328 공통 규약 "다중 비교 통제" 정합 | 산출물에 primary 1개 + exploratory 라벨 자동 표기 |
| 2 | **후보 깊이 목표**: 순위 평가 케이스 후보 수를 서빙 상한 30으로(중앙값 30, 후보 ≤10 케이스 0건) | Wang et al. 2013(컷오프 대비 깊이), Valcarce et al. 2018, #275 실측(9/18 무벌점 구조) | #328 공통 규약 "슬라이스 쿼터와 표본 산정" 정합 | 산출물이 후보 깊이 분포 히스토그램·≤10 비율을 인쇄 |
| 3 | **하드 네거티브 비율·채굴 규칙**: 케이스당 등급≥1 후보 비율 ≤ 1/4(현재 평균 0.389). 부족분은 3채널로 채운다 — ① 임베딩 최근접 오답(ANCE 유추) ≥ 50%, ② 제약 위반 근접 후보(가격 초과·금지 속성) 명시 비율, ③ 무작위 카탈로그 ≤ 25% | Karpukhin et al. 2020·Xiong et al. 2021(채굴 채널 유추), Bellogín et al. 2017(인기 편향 주입 금지), Cañamares&Castells 2020(풀 구성이 판정을 바꿈), Sato 2025(미관측을 자동 오답 취급 금지) | #333 acceptance 그대로 | 주입 규칙이 문서화·재현 가능, 채굴 후보는 사람 등급 판정 후에만 등급 0 확정(Sato 2025) |
| 4 | **슬라이스 쿼터 산정식**: `N_s = ⌈((z_{1−α_c/2}+z_{1−β})·sd_s/δ_s)²⌉`, α_c는 사전 등록 confirmatory 슬라이스 m개에 Holm–Bonferroni 보정, sd_s는 v2 재실측(그 전까지 pooled 0.402를 상한 가정). 1차 쿼터는 슬라이스당 30건(≈±0.14)에서 시작 | Sakai 2018, Carterette 2012 | #328 공통 규약 "슬라이스 쿼터와 표본 산정" 정합 | 슬라이스별 N이 산출물에 인쇄 |
| 5 | **다중 비교 통제**: confirmatory 슬라이스 2~3개 사전 등록 + Holm 보정, 나머지는 산출물이 스스로 exploratory 라벨 | Sakai 2018, Carterette 2012, Fuhr 2017 | #328 공통 규약 "다중 비교 통제" 그대로 — 충돌 없음 | exploratory/confirmatory 라벨 자동 분리 |
| 6 | **no-op 상설 등록**: 모든 랭킹 비교 산출물에 no-op 자동 병기 | Ferrari Dacrema et al. 2019 | #328 공통 규약 "trivial baseline 의무" | no-op 행이 모든 비교표에 자동 포함 |
| 7 | **풀 구성 고정**: 표본추출 지표 금지(전수 평가 유지), 풀 구성 규칙은 `datasetHash`에 포함되는 데이터로 기록 | Krichene&Rendle 2020, Cañamares&Castells 2020 | #328 공통 규약 "데이터셋 변경 시 datasetVersion·datasetHash 상향 + 전 baseline 재실행" | 풀 구성 규칙이 datasetHash와 함께 기록 |
| 8 | **라벨 검수**: adjudicator 2차 검수 완료를 v2 acceptance로(GUIDE.md 절차), agreement를 alpha로 보고 | Hayes&Krippendorff 2007, Voorhees 2000(시스템 비교 안정성은 라벨 오류로 인한 분산 확대를 면제하지 않음) | GUIDE.md 절차 정합 | agreement alpha가 산출물에 인쇄 |

각 항목은 #328 규약과 충돌 없이 정합·구체화한다 — 조사 과정에서 충돌은 발견되지 않았다.

## 10. 판정: `조건부 go`

- 전제: ① v2 재구축 시 `datasetVersion`/`datasetHash` 상향 + 전 baseline 재실행(#328 공통 규약 "데이터셋 변경 시 datasetVersion·datasetHash 상향 + 전 baseline 재실행"·GUIDE.md), ② adjudicator 검수 완료 후에만 confirmatory 사용, ③ 성공 판정은 `teacher − no-op` paired bootstrap 95% CI의 0 배제(#333 acceptance = #275 재평가 조건 1), ④ 슬라이스 confirmatory 판정은 사전 등록 + 보정 후에만.
- 재평가 트리거: ① 실사용 행동 로그([#140](https://github.com/toss-delta-final/jarvis-ai/issues/140)) 축적 시 오프라인-온라인 상관 검증 설계(§4 문헌 승계), ② v2 슬라이스별 sd 실측이 0.402와 크게 다르면 쿼터 재산정, ③ 서빙 후보 상한(30)이 계약에서 바뀌면 깊이 목표·cutoff 재검토, ④ holdout 슬라이스화(#333)가 release 게이트 요건을 바꾸면 봉인 규약(GUIDE.md) 재확인.
- 도입 금지 조건(#161 형식): 검수 미완 라벨로 confirmatory 판정을 내리는 것, 표본추출 지표 도입, 사전 등록 없는 슬라이스 유의성 주장, 오프라인 결과만으로 온라인 우월 주장.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- `evals/scoring/baselines/dev-v1/comparison.md` — passthrough 0.738210 / scoring 0.616852(재측정 없이 커밋된 파일 확인).
- `evals/ablation/baselines/20260803-dev-full-n5/` + `evals/ablation/DECISION.md` — 3-arm nDCG, pipeline 0.782943, paired bootstrap 규약(#146), 슬라이스 표본 표.
- `evals/goldenset/fixtures/search_responses.json` + `evals/goldenset/cases/buyer_dev.jsonl` — 후보 깊이 분포·productId 오름차순을 이 문서 작성 중 직접 재검증(read-only 파이썬, 파일 미생성). **검증 결과: §1의 깊이 분포·9/18·32/32 오름차순 전부 일치, 불일치 없음.**
- `evals/goldenset/GUIDE.md`·`manifest.json` — datasetVersion/Hash 규약, adjudicator 미완.
- teacher−no-op +0.044734 [−0.122337, 0.231914], no-op−student +0.121358 [0.039814, 0.206934], 델타 sd 0.402, 필요 N(63/249/634), 슬라이스 표본 표 — 출처는 이슈 [#328](https://github.com/toss-delta-final/jarvis-ai/issues/328)·[#333](https://github.com/toss-delta-final/jarvis-ai/issues/333) 본문과 PR #309의 `RESEARCH-TEACHER-275.md`(브랜치 `NyongCho/docs-275-llm-teacher-research`, **작성일 기준 dev 미병합**).
- 참고 문헌 전부 2026-08-05 WebSearch로 서지 검증(오케스트레이터 수행, `verified-bibliography-329.md`).

### 검증하지 못한 것

- Sakai(2018) 책 본문 공식의 정확한 표기(초록·출판사 페이지 수준 확인만 함) — §3의 공식은 표준 검정력 분석식 형태로만 제시했다.
- Ferrari Dacrema et al.의 TOIS 2021 확장판 세부(존재만 확인).
- Valcarce et al. 확장판(Information Retrieval Journal 2020) 세부(존재만 확인).
- Krippendorff's alpha 임계값(≥.800/≥.667) 원전 확인 — 통용 관행 수준으로만 인용.
- Gomez-Uribe·Hunt(2016)의 Netflix 세부 프로세스 수치.
- Jeunen(2019)의 세부 실험 수치(초록 수준만 확인).
- 슬라이스별 sd 실측 부재 — pooled 0.402를 상한으로 가정했다.
- Hidasi&Czapp(2023)이 지적한 "만연한 결함 4가지"의 구체 내용(제목 수준만 확인).
- Sato(2025)의 inverse probability weighting 보정 세부 수식·실험 수치(제안 존재만 확인).
- Parajuli, Vaez Barenji, Ekstrand(2026)은 **미심사 프리프린트**(arXiv 제출뿐 — peer review 미완, 결과 세부는 인용하지 않고 설계 선택이 순위를 바꾼다는 주장 수준까지만 인용).

## 참고 문헌

- Maurizio Ferrari Dacrema, Paolo Cremonesi, Dietmar Jannach, "Are We Really Making Much Progress? A Worrying Analysis of Recent Neural Recommendation Approaches", RecSys 2019 (Best Long Paper), DOI `10.1145/3298689.3347058` (arXiv:1907.06902).
- Norbert Fuhr, "Some Common Mistakes In IR Evaluation, And How They Can Be Avoided", SIGIR Forum 51(3), pp. 32–41, 2017, DOI `10.1145/3190580.3190586`.
- Tetsuya Sakai, "Laboratory Experiments in Information Retrieval: Sample Sizes, Effect Sizes, and Statistical Power", Springer, The Information Retrieval Series vol. 40, 2018, ISBN 9789811345814.
- Olivier Jeunen, "Revisiting Offline Evaluation for Implicit-Feedback Recommender Systems", RecSys 2019 (Doctoral Symposium), DOI `10.1145/3298689.3347069`.
- Yining Wang, Liwei Wang, Yuanzhi Li, Di He, Tie-Yan Liu, "A Theoretical Analysis of NDCG Type Ranking Measures", COLT 2013 (PMLR v30, pp. 25–54; arXiv:1304.6480).
- Daniel Valcarce, Alejandro Bellogín, Javier Parapar, Pablo Castells, "On the Robustness and Discriminative Power of IR Metrics for Top-N Recommendation", RecSys 2018, DOI `10.1145/3240323.3240347`.
- Walid Krichene, Steffen Rendle, "On Sampled Metrics for Item Recommendation", KDD 2020.
- Rocío Cañamares, Pablo Castells, "On Target Item Sampling in Offline Recommender System Evaluation", RecSys 2020, DOI `10.1145/3383313.3412259`.
- Alejandro Bellogín, Pablo Castells, Iván Cantador, "Statistical biases in Information Retrieval metrics for recommender systems", Information Retrieval Journal 20(6), pp. 606–634, 2017, DOI `10.1007/s10791-017-9312-z`.
- Paolo Cremonesi, Yehuda Koren, Roberto Turrin, "Performance of Recommender Algorithms on Top-N Recommendation Tasks", RecSys 2010, pp. 39–46, DOI `10.1145/1864708.1864721`.
- Ben Carterette, "Multiple Testing in Statistical Analysis of Systems-Based Information Retrieval Experiments", ACM TOIS 30(1), 4:1–4:34, 2012.
- Tetsuya Sakai, "Evaluating Evaluation Metrics Based on the Bootstrap", SIGIR 2006, pp. 525–532, DOI `10.1145/1148170.1148261`.
- Ellen M. Voorhees, "Variations in relevance judgments and the measurement of retrieval effectiveness", Information Processing & Management 36(5), pp. 697–716, 2000.
- Florent Garcin, Boi Faltings, Olivier Donatsch, Ayar Alazzawi, Christophe Bruttin, Amr Huber, "Offline and Online Evaluation of News Recommender Systems at swissinfo.ch", RecSys 2014, DOI `10.1145/2645710.2645745`.
- Marco Rossetti, Fabio Stella, Markus Zanker, "Contrasting Offline and Online Results when Evaluating Recommendation Algorithms", RecSys 2016, pp. 31–34, DOI `10.1145/2959100.2959176`.
- Carlos A. Gomez-Uribe, Neil Hunt, "The Netflix Recommender System: Algorithms, Business Value, and Innovation", ACM TMIS 6(4), 13:1–13:19, 2016, DOI `10.1145/2843948`.
- Thorsten Joachims, Adith Swaminathan, Tobias Schnabel, "Unbiased Learning-to-Rank with Biased Feedback", WSDM 2017, pp. 781–789, DOI `10.1145/3018661.3018699`.
- Lihong Li, Wei Chu, John Langford, Xuanhui Wang, "Unbiased Offline Evaluation of Contextual-Bandit-Based News Article Recommendation Algorithms", WSDM 2011, pp. 297–306 (arXiv:1003.5956).
- Vladimir Karpukhin, Barlas Oğuz, Sewon Min, Patrick Lewis, Ledell Wu, Sergey Edunov, Danqi Chen, Wen-tau Yih, "Dense Passage Retrieval for Open-Domain Question Answering", EMNLP 2020 (ACL Anthology 2020.emnlp-main.550).
- Lee Xiong, Chenyan Xiong, Ye Li, Kwok-Fung Tang, Jialin Liu, Paul Bennett, Junaid Ahmed, Arnold Overwijk, "Approximate Nearest Neighbor Negative Contrastive Learning for Dense Text Retrieval", ICLR 2021 (arXiv:2007.00808).
- Ben Carterette, Paul N. Bennett, David Maxwell Chickering, Susan T. Dumais, "Here or There: Preference Judgments for Relevance", ECIR 2008, LNCS 4956, pp. 16–27.
- Andrew F. Hayes, Klaus Krippendorff, "Answering the Call for a Standard Reliability Measure for Coding Data", Communication Methods and Measures 1(1), pp. 77–89, 2007, DOI `10.1080/19312450709336664`.
- Pablo Castells, Alistair Moffat, "Offline recommender system evaluation: Challenges and new directions", AI Magazine 43(2), pp. 225–238, 2022, DOI `10.1002/aaai.12051`.
- Balázs Hidasi, Ádám Tibor Czapp, "Widespread Flaws in Offline Evaluation of Recommender Systems", RecSys 2023, DOI `10.1145/3604915.3608839` (arXiv:2307.14951).
- Yang Liu, Alan Medlar, Dorota Glowacka, "On the Consistency, Discriminative Power and Robustness of Sampled Metrics in Offline Top-N Recommender System Evaluation", RecSys 2023, pp. 1152–1157, DOI `10.1145/3604915.3610651`.
- Olivier Jeunen, Ivan Potapov, Aleksei Ustimenko, "On (Normalised) Discounted Cumulative Gain as an Off-Policy Evaluation Metric for Top-n Recommendation", KDD 2024, DOI `10.1145/3637528.3671687` (arXiv:2307.15053).
- Timo Wilm, Philipp Normann, "Identifying Offline Metrics that Predict Online Impact: A Pragmatic Strategy for Real-World Recommender Systems", RecSys 2025, DOI `10.1145/3705328.3748111` (arXiv:2507.09566).
- Masahiro Sato, "Unobserved Negative Items in Recommender Systems: Challenges and Solutions for Evaluation and Learning", RecSys 2025, pp. 1317–1321, DOI `10.1145/3705328.3759315`.
- Sushobhan Parajuli, Samira Vaez Barenji, Michael D. Ekstrand, "On the Convergent Validity of Offline Evaluation Designs for Recommender Systems", arXiv:2607.25097, 2026(미심사 프리프린트).
