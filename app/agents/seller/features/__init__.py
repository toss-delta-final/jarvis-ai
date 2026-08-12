"""판매자 고객 축 피처 엔지니어링 패키지 (이슈 #593).

I-38(고객 행동 피처 집계) 응답을 **고객 1행 = 피처 27개**로 옮기고, 그중 12개만
K-Means 입력 벡터로 추려 군집한 뒤 `seller_analysis_snapshots` 1행으로 조립한다.

모듈 구성:
- ``spec``: 버전·벡터 차원 순서·구간 매핑 등 **상수 정본**(Settings 기본값의 출처)
- ``transforms``: log1p · shrinkage · 구간→로그 · recency · 오분위 (순수 함수, stdlib 만)
- ``customer``: I-38 rows → ``feature_rows``(6블록) + 벡터 행렬
- ``clustering``: 표준화 → 축군 가중치 → PCA 자동 판정 → K-Means → ``rule_label``
- ``snapshot``: 위 둘을 묶어 ``SnapshotRecord`` 조립 (I/O 없음 — 저장은 호출부 소관)

원칙(`analysis/__init__` 에서 계승, `04-CLUSTERING` §3.2):
- 순수 함수·결정론 — 같은 입력 = 같은 군집(`random_state`·`n_init` 주입).
- 튜너블 하드코딩 금지 — 호출부가 `app.core.config.Settings` 에서 읽어 주입한다.
- 결측(None)은 0 으로 위장하지 않는다 — 비율은 `_raw=null` + `_denom` 을 함께 남기고,
  군집 입력에서만 대체값(관측치 평균·평활값)을 쓴다.
- **개인 단위 데이터는 여기서 끝난다** — `customerLabel` 이 실린 행은 스냅샷에만 저장되고
  `AnalysisContext`(LLM 입력)에는 군집 통계만 올라간다(`sop/context` 규약).

상품 축(`product.py`)은 만들지 않는다 — `03-FEATURES` §7 이 후속으로 분리했다.
"""
