"""상주(무인) 분석 파이프라인 진입점 (이슈 #598, design-598 §3-8).

`run_analysis(...)` 하나만 공개한다 — 트리거·스케줄러·`mutation_lock` 중복실행
방지(10-TRIGGER.md 영역)는 별도 이슈 범위다(design-598 결정 4). 이 모듈은 그
콜러블 자체와 4워커 SOP 팬아웃 → findings 팬인 → 상주 보고서 검증 루프 → 상주
recommend → 저장까지만 담당한다.

[무인 실행 규약 — 이슈 본문 인용]
"실패는 로그+관측 이벤트, 판매자 미통지, 브랜치 실패는 degrade 유지." SSE 는 없다
(채팅 세션이 없다) — 모든 진행은 로그로만 남는다. 워커 1종 전체가 실패해도 나머지
워커로 부분 보고서를 만든다(`run_sop` 자체가 이미 이 규약을 보장한다, `engine.py`).

[채팅 레인 무접촉]
`REPORT_PROMPT`/`build_report_agent`/`orchestrator.run_report`·`build_recommend_agent`
/`RECOMMEND_TOOLS`/`RECOMMEND_PROMPT` 는 이 모듈에서 import 하지 않는다(design-598
§3-5 안 B 확정) — 대신 `RESIDENT_REPORT_PROMPT`/`build_resident_report_agent`·
`build_resident_recommend_agent` 를 쓴다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

from langchain_core.messages import HumanMessage

from app.agents.seller import analysis_store
from app.agents.seller.analysis_records import ReportRecord, RecommendationRecord, TriggerType
from app.agents.seller.context import SellerContext
from app.agents.seller.pipeline import (
    build_resident_report_title,
    format_report_input,
    format_judge_input,
    format_resident_recommend_input,
    format_rewrite_input,
    split_report_summary,
)
from app.agents.seller.schemas import AnalysisFinding, AnalysisType, RecommendationSet, ReportScore
from app.agents.seller.sop.assembly import build_sop
from app.agents.seller.sop.context import ActionCandidate, AnalysisContext
from app.agents.seller.sop.engine import run_sop
from app.agents.seller.sop.validate import ValidationResult
from app.agents.seller.verifier import run_deterministic_checks_v2, synthesize_grounding_finding
from app.agents.seller.workers import (
    build_report_judge,
    build_resident_recommend_agent,
    build_resident_report_agent,
)
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 상주 파이프라인이 조립하는 4워커 — abuse·review 는 SOP compute 미지원이라 범위 밖이다
# (design-598 §1 실측 — 이슈 본문의 6워커 언급과 실제 4워커 구현의 갈림, 사용자 확인).
RESIDENT_WORKERS: tuple[AnalysisType, ...] = ("behavior", "churn", "conversion", "sales_anomaly")


def _resident_context(brand_id: int) -> SellerContext:
    """상주 파이프라인은 JWT 가 없다 — zero-tool 에이전트라 `seller_id` 는 아무 도구도
    읽지 않는 자리표시자다(interpret 스텝과 동일 규약, `sop/interpret.py` 참조)."""
    return SellerContext(seller_id=0, brand_id=brand_id)


@dataclass(frozen=True)
class _WorkerRun:
    ctx: AnalysisContext
    validation: ValidationResult | None
    snapshot_id: object | None


async def _run_worker(
    worker: AnalysisType, brand_id: int, period_from: date, period_to: date, settings: Settings
) -> _WorkerRun:
    sop, box = build_sop(worker, settings=settings)
    ctx = AnalysisContext(
        worker=worker, brand_id=brand_id, period_from=period_from, period_to=period_to
    )
    ctx = await run_sop(sop, ctx)
    snapshot = box.raw.get("snapshot")
    snapshot_id = getattr(snapshot, "id", None)
    return _WorkerRun(ctx=ctx, validation=box.validation, snapshot_id=snapshot_id)


@dataclass(frozen=True)
class _VerifiedResidentReport:
    report: str
    passed: bool
    attempts: int
    last_score: ReportScore | None


def _content_to_text(content: object) -> str:
    """provider별 문자열·블록 메시지 content 를 텍스트로 정규화(orchestrator.py 관행 승계)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


