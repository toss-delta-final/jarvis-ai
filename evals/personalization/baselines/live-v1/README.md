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

## [#484] arm 의미 변경 — 이 baseline 의 수치를 이후 실행과 직접 비교하지 말 것

이 baseline 을 낼 때 `clean_rerank_only`·`clean_both` 는 `fixtures/profiles.json` 의 **케이스
무관 고정 프로필 하나**("Sony 이어폰 / 3~5만원 / 평점 4.5+")를 dev 전 케이스에 먹였다. 따라서
위 표의 `-0.056445` 는 "좋은 프로필의 효과"가 아니라 **"질의와 무관한 프로필의 효과"** 다.

\#484 이후 같은 이름의 arm 은 **케이스별로 파생·렌더한 프로필**을 받는다. 이름은 같지만 다른
것을 재므로 위 수치와 나란히 놓으면 오독이다. 전후를 비교하려면 고정 프로필을 그대로 쓰는
`clean_fixed` arm 을 **같은 실행 안에서** 함께 돌려야 한다:

```
uv run python -m evals.personalization --live \
  --arms guest,clean_rerank_only,clean_both,clean_fixed --out <dir>
```

또한 이후 실행의 `comparison.json` 은 프로필 신호가 있는 케이스만 모은
`slices.profile_signal` 을 함께 낸다 — dev 109건 중 35건은 후보에 grade≥2 정답이 없어 프로필이
비고, 그 케이스까지 섞은 전체 평균은 오라클 천장을 0쪽으로 희석한다.
