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
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.buyer._frames import sse
from app.agents.buyer.recommendation.rerank import rerank
from app.agents.buyer.recommendation.state import RouteDecision, build_condition_chips
from app.core.llm import LLMClient, LLMError, resolve_model_id
from app.core.text import _strip_unsafe
from app.core.tracing import current_request_trace, trace_span
from app.services import spring_client
from app.schemas.chat import (
    ConditionsData,
    DoneData,
    ErrorData,
    ProductsReadyData,
    RevertRef,
    SuggestionChip,
    SuggestionsData,
    TokenData,
)
from app.schemas.spring import (
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

    공백 제거 + casefold 후 유효한 지목이 **정확히 1건일 때만** 해소한다. 복수 지목은 모델이
    LAST_RECOMMENDATIONS 같은 맥락 목록을 에코했을 수 있어, 각 이름이 개별적으로 정확해도 전부
    미해제한다. 단일 지목은 가장 좁은 해석을 고른다 — 완전 일치가 있으면 그것만, 없을 때만
    `지목 in 구매명` 단방향 부분비교로 넓힌다. 단방향 폴백은 발화 표기("무선이어폰")와 상품명
    ("무선 이어폰 프로")이 띄어쓰기·수식어만 다른 경우를 잡되, 긴 지목("무선 이어폰 케이스")을
    짧은 구매명("이어폰")으로 축약해 다른 상품을 푸는 오매칭은 막는다. 아무것도 못 잡으면
    조용히 빈 집합(= 종전 제외 유지)으로 degrade 한다. 과잉 해제보다 미해제가 안전하다.
    """
    if not references:
        return set()
    norms = [n for r in references if (n := "".join(r.split()).casefold())]
    if len(norms) != 1:
        return set()
    names = [
        (item.product_id, "".join((item.product_name or "").split()).casefold()) for item in recent
    ]
    out: set[int] = set()
    for n in norms:
        exact = {pid for pid, name in names if name and name == n}
        if exact:
            # 완전 일치는 **몇 건이든 전부** 푼다 — 아래 부분비교의 모호성 규칙을 여기 적용하면
            # 안 된다(PR #230 리뷰 문의). 부분비교의 복수 매칭은 이름이 서로 달라 지목이 특정에
            # 실패한 것이지만, 완전 일치의 복수 매칭은 전부 **사용자가 말한 바로 그 이름**이라
            # "지목하지 않은 상품"이 없다. 재등록·옵션 분리로 같은 이름이 두 productId 로
            # 존재할 때 미해제하면 #120 버그가 그대로 재발한다(회귀 테스트로 고정).
            out |= exact
            continue
        # 부분비교는 **모호하지 않을 때만** 쓴다 — "세트"·"리필" 같은 짧고 흔한 지목은 최근 구매
        # 여러 건에 한꺼번에 걸려 dedup 을 통째로 무력화한다. 길이 하한은 한국어에서 쓸 수 없어
        # ("소금"·"우유"와 "세트"·"리필"이 모두 2자) 길이 대신 **모호성 자체**를 기준으로 삼는다 —
        # 언어 중립적이고 새 튜너블도 필요 없다. 2건 이상 걸리면 지목이 특정에 실패한 것이므로
        # 아무것도 풀지 않는다(과잉 해제보다 미해제, PR #230 리뷰).
        partial = {pid for pid, name in names if name and n in name}
        if len(partial) == 1:
            out |= partial
    return out


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
    thread_key: str | None = None,
    observer=None,
    request_id: str,
) -> AsyncIterator[str]:
    """추천 서브그래프 스트림. 프레임(SSE str)을 순서대로 산출한다."""
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
    chip_filters = decision.filters
    if drop_keyword:
        chip_filters = decision.filters.model_copy(update={"keyword": None})
    chips = build_condition_chips(chip_filters, categories=[c for c, _ in decision.category_legs])
    yield sse("conditions", ConditionsData(chips=chips).model_dump(by_alias=True))

    # dedup 소스(I-19)와 검색(§4.6)을 **병렬 실행** — §4.7 지연 가드(순차 시 최악 6s, first-token 예산 잠식).
    # dedup 은 검색 응답 뒤 사후필터라 두 호출은 독립적이다. 각 호출이 자체 실패를 삼켜 gather 는 안 깨진다.
    async def _run_search() -> tuple[ProductSearchResult, dict[int, int]] | None:
        """검색 결과와 `productId → leg 인덱스` 맵을 함께 돌려준다.

        맵이 비어 있으면 leg 개념이 없는 경로(단일 filters 검색)라 하류가 목록 1건으로 간다.
        """
        legs = decision.category_legs
        if not legs:
            # 카테고리 매핑 결과 없음(매핑 degrade·비-매핑 경로) → 단일 filters 검색(기존 경로).
            try:
                found = await search(decision.filters, exclude_product_ids=None)
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
        # 단일 카테고리(leg 1개)는 후보 폭을 좁히지 않게 merge_cap(=단일 rerank 입력 예산)을 쓰고,
        # 멀티 fan-out 일 때만 leg 당 per_cat_limit 으로 제한한다(합쳐서 merge_cap 로 재절단).
        leg_limit = (
            settings.category_fanout_per_cat_limit
            if len(legs) > 1
            else settings.category_fanout_merge_cap
        )

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
                    (query or decision.filters.semantic_query)
                    if len(legs) > 1
                    else decision.filters.semantic_query
                )
                # [#51] keyword 는 위에서 계산한 drop_keyword flag 를 공유한다 — 칩 파생과 동일 판단이라
                # 표시-실제가 어긋나지 않는다. 드롭 시 leg 검색어는 leg_semantic 으로 rerank 를 담당하고
                # category 가 후보를 확보하므로 keyword 중복 투입은 불필요(config off 면 leg query→keyword
                # 로 복원, 롤백 안전성). _leg 는 legs 비어있지 않을 때만 호출돼 canonical 은 항상 truthy.
                leg_keyword = None if drop_keyword else (query or decision.filters.keyword)
                leg_filters = decision.filters.model_copy(
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
        return _merge_fanout_results(survived, settings.category_fanout_merge_cap)

    async def _fetch_purchases():
        # 게스트/비회원/판매자/비숫자 sub 는 스킵, I-19 실패는 degrade(dedup 없이 진행, §4.7).
        # [IDOR] role==SELLER 는 user_id=sub·seller_id=sub — 판매자 sub 를 memberId 로 쓰면 안 됨.
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
            if trace := current_request_trace():
                trace.mark_degraded("dedup_skipped")
            return None
        except Exception as exc:  # noqa: BLE001 - I-19 실패는 degrade(dedup 없이 진행, SSE 유지)
            # 최근구매 조회가 예상외 예외를 던져도 추천 스트림을 죽이지 않는다 — None → dedup 스킵(§4.7).
            logger.warning("purchases_fetch_failed", extra={"reason": str(exc)})
            if trace := current_request_trace():
                trace.mark_degraded("dedup_skipped")
            return None

    search_bundle, purchases = await asyncio.gather(_run_search(), _fetch_purchases())
    if search_bundle is None:  # 검색 실패 → SEARCH_FAILED(종료)
        if trace := current_request_trace():
            trace.mark_degraded("search_failed")
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
        repurchase_ids = _resolve_repurchase_ids(recent, decision.repurchase_products)
        consumables = set(settings.consumable_categories)
        for i in recent:
            # 소모품 카테고리인데 사용자가 되돌리지 않은 것만 억제 대상.
            if i.category and i.category in consumables and i.category not in reverted_categories:
                cat_samples.setdefault(i.category, i.product_name or i.category)

    # 사후필터: exact productId 제외 + 소모품 카테고리 억제(§4.7, C-15).
    result: ProductSearchResult = search_result
    # [#101 #8] 관측성 — dedup 이전 수신 후보 수. **경로별 의미 주의(PR#166 리뷰)**: 비-fanout 은
    # Spring 매칭 전량(#101 로 search_catalog 절단 제거)이지만, fan-out 은 _run_search 안에서 이미
    # _merge_fanout_results(merge_cap) 로 절단된 **뒤** 값이라 leg 별 실제 수신 합계가 아니다 — fan-out
    # 의 가장 큰 recall 손실(leg 검색 → merge_cap 절단)은 이 수치 이전에 일어나 로그에 안 잡힌다.
    # leg-합 관측은 별도 과제(관측 이슈 #136/#137 라인, _run_search 가 pre-merge 합을 노출해야 함).
    received = len(result.products)
    had_candidates = bool(result.products)
    suppressed_by_cat: dict[str, int] = {}
    kept = []
    for product in result.products:
        # [#120] 명시 재구매 지목은 exact 제외와 소모품 카테고리 억제를 **둘 다** 면제한다 —
        # exact 만 풀면 소모품 재구매(소금·세제)가 카테고리 억제에 다시 걸려 되돌리기가 안 된다.
        # 카테고리 억제 카운트(suppressed_by_cat)에도 넣지 않아 되돌리기 칩 추정치가 부풀지 않는다.
        if product.product_id in repurchase_ids:
            kept.append(product)
            continue
        if product.product_id in exclude_ids:
            continue
        if product.category in cat_samples:
            suppressed_by_cat[product.category] = suppressed_by_cat.get(product.category, 0) + 1
            continue
        kept.append(product)
    # [#101] 최종 rerank 입력 상한 절단 — search_catalog(사전) 가 아니라 최근구매 dedup·소모품 억제
    # **이후** 여기서 embedding_rerank_limit 으로 압축한다. 사전 절단이면 dedup 대상이 상위에 몰릴 때
    # rerank 후보가 상한 미만이 되는 recall 손실이 있어, 절단을 dedup 이후로 옮겼다(비-fanout 전량·
    # fan-out merge_cap 병합 결과 모두 이 지점에서 최종 절단). matched_after_dedup 은 절단 전 매칭 수.
    matched_after_dedup = len(kept)
    # [#120 PR#230 리뷰] 명시 재구매 지목은 절단 **전에** 앞으로 당긴다 — 이 절단은 원본 검색
    # 순서 기준이라, 되살린 상품이 상한(기본 30) 밖이면 exact 제외를 면제해 놓고도 rerank 후보에
    # 조차 못 들어가 "지목하면 다시 추천된다"는 보장이 조용히 깨진다. 사용자가 직접 지목한 상품이
    # 검색 순서보다 우선하는 게 맞다. stable sort 라 지목 상품끼리·나머지끼리의 상대 순서는
    # 그대로고, 지목이 없으면(기본 경로) 정렬 자체를 건너뛰어 종전과 동일하다.
    if repurchase_ids:
        kept.sort(key=lambda p: p.product_id not in repurchase_ids)
    kept = kept[: settings.embedding_rerank_limit]
    result = ProductSearchResult(products=kept, total_count=matched_after_dedup)

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

    candidates = result.products
    if not candidates:
        # 3분기: 검색 자체 0건 / 소모품 카테고리 억제로 비워짐 / exact 최근구매로 비워짐 — 원인별 안내.
        if not had_candidates:
            text = "조건에 맞는 상품을 찾지 못했어요. 조건을 조금 바꿔볼까요?"
        elif suppressed_by_cat:
            text = "최근 구매하신 카테고리라 결과를 가렸어요. 아래에서 되돌리거나 다른 조건으로 찾아볼까요?"
        else:
            text = "찾은 상품이 모두 최근에 구매하신 것들이에요. 다른 상품을 추천해 드릴까요?"
        yield sse("token", TokenData(text=text).model_dump(by_alias=True))
        if revert_chips:  # 전부 억제됐어도 되돌리기 칩은 준다(사용자가 복원 가능)
            yield sse("suggestions", SuggestionsData(chips=revert_chips).model_dump(by_alias=True))
        yield sse("done", DoneData(finish_reason="zero_result").model_dump(by_alias=True))
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
        comment = "요청하신 조건으로 찾은 상품들이에요."

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
            # [#119] 회원/게스트 턴을 사후 분리해 깔때기(received·after_dedup)를 대조하기 위한
            # 조인 키. 개인화가 후보를 줄이면 회원 쪽 received 가 작게 나온다.
            "profile_present": bool(profile),
            "profile_scope": settings.profile_injection_scope,
        },
    )

    if comment:
        yield sse("token", TokenData(text=comment).model_dump(by_alias=True))

    if revert_chips:  # 소모품 카테고리 억제 되돌리기 칩(결정 14-F)
        yield sse("suggestions", SuggestionsData(chips=revert_chips).model_dump(by_alias=True))

    # push — I-21(경로 B). 성공 시에만 products.ready emit(§3.3).
    # 추천 실행 1회의 상관키(§4.2 v0.17.1) — 노출·클릭·담기·주문을 이 추천에 귀속시키는 조인 키다.
    # listId(사용자에게 전달된 목록)와 역할이 달라 서로 대체하지 않으므로 별도로 발급한다(이슈 #140).
    recommendation_request_id = str(uuid4())  # 정규 UUID 36자 — BE CHAR(36)

    def _entry(leg: int, product_ids: list[int]) -> RecommendationListEntry:
        # reasons — 근거가 있는 **그 목록의** 상품만(빈 rationale·expose_min 보충 상품은 제외).
        # productId 로 키잉하며 순서 권위는 product_ids 라 정렬 불필요(부분집합 허용, §4.2 이슈 #61).
        # 목록 밖 상품의 근거를 실으면 CH-5 가 매칭할 대상이 없어 그대로 버려진다.
        # push(신뢰경계) 직전 정제 — 개행 제거·안전 상한(config, 판매자 입력 영향 자유 텍스트 방어).
        reasons = [
            RecoReason(product_id=pid, reason=cleaned)
            for pid in product_ids
            if (cleaned := _sanitize_reason(reason_by_id.get(pid, ""), settings.reason_max_len))
        ]
        return RecommendationListEntry(
            list_id=uuid4().hex,  # 목록마다 새 id — 멱등 키 (requestId, listId) 가 겹치면 안 된다
            label=_need_label(need_legs[leg]) if split_by_need else None,
            product_ids=product_ids,
            reasons=reasons,
        )

    # listType 은 항상 싣는다 — 목록 개수는 lists 길이로 알 수 있지만 이 값은 개수로 복원할 수
    # 없다(§4.2). 니즈별이든 단일이든 목록 **안**의 상품들은 서로 대안이라 PICK_ONE 이다:
    # 니즈별은 "파우치 중 하나 + 어댑터 중 하나"이지 "목록 전부를 산다"(BUY_ALL)가 아니다.
    # 세트 여러 안(BUY_ALL×N)과 totalBudget 은 아직 이 그래프가 내지 않는다(이슈 #60·#163).
    push = RecommendationPush(
        session_id=request.session_id,
        recommendation_request_id=recommendation_request_id,
        list_type="PICK_ONE",
        lists=[_entry(leg, group) for leg, group in exposed_groups],
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
        if trace := current_request_trace():
            trace.mark_degraded("push_skipped")
        yield sse(
            "token",
            TokenData(
                text="목록을 준비하는 데 문제가 있었어요. 잠시 후 다시 시도해 주세요."
            ).model_dump(by_alias=True),
        )

    yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
