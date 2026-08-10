"""챗봇 요청/응답 스키마 (api-spec v0.7.0 §3.1).

[변경 v0.4.0] CH-2 명명 기준 채택 / [변경 v0.6.0] action.reason 재편(게스트 담기 허용):
  - SSE 이벤트명(구매자): token / conditions / action / suggestions / budget / products.ready /
    done / error (구 text.delta / products / (done) 세트 폐기). suggestions=완화/되돌리기 칩(결정 14-D/14-F, §3.1).
  - 모든 페이로드 필드는 camelCase (Pydantic alias). Python 속성은 snake_case 유지,
    직렬화 시 by_alias=True 로 camelCase 출력, 입력은 populate_by_name 으로 양쪽 허용.
  - [HARD] SSE 는 상품 카드를 싣지 않는다 (경로 B). products.ready 는
    {sessionId, listIds} 상관관계 키만 나른다. 상품 카드/표시 필드는
    Spring push(§4.2)+GET(§4.3)으로 이동.

[보안] ChatRequest 에는 userId 가 없다 — 신원은 토큰 클레임(sub/role)에서만 도출한다 (§3.1).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import field_validator, model_validator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.text import _strip_unsafe

logger = logging.getLogger(__name__)


class CamelModel(BaseModel):
    """camelCase 직렬화 공통 베이스.

    alias_generator=to_camel 로 스네이크 속성을 camelCase alias 에 매핑하고,
    populate_by_name=True 로 입력 시 스네이크/카멜 양쪽을 허용한다.
    직렬화는 .model_dump(by_alias=True) 로 camelCase 출력.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# pageType 어휘 (14종 — 구매자 10 · 판매자 4, api-spec §3.1 표, E-1 정본과 공유). 계약 어휘라
# config 가 아니라 스키마에 둔다(표시명 매핑은 app.core.config.screen_page_type_labels).
#
# [11차 리뷰] 역할별로 나눠 둔 이유 — 아래 `_SCREEN_PAGE_TYPES_BY_REQUEST_CLASS` 가 이 둘을
# `_normalize_screen` 의 역할별 허용 집합으로 쓴다. **주의: 정본 §3.1 표(라인 415~432)는 어휘를
# 구매자/판매자로 "분류"했을 뿐, 반대 역할 값을 거부하라고 명시적으로 요구하지 않는다.** 즉
# 구매자 요청이 `seller_orders` 를 보내도 정본 문면만 보면 위반이 아니다 — 이 분리는 **계약
# 위반을 고친 것이 아니라 계약이 이미 그어 둔 역할 경계를 코드로도 지키는 방어적 강화**다(관대
# 무시 없이는 구매자 요청의 `seller_orders` 가 "주문 관리" 라벨로 그대로 구매자 decompose
# 프롬프트에 실린다 — 실측 재현).
_BUYER_SCREEN_PAGE_TYPES: frozenset[str] = frozenset(
    {
        "home",
        "category",
        "search",
        "product_detail",
        "cart",
        "checkout",
        "order_complete",
        "my",
        "chat",
        "auth",
    }
)
_SELLER_SCREEN_PAGE_TYPES: frozenset[str] = frozenset(
    {
        "seller_dashboard",
        "seller_orders",
        "seller_products",
        "seller_chat",
    }
)
SCREEN_PAGE_TYPES: frozenset[str] = _BUYER_SCREEN_PAGE_TYPES | _SELLER_SCREEN_PAGE_TYPES

# [11차 리뷰] `_normalize_screen`(아래, `ChatRequest` 의 classmethod validator)이 `cls.__name__`
# 으로 조회하는 역할별 허용 집합. **리뷰가 제안한 형태(각 서브클래스가 `ClassVar` 를 override)를
# 그대로 쓰지 않은 이유**: `BuyerChatRequest` 는 이 파일에 있지만 `SellerChatRequest` 는
# `app/schemas/seller.py` 에 있고, 이번 수정 범위가 `app/schemas/chat.py` 로 한정돼 있어(#132·
# #277 등이 그 옆 파일을 동시에 작업 중이라 다른 파일을 건드리면 충돌 위험) 그 파일에 override
# 를 둘 수 없다. `ChatRequest` 가 `SellerChatRequest` 를 import 하면 `seller.py` → `chat.py` →
# `seller.py` 순환 임포트가 되므로 그 방향의 참조도 불가능하다. 그래서 이름으로 찾는 매핑을 여기
# 한 곳에 둔다 — 두 서브클래스가 서로 다른 메커니즘(한쪽은 `ClassVar` override, 한쪽은 이름
# 매핑)을 쓰면 다음 사람이 헷갈리므로 **둘 다 이 매핑 하나로** 통일했다. 매핑에 없는 클래스
# (맨 `ChatRequest` 를 직접 쓰는 경로 등)는 14종 전체를 허용해 기존 동작을 보존한다.
_SCREEN_PAGE_TYPES_BY_REQUEST_CLASS: dict[str, frozenset[str]] = {
    "BuyerChatRequest": _BUYER_SCREEN_PAGE_TYPES,
    "SellerChatRequest": _SELLER_SCREEN_PAGE_TYPES,
}

