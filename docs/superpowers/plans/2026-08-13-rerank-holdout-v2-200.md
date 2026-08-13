# Rerank Prospective Holdout v2 (200 Cases) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate, audit, and integrate a catalog-derived prospective rerank dataset containing exactly 200 independent ranking cases plus 24 separately counted safety cases.

**Architecture:** A new `evals.rerank_holdout_v2` package owns its schema, deterministic generator, validation, annotation/release gate, loader, and rerank adapter. It references the existing catalog snapshot by pinned hash without changing `evals.goldenset`, stores visible label-free cores separately from heuristic draft labels, and blocks confirmatory execution until genuine dual human review and adjudication are sealed.

**Tech Stack:** Python 3.12, Pydantic v2, standard-library JSON/hash/random/CSV, pytest, Ruff, pre-commit.

## Global Constraints

- Produce exactly 200 ranking cases and exactly 24 safety cases.
- Preserve an exact 100 guest / 100 member ranking split.
- Use stratum quotas `48/40/48/24/24/16` for `general/budget_multi/personalization/repurchase/long_tail/adversarial`.
- Use exactly 30 distinct catalog candidates per ranking case.
- Read `evals/goldenset/cases/buyer_dev.jsonl` and `buyer_holdout.jsonl` only for leakage checks; never read `buyer_holdout_labels.jsonl` during generation or draft validation.
- Keep heuristic labels marked `draft` and ineligible for confirmatory live evaluation.
- Keep the existing `evals.goldenset` artifacts, dataset version, and hash unchanged.
- Add no dependencies.
- Follow TDD: observe each new behavior fail before implementation.
- Commit through installed pre-commit hooks with Conventional Commit headers and Lore trailers.

---

## File map

- `evals/rerank_holdout_v2/schema.py`: Pydantic wire models and label-policy invariants.
- `evals/rerank_holdout_v2/io.py`: stable JSONL/JSON serialization, hashing, and dataset loading.
- `evals/rerank_holdout_v2/catalog.py`: normalized catalog records and deterministic indexes.
- `evals/rerank_holdout_v2/generator.py`: quota planner, ranking cases, draft labels, and safety cases.
- `evals/rerank_holdout_v2/validation.py`: count, quota, referential, label, constraint, and leakage audit.
- `evals/rerank_holdout_v2/annotation.py`: blind reviewer packets and review import validation.
- `evals/rerank_holdout_v2/release.py`: dual-review agreement/adjudication and sealed-release gate.
- `evals/rerank_holdout_v2/adapter.py`: conversion into existing `RankingCaseInput` values.
- `evals/rerank_holdout_v2/cli.py`, `__main__.py`: generate, audit, packet, and seal commands.
- `evals/rerank_holdout_v2/dataset/cases/*.jsonl`: committed label-free ranking and safety cores.
- `evals/rerank_holdout_v2/dataset/annotations/draft_labels.jsonl`: committed heuristic proposals.
- `evals/rerank_holdout_v2/dataset/audit/report.json`: machine-readable validation evidence.
- `evals/rerank_holdout_v2/dataset/manifest.json`: hashes, counts, quotas, status, and eligibility.
- `evals/rerank_holdout_v2/README.md`: provenance, generation, annotation, and evaluation rules.
- `evals/rerank_scoring/runner.py`: input-oriented probe loop preserving the legacy wrapper.
- `evals/rerank_scoring/cli.py`: explicit dataset selection and release-policy preflight.
- `tests/eval/test_rerank_holdout_v2_schema.py`: schema and IO contracts.
- `tests/eval/test_rerank_holdout_v2_generation.py`: generation and deterministic quota contracts.
- `tests/eval/test_rerank_holdout_v2_validation.py`: leakage and audit contracts.
- `tests/eval/test_rerank_holdout_v2_release.py`: blind review and sealing contracts.
- `tests/eval/test_rerank_holdout_v2_adapter.py`: rerank boundary and CLI policy contracts.

---

### Task 1: Schema and stable dataset IO

