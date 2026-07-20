"""보고서 결정론 검증 (SPEC-SELLER-001 §10-⑦ 전반부 — LLM judge 이전의 코드 검사).

체크는 DETERMINISTIC_CHECKS 레지스트리로 관리한다(2026-07-18 확정 — 추후
추가/제거/조정 용이). 각 체크는 (report, findings) -> list[str] 시그니처로,
실패 사유 목록을 반환한다(빈 리스트 = 통과). 실패 사유는 judge 의 feedback 과
함께 report_agent 재작성 지시 재료가 된다.

21/30 판정·≤3회 루프 배선은 3단계 소관 — 이 모듈은 순수 함수만 둔다(LLM·IO 없음).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.agents.seller.schemas import AnalysisFinding

_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")

# D2 과탐 완화(2026-07-18 확정, 추후 조정 가능): 정규화 후 2자리 이하 숫자는
# 서술 관용("3일 연속", "1위")으로 흔해 근거 대조에서 제외한다.
_MIN_SIGNIFICANT_DIGITS = 3

# R1(3-4 반영): 연도 계열 날짜 패턴은 수치가 아니라 표기 — 숫자 추출 전에 마스킹한다.
# 월·일("06-12"·"7일")은 2자리라 _MIN_SIGNIFICANT_DIGITS 완화가 이미 흡수하므로
# 과잉 마스킹(실제 환각 은폐)을 피해 4자리 연도 계열만 다룬다. 패턴 추가/조정은 여기만.
_DATE_MASK_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),  # 2026-07-18
    re.compile(r"\d{4}-\d{1,2}(?!\d)"),  # 2026-07 (연-월)
    re.compile(r"\d{4}\s*년"),  # 2026년
)


def _mask_dates(text: str) -> str:
    """연도 계열 날짜 표기를 제거한다 — D2 가 날짜를 근거 없는 수치로 오탐하지 않도록."""
    for pattern in _DATE_MASK_RES:
        text = pattern.sub(" ", text)
    return text


def _normalize_numbers(text: str) -> set[str]:
    """텍스트의 숫자 토큰을 정규화(날짜 마스킹·쉼표 제거·후행 소수점 정리)해 집합으로 반환한다.

    report·findings 양쪽에 동일하게 적용된다(대칭 — 한쪽만 마스킹하면 드리프트).
    """
    out: set[str] = set()
    for token in _NUMBER_RE.findall(_mask_dates(text)):
        normalized = token.replace(",", "").rstrip(".")
        if normalized:
            out.add(normalized)
    return out


def check_not_empty(report: str, findings: list[AnalysisFinding]) -> list[str]:
    """D1 — 빈/백지 보고서."""
    if not report.strip():
        return ["보고서가 비어 있다"]
    return []


def check_numbers_grounded(report: str, findings: list[AnalysisFinding]) -> list[str]:
    """D2 — 수치 정합(환각 탐지): 보고서 숫자는 finding 텍스트의 부분집합이어야 한다."""
    allowed: set[str] = set()
    for finding in findings:
        allowed |= _normalize_numbers(finding.summary)
        allowed |= _normalize_numbers(finding.recommendation)
        for item in finding.evidence:
            allowed |= _normalize_numbers(item)
    novel = {
        n
        for n in _normalize_numbers(report)
        if n not in allowed and len(n.replace(".", "")) >= _MIN_SIGNIFICANT_DIGITS
    }
    if novel:
        return [
            "근거 없는 수치 "
            + ", ".join(sorted(novel))
            + " — finding 의 summary/evidence 에 없는 숫자를 인용했다"
        ]
    return []


def _is_degrade_finding(finding: AnalysisFinding) -> bool:
    """degrade finding 판정 — R2(3-4 반영): 문자열이 아니라 구조 조합으로 본다.

    severity=info + 빈 evidence 조합이 degrade 규약(§4·WORKER_COMMON_RULES)의 구조다.
    트레이드오프(2026-07-18 사용자 위임 결정): "이상 없음"인데 evidence 를 비운 정상
    finding 을 degrade 로 오탐할 수 있으나(보고서에 한계 한 줄 요구 — 사족 수준),
    문자열 의존은 워커 표현 변화 시 은폐를 통과시킨다(미탐 — 신뢰 훼손). 오탐을
    감수하고 미탐을 막는 쪽을 택했다. 판정 변경은 이 함수만 고치면 된다.
    """
    return finding.severity == "info" and not finding.evidence


def check_degrade_disclosed(report: str, findings: list[AnalysisFinding]) -> list[str]:
    """D3 — degrade 정직성: 확보 실패 finding 이 있으면 보고서가 한계를 명시해야 한다."""
    has_degrade = any(_is_degrade_finding(f) for f in findings)
    if has_degrade and "확보 실패" not in report and "데이터 한계" not in report:
        return ["데이터 확보 실패 finding 이 있으나 보고서에 그 한계가 명시되지 않았다"]
    return []


CheckFn = Callable[[str, list[AnalysisFinding]], list[str]]

# 체크 레지스트리 — 항목 추가/제거로 검사 구성을 바꾼다(이름은 로그·디버깅용).
DETERMINISTIC_CHECKS: list[tuple[str, CheckFn]] = [
    ("not_empty", check_not_empty),
    ("numbers_grounded", check_numbers_grounded),
    ("degrade_disclosed", check_degrade_disclosed),
]


def run_deterministic_checks(report: str, findings: list[AnalysisFinding]) -> list[str]:
    """등록된 결정론 검사를 전부 실행해 실패 사유를 모아 반환한다(빈 리스트 = 통과)."""
    reasons: list[str] = []
    for _name, check in DETERMINISTIC_CHECKS:
        reasons.extend(check(report, findings))
    return reasons
