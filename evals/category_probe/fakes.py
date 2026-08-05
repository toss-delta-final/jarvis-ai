"""API 를 부르지 않는 결정론 가짜 LLM·임베딩·pg — `--dry-run` 과 유닛테스트 전용 (#331).

실 분포를 흉내 내지 않는다. **배관이 도는지**(페이서·N 채우기·기록 래퍼·채점·산출물)만 확인하는
물건이라 정답과 오답을 고정 패턴으로 낸다. 이 가짜로 잰 표를 매핑 정확도 판정 근거로 쓰면 안 된다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from app.agents.buyer.recommendation.category_select import _SYSTEM as _SELECT_SYSTEM
from app.core.llm import LLMError
from evals.category_probe.schema import AnchorCell, AnchorSet

_USER_MESSAGE_PREFIX = "USER_MESSAGE: "
_FAKE_DIM = 8


def _fake_vector(text: str) -> list[float]:
    """텍스트 → 결정론 8차원 벡터(sha256 바이트를 [-1,1] 로 스케일). 의미를 흉내 내지 않는다."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(byte - 127.5) / 127.5 for byte in digest[:_FAKE_DIM]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else -1.0


class FakeCatalog:
    """앵커의 accept 문자열 전부를 "사전"으로 삼는 결정론 벡터 공간.

    실 pg-catalog 접근 없이 search_categories_pg/exact_lookup 의 계약(정렬된 (canonical, distance)
    반환)만 흉내 낸다.
    """

    def __init__(self, categories: list[str]) -> None:
        self.categories = sorted(set(categories))
        self.vectors = {category: _fake_vector(category) for category in self.categories}

    def search(self, vec: list[float], dsn: str, *, k: int) -> list[tuple[str, float]]:  # noqa: ARG002
        scored = [(_cosine(vec, cvec), category) for category, cvec in self.vectors.items()]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(category, round(1.0 - similarity, 4)) for similarity, category in scored[:k]]

    def exact(self, values, dsn: str) -> set[str]:  # noqa: ANN001, ARG002
        known = set(self.categories)
        return {value for value in values if value in known}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_fake_vector(text) for text in texts]


def build_fake_catalog(anchors: AnchorSet) -> FakeCatalog:
    categories: list[str] = []
    for cell in anchors.cells:
        for leg in cell.expected_legs:
            categories.extend(leg.accept)
    return FakeCatalog(categories)


def _field(user: str, prefix: str) -> str:
    for line in user.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


class ScriptedCategoryLLM:
    """앵커의 정답을 되돌려주되 고정 주기로 오답·intent 슬립을 섞는 가짜 클라이언트.

    decompose 호출과 §4.4 카테고리 택일 호출을 **system 문면**으로 가른다(map_categories 가
    같은 llm 인자로 두 종류를 부르므로, intent_probe.fakes.ScriptedDecomposeLLM 이 category_scope
    를 가르는 것과 동일 패턴).
    """

    def __init__(
        self,
        anchors: AnchorSet,
        *,
        fail_first: int = 0,
        always_fail: bool = False,
        wrong_every: int = 7,
        intent_slip_every: int = 0,
    ) -> None:
        self.anchors = anchors
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.wrong_every = wrong_every
        self.intent_slip_every = intent_slip_every
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._per_cell_index: dict[str, int] = {}
        self._by_text: dict[str, AnchorCell] = {cell.utterance: cell for cell in anchors.cells}

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
        if system == _SELECT_SYSTEM:
            return json.dumps(self._select_answer(user), ensure_ascii=False)
        return json.dumps(self._decompose_answer(user), ensure_ascii=False)

    def _select_answer(self, user: str) -> dict[str, Any]:
        candidates = re.findall(r"^- (.+)$", user, flags=re.MULTILINE)
        return {"category": candidates[0] if candidates else None}

    def _decompose_answer(self, user: str) -> dict[str, Any]:
        text = _field(user, _USER_MESSAGE_PREFIX)
        cell = self._by_text.get(text)
        index = self._per_cell_index.get(text, 0)
        self._per_cell_index[text] = index + 1
        if cell is None:
            return self._envelope("general")
        if self.intent_slip_every and index % self.intent_slip_every == self.intent_slip_every - 1:
            return self._envelope("general")
        wrong = bool(self.wrong_every) and index % self.wrong_every == self.wrong_every - 1
        category_queries = self._category_queries(cell, wrong=wrong)
        return self._envelope("recommend", category_queries=category_queries)

    def _category_queries(self, cell: AnchorCell, *, wrong: bool) -> list[dict[str, Any]]:
        if cell.slice_id in ("none", "notInCatalog"):
            return []
        legs = cell.expected_legs
        out: list[dict[str, Any]] = []
        for leg in legs:
            accept = leg.accept[0]
            leaf = accept.rsplit(" > ", 1)[-1]
            if wrong:
                # 오답 주기 — 존재하지 않는 raw 를 내 임베딩 보정 경로(exact 미스)를 태운다.
                out.append({"category": f"오답후보 > {leaf}", "query": leaf})
            else:
                out.append({"category": accept, "query": leaf})
        return out

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        yield await self.complete(system=system, user=user, tier=tier, max_tokens=max_tokens)

    def _envelope(
        self, intent: str, *, category_queries: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return {
            "intent": intent,
            "reply": "네" if intent == "general" else "",
            "case": 2,
            "semanticQuery": "",
            "filters": {},
            "cart": {"productId": None, "optionId": None, "quantity": 1},
            "revertCategories": [],
            "repurchaseProducts": [],
            "categoryQueries": category_queries or [],
            "buyAll": False,
            "totalBudget": None,
            "scopedToPrevious": False,
        }
