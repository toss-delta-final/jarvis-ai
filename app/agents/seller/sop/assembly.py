"""SOP 스텝 조립 (이슈 #598, design-598 §3-1) — 워커별 `Sop` 인스턴스를 구성한다.

7스텝 공통 프레임: `load → compare → compute → validate → feedback → interpret →
verify`(`sop/__init__.py` 문서화 순서 + `verify` 신설). `compute` 뿐 아니라
`load`/`compare` 도 워커별로 다르다(Spring 조회 대상이 다르다) — 그래서 4워커 전부
이 모듈 하나에서 클로저로 조립한다.

[load/compare 분리 — 왜 나누는가]
`load` 는 **현재 기간**의 원자료를, `compare` 는 **비교 기준 기간**의 원자료를
가져온다 — 어느 쪽도 값을 판정하지 않는다(LLM 0회·판정 0회). 판정은 전부
`compute`(`sop/compute/*.py`, 기존·무접촉)가 한다.

[엔진 계약 — 왜 클로저인가]
`engine.StepFn` 은 `Callable[[AnalysisContext], Awaitable[None]]` 이다 — ctx 하나만
받고 반환값이 없다. 그런데 `compute_conversion`·`compute_churn` 등은 `SalesResult`·
`FunnelResult`·`SnapshotRecord` 같은 추가 인자가 필요하다(ctx 는 이런 원시 응답을
담지 않는다 — "LLM 이 보는 유일한 입력" 규약, `context.py`). `validate.py` 자체
docstring이 이미 예고한 필요("결과를 받아 두는 얇은 어댑터")를 `_Box`(클로저로
캡처되는 가변 상자)로 구현한다 — `load`/`compare` 가 채우고 `compute`/`validate` 가
읽는다.

[Spring 실패 처리]
`get_spring_client()` 호출이 `SpringUnavailableError` 를 던지면 여기서 잡지 않고
그대로 올린다 — `run_sop` 의 `except Exception` 이 `Hold` 로 흡수하고 그 워커는
`required=True` 스텝 실패로 판정 보류된다(§9-R1 과 같은 degrade 철학, "브랜치 실패는
degrade 유지").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.agents.seller import analysis_store
from app.agents.seller.schemas import AnalysisType
from app.agents.seller.sop.compute.behavior import compute_behavior
from app.agents.seller.sop.compute.churn import compute_churn
from app.agents.seller.sop.compute.conversion import compute_conversion
from app.agents.seller.sop.compute.sales_anomaly import compute_sales_anomaly
from app.agents.seller.sop.context import AnalysisContext
from app.agents.seller.sop.engine import Sop, Step
from app.agents.seller.sop.interpret import interpret_step
from app.agents.seller.sop.validate import ValidationResult, validate_context
from app.agents.seller.sop.verify import verify_step
from app.core.config import Settings
from app.schemas.spring import ChurnResult, FunnelResult, SalesResult
from app.services.spring_client import get_spring_client


@dataclass
class _Box:
    """load/compare/compute/validate 스텝 간 클로저 캡처용 가변 상자.

    `run_sop` 이후 `resident.py` 가 `validation` 을 읽어 V2-d(citable_dates) 재료로
    쓴다 — `ValidationResult` 는 `AnalysisContext` 에 필드를 두지 않기로 한
    설계(`validate.py` 참조)라, 이 상자가 그 값을 워커 밖으로 들고 나가는 유일한
    통로다.
    """

    raw: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult | None = None


def _iso(value: date) -> str:
    return value.isoformat()


async def _load_sales(ctx: AnalysisContext, settings: Settings) -> SalesResult:
    """I-6 매출 시계열 — STL 이 필요한 lookback 만큼 앞당겨 조회한다(`tools.py` 선례)."""
    fetch_from = ctx.period_from - timedelta(days=settings.seller_analysis_lookback_days)
    return await get_spring_client().get_sales(
        ctx.brand_id, _iso(fetch_from), _iso(ctx.period_to), "daily"
    )


async def _load_funnel(brand_id: int, period_from: date, period_to: date) -> FunnelResult:
    return await get_spring_client().get_funnel(brand_id, _iso(period_from), _iso(period_to))


async def _load_churn(
    brand_id: int, period_from: date, period_to: date, settings: Settings
) -> ChurnResult:
    return await get_spring_client().get_churn(
        brand_id, _iso(period_from), _iso(period_to), settings.seller_churn_inactive_days
    )


def _conversion_baseline_period(ctx: AnalysisContext) -> tuple[date, date]:
    """`compute_conversion` 기본값과 동일한 산식(인접 직전 동일 길이 구간) — 실제
    조회에 쓸 날짜를 먼저 확정해 `compute_conversion` 에는 이미 정해진 값을 넘긴다
    (계산이 두 곳에서 갈리는 것을 막는다)."""
    span = ctx.period_to - ctx.period_from
    baseline_to = ctx.period_from - timedelta(days=1)
    baseline_from = baseline_to - span
    return baseline_from, baseline_to


def build_sop(worker: AnalysisType, *, settings: Settings) -> tuple[Sop, _Box]:
    """워커 1종의 `Sop` 을 조립한다. 반환된 `_Box` 는 `run_sop` 이후에도 살아 있다."""
    box = _Box()

    async def load_step(ctx: AnalysisContext) -> None:
        if worker == "sales_anomaly":
            box.raw["sales"] = await _load_sales(ctx, settings)
        elif worker == "conversion":
            box.raw["current"] = await _load_funnel(ctx.brand_id, ctx.period_from, ctx.period_to)
        elif worker in ("behavior", "churn"):
            snapshot = await analysis_store.load_latest_snapshot(ctx.brand_id)
            if snapshot is None:
                raise RuntimeError(
                    f"no_snapshot: brand_id={ctx.brand_id} 의 고객 피처 스냅샷이 없다"
                    " (04-CLUSTERING 배치가 아직 돌지 않았거나 신규 브랜드)"
                )
            box.raw["snapshot"] = snapshot
            if worker == "churn":
                box.raw["churn_now"] = await _load_churn(
                    ctx.brand_id, ctx.period_from, ctx.period_to, settings
                )

    async def compare_step(ctx: AnalysisContext) -> None:
        if worker == "conversion":
            baseline_from, baseline_to = _conversion_baseline_period(ctx)
            box.raw["baseline"] = await _load_funnel(ctx.brand_id, baseline_from, baseline_to)
            box.raw["baseline_from"] = baseline_from
            box.raw["baseline_to"] = baseline_to
        elif worker == "churn":
            offset = timedelta(days=settings.seller_baseline_offset_days)
            baseline_snapshot = await analysis_store.load_snapshot_at(
                ctx.brand_id, ctx.period_to - offset
            )
            box.raw["baseline_snapshot"] = baseline_snapshot
            if baseline_snapshot is not None:
                churn_from = baseline_snapshot.period_from
                churn_to = baseline_snapshot.period_to
            else:
                churn_from = ctx.period_from - offset
                churn_to = ctx.period_to - offset
            box.raw["churn_prev"] = await _load_churn(ctx.brand_id, churn_from, churn_to, settings)
        # sales_anomaly·behavior 는 비교 기준 원자료가 필요 없다(STL 은 lookback 으로
        # 이미 처리했고, 세그먼트 이동은 churn 워커 소관이다) — no-op.

    async def compute_step(ctx: AnalysisContext) -> None:
        if worker == "sales_anomaly":
            compute_sales_anomaly(ctx, box.raw["sales"], settings=settings)
        elif worker == "conversion":
            compute_conversion(
                ctx,
                box.raw["current"],
                box.raw["baseline"],
                baseline_from=box.raw.get("baseline_from"),
                baseline_to=box.raw.get("baseline_to"),
                settings=settings,
            )
        elif worker == "behavior":
            compute_behavior(ctx, box.raw["snapshot"], settings=settings)
        elif worker == "churn":
            compute_churn(
                ctx,
                current=box.raw["snapshot"],
                baseline=box.raw.get("baseline_snapshot"),
                churn_now=box.raw.get("churn_now"),
                churn_prev=box.raw.get("churn_prev"),
                settings=settings,
            )

    async def validate_step(ctx: AnalysisContext) -> None:
        result = validate_context(
            ctx,
            settings=settings,
            current_snapshot=box.raw.get("snapshot"),
            baseline_snapshot=box.raw.get("baseline_snapshot"),
        )
        box.validation = result
        if result.blocked:
            # `validate_context` 는 이미 `ctx.holds` 에 상세 사유를 남겼다(period_reversed·
            # no_material) — 이 예외는 `run_sop` 이 후속 스텝(feedback·interpret·verify)을
            # 건너뛰게 하는 신호일 뿐이라 메시지를 짧게 둔다(상세 사유의 중복이 아니다).
            raise RuntimeError("validate_blocked: 격리 후 재료가 없거나 기간이 역전됐다")

    async def feedback_step(ctx: AnalysisContext) -> None:
        """과거 액션 성과 조회 — v1 은 항상 빈 목록(자리만 확보, 후속 이슈가 채운다)."""
        return

    steps = (
        Step("load", load_step, settings.seller_sop_load_timeout_s),
        Step("compare", compare_step, settings.seller_sop_compare_timeout_s),
        Step("compute", compute_step, settings.seller_sop_compute_timeout_s),
        Step("validate", validate_step, settings.seller_sop_validate_timeout_s),
        Step("feedback", feedback_step, settings.seller_sop_feedback_timeout_s, required=False),
        Step("interpret", interpret_step, settings.seller_sop_interpret_timeout_s),
        Step("verify", verify_step, settings.seller_sop_verify_timeout_s),
    )
    return Sop(worker=worker, steps=steps), box
