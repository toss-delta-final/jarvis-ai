"""기능 조합 커버리지 매트릭스 하네스 게이트 (이슈 #335 §7) — @pytest.mark.eval / slow, 기본 PR pytest 에서는 제외되고 별도 워크플로우에서 돈다."""

from __future__ import annotations

import hashlib
import typing

import pytest

from app.schemas.spring import ProductSearchFilters
from app.services import search_service
from app.services.spring_client import CartError, WishlistError
from evals.combo_matrix import fakes
from evals.combo_matrix.__main__ import refresh_observed
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
from evals.combo_matrix.runner import (
    _SEARCH_FILTER_KEYS,
    _failing_add_to_cart,
    _failing_add_wishlist,
    build_decompose_json,
    observe,
)
from evals.combo_matrix.schema import (
    OBSERVED_GUARDED_FIELDS,
    AxesDocument,
    ComboCase,
    ExpectedBehaviorRow,
)

# 전체 콤보 매트릭스를 여러 테스트 함수가 각자 observe()/refresh_observed()로 재관측 —
# 기본 PR pytest에서 제외하고 별도 워크플로우(nightly/수동)에서만 돈다 (CI 30분 병목, PR#? 참고).
pytestmark = [pytest.mark.eval, pytest.mark.slow]


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


def test_intent_axis_values_all_generated_or_explicitly_excluded() -> None:
    """[#285, I-25 4단계] intent 축의 모든 값은 최소 1개 leaf 에 나타나야 한다 — 단, `excludes`
    규칙이 그 값을 **명시적으로 이름 지어**(`forbid.intent` 에 값 문자열로) 제외한 경우는 예외다.

    위 `test_intent_axis_matches_route_decision_literal` 은 axes.json 의 intent **값 목록**이
    `RouteDecision.intent` 와 어휘 일치하는지만 본다 — 값을 목록에 올려놓고도 constraints 가
    조용히 모든 leaf 에서 그 값을 걸러(생성기가 그 값의 leaf 를 하나도 못 만들어) 커버리지가
    비어도 그 테스트는 통과한다. `cart_quantity_not_generated` 처럼 **의도된** 제외는 옳지만,
    다음에 누가 축값을 추가하고 실수로(오타·제약 축 이름 오기 등) 아무 leaf 도 못 얻으면 그
    구멍이 아무 신호 없이 남는다 — 오늘의 암묵적 구멍을 "제외하려면 반드시 그 값의 이름을
    `forbid.intent` 에 적어야 한다"는 명시적 탈출구 하나로 바꾼다."""
    doc = load_axes()
    leaves = enumerate_leaves(doc)
    generated_intents = {leaf["intent"] for leaf in leaves}
    all_intent_values = set(doc.axis_by_id()["intent"].value_ids()) - {"n/a"}

    explicitly_excluded = {
        value
        for constraint in doc.constraints
        if constraint.type == "excludes"
        for value in constraint.forbid.get("intent", [])
    }

    missing = all_intent_values - generated_intents
    unexplained = missing - explicitly_excluded
    assert not unexplained, (
        f"intent 값 {sorted(unexplained)} 이 leaf 생성에서 빠졌는데 어떤 excludes 규칙도 "
        "forbid.intent 에 그 값의 이름을 적어 제외를 선언하지 않는다 — 의도된 제외라면 "
        "excludes 규칙을 추가하고, 실수라면 leaf 를 못 만드는 제약을 고쳐라."
    )


def test_context_axis_is_subset_of_intent_probe_context_ids() -> None:
    from evals.intent_probe.schema import CONTEXT_IDS

    doc = load_axes()
    axes_context = set(doc.axis_by_id()["context"].value_ids()) - {"n/a"}
    assert axes_context <= set(CONTEXT_IDS)


# ─────────── 4a. observed 드리프트 가드 (이슈 #424) ───────────


