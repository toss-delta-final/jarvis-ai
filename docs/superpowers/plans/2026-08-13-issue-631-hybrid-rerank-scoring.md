# Issue #631 Hybrid Rerank Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 구매자 rerank와 grounding 동작을 보존하면서, 선택 가능한 구조화 rubric 순위와 검색순위 RRF hybrid arm을 구현하고 재현 가능한 A/B/C 평가 도구를 제공한다.

**Architecture:** `rerank_scoring.py`를 LLM·Settings·graph에 의존하지 않는 순수 검증/순위 계산 경계로 추가한다. `rerank.py`는 `current`일 때 기존 prompt/parser를 그대로 사용하고, `structured|hybrid`일 때만 scored prompt와 순수 계산기를 사용한다. Production graph는 독립적인 ranking/grounding 설정을 전달하며 기본 ranking arm은 계속 `current`; 별도 `evals/rerank_scoring` runner가 current와 structured를 호출하고 hybrid는 structured raw 응답을 replay해 인과를 분리한다.

**Tech Stack:** Python 3.12, dataclasses, Pydantic Settings, pytest/pytest-asyncio, Ruff, SciPy(기존 dependency), uv, pre-commit.

## Global Constraints

- `RERANK_RANKING_ARM` 기본값과 `rerank()` 직접 호출 기본값은 모두 `current`다.
- 기존 `RERANK_GROUNDING_ARM=current|prompt_only|validated`와 #632/#657 validator/template/overall claim 계약을 변경하지 않는다.
- `current` ranking arm은 scored prompt나 scored parser를 통과하지 않는다.
- component 범위는 `intentFit 0..4`, `needFit 0..3`, `profileFit 0..1`; rubric은 `4:2:1`이다.
- 프로필이 없으면 `profileFit=0`만 유효하며 개인화는 현재 발화를 뒤집지 않는 tie-break다.
- RRF 기본값은 `alpha=0.65`, `k=60`; config가 `0 <= alpha <= 1`, `k > 0`을 검증한다.
- grounding 오류는 상품이나 순위를 제거하지 않고 기존 정책대로 rationale만 강등한다.
- 외부 SSE/Spring push schema와 모델·score 비노출 계약을 유지한다.
- 새 dependency를 추가하지 않는다.
- 각 커밋은 Conventional Commit 제목과 Lore trailers를 함께 사용하며 `--no-verify`를 사용하지 않는다.
- 구현 worktree에서 `uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg`를 실행한다.
- 최종 검증은 targeted tests → 관련 회귀 → Ruff → pre-commit → diff check 순으로 fresh output을 확인한다.

## Execution Setup

구현 시작 전에 Orca native worktree에서 다음 상태를 만든다.

```text
base: latest origin/dev
branch: feat/631-hybrid-rerank-scoring
carried commits: 05633b08 (design spec), 이 구현 계획 문서 commit
```

새 worktree에서 `uv sync`, pre-commit hook 설치 후 아래 baseline을 먼저 실행한다.

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_rerank_grounding.py -q
uv run pre-commit run --files \
  docs/superpowers/specs/2026-08-13-issue-631-hybrid-rerank-scoring-design.md \
  docs/superpowers/plans/2026-08-13-issue-631-hybrid-rerank-scoring.md
```

baseline 실패가 feature 변경 전부터 재현되면 해당 출력과 commit SHA를 기록하고 원인을 분리한다.
2026-08-13 baseline에서 repo 전체 `--all-files` 실행은 pinned hook Ruff 0.8.6이 `origin/dev`의 무관한
66개 Python 파일을 재포맷해 feature scope를 오염시켰다. 따라서 각 commit의 정상 hook은 유지하고,
수동 pre-commit 검증은 carryover/feature diff 파일에 한정한다.

---

### Task 1: Pure Structured Scoring and RRF Engine

**Files:**
- Create: `app/agents/buyer/recommendation/rerank_scoring.py`
- Create: `tests/unit/test_rerank_scoring.py`

**Interfaces:**
- Produces: `RankingArm = Literal["current", "structured", "hybrid"]`
- Produces: `ScoredRankingArm = Literal["structured", "hybrid"]`
- Produces: `ScoringSchemaError(ValueError)`
- Produces: `RankingDecision`
- Produces: `RankingComputation`
- Produces: `compute_scored_ranking(candidate_ids, raw_evaluations, *, arm, profile_available, alpha, k, search_rank_by_id=None) -> RankingComputation`
- Consumes: only standard-library collection/dataclass/typing APIs.

- [x] **Step 1: Write failing tests for valid rubric ordering and bounded personalization**

```python
def test_structured_uses_421_rubric_and_profile_only_breaks_equal_query_fit() -> None:
    result = compute_scored_ranking(
        [101, 102, 103],
        [
            {"productId": 101, "intentFit": 4, "needFit": 3, "profileFit": 0},
            {"productId": 102, "intentFit": 4, "needFit": 3, "profileFit": 1},
            {"productId": 103, "intentFit": 3, "needFit": 3, "profileFit": 1},
        ],
        arm="structured",
        profile_available=True,
        alpha=0.65,
        k=60,
    )

    assert result.ordered_product_ids == (102, 101, 103)
    assert [row.rubric_score for row in result.decisions] == [22, 23, 19]
```

- [x] **Step 2: Run the focused tests and confirm import failure**

Run: `uv run pytest tests/unit/test_rerank_scoring.py -q`

Expected: collection fails because `rerank_scoring` does not exist.

- [x] **Step 3: Add immutable decision types and exact validation helpers**

```python
RankingArm = Literal["current", "structured", "hybrid"]
ScoredRankingArm = Literal["structured", "hybrid"]


class ScoringSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class RankingDecision:
    product_id: int
    search_rank: int
    intent_fit: int | None
    need_fit: int | None
    profile_fit: int | None
    rubric_score: int | None
    llm_rank: int
    final_score: float | None
    final_rank: int
    score_valid: bool
    fallback_reason: str | None


@dataclass(frozen=True)
class RankingComputation:
    ordered_product_ids: tuple[int, ...]
    decisions: tuple[RankingDecision, ...]
    model_items_by_id: Mapping[int, Mapping[str, object]]
    foreign_evaluation_count: int
    duplicate_evaluation_count: int
    invalid_evaluation_count: int
```

Validation helpers must reject bool IDs/scores, duplicate candidate IDs, invalid arm, invalid alpha/k, out-of-range scores and non-list `raw_evaluations`. Candidate search ranks are 1-based.

- [x] **Step 4: Add failing tests for malformed and partial evaluations**

Cover these exact outcomes:

```python
@pytest.mark.parametrize("value", [True, False, -1, 5, 1.5, "4"])
def test_invalid_intent_fit_recovers_that_candidate_by_search_rank(value: object) -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            {"productId": 101, "intentFit": value, "needFit": 3, "profileFit": 0},
            {"productId": 102, "intentFit": 4, "needFit": 3, "profileFit": 0},
        ],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )
    by_id = {row.product_id: row for row in result.decisions}
    assert result.ordered_product_ids == (102, 101)
    assert by_id[101].score_valid is False
    assert by_id[101].fallback_reason == "invalid_intent_fit"

def test_duplicate_evaluation_invalidates_that_product_instead_of_trusting_first() -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            {"productId": 101, "intentFit": 4, "needFit": 3, "profileFit": 0},
            {"productId": 101, "intentFit": 0, "needFit": 0, "profileFit": 0},
            {"productId": 102, "intentFit": 3, "needFit": 3, "profileFit": 0},
        ],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )
    by_id = {row.product_id: row for row in result.decisions}
    assert result.ordered_product_ids == (102, 101)
    assert by_id[101].fallback_reason == "duplicate_evaluation"
    assert 101 not in result.model_items_by_id

def test_missing_candidate_is_appended_in_search_order() -> None:
    result = compute_scored_ranking(
        [101, 102, 103],
        [{"productId": 102, "intentFit": 4, "needFit": 3, "profileFit": 0}],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )
    assert result.ordered_product_ids == (102, 101, 103)
    assert [row.fallback_reason for row in result.decisions] == ["missing_evaluation", None, "missing_evaluation"]

def test_out_of_candidate_id_is_audited_but_never_returned() -> None:
    result = compute_scored_ranking(
        [101],
        [
            {"productId": 999, "intentFit": 4, "needFit": 3, "profileFit": 0},
            {"productId": 101, "intentFit": 4, "needFit": 3, "profileFit": 0},
        ],
        arm="structured",
        profile_available=False,
        alpha=0.65,
        k=60,
    )
    assert result.ordered_product_ids == (101,)
    assert set(result.model_items_by_id) == {101}
    assert result.foreign_evaluation_count == 1

def test_all_invalid_evaluations_raise_schema_error() -> None:
    with pytest.raises(ScoringSchemaError, match="no valid evaluations"):
        compute_scored_ranking(
            [101],
            [{"productId": 101, "intentFit": 5, "needFit": 3, "profileFit": 0}],
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )

def test_profile_absence_requires_zero_profile_fit() -> None:
    with pytest.raises(ScoringSchemaError, match="no valid evaluations"):
        compute_scored_ranking(
            [101],
            [{"productId": 101, "intentFit": 4, "needFit": 3, "profileFit": 1}],
            arm="structured",
            profile_available=False,
            alpha=0.65,
            k=60,
        )
```

Assertions must inspect `score_valid`, `fallback_reason`, ordered IDs and the absence of foreign IDs.

- [x] **Step 5: Implement partial recovery and deterministic LLM ranks**

Use one occurrence-count pass before accepting evaluation rows. A duplicated product gets no trusted model item and a `duplicate_evaluation` fallback. Missing, duplicate and invalid-score products sort after all valid scored products by `searchRank`, then `productId`. If no valid score exists, raise `ScoringSchemaError("scored rerank has no valid evaluations")`.

- [x] **Step 6: Add failing RRF and explicit search-rank tests**

```python
def test_hybrid_combines_one_based_search_and_llm_ranks() -> None:
    result = compute_scored_ranking(
        [101, 102],
        [
            {"productId": 101, "intentFit": 0, "needFit": 0, "profileFit": 0},
            {"productId": 102, "intentFit": 4, "needFit": 3, "profileFit": 0},
        ],
        arm="hybrid",
        profile_available=False,
        alpha=0.65,
        k=60,
    )
    assert result.decisions[0].final_score == pytest.approx(
        (0.65 + 0.35 / 23) / 61 + (1 - (0.65 + 0.35 / 23)) / 62
    )


def test_prompt_permutation_does_not_change_explicit_search_ranks() -> None:
    result = compute_scored_ranking(
        [102, 101],
        [
            {"productId": 102, "intentFit": 4, "needFit": 3, "profileFit": 0},
            {"productId": 101, "intentFit": 4, "needFit": 3, "profileFit": 0},
        ],
        arm="hybrid",
        profile_available=False,
        alpha=0.65,
        k=60,
        search_rank_by_id={101: 1, 102: 2},
    )
    assert {row.product_id: row.search_rank for row in result.decisions} == {101: 1, 102: 2}
