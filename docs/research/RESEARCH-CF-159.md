# item-based CF 도입 판단 — [이슈 #159](https://github.com/toss-delta-final/jarvis-ai/issues/159) · 작성일 2026-08-03 · 상태(`no-go`)

## 요약

**판정: `no-go`.** 현재 Jarvis에는 item-based collaborative filtering을 만들 사용자×상품 협업 신호가 없다.

- `order_item`·`cart_item`·`wishlist`는 0행이고, `behavior_events` 1행은 실사용이 아닌 스모크 테스트 흔적이다(2026-08-03 MariaDB 실측).
- `review` 126,313행은 많아 보이지만 `member_id`가 전량 `NULL`이라 사용자 축이 없고 user×item 행렬은 희소한 것이 아니라 **구성 불가**다.
- 반면 콘텐츠 임베딩은 7,220개 상품 전량에 있고 HNSW로 이미 서빙한다. 지금 새로 만들 수 있는 item-item 유사도는 현행 콘텐츠 유사도와 중복된다.

관련 판단: LTR은 [RESEARCH-LTR-160.md](./RESEARCH-LTR-160.md), bandit은 [RESEARCH-BANDIT-161.md](./RESEARCH-BANDIT-161.md)를 본다. #159는 이들과 병렬 후보이나, 같은 이벤트 귀속 결손을 공유한다.

## 1. 이 리포의 실측 데이터 현황

| 신호 원천 | 규모 | 협업 신호로 현재 사용 가능 | 이 리포에서의 판단 |
|---|---:|:---:|---|
| `product` / `product_document` | 7,220 / 7,220 | ❌ | 상품은 충분하지만 사용자 상호작용이 아니다 |
| `product_document.embedding` | 7,220 non-null, `vector(1536)` | ❌ | 콘텐츠 유사도에는 사용 가능하며 전량 L2 정규화·HNSW `vector_ip_ops` 전제 |
| `review` | 126,313 | ❌ | `member_id` non-null 0, distinct `member_id` 0이라 동일 사용자 식별 불가 |
| `behavior_events` | 1 | ❌ | `recommendation_generated`, 익명, `itemCount=8`인 스모크 테스트 흔적 |
| `order_item` / `cart_item` / `wishlist` | 0 / 0 / 0 | ❌ | co-occurrence를 만들 행이 없음 |
| `recommendation_list` / `_item` | 1 / 8 | ❌ | 노출 구조는 있으나 실사용 표본이 없음 |

리뷰는 1,721개 상품에만 있고 5,499개(76.16%)는 리뷰가 없다. 리뷰가 있는 상품에서도 상위 172개(그 집합의 10%)가 전체 리뷰의 41.34%를 차지한다. 상품당 리뷰 수는 min 1, max 600, avg 73.40, sd 107.09이고, 버킷은 1–9건 751개, 10–29건 273개, 30–99건 273개, 100–299건 211개, 300–999건 213개, 1,000건 이상 0개다. 이 분포는 리뷰 수 기반 유사도나 인기도가 head 상품으로 쏠릴 위험을 보여주지만, 리뷰 작성자의 동일성을 복원하지 못하므로 CF 입력으로 바꾸지는 못한다.

> 리뷰 수는 시드 README 표기 126,957건과 달라 2026-08-03 MariaDB 실측 126,313건을 채택했다.

이벤트 스키마와 계약은 이미 있다. `behavior_events`에는 `member_id`·`guest_id`·`session_key`·`client_event_id`(UNIQUE)·`product_id`·`recommendation_request_id`·`list_id`·`surface`·`position`이 있고, 추천 목록과 순위도 `recommendation_list` 및 `recommendation_list_item`에 저장할 수 있다. 따라서 문제는 “이벤트 수집이 미합의”가 아니라 **기존 구조에 실사용 데이터가 0이고 핵심 필드의 FE 적재 여부가 확인되지 않았다**는 것이다.

### 지금 가능한 것

