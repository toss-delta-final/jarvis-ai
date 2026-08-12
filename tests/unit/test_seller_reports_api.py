"""R-1 / R-2 보고서 조회 API (이슈 #599) — 완료 조건 대응.

이슈가 요구한 두 가지를 고정한다.
  ① 페이지가 **본문 파싱 없이** 세그먼트·추천 표를 그릴 수 있는 응답 구조 스냅샷
  ② `noReportReason` 각 케이스의 판정

라우트 함수를 직접 호출한다(`test_seller_api.py` 관행) — HTTP 서버·실 DB 없음.
⚠️ 직접 호출이라 `Query(default=...)` 기본값이 적용되지 않는다(기본값 객체가 그대로
들어온다). 모든 인자를 명시해서 부른다.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest

from app.agents.seller.analysis_records import RecommendationRecord, ReportRecord
from app.agents.seller.analysis_store import TargetStatus
from app.api import seller as seller_api
from app.core.auth import Identity

_IDENTITY = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")
_BRAND_ID = 3
_REPORT_ID = UUID("0f8c1d2e-0000-4000-8000-000000000001")
_CREATED_AT = dt.datetime(2026, 8, 10, 4, 12, tzinfo=dt.UTC)


def _report(**over) -> ReportRecord:
    base = dict(
        id=_REPORT_ID,
        brand_id=_BRAND_ID,
        trigger_type="scheduled_daily",
        period_from=dt.date(2026, 8, 8),
        period_to=dt.date(2026, 8, 9),
        compared_from=dt.date(2026, 8, 1),
        compared_to=dt.date(2026, 8, 7),
        title="8월 9일 일간 분석",
        summary="구매 전환율이 전주 대비 감소했습니다.",
        report_md="## 데이터 분석\n전환율이 내렸다.\n## 원인 분석\n장바구니 이탈.",
        segments=[
            {
                "rule_label": "구매망설임형",
                "display_label": "구매망설임형",
                "llm_label": "장바구니 완주 실패군",
                "llm_desc": "결제 완주율이 낮은 고객군.",
                "size": 128,
                "delta_size": 12,
                "centroid_stats": {"cart_adds": 8.4, "order_count": 0.3},
            }
        ],
        findings=[
            {
                "analysis_type": "conversion",
                "severity": "warning",
                "summary": "전환율이 유의하게 감소했습니다.",
                "evidence": ["two_proportion_z p=0.012"],
                "recommendation": "장바구니 이탈 구간 점검",
            }
        ],
        holds=[{"step": "compare_previous", "reason": "스냅샷 한 개뿐 — 비교 보류"}],
        verified=True,
        score_total=24,
        attempts=1,
        created_at=_CREATED_AT,
        read_at=None,
    )
    base.update(over)
    return ReportRecord(**base)


def _rec(rank: int, **over) -> RecommendationRecord:
    base = dict(
        id=uuid4(),
        report_id=_REPORT_ID,
        brand_id=_BRAND_ID,
        rank=rank,
        action_type="product_visibility",
        target_kind="product",
        segment_label="",
        product_ids=[100 + rank],
        title=f"추천 {rank}",
        rationale="근거",
        expected_effect="기대 효과",
        effectiveness_score=0.5,
        status="proposed",
    )
    base.update(over)
    return RecommendationRecord(**base)


@pytest.fixture(autouse=True)
def _mute_target_hook(monkeypatch: pytest.MonkeyPatch):
    """targets 훅은 fire-and-forget 이라 테스트에서 태스크만 남긴다 — no-op 로 막는다."""
    monkeypatch.setattr(seller_api, "note_seller_seen", lambda _ctx: None)


def _patch_store(monkeypatch: pytest.MonkeyPatch, **fns) -> None:
    for name, fn in fns.items():
        monkeypatch.setattr(seller_api.analysis_store, name, fn)


# ── R-1 ────────────────────────────────────────────────────────────────────────


async def _call_r1(*, limit=20, offset=0, unread_only=False):
    return await seller_api.seller_reports(
        identity=_IDENTITY, limit=limit, offset=offset, unread_only=unread_only
    )


async def test_r1_item_shape_is_camel_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """목록 카드가 필요한 필드가 camelCase 로 전부 실린다."""
    _patch_store(
        monkeypatch,
        list_reports=_async([_report()]),
        count_reports=_async(14),
        count_recommendations_by_reports=_async({_REPORT_ID: 3}),
        get_target_status=_async(None),
    )
    body = (await _call_r1()).model_dump(by_alias=True)

    assert body["total"] == 14
    assert body["unreadCount"] == 14  # count_reports 스텁이 두 호출 모두를 받는다
    assert body["noReportReason"] is None
    assert set(body["items"][0]) == {
        "reportId",
        "triggerType",
        "periodFrom",
        "periodTo",
        "title",
        "summary",
        "recommendationCount",
        "hasHolds",
        "createdAt",
        "readAt",
    }
    item = body["items"][0]
    assert item["reportId"] == str(_REPORT_ID)  # UUID 는 문자열
    assert item["recommendationCount"] == 3
    assert item["hasHolds"] is True
    assert item["createdAt"] == "2026-08-10T04:12:00Z"  # 확정 3 — Z 표기
    assert item["readAt"] is None


async def test_r1_has_holds_false_when_no_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_store(
        monkeypatch,
        list_reports=_async([_report(holds=[])]),
        count_reports=_async(1),
        count_recommendations_by_reports=_async({}),
        get_target_status=_async(None),
    )
    body = (await _call_r1()).model_dump(by_alias=True)
    assert body["items"][0]["hasHolds"] is False
    assert body["items"][0]["recommendationCount"] == 0  # 키 없으면 0


async def test_r1_total_follows_filter_but_unread_count_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`total` 은 필터 적용 후, `unreadCount` 는 필터 무관 전량 — 배지가 탭 따라 흔들리면 안 된다."""
    seen: list[bool] = []

    async def _count(_brand_id, *, unread_only=False):
        seen.append(unread_only)
        return 2 if unread_only else 14

    _patch_store(
        monkeypatch,
        list_reports=_async([_report()]),
        count_reports=_count,
        count_recommendations_by_reports=_async({}),
        get_target_status=_async(None),
    )
    body = (await _call_r1(unread_only=True)).model_dump(by_alias=True)

    assert body["total"] == 2
    assert body["unreadCount"] == 2
    assert seen == [True, True]  # 필터 호출 + 배지 호출


