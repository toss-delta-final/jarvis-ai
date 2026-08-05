"""니즈 전개(legs) 프로브 앵커 정답지 — 스키마·해시 게이트·goldenset 대조 (#332)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evals.legs_probe.loader import FIXTURE_DIR, load_anchor_set, resolve_fixture_path
from evals.legs_probe.schema import AnchorSet, _load_goldenset_queries


def _raw() -> dict:
    return json.loads(resolve_fixture_path(None).read_text(encoding="utf-8"))


def test_committed_anchor_set_loads_and_matches_manifest_hash() -> None:
    anchors = load_anchor_set()
    assert anchors.fixture_version == "legs-anchors-v1"
    assert len(anchors.utterances) == 39


def test_slices_match_the_packet_counts() -> None:
    anchors = load_anchor_set()
    counts: dict[str, int] = {}
    for anchor in anchors.utterances:
        counts[anchor.slice] = counts.get(anchor.slice, 0) + 1
    assert counts == {
        "single": 9,
        "conditions": 5,
        "situational": 11,
        "purpose": 9,
        "multi": 5,
    }


def test_all_anchors_are_mft() -> None:
    anchors = load_anchor_set()
    assert {anchor.check_type for anchor in anchors.utterances} == {"MFT"}


def test_buy_prefixed_utterances_match_goldenset_verbatim() -> None:
    anchors = load_anchor_set()
    goldenset = _load_goldenset_queries()
    buy_anchors = [anchor for anchor in anchors.utterances if anchor.case_id.startswith("buy-")]
    assert len(buy_anchors) == 8
    for anchor in buy_anchors:
        assert anchor.utterance == goldenset[anchor.case_id]


def test_tampered_fixture_file_is_rejected(tmp_path) -> None:
    for name in ("anchors.json", "manifest.json"):
        (tmp_path / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    data = json.loads((tmp_path / "anchors.json").read_text(encoding="utf-8"))
    data["utterances"][0]["utterance"] = "손댄 발화"
    (tmp_path / "anchors.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_anchor_set(fixture_dir=tmp_path)


def test_legs_max_over_category_fanout_max_is_rejected() -> None:
    data = _raw()
    data["utterances"][0]["expected"]["legsMax"] = 6
    with pytest.raises(ValidationError, match="category_fanout_max"):
        AnchorSet.model_validate(data)


def test_legs_min_over_legs_max_is_rejected() -> None:
    data = _raw()
    data["utterances"][0]["expected"]["legsMin"] = 5
    data["utterances"][0]["expected"]["legsMax"] = 1
    with pytest.raises(ValidationError, match="legsMin"):
        AnchorSet.model_validate(data)


def test_conditions_slice_with_groups_is_rejected() -> None:
    data = _raw()
    conditions_anchor = next(u for u in data["utterances"] if u["slice"] == "conditions")
    conditions_anchor["expected"]["coverageGroups"] = [{"groupId": "x", "synonyms": ["가나다"]}]
    conditions_anchor["expected"]["coverageTarget"] = 1
    with pytest.raises(ValidationError, match="conditions"):
        AnchorSet.model_validate(data)


def test_conditions_slice_with_nonzero_legs_max_is_rejected() -> None:
    data = _raw()
    conditions_anchor = next(u for u in data["utterances"] if u["slice"] == "conditions")
    conditions_anchor["expected"]["legsMax"] = 2
    with pytest.raises(ValidationError, match="legsMax==0"):
        AnchorSet.model_validate(data)


def test_synonym_blank_after_normalization_is_rejected() -> None:
    data = _raw()
    single_anchor = next(u for u in data["utterances"] if u["expected"]["coverageGroups"])
    single_anchor["expected"]["coverageGroups"][0]["synonyms"] = ["   "]
    with pytest.raises(ValidationError, match="빈 문자열"):
        AnchorSet.model_validate(data)


def test_coverage_target_out_of_range_is_rejected() -> None:
    data = _raw()
    single_anchor = next(u for u in data["utterances"] if u["expected"]["coverageGroups"])
    single_anchor["expected"]["coverageTarget"] = 99
    with pytest.raises(ValidationError, match="coverageTarget"):
        AnchorSet.model_validate(data)


def test_coverage_target_without_groups_is_rejected() -> None:
    data = _raw()
    conditions_anchor = next(u for u in data["utterances"] if u["slice"] == "conditions")
    conditions_anchor["expected"]["coverageTarget"] = 1
    with pytest.raises(ValidationError):
        AnchorSet.model_validate(data)


def test_pair_kind_without_pair_id_is_rejected() -> None:
    data = _raw()
    data["utterances"][0]["pairKind"] = "INV-paraphrase"
    with pytest.raises(ValidationError, match="pairId"):
        AnchorSet.model_validate(data)


def test_duplicate_case_id_is_rejected() -> None:
    data = _raw()
    data["utterances"][1]["caseId"] = data["utterances"][0]["caseId"]
    with pytest.raises(ValidationError, match="중복"):
        AnchorSet.model_validate(data)


def test_buy_prefixed_utterance_mismatching_goldenset_is_rejected() -> None:
    data = _raw()
    buy_anchor = next(u for u in data["utterances"] if u["caseId"].startswith("buy-"))
    buy_anchor["utterance"] = "다른 발화"
    with pytest.raises(ValidationError, match="goldenset"):
        AnchorSet.model_validate(data)


def test_buy_prefixed_unknown_case_id_is_rejected() -> None:
    data = _raw()
    buy_anchor = next(u for u in data["utterances"] if u["caseId"].startswith("buy-"))
    buy_anchor["caseId"] = "buy-does-not-exist-9999"
    with pytest.raises(ValidationError, match="goldenset"):
        AnchorSet.model_validate(data)


def test_pair_kind_is_consistent_within_each_pair_id() -> None:
    """[F-3] DIR-budget 쌍의 무예산 팔이 INV-paraphrase 로 오라벨되던 결함의 회귀 가드.

    `gift-budget`·`school-budget` 은 두 멤버 다 DIR-budget 이어야 한다(예산 유무로 legs 가
    줄지 않는지를 재는 방향 쌍이지 패러프레이즈 쌍이 아니다) — `camp-paraphrase` 는 반대로
    두 멤버 다 INV-paraphrase 다.
    """
    anchors = load_anchor_set()
    grouped: dict[str, set[str | None]] = {}
    for anchor in anchors.utterances:
        if anchor.pair_id is not None:
            grouped.setdefault(anchor.pair_id, set()).add(anchor.pair_kind)
    assert grouped["gift-budget"] == {"DIR-budget"}
    assert grouped["school-budget"] == {"DIR-budget"}
    assert grouped["camp-paraphrase"] == {"INV-paraphrase"}
    # camp-budget 은 비교 대상(legs-situ-0001)이 다른 pairId 에 속해 note 로만 교차 참조한다
    # (schema.py `_pair_ids_have_at_least_two_members` 제거 근거) — 그래도 자기 자신은 DIR-budget.
    assert grouped["camp-budget"] == {"DIR-budget"}
