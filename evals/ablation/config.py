"""버전 관리 ablation 사전 등록 설정."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.model_eval.repeats import validate_metric_path

CONFIG_PATH = Path(__file__).with_name("ablation_config.json")
ARMS = ("pipeline", "scoring", "single_call")
PRIMARY_METRIC = "overall.ndcgAtK.10"
SECONDARY_METRICS = (
    "overall.filterAccuracy",
    "overall.hardConstraintViolationRate",
    "overall.recallAtK.10",
    "overall.precisionAtK.10",
    "overall.mrr",
)
CONFIG_VERSION = "ablation-config-v3"
CASE_TEST_TYPE_FILTER = "MFT"


def load_ablation_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """버전 관리 JSON 설정을 읽는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def validate_ablation_config(config: dict[str, Any]) -> None:
    """사전 등록에서 지원한 실험 공간 밖의 실행을 거부한다."""
    if config.get("arms") != list(ARMS):
        raise ValueError(f"지원하는 arms는 {list(ARMS)}뿐입니다")
    if config.get("split") != "dev":
        raise ValueError("ablation split은 dev만 지원합니다")
    if config.get("configVersion") != CONFIG_VERSION:
        raise ValueError(f"지원하는 configVersion은 {CONFIG_VERSION}뿐입니다")
    if config.get("primaryMetric") != PRIMARY_METRIC:
        raise ValueError(f"지원하는 primaryMetric은 {PRIMARY_METRIC}뿐입니다")
    validate_metric_path(PRIMARY_METRIC)
    secondary = config.get("secondaryMetrics")
    if secondary != list(SECONDARY_METRICS):
        raise ValueError(f"secondaryMetrics는 {list(SECONDARY_METRICS)} 순서로 고정됩니다")
    for metric in SECONDARY_METRICS:
        validate_metric_path(str(metric))
    if config.get("caseOrder") != "caseId-asc":
        raise ValueError("지원하는 caseOrder는 caseId-asc뿐입니다")
    if config.get("missingRunPolicy") != "excludePairReportCount":
        raise ValueError("지원하는 missingRunPolicy는 excludePairReportCount뿐입니다")
    if config.get("multiplicity") != "primaryConfirmatoryOthersExploratory":
        raise ValueError("지원하는 multiplicity 선언이 아닙니다")
    if config.get("rankingExclusionPolicy") != "nonDiscriminativeRanking":
        raise ValueError("지원하는 rankingExclusionPolicy가 아닙니다")
    if config.get("inconclusiveRule") != "ciIncludesZero":
        raise ValueError("지원하는 inconclusiveRule은 ciIncludesZero뿐입니다")
    bootstrap = config.get("bootstrap")
    if not isinstance(bootstrap, dict):
        raise ValueError("bootstrap 선언이 필요합니다")
    if bootstrap.get("resamples") != 2000 or bootstrap.get("confidence") != 0.95:
        raise ValueError("bootstrap은 resamples=2000, confidence=0.95만 지원합니다")
    if config.get("seed") != 20260805:
        raise ValueError("사전 등록 seed는 20260805입니다")
    if config.get("repeats") != 5:
        raise ValueError("사전 등록 repeats는 5입니다")
    if config.get("caseTestTypeFilter") != CASE_TEST_TYPE_FILTER:
        raise ValueError(f"지원하는 caseTestTypeFilter는 {CASE_TEST_TYPE_FILTER}뿐입니다")