async def test_r1_passes_paging_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def _list(brand_id, *, limit, offset=0, unread_only=False, before=None):
        captured.update(
            brand_id=brand_id, limit=limit, offset=offset, unread_only=unread_only, before=before
        )
        return []

    _patch_store(
        monkeypatch,
        list_reports=_list,
        count_reports=_async(0),
        count_recommendations_by_reports=_async({}),
        get_target_status=_async(None),
    )
    await _call_r1(limit=5, offset=10, unread_only=True)

    assert captured == {
        "brand_id": _BRAND_ID,
        "limit": 5,
        "offset": 10,
        "unread_only": True,
        "before": None,
    }


# ── noReportReason (이슈 완료 조건 ②) ───────────────────────────────────────────


def _target(**over) -> TargetStatus:
    base = dict(
        brand_id=_BRAND_ID,
        last_seen_at=dt.datetime.now(dt.UTC),
        last_run_at=dt.datetime.now(dt.UTC),
        last_skip_reason=None,
    )
    base.update(over)
    return TargetStatus(**base)


async def _reason_for(monkeypatch: pytest.MonkeyPatch, target_or_exc) -> str | None:
    async def _get(_brand_id):
        if isinstance(target_or_exc, Exception):
            raise target_or_exc
        return target_or_exc

    _patch_store(
        monkeypatch,
        list_reports=_async([]),
        count_reports=_async(0),
        count_recommendations_by_reports=_async({}),
        get_target_status=_get,
    )
    return (await _call_r1()).model_dump(by_alias=True)["noReportReason"]


async def test_reason_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _reason_for(monkeypatch, None) == "not_registered"


async def test_reason_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    assert await _reason_for(monkeypatch, _target(last_seen_at=old)) == "inactive"


async def test_reason_pending_first_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """등록됐으나 배치 미실행 — `not_registered`(사고)와 갈라야 한다(확정 2)."""
    assert await _reason_for(monkeypatch, _target(last_run_at=None)) == "pending_first_run"


@pytest.mark.parametrize("skip", ["no_trigger", "no_baseline"])
async def test_reason_from_last_skip_reason(monkeypatch: pytest.MonkeyPatch, skip: str) -> None:
    assert await _reason_for(monkeypatch, _target(last_skip_reason=skip)) == skip


