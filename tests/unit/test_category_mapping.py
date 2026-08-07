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
    override_margin: float = 0.035,
    select_margin_max: float = 0.02,
    select_max_calls: int = 2,
    expand_legs: int = 8,
    expand_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_db_url="postgresql://x",
        category_top_k=top_k,
        category_fanout_max=fanout_max,
        category_distance_max=distance_max,
        category_distance_override_margin=override_margin,
        category_select_margin_max=select_margin_max,
        category_select_max_calls=select_max_calls,
        embedding_task_query="RETRIEVAL_QUERY",
        category_expand_legs=expand_legs,  # [#222] 광역 fan-out 후보 수
        category_expand_enabled=expand_enabled,  # [PR #318 R6-2] map_categories 가 직접 읽는 킬스위치
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

    async def run(self, *args, **kwargs):
        """`legs` 만 돌려준다 — #217 로 반환형이 `CategoryMapping` 이 됐지만 기존 단언은 leg 목록만
        본다. `unresolved`(§4 D2 트리거 입력)를 검증하는 테스트만 `run_full` 을 쓴다."""
        return (await self.run_full(*args, **kwargs)).legs

    async def run_full(
        self,
        queries,
        utterance="발화",
        settings=None,
        *,
        select=None,
        llm=None,
        observer=None,
        select_max_calls=None,
        sibling_expansion=False,
    ):
        kwargs = {}
        if select is not None:  # §4.4 택일 주입(미지정이면 프로덕션 기본값 — llm=None 이면 미호출)
            kwargs["select_category"] = select
        if select_max_calls is not None:  # 턴당 예산 주입(#217 PR 리뷰) — 미지정이면 settings 값
            kwargs["select_max_calls"] = select_max_calls
        return await map_categories(
            category_queries=queries,
            utterance=utterance,
            settings=settings or _settings(),
            embed=self.embed,
            search_top_k=self.search,
            exact_lookup=self.exact_lookup,
            llm=llm,
            observer=observer,
            sibling_expansion=sibling_expansion,  # [#428] 형제 전개 합의 필터 게이트
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
    assert out.legs == [("가전 > X", None)]  # 3 leg 이 같은 canonical → dedup, query 는 전부 None


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


async def test_select_changed_sample_is_marked_downstream(caplog) -> None:
    """[PR #188 리뷰] 택일이 top-1 을 **교체한** 표본은 하류 로그에서 구분 가능해야 한다.

    교체되면 `distance` 는 새로 채택된 후보 기준인데 `margin` 은 택일 **이전** top1 vs top2 값이라,
    한 레코드 안의 두 숫자가 서로 다른 후보를 가리킨다. §11 은 거리컷 임계 재튜닝과 택일 트리거를
    모두 이 분포에 의존한다고 못박고 있어, 짝이 안 맞는 표본이 섞이면 분석이 오염된다.

    margin 을 pick 기준으로 **재계산하지 않는** 이유: margin 의 용도는 "택일을 발동시킨 애매함의
    세기"(트리거 임계 튜닝)라 발동 시점 값이어야 의미가 있다. distance 는 실제 채택값이어야
    거리컷 튜닝에 쓸 수 있다 — 둘 다 각자의 목적에는 이미 옳고, 필요한 건 **구분 표시**다.
    """
    sel = _FakeSelector(answer="취미 > 종교용품")  # top-1('취미 > 수집용품')과 다른 후보
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == [("취미 > 종교용품", "선물용품")]
    rec = _record(caplog, "category_fallback_top1")
    assert rec.select_changed is True
    # distance 는 채택된 후보 기준, margin 은 택일 이전 값 — 그래서 구분이 필요하다
    assert rec.distance == 0.2169
    assert rec.margin == 0.0095


async def test_untouched_leg_is_not_marked_as_select_changed(caplog) -> None:
    """택일을 거치지 않은 leg 은 `select_changed=False` — distance·margin 이 같은 후보 기준이다."""
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={"청바지": [("여성의류 > 청바지", 0.1224), ("남성의류 > 청바지", 0.19)]},
    )
    with caplog.at_level("INFO"):
        await m.run([CategoryQuery(None, "청바지")])
    assert _record(caplog, "category_fallback_top1").select_changed is False


# ── 거리컷 마진 예외 (§4.5 #115) ──────────────────────────────────────────────


async def test_far_but_confident_leg_is_kept(caplog) -> None:
    """[#115 §4.5] 거리컷을 넘어도 **마진이 두꺼우면** 채택한다 — "칸이 없다"의 신호는 거리가 아니다.

    실측(76 앵커): 식품은 상품명과 leaf 이름이 달라 정답 매핑도 거리가 멀다
    (`돼지 등뼈`→`축산 > 돼지고기` 0.2661, `미역`→`수산 > 해조류` 0.2436). 반면 taxonomy 에 칸이
    **없는** 목적 표현·조어는 여러 후보가 고만고만하게 멀어 마진이 얇다(`부모님 환갑 선물` 0.0249,
    `김밥용 김` 0.0013). 즉 "맞는 칸이 없다"를 직접 재는 지표는 거리가 아니라 **마진**이다.

    거리는 도메인 어휘 차이(상품명≠leaf명)에 오염되지만 마진은 그 오염이 상쇄된다 —
    `청바지`는 거리 0.1224 로 통과하면서 마진은 0.0010 뿐이고, `돼지 갈비`는 거리 0.2299 로
    드롭되면서 마진이 0.0846 이다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "돼지 등뼈": [("축산 > 돼지고기", 0.2661), ("냉장/냉동식품 > 햄/소시지/베이컨", 0.3465)]
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "돼지 등뼈")])
    assert out == [("축산 > 돼지고기", "돼지 등뼈")]  # 종전에는 거리컷이 드롭했다
    rec = _record(caplog, "category_distance_override")
    assert rec.distance == 0.2661
    assert rec.margin == 0.0804


async def test_far_and_ambiguous_leg_is_still_dropped(caplog) -> None:
    """마진이 얇으면 거리컷을 그대로 적용한다 — #115 가 막은 오분류를 되살리지 않는다.

    실측 차단군 최대 마진은 0.0249(`부모님 환갑 선물`→`출산/돌기념품`)이고, 회수 대상 상위 7건은
    0.034~0.085 다. 그 사이가 비어 있어 0.035 로 가른다. 회수 못 한 정답(`참기름` 0.0105)이
    남지만, 실패 방향이 비대칭이라 보수적으로 잡는다 — 미회수는 무필터(종전 동작, 안전)이고
    오분류 유입은 틀린 카테고리로 검색이 좁혀져 정답 상품이 후보에서 아예 빠진다(§4).
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
    assert out == []
    assert _record(caplog, "category_distance_rejected").margin == 0.0249


async def test_far_leg_with_single_hit_is_dropped() -> None:
    """히트가 1건이면 마진이 None — 확신을 잴 수 없으므로 예외를 적용하지 않는다(드롭)."""
    m = _FakeMapper(exact=set(), nearest={}, hits={"조어": [("엉뚱 > 카테고리", 0.31)]})
    assert await m.run([CategoryQuery(None, "조어")]) == []


async def test_select_null_leg_logs_exactly_one_drop_reason(caplog) -> None:
    """택일이 드롭한 leg 은 거리 이벤트를 남기지 않는다 — 드롭 사유는 하나여야 한다(§11).

    §4.4 트리거(`ambiguous`)가 **거리컷 통과분만** 대상으로 하므로, 택일을 거친 leg 의
    `nearest` 거리는 임계 이하다. 따라서 택일이 null 을 내 드롭돼도 위 거리 분기(드롭·§4.5
    마진 예외)에는 닿지 않는다 — 이 불변식이 깨지면 드롭 사유가 둘로 갈려 §11 의 사유별
    분리(정책 드롭 ≠ 품질 신호)가 무너지므로 테스트로 고정한다.

    (마진 예외 자체도 구조적으로 배타적이다: 트리거는 `margin <= 0.02`, 예외는 `margin >= 0.035`.)
    """
    sel = _FakeSelector(answer=None)  # "맞는 후보 없음" → leg 드롭
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)  # 마진 0.0095 → 트리거
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == []
    msgs = [r.msg for r in caplog.records]
    assert "category_select_null" in msgs  # 실제 드롭 사유는 이것 하나뿐
    assert "category_distance_override" not in msgs
    assert "category_distance_rejected" not in msgs


