"""app/agents/seller/hitl.py 4-2 HITL 실행 검증 — 실 LLM·PG·HTTP 없음.

InMemorySaver 주입 + 스텁 SpringClient 로 전체 흐름(draft 저장 → interrupt →
confirm resume → 쓰기)과 안전장치 5종(§6.2)을 검증한다. 설계 확정(2026-07-20):
코드 직접 실행 / dev InMemory 폴백 / stale 비교에서 stock 제외.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.seller import category_catalog, hitl
from app.agents.seller.schemas import DraftChange, DraftProposal
from app.schemas.spring import (
    BehaviorEventsResult,
    BehaviorProductRow,
    OrderItemStatusResult,
    ProductCreateResult,
    ProductDeleteResult,
    ProductUpdateResult,
    SellerProductList,
    SellerProductRow,
    SellerStockRow,
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
    SpringUnavailableError,
    set_spring_client,
)


@pytest.fixture(autouse=True)
def _fresh_checkpointer():
    """테스트마다 격리된 InMemorySaver — PG 연결 시도 자체를 차단한다."""
    hitl.set_checkpointer(InMemorySaver())
    yield
    hitl.set_checkpointer(None)
    set_spring_client(None)


_ROW = SellerProductRow(
    productId=101,
    name="감귤청",
    price=15000,
    originalPrice=18000,
    stockQuantity=100,
    status="ON_SALE",
    description="제주 감귤청입니다.",
)


class _StubSpring:
    """판매자 CRUD 4종만 흉내 — 호출 기록으로 실행 여부·인자를 검증한다."""

    def __init__(self, rows: list[SellerProductRow] | None = None, fail_list: bool = False):
        self.rows = rows if rows is not None else [_ROW]
        self.fail_list = fail_list
        self.calls: list[tuple] = []

    async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
        if self.fail_list:
            raise SpringUnavailableError("conn refused")
        self.calls.append(("list", brand_id, offset))
        start = offset or 0
        return SellerProductList(rows=self.rows[start : start + (limit or 20)])

    # [#524] I-10 422 INVALID_STOCK 주입용 — update_error 와 같은 규약.
    create_error: Exception | None = None

    async def create_product(self, brand_id, payload):
        self.calls.append(("create", brand_id, payload))
        if self.create_error is not None:
            raise self.create_error
        return ProductCreateResult(productId=999, status="ON_SALE")

    # [#511] I-11/I-12 409 전용 예외 주입 — "안 되는 일"이 장애로 뭉개지지 않는지 검증한다.
    update_error: Exception | None = None
    delete_error: Exception | None = None
    # [#620] I-11 응답 changes — 기본은 "뭔가 바뀌었다"(비어있지 않음, 실행 완료
    # 판정을 유지)로 두고, 빈 배열(실질 변경 없음 → already_done) 검증용 테스트만
    # None 이 아닌 [] 로 덮어쓴다.
    update_result_changes: list[str] | None = None

    async def update_product(self, brand_id, product_id, patch):
        self.calls.append(("update", brand_id, product_id, patch))
        if self.update_error is not None:
            raise self.update_error
        changes = self.update_result_changes if self.update_result_changes is not None else ["PRICE"]
        return ProductUpdateResult(productId=product_id, changes=changes)

    async def delete_product(self, brand_id, product_id):
        self.calls.append(("delete", brand_id, product_id))
        if self.delete_error is not None:
            raise self.delete_error
        return ProductDeleteResult(productId=product_id, status="DELETED")

    # [#297] I-30 발송 — ship_error 로 코드별 실패(409/400/404)를 주입한다.
    ship_error: Exception | None = None

    async def update_order_item_status(self, brand_id, order_item_id, payload):
        self.calls.append(("ship", brand_id, order_item_id, payload))
        if self.ship_error is not None:
            raise self.ship_error
        return OrderItemStatusResult(
            orderItemId=order_item_id,
            fromStatus="ORDERED",
            toStatus=payload.to_status,
            changedAt="2026-08-05T10:00:00+09:00",
        )

    # [#659] I-13 저성과 참고 문구 조회 — 기본은 "충분히 팔림"(경고 미발동)으로 두어
    # 이 문구와 무관한 기존 update 테스트의 outcome.text 를 건드리지 않는다.
    # 저성과 분기 자체를 검증하는 테스트만 low_sales_quantity 를 낮게 오버라이드한다.
    low_sales_quantity: int = 999
    events_error: Exception | None = None

    async def get_events(
        self, brand_id, from_=None, to=None, event_type=None, product_id=None, group_by=None
    ):
        self.calls.append(("get_events", brand_id, product_id))
        if self.events_error is not None:
            raise self.events_error
        rows = []
        if product_id is not None:
            rows = [
                BehaviorProductRow(
                    productId=product_id,
                    salesQuantity=self.low_sales_quantity,
                    counts={"productView": 40},
                )
            ]
        return BehaviorEventsResult(rows=rows)

    def write_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("create", "update", "delete", "ship")]


def _proposal(**kwargs) -> DraftProposal:
    base = dict(
        op="update",
        product_id=101,
        changes=[DraftChange(field="price", before="15000", after="12900")],
        summary="가격 12,900원으로 인하",
    )
    base.update(kwargs)
    return DraftProposal(**base)


def _category_id() -> str:
    """저장소 스냅샷의 실제 카테고리 id 하나 — 하드코딩하면 스냅샷 교체 때 깨진다.

    create 초안은 카테고리가 필수다(BE `categoryId` 필수) — 픽스처도 실제 값을 써야
    validate_draft·spring_category_id 를 정직하게 통과한다.
    """
    return category_catalog.all_entries()[0].id


def _create_changes(*extra: DraftChange) -> list[DraftChange]:
    """I-10 필수 4종(name/price/stock_quantity/category)이 채워진 create changes."""
    return [
        DraftChange(field="name", before="", after="한라봉청"),
        DraftChange(field="price", before="", after="20000"),
        DraftChange(field="stock_quantity", before="", after="50"),
        DraftChange(field="category", before="", after=_category_id()),
        *extra,
    ]


def _record(proposal: DraftProposal | None = None, **kwargs) -> hitl.DraftRecord:
    record, problem = hitl.validate_draft(proposal or _proposal(**kwargs), seller_id=7, brand_id=3)
    assert problem is None, problem
    assert record is not None
    return record


# ── validate_draft — 코드 선검증(캐스팅·필수 필드·C4) ───────────────────────────


def test_validate_draft_issues_id_and_identity() -> None:
    """draftId·신원·created_at 은 코드 발급 — LLM 필드가 아니다."""
    record = _record()
    assert record.draft_id
    assert record.seller_id == 7 and record.brand_id == 3
    assert datetime.fromisoformat(record.created_at).tzinfo is not None  # UTC aware


def test_validate_draft_accepts_comma_and_suffix_numbers() -> None:
    """도구 출력 표기("12,900원")를 옮겨적은 수치도 관용 캐스팅한다."""
    record = _record(changes=[DraftChange(field="price", before="15,000원", after="12,900원")])
    assert record is not None


def test_validate_draft_sanitizes_without_masking_executable_value() -> None:
    """실행 정본은 위험 문자만 제거하고 노출 전용 시크릿 마스킹으로 오염하지 않는다."""
    record = _record(
        changes=[
            DraftChange(
                field="description",
                before="기존 설명",
                after="키 ❤️ sk-abcdefghijklmnop1234 A\ufe0fB\U000e0061",
            )
        ]
    )

    assert record.changes[0].after == "키 ❤️ sk-abcdefghijklmnop1234 AB"
    assert "[민감 정보 차단]" not in record.changes[0].after


def test_validate_draft_rejects_uncastable_int() -> None:
    """정수 불가 수치는 되묻기 — confirm 시점 캐스팅 실패를 선차단한다."""
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="price", before="15000", after="열두배")]),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "price" in problem


def test_validate_draft_rejects_bad_status() -> None:
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="status", before="ON_SALE", after="SOLD_OUT")]),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "status" in problem


def test_validate_draft_update_requires_product_id() -> None:
    record, problem = hitl.validate_draft(_proposal(product_id=None), seller_id=7, brand_id=3)
    assert record is None and "상품" in problem


def test_validate_draft_update_requires_changes() -> None:
    record, problem = hitl.validate_draft(_proposal(changes=[]), seller_id=7, brand_id=3)
    assert record is None and problem


def test_validate_draft_create_requires_mandatory_fields() -> None:
    """I-10 필수(name/price/stockQuantity/categoryId) 누락 create 는 되묻기."""
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=[DraftChange(field="name", before="", after="한라봉청")],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None
    assert "가격" in problem and "재고 수량" in problem and "카테고리" in problem


def test_validate_draft_create_requires_category() -> None:
    """[2026-08-09] 카테고리 누락 create 는 되묻기 — BE categoryId 가 필수다.

    구 구현은 카테고리 없이도 초안을 통과시켰고, 판매자가 승인 버튼을 누른 뒤에야
    BE 가 거부해 "등록 중 오류"로만 보였다. 되물을 기회는 초안 단계뿐이다.
    """
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=[
                DraftChange(field="name", before="", after="한라봉청"),
                DraftChange(field="price", before="", after="20000"),
                DraftChange(field="stock_quantity", before="", after="50"),
            ],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None
    assert "카테고리" in problem
    # 이미 말한 상품명·가격까지 다시 묻지 않는다 — 되물을 것은 카테고리뿐이다.
    assert "상품명" not in problem


def test_validate_draft_create_rejects_unknown_category_id() -> None:
    """스냅샷 밖 카테고리 값(경로·이름·환각 id)은 되묻기로 전환한다(이중 방어)."""
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=_create_changes()[:3]
            + [DraftChange(field="category", before="", after="남성의류 > 셔츠")],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "카테고리" in problem


def test_validate_draft_create_allows_image_url() -> None:
    """[#506 계약 변경] create 의 image_url 은 허용 — 이미지 기반 등록 초안이 싣는다.

    구 C4/D3 금지(record is None + '이미지' 안내)는 폐기됐다 — canonical http(s) URL
    (≤ seller_image_url_max_len)은 통과하고 changes 에 그대로 남는다.
    """
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=_create_changes(
                DraftChange(field="image_url", before="", after="http://x/img.png")
            ),
        ),
        seller_id=7,
        brand_id=3,
    )
    assert problem is None
    assert record is not None
    assert {c.field: c.after for c in record.changes}["image_url"] == "http://x/img.png"


def test_validate_draft_create_forbids_status() -> None:
    """create 의 status 지정은 여전히 금지 — I-10 이 ON_SALE 로 발급한다."""
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=[
                DraftChange(field="name", before="", after="한라봉청"),
                DraftChange(field="price", before="", after="20000"),
                DraftChange(field="stock_quantity", before="", after="50"),
                DraftChange(field="status", before="", after="HIDDEN"),
            ],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "판매 상태" in problem


def test_validate_draft_create_rejects_presigned_or_long_image_url() -> None:
    """[#506] presigned·초과 길이 image_url 은 되묻기 — 저장되면 만료 시점에 이미지가 죽는다."""
    presigned = "https://bucket.s3.amazonaws.com/x.jpg?X-Amz-Signature=abc123"
    record, problem = hitl.validate_draft(
        _proposal(
            op="create",
            product_id=None,
            changes=_create_changes(DraftChange(field="image_url", before="", after=presigned)),
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "사진" in problem


def test_validate_draft_create_nullifies_product_id() -> None:
    """create 인데 LLM 이 product_id 를 넣어도 관용 — null 로 정규화(F2)."""
    record = _record(op="create", product_id=777, changes=_create_changes())
    assert record.product_id is None


# ── find_stale_changes — S-5 병존 대조(stock 제외) ──────────────────────────────


def test_stale_detects_changed_field() -> None:
    changes = [DraftChange(field="price", before="14000", after="12900")]
    mismatches = hitl.find_stale_changes(_ROW, changes)
    assert mismatches == [("price", "14000", "15000")]


def test_stale_ignores_number_formatting() -> None:
    """ "15,000원" vs 15000 — 표기 차이는 오탐하지 않는다(정수 비교)."""
    changes = [DraftChange(field="price", before="15,000원", after="12900")]
    assert hitl.find_stale_changes(_ROW, changes) == []


def test_stale_exempts_stock_quantity() -> None:
    """stock 은 주문 차감(F6)으로 자연 변동 — 비교 제외(2026-07-20 확정)."""
    changes = [DraftChange(field="stock_quantity", before="90", after="200")]
    assert hitl.find_stale_changes(_ROW, changes) == []


def test_stale_none_field_compares_as_empty() -> None:
    """현재값 None(예: category 미설정) 은 빈 문자열과 동치로 본다."""
    changes = [DraftChange(field="category", before="", after="청류")]
    assert hitl.find_stale_changes(_ROW, changes) == []


# ── 그래프 E2E — draft 저장 → interrupt → confirm resume → 쓰기 ─────────────────


def test_confirm_executes_update_with_draft_args() -> None:
    """confirm 후 실행되는 것은 'FE 에 보여준 draft 그 자체'(안전장치 ①) — 코드 매핑."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        assert spring.write_calls() == []  # 승인 전 쓰기 0회(발화 ≠ 동의)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert "101" in outcome.text
    writes = spring.write_calls()
    assert len(writes) == 1
    op, brand_id, product_id, patch = writes[0]
    assert (op, brand_id, product_id) == ("update", 3, 101)
    assert patch.price == 12900  # draft after 그대로 — LLM 재개입 없음
    assert "참고" not in outcome.text  # 기본 판매량(999)은 임계 이상 — 경고 미부착


def test_confirm_update_appends_low_sales_note_when_below_threshold() -> None:
    """[#659] 최근 N일 판매량이 임계 이하면 반영 완료 안내에 참고 문구가 붙는다."""
    spring = _StubSpring()
    spring.low_sales_quantity = 0
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert "101" in outcome.text  # 기존 안내는 그대로 유지
    assert "참고" in outcome.text
    assert "0개" in outcome.text
    assert "조회 40건" in outcome.text
    assert ("get_events", 3, 101) in spring.calls


def test_confirm_update_soft_fails_when_low_sales_query_unavailable() -> None:
    """[#659] 판매량 조회 실패는 조용히 무시한다 — 반영 자체는 막지 않는다(soft-fail)."""
    from app.services.spring_client import SpringUnavailableError

    spring = _StubSpring()
    spring.events_error = SpringUnavailableError("conn refused")
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert "101" in outcome.text
    assert "참고" not in outcome.text


def test_confirm_is_idempotent() -> None:
    """동일 draftId 재confirm 은 재실행 없이 안내(안전장치 ③ — 더블클릭 방지)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        first = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        second = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "executed"
    assert second.status == "already_done"
    assert len(spring.write_calls()) == 1  # 1회만 실행


def test_confirm_unknown_draft_id() -> None:
    spring = _StubSpring()
    set_spring_client(spring)

    outcome = asyncio.run(hitl.confirm_draft("no-such-draft", seller_id=7, brand_id=3))

    assert outcome.status == "not_found"
    assert spring.write_calls() == []


def test_confirm_brand_mismatch_hides_existence() -> None:
    """타 판매자의 draftId 추측 confirm — 미존재와 동일 문구(존재 비노출) + 실행 0회."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=999)

    outcome = asyncio.run(run())

    assert outcome.status == "not_found"
    assert outcome.text == hitl._NOT_FOUND_TEXT
    assert spring.write_calls() == []


def test_confirm_seller_mismatch_same_brand_blocks() -> None:
    """같은 브랜드(brand=3)라도 타 판매자(seller=8)의 draftId confirm 은 차단 — brand 만이
    아니라 seller 까지 대조한다(리뷰 반영: draft 소유권 IDOR 방지)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()  # seller_id=7, brand_id=3

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=8, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "not_found"
    assert outcome.text == hitl._NOT_FOUND_TEXT
    assert spring.write_calls() == []


def test_confirm_concurrent_executes_once() -> None:
    """동시 confirm 2건(안전장치 ③ 보강) — check-then-act 를 draftId 락으로 직렬화해
    상품 쓰기가 정확히 1회만 실행된다(중복 실행 방지)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await asyncio.gather(
            hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3),
            hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3),
        )

    outcomes = asyncio.run(run())

    statuses = sorted(o.status for o in outcomes)
    assert statuses == ["already_done", "executed"]
    assert len(spring.write_calls()) == 1  # 정확히 1회 실행


def test_confirm_expired_draft_blocks_execution() -> None:
    """TTL(안전장치 ⑤) — 만료 draft confirm 은 실행 없이 만료 안내."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()
    stale_created = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    expired = record.model_copy(update={"created_at": stale_created})

    async def run():
        await hitl.start_draft(expired)
        return await hitl.confirm_draft(expired.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "expired"
    assert "만료" in outcome.text
    assert spring.write_calls() == []


def test_confirm_stale_price_blocks_and_asks_again() -> None:
    """S-5 병존(F7) — before 불일치는 실행 중단 + 현재값 안내(되묻기)."""
    changed_row = _ROW.model_copy(update={"price": 13000})  # FE 직접 수정이 선행된 상황
    spring = _StubSpring(rows=[changed_row])
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert "13000" in outcome.text  # 현재값 안내
    assert spring.write_calls() == []


def test_confirm_stock_drift_executes_with_note() -> None:
    """stock 자연 변동은 실행을 막지 않되 결과 안내에 현재값을 표기한다."""
    drifted = _ROW.model_copy(update={"stock_quantity": 97})  # 주문 3건 차감
    spring = _StubSpring(rows=[drifted])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="100", after="200")],
        summary="재고 200건으로 보충",
    )

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert "97" in outcome.text  # 변동 사실 표기
    assert spring.write_calls()[0][3].stock_quantity == 200


def test_confirm_missing_product_is_stale() -> None:
    """I-9 재조회에서 상품 미발견(삭제 등, 짧은 페이지까지 다 돌았음) — 실행 중단 + 되묻기.

    [#622] `_find_product`가 (None, False) — 목록의 진짜 끝까지 다 돌았다 — 를 반환하는
    경로다. 안내 문구는 "해당 상품을 목록에서 찾지 못해서..."(exhausted=False 전용).
    """
    spring = _StubSpring(rows=[])
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert "목록에서 찾지 못해" in outcome.text
    assert hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT not in outcome.text
    assert spring.write_calls() == []


def test_confirm_product_lookup_exhausted_is_stale_with_distinct_text(monkeypatch) -> None:
    """[#622] 조회 상한 소진(exhausted=True) — "없다"가 아니라 "확인 못 했다" 문구를 쓴다.

    상품이 실제로 존재할 수도 있는데 짧은 페이지를 못 만나 순회를 못 끝낸 경우다 —
    삭제/이관 단정(`test_confirm_missing_product_is_stale`)과는 원인이 달라 문구를
    분리했다(hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT, _find_product 독스트링).
    """
    _lookup_page_size(monkeypatch, page_size=20, max_pages=2)
    filler = [_ROW.model_copy(update={"product_id": i, "name": f"상품{i}"}) for i in range(1, 41)]
    spring = _StubSpring(rows=filler)  # target(101) 은 filler 에 없다 + 매 페이지가 꽉 참
    set_spring_client(spring)
    record = _record()  # product_id=101 — filler 는 1..40, 101 은 없음

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT in outcome.text
    assert spring.write_calls() == []


def test_confirm_delete_maps_to_i12() -> None:
    """delete draft → I-12 soft delete — 결과는 DELETED 이고 "숨김"이라 말하지 않는다(§4.5)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(
        op="delete",
        changes=[DraftChange(field="status", before="ON_SALE", after="DELETED")],
        summary="상품 삭제",
    )

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert "삭제했어요" in outcome.text
    # 삭제를 숨김으로 안내하면 판매자가 되돌릴 수 있는 조작으로 오인한 채 승인한다.
    assert "숨김(판매정지)과 달리" in outcome.text
    assert spring.write_calls() == [("delete", 3, 101)]


# ── [#511] 삭제 상태 분리(BE 02 D41 · Notion I-12 2026-08-05) ────────────────────


def test_delete_draft_rejects_hidden_as_after() -> None:
    """delete 초안의 after 는 DELETED 뿐 — HIDDEN 은 되돌릴 수 있는 다른 상태다."""
    record, problem = hitl.validate_draft(
        _proposal(
            op="delete",
            changes=[DraftChange(field="status", before="ON_SALE", after="HIDDEN")],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and problem is not None


def test_update_draft_rejects_deleted_status() -> None:
    """삭제는 I-12 전용 전이 — DELETED 가 I-11 본문으로 새어나가면 BE 가 400 이다."""
    record, problem = hitl.validate_draft(
        _proposal(
            op="update",
            changes=[DraftChange(field="status", before="ON_SALE", after="DELETED")],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and problem is not None


def test_confirm_delete_of_hidden_product_is_not_stale() -> None:
    """숨겨둔 상품도 삭제된다 — HIDDEN→DELETED 는 정상 전이라 status 를 stale 비교하지 않는다.

    초안 작성 시점 ON_SALE 이던 상품을 판매자가 숨긴 뒤 승인해도 삭제가 막히면 안 된다
    (구 계약의 409 `ALREADY_HIDDEN` 이 만들던 것과 같은 증상).
    """
    hidden_row = _ROW.model_copy(update={"status": "HIDDEN"})
    spring = _StubSpring(rows=[hidden_row])
    set_spring_client(spring)
    record = _record(
        op="delete",
        changes=[DraftChange(field="status", before="ON_SALE", after="DELETED")],
        summary="상품 삭제",
    )

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert spring.write_calls() == [("delete", 3, 101)]


def test_confirm_delete_already_deleted_reports_already_done() -> None:
    """I-12 409 ALREADY_DELETED — 거짓 성공도, "재시도하세요"도 아니다(멱등 200 금지)."""
    spring = _StubSpring()
    spring.delete_error = ProductAlreadyDeleted("ALREADY_DELETED")
    set_spring_client(spring)
    record = _record(
        op="delete",
        changes=[DraftChange(field="status", before="HIDDEN", after="DELETED")],
        summary="상품 삭제",
    )

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "already_done"
    assert "이미 삭제되어 있어요" in outcome.text
    assert "다시 요청하셔도 결과는 같을 거예요" in outcome.text


def test_confirm_update_on_deleted_product_reports_already_done() -> None:
    """I-11 409 PRODUCT_DELETED — 삭제 상품은 챗봇이 되살릴 수 없다."""
    spring = _StubSpring()
    spring.update_error = ProductDeletedNotEditable("PRODUCT_DELETED")
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "already_done"
    assert "이미 삭제된 상품이라 수정할 수 없어요" in outcome.text


def test_confirm_create_maps_to_i10_without_image() -> None:
    """create draft → I-10 — C4/D3: imageUrl 미전송(BE 기본값 처리 가정)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(
        op="create",
        product_id=None,
        changes=[
            DraftChange(field="name", before="", after="한라봉청"),
            DraftChange(field="price", before="", after="20,000"),
            DraftChange(field="stock_quantity", before="", after="50"),
            DraftChange(field="category", before="", after=_category_id()),
            DraftChange(field="description", before="", after="제주 한라봉청"),
        ],
        summary="한라봉청 신규 등록",
    )

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed" and "등록했어요" in outcome.text
    payload = spring.write_calls()[0][2]
    assert (payload.name, payload.price, payload.stock_quantity) == ("한라봉청", 20000, 50)
    assert payload.image_url is None
    assert spring.calls[0][0] == "create"  # create 는 I-9 재조회(stale) 생략


def test_confirm_create_sends_numeric_category_id() -> None:
    """[2026-08-09] I-10 는 `categoryId`(Long)를 받는다 — leaf 명칭 문자열이 아니다.

    구 구현은 `category="셔츠"` 를 보냈고 BE 는 모르는 키를 버린 뒤 categoryId 누락으로
    등록을 거부했다. 와이어 키 이름과 타입이 이 회귀의 유일한 방어선이다.
    """
    spring = _StubSpring()
    set_spring_client(spring)
    category_id = _category_id()
    record = _record(op="create", product_id=None, changes=_create_changes())

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    payload = spring.write_calls()[0][2]
    assert payload.category_id == int(category_id)
    body = payload.model_dump(by_alias=True, exclude_none=True)
    assert body["categoryId"] == int(category_id)
    assert "category" not in body


def test_confirm_create_stale_category_does_not_call_spring() -> None:
    """초안 승인과 실행 사이에 스냅샷이 갈리면 등록하지 않고 재초안을 안내한다."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    record.changes[3].after = "999999999999999999"  # 스냅샷에 없는 id

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale" and "카테고리" in outcome.text
    assert spring.write_calls() == []


def test_confirm_spring_down_keeps_draft_retryable() -> None:
    """Spring 장애 시 예외 전파 — checkpoint 는 interrupt 에 남아 재confirm 가능."""
    spring = _StubSpring(fail_list=True)
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        with pytest.raises(SpringUnavailableError):
            await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        spring.fail_list = False  # 복구 후 재시도
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert len(spring.write_calls()) == 1


def _lookup_page_size(monkeypatch, page_size: int, max_pages: int | None = None) -> None:
    """[#622] `_find_product` 전용 page_size/max_pages 만 바꾼다 — 나머지 설정은 실값 그대로.

    운영 기본값(200×10)은 테스트 데이터가 비대해지므로, 페이지 순회 자체를 검증하는
    테스트는 작은 값으로 재설정한다(`_stock_mode`와 동일 패턴).
    """
    real = hitl.get_settings()
    update = {"seller_draft_lookup_page_size": page_size}
    if max_pages is not None:
        update["seller_draft_lookup_max_pages"] = max_pages
    monkeypatch.setattr(hitl, "get_settings", lambda: real.model_copy(update=update))


def test_find_product_paginates_until_found(monkeypatch) -> None:
    """I-9 productId 필터 부재 — 페이지 순회로 대상 행을 찾는다. (row, False) 반환."""
    _lookup_page_size(monkeypatch, page_size=20)
    filler = [_ROW.model_copy(update={"product_id": i, "name": f"상품{i}"}) for i in range(1, 41)]
    spring = _StubSpring(rows=[*filler, _ROW.model_copy(update={"product_id": 500})])
    set_spring_client(spring)

    row, exhausted = asyncio.run(hitl._find_product(3, 500))

    assert row is not None and row.product_id == 500
    assert exhausted is False
    assert len([c for c in spring.calls if c[0] == "list"]) == 3  # 20건 × 3페이지


def test_find_product_short_page_end_is_not_exhausted(monkeypatch) -> None:
    """목록의 진짜 끝(짧은 페이지)까지 다 돌았는데 없음 — (None, False), 상한 소진이 아니다."""
    _lookup_page_size(monkeypatch, page_size=20)
    filler = [_ROW.model_copy(update={"product_id": i, "name": f"상품{i}"}) for i in range(1, 11)]
    spring = _StubSpring(rows=filler)  # 10건 < page_size(20) → 첫 페이지가 곧 짧은 페이지
    set_spring_client(spring)

    row, exhausted = asyncio.run(hitl._find_product(3, 999))

    assert row is None
    assert exhausted is False
    assert len([c for c in spring.calls if c[0] == "list"]) == 1


def test_find_product_exhausts_page_cap_without_short_page(monkeypatch) -> None:
    """상한(max_pages)을 다 쓰도록 매 페이지가 꽉 차서 진짜 끝을 못 만남 — (None, True).

    #622 — 조회 실패(더 있을 수 있음)와 정상 없음(진짜 끝)을 구분하는 핵심 분기.
    """
    _lookup_page_size(monkeypatch, page_size=20, max_pages=2)
    filler = [_ROW.model_copy(update={"product_id": i, "name": f"상품{i}"}) for i in range(1, 41)]
    spring = _StubSpring(rows=filler)  # 정확히 20×2건 — 매 페이지가 꽉 차 짧은 페이지가 없다
    set_spring_client(spring)

    row, exhausted = asyncio.run(hitl._find_product(3, 999))

    assert row is None
    assert exhausted is True
    assert len([c for c in spring.calls if c[0] == "list"]) == 2


# ── [#297] op="ship" — I-30 발송 처리 HITL (§4.19) ───────────────────────────────


def _ship_proposal(**kwargs) -> DraftProposal:
    base = dict(
        op="ship",
        product_id=None,
        order_item_id=5551,
        changes=[],
        summary="주문 342 벨티드 린넨 원피스(블루/M) 발송 처리",
    )
    base.update(kwargs)
    return DraftProposal(**base)


def test_validate_ship_draft_requires_order_item_id() -> None:
    """대상 orderItemId 없는 ship 은 불성립 — 되묻기(임의 추측 금지)."""
    record, problem = hitl.validate_draft(
        _ship_proposal(order_item_id=None), seller_id=7, brand_id=3
    )
    assert record is None
    assert problem is not None and "주문" in problem


def test_validate_ship_draft_normalizes_changes_to_empty() -> None:
    """ship 의 changes 는 LLM 산물을 버리고 빈 목록으로 정규화 — 실행 인자는
    order_item_id 뿐이다(보여준 것==실행하는 것). 상품 필드 캐스팅도 적용되지 않는다."""
    record, problem = hitl.validate_draft(
        _ship_proposal(changes=[DraftChange(field="status", before="ORDERED", after="SHIPPING")]),
        seller_id=7,
        brand_id=3,
    )
    assert problem is None and record is not None
    assert record.op == "ship"
    assert record.order_item_id == 5551
    assert record.product_id is None
    assert record.changes == []


def test_confirm_ship_executes_i30_after_approval() -> None:
    """draft 저장 시 쓰기 0회 → confirm resume 시에만 I-30 1회 호출(ORDERED→SHIPPING)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)
    assert record is not None

    async def run():
        await hitl.start_draft(record)
        assert spring.write_calls() == []  # 승인 전 쓰기 0회(발화 ≠ 동의)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    op, brand_id, order_item_id, payload = spring.write_calls()[0]
    assert (op, brand_id, order_item_id) == ("ship", 3, 5551)
    assert payload.to_status == "SHIPPING"
    assert "발송 처리를 완료했어요" in outcome.text
    assert "반품" in outcome.text  # 발송 후 역전이 불가·구매자 구제 고지


def test_confirm_ship_already_shipped_is_already_done_not_success() -> None:
    """409 ORDER_ALREADY_SHIPPED — 멱등 성공으로 보고하지 않는다(I-12 논리)."""
    spring = _StubSpring()
    spring.ship_error = OrderAlreadyShipped("ORDER_ALREADY_SHIPPED")
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "already_done"
    assert "이미 발송 처리가" in outcome.text


def test_confirm_ship_invalid_transition_reports_stale() -> None:
    """400 ORDER_INVALID_TRANSITION(클레임 포함) — 실행 중단·현황 재확인 안내."""
    spring = _StubSpring()
    spring.ship_error = OrderInvalidTransition("ORDER_INVALID_TRANSITION")
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert "발송 처리를 할 수 없는 상태" in outcome.text


def test_confirm_ship_not_found_hides_existence() -> None:
    """404 ORDER_ITEM_NOT_FOUND — 타사 아이템 포함 존재 은닉(사유 미구분 안내)."""
    spring = _StubSpring()
    spring.ship_error = OrderItemNotFound("ORDER_ITEM_NOT_FOUND")
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert "찾지 못해" in outcome.text


def test_confirm_ship_spring_down_raises_and_allows_retry() -> None:
    """500·타임아웃은 예외 전파(성공 보고 금지) — draft 는 남아 재confirm 가능."""
    spring = _StubSpring()
    spring.ship_error = SpringUnavailableError("timeout")
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        with pytest.raises(SpringUnavailableError):
            await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        spring.ship_error = None  # 복구 후 재시도
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert len(spring.write_calls()) == 2  # 실패 1회 + 성공 1회(중복 반영 아님 — 첫 호출은 미반영)


def test_confirm_ship_second_confirm_is_idempotent() -> None:
    """실행 완료 후 재confirm 은 재실행 없이 멱등 안내(안전장치 ③)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        first = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        second = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "executed"
    assert second.status == "already_done"
    assert len(spring.write_calls()) == 1  # 쓰기는 1회뿐


def test_confirm_ship_ownership_mismatch_blocked() -> None:
    """타 판매자·타 브랜드의 draftId 추측 confirm 차단(안전장치 ④) — 실행 0회."""
    spring = _StubSpring()
    set_spring_client(spring)
    record, _ = hitl.validate_draft(_ship_proposal(), seller_id=7, brand_id=3)

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=8, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "not_found"
    assert spring.write_calls() == []


# ── [#524] 옵션별 재고 듀얼모드 (seller_stock_wire_mode) ───────────────────────


def _stock_mode(monkeypatch, mode: str) -> None:
    """hitl 이 보는 wire_mode 만 바꾼다 — 나머지 설정은 실값 그대로."""
    real = hitl.get_settings()
    monkeypatch.setattr(
        hitl, "get_settings", lambda: real.model_copy(update={"seller_stock_wire_mode": mode})
    )


_OPTIONED_ROW = SellerProductRow(
    productId=101,
    name="감귤청",
    price=15000,
    stockQuantity=5,
    stocks=[
        {"optionId": 10, "optionName": "블랙/M", "quantity": 5},
        {"optionId": 11, "optionName": "블랙/L", "quantity": 0},
        {"optionId": None, "optionName": None, "quantity": 0},
    ],
    status="ON_SALE",
)


def _run_confirm(record):
    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    return asyncio.run(run())


def test_quantity_mode_create_sends_stock_quantity_only(monkeypatch) -> None:
    """[회귀 고정] quantity 모드(기본)의 I-10 본문은 오늘 배포된 BE 계약 그대로다."""
    _stock_mode(monkeypatch, "quantity")
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][2].model_dump(by_alias=True, exclude_none=True)
    assert body["stockQuantity"] == 50
    assert "stocks" not in body


def test_stocks_mode_create_sends_null_option_row(monkeypatch) -> None:
    """stocks 모드 I-10 — 등록 시점엔 옵션이 없으므로 optionId null 한 줄(04 §I-10)."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][2].model_dump(by_alias=True, exclude_none=True)
    # exclude_none 직렬화라 optionId null 은 키 누락으로 나간다 — Jackson 은 누락을
    # null 로 바인딩하므로(record) "optionId: null 한 줄" 계약과 동등하다(StockEntry docstring).
    assert body["stocks"] == [{"quantity": 50}]
    assert "stockQuantity" not in body


def test_quantity_mode_update_unchanged(monkeypatch) -> None:
    """[회귀 고정] quantity 모드 I-11 — 구 계약 stockQuantity 정수 하나."""
    _stock_mode(monkeypatch, "quantity")
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="100", after="80")],
        summary="재고 80건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][3].model_dump(by_alias=True, exclude_none=True)
    assert body == {"stockQuantity": 80}


def test_quantity_mode_rejects_per_option_intent(monkeypatch) -> None:
    """quantity 모드에서 옵션 지정 재고는 반영하지 않는다 — BE 에 저장할 곳이 없다.

    합계로 뭉개면 "보여준 것 ≠ 실행하는 것"이 된다(카테고리 사고와 같은 유형).
    """
    _stock_mode(monkeypatch, "quantity")
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙/M")],
        summary="블랙/M 재고 10건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert spring.write_calls() == []


def test_quantity_mode_rejects_optioned_product_even_without_option_name(monkeypatch) -> None:
    """[#624] 옵션 상품은 option_name 이 없어도 quantity 모드에서 막는다.

    이전엔 이 경로가 그대로 stockQuantity 정수로 나가 BE 가 optionId=null 재고 행을
    찾다가(옵션 상품엔 그런 행이 없음) 422 INVALID_STOCK 을 던졌다 — 승인 이후에야
    터지고 안내도 "옵션 변경 레이스"로 잘못 나갔다. 승인 전에 걸러야 한다.
    """
    _stock_mode(monkeypatch, "quantity")
    spring = _StubSpring(rows=[_OPTIONED_ROW])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="5", after="10")],
        summary="재고 10건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert spring.write_calls() == []


def test_stocks_mode_update_resolves_option_name(monkeypatch) -> None:
    """stocks 모드 I-11 — option_name 을 confirm 시점 I-9 stocks 로 optionId 해소."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])
    set_spring_client(spring)
    record = _record(
        changes=[
            DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙/M"),
            DraftChange(field="stock_quantity", before="0", after="3", option_name="블랙/L"),
            DraftChange(field="price", before="15000", after="14000"),
        ],
        summary="옵션 재고 보충",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][3].model_dump(by_alias=True, exclude_none=True)
    assert body["stocks"] == [
        {"optionId": 10, "quantity": 10},
        {"optionId": 11, "quantity": 3},
    ]
    assert body["price"] == 14000
    assert "stockQuantity" not in body


def test_stocks_mode_ambiguous_option_reasks_without_write(monkeypatch) -> None:
    """ "블랙" 은 블랙/M·블랙/L 둘 다 — 실행하지 않고 되묻는다(INVALID_STOCK 선차단)."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙")],
        summary="재고",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale" and "블랙/M" in outcome.text
    assert spring.write_calls() == []


def test_stocks_mode_optionless_product_sends_null_option(monkeypatch) -> None:
    """옵션 없는 상품 — stocks 모드에서도 optionId null 한 줄로 나간다."""
    _stock_mode(monkeypatch, "stocks")
    row = _ROW.model_copy(update={"stocks": [SellerStockRow(optionId=None, quantity=100)]})
    spring = _StubSpring(rows=[row])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="100", after="70")],
        summary="재고 70건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][3].model_dump(by_alias=True, exclude_none=True)
    assert body["stocks"] == [{"quantity": 70}]  # optionId null = 키 누락(위 테스트와 동일 관용)


def test_stocks_mode_duplicate_option_reasks(monkeypatch) -> None:
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])
    set_spring_client(spring)
    record = _record(
        changes=[
            DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙/M"),
            DraftChange(field="stock_quantity", before="5", after="12", option_name="블랙/M"),
        ],
        summary="중복",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert spring.write_calls() == []


def test_validate_draft_clears_llm_option_id_and_foreign_option_name() -> None:
    """option_id 는 코드 전용(LLM 산물 불신), option_name 은 재고 change 에만 남는다."""
    record = _record(
        changes=[
            DraftChange(
                field="price", before="15000", after="12900", option_name="블랙/M", option_id=99
            ),
            DraftChange(
                field="stock_quantity", before="5", after="10", option_name="블랙/M", option_id=99
            ),
        ],
    )
    by_field = {c.field: c for c in record.changes}
    assert by_field["price"].option_name is None and by_field["price"].option_id is None
    assert by_field["stock_quantity"].option_name == "블랙/M"
    assert by_field["stock_quantity"].option_id is None


# ── [#524] stocks 모드 전제 가드 · 재고 변동 안내 · INVALID_STOCK ────────────────


def test_stocks_mode_refuses_when_be_not_ready(monkeypatch) -> None:
    """stocks 모드인데 I-9 가 stocks 를 안 주면 = BE 구버전 → 쓰기 없이 중단한다.

    구 BE 는 stocks 키를 버리므로, 그대로 보내면 같은 본문의 price 만 반영되고 재고는
    그대로인 채 "반영했습니다" 가 나간다(조용한 부분 실패). 그게 이 가드의 이유다.
    """
    _stock_mode(monkeypatch, "stocks")
    row = _ROW.model_copy(update={"stocks": []})  # 구 BE 응답
    spring = _StubSpring(rows=[row])
    set_spring_client(spring)
    record = _record(
        changes=[
            DraftChange(field="price", before="15000", after="14000"),
            DraftChange(field="stock_quantity", before="100", after="70"),
        ],
        summary="가격·재고 조정",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert spring.write_calls() == []  # 가격만 반영되는 부분 성공을 만들지 않는다


def test_stocks_mode_passes_when_no_stock_change(monkeypatch) -> None:
    """재고를 안 건드리는 update 는 stocks 계약과 무관 — 빈 stocks 여도 통과한다."""
    _stock_mode(monkeypatch, "stocks")
    row = _ROW.model_copy(update={"stocks": []})
    spring = _StubSpring(rows=[row])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="price", before="15000", after="14000")],
        summary="가격 인하",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][3].model_dump(by_alias=True, exclude_none=True)
    assert body == {"price": 14000}


def test_stock_drift_note_names_each_changed_option(monkeypatch) -> None:
    """옵션 2건이 동시에 변동하면 **둘 다** 옵션명과 함께 안내에 실린다(누적)."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])  # 블랙/M=5, 블랙/L=0
    set_spring_client(spring)
    record = _record(
        changes=[
            DraftChange(field="stock_quantity", before="9", after="10", option_name="블랙/M"),
            DraftChange(field="stock_quantity", before="9", after="3", option_name="블랙/L"),
        ],
        summary="옵션 재고 보충",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    assert "블랙/M 5건" in outcome.text and "블랙/L 0건" in outcome.text


def test_stock_drift_note_wording_unchanged_for_optionless(monkeypatch) -> None:
    """옵션 없는 상품의 변동 안내는 기존 문구 그대로 — 옵션명이 붙지 않는다(회귀 고정)."""
    _stock_mode(monkeypatch, "stocks")
    row = _ROW.model_copy(update={"stocks": [SellerStockRow(optionId=None, quantity=100)]})
    spring = _StubSpring(rows=[row])
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="80", after="70")],
        summary="재고 70건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    # 옵션 없는 상품은 옵션명이 섞이지 않고 수량만 나온다(회귀 고정) — 문구 톤은
    # 별개로 갱신됐지만 "옵션 라벨 없음" 이라는 계약은 그대로다.
    assert (
        " 참고로, 초안을 만든 뒤 주문 처리 등으로 재고가 100건으로 바뀌어 있었어요." in outcome.text
    )


def test_stocks_mode_partial_update_omits_untouched_option(monkeypatch) -> None:
    """I-11 부분 수정 — 초안에 없는 옵션은 본문 stocks 에 **실리지 않는다**(생략=불변)."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])  # optionId 10·11 + null 3행
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙/M")],
        summary="블랙/M 재고 10건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "executed"
    body = spring.write_calls()[0][3].model_dump(by_alias=True, exclude_none=True)
    assert body["stocks"] == [{"optionId": 10, "quantity": 10}]
    assert [entry.get("optionId") for entry in body["stocks"]] == [10]  # 11·null 없음


def test_update_invalid_stock_stops_without_success_report(monkeypatch) -> None:
    """I-11 422 INVALID_STOCK(옵션 변경 레이스) — 성공 보고 없이 재초안을 권한다."""
    _stock_mode(monkeypatch, "stocks")
    spring = _StubSpring(rows=[_OPTIONED_ROW])
    spring.update_error = InvalidStock("INVALID_STOCK")
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="5", after="10", option_name="블랙/M")],
        summary="블랙/M 재고 10건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "옵션이 바뀌어서" in outcome.text
    assert "반영했어요" not in outcome.text


def test_update_invalid_stock_quantity_mode_message_does_not_claim_race(monkeypatch) -> None:
    """[#624] quantity 모드의 INVALID_STOCK 은 "옵션 변경 레이스"로 단정하지 않는다.

    quantity 모드에는 옵션 상품을 사전 차단하는 가드가 없던 시절의 잔재 문구였다 —
    이제 hitl 이 옵션 상품을 이미 걸러내므로, 그래도 여기 도달했다면 원인을 안다고
    잘못 안내하지 않는다. stocks 모드 문구(옵션 레이스)와는 달라야 한다.
    """
    _stock_mode(monkeypatch, "quantity")
    spring = _StubSpring()  # 옵션 없는 기본 _ROW — 사전 차단 가드를 우회해 실제 BE 거부만 검증
    spring.update_error = InvalidStock("INVALID_STOCK")
    set_spring_client(spring)
    record = _record(
        changes=[DraftChange(field="stock_quantity", before="100", after="80")],
        summary="재고 80건",
    )
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "옵션이 변경되어" not in outcome.text
    assert "반영했습니다" not in outcome.text


def test_create_invalid_stock_stops_without_success_report() -> None:
    """I-10 422 INVALID_STOCK — 등록도 성공 보고 없이 중단한다."""
    spring = _StubSpring()
    spring.create_error = InvalidStock("INVALID_STOCK")
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "등록했습니다" not in outcome.text


# ── [#541] I-10 카테고리·필수값 거부가 "일시적 오류" 로 뭉개지지 않는다 ────────────


def test_create_category_invalid_guides_new_draft_not_retry() -> None:
    """400 PRODUCT_CATEGORY_INVALID — 스냅샷이 낡아 서버가 카테고리를 거부한 경우.

    같은 초안을 다시 confirm 해도 결과가 같으므로 재시도가 아니라 **카테고리를 다시
    말해 새 초안**을 권해야 한다. 매핑 전에는 SpringUnavailableError 로 낙성돼
    "일시적인 오류(재시도 가능)"가 나갔고, 판매자는 원인을 알 길이 없었다.
    """
    spring = _StubSpring()
    spring.create_error = ProductCategoryInvalid("PRODUCT_CATEGORY_INVALID")
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "카테고리" in outcome.text
    assert "등록했습니다" not in outcome.text


def test_create_missing_field_does_not_promise_retry() -> None:
    """422 MISSING_FIELD — 와이어 형식 불일치(#524 stocks 선전환 등).

    판매자가 초안을 고쳐서 풀 수 있는 문제가 아니라 담당자 확인이 필요하다. 그래서
    다른 stale 분기와 달리 "다시 요청해 주세요"(_STALE_RETRY_GUIDE)를 붙이지 않는다.
    """
    spring = _StubSpring()
    spring.create_error = ProductFieldMissing("MISSING_FIELD")
    set_spring_client(spring)
    record = _record(op="create", product_id=None, changes=_create_changes())
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "등록했습니다" not in outcome.text


# ── [#620] update 카테고리 선차단 · 상품명 길이 · 중복 필드 (가격 선차단은 D47로 폐지) ──


def test_validate_draft_update_rejects_category_change() -> None:
    """update 초안에 category 가 실리면 카드를 보여주기 전에 되묻는다.

    ProductUpdate 스키마에도 이제 category 필드가 없다(BE DTO 와 대칭) — 여기서 막지
    않으면 카드엔 "카테고리 변경"이 보이는데 confirm 해도 조용히 무시된다.
    """
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="category", before="", after=_category_id())]),
        seller_id=7,
        brand_id=3,
    )
    assert record is None
    assert "카테고리" in problem and "수정" in problem


def test_validate_draft_update_price_over_original_price_allowed_with_row() -> None:
    """[D47, 2026-08-13] 정책 폐지 — 판매가가 정가를 넘어도 더 이상 선차단하지 않는다.

    _ROW.original_price=18000 — 20000 원으로 바꿔도(정가 초과) 카드를 그대로 통과시킨다.
    """
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="price", before="15000", after="20000")]),
        seller_id=7,
        brand_id=3,
        row=_ROW,
    )
    assert problem is None and record is not None


def test_validate_draft_update_price_change_without_row_is_not_prechecked() -> None:
    """row 를 안 넘긴 호출부(레거시)는 이 선차단을 건너뛴다 — confirm 시점 BE 422 에 맡긴다."""
    record = _record(changes=[DraftChange(field="price", before="15000", after="20000")])
    assert record is not None


def test_validate_draft_update_price_within_original_price_with_row_passes() -> None:
    """정가 이하로 낮추는 정상 변경은 row 가 있어도 막히지 않는다."""
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="price", before="15000", after="12900")]),
        seller_id=7,
        brand_id=3,
        row=_ROW,
    )
    assert problem is None and record is not None


def test_confirm_update_invalid_price_reports_stale() -> None:
    """422 INVALID_PRICE(선차단을 우회한 레이스) — 재시도 대신 재조회 후 새 초안을 권한다."""
    spring = _StubSpring()
    spring.update_error = InvalidPrice("INVALID_PRICE")
    set_spring_client(spring)
    record = _record()
    outcome = _run_confirm(record)
    assert outcome.status == "stale"
    assert "정가" in outcome.text


def test_confirm_update_empty_changes_reports_already_done() -> None:
    """[#620] BE changes:[] (실질 변경 없음) — "반영했습니다" 대신 already_done 으로 갈음한다.

    panel 분기(_confirm_stream)는 status=="executed" 만 refresh 이므로, already_done 은
    자연히 keep 이 된다(추가 배선 없이).
    """
    spring = _StubSpring()
    spring.update_result_changes = []
    set_spring_client(spring)
    record = _record()
    outcome = _run_confirm(record)
    assert outcome.status == "already_done"
    assert "바뀐 내용은 없었어요" in outcome.text


def test_validate_draft_rejects_duplicate_non_stock_field() -> None:
    """같은 필드(재고 제외)가 changes 에 두 번 실리면 나중 값이 조용히 이기게 두지 않는다."""
    record, problem = hitl.validate_draft(
        _proposal(
            changes=[
                DraftChange(field="price", before="15000", after="12900"),
                DraftChange(field="price", before="15000", after="11000"),
            ]
        ),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "가격" in problem


def test_validate_draft_rejects_name_over_max_length() -> None:
    """[#620] BE @Size(max=200) 2차 방어 — 초과분은 카드 표시 전에 되묻는다."""
    from app.core.config import get_settings

    too_long = "가" * (get_settings().seller_name_max_len + 1)
    record, problem = hitl.validate_draft(
        _proposal(changes=[DraftChange(field="name", before="감귤청", after=too_long)]),
        seller_id=7,
        brand_id=3,
    )
    assert record is None and "상품명" in problem


def test_validate_draft_delete_normalizes_junk_changes_to_status_only() -> None:
    """delete 초안에 status 외 필드가 섞여도 카드 changes 는 status 한 건으로만 정규화된다.

    실행(I-12)은 changes 를 보지 않으므로("보여준 것==실행하는 것") 잡음을 카드에도
    보이지 않게 한다.
    """
    record, problem = hitl.validate_draft(
        _proposal(
            op="delete",
            changes=[
                DraftChange(field="status", before="ON_SALE", after="DELETED"),
                DraftChange(field="price", before="15000", after="0"),
            ],
        ),
        seller_id=7,
        brand_id=3,
    )
    assert problem is None and record is not None
    assert [c.field for c in record.changes] == ["status"]


def test_validate_draft_delete_without_status_change_normalizes_to_empty() -> None:
    """delete 초안이 status 변경을 안 실었으면(LLM 이 지시를 안 따른 경우) changes 를 비운다
    (ship 과 같은 패턴, I-9 재조회로 placeholder 를 합성하지 않는다)."""
    record, problem = hitl.validate_draft(
        _proposal(op="delete", changes=[]),
        seller_id=7,
        brand_id=3,
    )
    assert problem is None and record is not None
    assert record.changes == []


# ── 이슈 #621 — confirm 멱등(gate/execute 2노드 분리) ────────────────────────────


def test_gate_commits_attempted_at_before_execute_commits_result() -> None:
    """[증명, 이 이슈의 첫 커밋] gate 노드의 attempted_at 커밋이 execute 노드의 result
    커밋보다 먼저 체크포인트 이력에 영속화된다 — LangGraph super-step 커밋 타이밍이라는
    이 이슈의 전제를 확인한다. 이 전제가 깨지면 graph.aupdate_state 로 attempted_at 을
    직접 쓰는 대안으로 전환한다(이슈 본문 대안)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def run():
        await hitl.start_draft(record)
        await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        graph = await hitl._get_graph()
        history = [
            state
            async for state in graph.aget_state_history(hitl._thread_config(record.draft_id))
        ]
        return history

    history = asyncio.run(run())

    attempted_only = [
        s for s in history if s.values.get("attempted_at") and not s.values.get("result")
    ]
    has_result = [s for s in history if s.values.get("result")]
    assert attempted_only, "gate 커밋(attempted_at 단독)이 체크포인트 이력에 없다"
    assert has_result, "execute 커밋(result)이 체크포인트 이력에 없다"
    # created_at 타임스탬프로 직접 비교한다(이력 순서 규약에 기대지 않는 독립적 증거) —
    # attempted_at 단독 체크포인트가 result 있는 체크포인트보다 시간상 앞서야 한다.
    attempted_at_ts = min(s.created_at for s in attempted_only if s.created_at)
    result_ts = min(s.created_at for s in has_result if s.created_at)
    assert attempted_at_ts < result_ts, (
        "attempted_at 커밋이 result 커밋보다 먼저 영속화되지 않았다 — "
        "super-step 커밋 전제가 깨졌다"
    )


def test_confirm_unknown_when_own_resume_hits_wait_for_cap(monkeypatch) -> None:
    """[재현, LangGraph 1.2.9 실측] gate 는 interrupt 가 한 번 풀리면 재시도에서
    다시 실행되지 않는다(그 다음부턴 execute 만 재스케줄) — 그래서 "attempted_at
    有·result 無"는 방금 SpringUnavailableError 로 명확히 실패한 상태(재confirm
    허용, 아래 retryable 테스트들)에서도 그대로 남아, 그 값만으로는 unknown 을 판단할
    수 없다. unknown 은 confirm_draft **자신의** resume 시도가 execute 도중
    wait_for 상한에 걸렸을 때만 낸다 — 이 테스트는 그 상황을 실제로 재현한다."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    real = hitl.get_settings()
    monkeypatch.setattr(
        hitl,
        "get_settings",
        lambda: real.model_copy(update={"seller_confirm_execute_timeout_s": 0.05}),
    )

    original_update = spring.update_product

    async def hanging_update(*args, **kwargs):
        await asyncio.sleep(1.0)
        return await original_update(*args, **kwargs)

    spring.update_product = hanging_update

    async def run():
        await hitl.start_draft(record)
        outcome = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        graph = await hitl._get_graph()
        snapshot = await graph.aget_state(hitl._thread_config(record.draft_id))
        return outcome, snapshot

    outcome, snapshot = asyncio.run(run())

    assert outcome.status == "unknown"
    assert spring.write_calls() == []  # Spring 요청이 잘려 실제로 나가지 않았다
    # gate 커밋(attempted_at)은 남고 execute 는 다음 confirm 이 재시도할 대상으로 대기한다.
    assert snapshot.values.get("attempted_at")
    assert not snapshot.values.get("result")
    assert snapshot.next == ("execute",)


def test_confirm_after_unknown_can_still_retry(monkeypatch) -> None:
    """unknown 은 그 호출 한정 판정이다 — 다음 confirm 은 wait_for 상한 없이 정상
    재시도해 완주할 수 있다(#621, 사용자에게 "상품 목록에서 확인 후 안 됐으면 다시
    말씀해달라" 안내만 하고 draft 자체는 죽이지 않는다는 계약의 실측 확인)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    real = hitl.get_settings()
    monkeypatch.setattr(
        hitl,
        "get_settings",
        lambda: real.model_copy(update={"seller_confirm_execute_timeout_s": 0.05}),
    )

    original_update = spring.update_product
    hang = {"on": True}

    async def maybe_hanging_update(*args, **kwargs):
        if hang["on"]:
            await asyncio.sleep(1.0)
        return await original_update(*args, **kwargs)

    spring.update_product = maybe_hanging_update

    async def run():
        await hitl.start_draft(record)
        first = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        hang["on"] = False
        second = await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "unknown"
    assert second.status == "executed"
    assert len(spring.write_calls()) == 1  # 실제로 나간 쓰기는 재시도 1회뿐


def test_confirm_survives_outer_cancellation_via_shield() -> None:
    """[증명] confirm_draft 를 감싼 바깥 태스크가 취소돼도 asyncio.shield 덕분에 내부
    resume 실행은 끝까지 돈다 — Spring 쓰기가 나간 뒤 절단되면 checkpoint 미기록으로
    재confirm 시 중복 등록되는 문제(#621 ①)를 막는다."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    original_update = spring.update_product

    async def slow_update(*args, **kwargs):
        await asyncio.sleep(0.2)
        return await original_update(*args, **kwargs)

    spring.update_product = slow_update

    async def run():
        await hitl.start_draft(record)
        task = asyncio.create_task(hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3))
        await asyncio.sleep(0.05)  # inner 가 update_product 지연 중일 시점
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)  # shield 로 보호된 inner 완주 대기
        graph = await hitl._get_graph()
        snapshot = await graph.aget_state(hitl._thread_config(record.draft_id))
        return snapshot.values

    values = asyncio.run(run())

    assert values.get("outcome") == "executed"
    assert len(spring.write_calls()) == 1  # shield 덕분에 실행은 정확히 1회 완주


def test_mark_recommendation_applied_same_commit_as_write(monkeypatch) -> None:
    """추천 적용 경로(rec_id 有)의 마킹 호출이 execute 노드 반환값 계산 안에 있어, 쓰기
    (_execute_draft)와 같은 super-step 에 포함된다(#621 ④) — 별도 커밋으로 갈리면 쓰기는
    끝났는데 마킹만 유실되는 창이 생긴다."""
    spring = _StubSpring()
    set_spring_client(spring)
    calls: list[tuple] = []

    async def _fake_mark(rec_id, *, brand_id, draft_id):
        calls.append((rec_id, brand_id, draft_id))

    monkeypatch.setattr(hitl.analysis_store, "mark_recommendation_applied", _fake_mark)
    rec_id = "12345678-1234-5678-1234-567812345678"
    record = _record(_proposal(rec_id=rec_id))

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert len(calls) == 1
    assert str(calls[0][0]) == rec_id
    assert calls[0][1:] == (3, record.draft_id)


def test_confirm_fail_closed_when_outcome_missing(monkeypatch) -> None:
    """[안전장치 ⑤] execute 노드가 outcome 없이 반환하는 그래프 상태 이상을 흉내낸다 —
    resume 결과에 outcome 키가 없으면 이전(fail-open, "executed" 기본값) 대신 fail-closed
    (stale)로 처리한다(#621, 사용자 결정: stale 문구 재사용)."""
    spring = _StubSpring()
    set_spring_client(spring)
    record = _record()

    async def _broken_execute_node(state):
        return {}  # outcome/result 둘 다 없음

    monkeypatch.setattr(hitl, "_execute_node", _broken_execute_node)

    async def run():
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "stale"
    assert spring.write_calls() == []
