# DESIGN-SELLER-TOOLS-STAGE1 — 판매자 멀티에이전트 1단계(Tool 계층) 설계 메모

> **버전**: v0.1.0 · **상태**: 설계(구현 착수 전) · **작업 모델**: opus 설계 세션 산출물
> **상위 문서**: [SPEC-SELLER-001](SPEC-SELLER-001.md) §4·§5 · [IMPL-PLAN-SELLER-001](IMPL-PLAN-SELLER-001.md) §1 "1단계" · [api-spec](../api-spec.md) §2.3/§2.6/§2.9/§3.2/§4.4/§4.5
> **범위**: 1단계(LLM 없이 완결되는 최하층)만 — `spring_client` 판매자 함수군 · `app/agents/seller/tools.py`(@tool 래퍼) · `app/agents/seller/calc.py`(순수 계산 함수) · `app/core/config.py`(임계값 Settings). **서브에이전트/create_agent/LLM 호출은 2단계 이후 — 본 메모 범위 밖.**
> **어긋나면 api-spec 우선** (lessons 2026-07-17 — 설계 문서 드리프트 재발 방지).

---

## 0. api-spec 버전 대조 (착수 전 최우선, lessons 재발 방지)

| 항목 | 확인값 |
|---|---|
| api-spec 사본 버전 헤더 | **v0.14.0** (동기화 2026-07-16, 정본 = 기획 repo) |
| 판매자 챗 §3.2 | v0.11.0 확정: **모든 쓰기 HITL** + AI가 I-10/11/12 직접 호출. 구 "FE S-3 PATCH 반영" **폐기**(v0.13.0: S-3 = `GET /api/seller/products`, FE 대시보드용, I-9와 별개). 이벤트 `token/draft/done/error` 4종 |
| 판매자 집계 §4.4 | v0.8.0: `{brandId}` path 콜백. 전부 `internal`·`X-Internal-Token`·3s |
| 상품 CRUD §4.5 | v0.9.0: I-9 GET 목록 / I-10 POST / I-11 PATCH(재고 포함) / I-12 DELETE(soft, HIDDEN) |
| 인증 레인 §2.3 | **v0.13.0: AI→Spring 전 구간 `X-Internal-Token` 서비스 토큰 + 본문/쿼리 신원**(JWT `sub`/`brandId` 클레임 유래). 구 "JWT 포워딩" 제안 폐기 |
| 신원 §2.6 | `sellerId`=JWT `sub`(role=seller) · `brandId`=JWT `brandId` 클레임. **요청 본문에서 절대 받지 않음**(IDOR) |
| 타임아웃 §2.9 c | AI→Spring 전 구간 **3s**, 초과 시 각 계약 degrade |

**의존성 실측(uv.lock)**: `langchain-core 1.4.9` · `langgraph 1.2.9` · `langchain-anthropic 1.4.8` · `httpx 0.28.1` · `pydantic 2.x` · `pytest`. **`langchain`(v1) 없음** → `@tool`은 `langchain_core.tools.tool`. **`respx` 없음** → HTTP mock은 `httpx.MockTransport`. **`pandas` 없음** → 계산은 stdlib `statistics`만.

### 0.1 SPEC-SELLER-001 §4/§5 ↔ api-spec 대조 결과 (충돌 시 api-spec 채택)

