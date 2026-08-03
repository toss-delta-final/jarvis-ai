"""구매자 챗봇 그래프 진입점 (SPEC-RECOMMEND-001, 이슈 #2 MVP 슬라이스).

흐름 (product.md 결정 12-A / structure.md §3):
    entry → 프로필 조회(reader, 동기) → decompose(Haiku 1회, intent 라우팅) →
        - recommend: 추천 서브그래프(decompose→search(Spring 위임)→rerank→push, 경로 B)
        - order_status: 검증 JWT 회원 신원으로 I-4 조회 후 결정적 token→done
        - general  : fallback 서브그래프(일반 대화)

멀티턴: 스레드별 누적 필터를 ThreadFilterStore(LangGraph BaseStore, pg-profile)에 신원 스코프
키로 보관한다 — app/agents/seller/history.py 와 동일한 BaseStore 이관 패턴(이슈 #33, §6.3).
장바구니 서브그래프(결정 7, I-2/I-18)는 이슈 #3 소관 — 본 슬라이스 미포함.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.graph import stream_cart_add, stream_cart_view
from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.fallback import stream_fallback
from app.agents.buyer.order_status import stream_order_status
from app.agents.buyer.recommendation.category_mapping import CategoryMapping, dedup_truncate
from app.agents.buyer.recommendation.category_mapping import map_categories as _map_categories
from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.needs_expansion import detect_expansion_need
from app.agents.buyer.recommendation.needs_expansion import expand_needs as _expand_needs
from app.agents.buyer.recommendation.state import get_revert_store
from app.agents.buyer.recommendation.graph import stream_recommendation
from app.agents.profile.builder import record_remember
from app.agents.buyer.session_state import context_thread_key, ensure_thread_adopted
from app.agents.profile.gate import is_remember_command
from app.agents.profile.reader import read_profile_summary
from app.agents.profile.store import get_profile_store
from app.core import pg_store
from app.api.deps import buyer_owner_id
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core.errors import new_request_id
from app.core.llm import LLMError, get_llm, resolve_model_id
from app.core.pg_resilience import run_with_query_timeout
from app.core.tracing import current_request_trace, trace_span
from app.core.session_context import SessionStateUnavailable
from app.core.text import _strip_unsafe
from app.agents.buyer.recommendation.state import CartIntent, CategoryQuery
from app.schemas.chat import DoneData, ErrorData
from app.schemas.spring import ProductSearchFilters
from app.services import search_service, spring_client

logger = logging.getLogger(__name__)

_NAMESPACE_ROOT = "buyer_thread_filters_v2"
_FILTERS_KEY = "filters"


class ThreadFilterStore:
    """스레드별 누적 필터(멀티턴) — LangGraph BaseStore(pg-profile) 백엔드.

    키는 신원 스코프(conversation_key: owner:thread_id) — 타인 스레드 필터 열람 금지(IDOR 방지, §2.6).
    """

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    async def get(self, key: str) -> ProductSearchFilters | None:
        item = await run_with_query_timeout(self._store.aget((_NAMESPACE_ROOT, key), _FILTERS_KEY))
        return ProductSearchFilters.model_validate(item.value) if item else None

    async def put(self, key: str, filters: ProductSearchFilters) -> None:
        await run_with_query_timeout(
            self._store.aput((_NAMESPACE_ROOT, key), _FILTERS_KEY, filters.model_dump())
        )


async def get_thread_store() -> ThreadFilterStore:
    """스레드 필터 스토어 — pg-profile 공유 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return ThreadFilterStore(await pg_store.get_store())


def reset_thread_store() -> None:
    """테스트 격리용 — 공유 pg-profile store(InMemoryStore)를 비운다."""
    pg_store.reset_store()


def _is_timeout(exc: Exception) -> bool:
    """LLMError 메시지에서 타임아웃 여부를 추정한다(LLM_TIMEOUT vs LLM_UNAVAILABLE 매핑용)."""
    return "timeout" in str(exc).lower()


