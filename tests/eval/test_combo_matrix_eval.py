"""기능 조합 커버리지 매트릭스 하네스 게이트 (이슈 #335 §7) — @pytest.mark.eval, 기본 PR pytest 에서 돈다."""

from __future__ import annotations

import hashlib
import typing

import pytest

from evals.combo_matrix.generator import (
    FILTER_AXES,
    add_directed_cases,
    add_perturbations,
    assemble_cases,
    all_pairs,
    enumerate_leaves,
    generate,
)
from evals.combo_matrix.loader import (
    AXES_PATH,
    CASES_PATH,
    dump_cases_jsonl,
    load_axes,
    load_cases,
    load_expected,
    load_manifest,
)
from evals.combo_matrix.report import coverage_report
from evals.combo_matrix.runner import observe
from evals.combo_matrix.schema import AxesDocument, ComboCase, ExpectedBehaviorRow

pytestmark = pytest.mark.eval


# ─────────── 1. 재현성 ───────────


def test_regeneration_matches_committed_cases_byte_identical() -> None:
    doc = load_axes()
    result = generate(doc)
    cases = assemble_cases(result)
    cases = add_perturbations(doc, cases)
    cases = add_directed_cases(doc, cases)
    regenerated_text = dump_cases_jsonl(cases)
    committed_text = CASES_PATH.read_text(encoding="utf-8")
    assert regenerated_text == committed_text, (
        "같은 axes.json+seed 재생성이 커밋된 combo_cases.jsonl 과 바이트 동일해야 한다"
    )
    manifest = load_manifest()
    assert manifest.cases_sha256 == hashlib.sha256(committed_text.encode("utf-8")).hexdigest()
    assert manifest.axes_sha256 == hashlib.sha256(AXES_PATH.read_bytes()).hexdigest()


# ─────────── 2. 제약 검증 ───────────


def test_all_committed_cases_satisfy_axes_constraints() -> None:
    from evals.combo_matrix.generator import ConstraintIndex

    doc = load_axes()
    idx = ConstraintIndex(doc)
    cases = load_cases()
    assert cases
    for case in cases:
        assert idx.is_valid_leaf(case.axes), f"{case.case_id} 가 axes.json 제약을 위반한다"
        for axis, value in case.axes.items():
            domain = doc.axis_by_id()[axis].value_ids()
            assert value in domain, f"{case.case_id}.{axis}={value} 는 axes.json 도메인 밖이다"


# ─────────── 3. 스키마 검증 ───────────


def test_all_cases_and_expected_rows_satisfy_schema() -> None:
    cases = load_cases()  # ComboCase.model_validate 가 로드 시점에 이미 강제
    rows = load_expected()  # ExpectedBehaviorRow.model_validate 도 마찬가지
    assert {c.case_id for c in cases} == {r.case_id for r in rows}
    for row in rows:
        if row.status == "defined":
            assert row.evidence, f"{row.case_id}: defined 는 evidence >= 1 이어야 한다"
            assert row.expected
        if row.status in ("undefined", "partial"):
            assert row.undefined_tuple, f"{row.case_id}: {row.status} 는 undefined_tuple 필요"


def test_undefined_tuple_keys_are_axis_ids_only() -> None:
    """`undefined_tuple` 은 셀 좌표(축값 조합)다 — `aspect` 같은 의사 축이 섞이면 UNDEFINED_CELLS.md
    의 좌표 체계가 무너진다(리뷰 R2). 세부 구분은 별도 `aspect` 필드로만 표현해야 한다."""
    doc = load_axes()
    valid_axis_ids = {a.id for a in doc.axes}
    for row in load_expected():
        if not row.undefined_tuple:
            continue
        bad_keys = set(row.undefined_tuple) - valid_axis_ids
        assert not bad_keys, (
            f"{row.case_id}: undefined_tuple 에 축이 아닌 키 {bad_keys} — aspect 필드로 옮길 것"
        )


# ─────────── 4. 드리프트 가드 ───────────


def test_filter_axes_match_decompose_filter_axes() -> None:
    from app.agents.buyer.recommendation.decompose import _FILTER_AXES

    assert set(FILTER_AXES) == set(_FILTER_AXES)


