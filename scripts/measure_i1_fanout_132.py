"""#132 — 추천 1턴이 I-1 을 **몇 번, 몇 개 동시에** 부르는지 실측 (실 BE 필요, LLM 불필요).

이슈는 증폭을 "leg 수(≤ `category_fanout_max`)만큼"으로 봤지만 코드에는 곱이 셋이다:
  fan-out leg(≤5) × 자동 완화 라운드(≤`relaxation_max_rounds`) + 완화칩 probe(≤`relaxation_max_probes`)
그리고 완화칩 probe 는 `asyncio.gather` 로 **동시에** 나가며 각 probe 가 다시 leg 수만큼 fan-out 한다.
상한은 코드에서 읽히지만 **실제 턴이 그 상한에 닿는지**는 돌려 봐야 안다 — 이 스크립트가 그걸 센다.

LLM 은 부르지 않는다. decompose 산출(`RouteDecision`)을 손으로 만들어 주입하므로 fan-out·완화
경로는 전부 코드가 결정하고, rerank 만 스크립트가 대신 답한다. 결정적이고 비용이 없다.

전제
    로컬 BE 기동 + 실 카탈로그 적재(docs/specs/MEASURE-I1-RESPONSE-132.md §1)
    SPRING_BASE_URL 이 그 BE 를 가리킬 것
    uv run python scripts/measure_i1_fanout_132.py [--legs 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass

from app.agents.buyer.recommendation.graph import stream_recommendation
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters
from app.services import search_service, spring_client


@dataclass
class _Counter:
    """I-1 호출 수와 **동시 최대치**를 센다 — 증폭의 두 축이다."""

    total: int = 0
    inflight: int = 0
    peak: int = 0

    def enter(self) -> None:
        self.total += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)

    def leave(self) -> None:
        self.inflight -= 1


def _install_counter(counter: _Counter):
    """`search_products` 를 계수기로 감싼다 — 실제 BE 호출은 그대로 나간다."""
    original = spring_client.search_products

    async def counted(filters: ProductSearchFilters):
        counter.enter()
        try:
            return await original(filters)
        finally:
            counter.leave()

    spring_client.search_products = counted  # noqa: SLF001 - 측정용 경계 교체
    return original


class _ScriptedLLM:
    """rerank 만 답하는 최소 LLM — 후보 id 를 그대로 돌려준다(순서·근거는 측정과 무관)."""

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        ids = [item["productId"] for item in json.loads(user.split("CANDIDATES: ", 1)[1])]
        return json.dumps(
            {
                "ranked": [{"productId": pid, "rationale": "측정"} for pid in ids],
                "overallComment": "측정",
            },
            ensure_ascii=False,
        )


async def _heartbeat(stop: asyncio.Event, interval: float = 0.01) -> list[float]:
    """10ms tick 이 밀린 시간 = 이벤트루프가 막혀 다른 사용자 SSE 가 멈춘 시간."""
    gaps: list[float] = []
    prev = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = time.perf_counter()
        gaps.append(max(0.0, now - prev - interval))
        prev = now
    return gaps


async def _noop_push(_payload) -> bool:
    """I-21 push 대역 — 측정 대상이 아니라 성공만 답한다."""
    return True


@dataclass
class _Request:
    """`stream_recommendation` 이 실제로 읽는 필드만 — `message` 와 `session_id` 둘뿐이다."""

    message: str
    session_id: str = "measure-132-session"


async def _run(label: str, decision: RouteDecision, legs: int) -> None:
    settings = get_settings()
    counter = _Counter()
    original = _install_counter(counter)

    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop))
    await asyncio.sleep(0.05)  # 하트비트 정상화

    started = time.perf_counter()
    frames = 0
    try:
        async for _frame in stream_recommendation(
            request=_Request(message="측정용 발화"),
            decision=decision,
            llm=_ScriptedLLM(),
            # 라이브 기본 폴백(`search or search_catalog`)은 `run_buyer_turn` 에만 있고
            # `stream_recommendation` 에는 없다 — 명시로 넘겨야 실 BE 로 나간다.
            search=search_service.search_catalog,
            # I-21 push 는 측정 대상이 아니다 — 성공했다고만 답한다(`bool(await push_fn(...))`).
            push_fn=_noop_push,
            profile=None,
            settings=settings,
            request_id="measure-132",
        ):
            frames += 1
    except Exception as exc:  # noqa: BLE001 - 측정 스크립트: 실패해도 수치는 남긴다
        print(f"  ! 스트림 예외: {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        spring_client.search_products = original
        stop.set()
        gaps = await beat

    elapsed = (time.perf_counter() - started) * 1000
    gap_max = (max(gaps) if gaps else 0.0) * 1000
    print(
        f"{label:<22} legs={legs:<2} I-1호출={counter.total:<3} 동시최대={counter.peak:<3} "
        f"소요={elapsed:>8.1f}ms 루프정지최대={gap_max:>7.1f}ms frames={frames}"
    )


async def _main(legs: int, categories: list[str]) -> None:
    settings = get_settings()
    print(
        f"config: fanout_max={settings.category_fanout_max} "
        f"auto_rounds={settings.relaxation_max_rounds} probes={settings.relaxation_max_probes} "
        f"→ 코드상 상한: 호출 {settings.category_fanout_max * (1 + settings.relaxation_max_rounds + settings.relaxation_max_probes)}, "
        f"동시 {settings.category_fanout_max * settings.relaxation_max_probes}"
    )
    print("-" * 108)

    used = [(c, None) for c in categories[:legs]]

    # (1) 결과가 넉넉한 턴 — 완화가 안 돌아 fan-out 만 나간다.
    await _run(
        "넉넉한 결과",
        RouteDecision(intent="recommend", filters=ProductSearchFilters(), category_legs=used),
        len(used),
    )

    # (2) 0건 턴 — 자동 완화(ratingMin)와 완화칩 probe 가 모두 발동하는 경로.
    #     도달 불가능한 가격 상한으로 0건을 만들고, ratingMin 을 줘 자동 완화 대상을 만든다.
    await _run(
        "0건(자동완화+probe)",
        RouteDecision(
            intent="recommend",
            filters=ProductSearchFilters(price_max=1, rating_min=4.9),
            category_legs=used,
        ),
        len(used),
    )

    # (3) probe 동시성 최대 시나리오 — 완화 **칩** 은 축마다 하나씩 만들어지고 `asyncio.gather`
    #     로 동시에 나간다. ratingMin 을 빼면 자동 완화가 축을 하나 선점하지 않아 price·brand·
    #     color 세 축이 모두 probe 후보로 남는다. 각 probe 가 다시 leg 수만큼 fan-out 하므로
    #     동시 최대는 (probe 수 × leg 수)여야 한다 — 그 곱이 실제로 나오는지가 이 시나리오의 요점.
    await _run(
        "0건(probe 동시 최대)",
        RouteDecision(
            intent="recommend",
            filters=ProductSearchFilters(
                price_max=1, brand=["존재하지않는브랜드"], color="존재하지않는색"
            ),
            category_legs=used,
        ),
        len(used),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="추천 1턴의 I-1 호출 증폭 실측(#132)")
    parser.add_argument("--legs", type=int, default=5, help="fan-out leg 수")
    parser.add_argument(
        "--categories",
        default="티셔츠,선크림/선블록,여성 언더웨어,브랜드 여성시계,브랜드 여성주얼리",
        help="leg 로 쓸 canonical 카테고리(쉼표 구분) — 실 카탈로그에 존재해야 한다",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.legs, [c for c in args.categories.split(",") if c.strip()]))


if __name__ == "__main__":
    main()
