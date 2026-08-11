"""판매자 상품 쓰기 HITL 실행 (4-2 — SPEC-SELLER-001 §6, api-spec §3.2 2-스트림).

설계 확정(2026-07-20 사용자 결정 3건):
1. **실행 주체 = 코드**: confirm resume 시 checkpoint 의 draft 를 코드가 그대로
   I-10/11/12(§4.5)에 매핑한다 — "보여준 diff == 실행하는 쓰기"(안전장치 ①)를
   구조로 보장하고, 실행 시점 LLM 은 0회다. 강의(06_Middleware/02-HITL-V1)의
   interrupt/Command(resume) 개념은 그대로 쓰되 HumanInTheLoopMiddleware(LLM
   도구 호출 재개)는 채택하지 않는다 — 실행 인자가 LLM 산물이면 안전장치 ①이
   프롬프트 품질에 걸리기 때문.
2. **checkpointer = AsyncPostgresSaver(pg-profile)**, dev 폴백 허용: 연결 실패 시
   InMemorySaver + 경고 1회(service_token dev 스킵 선례). 운영(auth_mode=jwks)은
   폴백 금지 — 프로세스 재시작 시 draft 증발은 운영에서 허용 불가.
   [이관] 싱글턴은 공용 모듈 checkpoint.py 소관 — 채팅 대화 스레드(thread.py,
   `seller-chat:` 접두)와 같은 저장소를 나눠 쓴다. set_checkpointer 는 재수출.
3. **stale 검증(S-5 병존, REALIGN F7) = stock 제외 비교**: confirm 시점 I-9 재조회로
   changes[].before 를 재검증하되 stock_quantity 는 주문 재고 차감(F6)으로 자연
   변동하므로 제외한다. stock 변동은 실행 결과 안내에 현재값 표기로 보완.

안전장치 5종(§6.2) 구현 지점:
① draftId 바인딩 — thread_id=f"seller-draft:{draftId}", checkpoint 가 정제된 실행 정본 보유.
   SSE diff 는 같은 정본에서 만들되 시크릿 형태만 표시 계층에서 마스킹한다.
② 명시 액션만 — confirm 판정은 요청 스키마 최상위 action/draftId 필드(입구 ①, A-2).
③ 멱등성 — 실행 완료 스레드(result 보유) 재confirm 은 재실행 없이 안내만.
④ Spring 소유권 하드게이트 — brandId 불일치 confirm 은 존재 비노출 거절(+Spring 최종 방어).
⑤ 대기 TTL — created_at 기준 seller_draft_ttl_minutes 경과 시 만료 안내(실행 금지).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.agents.seller import category_catalog
from app.agents.seller import checkpoint as seller_checkpoint
from app.agents.seller import analysis_store
from app.agents.seller.schemas import DraftChange, DraftProposal
from app.agents.seller.stock_options import resolve_stock_option
from app.core.config import get_settings
from app.core.text import _strip_unsafe, _strip_unsafe_multiline
from app.core.tracing import trace_span
from app.schemas.spring import (
    OrderItemStatusUpdate,
    ProductCreate,
    ProductUpdate,
    SellerProductRow,
    StockEntry,
)
from app.services.spring_client import (
    InvalidPrice,
    InvalidStock,
    OrderAlreadyShipped,
    OrderInvalidTransition,
    OrderItemNotFound,
    ProductAlreadyDeleted,
    ProductCategoryInvalid,
    ProductDeletedNotEditable,
    ProductFieldMissing,
    get_spring_client,
)

logger = logging.getLogger(__name__)

# ── draft 정규화 (스트림 1 — emit 전에 실행 가능성을 코드로 선검증) ──────────────

# after(str) → 정수 캐스팅 대상 필드(ProposedChange "수치도 문자열" 계약의 역변환 지점).
_INT_FIELDS = frozenset({"price", "original_price", "stock_quantity"})
# I-10 등록·I-11 수정으로 지정할 수 있는 status. 삭제(`DELETED`)는 **I-12 전용 전이**라
# 여기 넣지 않는다 — 넣으면 update 본문으로 새어나가 BE 가 400 으로 막는다(api-spec §4.5).
_UPDATE_STATUS_VALUES = frozenset({"ON_SALE", "HIDDEN"})
# delete draft 의 표시용 after — I-12 는 본문이 없어 실행에 실리지 않는다(diff 카드 전용).
_DELETE_STATUS_VALUE = "DELETED"
# 도구 출력("가격 15,000원 재고 100건")을 옮겨적은 값 관용 처리용 단위 접미사.
_INT_SUFFIXES = ("원", "건", "개")
# status 는 create 지정 불가 — I-10 이 ON_SALE 로 발급한다.
# [#506] 구 금지 필드 image_url 은 해제 — 이미지 기반 등록 초안이 canonical URL 을
# 싣는다(검증은 validate_draft: http(s) + seller_image_url_max_len ≤ 500, DB VARCHAR).
_CREATE_FORBIDDEN_FIELDS = frozenset({"status"})
# I-10 필수 본문(api-spec §4.5) — 누락 draft 는 등록 자체가 불가하므로 되묻기.
# [2026-08-09] category 추가 — BE `SellerProductCreateRequest.categoryId` 가 필수다.
# 없이 보내면 422(MISSING_FIELD)로 되돌아오는데, 그때는 이미 판매자가 승인 버튼을
# 누른 뒤라 "등록 중 오류"로만 보인다. 초안 단계에서 막아야 되묻기가 가능하다.
_CREATE_REQUIRED_FIELDS = frozenset({"name", "price", "stock_quantity", "category"})
# 되묻기·안내 문구는 필드마다 다르다 — 영문 필드명을 그대로 노출하면 판매자가 못
# 알아본다. [#620] create 되묻기 전용이던 것을 stale 불일치 안내(find_stale_changes)
# 등 update 경로에서도 쓰도록 ProductField 8종 전체로 넓혔다.
_FIELD_LABELS = {
    "name": "상품명",
    "price": "가격",
    "original_price": "정가",
    "stock_quantity": "재고 수량",
    "category": "카테고리",
    "description": "상품 설명",
    "image_url": "상품 이미지",
    "status": "판매 상태",
}
# [#297] I-30 MVP 허용 전이는 ORDERED→SHIPPING 하나뿐(§4.19) — toStatus 를 코드가
# 고정한다(LLM 산물 아님). 전이 어휘가 늘면 여기와 validate_draft 만 확장한다.
_SHIP_TO_STATUS = "SHIPPING"


class DraftRecord(BaseModel):
    """checkpoint 에 저장되는 draft 원본 — 실행(confirm resume)이 참조하는 유일한 정본.

    DraftProposal(LLM 출력)과 달리 draftId·신원·created_at 은 **코드가 발급**한다
    (ReportScore.total 과 같은 '계약값은 코드' 원칙). brand_id 는 confirm 시점
    요청 신원과 대조해 타 판매자의 draftId 추측 승인을 차단한다(안전장치 ④ 보강).
    """

    draft_id: str
    op: Literal["create", "update", "delete", "ship"]
    product_id: int | None = None
    # [#297] ship(I-30 발송) 전용 — 대상 주문 아이템. 그 외 op 는 None.
    order_item_id: int | None = None
    changes: list[DraftChange] = Field(default_factory=list)
    summary: str = ""
    seller_id: int  # JWT sub — 숫자 계약(api-spec §2.6), SellerContext 와 동일 타입
    brand_id: int  # JWT brandId 클레임 — 숫자 계약, SellerContext 와 동일 타입
    created_at: str  # ISO8601(UTC) — TTL(안전장치 ⑤) 판정 기준
    # [이슈 #590] DraftProposal.rec_id 그대로 승계 — hitl._hitl_node 가 실행 성공 시
    # analysis_store.mark_recommendation_applied 로 되돌려 쓴다. 일반 채팅 draft(추천
    # 경로가 아닌)는 None.
    rec_id: str | None = None


def _parse_int(raw: str) -> int:
    """draft 의 문자열 수치를 정수로 — 콤마·공백·단위 접미사 관용, 그 외 실패는 ValueError."""
    text = raw.strip().replace(",", "").replace(" ", "")
    for suffix in _INT_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if not text.isdigit():  # 가격·재고는 음수 없음 — 부호 미허용
        raise ValueError(f"정수로 해석할 수 없습니다: {raw!r}")
    return int(text)


def _typed_after(change: DraftChange, *, op: str = "update") -> int | str:
    """changes[].after(str) → I-10/11 본문 타입. 실패는 ValueError 전파(호출부 되묻기).

    status 허용값이 op 별로 갈린다: 삭제 초안은 `DELETED` 뿐(표시용 — I-12 는 본문이 없다)
    이고 등록·수정은 `ON_SALE`/`HIDDEN` 뿐이다. 한 집합으로 합치면 삭제 값이 I-10·I-11
    본문으로 새어나가 BE 가 400 으로 막는 요청을 보내게 된다(api-spec §4.5).
    """
    if change.field in _INT_FIELDS:
        return _parse_int(change.after)
    if change.field == "status":
        value = change.after.strip()
        allowed = {_DELETE_STATUS_VALUE} if op == "delete" else _UPDATE_STATUS_VALUES
        if value not in allowed:
            allowed_text = _DELETE_STATUS_VALUE if op == "delete" else "ON_SALE/HIDDEN"
            raise ValueError(f"status 는 {allowed_text} 만 가능합니다: {change.after!r}")
        return value
    return change.after


def validate_draft(
    proposal: DraftProposal,
    *,
    seller_id: int,
    brand_id: int,
    row: SellerProductRow | None = None,
) -> tuple[DraftRecord | None, str | None]:
    """DraftProposal(LLM) → DraftRecord(실행 정본). 불성립은 (None, 되묻기 문구).

    emit 전에 검증하는 이유: 실행 불가능한 draft 를 FE diff 카드로 보여주고 confirm
    받은 뒤에야 실패하는 것보다, 스트림 1에서 되묻는 쪽이 계약(보여준 것==실행)에
    부합한다. 여기서 통과한 draft 는 confirm 시점에 캐스팅이 실패하지 않는다.

    `row`(#620, 선택) — op="update" 에서 price/original_price 를 건드릴 때만 호출부가
    미리 조회해 넘긴다(추가 Spring 왕복, `api.seller`·`history.apply_recommendation`
    경유). 있으면 BE `validatePriceRange` 와 같은 규칙(price ≤ originalPrice, 생략
    필드는 저장된 값)을 여기서 미리 계산해 **카드를 보여주기 전에** 되묻는다 —
    없으면(레거시 호출부·다른 필드만 바꾸는 update) 이 검사는 건너뛰고 confirm 시점
    BE 422(`InvalidPrice`)에 맡긴다(레이스만 남는 안전망, `_execute_draft` 참조)."""
    # [#297] ship(I-30 발송): 대상은 orderItemId 하나뿐이고 전이는 코드 고정
    # (ORDERED→SHIPPING)이라 상품 필드 검증·changes 캐스팅이 적용되지 않는다.
    # changes 는 LLM 산물을 버리고 빈 목록으로 정규화한다 — 카드 표시는 summary 가,
    # 실행 인자는 order_item_id 가 전부다(보여준 것==실행하는 것).
    if proposal.op == "ship":
        if proposal.order_item_id is None:
            return None, (
                "발송할 주문 아이템을 특정하지 못했습니다. 주문 번호나 상품명을 "
                "알려주시면 주문을 조회해 대상을 확정하겠습니다."
            )
        record = DraftRecord(
            draft_id=str(uuid.uuid4()),
            op="ship",
            product_id=None,
            order_item_id=proposal.order_item_id,
            changes=[],
            summary=_strip_unsafe(proposal.summary),
            seller_id=seller_id,
            brand_id=brand_id,
            created_at=datetime.now(UTC).isoformat(),
            rec_id=proposal.rec_id,
        )
        return record, None

    # after 는 승인 후 Spring 쓰기의 실행 정본이므로 위험 문자만 제거한다.
    # 시크릿 마스킹은 표시 계층 전용이며 여기에 적용하면 정상 상품 데이터가 오염된다.
    changes = [
        change.model_copy(
            update={
                "after": (
                    _strip_unsafe_multiline(change.after)
                    if change.field == "description"
                    else _strip_unsafe(change.after)
                ),
                # [#524] option_name 은 재고(stock_quantity) 변경에만 유효한 LLM 필드 —
                # 다른 필드에 실려 오면 잡음이므로 버린다. option_id 는 코드 전용이라
                # LLM 산물을 신뢰하지 않고 항상 비운다(실행 시점에 I-9 로 해소).
                "option_name": (
                    _strip_unsafe(change.option_name)
                    if change.field == "stock_quantity" and change.option_name
                    else None
                ),
                "option_id": None,
            }
        )
        for change in proposal.changes
    ]

    # [#620 Q2] delete 초안은 status(DELETED) 한 건 외에는 카드에 실을 게 없다 — LLM 이
    # PRODUCT_PROMPT 지시대로 status 변경을 실었으면 그대로 두고(정상 경로, 승인 카드에
    # before→DELETED 표시), 없으면 ship 과 같은 방식으로 changes 를 통째로 비운다.
    # I-9 재조회로 placeholder 를 합성하는 대신 단순 정규화를 택했다(선택지 검토 결과).
    if proposal.op == "delete":
        changes = [c for c in changes if c.field == "status"]

    fields = {c.field for c in changes}

    # [#620] 같은 필드가 changes 에 두 번 실리면 어느 값이 정본인지 판단할 수 없다 —
    # 나중 것이 조용히 이기게 두지 않고 여기서 되묻는다. stock_quantity 는 제외한다 —
    # option_name 이 다른 표기(예: "블랙" vs "블랙/M")로 같은 옵션을 가리킬 수 있어
    # 문자열 비교로는 판단할 수 없고, resolve_stock_option 으로 실제 옵션을 특정한
    # **뒤에**(`_build_update_patch`, confirm 시점) 중복을 걸러야 정확하다 — 그 경로가
    # 이미 옵션명을 밝힌 되묻기 메시지를 낸다.
    seen_fields: set[str] = set()
    for change in changes:
        if change.field == "stock_quantity":
            continue
        if change.field in seen_fields:
            label = _FIELD_LABELS.get(change.field, change.field)
            return None, (
                f"'{label}' 변경이 초안에 중복으로 실려 있어 어느 값을 반영할지 "
                "판단하지 못했습니다. 바꿀 내용을 다시 한 번 말씀해 주세요."
            )
        seen_fields.add(change.field)

    if proposal.op in ("update", "delete") and proposal.product_id is None:
        return None, (
            "어느 상품을 변경할지 특정하지 못했습니다. 상품명이나 상품 번호를 알려주세요."
        )
    if proposal.op == "update" and not changes:
        return None, "무엇을 어떻게 바꿀지 파악하지 못했습니다. 변경할 내용을 알려주세요."
    if proposal.op == "update" and "category" in fields:
        # [#620] ProductUpdate 스키마에 애초에 category 필드가 없다(BE DTO 에도 없음,
        # 2026-08-09 실측) — 여기서 막지 않으면 카드에는 "카테고리 변경"이 보이는데
        # confirm 해도 조용히 무시되는(보여준 것≠실행하는 것) 상황이 된다.
        return None, (
            "카테고리는 상품 수정으로 바꿀 수 없습니다 — 등록 시에만 정할 수 있는 "
            "값입니다. 카테고리를 바꾸려면 상품을 새로 등록해 주세요."
        )
    name_change = next((c for c in changes if c.field == "name"), None)
    if name_change is not None:
        name_max_len = get_settings().seller_name_max_len
        if len(name_change.after) > name_max_len:
            # [#620] BE SellerProductCreateRequest/UpdateRequest 의 @Size(max=200) 2차
            # 방어 — 초과분은 BE 400 VALIDATION_ERROR(미매핑 → "일시적 오류")로 새던 것을
            # 여기서 선차단한다.
            return None, (
                f"상품명이 {name_max_len}자를 초과해 저장할 수 없습니다. "
                "짧게 줄여 다시 말씀해 주세요."
            )
    if proposal.op == "update" and row is not None:
        price_change = next((c for c in changes if c.field == "price"), None)
        original_price_change = next((c for c in changes if c.field == "original_price"), None)
        if price_change is not None or original_price_change is not None:
            try:
                effective_price = (
                    _parse_int(price_change.after) if price_change is not None else row.price
                )
                effective_original_price = (
                    _parse_int(original_price_change.after)
                    if original_price_change is not None
                    else row.original_price
                )
            except ValueError:
                # 캐스팅 실패는 아래 공용 _typed_after 루프가 되묻는다 — 여기선 건너뛴다.
                effective_price = effective_original_price = None
            if (
                effective_price is not None
                and effective_original_price is not None
                and effective_price > effective_original_price
            ):
                # [#620] BE validatePriceRange 와 동일 규칙(price ≤ originalPrice, 생략
                # 필드는 저장된 값)을 카드 표시 전에 미리 계산 — confirm 후 422 로 처음
                # 알리는 대신 여기서 되묻는다("선차단").
                return None, (
                    f"판매가({effective_price:,}원)가 정가({effective_original_price:,}원)를 "
                    "넘어 저장할 수 없습니다. 가격을 다시 확인해 말씀해 주세요."
                )
    if proposal.op == "create":
        forbidden = fields & _CREATE_FORBIDDEN_FIELDS
        if forbidden:
            return None, (
                "상품 등록 시에는 상태(status)를 함께 지정할 수 없습니다 — 등록되면 "
                "판매중(ON_SALE)으로 시작합니다. 숨김이 필요하면 등록 후 수정으로 "
                "요청해 주세요."
            )
        missing = _CREATE_REQUIRED_FIELDS - fields
        if missing:
            labels = [_FIELD_LABELS.get(field, field) for field in sorted(missing)]
            if missing == {"category"}:
                # 카테고리만 비었으면 판매자가 다시 적을 것은 카테고리뿐이다 —
                # 상품명·가격까지 되물으면 이미 말한 값을 또 입력하게 된다.
                return None, (
                    "어느 카테고리에 등록할지 확정하지 못했습니다. "
                    "'남성 셔츠', '강아지 사료'처럼 상품 종류를 알려주시면 "
                    "카테고리를 찾아 초안에 반영하겠습니다."
                )
            return None, (
                "상품 등록에는 상품명·가격·재고 수량·카테고리가 필요합니다. "
                f"누락된 항목({', '.join(labels)})을 알려주세요."
            )
        # [#620] BE 는 originalPrice 생략 시 price 로 채운 뒤(동일값 → 무할인) 비교하므로
        # (SellerProductService.create), 여기서 명시된 값끼리만 비교해도 규칙이 같다 —
        # update 와 달리 create 는 partial 이 아니라 이 draft 의 changes 가 전체 값이라
        # row 조회 없이 판단 가능하다.
        create_price_change = next((c for c in changes if c.field == "price"), None)
        create_original_price_change = next(
            (c for c in changes if c.field == "original_price"), None
        )
        if create_price_change is not None and create_original_price_change is not None:
            try:
                create_price = _parse_int(create_price_change.after)
                create_original_price = _parse_int(create_original_price_change.after)
            except ValueError:
                create_price = create_original_price = None
            if (
                create_price is not None
                and create_original_price is not None
                and create_price > create_original_price
            ):
                return None, (
                    f"판매가({create_price:,}원)가 정가({create_original_price:,}원)를 "
                    "넘어 등록할 수 없습니다. 가격을 다시 확인해 말씀해 주세요."
                )
        # [#506] image_url 값 검증 — 2차 방어(1차는 요청 스키마·FE 서버 라우트).
        # presigned URL 이 저장되면 만료 시점에 상품 이미지가 조용히 죽는다(FE 계약 §2.3).
        image_url = next((c.after for c in changes if c.field == "image_url"), None)
        if image_url is not None:
            settings = get_settings()
            if not image_url.startswith(("https://", "http://")):
                return None, "상품 이미지 URL 형식이 올바르지 않습니다. 사진을 다시 첨부해 주세요."
            if len(image_url) > settings.seller_image_url_max_len or "X-Amz-Signature" in image_url:
                return None, (
                    "상품 이미지 URL 이 저장 가능한 형식이 아닙니다(길이 초과 또는 만료형 URL). "
                    "사진을 다시 첨부해 주세요."
                )
        # [#506] category 는 카테고리 스냅샷의 id 여야 한다 — LLM 은 주입된 후보 중에서만
        # 고르지만(프롬프트), 목록 밖 값·자유 문자열은 여기서 되묻기로 전환한다(이중 방어).
        # update 의 category 는 위(공통 검증부)에서 이미 통째로 차단되므로 여기 도달하지
        # 않는다 — 이 검증은 create 전용이다(#620).
        category_id = next((c.after for c in changes if c.field == "category"), None)
        if category_id is not None and category_catalog.get(category_id) is None:
            return None, (
                "카테고리를 확정하지 못했습니다. 원하시는 카테고리를 알려주시면 "
                "후보를 다시 찾아 초안에 반영하겠습니다."
            )
    for change in changes:
        try:
            _typed_after(change, op=proposal.op)
        except ValueError:
            return None, (
                f"'{change.field}' 값 '{change.after}' 을(를) 해석하지 못했습니다. "
                "값을 다시 확인해 주세요."
            )

    record = DraftRecord(
        draft_id=str(uuid.uuid4()),
        op=proposal.op,
        product_id=None if proposal.op == "create" else proposal.product_id,
        changes=changes,
        summary=_strip_unsafe(proposal.summary),
        seller_id=seller_id,
        brand_id=brand_id,
        created_at=datetime.now(UTC).isoformat(),
        rec_id=proposal.rec_id,
    )
    return record, None


# [#524] stocks 모드 전제가 서버에 없을 때의 안내 — 판매자에게는 "지금은 안 된다"까지만
# 알린다(설정·배포 사정은 내부 사정이라 표면에 내지 않는다).
_STOCKS_MODE_NOT_READY = (
    "재고 시스템 전환이 서버에 아직 반영되지 않아 재고 변경을 중단했습니다. "
    "가격·설명 등 다른 항목은 정상 반영됩니다."
)


def _stocks_mode_ready(row: SellerProductRow) -> bool:
    """stocks 모드 전제 확인 — I-9 응답이 신 계약을 아는지 본다 (#524).

    PR B 이후 I-9 는 **옵션 없는 상품에도 `optionId: null` 한 줄**을 반드시 내린다
    (BE 04 §I-9). 그래서 빈 배열은 "옵션이 없다"가 아니라 **"BE 가 아직 구버전"** 이다.

    이 확인 없이 설정만 믿으면 조용한 부분 실패가 난다: 구 BE 에 `stocks` 를 보내면
    Jackson 이 모르는 키를 버리고, 같은 본문의 price 는 반영되므로 **재고만 안 바뀐 채
    "반영했습니다"** 가 나간다. #506 카테고리 사고(`category` 키를 버려 등록이 실패하던
    것)와 같은 유형이고, 이쪽은 실패가 아니라 부분 성공이라 더 안 보인다.
    """
    return bool(row.stocks)


def _build_update_patch(
    changes: list[DraftChange], row: SellerProductRow
) -> tuple[ProductUpdate | None, str | None]:
    """I-11 PATCH 본문 조립 (#524 듀얼모드) — (patch, 되묻기 문구) 중 정확히 한쪽.

    quantity 모드(현행 BE): 구 계약 그대로 stock_quantity 정수 하나. 이 모드에서
    옵션 지정 재고 발화(option_name)나 재고 change 복수 건은 반영할 곳이 없다 —
    조용히 합계로 뭉개면 "보여준 것 ≠ 실행하는 것"이므로 반영하지 않고 안내한다.

    stocks 모드(PR B 이후): 재고 change 마다 option_name 을 confirm 시점의 I-9
    stocks 로 해소해 stocks[{optionId, quantity}] 부분 수정으로 보낸다. 배열에 실린
    옵션만 갱신된다(05 §I-11). 해소 실패·중복 옵션은 실행하지 않고 되묻는다 —
    INVALID_STOCK 422 를 승인 이후에 받는 것보다 여기서 멈추는 편이 낫다.

    stocks 모드는 **BE 가 실제로 그 계약을 아는지**를 I-9 응답으로 확인하고 나서야
    쓴다(_stocks_mode_ready) — 설정만 믿으면 조용한 부분 실패가 난다.
    """
    stock_changes = [c for c in changes if c.field == "stock_quantity"]
    other_fields = {c.field: _typed_after(c) for c in changes if c.field != "stock_quantity"}

    if get_settings().seller_stock_wire_mode != "stocks":
        if any(c.option_name for c in stock_changes) or len(stock_changes) > 1:
            return None, (
                "옵션별 재고 반영은 재고 시스템 전환 후에 열립니다. 지금은 상품 전체 "
                "재고 수량 하나만 바꿀 수 있어 반영을 중단했습니다."
            )
        if stock_changes:
            other_fields["stock_quantity"] = _typed_after(stock_changes[0])
        return ProductUpdate(**other_fields), None

    if not stock_changes:
        # 재고를 안 건드리는 update 는 stocks 계약과 무관하다 — 가드 대상이 아니다.
        return ProductUpdate(**other_fields), None

    if not _stocks_mode_ready(row):
        return None, _STOCKS_MODE_NOT_READY

    entries: list[StockEntry] = []
    seen: set[int | None] = set()
    for change in stock_changes:
        matched, problem = resolve_stock_option(change.option_name, row.stocks)
        if problem is not None:
            return None, problem
        option_id = matched.option_id if matched is not None else None
        if option_id in seen:
            return None, (
                "같은 옵션의 재고 변경이 초안에 두 번 실려 있어 반영을 중단했습니다. "
                "옵션별로 한 번씩만 말씀해 주세요."
            )
        seen.add(option_id)
        entries.append(StockEntry(option_id=option_id, quantity=int(_typed_after(change))))
    return ProductUpdate(**other_fields, stocks=entries), None


# ── stale 검증 (S-5 병존 — confirm 시점 I-9 재조회 대조) ─────────────────────────

# 주문 재고 차감(F6)으로 판매자 행위 없이 변하는 필드 — 비교 제외(2026-07-20 확정).
_STALE_EXEMPT_FIELDS = frozenset({"stock_quantity"})


def find_stale_changes(
    row: SellerProductRow, changes: list[DraftChange], *, op: str = "update"
) -> list[tuple[str, str, str]]:
    """draft.changes 의 before 를 현재 상품값과 대조 — 불일치 (field, before, current) 목록.

    int 필드는 표기 차이("15,000" vs "15000")로 인한 오탐을 막기 위해 정수 비교,
    문자열 필드는 strip 후 비교한다. stock_quantity 는 제외(모듈 docstring 3).

    **삭제(op="delete")는 `status` 를 비교하지 않는다** — `ON_SALE`·`HIDDEN` 어느 쪽에서든
    `DELETED` 로 가는 것이 정상 전이라(api-spec §4.5) 초안 작성 후 판매자가 상품을 숨겼다는
    이유로 삭제를 막을 근거가 없다. 비교하면 BE 가 열어준 "숨겼다가 나중에 지운다" 흐름이
    AI 쪽에서 다시 막힌다(구 계약의 409 `ALREADY_HIDDEN` 이 만들던 것과 같은 증상).
    """
    mismatches: list[tuple[str, str, str]] = []
    for change in changes:
        if change.field in _STALE_EXEMPT_FIELDS:
            continue
        if op == "delete" and change.field == "status":
            continue
        current = getattr(row, change.field, None)
        current_str = "" if current is None else str(current)
        if change.field in _INT_FIELDS:
            try:
                before_val: int | None = (
                    _parse_int(change.before) if change.before.strip() else None
                )
            except ValueError:
                before_val = None
            if before_val != current:
                mismatches.append((change.field, change.before, current_str))
        elif change.before.strip() != current_str.strip():
            mismatches.append((change.field, change.before, current_str))
    return mismatches


async def _find_product(brand_id: int, product_id: int) -> SellerProductRow | None:
    """I-9 목록에서 대상 상품 행을 찾는다 — productId 필터가 없어 페이지 순회.

    페이지 크기·상한은 Settings 주입(seller_list_default_limit·
    seller_draft_lookup_max_pages). 상한 내 미발견은 None(삭제/미귀속 가능성) —
    호출부가 stale 로 처리한다. Spring 장애는 SpringUnavailableError 전파(재시도 가능).
    """
    settings = get_settings()
    page_size = settings.seller_list_default_limit
    offset = 0
    for _ in range(settings.seller_draft_lookup_max_pages):
        result = await get_spring_client().list_products(brand_id, None, None, page_size, offset)
        for row in result.rows:
            if row.product_id == product_id:
                return row
        if len(result.rows) < page_size:
            return None
        offset += page_size
    return None


# ── 실행 (confirm resume 후 — LLM 0회, draft 그대로 I-10/11/12 매핑) ────────────

_STALE_RETRY_GUIDE = (
    "변경을 원하시면 다시 요청해 주세요. 최신 값으로 새 초안을 만들어 드리겠습니다."
)


async def _execute_draft(record: DraftRecord) -> tuple[str, str]:
    """draft 를 op 별 Spring 쓰기에 매핑 — (outcome, 사용자 안내 text) 반환.

    outcome: "executed"(반영 완료) | "stale"(불일치/미발견 — 실행 중단, 되묻기)
    | "already_done"(ship 409 — 이미 발송됨, 재실행 없음). Spring 장애
    (SpringUnavailableError)는 잡지 않는다 — 노드 예외로 전파되면 checkpoint 가
    interrupt 지점에 남아 동일 draftId 로 재confirm 이 가능하다.
    """
    client = get_spring_client()

    # [#297] ship — I-30 발송 처리(§4.19). 상품 재조회(stale 검증) 대상이 아니다:
    # 소유권·현재 상태 검증은 Spring 이 실행 시점에 재수행한다(타사=404 존재 은닉).
    # 4xx 는 코드별 결과로 정규화해 스레드에 기록한다(멱등 안내 가능) — 500·타임아웃만
    # 예외 전파로 재confirm 여지를 남긴다(성공 보고 금지, I-11·I-12 규칙).
    if record.op == "ship":
        assert record.order_item_id is not None  # validate_draft 가 보장
        try:
            shipped = await client.update_order_item_status(
                record.brand_id,
                record.order_item_id,
                OrderItemStatusUpdate(to_status=_SHIP_TO_STATUS, reason=None),
            )
        except OrderAlreadyShipped:
            return (
                "already_done",
                f"이미 발송 처리된 주문 아이템입니다 (orderItemId={record.order_item_id}) "
                "— 중복 실행하지 않았습니다.",
            )
        except OrderInvalidTransition:
            return (
                "stale",
                f"지금은 발송 처리할 수 없는 상태입니다 (orderItemId={record.order_item_id}) "
                "— 이미 배송 단계로 넘어갔거나 취소 요청 등 클레임이 진행 중입니다. "
                "주문 현황을 다시 확인해 주세요.",
            )
        except OrderItemNotFound:
            return (
                "stale",
                f"대상 주문 아이템(orderItemId={record.order_item_id})을 찾을 수 없어 "
                "반영을 중단했습니다. 주문 현황을 다시 확인해 주세요.",
            )
        return (
            "executed",
            f"발송 처리했습니다 (orderItemId={shipped.order_item_id}, "
            f"{shipped.from_status}→{shipped.to_status}). 발송 후에는 취소·되돌리기가 "
            "불가하며, 구매자 구제는 반품 절차로만 진행됩니다.",
        )

    if record.op == "create":
        values = {c.field: _typed_after(c) for c in record.changes}
        # [#506] category 는 changes 에 **스냅샷 id** 로 실려 있다(validate_draft 선검증).
        # I-10 에 쓸 값은 카탈로그가 만든다 — BE 는 `categoryId`(Long)만 받는다.
        # 스냅샷 교체(배포) 사이에 id 가 사라진 draft 는 실행하지 않고 되묻는다 —
        # 보여준 카테고리와 다른 값이 저장되는 것보다 재초안이 낫다.
        category_id: int | None = None
        if "category" in values:
            category_id = category_catalog.spring_category_id(str(values["category"]))
        if category_id is None:
            # validate_draft 가 create 의 category 를 필수로 막지만, 그 검증을 통과한
            # 뒤 스냅샷이 교체될 수 있다. 여기서도 확인해야 BE 422 대신 안내가 나간다.
            return (
                "stale",
                "초안의 카테고리가 더 이상 유효하지 않습니다(카테고리 목록 갱신). "
                "등록할 상품 종류를 다시 말씀해 주시면 새 초안을 만들어 드리겠습니다.",
            )
        # [#524 듀얼모드] 재고는 wire_mode 가 정한 딱 한 필드로 나간다. 등록 시점엔
        # 옵션이 없으므로(#506 create 흐름) stocks 모드는 optionId null 한 줄이다.
        #
        # update 와 달리 여기엔 _stocks_mode_ready 가드를 두지 않는다 — 등록은 조회하는
        # 상품 자체가 없어 I-9 로 BE 버전을 확인할 길이 없고, 확인하려면 create 흐름에
        # 없는 조회를 새로 넣어야 한다. 그럴 필요도 없다: stocks 모드 create 는
        # stockQuantity 를 아예 안 실으므로 구 BE 는 **422 MISSING_FIELD 로 시끄럽게
        # 실패**한다(조용한 부분 성공이 나는 update 와 실패 양상이 반대다).
        create_stock = int(values["stock_quantity"])
        stocks_mode = get_settings().seller_stock_wire_mode == "stocks"
        payload = ProductCreate(
            name=str(values["name"]),
            price=int(values["price"]),
            stock_quantity=None if stocks_mode else create_stock,
            stocks=[StockEntry(option_id=None, quantity=create_stock)] if stocks_mode else None,
            original_price=(int(values["original_price"]) if "original_price" in values else None),
            category_id=category_id,
            description=str(values["description"]) if "description" in values else None,
            # [#506] 이미지 기반 등록 — validate_draft 가 canonical URL(≤500자)을 보장.
            image_url=str(values["image_url"]) if "image_url" in values else None,
        )
        try:
            created = await client.create_product(record.brand_id, payload)
        except InvalidStock:
            # [#524] 등록 재고는 _parse_int 가 음수를 선차단하므로 정상 경로에선 안 온다.
            # 여기 오면 계약 해석이 어긋난 것이라 재시도를 권하지 않는다.
            return (
                "stale",
                "재고 수량이 서버에서 거부되어 등록을 중단했습니다. "
                f"재고를 다시 확인해 말씀해 주세요. {_STALE_RETRY_GUIDE}",
            )
        except InvalidPrice:
            # [#620] validate_draft 가 price/originalPrice 를 이미 선차단하므로 정상
            # 경로에선 안 온다 — InvalidPrice 는 SpringUnavailableError 하위가 아니라
            # 잡지 않으면 그대로 새어나가므로(§ 예외 계약) 안전망으로 둔다.
            return (
                "stale",
                "판매가가 정가를 넘어 서버에서 거부되어 등록을 중단했습니다. "
                "가격을 다시 확인해 말씀해 주세요.",
            )
        except ProductCategoryInvalid:
            # [#541] 위 spring_category_id 선검증은 **스냅샷 기준**이라 스냅샷이 정본 DB
            # 보다 낡으면 통과한 id 가 서버에서 거부된다. 안내를 위 stale 분기와 같은
            # 문안으로 맞춘다 — 판매자에게는 같은 사건("카테고리가 더 이상 유효하지 않다")
            # 이고, 어느 쪽이 먼저 걸렸는지는 판매자가 알 필요도 구분할 방법도 없다.
            logger.warning(
                "seller create rejected category snapshot_id=%s — 스냅샷 재생성 필요",
                values.get("category"),
            )
            return (
                "stale",
                "초안의 카테고리가 서버에서 거부되어 등록을 중단했습니다(카테고리 목록 갱신). "
                "등록할 상품 종류를 다시 말씀해 주시면 새 초안을 만들어 드리겠습니다.",
            )
        except ProductFieldMissing:
            # [#541] validate_draft 가 필수 4종을 모두 강제하므로 값 누락으로는 오지
            # 않는다 — 여기 오면 **와이어 형식이 서버와 어긋난 것**이다(대표적으로
            # seller_stock_wire_mode 를 BE 배포보다 먼저 켠 경우). 판매자가 초안을
            # 고쳐서 풀 수 있는 문제가 아니므로 재시도·재초안을 권하지 않는다.
            logger.error(
                "seller create rejected MISSING_FIELD — 와이어 형식 불일치 의심 "
                "(stock_wire_mode=%s)",
                get_settings().seller_stock_wire_mode,
            )
            return (
                "stale",
                "서버가 등록 요청을 받아들이지 않아 등록을 중단했습니다(필수 항목 누락). "
                "같은 내용으로 다시 시도해도 결과가 같아 담당자 확인이 필요합니다.",
            )
        return (
            "executed",
            f"상품을 등록했습니다 (productId={created.product_id}, status={created.status}).",
        )

    assert record.product_id is not None  # validate_draft 가 보장
    row = await _find_product(record.brand_id, record.product_id)
    if row is None:
        return (
            "stale",
            f"대상 상품(productId={record.product_id})을 상품 목록에서 찾을 수 없어 "
            "반영을 중단했습니다. 이미 삭제되었거나(삭제한 상품은 목록에서 빠집니다) "
            f"다른 브랜드로 옮겨진 것 같습니다. {_STALE_RETRY_GUIDE}",
        )

    mismatches = find_stale_changes(row, record.changes, op=record.op)
    if mismatches:
        lines = [
            f"- {_FIELD_LABELS.get(field, field)}: 초안 기준 '{before}' → 현재 '{current}'"
            for field, before, current in mismatches
        ]
        return (
            "stale",
            "초안 작성 이후 상품 정보가 변경되어 반영을 중단했습니다.\n"
            + "\n".join(lines)
            + f"\n{_STALE_RETRY_GUIDE}",
        )

    # stock 은 stale 비교 제외 대신 변동 사실을 결과 안내에 표기(2026-07-20 확정).
    # [#524] 옵션별 재고(row.stocks 채워짐)면 해당 옵션의 현재 수량과 비교하고 **어느
    # 옵션인지 밝힌다** — 합계와 비교하면 다른 옵션의 주문 차감까지 "변동"으로 오보하고,
    # 옵션명을 빼면 판매자가 무엇이 변했는지 알 수 없다. 변동 건은 **누적**한다: 대입으로
    # 두면 옵션 여러 개가 동시에 변해도 마지막 하나만 남아 나머지가 조용히 사라진다.
    stock_notes: list[str] = []
    for change in record.changes:
        if change.field != "stock_quantity":
            continue
        try:
            before_stock: int | None = _parse_int(change.before) if change.before.strip() else None
        except ValueError:
            before_stock = None
        current_stock = row.stock_quantity
        option_label: str | None = None
        if row.stocks:
            matched, _problem = resolve_stock_option(change.option_name, row.stocks)
            if matched is not None:
                current_stock = matched.quantity
                # 옵션 없는 상품(option_id null)은 라벨을 붙이지 않는다 — 붙일 이름도
                # 없고, 기존(단일 재고) 안내 문구를 글자 그대로 유지해 회귀를 막는다.
                option_label = matched.option_name if matched.option_id is not None else None
        if before_stock != current_stock:
            stock_notes.append(
                f"{option_label} {current_stock}건" if option_label else f"{current_stock}건"
            )
    stock_note = (
        f" 참고: 초안 작성 후 재고가 {' · '.join(stock_notes)}으로 변동되어 "
        "있었습니다(주문 처리 등)."
        if stock_notes
        else ""
    )

    if record.op == "delete":
        try:
            deleted = await client.delete_product(record.brand_id, record.product_id)
        except ProductAlreadyDeleted:
            return (
                "already_done",
                f"이미 삭제된 상품입니다 (productId={record.product_id}) — 중복 실행하지 "
                "않았습니다. 삭제는 되돌릴 수 없어 다시 시도해도 결과가 같습니다.",
            )
        return (
            "executed",
            f"상품을 삭제했습니다 (productId={deleted.product_id}, status={deleted.status}). "
            "숨김(판매정지)과 달리 판매자 상품 목록에서도 빠지며 되돌릴 수 없습니다. "
            "다만 물리 삭제는 아니라 기존 주문 내역·매출 통계는 그대로 남습니다.",
        )

    patch, stock_problem = _build_update_patch(record.changes, row)
    if patch is None:
        return ("stale", f"{stock_problem} {_STALE_RETRY_GUIDE}")
    try:
        updated = await client.update_product(record.brand_id, record.product_id, patch)
    except ProductDeletedNotEditable:
        # [#511] 삭제된 상품은 수정 대상이 아니다 — 재시도해도 결과가 같다.
        return (
            "already_done",
            f"이미 삭제된 상품이라 수정할 수 없습니다 (productId={record.product_id}). "
            "삭제는 되돌릴 수 없으니 같은 상품이 필요하시면 새로 등록해 주세요.",
        )
    except InvalidStock:
        # [#524] hitl 이 음수·타 상품 옵션을 모두 선차단하므로, 여기 오는 건 confirm
        # 시점 I-9 재조회와 이 PATCH 사이에 옵션이 삭제·변경된 레이스뿐이다. 같은 초안을
        # 다시 보내도 결과가 같으므로 재confirm 이 아니라 **재조회 후 새 초안**을 권한다.
        return (
            "stale",
            "재고를 반영하는 사이에 상품 옵션이 변경되어 반영을 중단했습니다. "
            f"{_STALE_RETRY_GUIDE}",
        )
    except InvalidPrice:
        # [#620] validate_draft 가 row 를 받았으면 price/originalPrice 를 이미
        # 선차단하므로, 여기 오는 건 draft 표시와 confirm 사이 다른 경로(웹 UI 등)에서
        # 가격이 바뀐 레이스뿐이다(row 없이 confirm 된 구 draft 포함). 같은 초안을
        # 다시 보내도 결과가 같으므로 재조회 후 새 초안을 권한다.
        return (
            "stale",
            "판매가가 정가를 넘어 서버에서 거부되어 반영을 중단했습니다. "
            f"초안 표시 이후 가격이 바뀐 것 같습니다. {_STALE_RETRY_GUIDE}",
        )
    if not updated.changes:
        # [#620] BE 는 실제로 바뀐 값이 없으면 changes 를 빈 배열로 응답한다(요청 자체는
        # 성공, SellerProductUpdateResponse). "반영했습니다"라고 하면 판매자가 뭔가
        # 바뀐 줄 알므로 already_done 으로 갈음한다 — 패널도 자연히 keep 이 된다
        # (_confirm_stream 의 panel 분기는 status=="executed" 만 refresh).
        return (
            "already_done",
            f"이미 요청하신 값으로 되어 있어 바뀐 내용이 없습니다 (productId={updated.product_id}).",
        )
    summary_part = f" {record.summary}" if record.summary else ""
    return (
        "executed",
        f"변경을 반영했습니다 (productId={updated.product_id}).{summary_part}{stock_note}",
    )


# ── HITL 그래프 (draft 저장 → interrupt → resume 실행) ──────────────────────────


class HitlState(TypedDict, total=False):
    """HITL 스레드 상태 — draft 가 입력이자 정본, outcome/result 는 실행 후 기록.

    cancelled 는 [#506] 무효화 마킹 — 수정 턴이 새 draft 를 발급하면 이전 draft 에
    True 를 기록해, 브라우저에 남은 옛 카드의 confirm(수정 전 값 등록 사고)을 차단한다.
    """

    draft: dict
    outcome: str
    result: str
    cancelled: bool


async def _hitl_node(state: HitlState) -> HitlState:
    """단일 노드: interrupt 로 승인 대기 → resume 시 코드 실행.

    interrupt() 이전 구간은 노드 재실행(resume) 시 다시 돌므로 부수효과를 두지
    않는다. resume 값 자체는 쓰지 않는다 — confirm 판정·신원/TTL/멱등 검사는
    confirm_draft(코드)가 resume 이전에 끝낸다.
    """
    record = DraftRecord.model_validate(state["draft"])
    interrupt({"draftId": record.draft_id, "op": record.op})
    with trace_span("seller.worker.hitl_write", "chain"):
        outcome, text = await _execute_draft(record)
    # [이슈 #590] 추천 적용 경로(rec_id 有) 실행 성공 시 저장 계층에 상태를 되돌려 쓴다.
    # 실행(가격/재고 변경)은 이미 끝난 뒤이므로, 이 갱신이 실패해도 실행 자체를 되돌리거나
    # 막지 않는다 — 추천 추적은 부가 데이터(save_history 와 동일한 degrade 원칙).
    if outcome == "executed" and record.rec_id:
        try:
            await analysis_store.mark_recommendation_applied(
                uuid.UUID(record.rec_id), brand_id=record.brand_id, draft_id=record.draft_id
            )
        except Exception:
            logger.warning(
                "seller_recommendation_apply_mark_failed rec_id=%s draft_id=%s",
                record.rec_id,
                record.draft_id,
                exc_info=True,
            )
    return {"outcome": outcome, "result": text}


# checkpointer 싱글턴은 공용 모듈(checkpoint.py)로 이관 — 채팅 스레드(thread.py)와
# 공유한다. set_checkpointer 는 기존 소비자(conftest 등) 호환을 위해 재수출한다.
# 교체 시 이 모듈의 컴파일된 그래프·confirm 락 캐시는 reset hook 으로 함께 무효화된다.
set_checkpointer = seller_checkpoint.set_checkpointer

_graph = None

# confirm 동시성 직렬화용 draftId→Lock 레지스트리(프로세스 내)다. 지금은 프로세스 로컬
# ActiveStreamRegistry 때문에 워커 다중화가 금지돼 있다. 다중화 허용 조건과 이 락이 워커 간에
# 무력화되는 위험은 docs/specs/OPS-SCALEOUT-476.md를 따른다. draft 는 1회성이라 항목 수는
# 프로세스 수명 동안의 draft 수로 유계 — 명시 정리는 생략한다.
_confirm_locks: dict[str, asyncio.Lock] = {}


def _reset_cache() -> None:
    global _graph
    _graph = None
    _confirm_locks.clear()


seller_checkpoint.register_reset_hook(_reset_cache)


def _confirm_lock(draft_id: str) -> asyncio.Lock:
    """draftId 별 asyncio.Lock 을 반환한다(없으면 생성). 이벤트 루프 단일 스레드에서
    dict 접근은 await 없이 원자적이라 별도 보호가 필요 없다."""
    lock = _confirm_locks.get(draft_id)
    if lock is None:
        lock = asyncio.Lock()
        _confirm_locks[draft_id] = lock
    return lock


async def _get_graph():
    """HITL 그래프 싱글턴 — 공용 checkpointer(checkpoint.py) 준비 후 1회 컴파일."""
    global _graph
    if _graph is None:
        checkpointer = await seller_checkpoint.get_checkpointer()
        builder = StateGraph(HitlState)
        builder.add_node("hitl", _hitl_node)
        builder.add_edge(START, "hitl")
        builder.add_edge("hitl", END)
        _graph = builder.compile(checkpointer=checkpointer)
    return _graph


def _thread_config(draft_id: str) -> dict:
    """draftId ↔ checkpoint 바인딩(안전장치 ①) — thread_id 가 곧 바인딩이다."""
    return {"configurable": {"thread_id": f"seller-draft:{draft_id}"}}


# ── 공개 API (app/api/seller.py 소비) ────────────────────────────────────────────


async def start_draft(record: DraftRecord) -> None:
    """스트림 1: draft 를 checkpoint 에 저장하고 interrupt 대기 상태로 만든다."""
    graph = await _get_graph()
    result = await graph.ainvoke(
        {"draft": record.model_dump()}, config=_thread_config(record.draft_id)
    )
    if "__interrupt__" not in result:
        raise RuntimeError("HITL draft 저장 실패 — interrupt 가 발생하지 않았다")


async def invalidate_draft(draft_id: str) -> None:
    """[#506] draft 를 무효화한다 — 수정 턴의 새 draft 발급 시 이전 draftId 에 호출.

    FE 계약 §5.6: 수정할 때마다 새 draftId 가 발급되고 **이전 것은 서버가 무효화**한다
    — 안 그러면 브라우저에 남은 옛 카드의 등록 버튼으로 수정 전 값이 등록된다.
    무효화된 draft 의 confirm 은 not_found(존재 비노출 문구)로 거절된다.
    실패는 warning 후 계속 — 이전 draft 는 TTL(⑤)이 최종 방어선이다.

    [리뷰 M-2] confirm 과 같은 draftId 락으로 직렬화한다 — 락 없이는 confirm 의
    cancelled 검사(snapshot 시점 1회)와 이 쓰기가 경합해 "무효화됐는데 실행"이
    가능하다. 락·스트림 레지스트리 모두 **프로세스 로컬**이라 다중 워커 배포에서는
    이 보장이 약하다(체크포인트 단일화가 별도 방어) — 한계를 여기 명시해 둔다.
    """
    try:
        graph = await _get_graph()
        async with _confirm_lock(draft_id):
            await graph.aupdate_state(_thread_config(draft_id), {"cancelled": True}, as_node="hitl")
    except Exception:
        logger.warning(
            "draft 무효화 실패 — TTL 만료가 최종 방어 (draftId=%s)", draft_id, exc_info=True
        )


@dataclass(frozen=True)
class ConfirmOutcome:
    """confirm 처리 결과 — text 는 그대로 사용자 token 이 된다."""

    status: Literal["executed", "stale", "already_done", "not_found", "expired"]
    text: str


# 미존재·소유 불일치 공용 문구 — 타 판매자에게 draft 존재 여부를 노출하지 않는다(④).
_NOT_FOUND_TEXT = (
    "해당 승인 요청을 찾을 수 없습니다. 초안이 만료됐거나 잘못된 요청입니다. "
    "변경 내용을 다시 말씀해 주시면 새 초안을 만들어 드리겠습니다."
)


async def confirm_draft(draft_id: str, *, seller_id: int, brand_id: int) -> ConfirmOutcome:
    """스트림 2: confirm — 코드 검사(존재→소유→멱등→TTL) 통과 시에만 resume 실행.

    검사를 resume 이전에 두는 이유: 실패 사유(만료·소유 불일치)로 스레드를
    실행/종결시키면 안 되기 때문 — 특히 소유 불일치는 draft 를 죽여서도 안 된다.
    Spring 장애는 예외 전파 — checkpoint 는 interrupt 에 남아 재confirm 가능.
    """
    graph = await _get_graph()
    config = _thread_config(draft_id)
    # 동시성(안전장치 ③ 보강): 같은 draftId 의 confirm 을 직렬화한다. 상태 조회→멱등
    # 판정→resume 실행이 원자적이지 않으면, 동시 confirm 2건이 모두 result 미설정을
    # 보고 각자 실행해 상품 쓰기가 중복된다(I-10/11/12). 락은 프로세스 내 직렬화 —
    # 다중 워커 환경은 checkpoint 단일화(pg-profile)가 별도로 보장한다.
    async with _confirm_lock(draft_id):
        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        draft_data = values.get("draft")
        if not draft_data:
            return ConfirmOutcome("not_found", _NOT_FOUND_TEXT)

        record = DraftRecord.model_validate(draft_data)
        # 소유 검증(안전장치 ④): brand_id 만이 아니라 seller_id 까지 대조 — 같은
        # 브랜드에 복수 판매자 계정이 있을 때 타 판매자 draftId 승인을 차단한다.
        if record.brand_id != brand_id or record.seller_id != seller_id:
            logger.warning("draft 소유 불일치 confirm 차단 (draftId=%s)", draft_id)
            return ConfirmOutcome("not_found", _NOT_FOUND_TEXT)

        # [#506] 무효화된 draft(수정 턴이 새 draftId 를 발급) — 존재 비노출 거절.
        # 소유 검증 **뒤**에 두는 이유: 타 판매자에게는 무효화 여부도 노출하지 않는다.
        if values.get("cancelled"):
            return ConfirmOutcome("not_found", _NOT_FOUND_TEXT)

        if values.get("result"):  # 실행 완료 스레드 — 멱등(안전장치 ③)
            return ConfirmOutcome(
                "already_done",
                f"이미 처리된 승인 요청입니다 — 중복 실행하지 않았습니다. 이전 결과: {values['result']}",
            )

        settings = get_settings()
        created = datetime.fromisoformat(record.created_at)
        # 경계 포함(>=) — draft_session(#346)과 같은 판정: ttl=0 은
        # "즉시 만료"이고, 엄격 부등호는 시계 분해능(Windows ~15.6ms 틱)에 걸린다.
        if datetime.now(UTC) - created >= timedelta(minutes=settings.seller_draft_ttl_minutes):
            return ConfirmOutcome(
                "expired",
                f"초안이 만료됐습니다(유효 {settings.seller_draft_ttl_minutes}분). "
                "변경 내용을 다시 말씀해 주시면 새 초안을 만들어 드리겠습니다.",
            )

        result = await graph.ainvoke(Command(resume=True), config=config)
    return ConfirmOutcome(result.get("outcome", "executed"), result.get("result", ""))