# screen.filters 허용 키 3종(api-spec §3.1) — 그 밖의 키는 관대 무시 대상.
#
# [13차 리뷰] **frozenset 이 아니라 순서 고정 tuple 이다.** `_normalize_screen` 이 이 3키만
# 직접 조회(raw_filters.get(key))하도록 바꾸면서(원본 dict.items() 순회를 없애 무계 순회 표면을
# 지우기 위해서 — 아래 `_normalize_screen` 주석 참조) 결과 `filters` dict 의 키 순서가 이 튜플의
# 순서를 따르게 됐다. frozenset 은 반복 순서가 정의 순서와 무관해(해시 기반) 이 자리에 쓰면
# 안 된다(실측: `frozenset({"status","sort","page"})` 를 순회하면 `page, status, sort` 순으로
# 나온다). 이 순서는 새 계약이 아니다 — 기존에도 `filters` 는 dict 순서를 보장하지 않았다
# (원본 raw_filters 가 보낸 순서를 그대로 흘렸을 뿐). 순서에 의존하는 기존 테스트를 전부 확인
# 했다 — 대부분 `==`(순서 무관)·`in` 부분일치이고, 유일한 예외인 SCREEN JSON 문자열 정확
# 일치 테스트(tests/unit/test_decompose.py)는 그 케이스가 `status`→`page` 순(둘 다 이 튜플의
# 상대 순서와 같다)이라 이 변경으로 깨지지 않는다.
SCREEN_FILTER_KEYS: tuple[str, ...] = ("status", "sort", "page")

# [15차 리뷰, F-18] PostgreSQL BIGINT 상한 — 신원·상품 id 범위 방어.
#
# app/schemas/recommendations.py·events.py·profile.py 가 이미 각자 파일에 같은 값으로
# `_BIGINT_MAX = 2**63 - 1` 를 두고 있다(모듈 간 import 로 공유하지 않는 게 이 저장소의 기존
# 관행 — 세 파일 다 로컬 정의다). 공용 모듈로 빼는 리팩터링도 검토했지만, 이 PR 은 F-18(BIGINT
# 상한 누락) 수정 범위이지 기존 3파일의 중복을 정리하는 리팩터링 범위가 아니다 — 지금 빼면
# 이 변경과 무관한 3개 파일의 import 를 건드리게 돼 리뷰 범위가 부풀고, 기존 관행과도 어긋나지
# 않는다(이미 4번째 로컬 정의가 아니라 기존 3번째 관행을 그대로 따르는 것). 값을 좁히는 리팩터링이
# 필요해지면 그때 네 파일을 한 번에 옮기는 편이 낫다.
_BIGINT_MAX = 2**63 - 1


