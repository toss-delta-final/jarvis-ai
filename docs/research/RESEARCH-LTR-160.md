# Learning-to-Rank 도입 판단 — [이슈 #160](https://github.com/toss-delta-final/jarvis-ai/issues/160) · 작성일 2026-08-03 · 상태(`조건부`)

## 요약

**판정: `조건부`.** Jarvis에는 impression·순위·상관키 저장 구조와 재사용 가능한 scoring feature가 있지만, 실사용 학습 행은 사실상 0이고 전환 label 및 시점 피처 snapshot이 결손이다.

- `recommendation_list_item(list_id, position, product_id)`으로 true impression negative를 만들 구조는 이미 있다.
- 클릭은 `list_id`·`position`이 실린 `product_view`로 복원할 수 있으나 FE가 실제 필드를 보내는지는 확인 불가이고, `purchase_complete`는 현재 상품에 귀속되지 않는다.
- 추천 이후 갱신되는 `reviewCount`·`rating`·`product_document.extras`의 과거 버전이 없어 그대로 학습하면 leakage를 피할 수 없다.

관련 판단: 협업 신호는 [RESEARCH-CF-159.md](./RESEARCH-CF-159.md), LTR 이후의 online 최적화는 [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)를 본다. #161은 이 문서의 데이터·평가 판정이 끝난 뒤의 후행 작업이다.

## 1. 이 리포의 실측 데이터 현황

| 자산 | 현재 상태 | LTR 의미 |
|---|---|---|
| `recommendation_list` | 1행 | 추천 시점·surface·list type 저장 구조는 있으나 실사용 표본 없음 |
| `recommendation_list_item` | 8행 | `(list_id, position)` PK와 `product_id`로 impression snapshot 가능 |
| `behavior_events` | 1행, `recommendation_generated` | click/conversion 학습 행 0 |
| `client_event_id` | UNIQUE 컬럼 존재 | 중복 배제 장치 존재 |
| `recommendationRequestId` / `listId` | I-21 계약 존재 | 추천 실행·목록 상관키 축은 해소 |
| `surface` | `CHAT`/`HOME`, Spring이 경로로 저장 | 지면별 분포 차이를 slice/feature로 사용 가능 |
| `product_document.embedding` | 7,220/7,220 non-null | semantic cosine feature 전량 계산 가능 |
| 구매 이벤트 | `purchase_complete.product_id` 미귀속 | 현 이벤트 조인으로 conversion label 생성 불가 |

AI 카탈로그는 7,220개 전량 임베딩을 보유하지만, MariaDB의 `member`·`orders`·`order_item`은 모두 0행이다. `review` 126,313행도 `member_id`가 전량 `NULL`이고 추천 당시 값의 snapshot이 아니다. 리뷰 수는 시드 README 표기 126,957건과 달라 2026-08-03 MariaDB 실측 126,313건을 채택했다.

### 지금 가능한 것

- 학습용 row schema, label window, leakage 금지 규칙을 문서화하는 것.
- 현 scoring 6성분을 고정 fixture에서 feature로 추출해 inference adapter의 형태를 검증하는 것.
- `pipeline`·`scoring` baseline과 같은 사전 등록 ablation 설계를 준비하는 것.

### 데이터가 쌓이면 가능한 것

- 목록 snapshot에서 노출됐지만 행동하지 않은 상품을 true impression negative로 만드는 것.
- `product_view`·`add_to_cart`·주문 귀속을 이용해 graded relevance를 학습하는 것.
- 추천 시점 feature snapshot으로 pointwise/pairwise/listwise 모델을 누출 없이 비교하는 것.

## 2. Jarvis용 학습 데이터·피처·누출 규칙

### 2.1 label과 attribution

추천 한 건의 impression은 `recommendation_list.created_at`과 `recommendation_list_item(list_id, position, product_id)`로 고정한다. 이후 동일 `list_id`·`product_id`로 들어온 이벤트를 정해진 window 안에서 결합해 다음처럼 graded label을 만들 수 있다.

