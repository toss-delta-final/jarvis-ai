"""I-22 홈 추천 랭킹 계약 테스트 (api-spec §3.7, 이슈 #148).

라이브 환경(Spring·임베딩 API·Anthropic 키)이 없는 상태에서 계약 표면을 고정한다 —
카탈로그 인덱스는 인메모리 CatalogArtifactStore 픽스처로, reason LLM 은 FakeLLM 으로 주입한다.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import Settings, get_settings
from app.main import app
from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
from app.services import home_recommendation as svc

client = TestClient(app)

_URL = "/internal/recommendations/home"


def _artifact(product_id: int, embedding: list[float], *, doc: str = "") -> CatalogArtifact:
    return CatalogArtifact(
        product_id=product_id,
        search_doc=doc or f"상품 {product_id}",
        embedding=embedding,
        extras={},
    )


@pytest.fixture
def store() -> CatalogArtifactStore:
    """3차원 임베딩 카탈로그 10건 — 축이 분명해 랭킹 순서를 눈으로 검증할 수 있다."""
    s = CatalogArtifactStore()
    # 1001 이 [1,0,0] 에 가장 가깝고 번호가 커질수록 멀어진다. 어느 것도 축(=시그널 상품)과
    # 완전히 겹치지 않게 0.05 씩 띄운다 — 겹치면 동점 tiebreak 이 순서를 지배해 의도가 흐려진다.
    for i in range(10):
        pid = 1001 + i
        s.upsert(_artifact(pid, [1.0 - (i + 1) * 0.05, (i + 1) * 0.05, 0.0]))
    # 시그널로 쓸 상품(카탈로그에도 존재) — [1,0,0] 축
    s.upsert(_artifact(9001, [1.0, 0.0, 0.0], doc="시그널 상품 A"))
    return s


@pytest.fixture(autouse=True)
def _inject(monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore):
    """전역 pg 스토어를 인메모리로 대체한다 (유닛 테스트는 실 DB·네트워크 금지).

    reason 은 요청 경로에서 LLM 을 부르지 않으므로 주입할 LLM 이 없다 — 미리 만들어 둔 `extras`
    재료를 읽을 뿐이다(#148 실측으로 LLM 경로 폐기).
    """
    monkeypatch.setattr(svc, "get_catalog_store", lambda: store)
    monkeypatch.setattr(svc, "read_profile_summary", _no_profile)
    yield


async def _no_profile(user_id: str | None) -> dict | None:
    return None


def _body(**over) -> dict:
    body = {
        "memberId": 123,
        "limit": 5,
        "catalogVersion": "catalog-20260728T0300Z",
        "signals": {
            "recentlyViewedProductIds": [9001],
            "cartProductIds": [],
            "recentPurchasedProductIds": [],
        },
    }
    body.update(over)
    return body


# ── 성공 경로 · 계약 표면 ──


def test_personalized_returns_camel_case_contract_surface() -> None:
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"outcome", "recommendationRequestId", "listId", "items"}
    assert data["outcome"] == "PERSONALIZED"
    assert data["items"], "개인화 성공이면 items 가 비어있지 않다"
    assert set(data["items"][0]) == {"productId", "reason"}
    assert isinstance(data["items"][0]["productId"], int)


def test_opted_out_member_gets_no_profile_even_with_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**중지 회원은 시그널이 있어도 `NO_PROFILE`** (api-spec §3.7 v0.32.7·§3.9.5, #359).

    v0.22.0 은 근거를 *"프로필 벡터 항을 빼면 개인화 근거가 남지 않으므로 기존 판정 기준에 그대로
    걸린다"* 로 적었는데 성립하지 않는다 — 이 요청은 `recentlyViewedProductIds` 가 있어 그
    임베딩만으로 질의 벡터가 만들어지고, 항만 빼면 `PERSONALIZED` 가 나간다. 중지는 판정 기준의
    **결과가 아니라 그보다 앞서는 단락**이다.

    같은 요청이 중지 없이는 `PERSONALIZED` 라는 것은 위
    `test_personalized_returns_camel_case_contract_surface` 가 이미 고정한다 — 그래서 이 테스트가
    공허하지 않다.
    """

    async def _disabled(user_id, *, on_error):  # noqa: ANN001
        return False

    monkeypatch.setattr(svc, "personalization_enabled", _disabled)

    r = client.post(_URL, json=_body())

    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "NO_PROFILE"
    assert data["items"] == []


def test_unknown_personalization_state_drops_the_profile_but_keeps_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 판정 불가는 **degrade** 다 — 프로필 항만 빠지고 랭킹은 계속한다.

    플래그도 pg-profile 에 있으므로 그 조회 실패는 api-spec §3.7 「HOME 실패 모드」의
    `profile_unavailable`(200 · 프로필 항만 빠짐 · 남은 근거로 판정)에 해당한다. `False` 로
    접어 `NO_PROFILE` 을 강제하면 그 표와 충돌한다 — 소비 fail-closed 는 "프로필을 쓰지
    않는다" 로 실현되고, 시그널까지 버릴 근거는 중지가 **확인됐을 때**뿐이다.
    """

    async def _unknown(user_id, *, on_error):  # noqa: ANN001
        return None

    monkeypatch.setattr(svc, "personalization_enabled", _unknown)

    r = client.post(_URL, json=_body())

    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"  # 시그널로 계속 랭킹한다


def test_items_order_is_the_ranking_and_carries_no_position() -> None:
    """배열 순서가 곧 순위 — position 을 싣지 않는다(§3.7)."""
    r = client.post(_URL, json=_body(limit=10))
    items = r.json()["items"]
    ids = [i["productId"] for i in items]
    assert "position" not in items[0]
    # 시그널이 [1,0,0] 이므로 같은 축의 9001 → 1001 → 1002 … 순
    assert ids[0] == 9001
    assert ids[1] == 1001
    assert ids == sorted(ids, key=lambda p: (p != 9001, p))


def test_returns_more_than_limit_for_spring_stock_drop() -> None:
    """limit 은 최종 노출 목표치 — 품절 드롭 대비해 넉넉히 반환하고 Spring 이 자른다(§3.7)."""
    r = client.post(_URL, json=_body(limit=3))
    items = r.json()["items"]
    assert len(items) > 3


def test_recent_purchased_ids_are_excluded_not_weighted() -> None:
    """recentPurchasedProductIds 는 가중치가 아니라 제외 필터다(§3.7)."""
    signals = {
        "recentlyViewedProductIds": [9001],
        "cartProductIds": [],
        "recentPurchasedProductIds": [1001, 1002],
    }
    r = client.post(_URL, json=_body(limit=10, signals=signals))
    ids = [i["productId"] for i in r.json()["items"]]
    assert 1001 not in ids
    assert 1002 not in ids
    assert 1003 in ids


def test_cart_signal_outweighs_recently_viewed(store: CatalogArtifactStore) -> None:
    """담기까지 갔다는 건 강한 신호 — cart 가중치가 조회보다 높다(§3.7).

    두 시그널의 역할을 서로 바꿔 두 번 돌린다. 카탈로그 기하가 아니라 **가중치**가 순서를 정한다는
    것을 보이려면 한 방향 단언만으로는 부족하다 — 역할을 swap 했을 때 순서도 뒤집혀야 한다.
    """
    store.upsert(_artifact(9002, [0.0, 1.0, 0.0], doc="시그널 상품 B"))

    def order(cart: list[int], viewed: list[int]) -> tuple[int, int]:
        signals = {
            "recentlyViewedProductIds": viewed,
            "cartProductIds": cart,
            "recentPurchasedProductIds": [],
        }
        ids = [
            i["productId"]
            for i in client.post(_URL, json=_body(limit=10, signals=signals)).json()["items"]
        ]
        return ids.index(9001), ids.index(9002)

    viewed_rank, cart_rank = order(cart=[9002], viewed=[9001])
    assert cart_rank < viewed_rank, "cart 축 시그널이 조회 축보다 앞선다"

    viewed_rank, cart_rank = order(cart=[9001], viewed=[9002])
    assert viewed_rank < cart_rank, "역할을 바꾸면 순서도 뒤집힌다(기하가 아니라 가중치가 원인)"


# ── 장기 취향(프로필 벡터)이 랭킹에 반영된다 (#148) ──


def _profile_with(vec: list[float] | None, markdown: str = "# 취향"):
    async def _read(user_id: str | None) -> dict | None:
        return {"markdown": markdown, "generated_at": "2026-07-31T00:00:00Z", "embedding": vec}

    return _read


def test_profile_vector_alone_personalizes_without_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """시그널이 비어도 프로필만으로 질의 벡터가 선다 — 홈 첫 방문 회원도 대화 취향으로 개인화된다.

    프로필 벡터 도입 전에는 이 경우가 무조건 NO_PROFILE 이었다.
    """
    monkeypatch.setattr(svc, "read_profile_summary", _profile_with([0.0, 1.0, 0.0]))
    signals = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    data = client.post(_URL, json=_body(limit=10, signals=signals)).json()
    assert data["outcome"] == "PERSONALIZED"
    ids = [i["productId"] for i in data["items"]]
    # [0,1,0] 축에 가장 가까운 1010(=[0.5,0.5,0]) 쪽이 앞선다
    assert ids.index(1010) < ids.index(1001)


def test_profile_vector_shifts_ranking_when_signals_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필이 시그널과 다른 축을 가리키면 순위가 그쪽으로 당겨진다."""

    def ranking(profile_vec: list[float] | None) -> list[int]:
        monkeypatch.setattr(svc, "read_profile_summary", _profile_with(profile_vec))
        return [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]

    without = ranking(None)
    with_profile = ranking([0.0, 1.0, 0.0])  # 시그널([1,0,0])과 직교
    assert without != with_profile, "프로필 벡터가 랭킹을 바꿔야 한다"
    assert with_profile.index(1010) < without.index(1010), "프로필 축 상품이 위로 올라온다"


def test_missing_profile_embedding_degrades_to_signals_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구 요약·임베딩 실패분(embedding=None)은 그 항만 빠지고 나머지는 그대로 동작한다."""
    monkeypatch.setattr(svc, "read_profile_summary", _profile_with(None))
    r = client.post(_URL, json=_body(limit=10))
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"


def test_profile_vector_dimension_mismatch_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """차원이 다른 벡터(모델 교체·마이그레이션 중)는 조용히 섞지 않는다."""
    monkeypatch.setattr(svc, "read_profile_summary", _profile_with([0.1] * 7))
    baseline_ids = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    monkeypatch.setattr(svc, "read_profile_summary", _no_profile)
    plain_ids = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    assert baseline_ids == plain_ids


def test_profile_weight_zero_with_no_signals_is_no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """가중치 0 + 시그널 없음 = 개인화 근거 0 → NO_PROFILE.

    가중치 0 을 빈 `acc` 에 곱하면 길이만 있는 **0 벡터**가 생겨 `if not query_vec` 를 통과한다.
    그대로 두면 개인화 근거가 없는데도 PERSONALIZED 로 응답하면서, 실제로는 productId 오름차순에
    불과한 순서를 "개인화"로 내보낸다(pg 는 거리 동점, 인메모리는 코사인 -1).
    """
    monkeypatch.setattr(svc, "read_profile_summary", _profile_with([0.0, 1.0, 0.0]))
    zeroed = get_settings().model_copy(update={"home_reco_weight_profile": 0.0})
    monkeypatch.setattr(svc, "get_settings", lambda: zeroed)
    signals = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    data = client.post(_URL, json=_body(signals=signals)).json()
    assert data["outcome"] == "NO_PROFILE"
    assert data["items"] == []


def test_all_zero_signal_embeddings_are_not_a_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """시그널 임베딩이 전부 0 이어도 마찬가지다 — 0 벡터는 질의가 아니다."""
    zeros = CatalogArtifactStore()
    for pid in (9001, *range(1001, 1011)):
        zeros.upsert(_artifact(pid, [0.0, 0.0, 0.0]))
    monkeypatch.setattr(svc, "get_catalog_store", lambda: zeros)
    assert client.post(_URL, json=_body()).json()["outcome"] == "NO_PROFILE"


def test_profile_weight_zero_removes_it_from_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """가중치 0 = 롤백 스위치 — 프로필이 랭킹에서 빠지고 reason 근거로만 남는다."""
    monkeypatch.setattr(svc, "read_profile_summary", _profile_with([0.0, 1.0, 0.0]))
    with_weight = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]

    base = get_settings()
    zeroed = base.model_copy(update={"home_reco_weight_profile": 0.0})
    monkeypatch.setattr(svc, "get_settings", lambda: zeroed)
    without = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    assert with_weight != without


# ── outcome 3종은 모두 200 (cold start 는 오류가 아니다) ──


def test_no_profile_is_200_not_an_error() -> None:
    """시그널이 비어 개인화 근거가 없으면 NO_PROFILE + 200 — fallback 판단은 Spring 이 한다."""
    signals = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    r = client.post(_URL, json=_body(signals=signals))
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "NO_PROFILE"
    assert data["items"] == []


def test_signals_with_no_indexed_embedding_is_no_profile() -> None:
    """시그널 상품이 인덱스에 없으면 질의 벡터를 못 만든다 → 개인화 근거 없음."""
    signals = {
        "recentlyViewedProductIds": [777777],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    r = client.post(_URL, json=_body(signals=signals))
    assert r.status_code == 200
    assert r.json()["outcome"] == "NO_PROFILE"


def test_insufficient_candidates_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """후보가 부족하면 INSUFFICIENT_CANDIDATES + 200."""
    thin = CatalogArtifactStore()
    thin.upsert(_artifact(9001, [1.0, 0.0, 0.0]))
    thin.upsert(_artifact(1001, [0.9, 0.1, 0.0]))
    monkeypatch.setattr(svc, "get_catalog_store", lambda: thin)
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "INSUFFICIENT_CANDIDATES"
    assert data["items"] == []


def test_all_outcomes_are_200_never_4xx_or_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 부재·후보 부족으로 AI 가 4xx/5xx 를 내면 계약 위반이다(§3.7 [HARD])."""
    empty = CatalogArtifactStore()
    monkeypatch.setattr(svc, "get_catalog_store", lambda: empty)
    for signals in (
        {"recentlyViewedProductIds": [], "cartProductIds": [], "recentPurchasedProductIds": []},
        {"recentlyViewedProductIds": [9001], "cartProductIds": [], "recentPurchasedProductIds": []},
    ):
        r = client.post(_URL, json=_body(signals=signals))
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] in {"NO_PROFILE", "INSUFFICIENT_CANDIDATES"}


