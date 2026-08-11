"""셀별 N 채우기 루프 — 이 프로브의 심장.

원칙 하나: **실패는 표본이 아니다.** 429·타임아웃·파싱 실패는 버리고 다시 호출해 성공 N개를
채운다. 못 채운 셀은 숨기지 않고 산출물에 드러내 종료 코드 4로 알린다 — 굶은 런이 깨끗한 런으로
위장하면 그 표를 근거로 잘못된 채택 판정이 나온다(#240).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.agents.buyer.recommendation.category_scope import classify_category_scope
from app.agents.buyer.recommendation.decompose import (
    decompose,
    has_new_category_signal,
    is_prior_echo_leg,
    prior_echo_tokens,
    resolve_category_action,
)
from app.agents.buyer.recommendation.state import RouteDecision
from app.agents.buyer.recommendation.underspecified_classifier import (
    apply_underspecified_classification,
    classify_underspecified,
    could_be_underspecified_message,
)
from app.agents.buyer.screen_reference import resolve_screen_reference
from app.core.config import get_settings
from app.core.llm import LLMClient
from evals.intent_probe.loader import Cell, build_context_kwargs
from evals.intent_probe.schema import AnchorSet
from evals.model_eval.budget import BudgetExceeded

BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0

# provider 오류 메시지에 섞여 오는 계정·키 식별자. 산출물은 리포에 커밋되므로 지우고 남긴다
# (429 본문에 org id 가 그대로 들어온다 — 실측에서 확인).
_IDENTIFIER_RE = re.compile(r"\b(org|proj|user|sk)-[A-Za-z0-9_-]{6,}")


def scrub_message(message: str) -> str:
    """실패 메시지에서 계정 식별자를 지운다 — 원인 판별에 필요한 문구는 남긴다."""
    return _IDENTIFIER_RE.sub(lambda match: f"{match.group(1)}-***", message)


@dataclass(frozen=True)
class Sample:
    """성공한 호출 1건. 실패는 여기 들어오지 않는다."""

    cell_id: str
    sample_index: int
    intent: str
    product_id: int | None
    option_id: int | None
    quantity: int | None
    case: int
    scoped_to_previous: bool
    latency_ms: int
    # [#84] 카테고리 승계 3분기. `scope_free` 는 **전용 분류기**(category_scope) 산출,
    # `resolved_category_action` 은 그래프 가드가 실제로 쓰는 확정값이다. 둘을 함께 남겨야
    # "분류기는 True 인데 확정값이 다르다" 같은 조합을 사후에 갈라볼 수 있다.
    has_category_signal: bool
    scope_free: bool | None
    resolved_category_action: str
    # 이번 턴 leg 이 전부 **직전 카테고리 에코**인가. 프롬프트가 리파인 턴에 직전 카테고리를
    # categoryQueries 로 복사하라고 지시하므로, 확정값이 `replace` 여도 결과적으로 카테고리가
    # 유지된다 — 그것을 오답으로 세면 축이 정상 동작을 실패로 읽는다(2차 리뷰 P2).
    category_legs_echo_prior: bool
    # leg 원문(`raw|query` 를 `;` 로 이어 붙인 것) — 판정 규칙이 바뀌어도 **런을 다시 돌리지 않고**
    # 재집계할 수 있게 남긴다(F-4). 이번 라운드가 그게 없어서 재측정이 필요해진 사례다.
    category_legs: str
    # [#300] screen 지시어 해소(#118) — **출고 배선의 최종값**. `product_id` 는 위에서 원본
    # decompose 산출 그대로 두고(F-4 규약 — 판정 규칙이 바뀌어도 런을 다시 돌리지 않고 재집계할
    # 수 있어야 한다), 이 필드가 `resolve_screen_reference` 를 통과한 뒤의 값이다. 사용자가 겪는
    # 동작은 이쪽이라 screen 축은 이 필드로 채점한다(§4.4).
    resolved_product_id: int | None
    # 해소기가 실제로 발동했는가(반환값이 `None` 이 아니었는가) — `None` 발동은 강제 되물음이다.
    screen_resolver_fired: bool
    screen_resolution_reason: str | None

    @classmethod
    def from_decision(
        cls,
        decision: RouteDecision,
        *,
        cell_id: str,
        sample_index: int,
        latency_ms: int,
        scope_free: bool | None = None,
        echo_tokens: frozenset[str] = frozenset(),
        resolved_product_id: int | None,
        screen_resolver_fired: bool,
        screen_resolution_reason: str | None,
    ) -> "Sample":
        cart = decision.cart
        # 판정 규칙을 **여기서 재구현하지 않는다** — 배포 경로(`graph.py` 승계 가드)와 같은 함수를
        # 그대로 부른다. 재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다(lessons 2026-08-04).
        has_category_signal = any(
            query.raw_category or query.query for query in decision.category_queries
        )
        return cls(
            cell_id=cell_id,
            sample_index=sample_index,
            intent=decision.intent,
            product_id=cart.product_id if cart else None,
            option_id=cart.option_id if cart else None,
            quantity=cart.quantity if cart else None,
            case=decision.case,
            scoped_to_previous=decision.scoped_to_previous,
            latency_ms=latency_ms,
            has_category_signal=has_category_signal,
            scope_free=scope_free,
            resolved_category_action=resolve_category_action(
                has_category_signal=has_category_signal,
                scope_free=scope_free,
                has_new_category_signal=has_new_category_signal(
                    decision.category_queries, echo_tokens
                ),
            ),
            category_legs_echo_prior=_legs_echo_prior(decision.category_queries, echo_tokens),
            category_legs=serialize_category_legs(decision.category_queries),
            category_leg_injected=decision.category_leg_injected,
            resolved_product_id=resolved_product_id,
            screen_resolver_fired=screen_resolver_fired,
            screen_resolution_reason=screen_resolution_reason,
        )

    # [#443] 모델 산출과 구분한 사전 보강 발동 여부 — condition_only 하드 불변식을 samples.csv
    # 재집계만으로 판정할 수 있어야 한다. **기본값을 둔다** — 이 필드가 없던 시절에 쓰인 생성자
    # 호출(테스트 픽스처 다수)이 전부 깨지고, 진단 필드라 "발동 안 함"이 안전한 기본값이다.
    category_leg_injected: bool = False


def _legs_echo_prior(queries, tokens: frozenset[str]) -> bool:  # noqa: ANN001
    """이번 턴 leg 이 **전부** 직전 카테고리를 되풀이하는가 (#84).

    판정 자체는 **배포 경로와 같은 함수**(`decompose.is_prior_echo_leg`)를 부른다 — 규칙을 여기서
    다시 쓰면 측정과 배포가 갈라진다(lessons 「재현이 틀리면 그 위의 모든 측정과 인과가 함께
    틀린다」). 그 함수가 정규화 후 **정확 일치**로 보는 이유와 채워진 필드를 **전부** 요구하는
    이유는 그쪽 docstring 에 있다.

    **모든** leg 가 에코일 때만 에코다 — 하나라도 새 상품이면 카테고리가 유지된 것이 아니다.
    leg 이 하나도 없으면 False("에코했다"가 아니라 "신호가 없다"이고, 그 경우는 확정값이 이미
    carry 라 보정할 것이 없다).
    """
    if not tokens or not queries:
        return False
    return all(is_prior_echo_leg(query, tokens) for query in queries)


def serialize_category_legs(queries) -> str:  # noqa: ANN001
    """leg 원문을 `samples.csv` 한 칸에 담는다 — `raw|query` 를 `;` 로 이어 붙인다 (F-4).

    판정 규칙을 나중에 바꿀 때 **런을 다시 돌리지 않고 재집계**할 수 있어야 한다. 이번 라운드가
    바로 그 상황이었다(에코 판정을 좁히자 기존 표를 재사용할 수 없었다).
    """
    return ";".join(f"{query.raw_category or ''}|{query.query or ''}" for query in queries)


@dataclass(frozen=True)
class FailureRecord:
    """버린 시도 1건. 표본이 아니라 '몇 번 만에 채웠나'의 근거다."""

    cell_id: str
    attempt: int
    error_type: str
    message: str


@dataclass
class CellResult:
    """한 셀의 측정 결과."""

    cell_id: str
    utterance_id: str
    context_id: str
    group: str
    samples: list[Sample] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    attempts: int = 0
    filled: bool = False

    def intent_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sample in self.samples:
            counts[sample.intent] = counts.get(sample.intent, 0) + 1
        return dict(sorted(counts.items()))


def _prior_echo_tokens(anchors: AnchorSet) -> frozenset[str]:
    """에코 판정 비교 집합 — 앵커 `categoryPriorFilters` 를 배포 함수에 그대로 넘긴다 (#84).

    집합을 만드는 규칙은 `decompose.prior_echo_tokens` 한 곳에만 있다. 여기서는 **앵커에서
    입력을 뽑는 일**만 한다 — 앵커 밖의 문자열을 쓰지 않으므로 정답지를 바꾸면 판정도 함께
    따라온다(픽스처가 유일한 입력이라는 이 하네스의 규약).
    """
    prior = anchors.category_prior_filters
    return prior_echo_tokens(
        category=str(prior.get("category") or ""),
        semantic_query=str(prior.get("semanticQuery") or ""),
    )


def backoff_seconds(failure_count: int) -> float:
    """연속 실패에 붙이는 추가 대기 — 페이서 위에 얹는 보호막이다."""
    return min(BACKOFF_BASE_S * (2 ** max(failure_count - 1, 0)), BACKOFF_MAX_S)


def _resolve_screen(
    *,
    utterance_text: str,
    intent: str,
    context_kwargs: dict[str, Any],
    settings: Any,
    decompose_product_id: int | None,
) -> tuple[int | None, bool, str | None]:
    """screen 지시어를 **러너가** 해소한다 — `graph.py` 의 cart_add 분기와 같은 조건·같은 인자다(§4.3).

    D-1: 이 하네스의 확립된 규약은 "판정 규칙을 프로브가 재구현하지 않고 배포 경로와 같은 함수를
    같은 순서로 부른다"다(#84 가 `resolve_category_action` 으로 이미 그렇게 한다). #118 의 채택
    근거 수치(48/48)도 `decompose` 다음 `resolve_screen_reference` 로 잰 값이라, 별도 채점 경로를
    두면 그 수치와 비교 자체가 불가능해진다.

    발동 조건은 `graph.py` 와 같다: **intent 가 이미 cart_add** 이고(전체 해소기 호출이 그
    분기 안에서만 일어난다, `graph.py:906` 참조), `screen` 이 있고 `screen.products` 가 비지 않고
    `pending_cart is None` 일 때만. 조건 미성립이면 `(decompose_product_id, False, None)` —
    resolved == 원본, 발동 안 함.

    **[#571 이후 측정 범위 축소, 의도적]** 배포 경로(`app/agents/buyer/graph.py`)는 #571 부터
    `screen.products` 뿐 아니라 **추천 카드 표면**(`last_reco[:turn_count]`, `screen` 없어도
    돈다)에서도 이 해소기를 부른다 — 위 "발동 조건은 `graph.py` 와 같다"는 이제 **화면 표면에
    한해서만** 참이다. 이 하네스는 그 확장을 재지 않는다: 픽스처(`Cell`/`build_context_kwargs`)
    에 `ordinal_span`(표시 순서=저장 순서 증명, §571) 개념이 없어 추천 표면 재현이 불가능하고,
    설령 넣더라도 기존 `screen` 계열 baseline(`evals/intent_probe/baselines/`)과의 산출
    비교 가능성이 깨진다 — 축소는 조용히 둔 것이 아니라 이 트레이드오프를 택한 결과다.
    """
    screen = context_kwargs.get("screen")
    pending_cart = context_kwargs.get("pending_cart")
    if intent != "cart_add" or screen is None or not screen.products or pending_cart is not None:
        return decompose_product_id, False, None
    last_recommendations = context_kwargs.get("last_recommendations") or []
    allowed = {pid for pid, _ in last_recommendations} | {pid for pid, _ in screen.products}
    resolved = resolve_screen_reference(
        utterance_text,
        products=list(screen.products),
        columns=screen.columns,
        allowed_product_ids=allowed,
        deictic_markers=settings.screen_deictic_markers,
        context_reference_markers=settings.screen_context_reference_markers,
        last_recommendation_products=last_recommendations,
        # [#571] 이 하네스는 `screen.products` 표면만 잰다(위 게이트가 그것만 통과시킨다) —
        # 화면 표면은 순번 게이트가 항상 참이고 이름 확정(N)은 꺼져 있다(`graph.py` 의
        # `screen_products` 분기와 같은 값, 오늘과 바이트 동일한 동작).
        positional_order_verified=True,
        name_confirmation_enabled=False,
        negation_markers=settings.utterance_negation_markers,
        prefix_negation_markers=settings.utterance_prefix_negation_markers,
    )
    if resolved is None:
        return decompose_product_id, False, None
    return resolved.product_id, True, resolved.reason


async def run_cell(
    *,
    llm: LLMClient,
    cell: Cell,
    anchors: AnchorSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    settings: Any = None,
    classifier_enabled: bool = True,
    underspecified_classifier_enabled: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CellResult:
    """성공 표본 N개를 채울 때까지 재시도한다. 예산 초과만 밖으로 던진다."""
    settings = settings if settings is not None else get_settings()
    result = CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group=cell.utterance.group,
    )
    context_kwargs = build_context_kwargs(anchors, cell.context, settings=settings)
    # [#84] 배포 경로는 decompose 와 **전용 분류기**를 함께 부른다 — 프로브가 decompose 만 부르면
    # 측정이 배포와 갈라진다(lessons 「재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다」).
    # 발동 조건도 `graph.py` 의 게이트와 같다: 직전 카테고리가 있는 컨텍스트일 때만.
    prior_filters = context_kwargs.get("prior_filters")
    # [#84·G-1] `--no-classifier` 런은 이 팔을 통째로 끈다(= 앱의
    # `CATEGORY_SCOPE_CLASSIFIER_ENABLED=false` 와 같은 상태). **앱 설정을 읽지 않는다** — 프로브는
    # 환경이 아니라 **인자**로 조건을 고정해야 재현된다. 기준선 README 의 `before1`(결함 재현) 열이
    # 이 플래그로 다시 만들어진다.
    prior_category = (getattr(prior_filters, "category", None) or "") if classifier_enabled else ""
    echo_tokens = _prior_echo_tokens(anchors) if prior_category else frozenset()
    underspecified_call_gate = (
        underspecified_classifier_enabled
        and prior_filters is None
        and context_kwargs.get("pending_cart") is None
        and not context_kwargs.get("last_recommendations")
        and context_kwargs.get("screen") is None
        and bool(cell.utterance.text.strip())
        and could_be_underspecified_message(cell.utterance.text)
    )
    dedicated_prompt = (
        underspecified_classifier_enabled
        and context_kwargs.get("pending_cart") is None
        and (
            prior_filters is not None
            or bool(context_kwargs.get("last_recommendations"))
            or context_kwargs.get("screen") is not None
            or underspecified_call_gate
        )
    )
    max_attempts = n * attempt_multiplier
    while len(result.samples) < n and result.attempts < max_attempts:
        result.attempts += 1
        started = perf_counter()
        try:
            decision = await decompose(
                llm,
                query=cell.utterance.text,
                profile_summary=None,
                tier=tier,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                category_leg_injection=settings.category_leg_injection_enabled,
                category_leg_injection_path=settings.category_leg_injection_path,
                category_leg_injection_min_length=settings.category_leg_injection_min_length,
                attr_axis_suppression=settings.attr_condition_axis_suppression_enabled,
                attr_constraint_axes=frozenset(settings.attr_condition_constraint_axes),
                dedicated_underspecified_classifier=dedicated_prompt,
                **context_kwargs,
            )
        except BudgetExceeded:
            raise
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
        # 분류기 호출은 **재시도 대상이 아니다** — 실패하면 None(=신호 없음)으로 떨어뜨린다.
        # 배포도 그렇게 하므로(보조 신호의 degrade), 여기서 재시도하면 프로브가 배포보다 관대한
        # 조건을 재게 된다. `classify_category_scope` 가 자기 예외를 이미 삼킨다.
        scope_free = None
        if prior_category:
            scope_free = await classify_category_scope(
                llm,
                message=cell.utterance.text,
                prior_category=prior_category,
                settings=settings,
            )
        # #463 배포 게이트와 같은 첫 무맥락 조건이다. 러너는 결과만 재므로 호출을 decompose와
        # 병렬화할 필요는 없지만, 분류 결과를 적용하는 순서와 상태 변형은 graph.py와 공유한다.
        underspecified = None
        if underspecified_call_gate:
            underspecified = await classify_underspecified(
                llm, message=cell.utterance.text, settings=settings
            )
        apply_underspecified_classification(
            decision, message=cell.utterance.text, verdict=underspecified
        )
        # [#300, D-1/§4.3] screen 지시어는 러너가 해소한다 — decompose 다음 `graph.py` cart_add
        # 분기와 같은 조건·같은 인자로 `resolve_screen_reference` 를 부른다.
        resolved_product_id, screen_resolver_fired, screen_resolution_reason = _resolve_screen(
            utterance_text=cell.utterance.text,
            intent=decision.intent,
            context_kwargs=context_kwargs,
            settings=settings,
            decompose_product_id=decision.cart.product_id if decision.cart else None,
        )
        result.samples.append(
            Sample.from_decision(
                decision,
                cell_id=cell.cell_id,
                sample_index=len(result.samples),
                latency_ms=int(round((perf_counter() - started) * 1000)),
                scope_free=scope_free,
                echo_tokens=echo_tokens,
                resolved_product_id=resolved_product_id,
                screen_resolver_fired=screen_resolver_fired,
                screen_resolution_reason=screen_resolution_reason,
            )
        )
    result.filled = len(result.samples) == n
    return result


async def run_probe(
    *,
    llm: LLMClient,
    cells: list[Cell],
    anchors: AnchorSet,
    n: int,
    tier: str,
    attempt_multiplier: int,
    concurrency: int = 1,
    category_fanout_max: int = 5,
    repurchase_max: int = 5,
    settings: Any = None,
    classifier_enabled: bool = True,
    underspecified_classifier_enabled: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_cell_done: Callable[[CellResult], None] | None = None,
) -> list[CellResult]:
    """모든 셀을 돌린다. 결과는 항상 cellId 정렬이라 동시성이 순서를 바꾸지 않는다."""
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def _one(cell: Cell) -> CellResult:
        async with semaphore:
            result = await run_cell(
                llm=llm,
                cell=cell,
                anchors=anchors,
                n=n,
                tier=tier,
                attempt_multiplier=attempt_multiplier,
                category_fanout_max=category_fanout_max,
                repurchase_max=repurchase_max,
                settings=settings,
                classifier_enabled=classifier_enabled,
                underspecified_classifier_enabled=underspecified_classifier_enabled,
                sleep=sleep,
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
