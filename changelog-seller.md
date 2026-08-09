# Changelog — 판매자 파트

> **이 파일이 판매자 파트 최신 정본이다.**
> 판매자 관련 작업은 `CHANGELOG.md` · `docs/api-spec.md` · `docs/lessons.md` 보다
> **이 파일(과 `docs/api-spec-seller.md` · `docs/lesson-seller.md`)을 먼저 읽고** 시작한다.
> 원본 3종은 판매자 구간에 한해 구버전 스냅샷이며 **수정하지 않는다**.
> 원본과 어긋나면 **이 파일이 이긴다**.

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 를 따른다.

---

## [Unreleased]

### Added

- **#524 — 판매자 재고 옵션별 전환 선대응(듀얼모드)** (api-spec-seller §2·§3 — 🔶 `blocked:spring`).
  BE 정본은 I-10/I-11 재고를 `stockQuantity` 정수에서 `stocks[{optionId,quantity}]` 로 바꾸지만
  **BE 코드(PR B)는 아직 없다**(머지된 것은 docs + 마이그레이션 SQL 뿐 — jarvis-back `bba0f9e`
  실측. `Product.stockQuantity` 컬럼도 `ProductRepository` 의 `stock_quantity > 0` 쿼리 4곳도
  그대로다). 그래서 와이어 형식을 설정 `seller_stock_wire_mode` 로 갈랐다 — `quantity`(기본)는
  현행 BE 계약을 **바이트 단위로 유지**하고, PR B 배포 확인 후 `stocks` 로 전환한다.
  - I-9 `stocks[{optionId,optionName,quantity}]` 관대 수신(`SellerStockRow`) — 구 BE 응답은 빈
    배열이라 배포 순서에 물리지 않는다. `list_my_products` 가 옵션별 재고를 펼쳐 보여주고
    ("재고 합계 12건 (옵션별: 블랙/M 5건 · 블랙/L 품절)"), 이 표기가 에이전트가 옮겨 적는
    옵션명의 **원천**이다.
  - `DraftChange.option_name`(LLM — 조회 목록의 옵션명 그대로)·`option_id`(코드 전용, LLM 산물
    불신). SSE `draft.changes[]` 에 **추가 전용** `optionName` 키 — FE 는 모르는 키를 무시한다.
  - 이름→optionId 해소기 `stock_options.resolve_stock_option` — 완전일치 > 세그먼트 > 부분일치
    순서에 **유일 매칭만** 채택. "블랙"이 블랙/M·블랙/L 둘 다에 걸리면 고르지 않고 되묻는다
    (엉뚱한 옵션의 재고 변경 > 한 번 더 묻기 — 구매자 `narrow_options` F-1 교훈).
  - I-11 **부분 수정** — 배열에 실린 옵션만 갱신되고 생략한 옵션은 그대로다.

- **#524 — I-10/I-11 422 `INVALID_STOCK` 전용 예외 매핑** (api-spec-seller §5).
  종전엔 매핑이 없어 `SpringUnavailableError` 로 뭉개져 판매자에게 "일시적 오류"만 나갔다.
  음수·타 상품 옵션은 AI 가 선차단하므로 정상 경로에선 나지 않고, 여기 오는 건 **confirm 시점
  I-9 재조회와 PATCH 사이에 옵션이 바뀐 레이스**뿐이다. 그래서 안내가 "재시도"가 아니라
  **"재조회 후 새 초안"** 이다. `SpringUnavailableError` 하위가 아니라 catch-all 에 삼켜지지 않는다.

### Fixed

- **#524 — `stocks` 모드를 설정만 믿고 보내던 문제(조용한 부분 실패)** (api-spec-seller §4).
  BE PR B 배포 전에 `stocks` 로 켜면 `{price, stocks}` 를 보냈을 때 구 BE 가 `stocks` 키를
  버리고 price 만 반영해 **재고는 그대로인 채 "반영했습니다"** 가 나갔다. #506 카테고리 사고와
  같은 유형이고 이쪽은 실패가 아니라 부분 성공이라 더 안 보인다. I-9 응답의 `stocks` 가 비면
  (= BE 구버전 신호, PR B 이후엔 옵션 없는 상품도 `optionId: null` 한 줄을 받는다) 재고 쓰기를
  실행하지 않고 안내한다. 등록(I-10)에는 두지 않는다 — 그쪽은 422 `MISSING_FIELD` 로 시끄럽게
  실패해 조용한 사고가 아니다.

