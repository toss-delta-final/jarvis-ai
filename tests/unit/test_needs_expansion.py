"""목적·상황형 발화의 상품 전개 — 감지 (이슈 #198, DESIGN-NEEDS-EXPANSION-198 §4).

`decompose` 가 목적형 발화를 구체 상품으로 전개하지 못한 것을 **코드가 결정적으로 판정**한다.
LLM 에게 "전개했니?"를 묻지 않는다 — `case` 는 같은 호출의 산출물이라 전개 실패 회차의 값을
신뢰할 수 없음이 실측으로 확인됐다(§4.1).

감지는 **재현율보다 정밀도** 우선 — 오탐하면 "청바지" 같은 정상 질의에 불필요한 LLM 호출과
엉뚱한 확장이 붙는다.
"""

from __future__ import annotations

from app.agents.buyer.recommendation.needs_expansion import detect_expansion_need
from app.agents.buyer.recommendation.state import CategoryQuery

# config 기본값과 동일(§9) — 테스트가 config 변경에 끌려다니지 않게 명시적으로 준다.
# '답례품'·'추천' 은 실측에서 나온 목적 표현이다(`['돌잔치 답례품']`, `['집들이 선물 추천']`).
MARKERS = ["선물", "답례품", "준비물", "용품", "아이템", "키트", "물품", "추천", "것", "거"]


def _detect(utterance: str, queries: list[str | None]) -> str | None:
    legs = [CategoryQuery(None, q) for q in queries]
    return detect_expansion_need(utterance, legs, markers=MARKERS)


# ── D1 신호 없음 ────────────────────────────────────────────────────────────


def test_d1_no_legs_triggers() -> None:
    """leg 이 아예 없으면 전개한다 — 실측 `"부모님 환갑 선물"` 3회차가 `[]` 였다."""
    assert _detect("부모님 환갑 선물", []) == "no_legs"


# ── D2 발화 복사 ────────────────────────────────────────────────────────────


def test_d2_query_copied_from_utterance_triggers() -> None:
    """leg 이 1개이고 그 query 가 발화의 일부면 전개한다 — 전개가 아니라 복사다.

    실측: `"집들이 선물로 뭐 사갈까"` → `['집들이 선물']`(3/3), `"돌잔치 답례품 뭐가 좋을까"` →
    `['돌잔치 답례품']`(3/3). 이 값은 물건 이름이 아니라 목적 표현이라 매핑이 불가능하다.
    """
    assert _detect("집들이 선물로 뭐 사갈까", ["집들이 선물"]) == "utterance_copy"
    assert _detect("돌잔치 답례품 뭐가 좋을까", ["돌잔치 답례품"]) == "utterance_copy"


def test_d2_reverse_containment_also_triggers() -> None:
    """query 가 발화를 포함하는 경우(LLM 이 발화에 살을 붙인 경우)도 복사로 본다."""
    assert _detect("집들이 선물", ["집들이 선물 추천"]) == "utterance_copy"


def test_d2_only_when_single_leg() -> None:
    """leg 이 여럿이면 D2 를 적용하지 않는다 — 이미 전개가 일어난 것이므로 나머지를 버리지 않는다."""
    assert _detect("집들이 선물로 뭐 사갈까", ["집들이 선물", "디퓨저"]) is None


# ── D3 목적 표현으로 끝남 ────────────────────────────────────────────────────


def test_d3_purpose_marker_suffix_triggers() -> None:
    """query 가 목적 marker 로 **끝나면** 전개한다.

    실측 실패: `['환갑 선물 아이템']`·`['자취 시작 키트']`·`['캠핑 용품']` — 발화 복사는 아니지만
    여전히 "무엇을 살지"가 없는 목적 표현이다.
    """
    assert _detect("부모님 환갑 선물 추천해줘", ["환갑 선물 아이템"]) == "purpose_marker"
    assert _detect("자취 시작할 때 필요한거", ["자취 시작 키트"]) == "purpose_marker"
    assert _detect("캠핑 처음 가는데 뭐 사야해", ["캠핑 용품"]) == "purpose_marker"


