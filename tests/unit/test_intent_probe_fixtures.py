"""intent 라우팅 프로브 앵커 정답지 — 스키마·해시 게이트·컨텍스트 조립 (#260)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from evals.intent_probe.loader import (
    FIXTURE_DIR,
    build_cells,
    build_context_kwargs,
    load_anchor_set,
    resolve_fixture_path,
)
from evals.intent_probe.schema import GROUP_ALLOWED_CONTEXTS, GROUPS, AnchorSet

SETTINGS = get_settings()

# 이슈 #260이 문자로 지정한 전환 발화 7종 — 이 목록 자체가 요구사항이라 테스트로 고정한다.
SWITCH_TEXTS = (
    "이어폰으로 할래",
    "다른 거 담아줘",
    "이거 말고 다른 거 담아줘",
    "그거 말고 이어폰 담아줘",
    "이어폰으로 바꿔줘",
    "섬유유연제로 담아줘",
    "아니 이어폰 담아줘",
)
GROUP_COUNTS = {
    "cart_control": 6,
    "demonstrative": 4,
    "option_answer": 4,
    "switch": 7,
    "order_status": 2,
    "general": 2,
    # [#84] 카테고리 승계 3분기 — 리파인 4 · 리셋 4 · 교체 3 · **혼합 4**(라운드 3).
    "category_action": 15,
    # [#300] #118 이관 — 확정 4(단일/순번/좌표/이름) · 되물음 1(안전 셀) · 확정금지 1.
    "screen": 6,
    # [#344 라운드 2] 조건 전용 발화(카테고리 어휘 없이 조건만 말하는 턴) — categoryQueries 비움
    # 불변식.
    "condition_only": 5,
    # [#386] 찜 목록 조회 — 양성 3("내가 뭐 찜했지?"류) · 음성 대조 3("보여줘"·"뭐 있어?" 단독,
    # "찜한 거 담아줘"). 음성 대조가 절반인 것은 이 축의 목적이 "새 intent 가 남의 발화를
    # 훔치지 않는가" 이기 때문이다.
    "wishlist_view": 6,
    # [#440] 찜 해제 대상 해소 — 양성 2("찜한 거 빼줘"류) · 음성 대조 2(음식명 "찜닭 빼줘",
    # 잠식 대조 "장바구니에서 빼줘"). "찜한 거 담아줘"(#386 대조)는 wishlist-view-006 과 발화가
    # 겹쳐 중복 셀을 만들지 않고 뺐다(기존 셀이 이미 재고 있다) — 그래서 패킷 5셀이 아니라 4셀.
    "wishlist_remove": 4,
    # [#285, I-25 §4.13 — 4단계] 장바구니 수량 변경 라우팅 — 양성 3("3개로 바꿔줘"류) · 음성
    # 대조 3(합산 "하나 더 담아줘"[가장 중요, 패킷 함정 2] · 삭제 "이어폰 빼줘" · 조회 "장바구니
    # 보여줘"). combo_matrix 는 이 intent 를 재지 않기로 결정했으므로(axes.json
    # cart_quantity_not_generated) 라우팅 회귀는 이 그룹이 전담한다.
    "cart_quantity": 6,
    # [#443] 상품군을 명시한 첫 턴에서 categoryQueries leg 이 나오는지 — `condition_only` 의
    # 반대 방향 축. 요인 분리(대조군·상황 선행/후행·추상도·일반화·수식어) 6발화.
    "named_category": 6,
}
# [#300, D-4] #118 원본(PR #292, 이관 전 별도 프로브의 신규(screen) 그룹 — #300 이 흡수하며
# 삭제했다)에서 **문자 단위로 옮긴** 발화 6종 — 이 목록 자체가 표본 동일성 요구사항이라
# 테스트로 고정한다.
SCREEN_TEXTS = (
    "이거 담아줘",
    "이거 담아줘",
    "3번째 거 담아줘",
    "3번째 줄 2번째 담아줘",
    "무선 이어폰 담아줘",
    "301 담아줘",
)


def _raw(name: str = "b") -> dict:
    return json.loads(resolve_fixture_path(name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["a", "b"])
def test_committed_anchor_sets_load_and_match_manifest_hash(name: str) -> None:
    anchors = load_anchor_set(name)
    assert anchors.fixture_version == f"intent-probe-anchors-{name}-v8"


@pytest.mark.parametrize("name", ["a", "b"])
def test_group_counts_match_issue_260(name: str) -> None:
    anchors = load_anchor_set(name)
    counts: dict[str, int] = {}
    for utterance in anchors.utterances:
        counts[utterance.group] = counts.get(utterance.group, 0) + 1
    assert counts == GROUP_COUNTS


def test_switch_utterances_are_verbatim_from_issue_260() -> None:
    anchors = load_anchor_set("b")
    texts = tuple(u.text for u in anchors.utterances if u.group == "switch")
    assert texts == SWITCH_TEXTS


def test_screen_utterances_are_verbatim_from_issue_118() -> None:
    anchors = load_anchor_set("b")
    texts = tuple(u.text for u in anchors.utterances if u.group == "screen")
    assert texts == SCREEN_TEXTS


def test_cell_count_matches_group_context_product() -> None:
    anchors = load_anchor_set("b")
    cells = build_cells(anchors)
    # 발화 × 컨텍스트: 대조군 18 + 지시대명사 12 + 옵션 4 + 전환 7 + 주문 6 + 일반 6
    # + [#84] 카테고리 15(단일 컨텍스트) + [#300] screen 6(단일 컨텍스트)
    # + [#344 라운드 2] 조건 전용 5(단일 컨텍스트)
    # + [#386] 찜 조회 6(단일 컨텍스트) + [#440] 찜 해제 4(단일 컨텍스트)
    # + [#285, I-25 §4.13] 수량 변경 6(단일 컨텍스트) = 95
    # + [#386] 찜 조회 6 + [#440] 찜 해제 4 + [#443] 상품군 명시 6 + [#285] 수량 변경 6(전부 단일 컨텍스트) = 101
    assert len(cells) == 101
    per_group: dict[str, int] = {}
    for cell in cells:
        per_group[cell.utterance.group] = per_group.get(cell.utterance.group, 0) + 1
    assert per_group == {
        "cart_control": 18,
        "demonstrative": 12,
        "option_answer": 4,
        "switch": 7,
        "order_status": 6,
        "general": 6,
        "category_action": 15,
        "screen": 6,
        "condition_only": 5,
        "wishlist_view": 6,
        "wishlist_remove": 4,
        "cart_quantity": 6,
        "named_category": 6,
    }


def test_cells_are_sorted_deterministically() -> None:
    cells = build_cells(load_anchor_set("b"))
    assert [cell.cell_id for cell in cells] == sorted(cell.cell_id for cell in cells)


def test_two_anchor_sets_differ_only_in_reask_position() -> None:
    a_raw, b_raw = _raw("a"), _raw("b")
    differing = {key for key in a_raw if a_raw[key] != b_raw.get(key)}
    assert differing == {"fixtureVersion", "reaskProductId", "reaskProductListPosition"}
    assert (a_raw["reaskProductListPosition"], b_raw["reaskProductListPosition"]) == (1, 2)


@pytest.mark.parametrize(("name", "position"), [("a", 1), ("b", 2)])
def test_reask_position_agrees_with_recommendation_list(name: str, position: int) -> None:
    anchors = load_anchor_set(name)
    assert anchors.reask_product_list_position == position
    assert anchors.last_recommendations[position - 1].product_id == anchors.reask_product_id


def test_reask_position_rationale_is_recorded() -> None:
    # 위치를 고정한 '이유'가 파일 안에 남아야 다음 사람이 함부로 바꾸지 않는다 (#240 사고).
    anchors = load_anchor_set("b")
    assert "#240" in anchors.reask_position_rationale
    assert len(anchors.reask_position_rationale) > 80


def test_reask_position_mismatch_is_rejected() -> None:
    data = _raw("b")
    data["reaskProductListPosition"] = 3
    with pytest.raises(ValidationError, match="되물음 상품"):
        AnchorSet.model_validate(data)


def test_option_token_inside_product_name_is_rejected() -> None:
    # #240 실제 결함: 되물음 상품명에 옵션 이름('드럼')이 섞여 `일반형` 답이 8/8 오답이었다.
    data = _raw("b")
    data["lastRecommendations"][1]["name"] = "드럼용 세탁 세제"
    with pytest.raises(ValidationError, match="옵션 이름"):
        AnchorSet.model_validate(data)


def test_switch_target_inside_reask_product_name_is_rejected() -> None:
    data = _raw("b")
    data["lastRecommendations"][1]["name"] = "무선 이어폰 세탁 세제"
    with pytest.raises(ValidationError, match="전환 대상"):
        AnchorSet.model_validate(data)


def test_option_answer_without_expected_option_id_is_rejected() -> None:
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["group"] == "option_answer":
            utterance["expected"].pop("optionId")
            break
    with pytest.raises(ValidationError, match="optionId"):
        AnchorSet.model_validate(data)


def test_switch_without_product_id_rule_is_rejected() -> None:
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["group"] == "switch":
            utterance["expected"]["productIdRule"] = "none"
            break
    with pytest.raises(ValidationError, match="productIdRule"):
        AnchorSet.model_validate(data)


def test_blank_rationale_is_rejected() -> None:
    data = _raw("b")
    data["reaskPositionRationale"] = "   "
    with pytest.raises(ValidationError):
        AnchorSet.model_validate(data)


def test_unknown_axis_is_rejected() -> None:
    data = _raw("b")
    data["utterances"][0]["axes"] = ["notAnAxis"]
    with pytest.raises(ValidationError, match="축"):
        AnchorSet.model_validate(data)


def test_tampered_fixture_file_is_rejected(tmp_path: Path) -> None:
    for name in ("anchors_b.json", "manifest.json"):
        (tmp_path / name).write_bytes((FIXTURE_DIR / name).read_bytes())
    data = json.loads((tmp_path / "anchors_b.json").read_text(encoding="utf-8"))
    data["utterances"][0]["text"] = "손댄 발화"
    (tmp_path / "anchors_b.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_anchor_set("b", fixture_dir=tmp_path)


def test_build_context_kwargs_none_context_is_empty() -> None:
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "none")
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs == {
        "prior_filters": None,
        "last_recommendations": None,
        "pending_cart": None,
        "screen": None,
    }


def test_build_context_kwargs_last_recommendations_shape() -> None:
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "lastRecommendations")
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs["last_recommendations"] == [
        (product.product_id, product.name) for product in anchors.last_recommendations
    ]
    assert kwargs["prior_filters"] is not None
    assert kwargs["prior_filters"].semantic_query == anchors.prior_filters["semanticQuery"]
    assert kwargs["pending_cart"] is None


def test_build_context_kwargs_pending_cart_shape_matches_graph() -> None:
    # app/agents/buyer/graph.py 가 decompose 에 넘기는 dict 모양과 같아야 한다.
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "pendingCart")
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs["pending_cart"] == {
        "productId": anchors.reask_product_id,
        "options": [
            {"optionId": option.option_id, "name": option.name} for option in anchors.options
        ],
    }


# ─────────── #84 카테고리 승계 3분기 축 ───────────


def _category_utterance(data: dict) -> dict:
    return next(u for u in data["utterances"] if u["group"] == "category_action")


def test_category_utterance_without_expected_action_is_rejected() -> None:
    data = _raw("b")
    _category_utterance(data)["expected"].pop("categoryAction")
    with pytest.raises(ValidationError, match="categoryAction"):
        AnchorSet.model_validate(data)


def test_category_utterance_declaring_a_legacy_axis_is_rejected() -> None:
    """새 셀이 기존 축의 분모를 늘리면 커밋된 기준선과 그 축을 비교할 수 없다 — 그 사유가
    에러 메시지에 들어 있어야 다음 사람이 '축 하나쯤' 하고 붙이지 않는다."""
    data = _raw("b")
    _category_utterance(data)["axes"].append("mainIntent")
    with pytest.raises(ValidationError) as excinfo:
        AnchorSet.model_validate(data)
    message = str(excinfo.value)
    assert "mainIntent" in message
    assert "baselines/fast-2026-08-04" in message
    assert "비교할 수 없" in message


def test_non_category_utterance_declaring_a_new_axis_is_rejected() -> None:
    data = _raw("b")
    next(u for u in data["utterances"] if u["group"] == "general")["axes"].append("categoryClear")
    with pytest.raises(ValidationError, match="categoryClear"):
        AnchorSet.model_validate(data)


def test_category_utterance_must_use_only_the_category_prior_context() -> None:
    data = _raw("b")
    _category_utterance(data)["contexts"] = ["categoryPrior", "none"]
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_category_prior_filters_without_a_category_is_rejected() -> None:
    data = _raw("b")
    data["categoryPriorFilters"] = {"semanticQuery": "무선 이어폰"}
    with pytest.raises(ValidationError, match="category"):
        AnchorSet.model_validate(data)


def test_category_utterance_repeating_the_prior_category_leaf_is_rejected() -> None:
    # 발화가 직전 카테고리 어휘를 쓰면 carry/replace 어느 쪽으로도 읽혀 정답이 자명하지 않다.
    data = _raw("b")
    _category_utterance(data)["text"] = "이어폰 말고 더 싼 걸로"
    with pytest.raises(ValidationError, match="이어폰"):
        AnchorSet.model_validate(data)


def test_category_prior_context_carries_the_category_filters() -> None:
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "categoryPrior")
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs["prior_filters"] is not None
    assert kwargs["prior_filters"].category == anchors.category_prior_filters["category"]
    # 이 축은 PRIOR_FILTERS.category 단독의 효과를 잰다 — 직전 추천 목록이 섞이면 오염된다.
    assert kwargs["last_recommendations"] is None
    assert kwargs["pending_cart"] is None


@pytest.mark.parametrize("context_id", ["lastRecommendations", "pendingCart"])
def test_existing_contexts_still_carry_the_default_prior_filters(context_id: str) -> None:
    # 회귀 — 기본 `priorFiltersRef` 가 붙었다고 기존 컨텍스트가 다른 필터를 싣기 시작하면
    # 기존 축이 통째로 다른 조건에서 측정된다(기준선과 비교 불가).
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == context_id)
    assert context.prior_filters_ref == "default"
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs["prior_filters"].semantic_query == anchors.prior_filters["semanticQuery"]
    assert kwargs["prior_filters"].category is None


# ─────────── [#300] screen 지시어 해소(#118 이관) ───────────

# (utteranceId, screenId, columns, screen 상품 (productId, name) 목록, productIdRule, productId,
# forbiddenProductId) — #118 원본(PR #292, 이관 전 별도 프로브의 `SCREEN_1/3/5/9/NAME`·
# screen_cells — #300 이 흡수하며 삭제했다)에서 **문자 단위로 옮긴** 모양. 상품 **이름 문자열**도
# 못박는다 — 이름 매칭 셀(`무선 이어폰 담아줘` → 3110)의 정답은 오로지 그 이름 문자열로
# 성립하므로, id·규칙만 고정하면 이름이 바뀌어도(예: `무선 블루투스 이어폰` → `유선 헤드폰`) 이
# 회귀 가드가 잡지 못한다(리뷰 1차 F-3 재현).
_R = "코튼 워셔블 러그 150x200"
_B = "라탄 수납 바구니 3종"
_H = "무드등 겸용 가습기"
_T = "우드 사이드테이블"
_C = "린넨 커튼 2p"
_F = "저상형 프레임 침대"
_M = "메모리폼 방석"
_E = "무선 블루투스 이어폰"
_S = "탁상 선풍기"
SCREEN_CELL_SHAPES = (
    ("screen-001", "screen-single", 1, ((3101, _R),), "screenExact", 3101, None),
    (
        "screen-002",
        "screen-triple",
        3,
        ((3101, _R), (3102, _B), (3103, _H)),
        "screenReask",
        None,
        None,
    ),
    (
        "screen-003",
        "screen-five",
        3,
        ((3101, _R), (3102, _B), (3103, _H), (3104, _T), (3105, _C)),
        "screenExact",
        3103,
        None,
    ),
    (
        "screen-004",
        "screen-nine",
        3,
        (
            (3101, _R),
            (3102, _B),
            (3103, _H),
            (3104, _T),
            (3105, _C),
            (3106, _F),
            (3107, _M),
            (3108, _E),
            (3109, _S),
        ),
        "screenExact",
        3108,
        None,
    ),
    (
        "screen-005",
        "screen-named",
        2,
        ((3101, _R), (3102, _B), (3103, _H), (3110, _E)),
        "screenExact",
        3110,
        None,
    ),
    (
        "screen-006",
        "screen-triple",
        3,
        ((3101, _R), (3102, _B), (3103, _H)),
        "screenNotHallucinated",
        None,
        301,
    ),
)
# 5개 screen 픽스처 전부 D-4 규약대로 `pageType="chat"` · `filters={"page": "1"}` 이다 — 라벨
# 문자열(`인기 상품`)은 프롬프트에 직접 쓰지 않고 프로덕션 투영으로 붙으므로 픽스처에는 없다.
SCREEN_PAGE_TYPE = "chat"
SCREEN_FILTERS = {"page": "1"}
# 원본 `RECO_BASE` — screen 컨텍스트 전용 LAST_RECOMMENDATIONS.
SCREEN_LAST_RECOMMENDATIONS = (
    (2001, "리필 세탁 세제 2L"),
    (2002, "드럼용 세탁 세제"),
    (2003, "섬유유연제"),
)


def test_screen_cell_shapes_are_pinned_literally() -> None:
    """[§5.2, 리뷰 1차 F-3] 표본 동일성 회귀 가드 — 픽스처가 조용히 흔들리면 이 테스트가
    실패해야 한다. id·columns·규칙뿐 아니라 **상품명·pageType·filters** 까지 못박는다."""
    anchors = load_anchor_set("b")
    context_by_id = {context.context_id: context for context in anchors.contexts}
    screen_by_id = {screen.screen_id: screen for screen in anchors.screens}
    utterance_by_id = {utterance.utterance_id: utterance for utterance in anchors.utterances}
    for (
        utterance_id,
        screen_id,
        columns,
        products,
        rule,
        product_id,
        forbidden_product_id,
    ) in SCREEN_CELL_SHAPES:
        utterance = utterance_by_id[utterance_id]
        context = context_by_id[utterance.contexts[0]]
        assert context.screen_ref == screen_id
        assert context.last_recommendations_ref == "screen"
        assert context.include_pending_cart is False
        screen = screen_by_id[screen_id]
        assert screen.columns == columns
        assert screen.page_type == SCREEN_PAGE_TYPE
        assert screen.filters == SCREEN_FILTERS
        assert tuple((product.product_id, product.name) for product in screen.products) == products
        assert utterance.expected.product_id_rule == rule
        assert utterance.expected.product_id == product_id
        assert utterance.expected.forbidden_product_id == forbidden_product_id


def test_screen_last_recommendations_are_pinned_literally() -> None:
    anchors = load_anchor_set("b")
    assert (
        tuple((product.product_id, product.name) for product in anchors.screen_last_recommendations)
        == SCREEN_LAST_RECOMMENDATIONS
    )


def test_build_context_kwargs_screen_context_projects_the_production_label() -> None:
    """screen 라벨은 픽스처가 아니라 프로덕션 투영 경로(`build_screen_prompt`)로 붙는다."""
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "screenSingle")
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    screen = kwargs["screen"]
    assert screen is not None
    assert screen.label == "인기 상품"  # settings.screen_page_type_labels["chat"]
    assert screen.columns == 1
    assert screen.products == [(3101, "코튼 워셔블 러그 150x200")]
    # screen 컨텍스트는 screenLastRecommendations 를 싣는다 — 기본 lastRecommendations 가 아니다.
    assert kwargs["last_recommendations"] == list(SCREEN_LAST_RECOMMENDATIONS)


@pytest.mark.parametrize(
    "context_id", ["none", "lastRecommendations", "pendingCart", "categoryPrior"]
)
def test_existing_contexts_do_not_carry_screen(context_id: str) -> None:
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == context_id)
    kwargs = build_context_kwargs(anchors, context, settings=SETTINGS)
    assert kwargs["screen"] is None


def test_pending_cart_and_screen_together_is_rejected() -> None:
    """[이슈 완료조건] 되물음 턴에는 화면 맥락을 프롬프트에 싣지 않는다(graph.py 배선과 동일)."""
    data = _raw("b")
    for context in data["contexts"]:
        if context["contextId"] == "screenSingle":
            context["includePendingCart"] = True
            break
    with pytest.raises(ValidationError, match="pendingCart"):
        AnchorSet.model_validate(data)


def test_non_screen_utterance_declaring_a_screen_context_is_rejected() -> None:
    """[리뷰 1차 F-1 → #313] 비-screen 발화가 screen 컨텍스트를 contexts 에 끼워 넣으면 안 된다.

    이걸 막지 않으면 `cartControl` 같은 기존 축의 분모가 셀 수와 함께 조용히 불어나 커밋된
    기준선과 비교할 수 없게 된다(재현: cart-control-001 이 screenTriple 을 추가로 선언). [#313]
    전용 검증자(`_non_screen_utterances_cannot_reference_screen_contexts`)를
    `GROUP_ALLOWED_CONTEXTS` 일반형 매핑이 흡수했다 — `cart_control` 의 허용 컨텍스트에
    `screenTriple` 이 없어 여전히 거부된다.
    """
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["utteranceId"] == "cart-control-001":
            utterance["contexts"].append("screenTriple")
            break
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_general_utterance_declaring_a_screen_context_is_rejected() -> None:
    """[리뷰 1차 F-1 → #313] 읽기 전용 리뷰어가 독립 재현한 두 번째 사례 — general 그룹도 막혀야 한다."""
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["utteranceId"] == "general-001":
            utterance["contexts"].append("screenSingle")
            break
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_screen_utterance_with_a_mismatched_rule_axis_is_rejected() -> None:
    """[리뷰 1차 F-2] screenExact 발화가 screenReask 축을 선언해도 거부해야 한다.

    이걸 막지 않으면 축 정의 문장이 말하는 분모(32/8/8/48)와 실제 채점 표본이 조용히 어긋난다.
    """
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["utteranceId"] == "screen-001":
            utterance["axes"] = ["screenReask", "screenResolution"]
            break
    with pytest.raises(ValidationError, match="axes"):
        AnchorSet.model_validate(data)


def test_screen_utterance_missing_screen_resolution_axis_is_rejected() -> None:
    """[리뷰 1차 F-2] `screenResolution` 은 합계 축이라 모든 screen 발화가 함께 선언해야 한다."""
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["utteranceId"] == "screen-002":
            utterance["axes"] = ["screenReask"]
            break
    with pytest.raises(ValidationError, match="axes"):
        AnchorSet.model_validate(data)


def test_screen_utterance_declaring_an_extra_screen_axis_is_rejected() -> None:
    """[리뷰 1차 F-2] 규칙에 맞는 축이어도 다른 screen 축을 추가로 선언하면 거부한다."""
    data = _raw("b")
    for utterance in data["utterances"]:
        if utterance["utteranceId"] == "screen-006":
            utterance["axes"] = ["screenNoHallucination", "screenResolution", "screenReask"]
            break
    with pytest.raises(ValidationError, match="axes"):
        AnchorSet.model_validate(data)


def test_include_screen_without_screen_ref_is_rejected() -> None:
    data = _raw("b")
    for context in data["contexts"]:
        if context["contextId"] == "screenSingle":
            del context["screenRef"]
            break
    with pytest.raises(ValidationError, match="screenRef"):
        AnchorSet.model_validate(data)


def test_screen_ref_without_include_screen_is_rejected() -> None:
    data = _raw("b")
    for context in data["contexts"]:
        if context["contextId"] == "lastRecommendations":
            context["screenRef"] = "screen-single"
            break
    with pytest.raises(ValidationError, match="screenRef"):
        AnchorSet.model_validate(data)


def test_screen_ref_pointing_to_a_missing_screen_is_rejected() -> None:
    data = _raw("b")
    for context in data["contexts"]:
        if context["contextId"] == "screenSingle":
            context["screenRef"] = "screen-does-not-exist"
            break
    with pytest.raises(ValidationError, match="screenId"):
        AnchorSet.model_validate(data)


def test_duplicate_screen_id_is_rejected() -> None:
    data = _raw("b")
    data["screens"][1]["screenId"] = data["screens"][0]["screenId"]
    with pytest.raises(ValidationError, match="screenId"):
        AnchorSet.model_validate(data)


def _screen_utterance(data: dict, utterance_id: str) -> dict:
    return next(u for u in data["utterances"] if u["utteranceId"] == utterance_id)


def test_screen_utterance_without_a_screen_rule_is_rejected() -> None:
    data = _raw("b")
    utterance = _screen_utterance(data, "screen-001")
    utterance["expected"]["productIdRule"] = "none"
    del utterance["expected"]["productId"]
    with pytest.raises(ValidationError, match="productIdRule"):
        AnchorSet.model_validate(data)


def test_screen_utterance_with_two_contexts_is_rejected() -> None:
    data = _raw("b")
    _screen_utterance(data, "screen-001")["contexts"] = ["screenSingle", "screenTriple"]
    with pytest.raises(ValidationError, match="screen 컨텍스트 1개"):
        AnchorSet.model_validate(data)


def test_screen_utterance_declaring_a_legacy_axis_is_rejected() -> None:
    data = _raw("b")
    _screen_utterance(data, "screen-001")["axes"].append("mainIntent")
    with pytest.raises(ValidationError, match="mainIntent"):
        AnchorSet.model_validate(data)


def test_non_screen_utterance_declaring_a_screen_axis_is_rejected() -> None:
    data = _raw("b")
    next(u for u in data["utterances"] if u["group"] == "general")["axes"].append("screenExactPick")
    with pytest.raises(ValidationError, match="screenExactPick"):
        AnchorSet.model_validate(data)


def test_screen_exact_product_id_outside_its_screen_is_rejected() -> None:
    data = _raw("b")
    _screen_utterance(data, "screen-001")["expected"]["productId"] = 9999
    with pytest.raises(ValidationError, match="products 안에 없습니다"):
        AnchorSet.model_validate(data)


def test_screen_reask_with_a_product_id_is_rejected() -> None:
    data = _raw("b")
    _screen_utterance(data, "screen-002")["expected"]["productId"] = 3101
    with pytest.raises(ValidationError, match="screenReask"):
        AnchorSet.model_validate(data)


def test_screen_not_hallucinated_without_forbidden_id_is_rejected() -> None:
    data = _raw("b")
    del _screen_utterance(data, "screen-006")["expected"]["forbiddenProductId"]
    with pytest.raises(ValidationError, match="forbiddenProductId"):
        AnchorSet.model_validate(data)


def test_screen_not_hallucinated_forbidding_an_in_list_id_is_rejected() -> None:
    data = _raw("b")
    # 3101 은 screen-triple(screen-006 이 가리키는 픽스처) 의 products 안이다.
    _screen_utterance(data, "screen-006")["expected"]["forbiddenProductId"] = 3101
    with pytest.raises(ValidationError, match="목록 안에 있습니다"):
        AnchorSet.model_validate(data)


def test_screen_product_name_overlapping_screen_last_recommendations_is_rejected() -> None:
    data = _raw("b")
    # screenLastRecommendations[0] 과 같은 이름을 screen-single 상품에 심는다.
    data["screens"][0]["products"][0]["name"] = "리필 세탁 세제 2L"
    with pytest.raises(ValidationError, match="screenLastRecommendations"):
        AnchorSet.model_validate(data)


# ─────────── [#344 라운드 2] 조건 전용 발화 categoryQueries 비움 불변식 ───────────


def _condition_only_utterance(data: dict) -> dict:
    return next(u for u in data["utterances"] if u["group"] == "condition_only")


def test_condition_only_utterances_are_verbatim_from_category_probe_none_slice() -> None:
    """category_probe none 슬라이스와 같은 문구여야 두 하네스가 같은 현상을 재는지 비교할 수 있다."""
    anchors = load_anchor_set("b")
    texts = {u.text for u in anchors.utterances if u.group == "condition_only"}
    assert texts == {
        "5만원 이하 아무거나 추천해줘",
        "평점 좋은 걸로 보여줘",
        "인기 많은 거 추천해줘",
        "무료배송 되는 걸로 찾아줘",
        "가성비 좋은 거 추천해줘",
    }


def test_condition_only_utterance_must_use_only_the_none_context() -> None:
    """[#344 라운드 3] 이 보호는 이제 [#313] `GROUP_ALLOWED_CONTEXTS`(condition_only → {none})가
    맡는다 — 우리 전용 검증자에서는 컨텍스트 검사를 뺐다(아래 [#313] 절 참조)."""
    data = _raw("b")
    _condition_only_utterance(data)["contexts"] = ["none", "categoryPrior"]
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_condition_only_utterance_declaring_a_legacy_axis_is_rejected() -> None:
    data = _raw("b")
    _condition_only_utterance(data)["axes"].append("mainIntent")
    with pytest.raises(ValidationError, match="mainIntent"):
        AnchorSet.model_validate(data)


def test_non_condition_only_utterance_declaring_the_new_axis_is_rejected() -> None:
    data = _raw("b")
    next(u for u in data["utterances"] if u["group"] == "general")["axes"].append(
        "conditionOnlyNoCategoryQuery"
    )
    with pytest.raises(ValidationError, match="conditionOnlyNoCategoryQuery"):
        AnchorSet.model_validate(data)


# ─────────── [#443] 상품군 명시 첫 턴 leg 산출(condition_only 의 반대 방향) ───────────


def _named_category_utterance(data: dict) -> dict:
    return next(u for u in data["utterances"] if u["group"] == "named_category")


def test_named_category_utterances_all_carry_a_product_name() -> None:
    """[#443] 이 축의 정답("leg 이 하나는 나와야 한다")이 자명하려면 발화에 살 물건 이름이
    반드시 들어 있어야 한다 — 조건·상황만 있는 발화는 condition_only 의 정의역이지 이 그룹이
    아니다."""
    anchors = load_anchor_set("b")
    texts = tuple(u.text for u in anchors.utterances if u.group == "named_category")
    assert texts == (
        "과일 추천해줘",
        "나 아기 키우는데 과일 추천해줘",
        "과일 추천해줘, 나 아기 키우고 있어서",
        "나 아기 키우는데 유아용 물티슈 추천해줘",
        "나 캠핑 다니는데 텐트 추천해줘",
        "가성비 좋은 텀블러 추천해줘",
    )


def test_named_category_utterance_must_use_only_the_none_context() -> None:
    """[#443] `GROUP_ALLOWED_CONTEXTS`(named_category → {none})가 막는다 —
    `test_condition_only_utterance_must_use_only_the_none_context` 와 같은 재현."""
    data = _raw("b")
    _named_category_utterance(data)["contexts"] = ["none", "categoryPrior"]
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_named_category_utterance_declaring_a_legacy_axis_is_rejected() -> None:
    data = _raw("b")
    _named_category_utterance(data)["axes"].append("mainIntent")
    with pytest.raises(ValidationError, match="mainIntent"):
        AnchorSet.model_validate(data)


def test_non_named_category_utterance_declaring_the_new_axis_is_rejected() -> None:
    data = _raw("b")
    next(u for u in data["utterances"] if u["group"] == "general")["axes"].append(
        "namedCategoryHasLeg"
    )
    with pytest.raises(ValidationError, match="namedCategoryHasLeg"):
        AnchorSet.model_validate(data)


# ─────────── [#313] group → 허용 컨텍스트 매핑 ───────────


def _utterance(data: dict, utterance_id: str) -> dict:
    return next(u for u in data["utterances"] if u["utteranceId"] == utterance_id)


def test_group_allowed_contexts_covers_every_group() -> None:
    # group 을 추가하면서 매핑을 안 채우면 이 테스트가 어긋난다 — 매핑에 한 줄을 넣지 않으면
    # 그 group 은 어떤 컨텍스트도 선언할 수 없다는 안전 기본값이 이슈의 요구다.
    assert GROUP_ALLOWED_CONTEXTS.keys() == GROUPS


@pytest.mark.parametrize(
    "utterance_id",
    ["cart-control-001", "order-status-001", "demonstrative-001"],
)
def test_denominator_changing_context_addition_is_rejected(utterance_id: str) -> None:
    """[#313 재현 1/3] categoryPrior 를 추가로 선언하면 셀 수(분모)가 조용히 늘어난다 — 기존 수치
    가드(`test_existing_axis_denominators_are_unaffected_by_screen_cells`)가 잡던 경로지만,
    이번엔 group→컨텍스트 매핑이 애초에 선언 자체를 막는다."""
    data = _raw("b")
    _utterance(data, utterance_id)["contexts"].append("categoryPrior")
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_option_answer_context_swap_to_none_is_rejected() -> None:
    """[#313 재현 — 분모 불변 케이스] `option_answer` 의 contexts 를 `pendingCart`→`none` 으로
    치환하면 셀 수·분모는 그대로라 기존 수치 가드를 통과하지만, 프롬프트에 PENDING_CART(옵션
    목록)가 실리지 않아 LLM 이 고를 대상 자체가 없어져 `optionAnswer` 가 조용히 ~0/32 로
    떨어진다. 매핑이 이 치환 자체를 거부해야 한다."""
    data = _raw("b")
    _utterance(data, "option-answer-001")["contexts"] = ["none"]
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_switch_context_swap_to_last_recommendations_is_rejected() -> None:
    """[#313 재현 — 분모 불변 케이스] `switch` 의 contexts 를 `pendingCart`→`lastRecommendations`
    로 치환하면 셀 수·분모는 그대로지만, 되물음이 없어 "되물음 상품이 아닌 목록 내 상품" 술어가
    성립하지 않는다."""
    data = _raw("b")
    _utterance(data, "switch-001")["contexts"] = ["lastRecommendations"]
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_cart_control_declaring_a_screen_context_is_still_rejected_after_mapping() -> None:
    """[#313, 대조] #300 전용 검증자를 삭제한 뒤에도 매핑이 같은 위반을 막는지 확인한다."""
    data = _raw("b")
    _utterance(data, "cart-control-001")["contexts"].append("screenTriple")
    with pytest.raises(ValidationError, match="만 선언할 수 있는데"):
        AnchorSet.model_validate(data)


def test_duplicate_context_in_contexts_is_rejected() -> None:
    data = _raw("b")
    _category_utterance(data)["contexts"] = ["categoryPrior", "categoryPrior"]
    with pytest.raises(ValidationError, match="중복이 있습니다"):
        AnchorSet.model_validate(data)


def test_include_screen_true_on_non_screen_context_id_is_rejected() -> None:
    """[리뷰 1차 F-1] contextId 가 screen 이 아닌데 includeScreen 을 켜면 거부해야 한다.

    `GROUP_ALLOWED_CONTEXTS`(contextId 문자열 기준)와 `_screen_utterances_reference_a_screen_context`
    (AnchorSet, includeScreen 플래그 기준)는 같은 뜻이라는 전제로 서 있다 — 이 전제가 깨지면
    (contextId="none" 인데 includeScreen=true) group→컨텍스트 매핑을 우회해 screen 맥락이
    비-screen 발화에 실릴 수 있다. `screenRef` 를 함께 채우는 이유: 비워두면
    `_screen_ref_presence_matches_include_screen` 이 먼저 발화해 대상 검증자에 도달하지 못한다.
    """
    data = _raw("b")
    screen_id = data["screens"][0]["screenId"]
    for context in data["contexts"]:
        if context["contextId"] == "none":
            context["includeScreen"] = True
            context["screenRef"] = screen_id
            break
    with pytest.raises(ValidationError, match="screen 여부"):
        AnchorSet.model_validate(data)


def test_include_screen_false_on_screen_context_id_is_rejected() -> None:
    """[리뷰 1차 F-1] contextId 가 screen 인데 includeScreen 을 끄면 거부해야 한다(역방향).

    `screenRef` 도 함께 비우는 이유: 남겨두면 `_screen_ref_presence_matches_include_screen`
    이 먼저 발화해 대상 검증자에 도달하지 못한다.
    """
    data = _raw("b")
    for context in data["contexts"]:
        if context["contextId"] == "screenSingle":
            context["includeScreen"] = False
            context["screenRef"] = None
            break
    with pytest.raises(ValidationError, match="screen 여부"):
        AnchorSet.model_validate(data)
