"""주입형 골든셋 회귀 — CI 편입 (이슈 #595, `12-EVAL` 결정 120).

`analysis/` 단위 테스트가 "함수가 안 터지는지"를 본다면 여기는 **"판정이 맞는지"** 를
본다. 케이스가 깨졌을 때 무엇을 못 지키게 됐는지는 테스트 이름이 아니라 케이스의
``claim`` 에 적혀 있다 — 실패 메시지가 그것을 그대로 옮긴다.
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis import scan
from app.agents.seller.sop.scan_params import thresholds_from_settings
from app.core.config import Settings
from evals.seller_trigger import goldenset

pytestmark = pytest.mark.eval


def _thresholds() -> scan.TriggerThresholds:
    return thresholds_from_settings(Settings(_env_file=None))


def test_every_case_passes() -> None:
    outcomes = goldenset.run_goldenset(_thresholds())
    failures = [
        f"{o.case_id} {o.title} — {o.claim} / 관측 {o.observed}" for o in outcomes if not o.passed
    ]
    assert not failures, "\n".join(failures)


def test_case_inventory_is_stable() -> None:
    """케이스를 조용히 빼면 통과율만 올라간다 — 목록 자체를 못박는다."""
    outcomes = goldenset.run_goldenset(_thresholds())
    assert [o.case_id for o in outcomes] == [f"gs-{n:02d}" for n in range(1, 11)]


def test_known_gap_is_registered_with_follow_up() -> None:
    """현재 미검출을 고정하는 케이스는 후속 작업을 함께 적어 둔다.

    후속 이슈가 하루 단위 급락 검출을 붙이면 이 케이스가 **실패해서** 알려준다 —
    그때 ``known_gap`` 을 지우고 기대를 뒤집는다. 결함을 주석으로만 남기면 고쳐진
    사실을 아무도 모른 채 지나간다.
    """
    outcomes = {o.case_id: o for o in goldenset.run_goldenset(_thresholds())}
    gap = outcomes["gs-10"]
    assert gap.known_gap
    assert gap.follow_up
    assert not gap.observed["fired"]


def test_goldenset_channel_is_not_vacuous() -> None:
    """[반대 테스트] 임계를 도달 불가로 올리면 검출 케이스가 **실제로** 실패해야 한다."""
    base = _thresholds()
    broken = type(base)(
        **{
            **{field: getattr(base, field) for field in base.__dataclass_fields__},
            "sales_pct": 0.99,
        }
    )
    assert not goldenset.case_sales_sustained_drop(broken).passed
