"""Wilson 신뢰구간 + two-proportion z-검정 — conversion·churn 공용 (이슈 #290).

근거(docs/worker-papers.md):
- Sismeiro & Bucklin 2004: 퍼널을 단계별 조건부 전환확률로 분해하는 프레임.
  세션 공변량이 계약(I-7)에 없어 로지스틱 회귀는 "비율 추정 + 검정" 축약형과
  동치다(공변량 없는 로지스틱의 MLE = 표본 비율) — 축약 근거는 설계 문서 참조.
- Wilson CI·z-검정 자체는 표준 통계 기법(Wilson 1927, 교과서 수준)이라 별도
  논문 인용 없이 쓴다. 채택 이유: 구 drop_pct 단순 임계는 표본 크기를 무시해
  저볼륨(view 10건)에서 오탐했다 — 신뢰구간·유의성이 그 오탐을 통제한다.

순수 함수·결정론(§10-②). 튜너블(confidence·alpha)은 전부 호출부 주입.
"""

from __future__ import annotations

import math

from app.agents.seller.analysis.types import CountComparison, RateComparison, RateEstimate

_VERDICT_DROP = "significant_drop"
_VERDICT_RISE = "significant_rise"
_VERDICT_NONE = "no_significant_change"


def _normal_ppf(p: float) -> float:
    """표준정규 분위수 — scipy 위임(지연 임포트: 모듈 로드 비용 절약)."""
    from scipy.stats import norm

    return float(norm.ppf(p))


def wilson_interval(successes: int, trials: int, *, confidence: float) -> RateEstimate:
    """Wilson score 신뢰구간을 붙인 비율 추정.

    Wald 구간이 아니라 Wilson 을 쓰는 이유: 저볼륨·극단 비율(0%·100%)에서도
    구간이 [0,1] 을 벗어나거나 폭이 0 으로 붕괴하지 않는다 — 저볼륨 오탐 통제가
    이 교체(#290)의 목적이라 경계 케이스 건전성이 필수다.

    trials=0 은 비율 자체가 정의 불가 — ValueError. 호출부는 생성을 생략하고
    "미집계/표본 없음"으로 표기한다(0% 위장 금지 원칙).
    """
    if trials < 1:
        raise ValueError(f"trials 는 1 이상이어야 한다(got {trials}) — 표본 0 은 판정 보류")
    if not 0 <= successes <= trials:
        raise ValueError(f"successes({successes})가 [0, trials={trials}] 밖이다")
    z = _normal_ppf(1.0 - (1.0 - confidence) / 2.0)
    p_hat = successes / trials
    denom = 1.0 + z**2 / trials
    center = (p_hat + z**2 / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / trials + z**2 / (4 * trials**2))
    return RateEstimate(
        successes=successes,
        trials=trials,
        rate=p_hat,
        ci_low=max(0.0, center - margin),
        ci_high=min(1.0, center + margin),
        confidence=confidence,
    )


def compare_rates(
    current_successes: int,
    current_trials: int,
    baseline_successes: int,
    baseline_trials: int,
    *,
    alpha: float,
    confidence: float,
) -> RateComparison:
    """두 기간 비율 비교 — pooled two-proportion z-검정(양측).

    verdict 는 p<alpha 일 때만 방향(significant_drop/rise)을 갖는다 — 그 외는
    no_significant_change 로, 워커 LLM 이 "유의하지 않은 변동"을 하락 서사로
    확대하지 않게 하는 근거 수치(p_value·alpha)를 함께 보존한다.

    pooled 비율이 0 또는 1 이면(두 기간 모두 전무/전부) 분산이 0 이라 z 가 정의되지
    않지만, 그 경우 두 표본 비율은 동일하므로 p=1.0(변화 없음)으로 처리한다.
    """
    current = wilson_interval(current_successes, current_trials, confidence=confidence)
    baseline = wilson_interval(baseline_successes, baseline_trials, confidence=confidence)
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha 는 (0, 1) 구간이어야 한다(got {alpha})")

    pooled = (current_successes + baseline_successes) / (current_trials + baseline_trials)
    if pooled in (0.0, 1.0):
        p_value = 1.0
    else:
        se = math.sqrt(pooled * (1 - pooled) * (1 / current_trials + 1 / baseline_trials))
        z_stat = (current.rate - baseline.rate) / se
        from scipy.stats import norm

        p_value = float(2.0 * norm.sf(abs(z_stat)))

    if p_value < alpha:
        verdict = _VERDICT_DROP if current.rate < baseline.rate else _VERDICT_RISE
    else:
        verdict = _VERDICT_NONE
    return RateComparison(
        current=current, baseline=baseline, p_value=p_value, alpha=alpha, verdict=verdict
    )


