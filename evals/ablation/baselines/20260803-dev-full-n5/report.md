# Recommendation pipeline ablation

## Arm summary

| arm | primary mean | calls | input tokens/case | output tokens/case | cost USD/case | total latency ms/case | token coverage | cost coverage | TTFT |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| pipeline | 0.782943 | 290 | 4170.10 | 540.53 | 0.00089723 | 6362.73 | 1.000 | 1.000 | unknown |
| scoring | 0.616852 | 0 | N/A | N/A | N/A | 3.78 | N/A | N/A | unknown |
| single_call | 0.696084 | 155 | 1993.87 | 432.51 | 0.00091779 | 4851.83 | 1.000 | 1.000 | unknown |

Arm B의 token/cost는 0이 아니라 LLM 호출 없음으로 해당 없음이다.
total latency는 adapter 전체 벽시계이며 TTFT는 server_first_text_token_ms를 오프라인에서 관측할 수 없어 unknown이다.

## Quality and safety

| arm | nDCG@10 | Precision@10 | Recall@10 | MRR | FilterAcc | hard failures | hard constraint violation rate | ranking excluded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pipeline | 0.782943 | 0.240000 | 0.824444 | 0.877791 | 0.063519 | 0 | 0.000000 | 13 |
| scoring | 0.616852 | 0.222222 | 0.705556 | 0.791667 | 1.000000 | 0 | 0.032258 | 13 |
| single_call | 0.696084 | 0.207778 | 0.731852 | 0.816667 | 0.248333 | 0 | 0.032258 | 13 |

hard constraint violation rate는 hardFailure 행도 evaluated metric row 분모에 포함하므로 희석될 수 있으며 hard failure count와 함께 해석한다.

## Exploratory slice primary metrics

### pipeline
- `category_mapping_failure`: n=6, mean=0.822754
- `cold_start`: n=2, mean=0.846149
- `failure`: n=0, mean=N/A
- `guest`: n=9, mean=0.787692
- `multi_constraint`: n=1, mean=0.604947
- `personalization`: n=7, mean=0.758779
- `personalization_overreach`: n=3, mean=0.622301
- `repurchase`: n=2, mean=1.000000
- `search`: n=18, mean=0.782943
### scoring
- `category_mapping_failure`: n=6, mean=0.679712
- `cold_start`: n=2, mean=0.775405
- `failure`: n=0, mean=N/A
- `guest`: n=9, mean=0.585591
- `multi_constraint`: n=1, mean=0.472944
- `personalization`: n=7, mean=0.611743
- `personalization_overreach`: n=3, mean=0.657697
- `repurchase`: n=2, mean=0.500000
- `search`: n=18, mean=0.616852
### single_call
- `category_mapping_failure`: n=6, mean=0.827615
- `cold_start`: n=2, mean=0.852778
- `failure`: n=0, mean=N/A
- `guest`: n=9, mean=0.750079
- `multi_constraint`: n=1, mean=0.521972
- `personalization`: n=7, mean=0.581891
- `personalization_overreach`: n=3, mean=0.467443
- `repurchase`: n=2, mean=0.815465
- `search`: n=18, mean=0.696084

## Failure cases

### pipeline
- 없음
### scoring
- 없음
### single_call
- 없음

## Confirmatory paired primary deltas

| pair (left-right) | paired N | mean delta | bootstrap 95% CI | verdict |
|---|---:|---:|---|---|
| pipeline-scoring | 18 | 0.166092 | [0.035314, 0.320129] | pipelineWins |
| pipeline-single_call | 18 | 0.086860 | [0.022035, 0.159958] | pipelineWins |
| single_call-scoring | 18 | 0.079232 | [-0.086218, 0.264728] | inconclusive |

Primary metric만 confirmatory이며 secondary metric·latency·token·cost는 exploratory다. CI가 0을 포함하면 승자를 정하지 않고 inconclusive로 표기한다.
