"""#132 조사 — 실제 추천 턴이 I-1 에 **무엇을 보내는지** 와이어 그대로 캡처한다.

AI 의 canonical 카테고리(pg-catalog `categories`)는 `"PC부품 > CPU"` 같은 **경로**인데 BE 의
`category.name` 은 맨 이름(`CPU`)이다. 이 형식 차이가 실제 요청에서 어떻게 나타나는지, 그래서
후보가 실제로 잡히는지를 추론이 아니라 캡처로 확인한다.

⚠️ **실제 LLM 을 호출한다** — 발화당 decompose 1회 + 카테고리 매핑(임베딩 + 택일). 조사용이라
상시 실행 대상이 아니다.

전제
    로컬 BE 기동 + 실 카탈로그 적재, pg-catalog 기동
    uv run python scripts/capture_i1_wire_132.py
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agents.buyer.graph import (
    ThreadFilterStore,
    _PrepareRecommendationOut,
    _prepare_recommendation,
)
from app.agents.buyer.recommendation.decompose import decompose
from app.core.config import get_settings
from app.core.llm import get_llm
from app.services import spring_client

UTTERANCES = [
    "가벼운 남자 반팔 티셔츠 추천해줘",
    "노트북 CPU 뭐가 좋아?",
    "선크림 하나 사려는데",
]


def _install_wire_capture(sink: list[dict]) -> None:
    """`_search_query_params` 를 감싸 **실제 전송 파라미터**를 기록한다.

    `search_products` 를 감싸면 filters(내부 표현)만 보이고 와이어 형태는 안 보인다 — BE 가 받는
    것은 이 함수의 산출물이므로 여기서 떠야 "무엇을 보냈나"에 답이 된다.
    """
    original = spring_client._search_query_params  # noqa: SLF001 - 조사용 경계 교체

    def wrapped(filters, *, color_values=None):
        params = original(filters, color_values=color_values)
        sink.append(dict(params))
        return params

    spring_client._search_query_params = wrapped  # noqa: SLF001


async def _run(utterance: str) -> None:
    settings = get_settings()
    llm = get_llm()
    sent: list[dict] = []
    _install_wire_capture(sent)

    decision = await decompose(
        llm,
        query=utterance,
        prior_filters=None,
        profile_summary=None,
        tier="fast",
        category_fanout_max=settings.category_fanout_max,
    )
    # `_prepare_recommendation` 은 progress 프레임(mapping/expanding)을 내야 해서 async
    # generator 다 — 이 스크립트는 프레임을 쓰지 않고 `decision` 부수효과만 보므로 그냥 버린다.
    out = _PrepareRecommendationOut()
    async for _ in _prepare_recommendation(
        request=SimpleNamespace(message=utterance, session_id="s", thread_id="t"),
        decision=decision,
        prior=None,
        llm=llm,
        settings=settings,
        map_categories=None,  # 실제 매퍼 — canonical 이 무엇으로 정해지는지 보려는 것이다
        expand_needs=None,
        observer=None,
        thread_store=ThreadFilterStore(),
        thread_key="capture-132",
        out=out,
    ):
        pass

    print(f"\n발화: {utterance}")
    print(f"  canonical legs : {[c for c, _ in decision.category_legs]}")

    # 매핑까지만 보고 검색은 직접 돌린다 — `_prepare_recommendation` 은 검색 전 단계다.
    from app.services import search_service

    for canonical, _query in decision.category_legs or [(None, None)]:
        leg_filters = decision.filters.model_copy(update={"category": canonical})
        sent.clear()
        try:
            result = await search_service.search_catalog(leg_filters)
            found = len(result.products)
        except Exception as exc:  # noqa: BLE001 - 조사 스크립트
            found = f"실패({type(exc).__name__})"
        wire = sent[-1] if sent else {}
        print(f"  → 전송 categoryName={wire.get('categoryName')!r}  후보={found}")


async def _main() -> None:
    for utterance in UTTERANCES:
        try:
            await _run(utterance)
        except Exception as exc:  # noqa: BLE001 - 한 발화 실패가 나머지를 막지 않게
            print(f"\n발화: {utterance}\n  ! {type(exc).__name__}: {str(exc)[:140]}")


if __name__ == "__main__":
    asyncio.run(_main())