| # | SPEC-SELLER-001 서술 | api-spec 최신(v0.8.0~v0.14.0) | 채택안 | SPEC 개정 필요 |
|---|---|---|---|---|
| A | §4/§5 제목 "**집계 7종(I-6~I-16)**" | 집계 **5종**(매출·퍼널·행동·이탈·계정) + 이력조회 2종(주문이벤트 I-14·상품변경 I-15) = **조회/집계 7 엔드포인트**(§4.4 표). "I-6~I-16" 연속 범위 표기는 부정확(I-8·I-13은 범위 밖) | api-spec §4.4의 **명시적 7 엔드포인트 목록**(I-6/7/13/14/15/16/8) 채택. "집계"는 5개 카테고리, "조회 도구"는 7개로 용어 분리 | ✅ §4/§5 "집계 7종(I-6~I-16)" → "조회 7 엔드포인트(집계 5 + 이력 2)"로 정정 |
| B | §4 표: I-7 = funnel, I-9 = list_my_products, I-10/11/12 = 쓰기 | 동일(§4.4 I-7 funnel, §4.5 I-9 GET·I-10 POST·I-11 PATCH·I-12 DELETE) | **일치 — 그대로 채택**. (구계약의 "I-7 상세 읽기"·"S-3 PATCH"는 이미 폐기됨) | — |
| C | §5·§10-② "AI 고도화 계산(pandas/순수 함수)" | pandas 미설치 | **stdlib `statistics` + 순수 함수**로 구현(pandas 금지) | ✅ §5 "pandas/순수 함수" → "stdlib 순수 함수" |
| D | §5 C-13: I-6 응답이 `isAnomaly`·`deviationPct` 반환(§4.4) — 3층 분담과 충돌(이상 판정은 AI-side) | §4.4 I-6 `series[{...,isAnomaly,deviationPct}]` 여전히 존재(🔴 C-13 미해소) | **calc는 원시 시계열(`date,sales,orderCount`)만 입력받아 자체 판정**. Spring `isAnomaly`/`deviationPct`는 **무시(참고치)** — 스키마엔 담되 계산 입력으로 쓰지 않음. 경계표 확정 시 어댑터만 수정 | 이미 §5 C-13에 기록됨 — 유지 |
| E | §4 `get_account_events` → I-8 `/internal/account-events` | §4.4: **전역(브랜드 스코프 아님)·admin 소유 🔴**. `{brandId}` path 없음 | I-8 함수는 **brandId 없는 시그니처**로 별도 설계. abuse 워커는 확정 전 **I-13/I-14 조합을 대체 소스**로(§4.4 주). 1단계는 인터페이스만, degrade 허용 | 유지(§4·§12 이미 🔴) |
| F | §4 도구 신원 인자 제거 방식 = "**클로저 또는 InjectedState**" / IMPL §0.2 = `ToolRuntime[SellerContext]` | — | **환경 확정: `ToolRuntime` 사용 금지 → 클로저 팩토리 채택**(langchain v1 미설치, 본 메모 §3) | ✅ IMPL §0.2 "ToolRuntime" 항목은 2단계 이후 재검토 — 1단계는 클로저 |
| G | §3·IMPL: `Identity`에서 `brandId` 확보 | **`Identity`(app/core/auth.py)에 `brand_id` 필드 없음**(현재 `user_id/is_guest/seller_id`만) | **선행 작업: `Identity`에 `brand_id: str \| None` 추가** + `_claims_to_identity`에 `brandId` 클레임 매핑. 클로저 팩토리가 `identity.brand_id`를 주입 | ✅ 블로킹 — §7 작업순서 0단계 |

> **결론**: SPEC-SELLER-001 §4 표 자체(도구↔I-number 매핑)는 api-spec과 **일치**한다. 정정 대상은 **§4/§5 서두의 "집계 7종(I-6~I-16)" 부정확 표기(A)**, **pandas 언급(C)**, **ToolRuntime 전제(F)**, 그리고 **`Identity.brand_id` 부재(G, 코드 블로커)** 다.

---

## 1. 전체 구조 요약

```
app/core/auth.py        Identity(+brand_id)                 ← 선행(0단계)
app/core/config.py      Settings(+판매자 임계값·토큰·타임아웃)  ← §5
app/schemas/spring.py   판매자 요청/응답 CamelModel(초안)      ← §2.4
app/services/spring_client.py
   class SpringClient    httpx.AsyncClient 래퍼(X-Internal-Token·3s·MockTransport 주입점)
                         조회 8 + 쓰기 3 = 11 메서드          ← §2
   (구스텁 get_seller_aggregates·get_product_detail 삭제, 구매자 함수 8종 불변)
app/agents/seller/tools.py
   build_seller_tools(identity, client) -> SellerToolset      ← §3 클로저 팩토리
      .read_tools     조회 8 + calculate + search_analysis_guide(stub)
      .product_tools  list_my_products + create/update/delete_product (product_agent 전용)
app/agents/seller/calc.py   순수 함수(이동평균·편차·이상판정·전환율·normalize_period)  ← §4
```

---

## 2. `spring_client` 판매자 함수군 신설 명세

### 2.1 구조 결정 — `SpringClient` 클래스 신설(구매자 함수 불변)

현재 `spring_client.py`는 모듈 레벨 구매자 함수 8종 + `_client()` 팩토리다. 판매자 함수군은 **신규 `SpringClient` 클래스**로 추가한다:

- **이유**: (1) 테스트에서 `httpx.MockTransport`를 주입하려면 `httpx.AsyncClient(transport=...)` 생성 지점이 필요한데, 현 `_client()`는 주입점이 없다. (2) `X-Internal-Token` 헤더·`base_url`·3s 타임아웃을 한 곳에 바인딩. (3) `build_seller_tools(identity, client)`가 요구하는 `client` 파라미터의 구체 타입.
- **구매자 함수 8종(`search_products`·`get_recent_purchases`·`add_to_cart`·`get_cart`·`push_recommendations`·`fetch_product_changes` 등)은 절대 건드리지 않는다** — 모듈 레벨 그대로 유지.
- **구스텁 2종 삭제**: `get_seller_aggregates`(구 I-6 단일)·`get_product_detail`(구 I-7 상세 읽기)는 신규 클래스 메서드로 **대체·삭제**한다. 두 함수는 `seller.py`의 docstring/TODO 주석에서만 참조되므로(임포트 아님) 주석 정리로 충분(§7).

