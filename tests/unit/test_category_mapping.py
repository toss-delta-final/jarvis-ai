"""카테고리 매핑(방식 A) canonical-or-null 테스트 (이슈 #59).

decompose 추측(raw)을 임베딩으로 실제 DB 카테고리에 보정한다. embed·search·exact 를 주입형
fake 로 대체해 매핑 분기(exact/raw 최근접/query 앵커/신호 없음→빈 결과·무필터/하드실패degrade)와
멀티 dedup·상한 절단을 검증한다.
결과는 fan-out leg 용 (canonical, query) 페어 — query 는 그 카테고리의 검색 키워드(§6·§9).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from app.agents.buyer.recommendation.category_mapping import map_categories
from app.agents.buyer.recommendation.state import CategoryQuery
from app.core.llm import LLMError


def _settings(
    *,
    top_k: int = 5,
    fanout_max: int = 5,
    distance_max: float = 0.22,
    select_margin_max: float = 0.02,
    select_max_calls: int = 2,
) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_db_url="postgresql://x",
        category_top_k=top_k,
        category_fanout_max=fanout_max,
        category_distance_max=distance_max,
        category_select_margin_max=select_margin_max,
        category_select_max_calls=select_max_calls,
        embedding_task_query="RETRIEVAL_QUERY",
    )


class _FakeMapper:
    """embed↔search 를 인덱스 인코딩으로 연결해, anchor 텍스트별 최근접을 제어한다."""

    def __init__(
        self,
        *,
        exact: set[str],
        nearest: dict[str, str],
        embed_raises: bool = False,
        search_raises_for: set[str] | None = None,
        hits: dict[str, list[tuple[str, float]]] | None = None,
        default_distance: float = 0.1,
    ):
        self._exact = exact
        self._nearest = nearest
        self._embed_raises = embed_raises
        self._search_raises_for = search_raises_for or set()  # 이 앵커 텍스트의 search 만 예외
        # 거리·마진(§11 #115)을 검증하는 테스트는 hits 로 top-k 전체를 직접 준다. 미지정 앵커는
        # nearest + default_distance 로 top-1 만 만든다(기존 테스트는 거리에 관심 없음).
        self._hits = hits or {}
        self._default_distance = default_distance
        self._embedded: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._embed_raises:
            raise RuntimeError("embed down")
        self._embedded = list(texts)
        return [[float(i)] for i in range(len(texts))]  # vec[0] = 배치 인덱스

    def search(self, vec: list[float], dsn: str, *, k: int) -> list[tuple[str, float]]:
        """프로덕션 search_categories_pg 와 동일 계약 — (category, distance) 를 거리 오름차순으로."""
        text = self._embedded[int(vec[0])]
        if text in self._search_raises_for:
            raise RuntimeError(f"search down for {text}")
        if text in self._hits:
            return self._hits[text][:k]
        hit = self._nearest.get(text)
        return [(hit, self._default_distance)] if hit else []

    def exact_lookup(self, values, dsn: str) -> set[str]:
        return {v for v in values if v in self._exact}

    async def run(
        self, queries, utterance="발화", settings=None, *, select=None, llm=None, observer=None
    ):
        kwargs = {}
        if select is not None:  # §4.4 택일 주입(미지정이면 프로덕션 기본값 — llm=None 이면 미호출)
            kwargs["select_category"] = select
        return await map_categories(
            category_queries=queries,
            utterance=utterance,
            settings=settings or _settings(),
            embed=self.embed,
            search_top_k=self.search,
            exact_lookup=self.exact_lookup,
            llm=llm,
            observer=observer,
            **kwargs,
        )


async def test_default_embed_carries_query_task_type(monkeypatch) -> None:
    """embed 미주입(프로덕션 경로)이면 질의 task_type(RETRIEVAL_QUERY)로 임베딩한다.

    저장소 비대칭 임베딩 관례(search_service=query / artifacts_batch=document, 이슈 #65)에서
    이 앵커(raw 추측·leg query)는 질의 쪽이다. task_type 이 안 실리면 Google 기본 모드로
    떨어져, 문서 쪽(category_seed=document)과 task 불일치 시 코사인이 왜곡된다(PR #73 리뷰).
    """
    import app.agents.buyer.recommendation.category_mapping as cm

    captured: dict = {}

    def fake_embed_texts(texts, *, task_type=None):
        captured["task_type"] = task_type
        return [[0.0] for _ in texts]

    monkeypatch.setattr(cm, "_embed_texts", fake_embed_texts)
    await map_categories(
        category_queries=[CategoryQuery("여행용품", "파우치")],  # exact 아님 → 임베딩 경로
        utterance="발화",
        settings=_settings(),
        search_top_k=lambda vec, dsn, *, k: [],
        exact_lookup=lambda values, dsn: set(),
    )
    assert captured["task_type"] == "RETRIEVAL_QUERY"


async def test_exact_match_uses_raw() -> None:
    """raw 가 DB에 exact match → raw 그대로 canonical, query 보존."""
    m = _FakeMapper(exact={"PC부품 > CPU"}, nearest={})
    out = await m.run([CategoryQuery("PC부품 > CPU", "cpu")])
    assert out == [("PC부품 > CPU", "cpu")]


async def test_unmapped_anchor_is_logged(caplog) -> None:
    """임베딩 조회는 정상인데 히트 0건이라 드롭되는 앵커를 관측 로그로 남긴다(PR #73 리뷰 #4).

    categories 미시드·임베딩 결측이면 매 턴 전부 이 분기로 빠져 매핑이 조용히 무력화되는데,
    로그가 없으면 운영 중 감지 불가 — canonical 을 못 낸 앵커를 warning 으로 남긴다.
    """
    m = _FakeMapper(exact=set(), nearest={})  # 모든 앵커 히트 0건
    with caplog.at_level("WARNING"):
        out = await m.run([CategoryQuery("없는카테고리", "q")])
    assert out == []
    assert any(r.msg == "category_unmapped" for r in caplog.records)


async def test_offlist_uses_nearest() -> None:
    """raw 가 exact 아님 → 임베딩 최근접 채택(거리 무관 항상), query 보존.

    (query 앵커에 히트가 없는 경우라 §4.3 병행 조회에서도 raw 쪽이 그대로 채택된다.)
    """
    m = _FakeMapper(exact=set(), nearest={"무선 이어폰": "가전 > 이어폰/헤드폰"})
    out = await m.run([CategoryQuery("무선 이어폰", "이어폰")])
    assert out == [("가전 > 이어폰/헤드폰", "이어폰")]


def _record(caplog, msg: str):
    """caplog 에서 구조화 로그 한 건을 집어온다(없으면 테스트 실패 메시지에 실제 msg 목록 노출)."""
    hits = [r for r in caplog.records if r.msg == msg]
    assert hits, f"{msg} 로그 없음 — 방출된 msg: {[r.msg for r in caplog.records]}"
    return hits[0]


async def test_repaired_log_carries_distance_margin_anchor_kind(caplog) -> None:
    """raw 최근접 보정 로그에 채택 거리·마진·앵커 종류를 싣는다(§11 #115 관측 구멍 보강).

    search_categories_pg 가 `embedding <=> q` 로 정렬해놓고 거리를 버려서, "얼마나 가까운
    매칭이었나"가 로그 어디에도 안 남았다 — 이것이 #115 진단을 막은 관측 구멍이다. 거리컷
    임계 재튜닝과 후속 top-k 택일 트리거가 모두 이 분포에 의존하므로 로그에 노출한다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "전자제품/음향": [
                ("자동차기기 > 카오디오음향기기", 0.1767),
                ("음향가전 > 기타 음향기기", 0.1873),
            ]
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery("전자제품/음향", "무선이어폰")])
    assert out == [("자동차기기 > 카오디오음향기기", "무선이어폰")]  # 동작은 종전과 동일(컷 없음)
    rec = _record(caplog, "category_repaired")
    assert rec.distance == 0.1767
    assert rec.margin == 0.0106  # 2위-1위, 소수 4자리로 반올림
    assert rec.anchor_kind == "raw"


async def test_fallback_log_carries_anchor_kind_query(caplog) -> None:
    """raw 없이 leg query 로 매핑한 경로는 anchor_kind=query 로 구분한다(§4.3 앵커 종류 관측).

    raw(LLM 창작 라벨)와 query(발화 유래) 중 어느 앵커가 canonical 을 냈는지가 #115 의 핵심
    구분이다 — 이 라벨 없이는 로그에서 두 경로의 정확도를 분리해 볼 수 없다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"층간소음 방지 용품": [("생활잡화 > 층간소음방지용품", 0.0850)]},
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "층간소음 방지 용품")])
    assert out == [("생활잡화 > 층간소음방지용품", "층간소음 방지 용품")]
    rec = _record(caplog, "category_fallback_top1")
    assert rec.anchor_kind == "query"
    assert rec.distance == 0.085


async def test_single_hit_margin_is_none(caplog) -> None:
    """히트가 1건이면 마진은 계산 불가 → None(0.0 으로 두면 "동점"으로 오독된다)."""
    m = _FakeMapper(exact=set(), nearest={}, hits={"양말": [("패션잡화 > 양말", 0.1435)]})
    with caplog.at_level("INFO"):
        await m.run([CategoryQuery("양말", "양말")])
    assert _record(caplog, "category_repaired").margin is None


async def test_query_anchor_wins_when_closer_than_raw(caplog) -> None:
    """raw·query 둘 다 조회해 **더 가까운 쪽**을 채택한다 (§4.3 #115 — 종전엔 raw 가 query 를 덮었다).

    #115 의 핵심 손실 경로 재현: LLM 이 창작한 raw 라벨('가전/생활용폼' — 오타까지 포함)은 실측
    0.2399/마진 0.0021 로 사실상 동전 던지기인데, 발화 유래 query 는 0.0850/마진 0.1561 로 압도적
    정답이었다. 종전 규칙(`raws[i] or qtexts[i]`)은 그 정답을 조회조차 하지 않고 버렸다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "가전/생활용폼": [("전기/산업자재 > 전기생활용품", 0.2399), ("생활가전 > LG", 0.2420)],
            "층간소음 방지 용품": [
                ("생활잡화 > 층간소음방지용품", 0.0850),
                ("자동차기기 > 방음/방진/마감재", 0.2411),
            ],
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery("가전/생활용폼", "층간소음 방지 용품")])
    assert out == [("생활잡화 > 층간소음방지용품", "층간소음 방지 용품")]
    rec = _record(caplog, "category_repaired")  # raw 가 있었으므로 이벤트는 repaired 유지
    assert rec.anchor_kind == "query"  # 다만 canonical 을 낸 앵커는 query
    assert rec.distance == 0.085


async def test_query_anchor_wins_even_when_raw_is_closer(caplog) -> None:
    """raw 가 **거리상 더 가까워도** query 히트가 있으면 query 를 채택한다(§4.3.1 #115 재개정).

    1차 개정("가까운 쪽 승리")은 역효과였다 — 라이브 실측에서 anchor=raw 로 채택된 12건 중 11건이
    오분류였다. 원인은 **추상 라벨의 가짜 근접**이다: '주방용품' 이 `주방용품 > 칼` 에 0.1387 로 붙는
    것은 의미 근접이 아니라 카테고리명과의 문자열 겹침 때문이고, 정작 의미가 맞는 '냄비 세트'
    (0.1941)보다 "가깝게" 나온다. 거리가 의미를 반영하지 않는 구간이라 raw·query 를 거리로 비교하는
    것 자체가 성립하지 않으므로, 신뢰도 높은 쪽(발화 유래 query)을 규칙으로 고정한다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "주방용품": [  # 더 가깝지만 오분류(문자열 겹침)
                ("주방용품 > 칼", 0.1387),
                ("주방용품 > 보관용기", 0.1415),
            ],
            "냄비 세트": [
                ("주방용품 > 냄비", 0.1941),
                ("주방용품 > 솥/찜기/뚝배기", 0.2338),
            ],
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery("주방용품", "냄비 세트")])
    assert out == [("주방용품 > 냄비", "냄비 세트")]
    rec = _record(caplog, "category_repaired")
    assert rec.anchor_kind == "query"
    assert rec.distance == 0.1941  # 더 먼 쪽이지만 의미가 맞는 앵커


async def test_raw_anchor_used_only_when_query_has_no_hit() -> None:
    """query 앵커가 히트 0건이면 raw 최근접으로 폴백한다(§4.3 — raw 는 폴백이지 무용지물이 아니다).

    query 가 사전에 없는 신조어·오타여서 히트가 없을 수 있다. 그때 raw 까지 버리면 canonical 을
    아예 못 내 무필터로 새므로, raw 조회를 유지해 하나라도 살린다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"음향가전": [("음향가전 > 오디오", 0.0640)]},  # query '헤드셋추천템' 은 히트 0건
    )
    out = await m.run([CategoryQuery("음향가전", "헤드셋추천템")])
    assert out == [("음향가전 > 오디오", "헤드셋추천템")]