async def _map_or_empty(
    mapper, queries, utterance, settings, llm, observer, *, select_max_calls: int | None = None
) -> CategoryMapping:
    """매핑 1회 — 호출 자체의 예외는 **빈 결과**로 흡수한다(canonical-or-null 불변식).

    embed/DB 실패는 `map_categories` 내부에서 leg 단위로 격리된다(exact 보존·§5·#20). 여기까지
    오는 건 호출 자체의 버그(시그니처 불일치 등)라 raw(DB 미검증)를 신뢰할 근거가 없다 — 빈 legs
    로 degrade 해(→ `filters.category=None`) 미검증 원문이 Spring·조건 칩·멀티턴 승계로 새지
    않게 한다(PR #73 리뷰). 관측 로그는 남긴다.

    `unresolved` 도 비운다 — 매핑이 성립하지 않았으므로 "발화가 매핑에 실패했다"는 판정을 낼 근거가
    없다. 여기서 채우면 인프라·코드 오류가 LLM 전개를 부른다(§4 ③ 과 같은 원칙).

    #217 로 이 함수가 **턴당 최대 2회**(원 legs·전개 legs) 호출된다. 늘어난 호출이 **어떤 예산도
    두 배로 만들지 않는지**를 리소스별로 따진다:

    - **pg 커넥션**: 두 호출이 순차라 동시 앵커 수는 그대로다 —
      `config._require_pool_covers_anchor_concurrency` 의 `pool >= 2 × fanout_max` 전제 유지.
    - **택일 LLM 호출**(PR 리뷰): `category_select_max_calls` 는 **턴당** 상한인데 매핑 내부에서는
      호출 단위로 적용된다. 그대로 두면 턴당 상한이 2배로 깨지므로 호출부가 **남은 예산을 계산해
      넘긴다**(`select_max_calls`). 첫 호출이 쓴 몫은 `CategoryMapping.select_calls` 로 돌아온다.
    - **임베딩·pg 왕복**: 1회 추가는 이 설계가 의도한 비용이다(§8).
    """
    try:
        return await mapper(
            category_queries=queries,
            utterance=utterance,
            settings=settings,
            # None 이면 매퍼가 settings 기본값을 쓴다(첫 호출). 두 번째 호출은 남은 몫을 받는다.
            select_max_calls=select_max_calls,
            # [#115 §4.4] 마진이 얇은 leg 만 top-k 택일에 쓰는 조건부 LLM — 정상 경로는 0회다.
            # llm=None 이면 매퍼가 택일을 건너뛰고 임베딩 top-1 을 쓴다(LLM 종속 없음).
            # tier 는 decompose 와 동일 fast — 후보 중 택일은 경량 판정이다(§4.4).
            llm=llm,
            tier="fast",
            # 택일 호출도 chat_request 모델 집계(§6.3)에 실어야 한다 — 기록은 모델을 실제로
            # 부르는 select_category 안에서 하므로 여기 책임은 seam 까지 전달하는 것뿐이다.
            observer=observer,
        )
    except Exception as exc:  # noqa: BLE001 - 매핑 호출 자체의 예외(시그니처 불일치·버그 등)
        logger.warning("category_map_failed", extra={"reason": str(exc)})
        return CategoryMapping()