async def _run_resident_report(
    findings: list[AnalysisFinding],
    *,
    synthesized: list[AnalysisFinding],
    holds: list[str],
    citable_dates: frozenset[date],
    context: SellerContext,
    settings: Settings,
) -> _VerifiedResidentReport:
    """상주 보고서 작성 → 검증(D1~D3+C1~C3+V2-d) → judge → ≤N회 재작성.

    `orchestrator.write_verified_report` 와 같은 루프 구조를 그대로 본떴다
    (design-598 §3-5) — 차이는 (1) `RESIDENT_REPORT_PROMPT`/`build_resident_report_agent`
    를 쓰고 (2) `run_deterministic_checks_v2`(C1~C3·V2-d 포함)를 쓰고 (3) SSE emit 이
    없고 (4) 글자수 상한(`seller_report_max_chars`)을 결정론 검사에 추가한다는 것.
    judge(`build_report_judge`/`JUDGE_PROMPT`)는 채팅 레인과 공유한다(design-598 §5
    "무접촉 확정" — 대상은 REPORT_PROMPT 계열뿐).
    """
    timeout_s = settings.seller_worker_timeout_s
    threshold = settings.seller_report_score_threshold
    max_attempts = settings.seller_report_max_retries
    max_chars = settings.seller_report_max_chars

    report_agent = build_resident_report_agent()
    judge_agent = build_report_judge()
    verification_findings = [*findings, *synthesized]

    report: str | None = None
    last_score: ReportScore | None = None
    feedback = ""

    for attempt in range(1, max_attempts + 1):
        message = (
            format_report_input(findings)
            if attempt == 1
            else format_rewrite_input(findings, report or "", feedback)
        )
        try:
            result = await asyncio.wait_for(
                report_agent.ainvoke(
                    {"messages": [HumanMessage(content=message)]}, context=context
                ),
                timeout=timeout_s,
            )
            report = _content_to_text(result["messages"][-1].content)
        except Exception as exc:
            if report is None:
                raise
            logger.warning("상주 보고서 재작성 %d회차 실패(%r) — 기존 보고서 미달 채택", attempt, exc)
            return _VerifiedResidentReport(report, False, attempt - 1, last_score)

        det_reasons = run_deterministic_checks_v2(
            report, verification_findings, holds=holds, citable_dates=citable_dates
        )
        if len(report) > max_chars:
            det_reasons.append(f"보고서가 {max_chars}자 상한을 넘었다({len(report)}자)")

        try:
            judge_result = await asyncio.wait_for(
                judge_agent.ainvoke(
                    {"messages": [HumanMessage(content=format_judge_input(findings, report))]},
                    context=context,
                ),
                timeout=timeout_s,
            )
            score = judge_result.get("structured_response")
            if not isinstance(score, ReportScore):
                raise TypeError("judge 가 ReportScore 를 반환하지 않았다")
        except Exception as exc:
            logger.warning("상주 judge %d회차 실패(%r) — 현재 보고서 미검증 채택", attempt, exc)
            return _VerifiedResidentReport(report, False, attempt, last_score)

        last_score = score
        if not det_reasons and score.total >= threshold:
            return _VerifiedResidentReport(report, True, attempt, score)

        feedback = "\n".join([*det_reasons, score.feedback])
        logger.info(
            "상주 보고서 검증 미달 %d회차 — 결정론 %d건, 점수 %d/%d",
            attempt,
            len(det_reasons),
            score.total,
            threshold,
        )

    logger.warning("상주 보고서 검증 %d회 미달 — 마지막 보고서 채택(degrade)", max_attempts)
    return _VerifiedResidentReport(report or "", False, max_attempts, last_score)


async def _run_resident_recommend(
    candidates: list[ActionCandidate], report: str, context: SellerContext, settings: Settings
) -> RecommendationSet:
    """상주 recommend 실행 — 후보가 없거나 실패하면 빈 추천으로 degrade(보고서는 산다).

    후보 생성기(design-598 §2 "이슈 12")가 아직 코드에 없으므로, `ctx.candidate_actions`
    가 항상 비어 있는 동안은 이 함수도 항상 빈 추천을 낸다 — 사용자 확인 결정 1
    ("과장 없음 방향으로 안전, 기능 저하는 정직하게 아는 채로 둔다")의 실행부다.
    """
    if not candidates:
        return RecommendationSet(recommendations=[], summary="이번 주기에는 추천 후보가 없습니다.")
    agent = build_resident_recommend_agent()
    try:
        result = await asyncio.wait_for(
            agent.ainvoke(
                {
                    "messages": [
                        HumanMessage(content=format_resident_recommend_input(candidates, report))
                    ]
                },
                context=context,
            ),
            timeout=settings.seller_worker_timeout_s,
        )
        recommendations = result.get("structured_response")
        if not isinstance(recommendations, RecommendationSet):
            raise TypeError("상주 recommend 가 RecommendationSet 을 반환하지 않았다")
        return recommendations
    except Exception as exc:
        logger.warning("상주 recommend 실패(%r) — 추천 없이 계속", exc)
        return RecommendationSet(recommendations=[], summary="")


