"""추천 파이프라인 스트리밍 (SPEC-RECOMMEND-001 §5.3/§6, 이슈 #2 MVP 슬라이스).

decompose 산출(RouteDecision) 이후: conditions → search(Spring 위임) → rerank(Sonnet) →
근거 token → push(I-21) → products.ready(경로 B) → done.
degrade(§7): SEARCH_FAILED(error·종료) / rerank 실패→검색순서 폴백 / push 실패→products.ready 스킵.
SSE 는 상품 카드를 싣지 않는다(경로 B) — products.ready 는 {sessionId, listIds} 상관키만.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.buyer._frames import sse
from app.agents.buyer.recommendation.budget_sets import BudgetSet, build_budget_sets
from app.agents.buyer.recommendation.no_condition import (
    has_total_budget,
    rank_by_profile,
    within_budget,
)
from app.agents.buyer.recommendation.rerank import rerank
from app.agents.buyer.recommendation.relaxation import (
    FIELD_TO_ATTR as RELAXATION_FIELD_TO_ATTR,
    RelaxationCandidate,
    build_relaxation_candidates,
)
from app.agents.buyer.recommendation.state import (
    RouteDecision,
    build_condition_chips,
    get_repurchase_store,
)
from app.core.llm import LLMClient, LLMError, resolve_model_id
from app.core.text import _strip_unsafe
from app.core.tracing import current_request_trace, trace_span
from app.services import spring_client
from app.schemas.chat import (
    ConditionsData,
    DoneData,
    ErrorData,
    ProductsReadyData,
    RelaxationRef,
    RevertRef,
    SuggestionChip,
    SuggestionsData,
    TokenData,
)
from app.schemas.spring import (
    LIST_MAX_PRODUCTS,
    ProductSearchFilters,
    ProductSearchResult,
    RecoReason,
    LIST_LABEL_MAX_LEN,
    MAX_LISTS,
    RecommendationListEntry,
    RecommendationPush,
    SpringProduct,
)
from app.services.spring_client import SpringUnavailableError

logger = logging.getLogger(__name__)

_INACTIVE_STATUSES = frozenset(
    {"CANCELED", "CANCELLED", "RETURNED"}
)  # 보유 아님(철자 양쪽 — spec §4.7 혼용) → dedup 제외 대상 아님


def _now() -> datetime:
    """현재 시각 — naive-UTC(ordered_at 정규화와 동일 기준으로 비교, 테스트 주입 지점)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sanitize_reason(text: str, max_len: int) -> str:
    """I-21 reason 방어 정제 — 제어·포맷 문자 제거 + 연속 공백 접기 + 안전 상한 truncate.

    rerank rationale 은 판매자 입력(상품명·브랜드)에 영향받는 자유 텍스트라 신뢰경계(→Spring→CH-5→FE)를
    넘기 전에 정제한다(§4.2 이슈 #61). (1) 비-whitespace 제어문자(NUL·ESC·DEL 등)와 zero-width·bidi
    포맷 문자를 제거하고(`\\s` 로는 안 걸리는 표시 조작/주입 문자), (2) 남은 공백류(개행 포함)를 단일
    공백으로 접은 뒤, (3) max_len 방어캡으로 자른다. 표시 목표(한글 40자)는 프롬프트로 유도하고, max_len
    은 비정상 초장문·인젝션성 텍스트를 막는 넉넉한 캡이라 정상값은 걸리지 않는다. 초과 시 말줄임표 부착.
    """
    collapsed = _strip_unsafe(text)
    if max_len <= 0:  # 오설정 방어 — 0 이하 상한은 음수 슬라이스로 뒤집히지 않게 차단
        return ""
    if len(collapsed) > max_len:
        collapsed = collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def _unrated_product_ids(
    candidates: list[SpringProduct], filters: ProductSearchFilters
) -> set[int]:
    """평점을 **명시한** 턴에서 고지가 필요한 무평점 상품 id (#132).

    판정은 rerank `_rating_tier` 와 **같은 규약**이다(#171 / #100 P0) — `rating is None`(미집계)과
    `review_count == 0`(리뷰가 없어 나온 rating=0 은 저평점이 반증된 게 아니라 데이터 부재) 둘 다
    무평점이다. 두 곳이 갈리면 LLM 에는 '평가없음'이라 해놓고 코드는 고지를 안 붙이는 식으로
    조용히 어긋난다.

    사용자가 평점을 말하지 않은 턴은 대상이 아니다 — 그 경로의 근거문은 한 글자도 바뀌지 않는다.
    """
    if filters.rating_min is None:
        return set()
    return {p.product_id for p in candidates if p.rating is None or p.review_count == 0}


def _apply_unrated_disclosure(reason: str, notice: str, max_len: int) -> str:
    """근거문에 무평점 고지를 덧붙인다 — 상한을 넘으면 **근거를 자르고 고지를 남긴다** (#132).

    `_sanitize_reason` 에 통째로 맡기면 뒤쪽인 고지가 먼저 잘려 나간다. 고지는 이 함수가 존재하는
    이유이므로 우선순위가 근거보다 높다 — 근거는 잘려도 뜻이 남지만 고지는 잘리면 없는 것과 같다.
    근거가 비어도 고지는 실린다: rationale 없는 검색순서 보충 카드가 `_reasons()` 에서 통째로
    빠지면 그 카드만 무고지로 나가는데, 하필 그런 카드일수록 사용자가 근거 없이 신뢰한다.

    고지 자체가 상한보다 길면 상한이 이긴다 — `reason_max_len` 은 신뢰경계(→Spring→CH-5→FE)를
    넘는 자유 텍스트의 방어캡이라 UX 문구가 뚫을 수 있는 값이 아니다.
    """
    cleaned_notice = _sanitize_reason(notice, max_len)
    if not cleaned_notice:  # 운영자가 고지를 껐다 — 기존 동작 그대로
        return _sanitize_reason(reason, max_len)
    suffix = f" ({cleaned_notice})"
    room = max_len - len(suffix)
    body = _sanitize_reason(reason, room) if room > 0 else ""
    return f"{body}{suffix}".strip() if body else cleaned_notice


def _merge_fanout_results(
    results: list[tuple[int, ProductSearchResult]], cap: int
) -> tuple[ProductSearchResult, dict[int, int]]:
    """fan-out leg 결과를 round-robin 인터리브 + productId dedup + cap 절단으로 병합한다(§6).

    leg 순서대로 한 상품씩 번갈아 뽑아(한 카테고리가 rerank 입력을 독점하지 않게) 최초 등장
    productId 만 남기고, cap 으로 절단해 rerank 입력 상한을 지킨다. 빈 leg 는 건너뛴다.

    입력은 `(원본 leg 인덱스, 결과)` 쌍이다 — 실패한 leg 이 빠져도 남은 leg 의 인덱스가 밀리지
    않아야 상류의 `category_legs[i]`(니즈 이름)와 계속 대응한다.
    두 번째 반환값은 `productId → 원본 leg 인덱스` 로, **니즈별 목록 분할의 유일한 근거**다
    (#209/REQ-REC-024). 여기서 leg 를 버리면 하류에서 복원할 방법이 없어 니즈가 한 묶음으로
    뭉개진다. leg 는 **round-robin 최초 등장** 기준이며(leg 단위 순회가 아니라) 실제로 채택된
    등장 위치와 같아야 상품이 엉뚱한 니즈 목록에 들어가지 않는다.
    """
    lists = [(leg, r.products) for leg, r in results]
    depth = max((len(pl) for _, pl in lists), default=0)
    seen: set[int] = set()
    merged: list[SpringProduct] = []
    leg_of: dict[int, int] = {}
    for i in range(depth):
        for leg, pl in lists:
            if i >= len(pl):
                continue
            product = pl[i]
            if product.product_id in seen:
                continue
            seen.add(product.product_id)
            leg_of[product.product_id] = leg
            merged.append(product)
    # slice 절단 — decompose 의 _parse_category_queries·dedup_truncate 와 동일 규약
    # (cap<=0 이면 정확히 0개; append 후 체크는 첫 상품이 남아 절단 의미가 어긋난다, PR #73 리뷰).
    merged = merged[:cap]
    # 절단으로 탈락한 상품의 leg 정체성은 남기지 않는다 — 하류가 없는 상품을 니즈에 배정하지 않게.
    kept_ids = {p.product_id for p in merged}
    leg_of = {pid: leg for pid, leg in leg_of.items() if pid in kept_ids}
    return ProductSearchResult(products=merged, total_count=len(merged)), leg_of


def _need_label(leg: tuple[str, str | None]) -> str | None:
    """니즈 목록의 표시 이름 — leg 검색어("파우치")를 쓰고 없으면 canonical 카테고리로 폴백한다.

    LLM 산출 자유 텍스트라 push(신뢰경계) 직전 정제 + 계약 상한(§4.2 `label` ≤50자)으로 자른다.
    폴백 판정은 **정제 이후**에 한다 — query 가 zero-width·제어문자로만 이뤄지면 truthy 인데도
    정제 결과가 비어, 정제 전에 고르면 canonical 을 못 보고 라벨이 조용히 사라진다(PR #212 리뷰).
    """
    canonical, query = leg
    label = _strip_unsafe((query or "").strip())
    if not label:
        label = _strip_unsafe((canonical or "").strip())
    return label[:LIST_LABEL_MAX_LEN] or None


def _need_names(
    need_legs: list[tuple[str, str | None]],
    *,
    leg_of: dict[int, int],
    product_ids: list[int],
) -> dict[int, str]:
    """rerank 에 넘길 `productId → 니즈 경계 이름` — 이름이 없으면 순번으로 채운다.

    push `label`(사용자 노출)과 **일부러 다르다**. rerank 에 필요한 건 사람이 읽는 이름이 아니라
    **서로 구분되는 토큰**이고, 이름이 하나도 없다고 경계 자체를 빼면(빈 dict → rerank 가 falsy 로
    무시) 단일 목록 경로와 똑같이 전역 정렬해 버린다 — 그런데 하류는 여전히 목록을 쪼개므로
    굶은 니즈가 rationale 없는 검색순서 보충으로 채워진다(PR #212 리뷰).
    반대로 이 순번을 `label` 로 쓰면 "니즈 2" 같은 무의미한 이름이 사용자에게 노출되므로
    `label` 은 종전대로 `_need_label` 이 만든 진짜 이름만 쓰고, 없으면 None 이다.

    이름이 **겹치는 경우에도** 순번을 붙인다(PR #212 리뷰) — 서로 다른 두 니즈가 같은 canonical
    로 매핑되면 같은 라벨이 나오는데, rerank 는 `dict.fromkeys(values)` 로 NEEDS 를 만들어
    **두 leg 를 하나로 뭉갠다**. 그러면 "니즈마다 상위 N개" 지시가 둘을 구분하지 못해 쏠림이
    그대로 재발한다. 겹치지 않으면 이름을 그대로 둬 프롬프트를 읽기 쉽게 유지한다.
    """
    legs_present: list[int] = []
    for pid in product_ids:
        leg = leg_of.get(pid)
        if leg is not None and leg not in legs_present:
            legs_present.append(leg)

    label_by_leg = {leg: _need_label(need_legs[leg]) or "" for leg in legs_present}
    seen = Counter(label_by_leg.values())
    for leg, label in label_by_leg.items():
        if not label:
            label_by_leg[leg] = f"니즈 {leg + 1}"
        elif seen[label] > 1:
            label_by_leg[leg] = f"{label} (니즈 {leg + 1})"

    return {pid: label_by_leg[leg] for pid in product_ids if (leg := leg_of.get(pid)) is not None}