async def test_query_anchor_failure_falls_back_to_raw(caplog) -> None:
    """query 앵커 조회가 예외로 실패하면 raw 최근접으로 폴백한다(앵커 단위 격리의 반대 방향, §5)."""
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"음향가전": [("음향가전 > 오디오", 0.0640)]},
        search_raises_for={"무선 이어폰"},  # query 앵커 조회만 예외
    )
    with caplog.at_level("WARNING"):
        out = await m.run([CategoryQuery("음향가전", "무선 이어폰")])
    assert out == [("음향가전 > 오디오", "무선 이어폰")]  # raw 앵커로 생존
    assert "category_leg_search_failed" in [r.msg for r in caplog.records]


async def test_distance_over_cut_drops_leg(caplog) -> None:
    """채택 거리가 category_distance_max 를 넘으면 그 leg 를 canonical 없이 드롭한다(§4 #115).

    거리 0.22 초과는 "맞는 칸이 taxonomy 에 없다"의 신호다. 실측에서 "부모님 환갑 선물"·
    "조카 입학 선물"·"집들이 선물"이 모두 `출산/돌기념품`(0.297~0.302)으로 붕괴했는데, 2056 leaf
    에 해당 칸이 없어 "가장 덜 틀린" 값을 억지로 집은 것이다. 틀린 카테고리로 좁히면 정답 상품이
    후보에서 아예 제외되므로, 카테고리를 빼고 semanticQuery 로 넓게 찾는 편이 낫다(종전 never-null
    "멀어도 억지로 채택" 폐기).
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "부모님 환갑 선물": [
                ("출산/돌기념품 > 출산선물/기념품", 0.2971),
                ("출산/돌기념품 > 출산준비패키지", 0.3220),
            ]
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "부모님 환갑 선물")])
    assert out == []  # 카테고리 없이 무필터 + semanticQuery
    rec = _record(caplog, "category_distance_rejected")
    assert rec.distance == 0.2971
    assert rec.canonical == "출산/돌기념품 > 출산선물/기념품"  # 무엇이 거부됐는지 관측 가능
    assert rec.threshold == 0.22


async def test_distance_at_cut_boundary_is_accepted() -> None:
    """경계값(거리 == 임계)은 채택한다 — 임계는 "초과"에서만 드롭(부등호 방향 회귀 방지).

    실측 경계 여유가 0.005 밖에 없어(정답 최대 0.2168 vs 오분류 최소 0.2221) 부등호 하나가
    정답·오분류 판정을 뒤집는다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={"양말": [("패션잡화 > 양말", 0.22)]})
    out = await m.run([CategoryQuery(None, "양말")])
    assert out == [("패션잡화 > 양말", "양말")]