async def test_observed_guarded_fields_match_recomputed_values_for_all_ci_rows() -> None:
    """`expected_behavior.jsonl` 의 `observed` 는 러너 재실행 **기록**이라, 다른 레인이 SSE 이벤트를
    바꾸면 커밋본이 조용히 낡아도 아무 테스트도 잡지 못했다(#424 — PR #420 작업 중 실측 2회, 둘 다
    `eventTypes` 만 드리프트했고 핵심 계약 필드는 불변). 전량 byte diff 는 SSE 를 건드리는 모든
    레인(동시 6~8개)에 이 eval 데이터 재생성을 강제해 레인 결합 비용이 크므로, `OBSERVED_
    GUARDED_FIELDS`(schema.py, 근거 동봉)로 추린 핵심 계약 필드만 딕셔너리째(키 존재 여부 포함)
    대조한다.

    **행 범위: `status` 와 무관하게 `observed` 가 있는 모든 ci 행**(partial 인 combo-0038 포함) —
    이건 기록 신선도 검사이지 미정의 동작의 스펙화가 아니다. `status`·`expected`·`undefined_tuple`
    은 이 테스트가 보지 않으며, 기록이 낡는 문제는 defined/partial 을 가리지 않는다.
    """
    committed_rows = {r.case_id: r for r in load_expected()}
    _, refreshed_rows = await refresh_observed(write=False)

    mismatches: list[tuple[str, dict, dict]] = []
    for refreshed in refreshed_rows:
        committed = committed_rows[refreshed.case_id]
        if committed.observed is None:
            continue  # manual 행(observed 항상 null) — 대조 대상 아님
        committed_proj = {
            k: v for k, v in committed.observed.items() if k in OBSERVED_GUARDED_FIELDS
        }
        refreshed_proj = {
            k: v for k, v in (refreshed.observed or {}).items() if k in OBSERVED_GUARDED_FIELDS
        }
        if committed_proj != refreshed_proj:
            mismatches.append((refreshed.case_id, committed_proj, refreshed_proj))

    if not mismatches:
        return

    lines = [
        "observed 핵심 계약 필드 드리프트 감지 — 커밋본과 재실행 결과가 어긋난다:",
    ]
    for case_id, committed_proj, refreshed_proj in mismatches:
        for key in sorted(set(committed_proj) | set(refreshed_proj)):
            old = committed_proj.get(key, "<필드 없음>")
            new = refreshed_proj.get(key, "<필드 없음>")
            if old != new:
                lines.append(f"  {case_id}.{key}: 커밋본={old!r} → 재실행={new!r}")
    lines.append("")
    lines.append("조치: `uv run python -m evals.combo_matrix refresh-observed` 로 갱신한 뒤")
    lines.append(
        "evals/combo_matrix/README.md 「관측 재생성 이력」에 무엇이 왜 바뀌었는지"
        "(실측 개선/회귀/필드 추가) 판정을 남길 것."
    )
    lines.append(
        "eventTypes·lastTokenText·notes 만 바뀐 경우엔 이 가드가 깨지지 않는다 — "
        "이 가드가 깨졌다면 핵심 계약 필드가 실제로 바뀐 것이다(OBSERVED_GUARDED_FIELDS 참조)."
    )
    pytest.fail("\n".join(lines))


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
                # #381 D5 — category=="present" 이면 이제 categoryQueries 를 채워 leg 를 1개
                # 만든다(구 서술 "categoryQueries 를 채우지 않는다"는 실측으로 반증됐다). 그래도
                # BUY_ALL 은 "니즈 2개 이상"이 조건이라(state.py:128-131) leg 1개로는 여전히
                # 트리거되지 않는다 — 실측으로 확인(모든 ci recommend 케이스가 PICK_ONE 로 관측됨).
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


# ─────────── 6a. 담기 계열 degrade 주입의 예외 타입 (이슈 #376) ───────────


async def test_add_wishlist_and_add_to_cart_injections_match_adapter_exception_convention() -> None:
    """`spring_timeout` 축이 담기 계열에 주입하는 fake 가 실 어댑터 규약(WishlistError/CartError)과
    같은 예외를 내는지 직접 호출로 잠근다 — `SpringUnavailableError` 로 되돌리면 이 테스트가
    반드시 깨진다(변이 시험 확인 후 원복, 보고 참조). 실 어댑터 근거는 `app/services/
    spring_client.py::add_wishlist`/`add_to_cart` 의 `except httpx.HTTPError` 가 각각
    `WishlistError`/`CartError` 로 낙성하는 것 — `httpx.TimeoutException` 은 `httpx.HTTPError` 의
    하위 클래스라 타임아웃도 같은 낙성 결과다."""
    with pytest.raises(WishlistError):
        await _failing_add_wishlist()
    with pytest.raises(CartError):
        await _failing_add_to_cart()


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


