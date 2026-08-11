"""리포트 산출 CLI — `uv run python -m evals.seller_trigger` (이슈 #595).

리포트 JSON 의 키는 **dataclass 필드명(snake_case) 그대로**다 — 와이어 스키마가 아니라
파이썬 산출물 덤프라, camelCase 로 옮기면 `asdict()` 결과와 손으로 쓴 껍데기가 서로 다른
규약을 쓰게 된다(실제로 그렇게 어긋나 테스트가 잡았다).

CI 테스트는 같은 하네스를 호출해 **게이트만** 확인하고, 사람이 읽는 근거 문서는 이
CLI 가 `reports/` 에 쓴다. 산출물에 타임스탬프를 넣지 않는다 — 리포트가 커밋되는
근거 문서라 같은 입력이면 같은 파일이어야 diff 가 의미를 갖는다.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.agents.seller.sop.scan_params import thresholds_from_settings
from app.core.config import Settings
from evals.seller_trigger import ari, goldenset, null_sim
from evals.seller_trigger.scenarios import DATASET_VERSION, build_scenarios

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"wrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="판매자 트리거 검증 리포트 산출")
    parser.add_argument("--days", type=int, default=None, help="기본값은 SELLER_EVAL_NULL_DAYS")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)

    settings = Settings(_env_file=None)
    thresholds = thresholds_from_settings(settings)
    days = args.days or settings.seller_eval_null_days

    scenarios = [
        null_sim.run_scenario(
            scenario.series(),
            thresholds=thresholds,
            lookback_days=settings.seller_analysis_lookback_days,
            gate_max=settings.seller_eval_trigger_rate_max,
            scenario=scenario.key,
            description=scenario.description,
            seed=scenario.params.seed,
        )
        for scenario in build_scenarios(days)
    ]
    report = null_sim.build_report(
        scenarios,
        thresholds=thresholds,
        lookback_days=settings.seller_analysis_lookback_days,
        gate_max=settings.seller_eval_trigger_rate_max,
        dataset_version=DATASET_VERSION,
    )
    _write(args.out / f"null-sim-{DATASET_VERSION}.md", report.to_markdown())
    _write(args.out / f"null-sim-{DATASET_VERSION}.json", report.to_json())

    outcomes = goldenset.run_goldenset(thresholds)
    _write(
        args.out / f"goldenset-{DATASET_VERSION}.json",
        json.dumps(
            {"dataset_version": DATASET_VERSION, "cases": [asdict(o) for o in outcomes]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    customer = ari.measure_customer_ari(settings=settings, dataset_version=DATASET_VERSION)
    product = ari.measure_product_ari(
        settings=settings,
        dataset_version=DATASET_VERSION,
        rows=goldenset.four_pattern_products(),
    )
    _write(
        args.out / f"ari-{DATASET_VERSION}.json",
        json.dumps(
            {
                "dataset_version": DATASET_VERSION,
                "gate": "customer",
                "measurements": [asdict(customer), asdict(product)],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    failed = [s.scenario for s in scenarios if not s.passed]
    gaps = [o.case_id for o in outcomes if not o.passed]
    print(
        f"null-sim {'PASS' if report.passed else 'FAIL'} (실패 시나리오 {failed or '없음'}) / "
        f"goldenset {len(outcomes) - len(gaps)}/{len(outcomes)} / "
        f"ARI(customer) {customer.ari} {'PASS' if customer.passed else 'FAIL'}"
    )
    return 0 if report.passed and not gaps and customer.passed else 1
