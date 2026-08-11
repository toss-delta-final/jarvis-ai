"""`AnalysisContext` — 상주 analysis 파이프라인에서 **LLM 이 보는 유일한 입력** (이슈 #589).

근거: `01-ARCHITECTURE.md` §4.3 / 감사 C-12.

SOP 스텝(load → compare → compute → feedback → interpret)이 이 컨테이너를 제자리에서
채우고, `interpret` 단계의 LLM 은 **오직 이 객체만** 본다. 원시 rows·개인 단위 데이터는
여기 들어오지 않는다(customerLabel 재식별 금지 — 스냅샷 생성 코드만 소비하고 ctx 에는
`segments` 통계만 남는다).

[불변 규약 — LLM 수치 계산·판정 번복 금지]
`verdicts`·`comparisons`·`causes`·`candidate_actions`·`rule_cards` 는 **전부 코드가
만든다**. LLM 이 채우는 필드는 `Segment.llm_label`·`Segment.llm_desc` 둘뿐이다
(보고서 표기 전용).

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
`Metric` · `Verdict` · `Comparison` · `Segment` · `CauseCandidate` · `CandidateChange` ·
`ActionCandidate` · `FiredRuleCard` · `ProductFlag` · `PastAction` · `Hold` ·
`AnalysisContext`
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.seller.schemas import AnalysisFinding, AnalysisType, ProductField

# 판정 어휘 4종 — 🔴 `undecided` 는 **신규 값**이다(감사 C-12).
# 기존 `analysis/types.RateComparison.verdict` 는 3종(drop/rise/no_change)뿐이라
# 통계 모듈이 이 값을 반환하지 않는다 → SOP `compute` 스텝이 직접 채워야 한다.
VerdictValue = Literal[
    "significant_drop",
    "significant_rise",
    "no_significant_change",
    "undecided",
]

# 원인 후보 어휘 7종 (`06-REPORT.md` §2.3, 이슈 #597). `CauseCandidate.event_kind` 를
# `Literal` 로 조이지 않는 대신 생성기가 자기 출력을 이 집합으로 검증한다 — 규칙 추가는
# 여기 한 줄이고, 스키마를 되돌리지 않는다.
CAUSE_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "price_change",
        "stock_out",
        "status_change",
        "payment_failure",
        "segment_shift",
        "review_drop",
        "past_action",
    }
)


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

    `event_kind` 는 `Literal` 로 조이지 않는다 — 생성기(`compute/causes.py`)가
    `CAUSE_EVENT_KINDS` 로 자기 출력을 검증하므로, 규칙이 늘어도 스키마를 되돌리지
    않는다(`06` §2.2 가 제안한 7종이 그 집합이다).

    `strength="temporal_only"` 는 **시간적 선후만 확인됐다**는 뜻이다 — 인과 단정 금지의
    근거이고, 보고서 C1(`cause_hedged`) 검사가 이 값을 본다.

    `product_id` 는 이벤트가 특정 상품에서 났을 때만 채운다(#597). `06` §2.2 스키마에는
    없던 필드인데, 같은 문서 §3.2 슬롯 3(가격 롤백)이 *"규칙 1 후보가 correlated 로
    성립한 **상품**"* 을 대상으로 삼는다 — 후보와 상품을 잇는 키가 없으면 그 슬롯이
    성립하지 않는다. 문장(`event_desc`)에서 상품명을 되파싱하는 것보다 필드가 정직하다.
    """

    target_key: str
    target_desc: str
    event_kind: str
    event_at: date
    event_desc: str
    lag_days: int
    strength: Literal["temporal_only", "correlated"]
    corroboration: str = ""
    product_id: int | None = None


class CandidateChange(BaseModel):
    """추천 후보가 제안하는 개별 필드 변경 (`06` §3.1, 이슈 #597).

    `after` 를 문자열로 통일하는 것은 `schemas.ProposedChange` 규약 승계다. `before` 는
    ProposedChange 와 달리 **여기서는 싣는다** — 후보 근거 문장이 "재고 3건 → 30건"을
    보여줘야 하고, 그 값은 이미 I-9 조회로 손에 있다. 실행 시점 diff 는 여전히
    `hitl.validate_draft` 가 재조회로 다시 만든다(승인 전 stale 검증은 그쪽 소관).
    """

    field: ProductField
    before: str
    after: str


