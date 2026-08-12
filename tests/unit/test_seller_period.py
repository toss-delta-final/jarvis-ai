"""app/agents/seller/period.py — 기간 어휘 판정·환산·확인 문구 (이슈 #345, #269 P1).

두 층을 검증한다.
1. **#269 P0 회귀 가드** (구 test_seller_calc.py 에서 이관) — 침묵 폴백 금지, 상한,
   자릿수, OverflowError 미유출, 되묻기 문구가 사용자 대면 문장일 것.
2. **#345 P1** — 어휘 확장(확인 후 통과), 경계 규칙 R1~R5, 확인 문구, 승인 판정.

전부 stdlib 만으로 실행 가능(결정론) — today 를 주입해 실행 시각에 의존하지 않는다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agents.seller import period

# 이 모듈 전반의 기준일. 2026-08-06(목) → 어제 = 2026-08-05.
# 8월을 고른 이유: 상반기는 이미 지났고(절단 없음), 4분기는 아직 오지 않아(R3)
# 절단·거절 규칙을 한 기준일로 같이 볼 수 있다.
TODAY = dt.date(2026, 8, 6)
YESTERDAY = dt.date(2026, 8, 5)

_KWARGS = {"today": TODAY, "recent_default_days": 7, "max_days": 731}


def _resolve(expr: str, **overrides: object) -> period.PeriodResolution:
    kwargs = {**_KWARGS, **overrides}
    return period.resolve_period(expr, **kwargs)  # type: ignore[arg-type]


# ── 1. 확인 없이 통과하는 기존 어휘 5종 (회귀 가드 — #345 완료 조건) ─────────────


def test_canonical_vocab_regression_guard() -> None:
    """기존 어휘 5종의 환산 결과와 needs_confirmation=False 는 #269 전후로 동일하다.

    이 테스트가 깨지면 기존 판매자가 잘 쓰던 경로를 건드린 것이다 — 값이 바뀌었거나,
    없던 확인 왕복을 새로 물렸거나 둘 중 하나다.
    """
    expected = {
        "지난달": (dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        "최근 7일": (dt.date(2026, 7, 30), YESTERDAY),
        "최근": (dt.date(2026, 7, 30), YESTERDAY),  # 기본 7일
        "어제": (YESTERDAY, YESTERDAY),
        "2026-06-01~2026-06-30": (dt.date(2026, 6, 1), dt.date(2026, 6, 30)),
    }
    for expr, period_range in expected.items():
        resolution = _resolve(expr)
        assert resolution.period == period_range, expr
        assert resolution.needs_confirmation is False, expr
        assert resolution.clipped is False, expr


def test_last_month_year_rollover() -> None:
    """1월 today → 전년 12/1~12/31 로 롤오버한다."""
    resolution = _resolve("지난달", today=dt.date(2026, 1, 15))
    assert resolution.period == (dt.date(2025, 12, 1), dt.date(2025, 12, 31))


def test_recent_n_excludes_today() -> None:
    """ "최근 N일"·"최근" 은 오늘을 포함하지 않는다(R1 — 당일 집계 미완결)."""
    assert _resolve("최근 7일", today=dt.date(2026, 7, 17)).period == (
        dt.date(2026, 7, 10),
        dt.date(2026, 7, 16),
    )
    assert _resolve("최근", today=dt.date(2026, 7, 17), recent_default_days=3).period == (
        dt.date(2026, 7, 14),
        dt.date(2026, 7, 16),
    )


def test_explicit_range_accepts_spacing() -> None:
    """명시 범위는 물결 앞뒤 공백을 허용한다(3-1 확장)."""
    assert _resolve("2026-06-01 ~ 2026-06-15").period == _resolve("2026-06-01~2026-06-15").period


def test_explicit_range_rejects_invalid() -> None:
    """명시 범위의 역전(from>to)·달력에 없는 날짜는 ValueError(되묻기 경로)."""
    with pytest.raises(ValueError):
        _resolve("2026-06-15~2026-06-01")
    with pytest.raises(ValueError):
        _resolve("2026-02-30~2026-03-01")


def test_explicit_future_range_is_not_clipped() -> None:
    """명시 범위는 판매자가 직접 지정한 값이라 코드가 말없이 자르지 않는다(DESIGN §3 단서).

    확인 대상 어휘에만 R2(미래 절단)를 적용한다 — 기존 P0 동작을 유지하는 지점이다.
    """
    resolution = _resolve("2027-01-01~2027-01-31")
    assert resolution.period == (dt.date(2027, 1, 1), dt.date(2027, 1, 31))
    assert resolution.clipped is False


def test_fullwidth_digits_are_normalized() -> None:
    """전각 숫자("최근 ７일")는 NFKC 정규화로 반각과 같게 해석한다(#269)."""
    assert _resolve("최근 ７일").period == _resolve("최근 7일").period


# ── 2. #269 P0 회귀 가드 (침묵 폴백·상한·자릿수·예외 종류) ───────────────────────

# 종전 구조(`"최근" in text` 부분 일치 + 정규식 실패 시 기본값)에서 **전부 조용히
# 7일**로 통과하던 표현들. P1 에서 주·개월은 지원 어휘가 됐으므로, 여기 남는 것은
# 여전히 되묻어야 하는 것들이다.
_STILL_REASK_EXPRS = (
    "최근 한 달",  # 한글 수사 — 열면 "두어 달"·"서너 주"까지 경계가 흐려진다
    "최근 반년",
    "최근 -3일",
    "최근 0일",
    "최근에",
    "최근 며칠",
    "이번 달 들어 최근 7일",  # 지원·미지원 혼합
    "작년 여름",
)


@pytest.mark.parametrize("expr", _STILL_REASK_EXPRS)
def test_no_silent_default_fallback(expr: str) -> None:
    """인식하지 못한 표현은 기본 일수로 떨어지지 않고 되묻기로 간다(#269 P0 계약)."""
    with pytest.raises(ValueError):
        _resolve(expr)


def test_upper_bound_raises_value_error() -> None:
    """ "최근 999999일" 은 OverflowError 가 아니라 ValueError 다(#269).

    호출부(resolve_plan → orchestrator)는 except ValueError 만 잡으므로, OverflowError
    면 되묻기가 아니라 파이프라인 예외로 전파돼 사과/error 경로로 샌다.
    """
    with pytest.raises(ValueError):
        _resolve("최근 999999일")
    # 상한 이내는 정상 통과 — 가드가 정상 범위를 막지 않는다.
    resolution = _resolve("최근 731일")
    assert (resolution.date_to - resolution.date_from).days + 1 == 731


def test_never_raises_overflow_error() -> None:
    """max_days 를 크게 넘겨도 OverflowError 가 밖으로 나가지 않는다(#269 리뷰).

    약 74만일부터 date 연산이 date.min 을 넘는다 — 설정 검증과 별개로 이 모듈 자체가
    "예외는 ValueError 뿐" 을 보장해야 한다.
    """
    with pytest.raises(ValueError):
        _resolve("최근 800000일", max_days=999_999_999)


def test_huge_digit_count_is_wrapped() -> None:
    """자릿수가 터무니없이 많아도 Python 내부 예외 메시지가 새지 않는다(#269 리뷰).

    Python 3.11+ 는 4300자리 초과 문자열→int 변환에서 영어 메시지 ValueError 를 낸다.
    그 메시지는 되묻기 문구로 판매자에게 그대로 노출된다.
    """
    with pytest.raises(ValueError) as exc:
        _resolve("최근 " + "9" * 4301 + "일")
    message = str(exc.value)
    assert "Exceeds the limit" not in message
    assert "integer string conversion" not in message
    assert "기간이 너무 깁니다" in message


def test_zero_and_negative_days_are_rejected() -> None:
    """N<=0 은 역전 범위(from>to)가 되므로 되묻기다(#269 마감 리뷰 M3)."""
    for expr in ("최근 0일", "최근 -3일", "최근 0주", "최근 0개월"):
        with pytest.raises(ValueError):
            _resolve(expr)
    # 설정 오류 방어 — recent_default_days 가 0 이어도 침묵 통과하지 않는다.
    with pytest.raises(ValueError):
        _resolve("최근", recent_default_days=0)


# 단위 글자가 **다른 단어 안에 우연히 포함**된 경우 — 판매자는 단위를 쓴 적이 없다.
# 부분 문자열 검사면 여기까지 단위 안내가 나가 되묻기 이유를 잘못 짚는다(#269 리뷰).
_INCIDENTAL_UNIT_EXPRS = ("최근 목표 달성 현황", "최근 주말 프로모션", "최근 분기점 지표")


@pytest.mark.parametrize("expr", _INCIDENTAL_UNIT_EXPRS)
def test_incidental_unit_char_gets_generic_guidance(expr: str) -> None:
    """단위 글자가 다른 단어에 섞였을 뿐이면 단위 안내가 아니라 지원 어휘 안내다.

    되묻기라는 결론이 같아도 이유가 틀리면 되묻기 대화가 더 꼬인다 — #269 의 목적이
    "판매자가 왜 되물어지는지 알게 하는 것" 이므로 이유를 정확히 짚어야 한다.
    """
    with pytest.raises(ValueError) as exc:
        _resolve(expr)
    message = str(exc.value)
    assert "지원하지 않는 기간 단위" not in message
    assert "지난달" in message  # 지원 어휘 안내


def test_error_message_is_user_facing() -> None:
    """되묻기 메시지는 판매자에게 그대로 노출된다 — 개발자 문자열을 쓰지 않는다(#269).

    resolve_plan 이 이 ValueError 메시지를 PipelineResult(kind="clarification").text 로
    그대로 흘리므로, 메시지 자체가 사용자 대면 문구여야 한다.
    """
    with pytest.raises(ValueError) as exc:
        _resolve("작년 여름")
    message = str(exc.value)
    assert "파싱" not in message
    assert "period_expr" not in message
    assert "지난달" in message


def test_long_input_is_truncated_in_message() -> None:
    """장문·개행이 되묻기 문구로 그대로 반사되지 않는다."""
    with pytest.raises(ValueError) as exc:
        _resolve("가" * 200 + "\n" + "나" * 200)
    assert "…" in str(exc.value)
    assert "\n" not in str(exc.value).split("기간을 이해하지 못했습니다")[0]


# ── 3. #345 P1 — 확인 후 통과하는 신규 어휘 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("이번 달", (dt.date(2026, 8, 1), YESTERDAY)),
        ("이번달", (dt.date(2026, 8, 1), YESTERDAY)),
        ("올해", (dt.date(2026, 1, 1), YESTERDAY)),
        ("상반기", (dt.date(2026, 1, 1), dt.date(2026, 6, 30))),
        ("2분기", (dt.date(2026, 4, 1), dt.date(2026, 6, 30))),
        ("최근 2주", (dt.date(2026, 7, 23), YESTERDAY)),
        ("최근 3개월", (dt.date(2026, 5, 6), YESTERDAY)),
        ("6월 1일~6월 30일", (dt.date(2026, 6, 1), dt.date(2026, 6, 30))),
    ],
)
def test_expanded_vocab_resolves_with_confirmation(
    expr: str, expected: tuple[dt.date, dt.date]
) -> None:
    """신규 어휘는 값이 나오되 needs_confirmation=True 로 확인을 요구한다(#345 완료 조건)."""
    resolution = _resolve(expr)
    assert resolution.period == expected
    assert resolution.needs_confirmation is True