async def test_select_stage_failure_keeps_confirmed_legs(caplog) -> None:
    """[PR #188 리뷰] §4.4 택일 단계가 통째로 실패해도 **이미 확정된 leg 은 살아남는다**.

    LLM 호출은 `gather(return_exceptions=True)` 로 격리돼 있지만, 그 앞뒤의 순수 파이썬 로직
    (설정 접근·마진 필터링·정렬·상한 처리)은 어떤 try 에도 안 감싸여 있었다. 여기서 예외가 나면
    `map_categories` 전체가 던지고, 호출부(graph)의 `except` 가 `category_legs = []` 로 만들어
    **exact 매치와 이미 성공한 임베딩 top-1 까지 전부 버린 채** 그 턴을 무필터로 강등시킨다 —
    이 함수가 docstring 으로 표방한 "실패는 leg 단위로 격리, exact 는 임베딩 실패와 무관하게 보존"
    원칙이 §4.4 추가로 깨져 있었다.

    실제로 밟은 경로다: `category_distance_override_margin` 을 추가했을 때 그 필드가 없는 설정
    객체에서 `AttributeError` 가 나 전 leg 이 사라졌다.
    """
    # category_select_max_calls 가 없는 설정 — §4.4 블록 진입 후 AttributeError
    broken = _settings()
    del broken.category_select_max_calls
    m = _FakeMapper(exact={"PC부품 > CPU"}, nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("WARNING"):
        out = await m.run(
            [CategoryQuery("PC부품 > CPU", "cpu"), CategoryQuery(None, "선물용품")],
            settings=broken,
            select=_FakeSelector(answer="취미 > 종교용품"),
            llm=object(),
        )
    # exact 매치는 보존되고, 택일 대상 leg 은 임베딩 top-1 로 degrade 한다(드롭 아님)
    assert out == [("PC부품 > CPU", "cpu"), ("취미 > 수집용품", "선물용품")]
    assert any(r.msg == "category_select_stage_failed" for r in caplog.records)


async def test_selected_distance_is_rounded_like_top1(caplog) -> None:
    """[PR #188 리뷰] 택일이 확정한 거리도 top-1 과 **같은 정밀도**(4자리)로 남긴다.

    `_top1_with_margin` 은 `round(distance, 4)` 로 저장하는데 택일 경로는 `candidates_by_leg` 의
    원시 float 를 그대로 실었다. 같은 `distance` 필드에 leg 마다 정밀도가 달라지면 §11 이 명시한
    임계 재튜닝(distance 분포)에 노이즈가 섞인다.

    정밀도만의 문제가 아니다 — 거리컷은 `picked[1] > distance_max` 비교라, 임계 근처에서 원시값과
    반올림값이 **채택/드롭을 가른다**(0.220001 은 드롭, 0.22 는 채택).
    """
    sel = _FakeSelector(answer="취미 > 종교용품")
    m = _FakeMapper(
        exact=set(),
        nearest={},
        # 2위를 일부러 4자리 밖 정밀도로 준다 — 택일이 이걸 고르면 로그에 원시값이 샌다
        hits={"선물용품": [("취미 > 수집용품", 0.2074), ("취미 > 종교용품", 0.21691234)]},
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery(None, "선물용품")], select=sel, llm=object())
    assert out == [("취미 > 종교용품", "선물용품")]
    assert _record(caplog, "category_selected").distance == 0.2169
    assert _record(caplog, "category_fallback_top1").distance == 0.2169


async def test_input_legs_are_truncated_to_fanout_max(caplog) -> None:
    """[PR #188 리뷰] 입력 leg 수를 `category_fanout_max` 로 방어적으로 절단한다.

    매핑은 leg 당 raw·query **두 앵커**를 gather 로 동시 조회하므로 한 턴의 pg 커넥션 점유는
    `2 × leg 수`다. `config._require_pool_covers_anchor_concurrency` 는 `pool >= 2 × fanout_max`
    를 기동 시 강제하지만, "leg 수 ≤ fanout_max"라는 전제는 호출부(`decompose._parse_category_queries`·
    `expand_needs`)의 절단에만 의존하고 있었다 — 새 호출부가 절단을 빠뜨리면 풀이 넘치고 증상은
    **다른 사용자 요청의 PoolTimeout** 으로 나타나 원인 추적이 어렵다. 불변식을 이 함수에서 보장한다.

    절단이 실제로 일어나면 경고로 남긴다 — 조용히 자르면 호출부의 계약 위반이 드러나지 않는다.
    """
    legs = [CategoryQuery(None, f"상품{i}") for i in range(8)]
    m = _FakeMapper(
        exact=set(), nearest={f"상품{i}": f"카테고리 > C{i}" for i in range(8)}, hits=None
    )
    with caplog.at_level("WARNING"):
        out = await m.run(legs, settings=_settings(fanout_max=3))
    assert [c for c, _q in out] == ["카테고리 > C0", "카테고리 > C1", "카테고리 > C2"]
    assert len(m._embedded) == 3  # 앵커 조회도 3건만 — 동시성 상한이 실제로 지켜진다
    rec = _record(caplog, "category_legs_truncated")
    assert rec.given == 8
    assert rec.fanout_max == 3


async def test_stage_failure_logs_error_type_for_triage(caplog) -> None:
    """[PR #188 리뷰] 단계 실패 로그에 예외 **타입**을 실어 인프라 장애와 코드 버그를 가른다.

    두 단계 모두 실제 I/O 실패는 이미 `gather(return_exceptions=True)` 로 앵커/leg 단위 격리돼
    각각 `category_leg_search_failed`·`category_select_unavailable` 로 남는다 — 바깥 except 에는
    **도달하지 못한다**. 따라서 바깥 except 가 잡는 것은 embed 배치 실패(I/O) 아니면 순수 로직
    버그인데, 이벤트 이름만으로는 둘이 구분되지 않아 triage 가 어렵다(§11).

    try 범위를 I/O 로 좁히는 대안은 채택하지 않는다 — 순수 로직 버그가 그대로 전파돼
    `map_categories` 가 통째로 던지고 호출부가 `category_legs = []` 로 만들어 **exact 매치까지**
    사라진다(PR #188 앞선 리뷰가 고치라고 한 바로 그 문제). 보호는 유지하고 귀속만 고친다.
    """
    m = _FakeMapper(exact=set(), nearest={}, embed_raises=True)
    with caplog.at_level("WARNING"):
        await m.run([CategoryQuery(None, "청바지")])
    assert _record(caplog, "category_embed_failed").error_type == "RuntimeError"

    broken = _settings()
    del broken.category_select_max_calls  # §4.4 단계에서 AttributeError
    m2 = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    with caplog.at_level("WARNING"):
        await m2.run(
            [CategoryQuery(None, "선물용품")],
            settings=broken,
            select=_FakeSelector(answer=None),
            llm=object(),
        )
    assert _record(caplog, "category_select_stage_failed").error_type == "AttributeError"


# ── unresolved — 전개 트리거 입력 (#217, DESIGN-198 §4·§6.2) ─────────────────
#
# 매핑이 canonical 을 못 낸 leg 을 상류(graph)에 알린다. 종전 반환형
# `list[tuple[str, str|None]]` 으로는 "드롭됨"과 "애초에 신호 없음"이 구분되지 않아, 호출부가
# 전개 여부를 판정할 수 없었다.
#
# **무엇을 담고 무엇을 담지 않는지가 이 계약의 전부다.** 인프라 실패(조회 예외)·시드 결측(히트 0건)을
# 섞으면 pg 순간 장애가 LLM 전개를 부른다 — PR #188 이 `error_type` 으로 "인프라 장애 vs 코드 버그"를
# 가른 것과 같은 원칙이다.


async def test_unresolved_collects_distance_rejected_leg() -> None:
    """§4 ① 거리컷 드롭 → unresolved. "맞는 칸이 taxonomy 에 없다"는 신호라 전개 대상이다.

    실측 `"김밥 재료"` 0.3027 / 마진 0.0054 대역(§4.5 ②) — 초판 marker 목록에 `재료` 가 없어
    놓치던 바로 그 경로다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "김밥 재료": [
                ("채소 > 파/마늘/양념채소", 0.3027),
                ("냉장/냉동식품 > 밥류", 0.3081),
            ]
        },
    )
    out = await m.run_full([CategoryQuery(None, "김밥 재료")])
    assert out.legs == []
    assert out.unresolved == ["김밥 재료"]


async def test_unresolved_excludes_margin_override_leg() -> None:
    """§4.5 마진 예외로 **채택된** leg 은 unresolved 가 아니다 — 거리만 보면 안 되는 이유.

    거리는 멀지만 1위가 확실히 이기면 "맞는 칸이 분명히 하나 있다"는 뜻이라 채택한다. 실측
    `"신학기 준비"` 0.2715 / 마진 0.0483 → `문구/사무용품 > 학용품/학습준비물` 은 **정답**이고,
    이걸 전개로 갈아엎으면 손해다(§4.5 ④).
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "신학기 준비": [
                ("문구/사무용품 > 학용품/학습준비물", 0.2715),
                ("문구/사무용품 > 필기구", 0.3198),
            ]
        },
    )
    out = await m.run_full([CategoryQuery(None, "신학기 준비")])
    assert out.legs == [("문구/사무용품 > 학용품/학습준비물", "신학기 준비")]
    assert out.unresolved == []


async def test_unresolved_excludes_mapped_leg() -> None:
    """거리 이내로 채택된 leg 은 unresolved 가 아니다 — 오탐 0 보장의 뿌리(§4.5 ①).

    `"한방재료"` 0.1443 / 마진 0.0640 대역. 초판 marker 에 `재료` 를 넣었다면 이 leg 이 전개로
    파괴됐다(이슈 본문이 실측으로 기각한 처방).
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "한방재료": [
                ("건강식품 > 인삼/한방재료", 0.1443),
                ("건강관리용품 > 한방건강보조용품", 0.2083),
            ]
        },
    )
    out = await m.run_full([CategoryQuery(None, "한방재료")])
    assert out.legs == [("건강식품 > 인삼/한방재료", "한방재료")]
    assert out.unresolved == []


async def test_unresolved_collects_select_null_leg() -> None:
    """§4 ② 택일 null → unresolved. "후보 top-k 중 맞는 것이 없다"는 ① 과 같은 의미다.

    거리는 가까운데 뜻이 틀린 추상 라벨이 여기 걸린다 — `'선물용품'` → `취미 > 수집용품` 0.2074 /
    마진 0.0095(§4.5 ⑤ 의 "오분류지만 마진 얇음" 4건). 거리컷으로는 못 잡고 택일이 잡아낸다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    out = await m.run_full(
        [CategoryQuery(None, "선물용품")],
        select=_FakeSelector(answer=None),
        llm=object(),
    )
    assert out.legs == []
    assert out.unresolved == ["선물용품"]


