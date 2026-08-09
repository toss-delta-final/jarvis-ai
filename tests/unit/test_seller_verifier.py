"""보고서 결정론 검증 테스트 (SPEC-SELLER-001 §10-⑦ 전반부 — 순수 함수, LLM 없음)."""

from __future__ import annotations

from app.agents.seller.schemas import AnalysisFinding
from app.agents.seller.verifier import (
    DETERMINISTIC_CHECKS,
    FINDING_CHECKS,
    run_deterministic_checks,
    run_finding_checks,
)

# [#504] 구 G1 차트 검증 테스트는 삭제됐다 — 좌표 생성 주체가 LLM → 코드로 바뀌며
# run_chart_checks 자체가 제거됐다(대체 검증: tests/unit/test_seller_charts.py).


def _finding(**overrides) -> AnalysisFinding:
    base = {
        "analysis_type": "sales_anomaly",
        "summary": "6월 12일 매출이 직전 7일 평균 대비 42.1% 급락했다.",
        "evidence": ["06-12 매출 180,000원 (직전 7일 평균 310,000원)"],
        "severity": "warning",
    }
    base.update(overrides)
    return AnalysisFinding(**base)


def test_pass_when_numbers_grounded() -> None:
    """finding 수치만 인용한 정상 보고서는 전체 검사를 통과한다(빈 리스트)."""
    report = (
        "6월 12일 매출이 180,000원으로 직전 7일 평균 310,000원 대비 "
        "42.1% 급락했습니다. 원인 점검을 권장드립니다."
    )
    assert run_deterministic_checks(report, [_finding()]) == []


def test_d1_empty_report_fails() -> None:
    """D1 — 공백뿐인 보고서는 실패한다."""
    reasons = run_deterministic_checks("   \n  ", [_finding()])
    assert any("비어" in r for r in reasons)


def test_d2_novel_number_fails() -> None:
    """D2 — finding 에 없는 수치(환각)를 인용하면 해당 숫자가 사유에 나열된다."""
    report = "매출이 999,999원으로 급락했습니다. 180,000원 대비 심각합니다."
    reasons = run_deterministic_checks(report, [_finding()])
    assert len(reasons) == 1
    assert "999999" in reasons[0]
    assert "180000" not in reasons[0]  # 근거 있는 수치는 통과


def test_d2_small_numbers_are_tolerated() -> None:
    """D2 과탐 완화 — 2자리 이하 숫자("3일 연속" 등)는 근거 대조에서 제외된다."""
    report = "최근 3일 연속 하락했고 06-12 에 180,000원까지 내려갔습니다."
    assert run_deterministic_checks(report, [_finding()]) == []


def test_d3_degrade_must_be_disclosed() -> None:
    """D3 — 확보 실패 finding 이 있으면 보고서가 한계를 명시해야 한다."""
    degraded = _finding(
        analysis_type="abuse",
        summary="데이터 확보 실패 — I-13 조회가 타임아웃되어 분석을 생략했다.",
        evidence=[],
        severity="info",
    )
    hiding = "매출 분석 결과 특이사항이 없습니다."
    reasons = run_deterministic_checks(hiding, [degraded])
    assert any("한계" in r for r in reasons)

    honest = "일부 데이터 확보 실패로 어뷰징 분석은 제외됐습니다."
    assert run_deterministic_checks(honest, [degraded]) == []


def test_d2_dates_are_masked_not_flagged() -> None:
    """R1(3-4) — 연도 계열 날짜 표기(ISO·연-월·N년)는 근거 없는 수치로 오탐하지 않는다."""
    report = (
        "2026-06-01~2026-06-30 기간 분석입니다. 2026년 6월 12일 매출이 "
        "180,000원으로 직전 7일 평균 310,000원 대비 42.1% 급락했습니다."
    )
    assert run_deterministic_checks(report, [_finding()]) == []


def test_d2_still_catches_four_digit_hallucination() -> None:
    """R1 마스킹은 날짜 표기만 — 날짜가 아닌 4자리 환각 수치(2026원 등)는 여전히 잡는다."""
    report = "매출이 2026원으로 떨어졌고 평균은 310,000원입니다."
    reasons = run_deterministic_checks(report, [_finding()])
    assert len(reasons) == 1
    assert "2026" in reasons[0]


