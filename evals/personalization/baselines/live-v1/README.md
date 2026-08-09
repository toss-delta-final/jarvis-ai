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
MODEL_EVAL_MAX_CALLS_PER_RUN=4000 uv run python -m evals.personalization --live \
  --arms guest,member_no_profile,clean_rerank_only,clean_both,clean_fixed --out <dir>
```

또한 이후 실행의 `comparison.json` 은 프로필 신호가 있는 케이스만 모은
`slices.profile_signal` 을 함께 낸다 — dev 109건 중 35건은 후보에 grade≥2 정답이 없어 프로필이
비고, 그 케이스까지 섞은 전체 평균은 오라클 천장을 0쪽으로 희석한다.

## [#483] 기준선 교체 — 위 표의 `guest 대비 ΔNDCG@10` 은 프로필 효과가 아니다

위 표의 기준선 `guest` 는 비교 arm 과 **프로필만 다른 게 아니라 identity 까지 다르다** —
`persona_id` 가 없어 I-19 구매이력 조회와 재구매 dedup 이 통째로 빠진다. 그래서
`clean_rerank_only` 의 `-0.056445` 는 **프로필 효과 + identity 효과의 합**이다. 이 산출물을
케이스 라벨로 갈라 보면 그 사실이 드러난다(순위 지표 산출 가능 26건):

| 그룹 | n | 평균 ΔnDCG@10 |
|---|---:|---:|
| `guest` 라벨 (persona_id 없음 → identity 혼입 없음) | 9 | **+0.0135** |
| `member` 라벨 | 8 | **−0.1258** |

member 쪽 하락은 `repurchase` 3건이 만든 것이고, 그 3건을 빼면 전체 평균이 −0.0340 →
−0.0106 으로 0 에 수렴한다. 즉 위 헤드라인의 절반 이상이 프로필이 아니라 dedup 효과다.

\#483 이후 주 비교는 identity 를 맞추고 프로필만 뺀 **`member_no_profile`** 기준선으로 옮겼다.
`comparison.json` 스키마가 함께 달라진다:

| 필드 | 의미 |
|---|---|
| `pairedVsMemberNoProfile` | **주 비교** — 프로필 순효과 |
| `pairedVsGuest` | 보조 비교(cold-start). identity 가 섞이므로 프로필 효과로 해석하지 않는다 |
| `baselineArm` / `secondaryBaselineArm` / `primaryComparison` | 어떤 기준선으로 계산했는지 |

`rankingChange`·`axisLeakage` 도 주 기준선을 따라간다. 특히 `axisLeakage["guest"]` 는 예전에는
자기 자신과의 비교라 늘 비어 있었지만, 이제는 **프로필 없이도 이만큼 흔들린다**는 지터 바닥이
된다 — 위 21~23행이 손으로 하던 해석("leakage 1건은 유출이 아니라 지터")을 지표가 스스로 낸다.

### 예산 — 실행에 env override 가 필요하다

호출 수 상한(기본 800)은 비용이 아니라 폭주 방어용 안전판이고, 예산은 튜너블이 아니라 운영
안전장치라 기본값을 올리지 않는다(`evals/model_eval/README.md`). preflight 은 case-run 당 3호출로
보수 추정하므로 **기본 4-arm × dev 109건 × repeats 3 = 3,924호출**이 필요하다:

```
MODEL_EVAL_MAX_CALLS_PER_RUN=4000 uv run python -m evals.personalization --live \
  --repeats 3 --out <dir>
```

비용 상한 $20 은 걸리지 않는다 — 보수 추정으로도 $3.92 이고 live-v1 실측 단가(≈$0.000478/call)
로는 ≈$1.9 다. (참고: dev 가 31건에서 109건으로 늘어난 시점부터 3-arm 기본 실행조차 981호출로
상한을 넘고 있었다 — override 요구는 이 이슈가 새로 만든 제약이 아니다.)
