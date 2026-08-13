"""추천 실행 provenance 로그 `recommend_provenance` 계약 테스트 (이슈 #140).

`recommendationRequestId`/`listId`/순서 복원·rankSource 판정·[HARD] 모델 식별자 비노출·
가명화·실패 격리·상한을 고정한다. 채팅 경로는 `tests/unit/test_recommendation.py` 의
`run_buyer_turn`/`FakeLLM`/`_RecordingPush` 하네스를, 홈 경로는 `TestClient` 를 직접
구동한다(§3.7 인메모리 카탈로그). caplog 는 렌더된 sink 문자열까지 `json.loads` 해서
검증한다(docs/lessons.md 2026-08-10 「extra= 검증은 렌더된 sink 문자열까지 확인한다」).
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.core import reco_provenance
from app.core.config import get_settings
from app.core.logging import safe_fingerprint
from app.core.tracing import (
    FakeTraceExporter,
    NoopRequestTrace,
    TraceFactory,
    bind_request_trace,
)
from app.main import app
from app.pipelines.artifact_store import CatalogArtifact, CatalogArtifactStore
from app.schemas.spring import ProductSearchResult, SpringProduct
from app.services import home_recommendation as home_svc
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM
from tests.unit.test_recommendation import (
    _NO_CONDITION_DECOMPOSE,
    _RecordingPush,
    _catalog_store,
    _collect,
    _counting_search_calls,
    _inject_profile,
    _make_search,
    _member,
    _member_num,
    _only_list,
    _prod,
    _recording_popular,
    _req,
    run_buyer_turn,
)
from tests.unit.test_tracing import PRIVACY_CANARIES, _assert_canaries_absent

_GRAPH_LOGGER = "app.agents.buyer.recommendation.graph"
_HOME_LOGGER = "app.services.home_recommendation"
_FORMATTER = logging.Formatter("%(message)s")


def _provenance_records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """`log_structured` 가 낸 `recommend_provenance` 레코드를 **렌더된 message** 로 파싱한다.

    `record.<attr>` 단언만으로는 formatter 가 실제로 무엇을 출력하는지 보증하지 못한다
    (docs/lessons.md 2026-08-10) — `logging.Formatter("%(message)s")` 로 렌더한 문자열을
    `json.loads` 한다.
    """
    return [
        json.loads(_FORMATTER.format(r))
        for r in caplog.records
        if getattr(r, "event", None) == "recommend_provenance"
    ]


def _only_provenance(caplog: pytest.LogCaptureFixture) -> dict:
    records = _provenance_records(caplog)
    assert len(records) == 1, f"recommend_provenance 1건을 기대했지만 {len(records)}건"
    return records[0]


def _scored_rerank_payload() -> dict[str, object]:
    return {
        "evaluations": [
            {
                "productId": product_id,
                "intentFit": intent_fit,
                "needFit": 3,
                "profileFit": 0,
                "rationale": "요청과의 관련도를 기준으로 추천했어요",
                "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                "evidenceFields": [],
            }
            for product_id, intent_fit in ((101, 4), (102, 3), (103, 2))
        ],
        "overallComment": "추천이에요",
        "overallClaims": [
            {
                "claimCode": "NO_VERIFIABLE_OVERALL_CLAIM",
                "scope": "FINAL_EXPOSED_PRODUCTS",
                "subjectProductIds": [],
                "evidenceFields": [],
            }
        ],
    }


# ─────────── join 결정성 · 순서 복원 ───────────


async def test_provenance_joins_with_push_via_recommendation_request_id_and_list_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """recommendationRequestId·listId 가 push·products.ready 와 정확히 같은 값이다(§4.2 join 키)."""
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)
    pushed = push.pushes[0]
    ready = next(e for e in events if e["type"] == "products.ready")["data"]

    assert record["recommendationRequestId"] == pushed.recommendation_request_id
    assert [lst["listId"] for lst in record["lists"]] == [e.list_id for e in pushed.lists]
    assert [lst["listId"] for lst in record["lists"]] == ready["listIds"]
    assert record["requestId"] == "unit-request"  # 테스트 하네스 기본 observer.request_id


async def test_provenance_item_positions_and_order_match_push_product_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`position` 은 0-기반이고 배열 순서·productId 가 push 순서와 정확히 일치한다."""
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)
    entry = _only_list(push.pushes[0])
    lst = record["lists"][0]

    assert [item["position"] for item in lst["items"]] == list(range(len(lst["items"])))
    assert [item["productId"] for item in lst["items"]] == entry.product_ids