def test_d3_structural_detection_survives_rewording() -> None:
    """R2(3-4) — degrade 판정은 구조(severity=info+빈 evidence) — 워커가 '확보 실패'
    문구를 안 써도 은폐를 잡는다(문자열 의존 제거)."""
    reworded = _finding(
        analysis_type="abuse",
        summary="조회가 원활하지 않아 이번에는 어뷰징 분석을 건너뛰었다.",  # 규약 문구 이탈
        evidence=[],
        severity="info",
    )
    hiding = "매출 분석 결과 특이사항이 없습니다. 180,000원과 310,000원, 42.1% 참조."
    reasons = run_deterministic_checks(hiding, [reworded])
    assert any("한계" in r for r in reasons)


def test_d3_info_with_evidence_is_not_degrade() -> None:
    """정상 '이상 없음' finding(info + evidence 있음)은 degrade 로 오인하지 않는다."""
    calm = _finding(
        summary="이상 신호가 없다.",
        evidence=["기간 매출 합계 1,200,000원"],
        severity="info",
    )
    report = "기간 매출 합계는 1,200,000원이며 특이사항이 없습니다."
    assert run_deterministic_checks(report, [calm]) == []


def test_registry_names_unique() -> None:
    """레지스트리 체크 이름은 유일해야 한다 — 로그·디버깅 식별자."""
    names = [name for name, _fn in DETERMINISTIC_CHECKS]
    assert len(names) == len(set(names))
    assert names == ["not_empty", "numbers_grounded", "degrade_disclosed"]


# ── F1~F3 브랜치 분석 검증 (이슈 #242, DESIGN-ANALYSIS-V31-242 §4.3) ────────────
# D1~D3(위)와 별개 레지스트리 — 이 절은 report/보고서가 아니라 finding 1건 +
# 그 브랜치의 도구 원출력만을 본다.


def test_finding_registry_names_unique() -> None:
    """F 레지스트리 체크 이름도 D 레지스트리와 마찬가지로 유일해야 한다."""
    names = [name for name, _fn in FINDING_CHECKS]
    assert len(names) == len(set(names))
    assert names == ["evidence_required", "evidence_grounded", "type_match"]


def test_f_pass_when_grounded_and_type_matches() -> None:
    """도구 출력에 있는 수치만 인용 + 유형 일치 → 전체 통과(빈 리스트)."""
    finding = _finding()  # summary·evidence 에 42.1%·180,000·310,000 포함(위 _finding 정의)
    tool_outputs = ["06-12 매출 180,000원 (직전 7일 평균 310,000원, 42.1% 급락)"]
    assert run_finding_checks(finding, tool_outputs, expected_type="sales_anomaly") == []


def test_f1_evidence_required_fails_when_non_degrade_has_no_evidence() -> None:
    """F1 — degrade 가 아닌데(severity!=info 등) evidence 가 비면 실패."""
    finding = _finding(evidence=[], severity="warning")
    reasons = run_finding_checks(finding, [], expected_type="sales_anomaly")
    assert any("evidence" in r for r in reasons)


def test_f1_exempts_degrade_finding() -> None:
    """F1 — degrade finding(severity=info + 빈 evidence)은 evidence 요구를 면제한다."""
    degraded = AnalysisFinding(
        analysis_type="abuse",
        summary="데이터 확보 실패 — 타임아웃",
        evidence=[],
        severity="info",
    )
    reasons = run_finding_checks(degraded, [], expected_type="abuse")
    assert reasons == []


def test_f2_novel_number_not_in_tool_outputs_fails() -> None:
    """F2 — finding 의 수치가 도구 출력에 없으면(환각) 실패, 도구 출력 없이는 그 브랜치
    허용 집합이 비어 있어 유의 수치는 전부 근거 없음으로 판정된다."""
    finding = _finding()  # summary·evidence 에 42.1%·180,000·310,000 포함
    reasons = run_finding_checks(finding, [], expected_type="sales_anomaly")
    assert any("근거 없는 수치" in r for r in reasons)


def test_f2_cross_branch_contamination_is_rejected() -> None:
    """F2 — 다른 브랜치(A)의 도구 출력으로 이 finding(B)의 수치를 근거 삼을 수 없다
    (D2 의 전 finding 합집합과 달리, F2 는 '그 브랜치의 도구 출력'만 허용한다)."""
    finding = _finding()  # 180,000원/310,000원/42.1% 주장
    other_branch_outputs = ["전환율 12.3%, 조회수 5,000건"]  # 전혀 다른 수치
    reasons = run_finding_checks(finding, other_branch_outputs, expected_type="sales_anomaly")
    assert any("근거 없는 수치" in r for r in reasons)