**Files:**
- Create: `evals/rerank_holdout_v2/__init__.py`
- Create: `evals/rerank_holdout_v2/schema.py`
- Create: `evals/rerank_holdout_v2/io.py`
- Test: `tests/eval/test_rerank_holdout_v2_schema.py`

**Interfaces:**
- Produces: `RankingCaseCore`, `DraftLabels`, `SealedLabels`, `SafetyCase`, `DatasetManifest`, `LabelPolicy`.
- Produces: `load_jsonl(path, model)`, `write_jsonl(path, rows)`, `sha256_file(path)`, and `load_dataset(root, label_policy)`.
- Consumes later: generator, validator, release gate, and adapter use the models without importing legacy goldenset schema literals.

- [ ] **Step 1: Write failing schema and IO tests**

```python
def test_ranking_core_requires_exactly_thirty_distinct_candidates():
    payload = ranking_core_payload(candidateProductIds=list(range(29)))
    with pytest.raises(ValueError, match="exactly 30"):
        RankingCaseCore.model_validate(payload)


def test_ranking_core_rejects_embedded_labels():
    payload = ranking_core_payload(relevanceGrades={"1": 3})
    with pytest.raises(ValueError, match="label fields"):
        RankingCaseCore.model_validate(payload)


def test_confirmatory_loader_rejects_draft_labels(tmp_path: Path):
    root = write_minimal_dataset(tmp_path, label_status="draft")
    with pytest.raises(ValueError, match="sealed labels required"):
        load_dataset(root, label_policy="sealed")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_schema.py -q`

Expected: collection fails because `evals.rerank_holdout_v2.schema` does not exist.

- [ ] **Step 3: Implement models and stable IO**

Use these exact public types and policies:

```python
LabelPolicy = Literal["none", "draft", "sealed"]
Stratum = Literal[
    "general", "budget_multi", "personalization", "repurchase", "long_tail", "adversarial"
]


class RankingCaseCore(CamelModel):
    case_id: str
    family_id: str
    schema_version: Literal["1.0.0"]
    dataset_version: Literal["1.0.0"]
    split: Literal["prospective_holdout"]
    stratum: Stratum
    variant: str
    slices: list[str]
    query: str
    identity: Identity
    profile_summary: str | None
    candidate_product_ids: list[int]
    candidate_provenance: dict[int, CandidateOrigin]
    catalog_sha256: str
    provenance: Literal["synthetic-catalog-derived"]


class LoadedDataset(NamedTuple):
    manifest: DatasetManifest
    ranking_cases: tuple[RankingCaseCore, ...]
    labels_by_case: Mapping[str, DraftLabels | SealedLabels]
    safety_cases: tuple[SafetyCase, ...]
    catalog: Mapping[str, dict[str, object]]
```

`write_jsonl` must sort rows by `caseId`, serialize aliases with sorted keys, and end every row with
one newline. `load_dataset` must verify every manifest file hash and the pinned catalog hash before
returning any rows.

- [ ] **Step 4: Run schema tests and verify GREEN**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_schema.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit schema boundary**

```bash
git add evals/rerank_holdout_v2 tests/eval/test_rerank_holdout_v2_schema.py
git commit -m "feat(eval): isolate prospective rerank dataset contracts" \
  -m "Constraint: Prior goldenset hashes must remain unchanged\nConfidence: high\nScope-risk: narrow\nTested: rerank holdout v2 schema and IO tests"
```

---

### Task 2: Deterministic catalog-derived case generation

**Files:**
- Create: `evals/rerank_holdout_v2/catalog.py`
- Create: `evals/rerank_holdout_v2/generator.py`
- Test: `tests/eval/test_rerank_holdout_v2_generation.py`

**Interfaces:**
- Consumes: `RankingCaseCore`, `DraftLabels`, `SafetyCase` from Task 1.
- Produces: `CatalogIndex.from_snapshot(catalog)`, `GenerationBundle`, and `generate_bundle(catalog, *, seed=631200)`.
- `GenerationBundle` contains immutable tuples `ranking_cases`, `draft_labels`, and `safety_cases`.

