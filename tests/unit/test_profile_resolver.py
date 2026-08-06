"""kind별 결정론적 resolver (SPEC-PROFILE-GRAPH-149 §6.2, REQ-PGRAPH-011~014).

LLM 은 타입 붙은 *제안*만 내고 식별 키는 코드가 확정한다(REQ-PGRAPH-011). 여기 테스트의 축은 셋:
결정론(같은 입력 → 같은 키), 임베딩을 쓰지 않아야 할 kind 가 실제로 안 쓰는가(REQ-PGRAPH-014),
그리고 확신이 없으면 **드롭**하는가(REQ-PGRAPH-012b — never-null 금지).
"""

from unittest.mock import Mock

import pytest

from app.agents.profile.resolver import resolve_triple
from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


NOW = "2026-08-06T00:00:00+00:00"


def _never_embed() -> Mock:
    """호출되면 테스트가 실패해야 하는 자리 — 호출 여부 자체가 단언 대상이다."""
    return Mock(side_effect=AssertionError("embedding must not be called for this kind"))


async def _resolve(settings: Settings, **kwargs: object):
    base: dict = {
        "kind": "brand",
        "label": "소니",
        "anchor_phrase": "소니 이어폰이 좋더라",
        "polarity": "positive",
        "predicate_hint": "likes",
        "settings": settings,
        "now": NOW,
    }
    base.update(kwargs)
    return await resolve_triple(**base)  # type: ignore[arg-type]


# ─────────── 밴드·상품: 임베딩 없는 결정론 경로 (REQ-PGRAPH-014) ───────────


@pytest.mark.parametrize(
    ("kind", "label", "expected_node_id"),
    [
        ("priceBand", "30000-50000", "priceBand:30000-50000"),
        ("priceBand", "0-100000", "priceBand:0-100000"),
        ("ratingBand", "4-5", "ratingBand:4-5"),
        ("product", "12345", "product:12345"),
    ],
)
async def test_rule_kinds_resolve_without_embedding(
    settings: Settings, kind: str, label: str, expected_node_id: str
) -> None:
    embed = _never_embed()

    resolved = await _resolve(settings, kind=kind, label=label, embed=embed)

    assert resolved is not None
    assert resolved.node.node_id == expected_node_id
    assert resolved.node.verified is True  # 자기완결적 판정 — 외부 어휘가 필요 없다
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.method == "rule"
    embed.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("priceBand", "3~5만원"),  # 자연어는 파싱하지 않는다
        ("priceBand", "50000-30000"),  # min >= max
        ("priceBand", "-1000-5000"),
        ("priceBand", "가성비"),
        ("ratingBand", "4-6"),  # 평점 스케일(0~5) 밖
        ("ratingBand", "높음"),
        ("product", "소니 WH-1000XM5"),  # 상품은 숫자 id 정확 일치만
        ("product", ""),
    ],
)
async def test_rule_kinds_drop_on_unparseable_label(
    settings: Settings, kind: str, label: str
) -> None:
    """엄격 파서가 실패하면 드롭한다 — 추측해서 만든 밴드는 틀린 노드가 된다(REQ-PGRAPH-012b)."""
    embed = _never_embed()

    assert await _resolve(settings, kind=kind, label=label, embed=embed) is None
    embed.assert_not_called()


# ─────────── 어휘 없는 kind: seam 만 만들고 verified:false (REQ-PGRAPH-013) ───────────


@pytest.mark.parametrize("kind", ["brand", "attribute", "situation"])
async def test_kinds_without_vocabulary_are_kept_unverified(settings: Settings, kind: str) -> None:
    """브랜드 통제 어휘(C-28/OPEN-G2)가 없어도 동작해야 한다 — 노출하되 신뢰하지 않는다."""
    embed = _never_embed()

    resolved = await _resolve(settings, kind=kind, label="소니", embed=embed)

    assert resolved is not None
    assert resolved.node.verified is False
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.method == "no_vocabulary"
    assert resolved.node.resolution.distance is None
    embed.assert_not_called()


