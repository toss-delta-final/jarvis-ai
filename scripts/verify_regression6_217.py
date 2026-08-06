"""#115 회귀 타깃 6건 라이브 재검증 — DESIGN-NEEDS-EXPANSION-198 §15.5.

`DESIGN-CATEGORY-HYBRID-59` §4.0.1 이 "회귀 6/6 재현 안 됨"으로 닫은 6건을, #217 로 바뀐 배선
(매핑 먼저 → 실패 시 전개 → 재매핑 → 합집합)에서 다시 돌린다.

전제
    docker compose up -d pg-catalog pg-profile
    uv run python scripts/verify_regression6_217.py [회차수]

⚠️ **실제 LLM 을 호출한다** — 발화 6종 × 회차당 `decompose 1회 + (전개 1회) + (택일 ≤2회)`.
기본 2회차면 대략 12~30 호출이고 비용이 든다. 임베딩만 쓰는
`verify_expansion_trigger_217.py` 와 달리 상시 실행용이 아니다.

프로덕션 `_prepare_recommendation` 을 **그대로 호출**한다 — 스크립트가 흐름을 흉내내면 그 흉내가
틀렸을 때 검증이 무의미해진다. 그래프가 바뀌면 이 스크립트도 함께 깨져야 맞다.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace

from app.agents.buyer.graph import (
    ThreadFilterStore,
    _PrepareRecommendationOut,
    _prepare_recommendation,
)
from app.agents.buyer.recommendation.decompose import decompose
from app.core.config import get_settings
from app.core.llm import get_llm

# (발화, 종전 버그 카테고리, #115 §4.0.1 재검증 결과)
CASES: list[tuple[str, str, str]] = [
    ("겨울에 손이 너무 시려워", "고양이용품 > 생활용품", "패션잡화 > 장갑"),
    ("갓성비 무선이어폰", "자동차기기 > 카오디오", "음향가전 > 블루투스 이어폰"),
    ("층간소음 방지 용품", "전기생활용품", "층간소음방지용품 / 문풍지·틈막이"),
    ("부모님 환갑 선물", "취미 > 수집용품", "전개 → 디퓨저·온수매트·안마의자"),
    ("자취 시작할 때 필요한거", "유아침구", "주방 4종"),
    ("발 시려울 때 신을거", "건강관리용품", "무필터 / 등산양말"),
]


class _Capture(logging.Handler):
    """전개 트리거·매핑 판정 로그만 모은다 — 어느 경로를 탔는지가 이 검증의 절반이다."""

    WATCH = {
        "needs_expansion_triggered",
        "needs_expanded",
        "needs_expansion_union",
        "needs_expansion_failed",
        "category_distance_rejected",
        "category_select_null",
        "category_distance_override",
    }

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg in self.WATCH:
            extra = getattr(record, "reason", None) or getattr(record, "canonical", None) or ""
            self.events.append(f"{record.msg}({extra})" if extra else str(record.msg))


async def _run_one(utterance: str, cap: _Capture) -> tuple[list[str], int, list[str]]:
    s = get_settings()
    llm = get_llm()
    if llm is None:
        raise SystemExit("LLM 미구성 — 이 스크립트는 실 LLM 이 있어야 의미가 있다.")
    decision = await decompose(
        llm,
        query=utterance,
        prior_filters=None,
        profile_summary=None,
        tier="fast",
        category_fanout_max=s.category_fanout_max,
    )
    cap.events.clear()
    # `_prepare_recommendation` 은 progress 프레임(mapping/expanding)을 내야 해서 async
    # generator 다 — 이 스크립트는 프레임을 쓰지 않고 `decision` 부수효과만 보므로 그냥 버린다.
    out = _PrepareRecommendationOut()
    async for _ in _prepare_recommendation(
        request=SimpleNamespace(message=utterance, session_id="s", thread_id="t"),
        decision=decision,
        prior=None,
        llm=llm,
        settings=s,
        map_categories=None,  # 실제 매퍼
        expand_needs=None,  # 실제 전개기
        observer=None,
        thread_store=ThreadFilterStore(),
        thread_key="regression-217",
        out=out,
    ):
        pass
    return [c for c, _ in decision.category_legs], decision.case, list(cap.events)


async def main(rounds: int) -> None:
    cap = _Capture()
    app_logger = logging.getLogger("app")
    app_logger.addHandler(cap)
    app_logger.setLevel(logging.INFO)

    reproduced: list[str] = []
    for utterance, old_bug, expected in CASES:
        print(f"\n■ {utterance}")
        print(f"   종전 버그: {old_bug}   |   #115 재검증: {expected}")
        for r in range(1, rounds + 1):
            try:
                cats, case, events = await _run_one(utterance, cap)
            except Exception as exc:  # noqa: BLE001 - 회차 하나가 죽어도 나머지를 본다
                print(f"   [{r}] 실행 실패: {type(exc).__name__}: {exc}")
                continue
            shown = cats or ["(무필터)"]
            # 종전 버그의 leaf 명이 결과에 다시 나타나면 회귀다.
            leaf = old_bug.split(" > ")[-1]
            hit = any(leaf in c for c in shown)
            if hit:
                reproduced.append(f"{utterance}[{r}]")
            print(f"   [{r}] case={case}  legs={shown}{'  <<< 회귀 재현!' if hit else ''}")
            if events:
                print(f"        경로: {' · '.join(events)}")

    print("\n=== 요약 ===")
    print(f"회귀 재현 {len(reproduced)}건 {reproduced if reproduced else '(6/6 재현 안 됨)'}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2))
