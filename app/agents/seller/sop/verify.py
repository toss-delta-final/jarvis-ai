"""워커별 `verify` 스텝 (이슈 #598, design-598 §3-3) — F1~F3 + `analysis_judge`.

`interpret` 이 만든 `ctx.findings[-1]` 1건을 채팅 레인 `_run_one_branch`(orchestrator.py)
와 같은 재료(`verifier.run_finding_checks` + `build_analysis_judge`)로 채점한다.
차이는 **재실행 루프가 없다는 것**이다 — 엔진(`sop/engine.py`)은 조건 분기·재시도를
모른다("만들지 않는 것" 목록)는 설계를 그대로 따른다. 미달 시 finding 을 강등하고
`Hold` 로 사유를 남길 뿐, interpret 을 다시 부르지 않는다.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage

from app.agents.seller.pipeline import format_analysis_judge_input
from app.agents.seller.schemas import AnalysisFinding, AnalysisScore
from app.agents.seller.sop.context import AnalysisContext, Hold
from app.agents.seller.sop.serialize import serialize_ctx
from app.agents.seller.verifier import run_finding_checks
from app.agents.seller.workers import build_analysis_judge
from app.core.config import get_settings
from app.core.llm import LLMNotConfigured

logger = logging.getLogger(__name__)

_REASON_MAX_CHARS = 200


def _degrade(worker: str, cause: str) -> AnalysisFinding:
    """`orchestrator._degrade_finding` 과 같은 규약(D3 탐지 문자열 "확보 실패" 유지).

    별도로 정의하는 이유: 그 함수는 orchestrator.py 의 private(`_`) 심볼이라 모듈
    경계를 넘겨 재사용하면 그 모듈의 채팅 레인 내부 변경에 이 스텝이 종속된다.
    """
    return AnalysisFinding(
        analysis_type=worker,  # type: ignore[arg-type]
        summary=f"데이터 확보 실패 — 분석 검증 미달({cause})",
        evidence=[],
        severity="info",
    )


async def verify_step(ctx: AnalysisContext) -> None:
    """`ctx.findings[-1]`(방금 interpret 이 만든 것) 1건을 검증하고 미달 시 강등한다.

    `interpret` 이 게이트로 스킵됐으면(`ctx.findings` 가 비어 있으면) 검증할 대상이
    없으므로 조용히 넘어간다.
    """
    if not ctx.findings:
        return
    finding = ctx.findings[-1]
    tool_outputs = serialize_ctx(ctx)
    failed = run_finding_checks(finding, tool_outputs, expected_type=ctx.worker)

    settings = get_settings()
    judge_agent = build_analysis_judge()
    judge_input = format_analysis_judge_input(finding, tool_outputs)
    try:
        judge_result = await asyncio.wait_for(
            judge_agent.ainvoke({"messages": [HumanMessage(content=judge_input)]}),
            timeout=settings.seller_analysis_judge_timeout_s,
        )
        score = judge_result.get("structured_response")
        if not isinstance(score, AnalysisScore):
            raise TypeError("analysis_judge 가 AnalysisScore 를 반환하지 않았다")
    except LLMNotConfigured:
        raise  # 전역 설정 오류 — degrade 대상이 아니다(orchestrator.py 관행 승계)
    except Exception as exc:
        logger.warning("verify %s analysis_judge 실패(%r)", ctx.worker, exc)
        if failed:
            ctx.findings[-1] = _degrade(ctx.worker, "F검사 미달 + judge 장애")
        ctx.holds.append(
            Hold(
                step="verify",
                reason=(f"analysis_judge_unavailable: {exc}; F검사 {len(failed)}건")[
                    :_REASON_MAX_CHARS
                ],
            )
        )
        return

    threshold = settings.seller_analysis_score_threshold
    if not failed and score.total >= threshold:
        return  # 통과 — ctx.findings[-1] 그대로 채택

    ctx.findings[-1] = _degrade(ctx.worker, "분석 검증 미달")
    reasons = list(failed)
    if score.total < threshold:
        reasons.append(f"analysis_judge {score.total}/{threshold} 미달: {score.feedback}")
    ctx.holds.append(Hold(step="verify", reason="; ".join(reasons)[:_REASON_MAX_CHARS]))
