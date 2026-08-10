"""#395 — I-1 응답 다이어트(협의 2·1) 회귀 — 계약 무변경, 방어적.

`docs/specs/PROPOSAL-I1-DIET-395.md` 가 BE 에 요청하는 필드 제외를 지금 파서(`spring_client.
_parse_search_response`)와 사후필터(`search_service`)가 이미 견딘다는 것을 고정한다. **BE 배포
(2026-08-08) 전에도 통과했고, 배포 후 실 응답 형태(2026-08-10 운영 실측)도 그대로 통과한다** —
방어적 성격은 그대로 유지한다(BE 가 이 필드들을 다시 보내도 깨지지 않는다).

6번은 버그 고정이 아니라 **"그래서 `totalCount` 가 필요하다"는 §4 주장의 근거를 코드로 고정**하는
것이다 — `ProductSearchResult.total_count` 가 실제 매칭 전체 수가 아니라 **받은 개수**를 따라가는
현재 동작을 실증한다(`size` 상한은 #395, 2026-08-07 로 재도입 폐지가 확정됐다 — 이 값은 그
폐지 판단의 근거로 남는다).
"""

from __future__ import annotations

from app.schemas.spring import ProductSearchFilters
from app.services import search_service, spring_client

# 실제 BE I-1 항목이 보내는 최소 필드 — 이 값들은 모든 테스트에서 그대로 유지한다(§2 "소비됨" 목록).
_CONSUMED_BASE = {
    "productId": 1,
    "name": "테스트 상품",
    "categoryName": "패션의류 > 티셔츠",
    "brandName": "브랜드A",
    "price": 29000,
    "rating": 4.2,
    "reviewCount": 10,
}


def _envelope(items: list[dict]) -> dict:
    """BE `ApiResponse<List<...>>` 형태({success, data:[...]})."""
    return {"success": True, "data": items}


def test_attributes_survives_without_ai_unconsumed_keys() -> None:
    """[§5 1층] `_extra`·`_source_pid`·`_domain`·`_category` 없이도 정상 파싱되고,
    소비되는 축(명시 속성 하드매칭)이 살아 있다 — `search_service._matches_attr_conditions`.
    """
    item = {
        **_CONSUMED_BASE,
        # 다이어트된 attributes: 명시 속성매칭에 실제로 쓰이는 축만 남기고
        # _extra·_source_pid·_domain·_category 는 전부 뺐다.
        "attributes": {"소재": "린넨", "색상": "네이비"},
    }
    result = spring_client._parse_search_response(_envelope([item]))

    assert len(result.products) == 1
    product = result.products[0]
    assert product.attributes == {"소재": "린넨", "색상": "네이비"}
    assert "_extra" not in (product.attributes or {})
    # 소비처: search_service._matches_attr_conditions — 축이 있고 값이 일치하면 통과.
    assert search_service._matches_attr_conditions(product, {"소재": "린넨"})
    # 조건 불일치는 여전히 탈락해야 한다(관대 매칭이 전부 통과로 퇴화하지 않았는지).
    assert not search_service._matches_attr_conditions(product, {"소재": "울"})


def test_summary_missing_still_parses_with_other_fields_intact() -> None:
    """[§5 2층] `summary` 가 없어도 파싱되고 나머지 필드는 온전하다(#101 전 미소비 확인)."""
    item = {k: v for k, v in _CONSUMED_BASE.items()}  # summary 키 자체가 없음
    item["attributes"] = {"소재": "린넨"}
    result = spring_client._parse_search_response(_envelope([item]))

    assert len(result.products) == 1
    product = result.products[0]
    assert product.summary is None
    assert product.name == "테스트 상품"
    assert product.price == 29000
    assert product.rating == 4.2
    assert product.review_count == 10
    assert product.category == "패션의류 > 티셔츠"
    assert product.brand == "브랜드A"


def test_options_and_option_count_missing_still_parses() -> None:
    """[§5 2층] `options`·`optionCount` 가 없어도 파싱된다(둘 다 아무 곳도 안 읽는다, #278)."""
    item = {k: v for k, v in _CONSUMED_BASE.items()}  # options/optionCount 키 자체가 없음
    result = spring_client._parse_search_response(_envelope([item]))

    assert len(result.products) == 1
    product = result.products[0]
    assert product.options is None
    assert product.option_count is None


