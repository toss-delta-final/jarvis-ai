"""추천 서브그래프 내부 상태·헬퍼 (이슈 #2 MVP 슬라이스).

decompose 산출(RouteDecision)·rerank 산출(RerankResult)·conditions 칩 파생을 담는다.
전체 SPEC State(RerankValidation·BundleState·relaxation·sources·priority 등)는
후속(SPEC-RECOMMEND-001 고급기능) — 본 슬라이스는 선형 파이프라인에 필요한 최소만 둔다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from weakref import WeakValueDictionary

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.core import pg_store
from app.core.llm import LLMError
from app.core.pg_resilience import mutation_lock, run_with_query_timeout
from app.core.text import _strip_unsafe
from app.schemas.chat import ConditionChip
from app.schemas.spring import ProductSearchFilters

if TYPE_CHECKING:
    from app.agents.buyer.recommendation.rerank_code_assisted import CodeAssistedDecision
    from app.agents.buyer.recommendation.rerank_grounding import GroundingDecision
    from app.agents.buyer.recommendation.rerank_scoring import RankingDecision

_NAMESPACE_ROOT = "buyer_revert_v2"
_CATEGORIES_KEY = "categories"
_REPURCHASE_NAMESPACE_ROOT = "buyer_repurchase_v1"
_PRODUCT_IDS_KEY = "product_ids"
_RELAX_NAMESPACE_ROOT = "buyer_relaxation_offers_v1"  # [#113] 완화 칩 제안 기억
_SNAPSHOT_KEY = "turn"  # 이번 턴 화면 상태 — 아래 둘을 한 덩어리로 쓴다(부분 실패 차단)
_OFFERS_KEY = "offers"  # 제안한 칩(누르면 이렇게 됩니다)
_APPLIED_KEY = "applied"  # 자동 적용된 완화(서버가 이미 이렇게 했습니다)

# key(thread_key)별 asyncio.Lock — RevertStore.add() 의 get→put(read-modify-write) 구간을
# 직렬화한다. 동일 스레드로 겹치는 요청(멀티탭·연속 발화)이 오면 나중 aput 이 앞선 갱신을
# 덮어써 되돌리기 카테고리가 유실될 수 있다(lost update, PR #46 리뷰).
#
# 실 PostgreSQL 경로는 mutation_lock의 advisory lock으로 인스턴스 간 직렬화한다. InMemory/test
# 경로만 이 로컬 lock을 사용하며, WeakValueDictionary라 유휴 key는 GC가 자동 회수한다(이슈 #50).
_add_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_repurchase_add_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _lock_for(key: str) -> asyncio.Lock:
    lock = _add_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _add_locks[key] = lock
    return lock


def _repurchase_lock_for(key: str) -> asyncio.Lock:
    lock = _repurchase_add_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _repurchase_add_locks[key] = lock
    return lock


@dataclass
class CartIntent:
    """decompose 가 추출한 장바구니 의도(이슈 #3). productId 는 직전 추천 문맥에서 해소."""

    product_id: int | None = None
    option_id: int | None = None
    quantity: int = 1
    # [#285, I-25 §4.13] 수량 **치환** 목표값 — 담기(I-2)의 `quantity`(합산, 기본값 1)와 의미가
    # 다르다. `quantity` 는 "추출 실패"에도 기본값 1이 그대로 실려 "명시적으로 1개"와 구별이
    # 안 되지만(담기는 그래도 안전 — 최소 1개를 담는 게 무해한 기본이라서다), 수량 변경은
    # 그 기본값을 그대로 쓰면 "수량 바꿔줘"(목표 미상)가 조용히 1개로 치환돼 장바구니를
    # 망가뜨린다. 그래서 **`None` = 미해소 = 되물음**을 표현할 수 있는 별도 필드를 둔다 —
    # `quantity` 필드 자체는 담기 경로가 계속 쓰므로 건드리지 않는다.
    target_quantity: int | None = None


@dataclass(frozen=True, slots=True)
class ScreenReference:
    """decompose가 구조화한 화면 지목 의미. 상품 ID가 아니라 좌표만 전달한다."""

    kind: Literal["grid"]
    row: int | None
    column: int | None


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

    intent: Literal[
        "recommend",
        "cart_add",
        "cart_view",
        "order_status",
        "general",
        # [라운드 24, #116·#117] decompose 가 직접 산출하는 삭제·찜 intent. cart/graph.py 의
        # classify_cart_utterance(cart_add 로 들어온 발화의 2선 방어)와 값 집합이 겹친다 —
        # buyer/graph.py 의 라우팅 docstring 참조.
        "cart_remove",
        "wishlist_add",
        "wishlist_remove",
        # [#285, I-25 §4.13] 수량 변경(치환). cart_remove 와 같은 성격 — decompose 가 직접
        # 산출하는 경로와 classify_cart_utterance(cart_add 로 들어온 발화의 2선 방어, §4.13)가
        # 값 집합을 공유한다(buyer/graph.py 라우팅 docstring 참조). `cart_add`(합산, I-2)와는
        # 반대 동작이라 오분류하면 재고 판정이 조용히 달라진다(패킷 함정 2).
        "cart_quantity",
        # [#386] 찜 목록 조회. cart_view 와 같은 성격(상태를 바꾸지 않는 조회)이라
        # classify_cart_utterance 의 값 집합에는 들어가지 않는다 — 그 판별기는 cart_add 로
        # 라우팅된 발화만 보고, 조회 발화는 애초에 거기 도달하지 않는다(intent_guard.py 참조).
        "wishlist_view",
    ]
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
    # [#664] LLM은 자연어의 행·열 의미만 구조화한다. 배열 index와 productId는 buyer graph가
    # 검증된 화면 표면·columns로 계산하며, 이 내부 필드는 외부 wire나 상태 저장소로 나가지 않는다.
    screen_reference: ScreenReference | None = None
    revert_categories: list[str] = field(default_factory=list)  # 소모품 억제 되돌리기(결정 14-F)
    # [#120] 명시 재구매/재추천 지목(상품 지칭 텍스트) — 최근 구매 exact 제외를 되돌리는 신호.
    # productId 가 아니라 **텍스트**인 이유는 graph 가 본인 구매 이력에 대해서만 해소해 신뢰
    # 경계를 유지하기 때문(#120).
    # graph 가 이 텍스트를 본인 최근 구매 이력의 productId 로 해소해 RepurchaseStore 에 누적한다.
    # 이후 매 턴 저장값을 그 시점의 최근 구매 이력과 교집합으로 재검증하므로, 저장소가 오염돼도
    # 면제 대상은 항상 본인 최근 구매의 부분집합이라는 신뢰 경계를 유지한다(#232).
    repurchase_products: list[str] = field(default_factory=list)
    # [#113] 이번 발화가 **직전에 보여준 결과 집합을 가리키는가**("그 중에", "여기서", "보여준
    # 것들에서"). 참인 턴은 직전 턴에 자동 적용된 완화를 이어받는다 — 사용자가 완화된 결과를
    # 자기 후보로 인정한 것이라 칩 클릭과 같은 **동의 신호**로 본다(팀 합의).
    # 기본 False(엄격) — 판정을 놓치면 원래 조건으로 되돌아가 또 완화·또 고지라 무해하지만,
    # 반대로 오탐하면 사용자가 말한 조건이 조용히 바뀌므로 애매하면 False 로 기운다.
    scoped_to_previous: bool = False
    # [#60] 목록 안 상품을 전부 사는가(BUY_ALL) — 하나만 고르는가(PICK_ONE). 판정 기준은 예산이
    # 아니다(api-spec §4.2): "감자탕 재료"는 예산이 없어도 BUY_ALL, "5만원으로 파우치"는 예산이
    # 있어도 PICK_ONE. 기본 False(엄격) — 오탐하면 사용자가 고르려던 대안이 세트로 묶여 버린다.
    buy_all: bool = False
    # [#60] 총액 예산(원). **개별 상품 상한(filters.price_max)과 다른 축**이다 — "각각 5만원"은
    # price_max, "전부 5만원"은 이 값이다. BUY_ALL + 이 값이 둘 다 있을 때만 push totalBudget 에
    # 실린다(§4.2). 음수는 decompose 파싱이 None 으로 떨군다.
    total_budget: int | None = None
    # 카테고리 하이브리드 매핑(이슈 #59, 방식 A):
    category_queries: list[CategoryQuery] = field(default_factory=list)  # decompose 추측(매핑 전)
    # [#443] 사전 기반 보강이 빈 모델 legs를 실제로 채웠는가. 모델이 원래 낸 leg와 구분해
    # 측정 산출물에서 condition_only 주입 0건 하드 불변식을 재집계한다.
    category_leg_injected: bool = False
    # 모델이 낸 축인지 후처리가 지운 축인지를 산출물에서 가르기 위한 진단 필드.
    attr_conditions_suppressed_axes: list[str] = field(default_factory=list)
    # 매핑 후 (canonical, query) leg 리스트(그래프가 채움; 신호 없거나 실패 시 빈 리스트 → 무필터,
    # #22) — fan-out 검색 leg 단위(§6).
    # query 는 그 카테고리 전용 검색 키워드. 대표 카테고리 = category_legs[0][0](칩·멀티턴 승계).
    category_legs: list[tuple[str, str | None]] = field(default_factory=list)
    # [#222] 이번 턴이 광역 발화 → leaf fan-out 폴백으로 category_legs 를 채웠는가. 참이면
    # 조건 칩에서 카테고리를 뺀다(칩 하나로 8개 leg 을 대표할 수 없다, #51 표시=실제) — 대신
    # 확장 고지 token 으로 검색한 중분류를 알린다.
    category_expanded: bool = False
    # [이슈 #434 라운드2] 값 지정 category 제거가 이 턴에 남은 카테고리 집합(대표 1개가 아니라
    # 실제로 검색한 집합)을 복원해 `category_legs` 를 멀티 leg 로 채웠는가(`buyer/graph.py`
    # `_prepare_recommendation` 의 승계 분기가 채운다). 참이면 `split_by_need`(및 그에 의존하는
    # `buy_all_mode`)를 억제한다 — 이 멀티 leg 은 새 니즈 전개가 아니라 **복원된 집합**이라
    # `case == 3` 우연 일치로 목록이 니즈별로 쪼개지면 "표시=실제"(#51) 원칙이 깨진다
    # (category_expanded 가 조건 칩을 억제하는 것과 같은 이유).
    category_legs_restored: bool = False
    # [#162] `filters.semantic_query` 가 **이번 턴 원문으로 폴백**된 값인가.
    # decompose 는 `llm_sq or cat_signal or prior_sq or query` 순으로 채우므로(decompose.py)
    # 이 필드는 **절대 비지 않는다** — 사용자가 아무 의미 신호를 주지 않아도 발화 원문이 들어온다.
    # 따라서 "조건 없는 발화"(#162)는 값의 유무로 판정할 수 없고 **출처**로 갈라야 한다.
    # 기본 False(= 진짜 신호가 있다)가 보수적이다 — 판정을 놓치면 종전 동작이지만, 반대로
    # 오탐하면 사용자가 말한 의미를 버리고 인기상품을 준다.
    semantic_query_is_fallback: bool = False


@dataclass
class RerankResult:
    """rerank(Sonnet) 산출 — 노출 순서 id + 상품별 근거, 전체 코멘트."""

    ranked: list[tuple[int, str]] = field(default_factory=list)  # (productId, rationale)
    overall_comment: str = ""
    overall_claims: tuple[Mapping[str, object], ...] = ()
    grounding_decisions: list[GroundingDecision] = field(default_factory=list)
    ranking_decisions: list[RankingDecision] = field(default_factory=list)
    code_assisted_decisions: list[CodeAssistedDecision] = field(default_factory=list)


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
    카테고리 칩을 먼저 둔다(api-spec §3.1 (3) 예시 순).

    [이슈 #434] 멀티 값 축(category·brand)은 **값당 칩 1개**를 낸다 — `value` 는 항상
    스칼라다. FE 는 배열 키를 (field, value) 조합으로 잡아야 한다(같은 field 가 반복될 수
    있다). 칩 제거(conditionActions)의 값 지정 제거가 "그 값만" 지우려면 애초에 값이 갈라져
    있어야 하고, 값을 조인 문자열 하나로 뭉치면 되돌릴 수 없다.

    categories 가 주어지면(fan-out 매핑 결과 canonical 전체) 그 값마다 카테고리 칩을 만든다.
    미지정이면 filters.category(스칼라) 로 파생한다(비-fan-out 보존). 단일 값일 때는 종전과
    바이트 동일(`label="카테고리 · X"`, `value="X"`).
    """
    chips: list[ConditionChip] = []
    seen_chip_keys: set[tuple[str, str]] = set()
    # categories(fan-out canonical 전체)는 빈 리스트(매핑 결과 없음)와 None(미지정·비-fan-out)을
    # 구분한다 — 빈 리스트는 filters.category 로 폴백하지 않아 미검증 원문이 칩에 새지 않는다(#16).
    source = (
        categories if categories is not None else ([filters.category] if filters.category else [])
    )
    for raw in source:
        if not raw:
            continue
        value = _strip_unsafe(raw)
        if not value:
            continue
        key = ("category", value)
        if key in seen_chip_keys:
            # leg 매핑이 같은 canonical 을 두 번 낼 수 있다 — 정제 후 순서 보존 중복 제거(#434).
            continue
        seen_chip_keys.add(key)
        chips.append(ConditionChip(field="category", label=f"카테고리 · {value}", value=value))
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
    for raw in filters.brand or []:
        if not raw:
            continue
        value = _strip_unsafe(raw)
        if not value:
            continue
        key = ("brand", value)
        if key in seen_chip_keys:
            continue
        seen_chip_keys.add(key)
        chips.append(ConditionChip(field="brand", label=value, value=value))
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


class RepurchaseStore:
    """스레드별 재구매 exact 제외 면제 productId 목록 — 오래된 순서대로 유계 누적한다."""

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    async def get(self, key: str) -> list[int]:
        item = await run_with_query_timeout(
            self._store.aget((_REPURCHASE_NAMESPACE_ROOT, key), _PRODUCT_IDS_KEY)
        )
        if item is None or not isinstance(item.value, dict):
            return []
        values = item.value.get(_PRODUCT_IDS_KEY)
        if not isinstance(values, list):
            return []
        return [value for value in values if type(value) is int]

    async def add(self, key: str, product_ids, *, cap: int) -> list[int]:
        """누적·상한 적용 결과를 반환해 첫 SSE 전 불필요한 재조회 왕복 1회를 없앤다."""
        if not product_ids:
            # 호출부는 빈 입력을 get으로 분기하지만, 직접 호출도 락·쓰기 없이 순수 읽기로 둔다.
            return await self.get(key)
        async with mutation_lock(
            self._store,
            f"buyer:repurchase:{key}",
            _repurchase_lock_for(key),
        ):
            current = await self.get(key)
            incoming: list[int] = []
            for product_id in product_ids:
                if type(product_id) is not int:
                    continue
                if product_id in incoming:
                    incoming.remove(product_id)
                incoming.append(product_id)
            incoming_ids = set(incoming)
            merged = [product_id for product_id in current if product_id not in incoming_ids]
            merged.extend(incoming)
            retained = merged[-cap:] if cap > 0 else []
            await run_with_query_timeout(
                self._store.aput(
                    (_REPURCHASE_NAMESPACE_ROOT, key),
                    _PRODUCT_IDS_KEY,
                    {_PRODUCT_IDS_KEY: retained},
                )
            )
            return retained


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

    두 값(`offers`·`applied`)은 **한 스냅샷**이다 — 둘 다 "이번 턴 화면에 무엇이 있었나"를
    기술한다. 그래서 **단일 키에 함께 저장**한다(PR #248 리뷰): 따로 두 번 쓰면 앞의 쓰기만
    성공하고 뒤가 pg 일시 장애로 실패했을 때 `offers` 는 이번 턴인데 `applied` 는 지난 턴인
    **찢어진 상태**가 남고, 다음 턴 "그 중에" 가 화면에 보여준 적 없는 완화를 이어붙인다.
    한 번의 `aput` 으로 만들면 그 부분 실패가 **구조적으로 불가능**해진다.
    """

    def __init__(self, store: BaseStore | None = None) -> None:
        self._store = store or InMemoryStore()

    async def _snapshot(self, key: str) -> dict:
        item = await run_with_query_timeout(
            self._store.aget((_RELAX_NAMESPACE_ROOT, key), _SNAPSHOT_KEY)
        )
        return item.value if item and isinstance(item.value, dict) else {}

    async def get_snapshot(self, key: str) -> tuple[dict[str, dict], dict | None]:
        """`(offers, applied)` 를 **한 번의 왕복으로 함께** 돌려준다.

        - `offers`: `label → {"field":…, "value":…}` — "누르면 이렇게 됩니다"(미동의). 없으면 빈 dict.
        - `applied`: 직전 턴에 **자동 적용**된 완화 — "서버가 이미 이렇게 적용했습니다"(미동의).
          다음 턴이 그 결과 집합을 가리키면("그 중에") 사용자가 수용한 것으로 보고 이어받는다(#113).

        **둘을 따로 읽지 않는다**(PR #248 리뷰). 쓰기를 원자적으로 묶어 찢어진 상태를 없애 놓고
        읽기를 두 번으로 나누면, 두 `aget` 사이에 다른 턴의 `put` 이 끼었을 때 `offers` 는 옛
        스냅샷 · `applied` 는 새 스냅샷인 **같은 찢어짐이 읽기 쪽에서 되살아난다.** 한 번 읽어
        둘 다 뽑으면 pg 왕복도 절반이다(승계 경로는 종전에 왕복 2회였다).
        """
        snapshot = await self._snapshot(key)
        offers = snapshot.get(_OFFERS_KEY)
        applied = snapshot.get(_APPLIED_KEY)
        return (
            offers if isinstance(offers, dict) else {},
            applied if isinstance(applied, dict) else None,
        )

    async def put(self, key: str, offers: dict[str, dict], applied: dict | None) -> None:
        """이번 턴 스냅샷으로 **통째 교체**한다 — 부분 갱신을 API 로 열어두지 않는다.

        락 불필요(읽고 더하는 게 아니라 덮어쓴다). 상품 후보가 확정된 턴마다 덮어쓴다 — 완화가
        안 걸린 턴에 옛 값이 남아 있으면 한참 뒤 "그 중에" 발화가 **화면에 있지도 않았던** 완화를
        되살린다.

        **호출되지 않는 경로가 있다**(검색 실패 `SEARCH_FAILED` 는 이 지점 전에 종료). 그런 턴은
        옛 값이 그대로 남는데, 의도된 쪽이다 — 사용자가 마지막으로 **본** 결과는 여전히 그 완화가
        걸린 목록이라 "그 중에"의 지시 대상이 바뀌지 않았다. 반대로 0건 턴은 여기까지 와서 비우므로
        승계가 끊기는데, 이것도 안전한 쪽이다(원래 조건으로 돌아가 다시 고지). 지시 대상이 애매한
        구간에서는 **승계하지 않는 쪽으로 기운다**(#113 설계 원칙).
        """
        await run_with_query_timeout(
            self._store.aput(
                (_RELAX_NAMESPACE_ROOT, key),
                _SNAPSHOT_KEY,
                {_OFFERS_KEY: offers, _APPLIED_KEY: applied},
            )
        )


async def get_revert_store() -> RevertStore:
    """되돌리기 스토어 — pg-profile 공유 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return RevertStore(await pg_store.get_store())


async def get_repurchase_store() -> RepurchaseStore:
    """재구매 면제 스토어 — pg-profile 공유 연결 백엔드(요청마다 얇은 래퍼 재생성)."""
    return RepurchaseStore(await pg_store.get_store())


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


def reset_repurchase_store() -> None:
    """테스트 격리용 — 공유 pg-profile store와 재구매 key별 락을 초기화한다.

    pytest-asyncio가 테스트마다 새 이벤트 루프를 쓰므로 이전 루프에 묶인 stale ``Lock``을
    재사용하면 hang이 날 수 있다. 따라서 저장값뿐 아니라 전용 lock dict도 함께 비운다.
    """
    pg_store.reset_store()
    _repurchase_add_locks.clear()