def test_bare_date_range_from_natural_phrasing() -> None:
    """판매자 원문("6월 1일부터 6월 30일까지")도 받는다 — planner 정규화와 같은 결과."""
    assert _resolve("6월 1일부터 6월 30일까지").period == _resolve("6월 1일~6월 30일").period


def test_recent_months_uses_calendar_months() -> None:
    """ "최근 N개월" 은 N×30일 근사가 아니라 달력 기준이다(DESIGN §2.4)."""
    assert _resolve("최근 1개월", today=dt.date(2026, 3, 31)).date_from == dt.date(2026, 2, 28)
    assert _resolve("최근 3개월", today=dt.date(2026, 1, 15)).date_from == dt.date(2025, 10, 15)


# ── 4. 경계 규칙 R2·R3·R5 ───────────────────────────────────────────────────────


def test_future_end_is_clipped_and_flagged() -> None:
    """R2 — 끝이 미래면 어제로 자르고, 잘랐다는 사실을 clipped 로 드러낸다.

    자르고 말하지 않으면 #269 가 없앤 "조용한 대체"가 형태만 바꿔 돌아온다.
    """
    resolution = _resolve("이번 달")
    assert resolution.date_to == YESTERDAY
    assert resolution.clipped is True
    # 이미 지나간 구간은 자를 것이 없다.
    assert _resolve("상반기").clipped is False


