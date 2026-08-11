"""sop/compute/render.py — 원인·추천·해석 카드 블록 (이슈 #597).

세 블록의 계약을 고정한다: ① 머리글이 "이 목록 밖을 쓰지 말 것"을 못 박는가
② **후보 0건 문장을 코드가 넣는가**(LLM 성의에 맡기면 빈 목록을 받은 LLM 이 원인을
지어 채운다) ③ 걸린 카드가 없으면 블록 자체가 없는가.
"""

from __future__ import annotations

from datetime import date

from app.agents.seller.sop.compute.render import (
    render_candidate_block,
    render_cause_block,
    render_rule_card_block,
)
from app.agents.seller.sop.context import (
    ActionCandidate,
    AnalysisContext,
    CandidateChange,
    CauseCandidate,
    FiredRuleCard,
)


def _ctx(**overrides) -> AnalysisContext:
    return AnalysisContext(
        worker="sales_anomaly",
        brand_id=7,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 10),
        **overrides,
    )


def _cause(
    *, strength: str = "correlated", corroboration: str = "이탈 회원 112명 중 34명"
) -> CauseCandidate:
    return CauseCandidate(
        target_key="sales_anomaly:2026-08-03",
        target_desc="8월 3일 매출 4,120,000원 — 통계적으로 유의한 감소",
        event_kind="price_change",
        event_at=date(2026, 8, 1),
        event_desc="8월 1일 감귤청 가격 12,900 → 15,900원 (+23.3%)",
        lag_days=2,
        strength=strength,
        corroboration=corroboration,
        product_id=101,
    )


def test_원인_후보가_0건이면_그_사실을_코드가_문장으로_넣는다() -> None:
    block = render_cause_block(_ctx())
    assert "원인 후보를 찾지 못했습니다" in block
    assert "이 목록 밖의 원인을 쓰지 말 것" in block


def test_원인_블록은_lag와_강도와_대조_근거를_함께_싣는다() -> None:
    block = render_cause_block(_ctx(causes=[_cause()]))
    assert "[lag 2일]" in block
    assert "대조 근거: 이탈 회원 112명 중 34명" in block
    assert "강도: correlated" in block
    assert "가설 표현 허용" in block


def test_temporal_only는_관측_순서까지만_쓰라고_적는다() -> None:
    block = render_cause_block(_ctx(causes=[_cause(strength="temporal_only", corroboration="")]))
    assert "강도: temporal_only" in block
    assert "관측 순서까지만 서술" in block
    assert "대조 근거" not in block


def test_추천_후보가_0건이면_억지_추천_대신_한_줄이다() -> None:
    block = render_candidate_block(_ctx())
    assert "이번 기간에는 즉시 실행할 만한 변경 후보가 없었습니다." in block


def test_추천_블록은_변경값을_사람이_읽는_라벨로_보여준다() -> None:
    candidate = ActionCandidate(
        slot="restock",
        action_type="stock_adjust",
        product_id=101,
        product_name="감귤청",
        changes=[CandidateChange(field="stock_quantity", before="3", after="30")],
        basis="재고 3건으로 임계 5건 이하입니다.",
    )
    block = render_candidate_block(_ctx(candidate_actions=[candidate]))

    assert "① 재고 보충 · 감귤청(상품 101)" in block
    assert "변경: 재고 3 → 30" in block
    assert "근거: 재고 3건으로 임계 5건 이하입니다." in block
    assert "유형·대상·변경값을 바꾸지 말 것" in block


def test_걸린_카드가_없으면_해석_블록_자체가_없다() -> None:
    """도구가 아니라 주입이라 "검색 결과 없음" 안내가 필요 없다(결정 116)."""
    assert render_rule_card_block(_ctx()) == ""


def test_해석_블록은_문장과_인용을_함께_싣는다() -> None:
    card = FiredRuleCard(
        card_id="moe2003_exploratory",
        scope="segment",
        subject="탐색형",
        statement="상품 조회가 브랜드 평균의 2.4배인데 주문은 0.3배에 그칩니다.",
        citation="Moe (2003), J. Consumer Psychology 13(1-2)",
        strength="empirical",
    )
    block = render_rule_card_block(_ctx(rule_cards=[card]))

    assert "① 탐색형 — 상품 조회가 브랜드 평균의 2.4배" in block
    assert "근거: Moe (2003), J. Consumer Psychology 13(1-2)" in block
    assert "이 목록 밖의 해석을 쓰지 말 것" in block