async def test_distance_rejected_is_not_logged_as_unmapped(caplog) -> None:
    """거리컷 드롭은 category_distance_rejected 로만 남기고 category_unmapped 로 이중 기록하지 않는다.

    category_unmapped 는 "신호 있으나 히트 0건"(시드 결측·top-k 미스 품질 신호, §11)이라 정책적
    드롭(거리컷)이 이 메트릭을 오염시키면 안 된다 — 실패 격리 로그 규약(§5)과 동일 취지.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={"환갑 선물": [("취미 > 수집용품", 0.31)]})
    with caplog.at_level("INFO"):
        await m.run([CategoryQuery(None, "환갑 선물")])
    msgs = [r.msg for r in caplog.records]
    assert "category_distance_rejected" in msgs
    assert "category_unmapped" not in msgs
    assert "category_fallback_top1" not in msgs  # 채택 로그도 남기지 않는다(채택이 아니므로)


async def test_exact_match_is_not_distance_cut() -> None:
    """exact 매치는 거리컷 대상이 아니다 — DB 검증값이라 거리 개념이 없다(§4.3).

    exact leg 가 컷에 걸려 사라지면 사전에 실재하는 canonical 을 버리는 것이라 후퇴다.
    """
    m = _FakeMapper(exact={"PC부품 > CPU"}, nearest={}, hits={"cpu": [("PC부품 > 쿨러", 0.9)]})
    out = await m.run([CategoryQuery("PC부품 > CPU", "cpu")])
    assert out == [("PC부품 > CPU", "cpu")]


async def test_partial_distance_cut_keeps_close_legs() -> None:
    """멀티 leg 에서 먼 leg 만 드롭하고 가까운 leg 는 유지한다(전개 실패 회차 격리, §6.0).

    "부모님 환갑 선물" 전개가 일부만 성공한 회차를 모사한다 — 홍삼(0.1398)은 살리고 코너 이름
    잔여물(0.31)만 버려야, 전개된 상품 검색이 회차마다 통째로 무필터로 새지 않는다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "홍삼": [("건강식품 > 홍삼", 0.1398)],
            "선물 세트": [("취미 > 수집용품", 0.31)],
        },
    )
    out = await m.run([CategoryQuery(None, "홍삼"), CategoryQuery(None, "선물 세트")])
    assert out == [("건강식품 > 홍삼", "홍삼")]


