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

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import cast

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

from app.agents.buyer._frames import progress as progress_frame
from app.agents.buyer._frames import sse
from app.agents.buyer.cart.graph import stream_cart_add, stream_cart_view
from app.agents.buyer.cart.remove import stream_cart_remove
from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.cart.wishlist import stream_wishlist_add, stream_wishlist_remove
from app.agents.buyer.fallback import stream_fallback
from app.agents.buyer.order_status import stream_order_status
from app.agents.buyer.recommendation.category_mapping import CategoryMapping, dedup_truncate
from app.agents.buyer.recommendation.category_scope import classify_category_scope
from app.agents.buyer.recommendation.category_mapping import map_categories as _map_categories
from app.agents.buyer.recommendation.decompose import (
    _resolve_contradictory_price_range,
    build_screen_prompt,
    decompose,
    has_new_category_signal,
    prior_echo_tokens,
    resolve_category_action,
)
from app.agents.buyer.recommendation.needs_expansion import detect_expansion_need
from app.agents.buyer.recommendation.needs_expansion import expand_needs as _expand_needs
from app.agents.buyer.recommendation.no_condition import is_no_condition_turn
from app.agents.buyer.recommendation.underspecified import is_underspecified_turn
from app.agents.buyer.recommendation.relaxation import FIELD_TO_ATTR as RELAXATION_FIELD_TO_ATTR
from app.agents.buyer.recommendation.state import get_relaxation_offer_store, get_revert_store
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
from app.agents.buyer.screen_reference import resolve_screen_reference
from app.schemas.chat import CONDITION_FIELD_TO_FILTER, ConditionAction, DoneData, ErrorData
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
        """저장된 누적 필터. 없거나 **지금 스키마로 못 읽으면** None (= 이전 맥락 없음).

        스키마 제약은 **소급 적용된다**(PR #248 3차 리뷰). `ge=0` 을 새로 걸면 그 전에 저장된
        음수 레코드가 지금은 `ValidationError` 가 되는데, 이 호출은 `run_buyer_turn` 진입 직후
        **decompose 보다도 먼저** 감싸이지 않은 채 실행된다. 감싸지 않으면 그런 스레드는 매 턴
        여기서 죽고, LLM degrade 같은 정상 오류 이벤트조차 못 내며(그 코드에 닿기 전이다),
        pg_store 에 TTL 도 없어 스스로 낫지 않는 **영구 broken** 상태가 된다.

        None 으로 떨구면 그 턴은 "이전 필터 없음"으로 정상 진행하고, 추천 턴 끝의 `put` 이 새
        값으로 덮어써 **스스로 회복**한다. 읽기 경로에서 삭제 쓰기를 하지는 않는다 — None 을
        돌려주는 것만으로 증상이 사라지고, 조회에 부작용을 넣으면 실패 모드가 늘어난다.
        """
        item = await run_with_query_timeout(self._store.aget((_NAMESPACE_ROOT, key), _FILTERS_KEY))
        if not item:
            return None
        try:
            # 모순 구간(`price_min > price_max`)도 여기서 푼다 — decompose 는 **자기 산출**만
            # 고치는데, 이 값은 칩 클릭 경로에서 `_relaxed_filters_from_offer` 의 base 로
            # **직접** 쓰여 그 보정을 우회한다. 이 수정 이전에 저장된 레코드가 남아 있을 수 있어
            # 저장소 경계에서 한 번 더 막는다(prior 의 prior 는 없으므로 하한을 버리는 폴백).
            return _resolve_contradictory_price_range(
                ProductSearchFilters.model_validate(item.value), None
            )
        except Exception as exc:  # noqa: BLE001 - 저장 값을 못 읽는 어떤 이유든 턴은 살려야 한다
            # 스키마 강화·배포 중 신구 혼재·손상 — 어느 쪽이든 이전 맥락을 잃을 뿐 턴은 산다.
            # **`ValidationError` 로 좁히지 않는다**: 이 try 는 "저장된 값을 해석한다" 전체를
            # 맡고, 그 안에 검증 말고도 보정 호출이 들어와 있다. 좁혀 두면 거기서 난 다른 예외가
            # `run_buyer_turn` 의 감싸이지 않은 호출부(`prior = await thread_store.get(...)`)로
            # 새어나가 스레드가 영구 broken 이 되는 — 이 가드가 애초에 막으려던 바로 그 상태가
            # 된다. pg 장애는 이 try 밖(`run_with_query_timeout`)이라
            # 삼켜지지 않고, CancelledError(BaseException)도 전파된다.
            logger.warning("thread_filters_unreadable", extra={"reason": str(exc)})
            return None

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


def _remove_condition_actions(
    prior: ProductSearchFilters,
    actions: list[ConditionAction],
) -> ProductSearchFilters:
    """conditionActions가 지목한 승계 필터 축을 제거한다(§3.1)."""
    updates = {CONDITION_FIELD_TO_FILTER[action.field]: None for action in actions}
    return prior.model_copy(update=updates)


async def _collect_scope_task(task) -> bool | None:  # noqa: ANN001
    """병렬 분류기 태스크를 회수한다 — 실패는 전부 None(=신호 없음) (#84).

    `classify_category_scope` 가 이미 자기 예외를 삼키지만 여기서 한 겹 더 감싼다: 태스크 레벨
    실패(이벤트루프 종료 등)는 그 함수 안에서 잡히지 않는데, 그것 때문에 무관한 추천 턴이
    죽으면 안 된다. 폴백은 오늘 동작(carry)이라 손해가 없다.

    **`except Exception` 을 `BaseException` 으로 넓히지 말 것.** `CancelledError` 는
    `BaseException` 이라 여기 걸리지 않고 **그대로 전파된다** — 값을 기다리는 이 자리에서 바깥
    취소가 오면 턴은 거기서 끝나야 한다(넓히면 끊긴 요청이 정상 턴처럼 계속 진행한다. 라운드 4 가
    `_discard_scope_task` 를 없앤 이유와 같은 함정이다).
    """
    if task is None:
        return None
    try:
        return await task
    except Exception as exc:  # noqa: BLE001 - 보조 신호 회수 실패가 턴을 죽이지 않게(degrade)
        logger.warning("category_scope_task_failed", extra={"reason": str(exc)})
        return None


def _cancel_scope_task(task) -> None:  # noqa: ANN001
    """분류기 태스크를 **동기적으로만** 취소한다 — 값을 쓰지 않는 **모든** 정리 지점의 정본 (#84).

    `await` 하지 않는다. 초판은 정리 경로가 둘이었고(본문 `await` 회수 + `finally` 동기 취소)
    본문 쪽이 `task.cancel()` 뒤 `with suppress(CancelledError, Exception): await task` 를 했는데,
    그 `await` 지점에서 **바깥에서 온 취소**(클라이언트 연결 종료 등)가 배달되면:

    - asyncio 는 대기 중 future 로 취소를 위임하고(`Task.cancel()` → `_fut_waiter.cancel()`),
      위임이 성공하면 바깥 태스크의 `_must_cancel` 이 **세팅되지 않는다.**
    - 그 `CancelledError` 는 "우리가 건 취소"와 구분되지 않은 채 `suppress` 에 **삼켜지고**,
      `_must_cancel` 이 없으니 다음 체크포인트에서 **재전파도 되지 않는다** → 이미 끊긴 요청인데
      `run_buyer_turn` 이 정상 턴처럼 계속 진행한다(추가 LLM·Spring 호출·`thread_store.put`).

    그래서 정리는 전부 이 동기 취소로 통일한다(라운드 4). 근거 셋:

    - **`await` 할 이유가 없다.** 이 태스크의 산출은 추천 경로에서만 쓰이고(`_collect_scope_task`),
      나머지 경로는 값을 버린다. 버릴 값을 기다리는 것은 이득이 0인데 위 구멍을 연다.
    - **"Task exception was never retrieved" 는 나지 않는다.** 취소된 태스크는 asyncio 의 그 경고
      대상이 아니고, `classify_category_scope` 는 자기 예외를 전부 삼켜 **값으로** 돌려주므로
      미회수 예외 자체가 없다.
    - **대기 0**(라운드 2 F-2 의 목적)도 그대로 달성된다 — 오히려 더 확실하다. 취소된 태스크를
      `await` 하는 것보다 아예 기다리지 않는 쪽이 짧다.

    **이미 끝난 태스크면 아무 것도 하지 않는다** — 정리 지점이 여럿(오류 경로·비추천 분기·
    `finally`)이라 같은 태스크에 두 번 불릴 수 있고, 그때 완료된 태스크를 건드리지 않아야 한다.
    """
    if task is not None and not task.done():
        task.cancel()


