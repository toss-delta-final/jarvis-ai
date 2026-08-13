# Rerank scoring paired evaluation

- status: `exploratory`
- dataset: `1.0.0` / `4fa52e596f97c60c2b067c0ca6b30345ed574fcb7ad67acb67009b344a49f87b`
- arms: `current,code_assisted`
- repeats/seeds: `1` / `[11]`
- dry-run: `False`
- label status: `draft`
- confirmatory: `False`

## Primary comparison: currentToCodeAssisted

| paired N | mean ΔnDCG@10 | CI low | CI high | verdict | statistical verdict |
|---:|---:|---:|---:|---|---|
| 170 | -0.1470792411913958 | -0.1902150550685125 | -0.1032817704033684 | exploratory | regressed |

## Integrity

| arm | samples | hard violations | foreign rows | duplicates | partial/full fallback |
|---|---:|---:|---:|---:|---:|
| current | 175 | 0 | 0 | 0 | 0/1 |
| code_assisted | 175 | 0 | 0 | 0 | 0/0 |

Heuristic draft labels make this exploratory only; it is not confirmatory evidence.
Initial 4:2:1 weights and RRF alpha/k remain experimental until a live paired run supports them.
