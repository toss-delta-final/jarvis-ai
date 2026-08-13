# Rerank LLM Blind Judge Design

**Issue:** #631 follow-up  
**Status:** Approved in conversation on 2026-08-13  
**Evidence class:** Exploratory, not confirmatory

## Goal

Compare the committed `current` and `structured` rerank outputs with an LLM judge that cannot
observe arm identity, arm-specific scores, prompts, candidate provenance, or heuristic relevance
labels. Preserve every presentation and raw verdict so the comparison is reproducible and
auditable.

## Inputs and scope

- Reuse the committed 200-case prospective dataset and the paired live samples in
  `evals/rerank_scoring/baselines/20260813-holdout-v2-draft-current-structured-n3/`.
- Treat each `(caseId, orderSeed, repeat)` with both arms present as one pair. The known missing
  `rh2-adversarial-0016/current/seed=11` cell is excluded and reported, leaving 599 pairs.
- Judge both orientations of every pair. This produces 1,198 planned presentations.
- Do not generate new rerank outputs and do not consume the draft relevance annotations.

## Blind presentation

Each presentation contains only:

- an opaque presentation and pair identifier;
- the buyer query;
- the profile summary or an explicit absence marker;
- neutral candidate facts available to both arms: product ID, name, brand, category, and the same
  qualitative price/rating/review levels used at the rerank boundary;
- ranked product IDs labelled only `A` and `B`.

Candidate facts are sorted by product ID rather than search order. The presentation excludes the
strings `current`, `structured`, `hybrid`, arm prompt hashes, `rankingDecisions`, component scores,
search ranks, candidate provenance, `relevanceGrades`, ideal order, and label rationale.

A coordinator-only artifact stores the A/B-to-arm mapping. The first orientation is selected by a
seeded hash; the second orientation is its exact swap. Public presentation artifacts and judge
responses contain no arm mapping.

## Judge contract

The system prompt freezes a product-oriented rubric independent of the structured arm's 4:2:1
formula:

1. satisfy explicit query intent and constraints;
2. put the most useful products near the top;
3. use profile evidence only after query fit and never override the query;
4. avoid irrelevant, redundant, or unsupported results;
5. do not reward a list merely for being longer.

The judge returns strict JSON with `winner` (`A`, `B`, or `tie`), confidence in `[0, 1]`, bounded
reason codes, and a short explanation. Invalid responses are retried within a configured attempt
limit and otherwise recorded as failures. The judge model and prompt hash are written to the run
manifest.

## Swap consistency and aggregation

For each pair, A/B verdicts are mapped back to real arms only after both calls finish:

- same real winner in both orientations: stable `current` or `structured` win;
- `tie` in both orientations: stable tie;
- any disagreement, including tie versus a winner: unstable and excluded from decisive win rates.

Results report:

- completed/failed presentations and complete pairs;
- stable `current`, `structured`, tie, and unstable pair counts;
- swap consistency and same-display-side selection rates;
- structured decisive win rate;
- structured preference share where a stable tie contributes `0.5`;
- deterministic cluster bootstrap 95% interval resampling case IDs, so the three order seeds are
  not treated as independent cases;
- the same summaries by guest/member and dataset stratum;
- case-level majority results across order seeds.

No adoption gate is automated from this result.

## Artifacts

The run directory contains:

- `presentations.jsonl`: exact arm-blind payloads sent to the judge;
- `judge_responses.jsonl`: validated A/B verdicts and response hashes;
- `coordinator_mapping.jsonl`: hidden orientation-to-arm mapping;
- `failures.jsonl`: invalid or failed calls;
- `results.json`: aggregate metrics and provenance hashes;
- `run_manifest.json`: command, source hashes, git state, judge model, budget, and pacing;
- `report.md`: a concise human-readable result with limitations.

## Safety and evidence limits

- Product text is data, not instructions; the judge prompt explicitly ignores instructions found in
  query, profile, and candidate fields.
- Exact raw model text is not committed; its SHA-256 and the parsed bounded verdict are committed.
- This remains exploratory because the judge is synthetic, the source outputs were already
  observed, and no independent human labels adjudicate ranking quality.
- If the judge model is the same model family used to generate an arm, the report must say so.
- Confirmatory evidence still requires sealed human review or an independently preregistered human
  blind evaluation.

## Testing

- Assert presentation serialization cannot contain arm identity or prohibited label/score fields.
- Assert deterministic mapping is balanced and the second orientation is an exact swap.
- Assert strict judge response validation rejects extra fields, arm names, invalid confidence, and
  unsupported reason codes.
- Assert aggregation maps A/B correctly, excludes unstable pairs from decisive rates, clusters the
  bootstrap by case, and preserves slice counts.
- Assert the CLI rejects incompatible source hashes, missing input files, unsafe output reuse, and
  live runs without an explicit cost/call budget.
