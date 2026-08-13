import json

import pytest

from app.agents.buyer.recommendation.rerank import _SYSTEM_STRUCTURED_SCORING
from tests._fakes import FakeLLM


@pytest.mark.asyncio
async def test_fake_llm_adapts_legacy_ranked_fixture_to_scored_contract() -> None:
    llm = FakeLLM(
        rerank={
            "ranked": [
                {"productId": 102, "rationale": "두 번째 상품 우선"},
                {"productId": 101, "rationale": "첫 번째 상품 다음"},
            ],
            "overallComment": "구조화 기본 테스트",
        }
    )

    raw = await llm.complete(
        system=_SYSTEM_STRUCTURED_SCORING,
        user="{}",
        tier="smart",
    )

    payload = json.loads(raw)
    assert [row["productId"] for row in payload["evaluations"]] == [102, 101]
    assert payload["evaluations"][0]["rationale"] == "두 번째 상품 우선"
    scores = [row["intentFit"] * 4 + row["needFit"] * 2 for row in payload["evaluations"]]
    assert scores[0] > scores[1]
    assert payload["overallComment"] == "구조화 기본 테스트"