| 관측 | label 예시 | 현재 가능 여부 | 주의 |
|---|---:|:---:|---|
| 노출만, 행동 없음 | 0 | 구조상 가능 | 목록 조회/실제 화면 노출 차이는 별도 확인 필요 |
| 추천 유래 `product_view` | 1 | ⚠️ | `list_id`·`position`·상관키 FE 전송 여부 확인 불가 |
| `add_to_cart` / `checkout_start` | 2 | ⚠️ | 동일 목록·상품 attribution과 window 필요 |
| `purchase_complete` | 3 | ❌ | 현재 `product_id=NULL`, 상품 조인 탈락 |

전환 대안은 I-6/I-7/I-14의 주문 권위 또는 주문 상세(I-19 등)에서 `order_item.product_id`를 추천 목록과 귀속하는 경로다. 다만 이것은 이벤트 조인이 아니라 주문 데이터 조인이다. 주문 생성·상태 전이·취소·반품에 따른 지연과 정합 규칙, 추천과 구매 사이 attribution window를 별도로 정의해야 한다. 현재 `orders`·`order_item`이 0행이라 대안도 아직 실측할 수 없다.

I-16 `preChurnSignals.zeroResultSearchSessions`는 E-1이 검색 결과 수를 적재하지 않아 상시 0이다(`docs/api-spec.md` v0.19.1). 이 값은 현재 negative label이나 context feature로 사용할 수 없고, 0을 “zero-result가 없었다”로 해석해서도 안 된다.

### 2.2 negative sampling

`recommendation_list_item`이 목록 전체를 보존하므로 random catalog negative보다 **true impression negative**가 우선이다. 사용자가 실제로 볼 수 있었던 같은 목록의 대안이라 difficulty와 노출 문맥이 positive에 가깝고, random negative가 만드는 지나치게 쉬운 분류 문제를 피한다.

I-21은 목록당 최대 9개다(`docs/api-spec.md` v0.17.1 §4.2). 9개가 모두 노출되고 그중 positive가 정확히 1개라면 positive:negative는 `1:8`; positive가 여러 개거나 목록이 짧으면 비율은 달라진다. 따라서 목록별 전량 negative를 먼저 보존하고, 학습 비용 때문에 줄일 때만 `list_id` 단위로 균일 sampling하며 원래 노출 수와 sampling probability를 함께 기록한다. 비노출 random negative는 별도 hard-negative 실험 arm으로만 둔다.

### 2.3 추천 이후 정보 누출 금지

모든 feature는 `recommendation_list.created_at` **이전 또는 그 시점**에 알 수 있었어야 한다. 다음 값은 현재 상태를 나중에 조회하면 누출된다.

- `review.rating`·`reviewCount`: 추천 이후 새 리뷰로 바뀔 수 있다.
- 상품 `updated_at`: 추천 이후 가격·상태·설명이 바뀔 수 있다.
- `product_document.extras`: I-17 배치가 약 5분 주기로 제자리 upsert하며 과거 버전이 남지 않는다. `CHANGELOG.md` #148 C-18은 이 때문에 당시 피처 상태 복원이 구조적으로 불가능하다고 기록한다.
- 구매/장바구니 기반 profile·recent-purchase feature: 추천 이후 행동을 섞으면 label을 feature에 되먹인다.

대응은 두 가지뿐이다. (A) 추천 시점에 모델 입력 feature를 별도 snapshot으로 저장하거나, (B) embedding model id처럼 시점 불변임을 입증할 수 있는 feature만 학습한다. A가 권장된다. 현재 `recommendation_list`에는 정책 결과는 있으나 scoring 6성분의 당시 값이 없어 새 저장 요구가 필요하다.

### 2.4 feature schema

현 scoring의 semantic cosine, profile match, popularity, recency, diversity bonus, recent-purchase penalty를 그대로 수치 feature로 쓸 수 있다(`evals/scoring/components.py`, `scorer.py`). 여기에 `listType`(`PICK_ONE`/`BUY_ALL`), `surface`(`CHAT`/`HOME`), 후보 수, guest/degrade flag를 더한다.

