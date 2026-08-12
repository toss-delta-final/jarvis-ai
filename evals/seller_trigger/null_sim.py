"""null 시뮬레이션 — 배포 전 필수 게이트 (이슈 #595, `12-EVAL` 결정 119).

변화가 **없는** 합성 브랜드를 길게 돌려 트리거 발동 횟수를 센다. `10-TRIGGER` §7 이
*"±5% 가 의미 있다는 근거는 없다 — 첫 달 발동 빈도를 보고 조정할 값"* 이라 인정한
가설값을, 배포 전으로 당겨 실증으로 고정하는 것이 목적이다.

[게이트 지표는 티어1 열림률 하나다]
`10-TRIGGER` §4.2 가 *"티어2는 발동 조건이 아니라 서술 재료"* 라고 정했으므로, 보고서가
나가는 빈도는 오직 티어1 이 결정한다. 티어2 발동률은 **참고 측정**이다 — 그래서 이
하네스는 티어1 개폐와 무관하게 티어2 를 매일 돌린다(운영은 열렸을 때만 조회한다).
그러지 않으면 티어2 통계가 "티어1 이 열린 드문 날"에서만 뽑혀 표본이 편향된다.

판정(`12-EVAL` §6.1)::

    < 1%   임계 확정
    1~5%   고정 임계 상향 후 재측정
    > 5%   AND 조건이 작동하지 않는다 — 설계 재검토

[시나리오를 여러 개 도는 이유]
브랜드 규모에 따라 AND 의 작동 방식이 뒤집힌다. 표본이 크면 고정 임계가 "작지만 유의한
변화"를 걸러내지만, 표본이 작으면 **유의한 날이 곧 변화가 큰 날**이라 두 조건이 거의
같은 사건이 되고 AND 가 α 로 퇴화한다. 한 규모만 재면 그 사실이 안 보인다.
요일 효과를 과장한 시나리오도 함께 돌려, 계절성이 오탐을 만들지 않는지 대조한다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from app.agents.seller.analysis import scan
from app.agents.seller.analysis.scan import TriggerThresholds
from evals.seller_trigger.synth import NullBrandSeries, tier1_inputs_at, tier2_inputs_at

# 지표 정의 동봉 — `evals/README.md` 공통 규약 ⑧("분자·분모 정의를 함께 싣는다").
METRIC_DEFINITIONS: dict[str, str] = {
    "tier1.openRate": "티어1 중 하나라도 발동한 날 수 / 스캔 가능 일수 (primary, 게이트)",
    "trigger.<name>.fireRate": "발동한 날 수 / 그 트리거가 판정 가능했던 날 수",
    "trigger.<name>.thresholdRate": "고정 임계를 통과한 날 수 / 판정 가능 일수",
    "trigger.<name>.significantRate": "통계적으로 유의했던 날 수 / 판정 가능 일수",
    "trigger.<name>.holdRate": "decided=False 인 날 수 / 스캔 가능 일수 (보류는 이상 없음이 아니다)",
    "product.falsePositivePerDay": "발동 상품 수 합 / 판정 가능 상품-일 수 합",
}


@dataclass(frozen=True)
class TriggerRate:
    """트리거 1종의 발동 통계. **AND 의 어느 쪽이 막았는지를 분리해 센다.**

    ``threshold_met_days`` 와 ``significant_days`` 가 각각 크고 ``fired_days`` 가 작다면
    AND 가 일하고 있다는 뜻이다. 둘 중 하나가 ``fired_days`` 와 같으면 그쪽은 사실상
    필터가 아니고, 판정이 나머지 하나에 전적으로 달려 있다.
    """

    trigger: str
    tier: int
    decided_days: int
    hold_days: int
    threshold_met_days: int
    significant_days: int
    fired_days: int
    fire_rate: float
    threshold_rate: float
    significant_rate: float
    hold_rate: float


@dataclass(frozen=True)
class ScenarioReport:
    """시나리오 1종(브랜드 규모·요일 진폭 조합)의 측정 결과."""

    scenario: str
    description: str
    seed: int
    days: int
    scanned_days: int
    tier1_open_days: int
    tier1_open_rate: float
    passed: bool
    verdict: str
    product_false_positive_per_day: float
    triggers: list[TriggerRate] = field(default_factory=list)


@dataclass(frozen=True)
class NullSimReport:
    """발동률 리포트 — 임계 확정의 근거 문서다(`12-EVAL` §6.1)."""

    dataset_version: str
    lookback_days: int
    baseline_days: int
    gate_max: float
    effective_rate_alpha: float
    thresholds: dict[str, float]
    scenarios: list[ScenarioReport] = field(default_factory=list)
    metric_definitions: dict[str, str] = field(default_factory=lambda: dict(METRIC_DEFINITIONS))

    @property
    def passed(self) -> bool:
        """**모든** 시나리오가 게이트를 통과해야 통과다 — 한 규모만 통과하면 의미가 없다."""
        return all(scenario.passed for scenario in self.scenarios)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["passed"] = self.passed
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def to_markdown(self) -> str:
        lines = [
            f"# null 시뮬레이션 발동률 리포트 — `{self.dataset_version}`",
            "",
            "이상 0건·요일 효과만 있는 합성 브랜드에서 트리거가 얼마나 발동하는가."
            " 게이트 지표는 **티어1 열림률**(= 보고서 생성률)이고, 상한은"
            f" `seller_eval_trigger_rate_max` = **{self.gate_max:.2%}** 다.",
            "",
            f"- 비교 구간 {self.baseline_days}일 / lookback {self.lookback_days}일",
            f"- 비율·카운트 검정 유의수준: `seller_rate_test_alpha /"
            f" seller_analysis_lookback_days` = **{self.effective_rate_alpha:.5f}**",
            f"- 종합 판정: **{'PASS' if self.passed else 'FAIL'}**",
            "",
            "## 시나리오 요약",
            "",
            "| 시나리오 | 설명 | 스캔 가능 | 티어1 열림 | 열림률 | 판정 |",
            "|---|---|---|---|---|---|",
        ]
        for scenario in self.scenarios:
            lines.append(
                f"| `{scenario.scenario}` | {scenario.description} |"
                f" {scenario.scanned_days} | {scenario.tier1_open_days} |"
                f" **{scenario.tier1_open_rate:.3%}** |"
                f" {'✅' if scenario.passed else '🔴'} {scenario.verdict} |"
            )
        for scenario in self.scenarios:
            lines += [
                "",
                f"### `{scenario.scenario}` — 트리거별",
                "",
                "| 트리거 | 티어 | 판정가능 | 보류 | 임계통과 | 유의 | **발동** | 발동률 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for rate in scenario.triggers:
                lines.append(
                    f"| `{rate.trigger}` | {rate.tier} | {rate.decided_days} |"
                    f" {rate.hold_days} | {rate.threshold_met_days} |"
                    f" {rate.significant_days} | **{rate.fired_days}** |"
                    f" {rate.fire_rate:.3%} |"
                )
            lines.append("")
            lines.append(
                "- `product.falsePositivePerDay`:"
                f" {scenario.product_false_positive_per_day:.6f} (exploratory)"
            )
        lines += [
            "",
            "## 읽는 법",
            "",
            "임계통과·유의 칸이 각각 크고 발동 칸이 작으면 **AND 가 일하고 있다**는 뜻이다."
            " 둘 중 하나가 발동 칸과 같으면 그쪽은 필터가 아니고, 판정이 나머지 하나에"
            " 전적으로 달려 있다는 신호다.",
            "",
            "티어2 는 발동 조건이 아니라 서술 재료라 게이트를 흐리지 않는다. 다만 상품"
            " 트리거는 상품 수만큼 검정을 돌리고 α 를 보정하지 않으므로 보고서 서술에"
            " 오탐 상품이 섞일 수 있다 — 그 크기를 `product.falsePositivePerDay` 가 드러낸다.",
            "",
            "## 이 수치가 보증하지 않는 것",
            "",
            "합성 데이터의 정상 변동은 검정이 가정하는 분포와 정확히 일치한다(퍼널이 중첩"
            " 이항, 카운트가 포아송). 실제 브랜드의 과분산·자기상관·프로모션은 없으므로"
            " **운영 발동률은 이 값보다 높게 나올 수 있다.** 게이트는 하한 검증이지 상한"
            " 보증이 아니다.",
            "",
            "재구매율의 기준 구간은 일별 합이라 회원 중복 제거가 안 돼 있다 — 실 운영의"
            " I-14 는 구간을 한 번에 조회해 distinct 회원을 주므로 분모가 이보다 작다."
            " 즉 여기서는 표본이 과대평가돼 검정이 더 민감하다(발동률 **과대** 추정).",
            "",
        ]
        return "\n".join(lines)


def _verdict(open_rate: float, gate_max: float) -> str:
    if open_rate < gate_max:
        return "임계 확정"
    if open_rate < 0.05:
        return "고정 임계 상향 후 재측정"
    return "AND 조건이 작동하지 않는다 — 설계 재검토"


def run_scenario(
    series: NullBrandSeries,
    *,
    thresholds: TriggerThresholds,
    lookback_days: int,
    gate_max: float,
    scenario: str,
    description: str,
    seed: int,
) -> ScenarioReport:
    """일별로 스캔을 돌려 발동률을 센다. 순수 계산 — LLM 0회 · Spring 0회 · DB 0회.

    ``scan.scan`` 이 아니라 ``scan_tier1``/``scan_tier2`` 를 직접 부르는 이유: 게이트는
    티어1 만 쓰지만 티어2 통계는 매일 재야 표본이 편향되지 않는다(모듈 docstring).
    운영 경로(열렸을 때만 티어2)는 `scan.scan` 이 그대로 강제한다.
    """
    counts: dict[str, dict[str, int]] = {}
    tiers: dict[str, int] = {}
    order: list[str] = []
    scanned = 0
    tier1_open = 0
    product_fired = 0
    product_decided = 0

    for index in range(len(series)):
        tier1_inputs = tier1_inputs_at(
            series, index, lookback_days=lookback_days, baseline_days=thresholds.baseline_days
        )
        if tier1_inputs is None:
            continue
        tier2_inputs = tier2_inputs_at(series, index, baseline_days=thresholds.baseline_days)
        scanned += 1

        tier1 = scan.scan_tier1(tier1_inputs, thresholds=thresholds)
        tier2 = scan.scan_tier2(tier2_inputs, thresholds=thresholds) if tier2_inputs else []
        if any(evaluation.fired for evaluation in tier1):
            tier1_open += 1

        for evaluation in (*tier1, *tier2):
            if evaluation.trigger not in counts:
                counts[evaluation.trigger] = dict.fromkeys(
                    ("decided", "hold", "threshold", "significant", "fired"), 0
                )
                tiers[evaluation.trigger] = evaluation.tier
                order.append(evaluation.trigger)
            bucket = counts[evaluation.trigger]
            if not evaluation.decided:
                bucket["hold"] += 1
                continue
            bucket["decided"] += 1
            if evaluation.threshold_met:
                bucket["threshold"] += 1
            if evaluation.significant:
                bucket["significant"] += 1
            if evaluation.fired:
                bucket["fired"] += 1

        if tier2_inputs is not None:
            rows = scan.evaluate_product_sales_rows(
                tier2_inputs.product_current, tier2_inputs.product_baseline, thresholds=thresholds
            )
            decided_rows = [row for row in rows if row.decided]
            product_decided += len(decided_rows)
            product_fired += sum(1 for row in decided_rows if row.fired)

    def _rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    rates = [
        TriggerRate(
            trigger=name,
            tier=tiers[name],
            decided_days=counts[name]["decided"],
            hold_days=counts[name]["hold"],
            threshold_met_days=counts[name]["threshold"],
            significant_days=counts[name]["significant"],
            fired_days=counts[name]["fired"],
            fire_rate=_rate(counts[name]["fired"], counts[name]["decided"]),
            threshold_rate=_rate(counts[name]["threshold"], counts[name]["decided"]),
            significant_rate=_rate(counts[name]["significant"], counts[name]["decided"]),
            hold_rate=_rate(counts[name]["hold"], scanned),
        )
        for name in order
    ]
    open_rate = _rate(tier1_open, scanned)
    return ScenarioReport(
        scenario=scenario,
        description=description,
        seed=seed,
        days=len(series),
        scanned_days=scanned,
        tier1_open_days=tier1_open,
        tier1_open_rate=open_rate,
        passed=open_rate < gate_max,
        verdict=_verdict(open_rate, gate_max),
        product_false_positive_per_day=_rate(product_fired, product_decided),
        triggers=rates,
    )


def build_report(
    scenarios: list[ScenarioReport],
    *,
    thresholds: TriggerThresholds,
    lookback_days: int,
    gate_max: float,
    dataset_version: str,
) -> NullSimReport:
    """시나리오 결과를 하나의 근거 문서로 묶는다."""
    return NullSimReport(
        dataset_version=dataset_version,
        lookback_days=lookback_days,
        baseline_days=thresholds.baseline_days,
        gate_max=gate_max,
        effective_rate_alpha=thresholds.effective_rate_alpha,
        thresholds={
            "sales_pct": thresholds.sales_pct,
            "conversion_pct": thresholds.conversion_pct,
            "product_drop_pct": thresholds.product_drop_pct,
            "cart_abandon_pp": thresholds.cart_abandon_pp,
            "new_customer_drop_pct": thresholds.new_customer_drop_pct,
            "repurchase_drop_pp": thresholds.repurchase_drop_pp,
            "rate_alpha": thresholds.rate_alpha,
        },
        scenarios=scenarios,
    )
