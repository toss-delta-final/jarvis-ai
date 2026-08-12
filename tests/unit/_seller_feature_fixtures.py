"""고객 피처·군집 테스트 공용 fixture 빌더 (이슈 #593).

`_` 접두어라 pytest 가 테스트로 수집하지 않는다(`tests/_fakes.py` 관행).
"""

from __future__ import annotations

from app.schemas.spring import SellerCustomerFeaturesResult

AMOUNT_BUCKETS = ["ZERO", "LT_10K", "10K_50K", "50K_100K", "100K_300K", "GTE_300K"]


def wire_row(index: int, **overrides) -> dict:
    """I-38 rows[] 1건(camelCase 와이어). BE 는 전 필드 primitive 라 null 이 없다."""
    row = {
        "customerLabel": f"L{index:04d}",
        "sessions": 1,
        "productViews": 1,
        "cartAdds": 0,
        "checkoutStarts": 0,
        "orderCount": 0,
        "cancelCount": 0,
        "amountBucket": "ZERO",
        "lastActivityDaysAgo": 10,
        "firstSeenDaysAgo": 100,
    }
    row.update(overrides)
    return row


def wire_result(rows: list[dict], **overrides) -> SellerCustomerFeaturesResult:
    payload = {
        "totalCustomers": len(rows),
        "rowLimit": 1000,
        "truncated": False,
        "insufficientCohort": False,
        "amountBuckets": list(AMOUNT_BUCKETS),
        "rows": rows,
    }
    payload.update(overrides)
    return SellerCustomerFeaturesResult.model_validate(payload)


def four_type_rows(per_group: int = 40) -> list[dict]:
    """명확히 분리된 4유형 — 충성 / 구매망설임 / 휴면 / 이탈위험 (라벨 순서와 무관).

    군집이 4개로 갈리는 것이 자명하도록 축을 서로 반대 방향으로 밀어 둔다. 그룹 내
    변주는 `j % n` 으로 작게 줘서 실루엣이 1.0 으로 퇴화하지 않게 한다.
    라벨 구간: 1~N 충성 / N+1~2N 구매망설임 / 2N+1~3N 휴면 / 3N+1~4N 이탈위험.
    """
    rows: list[dict] = []
    index = 0
    for j in range(per_group):  # 충성형 — 전 단계 활발 + 고액 + 최근
        index += 1
        rows.append(
            wire_row(
                index,
                sessions=20 + j % 5,
                productViews=60 + j % 7,
                cartAdds=18 + j % 3,
                checkoutStarts=15 + j % 3,
                orderCount=12 + j % 4,
                amountBucket="GTE_300K",
                lastActivityDaysAgo=1 + j % 3,
                firstSeenDaysAgo=300 + j,
            )
        )
    for j in range(per_group):  # 구매망설임형 — 담기 많고 주문 0
        index += 1
        rows.append(
            wire_row(
                index,
                sessions=12 + j % 4,
                productViews=55 + j % 6,
                cartAdds=22 + j % 3,
                checkoutStarts=9 + j % 2,
                orderCount=0,
                amountBucket="ZERO",
                lastActivityDaysAgo=2 + j % 3,
                firstSeenDaysAgo=120 + j,
            )
        )
    for j in range(per_group):  # 휴면형 — 활동 최소 + 오래됨
        index += 1
        rows.append(
            wire_row(
                index,
                sessions=1,
                productViews=1 + j % 2,
                lastActivityDaysAgo=300 + j,
                firstSeenDaysAgo=600 + j,
            )
        )
    for j in range(per_group):  # 이탈위험형 — 사던 사람이 뜸해짐
        index += 1
        rows.append(
            wire_row(
                index,
                sessions=6 + j % 3,
                productViews=16 + j % 4,
                cartAdds=5 + j % 2,
                checkoutStarts=5 + j % 2,
                orderCount=5 + j % 2,
                amountBucket="100K_300K",
                lastActivityDaysAgo=60 + j % 5,
                firstSeenDaysAgo=380 + j,
            )
        )
    return rows