def test_fully_future_period_is_rejected() -> None:
    """R3 — 8월에 "4분기"(10~12월)는 자를 구간 자체가 없어 되묻는다.

    작년으로 미루지 않는다: 어휘가 "올해의"를 뜻하므로 다른 질문에 답하는 셈이 된다.
    """
    with pytest.raises(ValueError) as exc:
        _resolve("4분기")
    assert "아직 지나지 않은" in str(exc.value)


def test_partially_future_period_is_clipped_not_rejected() -> None:
    """R2 vs R3 의 경계 — 8월의 "하반기"(7~12월)는 7월이 이미 지나 절단 대상이다.

    시작이 지나갔으면 자르고(R2), 시작조차 오지 않았으면 되묻는다(R3). 이 경계를
    잘못 잡으면 "하반기 매출" 같은 정상 질문이 통째로 막힌다.
    """
    resolution = _resolve("하반기")
    assert resolution.period == (dt.date(2026, 7, 1), YESTERDAY)
    assert resolution.clipped is True
    assert resolution.needs_confirmation is True


def test_bare_date_range_falls_back_to_last_year() -> None:
    """R5 — 연도 없는 날짜가 올해 기준으로 미래면 작년으로 읽는다.

    R5 를 분기·상반기·올해에 적용하지 않는 것과 대비되는 지점이다(DESIGN §3) —
    맨 날짜는 통상 지나간 날을 가리킨다.
    """
    resolution = _resolve("12월 1일~12월 31일")
    assert resolution.period == (dt.date(2025, 12, 1), dt.date(2025, 12, 31))
    assert resolution.needs_confirmation is True


