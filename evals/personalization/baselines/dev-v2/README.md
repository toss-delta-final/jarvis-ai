# personalization dev-v2 baseline

- command: `uv run python -m evals.personalization --out evals/personalization/baselines/dev-v2`
- dataset: 골든셋 v2.1.0(#333 Part 2 라운드2), `datasetHash`
  `d16eb0e98e28486e2e63a7218cb8b25a96f9ebe69dbe0a37c5b205a16f01efb1`(`run_manifest.json`에
  실제로 실려 있다 — 값이 바뀌면 이 baseline은 재생성 대상이다)
- matrix: 5 arms × 5 profile weights = 25 cells; hard-filter violations 0
- Tier D preference derivation: `case-oracle-distractor-v1`(dev-v1과 동일)
- dev-v1(31건, datasetHash 별도)은 이력 보존을 위해 그대로 둔다 — 서로 다른 datasetHash의
  점수는 직접 비교하지 않는다(GUIDE.md 원칙).

## 기본 weight 0.15

| arm | nDCG@10 | diversity |
|---|---:|---:|
| guest | 0.461219 | 0.840278 |
| member_no_profile | 0.429822 | 0.842593 |
| clean | 0.734220 | 0.746528 |
| noisy | 0.628061 | 0.769676 |
| repeated | 0.589522 | 0.822917 |

주 비교는 `clean_vs_member_no_profile`(+0.304398 nDCG@10)로 identity·최근구매 조건은 같고
profile만 다르다. `member_no_profile_vs_guest`(-0.031397)는 identity·최근구매 효과가 섞인
cold-start 보조 비교이며 profile 효과로 해석하지 않는다. `noisy_vs_clean`(-0.106159)과
`repeated_vs_clean`(-0.144698)은 과반영 민감도 비교다.

| profile weight | clean | noisy | repeated |
|---:|---:|---:|---:|
| 0.000 | 0.429822 | 0.429822 | 0.429822 |
| 0.075 | 0.612344 | 0.560000 | 0.552793 |
| 0.150 | 0.734220 | 0.628061 | 0.589522 |
| 0.300 | 0.843274 | 0.721964 | 0.702150 |
| 0.600 | 0.853948 | 0.792663 | 0.740043 |

clean→noisy ΔNDCG@10 CI는 `[-0.139443, -0.075280]`, 사전 선언 margin 0.03 기준 verdict는
**`regression`**이다(dev-v1의 `inconclusive`와 다르다 — **조작하지 않고 실측 그대로 기록**한다.
v2 골든셋은 실제 하드 네거티브로 candidate depth가 30으로 채워져(v1은 대부분 후보 20건 미만)
noisy arm이 실제로 정답을 밀어내는 효과가 더 뚜렷하게 드러난 것으로 해석한다 — 계측기가
좋아져 드러난 결과다). forbidden/recent 신규 유입과 hard-filter 위반은 0건이다. Tier D intent
contradiction 0건은 scripted decompose가 expectedFilters를 반환하는 구조적 불변식일 뿐 #119
안전성의 실제 증거가 아니다(dev-v1과 동일한 한계).

각 manifest는 personalization 모듈·eval_config·profile fixture SHA-256을 포함한다.
