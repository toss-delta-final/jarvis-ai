# personalization dev-v2 baseline

- command: `uv run python -m evals.personalization --out evals/personalization/baselines/dev-v2`
- dataset: 골든셋 v2.3.0, `datasetHash`
  `675520d999dc1fbf0a4b32e13914205bc61c606c9adc2f65833eb67fc133ae50`(`run_manifest.json`에
  실제로 실려 있다 — 값이 바뀌면 이 baseline은 재생성 대상이다). dev 109건.
- matrix: 5 arms × 5 profile weights = 25 cells; hard-filter violations 0
- Tier D preference derivation: `case-oracle-distractor-v1`(dev-v1과 동일)
- dev-v1(31건, datasetHash 별도)은 이력 보존을 위해 그대로 둔다 — 서로 다른 datasetHash의
  점수는 직접 비교하지 않는다(GUIDE.md 원칙).

## [2026-08-10, #361] 재생성 — 이전 수치는 다른 케이스 집합의 것이었다

직전 판(dev 96건)은 **#333 작업 도중의 더러운 워킹트리**(`run_manifest.dirty: true`, 이 저장소
히스토리에 없는 `commitSha`)에서 생성돼, 그 PR의 최종 dev 집합(103건)조차 반영하지 못했다.
이후 #474(`bbbf7159`)가 색상 동의어 케이스 6건을 더해 109건이 됐다.

`load_cases("dev")`에는 필터가 없어 `caseCount`가 곧 dev 파일 줄 수다 — 96 → 109는 **분모가
바뀐 것**이며 품질 저하가 아니다. 서로 다른 케이스 집합의 nDCG는 비교 대상이 아니다.

| arm | nDCG@10 (96건) | nDCG@10 (109건) |
|---|---:|---:|
| guest | 0.461219 | 0.448425 |
| member_no_profile | 0.429822 | 0.428237 |
| clean | 0.734220 | 0.686380 |
| noisy | 0.628061 | 0.567170 |
| repeated | 0.589522 | 0.526982 |

verdict는 셋 다 그대로다(`cleanNoisyDrop: regression`, forbidden/intent `pass`) — 기존 eval
게이트가 **verdict 문자열만** 비교했기 때문에 이 드리프트가 드러나지 않았다. 그래서 같은 PR이
`tests/eval/test_personalization_eval.py`에 수치 게이트를 추가한다(REQ-PGRAPH-114).

생성 플랫폼이 `Linux-…WSL2`에서 `Windows-11`로 바뀌었다. 수치 게이트는 manifest의 `platform`을
보지 않으며 허용 오차(`1e-6`)가 플랫폼 간 ulp 차를 흡수한다.

## 기본 weight 0.15

| arm | nDCG@10 | diversity |
|---|---:|---:|
| guest | 0.448425 | 0.853211 |
| member_no_profile | 0.428237 | 0.854230 |
| clean | 0.686380 | 0.755352 |
| noisy | 0.567170 | 0.780836 |
| repeated | 0.526982 | 0.826707 |

주 비교는 `clean_vs_member_no_profile`(+0.258142 nDCG@10)로 identity·최근구매 조건은 같고
profile만 다르다. `member_no_profile_vs_guest`(-0.020188)는 identity·최근구매 효과가 섞인
cold-start 보조 비교이며 profile 효과로 해석하지 않는다. `noisy_vs_clean`(-0.119210)과
`repeated_vs_clean`(-0.159397)은 과반영 민감도 비교다.

| profile weight | clean | noisy | repeated |
|---:|---:|---:|---:|
| 0.000 | 0.428237 | 0.428237 | 0.428237 |
| 0.075 | 0.542194 | 0.506259 | 0.489514 |
| 0.150 | 0.686380 | 0.567170 | 0.526982 |
| 0.300 | 0.793383 | 0.665877 | 0.635438 |
| 0.600 | 0.803591 | 0.719790 | 0.688330 |

clean→noisy ΔNDCG@10 CI는 `[-0.148908, -0.092666]`, 사전 선언 margin 0.03 기준 verdict는
**`regression`**이다(dev-v1의 `inconclusive`와 다르다 — **조작하지 않고 실측 그대로 기록**한다.
v2 골든셋은 실제 하드 네거티브로 candidate depth가 30으로 채워져(v1은 대부분 후보 20건 미만)
noisy arm이 실제로 정답을 밀어내는 효과가 더 뚜렷하게 드러난 것으로 해석한다 — 계측기가
좋아져 드러난 결과다). forbidden/recent 신규 유입과 hard-filter 위반은 0건이다. Tier D intent
contradiction 0건은 scripted decompose가 expectedFilters를 반환하는 구조적 불변식일 뿐 #119
안전성의 실제 증거가 아니다(dev-v1과 동일한 한계).

각 manifest는 personalization 모듈·eval_config·profile fixture SHA-256을 포함한다.
