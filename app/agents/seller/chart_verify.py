"""차트 해석 검증 — `chart_grounded` (이슈 #600, 09-CHART.md §3).

🔴 신규 검사를 최소로 둔다(결정 87) — D1~D3(`verifier.DETERMINISTIC_CHECKS`)·C1
(`verifier.check_cause_hedged`)·V2-d(`verifier._check_period_grounded`)는 전부
**무접촉 재사용**이다. 이 모듈이 하는 일은 (1) 차트 좌표·chart_facts 를 D2 허용
집합에 편입시키는 합성 finding을 만들고 (2) `ChartSpec` 메타데이터가 필요해
기존 `(report, findings)` 레지스트리에 못 끼워 넣는 C4(`chart_claims_bounded`)를
새로 두는 것뿐이다(§3.7 — 레지스트리를 나누는 이유는 `06` §4.6과 같다: 인자
규약이 다르면 섞지 않는다).

`verifier.py`는 이 모듈이 한 줄도 고치지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from app.agents.seller.charts import ChartFacts
from app.agents.seller.pipeline import format_chart_facts, format_chart_points
from app.agents.seller.schemas import AnalysisFinding, ChartSet, ChartSpec
from app.agents.seller.verifier import (
    _check_period_grounded,  # [결정 89] 06 V2-d 를 그대로 재사용한다 — 의도적 내부 접근.
    check_cause_hedged,
    run_deterministic_checks,
)
from app.core.config import get_settings

# ── 합성 finding — D2(numbers_grounded) 허용 집합 편입 (결정 87) ────────────────────


def _synthesize_chart_finding(charts: ChartSet, facts: Sequence[ChartFacts]) -> AnalysisFinding:
    """차트 좌표·summary·chart_facts 를 D2 허용 집합에 편입시키는 합성 finding.

    `verifier.py` 는 한 줄도 고치지 않는다 — allowed 집합의 입력을 넓힐 뿐이다.
    severity="info" 지만 evidence 가 비지 않으므로 `_is_degrade_finding` 에 걸리지
    않는다(D3 오탐 없음). `analysis_type` 은 D1~D3·C1·V2-d 어느 것도 참조하지 않는
    필드라(F1~F3 전용, 여기선 호출되지 않는다) 임의값을 쓴다.
    """
    evidence = [
        *format_chart_points(charts),
        *format_chart_facts(charts, facts),
        *(spec.summary for spec in charts.charts if spec.summary),
    ]
    return AnalysisFinding(
        analysis_type="sales_anomaly",  # 임의값 — 위 docstring 참조, 어떤 체크도 참조하지 않는다
        summary="(차트 검증용 근거 블록 — 해석문에는 쓰이지 않는다)",
        evidence=evidence,
        severity="info",
    )


# ── C4 chart_claims_bounded — 금지 4종을 ChartSpec 메타데이터로 결정론 검사 (결정 90) ──

# x_axis="behavior_type" 조합은 CHART_SOURCES 에 단 하나(behavior_type×event_count)뿐이고
# 그 조립기(`charts._build_behavior_chart`)는 항상 이 4라벨 전량을 만든다(정본 표기
# 고정, removeFromCart 미포함) — ChartSpec 자체에는 x_axis 필드가 없어(스키마 §5단계
# 참조) 라벨 집합으로 그 차트인지 구조적으로 판별한다. plan.title 로 제목이 바뀌어도
# 라벨은 조립기가 고정하므로 흔들리지 않는다.
_BEHAVIOR_LABELS = frozenset({"조회", "장바구니", "결제시작", "구매"})


def _is_behavior_chart(spec: ChartSpec) -> bool:
    points = spec.series[0].points if spec.series else []
    return {p.x for p in points} == _BEHAVIOR_LABELS


def check_chart_claims_bounded(text: str, charts: ChartSet) -> list[str]:
    """C4 — §2.4 금지 6종 중 ②~⑤를 `ChartSpec` 메타데이터로 결정론 판정한다(결정 90).

    ①(채운 0을 사실로 말하기)은 검사 대상이 아니다 — "0"을 금지 어휘로 두면
    `zero_points` 정상 인용까지 막힌다(§3.5 마지막 항, 프롬프트 소관으로 남긴다).
    ⑥(y축 "%") 도 어휘만으로 결정론 판정하면 delta_pct 인용까지 막혀 여기서는 다루지
    않는다 — CHART_INTERPRET_PROMPT 의 발화 금지 절이 맡는다.
    """
    settings = get_settings()
    terms = settings.seller_chart_forbidden_terms
    reasons: list[str] = []

    # C4-a — 스냅샷(aggregate=="none") 차트만 있는 턴에 추세 어휘.
    if charts.charts and all(spec.aggregate == "none" for spec in charts.charts):
        hit = [term for term in terms.get("snapshot_trend", []) if term in text]
        if hit:
            reasons.append(
                "스냅샷(현재 시점) 차트에 추세 어휘를 썼다 — 기간 축이 없다: " + ", ".join(hit)
            )

    # C4-b — 버킷(3일·1주) 묶음 차트에 하루 단위 서술.
    if any(
        ("3일 단위" in (spec.summary or "")) or ("1주 단위" in (spec.summary or ""))
        for spec in charts.charts
    ):
        hit = [term for term in terms.get("daily_bucket", []) if term in text]
        if hit:
            reasons.append(
                "버킷으로 묶은 차트에 하루 단위 서술을 썼다 — x 라벨은 구간 시작일이다: "
                + ", ".join(hit)
            )

    # C4-c — 상위 N 절단 차트에서 하위 단정.
    if any("개만 표시" in (spec.summary or "") for spec in charts.charts):
        hit = [term for term in terms.get("bottom_rank", []) if term in text]
        if hit:
            reasons.append(
                "상위만 표시된(절단된) 차트에서 하위를 단정했다 — 하위는 화면에 없다: "
                + ", ".join(hit)
            )

    # C4-d — 행동 유형별(4종) 차트를 "전체 행동"으로 서술.
    if any(_is_behavior_chart(spec) for spec in charts.charts):
        hit = [term for term in terms.get("behavior_all", []) if term in text]
        if hit:
            reasons.append(
                "행동 유형별 차트를 '전체 행동'으로 서술했다 — 담기 취소는 이 차트에 없다: "
                + ", ".join(hit)
            )

    return reasons


# ── 인과 어휘 L0 보강 — 차트 레인은 완화어(hedge) 예외를 두지 않는다 ────────────────

# ⚠️ 실측(verifier.py, 2026-08-11) — check_cause_hedged(C1)는 인과 단정 어휘가 있어도
# hedge_terms 가 같은 텍스트에 있으면 "원인 후보:" evidence 존재 여부를 확인하지 않고
# 곧바로 통과시킨다(완화어 분기가 원인 후보 검사보다 먼저 return 한다). 09-CHART.md
# §2.5 는 "L2 어휘가 있는데 correlated 후보가 0건이면 실패"할 것으로 서술했으나 실제
# C1 구현은 그 분기를 타지 않는다 — 즉 C1 단독 재사용만으로는 "완화된 인과 표현"이
# 근거 없이도 통과한다. 차트 레인엔 원인 후보 생성기 자체가 없어(§2.5) 완화어를
# 붙여도 근거가 생기지 않으므로, 이 보강 검사로 인과 단정 어휘를 완화어 유무와 무관
# 하게 전부 막는다(결정 86 "L0 — 한 등급도 허용하지 않는다"를 실제로 성립시킨다).
# C1 은 그대로 병행 호출한다(§3.7 레지스트리 표에 있는 그대로) — 겹치는 경우 사유가
# 중복될 수 있으나 무해하고, verifier.py 는 이 결정과 무관하게 무접촉으로 남는다.


def check_chart_causal_free(text: str) -> list[str]:
    """차트 레인 인과 어휘 L0 보강 — 완화어(hedge)가 있어도 예외를 두지 않는다.

    위 모듈 절 설명 참조 — C1(`check_cause_hedged`)의 완화어 예외 분기가 차트
    레인에는 맞지 않아 별도로 전면 차단한다.
    """
    settings = get_settings()
    hit = [term for term in settings.seller_report_causal_terms if term in text]
    if hit:
        return [
            "차트 해석에는 인과 단정·완화 표현을 전혀 쓸 수 없다(L0) — 인과 어휘 발견: "
            + ", ".join(hit)
        ]
    return []


# ── 기간 인용 — V2-d 를 차트 허용 집합으로 재사용 (결정 89) ─────────────────────────

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _chart_citable_dates(charts: ChartSet, chart_from: date, chart_to: date) -> frozenset[date]:
    """period_grounded(V2-d 재사용)의 허용 집합 — chart_from/to + summary 안의 ISO 날짜.

    ⚠️ 한계 — 재사용 원본(`verifier._check_period_grounded`)의 추출 정규식은
    `YYYY-MM-DD` 형태만 본다. §3.4 가 서술한 "M월 D일"·"MM-DD" 자유 표기·x 라벨까지의
    확장 추출은 원본 함수를 고쳐야 하는데, 이 이슈는 `verifier.py` 무접촉을 결정
    89로 못 박았다 — 함수 자체를 그대로 재사용하는 대가로 감수하는 한계다(§11 성격의
    한계와 동일).
    """
    dates = {chart_from, chart_to}
    for spec in charts.charts:
        for token in _ISO_DATE_RE.findall(spec.summary or ""):
            try:
                dates.add(date.fromisoformat(token))
            except ValueError:
                continue
    return frozenset(dates)


# ── 진입점 ────────────────────────────────────────────────────────────────────


def run_chart_verification(
    text: str,
    charts: ChartSet,
    facts: Sequence[ChartFacts],
    *,
    chart_from: date,
    chart_to: date,
) -> list[str]:
    """차트 해석문 검증 — D1~D3(무접촉) + C1(무접촉) + 인과 L0 보강 + C4(신설) + period_grounded.

    `run_chart_interpret`(orchestrator.py)의 재작성 루프(1회, judge 없음, 결정 91)가
    호출한다 — 반환된 사유 목록이 비어 있으면 통과다.
    """
    finding = _synthesize_chart_finding(charts, facts)
    findings = [finding]

    reasons = run_deterministic_checks(text, findings)  # D1~D3 — verifier.py 무접촉
    reasons.extend(check_cause_hedged(text, findings))  # C1 — verifier.py 무접촉
    reasons.extend(check_chart_causal_free(text))  # L0 보강 — 위 절 설명 참조
    reasons.extend(check_chart_claims_bounded(text, charts))  # C4 — 이 모듈 신설
    citable = _chart_citable_dates(charts, chart_from, chart_to)
    reasons.extend(_check_period_grounded(text, findings, citable_dates=citable))  # V2-d 재사용
    return reasons
