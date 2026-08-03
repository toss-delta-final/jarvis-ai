from __future__ import annotations

import hashlib
import json

import pytest

from evals.goldenset.loader import load_cases
from evals.metrics.runner import load_evaluation_fixtures
from evals.personalization.fixtures import (
    FIXTURE_DIR,
    derive_case_preferences,
    load_profile_arms,
)


def test_profile_fixtures_are_versioned_synthetic_and_hashed() -> None:
    arms = load_profile_arms()
    assert list(arms) == ["guest", "member_no_profile", "clean", "noisy", "repeated"]
    assert all(arm.version == "personalization-profile-v1" for arm in arms.values())
    assert arms["guest"].identity_kind == "guest" and arms["guest"].markdown is None
    assert arms["member_no_profile"].identity_kind == "member"
    assert arms["member_no_profile"].preferences is None
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
    assert manifest["provenance"] == "synthetic-deidentified"
    assert (
        manifest["sha256"]["profiles.json"]
        == hashlib.sha256((FIXTURE_DIR / "profiles.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("name", ["clean", "noisy", "repeated"])
def test_markdown_and_global_preferences_express_same_profile_signals(name: str) -> None:
    arm = load_profile_arms()[name]
    assert arm.preferences
    for brand in arm.preferences["brands"]:
        assert brand in arm.markdown
    for category in arm.preferences["categories"]:
        assert category in arm.markdown
    assert all(
        section in arm.markdown for section in ("## 구조화 블록", "## 취향 산문", "## 최근 맥락")
    )


def test_case_derived_preferences_are_discriminative_and_versioned() -> None:
    arms = load_profile_arms()
    fixtures = load_evaluation_fixtures()
    cases = list(load_cases("dev"))
    assert arms["clean"].derivation_version == "case-oracle-distractor-v1"
    noisy_differences = 0
    repeated_differences = 0
    for case in cases:
        clean = derive_case_preferences("clean", case, fixtures)
        noisy_differences += derive_case_preferences("noisy", case, fixtures) != clean
        repeated_differences += derive_case_preferences("repeated", case, fixtures) != clean
    assert noisy_differences > 0
    assert repeated_differences > 0
