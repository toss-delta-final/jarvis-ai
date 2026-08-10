"""등록 초안 preview 빌더 (#506) — 13필드/null/포맷/sections 계약.

[#541] 카테고리 2칸 표기(`categoryMajor`·`categorySubPath`) 추가로 11 → 13 필드
(api-spec-seller §6.1 — 추가 전용이라 기존 소비자는 불변).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.seller import category_catalog
from app.agents.seller.hitl import DraftRecord
from app.agents.seller.preview import build_create_preview, diff_notes
from app.agents.seller.schemas import DraftChange
from app.agents.seller.vision import ProductImageAnalysis
from app.api import seller as seller_api

_PREVIEW_KEYS = {
    "title",
    "imageUrl",
    "imagePlaceholder",
    "priceText",
    "originalPriceText",
    "discountRate",
    "stockText",
    "categoryPath",
    "categoryMajor",
    "categorySubPath",
    "summary",
    "description",
    "sections",
}


@pytest.fixture(autouse=True)
def _catalog(tmp_path, monkeypatch):
    snapshot = tmp_path / "categories.json"
    snapshot.write_text(
        json.dumps(
            [
                {"id": "100", "path": ["패션의류/잡화", "남성의류", "셔츠"]},
                {"id": "101", "path": ["패션의류/잡화", "남성의류", "남방"]},
                # [#541] 정본 스냅샷의 실제 모양(2칸, leaf 가 "중 > 소" 병합형) —
                # 3칸 픽스처만 두면 2칸에서만 드러나는 표기 회귀를 놓친다.
                {"id": "200", "path": ["디지털/가전", "컴퓨터 주변기기 > 키보드"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        category_catalog,
        "get_settings",
        lambda: SimpleNamespace(
            seller_category_snapshot_path=str(snapshot),
            seller_category_candidates_k=5,
            seller_category_write_mode="leaf",
        ),
    )
    category_catalog.reset_catalog_cache()
    yield
    category_catalog.reset_catalog_cache()


def _record(changes: list[DraftChange]) -> DraftRecord:
    return DraftRecord(
        draft_id="d-1",
        op="create",
        product_id=None,
        changes=changes,
        summary="새 상품 1건 등록 초안",
        seller_id=7,
        brand_id=3,
        created_at=datetime.now(UTC).isoformat(),
    )


_ANALYSIS = ProductImageAnalysis(
    name="코튼 오버핏 셔츠",
    summary="면 100% 오버핏 셔츠",
    description="부드러운 면 소재의 오버핏 셔츠입니다.",
    category_hint="셔츠",
    confidence=0.9,
)


def _full_changes(**overrides: str | None) -> list[DraftChange]:
    values: dict[str, str | None] = {
        "name": "코튼 오버핏 셔츠",
        "price": "32000",
        "original_price": "40000",
        "stock_quantity": "50",
        "category": "100",
        "image_url": "https://cdn.example.com/seller/7/ab12.jpg",
        "description": "부드러운 면 소재.",
    }
    values.update(overrides)
    return [
        DraftChange(field=field, before="", after=after)
        for field, after in values.items()
        if after is not None
    ]


def test_preview_keys_always_present_and_formatted() -> None:
    preview = build_create_preview(
        _record(_full_changes()), analysis=_ANALYSIS, seller_inputs="32,000원 / 50개"
    )
    assert set(preview) == _PREVIEW_KEYS  # 키는 항상 13개 — 빠지는 키가 없다(FE 계약)
    assert preview["title"] == "코튼 오버핏 셔츠"
    assert preview["priceText"] == "32,000원"  # 서버 포맷 완료 — FE 재가공 금지 계약
    assert preview["originalPriceText"] == "40,000원"
    assert preview["discountRate"] == 20
    assert preview["stockText"] == "50개"
    assert preview["categoryPath"] == "패션의류/잡화 > 남성의류 > 셔츠"
    assert preview["imageUrl"] == "https://cdn.example.com/seller/7/ab12.jpg"
    assert preview["imagePlaceholder"] is False
    kinds = [s["kind"] for s in preview["sections"]]
    assert kinds == ["source", "warning"]
    assert "판매자 입력: 32,000원 / 50개" in preview["sections"][0]["items"]


@pytest.mark.parametrize(
    ("category_id", "expected_major", "expected_sub"),
    [
        ("100", "패션의류/잡화", "남성의류 > 셔츠"),  # 3칸 스냅샷
        ("200", "디지털/가전", "컴퓨터 주변기기 > 키보드"),  # 2칸 스냅샷(정본 모양)
    ],
)
def test_preview_category_split_into_two_slots(
    category_id: str, expected_major: str, expected_sub: str
) -> None:
    """[#541] 카테고리는 판매자가 채우는 두 칸(대분류 / 중·소분류)으로도 내려간다.

    불변식 `categoryPath == categoryMajor + " > " + categorySubPath` 를 고정한다 —
    FE 가 categoryPath 를 " > " 로 쪼개 칸을 만들면 "패션의류/잡화" 처럼 이름에 슬래시가
    든 대분류나 "중 > 소" 병합형 leaf 에서 칸 수를 오판한다. 쪼개기는 서버가 한다.
    """
    preview = build_create_preview(_record(_full_changes(category=category_id)))
    assert preview["categoryMajor"] == expected_major
    assert preview["categorySubPath"] == expected_sub
    assert preview["categoryPath"] == f"{expected_major} > {expected_sub}"


def test_preview_category_slots_empty_when_category_missing() -> None:
    """카테고리 change 가 없으면 3키 모두 빈 문자열 — 키는 남는다(FE 계약)."""
    preview = build_create_preview(_record(_full_changes(category=None)))
    assert preview["categoryPath"] == ""
    assert preview["categoryMajor"] == ""
    assert preview["categorySubPath"] == ""


def test_preview_nulls_and_placeholder_without_optionals() -> None:
    """정가·이미지·설명 없음 — null/0/배지 규칙(§5.4): 키는 남고 값만 null."""
    preview = build_create_preview(
        _record(_full_changes(original_price=None, image_url=None, description=None)),
        analysis=None,
    )
    assert preview["originalPriceText"] is None
    assert preview["discountRate"] == 0
    assert preview["imageUrl"] is None
    assert preview["imagePlaceholder"] is True
    assert preview["description"] is None
    warning = next(s for s in preview["sections"] if s["kind"] == "warning")
    assert any("정가 미입력" in item for item in warning["items"])


def test_preview_analysis_failure_warning() -> None:
    """이미지는 있는데 분석 실패(analysis=None) — warning 에 실패 고지."""
    preview = build_create_preview(_record(_full_changes()), analysis=None)
    warning = next(s for s in preview["sections"] if s["kind"] == "warning")
    assert any("사진 분석에 실패" in item for item in warning["items"])


def test_preview_note_section_on_modified() -> None:
    preview = build_create_preview(
        _record(_full_changes()),
        analysis=_ANALYSIS,
        modified_notes=["카테고리: 남방 → 셔츠"],
    )
    note = next(s for s in preview["sections"] if s["kind"] == "note")
    assert note["title"] == "수정 반영" and note["items"] == ["카테고리: 남방 → 셔츠"]


def test_diff_notes_category_shows_leaf_not_id() -> None:
    """note 는 사람용 — 카테고리는 id 가 아니라 leaf 명칭으로 표기한다."""
    previous = {c.field: c.after for c in _full_changes(category="101")}
    notes = diff_notes(previous, _record(_full_changes(category="100", price="35000")))
    assert "카테고리: 남방 → 셔츠" in notes
    assert "가격: 32,000원 → 35,000원" in notes
    assert all("100" not in n and "101" not in n for n in notes)


def test_diff_notes_empty_when_unchanged() -> None:
    previous = {c.field: c.after for c in _full_changes()}
    assert diff_notes(previous, _record(_full_changes())) == []


def test_preview_parses_suffixed_numbers_like_execution_layer() -> None:
    """[리뷰 H-1] "29,900원"·"50개" 같은 접미사 값은 실행 계층(hitl._parse_int)이 정상
    허용하는 입력이다 — preview 가 더 좁게 파싱하면 실행되는 값이 카드에 빈 값·거짓
    '정가 미입력' 경고로 보인다(보여준 것 ≠ 실행하는 것). 같은 파서를 재사용해야 한다."""
    preview = build_create_preview(
        _record(_full_changes(price="29,900원", original_price="35,000원", stock_quantity="50개")),
        analysis=_ANALYSIS,
    )
    assert preview["priceText"] == "29,900원"
    assert preview["originalPriceText"] == "35,000원"
    assert preview["discountRate"] == 14
    assert preview["stockText"] == "50개"
    warning = next(s for s in preview["sections"] if s["kind"] == "warning")
    assert not any("정가 미입력" in item for item in warning["items"])


def test_draft_event_masks_preview_text_fields() -> None:
    """[리뷰 M-4] preview 도 changes[] 와 같은 표시 계층 마스킹을 탄다(imageUrl 면제)."""
    secret = "sk-abcdefghijklmnop1234"
    record = _record(_full_changes(description=f"비밀키 {secret} 포함 설명"))
    preview = build_create_preview(record, analysis=_ANALYSIS)
    frame = seller_api._draft_event(record, preview=preview)
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert secret not in payload["data"]["preview"]["description"]
    # imageUrl 은 면제 — URL 원형 보존(마스킹 오탐 방지).
    assert payload["data"]["preview"]["imageUrl"] == "https://cdn.example.com/seller/7/ab12.jpg"
    # [#541] 새 표시 키도 마스킹 목록에 들어 있어야 한다 — 표시 키를 늘리면서 목록을
    # 안 늘리면 그 키만 원문으로 나간다(마스킹 정책이 카드에서 반쪽이 된다).
    assert {"categoryMajor", "categorySubPath"} <= set(payload["data"]["preview"])
    assert seller_api._masked_preview({"categoryMajor": f"대분류 {secret}"})["categoryMajor"] != (
        f"대분류 {secret}"
    )
    assert seller_api._masked_preview({"categorySubPath": f"중소 {secret}"})["categorySubPath"] != (
        f"중소 {secret}"
    )


def test_draft_event_carries_preview_for_create_only() -> None:
    """[와이어] create draft 이벤트에만 preview 키가 실린다 — 추가 전용(§3.2 v0.31.0)."""
    record = _record(_full_changes())
    preview = build_create_preview(record, analysis=_ANALYSIS)
    frame = seller_api._draft_event(record, preview=preview)
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload["type"] == "draft"
    assert payload["data"]["preview"]["priceText"] == "32,000원"
    # imageUrl 은 마스킹을 태우지 않는다 — URL 원형 보존.
    changes = {c["field"]: c["after"] for c in payload["data"]["changes"]}
    assert changes["imageUrl"] == "https://cdn.example.com/seller/7/ab12.jpg"

    frame_without = seller_api._draft_event(record)
    payload_without = json.loads(frame_without.removeprefix("data: ").strip())
    assert "preview" not in payload_without["data"]
