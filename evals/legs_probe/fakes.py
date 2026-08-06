"""API 를 부르지 않는 결정론 가짜 LLM — `--dry-run` 과 유닛테스트 전용.

실 분포를 흉내 내지 않는다. **배관이 도는지**(페이서·N 채우기·채점·산출물)만 확인하는 물건이라
정답과 오답을 고정 패턴으로 낸다. 이 가짜로 잰 표를 프롬프트 판정 근거로 쓰면 안 된다
(intent_probe/fakes.py 와 같은 규약).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm import LLMError
from evals.legs_probe.schema import Anchor, AnchorSet, Expected

_USER_MESSAGE_PREFIX = "USER_MESSAGE: "


def _field(user: str, prefix: str) -> str:
    for line in user.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


class ScriptedDecomposeLLM:
    """앵커의 case·coverageGroups 를 최대치로 흉내내되 고정 주기로 과소전개 오답을 섞는다.

    발화 텍스트로 앵커를 찾는다 — 이 하네스의 39 앵커는 발화가 전부 고유하므로(fixtures 작성
    규약) intent_probe.fakes 의 모호 텍스트 처리는 필요 없다.
    """

    def __init__(
        self,
        anchors: AnchorSet,
        *,
        fail_first: int = 0,
        always_fail: bool = False,
        wrong_every: int = 7,
    ) -> None:
        self.anchors = anchors
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.wrong_every = wrong_every
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._per_cell: Counter[str] = Counter()
        self._by_text: dict[str, Anchor] = {
            anchor.utterance: anchor for anchor in anchors.utterances
        }

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
        return json.dumps(self._answer(user), ensure_ascii=False)

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        yield await self.complete(system=system, user=user, tier=tier, max_tokens=max_tokens)

    def _answer(self, user: str) -> dict[str, Any]:
        text = _field(user, _USER_MESSAGE_PREFIX)
        # 카운터는 발화 텍스트로 나눈다 — run_cell 은 셀 안에서 순차 호출하므로 이 키면 항상
        # 결정론이다(intent_probe.fakes 와 같은 규약).
        index = self._per_cell[text]
        self._per_cell[text] += 1
        anchor = self._by_text.get(text)
        if anchor is None:
            return self._envelope(intent="general", case=2, category_queries=[])
        wrong = index % self.wrong_every == self.wrong_every - 1
        expected = anchor.expected
        # 오답 주기는 **과소전개**를 흉내낸다 — #198 이 재려는 정확히 그 실패 모양(case==3인데
        # legs<=1)이라, dry-run 이 case3UnderExpansionRate 배관을 실제로 태운다.
        limit = 1 if wrong else None
        queries = self._legs(expected, limit=limit)
        return self._envelope(
            intent="recommend",
            case=expected.case,
            category_queries=queries,
            buy_all=expected.buy_all if expected.buy_all is not None else False,
            total_budget=expected.total_budget,
        )

    def _legs(self, expected: Expected, *, limit: int | None) -> list[dict[str, Any]]:
        groups = expected.coverage_groups
        if not groups:
            return []
        picked = groups if limit is None else groups[:limit]
        return [{"category": None, "query": group.synonyms[0]} for group in picked]

    def _envelope(
        self,
        *,
        intent: str,
        case: int,
        category_queries: list[dict[str, Any]],
        buy_all: bool = False,
        total_budget: int | None = None,
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "reply": "",
            "case": case,
            "semanticQuery": "",
            "filters": {},
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
            "categoryQueries": category_queries,
            "buyAll": buy_all,
            "totalBudget": total_budget,
            "scopedToPrevious": False,
        }
