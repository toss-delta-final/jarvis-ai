"""개인화 그래프 API 표면 I-32~I-37 (레인 b, api-spec §3.8·§3.9).

마이페이지 **"AI가 이해한 내 취향"** 화면의 데이터 소스이자 그 화면의 수정·삭제·초기화·중지
경로다. FE 는 AI 를 직접 호출하지 않는다 — 조회·변경 모두 Spring 이 프록시하고(`M-11`~`M-16`),
인증은 `X-Internal-Token` 하나다.

**신원은 경로에서만 온다.** `{userId}` 는 Spring 이 자기 로그인 세션에서 도출한 값이며, 본문·
쿼리로 신원을 받지 않는다(IDOR 방지). `Authorization` 헤더는 이 표면의 자격 증명이 아니다.

**`/internal/**` 에 `403` 은 없다** — 서비스 토큰 필터 하나로만 지키며 역할 개념이 없다.
실패는 `401 INTERNAL_TOKEN_INVALID` 다.

**[HARD] 요청 경로 LLM 0회.** 조회는 저장 문서의 결정론적 파생이고 변경은 잠금 + 문서 재작성이다.

상태 코드 매핑은 여기 없다 — 도메인 예외만 던지고 `app/core/errors.py` 의 `_GRAPH_ERROR_MAP` 이
옮긴다. 라우터가 코드를 기억하게 하면 `409` 하나만 빠뜨려도 FE 에 "스트림 진행 중"이 나가고,
그 결함은 정상 경로 테스트로 잡히지 않는다(§3.9 구현 노트 1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Request, Response

from app.agents.profile import graph_journal
from app.agents.profile.graph_errors import GraphEdgeNotFound
from app.agents.profile.graph_projection import project_edge, project_edges
from app.agents.profile.resolver import ObjectSpec
from app.agents.profile.store import get_profile_store
from app.api.deps import verify_service_token
from app.core.config import Settings, get_settings
from app.core.errors import get_request_id
from app.core.text import _strip_unsafe_multiline
from app.schemas.profile_graph import (
    DeleteGraphEdgeResponse,
    GraphPurgedView,
    GraphResetRequest,
    GraphResetResponse,
    PatchGraphEdgeRequest,
    PatchGraphEdgeResponse,
    PersonalizationRequest,
    PersonalizationResponse,
    PersonalizationView,
    ProfileGraphView,
)

router = APIRouter(tags=["internal-profile-graph"])

_BIGINT_MAX = 2**63 - 1


def _now_iso() -> str:
    """저장 모델과 **같은 포맷**이어야 한다 — `builder._now_iso` 와 동일(정렬 키에 쓰인다)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": message})


def _if_match_is_usable(value: str) -> bool:
    """`"g42"` 와 `g42` 는 동등하고, `*`·약한 태그·빈 값은 못 쓴다 (api-spec §3.9).

    *"정확히 이 버전"* 이라는 보장을 무너뜨리기 때문이다(`428` 은 도입하지 않는다). Spring 이
    선-400 으로 먼저 거르지만 **심층 방어로 남긴다** — 프록시를 거치지 않는 통합 테스트·목·직접
    호출이 있다.
    """
    return bool(value.strip()) and value.strip() != "*" and not value.strip().startswith("W/")


def require_if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> str:
    """§3.9.1·§3.9.2·§3.9.4 — 필수."""
    if if_match is None or not _if_match_is_usable(if_match):
        raise _bad_request("If-Match 헤더가 필요합니다")
    return if_match


def optional_if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> str | None:
    """§3.9.5 — 선택. **보내면 검증한다.**

    이 API 만 선택인 이유는 토글이 마지막 의사가 이기는 게 자연스럽고, 충돌로 막으면
    **프라이버시 스위치가 잠기는** 상황이 생기기 때문이다.
    """
    if if_match is not None and not _if_match_is_usable(if_match):
        raise _bad_request("If-Match 헤더 형식이 올바르지 않습니다")
    return if_match


def _user_id(user_id: int = Path(..., ge=1, le=_BIGINT_MAX)) -> int:
    """경로 `{userId}` — 양의 BIGINT. 범위 밖·비정수는 `RequestValidationError` → `400` 이다."""
    return user_id


