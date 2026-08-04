"""판매자 분석 순수 함수 — 기간 파싱·전환율·안전 계산기 (DESIGN-SELLER-TOOLS-STAGE1 §4).

3층 분담("Spring 원시 집계 → AI 고도화 계산 → LLM 자연어화") 중 계산 층의 stdlib 부분.
[#290] 통계 알고리즘(이상 탐지·비율 검정·군집화)은 `analysis/` 패키지로 이관됐다 —
구 detect_sales_anomalies(SMA 편차)는 analysis.timeseries(S-H-ESD)가 대체한다.
이 모듈에는 논문 의존이 없는 유틸(normalize_period·safe_eval·전환율 산식)만 남는다.

부작용 없는 순수 함수로만 구성해 같은 입력이면 같은 출력을 보장한다(결정론, §10-②).
임계값·상한은 전부 호출부가 app.core.config.Settings 에서 읽어 인자로 주입한다 —
이 파일 내부에 튜너블 숫자를 하드코딩하지 않는다.
"""

from __future__ import annotations

import ast
import math
import re
import unicodedata
from datetime import date, timedelta

from app.core.config import get_settings
from app.schemas.spring import FunnelResult

# "최근 N일" 표현 (normalize_period) — **문자열 전체가** 이 형태여야 한다(^…$).
# 종전에는 `"최근" in text` 부분 일치로 분기한 뒤 이 패턴이 안 걸리면 기본 일수로
# 떨어뜨렸다. 그 구조 때문에 "최근 3개월"·"최근 반년"·"이번 달 들어 최근 7일" 이
# 되묻기도 경고도 없이 전부 7일로 처리됐다(#269). 부분 일치 금지.
# 음수 부호를 패턴에 포함하는 이유: "최근 -3일" 이 매치 실패로 새어 기본값으로
# 떨어지지 않고 아래 n<=0 가드에 도달하게 하려는 것이다.
_RECENT_N_PATTERN = re.compile(r"^최근\s*(-?\d+)\s*일$")

# 명시 날짜 범위 "YYYY-MM-DD~YYYY-MM-DD" 패턴 (normalize_period, 3-1 확장).
_EXPLICIT_RANGE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})$")

# 되묻기 문구에 넣는 지원 어휘 안내 — 판매자에게 그대로 노출된다.
# 예외 메시지가 곧 사용자 대면 문구다(pipeline.resolve_plan → PipelineResult(kind="clarification")).
# 개발자용 문자열("파싱 불가한 기간 표현: ...")을 노출하지 않는다.
_PERIOD_GUIDE = "지난달 / 최근 7일 / 어제 / 2026-06-01~2026-06-30 처럼 말씀해 주세요"

# 되묻기 문구에 되비칠 입력 길이 상한 — 장문·개행이 그대로 흘러나가지 않게 자른다.
_PERIOD_ECHO_MAX_CHARS = 30

# "최근 …" 인데 단위가 '일' 이 아닌 경우를 짚어 안내하기 위한 패턴.
# **단어 경계에 앵커한다.** 부분 문자열 검사(`"달" in text`)로 두면 "최근 목표 달성
# 현황"의 "달성", "최근 주말 프로모션"의 "주말", "최근 분기점 지표"의 "분기점"에 걸려,
# 단위를 쓴 적도 없는 판매자가 "일 단위로 말씀해 주세요" 라는 원인을 잘못 짚은 안내를
# 받는다(#269 리뷰). 되묻기라는 결론은 같아도 이유가 틀리면 대화가 더 꼬인다.
# 앞은 숫자·공백·문두, 뒤는 공백·문말 — 확신이 설 때만 단위 안내로 보낸다.
_NON_DAY_UNIT_PATTERN = re.compile(r"(?:^|[\s\d])(?:주일|개월|반년|분기|주|달|년)(?=$|\s)")

# "최근 N일" 의 N 자릿수 상한 — int() 변환 **전에** 검사한다.
# Python 3.11+ 는 4300자리 초과 문자열→int 변환에서 영어 내부 메시지 ValueError 를
# 내고("Exceeds the limit (4300) for integer string conversion…"), 그게 되묻기 문구로
# 그대로 판매자에게 나간다(#269 리뷰). max_days 가 막으려던 것과 같은 실패 양상이
# 파싱 단계에서 재현되므로, 상한 판정 전에 자릿수로 먼저 끊는다.
_PERIOD_MAX_DIGITS = 9