def _coerce_positive_int(value: object) -> int | None:
    """screen.products[].productId 관대 파싱 — bool 제외, 정수 float·숫자 문자열 허용.

    app/agents/buyer/recommendation/decompose.py::_as_int 와 같은 규약이지만, 스키마 계층에서
    도메인 로직을 import 하면 계층 역전이라 여기 별도로 둔다. 상품 id 는 양의 BIGINT 라
    0 이하는 버린다(decompose._as_int 에는 없는 추가 규칙).

    [15차 리뷰, F-18] **상한(`_BIGINT_MAX`)도 같은 관대 유효성으로 버린다.** 다른 모든 BIGINT id
    필드(recommendations.ProductId·member_id, events.user_id, profile.user_id)는 `le=_BIGINT_MAX`
    를 강제하는데 여기만 빠져 있었다 — `productId=2**63+12345` 가 그대로 통과해
    `allowed_product_ids`(app/agents/buyer/graph.py)에 합류하고, 상한 없는
    `AddToCartRequest.product_id`(app/schemas/spring.py)로 Spring I-2 호출에 그대로 실려 Spring
    Long 역직렬화에서 예측 불가한 실패가 된다(실측 재현: 2**63+12345 가 파싱을 통과함). screen 은
    400 을 내지 않는 필드라(§3.1 유효성 표) 이 항목도 거부가 아니라 **그 상품 항목만 무효
    처리**한다 — 호출부(`_normalize_screen`)가 기존 `product_id is None` 경로로 이미 그렇게
    다룬다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str):
        try:
            result = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if 0 < result <= _BIGINT_MAX else None


def _clean_screen_text(value: str, max_chars: int, raw_scan_max: int) -> str:
    """screen 의 사람이 읽는 문자열 1건을 프롬프트에 실을 수 있게 정제·절단한다 (#118 라운드 2).

    `app.core.text._strip_unsafe` 와 같은 정제다 — 제어문자·zero-width·bidi 포맷 문자를 없애고
    공백류를 단일 공백으로 접는다. FE 문자열이 LLM 프롬프트로 직행하므로 ANSI 이스케이프·양방향
    조작·보이지 않는 지시문 삽입 표면을 여기서 닫는다. 그 뒤 `max_chars` 로 **절단**한다
    (거부하지 않는다 — screen 은 맥락 힌트라 400 을 내지 않는 것이 §3.1 유효성 표다).

    절단은 정제 **후**에 잰다. 먼저 자르면 제거될 문자가 예산을 먹어 멀쩡한 이름이 짧아진다.

    [14차 리뷰, F-17] **단, 정제 자체를 원문 전체에 돌리기 전에 `raw_scan_max` 로 먼저 한 번
    자른다.** `_strip_unsafe` → `strip_invalid_invisible_sequences` 는 문자 단위로 순회하는
    O(n) 검사라, `max_chars`(절단 상한)만 있고 원문 길이에 사전 상한이 없으면 name·filters 값이
    수백만 자여도 그 전체를 정제 비용으로 낸 **뒤에야** 잘린다(실측: 200만자 × 50건 = 25.02초,
    구매자 스트림 전체 상한 30s(§2.9 c)를 위협하는 서비스 거부). `raw_scan_max` 는
    `screen_text_max_chars` 의 넉넉한 배수라(config 주석 근거) 이 사전 절단이 정상 페이로드의
    정제 결과를 바꾸지 않는다 — 진짜 악성(수백만 자) 원문만 정제 비용이 유계가 된다.
    """
    return _strip_unsafe(value[:raw_scan_max])[:max_chars]


class ScreenProduct(CamelModel):
    """screen.products 항목 — 서버가 모르는 목록의 상품 1건 (api-spec §3.1)."""

    product_id: int
    name: str = ""  # 누락/비문자열이면 "" (관대, ChatRequest._normalize_screen 이 정제)


class ScreenContext(CamelModel):
    """screen — 사용자가 지금 보고 있는 화면 (api-spec §3.1, 이슈 #118).

    관대 유효성(400 을 내지 않음)은 ChatRequest._normalize_screen 이 미리 정제한 뒤에만
    이 모델로 넘기므로, 여기서는 이미 정제된 구조를 그대로 받는다.
    """

    page_type: str
    filters: dict[str, str] = Field(default_factory=dict)
    products: list[ScreenProduct] = Field(default_factory=list)
    columns: int | None = None


class ChatRequest(CamelModel):
    """POST /chat 및 POST /seller/chat 공통 요청 본문 (api-spec §3.1 / §3.2).

    screen 은 구매자·판매자 공용 필드다(conditionActions 가 구매자 전용인 것과 다르다) —
    BuyerChatRequest/SellerChatRequest 가 상속으로 받는다(api-spec §3.1 말미).
    """

    session_id: str = Field(..., description="Spring 발급 불투명 스레드 키 (만료 없음, §2.6)")
    thread_id: str = Field(..., description="대화 스레드 식별자. 멀티턴 필터 누적 대상")
    message: str = Field(
        ..., description="현재 턴 사용자 원문 질의 (길이 상한은 config chat_message_max_chars)"
    )
    screen: ScreenContext | None = Field(
        default=None, description="사용자가 보고 있는 화면 맥락 (api-spec §3.1, 이슈 #118)"
    )

    @field_validator("message")
    @classmethod
    def _limit_message_length(cls, v: str) -> str:
        """message 길이 상한을 config 에서 주입(하드코딩 금지). 초과 시 400(api-spec §3.1)."""
        from app.core.config import get_settings

        cap = get_settings().chat_message_max_chars
        if len(v) > cap:
            raise ValueError(f"message exceeds {cap} characters")
        return v

    @field_validator("session_id", "thread_id")
    @classmethod
    def _limit_key_length(cls, v: str) -> str:
        """불투명 식별자 길이 상한(config) — registry·저장소·로그 남용 방어. 초과 시 400."""
        from app.core.config import get_settings

        cap = get_settings().chat_key_max_chars
        if len(v) > cap:
            raise ValueError(f"identifier exceeds {cap} characters")
        return v

    @field_validator("screen", mode="before")
    @classmethod
    def _normalize_screen(cls, v: object) -> dict[str, object] | None:
        """screen 관대 유효성 (api-spec §3.1) — 맥락 힌트라 여기서는 절대 raise 하지 않는다.

        pageType 이 없거나 (역할별 허용 집합 기준, `_SCREEN_PAGE_TYPES_BY_REQUEST_CLASS`) 허용
        밖이면 screen 전체를 무시(None)한다. 그 밖의 하위 필드는 문제 있는 부분만 버리고
        나머지를 살린다(conditionActions 의 엄격(400)과 대비되는 철학).

        [11차 리뷰] **역할 경계도 "미지의 값"과 같은 취급이다.** `cls` 가 `BuyerChatRequest` 면
        허용 집합이 구매자 10종으로, `SellerChatRequest` 면 판매자 4종으로 좁혀진다(field_validator
        가 classmethod 라 `cls.__name__` 으로 실제 요청 클래스를 안다) — 구매자 요청이
        `seller_orders` 를 보내도 400 이 아니라 이 자리에서 screen 전체가 조용히 사라진다. 여전히
        400 을 내지 않는다 — 어휘 밖 값과 같은 경로(§3.1 유효성 표)를 타므로 이 필드의 "screen
        은 어떤 경우에도 400 을 내지 않는다"는 핵심 계약이 깨지지 않는다.

        입력은 세 모양을 받는다 — 와이어 경로의 dict, 임의의 `Mapping`, 그리고 **이미
        `ScreenContext` 인 인스턴스**. 마지막이 없으면 `BuyerChatRequest(screen=ScreenContext(...))`
        처럼 요청을 코드로 구성할 때 screen 이 소리 없이 사라진다(라운드 1 리뷰). 인스턴스도
        그대로 통과시키지 않고 dict 로 되돌려 **같은 정제 경로**를 태운다 — 그래야 구성 경로에
        따라 정제 여부가 갈리지 않는다.

        [#118 라운드 2] `products[].name` 과 `filters` 값은 **FE 가 보낸 문자열이 그대로 LLM
        프롬프트에 실린다**. 정제(제어·zero-width·bidi 제거)와 항목당 길이 절단
        (`screen_text_max_chars`)을 **여기 스키마 정규화 단계**에 둔 이유:
          1. 라운드 1 이 이미 이 한 곳에서 screen 전체를 정규화한다 — 신뢰경계 감사 지점이 하나다.
          2. `screen` 은 구매자·판매자 공용 필드다. 판매자 프롬프트(라운드 3)가 같은 문자열을
             다른 주입 지점에서 쓰는데, 주입 지점마다 정제를 두면 한쪽이 빠뜨릴 수 있다.
        초과 길이는 400 이 아니라 **절단**이다(관대 유효성 유지).
        """
        from collections.abc import Mapping

        if isinstance(v, ScreenContext):
            v = v.model_dump(by_alias=True)
        if not isinstance(v, Mapping):
            return None

        page_type = v.get("pageType", v.get("page_type"))
        allowed_page_types = _SCREEN_PAGE_TYPES_BY_REQUEST_CLASS.get(
            cls.__name__, SCREEN_PAGE_TYPES
        )
        if not isinstance(page_type, str) or page_type not in allowed_page_types:
            return None

        from app.core.config import get_settings

        settings = get_settings()
        products_max = settings.screen_products_max
        text_max = settings.screen_text_max_chars
        text_raw_scan_max = settings.screen_text_raw_scan_max

        raw_products = v.get("products")
        products: list[dict[str, object]] = []
        if isinstance(raw_products, list):
            # [13차 리뷰] **원본 배열 길이 자체를 먼저 하드 상한으로 자른다.** 아래 루프는(12차
            # 수정 이후) 유효 항목이 `products_max` 에 도달할 때까지 순회하므로, 이 슬라이스가
            # 없으면 `raw_products` 에 무효 항목(빈 dict 등)을 수만~수십만 건 채운 요청이 매번
            # 그 전체를 스캔한다 — `message` 는 `chat_message_max_chars` 로 길이 상한이 있는데
            # `products` 원본 크기는 상한이 없었던 비대칭이고, `app/main.py` 에 요청 바디 크기
            # 제한 미들웨어도 없어(레이트리밋은 §2.8 요청 "건수"만 제한) 이 경로가 열려 있었다
            # (실제 재현). `screen_products_raw_scan_max`(config, 기본 500 — 근거는 그 필드
            # 주석)는 `products_max`(20)보다 넉넉해 정상 FE 페이로드(화면에 보이는 상품은 한
            # 응답에 수십 건을 넘기 어렵다)는 자르지 않으면서 악성 페이로드의 스캔량을 유계로
            # 만든다. 관대 유효성은 유지한다 — 이 슬라이스도 400 이 아니라 절단이다.
            raw_products = raw_products[: settings.screen_products_raw_scan_max]
            # 상한 초과분은 화면 순서(배열 순서) 앞쪽만 취한다 — 좌표 해소가 이 순서에 의존하므로
            # dedup 은 하지 않는다(api-spec §3.1 지시어 해소).
            #
            # [12차 리뷰] **불량 항목을 먼저 거르고, 그다음 `products_max` 로 자른다** — 순서를
            # 반대로 하면(절단 먼저, 필터링 나중) 앞쪽 불량 항목이 상한 슬롯을 자릿수로 먹어
            # 배열 뒤쪽의 정상 상품이 불필요하게 잘려나간다(실제 재현: 불량 N건 + 정상
            # products_max 건이 앞뒤로 섞이면 정상 상품이 N건 밀려 잘린다). 정본 §3.1 유효성
            # 표의 "products 상한 초과 → 앞 products_max 건만 취하고 버림"은 "건"이 원본(raw)
            # 항목인지 유효 항목인지 명시하지 않는다 — 유효 항목으로 세는 편이 사용자에게
            # 유리하고(정상 상품이 덜 잘림) 계약 문면과도 어긋나지 않는다는 해석을 택했다.
            dropped_any_invalid_item = False
            for item in raw_products:
                if len(products) >= products_max:
                    break
                if not isinstance(item, Mapping):
                    dropped_any_invalid_item = True
                    continue
                product_id = _coerce_positive_int(item.get("productId", item.get("product_id")))
                if product_id is None:
                    dropped_any_invalid_item = True
                    continue
                name = item.get("name")
                products.append(
                    {
                        "productId": product_id,
                        "name": (
                            _clean_screen_text(name, text_max, text_raw_scan_max)
                            if isinstance(name, str)
                            else ""
                        ),
                    }
                )
            if dropped_any_invalid_item:
                # 이 인덱스 시프트 자체는 구조적으로 없앨 수 없다 — 서버는 FE 가 실제로 몇
                # 번째 자리에 그 상품을 그렸는지 알 방법이 없고, 순번·좌표 해소
                # (screen_reference.resolve_screen_reference)와 `grid_position` 은 여기 남은
                # 배열의 인덱스를 "화면 실제 위치"로 신뢰할 수밖에 없다(build_screen_prompt
                # 주석과 같은 전제). 되물음 강제·프롬프트 경고 주입 같은 대응은 과설계다 —
                # 정상 요청에서는 FE 가 애초에 불량 항목을 보내지 않으므로, 이 로그 한 줄로
                # "그 전제가 이번 요청에서 흔들렸다"만 관측 가능하게 남긴다.
                logger.warning("screen_products_contained_invalid_items")

        columns = v.get("columns")
        if isinstance(columns, bool) or not isinstance(columns, int) or columns < 1:
            columns = None

        raw_filters = v.get("filters")
        filters: dict[str, str] = {}
        if isinstance(raw_filters, Mapping):
            # [13차 리뷰] **`.items()` 로 원본을 순회하지 않고 허용 3키만 직접 조회한다.**
            # products 와 같은 문제였다 — 순회 방식이면 `raw_filters` 에 무효 키를 수만~수십만
            # 건 채운 요청이 매번 그 전체를 스캔한다. 허용 키가 `SCREEN_FILTER_KEYS` 3종으로
            # 고정돼 있으므로, `raw_filters` 가 아무리 커도 정확히 3회 `.get()` 만 하면 같은
            # 결과가 나온다 — products 처럼 별도 하드 상한을 둘 필요 없이 무계 순회 표면 자체가
            # 사라진다(이쪽이 products 보다 단순한 이유: 허용 집합이 유한하고 작다).
            for key in SCREEN_FILTER_KEYS:
                value = raw_filters.get(key)
                if isinstance(value, str):
                    # 정제 결과가 빈 문자열이면(제어문자만 온 경우) 키 자체를 버린다 — 프롬프트에
                    # `"sort": ""` 같은 빈 항목을 실어 봐야 의미가 없다.
                    if cleaned := _clean_screen_text(value, text_max, text_raw_scan_max):
                        filters[key] = cleaned

        return {
            "pageType": page_type,
            "filters": filters,
            "products": products,
            "columns": columns,
        }


class ConditionAction(CamelModel):
    """구매자 conditions 칩 제거 신호 (api-spec §3.1)."""

    op: Literal["remove"]
    field: Literal["category", "priceMax", "priceMin", "brand", "ratingMin", "keyword"]
    # [이슈 #434] 선택 — 스칼라(문자열/숫자). 없으면 그 field 전체 제거(하위호환, §3.1).
    # 있으면 멀티 값 축(category·brand)만 값 범위 제거를 시도하고, 나머지 4축은 값을 무시하고
    # 축 전체를 제거한다(app/agents/buyer/graph.py::_remove_condition_actions).
    value: str | int | float | None = None

    @field_validator("value")
    @classmethod
    def _limit_value_length(cls, v: str | int | float | None) -> str | int | float | None:
        """value 길이 상한을 config 에서 주입(하드코딩 금지). 초과 시 400(api-spec §3.1)."""
        if isinstance(v, str):
            from app.core.config import get_settings

            cap = get_settings().condition_action_value_max_chars
            if len(v) > cap:
                raise ValueError(f"value exceeds {cap} characters")
        return v


CONDITION_FIELD_TO_FILTER: dict[str, str] = {
    "category": "category",
    "priceMax": "price_max",
    "priceMin": "price_min",
    "brand": "brand",
    "ratingMin": "rating_min",
    "keyword": "keyword",
}


class BuyerChatRequest(ChatRequest):
    """POST /chat 구매자 요청 — 조건 칩 제거 액션을 선택으로 받는다."""

    condition_actions: list[ConditionAction] = Field(default_factory=list)
    # [2026-08-04] conditionActions 는 발화가 아닌 구조화 신호라 message 가 없을 수 있다.
    # 부모 ChatRequest 의 필수 message 를 선택(기본 "")으로 낮추고, 아래 validator 에서
    # 액션도 발화도 없는 요청만 거절해 기존 일반 발화 필수성은 유지한다.
    message: str = Field(
        default="",
        description="현재 턴 사용자 원문 질의. conditionActions가 있으면 비워도 된다.",
    )

    @model_validator(mode="after")
    def _validate_message_and_actions(self) -> "BuyerChatRequest":
        """중복 제거 액션을 정규화하고 빈 발화·빈 액션 요청은 400으로 거절한다.

        [이슈 #434] dedup 키는 (field, value) — 같은 축의 서로 다른 값 지정 제거는 둘 다
        남는다(§3.1 「값 지정 제거」). 단, 같은 축에 value 없는(전체 제거) 액션이 하나라도
        있으면 전체 제거가 상위집합이므로 그 축의 값 지정 액션은 흡수되고 전체 제거 1건만
        남는다(하위호환 — value 미전송 FE 는 종전과 100% 동일 동작).
        """
        seen: set[tuple[str, str | int | float | None]] = set()
        unique_actions: list[ConditionAction] = []
        for action in self.condition_actions:
            key = (action.field, action.value)
            if key in seen:
                continue
            seen.add(key)
            unique_actions.append(action)
        full_remove_fields = {action.field for action in unique_actions if action.value is None}
        self.condition_actions = [
            action
            for action in unique_actions
            if action.value is None or action.field not in full_remove_fields
        ]
        if not self.message.strip() and not self.condition_actions:
            raise ValueError("message 또는 conditionActions 가 필요합니다")
        return self


# ── SSE 이벤트 data 페이로드 모델 (api-spec §3.1 번호 이벤트 7종 — suggestions 는 §3.1 「MVP 추가 페이로드」 절이라 별도) ──


class ProgressData(CamelModel):
    """`progress` 이벤트 페이로드 (api-spec §3.1 (1), 이슈 #289 — 계약 등재 2026-08-05
    / 다회 emit·어휘 확장 이슈 #396 — api-spec §3.1 v0.27.0
    / retrying 추가 이슈 #406 — api-spec §3.1 v0.32.4).

    stage 확정 어휘는 8종(`analyzing`·`mapping`·`expanding`·`searching`·`retrying`·`relaxing`·
    `reranking`·`publishing`)이며 개방형(open set) — FE 는 모르는 값을 무시하고 오류로 다루지
    않는다. 그래도 서버 쪽은 `Literal` 로 강제한다 — 어휘를 넓히려면 계약(§3.1) 개정과 이
    `Literal` 을 함께 고친다.
    message 는 선택 — 서버가 비우면 와이어에서 키 자체가 빠진다(`app/agents/buyer/_frames.py`).
    """

    stage: Literal[
        "analyzing",
        "mapping",
        "expanding",
        "searching",
        "retrying",
        "relaxing",
        "reranking",
        "publishing",
    ]
    message: str | None = None


class TokenData(CamelModel):
    """`token` 이벤트 — 근거/코멘트 토큰 증분 (구 text.delta)."""

    text: str


class ConditionChip(CamelModel):
    """`conditions` 칩 1건 — FE 제거 가능한 추출 조건 (api-spec §3.1 (3))."""

    field: str
    label: str
    value: Any


class ConditionsData(CamelModel):
    """`conditions` 이벤트 — 추출 필터 조건 칩 목록 (0~1회)."""

    chips: list[ConditionChip] = Field(default_factory=list)


class RevertRef(CamelModel):
    """`revert` — 구매 이력 억제 되돌리기 대상 카테고리 (결정 14-F)."""

    category: str


class RelaxationRef(CamelModel):
    """`relaxation` — 0건 완화 제안 대상 (결정 14-D)."""

    field: str
    value: Any


class SuggestionChip(CamelModel):
    """`suggestions` 칩 1건 — 완화(relaxation) 또는 되돌리기(revert). estCount==0 은 제외(§3.1)."""

    label: str
    revert: RevertRef | None = None
    relaxation: RelaxationRef | None = None
    est_count: int  # 재포함/완화 적용 시 예상 결과 수(COUNT)

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> "SuggestionChip":
        """§3.1 — 칩 1건은 relaxation 또는 revert 중 **정확히 하나**여야 한다."""
        if (self.revert is None) == (self.relaxation is None):
            raise ValueError(
                "SuggestionChip 은 revert 또는 relaxation 중 정확히 하나여야 한다(§3.1)"
            )
        return self


class SuggestionsData(CamelModel):
    """`suggestions` 이벤트 — 완화·되돌리기 제안 칩 목록 (api-spec §3.1)."""

    chips: list[SuggestionChip] = Field(default_factory=list)


class ActionData(CamelModel):
    """`action` 이벤트 — 장바구니 담기 결과 (api-spec §3.1 (4)).

    type: CART_ADDED | CART_ADD_FAILED.
    reason(실패 시): PRODUCT_NOT_FOUND | STOCK_INSUFFICIENT | CART_ERROR (STOCK_INSUFFICIENT는
    BE I-2 CART_STOCK_INSUFFICIENT + availableStock 매핑, 2026-07-22 신설 — 남은 재고 수 안내.
    OUT_OF_STOCK 폐기: 품절=0 표현이라 'N개 남음'과 불일치, CH-2).
    GUEST_NOT_ALLOWED 폐기 — 게스트 담기 허용(결정 8 개정). 옵션 되물음(CART_OPTION_REQUIRED)은
    실패 action 이 아니라 token 재질문 멀티턴으로 처리한다(api-spec §3.1·§4.1).

    [이슈 #116·#117] `CART_REMOVED`·`CART_REMOVE_FAILED`·`WISHLIST_ADDED`·`WISHLIST_ADD_FAILED`·
    `WISHLIST_REMOVED`·`WISHLIST_REMOVE_FAILED`, reason `WISHLIST_ERROR` 는 **확정 2026-08-05**
    (정본 CH-2 등재 완료, I-24~I-28 — `docs/api-spec.md` §3.1 v0.22.0에 반영됨). **[라운드 23]**
    삭제·찜 흐름의 온/오프를 가리던 두 설정 필드를 제거했다 — 이제 항상 emit 된다.

    [#285] `CART_QUANTITY_CHANGED`·`CART_QUANTITY_CHANGE_FAILED` 는 §3.1 이 v0.22.0 에서 이미
    사전 등재해 둔 어휘다(**계약 신설이 아니라 구현이 계약을 따라잡는 것**, I-25/§4.13) — AI 가
    이제 emit 한다. `quantity` 는 이 두 type 전용(성공 시 최종 수량) — `cart_item_id` 는 기존
    필드를 그대로 재사용한다.
    """

    type: Literal[
        "CART_ADDED",
        "CART_ADD_FAILED",
        "CART_REMOVED",
        "CART_REMOVE_FAILED",
        "WISHLIST_ADDED",
        "WISHLIST_ADD_FAILED",
        "WISHLIST_REMOVED",
        "WISHLIST_REMOVE_FAILED",
        "CART_QUANTITY_CHANGED",
        "CART_QUANTITY_CHANGE_FAILED",
    ]
    message: str
    # [확정 2026-08-05] cartItemId 는 number(BIGINT) — 삭제 확장안이 제안했던 문자열 표기는
    # 채택되지 않았다. 정본 CH-2·FE 타입(`ChatAction`)·이 필드(CART_ADDED 가 이미 쓰는 int, FE
    # 사용 중) 셋 다 number 로 확정돼, CART_REMOVED 도 이 필드를 그대로 재사용한다(§2.6·
    # docs/api-spec.md §3.1).
    cart_item_id: int | None = None  # 숫자(BIGINT, cart_item.id)
    # [#285] CART_QUANTITY_CHANGED/CART_QUANTITY_CHANGE_FAILED 전용 — 성공 시 BE 가 반영한
    # 최종 수량(§3.1·§4.13). 다른 type 은 이 필드를 쓰지 않는다.
    quantity: int | None = None
    reason: (
        Literal["STOCK_INSUFFICIENT", "PRODUCT_NOT_FOUND", "CART_ERROR", "WISHLIST_ERROR"] | None
    ) = None


class ProductsReadyData(CamelModel):
    """`products.ready` 이벤트 — 목록 push 성공 상관관계 키 (정확히 1회, 카드 없음).

    [HARD] 상품 카드는 싣지 않는다. FE 는 최대 10개의 목록 키로 Spring 목록
    GET(§4.3)을 호출한다. 단일 목록도 배열로 직렬화한다.
    """

    session_id: str
    list_ids: list[str] = Field(..., min_length=1, max_length=10)


class DoneData(CamelModel):
    """`done` 이벤트 — 정상 종료. finishReason: stop | zero_result (api-spec §3.1 (6)).

    [#113] `relaxationNotice` 는 **싣지 않는다.** 정본(Notion CH-2)이 `done` 을 `finishReason` 만으로
    확정했고("구 명세 대비 정정 요약: done — relaxationNotice 제거"), FE 타입도 그 필드를 갖고 있지
    않다(jarvis-frontend `shared/types/chat.ts`). 자동 완화 투명 안내(REQ-REC-042)는 `token` 산문이
    담당하므로 사용자 고지는 그대로 유지된다 — 없어지는 건 아무도 읽지 않는 기계 판독 사본뿐이다.
    FE 가 문장 파싱 없이 분기할 근거가 필요해지면 그때 정본을 개정하고 되살린다.
    """

    finish_reason: Literal["stop", "zero_result"] = "stop"


class ErrorData(CamelModel):
    """`error` 이벤트 — 스트림 내부 오류 (api-spec §3.1 (7)).

    code 4종: LLM_TIMEOUT | LLM_UNAVAILABLE | SEARCH_FAILED | INTERNAL.
    스테이지 상세(decompose/rerank)는 서버 로그 전용 — 사용자 스트림 미노출.
    """

    code: Literal["LLM_TIMEOUT", "LLM_UNAVAILABLE", "SEARCH_FAILED", "INTERNAL"]
    message: str
    request_id: str = Field(..., min_length=1)
    retryable: bool
