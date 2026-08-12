"""유의성 게이트 — `interpret` 를 돌릴지 정한다 (이슈 #594, `01-ARCHITECTURE` §4.4).

*"`compute` 종료 시 `verdicts` 가 전부 `no_significant_change`/`undecided` 면 `interpret`
스킵(LLM 0회)"* 이 원문이다. 그런데 `behavior` 는 **verdict 를 하나도 만들지 않는 워커**라
문자 그대로 읽으면 빈 목록에 대한 `all()` 이 공허참이 되어 **언제나 스킵**된다. 세그먼트가
멀쩡히 있는데 서술을 못 하는 것은 명백히 의도가 아니므로 마지막 분기를 둔다.

⚠️ 이것은 **트리거 발동 게이트가 아니다.** "고정 임계 AND 통계 유의"로 보고서를 낼지
정하는 판정은 `10-TRIGGER` §3 이고 그 순수 함수는 이슈 #595(`scan.py`) 소관이다 —
여기서 같은 판정을 흉내 내면 시뮬레이션이 검증한 것과 운영이 도는 것이 갈린다.
이 함수가 정하는 것은 오직 **워커 1종의 LLM 호출 1회를 아낄지**다.
"""

from __future__ import annotations

from app.agents.seller.sop.context import AnalysisContext

# 방향을 가진 판정만 "서술할 것이 있다"로 본다. `undecided`(판정 보류)는 서술 재료가
# 아니라 한계 표기라 `holds[]`·`hasHolds` 로 이미 판매자에게 드러난다.
DECISIVE_VERDICTS = frozenset({"significant_drop", "significant_rise"})


def should_interpret(ctx: AnalysisContext) -> bool:
    """이 워커의 `interpret`(LLM 1회)를 돌려야 하는가.

    판단 순서:
    1. 유의 판정이 1건이라도 있으면 → 돌린다.
    2. 판정을 했는데 전부 무변화/보류면 → 스킵(LLM 0회). 억지 서술 금지 규약의 실행부다.
    3. 판정 축 자체가 없는 워커(`behavior`)면 → 서술 재료(`segments`·`product_flags`)
       유무로 정한다.
    """
    if any(verdict.verdict in DECISIVE_VERDICTS for verdict in ctx.verdicts):
        return True
    if ctx.verdicts:
        return False
    return bool(ctx.segments or ctx.product_flags)
