"""analysis/segmentation.py — 상품 k-means 군집화 테스트 (이슈 #290).

논문 재현: 3패턴 합성 상품 → k=3 복원 + 규칙 라벨(Moe 유형론 어휘) 부착.
결정론(random_state 주입)·폴백(상품 소수·분리 불능)·경계도 검증한다.
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis import segmentation

_PARAMS = {"k_min": 2, "k_max": 5, "random_state": 42}


def _product(pid: int, view: int, cart: int, checkout: int, purchase: int, visitors=None):
    return {
        "product_id": pid,
        "view": view,
        "cart": cart,
        "checkout": checkout,
        "purchase": purchase,
        "visitors": visitors,
    }


def _three_pattern_products() -> list[dict]:
    """합성 3패턴 — 전환직결형(6) / 카트이탈형(6) / 구경형(6). 패턴 내 소폭 변주."""
    products: list[dict] = []
    for i in range(6):  # 전환직결형: 전 단계 전환율 우량 (id 100~105)
        products.append(
            _product(100 + i, 200 + 10 * i, 80 + 4 * i, 60 + 3 * i, 50 + 2 * i, 150 + 5 * i)
        )
    for i in range(6):  # 카트이탈형: 담기율 높고 결제 진입이 급락 (id 200~205)
        products.append(_product(200 + i, 220 + 10 * i, 90 + 4 * i, 5 + i, 2, 160 + 5 * i))
    for i in range(6):  # 구경형: 조회 폭증·담기 저조 (id 300~305)
        products.append(_product(300 + i, 2000 + 100 * i, 20 + i, 5, 2, 1500 + 50 * i))
    return products


def test_reproduces_three_pattern_recovery_with_rule_labels() -> None:
    """논문 재현(핵심): 3패턴 합성 상품 → k=3 복원, 패턴별 소속 상품이 갈리지 않는다."""
    clusters = segmentation.cluster_products(_three_pattern_products(), **_PARAMS)

    assert len(clusters) == 3  # 실루엣이 k=3 을 선택
    memberships = {frozenset(c.product_ids) for c in clusters}
    assert frozenset(range(100, 106)) in memberships
    assert frozenset(range(200, 206)) in memberships
    assert frozenset(range(300, 306)) in memberships
    labels = {next(iter(c.product_ids)) // 100: c.label for c in clusters}
    assert labels[1] == "전환직결형"
    assert labels[2] == "카트이탈형"
    assert labels[3] == "구경형"
    assert all(c.silhouette > 0.3 for c in clusters)  # 분리가 뚜렷한 합성 데이터


def test_deterministic_same_input_same_output() -> None:
    """결정론(§10-②): random_state 고정 주입 — 같은 입력 2회 호출은 같은 결과다."""
    products = _three_pattern_products()
    first = segmentation.cluster_products(products, **_PARAMS)
    second = segmentation.cluster_products(products, **_PARAMS)
    assert first == second


def test_too_few_products_skips_clustering() -> None:
    """상품 수 < k_min×3 이면 군집 생략([]) — 군집당 표본 3개 미만은 실루엣 불안정."""
    few = _three_pattern_products()[:5]
    assert segmentation.cluster_products(few, **_PARAMS) == []


def test_identical_products_skip_clustering() -> None:
    """전 상품 피처 동일이면 분리 불능 — 빈 결과(군집 생략 신호)."""
    same = [_product(i, 100, 10, 5, 2, 50) for i in range(12)]
    assert segmentation.cluster_products(same, **_PARAMS) == []


def test_zero_denominators_do_not_crash() -> None:
    """view=0·cart=0 상품(비율 분모 0)도 피처 0.0 으로 흡수돼 군집화가 완주한다."""
    products = _three_pattern_products() + [_product(999, 0, 0, 0, 0, None)]
    clusters = segmentation.cluster_products(products, **_PARAMS)
    assert clusters  # 완주
    assert any(999 in c.product_ids for c in clusters)


def test_duplicate_labels_get_numbered() -> None:
    """같은 규칙 라벨이 여러 군집에 붙으면 번호로 구분된다 — LLM 군집 혼동 방지."""
    products = []
    for i in range(6):  # 구경형 패턴 A(조회 매우 큼)
        products.append(_product(400 + i, 5000 + 100 * i, 25, 5, 2, 4000))
    for i in range(6):  # 구경형 패턴 B(조회 큼 — A 와 스케일만 다름)
        products.append(_product(500 + i, 1500 + 50 * i, 8, 2, 1, 1200))
    clusters = segmentation.cluster_products(products, **_PARAMS)
    if len(clusters) >= 2:
        labels = [c.label for c in clusters]
        assert len(labels) == len(set(labels))  # 중복 라벨 없음(번호 부여)


def test_missing_visitors_does_not_distort_cluster_assignment() -> None:
    """[PR 리뷰] visitors 결측(None)은 0 위장이 아니라 관측치 평균 대체(표준화 후
    중립) — 방문자 미수집 정상 상품이 봇 패턴(visitors_per_view=0)으로 계산돼
    엉뚱한 군집에 묶이지 않는다."""
    products = _three_pattern_products()
    # 전환직결형 패턴과 동일한 상품인데 visitors 만 결측 — 같은 군집에 묶여야 한다.
    products.append(_product(106, 230, 92, 69, 56, None))
    clusters = segmentation.cluster_products(products, **_PARAMS)
    home = next(c for c in clusters if 106 in c.product_ids)
    assert 100 in home.product_ids  # 전환직결형 무리와 동거 — 결측이 소속을 바꾸지 않는다
    assert home.label.startswith("전환직결형")


def test_invalid_k_range_raises() -> None:
    """k 범위 오류는 호출부 설정 문제 — ValueError(도구가 degrade 로 흡수)."""
    with pytest.raises(ValueError):
        segmentation.cluster_products(_three_pattern_products(), k_min=1, k_max=5, random_state=42)
    with pytest.raises(ValueError):
        segmentation.cluster_products(_three_pattern_products(), k_min=4, k_max=3, random_state=42)
