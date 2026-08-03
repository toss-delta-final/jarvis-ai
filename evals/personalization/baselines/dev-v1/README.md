# personalization dev-v1 baseline

- command: `uv run python -m evals.personalization --out evals/personalization/baselines/dev-v1`
- dataset: dev 31건, nDCG ranking 분모 18건(제외 13건은 지표별 denominator에 사유 기록)
- matrix: 5 arms × 5 profile weights = 25 cells; hard-filter violations 0
- Tier D preference derivation: `case-oracle-distractor-v1`

Tier D `clean`은 각 케이스의 grade≥2 정답 상품 category/brand 분포에서 파생한
**oracle-aligned 상한**이다. `noisy`는 실제 후보 내 distractor 축을 0.8로 섞고,
`repeated`는 clean 최상위 축만 유지하며 나머지를 0.2배로 감쇠해 빈도 편향을 모사한다.
따라서 이 baseline은 운영 프로필의 절대 품질 추정치가 아니라 profile 신호가 맞거나 흐트러질 때의
민감도·상한·하락을 비교하는 counterfactual이다.

## 기본 weight 0.15

| arm | nDCG@10 | diversity |
|---|---:|---:|
| guest | 0.699745 | 0.705018 |
| member_no_profile | 0.628377 | 0.709319 |
| clean | 0.830253 | 0.666308 |
| noisy | 0.777236 | 0.666308 |
| repeated | 0.713641 | 0.709319 |

주 비교는 `clean_vs_member_no_profile`(+0.201876 nDCG@10)로 identity·최근구매 조건은 같고
profile만 다르다. `member_no_profile_vs_guest`(-0.071368)는 identity·최근구매 효과가 섞인
cold-start 보조 비교이며 profile 효과로 해석하지 않는다. `noisy_vs_clean`(-0.053017)과
`repeated_vs_clean`(-0.116612)은 과반영 민감도 비교다.

| profile weight | clean | noisy | repeated |
|---:|---:|---:|---:|
| 0.000 | 0.628377 | 0.628377 | 0.628377 |
| 0.075 | 0.737736 | 0.702101 | 0.681474 |
| 0.150 | 0.830253 | 0.777236 | 0.713641 |
| 0.300 | 0.871472 | 0.835049 | 0.787797 |
| 0.600 | 0.869446 | 0.854220 | 0.819429 |

clean→noisy ΔNDCG@10 CI는 `[-0.095486, -0.017239]`, 사전 선언 margin 0.03 기준 verdict는
`inconclusive`다(측정 후 margin 변경 없음). forbidden/recent 신규 유입과 hard-filter 위반은 0건이다.
Tier D intent contradiction 0건은 scripted decompose가 expectedFilters를 반환하는 구조적 불변식일
뿐 #119 안전성의 실제 증거가 아니다. 정본은 `--live`의 guest / clean_rerank_only / clean_both
scope paired 결과와 축 이름 전용 `filterAxisLeakage` 산출물이다.

각 manifest는 personalization 모듈·eval_config·profile fixture SHA-256을 포함한다. `run` 섹션만
실행 인스턴스 메타로 normalize하고, 다른 out/env/시각으로 재실행한 나머지 artifact를 byte 비교한다.