def test_attributes_entirely_absent_still_parses_and_search_unaffected() -> None:
    """`attributes` 자체가 통째로 없어도 파싱되고, attr 조건이 없는 검색은 정상 동작한다."""
    item = {k: v for k, v in _CONSUMED_BASE.items()}  # attributes 키 자체가 없음
    result = spring_client._parse_search_response(_envelope([item]))

    assert len(result.products) == 1
    product = result.products[0]
    assert product.attributes is None

    # attr_conditions 가 없는 검색 경로 — apply_ai_side_filters 가 그대로 통과시킨다.
    filters = ProductSearchFilters(keyword="셔츠")
    filtered = search_service.apply_ai_side_filters(result.products, filters)
    assert filtered == result.products


def test_diet_response_yields_same_post_filter_result_as_full_response() -> None:
    """[§7] 미사용 필드가 **전부** 빠진 "다이어트 응답"도 사후필터(가격·평점·명시속성)가
    통상 응답과 같은 결과를 낸다 — `search_service.apply_ai_side_filters` 가 소비 필드만으로
    판정한다는 것을 항목 집합 단위로 증명한다.
    """

    def _fat_item(pid: int, *, rating: float | None, material: str) -> dict:
        return {
            "productId": pid,
            "name": f"상품{pid}",
            "summary": "장문의 요약 설명" * 5,
            "attributes": {
                "소재": material,
                "_extra": {"visual_features": "미소비 원문"},
                "_source_pid": str(pid),
                "_domain": "패션의류",
                "_category": "패션의류 > 티셔츠",
            },
            "categoryName": "패션의류 > 티셔츠",
            "brandName": "브랜드A",
            "price": 29000,
            "rating": rating,
            "reviewCount": 0 if rating is None else 5,
            "options": ["S", "M", "L"],
            "optionCount": 3,
        }

    def _diet_item(pid: int, *, rating: float | None, material: str) -> dict:
        return {
            "productId": pid,
            "name": f"상품{pid}",
            "attributes": {"소재": material},  # _extra 등 미소비 키 전부 제외
            "categoryName": "패션의류 > 티셔츠",
            "brandName": "브랜드A",
            "price": 29000,
            "rating": rating,
            "reviewCount": 0 if rating is None else 5,
            # summary·options·optionCount 자체가 응답에 없음
        }

    specs = [
        (1, 4.5, "린넨"),  # 평점 통과 + 소재 일치 → 통과
        (2, 3.0, "린넨"),  # 평점 미달(반증됨) → 탈락
        (3, None, "린넨"),  # 무평점(데이터 부재, 보존) → 통과
        (4, 4.8, "폴리에스터"),  # 소재 불일치 → 탈락
    ]
    fat = spring_client._parse_search_response(
        _envelope([_fat_item(pid, rating=r, material=m) for pid, r, m in specs])
    )
    diet = spring_client._parse_search_response(
        _envelope([_diet_item(pid, rating=r, material=m) for pid, r, m in specs])
    )

    filters = ProductSearchFilters(
        keyword="티셔츠", rating_min=4.0, attr_conditions={"소재": "린넨"}
    )
    fat_ids = {p.product_id for p in search_service.apply_ai_side_filters(fat.products, filters)}
    diet_ids = {p.product_id for p in search_service.apply_ai_side_filters(diet.products, filters)}

    assert fat_ids == {1, 3}  # 2=평점 미달, 4=소재 불일치
    assert diet_ids == fat_ids  # 다이어트 응답도 같은 판정


