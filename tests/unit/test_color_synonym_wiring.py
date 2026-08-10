"""I-1 색상 확장 플래그·실패 degrade 배선 테스트 (이슈 #258)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters, SpringProduct
from app.services import spring_client as sc
from app.services.search_service import apply_ai_side_filters


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"success": True, "data": []}


class _Client:
    def __init__(self, seen):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path, params=None):
        self.seen.append(params)
        return _Response()


def test_search_query_params_default_color_path_is_unchanged() -> None:
    filters = ProductSearchFilters(keyword="원피스", color="남색")
    assert sc._search_query_params(filters) == {"keyword": "원피스", "color": "남색"}
    assert sc._search_query_params(filters, color_values=["네이비", "남색"])["color"] == [
        "네이비",
        "남색",
    ]


async def test_expansion_flag_off_never_loads_db(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"color_synonym_expansion_enabled": False})
    seen = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    from app.pipelines import color_synonyms

    monkeypatch.setattr(
        color_synonyms,
        "get_synonym_map",
        lambda *args, **kwargs: pytest.fail("flag off must not touch synonym DB"),
    )
    await sc.search_products(ProductSearchFilters(color="남색"))
    assert seen == [{"color": "남색"}]


async def test_expansion_failure_degrades_to_single_original_color(monkeypatch, caplog) -> None:
    settings = get_settings().model_copy(update={"color_synonym_expansion_enabled": True})
    seen = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    from app.pipelines import color_synonyms

    def fail(*args, **kwargs):
        raise RuntimeError("catalog offline")

    monkeypatch.setattr(color_synonyms, "get_synonym_map", fail)
    with caplog.at_level("WARNING"):
        await sc.search_products(ProductSearchFilters(color="남색"))
    assert seen == [{"color": "남색"}]
    assert "색상 동의어" in caplog.text


async def test_expansion_timeout_degrades_to_single_original_color(monkeypatch, caplog) -> None:
    settings = get_settings().model_copy(
        update={
            "color_synonym_expansion_enabled": True,
            "color_synonym_query_timeout_s": 0.001,
        }
    )
    seen = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    # 멈춰 세울 대상은 **색상 맵 조회 하나**다. 종전엔 `asyncio.to_thread` 를 모듈 전역으로
    # 스텁했는데, 검색 경로도 `to_thread` 를 쓰게 되면서(#132 파싱 이관) 그 스텁이 검색까지
    # 붙잡아 이 테스트가 2초를 쟀다. 진짜 느린 것은 DB 조회이므로 거기만 막는다 —
    # `_load_color_synonym_map` 의 shield·백그라운드 회수 경로도 실제 스레드로 함께 검증된다.
    from app.pipelines import color_synonyms

    def never_finishes(*args, **kwargs):
        time.sleep(0.2)  # wait_for(0.001s) 를 확실히 넘기는 동기 블로킹

    monkeypatch.setattr(color_synonyms, "get_synonym_map", never_finishes)
    started = asyncio.get_running_loop().time()
    with caplog.at_level("WARNING"):
        await sc.search_products(ProductSearchFilters(color="남색"))
    elapsed = asyncio.get_running_loop().time() - started
    assert seen == [{"color": "남색"}]
    assert "색상 동의어" in caplog.text
    assert elapsed < 0.1


async def test_expansion_saturation_degrades_immediately_then_recovers(monkeypatch) -> None:
    import threading

    first_settings = get_settings().model_copy(
        update={
            "color_synonym_expansion_enabled": True,
            "color_synonym_batch_harvest_enabled": True,
            "color_synonym_pool_max_size": 2,
            "color_synonym_harvest_max_concurrency": 1,
            "color_synonym_query_timeout_s": 0.01,
        }
    )
    later_settings = first_settings.model_copy(update={"color_synonym_query_timeout_s": 0.5})
    current_settings = [first_settings]
    seen = []
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    monkeypatch.setattr(sc, "get_settings", lambda: current_settings[0])
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))
    monkeypatch.setattr(sc, "_color_synonym_limiters", {}, raising=False)
    monkeypatch.setattr(sc, "_background_synonym_tasks", set(), raising=False)

    from app.pipelines import color_synonyms

    def load(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            release.wait(timeout=1.0)
            finished.set()
        return {"남색": ["네이비", "남색"]}

    monkeypatch.setattr(color_synonyms, "get_synonym_map", load)
    try:
        await sc.search_products(ProductSearchFilters(color="남색"))
        assert started.is_set()
        assert len(sc._background_synonym_tasks) == 1

        current_settings[0] = later_settings
        await asyncio.wait_for(
            sc.search_products(ProductSearchFilters(color="남색")),
            timeout=0.1,
        )
        assert calls == 1
        assert seen == [{"color": "남색"}, {"color": "남색"}]

        release.set()
        for _ in range(100):
            if finished.is_set():
                break
            await asyncio.sleep(0.001)
        assert finished.is_set()
        for _ in range(100):
            if not sc._background_synonym_tasks:
                break
            await asyncio.sleep(0.001)
        assert not sc._background_synonym_tasks

        await sc.search_products(ProductSearchFilters(color="남색"))
        assert calls == 2
        assert seen[-1] == {"color": ["네이비", "남색"]}
    finally:
        release.set()
        await asyncio.sleep(0.01)


def test_runtime_lookup_uses_full_pool_when_harvest_is_disabled() -> None:
    settings = get_settings().model_copy(
        update={
            "color_synonym_batch_harvest_enabled": False,
            "color_synonym_pool_max_size": 4,
            "color_synonym_harvest_max_concurrency": 2,
        }
    )
    assert sc._color_synonym_runtime_max_concurrency(settings) == 4

    enabled = settings.model_copy(update={"color_synonym_batch_harvest_enabled": True})
    assert sc._color_synonym_runtime_max_concurrency(enabled) == 2


async def test_expansion_loads_off_loop_and_sends_repeated_values(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"color_synonym_expansion_enabled": True})
    seen = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda *, timeout=None: _Client(seen))

    from app.pipelines import color_synonyms

    empty_guard_flags = []

    def loaded_map(dsn, ttl_s, warn_if_empty):
        empty_guard_flags.append(warn_if_empty)
        return {"남색": ["네이비", "남색"]}

    monkeypatch.setattr(
        color_synonyms,
        "get_synonym_map",
        loaded_map,
    )
    await sc.search_products(ProductSearchFilters(color="남색"))
    assert seen == [{"color": ["네이비", "남색"]}]
    assert empty_guard_flags == [True]


def test_ai_side_filters_leave_all_color_cases_to_spring() -> None:
    """정본 I-1 3갈래 판정의 ②는 BE 소관이다.

    색상 축이 없으면 통과하고, 축이 있어도 부분일치 판정은 BE가 한다. AI 사후필터가 색상을
    하드필터하면 색상 미상 상품과 불일치 상품이 사라져 ② 규칙과 부분일치 주체가 깨진다.
    """
    products = [
        SpringProduct(product_id=1, name="색상 축 없음", attributes={"소재": "면"}),
        SpringProduct(product_id=2, name="색상 일치", attributes={"색상": "그레이"}),
        SpringProduct(product_id=3, name="색상 불일치", attributes={"색상": "블랙"}),
    ]

    assert apply_ai_side_filters(products, ProductSearchFilters(color="그레이")) == products


def test_color_payload_axes_and_values_are_unchanged_by_expansion() -> None:
    """색상 배열 계약에서도 값 정규화·부분일치는 BE가 맡고 AI는 원문 값을 보존한다."""
    filters = ProductSearchFilters(color=" 그레이 ")
    expanded = ["다크그레이", " 그레이 "]

    assert sc.search_filter_axes(filters) == {"color"}
    assert sc.search_filter_axes(filters, color_values=expanded) == {"color"}
    assert sc._search_query_params(filters, color_values=expanded) == {"color": expanded}
