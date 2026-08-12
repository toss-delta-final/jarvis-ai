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

- **#600 — chart 레인 해석 에이전트** (와이어 변경 없음 — `report.text` 내용만 바뀐다).
  `chart_only` 턴의 고정 문구 3종("요청하신 그래프를 준비했습니다…" 등)을 좌표 기반
  해석문으로 대체한다. 좌표를 세거나 나누는 건 여전히 코드다 — `charts.chart_facts`가
  합계·평균·최고/최저·처음→끝·상위3·y=0 개수를 `aggregate`(sum/avg/none) 어휘를 따라
  미리 계산하고, LLM은 그 값만 인용한다. 검증은 신규 검사를 최소로 둔다: 좌표·
  chart_facts를 합성 finding에 실어 기존 D1~D3(`verifier.run_deterministic_checks`)에
  그대로 태우고, C1(`check_cause_hedged`)·V2-d(기간 인용)도 무접촉 재사용한다. 신설은
  C4(`chart_verify.check_chart_claims_bounded` — 스냅샷 추세·하루 단위 서술·하위 단정·
  "전체 행동" 서술 4종 차단)와 차트 전용 인과 L0 보강(완화어가 있어도 인과 단정 어휘를
  전면 차단 — C1 재사용만으로는 완화어가 있으면 근거 없이도 통과하는 사각이 있어 보강)
  뿐이다. 재작성은 최대 1회(judge 없음, 대화형 90s 예산 안). 실패/타임아웃/
  `seller_chart_interpret_enabled=false`는 전부 기존 고정 문구로 폴백 — 차트는 해석
  실패로 죽지 않는다. `verifier.py`·`schemas.py`·`api/seller.py`·FE 전부 무접촉.
  신설 Settings 5종(`seller_chart_interpret_enabled`·`_timeout_s`·`_max_retries`·
  `_max_chars`·`_forbidden_terms`) + `seller_chart_agent_timeout_s`(graph 축 선언 전용
  타임아웃 분리 — 기존엔 `seller_worker_timeout_s`(60s)를 재사용해 §6.1 예산 초과의
  절반을 차지했다).

- **#541 — `draft.preview{}` 카테고리 2칸 표기** (api-spec-seller §6.1, v0.31.3-seller).
  `preview.categoryMajor`("패션의류/잡화")·`categorySubPath`("남성의류 > 셔츠/남방")
  **추가 전용** 2키(11 → 13). 판매자 카테고리는 대분류 / 중·소분류 **두 칸**으로 정해지는데
  (정본 DB 가 2단이고 소분류 `name` 이 병합형 — §6) 그 두 칸을 FE 가 `categoryPath` 를
  `" > "` 로 쪼개서 만들 수 없다: 대분류 이름에 슬래시가 들어가고(`패션의류/잡화`) 토막
  수가 2개일 수도 3개일 수도 있다. 쪼개기는 스냅샷을 쥔 서버가 한다("계약값은 코드").
  불변식 `categoryPath == categoryMajor + " > " + categorySubPath` 를 테스트로 고정했고,
  둘째 칸은 `leaf`(=`path[-1]`)가 아니라 `path[1:]` 다 — leaf 기준이면 3칸 스냅샷에서
  중분류가 조용히 빠져 카드와 실제 등록 값이 달라진다. 표시 계층 마스킹
  (`_masked_preview`) 목록에도 두 키를 함께 넣었다. FE 는 기존대로 `categoryPath` 만
  써도 되며, 두 칸 UI 를 그릴 때만 새 키를 쓴다.

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

- **product 레인 대화 맥락 배선 — 상품 대상 되묻기에 답해도 다음 턴이 기억하지
  못하던 문제.** `_product_stream`/`_product_agent_input` 는 `general`/`analysis`
  레인과 달리 `recent_turns`(스레드 최근 대화)를 받지 않았다 — `product_agent` 는
  checkpointer 없이 매 턴 새로 호출되므로, "어느 상품을 말씀하시는 건가요?"라고
  되물은 다음 턴에서 판매자의 답변 한 줄만 보고 다시 헤매는 구조였다. supervisor
  라우팅(③)에서 이미 로드해 둔 `recent_turns` 를 `_product_stream` 에도 넘기고,
  `[최근 대화]` 블록으로 에이전트 입력에 주입한다. `PRODUCT_PROMPT` 에도 (a)
  `list_my_products` 의 `q` 파라미터를 먼저 시도하는 검색 전략과 (b) `[최근 대화]`
  블록을 되묻기 답변 해석에 우선 활용하라는 규칙을 추가했다. 등록 초안 대기 중
  사진 계속/수정 경로(pending)는 이미 다른 방식으로 맥락을 나르므로 영향 없음.

- **#620 — 매핑 안 된 4xx 가 5xx 와 뭉뚱그려 "일시적 오류(재시도 가능)"로 나가던 문제**
  (api-spec-seller §6.3). `_request` 의 공용 폴백이 `error_code_map` 에 없는 응답을
  전부 `SpringUnavailableError` 로 냈다 — 그러면 진짜 일시 장애(5xx·타임아웃)와 서버가
  영구 거부한 4xx 가 같은 예외·같은 안내로 섞인다. 4xx 는 `SpringRejected`
  (`SpringUnavailableError` 하위, catch-all 호환)로 분리하고, `_confirm_stream` 이 이를
  먼저 잡아 `retryable=false` 로 낸다.
