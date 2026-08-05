# personalization live-v1 baseline

- command: `uv run python -m evals.personalization --live --out evals/personalization/baselines/live-v1`
- dataset: dev 31건 × repeats 1, 실제 LLM 172호출
- budget: 436,688 tokens / $0.082136 (상한 800호출·30M tokens·$20)
- token/cost coverage: 전 arm 1.0

## #119 전후 arm

| arm | #119 매핑 | decompose profile | rerank profile | axis leakage | intent contradiction | guest 대비 ΔNDCG@10 (95% CI, n=18) |
|---|---|---|---|---:|---:|---:|
| guest | 기준선 | 없음 | 없음 | 0 | 5 | 기준 |
| clean_rerank_only | 수정 후·현행 기본 | 없음 | clean | 1 (`buy-pers-0001`) | 3 | -0.056445 (-0.202144, +0.049578) |
| clean_both | 수정 전 | clean | clean | 29/31 | 10 | -0.287932 (-0.481382, -0.118460) |

guest arm의 contradiction 5건은 decompose LLM 지터의 기준선이다. rerank_only의 3건은
기준선 이하이므로 프로필발 모순 신호가 아니고 CI도 0을 포함한다. 반면 both의 contradiction
10건과 29/31건 axis leakage, 음수로 분리된 ΔNDCG@10 CI는 decompose에 프로필을 넣던 #119
수정 전 동작의 프로필발 회귀 신호다.

rerank_only의 leakage 1건(`buy-pers-0001`)은 회원과 게스트의 decompose 프롬프트가 바이트
동일한 #223 불변식 아래에서 발생했다. 따라서 프로필 유출이 아니라 확률적 LLM 지터이며,
leakage 지표는 repeats>1 없이 단건만으로 유출을 단정할 수 없다.

이 baseline은 실제 LLM을 사용한 비결정 산출물이라 재실행 시 수치가 달라질 수 있다.