- [ ] **Step 1: Write failing count, quota, candidate, and determinism tests**

```python
@dataclass(frozen=True)
class GenerationBundle:
    ranking_cases: tuple[RankingCaseCore, ...]
    draft_labels: tuple[DraftLabels, ...]
    safety_cases: tuple[SafetyCase, ...]


def test_generation_hits_the_registered_200_case_design(catalog):
    bundle = generate_bundle(catalog, seed=631200)
    assert len(bundle.ranking_cases) == 200
    assert Counter(case.stratum for case in bundle.ranking_cases) == {
        "general": 48,
        "budget_multi": 40,
        "personalization": 48,
        "repurchase": 24,
        "long_tail": 24,
        "adversarial": 16,
    }
    assert Counter(case.identity.kind for case in bundle.ranking_cases) == {
        "guest": 100,
        "member": 100,
    }
    assert len(bundle.safety_cases) == 24


def test_generation_uses_thirty_unique_catalog_candidates(catalog):
    bundle = generate_bundle(catalog, seed=631200)
    known = {int(product_id) for product_id in catalog}
    for case in bundle.ranking_cases:
        assert len(case.candidate_product_ids) == 30
        assert len(set(case.candidate_product_ids)) == 30
        assert set(case.candidate_product_ids) <= known


def test_generation_is_byte_stable(catalog, tmp_path: Path):
    first = generate_bundle(catalog, seed=631200)
    second = generate_bundle(catalog, seed=631200)
    assert serialize_bundle(first) == serialize_bundle(second)
```

- [ ] **Step 2: Run generation tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_generation.py -q`

Expected: import failure for the missing generator.

- [ ] **Step 3: Implement normalized catalog indexes**

```python
@dataclass(frozen=True)
class CatalogProduct:
    product_id: int
    name: str
    category: str
    category_parent: str
    category_leaf: str
    brand: str
    price: int | None
    rating: float | None
    review_count: int | None


@dataclass(frozen=True)
class CatalogIndex:
    by_id: Mapping[int, CatalogProduct]
    by_category: Mapping[str, tuple[CatalogProduct, ...]]
    by_parent: Mapping[str, tuple[CatalogProduct, ...]]
    by_brand: Mapping[str, tuple[CatalogProduct, ...]]
```

Discard products missing a name or category only for anchor selection; keep every valid product ID
available as a negative. Sort every index by product ID before seeded sampling.

- [ ] **Step 4: Implement the fixed quota planner and case builders**

Define the immutable quota table:

```python
QUOTAS = {
    "general": {"guest": 40, "member": 8},
    "budget_multi": {"guest": 28, "member": 12},
    "personalization": {"guest": 0, "member": 48},
    "repurchase": {"guest": 0, "member": 24},
    "long_tail": {"guest": 24, "member": 0},
    "adversarial": {"guest": 8, "member": 8},
}
```

For every builder, select one to six positive candidates first, then fill exact-category negatives,
sibling-category negatives, wrong-brand or constraint-violation negatives, and finally seeded random
catalog negatives to exactly 30. Preserve assembly order as the search baseline. Create 24 safety
cases with exact `8/8/8` scenario counts. Raise `GenerationError` when any quota cannot be filled.

- [ ] **Step 5: Run generation tests and verify GREEN**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_generation.py -q`

Expected: all tests pass twice with byte-identical serialization.

- [ ] **Step 6: Commit deterministic generation**

```bash
git add evals/rerank_holdout_v2/catalog.py evals/rerank_holdout_v2/generator.py \
  tests/eval/test_rerank_holdout_v2_generation.py
git commit -m "feat(eval): generate 200 catalog-derived rerank cases" \
  -m "Constraint: No production query logs are available\nRejected: Mutating legacy cases | correlated samples would overstate evidence\nConfidence: high\nScope-risk: moderate\nTested: exact quotas, candidate integrity, and deterministic generation"
```

---

