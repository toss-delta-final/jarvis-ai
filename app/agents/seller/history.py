"""판매자 분석 이력 저장소 (4-3 — SPEC-SELLER-001 §9.1·§6.3).

역할 3가지:
1. **save_history**: 분석 파이프라인 성공(kind=report) 시 질문·분석 유형·기간·보고서
   요약·**구조화 recommendations** 를 pg-profile 에 저장한다 — §6.3 "N번 적용해줘"의
   조회 원천이자 planner 이력 주입(§9.1) 재료.
2. **load_recent / build_planner_input**: 최근 N건(seller_history_recent_n)을
   planner **입력 메시지**에 주입한다 — PLANNER_PROMPT 는 불변(2026-07-19 확정).
3. **apply_recommendation**: "N번 적용해줘"(입구 코드 선판정, 2026-07-20 사용자 확정)
   → [이슈 #590] 참조처를 analysis_store(DB, 이슈 #585)의 최신 보고서로 교체 —
   recommendations[N-1] 을 **대화 재해석 없이** DraftProposal 로 변환(rec_id 주입,
   07 결정 49), before 는 I-9 현재값으로 채워 4-2 HITL 경로(validate→start_draft→confirm)에
   합류. 1·2번 역할(Store 저장·조회)은 이번 이슈에서 그대로 유지 — Store 폐지는
   11-MIGRATION.md 결정 108 대로 별도(Phase 2) 이슈 소관이다.

저장 구조(SPEC §9.1 각색): AsyncPostgresStore(pg-profile) 네임스페이스
("sellers", {sellerId}) + 키 "analysis_history" 에 **최신순 목록 1건**으로 보관 —
per-item 키 대신 단일 목록을 쓰는 이유는 (a) "N번"의 기준인 '가장 최근 분석'과
'최근 N건' 조회가 원자적 1회 읽기가 되고 (b) store.asearch 의 정렬 비보장을 피하기
위해서다(정렬을 코드가 소유). 상한 seller_history_max_items 로 잘라 무한 성장 방지.
동시 분석의 읽고-쓰기는 seller별 mutation_lock으로 직렬화해 이력 유실을 막는다.
워커별 탐지 상세 테이블(analysis_detections)은 4-3 범위 밖(SPEC §9.1, 고도화).

checkpointer(hitl.py)와 동일한 dev 폴백 규약: pg-profile 연결 실패 시 InMemoryStore
+ 경고 1회, 운영(auth_mode=jwks)은 폴백 금지.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field

from app.agents.seller import analysis_store, hitl
from app.agents.seller.context import SellerContext
from app.agents.seller.schemas import (
    DraftChange,
    DraftProposal,
    ProposedChange,
    RecommendationSet,
)
from app.agents.seller.stock_options import option_labels
from app.core.config import get_settings
from app.schemas.spring import SellerProductRow
from app.core.pg_resilience import (
    hardened_pg_conninfo,
    mutation_lock,
    run_with_query_timeout,
    state_store_pool_config,
)

logger = logging.getLogger(__name__)

_NAMESPACE_ROOT = "sellers"
_HISTORY_KEY = "analysis_history"


class HistoryEntry(BaseModel):
    """분석 이력 1건 — recommendations 는 §6.3 조회를 위해 **구조화 그대로** 보존한다."""

    question: str
    analyses: list[str] = Field(default_factory=list)
    date_from: str  # ISO date — 분석 기간(planner 주입·4-4 캐시 동일 기간 판정 재료)
    date_to: str
    report_summary: str  # 전문이 아닌 요약(절단) — SPEC §9.1, 전문 재활용은 4-4 캐시 소관
    recommendations: dict = Field(default_factory=dict)  # RecommendationSet dump(순서=N번 계약)
    created_at: str  # ISO8601(UTC)


# ── store 싱글턴 (hitl.checkpointer 와 동일 규약 — set_store 테스트 주입) ─────────

_store: BaseStore | None = None
_store_ctx: object | None = None  # AsyncPostgresStore cm — 앱 수명 동안 GC 방지
_fallback_warned = False
_save_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_pending_cleanup: list[object] = []


def _save_lock(seller_id: int) -> asyncio.Lock:
    lock = _save_locks.get(seller_id)
    if lock is None:
        lock = asyncio.Lock()
        _save_locks[seller_id] = lock
    return lock


def set_store(store: BaseStore | None) -> None:
    """store 교체(테스트용) — 기존 ctx 정리는 다음 async 진입으로 미룬다."""
    global _store, _store_ctx
    old_ctx = _store_ctx
    _store = store
    _store_ctx = None
    _save_locks.clear()
    if old_ctx is not None:
        _pending_cleanup.append(old_ctx)


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
    """이전 store ctx를 닫되 현재 태스크의 실제 취소만 다시 전파한다.

    sync `set_store()`는 ctx를 직접 await-close 할 수 없어 대기열로 넘긴다. 이전 이벤트
    루프에 묶인 stale ctx의 `__aexit__()`가 `CancelledError`를 낼 수 있지만, 이를 무조건
    삼키면 현재 종료 태스크 자체의 실제 취소까지 무시된다. `task.cancelling()`으로 둘을
    구분해 실제 취소 요청만 다시 던진다(`app/core/pg_store.py`와 같은 근거).
    """
    first_error: Exception | None = None
    while _pending_cleanup:
        ctx = _pending_cleanup.pop()
        try:
            await ctx.__aexit__(None, None, None)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as exc:
            logger.warning("seller history store context cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_store() -> None:
    """지금 열린 분석 이력 store ctx를 이 이벤트 루프에서 닫는다 (이슈 #221)."""
    set_store(None)
    await _drain_pending_cleanup(propagate_errors=True)


async def _get_store() -> BaseStore:
    """AsyncPostgresStore(pg-profile) 지연 초기화 — 실패 시 dev 한정 InMemoryStore 폴백."""
    global _store, _store_ctx, _fallback_warned
    await _drain_pending_cleanup()
    if _store is None:
        settings = get_settings()
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            ctx = AsyncPostgresStore.from_conn_string(
                hardened_pg_conninfo(settings.profile_db_url),
                pool_config=state_store_pool_config(),
            )
            store = await asyncio.wait_for(
                ctx.__aenter__(), timeout=settings.seller_checkpoint_connect_timeout_s
            )
            await store.setup()
            _store_ctx = ctx
            _store = store
        except Exception as exc:
            if settings.auth_mode == "jwks":
                raise  # 운영 — 폴백 금지(이력·추천 적용이 조용히 증발하면 안 된다)
            if not _fallback_warned:
                logger.warning(
                    "pg-profile store 연결 실패(%s) — InMemoryStore 폴백 "
                    "(dev 전용: 프로세스 재시작 시 분석 이력 증발)",
                    exc,
                )
                _fallback_warned = True
            _store = InMemoryStore()
    return _store


def _namespace(seller_id: int) -> tuple[str, str]:
    # store 네임스페이스 원소는 str — 숫자 신원(§2.6)을 저장 키로만 문자열화한다.
    return (_NAMESPACE_ROOT, str(seller_id))


# ── 저장·조회 ────────────────────────────────────────────────────────────────────


async def save_history(
    seller_id: int,
    *,
    question: str,
    analyses: list[str],
    date_from: str,
    date_to: str,
    report: str,
    recommendations: RecommendationSet,
) -> None:
    """분석 1건을 최신순 목록 맨 앞에 저장한다 (compose 후 — orchestrator 소관 호출).

    보고서는 seller_history_report_max_chars 로 절단(요약) — planner 주입·이력
    맥락 용도라 전문이 필요 없다. 목록은 seller_history_max_items 로 자른다.
    """
    settings = get_settings()
    entry = HistoryEntry(
        question=question,
        analyses=analyses,
        date_from=date_from,
        date_to=date_to,
        report_summary=report[: settings.seller_history_report_max_chars],
        recommendations=recommendations.model_dump(),
        created_at=datetime.now(UTC).isoformat(),
    )
    store = await _get_store()
    namespace = _namespace(seller_id)
    async with mutation_lock(
        store,
        f"seller:history:{seller_id}",
        _save_lock(seller_id),
    ):
        item = await run_with_query_timeout(store.aget(namespace, _HISTORY_KEY))
        items: list[dict] = list(item.value.get("items", [])) if item else []
        items.insert(0, entry.model_dump())
        del items[settings.seller_history_max_items :]
        await run_with_query_timeout(store.aput(namespace, _HISTORY_KEY, {"items": items}))


async def load_recent(seller_id: int, n: int | None = None) -> list[HistoryEntry]:
    """최근 n건(기본 seller_history_recent_n)을 최신순으로 반환한다."""
    limit = n if n is not None else get_settings().seller_history_recent_n
    store = await _get_store()
    item = await run_with_query_timeout(store.aget(_namespace(seller_id), _HISTORY_KEY))
    if not item:
        return []
    return [HistoryEntry.model_validate(raw) for raw in item.value.get("items", [])[:limit]]


# ── planner 이력 주입 (§9.1 — 프롬프트 불변, 입력 메시지에만 주입) ────────────────


def build_planner_input(question: str, entries: list[HistoryEntry]) -> str:
    """planner 입력 메시지 조립 — 이력이 없으면 질문 원문 그대로(기존 계약 불변).

    이력 블록은 참고 맥락일 뿐 분류 대상이 아님을 라벨([이번 질문])로 구분한다.
    """
    if not entries:
        return question
    lines = ["[최근 분석 이력]"]
    for entry in entries:
        day = entry.created_at[:10]
        lines.append(
            f"- {day} {'+'.join(entry.analyses)} ({entry.date_from}~{entry.date_to}) "
            f"질문: {entry.question}"
        )
    return "\n".join(lines) + f"\n\n[이번 질문] {question}"


# ── "N번 적용해줘" → draft 변환 (§6.3 — 대화 재해석 금지, 조회·변환은 전부 코드) ──
# [이슈 #590] 참조처를 Store(analysis_history) 최근 1건에서 analysis_store(DB, 이슈 #585)
# 최신 보고서로 교체한다. Store 자체(save_history/load_recent)는 이번엔 그대로 둔다 —
# 폐지는 11-MIGRATION.md 결정 108 대로 별도(Phase 2) 이슈 소관이다.


def _current_value_str(row: object, field: str) -> str:
    value = getattr(row, field, None)
    return "" if value is None else str(value)


def _option_stock_blocker(
    title: str, changes: list[ProposedChange], row: SellerProductRow
) -> str | None:
    """옵션별 재고 상품의 재고 추천은 초안을 만들지 않는다 — 되묻기 문구 (#524).

    `ProposedChange` 에는 `option_name` 이 없다(추천 스키마는 옵션 개념보다 먼저
    확정됐다). 그대로 초안을 만들면 두 가지가 어긋난다:

    1. `before` 가 `row.stock_quantity` 즉 **옵션 합계**로 표시된다 — 추천이 의도한
       단일 재고와 층위가 달라 카드가 거짓을 보여준다.
    2. 실행 시점에야 `resolve_stock_option` 이 옵션을 못 좁혀 되묻는다 — 판매자가
       **[적용]을 누른 뒤에** 질문을 받는다.

    HITL 은 승인 전에 거르는 장치다. 승인 후 되묻기는 "보여준 것 == 실행하는 것" 보장을
    무르게 만든다 — 그래서 여기서 막고 대화로 되돌린다. quantity 모드에서는 옵션 자체가
    와이어에 없으므로 이 판정을 하지 않는다(기존 동작 불변).
    """
    if get_settings().seller_stock_wire_mode != "stocks":
        return None
    if not any(change.field == "stock_quantity" for change in changes):
        return None
    named = [stock for stock in row.stocks if stock.option_id is not None]
    if len(named) <= 1:
        return None
    return (
        f"'{title}' 추천은 재고를 바꾸는 내용인데, 이 상품은 옵션별로 재고가 따로 "
        f"관리되고 있어요({' · '.join(option_labels(row.stocks))}). 어느 옵션의 재고를 "
        "얼마로 바꿀지 알려주시면 바로 초안을 만들어 드릴게요."
    )


async def apply_recommendation(
    n: int, context: SellerContext
) -> tuple[hitl.DraftRecord | None, str | None]:
    """최신 보고서의 추천 N번을 DraftRecord 로 변환 — 불성립은 (None, 안내).

    [이슈 #590] §6.3 절차 — analysis_store(DB, 이슈 #585)에서 브랜드의 최신 보고서 1건과
    그 추천 전체(rank 순)를 조회 → 인덱스 검증 → before 를 I-9 현재값으로 채워
    DraftProposal 구성(rec_id 주입, 07 결정 49) → hitl.validate_draft(4-2 재사용).
    조회 실패·인덱스 불일치·적용 불가 유형은 실행하지 않고 안내 문구를 돌려준다(되묻기).
    Spring 장애는 전파(호출부 error 경로).

    [이슈 #622 결정] 조회는 의도적으로 브랜드 축(`brand_id`)만 쓴다 — **보고서는 브랜드
    자산**으로 취급한다. 같은 브랜드에 판매자 계정이 여럿이면 A가 만든 보고서의 추천을
    B가 이 함수로 초안화할 수 있다(승인은 `hitl.confirm_draft`가 자기 draftId로 독립
    검증하므로 그대로 통과) — 이는 사고가 아니라 명시적 결정이다. 좁히려면(추천을 요청한
    판매자만 적용 가능) `analysis_store.list_reports`·`list_recommendations_by_report`에
    `seller_id` 축을 추가해야 하는데, 이번 이슈에서는 채택하지 않았다. summary 에 출처
    보고서를 명시(결정 61, 아래)해 최소한의 추적성만 확보한다.
    """
    reports = await analysis_store.list_reports(context.brand_id, limit=1)
    if not reports:
        return None, (
            "아직 적용할 만한 분석 추천이 없어요. 먼저 분석을 요청해 주시면 추천을 만들어 드릴게요."
        )
    report = reports[0]
    items = await analysis_store.list_recommendations_by_report(
        report.id, brand_id=context.brand_id
    )
    if not items:
        return (
            None,
            "가장 최근 분석에는 적용할 만한 추천이 없었어요. 새로 분석을 요청해 주시겠어요?",
        )
    if not 1 <= n <= len(items):
        return None, (
            f"최근 분석의 추천은 1번부터 {len(items)}번까지 있어요. 몇 번을 적용해 드릴까요?"
        )
    rec = items[n - 1]
    changes = [ProposedChange.model_validate(c) for c in rec.changes]
    if not changes:
        return None, (
            f"'{rec.title}' 추천은 자동으로 반영할 항목이 딱히 없는 유형이에요. "
            "구체적으로 무엇을 바꾸고 싶으신지 말씀해 주시면 초안을 만들어 드릴게요."
        )
    product_id = rec.product_ids[0] if rec.product_ids else None
    if product_id is None:
        return None, (
            f"'{rec.title}' 추천에는 대상 상품이 따로 지정돼 있지 않네요. "
            "어느 상품을 바꿀지 알려주시면 초안을 만들어 드릴게요."
        )

    row, exhausted = await hitl._find_product(context.brand_id, product_id)
    if row is None:
        if exhausted:
            # [#622] 상한 소진 — 상품이 많아 못 찾은 것뿐이라 "삭제됨"으로 단정하지 않는다.
            return None, hitl.PRODUCT_LOOKUP_EXHAUSTED_TEXT
        # [#622] 문구를 hitl._execute_draft 의 판단("이미 삭제되었거나 다른 브랜드로
        # 옮겨진 것 같습니다")과 통일한다 — #590 전에는 이 함수만 "삭제되었을 수 있어요"라는
        # 더 부정확한 자체 문구를 달고 있었다.
        return None, (
            f"추천에 있던 상품을 목록에서 찾지 못했어요(상품ID {product_id}). 이미 "
            "삭제됐거나 다른 브랜드로 옮겨진 것 같아요 — 상품을 다시 확인한 뒤 "
            "요청해 주시겠어요?"
        )

    if problem := _option_stock_blocker(rec.title, changes, row):
        return None, problem

    proposal = DraftProposal(
        op="update",
        product_id=product_id,
        changes=[
            DraftChange(
                field=change.field,
                before=_current_value_str(row, change.field),  # 조회 시점 현재값 = diff 기준
                after=change.after,
            )
            for change in changes
        ],
        # [결정 61] 출처 보고서를 요약에 명시 — 판매자가 다른 날짜의 보고서를 보고 있었다면
        # 문구가 달라 승인 전에 알아챌 수 있다("최신 보고서 기준"의 함정 방어).
        summary=f"{report.title} · {n}번 — {rec.title}",
        rec_id=str(rec.id),
    )
    # [#620] row 는 어차피 이 함수가 위에서 이미 조회했다 — validate_draft 의 price
    # 선차단(row-aware)에 그대로 넘긴다(추가 Spring 왕복 없음).
    return hitl.validate_draft(
        proposal, seller_id=context.seller_id, brand_id=context.brand_id, row=row
    )
