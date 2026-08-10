"""I-32~I-37 HTTP 표면 (#360, api-spec §3.8·§3.9).

여기서 재는 것은 **와이어**다 — 저장 계층 규약(CAS·멱등·감사)은 `test_profile_graph_apply.py`
가 이미 고정했고, 이 파일은 그 결과가 **계약이 정한 모양·코드로 나가는지**만 본다.

축 넷:
  - **응답 필드가 정본과 일치한다** — 키 집합을 못 박는다(이 계약의 본질이 사본 정합이다).
  - **오류 코드가 계약 어휘다** — 특히 `409` 는 기본값이 `STREAM_IN_PROGRESS` 라 코드 지정을
    빠뜨리면 FE 에 "스트림 진행 중"이 나가고, 그 결함은 정상 경로 테스트로 안 잡힌다.
  - **신원은 경로에서만 온다** — 남의 JWT 를 실어도 경로 `userId` 가 이긴다. `403` 은 없다.
  - **중복 클릭이 오류 화면이 되지 않는다** — 같은 `If-Match` 재전송은 `200 replayed`.
"""

from __future__ import annotations

import httpx
import pytest

from app.agents.profile import graph_journal
from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    GraphNode,
    make_edge_id,
    make_edge_key,
)
from app.agents.profile.store import get_profile_store, reset_profile_store
from app.api import deps
from app.core.config import Settings
from app.main import app

USER = 358
NOW = "2026-08-11T00:00:00+00:00"
SONY = make_edge_id(make_edge_key("likes", "brand:소니"))
GRAPH = f"/internal/profile/{USER}/graph"
EDGE = f"{GRAPH}/edges/{SONY}"


@pytest.fixture(autouse=True)
def _isolate():
    reset_profile_store()
    graph_journal.reset()
    yield
    reset_profile_store()
    graph_journal.reset()


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


def _edge(label: str = "소니", *, predicate: str = "likes") -> GraphEdge:
    key = make_edge_key(predicate, f"brand:{label}")
    return GraphEdge(
        edge_key=key,
        edge_id=make_edge_id(key),
        node_id=f"brand:{label}",
        predicate=predicate,  # type: ignore[arg-type]
        status="active",
        promoted=True,
        origin="machine",
        source_latest="conversation",
        confidence=0.6,
        evidence_count=1,
        evidence_by_source={"conversation": 1},
        evidence_refs=["f1"],
        first_observed_at=NOW,
        last_observed_at=NOW,
        decay_evaluated_at=NOW,
        valid_from=NOW,
        superseded_by=None,
        suppressed_at=None,
        user_intent=None,
        challenge_count=0,
        derived_from_sensitive=False,
        sensitive_topic=None,
    )