```

- [x] **Step 7: Implement RRF and final deterministic ordering**

For profile absence compute `effective_alpha = alpha + (1 - alpha) * (1 / 23)`. Final ordering is `-final_score`, `search_rank`, `product_id`. Structured ordering uses `llm_rank`. Return decisions in original candidate/search-rank order so diagnostics are stable; `ordered_product_ids` carries final order.

- [x] **Step 8: Run focused tests, Ruff and commit**

```bash
uv run pytest tests/unit/test_rerank_scoring.py -q
uv run ruff check app/agents/buyer/recommendation/rerank_scoring.py tests/unit/test_rerank_scoring.py
uv run ruff format --check app/agents/buyer/recommendation/rerank_scoring.py tests/unit/test_rerank_scoring.py
git add app/agents/buyer/recommendation/rerank_scoring.py tests/unit/test_rerank_scoring.py
git commit -m "feat(rerank): make structured ranking deterministic" \
  -m "Constraint: Profile influence must remain a one-point tie-break and invalid rows must recover through search rank
Rejected: Summing raw search and model scores | their scales are not calibrated
Confidence: high
Scope-risk: narrow
Directive: Keep this module free of LLM, Settings, and graph dependencies
Tested: uv run pytest tests/unit/test_rerank_scoring.py -q; targeted Ruff check and format
Not-tested: Production graph and live provider behavior"
```

---

### Task 2: Ranking Configuration and Internal Result Contract

**Files:**
- Modify: `app/core/config.py` near existing `rerank_grounding_arm`
- Modify: `app/agents/buyer/recommendation/state.py` at `RerankResult`
- Modify: `.env.example` near `RERANK_GROUNDING_ARM`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_rerank_scoring.py`

**Interfaces:**
- Consumes: `RankingArm`, `RankingDecision` from Task 1.
- Produces: `Settings.rerank_ranking_arm: RankingArm = "current"`
- Produces: `Settings.rerank_rrf_alpha: float = Field(default=0.65, ge=0.0, le=1.0)`
- Produces: `Settings.rerank_rrf_k: int = Field(default=60, gt=0)`
- Produces: `Settings.rerank_scoring_prompt_version = "rerank-scoring-v1"`
- Produces: `RerankResult.ranking_decisions: list[RankingDecision]`.

- [x] **Step 1: Write failing configuration tests**

```python
def test_rerank_ranking_defaults_to_current_and_validates_hybrid_config(monkeypatch) -> None:
    defaults = Settings(_env_file=None)
    assert defaults.rerank_ranking_arm == "current"
    assert defaults.rerank_rrf_alpha == 0.65
    assert defaults.rerank_rrf_k == 60
    assert defaults.rerank_scoring_prompt_version == "rerank-scoring-v1"

    monkeypatch.setenv("RERANK_RANKING_ARM", "hybrid")
    assert Settings(_env_file=None).rerank_ranking_arm == "hybrid"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_ranking_arm="unknown")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_rrf_alpha=1.01)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_rrf_k=0)
```

- [x] **Step 2: Run the test and confirm missing fields**

Run: `uv run pytest tests/unit/test_config.py::test_rerank_ranking_defaults_to_current_and_validates_hybrid_config -q`

Expected: FAIL because the Settings fields do not exist.

- [x] **Step 3: Add Settings, environment documentation and result field**

Add the four Settings fields next to grounding config, document the independent axes in `.env.example`, and import `RankingDecision` under `TYPE_CHECKING` in `state.py`.

```python
@dataclass
class RerankResult:
    ranked: list[tuple[int, str]] = field(default_factory=list)
    overall_comment: str = ""
    overall_claims: tuple[Mapping[str, object], ...] = ()
    grounding_decisions: list[GroundingDecision] = field(default_factory=list)
    ranking_decisions: list[RankingDecision] = field(default_factory=list)
```

- [x] **Step 4: Add a default-result regression test**

Assert `RerankResult().ranking_decisions == []` and existing fields remain unchanged.

- [x] **Step 5: Run tests, Ruff and commit**

```bash
uv run pytest tests/unit/test_config.py tests/unit/test_rerank_scoring.py -q
uv run ruff check app/core/config.py app/agents/buyer/recommendation/state.py tests/unit/test_config.py tests/unit/test_rerank_scoring.py
git add .env.example app/core/config.py app/agents/buyer/recommendation/state.py tests/unit/test_config.py tests/unit/test_rerank_scoring.py
git commit -m "feat(rerank): expose independent ranking controls" \
  -m "Constraint: Ranking rollout must not alter the validated grounding default or direct-call compatibility
Rejected: One combined rerank arm | it couples unrelated rollback boundaries
Confidence: high
Scope-risk: narrow
Directive: Keep RERANK_RANKING_ARM=current until paired evidence is approved
Tested: config and scoring unit tests; targeted Ruff check
Not-tested: Graph wiring and live evaluation"
```

---

### Task 3: Scored Prompt and Rerank Arm Integration

**Files:**
- Modify: `app/agents/buyer/recommendation/rerank.py`
- Create: `tests/unit/test_rerank_scoring_integration.py`
- Modify: `tests/unit/test_rerank_grounding.py`

**Interfaces:**
- Consumes: `compute_scored_ranking`, `RankingArm`, `ScoringSchemaError` from Task 1.
- Consumes: `RerankResult.ranking_decisions` from Task 2.
- Produces: `_SYSTEM_STRUCTURED_SCORING` with `evaluations`, existing grounding fields and overall claims.
- Extends the existing keyword-only `rerank` API with `ranking_arm: RankingArm = "current"`, `rrf_alpha: float = 0.65`, `rrf_k: int = 60`, and `search_rank_by_id: Mapping[int, int] | None = None`.