def test_expanded_vocab_still_obeys_max_days() -> None:
    """R4 — 신규 어휘도 상한을 넘으면 되묻기다(확인 대기로 새지 않는다)."""
    with pytest.raises(ValueError):
        _resolve("최근 24개월", max_days=30)


# ── 5. general 레인 진입점 (#346 — 자유 발화 스캔) ──────────────────────────────


def _from_message(message: str, **overrides: object) -> period.PeriodResolution:
    kwargs = {**_KWARGS, **overrides}
    return period.resolve_from_message(message, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("message", "expected_expr"),
    [
        ("이번 달 매출 얼마야?", "이번 달"),
        ("지난달 매출 알려줘", "지난달"),
        ("최근 7일 주문 보여줘", "최근 7일"),
        ("최근 3개월 매출 추이가 어때", "최근 3개월"),
        ("최근 2주 전환율", "최근 2주"),
        ("상반기 매출 정리해줘", "상반기"),
        ("2026-06-01~2026-06-30 매출", "2026-06-01~2026-06-30"),
        ("어제 주문 몇 건이야", "어제"),
        ("최근 리뷰 보여줘", "최근"),
    ],
)
def test_resolve_from_message_extracts_supported_vocab(message: str, expected_expr: str) -> None:
    """어휘표의 표현이 문장 속에 있어도 잡힌다 — resolve_period 는 전체 매칭이라 못 하는 일."""
    assert _from_message(message).expr == expected_expr


