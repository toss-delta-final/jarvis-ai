"""로더 — 해시 게이트 + pre-flight 테스트 (#331). pg 접근은 전부 모킹한다."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from evals.category_probe.loader import PreflightError, load_anchor_set, preflight_check_catalog
from evals.category_probe.schema import AnchorSet


def test_manifest_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    anchors_path = tmp_path / "anchors.json"
    anchors_path.write_text(
        json.dumps(
            {
                "fixtureVersion": "broken",
                "cells": [
                    {
                        "cellId": "c-1",
                        "utterance": "이어폰 사고 싶어",
                        "sliceId": "single",
                        "testType": "MFT",
                        "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                        "boundaryNote": "이어폰 계열만 정답이다.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"sha256": {"anchors.json": "0" * 64}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="SHA-256"):
        load_anchor_set("anchors.json", fixture_dir=tmp_path)


def test_external_fixture_without_manifest_skips_hash_gate(tmp_path: Path) -> None:
    anchors_path = tmp_path / "candidate.json"
    anchors_path.write_text(
        json.dumps(
            {
                "fixtureVersion": "candidate",
                "cells": [
                    {
                        "cellId": "c-1",
                        "utterance": "이어폰 사고 싶어",
                        "sliceId": "single",
                        "testType": "MFT",
                        "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                        "boundaryNote": "이어폰 계열만 정답이다.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    anchors = load_anchor_set(str(anchors_path))
    assert isinstance(anchors, AnchorSet)
    assert anchors.fixture_version == "candidate"


_MINIMAL_ANCHOR_DOC = {
    "fixtureVersion": "v1",
    "cells": [
        {
            "cellId": "c-1",
            "utterance": "이어폰 사고 싶어",
            "sliceId": "single",
            "testType": "MFT",
            "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
            "boundaryNote": "이어폰 계열만 정답이다.",
        }
    ],
}


def test_default_fixture_without_manifest_is_rejected(tmp_path: Path) -> None:
    """F1-6 리뷰어 재현: `default` 픽스처 디렉터리에 manifest.json 자체가 없으면 해시 대조가
    조용히 통째로 생략된다 — 커밋된 앵커가 손상돼도 아무 신호 없이 통과한다."""
    (tmp_path / "anchors.json").write_text(json.dumps(_MINIMAL_ANCHOR_DOC), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        load_anchor_set("default", fixture_dir=tmp_path)


def test_default_fixture_missing_manifest_key_is_rejected(tmp_path: Path) -> None:
    """F1-6 리뷰어 재현: manifest.json 은 있지만 `anchors.json` 키가 없으면(다른 파일만 등재)
    구판은 `expected` 가 falsy 라 조용히 통과한다."""
    (tmp_path / "anchors.json").write_text(json.dumps(_MINIMAL_ANCHOR_DOC), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"sha256": {"other.json": "0" * 64}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="sha256"):
        load_anchor_set("default", fixture_dir=tmp_path)


def test_default_fixture_with_correct_manifest_still_loads(tmp_path: Path) -> None:
    """§F1-6 회귀 방지 — 정상 manifest(키 있음·해시 일치)는 여전히 통과해야 한다."""
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(_MINIMAL_ANCHOR_DOC), encoding="utf-8")
    from evals.category_probe.loader import fixture_sha256

    (tmp_path / "manifest.json").write_text(
        json.dumps({"sha256": {"anchors.json": fixture_sha256(path)}}), encoding="utf-8"
    )
    anchors = load_anchor_set("default", fixture_dir=tmp_path)
    assert anchors.fixture_version == "v1"


def test_preflight_rejects_missing_accept_label() -> None:
    anchors = AnchorSet.model_validate(
        {
            "fixtureVersion": "v1",
            "cells": [
                {
                    "cellId": "c-1",
                    "utterance": "이어폰 사고 싶어",
                    "sliceId": "single",
                    "testType": "MFT",
                    "expectedLegs": [{"accept": ["음향가전 > 이어폰"]}],
                    "boundaryNote": "이어폰 계열만 정답이다.",
                }
            ],
        }
    )
    with patch("app.pipelines.category_search.exact_lookup", return_value=set()) as mocked:
        with pytest.raises(PreflightError, match="accept 라벨"):
            preflight_check_catalog(anchors, "dsn://fake")
        mocked.assert_called_once()


def test_preflight_rejects_notincatalog_keyword_that_now_exists() -> None:
    anchors = AnchorSet.model_validate(
        {
            "fixtureVersion": "v1",
            "cells": [
                {
                    "cellId": "c-1",
                    "utterance": "드론 추천해줘",
                    "sliceId": "notInCatalog",
                    "testType": "MFT",
                    "expectedLegs": [],
                    "absentKeyword": "드론",
                    "boundaryNote": "드론은 사전에 없다.",
                }
            ],
        }
    )

    class _FakeCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return ("취미/드론 > 드론",)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    with (
        patch("app.pipelines.category_search.exact_lookup", return_value=set()),
        patch("psycopg.connect", return_value=_FakeConn()),
    ):
        with pytest.raises(PreflightError, match="notInCatalog"):
            preflight_check_catalog(anchors, "dsn://fake")