def test_f2_small_numbers_are_tolerated() -> None:
    """F2 도 D2 와 동일한 유의숫자 완화(2자리 이하)를 공유한다(대칭 유지)."""
    finding = _finding(
        summary="최근 3일 연속 하락했다.",
        evidence=["1위 카테고리"],
    )
    assert run_finding_checks(finding, [], expected_type="sales_anomaly") == []


def test_f2_novel_number_in_recommendation_only_fails() -> None:
    """F2 — summary/evidence 는 도구 출력에 근거해도 recommendation 에만 지어낸
    수치가 있으면 실패한다(PR 리뷰 반영).

    D2·G1 은 recommendation 발 수치를 "검증된 근거"로 인정해 보고서·차트 인용을
    허용한다 — F2 가 recommendation 을 검사하지 않으면 워커가 recommendation 에
    지어낸 숫자로 F2 를 그대로 우회하고, 그 숫자가 D2/G1 단계에서 오히려 정당한
    근거로 취급돼 근거 사슬(도구출력⊇finding⊇보고서⊇차트)이 끊긴다.
    """
    tool_outputs = ["06-12 매출 180,000원 (직전 7일 평균 310,000원)"]
    finding = _finding(
        summary="6월 12일 매출이 직전 7일 평균 대비 42.1% 급락했다.",
        evidence=["06-12 매출 180,000원 (직전 7일 평균 310,000원)"],
        recommendation="999999원 규모의 프로모션을 즉시 집행 권장",
    )
    reasons = run_finding_checks(finding, tool_outputs, expected_type="sales_anomaly")
    assert any("근거 없는 수치" in r and "999999" in r for r in reasons)


def test_f2_sign_flipped_recommendation_fails() -> None:
    """[PR 리뷰 반영] 도구 출력이 양수로 기록한 수치를 finding 이 부호만 뒤집어
    인용하면(+12,000 → -12,000) 실패한다 — 부호 무시 비교로는 통과하던 버그.

    사용자 리포트: _format_chart_number(-12000.0) 은 "-12000" 을 만들지만 기존
    _normalize_numbers(무부호)는 "12000" 으로 정규화해 부호 반전 환각이 탐지되지
    않았다. F2 도 G1 과 같은 부호 보존 정규화를 쓰도록 고쳤다.
    """
    tool_outputs = ["06-12 매출 180,000원 (직전 7일 평균 310,000원, 42.1% 급락, 12,000원 증가)"]
    finding = _finding(recommendation="전월 대비 -12,000원 감소 추세이니 점검 권장")
    reasons = run_finding_checks(finding, tool_outputs, expected_type="sales_anomaly")
    assert any("근거 없는 수치" in r and "-12000" in r for r in reasons)


def test_f2_negative_number_grounded_when_sign_matches() -> None:
    """도구 출력·finding 양쪽에 동일 부호의 음수가 있으면 정상 통과한다(회귀 방지)."""
    tool_outputs = ["06-12 매출 180,000원 (직전 7일 평균 310,000원), 전월 대비 -12,000원 감소"]
    finding = _finding(summary="전월 대비 -12,000원 감소했다.")
    assert run_finding_checks(finding, tool_outputs, expected_type="sales_anomaly") == []


def test_f2_range_hyphen_not_confused_with_sign() -> None:
    """[PR 리뷰 반영] "300-400원" 같은 구간 표기의 하이픈은 직전이 숫자라 부호로
    오인하지 않는다 — 새 부호 인식 정규식이 이를 깨뜨리면 evidence 의 "-400"(오인)이
    도구 출력의 하이픈 없는 "400"과 어긋나 정상 finding 이 F2 를 통과하지 못한다
    (회귀 방지). 두 숫자를 도구 출력에 하이픈 없이 각각 명시해 evidence 쪽만
    구간 표기(하이픈)를 쓰는 비대칭 구성으로 오인 여부를 가른다."""
    tool_outputs = ["매출 300원, 400원 두 구간 모두 집계됐다"]
    finding = _finding(
        summary="매출이 300-400원 구간이었다.",
        evidence=["300-400원 구간"],
    )
    assert run_finding_checks(finding, tool_outputs, expected_type="sales_anomaly") == []


def test_f3_type_mismatch_fails() -> None:
    """F3 — finding.analysis_type 이 배정된 워커 유형과 다르면 실패."""
    finding = _finding(analysis_type="conversion")
    reasons = run_finding_checks(finding, [], expected_type="sales_anomaly")
    assert any("analysis_type 불일치" in r for r in reasons)
    assert any("sales_anomaly" in r and "conversion" in r for r in reasons)
