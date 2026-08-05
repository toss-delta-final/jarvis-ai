# Recommendation pipeline ablation

## Arm summary

| arm | primary mean | calls | input tokens/case | output tokens/case | cost USD/case | total latency ms/case | token coverage | cost coverage | TTFT |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| pipeline | 0.738029 | 650 | 6572.81 | 678.19 | 0.00143353 | 8435.03 | 1.000 | 1.000 | unknown |
| scoring | 0.440818 | 0 | N/A | N/A | N/A | 6.43 | N/A | N/A | unknown |
| single_call | 0.706531 | 335 | 4689.97 | 539.49 | 0.00158539 | 6255.20 | 1.000 | 1.000 | unknown |

Arm B의 token/cost는 0이 아니라 LLM 호출 없음으로 해당 없음이다.
total latency는 adapter 전체 벽시계이며 TTFT는 server_first_text_token_ms를 오프라인에서 관측할 수 없어 unknown이다.

## Quality and safety

| arm | nDCG@10 | Precision@10 | Recall@10 | MRR | FilterAcc | hard failures | hard constraint violation rate | ranking excluded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pipeline | 0.738029 | 0.347742 | 0.725256 | 0.897778 | 0.113871 | 0 | 0.000000 | 5 |
| scoring | 0.440818 | 0.225806 | 0.460210 | 0.681336 | 1.000000 | 0 | 0.029851 | 5 |
| single_call | 0.706531 | 0.317742 | 0.679378 | 0.879785 | 0.279140 | 0 | 0.014925 | 5 |

hard constraint violation rate는 hardFailure 행도 evaluated metric row 분모에 포함하므로 희석될 수 있으며 hard failure count와 함께 해석한다.

## Exploratory slice primary metrics

### pipeline
- `budget`: n=12, mean=0.697629
- `category_mapping_failure`: n=8, mean=0.904771
- `cold_start`: n=2, mean=0.903827
- `failure`: n=0, mean=N/A
- `guest`: n=31, mean=0.792078
- `member`: n=31, mean=0.683981
- `multi_constraint`: n=10, mean=0.619373
- `personalization`: n=11, mean=0.791400
- `personalization_overreach`: n=6, mean=0.795072
- `repurchase`: n=7, mean=0.550209
- `search`: n=62, mean=0.738029
- `single_need`: n=33, mean=0.828518
### scoring
- `budget`: n=12, mean=0.360635
- `category_mapping_failure`: n=8, mean=0.539824
- `cold_start`: n=2, mean=0.946105
- `failure`: n=0, mean=N/A
- `guest`: n=31, mean=0.463703
- `member`: n=31, mean=0.417934
- `multi_constraint`: n=10, mean=0.364770
- `personalization`: n=11, mean=0.393690
- `personalization_overreach`: n=6, mean=0.446388
- `repurchase`: n=7, mean=0.176159
- `search`: n=62, mean=0.440818
- `single_need`: n=33, mean=0.549161
### single_call
- `budget`: n=12, mean=0.664724
- `category_mapping_failure`: n=8, mean=0.846989
- `cold_start`: n=2, mean=0.910350
- `failure`: n=0, mean=N/A
- `guest`: n=31, mean=0.774359
- `member`: n=31, mean=0.638703
- `multi_constraint`: n=10, mean=0.522705
- `personalization`: n=11, mean=0.705588
- `personalization_overreach`: n=6, mean=0.730719
- `repurchase`: n=7, mean=0.651110
- `search`: n=62, mean=0.706531
- `single_need`: n=33, mean=0.789195

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
| pipeline-scoring | 62 | 0.297211 | [0.231781, 0.363905] | pipelineWins |
| pipeline-single_call | 62 | 0.031498 | [-0.004701, 0.066304] | inconclusive |
| single_call-scoring | 62 | 0.265713 | [0.193565, 0.339099] | single_callWins |

Primary metric만 confirmatory이며 secondary metric·latency·token·cost는 exploratory다. CI가 0을 포함하면 승자를 정하지 않고 inconclusive로 표기한다.
