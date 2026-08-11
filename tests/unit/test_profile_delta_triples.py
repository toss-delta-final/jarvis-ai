"""델타 추출 → 게이트 → resolve-at-write (SPEC-PROFILE-GRAPH-149 §6.2, REQ-PROF-086 v0.7.1).

트리플의 **식별자 확정은 1단계**(게이트 통과 직후)다. 배치마다 다시 resolve 하면 거리 임계·통제
어휘가 바뀔 때 같은 fact 가 다른 node_id 로 붙어 tombstone 을 우회한다(REQ-PGRAPH-010).

프롬프트 교체는 동작 중인 LLM 계약을 바꾸는 일이라(OPEN-G8) `profile_graph_delta_enabled` 로
롤백 경로를 남긴다 — 그 스위치가 실제로 두 경로를 가르는지도 여기서 고정한다.
"""

import json

from app.agents.profile import builder as builder_mod
from app.agents.profile.builder import generate_session_delta
from app.agents.profile.store import get_profile_store
from app.core.config import Settings, get_settings
from app.core.conversation import conversation_key


class _StructuredLLM:
    """구조화 제안을 내는 델타 추출 fake. system 분기 문자열은 기존 fake 와 같다."""

    def __init__(self, deltas: list[dict] | None = None) -> None:
        self._deltas = (
            deltas
            if deltas is not None
            else [
                {
                    "fact": "소니 브랜드를 선호한다",
                    "kind": "brand",
                    "label": "소니",
                    "anchorPhrase": "소니 이어폰이 좋더라",
                    "polarity": "positive",
                    "predicateHint": "likes",
                    "salience": 0.9,
                    "explicit": True,
                    "repetitionEma": 0.0,
                }
            ]
        )
        self.systems: list[str] = []
        self.budgets: list[int] = []

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        self.systems.append(system)
        self.budgets.append(max_tokens)  # 예산이 **설정에서 온 값인지** 재려면 붙잡아야 한다
        if "델타 추출기" in system:
            return json.dumps({"deltas": self._deltas}, ensure_ascii=False)
        return "# 요약"

    async def stream(self, *, system, user, tier, max_tokens=1024):
        yield "x"


async def _run(llm, settings: Settings | None = None, *, user: str = "7", utterance: str = "소니"):
    store = await get_profile_store()
    key = conversation_key(user, "s1")
    await store.append_session_ctx(key, utterance)
    result = await generate_session_delta(
        user, key, profile_watermark=1, llm=llm, settings=settings or get_settings()
    )
    return store, result


# ─────────── 프롬프트 계약 ───────────


def test_delta_prompt_keeps_fake_branch_marker() -> None:
    """기존 테스트 fake 가 "델타 추출기" 문자열로 분기한다 — 첫 줄을 바꾸면 전부 조용히 깨진다."""
    assert "델타 추출기" in builder_mod._DELTA_SYSTEM
    assert "델타 추출기" in builder_mod._DELTA_SYSTEM_LEGACY


def test_delta_prompt_asks_for_typed_proposals_not_keys() -> None:
    """LLM 은 식별자를 만들지 않는다(REQ-PGRAPH-011) — 프롬프트가 요구하는 것은 제안뿐이다."""
    prompt = builder_mod._DELTA_SYSTEM

    for field in ("kind", "label", "anchorPhrase", "polarity", "predicateHint"):
        assert field in prompt
    assert "node_id" not in prompt
    assert "edge_id" not in prompt


def test_delta_prompt_specifies_band_label_format() -> None:
    """밴드는 엄격 파서가 받는다 — 프롬프트가 형식을 지정하지 않으면 밴드 노드가 안 생긴다."""
    assert "30000-50000" in builder_mod._DELTA_SYSTEM


def test_delta_prompt_forbids_inventing_a_missing_bound() -> None:
    """한쪽 경계만 있는 표현에 **없는 쪽을 지어내지 말라**고 지시해야 한다 (#581).

    이 지시가 없던 동안 모델은 형식을 맞추려고 경계를 만들어 냈다 — 실측 베이스라인에
    하한을 지어낸 `0-100000` 과 상한이 센티널인 `100000-999999999` 가 남아 있다.
    파서가 열린 밴드를 받아도 프롬프트가 그걸 **제안하지 않으면** 아무것도 달라지지 않는다.
    """
    prompt = builder_mod._DELTA_SYSTEM

    assert "-50000" in prompt
    assert "100000-" in prompt
    assert "지어내지" in prompt


# ─────────── resolve-at-write ───────────


async def test_promoted_delta_stores_resolved_triple() -> None:
    store, (promoted, _) = await _run(_StructuredLLM())

    assert promoted == ["소니 브랜드를 선호한다"]
    records = await store.get_fact_records("7")
    assert len(records) == 1
    triple = records[0].graph_triples[0]
    assert triple["node"]["node_id"] == "brand:소니"
    assert triple["predicate"] == "likes"
    assert triple["edge_key"] == "likes|brand:소니"
    assert triple["edge_id"].startswith("e_")
    assert triple["salience"] == 0.9  # confidence EMA 재료(REQ-PGRAPH-015)
    assert triple["source"] == "conversation"


