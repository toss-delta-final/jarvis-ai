"""app/agents/seller/history.py 4-3 분석 이력 검증 — 실 LLM·PG·HTTP 없음.

InMemoryStore 주입으로 저장·조회·planner 주입·"N번 적용해줘" 변환(§6.3)을 검증한다.
설계 확정(2026-07-20): 적용 발화는 입구 코드 선판정(엄격 전체-문장 패턴).
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from datetime import date
from uuid import uuid4

from app.agents.seller import analysis_store, history, hitl
from app.agents.seller.analysis_records import RecommendationRecord, ReportRecord
from app.agents.seller.context import SellerContext
from app.agents.seller.pipeline import parse_apply_message
from app.agents.seller.schemas import (
    ActionRecommendation,
    ProposedChange,
    RecommendationSet,
)
from app.schemas.spring import (
    BehaviorEventsResult,
    BehaviorProductRow,
    SellerProductList,
    SellerProductRow,
)
from app.services.spring_client import set_spring_client

_CTX = SellerContext(seller_id=7, brand_id=3)


class _StoreContext:
    def __init__(self) -> None:
        self.exit_calls = []

    async def __aexit__(self, *args):
        self.exit_calls.append(args)


class _FailingStoreContext:
    async def __aexit__(self, *_args):
        raise RuntimeError("history close failed")


# [이슈 #590] apply_recommendation 참조처가 Store -> analysis_store(DB, 이슈 #585)로
# 바뀌어, 이 테스트 파일의 "N번 적용" 계열 테스트는 analysis_store 를 가짜(in-memory)로
# 대체해야 한다 — 실 PG 연결 없이 _save() 가 심어둔 보고서/추천을 그대로 돌려준다.
_fake_reports: list[ReportRecord] = []
_fake_recs: dict = {}


async def _fake_list_reports(brand_id, *, limit, before=None):
    return [r for r in _fake_reports if r.brand_id == brand_id][:limit]


async def _fake_list_recommendations_by_report(report_id, *, brand_id):
    return [r for r in _fake_recs.get(report_id, []) if r.brand_id == brand_id]


async def _fake_mark_recommendation_applied(rec_id, *, brand_id, draft_id):
    return None


@pytest.fixture(autouse=True)
def _fresh_backends(monkeypatch: pytest.MonkeyPatch):
    """테스트마다 격리된 InMemory store/checkpointer — PG 연결 시도 차단."""
    history.set_store(InMemoryStore())
    hitl.set_checkpointer(InMemorySaver())
    _fake_reports.clear()
    _fake_recs.clear()
    monkeypatch.setattr(analysis_store, "list_reports", _fake_list_reports)
    monkeypatch.setattr(
        analysis_store, "list_recommendations_by_report", _fake_list_recommendations_by_report
    )
    monkeypatch.setattr(
        analysis_store, "mark_recommendation_applied", _fake_mark_recommendation_applied
    )
    yield
    history.set_store(None)
    hitl.set_checkpointer(None)
    set_spring_client(None)


def _rec_set(*recs: ActionRecommendation) -> RecommendationSet:
    return RecommendationSet(recommendations=list(recs), summary="요약")


def _rec(product_id: int = 101, changes: list[ProposedChange] | None = None):
    return ActionRecommendation(
        action_type="price_adjust",
        product_id=product_id,
        title="감귤청 가격 10% 인하",
        rationale="매출 하락 구간과 가격 인상 시점 일치",
        changes=changes if changes is not None else [ProposedChange(field="price", after="13500")],
    )


def test_close_store_closes_context_and_resets_owned_state() -> None:
    close_store = getattr(history, "close_store", None)
    assert callable(close_store)
    ctx = _StoreContext()
    history._store = InMemoryStore()
    history._store_ctx = ctx
    lock = history._save_lock(7)
    assert lock is not None and history._save_locks

    asyncio.run(close_store())

    assert ctx.exit_calls == [(None, None, None)]
    assert history._store is None
    assert history._store_ctx is None
    assert not history._save_locks


def test_close_store_is_safe_before_initialization() -> None:
    close_store = getattr(history, "close_store", None)
    assert callable(close_store)
    history.set_store(None)

    asyncio.run(close_store())
    asyncio.run(close_store())


def test_close_store_propagates_context_close_failure(caplog) -> None:
    history._store = InMemoryStore()
    history._store_ctx = _FailingStoreContext()

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError, match="history close failed"):
        asyncio.run(history.close_store())

    assert "seller history store context cleanup failed" in caplog.text


async def _save(question: str = "지난달 매출 분석", recs: RecommendationSet | None = None):
    resolved_recs = recs if recs is not None else _rec_set(_rec())
    await history.save_history(
        7,
        question=question,
        analyses=["sales_anomaly"],
        date_from="2026-06-01",
        date_to="2026-06-30",
        report="6월 매출 보고서 본문",
        recommendations=resolved_recs,
    )
    # [이슈 #590] apply_recommendation 이 이제 analysis_store 를 읽으므로, Store 저장과
    # 함께 가짜 analysis_store 에도 같은 보고서를 심는다(_fresh_backends 가 monkeypatch).
    report = ReportRecord(
        id=uuid4(),
        brand_id=_CTX.brand_id,
        trigger_type="manual",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        title=f"{question} 보고서",
        summary="6월 매출 보고서 본문",
        report_md="6월 매출 보고서 본문",
        verified=True,
        attempts=1,
    )
    _fake_reports.insert(0, report)
    _fake_recs[report.id] = [
        RecommendationRecord(
            id=uuid4(),
            report_id=report.id,
            brand_id=_CTX.brand_id,
            rank=i + 1,
            action_type=rec.action_type,
            product_ids=[rec.product_id],
            title=rec.title,
            rationale=rec.rationale,
            expected_effect=rec.expected_effect,
            changes=[c.model_dump() for c in rec.changes],
        )
        for i, rec in enumerate(resolved_recs.recommendations)
    ]


_ROW = SellerProductRow(productId=101, name="감귤청", price=15000, stockQuantity=100)


class _StubSpring:
    def __init__(self, rows: list[SellerProductRow] | None = None):
        self.rows = rows if rows is not None else [_ROW]

    async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
        start = offset or 0
        return SellerProductList(rows=self.rows[start : start + (limit or 20)])

    # [#659] 저성과 참고 문구 조회 — 임계 이상으로 두어 이 파일의 기존 반영 안내
    # 텍스트 검증을 건드리지 않는다.
    async def get_events(
        self, brand_id, from_=None, to=None, event_type=None, product_id=None, group_by=None
    ):
        return BehaviorEventsResult(
            rows=[BehaviorProductRow(productId=product_id, salesQuantity=999, counts={})]
        )


# ── parse_apply_message — 입구 ①.5 코드 선판정(엄격 전체-문장) ──────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("1번 적용해줘", 1),
        ("3번 추천 적용해줘", 3),
        (" 2번 적용 ", 2),
        ("5번을 적용해 주세요.", 5),
        ("12번 적용해줘!", 12),
    ],
)
def test_parse_apply_matches_canonical_forms(message: str, expected: int) -> None:
    assert parse_apply_message(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "2번 상품에 할인 적용해줘",  # 여분 토큰 — 일반 수정 요청(라우팅으로)
        "아까 그 두번째 거 적용해줘",  # 숫자 없음
        "적용해줘",
        "0번 적용해줘",  # 1 미만
        '{"action": "confirm", "draftId": "d-1"}',
        "지난달 매출 분석해줘",
    ],
)
def test_parse_apply_rejects_non_canonical(message: str) -> None:
    assert parse_apply_message(message) is None


# ── save/load — 최신순·상한·요약 절단 ───────────────────────────────────────────


def test_save_and_load_recent_newest_first() -> None:
    async def run():
        await _save(question="첫 분석")
        await _save(question="둘째 분석")
        return await history.load_recent(7, 5)

    entries = asyncio.run(run())

    assert [e.question for e in entries] == ["둘째 분석", "첫 분석"]
    assert entries[0].analyses == ["sales_anomaly"]
    assert entries[0].date_from == "2026-06-01"


def test_load_recent_respects_limit_and_isolation() -> None:
    async def run():
        for i in range(7):
            await _save(question=f"분석 {i}")
        other = await history.load_recent(999, 5)  # 다른 판매자 — 격리
        mine = await history.load_recent(7, 5)
        return other, mine

    other, mine = asyncio.run(run())

    assert other == []
    assert len(mine) == 5 and mine[0].question == "분석 6"


def test_save_trims_to_max_items_and_truncates_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    settings = config.Settings(seller_history_max_items=3, seller_history_report_max_chars=5)
    monkeypatch.setattr(history, "get_settings", lambda: settings)

    async def run():
        for i in range(5):
            await _save(question=f"분석 {i}")
        return await history.load_recent(7, 10)

    entries = asyncio.run(run())

    assert len(entries) == 3  # 상한 초과분은 오래된 것부터 폐기
    assert entries[0].report_summary == "6월 매출"[:5]  # 요약 절단


def test_history_store_operations_have_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangReadStore:
        async def aget(self, *args, **kwargs):
            await asyncio.sleep(10)

    class _HangWriteStore:
        async def aget(self, *args, **kwargs):
            return None

        async def aput(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(history.get_settings(), "state_store_query_timeout_s", 0.01)

    async def run() -> None:
        history.set_store(_HangReadStore())
        with pytest.raises(TimeoutError):
            await history.load_recent(7)
        history.set_store(_HangWriteStore())
        with pytest.raises(TimeoutError):
            await _save()

    asyncio.run(run())


def test_concurrent_history_saves_do_not_lose_entries() -> None:
    class _SlowReadStore(InMemoryStore):
        async def aget(self, *args, **kwargs):
            item = await super().aget(*args, **kwargs)
            await asyncio.sleep(0.01)
            return item

    history.set_store(_SlowReadStore())

    async def run():
        await asyncio.gather(_save(question="첫 분석"), _save(question="둘째 분석"))
        return await history.load_recent(7, 5)

    entries = asyncio.run(run())
    assert len(entries) == 2
    assert {entry.question for entry in entries} == {"첫 분석", "둘째 분석"}


# ── build_planner_input — 이력 주입(프롬프트 불변, 입력 메시지만) ────────────────


def test_planner_input_without_history_is_question_verbatim() -> None:
    assert history.build_planner_input("이번 주 매출?", []) == "이번 주 매출?"


def test_planner_input_with_history_appends_block() -> None:
    async def run():
        await _save(question="지난달 매출 분석")
        return await history.load_recent(7)

    entries = asyncio.run(run())
    text = history.build_planner_input("이번 주는?", entries)

    assert text.startswith("[최근 분석 이력]")
    assert "sales_anomaly" in text and "2026-06-01~2026-06-30" in text
    assert text.endswith("[이번 질문] 이번 주는?")


# ── apply_recommendation — §6.3 변환(대화 재해석 금지) ──────────────────────────


def test_apply_converts_recommendation_to_draft_with_current_before() -> None:
    """recommendations[N-1] → draft — before 는 저장값이 아니라 I-9 현재값."""
    set_spring_client(_StubSpring())

    async def run():
        await _save()
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())

    assert problem is None and record is not None
    assert record.op == "update" and record.product_id == 101
    assert record.changes[0].field == "price"
    assert record.changes[0].before == "15000"  # I-9 조회 시점 현재값
    assert record.changes[0].after == "13500"
    # [결정 61] summary 에 출처 보고서를 명시한다 — 판매자가 다른 날짜의 보고서를 보고
    # 있었다면 문구가 달라 승인 전에 알아챌 수 있다("최신 보고서 기준"의 함정 방어).
    assert record.summary == "지난달 매출 분석 보고서 · 1번 — 감귤청 가격 10% 인하"
    assert record.brand_id == 3  # confirm 소유 검증 재료


def test_apply_without_history_asks_for_analysis() -> None:
    record, problem = asyncio.run(history.apply_recommendation(1, _CTX))
    assert record is None and "적용할 만한 분석 추천이 없어요" in problem


def test_apply_out_of_range_reports_valid_range() -> None:
    set_spring_client(_StubSpring())

    async def run():
        await _save(recs=_rec_set(_rec(), _rec(product_id=102)))
        return await history.apply_recommendation(5, _CTX)

    record, problem = asyncio.run(run())
    assert record is None and "1번부터 2번까지" in problem


def test_apply_changeless_recommendation_is_refused() -> None:
    """promotion 등 필드 변경이 없는 추천 — 자동 적용 불가 안내(§6.3-4)."""

    async def run():
        await _save(recs=_rec_set(_rec(changes=[])))
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert record is None and "자동으로 반영할 항목이" in problem


def test_apply_missing_product_is_refused() -> None:
    """짧은 페이지까지 다 돌았는데 없음(exhausted=False) — "찾지 못했어요" 문구."""
    set_spring_client(_StubSpring(rows=[]))

    async def run():
        await _save()
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert record is None and "찾지 못했어요" in problem
    assert hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT not in problem


def test_apply_product_lookup_exhausted_uses_distinct_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#622] 조회 상한 소진(exhausted=True) — "찾지 못했어요" 대신 "확인 못 했다" 문구.

    `hitl._find_product`가 페이지 상한을 소진하도록 page_size/max_pages 를 작게 줄여서
    재현한다(`test_seller_hitl.py::_lookup_page_size`와 같은 패턴).
    """
    real = hitl.get_settings()
    monkeypatch.setattr(
        hitl,
        "get_settings",
        lambda: real.model_copy(
            update={"seller_draft_lookup_page_size": 20, "seller_draft_lookup_max_pages": 2}
        ),
    )
    filler = [_ROW.model_copy(update={"product_id": i, "name": f"상품{i}"}) for i in range(1, 41)]
    set_spring_client(_StubSpring(rows=filler))  # target(101) 없음 + 매 페이지가 꽉 참(20×2)

    async def run():
        await _save()  # 추천 대상 product_id=101 — filler 는 1..40, 101 은 없음
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert record is None
    assert problem == hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT


