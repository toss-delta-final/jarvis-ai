"""니즈 전개(legs) 평가 하네스 (#332).

`decompose` 단계의 산출(`RouteDecision.case`·`category_queries`·`buy_all`·`total_budget`·`intent`)
만 잰다 — #198 의 핵심 지표("case==3 ∧ legs<=1")를 로그 밖 고정 데이터셋으로 옮긴다. 비용·
비결정론 때문에 CI 에서 돌리지 않는 **수동 실행 도구**다. 자세한 측정 범위와 한계는
`evals/legs_probe/README.md` 참조.
"""
