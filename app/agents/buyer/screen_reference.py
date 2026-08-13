"""화면 지시어를 **코드에서** 해소한다 (이슈 #118, api-spec §3.1 "지시어 해소").

정본이 요구하는 것은 네 가지다.

    | 발화 | 필요한 것 |
    |---|---|
    | "이거 담아줘" | 후보가 **1건일 때만 확정**, 여러 건이면 **되물음** |
    | "3번째 거 담아줘" | 배열 순서 |
    | "3번째 줄 2번째 담아줘" | `columns` — `index = (row-1) × columns + (col-1)` |
    | "무선 이어폰 담아줘" | `name` 매칭 |

이 중 **순번·좌표·"후보 1건" 규칙은 결정적(deterministic)** 이라 LLM 에게 맡길 이유가 없다.
실제로 맡겨 봤더니 나빴다 — 실 LLM N=8 프로브(#118, 채택안 `SCREEN` 블록 기준. 지금은
`evals/intent_probe` group="screen" 이 #300 으로 흡수했다 —
`evals/intent_probe/baselines/fast-2026-08-05-300-screen/`)에서:

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

**발동 조건이 좁다는 것이 이 모듈의 안전 논거다** — `screen.products` 또는 서버가 이미 아는
이번 턴 추천 카드가 있는 경우에만 돈다. #234/#239/#240 이 세운 회귀 대조군은 둘 다 없는
요청이므로 **구조적으로 영향받지 않는다**(테스트로 고정). 옵션 되물음(`PENDING_CART`) 중에도
돌지 않는다 — 그 턴의 "2번"은 화면 순번이 아니라 옵션 번호이기 때문이다(호출부 `graph.py`가
그 조건을 건다).

**[#435] 추천 카드(CH-5) 턴에는 이 해소기가 붙지 않았다 — #571 이 그 제약을 좁혔다.** FE 는 그
카드를 `screen` 에서 **의도적으로 제외**한다(서버가 `listId` 로 이미 아는 목록을 되돌려주면
위조 경로가 된다, api-spec §3.1 지시어 해소 표) — 그 사실 자체는 변하지 않는다. 하지만 서버는
`last_reco`(§4.2 push 순서)로 이번 턴 카드 목록을 **노출 순서대로** 이미 쥐고 있어, `screen`
이 없어도 결정적으로 풀리는 입력(순번·"이거"·이름 전체 지목)까지 LLM 에 맡길 이유가 없었다
(#571, 아래 F-17~F-19). 그래서 이 해소기는 이제 `screen.products` **또는** 이번 턴 추천 카드
(`last_reco[:turn_count]`)가 있는 턴에 돈다 — `resolve_screen_reference` 의 `products` 인자가
호출부에서 어느 표면이든(화면·추천) 그 표면의 배열을 받는다. 추천 표면의 상품 ID는 계속 FE가
보내지 않는다. 다만 FE가 추천 패널의 반응형 열 수를
`screen={pageType: "chat", columns: N}`으로 보낸 경우, 호출부가 그 열 수와 서버의 추천 배열을
결합한다. 이때도 `ordinal_span == turn_count`로 노출 순서가 증명돼야 좌표를 확정하며, 증명되지
않으면 되묻는다. 운영 로그에서 `screen_reference_resolved`의 `extra.surface`가 `"reco"`로 남는
사례가 정상 동작이다(§4.2 관측 확장).

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
    | F-8 | `"무선 이어폰 2번째 옵션으로 담아줘"`(이름이 `screen.products` 가 아니라 `last_reco` 에만 있음) | 이름 출처를 화면으로만 좁혀 (B) 미발동 → 순번이 이겨 **오담기** | (B) 이름 검사에 `last_recommendation_products` 도 포함 |
    | F-9 | `"3줄 2단 정리함 담아줘"`·`"2줄 3인용 소파 담아줘"` | 두 번째 숫자 접미사가 선택이라 좌표로 오인 → **오담기** | `_COORD` 두 번째 숫자 접미사를 필수로 |
    | F-11 | `"3번째 줄에 있는 거 담아줘"`(columns=3) | `_COORD` 실패 뒤 순번이 "3"을 배열 순번으로 잡음 → **오담기** | `_ROW_ONLY` — 순번보다 먼저 되물음 |
    | F-12 | `"얘기했던 걸로 담아줘"`(화면 1건) | `deictic_markers` 의 `"얘"`(1글자)가 "얘기"에 부분일치 → 되물음 없이 확정 | config `screen_deictic_markers` 에서 `"얘"` 제거 |
    | F-14 | `"3번째 줄 5000원 넘는거 담아줘"`·`"3번째 줄에 5개 담아줘"`(columns=3) | `_ROW_ONLY` 가 "뒤에 숫자가 있으면" 무조건 무효화돼 순번이 "3"을 잡음 → **오담기**(F-11 재발) | `_ROW_ONLY` — 첫 숫자 `번째` 필수 + "뒤에 좌표 접미사 동반 숫자"만 배제 |
    | F-16 | `"302번 담아줘"`(두 목록 밖 id, `번` 접미) | `_BARE_NUMBER` 가 `번` 접미를 못 봐서 공집합 → 가드 미발동, LLM 오추출이 그대로 담김(F-3 재발) | 담기 동사 화이트리스트 앞에 `번(?!째)` 선택 허용 |
    | F-17 | `"이거 담아줘"`(추천 카드 다건, `screen` 없음) | 게이트가 `screen.products` 를 요구해 추천 카드 턴에 다건 되물음이 통째로 LLM 에 맡겨져 실측 8/8 오확정 | 게이트를 추천 표면(이번 턴 카드)으로 확대 |
    | F-18 | `"3번째 거 담아줘"`(다목록 턴, 또는 승계분까지 포함해 세면 순번이 밀림) | 추천 표면의 순번을 누적 `last_reco` 전체나 dedup 된 `ranked_ids` 로 세면 승계분·BUY_ALL 중복 붕괴로 화면과 다른 카드가 확정 | `turn_count` 경계로 배열을 자르고, `ordinal_span`(표시 순서=저장 순서 증명)이 없으면 순번을 되물음으로 강제 |
    | F-19 | `"무선 블루투스 이어폰 담아줘"`(카드 이름이 발화에 통째로 있음, 추천 카드 표면) | 추천 표면에서 (B) 를 그대로 두면 이름 지목이 영원히 LLM 산출에만 맡겨짐 | 배열=이름 출처인 표면에서만 좁은 이름 확정 규칙(N) 신설 — 부정·다건·빈 이름이면 종전대로 (B) 양보 |
    | F-20 | `"3번째 거 담아줘"`(이전 턴 추천 카드 3건, 이번 턴은 빈 검색 화면) | `graph.py`의 표면 계산이 "screen 자체가 없음"과 "추천 패널이 아닌 빈 screen"을 똑같이 처리해, 사용자가 지금 보지 않는 이전 추천 카드가 확정 → **오담기** | 추천 패널의 양성 신호인 `pageType=chat`+`columns`만 추천 표면과 결합하고, 그 밖의 빈 screen은 `surface=[]`로 유지 |
    | F-21 | `"Septwolves 지갑 담아줘"`(추천 카드 이름 일부의 유일 토큰) | 전체 상품명만 코드가 확정해, LLM이 같은 허용 목록 안의 다른 상품을 골라도 멤버십 가드가 통과 → **오담기** | NFKC·casefold 정확 토큰이 표면에서 유일하고 한 상품만 가리킬 때만 확정 — 공통·숫자·명령·부분 문자열·다중 상품·부정은 양보 |
    | F-22 | `"2번째 줄 3번째 상품 담아줘"`(추천 카드 3열) | FE는 추천 id를 빼고 `columns=3`만 보내는데 빈 `screen.products` 때문에 해소기를 호출하지 않아 LLM이 전체 3번째를 확정 → **오담기** | 순서가 증명된 추천 표면과 chat screen의 `columns`를 결합해 전체 6번째로 결정적 해소 |

    **전제와 잔여 위험(F-17~F-19 공통)**: 위 세 규칙은 "`last_reco` 순서 = I-21 push 순서 =
    사용자가 본 순서"를 전제한다. 그 전제의 근거는 두 갈래다 — (1) 정본 §4.2 규약이 명시하는
    "`listIds` 의 순서·개수는 `lists` 와 같다"(api-spec §4.2), (2) `recommendation/graph.py`
    의 PICK_ONE 경로가 `lists`(_entry(leg, group))와 `ranked_ids`(같은 leg·group 순회)를
    **같은 `exposed_groups` 에서** 파생한다는 사실 — 이건 정본 인용이 아니라 코드 확인
    항목이다. "FE 가 그 순서로 렌더한다"는 부분은 §4.2 가 직접 약속하지 않는다 — CH-5(§4.3)가
    `listId` 로 그 목록을 조회해 push 순서 그대로 렌더한다는 것은 이 모듈이 아니라 FE·CH-5
    계약의 영역이라, 여기서는 "코드가 그렇게 짜여 있다"는 확인 이상을 주장하지 않는다.
    **잔여 위험**: CH-5 조회 시점에 품절·HIDDEN 항목이 드롭될 수 있어(api-spec §4.2
    `itemsDropped`) 사용자가 화면에서 실제로 보는 개수가 push 시점보다 줄어들 수 있다. I-1
    검색이 이미 품절·비활성 상품을 후보에서 제거하므로(`app/services/search_service.py` 의
    "Spring이 가격·카테고리·브랜드를 적용하고 품절·비활성을 제거한다" 계약 문장 참조) 이 위험은
    push~조회 사이에 새로 품절/숨김 처리된 좁은 경우에 한정된다.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.agents.buyer.cart.negation import has_any_negation

if TYPE_CHECKING:
    from app.agents.buyer.recommendation.state import ScreenReference


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


def _has_explicit_row_first_grid_marker(message: str) -> bool:
    """구조화 좌표를 소비할 최소 원문 증거: 행 축이 있고 `열`보다 먼저 나온다."""
    row_marker = _STRUCTURED_ROW_MARKER.search(message)
    if row_marker is None:
        return False
    column_marker = _STRUCTURED_COLUMN_MARKER.search(message)
    return column_marker is None or row_marker.start() < column_marker.start()


# 한국어 순번·좌표 표기. **튜닝 노브가 아니라 문법**이라 config 가 아니라 여기 둔다
# (app/core/text.py 의 제어문자 정규식과 같은 성격).
#   좌표: 원문 정규식은 "3번째 줄 2번째" · "3줄 2칸" 같은 숫자 표기만 다룬다.
#   "세 번째 줄 두 번째" 같은 한글 수사는 decompose의 구조화 JSON으로 받고 여기서 ID를 계산한다.
#
# **첫 숫자는 행(row)이므로 표지는 `줄`·`행` 뿐이다.** 초판은 `열` 까지 같은 자리에 넣었는데
# 한국어에서 `열` 은 column 이라 축이 반대다 — `"2번째 열 3번째"`(col=2·row=3, index 7)를
# row=2·col=3(index 5)으로 읽어 **다른 상품을 확정했다**(PR 2차 리뷰, 재현 확인). 그 id 는
# 화면 목록 안이라 담기 가드가 막지 못한다(오담기).
#
# [7차 리뷰, F-9] **두 번째 숫자 뒤 접미사는 필수다.** 초판은 그 접미사(`번째|번|칸`)를
# 선택으로 뒀는데, 그러면 "숫자 + 줄/행 + 숫자"만 있어도 좌표로 확정된다 —
# `"3줄 2단 정리함 담아줘"` → `("3","2")` → 화면 8번째 상품 확정, `"2줄 3인용 소파 담아줘"` →
# `("2","3")` 도 마찬가지다(둘 다 실제 재현, 좌표를 말한 적 없는 발화가 화면 목록 **안**의
# 엉뚱한 상품으로 확정되는 오담기 — F-1/F-2/F-7 과 같은 클래스). 첫 숫자 뒤 `번째` 는 계속
# 선택으로 둔다 — `"3줄 2칸"` 처럼 첫 숫자에는 접미사가 없는 표기를 살려야 한다.
_COORD = re.compile(r"(\d+)\s*(?:번째\s*)?(?:줄|행)\s*(?:에\s*)?(\d+)\s*(?:번째|번|칸)")
# 열(column) 기준 좌표 표기. **해소하지 않고 LLM 에 넘기기 위해** 따로 잡는다 — 아래 양보 (C).
_COLUMN_FIRST = re.compile(r"\d+\s*(?:번째\s*)?열")
# LLM이 낸 `screenReference` 자체는 사용자 원문의 증거가 아니다. 값을 소비하기 전에 최소한
# 사용자가 **한글 순번 + 행 축**을 말했다는 사실만 확인한다. `줄`만 찾으면 기존 안전 대조군인
# `3줄 2단 정리함`까지 좌표로 오인하므로, 한글 순번 표기만 좁게 잡는다. 숫자값 해석은 여전히
# decompose가 맡고 이 정규식은 증거 유무·축 순서만 확인한다.
_KOREAN_GRID_ORDINAL = (
    r"(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉)?|스무)"
)
_STRUCTURED_ROW_MARKER = re.compile(rf"{_KOREAN_GRID_ORDINAL}\s*번째\s*(?:줄|행)")
_STRUCTURED_COLUMN_MARKER = re.compile(rf"{_KOREAN_GRID_ORDINAL}\s*번째\s*열")
# [8차 리뷰, F-11] **줄|행만 말하고 칸을 안 말한 경우** — `"3번째 줄에 있는 거 담아줘"`. `_COORD`
# 는 두 번째 숫자가 없으면 실패하고, 그러면 바로 아래 (2) 순번이 "3"을 **배열 순번**으로 잡아
# columns=3 일 때 실제 3번째 줄(index 6~8)과 무관한 상품(배열 3번째, index 2)을 확정한다(실제
# 재현). 사용자가 행을 말했는데 순번으로 해석하는 것은 F-1/F-2/F-7 이 막은 것과 같은 클래스의
# 오담기라, `_COORD` 실패 뒤에도 순번보다 먼저 걸러 되물음(§3.1 "좌표 지시만 불가"와 같은 취급,
# `coordinate_without_columns`)으로 보낸다.
#
# **F-9(두 번째 숫자는 있지만 접미사가 없는 경우, `"3줄 2단 정리함"`)와는 구분해야 한다** —
# 그쪽은 "2단"이 좌표가 아니라 상품 설명이라 LLM 산출을 존중해야(`None`) 한다.
#
# [9차 리뷰, F-14] **F-11 초판은 "뒤에 숫자가 아예 없을 때만" 매칭했는데, 그 조건이 F-9 와
# 겹치는 발화만 막고 정작 F-11 이 막으려던 발화의 절반을 못 막았다.** `"3번째 줄 5000원
# 넘는거 담아줘"`·`"3번째 줄에 5개 담아줘"` 는 줄 뒤에 (좌표가 아닌) 숫자가 있어 초판 조건이
# "뒤에 숫자 있음"으로 판정해 매칭을 포기했고, 그러면 `_COORD` 도 실패(접미사 없음)·`_ROW_ONLY`
# 도 실패해 (2) 순번이 "3"을 배열 순번으로 잡는 **F-11 과 완전히 같은 오담기**가 재발했다(실제
# 재현). 이 리뷰가 제안한 "뒤에 **유효한 좌표 접미사를 동반한** 숫자만 없으면 매칭"
# (`(?!\s*(?:에\s*)?\d+\s*(?:번째|번|칸))`)으로 단순 교체하면 **F-9 를 되살린다** — "2단"·"3인용"도
# "숫자 뒤에 좌표 접미사가 없다"는 조건을 그대로 만족해 매칭되므로 F-9 케이스도 다시 되물음으로
# 새 버렸다(직접 검증해 확인). 그래서 **두 조건을 함께 요구한다**:
#   ① 첫 숫자에 **`번째`가 붙어 있을 것** — "3번째"는 순번(ordinal)을 명시적으로 지목하는
#      표지라 뒤에 무엇이 오든 "행 위치를 말하려 했다"는 신호가 강하다. F-9 의 "3줄"·"2줄"은
#      `번째` 가 없는 **맨 수량 표기**(3-row 정리함처럼 상품 자체의 속성일 수 있다)라 이 신호가
#      없다 — `_ORDINAL` 자체도 `번째` 를 요구하는 것과 같은 근거다.
#   ② 뒤에 **유효한 좌표 접미사를 동반한 숫자만 없으면**(위 리뷰 제안 그대로) 매칭 — "5000원"·
#      "5개"처럼 접미사가 좌표와 무관한 숫자는 매칭을 막지 않는다.
# 어느 한쪽만으로는 부족하다 — ①만 두면(뒤에 숫자가 있든 없든 무조건) F-9 가 `번째` 없는
# 케이스라 우연히 안 깨지지만 가상의 `"3번째 줄 2단 정리함"` 류를 못 막고, ②만 두면(원 리뷰
# 제안) 위에서 보였듯 F-9 가 재발한다.
_ROW_ONLY = re.compile(r"\d+\s*번째\s*(?:줄|행)(?!\s*(?:에\s*)?\d+\s*(?:번째|번|칸))")
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
#
# [14차 리뷰, F-16] **`번` 접미가 통째로 빠져 있었다.** 한국어에서 상품 번호를 말할 때
# `"301번 담아줘"` 처럼 `번` 을 붙이는 표기가 `"301 담아줘"` 못지않게 흔한데, 숫자 바로 뒤에
# 담기 동사·문장 끝만 오는 경우로 화이트리스트를 좁혀 두는 바람에 `번` 이 끼면 매칭이 통째로
# 빠졌다(실제 재현: `"301번 담아줘"` → `_BARE_NUMBER` 공집합 → `unknown_product_id_spoken`
# 미발동 → LLM 이 오추출한 화면 안 다른 상품이 그대로 담긴다, F-3 과 같은 클래스의 오담기).
# `"번째"`(순서수사)와는 구분해야 한다 — `_ORDINAL` 이 이 규칙보다 먼저 검사돼 `"3번째"` 류는
# 애초에 여기까지 오지 않지만, 이 정규식 자체가 독립적으로도 `번째` 를 삼키지 않도록 `번` 뒤에
# `째` 가 오지 않을 때만 선택적으로 허용한다(이중 방어 — 호출 순서가 바뀌어도 안전).
#
# **`\d{2,}` 라 두 자리부터 대상이라 `"10번 담아줘"` 처럼 순번(10번째)인지 id 인지 진짜 애매한
# 입력이 생긴다.** 그래도 안전 방향이다 — 아래 (3) 은 토큰이 `allowed_product_ids` **밖**일
# 때만 되물음(`unknown_product_id_spoken`)을 반환하고, 안에 있으면 아무 것도 하지 않고 다음
# 규칙(맨 지시대명사 등)으로 넘어간다. 즉 이 규칙은 **스스로 무언가를 확정하지 않는다** —
# `"10번"` 을 id 로 읽든 순번으로 읽든 결과는 같다: 허용 목록 밖이면 되묻고, 안이면 침묵한다.
# 해석이 갈려도 오담기로 이어질 경로 자체가 없다.
_CART_VERBS = r"(?:담아|담기|담을|넣어|넣기|넣을|추가)"
_BARE_NUMBER = re.compile(
    rf"(?<![0-9A-Za-z가-힣])(\d{{2,}})(?:번(?!째))?(?=\s*(?:{_CART_VERBS}|$))"
)
# [#639] 상품명 일부 지목은 부분 문자열이 아니라 정확한 유니코드 영숫자 토큰으로만 비교한다.
_NAME_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
# 상품명에 지시문처럼 보이는 단어가 있어도 담기 동사 자체가 이름 선택 근거가 되면 안 된다.
# 새 동사 사전을 만들지 않고 위 `_CART_VERBS`가 소유한 어간과 같은 닫힌 어휘를 토큰에 적용한다.
_CART_ACTION_TOKEN_PREFIXES = ("담아", "담기", "담을", "넣어", "넣기", "넣을", "추가")
_CART_CONTEXT_TOKENS = frozenset({"장바구니", "장바구니에", "장바구니로"})


def mentions_screen_reference(message: str, settings) -> bool:
    """발화가 화면을 가리키려 **시도**했는지 — 그 시도가 실제로 확정 가능한지와는 무관하다
    (이슈 #440, 라운드 8 리뷰 F21 신설(`mentions_screen_position`) → 라운드 11 리뷰 F29 로
    확장·개명).

    `resolve_screen_reference` 가 이미 쓰는 네 정규식(`_COORD`·`_COLUMN_FIRST`·`_ROW_ONLY`·
    `_ORDINAL`) **또는** `settings.screen_deictic_markers`(`"이거"`류 지시대명사, 그 해소기가
    화면 참조로 처리하는 것과 같은 목록 — 새 마커 목록을 만들지 않는다) 중 하나라도 걸리면
    발화가 화면을 가리키려 한 것이다(확정에 성공했는지, `columns` 가 없어 거부됐는지, 범위를
    벗어났는지는 이 함수가 볼 일이 아니다 — 그건 `resolve_screen_reference` 의 반환값이 답한다).

    **[라운드 11 리뷰 F29] 왜 숫자 위치만으로는 부족한가** — 옛 `mentions_screen_position`
    은 좌표·순번 정규식만 봐서 `"이거 찜에서 빼줘"`(지시대명사, 화면을 명시적으로 가리킴)를
    "위치 미언급"으로 오독했다. 화면 1건이 501 로 확정됐고 그게 찜 목록에 없어도, 위치
    미언급으로 오독되면 규칙 3(목록 1건 자동)이 그 확정과 무관한 다른 항목을 지웠다(재현,
    파괴적). `resolve_screen_reference` 자체가 지시대명사를 화면 참조로 처리하는데
    (`deictic_markers` 인자), 이 헬퍼가 그 사실을 몰랐던 게 원인이다.

    **왜 필요한가**: `buyer/graph.py` 의 `screen_refused`(#440 라운드 4 리뷰 F12, 라운드 6
    리뷰 F18)는 원래 "화면이 있고(pending 이거나 해소기가 거부했다)"라는 **대리값**을 썼는데,
    발화가 화면을 **가리키지도 않은** 턴까지 거부로 오인해 이 이슈의 핵심 양성("찜한 거
    빼줘")을 되물음으로 퇴화시켰다(라운드 8 리뷰 F21 재현). 필요한 것은 "시도했는가"와 "그
    시도가 확정됐는가"를 각각 직접 보는 것이지, 화면 존재·pending 여부라는 대리 신호가 아니다.
    """
    return bool(
        _COORD.search(message)
        or _COLUMN_FIRST.search(message)
        or _ROW_ONLY.search(message)
        or _STRUCTURED_ROW_MARKER.search(message)
        or _ORDINAL.search(message)
        or any(marker and marker in message for marker in settings.screen_deictic_markers)
    )


@dataclass(frozen=True, slots=True)
class ScreenResolution:
    """코드가 확정한 담기 대상. `product_id=None` 은 **되물음 강제**다(임의 확정 금지)."""

    product_id: int | None
    reason: str


def _mentions_a_product_name(message: str, names: Iterable[str]) -> bool:
    """발화가 화면 상품 **이름**을 지목했는지. 이름 매칭은 LLM 이 8/8 로 잘한다 — 건드리지 않는다."""
    # 2자 미만 이름은 우연 일치가 잦아 신호로 쓰지 않는다.
    return any(len(name) >= 2 and name in message for name in names)


def _name_tokens(value: str) -> set[str]:
    """표기 차이만 접은 정확 이름 토큰. 숫자 전용·1글자는 자동 선택 근거에서 제외한다."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return {
        token
        for token in _NAME_TOKEN.findall(normalized)
        if len(token) >= 2 and any(character.isalpha() for character in token)
    }


