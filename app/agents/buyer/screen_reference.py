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

[라운드 3] 그런데 **"좁다"는 조건이 실제로는 충분히 좁지 않았다.** 읽기 전용 리뷰가 낸 3건을
전부 재현했고, 그중 둘은 **오담기**(사용자가 말하지 않은 상품이 담김)였다 — 원인은 하나다:
*발동 조건이 결정적이지 않은 입력까지 삼켰다.* 그래서 아래 두 **양보**를 앞단에 세웠다
(`resolve_screen_reference` 의 (A)(B)). 규칙이 확실할 때만 개입하고, 애매하면 LLM 산출을 존중한다.

    | 리뷰 | 재현 발화 | 수정 전 | 수정 후 |
    |---|---|---|---|
    | F-1 | `"아까 추천해준 그거 담아줘"` (화면 1건) | 화면 상품으로 확정 → **오담기** | 양보(A)·근칭 한정 |
    | F-2 | `"무선 이어폰 2번째 옵션으로 담아줘"` | 순번이 이겨 다른 상품 → **오담기** | 양보(B) |
    | F-3 | `"10만원대 무선 이어폰 담아줘"` | id 오인 → 정상 발화가 되물음 | `_BARE_NUMBER` 구조화 |
    | F-6 | `"3000 원짜리 담아줘"` (단위 띄어쓰기) | 같은 오인이 공백 케이스로 재발 | 담기 동사 화이트리스트 |
    | F-7 | `"2번째 열 3번째 담아줘"` | `열`(column)을 행으로 읽어 축 반전 → **오담기** | 양보(C) — 해소 안 함 |
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
#
# **첫 숫자는 행(row)이므로 표지는 `줄`·`행` 뿐이다.** 초판은 `열` 까지 같은 자리에 넣었는데
# 한국어에서 `열` 은 column 이라 축이 반대다 — `"2번째 열 3번째"`(col=2·row=3, index 7)를
# row=2·col=3(index 5)으로 읽어 **다른 상품을 확정했다**(PR 2차 리뷰, 재현 확인). 그 id 는
# 화면 목록 안이라 담기 가드가 막지 못한다(오담기).
_COORD = re.compile(r"(\d+)\s*(?:번째\s*)?(?:줄|행)\s*(?:에\s*)?(\d+)\s*(?:번째|번|칸)?")
# 열(column) 기준 좌표 표기. **해소하지 않고 LLM 에 넘기기 위해** 따로 잡는다 — 아래 양보 (C).
_COLUMN_FIRST = re.compile(r"\d+\s*(?:번째\s*)?열")
_ORDINAL = re.compile(r"(\d+)\s*번째")
# 사용자가 **상품 id 를 직접 말한** 경우의 맨 정수 토큰. 2자리 이상만 본다(한 자리 숫자는
# "2번"처럼 순번·옵션 표기일 확률이 높다).
#
# **뒤에 무엇이 오는지로 가른다 — 담기 동사(공백 허용) 또는 문장 끝일 때만 id 후보다.**
#   `"301 담아줘"` ✓ · `"301"` ✓ · `"3000 원짜리 담아줘"` ✗(뒤가 `원짜리`) · `"12 개월 할부로"` ✗
#
# 초판(접미 열거)은 `만`·`년`·`GB` 가 새어 `"10만원대 …"` 를 되물음으로 막았고(F-3), 그것을
# 고친 2판("앞뒤에 문자가 붙지 않은 토큰")은 **단위가 띄어쓰였을 때** 다시 샜다 —
# `"3000 원짜리"`·`"10 만원대"`·`"128 GB 모델"`·`"12 개월 할부로"` 가 전부 매칭됐다(PR 리뷰 지적,
# 재현 확인). 그 리뷰는 뒤 lookahead 를 `(?!\s*[0-9A-Za-z가-힣])` 로 넓히라고 했지만 **그러면
# 규칙이 죽는다**: `"301 담아줘"` 도 숫자 뒤가 공백+한글이라 함께 배제돼, 이 규칙이 존재하는
# 이유인 그 발화가 통째로 빠진다(프로브에서 screen 주입 후 1/8·6/8 로 깎였던 셀을 이 규칙이
# 8/8 로 되돌렸다). 그래서 **배제 목록을 넓히는 대신 허용 목록으로 뒤집었다.**
#
# 화이트리스트는 **놓치는 쪽으로 기운다**(`"301 상품 담아줘"` 는 못 잡는다). 그 비대칭이
# 안전 방향과 맞다 — 오탐하면 정상 가격·수량 발화가 되물음으로 막히지만, 놓치면 이 규칙이
# 개입하지 않아 LLM 산출이 그대로 남을 뿐이다(= 이 규칙 도입 전 동작).
# 담기 동사 어휘는 `_COORD`/`_ORDINAL` 과 같은 성격(튜닝 노브가 아니라 한국어 표기)이라 여기 둔다.
_CART_VERBS = r"(?:담아|담기|담을|넣어|넣기|넣을|추가)"
_BARE_NUMBER = re.compile(rf"(?<![0-9A-Za-z가-힣])(\d{{2,}})(?=\s*(?:{_CART_VERBS}|$))")


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
    # 기본값을 두지 않는다 — 빈 시퀀스를 기본값으로 두면 인자를 빠뜨렸을 때 아래 양보 (A) 가
    # **조용히 꺼져** 오담기 가드가 사라진다(리뷰 F-5, 실제로 인자를 빼고 호출해 재현했다).
    # 필수로 두면 빠뜨린 호출부가 `TypeError` 로 즉시 드러나고, 이미 필수인 `deictic_markers`
    # 와도 일관된다. "안전한 기본값"을 여기서 채우려면 이 모듈이 config 를 import 해야 하는데,
    # 그건 도메인 계층이 설정에 결합하는 것이라 택하지 않았다(호출부가 주입한다).
    context_reference_markers: Sequence[str],
) -> ScreenResolution | None:
    """발화의 화면 지시어를 해소한다. None = 해당 규칙 없음(LLM 산출을 그대로 둔다).

    `products` 는 **정제 후 남은 배열**이고 그 순서가 화면 순서라는 전제로 센다
    (`decompose.build_screen_prompt` 주석의 전제와 같다).

    **개입은 규칙이 확실할 때만 한다.** 아래 두 양보(A)(B)가 그 경계다 — 리뷰가 재현한 오담기
    2건이 전부 "결정적이지 않은 입력까지 삼킨" 사례였다. 애매하면 LLM 산출을 존중한다.
    """
    if not products:
        return None
    names = [name for _, name in products]

    # (A) [양보] 발화가 **대화 맥락**을 명시적으로 참조하면 화면 해소를 하지 않는다.
    #     `"아까 추천해준 그거 담아줘"` 는 직전 추천을 가리키는데, 화면 후보가 1건이면 (4) 가
    #     그 화면 상품으로 확정해 **사용자가 말하지 않은 상품이 담겼다**(리뷰 F-1, 실제 재현).
    #     이 저장소에서 `"아까"`·`"저번에"` 류는 직전 추천 맥락으로 확립돼 있다 —
    #     decompose `_SYSTEM` 의 하중 문구가 `"저번에 그거 다시 보여줘"` 를 그렇게 다루고
    #     #234 프로브가 그 경로를 측정했다. 그쪽은 LLM 이 LAST_RECOMMENDATIONS 로 푸는 것이 맞다.
    if any(marker in message for marker in context_reference_markers):
        return None

    # (B) [양보] 발화가 화면 상품 **이름**을 지목했으면 순번·좌표·id 규칙을 적용하지 않는다.
    #     `"무선 이어폰 2번째 옵션으로 담아줘"` 에서 `"2번째"` 는 **옵션**을 수식하는데 화면
    #     순번으로 읽혀 엉뚱한 상품이 담겼다(리뷰 F-2, 실제 재현). 이름 매칭은 프로브에서
    #     LLM 이 8/8 로 가장 잘하는 신호이고 순번은 그보다 약한 신호다 — 강한 신호가 있으면
    #     약한 신호로 덮지 않는다. `"옵션"` 같은 수식 대상 단어를 특별 취급하는 방식은 표현이
    #     조금만 달라져도 뚫리므로 **이름 우선**이라는 일반 규칙으로 세웠다.
    #     이름이 없는 `"3번째 거 담아줘"` 에서만 순번이 발동한다.
    if _mentions_a_product_name(message, names):
        return None

    # (C) [양보] **열(column) 기준 좌표는 해소하지 않는다.** 정본 §3.1 지시어 해소 표와 이 PR 이
    #     넣은 `_SCREEN_CART_RULE` 프롬프트가 가르치는 어휘는 `"3번째 줄 2번째"`(줄=row·칸=column)
    #     뿐이고 `열` 은 계약에도 프롬프트에도 없다 — 프로브가 잰 적도 없는 표기다. 결정적으로
    #     풀린다고 확인되지 않은 입력까지 삼키지 않는다(이 모듈이 리뷰 F-1·F-2 로 배운 것).
    #
    #     **`_COORD` 에서 `열` 을 빼는 것만으로는 부족하다** — 그러면 남은 `"2번째"` 를 아래
    #     (2) 순번이 가로채 또 다른 상품을 확정한다(제거만 적용해 재현: `"2번째 열 3번째"` →
    #     `ordinal` → 2번째 상품). 부분 해석이 오히려 위험하므로 **해소 자체를 건너뛰어**
    #     LLM 산출을 그대로 세운다(= 이 규칙 도입 전 동작, 되물음·가드가 그대로 받는다).
    if _COLUMN_FIRST.search(message):
        return None

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

    # (4) 맨 지시대명사 — "이거 담아줘". 이름·순번·좌표 어느 것도 없을 때만 여기 온다((B) 가
    #     이름을 이미 걸렀다). 정본: **후보가 1건일 때만 확정, 여러 건이면 되물음.**
    #     `deictic_markers` 는 config 기본값에서 **근칭(`이거`)만** 남겼다 — `"그거"` 는 이
    #     저장소에서 대화 지시어로 확립돼 있어 화면 지시로 보면 안 된다(리뷰 F-1, config 주석).
    if any(marker in message for marker in deictic_markers):
        if len(products) == 1:
            return ScreenResolution(products[0][0], "sole_screen_candidate")
        return ScreenResolution(None, "ambiguous_screen_candidates")

    return None
