"""니즈 priority 분류기 — 파싱·degrade·관측 (이슈 #281, #60 후속).

`budget_sets.build_budget_sets` 의 priority 소비(`priorities=`)는 `tests/unit/test_budget_sets.py`
가 커버한다. 여기 테스트는 **분류기 자체의 배관**만 고정한다 — 판정 품질은 실 LLM 프로브
(`evals/priority_probe/`, TASK 3)가 재고, 이 파일은 가짜 LLM 만 쓴다(CI API 콜 0).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.buyer.recommendation.need_priority import _SYSTEM, classify_need_priorities
from app.core.config import get_settings
from app.core.llm import LLMError

NEEDS = ["등뼈", "들깨가루", "청양고추"]


def _settings():
    """실 Settings 를 그대로 쓴다 — `resolve_model_id` 가 provider 설정까지 읽으므로 부분 흉내는
    그 자리에서 AttributeError 가 되고, 그 예외를 분류기가 삼켜 테스트가 조용히 무의미해진다."""
    return get_settings()


class _ScriptedLLM:
    """분류기 호출만 받는 최소 LLM — 응답 문자열을 그대로 돌려주거나 예외를 던진다."""

    def __init__(
        self, raw: str = '{"priorities": [1, 2, 3]}', *, error: Exception | None = None
    ) -> None:
        self._raw = raw
        self._error = error
        self.calls: list[tuple[str, str, int]] = []  # (system, user, max_tokens)

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        self.calls.append((system, user, max_tokens))
        if self._error is not None:
            raise self._error
        return self._raw

    async def stream(self, *, system, user, tier, max_tokens=1024):  # noqa: ANN001
        yield "x"


# ─────────── 파싱 ───────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"priorities": [1, 2, 3]}', (1, 2, 3)),
        ('{"priorities": [3, 3, 3]}', (3, 3, 3)),
        ('{"priorities": [1, 2]}', None),  # 길이 불일치(needs=3)
        ('{"priorities": [1, 2, 3, 1]}', None),  # 길이 불일치(초과)
        ('{"priorities": ["1", 2, 3]}', None),  # 문자열은 int 가 아니다
        ('{"priorities": [1, true, 3]}', None),  # bool 은 int 서브클래스지만 배제한다
        ('{"priorities": [0, 2, 3]}', None),  # 범위 밖(0)
        ('{"priorities": [1, 2, 4]}', None),  # 범위 밖(4)
        ("{}", None),  # 키 누락
        ('{"priorities": null}', None),
        ('{"priorities": "1,2,3"}', None),  # 배열이 아님
        ("판정할 수 없습니다", None),  # JSON 아님
        (
            '{"priorities": [1, 2, 3]} 라고 봅니다',
            (1, 2, 3),
        ),  # 코드펜스·군말은 extract_json 이 흡수
    ],
)
async def test_priority_parsing_is_strict_and_all_or_nothing(raw, expected) -> None:
    llm = _ScriptedLLM(raw)
    assert (
        await classify_need_priorities(
            llm, message="감자탕 재료", needs=NEEDS, settings=_settings()
        )
        == expected
    )


async def test_llm_failure_returns_none_instead_of_raising() -> None:
    llm = _ScriptedLLM(error=LLMError("boom"))
    assert (
        await classify_need_priorities(
            llm, message="감자탕 재료", needs=NEEDS, settings=_settings()
        )
        is None
    )


async def test_unexpected_exception_is_also_swallowed() -> None:
    llm = _ScriptedLLM(error=RuntimeError("네트워크 붕괴"))
    assert (
        await classify_need_priorities(
            llm, message="감자탕 재료", needs=NEEDS, settings=_settings()
        )
        is None
    )


async def test_cancellation_is_not_swallowed() -> None:
    """`CancelledError` 는 BaseException 이라 전파돼야 한다 — 그래프가 태스크를 취소할 수 있어야 한다."""
    llm = _ScriptedLLM(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await classify_need_priorities(
            llm, message="감자탕 재료", needs=NEEDS, settings=_settings()
        )


# ─────────── needs 빈 입력 ───────────


async def test_empty_needs_does_not_call_the_llm() -> None:
    llm = _ScriptedLLM()
    result = await classify_need_priorities(llm, message="아무거나", needs=[], settings=_settings())
    assert result is None
    assert llm.calls == []


# ─────────── 관측 ───────────


async def test_model_call_is_recorded_exactly_once() -> None:
    recorded: list[str] = []
    observer = SimpleNamespace(record_model_call=recorded.append)
    await classify_need_priorities(
        _ScriptedLLM(), message="감자탕 재료", needs=NEEDS, settings=_settings(), observer=observer
    )
    assert len(recorded) == 1


async def test_failed_call_is_still_recorded() -> None:
    # 실패한 호출도 비용이 든다 — 형제 경로(category_scope·needs_expansion)와 같이 **호출 전** 기록한다.
    recorded: list[str] = []
    observer = SimpleNamespace(record_model_call=recorded.append)
    await classify_need_priorities(
        _ScriptedLLM(error=LLMError("boom")),
        message="감자탕 재료",
        needs=NEEDS,
        settings=_settings(),
        observer=observer,
    )
    assert len(recorded) == 1


async def test_empty_needs_records_nothing() -> None:
    """호출 자체가 없으므로 관측 기록도 없다 — needs=[] 는 LLM 을 부르지 않는다(위 테스트)."""
    recorded: list[str] = []
    observer = SimpleNamespace(record_model_call=recorded.append)
    await classify_need_priorities(
        _ScriptedLLM(), message="아무거나", needs=[], settings=_settings(), observer=observer
    )
    assert recorded == []


# ─────────── 프롬프트 ───────────


async def test_user_block_carries_the_needs_and_the_message() -> None:
    llm = _ScriptedLLM()
    await classify_need_priorities(llm, message="감자탕 재료 좀", needs=NEEDS, settings=_settings())
    system, user, max_tokens = llm.calls[0]
    assert system is _SYSTEM
    assert '["등뼈", "들깨가루", "청양고추"]' in user
    assert "감자탕 재료 좀" in user
    assert max_tokens == get_settings().need_priority_max_tokens


def test_system_prompt_keeps_the_spec_wording() -> None:
    """SPEC-RECOMMEND-001 결정 14-H 의 판정 기준·정본 예시를 그대로 쓴다 — 동의어로 바꾸면
    #281 TASK 3 프로브(evals/priority_probe/)의 실측이 무효가 된다."""
    anchors = (
        "이게 빠지면 그 상황/요리가 성립하는가",
        "감자탕에 등뼈",
        "들깨가루",
        "청양고추",
        '{"priorities": [1, 2, 3, ...]}',
    )
    for anchor in anchors:
        assert anchor in _SYSTEM, anchor