### Task 3: Leakage, label, and quota validation

**Files:**
- Create: `evals/rerank_holdout_v2/validation.py`
- Test: `tests/eval/test_rerank_holdout_v2_validation.py`

**Interfaces:**
- Consumes: `GenerationBundle`, catalog mapping, and legacy core paths.
- Produces: `validate_bundle(bundle, catalog, legacy_root) -> AuditReport`.
- Produces: `normalized_query(value)`, `token_jaccard(left, right)`, and `violates_constraints(product, labels)` for focused tests.

- [ ] **Step 1: Write failing validation and sealed-label access tests**

```python
def test_validation_never_opens_legacy_holdout_labels(bundle, catalog, monkeypatch):
    real_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path.name == "buyer_holdout_labels.jsonl":
            raise AssertionError("legacy sealed labels were opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    report = validate_bundle(bundle, catalog, GOLDENSET_ROOT)
    assert report.passed is True


def test_validation_rejects_positive_price_violator(bundle, catalog):
    broken = with_positive_above_price_max(bundle)
    with pytest.raises(ValueError, match="positive candidate violates"):
        validate_bundle(broken, catalog, GOLDENSET_ROOT)
```

- [ ] **Step 2: Run validation tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_validation.py -q`

Expected: import failure for the missing validator.

- [ ] **Step 3: Implement one-pass validation and audit output**

`validate_bundle` must enforce exact counts and quotas, unique IDs/families/queries, 30 candidates,
catalog existence, candidate provenance coverage, one label row per core, one grade-3 positive,
one-to-six positives, grades 1–3 for listed positives, no constraint-violating positive, no identical
positive set, and no query collision or token Jaccard `>= 0.85` with legacy cores. Return an audit
record containing counts, distributions, maximum observed legacy similarity, catalog hash, seed, and
the complete list of passed checks.

- [ ] **Step 4: Run validation tests and verify GREEN**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_validation.py -q`

Expected: all validation tests pass and the guarded legacy label file remains unopened.

- [ ] **Step 5: Commit validation gates**

```bash
git add evals/rerank_holdout_v2/validation.py tests/eval/test_rerank_holdout_v2_validation.py
git commit -m "feat(eval): reject correlated or invalid rerank evidence" \
  -m "Constraint: The opened 19-case holdout cannot be reused for tuning\nConfidence: high\nScope-risk: narrow\nDirective: Do not weaken exact quotas or leakage thresholds to make generation pass\nTested: leakage, quota, referential, and hard-constraint validation"
```

---

### Task 4: Blind annotation packets and sealed release gate

**Files:**
- Create: `evals/rerank_holdout_v2/annotation.py`
- Create: `evals/rerank_holdout_v2/release.py`
- Test: `tests/eval/test_rerank_holdout_v2_release.py`

**Interfaces:**
- Produces: `write_review_packet(dataset, reviewer_slot, out) -> Path`.
- Produces: `load_review(path) -> ReviewSubmission`.
- Produces: `build_sealed_release(dataset, review_a, review_b, adjudication, out) -> DatasetManifest`.

- [ ] **Step 1: Write failing blindness and release-gate tests**

```python
def test_review_packets_hide_heuristic_labels(dataset, tmp_path: Path):
    packet = write_review_packet(dataset, reviewer_slot="A", out=tmp_path / "review-a.csv")
    text = packet.read_text(encoding="utf-8")
    assert "heuristic" not in text
    assert "suggestedGrade" not in text
    assert len(list(csv.DictReader(io.StringIO(text)))) == 200 * 30


def test_release_rejects_unadjudicated_disagreement(dataset, reviews, tmp_path: Path):
    review_a, review_b = reviews.with_one_disagreement()
    with pytest.raises(ValueError, match="unadjudicated disagreement"):
        build_sealed_release(dataset, review_a, review_b, {}, tmp_path)


def test_release_rejects_fake_or_missing_reviewers(dataset, reviews, tmp_path: Path):
    review_a, review_b = reviews.with_same_reviewer_identity()
    with pytest.raises(ValueError, match="independent reviewer"):
        build_sealed_release(dataset, review_a, review_b, {}, tmp_path)
```