- [x] **Step 1: Lock the legacy current path before editing**

Add a fake LLM that captures `system`, `user`, and `max_tokens`, then assert:

```python
llm = _StructuredLLM(
    [{
        "productId": 101,
        "rationale": "모델 문장",
        "reasonCode": "RATING_HIGH",
        "evidenceFields": ["ratingLevel"],
    }]
)
result = await rerank(
    llm,
    query="q",
    candidates=[SpringProduct(product_id=101, name="p", rating=4.8, review_count=120)],
    profile_summary=None,
    tier="smart",
    expose_max=1,
    grounding_arm="validated",
    ranking_arm="current",
)
assert llm.systems == [_SYSTEM_STRUCTURED_GROUNDING]
assert result.ranked == [(101, "평점 평가가 높은 상품이에요")]
assert result.ranking_decisions == []
assert llm.max_tokens == settings.rerank_max_tokens_base + settings.rerank_max_tokens_per_item
```

This test must exercise an existing grounding arm so the new ranking branch cannot silently replace #632 prompt behavior.

- [x] **Step 2: Run legacy and new integration tests to establish red/green boundaries**

```bash
uv run pytest tests/unit/test_rerank_grounding.py -q
uv run pytest tests/unit/test_rerank_scoring_integration.py -q
```

Expected: existing grounding tests pass; new tests fail on missing `ranking_arm` and prompt.

- [x] **Step 3: Add the scored prompt contract**

The prompt must require exactly one evaluation for every candidate and declare integer ranges. It must retain current `reasonCode`, `evidenceFields`, `rationale`, `overallComment`, and `overallClaims` rules. When no profile is present it must explicitly require `profileFit=0`.

- [x] **Step 4: Add failing structured/hybrid response tests**

Cover:

```python
async def test_structured_ranks_all_candidates_from_valid_scores() -> None:
    payload = _scored_payload((101, 4, 3, 0), (102, 3, 3, 0))
    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)
    assert [product_id for product_id, _ in result.ranked] == [101, 102]


async def test_hybrid_uses_same_scored_schema_but_changes_only_code_order() -> None:
    payload = _scored_payload((101, 0, 0, 0), (102, 4, 3, 0))
    structured, structured_llm = await _call_scored(payload, ranking_arm="structured", expose_max=2)
    hybrid, hybrid_llm = await _call_scored(payload, ranking_arm="hybrid", expose_max=2)
    assert structured_llm.systems == hybrid_llm.systems == [_SYSTEM_STRUCTURED_SCORING]
    assert [row[0] for row in structured.ranked] == [102, 101]
    assert [row[0] for row in hybrid.ranked] == [101, 102]


async def test_scored_arm_token_budget_uses_candidate_count_not_expose_max() -> None:
    payload = _scored_payload((101, 4, 3, 0), (102, 3, 3, 0), (103, 2, 3, 0))
    _, llm = await _call_scored(payload, ranking_arm="structured", expose_max=1)
    settings = get_settings()
    assert llm.max_tokens == settings.rerank_max_tokens_base + settings.rerank_max_tokens_per_item * 3


async def test_scored_arm_all_invalid_converts_schema_error_to_llm_error() -> None:
    payload = _scored_payload((101, 5, 3, 0))
    with pytest.raises(LLMError, match="no valid evaluations"):
        await _call_scored(payload, ranking_arm="structured", expose_max=1)


async def test_scored_missing_candidate_recovers_with_empty_rationale() -> None:
    payload = _scored_payload((102, 4, 3, 0))
    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)
    assert result.ranked == [(102, "평점 평가가 높은 상품이에요"), (101, "")]


async def test_invalid_grounding_neutralizes_reason_without_changing_scored_order() -> None:
    payload = _scored_payload((102, 4, 3, 0), (101, 3, 3, 0), invalid_reason_for=102)
    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)
    assert [row[0] for row in result.ranked] == [102, 101]
    assert result.ranked[0][1] == NEUTRAL_RATIONALE


async def test_explicit_search_rank_survives_prompt_candidate_permutation() -> None:
    payload = _scored_payload((102, 4, 3, 0), (101, 4, 3, 0))
    result, _ = await _call_scored(
        payload,
        ranking_arm="hybrid",
        expose_max=2,
        candidate_ids=(102, 101),
        search_rank_by_id={101: 1, 102: 2},
    )
    assert [row[0] for row in result.ranked] == [101, 102]
```

In the same test file define `_scored_payload` to emit the full `evaluations`/grounding/overall shape and `_call_scored` to build `SpringProduct` objects for the supplied IDs, capture the fake LLM call and return `(RerankResult, fake_llm)`.

- [x] **Step 5: Implement scored parsing and rationale assembly**

Branch before prompt selection:

```python
if ranking_arm == "current":
    system = _SYSTEM if grounding_arm == "current" else _SYSTEM_STRUCTURED_GROUNDING
    output_item_count = expose_max
else:
    system = _SYSTEM_STRUCTURED_SCORING
    output_item_count = len(candidates)
```

After `extract_json`, current keeps the existing `ranked` loop unchanged. Scored arms call `compute_scored_ranking`; convert `ScoringSchemaError` to `LLMError`. Build `ranked` in computed order up to `expose_max`:

- unique model item + `grounding_arm=current`: model rationale;
- unique model item + `prompt_only|validated`: existing validator decision;
- missing/duplicate model item: empty rationale and no grounding decision;
- never synthesize a foreign candidate.

Return computation decisions through `RerankResult.ranking_decisions`.

- [x] **Step 6: Preserve overall claims and final-list validation input**

For scored prompt with `grounding_arm=prompt_only|validated`, preserve raw `overallClaims` with existing `_parse_overall_claims`. `grounding_arm=current` continues to omit structured claims. Assert malformed claims still reach the #657 validator exactly as current structured grounding does.

- [x] **Step 7: Run focused regression, Ruff and commit**

```bash
uv run pytest tests/unit/test_rerank_grounding.py tests/unit/test_rerank_scoring.py tests/unit/test_rerank_scoring_integration.py -q
uv run ruff check app/agents/buyer/recommendation/rerank.py tests/unit/test_rerank_grounding.py tests/unit/test_rerank_scoring_integration.py
uv run ruff format --check app/agents/buyer/recommendation/rerank.py tests/unit/test_rerank_grounding.py tests/unit/test_rerank_scoring_integration.py
git add app/agents/buyer/recommendation/rerank.py tests/unit/test_rerank_grounding.py tests/unit/test_rerank_scoring_integration.py
git commit -m "feat(rerank): add scored and hybrid execution arms" \
  -m "Constraint: Current ranking and all validated grounding behavior must remain byte-for-byte compatible at their prompt boundary
Rejected: Replacing ranked output in place | it would make rollback and causal comparison unreliable
Confidence: high
Scope-risk: moderate
Directive: Scored arms must evaluate every candidate and never use grounding failure to change rank
Tested: rerank scoring, grounding, and integration unit tests; targeted Ruff check and format
Not-tested: Production graph selection and full goldenset evaluation"
```

---

### Task 4: Production Graph Wiring, Rollback and Provenance

**Files:**
- Modify: `app/agents/buyer/recommendation/graph.py` at the main `rerank()` call and provenance prompt version selection
- Modify: `tests/unit/test_fanout.py` near `test_production_graph_passes_validated_grounding_arm`
- Modify: `tests/unit/test_reco_provenance_140.py`
- Modify: `tests/unit/test_recommendation.py` for fallback/wire regressions

**Interfaces:**
- Consumes: Task 2 Settings fields.
- Produces: graph call arguments `ranking_arm`, `rrf_alpha`, `rrf_k`.
- Produces: prompt provenance mapping: legacy current/current=`rerank-v1`; current/structured-grounding=`rerank-grounding-v1`; scored ranking=`rerank-scoring-v1`; degraded=`None`.

- [x] **Step 1: Write failing graph wiring tests**

Patch graph-level `rerank` and capture kwargs:

```python
assert observed == [{
    "grounding_arm": "validated",
    "ranking_arm": "current",
    "rrf_alpha": 0.65,
    "rrf_k": 60,
}]
```

Parametrize settings with `structured` and `hybrid` to prove production can select each arm without changing grounding.

- [x] **Step 2: Add prompt provenance tests for all boundaries**

Parametrize:

```python
(
    ("current", "current", "rerank-v1"),
    ("current", "validated", "rerank-grounding-v1"),
    ("structured", "validated", "rerank-scoring-v1"),
    ("hybrid", "validated", "rerank-scoring-v1"),
)
```

Keep degraded rerank `promptVersion is None`.

- [x] **Step 3: Wire Settings and trace metadata**

Pass all four arm/tuner values explicitly at the graph boundary. Add `rankingArm` to the rerank trace span attributes without adding it to the external response. Refactor prompt version selection into a small pure helper if the nested expression would otherwise grow.

- [x] **Step 4: Lock full and partial fallback behavior**

Add graph-level tests proving:

- scored all-invalid `LLMError` uses existing search-order degrade notice;
- current fallback output is unchanged;
- partial scored recovery does not mark the whole turn degraded;
- final pushed IDs remain a subset of candidate IDs;
- grounding-invalid rationale does not alter pushed IDs.

- [x] **Step 5: Run graph/provenance regressions and commit**

```bash
uv run pytest tests/unit/test_fanout.py tests/unit/test_reco_provenance_140.py tests/unit/test_recommendation.py -q
uv run ruff check app/agents/buyer/recommendation/graph.py tests/unit/test_fanout.py tests/unit/test_reco_provenance_140.py tests/unit/test_recommendation.py
git add app/agents/buyer/recommendation/graph.py tests/unit/test_fanout.py tests/unit/test_reco_provenance_140.py tests/unit/test_recommendation.py
git commit -m "feat(rerank): wire ranking arms without changing the default" \
  -m "Constraint: Production must be able to select hybrid while current remains the startup and rollback default
Rejected: Reading Settings inside the pure ranker | it hides rollout inputs and weakens tests
Confidence: high
Scope-risk: moderate
Directive: Preserve degraded provenance as null and keep scoring diagnostics internal
Tested: fanout, recommendation, and provenance unit suites; targeted Ruff check
Not-tested: Live model quality and full repository regression"
```

---

### Task 5: Reproducible Paired A/B/C Runner

**Files:**
- Create: `evals/rerank_scoring/__init__.py`
- Create: `evals/rerank_scoring/schema.py`
- Create: `evals/rerank_scoring/runner.py`
- Create: `evals/rerank_scoring/fakes.py`
- Create: `tests/eval/test_rerank_scoring_runner.py`