```python
class SpringClient:
    """판매자 internal API 콜백 클라이언트 (api-spec §4.4/§4.5).

    X-Internal-Token 서비스 토큰 + {brandId} path + 3s 타임아웃(§2.3·§2.9 c).
    brand_id 는 메서드 인자로 명시 전달 — 호출자(tools.py 클로저)가 검증된
    Identity.brand_id 를 넣는다. 이 클래스는 신원을 스스로 판단하지 않는다.
    테스트는 transport 인자로 httpx.MockTransport 주입(respx 미설치).
    """

    def __init__(
        self,
        base_url: str,
        internal_token: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None: ...
```

- 공용 `_request()` 헬퍼가 `X-Internal-Token` 헤더·`raise_for_status()`를 처리하고, `httpx.TimeoutException`·`httpx.HTTPStatusError`·연결 실패를 **`SpringUnavailableError`로 변환**(기존 예외 재사용). degrade/오류 문자열화는 **tools 계층**에서 수행(클라이언트는 raise, 도구는 문자열 반환 — 관심사 분리).

### 2.2 조회 8종 (전부 GET · `X-Internal-Token` · 3s)

| 메서드 | HTTP · 경로 | 주요 파라미터 | 응답 모델 | api-spec |
|---|---|---|---|---|
| `get_sales(brand_id, from_, to, granularity)` | GET `/internal/seller/{brandId}/sales` | `from`·`to`(필수)·`granularity`(daily/weekly/monthly/summary) | `SalesResult` | §4.4 I-6 |
| `get_funnel(brand_id, from_, to)` | GET `/internal/seller/{brandId}/funnel` | `from`·`to` | `FunnelResult` | §4.4 I-7 |
| `get_events(brand_id, from_, to, ...)` | GET `/internal/seller/{brandId}/events` | 행동 이벤트 집계 파라미터 | `BehaviorEventsResult` | §4.4 I-13 |
| `get_order_events(brand_id, from_, to, to_status, actor_type, group_by)` | GET `/internal/seller/{brandId}/order-events` | `toStatus`(8종 복수)·`actorType`·`stats`·`groupBy` | `OrderEventsResult` | §4.4 I-14 |
| `get_product_changes(brand_id, from_, to, change_type, product_id)` | GET `/internal/seller/{brandId}/product-changes` | `changeType`(PRICE/STOCK/STATUS)·`productId` | `ProductChangeLogResult` | §4.4 I-15 |
| `get_churn(brand_id, inactive_days)` | GET `/internal/seller/{brandId}/churn` | `inactiveDays` | `ChurnResult` | §4.4 I-16 |
| `get_account_events(event_type, from_, to, group_by)` | GET `/internal/account-events` | **brandId 없음(전역)**·`eventType`·`groupBy` | `AccountEventsResult` | §4.4 I-8 🔴 |
| `list_products(brand_id, status, q, limit, offset)` | GET `/internal/seller/{brandId}/products` | `status`(ON_SALE/HIDDEN)·`q`·`limit`/`offset` | `SellerProductList` | §4.5 I-9 |

> ⚠️ **I-8은 brandId path가 없다** — 시그니처에서 `brand_id`를 받지 않는다(전역·admin 소유 🔴). 나머지 7종은 `{brandId}` path 필수. `I-15 product-changes`(판매자 감사 로그)는 구매자 `fetch_product_changes`(§4.8 I-17 AI 생성물 배치)와 **다른 계약**임에 주의(혼동 금지, §4.4 주).

### 2.3 쓰기 3종 (product_agent 전용 · `X-Internal-Token` · 3s)

| 메서드 | HTTP · 경로 | Body | 응답 | api-spec |
|---|---|---|---|---|
| `create_product(brand_id, payload)` | POST `/internal/seller/{brandId}/products` | `name`·`price`(≤`originalPrice`)·`stockQuantity`(≥0) 필수 | 201 `{productId, status:"ON_SALE"}` | §4.5 I-10 |
| `update_product(brand_id, product_id, patch)` | PATCH `/internal/seller/{brandId}/products/{productId}` | 바꿀 필드만(가격·설명·상태·`stockQuantity`) — 재고 통합, 별도 API 없음 | 200 갱신분 | §4.5 I-11 |
| `delete_product(brand_id, product_id)` | DELETE `/internal/seller/{brandId}/products/{productId}` | 없음 | 200 `{productId, status:"HIDDEN"}` (soft) | §4.5 I-12 |