- **#524 — 재고 변동 안내가 옵션을 밝히지 않고, 여러 옵션이 변해도 하나만 남던 문제.**
  비교는 옵션 단위로 고쳤는데 **문구는 그대로**였고, 루프가 `stock_note` 를 대입해 옵션 2건이
  동시에 변동해도 **마지막 하나만** 안내에 남았다. 이제 변동 건을 누적하고 옵션명을 함께 싣는다
  ("재고가 블랙/M 3건 · 블랙/L 1건으로 변동되어…"). 옵션 없는 상품의 문구는 **글자 그대로 종전과
  동일**하다(회귀 고정).

- **#524 — 옵션별 재고 상품의 추천 적용이 승인 뒤에 되묻던 문제.**
  `ProposedChange` 에 `option_name` 이 없어(추천 스키마는 옵션 개념보다 먼저 확정됐다) 추천→draft
  변환에서 옵션 정보가 유실됐다. 그대로 두면 카드의 `before` 가 **옵션 합계**로 뜨고(추천이 의도한
  단일 재고와 층위가 다르다) 판매자가 **[적용]을 누른 뒤에야** 되묻게 된다. HITL 은 승인 전에
  거르는 장치이므로, `apply_recommendation` 이 이미 쥐고 있는 I-9 행으로 **초안 생성 전에** 막고
  대화로 되돌린다. quantity 모드에서는 판정 자체를 하지 않는다(기존 동작 불변).

### Docs

- **판매자 파트 정본 3종 분리** — `docs/api-spec-seller.md` · `docs/lesson-seller.md` ·
  `changelog-seller.md` 신설. 판매자 관련 작업은 원본 3종(`docs/api-spec.md` · `docs/lessons.md` ·
  `CHANGELOG.md`)보다 **이 3종을 먼저 읽고** 시작하며, 원본 3종은 판매자 구간에 한해 구버전
  스냅샷으로 두고 **수정하지 않는다**.

- **FE 계약 문서 드리프트 5곳 정정** (`docs/specs/FE-CONTRACT-SELLER-CHAT.md`).
  draft 예시에 옵션별 재고 케이스 추가 · 필드표에 `changes[].optionName` 등재 ·
  `create` 필수값에 `category` 반영(#506 후속, 코드는 이미 바뀌어 있었다) ·
  `stale` 재고 비교 서술을 옵션 단위로 · 되묻기 문구표에 카테고리·옵션 관련 4행 추가.

- **주석 정정** — `charts.py` y축 `stock` 이 옵션 합계여도 동작이 같은 근거,
  `preview.py` 가 create 전용이라 옵션 대상이 아니라는 근거(+ field 키 dedupe 잠재 결함),
  `spring.py` `PurchaseState` · `spring_client.py` `CartStockInsufficient` 의
  "재고는 상품 단위, 옵션별 재고 없음" 서술이 BE 02 D33 이후 사실이 아님을 명시(동작 무변경,
  구매자 레인 정합은 #508 소관).

### 별도 이슈로 분리 (이번 PR 범위 밖)

- 죽은 쓰기 도구 정리 — `tools.create_product` 가 개명된 `ProductCreate.category` 를 그대로 호출
  (pydantic `extra='ignore'` 로 조용히 버려짐). `PRODUCT_TOOLS` 는 어느 에이전트에도 바인딩되지
  않아 지금은 안 터지지만, 재바인딩 시 듀얼모드·옵션 해소·HITL 을 우회하는 쓰기 경로가 열린다.
- `DraftChange.option_id` 데드 필드 제거 — 읽는 곳이 0곳(LLM 스키마 표면 변경이라 프롬프트 회귀 필요).
- `stock_lines_text` 옵션 나열 상한 — 절단하면 그 옵션명이 초안에 못 쓰이는 트레이드오프.
- `outcome: "stale"` 어휘 — 옵션 되묻기가 stale 로 나가 FE 배지가 오독된다(FE 협의 필요).
- `ProposedChange.option_name` 신설 — 추천이 옵션을 지목하게 하려면 분석 워커 프롬프트까지 파급.
