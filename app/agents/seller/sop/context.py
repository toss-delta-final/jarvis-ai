"""`AnalysisContext` — 상주 analysis 파이프라인에서 **LLM 이 보는 유일한 입력** (이슈 #589).

근거: `01-ARCHITECTURE.md` §4.3 / 감사 C-12.

SOP 스텝(load → compare → compute → feedback → interpret)이 이 컨테이너를 제자리에서
채우고, `interpret` 단계의 LLM 은 **오직 이 객체만** 본다. 원시 rows·개인 단위 데이터는
여기 들어오지 않는다(customerLabel 재식별 금지 — 스냅샷 생성 코드만 소비하고 ctx 에는
`segments` 통계만 남는다).

[불변 규약 — LLM 수치 계산·판정 번복 금지]
`verdicts`·`comparisons`·`causes`·`candidate_actions` 는 **전부 코드가 만든다**. LLM 이
채우는 필드는 `Segment.llm_label`·`Segment.llm_desc` 둘뿐이다(보고서 표기 전용).

[결측 표기 규약]
`Metric.value=None` 은 "미집계·결측"이다 — 0 으로 위장하지 않는다(`analysis/__init__` 원칙
승계). 판정이 불가능한 경우는 `Verdict.verdict="undecided"` 로 남기고, 스텝 자체가 실패한
경우는 `Hold` 로 남긴다. **"판정 보류" 는 "이상 없음" 이 아니다.**

[검증층 연결 계약 — `verifier.py` 무접촉]
`verifier.run_finding_checks(finding, tool_outputs, expected_type=…)` 의 `tool_outputs`
자리에 ctx 직렬화본을 넣으면 F1~F3 가 그대로 돈다. 다만 그 인자 타입이 `Sequence[str]`
이므로 **ctx 를 객체째로 넣을 수 없다** — `list[str]` 직렬화 헬퍼가 필요하다(후속 이슈).
F2(`check_evidence_grounded`)는 문자열의 숫자 토큰 합집합을 허용 집합으로 쓰므로, 직렬화본에
수치가 문자열로 등장하기만 하면 검사가 성립한다.
⚠️ `verifier._MIN_SIGNIFICANT_DIGITS = 3` 이라 **100 미만 값은 애초에 검사 대상이 아니다**
(세그먼트 size·평점·리뷰 수 계열). 이 사각은 이 이슈에서 손대지 않는다.

스키마 종류(와이어 아님 — snake_case `BaseModel`, `schemas.py` 규약 승계):
`Metric` · `Verdict` · `Comparison` · `Segment` · `CauseCandidate` · `ProductFlag` ·
`PastAction` · `Hold` · `AnalysisContext`
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.seller.schemas import AnalysisType

# 판정 어휘 4종 — 🔴 `undecided` 는 **신규 값**이다(감사 C-12).
# 기존 `analysis/types.RateComparison.verdict` 는 3종(drop/rise/no_change)뿐이라
# 통계 모듈이 이 값을 반환하지 않는다 → SOP `compute` 스텝이 직접 채워야 한다.
VerdictValue = Literal[
    "significant_drop",
    "significant_rise",
    "no_significant_change",
    "undecided",
]


class Metric(BaseModel):
    """지표 1건 — `load` 스텝 산출.

    `value=None` 은 결측(미집계)이다. 0 으로 위장하면 LLM 이 "0 원" 을 사실로 서술한다.
    `source` 는 원천 표기(`"I-6"` · `"snapshot"` · `"calc"`)로, 보고서가 근거를 밝히는 재료다.
    """

    key: str
    value: float | None
    unit: str
    source: str
    period_from: date
    period_to: date


class Verdict(BaseModel):
    """통계 판정 1건 — `compute` 스텝 산출. **LLM 번복 금지.**

    `method` 는 판정에 쓴 기법 표기(`"stl_gesd"` · `"two_proportion_z"` · `"mad"`)다.
    어휘가 워커 추가와 함께 늘 예정이라 `Literal` 로 조이지 않는다 — 판정 어휘(`verdict`)
    만 닫아 두면 "LLM 이 새 판정을 지어낼 수 없다" 는 목적은 달성된다.
    `p_value=None` 은 검정을 돌리지 않았다는 뜻이다(표본 부족 등 → `verdict="undecided"`).
    """

    key: str
    verdict: VerdictValue
    method: str
    p_value: float | None = None
    detail: dict[str, float] = Field(default_factory=dict)


class Comparison(BaseModel):
    """이전 기간 비교 1건 — `compare` 스텝 산출. **델타는 코드가 계산한다.**

    `delta_pct=None` 은 정의 불가(기준값 0 이하)다 — 0% 로 위장하지 않는다(#194 계승).
    """

    key: str
    current: float
    baseline: float
    delta_pct: float | None
    baseline_from: date
    baseline_to: date


class Segment(BaseModel):
    """고객 세그먼트 1건 — `compute` 스텝(스냅샷의 K-Means 결과 읽기) 산출.

    `rule_label` 은 조인 키(원형: 충성/탐색/구매망설임/이탈위험/휴면)이고,
    `display_label` 은 라벨 중복 시 번호가 붙은 표시용이다(`04` 결정 28a).
    **`llm_label`·`llm_desc` 만 LLM 이 채운다** — 크기·통계는 전부 코드 소유다.

    `amount_distribution` 은 금액 구간명 → 군집 내 비율(0~1)이다(이슈 #594). 평균을
    쓰지 않는 이유: `centroid_stats["amountOrdinal"]` 은 구간 서수의 평균이라
    "평균 3.2번째 구간" 같은 해석 불가 문장을 낳는다 — `05` §2.2 가 "금액은 구간
    이름이 아니라 분포로 넘긴다"고 정한 값의 저장 자리다. 키는
    `features.spec.AMOUNT_BUCKET_ORDER` 순서이고 매핑 밖 구간은 `"UNKNOWN"` 이다.
    """

    rule_label: str
    display_label: str = ""
    size: int
    centroid_stats: dict[str, float] = Field(default_factory=dict)
    ratio_to_mean: dict[str, float] = Field(default_factory=dict)
    flag_ratios: dict[str, float] = Field(default_factory=dict)
    amount_distribution: dict[str, float] = Field(default_factory=dict)
    delta_size: int | None = None
    llm_label: str = ""
    llm_desc: str = ""


class CauseCandidate(BaseModel):
    """원인 후보 1건 — 코드가 만든다(`06` 결정 37). LLM 은 이 목록 밖의 원인을 쓸 수 없다.

    `event_kind` 는 후보 생성기(후속 이슈)가 어휘를 확정할 때까지 `str` 로 열어 둔다 —
    `06` §2.2 가 7종(`price_change`·`stock_out`·`status_change`·`payment_failure`·
    `segment_shift`·`review_drop`·`past_action`)을 제안하나, 생성기가 없는 상태에서
    `Literal` 로 조이면 규칙 추가 때마다 스키마를 되돌려야 한다.

    `strength="temporal_only"` 는 **시간적 선후만 확인됐다**는 뜻이다 — 인과 단정 금지의
    근거이고, 보고서 C1(`cause_hedged`) 검사가 이 값을 본다.
    """

    target_key: str
    target_desc: str
    event_kind: str
    event_at: date
    event_desc: str
    lag_days: int
    strength: Literal["temporal_only", "correlated"]
    corroboration: str = ""


class ProductFlag(BaseModel):
    """상품 KPI 플래그 1건 — 상품 트랙(후속) 산출. v1 에서는 항상 빈 목록이다."""

    product_id: int
    flag: str
    evidence: dict[str, float] = Field(default_factory=dict)


class PastAction(BaseModel):
    """과거 적용 액션의 성과 1건 — `feedback` 스텝 산출(`required=False`).

    `significant=None` 은 아직 측정 창(+7일)이 차지 않았다는 뜻이다.
    """

    rec_id: str
    action_type: str
    target: str
    significant: bool | None = None
    delta: float | None = None


class Hold(BaseModel):
    """판정 보류 1건 — 스텝 실패를 흡수한 흔적(`engine.run_sop` 이 append 한다).

    **"판정 보류" 는 "이상 없음" 이 아니다.** 이 목록이 비지 않으면 보고서 본문에 한계를
    명시하고 R-1 `hasHolds` / R-2 `holds[]` 로 판매자에게 드러난다(`01` §8·§9).
    """

    step: str
    reason: str


class AnalysisContext(BaseModel):
    """워커 1종의 실행 상태 전부 — 스텝이 제자리에서 채운다(가변, `frozen` 아님).

    `worker` 는 `schemas.AnalysisType` 6종을 그대로 쓴다 — 라우팅·planner·finding 이
    공유하는 단일 출처라 여기서 새 어휘를 만들면 `analysis_type` 정합(F3)이 깨진다.
    """

    worker: AnalysisType
    brand_id: int
    period_from: date
    period_to: date
    metrics: list[Metric] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    causes: list[CauseCandidate] = Field(default_factory=list)
    candidate_actions: list[dict] = Field(default_factory=list)
    product_flags: list[ProductFlag] = Field(default_factory=list)
    past_actions: list[PastAction] = Field(default_factory=list)
    holds: list[Hold] = Field(default_factory=list)
