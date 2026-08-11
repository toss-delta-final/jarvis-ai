"""군집 안정성(ARI) 게이트 — 이슈 #595, `12-EVAL` 결정 121.

`random_state` 고정은 재현성이지 안정성이 아니다. `churn` 워커의 이동 행렬 전체가
"어제 충성형이 오늘 이탈위험형"이라는 비교 위에 서 있어, 군집이 불안정하면 이동이
전부 난수가 된다 — 그것을 실제로 재는 자리다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from evals.seller_trigger import ari, goldenset
from evals.seller_trigger.scenarios import DATASET_VERSION

pytestmark = pytest.mark.eval

_REPORT = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "seller_trigger"
    / "reports"
    / f"ari-{DATASET_VERSION}.json"
)


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_customer_axis_meets_stability_gate() -> None:
    """±5% 노이즈를 두 번 독립으로 섞어도 같은 군집이 나와야 한다."""
    settings = _settings()
    report = ari.measure_customer_ari(settings=settings, dataset_version=DATASET_VERSION)
    assert report.measured, report.note
    assert report.passed, f"ARI {report.ari} < {report.minimum}"
    assert report.ari is not None and report.ari >= settings.seller_cluster_stability_min


def test_cluster_omission_is_not_scored_as_zero() -> None:
    """군집 생략은 "불안정"이 아니라 **재지 못한 것**이다 — 0.0 으로 적지 않는다."""
    settings = _settings()
    # 30명 미만 군집이 2개 이상이면 k 후보가 전멸한다(seller_customer_segment_min_size).
    report = ari.measure_customer_ari(
        settings=settings, dataset_version=DATASET_VERSION, per_group=2
    )
    assert not report.measured
    assert report.ari is None
    assert not report.passed
    assert report.note


def test_ari_channel_is_not_vacuous() -> None:
    """[반대 테스트] 노이즈를 크게 주면 게이트가 **실제로** 실패해야 한다."""
    report = ari.measure_customer_ari(
        settings=_settings(), dataset_version=DATASET_VERSION, noise_pct=0.8
    )
    assert report.measured
    assert not report.passed


def test_product_axis_is_exploratory_only() -> None:
    """상품 축도 재되 게이트가 아니다 — `12-EVAL` §6.3 의 처방이 고객 축 튜너블만 가리킨다."""
    report = ari.measure_product_ari(
        settings=_settings(),
        dataset_version=DATASET_VERSION,
        rows=goldenset.four_pattern_products(),
    )
    assert report.axis == "product"
    assert report.note.startswith("exploratory")


def test_committed_report_matches_measurement() -> None:
    """커밋된 첫 실측값이 현재 코드의 산출과 같은가(리포트 드리프트 방지)."""
    payload = json.loads(_REPORT.read_text(encoding="utf-8"))
    assert payload["dataset_version"] == DATASET_VERSION
    committed = next(m for m in payload["measurements"] if m["axis"] == "customer")
    measured = ari.measure_customer_ari(settings=_settings(), dataset_version=DATASET_VERSION)
    assert committed["ari"] == pytest.approx(measured.ari)