# ─────────── algorithmVersion 복원 ───────────


async def test_algorithm_version_follows_config_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_settings(), "reco_algorithm_version", "test-algo-9000")
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)
    assert record["algorithmVersion"] == "search_rerank@test-algo-9000"


# ─────────── 정상 경로 회귀 ───────────


async def test_default_current_rank_source_uses_grounding_prompt_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """기본 current 턴은 기존 순위를 유지하고 validated grounding만 적용한다."""
    from app.core.llm import resolve_model_id

    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)

    assert record["degraded"] is False
    assert record["degradeReason"] is None
    assert record["rankerModel"] == resolve_model_id(get_settings(), "smart")
    assert record["promptVersion"] == "rerank-grounding-v1"
    source_by_id = {
        item["productId"]: item["rankSource"] for lst in record["lists"] for item in lst["items"]
    }
    assert source_by_id[101] == "rerank"
    assert source_by_id[102] == "rerank"
    assert source_by_id[103] == "expose_min_fill"


async def test_current_ranking_and_grounding_rollback_restores_legacy_prompt_version(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "current")
    monkeypatch.setattr(settings, "rerank_grounding_arm", "current")
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )

    assert _only_provenance(caplog)["promptVersion"] == "rerank-v1"


@pytest.mark.parametrize(
    ("ranking_arm", "grounding_arm", "expected_prompt_version"),
    [
        ("current", "current", "rerank-v1"),
        ("current", "validated", "rerank-grounding-v1"),
        ("structured", "validated", "rerank-scoring-v1"),
        ("hybrid", "validated", "rerank-scoring-v1"),
    ],
)
async def test_prompt_version_tracks_independent_ranking_and_grounding_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ranking_arm: str,
    grounding_arm: str,
    expected_prompt_version: str,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", ranking_arm)
    monkeypatch.setattr(settings, "rerank_grounding_arm", grounding_arm)
    rerank_payload = _scored_rerank_payload() if ranking_arm != "current" else None

    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(rerank=rerank_payload),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=_RecordingPush(),
            )
        )

    assert _only_provenance(caplog)["promptVersion"] == expected_prompt_version


async def test_rerank_trace_records_internal_ranking_arm_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "rerank_ranking_arm", "hybrid")
    exporter = FakeTraceExporter()
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0).start_request(
        name="buyer_chat_turn",
        request_id="req-ranking-arm",
        conversation_id="session-ranking-arm",
        thread_id="thread-ranking-arm",
        lane="recommend",
        environment="test",
    )

    with bind_request_trace(trace):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(rerank=_scored_rerank_payload()),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=_RecordingPush(),
            )
        )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    rerank_span = next(node for node in exporter.exported[0] if node.name == "llm.rerank")
    assert rerank_span.metadata["rankingArm"] == "hybrid"


