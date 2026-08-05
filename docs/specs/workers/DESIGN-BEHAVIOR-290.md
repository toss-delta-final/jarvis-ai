# behavior Worker 설계 — 상품 k-means 군집화 (이슈 #290)

> 근거 논문: Chen, Sain & Guo 2012 (RFM+k-means 레시피) · Moe 2003 (방문 의도 유형론 —
> 라벨 어휘). 구현: `analysis/segmentation.py` · 배선: `tools.get_behavior_events`.

## 1. 해결 문제

상품별 카운트 나열을 LLM 이 통해석하면 패턴을 놓치거나 환각한다. 상품을 행동
패턴 군집으로 묶고 규칙 라벨을 붙여, LLM 이 군집 단위로 해석·액션을 연결하게 한다.

## 2. 입력 데이터 (계약 매핑 — I-13 groupBy=product)

- `rows[]`: productView/addToCart/checkoutStart/purchaseComplete 카운트 + uniqueVisitors.
- 군집은 **절단 전 전체 rows** 로 계산한다(표시 상한 `seller_summary_max_products`
  와 무관 — 정보 소실 방지).
- [#196] purchaseComplete 상품 미귀속 가능 — purchase 파생 피처 0 은 '구매 전무'가
  아닐 수 있다(도구 상시 노트 + 프롬프트 교차 확인 규칙이 통제).

## 3. Feature 매핑 표 — 축 재정의

| 논문 변수 | 계약 필드 | 판정 |
|---|---|---|
| R (최근 구매일) | I-16 `members[].last_activity_at` | 🔶 프록시(활동≠구매) — churn 도구 출력에서 소비, basis 규약 |
| F (구매 빈도) | I-16 `members[].sessions_30d` | 🔶 프록시(방문≠구매) |
| M (구매 금액) | 없음 | ❌ 제외 → R-F 2축 (Phase B 에서 M 복원) |
| 군집 대상(고객) | 고객 원시 데이터 없음 → **상품 축 재정의** | 🔶 — 상품 행동 피처 5차원 |

상품 피처 벡터: `[log(1+view), cart/view, checkout/cart, purchase/checkout, visitors/view]`
— 카운트는 로그로 눌러(멱법칙) 비율 피처와 스케일 정합, 이후 z-score 표준화.

## 4. 알고리즘

1. k-means(k = `seller_behavior_kmeans_k_min`~`k_max`, 기본 2~5) — 실루엣 최대 k
   선택(동률은 작은 k). `random_state=42`·`n_init=10` 고정(결정론 §10-②).
2. 군집 중심(원 피처 단위)을 전체 평균과 비교하는 규칙 라벨링(순서가 계약):
   담기율↑·결제진입↓ → **카트이탈형** / 전 단계 ↑ → **전환직결형** /
   조회↑·담기율↓ → **구경형** / 나머지는 활동량으로 저활동형·혼합형.
   중복 라벨은 번호 구분(구경형(1)·(2)).
3. 폴백: 상품 수 < k_min×3 → 군집 생략(사유 표기) / 전 상품 피처 동일 → 분리 불능.

## 5. 판단 기준

k 범위·seed 는 config 주입(`seller_behavior_kmeans_k_min/max`·`seller_kmeans_random_state`).

## 6. 출력 → LLM

`행동 군집 2개(k-means, 실루엣 0.61): [카트이탈형] 6개(id: 200, …) — 중심: 담기율
41.2%·결제진입률 6.4%·구매완료율 28.6%; …`. LLM 규칙(프롬프트): 군집 단위 해석·
군집별 조치 힌트 연결, "군집 생략"은 판정 보류(패턴 부재 아님), 라벨 재배정 금지.

## 7. degrade·테스트

- 재현 테스트(`test_seller_analysis_segmentation.py`): 3패턴 합성 상품 18개 →
  k=3 정확 복원 + 소속 완전 일치 + 라벨 3종 정확 / 결정론 / 분모 0 / 분리 불능.

## 8. Phase B

고객별 {첫구매일, 마지막구매일, 구매횟수, 총구매액} 계약 확장 시 BG/NBD(P(alive)·
기대구매수)·Gamma-Gamma(CLV)·RFM 정식 도입 — 확장사항 중 가치 최대(handoff §8-1).