async def test_reason_null_when_skip_reason_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """알 수 없는 값을 `no_trigger`(이상 없음)로 **추정하지 않는다**."""
    assert await _reason_for(monkeypatch, _target(last_skip_reason="???")) is None


async def test_reason_null_when_columns_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`06_*.sql` 미적용(컬럼 부재)이어도 R-1 은 죽지 않고 null 을 낸다 — 이슈 방어 조항."""
    assert await _reason_for(monkeypatch, RuntimeError("column last_run_at does not exist")) is None


async def test_reason_absent_when_list_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """목록이 차 있으면 사유는 항상 null — 판정 자체를 하지 않는다."""

    async def _boom(_brand_id):
        raise AssertionError("목록이 비지 않았는데 사유를 판정했다")

    _patch_store(
        monkeypatch,
        list_reports=_async([_report()]),
        count_reports=_async(1),
        count_recommendations_by_reports=_async({}),
        get_target_status=_boom,
    )
    assert (await _call_r1()).model_dump(by_alias=True)["noReportReason"] is None


# ── R-2 ────────────────────────────────────────────────────────────────────────


def _patch_r2(monkeypatch: pytest.MonkeyPatch, report, recs, marked: list | None = None) -> None:
    async def _get_report(report_id, *, brand_id):
        return report if (report is not None and brand_id == _BRAND_ID) else None

    async def _list_recs(report_id, *, brand_id):
        return list(recs)

    async def _mark(report_id, *, brand_id):
        if marked is not None:
            marked.append((report_id, brand_id))

    _patch_store(
        monkeypatch,
        get_report=_get_report,
        list_recommendations_by_report=_list_recs,
        mark_report_read=_mark,
    )


async def test_r2_shape_covers_issue_and_fe_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """이슈 #599 요구 필드와 FE `SellerReport` 필드가 **동시에** 실린다(확정 4)."""
    _patch_r2(monkeypatch, _report(), [_rec(1)])
    body = await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY)

    # 이슈 문구 쪽
    for key in ("reportMd", "summary", "segments", "recommendations", "holds"):
        assert key in body, key
    assert body["periodFrom"] == "2026-08-08"
    assert body["periodTo"] == "2026-08-09"
    assert body["comparedFrom"] == "2026-08-01"
    assert body["comparedTo"] == "2026-08-07"

    # FE 쪽 (AnalysisReport.tsx 무수정 재사용 조건)
    assert body["period"] == {"from": "2026-08-08", "to": "2026-08-09"}
    assert body["comparedPeriod"] == {"from": "2026-08-01", "to": "2026-08-07"}
    assert body["title"] == "8월 9일 일간 분석"  # 고정 문구가 아니라 저장 제목
    assert body["generatedAt"] == "2026-08-10T04:12:00Z"
    assert body["findings"], "findings 가 비면 FE 가 구버전 폴백 모드로 떨어진다"


async def test_r2_report_md_matches_masked_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """`reportMd` 는 원본이 아니라 마스킹된 `body` 복사본이다 — 두 필드가 갈리면 유출 경로."""
    _patch_r2(monkeypatch, _report(), [])
    body = await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY)
    assert body["reportMd"] == body["body"]
    assert "데이터 분석" in body["body"]


async def test_r2_segments_are_camel_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """페이지가 **본문 파싱 없이** 세그먼트 표를 그릴 수 있어야 한다(완료 조건 ①)."""
    _patch_r2(monkeypatch, _report(), [])
    seg = (await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY))["segments"][
        0
    ]
    assert seg["ruleLabel"] == "구매망설임형"
    assert seg["displayLabel"] == "구매망설임형"
    assert seg["llmLabel"] == "장바구니 완주 실패군"
    assert seg["deltaSize"] == 12
    assert seg["centroidStats"] == {"cartAdds": 8.4, "orderCount": 0.3}  # 안쪽 키까지 변환


