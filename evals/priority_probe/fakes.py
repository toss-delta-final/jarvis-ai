"""API 를 부르지 않는 결정론 가짜 LLM — `--dry-run` 과 유닛테스트 전용.

`evals/intent_probe/fakes.py` 와 같은 규율: 실 분포를 흉내 내지 않는다. **배관이 도는지**만
확인하는 물건이라 이 가짜로 잰 표를 채택 판정 근거로 쓰면 안 된다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm import LLMError
from evals.priority_probe.schema import FixtureSet, PriorityCell


class ScriptedPriorityLLM:
    """기대 priority 를 되돌려주되 고정 주기로 형식을 무너뜨리는 가짜.

    `arm` 에 따라 응답 모양이 다르다 — 분류기는 `{"priorities": [...]}`, 인라인은 전체
    decompose 봉투(`categoryQueries[i].priority` 포함)다.
    """

    def __init__(
        self,
        fixture: FixtureSet,
        *,
        arm: str,
        fail_first: int = 0,
        always_fail: bool = False,
        wrong_every: int = 7,
    ) -> None:
        self.fixture = fixture
        self.arm = arm
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.wrong_every = wrong_every
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._per_cell: Counter[str] = Counter()
        self._by_utterance = {cell.utterance: cell for cell in fixture.cells}

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        self.calls.append({"system": system, "user": user, "tier": tier, "maxTokens": max_tokens})
        self._attempts += 1
        if self.always_fail or self._attempts <= self.fail_first:
            raise LLMError("scripted failure")
        cell = self._match_cell(user)
        if cell is None:
            return json.dumps(self._empty_envelope(), ensure_ascii=False)
        index = self._per_cell[cell.cell_id]
        self._per_cell[cell.cell_id] += 1
        wrong = index % self.wrong_every == self.wrong_every - 1
        priorities = list(cell.expected_priorities)
        if wrong:
            # 오답도 유효 범위 안에서 돈다(1→2→3→1) — dry-run 이 priorityExact 미스 배관도 태운다.
            priorities = [(value % 3) + 1 for value in priorities]
        if self.arm == "classifier":
            return json.dumps({"priorities": priorities}, ensure_ascii=False)
        return json.dumps(self._decompose_envelope(cell, priorities), ensure_ascii=False)

    def _match_cell(self, user: str) -> PriorityCell | None:
        for utterance, cell in self._by_utterance.items():
            if utterance in user:
                return cell
        return None

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        yield await self.complete(system=system, user=user, tier=tier, max_tokens=max_tokens)

    @staticmethod
    def _empty_envelope() -> dict[str, Any]:
        return {
            "intent": "recommend",
            "reply": "",
            "case": 3,
            "semanticQuery": "",
            "filters": {},
            "categoryQueries": [],
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
            "buyAll": True,
            "totalBudget": None,
            "scopedToPrevious": False,
        }

    @staticmethod
    def _decompose_envelope(cell: PriorityCell, priorities: list[int]) -> dict[str, Any]:
        envelope = ScriptedPriorityLLM._empty_envelope()
        envelope["categoryQueries"] = [
            {"category": None, "query": need, "priority": priority}
            for need, priority in zip(cell.needs, priorities, strict=True)
        ]
        return envelope