def _split_by_need(
    ranked_ids: list[int],
    candidates: list[SpringProduct],
    *,
    leg_of: dict[int, int],
    leg_count: int,
    expose_min: int,
    expose_max: int,
) -> list[tuple[int, list[int]]]:
    """노출 상품을 니즈(leg)별 그룹으로 나누고 그룹마다 보정·상한을 적용한다(REQ-REC-021/024).

    `(leg 인덱스, productId 목록)` 을 leg 순서대로 돌려준다. `leg_of` 가 비면 분할하지 않는
    경로라 leg 0 그룹 하나로 접어 종전(전역 보정·절단)과 같은 결과가 된다.

    보정·상한이 **목록 하나 기준**인 이유: 전역으로 자르면 상위를 독식한 니즈 하나만 채워지고
    나머지 니즈 목록이 비어 "유럽여행 준비물"에서 어댑터가 통째로 사라진다. 반대로 보정을
    전역으로 두면 목록마다 후보가 1~2개인 `PICK_ONE`(고를 게 없는 목록)이 나온다.
    후보가 부족한 니즈는 있는 만큼만 담고, 하나도 없으면 **그룹 자체를 만들지 않는다** —
    빈 목록은 400 이 아니라 "보내지 않는 것"이 맞다(§4.2).
    """
    # leg 를 모르는 상품은 leg 0 으로 접되 **조용히 넘기지 않는다**(PR #212 리뷰) — 어느 니즈에도
    # 속하지 않는 상품이 남의 목록에 섞이는 것이라, 아래 reco_lists_truncated 와 같은 기준으로
    # 진단을 남긴다. 현재 candidates 는 leg_of 의 부분집합이라 도달하지 않는 경계지만, 검색
    # 백엔드가 leg 없는 상품을 후보에 섞으면 깨진다.
    orphans: set[int] = set()

    def leg_for(pid: int) -> int:
        if not leg_of:
            return 0
        leg = leg_of.get(pid)
        if leg is None:
            orphans.add(pid)
            return 0
        return leg

    ranked_by_leg: dict[int, list[int]] = {}
    for pid in ranked_ids:
        ranked_by_leg.setdefault(leg_for(pid), []).append(pid)
    # 보정 재고 — 검색순서(하드 제약이 이미 반영된 안전한 순서)를 니즈별로 미리 나눠 둔다.
    fallback_by_leg: dict[int, list[int]] = {}
    for product in candidates:
        fallback_by_leg.setdefault(leg_for(product.product_id), []).append(product.product_id)

    groups: list[tuple[int, list[int]]] = []
    for leg in range(leg_count) if leg_of else (0,):
        group = list(ranked_by_leg.get(leg, ()))
        if len(group) < expose_min:
            have = set(group)
            for pid in fallback_by_leg.get(leg, ()):
                if pid in have:
                    continue
                group.append(pid)
                have.add(pid)
                if len(group) >= expose_min:
                    break
        group = group[:expose_max]
        if group:  # 후보 0건 니즈는 목록을 만들지 않는다(§4.2)
            groups.append((leg, group))
    if orphans:
        logger.warning(
            "reco_products_without_leg",
            extra={"count": len(orphans), "fallback_leg": 0},
        )
    return groups


def _resolve_repurchase_ids(recent, references: list[str]) -> set[int]:
    """명시 재구매 지목(상품명 텍스트) → 그 회원의 최근 구매 productId 집합 (#120).

    **해소 대상은 `recent`(I-19, JWT sub 유래 본인 구매 이력)뿐**이다 — 결과는 구성상 항상
    exact 제외 대상의 부분집합이라, LLM 이 무엇을 지목하든 임의 productId·타인 상품으로
    확장될 수 없다(신뢰 경계). 후보(candidates) id 나 LLM 정수는 여기 들어오지 않는다.

    공백 제거 + casefold 후 중복을 제거한 유효 **지목이 정확히 1건일 때만** 해소한다. 복수 지목은
    모델이 LAST_RECOMMENDATIONS 같은 맥락 목록을 에코했을 수 있어, 각 이름이 개별적으로 정확해도
    전부 미해제한다. 단일 지목은 가장 좁은 후보 집합을 고른다 — 완전 일치가 있으면 그것만, 없을
    때만 `지목 in 구매명` 단방향 부분비교로 넓힌다. 선택된 후보에서 **구분되는 정규화 상품명이
    정확히 1개일 때만** 그 이름의 productId를 전부 해제한다. 따라서 재등록·옵션 분리로 같은 이름이
    여러 productId에 걸려도 모두 해제하지만, 서로 다른 상품명에 걸린 모호한 부분일치는 미해제한다.
    단방향 폴백은 긴 지목("무선 이어폰 케이스")을 짧은 구매명("이어폰")으로 축약하는 오매칭을
    막는다. 아무것도 못 잡으면 조용히 빈 집합(= 종전 제외 유지)으로 degrade 한다.
    """
    if not references:
        return set()
    norms = {n for r in references if (n := "".join(r.split()).casefold())}
    if len(norms) != 1:
        return set()
    names = [
        (item.product_id, "".join((item.product_name or "").split()).casefold()) for item in recent
    ]
    reference = next(iter(norms))
    exact = [(pid, name) for pid, name in names if name and name == reference]
    matches = exact or [(pid, name) for pid, name in names if name and reference in name]
    if len({name for _, name in matches}) != 1:
        return set()
    return {pid for pid, _ in matches}