- 전량 콘텐츠 임베딩 코사인으로 유사 상품을 찾는 것.
- 리뷰 수를 `log1p(reviewCount)`로 압축한 현행 popularity 성분과 평점을 보조 신호로 쓰는 것.
- 임베딩 전량 보유를 이용해 리뷰 0인 5,499개 상품에도 cold-start 폴백을 제공하는 것.

### 데이터가 쌓이면 가능한 것

- 동일 `member_id`·`guest_id`·세션 안의 view/cart/checkout/purchase co-occurrence로 item 벡터를 구성하는 것.
- 시간 순서를 보존한 implicit feedback으로 콘텐츠와 협업 신호의 증분 효과를 비교하는 것.
- 사용자·세션별 holdout으로 leave-one-out 및 time-based split을 수행하는 것.

## 2. Jarvis에서의 item-item 유사도와 결합 방식

### 2.1 후보별 입력 현실성

| 후보 | 필요한 입력 | 현재 입력 확보 여부 | 장점 | 이 리포의 결론 |
|---|---|:---:|---|---|
| cosine(co-occurrence 벡터) | 식별 가능한 사용자/세션×상품 implicit 행렬 | ❌ | 구현·설명 단순, sparse 계산 용이 | 사용자 축이 없어 계산 불가 |
| conditional probability / lift | 방향성 있는 item pair count와 각 item 노출·행동 분모 | ❌ | “A 이후 B”와 인기 보정을 분리 | 분자·분모 모두 실사용 0 |
| Jaccard | item별 사용자/세션 집합 | ❌ | 집합 크기 차이를 정규화 | 식별 집합을 만들 수 없음 |
| 인기 보정 cosine/BM25류 정규화 | co-occurrence + item/user 빈도 | ❌ | head-item 지배 완화 | 원 co-occurrence가 없음 |
| ALS / implicit MF | 충분한 implicit user×item 행렬과 confidence | ❌ | 잠재 취향 축 학습 | 행렬 자체가 구성 불가, 복잡도만 증가 |
| 콘텐츠 임베딩 cosine(현행) | 상품별 임베딩 | ✅ | 7,220개 전량 cold-start, HNSW 서빙 | 이미 I-22에 존재; 새 CF가 아님 |

여기서 알고리즘의 일반적 성질은 일반 지식 기반이며 이 작업에서 외부 출처를 검증하지 못했다. 후보 판정의 주 근거는 위 Jarvis 실측 입력 유무다.

### 2.2 implicit feedback 설계와 현재 결손

데이터가 생기면 `product_view < add_to_cart < checkout_start < purchase_complete` 순으로 confidence를 높이고, 반복 view는 세션 내 cap을 두며 시간 감쇠를 적용하는 설계가 가능하다. 그러나 현재 `purchase_complete`는 FE가 `productId` 없이 `properties.orderId`만 보내 `product_id=NULL`이 되고 상품 조인에서 탈락한다(`docs/api-spec.md` v0.17.4 §4.4 I-13). 가장 강한 신호가 상품에 붙지 않으므로 이 상태에서 가중치를 정하면 “구매 없음”을 학습하는 잘못된 행렬이 된다. 근본 수정은 `jarvis-backend#62` 배포가 선행되어야 한다.

명시적 추천 클릭 타입도 없다. 클릭은 추천 유래 `product_view`에 `list_id`·`position`·`recommendation_request_id`가 실렸을 때만 복원 가능하다. FE 저장소가 이 worktree에 없어 실제 전송 여부는 확인하지 못했다.

또한 I-16 `preChurnSignals.zeroResultSearchSessions`는 E-1이 검색 결과 수를 적재하지 않아 상시 0이다(`docs/api-spec.md` v0.19.1). 이를 “검색 실패가 없었다”거나 CF의 negative signal로 해석해서는 안 되며, 검색 결과 수 계약이 생기기 전에는 학습 입력에서 제외한다.

### 2.3 popularity bias와 cold-start