async def test_resolver_uses_anchor_phrase_from_delta(monkeypatch) -> None:
    """앵커는 LLM 라벨이 아니라 발화 파생 구절이다(REQ-PGRAPH-012a)."""
    seen: dict = {}

    async def _spy(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(builder_mod, "resolve_triple", _spy)

    await _run(_StructuredLLM())

    assert seen["anchor_phrase"] == "소니 이어폰이 좋더라"
    assert seen["label"] == "소니"
    assert seen["kind"] == "brand"


async def test_gate_rejection_skips_resolve_entirely(monkeypatch) -> None:
    """게이트를 못 넘은 델타는 저장도 resolve 도 하지 않는다 — 임베딩 왕복을 낭비하지 않는다."""
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(builder_mod, "resolve_triple", _spy)
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "잡담",
                "kind": "brand",
                "label": "소니",
                "anchorPhrase": "그냥 지나가는 말",
                "polarity": "positive",
                "predicateHint": "likes",
                "salience": 0.1,
                "explicit": False,
                "repetitionEma": 0.0,
            }
        ]
    )

    store, (promoted, _) = await _run(llm)

    assert promoted == []
    assert calls == []
    assert await store.get_fact_records("7") == []


async def test_unresolvable_delta_still_stores_fact_without_triples() -> None:
    """트리플을 못 만들어도 fact 는 남는다 — 문서에 안 실리고 unprojected_count 로만 센다.

    이게 없으면 resolver 가 드롭한 취향이 요약에서도 통째로 사라진다(REQ-PGRAPH-004).
    """
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "가성비를 중시한다",
                "kind": "priceBand",
                "label": "가성비",  # 엄격 파서가 거부한다
                "anchorPhrase": "가성비 좋은 걸로",
                "polarity": "positive",
                "predicateHint": "prefers",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )

    store, (promoted, _) = await _run(llm)

    assert promoted == ["가성비를 중시한다"]
    records = await store.get_fact_records("7")
    assert len(records) == 1
    assert records[0].graph_triples == []


async def test_legacy_shaped_delta_is_backward_compatible() -> None:
    """구 스키마(fact + 게이트 신호만) 응답도 그대로 승격된다 — 프롬프트 전환 중 안전판."""
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "3~5만원 무선이어폰 선호",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )

    store, (promoted, _) = await _run(llm)

    assert promoted == ["3~5만원 무선이어폰 선호"]
    records = await store.get_fact_records("7")
    assert records[0].graph_triples == []


async def test_purchased_hint_stores_fact_but_no_triple() -> None:
    """대화에서 구매 사실을 만들지 않는다 — 취향 텍스트는 남기고 edge 만 안 만든다."""
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "소니 이어폰을 구매했다",
                "kind": "brand",
                "label": "소니",
                "anchorPhrase": "소니 이어폰 샀어",
                "polarity": "positive",
                "predicateHint": "purchased",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )

    store, (promoted, _) = await _run(llm)

    assert promoted == ["소니 이어폰을 구매했다"]
    records = await store.get_fact_records("7")
    assert records[0].graph_triples == []


# ─────────── 롤백 스위치 (OPEN-G8) ───────────


async def test_flag_off_uses_legacy_prompt_and_skips_resolve(monkeypatch) -> None:
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(builder_mod, "resolve_triple", _spy)
    llm = _StructuredLLM()
    settings = Settings(_env_file=None, profile_graph_delta_enabled=False)

    store, (promoted, _) = await _run(llm, settings)

    assert llm.systems[0] == builder_mod._DELTA_SYSTEM_LEGACY
    assert calls == []  # 구 경로는 트리플을 만들지 않는다
    records = await store.get_fact_records("7")
    assert promoted == ["소니 브랜드를 선호한다"]
    assert records[0].graph_triples == []


async def test_flag_on_uses_structured_prompt() -> None:
    llm = _StructuredLLM()

    await _run(llm, Settings(_env_file=None, profile_graph_delta_enabled=True))

    assert llm.systems[0] == builder_mod._DELTA_SYSTEM


# ─────────── 실패 격리 ───────────


async def test_resolver_failure_does_not_break_promotion(monkeypatch) -> None:
    """resolver 가 터져도 fact 승격은 계속된다 — 그래프는 부가물이지 승격의 전제가 아니다."""

    async def _boom(**kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(builder_mod, "resolve_triple", _boom)

    store, (promoted, _) = await _run(_StructuredLLM())

    assert promoted == ["소니 브랜드를 선호한다"]
    records = await store.get_fact_records("7")
    assert records[0].graph_triples == []


# ─────────── 출력 예산 배선 ───────────


async def test_delta_call_uses_the_configured_output_budget() -> None:
    """델타 호출이 **설정값을 실제로 넘기는지** 잰다 — 값만 있고 배선이 없으면 소용없다.

    하드코딩 800 이 운영 smart tier(reasoning 모델)에서 추론 토큰에 소진돼 분포 프로브 4세션 중
    2건이 `LengthFinishReasonError` 로 죽었고(#325 계열), 이관 후 0건이 됐다. 그래서 이 배선이
    되돌려지면 같은 장애가 돌아온다 — Settings 필드 존재만 확인하는 테스트는 그것을 못 잡는다
    (변이 검증에서 실제로 드러난 공백이다).
    """
    tuned = Settings(_env_file=None, profile_delta_max_tokens=4242)
    llm = _StructuredLLM()

    await _run(llm, tuned)

    assert llm.budgets == [4242]