async def test_unresolved_excludes_search_failure() -> None:
    """§4 ③ 조회 예외는 unresolved 가 **아니다** — 인프라 장애가 LLM 전개를 부르면 안 된다.

    실패 원인이 발화 내용이 아니라 pg 경합·타임아웃이라, 내용 기반 처방(전개)을 붙일 근거가 없다.
    전개해봐야 같은 인프라를 다시 두드릴 뿐이고 LLM 비용만 든다. PR #188 이 `error_type` 으로
    "인프라 장애 vs 코드 버그"를 가른 것과 같은 원칙(§4).
    """
    m = _FakeMapper(exact=set(), nearest={}, search_raises_for={"청바지"})
    out = await m.run_full([CategoryQuery(None, "청바지")])
    assert out.legs == []
    assert out.unresolved == []


async def test_unresolved_excludes_embed_failure() -> None:
    """embed 전면 실패도 unresolved 가 아니다 — 조회 예외와 같은 이유(인프라·로직 실패)."""
    m = _FakeMapper(exact=set(), nearest={}, embed_raises=True)
    out = await m.run_full([CategoryQuery(None, "집들이 선물")])
    assert out.legs == []
    assert out.unresolved == []


async def test_unresolved_excludes_zero_hit_leg() -> None:
    """§4 ④ 히트 0건도 unresolved 가 아니다 — `categories` 미시드 신호라 전개로 풀리지 않는다.

    전개된 상품명도 결국 같은 빈 사전을 조회한다. 이건 품질 메트릭(`category_unmapped`)으로
    남겨야 할 운영 신호이지 발화 해석 문제가 아니다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={"디퓨저": []})
    out = await m.run_full([CategoryQuery(None, "디퓨저")])
    assert out.legs == []
    assert out.unresolved == []


async def test_unresolved_is_empty_when_no_signal() -> None:
    """신호 없는 leg(raw·query 모두 없음)은 unresolved 가 아니다 — 매핑을 시도조차 안 했다.

    이 경우는 D1(`no_legs`)이 별도로 판정한다(§4). 여기서 unresolved 로 새면 사유가 이중으로
    기록돼 관측 분포가 오염된다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={})
    out = await m.run_full([CategoryQuery(None, None), CategoryQuery(None, "  ")])
    assert out.legs == []
    assert out.unresolved == []


async def test_unresolved_keeps_partial_success() -> None:
    """일부만 실패하면 성공 leg 은 legs 에, 실패 leg 만 unresolved 에 — 합집합 배선의 입력(§6).

    `"이사 가는데 냉장고랑 필요한 것들"` 대역. 종전 교체 배선은 전개가 트리거되면 냉장고까지
    날렸는데, 이제 성공분을 보존한 채 전개분을 더한다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "냉장고": [("냉장고 > 일반 냉장고", 0.09), ("냉장고 > 김치 냉장고", 0.19)],
            "이사 필요한 것들": [("수납정리용품 > 이사박스", 0.31), ("생활잡화 > 정리소품", 0.32)],
        },
    )
    out = await m.run_full([CategoryQuery(None, "냉장고"), CategoryQuery(None, "이사 필요한 것들")])
    assert out.legs == [("냉장고 > 일반 냉장고", "냉장고")]
    assert out.unresolved == ["이사 필요한 것들"]


async def test_unresolved_uses_winning_anchor_text() -> None:
    """unresolved 에는 **이긴 앵커 텍스트**를 담는다 — 관측이 목적이다(§6.2).

    전개 자체는 발화 원문으로 하므로 이 값이 트리거 판정을 바꾸지는 않는다. 다만 어떤 앵커가
    실패했는지가 로그에 남아야 하류 `category_distance_rejected` 의 거리·마진과 조인해 임계를
    재튜닝할 수 있다(§10). query 우선 규약(DESIGN-59 §4.3.1)을 그대로 따른다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "선물": [("취미 > 수집용품", 0.31), ("취미 > 파티용품", 0.32)],
            "환갑 선물용": [("출산/돌기념품 > 답례품", 0.30), ("출산/돌기념품 > 돌잔치용품", 0.31)],
        },
    )
    out = await m.run_full([CategoryQuery("선물", "환갑 선물용")])
    assert out.legs == []
    assert out.unresolved == ["환갑 선물용"]  # raw("선물") 이 아니라 query 앵커


async def test_select_max_calls_override_caps_select(caplog) -> None:
    """[#217 PR 리뷰] `select_max_calls` 주입이 settings 기본값을 대체한다 — 턴당 예산 공유의 수단.

    매핑이 턴에 2회 불릴 때(#217 §6.1) 호출부가 남은 예산을 넘겨야 `category_select_max_calls`
    ("**턴당** 택일 LLM 호출 상한")가 지켜진다. 주입이 없으면 종전대로 settings 값을 쓴다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={"선물용품": _AMBIGUOUS["선물용품"]})
    sel = _FakeSelector(answer=None)
    with caplog.at_level("INFO"):
        out = await m.run_full(
            [CategoryQuery(None, "선물용품")],
            settings=_settings(select_max_calls=2),
            select=sel,
            llm=object(),
            select_max_calls=0,  # 첫 매핑이 예산을 다 썼다고 가정
        )
    assert sel.calls == []  # 예산 0 → 택일 미호출
    assert out.select_calls == 0
    assert out.legs == [("취미 > 수집용품", "선물용품")]  # 임베딩 top-1 유지(종전 degrade)
    assert _record(caplog, "category_select_unavailable").reason == "max_calls"


async def test_select_calls_reports_attempted_budget() -> None:
    """`select_calls` 는 **시도 수**다 — 실패한 택일도 비용이 발생하므로 예산에서 빠지면 안 된다.

    관측 기록(`observer.record_model_call`)을 호출 **전**에 하는 것과 같은 이유다. 결과를 보고 세면
    LLM 오류가 난 호출이 예산에서 빠져 두 번째 매핑이 상한을 넘겨 쓴다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits={"선물용품": _AMBIGUOUS["선물용품"]})

    async def _boom(*_a, **_k):
        raise LLMError("select down")

    out = await m.run_full(
        [CategoryQuery(None, "선물용품")],
        settings=_settings(select_max_calls=2),
        select=_boom,
        llm=object(),
    )
    assert out.select_calls == 1  # 던졌어도 1회 소비로 센다
    assert out.legs == [("취미 > 수집용품", "선물용품")]  # 판정 실패 → top-1 유지(§4.4)


class _ExactBoomAfter:
    """`in` 검사를 N회까지만 통과시키고 그 뒤 터진다 — 조립 루프 안에서 예외를 만드는 유일한 주입점.

    루프는 설정을 읽지 않고(§4.5 튜너블은 밖에서 한 번에 읽는다) 이미 검증된 canonical 만 다뤄서
    밖에서 실패를 주입할 곳이 여기밖에 없다. `exact` 는 `exact_lookup` seam 의 반환값이라 이렇게
    바꿔 끼울 수 있다.
    """

    def __init__(self, allow: int) -> None:
        self.allow = allow
        self.calls = 0

    def __contains__(self, item) -> bool:
        self.calls += 1
        if self.calls > self.allow:
            raise RuntimeError("assembly loop blew up")
        return False


async def test_assembly_failure_keeps_confirmed_legs(caplog) -> None:
    """[#217 PR 리뷰] 조립 루프 예외가 **이미 확정된 leg 과 예산 회계를 버리지 않는다**.

    루프가 통째로 던지면 호출부(`graph._map_or_empty`)가 빈 legs 로 degrade 해 **DB 검증된 exact
    매치와 채택 canonical 까지** 사라진다 — PR #188 이 택일 단계를 감쌀 때 든 논거와 같고, 이 루프도
    같은 노출을 갖고 있었다.

    `select_calls` 회계가 함께 살아남는 것도 중요하다. 유실되면 두 번째(전개) 매핑이 턴당 예산을
    다시 받아 상한이 배수로 깨질 수 있다(§6.1). 지금은 같은 예외가 `unresolved` 도 비워 두 번째
    호출 자체가 안 일어나 우연히 막히지만, **그 안전성이 결합에 기대지 않게** 만든다.
    """
    # raw 2개 → need_idx 계산에서 2회, 조립 루프에서 leg0 이 3회째. leg1(4회째)에서 터진다.
    exact = _ExactBoomAfter(allow=3)
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "티비": [("가전 > TV", 0.10), ("가전 > 모니터", 0.30)],
            "냉장고": [("가전 > 냉장고", 0.11), ("가전 > 김치냉장고", 0.31)],
        },
    )
    m.exact_lookup = lambda values, dsn: exact  # type: ignore[assignment]
    with caplog.at_level("WARNING"):
        out = await m.run_full([CategoryQuery("가전1", "티비"), CategoryQuery("가전2", "냉장고")])
    assert out.legs == [("가전 > TV", "티비")]  # 터지기 전에 확정된 leg 은 살아남는다
    assert _record(caplog, "category_assembly_failed").error_type == "RuntimeError"


# ── expansion_leaves — 광역 발화 fan-out 후보 (#222) ──────────────────────────
#
# 이슈 원안(top-k 공통 조상으로 광역/협소 판정)은 오케스트레이터 실측으로 기각(정확도 0.50,
# 우연 수준). 대신 canonical 을 못 낸 leg(unresolved, #217 이 이미 만든 신호)의 앵커 top-N leaf 를
# 그대로 fan-out 후보로 낸다. `unresolved` 와 같은 조건(거리컷 드롭·택일 null)에서만 채우고,
# 조회 예외·히트 0건은 담지 않는다(후보 자체가 없다).