- 쓰기는 **HITL 승인 후에만** 호출(1단계는 함수만 배선, interrupt/confirm은 4단계). `product_change_logs`(PRICE/STOCK/STATUS)는 Spring이 기록(I-10 등록은 미기록).
- **소유권 검증은 Spring**(`brandId` 기준). AI는 신원을 본문에서 받지 않는다.

### 2.4 신규 CamelModel (app/schemas/spring.py 판매자 섹션)

api-spec §4.4/§4.5 **초안(🔴 C-13/C-14)** 필드를 그대로 미러. `CamelModel`(alias_generator=to_camel, populate_by_name) 규약 준수. 초안이라 확정 시 어댑터만 수정.

- **요청**: `ProductCreate`(name/price/originalPrice/stockQuantity/category/description/imageUrl…), `ProductUpdate`(전 필드 Optional — "바꿀 필드만").
- **응답**:
  - `SalesSeriesPoint`(date/sales/orderCount/**isAnomaly**/**deviationPct**) — 판정 2필드는 **보관만·계산 미사용**(§0.1 D). `SalesResult(series: list[...])`.
  - `FunnelResult`(view/cart/checkout/purchase 4단).
  - `ChurnResult`(churnRate/preChurnSignals…).
  - `OrderEventsResult`·`BehaviorEventsResult`·`ProductChangeLogResult`·`AccountEventsResult`(초안 필드 최소집합 + `extra="allow"` 고려 — Spring 실측 필드 유동 🔴).
  - `SellerProductRow`(productId/name/price/**originalPrice**/stockQuantity/status/displayedSalesCount/category/description/imageUrl) · `SellerProductList(rows: list[...])`. ※ **`originalPrice`**(구매자 `SpringProduct.listPrice`와 필드명 다름 — 별도 모델).
- `productId`는 전 구간 **string**. 판매자/게스트/사용자 id는 숫자.

---

## 3. `app/agents/seller/tools.py` 설계 — 클로저 팩토리

### 3.1 팩토리 시그니처와 반환 구조

```python
@dataclass(frozen=True)
class SellerToolset:
    read_tools: list[BaseTool]      # 분석 워커·general·recommend 용(조회만)
    product_tools: list[BaseTool]   # product_agent 전용(list_my_products + 쓰기 3종)

def build_seller_tools(identity: Identity, client: SpringClient) -> SellerToolset: ...
```

- SPEC §4의 `-> list[BaseTool]`를 **`SellerToolset`으로 정련** — 쓰기 도구를 **타입 수준에서 분리 반환**해 "쓰기는 product_agent에만 배정"(§4·§93행)을 강제. 오분류돼도 read_tools에는 쓰기 도구가 물리적으로 없다.
- `list_my_products`는 **양쪽 리스트에 모두** 포함(recommend/general이 조회용으로, product_agent가 `before` 확보용으로 사용, §4.5).

### 3.2 클로저로 신원 주입 (IDOR — 어떤 @tool 시그니처에도 sellerId/brandId 없음)

```python
def build_seller_tools(identity, client):
    brand_id = identity.brand_id  # 검증된 JWT 클레임 유래 — LLM 이 만들 수 없음

    @tool
    async def get_sales_timeseries(from_date: str, to_date: str, granularity: str = "daily") -> str:
        """지정 기간의 일/주/월별 매출·주문수 시계열을 조회한다. ...(한국어 docstring)"""
        try:
            res = await client.get_sales(brand_id, from_date, to_date, granularity)
        except SpringUnavailableError as e:
            return f"Error: 매출 데이터를 불러오지 못했습니다({e}). 다른 기간으로 다시 시도하거나 없이 진행하세요."
        return _summarize_sales(res)   # 문자열 요약 반환

    ... (나머지 도구 동일 패턴)
    return SellerToolset(read_tools=[...], product_tools=[...])
```

- `brand_id`는 **클로저 캡처** — 도구 인자가 아니다. LLM은 신원 인자를 생성할 수 없다. (langchain v1 `ToolRuntime`는 미설치라 사용 안 함, §0.1 F.)
- `@tool` = `langchain_core.tools.tool` (반환은 `StructuredTool`, `BaseTool` 하위). 비동기 도구 지원.

### 3.3 @tool 목록 · docstring · 인자 · 반환 포맷

모든 도구: **인자는 조회 파라미터뿐**(기간·상태·productId 등, 전부 LLM이 합법적으로 만들 수 있는 값) · **docstring 한국어**(언제 쓰는지 + Args) · **반환은 한국어 문자열 요약**(원시 JSON 아님) · **오류는 `"Error: ..."` 문자열**(raise 금지, 에이전트 자가수정 유도) · **기준 시점 고지 문구를 반환에 강제**(IMPL §참고 — "기준: 2026-07-01~07-14 집계값").

**조회 계열(read_tools)** — 소비 서브에이전트는 참고용(2단계에서 배정):

