"""sop/gate.py — `interpret` 스킵 게이트 (이슈 #594, `01` §4.4).

`behavior` 가 verdict 를 만들지 않는 워커라, 문서 문장을 문자 그대로 구현하면
빈 목록에 대한 `all()` 이 공허참이 되어 언제나 스킵된다 — 그 함정을 테스트로 못 박는다.
"""

from __future__ import annotations

from datetime import date

from app.agents.seller.sop.context import AnalysisContext, Segment, Verdict
from app.agents.seller.sop.gate import should_interpret


def _ctx(**overrides) -> AnalysisContext:
    return AnalysisContext(
        worker="behavior",
        brand_id=7,
        period_from=date(2026, 8, 3),
        period_to=date(2026, 9, 1),
        **overrides,
    )


def _verdict(value: str) -> Verdict:
    return Verdict(key="k", verdict=value, method="two_proportion_z")


def test_유의_판정이_하나라도_있으면_돌린다() -> None:
    ctx = _ctx(verdicts=[_verdict("no_significant_change"), _verdict("significant_drop")])
    assert should_interpret(ctx) is True


def test_판정이_전부_무변화면_스킵한다() -> None:
    ctx = _ctx(verdicts=[_verdict("no_significant_change"), _verdict("undecided")])
    assert should_interpret(ctx) is False


def test_판정_축이_없는_워커는_세그먼트로_판단한다() -> None:
    ctx = _ctx(segments=[Segment(rule_label="충성형", size=96)])
    assert should_interpret(ctx) is True


def test_판정도_세그먼트도_없으면_스킵한다() -> None:
    assert should_interpret(_ctx()) is False


def test_보류만_있는_ctx도_스킵한다() -> None:
    """판정 보류는 서술 재료가 아니라 한계 표기다 — `holds[]` 로 이미 드러난다."""
    ctx = _ctx(verdicts=[_verdict("undecided")])
    assert should_interpret(ctx) is False