def _message_name_tokens(message: str) -> set[str]:
    """발화의 상품명 후보 토큰 — 담기 명령 자체는 상품 라벨로 재해석하지 않는다."""
    return {
        token
        for token in _name_tokens(message)
        if token not in _CART_CONTEXT_TOKENS and not token.startswith(_CART_ACTION_TOKEN_PREFIXES)
    }


def _unique_product_name_token_match(
    message: str, products: Sequence[tuple[int, str]]
) -> int | None:
    """발화와 공유한 토큰이 추천 표면에서 유일한 **한 상품**을 가리킬 때만 그 ID를 돌려준다.

    같은 productId가 표면에 중복돼도 한 상품으로 센다. 공통 토큰만 있거나 서로 다른 상품의
    유일 토큰이 함께 언급되면 ``None``으로 양보해 임의 확정을 막는다.
    """
    tokens_by_product: dict[int, set[str]] = {}
    for product_id, name in products:
        tokens_by_product.setdefault(product_id, set()).update(_name_tokens(name))

    frequency = Counter(
        token for product_tokens in tokens_by_product.values() for token in product_tokens
    )
    unique_message_tokens = {
        token for token in _message_name_tokens(message) if frequency[token] == 1
    }
    matches = {
        product_id
        for product_id, product_tokens in tokens_by_product.items()
        if product_tokens & unique_message_tokens
    }
    return next(iter(matches)) if len(matches) == 1 else None


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
    # [6차 리뷰] 기본값을 두지 않는다 — `context_reference_markers` 와 같은 이유다. 빈 시퀀스가
    # 기본값이면 호출부가 이 인자를 빠뜨려도 아래 양보 (B) 가 **조용히** `screen.products` 이름만
    # 보고, 직전 추천에만 있는 이름을 지목한 발화에서 오담기가 재발한다(§ 아래 (B) 주석). 필수로
    # 두면 빠뜨린 호출부가 `TypeError` 로 즉시 드러난다.
    last_recommendation_products: Sequence[tuple[int, str]],
    # [#571] 아래 네 인자도 기본값을 두지 않는다 — 위 두 인자와 같은 이유(F-5)다. 이 넷을
    # 빠뜨리면 순번 규칙(2)이 증명 없이 켜지거나(오담기 재발) 이름 확정(N)이 화면 표면에서도
    # 조용히 켜져(F-8 재발) 이 모듈이 지킨 안전 경계가 소리 없이 무너진다.
    positional_order_verified: bool,
    name_confirmation_enabled: bool,
    negation_markers: Sequence[str],
    prefix_negation_markers: Sequence[str],
    structured_reference: ScreenReference | None = None,
) -> ScreenResolution | None:
    """발화의 화면 지시어를 해소한다. None = 해당 규칙 없음(LLM 산출을 그대로 둔다).

    `products` 는 **정제 후 남은 배열**이고 그 순서가 표시 순서라는 전제로 센다 — 호출부
    (`graph.py`)가 화면 표면(`screen.products`)과 추천 표면(`last_reco[:turn_count]`) 중 그
    턴에 있는 쪽을 넘긴다(#571, `decompose.build_screen_prompt` 주석의 화면 표면 전제와 같은
    개념을 추천 표면으로 넓혔다). `last_recommendation_products` 는 담기 허용 목록
    (`allowed_product_ids`)을 이루는 두 출처 중 하나인 **누적** 직전 추천 목록이다(`products` 가
    추천 표면일 때도 이번 턴 경계 이전 승계분까지 포함한 전체) — 호출부가 `allowed` 를 만들 때
    이미 손에 쥔 값을 그대로 넘긴다. 이 함수는 그 안의 상품을 화면 표면에 확정하지 않는다(순번·
    좌표는 어디까지나 `products` 기준) — 화면 표면에서는 아래 양보 (B) 의 이름 검사에만 쓰고,
    추천 표면에서는 (N) 이 `products` 자체를 이름 출처로 쓴다(`name_confirmation_enabled`).

    **개입은 규칙이 확실할 때만 한다.** 아래 양보(A)(B)와 순번 게이트(`positional_order_verified`)
    가 그 경계다 — 리뷰가 재현한 오담기 사례가 전부 "결정적이지 않은 입력까지 삼킨" 경우였다.
    애매하면 LLM 산출을 존중한다.
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

    # (N) [#571] 이름 확정 — **추천 카드 표면에서만**. 배열이 곧 이름 출처라 확정이 배열 밖으로
    #     나가지 않는다(화면 표면에서 이 규칙을 켜면 F-8 이 되살아난다 — 그쪽은 이름이
    #     last_recommendation_products 출신일 수 있고, 그 상품은 화면 배열에 없다).
    #     좁게 건다: 부정·대조 표지가 발화 전체에 없고, 먼저 비어 있지 않은 이름(2자 이상)이
    #     발화에 통째로 포함되는 카드가 **정확히 1건**일 때 확정한다. [#639] 전체 이름이 0건이면
    #     정확 토큰으로 한 번 더 좁히되, 표면에서 유일한 토큰들이 한 상품만 가리킬 때만 확정한다.
    #     전체 이름 다건·공통 토큰·다중 상품 토큰·부정 표지는 아무것도 하지 않고 아래 (B)로
    #     내려간다 — (B)는 이름이 있으면 순번·좌표·id 규칙을 막기만 할 뿐 스스로 확정하지
    #     않으므로, 여기서 확정하지 못한 이름 지목은 종전대로 LLM 산출(decompose 의
    #     LAST_RECOMMENDATIONS 해석)로 남는다.
    if name_confirmation_enabled and not has_any_negation(
        message, list(negation_markers), list(prefix_negation_markers)
    ):
        matches = {pid for pid, name in products if len(name) >= 2 and name in message}
        if len(matches) == 1:
            return ScreenResolution(next(iter(matches)), "screen_name_match")
        # [#639] 전체 이름이 하나도 매칭되지 않은 경우에만 유일 토큰으로 좁힌다. 전체 이름이
        # 2건 이상 걸린 기존 모호성은 그대로 LLM에 양보한다(#571-8 무회귀).
        if (
            not matches
            and (product_id := _unique_product_name_token_match(message, products)) is not None
        ):
            return ScreenResolution(product_id, "screen_unique_name_token_match")

    # (B) [양보] 발화가 화면 상품 **이름**을 지목했으면 순번·좌표·id 규칙을 적용하지 않는다.
    #     `"무선 이어폰 2번째 옵션으로 담아줘"` 에서 `"2번째"` 는 **옵션**을 수식하는데 화면
    #     순번으로 읽혀 엉뚱한 상품이 담겼다(리뷰 F-2, 실제 재현). 이름 매칭은 프로브에서
    #     LLM 이 8/8 로 가장 잘하는 신호이고 순번은 그보다 약한 신호다 — 강한 신호가 있으면
    #     약한 신호로 덮지 않는다. `"옵션"` 같은 수식 대상 단어를 특별 취급하는 방식은 표현이
    #     조금만 달라져도 뚫리므로 **이름 우선**이라는 일반 규칙으로 세웠다.
    #     이름이 없는 `"3번째 거 담아줘"` 에서만 순번이 발동한다.
    #
    #     [6차 리뷰] **이름 출처를 `screen.products` 로만 좁히면 구멍이 남는다.** 담기 허용 목록은
    #     `last_reco ∪ screen.products` 이고 프롬프트에도 두 블록이 다 실리는데, 이름이 **직전
    #     추천에만** 있으면 (예: 화면은 (501,"러그")·(502,"바구니"), 직전 추천은 (9001,"무선
    #     이어폰")) `"무선 이어폰 2번째 옵션으로 담아줘"` 가 이 검사를 통과해 아래 순번 규칙이
    #     화면 2번째 상품(바구니)으로 override 한다 — decompose 는 9001 을 옳게 뽑았을 텐데도
    #     같은 F-2 클래스 오담기가 이름의 출처만 바뀌어 재발한다(실제 재현). 그래서 화면 이름과
    #     직전 추천 이름을 **함께** 본다 — 어느 쪽 출처든 이름이 지목되면 이 함수는 개입하지 않고
    #     LLM 산출(decompose 가 고른 productId)을 그대로 세운다.
    if _mentions_a_product_name(
        message, [*names, *(name for _, name in last_recommendation_products)]
    ):
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

    # [8차 리뷰, F-11] 줄|행은 말했는데 칸이 없어 `_COORD` 가 실패한 경우 — 아래 (2) 순번에
    # 넘기지 않는다. 넘기면 "3번째 줄"의 "3"이 배열 순번으로 오인돼 실제 3번째 줄과 무관한
    # 상품이 확정된다(오담기). columns 없는 좌표와 같은 사유(`coordinate_without_columns`)로
    # 묶는다 — 사용자가 취해야 할 다음 행동이 "위치를 다시 말한다"로 같다(cart/graph.py
    # `_UNRESOLVED_SCREEN_POSITION` 문구가 그 사유를 그렇게 취급한다).
    if _ROW_ONLY.search(message):
        return ScreenResolution(None, "coordinate_without_columns")

    # (1-b) [#664] 한글 수사 좌표는 decompose가 상품 ID가 아니라 row·column만 구조화한다.
    #     원문 숫자 좌표와 행 단독 가드를 **뒤집지 않도록** 그 두 규칙 뒤에 둔다. 반대로 전체
    #     순번 규칙보다 앞에 둬 `두번째 줄 두번째`가 LLM의 cart.productId로 빠지지 않게 한다.
    #     kind=grid 주장은 축이 malformed여도 객체로 보존된다(decompose._parse_screen_reference) —
    #     이 자리에서 forced-null로 닫아 함께 온 허용 목록 내 오답 productId로 폴백하지 않는다.
    if structured_reference is None and _has_explicit_row_first_grid_marker(message):
        # 모델이 필드를 누락해도 명시적인 행 지시를 평범한 상품 순번으로 폴백시키지 않는다.
        return ScreenResolution(None, "coordinate_invalid")
    if structured_reference is not None:
        if not _has_explicit_row_first_grid_marker(message):
            return None
        row, col = structured_reference.row, structured_reference.column
        if row is None or col is None:
            return ScreenResolution(None, "coordinate_invalid")
        if not columns:
            return ScreenResolution(None, "coordinate_without_columns")
        index = resolve_grid_index(row, col, columns)
        if 0 <= index < len(products):
            return ScreenResolution(products[index][0], "coordinate")
        return ScreenResolution(None, "coordinate_out_of_range")

    # (2) 순번 — "3번째 거". 배열 순서만 있으면 풀린다.
    if ordinal := _ORDINAL.search(message):
        # [#571] 표시 순서 = 배열 순서가 증명되지 않으면(추천 표면의 다목록·BUY_ALL 턴) 인덱싱
        # 하지 않고 되물음을 강제한다 — §2 결정 2. 이 사유(`reco_order_unverifiable`)는
        # `cart/graph.py` 의 `_SCREEN_POSITION_REASONS` 에 넣지 않는다: 넣으면 "화면 위치를
        # 다시 말해 달라"는 문구가 나가는데, 사용자가 다시 몇 번째인지 말해도 증명 불가 상태는
        # 그대로라 같은 되물음이 반복된다. 넣지 않으면 `_unresolved_notice` 가 `has_last_reco`
        # 경로로 떨어져 기존 문구(`_UNRESOLVED_WITH_RECO`, "추천해 드린 상품 중에서 이름을
        # 말씀해 주시면…")를 낸다 — 사용자에게 다음 행동(이름으로 말하기)을 정확히 알려주고,
        # 새 문구를 0개 추가한다는 이 이슈의 제약(§0)도 지킨다.
        if not positional_order_verified:
            return ScreenResolution(None, "reco_order_unverifiable")
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