| @tool | client 메서드 | 인자 | 반환 요약 예 | 소비(참고) |
|---|---|---|---|---|
| `get_sales_timeseries` | `get_sales` | from_date, to_date, granularity | "기간 …의 총매출 X원/주문 Y건, 일별 최고 Z…(기준시점 고지)" | sales_anomaly·general·recommend |
| `get_funnel` | `get_funnel` | from_date, to_date | "view N→cart M→checkout K→purchase P, 단계별 전환율 …" | conversion·behavior |
| `get_behavior_events` | `get_events` | from_date, to_date | "행동 이벤트 집계 요약 …" | behavior·abuse |
| `get_order_events` | `get_order_events` | from_date, to_date, to_status?, actor_type?, group_by? | "주문 상태 전이 요약 …" | sales_anomaly·churn·abuse·general |
| `get_product_change_logs` | `get_product_changes` | from_date, to_date, change_type?, product_id? | "상품 변경 이력 N건(가격/재고/상태) …" | sales_anomaly·churn·recommend |
| `get_churn_cohort` | `get_churn` | inactive_days | "이탈률 X%, 이탈 전 신호 …" | churn |
| `get_account_events` | `get_account_events` | event_type?, from_date, to_date, group_by? | "계정/보안 이벤트 요약 …(전역·🔴)" | abuse·churn |
| `list_my_products` | `list_products` | status?, q?, limit?, offset? | "상품 N건: [P-…] 이름/가격/재고/상태 …" | recommend·general·product(before) |
| `calculate` | (없음, calc.safe_eval) | expression | "계산 결과: …" | 전 워커(수치 확인) |
| `search_analysis_guide` | (없음, RAG 미구현) | query | **NotImplementedError 스텁** → 도구는 `"Error: 분석 기준서 검색은 아직 준비 중입니다."` 반환 | 전 워커(기준서) |

> `calculate`: IMPL 참고 자산의 `safe_eval`(ast 화이트리스트 — 사칙연산·`round`·비율만, `__import__`·속성접근 금지)로 구현. 계산 로직은 calc.py에 두고 도구는 얇은 래퍼.
> `search_analysis_guide`: 기준서 문서 부재(🔴, SPEC §9.2)로 **인터페이스만** — api-spec/SPEC § 참조 주석 + `NotImplementedError` 내부, 도구 표면은 degrade 문자열. 4단계 활성화.

**쓰기 계열(product_tools 전용)**:

| @tool | client 메서드 | 인자 | 반환 |
|---|---|---|---|
| `create_product` | `create_product` | name, price, stock_quantity, original_price?, category?, description? | "등록됨: productId=… (status=ON_SALE)" / "Error: …" |
| `update_product` | `update_product` | product_id, **바꿀 필드 kwargs**(price?/description?/status?/stock_quantity?) | "수정됨: productId=… 변경필드 …" / "Error: …" |
| `delete_product` | `delete_product` | product_id | "삭제(숨김)됨: productId=… (status=HIDDEN)" / "Error: …" |

- 쓰기 도구는 1단계에서 **Spring 호출까지만** 배선. HITL draft/interrupt/confirm(§6 SPEC)은 4단계. 재고 delta→절대값 환산(IMPL 자산)은 update 래퍼에서 처리(현재고 조회 후 환산 or 절대값만 허용 — 2단계 프롬프트와 정합, 1단계는 절대값 인자).

### 3.4 degrade 규약 (SPEC §4·§7)

- 조회 실패/3s 초과 → 해당 도구가 **`"Error: ..."` 문자열 반환**, 파이프라인은 계속(부분 보고서). raise로 스트림을 죽이지 않는다.
- 쓰기 실패(I-10/11/12) → `"Error: ..."` 문자열(상위에서 `token` 안내 + `done` 종료, `error` 아님).

---

## 4. `app/agents/seller/calc.py` 설계 — 순수 함수(stdlib만, pandas 금지)

3층 분담(§5)의 **AI 고도화 계산(코드)** 층. 임계값은 전부 Settings 주입 — 하드코딩·프롬프트 숫자 금지. `statistics`(stdlib)만 사용. 도구가 아니라 **워커 코드/프롬프트 주입용 순수 함수**(단, `calculate`만 @tool 래퍼).

