"""멀티턴 카테고리 승계 의도 3분기 — carry/clear/replace (#84).

#59 승계 가드는 "이번 턴 카테고리 신호 없음"을 **무조건 리파인**으로 읽어, "5만원 이하 아무거나"
같은 카테고리-무관 리셋 발화까지 직전 카테고리(이어폰)로 좁혔다. decompose 가 내는
전용 분류기(`category_scope`)가 낸 `scopeFree` 를 가드가 소비해 세 의도를 가른다 — decompose
프롬프트 안의 인라인 필드로 받는 안은 실측으로 기각됐다(이득 0 · 전환 축 손해). 하네스는
`test_condition_actions.py` 패턴을 그대로 따른다(공용 모듈로 뽑지 않는다 — diff 를 키운다).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.graph import get_thread_store, run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.recommendation.category_scope import _SYSTEM as _SCOPE_SYSTEM
from app.agents.buyer.recommendation.decompose import resolve_category_action
from app.agents.buyer.session_state import context_thread_key
from app.agents.profile.store import get_profile_store
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core.session_context import BuyerSessionInput
from app.schemas.chat import BuyerChatRequest
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

# 1턴이 남기는 승계 카테고리 — tests/_fakes.py DEFAULT_DECOMPOSE + conftest 의 fake 매퍼 산출.
PRIOR_CATEGORY = "무선이어폰"
# 2턴 매퍼가 돌려주는 canonical — 승계값(PRIOR_CATEGORY)·해제(None)와 셋 다 구별된다.
LAPTOP_CANONICAL = "디지털/가전 > 노트북"


def _buyer_payload(**updates):
    payload = {"sessionId": "s1", "threadId": "t1", "message": "추천해줘"}
    payload.update(updates)
    return payload


def _member() -> Identity:
    return Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        request_id="category-intent-84-test",
        context_id=context.context_id,
        record_model_call=lambda *_: None,
    )


async def _run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(request, identity, observer=observer, **kwargs):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


class _RecordingSearch:
    def __init__(self) -> None:
        self.filters: list[ProductSearchFilters] = []

    async def __call__(self, filters, exclude_product_ids=None):  # noqa: ANN001
        self.filters.append(filters)
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )


async def _push_ok(push) -> bool:  # noqa: ANN001
    return True


async def _collect(gen) -> list[dict]:  # noqa: ANN001
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


def _turn2_llm(**over) -> FakeLLM:
    """2턴 decompose 산출 — 기본은 "신호 없는 조건 다듬기"(카테고리 신호 0, priceMax 만)."""
    payload = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "categoryQueries": [],
        "filters": {"priceMax": 50000},
    }
    payload.update(over)
    return FakeLLM(decompose=payload)


def _scope_llm(scope_free, **over):  # noqa: ANN001
    """2턴용 — 분류기 산출(`scopeFree`)과 decompose 산출을 함께 흉내 내는 가짜.

    배포 경로가 같은 클라이언트로 두 호출을 하므로 통합 테스트도 그 모양이어야 한다.
    `_ScopeAwareLLM` 은 아래에 정의돼 있다(호출 시점에는 이미 로드돼 있다).
    """
    payload = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "categoryQueries": [],
        "filters": {"priceMax": 50000},
    }
    payload.update(over)
    return _ScopeAwareLLM(scope_free=scope_free, decompose=payload)


def _laptop_mapper():
    """신호 있는 leg 를 canonical 로 매핑하는 매퍼 — 매핑을 실제로 탔는지 판별한다.

    **직전 카테고리를 복사한 leg(prior 에코)는 그 카테고리로 되돌린다** — 실 매퍼가 하는 일이
    그것이고, 그 사실이 이 이슈의 핵심이다(에코 leg 를 "강한 신호"로 읽으면 리셋이 무동작이 된다).
    그 밖의 leg 는 노트북 canonical 로 보내 승계값·해제(None)와 세 결과가 구별되게 한다.
    """

    async def _map(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        legs = []
        for query in category_queries:
            if not (query.raw_category or query.query):
                continue
            echo = PRIOR_CATEGORY in (query.raw_category or "") or "이어폰" in (query.query or "")
            legs.append((PRIOR_CATEGORY if echo else LAPTOP_CANONICAL, query.query))
        return CategoryMapping(legs=legs, unresolved=[])

    return _map


async def _seed_prior_category(identity) -> None:
    """1턴 — category=무선이어폰 / priceMax=50000 을 스레드에 남긴다."""
    await _collect(
        _run_buyer_turn(
            BuyerChatRequest.model_validate(_buyer_payload()),
            identity,
            llm=FakeLLM(),
            search=_RecordingSearch(),
            push_fn=_push_ok,
        )
    )


def _turn2_request(message: str = "5만원 이하로", **updates):
    return BuyerChatRequest.model_validate(_buyer_payload(message=message, **updates))


async def _second_turn(identity, llm, request=None, **kwargs):  # noqa: ANN001
    """2턴을 돌리고 (검색 필터, 이벤트, 요청)을 돌려준다."""
    request = request or _turn2_request()
    search = _RecordingSearch()
    events = await _collect(
        _run_buyer_turn(
            request,
            identity,
            llm=llm,
            search=search,
            push_fn=_push_ok,
            map_categories=_laptop_mapper(),
            **kwargs,
        )
    )
    return search, events, request


def _chip_fields(events) -> set[str]:  # noqa: ANN001
    chips = next(event for event in events if event["type"] == "conditions")["data"]["chips"]
    return {chip["field"] for chip in chips}


# ─────────── §4.1 3의도 구분 ───────────


async def test_carry_keeps_the_prior_category_on_a_refine_turn() -> None:
    """① 리파인 — 분류기가 "종류를 놓는 말이 아니다"(False)라 하면 직전 카테고리를 승계한다."""
    identity = _member()
    await _seed_prior_category(identity)

    search, events, _ = await _second_turn(identity, _scope_llm(False))

    assert search.filters[-1].category == PRIOR_CATEGORY
    assert search.filters[-1].price_max == 50000
    assert "category" in _chip_fields(events)


async def test_clear_releases_the_prior_category_and_persists_the_release() -> None:
    """③ 자연어 리셋 — 분류기가 True 를 내면 카테고리를 풀고(무필터) 그 상태가 저장된다."""
    identity = _member()
    await _seed_prior_category(identity)

    search, events, request = await _second_turn(identity, _scope_llm(True))

    assert search.filters[-1].category is None
    assert search.filters[-1].price_max == 50000
    assert "category" not in _chip_fields(events)
    assert "priceMax" in _chip_fields(events)

    stored = await (await get_thread_store()).get(await _thread_key(request, identity))
    assert stored is not None
    assert stored.category is None  # 다음 턴에 풀린 카테고리가 부활하지 않는다


async def test_replace_maps_the_new_category_instead_of_carrying() -> None:
    """교체 — 새 상품을 말한 턴은 매퍼가 canonical 로 바꾼 값이 실린다."""
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _scope_llm(False, categoryQueries=[{"category": "노트북", "query": "노트북"}]),
        request=_turn2_request("이번엔 노트북"),
    )

    assert search.filters[-1].category == LAPTOP_CANONICAL


# ─────────── §4.2 폴백·모순 ───────────


async def test_classifier_silence_falls_back_to_carry_without_error() -> None:
    """분류기가 판정하지 못하면(비 JSON·장애) **오늘 동작(carry)** 으로 돌아가고 턴은 살아 있다.

    보조 신호 하나의 실패로 추천 턴이 죽으면 안 된다 — `_map_or_empty`·완화 칩 조회와 같은
    degrade 원칙이다.
    """
    identity = _member()
    await _seed_prior_category(identity)

    search, events, _ = await _second_turn(identity, _scope_llm(None))

    assert search.filters[-1].category == PRIOR_CATEGORY
    assert "error" not in {event["type"] for event in events}


async def test_no_signal_and_no_release_stays_on_carry() -> None:
    """해제 신호도 leg 도 없으면 리파인(#59 승계)을 깨지 않는다 — 판정 규칙 3."""
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(identity, _scope_llm(False))

    assert search.filters[-1].category == PRIOR_CATEGORY


async def test_first_turn_without_prior_category_is_a_no_op() -> None:
    """prior 가 없는 첫 턴은 **게이트가 닫혀 분류기를 부르지도 않고**, 무필터로 끝난다."""
    identity = _member()

    search, _, _ = await _second_turn(identity, _scope_llm(True))

    assert search.filters[-1].category is None


@pytest.mark.parametrize(
    ("has_category_signal", "scope_free", "has_new_category_signal", "expected"),
    [
        # ── ① 새 카테고리 지목이 최우선 ── 혼합 발화("스피커 아무거나")에서 사용자가 말한
        # 카테고리가 버려지지 않게(라운드 3 F-1, 실측 32건 중 19건이 clear 였다).
        # `has_new` 가 참이면 유효 leg 이 있다는 뜻이라 `has_signal` 도 항상 참이다.
        (True, True, True, "replace"),
        (True, False, True, "replace"),
        (True, None, True, "replace"),
        # ── ② 그 다음이 해제 ── leg 가 있어도 **전부 prior 에코**면 clear 가 이긴다.
        (False, True, False, "clear"),
        (True, True, False, "clear"),
        # ── ③ 에코 leg 뿐이면 매핑 경로(오늘 동작) ──
        (True, False, False, "replace"),
        (True, None, False, "replace"),
        # ── ④ 신호가 하나도 없으면 carry(=오늘 동작) ──
        (False, False, False, "carry"),
        (False, None, False, "carry"),
    ],
)
def test_resolve_category_action_table(
    has_category_signal, scope_free, has_new_category_signal, expected
) -> None:
    """판정은 (신호 유무 × scopeFree × 새 카테고리 지목) 세 축으로 완결된다.

    인라인 `categoryAction` 축은 실측 기각으로 사라졌고(이득 0 · 전환 축 손해), prior 는 여전히
    인자가 아니다 — "승계할 것이 있는가"는 호출부(`graph.py`) 책임이다.
    """
    assert (
        resolve_category_action(
            has_category_signal=has_category_signal,
            scope_free=scope_free,
            has_new_category_signal=has_new_category_signal,
        )
        == expected
    )


def test_scope_free_only_fires_on_exact_true() -> None:
    """`None`(판정 실패)과 `False` 는 해제 신호가 아니다 — 애매한 산출을 강한 쪽으로 읽지 않는다."""
    for scope_free in (None, False):
        assert (
            resolve_category_action(
                has_category_signal=False,
                scope_free=scope_free,
                has_new_category_signal=False,
            )
            == "carry"
        )


def test_new_category_signal_outranks_scope_free_in_the_pure_function() -> None:
    """[라운드 3 F-1] 순수 함수 층에서도 새 카테고리 지목이 해제를 이긴다."""
    assert (
        resolve_category_action(
            has_category_signal=True, scope_free=True, has_new_category_signal=True
        )
        == "replace"
    )


# ─────────── §4.3 칩 제거(②)와의 결합 ───────────


async def test_action_only_removal_clears_the_category() -> None:
    """② 칩 제거 — 발화 없이 액션만 온 턴도 카테고리가 사라진다(#278 동작 잠금)."""
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _turn2_llm(),
        request=BuyerChatRequest.model_validate(
            {
                "sessionId": "s1",
                "threadId": "t1",
                "conditionActions": [{"op": "remove", "field": "category"}],
            }
        ),
    )

    assert search.filters[-1].category is None


