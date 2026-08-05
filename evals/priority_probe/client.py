"""priority 프로브 전용 래퍼 — 나머지 페이싱/프롬프트 교체 배관은 `evals.intent_probe.client` 를
그대로 import 해서 쓴다(#281 TASK 3 — 113줄 슬라이딩 윈도 페이서를 다시 쓰지 않는다).

⚠️ **결합 고지**: `evals.intent_probe` 를 import 로 결합한다 — #300 이 그 디렉터리를 옮기면 이
import 한 줄이 깨진다. `evals/priority_probe/README.md` 에도 같은 문장이 있다.

이 파일이 추가하는 것은 `RawCapture` 하나뿐이다. 두 가지 역할을 겸한다:

1. **원시 텍스트 곁가지 저장** — `decompose()`/`classify_need_priorities()` 는 파싱된 값만
   돌려주고 원시 JSON 텍스트를 버리는데, 이 프로브는 두 응답 모두에서 앱이 아직 읽지 않는 필드
   (`categoryQueries[i].priority` · 진단용 원시 길이)를 봐야 한다. 앱 코드를 고치지 않고 프로브
   쪽에서 원시 텍스트를 옆에 남겨 두는 것이 그 방법이다.
2. **전송 결과 관측**(TASK-3-CORRECTION) — `classify_need_priorities` 는 **전송 실패**(429·타임아웃·
   `BudgetExceeded` 포함)와 **모델이 파싱 불가 출력을 냈다**를 구분 없이 `None` 하나로 삼킨다.
   래퍼 사슬의 **맨 안쪽**(delegate 바로 앞)에서 `complete()` 자체가 예외를 던졌는지를 기록해
   두면, `classify_need_priorities` 가 그 예외를 삼킨 뒤에도 호출부가 "이 시도가 전송 실패였는지
   모델 출력 실패였는지" 를 구분할 수 있다 — 전자만 재시도 대상이다(#240 「실패는 표본이
   아니다」와 같은 원칙, 분류기 팔에도 그대로 적용한다).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

from app.core.llm import LLMClient
from evals.intent_probe.client import PacedLLM, SystemPromptOverrideLLM
from evals.intent_probe.pacer import GlobalPacer

Outcome = Literal["ok", "error"]


class RawCapture:
    """직전 `complete()` 호출의 원시 응답 텍스트 + 전송 결과를 곁가지로 저장한다.

    **래퍼 사슬의 맨 안쪽**(delegate 바로 앞)에 둬야 한다 — 페이서·프롬프트 교체보다 안쪽이라
    "provider 에 실제로 무슨 일이 있었는지" 를 있는 그대로 본다. `last_outcome`/`last_error` 는
    이번 시도가 **전송 실패**(재시도 대상)인지 **모델 출력 문제**(진짜 표본)인지를 가르는
    유일한 근거다 — `classify_need_priorities` 자신은 그 둘을 구분해 노출하지 않는다.
    예외는 **그대로 재전파**한다(흐름을 바꾸지 않는다) — 관측만 하고 삼키지 않는다.
    """

    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.last_raw: str | None = None
        self.last_outcome: Outcome | None = None
        self.last_error: BaseException | None = None

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        try:
            raw = await self.delegate.complete(
                system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
            )
        except BaseException as exc:  # noqa: BLE001 - 관측만 하고 그대로 재전파한다
            self.last_outcome = "error"
            self.last_error = exc
            self.last_raw = None
            raise
        self.last_outcome = "ok"
        self.last_error = None
        self.last_raw = raw
        return raw

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        return self.delegate.stream(system=system, user=user, tier=tier, max_tokens=max_tokens)


def build_inline_llm(
    delegate: LLMClient, *, pacer: GlobalPacer, system: str
) -> tuple[SystemPromptOverrideLLM, RawCapture]:
    """인라인 팔 래퍼 사슬 — `SystemPromptOverrideLLM(PacedLLM(RawCapture(delegate)))`."""
    capture = RawCapture(delegate)
    return SystemPromptOverrideLLM(PacedLLM(capture, pacer=pacer), system=system), capture


def build_classifier_llm(
    delegate: LLMClient, *, pacer: GlobalPacer
) -> tuple[LLMClient, RawCapture]:
    """분류기 팔 래퍼 사슬 — 프롬프트 교체가 없다(배포와 동일 `_SYSTEM`).

    `RawCapture` 는 진단(`unparsedCount` 등)에만 쓰고 채점에는 쓰지 않는다 — 채점은 항상
    `classify_need_priorities` 의 공식 반환값을 쓴다(규칙을 프로브에서 재구현하지 않는다).
    """
    capture = RawCapture(delegate)
    return PacedLLM(capture, pacer=pacer), capture