def test_truncated_response_parses_and_total_count_follows_received_count() -> None:
    """[§4 근거 고정] `size` 상한으로 절단된 응답도 정상 파싱되고, `total_count` 는
    **받은 개수**를 따라간다 — 매칭 전체 수가 아니다.

    이 테스트는 버그 재현이 아니다. `total_count` 가 "전체 매칭 수"가 아니라 **받은 개수**를
    따라간다는 사실은 그대로이고, 그래서 이 값은 `size` 상한을 **다시 도입하려면 `totalCount`
    가 반드시 동반돼야 한다는 폐지 판단(#395, 2026-08-07 확정 — `docs/api-spec.md` §4.6 요청
    표 `size` 노트)의 근거로 남는다**(추천 그래프의 0건 판정·자동완화·완화칩 estCount 가 이
    값을 소비한다 — `app/agents/buyer/recommendation/graph.py`).
    """
    full_items = [{**_CONSUMED_BASE, "productId": i} for i in range(1, 11)]  # 실제 매칭 10건

    full = spring_client._parse_search_response(_envelope(full_items))
    assert full.total_count == 10  # 절단 없음 — 받은 수 == 매칭 수(우연히 같다)

    # BE 가 size=3 상한으로 앞 3건만 잘라 보냈다고 가정 — AI 는 이 응답만 보고는 "매칭 3건"과
    # "매칭 10건 중 3건 절단"을 구분할 수단이 없다(payload 에 totalCount 가 없으므로).
    truncated_items = full_items[:3]
    truncated = spring_client._parse_search_response(_envelope(truncated_items))

    assert truncated.total_count == 3  # 매칭 전체(10)가 아니라 받은 개수(3)를 따라간다
    assert len(truncated.products) == 3


def test_live_deployed_item_shape_2026_08_10_parses_and_consumes() -> None:
    """[§11] 2026-08-10 운영 실측(6,128건 전건 4키 부재) 응답 형태를 고정한다.

    실제 운영이 보내는 최상위 11개 키 전부(productId·name·summary·attributes·categoryName·
    brandName·price·rating·reviewCount·options·optionCount)와, attributes 실측 상위 축
    (브랜드·제조국·색상·사이즈·소재)만 담은 항목이 파싱되는지, 4키(`_extra`·`_source_pid`·
    `_domain`·`_category`)가 실제로 없는지, 그리고 파싱 후 소비 경로(명시 속성 하드매칭·
    평점 하한 사후필터)가 살아 있는지(불일치 조건이 실제로 탈락하는지까지)를 함께 고정한다.
    """

    def _live_item(pid: int, *, material: str, rating: float) -> dict:
        return {
            "productId": pid,
            "name": f"상품{pid}",
            "summary": "운영 실측 요약",
            "attributes": {
                "브랜드": "브랜드A",
                "제조국": "대한민국",
                "색상": "네이비",
                "사이즈": "M",
                "소재": material,
            },
            "categoryName": "패션의류 > 티셔츠",
            "brandName": "브랜드A",
            "price": 29000,
            "rating": rating,
            "reviewCount": 5,
            "options": ["화이트/M", "블랙/M"],
            "optionCount": 2,
        }

    items = [
        _live_item(1, material="린넨", rating=4.5),  # 소재 일치 + 평점 통과 → 잔존
        _live_item(2, material="폴리에스터", rating=4.8),  # 소재 불일치 → 탈락
        _live_item(3, material="린넨", rating=3.0),  # 평점 미달 → 탈락
    ]
    result = spring_client._parse_search_response(_envelope(items))
    assert len(result.products) == 3

    for product in result.products:
        attrs = product.attributes or {}
        for key in ("_extra", "_source_pid", "_domain", "_category"):
            assert key not in attrs

    matched, mismatched, _ = result.products
    # 소비처 1: search_service._matches_attr_conditions — 일치·불일치 양쪽 판정이 실제로 갈린다.
    assert search_service._matches_attr_conditions(matched, {"소재": "린넨"})
    assert not search_service._matches_attr_conditions(mismatched, {"소재": "린넨"})

    # 소비처 2: apply_ai_side_filters — 평점 하한 경로까지 함께 태운다.
    filters = ProductSearchFilters(
        keyword="티셔츠", rating_min=4.0, attr_conditions={"소재": "린넨"}
    )
    filtered_ids = {
        p.product_id for p in search_service.apply_ai_side_filters(result.products, filters)
    }
    assert filtered_ids == {1}  # 2=소재 불일치, 3=평점 미달