class _FakeSelector:
    """select_category 주입형 fake — 호출 인자를 기록하고 정해진 답(또는 예외)을 돌려준다."""

    def __init__(self, *, answer: str | None = None, raises: bool = False):
        self._answer = answer
        self._raises = raises
        self.calls: list[tuple[str, tuple[str, ...]]] = []  # (query, candidates)
        self.kwargs: dict = {}  # 마지막 호출의 부가 인자(settings·observer 전달 검증, PR #188)

    async def __call__(self, llm, *, query, candidates, tier, **kw):
        self.kwargs = kw  # settings·observer 전달 여부 검증용(PR #188 리뷰)
        self.calls.append((query, tuple(candidates)))
        if self._raises:
            raise RuntimeError("llm down")
        return self._answer


_AMBIGUOUS = {  # '선물용품' 실측: 거리 0.2074(컷 통과) · 마진 0.0095(얇음) → 택일 트리거
    "선물용품": [
        ("취미 > 수집용품", 0.2074),
        ("취미 > 종교용품", 0.2169),
        ("도서/음반 > 독서용품", 0.2292),
    ]
}


async def test_thin_margin_triggers_topk_select_and_replaces_canonical(caplog) -> None:
    """마진이 얇으면 top-k 택일을 호출해 canonical 을 교체한다(§4.4 #115).

    거리컷이 못 막는 잔여 구멍 — 추상 라벨은 거리는 가까운데(0.2074 < 0.22 통과) 뜻이 틀린다.
    마진(0.0095)으로는 잡히지만 마진 드롭은 `양말`류 정답을 오탐하므로(§4.0), 드롭이 아니라
    **택일**한다. 1·2위가 둘 다 정답인 경우엔 LLM 이 뭘 골라도 맞으니 오탐이 무해해진다.
    """
    sel = _FakeSelector(answer="취미 > 종교용품")
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == [("취미 > 종교용품", "선물용품")]  # 임베딩 top-1(수집용품)이 택일로 교체됨
    assert len(sel.calls) == 1
    query, candidates = sel.calls[0]
    # 후보는 그 앵커의 top-k 전체 / 질의에는 발화와 앵커를 모두 싣는다(멀티 leg 정체성 + 라벨 보강)
    assert candidates == ("취미 > 수집용품", "취미 > 종교용품", "도서/음반 > 독서용품")
    assert "선물용품" in query
    rec = _record(caplog, "category_selected")
    assert rec.canonical == "취미 > 종교용품"
    assert rec.top1 == "취미 > 수집용품"


async def test_thick_margin_does_not_call_select() -> None:
    """마진이 두꺼우면 택일을 호출하지 않는다 — 조건부 LLM 예산(§4.4·§12)."""
    sel = _FakeSelector(answer="아무거나")
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "층간소음 방지 용품": [
                ("생활잡화 > 층간소음방지용품", 0.0850),
                ("자동차기기 > 방음/방진/마감재", 0.2411),
            ]
        },
    )
    out = await m.run([CategoryQuery(None, "층간소음 방지 용품")], select=sel, llm=object())
    assert out == [("생활잡화 > 층간소음방지용품", "층간소음 방지 용품")]
    assert sel.calls == []  # 마진 0.1561 → 트리거 안 됨


async def test_select_confirming_top1_is_still_logged(caplog) -> None:
    """택일이 top-1 을 그대로 확정해도 로그를 남긴다 — "확정"과 "미호출"을 구분해야 한다(§11).

    실측 '양말'(마진 0.0088)에서 LLM 이 top-1 을 골랐는데 로그가 없어, 택일이 돈 것인지 트리거가
    안 된 것인지 사후에 구분할 수 없었다. 트리거 임계 재튜닝이 이 구분에 의존한다.
    """
    sel = _FakeSelector(answer="패션잡화 > 양말")  # top-1 을 그대로 확정
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"양말": [("패션잡화 > 양말", 0.1435), ("브랜드 잡화/소품 > 양말", 0.1523)]},
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "양말")], select=sel, llm=object())
    assert out == [("패션잡화 > 양말", "양말")]  # 1·2위 둘 다 정답이라 어느 쪽이든 무해
    rec = _record(caplog, "category_selected")
    assert rec.changed is False  # 교체 없음 — 그래도 택일이 돌았다는 사실은 남는다


