"""골든셋 스키마 검증자 테스트 — #462. 픽스처 결함을 커밋 불가능하게 만드는 장치를 확인한다."""

from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from app.agents.profile.graph_models import make_node_id
from evals.taste_probe.loader import load_golden_set
from evals.taste_probe.schema import ExpectedTriple, GoldenSession, GoldenSet


def _triple(**overrides) -> dict:
    base = {
        "kind": "category",
        "predicate": "likes",
        "accept": ["음향가전 > 이어폰"],
        "canonicalLabel": "음향가전 > 이어폰",
        "nodeId": make_node_id("category", "음향가전 > 이어폰"),
    }
    base.update(overrides)
    return base


def _session(**overrides) -> dict:
    base = {
        "sessionId": "s-1",
        "sliceId": "kindCoverage",
        "testType": "MFT",
        "turns": ["이어폰 사고 싶어요", "무선으로 부탁해요", "케이스도 같이요"],
        "expectedTriples": [_triple()],
        "boundaryNote": "이어폰 계열만 정답이다.",
    }
    base.update(overrides)
    return base


# ── 정본 픽스처 ──


def test_committed_fixture_loads_and_matches_manifest() -> None:
    golden_set = load_golden_set("default")
    assert len(golden_set.sessions) == 30


def test_fixture_slice_quotas_match_issue_spec() -> None:
    golden_set = load_golden_set("default")
    counts = Counter(session.slice_id for session in golden_set.sessions)
    assert counts == {
        "kindCoverage": 10,
        "polarity": 4,
        "repetition": 3,
        "conflict": 3,
        "noise": 10,
    }


def test_fixture_covers_all_seven_kinds_in_kind_coverage_slice() -> None:
    golden_set = load_golden_set("default")
    kind_coverage_kinds = {
        triple.kind
        for session in golden_set.sessions
        if session.slice_id == "kindCoverage"
        for triple in session.expected_triples
    }
    assert kind_coverage_kinds == {
        "brand",
        "category",
        "attribute",
        "priceBand",
        "ratingBand",
        "product",
        "situation",
    }


def test_fixture_node_ids_match_make_node_id() -> None:
    golden_set = load_golden_set("default")
    for session in golden_set.sessions:
        for triple in session.expected_triples:
            assert triple.node_id == make_node_id(triple.kind, triple.canonical_label)


def test_fixture_noise_sessions_have_no_expected_triples() -> None:
    golden_set = load_golden_set("default")
    noise_sessions = [s for s in golden_set.sessions if s.slice_id == "noise"]
    assert len(noise_sessions) == 10
    assert all(session.expected_triples == [] for session in noise_sessions)


# ── ExpectedTriple validators ──


def test_canonical_label_must_be_in_accept() -> None:
    with pytest.raises(ValidationError, match="accept"):
        ExpectedTriple.model_validate(_triple(canonicalLabel="다른 카테고리"))


def test_node_id_must_match_make_node_id() -> None:
    with pytest.raises(ValidationError, match="nodeId"):
        ExpectedTriple.model_validate(_triple(nodeId="category:틀린값"))


def test_accept_forbids_normalize_label_duplicates() -> None:
    with pytest.raises(ValidationError, match="중복"):
        ExpectedTriple.model_validate(
            _triple(
                kind="brand",
                accept=["SONY", "sony"],
                canonicalLabel="SONY",
                nodeId=make_node_id("brand", "SONY"),
            )
        )


def test_predicate_must_match_positive_predicate_table_unless_avoids() -> None:
    with pytest.raises(ValidationError, match="positive predicate"):
        ExpectedTriple.model_validate(
            _triple(kind="category", predicate="prefers")  # category 의 positive 는 likes
        )


def test_avoids_predicate_is_allowed_for_any_kind() -> None:
    triple = ExpectedTriple.model_validate(_triple(kind="category", predicate="avoids"))
    assert triple.predicate == "avoids"


