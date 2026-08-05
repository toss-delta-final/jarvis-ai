"""개인화 평가 선언 파일 로더·검증."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.metrics.settings import EvaluationSettings
from evals.personalization.fixtures import DERIVATION_VERSION, load_profile_arms

CONFIG_PATH = Path(__file__).with_name("eval_config.json")


def load_eval_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """평가 선언의 형식과 Settings 정본 스냅샷 일치를 검증한다."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "configVersion",
        "bootstrap",
        "primaryMetric",
        "arms",
        "weightSweep",
        "margins",
        "hardFilterViolationMax",
        "profilePreferenceDerivation",
    }
    if set(payload) != required:
        raise ValueError(
            f"personalization eval config 필드가 다릅니다: {sorted(set(payload) ^ required)}"
        )
    bootstrap = payload["bootstrap"]
    if (
        bootstrap["resamples"] <= 0
        or not 0 < bootstrap["confidence"] < 1
        or not isinstance(bootstrap["seed"], int)
    ):
        raise ValueError("personalization bootstrap 설정이 유효하지 않습니다")
    settings = EvaluationSettings()
    primary = str(payload["primaryMetric"]).split(".")
    if (
        len(primary) != 3
        or primary[:2] != ["overall", "ndcgAtK"]
        or not primary[2].isdigit()
        or int(primary[2]) not in settings.eval_buyer_k_list
    ):
        raise ValueError("primaryMetric은 지원하는 overall.ndcgAtK.<K>여야 합니다")
    if payload["arms"] != list(load_profile_arms()):
        raise ValueError("eval config arm 이름·순서가 profile fixture와 다릅니다")
    if payload["profilePreferenceDerivation"] != DERIVATION_VERSION:
        raise ValueError("profile preference 파생 규칙 버전이 지원값과 다릅니다")
    expected_sweep = list(settings.personalization_eval_weight_sweep)
    if payload["weightSweep"] != expected_sweep:
        raise ValueError("weightSweep 스냅샷이 Settings 정본과 다릅니다")
    expected_margins = {
        "intentContradictionMax": settings.personalization_eval_intent_contradiction_max,
        "forbiddenInclusionMax": settings.personalization_eval_forbidden_inclusion_max,
        "cleanNoisyDropMargin": settings.personalization_eval_clean_noisy_drop_margin,
    }
    if payload["margins"] != expected_margins:
        raise ValueError("margin 스냅샷이 Settings 정본과 다릅니다")
    if payload["hardFilterViolationMax"] != settings.personalization_eval_hard_filter_violation_max:
        raise ValueError("hard-filter 허용치 스냅샷이 Settings 정본과 다릅니다")
    return payload