# ─────────── 6c. overspecified_zero 판정 잠금 (이슈 #425) ───────────


async def test_overspecified_zero_has_no_relaxable_axis_so_no_relaxation_search() -> None:
    """`constraint_strength=overspecified_zero` 는 0건이면서도 자동완화·완화칩이 돌지 않는다 —
    갭이 아니라 **완화 가능 축이 하나도 없어서 생기는 정의된 동작**이다(#425 판정).

    전제(코드 근거): `app.agents.buyer.recommendation.relaxation.FIELD_TO_ATTR` 는 `priceMax`·
    `ratingMin`·`brand`·`color` 뿐이다(모듈 docstring "비카테고리 조건(가격 상한·평점 하한·브랜드·
    색상)만 한 단계 푼다") — `price_min` 은 완화 축이 아니다. `app.core.config.
    Settings._require_known_relaxation_chip_fields` 가 기동 시점에 `FIELD_TO_ATTR` 밖 이름을
    거부하므로 config 로도 `priceMin` 을 완화 축에 넣을 수 없다. combo-0031(overspecified_zero)의
    실현 필터는 `price_min` 하나뿐이라 `build_relaxation_candidates(filters, settings) == []` 다.

    이로부터 나오는 관측(재검색 0회): `app.agents.buyer.recommendation.graph.
    stream_recommendation` 의 `may_auto_relax` 게이트가 False, 자동완화 루프(`if not candidates
    and not underspecified:`)는 진입해도 후보가 비어 0회 반복, 완화 칩 블록(`if not underspecified
    and (not candidates or len(candidates) < settings.relaxation_min_results):`)도 진입해도 probe
    후보가 비어 칩 0개 — 그래서 `searchCallCount == 1`·`finishReason == "zero_result"` 다.

    이 축에 완화 가능 축이 생겼다면(예: `FIELD_TO_ATTR` 에 `price_min` 추가) #425 판정("자동완화·
    완화칩은 돌지 않는다 — 정의된 동작")과 README 서술을 함께 재판정해야 한다.
    """
    from app.agents.buyer.recommendation.relaxation import build_relaxation_candidates
    from app.core.config import get_settings
    from app.schemas.spring import ProductSearchFilters

    cases = {c.case_id: c for c in load_cases()}
    # [#386] `degrade` 를 `none` 으로 좁힌다 — **판정을 완화한 게 아니라 적용 범위를 정확히 한
    # 것이다.** 이 판정은 "0건 → 완화 단계에서 후보가 없어 재검색 0회 → zero_result 종료"라는
    # 경로에 대한 것인데, `degrade=spring_timeout` 케이스는 검색 자체가 실패해(SEARCH_FAILED)
    # 완화 단계에 **닿지도 않는다** — 거기서 "완화 후보가 없어야 한다"를 요구하면 판정과 무관한
    # 축(필터 8축 전부 present 여도 무방한 케이스)까지 끌어들여 테스트가 엉뚱한 곳에서 깨진다.
    #
    # #448 시점에는 overspecified_zero ci 케이스가 `degrade=none` 하나뿐이라 이 구분이 필요
    # 없었다. #386 재생성으로 그 자리가 `spring_timeout` 으로 바뀌면서 드러났고, 판정이 관측되던
    # `degrade=none` 자리는 `axes.json` 의 `overspecified_zero_member_none_price_min`
    # directedCase 로 복원했다(그 `reason` 에 경위가 있다).
    overspecified_zero_ci_cases = [
        c
        for c in cases.values()
        if c.observation_mode == "ci"
        and c.axes.get("constraint_strength") == "overspecified_zero"
        and c.axes.get("degrade") == "none"
    ]
    assert overspecified_zero_ci_cases, (
        "overspecified_zero × degrade=none 인 ci 케이스가 하나도 없다 — #425 판정을 관측할 자리가 "
        "사라졌다는 뜻이다(재생성이 그 조합을 지우면 directedCase 로 복원할 것)."
    )

    settings = get_settings()
    for case in overspecified_zero_ci_cases:
        filters = ProductSearchFilters.model_validate(build_decompose_json(case.axes)["filters"])
        candidates = build_relaxation_candidates(filters, settings)
        assert candidates == [], (
            f"{case.case_id}: 완화 후보가 생겼다 — #425 판정이 전제하는 '완화 가능 축이 하나도 "
            f"없다'가 더 이상 성립하지 않는다(candidates={candidates}). README·판정 재검토 필요."
        )

        observed = await observe(case)
        assert observed is not None
        assert observed["searchCallCount"] == 1, (
            f"{case.case_id}: searchCallCount={observed['searchCallCount']} — 완화 후보가 없는데도 "
            "재검색이 돌았다면 #425 판정과 어긋난다."
        )
        assert observed["finishReason"] == "zero_result", (
            f"{case.case_id}: finishReason={observed['finishReason']!r} — 0건 zero_result 종료가 "
            "아니라면 #425 판정의 전제가 깨진 것이다."
        )


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