def _is_timeout(exc: Exception) -> bool:
    """LLMError 메시지에서 타임아웃 여부를 추정한다(LLM_TIMEOUT vs LLM_UNAVAILABLE 매핑용)."""
    return "timeout" in str(exc).lower()


def _carry_axis_untouched_this_turn(applied, prior, current: ProductSearchFilters) -> bool:  # noqa: ANN001
    """승계할 완화 축을 **이번 턴에 사용자가 다시 말하지 않았는지** 판정한다 (#113, PR #248 리뷰).

    자동 완화 승계는 "직전 결과를 그대로 받아들인다"는 뜻이라, 사용자가 **그 축을 새로 말한**
    턴에는 성립하지 않는다 — "그 중에 평점 3.0 이상도 볼래" 에서 저장된 완화값(4.0)으로 덮으면
    방금 말한 3.0 이 흔적도 없이 사라진다. 다른 축(가격 등)을 말한 경우는 승계해도 무해하므로
    **완화 축 하나만** 본다.

    판정 기준은 `이번 턴 값 == prior 값` 이다. decompose 는 PRIOR_FILTERS 를 병합하므로, 그 축을
    새로 언급하지 않은 턴은 prior 값이 그대로 실려 온다 — 값이 달라졌다는 건 이번 턴에 손댔다는
    신호다. 축을 아예 지운 경우(None)도 "달라졌다"에 포함되어 승계하지 않는다(사용자가 조건을
    빼달라고 했는데 되살리면 안 된다).

    prior 가 없으면(스레드 상태 유실) 비교 근거가 없으므로 **승계하지 않는다** — 애매하면
    사용자가 말한 값을 그대로 두는 쪽이 안전하다(#113 설계 원칙).

    **알려진 한계**: 사용자가 그 축을 **같은 값으로** 다시 말한 경우("그 중에서 고르되 평점은
    4.5 그대로")는 병합된 값과 구분되지 않아 승계가 걸린다. decompose 산출만으로는 "언급 안 함"과
    "같은 값으로 재확인"이 동일하기 때문이다 — 구분하려면 조건별 출처 태깅(REQ-REC-047)이 필요하다.
    발화 자체가 모순적("그 중에" = 완화된 결과 수용 + "4.5 그대로" = 완화 거부)이라 실사용 빈도가
    낮다고 보고 한계로 남긴다.
    """
    if not isinstance(applied, dict) or prior is None:
        return False
    attr = RELAXATION_FIELD_TO_ATTR.get(applied.get("field"))
    if attr is None:
        return False
    return getattr(current, attr, None) == getattr(prior, attr, None)


def _relaxed_filters_from_offer(offer, base: ProductSearchFilters) -> ProductSearchFilters | None:  # noqa: ANN001
    """저장된 완화 칩 제안을 검증해 `base` 에 적용한 필터를 낸다. 못 쓰는 값이면 None (#113).

    저장소는 **신뢰 경계 밖**이다 — 값이 pg-profile 을 왕복(JSON 직렬화/역직렬화)하고, 배포 사이에
    스키마가 바뀔 수도 있다. 그래서 (1) 봉투 모양, (2) 필드가 완화 대상인지 확인한 뒤,
    (3) **값 검증은 `model_validate` 에 맡긴다** — `model_copy` 였다면 Pydantic 검증을 건너뛰어
    어긋난 타입이 그대로 Spring I-1 쿼리 파라미터로 나갔을 자리다(PR #248 리뷰).

    값 타입을 여기서 열거하지 않는 이유(PR #248 2차 리뷰): 스키마가 이미 필드별로 정확히 거른다.
    사전 목록을 두면 스키마보다 **좁아져서**, 예컨대 `brand: list[str]` 에 리스트 값을 제안하도록
    확장하는 순간 여기서 조용히 None 이 되어 칩 클릭이 영구 무동작이 된다. 스키마와 어긋나는
    이중 규칙을 만들지 않는다.

    **예외는 `bool` 하나다** — Pydantic 은 `price_max=True` 를 거부하지 않고 **`1` 로 강제 변환**한다
    (실측 확인). 즉 손상된 `true` 하나가 "가격 상한 1원"으로 둔갑해 조용히 0건을 만든다. 스키마가
    잡아주지 못하는 유일한 케이스라 여기서만 막는다.
    """
    if not isinstance(offer, dict):
        return None  # 봉투 자체가 기대 형태가 아님(구 스키마·손상)
    attr = RELAXATION_FIELD_TO_ATTR.get(offer.get("field"))
    if attr is None:
        return None  # 완화 대상이 아닌(또는 알 수 없는) 필드 — 조용히 무시한다
    if "value" not in offer:
        # 키 자체가 없는 건 **손상**이다 — `.get()` 으로 뭉뚱그리면 None(=조건 해제)으로 읽혀
        # 의도보다 검색이 더 넓어진다. "없음"과 "null 로 해제"는 다른 사실이라 구분한다.
        return None
    value = offer["value"]
    if isinstance(value, bool):  # 위 docstring 참조 — 스키마가 못 잡는 유일한 케이스
        return None
    try:
        # None 은 '조건 해제'라는 정상 값이다(brand·color·평점 하한 소멸) — 스키마가 허용한다.
        return ProductSearchFilters.model_validate({**base.model_dump(), attr: value})
    except ValidationError as exc:
        # **거부도 관측 가능해야 한다**(PR #248 3차 리뷰 — 스윕에서 추가 발견). 조용히 None 을
        # 돌려주면 손상된 저장 값(음수 가격 등)이 칩 클릭을 영구 무동작으로 만드는데, 사용자에게는
        # "눌러도 아무 일이 없다"로만 보이고 서버에는 아무 흔적이 없다. 스키마가 잡아 준 사실을
        # 여기서 이름표와 함께 남겨야 원인 분류가 된다.
        logger.warning("relaxation_offer_rejected", extra={"reason": str(exc)})
        return None


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