def test_purchased_predicate_is_not_a_valid_literal() -> None:
    with pytest.raises(ValidationError):
        ExpectedTriple.model_validate(_triple(predicate="purchased"))


def test_price_band_label_must_pass_resolve_band() -> None:
    with pytest.raises(ValidationError, match="_resolve_band"):
        ExpectedTriple.model_validate(
            _triple(
                kind="priceBand",
                predicate="prefers",
                accept=["가성비"],
                canonicalLabel="가성비",
                nodeId=make_node_id("priceBand", "가성비"),
            )
        )


def test_rating_band_label_exceeding_scale_max_is_rejected() -> None:
    with pytest.raises(ValidationError, match="_resolve_band"):
        ExpectedTriple.model_validate(
            _triple(
                kind="ratingBand",
                predicate="prefers",
                accept=["4-9"],
                canonicalLabel="4-9",
                nodeId=make_node_id("ratingBand", "4-9"),
            )
        )


def test_product_label_must_be_digits_only() -> None:
    with pytest.raises(ValidationError, match="_resolve_product"):
        ExpectedTriple.model_validate(
            _triple(
                kind="product",
                predicate="likes",
                accept=["상품A"],
                canonicalLabel="상품A",
                nodeId=make_node_id("product", "상품A"),
            )
        )


# ── GoldenSession validators ──


def test_turns_require_at_least_two() -> None:
    with pytest.raises(ValidationError):
        GoldenSession.model_validate(_session(turns=["한 턴뿐"]))


def test_turns_cannot_be_blank() -> None:
    with pytest.raises(ValidationError, match="공백"):
        GoldenSession.model_validate(_session(turns=["정상 발화", "   ", "또 발화"]))


def test_boundary_note_cannot_be_blank() -> None:
    with pytest.raises(ValidationError, match="boundaryNote"):
        GoldenSession.model_validate(_session(boundaryNote="          "))


def test_noise_slice_forbids_expected_triples() -> None:
    with pytest.raises(ValidationError, match="noise"):
        GoldenSession.model_validate(_session(sliceId="noise"))


def test_non_noise_slice_requires_at_least_one_expected_triple() -> None:
    with pytest.raises(ValidationError, match="expectedTriples"):
        GoldenSession.model_validate(_session(expectedTriples=[]))


def test_price_band_canonical_leak_in_turns_is_rejected() -> None:
    with pytest.raises(ValidationError, match="누출|정규형"):
        GoldenSession.model_validate(
            _session(
                turns=["3만원에서 5만원 사이요", "정확히는 30000-50000 원이요", "그 가격대로요"],
                expectedTriples=[
                    _triple(
                        kind="priceBand",
                        predicate="prefers",
                        accept=["30000-50000"],
                        canonicalLabel="30000-50000",
                        nodeId=make_node_id("priceBand", "30000-50000"),
                    )
                ],
            )
        )


def test_category_canonical_leak_in_turns_is_rejected() -> None:
    with pytest.raises(ValidationError, match="누출"):
        GoldenSession.model_validate(_session(turns=["음향가전 > 이어폰 사줘", "무선으로요", "네"]))


# ── GoldenSet validators ──


def test_session_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="sessionId"):
        GoldenSet.model_validate(
            {
                "schemaVersion": "1.0.0",
                "datasetVersion": "test",
                "sessions": [_session(sessionId="dup"), _session(sessionId="dup")],
            }
        )


def test_first_turns_must_be_unique_across_sessions() -> None:
    with pytest.raises(ValidationError, match="첫 turn"):
        GoldenSet.model_validate(
            {
                "schemaVersion": "1.0.0",
                "datasetVersion": "test",
                "sessions": [
                    _session(sessionId="a"),
                    _session(sessionId="b", boundaryNote="다른 세션이지만 첫 turn 이 같다."),
                ],
            }
        )
