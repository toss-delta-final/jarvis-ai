"""추천 파이프라인 스트리밍 (SPEC-RECOMMEND-001 §5.3/§6, 이슈 #2 MVP 슬라이스).

decompose 산출(RouteDecision) 이후: conditions → search(Spring 위임) → rerank(Sonnet) →
근거 token → push(I-21) → products.ready(경로 B) → done.
degrade(§7): SEARCH_FAILED(error·종료) / rerank 실패→검색순서 폴백 / push 실패→products.ready 스킵.
SSE 는 상품 카드를 싣지 않는다(경로 B) — products.ready 는 {sessionId, listId} 상관키만.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from app.agents.buyer._frames import sse
from app.agents.buyer.recommendation.rerank import rerank
from app.agents.buyer.recommendation.state import RouteDecision, build_condition_chips
from app.core.llm import LLMClient, LLMError
from app.services import spring_client
from app.schemas.chat import ConditionsData, DoneData, ErrorData, ProductsReadyData, TokenData
from app.schemas.spring import ProductSearchResult, RecommendationPush
from app.services.spring_client import SpringUnavailableError


async def stream_recommendation(
    *,
    request,
    decision: RouteDecision,
    llm: LLMClient,
    search,
    push_fn,
    identity=None,
    profile: str | None,
    settings,
    get_purchases_fn=None,
    cart_store=None,
    thread_key: str | None = None,
    observer=None,
) -> AsyncIterator[str]:
    """추천 서브그래프 스트림. 프레임(SSE str)을 순서대로 산출한다."""
    # conditions 칩 (병합 필터에서 결정론적 파생)
    chips = build_condition_chips(decision.filters)
    yield sse("conditions", ConditionsData(chips=chips).model_dump(by_alias=True))

    # dedup — 회원이면 최근 구매(I-19)를 조회해 exact productId 제외 목록을 만든다(결정 14-F).
    # I-1 엔 제외 파라미터가 없어(C-15) 검색 응답 뒤 AI 사후필터(search_catalog)로 제외한다.
    # 게스트는 스킵(이력 없음), I-19 실패·비숫자 sub 는 degrade(dedup 없이 진행, §4.7).
    exclude_ids: list[int] | None = None
    if identity is not None and not identity.is_guest and identity.user_id:
        get_purchases_fn = get_purchases_fn or spring_client.get_recent_purchases
        try:
            purchases = await get_purchases_fn(int(identity.user_id))
            ids = purchases.purchased_product_ids()
            exclude_ids = list(ids) if ids else None
        except (SpringUnavailableError, ValueError, TypeError):
            exclude_ids = None

    # search — Spring GET 위임 (§4.6). 실패 시 SEARCH_FAILED(종료).
    try:
        result: ProductSearchResult = await search(decision.filters, exclude_product_ids=exclude_ids)
    except SpringUnavailableError:
        yield sse("error", ErrorData(code="SEARCH_FAILED", message="상품 검색에 실패했어요.").model_dump(by_alias=True))
        return

    candidates = result.products
    if not candidates:
        yield sse("token", TokenData(text="조건에 맞는 상품을 찾지 못했어요. 조건을 조금 바꿔볼까요?").model_dump(by_alias=True))
        yield sse("done", DoneData(finish_reason="zero_result").model_dump(by_alias=True))
        return

    # rerank — Sonnet 1회. 실패/타임아웃/유효후보 0건 시 검색순서 상위 N 으로 degrade(하드 제약 유지).
    if observer is not None:
        observer.record_model_call(settings.sonnet_model_id)
    try:
        rr = await rerank(
            llm,
            query=request.message,
            candidates=candidates,
            profile_summary=profile,
            model=settings.sonnet_model_id,
            expose_max=settings.expose_max,
        )
        ranked_ids = [pid for pid, _ in rr.ranked]
        comment = rr.overall_comment
    except LLMError:
        ranked_ids = [p.product_id for p in candidates[: settings.expose_max]]
        comment = "요청하신 조건으로 찾은 상품들이에요."

    # 노출 개수 보정 — rerank 가 expose_min 미만을 내면 검색순서(하드 제약 반영)로 채우고
    # expose_max 로 상한한다(REQ-REC-021 5~8개 계약, 후보가 부족하면 있는 만큼).
    if len(ranked_ids) < settings.expose_min:
        have = set(ranked_ids)
        for product in candidates:
            if product.product_id not in have:
                ranked_ids.append(product.product_id)
                have.add(product.product_id)
                if len(ranked_ids) >= settings.expose_min:
                    break
    ranked_ids = ranked_ids[: settings.expose_max]

    if comment:
        yield sse("token", TokenData(text=comment).model_dump(by_alias=True))

    # push — I-21(경로 B). 성공 시에만 products.ready emit(§3.3).
    list_id = uuid4().hex
    push = RecommendationPush(session_id=request.session_id, list_id=list_id, product_ids=ranked_ids)
    try:
        pushed = bool(await push_fn(push))
    except SpringUnavailableError:
        pushed = False
    if pushed:
        yield sse("products.ready", ProductsReadyData(session_id=request.session_id, list_id=list_id).model_dump(by_alias=True))
        # 직전 추천을 장바구니 담기(productId 해소, 경로 B)용으로 보관 — **push 성공 후에만**.
        # push 실패로 카드가 노출되지 않았으면 저장하지 않아 "그거 담아줘"가 미노출 상품을 담지 않는다.
        if cart_store is not None and thread_key is not None:
            name_by_id = {p.product_id: p.name for p in candidates}
            cart_store.set_last_reco(thread_key, [(pid, name_by_id.get(pid, "")) for pid in ranked_ids])
    else:
        # push 실패 → products.ready 없음. rerank 코멘트가 "찾았다"고 했으니 목록 지연을 고지하고
        # 정상 종료한다(경로 B 실패 계약 — error 아님, done 유지).
        yield sse("token", TokenData(text="목록을 준비하는 데 문제가 있었어요. 잠시 후 다시 시도해 주세요.").model_dump(by_alias=True))

    yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
