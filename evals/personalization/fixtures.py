"""합성 개인화 profile arm fixture 로더."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures

FIXTURE_DIR = Path(__file__).with_name("fixtures")
DERIVATION_VERSION = "case-oracle-distractor-v1"


@dataclass(frozen=True)
class ProfileArm:
    """Tier D/L이 공유하는 식별자·마크다운·구조화 선호 묶음."""

    name: str
    version: str
    identity_kind: str
    markdown: str | None
    preferences: dict[str, dict[str, float]] | None
    derivation_version: str


def load_profile_arms() -> dict[str, ProfileArm]:
    """manifest 해시를 검증한 뒤 선언 순서대로 profile arm을 읽는다."""
    profile_path = FIXTURE_DIR / "profiles.json"
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    actual = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    if actual != manifest["sha256"]["profiles.json"]:
        raise ValueError("personalization profile fixture SHA-256이 manifest와 다릅니다")
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    return {
        row["name"]: ProfileArm(
            name=row["name"],
            version=row["version"],
            identity_kind=row["identityKind"],
            markdown=row["markdown"],
            preferences=row["preferences"],
            derivation_version=row["derivationVersion"],
        )
        for row in payload["arms"]
    }


def _normalized_axes(
    product_ids: list[int], catalog: dict[str, dict[str, Any]]
) -> dict[str, dict[str, float]]:
    categories: Counter[str] = Counter()
    brands: Counter[str] = Counter()
    for product_id in product_ids:
        product = catalog.get(str(product_id), {})
        if category := product.get("categoryName"):
            categories[str(category)] += 1
        if brand := product.get("brandName"):
            brands[str(brand)] += 1

    def normalize(counts: Counter[str]) -> dict[str, float]:
        maximum = max(counts.values(), default=0)
        return {key: value / maximum for key, value in sorted(counts.items())} if maximum else {}

    return {"categories": normalize(categories), "brands": normalize(brands)}


def derive_case_preferences(
    arm_name: str,
    case: GoldenCase,
    fixtures: EvaluationFixtures,
) -> dict[str, dict[str, float]] | None:
    """정답·실제 후보로부터 case별 clean/noisy/repeated 선호를 결정론 파생한다.

    clean은 grade≥2 정답 분포의 oracle-aligned 상한이고, noisy는 후보 내 distractor 축을
    0.8로 섞는다. repeated는 clean 최상위 축 하나만 유지하고 나머지를 0.2배로 감쇠해
    반복 발화의 빈도 편향을 모사한다.
    """
    if arm_name in {"guest", "member_no_profile"}:
        return None
    fixture = fixtures.search_responses.get(case.search_fixture_id or "", {})
    candidate_ids = [int(value) for value in fixture.get("productIds", [])]
    aligned_ids = [
        product_id for product_id in candidate_ids if case.relevance_grades.get(product_id, 0) >= 2
    ]
    clean = _normalized_axes(aligned_ids, fixtures.catalog)
    if arm_name == "clean":
        return clean
    if arm_name == "noisy":
        distractor_ids = [
            product_id
            for product_id in candidate_ids
            if case.relevance_grades.get(product_id, 0) < 2
        ]
        distractors = _normalized_axes(distractor_ids, fixtures.catalog)
        return {
            axis: {
                **clean[axis],
                **{
                    key: max(clean[axis].get(key, 0.0), value * 0.8)
                    for key, value in distractors[axis].items()
                },
            }
            for axis in ("categories", "brands")
        }
    if arm_name == "repeated":
        ranked = sorted(
            (
                (-value, axis, key)
                for axis in ("categories", "brands")
                for key, value in clean[axis].items()
            )
        )
        winner = (ranked[0][1], ranked[0][2]) if ranked else None
        return {
            axis: {
                key: value if (axis, key) == winner else value * 0.2
                for key, value in clean[axis].items()
            }
            for axis in ("categories", "brands")
        }
    raise ValueError(f"알 수 없는 personalization arm: {arm_name}")
