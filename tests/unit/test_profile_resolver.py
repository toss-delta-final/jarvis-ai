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
        # **도메인 경계는 접힌다**(#581) — 가격은 늘 0 이상이라 하한 0 은 아무것도 안 거른다.
        # 접지 않으면 `"0-100000"` 과 `"-100000"` 이 같은 취향인데 별개 노드가 된다.
        ("priceBand", "0-100000", "priceBand:-100000"),
        ("ratingBand", "4-5", "ratingBand:4-"),  # 평점 상한 5 도 같은 이유로 접힌다
        # 베이스라인에 실재하던 센티널 상한도 같은 규칙으로 접힌다
        ("priceBand", f"100000-{9_223_372_036_854_775_807}", "priceBand:100000-"),
        ("product", "12345", "product:12345"),
        # 열린 밴드(#581) — 한쪽 경계만 있는 취향("5만원 이하"·"평점 4점 이상")을 담는 자리.
        # 이게 없으면 추출 LLM 이 없는 경계를 지어낸다(실측: 하한 0, 상한 999999999).
        ("priceBand", "-50000", "priceBand:-50000"),
        ("priceBand", "100000-", "priceBand:100000-"),
        ("ratingBand", "4-", "ratingBand:4-"),
        ("ratingBand", "5-", "ratingBand:5-"),
    ],
)
async def test_rule_kinds_resolve_without_embedding(
    settings: Settings, kind: str, label: str, expected_node_id: str
) -> None:
    embed = _never_embed()

    resolved = await _resolve(settings, kind=kind, label=label, embed=embed)

    assert resolved is not None
    assert resolved.node.node_id == expected_node_id
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.method == "rule"
    embed.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "label", "expected_verified"),
    [
        ("priceBand", "30000-50000", True),  # 자기완결 — 밴드는 자기 라벨이 곧 정의다
        ("ratingBand", "4-5", True),
        # 열린 밴드도 자기완결이다(#581) — "50000원 이하"는 경계가 하나여도 그 자체로 정의다
        ("priceBand", "-50000", True),
        ("ratingBand", "4-", True),
        ("product", "12345", False),  # 외부 엔티티 **참조** — 통제 어휘에 스냅되는 게 아니다
    ],
)
async def test_rule_kinds_claim_verified_only_when_self_contained(
    settings: Settings, kind: str, label: str, expected_verified: bool
) -> None:
    """`verified` 는 "통제 어휘에 스냅됐는가"다 — 정규식 통과는 그 축의 답이 아니다.

    밴드는 라벨 자체가 정의라(가격 30000~50000) 외부 어휘 없이 자기완결적으로 참이다. 반면
    `product` 는 **외부 엔티티를 가리키는 참조**다 — 숫자 형식이 맞다고 그 상품이 있다는 뜻이
    아니고, 있더라도 품절·판매종료로 사라진다(#310). 그래서 쓰기 시점 존재 검증은 애초에 답이
    될 수 없고(읽을 때 또 틀린다), 존재 확인은 소비 시점(#150 → Spring 조회)의 책임이다.
    AI 는 상품 원본을 소유하지 않는다(CLAUDE.md) — pg-catalog `products` 는 I-17 배치가 처리한
    분만 있어 대조 대상으로도 부적합하다(신상품이 거짓 음성이 된다).

    `verified=False` 는 미구현 표식이 아니라 **정확한 답**이다. REQ-PGRAPH-013 이 이 상태를
    정의한다 — "노출은 하되 신뢰하지 않는다". `brand` 도 같은 이유로 False 다(어휘 미확보).
    (PR #410 리뷰)
    """
    resolved = await _resolve(settings, kind=kind, label=label, embed=_never_embed())

    assert resolved is not None
    assert resolved.node.verified is expected_verified


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("priceBand", "3~5만원"),  # 자연어는 파싱하지 않는다
        ("priceBand", "50000-30000"),  # min >= max
        # 하이픈 2개 — 열린 밴드(#581) 이후에도 매치 자체가 안 된다. `(\d*)` 는 "-" 를 못 먹어서
        # 앞 그룹이 빈 문자열로 첫 하이픈을 넘겨도 뒤에 "1000-5000" 이 남아 `$` 에 도달 못 한다.
        ("priceBand", "-1000-5000"),
        ("priceBand", "가성비"),
        ("priceBand", "-"),  # 경계가 둘 다 없으면 밴드가 아니다(#581)
        ("priceBand", "50,000원 이하"),  # §3.8 이 내보내는 렌더 문장은 되돌려 받지 않는다(#581)
        # **아무것도 안 거르는 밴드는 취향이 아니다**(#581) — 도메인 경계를 접고 나면 양쪽이
        # 비어 `"-"` 와 같아진다. "0원 이상"·"5점 이하"·"0~5점"은 전부 전체 집합이다.
        ("priceBand", "0-"),
        ("ratingBand", "-5"),
        ("ratingBand", "0-5"),
        ("ratingBand", "4-6"),  # 평점 스케일(0~5) 밖
        # 열린 밴드도 스케일을 넘으면 드롭한다 — "6점 이상"은 존재할 수 없다(#581).
        # 종전 코드는 상한만 검사해서, 하한만 있는 밴드가 이 관문을 통째로 비켜갔다.
        ("ratingBand", "6-"),
        ("ratingBand", "-6"),
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


