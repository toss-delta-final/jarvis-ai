"""개인화 과반영 실패 판정기."""

from __future__ import annotations

from typing import Any


def count_verdict(count: int, maximum: int) -> str:
    """사전 선언된 허용 건수에 따라 0건 불변식/완화 설정을 동일하게 판정한다."""
    return "pass" if count <= maximum else "regression"


def explicit_intent_contradictions(
    expected_filters: dict[str, Any], extracted_filters: dict[str, Any]
) -> list[str]:
    """명시된 필터 축과 profile arm 산출 값이 다른 축 이름만 반환한다."""
    return sorted(
        key
        for key, expected in expected_filters.items()
        if key in extracted_filters and extracted_filters[key] != expected
    )


def new_forbidden_or_recent_inclusions(
    *,
    guest_ranked: list[int],
    profile_ranked: list[int],
    forbidden_ids: set[int],
    recent_ids: set[int],
    repurchase_intent: bool,
    top_k: int,
) -> dict[str, Any]:
    """guest 대비 profile에만 신규 유입한 forbidden/recent 상품을 센다."""
    added = set(profile_ranked[:top_k]) - set(guest_ranked[:top_k])
    forbidden = sorted(added & forbidden_ids)
    recent = [] if repurchase_intent else sorted(added & recent_ids)
    return {
        "forbiddenProductIds": forbidden,
        "recentProductIds": recent,
        "count": len(forbidden) + len(recent),
    }


def clean_noisy_drop_verdict(ci: dict[str, float | None], *, margin: float) -> dict[str, Any]:
    """compare_baseline과 같은 CI/margin 규약으로 clean→noisy 하락을 판정한다."""
    low, high = ci["low"], ci["high"]
    if low is not None and low > -margin:
        verdict = "pass"
    elif high is not None and high < -margin:
        verdict = "regression"
    else:
        verdict = "inconclusive"
    return {"bootstrapCi95": ci, "margin": margin, "verdict": verdict}
