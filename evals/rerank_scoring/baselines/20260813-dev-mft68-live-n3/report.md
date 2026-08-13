# Rerank scoring paired evaluation

- status: `regressed`
- dataset: `2.3.0` / `675520d999dc1fbf0a4b32e13914205bc61c606c9adc2f65833eb67fc133ae50`
- arms: `current,structured,hybrid`
- repeats/seeds: `1` / `[11, 29, 47]`
- dry-run: `False`

## Primary A→C comparison

| paired N | mean ΔnDCG@10 | CI low | CI high | verdict |
|---:|---:|---:|---:|---|
| 68 | -0.2469658207124669 | -0.3410002022868709 | -0.14990598198262634 | regressed |

## Integrity

| arm | samples | hard violations | foreign rows | duplicates | partial/full fallback |
|---|---:|---:|---:|---:|---:|
| current | 202 | 0 | 0 | 0 | 0/0 |
| structured | 204 | 0 | 1 | 0 | 2/0 |
| hybrid | 204 | 0 | 1 | 0 | 2/0 |

Dry-run verifies the harness only; it is never evidence of production quality.
Initial 4:2:1 weights and RRF alpha/k remain experimental until a live paired run supports them.