@pytest.mark.parametrize(
    ("kind", "variants", "expected_node_id"),
    [
        ("product", ["7", "007", "0000007"], "product:7"),
        ("priceBand", ["30-50", "030-050"], "priceBand:30-50"),  # 밴드는 이미 수렴한다
        ("ratingBand", ["4-5", "04-05"], "ratingBand:4-"),
        # 열린 밴드도 같은 수렴을 받는다(#581) — 빈 쪽이 재조립에서 그대로 비어야 한다
        ("priceBand", ["-50000", "-050000"], "priceBand:-50000"),
        ("priceBand", ["30-", "030-"], "priceBand:30-"),
        # **의미가 같은 두 인코딩도 한 노드로 수렴해야 한다**(#581). 가격은 늘 0 이상이고
        # 평점은 늘 5 이하라 이 쌍들은 같은 취향이다 — 갈리면 마이페이지에 같은 문구가 두 줄
        # 뜨고, 한쪽을 지워도 다른 쪽이 남으며 지운 인코딩의 재추출이 tombstone 을 비켜간다.
        ("priceBand", ["0-100000", "-100000", "0-0100000"], "priceBand:-100000"),
        ("ratingBand", ["4-5", "4-"], "ratingBand:4-"),
    ],
)
async def test_numeric_labels_converge_to_one_node_id(
    settings: Settings, kind: str, variants: list[str], expected_node_id: str
) -> None:
    """자리수 표기가 달라도 같은 대상이면 **하나의 `node_id`** 로 수렴한다 (REQ-PGRAPH-010).

    식별자 결정론은 이 이슈의 기능 요구사항이다 — 갈라지면 사용자가 지운 취향이 다른 표기로
    다시 언급될 때 **새 `active` 노드로 부활**해 tombstone 을 우회한다. `_resolve_band` 는
    `int()` 왕복으로 이미 수렴시키는데 `_resolve_product` 만 원문 문자열을 그대로 써서
    `product:7` 과 `product:007` 이 갈렸다(PR #410 리뷰).

    `normalize_label` 은 NFKC·공백·casefold 만 하므로 앞자리 0 을 없애 주지 않는다 — 숫자 정규화는
    파서의 몫이다.
    """
    node_ids = set()
    for label in variants:
        resolved = await _resolve(settings, kind=kind, label=label, embed=_never_embed())
        assert resolved is not None, f"{kind} {label!r} 이 드롭됐다"
        node_ids.add(resolved.node.node_id)

    assert node_ids == {expected_node_id}


