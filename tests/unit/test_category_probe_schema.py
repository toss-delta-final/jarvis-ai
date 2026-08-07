"""앵커 스키마 검증자 테스트 — #331. 픽스처 결함을 커밋 불가능하게 만드는 장치를 확인한다."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evals.category_probe.loader import load_anchor_set
from evals.category_probe.schema import AnchorCell, AnchorSet, ExpectedLeg


def _leg(*accept: str) -> dict:
    return {"accept": list(accept)}


def _cell(**overrides) -> dict:
    base = {
        "cellId": "c-1",
        "utterance": "이어폰 사고 싶어",
        "sliceId": "single",
        "testType": "MFT",
        "expectedLegs": [_leg("음향가전 > 이어폰")],
        "boundaryNote": "이어폰 계열만 정답이다.",
    }
    base.update(overrides)
    return base


def test_committed_fixture_loads_and_matches_manifest() -> None:
    anchors = load_anchor_set("default")
    assert len(anchors.cells) == 46  # #428 — 인스턴스형 앵커 8셀 추가(v1 38 → v2 46)


def test_accept_requires_exactly_one_arrow() -> None:
    with pytest.raises(ValidationError, match="' > '"):
        ExpectedLeg.model_validate({"accept": ["이어폰"]})
    with pytest.raises(ValidationError, match="' > '"):
        ExpectedLeg.model_validate({"accept": ["음향가전 > 이어폰 > 무선"]})


def test_utterance_cannot_leak_canonical_arrow() -> None:
    with pytest.raises(ValidationError, match="' > '"):
        AnchorCell.model_validate(_cell(utterance="음향가전 > 이어폰 사줘"))


def test_single_slice_requires_exactly_one_leg() -> None:
    with pytest.raises(ValidationError, match="expectedLegs"):
        AnchorCell.model_validate(_cell(expectedLegs=[]))
    with pytest.raises(ValidationError, match="expectedLegs"):
        AnchorCell.model_validate(
            _cell(expectedLegs=[_leg("음향가전 > 이어폰"), _leg("캠핑 > 텐트")])
        )


def test_multi_slice_requires_at_least_two_legs() -> None:
    with pytest.raises(ValidationError, match="expectedLegs"):
        AnchorCell.model_validate(_cell(sliceId="multi", expectedLegs=[_leg("음향가전 > 이어폰")]))


def test_none_and_not_in_catalog_forbid_expected_legs() -> None:
    with pytest.raises(ValidationError, match="expectedLegs"):
        AnchorCell.model_validate(_cell(sliceId="none", expectedLegs=[_leg("음향가전 > 이어폰")]))
    with pytest.raises(ValidationError, match="absentKeyword"):
        AnchorCell.model_validate(_cell(sliceId="notInCatalog", expectedLegs=[]))


def test_not_in_catalog_requires_absent_keyword() -> None:
    cell = AnchorCell.model_validate(
        _cell(sliceId="notInCatalog", expectedLegs=[], absentKeyword="드론")
    )
    assert cell.absent_keyword == "드론"


def test_absent_keyword_forbidden_outside_not_in_catalog() -> None:
    with pytest.raises(ValidationError, match="absentKeyword"):
        AnchorCell.model_validate(_cell(absentKeyword="드론"))


def test_inv_requires_group_id_and_mft_forbids_it() -> None:
    with pytest.raises(ValidationError, match="invGroupId"):
        AnchorCell.model_validate(_cell(testType="INV"))
    with pytest.raises(ValidationError, match="invGroupId"):
        AnchorCell.model_validate(_cell(invGroupId="g1"))


def test_cell_id_uniqueness_enforced() -> None:
    with pytest.raises(ValidationError, match="cellId"):
        AnchorSet.model_validate(
            {
                "fixtureVersion": "v1",
                "cells": [_cell(cellId="dup"), _cell(cellId="dup")],
            }
        )


def test_inv_group_requires_at_least_two_cells() -> None:
    with pytest.raises(ValidationError, match="invGroupId"):
        AnchorSet.model_validate(
            {
                "fixtureVersion": "v1",
                "cells": [
                    _cell(cellId="inv-only", testType="INV", invGroupId="lonely"),
                ],
            }
        )


def test_boundary_note_cannot_be_blank() -> None:
    with pytest.raises(ValidationError, match="characters"):
        AnchorCell.model_validate(_cell(boundaryNote="short"))
