# Rerank Prospective Holdout v2 (200 Cases) Design

## 1. Goal

Build a new, independent rerank evaluation dataset with exactly **200 ranking cases** so the
`current` versus `structured` scoring comparison is no longer driven by the previously opened
19-case holdout. Keep safety checks in a separate 24-case dataset so safety rows cannot inflate
ranking sample size or nDCG confidence.

This dataset is prospective evidence. The generator may create heuristic draft labels for review,
but those labels are not confirmatory evidence until two independent human reviewers agree or an
adjudicator resolves their disagreement and the resulting release is sealed.

## 2. Context and constraints

- Issue #631 introduced selectable `current`, `structured`, and `hybrid` rerank scoring.
- The dev experiment contained 68 eligible ranking cases. The old holdout produced 19 eligible
  paired ranking cases and has already been opened once.
- The old holdout must not be expanded, relabeled, copied, or used for tuning.
- The repository contains no production query log export. It does contain a pinned local catalog
  snapshot with 6,585 products. New cases therefore have `synthetic-catalog-derived` provenance,
  not `production-derived` provenance.
- The existing `evals/goldenset` schema pins dataset version `2.3.0` and its manifest hash supports
  prior release evidence. The new dataset must not mutate that package or its hashes.
- Work happens in the existing linked worktree on branch
  `NyongCho/feat-631-hybrid-rerank-scoring`.
- No new runtime dependency is required.

## 3. Considered approaches

### A. Add rows to `evals/goldenset` (rejected)

This would reuse existing loaders, but it would change the dataset hash behind prior dev and
holdout reports and invite accidental access to the already-opened holdout labels.

### B. Duplicate and mutate existing cases (rejected)

This is fast, but mutations are correlated with the 68 dev and 19 holdout ranking cases. Counting
them as independent samples would overstate effective sample size and narrow confidence intervals
without adding equivalent information.

### C. Separate catalog-derived prospective dataset (selected)

Create `evals/rerank_holdout_v2` with its own schema, manifest, generator, validator, loader, and
annotation/release workflow. Cases reference the pinned catalog by product ID instead of copying
product payloads. This preserves prior evidence, makes provenance explicit, and supports a hard
release gate before confirmatory evaluation.

## 4. Dataset composition

The ranking set contains exactly 200 rows and exactly 100 guest / 100 member identities.

| Stratum | Total | Guest | Member | Purpose |
| --- | ---: | ---: | ---: | --- |
| `general` | 48 | 40 | 8 | Plain category/brand intent and ordinary semantic ranking |
| `budget_multi` | 40 | 28 | 12 | Price ceilings and two explicit constraints |
| `personalization` | 48 | 0 | 48 | 24 preference-helpful and 24 profile-overreach cases |
| `repurchase` | 24 | 0 | 24 | Similar-to-prior-purchase and same-brand continuity |
| `long_tail` | 24 | 24 | 0 | Sparse/deep catalog categories and near-category negatives |
| `adversarial` | 16 | 8 | 8 | Conflicting soft preferences, lexical distractors, and hard exclusions |
| **Total** | **200** | **100** | **100** | |

The safety set contains exactly 24 rows, eight per scenario:

- `catalog_prompt_injection`: candidate text attempts to override ranking instructions;
- `hard_constraint_integrity`: attractive candidates violate an explicit price/category/product
  constraint;
- `candidate_set_integrity`: candidate text requests fabricated or foreign product IDs.

Safety cases are stored and reported separately and never contribute to ranking nDCG or the paired
200-case confidence interval.

## 5. Case and label boundaries

### 5.1 Visible ranking core

`cases/ranking_core.jsonl` is safe to inspect before release. Each row contains:

- `caseId`, `familyId`, `schemaVersion`, `datasetVersion`, and `split`;
- `stratum`, `variant`, and slice tags;
- query and identity (`guest` or `member`);
- an optional bounded `profileSummary` for member cases;
- exactly 30 distinct `candidateProductIds` in deterministic search order;
- candidate provenance for each ID (`exact_category`, `near_category`, `wrong_brand`,
  `constraint_violation`, or `random_catalog`);
- the pinned catalog SHA-256 and synthetic-catalog-derived provenance.

No relevance grade, ideal order, must-exclude list, or label rationale is embedded in the core.

### 5.2 Draft annotation labels

`annotations/draft_labels.jsonl` contains deterministic heuristic proposals:

- positive candidate grades in the range 1–3; omitted candidates are implicitly grade 0;
- hard constraints and must-exclude IDs;
- an observable-fact rationale naming the category, brand, price, or profile rule used;
- `status: draft`, `labelSource: heuristic`, and no adjudicator.

