"""판매자 상주 analysis 파이프라인의 SOP 층 (이슈 #589, `01-ARCHITECTURE.md` §4).

구성:
- ``engine``: `Step` / `Sop` / `run_sop` — 순차 실행 + 예외 → `Hold` 흡수(≈40줄)
- ``context``: `AnalysisContext` 와 서브모델 8종 — LLM 이 보는 유일한 입력

채팅 레인(대화형)과 무연결이다 — 이 층은 채팅 밖 상주 파이프라인에서만 돈다(§1).
"""

from app.agents.seller.sop.context import (
    AnalysisContext,
    CauseCandidate,
    Comparison,
    Hold,
    Metric,
    PastAction,
    ProductFlag,
    Segment,
    Verdict,
    VerdictValue,
)
from app.agents.seller.sop.engine import Sop, Step, StepFn, run_sop

__all__ = [
    "AnalysisContext",
    "CauseCandidate",
    "Comparison",
    "Hold",
    "Metric",
    "PastAction",
    "ProductFlag",
    "Segment",
    "Sop",
    "Step",
    "StepFn",
    "Verdict",
    "VerdictValue",
    "run_sop",
]