def test_resolve_from_message_defaults_to_recent_when_no_period() -> None:
    """기간 언급이 없으면 '최근' — planner 의 `[기간]` 절과 같은 규약이다.

    이 기본값이 planner 와 어긋나면 "기간을 말하지 않은 같은 질문"이 레인에 따라 다른
    기간을 쓰게 된다 — 이 이슈가 없애려는 바로 그 비대칭이다.
    """
    resolution = _from_message("재고 얼마 남았어?")
    assert resolution.expr == "최근"
    assert resolution.needs_confirmation is False
    assert resolution.period == (TODAY - dt.timedelta(days=7), YESTERDAY)


@pytest.mark.parametrize(
    "message",
    [
        "오늘 매출 얼마야?",
        "이번 주 매출 얼마야?",
        "작년 여름 매출 어땠어",
        "7월 매출 얼마야?",
        "2025년 매출 보여줘",
        "최근 한 달 매출",
    ],
)
def test_resolve_from_message_asks_back_on_unsupported_period(message: str) -> None:
    """기간처럼 보이지만 어휘표 밖이면 되묻는다 — **조용히 기본 7일로 답하지 않는다**.

    스캐너가 이 표현들을 일부러 잡는 이유가 여기 있다. 안 잡으면 "기간 언급 없음"으로
    읽혀 기본값이 적용되고, 판매자는 자기가 물은 기간과 다른 답을 받고도 알 방법이 없다
    (#269 가 분석 레인에서 없앤 침묵 대체가 general 레인에서 재현되는 경로).
    """
    with pytest.raises(ValueError):
        _from_message(message)


def test_resolve_from_message_asks_back_on_mixed_expressions() -> None:
    """지원·미지원이 섞이거나 표현이 둘이면 되묻는다(DESIGN §2.3 혼합 표현)."""
    with pytest.raises(ValueError, match="여러 개"):
        _from_message("이번 달 들어 최근 7일 매출")


def test_resolve_from_message_applies_upper_bound_guard() -> None:
    """[#346 완료 조건] general 레인에도 상한·0/음수 가드가 걸린다.

    종전에는 이 환산이 프롬프트에만 있어 seller_period_max_days 가 전혀 걸리지 않았다.
    """
    with pytest.raises(ValueError, match="731일 이내"):
        _from_message("최근 999999일 매출")
    with pytest.raises(ValueError, match="1 이상"):
        _from_message("최근 0일 매출")


def test_find_period_mentions_ignores_period_free_questions() -> None:
    """기간이 없는 질문에서 표현을 지어내지 않는다(오탐 시 엉뚱한 되묻기가 된다)."""
    assert period.find_period_mentions("판매중인 상품 목록 보여줘") == []
    assert period.find_period_mentions("재고 얼마 남았어?") == []


def test_find_period_mentions_prefers_longest_vocab() -> None:
    """'최근 3개월'을 '최근'으로 잘라 먹지 않는다 — 자르면 3개월 질문이 7일로 답해진다."""
    assert period.find_period_mentions("최근 3개월 매출") == ["최근 3개월"]
    assert period.find_period_mentions("6월 1일~6월 30일 매출") == ["6월 1일~6월 30일"]


# ── 6. 해석 고지 문구 (#346 — general 레인) ─────────────────────────────────────


def test_disclosure_text_shows_dates_not_vocabulary() -> None:
    """고지는 어휘가 아니라 **환산된 날짜**를 보여준다 — 확인 문구와 같은 원칙(§4.3)."""
    resolution = _from_message("이번 달 매출")
    text = period.disclosure_text(resolution)

    assert resolution.date_from.isoformat() in text
    assert resolution.date_to.isoformat() in text
    assert "이번 달" in text


def test_disclosure_text_admits_clipping() -> None:
    """R2 로 잘렸으면 그 사실을 밝힌다 — 자르고 말하지 않으면 조용한 대체다."""
    resolution = _from_message("이번 달 매출")
    assert resolution.clipped is True
    assert "지나지 않은 날짜" in period.disclosure_text(resolution)


def test_disclosure_text_omits_clip_note_when_not_clipped() -> None:
    """절단이 없었으면 절단 문구도 없다 — 없는 사실을 알리지 않는다."""
    resolution = _from_message("상반기 매출")
    assert resolution.clipped is False
    assert "지나지 않은 날짜" not in period.disclosure_text(resolution)