async def _prepare_recommendation(
    *,
    request,
    decision,
    prior,
    llm,
    settings,
    map_categories,
    expand_needs,
    observer,
    thread_store: ThreadFilterStore,
    thread_key: str,
) -> frozenset[str]:
    """Prepare mapped recommendation state inside the recommendation graph span."""
    # recommend — 카테고리 하이브리드 매핑(이슈 #59, 방식 A): decompose 추측을 canonical 로
    # 보정(canonical-or-null). 매핑이 죽거나 신호가 없으면 category 없이(전체) 검색으로 degrade.
    if (
        prior is not None
        and prior.category
        and not any(q.raw_category or q.query for q in decision.category_queries)
    ):
        # 리파인 턴(예: "더 저렴한 걸로") — 이번 턴에 카테고리 신호가 전혀 없음(빈 리스트, 또는
        # raw·query 가 모두 없는 leg 만). prior 는 이미 canonical(§7)이라 재매핑(pg 왕복) 없이 그대로
        # 승계한다. 매핑에 태우면 신호가 없어 빈 legs 가 나오고(#22), 아래 else 의 category=None 으로
        # 직전 카테고리가 지워진다 — 리파인인데 필터가 풀려버린다(PR #73 #12).
        # 단, raw 는 null 이라도 유의미한 query 가 있으면(신규 상황형 질의) 검색 의도가 있는 것이라
        # 아래 매핑을 태워야 한다 — prior 로 하이재킹하면 fan-out 이 죽고 #59 문제가 재발(PR #73 #19).
        decision.category_legs = [(prior.category, None)]
    else:
        # [#198·#217] 목적·상황형 발화의 상품 전개 — **승계 가드 안쪽(else)에 둔다**. D1(`no_legs`)은
        # 리파인 턴("더 저렴한 걸로")의 "신호 없음"과 조건이 겹치므로, 전개를 위 if 보다 앞에 놓으면
        # 리파인 턴이 엉뚱한 상품 목록으로 바뀌어 직전 맥락이 날아간다(PR #73 #12/#19 승계 규약이
        # 반대 방향으로 깨진다). 여기서는 이미 "승계 대상 아님"이 확정돼 있다.
        #
        # [#217] 순서가 뒤집혔다 — **매핑을 먼저** 돌리고 그 실패를 전개 트리거로 쓴다(§4·§6.1).
        # 초판은 목적 marker 열거로 매핑 전에 미리 맞혔는데, 열거는 목록에 없는 표현을 놓치고
        # 목록을 늘리면 이미 정답 매핑되는 표현이 파괴됐다(§4.0).
        mapper = map_categories or _map_categories
        mapping = await _map_or_empty(
            mapper, decision.category_queries, request.message, settings, llm, observer
        )
        if settings.needs_expansion_enabled:
            reason = detect_expansion_need(
                decision.category_queries,
                # case 는 게이트로만 쓴다(§4.2) — case 2("5만원 이하 아무거나")는 legs 가 비어 D1 에
                # 걸리고, 조건형 leg("평점 높은 거")은 taxonomy 에 맞는 칸이 없어 매핑 실패로 D2 에
                # 걸린다. 둘 다 처방은 정반대다(#22·#162 무필터 보존).
                case=decision.case,
                unresolved=mapping.unresolved,
            )
            if reason:
                logger.info(
                    "needs_expansion_triggered",
                    extra={
                        "reason": reason,
                        "legs": len(decision.category_queries),
                        # 어떤 앵커가 왜 실패했는지 — 하류 category_distance_rejected·
                        # category_select_null 의 거리·마진과 조인해 임계를 재튜닝한다(§10).
                        "unresolved": mapping.unresolved,
                    },
                )
                expander = expand_needs or _expand_needs
                # observer 는 전개기까지 내려보낸다 — 모델 호출을 하는 쪽이 기록해야(§6.3) LLM 을
                # 쓰지 않는 전개기(방식 B·C)에 유령 호출이 남지 않는다.
                items = await expander(
                    request.message, llm=llm, settings=settings, observer=observer
                )
                # 실패(빈 리스트)면 원 매핑 결과를 그대로 둔다 — 전개는 개선 시도이며 실패가 기존
                # 경로를 악화시키지 않는다(설계 §7 후퇴 없음).
                if items:
                    # raw 는 싣지 않는다 — 매핑이 query 우선이라(#115 §4.3.1) raw 는 폴백일 뿐이고,
                    # 창작 라벨은 표기 불일치·가짜 근접으로 해가 더 크다.
                    expanded = await _map_or_empty(
                        mapper,
                        [CategoryQuery(None, name) for name in items],
                        request.message,
                        settings,
                        llm,
                        observer,
                        # 택일 예산은 **턴당**이라 첫 매핑이 쓴 몫을 빼고 넘긴다(PR 리뷰) — 안 그러면
                        # 상한이 2배로 깨진다. 0 이면 매퍼가 택일을 건너뛰고 임베딩 top-1 을 쓴다.
                        select_max_calls=max(
                            0, settings.category_select_max_calls - mapping.select_calls
                        ),
                    )
                    # **합집합**(§6) — 원 leg 을 **앞에** 둬 fanout_max 절단에서 사용자가 명시한
                    # 카테고리가 먼저 살아남게 한다. 종전 교체 배선은 전개가 트리거되면 성공한 leg
                    # 까지 날렸다("냉장고랑 필요한 것들" → 냉장고 유실).
                    # 재전개는 하지 않는다 — `expanded.unresolved` 를 다시 트리거로 쓰면 전개가
                    # 전부 실패하는 회차(§4.5 ③)에서 턴이 끝나지 않는다.
                    merged = dedup_truncate(
                        mapping.legs + expanded.legs, settings.category_fanout_max
                    )
                    # 택일 소비는 **두 호출의 합**이다 — 상한이 턴당이므로 사후 검증도 턴 단위여야
                    # 한다. 로그에 실어 "상한이 실제로 지켜졌나"를 운영에서 확인할 수 있게 한다.
                    select_used = mapping.select_calls + expanded.select_calls
                    logger.info(
                        "needs_expansion_union",
                        extra={
                            "base_legs": len(mapping.legs),
                            "expanded_legs": len(expanded.legs),
                            "merged_legs": len(merged),
                            "select_calls": select_used,
                        },
                    )
                    # `replace` 로 합친다 — 필드를 나열해 새로 만들면 이번처럼 새 필드
                    # (`select_calls`)가 조용히 기본값으로 리셋된다. `unresolved` 는 첫 매핑 것을
                    # 그대로 둔다(재전개 금지, 위 주석).
                    mapping = replace(mapping, legs=merged, select_calls=select_used)
        decision.category_legs = mapping.legs
    if decision.category_legs:
        # 대표 canonical — 단일 filters.category 필드·조건 칩·멀티턴 승계 호환(§7).
        decision.filters.category = decision.category_legs[0][0]
    else:
        # 매핑 결과 없음 → LLM 이 echo 했을 수 있는 미검증 filters.category 를 비운다. category 는
        # 이제 전적으로 category_legs(canonical) 경유로만 흐른다 — 미시드·매핑 실패 시에도 보정 안 된
        # 원문이 Spring 검색·조건 칩으로 새지 않게(PR #73 리뷰 #13/#15).
        decision.filters.category = None

    # 멀티턴 병합 필터는 추천 intent 에서만 저장(담기/조회가 덮어쓰지 않게).
    await thread_store.put(thread_key, decision.filters)
    # 소모품 억제 되돌리기(결정 14-F) — 이번 턴 revert + 스레드 누적을 합쳐 억제 제외.
    # LLM 이 뽑은 임의 문자열을 무한 누적하지 않게 소모품 화이트리스트(억제 대상)와 대조해 통과분만 저장.
    revert_store = await get_revert_store()
    # SSE에는 정제된 category를 싣지만 내부 억제 키는 Spring 원본과 같아야 한다.
    # 정제값→원본 화이트리스트로 되매핑해 "보여준 revert 값"의 round-trip을 보존한다.
    consumable_by_exposed = {
        _strip_unsafe(category): category for category in settings.consumable_categories
    }
    await revert_store.add(
        thread_key,
        [
            consumable_by_exposed[exposed]
            for category in decision.revert_categories
            if (exposed := _strip_unsafe(category)) in consumable_by_exposed
        ],
    )
    reverted = await revert_store.get(thread_key)
    return frozenset(reverted)