# ─────────── 8. 검색 필터 경계 관측 (이슈 #381 D8) ───────────


async def test_representable_filters_actually_narrow_search_results() -> None:
    """D8-1 — 표현 가능한 하드필터(category·brand·rating_min)가 present 인 ci 케이스는 결과가
    필터 없을 때보다 실제로 줄어든다(공허 통과 방지). combo-0058(필터 8축 전부 present)의
    (#386 재생성 전에는 combo-0026 이 이 자리였다 — 축 조합이 같은 케이스로 옮겼다: recommend·
    guest·rerank_failed·case=3·normal·필터 8축 present. 관측값도 그대로다.)
    `PAIR_CATALOG`(4건) 대비 product 101 하나만 category=무선이어폰·brand=나이키·
    rating_min=4.0·price 20000~50000 을 전부 만족한다."""
    cases = {c.case_id: c for c in load_cases()}
    observed = await observe(cases["combo-0058"])
    assert observed["searchCallCount"] > 0
    assert observed["pushProductCount"] == 1, (
        "combo-0058 은 category·brand·rating_min·price 하드필터로 4건 중 1건(product 101)만 "
        "남아야 한다 — 필터가 실제로 결과를 줄이지 않으면 이 값이 4에 가깝게 나온다"
    )


async def test_search_filters_is_boundary_value_not_injected_value() -> None:
    """D8-2 — `observed.searchFilters` 는 decompose 주입값이 아니라 search 콜러블이 실제로 받은
    경계 도달값(첫 호출)이다. combo-0058 은 decompose 산출 filters.keyword="가벼운" 을 주입하지만,
    category leg 가 있으면(#381 D5) `#51` 규칙(`app/agents/buyer/recommendation/graph.py::_leg` 의
    `leg_keyword = None if drop_keyword else ...`)이 leg 검색어에서 keyword 를 비운다 — 주입값과
    경계 도달값이 실제로 갈리는 축이다. 또한 첫 호출은 자동완화 재검색(축별로 하나씩 완화)이 시작되기
    전 값이라 이후 호출(예: color 완화 재검색)과도 달라야 한다 — "첫 호출" 계약 자체를 잠근다."""
    cases = {c.case_id: c for c in load_cases()}
    case = cases["combo-0058"]
    injected = build_decompose_json(case.axes)["filters"]
    assert injected["keyword"] == "가벼운"

    observed = await observe(case)
    search_filters = observed["searchFilters"]
    assert search_filters is not None
    assert search_filters["keyword"] is None, (
        "주입값(가벼운)과 경계 도달값이 같으면 searchFilters 가 여전히 decompose 산출을 그대로 "
        "싣고 있다는 뜻 — 실제 search 콜러블 호출을 관측한 게 아니다"
    )
    # 첫 호출은 색상 완화가 아직 안 일어난 상태라 color 가 살아 있어야 한다(자동완화 재검색이
    # color 를 지운 이후 호출과 구별) — "첫 호출(주 검색)" 계약을 직접 잠근다.
    assert search_filters["color"] == "블랙"


