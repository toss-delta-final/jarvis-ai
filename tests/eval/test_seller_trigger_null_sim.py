"""null 시뮬레이션 게이트 — 배포 전 필수 (이슈 #595, `12-EVAL` 결정 119).

`@pytest.mark.eval` 이라 기본 pytest 에서 **돈다**(`addopts` 는 smoke/integration/slow 만
제외한다). 결정론이라 CI 에 둘 수 있다 — `evals/README.md` 공통 규약 ③.

전 시나리오(소·중·대·계절성 4종 × 1,000일)는 CLI 가 돌려 `reports/` 에 커밋한다.
여기서는 그 중 둘을 **다시 돌려** 게이트를 확인하고, 측정치가 커밋된 리포트와 정확히
일치하는지 대조한다 — 리포트가 코드와 따로 노는 것을 막는 유일한 방법이다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.seller.sop.scan_params import thresholds_from_settings
from app.core.config import Settings
from evals.seller_trigger import null_sim, synth
from evals.seller_trigger.scenarios import DATASET_VERSION, build_scenarios

pytestmark = pytest.mark.eval

_REPORT = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "seller_trigger"
    / "reports"
    / f"null-sim-{DATASET_VERSION}.json"
)
# CI 에서 다시 도는 시나리오 — 전 시나리오를 재실행하면 1분을 넘긴다. 소규모는 AND 가
# α 로 퇴화하는 경계라 반드시 포함하고, 중규모는 실제 발동이 나오는 유일한 시나리오다.
_RERUN_SCENARIOS = ("small", "medium")


def _settings() -> Settings:
    return Settings(_env_file=None)


def _committed() -> dict:
    return json.loads(_REPORT.read_text(encoding="utf-8"))


def test_default_dow_weights_match_stats_source() -> None:
    """요일 가중치 상수가 REES46 원본과 어긋나면 잡는다(하네스 자족성의 대가)."""
    derived = synth.load_dow_weights()
    assert set(derived) == set(synth.DEFAULT_DOW_WEIGHTS)
    for isodow, weight in derived.items():
        assert synth.DEFAULT_DOW_WEIGHTS[isodow] == pytest.approx(weight, abs=1e-6)


def test_generator_is_deterministic() -> None:
    """같은 params 면 같은 시리즈 — 리포트가 재현 가능한 근거이려면 이것이 전제다."""
    params = synth.NullBrandParams(days=40, seed=1234)
    first = synth.generate_null_brand(params)
    second = synth.generate_null_brand(params)
    assert first == second


def test_null_series_has_no_trend() -> None:
    """null 의 정의 확인 — 앞뒤 절반의 평균 매출이 크게 다르면 추세가 새어 들어간 것이다."""
    series = synth.generate_null_brand(synth.NullBrandParams(days=600, seed=77))
    half = len(series) // 2
    front = sum(series.sales[:half]) / half
    back = sum(series.sales[half:]) / (len(series) - half)
    assert back == pytest.approx(front, rel=0.05)


def test_committed_report_is_current_and_passing() -> None:
    """커밋된 리포트가 현재 데이터셋 버전의 것이고 게이트를 통과했는가."""
    payload = _committed()
    assert payload["dataset_version"] == DATASET_VERSION
    assert payload["passed"] is True
    assert {scenario["scenario"] for scenario in payload["scenarios"]} == {
        scenario.key for scenario in build_scenarios(1)
    }


def test_gate_holds_and_matches_committed_report() -> None:
    """게이트 재실행 — 티어1 열림률이 상한 미만이고 커밋된 값과 정확히 같아야 한다."""
    settings = _settings()
    thresholds = thresholds_from_settings(settings)
    committed = {s["scenario"]: s for s in _committed()["scenarios"]}

    for scenario in build_scenarios(settings.seller_eval_null_days):
        if scenario.key not in _RERUN_SCENARIOS:
            continue
        report = null_sim.run_scenario(
            scenario.series(),
            thresholds=thresholds,
            lookback_days=settings.seller_analysis_lookback_days,
            gate_max=settings.seller_eval_trigger_rate_max,
            scenario=scenario.key,
            description=scenario.description,
            seed=scenario.params.seed,
        )
        assert report.passed, f"{scenario.key} 열림률 {report.tier1_open_rate:.3%}"
        assert report.tier1_open_rate < settings.seller_eval_trigger_rate_max
        expected = committed[scenario.key]
        assert report.tier1_open_days == expected["tier1_open_days"], "리포트가 코드와 어긋났다"
        assert report.scanned_days == expected["scanned_days"]


def test_gate_is_not_vacuous() -> None:
    """[반대 테스트] 판정을 일부러 헐겁게 하면 게이트가 **실제로** 실패해야 한다.

    `evals/README.md` 가 요구하는 "채널이 헛돌지 않는가" 확인이다. 이 테스트가 없으면
    게이트가 항상 통과하는 코드여도 아무도 모른다.
    """
    settings = _settings()
    broken = thresholds_from_settings(settings)
    # 창 보정을 되돌리고(alpha=0.05) 고정 임계를 사실상 없앤 상태 — 개정 전 설계와 같다.
    broken = type(broken)(
        **{
            **{field: getattr(broken, field) for field in broken.__dataclass_fields__},
            "lookback_days": 1,
            "conversion_pct": 0.001,
        }
    )
    scenario = next(s for s in build_scenarios(300) if s.key == "medium")
    report = null_sim.run_scenario(
        scenario.series(),
        thresholds=broken,
        lookback_days=settings.seller_analysis_lookback_days,
        gate_max=settings.seller_eval_trigger_rate_max,
        scenario="broken",
        description="반대 테스트",
        seed=scenario.params.seed,
    )
    assert not report.passed
    assert report.tier1_open_rate > settings.seller_eval_trigger_rate_max
