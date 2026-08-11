"""판매자 분석 레인 기간 어휘 판정·환산 (docs/specs/DESIGN-SELLER-PERIOD.md, 이슈 #345).

`calc.normalize_period`(#269 P0)를 이관·확장한 모듈이다. 달라진 점은 둘이다.

1. **어휘 확장(P1)** — `이번 달`·`올해`·`상반기`·`N분기`·`최근 N주`·`최근 N개월`·
   연도 없는 날짜를 인식한다. 다만 코드가 값을 보충한 해석이므로 그대로 실행하지 않고
   `needs_confirmation=True` 로 올려 호출부가 판매자에게 확인받게 한다(DESIGN §5).
2. **문구 소유권 단일화(§4)** — 기간과 관련된 사용자 대면 문구(되묻기·확인)는 전부
   이 모듈이 만든다. planner 는 기간을 이유로 clarification 을 쓰지 않는다
   (PLANNER_PROMPT `[기간]` 절 + AnalysisPlan.period_expr description 양쪽에 명시).
   종전에는 planner 의 자유 문장과 calc 의 예외 메시지가 둘 다 되묻기 문구가 될 수
   있어, "예외 메시지가 곧 사용자 문구"라는 P0 의 보장이 조건부였다(#345 §3).

부작용 없는 순수 함수로만 구성한다(calc.py 와 같은 원칙) — 임계값은 호출부가
Settings 에서 읽어 주입하고, 이 파일에 튜너블 숫자를 하드코딩하지 않는다.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, timedelta

from app.core.config import get_settings

# ── 어휘 패턴 (전부 **문자열 전체** 매칭 — 부분 일치 금지, #269 침묵 폴백의 원인) ──

_EXPLICIT_RANGE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})$")
_RECENT_N_DAYS_PATTERN = re.compile(r"^최근\s*(-?\d+)\s*일$")
_RECENT_N_WEEKS_PATTERN = re.compile(r"^최근\s*(-?\d+)\s*주(?:일)?$")
_RECENT_N_MONTHS_PATTERN = re.compile(r"^최근\s*(-?\d+)\s*개월$")
_QUARTER_PATTERN = re.compile(r"^([1-4])\s*(?:/\s*4)?\s*분기$")
# 연도 없는 날짜 범위 — planner 가 "M월 D일~M월 D일" 로 옮겨적는다(PLANNER_PROMPT).
# 판매자 원문("6월 1일부터 6월 30일까지")이 그대로 올라오는 경우도 받는다.
_BARE_DATE_RANGE_PATTERN = re.compile(
    r"^(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:부터\s*|\s*[~\-]\s*)"
    r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:까지)?$"
)

_THIS_MONTH_EXPRS = frozenset({"이번 달", "이번달", "금월", "이번 달치"})
_THIS_YEAR_EXPRS = frozenset({"올해", "금년", "올 해"})
_LAST_MONTH_EXPRS = frozenset({"지난달", "지난 달"})
_HALF_YEAR_EXPRS = {"상반기": 1, "하반기": 2}

# 되묻기 문구에 넣는 지원 어휘 안내 — 판매자에게 그대로 노출된다(§4.2).
_PERIOD_GUIDE = (
    "지난달 / 최근 7일 / 이번 달 / 최근 3개월 / 2026-06-01~2026-06-30 처럼 말씀해 주세요"
)

# 되묻기 문구에 되비칠 입력 길이 상한 — 장문·개행이 그대로 흘러나가지 않게 자른다.
_PERIOD_ECHO_MAX_CHARS = 30

# 여전히 지원하지 않는 **기간 단위**를 짚어 안내하기 위한 패턴(P1 확장 후 잔여분).
# 주·개월은 이제 지원하므로 빠졌고, 경계가 합의되지 않은 반년·년·한글 수사 '달'만 남는다.
# **단어 경계에 앵커한다** — 부분 문자열 검사면 "최근 목표 달성 현황"의 '달성'에 걸려
# 단위를 쓴 적도 없는 판매자가 원인을 잘못 짚은 안내를 받는다(#269 리뷰).
_UNSUPPORTED_UNIT_PATTERN = re.compile(r"(?:^|[\s\d])(?:반년|년|달)(?=$|\s)")

# "최근 N…" 의 N 자릿수 상한 — int() 변환 **전에** 검사한다. Python 3.11+ 는 4300자리
# 초과 문자열→int 에서 영어 내부 메시지 ValueError 를 내고, 그게 되묻기 문구로 그대로
# 판매자에게 나간다(#269 리뷰).
_PERIOD_MAX_DIGITS = 9


@dataclass(frozen=True)
class PeriodResolution:
    """기간 어휘 판정 결과 — 값 + 확인 필요 여부 + 해석 근거(#269 P1 원안).

    - `expr`: NFKC·공백 정규화된 입력 표현. 확인 문구에 그대로 되비친다.
    - `needs_confirmation`: 코드가 값을 보충한 해석이면 True — 호출부는 그 해석을
      응답에 고지한다(DESIGN §7). 기존 어휘 5종은 항상 False(회귀 금지).
      [#584] 이름은 확인 게이트 시절의 것이다. 게이트가 사라진 지금 이 값이 켜는 것은
      확인이 아니라 **고지**다 — 판정 의미(코드가 값을 보충했는가)는 그대로다.
    - `clipped`: R2(미래 절단)로 `date_to` 가 어제까지 줄었는가. 고지 문구가 이 사실을
      반드시 밝힌다 — 자르고 말하지 않으면 P0 가 없앤 "조용한 대체"가 형태만 바꿔 돌아온다.
    """

    date_from: date
    date_to: date
    needs_confirmation: bool
    expr: str
    clipped: bool = False
    # [#346] 비교(기준) 기간 — "지난달 대비" 처럼 판매자가 대조군을 함께 말한 경우에만 채워진다.
    # 중첩 구조로 둔 이유: 비교 기간도 어휘·경계 규칙·확인 판정이 본 기간과 **완전히 같고**,
    # 평평한 4필드(compare_from/to/expr/confirm)로 펴면 그 동일성이 코드에서 사라진다.
    comparison: PeriodResolution | None = None

    @property
    def period(self) -> tuple[date, date]:
        """(from, to) 튜플 — 구 normalize_period 반환형과 같은 모양."""
        return self.date_from, self.date_to

    @property
    def any_confirmation_needed(self) -> bool:
        """본 기간·비교 기간 중 하나라도 코드가 값을 보충했는가.

        호출부의 고지 판정은 이 값을 봐야 한다 — `needs_confirmation` 만 보면
        본 기간이 명시적이고 비교 기간만 보충된 경우("2026-06-01~2026-06-30 을 작년 대비")
        보충 사실이 조용히 지나간다.
        """
        return self.needs_confirmation or (
            self.comparison is not None and self.comparison.needs_confirmation
        )


def _echo(text: str) -> str:
    """되묻기 문구에 되비칠 입력 — 개행 제거 + 길이 절단(장문 반사 방지)."""
    flat = " ".join(text.split())
    if len(flat) > _PERIOD_ECHO_MAX_CHARS:
        return flat[:_PERIOD_ECHO_MAX_CHARS] + "…"
    return flat


def _too_long(limit: int) -> ValueError:
    return ValueError(f"기간이 너무 깁니다. {limit}일 이내로 말씀해 주세요.")


def _parse_count(raw: str, *, limit: int, example: str) -> int:
    """ "최근 N…" 의 N 을 정수로 — 자릿수 상한과 N<=0 을 여기서 끊는다."""
    if len(raw.lstrip("-")) > _PERIOD_MAX_DIGITS:
        # int() 로 넘기지 않는다 — 자릿수 상한 ValueError 의 영어 메시지가 그대로
        # 판매자에게 노출된다(#269 리뷰). 결론은 상한 초과와 같으므로 문구도 같다.
        raise _too_long(limit)
    n = int(raw)
    if n <= 0:
        # "최근 0일"·"최근 -3일" — 역전 범위(from>to)가 무음 통과하던 구멍(#269 마감 리뷰 M3).
        raise ValueError(f"기간은 1 이상이어야 합니다. 예를 들어 '{example}' 입니다.")
    return n


def _days_before(today: date, days: int, *, limit: int) -> date:
    """today − days — date 연산 한계를 ValueError 로 감싼다.

    OverflowError 가 밖으로 나가면 호출부의 except ValueError 를 빠져나가 되묻기가
    아니라 에러 경로로 샌다(#269). 이 모듈은 "예외는 ValueError 뿐"을 보장한다.
    """
    try:
        return today - timedelta(days=days)
    except (OverflowError, ValueError) as exc:
        raise _too_long(limit) from exc


def _shift_months(anchor: date, months: int, *, limit: int) -> date:
    """anchor 에서 months 달 뺀 날 — 일자가 없는 달이면 그 달 말일로 맞춘다(DESIGN §2.4).

    N×30일 근사를 쓰지 않는 이유: 판매자는 월 단위로 생각하고, 30일 근사는 월별
    비교에서 경계가 미끄러진다.
    """
    total = anchor.year * 12 + (anchor.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    try:
        day = min(anchor.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    except (ValueError, OverflowError, calendar.IllegalMonthError) as exc:
        raise _too_long(limit) from exc


def _last_month_range(today: date) -> tuple[date, date]:
    """전월 1일 ~ 전월 말일 (연 경계 롤오버 — 1월이면 전년 12월)."""
    first_of_this_month = today.replace(day=1)
    last_day_prev = first_of_this_month - timedelta(days=1)
    return last_day_prev.replace(day=1), last_day_prev


def _finalize(
    start: date,
    end: date,
    *,
    expr: str,
    yesterday: date,
    limit: int,
    needs_confirmation: bool,
) -> PeriodResolution:
    """경계 규칙 R2·R3·R4 적용 후 결과 조립 (DESIGN §3).

    R1(오늘 제외)은 각 어휘의 환산에서 이미 지켜지고, R2(미래 절단)·R3(완전 미래 거절)는
    **확인 대상 어휘에만** 적용한다 — 명시 범위(`YYYY-MM-DD~`)는 판매자가 직접 지정한
    값이라 코드가 말없이 자르지 않는다(기존 동작 유지, #269 P0 계약).
    """
    clipped = False
    if needs_confirmation:
        if start > yesterday:
            # R3 — 자를 구간 자체가 없다(8월에 "4분기"). 작년으로 미루지 않는다:
            # 어휘가 "올해의"를 뜻하므로 다른 질문에 답하는 셈이 된다(DESIGN §3).
            raise ValueError(
                f"'{_echo(expr)}' 은 아직 지나지 않은 기간입니다. 지나간 기간으로 말씀해 주세요."
            )
        if end > yesterday:
            end = yesterday
            clipped = True
    if start > end:
        raise ValueError(
            f"시작일이 종료일보다 뒤입니다('{_echo(expr)}'). 순서를 바꿔 말씀해 주세요."
        )
    if (end - start).days + 1 > limit:
        raise _too_long(limit)
    return PeriodResolution(
        date_from=start,
        date_to=end,
        needs_confirmation=needs_confirmation,
        expr=expr,
        clipped=clipped,
    )


def resolve_period(
    expr: str,
    *,
    today: date,
    recent_default_days: int,
    max_days: int | None = None,
) -> PeriodResolution:
    """자연어 기간 표현 → PeriodResolution. 해석 불가는 전부 ValueError(되묻기).

    어휘표 정본은 docs/specs/DESIGN-SELLER-PERIOD.md §2 다. 요약:
    - 확인 없이 통과: `지난달` / `어제` / `최근` / `최근 N일` / `YYYY-MM-DD~YYYY-MM-DD`
    - 확인 후 통과: `이번 달` / `올해` / `상반기`·`하반기` / `N분기` /
      `최근 N주` / `최근 N개월` / 연도 없는 날짜 범위
    - 되묻기: 그 밖의 표현(`작년 여름`·`최근 반년`·`최근 한 달`·혼합 표현·상한 초과)

    **기본값 폴백 금지**(#269): 인식하지 못한 표현을 조용히 recent_default_days 로
    대체하지 않는다. 기본값이 쓰이는 지점은 expr 이 정확히 "최근" 일 때 하나뿐이다.

    예외 메시지는 그대로 판매자에게 노출되므로 개발자용 문자열이 아니라 다음 행동을
    알려주는 안내문으로 쓴다(§4.2 — 이 모듈이 기간 문구의 유일한 소유자다).
    """
    # 전각 숫자("최근 ７일")·이형 공백을 먼저 흡수한다 — 정규화 없이는 매치 실패로
    # 떨어져 되묻기가 되는데, 판매자 의도는 명확하므로 정상 해석하는 편이 맞다.
    text = " ".join(unicodedata.normalize("NFKC", expr).split())
    limit = max_days if max_days is not None else get_settings().seller_period_max_days
    yesterday = today - timedelta(days=1)

    def done(start: date, end: date, *, confirm: bool) -> PeriodResolution:
        return _finalize(
            start, end, expr=text, yesterday=yesterday, limit=limit, needs_confirmation=confirm
        )

    # ── 확인 없이 통과하는 기존 어휘 5종 (회귀 금지 — DESIGN §2.1) ──────────────
    if range_match := _EXPLICIT_RANGE_PATTERN.match(text):
        try:
            start = date.fromisoformat(range_match.group(1))
            end = date.fromisoformat(range_match.group(2))
        except ValueError as exc:
            raise ValueError(
                f"달력에 없는 날짜입니다('{_echo(text)}'). 다시 확인해 주세요."
            ) from exc
        return done(start, end, confirm=False)

    if text in _LAST_MONTH_EXPRS:
        return done(*_last_month_range(today), confirm=False)

    if text == "어제":
        return done(yesterday, yesterday, confirm=False)

    if text == "최근":
        # N 미지정은 **정확히 "최근"** 일 때만 — 부분 일치로 넓히지 않는다(#269).
        if recent_default_days <= 0:  # 설정 오류 방어
            raise ValueError("기간은 1 이상이어야 합니다. 예를 들어 '최근 7일' 입니다.")
        return done(_days_before(today, recent_default_days, limit=limit), yesterday, confirm=False)

    if days_match := _RECENT_N_DAYS_PATTERN.match(text):
        n = _parse_count(days_match.group(1), limit=limit, example="최근 7일")
        return done(_days_before(today, n, limit=limit), yesterday, confirm=False)

    # ── 확인 후 통과하는 신규 어휘 (DESIGN §2.2) ─────────────────────────────────
    if weeks_match := _RECENT_N_WEEKS_PATTERN.match(text):
        n = _parse_count(weeks_match.group(1), limit=limit, example="최근 2주")
        return done(_days_before(today, n * 7, limit=limit), yesterday, confirm=True)

    if months_match := _RECENT_N_MONTHS_PATTERN.match(text):
        n = _parse_count(months_match.group(1), limit=limit, example="최근 3개월")
        return done(_shift_months(today, n, limit=limit), yesterday, confirm=True)

    if text in _THIS_MONTH_EXPRS:
        first = today.replace(day=1)
        last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        return done(first, last, confirm=True)

    if text in _THIS_YEAR_EXPRS:
        return done(date(today.year, 1, 1), date(today.year, 12, 31), confirm=True)

    if half := _HALF_YEAR_EXPRS.get(text):
        start = date(today.year, 1, 1) if half == 1 else date(today.year, 7, 1)
        end = date(today.year, 6, 30) if half == 1 else date(today.year, 12, 31)
        return done(start, end, confirm=True)

    if quarter_match := _QUARTER_PATTERN.match(text):
        q = int(quarter_match.group(1))
        start_month = 3 * (q - 1) + 1
        end_month = start_month + 2
        start = date(today.year, start_month, 1)
        end = date(today.year, end_month, calendar.monthrange(today.year, end_month)[1])
        return done(start, end, confirm=True)

    if bare_match := _BARE_DATE_RANGE_PATTERN.match(text):
        return done(*_bare_date_range(bare_match, today=today, yesterday=yesterday), confirm=True)

    # ── 되묻기 (DESIGN §2.3) ────────────────────────────────────────────────────
    if _UNSUPPORTED_UNIT_PATTERN.search(text):
        # 단위를 짚어 안내한다 — 되묻기라는 결론이 같아도 이유가 틀리면 대화가 더 꼬인다.
        raise ValueError(
            f"'{_echo(text)}' 은 아직 지원하지 않는 기간 단위입니다. "
            "'최근 30일'·'최근 3개월' 처럼 일·주·개월 단위로 말씀해 주세요."
        )
    raise ValueError(f"'{_echo(text)}' 기간을 이해하지 못했습니다. {_PERIOD_GUIDE}.")


def _bare_date_range(match: re.Match[str], *, today: date, yesterday: date) -> tuple[date, date]:
    """연도 없는 날짜 범위의 연도 추론 (R5) — 올해로 읽고, 완전 미래면 작년으로 재시도.

    `6월 1일~6월 30일` 은 통상 지나간 날을 가리킨다. R5 를 `N분기`·`상반기`·`올해` 에는
    적용하지 않는 이유는 DESIGN §3 참조 — 그 어휘들은 "올해의"를 뜻하기 때문이다.
    """
    from_month, from_day, to_month, to_day = (int(g) for g in match.groups())
    for year_offset in (0, -1):
        year = today.year + year_offset
        try:
            start = date(year, from_month, from_day)
            # 종료가 시작보다 앞이면 연 경계를 넘긴 범위(12월~1월)로 읽는다.
            end_year = year if (to_month, to_day) >= (from_month, from_day) else year + 1
            end = date(end_year, to_month, to_day)
        except ValueError as exc:
            raise ValueError(
                f"달력에 없는 날짜입니다('{_echo(match.group(0))}'). 다시 확인해 주세요."
            ) from exc
        if start <= yesterday:
            return start, end
    # 올해도 작년도 아직 오지 않은 날짜 — _finalize 의 R3 가 문구를 담당한다.
    return date(today.year, from_month, from_day), date(today.year, to_month, to_day)


# ── 비교(기준) 기간 (DESIGN-SELLER-PERIOD §2.5 — 이슈 #346) ────────────────────

# 비교 기간은 **본 기간에 상대적**이라 단독으로 환산할 수 없다. 그래서 어휘도 본 기간과
# 분리해 두고(아래 표), 환산은 resolve_comparison(expr, base) 가 base 를 받아서 한다.
#
# "직전 동일 기간" 만 확인이 필요 없다 — 코드가 보충하는 값이 없고(길이도 끝점도 base 에서
# 결정된다), 이미 get_funnel·get_churn_cohort 가 내부적으로 쓰는 정의와 같다(tools._previous_period).
# 반면 "지난달 대비"·"작년 대비" 는 **정렬 방식을 코드가 고른다**(달력 시프트) — 판매자가
# "지난달 전체"를 뜻했는지 "지난달의 같은 날짜 구간"을 뜻했는지 알 수 없으므로 확인 대상이다.
_PREV_ADJACENT_EXPRS = frozenset({"직전 동일 기간", "직전 기간", "직전 동일기간", "이전 기간"})
_PREV_MONTH_EXPRS = frozenset({"지난달 대비", "전월 대비", "전월 동기간", "지난달 동기간"})
_PREV_YEAR_EXPRS = frozenset({"작년 대비", "전년 대비", "전년 동기간", "작년 동기간"})

_COMPARISON_GUIDE = "직전 동일 기간 / 지난달 대비 / 작년 대비 처럼 말씀해 주세요"

# 판정은 공백 제거형으로 한다 — 스캐너 정규식이 "직전동일기간"·"지난 달 대비" 를 모두
# 잡으므로, 리터럴 표기 그대로 비교하면 잡아 놓고 해석하지 못하는 어긋남이 생긴다.
_PREV_ADJACENT_COMPACT = frozenset(e.replace(" ", "") for e in _PREV_ADJACENT_EXPRS)
_PREV_MONTH_COMPACT = frozenset(e.replace(" ", "") for e in _PREV_MONTH_EXPRS)
_PREV_YEAR_COMPACT = frozenset(e.replace(" ", "") for e in _PREV_YEAR_EXPRS)

# 자유 발화 스캐너용 — 본 기간 스캐너보다 **먼저** 돌려 매치 구간을 걷어낸다.
# 안 그러면 "지난달 대비 이번 달" 의 '지난달' 이 본 기간 표현으로 잡혀, 표현이 둘이라는
# 이유로 되묻기가 된다(비교 질문이 통째로 막힌다).
_COMPARISON_MENTION_SOURCES: tuple[str, ...] = (
    r"직전\s*동일\s*기간",
    r"직전\s*기간",
    r"이전\s*기간",
    r"지난\s?달\s*(?:대비|동기간)",
    r"전월\s*(?:대비|동기간)",
    r"작년\s*(?:대비|동기간)",
    r"전년\s*(?:대비|동기간)",
)
_COMPARISON_MENTION_PATTERN = re.compile(
    "|".join(f"(?:{source})" for source in _COMPARISON_MENTION_SOURCES)
)


def resolve_comparison(
    expr: str,
    base: PeriodResolution,
    *,
    max_days: int | None = None,
) -> PeriodResolution:
    """비교 기간 표현 + 본 기간 → 대조군 PeriodResolution. 해석 불가는 ValueError.

    - `직전 동일 기간`: base 바로 앞의 **같은 길이** 구간. 확인 불필요(보충값 없음).
    - `지난달 대비`·`전월 동기간`: base 를 달력 1달 뒤로 민 구간(§2.4 와 같은 말일 보정).
    - `작년 대비`·`전년 동기간`: base 를 1년 뒤로 민 구간.

    달력 시프트를 N×30일 근사로 바꾸지 않는 이유는 §2.4 와 같다 — 판매자는 월 단위로
    생각하고, 근사는 월별 비교에서 경계가 미끄러진다.
    """
    text = " ".join(unicodedata.normalize("NFKC", expr).split())
    limit = max_days if max_days is not None else get_settings().seller_period_max_days
    length_days = (base.date_to - base.date_from).days
    # 공백 이형 흡수 — 스캐너가 잡은 "직전동일기간"·"지난 달 대비" 도 같은 어휘다.
    compact = text.replace(" ", "")

    if compact in _PREV_ADJACENT_COMPACT:
        end = base.date_from - timedelta(days=1)
        start = _days_before(end, length_days, limit=limit)
        confirm = False
    elif compact in _PREV_MONTH_COMPACT:
        start = _shift_months(base.date_from, 1, limit=limit)
        end = _shift_months(base.date_to, 1, limit=limit)
        confirm = True
    elif compact in _PREV_YEAR_COMPACT:
        start = _shift_months(base.date_from, 12, limit=limit)
        end = _shift_months(base.date_to, 12, limit=limit)
        confirm = True
    else:
        raise ValueError(f"'{_echo(text)}' 비교 기간을 이해하지 못했습니다. {_COMPARISON_GUIDE}.")

    if start > end:
        # 달력 시프트가 순서를 뒤집는 경우(말일 보정) — 조용히 뒤집어 쓰지 않는다.
        raise ValueError(
            f"'{_echo(text)}' 로 비교할 기간을 만들지 못했습니다. {_COMPARISON_GUIDE}."
        )
    if (end - start).days + 1 > limit:
        raise _too_long(limit)
    return PeriodResolution(date_from=start, date_to=end, needs_confirmation=confirm, expr=text)


def find_comparison_mention(message: str) -> tuple[str, str | None]:
    """발화에서 비교 기간 표현을 떼어낸다 → (남은 발화, 비교 표현 or None).

    본 기간 스캐너보다 **먼저** 돌려야 한다 — "지난달 대비 이번 달" 의 '지난달' 이 본 기간
    표현으로 잡히면 표현이 둘이라는 이유로 비교 질문이 통째로 되묻기가 된다.
    """
    text = " ".join(unicodedata.normalize("NFKC", message).split())
    match = _COMPARISON_MENTION_PATTERN.search(text)
    if match is None:
        return text, None
    remainder = f"{text[: match.start()]} {text[match.end() :]}"
    return " ".join(remainder.split()), " ".join(match.group(0).split())


# ── general 레인 진입점 (DESIGN-SELLER-PERIOD §7 — 이슈 #346) ──────────────────

# 기간 미언급일 때 쓰는 표현. planner 의 `[기간]` 절과 **같은 규약**이다("기간 언급이
# 없으면 '최근'") — 두 레인이 같은 기본값에 도달해야 대조가 성립한다.
_NO_MENTION_EXPR = "최근"

# 자유 발화 스캐너용 어휘 패턴. resolve_period 의 패턴은 문자열 **전체** 매칭(^…$)이라
# 문장 속에서 쓸 수 없어, 같은 어휘를 부분 스캔용으로 한 번 더 적는다. 두 벌이 갈라지면
# "어휘표엔 있는데 general 에서만 안 잡히는" 구멍이 생기므로 §2 어휘표 전 항목이 이
# 스캐너에도 걸리는지 대조 테스트가 확인한다(tests/unit/test_seller_period_lane_parity.py).
#
# **미지원 어휘도 일부러 잡는다.** 잡아서 resolve_period 로 넘겨야 되묻기 문구가 나가고,
# 안 잡으면 "기간 언급 없음"으로 읽혀 기본 7일로 조용히 답한다 — #269 가 없앤 침묵
# 대체가 general 레인에서 형태만 바꿔 되살아난다("오늘 매출 얼마야?" → 7일치 응답).
#
# 순서가 곧 우선순위다(정규식 교체는 앞 대안부터 시도된다) — 긴 표현을 먼저 둔다.
# 특히 단독 "최근" 은 맨 끝이어야 "최근 3개월" 의 앞부분만 선점하지 않는다.
_MENTION_SOURCES: tuple[str, ...] = (
    # 지원 — 날짜 범위(명시·연도 없음)
    r"\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}",
    r"\d{1,2}\s*월\s*\d{1,2}\s*일\s*(?:부터\s*|[~\-]\s*)\d{1,2}\s*월\s*\d{1,2}\s*일(?:\s*까지)?",
    # 지원 — "최근 N단위"(미지원 단위는 아래 미지원 절이 잡는다)
    r"최근\s*-?\d+\s*(?:일|주일|주|개월)",
    # 지원 — 고정 어휘
    r"지난\s?달",
    r"이번\s?달",
    r"금월",
    r"올\s?해",
    r"금년",
    r"상반기",
    r"하반기",
    r"[1-4]\s*(?:/\s*4)?\s*분기",
    r"어제",
    # 미지원(→ 되묻기) — 기간처럼 보이는 표현. 여기서 잡지 않으면 침묵 대체가 된다.
    r"최근\s*(?:반\s?년|한\s?달|두\s?달|세\s?달|몇\s?[일달주]|며칠)",
    r"오늘",
    r"금일",
    r"내일",
    r"이번\s?주",
    r"저번\s?주",
    r"지난\s?주",
    r"다음\s?주",
    r"지지난\s?달",
    r"저번\s?달",
    r"다음\s?달",
    r"재작년",
    r"작년",
    r"내년",
    r"\d{4}\s*년",
    r"\d{1,2}\s*월",
    # 지원 — 단독 "최근"(맨 끝, 위 주석 참조)
    r"최근",
)
_PERIOD_MENTION_PATTERN = re.compile("|".join(f"(?:{source})" for source in _MENTION_SOURCES))


def find_period_mentions(message: str) -> list[str]:
    """자유 발화에서 기간 표현을 등장 순서대로 추출한다(중복 제거).

    반환값은 **판정하지 않은 원문**이다 — 지원 어휘인지, 어떤 문구로 되물을지는 전부
    resolve_period 소관이다(§4.2 문구 소유권 단일화). 이 함수는 "문장의 어느 조각이
    기간인가"만 고른다.
    """
    text = " ".join(unicodedata.normalize("NFKC", message).split())
    found: list[str] = []
    for match in _PERIOD_MENTION_PATTERN.finditer(text):
        expr = " ".join(match.group(0).split())
        if expr not in found:
            found.append(expr)
    return found


def resolve_from_message(
    message: str,
    *,
    today: date,
    recent_default_days: int,
    max_days: int | None = None,
) -> PeriodResolution:
    """자유 발화 → PeriodResolution — planner 가 없는 general 레인의 기간 진입점.

    분석 레인은 planner 가 기간 표현을 옮겨적어 주지만 general 레인에는 planner 가 없다.
    그렇다고 이 레인에 planner 를 붙이면 가장 싼 레인에 LLM 왕복이 하나 늘어난다 —
    어휘표가 폐집합이므로 **코드로 훑는다**(LLM 0회).

    - 표현 1개: 그대로 resolve_period. 어휘표 밖이면 ValueError(되묻기).
    - 표현 0개: ``"최근"`` — planner 의 `[기간]` 절과 같은 규약이라 두 레인이 같은
      기본값에 도달한다. 기간 인자가 필요 없는 질문("재고 얼마 남았어?")은 이 값을
      쓰지 않을 뿐 해가 없다.
    - 표현 2개 이상("이번 달 들어 최근 7일"): 되묻는다 — 어느 쪽을 뜻하는지 코드가
      고르면 그 선택이 곧 조용한 대체다(DESIGN §2.3 혼합 표현 규칙과 같은 결론).
    - 비교 표현("지난달 대비")이 섞여 있으면 먼저 떼어내 `comparison` 으로 채운다(§2.5).
    """
    # 비교 표현을 **먼저** 떼어낸다 — "지난달 대비 이번 달" 의 '지난달' 이 본 기간으로
    # 잡히면 표현이 둘이라는 이유로 비교 질문이 통째로 되묻기가 된다.
    remainder, comparison_expr = find_comparison_mention(message)
    mentions = find_period_mentions(remainder)
    if len(mentions) > 1:
        listed = " / ".join(_echo(mention) for mention in mentions[:3])
        raise ValueError(
            f"기간 표현이 여러 개 보입니다({listed}). 어느 기간을 말씀하시는지 하나만 알려주세요."
        )
    expr = mentions[0] if mentions else _NO_MENTION_EXPR
    base = resolve_period(
        expr, today=today, recent_default_days=recent_default_days, max_days=max_days
    )
    if comparison_expr is None:
        return base
    return replace(base, comparison=resolve_comparison(comparison_expr, base, max_days=max_days))


# ── 고지 문구 (DESIGN §4.3 — 기간 문구의 유일한 생성 지점) ──────────────────────


def _comparison_line(comparison: PeriodResolution) -> str:
    """비교 기간을 고지 문구에 붙이는 한 줄 — 본 기간과 같은 형식으로 날짜를 밝힌다.

    비교 기간도 코드가 정렬 방식을 고른 해석이다("지난달 대비" → 달력 시프트).
    본 기간만 밝히고 대조군을 감추면 고지의 의미가 절반만 성립한다(§2.5).
    """
    return (
        f"비교 기준은 '{_echo(comparison.expr)}' — {comparison.date_from.isoformat()} ~ "
        f"{comparison.date_to.isoformat()} 입니다."
    )


_DISCLOSURE_CLIPPED_NOTE = " (아직 지나지 않은 날짜는 빼고 어제까지만 봤습니다.)"


def disclosure_text(resolution: PeriodResolution) -> str:
    """해석 고지 — 확인 대신 "무엇으로 봤는지"를 먼저 밝힌다(§7, #346·#584).

    [#584] 두 레인이 **같은 이 문구**를 쓴다. 종전에는 분석 레인만 실행 전에 확인을
    받았고(구 게이트 ①.7) general 은 고지만 했는데, 그 비대칭이 확인 상태기계·승인
    어휘·TTL 을 한 레인에만 얹는 값을 치렀다. 게이트를 걷어낸 지금은 두 레인 모두
    관용 해석 후 고지한다 — P0 가 없앤 "조용한 대체"는 확인이 아니라 이 고지가 막는다.

    고지는 **코드가 값을 보충한 해석에만** 붙인다(needs_confirmation). 기간을 말하지
    않은 질문까지 "최근 7일 기준입니다"로 열면 "재고 얼마 남았어?" 같은 기간 무관
    질문에 무관한 고지가 매번 따라붙는다.
    """
    note = _DISCLOSURE_CLIPPED_NOTE if resolution.clipped else ""
    text = (
        f"'{_echo(resolution.expr)}' 을 {resolution.date_from.isoformat()} ~ "
        f"{resolution.date_to.isoformat()} 기간으로 봤습니다.{note}"
    )
    if resolution.comparison is not None:
        text = f"{text}\n{_comparison_line(resolution.comparison)}"
    return text


# ── 기간 버킷 분할 (이슈 #518 — I-31 리뷰 추이) ────────────────────────────────

# 사용자가 지정하는 버킷 어휘. `charts.py` 의 `_bucket_days` 는 **기간 길이로 자동
# 선택**하는 별개 축이라 여기 어휘와 겹치지 않는다(그쪽은 좌표 상한 60점을 지키는 게
# 목적, 이쪽은 "주별로 보여줘" 라는 판매자 요구가 목적).
BUCKET_UNITS = ("daily", "weekly", "monthly")


def split_buckets(
    date_from: date, date_to: date, unit: str, *, max_buckets: int
) -> list[tuple[date, date]]:
    """[from, to] 를 버킷 단위로 잘라 (시작일, 종료일) 목록을 만든다 — 순수 함수.

    경계 규칙:
    - `daily`·`weekly` 는 **from 앵커 고정창**(1일·7일)이다. 달력 주(월요일 시작)를
      쓰면 "최근 4주"가 5조각으로 갈라지며 첫 조각이 2~3일짜리로 나와, 판매자가
      첫 구간을 급락으로 오독한다. `charts._bucket_starts` 도 같은 앵커 규칙이라
      두 경로의 x축이 어긋나지 않는다.
    - `monthly` 만 **달력 월 경계**다 — "7월/8월"이라는 판매자 어휘가 곧 경계이고,
      30일 고정창은 그 어휘와 어긋난다. 첫·마지막 구간은 from/to 로 클립한다.

    마지막 구간은 to 를 넘지 않게 자른다 — 넘겨서 조회하면 I-31 이 기간 밖 리뷰를
    함께 세어 마지막 버킷만 부풀어 오른다.

    Raises:
        ValueError: 어휘 밖 unit, from > to, 또는 구간 수가 max_buckets 초과.
            호출부(도구)가 "Error:" 문자열로 옮긴다 — 이 모듈은 문구만 만들고
            degrade 판정은 하지 않는다(§4 문구 소유권과 같은 결).
    """
    if unit not in BUCKET_UNITS:
        raise ValueError(
            f"bucket 은 {'/'.join(BUCKET_UNITS)} 중 하나여야 합니다(받은 값: {unit!r})"
        )
    if date_to < date_from:
        raise ValueError(f"시작일이 종료일보다 뒤입니다({date_from}~{date_to})")

    spans: list[tuple[date, date]] = []
    cursor = date_from
    while cursor <= date_to:
        if unit == "monthly":
            last_day = calendar.monthrange(cursor.year, cursor.month)[1]
            end = min(cursor.replace(day=last_day), date_to)
        else:
            width = 1 if unit == "daily" else 7
            end = min(cursor + timedelta(days=width - 1), date_to)
        spans.append((cursor, end))
        # 상한 초과는 조기 탈출한다 — 731일(seller_period_max_days) × daily 를
        # 끝까지 돌고 나서 거절하면 731개 튜플을 헛으로 만든다.
        if len(spans) > max_buckets:
            raise ValueError(
                f"{date_from}~{date_to} 를 {unit} 로 나누면 구간이 {max_buckets}개를 넘습니다"
            )
        cursor = end + timedelta(days=1)
    return spans
