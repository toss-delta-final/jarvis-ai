"""등록 초안 preview 빌더 (#506, api-spec §3.2 v0.31.0) — 순수 함수.

`draft` 이벤트의 `preview{}`(op=="create" 한정)를 **코드가** 만든다 — LLM 산물이
아니다("계약값은 코드", hitl.DraftRecord docstring과 동일 원칙). FE 계약:

- `changes[]` 는 실행 정본(기계용), `preview{}` 는 표시 사본(사람용) — FE 는 preview
  만 보고 그린다. preview 를 고쳐 보내도 저장 값은 안 바뀐다(confirm 은 draftId 만).
- 13개 키는 **항상 존재**한다 — 값이 없으면 null 로 내려가지 키가 빠지지 않는다.
  ([#541] `categoryMajor`·`categorySubPath` 2키 추가 — 추가 전용이라 기존 소비자 불변.)
- 포맷은 서버가 완료한다("32,000원"·"50개") — FE 재가공 금지 계약이므로 역파싱이
  필요 없도록 숫자는 changes 원값에서만 뽑는다.
- `sections[].kind` 는 source/warning/note — FE 는 모르는 kind 를 무시한다(확장 규칙).

[#524] 옵션별 재고는 이 모듈의 대상이 아니다 — `preview{}` 는 `op=="create"` 전용 키이고
등록 시점엔 옵션이 아직 없어(I-10 은 optionId null 한 줄) 옵션별 diff 가 성립하지 않는다.

다만 잠재 결함을 하나 적어 둔다: `build_create_preview` 의 `values` 와 `diff_notes` 의
비교는 둘 다 **field 키 기준**이라 같은 필드의 change 가 둘 이상이면 조용히 뭉갠다. 지금은
create 에 재고 change 가 1건뿐이라 드러나지 않지만, **"옵션 있는 상품 등록"이 열리는 순간
여기가 먼저 깨진다** — 그때는 이 dedupe 를 (field, option_name) 기준으로 먼저 고칠 것.
"""

from __future__ import annotations

from app.agents.seller import category_catalog
from app.agents.seller.hitl import DraftRecord, _parse_int
from app.agents.seller.vision import ProductImageAnalysis


def parse_int_or_none(raw: str | None) -> int | None:
    """실행 계층 파서(hitl._parse_int)와 **동일 관용**의 표시용 래퍼.

    실행은 "29,900원" 같은 단위 접미사 값을 정상 입력으로 허용한다(_parse_int 주석).
    표시가 더 좁게 파싱하면 실행되는 값이 카드에 빈 값/거짓 경고("정가 미입력")로
    보이는 "보여준 것 ≠ 실행하는 것" 사고가 난다 — 반드시 같은 파서를 재사용한다.
    """
    if raw is None:
        return None
    try:
        return _parse_int(raw)
    except ValueError:
        return None


_int_or_none = parse_int_or_none  # 내부 별칭 — 기존 호출부 유지


def _price_text(value: int) -> str:
    return f"{value:,}원"


