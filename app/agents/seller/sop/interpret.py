"""워커 4종 상주 `interpret` 스텝 (이슈 #598, `06-REPORT` §4.1).

채팅 레인 워커(`workers.WORKER_BUILDERS` 계열)와 무접촉이다 — 이 스텝은 도구를
호출하지 않는다(zero-tool). 입력은 `sop/serialize.serialize_ctx` 가 만든 표 문자열
그대로이고, `should_interpret` 게이트가 "서술할 것이 없으면 LLM 호출 자체를
건너뛴다"를 그대로 판단한다(수정 없음, design-598 §3-2) — 엔진(`sop/engine.py`)은
조건 분기를 모르므로 게이트 판단은 스텝 함수 자신이 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from app.agents.seller.context import SellerContext
from app.agents.seller.schemas import AnalysisFinding, AnalysisType, BehaviorFinding, SegmentNaming
from app.agents.seller.sop.context import AnalysisContext
from app.agents.seller.sop.gate import should_interpret
from app.agents.seller.sop.serialize import serialize_ctx
from app.agents.seller.workers import (
    build_behavior_interpret_agent,
    build_churn_interpret_agent,
    build_conversion_interpret_agent,
    build_sales_anomaly_interpret_agent,
)

logger = logging.getLogger(__name__)

_INTERPRET_BUILDERS: dict[AnalysisType, Callable[[], CompiledStateGraph]] = {
    "behavior": build_behavior_interpret_agent,
    "churn": build_churn_interpret_agent,
    "conversion": build_conversion_interpret_agent,
    "sales_anomaly": build_sales_anomaly_interpret_agent,
}

_RESPONSE_TYPES: dict[AnalysisType, type[AnalysisFinding]] = {
    "behavior": BehaviorFinding,
    "churn": AnalysisFinding,
    "conversion": AnalysisFinding,
    "sales_anomaly": AnalysisFinding,
}


def _apply_segment_namings(ctx: AnalysisContext, namings: list[SegmentNaming]) -> None:
    """`BehaviorFinding.segment_namings` → `ctx.segments[i].llm_label/llm_desc`.

    `rule_label` 로 조인한다(원형 — `context.py` Segment 규약). 표에 없는 라벨을
    LLM 이 지어내 반환하면 무시하고 경고만 남긴다(그 라벨의 세그먼트는 `llm_label`
    이 빈 채로 남아 보고서에서 원형 라벨로 대체 표기된다, `render.py` 선례).
    """
    by_label = {segment.rule_label: segment for segment in ctx.segments}
    for naming in namings:
        segment = by_label.get(naming.rule_label)
        if segment is None:
            logger.warning(
                "behavior interpret 가 알 수 없는 rule_label=%s 을 반환했다 — 무시",
                naming.rule_label,
            )
            continue
        segment.llm_label = naming.llm_label
        segment.llm_desc = naming.llm_desc


async def interpret_step(ctx: AnalysisContext) -> None:
    """워커 공통 interpret 스텝 — `ctx.worker` 로 프롬프트·응답 스키마를 고른다.

    4워커(`behavior`·`churn`·`conversion`·`sales_anomaly`) 밖의 `worker` 값은
    상주 파이프라인 대상이 아니다(design-598 범위 — abuse·review 는 SOP compute
    미지원)라 조용히 넘어간다.
    """
    if not should_interpret(ctx):
        return
    builder = _INTERPRET_BUILDERS.get(ctx.worker)
    if builder is None:
        return
    response_type = _RESPONSE_TYPES[ctx.worker]

    agent = builder()
    message = "\n\n".join(serialize_ctx(ctx))
    # 상주 파이프라인은 JWT 가 없다 — zero-tool 에이전트라 `ToolRuntime.context` 를
    # 아무 도구도 읽지 않으므로 `seller_id` 는 사용되지 않는 자리표시자다(brand_id 만
    # 실제로 프롬프트 조립 밖에서 의미가 있다 — 여기서도 프롬프트에 노출되지 않는다).
    context = SellerContext(seller_id=0, brand_id=ctx.brand_id)
    result = await agent.ainvoke({"messages": [HumanMessage(content=message)]}, context=context)
    finding = result.get("structured_response")
    if not isinstance(finding, response_type):
        raise TypeError(f"{ctx.worker} interpret 이 {response_type.__name__} 을 반환하지 않았다")

    ctx.findings.append(finding)
    if isinstance(finding, BehaviorFinding):
        _apply_segment_namings(ctx, finding.segment_namings)
