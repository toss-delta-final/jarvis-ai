"""옵션명 → 재고 행 해소 (#524) — 유일 매칭만 채택, 모호·불일치는 되묻기.

재고는 돈이다: 엉뚱한 옵션의 재고를 바꾸는 것보다 한 번 더 묻는 편이 싸다. 그래서
이 모듈의 계약은 "확신 없으면 None + 되묻기 문구"이며, 테스트도 그 경계를 조준한다.
"""

from __future__ import annotations

from app.agents.seller.stock_options import (
    option_labels,
    resolve_stock_option,
    stock_lines_text,
)
from app.schemas.spring import SellerStockRow


def _stocks(*rows: tuple[int | None, str | None, int]) -> list[SellerStockRow]:
    return [
        SellerStockRow(optionId=option_id, optionName=name, quantity=quantity)
        for option_id, name, quantity in rows
    ]


_OPTIONED = _stocks((10, "블랙/M", 5), (11, "블랙/L", 0), (12, "화이트/M", 7))


# ── resolve_stock_option ────────────────────────────────────────────────────────


def test_exact_name_match() -> None:
    row, problem = resolve_stock_option("블랙/M", _OPTIONED)
    assert problem is None and row is not None and row.option_id == 10


def test_exact_match_is_case_and_space_tolerant() -> None:
    row, problem = resolve_stock_option("  블랙/m ", _OPTIONED)
    assert problem is None and row is not None and row.option_id == 10


def test_segment_match_unique() -> None:
    """ "화이트" 는 화이트/M 하나에만 걸린다 — 세그먼트 일치 유일 → 채택."""
    row, problem = resolve_stock_option("화이트", _OPTIONED)
    assert problem is None and row is not None and row.option_id == 12


def test_segment_match_ambiguous_reasks() -> None:
    """ "블랙" 은 블랙/M·블랙/L 둘 다 — 골라주지 않고 되묻는다."""
    row, problem = resolve_stock_option("블랙", _OPTIONED)
    assert row is None and problem is not None
    assert "블랙/M" in problem and "블랙/L" in problem


def test_unknown_name_reasks_with_labels() -> None:
    row, problem = resolve_stock_option("레드", _OPTIONED)
    assert row is None and problem is not None
    assert "레드" in problem and "블랙/M" in problem


def test_no_name_with_multiple_options_reasks() -> None:
    row, problem = resolve_stock_option(None, _OPTIONED)
    assert row is None and problem is not None


def test_no_name_with_single_option_auto_selects() -> None:
    single = _stocks((10, "블랙/M", 5))
    row, problem = resolve_stock_option(None, single)
    assert problem is None and row is not None and row.option_id == 10


def test_optionless_product_uses_null_row_even_with_name() -> None:
    """옵션 없는 상품은 이름을 말해도 재고가 한 칸뿐 — 모호함이 없다."""
    base = _stocks((None, None, 30))
    row, problem = resolve_stock_option("블랙", base)
    assert problem is None and row is not None and row.option_id is None
    assert row.quantity == 30


def test_old_be_empty_stocks_returns_none_without_problem() -> None:
    """구 BE(단일 stockQuantity)는 stocks 가 빈 목록 — 오류가 아니라 optionId null 취급."""
    row, problem = resolve_stock_option(None, [])
    assert row is None and problem is None


def test_partial_match_unique() -> None:
    """세그먼트로 안 잘리는 이름도 유일 부분일치면 잇는다 — "화이트M" 표기 관용."""
    stocks = _stocks((20, "화이트(M)", 3), (21, "블랙(L)", 4))
    row, problem = resolve_stock_option("화이트", stocks)
    assert problem is None and row is not None and row.option_id == 20


def test_duplicate_names_reask() -> None:
    """동명 옵션(데이터 결함)은 실행 계층이 고르지 않는다."""
    stocks = _stocks((30, "단일", 1), (31, "단일", 2))
    row, problem = resolve_stock_option("단일", stocks)
    assert row is None and problem is not None


# ── 표시 도우미 ─────────────────────────────────────────────────────────────────


def test_stock_lines_text_optioned() -> None:
    text = stock_lines_text(12, _OPTIONED)
    assert "재고 합계 12건" in text
    assert "블랙/M 5건" in text
    assert "블랙/L 품절" in text  # 0 은 숫자 대신 품절 — 판매자 동선 어휘


def test_stock_lines_text_optionless_unchanged() -> None:
    """옵션 없는 상품(또는 구 BE 응답)은 기존 표기 그대로 — 프롬프트 회귀 방지."""
    assert stock_lines_text(30, []) == "재고 30건"
    assert stock_lines_text(30, _stocks((None, None, 30))) == "재고 30건"


def test_option_labels_fall_back_to_id() -> None:
    stocks = _stocks((40, None, 1))
    assert option_labels(stocks) == ["옵션 40"]