async def test_r2_recommendation_index_matches_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    """`index` 와 `rank` 가 어긋나면 "N번 적용해줘"가 **다른 추천**을 적용한다."""
    _patch_r2(monkeypatch, _report(), [_rec(3), _rec(1), _rec(2)])  # 일부러 뒤섞어 넣는다
    recs = (await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY))[
        "recommendations"
    ]

    assert [r["index"] for r in recs] == [1, 2, 3]
    assert [r["rank"] for r in recs] == [1, 2, 3]
    assert [r["title"] for r in recs] == ["추천 1", "추천 2", "추천 3"]
    assert [r["productId"] for r in recs] == [101, 102, 103]
    assert [r["productIds"] for r in recs] == [[101], [102], [103]]
    assert recs[0]["status"] == "proposed"
    assert recs[0]["targetKind"] == "product"
    assert recs[0]["rationale"] == "근거"


async def test_r2_holds_appended_to_limitations(monkeypatch: pytest.MonkeyPatch) -> None:
    """판정 보류를 화면에서 지우면 "판정 보류 != 이상 없음" 규약이 와이어에서 깨진다."""
    _patch_r2(monkeypatch, _report(), [])
    body = await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY)
    assert "compare_previous: 스냅샷 한 개뿐 — 비교 보류" in body["limitations"]
    assert body["holds"] == [{"step": "compare_previous", "reason": "스냅샷 한 개뿐 — 비교 보류"}]


async def test_r2_has_no_chart_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """차트는 chart 레인 전용 — 보고서에는 넣지 않는다(빈 값이라 FE 섹션이 사라진다)."""
    _patch_r2(monkeypatch, _report(), [])
    body = await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY)
    assert body["charts"] == []
    assert body["chartRequested"] is False
    assert body["chartUnavailable"] == []


async def test_r2_read_at_is_value_before_marking(monkeypatch: pytest.MonkeyPatch) -> None:
    """방금 바뀐 값을 실으면 화면이 "안 읽음 -> 읽음" 전환을 감지할 수 없다."""
    marked: list = []
    _patch_r2(monkeypatch, _report(read_at=None), [], marked)
    body = await seller_api.seller_report_detail(str(_REPORT_ID), identity=_IDENTITY)

    assert body["readAt"] is None
    assert marked == [(_REPORT_ID, _BRAND_ID)]  # 각인은 실제로 일어났다


async def test_r2_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    _patch_r2(monkeypatch, None, [])
    with pytest.raises(HTTPException) as exc:
        await seller_api.seller_report_detail(str(uuid4()), identity=_IDENTITY)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "REPORT_NOT_FOUND"


async def test_r2_404_for_other_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    """남의 브랜드는 403 이 아니라 404 — 존재 여부를 알려주면 id 열거가 된다."""
    from fastapi import HTTPException

    _patch_r2(monkeypatch, _report(), [])
    other = Identity(user_id=None, is_guest=False, seller_id="9", brand_id="99")
    with pytest.raises(HTTPException) as exc:
        await seller_api.seller_report_detail(str(_REPORT_ID), identity=other)
    assert exc.value.status_code == 404


async def test_r2_400_on_bad_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    _patch_r2(monkeypatch, _report(), [])
    with pytest.raises(HTTPException) as exc:
        await seller_api.seller_report_detail("not-a-uuid", identity=_IDENTITY)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "BAD_REQUEST"


async def test_r2_401_on_non_numeric_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """신원 캐스팅 실패가 500 으로 새면 토큰 문제가 "서버 장애"로 보인다."""
    from fastapi import HTTPException

    _patch_r2(monkeypatch, _report(), [])
    bad = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="abc")
    with pytest.raises(HTTPException) as exc:
        await seller_api.seller_report_detail(str(_REPORT_ID), identity=bad)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "INVALID_SELLER_IDENTITY"


# ── 리팩터 회귀 가드 ────────────────────────────────────────────────────────────


async def test_report_event_still_uses_fixed_title_and_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_report_payload` 추출은 **순수 추출**이다 — 채팅 경로(S-4)의 동작이 변하면 안 된다."""
    from app.agents.seller import report_view

    result = report_view.record_to_pipeline_result(_report(), [_rec(1)])
    payload = seller_api._report_payload(result)  # title/generated_at 미지정 = 채팅 경로

    assert payload["title"] == "판매 분석 보고서"  # 저장 제목이 아니라 고정 문구
    assert payload["generatedAt"] != "2026-08-10T04:12:00Z"  # 저장 시각이 아니라 호출 시각
    assert seller_api._report_event(result).startswith('data: {"type": "report"')


# ── 헬퍼 ───────────────────────────────────────────────────────────────────────


def _async(value):
    async def _fn(*_a, **_kw):
        return value

    return _fn
