# LLM teacher 기반 랭킹 학습 도입 판단 — [이슈 #275](https://github.com/toss-delta-final/jarvis-ai/issues/275) · 작성일 2026-08-05 · 상태(`no-go`)

## 요약

**판정: `no-go`.** 현행 6성분 결정론 student(`evals/scoring`)로의 teacher distillation은 성립하지 않는다 — LLM teacher 자체나 [#146](https://github.com/toss-delta-final/jarvis-ai/issues/146)의 production 결정(pipeline 유지)을 뒤집는 것은 아니다.

- **student 는 아무것도 하지 않는 순서보다 유의하게 낮다.** no-op(dev search fixture 순서=productId 오름차순=`passthrough`, 0.738210) − 현행 student(0.616852) = **+0.121358**(95% CI [0.039814, 0.206934]) — 즉 현행 student 가 그만큼 낮다. `passthrough` 는 "검색 순위" 기준선이 아니라 **임의 순서 기준선**이었다.
- **6성분 가중치로는 그 임의 순서조차 넘지 못한다.** 축퇴를 배제한 오라클 상한이 0.738208 로 no-op(0.738210) 과 사실상 같다 — 담을 그릇의 문제지 라벨의 문제가 아니다.
- **teacher − no-op 은 `inconclusive`**(+0.044734, 95% CI [−0.122337, 0.231914], N=18). teacher 가 임의 순서보다 낫다는 것 자체가 이 계측기로는 아직 확립되지 않았다.
- **transfer set 합성은 되지만(E3 12/12, 콜당 $0.00101052) 실사용과 닮았는지는 지금 검증할 수 없다** — MAUVE·C2ST 는 실데이터 표본을 요구하는데 관측된 실사용 발화는 25종·7명뿐이다.

관련 판단: 협업 신호는 [RESEARCH-CF-159.md](./RESEARCH-CF-159.md), 저장 구조·label 조건은 [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md) §3-1(이 이슈의 출발점), online 최적화는 [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)를 본다. 측정 하네스는 [research-275-harness/](./research-275-harness/)에 커밋했다.

## 1. 이 리포의 실측 현황

`evals/ablation/baselines/20260803-dev-full-n5/`(dev 31건×N=5, seed 20260803, 순위 유효 18건)의 3-arm 요약:

| arm | nDCG@10 | Filter Accuracy | HCV | calls | latency/case | cost/case |
|---|---:|---:|---:|---:|---:|---:|
| `pipeline`(teacher 후보) | 0.782943 | 0.063519 | 0.000000 | 290 | 6,362.729 ms | $0.000897 |
| `scoring`(student 후보) | 0.616852 | 1.000000 | 0.032258 | 0 | 3.781 ms | — |
| `single_call` | 0.696084 | 0.248333 | 0.032258 | 155 | 4,851.826 ms | $0.000918 |

`pipeline − scoring` paired bootstrap: **+0.166, 95% CI [0.035, 0.320]**, `pipelineWins`. 전량 실행 실측 445 calls·1,106,237 tokens·**$0.28132815**.

teacher 호출 단위(`pipeline/calls.csv` 직접 집계): rerank 135콜(smart `gpt-5.6-luna`), decompose 155콜(fast `gpt-5-nano`), 오류 0. 155 case-repeat 중 rerank 콜은 135건 — **20건(12.9%)은 teacher 를 아예 타지 않았다.** rerank 평균 **$0.00077926**/콜(총 $0.10520060), 입력 토큰 평균 1,380.674(중앙 926, p95 3,171), 출력 토큰 평균 419.274(reasoning 평균 229.089), latency 평균 4,262.874 ms(p95 7,590). rerank cacheTokens 는 86콜이 0(후보 목록이 매번 달라 캐시가 거의 먹지 않음), decompose 는 154/155 캐시 적중.

골든셋(`evals/goldenset/`)은 dev 31 + sealed holdout 12 = **43건**(hash `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`, v1.0.0). 후보 fixture 43건의 후보 수 분포는 0건 6개·30건(상한) 7개를 포함해 넓게 퍼져 있고, `provenance` 는 전량 `curated`(production-derived 0건), `adjudicator` 는 v1 전량 비어 있어 **사람 2차 검수 미완**이다. 순위 판별력 없는 케이스는 `nonDiscriminativeRankingCases`로 nDCG 분모에서 제외한다(#143 규약).

`conversation_turns`(pg-profile, 2026-08-05 직접 실측): 1,471행이지만 **distinct `user_text`는 25개, distinct `user_id`는 7명**뿐이다(07-20/07-21 각 8행, 08-02 718행, 08-03 22행, 08-04 715행으로 집중). 발화 길이 분산도 거의 없다(guest 평균 20.0, member 평균 26.4) — **실사용이 아니라 벤치마크·스모크 스크립트 트래픽**이다.

관측 로그에는 `chat_request`(lane·degraded·costUsd·messageLength·messageHash 등, 원문 없음)와 `decompose_case`(`leg_queries` 원문·`filters_set` 축 이름·`profile_injected`)가 남는다 — 상세는 §4(d).

### 지금 가능한 것

- 임의 순서 기준선(no-op)을 1급 baseline 으로 사전 등록하고 그 위에서 student/teacher 를 평가하는 것.
- E3 규모(12질의·72콜)로 teacher 라벨의 순서/반복 민감도와 콜당 비용을 실측하는 것.
- Promptagator 왕복 필터로 합성 질의의 **내적** 타당도(질의↔후보 일관성)만 검증하는 것.

### 조건이 갖춰지면 가능한 것

- 순위 판별력 있는 골든 케이스를 늘려 `teacher − no-op` CI 가 0을 배제하게 만드는 것.
- `conversation_turns`에 실사용 발화가 쌓이면 MAUVE/C2ST 로 합성 질의의 **외적** 타당도를 재는 것.
- 질의 조건부 피처를 추가한 student basis 로 오라클 상한 자체를 끌어올리는 것(§3-1).

## 2. teacher·student 정의와 물려받을 축

teacher = `pipeline` arm(smart 티어 LLM 1회 rerank 호출, `app/agents/buyer/recommendation/rerank.py`). 받는 `query` 는 `request.message` — decompose 산출 필터가 아니라 **사용자 발화 원문**이다(`graph.py` 확인). 후보 payload 는 `{productId, name, brand, priceLevel, ratingLevel, reviewLevel, category}`(정밀 가격·평점·리뷰수 제외, #171/#173/#236). 후보 상한 30, 출력 예산 960+60×expose_max.

student = `scoring` arm(`evals/scoring/`), semantic 0.55·profile_match 0.15·popularity 0.15·recency 0.05·diversity_bonus 0.10·recent_purchase_penalty 0.20 가중합, 자유 파라미터 사실상 6개. `evals/scoring/components.py` 확인: **6성분 중 질의에 의존하는 성분은 `semantic` 하나뿐**이다 — 나머지 5개는 query 를 인자로 받지 않는다. 즉 이 student 가 배울 수 있는 것은 "질의 유사도 대 정적 사전확률들의 전역 교환비" 하나이며, "선물 질의에서는 브랜드를 더 본다" 류의 질의 조건부 거동은 구조적으로 표현할 수 없다. teacher 는 후보의 name·brand·category·등급 텍스트를 전부 읽고 질의와 대조한다.

**무비판적 모방 금지**: `pipeline` 의 Filter Accuracy 는 **0.063519**, `scoring` 은 **1.000000** 이다 — 두 arm 이 잘하는 축이 정반대다. 그래서 물려받을 것과 남길 것을 가른다.

| 축 | 물려받는가 | 근거 |
|---|:---:|---|
| 후보 재정렬(순위 신호) | ✅ 모방 대상 | teacher 가 잘하는 축(nDCG +0.166) |
| `hard_filter.py`(가격·금지 카테고리·금지 상품·must-exclude) | ❌ 결정론 유지 | scorer 와 분리된 컷, teacher 의 Filter Accuracy 0.063519 가 그 이유 |
| `productId` tiebreak | ❌ 결정론 유지 | 동점 처리 규약, 모방 대상 아님 |
| degrade 기록(임베딩 누락·guest·recency 미주입) | ❌ 결정론 유지 | 가짜 중립 프로필 금지 원칙과 충돌 |
| 비표시 정밀 가격·평점 유출 방지(#171/#173/#236) | ❌ 결정론 유지 | teacher 도 이 값을 받지 않는다 — 물려받을 것 자체가 없음 |

## 3. student 용량 상한

**E2 는 축퇴 해라 폐기한다**(근거: `docs/research/research-275-harness/e2_analyze.py` 원본과 결함을 교정한 `e4_analyze.py`, `evals/scoring/baselines/dev-v1/comparison.md`, `evals/goldenset/fixtures/search_responses.json`). `ScoringBuyerAdapter` 는 `recency_by_product=None` 이라 `recency` 성분은 항상 0(무력한 축)인데, `EvaluationSettings` 검증자는 "5개 양의 신호 가중치 중 하나 이상 양수"만 요구해 이 무력한 축에 값을 몰아주고 나머지 실질 신호를 전부 0으로 만드는 해를 막지 못한다. 그 해의 순위는 전 케이스에서 productId 오름차순이고 nDCG@10 = **0.7382095783451728** — dev search fixture 32/32 건이 이미 productId 오름차순으로 기록돼 있어(`search_responses.json`), 이는 커밋된 **passthrough baseline 0.738210**(`evals/scoring/baselines/dev-v1/comparison.md`)과 같은 no-op 순위다.

이 세 값은 **같은 후보 집합 위에서의 순서 차이**임을 직접 확인했다 — `pipeline` arm 과 `scoring` arm 의 `metrics.eligibleProductIds` 가 dev **31/31 케이스에서 완전히 동일**하다(5반복 내내 불변). 남는 confound 는 decompose 산출 필터뿐이다.

**E4(교정 재측정, 실 LLM 호출 0)** — 랭킹 유효 18케이스 기준:

| 항목 | nDCG@10 |
|---|---:|
| no-op(=fixture 순서=productId 오름차순=passthrough) | **0.738210** |
| 현재 기본 가중치(student) | 0.616852 |
| 축퇴 배제 오라클 상한(in-sample, `{semantic 0.85, popularity 0.0404, penalty 0.1613, 나머지 0}`) | **0.738208** |
| teacher-fit(teacher 순위를 graded relevance 로 삼아 적합, 표준 경로 골든 평가) | 0.718357 |
| teacher(`pipeline` arm, 커밋 참고값) | 0.782943 |

축퇴 배제 오라클 상한은 `recency`를 탐색에서 빼고(주입되지 않는 무력 축), 나머지 4축 중 하나 이상 0.05 이상 + no-op 과 최소 1케이스 다른 순위를 강제한 4,072회 평가의 결과다 — **no-op 대비 −0.000001**. 6성분을 어떻게 조합해도 "아무것도 하지 않는 순서"를 넘지 못한다. teacher-fit 가중치조차 no-op 보다 **0.019853 낮다**(teacher 후보집합 위에서 잰 0.8344024, n=26 은 후보집합이 달라 직접 비교 금지).

paired bootstrap 95% CI(#146 규약, resamples 2000, paired N=18):

| 비교 | 평균 델타 | 95% CI | 판정 |
|---|---:|---|---|
| no-op − 현행 student | +0.121358 | [0.039814, 0.206934] | no-op 우세 |
| 축퇴 배제 오라클 상한 − 현행 student | +0.121357 | [0.055285, 0.196624] | 오라클 우세 |
| teacher-fit − 현행 student | +0.101505 | [0.035183, 0.178018] | teacher-fit 우세 |
| **teacher − no-op** | **+0.044734** | **[−0.122337, 0.231914]** | **`inconclusive`** |

마지막 줄이 이 조사의 핵심이다. 사전 등록한 판정 규칙(CI 가 0 을 포함하면 `inconclusive`)을 그대로 적용하면, 현재 dev 골든셋 위에서는 **LLM teacher 가 임의 순서보다 낫다는 것조차 확립되지 않는다.**

**문헌 대비**: teacher 를 따라잡거나 넘은 student 는 전부 435M~7B 급이다. Sun et al. 2023은 DeBERTa-v3-large cross-encoder(≈435M, LLaMA-7B 변형)를 MS MARCO 10,000질의×BM25 후보 20건으로 permutation distillation 해 BEIR 평균 nDCG@10 53.03(monoT5-3B 지도학습 51.36 대비 +1.67, EMNLP 2023 Outstanding Paper)를 얻었다. Pradeep et al. 2023의 RankZephyr/RankVicuna 도 GPT-3.5/GPT-4 teacher 라벨로 파인튜닝한 **7B** listwise reranker다. Tang and Wang 2018의 Ranking Distillation 은 압축비 약 2배(student 파라미터가 teacher 의 40~53%)에서 teacher 와 대등·상회를 보였다. Cho and Hariharan 2019는 teacher-student 용량 격차(capacity gap)가 클수록 student 성능이 오히려 떨어지는 현상을 보고한다. 우리 조합(프롬프트 LLM teacher → 자유 파라미터 6개 student)은 이 실패 방향의 극단이며, 오라클 상한(0.738208)이 teacher(0.782943)에 못 미친다는 실측이 그 벽을 수치로 보여준다 — **라벨을 완벽히 만들어도 못 넘는다.**

### 3-1. student 를 키운다면 — 신규 ML 의존성 없이 가능한 형태

현행 basis 의 질의 의존 축이 `semantic` 하나뿐이라는 §2 의 구조적 한계를 직접 겨냥해, 질의 토큰과 `name`·`category`·`brand` 의 어휘 겹침(정규화 Jaccard 또는 간단한 BM25 유사값), 브랜드 명시 여부와 일치, 가격대 언급과 `priceLevel` 일치, 카테고리 일치 같은 **질의 조건부 피처**를 더한다. 전부 순수 파이썬으로 계산할 수 있어 신규 의존성이 없다. 모델 형태는 [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md) §3 비교표가 이미 1차 후보로 판정한 pointwise logistic 이며(의존성 없거나 최소, 계수 0 롤백 쉬움), 계수는 오프라인에서 적합하고 `app/core/config.py` 로 주입하면 런타임에 학습 코드가 들어가지 않는다. 지연은 #160 §3 이 사전 등록한 gate(추가 p50 5ms·p95 15ms)를 승계하되 **실측 전에는 가능하다고 주장하지 않는다.**

## 4. transfer set 합성과 분포 타당성 — 이슈가 지목한 중심 질문

**(a) 합성은 되는가.** 된다. E3 파일럿(실제 지출 $0.073069/상한 $0.498)에서 fast 티어로 카테고리 seed 12개(dev 케이스 카테고리 분포 상위) 전량 합성 질의를 만들었다(12/12 성공, 손으로 대체 0건). 다만 결함도 섞였다 — `"제 moyenne 속도에 맞는 1루수용 글러브"`(프랑스어 혼입), 한 발화에 두 상품군을 섞은 질의 등. Dai et al. 2023의 Promptagator 도 LLM(FLAN 137B) 8-shot 으로 task 별 질의 생성기를 만들어 11개 retrieval set 평균 nDCG +1.2(합성으로 reranker 추가 학습 시 +5.0)를 보였다.

**(b) 실사용과 닮았는지 검증할 수 있는가.** 지금은 못한다. Pillutla et al. 2021의 MAUVE 와 Classifier two-sample test(C2ST) 둘 다 **실데이터 표본을 요구**하는데, §1 실측대로 distinct 실사용 발화는 25종·사용자 7명뿐이다. Promptagator 의 왕복 일관성 필터(생성 질의 q 는 그 질의로 학습한 retriever 의 Top-K 에 그 문서가 다시 들어올 때만 채택, K=1 이 최선, 효과 평균 +2.5 nDCG@10)는 **질의↔문서 내적 타당도**만 준다 — 실사용 수요와의 유사성(외적 타당도)은 다른 질문이다. 이 구분이 이 조사의 중심이다.

**(c) 이슈 전제의 정정.** 이슈 본문은 "#136 관측 로그가 원문을 금지해 실사용 발화를 저장하지 않는다"고 적었다. **관측 로그**(`chat_request`)에 한해서는 참이다(`message_fingerprint()`가 길이+peppered HMAC 지문만 만든다). 그러나 **대화 저장소**(`conversation_turns`, api-spec §6.3 a, #33)에는 `user_text` 원문이 그대로 남는다. §1 실측대로 1,471행 중 distinct 25종·7명뿐인 이유는 **저장 금지가 아니라 트래픽 부재**다. 이 구분이 중요한 이유: 저장 금지라면 이 경로는 영구 봉쇄지만, 트래픽 부재라면 **관측 가능한 착수 조건**으로 환원된다(§13).

**(d) 지금 할 수 있는 대체 검증.** ① Promptagator 왕복 일관성 필터(내적 타당도). ② `decompose_case.filters_set`(축 이름만, 값은 PII 라 제외)·`legs`·`leg_queries`(LLM 이 만든 카테고리별 질의 원문)와 `chat_request.messageLength` 로, **원문 없이도** intent 혼합·필터 축 혼합·leg 수·발화 길이의 주변분포를 실사용 트래픽과 대조할 수 있다. 이는 외적 타당도의 완전한 대체가 아니라 부분적 근사다.

## 5. teacher 라벨 품질

**반복 안정성(E1-a, 순위 유효 18케이스)**: 5회 완전 동일 순위 **5/18(27.8%)**, top-1 최빈값 비율 평균 **0.8889**(최소 0.4000), top-5 Jaccard 평균 0.8509, 공통항목 Kendall tau-b 평균 **0.7922**(최소 0.4000). **품질 반복 분산(E1-b)**: 케이스별 nDCG@10 표준편차 평균 **0.0678**(최대 0.2472), 반복별(0~4) 전체 평균이 0.7505~0.8037 사이(**폭 0.053**)로 움직인다 → **teacher 자신의 실행 간 변동폭이 teacher−no-op 격차(0.044734)보다 크고, teacher−student 격차(0.166)의 약 1/3 이다.**

**순서 민감도(E3)**: 순서를 바꾼 쌍(identity/reversed/shuffled) Kendall tau-b **0.3654**·top-1 일치 **47.2%**·top-5 Jaccard 0.7962. 같은 순서 반복 쌍은 tau **0.6463**·top-1 일치 **63.9%**로 더 안정적이다 → **후보 제시 순서만 바꿔도 teacher 의 1위가 절반 이상 바뀐다.** 문헌의 listwise 위치 편향이 우리 teacher 에서 실측됐다. Tang et al. 2024는 프롬프트 목록 순서를 여러 번 섞어 중심 순위를 취하는 순열 자기일관성으로 GPT-3.5 7~18%·LLaMA v2(70B) 8~16% 개선을 보고한다. Wang et al. 2023/2024는 제시 순서만 바꿔도 LLM 심판의 품질 순위가 뒤집힘(80개 질의 중 66개에서 역전 사례)을 보고하며, teacher 를 "정답"으로 쓰는 순간 이 편향이 라벨에 그대로 새겨진다고 경고한다.

**처방 비용의 환산**: 순열 자기일관성을 3회 순열로 적용하면 콜 수가 3배가 되고, E1-c 회귀식(입력 토큰 = 443.814 + 88.7552×K)과 rerank 콜당 평균 $0.00077926 을 그대로 곱하면 캠페인 비용도 그만큼 선형으로 는다(§6). "라벨을 더 안정적으로 만드는" 처방은 있지만 비용 없이는 오지 않는다.

## 6. 비용 구조

E1-c 회귀(rerank 135콜, R²=0.9952): **입력 토큰 = 443.814 + 88.7552 × K**(K=후보 수). 출력 토큰은 K 와 약한 상관(R²=0.3130), 비용도 R²=0.6583 로 K 만으로는 설명이 약하다. 단가식(입력×$0.0002/1k + 출력×$0.0012/1k)이 rerank 135콜 **전량 일치**(불일치 0) — **프롬프트 캐시 할인은 비용에 반영되지 않는다**(rerank cacheTokens 0인 콜 86/135). E3 실측(K=20)은 콜당 평균 **$0.00101052**, 평균 입력 2,259.75 토큰으로 E1-c 회귀 예측(2,218.92 토큰)과 **1.8% 이내**로 일치해 비용 모델이 교차 검증됐다.

캠페인 비용 시나리오는 **입력 토큰 회귀(R²=0.9952) 입력 토큰 = 443.814 + 88.7552 × K** 에 단가 $0.0002/1k 를 곱하고, 출력 비용은 K 상관이 약해(R²=0.3130) 회귀 대신 **E1 rerank 전체 평균 출력 토큰 419.274**(단가 $0.0012/1k, 콜당 고정 ≈$0.000503)를 더한 산식이다(질의 1건 = rerank 1콜, 순열 자기일관성은 콜 수 배수):

| 질의 규모 | K=10 (≈$0.000769/콜) | K=20 (≈$0.000947/콜) | K=30 (≈$0.001124/콜) |
|---|---:|---:|---:|
| 1,000질의 × 순열 1회 | ≈ $0.77 | ≈ $0.95 | ≈ $1.12 |
| 10,000질의 × 순열 1회 | ≈ $7.69 | ≈ $9.47 | ≈ $11.24 |
| 10,000질의 × 순열 3회(Tang et al. 2024 처방) | ≈ $23.08 | ≈ $28.41 | ≈ $33.73 |
| 100,000질의 × 순열 1회 | ≈ $76.94 | ≈ $94.69 | ≈ $112.44 |

**K=20 교차 검증**: 이 산식의 K=20 예측(콜당 $0.000947)은 E3 실측 콜당 평균 **$0.00101052** 보다 약 **6.3% 낮다**. 출력 비용에 전체 평균(419.274 토큰)을 고정으로 썼는데, E3 는 K=20 구간 실측 평균 출력 토큰이 465.472 로 더 크기 때문이다(E3 의 K=20 실측 출력 토큰을 그대로 쓰면 $0.001002 로 실측과 1% 이내로 좁혀진다). (재시도율·캐시 변동도 반영하지 않았다 — 실제 캠페인 전 재확인 대상.) 비교 기준은 #146 전량 실행 실측 **445 calls·$0.28132815**뿐이다. 즉 transfer set 합성 자체는 **비용이 이 경로의 병목이 아니다** — §4(a)·§11 의 결론과 같다.

후보 축소 위치는 이미 고정돼 있다: `search_backend=embedding_rerank` 기본이고 #254 로 의미 재정렬이 pgvector `top_k_by_vector`로 이관돼 후보 30/300/3,000/7,220건에서 DB p50 **2.1/4.1/21.4/44.9ms**(Python 단계 10.4/99.4/1,024.8/2,490.4ms 대비 대폭 단축)다. Huang et al. 2025(ColdLLM)의 coupled funnel(값싼 필터로 후보를 줄인 뒤 좁은 집합에만 LLM)과 같은 형태이며, Jarvis 는 이미 이 구조 위에서 teacher 를 호출한다.

## 7. 전이 신호의 형태와 결합 방식

| 형태 | 필요한 것 | Jarvis 적용성 |
|---|---|---|
| top-K 목록 전이(Tang and Wang 2018) | teacher top-K + 위치 가중 손실 항 | 하이브리드 손실(정답 항+teacher 항, α 조절) 형태는 옮길 수 있다 — §3의 no-op 기준선을 넘지 못하는 용량 문제와는 별개 축 |
| 점수 회귀 | teacher 가 스칼라 점수를 내야 함 | teacher 는 순위만 내고 점수는 내지 않는다(rerank 출력이 순서) — 그대로는 부적합 |
| pairwise 선호 비교 | teacher 순위에서 쌍 추출 | top-K 목록 전이의 특수형, 같은 논의로 흡수 |

결합은 **auxiliary 항으로 주입**하고 `weight=0` 을 즉시 롤백으로 삼는다(Wang et al. 2024: LLM 합성 신호를 auxiliary pairwise loss 로 주입하며 전면 대체하지 않음). 튜너블은 `app/core/config.py` 로 주입한다 — `home_reco_weight_profile=0`(#148) 선례와 같은 규약이다. 다만 §3 이 이미 확정했듯, 결합 방식을 아무리 정교하게 설계해도 **담을 그릇(6성분 선형결합)의 오라클 상한이 no-op 을 못 넘는 한** auxiliary 항의 실효 이득은 없다.

## 8. 반복 self-training 판단

고정 teacher 모방의 상한은 teacher nDCG@10 0.782943 이고, 오라클 상한(0.738208)은 이미 그 아래다. Xie et al. 2020(Noisy Student)은 teacher 가 pseudo-label 을 만들고 더 크고 노이즈를 준 student 를 학습한 뒤 그 student 를 새 teacher 로 반복해 기존 teacher 를 넘었다 — Pradeep et al. 2023 의 RankZephyr/RankVicuna 처럼 **7B 급 student** 에서나 확인된 패턴이다.

Caron et al. 2021(DINO)은 teacher 를 student 가중치의 EMA 로 갱신하고 centering·sharpening 으로 붕괴를 막는다. **DINO 식 EMA 는 여기 그대로 옮길 수 없다** — teacher 는 프롬프트 기반 LLM, student 는 6성분 선형결합이라 평균 낼 가중치 공간이 없다([RESEARCH-LTR-160.md §3-1](./RESEARCH-LTR-160.md#3-1-행동-로그-없이-가능한-대안-경로--llm-teacher-기반-학습)의 같은 결론을 승계).

옮길 수 있는 것은 Noisy Student 식 반복 self-training 뿐이다. 그러나 Shumailov et al. 2024(Nature)는 생성 모델을 자기 출력으로 반복 학습하면 model collapse(분포 오차 누적, 저빈도 사건 영구 소실)가 일어남을 보고한다. Gerstgrasser et al. 2024는 합성 데이터를 **누적**(원본 실데이터 유지 + 세대별 추가)하면 붕괴를 회피할 수 있음을 보인다 — 반복한다면 세대마다 골든셋(실라벨)을 유지·누적해야 한다는 근거다.

**종료 조건(사전 등록 형태)**: 반복은 **dev 로만** 돌린다(#146 도 dev-only 였다) — `evals/goldenset/GUIDE.md` 가 holdout 을 release 전용으로 두고 `unseal_holdout_labels(reason, commit_sha)` 호출과 `audit/holdout_runs.jsonl` 기록을 요구하므로, 매 세대 holdout 을 열면 봉인의 의미가 소모된다. 세대마다 dev 기준 paired bootstrap 95% CI 로 실제 개선을 확인하고, CI 가 0을 포함하면 즉시 `inconclusive`로 중단한다. holdout 은 반복이 끝나고 release 후보로 확정된 커밋에서 **한 번만** `unseal_holdout_labels(reason, commit_sha)` 로 연다. 현재는 1세대(teacher-fit)조차 no-op 을 못 넘으므로(§3), 반복 self-training 은 §13 착수 조건 충족 전에는 시작할 이유가 없다.

## 9. 평가 설계

#146 규약을 재사용한다(`evals/ablation/DECISION.md` 의 사전 등록 규약): arm·seed·case order 고정, **primary confirmatory metric 은 `overall.ndcgAtK.10` 1개**뿐이고 나머지는 exploratory. case-level primary delta 의 paired bootstrap 95% CI(resamples 2000, confidence 0.95)가 0을 포함하면 `inconclusive`. baseline 은 `pipeline` 0.782943·`scoring` 0.616852·**no-op(passthrough) 0.738210**(이 조사가 §3 에서 추가한 1급 baseline). 서로 다른 `datasetHash` 점수는 직접 비교 금지, holdout 은 release 전용 봉인이다.

지연 gate 는 [RESEARCH-LTR-160.md §3](./RESEARCH-LTR-160.md)가 사전 등록한 값을 승계한다 — 현 scorer(3.781ms/case) 대비 추가 **p50 5ms 이하·p95 15ms 이하**, I-22 종단 p50 60ms 이하. HCV(hard-constraint violation) 는 **0 회귀 gate**로 둔다 — `pipeline` arm 의 현재 HCV 는 0.000000 이다.

## 10. 안전 경계

student 경로에서도 다음을 그대로 지킨다.

- **`hard_filter.py` 재진입 금지**: 점수와 분리된 별도 컷(가격·금지 카테고리·금지 상품·must-exclude)이며 컷된 상품은 높은 점수로 재진입할 수 없다. auxiliary 항이 아무리 높은 점수를 줘도 이 컷을 우회하지 않는다.
- **#173 비표시 가격 유출 방어**: teacher 후보 payload 자체가 정밀 가격·평점·리뷰수를 담지 않는다(§2) — student 도 같은 입력 경계를 지킨다.
- **`productId` tiebreak**: 결정론 동점 처리는 student·teacher 모방 여부와 무관하게 유지한다.
- **degrade 기록**: 임베딩 누락·guest·recency 미주입은 값 0 + degrade 기록이며, teacher 모방이 이를 가짜 중립 프로필로 대체해서는 안 된다.

**teacher 가 틀린 케이스를 그대로 학습하는 문제**: §5 가 실측한 teacher 반복 분산(0.0678)과 순서 민감도(tau 0.3654)는 라벨 자체가 잡음임을 보인다. 대응은 세 가지다. ① **안전 축은 학습 대상에서 제외**한다 — hard filter·가격 유출 방지·tiebreak·degrade 는 §2 표에서 이미 결정론으로 남겼다. ② **HCV 자동 중지**: offline replay·shadow 단계에서 HCV 가 한 건이라도 나오면 즉시 중단한다(#161 §2.4 의 exploration guardrail 과 같은 규약). ③ **라벨 필터**: 순열 자기일관성으로 반복 간 불일치가 큰 케이스는 학습 라벨에서 제외하거나 낮은 가중치를 준다(§5 처방, 비용은 §6).

## 11. 판정: `no-go`

**현행 6성분 결정론 student 로의 teacher distillation은 `no-go`다.** §3~§6 실측이 논지 사슬의 각 고리를 수치로 막았다 — transfer set 합성·비용은 병목이 아니었고(§4(a)·§6), student 용량이 먼저 막혔다(§3: 오라클 상한이 no-op 과 사실상 같음). LLM teacher 자체나 #146 의 production 결정(pipeline 유지)을 뒤집지 않으며, [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md)의 `조건부`·[RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)의 `no-go` 도 그대로다.

## 12. offline 실험 arm(설계 확정 — 하나)

모델 학습·배포는 하지 않는다. §13 착수 조건이 충족된 뒤에만 실행하는 **단일** offline arm을 다음 순서로 확정한다.

1. **계측기 재검증**: 늘어난 골든 케이스로 `teacher − no-op` paired bootstrap 95% CI 를 다시 낸다. CI 가 0을 배제하지 못하면 여기서 **중단**한다(§13 조건 1).
2. **student basis 확장 오라클 상한**: §3-1 의 질의 조건부 피처(어휘 겹침·브랜드 일치·가격대 일치·카테고리 일치, 전부 순수 파이썬)를 추가한 basis 로 `e4_analyze.py` 와 같은 축퇴 배제 탐색을 다시 돌려 오라클 상한이 no-op 을 유의하게 넘는지 확인한다. 못 넘으면 **중단**한다(§13 조건 2).
3. **transfer set 합성 + 왕복 필터**: `e3_run.py` 를 K=20 고정으로 확장 실행(예산은 §6 표에서 목표 규모에 맞게 사전 승인), Promptagator 왕복 일관성 필터(K=1)로 내적 타당도가 낮은 질의를 제외한다.
4. **auxiliary 항 학습**: §7 형태(정답 항+teacher 항, α 조절)로 확장 basis 의 계수를 오프라인 적합하고 `app/core/config.py` 로 주입한다. `weight=0` 이 즉시 롤백이다.
5. **평가**: §9 규약대로 반복 중에는 **dev 로만** paired bootstrap 95% CI 를 낸다(primary `nDCG@10` 1개). HCV 0 회귀 gate, 지연 gate(추가 p50 5ms·p95 15ms) 통과가 필수다. **sealed holdout 은 이 arm 전체가 dev 에서 유의한 개선을 확정하고 release 후보로 커밋된 뒤에만** `unseal_holdout_labels(reason, commit_sha)` 로 **한 번** 연다 — 중간 단계·반복 세대마다 열지 않는다.
6. **중단 조건**: 1~5 어느 단계에서든 CI 가 0을 포함하거나 HCV·지연 gate 를 위반하면 그 단계에서 멈추고 다음 단계로 진행하지 않는다. 반복 self-training(§8)은 이 arm 이 1세대에서 유의한 개선을 보인 뒤에만 검토한다.

## 13. 착수 조건(재평가 트리거)

아래를 **모두** 충족해야 §12 offline arm 을 시작한다.

1. **계측기 먼저** — 순위 판별력 있는 골든 케이스를 늘려(현재 18건) `teacher − no-op` paired bootstrap 95% CI 가 **0을 배제**할 것. 필요 표본 수는 현재 델타 +0.044734 와 케이스 분산으로 재차 사전 등록한다.
2. **student 그릇** — 질의 조건부 피처를 추가한 basis 의 오라클 상한이 no-op(0.738210)을 유의하게 넘을 것(CI 가 0 배제). 신규 ML 의존성 없이 가능한 형태는 §3-1 을 본다.
3. **라벨 안정성** — 순열 자기일관성(k 회 집계)을 적용했을 때 top-1 일치율과 Kendall tau 가 목표치를 넘고, 비용 배수(k×, §6)를 감당할 수 있을 것. 현재 k=1 기준선은 E3 수치(tau 0.3654, top-1 47.2%).
4. **분포 검증 가능성** — `conversation_turns`에 벤치마크가 아닌 실사용 발화가 쌓이고(현재 distinct 25종·7명) 개인정보 검토를 통과해 MAUVE/C2ST 대조군을 만들 수 있을 것. 그 전에도 `decompose_case.filters_set`·`legs`·`chat_request.messageLength` 로 원문 없이 주변분포는 맞출 수 있다(§4(d)).
5. 롤백은 `app/core/config.py` 주입 한 번으로 성립한다(`home_reco_weight_profile=0` 선례).

**도입 금지 조건**(#161 형식 참고, 다음 중 하나라도 참이면 §12 arm 을 시작하지 않는다):

- 조건 1의 계측기 CI 가 0을 배제하지 못한 채로 arm 을 시작하려는 경우.
- 조건 2의 확장 basis 오라클 상한이 no-op 을 유의하게 넘지 못했는데 auxiliary 항 학습(§12 4단계)으로 건너뛰려는 경우.
- 실사용 검증 없이(조건 4 미충족) 합성 transfer set 만으로 production 배포를 결정하려는 경우.
- 신규 ML 의존성(basis 확장을 넘어서는 모델 계열)을 사람 승인 없이 도입하려는 경우.

**비범위**: 실제 모델 학습·배포, 행동 로그 수집·상관키 기록(#140), off-policy·bandit — 합성 라벨은 propensity 를 만들지 않으므로 [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)의 `no-go` 는 이 경로가 진행돼도 불변이다. 신규 ML 의존성 도입도 별도 사람 승인 게이트다.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- 2026-08-05 실측·문헌 조사(이 문서 집필의 유일한 인용 원천): sub-orchestrator 가 직접 수집·검증, E1~E4 산출물은 [research-275-harness/](./research-275-harness/)로 같이 커밋.
- `evals/ablation/baselines/20260803-dev-full-n5/`, `evals/ablation/DECISION.md`: 3-arm ablation baseline, dev 31건×N=5.
- `evals/scoring/baselines/dev-v1/comparison.md`: 커밋된 passthrough baseline 0.738210.
- `evals/goldenset/`, `evals/metrics/`: dev 31·sealed holdout 12, 결정론 metric runner.
- `evals/model_eval/pricing_manifest.json`: smart/fast 티어 단가.
- 2026-08-05 pg-profile `conversation_turns` 직접 실측(집계만 조회, 본문은 읽지 않음).
- `app/core/observability.py`, `app/core/conversation.py`, `db/profile/init/01_conversation_turns.sql`: 관측 로그 대 대화 저장소의 원문 보존 여부.
- `app/agents/buyer/recommendation/rerank.py`, `graph.py`: teacher 입력·후보 payload·출력 예산.
- `evals/scoring/components.py`, `scorer.py`, `hard_filter.py`, `app/core/config.py`: student 6성분·질의 의존성·안전 경계·기본 가중치.
- `CHANGELOG.md` #254/#148: pgvector 이관 p50 실측, `home_reco_weight_profile=0` 롤백 선례.
- [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md) §3(지연 gate)·§3-1(이 이슈의 출발점), [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md) §2.4(exploration guardrail 규약).
- 참고 문헌 절의 문헌은 전부 2026-08-05 WebSearch/WebFetch 로 원문 또는 공식 초록에서 직접 확인했다.

### 검증하지 못한 것

- **합성 질의가 실사용과 얼마나 닮았는지(외적 타당도)**: MAUVE·C2ST 계산에 필요한 실데이터 표본이 0이라 측정 자체가 불가능하다(§4(b)).
- **teacher−no-op 격차의 부호**: CI 가 0을 포함해 teacher 가 실제로 no-op 보다 나은지 나쁜지도 이 표본 규모로는 결론 내리지 못한다(§3).
- **Shumailov et al. 2024 의 2025년 Nature Author Correction 내용**: 존재만 확인했고 정정 내용은 읽지 못했다.
- **E1 latency p95 는 계산값 7,612.2ms, 이 조사 검토 과정에서 별도로 계산한 참고값은 7,590ms**(참고값은 커밋된 산출물이 아니라 다른 백분위 보간법을 쓴 값으로 추정된다) — 같은 원본 데이터에 서로 다른 보간법을 적용한 결과이며 커밋 산출물과의 불일치가 아니다. 어느 보간법이 맞는지는 확인하지 못했다.
- **E3 fallback 경로**(합성 질의 생성 실패 시 dev 골든 대체): 12/12 전량 성공해 실행되지 않았다 — 코드는 있으나 실측 검증되지 않았다.
- **E3 round-trip consistency(Promptagator 필터) 적용 실측**: 이번 파일럿은 순서/반복 민감도와 비용에 집중해 수행하지 않았다.
- **§6 캠페인 비용 시나리오**: E1-c 회귀식의 선형 외삽이며 재시도율·프롬프트 캐시 변동은 반영하지 않았다 — 실제 캠페인 전 재확인이 필요한 추정치다.

## 참고 문헌

- Geoffrey E. Hinton, Oriol Vinyals, Jeff Dean, "Distilling the Knowledge in a Neural Network", CoRR abs/1503.02531, 2015.
- Jiaxi Tang, Ke Wang, "Ranking Distillation: Learning Compact Ranking Models With High Performance for Recommender System", KDD 2018, pp. 2289–2298 (arXiv:1809.07428).
- Qizhe Xie, Minh-Thang Luong, Eduard H. Hovy, Quoc V. Le, "Self-training with Noisy Student improves ImageNet classification", CVPR 2020, pp. 10684–10695.
- Mathilde Caron, Hugo Touvron, Ishan Misra, Hervé Jégou, Julien Mairal, Piotr Bojanowski, Armand Joulin, "Emerging Properties in Self-Supervised Vision Transformers", ICCV 2021, pp. 9630–9640.
- Feiran Huang et al., "Large Language Model Simulator for Cold-Start Recommendation", WSDM 2025 (arXiv:2402.09176).
- Jianling Wang, Haokai Lu, James Caverlee, Ed H. Chi, Minmin Chen, "Large Language Models as Data Augmenters for Cold-Start Item Recommendation", arXiv:2402.11724, 2024.
- Weiwei Sun, Lingyong Yan, Xinyu Ma, Shuaiqiang Wang, Pengjie Ren, Zhumin Chen, Dawei Yin, Zhaochun Ren, "Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents", EMNLP 2023, pp. 14918–14937 (arXiv:2304.09542).
- Ronak Pradeep, Sahel Sharifymoghaddam, Jimmy Lin, "RankZephyr: Effective and Robust Zero-Shot Listwise Reranking is a Breeze!", arXiv:2312.02724, 2023.
- Zhuyun Dai, Vincent Y. Zhao, Ji Ma, Yi Luan, Jianmo Ni, Jing Lu, Anton Bakalov, Kelvin Guu, Keith B. Hall, Ming-Wei Chang, "Promptagator: Few-shot Dense Retrieval From 8 Examples", ICLR 2023 (arXiv:2209.11755).
- Raphael Tang, Xinyu Zhang, Xueguang Ma, Jimmy Lin, Ferhan Ture, "Found in the Middle: Permutation Self-Consistency Improves Listwise Ranking in Large Language Models", NAACL 2024 (arXiv:2310.07712).
- Peiyi Wang, Lei Li, Liang Chen, Zefan Cai, Dawei Zhu, Binghuai Lin, Yunbo Cao, Qi Liu, Tianyu Liu, Zhifang Sui, "Large Language Models are not Fair Evaluators", ACL 2024 (arXiv:2305.17926).
- Ilia Shumailov, Zakhar Shumaylov, Yiren Zhao, Nicolas Papernot, Ross Anderson, Yarin Gal, "AI models collapse when trained on recursively generated data", Nature 631, 755–759 (2024).
- Matthias Gerstgrasser et al., "Is Model Collapse Inevitable? Breaking the Curse of Recursion by Accumulating Real and Synthetic Data", arXiv:2404.01413, 2024.
- Jang Hyun Cho, Bharath Hariharan, "On the Efficacy of Knowledge Distillation", ICCV 2019.
- Krishna Pillutla, Swabha Swayamdipta, Rowan Zellers, John Thickstun, Sean Welleck, Yejin Choi, Zaid Harchaoui, "MAUVE: Measuring the Gap Between Neural Text and Human Text using Divergence Frontiers", NeurIPS 2021 (arXiv:2102.01454).