async def test_removal_plus_new_category_does_not_resurrect_the_old_one() -> None:
    """FE 액션 선적용 → decompose 순서 — 제거된 옛 카테고리가 승계로 되살아나지 않는다."""
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _turn2_llm(categoryQueries=[{"category": "노트북", "query": "노트북"}]),
        request=_turn2_request(
            "이번엔 노트북 보여줘",
            conditionActions=[{"op": "remove", "field": "category"}],
        ),
    )

    assert search.filters[-1].category == LAPTOP_CANONICAL


async def test_price_max_removal_lets_the_new_price_win() -> None:
    """가격 칩 제거 + 새 가격 발화 — 옛 상한은 사라지고 이번 턴 값만 남는다."""
    identity = _member()
    await _seed_prior_category(identity)

    search, _, request = await _second_turn(
        identity,
        _turn2_llm(filters={"priceMax": 30000}),
        request=_turn2_request(
            "3만원 이하",
            conditionActions=[{"op": "remove", "field": "priceMax"}],
        ),
    )

    assert search.filters[-1].price_max == 30000
    assert search.filters[-1].category == PRIOR_CATEGORY
    stored = await (await get_thread_store()).get(await _thread_key(request, identity))
    assert stored is not None
    assert stored.price_max == 30000


# ─────────── §4.4 액션-only 턴의 프로필 세션 버퍼 ───────────


