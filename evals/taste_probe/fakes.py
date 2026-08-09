"""API 를 부르지 않는 결정론 가짜 LLM·카탈로그 — `--dry-run` 과 유닛테스트 전용 (#462).

실 분포를 흉내 내지 않는다. **배관이 도는지**(N 채우기·단계 귀속·채점·산출물)만 확인하는
물건이라 정답과 오답을 고정 패턴으로 낸다(`evals/category_probe/fakes.py` 와 동일 철학). 이
가짜로 잰 표를 추출 정확도 판정 근거로 쓰면 안 된다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.llm import LLMError
from evals.taste_probe.schema import GoldenSession, GoldenSet

_FAKE_DIM = 8

# 미분류 시험용 kind 회전표 — brand/attribute/situation 은 통제 어휘가 없어 항상 resolve 되므로
# (verified=false), 회전해도 결정론적으로 산출 트리플이 생겨 혼동 행렬에 잡힌다.
_KIND_ROTATION: dict[str, str] = {
    "brand": "attribute",
    "attribute": "situation",
    "situation": "brand",
    "category": "attribute",
    "priceBand": "ratingBand",
    "ratingBand": "priceBand",
    "product": "attribute",
}


def _fake_vector(text: str) -> list[float]:
    """텍스트 → 결정론 8차원 벡터(sha256 바이트를 [-1,1] 로 스케일). 의미를 흉내 내지 않는다."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(byte - 127.5) / 127.5 for byte in digest[:_FAKE_DIM]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else -1.0


class FakeCatalog:
    """골든셋의 category accept 문자열 전부를 "사전"으로 삼는 결정론 벡터 공간.

    실 pg-catalog 접근 없이 `search_categories_pg`/`exact_lookup`/`embed_texts` 의 계약(정렬된
    (canonical, distance) 반환)만 흉내 낸다.
    """

    def __init__(self, categories: list[str]) -> None:
        self.categories = sorted(set(categories))
        self.vectors = {category: _fake_vector(category) for category in self.categories}

    def search(self, vec: list[float], dsn: str, *, k: int) -> list[tuple[str, float]]:  # noqa: ARG002
        scored = [(_cosine(vec, cvec), category) for category, cvec in self.vectors.items()]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(category, round(1.0 - similarity, 4)) for similarity, category in scored[:k]]

    def exact(self, values: list[str], dsn: str) -> set[str]:  # noqa: ARG002
        known = set(self.categories)
        return {value for value in values if value in known}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_fake_vector(text) for text in texts]


def build_fake_catalog(golden_set: GoldenSet) -> FakeCatalog:
    categories: list[str] = []
    for session in golden_set.sessions:
        for triple in session.expected_triples:
            if triple.kind == "category":
                categories.extend(triple.accept)
    return FakeCatalog(categories)


def _fabricated_delta(seed: int) -> dict[str, Any]:
    """어떤 기대 트리플과도 매칭되지 않는 오탐 델타 — 결정론(seed 로만 갈린다)."""
    return {
        "fact": f"조작된 오탐 취향 #{seed}",
        "kind": "brand",
        "label": f"존재하지않는브랜드{seed}",
        "anchorPhrase": "조작된 근거 구절",
        "polarity": "positive",
        "predicateHint": "likes",
        "salience": 0.9,
        "explicit": True,
        "repetitionEma": 0.0,
    }


def _correct_delta(
    session: GoldenSession, triple: Any, *, kind: str | None = None
) -> dict[str, Any]:
    """기대 트리플 1개를 정확히 재현하는 델타 — `ScriptedDeltaLLM`(정답 경로)과
    `NoiseFalsePositiveLLM`(§A-14, 비노이즈 세션은 정답을 그대로 낸다)이 공유한다.

    `kind` 를 별도로 받는 이유는 misclassify 시험이 kind 만 회전시키고 나머지(라벨·polarity)는
    그대로 두기 때문이다 — 공유 로직이 갈리면 두 가짜의 "정답"이 서로 다른 의미가 된다.
    """
    polarity = "negative" if triple.predicate == "avoids" else "positive"
    return {
        "fact": f"{session.session_id}:{triple.node_id}",
        "kind": kind or triple.kind,
        "label": triple.canonical_label,
        "anchorPhrase": triple.canonical_label,
        "polarity": polarity,
        "predicateHint": triple.predicate,
        "salience": 0.9,
        "explicit": True,
        "repetitionEma": 0.0,
    }