def test_intent_axis_matches_route_decision_literal() -> None:
    from app.agents.buyer.recommendation.state import RouteDecision

    doc = load_axes()
    hints = typing.get_type_hints(RouteDecision)
    code_intents = set(typing.get_args(hints["intent"]))
    axes_intents = set(doc.axis_by_id()["intent"].value_ids()) - {"n/a"}
    assert axes_intents == code_intents, (
        "axes.json intent 축이 RouteDecision.intent Literal 과 어긋난다 — "
        "코드가 축을 늘리면 이 테스트가 깨져 매트릭스 갱신을 강제한다"
    )


def test_context_axis_is_subset_of_intent_probe_context_ids() -> None:
    from evals.intent_probe.schema import CONTEXT_IDS

    doc = load_axes()
    axes_context = set(doc.axis_by_id()["context"].value_ids()) - {"n/a"}
    assert axes_context <= set(CONTEXT_IDS)


# ─────────── 5. 커버리지 하한 ───────────


def test_coverage_ratios_match_recorded_values() -> None:
    report = coverage_report()
    manifest = load_manifest()
    assert report["pairwise"]["denominator"] == manifest.generator_params["validPairs"]
    assert report["pairwise"]["numerator"] == report["pairwise"]["denominator"], (
        "커밋된 케이스가 유효 쌍 전체를 커버해야 한다(생성 시점 기준 재현)"
    )
    assert report["pairwise"]["uncovered"] == []
    for rt_id, recorded in manifest.generator_params["riskTriples"].items():
        assert report["riskTriples"][rt_id]["targetCount"] == recorded["targets"]
        assert report["riskTriples"][rt_id]["coveredCount"] == recorded["covered"]
        assert (
            report["riskTriples"][rt_id]["coveredCount"]
            == report["riskTriples"][rt_id]["targetCount"]
        )
    # 1-wise 비교 참고선은 pairwise 보다 항상 좁아야 한다(§6 정량 근거 정신).
    assert report["oneWiseBaseline"]["ratio"] < report["pairwise"]["ratio"]
    assert report["oneWiseBaseline"]["suiteSize"] < len(load_cases())


def test_no_leaf_pair_is_silently_dropped() -> None:
    """제약 위반 배제 후 유효한 쌍 = leaf 전체 합집합. 사후 필터로 조용히 줄지 않았는지 재확인."""
    doc = load_axes()
    leaves = enumerate_leaves(doc)
    valid_pairs: set = set()
    for leaf in leaves:
        valid_pairs |= all_pairs(leaf)
    report = coverage_report()
    assert report["pairwise"]["denominator"] == len(valid_pairs)


# ─────────── 6. 결정론 관측 ───────────


