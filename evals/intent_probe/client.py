"""프롬프트 주입·페이싱 래퍼와 실 provider 클라이언트 구성.

**프로덕션 코드는 건드리지 않는다.** `decompose` 는 `llm` 을 인자로 받고 `_SYSTEM` 을 호출부
한 곳에서만 쓰므로(`decompose.py` 의 `llm.complete(system=_SYSTEM, ...)`), 클라이언트를 감싸
`system=` 을 바꿔치기하면 후보 프롬프트를 잴 수 있다. `decompose` 에 파라미터를 새로 뚫는 것보다
안전하다 — 프로덕션 호출부가 실수로 프롬프트를 넘길 여지가 없다.

래퍼 순서(바깥 → 안):
    SystemPromptOverrideLLM → PacedLLM → RecordingLLM → OpenAILLM | AnthropicLLM
`PacedLLM` 이 `RecordingLLM` 바깥이라 **실패할 시도까지 전부 페이서를 지난다** — 429 도 레이트를
소모하므로 재시도도 페이싱돼야 한다.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation import decompose as decompose_module
from app.agents.buyer.recommendation.category_scope import _SYSTEM as CATEGORY_SCOPE_SYSTEM
from app.agents.buyer.recommendation.underspecified_classifier import (
    _SYSTEM as UNDERSPECIFIED_SYSTEM,
)
from app.core.config import Settings
from app.core.llm import AnthropicLLM, LLMClient, OpenAILLM, resolve_provider_model
from evals.intent_probe.pacer import GlobalPacer
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetTracker
from evals.model_eval.pricing import PriceBook
from evals.model_eval.recording import RecordingLLM

DECOMPOSE_RELPATH = "app/agents/buyer/recommendation/decompose.py"
REPO_PROMPT_SOURCE = "repo:_SYSTEM_WITH_DEDICATED_UNDERSPECIFIED"
# `git show` 는 CWD 기준으로 리포를 찾는다 — 실행 위치에 기대지 않도록 루트를 명시한다
# (`evals/metrics/run_manifest.py` 가 커밋 해시를 읽을 때와 같은 규약).
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PromptIdentity:
    """무엇을 쟀는지 남기는 라벨. sha12 는 리포트 첫 줄에 그대로 실린다."""

    source: str
    sha256: str
    char_count: int

    @property
    def sha12(self) -> str:
        return self.sha256[:12]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "sha12": self.sha12,
            "charCount": self.char_count,
        }


def repo_system_prompt() -> str:
    """#463 기본 배포 경로가 provider에 보내는 decompose 문면."""
    return decompose_module._SYSTEM_WITH_DEDICATED_UNDERSPECIFIED


def prompt_identity(text: str, *, source: str) -> PromptIdentity:
    return PromptIdentity(
        source=source,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        char_count=len(text),
    )


def extract_system_prompt(source_code: str) -> str:
    """decompose.py 소스에서 `_SYSTEM` 리터럴만 뽑는다 (과거 판 재현용)."""
    for node in ast.parse(source_code).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_SYSTEM":
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("decompose.py 에서 _SYSTEM 을 찾지 못했습니다")


