"""목적·상황형 발화의 상품 전개 — 감지 (이슈 #198·**#217**, DESIGN-NEEDS-EXPANSION-198 §4).

`decompose` 가 목적형 발화를 구체 상품으로 전개하지 못한 것을 **코드가 결정적으로 판정**한다.
LLM 에게 "전개했니?"를 묻지 않는다 — `case` 는 같은 호출의 산출물이라 전개 실패 회차의 값을
신뢰할 수 없음이 실측으로 확인됐다(§4.1, 트리거로 부적합).

**#217 로 판정 시점이 바뀌었다** — 목적 marker 열거(초판 D2·D3)로 **미리 맞히지 않고**, 매핑을
해본 뒤 **실패한 leg 이 있으면** 전개한다(§4). 열거는 목록에 없는 표현(`재료`·`아이디어`·`필수템`)을
놓치는데, 목록을 늘리면 이미 정답 매핑되는 표현이 파괴된다(§4.0). 남는 규칙은 둘이다:

- **D1 `no_legs`** — 신호 leg 이 하나도 없다. 매핑할 것이 없어 실패 신호가 나오지 않으므로 이
  규칙만은 매핑보다 앞에 남는다.
- **D2 `mapping_failed`** — `map_categories` 가 canonical 을 못 낸 leg 이 있다(`unresolved`).
  무엇이 `unresolved` 에 담기는지는 매핑 쪽 계약이다(§4 ①②만, ③④ 제외 — `test_category_mapping`).

`case` 는 **게이트**로 쓴다(§4.2, PR #203 리뷰) — `case != 3` 이면 어떤 규칙도 발동하지 않는다.
case 2 는 의도적으로 카테고리 무관이라 좁히면 안 되는데(#22·#162) **매핑 실패는 case 2 에서
구조적으로 발생한다** — 맞는 칸이 없는 것이 정상이기 때문이다(`'평점 높은 거'` → `게임 > PC게임`
0.3420 / 마진 0.0171). 배제에만 쓰이므로 트리거보다 낮은 신뢰도로 충분하다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.buyer.recommendation.needs_expansion import (
    count_signal_legs,
    detect_expansion_need,
    expand_needs,
)
from app.agents.buyer.recommendation.state import CategoryQuery
from app.core.llm import LLMError


def _detect(
    queries: list[str | None],
    *,
    case: int = 3,
    unresolved: list[str] | None = None,
) -> str | None:
    """기본 case=3 — 대부분의 테스트는 목적형 발화를 다루므로 그 전제를 기본값으로 둔다.

    `unresolved` 미지정은 **매핑이 전부 성공했다**는 뜻이다(#217 §4 D2 미해당).
    """
    legs = [CategoryQuery(None, q) for q in queries]
    return detect_expansion_need(legs, case=case, unresolved=unresolved or [])


def _detect_legs(
    legs: list[CategoryQuery], *, case: int = 3, unresolved: list[str] | None = None
) -> str | None:
    return detect_expansion_need(legs, case=case, unresolved=unresolved or [])


# ── case 게이트: case 2(구조화 조건만) 보호 (PR #203 리뷰 · #217 로 근거 보강) ──


def test_case_two_is_gated_even_when_mapping_fails() -> None:
    """[#217] case 2 발화는 **매핑이 실패해도** 전개하지 않는다 — 게이트가 전 규칙에 걸린다.

    이 회귀는 #217 이후 오히려 더 잘 터진다. 초판에서는 marker `'거'` 로 끝나는 leg 이 D3 를
    발동시키는 경로였는데, 이제는 **거리·마진이 직접** 걸어들인다 — `'평점 높은 거'` 는
    `게임 > PC게임` 0.3420 / 마진 0.0171 로 매핑 실패다(§4.5 실측).

    case 2 leg 은 **taxonomy 에 맞는 칸이 없는 것이 정상**이라 매핑 실패가 구조적으로 발생한다.
    게이트가 없으면 "카테고리 무관·조건만" 의도가 지어낸 상품 목록으로 좁혀진다(#22·#162).
    """
    assert _detect(["평점 높은 거"], case=2, unresolved=["평점 높은 거"]) is None
    assert _detect(["인기 많은 거"], case=2, unresolved=["인기 많은 거"]) is None
    # 같은 입력이라도 case 3 이면 전개 대상이다(게이트만의 차이임을 고정)
    assert _detect(["방한 아이템"], case=3, unresolved=["방한 아이템"]) == "mapping_failed"


def test_d1_requires_case_three() -> None:
    """[PR #203 리뷰] D1 은 `case == 3` 일 때만 발동한다 — case 2 를 전개에서 보호한다.

    `"5만원 이하 아무거나"`(case 2, 구조화 조건만)와 `"부모님 환갑 선물"`(case 3, 전개 실패)은
    **둘 다 `categoryQueries` 가 빈다**. leg 유무만 보면 구분되지 않는데, 처방은 정반대다 —
    case 2 는 좁히면 안 되고(무필터 전체 검색, #22·#162) case 3 은 좁혀야 한다.

    `case` 를 **트리거**로 쓰는 것은 §4.1 에서 기각했지만(선언≠사실·오탐), **게이트**로 쓰는 것은
    실측이 뒷받침한다 — `"5만원 이하 아무거나"` 는 3/3 안정적으로 `case=2` 였다.
    """
    assert _detect([], case=3) == "no_legs"
    assert _detect([], case=2) is None
    assert _detect([], case=1) is None


def test_category_agnostic_query_never_expanded() -> None:
    """조건만 있는 무필터 조회는 전개하지 않는다 — 전개 LLM 이 카테고리를 지어내면 계약이 깨진다.

    전개 프롬프트는 "최소 2개를 채우세요"로 **항상 ≥2개를 만들도록** 강제하므로, 목적이 없는
    입력에도 그럴듯한 상품명을 지어낸다. 그것이 legs 에 얹히면 `filters.category` 가 채워져
    "카테고리 무관, 가격만 필터"라는 사용자 의도가 파괴된다(#22 가 테스트로 고정한 계약).
    이 경로는 #162(조건 없는 발화를 인기상품·프로필 후보로)가 개선할 자리이므로 보존해야 한다.
    """
    assert _detect([], case=2) is None
    assert _detect(["인기 많은 거"], case=2, unresolved=["인기 많은 거"]) is None


# ── 신호 판정: raw_category 포함 (PR #203 리뷰) ───────────────────────────────


def test_raw_only_leg_counts_as_signal() -> None:
    """[PR #203 리뷰] `raw_category` 만 있는 leg 도 **신호**다 — 저장소 규약은 `raw_category or query`.

    `decompose._parse_category_queries`(`q.raw_category or q.query`)·graph 멀티턴 승계 판정
    (`not any(q.raw_category or q.query ...)`) 모두 두 필드를 함께 본다. 스키마상 `category` 만
    채우고 `query=null` 인 leg 이 나올 수 있는데, 그건 **이미 올바른 카테고리 신호**다. query 만
    보면 D1 이 오탐해 정상 분류된 leg 을 지어낸 상품 목록으로 교체해버린다.

    #217 이후 이 leg 은 **매핑에 태워져** raw 앵커로 조회된다 — 성공하면 canonical 을 내고,
    실패하면 `unresolved` 로 잡힌다. 어느 쪽이든 D1 이 가로챌 일이 아니다.
    """
    assert _detect_legs([CategoryQuery("음향가전", None)]) is None


def test_blank_and_null_queries_are_ignored_as_signal() -> None:
    """공백·None query 는 신호가 아니다 — 전부 비면 D1 이다.

    신호 없는 leg 은 map_categories 가 어차피 스킵하므로(#22), 그것 때문에 전개를 트리거하면
    멀티턴 승계 등 무관한 경로까지 끌려온다.
    """
    assert _detect([None]) == "no_legs"
    assert _detect(["   "]) == "no_legs"
    assert _detect([None, "디퓨저"]) is None


# ── #428 리뷰 5차 R5-1 — `count_signal_legs` 헬퍼 (판정식 단일화) ────────────────


def test_count_signal_legs_matches_signal_rule() -> None:
    """[#428 리뷰 5차 R5-1] `count_signal_legs` 는 위 신호 판정(`raw_category or query`, 공백
    trim)과 **같은 식**이다 — `graph.py` 의 `sibling_expansion` 게이트(원 발화 니즈 개수로
    합의 필터를 끄는 판정, #444 Claude 리뷰)가 이 함수를 그대로 재사용하므로, 여기서 규칙이
    갈리면 그래프와 이 모듈의 D1 판정이 어긋난다(규칙은 한 벌 — `resolve_category_action`·
    `has_new_category_signal` 과 같은 저장소 규약)."""
    assert count_signal_legs([]) == 0
    assert count_signal_legs([CategoryQuery(None, None)]) == 0
    assert count_signal_legs([CategoryQuery(None, "   ")]) == 0  # 공백-only 는 신호 아님
    assert count_signal_legs([CategoryQuery("음향가전", None)]) == 1  # raw 만
    assert count_signal_legs([CategoryQuery(None, "디퓨저")]) == 1  # query 만
    assert (
        count_signal_legs([CategoryQuery("음향가전", "이어폰"), CategoryQuery(None, "노트북")]) == 2
    )
    assert (
        count_signal_legs(
            [CategoryQuery(None, None), CategoryQuery(None, "  "), CategoryQuery(None, "디퓨저")]
        )
        == 1
    )


# ── D1 신호 없음 ────────────────────────────────────────────────────────────


def test_d1_no_legs_triggers() -> None:
    """leg 이 아예 없으면 전개한다 — 실측 `"부모님 환갑 선물"` 3회차가 `[]` 였다.

    D1 이 매핑 실패 판정과 별도로 남는 이유: **매핑할 것이 없으면 실패 신호도 나오지 않는다.**
    `unresolved` 는 당연히 비어 있으므로 D2 로는 잡히지 않는다.
    """
    assert _detect([]) == "no_legs"
    assert _detect([], unresolved=[]) == "no_legs"


def test_d1_wins_when_no_signal_legs() -> None:
    """신호 leg 이 없으면 `no_legs` 로 라벨한다 — 사유는 턴당 하나여야 한다(관측 분포 오염 방지)."""
    assert _detect([None, "  "], unresolved=["잔여"]) == "no_legs"


# ── D2 매핑 실패 (#217) ─────────────────────────────────────────────────────


def test_mapping_failure_triggers() -> None:
    """매핑이 canonical 을 못 낸 leg 이 있으면 전개한다 — marker 목록과 무관하다.

    실측(§4.5 ②)에서 회수되는 것들이 전부 여기다. `재료`·`아이디어`·`필수템` 은 초판 marker
    목록에 없어 미검출이었다:

        김밥 재료          0.3027 / 0.0054
        감자탕 재료        0.3177 / 0.0213
        집들이 선물 아이디어  0.3156 / 0.0133
        자취 필수템        0.2736 / 0.0202
    """
    assert _detect(["김밥 재료"], unresolved=["김밥 재료"]) == "mapping_failed"
    assert _detect(["자취 필수템"], unresolved=["자취 필수템"]) == "mapping_failed"


def test_partial_mapping_failure_triggers() -> None:
    """leg 일부만 실패해도 전개한다 — 합집합 배선이라 성공한 leg 을 잃지 않는다(§6).

    초판 D3 는 **모든** leg 이 목적 표현일 때만 트리거했다. 전개가 legs 를 교체했으므로 좋은 leg 이
    섞이면 그것까지 날아갔기 때문이다. #217 은 성공분을 보존하고 전개분을 **더하므로** 그 보수성이
    필요 없다 — `"이사 가는데 냉장고랑 필요한 것들"` 에서 냉장고를 지키면서 나머지를 전개한다.
    """
    assert (
        _detect(["냉장고", "이사 필요한 것들"], unresolved=["이사 필요한 것들"]) == "mapping_failed"
    )


# ── 미트리거(정상 경로) — §4.5 ① 대조군 회귀 고정 ────────────────────────────


def test_successful_mapping_never_triggers() -> None:
    """매핑이 전부 성공하면 전개하지 않는다 — 이 이슈의 오탐 0 보장이 여기서 나온다.

    아래는 §4.5 ① 실측 대조군이다. 초판 marker 에 `재료` 를 넣었다면 앞의 넷이 전개로 갈아엎혔다
    (오탐 4 / 회수 2 로 손해가 커서 이슈 본문이 기각한 처방):

        한방재료     건강식품 > 인삼/한방재료   0.1443 / 0.0640
        떡볶이 재료   냉장/냉동식품 > 떡볶이/떡국 0.1736 / 0.1438
        수예 재료    홈패브릭/수예 > 수예용품    0.1590 / 0.0317
        베이킹 재료   주방용품 > 홈베이킹용품     0.2068 / 0.0397
    """
    for q in ("한방재료", "떡볶이 재료", "수예 재료", "베이킹 재료"):
        assert _detect([q]) is None, q


def test_normal_product_query_not_triggered() -> None:
    """단일 상품 질의는 전개하지 않는다 — 불필요한 LLM 호출·엉뚱한 확장 방지.

    `한우 선물세트`(0.1459)·`청바지`(0.1224)·`무선 이어폰`(0.1955) 전부 매핑 성공이다(§4.5 ①).
    초판에서는 `endswith` 로 `'선물'` 오탐을 피했는데, 이제는 매핑 결과가 직접 판정한다.
    """
    for q in ("청바지", "무선 이어폰", "한우 선물세트", "청소도구 세트"):
        assert _detect([q]) is None, q


def test_already_expanded_turn_not_triggered() -> None:
    """이미 구체 상품으로 전개된 턴은 손대지 않는다 — 실측 성공 회차 재현."""
    assert _detect(["홍삼", "안마의자", "한우 선물세트", "영양제"]) is None
    assert _detect(["전자레인지", "이불", "냄비", "빨래바구니"]) is None


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