# ── 식별자 규약 ──


def test_list_id_is_at_least_128bit_random_hex() -> None:
    """listId 는 ≥128bit 무작위 — 순번·타임스탬프 등 추측 가능한 형식 금지(I-21 과 동일 규칙)."""
    list_id = client.post(_URL, json=_body()).json()["listId"]
    assert len(list_id) == 32
    assert all(c in "0123456789abcdef" for c in list_id)


def test_not_idempotent_new_ids_per_call() -> None:
    """재시도하면 새 recommendationRequestId·listId 가 발급된다(멱등 아님, §3.7)."""
    first = client.post(_URL, json=_body()).json()
    second = client.post(_URL, json=_body()).json()
    assert first["listId"] != second["listId"]
    assert first["recommendationRequestId"] != second["recommendationRequestId"]


def test_same_snapshot_and_config_yield_same_ranking() -> None:
    """동일 snapshot·config 입력은 동일 ranking (완료조건: provenance 복원 가능)."""
    a = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    b = [i["productId"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]]
    assert a == b


def test_ranking_is_stable_regardless_of_store_iteration_order() -> None:
    """동점·저장 순서가 달라도 순위가 흔들리지 않는다 — productId 로 결정적 tiebreak."""
    forward = CatalogArtifactStore()
    reverse = CatalogArtifactStore()
    tied = [(2001, [1.0, 0.0, 0.0]), (2002, [1.0, 0.0, 0.0]), (2003, [1.0, 0.0, 0.0])]
    for pid, vec in tied:
        forward.upsert(_artifact(pid, vec))
    for pid, vec in reversed(tied):
        reverse.upsert(_artifact(pid, vec))
    got = []
    for st in (forward, reverse):
        ranked = svc.rank_candidates(
            query_vec=[1.0, 0.0, 0.0], store=st, exclude=set(), settings=get_settings()
        )
        got.append(ranked)
    assert got[0] == got[1] == [2001, 2002, 2003]


