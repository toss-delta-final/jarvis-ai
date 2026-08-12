"""Deterministic LLM fakes for the rerank-grounding harness."""

from __future__ import annotations

import json

from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE


def _candidates(user: str) -> list[dict[str, object]]:
    marker = "CANDIDATES: "
    if marker not in user:
        return []
    payload = json.loads(user.split(marker, 1)[1])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _supported_reason(candidate: dict[str, object]) -> tuple[str, list[str], str]:
    if candidate.get("ratingLevel") in {"높음", "매우높음"}:
        return "RATING_HIGH", ["ratingLevel"], "평점 평가가 높은 상품이에요"
    if candidate.get("reviewLevel") in {"많음", "매우많음"}:
        return "REVIEW_MANY", ["reviewLevel"], "리뷰 정보가 많은 상품이에요"
    if candidate.get("priceLevel") in {"저렴", "매우저렴"}:
        return "PRICE_RELATIVE_LOW", ["priceLevel"], "같은 후보군에서 비교적 저렴해요"
    return "NO_VERIFIABLE_EVIDENCE", [], NEUTRAL_RATIONALE


class ScriptedGroundingLLM:
    """Derive deterministic outputs from the actual candidates sent by rerank."""

    def __init__(
        self,
        *,
        invalid_evidence: bool = False,
        invalid_overall: bool = False,
    ) -> None:
        self.invalid_evidence = invalid_evidence
        self.invalid_overall = invalid_overall

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        candidates = _candidates(user)
        structured = "reasonCode" in system
        ranked: list[dict[str, object]] = []
        for index, candidate in enumerate(candidates):
            product_id = candidate.get("productId")
            if not isinstance(product_id, int):
                continue
            if self.invalid_evidence and index == 0:
                rationale = "평점 5.0이고 리뷰 999개예요"
                if structured:
                    ranked.append(
                        {
                            "productId": product_id,
                            "rationale": rationale,
                            "reasonCode": "RATING_HIGH",
                            "evidenceFields": ["ratingLevel"],
                        }
                    )
                else:
                    ranked.append({"productId": product_id, "rationale": rationale})
                continue
            code, fields, rationale = _supported_reason(candidate)
            item: dict[str, object] = {"productId": product_id, "rationale": rationale}
            if structured:
                item.update({"reasonCode": code, "evidenceFields": fields})
            ranked.append(item)
        payload: dict[str, object] = {
            "ranked": ranked,
            "overallComment": "조건에 맞춰 골라봤어요",
        }
        if structured:
            product_ids = [
                candidate["productId"]
                for candidate in candidates
                if isinstance(candidate.get("productId"), int)
            ]
            if self.invalid_overall:
                payload["overallClaims"] = [
                    {
                        "claimCode": "POPULARITY_TOP",
                        "scope": "FINAL_EXPOSED_PRODUCTS",
                        "subjectProductIds": product_ids[:1],
                        "evidenceFields": [],
                    }
                ]
            elif product_ids and all(
                candidate.get("ratingLevel") in {"높음", "매우높음"} for candidate in candidates
            ):
                payload["overallClaims"] = [
                    {
                        "claimCode": "ALL_RATING_HIGH",
                        "scope": "FINAL_EXPOSED_PRODUCTS",
                        "subjectProductIds": product_ids,
                        "evidenceFields": ["ratingLevel"],
                    }
                ]
            else:
                payload["overallClaims"] = [
                    {
                        "claimCode": "NO_VERIFIABLE_OVERALL_CLAIM",
                        "scope": "FINAL_EXPOSED_PRODUCTS",
                        "subjectProductIds": [],
                        "evidenceFields": [],
                    }
                ]
        return json.dumps(payload, ensure_ascii=False)
