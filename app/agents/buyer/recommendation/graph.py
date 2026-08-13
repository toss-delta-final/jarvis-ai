"""추천 파이프라인 스트리밍 (SPEC-RECOMMEND-001 §5.3/§6, 이슈 #2 MVP 슬라이스).

decompose 산출(RouteDecision) 이후: conditions → search(Spring 위임) → rerank(Sonnet) →
근거 token → push(I-21) → products.ready(경로 B) → done.
degrade(§7): SEARCH_FAILED(error·종료) / rerank 실패→검색순서 폴백 / push 실패→products.ready 스킵.
SSE 는 상품 카드를 싣지 않는다(경로 B) — products.ready 는 {sessionId, listIds} 상관키만.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.buyer._frames import progress as progress_frame
from app.agents.buyer._frames import sse
from app.agents.buyer.cart.options import OptionHint
from app.agents.buyer.recommendation.budget_sets import BudgetSet, BudgetSetPlan, build_budget_sets
from app.agents.buyer.recommendation.need_priority import classify_need_priorities
from app.agents.buyer.recommendation.no_condition import (
    dedup_exposed_names,
    has_total_budget,
    rank_by_profile,
    within_budget,
)
from app.agents.buyer.recommendation.overall_comment_grounding import (
    FinalRecommendationView,
    validate_and_render_overall_comment,
)
from app.agents.buyer.recommendation.search_guard import (
    is_category_mapping_dropped,
    is_popular_fallback_safe,
    is_unfiltered_payload,
)
from app.agents.buyer.recommendation.underspecified import (
    build_reask_question,
    within_price_range,
)
from app.agents.buyer.recommendation.rerank import code_assisted_fallback_reasons, rerank
from app.agents.buyer.recommendation.rerank_code_assisted import CodeScoringContext
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
from app.core.config import _rescue_chain_stage_counts
from app.core.logging import log_structured, safe_fingerprint
from app.core.llm import LLMClient, LLMError, resolve_model_id
from app.core.reco_provenance import (
    ProvenanceItem,
    ProvenanceList,
    RankSource,
    emit_recommendation_provenance,
)
from app.core.text import _strip_unsafe
from app.core.tracing import current_request_trace, trace_span
from app.services import spring_client
from app.services.search_service import apply_ai_side_filters
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
    RecommendationContext,
    RecommendationListEntry,
    RecommendationPush,
    SpringProduct,
)
from app.services.spring_client import SpringUnavailableError

logger = logging.getLogger(__name__)

_INACTIVE_STATUSES = frozenset(
    {"CANCELED", "CANCELLED", "RETURNED"}
)  # 보유 아님(철자 양쪽 — spec §4.7 혼용) → dedup 제외 대상 아님
_SEARCH_RETRY = object()
_SEARCH_DONE = object()


def _rerank_prompt_version(
    *,
    degraded: bool,
    ranking_arm: str,
    grounding_arm: str,
    grounding_prompt_version: str,
    scoring_prompt_version: str,
    code_assisted_prompt_version: str,
) -> str | None:
    """Return the prompt contract that actually produced the exposed ranking."""

    if degraded:
        return None
    if ranking_arm == "code_assisted":
        return code_assisted_prompt_version
    if ranking_arm != "current":
        return scoring_prompt_version
    if grounding_arm != "current":
        return grounding_prompt_version
    return "rerank-v1"


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


def _need_priority_labels(need_legs: list[tuple[str, str | None]]) -> list[str] | None:
    """priority 분류기에 넘길 니즈 이름 목록 — 라벨 없는 leg 이 하나라도 있으면 `None`.

    [PR #314 리뷰 F-8] 반환 타입 자체(`list[str] | None`)가 all-or-nothing 을 강제한다 — 라벨을
    만나는 즉시 담고, `None` 을 만나면 그 자리에서 포기하고 돌아간다. 부분적으로 걸러낸
    `list[str]` 을 만들 길이 코드 모양에 없으므로 "None 인 leg 만 개별 스킵"으로 리팩터하려면
    이 함수의 조기 반환 자체를 고쳐야 한다(= 실수로 못 미끄러진다). 호출부는 `is None` 분기
    이후 `list[str]` 로 자연히 좁혀지므로 `cast` 가 필요 없다.
    """
    labels: list[str] = []
    for leg in need_legs:
        label = _need_label(leg)
        if label is None:
            return None
        labels.append(label)
    return labels


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


async def _collect_priority_task(task) -> tuple[int, ...] | None:  # noqa: ANN001
    """니즈 priority 분류기 태스크를 회수한다 — 실패는 전부 None(=신호 없음) (#281).

    `classify_need_priorities` 가 이미 자기 예외를 삼키지만 여기서 한 겹 더 감싼다: 태스크
    레벨 실패(이벤트루프 종료 등)는 그 함수 안에서 잡히지 않는데, 그것 때문에 무관한 BUY_ALL
    턴이 죽으면 안 된다. 폴백은 오늘 동작(`budget_sets` 의 균일 priority 처리)이라 손해가 없다.

    **`except Exception` 을 `BaseException` 으로 넓히지 말 것.** `CancelledError` 는
    `BaseException` 이라 여기 걸리지 않고 그대로 전파된다 — 값을 기다리는 이 자리에서 바깥
    취소가 오면 턴은 거기서 끝나야 한다(`app/agents/buyer/graph.py::_collect_scope_task` 와
    같은 함정, #84 — 그 파일은 편집 금지라 같은 패턴을 여기 지역 헬퍼로 다시 둔다).
    """
    if task is None:
        return None
    try:
        return await task
    except Exception as exc:  # noqa: BLE001 - 보조 신호 회수 실패가 턴을 죽이지 않게(degrade)
        logger.warning("need_priority_task_failed", extra={"reason": str(exc)})
        return None


def _cancel_priority_task(task) -> None:  # noqa: ANN001
    """니즈 priority 분류기 태스크를 **동기적으로만** 취소한다 (#281).

    `await` 하지 않는다 — 취소된 태스크를 `await` 하면 바깥에서 온 취소(클라이언트 연결 종료
    등)가 삼켜져, 이미 끊긴 요청인데 스트림이 정상처럼 계속 진행하는 함정이 있다(근거 전문은
    `app/agents/buyer/graph.py::_cancel_scope_task` docstring 참조 — 그 파일은 편집 금지라
    같은 패턴을 여기 지역 헬퍼로 다시 둔다).

    이미 끝난 태스크면 아무 것도 하지 않는다 — 정리 지점이 정상 회수(try 본문)와 `finally`
    둘이라 같은 태스크에 두 번 불릴 수 있다.
    """
    if task is not None and not task.done():
        task.cancel()


def _need_priority_required_dropped(
    priorities: tuple[int, ...] | None, plan: BudgetSetPlan | None
) -> bool:
    """필수(priority 1) 니즈가 예산 **또는** 목록 상한 때문에 빠진 턴인가 — `recommend_pipeline`
    관측 전용 (#281).

    [PR #314 리뷰] 이름·필드 키는 `_dropped`(예산 제외 전용)로 남기지만 **의미는 `dropped_legs`
    (총액 예산 초과 제외)와 `limited_legs`(계약 상한 `max_items` 초과 제외) 둘 다**를 덮는다 —
    게이트가 이제 이 둘 중 어느 경로든 열릴 수 있는 턴에서 분류기를 부르므로(위
    `need_priority_gate` 참조), 관측이 한쪽만 보면 `limited_legs` 로 필수 니즈가 빠져도
    조용히 안 잡힌다. 이름을 안 바꾼 이유: 이 필드는 아직 출고된 적이 없어 개명 자체는
    자유롭지만, "예산 초과로 제외"라는 원래 취지가 "목록 상한 초과"에도 그대로 대응돼(둘 다
    "이 니즈를 포기해야 했다") 새 이름을 짓기보다 이 docstring 으로 범위를 넓히는 쪽을 택했다.

    `plan.dropped_legs`/`plan.limited_legs` 의 leg 인덱스는 `priorities` 범위 안에 있다는 것이
    오늘은 항상 참이지만(게이트가 라벨 `None` 인 leg 이 있으면 태스크 자체를 안 만들어
    `len(priorities) == len(need_legs) == len(pools)` 가 성립한다), 그 정합은
    `graph.py`(게이트)와 `need_priority.py`(`_validate_priorities` 길이 검증) **두 파일에
    걸친 암묵적 불변식**이다. 관측 필드 하나가 그 불변식에 기대 `IndexError` 를 내면 이미
    계산이 끝난 추천 턴 전체가 죽는다 — 이 파일이 고지 문구 생성부(`if leg < len(need_legs)`,
    아래 `_split_by_need` 이후 dropped/unavailable/limited 알림 블록 참조)에서 이미 쓰는
    관용구와 같은 모양으로 범위 밖 leg 을 **두 경로 모두에서** 조용히 건너뛴다.
    """
    if priorities is None or plan is None:
        return False
    excluded_legs = (*plan.dropped_legs, *plan.limited_legs)
    return any(leg < len(priorities) and priorities[leg] == 1 for leg in excluded_legs)


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
    # [#336] 과소지정 발화인가(`underspecified.is_underspecified_turn` 판정 결과). no_condition
    # 은 이 집합의 부분집합이다 — 판정은 호출부에서 한다(그쪽에만 `prior` 가 있다, no_condition
    # 과 같은 근거). 기본 False 라 미주입 호출·기존 테스트는 그대로 통과한다.
    underspecified: bool = False,
    popular_fn=None,  # I-3 조회. 미지정 시 라이브 기본값(테스트는 fake 주입)
    # [#162] 미리 만들어 둔 취향 벡터(`read_profile_summary()["embedding"]`). 회원이면서 벡터가
    # 있을 때만 취향 랭킹 경로를 탄다 — 게스트·신규회원·구 요약(벡터 없음)은 인기 상품으로 간다.
    profile_vec: list[float] | None = None,
    # [#427, DESIGN-SHARED-BUDGET-384 §3 D2] 스트림 시작 절대 시각(`open_stream` 의
    # `loop.time()` 원점) — 구제 체인 공유 예산(`rescue_deadline`)의 원점이다. `None` 이면
    # 예산 판정 전체가 무동작이다(아래 참조) — "없으면 지금을 원점으로" 폴백은 절대 금지다.
    turn_started_at: float | None = None,
) -> AsyncIterator[str]:
    """추천 서브그래프 스트림. 프레임(SSE str)을 순서대로 산출한다."""
    popular_fn = popular_fn or spring_client.get_popular_products
    # [#427, DESIGN-SHARED-BUDGET-384 §3 D2·D3] 구제 체인(F-1/#343/자동완화 probe)이 잔여
    # 예산을 판정할 데드라인 — 함수 진입 시 **한 번만** 계산한다(재계산 금지: 매 단계에서
    # 다시 계산하면 D2 가 고치려던 "새 창을 부여" 버그(F1)가 되살아난다). `turn_started_at`
    # 이 None 이면(=open_stream 배선이 없는 호출부·구 테스트) `rescue_deadline` 도 None 이고
    # 아래 예산 판정 헬퍼는 전부 "full"(무동작)만 반환한다 — **"없으면 지금 시각을 원점으로
    # 잡는다"로 폴백하지 않는다**: 진짜 원점보다 늦은 시각을 원점으로 쓰면 `rescue_deadline`
    # 이 스트림 실제 캡(`stream.py` 의 `deadline`)을 넘어갈 수 있다(D2 가 F1 로 기각한 바로 그
    # 예산 초과 승인 버그).
    rescue_deadline: float | None = (
        None
        if turn_started_at is None
        else turn_started_at
        + (settings.stream_total_timeout_buyer_s - settings.rescue_tail_reserve_s)
    )
    # 공유 계수 원천(D7) — 기동 검증기(`config.py::Settings.
    # _require_search_retry_within_stream_budget`)와 이 함수의 "남은 단 수" 계산이 같은
    # 함수에서 계수를 얻어야 한쪽만 고쳐지는 드리프트를 막는다.
    _rescue_stage_counts = _rescue_chain_stage_counts(
        relaxation_max_rounds=settings.relaxation_max_rounds,
        auto_fields=settings.relaxation_auto_fields,
        chip_fields=settings.relaxation_chip_fields,
        category_expand_enabled=settings.category_expand_enabled,
    )
    # [#427 D7 관측 필드] observe 모드에서는 "집행했다면 이랬을 값"(반사실) — 아래
    # `recommend_zero_result`/`recommend_pipeline` 로그가 `rescue_budget_mode` 와 함께 싣는다.
    rescue_stage_narrowed_timeout_ms: int | None = None
    rescue_stage_skipped_budget = False

    def _stage_budget(remaining_stages: int, *, attempts: int) -> tuple[str, float]:
        """이 단 진입 직전 잔여 예산 판정 (D3·D4). 반환은 (verdict, 이 단에 줄 수 있는 초).

        verdict 는 `"full"`(좁히지 않는다) / `"narrow"`(좁혀서 시도) / `"skip"`(건너뛴다
        — 호출부가 이 단의 skip 허용 여부에 따라 실제로 건너뛸지 narrow 로 강등할지 정한다).
        남은 단 수로 균등 배분한다(D4) — 지금 단이 잔여를 통째로 쓰면 뒤에 남은 단이 곧바로
        굶는다. `remaining_stages` 는 이 단을 포함해 이 턴에 이론상 남아 있는 단 수다.

        [PR #452 리뷰 R2, #306] `attempts` 는 이 단이 실제로 밟을 시도 횟수 —
        `spring_client.search_products` 의 `attempts = spring_max_retries + 1` 산출식과
        **글자 그대로 같은 규칙**이어야 한다(호출부가 넘긴다, D7 —
        `config.py::_rescue_chain_serial_budget_s` 가 같은 균일식을 기동 검증 쪽에서
        모델링한다). 단 상한(`stage_cap`)은 `spring_search_timeout_s * attempts` 다 —
        `"full"` 판정을 1 회분 상한으로 고정하면, 재시도가 있는 단은 그 배만큼 벽시계를 실제로
        쓰는데도 좁히지 않고 통과시켜 `rescue_deadline` 을 과다 승인한다.
        """
        stage_cap = settings.spring_search_timeout_s * attempts
        if rescue_deadline is None:
            return "full", stage_cap
        # [PR #452 리뷰 R1] `rescue_deadline` 의 원점은 `open_stream` 의 `loop.time()` 이다 —
        # 데드라인과 비교하는 이 지점만 같은 시계(`asyncio.get_running_loop().time()`)를 써야
        # D2 의 "원점 일치" 가 실측 우연이 아니라 증명이 된다(uvloop 는 `time.monotonic()` 과
        # 같다는 보장이 언어 차원에 없다).
        remaining = rescue_deadline - asyncio.get_running_loop().time()
        n = max(remaining_stages, 1)
        # 균등 배분(remaining / n)은 근사다 — 단별 상한(stage_cap)이 서로 달라 엄밀히는 가중
        # 배분이 맞지만, 그 정밀화는 이 수정 범위 밖이다(R2 는 "full" 경계와 상한만 바꾼다).
        granted = min(stage_cap, remaining / n)
        if granted >= stage_cap:
            return "full", stage_cap
        if granted >= settings.rescue_stage_min_timeout_s:
            return "narrow", granted
        return "skip", granted

    def _apply_stage_budget(
        remaining_stages: int, *, allow_skip: bool, attempts: int
    ) -> tuple[bool, float | None]:
        """`_stage_budget` 판정을 관측 필드에 반영하고, `rescue_budget_mode` 에 따라
        (건너뛸지, narrow 로 줄 budget_s) 를 반환한다 (D4).

        판정·관측(`rescue_stage_narrowed_timeout_ms`/`rescue_stage_skipped_budget`)은 모드와
        무관하게 항상 계산한다 — observe 모드에서는 이 값이 "집행했다면 이랬을 값"(반사실)이다.
        실제 집행(좁히기/건너뛰기)만 모드로 가른다. `allow_skip=False`(본검색)는 `skip` 판정을
        절대 skip 으로 실행하지 않는다 — narrow 로 강등한다(본검색을 건너뛰면 그 턴은 무조건
        `SEARCH_FAILED` 라, 예산을 아끼려다 턴을 죽인다).

        [리뷰 F1] **실제로 집행되는 narrow 예산은 항상 `rescue_stage_min_timeout_s` 이상으로
        clamp 한다.** 데드라인이 이미 지난 턴(`remaining <= 0`)에서는 `_stage_budget` 의
        `granted` 가 음수/0 일 수 있는데, 그 값을 그대로 `narrow_search_budget` 에 넘기면
        `asyncio.wait_for(timeout=음수)` 가 즉시 만료돼 **HTTP 요청 자체가 나가지 않는다** —
        `allow_skip=False`(본검색)가 정확히 막으려는 상황(요청 없이 실패)이 그대로 재현되고,
        F-1/#343/자동완화 probe 는 `narrow` 모드에서 `relaxing` 등 progress 를 emit 해 놓고
        아무 일도 하지 않는 거짓 신호(H4)가 된다. clamp 는 두 경로 모두에 적용한다 — 본검색의
        `skip → narrow` 강등과, `narrow`(narrow_skip 아님) 모드에서 `skip` 판정을 그대로
        집행하는 경로. 데드라인을 최대 `rescue_stage_min_timeout_s` 만큼 넘길 수 있지만 그건
        `rescue_tail_reserve_s` 가 흡수한다 — 예산이 없다고 요청 자체를 안 보내는 것보다 낫다.

        [PR #452 리뷰 R2] `narrow_search_budget()` 로 주입하는 `granted` 는 이미 **총시간
        (재시도 포함) 상한**이다(`search_products` 가 override 를 그 의미로 쓴다) — 좁힐 때
        `attempts` 를 다시 곱하지 않는다. `attempts` 는 오직 `_stage_budget` 의 `"full"`
        경계·`min()` 상한에만 쓰인다.

        [PR #452 리뷰 R5] **F1 하한 clamp(아래) 는 상한(`stage_cap`)으로 다시 씌운다.**
        `RESCUE_STAGE_MIN_TIMEOUT_S >= stage_cap`(=`spring_search_timeout_s * attempts`)로
        튜닝되면(기동 검증기가 이제 이 조합을 막는다 — `Settings.
        _require_rescue_stage_min_timeout_below_search_budget` 참조) 하한 clamp 만 있는
        코드는 "예산이 모자라 좁힌다"면서 안 좁힌 것보다 **더 큰** 값을 주입한다 — 좁히기의
        취지와 정반대다. `stage_cap` 은 항상 양수(`spring_search_timeout_s > 0`,
        `attempts >= 1`)이므로 이 상한 clamp 는 F1 의 "집행되는 예산이 음수/0 이 아니다"라는
        성질과 양립한다(하한이 상한을 넘는 병적 설정은 기동 검증기가 별도로 막는다).
        """
        nonlocal rescue_stage_narrowed_timeout_ms, rescue_stage_skipped_budget
        stage_cap = settings.spring_search_timeout_s * attempts
        verdict, granted = _stage_budget(remaining_stages, attempts=attempts)
        if verdict == "full":
            return False, None
        if verdict == "skip" and not allow_skip:
            verdict = "narrow"  # 본검색은 절대 건너뛰지 않는다
        # [리뷰 G1] 세 모드가 `rescue_stage_skipped_budget`/`rescue_stage_narrowed_timeout_ms` 에
        # 기록하는 값: `narrow_skip` = 실제로 건너뜀(skipped_budget=True) / `observe` = 집행하지
        # 않고 "skip 이었다면"의 반사실(skipped_budget=True) / `narrow` = skip 판정도 narrow 로
        # 강등해 실제로 시도하므로 skipped_budget=False 로 남기고 clamp 된 하한값을
        # narrowed_timeout_ms 에 남긴다(하한에 걸렸다는 신호).
        if verdict == "skip":
            if settings.rescue_budget_mode == "narrow_skip":
                rescue_stage_skipped_budget = True
                return True, None
            if settings.rescue_budget_mode == "observe":
                rescue_stage_skipped_budget = True
                return False, None
            # narrow 모드: skip 판정도 narrow 로 강등해 시도한다 — 아래 clamp 로 실제 집행.
        elif settings.rescue_budget_mode == "observe":
            # 반사실 — 집행하지 않되 "narrow 였다면 이랬을 값"을 clamp 후 남긴다.
            # [리뷰 R5] 하한 clamp 뒤 상한(stage_cap)으로 다시 씌운다 — 안 좁힌 것보다 큰 값을
            # "narrow 였다면 이랬을 값"이라고 반사실을 내면 그 자체로 모순이다.
            ms = round(min(stage_cap, max(granted, settings.rescue_stage_min_timeout_s)) * 1000)
            if rescue_stage_narrowed_timeout_ms is None or ms < rescue_stage_narrowed_timeout_ms:
                rescue_stage_narrowed_timeout_ms = ms
            return False, None
        # 여기 도달하면 narrow 를 실제로 집행한다(verdict 는 원래 "narrow"이거나, allow_skip=True
        # + narrow 모드로 강등 실행되는 "skip"이다) — [F1] 항상 최소 하한 이상으로 clamp 한다.
        # [리뷰 R5] 하한 clamp 뒤 상한(stage_cap)으로 다시 씌운다 — 좁히기는 절대 안 좁힌
        # 것보다 많이 주지 않는다(설정과 무관하게 항상 성립해야 하는 지역 불변식).
        granted = min(stage_cap, max(granted, settings.rescue_stage_min_timeout_s))
        ms = round(granted * 1000)
        if rescue_stage_narrowed_timeout_ms is None or ms < rescue_stage_narrowed_timeout_ms:
            rescue_stage_narrowed_timeout_ms = ms
        return False, granted

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
        # [#222] 확장 턴은 카테고리 칩을 내지 않는다 — leg 이 최대 8개라 build_condition_chips 가
        # 칩을 8개 뱉는데, 칩 하나를 지웠을 때 무엇이 빠지는지 사용자가 알 수 없으면 "표시=실제"
        # (#51)가 깨진다. 확장 여부는 확장 고지 token(아래)이 전담한다.
        chip_categories = (
            [] if decision.category_expanded else [c for c, _ in decision.category_legs]
        )
        return build_condition_chips(source, categories=chip_categories)

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
        # [#336] 과소지정 턴은 자동완화·완화칩을 타지 않는다 — 자동완화 probe 는 0건 재검색을
        # 카테고리 없이 부르는데, 그건 이 턴이 되묻기로 처리하려는 바로 그 무필터 검색이다.
        may_auto_relax = (
            not underspecified
            and settings.relaxation_max_rounds > 0
            and any(
                candidate.field in settings.relaxation_auto_fields
                for candidate in build_relaxation_candidates(decision.filters, settings)
            )
        )
    except Exception as exc:  # noqa: BLE001 - 판정 실패가 conditions 를 통째로 막지 않게
        # 이 호출은 conditions 발신 **이전** 경로라, 터지면 이벤트가 하나도 안 나간 채 스트림이
        # 죽는다(`_merge_fanout_results` 와 같은 모양의 실패 — PR #248 리뷰).
        # 미루지 않는 쪽으로 떨군다: 같은 이유로 아래 완화 블록도 실패해 완화가 일어나지 않으므로,
        # 조건 칩을 먼저 내보내도 표시-실제 불일치가 생기지 않는다.
        logger.warning("relaxation_gate_failed", extra={"reason": str(exc)})
        may_auto_relax = False
    # [PR #452 리뷰 R2, #306] `_apply_stage_budget` 의 `attempts` 산출 — `spring_client.
    # search_products` 의 `attempts = spring_max_retries + 1` 과 글자 그대로 같은 규칙이어야
    # 한다(D7). #306 이 미룬 턴 억제를 제거하면서 이 값은 **모든 단에서 동일**해졌다 —
    # 본검색·F-1/#343 재검색·자동완화 probe 가 턴 유형과 무관하게 같은 시도 수를 쓴다
    # (`config.py::_rescue_chain_serial_budget_s` 의 균일식과 동일).
    _search_attempts = settings.spring_max_retries + 1
    # [#393 A] 최종 payload 기준 최소 필터 가드 — **의도 판정이 아니라 payload 사실 판정**이다
    # ("이번 턴이 Spring 쿼리 파라미터 0개로 나가는가"만 본다). no_condition/underspecified 처럼
    # `prior is None`(첫 턴) 에 한정하지 않는다 — 그 둘은 "리파인/칩 제거/카테고리-무관 리셋"
    # 3의도 구분(#84 소관)을 멀티턴에서 함부로 재해석하지 않으려 첫 턴에 한정하는데, A 는 의도를
    # 해석하지 않으므로 그 경계가 적용되지 않는다. 답이 참이면 턴 번호와 무관하게 매칭 전량
    # (운영 실측 7.74초·12.3MB)이 돌아와 SEARCH_FAILED 가 된다 — #393 은 "판정 로직이 또 어긋나도
    # 12MB 를 안 받는 마지막 방어선"을 요구했고, 첫 턴에 한정하면 2턴째부터 그 방어선이 사라진다
    # (되묻기 다음 턴이 실사용에서 실제로 밟는 경로다). no_condition/underspecified 가 이미 캐치한
    # 턴은 제외한다(중복 판정 방지, 아래 로그 사유도 상호배타).
    #
    # [#393 F1] **`is_category_mapping_dropped` 는 여기서 보지 않는다** — A 의 판정 축은
    # "payload 가 비었는가" 하나뿐이고, 매핑 드롭 여부는 무관하다. 매핑 드롭을 A 에서 제외하면
    # 12MB 경로가 그대로 열린다: `cat_signal` 승격(decompose.py:585)은 `semantic_query` 만
    # 채우고 `filters.keyword` 는 채우지 않으므로, 멀티 아이템 턴("이어폰이랑 노트북 추천해줘")
    # 처럼 매핑이 드롭되면서 keyword 도 안 실리는 턴은 payload 가 정확히 0개가 된다. 그 턴을
    # B(아래, "먼저 검색하고 0건일 때만 대체")로 돌리면 **먼저** 무필터 I-1 이 실제로 나가
    # 3초 타임아웃 → SEARCH_FAILED 로 끝난다(B 의 0건 폴백 자리에 도달조차 못 한다) — 이 조합에서
    # #393 이 아무것도 못 고치는 것과 같다. 역할 분담:
    #   payload 0개(매핑 드롭 여부 무관)              → A: I-1 을 아예 안 보내고 I-3(12.3MB 방어선)
    #   payload 에 keyword 만 남음 + 매핑 드롭         → B: 먼저 검색(435 bytes, 관련 결과 우선), 0건이면 I-3
    #   payload 에 실필터(카테고리·브랜드·가격·색) 있음 → 종전 검색 경로 그대로
    unfiltered_bypass = (
        settings.search_filter_guard_enabled
        and not (no_condition or underspecified)
        and is_unfiltered_payload(decision)
    )
    if not may_auto_relax:
        yield sse(
            "conditions",
            ConditionsData(chips=_condition_chips(decision.filters)).model_dump(by_alias=True),
        )

    # [PR #318 리뷰 R14-1] 확장 고지(§ 아래 category_expand_notice 블록)가 mids 를 조립할 때 쓸
    # "실제로 검색한 leg" 인덱스 — `_run_search` 가 이 클로저 변수에 본 검색(§ 아래 nonlocal
    # 대입 조건 참조)의 생존 leg 만 기록한다. None 이면 확장 fan-out 이 아니었거나(단일 filters
    # 검색) 아직 기록되지 않은 것 — 그 경우 고지는 종전대로 `decision.category_legs` 전체를 쓴다.
    expansion_searched_legs: list[int] | None = None

    # [이슈 #168 T1] case 3 다중 leg 턴은 rerank 입력 예산(fan-out 절단 상한)을 니즈 수에
    # 비례시킨다 — 실측(실 카탈로그 leaf 폭 9~17): merge_cap=30 은 5니즈 턴에서 니즈당 6개로
    # 자연 공급량보다 아래를 절단해 per-need expose_max(9)에 도달할 수 없다. 판정 축은 아래
    # `split_by_need`(검색 **후** 확정)와 **동일**해야 한다 — 여기는 검색 전이라 같은 조건식
    # (`case==3 and len(legs)>1`)을 미리 한 번 더 쓴다. 어긋나면 안 되므로 바뀌면 같이 고칠 것.
    # 확장 턴(category_expanded)의 need_count 는 leaf 개수가 아니라 distinct query(원 니즈) 개수다
    # (T3) — leaf 단위로 세면 8-leaf 확장 턴이 실제 니즈(대개 1~2개)보다 훨씬 큰 예산을 받는다.
    # [PR #351 리뷰 R4-1] 이 need_count 계산은 아래 T3(`expansion_grouped_by_need`) 판정과
    # **문자 그대로 같은 식**이어야 한다 — R3-1 이 T3 를 "distinct query 2개 이상 **이고 None 이
    # 안 섞였을 때만** 니즈 단위로 그룹핑"으로 강화했는데, 여기 need_count 는 그 None 예외를
    # 반영하지 않아 두 축이 어긋났었다: query {"A","B",None} 확장 턴은 T3 가 그룹핑을 포기해
    # 목록 1개로 나가는데 T1 은 여전히 need_count=3 으로 예산을 넓혀, 분할되지 않을 턴에 Spring
    # 페이로드·rerank 입력만 낭비했다. **폴백은 `len(legs_preview)`(리뷰어 제안)가 아니라
    # need_count=1 이다** — 확장 턴의 `legs_preview` 는 leaf(최대 8개)라 그걸로 떨어뜨리면
    # 그룹핑을 포기한 턴을 오히려 더 넓히는 반대 방향 오류가 난다. 두 축은 어긋나면 안 되므로
    # 한쪽을 고치면 반드시 같이 고칠 것.
    # **회귀 0의 핵심**: 이 축에 안 걸리는 턴(비-case3·단일 leg)과 3니즈 이하(3×10=30=merge_cap)
    # 턴은 effective_cap 이 정확히 merge_cap — 기존과 동일. 4~5니즈 턴만 40~50 으로 커진다.
    legs_preview = decision.category_legs
    if decision.case == 3 and len(legs_preview) > 1:
        if decision.category_expanded:
            queries = {query for _, query in legs_preview}
            need_count = len(queries) if (len(queries) > 1 and None not in queries) else 1
        else:
            need_count = len(legs_preview)
        effective_cap = max(
            settings.category_fanout_merge_cap,
            min(need_count, MAX_LISTS) * settings.category_group_per_need_candidates,
        )
    else:
        effective_cap = settings.category_fanout_merge_cap

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
            # [#393 A-5] 완화 probe(`base_filters is not None`)가 완화 결과로 payload 를 비우면
            # 본 검색과 같은 12.3MB 무필터 I-1 을 받는다. **probe 호출일 때만** 하드 가드를
            # 건다 — 본 검색(`base_filters is None`)에는 걸지 않는다. 그 경로로 무필터 I-1 이
            # 나가는 경우는 "I-3 가 죽어 폴백한 턴"(`popular_degraded`) 뿐인데, api-spec §4.17
            # 이 그 degrade("500·타임아웃 → 종전 무필터 I-1 검색으로 degrade")를 계약으로
            # 명시한다 — 계약 개정은 사람 승인 게이트라 이 PR 은 그 문장을 지킨다.
            if (
                base_filters is not None
                and settings.search_filter_guard_enabled
                and not spring_client.search_filter_axes(base)
            ):
                logger.info("search_probe_unfiltered_skipped")
                return (ProductSearchResult(products=[], total_count=0), {})
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
        # [이슈 #168 T1] 니즈 수 비례 effective_cap(위 선언) — case3 다중 leg 턴만 merge_cap 을
        # 넘어설 수 있고, 그 외 턴은 effective_cap == merge_cap 이라 종전과 동일하다.
        leg_limit = effective_cap

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
        if base_filters is None:
            # [PR #318 리뷰 R14-1] **본 검색일 때만** 기록한다 — `base_filters is not None` 은
            # #113 자동완화 probe 가 같은 fan-out 의미로 이 함수를 재호출하는 경우인데, 조건 없이
            # 덮어쓰면 probe 재검색의 생존 leg 이 본 검색 결과를 가려 확장 고지가 probe 기준으로
            # 어긋난다(고지는 사용자가 실제로 받은 본 검색 결과를 설명해야 한다).
            nonlocal expansion_searched_legs
            expansion_searched_legs = [i for i, _ in survived]
        if not survived:  # 전량 leg 실패 → SEARCH_FAILED(§6)
            return None
        if len(survived) < len(leg_results):
            if trace := current_request_trace():
                trace.mark_degraded("fanout_partial")
        try:
            return _merge_fanout_results(survived, effective_cap)
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
            ranked_by_profile, profile_reason_by_id, profile_name_by_id = profile_ranked
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
            profile_recommendation_request_id = str(uuid4())  # 정규 UUID 36자 — BE CHAR(36)
            profile_push = RecommendationPush(
                session_id=request.session_id,
                recommendation_request_id=profile_recommendation_request_id,
                list_type="PICK_ONE",
                lists=[profile_entry],
            )
            if notice := _strip_unsafe(settings.no_condition_notice_profile):
                yield sse("token", TokenData(text=notice).model_dump(by_alias=True))
            if settings.progress_events_enabled:
                yield progress_frame("publishing", settings.progress_publishing_message)
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
                # [이슈 #140] provenance — 프로필 벡터 경로는 rerank 를 타지 않으므로 전 항목
                # rankSource="profile_vector", rankerModel/promptVersion 은 null(§0.1 [HARD]).
                profile_reason_pids = {r.product_id for r in profile_entry.reasons}
                emit_recommendation_provenance(
                    logger,
                    settings=settings,
                    request_id=request_id,
                    recommendation_request_id=profile_recommendation_request_id,
                    surface="chat",
                    pipeline="profile_vector",
                    prompt_version=None,
                    ranker_model=None,
                    personalized=True,
                    deterministic=True,
                    list_type="PICK_ONE",
                    owner_fp=safe_fingerprint(identity.subject) if identity is not None else None,
                    session_fp=safe_fingerprint(request.session_id),
                    lists=[
                        ProvenanceList(
                            list_id=profile_entry.list_id,
                            label=None,
                            items=[
                                ProvenanceItem(
                                    product_id=pid,
                                    rank_source="profile_vector",
                                    has_reason=pid in profile_reason_pids,
                                )
                                for pid in exposed
                            ],
                        )
                    ],
                )
                # 담기 해소용 보관 — [#435] 이 경로도 이제 상품명을 싣는다(AI 인덱스에 원본
                # 컬럼은 없지만, 임베딩 입력으로 이미 조립된 `search_doc` 첫 줄에서 최선노력
                # 복원한다 — `no_condition.rank_by_profile` 참조). 추출 실패·노출 집합 내 중복
                # (G2)이면 오늘처럼 빈 이름으로 degrade 하고, id 기반 담기 가드(#118)는 이름
                # 유무와 무관하게 그대로 산다.
                if cart_store is not None and thread_key is not None:
                    # [#435 리뷰 C1] G2 판정 범위를 이번 턴 노출 집합 → **스레드 누적 last_reco
                    # 와의 합집합**으로 넓힌다. 이번 턴만 보면 못 잡는 경우가 남는다 — 이름 없는
                    # 상품(name 미상)은 search_doc 첫 줄이 category 로 밀리는데(예: "생활용품"),
                    # 턴 1 이 [101]→"생활용품"을 유일하게 저장하고 턴 3 이 [202]→"생활용품"도
                    # 그 턴 안에서는 유일해 그대로 저장되면, 누적 `last_reco` 에 다른 productId
                    # 둘이 같은 이름으로 남아 "생활용품 찜해줘"에 LLM 이 임의로 하나를 확정한다
                    # (오확정). 누적 이름은 정상 경로(B, Spring 원본 이름)에서 온 것도 섞여 있어
                    # 그쪽과 겹쳐도 같은 이유로 버린다 — 조회 실패는 이름 보강 없이 degrade한다
                    # (선택 필드, §7 "실패해도 턴을 죽이지 않는다").
                    try:
                        accumulated_names = dict(await cart_store.get_last_reco(thread_key))
                    except Exception as exc:  # noqa: BLE001 - 이름 보강 실패로 담기 흐름을 죽이지 않는다
                        logger.warning(
                            "profile_accumulated_names_failed", extra={"reason": str(exc)}
                        )
                        accumulated_names = {}
                    exposed_names = dedup_exposed_names(
                        exposed, profile_name_by_id, accumulated_names
                    )
                    await cart_store.set_last_reco(
                        thread_key,
                        [(pid, exposed_names.get(pid, "")) for pid in exposed],
                        recommendation_contexts={
                            pid: RecommendationContext(
                                recommendation_request_id=profile_recommendation_request_id,
                                list_id=profile_entry.list_id,
                            )
                            for pid in exposed
                        },
                        # #571 — 프로필 벡터 경로는 항상 목록 1개(`exposed`)라 표시 순서 = 저장
                        # 순서가 성립한다.
                        ordinal_span=len(exposed),
                    )
            else:
                if trace := current_request_trace():
                    trace.mark_degraded("push_skipped")
                if cart_store is not None and thread_key is not None:
                    try:
                        await cart_store.set_push_failed(thread_key)
                    except Exception as exc:  # noqa: BLE001 - 문구 보강 실패로 추천 턴을 죽이지 않는다
                        logger.warning(
                            "push_failed_marker_write_failed", extra={"reason": str(exc)}
                        )
                if push_notice := _strip_unsafe(settings.push_skipped_notice):
                    yield sse("token", TokenData(text=push_notice).model_dump(by_alias=True))
            # [리뷰 F2] 되물음은 **여기**(push 성공/실패 분기 뒤)로 옮겼다 — push 앞에서 내면
            # products.ready 도 카드도 없는데 질문이 먼저 나가 "표시=실제"(#51)가 깨진다. 이
            # 경로는 categoryName 이 없어(AI 카탈로그 인덱스) 성공·실패 둘 다 generic 질문뿐이라
            # (D3 emit 지점 1) 분기마다 다른 문구를 만들 필요는 없다 — 성공 시엔 위 products.ready
            # 뒤, 실패 시엔 위 push_skipped_notice 뒤로 자연히 온다.
            if underspecified and (
                question := _strip_unsafe(settings.underspecified_reask_question)
            ):
                yield sse("token", TokenData(text=question).model_dump(by_alias=True))
            # [#311 리뷰] 하류의 dedup 실패 고지 지점(아래 `if dedup_degraded ...`)에 이 경로는
            # 도달하지 못한다 — 여기서 `done` 을 내고 return 하기 때문이다. 같은 검사를 넣어
            # 경로 간 비대칭을 없앤다. 기본값이 빈 값(미고지)이라 오늘은 아무것도 안 나가지만,
            # 그 값은 **판단을 코드 재배포 없이 되돌리기 위한 스위치**라(#133) 한쪽 경로만
            # 무시하면 켜는 순간 계약이 갈린다.
            if dedup_degraded and (dedup_notice := _strip_unsafe(settings.dedup_skipped_notice)):
                yield sse("token", TokenData(text=dedup_notice).model_dump(by_alias=True))
            yield sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))
            return
        # 랭킹 실패·0건 → 아래 인기 상품(I-3) 경로로 폴백한다(스트림은 계속된다).
        if trace := current_request_trace():
            trace.mark_degraded("profile_ranking_fallback")

    # [#162/#336/#393] 조건이 하나도 없는 턴(no_condition) + 제약만 있는 턴(underspecified) +
    # 최종 payload 가 무필터인 턴(unfiltered_bypass, #393 A)은 후보 소스를 I-3(인기 상품,
    # §4.17)로 바꾼다. `no_condition ⊂ underspecified` 라 후자만 검사하면 되지만, 조건식을
    # 읽는 사람이 판정 관계를 다시 추론하지 않도록 명시한다.
    popular_degraded = False
    # [#393 B] 카테고리 매핑 드롭 턴("신발")이 0건이라 인기 상품으로 대체됐는가 — 고지 분기가
    # 쓴다. `popular_degraded` 와는 상호배타다(이 대체는 성공한 I-3 조회에서만 True 가 된다).
    category_unmapped_zero_result = False

    async def _fetch_popular_candidates() -> tuple[ProductSearchResult, dict[int, int]] | None:
        """I-3 조회 + 가격/평점/속성 사후필터를 적용한다 — no_condition/underspecified/#393
        A·B 가 공유하는 인기 후보 확보 경로다(중복 구현 금지, PR #311/#336 이 지킨 규약).

        실패·조회 결과 없음·필터링 실패는 모두 `None` — 호출부가 폴백을 판단한다. **0건도
        성공이다**(§4.17) — 빈 배열이면 하류 zero-result 경로가 카드 없이 답한다.
        """
        try:
            found = await popular_fn(settings.popular_candidate_size)
        except Exception as exc:  # noqa: BLE001 - 폴백 소스 실패로 스트림을 죽이지 않는다
            logger.warning("popular_products_failed", extra={"reason": str(exc)})
            return None
        if found is None:
            return None
        # [리뷰 R1] 필터링~재조립 전체를 try 로 감싼다 — 이 구간은 `asyncio.gather`(위 호출부)를
        # 타는 코루틴 안이라, 여기서 예외가 새면 제너레이터가 done 도 error 도 없이 그대로
        # 죽는다(`no_condition.rank_by_profile` [PR #311 리뷰] 주석이 같은 파일에서 재현 확인한
        # 바로 그 실패 모양). §7 원칙("실패해도 턴을 죽이지 않는다")에 맞춰 실패 시 `None` 을
        # 돌려줘 호출부가 무필터 `_run_search` 등으로 합류시킨다 — I-3 자체 실패와 같은 처치다.
        try:
            # [#311 리뷰] 총액 예산을 말한 턴은 **예산 안의 후보만** 남긴다. 세트로 묶지 않고
            # 대안으로 보여주므로 상품 하나가 예산 이하이면 된다 — 무엇을 몇 개 살지 사용자가
            # 말하지 않은 턴에 조합을 지어내면 근거 없는 세트가 된다(`has_total_budget` 참조).
            products = found.products
            if decision.total_budget is not None:
                products = within_budget(products, decision.total_budget)
            # [#336] 상품당 가격 제약(priceMin/priceMax) — 총액 예산과 별개 축이라 함께 적용될
            # 수 있다. 둘 다 없으면 `within_price_range` 는 사본만 돌려줘 순서·내용이 그대로다.
            products = within_price_range(
                products, decision.filters.price_min, decision.filters.price_max
            )
            # [#393 C] rating_min·attr_conditions 는 Spring payload 축이 아니라 AI 사후필터다
            # (search_service.search_catalog 과 같은 함수 공유) — 인기 후보에 안 걸면 조건
            # 칩엔 "평점 4.0 이상"이 떠 있는데 후보는 그 조건을 안 지키는 표시-실제 불일치가
            # 된다. 이 필터가 새로 태우는 턴은 A(unfiltered_bypass)가 인기 상품으로 돌린,
            # rating_min·attr_conditions 만 있는 턴이다 — 그 외(no_condition/underspecified)
            # 턴은 이 두 축이 정의상 비어 있어 no-op 이다.
            products = apply_ai_side_filters(products, decision.filters)
            if len(products) != len(found.products):
                found = ProductSearchResult(products=products, total_count=len(products))
            return (found, {})
        except Exception as exc:  # noqa: BLE001 - 필터링 실패로 스트림을 죽이지 않는다
            logger.warning("popular_candidate_filter_failed", extra={"reason": str(exc)})
            return None

    async def _run_candidate_source():
        """이번 턴의 후보를 확보한다 — `_run_search` 와 같은 `(결과, leg맵)` 형태로 돌려준다.

        조건 없는 턴에 종전 경로를 그대로 태우면 파라미터 0개의 I-1 이 나가 매칭 전량
        (실측 7,245건·13.33MB)을 받는데, 그 상위는 사용자 의도와 무관하다. I-1 정본이
        "정형조건 없는 요청 차단은 LLM 단 책임"으로 규정한 자리다(§4.6·§4.17).

        [#336] 가격 제약만 있는 턴(예: "5만원 이하로 아무거나")도 같은 이유로 여기로 온다 —
        price 파라미터만 실린 I-1 은 여전히 매칭 수천 건을 돌려주고(무필터 실측의 아류),
        semantic 정렬 입력이 발화 원문 폴백이라 상위가 의도와 무관하다. I-3 는 price 가 있어
        예산·가격 준수를 입증할 수 있다(#162 와 같은 논리, SPEC-UNDERSPECIFIED-336 §3).

        [#393 A] `rating_min`·`attr_conditions` 만 있는 턴처럼 no_condition/underspecified
        축엔 안 걸리지만 최종 payload 는 파라미터 0개(운영 실측 12.3MB)인 턴도 여기로 온다
        (`unfiltered_bypass`). A 는 `may_auto_relax` 게이트가 **없다** — 검색을 대체하는
        것이지 추가하는 것이 아니라서다(무필터 `_run_search()` 호출 자체가 안 나간다), 그래서
        first-token 예산이 늘지 않는다. 아래 B 의 `not may_auto_relax` 게이트와 대비된다.

        [#393 B] 사용자가 카테고리를 지목했는데 매핑이 leg 를 하나도 못 낸 턴("신발 추천해줘")은
        A 에 걸리지 않는다(대개 keyword 는 남는다 — 실측 435 bytes). 이 턴은 **사전 우회가
        아니라 사후 폴백**이다 — keyword 검색이 실제로 결과를 내면 인기 상품보다 관련성이 높으므로
        먼저 검색하고, **0건일 때만** 인기 후보로 대체한다. B 는 A 와 달리 검색을 **대체하지
        않고 추가**한다(0건 확인 후 I-3 를 한 번 더 부른다) — 그래서 `not may_auto_relax` 로
        제한한다. 이유: `may_auto_relax` 턴은 `conditions` 를 검색 뒤로 미루므로(#113·#277)
        첫 이벤트 앞에 직렬 Spring 호출이 하나 더 붙으면(decompose head + 검색 + I-3 + 완화 probe)
        first-token 10s 상한을 넘길 수 있다(#277 이 고친 바로 그 실패 모양) — 미루지 않는
        턴은 `conditions` 가 이미 나가 관문을 통과한 뒤라 안전하다.

        [PR #411 Claude 리뷰] payload 축이 `keyword`/가격(`is_popular_fallback_safe`) 밖이면
        B 를 발동하지 않는다 — `brand=["나이키"]` 처럼 인기 후보가 걸러주지 못하는 축이 남아
        있으면 대체 후 `conditions` 칩("나이키")과 실제 후보가 어긋난다. 그 경우는 종전 동작
        (0건 응답)을 그대로 둔다.

        leg 맵은 빈 dict 다 — 인기 목록은 카테고리 fan-out 이 아니라 단일 목록이다.
        """
        nonlocal popular_degraded, category_unmapped_zero_result
        if not (no_condition or underspecified or unfiltered_bypass):
            found = await _run_search()
            if (
                settings.search_filter_guard_enabled
                and not may_auto_relax
                and is_category_mapping_dropped(decision)
                # [PR #411 Claude 리뷰] payload 축이 keyword/가격 밖이면(brand·color 등) B 를
                # 발동하지 않는다 — 인기 후보는 그 축을 걸러주지 않는데 `conditions` 칩은
                # `decision.filters` 그대로 파생돼(state.build_condition_chips) 표시-실제가
                # 어긋난다("나이키" 칩이 뜬 채 나이키 아닌 상품이 나간다). 자세한 근거는
                # `search_guard._POPULAR_SAFE_AXES` 참조.
                and is_popular_fallback_safe(decision)
                and found is not None
                and not found[0].products
            ):
                fallback = await _fetch_popular_candidates()
                if fallback is not None:
                    category_unmapped_zero_result = True
                    # PII 금지(#119) — 카테고리·발화 문자열은 싣지 않는다, 사유 라벨만.
                    logger.info(
                        "search_candidate_source_bypass",
                        extra={"reason": "category_unmapped_zero_result"},
                    )
                    return fallback
                # I-3 도 실패·0건이면 원래의 0건 검색 결과를 그대로 돌려준다(스트림을 죽이지
                # 않는다 — 재검색으로 무필터 `_run_search` 를 다시 부르지 않는다).
            return found

        logger.info(
            "search_candidate_source_bypass",
            extra={
                "reason": "no_condition"
                if no_condition
                else ("underspecified" if underspecified else "unfiltered_payload")
            },
        )
        fallback = await _fetch_popular_candidates()
        if fallback is not None:
            return fallback
        popular_degraded = True
        if trace := current_request_trace():
            trace.mark_degraded("popular_fallback")
        return await _run_search()

    # [#396] `no_condition`/`underspecified`/`unfiltered_bypass`(#393) 턴은
    # `_run_candidate_source` 가 I-1 이 아니라 I-3(인기 목록)을 타지만, 사용자에게는 어느
    # 쪽이든 "상품을 찾는 중"이라 같은 stage 를 쓴다 — 후보 소스가 무엇이든 사용자가
    # 기다리는 이유는 하나다.
    if settings.progress_events_enabled:
        yield progress_frame("searching", settings.progress_searching_message)
    # [#306] 미룬 턴만 재시도를 끄던 #277 의 응급 처치는 제거됐다 — 그 근거였던 first-token
    # 관문은 #396 이 `progress` 를 검색 앞으로 보내며 이 체인 밖으로 나갔다. 이제 이 단도 다른
    # 단과 같은 `_search_attempts` 를 쓰고, 폭주 방지는 아래 `_apply_stage_budget`(#427 D4)의
    # 런타임 좁히기가 맡는다.
    # [#427 D4] 본검색 — 절대 건너뛰지 않는다(allow_skip=False). 남은 단 수는 본검색 자신(1) +
    # 이 턴에 이론상 남은 구제 체인(rescue·auto_relax)이다.
    # [PR #452 리뷰 R3] `_rescue_stage_counts.main` 은 이제 물리적 사실이라 항상 1 이다(#427
    # D6 — 본검색은 `may_auto_relax` 와 무관하게 항상 돈다) — `max(..., 1)` 보정은 더 이상
    # 필요 없다(옛 `_rescue_chain_stage_counts` 가 미룸 게이트로 조기 return 하던 시절의
    # 방어였다). 그대로 두면 `RELAXATION_MAX_ROUNDS=0` 턴에서 F-1 몫을 셈에서 빠뜨린다.
    # allow_skip=False 라 반환되는 skip 플래그는 항상 False — 버린다.
    _, _main_narrow_budget = _apply_stage_budget(
        _rescue_stage_counts.main + _rescue_stage_counts.rescue + _rescue_stage_counts.auto_relax,
        allow_skip=False,
        attempts=_search_attempts,
    )
    _main_narrow_cm = (
        spring_client.narrow_search_budget(_main_narrow_budget)
        if _main_narrow_budget is not None
        else nullcontext()
    )

    async def _collect_main_search():
        """본검색과 구매 이력의 기존 병렬 수집을 한 곳에 고정한다 (#406 D1)."""
        return await asyncio.gather(_run_candidate_source(), _fetch_purchases_once())

    retry_progress_possible = settings.progress_events_enabled and settings.spring_max_retries > 0
    # [#406 D1] 기본값은 재시도를 켜므로 이 경로가 기본이다. 재시도를 끈 배포(`SPRING_MAX_
    # RETRIES=0`)에서는 기존 인라인 경로를 유지해 취소·ContextVar·trace 의미를 불필요하게
    # 바꾸지 않는다. [#306] 종전에는 미룬 턴 억제도 이 게이트를 닫았으나 그 억제가 사라져
    # 재시도 설정만 남았다 — 미룬 턴도 이제 드레인 경로를 타고 `retrying` 을 낸다.
    if not retry_progress_possible:
        with _main_narrow_cm:
            search_bundle, purchases = await _collect_main_search()
    else:
        queue: asyncio.Queue[object] = asyncio.Queue()
        # [#406 D3] create_task가 생성 시점 ContextVar를 복사하므로 with는 즉시 닫는다. yield를
        # 안에 두면 observer·budget이 다음 턴으로 새며, 관측 콜백은 항목 sentinel을
        # 넣어 queue.put_nowait의 인자 계약도 보존한다.
        with (
            _main_narrow_cm,
            spring_client.observe_search_retry(lambda: queue.put_nowait(_SEARCH_RETRY)),
        ):
            search_task = asyncio.create_task(_collect_main_search())
        search_task.add_done_callback(lambda _task: queue.put_nowait(_SEARCH_DONE))
        retrying_progress_emitted = False
        try:
            while True:
                item = await queue.get()
                if item is _SEARCH_DONE:
                    break
                if not retrying_progress_emitted:
                    retrying_progress_emitted = True
                    yield progress_frame("retrying", settings.progress_retrying_message)
            # [#406 D3] 예외·반환 위치는 기존 gather await와 같게 정상 회수에서만 유지한다.
            search_bundle, purchases = await search_task
        finally:
            # [#406/#84] 값이 필요 없는 정리는 동기 cancel만 한다. 여기서 await하면 외부 취소를
            # 삼켜 끊긴 스트림이 계속 진행할 수 있다.
            if not search_task.done():
                search_task.cancel()
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

    # [#222 F-1] 확장 fan-out 이 0건이면 카테고리 없이 1회만 재검색한다. 확장 leg 은 거리컷이
    # "맞는 칸이 없다"고 이미 판정해 버린 후보라, 8개가 전부 빗나가면 Spring 이 leg 마다 0건을
    # 내고 `_merge_fanout_results` 도 정상적으로 빈 결과를 병합해 낸다(위 `search_bundle is None`
    # 분기는 **전량 leg 실패** 만 잡으므로 여기서는 걸리지 않는다). 확장 이전엔 같은 발화가
    # `legs=[]` → 카테고리 무필터 검색으로 결과가 나왔으므로, 이대로 두면 이 PR 이 "결과 있음"을
    # "0건"으로 바꾸는 회귀가 된다(이슈 #222 ⑤). `relaxation` 은 category 를 완화 대상으로 다루지
    # 않아(`RELAXATION_FIELD_TO_ATTR` 에 없음, relaxation.py:87) 이 경로를 구제하지 못한다.
    # **확장 턴에만** 적용한다 — 일반 fan-out(사용자가 명시한 카테고리)의 0건은 종전대로 둔다.
    # 조용히 카테고리를 풀면 "표시=실제"(#51)가 깨지고 이 PR 의 범위 밖이다.
    # [PR #318 리뷰 R6-4] `search_result.total_count` 는 **최근구매 억제(exact 제외·소모품
    # 카테고리 억제) 이전** 값이다(그 억제는 이 지점 아래 `_post_filter` 가 한다). 검색이 결과를
    # 냈는데 그 전량이 억제돼 `candidates` 가 0이 되는 턴은 이 폴백이 트리거되지 않는다. 이
    # 실패 모드 자체는 이 PR 이전부터 있던 기존 갭이지만, **이 PR 이 그 발생 확률을 구조적으로
    # 높인다** — 후보 풀을 무필터에서 앵커 근방 leaf 8개로 좁히면 최근구매·소모품 억제와 겹칠
    # 사전 확률이 올라간다("기존 갭이라 무관"이 아니라 "기존 갭인데 이 PR 이 노출 확률을 올린다").
    # 그래서 후속 이슈는 이 PR 이 확률을 올린 갭의 후속으로 우선순위를 매겨야 한다. 억제 이후
    # 기준으로 다시 판정하려면 폴백 재검색 뒤 억제를 다시 돌려야 해서(순서·중복 억제 문제가
    # 새로 생긴다) — 억제-후 재판정은 아래 [#343] 블록(`candidates = result.products` 직후)이
    # 담당한다(갭 실재는 PR #318 리뷰에서 재현·확정했고, 운영 실빈도는 아직 미확인이다 — 아래
    # `recommend_zero_result` 로그의 had_candidates=True & category_expanded=True 조합으로
    # 배포 후 후속 관측한다).
    category_expand_notice_suppressed = False
    # [#363] F-1 폴백과 #343 재판정이 쓴 무필터 재검색 왕복의 누적 소요 — 시도했으나 구제
    # 실패로 0건 종결된 턴에서도 남아야 아래 `recommend_zero_result` 로그로 최악 경로 지연을
    # 관측할 수 있다(시도가 없으면 0). 성공 시 각 이벤트 로그의 `elapsed_ms` 는 그 왕복 1회분,
    # 이 변수는 한 턴에 F-1+#343 이 함께 시도됐을 경우의 합이다(상호배타 가드로 실질 1회분).
    rescue_elapsed_ms = 0

    async def _run_search_unfiltered() -> tuple[ProductSearchResult, dict[int, int]] | None:
        """카테고리를 빼고 1회만 재검색한다 — 비-fan-out 단일검색과 같은 계약(§6 `_run_search`
        의 `if not legs` 분기와 동일 처리, leg_of 는 leg 개념이 없으니 빈 dict).

        [#343] 억제-이전 F-1 폴백과 억제-이후 재판정이 함께 쓴다 — 지역 함수를 두 곳에 각자
        정의하면 같은 판정 개념이 한쪽만 고쳐지는 드리프트가 생긴다(lessons).

        [PR #411 Claude 리뷰 2라운드] `_run_search` 의 probe 가드(위, `search_probe_unfiltered_
        skipped`)와 같은 성질의 누락이었다 — `category_expanded` 턴은 `category_legs` 가 차 있어
        `is_unfiltered_payload` 가 조기에 False 라 A 가 이 재검색을 보호하지 못했다. 확장 턴은
        `filters.category` 가 이미 None 이므로(PR #318 R6-1) 사용자가 다른 축을 안 준 순수
        카테고리 발화면 이 재검색 payload 가 파라미터 0개 — 이 PR 이 막으려는 바로 그 12.3MB
        무필터 I-1 이다. **운영에서는 결과가 같고 3초 빠르다**: 그 무필터 재검색은 오늘 실측상
        7.74초·12.3MB 라 AI 3초 타임아웃에 걸려 `search` 가 예외를 내고, 이 함수는 그 예외를
        삼켜 어차피 `None`(원래 0건 유지)으로 끝난다 — 이 가드는 결과를 바꾸지 않고 Spring 에
        무거운 쿼리를 안 보내고 사용자 대기를 3초 줄인다. 카탈로그가 작을 때만 결과가 갈리며
        (그때는 F-1 구제가 성공한다), 그 경우를 위해 `search_filter_guard_enabled=False` 롤백이
        있다. 로그 이름은 probe 가드와 구분한다 — 어느 경로가 얼마나 막히는지 관측에서 갈려야
        한다."""
        unfiltered = decision.filters.model_copy(update={"category": None})
        if settings.search_filter_guard_enabled and not spring_client.search_filter_axes(
            unfiltered
        ):
            logger.info("search_rescue_unfiltered_skipped")
            return None
        try:
            found = await search(unfiltered, exclude_product_ids=None)
            return (found, {}) if found is not None else None
        except SpringUnavailableError:
            return None
        except Exception as exc:  # noqa: BLE001 - 재검색 실패는 원래(0건) 결과를 유지
            logger.warning("category_expand_zero_fallback_failed", extra={"reason": str(exc)})
            return None

    if decision.category_expanded and search_result.total_count == 0:
        # [#427 D4] F-1 구제 재검색 — 남은 단은 이 단(1) + 이 턴에 이론상 남은 자동완화 단.
        # narrow_skip 모드에서만 실제로 건너뛴다(allow_skip=True).
        # [PR #452 리뷰 R2, #306] 다른 단과 같은 시도 수를 쓴다 — attempts=_search_attempts.
        _f1_skip, _f1_narrow_budget = _apply_stage_budget(
            1 + _rescue_stage_counts.auto_relax, allow_skip=True, attempts=_search_attempts
        )
        if _f1_skip:
            logger.info("rescue_stage_skipped_budget", extra={"stage": "f1_fallback"})
            fallback_bundle = None
        else:
            _f1_narrow_cm = (
                spring_client.narrow_search_budget(_f1_narrow_budget)
                if _f1_narrow_budget is not None
                else nullcontext()
            )
            _f1_fallback_started_at = time.monotonic()
            with _f1_narrow_cm:
                fallback_bundle = await _run_search_unfiltered()
            _f1_fallback_elapsed_ms = round((time.monotonic() - _f1_fallback_started_at) * 1000)
            rescue_elapsed_ms += _f1_fallback_elapsed_ms
            if fallback_bundle is not None:
                search_result, leg_of = fallback_bundle
                # 무필터로 실제 되돌아갔으니 "중분류를 훑었다"는 확장 고지는 이제 거짓 고지다 —
                # `decision.category_expanded` 자체는 건드리지 않는다(칩 억제는 그대로 유지해야
                # 한다 — 실제로 안 쓴 확장 leg 8개를 카테고리 칩으로 보여주면 더 큰 거짓말이 된다).
                category_expand_notice_suppressed = True
                log_structured(
                    logger,
                    "category_expand_zero_fallback",
                    **{
                        "legs": len(decision.category_legs),
                        "elapsed_ms": _f1_fallback_elapsed_ms,
                    },
                )

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
        # [이슈 #168 T1 리뷰 R2-1] ①②(leg_limit·merge cap)가 effective_cap 으로 넓힌 폭이
        # 여기서 도로 embedding_rerank_limit 으로 잘리면 T1 이 무의미해진다 — 단 **effective_cap
        # 이 실제로 merge_cap 을 넘어선 턴(=4니즈 이상 case3 분할 턴)에만** 그 넓힌 값을 하한으로
        # 쓴다. 조건 없이 `max` 를 걸면 이 슬라이스 자체의 하한이 조용히 merge_cap 으로 올라가,
        # 운영자가 `embedding_rerank_limit` 을 merge_cap 미만(토큰 절감 튜닝)으로 낮춘 배포에서
        # T1 과 무관한 비분할·3니즈 이하 턴까지 rerank 입력이 커진다(#222 패킷이 경고한 유형의
        # 설정 조합 회귀, PR #168 리뷰 R2-1). `merged` 는 round-robin 인터리브(①②)라 넓어졌을
        # 때의 flat 슬라이스([:N])도 니즈별 공평성이 보존된다(한 니즈가 앞을 독점해 잘리는 게
        # 아니다).
        rerank_input_limit = (
            max(settings.embedding_rerank_limit, effective_cap)
            if effective_cap > settings.category_fanout_merge_cap
            else settings.embedding_rerank_limit
        )
        return (
            ProductSearchResult(products=survivors[:rerank_input_limit], total_count=matched),
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

    # [#343] 억제-후 재판정 — 검색은 히트를 냈는데 위 `_post_filter`(최근구매 exact 제외 + 소모품
    # 카테고리 억제)가 전량을 지워 candidates 가 0이 된 확장 턴을 무필터 재검색으로 구제한다.
    # 위 F-1(883행)은 억제 **이전** `search_result.total_count` 만 보므로 이 갭을 못 잡는다
    # (PR #318 리뷰 R6-4, #222 가 후보 풀을 앵커 근방 leaf 8개로 좁혀 이 사전 확률을 구조적으로
    # 높였다). `not category_expand_notice_suppressed` 는 상호배타 가드다 — 억제-이전 F-1 이 이미
    # 무필터 재검색 1회를 써서 채택했으면 여기서 또 돌지 않는다(재검색은 결정론적이라 같은 쿼리를
    # 두 번 돌려도 결과가 같으므로 2회 이상 시도는 무의미하고, 이 가드로 턴당 무필터 재검색
    # 왕복은 최대 1회로 고정된다 — Spring 3s 타임아웃 1회분, 구매자 스트림 30s 예산 §2.9 안).
    post_suppress_fallback_attempted = False
    if (
        settings.category_expand_post_suppress_fallback_enabled
        and decision.category_expanded
        and not candidates
        and had_candidates
        and not category_expand_notice_suppressed
    ):
        # [#427 D4] #343 억제-후 재판정 — F-1 과 상호배타(위 가드)이므로 "남은 단"은 F-1 과
        # 같은 식(이 단(1) + 이 턴에 이론상 남은 자동완화 단)이다.
        # [PR #452 리뷰 R2, #306] F-1 과 같은 시도 수를 쓴다 — attempts=_search_attempts.
        _post_suppress_skip, _post_suppress_narrow_budget = _apply_stage_budget(
            1 + _rescue_stage_counts.auto_relax, allow_skip=True, attempts=_search_attempts
        )
        if _post_suppress_skip:
            logger.info("rescue_stage_skipped_budget", extra={"stage": "post_suppress_fallback"})
        else:
            post_suppress_fallback_attempted = True
            _post_suppress_narrow_cm = (
                spring_client.narrow_search_budget(_post_suppress_narrow_budget)
                if _post_suppress_narrow_budget is not None
                else nullcontext()
            )
            _post_suppress_fallback_started_at = time.monotonic()
            with _post_suppress_narrow_cm:
                fallback_bundle = await _run_search_unfiltered()
            if fallback_bundle is not None:
                refetched, _ = fallback_bundle  # leg_of 는 이 함수 계약상 항상 빈 dict
                # [#363 R4] `_post_filter` 가 성공한 시점의 소요를 여기 잡아 두고, 아래 `finally`에서
                # **정확히 1회만** `rescue_elapsed_ms`에 더한다 — `_post_filter` 성공 뒤(상태 대입·
                # `logger.info`) 코드가 나중에 예외를 내도 이중 계상되지 않도록, try 본문 안에서
                # 직접 누적하지 않는다.
                _post_suppress_fallback_elapsed_ms: int | None = None
                # 후보는 이미 0건으로 확정돼 있고 이 재판정은 구제라는 **부가 기능**이다 — 첫 번째
                # `_post_filter(search_result)`(994행)와 달리 여기서 예외가 나도 후보 확정 자체는
                # 이미 끝나 있어 raise 할 이유가 없다. 감싸지 않으면 재적용(`_post_filter` 자체는
                # 이미 자기 예외를 삼키는 `_run_search_unfiltered` 와 달리 무방어라 이 try 의 주 보호
                # 대상이다)이 실패한 순간 conditions 도 zero_result 안내도 없이 스트림이 죽는다 —
                # `_probe`·relaxation 루프와 같은 "부가 기능 실패가 턴을 죽이지 않는다"(§7) 원칙.
                try:
                    (
                        refiltered,
                        refiltered_suppressed_by_cat,
                        refiltered_received,
                        refiltered_had_candidates,
                    ) = _post_filter(refetched)
                    # [#363] 재검색 + `_post_filter` 재적용까지를 이 블록의 소요로 잰다 — 후속
                    # `_post_filter` 가 재검색 왕복만큼 무겁지 않다는 보장이 없어 왕복만 재면 과소
                    # 계상된다.
                    _post_suppress_fallback_elapsed_ms = round(
                        (time.monotonic() - _post_suppress_fallback_started_at) * 1000
                    )
                    if refiltered.products:
                        result = refiltered
                        suppressed_by_cat = refiltered_suppressed_by_cat
                        received = refiltered_received
                        had_candidates = refiltered_had_candidates
                        candidates = result.products
                        leg_of = {}  # leg 개념 없음 — 기존 F-1 과 동일 규약, split_by_need 자연 차단
                        # 무필터로 실제 찾았으니 "중분류를 훑었다"는 확장 고지는 이제 거짓 고지다
                        # (F-1 과 같은 원칙, 883행 참조). `decision.category_expanded` 자체는
                        # 건드리지 않는다.
                        category_expand_notice_suppressed = True
                        log_structured(
                            logger,
                            "category_expand_post_suppress_fallback",
                            **{
                                "legs": len(decision.category_legs),
                                "elapsed_ms": _post_suppress_fallback_elapsed_ms,
                            },
                        )
                    # 재적용 후에도 0건이면 위에서 result·suppressed_by_cat·candidates 를 갱신하지
                    # 않았으므로 원래(억제된) 상태가 그대로 유지된다 — 되돌리기 칩·안내 문구는 원래
                    # 억제 기준으로 조립돼야 한다(재검색분으로 교체하면 안 된다).
                except Exception as exc:  # noqa: BLE001 - 재판정 실패는 원래(억제된) 상태를 유지
                    # CancelledError(BaseException)는 전파돼 협조적 취소가 보존된다.
                    logger.warning(
                        "category_expand_post_suppress_fallback_failed", extra={"reason": str(exc)}
                    )
                finally:
                    # [#363 R4] 성공(사전 계산값 재사용) · `_post_filter` 자체 실패(여기서 재계산) ·
                    # 성공 후 늦은 예외(사전 계산값 재사용, 재계산해 이중으로 재지 않는다) 세 경로 전부
                    # 정확히 1회만 더한다.
                    rescue_elapsed_ms += (
                        _post_suppress_fallback_elapsed_ms
                        if _post_suppress_fallback_elapsed_ms is not None
                        else round((time.monotonic() - _post_suppress_fallback_started_at) * 1000)
                    )
            else:
                # fallback_bundle 이 None(재검색 실패)이어도 같은 이유로 원래 상태를 그대로 둔다 —
                # 시도 자체는 있었으니 [#363] 소요는 남긴다.
                rescue_elapsed_ms += round(
                    (time.monotonic() - _post_suppress_fallback_started_at) * 1000
                )

    # ── 0건/소량 조건 완화 (#113, api-spec §3.1 · SPEC-RECOMMEND-001 §6.6) ──
    # estCount 는 page-local 로 못 구한다 — priceMax·brand·color 는 Spring I-1 쿼리 파라미터라 탈락
    # 상품이 응답에 아예 없다(schemas/spring.py ProductSearchResult docstring). 그래서 완화 필터로
    # 재검색(probe)해 실제 매칭 수를 센다.
    #
    # **estCount 가 정확하다는 전제 = I-1 전량 반환**(api-spec §4.6, 2026-07-23 BE 합의로 size 제거,
    # #395 2026-08-07 로 재도입 요청 자체가 폐지 확정됐다 — 다만 이는 AI 쪽 재요청 폐지일 뿐, BE 가
    # 단독으로 상한을 다시 넣을 가능성까지 배제하지는 않는다).
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
    # [#363 R3] 자동완화 루프와 칩 probe는 first SSE(conditions) **기준으로 서로 다른 쪽**에
    # 있다 — `may_auto_relax=True`인 턴은 conditions를 자동완화 루프 직후·칩 probe **이전**에
    # 내보낸다(아래 `if may_auto_relax: yield sse("conditions", ...)`). 한 필드에 합치면 칩
    # probe 몫이 섞여 들어와 first-token 지연을 과대계상하므로 반드시 나눈다 — first-token
    # 지연 판정 기준은 `rescue_elapsed_ms + relax_auto_elapsed_ms`만 봐야 한다(chip 몫 제외,
    # 근거는 docs/specs/MEASURE-FIRST-TOKEN-363.md).
    relax_auto_elapsed_ms = 0  # 자동완화 루프 소요 — first SSE 이전
    relax_chip_elapsed_ms = 0  # 완화 칩 probe 소요 — first SSE 이후

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

    # [#336] 과소지정 턴은 자동완화 루프 자체를 태우지 않는다 — `may_auto_relax`(위 §)는 이
    # 턴에서 이미 False 로 잠겨 있지만, 이 루프는 `may_auto_relax` 를 보지 않고 `not candidates`
    # 만으로 도는 별개 게이트라 명시적으로 한 번 더 막는다(안 막으면 가격 폭을 넓힌 재검색이
    # I-1 로 나가 "카테고리 없이 되묻는다"는 이 이슈의 취지를 어긴다).
    if not candidates and not underspecified:
        # [PR #248 2차 리뷰] 루프 전체를 감싼다 — `_probe` 안쪽은 이미 방어하지만 후보 생성
        # (`build_relaxation_candidates`)과 루프 자체는 밖이었다. 여기서 터지면 아래 conditions
        # 발신까지 못 가 조건 칩이 사라진다. 자동 완화는 **선택 기능**이라 실패는 삼키고
        # 완화 없이 계속한다(§7) — 0건 안내와 완화 칩 경로는 그대로 살아 있다.
        _auto_relax_started_at = time.monotonic()
        # [#396] "완화 중" 은 probe 를 **실제로** 부를 때만 정직하다 — 루프 진입만으로 내면
        # auto_fields 필터에 다 걸려 probe 가 0회인 턴에도 뜬다(거짓 신호). 그래서 진입 시점이
        # 아니라 첫 probe 직전에, 지역 플래그로 딱 1회만 낸다.
        relaxing_progress_emitted = False
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
                # [#427 D4·H4] 예산 판정을 (2)rounds+=1 과 (3)relaxing emit **사이**에 넣는다 —
                # 예산 부족으로 이 라운드를 건너뛰면 relaxing 을 emit 하지 않는다(거짓 신호
                # 금지). 남은 단 수는 이 단을 포함해 아직 남은 자동완화 라운드 수다.
                _auto_relax_stages_left = max(_rescue_stage_counts.auto_relax - rounds + 1, 1)
                # [PR #452 리뷰 R2, #306] 본검색과 같은 시도 수 — attempts=_search_attempts.
                _relax_skip, _relax_narrow_budget = _apply_stage_budget(
                    _auto_relax_stages_left, allow_skip=True, attempts=_search_attempts
                )
                if _relax_skip:
                    logger.info(
                        "rescue_stage_skipped_budget",
                        extra={"stage": "auto_relax", "round": rounds},
                    )
                    continue  # relaxing 을 emit 하지 않고 다음 후보로 — H4
                if settings.progress_events_enabled and not relaxing_progress_emitted:
                    relaxing_progress_emitted = True
                    yield progress_frame("relaxing", settings.progress_relaxing_message)
                _relax_narrow_cm = (
                    spring_client.narrow_search_budget(_relax_narrow_budget)
                    if _relax_narrow_budget is not None
                    else nullcontext()
                )
                with _relax_narrow_cm:
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
        # [#363] 성공·예외 어느 쪽이든 이 루프가 돈 만큼은 소요다.
        relax_auto_elapsed_ms += round((time.monotonic() - _auto_relax_started_at) * 1000)
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
    # [#336] 과소지정 턴은 이 probe 도 태우지 않는다(위 자동완화 루프와 같은 이유) — 완화 칩은
    # `may_auto_relax`(auto_fields 화이트리스트)와 무관하게 filters 에 설정된 모든 축(가격 포함)
    # 에서 후보를 만들어 실제로 I-1 을 부른다.
    relaxation_chips: list[SuggestionChip] = []
    if not underspecified and (not candidates or len(candidates) < settings.relaxation_min_results):
        # 자동 완화 루프와 같은 이유로 전체를 감싼다(PR #248 2차 리뷰) — 후보 생성·칩 조립은
        # `_probe` 바깥이라 방어가 없었다. 완화 칩은 **부가 제안**이라 실패하면 칩 없이 계속한다.
        _chip_probe_started_at = time.monotonic()
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
        # [#363] 성공·예외 어느 쪽이든 이 블록이 쓴 만큼은 소요다.
        relax_chip_elapsed_ms += round((time.monotonic() - _chip_probe_started_at) * 1000)

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
        # [#336] 과소지정 턴의 0건 경로 — 카드가 없으니 노출 후보가 없다(D3 emit 지점 3,
        # generic 질문만). "카드 없는 답 + 되물음"으로 다음 턴에 카테고리를 지목할 실마리를 준다.
        if underspecified and (question := _strip_unsafe(settings.underspecified_reask_question)):
            yield sse("token", TokenData(text=question).model_dump(by_alias=True))
        # 전부 억제됐어도 되돌리기 칩은 준다(사용자가 복원 가능) + 0건 완화 칩(#113).
        if zero_chips := revert_chips + relaxation_chips:
            yield sse("suggestions", SuggestionsData(chips=zero_chips).model_dump(by_alias=True))
        yield sse(
            "done",
            DoneData(finish_reason="zero_result").model_dump(by_alias=True),
        )
        # [PR #318 리뷰 R11-1] 0건 턴은 위 zero_result 분기가 곧장 return 해 아래
        # `recommend_pipeline` 구조화 로그(§)까지 못 간다 — 원인별 빈도(특히 검색은 히트가
        # 있었는데 하류 억제가 전량을 지운 케이스, #343 갭)를 잴 수단이 없어 여기 별도로 남긴다.
        # PII 금지: 카테고리 문자열·상품 id 는 싣지 않고 개수만 싣는다.
        log_structured(
            logger,
            "recommend_zero_result",
            **{
                # False = 검색 자체 0건 / True = 히트는 있었는데 하류 억제가 전량을 지움
                "had_candidates": had_candidates,
                "suppressed_categories": len(suppressed_by_cat),
                # 확장 턴 여부 — #222 가 발생 확률을 높인 갭(F-1 미구제)의 빈도를 이 조합
                # (had_candidates=True & category_expanded=True)으로 관측한다(PR #318 리뷰).
                "category_expanded": decision.category_expanded,
                # [#343] 위 억제-후 재판정을 시도했는지 — 수정 후에도 이 조합이 남을 때 "폴백을
                # 시도했는데도 정말 0건"인지 "플래그 off 로 갭이 그대로"인지 로그로 갈라야 한다.
                "post_suppress_fallback_attempted": post_suppress_fallback_attempted,
                # [#363] 이 턴이 F-1/#343 구제·완화(자동+칩 probe)에 쓴 소요(ms) — 시도 없으면
                # 0. 이슈 #363 이 실측 불가로 판정한 지연을 배포 후 운영 로그만으로 관측하기
                # 위한 계측(설계·가드는 후속 이슈).
                "rescue_elapsed_ms": rescue_elapsed_ms,
                "relax_probes": probes_spent,
                # [#363 R3] first SSE(conditions) **이전**(자동완화)과 **이후**(칩 probe)를 반드시
                # 나눈다 — 합치면 first-token 지연 판정(rescue_elapsed_ms + relax_auto_elapsed_ms)
                # 이 아직 스트림에 영향 없는 칩 probe 소요까지 끌어와 과대계상된다.
                "relax_auto_elapsed_ms": relax_auto_elapsed_ms,
                "relax_chip_elapsed_ms": relax_chip_elapsed_ms,
                # [#363 R7] False면 conditions가 검색 이전에 이미 나가(545행) 위 소요가
                # first-token 을 전혀 늦추지 않는다 — `recommend_pipeline`과 같은 근거로 여기도
                # 싣는다(판정 시 True인 턴만 봐야 한다).
                "may_auto_relax": may_auto_relax,
                # [#427 D4·D7] 구제 체인 예산 판정 — 셋을 반드시 함께 싣는다. observe 모드
                # (기본)에서는 narrowed/skipped 두 필드가 "집행했다면 이랬을 값"(반사실)이라,
                # 이 필드(rescue_budget_mode)가 같은 줄에 없으면 읽는 사람이 실제로 좁혔다고
                # 오해한다.
                "rescue_stage_narrowed_timeout_ms": rescue_stage_narrowed_timeout_ms,
                "rescue_stage_skipped_budget": rescue_stage_skipped_budget,
                "rescue_budget_mode": settings.rescue_budget_mode,
            },
        )
        return

    # 니즈별 목록 분할 판정(REQ-REC-024, api-spec §4.2 PICK_ONE×N) — case 3(목적·상황형 발화)이
    # 니즈 여럿으로 전개된 턴에서만 나눈다. "유럽여행 필요한 거"의 파우치 후보와 어댑터 후보는
    # 서로 대안이 아니라 **다른 니즈**라 한 카드 묶음에 섞이면 사용자가 비교할 수 없다.
    # case 3 이 아닌 멀티 leg(예: 리파인 승계)은 종전대로 목록 1건 — 전개가 일어난 턴만 분할한다.
    # leg_of 가 비면(단일 filters 검색 경로) 나눌 근거 자체가 없다.
    need_legs = decision.category_legs
    # [이슈 #168 T3] 확장 턴이면서 unresolved leg 이 여럿(distinct query 2개 이상, "캠핑용품이랑
    # 낚시용품")이면 leaf 단위가 아니라 니즈(원 query) 단위로 분할 판정을 하도록 leg_of·need_legs
    # 를 번역한다 — `_interleave_by_leg` 가 서로 다른 query 의 leaf 를 섞어 내므로(PR #318 R12-2)
    # leaf 인덱스 그대로 `_split_by_need` 를 돌리면 leaf 하나당 목록 하나(최대 8개, 라벨은
    # `_need_label` 이 leaf query 를 그대로 써 "캠핑용품"×4·"낚시용품"×4 로 **중복**)가 되어
    # R4-1 이 재발한다. `first_leaf_for_query` 의 canonical(그 니즈 첫 leaf 것)을 대표로 쓴다 —
    # 니즈 단위 검색 자체가 없으니 대표 canonical 을 새로 만들 수는 없고, 하류(`_need_label` 등)는
    # 어차피 `query` 를 우선하므로 canonical 선택은 라벨에 드러나지 않는다.
    # distinct query 가 1개(대다수 확장 턴 — leaf 8개가 전부 같은 원 query 를 공유)면 번역할 게
    # 없다 — leaf 리스트를 그대로 두고, 아래 `split_by_need` 가드가 분할 자체를 막는다(기존 동작).
    # [PR #351 리뷰 R3-1] **query 가 전부 실재할 때만** 번역한다 — `query=None` 인 확장 leaf(raw
    # 만 있던 unresolved leg 파생)가 서로 다른 니즈 2개 이상에서 나오면 `None` 하나의 키로
    # 뭉쳐 서로 다른 니즈의 상품이 한 그룹에 섞이고, 그 혼합 그룹이 canonical 폴백 라벨로
    # 나간다 — 니즈 정체성을 확신할 수 없을 때 틀리게 가르느니 안 가른다(#51). `None` 이 하나라도
    # 섞이면 번역하지 않아 아래 가드가 분할을 막고 단일 목록(T3 이전 동작)으로 안전 후퇴한다.
    # 원본 leg 인덱스를 키로 쓰는 대안(리뷰어 제안)은 채택하지 않는다 — ① 동일 텍스트 query 인
    # leg 2개를 인덱스로 가르면 **라벨이 같은 목록 2개**가 나온다(R4-1[PR #318]이 결함으로 규정한
    # 바로 그 출력 — 라벨이 같으면 사용자 관점에선 같은 니즈라 병합이 옳다). ② 원본 leg 인덱스는
    # `expansion_leaves`(2-튜플) 평탄화에서 이미 소실됐고, 실으려면 `category_legs` 의
    # `list[tuple[str, str|None]]` 계약을 3-튜플로 넓혀 `_leg` 언패킹 등 소비부 전체가 흔들린다
    # — None 엣지 하나에 비례하지 않는 변경이다.
    expansion_grouped_by_need = False
    if decision.category_expanded and leg_of:
        seen_queries: set[str | None] = set()
        distinct_queries: list[str | None] = []
        first_leaf_for_query: dict[str | None, int] = {}
        for i, (_canonical, query) in enumerate(need_legs):
            if query not in seen_queries:
                seen_queries.add(query)
                distinct_queries.append(query)
                first_leaf_for_query[query] = i
        if len(distinct_queries) > 1 and None not in seen_queries:
            query_to_need_idx = {q: idx for idx, q in enumerate(distinct_queries)}
            leaf_query = [query for _, query in need_legs]
            leg_of = {pid: query_to_need_idx[leaf_query[leaf]] for pid, leaf in leg_of.items()}
            need_legs = [(need_legs[first_leaf_for_query[q]][0], q) for q in distinct_queries]
            expansion_grouped_by_need = True
    # [#222 PR #318 리뷰] 확장 턴(category_expanded)은 **기본적으로** 니즈 경계로 쪼개지 않는다
    # (조건 칩을 category_expanded 로 억제한 것과 같은 원칙, #51 표시=실제) — 단 위에서 니즈
    # 단위로 이미 번역된 턴(`expansion_grouped_by_need`)은 leaf 단위가 아니라 니즈 단위이므로
    # 이 가드에서 예외로 둔다(#168 이 의도적으로 바꾼 지점, PR #318 R12-2 고정 테스트가 예고).
    # `buy_all_mode`(아래)도 이 값을 참조하므로 니즈 단위 예산 세트(BUY_ALL)도 같이 열린다.
    # [이슈 #434 라운드2] 복원 턴(값 지정 category 제거가 남은 집합을 멀티 leg 으로 되살린 턴,
    # `category_legs_restored`)도 category_expanded 와 같은 이유로 니즈 경계 분할에서 뺀다 —
    # 이 멀티 leg 은 새 니즈 전개가 아니라 사용자가 지운 뒤 **남은 조건 집합**이다(#51 표시=실제).
    # 우연히 case==3 이 되는 턴이 와도(예: LLM 이 이번 발화를 목적형으로 오분류) 목록이 니즈별로
    # 쪼개지면 buy_all_mode·expose_budget·need_priority_gate 까지 함께 열려 버려 사용자가 기대한
    # "그 카테고리만 뺀 같은 목록"과 다른 결과가 나간다.
    split_by_need = (
        decision.case == 3
        and len(need_legs) > 1
        and bool(leg_of)
        and (not decision.category_expanded or expansion_grouped_by_need)
        and not decision.category_legs_restored
    )
    buy_all_mode = settings.budget_set_enabled and decision.buy_all and split_by_need

    # [#281] 니즈 priority 분류기 — 첫 이벤트 앞 직렬 지연을 0 으로 두려고 게이트 입력이 확정되는
    # 가장 이른 지점(split_by_need·buy_all_mode 확정 직후)에서 띄운다. 아래 rerank(smart tier)
    # 호출이 이 짧은 fast 호출을 완전히 가려 실질 추가 지연이 0 이다(lessons 2026-08-04
    # 「상한이 안전한지는 단일 호출 예산이 아니라 첫 이벤트 앞 직렬 합으로 잰다」).
    #
    # 게이트가 거짓이면 태스크를 아예 만들지 않는다 → LLM 호출 0회, 오늘과 완전히 동일(비용도 0).
    # [PR #314 리뷰] 이전 판은 "`limited_legs`(계약 상한 max_items)는 config 가
    # `category_fanout_max ≤ MAX_LISTS` 를 강제해 실무상 도달하지 않는다"고 적었는데 **틀렸다**
    # — `MAX_LISTS`(10)와 `max_items`로 넘기는 `LIST_MAX_PRODUCTS`(9)를 혼동한 주석이었다.
    # `category_fanout_max` 는 `MAX_LISTS` 까지(즉 10까지) 설정 가능한데 `max_items` 는 9라,
    # `category_fanout_max` 를 10으로 운영하면 leg 10개 > max_items 9 로 `limited_legs` 가
    # **설정에 따라 실제로 발동한다.** 그래서 게이트를 두 조건의 OR 로 연다:
    #   - `total_budget is not None` — `dropped_legs`(예산 제외)는 total_budget 이 있을 때만 발생.
    #   - `len(need_legs) > LIST_MAX_PRODUCTS` — `limited_legs` 가 발동할 수 있는 유일한 조건과
    #     정확히 같은 임계다(값을 따로 적지 않고 `build_budget_sets(max_items=...)` 에 넘기는
    #     것과 **같은 상수**에서 파생시킨다 — 숫자를 두 곳에 따로 적으면 이번처럼 갈라진다).
    # 예산도 없고 leg 수도 임계 이하인 BUY_ALL 턴은 여전히 호출 0회다(과하게 넓히지 않는다).
    need_priority_gate = (
        settings.need_priority_classifier_enabled
        and buy_all_mode
        and (decision.total_budget is not None or len(need_legs) > LIST_MAX_PRODUCTS)
    )
    priority_task = None
    if need_priority_gate:
        need_priority_labels = _need_priority_labels(need_legs)
        if need_priority_labels is None:
            # 라벨 없는 leg 이 섞이면 분류기 프롬프트의 니즈 이름 목록에 빈 자리가 생겨 LLM 이
            # 보는 인덱스와 need_legs 인덱스의 정합을 장담할 수 없다 — 그 leg 하나 때문에 신호
            # 전체를 못 믿느니 분류기를 아예 돌리지 않는다(폴백은 오늘 동작이라 손해가 없다).
            pass
        else:
            priority_task = asyncio.create_task(
                classify_need_priorities(
                    llm,
                    message=request.message,
                    needs=need_priority_labels,
                    settings=settings,
                    observer=observer,
                )
            )

    # [#281] 태스크 생성부터 회수까지를 `try/finally` 로 감싼다 — 정리 지점이 정상 회수(아래
    # `_collect_priority_task`) 하나뿐이면 **바깥에서 온 취소**(클라이언트가 끊거나 SSE
    # 제너레이터가 조기 종료되는 GeneratorExit)가 이 try 안의 어느 `await`(rerank 등)에서 오든
    # 분류기 태스크가 정리되지 않고 고아로 남는다(`app/agents/buyer/graph.py::_cancel_scope_task`
    # docstring 의 함정과 같다, #84).
    try:
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
        search_rank_by_id: dict[int, int] = {}
        for search_rank, candidate in enumerate(candidates, 1):
            search_rank_by_id.setdefault(candidate.product_id, search_rank)
        code_scoring_context = CodeScoringContext(
            filters=effective_filters,
            search_rank_by_id=search_rank_by_id,
            need_of=need_of,
            total_budget=decision.total_budget,
        )

        # rerank — smart tier 1회. 실패/타임아웃/유효후보 0건 시 검색순서 상위 N 으로 degrade(하드 제약 유지).
        if observer is not None:
            observer.record_model_call(resolve_model_id(settings, "smart"))
        if settings.progress_events_enabled:
            yield progress_frame("reranking", settings.progress_reranking_message)
        rerank_degraded = False
        raw_overall_comment = ""
        raw_overall_claims = ()
        # [이슈 #140] rerank 실패 시엔 §2 판정 규칙상 전 항목 search_order 로 분류되므로
        # 빈 집합으로 충분하다 — 성공 경로에서만 아래 스냅샷으로 채워진다.
        rerank_ranked_ids: set[int] = set()
        try:
            with trace_span(
                "llm.rerank",
                "llm",
                {
                    "model": resolve_model_id(settings, "smart"),
                    "rankingArm": settings.rerank_ranking_arm,
                },
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
                    # grounding과 ranking은 독립 rollout 축이다. 기존 grounding 선택은 유지하되
                    # ranking은 current를 시작/롤백 기본값으로 두고 graph 경계에서만 명시 전달한다.
                    # 따라서 한 축의 실험이 다른 축의 prompt/validator 계약을 조용히 바꾸지 않는다.
                    grounding_arm=settings.rerank_grounding_arm,
                    ranking_arm=settings.rerank_ranking_arm,
                    rrf_alpha=settings.rerank_rrf_alpha,
                    rrf_k=settings.rerank_rrf_k,
                    search_rank_by_id=search_rank_by_id,
                    code_scoring_context=code_scoring_context,
                )
            ranked_ids = [pid for pid, _ in rr.ranked]
            # [이슈 #140] provenance rankSource 판정용 스냅샷 — pin 을 얹기 **전**의 rerank
            # 순위 집합. `reason_by_id` 로 판정하지 않는다 — rerank 가 빈 rationale 을 줄 수
            # 있어 오분류된다(§2 판정 방법).
            rerank_ranked_ids = set(ranked_ids)
            reason_by_id = dict(rr.ranked)  # 상품별 근거(§4.2) — (productId, rationale) 튜플 → 맵
            if settings.rerank_ranking_arm == "code_assisted":
                # LLM이 선택하지 않은 expose-min 보충·지목 상품에는 semantic 이유를 붙일 수 없다.
                # 대신 같은 code evidence에서 만든 사실 근거를 미리 채워 실제 노출 목록에 들어올 때만
                # `_reasons()`가 꺼내 쓰게 한다. LLM이 고른 상품의 근거는 setdefault로 보존한다.
                for product_id, fallback_reason in code_assisted_fallback_reasons(
                    candidates,
                    code_scoring_context,
                ).items():
                    reason_by_id.setdefault(product_id, fallback_reason)
            raw_overall_comment = rr.overall_comment
            raw_overall_claims = rr.overall_claims
        except LLMError:
            rerank_degraded = True
            if trace := current_request_trace():
                trace.mark_degraded("rerank_fallback")
            ranked_ids = [p.product_id for p in candidates[:expose_budget]]
            if settings.rerank_ranking_arm == "code_assisted":
                fallback_reasons = code_assisted_fallback_reasons(
                    candidates,
                    code_scoring_context,
                )
                reason_by_id = {
                    product_id: fallback_reasons.get(product_id, "") for product_id in ranked_ids
                }
            else:
                # 기존 arm degrade 경로엔 rerank 근거가 없다 — 선택 필드인 reasons는 비운다.
                reason_by_id = {}
            # [#133] 품질 저하를 **고지한다**. 종전 문구("요청하신 조건으로 찾은 상품들이에요")는
            # 평상시와 구분되지 않아 개인화·근거가 통째로 사라진 사실이 사용자에게 가려졌다.
            # config 값은 운영자 주입이라 소스 리터럴이 아니다 — 정상 경로(rr.overall_comment)와
            # 같은 _strip_unsafe 정제를 받는다.
            raw_overall_comment = settings.rerank_fallback_notice

        # [#120 PR#230 리뷰] 지목 상품 고정 — rerank 는 relevance 로 expose_max 개만 고르고 "이건 반드시"
        # 라는 고정 수단이 없어(need_of/per_need 는 니즈 분할용), exact 제외·상한 절단을 다 통과한
        # 지목 상품이 여기서 조용히 빠질 수 있다. 쿼리에 상품명이 있으니 보통은 뽑히지만 그건
        # 휴리스틱이지 보장이 아니다. **후보에 남아 있는데 rerank 가 빠뜨린 것만** 앞에 얹어
        # "지목하면 다시 추천된다"를 강제한다 — rerank 가 이미 골랐으면 순서를 건드리지 않는다.
        # 근거(reason)는 없지만 §4.2 상 reasons 는 선택이고 degrade 경로도 같은 형태다.
        pinned_ids: set[int] = set()  # [이슈 #140] provenance rankSource 판정용
        if repurchase_ids:
            already = set(ranked_ids)
            pinned = [
                p.product_id
                for p in candidates
                if p.product_id in repurchase_ids and p.product_id not in already
            ]
            pinned_ids = set(pinned)
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
        plan = None
        budget_sets_failed = False
        infeasible_due_to_budget = False
        priorities: tuple[int, ...] | None = None  # buy_all_mode 가 아니면 회수 자체가 없다
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
            # priority 회수 — build_budget_sets 호출 직전(PACKET §5(c)). 분류기가 없거나
            # (priority_task None) 실패해도 `_collect_priority_task` 가 None 을 돌려주므로
            # build_budget_sets 의 엄격 폴백(전 leg 균일값)이 2차 방어로 남는다.
            priorities = await _collect_priority_task(priority_task)
            try:
                plan = await asyncio.to_thread(
                    build_budget_sets,
                    pools=pools,
                    total_budget=decision.total_budget,
                    max_sets=settings.budget_set_max_count,
                    max_combinations=settings.budget_set_max_combinations,
                    max_items=LIST_MAX_PRODUCTS,
                    priorities=priorities,
                )
            except Exception as exc:  # noqa: BLE001 - 세트 실패는 종전 PICK_ONE으로 degrade
                logger.warning("budget_sets_failed", extra={"reason": str(exc)})
                budget_sets_failed = True
                plan = None
            if plan is None and not budget_sets_failed and decision.total_budget is not None:
                try:
                    # 총액 제한만 제거했을 때 조합이 생기는 경우에만 예산을 실패 원인으로 고지한다.
                    # 후보/가격 자체가 부족한 경우는 같은 입력으로도 None 이라 별도 문구로 분리된다.
                    # [#281] 두 호출이 다른 priority 근거를 쓰면 진단이 실제 실패 원인을 가리키지
                    # 않으므로 같은 priorities 를 넘긴다.
                    infeasible_due_to_budget = (
                        await asyncio.to_thread(
                            build_budget_sets,
                            pools=pools,
                            total_budget=None,
                            max_sets=settings.budget_set_max_count,
                            max_combinations=settings.budget_set_max_combinations,
                            max_items=LIST_MAX_PRODUCTS,
                            priorities=priorities,
                        )
                        is not None
                    )
                except Exception as exc:  # noqa: BLE001 - 진단 실패도 PICK_ONE 스트림은 살린다
                    logger.warning(
                        "budget_sets_failure_diagnosis_failed", extra={"reason": str(exc)}
                    )
                    budget_sets_failed = True
            if plan is not None:
                ranked_ids = list(
                    dict.fromkeys(
                        product_id for item in plan.sets for product_id in item.product_ids
                    )
                )
    finally:
        _cancel_priority_task(priority_task)

    overall_grounding_decision = None
    if not rerank_degraded and settings.rerank_grounding_arm == "validated":
        final_view = FinalRecommendationView(
            list_type="BUY_ALL" if plan is not None else "PICK_ONE",
            total_budget=decision.total_budget if plan is not None else None,
            product_groups=(
                tuple(tuple(item.product_ids) for item in plan.sets)
                if plan is not None
                else tuple(tuple(group) for _leg, group in exposed_groups)
            ),
        )
        overall_grounding_decision = validate_and_render_overall_comment(
            raw_overall_claims,
            final_view=final_view,
            products_by_id={product.product_id: product for product in candidates},
            settings=settings,
        )
        raw_overall_comment = overall_grounding_decision.rendered_comment
    comment = _strip_unsafe(raw_overall_comment)

    # [#101 #8] 관측성 — 파이프라인 후보 깔때기를 한 줄 구조화 로그로 남긴다(recall 손실·자원 진단).
    # received(수신) → after_dedup(최근구매 제외 후) → compressed(embedding_rerank_limit 절단 후)
    # → final(노출). 임베딩 재정렬 degrade 사유는 backend(_log.warning), rerank degrade 는 여기서.
    log_structured(
        logger,
        "recommend_pipeline",
        **{
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
            # [#363 R7] `recommend_zero_result`(0건 종결, 곧장 return)와 이 로그(성공 종결)는
            # 같은 턴에서 상호 배타다 — 합쳐서 봐야 "구제를 시도한 전체 턴"이 된다. 구제가
            # 실제로 통해 지연된 첫 토큰이라도 결과를 받은 턴이야말로 이 이슈가 재려는 표본이라,
            # 0건 로그에만 있으면 그 절반이 관측되지 않는다. 값의 정의는 zero_result 쪽과
            # 동일한 변수를 그대로 싣는다.
            "rescue_elapsed_ms": rescue_elapsed_ms,
            "relax_auto_elapsed_ms": relax_auto_elapsed_ms,
            "relax_chip_elapsed_ms": relax_chip_elapsed_ms,
            # [#363 R7] False면 conditions가 검색 이전에 이미 나가(545행) 위 소요가 first-token
            # 을 전혀 늦추지 않는다 — 이 필드 없이는 로그만으로 지연 여부를 가릴 수 없다.
            "may_auto_relax": may_auto_relax,
            # [#427 D4·D7] 구제 체인 예산 판정 — `recommend_zero_result` 와 같은 반사실 규약.
            "rescue_stage_narrowed_timeout_ms": rescue_stage_narrowed_timeout_ms,
            "rescue_stage_skipped_budget": rescue_stage_skipped_budget,
            "rescue_budget_mode": settings.rescue_budget_mode,
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
            # [#281] priority 신호 관측 — 값(니즈 이름)은 싣지 않는다(#119 PII). 신호가 실제로
            # 적용됐는지(분류기 성공)와, REQ-REC-075 가 요구하는 "조용히 누락하지 않는다"의 빈도를
            # 운영에서 볼 수 있어야 하므로 필수(priority 1) 니즈가 예산 또는 목록 상한 때문에
            # 빠진 턴을 표시한다(둘 다 덮는 이유는 아래 헬퍼 docstring 참조, PR #314 리뷰).
            "need_priority_applied": priorities is not None,
            # 범위 밖 leg 방어·두 제외 경로(dropped_legs·limited_legs)를 순수 헬퍼로 뽑았다
            # (_need_priority_required_dropped 참조) — 근거·불변식 설명도 그쪽 docstring 에 있다.
            "need_priority_required_dropped": _need_priority_required_dropped(priorities, plan),
            "overall_supported_claim_codes": (
                list(overall_grounding_decision.supported_claim_codes)
                if overall_grounding_decision is not None
                else []
            ),
            "overall_claim_downgraded": bool(
                overall_grounding_decision and overall_grounding_decision.downgraded
            ),
            "overall_claim_failure_reasons": (
                list(overall_grounding_decision.failure_reasons)
                if overall_grounding_decision is not None
                else []
            ),
        },
    )

    if comment:
        yield sse("token", TokenData(text=comment).model_dump(by_alias=True))

    # [#222] 광역 fan-out 확장 고지 — **질문이 아니라 고지**다("더 좁혀 드릴까요?" 같은 되물음은
    # 넣지 않는다, 그 판정 게이트가 실측으로 없다). 문구는 LLM 이 짓지 않는다 — 확장 leaf 이름의
    # " > " 앞부분(중분류)을 중복 제거해 그대로 쓴다. DB 값이라 존재하지 않는 카테고리를 말할 수
    # 없다(#59 재발 방지). 템플릿은 config 주입(rerank_fallback_notice 와 동일 패턴).
    # [#222 F-1] `category_expand_notice_suppressed` 면 위에서 무필터로 되돌아간 것이다 — 중분류를
    # 훑었다고 고지해 놓고 실제로는 무필터로 찾은 것이 되면 거짓 고지라 여기서 막는다.
    # 전 leg 실패(전량 SpringUnavailableError 등)는 이 지점에 아예 도달하지 않는다 —
    # `_run_search` 가 `survived` 가 비면 None 을 돌려주고 그건 `search_bundle is None` 분기로
    # 가 SEARCH_FAILED 로 끝나기 때문이다(§6, 고지 블록은 그 아래에서만 실행된다).
    if (
        decision.category_expanded
        and not category_expand_notice_suppressed
        and settings.category_expand_notice_enabled
        and (template := _strip_unsafe(settings.category_expand_notice))
    ):
        mids: list[str] = []
        seen_mid: set[str] = set()
        # [PR #318 리뷰 R14-1] fan-out 은 leg 별로 부분 실패할 수 있다(`survived`, `fanout_
        # partial`) — 실패한 leg 의 카테고리는 실제로 검색하지 못했는데 전체 `category_legs` 로
        # mids 를 조립하면 "찾아봤어요"라고 거짓 고지된다(#51 표시=실제). `expansion_searched_legs`
        # 가 있으면(확장 fan-out 이 실제로 돈 턴) 그 생존 leg 인덱스만 쓰고, None 이면(단일
        # filters 검색 등 fan-out 자체가 없던 경로) 종전대로 전체를 쓴다.
        notice_legs = (
            [decision.category_legs[i] for i in expansion_searched_legs]
            if expansion_searched_legs is not None
            else decision.category_legs
        )
        for cat, _query in notice_legs:
            mid = cat.split(" > ", 1)[0]
            if mid not in seen_mid:
                seen_mid.add(mid)
                mids.append(mid)
        if mids:
            try:
                expand_notice = template.format(items=" · ".join(mids))
            except (KeyError, IndexError, ValueError):
                logger.warning("category_expand_notice_invalid")
            else:
                if expand_notice := _strip_unsafe(expand_notice):
                    yield sse("token", TokenData(text=expand_notice).model_dump(by_alias=True))

    # [이슈 #168 T2] split 턴(니즈별 그룹 출력)에 그룹 구조를 결정론적으로 서술한다 — #222 확장
    # 고지와 같은 패턴으로 LLM 이 짓지 않고 여기서 조립한다(`rerank.py` 는 이 레인 금지 파일).
    # 순서는 위 확장 고지(mids) **뒤** — 확장 턴에서도 두 고지가 공존한다(니즈 그룹핑은 "어디를
    # 찾았는지"가 아니라 "몇 개씩 나눴는지"라 서로 다른 정보다). 라벨은 push 에 실릴 것과 같은
    # `_need_label` 산출을 재사용해 표시=실제(#51)를 지키고 길이 캡도 그대로 물려받는다. 라벨
    # 없는 그룹은 "니즈 k" 같은 의미 없는 폴백 대신 **건너뛴다** — 사용자에게 무의미한 텍스트를
    # 보여주는 것보다 그 그룹만 서술에서 조용히 빠지는 편이 낫다. 전부 없으면 미발신한다.
    # **`plan is None` 도 함께 요구한다** — `plan is not None`(BUY_ALL 세트가 실제로 만들어진
    # 턴)이면 실제 push 되는 lists 는 `exposed_groups` 가 아니라 budget set 조합이라(아래 push
    # 조립부의 `if plan is not None: ... else: lists = [_entry(leg, group) for leg, group in
    # exposed_groups]` 와 동일 조건), "니즈별로 나눠 담았어요"가 실제로 나간 목록과 어긋나
    # 표시=실제(#51)가 깨진다. BUY_ALL 세트는 `budget_set_label_focus`(라벨에 니즈 이름 포함)로
    # 이미 자기 서술을 갖고 있다.
    if (
        split_by_need
        and plan is None
        and settings.group_notice_enabled
        and (group_template := _strip_unsafe(settings.group_notice))
    ):
        group_items = [
            item
            for leg, group in exposed_groups
            if (label := _need_label(need_legs[leg]))
            and (item := _strip_unsafe(f"{label} {len(group)}개"))
        ]
        if group_items:
            try:
                group_notice = group_template.format(items=" · ".join(group_items))
            except (KeyError, IndexError, ValueError):
                logger.warning("group_notice_invalid")
            else:
                if group_notice := _strip_unsafe(group_notice):
                    yield sse("token", TokenData(text=group_notice).model_dump(by_alias=True))

    # [#162/#393] 조건 없음 안내 — 없으면 사용자가 인기 상품을 **자기 조건이 반영된 결과**로
    # 오해한다. `unfiltered_bypass`(A) 도 이 블록을 쓴다 — 그 턴은 payload 기준으로 실제 조건이
    # 하나도 안 나간 턴이라 #162 고지가 그대로 참이다(rating_min·attr_conditions 만 있는 턴처럼
    # 사용자가 무언가는 말했어도, Spring 에는 아무 필터도 전달되지 않았다는 사실은 동일하다).
    # `popular_degraded` 턴에는 내지 않는다: I-3 가 죽어 무필터 검색으로 떨어진 결과에
    # "인기 상품으로 보여드릴게요" 라고 말하면 거짓이 된다(#133 정직성 규약 — 그 턴은
    # `mark_degraded("popular_fallback")` 로 관측에 남는다).
    if (no_condition or unfiltered_bypass) and not popular_degraded:
        if decision.total_budget is not None:
            # 예산을 말한 턴에는 그 금액을 되짚어 준다 — "조건을 안 주셨다"고 하면 거짓이 된다.
            # [PR #311 리뷰] **자리표시자 존재를 먼저 검사한다** — `str.format` 은 쓰지 않는
            # 키워드를 조용히 무시하므로(`"금액 없이".format(budget=...)` 는 예외 없이 그대로),
            # `{budget}` 이 통째로 빠진 오설정은 아래 except 로 잡히지 않고 금액만 소리 없이
            # 사라진다. 오타(`{budgt}`)만 KeyError 로 잡힌다. `_set_label` 이 `"{need}" not in`
            # 으로 같은 검사를 하는 것과 맞춘다.
            # 폴백은 인기 상품 문구다 — 이 경로의 후보는 실제로 인기 상품이라 참이고,
            # 금액을 주장하지 않으므로 거짓 고지가 되지 않는다.
            if "{budget}" not in settings.no_condition_notice_budget:
                logger.warning("no_condition_budget_notice_missing_placeholder")
                raw_notice = settings.no_condition_notice_popular
            else:
                try:
                    raw_notice = settings.no_condition_notice_budget.format(
                        budget=f"{decision.total_budget:,}원"
                    )
                except (KeyError, IndexError, ValueError):
                    logger.warning("no_condition_budget_notice_invalid")
                    raw_notice = settings.no_condition_notice_popular
        else:
            raw_notice = settings.no_condition_notice_popular
        if notice := _strip_unsafe(raw_notice):
            yield sse("token", TokenData(text=notice).model_dump(by_alias=True))

    # [#336] 제약만 있는 턴(과소지정 ∧ not no_condition)의 인기 상품 고지 — 위 no_condition
    # 블록과 상호배타다(no_condition 은 이미 자기 고지를 냈다). 실제로 가격 필터를 통과한 인기
    # 상품이라 참인 문구다(SPEC-UNDERSPECIFIED-336 §4). `popular_degraded` 면 인기 주장 고지는
    # 스킵한다 — 그 턴은 무필터 검색으로 떨어진 결과라 "인기 상품"이 거짓이 된다(#162 와 같은
    # 정직성 규약). 되물음은 아래에서 no_condition 여부와 무관하게 낸다.
    if underspecified and not no_condition and not popular_degraded:
        if underspecified_notice := _strip_unsafe(settings.underspecified_notice):
            yield sse("token", TokenData(text=underspecified_notice).model_dump(by_alias=True))

    # [#393 B] 카테고리 매핑 드롭 + 검색 0건 → 인기 상품 대체 고지. no_condition/underspecified
    # 블록과 상호배타다(이 턴은 정의상 둘 다 아니다 — `is_category_mapping_dropped` 는
    # `category_queries` 축이 있어야 참인데 그 축은 두 판정을 이미 False 로 만든다). 없으면
    # 사용자가 말한 상품군을 못 찾아 인기 상품으로 답한다는 사실이 감춰져 거짓이 된다(#132·#133
    # 정직성 규약). `popular_degraded` 와는 자연히 배타지만(이 대체는 성공한 I-3 조회에서만
    # True 가 된다) 다른 고지 블록과 같은 규약으로 방어적으로 검사한다.
    if category_unmapped_zero_result and not popular_degraded:
        if category_unmapped_notice := _strip_unsafe(settings.category_unmapped_notice):
            yield sse("token", TokenData(text=category_unmapped_notice).model_dump(by_alias=True))

    # [리뷰 F2] 카테고리 되물음(D3 emit 지점 2) 은 여기서 내지 않는다 — push 가 성공했는지가
    # 아직 정해지지 않아, "무선이어폰 중에…" 라고 예시를 들며 되물으면서 정작 그 상품이 화면에
    # 뜨지도 않을 수 있다(표시=실제 #51 위반). push 결과가 정해진 뒤(아래, done 직전)로 옮겼다.

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
    if settings.progress_events_enabled:
        yield progress_frame("publishing", settings.progress_publishing_message)
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

        # [이슈 #140] provenance — push 에 실제로 실린 값을 그대로 반영한다(§2 판정 방법).
        def _rank_source(pid: int) -> RankSource:
            if rerank_degraded:
                return "search_order"
            if pid in pinned_ids:
                return "repurchase_pin"
            if pid in rerank_ranked_ids:
                return "rerank"
            return "expose_min_fill"

        def _provenance_list(entry: RecommendationListEntry) -> ProvenanceList:
            # 목록(entry)당 한 번만 만든다 — 항목(pid)마다 다시 만들지 않는다(프로필 경로의
            # profile_reason_pids 와 같은 모양).
            reason_pids = {r.product_id for r in entry.reasons}
            return ProvenanceList(
                list_id=entry.list_id,
                label=entry.label,
                items=[
                    ProvenanceItem(
                        product_id=pid,
                        rank_source=_rank_source(pid),
                        has_reason=pid in reason_pids,
                    )
                    for pid in entry.product_ids
                ],
            )

        emit_recommendation_provenance(
            logger,
            settings=settings,
            request_id=request_id,
            recommendation_request_id=recommendation_request_id,
            surface="chat",
            pipeline="search_rerank",
            prompt_version=_rerank_prompt_version(
                degraded=rerank_degraded,
                ranking_arm=settings.rerank_ranking_arm,
                grounding_arm=settings.rerank_grounding_arm,
                grounding_prompt_version=settings.rerank_prompt_version,
                scoring_prompt_version=settings.rerank_scoring_prompt_version,
                code_assisted_prompt_version=settings.rerank_code_assisted_prompt_version,
            ),
            ranker_model=None if rerank_degraded else resolve_model_id(settings, "smart"),
            personalized=bool(profile),
            deterministic=True,
            list_type=list_type,
            owner_fp=safe_fingerprint(identity.subject) if identity is not None else None,
            session_fp=safe_fingerprint(request.session_id),
            lists=[_provenance_list(entry) for entry in push.lists],
        )
        # 직전 추천을 장바구니 담기(productId 해소, 경로 B)용으로 보관 — **push 성공 후에만**.
        # push 실패로 카드가 노출되지 않았으면 저장하지 않아 "그거 담아줘"가 미노출 상품을 담지 않는다.
        if cart_store is not None and thread_key is not None:
            name_by_id = {p.product_id: p.name for p in candidates}
            # I-1 옵션 힌트(이슈 #455) — 되물음 문구·자동 선택 정합 가드용. 이 경로만 실은 이유는
            # SpringProduct 원본이 있는 유일한 push 경로라서다(프로필 랭킹 경로는 AI 인덱스라
            # options/optionCount 가 없다 — 아래 힌트 없이 오늘 경로로 degrade).
            option_hints = {
                p.product_id: OptionHint(names=tuple(p.options or ()), total=p.option_count)
                for p in candidates
                if p.options or p.option_count is not None
            }
            await cart_store.set_last_reco(
                thread_key,
                [(pid, name_by_id.get(pid, "")) for pid in ranked_ids],
                option_hints=option_hints,
                recommendation_contexts={
                    product_id: RecommendationContext(
                        recommendation_request_id=recommendation_request_id,
                        list_id=entry.list_id,
                    )
                    for entry in push.lists
                    for product_id in entry.product_ids
                },
                # #571 — 표시 순서 = 저장 순서는 목록이 정확히 1개일 때만 성립한다(BUY_ALL 은
                # 세트 간 중복이 dedup 로 접혀 화면 칸 수와 저장 건수가 어긋나고, 다목록 PICK_ONE
                # 은 화면이 섹션으로 쪼개져 전역 순번이 정의되지 않는다 — §2 결정 2).
                ordinal_span=len(push.lists[0].product_ids) if len(push.lists) == 1 else 0,
            )
    else:
        # push 실패 → products.ready 없음. rerank 코멘트가 "찾았다"고 했으니 목록 지연을 고지하고
        # 정상 종료한다(경로 B 실패 계약 — error 아님, done 유지).
        # [#133] 문안은 config 주입 — degrade 고지 문구를 한 곳에서 관리한다.
        if trace := current_request_trace():
            trace.mark_degraded("push_skipped")
        if cart_store is not None and thread_key is not None:
            try:
                await cart_store.set_push_failed(thread_key)
            except Exception as exc:  # noqa: BLE001 - 문구 보강 실패로 추천 턴을 죽이지 않는다
                logger.warning("push_failed_marker_write_failed", extra={"reason": str(exc)})
        if push_notice := _strip_unsafe(settings.push_skipped_notice):
            yield sse("token", TokenData(text=push_notice).model_dump(by_alias=True))

    # [리뷰 F2] 카테고리 되물음(D3 emit 지점 2, no_condition ⊂ underspecified 라 두 턴 모두
    # 여기서 낸다) — push 결과가 정해진 **뒤**라 표시=실제(#51)를 지킨다.
    #   - push 성공: 노출 후보(실제로 push 된 상품, `ranked_ids`) 기반 예시 질문.
    #   - push 실패: 보여준 게 없으니 예시 없이 generic 질문만(되묻기 자체는 유지 — 다음 턴을
    #     위한 질문이라 카드 유무와 무관하게 유효하다).
    if underspecified:
        if pushed:
            id_to_product = {p.product_id: p for p in candidates}
            exposed_products = [id_to_product[pid] for pid in ranked_ids if pid in id_to_product]
            reask_question = build_reask_question(exposed_products, settings)
        else:
            reask_question = _strip_unsafe(settings.underspecified_reask_question)
        if reask_question:
            yield sse("token", TokenData(text=reask_question).model_dump(by_alias=True))

    yield sse(
        "done",
        DoneData(finish_reason="stop").model_dump(by_alias=True),
    )