async def test_select_candidates_stay_at_top_k_even_when_expand_legs_is_larger() -> None:
    """[#222 필수 안전장치] `category_expand_legs` 를 올려도 택일(§4.4) 후보 수는 `category_top_k`
    그대로다.

    조회 k 는 `max(category_top_k, category_expand_legs)` 로 늘어 pg 왕복 없이 더 많은 히트를
    받아오지만, `select_category` 로 넘기는 후보는 여전히 앞 `category_top_k` 개만 슬라이스한다.
    여기를 빠뜨리면 택일 후보가 5개에서 8개로 늘어 #115 가 튜닝한 동작이 조용히 바뀐다.
    """
    hits = [
        ("취미 > 수집용품", 0.2000),
        ("취미 > 종교용품", 0.2010),  # margin 0.001 ≤ select_margin_max(0.02) → 택일 트리거
        ("도서/음반 > 독서용품", 0.2050),
        ("생활잡화 > 정리소품", 0.2080),
        ("문구/사무용품 > 학용품", 0.2090),
        ("패션잡화 > 모자", 0.2100),  # top_k(5) 밖 — 후보에 들어오면 안 됨
        ("스포츠 > 캠핑용품", 0.2110),
        ("주방용품 > 잔/컵", 0.2120),
    ]
    sel = _FakeSelector(answer="취미 > 종교용품")
    m = _FakeMapper(exact=set(), nearest={}, hits={"선물용품": hits})
    out = await m.run_full(
        [CategoryQuery(None, "선물용품")],
        settings=_settings(expand_legs=8),  # top_k(기본 5) < expand_legs(8)
        select=sel,
        llm=object(),
    )
    assert len(sel.calls) == 1
    _query, candidates = sel.calls[0]
    assert len(candidates) == 5  # category_top_k — 8 이 아니다
    assert candidates == tuple(c for c, _ in hits[:5])
    assert out.legs == [("취미 > 종교용품", "선물용품")]


async def test_expansion_leaves_filled_from_distance_rejected_leg() -> None:
    """§4 ① 거리컷 드롭 leg → `expansion_leaves` 가 그 앵커의 top-N leaf 로 채워진다.

    `unresolved` 를 채우는 바로 그 조건이다 — 광역 발화("김밥 재료")가 leaf 하나로 못 접히고
    카테고리가 통째로 사라지는 문제를 이 후보로 메운다.
    """
    hits = [
        ("채소 > 파/마늘/양념채소", 0.3027),
        ("냉장/냉동식품 > 밥류", 0.3081),
        ("수산 > 어묵/맛살", 0.3120),
    ]
    m = _FakeMapper(exact=set(), nearest={}, hits={"김밥 재료": hits})
    out = await m.run_full([CategoryQuery(None, "김밥 재료")], settings=_settings(expand_legs=3))
    assert out.legs == []
    assert out.unresolved == ["김밥 재료"]
    assert out.expansion_leaves == [(c, "김밥 재료") for c, _ in hits]