async def test_rerank_ranked_item_without_rationale_is_still_rank_source_rerank(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[핵심 회귀] `reason_by_id` 로 rankSource 를 판정하면 빈 rationale 항목이
    `expose_min_fill` 로 오분류된다(§2) — rerank 가 골랐다는 사실(멤버십)만으로 판정해야 한다."""
    # 빈 모델 rationale 자체를 관찰하는 테스트라 C의 중립 템플릿 생성 전에 A rollback으로 고정한다.
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "current")
    monkeypatch.setattr(settings, "rerank_grounding_arm", "current")
    llm = FakeLLM(
        rerank={
            "ranked": [
                {"productId": 101, "rationale": "가성비가 좋아요"},
                {"productId": 102, "rationale": ""},  # rerank 가 골랐지만 근거는 빈 문자열
            ],
            "overallComment": "추천이에요",
        }
    )
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(), _member(), llm=llm, search=_make_search(DEFAULT_PRODUCTS), push_fn=push
            )
        )
    record = _only_provenance(caplog)
    by_id = {item["productId"]: item for lst in record["lists"] for item in lst["items"]}
    assert by_id[102]["rankSource"] == "rerank"
    assert by_id[102]["hasReason"] is False
    assert by_id[103]["rankSource"] == "expose_min_fill"
    assert by_id[103]["hasReason"] is False


# ─────────── degrade 경로 회귀 ───────────


async def test_rerank_degrade_marks_search_order_and_hides_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """rerank 가 LLMError 를 내면 전 항목 `search_order`, `degraded=true`,
    `degradeReason="rerank_fallback"`, `rankerModel`/`promptVersion` 은 `null`."""
    push = _RecordingPush()
    trace = NoopRequestTrace(lane="recommend")
    with bind_request_trace(trace):
        with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
            await _collect(
                run_buyer_turn(
                    _req(),
                    _member(),
                    llm=FakeLLM(rerank_error=True),
                    search=_make_search(DEFAULT_PRODUCTS),
                    push_fn=push,
                )
            )
    record = _only_provenance(caplog)

    assert record["degraded"] is True
    assert record["degradeReason"] == "rerank_fallback"
    assert record["rankerModel"] is None
    assert record["promptVersion"] is None
    assert all(
        item["rankSource"] == "search_order" for lst in record["lists"] for item in lst["items"]
    )


# ─────────── repurchase_pin / expose_min_fill 판정 고정 ───────────


async def test_repurchase_pin_rank_source_for_item_rerank_omitted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#120 명시 재구매 지목으로 앞에 고정된 상품은 rerank 가 빠뜨려도 `repurchase_pin`."""
    import app.services.spring_client as _sc_mod
    from tests.unit.test_recommendation import _fix_now, _purchases_cat

    _fix_now(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "rerank_ranking_arm", "current")
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 3)
    monkeypatch.setattr(
        _sc_mod, "get_recent_purchases", _purchases_cat((101, "음향가전", "무선 이어폰"))
    )
    products = [
        _prod(101, "음향가전", "무선 이어폰"),
        _prod(201, "음향가전", "유선 이어폰"),
        _prod(202, "음향가전", "헤드폰"),
    ]
    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "repurchaseProducts": ["무선 이어폰"],
            "filters": {},
            "case": 1,
        },
        # rerank 가 지목 상품(101)을 빼고 고른 상황 — pin 이 앞에 얹는다.
        rerank={
            "ranked": [
                {"productId": 201, "rationale": "가성비가 좋아요"},
                {"productId": 202, "rationale": "음질이 우수해요"},
            ],
            "overallComment": "추천이에요",
        },
    )
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(), _member_num(), llm=llm, search=_make_search(products), push_fn=push
            )
        )
    record = _only_provenance(caplog)
    source_by_id = {
        item["productId"]: item["rankSource"] for lst in record["lists"] for item in lst["items"]
    }
    assert source_by_id[101] == "repurchase_pin"
    assert source_by_id[201] == "rerank"
    assert source_by_id[202] == "rerank"


# ─────────── BUY_ALL(예산 세트) 경로 ───────────