async def test_ci_cases_execute_and_defined_cases_match_contract() -> None:
    cases = {c.case_id: c for c in load_cases()}
    rows = {r.case_id: r for r in load_expected()}

    ci_cases = [c for c in cases.values() if c.observation_mode == "ci"]
    assert ci_cases, "ci 케이스가 하나도 없으면 이 하네스가 아무것도 관측하지 못한다"

    for case in ci_cases:
        observed = await observe(case)
        assert observed is not None, f"{case.case_id}: ci 케이스는 관측 결과가 있어야 한다"
        row = rows[case.case_id]
        if row.status != "defined":
            continue  # undefined/partial 은 관측·기록만 — 기대값으로 고정하지 않는다(§5).
        if "note" in observed and ("실행 생략" in observed["note"]):
            continue
        if case.axes["surface"] == "HOME":
            degrade = case.axes["degrade"]
            if degrade in ("catalog_unavailable", "catalog_timeout"):
                # #367 — 카탈로그 인덱스 장애/타임아웃은 outcome 이 아니라 예외(503/504)로 답한다
                # (api-spec §3.7 v0.26.1 「HOME 실패 모드」 표).
                assert "exception" in observed, f"{case.case_id}: {degrade} 는 exception 이 있어야"
                expected_status = 503 if degrade == "catalog_unavailable" else 504
                assert observed["statusCode"] == expected_status, case.case_id
                continue
            assert "outcome" in observed, f"{case.case_id}: HOME defined 케이스는 outcome 이 있어야"
            assert observed["outcome"] in ("NO_PROFILE", "INSUFFICIENT_CANDIDATES", "PERSONALIZED")
            continue
        # CHAT — defined 라벨의 계약 형태를 대조한다(§6 결정론 관측).
        assert observed["terminal"] in ("done", "error"), case.case_id
        if case.axes["intent"] == "recommend":
            degrade = case.axes["degrade"]
            constraint_strength = case.axes["constraint_strength"]
            if degrade == "spring_timeout" and constraint_strength != "unspecified":
                # 무지정 턴은 popular_fn(I-3) 폴백이 search 보다 우선이라 예외다(runner.py 주석).
                assert observed["terminal"] == "error"
                assert observed["errorCode"] == "SEARCH_FAILED"
            elif constraint_strength == "overspecified_zero" and degrade != "spring_timeout":
                # 리뷰 R1 — 정의상 검색 0건이어야 자동완화·zero_result 종료가 실제로 돈다.
                assert observed["terminal"] == "done"
                assert observed["finishReason"] == "zero_result"
                assert observed["pushCount"] == 0
            else:
                assert observed["terminal"] == "done"
                assert observed["pushCount"] == 1
                # 이 매트릭스는 categoryQueries 를 채우지 않아 buy_all_mode(세트)가 트리거되지
                # 않는다 — 표준 검색 경로는 항상 PICK_ONE (state.py:128-131 명시 설계).
                assert observed["listType"] == "PICK_ONE"
        elif case.axes["intent"] == "cart_add" and case.axes["degrade"] == "spring_timeout":
            # 리뷰 R7 — add_to_cart 몽키패치가 실제로 CartError 계열 catch 를 태워야 한다.
            assert observed["terminal"] == "done", case.case_id
            assert observed["actionType"] == "CART_ADD_FAILED", case.case_id
            assert observed["actionReason"] == "CART_ERROR", case.case_id
        elif case.axes["intent"] == "order_status" and case.axes["degrade"] == "spring_timeout":
            assert observed["terminal"] == "done", case.case_id
            if case.axes["identity"] == "member":
                # 리뷰 R8 — failing_order_status 가 OrderStatusUnavailableError 를 실제로
                # 태워야 한다(member_order_identity 가 로그인 게이트로 안 걸러야 도달).
                assert observed["lastTokenText"] and "불러오지 못했" in observed["lastTokenText"], (
                    case.case_id
                )
            else:
                # identity=guest 는 로그인 게이트가 fetch_order_status 호출보다 먼저 걸려
                # 이 축을 실제로 밟지 않는다(wishlist_add 와 같은 유형, 리뷰 R2 R8 대응 중 발견).
                assert observed["lastTokenText"] and "로그인" in observed["lastTokenText"], (
                    case.case_id
                )
        else:
            assert observed["terminal"] == "done", case.case_id


# ─────────── 6b. HOME 픽스처 공허 방지 가드 (리뷰 F1) ───────────
#
# `_observe_home` 의 건강한 픽스처가 `home_reco_min_candidates` 미만이면 `rank_home` 이
# INSUFFICIENT_CANDIDATES 로 조기 반환해 reason 관측 경로에 영원히 도달하지 못한다 — 그
# 상태에서도 `reasonsNull: True` 는 빈 리스트에 대한 vacuous truth(all([]) == True)라 겉으로는
# 통과해 버린다. 아래 두 테스트가 그 공허를 다시 못 밟게 잠근다.


async def test_home_healthy_fixture_meets_min_candidates() -> None:
    """건강한 스토어 HOME 케이스(degrade=none)가 실제로 `home_reco_min_candidates` 이상의
    후보를 채워 PERSONALIZED + itemCount>0 로 관측되는지 — 픽스처 상품 수가 설정값 아래로
    떨어지면 여기서 시끄럽게 깨진다."""
    from app.core.config import get_settings

    cases = {c.case_id: c for c in load_cases()}
    home_none = next(
        c for c in cases.values() if c.axes["surface"] == "HOME" and c.axes["degrade"] == "none"
    )
    observed = await observe(home_none)
    assert observed is not None
    assert observed["outcome"] == "PERSONALIZED", observed
    assert observed["itemCount"] > 0
    assert observed["itemCount"] >= get_settings().home_reco_min_candidates