async def test_expansion_leaves_filled_from_select_null_leg() -> None:
    """§4 ② 택일이 "맞는 후보 없음" → `expansion_leaves` 도 채워진다(①과 같은 뜻).

    `nearest[i]` 는 택일이 None 을 골라도 원래 임베딩 top-1 로 남아 있으므로, 그 앵커의 top-k
    (택일 이전에 조회된 원본 히트)를 그대로 fan-out 후보로 쓸 수 있다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits=_AMBIGUOUS)
    out = await m.run_full(
        [CategoryQuery(None, "선물용품")],
        settings=_settings(expand_legs=3),
        select=_FakeSelector(answer=None),
        llm=object(),
    )
    assert out.legs == []
    assert out.unresolved == ["선물용품"]
    assert out.expansion_leaves == [(c, "선물용품") for c, _ in _AMBIGUOUS["선물용품"]]


async def test_expansion_leaves_empty_on_search_failure() -> None:
    """§4 ③ 조회 예외 leg 은 `expansion_leaves` 에 담지 않는다 — 후보 자체가 없다.

    `unresolved` 도 비지만(기존 계약), 여기서는 후보 리스트가 별도 필드라 **둘 다** 비어야 한다.
    """
    m = _FakeMapper(exact=set(), nearest={}, search_raises_for={"청바지"})
    out = await m.run_full([CategoryQuery(None, "청바지")], settings=_settings(expand_legs=3))
    assert out.unresolved == []
    assert out.expansion_leaves == []


async def test_expansion_leaves_empty_on_zero_hits() -> None:
    """§4 ④ 히트 0건 leg 도 `expansion_leaves` 에 담지 않는다 — 담을 후보 자체가 없다."""
    m = _FakeMapper(exact=set(), nearest={}, hits={"디퓨저": []})
    out = await m.run_full([CategoryQuery(None, "디퓨저")], settings=_settings(expand_legs=3))
    assert out.unresolved == []
    assert out.expansion_leaves == []


async def test_expansion_leaves_capped_at_expand_legs() -> None:
    """확장 leg 수는 `category_expand_legs` 를 넘지 않는다(`dedup_truncate` 절단)."""
    hits = [(f"카테고리{i} > 소분류{i}", 0.30 + i * 0.001) for i in range(8)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"애매한 발화": hits})
    out = await m.run_full([CategoryQuery(None, "애매한 발화")], settings=_settings(expand_legs=3))
    assert out.expansion_leaves == [(c, "애매한 발화") for c, _ in hits[:3]]


async def test_expansion_leaves_dedup_across_legs() -> None:
    """멀티 leg 에서 같은 canonical 이 겹치면 dedup_truncate 가 첫 등장만 남긴다(기존 legs 규약과 동일)."""
    hits_a = [("채소 > 파/마늘/양념채소", 0.30), ("수산 > 어묵/맛살", 0.31)]
    hits_b = [("채소 > 파/마늘/양념채소", 0.29), ("냉장식품 > 밥류", 0.32)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"김밥 재료": hits_a, "떡볶이 재료": hits_b})
    out = await m.run_full(
        [CategoryQuery(None, "김밥 재료"), CategoryQuery(None, "떡볶이 재료")],
        settings=_settings(expand_legs=4),
    )
    canonicals = [c for c, _ in out.expansion_leaves]
    assert canonicals.count("채소 > 파/마늘/양념채소") == 1  # 중복 제거


# ── 라운드로빈 인터리브 (PR #318 리뷰 R5-1) ────────────────────────────────────
#
# `category_expand_legs` 는 **턴 전체 상한**이지 leg 당 상한이 아니다. unresolved leg 이 여럿일 때
# leg 순서대로 이어 붙이기만 하면 앞 leg 이 예산을 통째로 채우고 뒤 leg 은 0개가 된다 —
# "캠핑용품이랑 낚시용품 추천해줘"에서 둘 다 매핑 실패하면 캠핑 8개만 나오고 낚시가 사용자가
# 알아챌 방법도 없이 조용히 사라진다(R4-1 로 확장 턴은 니즈별 목록 분할도 안 하므로 더 그렇다).
# `recommendation/graph._merge_fanout_results` 와 같은 round-robin 규약으로 고친다.


async def test_expansion_leaves_interleaved_when_both_legs_exceed_cap() -> None:
    """leg 2개가 각각 cap 이상 히트 → 결과에 두 leg 의 후보가 모두 들어간다(어느 한쪽도 0이 아니다).

    수정 전에는 leg 0(캠핑)이 `category_expand_legs`(8) 를 전부 채워 leg 1(낚시)은 0개였다.
    """
    hits_camp = [(f"캠핑 > 종류{i}", 0.30 + i * 0.001) for i in range(10)]
    hits_fish = [(f"낚시 > 종류{i}", 0.30 + i * 0.001) for i in range(10)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"캠핑용품": hits_camp, "낚시용품": hits_fish})
    out = await m.run_full(
        [CategoryQuery(None, "캠핑용품"), CategoryQuery(None, "낚시용품")],
        settings=_settings(expand_legs=8),
    )
    canonicals = [c for c, _ in out.expansion_leaves]
    assert len(out.expansion_leaves) == 8  # 턴 전체 상한
    camp = sum(1 for c in canonicals if c.startswith("캠핑"))
    fish = sum(1 for c in canonicals if c.startswith("낚시"))
    assert camp > 0 and fish > 0  # 어느 한쪽도 0이 아니다 — 이게 이 결함의 핵심
    assert camp == 4 and fish == 4  # 라운드로빈이면 정확히 반반


async def test_expansion_leaves_each_leg_gets_at_least_one_when_uneven_split() -> None:
    """leg 3개 + `category_expand_legs=4`(나누어떨어지지 않음) 도 각 leg 이 최소 1개는 받는다."""
    hits_a = [(f"A > 종류{i}", 0.30 + i * 0.001) for i in range(4)]
    hits_b = [(f"B > 종류{i}", 0.30 + i * 0.001) for i in range(4)]
    hits_c = [(f"C > 종류{i}", 0.30 + i * 0.001) for i in range(4)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"가": hits_a, "나": hits_b, "다": hits_c})
    out = await m.run_full(
        [CategoryQuery(None, "가"), CategoryQuery(None, "나"), CategoryQuery(None, "다")],
        settings=_settings(expand_legs=4),
    )
    canonicals = [c for c, _ in out.expansion_leaves]
    assert len(out.expansion_leaves) == 4
    assert any(c.startswith("A") for c in canonicals)
    assert any(c.startswith("B") for c in canonicals)
    assert any(c.startswith("C") for c in canonicals)


async def test_expansion_leaves_big_leg_backfills_after_small_leg_exhausted() -> None:
    """후보가 적은 leg(1개)이 먼저 소진돼도 나머지 예산을 후보가 많은 leg 이 이어받아 총 개수가
    cap 을 채운다(예산 누수 없음) — 적은 leg 을 건너뛰고 계속 진행해야 하는 이유."""
    hits_small = [("작은카테고리 > 유일종류", 0.30)]
    hits_big = [(f"큰카테고리 > 종류{i}", 0.30 + i * 0.001) for i in range(10)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"희귀 발화": hits_small, "흔한 발화": hits_big})
    out = await m.run_full(
        [CategoryQuery(None, "희귀 발화"), CategoryQuery(None, "흔한 발화")],
        settings=_settings(expand_legs=8),
    )
    assert len(out.expansion_leaves) == 8  # 예산이 전부 채워진다 — 적은 leg 때문에 새지 않는다
    canonicals = [c for c, _ in out.expansion_leaves]
    assert canonicals.count("작은카테고리 > 유일종류") == 1  # 적은 leg 의 유일 후보도 살아남는다
    assert (
        sum(1 for c in canonicals if c.startswith("큰카테고리")) == 7
    )  # 나머지는 큰 leg 이 채운다


async def test_expansion_leaves_single_leg_unaffected_by_interleave() -> None:
    """[회귀 고정] 단일 leg 턴은 인터리브를 거쳐도 종전과 동일한 순서·개수를 낸다."""
    hits = [(f"카테고리{i} > 소분류{i}", 0.30 + i * 0.001) for i in range(8)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"애매한 발화": hits})
    out = await m.run_full([CategoryQuery(None, "애매한 발화")], settings=_settings(expand_legs=3))
    assert out.expansion_leaves == [(c, "애매한 발화") for c, _ in hits[:3]]


# ── R6-2 (PR #318 리뷰 3차) — `category_expand_enabled` 킬스위치가 `map_categories` 까지 닿는다 ──
#
# 종전엔 이 플래그를 검사하는 곳이 `buyer/graph.py` 의 소비 지점뿐이라, 꺼도 조회 k 가 그대로
# 늘어난 채 나가고 `_collect_expansion_leaves` 가 계속 돌아 로그가 계속 쌓였다 — 부하·로그
# 노이즈 때문에 롤백하는 인시던트에서 "롤백이 안 되는 롤백 스위치"였다.


async def test_expand_disabled_keeps_search_k_at_top_k() -> None:
    """[R6-2] `category_expand_enabled=False` 면 조회 k 가 `category_top_k` 로 고정된다
    (`max(top_k, expand_legs)` 로 늘지 않는다)."""
    calls: list = []

    def _search(vec, dsn, *, k):
        calls.append(k)
        return [("아무 카테고리 > 소분류", 0.30)]

    await map_categories(
        category_queries=[CategoryQuery(None, "화장품")],
        utterance="화장품 추천해줘",
        settings=_settings(top_k=5, expand_legs=8, expand_enabled=False),
        embed=lambda texts: [[0.0] for _ in texts],
        search_top_k=_search,
        exact_lookup=lambda values, dsn: set(),
    )
    assert calls and all(k == 5 for k in calls)  # category_top_k — expand_legs(8) 아님


async def test_expand_disabled_yields_no_expansion_leaves() -> None:
    """[R6-2] 킬스위치가 꺼져 있으면 거리컷 드롭 leg 이 있어도 `expansion_leaves` 는 비어 있다 —
    `_collect_expansion_leaves` 자체가 호출되지 않는다."""
    hits = [("채소 > 파/마늘/양념채소", 0.30), ("냉장/냉동식품 > 밥류", 0.31)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"김밥 재료": hits})
    out = await m.run_full(
        [CategoryQuery(None, "김밥 재료")],
        settings=_settings(expand_legs=8, expand_enabled=False),
    )
    assert out.unresolved == ["김밥 재료"]  # #217 전개 트리거는 그대로 살아있다
    assert out.expansion_leaves == []


async def test_expand_disabled_does_not_change_normal_mapping_result() -> None:
    """[R6-2 회귀 고정] 킬스위치를 꺼도 매핑 본체 결과(legs·unresolved)는 켰을 때와 동일하다 —
    이 플래그는 확장 후보 수집에만 영향을 준다."""
    hits = {
        "김밥 재료": [
            ("채소 > 파/마늘/양념채소", 0.3027),
            ("냉장/냉동식품 > 밥류", 0.3081),
        ]
    }
    out_on = await _FakeMapper(exact=set(), nearest={}, hits=hits).run_full(
        [CategoryQuery(None, "김밥 재료")], settings=_settings(expand_enabled=True)
    )
    out_off = await _FakeMapper(exact=set(), nearest={}, hits=hits).run_full(
        [CategoryQuery(None, "김밥 재료")], settings=_settings(expand_enabled=False)
    )
    assert out_on.legs == out_off.legs == []
    assert out_on.unresolved == out_off.unresolved == ["김밥 재료"]


async def test_expansion_leaves_log_carries_anchor_kind_count_and_mids(caplog) -> None:
    """관측 로그 `category_expansion_leaves` 가 anchor_kind·count·중복 제거된 중분류를 싣는다."""
    hits = [
        ("메이크업 > 페이스메이크업", 0.30),
        ("스킨케어 > 스킨/토너", 0.31),
        ("스킨케어 > 에센스/세럼", 0.32),
    ]
    m = _FakeMapper(exact=set(), nearest={}, hits={"화장품": hits})
    with caplog.at_level("INFO"):
        out = await m.run_full([CategoryQuery(None, "화장품")], settings=_settings(expand_legs=3))
    assert out.expansion_leaves  # 전제
    record = _record(caplog, "category_expansion_leaves")
    assert record.anchor_kind == "query"
    assert record.count == 3
    assert record.mids == ["메이크업", "스킨케어"]  # 중복 제거(순서 보존)


# ── #428 전개 후 재매핑 전용 대분류 합의(consensus) 필터 ──────────────────────────
#
# 전개 아이템(#217)은 하나의 니즈에서 나온 형제들이라 서로의 카테고리를 검증할 수 있다 — 여러
# 형제가 공통 지목한 대분류가 사용자가 말한 상품군이고, 한 형제만 지목한 대분류는 동음이의어·
# 표면 근접이 만든 노이즈다. `sibling_expansion=True` 로 이 필터가 켜진다(원 매핑에는 안 켠다 —
# "캠핑용품이랑 낚시용품"처럼 서로 다른 니즈는 대분류가 갈리는 것이 정상).

# #428 로컬 pg-catalog 실측 (2026-08-07, 사전 1,007행 / 임베딩 결측 0)
_FRUIT_HITS = {
    "바나나": [
        ("과일 > 수입과일", 0.2908),
        ("과일 > 국산과일", 0.3220),
        ("과일 > 냉동/간편과일", 0.3278),
        ("과자/간식 > 원물간식", 0.3287),
        ("과일 > 과일선물세트", 0.3307),
        ("꽃/원예 > 꽃/식물", 0.3332),
        ("유아동식/영양제 > 유아동 간식", 0.3367),
        ("과자/간식 > 빵/베이커리", 0.3467),
    ],
    "사과": [
        ("과일 > 국산과일", 0.2732),
        ("과일 > 수입과일", 0.2812),
        ("과일 > 과일선물세트", 0.2960),
        ("과일 > 냉동/간편과일", 0.3232),
        ("과자/간식 > 원물간식", 0.3249),
        ("꽃/원예 > 꽃/식물", 0.3323),
        ("커피/생수/음료 > 주스/과즙음료", 0.3348),
        ("건과/견과 > 견과류", 0.3433),
    ],
    "배": [
        ("여성가방 > 백팩", 0.3184),
        ("신생아의류 (0~24개월) > 배냇저고리", 0.3248),
        ("여성가방 > 스포츠가방", 0.3292),
        ("실버용품 > 환자용 배변용품", 0.3297),
        ("유아목욕/스킨케어 > 유아목욕용품", 0.3323),
        ("과일 > 국산과일", 0.3330),
        ("과자/간식 > 빵/베이커리", 0.3354),
        ("구기/라켓/스포츠 > 야구", 0.3358),
    ],
    "오렌지": [
        ("커피/생수/음료 > 주스/과즙음료", 0.3164),
        ("과일 > 수입과일", 0.3216),
        ("과일 > 과일선물세트", 0.3244),
        ("과일 > 국산과일", 0.3287),
        ("꽃/원예 > 꽃/식물", 0.3417),
        ("과자/간식 > 원물간식", 0.3419),
        ("가공식품 > 잼", 0.3455),
        ("과일 > 냉동/간편과일", 0.3461),
    ],
}


async def test_consensus_filter_keeps_majority_mid_drops_homonym_noise() -> None:
    """[#428 핵심 회귀, 리뷰 1차 F-1 갱신] 과일 4형제 전개에서 동음이의어 노이즈(여성가방·
    신생아의류·실버용품·유아목욕·구기라켓스포츠)가 전부 사라지고 **과일 4종만 정확히** 남는다.

    "배"가 동음이의어(과일/가방/배냇저고리/배변용품)라 top-8이 무관 카테고리로 흩어지는데,
    형제의 **최근접(top-1)** 지지를 세면(리뷰 1차 F-1) 바나나→과일(수입과일)·사과→과일
    (국산과일)로 과일이 2/4 지지를 얻어 최다이고(배→여성가방, 오렌지→커피/생수/음료는 각 1
    지지), 그 합의로 "배"의 노이즈만 걸러낸다(#428 이슈 코멘트 ④⑤). top-1 만으로 세므로
    "과자/간식"은 꼬리 순위에서만 등장해(초판의 결함) 승자가 되지 않는다 — 이슈 마지막
    코멘트가 적은 목표("8개가 아니라 과일 계열 4개로 깔끔하게")와 정확히 일치한다.

    [#428 리뷰 3차 R3-1] 이 케이스가 R3-1 가드(승자 대분류가 형제 전원의 후보에 있어야
    함)를 통과하는 이유: "배"의 후보 6위에 `과일 > 국산과일`이 있어 "배" leg 의 `kept`
    가 비지 않는다 — 가드가 들어가도 `#428` 본체는 약해지지 않는다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits=_FRUIT_HITS)
    out = await m.run_full(
        [CategoryQuery(None, name) for name in ["바나나", "사과", "배", "오렌지"]],
        settings=_settings(expand_legs=8),
        sibling_expansion=True,
    )
    canonicals = {c for c, _ in out.expansion_leaves}
    assert canonicals == {
        "과일 > 국산과일",
        "과일 > 수입과일",
        "과일 > 냉동/간편과일",
        "과일 > 과일선물세트",
    }
    mids = {c.split(" > ", 1)[0] for c in canonicals}
    assert mids == {"과일"}