async def test_brand_lexicon_converges_notation_variants(settings: Settings) -> None:
    """#115 회귀 — 표기 변형이 하나의 node_id 로 수렴한다.

    어휘는 color_synonyms(#258)와 같은 모양의 alias -> canonical 매핑이다. 정규화만으로는
    `소니`와 `SONY` 가 문자 체계가 달라 합쳐지지 않으므로, 수렴은 어휘의 몫이다.
    """
    lexicon = {"소니": "소니", "SONY": "소니", "sony": "소니"}

    korean = await _resolve(settings, label="소니", brand_lexicon=lexicon, embed=_never_embed())
    roman = await _resolve(settings, label="SONY", brand_lexicon=lexicon, embed=_never_embed())

    assert korean is not None and roman is not None
    assert korean.node.node_id == roman.node.node_id == "brand:소니"
    assert korean.edge_id == roman.edge_id
    assert korean.node.verified is True
    assert korean.node.resolution is not None
    assert korean.node.resolution.method == "exact"


async def test_brand_typo_drops_when_lexicon_exists(settings: Settings) -> None:
    """어휘가 있는데 못 붙으면 드롭한다 — 틀린 노드는 측정된 손실을 만든다(REQ-PGRAPH-012b)."""
    lexicon = {"소니": "소니", "SONY": "소니"}

    assert (
        await _resolve(settings, label="쏘니", brand_lexicon=lexicon, embed=_never_embed()) is None
    )


async def test_brand_lexicon_absent_keeps_typo_as_unverified_node(settings: Settings) -> None:
    """어휘가 없으면 오타도 구분할 수 없다 — 드롭이 아니라 미검증으로 남긴다(C-28 미해결 상태)."""
    resolved = await _resolve(settings, label="쏘니", embed=_never_embed())

    assert resolved is not None
    assert resolved.node.verified is False


# ─────────── category: 유일한 임베딩 경로 ───────────


async def test_category_exact_match_skips_embedding(settings: Settings) -> None:
    """어휘에 그대로 있으면 임베딩을 부르지 않는다 — DB 검증값은 비교 대상이 아니다(#59)."""
    embed = _never_embed()
    exact = Mock(return_value={"음향가전 > 블루투스 이어폰"})

    resolved = await _resolve(
        settings,
        kind="category",
        label="음향가전 > 블루투스 이어폰",
        embed=embed,
        category_exact=exact,
    )

    assert resolved is not None
    assert resolved.node.node_id == "category:음향가전 > 블루투스 이어폰"
    assert resolved.node.label == "음향가전 > 블루투스 이어폰"
    assert resolved.node.verified is True
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.method == "exact"
    embed.assert_not_called()


async def test_category_embedding_within_distance_cut_is_verified(settings: Settings) -> None:
    embed = Mock(return_value=[[0.1] * 4])
    search = Mock(return_value=[("음향가전 > 블루투스 이어폰", 0.05), ("음향가전 > 헤드폰", 0.30)])

    resolved = await _resolve(
        settings,
        kind="category",
        label="블루투스 이어폰",
        anchor_phrase="블루투스 이어폰 찾고 있어",
        embed=embed,
        category_exact=Mock(return_value=set()),
        category_search=search,
    )

    assert resolved is not None
    assert resolved.node.verified is True
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.method == "embedding"
    assert resolved.node.resolution.distance == 0.05
    assert resolved.node.resolution.margin == 0.25
    assert resolved.node.resolution.lexicon_version is not None


async def test_category_anchor_is_the_utterance_phrase_not_the_llm_label(
    settings: Settings,
) -> None:
    """앵커는 LLM 라벨이 아니라 발화 파생 구절이다(REQ-PGRAPH-012a).

    추상 라벨은 카테고리명과의 문자열 겹침으로 가짜 근접을 만든다 — #115 실측에서 라벨 앵커
    채택 12건 중 11건이 오분류였다.
    """
    embed = Mock(return_value=[[0.1] * 4])
    search = Mock(return_value=[("음향가전 > 블루투스 이어폰", 0.05)])

    await _resolve(
        settings,
        kind="category",
        label="블루투스 이어폰",
        anchor_phrase="퇴근길에 낄 이어폰 찾고 있어",
        embed=embed,
        category_exact=Mock(return_value=set()),
        category_search=search,
    )

    assert embed.call_args.args[0] == ["퇴근길에 낄 이어폰 찾고 있어"]