async def test_every_filter_axis_is_actually_measured_by_some_ci_case() -> None:
    """D8-3(#426) — 하드필터 8축 **각각**에 대해, 그 축이 검색 경계에서 실제로 재진 ci 케이스가
    최소 1건 존재한다.

    #381 이 남긴 공백을 실행 가능한 게이트로 바꾼 테스트다. 예전 D8-3 은
    `unappliedSearchFilters == {"color","attrConditions"}` 를 단언했는데, 그건 "이 축을 못 잰다"를
    고정하는 단언이라 못 재는 상태가 영구화된다. 여기서는 반대로 **재고 있음**을 요구한다.

    - Spring 와이어 7축: 경계 도달값(`observed.searchFilters[k]`)이 non-null 인 케이스가 있는가.
      `keyword` 는 `#51 drop_keyword`(category leg 가 있으면 앱이 leg 검색어에서 비운다) 때문에
      **category 가 없는 케이스**가 있어야만 도달한다 — 대역의 한계가 아니라 앱의 정의된 동작이다.
    - `attr_conditions` 는 Spring payload 축이 아니라 AI 사후필터라 `searchFilters` 로는 잴 수
      없다 — 사후필터가 실제로 호출됐는지(`attrConditionsPostFilter.invoked`)로 잰다.
    """
    # `attrConditions` 는 Spring 에 안 나가는 AI 사후필터 축이라 `searchFilters` 에 값이 실려
    # 있어도 그건 "경계까지 실려 갔다"일 뿐 "적용됐다"가 아니다 — 그 축만 계측으로 판정한다.
    wire_axes = tuple(k for k in _SEARCH_FILTER_KEYS if k != "attrConditions")
    measured: set[str] = set()
    for case in load_cases():
        if case.observation_mode != "ci":
            continue
        observed = await observe(case)
        if not observed:
            continue
        for key, value in (observed.get("searchFilters") or {}).items():
            if key in wire_axes and value not in (None, "", [], {}):
                measured.add(key)
        if (observed.get("attrConditionsPostFilter") or {}).get("invoked"):
            measured.add("attrConditions")

    missing = set(_SEARCH_FILTER_KEYS) - measured
    assert not missing, (
        f"검색 경계에서 한 번도 재지지 않은 하드필터 축: {sorted(missing)} — 그 축은 present/absent 가 "
        "결과에 아무 차이를 만들지 않아, 망가져도 이 하네스가 초록불로 보고한다(이슈 #426)"
    )


async def test_unapplied_axes_are_loud_when_the_fake_cannot_express_them() -> None:
    """D8-3b(D1 loudness 드리프트 가드) — `_UNREPRESENTABLE_FILTER_CAMEL` 에 축을 넣으면 그 축이
    present 인 호출에서 `unapplied_calls` 에 실린다.

    #426 이후 이 목록은 비어 있다(8축 전부 재진다). 그래서 `unappliedSearchFilters == []` 를
    단언하면 빈 리스트끼리 비교하는 **공허한 단언**이 된다 — `docs/lessons.md` 가 기록한 실패다.
    대신 loudness 메커니즘 자체가 살아 있음을 성질로 잠근다: 미래에 대역이 표현 못 하는 축이
    생겨 목록에 등록되면, 조용히 무시되지 않고 데이터로 드러나야 한다.
    """
    assert fakes._UNREPRESENTABLE_FILTER_CAMEL == {}, (
        "#426 기준 대역은 8축을 전부 표현한다 — 축이 추가됐다면 README '알려진 관측 한계'도 함께 갱신할 것"
    )
    search = fakes.make_recording_filtering_search()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fakes, "_UNREPRESENTABLE_FILTER_CAMEL", {"color": "color"})
        await search(ProductSearchFilters(color="블랙"))
        await search(ProductSearchFilters(category="무선이어폰"))
    assert search.unapplied_calls == [["color"], []], search.unapplied_calls


async def test_keyword_axis_actually_narrows_and_dies_under_mutation() -> None:
    """D8-5(#426, 변이 시험) — combo-0063(keyword 만 present)은 대역의 keyword 매칭으로 후보가
    실제로 줄고, 그 매칭을 없애면 관측이 **달라진다**.

    "필터가 걸린다"는 단언은 필터를 빼도 값이 같으면 아무것도 검증하지 않는다(공허). 그래서
    실제로 빼 보고 죽는지 확인한다 — 이슈 #426 체크리스트 5번.
    """
    cases = {c.case_id: c for c in load_cases()}
    observed = await observe(cases["combo-0063"])
    assert observed["searchFilters"]["keyword"] == "가벼운", (
        "category leg 가 없으면 #51 drop_keyword 가 발동하지 않아 keyword 가 경계까지 살아 있어야 한다"
    )
    assert observed["pushProductCount"] == 3, (
        "PAIR_CATALOG 5건 중 name+summary+attributes 에 '가벼운' 이 없는 102·105 가 탈락해 "
        "3건이어야 한다(105 는 summary/attributes 가 비어 있어 LIKE 대상이 name 뿐이다 — "
        "데이터 부재로 제외되는 것이 Spring LIKE 의 실제 동작이다)"
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fakes.SpringWhereCatalogBackend, "search", _search_ignoring("keyword"))
        mutated = await observe(cases["combo-0063"])
    assert mutated["pushProductCount"] == 5, (
        "keyword 매칭을 빼도 결과가 같다면 이 케이스는 keyword 축을 재고 있지 않다(공허한 단언)"
    )


