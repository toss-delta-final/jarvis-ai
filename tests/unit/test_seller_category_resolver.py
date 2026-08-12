"""카테고리 LLM 택1 폴백 (#506 후속) — 후보 구성·목록 밖 값 거부·실패 degrade.

이 모듈은 "판매자가 대충 말한 카테고리를 AI 가 알아서 찾는다"의 마지막 단계다.
product 에이전트가 카테고리를 못 고른 턴에만 돌고, 여기서도 못 고르면 되묻는다 —
잘못 배정하면 등록 후 되돌릴 수 없으므로 **틀리느니 비운다**가 계약이다.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.seller import category_catalog, category_resolver
from app.agents.seller.schemas import DraftChange, DraftProposal
from app.api import seller as seller_api

_ENTRIES = [
    {"id": "100", "path": ["패션의류/잡화", "남성의류 > 셔츠/남방"]},
    {"id": "101", "path": ["패션의류/잡화", "여성의류 > 셔츠"]},
    {"id": "102", "path": ["패션의류/잡화", "남성의류 > 티셔츠"]},
]


def _settings(snapshot, **overrides) -> SimpleNamespace:
    values = {
        "seller_category_snapshot_path": str(snapshot),
        "seller_category_candidates_k": 2,
        "seller_category_fallback_k_factor": 3,
        "seller_category_resolve_timeout_s": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def catalog(tmp_path, monkeypatch):
    snapshot = tmp_path / "categories.json"
    snapshot.write_text(
        json.dumps({"version": "test.1", "categories": _ENTRIES}, ensure_ascii=False),
        encoding="utf-8",
    )
    settings = _settings(snapshot)
    monkeypatch.setattr(category_catalog, "get_settings", lambda: settings)
    monkeypatch.setattr(category_resolver, "get_settings", lambda: settings)
    category_catalog.reset_catalog_cache()
    yield snapshot
    category_catalog.reset_catalog_cache()


class _FakeModel:
    """with_structured_output 체인 흉내 — ainvoke 가 미리 정한 값을 돌려준다."""

    def __init__(self, result):
        self._result = result
        self.calls: list[list] = []

    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch_model(monkeypatch, model) -> None:
    monkeypatch.setattr(category_resolver, "init_seller_model", lambda _role: model)


def test_wide_candidates_widens_beyond_injection_k(catalog) -> None:
    """폴백 후보는 주입용 k(2)보다 넓다 — 같은 폭으로 다시 물으면 의미가 없다."""
    entries = category_resolver.wide_candidates("셔츠 팔아요")
    assert len(entries) > 2


def test_resolve_picks_from_candidates(catalog, monkeypatch) -> None:
    model = _FakeModel(category_resolver.CategoryPick(category_id="102", reason="반팔 티셔츠"))
    _patch_model(monkeypatch, model)
    entry = asyncio.run(category_resolver.resolve_category("남자 티셔츠 등록해줘"))
    assert entry is not None and entry.id == "102"
    # 후보 블록이 프롬프트에 실제로 실려야 LLM 이 id 를 고를 수 있다.
    assert "[카테고리 후보]" in model.calls[0][1].content


def test_resolve_rejects_out_of_list_id(catalog, monkeypatch) -> None:
    """후보로 보여준 적 없는 id 는 스냅샷에 있어도 받지 않는다(환각 차단)."""
    _patch_model(monkeypatch, _FakeModel(category_resolver.CategoryPick(category_id="999")))
    assert asyncio.run(category_resolver.resolve_category("셔츠")) is None


def test_resolve_empty_pick_is_none(catalog, monkeypatch) -> None:
    """LLM 이 포기하면(빈 문자열) 되묻기로 넘긴다 — 억지 배정 금지."""
    _patch_model(monkeypatch, _FakeModel(category_resolver.CategoryPick(category_id="")))
    assert asyncio.run(category_resolver.resolve_category("셔츠")) is None


def test_resolve_no_candidates_skips_llm(catalog, monkeypatch) -> None:
    """후보 0건이면 LLM 을 부르지 않는다 — 지연만 늘고 지어낼 여지만 준다."""
    model = _FakeModel(category_resolver.CategoryPick(category_id="100"))
    _patch_model(monkeypatch, model)
    assert asyncio.run(category_resolver.resolve_category("냉장고")) is None
    assert model.calls == []


def test_resolve_single_candidate_skips_llm(catalog, monkeypatch) -> None:
    model = _FakeModel(category_resolver.CategoryPick(category_id="100"))
    _patch_model(monkeypatch, model)
    only = [category_catalog.get("101")]
    entry = asyncio.run(category_resolver.resolve_category("셔츠", candidates=only))
    assert entry is not None and entry.id == "101"
    assert model.calls == []


def test_resolve_llm_failure_degrades_to_none(catalog, monkeypatch) -> None:
    """LLM 오류는 되묻기로 흡수한다 — 스트림을 죽이지 않는다."""
    _patch_model(monkeypatch, _FakeModel(RuntimeError("boom")))
    assert asyncio.run(category_resolver.resolve_category("셔츠")) is None


def test_resolve_timeout_degrades_to_none(catalog, monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeModel(TimeoutError()))
    assert asyncio.run(category_resolver.resolve_category("셔츠")) is None


# ── _ensure_draft_category — 초안 카테고리 복구(입구 배선) ──────────────────────


def _proposal(*changes: DraftChange, op: str = "create") -> DraftProposal:
    return DraftProposal(
        op=op,
        product_id=None,
        changes=[
            DraftChange(field="name", before="", after="오버핏 셔츠"),
            DraftChange(field="price", before="", after="32000"),
            DraftChange(field="stock_quantity", before="", after="50"),
            *changes,
        ],
        summary="셔츠 등록",
    )


def _category_of(proposal: DraftProposal) -> str | None:
    return next((c.after for c in proposal.changes if c.field == "category"), None)


def test_ensure_keeps_valid_agent_pick(catalog, monkeypatch) -> None:
    """에이전트가 유효한 id 를 골랐으면 LLM 을 부르지 않는다(happy path 지연 불변)."""
    model = _FakeModel(category_resolver.CategoryPick(category_id="102"))
    _patch_model(monkeypatch, model)
    proposal = _proposal(DraftChange(field="category", before="", after="100"))
    out, revived = asyncio.run(
        seller_api._ensure_draft_category(proposal, message="셔츠", analysis=None, pending=None)
    )
    assert _category_of(out) == "100"
    assert model.calls == []
    assert revived is False  # 이미 유효한 값 — 되살릴 이전 초안이 없다


def test_ensure_fills_missing_category_via_llm(catalog, monkeypatch) -> None:
    """에이전트가 카테고리를 비웠으면 폴백이 채운다 — 되묻기 전 마지막 기회."""
    _patch_model(monkeypatch, _FakeModel(category_resolver.CategoryPick(category_id="102")))
    out, revived = asyncio.run(
        seller_api._ensure_draft_category(
            _proposal(), message="남자 티셔츠 등록해줘", analysis=None, pending=None
        )
    )
    assert _category_of(out) == "102"
    assert revived is False  # LLM 폴백으로 채웠다 — 이전 초안 값 복구가 아니다


def test_ensure_drops_out_of_snapshot_value(catalog, monkeypatch) -> None:
    """LLM 이 경로·이름을 적었고 폴백도 실패하면 그 값을 걷어낸다 — '누락' 안내로 간다."""
    _patch_model(monkeypatch, _FakeModel(category_resolver.CategoryPick(category_id="")))
    proposal = _proposal(DraftChange(field="category", before="", after="남성의류 > 셔츠"))
    out, revived = asyncio.run(
        seller_api._ensure_draft_category(proposal, message="셔츠", analysis=None, pending=None)
    )
    assert _category_of(out) is None
    assert revived is False


def test_ensure_restores_pending_category_on_modify_turn(catalog, monkeypatch) -> None:
    """수정 턴("가격만 바꿔줘")에서 확정해 둔 카테고리가 사라지지 않는다.

    등록 후 변경 불가 필드라, 조용히 다른 값으로 바뀌는 것이 가장 비싼 사고다.
    """
    model = _FakeModel(category_resolver.CategoryPick(category_id="102"))
    _patch_model(monkeypatch, model)
    pending = SimpleNamespace(changes={"category": "101"})
    out, revived = asyncio.run(
        seller_api._ensure_draft_category(
            _proposal(), message="가격만 3만원으로 바꿔줘", analysis=None, pending=pending
        )
    )
    assert _category_of(out) == "101"
    assert model.calls == []  # 기존 값이 있으면 LLM 을 부르지 않는다
    assert revived is True  # [#622] 이전 초안 값을 되살린 경로 — preview note 노출 대상


def test_ensure_skips_non_create_ops(catalog, monkeypatch) -> None:
    """update/delete 는 카테고리를 다루지 않는다(BE I-11 에 필드 자체가 없다)."""
    model = _FakeModel(category_resolver.CategoryPick(category_id="102"))
    _patch_model(monkeypatch, model)
    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[DraftChange(field="price", before="15000", after="12900")],
        summary="가격 인하",
    )
    out, revived = asyncio.run(
        seller_api._ensure_draft_category(
            proposal, message="가격 내려줘", analysis=None, pending=None
        )
    )
    assert out is proposal
    assert revived is False
    assert model.calls == []
