"""evals/option_color — #454 "옵션에 그 색이 없다" 잔존 문제 측정 하네스(#508 도입 이후).

정의는 `README.md`, 판정 로직은 `harness.py`(`app.agents.buyer.cart.options.narrow_options` 를
그대로 호출 — 재구현하지 않는다). BE 마이그레이션의 결정적 초기 재고 규칙을 오프라인 재현해
로컬 구버전 BE 로는 볼 수 없는 신 계약 응답을 시뮬레이션한다.
"""

from evals.option_color.harness import measure, option_stock, verify_against_production

__all__ = ["measure", "option_stock", "verify_against_production"]