async def test_selected_candidate_still_faces_distance_cut(caplog) -> None:
    """택일이 고른 후보도 거리컷을 다시 통과해야 한다(§4·§4.4 결합).

    실측 '선물용품'(top-1 0.2074 통과, 마진 0.0095)에서 LLM 이 `도서/음반 > 독서용품`(0.2292)을
    골랐다 — top-k 안에서 고르더라도 top-1 보다 먼 후보일 수 있다. 그 경우 "가까운 칸이 없다"는
    뜻이므로 억지로 채택하지 않고 드롭한다(거리컷을 택일 **이후** 값에 적용).
    """
    sel = _FakeSelector(answer="도서/음반 > 독서용품")
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == []  # 0.2292 > 0.22 → 드롭
    rec = _record(caplog, "category_distance_rejected")
    assert rec.canonical == "도서/음반 > 독서용품"  # 택일 결과에 컷이 적용됐음이 로그로 보인다
    assert rec.distance == 0.2292


async def test_select_null_drops_leg(caplog) -> None:
    """택일이 null(맞는 후보 없음)이면 그 leg 를 드롭한다(§4.4).

    '선물용품' 의 후보에는 "부모님 환갑 선물"에 맞는 칸이 없다 — 억지로 `취미 > 수집용품` 을
    보내는 것보다 카테고리를 빼고 semanticQuery 로 찾는 편이 낫다.
    """
    sel = _FakeSelector(answer=None)
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == []
    assert "category_select_null" in [r.msg for r in caplog.records]


async def test_select_failure_keeps_embedding_top1(caplog) -> None:
    """택일 LLM 이 예외로 죽으면 드롭하지 않고 임베딩 top-1 을 유지한다(§4.4 실패 degrade).

    "맞는 후보 없음"(드롭)과 "판정 실패"(유지)는 후속 조치가 반대다 — 인프라 실패로 카테고리를
    잃으면 종전보다 후퇴이므로, 예외는 top-1 유지로 흡수하고 별 이벤트로 관측한다.
    """
    sel = _FakeSelector(raises=True)
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == [("취미 > 수집용품", "선물용품")]  # 임베딩 top-1 유지
    assert "category_select_unavailable" in [r.msg for r in caplog.records]


async def test_select_skipped_when_llm_not_configured() -> None:
    """llm 미주입(미구성)이면 택일을 건너뛰고 임베딩 top-1 을 쓴다 — 매핑이 LLM 에 종속되지 않는다."""
    sel = _FakeSelector(answer="취미 > 종교용품")
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=None)
    assert out == [("취미 > 수집용품", "선물용품")]
    assert sel.calls == []


async def test_llm_unavailable_never_logs_max_calls_reason(caplog) -> None:
    """llm 미구성 + 애매한 leg 이 상한보다 많을 때, 초과분에 "max_calls" 사유를 붙이지 않는다.

    [PR #188 리뷰] 상한 초과 로깅이 llm 상태와 무관하게 먼저 돌면, 같은 leg 이 "max_calls"(사실
    아님)와 "llm_unavailable"(진짜 사유)로 **두 번** 기록된다. 이 PR 의 취지가 category_select_*
    이벤트로 임계를 재튜닝하는 관측이라, 거짓 사유가 섞이면 목적을 스스로 훼손한다.
    """
    ambiguous = {
        f"라벨{i}": [(f"카테고리 > A{i}", 0.20), (f"카테고리 > B{i}", 0.2050)] for i in range(3)
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=ambiguous)
    with caplog.at_level("INFO"):
        out = await m.run(
            [CategoryQuery(None, f"라벨{i}") for i in range(3)],
            settings=_settings(select_max_calls=2),
            llm=None,  # 미구성 — 상한과 무관하게 전부 llm_unavailable 이어야 한다
        )
    assert len(out) == 3  # 전 leg 이 임베딩 top-1 로 살아남는다
    reasons = [r.reason for r in caplog.records if r.msg == "category_select_unavailable"]
    assert reasons == ["llm_unavailable"] * 3  # 3건, 사유 단일 — 중복·모순 없음
    assert "max_calls" not in reasons


async def test_real_select_category_llm_failure_keeps_top1(caplog) -> None:
    """[PR #188] **실제 select_category** 와 조합했을 때도 LLM 실패가 top-1 을 유지하는가 (seam 테스트).

    이 이음새가 결함이 났던 자리다 — 호출부는 "예외=판정 실패(top-1 유지) / None=맞는 후보 없음
    (드롭)"을 가정했는데, `select_category` 는 `LLMError` 를 삼켜 None 을 돌려주고 있었다. 즉 LLM
    타임아웃이 "맞는 카테고리 없음"으로 오해돼 **카테고리를 삭제**했다. 양쪽 모듈을 각각 fake 로
    덮은 테스트는 이 불일치를 통과시킨다(호출부 테스트의 fake 는 예외를 던지고, select_category
    테스트는 호출부를 안 본다) — 그래서 여기서는 **주입 없이 실제 함수**를 쓴다.
    """

    class _RaisingLLM:
        async def complete(self, **kwargs):
            raise LLMError("llm timeout")

    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        # select 주입 없음 → 프로덕션 기본값(실제 select_category)이 쓰인다
        out = await m.run([CategoryQuery(None, "선물용품")], llm=_RaisingLLM())
    assert out == [("취미 > 수집용품", "선물용품")]  # 드롭이 아니라 임베딩 top-1 유지
    rec = _record(caplog, "category_select_unavailable")
    assert "llm timeout" in rec.reason  # 사유가 관측된다(llm_unavailable·max_calls 아님)