# ── catalogVersion (C-18) — 계약에서 폐기 제안 중 ──


def test_catalog_version_is_accepted_but_never_echoed() -> None:
    """받아도 무시한다 — 응답에 싣지 않는다(C-18 폐기 제안).

    AI 가 지문을 만들어 실어 보낸 적이 있으나 되돌렸다. `products` 는 I-17 이 제자리 upsert 해
    **그 시점의 임베딩을 보존하지 않으므로** 버전 라벨로 재현할 수 없고, 재현이 필요하지도 않다 —
    산출물(목록·reason)은 Spring 이 `recommendation_generated` 로 이미 저장한다(§3.7).
    """
    data = client.post(_URL, json=_body(catalogVersion="spring-이-보낸-값")).json()
    assert "catalogVersion" not in data


def test_catalog_version_is_omittable() -> None:
    """Spring 이 안 보내도 깨지지 않는다 — 선택 필드로 완화했다(계약 제거 전 전환기)."""
    body = _body()
    body.pop("catalogVersion")
    r = client.post(_URL, json=body)
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"


def test_catalog_version_null_is_accepted() -> None:
    """Spring 이 null 을 보내도 400 이 아니라 200 이어야 한다(C-18 과잉 안전성)."""
    r = client.post(_URL, json=_body(catalogVersion=None))
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"


def test_limit_above_contract_max_is_400() -> None:
    """`limit` 상한은 스키마가 막는다 — 서비스가 뒤에서 깎으면 요청보다 적게 반환하게 된다."""
    from app.schemas.recommendations import LIMIT_MAX

    assert client.post(_URL, json=_body(limit=LIMIT_MAX)).status_code == 200
    r = client.post(_URL, json=_body(limit=LIMIT_MAX + 1))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_config_max_items_cannot_drop_below_contract_limit() -> None:
    """config 가 계약 상한 아래로 내려가면 기동을 막는다 — 그래야 want < limit 이 불가능하다.

    두 값이 갈리면 `_overfetch_size` 가 어느 쪽으로든 계약을 깬다(응답 크기 상한 뚫림 / 요청보다
    적게 반환). `expose_max`↔`LIST_MAX_PRODUCTS` 와 같은 방식으로 기동 시점에 잡는다.
    """
    import pydantic

    from app.schemas.recommendations import LIMIT_MAX

    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, home_reco_max_items=LIMIT_MAX - 1)
    # 동률도 거부한다 — max_items == LIMIT_MAX 면 상한 limit 에서 overfetch 여유가 0 이 된다.
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, home_reco_max_items=LIMIT_MAX)


