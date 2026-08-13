# Rerank scoring paired evaluation

- status: `exploratory`
- dataset: `1.0.0` / `4fa52e596f97c60c2b067c0ca6b30345ed574fcb7ad67acb67009b344a49f87b`
- arms: `code_assisted`
- repeats/seeds: `1` / `[11]`
- dry-run: `False`
- label status: `draft`
- confirmatory: `False`

## Primary comparison: not-tested

| paired N | mean ΔnDCG@10 | CI low | CI high | verdict | statistical verdict |
|---:|---:|---:|---:|---|---|
| 0 | None | None | None | not-tested | not-tested |

## Integrity

| arm | samples | hard violations | foreign rows | duplicates | partial/full fallback |
|---|---:|---:|---:|---:|---:|
| code_assisted | 1 | 0 | 0 | 0 | 0/0 |

Heuristic draft labels make this exploratory only; it is not confirmatory evidence.
Initial 4:2:1 weights and RRF alpha/k remain experimental until a live paired run supports them.