- **#620 — I-11 `INVALID_PRICE` 미매핑 + update 초안이 price≤originalPrice 를 사후에만
  알던 문제** (api-spec-seller §6.3). `update_product`/`create_product` 의
  `error_code_map` 에 `INVALID_PRICE` → `InvalidPrice` 전용 예외를 추가했고,
  `validate_draft` 가 (a) create 는 changes 값끼리, (b) update 는 `row`(선택 인자 —
  호출부가 price/originalPrice 를 건드릴 때만 I-9 재조회해 넘긴다)로 BE
  `validatePriceRange` 와 같은 규칙을 **카드 표시 전에** 선계산해 되묻는다.
- **#620 — update 초안의 category 변경이 카드엔 보이는데 조용히 드롭되던 문제**
  (api-spec-seller §6). `ProductUpdate` 스키마에서 `category` 필드를 제거(BE DTO 와
  대칭 — I-11 에는 애초에 그 필드가 없다)하고, `validate_draft` 가 `op=="update"` 에서
  `category` change 를 보면 카드를 보여주기 전에 되묻는다.
- **#620 — delete 초안에 status 외 잡음 필드가 섞이면 카드에 그대로 노출되던 문제.**
  `validate_draft` 가 delete 초안의 changes 를 `status` 필드 하나로만 정규화한다(없으면
  ship 과 동일하게 빈 목록) — 실행(I-12)은 애초에 changes 를 안 본다("보여준 것==
  실행하는 것").
- **#620 — 같은 필드가 초안에 중복으로 실려도 나중 값이 조용히 이기던 문제.**
  `validate_draft` 가 `(field, option_name)` 중복을 선차단하고 다시 말해 달라고 되묻는다.
- **#620 — 상품명 200자 초과가 BE 400 `VALIDATION_ERROR`(미매핑 → "일시적 오류")로만
  보이던 문제.** BE `@Size(max=200)`(create/update DTO 공통)과 동일한
  `seller_name_max_len` 설정을 추가하고 `validate_draft` 가 카드 표시 전에 되묻는다.
- **#620 — I-11 응답이 실질 변경 없이 `changes:[]` 로 와도 "변경을 반영했습니다"로
  보이던 문제.** `ProductUpdateResult` 에 `changes` 필드를 추가하고, 비어 있으면
  `already_done`("이미 요청하신 값으로 되어 있어…")으로 갈음한다 — `_confirm_stream` 의
  패널 분기(`status=="executed"` 만 refresh)가 자연히 keep 이 된다.
- **#620 — 바인딩되지 않는 죽은 쓰기 도구 4종 제거.** `tools.py` 의
  `create_product`/`update_product`/`delete_product`/`update_order_status` @tool 은
  어느 에이전트에도 바인딩된 적이 없다(실행은 `hitl._execute_draft` 가 코드로 담당,
  HITL 모듈 결정 1) — `workers.py` 의 관련 죽은 주석(`PRODUCT_TOOLS`/`ORDER_WRITE_TOOLS`)도
  함께 정리했다. `SpringClient.create_product`/`update_product`/`delete_product`
  (HTTP 호출 본체, spring_client.py)는 이름만 같을 뿐 별개이며 그대로 유지된다.

- **#541 — I-10 카테고리·필수값 거부가 "일시적인 오류(재시도 가능)" 로 뭉개지던 문제**
  (api-spec-seller §6.2). `create_product` 에만 `error_code_map` 이 없어(update 는
  `PRODUCT_DELETED`, delete 는 `ALREADY_DELETED` 를 이미 매핑) 400
  `PRODUCT_CATEGORY_INVALID` 와 422 `MISSING_FIELD` 가 `SpringUnavailableError` 로
  낙성됐다. 그래서 **#506 의 카테고리 사고가 판매자에게 "등록 중 오류"로만 보였다** —
  매번 실패하는 상태인데 재시도를 권하는 안내가 나갔다. 전용 예외
  `ProductCategoryInvalid`·`ProductFieldMissing` 로 분리한다(둘 다
  `SpringUnavailableError` 하위 아님 — catch-all 회피).
  - 카테고리 거부는 **스냅샷이 정본 DB 보다 낡을 때만** 난다(AI 는 소분류만 담긴
    스냅샷에서 고른다). 안내는 재시도가 아니라 "카테고리를 다시 말해 새 초안"이고,
    서버 로그에 거부된 스냅샷 id 를 남긴다(스냅샷 재생성 신호).
  - 필수값 누락은 값이 빠진 게 아니라 **와이어 형식 불일치**의 신호다(`validate_draft`
    가 4종을 이미 강제한다) — 대표 경로가 `seller_stock_wire_mode="stocks"` 를 BE PR B
    배포 전에 켠 경우다(§4 의 "등록은 시끄럽게 실패한다"가 실제로 시끄러워지는 지점).
    판매자가 초안을 고쳐 풀 문제가 아니라 담당자 확인이 필요해, 다른 stale 분기와 달리
    "다시 요청해 주세요"(`_STALE_RETRY_GUIDE`)를 붙이지 않는다.

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
