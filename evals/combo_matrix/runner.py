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
- **HOME(I-22) 의 `embedding_missing`·`rerank_failed` degrade 는 실행하지 않는다** — 대응하는
  코드 경로가 없다(`expected_behavior.jsonl` status=undefined 근거, README 리스크 참조). `none`·
  `spring_timeout`(카탈로그 인덱스 조회 타임아웃으로 근사) 만 실행한다.
- **카테고리 leg fan-out(`category_queries`/`map_categories`)은 범위 밖** — `category` 필터축은
  `ProductSearchFilters.category`(하드필터 문자열) 만 재며, leg 분해·매핑은 관측하지 않는다.
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
from app.services import spring_client  # noqa: E402
from app.services.spring_client import SpringUnavailableError  # noqa: E402
from evals.combo_matrix.fakes import (  # noqa: E402
    RecordingPush,
    failing_order_status,
    failing_search,
    make_order_status_ok,
    make_popular,
    make_search,
    map_categories_noop,
)
from evals.combo_matrix.generator import FILTER_AXES  # noqa: E402
from evals.combo_matrix.schema import ComboCase  # noqa: E402
from tests.integration._stubs import ScriptedLLM  # noqa: E402
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
    """cart_add/wishlist_add 의 담기 허용목록(§3.1 [보안])에 productId 101 을 올려 둔다.

    두 인텐트는 `allowed_product_ids`(직전 추천 ∪ screen.products) 밖 상품을 조용히 차단하고
    되물음으로 돌린다(`app/agents/buyer/graph.py:994-1019`) — 이 하네스가 last_reco 를 채우지
    않으면 degrade(SpringUnavailableError 등)를 주입해도 그 코드에 절대 도달하지 못하고 매번
    "추천을 먼저 받아보세요" 되물음에서 끝난다(§ 리뷰 R3 관측 중 발견). 같은 thread_id 로 정상
    recommend 턴을 1회 먼저 태워 last_reco 를 채운 뒤 실제 관측 턴을 돌린다 — `get_cart_store()`
    가 공유 백엔드를 감싸므로(cart/state.py:234-236) 두 턴 사이에 상태가 이어진다.
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
            search=make_search(),
            push_fn=RecordingPush(),
            popular_fn=make_popular(),
            order_status_fn=make_order_status_ok,
            map_categories=map_categories_noop,
        )
    )


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
    # search 실패(spring_timeout)가 최우선 — 검색 자체가 안 되면 결과 건수는 의미가 없다.
    # 그다음 constraint_strength=overspecified_zero 는 **정의상 검색 0건**이어야 자동완화·
    # 완화칩·zero_result 종료(recommendation/graph.py:1075-1115·:1146)가 실제로 돈다 — fake 가
    # 항상 3건을 내면 이 축이 normal 과 구별 없이 실행되는 공회전이었다(리뷰 R1).
    if degrade == "spring_timeout":
        search = failing_search
    elif axes.get("constraint_strength") == "overspecified_zero":
        search = make_search([])
    else:
        search = make_search()
    push = RecordingPush()

    # `run_buyer_turn` 은 담기 계열 Spring 호출(`add_fn`/`add_wishlist_fn`)을 주입 파라미터로
    # 노출하지 않는다(`app/agents/buyer/graph.py:1099`·`cart/graph.py:275` 가 항상 기본값
    # `spring_client.add_wishlist`/`add_to_cart` 를 씀) — search·order_status_fn 주입과 달리
    # 여기는 모듈 함수를 직접 몽키패치해야 한다(HOME 러너의 `home_svc.get_catalog_store` 패턴과
    # 동일). 패치 없이는 실 네트워크 호출이 나가 환경에 따라 결과가 달라진다(리뷰 R3 관측 중
    # 발견 — 비결정론 원천). cart_add 는 R7(리뷰 라운드 2)에서 같은 공백을 발견해 추가했다 —
    # wishlist_add 만 패치하고 cart_add 는 항상 성공으로 관측되던 채 defined 라벨("Spring 조회
    # 실패를 잡아 degrade 처리")과 어긋나 있었다.
    patch_add_wishlist = axes["intent"] == "wishlist_add" and degrade == "spring_timeout"
    patch_add_to_cart = axes["intent"] == "cart_add" and degrade == "spring_timeout"
    original_add_wishlist = spring_client.add_wishlist
    original_add_to_cart = spring_client.add_to_cart
    if patch_add_wishlist:

        async def _failing_add_wishlist(*_args, **_kwargs):
            raise SpringUnavailableError("wishlist 담기 시간 초과(주입)")

        spring_client.add_wishlist = _failing_add_wishlist
    if patch_add_to_cart:

        async def _failing_add_to_cart(*_args, **_kwargs):
            raise SpringUnavailableError("장바구니 담기 시간 초과(주입)")

        spring_client.add_to_cart = _failing_add_to_cart
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
                map_categories=map_categories_noop,
            )
        )
    except Exception as exc:  # noqa: BLE001 - stream_wishlist_add 가 SpringUnavailableError 를
        # 개별 처리하지 않아(발견 3번, cart/wishlist.py:175-233) 여기서 그대로 새어나온다 — 이
        # 유닛 경계(run_buyer_turn 직접 호출)엔 프로덕션의 open_stream 범용 catch-all
        # (core/stream.py:688-705)이 없어 진짜로 처리 안 된 예외를 그대로 보여준다. 그 catch-all
        # 이 error(INTERNAL)로 감싸는 것은 통합(SSE 스트림) 레벨 동작이라 이 하네스 범위 밖이다.
        return {
            "unhandledException": type(exc).__name__,
            "note": (
                "stream_wishlist_add 가 이 예외를 개별 처리하지 않아 run_buyer_turn 을 직접 호출한 "
                "이 경계(unit)에서 그대로 전파됐다 — 프로덕션에서는 core/stream.py 의 open_stream "
                "범용 catch-all이 error(INTERNAL) SSE 로 감싼다(통합 레벨, 이 하네스 범위 밖). "
                "이 unhandledException 자체가 발견 3번의 직접 증거다."
            ),
        }
    finally:
        if patch_add_wishlist:
            spring_client.add_wishlist = original_add_wishlist
        if patch_add_to_cart:
            spring_client.add_to_cart = original_add_to_cart
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
    }
    # 여러 관측 한계가 동시에 해당할 수 있다(예: combo-0038 unspecified+embedding_missing) —
    # 단일 `note` 덮어쓰기는 먼저 붙인 한계를 지운다(리뷰 R4). 리스트로 전부 append.
    notes: list[str] = []
    if degrade == "embedding_missing":
        notes.append(
            "search 주입 경계는 search_service.py 임베딩 재정렬보다 상류 — degrade=none 과 "
            "동일하게 실행됨(runner.py 모듈 docstring 참조)"
        )
    if axes["intent"] in ("wishlist_add", "wishlist_remove") and axes["identity"] == "guest":
        notes.append(
            "identity=guest 는 로그인 필요 게이트가 Spring 호출보다 먼저 걸려 이 케이스는 "
            "SpringUnavailableError 미처리 갭(expected_behavior.status=partial 근거)을 실제로는 "
            "밟지 않는다 — 그 갭은 identity=member 조합에서만 실측된다(README 리스크 참조)."
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
    return observed


async def _observe_home(case: ComboCase) -> dict:
    axes = case.axes
    degrade = axes["degrade"]
    if degrade in ("embedding_missing", "rerank_failed"):
        return {
            "note": "HOME(I-22) 엔 대응 코드 경로 없음 — 실행 생략(expected_behavior.status=undefined)"
        }

    from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
    from app.schemas.recommendations import HomeRecommendationRequest, HomeRecommendationSignals

    async def _no_profile(user_id: str | None) -> dict | None:
        return None

    if degrade == "spring_timeout":

        class _TimeoutStore:
            def get_many(self, product_ids):
                raise TimeoutError("catalog index 조회 시간 초과(주입)")

            def top_k_by_vector(self, query_vec, *, k, exclude=None, include=None):
                raise TimeoutError("catalog index 조회 시간 초과(주입)")

        target_store = _TimeoutStore()
    else:
        store = CatalogArtifactStore()
        store.upsert(
            CatalogArtifact(product_id=101, embedding=[1.0, 0.0, 0.0], search_doc="이어폰")
        )
        store.upsert(CatalogArtifact(product_id=102, embedding=[0.0, 1.0, 0.0], search_doc="가방"))
        target_store = store

    original_get_store = home_svc.get_catalog_store
    original_read_profile = home_svc.read_profile_summary
    home_svc.get_catalog_store = lambda: target_store
    home_svc.read_profile_summary = _no_profile
    try:
        # cart_product_ids 를 채워야 build_query_vector 가 실제로 store.get_many 를 호출한다 —
        # 신호가 전부 비면 프로필도 없을 때 조회 자체를 생략하고 NO_PROFILE 로 조기 반환해
        # spring_timeout(카탈로그 조회 타임아웃) 축이 관측되지 않는다.
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
            response = await home_svc.rank_home(request)
            return {"outcome": response.outcome, "itemCount": len(response.items)}
        except Exception as exc:  # noqa: BLE001 - degrade=spring_timeout 기대 경로(Upstream*)
            return {
                "exception": type(exc).__name__,
                "statusCode": getattr(exc, "status_code", None),
            }
    finally:
        home_svc.get_catalog_store = original_get_store
        home_svc.read_profile_summary = original_read_profile


async def observe(case: ComboCase) -> dict | None:
    """`ci` 케이스 1건을 실행해 관측 결과를 돌려준다. `manual` 케이스는 `None`(호출 측이 건너뛴다)."""
    if case.observation_mode != "ci":
        return None
    if case.axes["surface"] == "HOME":
        return await _observe_home(case)
    return await _observe_chat(case)