def test_d2_wins_when_copy_and_marker_both_apply() -> None:
    """복사이면서 목적 표현이면 `utterance_copy` 로 라벨한다 — 사유는 leg 당 하나여야 한다.

    둘 다 참인 경우가 흔하다(`"자취 시작할 때"` → `['자취 시작할 때 필요한 것']`). 어느 쪽으로
    기록하든 동작은 같지만, 프롬프트 튜닝 시 "발화를 베꼈다"와 "목적 표현을 만들어냈다"는 다른
    처방으로 이어지므로 더 구체적인 D2 를 우선한다.
    """
    assert _detect("자취 시작할 때", ["자취 시작할 때 필요한 것"]) == "utterance_copy"


def test_copy_alone_is_not_enough_when_utterance_is_a_product() -> None:
    """발화 자체가 상품명이면 복사여도 트리거하지 않는다 — D2 에 marker 조건이 필요한 이유.

    `"청바지"` → `['청바지']` 는 완전한 복사지만 **올바른 leg** 이다. 복사 자체가 아니라 **목적
    표현을 복사한 것**이 문제이며, 이 구분이 없으면 가장 흔한 정상 질의가 전부 오탐된다.
    """
    assert _detect("청바지", ["청바지"]) is None
    assert _detect("무선 이어폰", ["무선 이어폰"]) is None
    assert (
        _detect("한우 선물세트 추천해줘", ["한우 선물세트"]) is None
    )  # '선물' 포함이나 endswith 아님


def test_d3_does_not_flag_legitimate_product_names() -> None:
    """정당한 상품명은 트리거하지 않는다 — `endswith` 를 쓰는 이유가 이것이다.

    `'집들이 선물'.endswith('선물')` 은 True 지만 `'한우 선물세트'.endswith('선물')` 은 **False** 다.
    marker 를 `in` 으로 검사하면 `한우 선물세트`·`과일 선물세트` 같은 실제 상품명이 전부 오탐된다.
    """
    for q in ("한우 선물세트", "청소도구 세트", "청바지", "무선 이어폰", "디퓨저", "방한 부츠"):
        assert _detect("발화", [q]) is None, q


def test_d3_requires_all_legs_to_be_purpose_expressions() -> None:
    """일부 leg 만 목적 표현이면 전개하지 않는다 — 이미 전개된 구체 상품을 버리지 않기 위해서다.

    전개는 legs 를 **교체**하므로(§6), 좋은 leg 이 섞여 있을 때 트리거하면 그것까지 날아간다.
    남은 목적 표현 leg 은 하류에서 거리컷·택일이 흡수한다(DESIGN-59 §4·§4.4).
    """
    assert _detect("부모님 환갑 선물", ["홍삼", "선물용품"]) is None
    assert _detect("부모님 환갑 선물", ["선물용품", "기념 아이템"]) == "purpose_marker"


# ── 미트리거(정상 경로) ──────────────────────────────────────────────────────


def test_normal_product_query_not_triggered() -> None:
    """단일 상품 질의는 전개하지 않는다 — 불필요한 LLM 호출·엉뚱한 확장 방지."""
    assert _detect("청바지", ["청바지"]) is None
    assert _detect("갓성비 무선이어폰", ["무선 이어폰"]) is None


def test_already_expanded_turn_not_triggered() -> None:
    """이미 구체 상품으로 전개된 턴은 손대지 않는다 — 실측 성공 회차 재현."""
    assert _detect("부모님 환갑 선물", ["홍삼", "안마의자", "한우 선물세트", "영양제"]) is None
    assert _detect("자취 시작할 때 필요한거", ["전자레인지", "이불", "냄비", "빨래바구니"]) is None


def test_blank_and_null_queries_are_ignored_as_signal() -> None:
    """query 가 없는 leg(raw 만 있는 leg)은 D2/D3 판정 대상이 아니다.

    신호 없는 leg 은 map_categories 가 어차피 스킵하므로(#22), 그것 때문에 전개를 트리거하면
    멀티턴 승계 등 무관한 경로까지 끌려온다.
    """
    assert _detect("발화", [None]) == "no_legs"  # 신호 있는 leg 이 0개 → D1
    assert _detect("발화", [None, "디퓨저"]) is None