async def run_buyer_turn(
    request,
    identity,
    *,
    llm=None,
    search=None,
    push_fn=None,
    map_categories=None,
    order_status_fn=None,
    expand_needs=None,
    observer=None,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """구매자 1턴을 SSE 프레임으로 스트리밍한다(open_stream 이 감싸는 inner).

    llm/search/push_fn/map_categories 미지정 시 라이브 기본값 — 테스트는 fake 를 주입한다.
    LLM 미구성(개발·CI)이면 네트워크 호출 없이 곧바로 LLM_UNAVAILABLE error 를 낸다.
    """
    settings = get_settings()
    resolved_request_id = cast(
        str, request_id or getattr(observer, "request_id", None) or new_request_id()
    )
    # lifecycle authority가 원자적 turn 저장에서 확정한 context만 buyer 상태 키로 사용한다.
    # raw owner/session 식별자는 상태 키나 로그 상관키로 재사용하지 않는다.
    # 검증은 LLM/상태 접근보다 먼저 수행해 서명 세션 실패가 200 SSE로 완화되지 않게 한다.
    context_id = getattr(observer, "context_id", None)
    if not isinstance(context_id, str) or not context_id:
        raise SessionStateUnavailable
    await ensure_thread_adopted(
        context_id,
        request.thread_id,
        buyer_owner_id(identity, settings),
    )
    thread_key = context_thread_key(context_id, request.thread_id)

    llm = llm or get_llm()
    if llm is None:
        yield sse(
            "error",
            ErrorData(
                code="LLM_UNAVAILABLE",
                message="LLM 이 구성되지 않았어요.",
                request_id=resolved_request_id,
                retryable=False,
            ).model_dump(by_alias=True),
        )
        return
    search = search or search_service.search_catalog
    push_fn = push_fn or spring_client.push_recommendations
    thread_store = await get_thread_store()
    prior = await thread_store.get(thread_key)

    # 프로필 주입 (회원만, read-only) — 게스트/신규는 None(개인화 스킵, 결정 8)
    profile = None
    profile_eligible = bool(not identity.is_guest and identity.user_id and not identity.seller_id)
    if profile_eligible:
        summary = await read_profile_summary(identity.user_id)
        profile = summary.get("markdown") if summary else None
        # "기억해"류 명시 명령은 게이트 없이 즉시 승격(hot-path, REQ-PROF). intent 와 무관한
        # 명시 명령이라 라우팅 앞에 둔다 — decompose 가 실패한 턴에도 기록돼야 한다.
        if is_remember_command(request.message):
            await record_remember(identity.user_id, request.message)

    # 장바구니 문맥 — 직전 추천(담기 productId 해소)·옵션 되물음 대기 상태.
    cart_store = await get_cart_store()
    pending = await cart_store.get_pending(thread_key)
    pending_dict = None
    if pending is not None:
        pending_dict = {
            "productId": pending.product_id,
            "options": [{"optionId": o.option_id, "name": o.name} for o in pending.options],
        }

    # decompose — fast tier 1회 (intent 5-way 라우팅 + 필터 + 장바구니 의도)
    if observer is not None:
        observer.record_model_call(resolve_model_id(settings, "fast"))
    last_reco = await cart_store.get_last_reco(thread_key)
    try:
        with trace_span("buyer.routing", "chain"):
            with trace_span(
                "llm.decompose",
                "llm",
                {"model": resolve_model_id(settings, "fast")},
            ):
                decision = await decompose(
                    llm,
                    query=request.message,
                    prior_filters=prior,
                    # [#119] 프로필은 **후보를 줄이는 단계에 넣지 않는다**(REQ-REC-005-A).
                    # decompose 는 하드필터(WHERE 술어)를 산출하는데, 프로필을 발화와 같은 격으로
                    # 주면 LLM 이 "3~5만원대 선호"를 priceMax 로 승격시키고 그 필터가
                    # thread_store 에 영속돼 다음 턴 PRIOR_FILTERS 로 재주입된다(세션 내 래칫).
                    # 게스트는 그 손실이 없어 개인화가 순손실이 됐다 — 주입을 끊으면 회원
                    # 프롬프트가 게스트와 바이트 동일해진다. 취향은 rerank 순서로만 반영한다.
                    profile_summary=(
                        profile if settings.profile_injection_scope == "both" else None
                    ),
                    tier="fast",
                    last_recommendations=last_reco,
                    pending_cart=pending_dict,
                    category_fanout_max=settings.category_fanout_max,
                    repurchase_max=settings.dedup_repurchase_max,
                )
    except LLMError as exc:
        code = "LLM_TIMEOUT" if _is_timeout(exc) else "LLM_UNAVAILABLE"
        yield sse(
            "error",
            ErrorData(
                code=code,
                message="질의를 이해하지 못했어요.",
                request_id=resolved_request_id,
                retryable=True,
            ).model_dump(by_alias=True),
        )
        return

    # transient 세션 버퍼에 발화 누적(승격 전 격리, SPEC-PROFILE-001) — 세션 종료 델타 소스.
    # [#119 REQ-PROF-026] intent 판정 **뒤에** 둔다: 주문조회·장바구니 조회 발화는 취향 신호가
    # 0인데 버퍼(슬라이딩 윈도우)를 채워 정작 취향 발화를 밀어낸다. 반복 발화는 지우지 않고
    # **상한**만 둔다 — 버퍼가 델타 추출 LLM 에 통째로 실려 반복 횟수가 곧 취향 강도가 되지만,
    # 전부 접으면 게이트의 반복 승격 경로(explicit OR repeated)까지 죽는다.
    # 대가로 decompose 실패 턴의 발화는 쌓이지 않는데, 의도를 파악하지 못한 발화는 취향
    # 신호로도 쓰지 않는다는 판단이다.
    if profile_eligible and decision.intent not in settings.profile_buffer_excluded_intents:
        pstore = await get_profile_store()
        await pstore.append_session_ctx(
            conversation_key(identity.user_id, request.session_id),
            request.message,
            cap=settings.profile_session_buffer_cap,
            repeat_cap=settings.profile_buffer_repeat_cap,
        )

    # 되물음 대기 중 사용자가 담기 아닌 의도로 전환(취소·조회·추천)하면 stale pending 을 정리한다
    # (프롬프트가 약속한 "옛 상품에 갇히지 않게"와 실제 동작 일치).
    if decision.intent != "cart_add" and pending is not None:
        await cart_store.clear_pending(thread_key)

    if decision.intent == "order_status":
        if trace := current_request_trace():
            trace.set_lane("fallback")
        fetch_order_status = (
            order_status_fn
            if order_status_fn is not None
            else getattr(spring_client, "get_order_status", None)
        )
        if not callable(fetch_order_status):
            raise TypeError("order_status_fn must be callable")
        async for frame in stream_order_status(
            identity=identity,
            fetch_order_status=fetch_order_status,
            request_id=resolved_request_id,
        ):
            yield frame
        return

    if decision.intent == "general":
        if trace := current_request_trace():
            trace.set_lane("fallback")
        async for frame in stream_fallback(decision, observer=observer):
            yield frame
        yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
        return

    if decision.intent == "cart_view":
        if trace := current_request_trace():
            trace.set_lane("cart")
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_cart_view(identity=identity, observer=observer):
                yield frame
        return

    if decision.intent == "cart_add":
        if trace := current_request_trace():
            trace.set_lane("cart")
        allowed = {pid for pid, _ in last_reco}
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_cart_add(
                identity=identity,
                cart=decision.cart or CartIntent(),
                cart_store=cart_store,
                thread_key=thread_key,
                settings=settings,
                message=request.message,
                allowed_product_ids=allowed,
                observer=observer,
            ):
                yield frame
        return

    if trace := current_request_trace():
        trace.set_lane("recommend")
    with trace_span("buyer.graph.recommendation", "chain"):
        reverted = await _prepare_recommendation(
            request=request,
            decision=decision,
            prior=prior,
            llm=llm,
            settings=settings,
            map_categories=map_categories,
            expand_needs=expand_needs,
            observer=observer,
            thread_store=thread_store,
            thread_key=thread_key,
        )
        async for frame in stream_recommendation(
            request=request,
            decision=decision,
            llm=llm,
            search=search,
            push_fn=push_fn,
            identity=identity,
            # [#119] rerank 는 개인화의 **유일한 정상 경로**다 — decompose 주입을 끊을 때 여기까지
            # 같이 끄면 개인화가 통째로 사라진다. off 는 A/B baseline arm 일 때만 — 주입만
            # 끊을 뿐 위쪽 프로필 read·"기억해" 기록·버퍼 적재는 계속 돈다(config 주석 참조).
            profile=(None if settings.profile_injection_scope == "off" else profile),
            settings=settings,
            reverted_categories=reverted,
            cart_store=cart_store,
            thread_key=thread_key,
            observer=observer,
            request_id=resolved_request_id,
        ):
            yield frame