def build_create_preview(
    record: DraftRecord,
    *,
    analysis: ProductImageAnalysis | None = None,
    seller_inputs: str | None = None,
    modified_notes: list[str] | None = None,
) -> dict:
    """DraftRecord(op=="create") → preview dict (FE 계약 §5.4 — 11개 필드 확정형).

    - analysis: vision 분석 결과 — sections `source` 근거 표기용(값 자체는 changes 가 정본).
    - seller_inputs: "32,000원 / 50개" 처럼 판매자 발화에서 온 값의 근거 표기.
    - modified_notes: 수정 턴 반영 내역 — 있으면 sections 에 `note` 블록이 붙는다(§5.6).
    """
    values = {c.field: c.after for c in record.changes}
    price = _int_or_none(values.get("price"))
    original_price = _int_or_none(values.get("original_price"))
    stock = _int_or_none(values.get("stock_quantity"))
    image_url = values.get("image_url") or None
    category_id = values.get("category")
    category = category_catalog.get(category_id) if category_id else None

    # 할인율 — 정가와 판매가가 모두 있을 때만 계산(내림). 정가 미입력은 0(배지 숨김).
    discount_rate = 0
    if price is not None and original_price and original_price > price:
        discount_rate = int((original_price - price) * 100 / original_price)

    sections: list[dict] = []
    source_items: list[str] = []
    if image_url and analysis is not None:
        source_items.append("업로드 이미지 1장 분석")
    if seller_inputs:
        source_items.append(f"판매자 입력: {seller_inputs}")
    if source_items:
        sections.append({"kind": "source", "title": "초안 근거", "items": source_items})

    warning_items = [
        "상품명·카테고리·설명은 AI가 작성했습니다 — 다르면 알려주세요",
        "카테고리는 등록 후 변경할 수 없습니다",
    ]
    if original_price is None:
        warning_items.append("정가 미입력 → 판매가와 동일(할인 0%)로 등록됩니다")
    if image_url and analysis is None:
        warning_items.append("사진 분석에 실패했습니다 — 상품명·설명을 직접 확인해 주세요")
    sections.append({"kind": "warning", "title": "확인 필요", "items": warning_items})

    if modified_notes:
        sections.append({"kind": "note", "title": "수정 반영", "items": list(modified_notes)})

    # 13개 키는 항상 존재 — 없는 값은 null(FE 계약: undefined 분기를 만들지 않게 한다).
    return {
        "title": values.get("name") or "",
        "imageUrl": image_url,
        "imagePlaceholder": image_url is None,
        "priceText": _price_text(price) if price is not None else "",
        "originalPriceText": _price_text(original_price) if original_price is not None else None,
        "discountRate": discount_rate,
        "stockText": f"{stock:,}개" if stock is not None else "",
        "categoryPath": category.path_str if category else "",
        # [#541] 카테고리는 판매자가 **두 칸**(대분류 / 중·소분류)으로 채우는 값이라
        # 조각도 함께 싣는다 — FE 가 categoryPath 를 " > " 로 쪼개면 대분류 이름에
        # 슬래시가 든 경우("패션의류/잡화")나 중·소 병합형에서 칸 수를 오판한다.
        # categoryPath == categoryMajor + " > " + categorySubPath 불변식(§6.1).
        "categoryMajor": category.major if category else "",
        "categorySubPath": category.sub_path if category else "",
        # summary 는 ProductField 가 아니라 changes 에 실리지 않는다 — vision 분석이 원천.
        "summary": analysis.summary if analysis else None,
        "description": values.get("description") or None,
        "sections": sections,
    }


def diff_notes(previous_changes: dict[str, str], record: DraftRecord) -> list[str]:
    """수정 턴 note 항목 — 이전 초안 changes 스냅샷과 새 draft 의 필드 diff.

    카테고리는 id 대신 **leaf 명칭**으로 표기한다(사람용) — id 는 표시 계층에 노출하지
    않는다. 항목이 없으면 빈 목록(호출부가 note 블록 자체를 생략).
    """
    labels = {
        "name": "상품명",
        "price": "가격",
        "original_price": "정가",
        "stock_quantity": "재고",
        "category": "카테고리",
        "description": "설명",
        "summary": "요약",
        "image_url": "대표사진",
    }

    def _display(field: str, raw: str) -> str:
        if field == "category":
            entry = category_catalog.get(raw)
            return entry.leaf if entry else raw
        if field == "image_url":
            return "교체됨"
        if field in ("price", "original_price"):
            value = _int_or_none(raw)
            return _price_text(value) if value is not None else raw
        if field == "stock_quantity":
            value = _int_or_none(raw)
            return f"{value:,}개" if value is not None else raw
        return raw

    notes: list[str] = []
    for change in record.changes:
        before_raw = previous_changes.get(change.field)
        if before_raw is None or before_raw == change.after:
            continue
        label = labels.get(change.field, change.field)
        if change.field in ("description",):
            notes.append(f"{label}을 수정했습니다")
        elif change.field == "image_url":
            notes.append("대표사진을 교체했습니다")
        else:
            notes.append(
                f"{label}: {_display(change.field, before_raw)} → "
                f"{_display(change.field, change.after)}"
            )
    return notes
