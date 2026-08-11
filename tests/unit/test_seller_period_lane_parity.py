"""general·분석 두 레인의 기간 해석 대조 (이슈 #346 — #269 P2 "레인 통일").

이 파일 하나가 이슈의 완료 조건 ①②를 지킨다.

- ① **같은 기간 표현이 두 레인에서 같은 (from, to) 로 환산된다.** 종전에는 general
  레인의 환산이 `GENERAL_PROMPT_TEMPLATE` 의 산문에만 있어 `period.py` 와 갈라졌고
  (`이번 달` = 당월 1일~오늘 vs 당월 1일~어제), 어긋난 사실을 아무도 재지 않았다.
- ② **general 레인에도 상한·0/음수 가드가 걸린다.** 프롬프트 환산은 `period.py` 를
  타지 않아 `seller_period_max_days` 가 통째로 비켜갔다.

두 레인의 **진입 함수가 다르다**는 것이 이 대조의 핵심이다 —
분석은 `pipeline.resolve_plan`(planner 가 옮겨적은 period_expr), general 은
`period.resolve_from_message`(코드가 자유 발화에서 추출). 어휘표는 하나지만 도달
경로가 둘이라, 한쪽만 고치는 회귀는 이 파일에서만 잡힌다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agents.seller import period, pipeline
from app.agents.seller.schemas import AnalysisPlan

# test_seller_period.py 와 같은 기준일 — 상반기는 지났고(절단 없음) 4분기는 아직
# 오지 않아(R3), 절단·거절을 한 기준일로 함께 볼 수 있다.
TODAY = dt.date(2026, 8, 6)

_RECENT_DEFAULT_DAYS = 7
_MAX_DAYS = 731

# (planner 가 적는 period_expr, 판매자가 general 레인에 실제로 치는 발화)
# 왼쪽은 DESIGN-SELLER-PERIOD §2 어휘표 전 항목이다 — 항목이 늘면 여기도 늘려야 하고,
# 안 늘리면 "분석 레인만 아는 어휘"가 다시 생긴다.
_VOCAB_PAIRS = [
    # §2.1 확인 없이 통과
    ("지난달", "지난달 매출 알려줘"),
    ("어제", "어제 주문 몇 건이야?"),
    ("최근 7일", "최근 7일 매출 보여줘"),
    ("2026-06-01~2026-06-30", "2026-06-01~2026-06-30 매출 얼마야?"),
    # §2.2 확인 후 통과
    ("이번 달", "이번 달 매출 얼마야?"),
    ("올해", "올해 매출 얼마야?"),
    ("상반기", "상반기 매출 정리해줘"),
    ("하반기", "하반기 매출 정리해줘"),
    ("3분기", "3분기 매출 얼마야?"),
    ("최근 2주", "최근 2주 매출 보여줘"),
    ("최근 3개월", "최근 3개월 매출 보여줘"),
    ("6월 1일~6월 30일", "6월 1일~6월 30일 매출 얼마야?"),
]

# 두 레인 모두 되묻어야 하는 표현. general 은 발화에서 이 조각을 **잡아내야** 되묻기가
# 되고, 못 잡으면 "기간 언급 없음"으로 읽혀 기본 7일로 조용히 답한다(#269 침묵 대체).
_ASK_BACK_PAIRS = [
    ("오늘", "오늘 매출 얼마야?"),
    ("이번 주", "이번 주 매출 얼마야?"),
    ("작년", "작년 매출 얼마야?"),
    ("7월", "7월 매출 얼마야?"),
    ("최근 한 달", "최근 한 달 매출 얼마야?"),
    ("최근 999999일", "최근 999999일 매출 얼마야?"),
    ("최근 0일", "최근 0일 매출 얼마야?"),
]


def _analysis_lane(period_expr: str, comparison_expr: str = "") -> pipeline.ResolvedPlan:
    """분석 레인 경로 — planner 산출(AnalysisPlan) → resolve_plan."""
    plan = AnalysisPlan(
        analyses=["sales_anomaly"],
        period_expr=period_expr,
        comparison_expr=comparison_expr,
        reason="대조 테스트",
    )
    return pipeline.resolve_plan(
        plan,
        today=TODAY,
        recent_default_days=_RECENT_DEFAULT_DAYS,
        max_days=_MAX_DAYS,
    )


def _general_lane(message: str) -> period.PeriodResolution:
    """general 레인 경로 — 자유 발화 → resolve_from_message."""
    return period.resolve_from_message(
        message,
        today=TODAY,
        recent_default_days=_RECENT_DEFAULT_DAYS,
        max_days=_MAX_DAYS,
    )


@pytest.mark.parametrize(("period_expr", "message"), _VOCAB_PAIRS)
def test_both_lanes_resolve_to_same_range(period_expr: str, message: str) -> None:
    """[완료 조건 ①] 같은 기간 표현 → 같은 (from, to)."""
    analysis = _analysis_lane(period_expr)
    general = _general_lane(message)

    assert (general.date_from, general.date_to) == (analysis.date_from, analysis.date_to)


@pytest.mark.parametrize(("period_expr", "message"), _VOCAB_PAIRS)
def test_both_lanes_agree_on_supplement_flag(period_expr: str, message: str) -> None:
    """코드가 값을 보충했는가 — 그 판정도 두 레인이 같아야 한다.

    값이 같아도 이 판정이 갈리면 한쪽 레인만 고지 없이 지나간다 — 정합이
    (from, to) 에서만 성립하고 판매자가 보는 화면에서는 깨진다.
    """
    assert (
        _general_lane(message).needs_confirmation
        == _analysis_lane(period_expr).period_supplemented
    )


@pytest.mark.parametrize(("period_expr", "message"), _VOCAB_PAIRS)
def test_general_scanner_covers_every_vocabulary_entry(period_expr: str, message: str) -> None:
    """어휘표 전 항목이 자유 발화 스캐너에도 걸린다.

    스캐너 패턴은 어휘표의 **두 번째 사본**이다(전체 매칭 ^…$ 을 문장 속에서 쓸 수 없어
    불가피하다). 사본이 갈라지는 순간 "분석 레인은 아는데 general 은 못 잡는" 어휘가
    생기므로, 여기서 두 벌을 묶어 둔다.
    """
    assert period.find_period_mentions(message) == [_general_lane(message).expr]


@pytest.mark.parametrize(("period_expr", "message"), _ASK_BACK_PAIRS)
def test_both_lanes_ask_back_with_identical_wording(period_expr: str, message: str) -> None:
    """[완료 조건 ②] 되묻기 여부도 **문구까지** 같다.

    문구를 대조하는 이유: 기간 문구의 소유자는 period.py 하나라는 것이 #345 가 구조로
    세운 보장이다(DESIGN §4.2). general 레인이 자기 문구를 만들기 시작하면 그 보장이
    조용히 무너지고, 같은 질문에 레인마다 다른 안내가 나간다.
    """
    with pytest.raises(ValueError) as analysis_error:
        _analysis_lane(period_expr)
    with pytest.raises(ValueError) as general_error:
        _general_lane(message)

    assert str(general_error.value) == str(analysis_error.value)


# ── 비교(기준) 기간 대조 (#346 완료 조건 ③) ────────────────────────────────────

# (period_expr, comparison_expr, 판매자가 general 레인에 치는 발화)
_COMPARISON_TRIPLES = [
    ("이번 달", "지난달 대비", "지난달 대비 이번 달 매출 어때"),
    ("최근 7일", "직전 동일 기간", "최근 7일 매출을 직전 동일 기간과 비교해줘"),
    ("지난달", "작년 대비", "지난달 매출 작년 대비 어때"),
    ("최근 30일", "전월 동기간", "전월 동기간 대비 최근 30일 매출"),
]


@pytest.mark.parametrize(("period_expr", "comparison_expr", "message"), _COMPARISON_TRIPLES)
def test_both_lanes_resolve_the_same_comparison_range(
    period_expr: str, comparison_expr: str, message: str
) -> None:
    """[완료 조건 ③] 비교 기간 표현도 두 레인에서 같은 (from, to) 로 해석된다.

    도달 경로가 다르다 — 분석 레인은 planner 가 `comparison_expr` 필드를 따로 채우고,
    general 레인은 코드가 한 발화에서 비교 표현을 떼어낸다. 그 둘이 같은 구간에
    도달하는지는 여기서만 재진다.
    """
    analysis = _analysis_lane(period_expr, comparison_expr)
    general = _general_lane(message)

    assert general.comparison is not None
    assert (general.comparison.date_from, general.comparison.date_to) == (
        analysis.compare_from,
        analysis.compare_to,
    )


@pytest.mark.parametrize(("period_expr", "comparison_expr", "message"), _COMPARISON_TRIPLES)
def test_both_lanes_keep_the_base_period_intact_with_comparison(
    period_expr: str, comparison_expr: str, message: str
) -> None:
    """비교 표현이 붙어도 본 기간 해석은 흔들리지 않는다.

    general 레인은 비교 표현을 먼저 떼어낸 뒤 본 기간을 찾는다 — 이 순서가 뒤집히면
    "지난달 대비 이번 달" 의 '지난달' 이 본 기간으로 잡혀 두 레인이 갈라진다.
    """
    analysis = _analysis_lane(period_expr, comparison_expr)
    general = _general_lane(message)

    assert (general.date_from, general.date_to) == (analysis.date_from, analysis.date_to)


@pytest.mark.parametrize(("period_expr", "comparison_expr", "message"), _COMPARISON_TRIPLES)
def test_both_lanes_agree_on_supplement_flag_with_comparison(
    period_expr: str, comparison_expr: str, message: str
) -> None:
    """고지 판정도 **합집합**으로 같다 — 비교 기간만 보충된 경우를 놓치지 않는다."""
    analysis = _analysis_lane(period_expr, comparison_expr)
    general = _general_lane(message)

    assert general.any_confirmation_needed == analysis.period_supplemented