리뷰 보유 상품 상위 10%가 리뷰 41.34%를 차지하므로 raw count를 similarity/confidence에 직접 쓰면 인기 상품이 거의 모든 이웃 목록에 침투할 수 있다. 현 scoring은 이미 후보 내 `log1p(reviewCount)`와 `rating/5`의 평균으로 popularity를 계산해 count를 로그 압축한다(`evals/scoring/components.py`). CF를 재검토할 때도 lift·Jaccard·빈도 정규화 같은 인기 보정 arm을 raw cosine과 함께 비교하고, tail 상품 coverage를 별도 지표로 둬야 한다.

반대로 cold-start는 이 리포의 강점이다. 리뷰 0 상품 5,499개(76.16%)에도 임베딩이 전량 존재하므로 협업 이력이 없는 item은 콘텐츠 cosine으로 즉시 폴백할 수 있다. 사용자/게스트 이력이 없을 때도 가짜 중립 프로필을 만들지 않고 기존 degrade 규약을 유지한다.

### 2.4 현 scoring과 결합

현 결정론 scorer는 semantic 0.55, profile match 0.15, popularity 0.15, recency 0.05, diversity bonus 0.10, 최근 90일 exact 구매 penalty 0.20을 쓴다(`evals/scoring/scorer.py`, `app/core/config.py`). CF를 붙이는 두 선택지는 다음과 같다.

| 결합 위치 | 방식 | 장점 | 위험·제약 |
|---|---|---|---|
| 점수 성분 | 정규화한 `cf_similarity`를 새 가중 항으로 추가 | 현 성분별 설명·ablation·가중치 0 롤백을 재사용 | semantic과 중복 신호일 수 있어 가중치 재배분 필요 |
| 후보 생성 | seed item의 CF 이웃을 후보 풀에 합침 | 콘텐츠 검색이 놓친 행동 연관 상품을 회수 가능 | 후보 출처 편향·중복과 latency가 늘고, 후단 평가가 복잡 |

첫 prototype은 점수 성분 arm이 더 좁고 되돌리기 쉽다. 어느 방식도 `hard_filter.py`의 가격·금지 카테고리·금지 상품·must-exclude 경계를 우회해서는 안 된다. 컷된 상품은 높은 CF 점수로 재진입할 수 없고, 동점은 `productId` 오름차순, 결손은 값 0 + degrade 기록이어야 한다.

## 3. 후보 비교표

| 선택 | 현행과 다른 정보 | offline 검증 가능성 | 서빙 비용 | 판단 |
|---|---|---|---|---|
| 현 콘텐츠 cosine 유지 | 없음(현행) | dev 골든셋 즉시 가능 | HNSW 랭킹 p50 39ms, I-22 종단 p50 45ms | 유지 baseline |
| 리뷰 기반 “CF” | 사용자 공동행동이 아님 | 인기도 효과만 보게 됨 | 낮음 | 명칭·인과 모두 부정확, 제외 |
| co-occurrence CF score arm | 행동 연관성 | 로그 축적 뒤 가능 | precompute 시 낮음 | 재검토 1순위 |
| CF 후보 생성 arm | 행동 기반 recall | 로그 축적 뒤 가능 | 후보 병합·조회 추가 | score arm 승리 뒤 검토 |
| ALS / implicit MF | 잠재 사용자·상품 요인 | 더 큰 행렬 필요 | 학습·서빙·의존성 증가 | 초기 prototype 비추천 |

## 4. 판정: `no-go`

지금 구현하면 실제 협업 신호가 아닌 리뷰 인기도나 기존 임베딩 cosine을 CF라고 다시 포장하게 된다. 상관키 축은 I-21의 `recommendationRequestId`와 `listId` 계약으로 해소됐지만, 실사용 이벤트·상품 귀속·FE 클릭 필드 확인이 남았다. 따라서 #159 구현은 `no-go`이고, 현재는 로깅 정합과 재검토 계측만 준비한다.

## 5. offline prototype 범위 (지금은 하지 않는 이유)

지금은 CF model·similarity table·서빙 코드를 만들지 않는다. 입력 0에서 생성한 synthetic co-occurrence는 성능 판정 근거가 될 수 없고, 콘텐츠 cosine arm과 다른 production 가치도 입증하지 못한다.