# ─────────── LLM 통제 필드의 경계 — **구조적 가드 + 전수 표** ───────────
#
# `resolve_triple` 의 인자는 두 종류다: **LLM 이 값을 정하는 것**과 호출부가 주입하는 것(설정·시계·
# I/O seam). 앞쪽은 전부 검증 대상이고, 아래 표에 한 줄씩 있어야 한다.
#
# 지금까지 PR #410 리뷰가 이 계열로만 일곱 번 나왔다 — `predicate`·`source` Literal, `salience`
# 범위, `anchor_phrase` 길이, 상품 id 표기, 숫자 크기, 그리고 `polarity` 반전. 매번 "새 필드를
# 표에 넣는 것"을 사람이 기억해야 했던 게 원인이라, **시그니처를 훑어 기계가 강제**하게 한다.
_INJECTED_PARAMS = {
    "settings",
    "now",
    "brand_lexicon",
    "embed",
    "category_exact",
    "category_search",
}
_LLM_CONTROLLED = {"kind", "label", "anchor_phrase", "polarity", "predicate_hint"}


def test_every_llm_controlled_field_is_registered() -> None:
    """LLM 이 값을 정하는 인자는 **전부 아래 적대적 입력 표에 등록**돼 있어야 한다.

    새 필드를 더하면 여기서 깨진다 — 그때 표에 줄을 넣으라는 뜻이다. 이 단언이 없으면 "표에 적은
    것만 검사한다"가 되어, 새 필드가 무검증으로 들어가도 아무도 모른다(그 결과가 리뷰 일곱 번이다).
    """
    import inspect

    params = set(inspect.signature(resolve_triple).parameters) - _INJECTED_PARAMS

    assert params == _LLM_CONTROLLED


@pytest.mark.parametrize(
    ("polarity", "expected"),
    [
        ("negative", "avoids"),
        ("positive", "likes"),
        # 표기 흔들림은 **정규화로 흡수**한다 — 대소문자·공백 때문에 극성이 뒤집히면 안 된다.
        ("Negative", "avoids"),
        (" negative ", "avoids"),
        ("NEGATIVE", "avoids"),
        ("Positive", "likes"),
        # 어휘 밖은 **드롭**한다. 긍정으로 흘려보내면 "싫어한다"가 "좋아한다"로 확정 저장된다 —
        # 이 파일의 다른 필드가 전부 "실패는 드롭"인데 여기만 의미가 반전됐다(PR #410 리뷰).
        ("부정", None),
        ("neg", None),
        ("", None),
    ],
)
async def test_polarity_never_flips_silently(
    settings: Settings, polarity: str, expected: str | None
) -> None:
    """극성은 뒤집히느니 드롭한다 — 반대 취향을 확정 저장하는 것이 가장 나쁜 실패다.

    "소니는 별로예요" 가 `likes 소니` 로 저장되면 요약·rerank 가 사용자가 싫다고 말한 것을 밀어
    올린다. 에러도 로그도 없어 발견도 안 된다.
    """
    resolved = await _resolve(settings, polarity=polarity, embed=_never_embed())

    assert (resolved.predicate if resolved else None) == expected


@pytest.mark.parametrize("hint", ["purchased", "Purchased", " purchased ", "PURCHASED"])
async def test_purchased_hint_is_rejected_regardless_of_casing(
    settings: Settings, hint: str
) -> None:
    """`purchased` 거부가 대소문자 하나로 뚫리면 안 된다 — 리뷰가 짚은 `polarity` 와 같은 결함이다.

    구매 사실의 원천은 질의 시점 구매 이력(I-19)이지 발화가 아니다(REQ-PGRAPH-078 · 결정 14-F).
    거부가 우회되면 대화로 만든 구매 기록이 취향으로 확정 저장된다.
    """
    resolved = await _resolve(
        settings, kind="product", label="123", predicate_hint=hint, embed=_never_embed()
    )

    assert resolved is None


_BIGINT_MAX = 9_223_372_036_854_775_807  # Spring productId 상한(CLAUDE.md — 숫자 id 는 BIGINT)