# safe_eval 화이트리스트 — 사칙연산·거듭제곱·round() 만 허용한다(§3.3, `calculate` 도구용).
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
_ALLOWED_FUNCS = {"round": round}


def deviation_pct(actual: float, baseline: float) -> float:
    """기준(baseline) 대비 실측(actual)의 편차 %. baseline==0 이면 0 나눗셈 방지로 0.0."""
    if baseline == 0:
        return 0.0
    return (actual - baseline) / baseline * 100


def _safe_ratio_pct(numerator: int, denominator: int) -> float:
    """분모 0 이면 0.0 (0 나눗셈 방지) — 전환율 계산 내부 헬퍼."""
    if denominator == 0:
        return 0.0
    return numerator / denominator * 100


def conversion_rates(funnel: FunnelResult) -> dict[str, float | None]:
    """구매전환 퍼널 단계별 전환율(%) — view→cart→checkout→purchase.

    [PR#184 리뷰 반영] 미집계 단계(funnel.uncomputable_stages — I-7 stages[] 의
    count=null·computable=false, 예: checkout v1 미계산 구간)가 분자/분모로 걸리는
    전환율은 None 을 반환한다 — 0%(전환 전무)와 "집계 안 됨"을 구분하기 위함이다.
    """
    uncomputable = set(funnel.uncomputable_stages)

    def _rate(num_field: str, den_field: str, numerator: int, denominator: int) -> float | None:
        if num_field in uncomputable or den_field in uncomputable:
            return None
        return _safe_ratio_pct(numerator, denominator)

    return {
        "view_to_cart": _rate("cart", "view", funnel.cart, funnel.view),
        "cart_to_checkout": _rate("checkout", "cart", funnel.checkout, funnel.cart),
        "checkout_to_purchase": _rate("purchase", "checkout", funnel.purchase, funnel.checkout),
    }


def compare_conversion(
    current: FunnelResult, baseline: FunnelResult, *, drop_pct: float
) -> dict[str, bool]:
    """단계별 전환율이 baseline 대비 drop_pct 이상 하락했는지 판정한다.

    baseline 전환율이 0 이면 비교 기준이 없어 하락 판정을 내리지 않는다(False).
    어느 한쪽이 미집계(None, PR#184 리뷰 반영)여도 판정 불가 = False 다.
    """
    current_rates = conversion_rates(current)
    baseline_rates = conversion_rates(baseline)

    result: dict[str, bool] = {}
    for stage, base_rate in baseline_rates.items():
        current_rate = current_rates[stage]
        if base_rate is None or current_rate is None or base_rate == 0:
            result[stage] = False
            continue
        deviation = deviation_pct(current_rate, base_rate)
        result[stage] = deviation <= -drop_pct
    return result


def _echo_period(text: str) -> str:
    """되묻기 문구에 되비칠 입력 — 개행 제거 + 길이 절단(장문 반사 방지)."""
    flat = " ".join(text.split())
    if len(flat) > _PERIOD_ECHO_MAX_CHARS:
        return flat[:_PERIOD_ECHO_MAX_CHARS] + "…"
    return flat


