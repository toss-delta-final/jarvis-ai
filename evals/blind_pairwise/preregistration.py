"""수집 전에 고정하는 #153 평가 설계 설정."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("preregistration.json")
CONFIG_VERSION = "blind-pairwise-prereg-v1"
DIMENSIONS = ("relevance_fit", "explainability", "trustworthiness")


def load_preregistration(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """버전 관리된 사전 등록 JSON을 읽는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    """수집에 사용한 설정/입력 파일의 byte-level 지문을 계산한다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_preregistration(config: dict[str, Any]) -> None:
    """지원하는 고정 설계 외의 수집/분석 설정을 거부한다."""
    if config.get("schemaVersion") != CONFIG_VERSION:
        raise ValueError(f"schemaVersion must be {CONFIG_VERSION}")
    if config.get("issue") != 153 or config.get("scope") != "buyer":
        raise ValueError("#153 is a buyer-only evaluation")
    if not isinstance(config.get("pairCount"), int) or config["pairCount"] < 20:
        raise ValueError("pairCount must be at least 20")
    if config.get("ratingsPerPair") != 3:
        raise ValueError("ratingsPerPair must remain 3 independent ratings")
    if not isinstance(config.get("minimumEligibleEvaluators"), int) or config[
        "minimumEligibleEvaluators"
    ] < 5:
        raise ValueError("minimumEligibleEvaluators must be at least 5")
    if not isinstance(config.get("seed"), int) or isinstance(config["seed"], bool):
        raise ValueError("seed must be an integer fixed before collection")
    randomization = config.get("randomization")
    if not isinstance(randomization, dict):
        raise ValueError("randomization declaration is required")
    if randomization.get("algorithm") != "sha256-seeded-constrained-balanced-left-right-v2":
        raise ValueError("unsupported randomization algorithm")
    if randomization.get("algorithmIdentityVisibleToEvaluator") is not False:
        raise ValueError("algorithm identity must be hidden from evaluators")
    if randomization.get("labels") != ["A", "B"]:
        raise ValueError("evaluator labels must be A and B")
    if config.get("dimensions") != list(DIMENSIONS):
        raise ValueError(f"dimensions must be {list(DIMENSIONS)}")
    ordinal = config.get("ordinalScale")
    if ordinal != {"min": 1, "max": 5, "values": [1, 2, 3, 4, 5]}:
        raise ValueError("ordinalScale must be the fixed 1-5 rubric")
    if config.get("confidence") != 0.95:
        raise ValueError("confidence must be 0.95")
    if config.get("intervalEstimand") != {
        "method": "wilson",
        "scope": "descriptive-conditional-response-level",
        "accountsForCrossedPairEvaluatorDependence": False,
    }:
        raise ValueError("intervalEstimand must declare descriptive Wilson scope")
    if config.get("agreement") != {
        "method": "krippendorff-alpha",
        "ordinalDistance": "pooled-marginal-cumulative",
    }:
        raise ValueError("agreement method must be Krippendorff alpha")
    if config.get("preferenceAlphaAbstain") != "missing":
        raise ValueError("preference alpha must treat abstain as missing")
    if config.get("fixedBeforeCollection") is not True:
        raise ValueError("analysis plan must be fixed before collection")
    if config.get("humanResponsesArtifact") is not None:
        raise ValueError("pre-human preregistration must not embed response data")


__all__ = [
    "CONFIG_PATH",
    "CONFIG_VERSION",
    "load_preregistration",
    "sha256_file",
    "validate_preregistration",
]
