# jarvis-ai 계약 정본 — 판매자 파트

> **이 파일이 판매자 파트 최신 정본이다.**
> 판매자 관련 작업은 `docs/api-spec.md` · `docs/lessons.md` · `CHANGELOG.md` 보다
> **이 파일(과 `docs/lesson-seller.md` · `changelog-seller.md`)을 먼저 읽고** 시작한다.
> 원본 3종은 판매자 구간에 한해 **구버전 스냅샷**이며 **수정하지 않는다** — 판매자 변경은
> 전부 이 파일에 기록한다. 원본과 어긋나면 **이 파일이 이긴다**.
>
> 개정 이력: v0.31.2(2026-08-09 분리 신설) → v0.31.3(2026-08-10, #541 — §6.1
> `preview.categoryMajor`·`categorySubPath` 추가 전용 2키, §6.2 I-10 오류 코드 2종)
> → **v0.31.4**(2026-08-11, #620 — §6 update 카테고리 선차단 명문화, §6.2 성격의
> I-11 `INVALID_PRICE` 전용 예외·매핑 안 된 4xx `SpringRejected` 분리 추가, §7
> changes 로그 어휘 소비 항목 부분 착수로 갱신).
>
> 범위: S-4 판매자 챗(SSE) · I-9~I-12 상품 CRUD · I-29~I-31 주문·리뷰.
> 구매자 레인(I-1·I-2·I-18 등)은 이 파일의 대상이 아니다 — `docs/api-spec.md` 를 본다.