Hidasi et al. 2016이 다룬 session-based 신호처럼 직전 조회 상품, 세션 내 상품 전이, 카테고리 이동은 사용자 식별 없이도 얻을 수 있는 context feature다. Jarvis에서는 이를 `behavior_events.session_key` 안의 시간순 이벤트로 표현할 수 있다. 다만 현재 실사용 세션 이벤트가 0이므로 #160의 `조건부` 판정과 §6 임계값은 바뀌지 않는다.

`position`은 노출 편향을 추정하는 학습 보조 정보지만, **재랭킹 전 inference 시점에는 최종 position을 알 수 없다.** 이를 일반 feature로 넣으면 train/serve skew가 생긴다. Joachims et al. 2017은 click의 position bias에 대해 rank-conditional examination probability를 propensity로 추정하고 로그 상호작용을 inverse-propensity weighting하는 처리를 제시한다. 따라서 Jarvis도 실제 노출 `position`은 serving feature가 아니라 IPS debiasing에 쓰고, 1차 점수의 provisional rank가 필요하면 별도 이름으로 저장해 학습·서빙 양쪽에서 같은 계산을 해야 한다.

이 방식도 추천 시점의 rank와 click 로그가 함께 보존되어야 하므로, 현 FE 전송 여부 확인과 feature snapshot 결손을 해결하지 않으면 적용할 수 없다. 문헌은 position을 처리할 방법을 제공하지만 Jarvis의 입력 부재라는 판정 근거는 그대로다.

## 3. 후보 비교표

| 모델 계열 | 필요 label량 | 구현 난도 | 서빙 지연 판단 | 의존성 추가 | 롤백 용이성 | Jarvis 1차 후보 |
|---|---|---|---|---|---|:---:|
| pointwise logistic | 낮음~중간 | 낮음 | 수치 feature dot-product라 3.781ms scoring baseline에 가장 근접 가능 | 표준 구현이면 없거나 최소 | 계수/weight 0으로 쉬움 | ✅ |
| pointwise GBDT | 중간 | 중간 | 작은 tree ensemble은 예산 내 가능하나 실제 p50 계측 필요 | 보통 신규 ML runtime 필요 | 모델 파일/feature schema 버전 필요 | ✅ 비교 arm |
| pairwise RankNet / LambdaRank 계열 | 중간~높음 | 중간~높음 | model 크기에 따라 증가, 목록당 후보 전체 inference 필요 | 학습 framework 가능성 큼 | pointwise보다 복잡 | 후순위 |
| listwise LambdaMART / ListNet 계열 | 높음 | 높음 | tree/ensemble 또는 목록 전체 처리 비용, benchmark 필수 | 대개 신규 학습·서빙 의존성 | model switch와 schema 운영 필요 | 데이터 충족 뒤 |

Burges 2010은 RankNet→LambdaRank→LambdaMART의 계보를 설명하고 LambdaMART ranker 앙상블이 2010 Yahoo! Learning To Rank Challenge Track 1에서 우승했다고 기록한다. 따라서 LambdaMART의 후순위 판정은 기법의 성능 가능성을 낮게 본 것이 아니라, 현재 Jarvis의 label 0·신규 의존성 승인·feature snapshot·서빙 benchmark가 선행되지 않았기 때문이다.