@dataclass
class _PrepareRecommendationOut:
    """`_prepare_recommendation` 결과 홀더.

    이 함수는 SSE 프레임(`mapping`/`expanding` progress)을 내야 해서 async generator 로
    바뀌었는데, generator 는 `return` 값을 줄 수 없다(`yield` 만 밖으로 흐른다) — 그래서
    호출부가 만들어 넘긴 이 홀더에 결과를 채워 넣는 방식으로 대체한다.
    """

    reverted: frozenset[str] = field(default_factory=frozenset)


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
    out: _PrepareRecommendationOut,
    scope_free: bool | None = None,
) -> AsyncIterator[str]:
    """Prepare mapped recommendation state inside the recommendation graph span."""
    # recommend — 카테고리 하이브리드 매핑(이슈 #59, 방식 A): decompose 추측을 canonical 로
    # 보정(canonical-or-null). 매핑이 죽거나 신호가 없으면 category 없이(전체) 검색으로 degrade.
    # [#84] "카테고리 신호 없음"이 곧 리파인은 아니다 — "5만원 이하 아무거나"(카테고리-무관 리셋)도
    # 신호가 없다. 그 구분은 **전용 분류기 하나**(`category_scope`)가 낸 `scope_free` 로 한다
    # (정본은 `resolve_category_action` — 그래프와 프로브가 같은 규칙을 쓴다). decompose 프롬프트
    # 안의 인라인 필드로 받는 안은 실측으로 기각됐다(이득 0 · 전환 축 손해, 그 함수 docstring 참조).
    # 승계할 prior 가 있는지는 **호출부인 여기서** 본다(아래 if) — 판정 함수는 prior 를 받지 않는다.
    # [라운드 3 F-1] "새 카테고리를 지목했는가"는 **prior 에코 leg 를 제외한** 유효 leg 유무다.
    # 판정 규칙은 `decompose` 에 한 벌만 두고 프로브(`evals/intent_probe/runner.py`)도 같은 함수를
    # 부른다 — 규칙이 두 벌이면 측정과 배포가 갈라진다.
    echo_tokens = prior_echo_tokens(
        category=prior.category if prior is not None else None,
        semantic_query=prior.semantic_query if prior is not None else None,
    )
    has_category_signal = any(q.raw_category or q.query for q in decision.category_queries)
    action = resolve_category_action(
        has_category_signal=has_category_signal,
        scope_free=scope_free,
        has_new_category_signal=has_new_category_signal(decision.category_queries, echo_tokens),
    )
    # 값(카테고리 문자열)은 싣지 않는다 — 이 파일의 기존 규약(#119 PII).
    logger.info(
        "category_carry_resolved",
        extra={
            "action": action,
            "had_prior": bool(prior and prior.category),
            "scope_free": scope_free,
        },
    )
    if prior is not None and prior.category and action == "carry":
        # 리파인 턴(예: "더 저렴한 걸로") — 이번 턴에 카테고리 신호가 전혀 없음(빈 리스트, 또는
        # raw·query 가 모두 없는 leg 만). prior 는 이미 canonical(§7)이라 재매핑(pg 왕복) 없이 그대로
        # 승계한다. 매핑에 태우면 신호가 없어 빈 legs 가 나오고(#22), 아래 else 의 category=None 으로
        # 직전 카테고리가 지워진다 — 리파인인데 필터가 풀려버린다(PR #73 #12).
        # 단, raw 는 null 이라도 유의미한 query 가 있으면(신규 상황형 질의) 검색 의도가 있는 것이라
        # 아래 매핑을 태워야 한다 — prior 로 하이재킹하면 fan-out 이 죽고 #59 문제가 재발(PR #73 #19).
        decision.category_legs = [(prior.category, None)]
    elif action == "clear":
        # [#84] 카테고리-무관 리셋 — **legs 를 비운다**(→ 아래에서 `filters.category = None`,
        # #22 무필터 복원). 매핑을 태우지 않으므로 임베딩·pg 왕복도 이 턴에는 없다.
        #
        # 초판은 "clear 면 신호가 없으니 매핑이 알아서 빈 legs 를 낸다"고 보고 별도 분기를 두지
        # 않았는데, **실측이 그 전제를 반증했다**: 리셋 발화의 30~31/32 가 `_SYSTEM` 의
        # categoryQueries 불릿("조건 다듬기면 PRIOR_FILTERS.category 를 그대로 실어라") 지시대로
        # **직전 카테고리를 복사한 leg** 를 함께 낸다. 그대로 매핑에 태우면 그 에코가 canonical 이
        # 돼 사용자가 "아무거나"라고 한 턴이 다시 이어폰으로 좁혀진다 — 해제가 무동작이 된다.
        decision.category_legs = []
    else:
        # [#198·#217] 목적·상황형 발화의 상품 전개 — **승계 가드 안쪽(else)에 둔다**. D1(`no_legs`)은
        # 리파인 턴("더 저렴한 걸로")의 "신호 없음"과 조건이 겹치므로, 전개를 위 if 보다 앞에 놓으면
        # 리파인 턴이 엉뚱한 상품 목록으로 바뀌어 직전 맥락이 날아간다(PR #73 #12/#19 승계 규약이
        # 반대 방향으로 깨진다). 여기서는 이미 "승계 대상 아님"이 확정돼 있다.
        #
        # [#84] 여기 오는 것은 `replace`(새 상품을 말한 턴)와 prior 가 없는 턴뿐이다 — 이 매핑
        # 경로가 원래 하던 일을 그대로 한다. `carry` 는 위 if, `clear` 는 위 elif 로 갈린다.
        #
        # [#217] 순서가 뒤집혔다 — **매핑을 먼저** 돌리고 그 실패를 전개 트리거로 쓴다(§4·§6.1).
        # 초판은 목적 marker 열거로 매핑 전에 미리 맞혔는데, 열거는 목록에 없는 표현을 놓치고
        # 목록을 늘리면 이미 정답 매핑되는 표현이 파괴됐다(§4.0).
        # 매핑을 실제로 태우는 턴에서만 낸다. `carry`(리파인 승계)·`clear`(카테고리 리셋)는 이
        # else 분기 자체에 안 들어오지만, **이 분기가 곧 "매핑을 태운다"는 아니다** — prior 가
        # 없는 첫 턴은 action == "carry" 여도 위 `if prior is not None and ...` 가드를 못 통과해
        # 여기로 떨어진다(카테고리 신호가 하나도 없어도). 그 턴은 `map_categories` 규칙 (4)
        # ("raw·query 모두 없으면 신호 없음으로 보고 leg 를 만들지 않는다")대로 매퍼가 아무 일도
        # 하지 않으므로, `has_category_signal`(위에서 이미 계산해 판정과 emit 이 같은 값을
        # 쓴다 — 판정 규칙을 두 벌로 만들지 않는다)이 False 면 이 stage 를 내지 않는다.
        if settings.progress_events_enabled and has_category_signal:
            yield progress_frame("mapping", settings.progress_mapping_message)
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
                if settings.progress_events_enabled:
                    yield progress_frame("expanding", settings.progress_expanding_message)
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
                    # 여기서는 `mapping` progress 를 다시 내지 않는다 — 전개 후 재매핑은 위에서
                    # 이미 알린 "매핑 중" 논리 단계의 연장이지, 사용자 입장에서 새로 시작하는
                    # 단계가 아니다.
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
                    # [PR #318 리뷰 R6-3] expansion_leaves 도 **합친다** — legs 와 같은 이유·같은
                    # 규약(원 매핑 것을 앞에, dedup_truncate 로 정리)이다. 안 합치면 원 발화가
                    # D1(신호 없음)로 expansion_leaves 가 비어 있고 #217 전개 아이템들만 거리컷에
                    # 드롭돼 expanded.expansion_leaves 가 채워진 턴에서 그 후보가 조용히 버려져
                    # #222 폴백이 아예 발동하지 않는다 — 쓸 수 있는 후보가 있는데 놓치는 셈이다.
                    # [PR #318 리뷰 R9-1 캐비엇] "합친다"는 표현이 두 소스가 실제로 섞인다는
                    # 인상을 주지만, dedup_truncate 는 앞에서부터 자르므로 mapping.expansion_leaves
                    # 가 이미 상한(category_expand_legs)을 채우는 흔한 경우 expanded.expansion_leaves
                    # 는 전부 잘려나간다. 이는 `merged`(위)와 **동일한 의도된 우선순위**다 — 원
                    # 매핑 쪽은 사용자가 실제로 말한 앵커의 top-N leaf 이고 expanded 쪽은 LLM 이
                    # 지어낸 아이템이 다시 실패해서 나온 leaf 라, 인터리브하면 LLM 창작 아이템의
                    # 후보가 사용자 발화의 후보를 밀어낸다(legs 규약과 반대 방향). R6-3 이 풀려던
                    # 문제는 원 쪽이 **비었을 때** 전개 쪽이 통째로 버려지는 것이었고, 그 경우는
                    # (원이 비면 전개 후보가 상한까지 그대로 채워지므로) 지금도 정확히 해결된다.
                    # `_interleave_by_leg`(R5-1)는 **같은 서열의 leg 들 사이** 형평을 맞추는
                    # 것이라 여기(서로 다른 서열의 두 소스)와는 상황이 다르다.
                    merged_expansion_leaves = dedup_truncate(
                        mapping.expansion_leaves + expanded.expansion_leaves,
                        settings.category_expand_legs,
                    )
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
                    mapping = replace(
                        mapping,
                        legs=merged,
                        select_calls=select_used,
                        expansion_leaves=merged_expansion_leaves,
                    )
        decision.category_legs = mapping.legs
        # [#222] 매핑이 leg 를 하나도 못 냈고 확장 후보가 있으면 그것으로 fan-out 한다.
        # **legs 가 비었을 때만** 발동한다 — canonical 을 낸 발화는 이 분기에 진입하지 않는다,
        # 그 자체는 구조적이다. [PR #318 리뷰 R14-2] 단, "협소 발화는 canonical 을 내므로 이
        # 경로에 안 들어온다"는 **거리 임계가 정상 튜닝돼 있을 때만** 성립한다 — 재측정 완료
        # (#344, 0.26). 잔존 드롭(정당: 사전에 칸이 없거나 d1>0.26)이 이 경로로 들어오는 것은
        # 여전히 정상 동작이다 — 확장 top-N 은 의미 최근접이라 정답 leaf 가 대체로 상위에 포함되고
        # (실측: "무선 이어폰" top-1 = 음향가전 > 이어폰) leg 마다 keyword·semantic_query 가
        # 유지되므로, 무필터 degrade(종전 동작) 대비 악화는 아니다.
        # 멀티 니즈 중 일부만 unresolved 인 턴의 부분 확장은 v1 범위 밖이다.
        # [#222 F-3] #217 이 위 needs_expansion 블록에서 먼저 legs 를 채우면(예: "화장품 추천해줘"
        # → case 3 게이트 통과 → LLM 전개로 재매핑 성공) 이 경로는 타지 않는다 — 이 폴백이 새로
        # 여는 것은 #217 도 실패하는 턴(비-case3, 또는 전개 후에도 매핑이 전량 실패한 턴)뿐이다.
        if (
            not decision.category_legs
            and mapping.expansion_leaves
            and settings.category_expand_enabled
        ):
            decision.category_legs = mapping.expansion_leaves[: settings.category_expand_legs]
            decision.category_expanded = True
            # filters.category 자체는 아래 공유 if 가 category_expanded 를 보고 None 으로 비운다
            # (PR #318 리뷰 R6-1, §3 이슈 ④ 비범위는 그대로다: 8개 확장 leaf 를 대표하는 단일
            # LCA 값을 만드는 게 아니라, **틀린 값을 저장하지 않는 것**만 한다).
            # [PR #318 리뷰 R12-1] `extra` 에는 개수·불리언만 싣는다(#119 PII 규약, 위
            # category_carry_resolved 와 동일 규약) — 예전엔 `carry_leaf` 로 대표 leaf 카테고리
            # 문자열을 그대로 실었는데, 그건 R6-1 이전 "대표값이 filters.category 로 승계되는
            # 함정"을 관측하려던 것이었고 R6-1 이 그 승계 자체를 없애 관측 대상이 사라졌다.
            logger.info(
                "category_expanded",
                extra={"legs": len(decision.category_legs)},
            )
    if decision.category_legs and not decision.category_expanded:
        # 대표 canonical — 단일 filters.category 필드·조건 칩·멀티턴 승계 호환(§7).
        decision.filters.category = decision.category_legs[0][0]
    else:
        # [PR #318 리뷰 R6-1] 확장 턴은 대표값을 저장하지 않는다 — category_legs[0][0] 은 8개
        # 확장 leaf 중 임의의 하나일 뿐이라(§3 이슈 ④), 이걸 그대로 영속하면 (a) F-1 무필터
        # 폴백이 걸린 턴은 **실제로 쓰이지 않은** 카테고리가 저장되고, (b) 폴백이 안 걸려도
        # 다음 리파인 턴("더 저렴한 걸로")이 그 leaf 하나로 조용히 좁혀진다(action=="carry").
        # 칩·고지에서 지킨 "표시=실제"(#51)가 멀티턴 영속 경로에서 깨지는 것을 막는다.
        # **검색에는 영향이 없다** — fan-out(`_run_search`)은 `decision.category_legs` 로 돌고
        # `_leg` 가 leg 마다 `category` 를 override 하므로(`base.category` 를 읽지 않음)
        # 여기서 None 을 둬도 이번 턴의 8-leg 검색 자체는 그대로다.
        #
        # 매핑 결과 없음(비-확장 degrade) → LLM 이 echo 했을 수 있는 미검증 filters.category 를
        # 비운다. category 는 이제 전적으로 category_legs(canonical) 경유로만 흐른다 —
        # 미시드·매핑 실패 시에도 보정 안 된 원문이 Spring 검색·조건 칩으로 새지 않게
        # (PR #73 리뷰 #13/#15).
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
    out.reverted = frozenset(reverted)


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
    popular_fn=None,
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
    popular_fn = popular_fn or spring_client.get_popular_products  # [#162] I-3
    thread_store = await get_thread_store()
    prior = await thread_store.get(thread_key)
    condition_actions = getattr(request, "condition_actions", None) or []
    if prior is not None and condition_actions:
        # conditionActions 반영 — 제거된 축을 prior 에서 실제로 비운다(§3.1).
        prior = _remove_condition_actions(prior, condition_actions)
        # 추천 외 intent 로 라우팅돼도 다음 턴에 제거한 칩이 되살아나지 않게 즉시 영속한다.
        await thread_store.put(thread_key, prior)

    # 프로필 주입 (회원만, read-only) — 게스트/신규는 None(개인화 스킵, 결정 8)
    profile = None
    profile_vec = None
    profile_eligible = bool(not identity.is_guest and identity.user_id and not identity.seller_id)
    if profile_eligible:
        summary = await read_profile_summary(identity.user_id)
        profile = summary.get("markdown") if summary else None
        # [#162] 요약 생성 시점에 미리 만들어 둔 취향 벡터(#148 `store._embed_summary`).
        # 종전에는 markdown 만 꺼내 쓰고 이 값을 버렸다 — 조건 없는 발화의 회원 경로가 이걸로
        # 홈과 같은 벡터 랭킹을 돌린다. 구 요약·임베딩 실패분은 None 이라 인기 상품으로 간다.
        profile_vec = summary.get("embedding") if summary else None
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
    # [#118] **옵션 되물음 중에는 화면 맥락을 통째로 끈다.** 이 한 플래그를 아래 세 지점이 함께
    # 쓴다 — ① decompose 프롬프트 주입(`prompt_screen`) ② 담기 허용 목록(`allowed`)
    # ③ 코드 해소기(`resolve_screen_reference`). 셋 중 하나만 열려 있어도 구멍이 된다.
    screen_context_active = pending_dict is None

    # [#84] 카테고리 범위 해제 분류기 — decompose 와 **병렬**로 띄운다.
    #
    # 이 판정에 필요한 입력은 `prior.category` 와 이번 발화뿐이라 decompose 결과를 기다릴 이유가
    # 없다. 순차로 부르면 첫 SSE 이벤트 앞 **직렬 합**이 한 호출만큼 늘어나는데, 그 예산이 실제로
    # 터진 전례가 있다(#277 — 미룬 턴의 두 I-1 호출이 직렬로 놓여 10s 상한을 8/8 초과).
    # lessons 2026-08-04 「상한이 안전한지는 단일 호출 예산이 아니라 첫 이벤트 앞 직렬 합으로
    # 잰다」가 가리키는 자리라, 먼저 띄우고 decompose 뒤에 회수해 **직렬 지연을 0** 으로 둔다.
    #
    # 게이트가 거짓이면 태스크를 아예 만들지 않는다 — 호출 0회라 첫 턴·prior 없는 스레드·
    # 액션-only 턴은 오늘과 **완전히 동일**하다(비용도 0).
    #
    # [2차 리뷰 F-1] **되물음(PENDING_CART) 턴을 막지 않는다.** 초판은 "되물음 턴은 카테고리 리셋
    # 턴일 수 없다"고 단정했는데 틀렸다 — 사용자는 되물음을 버릴 수 있다:
    # `"그건 됐고 종류 상관없이 5만원 이하 아무거나 보여줘"` 는 decompose 가 recommend 로 보내고
    # (그 프롬프트가 "담기를 취소·중단하려 하면 … 옛 상품에 갇히지 않게"라고 명시한다) 아래에서
    # pending 도 정리되는데, 분류기만 안 돌면 carry 로 떨어져 **이 이슈가 고치려는 결함이 그
    # 경로에 그대로 남는다.**
    # 되물음에서 여는 것이 안전한 이유:
    #   · 분류기 프롬프트에는 `PENDING_CART` 가 **실리지 않는다**(입력이 직전 카테고리와 발화뿐)라
    #     되물음 맥락과 교란될 표면이 없다. decompose 의 `prompt_screen`·`prompt_reco` 를 되물음에서
    #     끄는 이유("한 턴에 두 지시가 겹친다")가 여기에는 해당하지 않는다.
    #   · 산출은 **추천 경로에서만 소비**된다(`_prepare_recommendation`). 옵션 답변 턴("2번으로")은
    #     cart_add 로 가서 값이 쓰이지 않는다(아래 intent 분기 회수에서 취소된다).
    #   · 비용은 되물음 중 prior 카테고리가 있는 턴에 `max_tokens=32` 호출 1회다.
    scope_gate = (
        settings.category_scope_classifier_enabled
        and prior is not None
        and bool(prior.category)
        # 액션-only 턴(conditionActions 만, message 빈/공백)은 판정할 발화가 없다.
        and bool(request.message.strip())
    )
    scope_task = (
        asyncio.create_task(
            classify_category_scope(
                llm,
                message=request.message,
                prior_category=cast(str, prior.category if prior else ""),
                settings=settings,
                observer=observer,
            )
        )
        if scope_gate
        else None
    )

    # [#84·2차 리뷰 F-3] 태스크 생성부터 회수까지를 `try/finally` 로 감싼다. 정리 지점이 정상 회수와
    # `except LLMError` 둘뿐이면 **바깥에서 온 취소**(클라이언트가 첫 이벤트 전에 끊어 요청 태스크가
    # 취소되거나 SSE 제너레이터가 조기 종료되는 경우)에 `CancelledError` 가 두 지점을 모두 건너뛰어
    # 분류기 태스크와 그것이 붙든 HTTP 연결이 스스로 끝날 때까지 남는다.
    scope_free: bool | None = None
    scope_settled = False
    # [병합 #84 × #289] 두 변경이 같은 지점에 붙는다. #289 의 첫 프레임은 `try` **안**에 둔다 —
    # 그 자리에서 소비자가 스트림을 닫아도(`GeneratorExit`) 아래 `finally` 가 분류기 태스크를
    # 정리한다. `try` 밖에 두면 태스크는 이미 떠 있는데 정리 범위 밖이라 고아가 될 수 있다.
    # #289 가 요구하는 위치(세션 프렐류드 뒤 · decompose 앞)는 그대로 지켜진다.
    try:
        # [#289] 첫 SSE 프레임을 decompose 앞으로 당긴다 — first-token 관문(§2.9 c, 10s)이
        # LLM head·검색·재시도·자동 완화를 통째로 안고 있어 미룬 턴 최악에서 이벤트 0건·504가
        # 재현됐다(#277). 계약 미등재라 기본 off — 켜면 신규 이벤트 타입이 와이어에 나간다.
        # 세션 프렐류드보다 **뒤**에 두는 이유: 앞에 두면 200 헤더가 먼저 나가
        # SessionStateUnavailable(503 STATE_UNAVAILABLE, §2.5 봉투)이 in-stream error 로 바뀐다.
        # 관문에서 빠지는 건 decompose LLM head 이후뿐 — 앞의 ensure_thread_adopted·thread_store.get·
        # 회원 턴 read_profile_summary·cart_store.get_pending/get_last_reco_state 는 여전히 관문 안이다
        # (flag-on 실측 p50 ~12ms, evals/first_event_budget/). 이 넷은 각각 state_store_query_timeout_s
        # (3.0s)라 직렬 최악 12.0s > first-token 상한 10.0s — 관문 통과를 보장하지 않는다(pg-profile
        # 장애 시 504 재현 가능). 상세·협의 선택지는 scratchpad/draft-progress-contract.md §4.
        if settings.progress_events_enabled:
            yield progress_frame("analyzing", settings.progress_analyzing_message)
        # decompose — fast tier 1회 (intent 5-way 라우팅 + 필터 + 장바구니 의도)
        if observer is not None:
            observer.record_model_call(resolve_model_id(settings, "fast"))
        reco_state = await cart_store.get_last_reco_state(thread_key)
        last_reco = reco_state.items
        # [#118] **담기 가드와 프롬프트를 가른다.** 정본 §3.1 [보안]이 누적을 요구하는 대상은
        # `allowed`(가드)이고, 프롬프트 LAST_RECOMMENDATIONS 에 무엇을 싣는지는 계약이 아니다.
        #
        # 옵션 되물음(PENDING_CART) 중에는 **승계분을 싣지 않는다.** 실 LLM N=8 프로브
        # (#118, 이관 전 별도 프로브 — 지금은 `evals/intent_probe` 가 흡수했다) 에서
        # "PENDING_CART 중 상품 전환"(`이어폰으로 할래`)이
        #   승계 없음 6/8(=오늘) · 승계 없음+screen 주입 7/8 · 승계 11건 1/8 · 승계 상한 6건 2/8
        # 로, 승계분이 **2건만 붙어도** #240 이 "낮추지 말 것"으로 못박은 상품 전환 경로가 무너졌다.
        # 되물음 중에는 사용자가 특정 상품 하나를 놓고 답하는 중이라, 긴 과거 목록이 그 초점을 흩는다.
        #
        # 되물음이 아닌 턴에서는 누적 전체를 싣는다 — 실측상 무해했고(지시대명사 92~94/96,
        # order_status 48/48), #118 이 풀려는 4단계 시나리오(추천 A → 질문 → 추천 B → "이거 담아줘")의
        # **해소가 바로 여기서** 일어난다. 이걸 끄면 가드만 열리고 LLM 이 옛 상품을 지목하지 못한다.
        prompt_reco = last_reco if pending_dict is None else last_reco[: reco_state.turn_count]
        # [#118] **screen 도 같은 규약을 따른다 — 되물음 턴에는 넘기지 않는다.**
        # 초판은 위 `prompt_reco` 만 되물음으로 가르고 screen 은 조건 없이 실었는데, 그러면 한 턴에
        # "options 의 번호로 골라라"(PENDING_CART)와 "화면 순번으로 골라라"(SCREEN.상품 + 규칙)가
        # **동시에** 주어진다. `"2번으로"` 같은 정상 옵션 답변이 화면 순번 2로 오인될 여지가 생기고,
        # 그때 채워지는 productId 는 `screen.products` 출신이라 `allowed` 에 **반드시** 들어 있어
        # cart/graph.py 의 전환 조건(`product_id != pending.product_id and product_id in allowed`)을
        # 그대로 통과한다 → 진행 중이던 옵션 되물음이 조용히 버려지고 사용자가 답한 적 없는 상품이
        # 담긴다(PR 4차 리뷰, end-to-end 재현 확인: 담긴 productId=502·pending 소멸·CART_ADDED).
        # 이 PR 이 막으려는 오담기 클래스와 같은 것이라 프롬프트에서 뺀다.
        #
        # 설계 일관성 근거도 같은 방향이다 — 코드 해소기(`resolve_screen_reference`)는 이미 아래
        # cart_add 분기에서 `pending is None` 일 때만 돈다("그 턴의 2번은 화면 순번이 아니라 옵션
        # 번호"). 해소기는 안 도는데 프롬프트만 화면 순번을 가르치고 있었던 것이 비대칭이었다.
        #
        # 프로브 커버리지도 이쪽이 맞다: 옵션 답변 셀은 전부 `screen=None` 로 측정됐으므로
        # (`evals/intent_probe` 의 `pendingCart` 컨텍스트에 screen 없음 — #300 이 흡수하며
        # 확인한 규약이다), 이렇게 빼야 배포 경로가 실제로 잰 조건과 일치한다.
        prompt_screen = (
            build_screen_prompt(
                getattr(request, "screen", None), labels=settings.screen_page_type_labels
            )
            if screen_context_active
            else None
        )
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
                        last_recommendations=prompt_reco,
                        pending_cart=pending_dict,
                        # [#118] 지금 보고 있는 화면 — "이거 담아줘"의 대상 확정. screen 이 없거나
                        # 관대 무시로 사라졌으면(또는 되물음 턴이면, 위 prompt_screen 주석 참조)
                        # None 이라 프롬프트가 오늘과 바이트 동일하다.
                        screen=prompt_screen,
                        category_fanout_max=settings.category_fanout_max,
                        repurchase_max=settings.dedup_repurchase_max,
                    )
        except LLMError as exc:
            # [#84] 이 경로에서 나가기 전에 병렬 태스크를 반드시 정리한다 — 안 하면 취소되지 않은
            # LLM 호출이 스트림이 끝난 뒤까지 예산을 먹는다. **동기 취소만 한다**(라운드 4) —
            # 여기서 `await` 하면 바깥에서 온 취소가 그 지점에 배달돼 삼켜질 수 있다
            # (`_cancel_scope_task` docstring 참조).
            _cancel_scope_task(scope_task)
            scope_settled = True
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

        # [#113] 완화 칩 클릭 되받기 — 직전 턴에 제안한 칩 label 과 **정확히 일치**하면 LLM 해석을
        # 건너뛰고 그때 계산해 둔 값을 그대로 적용한다. FE 는 칩을 누르면 label 을 그대로 message 로
        # 보내는데(jarvis-frontend `applySuggestion`), label 은 "65,000원까지 볼까요?" 같은 **의문문**이라
        # decompose 가 조건 추출에 실패하거나 되물음으로 흘릴 수 있다 — 그러면 칩이 무동작이 된다.
        # intent 도 recommend 로 고정한다: 정확 일치는 "사용자가 우리가 만든 버튼을 눌렀다"는 명확한
        # 신호라 일반 대화로 라우팅될 여지가 없다.
        # 조회는 **추천/일반 턴에서만** 한다 — 담기·장바구니·주문조회 발화는 칩 label 과 겹칠 수
        # 없는데 매 턴 pg 왕복을 얹으면 완화와 무관한 흐름이 느려진다.
        relax_store = await get_relaxation_offer_store()
        if decision.intent in ("recommend", "general"):
            # 칩 제안(`offers`)과 적용된 완화(`applied`)를 **한 번에** 읽는다(PR #248 리뷰) —
            # 둘은 한 스냅샷이라, 따로 두 번 읽으면 그 사이에 다른 턴의 `put` 이 끼었을 때
            # 옛 offers + 새 applied 라는 찢어진 조합을 볼 수 있다(쓰기 쪽에서 없앤 바로 그 상태).
            # 승계 경로의 pg 왕복도 2회 → 1회로 준다. 읽기가 통째로 실패하면 아래 승계 분기가
            # `applied=None` 을 보고 조용히 건너뛴다.
            applied: dict | None = None
            # **해석까지 통째로 감싼다**(PR #248 리뷰) — 읽기만 감싸면 저장 값이 기대한
            # `{"field":…, "value":…}` 형태가 아닐 때(스키마 변경·롤링 배포 중 신구 혼재·손상)
            # `AttributeError` 가 올라가 턴이 죽는다. 아래 주석이 약속하는 "무해하게 폴백"이
            # 실제로 성립하려면 파싱·검증도 같은 범위 안에 있어야 한다.
            try:
                offers, applied = await relax_store.get_snapshot(thread_key)
                relaxed = _relaxed_filters_from_offer(
                    offers.get(request.message.strip()), prior or decision.filters
                )
            except Exception as exc:  # noqa: BLE001 - 상태 저장소 장애가 턴을 죽이지 않게(degrade)
                # 이 경로는 **편의 기능**이다 — 실패하면 칩 클릭이 종전처럼 decompose 해석으로
                # 처리될 뿐이다. 여기서 예외를 올리면 pg 한 번 흔들릴 때 완화와 무관한 일반 대화
                # 턴까지 깨진다(§7 degrade 원칙). CancelledError(BaseException)는 전파된다.
                # 읽기가 성공한 뒤 **해석만** 터진 경우 `applied` 는 이미 채워져 승계 경로가 살아
                # 있다 — 칩 하나가 손상돼도 무관한 승계까지 같이 죽이지 않는다(종전 동작 유지).
                logger.warning("relaxation_offer_read_failed", extra={"reason": str(exc)})
                relaxed = None
            if relaxed is not None:
                # 정확 일치는 "사용자가 우리가 만든 버튼을 눌렀다"는 명확한 신호라 일반 대화로
                # 라우팅될 여지가 없다 — decompose 가 의문문을 general 로 봤어도 추천으로 고정한다.
                decision.intent = "recommend"
                decision.filters = relaxed
            elif decision.scoped_to_previous and decision.intent == "recommend":
                # [#113] "그 중에 더 저렴한 걸로" — 직전 턴에 **자동 적용**된 완화를 이어받는다.
                # 사용자가 완화된 결과를 자기 후보로 인정한 것이라 칩 클릭과 같은 **동의 신호**로
                # 본다(팀 합의). 승계값은 `decision.filters` 에 녹아 아래 `_prepare_recommendation`
                # 의 `thread_store.put` 으로 영속된다 — 칩 클릭 경로와 같은 취급이다.
                #
                # **`intent == "recommend"` 일 때만 한다**(PR #248 리뷰). general 턴은 이 아래에서
                # `stream_fallback` 으로 바로 빠져 `decision.filters` 를 아무도 안 쓰므로, 승계를
                # 계산해 봐야 조용히 버려지고 위 주석만 거짓이 된다.
                # 칩 클릭 분기처럼 intent 를 **강제하지는 않는다** — 저쪽은 메시지가 우리가 만든 칩
                # label 과 정확히 일치해 오해의 여지가 없지만, `scopedToPrevious` 는 LLM 판정이라
                # "그 중에 뭐가 제일 인기 많아?" 같은 정보성 질문까지 추천으로 납치할 수 있다.
                # 리파인을 general 로 오분류한 턴은 승계 이전에 턴 전체가 어긋난 것이라, 그 증상
                # 하나만 덮기보다 라우팅 문제로 두는 편이 정직하다.
                # 참조가 **없는** 리파인("더 저렴한 걸로")은 여기 오지 않아 원래 조건으로 되돌아가고,
                # 그 턴에 다시 완화가 필요하면 다시 고지된다(SPEC "매 완화 알림" 유지).
                try:
                    # `applied` 는 위에서 `offers` 와 **같은 스냅샷으로 이미 읽었다**(PR #248 리뷰).
                    # 기준은 **이번 턴 filters** 다(칩 클릭 경로와 다르다) — 칩 클릭은 메시지가 칩
                    # 문구뿐이라 새 의도가 없어 prior 를 그대로 재현하지만, 여기서는 사용자가
                    # "그 중에 **더 저렴한** 걸로"처럼 새 조건을 함께 말한다. prior 를 기준으로 삼으면
                    # 이번 턴에 말한 조건이 통째로 버려진다. 완화 축 하나만 덮어쓴다.
                    #
                    # **단, 그 축을 이번 턴에 사용자가 다시 말했으면 승계하지 않는다**(PR #248 리뷰).
                    # "그 중에 평점 3.0 이상도 볼래" 처럼 같은 축의 새 값을 말했는데 저장된 완화값(4.0)
                    # 으로 덮으면 **방금 말한 조건이 흔적도 없이 사라진다.** 판정은 이번 턴 값이
                    # prior(직전 확정 필터)와 **다른가** 로 한다 — 다르면 이번 턴에 새로 언급한 것이다.
                    # prior 가 없으면(스레드 상태 유실) 비교할 근거가 없으므로 승계하지 않는다(엄격한 쪽).
                    carried = None
                    if _carry_axis_untouched_this_turn(applied, prior, decision.filters):
                        carried = _relaxed_filters_from_offer(applied, decision.filters)
                except Exception as exc:  # noqa: BLE001 - 손상된 저장 값이 턴을 죽이지 않게(degrade)
                    # 읽기는 위로 합쳐졌으니 여기 남은 실패는 **해석**뿐이다(저장 값 손상·스키마 혼재).
                    # 그래도 감싼 채로 둔다 — 승계는 편의 기능이라, 실패하면 사용자가 말한 조건만으로
                    # 검색하면 될 뿐 턴을 죽일 이유가 없다(§7 degrade 원칙).
                    logger.warning("relaxation_carry_failed", extra={"reason": str(exc)})
                    carried = None
                if carried is not None:
                    decision.filters = carried
                    # [#113] 승계 턴은 `recommend_pipeline` 의 `relax_field` 에 안 잡힌다(그건 "이번 턴에
                    # 채택된" 완화만 센다). 그런데 이 턴도 **사용자가 처음 말한 조건이 아닌 상태**로
                    # 결과를 받으므로 품질 지표에 그냥 섞으면 안 된다 — 여기서 따로 남긴다.
                    logger.info("relaxation_carried", extra={"field": applied.get("field")})

        # [#84·라운드 5] 회수/취소는 **`decision.intent` 가 확정된 뒤**에 가른다. 그 판단을
        # decompose 직후에 두면 순서 의존성이 생긴다 — 바로 위 완화 칩 정확 일치 분기가
        # `decision.intent = "recommend"` 로 **사후 재분류**하기 때문이다. 칩 label 은
        # `"65,000원까지 볼까요?"` 같은 의문문이라 decompose 가 `general` 로 볼 수 있고, 그러면
        # 옛 위치에서는 "취소해 놓고 나중에 추천이 되는" 조합이 나온다 — 그 턴은 분류기 판정이
        # 전혀 반영되지 않은 채(`scope_free=None`) 추천 경로를 탄다. **취소된 태스크의 산출은
        # 되살릴 수 없다**는 점이 이 순서를 강제한다.
        #
        # 옮긴 뒤에도 지키는 불변식: ① 아래 **모든 조기 return 보다 앞**이다(정리 누락 없음),
        # ② `try/finally` 가 이 지점을 포함한다(그 사이 완화 칩 pg 조회 `await` 에서 바깥 취소가
        # 와도 `finally` 가 정리한다), ③ 취소는 **동기**다(라운드 4 — `await` 하면 바깥 취소를
        # 삼킬 수 있다), ④ 비추천 턴은 분류기를 기다리지 않는다(동기 취소라 그 자체로 대기 0).
        #
        # **이미 나간 호출의 비용은 취소로 돌아오지 않는다.** 그것은 "intent 를 알기 전에 띄운다"는
        # 병렬 설계의 의도된 대가다 — 알고 나서 띄우면 추천 턴마다 한 호출이 직렬로 붙는다(#277 이
        # 밟은 자리). 취소로 **대기**만 없애고 호출은 남긴다.
        if decision.intent == "recommend":
            scope_free = await _collect_scope_task(scope_task)
        else:
            _cancel_scope_task(scope_task)
        scope_settled = True
    finally:
        # 어느 경로로 나가든 **정확히 한 번** 정리된다 — 추천 턴은 위에서 값을 회수하고
        # (`scope_settled`), 그 밖은 위·아래 어느 쪽이든 `_cancel_scope_task` 로 끝난다.
        # 이 `finally` 가 맡는 것은 **바깥에서 온 취소·teardown** 이다: `CancelledError` 는
        # `except LLMError` 에 걸리지 않아 본문 정리 지점을 전부 건너뛴다.
        if not scope_settled:
            _cancel_scope_task(scope_task)

    # transient 세션 버퍼에 발화 누적(승격 전 격리, SPEC-PROFILE-001) — 세션 종료 델타 소스.
    # [#119 REQ-PROF-026] intent 판정 **뒤에** 둔다: 주문조회·장바구니 조회 발화는 취향 신호가
    # 0인데 버퍼(슬라이딩 윈도우)를 채워 정작 취향 발화를 밀어낸다. 반복 발화는 지우지 않고
    # **상한**만 둔다 — 버퍼가 델타 추출 LLM 에 통째로 실려 반복 횟수가 곧 취향 강도가 되지만,
    # 전부 접으면 게이트의 반복 승격 경로(explicit OR repeated)까지 죽는다.
    # 대가로 decompose 실패 턴의 발화는 쌓이지 않는데, 의도를 파악하지 못한 발화는 취향
    # 신호로도 쓰지 않는다는 판단이다.
    # [#84] 빈 발화 가드 — conditionActions 만 있고 message 가 빈 턴(계약상 허용, api-spec §3.1)은
    # 취향 신호가 0인데 버퍼(슬라이딩 윈도우)만 밀어낸다. 공백-only 도 같이 막는다.
    if (
        profile_eligible
        and request.message.strip()
        and decision.intent not in settings.profile_buffer_excluded_intents
    ):
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

    # [라운드 24, #116·#117] 담기 허용 목록(경로 B 가드) — cart_add 뿐 아니라 wishlist_add 도
    # LLM 이 문맥 밖 productId 를 오추출해 찜하는 것을 막으려면 이 값이 **반드시** 필요하다
    # (api-spec §3.1 [보안]). intent 분기 전에 한 번만 계산해 cart_add·wishlist_add 가 같은
    # 값을 재사용한다 — 아래 cart_add 분기 docstring 에 있던 근거는 그대로 유효하다.
    screen = getattr(request, "screen", None)
    screen_product_ids = (
        {p.product_id for p in screen.products}
        if screen is not None and screen_context_active
        else set()
    )
    allowed = {pid for pid, _ in last_reco} | screen_product_ids

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

    # 화면 지시어("2번"·"이거") 해소 — cart_add·wishlist_add·wishlist_remove 세 분기가 공유한다
    # (api-spec §3.1 [보안] 문단, 이슈 #118·#116·#117). 여기서 **한 번만** 계산해 세 분기가 같은
    # `cart_intent` 를 쓴다(분기마다 같은 호출을 복붙하지 않는다) — 이 하나로 뽑아 두지 않으면
    # 분기 하나가 화면 해소를 빠뜨리는 결함이 재발한다("2번 찜해줘"가 어느 판별 경로로 오는지에
    # 따라 되고 안 되고가 갈리던 것이 바로 그 결함이었다). `screen`·`allowed` 는 위에서 intent
    # 분기보다 먼저 계산해 둔 값을 그대로 쓴다.
    #
    # 담기 허용 목록(`allowed`) = 직전 추천 ∪ screen.products 의 productId. screen 이 없거나
    # 무시된 요청은 last_reco 만으로 판정해 기존 동작과 동일하다. 프리패스가 아니다 — 두 목록
    # 밖 id 차단은 cart/graph.py 의 unresolved 판정이 그대로 맡는다.
    #
    # **되물음 턴에는 screen 합류를 끈다**(`screen_context_active`, 위 정의). 정본 문면은
    # "(누적 추천 ∪ `screen.products`) 를 allowed 로 취급"이라 이 게이트는 문면과 어긋난다 —
    # 그럼에도 그렇게 하는 근거:
    #   ① 같은 문단이 **강제**하는 것은 "두 목록 **밖**의 id 는 여전히 차단"이고, 빼는 것은
    #      **더 차단하는** 방향이라 그 보증을 깨지 않는다.
    #   ② 같은 문단이 스스로 밝힌 목적이 *"LLM 이 발화 속 임의 숫자를 오추출해 담는 것을 막는
    #      기존 가드는 유지된다"* 인데, 되물음 턴에서 screen id 를 allowed 에 두면 바로 그
    #      오추출이 **우연히 screen id 와 일치할 때** 가드를 통과한다. 실제로 재현했다 —
    #      `"502 그램짜리로 할게"` → 오추출 502 가 allowed 에 있어 stream_cart_add 의 전환
    #      조건을 통과, 되물음이 폐기되고 502 가 담겼다. 즉 게이트는 문면과 어긋나지만
    #      **그 문단의 목적을 지키는** 방향이다.
    #   ③ 잃는 실익이 없다. 4차 수정으로 되물음 턴에는 SCREEN 블록이 프롬프트에 실리지
    #      않으므로(테스트로 고정) LLM 은 screen 상품의 id 도 이름도 알 경로가 없고, id 는
    #      화면에 표시되지 않아 사용자가 말할 수도 없다. screen 상품이 동시에 직전 추천이면
    #      `last_reco` 쪽으로 그대로 allowed 에 남는다 — 정상 경로는 하나도 닫히지 않는다.
    cart_intent = decision.cart or CartIntent()
    screen_reason: str | None = None
    if decision.intent in ("cart_add", "wishlist_add", "wishlist_remove"):
        # [#118] 화면 지시어는 **코드가 해소**한다 — 순번·좌표·"후보 1건" 은 결정적인 규칙이라
        # 확률적 계층에 맡길 이유가 없고, 맡겼더니 사용자가 말하지 않은 상품을 확정하는 일이
        # 잦았다(실측표는 screen_reference 모듈 docstring). `screen.products` 가 있는 턴에만
        # 돌아서 #240 회귀 대조군(전부 screen 없음)에는 구조적으로 닿지 않는다. 옵션 되물음
        # 중에는 "2번"이 화면 순번이 아니라 옵션 번호라 아예 건너뛴다.
        if screen is not None and screen.products and screen_context_active:
            resolved = resolve_screen_reference(
                request.message,
                products=[(p.product_id, p.name) for p in screen.products],
                columns=screen.columns,
                allowed_product_ids=allowed,
                deictic_markers=settings.screen_deictic_markers,
                context_reference_markers=settings.screen_context_reference_markers,
                # [6차 리뷰] 이름 지목 검사(양보 (B))가 화면 상품만 보면, 직전 추천에만 있는
                # 이름을 지목했을 때 순번 규칙이 이겨 화면의 다른 상품으로 override 한다(오담기,
                # screen_reference.py 상단 F-8). `last_reco` 는 위에서 `allowed` 를 만들 때 이미
                # 손에 쥔 값을 그대로 넘긴다.
                last_recommendation_products=last_reco,
            )
            if resolved is not None:
                logger.info(
                    "screen_reference_resolved",
                    extra={"reason": resolved.reason, "forced_null": resolved.product_id is None},
                )
                cart_intent = replace(cart_intent, product_id=resolved.product_id)
                # 되물음 문구를 가르는 신호로만 넘긴다 — 확정된 사유는 문구와 무관하다. 찜
                # 스트림은 이 사유를 받지 않는다(아래 wishlist_add·wishlist_remove 참조) —
                # 해소된 product_id 만 전달되면 화면 지시어 자체는 해소된다.
                if resolved.product_id is None:
                    screen_reason = resolved.reason

    if decision.intent == "cart_add":
        if trace := current_request_trace():
            trace.set_lane("cart")
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_cart_add(
                identity=identity,
                cart=cart_intent,
                cart_store=cart_store,
                thread_key=thread_key,
                settings=settings,
                message=request.message,
                allowed_product_ids=allowed,
                screen_reason=screen_reason,
                observer=observer,
            ):
                yield frame
        return

    # decompose 가 cart_remove/wishlist_add/wishlist_remove 를 직접 산출하면 여기서 바로 각
    # 서브그래프로 위임한다. `cart/graph.py::stream_cart_add` 안의 `classify_cart_utterance`
    # (2선 방어, intent_guard.py)는 그대로 둔다 — decompose 가 이 발화를 여전히 `cart_add` 로
    # 오분류하면(예: 위 판정 순서 1-1)~1-3) 밖의 표현) 그 안에서 다시 갈라내 같은 세 서브그래프로
    # 보낸다. 즉 같은 발화가 ① 여기(위 분기) ② 저기(2선 방어) 두 경로 중 하나로 올 수 있는데,
    # **도착지도 입력도 같아졌기 때문에**(화면 해소를 위에서 한 번만 수행해 `cart_intent` 를
    # 공유한다) 중복 판정이 동작을 바꾸지 않는다 — 결과가 다르면 그건 두 판별기가 이견을 낸
    # 것이지 이 라우팅의 결함이 아니다.
    if decision.intent == "cart_remove":
        if trace := current_request_trace():
            trace.set_lane("cart")
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_cart_remove(
                identity=identity,
                message=request.message,
                cart_store=cart_store,
                thread_key=thread_key,
                settings=settings,
                observer=observer,
            ):
                yield frame
        return

    if decision.intent == "wishlist_add":
        if trace := current_request_trace():
            trace.set_lane("cart")
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_wishlist_add(
                identity=identity,
                cart=cart_intent,
                settings=settings,
                allowed_product_ids=allowed,
                observer=observer,
            ):
                yield frame
        return

    if decision.intent == "wishlist_remove":
        if trace := current_request_trace():
            trace.set_lane("cart")
        with trace_span("buyer.graph.cart", "chain"):
            async for frame in stream_wishlist_remove(
                identity=identity,
                cart=cart_intent,
                message=request.message,
                settings=settings,
                observer=observer,
            ):
                yield frame
        return

    if trace := current_request_trace():
        trace.set_lane("recommend")
    with trace_span("buyer.graph.recommendation", "chain"):
        # `_prepare_recommendation` 은 progress 프레임(mapping/expanding)을 내야 해서 async
        # generator 다 — `return` 을 못 쓰므로 결과는 이 홀더에 담아 받는다.
        prepare_out = _PrepareRecommendationOut()
        async for frame in _prepare_recommendation(
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
            out=prepare_out,
            # [#84] `RouteDecision` 에 싣지 않는다 — 그것은 **decompose 산출**을 담는 자료구조이고
            # 이 값은 다른 호출에서 온 별개 신호다. 섞으면 다음 사람이 decompose 가 낸 값으로
            # 오해하고, 그 오해 위에서 프롬프트를 고치게 된다.
            scope_free=scope_free,
        ):
            yield frame
        reverted = prepare_out.reverted
        # [#162] 조건 없음 판정은 **여기서** 한다 — `prior`(첫 턴 여부)가 이 스코프에만 있고,
        # `_prepare_recommendation` 이 카테고리 매핑·승계를 끝낸 뒤라야 `category_legs` 가 확정된다.
        no_condition = is_no_condition_turn(decision, prior)
        # [#336] 과소지정(no_condition 의 상위 집합) 판정 — 같은 이유로 여기서 한다(`prior`·
        # 확정된 `category_legs` 가 이 스코프에만 있다).
        underspecified = is_underspecified_turn(decision, prior, settings)
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
            relax_store=relax_store,  # [#113] 이번 턴 완화 칩을 기억해 다음 턴 클릭을 되받는다
            thread_key=thread_key,
            observer=observer,
            request_id=resolved_request_id,
            no_condition=no_condition,
            underspecified=underspecified,
            popular_fn=popular_fn,
            # [#119] 개인화 off(A/B baseline arm)면 취향 랭킹도 함께 끈다 — rerank 주입과 같은
            # 스위치를 따라야 arm 이 "개인화 없음"으로 일관된다.
            profile_vec=(None if settings.profile_injection_scope == "off" else profile_vec),
        ):
            yield frame
