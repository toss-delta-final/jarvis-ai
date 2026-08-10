""" "기억해" 원문·세션 델타·요약·세션 버퍼의 하드 PII 게이트 (이슈 #321).

경계마다 처방이 다르다: fact/요약 저장은 **드롭**(REQ-PGRAPH-071), 세션 버퍼는 **치환**(델타
추출 LLM 입력이라 세탁 경로를 구조적으로 닫는다). 합성 값만 쓴다(전화는 `0000` 계열, 이메일은
`example.com`).
"""

from __future__ import annotations

import json

from app.agents.profile.builder import generate_session_delta, record_remember
from app.agents.profile.store import get_profile_store
from app.core.config import get_settings
from app.core.conversation import conversation_key

_PII_PHONE = "010-0000-0000"
_PII_EMAIL = "tester@example.com"


class _StructuredLLM:
    """구조화 제안(kind/label/anchorPhrase)을 내는 델타 추출 fake (test_profile_delta_triples.py 와 동형)."""

    def __init__(self, deltas: list[dict]) -> None:
        self._deltas = deltas

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        if "델타 추출기" in system:
            return json.dumps({"deltas": self._deltas}, ensure_ascii=False)
        return "# 요약"

    async def stream(self, *, system, user, tier, max_tokens=1024):
        yield "x"


# ─────────── "기억해" hot-path (record_remember) — 드롭 ───────────


async def test_record_remember_drops_fact_with_phone_number() -> None:
    await record_remember("u1", f"제 번호는 {_PII_PHONE} 이고 무선 이어폰 좋아해요")
    store = await get_profile_store()
    assert await store.get_facts("u1") == []


async def test_record_remember_drops_fact_with_rrn_even_with_bad_checksum() -> None:
    await record_remember("u2", "제 주민번호는 990101-1234567 입니다")
    store = await get_profile_store()
    assert await store.get_facts("u2") == []


async def test_record_remember_does_not_raise_on_pii_or_malformed_input() -> None:
    """게이트가 예외를 던지면 hot-path 턴이 500 이 된다 — 어떤 입력도 raise 하면 안 된다."""
    for text in (
        f"{_PII_PHONE} {_PII_EMAIL} 990101-1234567 sk-abcdefghijklmnop",
        "​".join("01000000000"),  # zero-width 우회 시도
        "정상적인 취향 발화입니다",
        "",
    ):
        await record_remember("u3", text)  # 예외 없이 반환되면 통과


async def test_record_remember_checks_pii_before_truncation_boundary() -> None:
    """절단이 먼저면 `010-1234-` 처럼 잘려 정규식이 못 잡는다 — 검사가 절단보다 먼저여야 한다."""
    settings = get_settings()
    cap = settings.profile_fact_char_cap
    # 번호가 cap 경계에 걸치도록 앞을 채운다: padding(cap-5) + 전화번호(13자) → cap 훨씬 이후에
    # 번호가 시작하지만, 절단 전 원문 검사라면 여전히 잡혀야 한다.
    padding = "가" * (cap - 5)
    text = f"{padding}{_PII_PHONE}"
    await record_remember("u4", text)
    store = await get_profile_store()
    assert await store.get_facts("u4") == []


async def test_record_remember_still_stores_clean_fact() -> None:
    await record_remember("u5", "겨울 등산 자주 감")
    store = await get_profile_store()
    assert "겨울 등산 자주 감" in await store.get_facts("u5")


# ─────────── 세션 델타 승격 경로 (generate_session_delta → add_fact) — 드롭 ───────────


async def test_promoted_fact_with_pii_is_not_stored() -> None:
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": f"제 번호는 {_PII_PHONE} 인데 무선이어폰 선호",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )
    store = await get_profile_store()
    key = conversation_key("u6", "s1")
    await store.append_session_ctx(key, "취향 발화")
    promoted, _ = await generate_session_delta(
        "u6", key, profile_watermark=1, llm=llm, settings=get_settings()
    )
    # generate_session_delta 자체는 "게이트를 통과했다"고 promoted 에 담지만(호출자 관측용),
    # 저장소에는 실제로 남지 않아야 한다 — REQ-PGRAPH-071 은 저장 관문이다.
    assert await store.get_facts("u6") == []
    assert promoted == [f"제 번호는 {_PII_PHONE} 인데 무선이어폰 선호"]


