"""추천 서브그래프 내부 상태·헬퍼 (이슈 #2 MVP 슬라이스).

decompose 산출(RouteDecision)·rerank 산출(RerankResult)·conditions 칩 파생을 담는다.
전체 SPEC State(RerankValidation·BundleState·relaxation·sources·priority 등)는
후속(SPEC-RECOMMEND-001 고급기능) — 본 슬라이스는 선형 파이프라인에 필요한 최소만 둔다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Literal
from weakref import WeakValueDictionary

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.core import pg_store
from app.core.llm import LLMError
from app.core.pg_resilience import mutation_lock, run_with_query_timeout
from app.core.text import _strip_unsafe
from app.schemas.chat import ConditionChip
from app.schemas.spring import ProductSearchFilters

_NAMESPACE_ROOT = "buyer_revert_v2"
_CATEGORIES_KEY = "categories"
_RELAX_NAMESPACE_ROOT = "buyer_relaxation_offers_v1"  # [#113] 완화 칩 제안 기억
_OFFERS_KEY = "offers"

# key(thread_key)별 asyncio.Lock — RevertStore.add() 의 get→put(read-modify-write) 구간을
# 직렬화한다. 동일 스레드로 겹치는 요청(멀티탭·연속 발화)이 오면 나중 aput 이 앞선 갱신을
# 덮어써 되돌리기 카테고리가 유실될 수 있다(lost update, PR #46 리뷰).
#
# 실 PostgreSQL 경로는 mutation_lock의 advisory lock으로 인스턴스 간 직렬화한다. InMemory/test
# 경로만 이 로컬 lock을 사용하며, WeakValueDictionary라 유휴 key는 GC가 자동 회수한다(이슈 #50).
_add_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _lock_for(key: str) -> asyncio.Lock:
    lock = _add_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _add_locks[key] = lock
    return lock


@dataclass
class CartIntent:
    """decompose 가 추출한 장바구니 의도(이슈 #3). productId 는 직전 추천 문맥에서 해소."""

    product_id: int | None = None
    option_id: int | None = None
    quantity: int = 1


@dataclass
class CategoryQuery:
    """decompose 가 추출한 카테고리 추측 1건(이슈 #59, 방식 A).

    raw_category 는 LLM 의 자유 추측(매핑 전, DB 실재값이 아닐 수 있음), query 는 그 카테고리
    전용 검색 키워드(fan-out leg 에서 사용). 그래프가 raw_category 를 임베딩 보정해 canonical
    카테고리로 바꾼다.
    """

    raw_category: str | None = None
    query: str | None = None


@dataclass
class RouteDecision:
    """decompose(Haiku) 1회 산출 — intent 라우팅 + 병합 필터/의미쿼리/case + 폴백 답변 + 장바구니 의도."""

    intent: Literal["recommend", "cart_add", "cart_view", "order_status", "general"]
    filters: ProductSearchFilters
    # [#101] 의미쿼리는 검색 입력이라 filters.semantic_query 로 이관(decompose 가 세팅). 하류가
    # 그 필드를 읽으므로 RouteDecision 에는 더 두지 않는다.
    # [이슈 #198] 전개 게이트로 사용 — `case != 3` 이면 상품 전개를 발동하지 않는다
    # (`needs_expansion.detect_expansion_need`, DESIGN-NEEDS-EXPANSION-198 §4.2). case 2
    # ("5만원 이하 아무거나")의 무필터 의도를 보호하는 유일한 신호이므로 제거하면 안 된다.
    # 단일/멀티 판정에는 여전히 쓰지 않는다(len(category_queries) 기준, #59).
    case: int = 2
    reply: str = ""  # intent == general 일 때만 사용자에게 줄 답변
    cart: CartIntent | None = None  # intent == cart_add/cart_view 일 때
    revert_categories: list[str] = field(default_factory=list)  # 소모품 억제 되돌리기(결정 14-F)
    # 카테고리 하이브리드 매핑(이슈 #59, 방식 A):
    category_queries: list[CategoryQuery] = field(default_factory=list)  # decompose 추측(매핑 전)
    # 매핑 후 (canonical, query) leg 리스트(그래프가 채움; 신호 없거나 실패 시 빈 리스트 → 무필터,
    # #22) — fan-out 검색 leg 단위(§6).
    # query 는 그 카테고리 전용 검색 키워드. 대표 카테고리 = category_legs[0][0](칩·멀티턴 승계).
    category_legs: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class RerankResult:
    """rerank(Sonnet) 산출 — 노출 순서 id + 상품별 근거, 전체 코멘트."""

    ranked: list[tuple[int, str]] = field(default_factory=list)  # (productId, rationale)
    overall_comment: str = ""


def extract_json(text: str) -> dict:
    """LLM 응답 문자열에서 첫 '{'~마지막 '}' 구간의 JSON 객체를 파싱한다(코드펜스 허용).

    파싱 불가/객체 아님이면 LLMError — 상위가 degrade/error 로 처리한다.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError("LLM 응답에서 JSON 을 찾지 못함")
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError) as exc:
        raise LLMError("LLM JSON 파싱 실패") from exc
    if not isinstance(obj, dict):
        raise LLMError("LLM JSON 이 객체가 아님")
    return obj


def build_condition_chips(
    filters: ProductSearchFilters, *, categories: list[str] | None = None
) -> list[ConditionChip]:
    """병합 필터에서 conditions 칩을 결정론적으로 파생한다(FE 제거 가능, 카드 아님).

    LLM 의 임의 conditions 출력에 의존하지 않고 확정된 필터에서 파생 — 테스트 가능·일관.
    카테고리 칩을 먼저 둔다(api-spec §3.1 (2) 예시 순).

    categories 가 주어지면(fan-out 매핑 결과 canonical 전체)로 카테고리 칩을 만든다 — 멀티면
    검색한 카테고리 전부를 조인 문자열 하나로 표시한다(api-spec §3.1 예시가 value 를 스칼라
    문자열로 명시하므로 계약 정합을 위해 리스트가 아닌 문자열). 칩 제거 왕복은 field 단위라
    카테고리 칩은 멀티여도 1개로 유지한다. 미지정이면 filters.category 로 파생(비-fan-out 보존).
    """
    chips: list[ConditionChip] = []
    # categories(fan-out canonical 전체)는 빈 리스트(매핑 결과 없음)와 None(미지정·비-fan-out)을
    # 구분한다 — 빈 리스트는 filters.category 로 폴백하지 않아 미검증 원문이 칩에 새지 않는다(#16).
    source = (
        categories if categories is not None else ([filters.category] if filters.category else [])
    )
    cats = [c for c in source if c]
    if cats:
        cats = [_strip_unsafe(c) for c in cats]
        joined = " · ".join(cats)  # 단일=그 값, 멀티=전체 조인(스칼라 문자열 — §3.1 정합)
        chips.append(ConditionChip(field="category", label=f"카테고리 · {joined}", value=joined))
    if filters.price_max is not None:
        chips.append(
            ConditionChip(
                field="priceMax", label=f"{filters.price_max:,}원 이하", value=filters.price_max
            )
        )
    if filters.price_min is not None:
        chips.append(
            ConditionChip(
                field="priceMin", label=f"{filters.price_min:,}원 이상", value=filters.price_min
            )
        )
    if filters.brand:
        brands = [_strip_unsafe(brand) for brand in filters.brand]
        chips.append(ConditionChip(field="brand", label=" · ".join(brands), value=brands))
    if filters.rating_min is not None:
        chips.append(
            ConditionChip(
                field="ratingMin", label=f"평점 {filters.rating_min}+", value=filters.rating_min
            )
        )
    if filters.keyword:
        keyword = _strip_unsafe(filters.keyword)
        chips.append(ConditionChip(field="keyword", label=keyword, value=keyword))
    return chips


class RevertStore:
    """스레드별 소모품 억제 되돌리기 카테고리 집합 — LangGraph BaseStore(pg-profile) 백엔드(신원 스코프 키).

    사용자가 "다시 추천받기"(되돌리기 칩)한 카테고리는 이후 턴에서도 억제하지 않는다(결정 14-F).
    """

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    async def get(self, key: str) -> set[str]:
        item = await run_with_query_timeout(
            self._store.aget((_NAMESPACE_ROOT, key), _CATEGORIES_KEY)
        )
        return set(item.value[_CATEGORIES_KEY]) if item else set()

    async def add(self, key: str, categories) -> None:
        if not categories:
            return
        async with mutation_lock(
            self._store,
            f"buyer:revert:{key}",
            _lock_for(key),
        ):
            current = await self.get(key)
            current.update(categories)
            await run_with_query_timeout(
                self._store.aput(
                    (_NAMESPACE_ROOT, key),
                    _CATEGORIES_KEY,
                    {_CATEGORIES_KEY: sorted(current)},
                )
            )


class RelaxationOfferStore:
    """스레드별 **직전 턴에 제안한 완화 칩** — 칩 클릭을 결정론적으로 되받기 위한 기억(#113).

    FE 는 완화 칩을 누르면 **칩 label 을 다음 턴 message 로 그대로 보낸다**(jarvis-frontend
    `SuggestionChips.onApply` → `useChat.applySuggestion` → `send(label)`). 그런데 label 은
    "65,000원까지 볼까요?" 같은 **의문문**이라, 그대로 두면 decompose(LLM)가 그 문장에서 숫자를
    다시 뽑아내야 한다 — 우리가 이미 정확히 계산해 둔 값(65000)을 버리고 추측으로 복원하는 셈이고,
    질문으로 해석되면 완화가 아예 안 걸린다.

    그래서 칩을 내보낼 때 `label → (field, value)` 를 기억해 두고, 다음 턴 message 가 그 label 과
    **정확히 일치**하면 LLM 해석을 건너뛰고 저장된 값을 그대로 적용한다. 일치하지 않으면 아무 일도
    하지 않고 기존 경로(decompose 해석)로 흐른다 — 지금보다 나빠지지 않는다.

    턴마다 **덮어쓴다**(누적하지 않는다) — 화면에 떠 있는 칩은 항상 마지막 턴 것뿐이라, 옛 제안이
    남아 있으면 사용자가 보지도 않은 조건이 되살아난다.
    """

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    async def get(self, key: str) -> dict[str, dict]:
        """`label → {"field":…, "value":…}`. 저장분이 없으면 빈 dict."""
        item = await run_with_query_timeout(
            self._store.aget((_RELAX_NAMESPACE_ROOT, key), _OFFERS_KEY)
        )
        offers = item.value.get(_OFFERS_KEY) if item else None
        return offers if isinstance(offers, dict) else {}

    async def put(self, key: str, offers: dict[str, dict]) -> None:
        """이번 턴 제안으로 **교체**한다(빈 dict 면 비우기). 락 불필요 — 읽고 더하는 게 아니라 덮어쓴다."""
        await run_with_query_timeout(
            self._store.aput(
                (_RELAX_NAMESPACE_ROOT, key),
                _OFFERS_KEY,
                {_OFFERS_KEY: offers},
            )
        )


async def get_revert_store() -> RevertStore:
    """되돌리기 스토어 — pg-profile 공유 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return RevertStore(await pg_store.get_store())


async def get_relaxation_offer_store() -> RelaxationOfferStore:
    """완화 칩 제안 스토어 — 되돌리기와 같은 pg-profile 백엔드(요청마다 얇은 래퍼)."""
    return RelaxationOfferStore(await pg_store.get_store())


def reset_revert_store() -> None:
    """테스트 격리용 — 공유 pg-profile store(InMemoryStore)를 비우고 key별 락도 초기화한다.

    `_add_locks` 도 비운다 — pg_store.py 의 `_init_lock` 과 동일한 이유로, pytest-asyncio
    의 테스트 함수별 새 이벤트 루프에서 이전 루프에 묶인 stale `Lock` 을 재사용하면
    hang 이 발생할 수 있다(app/core/pg_store.py 리뷰와 동일 클래스 버그).
    """
    pg_store.reset_store()
    _add_locks.clear()