async def run_analysis(
    brand_id: int, *, trigger_type: TriggerType, period_from: date, period_to: date
) -> None:
    """상주 분석 1회 실행 — 저장으로 귀결될 뿐 반환값이 없다(SSE 없음, 무인 실행).

    `period_from`/`period_to` 를 명시 인자로 받는다 — "일간이면 며칠, 주간이면 며칠"
    같은 트리거 정책은 design-598 결정 4로 범위 밖(10-TRIGGER 영역)이라, 이 콜러블은
    호출부가 이미 정한 기간을 받기만 한다(트리거 정책과 분석 실행을 분리).
    """
    settings = get_settings()
    context = _resident_context(brand_id)

    runs = await asyncio.gather(
        *(
            _run_worker(worker, brand_id, period_from, period_to, settings)
            for worker in RESIDENT_WORKERS
        ),
        return_exceptions=True,
    )

    worker_runs: list[_WorkerRun] = []
    for worker, result in zip(RESIDENT_WORKERS, runs, strict=True):
        if isinstance(result, BaseException):
            # `run_sop` 은 스텝 예외를 이미 Hold 로 흡수한다 — 여기 도달하는 예외는
            # SOP 엔진 밖(예: `AnalysisContext` 생성 자체 실패)이라 그 워커를 통째로
            # 건너뛴다. 다른 워커는 계속한다(브랜치 실패는 degrade 유지).
            logger.error("brand_id=%s 상주 워커 %s 파이프라인 실패(엔진 밖) — %r", brand_id, worker, result)
            continue
        worker_runs.append(result)

    findings = [f for run in worker_runs for f in run.ctx.findings]
    if not findings:
        logger.info(
            "brand_id=%s 상주 분석: 4워커 전부 서술할 finding 이 없어 보고서를 생성하지 않는다",
            brand_id,
        )
        return

    synthesized = [synthesize_grounding_finding(run.ctx) for run in worker_runs]
    holds = [f"{hold.step}: {hold.reason}" for run in worker_runs for hold in run.ctx.holds]
    citable_dates: frozenset[date] = frozenset().union(
        *(run.validation.citable_dates for run in worker_runs if run.validation is not None)
    )

    verified = await _run_resident_report(
        findings,
        synthesized=synthesized,
        holds=holds,
        citable_dates=citable_dates,
        context=context,
        settings=settings,
    )
    if not verified.report.strip():
        logger.warning("brand_id=%s 상주 보고서 작성 자체가 실패해 저장을 생략한다", brand_id)
        return

    candidates = [c for run in worker_runs for c in run.ctx.candidate_actions]
    recommendations = await _run_resident_recommend(candidates, verified.report, context, settings)

    now = datetime.now(UTC)
    report_id = uuid4()
    snapshot_id = next((run.snapshot_id for run in worker_runs if run.snapshot_id is not None), None)

    report_record = ReportRecord(
        id=report_id,
        brand_id=brand_id,
        trigger_type=trigger_type,
        period_from=period_from,
        period_to=period_to,
        title=build_resident_report_title(trigger_type, period_to),
        summary=split_report_summary(verified.report),
        report_md=verified.report,
        holds=holds,
        verified=verified.passed,
        score_total=verified.last_score.total if verified.last_score else None,
        attempts=verified.attempts,
        snapshot_id=snapshot_id,
        created_at=now,
    )
    # rank·title 저장 확인(완료 조건) — 목록 순서가 곧 rank 다(RecommendationSet 규약,
    # 채팅 레인과 동일). effectiveness_score 는 명시하지 않는다 — 기본값 0.5(이슈 명시).
    recommendation_records = [
        RecommendationRecord(
            id=uuid4(),
            report_id=report_id,
            brand_id=brand_id,
            rank=rank,
            action_type=rec.action_type,
            target_kind="product",
            product_ids=[rec.product_id],
            title=rec.title,
            rationale=rec.rationale,
            expected_effect=rec.expected_effect,
            changes=[change.model_dump() for change in rec.changes],
            created_at=now,
        )
        for rank, rec in enumerate(recommendations.recommendations, start=1)
    ]

    try:
        await analysis_store.save_report(report_record, recommendation_records)
    except Exception as exc:
        logger.error("brand_id=%s 상주 보고서 저장 실패(%r) — 판매자에게 통지하지 않는다", brand_id, exc)
