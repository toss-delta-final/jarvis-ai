"""`AnalysisContext` → `list[str]` 직렬화 (이슈 #598, `context.py` 검증층 연결 계약).

`context.py` 모듈 docstring이 예고한 자리다: `verifier.run_finding_checks` 의
`tool_outputs` 인자는 `Sequence[str]` 이라 ctx 를 객체째로 넘길 수 없다. 이 모듈이
그 어댑터다 — `interpret` 스텝의 LLM 입력과 `verify` 스텝의 근거 집합이 **같은
문자열**을 쓴다("LLM 입력과 동일한 format_* 함수를 재사용", `06-REPORT` §4.1) —
표기(반올림·단위)가 두 곳에서 갈리면 정상 서술도 F2 에 걸릴 수 있어서다.
"""

from __future__ import annotations

from app.agents.seller.sop.compute import render
from app.agents.seller.sop.context import AnalysisContext

# 워커별 표 포맷터 — `analysis_type` 당 렌더할 블록 목록(`sop/compute/render.py`).
_RENDERERS: dict[str, tuple] = {
    "behavior": (render.render_segment_block,),
    "churn": (render.render_segment_block, render.render_shift_block),
    "conversion": (render.render_funnel_block,),
    "sales_anomaly": (render.render_anomaly_block,),
}


def render_causes_block(ctx: AnalysisContext) -> str:
    """[원인 후보] 목록 — interpret 입력·verify 근거집합이 공유한다.

    `strength` 를 한글로 풀어 적는다 — LLM 이 필드명(`temporal_only`)을 그대로
    옮겨쓰는 대신 규칙(WORKER_COMMON_RULES 대응 절)이 요구하는 완곡 표현을 쓰게
    유도한다.
    """
    if not ctx.causes:
        return "[원인 후보]\n\n원인 후보가 없습니다."
    lines = ["[원인 후보]", ""]
    for cause in ctx.causes:
        strength_text = (
            "상관(correlated)" if cause.strength == "correlated" else "시간적 선후만(temporal_only)"
        )
        lines.append(
            f"■ {cause.target_desc} · {cause.event_kind} · {cause.event_at.isoformat()}"
            f" (지연 {cause.lag_days}일) · 강도: {strength_text}"
        )
        lines.append(f"  {cause.event_desc}")
        if cause.corroboration:
            lines.append(f"  보강 근거: {cause.corroboration}")
    return "\n".join(lines)


def render_holds_block(ctx: AnalysisContext) -> str:
    """[판정 보류] 목록 — "판정 보류 ≠ 이상 없음" 규약을 interpret LLM 입력에도 노출한다."""
    if not ctx.holds:
        return "[판정 보류]\n\n판정 보류 없음."
    lines = ["[판정 보류]", ""]
    for hold in ctx.holds:
        lines.append(f"  {hold.step}: {hold.reason}")
    return "\n".join(lines)


def serialize_ctx(ctx: AnalysisContext) -> list[str]:
    """워커 표 + 원인 후보 + 판정 보류 → 문자열 목록.

    `verifier.run_finding_checks` 의 `tool_outputs` 자리에 그대로 쓰인다 — F2
    (`check_evidence_grounded`)가 이 목록 전체에서 숫자 토큰 합집합을 허용 집합으로
    삼는다. `interpret` 스텝은 이 반환값을 그대로 `"\\n\\n".join(...)` 해 LLM 입력으로
    쓴다(같은 문자열 재사용).
    """
    blocks = [renderer(ctx) for renderer in _RENDERERS.get(ctx.worker, ())]
    blocks.append(render_causes_block(ctx))
    blocks.append(render_holds_block(ctx))
    return blocks
