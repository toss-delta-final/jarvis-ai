# HANDOFF — REES46 기반 테스트용 더미데이터 생성 (#128)

> 이전 Cowork 세션 인수인계 문서. 새 세션은 이 문서만 읽으면 이어서 작업 가능.
> 작성일: 2026-08-04

## 1. 목표

Kaggle의 실제 쇼핑몰 사용자 행동 데이터(REES46)에서 비율·분포 통계를 추출하고,
그 통계를 따르는 **테스트용 더미데이터(최소 풀세트)** 를 생성해 로컬 DB에 적재한다.

- 용도: **로컬 테스트** (운영/데모용 아님 — 과한 정교화 금지)
- 풀세트 범위: `member` / `guest` / `behavior_events` / `orders` / `order_item`
  (+ `order_status_logs`, `cart_item` — 정합성 유지 최소한)
- 추천 관련 이벤트 4종 + `recommendation_generated`는 **2차 범위로 보류**

## 2. 작업 환경

- 작업 폴더: `C:\Users\vssea\jarvis-worktrees\128-dummy-data-stats` (git worktree)
- 브랜치: `feat/128-dummy-data-stats` (⚠️ 베이스가 PR #284 브랜치 커밋 `322a32c` —
  dev 기준 재분기 여부는 사용자 판단, 커밋 전이면 `git switch -C ... origin/dev` 가능)
