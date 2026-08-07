"""과소지정 판정 축 프로브 앵커 정답지 — 스키마·해시 게이트·척추 대조·D4 쿼터 (#380)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evals.underspecified_probe.loader import FIXTURE_DIR, load_anchor_set, resolve_fixture_path
from evals.underspecified_probe.schema import AnchorSet


def _raw() -> dict:
    return json.loads(resolve_fixture_path(None).read_text(encoding="utf-8"))


def test_committed_anchor_set_loads_and_matches_manifest_hash() -> None:
    anchors = load_anchor_set()
    assert anchors.fixture_version == "underspec-anchors-v1"
    assert len(anchors.utterances) == 30


def test_slices_match_the_packet_d4_quotas() -> None:
    anchors = load_anchor_set()
    counts: dict[str, int] = {}
    for anchor in anchors.utterances:
        counts[anchor.slice] = counts.get(anchor.slice, 0) + 1
    assert counts == {
        "no_condition": 5,
        "constraint_price": 5,
        "constraint_budget_set": 4,
        "what_axis": 8,
        "blocking_rating": 5,
        "multiturn_gate": 3,
    }


def test_test_type_counts_match_mft_14_inv_16() -> None:
    anchors = load_anchor_set()
    counts: dict[str, int] = {}
    for anchor in anchors.utterances:
        counts[anchor.test_type] = counts.get(anchor.test_type, 0) + 1
    assert counts == {"MFT": 14, "INV": 16}


def test_cases_json_inherited_seven_anchors_are_all_present() -> None:
    """[§D4] cases.json 승계분(0001~0007, 0008 은 셀 제외) 이 전부 존재하고 caseId==sourceCaseId."""
    anchors = load_anchor_set()
    by_id = {anchor.case_id: anchor for anchor in anchors.utterances}
    inherited = [f"buy-under-000{i}" for i in range(1, 8)]
    for case_id in inherited:
        assert case_id in by_id, f"{case_id} 가 앵커 집합에 없습니다"
        assert by_id[case_id].source_case_id == case_id
    assert "buy-under-0008" not in by_id  # 플래그 off 롤백 경로 — 셀로 만들지 않는다(D4)


def test_what_axis_slice_has_two_of_each_subaxis_via_reference_axes() -> None:
    """[§D4] what_axis 8건 = category 2 · brand 2 · color 2 · keyword 2.

    `categoryQueries` 는 category 전용 라벨(2건)과 keyword 앵커의 보조 라벨(2건, §D8 채널
    불확실성)에 함께 쓰이므로 "정확히 category" 를 가리려면 brand/color/keyword 신호가 섞이지
    않은 경우만 category 로 센다.
    """
    anchors = load_anchor_set()
    what_axis = [a for a in anchors.utterances if a.slice == "what_axis"]
    assert len(what_axis) == 8
    category_only = 0
    brand = 0
    color = 0
    keyword = 0
    for anchor in what_axis:
        axes = set(anchor.reference_axes)
        if "filters.brand" in axes:
            brand += 1
        elif "filters.color" in axes:
            color += 1
        elif "filters.keyword" in axes:
            keyword += 1
        elif axes == {"categoryQueries"}:
            category_only += 1
    assert (category_only, brand, color, keyword) == (2, 2, 2, 2)


def test_utterance_duplicate_is_rejected() -> None:
    data = _raw()
    data["utterances"][1]["utterance"] = data["utterances"][0]["utterance"]
    with pytest.raises(ValidationError, match="utterance"):
        AnchorSet.model_validate(data)


def test_case_id_duplicate_is_rejected() -> None:
    data = _raw()
    data["utterances"][1]["caseId"] = data["utterances"][0]["caseId"]
    with pytest.raises(ValidationError, match="caseId"):
        AnchorSet.model_validate(data)


def test_reference_axes_typo_is_rejected() -> None:
    data = _raw()
    what_axis_anchor = next(u for u in data["utterances"] if u["slice"] == "what_axis")
    what_axis_anchor["referenceAxes"] = ["filters.bran"]  # 오타
    with pytest.raises(ValidationError, match="referenceAxes"):
        AnchorSet.model_validate(data)


def test_constraint_axes_typo_is_rejected() -> None:
    data = _raw()
    price_anchor = next(u for u in data["utterances"] if u["slice"] == "constraint_price")
    price_anchor["constraintAxes"] = ["filters.pricemax"]  # 오타(카멜 아님)
    with pytest.raises(ValidationError, match="constraintAxes"):
        AnchorSet.model_validate(data)


def test_expected_reask_true_with_nonempty_reference_axes_is_rejected() -> None:
    data = _raw()
    reask_true_anchor = next(u for u in data["utterances"] if u["expectedReask"] is True)
    reask_true_anchor["referenceAxes"] = ["filters.brand"]
    with pytest.raises(ValidationError, match="referenceAxes"):
        AnchorSet.model_validate(data)


def test_expected_reask_true_with_inv_test_type_is_rejected() -> None:
    data = _raw()
    reask_true_anchor = next(u for u in data["utterances"] if u["expectedReask"] is True)
    reask_true_anchor["testType"] = "INV"
    with pytest.raises(ValidationError, match="MFT"):
        AnchorSet.model_validate(data)


def test_expected_reask_false_without_reference_axes_or_prior_is_rejected() -> None:
    data = _raw()
    # what_axis 앵커는 referenceAxes 가 있다 — 지우고 priorExists 도 false 로 두면 위반이어야 한다.
    what_axis_anchor = next(u for u in data["utterances"] if u["slice"] == "what_axis")
    what_axis_anchor["referenceAxes"] = []
    with pytest.raises(ValidationError, match="referenceAxes"):
        AnchorSet.model_validate(data)


def test_expected_reask_false_with_mft_test_type_is_rejected() -> None:
    data = _raw()
    what_axis_anchor = next(u for u in data["utterances"] if u["slice"] == "what_axis")
    what_axis_anchor["testType"] = "MFT"
    with pytest.raises(ValidationError, match="INV"):
        AnchorSet.model_validate(data)


def test_gate_anchor_with_empty_reference_axes_and_prior_exists_is_valid() -> None:
    """[대조] priorExists=true 게이트 앵커는 referenceAxes=[] 라도 유효해야 한다."""
    data = _raw()
    gate_anchor = next(u for u in data["utterances"] if u["slice"] == "multiturn_gate")
    assert gate_anchor.get("referenceAxes", []) == []
    assert gate_anchor["priorExists"] is True
    AnchorSet.model_validate(data)  # 예외 없이 통과해야 한다


def test_source_case_id_mismatch_with_cases_json_is_rejected() -> None:
    """[척추 검증자] sourceCaseId 가 있으면 cases.json 발화와 글자 그대로 같아야 한다."""
    data = _raw()
    inherited_anchor = next(u for u in data["utterances"] if u.get("sourceCaseId"))
    inherited_anchor["utterance"] = "cases.json 과 다른 발화"
    with pytest.raises(ValidationError, match="cases.json"):
        AnchorSet.model_validate(data)


def test_source_case_id_not_found_in_cases_json_is_rejected() -> None:
    data = _raw()
    inherited_anchor = next(u for u in data["utterances"] if u.get("sourceCaseId"))
    inherited_anchor["sourceCaseId"] = "buy-under-9999"
    with pytest.raises(ValidationError, match="cases.json"):
        AnchorSet.model_validate(data)


def test_tampered_fixture_file_is_rejected(tmp_path) -> None:
    for name in ("anchors.json", "manifest.json"):
        (tmp_path / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    data = json.loads((tmp_path / "anchors.json").read_text(encoding="utf-8"))
    data["utterances"][0]["utterance"] = "손댄 발화"
    (tmp_path / "anchors.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_anchor_set(fixture_dir=tmp_path)
