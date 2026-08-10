"""#443 사전 기반 단일 leg 주입; 카탈로그 이름만 정확 일치로 사용한다.

근거: N=24 독립 2런에서 namedCategoryHasLeg가 98.6%·100.0%(문턱 83.7%)로 올랐고,
conditionOnlyNoCategoryQuery는 90.0%·92.5%(문턱 84.2%)를 유지했다. 문면 7종의 최대
+7.3%p 개선은 반대 축 −10.8%p 비용을 냈지만, 이 결정론 보강은 +25%p와 반대 축 손실 0을
보였다. 정본 사전에는 조건어가 없어 condition_only 발화는 매칭 자체가 불가능하다.

사전 `app/data/seller_categories.json`은 정본 DB 스냅샷이다. 손으로 고치지 말 것.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.agents.buyer.recommendation.state import CategoryQuery

DEFAULT_CATEGORY_LEG_INJECTION_PATH = str(
    Path(__file__).resolve().parents[3] / "data" / "seller_categories.json"
)


@lru_cache(maxsize=4)
def _names(path: str) -> tuple[str, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {part for row in payload["categories"] for segment in row["path"] for part in segment.split(" > ")}
    # 최장 일치가 우선이다. 길이가 같으면 set 순회 순서가 결과를 흔들지 않도록 역어순으로 고정한다.
    return tuple(sorted((name for name in names if len(name) >= 2), key=lambda name: (len(name), name), reverse=True))


def inject_category_leg(
    legs: list[CategoryQuery],
    *,
    intent: str,
    utterance: str,
    path: str = DEFAULT_CATEGORY_LEG_INJECTION_PATH,
    min_length: int,
) -> list[CategoryQuery]:
    """빈 recommend leg에만 최장 카탈로그명을 category=null leg로 넣는다.

    한국어 어절 경계를 판정하지 않는 단순 부분문자열 매칭이다. 따라서 사전의 짧은 이름이
    무관한 어절 내부에 포함되면 오탐할 수 있으며, 이 위험은 적대적 회귀 테스트로 명시한다.
    """
    if intent != "recommend" or legs:
        return legs
    try:
        names = _names(path)
    except Exception:
        # 사전은 선택적 보강이다. 파일·JSON·스키마 문제가 추천 턴 자체를 깨면 안 된다.
        return legs
    for name in names:
        if len(name) >= min_length and name in utterance:
            return [CategoryQuery(raw_category=None, query=name)]
    return legs
