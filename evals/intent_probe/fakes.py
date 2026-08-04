"""API 를 부르지 않는 결정론 가짜 LLM — `--dry-run` 과 유닛테스트 전용.

실 분포를 흉내 내지 않는다. **배관이 도는지**(페이서·N 채우기·채점·산출물)만 확인하는 물건이라
정답과 오답을 고정 패턴으로 낸다. 이 가짜로 잰 표를 프롬프트 판정 근거로 쓰면 안 된다.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm import LLMError
from evals.intent_probe.schema import AnchorSet

_USER_MESSAGE_PREFIX = "USER_MESSAGE: "
_PENDING_PREFIX = "PENDING_CART: "


def _field(user: str, prefix: str) -> str:
    for line in user.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


class ScriptedDecomposeLLM:
    """앵커의 정답을 되돌려주되 고정 주기로 오답을 섞는 가짜 클라이언트.

    `reaskProductListPosition` 에 **일부러 민감**하다 — `--fixture a` 와 `--fixture b` 의
    산출물이 달라진다는 사실 자체를 API 없이 시연하기 위해서다(#260 수용 기준).
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
        self._by_text = {utterance.text: utterance for utterance in anchors.utterances}

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
        has_pending = _field(user, _PENDING_PREFIX) not in ("", "null")
        cell_key = f"{text}|{has_pending}"
        index = self._per_cell[cell_key]
        self._per_cell[cell_key] += 1
        utterance = self._by_text.get(text)
        if utterance is None:
            return self._envelope("general")
        wrong = index % self.wrong_every == self.wrong_every - 1
        expected = utterance.expected

        if utterance.group == "option_answer":
            option_id = expected.option_id
            # 되물음 상품이 목록 1번이 아니면 옵션 선택이 흔들린다 — #240 실측을 모사한 결정론 규칙.
            slipped = self.anchors.reask_product_list_position != 1 and index % 2 == 1
            if wrong or slipped:
                others = [o.option_id for o in self.anchors.options if o.option_id != option_id]
                option_id = others[0] if others else option_id
            return self._envelope(
                "cart_add", product_id=self.anchors.reask_product_id, option_id=option_id
            )

        if utterance.group == "switch":
            target = self._switch_target(text)
            product_id = self.anchors.reask_product_id if wrong else target
            return self._envelope("cart_add", product_id=product_id)

        if wrong:
            fallback = "general" if expected.intent == "recommend" else "recommend"
            return self._envelope(fallback)
        if expected.intent == "cart_add":
            return self._envelope("cart_add", product_id=self.anchors.reask_product_id)
        return self._envelope(expected.intent)

    def _switch_target(self, text: str) -> int:
        for product in self.anchors.last_recommendations:
            if product.product_id == self.anchors.reask_product_id:
                continue
            if any(
                target in text and target in product.name for target in self.anchors.switch_targets
            ):
                return product.product_id
        return next(
            product.product_id
            for product in self.anchors.last_recommendations
            if product.product_id != self.anchors.reask_product_id
        )

    def _envelope(
        self, intent: str, *, product_id: int | None = None, option_id: int | None = None
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "reply": "네" if intent == "general" else "",
            "case": 2,
            "semanticQuery": "세탁 세제",
            "filters": {},
            "cart": {"productId": product_id, "optionId": option_id, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
            "categoryQueries": [],
            "buyAll": False,
            "totalBudget": None,
            "scopedToPrevious": False,
        }