async def _session_buffer(identity, request) -> list[str]:  # noqa: ANN001
    store = await get_profile_store()
    return await store.get_session_ctx(conversation_key(identity.user_id, request.session_id))


@pytest.mark.parametrize("blank_message", ["", "   "])
async def test_action_only_turn_does_not_push_a_blank_utterance_into_the_buffer(
    blank_message,
) -> None:
    """빈/공백 발화는 취향 신호가 0인데 버퍼(슬라이딩 윈도우)만 밀어낸다 — 쌓지 않는다(§2.6)."""
    identity = _member()
    await _seed_prior_category(identity)

    _, _, request = await _second_turn(
        identity,
        _turn2_llm(),
        request=BuyerChatRequest.model_validate(
            {
                "sessionId": "s1",
                "threadId": "t1",
                "message": blank_message,
                "conditionActions": [{"op": "remove", "field": "priceMax"}],
            }
        ),
    )

    assert await _session_buffer(identity, request) == ["추천해줘"]


async def test_normal_utterance_still_reaches_the_session_buffer() -> None:
    """대조군 — 빈 발화 가드가 정상 경로까지 죽이지 않았다."""
    identity = _member()
    await _seed_prior_category(identity)

    _, _, request = await _second_turn(
        identity,
        _turn2_llm(),
        request=_turn2_request("더 저렴한 걸로"),
    )

    assert await _session_buffer(identity, request) == ["추천해줘", "더 저렴한 걸로"]