async def test_consensus_log_carries_source_legs_when_applied(caplog) -> None:
    """[#428 리뷰 5차 R5-2] 합의가 **적용**된 케이스에서 `category_expansion_consensus` 로그에
    `source_legs`(이번 매핑의 입력 leg 수 = 전개 아이템 수)가 실린다 — R5-1 이 게이트를
    상류(`graph.py`)로 옮겼으므로 이 로그가 남았다는 것 자체가 "원 발화 신호 leg 이 0~1개"를
    함의하지만, `source_legs` 는 그 위에 "형제 몇 개가 합의에 참여했나"를 더해 이 상호작용이
    실제로 발동한 턴을 운영에서 식별할 수 있게 한다."""
    m = _FakeMapper(exact=set(), nearest={}, hits=_FRUIT_HITS)
    with caplog.at_level("INFO"):
        await m.run_full(
            [CategoryQuery(None, name) for name in ["바나나", "사과", "배", "오렌지"]],
            settings=_settings(expand_legs=8),
            sibling_expansion=True,
        )
    record = _record(caplog, "category_expansion_consensus")
    assert record.source_legs == 4


async def test_consensus_filter_disabled_keeps_legacy_noise() -> None:
    """[#428 미적용 대조] `sibling_expansion=False`(기본값)면 같은 입력에서 종전처럼 노이즈가
    섞인 8개가 그대로 나온다 — 이 대조가 있어야 위 회귀가 "합의 필터가 실제로 일했다"는 증거다."""
    m = _FakeMapper(exact=set(), nearest={}, hits=_FRUIT_HITS)
    out = await m.run_full(
        [CategoryQuery(None, name) for name in ["바나나", "사과", "배", "오렌지"]],
        settings=_settings(expand_legs=8),
    )
    canonicals = [c for c, _ in out.expansion_leaves]
    assert canonicals == [
        "과일 > 수입과일",
        "과일 > 국산과일",
        "여성가방 > 백팩",
        "커피/생수/음료 > 주스/과즙음료",
        "신생아의류 (0~24개월) > 배냇저고리",
        "과일 > 냉동/간편과일",
        "과일 > 과일선물세트",
        "여성가방 > 스포츠가방",
    ]


async def test_consensus_filter_skips_when_no_majority_mid() -> None:
    """[#428] 형제 전개가 애초에 이질적이면(대분류가 leg 마다 전부 다름) 필터가 적용되지 않고
    종전과 동일하다 — `max_support == 1` 이라 합의 신호 자체가 없다(예: "이사 갈 때 필요한
    것들" → 행거·커튼·이불). 기존 R5-1 형평 규약이 이 경우에도 깨지지 않는다는 고정이다."""
    hits = {
        "행거": [("수납정리용품 > 행거", 0.30), ("수납정리용품 > 선반", 0.31)],
        "커튼": [("커튼/블라인드 > 커튼", 0.30), ("커튼/블라인드 > 블라인드", 0.31)],
        "이불": [("침구 > 이불", 0.30), ("침구 > 베개", 0.31)],
    }
    queries = [CategoryQuery(None, name) for name in ["행거", "커튼", "이불"]]
    out_with = await _FakeMapper(exact=set(), nearest={}, hits=hits).run_full(
        queries, settings=_settings(expand_legs=6), sibling_expansion=True
    )
    out_without = await _FakeMapper(exact=set(), nearest={}, hits=hits).run_full(
        queries, settings=_settings(expand_legs=6)
    )
    assert out_with.expansion_leaves == out_without.expansion_leaves


# [#428 리뷰 1차 F-1 회귀] "집들이 선물" 전개(디퓨저·캔들·와인잔·식기 세트) 로컬 pg-catalog
# 실측(2026-08-07, embed_texts(RETRIEVAL_QUERY) → search_categories_pg(k=8), 사전 1,007행).
# 각 leg 의 top-1 대분류는 향수·조명·스포츠 잡화·주방잡화로 전부 다르지만, "주얼리"가 캔들·
# 와인잔·식기 세트 3개 leg 의 **꼬리 순위**에 걸쳐 등장한다 — top-k 전체로 지지를 셌던 초판은
# 이 "주얼리"를 승자로 잘못 뽑아 디퓨저(향수)·캔들(조명)·식기 세트(주방잡화)의 정답급 후보를
# 전부 버렸다(#428 리뷰 1차 F-1). 이 셀은 그 실패를 재현·고정한다.
_HOUSEWARMING_HITS = {
    "디퓨저": [
        ("향수 > 남녀공용향수", 0.3173),
        ("향수 > 드레스퍼퓸", 0.3207),
        ("향수 > 여성향수", 0.3246),
        ("향수 > 향수세트", 0.3302),
        ("향수 > 남성향수", 0.3349),
        ("조명 > 조명", 0.3365),
        ("바디케어 > 바디미스트", 0.3388),
        ("전기/산업자재 > 전기생활용품", 0.3400),
    ],
    "캔들": [
        ("조명 > 조명", 0.3188),
        ("바디케어 > 제모/왁싱", 0.3407),
        ("조명 > 전구", 0.3436),
        ("패션잡화 > 파티용 소품", 0.3477),
        ("화방용품 > 캔버스/판넬", 0.3478),
        ("주얼리 > 주얼리 소품", 0.3504),
        ("꽃/원예 > 꽃/식물", 0.3528),
        ("자동차용품 > 램프", 0.3537),
    ],
    "와인잔": [
        ("스포츠 잡화 > 스포츠 글라스", 0.3394),
        ("주얼리 > 주얼리 소품", 0.3402),
        ("패션잡화 > 파티용 소품", 0.3417),
        ("주방잡화 > 냄비/컵/수저받침", 0.3452),
        ("여행가방/소품 > 여행소품", 0.3467),
        ("이유용품 > 아동용컵", 0.3513),
        ("브랜드 잡화/소품 > 기타 액세서리", 0.3560),
        ("수입명품 > 럭셔리 라이프", 0.3611),
    ],
    "식기 세트": [
        ("주방잡화 > 일회용식기/도시락", 0.2869),
        ("주방잡화 > 냄비/컵/수저받침", 0.3160),
        ("조류용품 > 모이통/식기", 0.3243),
        ("주얼리 > 주얼리세트", 0.3338),
        ("스킨케어 > 스킨케어 세트", 0.3392),
        ("임부복/소품 > 잡화", 0.3433),
        ("주방용품 > 냄비", 0.3433),
        ("도서/음반 > 유아동 기획세트", 0.3450),
    ],
}


async def test_consensus_filter_skips_when_top1_mids_all_differ() -> None:
    """[#428 리뷰 1차 F-1 회귀·필수] top-1 대분류가 leg 마다 전부 달라 합의가 성립하지 않으면,
    꼬리 순위에 공통 대분류("주얼리")가 있어도 필터가 발동하지 않고 종전(top-k 전체 집계)이
    잘못 골랐던 결과와 달리 8개 후보가 그대로 보존된다.

    top-k 전체로 지지를 세면 `주얼리`(캔들·와인잔·식기 세트 3개 leg 의 꼬리)가 승자가 돼
    향수·조명·주방잡화라는 정답급 후보를 전부 버렸다 — 이것이 F-1 이 고친 결함이다. top-1 만
    세면 향수(디퓨저)·조명(캔들)·스포츠 잡화(와인잔)·주방잡화(식기 세트)로 4개 leg 의 최근접이
    전부 달라 `max_support == 1`이라 필터가 적용되지 않는다.
    """
    m = _FakeMapper(exact=set(), nearest={}, hits=_HOUSEWARMING_HITS)
    queries = [CategoryQuery(None, name) for name in ["디퓨저", "캔들", "와인잔", "식기 세트"]]
    out_with = await m.run_full(queries, settings=_settings(expand_legs=8), sibling_expansion=True)
    out_without = await _FakeMapper(exact=set(), nearest={}, hits=_HOUSEWARMING_HITS).run_full(
        queries, settings=_settings(expand_legs=8)
    )
    assert out_with.expansion_leaves == out_without.expansion_leaves
    canonicals = {c for c, _ in out_with.expansion_leaves}
    # 정답급 후보(향수·조명·주방잡화)가 살아 있어야 한다 — 잡동사니 대분류(주얼리)에 밀려
    # 사라지면 이 테스트가 재현하려는 결함이 되돌아온 것이다.
    assert any(c.startswith("향수") for c in canonicals)
    assert any(c.startswith("조명") for c in canonicals)
    assert any(c.startswith("주방잡화") for c in canonicals)