- [ ] **Step 2: Run release tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_release.py -q`

Expected: import failure for the missing annotation/release modules.

- [ ] **Step 3: Implement blind packets and strict review imports**

Each CSV has 6,000 rows with columns `caseId,candidateSlot,productId,query,profileSummary,name,brand,
category,price,rating,reviewCount,grade,reviewerId,reviewedAt,rationale`. Candidate order is shuffled
with seeds `631201` and `631202`. Imports require grade 0–3 and non-empty reviewer metadata on every
row, reject duplicate/missing `(caseId, productId)` pairs, and require different reviewer IDs.

- [ ] **Step 4: Implement sealing**

Combine equal grades directly. For disagreements, require an adjudication entry containing grade,
adjudicator ID, timestamp, and rationale. Derive relevant IDs and ideal order deterministically by
descending final grade, then original candidate order. Write `sealed_labels.jsonl` and a manifest
whose `confirmatoryEligible` is true only after validating all 200 cases and hashing all release
files.

- [ ] **Step 5: Run release tests and verify GREEN**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_release.py -q`

Expected: all tests pass; tests use explicit synthetic reviewer fixtures and production code never
creates reviewer identities.

- [ ] **Step 6: Commit annotation workflow**

```bash
git add evals/rerank_holdout_v2/annotation.py evals/rerank_holdout_v2/release.py \
  tests/eval/test_rerank_holdout_v2_release.py
git commit -m "feat(eval): require independent review before holdout release" \
  -m "Constraint: Heuristic labels are not confirmatory evidence\nRejected: Auto-sealing generated grades | it would manufacture review confidence\nConfidence: high\nScope-risk: moderate\nTested: packet blindness, reviewer independence, disagreement adjudication, and release hashes"
```

---

### Task 5: Rerank input adapter and execution policy

**Files:**
- Create: `evals/rerank_holdout_v2/adapter.py`
- Modify: `evals/rerank_scoring/runner.py`
- Modify: `evals/rerank_scoring/cli.py`
- Test: `tests/eval/test_rerank_holdout_v2_adapter.py`
- Modify test: `tests/eval/test_rerank_scoring_runner.py`

**Interfaces:**
- Produces: `build_case_input(case, labels, catalog) -> RankingCaseInput`.
- Produces: `run_input_probe(llm, *, case_inputs, arms, repeats, attempt_multiplier, order_seeds, dataset_version, dataset_hash, grounding_arm="validated", expose_max=9, alpha=0.65, k=60) -> RankingProbeRun`.
- Preserves: existing `run_probe(llm, *, cases, fixtures, arms, repeats, attempt_multiplier, order_seeds, grounding_arm="validated", expose_max=9, alpha=0.65, k=60) -> RankingProbeRun` behavior and CLI defaults.

- [ ] **Step 1: Write failing adapter and zero-provider-call policy tests**

```python
def test_adapter_preserves_candidate_order_profile_and_labels(dataset):
    case = dataset.ranking_cases[0]
    labels = dataset.labels_by_case[case.case_id]
    result = build_case_input(case, labels, dataset.catalog)
    assert tuple(result.search_rank_by_id) == tuple(case.candidate_product_ids)
    assert result.profile_summary == case.profile_summary
    assert result.relevance_grades == labels.relevance_grades


def test_live_cli_rejects_draft_dataset_before_provider_build(monkeypatch, tmp_path: Path):
    called = False

    def forbidden_provider(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider should not be built")

    monkeypatch.setattr(rerank_cli, "build_live_delegate", forbidden_provider)
    code = rerank_cli.main([
        "--dataset", "rerank-holdout-v2", "--arms", "current,structured",
        "--out", str(tmp_path / "run"),
    ])
    assert code == rerank_cli.EXIT_REJECTED
    assert called is False
```

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_adapter.py -q`

Expected: import failure for the missing adapter or unknown CLI option.

- [ ] **Step 3: Implement adapter and input-oriented probe loop**

Move the current probe loop body into `run_input_probe`; have legacy `run_probe` map each
`GoldenCase` through its existing builder and delegate. The new adapter validates catalog products
as `SpringProduct`, applies hard filters, preserves original search ranks, and constructs slices
including stratum and identity.

- [ ] **Step 4: Add explicit CLI dataset selection**

`--dataset` accepts `goldenset-dev` (default) or `rerank-holdout-v2`. `--dry-run` may load draft
labels and writes `labelStatus: draft` plus `confirmatory: false` to the manifest. A non-dry run of
v2 requires `confirmatoryEligible: true` and sealed labels before building the provider, pacing, or
budget objects.

- [ ] **Step 5: Run adapter and legacy runner tests and verify GREEN**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_adapter.py tests/eval/test_rerank_scoring_runner.py -q`

