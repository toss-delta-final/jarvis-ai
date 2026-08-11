"""decompose + `is_underspecified_turn` 호출 — 판정 로직을 복제하지 않는다 (#380).

셀별 N 채우기 루프는 `evals/legs_probe/runner.py` 규약을 그대로 승계한다: **실패는 표본이
아니다**(재시도로 N 을 채운다) · `BudgetExceeded` 는 밖으로 던지지 않고 그 셀만 멈춘다
(legs_probe `[R2-1]` 근거를 그대로 옮긴다) · 결과는 항상 `cellId` 정렬 · 실패 메시지는
`scrub_message` 로 계정 식별자 제거.

[§D2] 원인 축 분해(§D11)·불변 재판정(§D9)도 재구현이 아니라 프로덕션 함수(`is_underspecified_turn`·
`detect_expansion_need`) 재호출로 한다 — 판정식을 이 파일에 옮겨 적지 않는다.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING, Any

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.leg_head import suppress_generic_single_leg
from app.agents.buyer.recommendation.needs_expansion import detect_expansion_need
from app.agents.buyer.recommendation.no_condition import _is_blank
from app.agents.buyer.recommendation.state import RouteDecision
from app.agents.buyer.recommendation.underspecified import is_underspecified_turn
from app.agents.buyer.recommendation.underspecified_classifier import (
    apply_underspecified_classification,
    classify_underspecified,
    could_be_underspecified_message,
)
from app.core.config import Settings
from app.core.llm import LLMClient
from app.schemas.spring import ProductSearchFilters
from evals.model_eval.budget import BudgetExceeded
from evals.underspecified_probe.loader import Cell
from evals.underspecified_probe.schema import BLOCKING_AXES

if TYPE_CHECKING:  # 순환 임포트 회피(union.py 가 이 파일의 _clone_decision 등을 쓴다) — 타입만.
    from evals.underspecified_probe.union import UnionSampleResult

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0

# provider 오류 메시지에 섞여 오는 계정·키 식별자 — legs_probe/intent_probe.runner.scrub_message
# 와 같은 규약(산출물이 리포에 커밋되므로 지우고 남긴다).
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다 — 원인 판별에 필요한 문구는 남긴다."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


@dataclass(frozen=True)
class JudgmentSettings:
    """[§D6] `is_underspecified_turn` 이 duck-typed 로 읽는 필드는 `underspecified_reask_enabled`
    하나뿐이다 — `SimpleNamespace` 대신 frozen dataclass 를 써서 오타 필드가 조용히 붙지 않게 한다."""

    underspecified_reask_enabled: bool


def dedicated_suppressed_decision(decision: RouteDecision) -> tuple[RouteDecision, bool]:
    """leg 제거 뒤 decompose의 fallback 파생식을 표본 정보로 재현한다.

    원 semantic query가 단일 leg query와 같으면 cat_signal이었을 가능성으로 본다. llm_sq가 같은
    문자열을 냈을 수도 있어 구분 불가하며, 그 모호 표본은 두번째 반환값으로 산출물에 남긴다.
    """
    clone = _clone_decision(decision)
    leg_query = (
        (clone.category_queries[0].query or "").strip() if len(clone.category_queries) == 1 else ""
    )
    semantic = (clone.filters.semantic_query or "").strip()
    ambiguous = bool(leg_query and semantic == leg_query and not clone.semantic_query_is_fallback)
    clone.category_queries = []
    if clone.semantic_query_is_fallback or ambiguous:
        clone.semantic_query_is_fallback = True
    return clone, ambiguous


def postprocessed_decision(
    decision: RouteDecision, *, generic_heads: frozenset[str], condition_terms: frozenset[str]
) -> tuple[RouteDecision, bool]:
    """파싱부 head 억제와 동형인 사본; fallback 파생은 dedicated와 단일 구현을 공유한다."""
    legs = suppress_generic_single_leg(
        decision.category_queries,
        decision.filters,
        enabled=True,
        generic_heads=generic_heads,
        condition_terms=condition_terms,
    )
    if len(legs) == len(decision.category_queries):
        return _clone_decision(decision), False
    return dedicated_suppressed_decision(decision)


def _clone_decision(decision: RouteDecision) -> RouteDecision:
    """[§D11] 얕은 `dataclasses.replace` 는 `filters`(pydantic 모델)·리스트 필드를 공유한다 —
    재판정용 사본은 가변 필드를 전부 새로 만들어야 원본을 건드리지 않는다."""
    return replace(
        decision,
        filters=decision.filters.model_copy(deep=True),
        category_queries=list(decision.category_queries),
        category_legs=list(decision.category_legs),
        repurchase_products=list(decision.repurchase_products),
        revert_categories=list(decision.revert_categories),
        attr_conditions_suppressed_axes=list(decision.attr_conditions_suppressed_axes),
    )


def _clear_axis(decision: RouteDecision, axis: str) -> None:
    """[§D11] 정규 축 하나를 "빈 값"으로 만든다(제자리 변경 — 호출부가 사본에만 적용한다).

    `semanticQueryIsFallback` 의 "빈 값"은 `True` 다(폴백 = 신호 없음, 패킷 §D11)."""
    if axis == "categoryLegs":
        decision.category_legs = []
    elif axis == "categoryQueries":
        decision.category_queries = []
    elif axis == "semanticQueryIsFallback":
        decision.semantic_query_is_fallback = True
    elif axis == "repurchaseProducts":
        decision.repurchase_products = []
    elif axis == "revertCategories":
        decision.revert_categories = []
    elif axis == "filters.category":
        decision.filters.category = None
    elif axis == "filters.brand":
        decision.filters.brand = None
    elif axis == "filters.keyword":
        decision.filters.keyword = None
    elif axis == "filters.color":
        decision.filters.color = None
    elif axis == "filters.attrConditions":
        decision.filters.attr_conditions = None
    elif axis == "filters.rating_min":
        decision.filters.rating_min = None
    else:
        raise ValueError(f"알 수 없는 정규 축입니다: {axis}")


def ablate_axis(decision: RouteDecision, axis: str) -> RouteDecision:
    """[§D11] 축 하나를 소거한 `RouteDecision` 사본 — 원본은 건드리지 않는다."""
    clone = _clone_decision(decision)
    _clear_axis(clone, axis)
    return clone


def nonblank_blocking_axes(decision: RouteDecision) -> list[str]:
    """[§D11] 현재 판정에 기여 중인(=비어 있지 않은) 정규 축 전부 — 미탐의 `blockingAxes` 컬럼.

    `is_underspecified_turn` 이 실제로 보는 술어와 정확히 같은 것을 쓴다 — `filters.*` 축은
    프로덕션 헬퍼 `_is_blank` 를 그대로 재사용하고(복제 아님), 리스트 축(`categoryLegs` 등)은
    프로덕션과 같은 단순 truthy 판정이다.
    """
    out: list[str] = []
    if decision.category_legs:
        out.append("categoryLegs")
    if decision.category_queries:
        out.append("categoryQueries")
    if not decision.semantic_query_is_fallback:
        out.append("semanticQueryIsFallback")
    if decision.repurchase_products:
        out.append("repurchaseProducts")
    if decision.revert_categories:
        out.append("revertCategories")
    what_and_blocking = {
        "filters.category": decision.filters.category,
        "filters.brand": decision.filters.brand,
        "filters.keyword": decision.filters.keyword,
        "filters.color": decision.filters.color,
        "filters.attrConditions": decision.filters.attr_conditions,
        "filters.rating_min": decision.filters.rating_min,
    }
    for axis, value in what_and_blocking.items():
        if not _is_blank(value):
            out.append(axis)
    return sorted(out)


def cause_axes_for_miss(
    decision: RouteDecision, prior: ProductSearchFilters | None, judgment_settings: JudgmentSettings
) -> list[str]:
    """[§D11] 미탐 원인 — 단일 축 소거 재판정(ablation). 판정이 True 로 뒤집히는 축을 원인으로
    기록한다(복수 가능). 어느 단일 축으로도 안 뒤집히면 `["multiple"]`."""
    flips = [
        axis
        for axis in sorted(BLOCKING_AXES)
        if is_underspecified_turn(ablate_axis(decision, axis), prior, judgment_settings)
    ]
    return flips or ["multiple"]


def reverdict_with_flag_off(decision: RouteDecision, prior: ProductSearchFilters | None) -> bool:
    """[§D9] `flagOffInvariant` — `underspecified_reask_enabled=False` 로 재판정. LLM 콜 0."""
    return is_underspecified_turn(
        decision, prior, JudgmentSettings(underspecified_reask_enabled=False)
    )


def reverdict_with_prior_gate(decision: RouteDecision) -> bool:
    """[§D9] `priorGateInvariant` — `prior=ProductSearchFilters()` 로 재판정. LLM 콜 0."""
    return is_underspecified_turn(
        decision, ProductSearchFilters(), JudgmentSettings(underspecified_reask_enabled=True)
    )


@dataclass(frozen=True)
class Sample:
    """성공한 decompose+판정 호출 1건. 실패는 여기 들어오지 않는다.

    `decision`·`prior` 를 그대로 들고 있어 D9(불변 재판정)·D11(원인 축 분해)가 **런 재실행 없이**
    `is_underspecified_turn` 을 다시 호출할 수 있다(legs_probe 규약 승계: 판정 근거는 전부
    `samples.csv` 로 재집계 가능해야 한다).
    """

    case_id: str
    sample_index: int
    decision: RouteDecision
    prior: ProductSearchFilters | None
    verdict: bool
    expansion_reason: str | None
    latency_ms: int
    # [#432] `--union` 일 때만 채워진다 — decompose 단계 표본은 union 단계 성패와 무관하게
    # 그대로 산다(§2-6 항목 2). None 이면 union 모드가 아니었다는 뜻이다.
    union: "UnionSampleResult | None" = None
    dedicated_called: bool = False
    dedicated_disagreed: bool = False
    dedicated_failed: bool = False
    dedicated_ambiguous: bool = False
    before_verdict: bool | None = None
    postprocess_verdict: bool | None = None
    # [#464] 후처리 뒤 `filters.attr_conditions` 에 남은 축 — samples.csv 재집계용 원문.
    attr_condition_axes: list[str] = field(default_factory=list)
    # [#464] attrConditions 에서 결정론 후처리로 제거한 축 — samples.csv 재집계용 진단 원문.
    attr_conditions_suppressed_axes: list[str] = field(default_factory=list)

    @property
    def intent(self) -> str:
        """[F-1] 프로덕션은 `intent == "recommend"` 인 턴에서만 판정을 호출한다 — confirmatory
        분모를 이 값으로 좁힌다(metrics.py `_recommend_pairs`)."""
        return self.decision.intent

    @property
    def case(self) -> int:
        return self.decision.case

    @property
    def semantic_query_is_fallback(self) -> bool:
        return self.decision.semantic_query_is_fallback

    @property
    def semantic_query(self) -> str | None:
        """decompose 산출 원문 — 이번 런의 핵심 발견("fast 가 무조건 발화에도 의미쿼리를
        지어낸다")을 산출물만으로 재집계할 수 있게 한다(F-1)."""
        return self.decision.filters.semantic_query

    @classmethod
    def from_decision(
        cls,
        decision: RouteDecision,
        *,
        case_id: str,
        sample_index: int,
        prior: ProductSearchFilters | None,
        judgment_settings: JudgmentSettings,
        latency_ms: int,
        union: "UnionSampleResult | None" = None,
        dedicated_called: bool = False,
        dedicated_disagreed: bool = False,
        dedicated_failed: bool = False,
        dedicated_ambiguous: bool = False,
        before_verdict: bool | None = None,
        postprocess_verdict: bool | None = None,
    ) -> "Sample":
        verdict = is_underspecified_turn(decision, prior, judgment_settings)
        # [§D10.3] `unresolved=[]` — 이 하네스는 카테고리 매핑(2단계)을 돌리지 않으므로(§D2 규약)
        # 매핑 실패 목록이 존재하지 않는다는 사실의 정직한 반영이다. D2 의 `mapping_failed` 규칙은
        # 이 하네스에서는 발동할 수 없다. `detect_expansion_need` 는 프로덕션 함수를 그대로
        # 호출한다(재구현 금지 규약 유지). [#432] union 모드의 **실측** 대응 축은
        # `sample.union.expansion_reason`(실제 unresolved) 이다 — 이 값(가정판)과 혼동 금지.
        expansion_reason = detect_expansion_need(
            decision.category_queries, case=decision.case, unresolved=[]
        )
        return cls(
            case_id=case_id,
            sample_index=sample_index,
            decision=decision,
            prior=prior,
            verdict=verdict,
            expansion_reason=expansion_reason,
            union=union,
            latency_ms=latency_ms,
            dedicated_called=dedicated_called,
            dedicated_disagreed=dedicated_disagreed,
            dedicated_failed=dedicated_failed,
            dedicated_ambiguous=dedicated_ambiguous,
            before_verdict=before_verdict,
            postprocess_verdict=postprocess_verdict,
            attr_condition_axes=list((decision.filters.attr_conditions or {}).keys()),
            attr_conditions_suppressed_axes=list(decision.attr_conditions_suppressed_axes),
        )


@dataclass(frozen=True)
class FailureRecord:
    """버린 시도 1건. 표본이 아니라 '몇 번 만에 채웠나'의 근거다."""

    cell_id: str
    attempt: int
    error_type: str
    message: str


@dataclass
class CellResult:
    """한 셀(앵커 1건)의 측정 결과."""

    cell_id: str
    case_id: str
    slice: str
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False


def backoff_seconds(failure_count: int) -> float:
    """연속 실패에 붙이는 추가 대기 — 페이서 위에 얹는 보호막이다."""
    return min(BACKOFF_BASE_S * (2 ** max(failure_count - 1, 0)), BACKOFF_MAX_S)


async def run_cell(
    *,
    llm: LLMClient,
    cell: Cell,
    n: int,
    tier: str,
    attempt_multiplier: int,
    judgment_settings: JudgmentSettings,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    leg_head_suppression: bool = False,
    leg_generic_heads: frozenset[str] = frozenset(),
    leg_condition_terms: frozenset[str] = frozenset(),
    attr_axis_suppression: bool = False,
    attr_constraint_axes: frozenset[str] = frozenset(),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    union_enabled: bool = False,
    union_llm: Any | None = None,
    union_settings: Settings | None = None,
    dedicated_llm: LLMClient | None = None,
    dedicated_settings: Settings | None = None,
    tri_generic_heads: frozenset[str] = frozenset(),
    tri_condition_terms: frozenset[str] = frozenset(),
) -> CellResult:
    """성공 표본 N개를 채울 때까지 재시도한다.

    [R2-1, legs_probe 승계] 예산 초과(`BudgetExceeded`)는 밖으로 던지지 않는다 — 잡아서 지금까지
    모은 `CellResult` 를 그대로 보존한 채 이 셀의 재시도만 멈춘다. 예외가 `run_probe`
    (`asyncio.gather`)까지 올라가면 완주하지 못한 다른 셀의 결과가 통째로 사라져 거짓 부분
    산출물이 나온다 — 그래서 이 함수는 항상 정상 반환하고, 예산 소진 여부는 호출부가
    `BudgetTracker.snapshot()` 으로 사후 판정한다.
    """
    anchor = cell.anchor
    # [§D5] priorExists 앵커는 decompose·판정에 같은 prior(빈 멀티턴 상태)를 넘긴다.
    prior = ProductSearchFilters() if anchor.prior_exists else None
    dedicated_call_gate = (
        dedicated_llm is not None
        and dedicated_settings is not None
        and prior is None
        and could_be_underspecified_message(anchor.utterance)
    )
    dedicated_prompt = dedicated_settings is not None and (prior is not None or dedicated_call_gate)
    result = CellResult(cell_id=cell.cell_id, case_id=anchor.case_id, slice=anchor.slice)
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            decision = await decompose(
                llm,
                query=anchor.utterance,
                prior_filters=prior,
                profile_summary=None,
                tier=tier,
                last_recommendations=None,
                pending_cart=None,
                screen=None,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                leg_head_suppression=leg_head_suppression,
                leg_generic_heads=leg_generic_heads,
                leg_condition_terms=leg_condition_terms,
                attr_axis_suppression=attr_axis_suppression,
                attr_constraint_axes=attr_constraint_axes,
                dedicated_underspecified_classifier=dedicated_prompt,
            )
        except BudgetExceeded as exc:
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            break  # [R2-1] 이 셀은 그만 — 남은 표본은 예산 소진으로 채우지 못했다
        except Exception as exc:  # provider·파싱 실패 전부 — 표본으로 세지 않는다
            result.failures.append(
                FailureRecord(
                    cell_id=cell.cell_id,
                    attempt=result.attempts,
                    error_type=type(exc).__name__,
                    message=scrub_message(str(exc))[:200],
                )
            )
            await sleep(backoff_seconds(len(result.failures)))
            continue
        before_verdict = is_underspecified_turn(decision, prior, judgment_settings)
        post_decision, _ = postprocessed_decision(
            decision, generic_heads=tri_generic_heads, condition_terms=tri_condition_terms
        )
        postprocess_verdict = is_underspecified_turn(post_decision, prior, judgment_settings)
        dedicated_called = dedicated_disagreed = dedicated_failed = dedicated_ambiguous = False
        if dedicated_call_gate:
            dedicated_called = True
            try:
                verdict = await classify_underspecified(
                    dedicated_llm, message=anchor.utterance, settings=dedicated_settings
                )
                clone = _clone_decision(decision)
                if apply_underspecified_classification(
                    clone, message=anchor.utterance, verdict=verdict
                ):
                    dedicated_disagreed = is_underspecified_turn(
                        clone, prior, judgment_settings
                    ) != is_underspecified_turn(decision, prior, judgment_settings)
                    decision = clone
            except Exception:
                dedicated_failed = True
        union_result = None
        if union_enabled:
            # [#432] 순환 임포트 회피(LAZY) — union.py 가 이 파일의 _clone_decision 등을 쓴다.
            if union_llm is not None and union_settings is not None:
                from evals.underspecified_probe.union import run_union_stage

                union_result = await run_union_stage(
                    decision=decision,
                    prior=prior,
                    utterance=anchor.utterance,
                    llm=union_llm,
                    settings=union_settings,
                    judgment_settings=judgment_settings,
                    # 표본마다 유일해야 한다 — 스레드 상태(멀티턴 필터·되돌리기)가 표본 간에
                    # 새면 뒤 표본이 앞 표본의 승계를 오염된 채 받는다(§2-2 인자표).
                    thread_key=f"union:{cell.cell_id}:{result.attempts}",
                )
            else:
                # [§2-6 항목5] --dry-run 은 pg·임베딩 접근이 0 이어야 한다 — 실 호출 대신
                # "건너뜀" 표시만 남긴다(배관 확인 전용, 실측 아님).
                from evals.underspecified_probe.union import skipped_union_result

                union_result = skipped_union_result(
                    "dry-run: union 단계 건너뜀 — pg 접근 0(packet-432 §2-6 항목5)"
                )
        result.samples.append(
            Sample.from_decision(
                decision,
                case_id=anchor.case_id,
                sample_index=len(result.samples),
                prior=prior,
                judgment_settings=judgment_settings,
                latency_ms=int(round((perf_counter() - started) * 1000)),
                union=union_result,
                dedicated_called=dedicated_called,
                dedicated_disagreed=dedicated_disagreed,
                dedicated_failed=dedicated_failed,
                dedicated_ambiguous=dedicated_ambiguous,
                before_verdict=before_verdict,
                postprocess_verdict=postprocess_verdict,
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    llm: LLMClient,
    cells: list[Cell],
    n: int,
    tier: str,
    attempt_multiplier: int,
    judgment_settings: JudgmentSettings,
    concurrency: int = 1,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    leg_head_suppression: bool = False,
    leg_generic_heads: frozenset[str] = frozenset(),
    leg_condition_terms: frozenset[str] = frozenset(),
    attr_axis_suppression: bool = False,
    attr_constraint_axes: frozenset[str] = frozenset(),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_cell_done: Callable[[CellResult], None] | None = None,
    union_enabled: bool = False,
    union_llm: Any | None = None,
    union_settings: Settings | None = None,
    dedicated_llm: LLMClient | None = None,
    dedicated_settings: Settings | None = None,
    tri_generic_heads: frozenset[str] = frozenset(),
    tri_condition_terms: frozenset[str] = frozenset(),
) -> list[CellResult]:
    """모든 셀을 돌린다. 결과는 항상 cellId 정렬이라 동시성이 순서를 바꾸지 않는다."""
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(cell: Cell) -> CellResult:
        async with semaphore:
            result = await run_cell(
                llm=llm,
                cell=cell,
                n=n,
                tier=tier,
                attempt_multiplier=attempt_multiplier,
                judgment_settings=judgment_settings,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                leg_head_suppression=leg_head_suppression,
                leg_generic_heads=leg_generic_heads,
                leg_condition_terms=leg_condition_terms,
                attr_axis_suppression=attr_axis_suppression,
                attr_constraint_axes=attr_constraint_axes,
                sleep=sleep,
                union_enabled=union_enabled,
                union_llm=union_llm,
                union_settings=union_settings,
                dedicated_llm=dedicated_llm,
                dedicated_settings=dedicated_settings,
                tri_generic_heads=tri_generic_heads,
                tri_condition_terms=tri_condition_terms,
            )
        if on_cell_done is not None:
            on_cell_done(result)
        return result

    results = await asyncio.gather(*(_one(cell) for cell in cells))
    return sorted(results, key=lambda result: result.cell_id)


def unfilled_cells(results: list[CellResult], *, n: int) -> list[dict[str, Any]]:
    """못 채운 셀 목록 — 리포트와 종료 코드 4의 근거."""
    return [
        {
            "cellId": result.cell_id,
            "got": len(result.samples),
            "want": n,
            "attempts": result.attempts,
            "errorTypes": sorted({failure.error_type for failure in result.failures}),
        }
        for result in results
        if not result.filled
    ]