def normalize_period(
    expr: str,
    *,
    today: date,
    recent_default_days: int,
    max_days: int | None = None,
) -> tuple[date, date]:
    """자연어 기간 표현 → (from, to) 날짜 범위.

    - "지난달": 전월 1일 ~ 전월 말일(연 경계 롤오버 처리 — 1월이면 전년 12월).
    - "최근 N일" / "최근": (today - N) ~ (today - 1). 오늘은 항상 제외한다
      (§10-④, 당일 데이터는 아직 집계가 완결되지 않았을 수 있어 경계에서 뺀다).
      N 이 없는 "최근"(정확히 이 두 글자)일 때만 recent_default_days 를 쓴다.
    - "어제": (today - 1) ~ (today - 1).
    - "YYYY-MM-DD~YYYY-MM-DD"(3-1 확장): 명시 범위 그대로. LLM 은 질문의 날짜를
      옮겨적기만 한다(날짜 산수 금지, 장치 ④). from > to 면 ValueError.
    - 그 밖의 표현("이번 달"·"최근 3개월"·"최근 2주" 등)은 전부 ValueError —
      호출부가 사용자에게 되묻는다.

    [#269, 2026-08-03] **기본값 폴백 금지.** 인식하지 못한 표현을 조용히
    recent_default_days 로 대체하지 않는다. 종전 구조(`"최근" in text` 부분 일치 +
    정규식 실패 시 기본값)에서는 판매자가 3개월을 물어도 7일 결과를 받고 그 사실을
    알 방법이 없었다. 기본값이 쓰이는 지점은 expr 이 정확히 "최근" 일 때 하나뿐이다.

    예외 메시지는 그대로 판매자에게 노출되므로(호출부가 되묻기 token 으로 전달)
    개발자용 문자열이 아니라 다음 행동을 알려주는 안내문으로 쓴다.

    max_days 는 기간 상한(일)이다. 미지정이면 Settings 에서 읽는다 — 상한이 없으면
    "최근 999999일" 이 date 연산에서 OverflowError 를 내고, 호출부의 except ValueError
    를 빠져나가 되묻기가 아니라 에러 경로로 샌다(#269).
    """
    # 전각 숫자("최근 ７일")·이형 공백을 먼저 흡수한다 — 정규화 없이는 매치 실패로
    # 떨어져 되묻기가 되는데, 판매자 의도는 명확하므로 정상 해석하는 편이 맞다.
    text = " ".join(unicodedata.normalize("NFKC", expr).split())
    limit = max_days if max_days is not None else get_settings().seller_period_max_days

    range_match = _EXPLICIT_RANGE_PATTERN.match(text)
    if range_match:
        try:
            start = date.fromisoformat(range_match.group(1))
            end = date.fromisoformat(range_match.group(2))
        except ValueError as exc:
            raise ValueError(
                f"달력에 없는 날짜입니다('{_echo_period(text)}'). 다시 확인해 주세요."
            ) from exc
        if start > end:
            raise ValueError(
                f"시작일이 종료일보다 뒤입니다('{_echo_period(text)}'). 순서를 바꿔 말씀해 주세요."
            )
        if (end - start).days + 1 > limit:
            raise ValueError(f"기간이 너무 깁니다. {limit}일 이내로 말씀해 주세요.")
        return start, end

    if text in ("지난달", "지난 달"):
        first_of_this_month = today.replace(day=1)
        last_day_prev_month = first_of_this_month - timedelta(days=1)
        first_day_prev_month = last_day_prev_month.replace(day=1)
        return first_day_prev_month, last_day_prev_month

    if text == "어제":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday

    # N 미지정은 **정확히 "최근"** 일 때만 — 부분 일치로 넓히지 않는다(#269).
    if text == "최근":
        n = recent_default_days
    elif recent_match := _RECENT_N_PATTERN.match(text):
        raw = recent_match.group(1)
        if len(raw.lstrip("-")) > _PERIOD_MAX_DIGITS:
            # int() 로 넘기지 않는다 — 자릿수 상한 ValueError 의 영어 메시지가 그대로
            # 판매자에게 노출된다(#269 리뷰). 결론은 아래 n>limit 과 같으므로 문구도 같다.
            raise ValueError(f"기간이 너무 깁니다. {limit}일 이내로 말씀해 주세요.")
        n = int(raw)
    elif text.startswith("최근") and _NON_DAY_UNIT_PATTERN.search(text):
        # "최근 3개월"·"최근 2주"·"최근 반년" — 주·개월 환산은 P1(확인 흐름) 소관이라
        # 여기서는 되묻는다. 조용히 기본 7일로 떨어뜨리지 않는 것이 이 이슈의 핵심이다.
        raise ValueError(
            f"아직 일 단위 기간만 볼 수 있습니다. '{_echo_period(text)}' 대신 "
            "'최근 90일' 처럼 말씀해 주세요."
        )
    else:
        raise ValueError(f"'{_echo_period(text)}' 기간을 이해하지 못했습니다. {_PERIOD_GUIDE}.")

    if n <= 0:
        # "최근 0일"·"최근 -3일" — 역전 범위(from>to)가 무음 통과하던 구멍(마감 리뷰 M3).
        raise ValueError("기간 일수는 1일 이상이어야 합니다. 예를 들어 '최근 7일' 입니다.")
    if n > limit:
        raise ValueError(f"기간이 너무 깁니다. {limit}일 이내로 말씀해 주세요.")
    try:
        end = today - timedelta(days=1)
        start = today - timedelta(days=n)
    except OverflowError as exc:
        # Settings 는 seller_period_max_days 를 10년으로 묶지만(#269 리뷰), max_days 는
        # 함수 인자라 호출부가 직접 큰 값을 넘길 수 있다. 그 경우에도 OverflowError 가
        # 밖으로 나가면 호출부의 except ValueError 를 빠져나가 되묻기 대신 에러 경로가
        # 된다 — 설정 검증과 별개로 이 함수 자체가 "예외는 ValueError 뿐" 을 보장한다.
        raise ValueError(f"기간이 너무 깁니다. {limit}일 이내로 말씀해 주세요.") from exc
    return start, end


