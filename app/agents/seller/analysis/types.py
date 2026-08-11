"""분석 계산 층 결과 타입 (이슈 #290, 「논문 기반 Worker 설계」 템플릿의 출력 스키마).

계산 모듈(timeseries/proportions/segmentation/outliers)이 워커 LLM 에게 넘기는
구조화 결과다. dataclass 로 고정하는 이유:
- 도구(tools.py)가 LLM 요약문을 만들 때 필드 누락·오타를 타입 수준에서 차단.
- analysis_judge(#242 F1~F3)가 수치 근거를 검증할 수 있도록 원수치를 보존.

[프록시 표기 규약 — 결측 파라미터 4단계 판정의 🔶 케이스]
논문 변수에 정확히 대응하는 계약 필드가 없어 의미 유사 필드로 근사한 값은
``basis`` 필드에 ``"proxy:<원천 필드>"`` 를 적는다(예: ``"proxy:sessions_30d"``).
도구 요약문은 basis 가 proxy 인 값에 "(근사)" 를 병기해 LLM 이 정밀값으로
과신하지 않게 한다. 직접 매핑(✅)·파생(⚠️)은 basis 를 비워 둔다(None).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeasonalAnomaly:
    """S-H-ESD 이상점 1건 (timeseries.detect_seasonal_anomalies 출력).

    expected 는 STL 재구성 기대값(추세+계절), deviation_pct 는 계절조정 편차(%) —
    expected 가 0 이하면 정의 불가라 None 이다(0 나눗셈 위장 금지, #194 계승).
    sigma 는 잔차의 robust z(|residual - median| / (1.4826×MAD)) 크기다 — MAD=0 인
    극단 수열은 MeanAD 폴백으로 표준화하고, 그마저 0 인 방어 경로만 math.inf 다.
    direction 은 "drop" | "spike" — LLM 프롬프트가 급락/급증 어휘로 옮긴다.
    """

    date: str
    actual: float
    expected: float
    deviation_pct: float | None
    sigma: float
    direction: str


@dataclass(frozen=True)
class SeasonalAnomalyDetection:
    """detect_seasonal_anomalies 출력 — 이상 목록 + **판정 가능 여부** (#512).

    빈 목록 하나가 "이상 없음"과 "판정 보류(표본 부족)"를 동시에 뜻하던 모호성을
    타입으로 가른다. 호출부는 ``anomalies`` 길이가 아니라 ``decided`` 로 먼저 분기해야
    한다 — 표본 2개짜리 확정적 all-clear 가 판매자에게 나가던 경로를 막는다.

    ``seasonal_adjusted`` 는 STL 계절조정 분기를 실제로 탔는지다. 호출부가
    ``len(values) >= min_history_for_stl`` 을 재계산하면 모듈 내부 임계와 조용히
    어긋날 수 있어 판정 주체가 직접 알린다.
    """

    anomalies: list[SeasonalAnomaly]
    decided: bool
    sample_size: int
    min_samples: int
    seasonal_adjusted: bool


@dataclass(frozen=True)
class RateEstimate:
    """비율 추정 1건 — Wilson 신뢰구간 부착 (proportions.wilson_interval).

    successes/trials 원수치를 보존한다(judge 검증·저볼륨 판독용).
    trials=0 이면 rate/ci 는 정의 불가라 호출부가 생성 자체를 생략한다(0% 위장 금지).
    """

    successes: int
    trials: int
    rate: float
    ci_low: float
    ci_high: float
    confidence: float


@dataclass(frozen=True)
class RateComparison:
    """두 기간 비율 비교 1건 — two-proportion z-검정 (proportions.compare_rates).

    verdict 는 "significant_drop" | "significant_rise" | "no_significant_change".
    p_value 와 alpha 를 함께 보존해 LLM 이 "유의하지 않음"을 수치 근거로 서술한다.
    """

    current: RateEstimate
    baseline: RateEstimate
    p_value: float
    alpha: float
    verdict: str


@dataclass(frozen=True)
class ClusterSummary:
    """행동 군집 1건 (segmentation.cluster_products 출력).

    label 은 규칙 라벨링 결과(카트이탈형/구경형/전환직결형 등 — Moe 2003 유형론 어휘),
    centroid 는 표준화 이전 원 피처 공간의 중심값(피처명→값), silhouette 는
    선택된 k 전체의 실루엣 점수(군집별이 아니라 분할 전체 품질)다.
    """

    label: str
    product_ids: list[int]
    size: int
    centroid: dict[str, float]
    silhouette: float


@dataclass(frozen=True)
class OutlierFlag:
    """abuse 3-트랙 이상 1건 (outliers/timeseries 산출의 공통 표현).

    type 은 Chandola 2009 유형 체계 매핑: "point" | "contextual" | "collective".
    target 은 대상 식별(날짜/상품 id/IP 등 문자열화), metric·value·threshold 는
    판정 수치 3요소다. normal_explanations 는 오탐 통제 — 예: 스파이크일이 가격
    변경일과 겹치면 "가격 인하(변경 이력 N건)와 겹침" 을 담아 LLM 이 봇 단정
    대신 "설명 가능" 분류를 선택할 근거를 준다.
    """

    type: str
    target: str
    metric: str
    value: float
    threshold: float
    normal_explanations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProxyValue:
    """🔶 프록시 근사 수치 1건 — basis 필수 (모듈 공통).

    예: behavior 의 R 근사 = ProxyValue(name="recency_days", value=12.0,
    basis="proxy:last_activity_at") — 활동≠구매라는 한계는 설계 문서에,
    표기는 도구 요약문에 "(근사)" 로 드러난다.
    """

    name: str
    value: float
    basis: str


@dataclass(frozen=True)
class CountComparison:
    """두 기간 **카운트** 비교 1건 — 포아송 비율 검정 (proportions.compare_counts).

    비율(성공/시행)이 아니라 발생 건수만 있는 지표 전용이다 — 트리거 5(신규 고객)의
    원천 I-8 은 groupBy=eventType 에서 ``{key, count}`` 버킷만 주고 분모가 없다.

    ``expected`` 는 현재 구간 길이로 환산한 기대 건수
    (= baseline_total / baseline_days × current_days)이고, ``rate_ratio`` 는
    current / expected 다 — expected 가 0 이면 정의 불가라 호출부가 생성 자체를
    생략한다(0 나눗셈 위장 금지, #194 계승).
    ``current_days`` 가 있는 이유: 매출 트리거는 7일 합끼리, 신규 고객은 1일 대 7일로
    재기 때문이다. 구간 길이를 인자로 받지 않으면 검정이 조용히 틀린다.
    verdict 어휘는 RateComparison 과 같다(significant_drop | significant_rise |
    no_significant_change) — 소비처가 두 검정을 같은 분기로 다룰 수 있어야 한다.
    """

    current: int
    current_days: int
    baseline_total: int
    baseline_days: int
    expected: float
    rate_ratio: float | None
    p_value: float
    alpha: float
    verdict: str


@dataclass(frozen=True)
class TriggerEvaluation:
    """트리거 1종 판정 — **고정 임계 AND 통계 유의** (이슈 #595, `10-TRIGGER` 결정 94).

    ``decided=False`` 는 판정 보류다 — **"이상 없음"이 아니다.** 빈 이상 목록 하나가
    두 뜻을 겸하던 모호성을 타입으로 가른 `SeasonalAnomalyDetection.decided`(#512)의
    규약을 트리거 층으로 올린 것이다. 보류인데 ``fired=False`` 하나만 보고 넘어가면
    "표본이 부족해 못 봤다"가 "봤는데 정상이다"로 둔갑한다.

    ``threshold_met``·``significant`` 를 각각 보존하는 이유는 AND 의 어느 쪽이 막았는지가
    임계 조정의 근거이기 때문이다 — null 시뮬레이션 리포트가 이 두 값을 따로 센다.
    ``significant=None`` 은 검정을 돌리지 않았다는 뜻이다(트리거 7 은 BE 판정을 그대로
    쓰므로 상시 None — 결정 103).

    ``change_unit`` 은 ``change`` 의 단위다: ``"pct"``(상대 변화율, 0.05=5%) ·
    ``"pp"``(퍼센트포인트 차, 0.10=10%p) · ``"count"``(건수 차). 매출·전환율·상품
    판매량은 상대 변화율이고 장바구니 이탈률·재구매율은 퍼센트포인트다 —
    `10-TRIGGER` §3.2 표의 단위를 그대로 옮겼으므로 섞어 쓰면 임계 의미가 바뀐다.
    ``change=None`` 은 정의 불가(기준값 0 이하)다 — 0 으로 위장하지 않는다.
    """

    trigger: str
    tier: int
    fired: bool
    decided: bool
    threshold_met: bool | None
    significant: bool | None
    change: float | None
    change_unit: str
    threshold: float
    method: str
    direction: str | None = None
    p_value: float | None = None
    alpha: float | None = None
    basis: str = ""
    detail: dict[str, float] = field(default_factory=dict)
    hold_reason: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """스캔 1회 결과 — 티어1(문을 연다) + 티어2(서술 재료) (`10-TRIGGER` §4.2).

    ``opened`` 는 티어1 중 하나라도 발동했는가다 — **이 값이 곧 보고서 생성 여부**이고,
    null 시뮬레이션의 게이트 지표(tier1.openRate)도 이것을 센다. 티어2 발동은 발동
    조건이 아니라 보고서 1부를 두텁게 하는 재료라 ``opened`` 에 기여하지 않는다.
    """

    tier1: list[TriggerEvaluation] = field(default_factory=list)
    tier2: list[TriggerEvaluation] = field(default_factory=list)
    opened: bool = False
    fired: list[str] = field(default_factory=list)
