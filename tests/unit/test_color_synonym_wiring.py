"""I-1 색상 확장 플래그·실패 degrade 배선 테스트 (이슈 #258)."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters
from app.services import spring_client as sc


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
    monkeypatch.setattr(sc, "_client", lambda: _Client(seen))

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
    monkeypatch.setattr(sc, "_client", lambda: _Client(seen))

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
    monkeypatch.setattr(sc, "_client", lambda: _Client(seen))

    async def never_finishes(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(sc.asyncio, "to_thread", never_finishes)
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
            "color_synonym_pool_max_size": 1,
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
    monkeypatch.setattr(sc, "_client", lambda: _Client(seen))
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


async def test_expansion_loads_off_loop_and_sends_repeated_values(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"color_synonym_expansion_enabled": True})
    seen = []
    monkeypatch.setattr(sc, "get_settings", lambda: settings)
    monkeypatch.setattr(sc, "_client", lambda: _Client(seen))

    from app.pipelines import color_synonyms

    monkeypatch.setattr(
        color_synonyms,
        "get_synonym_map",
        lambda dsn, ttl_s: {"남색": ["네이비", "남색"]},
    )
    await sc.search_products(ProductSearchFilters(color="남색"))
    assert seen == [{"color": ["네이비", "남색"]}]