def compare_counts(
    current: int,
    baseline_total: int,
    baseline_days: int,
    *,
    alpha: float,
    current_days: int = 1,
) -> CountComparison:
    """두 기간 **카운트** 비교 — 포아송 강도 동일성의 조건부 이항 정확검정(양측, 이슈 #595).

    두 기간의 발생 강도가 같다는 귀무가설 아래에서, 총합을 조건으로 걸면 현재 구간의
    건수는 이항분포를 따른다 — X_cur | (X_cur + X_base) ~ Binom(n, C/(C+D)), C 는 현재
    구간 일수, D 는 기준 구간 일수. 정확검정이라 하루 2~3건짜리 저빈도(신규 가입)에서도
    정규근사가 무너지지 않는다(2-proportion z 는 그 구간에서 신뢰할 수 없다).

    소비처 둘의 구간이 다르다 — 매출 트리거는 7일 대 7일, 신규 고객은 1일 대 7일이다.

    [왜 compare_rates 가 아닌가 — 실측] 트리거 5(신규 고객)의 원천 I-8 브랜드 스코프는
    ``groupBy=eventType`` 에서 ``{key, count}`` 버킷만 준다. **비율의 분모가 계약에
    없다.** 없는 분모를 "계정 이벤트 총량"으로 대신 채우면 잰 것이 "가입 건수의 감소"가
    아니라 "가입 비중의 변화"가 되어(로그인이 늘어도 발동한다) 지표 이름과 뜻이 어긋난다.
    계약이 논문/기존 기법을 못 받쳐줄 때의 처리 선례(축약·재정의 후 근거를 남긴다)를
    따라, 여기서는 **검정을 카운트 데이터에 맞게 교체**하고 그 근거를 이 docstring 에 남긴다.

    ⚠️ I-8 은 zero-fill 하지 않는다 — 발생이 0 인 날은 버킷 자체가 응답에 없다. 그
    "버킷 없음"을 0 으로 번역해도 안전한 이유는 코호트 쿼리 자체는 항상 돌고 버킷만
    안 실리는 것이라, 결측(미집계)이 아니라 실측 0 이기 때문이다. 번역은 호출부 소관이다.

    ``baseline_total=0`` 은 **거절하지 않는다.** 조건부 이항에서 기준 구간 발생이 0 이면
    관측 전부가 현재 구간에 몰린 것이라 p = 2×0.5^n 으로 검정이 성립한다 — 무매출 이력
    직후 매출 발생이 정확히 그 경우다(`timeseries` 가 sigma=inf 로 다루던 케이스와 같은
    사건). 다만 ``rate_ratio`` 는 정의 불가라 **None** 이다(0 나눗셈을 1.0 으로 위장하지
    않는다). 두 구간 모두 0 일 때만 ValueError 로 거절한다 — 그때는 변화 자체가 없다.
    """
    if current < 0 or baseline_total < 0:
        raise ValueError(f"카운트는 음수일 수 없다(current={current}, baseline={baseline_total})")
    if baseline_days < 1 or current_days < 1:
        raise ValueError(
            f"구간 일수는 1 이상이어야 한다(current={current_days}, baseline={baseline_days})"
        )
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha 는 (0, 1) 구간이어야 한다(got {alpha})")
    if current == 0 and baseline_total == 0:
        raise ValueError("두 구간 모두 발생이 0 이라 변화가 정의되지 않는다 — 판정 보류")
    expected = baseline_total / baseline_days * current_days

    from scipy.stats import binomtest

    total = current + baseline_total
    p_value = float(
        binomtest(
            current,
            total,
            current_days / (current_days + baseline_days),
            alternative="two-sided",
        ).pvalue
    )
    if p_value < alpha:
        verdict = _VERDICT_DROP if current < expected else _VERDICT_RISE
    else:
        verdict = _VERDICT_NONE
    return CountComparison(
        current=current,
        current_days=current_days,
        baseline_total=baseline_total,
        baseline_days=baseline_days,
        expected=expected,
        rate_ratio=(current / expected if expected > 0 else None),
        p_value=p_value,
        alpha=alpha,
        verdict=verdict,
    )
