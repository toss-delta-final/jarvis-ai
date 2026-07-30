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


def _settings(*, top_k: int = 5, fanout_max: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_db_url="postgresql://x",
        category_top_k=top_k,
        category_fanout_max=fanout_max,
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

    async def run(self, queries, utterance="발화", settings=None):
        return await map_categories(
            category_queries=queries,
            utterance=utterance,
            settings=settings or _settings(),
            embed=self.embed,
            search_top_k=self.search,
            exact_lookup=self.exact_lookup,
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


async def test_raw_anchor_wins_when_closer_than_query() -> None:
    """raw 가 더 가까우면 raw 쪽 canonical 을 채택한다(§4.3 — query 우선이 아니라 '가까운 쪽').

    raw 를 무조건 버리는 것도 정답이 아니다. LLM 이 정확한 라벨을 낸 경우("전자제품>오디오>이어폰"
    → 음향가전 > 이어폰 0.1329)는 발화의 수식어 낀 query 보다 더 가까울 수 있다.
    """
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "전자제품>오디오>이어폰": [
                ("음향가전 > 이어폰", 0.1329),
                ("음향가전 > 헤드폰", 0.2095),
            ],
            "갓성비 무선이어폰": [
                ("음향가전 > 블루투스 이어폰", 0.2566),
                ("음향가전 > 이어폰", 0.2569),
            ],
        },
    )
    out = await m.run([CategoryQuery("전자제품>오디오>이어폰", "갓성비 무선이어폰")])
    assert out == [("음향가전 > 이어폰", "갓성비 무선이어폰")]


async def test_tie_prefers_query_anchor(caplog) -> None:
    """거리가 동일하면 발화 유래 query 를 택한다 — LLM 창작 라벨보다 발화가 신뢰도 높다(§4.3)."""
    m = _FakeMapper(
        exact=set(),
        nearest={},
        hits={
            "선물용품": [("취미 > 수집용품", 0.2074)],
            "홍삼": [("건강식품 > 홍삼", 0.2074)],  # 같은 거리
        },
    )
    with caplog.at_level("INFO"):
        out = await m.run([CategoryQuery("선물용품", "홍삼")])
    assert out == [("건강식품 > 홍삼", "홍삼")]
    assert _record(caplog, "category_repaired").anchor_kind == "query"


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
