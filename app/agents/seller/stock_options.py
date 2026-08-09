"""옵션명 → 재고 행 해소 (#524) — "블랙만 10개로"를 어느 optionId 에 반영할지 정한다.

I-11 이 `stocks[{optionId, quantity}]` 를 받게 되면(옵션별 재고, BE PR B) 실행 계층은
반드시 optionId 를 알아야 한다. LLM 에게 id 를 맡기지 않는다 — 조회 목록의 **옵션명**
만 옮겨 적게 하고(DraftChange.option_name), 이 모듈이 confirm 시점의 I-9 `stocks[]`
로 해소한다("계약값은 코드", hitl.DraftRecord 와 동일 원칙). 이름은 스냅샷이라
초안과 실행 사이에 옵션이 갈렸으면 여기서 걸려 되묻기로 전환된다.

매칭 규약은 구매자 레인 narrow_options(app/agents/buyer/cart/options.py) 의 세그먼트
방식을 차용하되 문제 자체가 다르다: narrow_options 는 **발화 토큰**에서 옵션을 좁히고
(부분일치 허용 티어 포함), 여기는 LLM 이 조회 목록에서 **옮겨 적은 이름 하나**를
행에 잇는다. 그래서 완전일치 > 세그먼트 일치 > 부분일치 순서에 **유일할 때만** 채택
한다 — "블랙" 이 "블랙/M"·"블랙/L" 둘 다에 걸리면 고르지 않고 되묻는다(재고는 돈이다).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.schemas.spring import SellerStockRow

# narrow_options 와 동일한 세그먼트 구분자 — "블랙/M", "블랙, M", "블랙 M" 모두 지원.
_SEGMENT_SPLIT = re.compile(r"[/,|\s]+")


def _normalize(text: str) -> str:
    return text.strip().casefold()


def _segments(name: str) -> set[str]:
    return {seg for seg in _SEGMENT_SPLIT.split(_normalize(name)) if seg}


def option_labels(stocks: Sequence[SellerStockRow]) -> list[str]:
    """되묻기 문구에 나열할 옵션명 목록 — 이름 없는 행은 id 로 표기(정보 손실 방지)."""
    return [
        (row.option_name or f"옵션 {row.option_id}") for row in stocks if row.option_id is not None
    ]


def resolve_stock_option(
    option_name: str | None, stocks: Sequence[SellerStockRow]
) -> tuple[SellerStockRow | None, str | None]:
    """(매칭 행, 되묻기 문구) — 정확히 한쪽만 값이 있다.

    - 옵션 없는 상품(named 0건): 이름 지정 여부와 무관하게 유일한 재고 행
      (option_id null)을 쓴다 — 판매자가 "블랙 재고…"라고 말해도 재고가 한 칸뿐이라
      모호함이 없다. null 행조차 없으면(구 BE 응답) None 을 돌려주고 호출부가
      optionId null 로 다룬다.
    - 옵션 있는 상품: 이름 미지정이면 옵션이 하나일 때만 자동 선택, 여러 개면 되묻기.
      이름 지정이면 완전일치 > 세그먼트 완전일치 > 부분일치 순서로, **유일 매칭만**
      채택한다. 모호·불일치는 되묻기 — 엉뚱한 옵션 재고를 바꾸는 것보다 한 번 더
      묻는 편이 싸다(구매자 F-1 "블루 ⊄ 블루투스" 교훈의 실행 계층판).
    """
    named = [row for row in stocks if row.option_id is not None]
    if not named:
        base = next((row for row in stocks if row.option_id is None), None)
        return base, None

    labels = " · ".join(option_labels(named))
    needle = _normalize(option_name) if option_name else ""
    if not needle:
        if len(named) == 1:
            return named[0], None
        return None, (
            f"이 상품은 옵션별로 재고가 관리됩니다. 어느 옵션의 재고를 바꿀지 "
            f"알려주세요: {labels}"
        )

    exact = [row for row in named if _normalize(row.option_name or "") == needle]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        # 동명 옵션 — 데이터 결함이지만 실행 계층은 고르지 않는다.
        return None, (f"'{option_name}' 옵션이 여러 개라 대상을 특정하지 못했습니다: {labels}")

    needle_segs = _segments(needle)
    seg_hits = [
        row for row in named if needle_segs and needle_segs <= _segments(row.option_name or "")
    ]
    if len(seg_hits) == 1:
        return seg_hits[0], None
    if len(seg_hits) > 1:
        return None, (
            f"'{option_name}' 만으로는 옵션이 하나로 좁혀지지 않습니다. "
            f"다음 중에서 알려주세요: {labels}"
        )

    partial = [row for row in named if needle in _normalize(row.option_name or "")]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, (
            f"'{option_name}' 만으로는 옵션이 하나로 좁혀지지 않습니다. "
            f"다음 중에서 알려주세요: {labels}"
        )
    return None, (
        f"'{option_name}' 옵션을 찾지 못했습니다. 현재 옵션은 {labels} 입니다. "
        "어느 옵션의 재고를 바꿀지 다시 알려주세요."
    )


def stock_lines_text(stock_total: int, stocks: Sequence[SellerStockRow]) -> str:
    """I-9 조회 목록의 재고 표기 — 옵션이 있으면 옵션별로 펼친다.

    이 문자열이 product 에이전트가 option_name 을 옮겨 적는 **원천**이다 — 여기 없는
    이름은 초안에 나올 수 없고, 나오면 resolve_stock_option 이 되묻기로 걸러 낸다.
    구(stockQuantity 단일) BE 응답은 stocks 가 비어 있어 기존 표기 그대로다.
    """
    named = [row for row in stocks if row.option_id is not None]
    if not named:
        return f"재고 {stock_total}건"
    detail = " · ".join(
        f"{row.option_name or f'옵션 {row.option_id}'} "
        + ("품절" if row.quantity == 0 else f"{row.quantity}건")
        for row in named
    )
    return f"재고 합계 {stock_total}건 (옵션별: {detail})"
