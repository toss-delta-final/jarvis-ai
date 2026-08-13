# Rerank scoring paired evaluation

- status: `exploratory`
- dataset: `1.0.0` / `4fa52e596f97c60c2b067c0ca6b30345ed574fcb7ad67acb67009b344a49f87b`
- arms: `current,structured`
- repeats/seeds: `1` / `[11, 29, 47]`
- dry-run: `False`
- label status: `draft`
- confirmatory: `False`

## Primary comparison: currentToStructured

| paired N | mean ΔnDCG@10 | CI low | CI high | verdict | statistical verdict |
|---:|---:|---:|---:|---|---|
| 200 | 0.12187363360771485 | 0.0925461671532019 | 0.1534501324698942 | exploratory | supported |

## Integrity

| arm | samples | hard violations | foreign rows | duplicates | partial/full fallback |
|---|---:|---:|---:|---:|---:|
| current | 599 | 0 | 0 | 0 | 0/9 |
| structured | 600 | 0 | 1 | 0 | 3/0 |

Heuristic draft labels make this exploratory only; it is not confirmatory evidence.
Initial 4:2:1 weights and RRF alpha/k remain experimental until a live paired run supports them.
