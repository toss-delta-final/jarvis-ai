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
from app.agents.buyer.recommendation.decompose import decompose, resolve_category_action
from app.agents.buyer.recommendation.state import RouteDecision
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

    @classmethod
    def from_decision(
        cls,
        decision: RouteDecision,
        *,
        cell_id: str,
        sample_index: int,
        latency_ms: int,
        scope_free: bool | None = None,
        prior_echo_tokens: frozenset[str] = frozenset(),
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
            ),
            category_legs_echo_prior=_legs_echo_prior(decision.category_queries, prior_echo_tokens),
            category_legs=serialize_category_legs(decision.category_queries),
        )


def _normalize_echo_token(value: str | None) -> str:
    """에코 비교용 정규화 — 공백 접기 + 소문자 (#84, 2차 리뷰 F-4)."""
    return " ".join((value or "").split()).lower()


def _legs_echo_prior(queries, tokens: frozenset[str]) -> bool:  # noqa: ANN001
    """이번 턴 leg 이 **전부** 직전 카테고리를 되풀이하는가 (#84).

    **부분 문자열이 아니라 정규화 후 정확 일치**로 본다(2차 리뷰 F-4). 초판은 `"이어폰"` 이
    들어 있기만 하면 에코로 셌는데, 그러면 leg query 가 `"이어폰 케이스"` 여도 에코가 돼
    **카테고리가 바뀐 턴을 "유지됐다"로 세는** 축이 된다(배포 매퍼는 그것을 액세서리 카테고리로
    바꿀 수 있다). `docs/lessons.md` [2026-08-02] 「부분 문자열 매칭은 포함 방향마다 의미가
    다르다」가 가리키는 함정이라 정확 일치로 좁혔다.

    비교 집합은 앵커의 `categoryPriorFilters` 에서 만든다(`_prior_echo_tokens`) — 카테고리 전체·
    각 조각(잎 포함)·`semanticQuery`. 실측 표본이 `("음향가전 > 이어폰", "무선 이어폰")` 과
    `("음향가전", "무선 이어폰")` 두 모양이라 이 집합이면 전부 잡힌다.

    **모든** leg 가 에코일 때만 에코다 — 하나라도 새 상품이면 카테고리가 유지된 것이 아니다.
    leg 이 하나도 없으면 False("에코했다"가 아니라 "신호가 없다"이고, 그 경우는 확정값이 이미
    carry 라 보정할 것이 없다).
    """
    if not tokens or not queries:
        return False
    return all(
        _normalize_echo_token(query.raw_category) in tokens
        or _normalize_echo_token(query.query) in tokens
        for query in queries
    )


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
    """에코 판정 비교 집합 — 앵커 `categoryPriorFilters` 에서만 만든다 (#84, F-4).

    담는 것: `category` **전체**(`"음향가전 > 이어폰"`) · `>` 로 나눈 **각 조각**(잎 `"이어폰"` 과
    상위 `"음향가전"`) · `semanticQuery`(`"무선 이어폰"`). 전부 `_normalize_echo_token` 으로
    정규화해 담고, 비교도 정규화 후 **정확 일치**다.

    앵커 밖의 문자열을 쓰지 않으므로 정답지를 바꾸면 판정도 함께 따라온다(픽스처가 유일한
    입력이라는 이 하네스의 규약).
    """
    prior = anchors.category_prior_filters
    category = str(prior.get("category") or "")
    candidates = [category, *category.split(">"), str(prior.get("semanticQuery") or "")]
    return frozenset(
        token for token in (_normalize_echo_token(c) for c in candidates) if len(token) >= 2
    )


def backoff_seconds(failure_count: int) -> float:
    """연속 실패에 붙이는 추가 대기 — 페이서 위에 얹는 보호막이다."""
    return min(BACKOFF_BASE_S * (2 ** max(failure_count - 1, 0)), BACKOFF_MAX_S)


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
    context_kwargs = build_context_kwargs(anchors, cell.context)
    # [#84] 배포 경로는 decompose 와 **전용 분류기**를 함께 부른다 — 프로브가 decompose 만 부르면
    # 측정이 배포와 갈라진다(lessons 「재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다」).
    # 발동 조건도 `graph.py` 의 게이트와 같다: 직전 카테고리가 있는 컨텍스트일 때만.
    prior_filters = context_kwargs.get("prior_filters")
    # [#84·G-1] `--no-classifier` 런은 이 팔을 통째로 끈다(= 앱의
    # `CATEGORY_SCOPE_CLASSIFIER_ENABLED=false` 와 같은 상태). **앱 설정을 읽지 않는다** — 프로브는
    # 환경이 아니라 **인자**로 조건을 고정해야 재현된다. 기준선 README 의 `before1`(결함 재현) 열이
    # 이 플래그로 다시 만들어진다.
    prior_category = (getattr(prior_filters, "category", None) or "") if classifier_enabled else ""
    prior_echo_tokens = _prior_echo_tokens(anchors) if prior_category else frozenset()
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
        result.samples.append(
            Sample.from_decision(
                decision,
                cell_id=cell.cell_id,
                sample_index=len(result.samples),
                latency_ms=int(round((perf_counter() - started) * 1000)),
                scope_free=scope_free,
                prior_echo_tokens=prior_echo_tokens,
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
