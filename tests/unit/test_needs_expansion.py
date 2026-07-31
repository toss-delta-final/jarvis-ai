"""목적·상황형 발화의 상품 전개 — 감지 (이슈 #198, DESIGN-NEEDS-EXPANSION-198 §4).

`decompose` 가 목적형 발화를 구체 상품으로 전개하지 못한 것을 **코드가 결정적으로 판정**한다.
LLM 에게 "전개했니?"를 묻지 않는다 — `case` 는 같은 호출의 산출물이라 전개 실패 회차의 값을
신뢰할 수 없음이 실측으로 확인됐다(§4.1, 트리거로 부적합).

단 `case` 는 **게이트**로 쓴다(§4.2, PR #203 리뷰) — `case != 3` 이면 어떤 규칙도 발동하지 않는다.
case 2 는 의도적으로 카테고리 무관이라 좁히면 안 되는데(#22·#162) leg 유무로도 marker 로도 case 3 과
구분되지 않기 때문이다. 배제에만 쓰이므로 트리거보다 낮은 신뢰도로 충분하다.

감지는 **재현율보다 정밀도** 우선 — 오탐하면 "청바지" 같은 정상 질의에 불필요한 LLM 호출과
엉뚱한 확장이 붙는다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.buyer.recommendation.needs_expansion import (
    detect_expansion_need,
    expand_needs,
)
from app.agents.buyer.recommendation.state import CategoryQuery
from app.core.llm import LLMError

# config 기본값과 동일(§9) — 테스트가 config 변경에 끌려다니지 않게 명시적으로 준다.
# '답례품'·'추천' 은 실측에서 나온 목적 표현이다(`['돌잔치 답례품']`, `['집들이 선물 추천']`).
MARKERS = ["선물", "답례품", "준비물", "용품", "아이템", "키트", "물품", "추천", "것", "거"]


def _detect(utterance: str, queries: list[str | None], *, case: int = 3) -> str | None:
    """기본 case=3 — 대부분의 테스트는 목적형 발화를 다루므로 그 전제를 기본값으로 둔다."""
    legs = [CategoryQuery(None, q) for q in queries]
    return detect_expansion_need(utterance, legs, markers=MARKERS, case=case)


def _detect_legs(utterance: str, legs: list[CategoryQuery], *, case: int = 3) -> str | None:
    return detect_expansion_need(utterance, legs, markers=MARKERS, case=case)


# ── D1 게이트: case 2(구조화 조건만) 보호 (PR #203 리뷰) ──────────────────────


def test_case_two_leg_with_marker_suffix_is_still_gated() -> None:
    """[자체 점검] case 2 발화의 leg 이 marker 로 끝나도 전개하지 않는다 — 게이트는 D3 에도 걸린다.

    D1 만 게이트하면 이 경로로 새어나간다: `"평점 높은 거 보여줘"` → `['평점 높은 거']` 는 marker
    `'거'` 로 끝나 D3 가 발동한다. `"인기 많은 거 추천해줘"` 도 같다. 무필터 의도인데 지어낸
    카테고리로 좁혀지므로, 게이트를 **전 규칙**에 건다.

    실패 방향이 비대칭이라는 것이 근거다 — 과하게 막으면 목적 표현 leg 이 남아 하류 거리컷이
    드롭해 무필터로 흡수되지만(안전), 새면 검색이 엉뚱하게 좁혀진다(유해).
    """
    assert _detect("평점 높은 거 보여줘", ["평점 높은 거"], case=2) is None
    assert _detect("인기 많은 거 추천해줘", ["인기 많은 거"], case=2) is None
    # 같은 규칙이라도 case 3 이면 전개 대상이다(게이트만의 차이임을 고정)
    assert _detect("발이 시려워", ["방한 아이템"], case=3) == "purpose_marker"
    assert _detect("발이 시려워", ["방한 아이템"], case=2) is None


def test_d1_requires_case_three() -> None:
    """[PR #203 리뷰] D1 은 `case == 3` 일 때만 발동한다 — case 2 를 전개에서 보호한다.

    `"5만원 이하 아무거나"`(case 2, 구조화 조건만)와 `"부모님 환갑 선물"`(case 3, 전개 실패)은
    **둘 다 `categoryQueries` 가 빈다**. leg 유무만 보면 구분되지 않는데, 처방은 정반대다 —
    case 2 는 좁히면 안 되고(무필터 전체 검색, #22·#162) case 3 은 좁혀야 한다.

    `case` 를 **트리거**로 쓰는 것은 §4.1 에서 기각했지만(선언≠사실·오탐), **게이트**로 쓰는 것은
    실측이 뒷받침한다 — `"5만원 이하 아무거나"` 는 3/3 안정적으로 `case=2` 였다.
    """
    assert _detect("부모님 환갑 선물", [], case=3) == "no_legs"
    assert _detect("5만원 이하 아무거나", [], case=2) is None
    assert _detect("청바지", [], case=1) is None


def test_category_agnostic_query_never_expanded() -> None:
    """조건만 있는 무필터 조회는 전개하지 않는다 — 전개 LLM 이 카테고리를 지어내면 계약이 깨진다.

    전개 프롬프트는 "최소 2개를 채우세요"로 **항상 ≥2개를 만들도록** 강제하므로, 목적이 없는
    입력에도 그럴듯한 상품명을 지어낸다. 그것이 legs 를 교체하면 `filters.category` 가 채워져
    "카테고리 무관, 가격만 필터"라는 사용자 의도가 파괴된다(#22 가 테스트로 고정한 계약).
    이 경로는 #162(조건 없는 발화를 인기상품·프로필 후보로)가 개선할 자리이므로 보존해야 한다.
    """
    for utt in ("5만원 이하 아무거나", "인기 많은 거 추천해줘", "평점 높은 거 보여줘"):
        assert _detect(utt, [], case=2) is None, utt


# ── 신호 판정: raw_category 포함 (PR #203 리뷰) ───────────────────────────────


def test_raw_only_leg_counts_as_signal() -> None:
    """[PR #203 리뷰] `raw_category` 만 있는 leg 도 **신호**다 — 저장소 규약은 `raw_category or query`.

    `decompose._parse_category_queries`(`q.raw_category or q.query`)·graph 멀티턴 승계 판정
    (`not any(q.raw_category or q.query ...)`) 모두 두 필드를 함께 본다. 스키마상 `category` 만
    채우고 `query=null` 인 leg 이 나올 수 있는데, 그건 **이미 올바른 카테고리 신호**다. query 만
    보면 D1 이 오탐해 그 leg 을 지어낸 상품 목록으로 통째로 교체해버린다.
    """
    assert _detect_legs("무선 이어폰 추천해줘", [CategoryQuery("음향가전", None)]) is None


def test_d3_not_triggered_when_a_leg_has_only_raw() -> None:
    """query 없는 leg 이 섞이면 D3(모든 leg 이 목적 표현)는 성립하지 않는다.

    raw 만 있는 leg 은 목적 표현 판정 대상이 아니므로 `all()` 을 깨야 한다 — 그 leg 은 정상적으로
    분류된 카테고리이고, 전개로 교체하면 유실된다.
    """
    legs = [CategoryQuery(None, "선물용품"), CategoryQuery("음향가전", None)]
    assert _detect_legs("부모님 환갑 선물", legs) is None


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


# ── 전개 호출 (§5·§7) ────────────────────────────────────────────────────────


class _FakeLLM:
    """지정 raw 문자열을 돌려주거나 error=True 면 LLMError 를 던지는 최소 LLM."""

    def __init__(self, *, raw: str = "", error: bool = False) -> None:
        self._raw = raw
        self._error = error
        self.calls: list[tuple[str, str]] = []  # (system, user)

    async def complete(self, *, system: str, user: str, tier: str, max_tokens: int = 1024) -> str:
        self.calls.append((system, user))
        if self._error:
            raise LLMError("llm down")
        return self._raw


def _settings(*, fanout_max: int = 5, min_items: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        category_fanout_max=fanout_max,
        needs_expansion_min_items=min_items,
        needs_expansion_tier="fast",
        # resolve_model_id 용 — 관측 기록(§10)이 tier 를 모델 ID 로 바꿀 때 읽는다.
        llm_provider="openai",
        openai_fast_model_id="gpt-5-nano",
        openai_smart_model_id="gpt-5.6-luna",
    )


class _FakeObserver:
    """record_model_call 만 갖는 최소 관측자 — 기록된 모델 ID 를 모아둔다."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def record_model_call(
        self, model: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        self.models.append(model)


def _items(*names: str) -> str:
    return json.dumps({"items": list(names)}, ensure_ascii=False)


async def test_expand_returns_concrete_product_names() -> None:
    """전개는 구체 상품명 목록을 돌려준다 — 이것이 leg query 가 되어 매핑(#115)에 태워진다."""
    llm = _FakeLLM(raw=_items("디퓨저", "식기 세트", "핸드워시 세트"))
    out = await expand_needs("집들이 선물로 뭐 사갈까", llm=llm, settings=_settings())
    assert out == ["디퓨저", "식기 세트", "핸드워시 세트"]
    assert len(llm.calls) == 1
    assert "집들이 선물로 뭐 사갈까" in llm.calls[0][1]  # 발화가 user 프롬프트에 실린다


async def test_expand_truncates_to_fanout_max() -> None:
    """개수 상한은 `category_fanout_max` 를 재사용한다(§6) — 새 튜너블을 만들지 않는다.

    매핑·검색 단계가 이미 그 상한으로 절단하므로 별도 값을 두면 두 상한이 어긋난다.
    """
    llm = _FakeLLM(raw=_items("a", "b", "c", "d", "e", "f", "g"))
    out = await expand_needs("발화", llm=llm, settings=_settings(fanout_max=3))
    assert out == ["a", "b", "c"]


async def test_expand_rejects_when_below_min_items() -> None:
    """결과가 `min_items` 미만이면 전개 실패로 본다 — 1개면 발화 복사로 되돌아간다(§7)."""
    llm = _FakeLLM(raw=_items("디퓨저"))
    assert await expand_needs("발화", llm=llm, settings=_settings(min_items=2)) == []


async def test_expand_filters_blank_and_non_string_items() -> None:
    """공백·비문자열 항목은 거른다 — 미검증 LLM 값이 leg query 로 새면 앵커가 오염된다.

    (decompose `_parse_category_queries` 의 blank=falsy 규약과 동일.)
    """
    llm = _FakeLLM(raw=json.dumps({"items": ["디퓨저", "   ", 123, None, "식기 세트"]}))
    assert await expand_needs("발화", llm=llm, settings=_settings()) == ["디퓨저", "식기 세트"]


async def test_expand_returns_empty_on_llm_error() -> None:
    """LLM 오류·타임아웃은 빈 리스트 — 호출부가 `decompose` 원본을 그대로 쓴다(§7 후퇴 없음).

    전개는 **개선 시도**이며, 실패가 기존 경로를 악화시켜서는 안 된다. 최악의 경우가 "지금과 동일".
    """
    llm = _FakeLLM(error=True)
    assert await expand_needs("발화", llm=llm, settings=_settings()) == []


async def test_expand_returns_empty_on_malformed_json() -> None:
    """JSON 파싱 실패·형식 불일치도 빈 리스트(후퇴 없음)."""
    for raw in ("설명만 있고 JSON 이 없음", json.dumps({"items": "문자열"}), json.dumps({})):
        llm = _FakeLLM(raw=raw)
        assert await expand_needs("발화", llm=llm, settings=_settings()) == [], raw


async def test_expand_skipped_when_llm_missing() -> None:
    """llm 미구성이면 호출하지 않고 빈 리스트 — 매핑을 LLM 에 종속시키지 않는다."""
    assert await expand_needs("발화", llm=None, settings=_settings()) == []


async def test_expand_is_injectable_without_llm() -> None:
    """전개는 주입형 seam(§3.2) — A(LLM)→B(카탈로그)→C(캐시) 교체가 주입 한 줄이어야 한다.

    **`llm=None` 이어도 주입 전개기는 동작해야 한다** — B(카탈로그 임베딩)·C(캐시)는 LLM 을 쓰지
    않는다. `llm` 미구성 가드를 `expand_needs` 에 두면 그 교체가 막혀 seam 이 무력해지므로,
    가드는 `_llm_expand`(LLM 을 실제로 쓰는 쪽) 안에 있어야 한다.
    """

    async def _fake_expand(utterance, **_):
        return ["디퓨저", "식기 세트"]

    out = await expand_needs("발화", llm=None, settings=_settings(), expand=_fake_expand)
    assert out == ["디퓨저", "식기 세트"]


# ── 관측 기록 (§10, api-spec §6.3) ───────────────────────────────────────────


async def test_expand_records_model_call() -> None:
    """전개 LLM 호출은 `chat_request` 로그에 잡혀야 한다 (PR #203 리뷰).

    `observability.py` 가 `model_calls` 를 `model`/`promptTokens`/`completionTokens` 로 합산하고,
    api-spec §6.3 은 이를 "LLM 호출별 합산"으로 규정한다. 전개는 **조건부 +1 호출**이라고 정본
    `SPEC-RECOMMEND-001` AC-REC-37·§비기능(`2 + 1`)에 명시했으므로, 기록이 빠지면 그 주장을
    운영 로그로 검증할 수 없다(설계 OPEN-2 의 tier 실측도 같은 로그에 의존).
    """
    observer = _FakeObserver()
    llm = _FakeLLM(raw=_items("디퓨저", "식기 세트"))
    out = await expand_needs("집들이 선물", llm=llm, settings=_settings(), observer=observer)
    assert out == ["디퓨저", "식기 세트"]
    assert observer.models == ["gpt-5-nano"]  # needs_expansion_tier="fast"


async def test_expand_records_model_call_even_when_llm_fails() -> None:
    """호출이 실패해도 기록한다 — 실패한 호출도 비용이 발생하고, `decompose`(graph.py:154)·
    rerank(recommendation/graph.py:311)도 **호출 전** 기록이라 같은 규약을 따른다."""
    observer = _FakeObserver()
    llm = _FakeLLM(error=True)
    assert await expand_needs("집들이 선물", llm=llm, settings=_settings(), observer=observer) == []
    assert observer.models == ["gpt-5-nano"]


async def test_expand_records_nothing_when_llm_missing() -> None:
    """llm 미구성이면 호출 자체가 없으므로 기록도 없다 — 없는 비용을 로그에 만들지 않는다."""
    observer = _FakeObserver()
    assert (
        await expand_needs("집들이 선물", llm=None, settings=_settings(), observer=observer) == []
    )
    assert observer.models == []


async def test_injected_non_llm_expander_records_nothing() -> None:
    """주입형 전개기(B 카탈로그·C 캐시)는 LLM 을 쓰지 않으므로 모델 호출을 기록하지 않는다.

    기록을 호출부(`graph.py`)에 두면 **전개기 종류와 무관하게** 기록돼 유령 모델 호출이 남는다
    (LLM 을 쓰는 `_llm_expand` 안에 둬야 하는 이유 — `llm is None` 가드와 동일한 원칙, §3.2).
    """
    observer = _FakeObserver()

    async def _fake_expand(utterance, **_):
        return ["디퓨저", "식기 세트"]

    out = await expand_needs(
        "집들이 선물", llm=None, settings=_settings(), expand=_fake_expand, observer=observer
    )
    assert out == ["디퓨저", "식기 세트"]
    assert observer.models == []