async def test_buy_all_budget_set_provenance_matches_push_and_rank_source_membership(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[F3, 리뷰 라운드 1] `plan is not None` 분기는 `_split_by_need` 를 타지 않고 예산 세트가
    상품을 재배열·재분배한다 — §2 판정 규칙(`rerank_ranked_ids`/`pinned_ids` 멤버십)이 이
    경로에서도 그대로 도는지, listId·순서가 push 와 일치하는지, provenance 가 정확히 1건
    나오는지를 고정한다. 픽스처는 기존 `test_buy_all_budget_builds_top_k_sets_from_wider_
    candidate_pools`(#60)를 재사용한다."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    settings = get_settings()
    monkeypatch.setattr(settings, "expose_min", 1)
    monkeypatch.setattr(settings, "expose_max", 1)
    monkeypatch.setattr(settings, "budget_set_alt_pool", 3)
    monkeypatch.setattr(settings, "budget_set_max_count", 3)

    async def _map(**kwargs: object) -> CategoryMapping:
        return CategoryMapping(legs=[("A", "파우치"), ("B", "어댑터")])

    pools = {
        "A": [
            _prod(11, "A", "A1").model_copy(update={"price": 30_000}),
            _prod(12, "A", "A2").model_copy(update={"price": 20_000}),
            _prod(13, "A", "A3").model_copy(update={"price": 10_000}),
        ],
        "B": [
            _prod(21, "B", "B1").model_copy(update={"price": 30_000}),
            _prod(22, "B", "B2").model_copy(update={"price": 20_000}),
            _prod(23, "B", "B3").model_copy(update={"price": 10_000}),
        ],
    }

    async def _search(filters, exclude_product_ids=None):
        products = pools[filters.category]
        return ProductSearchResult(products=products, total_count=len(products))

    push = _RecordingPush()
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "buyAll": True,
            "totalBudget": 50_000,
            "categoryQueries": [
                {"category": "A", "query": "파우치"},
                {"category": "B", "query": "어댑터"},
            ],
            "filters": {"priceMax": 50_000},
        },
        rerank={
            "ranked": [
                {"productId": 11, "rationale": "튼튼해요"},
                {"productId": 21, "rationale": "호환돼요"},
            ],
            "overallComment": "세트 추천이에요",
        },
    )
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req("5만원으로 파우치와 어댑터 전부"),
                _member_num(),
                llm=llm,
                search=_search,
                push_fn=push,
                map_categories=_map,
            )
        )

    sent = push.pushes[0]
    assert sent.list_type == "BUY_ALL"
    assert len(sent.lists) >= 2, "BUY_ALL 세트가 2개 이상이어야 재배열 판정을 실제로 잰다"

    record = _only_provenance(caplog)
    assert record["listType"] == "BUY_ALL"
    # provenance 는 이 턴에 정확히 1건 — 세트가 여럿이어도 `lists` 배열 하나에 다 담긴다.
    assert len(_provenance_records(caplog)) == 1
    assert [lst["listId"] for lst in record["lists"]] == [item.list_id for item in sent.lists]
    for prov_list, entry in zip(record["lists"], sent.lists, strict=True):
        assert [item["productId"] for item in prov_list["items"]] == entry.product_ids
        assert [item["position"] for item in prov_list["items"]] == list(
            range(len(prov_list["items"]))
        )

    source_by_id = {
        item["productId"]: item["rankSource"] for lst in record["lists"] for item in lst["items"]
    }
    # rerank 가 실제로 고른 11/21 은 세트 어디에 배치되든 rankSource="rerank" 여야 한다.
    assert source_by_id[11] == "rerank"
    assert source_by_id[21] == "rerank"
    # rerank 가 고르지 않고 예산 세트가 가격순으로 채운 나머지는 expose_min_fill 이다 — 이
    # 경로가 "세트가 채웠다"는 사실을 rankSource 로 드러내야 한다(§2 판정 규칙).
    fill_ids = {12, 13, 22, 23} & source_by_id.keys()
    assert fill_ids, "세트가 rerank 밖 후보로 채운 상품이 최소 1건은 있어야 이 판정을 잰다"
    for pid in fill_ids:
        assert source_by_id[pid] == "expose_min_fill"


# ─────────── 프로필 벡터 경로 ───────────


async def test_profile_vector_path_is_personalized_and_hides_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """조건 없는 취향 벡터 경로는 rerank 를 타지 않으므로 전 항목 `profile_vector`,
    `rankerModel`/`promptVersion` 은 `null`이고 `personalized=true`."""
    _inject_profile(monkeypatch, vector=[1.0, 0.0, 0.0], store=_catalog_store([301, 302, 303]))
    search, _ = _counting_search_calls()
    popular, _ = _recording_popular()
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(message="아무거나 추천해줘", thread_id="nc-provenance-140"),
                _member(),
                llm=FakeLLM(decompose=_NO_CONDITION_DECOMPOSE),
                search=search,
                push_fn=push,
                popular_fn=popular,
            )
        )
    record = _only_provenance(caplog)
    assert record["surface"] == "chat"
    assert record["algorithmVersion"].startswith("profile_vector@")
    assert record["rankerModel"] is None
    assert record["promptVersion"] is None
    assert record["personalized"] is True
    lst = record["lists"][0]
    assert all(item["rankSource"] == "profile_vector" for item in lst["items"])
    assert lst["listId"] == _only_list(push.pushes[0]).list_id


# ─────────── 홈 경로 ───────────


def _catalog_artifact(product_id: int, embedding: list[float], *, doc: str = "") -> CatalogArtifact:
    return CatalogArtifact(
        product_id=product_id,
        search_doc=doc or f"상품 {product_id}",
        embedding=embedding,
        extras={},
    )


def _home_catalog_store() -> CatalogArtifactStore:
    store = CatalogArtifactStore()
    for i in range(6):
        pid = 5001 + i
        store.upsert(_catalog_artifact(pid, [1.0 - (i + 1) * 0.05, (i + 1) * 0.05, 0.0]))
    store.upsert(_catalog_artifact(9101, [1.0, 0.0, 0.0], doc="시그널 상품"))
    return store