**Interfaces:**
- Consumes: goldenset v2.3 loader and `EvaluationFixtures`.
- Consumes: existing hard-filter helper, profile fixture derivation/markdown renderer, `rerank()` and metrics helpers.
- Produces: `RankingSample`, `RankingFailure`, `CaseArmResult`, `RankingProbeRun` dataclasses.
- Produces: `build_case_input(case: GoldenCase, fixtures: EvaluationFixtures) -> RankingCaseInput`.
- Produces: `run_case_arms(case_input: RankingCaseInput, llm: LLMClient, *, arms: tuple[str, ...], grounding_arm: GroundingArm, expose_max: int, order_seed: int) -> dict[str, CaseArmResult]`.
- Produces: `run_probe(llm: LLMClient, *, cases: tuple[GoldenCase, ...], fixtures: EvaluationFixtures, arms: tuple[str, ...], repeats: int, attempt_multiplier: int, order_seeds: tuple[int, ...]) -> RankingProbeRun`.
- Produces: `ReplayLLM` that returns captured structured raw output without provider calls.

- [x] **Step 1: Write failing fixture conversion tests**

For a goldenset case, assert the runner:

- reads candidates in `search_responses[searchFixtureId].productIds` order;
- converts catalog rows to `SpringProduct`;
- applies existing hard constraints before rerank;
- derives no profile for guests;
- derives case-specific profile markdown for member personas;
- records original `searchRank` before candidate prompt permutation.

- [x] **Step 2: Implement read-only case input construction**

Create `RankingCaseInput` with exact fields:

```python
@dataclass(frozen=True)
class RankingCaseInput:
    case_id: str
    query: str
    candidates: tuple[SpringProduct, ...]
    search_rank_by_id: Mapping[int, int]
    profile_summary: str | None
    relevance_grades: Mapping[int, int]
    hard_constraints: Mapping[str, object]
    must_exclude_product_ids: tuple[int, ...]
    slices: tuple[str, ...]
```

Do not open sealed holdout labels in the default runner; default split is `dev`.

- [x] **Step 3: Write failing B/C raw-sharing tests**

Use a counting fake provider and assert one scored provider call produces both arms:

```python
case_input = build_case_input(cases[0], fixtures)
result = await run_case_arms(
    case_input,
    provider,
    arms=("structured", "hybrid"),
    grounding_arm="validated",
    expose_max=9,
    order_seed=7,
)
assert provider.scored_calls == 1
assert result["structured"].raw_response_sha256 == result["hybrid"].raw_response_sha256
assert result["structured"].provider_called is True
assert result["hybrid"].provider_called is False
```

The hybrid arm must call `rerank()` with `ReplayLLM(raw)` and the same `search_rank_by_id`, not duplicate ranking logic in eval code.

- [x] **Step 4: Add deterministic current/structured/hybrid samples**

`ScriptedScoringLLM` must distinguish the legacy/current prompt from scored prompt and return valid existing grounding fields. It must expose a switch for duplicate, missing, out-of-range, and out-of-candidate rows so failure metrics are testable without network calls.

- [x] **Step 5: Add permutation and stability tests**

For seeds `(11, 29, 47)` assert:

- same seed produces the same prompt candidate order;
- different seeds alter prompt order when candidate count permits;
- explicit `searchRank` remains unchanged;
- each sample records `rankedProductIds`, top-3 IDs, top-1, latency, raw hash, decisions and fallback counts.

- [x] **Step 6: Implement bounded attempts and failure separation**

Follow `evals/rerank_grounding/runner.py`: requested successful repeats are filled up to `attempt_multiplier`; provider/parser failures are stored separately and never converted to zero-quality samples. Record candidate order seed, case ID, arm, repeat, attempt and failure type.

- [x] **Step 7: Run eval-runner tests, Ruff and commit**

```bash
uv run pytest tests/eval/test_rerank_scoring_runner.py -q
uv run ruff check evals/rerank_scoring tests/eval/test_rerank_scoring_runner.py
uv run ruff format --check evals/rerank_scoring tests/eval/test_rerank_scoring_runner.py
git add evals/rerank_scoring tests/eval/test_rerank_scoring_runner.py
git commit -m "feat(eval): isolate rerank scoring arms on shared responses" \
  -m "Constraint: Structured and hybrid comparisons must differ only in local fusion, not provider resampling
Rejected: Calling the provider independently for every arm | it confounds RRF with model variance
Confidence: high
Scope-risk: moderate
Directive: Preserve original search ranks when permuting prompt candidate order
Tested: deterministic rerank scoring runner tests; targeted Ruff check and format
Not-tested: Live provider execution and report artifact generation"
```

---

### Task 6: Metrics, Artifacts and CLI

**Files:**
- Create: `evals/rerank_scoring/metrics.py`
- Create: `evals/rerank_scoring/report.py`
- Create: `evals/rerank_scoring/cli.py`
- Create: `evals/rerank_scoring/__main__.py`
- Create: `evals/rerank_scoring/README.md`
- Modify: `tests/eval/test_rerank_scoring_runner.py`

**Interfaces:**
- Consumes: `RankingProbeRun` from Task 5.
- Reuses: `ndcg_at_k`, `hard_constraint_violations`, `bootstrap_mean_ci`.
- Produces: raw `samples.csv`, `failures.csv`, `results.json`, `run_manifest.json`, `report.md`.
- Produces: `main(argv: list[str] | None = None) -> int`.

- [x] **Step 1: Write failing metric tests with explicit numerators and denominators**

Cover:

