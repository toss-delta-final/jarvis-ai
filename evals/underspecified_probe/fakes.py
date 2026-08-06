"""API 를 부르지 않는 결정론 가짜 LLM — `--dry-run` 과 유닛테스트 전용 (#380).

**무상태 결정론이다** — 발화 텍스트 → 고정 산출(호출 순서·동시성과 무관). legs_probe 의
`wrong_every` 같은 주기 카운터를 쓰지 않는다(§D7) — 동시성에서 산출이 흔들려 dry-run 재현성이
깨진다. 이 가짜로 잰 표를 프롬프트 판정 근거로 쓰면 안 된다(legs_probe/fakes.py 와 같은 규약).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm import LLMError
from evals.underspecified_probe.schema import Anchor, AnchorSet

_USER_MESSAGE_PREFIX = "USER_MESSAGE: "


def _field(user: str, prefix: str) -> str:
    for line in user.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _apply_axis_signal(payload: dict[str, Any], axis: str) -> None:
    """referenceAxes 축 이름 → decompose JSON 산출에 그 축이 채워진 것처럼 patch 한다.

    `categoryLegs`·`filters.category` 는 이 함수가 아무것도 하지 않는다 — 둘 다 decompose 단계
    에서는 애초에 채워질 수 없는 축이다(`category_legs` 는 graph.py 매핑 전용, `filters.category`
    는 `_SYSTEM` 의 filters JSON 스키마에 키 자체가 없다, §D10) — 이 하네스의 앵커는 그래서 이
    두 축을 referenceAxes 로 쓰지 않는다(fixtures/anchors.json 참조).
    """
    filters = payload.setdefault("filters", {})
    if axis == "categoryQueries":
        payload["categoryQueries"] = [{"category": None, "query": "스크립트 상품"}]
    elif axis == "semanticQueryIsFallback":
        payload["semanticQuery"] = "스크립트 의미 쿼리"
    elif axis == "repurchaseProducts":
        payload["repurchaseProducts"] = ["스크립트 재구매 상품"]
    elif axis == "revertCategories":
        payload["revertCategories"] = ["스크립트 카테고리"]
    elif axis == "filters.brand":
        filters["brand"] = ["스크립트 브랜드"]
    elif axis == "filters.keyword":
        filters["keyword"] = "스크립트 키워드"
    elif axis == "filters.color":
        filters["color"] = "스크립트 색상"
    elif axis == "filters.attrConditions":
        payload["attrConditions"] = {"스크립트축": "스크립트값"}
    elif axis == "filters.rating_min":
        filters["ratingMin"] = 4.0


def _apply_constraint_signal(payload: dict[str, Any], axis: str) -> None:
    """constraintAxes 축 이름 → 판정에 영향 없는 값으로 채운다(dry-run 산출을 현실적으로)."""
    filters = payload.setdefault("filters", {})
    if axis == "filters.price_min":
        filters["priceMin"] = 10_000
    elif axis == "filters.price_max":
        filters["priceMax"] = 50_000
    elif axis == "totalBudget":
        payload["totalBudget"] = 80_000
    elif axis == "buyAll":
        payload["buyAll"] = True


def _select_wrong_case_ids(anchors: AnchorSet) -> frozenset[str]:
    """[§D7] caseId 로 고정 선택한 소수 앵커를 일부러 기대와 어긋나게 낸다 — 미탐 1건·오탐 1건이
    dry-run 산출물에 반드시 나타나야 원인 축 분해(D11)·miss/falseAlarm 배관이 실제로 태워진다.

    패킷 예시("정렬된 caseId 중 3·4번째")는 픽스처 슬라이스 구성이 바뀌면 우연히 게이트
    앵커(`priorExists=true`, 어떤 산출을 내도 판정을 못 바꾼다)를 가리킬 수 있다 — 대신 정렬된
    caseId 순서에서 "미탐을 만들 수 있는 첫 앵커"와 "오탐을 만들 수 있는 첫 앵커"를 각각
    결정론으로 고른다(앵커 집합 자체로만 결정되므로 여전히 무상태 결정론이다).
    """
    sorted_anchors = sorted(anchors.utterances, key=lambda a: a.case_id)
    wrong: set[str] = set()
    for anchor in sorted_anchors:
        if anchor.expected_reask:
            wrong.add(anchor.case_id)
            break
    for anchor in sorted_anchors:
        if not anchor.expected_reask and not anchor.prior_exists:
            wrong.add(anchor.case_id)
            break
    return frozenset(wrong)


class ScriptedDecomposeLLM:
    """앵커의 기대 판정을 최대한 흉내내되, 고정 선택한 소수 앵커는 일부러 반대로 낸다.

    발화 텍스트로 앵커를 찾는다 — 이 하네스의 30 앵커는 발화가 전부 고유하므로(스키마 검증자,
    §D3) intent_probe.fakes 의 모호 텍스트 처리는 필요 없다.

    `fail_first`/`always_fail` 은 유닛테스트 전용이다 — 호출 카운터에 의존하는 실패 주입은
    단일 셀 테스트에서만 쓴다(동시 실행에서는 어느 셀이 몇 번째 호출인지 결정되지 않는다).
    """

    def __init__(
        self,
        anchors: AnchorSet,
        *,
        fail_first: int = 0,
        always_fail: bool = False,
    ) -> None:
        self.anchors = anchors
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._by_text: dict[str, Anchor] = {
            anchor.utterance: anchor for anchor in anchors.utterances
        }
        self._wrong_case_ids = _select_wrong_case_ids(anchors)

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
        anchor = self._by_text.get(text)
        if anchor is None:
            return self._envelope()
        if anchor.prior_exists:
            # [§D5] 첫 턴 게이트(`prior is not None: return False`)가 판정을 결정하므로 decompose
            # 산출 내용 자체는 판정에 영향이 없다 — 블랭크로 둔다.
            payload = self._envelope()
        else:
            wants_reask_signal = anchor.expected_reask
            if anchor.case_id in self._wrong_case_ids:
                wants_reask_signal = not wants_reask_signal
            payload = self._envelope()
            if not wants_reask_signal:
                for axis in anchor.reference_axes or ["categoryQueries"]:
                    _apply_axis_signal(payload, axis)
        for axis in anchor.constraint_axes:
            _apply_constraint_signal(payload, axis)
        return payload

    def _envelope(
        self,
        *,
        semantic_query: str = "",
        filters: dict[str, Any] | None = None,
        category_queries: list[dict[str, Any]] | None = None,
        repurchase_products: list[str] | None = None,
        revert_categories: list[str] | None = None,
        buy_all: bool = False,
        total_budget: int | None = None,
    ) -> dict[str, Any]:
        return {
            "intent": "recommend",
            "reply": "",
            "case": 1,
            "semanticQuery": semantic_query,
            "attrConditions": {},
            "attrRemovals": [],
            "categoryQueries": category_queries or [],
            "filters": filters or {},
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": revert_categories or [],
            "repurchaseProducts": repurchase_products or [],
            "buyAll": buy_all,
            "totalBudget": total_budget,
            "scopedToPrevious": False,
        }
