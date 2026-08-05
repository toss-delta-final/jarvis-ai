"""priority 프로브 픽스처 로더 — 해시 게이트·스키마 (#281 TASK 3 §6)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from evals.priority_probe.loader import (
    FIXTURE_DIR,
    build_decompose_kwargs,
    fixture_sha256,
    load_fixture_set,
    resolve_fixture_path,
)
from evals.priority_probe.schema import Channel, FixtureSet, PriorityCell


def test_committed_fixture_loads_and_matches_manifest_hash() -> None:
    fixture = load_fixture_set("default")
    assert fixture.fixture_version == "priority-probe-v1"
    assert len(fixture.cells) >= 10


def test_tampering_with_the_committed_fixture_fails_the_hash_gate(tmp_path) -> None:
    path = resolve_fixture_path("default")
    tampered_dir = tmp_path / "fixtures"
    tampered_dir.mkdir()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cells"][0]["utterance"] = payload["cells"][0]["utterance"] + " (변조됨)"
    (tampered_dir / "priority_fixture.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    manifest_path = path.parent / "manifest.json"
    (tampered_dir / "manifest.json").write_text(manifest_path.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="SHA-256"):
        load_fixture_set("default", fixture_dir=tampered_dir)


def test_external_fixture_path_skips_the_hash_gate(tmp_path) -> None:
    payload = {
        "fixtureVersion": "external-v1",
        "schemaVersion": "1.0.0",
        "channel": {
            "priorFilters": {"category": None},
            "lastRecommendations": [],
            "categoryFanoutMax": 5,
        },
        "cells": [
            {
                "cellId": f"c{i}",
                "utterance": f"발화 {i}",
                "needs": ["a", "b"],
                "expectedPriorities": [1, 2],
                "rationale": "외부 픽스처 해시 우회 테스트용 근거 문장입니다.",
            }
            for i in range(10)
        ],
    }
    path = tmp_path / "external.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    fixture = load_fixture_set(str(path))

    assert fixture.fixture_version == "external-v1"
    assert fixture_sha256(path)  # 해시는 그래도 계산 가능(산출물에 남기기 위해)


def test_needs_and_expected_priorities_length_must_match() -> None:
    with pytest.raises(ValidationError, match="길이"):
        PriorityCell(
            cell_id="bad",
            utterance="발화",
            needs=["a", "b", "c"],
            expected_priorities=[1, 2],
            rationale="길이 불일치 검증용 근거 문장입니다 스무 자 이상.",
        )


def test_expected_priorities_reject_out_of_range_values() -> None:
    with pytest.raises(ValidationError, match="1/2/3"):
        PriorityCell(
            cell_id="bad",
            utterance="발화",
            needs=["a", "b"],
            expected_priorities=[1, 4],
            rationale="범위 밖 값 검증용 근거 문장입니다 스무 자 이상.",
        )


def test_utterance_with_forbidden_vocabulary_is_rejected() -> None:
    with pytest.raises(ValidationError, match="정답 신호 어휘"):
        PriorityCell(
            cell_id="bad",
            utterance="이건 필수로 사야 하는 물건",
            needs=["a", "b"],
            expected_priorities=[1, 2],
            rationale="정답 어휘 금지 검증용 근거 문장입니다 스무 자 이상.",
        )


def test_cell_with_all_equal_expected_priorities_is_rejected() -> None:
    """priorityOrderPairs 축에 아무 기여도 못 하는 vacuous 셀은 커밋할 수 없다."""
    with pytest.raises(ValidationError, match="구분할 수 없어"):
        PriorityCell(
            cell_id="bad",
            utterance="발화",
            needs=["a", "b"],
            expected_priorities=[2, 2],
            rationale="전부 동일 기대값 검증용 근거 문장입니다 스무 자 이상.",
        )


def test_channel_must_be_filled_not_empty() -> None:
    """[§1 규율 5] 빈 맥락 프로브는 거짓 결론을 준다."""
    with pytest.raises(ValidationError, match="비어 있습니다"):
        Channel(prior_filters=None, last_recommendations=[])


def test_fixture_rejects_spec_example_vocabulary() -> None:
    """후보 프롬프트가 정본 예시(등뼈/들깨가루/청양고추)를 문면에 담고 있어 암기를 재게 된다."""
    cells = [
        PriorityCell(
            cell_id=f"c{i}",
            utterance="감자탕 재료 좀 사려고" if i == 0 else f"발화 {i}",
            needs=["등뼈", "대파"] if i == 0 else ["a", "b"],
            expected_priorities=[1, 2],
            rationale="정본 예시 금지 검증용 근거 문장입니다 스무 자 이상.",
        )
        for i in range(10)
    ]
    with pytest.raises(ValidationError, match="정본 예시"):
        FixtureSet(
            fixture_version="bad-v1",
            channel=Channel(prior_filters={"category": None}, last_recommendations=[]),
            cells=cells,
        )


def test_build_decompose_kwargs_carries_the_channel_shape() -> None:
    fixture = load_fixture_set("default")
    kwargs = build_decompose_kwargs(fixture.channel)

    assert kwargs["prior_filters"] is not None
    assert kwargs["last_recommendations"]
    assert kwargs["category_fanout_max"] == fixture.channel.category_fanout_max


def test_fixture_dir_points_at_the_committed_directory() -> None:
    assert (FIXTURE_DIR / "priority_fixture.json").exists()
    assert (FIXTURE_DIR / "manifest.json").exists()