# ── 7. 비교(기준) 기간 (#346 — DESIGN §2.5) ────────────────────────────────────


def _compare(expr: str, base_expr: str) -> period.PeriodResolution:
    return period.resolve_comparison(expr, _resolve(base_expr), max_days=731)


def test_previous_adjacent_period_needs_no_confirmation() -> None:
    """'직전 동일 기간'은 코드가 보충하는 값이 없다 — base 에서 길이·끝점이 다 나온다.

    이 정의는 get_funnel·get_churn_cohort 가 이미 내부적으로 쓰는 것과 같다
    (tools._previous_period) — 어휘로 노출하면서 정의가 갈라지면 같은 질문에 도구
    자동 비교와 판매자 지정 비교가 다른 구간을 보게 된다.
    """
    base = _resolve("최근 7일")  # 2026-07-30 ~ 2026-08-05
    comparison = _compare("직전 동일 기간", "최근 7일")

    assert comparison.needs_confirmation is False
    assert comparison.date_to == base.date_from - dt.timedelta(days=1)
    assert (comparison.date_to - comparison.date_from) == (base.date_to - base.date_from)


def test_previous_month_comparison_shifts_by_calendar() -> None:
    """'지난달 대비'는 달력 1달 시프트다 — 30일 근사가 아니다(§2.4 와 같은 이유)."""
    comparison = _compare("지난달 대비", "이번 달")  # base 2026-08-01~2026-08-05

    assert comparison.period == (dt.date(2026, 7, 1), dt.date(2026, 7, 5))
    assert comparison.needs_confirmation is True  # 정렬 방식을 코드가 골랐다


def test_previous_year_comparison_shifts_by_year() -> None:
    comparison = _compare("작년 대비", "지난달")  # base 2026-07-01~2026-07-31

    assert comparison.period == (dt.date(2025, 7, 1), dt.date(2025, 7, 31))
    assert comparison.needs_confirmation is True


def test_unknown_comparison_expression_asks_back() -> None:
    """비교 어휘도 어휘표 밖이면 되묻는다 — 코드가 대조군을 지어내지 않는다."""
    with pytest.raises(ValueError, match="비교 기간"):
        _compare("작년 여름 대비", "지난달")


def test_comparison_mention_is_split_before_base_extraction() -> None:
    """'지난달 대비' 의 '지난달' 이 본 기간으로 잡히면 비교 질문이 통째로 되묻기가 된다."""
    remainder, comparison = period.find_comparison_mention("지난달 대비 이번 달 매출")

    assert comparison == "지난달 대비"
    assert period.find_period_mentions(remainder) == ["이번 달"]


def test_resolve_from_message_fills_comparison() -> None:
    """general 레인도 한 발화에서 본 기간·비교 기간을 함께 뽑는다."""
    resolution = _from_message("지난달 대비 이번 달 매출 어때")

    assert resolution.expr == "이번 달"
    assert resolution.comparison is not None
    assert resolution.comparison.period == (dt.date(2026, 7, 1), dt.date(2026, 7, 5))


def test_any_confirmation_needed_covers_comparison_only_case() -> None:
    """본 기간이 명시적이어도 비교 기간이 보충됐으면 확인 대상이다.

    needs_confirmation 만 보면 이 경우가 조용히 지나간다 — 고지 없이 코드가 고른
    대조군으로 답하게 되고, 그것이 P0 가 없앤 조용한 대체다.
    """
    resolution = _from_message("2026-06-01~2026-06-30 매출 작년 대비 어때")

    assert resolution.needs_confirmation is False  # 명시 범위 — 보충 없음
    assert resolution.comparison is not None and resolution.comparison.needs_confirmation is True
    assert resolution.any_confirmation_needed is True


def test_disclosure_text_reveals_the_comparison_dates() -> None:
    """고지 문구가 비교 기간 날짜도 밝힌다 — 본 기간만 밝히면 고지가 절반이다."""
    resolution = _from_message("지난달 대비 이번 달 매출")

    text = period.disclosure_text(resolution)
    assert resolution.comparison is not None
    assert resolution.comparison.date_from.isoformat() in text
    assert resolution.comparison.date_to.isoformat() in text