```python
def moving_average(values: list[float], window: int) -> list[float | None]:
    """단순 이동평균. window 미만 구간은 None(경계 안전)."""

def deviation_pct(actual: float, baseline: float) -> float:
    """기준 대비 편차 %. baseline==0 이면 0.0(0 나눗셈 방지)."""

def is_anomaly(deviation: float, *, threshold_pct: float) -> bool:
    """|편차| >= threshold_pct 면 이상. 경계(==)는 이상으로 판정."""

def detect_sales_anomalies(series: list[SalesSeriesPoint], *, window: int, threshold_pct: float
                          ) -> list[tuple[str, float, bool]]:
    """일별 매출을 이동평균 대비 편차·이상판정. (date, deviationPct, isAnomaly) 목록.
    Spring 이 준 isAnomaly/deviationPct 는 무시하고 원시 sales 로 재계산(§0.1 D)."""

def conversion_rates(funnel: FunnelResult) -> dict[str, float]:
    """단계별 전환율(view→cart→checkout→purchase)."""

def compare_conversion(current: FunnelResult, baseline: FunnelResult, *, drop_pct: float
                      ) -> dict[str, bool]:
    """단계별 전환율이 baseline 대비 drop_pct 이상 하락했는지."""

def normalize_period(expr: str, *, today: date, recent_default_days: int) -> tuple[date, date]:
    """자연어 기간 → (from, to).
    - "지난달"  = 전월 1일 ~ 전월 말일(연 경계 롤오버: 1월 처리 → 전년 12월).
    - "최근 N일" = (today - N) ~ (today - 1)  ← 오늘 제외(§10-④, 당일 데이터 미완결).
    - "이번 주"/"어제" 등은 동일 규약으로 확장. 파싱 불가 시 ValueError(호출부에서 되물음)."""
```

- 임계값 인자(`window`·`threshold_pct`·`drop_pct`·`inactive_days`·`recent_default_days`)는 **호출부가 Settings에서 주입** — calc.py 내부에 기본 숫자 하드코딩 금지.
- 함수는 전부 **결정론·부작용 없음**(일관성 장치 §10-②) — 같은 입력 = 같은 출력, 테스트 용이.

---

## 5. `app/core/config.py` 추가 Settings 필드

| 필드 | 타입 | 기본값 | 용도 · 근거 |
|---|---|---|---|
| `internal_token` | `str \| None` | `None` | AI→Spring `X-Internal-Token` 서비스 토큰(§2.3 v0.13.0). ※ 기존 `service_token`은 이벤트용(deprecated)이라 별도 신설 |
| `spring_timeout_s` | `float` | `3.0` | AI→Spring 3s(§2.9 c). 하드코딩(`_client()` `timeout=3.0`)을 config로 이관 |
| `seller_ma_window` | `int` | `7` | 매출 이동평균 window(일) |
| `seller_anomaly_deviation_pct` | `float` | `30.0` | 매출 이상판정 편차 임계(%) |
| `seller_conversion_drop_pct` | `float` | `20.0` | 전환율 하락 이상 임계(%) |
| `seller_churn_inactive_days` | `int` | `30` | 이탈 코호트 무활동 일수(I-16 `inactiveDays` 기본) |
| `seller_recent_days_default` | `int` | `7` | `normalize_period` "최근 N일" 기본 N |
| `seller_report_score_threshold` | `int` | `21` | 보고서 검증 통과 점수(21/30, §10-⑦) — 1단계는 필드만, 소비는 2·3단계 |
| `seller_report_max_retries` | `int` | `3` | 검증 루프 상한(≤3, §10-⑦) |
| `seller_draft_ttl_minutes` | `int` | `10` | HITL 미승인 draft 만료(§6.2-5) — 4단계 소비, 필드 선등록 |
| `seller_history_recent_n` | `int` | `5` | planner 최근 분석 이력 주입 건수(§9.1) — 4단계 소비 |
| `seller_tool_call_limit` | `int` | `8` | (선택) ToolCallLimit 전역 한도(IMPL 자산) — 미들웨어(3단계) 소비, 필드 선등록 |

- 전부 환경변수 주입, 대문자 매핑(기존 Settings 규약). 1단계에서 실제 사용하는 것은 `internal_token`·`spring_timeout_s`·`seller_ma_window`·`seller_anomaly_deviation_pct`·`seller_conversion_drop_pct`·`seller_churn_inactive_days`·`seller_recent_days_default`. 나머지는 후속 단계 대비 선등록(하드코딩 재발 방지).

---

## 6. 테스트 목록 (TDD — 테스트 먼저)

HTTP는 `httpx.MockTransport`(respx 미설치). LLM 호출 없음(1단계는 무-LLM).

