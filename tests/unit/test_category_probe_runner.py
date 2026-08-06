"""N 채우기 러너 테스트 — #331. 전부 가짜 LLM·가짜 pg 라 API/pg 콜이 0이다."""

from __future__ import annotations

from app.core.config import get_settings
from evals.category_probe.fakes import ScriptedCategoryLLM, build_fake_catalog
from evals.category_probe.loader import load_anchor_set
from evals.category_probe.runner import run_cell, run_probe

ANCHORS = load_anchor_set("default")
CATALOG = build_fake_catalog(ANCHORS)
SINGLE_CELL = next(cell for cell in ANCHORS.cells if cell.slice_id == "single")
NONE_CELL = next(cell for cell in ANCHORS.cells if cell.slice_id == "none")
MULTI_CELL = next(cell for cell in ANCHORS.cells if cell.slice_id == "multi")


async def _no_sleep(seconds: float) -> None:
    return None


def _kwargs(**overrides):
    base = dict(
        n=4,
        tier="fast",
        attempt_multiplier=3,
        category_fanout_max=5,
        settings=get_settings(),
        embed=CATALOG.embed,
        search_top_k=CATALOG.search,
        exact_lookup=CATALOG.exact,
        sleep=_no_sleep,
    )
    base.update(overrides)
    return base


async def test_retries_fill_n_and_failures_never_become_samples() -> None:
    llm = ScriptedCategoryLLM(ANCHORS, fail_first=2, wrong_every=0)
    result = await run_cell(llm=llm, cell=SINGLE_CELL, **_kwargs())
    assert len(result.samples) == 4
    assert result.filled is True
    assert len(result.failures) == 2
    assert result.attempts == 6


async def test_unfillable_cell_is_reported_not_raised() -> None:
    llm = ScriptedCategoryLLM(ANCHORS, always_fail=True)
    result = await run_cell(llm=llm, cell=SINGLE_CELL, **_kwargs(n=2, attempt_multiplier=3))
    assert result.samples == []
    assert result.filled is False
    assert result.attempts == 6
    assert len(result.failures) == 6


async def test_intent_slip_is_discarded_and_counted_not_a_sample() -> None:
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=0, intent_slip_every=2)
    result = await run_cell(llm=llm, cell=SINGLE_CELL, **_kwargs(n=3, attempt_multiplier=5))
    assert len(result.samples) == 3
    assert result.filled is True
    assert result.intent_slip_count > 0
    assert result.failures == []  # slips are not failures


async def test_none_slice_cell_yields_empty_legs() -> None:
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=0)
    result = await run_cell(llm=llm, cell=NONE_CELL, **_kwargs(n=2))
    assert result.filled is True
    assert all(sample.legs == [] for sample in result.samples)


async def test_exact_match_path_produces_no_hits_but_a_sample() -> None:
    """raw==accept 라 exact_lookup 이 즉시 맞춰 임베딩 검색을 타지 않는 결정론 경로."""
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=0)
    result = await run_cell(llm=llm, cell=SINGLE_CELL, **_kwargs(n=1))
    sample = result.samples[0]
    assert sample.legs  # 매핑에 성공
    accept_values = {a for leg in SINGLE_CELL.expected_legs for a in leg.accept}
    assert any(canonical in accept_values for canonical, _ in sample.legs)


async def test_wrong_cycle_forces_embedding_path_and_records_hits() -> None:
    """오답 주기에서 raw 가 exact 미스라 임베딩 검색을 타고 hits 가 기록돼야 한다."""
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=1)  # 매 시도 오답 주기
    result = await run_cell(llm=llm, cell=SINGLE_CELL, **_kwargs(n=1, attempt_multiplier=5))
    sample = result.samples[0]
    assert sample.hits, "임베딩 경로를 타면 hits 가 기록돼야 한다"
    assert {hit.anchor_kind for hit in sample.hits} <= {"raw", "query"}


async def test_multi_cell_reports_multiple_legs() -> None:
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=0)
    result = await run_cell(llm=llm, cell=MULTI_CELL, **_kwargs(n=1))
    sample = result.samples[0]
    assert len(sample.legs) == len(MULTI_CELL.expected_legs)