조건 충족 뒤 prototype은 다음으로 제한한다.

1. 사용자/세션별 이벤트를 추천 시점 기준으로 time split한다.
2. `view/cart/checkout/purchase` confidence를 사전 등록하고, raw cosine·인기 보정 cosine·lift/Jaccard를 비교한다.
3. `cf_similarity` score arm만 먼저 추가하고 가중치 0을 rollback으로 둔다.
4. dev 31건과 `evals/metrics`로 기존 질의 품질·Filter Accuracy·HCV 회귀를 확인한다. 다만 이 골든셋은 질의-정답 기반이라 CF 행동 연관성을 평가하지 못한다.
5. 별도 행동 평가에서는 leave-one-out과 time-based next-item 예측을 사용하고 nDCG/Recall뿐 아니라 head/tail Coverage를 본다.

## 6. 착수 조건 (재검토 트리거)

아래를 **모두** 충족할 때만 offline prototype을 재검토한다.

1. `jarvis-backend#62` 배포 후 30일 연속으로 상품 귀속 가능한 `purchase_complete`가 200건 이상이고, I-6/I-7/I-14 주문 권위와 일 단위 건수 차이가 5% 이내다.
2. 같은 30일에 `list_id IS NOT NULL`인 추천 유래 `product_view` 10,000건, `add_to_cart` 1,000건, `checkout_start` 300건 이상이 적재된다.
3. 식별 가능한 `member_id` 또는 동의된 guest/session 축이 distinct 500개 이상, 상호작용 상품이 distinct 500개 이상이며, 5회 이상 반복된 item pair가 1,000쌍 이상이다.
4. FE가 추천 유래 `product_view`에 `listId`·`position`·`recommendationRequestId`를 싣고 Spring이 동일 값으로 적재한다는 계약 테스트 또는 샘플 로그를 확보한다.
5. 학습 snapshot에서 hard filter 통과 전/후 경계를 보존하고 `productId` tiebreak·degrade 기록을 검증하는 테스트 계획을 승인한다.

위 수치는 외부 벤치마크가 아니라 “여러 item pair가 반복 관측되고 30일 time split을 만들 수 있는가”를 거르는 내부 screening threshold다. 첫 축적분으로 pair-frequency 분포와 bootstrap CI를 산출한 뒤 본 학습 최소량을 다시 사전 등록한다.

## 부록: 근거 출처 · 검증하지 못한 것

### 근거 출처

- 2026-08-03 pg-catalog/MariaDB 실측: 구현 팩킷 §1-A~§1-D(이 문서에서는 DB 재측정하지 않음).
- `docs/api-spec.md` v0.17.4 §4.4 I-13: `purchase_complete` 상품 미귀속과 주문 권위.
- `docs/api-spec.md` v0.17.1 §4.2 I-21: `recommendationRequestId`·`listId`·목록당 9개·멱등 규약.
- `evals/scoring/components.py`, `scorer.py`, `hard_filter.py`, `app/core/config.py`: 현 scoring·안전 경계.
- `evals/ablation/DECISION.md`, `baselines/20260803-dev-full-n5/`: scoring 3.781ms 및 품질 baseline.
- `CHANGELOG.md` #148: HNSW 전환 전 3,321ms, 전환 후 p50 39ms, 홈 종단 p50 45ms.
- `evals/goldenset/`, `evals/metrics/`: dev 31·sealed holdout 12와 결정론 metric runner.

### 검증하지 못한 것

- FE가 추천 유래 `product_view`에 `listId`·`position`·`recommendationRequestId`를 실제로 채우는지: FE 저장소가 없어 확인 불가.
- BE 수정 `jarvis-backend#62`의 배포 상태와 배포 후 이벤트-주문 정합: 이 worktree 밖이라 확인 불가.
- 외부 CF 문헌의 특정 표본량·성능 수치: 웹/문헌 검증을 수행하지 않았으며 판정 근거로 사용하지 않음.
