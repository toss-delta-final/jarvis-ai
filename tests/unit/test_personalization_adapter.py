from __future__ import annotations

import inspect
import json

import pytest

from evals.goldenset.loader import load_cases
from evals.personalization.adapter import ProfileScoringBuyerAdapter
from evals.personalization.cli import (
    PERSONALIZATION_MODULE_PATHS,
    _manifest,
    _primary_k,
    case_has_repurchase_intent,
)
from evals.personalization.config import load_eval_config
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters


def test_eval_config_is_valid_and_matches_declared_settings() -> None:
    config = load_eval_config()
    assert config["configVersion"] == "personalization-eval-config-v1"
    assert config["primaryMetric"] == "overall.ndcgAtK.10"
    assert config["bootstrap"] == {"resamples": 2000, "confidence": 0.95, "seed": 20260803}
    assert config["weightSweep"] == [0.0, 0.075, 0.15, 0.3, 0.6]


def test_profile_adapter_uses_case_fixture_preferences_not_purchase_history() -> None:
    prefs = {"categories": {"이어폰": 1.0}, "brands": {"Sony": 1.0}}
    adapter = ProfileScoringBuyerAdapter(
        profile_preferences=prefs, preference_resolver=lambda *_: prefs
    )
    assert adapter.profile_preferences is prefs
    assert adapter.model_config["profileSource"] == "caseDerivedFixtureArm"


def test_hard_filter_has_no_profile_input_and_negative_fixture_fires() -> None:
    assert "profile" not in inspect.signature(apply_hard_filters).parameters
    result = apply_hard_filters(
        [{"productId": 1, "price": 10, "categoryName": "금지"}],
        HardConstraints(forbidden_categories=frozenset({"금지"})),
    )
    assert result.products == []
    assert [(cut.product_id, cut.reason) for cut in result.cuts] == [(1, "forbiddenCategory")]


def test_eval_config_rejects_arm_order_drift(tmp_path) -> None:
    payload = load_eval_config()
    payload["arms"] = list(reversed(payload["arms"]))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="arm"):
        load_eval_config(path)


def test_primary_metric_k_and_repurchase_slice_are_consumed() -> None:
    assert _primary_k({"primaryMetric": "overall.ndcgAtK.5"}) == 5
    cases = list(load_cases("dev"))
    assert sum("repurchase" in case.slices for case in cases) == 8
    repurchase = next(case for case in cases if "repurchase" in case.slices)
    ordinary = next(case for case in cases if "repurchase" not in case.slices)
    assert case_has_repurchase_intent(repurchase)
    assert not case_has_repurchase_intent(ordinary)


def test_manifest_changes_when_personalization_source_changes(tmp_path, monkeypatch) -> None:
    copied = tmp_path / "cli.py"
    copied.write_bytes(PERSONALIZATION_MODULE_PATHS["cli.py"].read_bytes())
    monkeypatch.setitem(PERSONALIZATION_MODULE_PATHS, "cli.py", copied)
    before = _manifest(command="test", seed=1)
    copied.write_text(copied.read_text() + "\n# mutation\n", encoding="utf-8")
    after = _manifest(command="test", seed=1)
    assert before["hashes"]["personalizationModules"] != after["hashes"]["personalizationModules"]