지연 비교 기준은 결정론 `scoring` 3.781ms/case, I-22 홈 종단 p50 45ms(예산 3s), 채팅 스트림 전체 상한 30s다(`evals/ablation/DECISION.md`, `CHANGELOG.md` #148/#138). 3s나 30s가 크다고 모델 시간을 임의 소비해서는 안 된다. 초기 gate는 **현 scorer 대비 추가 p50 5ms 이하·p95 15ms 이하**, I-22 종단 p50 60ms 이하로 사전 등록한다. 이는 외부 SLA가 아니라 현재 45ms 경로의 회귀를 조기에 막기 위한 내부 예산이다.

신규 ML 의존성은 팀 규칙상 사람 승인 게이트다. 의존성을 추가하지 않고 고정 계수 pointwise prototype을 먼저 만들고, GBDT/Lambda 계열은 데이터 승리와 dependency 승인을 모두 받은 뒤 비교한다.

## 3-1. 행동 로그 없이 가능한 대안 경로 — LLM teacher 기반 학습

### 발상과 Jarvis 대응

Hinton et al. 2015의 knowledge distillation은 강한 teacher의 지식을 작은 student로 옮기는 출발점이다. 추천에서는 Tang and Wang 2018이 teacher 추천 목록의 top-K 항목으로 student 점수를 유도했다. Jarvis에서는 `pipeline` arm의 LLM rerank를 teacher, 결정론 6성분 scorer를 student로 둘 수 있다.

ablation 실측은 `pipeline` nDCG@10 0.782943·6,362.729ms/case·$0.000897/case, `scoring` nDCG@10 0.616852·0 calls·3.781ms/case다(`evals/ablation/DECISION.md`). 격차는 0.166이다. teacher가 이미 production 기준이므로 목표는 품질 상한 돌파가 아니라 그 격차 일부를 3.781ms 가격으로 가져와 비용·지연을 줄이는 것이다.

### transfer set과 coupled funnel

진짜 병목은 teacher를 실행할 transfer query다. 골든셋은 dev 31건과 sealed holdout 12건, 합계 43건뿐이고, #136 관측 로그는 개인정보 규약상 사용자 message 원문을 금지해 길이와 peppered HMAC 지문만 남긴다(`CHANGELOG.md`). 따라서 질의를 합성해야 하며, 합성 분포가 실사용과 어긋나면 student는 엉뚱한 목표에 최적화된다.

Huang et al. 2025의 ColdLLM은 값싼 필터로 후보를 크게 줄인 뒤 좁은 집합에만 LLM을 적용하는 coupled funnel로 비용을 제한한다. Jarvis에는 7,220건을 검색하는 pgvector HNSW가 이미 있고 #148 실측 p50은 39ms다. 이를 먼저 적용해 후보를 수십 개로 줄인 뒤 teacher를 호출하면 비용과 합성 분포의 탐색 범위를 함께 제한할 수 있다.

teacher 단가를 $0.000897/case로 선형 외삽하면 1,000질의는 약 $0.9, 10,000질의는 약 $9, 100,000질의는 약 $90다. 비교로 기존 ablation 전량은 445 calls·$0.28132815였다. 이 값은 고정 case당 단가의 단순 외삽이며, 실제 transfer set의 길이·후보 수·재시도율을 포함한 실행으로 다시 확인해야 한다.

### 보조 항과 반복 self-training

Wang et al. 2024는 LLM 합성 신호를 auxiliary pairwise loss로 추천 모델에 주입하며 전면 대체하지 않는다. Jarvis도 합성 기여를 별도 가중 항으로 두고 `weight=0`을 즉시 롤백으로 삼는 편이 안전하다. 이는 #148의 `home_reco_weight_profile=0` 사례와 같은 규약이며 튜너블은 `app/core/config.py`로 주입한다.

고정 teacher 모방의 상한은 teacher nDCG@10 0.782943이고 student는 보통 그 아래에 착지한다. Xie et al. 2020의 Noisy Student는 teacher가 pseudo-label을 만들고 더 크고 노이즈를 준 student를 학습한 뒤 그 student를 새 teacher로 반복해 기존 teacher를 넘었다.

Caron et al. 2021의 DINO는 teacher를 student 가중치의 EMA로 갱신하고 teacher에는 stop-gradient를 적용하며 서로 반대 방향인 centering과 sharpening의 균형으로 붕괴를 막는다. 그러나 DINO는 같은 아키텍처의 가중치 평균이 가능한 반면 Jarvis teacher는 프롬프트 기반 LLM, student는 6성분 선형결합이라 평균 낼 가중치 공간이 없다.

따라서 옮길 수 있는 것은 DINO식 EMA가 아니라 Noisy Student식 반복 self-training이다. 반복마다 sealed holdout과 bootstrap 95% CI로 실제 개선을 확인하지 않으면 teacher 오류와 합성 분포 편향을 증폭시킬 수 있으므로, CI가 0을 포함하면 `inconclusive`로 중단한다.

### 안전 경계와 이슈 범위

student도 `hard_filter` 경계, #173의 비표시 정밀 가격 유출 방어, `productId` tiebreak, degrade 기록을 그대로 지켜야 한다. teacher가 틀린 케이스도 그대로 학습되므로 이 불변식은 모방 대상이 아니다. ablation에서 `scoring` Filter Accuracy는 1.000000, `pipeline`은 0.063519로 서로 잘하는 축이 다르므로, teacher의 순위 신호만 물려받고 결정론 filter 축은 남길지 사전 등록해야 한다.

이 경로는 #160이 묻는 행동 로그 기반 supervised LTR의 답이 아니라 라벨 출처를 LLM으로 바꾼 별개 경로다. 따라서 #160의 `조건부` 판정을 바꾸지 않으며, 구현·transfer set 생성·비용 승인은 후속 이슈로 분리한다.

## 4. 판정: `조건부`

저장 구조와 baseline은 준비됐지만 학습 데이터와 누출 없는 feature snapshot은 준비되지 않았다. 특히 conversion label은 `purchase_complete` 상품 미귀속이 해결되기 전 성립하지 않는다. 따라서 production LTR은 보류하며, §6의 수치 조건과 저장 계약을 충족할 때 offline prototype만 `go`로 전환한다.

## 5. offline prototype 범위

조건 충족 뒤 첫 prototype은 다음으로 제한한다.

1. `list_id` 단위 time split과 7일 click/cart, 30일 purchase attribution window를 사전 등록한다. window 값은 첫 지연분포를 본 뒤 변경 가능하되, 평가 실행 전 고정한다.
2. 추천 시점 scoring 6성분·surface·listType·degrade flag를 snapshot한다. 실제 `position`은 Joachims et al. 2017의 rank-conditional examination propensity를 적용하는 IPS debiasing 전용이며 serving feature에서 제외한다.
3. true impression negative를 전량 사용하고 pointwise logistic과 작은 GBDT만 비교한다.
4. `evals/ablation` 규약대로 arm·seed·N·primary metric을 실행 전 고정한다. primary는 nDCG@10, bootstrap 95% CI가 0을 포함하면 `inconclusive`다.
5. baseline은 `pipeline` nDCG@10 0.782943과 `scoring` 0.616852다. Cremonesi et al. 2010이 다룬 top-N 평가 문제와 연결해 nDCG@10을 primary로 두되, dev 31건과 sealed holdout 승인 경계를 지키며 Filter Accuracy·HCV·Coverage·latency도 함께 본다.

기존 골든셋은 실제 impression label이 아니라 질의-정답 relevance이므로 LTR 학습 성능을 단독 증명하지 못한다. 행동 로그 time split을 primary 데이터셋으로 하고, 기존 dev는 추천 품질·hard constraint 회귀 검사로 병행한다.

## 6. 착수 조건 (재검토 트리거)

아래를 **모두** 충족해야 offline prototype을 시작한다.

1. 30일 연속 `recommendation_list` 10,000개 이상, 대응하는 `recommendation_list_item` 50,000행 이상, 실제 목록별 item 수와 `position` 연속성 검증 오류 0건.
2. 같은 기간 `list_id IS NOT NULL` 추천 유래 `product_view` 2,000건 이상, distinct 식별 사용자/세션 1,000개 이상, 클릭 positive가 있는 목록 1,000개 이상.
3. `jarvis-backend#62` 배포 후 상품 귀속 `purchase_complete` 500건 이상이며 I-6/I-7/I-14 주문 권위와 일 단위 건수 차이가 5% 이내다.
4. 추천 시점 feature snapshot 저장 스키마가 마련되고 scoring 6성분·feature schema version·model/policy version·degrade flag의 non-null coverage가 99% 이상이다.
5. FE가 추천 유래 `product_view`에 `listId`·`position`·`recommendationRequestId`를 보내는지 계약 테스트나 샘플 로그로 확인한다.
6. 신규 의존성이 필요한 arm은 사람 승인 후 lockfile·license·서빙 이미지 영향까지 검토한다.

10,000개 목록과 2,000 click은 이번에 인용한 문헌에서 확인한 수치가 아니라 최소 두 개의 시간 구간과 surface/listType slice에 positive를 나눌 수 있는지 확인하는 이 리포의 내부 screening threshold다. 조건 도달 시 실제 click/conversion 비율과 군집 상관을 이용해 power/CI 기반 표본량을 다시 사전 등록한다.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- 2026-08-03 pg-catalog/MariaDB 실측: 구현 팩킷 §1-A~§1-D(이 문서에서는 DB 재측정하지 않음).
- `docs/api-spec.md` v0.17.1 §4.2 I-21: 상관키·목록당 최대 9개·멱등 규약.
- `docs/api-spec.md` v0.17.4 §4.4 I-13: `purchase_complete` 상품 미귀속과 주문 권위.
- `CHANGELOG.md` #148 C-18: I-17 제자리 upsert로 과거 catalog feature snapshot 부재.
- `evals/scoring/`, `app/core/config.py`: 6성분과 기본 가중치·결정성.
- `evals/ablation/DECISION.md`, `baselines/20260803-dev-full-n5/`: 사전 등록 규약, 품질·지연 baseline.
- `evals/goldenset/`, `evals/metrics/`: dev 31·sealed holdout 12, 결정론 metrics.

### 검증하지 못한 것

- FE가 실제 노출 및 추천 유래 `product_view`에 세 상관 필드를 채우는지: FE 저장소가 없어 확인 불가.
- BE의 recommendation row가 “저장됨”과 실제 viewport 노출을 어떻게 구분하는지: BE/FE 저장소가 없어 확인 불가.
- `jarvis-backend#62` 배포 상태와 주문 데이터 조인용 상세 스키마: 이 worktree 밖이라 확인 불가.

## 참고 문헌

- Paolo Cremonesi, Yehuda Koren, Roberto Turrin, “Performance of recommender algorithms on top-N recommendation tasks”, RecSys 2010, pp. 39–46, DOI `10.1145/1864708.1864721`.
- Christopher J. C. Burges, “From RankNet to LambdaRank to LambdaMART: An Overview”, Microsoft Research Technical Report MSR-TR-2010-82, 2010.
- Thorsten Joachims, Adith Swaminathan, Tobias Schnabel, “Unbiased Learning-to-Rank with Biased Feedback”, WSDM 2017, pp. 781–789.
- Geoffrey E. Hinton, Oriol Vinyals, Jeff Dean, “Distilling the Knowledge in a Neural Network”, CoRR abs/1503.02531, 2015.
- Jiaxi Tang, Ke Wang, “Ranking Distillation: Learning Compact Ranking Models With High Performance for Recommender System”, KDD 2018, pp. 2289–2298.
- Mathilde Caron, Hugo Touvron, Ishan Misra, Hervé Jégou, Julien Mairal, Piotr Bojanowski, Armand Joulin, “Emerging Properties in Self-Supervised Vision Transformers”, ICCV 2021, pp. 9630–9640.
- Qizhe Xie, Minh-Thang Luong, Eduard H. Hovy, Quoc V. Le, “Self-training with Noisy Student improves ImageNet classification”, CVPR 2020, pp. 10684–10695.
- Feiran Huang, Yuanchen Bei, Zhenghang Yang, Junyi Jiang, Hao Chen, Qijie Shen, Senzhang Wang, Fakhri Karray, Philip S. Yu, “Large Language Model Simulator for Cold-Start Recommendation”, WSDM 2025 (arXiv:2402.09176).
- Jianling Wang, Haokai Lu, James Caverlee, Ed H. Chi, Minmin Chen, “Large Language Models as Data Augmenters for Cold-Start Item Recommendation”, arXiv:2402.11724, 2024.
- Balázs Hidasi, Alexandros Karatzoglou, Linas Baltrunas, Domonkos Tikk, “Session-based Recommendations with Recurrent Neural Networks”, ICLR 2016 (Poster) (arXiv:1511.06939).
