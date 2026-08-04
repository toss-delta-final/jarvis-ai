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

    expected 는 STL 재구성 기대값(추세+계절), deviation_pct 는 계절조정 편차(%),
    sigma 는 잔차의 robust z(|residual - median| / MAD 기준) 크기다.
    direction 은 "drop" | "spike" — LLM 프롬프트가 급락/급증 어휘로 옮긴다.
    """

    date: str
    actual: float
    expected: float
    deviation_pct: float
    sigma: float
    direction: str


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