def read_prompt_from_git(rev: str, *, relpath: str = DECOMPOSE_RELPATH) -> str:
    """`git show <rev>:decompose.py` 로 과거 판 프롬프트를 읽는다.

    #240 기준선 `e5e195822495` 는 커밋 `3f1dec7` 에 있다. 프롬프트 사본을 리포에 늘리지 않고
    과거 표를 다시 재기 위한 통로다.
    """
    result = subprocess.run(
        ["git", "show", f"{rev}:{relpath}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 or not result.stdout:
        raise ValueError(
            f"git rev '{rev}' 에서 {relpath} 를 읽지 못했습니다: {result.stderr.strip()}"
        )
    return extract_system_prompt(result.stdout)


def resolve_system_prompt(
    *, prompt_path: Path | None = None, prompt_rev: str | None = None
) -> tuple[str | None, PromptIdentity]:
    """후보 프롬프트를 고른다. 반환 text 가 None 이면 리포의 `_SYSTEM` 을 그대로 쓴다.

    **strip 하지 않는다** — 조용한 정규화는 '무엇을 쟀는지'를 흐린다. 파일 끝 개행 하나가
    sha256 을 바꾸며, 그게 의도된 동작이다(왕복은 `--dump-prompt` 로).
    """
    if prompt_path is not None and prompt_rev is not None:
        raise ValueError("--prompt 와 --prompt-rev 는 함께 쓸 수 없습니다")
    if prompt_path is not None:
        text = prompt_path.read_text(encoding="utf-8")
        return text, prompt_identity(text, source=f"file:{prompt_path.as_posix()}")
    if prompt_rev is not None:
        text = read_prompt_from_git(prompt_rev)
        return text, prompt_identity(text, source=f"git:{prompt_rev}")
    return None, prompt_identity(repo_system_prompt(), source=REPO_PROMPT_SOURCE)


# [#84] 후보 프롬프트로 **갈아끼우면 안 되는** system 문면. 이 프로브는 더 이상 decompose 만
# 부르지 않는다 — 카테고리 범위 해제 분류기(`category_scope`)를 같은 클라이언트로 함께 부른다.
# 아래 래퍼의 초판 docstring 이 스스로 경고한 자리다("다른 노드를 함께 재게 되면 호출부 라벨로
# 분기해야 한다"). 그대로 뒀다면 `--prompt`/`--prompt-rev` 런에서 분류기가 **decompose 후보
# 프롬프트**를 받아 판정이 무의미해지고, 그 사실이 표에는 "분류기가 갑자기 무동작"으로만 보인다.
PASSTHROUGH_SYSTEMS = frozenset({CATEGORY_SCOPE_SYSTEM, UNDERSPECIFIED_SYSTEM})


class SystemPromptOverrideLLM:
    """통과하는 `complete` 의 system 을 후보 텍스트로 갈아끼운다.

    **`PASSTHROUGH_SYSTEMS` 에 있는 문면은 건드리지 않는다** — 그 문면은 decompose 가 아니라
    보조 노드(전용 분류기)의 것이라, 후보 프롬프트로 덮으면 그 노드가 통째로 망가진다.
    새 보조 노드를 프로브에 태울 때마다 그 목록을 함께 늘려야 한다.
    """

    def __init__(self, delegate: LLMClient, *, system: str | None) -> None:
        self.delegate = delegate
        self.system = system

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        override = self.system is not None and system not in PASSTHROUGH_SYSTEMS
        return await self.delegate.complete(
            system=self.system if override else system,
            user=user,
            tier=tier,
            max_tokens=max_tokens,
            json_output=json_output,
        )

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        return self.delegate.stream(
            system=self.system if self.system is not None else system,
            user=user,
            tier=tier,
            max_tokens=max_tokens,
        )


class PacedLLM:
    """provider 호출 직전에 전역 페이서를 통과시킨다."""

    def __init__(self, delegate: LLMClient, *, pacer: GlobalPacer) -> None:
        self.delegate = delegate
        self.pacer = pacer

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        await self.pacer.acquire()
        return await self.delegate.complete(
            system=system, user=user, tier=tier, max_tokens=max_tokens, json_output=json_output
        )

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        await self.pacer.acquire()
        async for chunk in self.delegate.stream(
            system=system, user=user, tier=tier, max_tokens=max_tokens
        ):
            yield chunk


def build_live_delegate(
    *,
    runtime_settings: Settings,
    budget: BudgetTracker,
    pricing: PriceBook,
    max_retries: int = 0,
) -> tuple[RecordingLLM, dict[str, Any]]:
    """배포 후보 모델 필드만 복사해 실 provider 클라이언트를 만든다.

    `evals/model_eval/cli.py::build_live_adapter` 와 같은 방식(env 를 읽지 않는
    `EvaluationSettings`)이되, **max_retries 를 0 으로 낮춘다** — SDK 내부 재시도는 페이서를
    우회하고 N 채우기 로직에도 보이지 않는다. 재시도를 프로브가 직접 통제해야 분포가 정직해진다
    (`evals/model_eval` 은 배포 후보의 재시도 거동까지 평가하려고 반대 선택을 한다).
    """
    settings = EvaluationSettings(
        llm_provider=runtime_settings.llm_provider,
        anthropic_api_key=runtime_settings.anthropic_api_key,
        haiku_model_id=runtime_settings.haiku_model_id,
        sonnet_model_id=runtime_settings.sonnet_model_id,
        openai_api_key=runtime_settings.openai_api_key,
        openai_fast_model_id=runtime_settings.openai_fast_model_id,
        openai_smart_model_id=runtime_settings.openai_smart_model_id,
        openai_fast_reasoning_effort=runtime_settings.openai_fast_reasoning_effort,
        openai_smart_reasoning_effort=runtime_settings.openai_smart_reasoning_effort,
        llm_timeout_s=runtime_settings.llm_timeout_s,
        llm_max_retries=max_retries,
        auth_mode="dev",
        internal_api_token="intent-probe-internal-token",
        search_backend="spring",
    )
    fast = resolve_provider_model(settings, "fast")
    smart = resolve_provider_model(settings, "smart")
    delegate: LLMClient
    if fast.provider == "openai":
        delegate = OpenAILLM(
            fast.api_key,
            fast_model=fast.model_id,
            smart_model=smart.model_id,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            fast_reasoning_effort=fast.reasoning_effort or "",
            smart_reasoning_effort=smart.reasoning_effort or "",
        )
    else:
        delegate = AnthropicLLM(
            fast.api_key,
            fast_model=fast.model_id,
            smart_model=smart.model_id,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    recorder = RecordingLLM(
        delegate,
        models={"fast": fast.model_id, "smart": smart.model_id},
        reasoning_efforts={"fast": fast.reasoning_effort, "smart": smart.reasoning_effort},
        pricing=pricing,
        budget=budget,
    )
    model_config = {
        "provider": fast.provider,
        "fastModel": fast.model_id,
        "smartModel": smart.model_id,
        "fastReasoningEffort": fast.reasoning_effort,
        "smartReasoningEffort": smart.reasoning_effort,
        "timeoutS": settings.llm_timeout_s,
        "maxRetries": settings.llm_max_retries,
    }
    return recorder, model_config


def build_probe_llm(
    delegate: LLMClient, *, pacer: GlobalPacer, system: str | None
) -> SystemPromptOverrideLLM:
    """래퍼 사슬을 조립한다 — 페이싱은 항상 프롬프트 교체 안쪽에 있다."""
    return SystemPromptOverrideLLM(PacedLLM(delegate, pacer=pacer), system=system)