async def _within(budget_s: float, coro):  # noqa: ANN001 - 임의 코루틴을 감싼다
    """응답 예산 (api-spec §3.8 2s / §3.9 3s).

    초과는 `504 UPSTREAM_TIMEOUT` 이다. 저장소가 **즉시** 실패하면 예산 안에 잡혀 `503` 이고,
    **느리게** 실패하면 여기서 끊겨 `504` 다 — "예산 초과 = 504" 가 계약의 뜻이다.

    취소 안전성: per-user 잠금은 `pg_advisory_xact_lock` 이라 취소·연결 끊김에 자동 해제되고,
    의도 기록과 문서 쓰기 사이에서 끊기면 원장 `processing` 잔재가 남아 다음 재시도가 재개한다.
    """
    try:
        return await asyncio.wait_for(coro, timeout=budget_s)
    except (TimeoutError, asyncio.TimeoutError) as exc:  # noqa: UP041 - 3.12 별칭 명시
        raise HTTPException(
            status_code=504, detail={"code": "UPSTREAM_TIMEOUT", "message": "상류 응답 지연"}
        ) from exc


def _to_spec(body: PatchGraphEdgeRequest) -> ObjectSpec | None:
    """요청 `object` → 해석 입력. 생략이면 `None`("대상 유지")이다."""
    if body.object is None:
        return None
    return ObjectSpec(
        node_id=body.object.node_id, node_type=body.object.type, label=body.object.label
    )


# ─────────── I-32 조회 (§3.8) ───────────


@router.get("/internal/profile/{user_id}/graph", response_model=ProfileGraphView)
async def get_profile_graph(
    response: Response,
    user_id: int = Depends(_user_id),
    _token: None = Depends(verify_service_token),
    settings: Settings = Depends(get_settings),
) -> ProfileGraphView:
    """취향 목록 + 요약 마크다운 (I-32).

    **프로필이 없어도 오류가 아니다** — `exists: false` + 빈 배열로 `200` 이다. 신규 회원이나
    아직 취향이 쌓이지 않은 회원의 정상 상태이며 `404` 는 이 경로에 없다.

    **개인화 중지 중에도 200 + 전체 그래프**를 낸다. 중지는 "쓰지 않는다"이지 "숨긴다"가 아니고,
    보존된 데이터를 검토하고 지우려면 볼 수 있어야 한다. **`exists` 도 그대로 `true` 다** — 중지
    회원의 프로필은 존재하고 보존된다.

    다만 **`markdown` 은 중지 중 `null` 이다** — 요약은 소비 산출물이라 "사용 중지"의 대상이고,
    #359 가 rerank·홈 벡터·마이페이지 세 소비처를 같은 규칙으로 막았다(REQ-PGRAPH-100). 지울
    대상을 보는 데는 `edges` 로 충분하다.
    """
    return await _within(
        settings.profile_graph_read_budget_s, _load_graph(user_id, response, settings)
    )


async def _load_graph(user_id: int, response: Response, settings: Settings) -> ProfileGraphView:
    store = await get_profile_store()
    document = await store.get_graph(str(user_id))
    enabled = await graph_journal.get_personalization_flag(user_id=user_id)
    # **`reader.read_profile_summary` 를 쓰지 않는다** — 그쪽은 중지 시 `None` 을 돌려주는
    # 소비 게이트(#359, REQ-PGRAPH-100)라 여기 쓰면 `exists` 까지 `false` 가 된다. 중지 회원의
    # 프로필은 **존재하고 보존된다**(§3.9.5) — "없다"고 답하면 계약이 거짓이 되고, 사용자가
    # 지울 것이 있는지조차 알 수 없다. 존재 여부는 저장소에서 직접 읽고, **사용 차단은 아래
    # `markdown` 을 비우는 것으로** 집행한다.
    summary = await store.get_summary(str(user_id))

    version = graph_journal.graph_version(document.revision) if document else "g0"
    # `ETag` 는 **편의 사본**이고 정규 출처는 본문 `graphVersion` 이다 — 응답 헤더는 CORS 제약으로
    # 브라우저 JS 가 읽지 못한다(api-spec §3.8).
    response.headers["ETag"] = f'"{version}"'
    return ProfileGraphView(
        user_id=user_id,
        # 「승격된 프로필 존재 여부」 — 요약 유무가 기준이다(§3.4 선례와 동일).
        # **중지는 존재 여부를 바꾸지 않는다** — 데이터는 보존되고 재개하면 복원된다.
        exists=summary is not None,
        # 중지 중에는 **요약을 내보내지 않는다** — "사용 중지"의 집행이다(#359 REQ-PGRAPH-100 이
        # rerank·홈 벡터·마이페이지 세 소비처를 같은 규칙으로 막는다). `edges` 는 그대로 나간다:
        # 중지는 "쓰지 않는다"이지 "숨긴다"가 아니고, 보존된 데이터를 지우려면 볼 수 있어야 한다.
        markdown=_strip_unsafe_multiline(summary.markdown) if summary and enabled else None,
        generated_at=summary.generated_at if summary and enabled else None,
        graph_version=version,
        personalization=PersonalizationView(enabled=enabled),
        edges=project_edges(document, settings=settings),
    )