async def test_consensus_filter_single_leg_untouched() -> None:
    """[#428] leg 1개면 `sibling_expansion=True` 여도 종전과 완전히 동일 — 기여 leg 이 2개
    미만이면 합의 자체가 성립하지 않는다(§1.3-1)."""
    hits = [(f"카테고리{i} > 소분류{i}", 0.30 + i * 0.001) for i in range(8)]
    m = _FakeMapper(exact=set(), nearest={}, hits={"애매한 발화": hits})
    out = await m.run_full(
        [CategoryQuery(None, "애매한 발화")],
        settings=_settings(expand_legs=3),
        sibling_expansion=True,
    )
    assert out.expansion_leaves == [(c, "애매한 발화") for c, _ in hits[:3]]


async def test_consensus_filter_skipped_when_a_sibling_lacks_winning_mid(caplog) -> None:
    """[#428 리뷰 3차 R3-1] 승자 대분류(과일)가 형제 "다"의 후보 목록 어디에도 없으면 — "다"는
    잡화라는 정당하게 다른 상품군이지 합의에서 벗어난 노이즈가 아니다 — 필터를 통째로
    미적용하고(부분 적용 금지) 원본을 그대로 보존한다. 가드 발동은 `category_expansion_consensus`
    가 아니라 `category_expansion_consensus_skipped` 로 관측된다."""
    hits = {
        "가": [("과일 > 국산과일", 0.30)],
        "나": [("과일 > 수입과일", 0.30)],
        "다": [("잡화 > 소품", 0.30)],
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=hits)
    with caplog.at_level("INFO"):
        out = await m.run_full(
            [CategoryQuery(None, name) for name in ["가", "나", "다"]],
            settings=_settings(expand_legs=4),
            sibling_expansion=True,
        )
    record = _record(caplog, "category_expansion_consensus_skipped")
    assert record.reason == "leg_without_winning_mid"
    assert record.max_support == 2
    assert record.winning_mids == ["과일"]
    assert not [r for r in caplog.records if r.msg == "category_expansion_consensus"]
    canonicals = {c for c, _ in out.expansion_leaves}
    assert "잡화 > 소품" in canonicals
    assert "과일 > 국산과일" in canonicals
    assert "과일 > 수입과일" in canonicals


async def test_consensus_skipped_log_carries_source_legs(caplog) -> None:
    """[#428 리뷰 5차 R5-2] 가드 **스킵** 케이스에서도 `category_expansion_consensus_skipped`
    로그에 `source_legs` 가 실린다 — 적용·스킵 양쪽 다 관측 가능해야 이 상호작용의 발동 빈도를
    운영에서 온전히 잴 수 있다."""
    hits = {
        "가": [("과일 > 국산과일", 0.30)],
        "나": [("과일 > 수입과일", 0.30)],
        "다": [("잡화 > 소품", 0.30)],
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=hits)
    with caplog.at_level("INFO"):
        await m.run_full(
            [CategoryQuery(None, name) for name in ["가", "나", "다"]],
            settings=_settings(expand_legs=4),
            sibling_expansion=True,
        )
    record = _record(caplog, "category_expansion_consensus_skipped")
    assert record.source_legs == 3


# [#428 리뷰 3차 회귀] "신학기 준비물" 전개 중 거리컷에 드롭된 3형제. 로컬 pg-catalog 실측
# (2026-08-07, 사전 1,007행). 책가방·필통이 우연히 '여성가방'에서 겹치지만 물통에는 그 대분류
# 후보가 아예 없다 — 정당하게 다른 상품군이라는 뜻이므로 합의를 적용하면 안 된다.
_SCHOOL_HITS = {
    "책가방": [
        ("여성가방 > 백팩", 0.2648),
        ("여성가방 > 노트북가방", 0.2767),
        ("여성가방 > 스포츠가방", 0.2778),
        ("브랜드 여성가방 > 백팩", 0.2818),
        ("여행가방/소품 > 이민/유학용가방", 0.2829),
        ("남성가방 > 백팩", 0.2833),
        ("여행가방/소품 > 보조가방", 0.2874),
        ("브랜드 여성가방 > 노트북가방", 0.2920),
    ],
    "필통": [
        ("여성가방 > 파우치", 0.2926),
        ("문구/사무용품 > 문구용품", 0.2989),
        ("뷰티소품 > 화장품파우치", 0.3064),
        ("여행가방/소품 > 여행소품", 0.3073),
        ("수납가구 > 기타 수납소품", 0.3093),
        ("뷰티소품 > 메이크업정리함", 0.3096),
        ("여행가방/소품 > 여행파우치", 0.3128),
        ("수납가구 > 소품 수납정리함", 0.3138),
    ],
    "물통": [
        ("이유용품 > 아동용물병", 0.2819),
        ("커피/생수/음료 > 생수", 0.2830),
        ("조류용품 > 모이통/식기", 0.2890),
        ("물티슈 > 물티슈", 0.3096),
        ("물티슈 > 물티슈액세서리", 0.3155),
        ("뷰티소품 > 화장품용기", 0.3190),
        ("여행가방/소품 > 여행소품", 0.3258),
        ("이유용품 > 아동용컵", 0.3266),
    ],
}


async def test_consensus_filter_skipped_for_school_supplies_keeps_water_bottle(caplog) -> None:
    """[#428 리뷰 3차 R3-1 회귀·실측 기반] "신학기 준비물"(책가방·필통·물통) 전개 — 책가방·필통이
    우연히 "여성가방"에서 겹쳐(`max_support=2`) 물통이 통째로 드롭될 뻔했던 실제 재현 사례다.
    R3-1 가드가 "물통"의 후보 어디에도 "여성가방"이 없음을 감지해 필터를 통째로 미적용하고,
    `문구/사무용품 > 문구용품`(필통의 진짜 정답)·`이유용품 > 아동용물병`·`커피/생수/음료 > 생수`
    (물통의 정답급 후보)가 전부 살아남는다."""
    m = _FakeMapper(exact=set(), nearest={}, hits=_SCHOOL_HITS)
    with caplog.at_level("INFO"):
        out = await m.run_full(
            [CategoryQuery(None, name) for name in ["책가방", "필통", "물통"]],
            settings=_settings(expand_legs=8),
            sibling_expansion=True,
        )
    record = _record(caplog, "category_expansion_consensus_skipped")
    assert record.reason == "leg_without_winning_mid"
    assert record.max_support == 2
    assert record.winning_mids == ["여성가방"]
    canonicals = {c for c, _ in out.expansion_leaves}
    assert "문구/사무용품 > 문구용품" in canonicals
    assert "이유용품 > 아동용물병" in canonicals
    assert "커피/생수/음료 > 생수" in canonicals


# ── #428 리뷰 4차(Claude PR Review, PR #444) — 멀티 니즈(case=3) 전개가 합의 필터에 실제로
# 섞이는 상호작용. `case=3`("이어폰이랑 노트북 추천해줘")은 서로 다른 상품 2개 이상도 포함하고
# (decompose.py case 정의) `expand_needs` 는 발화 전체를 한 번에 전개하므로, 전개 산출에 서로
# 다른 니즈가 섞일 수 있다. 아래 두 테스트는 그때 합의 필터가 소수 니즈를 죽이지 않는다는 것을
# 실물 `map_categories` + 실물 `_consensus_filter` 조합(스텁 아님)으로 동률 승자·R3-1 가드
# 두 경로에 고정한다.


async def test_consensus_filter_multi_need_expansion_keeps_both_mids_on_tie(caplog) -> None:
    """[#428 리뷰 4차 R4-1] `case=3` 은 서로 다른 상품 2개 이상도 포함하고(`decompose.py` case
    정의) `expand_needs` 는 발화 전체를 한 번에 전개하므로, 전개 산출에 서로 다른 니즈가 섞일
    수 있다(#444 Claude 리뷰). 그때 합의 필터가 소수 니즈를 죽이지 않는다는 것을 동률 승자
    경로로 고정한다: 책가방·필통(니즈 A)과 사과·바나나(니즈 B)가 섞인 전개에서 각 니즈가 형제
    2개씩 대분류에 합의하면 `max_support` 가 동률이라 `여성가방`·`과일` 둘 다 승자가 되고, 네
    아이템 모두 후보를 유지한다 — 실물 `map_categories`+`_consensus_filter` 조합을 태워
    검증한다(종전 테스트는 이 상호작용을 스텁으로 가려 왔다)."""
    m = _FakeMapper(exact=set(), nearest={}, hits={**_SCHOOL_HITS, **_FRUIT_HITS})
    with caplog.at_level("INFO"):
        out = await m.run_full(
            [CategoryQuery(None, name) for name in ["책가방", "필통", "사과", "바나나"]],
            settings=_settings(expand_legs=8),
            sibling_expansion=True,
        )
    # 동률 승자가 **둘 다** 남아야 한다 — 승자를 1개로 좁히면(예: 사전순 첫 번째만) 소수 니즈가
    # 죽는다. queries 단언만으로는 가드 스킵(원본 보존)과 구분이 안 되므로 winning_mids 로
    # "필터가 실제로 적용됐고 두 대분류 다 승자였다"를 명시적으로 고정한다.
    record = _record(caplog, "category_expansion_consensus")
    assert record.max_support == 2
    assert record.winning_mids == ["과일", "여성가방"]
    queries = {q for _, q in out.expansion_leaves}
    assert queries == {"책가방", "필통", "사과", "바나나"}


