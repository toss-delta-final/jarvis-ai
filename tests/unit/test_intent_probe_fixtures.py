"""intent 라우팅 프로브 앵커 정답지 — 스키마·해시 게이트·컨텍스트 조립 (#260)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.intent_probe.loader import (
    FIXTURE_DIR,
    build_cells,
    build_context_kwargs,
    load_anchor_set,
    resolve_fixture_path,
)
from evals.intent_probe.schema import AnchorSet

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
    # [#84] 카테고리 승계 3분기 — 리파인 4 · 리셋 4 · 교체 3.
    "category_action": 11,
}


def _raw(name: str = "b") -> dict:
    return json.loads(resolve_fixture_path(name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["a", "b"])
def test_committed_anchor_sets_load_and_match_manifest_hash(name: str) -> None:
    anchors = load_anchor_set(name)
    assert anchors.fixture_version == f"intent-probe-anchors-{name}-v2"


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


def test_cell_count_is_64_and_matches_group_context_product() -> None:
    anchors = load_anchor_set("b")
    cells = build_cells(anchors)
    # 발화 × 컨텍스트: 대조군 18 + 지시대명사 12 + 옵션 4 + 전환 7 + 주문 6 + 일반 6
    # + [#84] 카테고리 11(단일 컨텍스트) = 64
    assert len(cells) == 64
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
        "category_action": 11,
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
    kwargs = build_context_kwargs(anchors, context)
    assert kwargs == {"prior_filters": None, "last_recommendations": None, "pending_cart": None}


def test_build_context_kwargs_last_recommendations_shape() -> None:
    anchors = load_anchor_set("b")
    context = next(c for c in anchors.contexts if c.context_id == "lastRecommendations")
    kwargs = build_context_kwargs(anchors, context)
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
    kwargs = build_context_kwargs(anchors, context)
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
    with pytest.raises(ValidationError, match="categoryPrior"):
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
    kwargs = build_context_kwargs(anchors, context)
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
    kwargs = build_context_kwargs(anchors, context)
    assert kwargs["prior_filters"].semantic_query == anchors.prior_filters["semanticQuery"]
    assert kwargs["prior_filters"].category is None