| 항목 | 값 |
|---|---|
| 판매자 계약 버전 | **v0.31.4-seller** (2026-08-11 — #620 update 카테고리 선차단·INVALID_PRICE·SpringRejected) |
| 대응 원본 스냅샷 | `docs/api-spec.md` v0.31.1 |
| 상태 | 🔶 `blocked:spring` — I-9/I-10/I-11 옵션별 재고는 BE PR B 미배포 |

---

## 1. §3.2 S-4 `draft` 이벤트 — `changes[].optionName` (#524)

`draft.changes[]` 에 **추가 전용 키** `optionName` 을 신설한다.
`preview`(create 전용)·`orderItemId`(ship 전용)와 같은 규약이며, **FE 는 모르는 키를 무시**하므로
기존 소비자가 깨지지 않는다.

| 필드 | 타입 | 계약 |
|---|---|---|
| `changes[].field` | string | camelCase 8종 — `name`·`price`·`originalPrice`·`description`·`category`·`imageUrl`·`status`·`stockQuantity` (불변) |
| `changes[].optionName` | string | **[v0.31.2-seller, #524]** 옵션별 재고 change 에만 실린다 — 그 외 change 에는 **키 자체가 없다**. 값은 **표시용 옵션명**(`"블랙/M"`)이고, 실행 대상 `optionId` 는 서버가 confirm 시점 I-9 로 해소한다(이름은 표시·의도, id 는 실행 — "계약값은 코드") |

- 옵션마다 `stockQuantity` change **1건**이다. 같은 옵션이 두 번 실리면 서버가 실행하지 않고 되묻는다.
- FE 는 이 키가 있으면 **재고 행 라벨에 옵션명을 함께 표시해야 한다.** 없으면
  "재고 5 → 10" 두 줄이 옵션 구분 없이 떠서 판매자가 무엇을 승인하는지 모른다.

> ⚠️ **BE 문서와의 어휘 충돌(미해결)** — `jarvis-back/docs/backend/05-llm-contract.md:141` 은
> draft `field` 어휘를 `stockQuantity` → **`stocks`** 로 바꿔 적었다. AI 구현과 FE 타입
> (`SellerDraftField`)은 `stockQuantity` 유지 + `optionName` 추가다. AI 방식이 FE 파손이 없어
> 낫지만 **노션 정본(CH-2/S-4) 확인이 필요하다.** 협의 항목이며 이 PR 범위 밖.

---

## 2. §4.5 I-9 자사 상품 목록 — `stocks[]` (#524)

`GET /internal/seller/{brandId}/products`

응답 `rows[]` 에 `stocks[{optionId, optionName, quantity}]` 를 **추가**한다.

- **품절 옵션(quantity 0)도 그대로 내려온다** — 판매자용이라 구매자 I-1 과 반대다.
- 옵션 없는 상품도 **같은 모양**이다: `[{ "optionId": null, "quantity": 100 }]` 한 줄.
  옵션 유무로 필드를 가르지 않는다(가르면 AI 가 매번 분기해야 한다 — BE 02 D33).
- `stockQuantity` 는 계속 내려오되 의미가 **"상품 재고" → "옵션 재고 합계(파생)"** 로 바뀐다.
- AI 는 **관대 수신**이다(없으면 빈 배열) — 그래서 이 필드만으로는 배포 순서에 물리지 않는다.

**🔑 빈 배열의 의미**: PR B 이후 I-9 는 옵션 없는 상품에도 `optionId: null` 한 줄을 **반드시**
내린다(BE 04 §I-9). 따라서 **빈 배열 = "옵션이 없다"가 아니라 "BE 가 아직 구버전"** 이다.
AI 는 이 판정을 `stocks` 모드 재고 쓰기의 전제 검사에 쓴다(§4 참조).

---

## 3. §4.5 I-10 / I-11 재고 쓰기 — 듀얼모드

BE 정본은 재고 입력을 `stockQuantity`(정수) → `stocks[{optionId, quantity}]` 로 바꾸지만
**BE 코드(PR B)는 아직 없다**(머지된 것은 docs + 마이그레이션 SQL 뿐 — jarvis-back `bba0f9e` 실측.
`Product.stockQuantity` 컬럼도 `ProductRepository` 의 `stock_quantity > 0` 쿼리 4곳도 그대로다).

그래서 AI 는 설정 `seller_stock_wire_mode` 로 **정확히 한 형식만** 보낸다.

| 모드 | I-10 (등록) | I-11 (수정) |
|---|---|---|
| `quantity` (기본) | `stockQuantity` 정수 | `stockQuantity` 정수 |
| `stocks` (PR B 이후) | `stocks: [{quantity: N}]` (등록 시점엔 옵션이 없어 optionId null 한 줄) | `stocks: [{optionId, quantity}, …]` **부분 수정** |

- **quantity 모드의 기존 와이어는 바이트 단위로 불변**이다(회귀 테스트가 고정).
- 전환은 **설정 1건**. 단 §4 의 전제 검사를 통과해야 실제로 나간다.
- **I-11 은 부분 수정**이다 — 배열에 실린 옵션만 갱신되고 **생략한 옵션은 그대로**다.
- 직렬화: 클라이언트가 `exclude_none` 으로 본문을 만들어 `optionId: null` 은 **키가 빠져**
  나간다. Jackson 은 키 누락을 null 로 바인딩하므로(record) 계약상 동등하다.
  BE 가 키 존재를 강제하게 되면 `StockEntry` 를 고친다.

### 이름 → `optionId` 해소

판매자는 옵션 id 를 모르고 이름으로 말한다. LLM 에게 id 를 맡기지 않는다 —
조회 목록의 **옵션명만** 옮겨 적게 하고(`DraftChange.option_name`),
`stock_options.resolve_stock_option` 이 confirm 시점 I-9 `stocks[]` 로 해소한다.

매칭은 **완전일치 > 세그먼트 일치 > 부분일치** 순서이며 **유일 매칭만** 채택한다.
"블랙" 이 "블랙/M"·"블랙/L" 둘 다에 걸리면 고르지 않고 **되묻는다** — 엉뚱한 옵션의 재고를
바꾸는 것보다 한 번 더 묻는 편이 싸다(구매자 `narrow_options` F-1 "블루 ⊄ 블루투스" 교훈).

---

## 4. `stocks` 모드 전제 검사 (#524, 이 PR 신설)

**`seller_stock_wire_mode="stocks"` 만으로는 보내지 않는다.** I-9 응답의 `stocks` 가
비어 있으면(= BE 구버전) **재고 쓰기를 실행하지 않고 안내**한다.

근거: 구 BE 에 `stocks` 를 보내면 Jackson 이 모르는 키를 버리는데, 같은 본문의 `price` 는
반영된다. 그러면 **재고만 안 바뀐 채 "반영했습니다"** 가 나간다 — #506 카테고리 사고
(`category` 키를 버려 등록이 실패하던 것)와 같은 유형이고, 이쪽은 실패가 아니라 **부분 성공**
이라 더 안 보인다.

- 적용 대상: **I-11 재고 change 가 있을 때만**. 재고를 안 건드리는 수정은 통과한다.
- **I-10(등록)에는 두지 않는다** — 조회하는 상품 자체가 없어 BE 버전을 확인할 길이 없고,
  확인할 필요도 없다: stocks 모드 create 는 `stockQuantity` 를 아예 안 실으므로 구 BE 가
  **422 `MISSING_FIELD` 로 시끄럽게 실패**한다(조용한 부분 성공이 나는 update 와 반대).

---

## 5. 오류 코드 — 422 `INVALID_STOCK`

BE 는 새 code 를 만들지 않고 기존 재고 오류에 조건을 접었다(BE 04 §9, 2026-08-09).

| 코드 | 상태 | 조건 |
|---|---|---|
| `INVALID_STOCK` | **422** | `stocks[].quantity` **음수** 또는 그 상품의 옵션이 아닌 **`optionId`** |

AI 는 두 조건을 **모두 선차단**한다 — 음수는 `hitl._parse_int` 의 `isdigit()`,
타 상품 옵션은 `resolve_stock_option` 의 유일 매칭이다.
그래서 정상 경로에서는 나지 않고, **confirm 시점 I-9 재조회와 PATCH 사이에 옵션이
삭제·변경된 레이스**만 여기로 온다. 전용 예외 `InvalidStock` 으로 매핑하며
`SpringUnavailableError` 하위가 **아니다** — 재시도해도 같은 결과라 catch-all 에 뭉개지면
판매자가 무한 재confirm 에 갇힌다(I-12 `ALREADY_DELETED` 와 같은 규약).

---

## 6. §4.5 I-10 카테고리 — `categoryId`(Long) 필수 (#506 후속)

`category`(자유 문자열) → **`categoryId`(Long, 소분류 leaf)**. BE `SellerProductCreateRequest`
는 `categoryId` 만 받고 leaf 인지까지 검증한다(`Category.isRoot()` → 400 `PRODUCT_CATEGORY_INVALID`).
구 구현이 보내던 `category` 키는 BE 가 조용히 버렸고, 남은 `categoryId` 누락으로 **등록이 항상
실패**했다 — 판매자에게는 "등록 중 오류"로만 보였다.

- AI 설정 `seller_category_write_mode` **폐기** — BE 에 이름·경로를 받는 필드가 없어 고를 여지가 없었다.
- create 초안에서 **카테고리 필수화**(`_CREATE_REQUIRED_FIELDS`) — 승인 버튼 전에 되묻는다.
- **I-11 에는 카테고리 필드가 없다** — `SellerProductUpdateRequest` 에 `category`·`categoryId` 가
  없어 보내도 무시된다. 카테고리는 등록 시에만 정한다.
- **[#620] update 초안은 category 변경을 통째로 선차단한다** — `validate_draft` 가
  `op=="update"` 에서 `category` 필드를 보면 카드를 보여주기 전에 되묻는다(2단 방어의
  1단). 예전엔 update 의 category change 가 검증 없이 통과해 `ProductUpdate(category=…)`
  로 실렸는데, `ProductUpdate` 스키마에도 이제 그 필드가 없어(BE DTO 와 대칭) 조용히
  드롭됐다 — "카드엔 카테고리 변경이 보이는데 confirm 해도 반영 안 됨" 상태를 막는다.

### 6.1 §3.2 `draft.preview{}` — 카테고리 2칸 표기 (#541)

`preview{}`(op=="create" 전용)에 **추가 전용 키 2개**를 신설한다. 11개 → **13개**이며
값이 없으면 빈 문자열이다(키는 빠지지 않는다 — 기존 규약 그대로).

| 필드 | 타입 | 계약 |
|---|---|---|
| `preview.categoryMajor` | string | **[#541]** 대분류 한 칸 — `"패션의류/잡화"` |
| `preview.categorySubPath` | string | **[#541]** 중·소분류 한 칸 — `"남성의류 > 셔츠/남방"` |

판매자 카테고리는 **대분류 / 중·소분류 두 칸**으로 정해진다(정본 DB 가 2단이고 소분류
`name` 이 "중분류 > 소분류" 병합형 — §6). FE 가 그 두 칸을 그리려면 지금은
`categoryPath` 를 `" > "` 로 쪼개야 하는데, **쪼개기가 FE 에서 성립하지 않는다**:
대분류 이름 자체에 슬래시가 들어가고(`"패션의류/잡화"`) 병합형 leaf 때문에 토막 수가
2개일 수도 3개일 수도 있다. 쪼개는 주체는 스냅샷을 쥔 서버다("계약값은 코드").

**불변식** — 세 키는 항상 다음을 만족한다(테스트로 고정):

```
categoryPath == categoryMajor + " > " + categorySubPath
```

`categorySubPath` 는 `path[1:]` 를 이어 붙인 값이지 `leaf`(=`path[-1]`)가 **아니다**.
낡은 3칸 스냅샷이 섞이면 leaf 기준은 중분류를 조용히 떨어뜨려 카드가 보여준 카테고리와
등록될 카테고리가 달라진다 — 카테고리는 등록 후 변경 불가라 그 오차의 비용이 크다.

- FE 는 **기존대로 `categoryPath` 만 써도 된다**(추가 전용, 모르는 키 무시 규약).
- 두 칸 UI 를 그릴 때만 새 키를 쓴다. 값을 다시 쪼개거나 이어 붙이지 않는다.
- 표시 계층 마스킹(`_masked_preview`) 대상에 두 키를 함께 넣었다 — 표시 키를 늘리면서
  마스킹 목록을 안 늘리면 그 키만 원문으로 나간다.

### 6.2 §4.5 I-10 오류 코드 — 전용 예외 2종 (#541)

| 코드 | 상태 | 조건 | AI 처리 |
|---|---|---|---|
| `PRODUCT_CATEGORY_INVALID` | **400** | 없는 `categoryId` 또는 **대분류** id(BE `Category.isRoot()` 거부) | `ProductCategoryInvalid` → 등록 중단, **카테고리를 다시 말해 새 초안** |
| `MISSING_FIELD` | **422** | 필수값(name·price·stockQuantity·categoryId) 누락 | `ProductFieldMissing` → 등록 중단, **담당자 확인 안내**(재시도 권하지 않음) |

둘 다 `SpringUnavailableError` 하위가 **아니다** — `INVALID_STOCK`(§5)·I-12
`ALREADY_DELETED` 와 같은 규약이다. 매핑 전에는 두 코드가 catch-all 로 낙성돼
`error{code:"INTERNAL", retryable:true}` 로 나갔고, 판매자에게는 등록이 계속 실패하는데
원인이 화면 어디에도 없었다(**#506 사고가 "등록 중 오류"로만 보였던 이유가 이것**).

- `PRODUCT_CATEGORY_INVALID` 는 **스냅샷이 정본 DB 보다 낡을 때만** 난다(AI 는 소분류만
  담긴 스냅샷에서 고른다). 그래서 안내가 "재시도"가 아니라 재초안이고, 서버 로그에
  거부된 스냅샷 id 를 남긴다 — 스냅샷 재생성 신호다.
- `MISSING_FIELD` 는 값 누락이 아니라 **와이어 형식 불일치**의 신호다. `validate_draft`
  가 필수 4종을 이미 강제하므로, 실제로 여기 오는 건 `seller_stock_wire_mode="stocks"`
  를 BE PR B 배포 전에 켠 경우다(§4 가 말한 "등록은 시끄럽게 실패한다"의 그 지점).
  판매자가 초안을 고쳐 풀 수 있는 문제가 아니라 설정·배포를 고쳐야 한다.

### 6.3 §4.5 I-10/I-11 `INVALID_PRICE` 전용 예외 + 매핑 안 된 4xx `SpringRejected` (#620)

BE `validatePriceRange`(price > originalPrice, 생략 필드는 저장된/등록되는 값 기준)가
거부하면 **422 `INVALID_PRICE`** — `InvalidPrice` 전용 예외로 매핑한다(`SpringUnavailableError`
하위 아님, §6.2 두 예외와 같은 규약). `validate_draft` 가 create 는 changes 값끼리,
update 는 `row`(선택 인자, 호출부가 price/originalPrice 를 건드릴 때만 I-9 재조회해
넘긴다)로 **카드 표시 전에** 같은 규칙을 선계산해 되묻는다 — 그래도 여기 오면 draft
표시와 confirm 사이의 레이스뿐이라 안내는 "재조회 후 새 초안"이다.

`_request` 의 공용 폴백도 갈라졌다: `error_code_map` 에 없는 응답은 상태코드로
`SpringRejected`(4xx, 영구 거부)/`SpringUnavailableError`(5xx·그 외, 일시 장애)를
가른다. `SpringRejected` 는 `SpringUnavailableError` 하위라 기존 `except
SpringUnavailableError` 호출부는 그대로 잡지만, `_confirm_stream` 은 `SpringRejected` 를
먼저 잡아 `retryable=false` 로 낸다 — 매핑 없는 4xx 를 5xx 와 뭉뚱그려 "재시도 가능"으로
안내하던 것(이 이슈의 핵심 증상)을 고친다.

---

## 7. 미해결 / 별도 이슈

| 항목 | 상태 |
|---|---|
| BE PR B(Java) 배포 시점 | 🔶 대기 — `seller_stock_wire_mode` 전환 신호 |
| draft `field` 어휘(`stockQuantity` vs `stocks`) | 🔴 BE 문서와 충돌 — 노션 정본 확인 필요 (§1) |
| `ProductRepository` 의 `stock_quantity > 0` 4곳 | 🔴 BE 미전환 — 옵션 하나만 품절인 상품이 검색·추천에서 사라진다 |
| I-11 응답 `changes:["PRICE","STOCK"]` 로그 어휘 소비 | 🔶 부분 착수(#620) — `ProductUpdateResult.changes` 필드는 추가돼 **빈 배열 여부**(실질 변경 없음 → `already_done`)만 본다. PRICE/STOCK 개별 항목을 반영 안내 문구에 반영하는 것은 여전히 미착수 |
| I-15 `product_change_logs.option_id` — 재고 로그가 옵션마다 1행 | 미착수 — 워커가 "재고를 세 번 바꿨다"로 오보할 수 있다 |
| `ProposedChange.option_name` 신설 | 미착수 — 추천이 옵션을 지목하지 못한다(§`apply` 는 현재 차단으로 대응) |
| **[#620 BE 계약 위반 의심 2건, jarvis-back 코드 미수정 — 보고만]** `SellerProductService.updateInternal` 이 빈 PATCH 본문(`request.isEmpty()`)을 **400 VALIDATION_ERROR** 로 거부 | 🔴 노션 I-11 정본은 "본문이 `{}` 여도 200 + `changes:[]`"라고 명시(§본 문서 3장 인접 서술 근거) — 실측 코드(`SellerProductService.java:214` 부근)와 어긋난다. AI 쪽은 빈 본문 PATCH 를 보내지 않아(항상 `changes` 1개 이상 강제) 실사용 영향은 없지만, 노션·코드 중 어느 쪽이 정본인지는 BE 팀 확인 필요 |
| **[#620 BE 계약 위반 의심]** I-9 목록 조회 파라미터 오류 시 BE 가 노션 명시 코드(`PRODUCT_INVALID_PARAM`)가 아니라 **`VALIDATION_ERROR`** 를 낸다 | 🔴 실측(`ErrorCode.java`) 기준 — AI 는 이 코드를 개별 매핑하지 않고 `SpringUnavailableError`(현재는 `SpringRejected`, 4xx)로 낙성해 실사용 영향은 제한적이지만, 노션 문서와 실제 코드가 다른 코드명을 쓰고 있어 별도 확인 필요 |
