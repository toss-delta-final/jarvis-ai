"""`causes` 생성기 — 원인 후보 7규칙 (이슈 #597, `06-REPORT.md` §2).

"왜 그랬을까"를 LLM 에 자유롭게 물으면 **그럴듯한 서사**가 나온다 — 마케팅 축소·경쟁사
프로모션·계절 요인은 전부 우리 데이터에 축이 없는 것들인데, 숫자를 안 쓰고 지어내면
D2(수치 근거 대조)에 아무것도 걸리지 않는다(`06` §2.1). 그래서 원인도 계산으로 내린다 —
코드가 "언제 무엇이 있었고 지표 변화보다 앞섰는가"를 판정해 후보 목록을 만들고, LLM 은
그 목록에서 골라 서술만 한다. **LLM 0회.**

[이 모듈이 쓰는 실측 원천 — 문서와 다른 곳]
- 규칙 1~3: I-15 `ProductChangeLogRow`. 시각 필드는 `created_at` 이다(`changedAt` 아님).
  STOCK 행에 `optionId` 가 **응답에 없어**(BE SELECT 미투영) 품절은 상품 단위로만 본다.
- 규칙 4: `06` §2.3 의 "I-14 B41" 은 실재하지 않는 지표 ID다. I-14 `by_status` 는 기간
  집계라 이벤트 일자가 없으므로 **목록 모드 rows(`createdAt`·`toStatus`)를 일자별로
  버킷팅**해 급증일을 찾는다(사용자 확정, 2026-08-11).
- 규칙 6: 같은 이유로 "I-31 B72" 도 없다. `SellerReviewStats.distribution` 은 기간
  집계뿐이라 **목록 모드 rows(`createdAt`·`rating`)를 일자별로 버킷팅**한다.
- 규칙 5: ctx 내부(`churn` compute 가 만든 `segment_size:*` 판정)만 쓴다. 외부 조회 0회.

[설명 대상(target)을 고르는 방식]
`06` §2.3 의 "대상" 열은 서술이고, 코드가 참조할 수 있는 실체는 **그 워커의 유의 판정**
뿐이다(조회수 하락 같은 전용 verdict 는 아직 없다). 그래서 대상 = `ctx.verdicts` 중
`significant_drop`/`significant_rise` 전부로 두고, 규칙마다 적용 워커를 `_RULE_WORKERS`
로 좁힌다. 규칙 5 만은 이벤트 자신이 `segment_size:*` 판정이라 대상에서 제외한다.

[지표 변화일]
verdict key 끝에 ISO 날짜가 붙어 있으면(`sales_anomaly:2026-08-09`) 그 날, 없으면
`ctx.period_to`(사용자 확정, 2026-08-11). 기간 집계 지표는 변화일이 하루로 특정되지
않으므로 기간 종료일을 기준으로 삼는다.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta

from app.agents.seller.analysis import proportions
from app.agents.seller.features import spec
from app.agents.seller.sop.compute.render import VERDICT_TEXT
from app.agents.seller.sop.context import (
    CAUSE_EVENT_KINDS,
    AnalysisContext,
    CauseCandidate,
    Verdict,
)
from app.core.config import Settings
from app.schemas.spring import (
    ChurnResult,
    OrderEventsResult,
    ProductChangeLogResult,
    SellerReviewList,
)

_DECISIVE = frozenset({"significant_drop", "significant_rise"})

# 규칙별 적용 워커. 워커가 다르면 후보를 만들지 않는다 — ctx 는 워커 1종의 상태이고,
# 매출 이상을 설명할 이벤트를 churn ctx 에 달면 보고서에서 대상이 어긋난다.
_RULE_WORKERS: dict[str, frozenset[str]] = {
    "price_change": frozenset({"sales_anomaly", "conversion"}),
    "stock_out": frozenset({"sales_anomaly", "conversion"}),
    "status_change": frozenset({"sales_anomaly", "conversion", "behavior"}),
    "payment_failure": frozenset({"conversion"}),
    "segment_shift": frozenset({"churn"}),
    "review_drop": frozenset({"conversion", "review"}),
    "past_action": frozenset({"sales_anomaly", "conversion", "behavior", "churn", "review"}),
}

# 대상 문장에 쓰는 지표 이름 — 키를 그대로 노출하면 보고서에 필드명이 흘러나온다.
_KEY_LABELS: dict[str, str] = {
    "conversion:view_to_cart": "조회→담기 전환율",
    "conversion:cart_to_checkout": "담기→결제 시작 전환율",
    "conversion:checkout_to_purchase": "결제 시작→구매 전환율",
    "churn_rate": "이탈률",
}

_SEGMENT_PREFIX = "segment_size:"

# 저평점 판정 구간 — `tools._REVIEW_NEGATIVE_STARS` 와 같은 1~2점이다.
_LOW_RATING_MAX = 2


def parse_event_date(value: str | None) -> date | None:
    """ISO8601(오프셋 포함) 또는 `YYYY-MM-DD` → date. 해석 불가는 None(폐기)."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_wire_int(value: str | None) -> int | None:
    """I-15 는 PRICE·STOCK 도 문자열로 내려보낸다(BE Java String) — 숫자만 받아들인다."""
    if value is None:
        return None
    try:
        return int(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _day(value: date) -> str:
    return f"{value.month}월 {value.day}일"


def _target_date(ctx: AnalysisContext, verdict: Verdict) -> date:
    _, separator, tail = verdict.key.rpartition(":")
    if separator:
        parsed = parse_event_date(tail)
        if parsed is not None:
            return parsed
    return ctx.period_to


def _fmt_value(key: str, value: float) -> str:
    if key.startswith(_SEGMENT_PREFIX):
        return f"{int(round(value)):,}명"
    if key.startswith("conversion:") or key == "churn_rate":
        return f"{value:.3f}"
    return f"{value:,.0f}"


def _target_desc(ctx: AnalysisContext, verdict: Verdict) -> str:
    """1부 문장과 같은 표현으로 대상을 적는다(`06` §1 — 2부는 재인용만 한다)."""
    text = VERDICT_TEXT.get(verdict.verdict, verdict.verdict)
    tail = f", p={verdict.p_value:.3f}" if verdict.p_value is not None else ""

    if verdict.key.startswith("sales_anomaly"):
        day = _target_date(ctx, verdict)
        head = f"{_day(day)} 매출"
        actual = verdict.detail.get("actual")
        if actual is not None:
            head += f" {actual:,.0f}원"
        deviation = verdict.detail.get("deviation_pct")
        if deviation is not None:
            head += f" (계절조정 기대 대비 {deviation:+.1f}%)"
        return f"{head} — {text}{tail}"

    label = _KEY_LABELS.get(verdict.key)
    if label is None and verdict.key.startswith(_SEGMENT_PREFIX):
        label = f"{verdict.key[len(_SEGMENT_PREFIX) :]} 세그먼트 인원"
    label = label or verdict.key

    comparison = next((item for item in ctx.comparisons if item.key == verdict.key), None)
    if comparison is None:
        return f"{label} — {text}{tail}"
    before = _fmt_value(verdict.key, comparison.baseline)
    after = _fmt_value(verdict.key, comparison.current)
    return f"{label} {before} → {after} — {text}{tail}"


def _make(
    ctx: AnalysisContext,
    target: Verdict,
    *,
    event_kind: str,
    event_at: date,
    event_desc: str,
    settings: Settings,
    strength: str = "temporal_only",
    corroboration: str = "",
    product_id: int | None = None,
) -> CauseCandidate | None:
    """공통 가드를 통과한 후보 1건. 통과하지 못하면 None (`06` §2.3 공통 가드).

    `lag_days > 0` 만 남기는 것이 이 함수의 핵심이다 — 동시·후행 이벤트는 버린다.
    결과가 원인을 앞설 수 없기 때문이고, 이 한 줄이 없으면 "매출이 떨어진 날 가격을
    내렸다"가 "가격 때문에 떨어졌다"로 읽히는 후보가 그대로 LLM 에 넘어간다.
    """
    if event_kind not in CAUSE_EVENT_KINDS:  # 생성기 자기 검증(어휘 오타 차단)
        raise ValueError(f"알 수 없는 event_kind: {event_kind}")
    window = settings.seller_cause_window_days
    if event_at > ctx.period_to or event_at < ctx.period_from - timedelta(days=window):
        return None
    lag = (_target_date(ctx, target) - event_at).days
    if lag <= 0 or lag > window:
        return None
    return CauseCandidate(
        target_key=target.key,
        target_desc=_target_desc(ctx, target),
        event_kind=event_kind,
        event_at=event_at,
        event_desc=event_desc,
        lag_days=lag,
        strength="correlated" if strength == "correlated" else "temporal_only",
        corroboration=corroboration,
        product_id=product_id,
    )


def _price_exposure_note(churn: ChurnResult | None) -> str:
    """규칙 1 승격 근거 — I-16 `priceIncreaseExposed`.

    ⚠️ 이 값은 **브랜드 단위**다(특정 상품의 인상에 노출된 인원이 아니다). 그래서 문구도
    "해당 상품"이 아니라 "가격 인상 이후"로 적는다 — 상품 단위로 읽히면 대조 근거가
    실제보다 강해 보인다.
    """
    if churn is None or churn.pre_churn_signals is None:
        return ""
    exposed = int(churn.pre_churn_signals.price_increase_exposed or 0)
    if exposed <= 0:
        return ""
    cohort = int(churn.cohort_size or 0)
    if cohort <= 0:
        return f"이탈 회원 중 {exposed:,}명이 가격 인상 이후 상품을 조회했습니다"
    return (
        f"이탈 회원 {cohort:,}명 중 {exposed:,}명이 가격 인상 이후 상품을 조회했습니다"
        f" ({exposed / cohort:.1%})"
    )


def _from_change_logs(
    ctx: AnalysisContext,
    targets: Sequence[Verdict],
    change_logs: ProductChangeLogResult | None,
    churn: ChurnResult | None,
    settings: Settings,
) -> Iterator[CauseCandidate]:
    """규칙 1(가격 인상) · 2(품절) · 3(노출 중단) — 전부 I-15 한 원천이다."""
    if change_logs is None:
        return
    exposure = _price_exposure_note(churn)
    for row in change_logs.rows:
        event_at = parse_event_date(row.created_at)
        if event_at is None:
            continue
        name = row.product_name or f"상품 {row.product_id}"
        change_type = (row.change_type or "").upper()
        strength = "temporal_only"
        corroboration = ""

        if change_type == "PRICE":
            old = parse_wire_int(row.old_value)
            new = parse_wire_int(row.new_value)
            if old is None or new is None or new <= old:
                continue  # 인하·동결은 하락의 선행 원인 후보가 아니다
            event_kind = "price_change"
            event_desc = f"{_day(event_at)} {name} 가격 {old:,} → {new:,}원"
            if old > 0:
                event_desc += f" ({(new - old) / old * 100:+.1f}%)"
            if exposure:
                strength = "correlated"
                corroboration = exposure
        elif change_type == "STOCK":
            if parse_wire_int(row.new_value) != 0:
                continue
            old = parse_wire_int(row.old_value)
            event_kind = "stock_out"
            event_desc = f"{_day(event_at)} {name} 품절"
            if old is not None:
                event_desc += f" (재고 {old:,} → 0)"
        elif change_type == "STATUS":
            if (row.new_value or "").upper() != "HIDDEN":
                continue
            event_kind = "status_change"
            event_desc = f"{_day(event_at)} {name} 노출 중단(판매중 → 숨김)"
        else:
            continue

        if ctx.worker not in _RULE_WORKERS[event_kind]:
            continue
        for target in targets:
            candidate = _make(
                ctx,
                target,
                event_kind=event_kind,
                event_at=event_at,
                event_desc=event_desc,
                strength=strength,
                corroboration=corroboration,
                product_id=row.product_id,
                settings=settings,
            )
            if candidate is not None:
                yield candidate


def _spike_day(
    buckets: dict[date, list[int]], *, settings: Settings
) -> tuple[date, proportions.RateComparison] | None:
    """일자별 (사건 수, 시행 수) 중 **나머지 날 대비 유의하게 높은** 하루를 고른다.

    leave-one-out 2-proportion z 다 — 임의 임계("2배 이상") 대신 검정을 쓰는 이유는
    저볼륨에서 하루 2건이 100% 로 읽히는 오탐을 통제하기 위해서다(`proportions` 도입
    근거 그대로). 동률이면 p 가 작은 날을 고른다.
    """
    total_events = sum(slot[0] for slot in buckets.values())
    total_trials = sum(slot[1] for slot in buckets.values())
    best: tuple[date, proportions.RateComparison] | None = None
    for day in sorted(buckets):
        events, trials = buckets[day]
        rest_events = total_events - events
        rest_trials = total_trials - trials
        if trials < 1 or rest_trials < 1:
            continue
        try:
            comparison = proportions.compare_rates(
                events,
                trials,
                rest_events,
                rest_trials,
                alpha=settings.seller_rate_test_alpha,
                confidence=settings.seller_wilson_confidence,
            )
        except ValueError:
            continue
        if comparison.verdict != "significant_rise":
            continue
        if best is None or comparison.p_value < best[1].p_value:
            best = (day, comparison)
    return best


def _from_payment_failures(
    ctx: AnalysisContext,
    targets: Sequence[Verdict],
    order_events: OrderEventsResult | None,
    settings: Settings,
) -> Iterator[CauseCandidate]:
    """규칙 4 — 결제 실패율 급증 (I-14 목록 모드 rows 일자 버킷팅).

    분모를 `PAID + PAYMENT_FAILED` 로 두는 이유: `byStatus` 는 상태별로 층위가 다르고
    (SHIPPING·DELIVERED 는 아이템 수, PAID·PAYMENT_FAILED 는 주문 수) 섞으면 비율이
    성립하지 않는다(`spring.OrderEventsResult` 주 ④). 주문 단위 두 상태만 쓴다.
    """
    if order_events is None or ctx.worker not in _RULE_WORKERS["payment_failure"]:
        return
    buckets: dict[date, list[int]] = {}
    for row in order_events.rows:
        if not isinstance(row, dict):
            continue  # groupBy=memberId 응답(MemberRow)은 전이 행이 아니다
        day = parse_event_date(row.get("createdAt"))
        status = str(row.get("toStatus") or "").upper()
        if day is None or status not in ("PAID", "PAYMENT_FAILED"):
            continue
        slot = buckets.setdefault(day, [0, 0])
        slot[1] += 1
        if status == "PAYMENT_FAILED":
            slot[0] += 1

    spike = _spike_day(buckets, settings=settings)
    if spike is None:
        return
    day, comparison = spike
    event_desc = (
        f"{_day(day)} 결제 실패율 {comparison.baseline.rate:.1%} → {comparison.current.rate:.1%}"
        f" (같은 기간 나머지 날 대비)"
    )
    corroboration = (
        f"2-proportion z 검정 p={comparison.p_value:.3f}"
        f" (유의수준 {settings.seller_rate_test_alpha})"
    )
    for target in targets:
        candidate = _make(
            ctx,
            target,
            event_kind="payment_failure",
            event_at=day,
            event_desc=event_desc,
            strength="correlated",
            corroboration=corroboration,
            settings=settings,
        )
        if candidate is not None:
            yield candidate


def _from_reviews(
    ctx: AnalysisContext,
    targets: Sequence[Verdict],
    reviews: SellerReviewList | None,
    settings: Settings,
) -> Iterator[CauseCandidate]:
    """규칙 6 — 저평점(1~2점) 비중 급증 (I-31 목록 모드 rows 일자 버킷팅).

    검정으로 **탐지**하되 강도는 `temporal_only` 로 둔다(`06` §2.3 규칙 6 — 승격 조건
    없음). 리뷰가 늘었다는 사실과 전환이 떨어졌다는 사실을 코드가 대조한 적은 없다.
    """
    if reviews is None or ctx.worker not in _RULE_WORKERS["review_drop"]:
        return
    buckets: dict[date, list[int]] = {}
    for row in reviews.rows:
        day = parse_event_date(row.created_at)
        if day is None:
            continue
        slot = buckets.setdefault(day, [0, 0])
        slot[1] += 1
        if 1 <= row.rating <= _LOW_RATING_MAX:
            slot[0] += 1

    spike = _spike_day(buckets, settings=settings)
    if spike is None:
        return
    day, comparison = spike
    event_desc = (
        f"{_day(day)} 저평점(1~2점) 비중 {comparison.baseline.rate:.1%}"
        f" → {comparison.current.rate:.1%} (같은 기간 나머지 날 대비,"
        f" p={comparison.p_value:.3f})"
    )
    for target in targets:
        candidate = _make(
            ctx,
            target,
            event_kind="review_drop",
            event_at=day,
            event_desc=event_desc,
            settings=settings,
        )
        if candidate is not None:
            yield candidate


def _from_segment_shift(
    ctx: AnalysisContext, targets: Sequence[Verdict], settings: Settings
) -> Iterator[CauseCandidate]:
    """규칙 5 — 세그먼트 순유출(충성형↓ · 이탈위험형↑). 외부 조회 없이 ctx 만 본다.

    이벤트 일자는 **비교 스냅샷 다음 날**이다 — 이동이 두 스냅샷 사이 어딘가에서 일어났고,
    그 구간의 시작이 비교 기간 종료 직후이기 때문이다(사용자 확정, 2026-08-11).
    """
    if ctx.worker not in _RULE_WORKERS["segment_shift"]:
        return
    watched = {spec.LABEL_LOYAL: "significant_drop", spec.LABEL_AT_RISK: "significant_rise"}
    plain_targets = [target for target in targets if not target.key.startswith(_SEGMENT_PREFIX)]
    if not plain_targets:
        return

    for verdict in ctx.verdicts:
        if not verdict.key.startswith(_SEGMENT_PREFIX):
            continue
        label = verdict.key[len(_SEGMENT_PREFIX) :]
        # 라벨 중복 시 표시 라벨은 `탐색형(2)` 형태다(`04` 결정 28a) — 원형으로 되돌린다.
        if watched.get(label.partition("(")[0]) != verdict.verdict:
            continue
        comparison = next((item for item in ctx.comparisons if item.key == verdict.key), None)
        if comparison is None:
            continue
        delta = int(round(comparison.current)) - int(round(comparison.baseline))
        event_desc = (
            f"{label} 세그먼트 {int(round(comparison.baseline)):,}명"
            f" → {int(round(comparison.current)):,}명 ({delta:+,d}명)"
        )
        corroboration = (
            f"2-proportion z 검정 p={verdict.p_value:.3f}" if verdict.p_value is not None else ""
        )
        for target in plain_targets:
            candidate = _make(
                ctx,
                target,
                event_kind="segment_shift",
                event_at=comparison.baseline_to + timedelta(days=1),
                event_desc=event_desc,
                strength="correlated",
                corroboration=corroboration,
                settings=settings,
            )
            if candidate is not None:
                yield candidate


def _from_past_actions(
    ctx: AnalysisContext, targets: Sequence[Verdict], settings: Settings
) -> Iterator[CauseCandidate]:
    """규칙 7 — 과거 적용 액션. `applied_at` 이 없으면 lag 기준이 없어 후보가 아니다."""
    if ctx.worker not in _RULE_WORKERS["past_action"]:
        return
    for action in ctx.past_actions:
        if action.applied_at is None:
            continue
        event_desc = (
            f"{_day(action.applied_at)} 과거 추천 적용 — {action.action_type}"
            f" (대상 {action.target})"
        )
        for target in targets:
            candidate = _make(
                ctx,
                target,
                event_kind="past_action",
                event_at=action.applied_at,
                event_desc=event_desc,
                settings=settings,
            )
            if candidate is not None:
                yield candidate


def _sort_key(candidate: CauseCandidate) -> tuple:
    """lag 오름차순 · 동률은 correlated 우선 · 그다음 최신 이벤트 (`06` §2.3 공통 가드)."""
    return (
        candidate.lag_days,
        0 if candidate.strength == "correlated" else 1,
        -candidate.event_at.toordinal(),
        candidate.event_kind,
        candidate.target_key,
        candidate.product_id or 0,
    )


def compute_causes(
    ctx: AnalysisContext,
    *,
    change_logs: ProductChangeLogResult | None = None,
    churn_now: ChurnResult | None = None,
    order_events: OrderEventsResult | None = None,
    reviews: SellerReviewList | None = None,
    settings: Settings,
) -> None:
    """원인 후보 생성 — LLM 0회, Spring 0회, DB 0회 (`compute` 스텝 공통 성질).

    원천이 `None` 이면 그 규칙만 조용히 건너뛴다. 조회 실패 자체를 `Hold` 로 남기는 것은
    `load` 스텝 소관이라 여기서 중복해 남기지 않는다.

    **후보 0건도 결과다** — `ctx.causes` 를 빈 목록으로 두면
    `render.render_cause_block` 이 "원인 후보를 찾지 못했습니다"를 코드로 넣는다.
    """
    targets = [verdict for verdict in ctx.verdicts if verdict.verdict in _DECISIVE]
    if not targets:
        return

    found = [
        *_from_change_logs(ctx, targets, change_logs, churn_now, settings),
        *_from_payment_failures(ctx, targets, order_events, settings),
        *_from_reviews(ctx, targets, reviews, settings),
        *_from_segment_shift(ctx, targets, settings),
        *_from_past_actions(ctx, targets, settings),
    ]

    seen: set[tuple[str, str, int | None]] = set()
    for candidate in sorted(found, key=_sort_key):
        if len(ctx.causes) >= settings.seller_cause_max_candidates:
            break
        # 같은 (대상, 유형, 상품)은 1건만 — 정렬이 lag 오름차순이라 남는 건 최신 이벤트다.
        key = (candidate.target_key, candidate.event_kind, candidate.product_id)
        if key in seen:
            continue
        seen.add(key)
        ctx.causes.append(candidate)