async def _no_home_profile(user_id: str | None) -> dict | None:
    del user_id
    return None


def _home_body(**over: object) -> dict:
    body = {
        "memberId": 777,
        "limit": 5,
        "signals": {
            "recentlyViewedProductIds": [9101],
            "cartProductIds": [],
            "recentPurchasedProductIds": [],
        },
    }
    body.update(over)
    return body


def test_home_provenance_matches_response_ids_and_hides_ranker_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """I-22 응답의 recommendationRequestId·listId·items 순서가 provenance 와 일치하고,
    `rankerModel is None`, `surface == "home"`(§3.7 [HARD])."""
    store = _home_catalog_store()
    monkeypatch.setattr(home_svc, "get_catalog_store", lambda: store)
    monkeypatch.setattr(home_svc, "read_profile_summary", _no_home_profile)
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger=_HOME_LOGGER):
        response = client.post("/internal/recommendations/home", json=_home_body())
    assert response.status_code == 200
    data = response.json()

    record = _only_provenance(caplog)
    assert record["surface"] == "home"
    assert record["rankerModel"] is None
    assert record["promptVersion"] is None
    assert record["listType"] == "PICK_ONE"
    assert record["algorithmVersion"].startswith("home_vector@")
    assert record["personalized"] is True  # PERSONALIZED outcome
    assert record["recommendationRequestId"] == data["recommendationRequestId"]

    lst = record["lists"][0]
    assert lst["listId"] == data["listId"]
    assert [item["productId"] for item in lst["items"]] == [i["productId"] for i in data["items"]]
    assert all(item["rankSource"] == "profile_vector" for item in lst["items"])
    assert record["sessionFp"] is None
    assert record["ownerFp"] == safe_fingerprint(str(_home_body()["memberId"]))


def test_home_provenance_emitted_even_when_no_profile_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """시그널이 비어 NO_PROFILE 로 답해도 홈 응답은 반환되므로 provenance 1건이 난다."""
    store = CatalogArtifactStore()  # 비어 있음 — query_vec 이 항상 빈 리스트
    monkeypatch.setattr(home_svc, "get_catalog_store", lambda: store)
    monkeypatch.setattr(home_svc, "read_profile_summary", _no_home_profile)
    client = TestClient(app)

    body = _home_body(
        signals={
            "recentlyViewedProductIds": [],
            "cartProductIds": [],
            "recentPurchasedProductIds": [],
        }
    )
    with caplog.at_level(logging.INFO, logger=_HOME_LOGGER):
        response = client.post("/internal/recommendations/home", json=body)
    assert response.status_code == 200
    assert response.json()["outcome"] == "NO_PROFILE"

    record = _only_provenance(caplog)
    assert record["personalized"] is False
    assert record["lists"] == [{"listId": response.json()["listId"], "label": None, "items": []}]


# ─────────── [HARD] 홈 모델 식별자 부재 ───────────


def test_home_provenance_log_never_contains_model_identifiers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _home_catalog_store()
    monkeypatch.setattr(home_svc, "get_catalog_store", lambda: store)
    monkeypatch.setattr(home_svc, "read_profile_summary", _no_home_profile)
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger=_HOME_LOGGER):
        client.post("/internal/recommendations/home", json=_home_body())
    rendered = json.dumps(_only_provenance(caplog))
    for banned in ("claude", "haiku", "gpt", "sonnet"):
        assert banned not in rendered.lower()


# ─────────── 와이어 불변 ───────────


async def test_push_and_products_ready_payloads_gain_no_new_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """provenance 도입이 push 페이로드·`products.ready` 의 필드 집합을 늘리지 않는다."""
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    sent = push.pushes[0]
    assert set(sent.model_dump(by_alias=True).keys()) == {
        "sessionId",
        "recommendationRequestId",
        "listType",
        "totalBudget",
        "lists",
    }
    assert set(sent.lists[0].model_dump(by_alias=True).keys()) == {
        "listId",
        "label",
        "productIds",
        "reasons",
    }
    ready = next(e for e in events if e["type"] == "products.ready")["data"]
    assert set(ready.keys()) == {"sessionId", "listIds"}


# ─────────── 가명화 ───────────