### `tests/unit/test_seller_spring_client.py` (MockTransport)
- `test_get_sales_hits_brand_path_with_internal_token` — 요청 URL이 `/internal/seller/{brandId}/sales`, 헤더 `X-Internal-Token` 존재, `from/to/granularity` 쿼리 확인.
- `test_get_sales_parses_camel_response` — camelCase 응답(`series[{date,sales,orderCount,isAnomaly,deviationPct}]`) → `SalesResult` 파싱.
- `test_account_events_has_no_brand_in_path` — I-8은 `/internal/account-events`(brandId 없음).
- `test_create_product_posts_body_by_alias` — POST 바디가 camelCase(`stockQuantity` 등), 201 파싱.
- `test_update_product_uses_patch_and_product_path` — PATCH `/…/products/{productId}`, 바꿀 필드만 전송.
- `test_delete_product_uses_delete_and_returns_hidden` — DELETE, 응답 `status=HIDDEN`.
- `test_timeout_maps_to_spring_unavailable` — `MockTransport`가 `httpx.TimeoutException` → `SpringUnavailableError`.
- `test_4xx_maps_to_spring_unavailable` — 404/500 → `SpringUnavailableError`.
- `test_client_uses_configured_timeout_3s` — `SpringClient`가 `settings.spring_timeout_s`(=3.0) 사용.

### `tests/unit/test_seller_tools.py`
- `test_write_tools_isolated_from_read` — `SellerToolset.read_tools`에 create/update/delete 이름 없음, `product_tools`에만 존재.
- `test_no_identity_params_in_any_tool` — 모든 도구 `tool.args`(args_schema)에 `sellerId`/`brandId`/`seller_id`/`brand_id` 키 부재(IDOR — 신원 미노출).
- `test_closure_injects_brand_id` — mock `SpringClient`가 기록한 `brand_id == identity.brand_id`(도구 인자로 전달되지 않았음을 확인).
- `test_tool_returns_error_string_on_spring_failure` — client가 `SpringUnavailableError` → 도구 반환이 `"Error:"`로 시작(raise 아님).
- `test_tool_returns_error_string_on_timeout` — 타임아웃 → `"Error:"` 문자열.
- `test_sales_tool_summary_includes_reference_period` — 반환 문자열에 기준 기간 고지 포함.
- `test_search_analysis_guide_is_stub` — degrade 문자열 반환(내부 NotImplementedError 비노출).
- `test_list_my_products_in_both_lists` — read·product 양쪽에 존재.

### `tests/unit/test_seller_calc.py`
- `test_moving_average_window_boundary` — len<window 구간 None, 이후 정확값.
- `test_deviation_pct_sign_and_zero_baseline` — 음/양 부호, baseline 0 → 0.0.
- `test_is_anomaly_threshold_boundary` — 편차 == 임계면 이상(True), 미만이면 False.
- `test_detect_sales_anomalies_ignores_spring_flags` — 입력 series의 `isAnomaly`가 반대여도 원시 sales로 재판정(§0.1 D).
- `test_conversion_rates_and_drop` — 단계 전환율·하락 임계 판정.
- `test_normalize_period_last_month_year_rollover` — 1월 today → 전년 12/1~12/31.
- `test_normalize_period_recent_n_excludes_today` — (today-N)~(today-1), 오늘 미포함.
- `test_calc_uses_injected_thresholds` — 임계값이 인자로 주입됨(하드코딩 부재 — 다른 임계값 주입 시 결과 변화).

### `tests/unit/test_config_seller.py`
- `test_seller_settings_defaults` — 신규 필드 기본값(§5 표).
- `test_spring_timeout_default_is_3s` — `spring_timeout_s == 3.0`.

### `tests/unit/test_schemas_camel.py` (기존 파일에 추가)
- `test_seller_product_row_serializes_camel` — `originalPrice`/`stockQuantity`/`displayedSalesCount` camelCase.
- `test_product_create_by_alias` — 요청 바디 camelCase 직렬화.

---

## 7. 파일 단위 작업 순서 (구현 에이전트 체크리스트)

> 착수 전: **api-spec 사본 버전 헤더(v0.14.0)·§3.2/§4.4/§4.5 재확인**(lessons). `cd /home/…/jarvis-ai-repo && pwd`로 cwd 못 박기. 구매자 `spring_client` 함수 8종·구매자/추천 코드 **미접촉**.

