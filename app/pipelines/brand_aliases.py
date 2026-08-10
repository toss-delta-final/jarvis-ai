"""브랜드 법인 접미사 표기 확장 — I-1 `brandName` 와이어 전용 (이슈 #466).

## 왜 필요한가

I-1 `brandName` 은 **exact IN OR 매칭**이다(api-spec §4.6 — "존재하지 않는 이름은 무시하고,
전부 미존재일 때만 0건"). 그런데 카탈로그는 같은 회사를 **법인 접미사가 붙은 표기**로도 들고
있다. 운영 시드 실측(`~/inte-final/_sql/mariadb/20_brand.sql` 2,368행 × `30_product.sql`
6,559건 조인):

| 사용자 발화 | 원문 표기 도달 | 같은 회사 총합 | 도달률 |
|---|---|---|---|
| "삼성" | `삼성` 7건 | + `삼성전자` 71건 = 78 | **9.0%** |
| "LG"  | `LG` 1건   | + `LG전자` 37건 = 38 | **2.6%** |

즉 #466 의 프롬프트 절이 브랜드를 **원문 그대로** 뽑아내도(그래야 한다 — 번안은 exact IN 을
빗나간다), "삼성 제품 아무거나"는 71건을 못 본 채 7건만 돌려준다. 그리고 브랜드는 자동 완화
허용 목록에 없어(`config._forbid_auto_relaxing_explicit_constraints` — 허용은 `ratingMin` 뿐)
이 부족분이 조용히 사용자에게 간다.

## 왜 사전(브랜드 목록)을 두지 않는가

`brandName` 이 **존재하지 않는 이름을 무시**하므로 확장은 **추측해도 공짜**다 — 카탈로그에
`삼성코리아` 가 없으면 BE 가 그냥 버린다. 그래서 카탈로그 브랜드 사본을 저장소나 AI Postgres
에 두지 않는다(CLAUDE.md "상품 원본 컬럼 사본 금지", api-spec C-28). 규칙만 두면 사본도
노후화도 없다. 색상(`app.pipelines.color_synonyms`)이 DB 사전을 쓰는 것과 갈리는 지점이다 —
색상은 BE 가 **부분 일치**라 동의어의 의미 판단이 필요하지만, 브랜드는 exact IN 이라
"표기 후보를 더 던진다"로 끝난다.

## 정밀도 — 접미사 화이트리스트는 **닫힌 집합**이다

임의 접미사를 붙이면 `삼성` 이 `삼성도어`(1건)·`삼성메디칼`(1건) 같은 **다른 업종**을 끌어온다.
그래서 법인격 접미사만 담은 닫힌 튜플을 쓴다. 이 규칙이 2,368행 전수에서 실제로 잇는 쌍은
**3건뿐**이고 전부 같은 회사다(교차 오염 0건):

    '삼성'(7) ↔ '삼성전자'(71) · 'LG'(1) ↔ 'LG전자'(37) · '한일'(1) ↔ '한일전자'(1)

⚠️ 세 쌍 중 **같은 회사임이 분명한 것은 삼성·LG 둘**이고, 측정된 이득도 사실상 이 둘이다
(+71·+37). `한일` 은 흔한 보통명사라 `한일`↔`한일전자` 가 한 회사라는 것은 **이름에서 온 추론**
이지 시드가 보증하는 사실이 아니다 — 다만 양쪽 다 1건이고 확장은 가산적이라 최악이어도
상품 1건이 더 붙을 뿐이라 **유해하지 않다고 보고 수용**한다. "교차 오염 0건"을 "세 쌍 모두
회사 검증됨"으로 읽지 말 것.

⚠️ 이 대조는 **특정 시점의 시드**에 대한 것이고 CI 가 다시 확인하지 않는다. BE 가 `한샘전자`
같은 무관한 행을 추가하면 조용히 오염이 시작된다. 재대조 쿼리는
`evals/filter_axes/README.md` 「브랜드 추출 축」절에 적어 뒀다 — 브랜드 쪽을 다시 만지는
사람은 그걸 먼저 돌릴 것.

`전자` 가 안전한 것은 대기업 전자 계열사가 모회사 이름을 그대로 쓰기 때문이지, 접미사 일반의
성질이 아니다(❌ `현대`+`백화점`·`건설` 은 서로 다른 회사다 — 그래서 목록에 없다).

**후보였다가 뺀 접미사**: `코리아`·`KOREA`·`그룹` 은 같은 전수 대조에서 잇는 쌍이 **0건**이었다.
이득이 0 인데 요청 파라미터만 늘어 뺐다 — 이 저장소의 "재서 못 넘으면 넣지 않는다" 규약
(`evals/README.md` 규약1)과 같은 취지다. 접미사를 넓히려면 **같은 전수 대조를 다시 돌려**
① 새로 잇는 쌍이 있고 ② 교차 오염이 0건임을 보이고 위 표를 갱신할 것.

카탈로그를 안 보므로 `나이키전자` 처럼 **존재하지 않는 후보도 실린다** — 의도된 비용이다
(§4.6 이 미존재 이름을 무시한다). 브랜드 1개당 파라미터는 2개로 유계이고, 전체 상한은
`settings.brand_alias_max_values` 다.

## 확장은 **가산적**이다

사용자가 말한 표기를 **항상 먼저, 항상 그대로** 싣고 그 뒤에 후보를 덧붙인다. 그래서 최악의
경우가 "오늘 동작 + BE 가 무시할 이름 몇 개"이고, 결과 집합이 줄어들 수 없다. `filters.brand`
자체는 건드리지 않는다 — 조건칩(`state.py`)은 사용자가 말한 표기를 그대로 보여주고,
`search_filter_axes` 의 축 집합도 바뀌지 않는다(`brandName` 키의 유무는 확장과 무관하다).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable, Sequence

_log = logging.getLogger(__name__)

# 법인격 접미사 — **닫힌 집합**(위 docstring 의 전수 대조 근거). 업종·라인 명사는 넣지 않는다.
# 지금 한 개뿐인 것은 축소가 아니라 **측정 결과**다 — 후보 4종 중 `전자` 만 실제로 쌍을 이었다.
CORPORATE_SUFFIXES: tuple[str, ...] = ("전자",)

# 한글 음차 ↔ 라틴 표기 쌍. **카탈로그에서 뽑은 목록이 아니라 음차 상식**이다(그래서 사본이
# 아니다). 접미사 규칙과 같은 근거로 **틀려도 공짜**라 실재 여부를 확인하지 않는다 — 카탈로그에
# 없으면 BE 가 무시한다(§4.6).
#
# 왜 필요한가: 프롬프트의 "원문 표기 그대로" 규칙은 옳지만(번안은 어느 표기가 실재하는지 모른 채
# 찍는 도박이다), **카탈로그가 라틴 표기 쪽에 상품을 몰아둔 브랜드**에서는 원문만으로 손해가 난다.
# 운영 시드 실측: `애플` 1건 / `Apple` **7건**. 즉 #466 이전에 LLM 이 "애플"→`["Apple"]` 로
# 번안하던 동작은 **이 한 토큰에 한해** 더 많은 상품에 닿고 있었다. 원문 표기를 유지하면서 그
# 몫을 잃지 않으려면 둘 다 실어야 한다(가산이라 어느 쪽도 잃지 않는다).
# 다른 브랜드는 반대 방향이다 — `나이키` 106 / `Nike` 1, `아디다스` 83 / `Adidas` 0,
# `크록스` 66 / `Crocs` 0. 그래서 "번안"이 아니라 **병기**가 맞는 처치다.
#
# 등재 기준: **라틴 상표명의 한글 음차**만(의미 번역이 아니라 소리). 전수 목록을 목표하지 않는다 —
# 빠진 브랜드는 종전 동작(원문만)이라 회귀가 아니다.
SCRIPT_PAIRS: tuple[tuple[str, str], ...] = (
    ("애플", "Apple"),
    ("나이키", "Nike"),
    ("아디다스", "Adidas"),
    ("크록스", "Crocs"),
    ("다이슨", "Dyson"),
    ("퓨마", "Puma"),
    ("리복", "Reebok"),
    ("뉴발란스", "New Balance"),
    ("컨버스", "Converse"),
    ("버버리", "Burberry"),
    ("구찌", "Gucci"),
    ("샤넬", "Chanel"),
    ("소니", "Sony"),
    ("필립스", "Philips"),
    ("브라운", "Braun"),
    ("샤오미", "Xiaomi"),
)


def _norm(value: str) -> str:
    """비교용 정규화 — NFC + strip + casefold (`color_synonyms._norm` 과 같은 관례)."""
    return unicodedata.normalize("NFC", value).strip().casefold()


def _script_counterparts(token: str) -> list[str]:
    """음차 쌍의 반대쪽 표기 — **양방향**이다("애플"→Apple, "Apple"→애플)."""
    key = _norm(token)
    out = []
    for korean, latin in SCRIPT_PAIRS:
        if key == _norm(korean):
            out.append(latin)
        elif key == _norm(latin):
            out.append(korean)
    return out


def is_admissible_alias(token: str, candidate: str) -> bool:
    """`candidate` 가 `token` 의 **표기 변형**으로 인정되는가 — 법인 접미사 또는 음차 쌍.

    접미사는 **양방향**이다: `token + 접미사`(삼성→삼성전자)와 `token - 접미사`(삼성전자→삼성)
    둘 다 인정한다. 한쪽만 하면 "삼성전자 제품 아무거나"라고 말한 사용자가 `삼성` 행 7건을
    못 보는 **거울상 결함**이 남는다 — 그리고 프롬프트의 "원문 표기 그대로" 규칙 때문에 그
    발화가 실제로 자주 들어온다.

    부분 문자열 포함으로 보지 않는다 — 포함으로 보면 `삼성` 이 `삼성도어`·`삼성메디칼` 을
    삼킨다(lessons 2026-08-02 「부분 문자열 매칭은 포함 방향마다 의미가 다르다」와 같은 함정).
    """
    base, cand = _norm(token), _norm(candidate)
    if not base or not cand or cand == base:
        return False
    for suffix in CORPORATE_SUFFIXES:
        norm_suffix = _norm(suffix)
        if cand == base + norm_suffix or base == cand + norm_suffix:
            return True
    return any(cand == _norm(other) for other in _script_counterparts(token))


def expand_brands(values: Iterable[str], *, cap: int) -> list[str]:
    """`filters.brand` → I-1 `brandName` 에 실을 표기 목록 (가산적·순서 보존).

    사용자가 말한 표기가 **먼저** 오고 그 뒤에 법인 접미사 후보가 붙는다. 공백-only 는
    버린다(`spring_client._search_query_params` 와 같은 blank=falsy 규약 — 확장을 켜도
    `brandName=` 빈값이 나가지 않게). 중복은 정규화 기준으로 접되 **표기는 원문을 남긴다**
    (BE 비교는 BE 소관이고, AI 가 대소문자를 재단하면 "같은 판정을 두 곳에 둔다"가 된다).

    `cap` 은 실을 표기 수의 상한이다(하드코딩 금지 — `settings.brand_alias_max_values`).
    상한에 걸리면 **사용자 원문 쪽이 남는다** — 원문이 먼저 채워지기 때문이다. `cap <= 0`
    이면 빈 목록을 돌려주고, 호출부는 확장 없이 원문으로 검색한다.
    """
    originals = [v for v in values if v and v.strip()]
    if not originals or cap <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        key = _norm(value)
        if key and key not in seen and len(out) < cap:
            seen.add(key)
            out.append(value)

    # 1단계: 사용자 원문 전량 — 확장이 원문을 밀어내지 않게 먼저 채운다.
    for value in originals:
        _add(value)
    # 2단계: 법인 접미사 후보(**양방향**) + 음차 쌍. 카탈로그 조회 없이 붙인다 — 미존재 이름은
    # BE 가 무시한다(§4.6). 접미사를 붙이기만 하면 "삼성전자"라고 말한 사용자가 `삼성` 행을
    # 못 보는 거울상 결함이 남는다.
    for value in originals:
        base = value.strip()
        for suffix in CORPORATE_SUFFIXES:
            _add(base + suffix)
            if _norm(base).endswith(_norm(suffix)) and len(base) > len(suffix):
                _add(base[: -len(suffix)])
        for counterpart in _script_counterparts(base):
            _add(counterpart)
    if len(out) == cap:
        # 상한에 걸려 후보가 잘렸을 수 있다 — 조용히 잘리면 "확장을 켰는데 왜 안 넓어지지"를
        # 추적할 방법이 없다. 값은 싣지 않는다(#119 규약).
        _log.info("brand_alias_expansion_capped", extra={"cap": cap, "inputs": len(originals)})
    return out


def brand_wire_values(values: Sequence[str] | None, *, enabled: bool, cap: int) -> list[str] | None:
    """와이어에 실을 값 — 확장이 꺼져 있거나 낼 것이 없으면 None(= 종전 경로 그대로).

    None 을 돌려주는 것과 빈 목록을 돌려주는 것은 다르다. `_search_query_params` 는 None 이면
    `filters.brand` 원문을 쓰므로, **확장 실패가 검색을 좁히거나 비우지 않는다.**
    """
    if not enabled or not values:
        return None
    expanded = expand_brands(values, cap=cap)
    return expanded or None
