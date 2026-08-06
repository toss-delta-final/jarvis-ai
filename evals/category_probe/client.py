"""LLM 클라이언트 조립 — 페이싱 + provider 채록 (#331).

프롬프트 override 는 이 하네스 범위 밖이라 만들지 않는다(packet-331 §1 — decompose·category_select
_SYSTEM 을 갈아끼우는 실험은 이 이슈가 재는 것이 아니다). 페이싱·채록 래퍼는
`evals/intent_probe`·`evals/model_eval` 의 확립된 조립을 그대로 **import 해 재사용**한다
(intent_probe 디렉터리는 수정하지 않는다, packet-331 §1).
"""

from __future__ import annotations

from app.core.llm import LLMClient
from evals.intent_probe.client import PacedLLM, build_live_delegate
from evals.intent_probe.pacer import GlobalPacer, PacerLimits

__all__ = ["GlobalPacer", "PacerLimits", "PacedLLM", "build_live_delegate", "build_probe_llm"]


def build_probe_llm(delegate: LLMClient, *, pacer: GlobalPacer) -> PacedLLM:
    """래퍼 사슬을 조립한다 — 이 하네스는 프롬프트 override 가 없어 페이싱 한 겹뿐이다."""
    return PacedLLM(delegate, pacer=pacer)