async def _seed(*, revision: int = 42, summary: str | None = "소니 선호") -> None:
    store = await get_profile_store()
    await store.set_graph(
        str(USER),
        GraphDocument(
            revision=revision,
            nodes=[GraphNode(node_id="brand:소니", type="brand", label="소니", verified=False)],
            edges=[_edge()],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )
    if summary is not None:
        await store.set_summary(str(USER), summary, NOW)


# ─────────── I-32 조회 (§3.8) ───────────


async def test_graph_response_matches_the_contract_field_set(client: httpx.AsyncClient) -> None:
    """§3.8 응답 최상위 7키 · 항목 5키 · `object` 3키 — 정본과 필드 단위로 일치해야 한다."""
    await _seed()

    body = (await client.get(GRAPH)).json()

    assert set(body) == {
        "userId",
        "exists",
        "markdown",
        "generatedAt",
        "graphVersion",
        "personalization",
        "edges",
    }
    assert set(body["edges"][0]) == {"edgeId", "predicate", "object", "editable", "challenged"}
    assert set(body["edges"][0]["object"]) == {"nodeId", "type", "label"}
    assert set(body["personalization"]) == {"enabled"}


async def test_graph_echoes_the_path_identity_as_a_number(client: httpx.AsyncClient) -> None:
    """`userId` 는 number(BIGINT)다 — 조회·변경이 같은 타입을 쓴다(v0.26.0 비대칭 해소)."""
    await _seed()

    body = (await client.get(GRAPH)).json()

    assert body["userId"] == USER and isinstance(body["userId"], int)


async def test_missing_profile_is_a_normal_200(client: httpx.AsyncClient) -> None:
    """프로필이 없어도 오류가 아니다 — `exists:false` + 빈 배열. `404` 는 이 경로에 없다."""
    response = await client.get(GRAPH)

    assert response.status_code == 200
    assert response.json() == {
        "userId": USER,
        "exists": False,
        "markdown": None,
        "generatedAt": None,
        "graphVersion": "g0",
        "personalization": {"enabled": True},
        "edges": [],
    }


async def test_graph_version_also_rides_the_etag_header(client: httpx.AsyncClient) -> None:
    """`ETag` 는 **편의 사본**이고 정규 출처는 본문 `graphVersion` 이다 (§3.8)."""
    await _seed()

    response = await client.get(GRAPH)

    assert response.json()["graphVersion"] == "g42"
    assert response.headers["ETag"] == '"g42"'


async def test_disabled_personalization_still_returns_the_whole_graph(
    client: httpx.AsyncClient,
) -> None:
    """중지는 "쓰지 않는다"이지 "숨긴다"가 아니다 — `edges` 는 전량 나가고 `exists` 도 `true` 다.

    **중지 회원의 프로필은 존재하고 보존된다**(§3.9.5). `exists: false` 로 답하면 사용자가 지울
    것이 있는지조차 알 수 없다 — `reader.read_profile_summary` 는 중지 시 `None` 을 내는 **소비
    게이트**(#359)라 여기서 쓰면 안 되는 이유다.

    바뀌는 것은 `markdown` 하나다 — 아래 테스트가 그쪽을 잰다.
    """
    await _seed()
    await graph_journal.set_personalization(
        user_id=USER, enabled=False, request_id="req-0", now=NOW
    )

    body = (await client.get(GRAPH)).json()

    assert body["personalization"] == {"enabled": False}
    assert body["exists"] is True  # 데이터는 보존된다
    assert len(body["edges"]) == 1


async def test_the_summary_is_withheld_while_personalization_is_paused(
    client: httpx.AsyncClient,
) -> None:
    """`markdown` 은 중지 중 `null` 이다 — 요약은 소비 산출물이라 "사용 중지"의 대상이다.

    #359 가 rerank·홈 프로필 벡터·마이페이지 세 소비처를 같은 규칙으로 막았다(REQ-PGRAPH-100).
    **지운 게 아니다** — 재개하면 그대로 돌아온다.
    """
    await _seed()
    await graph_journal.set_personalization(
        user_id=USER, enabled=False, request_id="req-0", now=NOW
    )

    paused = (await client.get(GRAPH)).json()
    await graph_journal.set_personalization(
        user_id=USER, enabled=True, request_id="req-1", now=NOW
    )
    resumed = (await client.get(GRAPH)).json()

    assert paused["markdown"] is None and paused["generatedAt"] is None
    assert resumed["markdown"] == "소니 선호"  # 복원된다


@pytest.mark.parametrize("raw", ["0", "abc", "true", str(2**63)])
async def test_a_bad_path_identity_is_400(client: httpx.AsyncClient, raw: str) -> None:
    """`{userId}` 가 `1..2^63-1` 밖이거나 정수가 아니면 `400 BAD_REQUEST` 다 (§3.8 실패표)."""
    response = await client.get(f"/internal/profile/{raw}/graph")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


# ─────────── I-33 수정 (§3.9.1) ───────────


async def test_patch_returns_an_item_shaped_exactly_like_the_list(
    client: httpx.AsyncClient,
) -> None:
    """응답 `edge` 는 §3.8 `edges[]` 항목과 **완전히 같은 모양**이다 — FE 가 통째로 교체한다."""
    await _seed()

    body = (
        await client.patch(EDGE, json={"predicate": "avoids"}, headers={"If-Match": '"g42"'})
    ).json()

    assert set(body) == {"userId", "graphVersion", "edge", "merged", "replayed"}
    assert set(body["edge"]) == {"edgeId", "predicate", "object", "editable", "challenged"}
    assert body["edge"]["predicate"] == "avoids"
    assert body["edge"]["edgeId"] != SONY  # 식별자는 (관계, 대상) 파생이라 바뀐다


async def test_patch_accepts_the_node_id_form(client: httpx.AsyncClient) -> None:
    """FE 자동완성이 고른 노드는 `nodeId` 로 그대로 되돌려 보낸다 — 재정규화를 안 탄다."""
    await _seed()

    response = await client.patch(
        EDGE,
        json={"predicate": "avoids", "object": {"nodeId": "brand:소니"}},
        headers={"If-Match": '"g42"'},
    )

    assert response.status_code == 200
    assert response.json()["edge"]["object"]["nodeId"] == "brand:소니"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"predicate": "purchased"}, id="purchased-is-not-a-user-opinion"),
        pytest.param({}, id="predicate-and-object-both-missing"),
        pytest.param(
            {"object": {"nodeId": "brand:소니", "type": "brand", "label": "소니"}},
            id="both-object-forms",
        ),
        pytest.param({"object": {"type": "brand"}}, id="type-without-label"),
        pytest.param({"predicate": "avoids", "unknown": 1}, id="unknown-field"),
        pytest.param({"Predicate": "avoids"}, id="wrong-case"),
        pytest.param({"object": {"node_id": "brand:소니"}}, id="snake-case"),
    ],
)
async def test_patch_rejects_bodies_outside_the_contract(
    client: httpx.AsyncClient, payload: dict
) -> None:
    """strict 스키마 — 미지 필드를 조용히 흡수하면 계약 불일치가 은폐된다 (§3.9)."""
    await _seed()

    response = await client.patch(EDGE, json=payload, headers={"If-Match": '"g42"'})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_patch_of_an_unknown_edge_is_404(client: httpx.AsyncClient) -> None:
    """**남의 edge 든 미존재든 동일 응답**이다 — 구분하면 남의 취향을 열거할 수 있다."""
    await _seed()

    response = await client.patch(
        f"{GRAPH}/edges/e_deadbeefdeadbeef",
        json={"predicate": "avoids"},
        headers={"If-Match": '"g42"'},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROFILE_EDGE_NOT_FOUND"
    assert "e_deadbeefdeadbeef" not in response.text  # 식별자가 되돌아 나오지 않는다


async def test_a_stale_precondition_conflicts_with_the_latest_version(
    client: httpx.AsyncClient,
) -> None:
    """`409 PROFILE_VERSION_CONFLICT` + **`error.detail.graphVersion`** (§3.9).

    ⚠️ `409` 의 기본 코드는 `STREAM_IN_PROGRESS` 다 — 이 단언이 그 함정을 잡는다.
    """
    await _seed()

    response = await client.patch(EDGE, json={"predicate": "avoids"}, headers={"If-Match": '"g41"'})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROFILE_VERSION_CONFLICT"
    assert error["detail"] == {"graphVersion": "g42"}


async def test_editing_a_purchased_item_is_refused(client: httpx.AsyncClient) -> None:
    """구매 이력 파생은 수정 불가 — `409 PROFILE_EDGE_NOT_EDITABLE` (§3.9.1)."""
    store = await get_profile_store()
    bought = _edge("애플", predicate="purchased")
    await store.set_graph(
        str(USER),
        GraphDocument(
            revision=42,
            nodes=[GraphNode(node_id="brand:애플", type="brand", label="애플", verified=False)],
            edges=[bought],
            unprojected_count=0,
            truncated=False,
            purged_at=None,
            updated_at=NOW,
            tombstones=[],
        ),
    )

    response = await client.patch(
        f"{GRAPH}/edges/{bought.edge_id}",
        json={"predicate": "avoids"},
        headers={"If-Match": '"g42"'},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROFILE_EDGE_NOT_EDITABLE"


async def test_an_unresolvable_label_is_400(client: httpx.AsyncClient) -> None:
    """어휘 밖 라벨은 `400` — 가까운 대상으로 추측해 붙이지 않는다 (§3.9.1 v0.32.7)."""
    await _seed()

    response = await client.patch(
        EDGE,
        json={"object": {"type": "priceBand", "label": "가성비"}},
        headers={"If-Match": '"g42"'},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_a_replayed_patch_still_answers_with_the_original_item(
    client: httpx.AsyncClient,
) -> None:
    """수정 → 그 항목 삭제 → 원래 `If-Match` 로 재시도해도 **최초 응답 그대로**다.

    네트워크 재시도가 지연 도착하는 실제 창이고, 원장이 투영된 항목을 들지 않으면 여기서
    조립할 대상이 없어 `404`·`500` 이 나간다.
    """
    await _seed()
    first = (
        await client.patch(EDGE, json={"predicate": "avoids"}, headers={"If-Match": '"g42"'})
    ).json()
    await client.delete(f"{GRAPH}/edges/{first['edge']['edgeId']}", headers={"If-Match": '"g43"'})

    replay = await client.patch(EDGE, json={"predicate": "avoids"}, headers={"If-Match": '"g42"'})

    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body["edge"] == first["edge"]
    assert body["edge"]["object"]["label"] == "소니"


# ─────────── If-Match (§3.9 preamble) ───────────


@pytest.mark.parametrize("header", [None, "", "*", 'W/"g42"'])
async def test_a_missing_or_weak_precondition_is_400(
    client: httpx.AsyncClient, header: str | None
) -> None:
    """`*`·약한 태그·누락·빈 값은 `400` — "정확히 이 버전"이라는 보장을 무너뜨린다.

    Spring 이 선-400 으로 먼저 거르지만 **심층 방어로 남긴다**(프록시를 거치지 않는 호출이 있다).
    """
    await _seed()
    headers = {} if header is None else {"If-Match": header}

    response = await client.delete(EDGE, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_quoted_and_bare_preconditions_are_equivalent(client: httpx.AsyncClient) -> None:
    """`If-Match: "g42"` 와 `If-Match: g42` 는 동등하다 (§3.9)."""
    await _seed()

    response = await client.delete(EDGE, headers={"If-Match": "g42"})

    assert response.status_code == 200


# ─────────── I-34 삭제 (§3.9.2) ───────────


async def test_delete_echoes_the_target_and_the_new_version(client: httpx.AsyncClient) -> None:
    """응답 4키 — `edgeId` 는 **요청 대상**을 그대로 돌려준다."""
    await _seed()

    body = (await client.delete(EDGE, headers={"If-Match": '"g42"'})).json()

    assert set(body) == {"userId", "graphVersion", "edgeId", "replayed"}
    assert body["edgeId"] == SONY
    assert body["replayed"] is False
    assert body["graphVersion"] == "g43"


async def test_resending_the_same_delete_replays_instead_of_404(
    client: httpx.AsyncClient,
) -> None:
    """**중복 클릭·네트워크 재시도가 오류 화면이 되지 않는다** (AC-PGRAPH-16).

    멱등 판정이 edge 존재 여부가 아니라 **파생 키** 기준이라 원문이 이미 없어도 `200` 이다.
    """
    await _seed()
    first = await client.delete(EDGE, headers={"If-Match": '"g42"'})

    second = await client.delete(EDGE, headers={"If-Match": '"g42"'})

    assert first.status_code == second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["graphVersion"] == first.json()["graphVersion"]  # 버전 불변


async def test_deleting_again_after_a_refetch_is_404(client: httpx.AsyncClient) -> None:
    """새로 조회한 뒤의 삭제만 `404` 다 — `If-Match` 가 달라 파생 키가 다르다 (AC-PGRAPH-16).

    이 둘을 "이미 삭제된 edge 는 404" 로 뭉치면 멱등 규약과 정면 모순이다.
    """
    await _seed()
    await client.delete(EDGE, headers={"If-Match": '"g42"'})

    response = await client.delete(EDGE, headers={"If-Match": '"g43"'})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROFILE_EDGE_NOT_FOUND"


async def test_the_deleted_item_is_gone_from_the_very_next_read(
    client: httpx.AsyncClient,
) -> None:
    """삭제 응답이 돌아온 시점에 이미 목록에서 빠져 있다 (AC-PGRAPH-13).

    유예를 기다리는 스윕이 존재하지 않는다.
    """
    await _seed()

    await client.delete(EDGE, headers={"If-Match": '"g42"'})

    assert (await client.get(GRAPH)).json()["edges"] == []


# ─────────── I-36 초기화 (§3.9.4) ───────────


async def test_reset_reports_two_counts(client: httpx.AsyncClient) -> None:
    """`purged` 는 **정확히 2키**다 — 화면 문구는 "취향 N건 · 대화 기록 N건"이면 충분하다."""
    await _seed()

    body = (
        await client.post(f"{GRAPH}/reset", json={"scope": "ALL"}, headers={"If-Match": '"g42"'})
    ).json()

    assert set(body) == {"userId", "graphVersion", "purged", "personalization", "replayed"}
    assert set(body["purged"]) == {"edges", "transcriptTurns"}
    assert body["purged"]["edges"] == 1


async def test_reset_without_a_profile_is_a_normal_200(client: httpx.AsyncClient) -> None:
    """프로필이 없어도 오류가 아니다 — `purged` 전부 0. `404` 는 이 경로에 없다."""
    response = await client.post(
        f"{GRAPH}/reset", json={"scope": "ALL"}, headers={"If-Match": '"g0"'}
    )

    assert response.status_code == 200
    assert response.json()["purged"] == {"edges": 0, "transcriptTurns": 0}


async def test_reset_does_not_flip_the_personalization_switch(client: httpx.AsyncClient) -> None:
    """초기화와 중지는 **별개 제어**다 — 지우고도 계속 쓰거나, 끄고도 데이터를 남길 수 있어야 한다."""
    await _seed()
    await graph_journal.set_personalization(
        user_id=USER, enabled=False, request_id="req-0", now=NOW
    )

    body = (
        await client.post(f"{GRAPH}/reset", json={"scope": "ALL"}, headers={"If-Match": '"g42"'})
    ).json()

    assert body["personalization"] == {"enabled": False}


@pytest.mark.parametrize(
    "payload", [{}, {"scope": "EVERYTHING"}, {"scope": "ALL", "extra": 1}, {"scope": "all"}]
)
async def test_reset_requires_the_exact_scope_word(
    client: httpx.AsyncClient, payload: dict
) -> None:
    """`scope` 는 **파괴 범위를 호출자가 명시적으로 이름 붙이게** 하는 판별자다."""
    await _seed()

    response = await client.post(f"{GRAPH}/reset", json=payload, headers={"If-Match": '"g42"'})

    assert response.status_code == 400


# ─────────── I-37 중지·재개 (§3.9.5) ───────────


async def test_personalization_toggle_returns_the_new_state(client: httpx.AsyncClient) -> None:
    await _seed()

    body = (
        await client.put(f"/internal/profile/{USER}/personalization", json={"enabled": False})
    ).json()

    assert set(body) == {"userId", "graphVersion", "personalization", "replayed"}
    assert body["personalization"] == {"enabled": False}


async def test_the_toggle_works_without_a_precondition(client: httpx.AsyncClient) -> None:
    """`If-Match` 는 이 API 만 선택이다 — 프라이버시 스위치가 백그라운드 작업에 잠기면 안 된다."""
    response = await client.put(
        f"/internal/profile/{USER}/personalization", json={"enabled": False}
    )

    assert response.status_code == 200


async def test_turning_it_back_on_is_not_blocked(client: httpx.AsyncClient) -> None:
    """껐다 켜기가 같은 `If-Match` 를 들고 와도 막히지 않는다 — 토글은 마지막 의사가 이긴다."""
    await _seed()
    url = f"/internal/profile/{USER}/personalization"
    await client.put(url, json={"enabled": False}, headers={"If-Match": '"g42"'})

    body = (await client.put(url, json={"enabled": True}, headers={"If-Match": '"g42"'})).json()

    assert body["personalization"] == {"enabled": True}


@pytest.mark.parametrize("payload", [{}, {"enabled": "true"}, {"enabled": 1}, {"Enabled": False}])
async def test_the_toggle_rejects_coercible_bodies(
    client: httpx.AsyncClient, payload: dict
) -> None:
    response = await client.put(f"/internal/profile/{USER}/personalization", json=payload)

    assert response.status_code == 400


# ─────────── 인증 (§2.3 b) ───────────


def _jwks_settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="jwks",
        jwks_url="https://spring.test/.well-known/jwks.json",
        pii_hash_pepper="test-pepper",
        internal_api_token="right-token",
        google_api_key="test-google-key",
    )


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong-token"}])
async def test_every_route_needs_the_service_token(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, headers: dict
) -> None:
    """토큰 없음·불일치는 `401 INTERNAL_TOKEN_INVALID` — 운영은 fail-closed 다."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)

    response = await client.get(GRAPH, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"


async def test_the_right_token_passes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """항상-거부로 위 테스트가 통과하지 않음을 고정한다."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)

    response = await client.get(GRAPH, headers={"X-Internal-Token": "right-token"})

    assert response.status_code == 200


async def test_a_bearer_token_is_not_a_credential_here(client: httpx.AsyncClient) -> None:
    """**신원은 경로에서만 온다** — 남의 JWT 를 실어도 경로 `userId` 가 이긴다 (§3.8 [HARD])."""
    await _seed()

    plain = (await client.get(GRAPH)).json()
    with_jwt = (
        await client.get(GRAPH, headers={"Authorization": "Bearer someone-elses-token"})
    ).json()

    assert plain == with_jwt


async def test_no_route_ever_answers_403(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/internal/**` 에 `403` 은 존재하지 않는다 — 서비스 토큰 필터 하나로만 지킨다."""
    monkeypatch.setattr(deps, "get_settings", _jwks_settings)

    statuses = {
        (await client.get(GRAPH)).status_code,
        (await client.patch(EDGE, json={"predicate": "avoids"})).status_code,
        (await client.delete(EDGE)).status_code,
        (await client.post(f"{GRAPH}/reset", json={"scope": "ALL"})).status_code,
        (
            await client.put(f"/internal/profile/{USER}/personalization", json={"enabled": False})
        ).status_code,
    }

    assert 403 not in statuses
