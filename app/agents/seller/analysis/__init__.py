"""판매자 분석 계산 층 — 논문 기반 알고리즘 모듈 (이슈 #290).

`calc.py`(stdlib 시대)의 후속 패키지다. 구조(3층 분담)는 유지한다:
"Spring 원시 집계 → **계산 층(본 패키지, 순수 함수·결정론)** → 워커 LLM(해석만)".

모듈 구성(각 모듈 docstring 에 근거 논문 명시 — docs/worker-papers.md):
- ``timeseries``: STL 분해 + robust GESD 검정 (S-H-ESD) — sales_anomaly·abuse 재사용
- ``proportions``: Wilson CI + two-proportion z-검정 — conversion·churn 공용
- ``segmentation``: 상품 행동 피처화 + k-means — behavior
- ``outliers``: MAD 스파이크·Tukey fence — abuse
- ``scan``: 트리거 7종 판정(고정 임계 AND 통계 유의) — 무인 스캔·null 시뮬레이션 공용
- ``types``: 결과 dataclass (프록시 근사는 ``basis`` 필드로 명시)

원칙(calc.py 에서 계승, SPEC-SELLER-001 §10-②·§0.1 D):
- 순수 함수·결정론 — 같은 입력이면 같은 출력(k-means 는 random_state 고정 주입).
- 튜너블 하드코딩 금지 — 호출부가 app.core.config.Settings 에서 읽어 인자로 주입.
- Spring 이 준 isAnomaly/deviationPct 등 참고치는 무시하고 원시값으로 재계산.
- 미집계·결측(None)은 0 으로 위장하지 않는다 — 해당 계산만 생략(판정 보류)하고 완주.
"""