async def test_run_probe_order_is_independent_of_concurrency() -> None:
    llm_factory = lambda: ScriptedCategoryLLM(ANCHORS, wrong_every=0)  # noqa: E731
    sequential = await run_probe(
        llm=llm_factory(),
        anchors=ANCHORS,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        concurrency=1,
        settings=get_settings(),
        embed=CATALOG.embed,
        search_top_k=CATALOG.search,
        exact_lookup=CATALOG.exact,
        sleep=_no_sleep,
    )
    concurrent = await run_probe(
        llm=llm_factory(),
        anchors=ANCHORS,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        concurrency=8,
        settings=get_settings(),
        embed=CATALOG.embed,
        search_top_k=CATALOG.search,
        exact_lookup=CATALOG.exact,
        sleep=_no_sleep,
    )
    assert [r.cell_id for r in sequential] == [r.cell_id for r in concurrent]


async def test_infra_search_failure_is_discarded_not_a_sample() -> None:
    """F1-1 리뷰어 재현: map_categories 가 내부에서 흡수하는 pg/embed/select 예외는
    category_leg_search_failed 등 이벤트로만 남고 예외를 전파하지 않는다 — search_top_k 가
    항상 던져도 종전 러너는 legs=[] '가짜 정상' 표본을 냈다. 인프라 실패는 표본이 아니다."""

    def _always_raise_search(vec, dsn, *, k):  # noqa: ANN001, ARG001
        raise TimeoutError("boom")

    llm = ScriptedCategoryLLM(
        ANCHORS, wrong_every=1
    )  # 항상 raw 오답 → exact 미스로 임베딩 경로 강제
    result = await run_cell(
        llm=llm,
        cell=SINGLE_CELL,
        n=2,
        tier="fast",
        attempt_multiplier=3,
        sleep=_no_sleep,
        embed=CATALOG.embed,
        search_top_k=_always_raise_search,
        exact_lookup=CATALOG.exact,
    )
    assert result.samples == [], "인프라 실패로 만들어진 legs=[] 표본은 표본으로 세면 안 된다"
    assert result.filled is False
    assert result.failures, "인프라 실패가 failures.csv 로 남아야 한다"
    assert any(f.error_type == "category_leg_search_failed" for f in result.failures)


async def test_infra_select_stage_failure_is_discarded() -> None:
    """F1-1 — category_select_unavailable 의 reason 이 정책 사유 3종이 아니면(=LLM 실패 문자열)
    인프라 실패로 취급한다. margin 트리거를 강제로 만들어 §4.4 택일이 항상 실패하게 한다."""

    class _AlwaysFailSelectLLM(ScriptedCategoryLLM):
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            from app.agents.buyer.recommendation.category_select import _SYSTEM as _SELECT_SYSTEM

            if system == _SELECT_SYSTEM:
                raise RuntimeError("scripted select failure")
            return await super().complete(
                system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
            )

    def _ambiguous_search(vec, dsn, *, k):  # noqa: ANN001, ARG001
        # 거리컷(0.22 기본)은 통과하고 마진(0.02 기본)은 못 넘는 고정 히트 2건 — §4.4 택일을
        # 결정론으로 강제 트리거한다(FakeCatalog 의 해시 벡터 거리는 임계 근처를 재현하기
        # 어려워 여기서는 값을 직접 고정한다).
        return [("건강식품 > 인삼/한방재료", 0.10), ("여성의류 > 원피스", 0.101)]

    llm = _AlwaysFailSelectLLM(ANCHORS, wrong_every=1)
    settings = get_settings()
    result = await run_cell(
        llm=llm,
        cell=SINGLE_CELL,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        sleep=_no_sleep,
        embed=CATALOG.embed,
        search_top_k=_ambiguous_search,
        exact_lookup=CATALOG.exact,
        settings=settings,
    )
    assert result.samples == []
    assert result.filled is False
    assert any(f.error_type == "category_select_unavailable" for f in result.failures)


async def test_default_settings_smoke() -> None:
    """전 테스트가 기본값을 덮어쓰면 기본 조합의 결함이 숨는다 — 실 Settings 기본으로 최소 1회."""
    llm = ScriptedCategoryLLM(ANCHORS, wrong_every=0)
    result = await run_cell(
        llm=llm,
        cell=SINGLE_CELL,
        n=1,
        tier="fast",
        attempt_multiplier=3,
        sleep=_no_sleep,
        embed=CATALOG.embed,
        search_top_k=CATALOG.search,
        exact_lookup=CATALOG.exact,
        # settings 미지정 — get_settings() 기본값 그대로, category_fanout_max 도 기본값.
    )
    assert result.filled is True
