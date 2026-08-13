# Current vs code-assisted: 180-case exploratory evaluation

## Executive conclusion

`code_assisted` should **not** replace `current` in its present form. On 170 complete paired cases,
heuristic draft-label nDCG@10 regressed and the position-swapped fresh-context Codex blind judge strongly
preferred `current`. The main observable failure mode is under-selection: `code_assisted` returned too few
useful products rather than adding an irrelevant long tail.

## Run scope

- selected cases: 180 of 200, deterministic SHA-256 selection seed `631`
- identities: 90 guest / 90 member
- generation arms: `current`, `code_assisted`
- candidate-order seed: `11`; repeats: 1; retries: 0
- provider calls: 360 (`gpt-5.6-luna`)
- successful samples: 350; generation failures: 10
- complete paired cases: 170
- blind presentations: 340 (A/B and B/A), all completed
- blind judge: fresh-context Codex `gpt-5.6-sol`; API judge calls: 0

## Ranking metrics

| metric | current | code_assisted | delta |
|---|---:|---:|---:|
| mean nDCG@10 | 0.7185 | 0.5731 | -0.1471 |
| p50 latency | 4552 ms | 6146 ms | +1594 ms |
| p95 latency | 9136 ms | 11643 ms | +2507 ms |
| average exposed products | 3.63 | 2.20 | -1.43 |

Paired mean nDCG@10 delta is `-0.1471` with draft-label
bootstrap CI `[-0.1902, -0.1033]`.
These labels remain heuristic and exploratory.

## Blind judge

| swap-stable outcome | pairs |
|---|---:|
| current win | 78 |
| code_assisted win | 15 |
| stable tie | 58 |
| position-swap unstable | 19 |

- swap consistency: `0.8882`
- decisive `code_assisted` win rate: `0.1613` (15 / 93)
- case-clustered `code_assisted` preference share: `0.2914`, 95% CI `[0.2384, 0.3444]`
- same-display-side rate: `0.0765`

## Failure pattern

Across all successful samples, `current` exposed 3.63 products on average while
`code_assisted` exposed 2.20. In the 78 stable current-win cases,
the averages were 4.29 versus
1.88. `USEFUL_COVERAGE` appeared in
97 of the 156 current-win presentations.

This points to a selection-policy problem: the prompt's lower bound of one prevents empty output but gives no target
coverage, so the model often returns only one or two products. Any next experiment should fix desired output count or
minimum semantic threshold explicitly before reconsidering code-assisted ranking.

## Cost audit

The execution-time committed Luna price table was stale: it used `$0.20/M` input and `$1.20/M` output,
while current official rates are `$1/M` input and `$6/M` output. Therefore the recorded `$0.5064` did **not**
represent the likely API bill and the requested `$1` target was exceeded.

- official-price lower bound before two timeout calls: `$1.4933`
- uncached-input estimate before timeouts: `$2.6284`
- cache-write upper bound before timeouts: `$2.9438`
- timeout calls with unknown billing: 2
- blind-judge API cost: `$0` (Codex agent surface)

The repository price manifest was corrected after discovery so future budget gates use the current official Luna rates
and include Sol. Exact billing must still be checked in the OpenAI dashboard because timeout usage and successful-call
cache splits were not persisted.

## Limitations

- one candidate-order seed only
- 10 generation failures leave 170 complete pairs
- heuristic draft labels are not sealed relevance judgments
- Codex synthetic judge is not a human blind review or production A/B
- fresh-context judge outputs are saved, but the Codex product surface is not identical to an API eval runtime