def test_apply_recommendation_is_brand_scoped_not_seller_scoped() -> None:
    """[#622 결정 — 이슈 ⑥] 보고서는 브랜드 자산이다 — 같은 브랜드의 다른 판매자 계정도
    적용할 수 있다(현행 유지, 좁히지 않기로 명시적으로 결정). apply_recommendation 독스트링
    참조. `_save()`는 seller_id=7 로 저장하지만, 조회는 brand_id 만 쓰므로 seller_id=99인
    다른 계정(같은 brand_id=3)도 동일하게 성공해야 한다."""
    set_spring_client(_StubSpring())
    other_seller_same_brand = SellerContext(seller_id=99, brand_id=3)

    async def run():
        await _save()
        return await history.apply_recommendation(1, other_seller_same_brand)

    record, problem = asyncio.run(run())

    assert problem is None and record is not None
    assert record.op == "update" and record.product_id == 101
    assert record.brand_id == 3
    assert record.seller_id == 99  # confirm 은 이 draft 를 만든 신원 기준으로 저장된다


def test_applied_draft_flows_into_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E: 적용 draft 가 4-2 confirm 흐름에 합류 — I-11 이 추천 after 로 실행된다."""
    from app.schemas.spring import ProductUpdateResult

    class _WritableSpring(_StubSpring):
        def __init__(self):
            super().__init__()
            self.patches = []

        async def update_product(self, brand_id, product_id, patch):
            self.patches.append((brand_id, product_id, patch))
            # [#620] changes 가 비면 already_done 으로 갈음된다 — 이 테스트는 실제
            # 반영(executed)을 검증하므로 비어있지 않은 값을 준다.
            return ProductUpdateResult(productId=product_id, changes=["PRICE"])

    spring = _WritableSpring()
    set_spring_client(spring)

    async def run():
        await _save()
        record, problem = await history.apply_recommendation(1, _CTX)
        assert problem is None
        await hitl.start_draft(record)
        return await hitl.confirm_draft(record.draft_id, seller_id=7, brand_id=3)

    outcome = asyncio.run(run())

    assert outcome.status == "executed"
    assert spring.patches[0][2].price == 13500  # 추천 after 그대로 — 재해석 없음


# ── [#524] 옵션별 재고 상품의 재고 추천 — 초안 생성 전 차단 ─────────────────────


_OPTIONED_ROW = SellerProductRow(
    productId=101,
    name="감귤청",
    price=15000,
    stockQuantity=5,
    stocks=[
        {"optionId": 10, "optionName": "블랙/M", "quantity": 5},
        {"optionId": 11, "optionName": "블랙/L", "quantity": 0},
    ],
)

# 옵션이 하나뿐인 상품. **생성자로 만든다** — model_copy(update=…) 는 검증을 거치지 않아
# dict 를 SellerStockRow 로 바꿔 주지 않는다(실 응답은 항상 _validate 를 타 모델이다).
_SINGLE_OPTION_ROW = SellerProductRow(
    productId=101,
    name="감귤청",
    price=15000,
    stockQuantity=5,
    stocks=[{"optionId": 10, "optionName": "블랙/M", "quantity": 5}],
)


def _stock_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """history 가 보는 wire_mode 만 바꾼다 — 나머지 설정은 실값 그대로."""
    real = history.get_settings()
    monkeypatch.setattr(
        history, "get_settings", lambda: real.model_copy(update={"seller_stock_wire_mode": mode})
    )


def _stock_rec():
    return _rec(changes=[ProposedChange(field="stock_quantity", after="30")])


def test_apply_blocks_stock_recommendation_on_optioned_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """옵션이 둘 이상이면 초안을 만들지 않고 되묻는다 — 승인 뒤 되묻기를 없앤다.

    ProposedChange 에 option_name 이 없어 초안을 만들면 (a) before 가 옵션 합계로
    표시되고 (b) 실행 시점에야 옵션을 못 좁혀 되묻는다. HITL 은 승인 전에 거른다.
    """
    _stock_mode(monkeypatch, "stocks")
    set_spring_client(_StubSpring(rows=[_OPTIONED_ROW]))

    async def run():
        await _save(recs=_rec_set(_stock_rec()))
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert record is None
    assert "옵션별로 재고가 따로 관리되고" in problem
    assert "블랙/M" in problem and "블랙/L" in problem  # 고를 수 있게 나열한다


def test_apply_allows_stock_recommendation_on_single_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """옵션이 하나뿐이면 모호함이 없다 — 종전대로 초안을 만든다."""
    _stock_mode(monkeypatch, "stocks")
    set_spring_client(_StubSpring(rows=[_SINGLE_OPTION_ROW]))

    async def run():
        await _save(recs=_rec_set(_stock_rec()))
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert problem is None and record is not None
    assert record.changes[0].field == "stock_quantity"


def test_apply_stock_recommendation_unchanged_in_quantity_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[회귀 고정] quantity 모드에서는 옵션 판정 자체를 하지 않는다 — 기존 동작 그대로."""
    _stock_mode(monkeypatch, "quantity")
    set_spring_client(_StubSpring(rows=[_OPTIONED_ROW]))

    async def run():
        await _save(recs=_rec_set(_stock_rec()))
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert problem is None and record is not None
    assert record.changes[0].after == "30"


def test_apply_non_stock_recommendation_ignores_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가격 추천은 옵션과 무관 — 옵션이 여럿이어도 막지 않는다."""
    _stock_mode(monkeypatch, "stocks")
    set_spring_client(_StubSpring(rows=[_OPTIONED_ROW]))

    async def run():
        await _save(recs=_rec_set(_rec()))  # price 추천
        return await history.apply_recommendation(1, _CTX)

    record, problem = asyncio.run(run())
    assert problem is None and record is not None
    assert record.changes[0].field == "price"