async def test_home_reason_degraded_injection_actually_runs() -> None:
    """`reason_degraded` 케이스에서 `build_reasons` 주입이 실제로 호출됐고 reason 이 전부
    null 인 반면, 주입 없는 대응 케이스(degrade=none)는 reason 이 하나 이상 채워지는지 —
    build_reasons 가 조기 반환에 막혀 영원히 호출되지 않는 공허를 막는다."""
    cases = {c.case_id: c for c in load_cases()}
    home_none = next(
        c for c in cases.values() if c.axes["surface"] == "HOME" and c.axes["degrade"] == "none"
    )
    home_reason_degraded = next(
        c
        for c in cases.values()
        if c.axes["surface"] == "HOME" and c.axes["degrade"] == "reason_degraded"
    )
    observed_none = await observe(home_none)
    observed_degraded = await observe(home_reason_degraded)
    assert observed_none is not None
    assert observed_degraded is not None

    assert observed_degraded["buildReasonsInvoked"] is True, observed_degraded
    assert observed_degraded["itemCount"] > 0
    assert observed_degraded["reasonsNull"] is True, observed_degraded
    assert observed_degraded["reasonsFilledCount"] == 0

    assert observed_none["reasonsFilledCount"] > 0, (
        "주입 없는 대응 케이스에서 reason 이 하나도 안 채워지면 위 대비가 성립하지 않는다"
    )
    assert observed_none["reasonsNull"] is False


async def test_home_profile_unavailable_injection_actually_runs() -> None:
    """`profile_unavailable` 은 outcome 만으로 degrade=none 과 구별되지 않는다(계약상 와이어
    구별 신호 없음, api-spec §3.7 v0.26.1) — 러너 계측(profileHookInvoked)이 실패 주입이
    실제로 실행됐음을 증명해야 이 셀이 "관측됐다"고 말할 수 있다."""
    cases = {c.case_id: c for c in load_cases()}
    home_profile_unavailable = next(
        c
        for c in cases.values()
        if c.axes["surface"] == "HOME" and c.axes["degrade"] == "profile_unavailable"
    )
    observed = await observe(home_profile_unavailable)
    assert observed is not None
    assert observed["profileHookInvoked"] is True, observed
    assert observed["outcome"] == "PERSONALIZED", observed


# ─────────── 7. 미정의 셀 최소 보증 ───────────


def test_336_cell_is_tracked_as_undefined() -> None:
    rows = load_expected()
    hits = [
        r
        for r in rows
        if r.undefined_tuple
        and r.undefined_tuple.get("constraint_strength") == "unspecified"
        and r.undefined_tuple.get("total_budget") == "present"
        and r.undefined_tuple.get("buy_all") == "true"
    ]
    assert hits, "#336 셀(무지정+예산+세트)이 undefined/partial 목록에 없다"
    assert any(r.tracking and "#336" in r.tracking for r in hits)
    for hit in hits:
        assert set(hit.undefined_tuple) == {"constraint_strength", "total_budget", "buy_all"}, (
            "#336 셀 좌표는 축 3개만 — aspect 는 별도 필드로(리뷰 R2)"
        )


def test_recall_dir_case_is_not_a_spec_gap() -> None:
    """combo-0055(DIR 회원 recall >= 게스트) 는 관측 범위 한계지 스펙 구멍이 아니다(리뷰 R2) —
    UNDEFINED_CELLS.md 를 오염시키지 않도록 defined 로 분류돼야 한다."""
    rows = {r.case_id: r for r in load_expected()}
    cases = {c.case_id: c for c in load_cases()}
    recall_dir = next(
        c
        for c in cases.values()
        if c.checklist_type == "DIR" and c.perturbation_of and "recall" in c.utterance.lower()
    )
    row = rows[recall_dir.case_id]
    assert row.status == "defined"
    assert row.undefined_tuple is None


def test_axes_document_is_the_single_source_of_truth() -> None:
    """스키마·axes.json 정합 자체를 한 번 더 — AxesDocument 로 로드되면 그걸로 충분."""
    doc = load_axes()
    assert isinstance(doc, AxesDocument)
    assert len(doc.axes) >= 10
    for case in load_cases():
        assert isinstance(case, ComboCase)
    for row in load_expected():
        assert isinstance(row, ExpectedBehaviorRow)