# ─────────── I-33 수정 (§3.9.1) ───────────


@router.patch(
    "/internal/profile/{user_id}/graph/edges/{edge_id}", response_model=PatchGraphEdgeResponse
)
async def patch_graph_edge(
    body: PatchGraphEdgeRequest,
    http_request: Request,
    user_id: int = Depends(_user_id),
    edge_id: str = Path(..., min_length=1),
    if_match: str = Depends(require_if_match),
    _token: None = Depends(verify_service_token),
    settings: Settings = Depends(get_settings),
) -> PatchGraphEdgeResponse:
    """취향 수정 — **원자적 1회 변경** (I-33, REQ-PGRAPH-034).

    구 edge 제거와 대상 edge 고정이 **한 revision·한 감사 행**이다. 두 호출로 나누면 중간 상태와
    충돌 창이 둘 생긴다.

    응답 `edge` 는 §3.8 `edges[]` 항목과 **완전히 같은 모양**이라 FE 가 목록 항목을 통째로
    교체한다. `edgeId` 는 `(predicate, nodeId)` 파생이라 **바뀐다**.
    """
    result = await _within(
        settings.profile_graph_write_budget_s,
        graph_journal.apply_edge_mutation(
            user_id=user_id,
            action="edgeUpdate",
            edge_id=edge_id,
            if_match=if_match,
            request_id=get_request_id(http_request),
            now=_now_iso(),
            predicate=body.predicate,
            object_spec=_to_spec(body),
        ),
    )
    edge = await _edge_view(user_id, result, settings=settings)
    return PatchGraphEdgeResponse(
        user_id=user_id,
        graph_version=result.graph_version,
        edge=edge,
        merged=result.merged,
        replayed=result.replayed,
    )


async def _edge_view(user_id: int, result, *, settings: Settings):  # noqa: ANN001, ANN201
    """응답의 `edge` — **원장이 든 값이 먼저**다 (api-spec §3.9.1 재전송 규약).

    재전송은 *"최초 응답 본문을 그대로"* 여야 하는데, 그 사이 그 edge 가 삭제됐으면 **현재 문서로는
    재구성할 수 없다**. 그래서 원장이 투영된 항목을 들고 있고 여기서 먼저 본다.

    원장에 없으면(no-op 경로는 claim 을 되돌려 아무것도 안 남긴다) 현재 문서에서 조립한다 —
    no-op 은 정의상 문서가 안 바뀌었으므로 그 값이 정확하다. 둘 다 없으면 대상이 사라진 것이니
    `404` 가 `500` 보다 정직한 답이다.
    """
    from app.schemas.profile_graph import GraphEdgeView

    if result.edge is not None:
        return GraphEdgeView.model_validate(result.edge)
    store = await get_profile_store()
    document = await store.get_graph(str(user_id))
    view = project_edge(document, result.edge_id or "", settings=settings)
    if view is None:
        raise GraphEdgeNotFound(result.edge_id or "")
    return view


# ─────────── I-34 삭제 (§3.9.2) ───────────