async def test_color_axis_actually_narrows_and_dies_under_mutation() -> None:
    """D8-6(#426, 변이 시험) — combo-0064(color 만 present)에서 color 가 결정타다."""
    cases = {c.case_id: c for c in load_cases()}
    observed = await observe(cases["combo-0064"])
    assert observed["searchFilters"]["color"] == "블랙"
    assert observed["pushProductCount"] == 3, (
        "attributes 색상이 블랙이 아닌 102 와 attributes 자체가 없는 105 가 탈락해 3건이어야 한다"
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fakes.SpringWhereCatalogBackend, "search", _search_ignoring("color"))
        mutated = await observe(cases["combo-0064"])
    assert mutated["pushProductCount"] == 5, (
        "color 매칭을 빼도 결과가 같다면 이 케이스는 color 축을 재고 있지 않다(공허한 단언)"
    )


async def test_attr_conditions_post_filter_actually_narrows_and_dies_under_mutation() -> None:
    """D8-7(#426, 변이 시험) — combo-0065(attr_conditions 만 present)은 **사후필터가 실제로
    호출돼** 후보를 좁히고, 그 사후필터를 무력화하면 관측이 달라진다.

    이 케이스는 Spring 와이어 축이 전부 absent 라 #393 A 로 후보 소스가 I-3(인기 상품)이 된다 —
    `searchCallCount == 0` 이라 search 대역이 아예 개입하지 않고, 사후필터 계측만이 이 축의
    유일한 관측 수단이다(계측을 fakes 인스턴스가 아니라 runner 스파이로 둔 이유).
    """
    cases = {c.case_id: c for c in load_cases()}
    observed = await observe(cases["combo-0065"])
    assert observed["searchCallCount"] == 0, "이 케이스는 인기 상품 폴백 경로여야 한다(#393 A)"
    assert observed["attrConditionsPostFilter"] == {
        "invoked": True,
        "inputCount": 3,
        "outputCount": 2,
    }, observed["attrConditionsPostFilter"]
    assert observed["pushProductCount"] == 2, "방수=False 인 102 가 실제로 걸러져야 한다"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(search_service, "_apply_attr_conditions", lambda products, conditions: products)
        mutated = await observe(cases["combo-0065"])
    assert mutated["pushProductCount"] == 3, (
        "사후필터를 무력화해도 결과가 같다면 이 케이스는 attr_conditions 축을 재고 있지 않다"
    )


def _search_ignoring(axis: str):
    """`SpringWhereCatalogBackend.search` 에서 축 하나를 무시하는 변이체 — 원본을 복사하지 않고
    필터 객체에서 그 축만 지운 뒤 원본에 위임한다(대역 로직을 두 벌 갖지 않기 위해)."""
    original = fakes.SpringWhereCatalogBackend.search

    async def _mutated(self, filters):
        return await original(self, filters.model_copy(update={axis: None}))

    return _mutated


async def test_search_not_called_cases_report_zero_call_count_and_null_filters() -> None:
    """D8-4 — search 콜러블이 아예 안 불린 케이스(cart_add 등)는 `searchCallCount == 0`·
    `searchFilters is None` 이다 — "안 불렸다"와 "불렸지만 전부 null 이었다"를 데이터에서
    구별한다."""
    cases = {c.case_id: c for c in load_cases()}
    observed = await observe(cases["combo-0004"])  # cart_add, degrade=spring_timeout
    assert observed["searchCallCount"] == 0
    assert observed["searchFilters"] is None
    assert observed["unappliedSearchFilters"] == []