@pytest.mark.parametrize(
    ("kind", "label", "kept"),
    [
        # 숫자 **크기** — 형식이 맞아도 도메인 범위를 벗어나면 존재할 수 없는 대상이다.
        ("product", str(_BIGINT_MAX), True),
        ("product", str(_BIGINT_MAX + 1), False),
        ("product", "9" * 30, False),
        ("priceBand", f"1-{_BIGINT_MAX}", True),
        ("priceBand", f"1-{_BIGINT_MAX + 1}", False),
        ("priceBand", "1-" + "9" * 30, False),
        # 열린 밴드(#581)는 **경계가 하나뿐이라 서로를 못 가려 준다.** 종전에는 `low <= high` 라
        # 상한 검사 하나가 둘 다 덮었는데, 하한만 있는 밴드에는 그 상한이 아예 없다 —
        # 각 경계를 따로 재지 않으면 존재할 수 없는 크기가 그대로 통과한다.
        ("priceBand", f"{_BIGINT_MAX}-", True),
        ("priceBand", f"{_BIGINT_MAX + 1}-", False),
        ("priceBand", f"-{_BIGINT_MAX + 1}", False),
        ("priceBand", "9" * 30 + "-", False),
        ("ratingBand", "5-", True),  # "5점 이상" — 하한은 도메인 경계가 아니라 그대로 남는다
        ("ratingBand", "6-", False),
        ("ratingBand", "-6", False),
        # **도메인 경계 상한만 있는 밴드는 전체 집합이라 드롭된다**(#581) — 크기 위반이 아니라
        # "아무것도 안 거른다"는 이유다. 상한 검사(`> _BIGINT_MAX`)가 아니라 접기가 잡는다.
        ("priceBand", f"-{_BIGINT_MAX}", False),
        ("ratingBand", "-5", False),
        # 숫자 **형식** — 이미 막고 있던 것들(회귀 가드로 표에 함께 둔다).
        ("product", "12345", True),
        ("product", "abc", False),
        ("priceBand", "50000-30000", False),  # min >= max
        ("priceBand", "-", False),  # 경계가 둘 다 없다
        ("ratingBand", "4-6", False),  # 평점 스케일 밖
    ],
)
async def test_numeric_labels_are_bounded_by_domain_range(
    settings: Settings, kind: str, label: str, kept: bool
) -> None:
    """숫자 라벨은 형식뿐 아니라 **크기**도 도메인 범위 안이어야 한다 (PR #410 리뷰).

    `^\\d+$` 는 자릿수를 보지 않고 `int()` 는 파이썬 임의 정밀도라 예외도 안 난다 — 30자리 숫자가
    그대로 통과해 **존재할 수 없는 productId** 노드가 만들어지고, 문서 상한
    (`profile_graph_max_edges`) 슬롯을 영구히 차지한다. 소비자(#150)가 Long 으로 파싱하면
    오버플로우다. 밴드도 같은 이유로 상한을 받는다.
    """
    resolved = await _resolve(settings, kind=kind, label=label, embed=_never_embed())

    assert (resolved is not None) is kept


async def test_anchor_phrase_is_bounded_by_the_utterance_cap() -> None:
    """앵커도 상한을 받는다 — 저장(jsonb)과 임베딩 페이로드 양쪽에 실리기 때문이다.

    상한을 `chat_message_max_chars` 로 잡는 근거는 **인용 구절은 인용 대상보다 길 수 없다**는
    것이다(`anchorPhrase` 는 "발화에서 그대로 인용한 구절"이고 발화 자체가 그 값으로 묶인다).
    정상 앵커는 한국어 구절 10~30자라 절대 안 잘리고, 지시를 어긴 비정상 출력만 막는다 —
    라벨 상한(60자)을 재사용하면 정당한 인용이 잘려 **임베딩 입력이 바뀌므로** 쓰지 않는다
    (앵커를 라벨 대신 쓰는 이유가 #59 의 오분류 실측이다). PR #410 리뷰.
    """
    tight = Settings(_env_file=None, chat_message_max_chars=10)

    resolved = await _resolve(tight, anchor_phrase="가" * 500, embed=_never_embed())

    assert resolved is not None
    assert resolved.node.resolution is not None
    assert resolved.node.resolution.anchor_phrase == "가" * 10


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
