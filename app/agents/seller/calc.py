"""판매자 분석 순수 함수 — 전환율·안전 계산기 (DESIGN-SELLER-TOOLS-STAGE1 §4).

3층 분담("Spring 원시 집계 → AI 고도화 계산 → LLM 자연어화") 중 계산 층의 stdlib 부분.
[#290] 통계 알고리즘(이상 탐지·비율 검정·군집화)은 `analysis/` 패키지로 이관됐다 —
구 detect_sales_anomalies(SMA 편차)는 analysis.timeseries(S-H-ESD)가 대체한다.
[#345] 기간 어휘 판정·환산(구 normalize_period)은 `period.py` 로 이관됐다 — 어휘가
확장되면서 "기간 문구의 유일한 소유자"를 한 모듈로 모았다
(docs/specs/DESIGN-SELLER-PERIOD.md §4.2). 이 모듈에는 전환율 산식과 safe_eval 만 남는다.

부작용 없는 순수 함수로만 구성해 같은 입력이면 같은 출력을 보장한다(결정론, §10-②).
임계값·상한은 전부 호출부가 app.core.config.Settings 에서 읽어 인자로 주입한다 —
이 파일 내부에 튜너블 숫자를 하드코딩하지 않는다.
"""

from __future__ import annotations

import ast
import math

from app.core.config import get_settings
from app.schemas.spring import FunnelResult

# safe_eval 화이트리스트 — 사칙연산·거듭제곱·round() 만 허용한다(§3.3, `calculate` 도구용).
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
_ALLOWED_FUNCS = {"round": round}


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