async def test_owner_and_session_fp_are_peppered_not_raw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    push = _RecordingPush()
    identity = _member()
    request = _req(session_id="session-fp-140")
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                request,
                identity,
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    assert identity.subject is not None
    record = _only_provenance(caplog)
    assert record["ownerFp"] == safe_fingerprint(identity.subject)
    assert record["sessionFp"] == safe_fingerprint(request.session_id)
    rendered = json.dumps(record)
    assert identity.subject not in rendered
    assert request.session_id not in rendered


# ─────────── 실패 격리 ───────────


async def test_provenance_emit_failure_does_not_break_stream_or_push(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """emit 내부(`log_structured`)가 예외를 내도 SSE 스트림은 `done` 으로 정상 종료하고
    push 결과가 바뀌지 않는다(관측이 스트림·응답을 죽이면 안 된다)."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(reco_provenance, "log_structured", _boom)
    push = _RecordingPush()
    with caplog.at_level(logging.WARNING, logger="app.core.reco_provenance"):
        events = await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "products.ready" in types
    assert len(push.pushes) == 1
    assert any("RECO_PROVENANCE_EMIT_FAILED" in r.getMessage() for r in caplog.records)


# ─────────── 상한 ───────────


async def test_items_truncated_flag_set_when_max_items_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_settings(), "reco_provenance_max_items", 1)
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)
    assert record["itemsTruncated"] is True
    assert sum(len(lst["items"]) for lst in record["lists"]) == 1


async def test_items_not_truncated_within_natural_bounds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """자연 상한(90) 안이면 기본 설정에서 잘리지 않는다 — silent cap 오탐 방지 대조군."""
    push = _RecordingPush()
    with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
        await _collect(
            run_buyer_turn(
                _req(),
                _member(),
                llm=FakeLLM(),
                search=_make_search(DEFAULT_PRODUCTS),
                push_fn=push,
            )
        )
    record = _only_provenance(caplog)
    assert record["itemsTruncated"] is False


# ─────────── canary / PII 전수 ───────────


async def test_provenance_log_and_trace_export_exclude_canaries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """발화·프로필·상품명/브랜드·근거·예외에 심은 카나리아가 provenance 로그 원문과 export 된
    telemetry 전체에 없다(#141 규약, `tests/unit/test_tracing.py` 하네스 재사용)."""
    import app.agents.buyer.graph as buyer_graph

    async def _canary_profile(user_id: str | None) -> dict | None:
        del user_id
        return {
            "markdown": f"# 취향\n{PRIVACY_CANARIES['customer_address']}",
            "generatedAt": "2026-08-05",
            "embedding": None,
        }

    monkeypatch.setattr(buyer_graph, "read_profile_summary", _canary_profile)

    products = [
        SpringProduct(
            product_id=101,
            name=PRIVACY_CANARIES["customer_name"],
            price=39000,
            rating=4.5,
            category="카테고리",
            brand=PRIVACY_CANARIES["nested_metadata"],
        ),
        SpringProduct(
            product_id=102,
            name="상품B",
            price=48000,
            rating=4.2,
            category="카테고리",
            brand="BrandY",
        ),
    ]
    llm = FakeLLM(
        rerank={
            "ranked": [
                {"productId": 101, "rationale": PRIVACY_CANARIES["tool_result"]},
                {"productId": 102, "rationale": PRIVACY_CANARIES["seller_message"]},
            ],
            "overallComment": "ok",
        }
    )
    push = _RecordingPush()
    fake_exporter = FakeTraceExporter()
    factory = TraceFactory(exporter=fake_exporter, enabled=True, sampling_rate=1.0)
    trace = factory.start_request(
        name="buyer_chat_turn",
        request_id="req-canary-140",
        conversation_id="session-canary-140",
        thread_id="thread-canary-140",
        lane="recommend",
        environment="test",
    )
    with bind_request_trace(trace):
        with caplog.at_level(logging.INFO, logger=_GRAPH_LOGGER):
            await _collect(
                run_buyer_turn(
                    _req(message=PRIVACY_CANARIES["buyer_message"]),
                    _member(),
                    llm=llm,
                    search=_make_search(products),
                    push_fn=push,
                )
            )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    provenance_records = [
        _FORMATTER.format(r)
        for r in caplog.records
        if getattr(r, "event", None) == "recommend_provenance"
    ]
    assert len(provenance_records) == 1
    for canary in PRIVACY_CANARIES.values():
        assert canary not in provenance_records[0]
    _assert_canaries_absent(fake_exporter.exported)