@router.delete(
    "/internal/profile/{user_id}/graph/edges/{edge_id}", response_model=DeleteGraphEdgeResponse
)
async def delete_graph_edge(
    http_request: Request,
    user_id: int = Depends(_user_id),
    edge_id: str = Path(..., min_length=1),
    if_match: str = Depends(require_if_match),
    _token: None = Depends(verify_service_token),
    settings: Settings = Depends(get_settings),
) -> DeleteGraphEdgeResponse:
    """개별 삭제 — **즉시 물리 삭제** (I-34). Body 없음.

    되돌리기 단계가 없다. 재파생 차단 표식만 영구히 남아 다음 배치가 같은 취향을 다시 만들어도
    부활하지 않는다.

    **같은 `If-Match` 재전송은 원문이 이미 없어도 `200` + `replayed: true`** 다 — 멱등 판정이
    edge 존재 여부가 아니라 파생 키 기준이기 때문이다. 새로 조회한 뒤 다시 삭제하면 `If-Match` 가
    달라 `404` 다. 두 경우를 "이미 삭제된 edge 는 404" 로 뭉치면 멱등 규약과 정면 모순이다.
    """
    result = await _within(
        settings.profile_graph_write_budget_s,
        graph_journal.apply_edge_mutation(
            user_id=user_id,
            action="edgeDelete",
            edge_id=edge_id,
            if_match=if_match,
            request_id=get_request_id(http_request),
            now=_now_iso(),
        ),
    )
    return DeleteGraphEdgeResponse(
        user_id=user_id,
        graph_version=result.graph_version,
        # 순수 변형이 `edge_id_after=None` 을 돌려주므로 **요청 대상을 echo** 한다
        # (계약 정의 = "삭제한 edge", api-spec §3.9.2).
        edge_id=edge_id,
        replayed=result.replayed,
    )


# ─────────── I-36 전체 초기화 (§3.9.4) ───────────


@router.post("/internal/profile/{user_id}/graph/reset", response_model=GraphResetResponse)
async def reset_profile_graph(
    http_request: Request,
    body: GraphResetRequest = Body(...),
    user_id: int = Depends(_user_id),
    if_match: str = Depends(require_if_match),
    _token: None = Depends(verify_service_token),
    settings: Settings = Depends(get_settings),
) -> GraphResetResponse:
    """전체 초기화 — **파괴적·비가역** (I-36).

    지우는 것: 그래프 node·edge · fact · 요약(마크다운·임베딩) · 재파생 차단 표식 · 미처리 세션
    버퍼 · 누적 게이트 상태 · **대화 전사록**. 남는 것은 **변경 감사 로그** 하나다 — 지우면 파괴
    동작 자체가 추적 불가가 된다.

    **프로필이 없어도 오류가 아니다** — `purged` 전부 0 으로 `200` 이고 `404` 는 이 경로에 없다.
    `personalization` 은 **바꾸지 않는다**(초기화와 중지는 별개 제어다).
    """
    del body  # `scope` 는 판별자다 — 값 자체는 Literal 이 이미 검증했다
    result = await _within(
        settings.profile_graph_write_budget_s,
        graph_journal.reset_graph(
            user_id=user_id,
            if_match=if_match,
            request_id=get_request_id(http_request),
            now=_now_iso(),
        ),
    )
    purged = result.purged or {}
    return GraphResetResponse(
        user_id=user_id,
        graph_version=result.graph_version,
        purged=GraphPurgedView(
            edges=purged.get("edges", 0), transcript_turns=purged.get("transcriptTurns", 0)
        ),
        personalization=PersonalizationView(
            enabled=await graph_journal.get_personalization_flag(user_id=user_id)
        ),
        replayed=result.replayed,
    )


# ─────────── I-37 중지·재개 (§3.9.5) ───────────


@router.put("/internal/profile/{user_id}/personalization", response_model=PersonalizationResponse)
async def set_personalization(
    body: PersonalizationRequest,
    http_request: Request,
    user_id: int = Depends(_user_id),
    if_match: str | None = Depends(optional_if_match),
    _token: None = Depends(verify_service_token),
    settings: Settings = Depends(get_settings),
) -> PersonalizationResponse:
    """개인화 중지·재개 (I-37).

    끄면 **사용과 수집이 동시에 멈추고 데이터는 보존**된다 — 지우려면 I-36 이다. 중지 중에도
    조회는 `200` 이고 모든 정리 동작(I-33·I-34·I-36)이 허용된다. 보존된 데이터를 지우려고
    개인화를 다시 켜야 하는 상황을 만들지 않는다.

    같은 값 재전송은 no-op 이다 — 감사 행 없음, 버전 불변, `replayed: true`.
    """
    result = await _within(
        settings.profile_graph_write_budget_s,
        graph_journal.set_personalization(
            user_id=user_id,
            enabled=body.enabled,
            request_id=get_request_id(http_request),
            now=_now_iso(),
            if_match=if_match,
        ),
    )
    return PersonalizationResponse(
        user_id=user_id,
        graph_version=result.graph_version,
        personalization=PersonalizationView(
            enabled=await graph_journal.get_personalization_flag(user_id=user_id)
        ),
        replayed=result.replayed,
    )