```python
def test_paired_ndcg_delta_uses_only_shared_valid_case_ids() -> None:
    report = score_run(_probe_run_with_case_scores(current={"a": 0.5, "b": 0.2}, hybrid={"a": 0.7}))
    comparison = report["comparisons"]["currentToHybrid"]
    assert comparison["pairedCaseIds"] == ["a"]
    assert comparison["pairedCount"] == 1
    assert comparison["meanDelta"] == pytest.approx(0.2)


def test_ci_crossing_zero_is_inconclusive() -> None:
    report = score_run(_probe_run_with_case_scores(current={"a": 0.6, "b": 0.6}, hybrid={"a": 0.7, "b": 0.5}))
    assert report["comparisons"]["currentToHybrid"]["verdict"] == "inconclusive"


def test_hard_constraint_violation_forces_regressed_verdict() -> None:
    run = _probe_run_with_case_scores(current={"a": 0.5, "b": 0.5}, hybrid={"a": 0.8, "b": 0.8})
    run = replace(run, samples=tuple(replace(sample, hard_constraint_violation_count=1) if sample.arm == "hybrid" else sample for sample in run.samples))
    assert score_run(run)["comparisons"]["currentToHybrid"]["verdict"] == "regressed"


def test_top3_jaccard_top1_agreement_and_spearman_are_grouped_by_case_arm() -> None:
    report = score_run(_probe_run_for_rankings((101, 102, 103), (101, 103, 102)))
    stability = report["stability"]["hybrid"]
    assert stability["top3Jaccard"] == 1.0
    assert stability["top1Agreement"] == 1.0
    assert stability["spearman"] == pytest.approx(0.5)


def test_invalid_partial_and_full_fallback_rates_have_explicit_denominators() -> None:
    report = score_run(_probe_run_for_fallbacks(partial=1, full=1, total=4))
    integrity = report["integrity"]["hybrid"]
    assert integrity["partialFallback"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert integrity["fullFallback"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
```

The test module defines `_probe_run_with_case_scores`, `_probe_run_for_rankings`, and `_probe_run_for_fallbacks` as concrete `RankingProbeRun` fixture builders using the Task 5 dataclasses; those builders set all unrelated counts to zero and latency/token/cost to explicit values rather than omitting fields.

Use SciPy only through the already installed dependency. If fewer than two common ranked IDs exist, rank correlation is `None` with an explicit denominator reason.

- [x] **Step 2: Implement A/B/C and stability metrics**

Primary comparison is A→C nDCG@10 paired case delta with fixed seed, 2000 bootstrap resamples and 95% confidence. Also emit A→B and B→C. Verdict values are exactly `supported|inconclusive|regressed|not-tested`.

Safety output includes counts/rates for hard constraint violations, foreign IDs, duplicate IDs, invalid score rows, evaluated coverage, partial fallback and full fallback. Efficiency output includes p50/p95 latency, input/output tokens and cost or a non-empty unknown reason.

- [x] **Step 3: Write failing artifact regeneration tests**

Assert a scripted run writes exactly the five artifact files, report values can be reconstructed from `samples.csv` and `failures.csv`, and manifest includes:

```text
gitCommit, dirty, command, datasetVersion, datasetHash,
promptHashes, modelConfig, repeats, orderSeeds,
alpha, k, componentWeights, groundingArm, budget
```

Mixed dataset hash, prompt hash or model config must raise `ValueError` before comparison.

- [x] **Step 4: Implement report and immutable output behavior**

Refuse an existing output directory. Serialize JSON with sorted keys and stable newlines. Preserve raw provider response hashes and decision rows, but do not write secrets or unsanitized profile text. README must document dry-run, live-run, case filtering, budget and reproduction commands.

- [x] **Step 5: Add CLI dry-run and argument validation tests**

Support:

```text
--arms current,structured,hybrid|all
--split dev
--case-ids comma,separated
--repeats positive-int
--attempt-multiplier positive-int
--order-seeds comma,separated
--alpha 0..1
--k positive-int
--dry-run
--out new-directory
```

Dry-run uses `ScriptedScoringLLM` and emits `not-tested`. Live mode uses the repository LLM factory/RecordingLLM and budget controls already used by model eval; credentials or usage absence must be reported, not replaced with zero.

- [x] **Step 6: Run eval tests, scripted smoke and commit**

```bash
uv run pytest tests/eval/test_rerank_scoring_runner.py -q
out="$(mktemp -d)/rerank-scoring-smoke"
uv run python -m evals.rerank_scoring --arms all --repeats 1 --order-seeds 11,29,47 --dry-run --out "$out"
test -f "$out/results.json" && test -f "$out/run_manifest.json" && test -f "$out/report.md"
uv run ruff check evals/rerank_scoring tests/eval/test_rerank_scoring_runner.py
git add evals/rerank_scoring tests/eval/test_rerank_scoring_runner.py
git commit -m "feat(eval): report paired rerank quality and stability" \
  -m "Constraint: Claims must be reproducible from raw paired samples with explicit denominators and immutable hashes
Rejected: Reporting arm averages without case pairing | it hides missing and failed runs
Confidence: high
Scope-risk: moderate
Directive: Treat dry-run and credential-blocked live runs as not-tested, never supported
Tested: eval runner tests, scripted three-seed smoke, targeted Ruff check
Not-tested: Full live goldenset run"
```

---

### Task 7: Changelog, Full Verification and Commit Hygiene

**Files:**
- Modify: `CHANGELOG.md` under `[Unreleased]`
- Modify: `docs/superpowers/specs/2026-08-13-issue-631-hybrid-rerank-scoring-design.md` only if implementation names differ from the approved interfaces
- Modify: `docs/superpowers/plans/2026-08-13-issue-631-hybrid-rerank-scoring.md` only to check completed boxes or correct verified commands