async def test_select_calls_are_capped_per_turn(caplog) -> None:
    """택일 호출은 category_select_max_calls 로 제한하고, 초과 leg 는 임베딩 top-1 을 유지한다.

    fan-out 5 leg 이 모두 애매하면 턴당 LLM 이 7회로 뛴다 — 상한으로 막고 초과분은 종전 동작
    (top-1)으로 흡수한다(§4.4 LLM 예산).
    """
    sel = _FakeSelector(answer=None)  # 호출되면 드롭 → 상한 준수 여부가 결과로 드러난다
    ambiguous = {
        f"라벨{i}": [(f"카테고리 > A{i}", 0.20), (f"카테고리 > B{i}", 0.2050)] for i in range(3)
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=ambiguous)
    with caplog.at_level("INFO"):
        out = await m.run(
            [CategoryQuery(None, f"라벨{i}") for i in range(3)],
            settings=_settings(select_max_calls=2),
            select=sel,
            llm=object(),
        )
    assert len(sel.calls) == 2  # 3개 애매하지만 2회만 호출
    assert out == [("카테고리 > A2", "라벨2")]  # 상한 초과 leg 만 top-1 로 살아남음
    assert "category_select_unavailable" in [r.msg for r in caplog.records]


async def test_exact_raw_wins_without_distance_comparison() -> None:
    """raw 가 exact match 면 query 앵커와 거리 비교조차 하지 않는다(§4.3 — exact 는 DB 검증값).

    exact 는 사전에 실재하는 canonical 이라 거리 개념이 없다(0 이 아니라 비교 대상 아님).
    query 쪽에 더 가까운 히트가 있어도 exact 를 밀어내지 못한다.
    """
    m = _FakeMapper(
        exact={"PC부품 > CPU"},
        nearest={},
        hits={"라이젠": [("PC부품 > 기타 PC부품", 0.0001)]},  # 거리상 훨씬 가깝지만 무의미
    )
    out = await m.run([CategoryQuery("PC부품 > CPU", "라이젠")])
    assert out == [("PC부품 > CPU", "라이젠")]