async def test_consensus_filter_multi_need_expansion_guard_preserves_minority_need(
    caplog,
) -> None:
    """[#428 리뷰 4차 R4-1] `case=3` 은 서로 다른 상품 2개 이상도 포함하고(`decompose.py` case
    정의) `expand_needs` 는 발화 전체를 한 번에 전개하므로, 전개 산출에 서로 다른 니즈가 섞일
    수 있다(#444 Claude 리뷰). 그때 합의 필터가 소수 니즈를 죽이지 않는다는 것을 R3-1 가드
    경로로 고정한다: 디퓨저·캔들(니즈 A, 최근접이 향수·조명으로 서로 다름)과 사과·바나나
    (니즈 B, 과일로 합의)가 섞인 전개에서 `winning={과일}` 이지만 디퓨저·캔들에는 과일 후보가
    아예 없어 R3-1 가드가 발동하고 원본 8개가 그대로 보존된다 — 실물 `map_categories`+
    `_consensus_filter` 조합을 태워 검증한다."""
    m = _FakeMapper(exact=set(), nearest={}, hits={**_HOUSEWARMING_HITS, **_FRUIT_HITS})
    with caplog.at_level("INFO"):
        out = await m.run_full(
            [CategoryQuery(None, name) for name in ["디퓨저", "캔들", "사과", "바나나"]],
            settings=_settings(expand_legs=8),
            sibling_expansion=True,
        )
    record = _record(caplog, "category_expansion_consensus_skipped")
    assert record.reason == "leg_without_winning_mid"
    assert record.max_support == 2
    assert record.winning_mids == ["과일"]
    canonicals = {c for c, _ in out.expansion_leaves}
    assert "향수 > 남녀공용향수" in canonicals
    assert "조명 > 조명" in canonicals


async def test_consensus_skip_reasons_are_logged_and_only_disabled_is_silent(caplog) -> None:
    """[#428 리뷰 6차 R6-1/R6-3] 라운드 3까지는 단일 leg · `max_support<2` · `sibling_
    expansion=False` 셋 다 무기록이었다(#444 Claude 리뷰 5차 지적) — `_consensus_filter` 가
    미적용을 `None` 으로 냈고 호출부가 `is not None` 으로 게이트를 걸었기 때문이다. 이제
    앞 둘은 각각 `reason == "single_leg"`/`"no_consensus"` 로 `category_expansion_
    consensus_skipped` 에 **기록되고**, `sibling_expansion=False` 만 여전히 무기록이다 —
    이게 리뷰어가 요구한 "실제 발동 여부를 로그만으로 판별"이 성립하는 근거다."""
    hits_pair = {
        "가": [("A > a1", 0.30)],
        "나": [("B > b1", 0.30)],
    }
    single_hits = [(f"카테고리{i} > 소분류{i}", 0.30 + i * 0.001) for i in range(3)]

    with caplog.at_level("INFO"):
        await _FakeMapper(exact=set(), nearest={}, hits={"애매한 발화": single_hits}).run_full(
            [CategoryQuery(None, "애매한 발화")],
            settings=_settings(expand_legs=3),
            sibling_expansion=True,
        )
    single_leg_record = _record(caplog, "category_expansion_consensus_skipped")
    assert single_leg_record.reason == "single_leg"
    assert single_leg_record.source_legs == 1
    caplog.clear()

    with caplog.at_level("INFO"):
        await _FakeMapper(exact=set(), nearest={}, hits=hits_pair).run_full(
            [CategoryQuery(None, name) for name in ["가", "나"]],
            settings=_settings(expand_legs=4),
            sibling_expansion=True,
        )
    no_consensus_record = _record(caplog, "category_expansion_consensus_skipped")
    assert no_consensus_record.reason == "no_consensus"
    assert no_consensus_record.max_support == 1
    assert no_consensus_record.source_legs == 2
    caplog.clear()

    # `sibling_expansion=False` — 필터가 애초에 호출되지 않는다. 이게 이제 유일한 무기록 상태다.
    with caplog.at_level("INFO"):
        await _FakeMapper(exact=set(), nearest={}, hits=hits_pair).run_full(
            [CategoryQuery(None, name) for name in ["가", "나"]],
            settings=_settings(expand_legs=4),
        )
    assert not [
        r
        for r in caplog.records
        if r.msg in ("category_expansion_consensus", "category_expansion_consensus_skipped")
    ]


# ── #428 리뷰 2차(PR #444 Claude Review) — `category_expand_legs=0` IndexError 회귀 ──────
#
# `category_expand_legs` 는 `ge=0` 필드(`app/core/config.py:879`)라 0 은 합법값이다. 0이면
# `_collect_expansion_leaves` 의 `hits[: 0]` 슬라이스가 빈 리스트를 내고, 그 빈 리스트가
# `expansion_by_leg[i]` 에 그대로 담기면 `_consensus_filter` 의 `leaves[0]`(top-1) 인덱싱이
# IndexError 를 낸다. 더 심각한 건 원래 호출 위치가 조립 루프의 격리 try/except **밖**이라,
# 이 예외가 `map_categories` 전체를 던져 이미 채택된 canonical 까지 버렸다(리뷰 2차 R2-3).


async def test_expand_legs_zero_does_not_crash_and_yields_no_expansion_leaves() -> None:
    """[#428 리뷰 2차 R2-1/R2-2 회귀·필수] `category_expand_legs=0` + unresolved leg 2개 이상 +
    `sibling_expansion=True` 에서 예외 없이 정상 반환하고 `expansion_leaves == []` 다."""
    hits = {
        "김밥 재료": [("채소 > 파/마늘/양념채소", 0.30), ("수산 > 어묵/맛살", 0.31)],
        "떡볶이 재료": [("채소 > 파/마늘/양념채소", 0.29), ("냉장식품 > 밥류", 0.32)],
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=hits)
    out = await m.run_full(
        [CategoryQuery(None, "김밥 재료"), CategoryQuery(None, "떡볶이 재료")],
        settings=_settings(expand_legs=0, expand_enabled=True),
        sibling_expansion=True,
    )
    assert out.expansion_leaves == []
    assert out.unresolved == ["김밥 재료", "떡볶이 재료"]  # #217 전개 트리거는 그대로 살아있다


async def test_expand_legs_zero_suppresses_expansion_leaves_log(caplog) -> None:
    """[#428 리뷰 2차 R2-1 회귀] `category_expand_legs=0` 이면 `category_expansion_leaves`
    로그가 나오지 않는다 — 종전엔 빈 leg 도 담겨 `count: 0` 으로 무의미하게 찍혔다."""
    hits = {
        "김밥 재료": [("채소 > 파/마늘/양념채소", 0.30), ("수산 > 어묵/맛살", 0.31)],
        "떡볶이 재료": [("채소 > 파/마늘/양념채소", 0.29), ("냉장식품 > 밥류", 0.32)],
    }
    m = _FakeMapper(exact=set(), nearest={}, hits=hits)
    with caplog.at_level("INFO"):
        await m.run_full(
            [CategoryQuery(None, "김밥 재료"), CategoryQuery(None, "떡볶이 재료")],
            settings=_settings(expand_legs=0, expand_enabled=True),
            sibling_expansion=True,
        )
    assert not [r for r in caplog.records if r.msg == "category_expansion_leaves"]


async def test_consensus_filter_failure_preserves_accepted_legs(monkeypatch, caplog) -> None:
    """[#428 리뷰 2차 R2-3 회귀·핵심] `_consensus_filter` 가 예외를 던져도 `map_categories` 는
    던지지 않는다 — ① 원본 `expansion_leaves` 를 그대로 내고 ② `category_expansion_consensus_
    failed` 로그를 남기며 ③ **이미 채택된 canonical(`legs`)이 보존**된다.

    ③ 이 핵심이다 — 이 호출이 조립 루프의 try/except 밖에 있어 예외가 `map_categories` 전체를
    던지면 `_map_or_empty`(graph.py)가 빈 `CategoryMapping` 으로 degrade 해, 이미 DB 검증된
    exact 매치까지 버린다(Claude PR Review, PR #444). 거리컷 통과 leg 하나(exact match)와
    드롭된 leg 둘을 섞어야 이 보존을 실제로 잰다.
    """
    import app.agents.buyer.recommendation.category_mapping as cm

    def _raise(_expansion_by_leg):
        raise RuntimeError("consensus filter boom")

    monkeypatch.setattr(cm, "_consensus_filter", _raise)

    hits = {
        "김밥 재료": [("채소 > 파/마늘/양념채소", 0.30), ("수산 > 어묵/맛살", 0.31)],
        "떡볶이 재료": [("채소 > 파/마늘/양념채소", 0.29), ("냉장식품 > 밥류", 0.32)],
    }
    m = _FakeMapper(exact={"PC부품 > CPU"}, nearest={}, hits=hits)
    with caplog.at_level("WARNING"):
        out = await m.run_full(
            [
                CategoryQuery("PC부품 > CPU", "cpu"),  # exact match — 거리컷 무관하게 채택
                CategoryQuery(None, "김밥 재료"),  # 거리컷 드롭 → expansion_leaves 후보
                CategoryQuery(None, "떡볶이 재료"),  # 거리컷 드롭 → expansion_leaves 후보
            ],
            settings=_settings(expand_legs=8),
            sibling_expansion=True,
        )
    assert out.legs == [("PC부품 > CPU", "cpu")]  # ③ 이미 채택된 canonical 이 보존된다
    # ① 필터 미적용 원본(인터리브·dedup_truncate 는 정상 통과) — 노이즈 걸러내기 전 8종 이하 원본
    assert out.expansion_leaves  # 원본 후보가 그대로 살아있다(빈 리스트로 날아가지 않음)
    record = _record(caplog, "category_expansion_consensus_failed")
    assert record.reason == "consensus filter boom"
    assert record.error_type == "RuntimeError"