0. **[선행·블로커] `app/core/auth.py`**: `Identity`에 `brand_id: str | None = None` 추가 + `CLAIM_BRAND_ID = "brandId"` 상수 + `_claims_to_identity`의 SELLER 분기에서 `brand_id=claims.get(CLAIM_BRAND_ID)` 매핑. (테스트: dev 모드 seller 토큰에 brandId 클레임 → Identity.brand_id 확인.) — 구매자 경로 영향 없음(신규 필드 기본 None).
1. **`app/core/config.py`**: §5 표 필드 추가. 테스트 `test_config_seller.py` 먼저.
2. **`app/schemas/spring.py`**: 판매자 섹션 CamelModel(§2.4) 추가. 테스트 `test_schemas_camel.py` 보강.
3. **`app/agents/seller/calc.py`**: 순수 함수(§4). 테스트 `test_seller_calc.py` 먼저 → 구현.
4. **`app/services/spring_client.py`**: `SpringClient` 클래스(§2.1~2.3) 추가, 구스텁 2종(`get_seller_aggregates`·`get_product_detail`) 삭제. 테스트 `test_seller_spring_client.py`(MockTransport) 먼저 → 구현.
5. **`app/api/seller.py`**: 삭제된 구스텁을 참조하는 **docstring/TODO 주석 정리**(임포트 없음 — 서술만 v0.14.0로 갱신, "FE S-3 PATCH" 폐기 반영). 동작 변경 없음.
6. **`app/agents/seller/tools.py`**: `SellerToolset` + `build_seller_tools`(§3) 클로저 팩토리. `calculate`·`search_analysis_guide`(스텁) 포함. 테스트 `test_seller_tools.py` 먼저 → 구현.
7. **정리·커밋**: `uv run ruff check --fix && uv run ruff format` → `uv run pytest`(전 그린 근거 제시) → `docs/lessons.md`에 진단 사항 기록 → CHANGELOG `[Unreleased]` Added 항목 → `feat(seller): ...` Conventional Commit(1 논리단위 = 1 커밋; 선행 auth 변경은 별도 커밋 권장).

> **미확정 스키마(🔴 C-13/C-14)**: api-spec 초안 필드로 구현 + MockTransport. 확정 시 `SpringClient`/스키마 **어댑터만** 수정(도구·calc 불변). I-8·`search_analysis_guide`는 인터페이스만(degrade).

---

## 8. 핵심 결정 5줄 요약

1. SPEC-SELLER-001 §4 도구↔I-number 매핑은 api-spec과 일치 — 정정 대상은 서두 "집계 7종(I-6~I-16)" 부정확 표기(→ 조회 7 엔드포인트=집계 5+이력 2), pandas 언급(→ stdlib), ToolRuntime 전제(→ 클로저).
2. 신원 주입은 **클로저 팩토리** `build_seller_tools(identity, client) -> SellerToolset` — 어떤 @tool 시그니처에도 brandId/sellerId 없음(IDOR). 단, **`Identity.brand_id` 필드가 없어 선행 추가가 블로커**.
3. `spring_client`에 **`SpringClient` 클래스 신설**(httpx.AsyncClient+X-Internal-Token+3s, MockTransport 주입점) — 구매자 함수 8종 불변, 구스텁 2종 삭제.
4. 쓰기 3종은 `SellerToolset.product_tools`로 **타입 수준 분리** — 오분류돼도 read_tools엔 쓰기 도구 부재. 오류는 raise 대신 `"Error: ..."` 문자열(degrade).
5. calc.py는 **stdlib `statistics` 순수 함수**(pandas 금지), 임계값 전부 Settings 주입 — Spring의 `isAnomaly`/`deviationPct`는 무시하고 원시 시계열로 재판정(C-13 대비).

---

*문서 끝. 개정 시 CHANGELOG와 본 헤더 버전을 함께 갱신한다.*
</content>
</invoke>

---

## 부록 A. 백엔드 API 명세서 CSV(MVP 최종본) 대조 — 2026-07-18

사용자 제공 「API 명세서」 CSV(인호꺼=Yes 14건 = 판매자 MVP 범위 최종본)와 1단계 도구를 행 단위 대조한 결과.

- **I-6~I-16 (internal 11건)**: 경로·메서드·인증(서비스 토큰) 전부 구현과 일치 — 수정 0건.
  리포 api-spec v0.14.0 판매자 섹션과 CSV의 I-번호 체계 동일 확인.
- **I-6 "이상플래그"**: Spring이 판정 필드를 반환하나 도구는 참고치로 무시하고 원시 수치로
  재판정(§0.1 D 유지). 계산 경계는 C-13 협의 항목 그대로.
- **I-11 필드 범위**: CSV 설명은 "가격·설명·상태·재고 통합" 4필드 언급이나,
  **8필드(name·originalPrice·category·imageUrl 포함) 노출 유지로 확정**(2026-07-18 사용자).
  ⚠️ 잔여 리스크: 백엔드가 name 등 미지원 시 400 → 도구의 Error 문자열 degrade 로 흡수,
  C-14 협의에서 지원 필드 확정 필요.
- **I-12**: CSV "DB 논의 필요" 플래그 인지 — soft delete(HIDDEN) 전제 유지.
- **S-1/S-2/S-3 (SELLER JWT, FE→Spring 레인)**: **FE 전용, AI 도구 미구현으로 확정**
  (2026-07-18 사용자). AI는 서비스 토큰 internal 레인(I-6~I-16)만 사용 —
  api-spec v0.13.0 정정("S-3는 FE 대시보드용, AI는 I-9")과 일치.
- **S-4** `{AI_SERVER}/seller/chat`: AI 서버 자기 엔드포인트(도구 대상 아님, 3~4단계 배선).