# ── [#518] 버킷 분할 (I-31 리뷰 추이) ─────────────────────────────────────────


def _spans(from_: str, to: str, unit: str, max_buckets: int = 12) -> list[tuple[str, str]]:
    result = period.split_buckets(
        dt.date.fromisoformat(from_), dt.date.fromisoformat(to), unit, max_buckets=max_buckets
    )
    return [(start.isoformat(), end.isoformat()) for start, end in result]


def test_split_buckets_weekly_anchors_on_from_not_calendar_week() -> None:
    """주 버킷은 from 앵커 7일 고정창이다 — 달력 주면 첫 조각이 2~3일로 잘린다.

    2026-07-01 은 수요일이라 달력 주(월 시작) 규칙이면 첫 구간이 07-01~07-05(5일)로
    나온다. 그 구간만 건수가 작게 나와 판매자가 없는 급락을 읽는다.
    """
    assert _spans("2026-07-01", "2026-07-31", "weekly") == [
        ("2026-07-01", "2026-07-07"),
        ("2026-07-08", "2026-07-14"),
        ("2026-07-15", "2026-07-21"),
        ("2026-07-22", "2026-07-28"),
        ("2026-07-29", "2026-07-31"),
    ]


def test_split_buckets_monthly_uses_calendar_boundary_and_clips_ends() -> None:
    """월 버킷만 달력 경계다 — "7월/8월"이라는 판매자 어휘가 곧 경계이기 때문."""
    assert _spans("2026-07-15", "2026-09-03", "monthly") == [
        ("2026-07-15", "2026-07-31"),
        ("2026-08-01", "2026-08-31"),
        ("2026-09-01", "2026-09-03"),
    ]


def test_split_buckets_monthly_handles_leap_february() -> None:
    """윤년 2월은 29일까지다 — 30/31 고정이면 3월 1일이 2월 버킷에 섞인다."""
    assert _spans("2028-02-01", "2028-03-02", "monthly") == [
        ("2028-02-01", "2028-02-29"),
        ("2028-03-01", "2028-03-02"),
    ]


def test_split_buckets_never_exceeds_requested_end() -> None:
    """마지막 구간은 to 를 넘지 않는다 — 넘기면 기간 밖 리뷰가 함께 집계된다."""
    assert _spans("2026-07-01", "2026-07-03", "weekly") == [("2026-07-01", "2026-07-03")]
    assert _spans("2026-07-01", "2026-07-01", "daily") == [("2026-07-01", "2026-07-01")]


def test_split_buckets_covers_every_day_exactly_once() -> None:
    """분할은 기간을 빠짐없이·겹침없이 덮는다(세 단위 공통 불변식)."""
    for unit in period.BUCKET_UNITS:
        days: list[dt.date] = []
        for start, end in period.split_buckets(
            dt.date(2026, 7, 1), dt.date(2026, 9, 30), unit, max_buckets=200
        ):
            cursor = start
            while cursor <= end:
                days.append(cursor)
                cursor += dt.timedelta(days=1)
        assert days == sorted(set(days))  # 겹침 없음
        assert days[0] == dt.date(2026, 7, 1) and days[-1] == dt.date(2026, 9, 30)
        assert len(days) == 92  # 누락 없음


def test_split_buckets_rejects_over_max() -> None:
    """상한 초과는 ValueError — 도구가 "Error:" 로 옮긴다(조회 전 거절)."""
    assert len(_spans("2026-07-01", "2026-07-12", "daily")) == 12  # 경계는 통과
    with pytest.raises(ValueError, match="12개를 넘습니다"):
        _spans("2026-07-01", "2026-07-13", "daily")


def test_split_buckets_rejects_unknown_unit_and_reversed_period() -> None:
    with pytest.raises(ValueError, match="daily/weekly/monthly"):
        _spans("2026-07-01", "2026-07-31", "yearly")
    with pytest.raises(ValueError, match="시작일이 종료일보다 뒤"):
        _spans("2026-07-31", "2026-07-01", "weekly")