Expected: all tests pass; legacy goldenset behavior remains unchanged.

- [ ] **Step 6: Commit evaluation integration**

```bash
git add evals/rerank_holdout_v2/adapter.py evals/rerank_scoring/runner.py \
  evals/rerank_scoring/cli.py tests/eval/test_rerank_holdout_v2_adapter.py \
  tests/eval/test_rerank_scoring_runner.py
git commit -m "feat(eval): load prospective rerank cases without weakening release gates" \
  -m "Constraint: Current dev CLI behavior must remain compatible\nConfidence: high\nScope-risk: moderate\nDirective: Provider construction must stay behind the sealed-label preflight\nTested: adapter, draft rejection, dry-run policy, and legacy runner regressions"
```

---

### Task 6: Generate and commit the 200-case artifacts

**Files:**
- Create: `evals/rerank_holdout_v2/cli.py`
- Create: `evals/rerank_holdout_v2/__main__.py`
- Create: `evals/rerank_holdout_v2/dataset/cases/ranking_core.jsonl`
- Create: `evals/rerank_holdout_v2/dataset/cases/safety.jsonl`
- Create: `evals/rerank_holdout_v2/dataset/annotations/draft_labels.jsonl`
- Create: `evals/rerank_holdout_v2/dataset/audit/report.json`
- Create: `evals/rerank_holdout_v2/dataset/manifest.json`
- Test: extend `tests/eval/test_rerank_holdout_v2_generation.py`

**Interfaces:**
- Produces CLI commands `generate`, `audit`, `packet`, and `seal`.
- Commits generated files whose hashes are recorded in `manifest.json`.

- [ ] **Step 1: Write failing committed-artifact and regeneration tests**

```python
def test_committed_dataset_is_complete_and_audited():
    dataset = load_dataset(ROOT, label_policy="draft")
    assert len(dataset.ranking_cases) == 200
    assert len(dataset.labels_by_case) == 200
    assert len(dataset.safety_cases) == 24
    assert dataset.manifest.confirmatory_eligible is False
    assert dataset.manifest.label_status == "draft"


def test_committed_artifacts_match_fresh_generation(tmp_path: Path):
    assert main(["generate", "--out", str(tmp_path), "--seed", "631200"]) == 0
    for relative in GENERATED_FILES:
        assert (tmp_path / relative).read_bytes() == (ROOT / relative).read_bytes()
```

- [ ] **Step 2: Run committed-artifact tests and verify RED**

Run: `uv run pytest tests/eval/test_rerank_holdout_v2_generation.py -q`

Expected: committed dataset root or CLI is missing.

- [ ] **Step 3: Implement atomic CLI generation and audit commands**

Generate into a sibling temporary directory, run `validate_bundle`, write stable artifacts and the
manifest last, then atomically replace the requested empty output directory. Refuse a non-empty
destination. `audit` reloads hashes and labels, reruns validation, and writes the same stable report.

- [ ] **Step 4: Generate the committed dataset**

Run:

```bash
uv run python -m evals.rerank_holdout_v2 generate \
  --catalog evals/goldenset/fixtures/catalog_snapshot.json \
  --legacy-root evals/goldenset \
  --seed 631200 \
  --out evals/rerank_holdout_v2
```

Expected: summary prints `ranking=200 draft_labels=200 safety=24 confirmatory=false`.

- [ ] **Step 5: Verify generated assets and clean regeneration**

Run:

```bash
uv run pytest tests/eval/test_rerank_holdout_v2_generation.py \
  tests/eval/test_rerank_holdout_v2_validation.py -q
tmp=$(mktemp -d)
uv run python -m evals.rerank_holdout_v2 generate --out "$tmp/dataset" --seed 631200
diff -qr evals/rerank_holdout_v2/dataset/cases "$tmp/dataset/cases"
diff -q evals/rerank_holdout_v2/dataset/annotations/draft_labels.jsonl \
  "$tmp/dataset/annotations/draft_labels.jsonl"
```

Expected: tests pass and both diffs report no differences.

- [ ] **Step 6: Commit generated evidence**

```bash
git add evals/rerank_holdout_v2 tests/eval/test_rerank_holdout_v2_generation.py
git commit -m "test(eval): establish a 200-case prospective rerank baseline" \
  -m "Constraint: Generated labels remain draft pending real human review\nConfidence: high\nScope-risk: moderate\nDirective: Do not cite this dataset as confirmatory until a sealed release manifest exists\nTested: exact 200/24 counts, audit gates, hashes, and byte-identical regeneration"
```

---

### Task 7: Documentation and final verification

**Files:**
- Create: `evals/rerank_holdout_v2/README.md`
- Modify: `evals/rerank_scoring/README.md`
- Modify: `evals/README.md`

**Interfaces:**
- Documents exact commands, status meanings, provenance limitations, and the old-holdout prohibition.

- [ ] **Step 1: Document dataset use and non-claims**

The README must state:

```text
Ranking N is 200; safety N is 24 and is never included in ranking metrics.
Source is the pinned local catalog snapshot, not production query logs.
Draft heuristic labels support exploratory dry-runs only.
Confirmatory live evaluation requires two independent human reviews, complete adjudication,
and a sealed release manifest.
The previously opened buyer_holdout labels must not be copied, reopened, or used to tune v2.
```

- [ ] **Step 2: Run focused formatting, lint, and tests**

Run:

```bash
uv run ruff format --check evals/rerank_holdout_v2 tests/eval/test_rerank_holdout_v2_*.py
uv run ruff check evals/rerank_holdout_v2 tests/eval/test_rerank_holdout_v2_*.py \
  evals/rerank_scoring/runner.py evals/rerank_scoring/cli.py
uv run pytest tests/eval/test_rerank_holdout_v2_*.py \
  tests/eval/test_rerank_scoring_runner.py tests/eval/test_goldenset_eval.py -q
```

Expected: zero formatting/lint findings and all focused tests pass.

- [ ] **Step 3: Run the applicable full test suite and pre-commit**

Run:

```bash
uv run pytest -q
uv run pre-commit run --all-files
git diff --check
git status --short
```

Expected: all tests and hooks pass; status contains only intended documentation changes before the
final commit.

- [ ] **Step 4: Commit documentation and verification record**

```bash
git add evals/rerank_holdout_v2/README.md evals/rerank_scoring/README.md evals/README.md
git commit -m "docs(eval): prevent overclaiming the 200-case rerank draft" \
  -m "Constraint: Catalog-derived heuristic labels are not human ground truth\nConfidence: high\nScope-risk: narrow\nDirective: Report ranking and safety sample sizes separately\nTested: full pytest, Ruff, pre-commit, regeneration diff, and git diff checks"
```

- [ ] **Step 5: Final repository evidence check**

Run:

```bash
git status --short --branch
git log --oneline --decorate -10
uv run python -m evals.rerank_holdout_v2 audit --root evals/rerank_holdout_v2
```

Expected: clean worktree and audit summary
`ranking=200 guest=100 member=100 safety=24 label_status=draft confirmatory=false`.
