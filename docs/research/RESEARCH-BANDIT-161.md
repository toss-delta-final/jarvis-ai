# contextual bandit·RL 도입 판단 — [이슈 #161](https://github.com/toss-delta-final/jarvis-ai/issues/161) · 작성일 2026-08-03 · 상태(`no-go`)

## 요약

**판정: `no-go`.** 현재 traffic은 실사용 0이고, off-policy 평가에 필수인 행동 정책 propensity와 상품 귀속 conversion reward가 모두 없다.

- `behavior_events`는 스모크 테스트 `recommendation_generated` 1행뿐이며 `member`·`orders`는 0행이다.
- 현 랭킹은 `productId` tiebreak까지 결정론적이어서 선택확률이 사실상 0/1이고, `recommendation_list`에 policy·propensity 필드가 없다.
- CTR만 최적화할 수 없는데 `purchase_complete`가 상품에 귀속되지 않아 장기 conversion reward 정의도 현재 성립하지 않는다.

관련 판단: 병렬인 협업 후보는 [RESEARCH-CF-159.md](./RESEARCH-CF-159.md), 선행 offline reranker 판단은 [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md)를 본다. #161 재검토는 #160 판정·계측이 끝난 뒤다.

## 1. 이 리포의 실측 데이터 현황

| 항목 | 2026-08-03 실측 | bandit 의미 |
|---|---:|---|
| `behavior_events` | 1 | 실사용 reward·context 표본 0 |
| 유일 이벤트 | `recommendation_generated`, 익명, itemCount 8 | 스모크 테스트 흔적이며 학습 불가 |
| `recommendation_list` / `_item` | 1 / 8 | action snapshot 구조만 존재 |
| `member` / `guest` | 0 / 0 | 식별 가능한 traffic 0 |
| `orders` / `order_item` | 0 / 0 | conversion·매출 reward 0 |
| `claim` | 0 | 취소·반품으로 뒤집힌 reward 관측 0 |
| propensity 필드 | 없음 | IPS/SNIPS/DR 평가 불가 |

I-21은 추천 실행 상관키 `recommendationRequestId`와 목록 키 `listId`를 제공하고, Spring schema는 `surface`·`position`·노출 목록을 저장할 수 있다. 즉 노출 snapshot의 그릇은 있다. 그러나 `recommendation_list`에는 `source`·`list_type`만 있고 정책 id, 후보별 선택확률, randomization unit이 없다. “데이터가 덜 쌓였다”보다 먼저 **로깅 정책의 계약이 빠졌다**.

### 지금 가능한 것

- 도입 금지 조건, reward 정의, hard guardrail, 로깅 계약을 사전 등록하는 것.
- 현 결정론 scoring과 pipeline을 control로 고정하고 weight 0 rollback 설계를 준비하는 것.
- synthetic replay로 propensity schema·IPS 계산 코드의 수학적 sanity만 검증하는 것. 이 결과를 production 효과 근거로 쓰지는 않는다.

### 데이터가 쌓이면 가능한 것

- 기록된 propensity로 IPS/SNIPS/DR 계열 off-policy evaluation을 수행하는 것.
- click→cart→checkout→purchase→취소/반품까지 delayed reward를 비교하는 것.
- hard filter 통과 후보 안에서 제한된 exploration을 단계적으로 켜는 것.

## 2. Jarvis에서 bandit이 성립하기 위한 계약

### 2.1 traffic과 표본 자릿수

목록당 최대 9개 행동 후보, 여러 context feature(surface·profile·listType·semantic 등), 낮을 수 있는 conversion 확률을 함께 추정하려면 각 action/context slice에 반복 노출이 필요하다. 내부 screening 관점에서 production 판단에는 최소 `10^5` 노출, 세분화가 많거나 conversion을 보려면 `10^6` 자릿수를 예상한다. 이 자릿수는 이번에 인용한 문헌에서 확인한 수치가 아니라 Jarvis의 후보 수와 slice 수를 보고 정한 내부 판단이며, 정확한 최소량은 실제 CTR·전환율·분산으로 power analysis를 다시 해야 한다.

현재는 실사용 노출이 0이므로 어느 자릿수와 비교해도 학습 가능 여부는 `no`다. `member` 0명·`orders` 0건에서는 context 일반화와 장기 reward를 검증할 holdout도 만들 수 없다.