- 산출물 폴더: `data-analysis\`
  - `extract_stats.py` — multi-category 메인 통계 추출 (실행 완료)
  - `extract_aux_stats.py` — `--mode cosmetics` / `--mode orders` (실행 완료)
  - `stats_full.json` / `stats_jarvis.json` / `REPORT.md` — 2019-Oct multi-category 결과
  - `stats_cosmetics.json` — 2019-Dec cosmetics 결과
  - `stats_orders.json` — kz.csv(electronics 주문 이력) 결과
- 원본 CSV: 사용자 PC `Downloads\` (2019-Oct.csv, cosmetics zip, kz.csv.zip)
- 실행 방식: 사용자 PC에서 `uv run --with duckdb python <script> ...` (Kaggle 토큰 공유 안 함)

## 3. 데이터 소스와 역할 분담 (3개 통계 → 1개 더미 풀세트)

| 소스 | 파일 | 더미에서 담당 |
|---|---|---|
| [multi-category store](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store) 2019-Oct (42.4M행) | stats_jarvis.json | **메인 뼈대**: 세션 구조·퍼널·사용자 구조·시간 패턴·지프 분포 |
| [cosmetics shop](https://www.kaggle.com/datasets/mkechinov/ecommerce-events-history-in-cosmetics-shop) 2019-Dec (3.5M행) | stats_cosmetics.json | **remove_from_cart 행동** (메인 데이터에 remove가 0건이라 보완) |
| [electronics purchase history](https://www.kaggle.com/datasets/mkechinov/ecommerce-purchase-history-from-electronics-store) kz.csv (2.6M라인) | stats_orders.json | **주문 구성**: 다품목·동시구매·수량·재구매 간격 |
| API 명세 E-1 (노션) | — | search·page_view·checkout_start·session_start·login **합성 규칙** |

## 4. 핵심 실측 수치 (더미 생성 파라미터)

### 메인 (stats_jarvis.json — multi-category, 봇 세션 112개 제외)

- 이벤트 비율 view : cart : purchase = **96.1% : 2.2% : 1.7%**
- 세션 퍼널: p(cart|view 세션) **6.2%**, p(purchase|cart 세션) **50.9%**,
  구매 세션 중 **53.6%는 cart 없이 직접구매** (직접구매 경로 필수)
- 세션: 이벤트 중앙값 2·평균 4.6, 바운스 35.4%, 길이 중앙값 63초(구매 세션 248초·평균 7.5이벤트)
- 사용자(월간): 세션 중앙값 2·평균 3.1, 1회 방문 48.8%, **구매 전환 11.5%**, 구매자 중 재구매 32.2%
- 카테고리 집중도: 사용자별 top1 카테고리 점유율 **평균 79%** (개인화 신호 뚜렷하게 생성 가능)
- 상품 인기: 지프 α=**0.883**, 상위 1%가 조회 49%·상위 10%가 82.6%
- 시간: UTC 저녁 피크 — **하루 모양만 가져와 KST로 재배치**
- 가격: 고가일수록 전환율 높음(전자몰 특성) — 가격분위별 p/v 1.3%→2.0%

### remove (stats_cosmetics.json)

- remove/cart = 0.685 (⚠️ 화장품몰 특성으로 과함 — **양은 설정값(기본 0.2~0.3 권장), 구조만 이식**)
- remove 세션의 구매율 20.6% vs remove 없는 cart 세션 7.7% — **remove는 이탈이 아니라 구매 전 정리 행동** → 구매 세션에 편중 배치
- cart→remove 중앙값 425초, remove 후 재담기 4.4%, remove의 62%는 같은 세션에 선행 cart 없음(**세션 넘는 cart_item 지속** 재현 필요)

### 주문 (stats_orders.json)

- 라인/주문 중앙값 1·평균 1.84, 다품목 39.2%, 다카테고리 31.8%
- **수량은 사실상 항상 1** (>1 비율 0.02%) → quantity=1 기본
- 재구매 간격 중앙값 5일·p75 27일 (몰아서 구매)
- 실측 동시구매 쌍: 냉장고+세탁기, 노트북+마우스, 후드+오븐, 헤드폰+스마트폰, 식탁+거실장
- ⚠️ 데이터 오염 인지: ① 1970-01-01 쓰레기 행 → 재구매 간격 평균·p99.9 오염(중앙값·p75는 신뢰),
  ② 동시구매 쌍의 `"16.18"` 같은 숫자 카테고리는 컬럼 밀림 아티팩트 → **생성기에서 필터** (재실행 필터 패치는 미결)

## 5. 타깃 스키마·이벤트 명세 (확인 완료)

- **DB**: MariaDB, `schema 1.sql` (사용자가 채팅에 업로드했음 — 새 세션에서 다시 받아야 함).
  핵심: `behavior_events`(member_id/guest_id 서버 주입, session_key 30분, client_event_id UUID UNIQUE,
  occurred_at(FE)/created_at(수신) 분리, properties JSON), `orders`(**회원 전용** — 게스트 주문 없음),
  `order_item`(스냅샷 컬럼), `cart_item`(member/guest XOR), `guest`(2시간 sliding, converted_member_id)
- **이벤트**: E-1 `/api/events` — **FE 12종** + 서버 1종(`recommendation_generated`).
  12종: session_start / page_view / search / product_view / add_to_cart / checkout_start /
  purchase_complete / login / recommendation_impression / product_visible / product_click / recommendation_dismiss
  - `remove_from_cart`는 **아직 화이트리스트에 없음** — jarvis-ai가 추가 예정(13번째)
  - properties 필수 키: search{query,resultsCount}, product_view{price}, add_to_cart{quantity,price},
    checkout_start{amount,productIds[]}, purchase_complete{orderId,amount}, page_view{pageType 14종}
  - checkout_start는 실측에 없음 → p(checkout|cart)×p(purchase|checkout)=**실측 50.9%** 가 되도록 분해(설정값)
- **카테고리**: `10_category.sql`(사용자 업로드, 1,221행) — DB상 **2단**: 대분류=대+중 합친 텍스트, 소분류=leaf.
  노션 API 명세서: https://app.notion.com/p/7015ca79037b826f8b52813815cfa53c (**읽기 전용 — 수정·등록·삭제 절대 금지**)

## 6. 확정된 설계 결정

1. 실측 3종(view/cart/purchase)을 **앵커로 고정**, search·checkout_start 등은 앵커와 모순 없게 보간·합성
2. 카테고리 매핑은 **하지 않음** (테스트용이라 축소 확정) — 10_category.sql은 유효 category_id 공급원일 뿐.
   카테고리 가중치는 지프 분포 자동 배정, 퍼널은 전체 평균 ± 가격분위 보정. mapping.json 산출물 없음
3. remove_from_cart: 구조는 cosmetics 실측, 양(remove/cart)은 설정값
4. 구매는 회원만(D30) → 게스트 구매 세션에는 login 이벤트 + guest.converted_member_id 전환 반영
5. 출력 포맷: 범용 CSV + Spring(MariaDB)용 INSERT SQL **둘 다**
6. 검증: 생성 더미에서 통계 역산 → 실측 대비 리포트

## 7. 미결 사항 (새 세션에서 먼저 확인)

- **상품 시드**: 더미가 참조할 product_id — 상품 시드 SQL(20_product.sql 류)이 있는지 사용자에게 확인.
  있으면 실제 product/brand/category FK 사용, 없으면 더미 상품도 생성
- **규모 기본값**: 당초 "중형(5천 유저)"이었으나 로컬 테스트 목적이면 소형(~500유저) 권장 — 사용자 답 대기
- kz.csv 오염 필터(날짜≥2018, category_code 형식 검증) 재실행 여부 — 생성기에서 걸러도 무방
- 파일 업로드 필요: `schema 1.sql`, `10_category.sql` (이전 세션 채팅에 첨부됐던 것 — 새 세션에 다시 첨부)

## 8. 다음 단계

1. 미결 사항(§7) 확인
2. 더미 생성기 상세 설계 승인받기 — 테이블별 생성 순서·정합성 규칙
   (member/guest 풀 → 사용자별 세션 → 세션 내 이벤트 시퀀스(마르코프) → 주문 승격 → SQL/CSV 출력)
3. 생성기 구현 (`data-analysis\generate_dummy.py` 예정) — 시드 고정으로 재현 가능하게
4. 역검증 리포트 → 로컬 DB 적재 테스트

## 9. 작업 관례 (이 프로젝트에서 지켜온 것)

- 설계 먼저 설명하고 승인 후 구현 ("코드 수정하지 마"가 기본값 — 명시 승인 후 작성)
- 스크립트는 사용자 PC에서 실행 (Kaggle 토큰 공유 안 함), 결과 JSON만 세션이 읽음
- 대용량 처리는 DuckDB 스트리밍 (`--memory-limit`, `--temp-dir` 옵션 지원, C: 용량 부족 이슈 이력 있음)
- 노션은 읽기만. 커밋·푸시는 사용자가 직접