async def test_category_uses_injected_settings_dsn(settings: Settings) -> None:
    """주입받은 `Settings` 의 dsn 을 쓴다 — 전역 설정을 다시 읽지 않는다 (PR #410 리뷰).

    전역 `get_settings()` 를 부르면 호출자가 넘긴 Settings 가 조용히 무시된다. 콜러블만 모킹하면
    dsn 이 검증되지 않아 이 불일치가 테스트를 통과해버린다 — 인자로 실제로 전달되는지 본다.
    """
    injected = Settings(_env_file=None, catalog_db_url="postgresql://injected/catalog")
    exact = Mock(return_value=set())
    search = Mock(return_value=[("음향가전 > 블루투스 이어폰", 0.05), ("음향가전 > 헤드폰", 0.30)])

    await _resolve(
        injected,
        kind="category",
        label="블루투스 이어폰",
        embed=Mock(return_value=[[0.1] * 4]),
        category_exact=exact,
        category_search=search,
    )

    assert exact.call_args.args[1] == "postgresql://injected/catalog"
    assert search.call_args.args[1] == "postgresql://injected/catalog"


async def test_category_beyond_distance_cut_drops(settings: Settings) -> None:
    embed = Mock(return_value=[[0.1] * 4])
    far = settings.graph_node_distance_max + 0.05
    search = Mock(return_value=[("음향가전 > 헤드폰", far), ("음향가전 > 스피커", far + 0.01)])

    resolved = await _resolve(
        settings,
        kind="category",
        label="이어폰",
        embed=embed,
        category_exact=Mock(return_value=set()),
        category_search=search,
    )

    assert resolved is None


async def test_category_beyond_cut_but_confident_margin_is_accepted(settings: Settings) -> None:
    """거리는 도메인 어휘에 오염되지만 margin 은 차분이라 상쇄된다(#59 §4.3)."""
    embed = Mock(return_value=[[0.1] * 4])
    far = settings.graph_node_distance_max + 0.05
    wide = far + settings.graph_node_override_margin + 0.01
    search = Mock(return_value=[("음향가전 > 헤드폰", far), ("음향가전 > 스피커", wide)])

    resolved = await _resolve(
        settings,
        kind="category",
        label="이어폰",
        embed=embed,
        category_exact=Mock(return_value=set()),
        category_search=search,
    )

    assert resolved is not None
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.distance == pytest.approx(far)


async def test_category_single_hit_margin_is_none_and_not_override_eligible(
    settings: Settings,
) -> None:
    """히트 1건이면 확신을 잴 수 없다 — margin None 은 0.0(동점)이 아니며 예외 대상도 아니다."""
    embed = Mock(return_value=[[0.1] * 4])
    far = settings.graph_node_distance_max + 0.05
    search = Mock(return_value=[("음향가전 > 헤드폰", far)])

    assert (
        await _resolve(
            settings,
            kind="category",
            label="이어폰",
            embed=embed,
            category_exact=Mock(return_value=set()),
            category_search=search,
        )
        is None
    )


async def test_category_drops_when_embedding_fails(settings: Settings) -> None:
    """임베딩 장애는 예외로 전파하지 않는다 — consolidation 전체가 영구 RETRYABLE 이 된다.

    GOOGLE_API_KEY 미구성이면 embed_texts 가 EmbeddingError 를 던지는데, 그걸 그대로 올리면
    로컬은 통과하고 키 없는 환경만 폭발한다(docs/lessons.md GOOGLE_API_KEY 항목).
    """
    embed = Mock(side_effect=RuntimeError("embedding backend down"))

    resolved = await _resolve(
        settings,
        kind="category",
        label="이어폰",
        embed=embed,
        category_exact=Mock(return_value=set()),
        category_search=Mock(return_value=[]),
    )

    assert resolved is None


