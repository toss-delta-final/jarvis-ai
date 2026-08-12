"""판매자 상주 analysis 파이프라인의 SOP 층 (이슈 #589, `01-ARCHITECTURE.md` §4).

구성:
- ``engine``: `Step` / `Sop` / `run_sop` — 순차 실행 + 예외 → `Hold` 흡수(≈40줄)
- ``context``: `AnalysisContext` 와 서브모델 11종 — LLM 이 보는 유일한 입력
- ``compute``: 워커별 통계 판정 4종 + 원인·추천 후보 생성 + LLM 입력 표 포맷터
  (이슈 #594·#597)
- ``rule_cards``: 논문 지식 카드 — 조건은 코드가 평가하고 LLM 은 문장만 옮긴다(결정 115)
- ``validate``: `validate_context` — ctx 의 숫자·기간·evidence 정합성 (이슈 #596, LLM 0회)
- ``gate``: `should_interpret` — 서술할 것이 없으면 LLM 호출 자체를 건너뛴다
- ``serialize``: `serialize_ctx` — ctx → `list[str]`, interpret 입력과 verify 근거집합 공유
- ``interpret``: `interpret_step` — 워커 4종 공통 zero-tool interpret 스텝 (이슈 #598)
- ``verify``: `verify_step` — F1~F3 + analysis_judge, 재작성 없이 강등만 (이슈 #598)
- ``assembly``: `build_sop` — 워커별 `Sop` 조립(load/compare/compute 클로저) (이슈 #598)

스텝 순서는 `load → compare → compute → validate → feedback → interpret → verify` 다 —
`validate` 가 `interpret` **앞**에 서는 것이 요점이다(못 쓸 재료로 LLM 을 부르지 않는다).

채팅 레인(대화형)과 무연결이다 — 이 층은 채팅 밖 상주 파이프라인에서만 돈다(§1).
"""

from app.agents.seller.sop.assembly import build_sop
from app.agents.seller.sop.context import (
    CAUSE_EVENT_KINDS,
    ActionCandidate,
    AnalysisContext,
    CandidateChange,
    CauseCandidate,
    Comparison,
    FiredRuleCard,
    Hold,
    Metric,
    PastAction,
    ProductFlag,
    Segment,
    Verdict,
    VerdictValue,
)
from app.agents.seller.sop.engine import Sop, Step, StepFn, run_sop
from app.agents.seller.sop.gate import should_interpret
from app.agents.seller.sop.interpret import interpret_step
from app.agents.seller.sop.rule_cards import RULE_CARDS, RuleCard, evaluate_rule_cards
from app.agents.seller.sop.serialize import serialize_ctx
from app.agents.seller.sop.validate import ValidationResult, validate_context
from app.agents.seller.sop.verify import verify_step

__all__ = [
    "CAUSE_EVENT_KINDS",
    "RULE_CARDS",
    "ActionCandidate",
    "AnalysisContext",
    "CandidateChange",
    "CauseCandidate",
    "Comparison",
    "FiredRuleCard",
    "Hold",
    "Metric",
    "PastAction",
    "ProductFlag",
    "RuleCard",
    "Segment",
    "Sop",
    "Step",
    "StepFn",
    "ValidationResult",
    "Verdict",
    "VerdictValue",
    "build_sop",
    "evaluate_rule_cards",
    "interpret_step",
    "run_sop",
    "serialize_ctx",
    "should_interpret",
    "validate_context",
    "verify_step",
]
