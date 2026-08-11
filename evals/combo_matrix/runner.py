"""ScriptedLLM/fake 주입 결정론 관측 러너 (§5) — `observation_mode=ci` 케이스만 실행한다.

`observation_mode=manual` 케이스(context != none — 멀티턴 승계는 실 LLM 해석이 필요)는 여기서
건너뛰고 `linked` 로 intent_probe 셀만 가리킨다. **미정의 셀의 관측은 기록만 하고 기대값으로
고정(assert)하지 않는다** — 고정하면 미정의 동작을 사실상 스펙화하는 것이다(§5).

## 알려진 관측 경계 (조용한 truncation 이 아니라 명시적 스코프)

- **`embedding_missing` degrade 는 이 하네스가 관측할 수 없다.** `run_buyer_turn` 의 `search`
  주입은 `app/services/search_service.py` 의 임베딩 재정렬 단계보다 **상류**를 대체한다 —
  주입된 `search` 콜러블은 이미 "완성된" 검색 결과를 돌려주므로, 그 안에서 임베딩 재정렬이
  성공했는지 조용히 degrade 했는지는 이 경계에서 구별할 방법이 없다(둘 다 "정상 순서의 결과"로
  보인다). 그래서 `embedding_missing` 케이스는 `degrade=none` 과 동일하게 실행하고, `observed`
  에 이 한계를 명시한다. 임베딩 재정렬 자체의 단위 테스트는 `tests/unit/test_search_service.py`
  (있다면) 소관이다.
- **HOME(I-22) 의 degrade 축은 지면별 어휘를 쓴다(#367)** — CHAT 전용 3종(`embedding_missing`·
  `rerank_failed`·`spring_timeout`)은 `axes.json` 의 excludes 제약으로 HOME 조합 자체가 생성되지
  않는다. HOME 은 `none`·`profile_unavailable`·`catalog_unavailable`·`catalog_timeout`·
  `reason_degraded` 5종만 실행한다(api-spec §3.7 v0.26.1 규범).
- **필터 축의 검색 경계 관측(#381 → #426)** — 이 러너는 `fakes.make_recording_filtering_search()`
  를 써서 search 콜러블이 실제로 받은 `ProductSearchFilters`(경계 도달값)를
  `observed.searchFilters` 에 담는다. 대역은 `search_catalog` 를 대체하지 않고 그 아래
  `SearchBackend` 자리에 서므로(#426), **하드필터 8축이 전부 실제로 걸러진다** — Spring 와이어
  축(keyword·category·price·brand·color)은 대역이 WHERE 계약으로, AI 사후필터 축
  (rating_min·attr_conditions)은 배포 코드(`apply_ai_side_filters`)가. `attr_conditions` 는
  Spring 에 안 나가는 축이라 `searchFilters` 에 값이 실려 있어도 적용 여부를 알 수 없어,
  사후필터 호출 자체를 `observed.attrConditionsPostFilter` 로 계측한다(attr 축 present 행 한정).
- **`keyword` 는 category leg 가 있으면 경계에 도달하지 않는다** — `#51` 규칙
  (`recommendation/graph.py` 의 `drop_keyword`)이 leg 검색어에서 keyword 를 비우기 때문이다.
  **대역의 한계가 아니라 앱의 정의된 동작**이고, 그래서 `searchFilters.keyword` 는 category 가
  present 인 케이스에서 `null` 이 정상이다. keyword 축을 실제로 재는 것은 category 가 없는
  directed 케이스가 담당한다(§ `axes.json` directedCases, README "알려진 관측 한계").
- search 가 아예 안 불린 케이스(popular 경로·담기 계열 등)는 `observed.searchCallCount == 0` 이고
  `searchFilters` 는 `null` 이다 — "안 불렸다"와 "전부 null 이었다"를 데이터에서 구별한다(§ README
  "알려진 관측 한계"). 상세 근거는 `evals/combo_matrix/README.md` 참조.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.auth import Identity  # noqa: E402
from app.services import home_recommendation as home_svc  # noqa: E402
from app.services import search_service  # noqa: E402
from app.services import spring_client  # noqa: E402
from app.schemas.spring import WishlistItem, WishlistView  # noqa: E402
from app.services.spring_client import (  # noqa: E402
    CartError,
    SpringUnavailableError,
    WishlistError,
)
from evals.combo_matrix.fakes import (  # noqa: E402
    CATALOG_PRODUCTS,
    RecordingFilteringSearch,
    RecordingPush,
    failing_order_status,
    failing_search,
    make_exact_match_category_mapping,
    make_order_status_ok,
    make_popular,
    make_recording_filtering_search,
    make_search,
    map_categories_noop,
)
from evals.combo_matrix.generator import FILTER_AXES  # noqa: E402
from evals.combo_matrix.schema import ComboCase  # noqa: E402
from tests.integration._stubs import ScriptedLLM  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from tests.unit.test_recommendation import _collect, run_buyer_turn  # noqa: E402

_CAMEL_FILTER_KEY = {
    "category": "category",
    "price_min": "priceMin",
    "price_max": "priceMax",
    "brand": "brand",
    "rating_min": "ratingMin",
    "keyword": "keyword",
    "color": "color",
}
_FILTER_SAMPLE = {
    "category": "무선이어폰",
    "price_min": 20000,
    "price_max": 50000,
    "brand": ["나이키"],
    "rating_min": 4.0,
    "keyword": "가벼운",
    "color": "블랙",
    "attr_conditions": {"방수": "true"},
}

# `observed.searchFilters`/`Projection.search_filters` 에 담는 8개 하드필터 camelCase 키 —
# `pair_runner.py` 와 정의를 공유한다(§ 이슈 #381 D3, 드리프트 방지). 한쪽에 정의하고 다른 쪽이
# import 한다 — decompose._FILTER_AXES 와 같은 축 범위(semanticQuery·excludeProductIds·limit 은
# "거르는" 조건이 아니라 후처리/제외 조건이라 뺀다).
_SEARCH_FILTER_KEYS = (
    "category",
    "priceMin",
    "priceMax",
    "brand",
    "ratingMin",
    "keyword",
    "color",
    "attrConditions",
)


def search_filters_projection(filters) -> dict:
    """search 콜러블이 실제로 받은 `ProductSearchFilters` 를 경계 도달값(camelCase 8축)으로 투영한다."""
    return {
        key: value
        for key, value in filters.model_dump(mode="json", by_alias=True).items()
        if key in _SEARCH_FILTER_KEYS
    }


def build_decompose_json(axes: dict[str, str]) -> dict:
    """축 할당을 decompose(LLM) 산출 JSON(camelCase) 으로 실현한다 — ScriptedLLM 고정 주입용."""
    intent = axes["intent"]
    data: dict = {"intent": intent, "reply": "필요하신 걸 말씀해 주세요."}
    if intent != "recommend":
        if intent in ("cart_add", "wishlist_add", "wishlist_remove"):
            data["cart"] = {"productId": 101, "quantity": 1}
        return data

    data["case"] = int(axes.get("case", "2")) if axes.get("case") not in (None, "n/a") else 2
    filters: dict = {}
    for axis in FILTER_AXES:
        if axis == "attr_conditions":
            continue
        if axes.get(axis) == "present":
            filters[_CAMEL_FILTER_KEY[axis]] = _FILTER_SAMPLE[axis]
    data["filters"] = filters
    if axes.get("attr_conditions") == "present":
        data["attrConditions"] = _FILTER_SAMPLE["attr_conditions"]
    if axes.get("category") == "present":
        # `decision.category_legs` 가 비면 `filters.category` 는 canonical-or-null degrade 로
        # 무조건 None 이 된다(app/agents/buyer/graph.py:520-537) — legs 는 오직 `categoryQueries`
        # 를 map_categories 로 매핑해야 채워진다. pair_runner 의 구 `_pair_decompose_json` 이 하던
        # 것과 동일한 값(`_FILTER_SAMPLE["category"]`)을 공유해 하드필터 값과 leg 매핑이 어긋나지
        # 않게 한다(이슈 #381 D5).
        data["categoryQueries"] = [{"category": _FILTER_SAMPLE["category"], "query": None}]
    if axes.get("buy_all") == "true":
        data["buyAll"] = True
    if axes.get("total_budget") == "present":
        data["totalBudget"] = 50000
    if axes.get("constraint_strength") == "unspecified":
        data["semanticQuery"] = ""
    return data


def _identity_for(axes: dict[str, str], case_id: str) -> Identity:
    is_guest = axes["identity"] == "guest"
    # 회원 user_id 는 **숫자 문자열**이어야 한다 — `app/agents/buyer/cart/identity.py::cart_identity`
    # 가 `int(identity.user_id)` 파싱에 실패하면 게스트·익명과 구분 없이 (None, None) 으로
    # 떨어뜨려(ValueError 흡수) 장바구니/찜 계열이 "로그인 필요"로 조용히 오분류된다 — 비숫자
    # user_id("combo-0057" 등)를 썼다가 실제로 이 오분류를 밟았다(리뷰 R3 관측 중 발견).
    numeric_uid = str(10_000 + int(case_id.rsplit("-", 1)[-1]))
    subject = f"combo-{case_id}"
    return Identity(
        user_id=None if is_guest else numeric_uid,
        is_guest=is_guest,
        seller_id=None,
        subject=subject,
    )


async def _warm_up_last_reco(request, identity) -> None:
    """cart_add/wishlist_add 의 담기 허용목록(§3.1 [보안])에 productId 101 **하나만** 올려 둔다.

    두 인텐트는 `allowed_product_ids`(직전 추천 ∪ screen.products) 밖 상품을 조용히 차단하고
    되물음으로 돌린다(`app/agents/buyer/graph.py:994-1019`) — 이 하네스가 last_reco 를 채우지
    않으면 degrade(SpringUnavailableError 등)를 주입해도 그 코드에 절대 도달하지 못하고 매번
    "추천을 먼저 받아보세요" 되물음에서 끝난다(§ 리뷰 R3 관측 중 발견). 같은 thread_id 로 정상
    recommend 턴을 1회 먼저 태워 last_reco 를 채운 뒤 실제 관측 턴을 돌린다 — `get_cart_store()`
    가 공유 백엔드를 감싸므로(cart/state.py:234-236) 두 턴 사이에 상태가 이어진다.

    [#571] 검색 결과를 `CATALOG_PRODUCTS[:1]`(상품 1건)로 좁힌다 — 전체 카탈로그를 그대로
    넘기면 push 가 여러 건을 노출해 이번 턴의 추천 카드 표면(`last_reco[:turn_count]`)이
    다건이 되고, "이거 담아줘"(combo-0004)·"이거 찜해줘"(combo-0059) 같은 근칭 지시대명사가
    해소기의 (4) 규칙("후보 다건이면 되물음")에 걸려 이 하네스가 재려는 CartError/timeout
    처리 경로 자체에 도달하지 못한다(실제 재현). 이 함수의 계약(§ 위 docstring)이 원래도
    "productId 101 **하나**"였으므로 이건 그 계약을 실제로 지키는 수정이지 새 동작이 아니다.
    """
    warm_up_decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 1,
        "filters": {"keyword": "무선 이어폰"},
    }
    await _collect(
        run_buyer_turn(
            request,
            identity,
            llm=ScriptedLLM(decompose=warm_up_decompose),
            search=make_search(CATALOG_PRODUCTS[:1]),
            push_fn=RecordingPush(),
            popular_fn=make_popular(),
            order_status_fn=make_order_status_ok,
            map_categories=map_categories_noop,
        )
    )


async def _failing_add_wishlist(*_args, **_kwargs):
    """`spring_client.add_wishlist` 몽키패치용 타임아웃 근사 — 모듈 레벨로 둬서 테스트가 직접
    호출해 예외 타입을 잠글 수 있게 한다(§ 회귀 테스트, 이슈 #376).

    실 어댑터는 `httpx.HTTPError`(TimeoutException 포함)를 전부 `WishlistError` 로 낙성한다
    (app/services/spring_client.py::add_wishlist).
    """
    raise WishlistError("wishlist 담기 시간 초과(주입)")


async def _failing_add_to_cart(*_args, **_kwargs):
    """`spring_client.add_to_cart` 몽키패치용 타임아웃 근사 — 모듈 레벨로 둬서 테스트가 직접
    호출해 예외 타입을 잠글 수 있게 한다(§ 회귀 테스트, 이슈 #376).

    실 어댑터는 `httpx.HTTPError`(TimeoutException 포함)를 전부 `CartError` 로 낙성한다
    (app/services/spring_client.py::add_to_cart).
    """
    raise CartError("장바구니 담기 시간 초과(주입)")


async def _failing_get_wishlist(*_args, **_kwargs):
    """`spring_client.get_wishlist`(I-28) 몽키패치용 타임아웃 근사.

    **담기 계열과 예외 타입이 다르다** — 조회 어댑터는 4xx/5xx·도달 불가·스키마 불일치를 전부
    `SpringUnavailableError` 로 낙성한다(`get_cart` 와 같은 degrade 규약, spring_client.py).
    담기 계열의 `WishlistError` 를 여기에 쓰면 실 어댑터가 절대 내지 않는 예외를 주입하는
    것이라 관측이 인공물이 된다(#376 이 고친 바로 그 실수, docs/lessons.md).
    """
    raise SpringUnavailableError("wishlist 조회 시간 초과(주입)")


async def _ok_get_wishlist(*_args, **_kwargs):
    """정상 찜 목록 — degrade=none 인 찜 조회·해제 케이스용.

    패치하지 않으면 실 네트워크 호출이 나가 로컬에 Spring 이 없을 때 **정상 케이스가 degrade 를
    관측한다**(관측이 환경에 따라 뒤집힌다 — docs/lessons.md "주입하지 않은 기본값은 하네스 경계
    밖"). 품절 항목을 하나 섞어 `state_suffix` 라벨 경로(#310)도 함께 관측되게 한다.
    """
    return WishlistView(
        items=[
            WishlistItem(product_id=101, name="무선 이어폰", purchase_state="AVAILABLE"),
            WishlistItem(product_id=102, name="가죽 크로스백", purchase_state="SOLD_OUT"),
        ]
    )


async def _ok_remove_wishlist(*_args, **_kwargs):
    """정상 찜 해제 — `_ok_get_wishlist` 와 같은 이유로 둔다(해제 성공 경로 관측용)."""
    return None


async def _observe_chat(case: ComboCase) -> dict:
    axes = case.axes
    degrade = axes["degrade"]
    request = SimpleNamespace(
        session_id=f"s-{case.case_id}", thread_id=f"t-{case.case_id}", message=case.utterance
    )
    identity = _identity_for(axes, case.case_id)
    if axes["intent"] in ("cart_add", "wishlist_add"):
        await _warm_up_last_reco(request, identity)
    decompose_json = build_decompose_json(axes)
    llm = ScriptedLLM(decompose=decompose_json, rerank_error=(degrade == "rerank_failed"))
    # [#443] 사전 기반 leg 보강을 이 하네스에서는 끈다. 이 하네스의 설계 전제가 **"decompose
    # 산출을 고정 주입해 축을 통제한다"** 인데(모듈 docstring), 보강은 산출이 아니라 **발화**를
    # 읽어 leg 을 만든다 — `build_utterance` 가 `case=1` 을 항상 `"무선 이어폰"` 으로 realize 하고
    # `이어폰` 이 카탈로그 사전에 있어, 켜 두면 `category=absent` 로 통제한 축이 발화 쪽에서
    # 되살아나 축 자체가 뜻을 잃는다(실측: combo-0062 가 zero_result → stop·3건).
    # #443 축의 실측 자리는 `evals/intent_probe` 의 `namedCategoryHasLeg` 다.
    _settings_for_case = get_settings()
    _prev_injection = _settings_for_case.category_leg_injection_enabled
    _settings_for_case.category_leg_injection_enabled = False
    try:
        # search 실패(spring_timeout)가 최우선 — 검색 자체가 안 되면 결과 건수는 의미가 없다.
        # 그다음 constraint_strength=overspecified_zero 는 **정의상 검색 0건**이다 — 0건을 돌려주는
        # recording 대역(`RecordingFilteringSearch(products=[])`)을 써서 표현 가능한 필터를 적용해도
        # 항상 0건이라는 목적은 만족하면서 경계 도달 filters 도 기록한다(이슈 #381 D2·D4 —
        # overspecified_zero 재검토: `PAIR_CATALOG` 표본으로는 필터를 실제로 적용해도 0건이 자연
        # 발생하지 않아 주입을 유지, README "관측 재생성 이력" 참조).
        #
        # **실제로 도는 것은 `zero_result` 종료뿐이다 — 자동완화·완화칩은 돌지 않는다(#425 판정: 정의된
        # 동작, 갭 아님).** combo-0031 에 present 인 필터축은 `price_min` 하나뿐인데,
        # `app.agents.buyer.recommendation.relaxation.FIELD_TO_ATTR` 에 `price_min`(와이어명
        # `priceMin`)이 없어(완화 축은 `priceMax`·`ratingMin`·`brand`·`color` 뿐, 모듈 docstring
        # "비카테고리 조건(가격 상한·평점 하한·브랜드·색상)만 한 단계 푼다") `build_relaxation_
        # candidates(filters, settings) == []` 다 — config 로도 `priceMin` 을 완화 축에 넣을 수 없다
        # (`app.core.config.Settings._require_known_relaxation_chip_fields` 가 기동 시점에
        # `FIELD_TO_ATTR` 밖 이름을 거부). 그 결과 `stream_recommendation`(recommendation/graph.py)의
        # `may_auto_relax` 게이트가 False, 자동완화 루프(`if not candidates and not underspecified:`)는
        # 진입해도 후보가 비어 0회 반복, 완화 칩 블록(`if not underspecified and (not candidates or
        # len(candidates) < settings.relaxation_min_results):`)도 진입해도 probe 후보가 비어 칩
        # 0개다. README 「알려진 관측 한계」의 `overspecified_zero` 항목 참조.
        # 그 외(normal)는 `fakes.make_recording_filtering_search()`(대역 카탈로그 `PAIR_CATALOG`)를
        # 써서 search 콜러블이 실제로 받은 필터를 기록한다 — [#426] 대역이 `SearchBackend` 자리에
        # 서므로 Spring 와이어 축(keyword·category·price·brand·color)은 대역이, AI 사후필터 축
        # (rating_min·attr_conditions)은 배포 코드가 실제로 적용한다(`fakes.RecordingFilteringSearch`).
        if degrade == "spring_timeout":
            raw_search = failing_search
        elif axes.get("constraint_strength") == "overspecified_zero":
            raw_search = make_recording_filtering_search(products=[])
        else:
            raw_search = make_recording_filtering_search()
        search_calls: list = []

        async def search(filters, exclude_product_ids=None):
            # search 가 여러 번 불릴 수 있어(무필터 보충 재검색 등) 매 호출을 기록하고 첫 호출(주
            # 검색)만 관측에 쓴다(pair_runner.py 와 같은 규약) — raw_search 가 실패해도(spring_timeout)
            # 호출 자체는 일어났다는 사실과 그때 전달된 filters 는 남긴다.
            search_calls.append(filters)
            return await raw_search(filters, exclude_product_ids=exclude_product_ids)

        push = RecordingPush()

        # `run_buyer_turn` 은 담기 계열 Spring 호출(`add_fn`/`add_wishlist_fn`)을 주입 파라미터로
        # 노출하지 않는다(`app/agents/buyer/graph.py::run_buyer_turn`·`cart/graph.py::stream_cart_add` 가 항상 기본값
        # `spring_client.add_wishlist`/`add_to_cart` 를 씀) — search·order_status_fn 주입과 달리
        # 여기는 모듈 함수를 직접 몽키패치해야 한다(HOME 러너의 `home_svc.get_catalog_store` 패턴과
        # 동일). 패치 없이는 실 네트워크 호출이 나가 환경에 따라 결과가 달라진다(리뷰 R3 관측 중
        # 발견 — 비결정론 원천). cart_add 는 R7(리뷰 라운드 2)에서 같은 공백을 발견해 추가했다 —
        # wishlist_add 만 패치하고 cart_add 는 항상 성공으로 관측되던 채 defined 라벨("Spring 조회
        # 실패를 잡아 degrade 처리")과 어긋나 있었다.
        patch_add_wishlist = axes["intent"] == "wishlist_add" and degrade == "spring_timeout"
        patch_add_to_cart = axes["intent"] == "cart_add" and degrade == "spring_timeout"
        # [#386] 찜 **조회**(I-28)는 degrade 여부와 무관하게 늘 패치한다 — 담기 계열이 실패 주입
        # 때만 패치하면 되는 것과 다르다. `get_wishlist` 는 정상 케이스에서도 호출되는데, 패치하지
        # 않으면 로컬에 Spring 이 없을 때 실 네트워크 호출이 실패해 **degrade=none 케이스가 degrade 를
        # 관측한다**(환경에 따라 결과가 뒤집힌다). wishlist_remove 도 대상 해소용으로 같은 함수를
        # 부르므로 함께 잡는다 — 이 케이스가 이번 재생성으로 ci·degrade=none 조합이 되면서 그
        # 공백이 드러났다.
        patch_wishlist_read = axes["intent"] in ("wishlist_view", "wishlist_remove")
        original_add_wishlist = spring_client.add_wishlist
        original_add_to_cart = spring_client.add_to_cart
        original_get_wishlist = spring_client.get_wishlist
        original_remove_wishlist = spring_client.remove_wishlist
        if patch_add_wishlist:
            spring_client.add_wishlist = _failing_add_wishlist
        if patch_add_to_cart:
            spring_client.add_to_cart = _failing_add_to_cart
        if patch_wishlist_read:
            spring_client.get_wishlist = (
                _failing_get_wishlist if degrade == "spring_timeout" else _ok_get_wishlist
            )
            spring_client.remove_wishlist = _ok_remove_wishlist

        # [#426] attr_conditions 사후필터 계측 — 대역이 흉내 내는 게 아니라(그건 앱 판정 로직
        # 재구현 금지 위반) **배포 함수가 실제로 호출됐는지·무엇을 걸렀는지**를 관측한다(HOME 러너의
        # `profileHookInvoked`/`buildReasonsInvoked` 와 같은 계열, § `_observe_home`).
        # 패치 대상이 `apply_ai_side_filters` 가 아니라 `_apply_attr_conditions` 인 이유 둘:
        #   ① `apply_ai_side_filters` 는 rating_min 만 present 여도 호출돼 신호가 뭉개진다.
        #      `_apply_attr_conditions` 는 `filters.attr_conditions` 가 truthy 일 때만 불린다
        #      (`search_service.py` `if filters.attr_conditions:`) — 호출됨 == 이 축이 평가됨.
        #   ② `recommendation/graph.py` 는 `from ... import apply_ai_side_filters` 로 이름을
        #      바인딩해 가서 `search_service.apply_ai_side_filters` 패치가 안 먹지만,
        #      `_apply_attr_conditions` 는 정의 모듈 전역으로 조회되므로 인기 상품 폴백 경로
        #      (`_run_candidate_source`, search 대역이 아예 개입하지 않는 자리)까지 한 번에 잡힌다.
        attr_post_filter_calls: list[tuple[int, int]] = []
        original_apply_attr_conditions = search_service._apply_attr_conditions

        def _apply_attr_conditions_spy(products, conditions):
            result = original_apply_attr_conditions(products, conditions)
            attr_post_filter_calls.append((len(products), len(result)))
            return result

        search_service._apply_attr_conditions = _apply_attr_conditions_spy
        try:
            events = await _collect(
                run_buyer_turn(
                    request,
                    identity,
                    llm=llm,
                    search=search,
                    push_fn=push,
                    popular_fn=make_popular(),
                    order_status_fn=(
                        failing_order_status
                        if axes["intent"] == "order_status" and degrade == "spring_timeout"
                        else make_order_status_ok
                    ),
                    # #331 소관인 임베딩 최근접·거리컷·택일·확장은 재구현하지 않는다 — raw exact
                    # match 만 대역한다(이슈 #381 D5, `fakes.make_exact_match_category_mapping`).
                    map_categories=make_exact_match_category_mapping(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 안전망: cart_add/wishlist_add 는 CartError/
            # WishlistError(+ SpringUnavailableError)를 개별 처리한다(#368/PR #374 로 해소, cart/
            # graph.py:454 · cart/wishlist.py:245) — 정상 경로라면 여기 도달하지 않는다. 그래도
            # 다른 축·미래 변경이 실제로 처리 안 된 예외를 내면(회귀) 관측 데이터에 조용히 사라지지
            # 않고 남기려고 이 블록을 유지한다. 프로덕션에서는 core/stream.py 의 open_stream 범용
            # catch-all(:688-705)이 error(INTERNAL) SSE 로 감싼다(통합 레벨, 이 하네스 범위 밖).
            return {
                "unhandledException": type(exc).__name__,
                "note": (
                    "미처리 예외 안전망 — 정상 경로라면 cart_add/wishlist_add 의 개별 except 가 이미 "
                    "잡아 action(*_FAILED)+done 으로 degrade 한다(#368/PR #374). 여기 도달했다면 그 "
                    "처리 범위 밖의 예외가 run_buyer_turn 을 직접 호출한 이 unit 경계에서 그대로 "
                    "전파된 것이다 — 관측 데이터에 남겨 회귀를 드러낸다."
                ),
            }
        finally:
            if patch_add_wishlist:
                spring_client.add_wishlist = original_add_wishlist
            if patch_add_to_cart:
                spring_client.add_to_cart = original_add_to_cart
            if patch_wishlist_read:
                spring_client.get_wishlist = original_get_wishlist
                spring_client.remove_wishlist = original_remove_wishlist
            search_service._apply_attr_conditions = original_apply_attr_conditions
        event_types = [e["type"] for e in events]
        terminal = events[-1] if events else None
        # action/token 이벤트는 degrade 결과(CART_ADD_FAILED reason 등)를 실어 나르는데, 예전엔
        # `errorCode`(SSE `error` 전용)만 봐서 action 계열 degrade 결과가 observed 에 전혀 안
        # 남았다 — R7/R8 실측을 데이터에도 남기려면 action 의 type·reason·message, token 의 text 도
        # 있어야 한다("성공/실패"를 observed 만 보고 판단할 수 있어야 한다).
        action_event = next((e for e in events if e["type"] == "action"), None)
        token_events = [e for e in events if e["type"] == "token"]
        observed = {
            "eventTypes": event_types,
            "terminal": terminal["type"] if terminal else None,
            "finishReason": (
                terminal["data"].get("finishReason")
                if terminal and terminal["type"] == "done"
                else None
            ),
            "errorCode": (
                terminal["data"].get("code") if terminal and terminal["type"] == "error" else None
            ),
            "actionType": action_event["data"].get("type") if action_event else None,
            "actionReason": action_event["data"].get("reason") if action_event else None,
            "lastTokenText": token_events[-1]["data"].get("text") if token_events else None,
            "pushCount": len(push.pushes),
            "listType": push.pushes[0].list_type if push.pushes else None,
            # pushCount 는 push **이벤트** 수다 — 필터가 결과를 실제로 줄였다는 가시적 결과를 재려면
            # push 된 **상품** 수가 필요하다(이슈 #381 D3, pushCount 와 혼동 금지).
            "pushProductCount": sum(len(entry.product_ids) for p in push.pushes for entry in p.lists),
            # search 가 아예 안 불린 케이스(popular 경로·담기 계열 등)는 0 — "안 불렸다"와 "전부
            # null 이었다"를 구별한다(이슈 #381 D3). 첫 호출(주 검색)만 searchFilters 에 싣는다.
            "searchCallCount": len(search_calls),
            "searchFilters": (search_filters_projection(search_calls[0]) if search_calls else None),
            "unappliedSearchFilters": (
                raw_search.unapplied_calls[0]
                if isinstance(raw_search, RecordingFilteringSearch) and raw_search.unapplied_calls
                else []
            ),
        }
        # [#426] attr_conditions 축이 present 인 케이스에만 싣는다 — 그 축이 없는 행에까지 필드를
        # 늘리면 `refresh-observed` diff 가 전 행으로 번져 케이스별 사람 판정이 불가능해진다.
        # 첫 호출(주 검색/주 후보 확보) 기준 — `searchFilters`·`unappliedSearchFilters` 와 같은 규약.
        # `invoked=true, input==output`(호출됐지만 안 걸렀다)과 `invoked=false`(아예 안 돌았다)를
        # 데이터에서 구별한다.
        if axes.get("attr_conditions") == "present":
            first_call = attr_post_filter_calls[0] if attr_post_filter_calls else None
            observed["attrConditionsPostFilter"] = {
                "invoked": first_call is not None,
                "inputCount": first_call[0] if first_call else 0,
                "outputCount": first_call[1] if first_call else 0,
            }
        # 여러 관측 한계가 동시에 해당할 수 있다(예: combo-0038 unspecified+embedding_missing) —
        # 단일 `note` 덮어쓰기는 먼저 붙인 한계를 지운다(리뷰 R4). 리스트로 전부 append.
        notes: list[str] = []
        if degrade == "embedding_missing":
            notes.append(
                "search 주입 경계는 search_service.py 임베딩 재정렬보다 상류 — degrade=none 과 "
                "동일하게 실행됨(runner.py 모듈 docstring 참조)"
            )
        if (
            axes["intent"] in ("wishlist_add", "wishlist_remove", "wishlist_view")
            and axes["identity"] == "guest"
        ):
            notes.append(
                "identity=guest 는 로그인 필요 게이트가 Spring 호출보다 먼저 걸려 이 케이스는 "
                "SpringUnavailableError 미처리 갭(expected_behavior.status=partial 근거)을 실제로는 "
                "밟지 않는다 — 그 갭은 identity=member 조합에서만 실측된다(README 리스크 참조)."
            )
        if axes.get("constraint_strength") == "overspecified_zero" and degrade != "spring_timeout":
            notes.append(
                "0건은 주입이며, 이 케이스에 present 인 필터축(price_min)이 완화 축이 아니라 "
                "자동완화·완화칩은 돌지 않는다(#425 판정: 정의된 동작)"
            )
        if axes.get("constraint_strength") == "unspecified" and degrade != "none":
            notes.append(
                "조건 없는 턴은 후보 소스가 popular_fn(I-3) 이라(graph.py:797-830) 이 하네스의 "
                "popular_fn fake 가 항상 성공해 search/rerank degrade 축은 관측되지 않는다 — "
                "popular_fn 실패 시에만 _run_search() 로 폴백한다(코드 정의 동작, 갭 아님)."
            )
        if axes["intent"] in ("cart_add", "wishlist_add"):
            # 리뷰 R9 — 이 케이스의 context 축은 none(decompose 입력 관점, 되묻기 컨텍스트 없음)
            # 이지만, 담기 허용목록 게이트를 통과시키려고 세션 스토어엔 웜업 턴의 직전 추천이 이미
            # 있다(`_warm_up_last_reco`) — 이 전제가 observed 데이터만 보면 안 드러나므로 명시한다.
            notes.append(
                "웜업으로 last_reco 채움(productId 101) — context 축은 decompose 입력 관점(none)이고 "
                "세션 스토어 상태와는 별개다(runner.py::_warm_up_last_reco)."
            )
        if notes:
            observed["notes"] = notes
    finally:
        _settings_for_case.category_leg_injection_enabled = _prev_injection
    return observed


async def _observe_home(case: ComboCase) -> dict:
    axes = case.axes
    degrade = axes["degrade"]

    from app.core.config import get_settings
    from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
    from app.schemas.recommendations import HomeRecommendationRequest, HomeRecommendationSignals

    profile_hook_calls = 0
    build_reasons_calls = 0

    async def _no_profile(user_id: str | None) -> dict | None:
        nonlocal profile_hook_calls
        profile_hook_calls += 1
        return None

    async def _profile_unavailable(user_id: str | None) -> dict | None:
        nonlocal profile_hook_calls
        profile_hook_calls += 1
        raise RuntimeError("profile store 조회 실패(주입, #367 profile_unavailable)")

    original_build_reasons = home_svc.build_reasons

    def _build_reasons_unavailable(*args, **kwargs):
        nonlocal build_reasons_calls
        build_reasons_calls += 1
        raise RuntimeError("reason 재료 조회 실패(주입, #367 reason_degraded)")

    def _build_reasons_spy(*args, **kwargs):
        # 주입이 없는 대응 케이스(none/profile_unavailable)에서도 실제 build_reasons 가
        # 도달했는지 계측해야 reason_degraded 와의 대비가 성립한다(리뷰 F1).
        nonlocal build_reasons_calls
        build_reasons_calls += 1
        return original_build_reasons(*args, **kwargs)

    if degrade == "catalog_unavailable":

        class _UnavailableStore:
            def get_many(self, product_ids):
                raise RuntimeError("catalog index 조회 실패(주입)")

            def top_k_by_vector(self, query_vec, *, k, exclude=None, include=None):
                raise RuntimeError("catalog index 조회 실패(주입)")

        target_store = _UnavailableStore()
    elif degrade == "catalog_timeout":

        class _TimeoutStore:
            def get_many(self, product_ids):
                raise TimeoutError("catalog index 조회 시간 초과(주입)")

            def top_k_by_vector(self, query_vec, *, k, exclude=None, include=None):
                raise TimeoutError("catalog index 조회 시간 초과(주입)")

        target_store = _TimeoutStore()
    else:
        # `home_reco_min_candidates`(기본 5) 미만이면 rank_home 이 INSUFFICIENT_CANDIDATES 로
        # 조기 반환해 reason 관측 경로에 영원히 도달하지 못한다(리뷰 F1) — 후보 수를 설정값에서
        # 유도해 기본값이 바뀌어도 조용히 다시 공허해지지 않게 한다.
        min_candidates = get_settings().home_reco_min_candidates
        store = CatalogArtifactStore()
        store.upsert(
            CatalogArtifact(
                product_id=101,
                embedding=[1.0, 0.0, 0.0],
                search_doc="이어폰",
                extras={"situation_tags": ["출퇴근", "운동"]},
            )
        )
        store.upsert(
            CatalogArtifact(
                product_id=102,
                embedding=[0.0, 1.0, 0.0],
                search_doc="가방",
                # cart 시그널(101)과 situation_tags 가 겹치게 해 build_reasons 가 실제로 문장을
                # 고를 재료를 준다 — 그래야 "주입 없음 → reason 채워짐"의 대비가 성립한다.
                extras={"situation_tags": ["출퇴근"]},
            )
        )
        next_pid = 103
        while store.count() < min_candidates:
            store.upsert(
                CatalogArtifact(
                    product_id=next_pid,
                    embedding=[0.0, 0.0, 1.0],
                    search_doc=f"상품{next_pid}",
                )
            )
            next_pid += 1
        target_store = store

    original_get_store = home_svc.get_catalog_store
    original_read_profile = home_svc.read_profile_summary
    home_svc.get_catalog_store = lambda: target_store
    home_svc.read_profile_summary = (
        _profile_unavailable if degrade == "profile_unavailable" else _no_profile
    )
    home_svc.build_reasons = (
        _build_reasons_unavailable if degrade == "reason_degraded" else _build_reasons_spy
    )
    try:
        # cart_product_ids 를 채워야 build_query_vector 가 실제로 store.get_many 를 호출한다 —
        # 신호가 전부 비면 프로필도 없을 때 조회 자체를 생략하고 NO_PROFILE 로 조기 반환해
        # catalog_timeout/catalog_unavailable(카탈로그 조회 실패) 축이 관측되지 않는다.
        request = HomeRecommendationRequest.model_validate(
            {
                "memberId": 1,
                "limit": 5,
                "signals": HomeRecommendationSignals.model_validate(
                    {"cartProductIds": [101]}
                ).model_dump(by_alias=True),
            }
        )
        try:
            response = await home_svc.rank_home(request, request_id="combo-matrix-eval")
            observed = {"outcome": response.outcome, "itemCount": len(response.items)}
            # 주입이 실제로 실행됐는지를 관측에 남긴다 — profile_unavailable 은 outcome 만으로
            # none 과 구별되지 않는다(계약상 와이어 구별 신호 없음, api-spec §3.7 v0.26.1).
            observed["profileHookInvoked"] = profile_hook_calls > 0
            observed["buildReasonsInvoked"] = build_reasons_calls > 0
            filled = sum(1 for item in response.items if item.reason is not None)
            observed["reasonsFilledCount"] = filled
            # items 가 0건이면 vacuous truth(all([]) == True)라 절대 참이 되지 않게 막는다.
            observed["reasonsNull"] = bool(response.items) and filled == 0
            return observed
        except Exception as exc:  # noqa: BLE001 - degrade=catalog_* 기대 경로(Upstream*)
            return {
                "exception": type(exc).__name__,
                "statusCode": getattr(exc, "status_code", None),
            }
    finally:
        home_svc.get_catalog_store = original_get_store
        home_svc.read_profile_summary = original_read_profile
        home_svc.build_reasons = original_build_reasons


async def observe(case: ComboCase) -> dict | None:
    """`ci` 케이스 1건을 실행해 관측 결과를 돌려준다. `manual` 케이스는 `None`(호출 측이 건너뛴다)."""
    if case.observation_mode != "ci":
        return None
    if case.axes["surface"] == "HOME":
        return await _observe_home(case)
    return await _observe_chat(case)