bandit의 randomization unit은 `list_id`/request뿐 아니라 `session_key`가 될 수도 있어 익명 세션 안에서 arm을 고정할 수 있다. 그러나 세션 단위로 무작위화해도 실제 선택확률을 기록하지 않으면 off-policy 평가는 불가능하므로 propensity 선행 요구와 #161의 `no-go`는 그대로다.

### 2.2 propensity 기록이 진짜 선행 조건

Li et al. 2011은 contextual-bandit 추천 알고리즘의 unbiased offline replay 평가를 다룬다. 이 평가와 IPS·SNIPS·DR 계열은 “그 시점의 행동 정책이 선택한 action의 확률”을 알아야 하며, 사후에 현재 모델로 확률을 재계산하면 당시 후보·feature·정책 버전이 달라질 수 있어 유효하지 않다. 최소 로깅 단위는 다음이다.

- `policy_id` / `policy_version` / `model_version`
- randomization unit(`list_id` 또는 request)과 seed/nonce
- 후보 집합과 각 후보의 `action_probability`
- 실제 선택 action, 순위, `surface`, context/feature schema version
- control/experiment arm과 exploration budget

현 scorer는 같은 snapshot·config에서 `productId` 오름차순 tiebreak로 결정적이다(`evals/scoring/scorer.py`). 이 정책의 propensity는 선택 상품 1, 나머지 0이어서 support가 없는 action의 counterfactual을 평가할 수 없다. 더구나 현재 저장 컬럼에는 확률을 둘 자리도 없다. 따라서 **policy logging schema 추가와 실제 확률 저장이 bandit의 첫 구현**이며, Li et al. 2011의 replay 조건을 만족하는지 검증한 뒤 bandit 모델을 배포해야 한다.