The draft file exists to accelerate blind human labeling. Its manifest state is
`confirmatoryEligible: false`, and evaluation code rejects it for a release run.

### 5.3 Review packet and sealing

`annotation.py` emits two reviewer packets whose candidate presentation orders are independently
shuffled and whose heuristic grades are hidden. Review imports require every one of the 200 case
IDs, every candidate grade, reviewer identity, and timestamp. `release.py` accepts two completed
imports, records agreement, requires adjudication for every disagreement, writes a sealed label
file, and changes eligibility only when all gates pass.

The repository implementation can generate draft cases and packets autonomously. It must not claim
that human review occurred and must not manufacture reviewer identities.

## 6. Deterministic generation

The generator uses seed `631200` and the SHA-pinned
`evals/goldenset/fixtures/catalog_snapshot.json`.

1. Normalize catalog category, brand, name, price, rating, and review-count facts.
2. Build exact-category, sibling-category, brand, and priced-product indexes.
3. Select unique case families without replacement from eligible category/brand combinations.
4. Render a query from the stratum-specific template and observable catalog facts.
5. Assemble six or fewer positive candidates and hard negatives to exactly 30 candidates.
6. Produce draft grades from explicit rules. Price/category/product hard-constraint violators always
   receive grade 0.
7. Sort output rows by case ID and serialize JSON with stable key ordering and UTF-8 newlines.

Generation fails rather than relaxing quotas, using fewer candidates, duplicating a family, or
silently changing the seed.

## 7. Independence and leakage controls

The validator loads legacy **case cores** from `buyer_dev.jsonl` and `buyer_holdout.jsonl` but never
opens `buyer_holdout_labels.jsonl`. It enforces:

- no case ID, family ID, or normalized exact-query collision;
- no new-case duplicate family or normalized query;
- no identical positive-label set across new cases;
- token-Jaccard similarity below `0.85` against legacy queries, excluding common Korean stop words;
- exactly 30 distinct, catalog-existing candidates per ranking case;
- candidate-to-label referential integrity;
- at least one grade-3 candidate and at most six positive candidates per case;
- no positive candidate violating a hard constraint;
- exact stratum and guest/member quotas;
- exact ranking count 200 and separate safety count 24;
- byte-identical output from two runs with the same seed and snapshot.

Tests spy on file access and fail if generation or draft validation attempts to read the legacy
holdout label path.

## 8. Evaluation integration

The loader combines a core row with labels only through an explicit label policy:

- `draft`: dry-run이 기본이며, 명시적 `--allow-draft-live`에서만 live exploratory 비교를 허용한다.
  이 경우 수치·CI는 보존하되 claim status와 verdict를 `exploratory`로 강제한다;
- `sealed`: required for live confirmatory runs;
- missing labels: core inspection and annotation packet generation only.

The adapter constructs existing `RankingCaseInput` values from the shared catalog snapshot. The
rerank runner gains an input-oriented execution path while its current `GoldenCase` path remains
unchanged. The CLI adds a separate `--dataset rerank-holdout-v2` selection and rejects live runs
unless sealed labels pass manifest/hash validation. It never silently falls back to dev data.

## 9. Error handling

- Schema, quota, hash, leakage, label, and release-gate failures raise a case-specific `ValueError`.
- Generation writes into a temporary directory and replaces committed artifacts only after full
  validation, preventing partial datasets.
- Existing output directories remain immutable; reruns require a new destination.
- A catalog hash mismatch stops loading and generation before any cases are returned.
- A draft-label live run without `--allow-draft-live` exits with the existing CLI rejection code and
  performs zero provider calls; explicit opt-in is always marked exploratory.

## 10. Verification and completion criteria

Completion requires fresh evidence for all of the following:

1. Unit tests observe RED before each schema/generator/validator/release implementation.
2. The committed generator produces exactly 200 ranking cores, 200 draft label rows, and 24 safety
   rows from the pinned snapshot.
3. Regeneration is byte-identical and leaves `git diff` empty.
4. The audit report records all quota, candidate-depth, provenance, leakage, and label-status checks.
5. Draft labels cannot pass the confirmatory release gate; only explicit exploratory live runs may
   trigger provider calls, and their claim status/verdict is suppressed to `exploratory`.
6. Existing goldenset and rerank-scoring tests remain green.
7. Ruff, targeted tests, the applicable full test suite, and all pre-commit hooks pass.
8. Commits follow Conventional Commits plus the repository Lore trailers.

The implementation is complete when the 200-case draft dataset and 24-case safety set are generated,
audited, loadable for exploratory runs, and mechanically blocked from confirmatory use until genuine
human review and sealing occur.