**Interfaces:**
- Consumes: all prior tasks.
- Produces: final evidence summary and a clean feature branch.

- [x] **Step 1: Add an evidence-bounded changelog entry**

State that ranking arms are selectable, default remains `current`, grounding remains independent, hybrid uses config-backed RRF and the evaluation harness exists. Do not claim quality improvement unless a live run with CI supports it.

- [x] **Step 2: Run the targeted feature suite**

```bash
uv run pytest \
  tests/unit/test_config.py \
  tests/unit/test_rerank_scoring.py \
  tests/unit/test_rerank_scoring_integration.py \
  tests/unit/test_rerank_grounding.py \
  tests/unit/test_fanout.py \
  tests/unit/test_reco_provenance_140.py \
  tests/unit/test_recommendation.py \
  tests/eval/test_rerank_scoring_runner.py -q
```

- [x] **Step 3: Run repository static checks and pre-commit exactly as installed**

```bash
uv run ruff check .
uv run ruff format --check .
git diff --name-only -z origin/dev...HEAD | xargs -0 uv run pre-commit run --files
git diff --check
```

Verification (2026-08-13): `ruff check .` and diff-scoped pre-commit passed. Repository-wide
`ruff format --check .` reported 57 pre-existing `origin/dev` files; none is in this feature's
changed Python set, whose `ruff-format` hook passed. The unrelated baseline files were not rewritten.

Read all outputs. If a hook auto-fixes a file, rerun the affected tests and the hook before committing.

- [x] **Step 4: Run the broader regression suite**

```bash
uv run pytest -q
```

Verification (2026-08-13): `7332 passed, 229 deselected, 2 warnings in 119.68s`.

Post-implementation live smoke (6 budget MFT cases, one seed) exposed a common scored-arm
`LengthFinishReasonError`: the 2,760-token output cap was consumed entirely by reasoning before JSON.
The same failing case succeeded with a 4,096-token scored-only reserve, while the current-arm budget
remained unchanged. Initial `alpha=0.65` quality remains experimental; the smoke is not a release gate.

Full dev screening then ran all 68 eligible MFT cases with seeds `11,29,47` on clean commit
`aa8f85b0`. Structured improved mean case-level nDCG@10 by `+0.1257` with bootstrap 95% CI
`[+0.0801,+0.1702]`; hybrid `alpha=0.65,k=60` regressed by `-0.2470` with CI
`[-0.3410,-0.1499]`. The immutable raw samples and manifest are preserved under
`evals/rerank_scoring/baselines/20260813-dev-mft68-live-n3/`. This promotes structured only to a
sealed-holdout candidate and rejects the initial hybrid setting; it does not switch production.

The frozen structured candidate was then evaluated once through `unseal_holdout_labels()` at commit
`a01dae74`. The 19 ranking-capable holdout cases produced mean ΔnDCG@10 `+0.0575` with 95% CI
`[-0.0385,+0.1696]` (8 improved / 6 tied / 5 regressed), so the release verdict is `inconclusive` and
production remains `current`. Aggregate evidence is under
`evals/rerank_scoring/releases/20260813-holdout-structured-n3/`; raw relevance rows were not duplicated.

If environment-dependent auth/PostgreSQL tests or a known hang recur, record exact node IDs and fresh output; do not count them as passed and do not hide them behind deselection unless the repository's documented command already specifies that selection.

- [x] **Step 5: Inspect branch scope and commit final docs**

```bash
git status --short
git diff --stat origin/dev...HEAD
git log --format=fuller --decorate --oneline origin/dev..HEAD
git diff --check origin/dev...HEAD
git add CHANGELOG.md docs/superpowers/specs/2026-08-13-issue-631-hybrid-rerank-scoring-design.md docs/superpowers/plans/2026-08-13-issue-631-hybrid-rerank-scoring.md
git commit -m "docs(rerank): record the selectable scoring experiment" \
  -m "Constraint: Documentation must distinguish implementation availability from measured production improvement
Rejected: Switching the default in the feature commit | live paired evidence is a separate release decision
Confidence: high
Scope-risk: narrow
Directive: Keep ranking current by default until the documented release gates pass
Tested: targeted feature suite, Ruff, pre-commit, diff check, and broader pytest result recorded in the final report
Not-tested: Production traffic and sealed holdout unless separately reported"
```

- [x] **Step 6: Re-run final status checks after the commit**

```bash
git status --short --branch
git diff --name-only -z origin/dev...HEAD | xargs -0 uv run pre-commit run --files
git diff --check origin/dev...HEAD
```

The worktree must be clean. Final report must list worktree path, branch, commits, changed files, test counts, skipped/failed environment checks, dry/live evaluation status and remaining risk that initial weights are experimental.

## Plan Self-Review

- Spec coverage maps to Tasks 1–7: pure scoring, config, rerank, graph, paired runner, metrics/artifacts, rollout verification.
- Current-path compatibility and independent grounding are tested before and after integration.
- `search_rank_by_id` makes prompt permutation independent from retrieval rank as required by the design.
- B/C provider-response sharing is implemented through the production `rerank()` API and ReplayLLM, not duplicated eval ranking code.
- All task interfaces use the same names: `RankingArm`, `RankingDecision`, `RankingComputation`, `compute_scored_ranking`.
- No new package is required; SciPy is already declared in `pyproject.toml`.
- Every code task begins with a failing test, runs a focused green check and ends with a verified Conventional+Lore commit.
- Production default switching is absent from implementation tasks and remains a later evidence-gated decision.