# ─────────── #84 Task 3 — 전용 분류기(scopeFree)와의 통합 ───────────


class _ScopeAwareLLM(FakeLLM):
    """system 프롬프트로 분기하는 가짜 — 분류기에는 scopeFree, 그 밖에는 decompose 산출.

    두 호출이 같은 `tier="fast"` 라 tier 로는 갈리지 않는다. 배포 경로가 두 호출을 하므로 통합
    테스트도 그 모양이어야 한다(하나만 흉내 내면 측정이 배포와 갈라진다).
    """

    def __init__(self, *, scope_free, decompose=None) -> None:  # noqa: ANN001
        super().__init__(decompose=decompose)
        self._scope_free = scope_free

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        if system == _SCOPE_SYSTEM:
            if self._scope_free is None:
                return "판정 불가"  # JSON 아님 → 분류기가 None 으로 떨어뜨린다
            return json.dumps({"scopeFree": self._scope_free})
        return await super().complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )


def _prior_echo_decompose(**over):
    """실측된 배포 모양 — 리셋 발화에도 LLM 이 **직전 카테고리를 복사한 leg** 를 함께 낸다.

    `_SYSTEM` 의 categoryQueries 불릿이 그렇게 지시하기 때문이며, 리셋 기대 32건 중 30~31건이
    이 모양이었다. 이 leg 를 "강한 신호"로 읽으면 해제가 구조적으로 불가능해진다.
    """
    payload = {
        "intent": "recommend",
        "reply": "",
        "case": 2,
        "categoryQueries": [{"category": PRIOR_CATEGORY, "query": "무선 이어폰"}],
        "filters": {"priceMax": 50000},
    }
    payload.update(over)
    return payload