async def test_one_anchor_failure_keeps_the_other(caplog) -> None:
    """한 leg 의 앵커 둘 중 하나만 조회 실패하면 성공한 앵커로 살린다(부분 성공 보존, §5 격리 규약).

    앵커가 2개로 늘었으니 실패 격리 단위도 leg→앵커로 내려간다. raw 조회가 죽어도 query 조회가
    canonical 을 냈다면 그 leg 를 드롭할 이유가 없다 — 드롭하면 종전보다 오히려 후퇴한다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"층간소음 방지 용품": [("생활잡화 > 층간소음방지용품", 0.0850)]},
        search_raises_for={"가전/생활용폼"},  # raw 앵커 조회만 예외
    )
    with caplog.at_level("WARNING"):
        out = await m.run([CategoryQuery("가전/생활용폼", "층간소음 방지 용품")])
    assert out == [("생활잡화 > 층간소음방지용품", "층간소음 방지 용품")]  # query 앵커로 생존
    assert "category_leg_search_failed" in [r.msg for r in caplog.records]  # 실패는 관측


async def test_null_raw_uses_leg_query_as_anchor() -> None:
    """raw==null 이면 그 leg 의 query 를 앵커로 embed → top-1(발화 아님), query 보존(PR #73 #17).

    leg 고유 query 가 있으면 발화 전체가 아니라 query 로 임베딩해야 leg 별로 구분된다.
    """
    m = _FakeMapper(exact=set(), nearest={"집들이 선물": "생활/건강 > 생활용품"})
    out = await m.run([CategoryQuery(None, "집들이 선물")], utterance="집들이 선물 추천")
    assert out == [("생활/건강 > 생활용품", "집들이 선물")]


async def test_multi_null_raw_uses_per_leg_query_anchor() -> None:
    """null-raw leg 이 여럿이면 각 leg 의 query 를 앵커로 써서 서로 다른 canonical 로 매핑한다(PR #73 #17).

    발화 전체를 공유 앵커로 쓰면 서로 다른 아이템이 같은 최근접으로 붙어 dedup 으로 fan-out
    폭이 조용히 줄어든다 — leg 고유 query 로 임베딩해 이를 막는다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={"이어폰": "가전 > 이어폰/헤드폰", "노트북": "컴퓨터 > 노트북"},
    )
    out = await m.run(
        [CategoryQuery(None, "이어폰"), CategoryQuery(None, "노트북")],
        utterance="싼거 추천",
    )
    assert [c for c, _ in out] == [
        "가전 > 이어폰/헤드폰",
        "컴퓨터 > 노트북",
    ]  # 발화 공유로 합쳐지지 않음


async def test_empty_queries_yields_no_category() -> None:
    """categoryQueries 빈 리스트(카테고리 신호 없음) → 빈 결과 → 무필터 검색(카테고리 강제 안 함, PR #73 #22).

    "5만원 이하 아무거나" 같은 category-agnostic 질의를 발화 임베딩으로 엉뚱한 카테고리에 좁히지 않는다.
    """
    m = _FakeMapper(exact=set(), nearest={"유럽여행 준비물": "여행/캠핑 > 여행용품"})
    out = await m.run([], utterance="유럽여행 준비물")
    assert out == []


async def test_null_null_leg_skipped_no_category_forced() -> None:
    """raw·query 모두 없는 leg(신호 없음)는 발화로 강제 매핑하지 않고 스킵한다(PR #73 #22)."""
    m = _FakeMapper(exact=set(), nearest={"싼거 추천": "여행/캠핑 > 여행용품"})
    out = await m.run([CategoryQuery(None, None)], utterance="싼거 추천")
    assert out == []


async def test_embed_failure_without_exact_degrades_to_empty_not_raw() -> None:
    """exact 매치가 없는데 embed 까지 다운 → 미검증 raw 를 신뢰하지 않고 빈 legs 로 degrade한다.

    raw 는 검증이 필요할 만큼 자주 틀린다(이 PR의 전제) — 검증 불가 시 raw 를 보내면 가짜
    categoryName 으로 0건이 날 수 있어, 카테고리 없이(전체) 검색하도록 빈 리스트로 degrade 한다
    (canonical-or-null, PR #73 #20). exact 매치가 있으면 §5·별도 테스트대로 보존된다(여기선 없음).
    """
    m = _FakeMapper(exact=set(), nearest={}, embed_raises=True)
    out = await m.run([CategoryQuery("PC부품 > CPU", "cpu"), CategoryQuery(None, "뭐")])
    assert out == []


async def test_search_failure_logs_leg_failed_not_unmapped(caplog) -> None:
    """조회가 예외로 실패한 leg 는 category_leg_search_failed 로만 남기고 category_unmapped 로는
    이중 기록하지 않는다(PR #73 리뷰).

    category_unmapped 는 "신호 있으나 히트 0건"(top-k 미스율 품질 신호, §11)이라 인프라 실패
    (조회 예외)가 이 메트릭을 오염시키면 안 된다 — 실패 leg 는 실패 로그로만 관측한다.
    """
    m = _FakeMapper(exact=set(), nearest={}, search_raises_for={"이어폰"})
    with caplog.at_level("WARNING"):
        out = await m.run([CategoryQuery(None, "이어폰")])
    assert out == []
    msgs = [r.msg for r in caplog.records]
    assert "category_leg_search_failed" in msgs  # 인프라 실패는 남김
    assert "category_unmapped" not in msgs  # 품질 메트릭은 오염 안 됨


async def test_exact_match_survives_embed_failure() -> None:
    """embed/search 가 실패해도 이미 DB 검증된 exact 매치 leg 는 보존한다(PR #73 리뷰).

    exact 조회(DB 직접)는 임베딩 경로와 독립이라 그 자체로 canonical 검증이다 — 임베딩 API
    일시 오류가 확정된 exact canonical 까지 무필터로 날리면 안 된다. exact leg 유지 +
    임베딩 필요 leg 만 드롭(canonical-or-null 은 exact·search 히트 둘 다 canonical 이라 성립).
    """
    m = _FakeMapper(exact={"전자기기 > 노트북"}, nearest={}, embed_raises=True)
    out = await m.run(
        [
            CategoryQuery("전자기기 > 노트북", "노트북"),  # exact match(DB 검증)
            CategoryQuery("여행용품", "파우치"),  # 임베딩 보정 필요 → embed 실패로 드롭
        ]
    )
    assert out == [("전자기기 > 노트북", "노트북")]  # exact 보존, 여행용품 leg 만 드롭


async def test_one_leg_search_failure_does_not_drop_other_legs() -> None:
    """leg 하나의 search_top_k 실패는 그 leg 만 unmapped 로 드롭하고 나머지는 유지한다(PR #73 리뷰).

    fan-out gather 를 return_exceptions 로 돌려 부분 실패를 격리한다 — recommendation/graph 의
    leg 별 SpringUnavailable 격리(§6)와 일관. return_exceptions 없이 던지면 gather 가 즉시 예외 →
    전체 빈 legs 로 degrade 돼, 정상 매핑된 leg 까지 무필터로 새는 걸 이 테스트가 막는다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={"이어폰": "가전 > 이어폰/헤드폰", "노트북": "컴퓨터 > 노트북"},
        search_raises_for={"노트북"},  # 노트북 leg 의 search 만 예외
    )
    out = await m.run([CategoryQuery(None, "이어폰"), CategoryQuery(None, "노트북")])
    assert out == [("가전 > 이어폰/헤드폰", "이어폰")]  # 실패한 노트북 leg 만 드롭, 이어폰 leg 유지


async def test_multi_dedup_and_truncate() -> None:
    """서로 다른 raw 가 같은 canonical 로 모이면 dedup(첫 query 유지), fanout_max 로 절단."""
    m = _FakeMapper(
        exact=set(),
        nearest={
            "이어폰": "가전 > 이어폰/헤드폰",
            "무선이어폰": "가전 > 이어폰/헤드폰",
            "TV": "가전 > TV",
        },
    )
    out = await m.run(
        [
            CategoryQuery("이어폰", "이어폰검색"),
            CategoryQuery("무선이어폰", "무선검색"),
            CategoryQuery("TV", "티비검색"),
        ],
        settings=_settings(fanout_max=5),
    )
    # 중복 canonical 합침 — 첫 leg 의 query 유지
    assert out == [("가전 > 이어폰/헤드폰", "이어폰검색"), ("가전 > TV", "티비검색")]


async def test_search_lookups_run_in_parallel() -> None:
    """need_idx 앵커별 search_top_k 를 병렬 실행한다 — 순차면 동시성 peak 1 (PR #73 리뷰 #3).

    검색 조회를 asyncio.to_thread 로 넘기므로, gather 병렬화 시 여러 조회가 동시에 스레드에서
    돌아 peak concurrency ≥2. 순차 for-loop 면 항상 1이라 이 테스트가 회귀를 잡는다.
    """
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def _slow_search(vec: list[float], dsn: str, *, k: int) -> list[tuple[str, float]]:
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)  # 겹칠 시간 확보(병렬이면 동시 진입)
        with lock:
            state["cur"] -= 1
        return [
            ("가전 > X", 0.1)
        ]  # (category, distance) 계약 — str 만 주면 매핑이 실패 경로로 샌다

    def _embed(texts: list[str]) -> list[list[float]]:
        return [[float(i)] for i in range(len(texts))]

    def _exact(values, dsn: str) -> set[str]:
        return set()

    out = await map_categories(
        category_queries=[
            CategoryQuery("a", None),
            CategoryQuery("b", None),
            CategoryQuery("c", None),
        ],
        utterance="u",
        settings=_settings(),
        embed=_embed,
        search_top_k=_slow_search,
        exact_lookup=_exact,
    )
    assert state["peak"] >= 2  # 병렬이면 동시 진입 ≥2, 순차면 1
    # 성공 경로였음을 함께 확인 — fake 반환형이 계약과 어긋나면 매핑이 조용히 하드실패 경로로
    # 빠져(빈 legs) peak 만 보는 이 테스트가 통과해버린다(실제로 그런 상태를 한 번 지나왔다).
    assert out == [("가전 > X", None)]  # 3 leg 이 같은 canonical → dedup, query 는 전부 None


async def test_select_budget_goes_to_smallest_margins_first(caplog) -> None:
    """[PR #188 리뷰] 택일 예산은 leg 번호가 아니라 **마진이 작은(가장 애매한) leg** 부터 쓴다.

    코드는 이미 마진으로 "애매하다"를 **판정**하는데, 예산을 **배분**할 때 마진을 무시하고 leg
    인덱스 순으로 잘라내면 기준이 코드 안에서 어긋난다 — 마진 0.002(1·2위가 거의 붙음)가 검증 없이
    top-1 로 남고, 컷 턱걸이 0.019 가 예산을 먹는 역전이 생긴다.

    트레이드오프 기록: `legs[0]` 은 **대표 카테고리**(칩 표시·멀티턴 승계, `state.py`)라 "가장 눈에
    띄는 leg 을 먼저 확인한다"는 인덱스 순의 명분도 있다. 그럼에도 마진 순을 택한 이유는 §4.4 의
    전제가 "마진 = 애매함의 세기"이고, 애매함이 큰 leg 을 방치하면 **틀린 카테고리로 검색이 좁혀지는
    손해**가 대표 여부와 무관하게 발생하기 때문이다. 대표 leg 이 정말 애매하면 마진도 작아 우선순위를
    자연히 얻는다.
    """
    sel = _FakeSelector(answer=None)
    hits = {  # margin: leg0=0.019(가장 덜 애매) / leg1=0.002(가장 애매) / leg2=0.010
        "라벨0": [("카테고리 > A0", 0.200), ("카테고리 > B0", 0.219)],
        "라벨1": [("카테고리 > A1", 0.200), ("카테고리 > B1", 0.202)],
        "라벨2": [("카테고리 > A2", 0.200), ("카테고리 > B2", 0.210)],
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=hits)
    with caplog.at_level("INFO"):
        await m.run(
            [CategoryQuery(None, f"라벨{i}") for i in range(3)],
            settings=_settings(select_max_calls=2),
            select=sel,
            llm=object(),
        )
    asked = {q.split("찾는 상품: ")[-1] for q, _ in sel.calls}
    assert asked == {"라벨1", "라벨2"}  # 마진 0.002·0.010 — 0.019 는 예산 밖
    # 잘린 leg 은 사유와 마진을 남긴다 — 예산 배분을 사후 검증할 수 있어야 한다(§11)
    skipped = [r for r in caplog.records if getattr(r, "reason", None) == "max_calls"]
    assert [round(r.margin, 4) for r in skipped] == [0.019]


# ── 관측 기록 (§11, api-spec §6.3 — PR #188 리뷰) ────────────────────────────


class _ProbeObserver:
    """record_model_call 만 갖는 최소 관측자 — 기록된 모델 ID 를 모아둔다."""

    request_id = "req-probe"

    def __init__(self) -> None:
        self.models: list[str] = []

    def record_model_call(
        self, model: str, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        self.models.append(model)


async def test_select_seam_receives_observer_and_settings() -> None:
    """[PR #188 리뷰] 택일 호출에 `observer`·`settings` 를 전달한다 — §6.3 모델 호출 기록의 전제.

    기록 자체는 모델을 실제로 부르는 `select_category` 안에서 한다(주입형 seam 에 유령 호출을
    남기지 않기 위해, `needs_expansion._llm_expand` 와 동일 원칙). 따라서 `map_categories` 의
    책임은 **seam 까지 전달**하는 것이고, 이 배선이 끊기면 기록이 조용히 사라진다.
    """
    observer = _ProbeObserver()
    settings = _settings()
    sel = _FakeSelector(answer="취미 > 종교용품")
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    out = await m.run(
        [CategoryQuery(None, "선물용품")],
        settings=settings,
        select=sel,
        llm=object(),
        observer=observer,
    )
    assert len(sel.calls) == 1
    assert out == [("취미 > 종교용품", "선물용품")]
    assert sel.kwargs["observer"] is observer
    assert sel.kwargs["settings"] is settings


async def test_select_seam_not_called_when_margin_is_thick() -> None:
    """트리거가 안 걸리면(마진 두꺼움) 택일 호출 자체가 없다 — 없는 비용을 만들지 않는다."""
    sel = _FakeSelector(answer="여성의류 > 청바지")
    m = _FakeMapper(exact=set(), nearest={"청바지": "여성의류 > 청바지"}, hits=None)
    await m.run([CategoryQuery(None, "청바지")], select=sel, llm=object())
    assert sel.calls == []