def safe_eval(expression: str) -> float:
    """LLM 이 만든 계산식을 안전하게 평가한다 (ast 화이트리스트, §3.3 `calculate` 도구용).

    허용: 숫자·괄호·사칙연산(+ - * / // % **)·단항 부호·`round()` 호출뿐이다.
    `__import__`·속성 접근(`a.b`)·변수 참조(`a`)·기타 함수 호출은 전부 ValueError 로 차단한다
    — LLM 이 생성한 임의 코드를 신뢰하지 않고 구조적으로 막는다(보안).
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"계산식을 파싱할 수 없습니다: {expression!r}") from exc
    return _safe_eval_node(tree.body)


def _safe_eval_node(node: ast.AST) -> float:
    """safe_eval 내부 재귀 평가기 — 화이트리스트 밖 노드는 전부 ValueError."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ValueError(f"허용되지 않는 상수: {node.value!r}")
        return node.value

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise ValueError(f"허용되지 않는 연산자: {type(node.op).__name__}")
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        return _apply_binop(node.op, left, right)

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise ValueError(f"허용되지 않는 단항 연산자: {type(node.op).__name__}")
        value = _safe_eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError("허용되지 않는 함수 호출입니다(round() 만 허용)")
        if node.keywords:
            raise ValueError("키워드 인자는 허용되지 않습니다")
        args = [_safe_eval_node(arg) for arg in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)

    # Name(변수 참조)·Attribute(속성 접근)·Import 등은 여기서 전부 차단된다.
    raise ValueError(f"허용되지 않는 표현식 요소: {type(node).__name__}")


def _apply_binop(op: ast.operator, left: float, right: float) -> float:
    """BinOp 화이트리스트 연산 적용 — _safe_eval_node 헬퍼."""
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return left / right
    if isinstance(op, ast.FloorDiv):
        return left // right
    if isinstance(op, ast.Mod):
        return left % right
    if isinstance(op, ast.Pow):
        # DoS 방어(리뷰 반영): 동기 블로킹은 int**int(CPython 임의정밀도, 결과가 수백만
        # 자리로 커짐)에서만 발생한다 — 9**9**9**9·10**9999999 등이 이벤트 루프를 막아
        # 프로세스 공유 세션까지 정지시킨다. float 가 섞이면 C pow(O(1))라 블로킹이 없고
        # 과대 지수는 OverflowError 로 빠르게 종결되므로 가드 대상에서 제외한다(오탐 방지).
        if (
            isinstance(left, int)
            and isinstance(right, int)
            and right > 0
            and left not in (0, 1, -1)
        ):
            est_digits = right * math.log10(abs(left))
            max_digits = get_settings().seller_calc_max_result_digits
            if est_digits > max_digits:
                raise ValueError(
                    f"계산 결과가 너무 큽니다(약 {int(est_digits)}자리, 상한 {max_digits}자리)"
                )
        return left**right
    raise ValueError(f"허용되지 않는 연산자: {type(op).__name__}")