async def test_classifier_true_releases_the_category_even_when_legs_echo_the_prior() -> None:
    """**이 PR 의 핵심 회귀 잠금** — "5만원 이하 아무거나" 가 이어폰 안에 갇히지 않는다.

    분류기가 True 를 내고 decompose 는 직전 카테고리를 복사한 leg 를 함께 내는 조합이 실측된
    배포 모양이다(30~31/32). 이 턴을 살리는 것은 분류기뿐이다 — 같은 판정을 decompose 인라인
    필드로 받는 안은 실측으로 기각됐다(이득 0 · 전환 축 손해).
    """
    identity = _member()
    await _seed_prior_category(identity)

    search, events, request = await _second_turn(
        identity,
        _ScopeAwareLLM(scope_free=True, decompose=_prior_echo_decompose()),
        request=_turn2_request("5만원 이하 아무거나 있어?"),
    )

    assert search.filters[-1].category is None
    assert search.filters[-1].price_max == 50000
    assert "category" not in _chip_fields(events)
    stored = await (await get_thread_store()).get(await _thread_key(request, identity))
    assert stored is not None and stored.category is None


async def test_classifier_false_keeps_the_prior_category_on_the_same_output() -> None:
    """대조군 — 같은 decompose 산출이라도 분류기가 False 면 카테고리가 유지된다.

    이 짝이 없으면 위 테스트는 "분류기가 실제로 판정을 갈랐다"를 증명하지 못한다(둘 다 같은
    leg·같은 필터라 차이는 분류기 산출 하나뿐이다). 유지되는 경로는 **에코 leg 재매핑**이다 —
    확정값은 `replace` 지만 그 leg 가 직전 카테고리라 결과적으로 같은 값이 실린다(P2 지적).
    """
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _ScopeAwareLLM(scope_free=False, decompose=_prior_echo_decompose()),
        request=_turn2_request("더 저렴한 걸로"),
    )

    assert search.filters[-1].category == PRIOR_CATEGORY


async def test_new_category_beats_scope_free_on_a_mixed_utterance() -> None:
    """[라운드 3 F-1] **혼합 발화**에서 사용자가 말한 카테고리가 버려지면 안 된다.

    `"스피커 아무거나 보여줘"` 는 분류기가 True 를 내면서 decompose 가 `(None,"스피커")` leg 를
    함께 내는 턴이다. 초판 순서(`scope_free` 우선)에서는 그 leg 가 통째로 버려져 **무필터**가
    됐다 — 실 LLM 실측에서 이 발화는 **8/8 이 clear** 였다(혼합 발화 4종 32건 중 19건).

    새 카테고리 지목이 가장 강한 신호다: 리셋 발화가 함께 내는 leg 는 실측상 **전부 prior
    에코**라 `clear` 는 그대로 살아나고, 새 카테고리를 말한 leg 만 이 규칙에 걸린다.
    잔여 위험의 방향도 바뀐다 — 오탐하면 "카테고리가 안 풀림"(사용자가 한 번 더 말하면 된다)이고,
    종전은 "사용자가 말한 카테고리가 사라짐"이었다.
    """
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _ScopeAwareLLM(
            scope_free=True,
            decompose=_prior_echo_decompose(
                categoryQueries=[{"category": None, "query": "스피커"}]
            ),
        ),
        request=_turn2_request("스피커 아무거나 보여줘"),
    )

    assert search.filters[-1].category == LAPTOP_CANONICAL  # 새 카테고리로 매핑(에코가 아니다)


async def test_scope_free_still_wins_when_every_leg_is_a_prior_echo() -> None:
    """대조군 — leg 가 전부 prior 에코면 `clear` 가 그대로 이긴다(#84 본 결함의 모양).

    이 짝이 없으면 위 테스트는 "leg 만 있으면 무조건 replace"와 구분되지 않는다.
    """
    identity = _member()
    await _seed_prior_category(identity)

    search, _, _ = await _second_turn(
        identity,
        _ScopeAwareLLM(scope_free=True, decompose=_prior_echo_decompose()),
        request=_turn2_request("5만원 이하 아무거나 있어?"),
    )

    assert search.filters[-1].category is None