async def test_category_drops_when_exact_lookup_fails(settings: Settings) -> None:
    """어휘 조회 장애도 마찬가지로 드롭 — 사전이 비면 조용히 무필터가 되는 함정을 피한다."""
    resolved = await _resolve(
        settings,
        kind="category",
        label="이어폰",
        embed=Mock(side_effect=RuntimeError("no embedding either")),
        category_exact=Mock(side_effect=RuntimeError("catalog down")),
        category_search=Mock(return_value=[]),
    )

    assert resolved is None


# ─────────── predicate 결정: 코드가 정한다 ───────────


@pytest.mark.parametrize(
    ("kind", "label", "polarity", "expected"),
    [
        ("brand", "소니", "positive", "likes"),
        ("brand", "소니", "negative", "avoids"),
        ("priceBand", "30000-50000", "positive", "prefers"),
        ("ratingBand", "4-5", "positive", "prefers"),
        ("attribute", "노이즈캔슬링", "positive", "prefers"),
        ("attribute", "노이즈캔슬링", "negative", "avoids"),
        ("situation", "출퇴근", "positive", "interestedIn"),
        ("product", "12345", "negative", "avoids"),
    ],
)
async def test_predicate_is_decided_by_polarity_and_node_type(
    settings: Settings, kind: str, label: str, polarity: str, expected: str
) -> None:
    """predicateHint 는 힌트일 뿐이다 — 결정은 코드가 한다(REQ-PGRAPH-011)."""
    resolved = await _resolve(
        settings,
        kind=kind,
        label=label,
        polarity=polarity,
        predicate_hint="likes",
        embed=_never_embed(),
    )

    assert resolved is not None
    assert resolved.predicate == expected
    assert resolved.edge_key == f"{expected}|{resolved.node.node_id}"


async def test_purchased_hint_is_refused(settings: Settings) -> None:
    """대화에서 구매 사실을 만들어낼 수 없다.

    `purchased` 의 원천은 질의 시점 구매 이력(I-19)이지 발화가 아니다. 발화에서 나온 구매
    언급을 `likes` 로 강등해 살리는 선택도 있지만, 사용자가 지울 수 있는 경로(#150)가 아직
    없어서 잘못 만든 노드를 되돌릴 방법이 없다 — 되돌릴 수 없는 쪽의 오류를 피한다.
    """
    assert (await _resolve(settings, predicate_hint="purchased", embed=_never_embed())) is None


@pytest.mark.parametrize("kind", ["color", "", "Brand"])
async def test_unknown_kind_drops(settings: Settings, kind: str) -> None:
    assert await _resolve(settings, kind=kind, embed=_never_embed()) is None


# ─────────── 결정론·상한 ───────────


async def test_resolution_is_deterministic_across_calls(settings: Settings) -> None:
    """같은 관측을 두 번 처리하면 같은 식별자가 나와야 재생 동일성이 성립한다(REQ-PGRAPH-015)."""
    first = await _resolve(settings, embed=_never_embed())
    second = await _resolve(settings, embed=_never_embed())

    assert first is not None and second is not None
    assert first.node.node_id == second.node.node_id
    assert first.edge_key == second.edge_key
    assert first.edge_id == second.edge_id


async def test_label_is_truncated_to_configured_cap(settings: Settings) -> None:
    """라벨 상한은 설정 주입이다 — 하드코딩 금지(§5 "모든 길이 상한은 설정 주입")."""
    tight = Settings(_env_file=None, profile_graph_label_max_chars=4)

    resolved = await _resolve(tight, label="가나다라마바사", embed=_never_embed())

    assert resolved is not None
    assert resolved.node.label == "가나다라"


async def test_blank_label_drops(settings: Settings) -> None:
    assert await _resolve(settings, label="   ", embed=_never_embed()) is None