LLM teacher가 합성한 label은 propensity를 만들어 주지 않는다. off-policy 평가가 요구하는 값은 실제 로깅 정책이 그 시점에 해당 action을 고를 확률이므로 오프라인 합성으로 대체할 수 없다. [RESEARCH-LTR-160.md §3-1](./RESEARCH-LTR-160.md#3-1-행동-로그-없이-가능한-대안-경로--llm-teacher-기반-학습)이 진행돼도 #161의 `no-go`와 로깅 계약 선행 요구는 그대로다.

### 2.3 reward: CTR만으로 부족한 이유

클릭만 올리면 호기심을 자극하지만 구매에 불리한 상품, 상단 노출에 유리한 상품, 반복 클릭을 유도하는 상품을 과대평가할 수 있다. Jarvis reward는 최소 다음 단계를 분리해야 한다.

| 단계 | 즉시성 | reward 역할 | 현재 상태 |
|---|---|---|---|
| 추천 유래 `product_view` | 빠름 | click proxy | `list_id`·`position` FE 전송 여부 확인 불가 |
| `add_to_cart` | 빠름~중간 | stronger intent | 데이터 0 |
| `checkout_start` | 중간 | conversion intent | 데이터 0 |
| `purchase_complete` | 지연 | 구매 conversion | `product_id` 미귀속으로 사용 불가 |
| 주문 상태·claim | 수일 이상 가능 | 취소·반품 시 reward 차감/뒤집기 | 주문·claim 데이터 0 |

`purchase_complete`는 `properties.orderId`만 있고 `product_id=NULL`이라 현 이벤트 스트림에서 상품 reward가 되지 않는다(`docs/api-spec.md` v0.17.4 §4.4 I-13). 근본 수정 `jarvis-backend#62` 배포 뒤에야 conversion reward가 성립한다. I-14 주문 상태 전이와 취소·반품 `claim`은 보상을 며칠 뒤 바꾸므로, 확정 reward window와 provisional/final 상태를 나눠야 한다. 매출·구매 존재 권위는 I-6/I-7/I-14이며 이벤트 0을 “구매 없음”으로 해석하면 안 된다.

I-16 `preChurnSignals.zeroResultSearchSessions`도 E-1의 검색 결과 수 미적재 때문에 상시 0이다(`docs/api-spec.md` v0.19.1). 이 값을 장기 불만족 reward나 guardrail로 사용하면 항상 안전한 것처럼 보이므로, 결과 수 계약이 생기기 전 reward 후보에서 제외한다.

### 2.4 exploration guardrail

탐색은 `hard_filter.py`를 통과한 후보 **안에서만** 허용한다. 가격 상한, 금지 카테고리·상품, must-exclude, 최근 구매 dedup은 action space 생성 전에 적용하고 bandit 점수로 재진입시킬 수 없다. guest는 가짜 중립 프로필을 만들지 않고 missing profile degrade를 유지한다.

현재 `pipeline` arm의 hard-constraint violation rate는 0.000000이다(`evals/ablation/DECISION.md`). 이를 회귀 gate로 삼아 offline replay·shadow·canary 각 단계에서 HCV가 한 건이라도 나오면 exploration을 자동 중지한다. 예산 상한·품절/숨김 드롭 등 Spring 표시 경계도 유지한다.

## 3. 후보 비교표

| 후보 | 필요한 로깅 | reward 요구 | off-policy 평가 | rollback | 현재 판단 |
|---|---|---|---|---|---|
| 결정론 scoring 유지 | 현행 | 불필요 | 비교 control | 이미 가능 | ✅ control |
| 고정 소규모 A/B | arm assignment·후보 snapshot | click부터 가능 | direct randomized comparison | arm 0% | 로깅 검증 1순위 |
| epsilon-greedy contextual bandit | action별 propensity·policy version | click/cart, 이후 conversion | IPS/SNIPS/DR 가능 | epsilon=0 | 조건 충족 뒤 제한 canary |
| LinUCB contextual bandit | context·action feature와 선택 propensity | click/cart, 이후 conversion | replay·IPS/SNIPS/DR 가능 | allocation/weight=0 | 데이터 충족 뒤 비교 arm |
| Thompson/UCB 계열 | posterior/stat state + propensity 재현 | 안정적 reward | 구현에 따라 복잡 | exploration weight=0 | 후순위 |
| 장기 RL | trajectory·상태 전이·delayed reward | 구매·취소·반품 필수 | 장기 OPE 난도 높음 | 정책 전체 switch | 현재 범위에서 제외 |

Li et al. 2010은 콘텐츠·사용자 정보를 함께 쓰는 contextual bandit으로 disjoint linear model 기반 LinUCB를 제시한다. 동적으로 바뀌는 콘텐츠 풀에서 전통적 CF 적용이 어렵다는 문제 의식은 #159와의 차이를 설명하지만, Jarvis에서는 context·reward·propensity가 모두 비어 있어 LinUCB 역시 지금 학습할 수 없다.

초기 online 실험은 contextual bandit보다 먼저 고정 A/B로 logging·attribution을 검증해야 한다. control과 exploration 후보 생성은 동일 hard filter를 공유하고, 모델 선택을 config로 주입한다. `app/core/config.py`의 기존 `home_reco_weight_profile=0` 롤백 사례처럼 `bandit_exploration_weight=0` 또는 traffic allocation 0%로 즉시 결정론 control에 돌아가게 한다. 튜너블 하드코딩은 금지한다.

## 4. 판정: `no-go`

현재 traffic, propensity, conversion attribution 중 하나도 충족하지 못한다. 특히 propensity는 데이터가 자연히 쌓인다고 생기지 않으므로 로깅 계약을 먼저 바꾸지 않으면 시간이 지나도 off-policy 평가가 영구히 불가능하다. #161 production 도입은 `no-go`이며 #160 offline LTR 판정과 로깅 품질 검증 뒤에만 재검토한다.

## 5. offline prototype 범위 (하지 않는 것과 허용 범위)

현재 실제 traffic을 사용한 bandit 학습·정책 비교·CTR uplift 주장은 하지 않는다. synthetic context로 estimator가 알려진 정답을 복원하는지, propensity 0·누락·극단 weight를 fail-closed하는지만 단위 수준에서 검증할 수 있다.

조건 충족 뒤 순서는 다음으로 제한한다.

1. 결정론 control + 매우 작은 randomized logging arm으로 support를 만든다. Li et al. 2011의 replay 전제를 만족하도록 당시 후보와 선택확률을 함께 고정한다.
2. policy version·후보 snapshot·propensity coverage를 먼저 감사한다.
3. click reward로 IPS, Swaminathan and Joachims 2015의 self-normalized estimator(SNIPS), Dudík et al. 2011의 doubly robust estimator(DR)를 모두 산출하고 estimator 간 방향이 다르면 `inconclusive`로 둔다. SNIPS도 propensity 누락·0과 극단 weight를 자동으로 정당화하지 않으므로 해당 행은 fail-closed한다.
4. `jarvis-backend#62` 이후 conversion 및 취소/반품 보상 window를 별도 평가한다.
5. shadow→1% canary→단계 확대 순으로 진행하고 HCV·latency·conversion guardrail 위반 시 allocation 0%로 롤백한다.

## 6. 착수 조건 (재검토 트리거)

### 명시적 도입 금지 조건

다음 중 **하나라도 참이면 도입 금지**다.

1. 후보별 propensity가 추천 시점에 기록되지 않거나 coverage가 100% 미만이다.
2. `purchase_complete` 상품 귀속이 없거나 I-6/I-7/I-14 주문 권위와 정합되지 않는다.
3. 실제 추천 노출이 28일간 100,000개 미만이거나 각 활성 arm 할당이 10,000개 미만이다.
4. target policy가 평가하려는 action 중 logging policy 확률 0인 행이 단 1건이라도 있다.
5. IPS/SNIPS/DR과 direct A/B 중 어떤 방식으로도 offline/online sanity를 교차 확인할 수 없다.
6. hard filter 후 action-space 생성과 HCV=0 회귀 gate가 자동화되지 않았다.
7. config 한 번으로 exploration allocation/weight를 0으로 되돌리고 결정론 scorer로 복귀할 수 없다.
8. #160의 누출 없는 feature snapshot·baseline 판정이 완료되지 않았다.

### 관측 가능한 재검토 시점

- #160 offline LTR 판정 완료 후, 연속 28일 `recommendation_list` 100,000개 이상.
- 같은 기간 distinct 식별 사용자/동의된 세션 10,000개 이상, 추천 유래 `product_view` 10,000건 이상, 상품 귀속 purchase 1,000건 이상.
- randomized logging arm의 각 action/arm이 10,000회 이상 노출되고 propensity·policy version·후보 snapshot non-null coverage 100%.
- 일 단위 purchase reward가 I-6/I-7/I-14 권위와 5% 이내이고, 취소·반품 반영 final reward coverage 95% 이상.
- shadow replay에서 HCV 0건, propensity validation error 0건, control 대비 추가 p95 latency 15ms 이하.

`10^5` 노출과 arm당 10,000회는 이번에 인용한 문헌에서 확인한 수치가 아니라 9개까지의 후보와 여러 context slice에 최소 반복을 확보하려는 이 리포의 내부 screening threshold다. 최초 4주 실제 CTR·conversion 분산을 얻은 뒤 최소 detectable effect와 confidence interval을 사전 등록해 확대 표본량을 다시 정한다.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- 2026-08-03 MariaDB 실측: 구현 팩킷 §1-B~§1-D(이 문서에서는 DB 재측정하지 않음).
- `docs/api-spec.md` v0.17.1 §4.2 I-21: 상관키·목록당 최대 9개·추천 저장 규약.
- `docs/api-spec.md` v0.17.4 §4.4 I-13: `purchase_complete` 상품 미귀속과 주문 권위.
- `evals/scoring/scorer.py`, `hard_filter.py`, `app/core/config.py`: 결정론 tiebreak·hard filter·config 주입.
- `evals/ablation/DECISION.md`, `baselines/20260803-dev-full-n5/`: pipeline HCV 0.000000, scoring 3.781ms.
- `CHANGELOG.md` #148: `home_reco_weight_profile=0` rollback, I-22 종단 p50 45ms.
- [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md): 선행 label·snapshot·baseline 조건.

### 검증하지 못한 것

- FE가 추천 유래 `product_view`에 `listId`·`position`·`recommendationRequestId`를 실제 전송하는지: FE 저장소가 없어 확인 불가.
- `jarvis-backend#62` 배포 상태, 주문/claim의 실제 지연분포와 취소·반품 귀속: 이 worktree 밖이라 확인 불가.
- 현재 production traffic/CTR/conversion: 제공된 DB에는 실사용 traffic이 없고 별도 운영 telemetry를 확인하지 못함.

## 참고 문헌

- Lihong Li, Wei Chu, John Langford, Robert E. Schapire, “A Contextual-Bandit Approach to Personalized News Article Recommendation”, WWW 2010.
- Lihong Li, Wei Chu, John Langford, Xuanhui Wang, “Unbiased Offline Evaluation of Contextual-Bandit-Based News Article Recommendation Algorithms”, WSDM 2011, pp. 297–306, DOI `10.1145/1935826.1935878`.
- Miroslav Dudík, John Langford, Lihong Li, “Doubly Robust Policy Evaluation and Learning”, ICML 2011, pp. 1097–1104.
- Adith Swaminathan, Thorsten Joachims, “The Self-Normalized Estimator for Counterfactual Learning”, NIPS 2015, pp. 3231–3239.