async def _load_persisted_repurchase(thread_key, turn_ids, settings) -> set[int]:  # noqa: ANN001
    """이번 턴 지목을 누적하고 저장된 재구매 면제 id를 반환한다(#232)."""
    if thread_key is None:
        return set()
    try:
        store = await get_repurchase_store()
        cap = settings.dedup_repurchase_store_max
        if turn_ids:
            persisted = await store.add(
                thread_key,
                turn_ids,
                cap=cap,
            )
        else:
            # 지목 없는 턴은 락·쓰기 없이 기존 지속값만 한 번 읽는다.
            persisted = await store.get(thread_key)
        return set(persisted[-cap:] if cap > 0 else [])
    except Exception as exc:  # noqa: BLE001 - 상태 실패는 SSE를 끊지 않고 이번 턴 신호로 degrade
        # [#113] `may_auto_relax` 턴은 conditions를 검색·완화 뒤로 미루므로 이 pg 왕복이
        # **첫 이벤트보다 앞설 수 있다**. 여기서 예외를 올리면 `_merge_fanout_results`·
        # `_post_filter` 실패와 같은 모양으로 conditions조차 못 낸 채 스트림이 죽는다.
        # 따라서 예외를 삼켜 이번 턴 지목만 쓰는 쪽으로 degrade한다. 미뤄진 턴은 이 왕복 1회가
        # 첫 프레임 예산에 들어가지만 `run_with_query_timeout`이 지연 상한을 건다.
        logger.warning("repurchase_store_failed", extra={"reason": str(exc)})
        return set()


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
    reverted_categories=frozenset(),
    cart_store=None,
    relax_store=None,
    thread_key: str | None = None,
    observer=None,
    request_id: str,
    # [#162] 조건이 하나도 없는 발화인가(`no_condition.is_no_condition_turn` 판정 결과).
    # 판정은 호출부(buyer/graph.py)에서 한다 — 그쪽에만 `prior`(첫 턴 여부)가 있다.
    no_condition: bool = False,
    popular_fn=None,  # I-3 조회. 미지정 시 라이브 기본값(테스트는 fake 주입)
    # [#162] 미리 만들어 둔 취향 벡터(`read_profile_summary()["embedding"]`). 회원이면서 벡터가
    # 있을 때만 취향 랭킹 경로를 탄다 — 게스트·신규회원·구 요약(벡터 없음)은 인기 상품으로 간다.
    profile_vec: list[float] | None = None,
) -> AsyncIterator[str]:
    """추천 서브그래프 스트림. 프레임(SSE str)을 순서대로 산출한다."""
    popular_fn = popular_fn or spring_client.get_popular_products
    # [#51] keyword 드롭 판단은 **한 곳에서** 계산해 칩 표시(아래)와 leg 검색(_leg)이 같은 flag 를
    # 공유하게 한다 — 두 지점이 독립 판단하면 전제(leg 엔 항상 canonical)가 미래 리팩터에서 깨질 때
    # 표시-실제가 어긋날 수 있다(리뷰 반영). canonical category(= category_legs 존재) + config on 이면
    # keyword(상품명 LIKE, retrieval AND-필터)를 드롭해 동의어("청바지" vs 상품명 "데님 팬츠")가
    # retrieval 후보를 원천 배제하지 못하게 한다.
    # [#51 리뷰] 단, keyword 드롭은 **embedding_rerank 백엔드에서만** 안전하다 — 그 경우에만 category
    # 로 확보한 후보를 semanticQuery 임베딩이 재정렬해 keyword 부재를 메운다. spring(재정렬 없음)은
    # keyword 가 유일한 텍스트 신호이고, vector(VectorSearchBackend)는 filters.keyword 를 쿼리 임베딩
    # 입력으로 써서(search_service.py:164) 드롭 시 빈 문자열을 임베딩한다 → 두 경우 모두 드롭하면
    # 품질이 급락한다. 그래서 backend 가 embedding_rerank 가 아니면 플래그와 무관하게 keyword 를 유지한다.
    drop_keyword = (
        settings.search_drop_keyword_with_category
        and settings.search_backend == "embedding_rerank"
        and bool(decision.category_legs)
    )

    # conditions 칩 (병합 필터에서 결정론적 파생) — fan-out 이면 canonical 전체를 표시한다(§3.1)
    # keyword 를 드롭하면 조건 칩에서도 keyword 를 빼 "적용되지 않는 필터를 제거 가능 조건으로 광고"
    # 하는 표시-실제 불일치를 막는다. keyword 값은 decision.filters 에 그대로 남겨 멀티턴 기억
    # (PRIOR_FILTERS)으로만 쓰고, 칩 파생용 사본에서만 제거한다(칩 제거 왕복 X 는 별개 관심사).
    def _condition_chips(filters: ProductSearchFilters):
        """확정 필터에서 conditions 칩을 만든다(자동 완화 후 재파생할 수 있게 함수로 뽑음)."""
        source = filters.model_copy(update={"keyword": None}) if drop_keyword else filters
        return build_condition_chips(source, categories=[c for c, _ in decision.category_legs])

    # [#113 PR #248 리뷰 A] 자동 완화가 **일어날 수 있는 턴이면** conditions 를 검색 뒤로 미룬다.
    # 조건 칩은 원래 검색 **전에** 내보내 화면이 빨리 뜨게 하는데, 자동 완화는 검색 **후에**
    # 조건을 바꾼다 — 먼저 내보내면 "평점 4.5 이상" 칩이 떠 있는데 실제 상품은 4.0 기준인
    # 표시-실제 불일치가 남는다(산문 token 은 고지해도 구조화된 칩은 거짓말을 계속한다).
    # §3.1 이 conditions 를 **0~1회**로 못박아 "고쳐서 재전송"이 불가하므로 순서로 푼다.
    #
    # **모든 턴을 미루지 않는다** — 자동 완화 대상(config 허용 목록, 기본 `ratingMin`)으로 만들
    # 수 있는 후보가 이번 턴 필터에 실제로 있어야 한다. 해당 없는 턴은 종전대로 검색 전에 칩을
    # 내보내 첫 프레임 지연이 없다.
    #
    # **다만 이 판정은 "완화가 일어날지"가 아니라 "일어날 수 있는지"다**(PR #248 리뷰) — 자동
    # 완화는 검색 0건일 때만 도는데 그건 검색 전에 알 수 없다. 그래서 평점 조건이 걸린 턴은
    # 결과가 넉넉해 완화가 안 일어나도 **매번** 조건 칩이 검색만큼 늦는다. 검색 이전에 나가는
    # 이벤트는 이 conditions 하나뿐이라(첫 token 도 검색 뒤다) 그 턴의 첫 프레임이 통째로 밀린다.
    # 그럼에도 미루는 쪽을 택한 이유는 대안이 더 나쁘기 때문이다: 먼저 내보내고 고치는 것은
    # §3.1 conditions **0~1회**가 막고, 먼저 내보내고 두는 것은 "평점 4.5 이상" 칩 아래 4.0
    # 상품이 깔리는 거짓말이 된다. 지연은 회복되지만 거짓 표시는 회복되지 않는다.
    # 이 비용이 아까운 배포는 `relaxation_max_rounds=0` 으로 자동 완화와 함께 지연도 끈다.
    #
    # 판정은 **자동 완화 자신의 조건**만 본다 — 칩 예산(`relaxation_max_probes`)과 엮으면,
    # 칩을 끈 설정(`=0`)에서 자동 완화는 도는데 조건 칩만 미리 나가 표시-실제 불일치가
    # 되살아난다(PR #248 리뷰 A 로 고친 바로 그 문제).
    # 판정에 후보 생성기를 그대로 쓴다 — 아래 루프가 도는 조건과 **같은 식**이라 어긋날 수 없다.
    # 기동 검증이 허용 목록을 `{ratingMin}` 으로 잠가 둔 지금은 "필드가 설정돼 있나"를 손으로
    # 판정하는 것과 결과가 같지만, 그건 그 잠금에 기댄 우연이다: 목록이 넓어지면 "설정은 됐는데
    # 후보는 안 나오는" 경우(올림 단위 때문에 못 넓히는 priceMax 등)가 생겨 헛되이 미루게 된다.
    # 완화 가능 여부의 정의를 두 곳에 두지 않는다. 순수 함수라 검색 왕복도 늘지 않는다.
    try:
        may_auto_relax = settings.relaxation_max_rounds > 0 and any(
            candidate.field in settings.relaxation_auto_fields
            for candidate in build_relaxation_candidates(decision.filters, settings)
        )
    except Exception as exc:  # noqa: BLE001 - 판정 실패가 conditions 를 통째로 막지 않게
        # 이 호출은 conditions 발신 **이전** 경로라, 터지면 이벤트가 하나도 안 나간 채 스트림이
        # 죽는다(`_merge_fanout_results` 와 같은 모양의 실패 — PR #248 리뷰).
        # 미루지 않는 쪽으로 떨군다: 같은 이유로 아래 완화 블록도 실패해 완화가 일어나지 않으므로,
        # 조건 칩을 먼저 내보내도 표시-실제 불일치가 생기지 않는다.
        logger.warning("relaxation_gate_failed", extra={"reason": str(exc)})
        may_auto_relax = False
    suppress_deferred_search_retry = (
        may_auto_relax and not settings.search_retry_on_deferred_conditions
    )
    if not may_auto_relax:
        yield sse(
            "conditions",
            ConditionsData(chips=_condition_chips(decision.filters)).model_dump(by_alias=True),
        )

    # dedup 소스(I-19)와 검색(§4.6)을 **병렬 실행** — §4.7 지연 가드(순차 시 최악 6s, first-token 예산 잠식).
    # dedup 은 검색 응답 뒤 사후필터라 두 호출은 독립적이다. 각 호출이 자체 실패를 삼켜 gather 는 안 깨진다.
    async def _run_search(
        base_filters: ProductSearchFilters | None = None,
    ) -> tuple[ProductSearchResult, dict[int, int]] | None:
        """검색 결과와 `productId → leg 인덱스` 맵을 함께 돌려준다.

        맵이 비어 있으면 leg 개념이 없는 경로(단일 filters 검색)라 하류가 목록 1건으로 간다.
        [#113] base_filters 로 **완화된 필터**를 넣어 같은 fan-out 의미 그대로 재검색할 수 있다 —
        완화 probe 가 leg 구성을 따로 재현하면 본 검색과 조건이 어긋나 estCount 가 거짓이 된다.
        """
        base = decision.filters if base_filters is None else base_filters
        legs = decision.category_legs
        if not legs:
            # 카테고리 매핑 결과 없음(매핑 degrade·비-매핑 경로) → 단일 filters 검색(기존 경로).
            try:
                found = await search(base, exclude_product_ids=None)
                return (found, {}) if found is not None else None
            except SpringUnavailableError:
                return None
            except Exception as exc:  # noqa: BLE001 - 예상외 예외도 삼켜 SEARCH_FAILED 로 degrade
                # 검색 호출이 SpringUnavailable 아닌 예외를 던져도 SSE 스트림을 미처리 예외로 죽이지
                # 않는다 — None → 상위에서 SEARCH_FAILED(§6). CancelledError(BaseException)는 전파.
                logger.warning("search_failed", extra={"reason": str(exc)})
                return None

        # fan-out — canonical 카테고리마다 leg 를 병렬 검색(§6). leg 별 filters 는 category·
        # keyword([#51] 위 drop_keyword flag 로 결정 — 드롭 아니면 그 카테고리 query/base)·semantic_query·
        # limit(leg 별 AI top-K, §4.6 size 아님) 만 교체한다.
        # leg 사전 절단 상한은 생존 leg 수에 의존하지 않는다(#89). leg 수(요청 시점)로 정하면
        # 일부 leg 가 SpringUnavailableError 로 죽어도 재조정되지 않아, 생존 leg 의 후보 폭이
        # 단일 카테고리 턴보다 좁아진다. 한 leg 가 병합 결과에 실을 수 있는 항목 수는
        # `merged[:merge_cap]` 때문에 merge_cap 을 넘지 못한다 — 초과분이 쓰일 여지는 그 leg
        # 앞쪽이 전부 다른 leg 와 중복되는 dedup 경계뿐이라 극히 좁아, merge_cap 이 사전 절단
        # 상한으로 실질 tight bound 다. 더 작게 잡으면 부분 실패 턴에서만 손해다. 균형(한
        # 카테고리 독점 방지)은 사전 절단이 아니라 `_merge_fanout_results` 의 round-robin 이
        # 담당한다. 재조회(2차 왕복) 없이 해결되는 이유: `filters.limit` 은 Spring 요청
        # 파라미터가 아니라 AI 쪽 절단 knob 이라(§4.6 size 제거, 2026-07-23) 값을 키워도
        # 왕복·페이로드는 그대로다.
        leg_limit = settings.category_fanout_merge_cap

        async def _leg(canonical: str, query: str | None) -> ProductSearchResult | None:
            # leg 전체를 try 로 감싼다 — model_copy·search 어디서 실패해도 그 leg 만 드롭한다.
            try:
                # [#101 PR#166] 멀티 카테고리면 semantic_query 도 leg 검색어(query)로 override 한다 —
                # 안 하면 전 leg 가 동일 전역 벡터로 pgvector 재정렬돼 leg 관련성이 깨진다("유럽여행
                # 준비물"로 여행용품·전자기기·의류를 똑같이 정렬). query=null 인 leg 는 canonical
                # ("가전 > 이어폰/헤드폰" 같은 분류 경로 breadcrumb)이 아니라 전역 semantic_query(broad
                # 해도 자연어)로 폴백한다 — breadcrumb 는 임베딩 앵커로 부적합(decompose cat_signal 과
                # 동일 원칙). 단일 카테고리(leg 1개)는 전역값(LLM 의 가장 풍부한 전체 의도)을 유지한다.
                leg_semantic = (
                    (query or base.semantic_query) if len(legs) > 1 else base.semantic_query
                )
                # [#51] keyword 는 위에서 계산한 drop_keyword flag 를 공유한다 — 칩 파생과 동일 판단이라
                # 표시-실제가 어긋나지 않는다. 드롭 시 leg 검색어는 leg_semantic 으로 rerank 를 담당하고
                # category 가 후보를 확보하므로 keyword 중복 투입은 불필요(config off 면 leg query→keyword
                # 로 복원, 롤백 안전성). _leg 는 legs 비어있지 않을 때만 호출돼 canonical 은 항상 truthy.
                leg_keyword = None if drop_keyword else (query or base.keyword)
                leg_filters = base.model_copy(
                    update={
                        "category": canonical,
                        "keyword": leg_keyword,
                        "semantic_query": leg_semantic,
                        "limit": leg_limit,
                    }
                )
                return await search(leg_filters, exclude_product_ids=None)
            except SpringUnavailableError:
                return None  # leg 별 실패는 삼켜 다른 leg 는 계속(§6)
            except Exception as exc:  # noqa: BLE001 - 예상외 예외도 그 leg 만 격리(SSE 스트림 보호)
                # SpringUnavailable 아닌 예외가 gather → 스트림 상위로 전파돼 SSE 전체가 죽지 않게
                # 격리한다. return_exceptions 대신 여기서 잡아 로그 + None — CancelledError(BaseException)
                # 는 전파돼 협조적 취소가 보존된다. category_mapping fan-out(§6)과 격리 목적 일관.
                logger.warning("search_leg_failed", extra={"reason": str(exc)})
                return None

        leg_results = await asyncio.gather(*(_leg(c, q) for c, q in legs))
        # 원본 leg 인덱스를 함께 들고 간다 — 실패한 leg 이 빠져 인덱스가 밀리면 하류 니즈 라벨이
        # 한 칸씩 어긋난다(category_legs[i] 와의 대응이 깨진다).
        survived = [(i, r) for i, r in enumerate(leg_results) if r is not None]
        if not survived:  # 전량 leg 실패 → SEARCH_FAILED(§6)
            return None
        if len(survived) < len(leg_results):
            if trace := current_request_trace():
                trace.mark_degraded("fanout_partial")
        try:
            return _merge_fanout_results(survived, settings.category_fanout_merge_cap)
        except Exception as exc:  # noqa: BLE001 - leg 격리와 같은 원칙으로 병합도 감싼다
            # [PR #248 리뷰] `_leg` 는 leg 별 실패를 잡는데 그 결과를 합치는 이 호출만 밖에 있었다.
            # 여기서 터지면 `search_bundle is None` 분기(→ SEARCH_FAILED)에 **도달하지 못해**
            # 미뤄 둔 conditions 가 통째로 사라진다 — conditions 를 검색 뒤로 미루면서 생긴 새
            # 실패 경로다(미루기 전에는 이미 나간 뒤였다). None 으로 떨궈 그 분기를 타게 한다.
            # CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
            logger.warning("search_merge_failed", extra={"reason": str(exc)})
            return None

    # I-19 조회가 **실패**했는지 — "이력이 없다"(게스트·비회원)와 구분한다. 둘 다 None 을
    # 돌려주므로 호출부에서는 갈라낼 수 없는데, 없는 기능을 "고장났다"고 고지하면 거짓말이 된다.
    dedup_degraded = False

    async def _fetch_purchases():
        # 게스트/비회원/판매자/비숫자 sub 는 스킵, I-19 실패는 degrade(dedup 없이 진행, §4.7).
        # [IDOR] role==SELLER 는 user_id=sub·seller_id=sub — 판매자 sub 를 memberId 로 쓰면 안 됨.
        nonlocal dedup_degraded
        if identity is None or identity.is_guest or not identity.user_id or identity.seller_id:
            return None
        try:
            uid = int(identity.user_id)
        except (ValueError, TypeError):
            return None
        fn = get_purchases_fn or spring_client.get_recent_purchases
        try:
            return await fn(uid)
        except SpringUnavailableError:
            dedup_degraded = True
            if trace := current_request_trace():
                trace.mark_degraded("dedup_skipped")
            return None
        except Exception as exc:  # noqa: BLE001 - I-19 실패는 degrade(dedup 없이 진행, SSE 유지)
            # 최근구매 조회가 예상외 예외를 던져도 추천 스트림을 죽이지 않는다 — None → dedup 스킵(§4.7).
            logger.warning("purchases_fetch_failed", extra={"reason": str(exc)})
            dedup_degraded = True
            if trace := current_request_trace():
                trace.mark_degraded("dedup_skipped")
            return None

    # [#162] 취향 랭킹이 먼저 I-19 를 부르고 실패해 아래 인기 상품 경로로 폴백하면, 같은 턴에
    # I-19 가 **두 번** 나간다(각 3s). 한 번만 부르고 결과를 재사용한다 — 리스트를 쓰는 이유는
    # 중첩 함수에서 `nonlocal` 없이 "아직 안 불렀음"과 "불렀는데 None"(게스트·조회 실패)을
    # 구분하기 위해서다. 그 둘을 뭉개면 게스트 턴마다 헛호출이 반복된다.
    _purchases_memo: list = []

    async def _fetch_purchases_once():
        if not _purchases_memo:
            _purchases_memo.append(await _fetch_purchases())
        return _purchases_memo[0]

    # 조건 없는 턴 + 프로필 벡터 → **취향 벡터 랭킹**(홈 I-22 와 같은 엔진·같은 인덱스).
    # 이 경로는 검색도 rerank 도 타지 않아 아래 파이프라인과 갈라지므로 여기서 끝내고 return 한다.
    # `conditions` 는 위에서 이미 나갔다(조건 없는 턴은 `may_auto_relax` 가 False 라 미루지 않는다).
    # [#311 리뷰] 예산·전부구매 의도가 있으면 취향 경로를 타지 않는다 — AI 인덱스에 가격이 없어
    # 예산을 확인할 수 없다. 그 턴은 아래 인기 상품 경로에서 예산으로 걸러진다.
    if no_condition and profile_vec and not has_total_budget(decision):
        profile_purchases = await _fetch_purchases_once()
        profile_exclude: set[int] = set()
        if profile_purchases is not None:
            profile_recent = profile_purchases.recent_items(
                since=_now() - timedelta(days=settings.dedup_recent_days),
                exclude_statuses=_INACTIVE_STATUSES,
            )
            profile_exclude = {item.product_id for item in profile_recent}
        profile_ranked = await rank_by_profile(
            profile_vec, exclude=profile_exclude, settings=settings
        )
        if profile_ranked is not None:
            ranked_by_profile, profile_reason_by_id = profile_ranked
            exposed = ranked_by_profile[: settings.expose_max]
            profile_entry = RecommendationListEntry(
                list_id=uuid4().hex,
                product_ids=exposed,
                # 근거는 선택 필드다(§4.2) — `build_reasons` 가 못 고른 상품은 항목을 만들지 않는다.
                reasons=[
                    RecoReason(product_id=pid, reason=text)
                    for pid in exposed
                    if (text := profile_reason_by_id.get(pid))
                ],
            )
            profile_push = RecommendationPush(
                session_id=request.session_id,
                recommendation_request_id=str(uuid4()),  # 정규 UUID 36자 — BE CHAR(36)
                list_type="PICK_ONE",
                lists=[profile_entry],
            )
            if notice := _strip_unsafe(settings.no_condition_notice_profile):
                yield sse("token", TokenData(text=notice).model_dump(by_alias=True))
            try:
                profile_pushed = bool(await push_fn(profile_push))
            except SpringUnavailableError:
                profile_pushed = False
            logger.info(
                "chat_recommendation_profile_ranked",
                extra={"exposed": len(exposed), "pushed": profile_pushed},
            )
            if profile_pushed:
                yield sse(
                    "products.ready",
                    ProductsReadyData(
                        session_id=request.session_id,
                        list_ids=[profile_entry.list_id],
                    ).model_dump(by_alias=True),
                )
                # 담기 해소용 보관 — 이 경로는 **상품명을 모른다**(AI 인덱스에 원본 컬럼 없음).
                # 빈 이름으로 넣어 id 기반 담기 가드(#118)는 살리고, 이름 지칭("그 이어폰")만
                # 이 턴에서 해소되지 않는다.
                if cart_store is not None and thread_key is not None:
                    await cart_store.set_last_reco(thread_key, [(pid, "") for pid in exposed])
            else:
                if trace := current_request_trace():
                    trace.mark_degraded("push_skipped")
                if push_notice := _strip_unsafe(settings.push_skipped_notice):
                    yield sse("token", TokenData(text=push_notice).model_dump(by_alias=True))
            yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
            return
        # 랭킹 실패·0건 → 아래 인기 상품(I-3) 경로로 폴백한다(스트림은 계속된다).
        if trace := current_request_trace():
            trace.mark_degraded("profile_ranking_fallback")

    # [#162] 조건이 하나도 없는 턴은 후보 소스를 I-3(인기 상품, §4.12)로 바꾼다.
    popular_degraded = False

    async def _run_candidate_source():
        """이번 턴의 후보를 확보한다 — `_run_search` 와 같은 `(결과, leg맵)` 형태로 돌려준다.

        조건 없는 턴에 종전 경로를 그대로 태우면 파라미터 0개의 I-1 이 나가 매칭 전량
        (실측 7,245건·13.33MB)을 받는데, 그 상위는 사용자 의도와 무관하다. I-1 정본이
        "정형조건 없는 요청 차단은 LLM 단 책임"으로 규정한 자리다(§4.6·§4.12).

        leg 맵은 빈 dict 다 — 인기 목록은 카테고리 fan-out 이 아니라 단일 목록이다.
        """
        nonlocal popular_degraded
        if not no_condition:
            return await _run_search()
        try:
            found = await popular_fn(settings.popular_candidate_size)
        except Exception as exc:  # noqa: BLE001 - 폴백 소스 실패로 스트림을 죽이지 않는다
            logger.warning("popular_products_failed", extra={"reason": str(exc)})
            found = None
        if found is not None:
            # [#311 리뷰] 총액 예산을 말한 턴은 **예산 안의 후보만** 남긴다. 세트로 묶지 않고
            # 대안으로 보여주므로 상품 하나가 예산 이하이면 된다 — 무엇을 몇 개 살지 사용자가
            # 말하지 않은 턴에 조합을 지어내면 근거 없는 세트가 된다(`has_total_budget` 참조).
            if decision.total_budget is not None:
                affordable = within_budget(found.products, decision.total_budget)
                found = ProductSearchResult(products=affordable, total_count=len(affordable))
            # **0건도 성공이다**(§4.12) — 빈 배열이면 하류 zero-result 경로가 카드 없이 답한다.
            # 여기서 degrade 로 처리하면 이 이슈가 없애려는 무필터 I-1 을 도로 부른다.
            return (found, {})
        popular_degraded = True
        if trace := current_request_trace():
            trace.mark_degraded("popular_fallback")
        return await _run_search()

    # 미룬 턴은 첫 이벤트 앞 본 검색·자동 완화 probe만 재시도를 끈다(#277). conditions 뒤의
    # 완화 칩 probe는 첫 이벤트 예산 밖이라 제외하며, 이 with는 await 뒤 즉시 닫아 yield·다음
    # 턴으로 ContextVar가 새지 않게 한다. progress 이벤트가 계약에 생기면 이 스킵은 원복 가능하다.
    with (
        spring_client.suppress_search_retry()
        if suppress_deferred_search_retry
        else nullcontext()
    ):
        search_bundle, purchases = await asyncio.gather(
            _run_candidate_source(), _fetch_purchases_once()
        )
    if search_bundle is None:  # 검색 실패 → SEARCH_FAILED(종료)
        if trace := current_request_trace():
            trace.mark_degraded("search_failed")
        if may_auto_relax:
            # [#113] 미뤄 둔 conditions 를 여기서 낸다 — 검색이 실패했으니 완화는 일어나지 않았고,
            # 그냥 return 하면 이 턴만 조건 칩이 통째로 사라진다(미루기 전에는 항상 나갔다).
            yield sse(
                "conditions",
                ConditionsData(chips=_condition_chips(decision.filters)).model_dump(by_alias=True),
            )
        yield sse(
            "error",
            ErrorData(
                code="SEARCH_FAILED",
                message="상품 검색에 실패했어요.",
                request_id=request_id,
                retryable=True,
            ).model_dump(by_alias=True),
        )
        return

    search_result, leg_of = search_bundle

    # 최근 구매(윈도우·취소반품 필터) → exact 제외 + 소모품 카테고리 억제(결정 14-F).
    exclude_ids: set[int] = set()
    # [#120] 명시 재구매 지목으로 되돌린 productId — exact 제외·소모품 억제를 함께 면제한다.
    repurchase_ids: set[int] = set()
    cat_samples: dict[str, str] = {}  # 억제 소모품 카테고리 -> 최근 구매 상품명(되돌리기 칩 라벨용)
    if purchases is not None:
        since = _now() - timedelta(days=settings.dedup_recent_days)
        recent = purchases.recent_items(since=since, exclude_statuses=_INACTIVE_STATUSES)
        exclude_ids = {i.product_id for i in recent}
        turn_ids = _resolve_repurchase_ids(recent, decision.repurchase_products)
        # 검색 실패 조기 return 뒤에서만 누적한다 — 검색 실패 턴의 지목을 저장하지 않는 #120의
        # 기존 해소 위치와 일관된다. 저장값은 매 턴 본인 최근 구매 집합과 교집합해 재검증한다.
        persisted = await _load_persisted_repurchase(thread_key, turn_ids, settings)
        repurchase_ids = (turn_ids | persisted) & exclude_ids
        consumables = set(settings.consumable_categories)
        for i in recent:
            # 소모품 카테고리인데 사용자가 되돌리지 않은 것만 억제 대상.
            if i.category and i.category in consumables and i.category not in reverted_categories:
                cat_samples.setdefault(i.category, i.product_name or i.category)

    # 사후필터: exact productId 제외 + 소모품 카테고리 억제(§4.7, C-15).
    def _post_filter(
        found: ProductSearchResult,
    ) -> tuple[ProductSearchResult, dict[str, int], int, bool]:
        """검색 결과에 dedup·소모품 억제를 적용해 (결과, 억제수, 수신수, 후보유무)를 낸다.

        [#113] 지역 함수로 뽑아 **완화 probe 도 같은 사후필터를 통과**하게 한다 — 칩이 약속한
        estCount 와 실제로 노출될 수를 같은 기준으로 세지 않으면 "12건"이라 해놓고 8건이 뜬다.
        """
        # [#101 #8] 관측성 — dedup 이전 수신 후보 수. **경로별 의미 주의(PR#166 리뷰)**: 비-fanout 은
        # Spring 매칭 전량(#101 로 search_catalog 절단 제거)이지만, fan-out 은 _run_search 안에서 이미
        # _merge_fanout_results(merge_cap) 로 절단된 **뒤** 값이라 leg 별 실제 수신 합계가 아니다 —
        # fan-out 의 가장 큰 recall 손실(leg 검색 → merge_cap 절단)은 이 수치 이전에 일어나 안 잡힌다.
        # leg-합 관측은 별도 과제(관측 이슈 #136/#137 라인, _run_search 가 pre-merge 합을 노출해야 함).
        seen = len(found.products)
        suppressed: dict[str, int] = {}
        survivors = []
        for product in found.products:
            # [#120] 명시 재구매 지목은 exact 제외와 소모품 카테고리 억제를 **둘 다** 면제한다 —
            # exact 만 풀면 소모품 재구매(소금·세제)가 카테고리 억제에 다시 걸려 되돌리기가 안 된다.
            # 카테고리 억제 카운트(suppressed)에도 넣지 않아 되돌리기 칩 추정치가 부풀지 않는다.
            if product.product_id in repurchase_ids:
                survivors.append(product)
                continue
            if product.product_id in exclude_ids:
                continue
            if product.category in cat_samples:
                suppressed[product.category] = suppressed.get(product.category, 0) + 1
                continue
            survivors.append(product)
        # [#101] 최종 rerank 입력 상한 절단 — search_catalog(사전) 가 아니라 최근구매 dedup·소모품
        # 억제 **이후** 여기서 embedding_rerank_limit 으로 압축한다. 사전 절단이면 dedup 대상이 상위에
        # 몰릴 때 rerank 후보가 상한 미만이 되는 recall 손실이 있어, 절단을 dedup 이후로 옮겼다
        # (비-fanout 전량·fan-out merge_cap 병합 결과 모두 이 지점에서 최종 절단).
        matched = len(survivors)
        # [#120 PR#230 리뷰] 명시 재구매 지목은 절단 **전에** 앞으로 당긴다 — 이 절단은 원본 검색
        # 순서 기준이라, 되살린 상품이 상한(기본 30) 밖이면 exact 제외를 면제해 놓고도 rerank 후보에
        # 조차 못 들어가 "지목하면 다시 추천된다"는 보장이 조용히 깨진다. 사용자가 직접 지목한 상품이
        # 검색 순서보다 우선하는 게 맞다. stable sort 라 지목 상품끼리·나머지끼리의 상대 순서는
        # 그대로고, 지목이 없으면(기본 경로) 정렬 자체를 건너뛰어 종전과 동일하다.
        if repurchase_ids:
            survivors.sort(key=lambda p: p.product_id not in repurchase_ids)
        return (
            ProductSearchResult(
                products=survivors[: settings.embedding_rerank_limit], total_count=matched
            ),
            suppressed,
            seen,
            bool(found.products),
        )

    try:
        result, suppressed_by_cat, received, had_candidates = _post_filter(search_result)
    except Exception as exc:
        # [PR #248 2차 리뷰] **conditions 발신 보장** — 미룬 턴에서 여기가 터지면 조건 칩이
        # 통째로 사라진다(미루기 전에는 검색 **전** 순수 계산이라 사실상 실패할 일이 없었다).
        # 예외는 삼키지 않고 다시 올린다 — 후보가 확정되지 않은 채 추천을 이어갈 수는 없다.
        #
        # **다시 올리기 전에 이름표를 남긴다**(PR #248 3차 리뷰) — 이 함수의 다른 실패 경로는 전부
        # `logger.warning(<이벤트명>, extra={"reason": …})` 로 단계를 특정하는데 여기만 빠져 있어,
        # 프로덕션에서 터지면 원인 태그 없는 raw traceback 만 남아 집계·분류가 안 된다. 예외가
        # 유실되는 문제는 아니지만(raise 로 상위에 그대로 간다) **어느 단계였는지**를 잃는다.
        logger.warning("search_post_filter_failed", extra={"reason": str(exc)})
        if may_auto_relax:
            yield sse(
                "conditions",
                ConditionsData(chips=_condition_chips(decision.filters)).model_dump(by_alias=True),
            )
        raise
    candidates = result.products

    # ── 0건/소량 조건 완화 (#113, api-spec §3.1 · SPEC-RECOMMEND-001 §6.6) ──
    # estCount 는 page-local 로 못 구한다 — priceMax·brand·color 는 Spring I-1 쿼리 파라미터라 탈락
    # 상품이 응답에 아예 없다(schemas/spring.py ProductSearchResult docstring). 그래서 완화 필터로
    # 재검색(probe)해 실제 매칭 수를 센다.
    #
    # **estCount 가 정확하다는 전제 = I-1 전량 반환**(api-spec §4.6, 2026-07-23 BE 합의로 size 제거).
    # I-1 응답에는 totalCount 필드가 없어서(C-15 🔴) 우리가 받은 배열 길이를 세는 것뿐인데, Spring 이
    # 매칭 전량을 주므로 그 길이가 곧 전체 매칭 수다. **BE 가 나중에 반환 상한을 다시 넣으면 이 값은
    # 조용히 상한값으로 고정된다** — 오류 없이 숫자만 작아지므로 여기 전제를 적어 둔다.
    # 단, fan-out(멀티 leg) 턴은 _merge_fanout_results 가 category_fanout_merge_cap 으로 절단한 **뒤**
    # 라 estCount 가 그 상한을 넘지 않는다(단일 카테고리 턴은 절단 없이 정확).
    #
    # estCount 의미 = **완화 적용 후 결과 총수**(§3.1 "완화 적용 시 예상 결과 수"). 소량 경로에서는
    # 이미 노출 중인 건수도 포함한다 — 되돌리기 칩의 estCount(억제분 delta)와 기준이 다르므로 주의.
    #
    # **알려진 한계 — fan-out 턴의 칩 클릭**: probe 는 이번 턴의 leg 전부를 fan-out 해 세지만,
    # 다음 턴 클릭은 카테고리 신호가 없어 리파인 승계 경로를 타 `prior.category`(대표 canonical)
    # **한 leg 로 좁혀진다**(buyer/graph.py `_prepare_recommendation`). 그래서 다중 카테고리 턴에서는
    # 실제 결과가 estCount 보다 적을 수 있다. 이는 "더 저렴한 걸로" 같은 **모든 리파인 턴에 이미
    # 있는 #59/#73 동작**이지 완화 칩이 만든 회귀가 아니다 — 승계 규약을 바꾸는 건 #84 소관이라
    # 여기서 건드리지 않고 한계로 남긴다.
    relaxation_notice: str | None = None
    relax_candidates: list[RelaxationCandidate] = []
    probed_counts: dict[str, int] = {}  # 와이어 필드명 -> 완화 시 매칭 수(probe 실패는 미기록)
    adopted_field: str | None = None
    adopted_value = None  # 채택된 완화 값 — 미뤄 둔 conditions 칩을 이 값으로 다시 파생한다
    # [PR #248 리뷰] 예산은 **칩 probe 전용**이다 — 자동 완화와 공유하면, 자동 완화가 먼저 돌아
    # 예산을 다 쓴 턴에서 칩이 통째로 굶는다(`relaxation_max_probes=1` + 평점 조건이면 항상).
    # 정작 칩은 **자동 완화가 실패했을 때 쓰라고 있는 폴백**이라, 그 폴백이 굶으면 사용자는
    # "조건을 바꿔볼까요?"라는 말만 듣고 누를 게 하나도 없는 화면을 받는다.
    # 자동 완화는 자기 상한(`relaxation_max_rounds`, SPEC REQ-REC-040)으로 따로 제한한다 —
    # 손잡이 하나가 하나씩만 맡아야 설정값이 이름대로 동작한다.
    probe_budget = settings.relaxation_max_probes  # 완화 칩 probe 상한(자동 완화와 무관)
    probes_spent = 0  # 관측용 — 이 턴이 실제로 쓴 추가 Spring 호출 수

    async def _probe(cand: RelaxationCandidate):
        """완화 필터로 재검색해 본 경로와 같은 사후필터를 통과시킨다. 실패면 None(그 후보만 탈락).

        **전 구간을 방어한다**(PR #248 리뷰) — `_run_search` 만 감싸면 뒤이은 `_post_filter` 예외가
        그대로 올라가고, 아래 `asyncio.gather` 는 `return_exceptions` 가 없어 **칩 하나 만들려던
        부가 조회가 추천 턴 전체(SSE 스트림)를 죽인다.** 이 함수의 다른 편의 기능(I-19 조회·칩 기억
        저장소)이 전부 따르는 "실패해도 턴을 죽이지 않는다"(§7)를 이 경로만 빠뜨릴 이유가 없다.
        """
        nonlocal probes_spent
        probes_spent += 1  # 실패분도 센다 — 지연·비용은 성공 여부와 무관하게 발생한다
        try:
            # _run_search 는 자체 예외를 삼켜 None 을 준다(§6 degrade). 그 **뒤**의 병합·사후필터가
            # 이 try 의 주 보호 대상이다.
            probed = await _run_search(cand.filters)
            if probed is None:
                return None
            relaxed, relaxed_leg_of = probed
            return (*_post_filter(relaxed), relaxed_leg_of)
        except Exception as exc:  # noqa: BLE001 - 완화 probe 실패가 스트림을 죽이지 않게(degrade)
            # CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
            logger.warning(
                "relaxation_probe_failed", extra={"field": cand.field, "reason": str(exc)}
            )
            return None

    if not candidates:
        # [PR #248 2차 리뷰] 루프 전체를 감싼다 — `_probe` 안쪽은 이미 방어하지만 후보 생성
        # (`build_relaxation_candidates`)과 루프 자체는 밖이었다. 여기서 터지면 아래 conditions
        # 발신까지 못 가 조건 칩이 사라진다. 자동 완화는 **선택 기능**이라 실패는 삼키고
        # 완화 없이 계속한다(§7) — 0건 안내와 완화 칩 경로는 그대로 살아 있다.
        try:
            relax_candidates = build_relaxation_candidates(decision.filters, settings)
            auto_fields = set(settings.relaxation_auto_fields)
            rounds = 0
            for cand in relax_candidates:
                # [REQ-REC-043 / AC-REC-08] 명시 제약(가격·브랜드)은 **자동으로 넘지 않는다** —
                # 사용자가 칩으로 동의하기 전까지 상한 초과 상품을 조용히 노출하면 안 된다.
                # 자동은 config 화이트리스트(기본 평점)에 든 약한 조건뿐이다.
                if cand.field not in auto_fields:
                    continue
                # 자동 완화는 **자기 상한**만 쓴다(칩 예산과 분리, 위 probe_budget 주석 참조).
                # 실질 상한은 허용 목록 크기다 — 기동 검증이 `{ratingMin}` 으로 잠가 뒀으므로
                # 후보가 1개뿐이라 이 루프는 현재 최대 1회 돈다. max_rounds 는 그 위의 안전망이다.
                if rounds >= settings.relaxation_max_rounds:
                    break
                rounds += 1
                # conditions 전 자동 완화 probe까지만 억제한다. 아래 완화 칩 probe는 감싸지 않는다.
                with (
                    spring_client.suppress_search_retry()
                    if suppress_deferred_search_retry
                    else nullcontext()
                ):
                    outcome = await _probe(cand)
                if outcome is None:
                    continue
                relaxed_result, relaxed_suppressed, relaxed_seen, relaxed_had, relaxed_leg = outcome
                probed_counts[cand.field] = relaxed_result.total_count
                if relaxed_result.products:  # 완화가 결과를 살렸다 → 정상 경로로 합류
                    result, suppressed_by_cat = relaxed_result, relaxed_suppressed
                    received, had_candidates = relaxed_seen, relaxed_had
                    leg_of, candidates = relaxed_leg, relaxed_result.products
                    relaxation_notice, adopted_field = cand.notice, cand.field
                    adopted_value = cand.value
                    break
        except Exception as exc:  # noqa: BLE001 - 자동 완화 실패가 턴을 죽이지 않게(degrade)
            # CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
            logger.warning("relaxation_auto_failed", extra={"reason": str(exc)})
    # [#113 PR #248 리뷰] **이 턴에 실제로 적용된 필터**. 자동 완화가 채택됐으면 그 값이 반영된
    # 사본이고 아니면 원본이다. 조건 칩 표시와 **완화 칩 후보 생성**이 같은 기준을 쓰게 하는 게
    # 핵심이다 — 원본으로 후보를 만들면 "평점 4.0" 이 화면에 떠 있는데 그 옆 가격 칩은 4.5 기준으로
    # probe·클릭돼, 표시와 실제가 어긋날 뿐 아니라 4.5 로 재보면 0건이라 **칩이 통째로 사라진다**.
    effective_filters = decision.filters
    if adopted_field and (attr := RELAXATION_FIELD_TO_ATTR.get(adopted_field)):
        effective_filters = decision.filters.model_copy(update={attr: adopted_value})
        relax_candidates = []  # 완화 전 기준으로 만든 후보는 버린다 — 아래에서 재생성

    # [PR #248 리뷰 A] 미뤄 둔 conditions 를 여기서 낸다. **완화 고지 token 보다 먼저** 내보낸다 —
    # §3.1 순서 계약이 conditions → token 이다.
    if may_auto_relax:
        yield sse(
            "conditions",
            ConditionsData(chips=_condition_chips(effective_filters)).model_dump(by_alias=True),
        )

    if relaxation_notice:
        # [REQ-REC-042] 조용히 조건을 바꾸지 않는다 — 고지는 **token 산문**이 전담한다.
        # done 에는 싣지 않는다: 정본(CH-2)이 done 을 finishReason 만으로 확정했고 FE 도
        # 그 필드를 읽지 않는다(api-spec §3.1 사본 drift 정정 v0.19.1).
        # 다른 모든 사용자 노출 텍스트(칩 label·dedup·push 안내·rerank comment)와 같이
        # `_strip_unsafe` 를 거친다(PR #248 3차 리뷰). 지금 notice 는 하드코딩 한국어 + 숫자
        # 포맷뿐이라 제어·zero-width·bidi 문자가 섞일 경로가 없지만, 이 자리만 방어가 빠져 있으면
        # 나중에 문구에 config·가변 텍스트를 섞는 순간 조용히 구멍으로 남는다.
        yield sse(
            "token", TokenData(text=_strip_unsafe(relaxation_notice)).model_dump(by_alias=True)
        )

    # 완화 칩 — 0건이거나 소량(config 임계 미만)일 때 남은 예산만큼 후보를 probe 한다.
    relaxation_chips: list[SuggestionChip] = []
    if not candidates or len(candidates) < settings.relaxation_min_results:
        # 자동 완화 루프와 같은 이유로 전체를 감싼다(PR #248 2차 리뷰) — 후보 생성·칩 조립은
        # `_probe` 바깥이라 방어가 없었다. 완화 칩은 **부가 제안**이라 실패하면 칩 없이 계속한다.
        try:
            if not relax_candidates:
                relax_candidates = build_relaxation_candidates(effective_filters, settings)
            probeable = [
                c
                for c in relax_candidates
                if c.field not in probed_counts and c.field != adopted_field
            ]
            pending = probeable[:probe_budget]
            if len(probeable) > len(pending):
                # **잘린 걸 조용히 넘기지 않는다**(PR #248 2차 리뷰) — 예산을 넘은 후보는 estCount 를
                # 못 구하고, estCount 없는 칩은 만들 수 없어 아래 조립에서 통째로 빠진다. 실제로
                # 풀면 결과가 있었을 수도 있는데 화면에는 아무 흔적이 없다. 상한이 얼마나 자주
                # 무는지 보이지 않으면 다음 사람이 근거 없이 튜닝하게 된다.
                logger.info(
                    "relaxation_chips_truncated",
                    extra={
                        "budget": probe_budget,
                        "dropped": [c.field for c in probeable[probe_budget:]],
                    },
                )
            if pending:
                # [지연 특성] 이 probe 는 아래 산문 token 보다 **앞**이라, 결과가 부족한 턴일수록
                # 첫 글자가 늦게 나간다(결과가 넉넉하면 이 블록 자체를 안 탄다). 병렬이라 후보 수와는
                # 무관하고 왕복 1회분이며 first-token 예산 안이지만, "실망스러운 턴이 더 느리다"는
                # 특성은 남는다. 없애려면 token 을 먼저 흘리고 칩만 나중에 붙이는 구조 분리가
                # 필요한데(§3.1 상 suggestions 는 token 뒤여도 된다), 실측 없이 손댈 일은 아니다.
                outcomes = await asyncio.gather(*(_probe(c) for c in pending))
                for cand, outcome in zip(pending, outcomes, strict=True):
                    if outcome is not None:
                        probed_counts[cand.field] = outcome[0].total_count
            relaxation_chips = [
                SuggestionChip(
                    label=_strip_unsafe(c.label),
                    relaxation=RelaxationRef(field=c.field, value=c.value),
                    est_count=probed_counts[c.field],
                )
                for c in relax_candidates
                # estCount==0 인 칩은 제외(§3.1) — 눌러도 빈 화면인 제안을 주지 않는다.
                if c.field != adopted_field and probed_counts.get(c.field, 0) > 0
            ]
        except Exception as exc:  # noqa: BLE001 - 완화 칩 실패가 턴을 죽이지 않게(degrade)
            # CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
            logger.warning("relaxation_chips_failed", extra={"reason": str(exc)})
            relaxation_chips = []

    # [#113] 이번 턴에 제안한 칩을 스레드에 기억한다 — FE 는 칩을 누르면 label 을 그대로 다음 턴
    # message 로 보내므로(jarvis-frontend `applySuggestion`), 다음 턴에 그 label 을 만나면 LLM 이
    # 의문문에서 숫자를 다시 뽑는 대신 여기 저장한 정확한 값을 쓴다. 칩이 없으면 빈 dict 로 비운다 —
    # 화면에 없는 옛 제안이 살아 있으면 사용자가 보지도 않은 조건이 되살아난다.
    if relax_store is not None and thread_key:
        try:
            # 제안한 칩과 **자동 적용된 완화**를 한 스냅샷으로 함께 쓴다(PR #248 리뷰) — 따로
            # 두 번 쓰면 뒤엣것만 실패했을 때 "칩은 이번 턴, 완화는 지난 턴"인 찢어진 상태가
            # 남아, 다음 턴 "그 중에" 가 화면에 보여준 적 없는 완화를 이어붙인다.
            # 채택이 없었으면 applied 를 None 으로 **비운다**: 옛 값이 남으면 같은 일이 벌어진다.
            await relax_store.put(
                thread_key,
                {
                    chip.label: {"field": chip.relaxation.field, "value": chip.relaxation.value}
                    for chip in relaxation_chips
                    if chip.relaxation is not None
                },
                {"field": adopted_field, "value": adopted_value} if adopted_field else None,
            )
        except Exception as exc:  # noqa: BLE001 - 기억 실패가 스트림을 죽이지 않게(degrade)
            # 여기는 상품·칩이 이미 확정된 지점이다. 기억에 실패하면 다음 턴 칩 클릭이 종전처럼
            # decompose 해석으로 처리될 뿐인데, 예외를 올리면 **정상 완료될 추천 턴이 통째로**
            # 죽는다. CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
            logger.warning("relaxation_offer_write_failed", extra={"reason": str(exc)})

    matched_after_dedup = result.total_count

    # 되돌리기 칩 — 억제된 소모품 카테고리별(estCount==0 제외, §3.1).
    # estCount 는 **이번 검색 응답 내 억제 수**(page-local 근사)다 — 소모품 억제는 embedding_rerank_limit
    # 최종 절단 **전** 후보 전량을 훑어 세므로(위 loop) DB 전체 기준 진짜 억제 수보다 작을 수 있어
    # 가용한 최선의 추정치를 쓴다. 별도 totalCount 필드는 불필요로 확정(#100 P2).
    revert_chips = [
        SuggestionChip(
            label=_strip_unsafe(f"{cat_samples[c]}은 최근 구매 — 다시 추천받기"),
            revert=RevertRef(category=_strip_unsafe(c)),
            est_count=n,
        )
        for c, n in suppressed_by_cat.items()
        if n > 0
    ]

    if not candidates:
        # 3분기: 검색 자체 0건 / 소모품 카테고리 억제로 비워짐 / exact 최근구매로 비워짐 — 원인별 안내.
        if not had_candidates:
            text = (
                "조건에 맞는 상품을 찾지 못했어요. 아래 조건을 넓혀볼까요?"
                if relaxation_chips  # 무엇을 어떻게 바꿀지 칩으로 함께 제시한다(#113)
                else "조건에 맞는 상품을 찾지 못했어요. 조건을 조금 바꿔볼까요?"
            )
        elif suppressed_by_cat:
            text = "최근 구매하신 카테고리라 결과를 가렸어요. 아래에서 되돌리거나 다른 조건으로 찾아볼까요?"
        else:
            text = "찾은 상품이 모두 최근에 구매하신 것들이에요. 다른 상품을 추천해 드릴까요?"
        yield sse("token", TokenData(text=text).model_dump(by_alias=True))
        # 전부 억제됐어도 되돌리기 칩은 준다(사용자가 복원 가능) + 0건 완화 칩(#113).
        if zero_chips := revert_chips + relaxation_chips:
            yield sse("suggestions", SuggestionsData(chips=zero_chips).model_dump(by_alias=True))
        yield sse(
            "done",
            DoneData(finish_reason="zero_result").model_dump(by_alias=True),
        )
        return

    # 니즈별 목록 분할 판정(REQ-REC-024, api-spec §4.2 PICK_ONE×N) — case 3(목적·상황형 발화)이
    # 니즈 여럿으로 전개된 턴에서만 나눈다. "유럽여행 필요한 거"의 파우치 후보와 어댑터 후보는
    # 서로 대안이 아니라 **다른 니즈**라 한 카드 묶음에 섞이면 사용자가 비교할 수 없다.
    # case 3 이 아닌 멀티 leg(예: 리파인 승계)은 종전대로 목록 1건 — 전개가 일어난 턴만 분할한다.
    # leg_of 가 비면(단일 filters 검색 경로) 나눌 근거 자체가 없다.
    need_legs = decision.category_legs
    split_by_need = decision.case == 3 and len(need_legs) > 1 and bool(leg_of)
    # 분할 시 rerank 예산은 목록 수만큼 늘린다 — 전역 expose_max 로 자르면 니즈 하나가 예산을
    # 독식해 나머지 니즈 목록이 비어버린다.
    # 세는 단위는 **후보가 실제로 남은 니즈**다(PR #212 리뷰) — 검색 0건·최근구매 dedup 으로
    # 비워진 니즈까지 세면 rerank 가 쓰지도 못할 항목 수를 요구하고 출력 예산만 부푼다.
    # 후보 수를 넘겨도 의미가 없어 함께 상한한다.
    # MAX_LISTS 로 클램프 — 계약상 그 이상은 push 되지 않으므로(아래 절단) 잘려나갈 니즈까지
    # 예산에 세면 rerank 가 쓰지도 못할 항목을 요구한다. config 가 category_fanout_max ≤
    # MAX_LISTS 를 이미 강제하지만, 두 경로가 나중에 갈라져도 예산은 틀리지 않게 여기서도 막는다.
    populated_needs = min(
        len({leg_of[p.product_id] for p in candidates if p.product_id in leg_of}), MAX_LISTS
    )
    expose_budget = (
        min(settings.expose_max * populated_needs, len(candidates))
        if split_by_need
        else settings.expose_max
    )
    # 니즈 경계를 rerank 에도 알린다(PR #212 리뷰) — 안 알리면 LLM 이 전역 관련도로만 정렬해
    # 한 니즈가 상위권을 쓸고, 굶은 니즈는 아래 _split_by_need 가 검색순서로 보충한다.
    # 그 보충분엔 rationale 이 없어 근거 없는 카드가 나가는데 rerank 는 "정상 성공"이라
    # rerank_degraded 로 드러나지 않는다. 단일 목록 경로에는 None 을 넘겨 프롬프트를 그대로 둔다.
    need_of = (
        _need_names(need_legs, leg_of=leg_of, product_ids=[p.product_id for p in candidates])
        if split_by_need
        else None
    )

    # rerank — smart tier 1회. 실패/타임아웃/유효후보 0건 시 검색순서 상위 N 으로 degrade(하드 제약 유지).
    if observer is not None:
        observer.record_model_call(resolve_model_id(settings, "smart"))
    rerank_degraded = False
    try:
        with trace_span(
            "llm.rerank",
            "llm",
            {"model": resolve_model_id(settings, "smart")},
        ):
            rr = await rerank(
                llm,
                query=request.message,
                candidates=candidates,
                profile_summary=profile,
                tier="smart",
                expose_max=expose_budget,
                need_of=need_of,
                per_need=settings.expose_max if split_by_need else None,
                # [#132] 사용자가 평점을 명시했는지 — 무평점 후보의 근거문 고지 지시를 켠다.
                # 완화가 적용됐으면 `effective_filters` 가 그 결과라 표시-실제가 어긋나지 않는다.
                rating_min_requested=effective_filters.rating_min is not None,
            )
        ranked_ids = [pid for pid, _ in rr.ranked]
        reason_by_id = dict(rr.ranked)  # 상품별 근거(§4.2) — (productId, rationale) 튜플 → 맵
        comment = _strip_unsafe(rr.overall_comment)
    except LLMError:
        rerank_degraded = True
        if trace := current_request_trace():
            trace.mark_degraded("rerank_fallback")
        ranked_ids = [p.product_id for p in candidates[:expose_budget]]
        reason_by_id = {}  # degrade 경로엔 rerank 근거 없음 — reasons 는 빈 배열(계약상 선택)
        # [#133] 품질 저하를 **고지한다**. 종전 문구("요청하신 조건으로 찾은 상품들이에요")는
        # 평상시와 구분되지 않아 개인화·근거가 통째로 사라진 사실이 사용자에게 가려졌다.
        # config 값은 운영자 주입이라 소스 리터럴이 아니다 — 정상 경로(rr.overall_comment)와
        # 같은 _strip_unsafe 정제를 받는다.
        comment = _strip_unsafe(settings.rerank_fallback_notice)

    # [#120 PR#230 리뷰] 지목 상품 고정 — rerank 는 relevance 로 expose_max 개만 고르고 "이건 반드시"
    # 라는 고정 수단이 없어(need_of/per_need 는 니즈 분할용), exact 제외·상한 절단을 다 통과한
    # 지목 상품이 여기서 조용히 빠질 수 있다. 쿼리에 상품명이 있으니 보통은 뽑히지만 그건
    # 휴리스틱이지 보장이 아니다. **후보에 남아 있는데 rerank 가 빠뜨린 것만** 앞에 얹어
    # "지목하면 다시 추천된다"를 강제한다 — rerank 가 이미 골랐으면 순서를 건드리지 않는다.
    # 근거(reason)는 없지만 §4.2 상 reasons 는 선택이고 degrade 경로도 같은 형태다.
    if repurchase_ids:
        already = set(ranked_ids)
        pinned = [
            p.product_id
            for p in candidates
            if p.product_id in repurchase_ids and p.product_id not in already
        ]
        ranked_ids = pinned + ranked_ids
    # 노출 개수 보정 + 목록 분할 — 보정·상한은 **목록 하나 기준**이다(REQ-REC-021 5~9개, v0.11.0).
    # 분할하지 않으면 목록이 하나뿐이라 종전과 같은 전역 보정·절단이다.
    exposed_groups = _split_by_need(
        ranked_ids,
        candidates,
        leg_of=leg_of if split_by_need else {},
        leg_count=len(need_legs) if split_by_need else 1,
        expose_min=settings.expose_min,
        expose_max=settings.expose_max,
    )
    # 계약 상한(§4.2 lists ≤10)을 **여기서** 자른다 — 아래 ranked_ids 는 "실제로 push 되는 상품"
    # 이어야 last_reco("그거 담아줘")와 관측 로그가 노출과 어긋나지 않는다.
    # config 가 category_fanout_max ≤ MAX_LISTS 를 강제하므로 도달하지 않는 방어선이지만,
    # 도달하면 니즈가 조용히 사라지는 것이라 로그를 남긴다(silent cap 금지).
    if len(exposed_groups) > MAX_LISTS:
        logger.warning(
            "reco_lists_truncated",
            extra={"groups": len(exposed_groups), "cap": MAX_LISTS},
        )
        exposed_groups = exposed_groups[:MAX_LISTS]
    ranked_ids = [pid for _, group in exposed_groups for pid in group]
    buy_all_mode = settings.budget_set_enabled and decision.buy_all and split_by_need
    plan = None
    budget_sets_failed = False
    infeasible_due_to_budget = False
    if buy_all_mode:
        ranked_priority = {product_id: rank for rank, product_id in enumerate(ranked_ids)}
        candidate_order: dict[int, int] = {}
        for index, product in enumerate(candidates):
            candidate_order.setdefault(product.product_id, index)
        pools: list[list[tuple[int, int | None]]] = []
        for leg in range(len(need_legs)):
            # 동일 productId 는 최초 후보 하나만 권위로 삼는다. relevance 순서와 저가 순서를
            # 교대로 병합해야 cap 안에서도 상위 품질 신호와 예산 가능 대안을 함께 보존한다.
            unique_candidates = {}
            for product in candidates:
                if (
                    leg_of.get(product.product_id) == leg
                    and product.product_id not in unique_candidates
                ):
                    unique_candidates[product.product_id] = product
            relevance_order = sorted(
                unique_candidates.values(),
                key=lambda product: (
                    ranked_priority.get(product.product_id, len(ranked_priority)),
                    candidate_order[product.product_id],
                    product.product_id,
                ),
            )
            price_order = sorted(
                unique_candidates.values(),
                key=lambda product: (
                    product.price is None,
                    product.price if product.price is not None else 0,
                    ranked_priority.get(product.product_id, len(ranked_priority)),
                    candidate_order[product.product_id],
                    product.product_id,
                ),
            )
            merged: list[tuple[int, int | None]] = []
            selected: set[int] = set()
            positions = [0, 0]
            while len(merged) < settings.budget_set_alt_pool:
                added = False
                for source, ordered in enumerate((relevance_order, price_order)):
                    while (
                        positions[source] < len(ordered)
                        and ordered[positions[source]].product_id in selected
                    ):
                        positions[source] += 1
                    if positions[source] >= len(ordered):
                        continue
                    product = ordered[positions[source]]
                    positions[source] += 1
                    selected.add(product.product_id)
                    merged.append((product.product_id, product.price))
                    added = True
                    if len(merged) >= settings.budget_set_alt_pool:
                        break
                if not added:
                    break
            pools.append(merged)
        try:
            plan = await asyncio.to_thread(
                build_budget_sets,
                pools=pools,
                total_budget=decision.total_budget,
                max_sets=settings.budget_set_max_count,
                max_combinations=settings.budget_set_max_combinations,
                max_items=LIST_MAX_PRODUCTS,
            )
        except Exception as exc:  # noqa: BLE001 - 세트 실패는 종전 PICK_ONE으로 degrade
            logger.warning("budget_sets_failed", extra={"reason": str(exc)})
            budget_sets_failed = True
            plan = None
        if plan is None and not budget_sets_failed and decision.total_budget is not None:
            try:
                # 총액 제한만 제거했을 때 조합이 생기는 경우에만 예산을 실패 원인으로 고지한다.
                # 후보/가격 자체가 부족한 경우는 같은 입력으로도 None 이라 별도 문구로 분리된다.
                infeasible_due_to_budget = (
                    await asyncio.to_thread(
                        build_budget_sets,
                        pools=pools,
                        total_budget=None,
                        max_sets=settings.budget_set_max_count,
                        max_combinations=settings.budget_set_max_combinations,
                        max_items=LIST_MAX_PRODUCTS,
                    )
                    is not None
                )
            except Exception as exc:  # noqa: BLE001 - 진단 실패도 PICK_ONE 스트림은 살린다
                logger.warning("budget_sets_failure_diagnosis_failed", extra={"reason": str(exc)})
                budget_sets_failed = True
        if plan is not None:
            ranked_ids = list(
                dict.fromkeys(product_id for item in plan.sets for product_id in item.product_ids)
            )

    # [#101 #8] 관측성 — 파이프라인 후보 깔때기를 한 줄 구조화 로그로 남긴다(recall 손실·자원 진단).
    # received(수신) → after_dedup(최근구매 제외 후) → compressed(embedding_rerank_limit 절단 후)
    # → final(노출). 임베딩 재정렬 degrade 사유는 backend(_log.warning), rerank degrade 는 여기서.
    logger.info(
        "recommend_pipeline",
        extra={
            "received": received,
            "after_dedup": matched_after_dedup,
            "compressed": len(candidates),
            "final": len(ranked_ids),
            "rerank_degraded": rerank_degraded,
            # [#209 PR#212 리뷰] 니즈별 분할 관측 — rerank degrade 가 **다중 니즈에서만** 튀는지
            # 보려면 목록 수와 요청한 출력 예산이 같은 줄에 있어야 한다. 출력 잘림은 조용히
            # LLMError 로만 보여서(파싱 실패) 이 두 값 없이는 원인을 분리할 수 없다.
            "lists": len(exposed_groups),
            "expose_budget": expose_budget,
            # 근거 없이 나가는 카드 수 — rerank 가 "정상 성공"해도 랭킹이 한 니즈로 쏠리면
            # 굶은 니즈가 검색순서 보충으로 채워져 여기가 오른다. rerank_degraded 로는 안 보이는
            # 품질 저하라 별도 지표가 필요하다(PR #212 리뷰).
            "without_reason": sum(1 for pid in ranked_ids if not reason_by_id.get(pid)),
            # [#113] 완화 관측 — degrade 가 아니라 **정상 동작**이라 mark_degraded 를 쓰지 않는다.
            # 그래도 로그에 남겨야 하는 이유: (1) probe 는 0건/소량 턴에만 붙는 추가 Spring 호출이라
            # 지연·비용의 출처이고, (2) 자동 완화가 걸린 턴은 사용자가 **요청하지 않은 조건**으로
            # 받은 결과라 품질 지표를 그냥 섞으면 안 된다. relax_field 가 null 이 아닌 턴을 분리해
            # 볼 수 있어야 한다(#136/#137 관측 라인과 동일 목적).
            "relax_field": adopted_field,  # 자동 완화로 채택된 필드(없으면 null)
            "relax_probes": probes_spent,  # 이 턴에 쓴 완화 재검색 횟수
            "relax_chips": len(relaxation_chips),
            # [#119] 회원/게스트 턴을 사후 분리해 깔때기(received·after_dedup)를 대조하기 위한
            # 조인 키. 개인화가 후보를 줄이면 회원 쪽 received 가 작게 나온다.
            "profile_present": bool(profile),
            "profile_scope": settings.profile_injection_scope,
            "budget_sets": len(plan.sets) if plan else 0,
            "budget_dropped_legs": len(plan.dropped_legs) if plan else 0,
            "budget_unavailable_legs": len(plan.unavailable_legs) if plan else 0,
            "budget_limited_legs": len(plan.limited_legs) if plan else 0,
            "budget_mode": buy_all_mode,
            "budget_truncated": bool(plan and plan.combinations_truncated),
        },
    )

    if comment:
        yield sse("token", TokenData(text=comment).model_dump(by_alias=True))

    # [#162] 조건 없음 안내 — 없으면 사용자가 인기 상품을 **자기 조건이 반영된 결과**로 오해한다.
    # `popular_degraded` 턴에는 내지 않는다: I-3 가 죽어 무필터 검색으로 떨어진 결과에
    # "인기 상품으로 보여드릴게요" 라고 말하면 거짓이 된다(#133 정직성 규약 — 그 턴은
    # `mark_degraded("popular_fallback")` 로 관측에 남는다).
    if no_condition and not popular_degraded:
        if decision.total_budget is not None:
            # 예산을 말한 턴에는 그 금액을 되짚어 준다 — "조건을 안 주셨다"고 하면 거짓이 된다.
            # format 실패는 문구를 통째로 잃지 않게 원문으로 떨군다(`_set_label` 과 같은 관용구).
            try:
                raw_notice = settings.no_condition_notice_budget.format(
                    budget=f"{decision.total_budget:,}원"
                )
            except (KeyError, IndexError, ValueError):
                logger.warning("no_condition_budget_notice_invalid")
                raw_notice = settings.no_condition_notice_budget
        else:
            raw_notice = settings.no_condition_notice_popular
        if notice := _strip_unsafe(raw_notice):
            yield sse("token", TokenData(text=notice).model_dump(by_alias=True))

    # [#133] 최근 구매 제외(I-19) 실패 고지 — **기본 미고지**(config 기본값 "")다. 조회 실패는
    # "중복이 노출됐다"가 아니라 "걸러내지 못했다"라 실제 중복 여부를 알 수 없어 매 턴 노이즈가
    # 되고, rerank 폴백과 달리 거짓 주장을 하고 있지도 않다. 판단을 되돌릴 여지만 남긴다.
    if dedup_degraded and (dedup_notice := _strip_unsafe(settings.dedup_skipped_notice)):
        yield sse("token", TokenData(text=dedup_notice).model_dump(by_alias=True))

    if (
        buy_all_mode
        and plan is None
        and (
            fallback_notice := _strip_unsafe(
                settings.budget_set_infeasible_notice
                if infeasible_due_to_budget
                else settings.budget_set_candidate_fallback_notice
            )
        )
    ):
        yield sse("token", TokenData(text=fallback_notice).model_dump(by_alias=True))
    if plan is not None and decision.total_budget is not None and plan.dropped_legs:
        dropped_names = [
            name
            for leg in plan.dropped_legs
            if leg < len(need_legs)
            if (name := _need_label(need_legs[leg]))
        ]
        template = _strip_unsafe(settings.budget_set_dropped_notice)
        if dropped_names and template:
            try:
                notice = template.format(items=" · ".join(dropped_names))
            except (KeyError, IndexError, ValueError):
                logger.warning("budget_set_dropped_notice_invalid")
            else:
                if notice := _strip_unsafe(notice):
                    yield sse("token", TokenData(text=notice).model_dump(by_alias=True))
    if plan is not None and plan.unavailable_legs:
        unavailable_names = [
            name
            for leg in plan.unavailable_legs
            if leg < len(need_legs)
            if (name := _need_label(need_legs[leg]))
        ]
        template = _strip_unsafe(settings.budget_set_unavailable_notice)
        if unavailable_names and template:
            try:
                notice = template.format(items=" · ".join(unavailable_names))
            except (KeyError, IndexError, ValueError):
                logger.warning("budget_set_unavailable_notice_invalid")
            else:
                if notice := _strip_unsafe(notice):
                    yield sse("token", TokenData(text=notice).model_dump(by_alias=True))
    if plan is not None and plan.limited_legs:
        limited_names = [
            name
            for leg in plan.limited_legs
            if leg < len(need_legs)
            if (name := _need_label(need_legs[leg]))
        ]
        template = _strip_unsafe(settings.budget_set_limited_notice)
        if limited_names and template:
            try:
                notice = template.format(items=" · ".join(limited_names))
            except (KeyError, IndexError, ValueError):
                logger.warning("budget_set_limited_notice_invalid")
            else:
                if notice := _strip_unsafe(notice):
                    yield sse("token", TokenData(text=notice).model_dump(by_alias=True))

    # 소모품 카테고리 억제 되돌리기 칩(결정 14-F) + 소량 결과 완화 칩(#113).
    # products.ready **앞**이라 §3.1 순서 계약(conditions → token+suggestions → products.ready)을 지킨다.
    if chips_out := revert_chips + relaxation_chips:
        yield sse("suggestions", SuggestionsData(chips=chips_out).model_dump(by_alias=True))

    # push — I-21(경로 B). 성공 시에만 products.ready emit(§3.3).
    # 추천 실행 1회의 상관키(§4.2 v0.17.1) — 노출·클릭·담기·주문을 이 추천에 귀속시키는 조인 키다.
    # listId(사용자에게 전달된 목록)와 역할이 달라 서로 대체하지 않으므로 별도로 발급한다(이슈 #140).
    recommendation_request_id = str(uuid4())  # 정규 UUID 36자 — BE CHAR(36)

    # [#132] 사용자가 평점을 명시한 턴의 무평점 상품 — 근거문에 그 사실을 고지한다.
    # 목록 조립 밖에서 한 번만 구한다(목록마다 후보 전체를 다시 훑지 않게).
    unrated_ids = _unrated_product_ids(candidates, effective_filters)

    def _reasons(product_ids: list[int]) -> list[RecoReason]:
        # reasons — 근거가 있는 **그 목록의** 상품만(빈 rationale·expose_min 보충 상품은 제외).
        # productId 로 키잉하며 순서 권위는 product_ids 라 정렬 불필요(부분집합 허용, §4.2 이슈 #61).
        # 목록 밖 상품의 근거를 실으면 CH-5 가 매칭할 대상이 없어 그대로 버려진다.
        # push(신뢰경계) 직전 정제 — 개행 제거·안전 상한(config, 판매자 입력 영향 자유 텍스트 방어).
        # [#132] 무평점 상품만 예외적으로 **근거가 비어도** 항목을 만든다 — 고지가 실려야 하고,
        # 근거 없이 보충된 카드일수록 사용자가 그 상품을 근거 없이 신뢰한다.
        out: list[RecoReason] = []
        for pid in product_ids:
            raw = reason_by_id.get(pid, "")
            cleaned = (
                _apply_unrated_disclosure(
                    raw, settings.rating_unrated_disclosure_notice, settings.reason_max_len
                )
                if pid in unrated_ids
                else _sanitize_reason(raw, settings.reason_max_len)
            )
            if cleaned:
                out.append(RecoReason(product_id=pid, reason=cleaned))
        return out

    def _entry(leg: int, product_ids: list[int]) -> RecommendationListEntry:
        return RecommendationListEntry(
            list_id=uuid4().hex,  # 목록마다 새 id — 멱등 키 (requestId, listId) 가 겹치면 안 된다
            label=_need_label(need_legs[leg]) if split_by_need else None,
            product_ids=product_ids,
            reasons=_reasons(product_ids),
        )

    def _set_label(item: BudgetSet) -> str | None:
        if item.kind == "cheap":
            raw = settings.budget_set_label_cheap
        elif item.kind == "balanced":
            raw = settings.budget_set_label_balanced
        elif item.kind == "focus" and item.focus_leg is not None:
            need = _need_label(need_legs[item.focus_leg])
            if need is None:
                raw = settings.budget_set_label_balanced
            elif "{need}" not in settings.budget_set_label_focus:
                logger.warning("budget_set_focus_label_invalid")
                raw = settings.budget_set_label_balanced
            else:
                try:
                    raw = settings.budget_set_label_focus.format(need=need)
                except (KeyError, IndexError, ValueError):
                    logger.warning("budget_set_focus_label_invalid")
                    raw = settings.budget_set_label_balanced
        else:
            raw = settings.budget_set_label_alt
        return _strip_unsafe(raw)[:LIST_LABEL_MAX_LEN] or None

    if plan is not None:
        lists = [
            RecommendationListEntry(
                list_id=uuid4().hex,
                label=_set_label(item),
                product_ids=list(item.product_ids),
                reasons=_reasons(list(item.product_ids)),
            )
            for item in plan.sets
        ]
        list_type = "BUY_ALL"
        total_budget = decision.total_budget
    else:
        lists = [_entry(leg, group) for leg, group in exposed_groups]
        list_type = "PICK_ONE"
        total_budget = None

    push = RecommendationPush(
        session_id=request.session_id,
        recommendation_request_id=recommendation_request_id,
        list_type=list_type,
        total_budget=total_budget,
        lists=lists,
    )
    try:
        pushed = bool(await push_fn(push))
    except SpringUnavailableError:
        pushed = False
    if pushed:
        yield sse(
            "products.ready",
            # listIds 의 순서·개수는 push 한 lists 와 같다(§4.2 규약, §3.1 v0.15.26).
            ProductsReadyData(
                session_id=request.session_id,
                list_ids=[entry.list_id for entry in push.lists],
            ).model_dump(by_alias=True),
        )
        # 직전 추천을 장바구니 담기(productId 해소, 경로 B)용으로 보관 — **push 성공 후에만**.
        # push 실패로 카드가 노출되지 않았으면 저장하지 않아 "그거 담아줘"가 미노출 상품을 담지 않는다.
        if cart_store is not None and thread_key is not None:
            name_by_id = {p.product_id: p.name for p in candidates}
            await cart_store.set_last_reco(
                thread_key, [(pid, name_by_id.get(pid, "")) for pid in ranked_ids]
            )
    else:
        # push 실패 → products.ready 없음. rerank 코멘트가 "찾았다"고 했으니 목록 지연을 고지하고
        # 정상 종료한다(경로 B 실패 계약 — error 아님, done 유지).
        # [#133] 문안은 config 주입 — degrade 고지 문구를 한 곳에서 관리한다.
        if trace := current_request_trace():
            trace.mark_degraded("push_skipped")
        if push_notice := _strip_unsafe(settings.push_skipped_notice):
            yield sse("token", TokenData(text=push_notice).model_dump(by_alias=True))

    yield sse(
        "done",
        DoneData(finish_reason="stop").model_dump(by_alias=True),
    )
