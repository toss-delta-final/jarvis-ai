"""화면 지시어를 **코드에서** 해소한다 (이슈 #118, api-spec §3.1 "지시어 해소").

정본이 요구하는 것은 네 가지다.

    | 발화 | 필요한 것 |
    |---|---|
    | "이거 담아줘" | 후보가 **1건일 때만 확정**, 여러 건이면 **되물음** |
    | "3번째 거 담아줘" | 배열 순서 |
    | "3번째 줄 2번째 담아줘" | `columns` — `index = (row-1) × columns + (col-1)` |
    | "무선 이어폰 담아줘" | `name` 매칭 |

이 중 **순번·좌표·"후보 1건" 규칙은 결정적(deterministic)** 이라 LLM 에게 맡길 이유가 없다.
실제로 맡겨 봤더니 나빴다 — 실 LLM N=8 프로브(scripts/verify_screen_context_118.py, 채택안
`SCREEN` 블록 기준)에서:

    | 셀 | 목표 | 성공 | **다른 상품을 확정**(=오담기) |
    |---|---|---|---|
    | `이거 담아줘` · 화면 1건 | 그 상품 | 2/8 | 6/8 (직전 추천의 다른 상품) |
    | `이거 담아줘` · 화면 3건 | **되물음** | 0/8 | **8/8** |
    | `3번째 거 담아줘` · 5건 | 3번째 | 6/8 | 1/8 |
    | `3번째 줄 2번째` · 9건 `columns=3` | 8번째 | 5/8 | 3/8 (이웃 칸) |
    | `301 담아줘` (두 목록 밖 id) | 확정 금지 | 6/8 | 6/8 (엉뚱한 상품으로 대체) |

"다른 상품을 확정"은 **담기 가드가 막지 못한다** — 가드는 두 목록 **밖** id 만 막고, 이 오답들은
전부 목록 **안**이라 그대로 담긴다. 사용자가 말하지 않은 상품이 장바구니에 들어가는 것이라
정확도 문제가 아니라 결함이다. 그래서 이 모듈이 **LLM 산출을 덮어쓴다.**

**발동 조건이 좁다는 것이 이 모듈의 안전 논거다** — `screen.products` 가 실제로 있는 턴에만 돈다.
#234/#239/#240 이 세운 회귀 대조군은 전부 `screen` 이 없는 요청이므로 **구조적으로 영향받지
않는다**(테스트로 고정). 옵션 되물음(`PENDING_CART`) 중에도 돌지 않는다 — 그 턴의 "2번"은 화면
순번이 아니라 옵션 번호이기 때문이다(호출부 `graph.py` 가 그 조건을 건다).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


def grid_position(index: int, columns: int | None) -> tuple[int, int] | None:
    """0-base 배열 인덱스 → 화면 (줄, 칸) 1-base. `columns` 가 없으면 좌표 지시 불가라 None.

    정본 §3.1 의 `index = (row-1) × columns + (col-1)` 을 뒤집은 것이다
    (`resolve_grid_index` 와 왕복 일치해야 한다 — 단위 테스트로 고정).
    """
    if index < 0 or not columns or columns < 1:
        return None
    return index // columns + 1, index % columns + 1


def resolve_grid_index(row: int, col: int, columns: int) -> int:
    """정본 §3.1 좌표 산술 — (줄, 칸) 1-base → 0-base 배열 인덱스."""
    return (row - 1) * columns + (col - 1)


# 한국어 순번·좌표 표기. **튜닝 노브가 아니라 문법**이라 config 가 아니라 여기 둔다
# (app/core/text.py 의 제어문자 정규식과 같은 성격).
#   좌표: "3번째 줄 2번째" · "3줄 2칸" · "세 번째 줄 두 번째" 는 아직 다루지 않는다(숫자 표기만).
_COORD = re.compile(r"(\d+)\s*(?:번째\s*)?(?:줄|행|열)\s*(?:에\s*)?(\d+)\s*(?:번째|번|칸)?")
_ORDINAL = re.compile(r"(\d+)\s*번째")
# 조사·수량 접미가 붙지 않은 **맨 정수 토큰** — 사용자가 상품 id 를 직접 말한 경우.
# 2자리 이상만 본다(한 자리 숫자는 "2번"처럼 순번·옵션 표기일 확률이 높다).
_BARE_NUMBER = re.compile(r"(?<!\d)(\d{2,})(?!\d)\s*(?!번째|번|줄|칸|개|원|월|일|시|분|%)")


@dataclass(frozen=True, slots=True)
class ScreenResolution:
    """코드가 확정한 담기 대상. `product_id=None` 은 **되물음 강제**다(임의 확정 금지)."""

    product_id: int | None
    reason: str


def _mentions_a_product_name(message: str, names: Iterable[str]) -> bool:
    """발화가 화면 상품 **이름**을 지목했는지. 이름 매칭은 LLM 이 8/8 로 잘한다 — 건드리지 않는다."""
    # 2자 미만 이름은 우연 일치가 잦아 신호로 쓰지 않는다.
    return any(len(name) >= 2 and name in message for name in names)


def resolve_screen_reference(
    message: str,
    *,
    products: Sequence[tuple[int, str]],
    columns: int | None,
    allowed_product_ids: set[int],
    deictic_markers: Sequence[str],
) -> ScreenResolution | None:
    """발화의 화면 지시어를 해소한다. None = 해당 규칙 없음(LLM 산출을 그대로 둔다).

    `products` 는 **정제 후 남은 배열**이고 그 순서가 화면 순서라는 전제로 센다
    (`decompose.build_screen_prompt` 주석의 전제와 같다).
    """
    if not products:
        return None
    names = [name for _, name in products]

    # (1) 좌표 — "3번째 줄 2번째". columns 가 없으면 **좌표 지시만 불가**(§3.1 유효성 표)라
    #     확정하지 않고 되물음으로 보낸다(엉뚱한 칸을 담느니 한 번 더 묻는다).
    if coord := _COORD.search(message):
        row, col = int(coord.group(1)), int(coord.group(2))
        if not columns:
            return ScreenResolution(None, "coordinate_without_columns")
        index = resolve_grid_index(row, col, columns)
        if 0 <= index < len(products):
            return ScreenResolution(products[index][0], "coordinate")
        return ScreenResolution(None, "coordinate_out_of_range")

    # (2) 순번 — "3번째 거". 배열 순서만 있으면 풀린다.
    if ordinal := _ORDINAL.search(message):
        index = int(ordinal.group(1)) - 1
        if 0 <= index < len(products):
            return ScreenResolution(products[index][0], "ordinal")
        return ScreenResolution(None, "ordinal_out_of_range")

    # (3) 사용자가 **상품 id 를 직접 말했는데 어느 목록에도 없는** 경우. 가드가 그 id 를 막고
    #     끝나면 좋겠지만, 실측에서는 LLM 이 대신 **화면의 다른 상품을 확정**했다(6/8). 사용자가
    #     말한 것을 못 들어줄 때 조용히 다른 물건을 담지 않는다.
    for token in _BARE_NUMBER.findall(message):
        if int(token) not in allowed_product_ids:
            return ScreenResolution(None, "unknown_product_id_spoken")

    # (4) 맨 지시대명사 — "이거 담아줘". 이름·순번·좌표 어느 것도 없을 때만 여기 온다.
    #     정본: **후보가 1건일 때만 확정, 여러 건이면 되물음.**
    if any(marker in message for marker in deictic_markers) and not _mentions_a_product_name(
        message, names
    ):
        if len(products) == 1:
            return ScreenResolution(products[0][0], "sole_screen_candidate")
        return ScreenResolution(None, "ambiguous_screen_candidates")

    return None
