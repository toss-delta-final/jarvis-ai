"""카탈로그 검색 서비스 — SearchBackend 심(seam) (확정 2026-07-15, 이슈 #2 배선).

MVP: 질의 시점 Spring 위임(GET /internal/products/search, I-1, §4.6). decompose 필터를 Spring 에
넘기고 후보를 받는다. **BE I-1 은 excludeProductIds·ratingMin 파라미터가 없으므로**
(v0.15.5, C-15 해소) dedup 제외·평점 하한은 **응답 수신 후 AI 사후필터**로 적용한다.
정렬은 rerank(LLM) 소관이라 sort 필드를 두지 않는다(#100 P2).

[결정 2026-07-20, api-spec §4.8 말미] 임베딩 검색을 두 방식으로 구현해 골든셋 확정:
  방식2 EmbeddingRerankBackend — Spring 후보를 AI 임베딩으로 재정렬(라이브, BE 계약 변경 없음).
  방식1 VectorSearchBackend    — #32에서 미채택·C-17 기각, 오프라인 골든셋 비교 전용으로 존치.
AI 생성물(임베딩)은 I-17 배치(§4.8, artifact_store)가 갱신하며 상품 원본 컬럼 미러는 영구 미채택.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
from time import monotonic
from typing import Protocol

from app.agents.buyer.cart.options import narrow_options
from app.core.config import get_settings
from app.core.tracing import current_request_trace
from app.pipelines import embedding as _embedding
from app.pipelines.artifact_store import ArtifactStore, get_catalog_store
from app.schemas.spring import CartOption, ProductSearchFilters, ProductSearchResult
from app.services import spring_client

_log = logging.getLogger(__name__)


async def _search_products_with_observation(
    filters: ProductSearchFilters,
) -> ProductSearchResult:
    """Spring 검색 성공 결과를 요청 관측에만 누적하고, 관측 실패는 검색 결과에 영향 주지 않는다."""
    started = monotonic()
    result = await spring_client.search_products(filters)
    elapsed_ms = round((monotonic() - started) * 1_000)
    try:
        trace = current_request_trace()
        if trace is not None:
            trace.record_search_result(len(result.products), result.total_count, elapsed_ms)
    except Exception:
        _log.warning("search result observation failed code=SEARCH_RESULT_OBSERVATION_FAILED")
    return result


class SearchBackend(Protocol):
    """검색 백엔드 계약. 기본=Spring 위임, 임베딩 방식1/2로 교체 가능한 심."""

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        """필터로 상품을 검색해 결과를 반환한다."""
        ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """코사인 유사도. 빈 벡터/0벡터/차원 불일치는 -1.0(최하위=제외)로 처리한다.

    차원이 다르면(모델 교체·마이그레이션) zip 절단으로 잘못된 값이 나오므로 조용히 계산하지 않고 제외한다.

    [#148] 홈 추천 랭킹(I-22, `services/home_recommendation.py`)도 같은 척도를 써야 채팅·홈의
    유사도 정의가 갈라지지 않으므로 공개 이름을 둔다. `_cosine` 은 기존 호출부 호환 별칭이다.
    """
    if not a or not b or len(a) != len(b):
        return -1.0
    num = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return -1.0
    return num / (na * nb)


_cosine = cosine_similarity  # 기존 내부 호출부 호환 별칭


def vector_rank(query_vec: list[float], store: ArtifactStore, *, k: int) -> list[int]:
    """query 임베딩과 저장 임베딩의 코사인으로 상위 k productId 를 반환한다 (방식1 코어, 오프라인 안전).

    #32에서 방식1 라이브 채택과 C-17 hydrate를 기각했다. 오프라인 골든셋 비교는 이 랭킹만 사용한다.
    """
    scored = [
        (_cosine(query_vec, art.embedding), art.product_id) for art in store.all() if art.embedding
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [pid for _, pid in scored[:k]]


class SpringSearchBackend:
    """MVP 기본 백엔드 — Spring GET /internal/products/search 위임 (I-1)."""

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        """Spring 위임 검색. 실패 시 spring_client 가 SpringUnavailableError 를 던진다."""
        return await _search_products_with_observation(filters)


class EmbeddingRerankBackend:
    """방식2 — Spring 검색(I-1) 후보를 AI 임베딩으로 시맨틱 재정렬. 라이브(BE 계약 변경 없음).

    후보 id 집합을 artifact_store.top_k_by_vector 로 넘겨 DB 코사인 거리 순으로 재정렬한다.
    DB에 행이 없거나 순위 상한 밖인 후보는 Spring 상대순서를 보존해 꼬리에 둔다. keyword 없거나
    후보 없으면 Spring 순서 그대로 반환.

    벡터 순위화와 임베딩 호출 둘 다 블로킹 I/O 다 — store 는 pg-catalog 대상이면 psycopg
    동기 드라이버(이슈 #31), 임베딩은 Google API 동기 HTTP 호출(embedding.py). 둘 다
    asyncio.to_thread 로 별도 스레드에 넘겨 FastAPI 이벤트루프를 막지 않는다(PR #42 리뷰).
    #101 규약대로 여기서 후보를 절단하지 않고, graph가 dedup·소모품 억제 후 최종 상한을 적용한다.
    """

    def __init__(self, *, store: ArtifactStore | None = None, embed=None) -> None:
        self._store = store or get_catalog_store()
        # 미주입 기본값은 질의(query) 임베딩 — 비대칭 임베딩 바인딩(이슈 #65)
        self._embed = embed or functools.partial(
            _embedding.embed_texts, task_type=get_settings().embedding_task_query
        )

    def _rerank(self, products: list, qvec: list[float]) -> list:
        ids = [product.product_id for product in products]
        k = min(len(ids), get_settings().embedding_rerank_vector_k_max)
        ranked_ids = self._store.top_k_by_vector(qvec, k=k, include=set(ids))

        products_by_id: dict[int, list] = {}
        for product in products:
            products_by_id.setdefault(product.product_id, []).append(product)

        ranked: list = []
        ranked_id_set: set[int] = set()
        for product_id in ranked_ids:
            if product_id in ranked_id_set:
                continue
            ranked.extend(products_by_id.get(product_id, ()))
            ranked_id_set.add(product_id)
        # DB 미존재·빈 임베딩·k 상한 밖 후보는 유실하지 않고 원래 Spring 상대순서로 꼬리에 둔다.
        ranked.extend(product for product in products if product.product_id not in ranked_id_set)
        return ranked

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        result = await _search_products_with_observation(filters)
        # 의미검색 입력은 semantic_query(#101) — 없으면 상품명 keyword 로 폴백. 둘 다 없거나 후보가
        # 없으면 Spring 순서 그대로(재정렬 skip). keyword 유무와 무관하게 semantic 이 있으면 재정렬.
        # 최종 소비 지점 방어 — semantic_query/keyword 가 공백-only('  ')여도 truthy 라 무의미한
        # 텍스트로 임베딩 API 호출·정렬하게 되므로 strip 후 빈 값이면 Spring 순서 그대로(PR#166 리뷰).
        query_text = (filters.semantic_query or filters.keyword or "").strip()
        if not query_text or not result.products:
            return result
        try:
            # 재정렬 단계(query 임베딩·후보 embedding batch 조회)만 별도 격리 — Spring I-1 자체 실패는
            # 위에서 이미 전파됐다(SEARCH_FAILED). 여기 실패(임베딩 API·pgvector 장애)는 추천 전체를
            # 죽이지 않고 Spring 순서로 degrade 한다(#101 #7). CancelledError(BaseException)는 전파.
            embedded = await asyncio.to_thread(self._embed, [query_text])
            qvec = embedded[0]
            reranked = await asyncio.to_thread(self._rerank, result.products, qvec)
        except Exception as exc:  # noqa: BLE001 - 재정렬 degrade(Spring 순서 보존)
            _log.warning("임베딩 재정렬 실패 → Spring 순서 degrade(SEARCH_FAILED 아님) — %s", exc)
            return result
        return ProductSearchResult(products=reranked, total_count=result.total_count)


class VectorSearchBackend:
    """방식1 — AI 벡터검색으로 상위 N productId 확보 → Spring hydrate(필터·가용성·상세).

    #32 실측으로 방식1을 미채택하고 C-17을 기각했다. 오프라인 골든셋 비교 전용으로 존치하며
    미주입(hydrate=None) 시 SpringUnavailableError 로 미채택 신호를 낸다. 역사적 hydrate seam의
    계약은 (ids, filters): Spring이 가격·카테고리·브랜드를 적용하고 품절·비활성을 제거한다.
    hydrate 후 후보가 줄 수 있어 벡터 후보는 limit 의 over_fetch 배로 여유 조회한다
    (config.catalog_vector_overfetch). 오프라인 비교는 vector_rank(랭킹)만 쓰고 hydrate 없이 한다.

    vector_rank 의 store.all() 은 pg-catalog 대상이면 카탈로그 전체를 블로킹으로 읽어오는
    비용이 크고, 임베딩 호출도 Google API 동기 HTTP 라 블로킹이다 — 둘 다 asyncio.to_thread 로
    이벤트루프 차단은 막았지만(이슈 #31, PR #42 리뷰), "SQL 에서 ORDER BY embedding <-> %s
    LIMIT k 로 직접 top-k 만 조회"하는 근본 최적화는 아니다. 향후 방식1을 별도 결정으로 다시
    채택할 때 함께 재설계할 과제로 남겨둔다.
    """

    def __init__(
        self,
        *,
        store: ArtifactStore | None = None,
        embed=None,
        hydrate=None,
        over_fetch: int | None = None,
    ) -> None:
        self._store = store or get_catalog_store()
        # 미주입 기본값은 질의(query) 임베딩 — 비대칭 임베딩 바인딩(이슈 #65)
        self._embed = embed or functools.partial(
            _embedding.embed_texts, task_type=get_settings().embedding_task_query
        )
        self._hydrate = (
            hydrate  # Callable[[list[int], ProductSearchFilters], Awaitable[...]] | None
        )
        self._over_fetch = (
            over_fetch if over_fetch is not None else get_settings().catalog_vector_overfetch
        )

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        embedded = await asyncio.to_thread(self._embed, [filters.keyword or ""])
        qvec = embedded[0]
        k = max(
            filters.limit, filters.limit * self._over_fetch
        )  # hydrate 필터/품절 제거 대비 여유조회
        ids = await asyncio.to_thread(vector_rank, qvec, self._store, k=k)
        if self._hydrate is None:
            raise spring_client.SpringUnavailableError(
                "VectorSearchBackend(방식1) 미채택(#32) — 오프라인 비교 전용"
            )
        return await self._hydrate(ids, filters)


# hot path 기본 백엔드 — config search_backend 로 결정(#101). 방식2(embedding_rerank)가 MVP 기본.
# None = 아직 미해결(lazy) — import 시점에 EmbeddingRerankBackend 를 만들면 get_catalog_store() 가
# pg 풀을 즉시 열어 단위테스트를 깨므로, search_catalog 가 첫 사용 시 config 로 생성한다.
# 테스트(conftest.buyer_fakes)는 이 모듈 attr 를 FakeBackend 로 override 한다 — 그 값이 우선한다.
_BACKENDS: dict[str, type] = {
    "spring": SpringSearchBackend,
    "embedding_rerank": EmbeddingRerankBackend,
    "vector": VectorSearchBackend,
}
default_backend: SearchBackend | None = None


def _make_default_backend() -> SearchBackend:
    """config search_backend 로 hot path 기본 백엔드를 생성한다(#101). vector(방식1)는 #32에서
    미채택됐고 hydrate 미주입이라 search() 시 SpringUnavailableError 로 그 상태를 알린다."""
    return _BACKENDS[get_settings().search_backend]()


def _norm_attr(value: object) -> str:
    """속성 값 관대 비교용 정규화 — 문자열화 + 양끝 공백 제거 + casefold(대소문자 무시)."""
    return str(value).strip().casefold()


def _attr_value_matches(want: str, have: object) -> bool:
    """속성 값 비교(PR② PR#169 리뷰) — 숫자 조건은 완전 일치, 문자열은 관대 부분매칭.

    부분매칭만 쓰면 숫자·짧은 값 축(사이즈·용량·무게)에서 "1" 이 "100"·"21" 을 통과시켜 하드필터
    취지가 깨진다. 조건값이 순수 숫자면 완전 일치를, 아니면(자연어 값) 부분포함을 쓴다.
    """
    nw, nh = _norm_attr(want), _norm_attr(have)
    if nw.isdigit():
        return nw == nh
    return nw in nh


def _matches_attr_conditions(product, conditions: dict[str, str]) -> bool:
    """SpringProduct.attributes 가 명시 속성조건을 모두 만족하는지(하드 AND) — _attr_value_matches(PR②).

    축이 상품에 없으면 '반증 아님'으로 보존한다(#100 P0 rating 정책과 정합 — 데이터 부재 ≠ 불일치).
    축이 있는데 값이 조건을 만족하지 않으면(문자열 부분매칭·숫자 완전일치, PR#169) 반증 → 탈락.
    bool/숫자 값(dict[str, object])은 문자열화해 비교한다(예: 방수=true).
    """
    attrs = product.attributes or {}
    for axis, want in conditions.items():
        have = attrs.get(axis)
        if have is None:
            continue  # 축 부재 → 보존(반증 아님)
        if not _attr_value_matches(want, have):
            return False
    return True


def _apply_attr_conditions(products: list, conditions: dict[str, str]) -> list:
    """명시 속성 하드필터 + 0건 축별 완화(PR②).

    모든 축 매칭이 0건이면 마지막 축부터 하나씩 빼며 재시도해 과다제외를 막는다 — 남은 축이라도
    만족하는 상품을 살린다. 완화칩 emit·축 중요도 기반 완화 순서는 #113 소관(여기선 과다제외만 방지).
    전부 완화해도 0이면 속성 필터를 미적용(원본 반환)해 zero-result 를 강제하지 않는다.
    """
    axes = list(conditions.items())
    while axes:
        matched = [p for p in products if _matches_attr_conditions(p, dict(axes))]
        if matched:
            return matched
        axes = axes[:-1]  # 마지막 축 완화 후 재시도
    return products


def apply_ai_side_filters(products: list, filters: ProductSearchFilters) -> list:
    """rating_min 사후필터 + attr_conditions 하드필터 — Spring payload 축이 아닌 AI 사후필터.

    [#393 C] `search_catalog`(정상 검색 경로)와 인기 상품 폴백 경로(`recommendation/graph.py`
    `_run_candidate_source`, 매핑 드롭·무필터 우회 시 인기 상품으로 후보를 대체하는 자리) 가
    **같은 함수를 공유**한다 — 복제하면 "같은 판정을 두 곳에 둔다"는 규약 위반이다(#336 이 거부한
    자리). 인기 후보로 대체된 턴은 조건 칩에 "평점 4.0 이상"이 떠 있는데 후보는 그 조건을 안
    지키는 표시-실제 불일치가 생길 수 있어(rating_min·attr_conditions 는 payload 축이 아니라
    Spring 이 걸러주지 않는다), 인기 후보에도 이 사후필터를 그대로 적용해야 정직성이 유지된다.
    `exclude_product_ids`(dedup)는 그래프 하류가 담당하므로 여기 넣지 않는다.
    """
    if filters.rating_min is not None:
        threshold = filters.rating_min
        # '반증된 것만' 제거(#100 P0 / #171): 실제 리뷰가 있는데 하한 미달인 상품만 탈락시킨다.
        # 데이터 부재는 보존 — ① rating=None(무평점) ② review_count==0(리뷰가 아예 없어 나온
        # rating=0 은 저평점이 반증된 게 아니라 데이터 부재)은 rerank 가 판단하도록 남긴다.
        # review_count 가 None(BE 미전송)이면 rating 이 지배하는 구 동작으로 폴백한다.
        products = [
            p for p in products if p.rating is None or p.review_count == 0 or p.rating >= threshold
        ]

    # 명시 속성 하드필터(PR②) — SpringProduct.attributes 관대 매칭, 축 부재는 보존(#100 P0), 0건이면
    # 축별 완화. 추측 선호(소프트)는 여기서 안 거르고 rerank(원문+attributes)에 맡긴다.
    if filters.attr_conditions:
        products = _apply_attr_conditions(products, filters.attr_conditions)
    return products


def _attribute_color_values(product) -> list[str]:
    """`SpringProduct.attributes["색상"]` 값을 리스트로 정규화한다 — 문자열 하나거나 배열일 수
    있다(D7 자유 텍스트). `evals/option_color/harness.py::_parse_attribute_colors` 와 같은 규약
    (raw TSV JSON 텍스트가 아니라 이미 파싱된 dict 를 다루므로 별도 함수다 — 공유할 대상이 없다)."""
    if not product.attributes:
        return []
    value = product.attributes.get("색상")
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return []


def _is_color_unbuyable(
    product, color: str, color_synonyms: dict[str, list[str]], settings
) -> bool:
    """판정식 A~D(이슈 #454 Phase 2, `docs/specs/MEASURE-OPTION-COLOR-454.md` §"판정식") — 넷을
    전부 만족해야 "이 상품에서 이 색 옵션을 고를 수 없다"다. A(색상 조건 있음)는 호출부가 이미
    보장한다.

    D 판정은 `app.agents.buyer.cart.options.narrow_options` 의 R2(`by_condition`,
    `color_synonyms` 확장)를 **그대로 호출**한다 — #454 되물음 좁히기와 같은 함수라 판정
    로직을 두 곳에 두지 않는다(`evals/option_color` 하네스도 같은 함수를 쓴다).
    """
    if not product.options:
        return False  # 옵션 자체가 없는 단일 SKU — "그 옵션에 색이 없다"는 질문이 성립 안 함
    attribute_colors = _attribute_color_values(product)
    if len(attribute_colors) < 2:
        return False  # B 거짓 — 단일색/색상 축 없음은 정상(사이즈만 고르면 된다)
    if product.option_count is None or product.option_count != len(product.options):
        return False  # C 거짓(또는 미상) — 20개 절단이면 안 보이는 옵션에 그 색이 있을 수 있다
    options = [CartOption(option_id=i, name=n) for i, n in enumerate(product.options)]
    narrowing = narrow_options(
        options,
        message="",
        terms=(color,),
        min_term_len=settings.cart_option_narrow_min_term_len,
        match_suffixes=settings.cart_option_match_suffixes,
        color_synonyms=color_synonyms,
    )
    # D — 승인 동의어 확장 어느 것도 옵션 이름에 안 나타남("0건 매칭"만 참, 전건 일치는 매칭).
    return narrowing.by_condition == () and not narrowing.condition_matched_all


async def _filter_unbuyable_color_options(products: list, filters: ProductSearchFilters) -> list:
    """검색 사후필터 — 옵션에 그 색이 없는 후보를 뺀다(이슈 #454 Phase 2, api-spec §4.6 [].options
    소비 확대).

    `rating_min` 사후필터(위 `apply_ai_side_filters`)와 같은 결 — **반증된 것만** 제거한다.
    판정식 A~D 를 모두 만족하는(=옵션 목록 어디에도 그 색이 없다고 확신할 수 있는) 후보만 뺀다.
    B(색상 축 없음/단일)·C(20개 절단)는 반증이 아니라 "모른다"이므로 보존한다 — `rating=None`을
    저평점으로 단정하지 않는 것과 같은 철학.

    [#393 C 와 다른 자리] `apply_ai_side_filters` 는 인기 상품 폴백 경로와 공유하는 동기 함수인데,
    이 필터는 색상 동의어 사전 조회(비동기 I/O, TTL 캐시 히트가 아니면 DB 왕복)가 필요해 그
    함수에 넣지 않았다 — `search_catalog`(정상 검색 경로)에만 적용하고 인기 상품 폴백에는
    적용하지 않는다(#454 는 색상 조건이 있는 검색 턴을 다루는 이슈이고, 폴백은 범위 밖).

    사전 적재 실패·설정 off·색상 조건 없음이면 오늘 동작(무필터)으로 degrade한다. **0건 가드** —
    제외 후 후보가 0건이면 제외를 통째로 취소하고 원래 목록을 돌려준다(SKU 코드가 실제로는
    색상 코드일 수 있어 AI 는 판별 못 한다 — 하방을 유계로 만든다).
    """
    settings = get_settings()
    if not settings.search_color_option_postfilter_enabled:
        return products
    if not filters.color or not filters.color.strip():
        return products
    try:
        color_synonyms = await spring_client._load_color_synonym_map(settings)
    except Exception:
        _log.warning("색상 옵션 사후필터 사전 적재 실패 — 오늘 동작으로 degrade", exc_info=True)
        return products
    if color_synonyms is None:
        return products

    color = filters.color
    filtered = [p for p in products if not _is_color_unbuyable(p, color, color_synonyms, settings)]
    if not filtered:
        if trace := current_request_trace():
            trace.mark_degraded("color_option_postfilter_all_excluded")
        return products
    return filtered


async def search_catalog(
    filters: ProductSearchFilters,
    exclude_product_ids: list[int] | None = None,
    backend: SearchBackend | None = None,
) -> ProductSearchResult:
    """활성 백엔드로 카탈로그를 검색하고 AI 사후필터(dedup 제외·평점 하한)를 적용한다.

    BE I-1 에 dedup·평점 파라미터가 없어(C-15), Spring 검색은 keyword/category/price/brand 만
    보내고 exclude_product_ids(최근 구매 dedup, §4.7 결정 14-F)·rating_min 은 여기서 사후 제외한다.
    rating_min 사후필터는 '반증된 것만' 제거한다 — 리뷰가 있고 미달인 상품만 탈락, rating=None
    신상품과 review_count==0(리뷰 없어 rating=0) 상품은 데이터 부재로 보존(#100 P0 / #171).
    정렬은 rerank(LLM) 소관이라 별도 sort 필드가 없다(#100 P2) — 여기서는 검색순서를 보존한다.
    [2026-07-23, BE 합의] size 제거로 Spring 이 전량 반환한다(api-spec §4.6).
    [#101] **여기서 top-K 절단하지 않는다** — 재정렬·사후필터(dedup 제외·평점 하한)만 하고 전량을
    반환한다. 최종 rerank 입력 상한(embedding_rerank_limit) 절단은 graph 가 최근구매 dedup·소모품
    억제(stream_recommendation) **이후**에 적용한다. 이전엔 여기서 filters.limit 로 dedup 이전에
    절단해, dedup 대상이 상위에 몰리면 rerank 후보가 상한 미만이 되는 recall 손실이 있었다 — 절단을
    dedup 이후로 옮겨 근본 해소한다(방식1 VectorSearchBackend 는 자체 경로에서 filters.limit 사용).
    backend 미지정 시: 테스트 override(default_backend) 우선, 없으면 config 로 생성(#101).
    """
    # 우선순위: 명시 주입 backend → 테스트 override default_backend → config 기반 생성(prod hot path).
    used = backend or default_backend or _make_default_backend()
    result = await used.search(filters)
    products = result.products

    if exclude_product_ids:
        excluded = set(exclude_product_ids)
        products = [p for p in products if p.product_id not in excluded]

    # rating_min 사후필터 + attr_conditions 하드필터 — 인기 상품 폴백 경로와 공유(#393 C).
    products = apply_ai_side_filters(products, filters)

    # 색상 옵션 사후필터(이슈 #454 Phase 2) — 인기 상품 폴백과 공유하지 않는다(위 함수 docstring
    # "#393 C 와 다른 자리" 참조). rating_min/attr_conditions 뒤에 둬 그 필터가 이미 줄인 목록만
    # 판정한다(비용 절감 — 판정 자체는 순서 무관하게 같은 결과).
    products = await _filter_unbuyable_color_options(products, filters)

    # total_count = 사후필터 통과 매칭 수(전량). top-K 절단은 graph dedup 이후로 이동(#101).
    return ProductSearchResult(products=products, total_count=len(products))
