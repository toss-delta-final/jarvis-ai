"""워커별 `compute` 스텝 — 통계 판정 4종 + 원인·추천 후보 생성 (이슈 #594·#597).

공통 프레임(load → compare → compute → feedback → interpret) 중 **워커마다 다른 유일한
스텝**이다(`01` 결정 4). 여기 함수들의 공통 성질 셋:

- **LLM 0회 · Spring 0회 · DB 0회.** 이미 조회된 응답/레코드를 인자로 받는 순수 함수다.
  `features/snapshot.py` 가 *"I/O 가 없다 — Spring 조회도 save_snapshot 호출도 하지
  않는다"* 로 끊은 경계를 그대로 승계한다. 조회·배선은 `load` 스텝(후속 이슈) 소관이고,
  여기서 경계를 넘으면 단위 테스트가 Spring/DB 스텁을 요구하게 된다.
- **통계를 새로 구현하지 않는다.** `analysis.timeseries`(STL+GESD) ·
  `analysis.proportions`(Wilson·2-proportion z) · `features.clustering`(K-Means, 스냅샷
  생성 시 1회) 재사용뿐이다.
- **판정과 근거 수치를 함께 낸다.** `verdicts`·`segments` 만이 아니라 `metrics`·
  `comparisons` 까지 이 스텝이 채운다 — 판정에 쓴 수치가 같은 함수에서 같이 나와야
  검증층 F2(수치 근거 대조)의 허용 집합이 성립한다.

구성:
- ``behavior``      : 스냅샷 군집 → `ctx.segments` (전사 + 제외 판정)
- ``churn``         : 스냅샷 2개 → 명단 3분할 · 이동 행렬 · 순증감 · `delta_size`
- ``sales_anomaly`` : I-6 매출 시계열 → STL+GESD 이상 판정
- ``conversion``    : I-7 퍼널 2기간 → 단계별 2-proportion z
- ``causes``        : 판정 + I-15/I-14/I-31 → `ctx.causes` (원인 후보 7규칙)
- ``candidates``    : I-9/I-13 + 원인 후보 → `ctx.candidate_actions` (추천 후보 4슬롯)
- ``render``        : 위 결과를 LLM 이 읽는 표로 직렬화(JSON 덤프 금지 — `05` §2.2)
"""

from app.agents.seller.sop.compute.behavior import compute_behavior
from app.agents.seller.sop.compute.candidates import compute_candidates
from app.agents.seller.sop.compute.causes import compute_causes
from app.agents.seller.sop.compute.churn import compute_churn
from app.agents.seller.sop.compute.conversion import compute_conversion
from app.agents.seller.sop.compute.render import (
    VERDICT_TEXT,
    render_candidate_block,
    render_cause_block,
    render_rule_card_block,
    render_segment_block,
    render_shift_block,
)
from app.agents.seller.sop.compute.sales_anomaly import compute_sales_anomaly

__all__ = [
    "VERDICT_TEXT",
    "compute_behavior",
    "compute_candidates",
    "compute_causes",
    "compute_churn",
    "compute_conversion",
    "compute_sales_anomaly",
    "render_candidate_block",
    "render_cause_block",
    "render_rule_card_block",
    "render_segment_block",
    "render_shift_block",
]
