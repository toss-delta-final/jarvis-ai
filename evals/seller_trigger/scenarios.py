"""시뮬레이션 시나리오 정의 — 하네스와 테스트가 공유하는 단일 출처 (이슈 #595).

브랜드 규모를 세 단계로 나누는 이유는 실측이다. **표본 크기에 따라 AND 의 작동 방식이
뒤집힌다** — 표본이 크면 고정 임계가 "작지만 유의한 변화"를 걸러내지만, 표본이 작으면
유의한 날이 곧 변화가 큰 날이라 두 조건이 거의 같은 사건이 되고 AND 가 α 로 퇴화한다.
한 규모만 재면 그 사실이 안 보이고, 임계를 "확정"했다는 말이 근거를 잃는다.

요일 진폭을 과장한 시나리오는 계절성 대조군이다 — REES46 실측 요일 진폭은 ±11% 뿐이라
그것만으로는 "요일 효과가 오탐을 만드는가"에 답이 안 된다.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.seller_trigger import synth

# 데이터셋 버전 — 시나리오 모수나 생성기 로직이 바뀌면 올린다. 리포트 파일명이 이 값을
# 달고 있어서, 버전을 올리지 않고 모수를 바꾸면 옛 근거 위에 새 숫자가 덮인다.
DATASET_VERSION = "seller-trigger-v1"


@dataclass(frozen=True)
class Scenario:
    key: str
    description: str
    params: synth.NullBrandParams

    def series(self) -> synth.NullBrandSeries:
        return synth.generate_null_brand(self.params)


def _params(days: int, *, seed: int, views: float, **overrides) -> synth.NullBrandParams:
    return synth.NullBrandParams(
        days=days,
        seed=seed,
        daily_views=views,
        # 상품 축 조회량은 브랜드 조회량에 비례시킨다 — 규모만 바뀌고 형태는 같게.
        daily_product_views=views / 2.9,
        daily_order_members=views / 65.0,
        daily_signups=views / 433.0,
        **overrides,
    )


def build_scenarios(days: int) -> list[Scenario]:
    """게이트가 도는 시나리오 4종. 순서 고정(결정론)."""
    return [
        Scenario(
            key="small",
            description="소규모 — 구매 약 11건/일",
            params=_params(days, seed=595101, views=650.0),
        ),
        Scenario(
            key="medium",
            description="중규모 — 구매 약 44건/일",
            params=_params(days, seed=595102, views=2600.0),
        ),
        Scenario(
            key="large",
            description="대규모 — 구매 약 175건/일",
            params=_params(days, seed=595103, views=10400.0),
        ),
        Scenario(
            key="weekend_dip",
            description="계절성 대조군 — 중규모 + 주말 −30%(REES46 실측 진폭 ±11% 의 과장)",
            params=_params(
                days, seed=595104, views=2600.0, dow_weights=synth.WEEKEND_DIP_DOW_WEIGHTS
            ),
        ),
    ]
