"""N 채우기 러너와 프롬프트·페이싱 래퍼 (#260).

핵심 불변식: **실패는 표본이 아니다.** 429·타임아웃으로 빈 칸이 생겼는데 그것을 오답으로 세면
분포 전체가 거짓이 된다(#240 초기 2런 폐기 사유).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.intent_probe.client import (
    REPO_PROMPT_SOURCE,
    PacedLLM,
    SystemPromptOverrideLLM,
    build_probe_llm,
    extract_system_prompt,
    repo_system_prompt,
    resolve_system_prompt,
)
from evals.intent_probe.fakes import ScriptedDecomposeLLM
from evals.intent_probe.loader import build_cells, load_anchor_set
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.intent_probe.runner import run_cell, run_probe, scrub_message
from evals.model_eval.budget import BudgetExceeded

ANCHORS = load_anchor_set("b")
CELLS = build_cells(ANCHORS)


async def _no_sleep(seconds: float) -> None:
    return None


def _loose_pacer() -> GlobalPacer:
    return GlobalPacer(PacerLimits(max_rpm=10_000, max_tpm=10_000_000_000))


async def _run_one(llm, *, cell=None, n=8, attempt_multiplier=3, tier="fast"):
    return await run_cell(
        llm=llm,
        cell=cell or CELLS[0],
        anchors=ANCHORS,
        n=n,
        tier=tier,
        attempt_multiplier=attempt_multiplier,
        sleep=_no_sleep,
    )


async def test_retries_fill_n_and_failures_never_become_samples() -> None:
    llm = ScriptedDecomposeLLM(ANCHORS, fail_first=3)
    result = await _run_one(llm)
    assert len(result.samples) == 8
    assert result.attempts == 11
    assert len(result.failures) == 3
    assert result.filled is True
    assert [sample.sample_index for sample in result.samples] == list(range(8))


async def test_unfillable_cell_is_reported_not_raised() -> None:
    llm = ScriptedDecomposeLLM(ANCHORS, always_fail=True)
    result = await _run_one(llm, n=8, attempt_multiplier=3)
    assert result.samples == []
    assert result.filled is False
    assert result.attempts == 24
    assert len(result.failures) == 24


async def test_budget_exceeded_stops_the_run() -> None:
    class _BudgetBurst:
        async def complete(self, **_: object) -> str:
            raise BudgetExceeded("maxCallsExceeded")

    with pytest.raises(BudgetExceeded):
        await _run_one(_BudgetBurst())


async def test_every_attempt_passes_the_pacer_including_failures() -> None:
    # 429 도 레이트를 소모한다 — 재시도가 페이서를 우회하면 다음 런이 또 굶는다.
    pacer = _loose_pacer()
    llm = build_probe_llm(ScriptedDecomposeLLM(ANCHORS, fail_first=3), pacer=pacer, system=None)
    result = await _run_one(llm)
    assert result.attempts == 11
    assert len(pacer.granted_at) == 11


async def test_prompt_override_replaces_system_for_every_call() -> None:
    fake = ScriptedDecomposeLLM(ANCHORS)
    llm = SystemPromptOverrideLLM(fake, system="후보 프롬프트")
    await _run_one(llm, n=2)
    assert {call["system"] for call in fake.calls} == {"후보 프롬프트"}


async def test_without_override_the_repo_prompt_reaches_the_provider() -> None:
    fake = ScriptedDecomposeLLM(ANCHORS)
    llm = PacedLLM(fake, pacer=_loose_pacer())
    await _run_one(llm, n=2)
    assert {call["system"] for call in fake.calls} == {repo_system_prompt()}


async def test_tier_reaches_the_provider() -> None:
    fake = ScriptedDecomposeLLM(ANCHORS)
    await _run_one(fake, n=2, tier="smart")
    assert {call["tier"] for call in fake.calls} == {"smart"}


async def test_pending_cart_cells_carry_the_reask_product_and_options() -> None:
    cell = next(cell for cell in CELLS if cell.context.context_id == "pendingCart")
    fake = ScriptedDecomposeLLM(ANCHORS)
    await _run_one(fake, cell=cell, n=1)
    user = fake.calls[0]["user"]
    assert f'"productId": {ANCHORS.reask_product_id}' in user
    assert "드럼형" in user


async def test_run_probe_order_is_independent_of_concurrency() -> None:
    sequential = await run_probe(
        llm=ScriptedDecomposeLLM(ANCHORS),
        cells=CELLS[:6],
        anchors=ANCHORS,
        n=2,
        tier="fast",
        attempt_multiplier=3,
        concurrency=1,
        sleep=_no_sleep,
    )
    parallel = await run_probe(
        llm=ScriptedDecomposeLLM(ANCHORS),
        cells=CELLS[:6],
        anchors=ANCHORS,
        n=2,
        tier="fast",
        attempt_multiplier=3,
        concurrency=4,
        sleep=_no_sleep,
    )
    assert [cell.cell_id for cell in sequential] == [cell.cell_id for cell in parallel]
    assert [cell.cell_id for cell in sequential] == sorted(cell.cell_id for cell in sequential)


def test_resolve_system_prompt_defaults_to_repo_prompt() -> None:
    text, identity = resolve_system_prompt()
    assert text is None
    assert identity.source == REPO_PROMPT_SOURCE
    assert identity.char_count == len(repo_system_prompt())
    assert len(identity.sha12) == 12


def test_resolve_system_prompt_reads_candidate_file_verbatim(tmp_path: Path) -> None:
    # 끝 개행 하나가 해시를 바꾸는 것이 의도된 동작이다 — strip 하면 무엇을 쟀는지 흐려진다.
    path = tmp_path / "cand.txt"
    path.write_text("후보\n", encoding="utf-8")
    text, identity = resolve_system_prompt(prompt_path=path)
    assert text == "후보\n"
    assert identity.source.startswith("file:")
    assert identity.sha12 != resolve_system_prompt()[1].sha12


def test_prompt_and_prompt_rev_are_mutually_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "cand.txt"
    path.write_text("후보", encoding="utf-8")
    with pytest.raises(ValueError, match="함께"):
        resolve_system_prompt(prompt_path=path, prompt_rev="HEAD")


def test_prompt_rev_reads_git_regardless_of_cwd(tmp_path: Path, monkeypatch) -> None:
    # `git show` 는 CWD 기준으로 리포를 찾는다 — 실행 위치에 기대면 리포 밖에서 조용히 실패한다.
    monkeypatch.chdir(tmp_path)
    text, identity = resolve_system_prompt(prompt_rev="HEAD")
    assert text and "당신은 커머스 어시스턴트의 질의 분해기입니다" in text
    assert identity.source == "git:HEAD"


def test_prompt_rev_reports_unknown_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="읽지 못했습니다"):
        resolve_system_prompt(prompt_rev="not-a-real-rev")


def test_failure_messages_drop_account_identifiers() -> None:
    # 산출물은 리포에 커밋된다. 429 본문에 org id 가 그대로 들어오므로 지우고 남긴다.
    raw = (
        "Error code: 429 - Rate limit reached for gpt-5-nano in organization "
        "org-erkD3CljLOJzAjKPgPzUBmRl on tokens per min (TPM): Limit 200000, Used 200000"
    )
    scrubbed = scrub_message(raw)
    assert "org-erkD3CljLOJzAjKPgPzUBmRl" not in scrubbed
    assert "org-***" in scrubbed
    assert "Limit 200000, Used 200000" in scrubbed  # 원인 판별에 필요한 문구는 살린다


def test_extract_system_prompt_reads_the_literal() -> None:
    source = 'X = 1\n_SYSTEM = """규칙"""\n'
    assert extract_system_prompt(source) == "규칙"


def test_extract_system_prompt_rejects_source_without_the_constant() -> None:
    with pytest.raises(ValueError, match="_SYSTEM"):
        extract_system_prompt("X = 1\n")


# ─────────── #84 카테고리 승계 3분기 ───────────


async def test_sample_carries_the_raw_signal_and_the_resolved_action() -> None:
    """확정값은 **배포 경로와 같은 함수**로 낸다 — 프로브가 규칙을 재구현하면 이 테스트가 잡는다.

    재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다(lessons 2026-08-04). 그래서
    `resolve_category_action` 을 여기서 직접 부른 값과 표본을 대조한다.
    """
    from app.agents.buyer.recommendation.decompose import resolve_category_action

    cells = [cell for cell in CELLS if cell.utterance.group == "category_action"]
    assert cells, "카테고리 셀이 없다 — 픽스처가 어긋났다"
    for cell in cells:
        result = await _run_one(ScriptedDecomposeLLM(ANCHORS), cell=cell, n=8)
        for sample in result.samples:
            assert sample.resolved_category_action == resolve_category_action(
                has_category_signal=sample.has_category_signal,
                scope_free=sample.scope_free,
            )


async def test_scope_classifier_runs_on_category_cells() -> None:
    """[#84] 프로브도 배포처럼 **두 호출**을 한다 — 분류기가 빠지면 측정이 배포와 갈라진다."""
    cell = next(cell for cell in CELLS if cell.utterance.utterance_id.startswith("category-clear"))
    result = await _run_one(ScriptedDecomposeLLM(ANCHORS, wrong_every=1000), cell=cell, n=2)
    assert all(sample.scope_free is True for sample in result.samples)
    assert all(sample.resolved_category_action == "clear" for sample in result.samples)


async def test_scope_classifier_is_skipped_without_a_prior_category() -> None:
    """직전 카테고리가 없는 컨텍스트는 호출하지 않는다 — `graph.py` 게이트와 같은 조건."""
    cell = next(cell for cell in CELLS if cell.context.context_id == "none")
    result = await _run_one(ScriptedDecomposeLLM(ANCHORS), cell=cell, n=2)
    assert all(sample.scope_free is None for sample in result.samples)


async def test_carry_cells_are_recognised_as_prior_echo() -> None:
    """[2차 리뷰 P2] carry 턴의 leg 은 prior 에코라 확정값이 replace 여도 카테고리가 유지된다."""
    cell = next(cell for cell in CELLS if cell.utterance.utterance_id.startswith("category-carry"))
    result = await _run_one(ScriptedDecomposeLLM(ANCHORS, wrong_every=1000), cell=cell, n=2)
    assert all(sample.category_legs_echo_prior for sample in result.samples)


async def test_replace_needs_a_leg_to_resolve_as_replace() -> None:
    """신호 없는 `replace` 는 carry 로 확정된다(규칙 3) — 가짜 LLM 도 실 LLM 처럼 leg 를 싣는다."""
    cell = next(
        cell for cell in CELLS if cell.utterance.utterance_id.startswith("category-replace")
    )
    result = await _run_one(ScriptedDecomposeLLM(ANCHORS, wrong_every=1000), cell=cell, n=4)
    assert {sample.resolved_category_action for sample in result.samples} == {"replace"}
    assert all(sample.has_category_signal for sample in result.samples)


async def test_non_category_cells_report_the_carry_fallback() -> None:
    """카테고리 신호도 산출도 없는 턴은 carry(=오늘 동작)로 떨어진다 — 폴백이 관측된다."""
    cell = next(cell for cell in CELLS if cell.utterance.group == "cart_control")
    result = await _run_one(ScriptedDecomposeLLM(ANCHORS), cell=cell, n=2)
    for sample in result.samples:
        assert sample.scope_free is None  # 분류기 게이트가 닫힌 셀이다
        assert sample.has_category_signal is False
        assert sample.resolved_category_action == "carry"


def test_candidate_prompt_override_does_not_reach_the_classifier() -> None:
    """[#84] `--prompt` 는 decompose 후보만 갈아끼운다 — 보조 노드 문면은 통과시킨다.

    이 래퍼는 원래 **모든** 호출의 system 을 덮었고 그 docstring 이 "다른 노드를 함께 재게 되면
    분기하라"고 스스로 경고했다. 프로브가 분류기를 함께 부르게 된 지금 그 경고가 실제 결함이
    된다 — 분류기가 decompose 후보 프롬프트를 받으면 판정이 무의미해지고, 표에는 "분류기가
    갑자기 무동작"으로만 보인다.
    """
    from app.agents.buyer.recommendation.category_scope import _SYSTEM as SCOPE_SYSTEM
    from evals.intent_probe.client import PASSTHROUGH_SYSTEMS, SystemPromptOverrideLLM

    seen: list[str] = []

    class _Echo:
        async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
            seen.append(system)
            return "{}"

    wrapper = SystemPromptOverrideLLM(_Echo(), system="후보 프롬프트")
    assert SCOPE_SYSTEM in PASSTHROUGH_SYSTEMS

    async def _drive() -> None:
        await wrapper.complete(system="원래 decompose", user="u", tier="fast")
        await wrapper.complete(system=SCOPE_SYSTEM, user="u", tier="fast")

    asyncio.run(_drive())
    assert seen == ["후보 프롬프트", SCOPE_SYSTEM]


# ─────────── #84 2차 리뷰 F-4 — prior 에코 판정은 정확 일치다 ───────────


def _echo(raw, query):  # noqa: ANN001
    """앵커 B 의 `categoryPriorFilters` 로 leg 하나를 판정한다."""
    from app.agents.buyer.recommendation.state import CategoryQuery
    from evals.intent_probe.runner import _legs_echo_prior, _prior_echo_tokens

    return _legs_echo_prior([CategoryQuery(raw, query)], _prior_echo_tokens(ANCHORS))


def test_substring_of_the_prior_category_is_not_an_echo() -> None:
    """[F-4] `"이어폰 케이스"` 는 **새 상품**이다 — 부분 문자열이면 카테고리가 바뀐 턴을
    "유지됐다"로 세게 된다(배포 매퍼는 그것을 액세서리 카테고리로 바꾼다).

    `docs/lessons.md` [2026-08-02] 「부분 문자열 매칭은 포함 방향마다 의미가 다르다」가 가리키는
    함정이라 정규화 후 정확 일치로 좁혔다.
    """
    assert _echo(None, "이어폰 케이스") is False
    assert _echo("음향가전 > 이어폰 액세서리", "이어폰 케이스") is False


@pytest.mark.parametrize(
    ("raw", "query"),
    [
        ("음향가전 > 이어폰", "무선 이어폰"),  # 실측 표본 ①
        ("음향가전", "무선 이어폰"),  # 실측 표본 ②
        ("이어폰", None),  # 잎만 되풀이
        (None, "무선 이어폰"),  # semanticQuery 만
        ("  음향가전   >   이어폰  ", None),  # 공백 정규화
        ("음향가전 > 이어폰".upper(), None),  # 대소문자(라틴 문자가 섞여도 안전)
    ],
)
def test_prior_vocabulary_is_recognised_as_an_echo(raw, query) -> None:
    assert _echo(raw, query) is True


def test_echo_needs_every_leg_to_be_an_echo() -> None:
    """하나라도 새 상품이면 카테고리가 유지된 것이 아니다."""
    from app.agents.buyer.recommendation.state import CategoryQuery
    from evals.intent_probe.runner import _legs_echo_prior, _prior_echo_tokens

    tokens = _prior_echo_tokens(ANCHORS)
    legs = [CategoryQuery("음향가전 > 이어폰", "무선 이어폰"), CategoryQuery(None, "노트북")]
    assert _legs_echo_prior(legs, tokens) is False
    assert _legs_echo_prior([], tokens) is False  # 신호 없음은 에코가 아니다


def test_category_legs_are_serialised_for_recounting() -> None:
    """[F-4] leg 원문을 남겨야 판정 규칙이 바뀌어도 **런을 다시 돌리지 않고** 재집계할 수 있다."""
    from app.agents.buyer.recommendation.state import CategoryQuery
    from evals.intent_probe.runner import serialize_category_legs

    assert serialize_category_legs([]) == ""
    assert (
        serialize_category_legs(
            [CategoryQuery("음향가전 > 이어폰", "무선 이어폰"), CategoryQuery(None, "노트북")]
        )
        == "음향가전 > 이어폰|무선 이어폰;|노트북"
    )