def test_config_min_candidates_cannot_exceed_max_items() -> None:
    """후보 하한이 응답 상한을 넘으면 기동 실패 — `k=max(want, min_candidates)` 가 상한을 뚫는다."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, home_reco_min_candidates=61, home_reco_max_items=60)


def test_response_never_returns_fewer_than_requested_limit(store: CatalogArtifactStore) -> None:
    """§3.7 — `limit` 은 최종 노출 목표치이고 AI 는 그보다 **넉넉히** 반환해야 한다.

    "이상(≥)"이 아니라 **초과(>)** 를 단언한다 — max_items 기본값이 LIMIT_MAX 와 같으면
    `limit` 최댓값에서 여유분이 정확히 0 이 되는데(overfetch 1.0x), ≥ 단언은 그 축소를
    통과시켰다(PR 리뷰). 후보가 충분한 한 상한 `limit` 에서도 품절 드롭 여유가 있어야 한다.
    """
    from app.schemas.recommendations import LIMIT_MAX

    for pid in range(2000, 2200):
        store.upsert(_artifact(pid, [1.0, 0.0, 0.0], doc=f"상품 {pid}"))
    items = client.post(_URL, json=_body(limit=LIMIT_MAX)).json()["items"]
    assert len(items) > LIMIT_MAX, "상한 limit 에서도 품절 드롭 대비 여유가 있어야 한다"
    assert len(items) <= get_settings().home_reco_max_items


# ── 인증 · 입력 검증 ──


def _jwks_settings() -> Settings:
    """운영(jwks) 모드 Settings — .env 미참조, 필수값만 채운다(test_auth_e2e 와 동일 관행)."""
    return Settings(
        _env_file=None,
        auth_mode="jwks",
        jwks_url="https://spring.test/.well-known/jwks.json",
        pii_hash_pepper="test-pepper",
        internal_api_token="right-token",
        google_api_key="test-google-key",
    )


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong-token"}])
def test_service_token_missing_or_mismatch_is_401(
    monkeypatch: pytest.MonkeyPatch, headers: dict
) -> None:
    """토큰 없음/불일치 → 401 INTERNAL_TOKEN_INVALID (§3.7). 운영은 fail-closed."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)
    r = client.post(_URL, json=_body(), headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"


def test_service_token_match_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """올바른 서비스 토큰이면 통과한다 — 401 테스트가 항상-거부로 통과하지 않음을 고정."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)
    r = client.post(_URL, json=_body(), headers={"X-Internal-Token": "right-token"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"memberId": 0},  # 양의 BIGINT 아님
        {"memberId": "123"},  # 문자열 coercion 거부(strict)
        {"memberId": 2**63},  # BIGINT 범위 초과
        {"limit": 0},  # 노출 목표는 1 이상
        {"limit": -1},
        {"limit": 2**31},  # 상한 초과
        {"sessionId": "s-1"},  # 홈에는 채팅 세션이 없다 — 미지 필드 거부
    ],
)
def test_bad_request_is_400(bad: dict) -> None:
    r = client.post(_URL, json=_body(**bad))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    "signals",
    [
        {"recentlyViewedProductIds": [2**63]},  # BIGINT 상한 초과 — psycopg 바인딩에서 터진다
        {"cartProductIds": [0]},  # 양수 아님
        {"recentPurchasedProductIds": [-1]},
        {"cartProductIds": ["9001"]},  # 문자열 coercion 거부(strict)
        {"recentlyViewedProductIds": list(range(1, 202))},  # 배열 길이 상한 초과
    ],
)
def test_signal_ids_are_range_and_length_checked(signals: dict) -> None:
    """시그널 id 도 memberId 와 같은 수준으로 막는다 — DB 경계에서 터지기 전에 400 으로 거절.

    길이 상한이 없으면 요청당 `get_many`/`exclude` 조회 비용에 상한이 없다(리뷰 지적).
    """
    body = _body()
    body["signals"] = {
        "recentlyViewedProductIds": [],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
        **signals,
    }
    r = client.post(_URL, json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BAD_REQUEST"


def test_signal_ids_at_the_length_limit_are_accepted() -> None:
    """상한 자체는 허용한다 — 경계에서 정상 요청을 막지 않는지 확인."""
    body = _body()
    body["signals"] = {
        "recentlyViewedProductIds": [9001, *range(1, 200)],
        "cartProductIds": [],
        "recentPurchasedProductIds": [],
    }
    assert client.post(_URL, json=body).status_code == 200


def test_identity_comes_from_body_under_service_token_only() -> None:
    """레인 b 계약 — 신원은 본문 memberId 이고 인가는 서비스 토큰이 담당한다(§2.3 b).

    사용자 JWT 를 받지 않으므로 Authorization 헤더가 있어도 신원에 영향을 주지 않는다.
    """
    plain = client.post(_URL, json=_body()).json()
    with_jwt = client.post(
        _URL, json=_body(), headers={"Authorization": "Bearer someone-elses-token"}
    ).json()
    assert [i["productId"] for i in plain["items"]] == [i["productId"] for i in with_jwt["items"]]


# ── 의존성 장애 (cold start 와 구분된다) ──


def test_catalog_store_failure_is_503_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """랭킹의 유일한 입력이 죽으면 503 UPSTREAM_UNAVAILABLE — 500 이 아니다(§3.7 실패 응답표)."""

    def _boom():
        raise RuntimeError("pg-catalog down")

    monkeypatch.setattr(svc, "get_catalog_store", _boom)
    r = client.post(_URL, json=_body())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


def test_catalog_failure_message_leaks_no_upstream_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """업스트림 예외 문자열·클래스명이 응답으로 새지 않는다(§3.7 [HARD]·#141 규약)."""

    def _boom():
        raise RuntimeError("psycopg.OperationalError: host=10.0.0.5 password=hunter2")

    monkeypatch.setattr(svc, "get_catalog_store", _boom)
    raw = client.post(_URL, json=_body()).text
    for banned in ("psycopg", "10.0.0.5", "hunter2", "OperationalError", "RuntimeError"):
        assert banned not in raw


def test_store_calls_never_run_on_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore
) -> None:
    """스토어 호출은 전부 별도 스레드에서 돈다 — pg 구현이 psycopg **동기** 드라이버라서다.

    직접 부르면 쿼리가 끝날 때까지 이벤트 루프가 막혀 같은 워커의 채팅 SSE 까지 지연된다
    (`search_service`·`category_mapping` 이 PR #42 이후 지키는 컨벤션). 인메모리 스토어는
    블로킹을 재현하지 못하므로 **호출 스레드**를 관찰해 규약을 고정한다.
    """
    import asyncio as _asyncio

    # 테스트 함수의 스레드와 비교하면 안 된다 — TestClient 는 이벤트 루프를 별도 portal 스레드에서
    # 돌리므로 그 비교는 to_thread 없이도 통과한다(헛도는 테스트). **호출 스레드에 실행 중인
    # 루프가 있는지**를 본다 — 루프 스레드에서 돌면 `get_running_loop()` 가 성공한다.
    def on_loop_thread() -> bool:
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    seen: dict[str, list[bool]] = {"get_many": [], "top_k": []}
    orig_get_many, orig_top_k = store.get_many, store.top_k_by_vector

    def spy_get_many(product_ids):
        seen["get_many"].append(on_loop_thread())
        return orig_get_many(product_ids)

    def spy_top_k(query_vec, *, k, exclude=None):
        seen["top_k"].append(on_loop_thread())
        return orig_top_k(query_vec, k=k, exclude=exclude)

    monkeypatch.setattr(store, "get_many", spy_get_many)
    monkeypatch.setattr(store, "top_k_by_vector", spy_top_k)

    assert client.post(_URL, json=_body(limit=10)).status_code == 200
    assert seen["get_many"] and seen["top_k"], "스토어가 실제로 호출돼야 관찰이 유효하다"
    for name, flags in seen.items():
        assert not any(flags), f"{name} 이 이벤트 루프 스레드에서 실행됐다(블로킹)"


def test_reason_lookup_failure_degrades_to_null_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_reasons` 가 터져도 **이미 확정된 개인화 순위는 버리지 않는다** — reason 만 null.

    처리되지 않은 500 은 막아야 하지만(계약 "outcome 3종 모두 200"), 503 으로 올리면 Spring 이
    멀쩡한 랭킹 대신 인기상품 폴백(AI_ERROR)을 쓰게 된다. `reason` 은 nullable 이고 프로필 조회도
    같은 이유로 degrade 하므로 여기서도 degrade 가 맞다(§3.7·§4.11 "홈 렌더 비차단").
    """

    def _boom(**kwargs):
        raise RuntimeError("pg-catalog down mid-request")

    monkeypatch.setattr(svc, "build_reasons", _boom)
    r = client.post(_URL, json=_body(limit=10))
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "PERSONALIZED"
    assert data["items"], "랭킹은 그대로 나가야 한다"
    assert all(i["reason"] is None for i in data["items"])


def test_catalog_hang_is_504_not_indefinite_wait(
    monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore
) -> None:
    """pg 지연·락이 요청을 붙들면 계약의 504 UPSTREAM_TIMEOUT 으로 끝난다(§3.7 실패 응답표).

    이 경로가 없으면 계약이 정의한 504 가 어떤 코드에서도 발생하지 않는다(PR 리뷰) —
    hang 또는 미처리 예외로 끝난다.
    """
    import time as _time

    fast = get_settings().model_copy(update={"home_reco_store_timeout_s": 0.05})
    monkeypatch.setattr(svc, "get_settings", lambda: fast)

    def slow_top_k(query_vec, *, k, exclude=None):
        _time.sleep(0.5)  # 예산(0.05s)을 확실히 넘긴다
        return []

    monkeypatch.setattr(store, "top_k_by_vector", slow_top_k)
    r = client.post(_URL, json=_body())
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_total_wall_clock_is_bounded_by_budget_not_per_call_sum(
    monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore
) -> None:
    """호출별 상한의 합(3×)이 아니라 **요청 전체 예산**이 벽시계를 지배한다(§3.7 응답 3s).

    각 단계가 상한 직전까지 걸리면 고정 상한 방식은 최대 3배까지 늘어난다(PR 리뷰) — 잔여 예산
    방식에서는 앞 단계가 먹은 시간이 뒷 단계 상한에서 빠져 총합이 예산 근처에서 끊긴다.
    """
    import time as _time

    tuned = get_settings().model_copy(
        update={"home_reco_store_timeout_s": 10.0, "home_reco_budget_s": 0.3}
    )
    monkeypatch.setattr(svc, "get_settings", lambda: tuned)

    orig_get_many = store.get_many

    def slow_get_many(product_ids):
        _time.sleep(0.15)  # 1단계가 예산 절반을 먹는다
        return orig_get_many(product_ids)

    def slow_top_k(query_vec, *, k, exclude=None):
        _time.sleep(1.0)  # 잔여 예산(~0.15s)보다 길다 — 고정 상한(10s)이면 통과했을 시간
        return []

    monkeypatch.setattr(store, "get_many", slow_get_many)
    monkeypatch.setattr(store, "top_k_by_vector", slow_top_k)

    # HTTP(TestClient) 대신 핸들러를 직접 잰다 — TestClient 는 요청별 portal 을 닫으며 포기된
    # 스토어 스레드까지 join 해서(운영 uvicorn 에는 없는 동작) 응답 시점이 가려진다.
    import asyncio as _asyncio

    from app.schemas.recommendations import HomeRecommendationRequest

    req = HomeRecommendationRequest.model_validate(_body())

    async def run() -> float:
        t = _time.perf_counter()
        try:
            await svc.rank_home(req, request_id="wall-clock-budget-test")
        except svc.UpstreamTimeout:
            return _time.perf_counter() - t
        pytest.fail("예산 초과인데 504 가 나오지 않았다")

    elapsed = _asyncio.run(run())
    assert elapsed < 1.0, f"예산 0.3s 인데 {elapsed:.2f}s — 잔여 예산이 반영되지 않았다"


def test_slow_profile_read_cannot_starve_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 조회도 예산·호출 상한 아래다 — 느려도 랭킹을 굶기지 못하고 그 항만 빠진다.

    프로필 스토어 자체 타임아웃(state_store_query_timeout_s 3s)이 홈 예산(2.5s)보다 커서,
    예산 밖에 두면 §3.7 "응답 3s" 가 프로필 구간에서 이미 깨진다(PR 리뷰).
    """
    import asyncio as _asyncio
    import time as _time

    tuned = get_settings().model_copy(
        update={"home_reco_store_timeout_s": 0.2, "home_reco_budget_s": 1.0}
    )
    monkeypatch.setattr(svc, "get_settings", lambda: tuned)

    async def slow_profile(user_id: str | None) -> dict | None:
        await _asyncio.sleep(5)  # 자체 타임아웃(3s)보다도 길다
        return None

    monkeypatch.setattr(svc, "read_profile_summary", slow_profile)
    t = _time.perf_counter()
    r = client.post(_URL, json=_body(limit=10))
    elapsed = _time.perf_counter() - t
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED", "프로필이 느려도 시그널 랭킹은 나간다"
    assert elapsed < 1.0, f"프로필 상한 0.2s 인데 {elapsed:.2f}s"


def test_config_db_timeout_must_exceed_app_timeout() -> None:
    """DB statement_timeout ≤ 앱 호출 상한이면 기동 실패 — 느린 쿼리가 503/504 로 비결정적으로 갈린다."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, catalog_store_query_timeout_s=2.0, home_reco_store_timeout_s=2.0)


def test_reason_timeout_degrades_not_504(
    monkeypatch: pytest.MonkeyPatch, store: CatalogArtifactStore
) -> None:
    """reason 재료 조회의 타임아웃은 504 가 아니라 degrade — 확정된 랭킹을 버리지 않는다."""
    import time as _time

    fast = get_settings().model_copy(update={"home_reco_store_timeout_s": 0.2})
    monkeypatch.setattr(svc, "get_settings", lambda: fast)

    def slow_reasons(**kwargs):
        _time.sleep(0.5)
        return {}

    monkeypatch.setattr(svc, "build_reasons", slow_reasons)
    r = client.post(_URL, json=_body(limit=10))
    assert r.status_code == 200
    data = r.json()
    assert data["outcome"] == "PERSONALIZED"
    assert data["items"] and all(i["reason"] is None for i in data["items"])


def test_overfetch_uses_ceil_not_float_truncation() -> None:
    """배율이 이진 부동소수점으로 부정확한 값(1.3)일 때 int 절단이 여유분을 1개 깎는다 — ceil 로 고정.

    10×1.3 = 12.999…(IEEE-754) → int() 는 12, ceil 은 13. "넉넉히" 계약의 안전한 방향은 올림이다.
    """
    tuned = get_settings().model_copy(
        update={"home_reco_overfetch_ratio": 1.3, "home_reco_max_items": 120}
    )
    assert svc._overfetch_size(10, tuned) == 13


def test_profile_store_failure_degrades_to_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 저장소 장애는 degrade — 프로필은 reason 근거일 뿐 랭킹 입력이 아니다."""

    async def _boom(user_id: str | None) -> dict | None:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(svc, "read_profile_summary", _boom)
    r = client.post(_URL, json=_body())
    assert r.status_code == 200
    assert r.json()["outcome"] == "PERSONALIZED"


# ── reason: 미리 만들어 둔 extras 재료에서 고른다 (LLM 0회) ──


def _with_extras(store: CatalogArtifactStore, product_id: int, **extras) -> None:
    art = store.get(product_id)
    art.extras = dict(extras)
    store.upsert(art)


def test_reason_picks_cart_tag_first(store: CatalogArtifactStore) -> None:
    """담기는 §3.7 이 '강한 신호'라 한 축 — 담기 태그가 조회 태그보다 우선한다."""
    _with_extras(store, 9001, situation_tags=["조회태그"])
    store.upsert(_artifact(9002, [1.0, 0.0, 0.0], doc="담기상품"))
    _with_extras(store, 9002, situation_tags=["담기태그"])
    _with_extras(store, 1001, situation_tags=["담기태그", "조회태그"])

    signals = {
        "recentlyViewedProductIds": [9001],
        "cartProductIds": [9002],
        "recentPurchasedProductIds": [],
    }
    items = client.post(_URL, json=_body(limit=10, signals=signals)).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 1001)
    assert reason == "장바구니 상품과 함께 담기태그에 쓰기 좋아요"


def test_reason_falls_back_to_viewed_tag(store: CatalogArtifactStore) -> None:
    _with_extras(store, 9001, situation_tags=["캠핑"])
    _with_extras(store, 1001, situation_tags=["캠핑"])
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 1001)
    assert reason == "최근 보신 상품처럼 캠핑에 맞아요"


def test_avoided_brand_is_never_presented_as_a_match(
    store: CatalogArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """회피 브랜드가 "맞춰 골랐어요"로 소개되지 않는다 — 프로필 문자열 매칭 분기 제거의 근거.

    프로필 요약은 LLM 자유 마크다운이라 선호/회피 극성을 파싱할 앵커가 없다(PR 리뷰). 분기가
    있으면 "회피 브랜드: 나이키"의 "나이키"가 태그와 부분 문자열로 매칭돼, 사용자가 피하겠다는
    브랜드를 취향이라고 소개한다.
    """
    _with_extras(store, 9001, situation_tags=["무관한태그"])
    _with_extras(store, 1001, situation_tags=["나이키"], review_pros=["쿠션이 좋다는 평"])

    async def _profile(user_id: str | None) -> dict | None:
        return {
            "markdown": "## 구조화 블록\n- 회피 브랜드: 나이키\n",
            "generated_at": "2026-07-31T00:00:00Z",
            "embedding": None,
        }

    monkeypatch.setattr(svc, "read_profile_summary", _profile)
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 1001)
    assert reason == "쿠션이 좋다는 평", f"회피 브랜드가 취향으로 소개됐다: {reason!r}"
    assert all("맞춰 골랐어요" not in (i["reason"] or "") for i in items)


def test_duplicate_signal_ids_do_not_inflate_the_query_vector(
    store: CatalogArtifactStore,
) -> None:
    """같은 id 가 여러 번 와도 질의 벡터를 지배하지 못한다 — 가중 누적도 dedup 목록을 돈다.

    스키마는 배열 길이·값 범위만 막고 중복은 막지 않는다(PR 리뷰). dedup 없이는 viewed 의
    decay 도 같은 상품을 서로 다른 rank 로 중복 합산한다.
    """
    store.upsert(_artifact(9002, [0.0, 1.0, 0.0], doc="시그널 상품 B"))

    def ranking(viewed: list[int]) -> list[int]:
        signals = {
            "recentlyViewedProductIds": viewed,
            "cartProductIds": [],
            "recentPurchasedProductIds": [],
        }
        return [
            i["productId"]
            for i in client.post(_URL, json=_body(limit=10, signals=signals)).json()["items"]
        ]

    assert ranking([9001, 9002]) == ranking([9001, 9002, 9002, 9002, 9002]), (
        "중복 9002 가 벡터를 지배하면 순위가 달라진다"
    )


def test_reason_falls_back_to_review_pro(store: CatalogArtifactStore) -> None:
    """아무 매칭도 없으면 상품 고유 재료(리뷰 장점)를 쓴다."""
    _with_extras(store, 9001, situation_tags=["무관"])
    _with_extras(
        store, 1001, situation_tags=["다른것"], review_pros=["수납공간이 넓어 정리에 좋음"]
    )
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 1001)
    assert reason == "수납공간이 넓어 정리에 좋음"


def test_reason_is_null_when_no_material(store: CatalogArtifactStore) -> None:
    """재료가 없으면 null — 계약상 정상이고 P-5 가 표시하지 않는다."""
    _with_extras(store, 9001)
    _with_extras(store, 1001)
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    assert next(i["reason"] for i in items if i["productId"] == 1001) is None


def test_reason_reads_legacy_tags_key(store: CatalogArtifactStore) -> None:
    """구 enrichment 스키마(`tags`)도 재료로 인정한다 — 신규/기존 상품이 섞여 있다."""
    _with_extras(store, 9001, tags=["여행"])
    _with_extras(store, 1001, tags=["여행"])
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    assert (
        next(i["reason"] for i in items if i["productId"] == 1001)
        == "최근 보신 상품처럼 여행에 맞아요"
    )


def test_reason_is_deterministic_across_calls(store: CatalogArtifactStore) -> None:
    """LLM 이 없으므로 reason 도 결정적이다 — 완료조건(동일 입력 → 복원 가능)에 부합."""
    _with_extras(store, 9001, situation_tags=["가", "나"])
    _with_extras(store, 1001, situation_tags=["나", "가"])
    a = {
        i["productId"]: i["reason"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]
    }
    b = {
        i["productId"]: i["reason"] for i in client.post(_URL, json=_body(limit=10)).json()["items"]
    }
    assert a == b
    # 공통 태그가 여럿이면 사전순 첫 항목 — 저장 순서에 의존하지 않는다
    assert a[1001] == "최근 보신 상품처럼 가에 맞아요"


def test_signal_product_itself_gets_own_frame(store: CatalogArtifactStore) -> None:
    """시그널 상품이 후보로 올라오면 '비슷한 상품' 서술이 어색하다 — 전용 문구."""
    _with_extras(store, 9001, situation_tags=["캠핑"])
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    assert (
        next(i["reason"] for i in items if i["productId"] == 9001)
        == "최근 관심 있게 보신 상품이에요"
    )


def test_reason_is_sanitized_and_capped(store: CatalogArtifactStore) -> None:
    """재료가 신뢰경계를 넘기 전에 제어문자 제거 + 상한 truncate (I-21 과 동일 규약)."""
    dirty = "줄바꿈\n과 제어\x00문자 " + "가" * 500
    _with_extras(store, 9001, situation_tags=["무관"])
    _with_extras(store, 1001, review_pros=[dirty])
    items = client.post(_URL, json=_body(limit=10)).json()["items"]
    reason = next(i["reason"] for i in items if i["productId"] == 1001)
    assert "\n" not in reason
    assert "\x00" not in reason
    assert len(reason) <= get_settings().reason_max_len


def test_reason_never_calls_an_llm(
    store: CatalogArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 경로에서 LLM 을 부르지 않는다 — 3s 예산의 근거이자 결정성의 근거다."""

    def _boom():
        raise AssertionError("요청 경로에서 LLM 을 부르면 안 된다")

    monkeypatch.setattr("app.core.llm.get_llm", _boom)
    _with_extras(store, 9001, situation_tags=["캠핑"])
    _with_extras(store, 1001, situation_tags=["캠핑"])
    r = client.post(_URL, json=_body(limit=10))
    assert r.status_code == 200
    assert any(i["reason"] for i in r.json()["items"])


# ── provenance 비노출 [HARD] ──


def test_response_carries_no_algorithm_or_model_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """알고리즘·모델 버전은 와이어에 싣지 않는다 — 내부 테이블 보관(§3.7 [HARD])."""
    raw = client.post(_URL, json=_body()).text
    for banned in ("algorithmVersion", "modelVersion", "claude", "haiku", "gpt", "prompt"):
        assert banned not in raw


def test_profile_text_never_reaches_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로필 원문은 응답에 포함하지 않는다(§3.7 [HARD])."""
    secret = "사용자는 캠핑을 좋아하고 예산은 20만원이다"

    async def _profile(user_id: str | None) -> dict | None:
        return {"markdown": secret, "generated_at": "2026-07-31T00:00:00Z"}

    monkeypatch.setattr(svc, "read_profile_summary", _profile)
    raw = client.post(_URL, json=_body()).text
    assert secret not in raw


def test_log_has_fixed_safe_key_set_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`home_reco_request` 관측 로그(§6.3 b)에 프로필 원문·상품 id·모델 식별자·토큰이 남지 않는다.

    [이슈 #140] `recommend_provenance`(§6.3 d)는 같은 로거로 나가지만 **의도적으로**
    productId·listId 를 싣는다(CH-5 가 인증 불필요 공개 조회라 PII 가 아니다, §6 v2 표) — 그
    검사는 `9001`/`1001` 배제 대상에서 뺀다. 프로필 원문·모델 식별자·토큰은 두 로그 어디에도
    남으면 안 되므로 전체 레코드를 계속 검사한다.
    """
    secret = "사용자는 캠핑을 좋아한다"

    async def _profile(user_id: str | None) -> dict | None:
        return {"markdown": secret, "generated_at": "2026-07-31T00:00:00Z"}

    monkeypatch.setattr(svc, "read_profile_summary", _profile)
    with caplog.at_level(logging.INFO, logger=svc.logger.name):
        client.post(_URL, json=_body())

    records = [r for r in caplog.records if r.name == svc.logger.name]
    assert records, "요청마다 관측 로그 1건을 남긴다"
    standard_keys = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "asctime",
        "message",
    }
    blob = " ".join(
        r.getMessage()
        + " "
        + json.dumps(
            {key: value for key, value in r.__dict__.items() if key not in standard_keys},
            default=str,
        )
        for r in records
    )
    for banned in (secret, "캠핑", "claude", "haiku", "right-token"):
        assert banned not in blob, f"로그에 {banned!r} 가 남았다"

    # `home_reco_request` 는 `log_structured` 가 아니라 `logger.info(json.dumps(...))` 로 직접
    # 나가 LogRecord 에 `event` extra 가 없다(#469 관례) — 메시지를 파싱해 이벤트로 가려낸다.
    request_log = next(
        r for r in records if json.loads(r.getMessage()).get("event") == "home_reco_request"
    )
    request_blob = request_log.getMessage()
    for banned in ("9001", "1001"):
        assert banned not in request_blob, f"home_reco_request 로그에 {banned!r} 가 남았다"


def test_log_records_outcome_and_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """장애 관측에 필요한 유계 코드는 남긴다 — outcome·후보 수·소요.

    [#469] 필드는 `extra` 가 아니라 **JSON 메시지**에 실린다 — extra 는 기본 포맷터가 출력하지
    않아 실 로그(docker logs)에서 증발했다(운영 실측 2026-08-08).
    """
    with caplog.at_level(logging.INFO, logger=svc.logger.name):
        client.post(_URL, json=_body())
    rec = next(r for r in caplog.records if r.name == svc.logger.name)
    payload = json.loads(rec.getMessage())
    assert payload["outcome"] == "PERSONALIZED"
    assert isinstance(payload["candidateCount"], int)
    assert isinstance(payload["returnedCount"], int)
    assert isinstance(payload["elapsedMs"], int)
    assert payload["reasonSource"] in {"extras", "none"}


# ── [#469] I-22 요청 트레이스 ──


def _fake_http_request():
    """requestId 상관관계용 http_request 더블 — get_request_id 는 state 만 읽는다."""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(request_id="rid-469-test"))


async def test_home_trace_exports_stage_spans_and_outcome() -> None:
    """콘텐츠 모드에서 I-22 가 루트+단계 span 을 export 한다(finish 는 fire-and-forget)."""
    import asyncio

    from app.api.internal import home_recommendations
    from app.core.tracing import (
        FakeTraceExporter,
        TraceFactory,
        set_trace_factory,
        validate_export_payload,
    )
    from app.schemas.recommendations import HomeRecommendationRequest

    exporter = FakeTraceExporter()
    set_trace_factory(
        TraceFactory(
            exporter=exporter,
            enabled=True,
            sampling_rate=1.0,
            payload_validator=lambda p: validate_export_payload(p, allow_content=True),
            capture_content=True,
            content_max_chars=20000,
        )
    )
    try:
        request = HomeRecommendationRequest.model_validate(_body())
        response = await home_recommendations(request, _fake_http_request(), None)
        assert response.outcome == "PERSONALIZED"
        for _ in range(50):  # 분리 finish 태스크가 export 를 마칠 때까지
            if exporter.exported:
                break
            await asyncio.sleep(0.01)
        nodes = exporter.exported[0]
        names = {node.name for node in nodes}
        assert {
            "home_recommendation",
            "home.profile",
            "home.query_vector",
            "home.rank",
            "home.reasons",
        } <= names
        root = next(node for node in nodes if node.parent_id is None)
        # memberId 원값은 어디에도 없다 — conversation_id 는 지문(sessionFp)으로만.
        assert "123" not in str(root.metadata.get("sessionFp"))
        assert root.metadata["terminalReason"] == "personalized"
        # 미들웨어가 심은 requestId 와 동일해야 §2.4 오류 봉투·X-Request-Id 와 상관된다(PR #470 리뷰).
        assert root.metadata["requestId"] == "rid-469-test"
        assert "signals" in root.inputs
        assert "PERSONALIZED" in root.outputs["message"]
    finally:
        set_trace_factory(None)


async def test_home_trace_disabled_leaves_endpoint_untouched() -> None:
    """트레이스 비활성(Noop)에서도 응답 계약이 동일하다 — 지연·본문 영향 0."""
    from app.api.internal import home_recommendations
    from app.core.tracing import NoopTraceExporter, TraceFactory, set_trace_factory
    from app.schemas.recommendations import HomeRecommendationRequest

    set_trace_factory(TraceFactory(exporter=NoopTraceExporter(), enabled=False, sampling_rate=1.0))
    try:
        request = HomeRecommendationRequest.model_validate(_body())
        response = await home_recommendations(request, _fake_http_request(), None)
        assert response.outcome == "PERSONALIZED"
        assert len(response.items) >= 1
    finally:
        set_trace_factory(None)


def test_home_reco_log_carries_outcome_in_message(caplog: pytest.LogCaptureFixture) -> None:
    """[#469] outcome·개수가 로그 **메시지**에 실린다 — extra 는 포맷터가 버리던 결함의 핀."""
    with caplog.at_level(logging.INFO, logger="app.services.home_recommendation"):
        response = client.post(_URL, json=_body())
    assert response.status_code == 200
    records = [r.getMessage() for r in caplog.records if "home_reco_request" in r.getMessage()]
    assert records, "home_reco_request 로그가 없다"
    payload = json.loads(records[-1])
    assert payload["outcome"] == "PERSONALIZED"
    assert payload["returnedCount"] >= 1