class ScriptedDeltaLLM:
    """골든셋의 기대 트리플에서 정답 델타를 만들어 돌려주되, 고정 주기로 결함을 섞는 가짜 클라이언트.

    세션은 **첫 turn 텍스트**로 식별한다(`user` 인자는 `"\\n".join(buffer)` 라 첫 줄이 첫 turn과
    같다) — `schema.GoldenSet._first_turns_are_unique` 가 이 전제를 강제한다.
    """

    def __init__(
        self,
        golden_set: GoldenSet,
        *,
        fail_first: int = 0,
        always_fail: bool = False,
        fail_kind: str = "generic",  # "generic" | "timeout"
        omit_every: int = 0,
        extra_every: int = 0,
        misclassify_every: int = 0,
        schema_violation_every: int = 0,
    ) -> None:
        self.golden_set = golden_set
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.fail_kind = fail_kind
        self.omit_every = omit_every
        self.extra_every = extra_every
        self.misclassify_every = misclassify_every
        self.schema_violation_every = schema_violation_every
        self.calls: list[dict[str, Any]] = []
        self._attempts = 0
        self._by_first_turn: dict[str, GoldenSession] = {
            session.turns[0]: session for session in golden_set.sessions
        }

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        self.calls.append({"system": system, "user": user, "tier": tier, "maxTokens": max_tokens})
        self._attempts += 1
        index = self._attempts
        if self.always_fail or index <= self.fail_first:
            if self.fail_kind == "timeout":
                try:
                    raise TimeoutError("scripted timeout")
                except TimeoutError as exc:
                    raise LLMError("scripted transport failure") from exc
            raise LLMError("scripted transport failure")
        if self.schema_violation_every and index % self.schema_violation_every == 0:
            return "이 응답에는 JSON 오브젝트가 없습니다(스키마 위반 시험)"
        first_turn = user.splitlines()[0] if user else ""
        session = self._by_first_turn.get(first_turn)
        if session is None:
            return json.dumps({"deltas": []}, ensure_ascii=False)
        deltas = self._session_deltas(session, index)
        return json.dumps({"deltas": deltas}, ensure_ascii=False)

    def _session_deltas(self, session: GoldenSession, index: int) -> list[dict[str, Any]]:
        deltas: list[dict[str, Any]] = []
        for triple in session.expected_triples:
            if self.omit_every and index % self.omit_every == 0:
                continue
            kind = triple.kind
            if self.misclassify_every and index % self.misclassify_every == 0:
                kind = _KIND_ROTATION[triple.kind]
            deltas.append(_correct_delta(session, triple, kind=kind))
        if self.extra_every and index % self.extra_every == 0:
            deltas.append(_fabricated_delta(index))
        return deltas

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        raise NotImplementedError("taste_probe 는 stream 을 쓰지 않는다")


class NoiseFalsePositiveLLM:
    """[A-14] **비노이즈 세션은 정답을 그대로 내고, noise 세션에만 조작 트리플을 더한다.**

    이래야 `noiseFalsePositiveRate` 를 `recall` 과 독립적으로 잴 수 있다 — 종전 버전(noise 아닌
    세션도 전부 생략)은 recall 이 0 으로 함께 움직여 "오탐 축만 오른다"는 것을 실측하지 못했다
    (리뷰 재현: recall 1→0). 이제는 recall==1.0 을 유지한 채 noiseFalsePositiveRate 만 오르는지
    확인할 수 있다.
    """

    def __init__(self, golden_set: GoldenSet) -> None:
        self._by_first_turn: dict[str, GoldenSession] = {
            session.turns[0]: session for session in golden_set.sessions
        }
        self._attempts = 0

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        self._attempts += 1
        first_turn = user.splitlines()[0] if user else ""
        session = self._by_first_turn.get(first_turn)
        if session is None:
            return json.dumps({"deltas": []}, ensure_ascii=False)
        if session.slice_id == "noise":
            return json.dumps({"deltas": [_fabricated_delta(self._attempts)]}, ensure_ascii=False)
        deltas = [_correct_delta(session, triple) for triple in session.expected_triples]
        return json.dumps({"deltas": deltas}, ensure_ascii=False)

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        raise NotImplementedError("taste_probe 는 stream 을 쓰지 않는다")