# ─────────── triple 경로 — label/anchorPhrase 히트 시 fact 까지 통째로 드롭 ───────────


async def test_triple_anchor_phrase_pii_drops_entire_fact() -> None:
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "소니 브랜드를 선호한다",  # fact 자체는 깨끗하다
                "kind": "brand",
                "label": "소니",
                "anchorPhrase": f"소니 이어폰 사려는데 제 번호는 {_PII_PHONE} 이에요",
                "polarity": "positive",
                "predicateHint": "likes",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )
    store = await get_profile_store()
    key = conversation_key("u7", "s1")
    await store.append_session_ctx(key, "소니 취향 발화")
    await generate_session_delta("u7", key, profile_watermark=1, llm=llm, settings=get_settings())
    # anchorPhrase 만 히트했어도 fact 를 살리는 절충은 하지 않는다 — 통째로 버린다.
    assert await store.get_facts("u7") == []


async def test_triple_label_pii_drops_entire_fact() -> None:
    llm = _StructuredLLM(
        deltas=[
            {
                "fact": "선호 브랜드가 있다",
                "kind": "brand",
                "label": _PII_EMAIL,
                "anchorPhrase": "이 브랜드가 좋더라",
                "polarity": "positive",
                "predicateHint": "likes",
                "salience": 0.9,
                "explicit": True,
                "repetitionEma": 0.0,
            }
        ]
    )
    store = await get_profile_store()
    key = conversation_key("u8", "s1")
    await store.append_session_ctx(key, "브랜드 취향 발화")
    await generate_session_delta("u8", key, profile_watermark=1, llm=llm, settings=get_settings())
    assert await store.get_facts("u8") == []


async def test_promoted_fact_without_pii_still_keeps_triple() -> None:
    """PII 게이트가 정상 트리플까지 죽이지 않는지 대조군."""
    llm = _StructuredLLM(
        deltas=[
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
    store = await get_profile_store()
    key = conversation_key("u9", "s1")
    await store.append_session_ctx(key, "소니 취향 발화")
    await generate_session_delta("u9", key, profile_watermark=1, llm=llm, settings=get_settings())
    records = await store.get_fact_records("u9")
    assert len(records) == 1
    assert records[0].graph_triples[0]["node"]["node_id"] == "brand:소니"


# ─────────── 요약 저장 — 드롭 + 기존 요약 보존 ───────────


async def test_set_summary_rejects_pii_markdown_and_preserves_existing() -> None:
    store = await get_profile_store()
    ok = await store.set_summary(
        "u10", "# 취향 요약\n- 무선이어폰 선호", "2026-08-10T00:00:00+00:00"
    )
    assert ok is True

    blocked = await store.set_summary(
        "u10", f"# 취향 요약\n- 연락처 {_PII_PHONE}", "2026-08-10T01:00:00+00:00"
    )
    assert blocked is False

    summary = await store.get_summary("u10")
    assert summary is not None
    assert summary.markdown == "# 취향 요약\n- 무선이어폰 선호"


# ─────────── 세션 버퍼 — 치환(드롭 아님) ───────────


async def test_append_session_ctx_redacts_phone_number() -> None:
    store = await get_profile_store()
    key = conversation_key("u11", "s1")
    await store.append_session_ctx(key, f"제 번호는 {_PII_PHONE} 입니다 무선이어폰 좋아해요")
    buffered = await store.get_session_ctx(key)
    assert len(buffered) == 1
    assert "0000" not in buffered[0]
    assert "[전화번호]" in buffered[0]
    assert "무선이어폰" in buffered[0]  # 치환은 매치 구간만 — 나머지 원문은 보존


async def test_append_session_ctx_leaves_clean_text_untouched() -> None:
    store = await get_profile_store()
    key = conversation_key("u12", "s1")
    await store.append_session_ctx(key, "3만원대 무선 이어폰 위주로 보고 있어요")
    buffered = await store.get_session_ctx(key)
    assert buffered == ["3만원대 무선 이어폰 위주로 보고 있어요"]
