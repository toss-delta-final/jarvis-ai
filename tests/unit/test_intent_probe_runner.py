"""N 채우기 러너와 프롬프트·페이싱 래퍼 (#260).

핵심 불변식: **실패는 표본이 아니다.** 429·타임아웃으로 빈 칸이 생겼는데 그것을 오답으로 세면
분포 전체가 거짓이 된다(#240 초기 2런 폐기 사유).
"""

from __future__ import annotations

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