class ActionCandidate(BaseModel):
    """추천 후보 1건 — 코드가 만든다(`06` 결정 39). LLM 은 **선별·순서·문장화만** 한다.

    `action_type` 에 `order_fulfillment` 를 포함하되 v1 생성기는 만들지 않는다(`06` §3.4
    — `history.apply_recommendation` 이 `op="update"` 고정이라 "N번 적용" 경로를 못 탄다).
    `promotion` 도 어휘에는 남기되 생성하지 않는다(`06` 결정 40 — 실행 수단 0건).
    ⚠️ 이 어휘를 `schemas.ActionRecommendation` 으로 넓히지 않는다 — 그쪽 5종은 FE
    `SellerReportRecommendation.actionType` 과 **글자 단위로 같은 계약**이다.
    """

    slot: Literal["restock", "stockout", "price_rollback", "unhide"]
    action_type: Literal[
        "price_adjust",
        "description_update",
        "stock_adjust",
        "product_visibility",
        "promotion",
        "order_fulfillment",
    ]
    product_id: int
    product_name: str = ""
    changes: list[CandidateChange] = Field(default_factory=list)
    basis: str = ""
    cause_ref: str = ""


class FiredRuleCard(BaseModel):
    """조건이 걸린 rule card 1건 — `rule_cards.evaluate_rule_cards` 산출 (결정 115).

    레지스트리의 `RuleCard` 는 `condition` 이 `Callable` 이라 ctx 에 담을 수 없다
    (ctx 는 `model_dump()` 로 로깅·저장 경로를 탄다). **걸린 결과만** 직렬화 가능한
    형태로 옮겨 담는다 — LLM 은 어차피 걸린 카드만 본다(`12-EVAL` §2.2).

    `statement` 는 포맷이 끝난 완성 문장이고, 대입된 수치는 전부 ctx 에 이미 있는 값이라
    D2(수치 근거 대조) 허용 집합 안이다.
    """

    card_id: str
    scope: Literal["segment", "product", "brand"]
    subject: str = ""
    statement: str
    citation: str
    strength: Literal["definitional", "empirical"]


class ProductFlag(BaseModel):
    """상품 KPI 플래그 1건 — 상품 트랙(후속) 산출. v1 에서는 항상 빈 목록이다."""

    product_id: int
    flag: str
    evidence: dict[str, float] = Field(default_factory=dict)


class PastAction(BaseModel):
    """과거 적용 액션의 성과 1건 — `feedback` 스텝 산출(`required=False`).

    `significant=None` 은 아직 측정 창(+7일)이 차지 않았다는 뜻이다.
    `applied_at=None` 은 적용 시각을 모른다는 뜻이고, 그때 원인 규칙 7(`past_action`)은
    후보를 만들지 않는다 — `lag_days` 를 계산할 기준이 없기 때문이다(#597).
    """

    rec_id: str
    action_type: str
    target: str
    significant: bool | None = None
    delta: float | None = None
    applied_at: date | None = None


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
    candidate_actions: list[ActionCandidate] = Field(default_factory=list)
    rule_cards: list[FiredRuleCard] = Field(default_factory=list)
    product_flags: list[ProductFlag] = Field(default_factory=list)
    past_actions: list[PastAction] = Field(default_factory=list)
    holds: list[Hold] = Field(default_factory=list)
    # [이슈 #598] `interpret` 스텝 산출 — 채팅 레인 팬인(`run_branches`)의 `finding`과
    # 같은 타입이다. behavior 는 `BehaviorFinding`(서브클래스)을 담는다. `verify` 스텝이
    # 미달분을 이 자리에서 직접 강등(교체)한다 — 별도 "검증된 findings" 리스트를 두지
    # 않는 이유는 자리가 하나여야 상주 report 스텝이 이 필드 하나만 읽으면 되기 때문이다.
    findings: list[AnalysisFinding] = Field(default_factory=list)
