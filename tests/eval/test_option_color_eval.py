"""evals/option_color 입력 가드 (task-6 회귀) — 잘못된 입력이 조용히 거짓 결과를 내지 않는지.

`products.tsv` 를 4컬럼(`id, status, stock_quantity, attributes`)으로 넣으면 3번째 필드(재고
숫자)가 `attributes` 로 읽혀 JSON 파싱이 전량 실패하고, 그 결과 모든 상품이 "색상 축 없음"
으로 §4.6 ②갈래(축 없으면 통과)를 타 `unbuyable_rate=0%` 같은 그럴듯하지만 완전히 틀린 결과를
냈다(실측 재현 중 실제로 밟은 사고). 이 파일은 그 사고를 재현·고정한다.
"""

from __future__ import annotations

import pytest

from evals.option_color.harness import HarnessError, load_products


def _write(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.eval
def test_products_tsv_with_stock_quantity_column_is_rejected(tmp_path) -> None:
    """(task-6 핵심 회귀) 4컬럼(id, status, stock_quantity, attributes) 입력은 에러다 — 조용히
    attributes 파싱을 실패시키고 넘어가지 않는다."""
    products = tmp_path / "products.tsv"
    options = tmp_path / "options.tsv"
    _write(
        products,
        ['1\tON_SALE\t99\t{"색상": ["블랙", "화이트"]}'],
    )
    _write(options, ["10\t1\t블랙 / M"])

    with pytest.raises(HarnessError, match="정확히 3개"):
        load_products(products, options)


@pytest.mark.eval
def test_products_tsv_with_too_few_columns_is_rejected(tmp_path) -> None:
    """2컬럼(id, status)만 있어도(3 미만) 에러다 — 3 초과뿐 아니라 미만도 여전히 막는다."""
    products = tmp_path / "products.tsv"
    options = tmp_path / "options.tsv"
    _write(products, ["1\tON_SALE"])
    _write(options, ["10\t1\t블랙 / M"])

    with pytest.raises(HarnessError, match="정확히 3개"):
        load_products(products, options)


@pytest.mark.eval
def test_attributes_parse_failure_rate_above_threshold_aborts(tmp_path) -> None:
    """attributes JSON 파싱 실패율이 임계(50%)를 넘으면 결과를 내지 않고 중단한다."""
    products = tmp_path / "products.tsv"
    options = tmp_path / "options.tsv"
    # 3건 모두 attributes 자리에 유효 JSON이 아닌 값 — 100% 파싱 실패.
    _write(
        products,
        [
            "1\tON_SALE\tnot-json-at-all",
            "2\tON_SALE\tnot-json-either",
            "3\tON_SALE\tstill-not-json",
        ],
    )
    _write(options, ["10\t1\t블랙 / M", "11\t2\t화이트 / M", "12\t3\t레드 / M"])

    with pytest.raises(HarnessError, match="파싱 실패율"):
        load_products(products, options)


@pytest.mark.eval
def test_zero_color_axis_products_aborts(tmp_path) -> None:
    """attributes 파싱은 되지만(성공률 100%) 색상 축을 가진 상품이 0건이면 중단한다."""
    products = tmp_path / "products.tsv"
    options = tmp_path / "options.tsv"
    _write(
        products,
        [
            '1\tON_SALE\t{"소재": "면"}',
            '2\tON_SALE\t{"소재": "울"}',
        ],
    )
    _write(options, ["10\t1\tS", "11\t2\tM"])

    with pytest.raises(HarnessError, match="색상 축"):
        load_products(products, options)


@pytest.mark.eval
def test_valid_three_column_input_loads_without_error(tmp_path) -> None:
    """(대조) 정상 3컬럼 입력은 예외 없이 로드되고 색상 축을 정확히 읽는다 — 위 가드들이
    정상 입력까지 막지 않는지 확인하는 짝 테스트."""
    products = tmp_path / "products.tsv"
    options = tmp_path / "options.tsv"
    _write(
        products,
        [
            '1\tON_SALE\t{"색상": ["블랙", "화이트"]}',
            '2\tON_SALE\t{"소재": "면"}',
        ],
    )
    _write(options, ["10\t1\t블랙 / M", "11\t2\tS"])

    rows = load_products(products, options)

    assert {r.product_id for r in rows} == {1, 2}
    black = next(r for r in rows if r.product_id == 1)
    assert black.attribute_colors == ("블랙", "화이트")
