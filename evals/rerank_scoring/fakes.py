"""Deterministic provider and replay clients for rerank-scoring evaluation."""

from __future__ import annotations

import json
from typing import Literal

from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE

FaultMode = Literal["valid", "duplicate", "missing", "out_of_range", "out_of_candidate"]


def _candidates(user: str) -> list[dict[str, object]]:
    marker = "CANDIDATES: "
    if marker not in user:
        return []
    payload = json.loads(user.split(marker, 1)[1])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _grounding(candidate: dict[str, object]) -> tuple[str, list[str], str]:
    if candidate.get("ratingLevel") in {"높음", "매우높음"}:
        return "RATING_HIGH", ["ratingLevel"], "평점 평가가 높은 상품이에요"
    if candidate.get("reviewLevel") in {"많음", "매우많음"}:
        return "REVIEW_MANY", ["reviewLevel"], "리뷰 정보가 많은 상품이에요"
    if candidate.get("priceLevel") in {"저렴", "매우저렴"}:
        return "PRICE_RELATIVE_LOW", ["priceLevel"], "같은 후보군에서 비교적 저렴해요"
    return "NO_VERIFIABLE_EVIDENCE", [], NEUTRAL_RATIONALE


def _code_evidence_refs(candidate: dict[str, object]) -> list[str]:
    signals = candidate.get("codeSignals")
    if not isinstance(signals, dict):
        return []
    raw_evidence = signals.get("evidence")
    if not isinstance(raw_evidence, list):
        return []
    refs: list[str] = []
    for raw_item in raw_evidence:
        ref = raw_item.get("ref") if isinstance(raw_item, dict) else None
        if isinstance(ref, str):
            refs.append(ref)
    return refs


class ReplayLLM:
    """Return one captured response without another provider call."""

    def __init__(self, raw_response: str) -> None:
        self.raw_response = raw_response
        self.calls = 0

    async def complete(self, **kwargs: object) -> str:
        del kwargs
        self.calls += 1
        return self.raw_response


class ScriptedScoringLLM:
    """Generate deterministic legacy or scored output from the actual prompt candidates."""

    def __init__(self, *, mode: FaultMode = "valid") -> None:
        self.mode = mode
        self.model_config = {"provider": "ScriptedScoringLLM", "mode": mode}
        self.current_calls = 0
        self.scored_calls = 0
        self.code_assisted_calls = 0
        self.candidate_orders: list[tuple[int, ...]] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        del tier, max_tokens, json_output
        candidates = _candidates(user)
        product_ids = tuple(
            int(item["productId"])
            for item in candidates
            if isinstance(item.get("productId"), int)
            and not isinstance(item.get("productId"), bool)
        )
        self.candidate_orders.append(product_ids)
        code_assisted = '"semanticIntentFit"' in system
        if code_assisted:
            self.code_assisted_calls += 1
            ranked: list[dict[str, object]] = []
            for candidate in candidates:
                product_id = candidate.get("productId")
                if isinstance(product_id, bool) or not isinstance(product_id, int):
                    continue
                if self.mode == "missing" and product_id == product_ids[0]:
                    continue
                semantic_intent_fit = 4
                if self.mode == "out_of_range" and product_id == product_ids[0]:
                    semantic_intent_fit = 5
                ranked.append(
                    {
                        "productId": product_id,
                        "semanticIntentFit": semantic_intent_fit,
                        "useCaseFit": 3,
                        "profileFit": 0,
                        "semanticReasonCode": "DIRECT_INTENT_MATCH",
                        "evidenceRefs": _code_evidence_refs(candidate)[:2],
                        "rationale": "요청 의도와 코드 근거를 함께 고려했어요",
                    }
                )
            if self.mode == "duplicate" and ranked:
                ranked.append(dict(ranked[0]))
            if self.mode == "out_of_candidate":
                ranked.append(
                    {
                        "productId": 9_999_999,
                        "semanticIntentFit": 4,
                        "useCaseFit": 3,
                        "profileFit": 0,
                        "semanticReasonCode": "DIRECT_INTENT_MATCH",
                        "evidenceRefs": [],
                        "rationale": NEUTRAL_RATIONALE,
                    }
                )
            return json.dumps(
                {
                    "ranked": ranked,
                    "overallComment": "코드 신호와 의미 적합도를 함께 고려했어요",
                    "overallClaims": [
                        {
                            "claimCode": "NO_VERIFIABLE_OVERALL_CLAIM",
                            "scope": "FINAL_EXPOSED_PRODUCTS",
                            "subjectProductIds": [],
                            "evidenceFields": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )

        scored = '"evaluations"' in system
        if not scored:
            self.current_calls += 1
            structured_grounding = "reasonCode" in system
            ranked: list[dict[str, object]] = []
            for candidate in candidates:
                product_id = candidate.get("productId")
                if isinstance(product_id, bool) or not isinstance(product_id, int):
                    continue
                code, fields, rationale = _grounding(candidate)
                item: dict[str, object] = {
                    "productId": product_id,
                    "rationale": rationale,
                }
                if structured_grounding:
                    item.update({"reasonCode": code, "evidenceFields": fields})
                ranked.append(item)
            return json.dumps(
                {
                    "ranked": ranked,
                    "overallComment": "검색 후보를 기준으로 골랐어요",
                },
                ensure_ascii=False,
            )

        self.scored_calls += 1
        sorted_ids = sorted(product_ids)
        priority = {product_id: index for index, product_id in enumerate(sorted_ids)}
        evaluations: list[dict[str, object]] = []
        for candidate in candidates:
            product_id = candidate.get("productId")
            if isinstance(product_id, bool) or not isinstance(product_id, int):
                continue
            if self.mode == "missing" and product_id == product_ids[0]:
                continue
            index = priority[product_id]
            intent_fit = max(0, 4 - index // 4)
            need_fit = max(0, 3 - index % 4)
            if self.mode == "out_of_range" and product_id == product_ids[0]:
                intent_fit = 5
            code, fields, rationale = _grounding(candidate)
            evaluations.append(
                {
                    "productId": product_id,
                    "intentFit": intent_fit,
                    "needFit": need_fit,
                    "profileFit": 0,
                    "rationale": rationale,
                    "reasonCode": code,
                    "evidenceFields": fields,
                }
            )
        if self.mode == "duplicate" and evaluations:
            evaluations.append(dict(evaluations[0]))
        if self.mode == "out_of_candidate":
            evaluations.append(
                {
                    "productId": 9_999_999,
                    "intentFit": 4,
                    "needFit": 3,
                    "profileFit": 0,
                    "rationale": NEUTRAL_RATIONALE,
                    "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                    "evidenceFields": [],
                }
            )
        return json.dumps(
            {
                "evaluations": evaluations,
                "overallComment": "점수 구성요소를 기준으로 골랐어요",
                "overallClaims": [
                    {
                        "claimCode": "NO_VERIFIABLE_OVERALL_CLAIM",
                        "scope": "FINAL_EXPOSED_PRODUCTS",
                        "subjectProductIds": [],
                        "evidenceFields": [],
                    }
                ],
            },
            ensure_ascii=False,
        )
