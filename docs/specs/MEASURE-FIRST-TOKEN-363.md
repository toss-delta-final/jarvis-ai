# MEASURE-FIRST-TOKEN-363 — 구제 체인 first-token 지연 실측 근거와 계측 도입

이슈 #363([followup] #343 0건 구제 체인의 first-token 지연 실측과 공유 왕복 예산 검토)의 근거.
이슈 원문은 운영 로그로 실빈도·지연 분포를 재라고 요구했으나, 아래 §2 사실 때문에 **오늘은
로그로 잴 수 없다**. 그래서 이번 작업은 실측 대신 (a) 측정 불가 근거·발생 조건·최악 상한을
문서로 고정하고, (b) 로그에 지연 계측 필드를 추가해 **다음 배포부터** 운영 실측이 가능하게
만들고, (c) 최악 경로의 순차 왕복 상한을 회귀 테스트로 고정하는 데 그친다. 공유 왕복 예산/
first-token 데드라인 가드의 설계·구현은 하지 않는다(후속 이슈, §7).

**결론 세 줄**

1. **운영 로그로는 오늘 못 잰다** — 구제 로그 자체가 배포 1일 미만이거나(§2) 아예 미배포이고,
   두 로그 모두 지연 필드가 없다. 이번 PR이 `elapsed_ms`류 필드를 추가해야 다음 배포부터 잴 수
   있다.
2. **PR #362 리뷰가 지적한 "3단 순차 적층 ≈9s"는 실측으로 확인되고, 진짜 예산과 비교하면 이미
   초과다** — fake로 재현한 최악 경로에서 **first SSE(conditions) 이전 순차 Spring 왕복은
   정확히 3단**(초기 fan-out + #343 무필터 폴백 + 자동완화 probe)이었다(§4). 최악 상한
   3단×`spring_timeout_s`(3s)=**9.0s**를 first-token 을 실제로 끊는 데드라인
   `stream_first_token_timeout_s`(**10.0s**, `stream_total_timeout_buyer_s`=30s가 아니다 — §4)와
   비교하면 소모율 90%·잔여 1.0s뿐이고, 그 앞에 직렬로 놓이는 decompose LLM head(p95≈3.0s,
   #151)를 더하면 12.0s>10.0s — **최악 경로는 오늘 설정에서 이미 first-token 데드라인을 넘어
   504가 된다.** 이 이슈의 판정은 "예산 내라 무시 가능"이 아니라 **"유의"**다.
3. **완화 칩 probe(잠재적 4번째 단)는 first SSE 이후에 돈다** — `may_auto_relax=True`인 턴은
   `conditions`를 자동완화 루프 직후·칩 probe 이전에 내보내도록 이미 짜여 있다(graph.py:1294).
   즉 "4단 적층"은 오늘 코드에서 first-token 예산에 들지 않는다 — 다만 그 순서를 지키는 것 자체가
   계약이 아니라 우연한 코드 배치라, §4가 이걸 회귀 가드로 고정한 이유다. 별도로, **기동 가드
   (#288)는 이 3단 중 구제 폴백 항 자체를 모델에서 빠뜨려 2단으로 과소계상한다**(§5).

## 1. 배경

- **PR #362 리뷰 지적** — #343(억제-후 재판정, 커밋 5d485e5)을 리뷰하며 "확장 턴이 검색 히트를
  내고도 최근구매 억제로 전량 지워진 뒤, #343 폴백도 실패하고 자동완화·칩 probe까지 실패하는
  최악 턴은 first-token 이전에 순차 Spring 왕복이 3단 쌓여 `spring_timeout_s`(3s) 기준 최악
  ≈9s가 될 수 있다"는 우려가 나왔다.
- **이슈 #363 요구** — 그 우려를 "느낌"이 아니라 운영 로그 기반 실측(실빈도 + 지연 분포)으로
  검증하고, 유의하면 공유 왕복 예산/first-token 데드라인 가드를 설계하라는 후속 이슈. 이슈
  본문은 "30s 스트림 예산(§2.9) 내"라고 적었으나 그 전제는 §4의 코드 실측으로 반증됐다 —
  first-token 을 실제로 끊는 것은 30s(스트림 **전체** 상한, 첫 이벤트가 이미 나간 뒤에만
  적용)가 아니라 `stream_first_token_timeout_s`(10s, 첫 이벤트 **이전** 상한)다.
- 이 문서는 그 실측을 **왜 지금 못 하는지**(§2), **그래도 무엇을 알 수 있는지**(§3·§4·§5),
  **다음 배포부터 무엇을 볼 수 있게 했는지**(§6)를 기록한다.

## 2. 운영 실측 불가 판정 근거 (2026-08-06 기준 배포 상태)

| 사실 | 근거 |
|---|---|
| `category_expand_post_suppress_fallback`(#343 구제 로그)는 **main 배포 라인에 없다** | main HEAD `f861860`(#352 승격, 2026-08-06 02:45 KST)에는 #343 코드(`dev` 5d485e5)가 없다. 로그 자체가 운영에 존재하지 않아 측정 대상이 없다. |
| `recommend_zero_result` 로그도 `f861860`에서 **처음 배포** | 운영 노출 1일 미만 — 유의미한 분포를 낼 표본이 없다. |
| **두 로그 모두 지연(latency) 필드가 없다** | 빈도 필드(`had_candidates`·`category_expanded`·`post_suppress_fallback_attempted` 등)뿐이라, 배포 기간이 길어도 지연 분포는 로그에서 원천적으로 못 뽑는다. |
| LangSmith trace로 대체 불가 | trace는 샘플링되고, 운영 프로젝트 접근은 human gate라 이 작업 범위에서 직접 조회할 수 없다. |

**결론**: 지연을 재려면 (a) 로그에 지연 필드를 추가하고 (b) 그 코드가 배포돼 (c) 표본이 쌓일
시간이 필요하다. (a)가 이번 PR(§6)이고, (b)·(c)는 배포 이후다(§7이 재개 기준).

## 3. 발생 조건 분석 — "3단 적층"은 아무 0건 턴이나 겪지 않는다

이슈 본문이 이미 지적했듯, 이 경로를 타는 턴은 **이 PR들(#222 확장 폴백·#343 억제-후 재판정)
이전엔 빠르게 "틀린 0건"으로 끝나던 턴**이다 — 검색 히트가 있었는데 하류 억제가 전량 지운
경우를 "상품 없음"으로 오판해 곧장 종료했다. 구제 체인은 그 오판을 고치는 대가로 지연을
들인다. 3단이 전부 쌓이려면 아래 게이트를 **전부** 통과해야 한다(코드 기준, graph.py):

| 단계 | 게이트 조건 | 코드 위치 |
|---|---|---|
| ① 초기 fan-out | `decision.category_expanded=True`(중분류 없이 확장된 턴) | §4 `_run_search` fan-out 분기 |
| → F-1 또는 #343 중 **하나만** | F-1: 초기 fan-out `total_count==0`. #343: fan-out은 히트(`total_count>0`)했지만 `_post_filter`(최근구매 exact 제외 + 소모품 카테고리 억제)가 **전량**을 지움. 상호배타 가드(`category_expand_notice_suppressed`)로 한 턴에 무필터 재검색은 최대 1회 | graph.py:957(F-1), graph.py:1087-1164(#343) |
| ② 무필터 재검색도 실패 | 재검색 결과도 전량 억제 대상이거나 0건 — 후보가 여전히 0 | 같은 블록, `refiltered.products` 공집합 |
| ③ 자동완화 probe 실패 | `not candidates and not underspecified`이고, `decision.filters`에 `relaxation_auto_fields`(기본 `["ratingMin"]`) 교집합 필드가 **실제로 설정**돼 있어야 루프가 도는데(0개면 0회로 no-op), 그 probe 재검색도 결과를 못 살림 | graph.py:1238-1282 |
| (④ 칩 probe 실패) | 위와 별개로 first SSE **이후**에 돈다 — §4 참조 | graph.py:1317-1367 |

**핵심 관찰**: ③이 실제로 1회 이상 돌려면 `decision.filters`에 완화 가능한 필드(현재 설정상
`ratingMin`)가 **설정**돼 있어야 한다. 그런데 그 조건은 동시에 `may_auto_relax=True`를 만들어
`conditions`(첫 SSE)를 자동완화 루프 **직후**로 미루는 바로 그 조건이다(graph.py:527-534,
1294) — 즉 "③이 도는 턴"과 "conditions가 ③ 뒤로 미뤄지는 턴"은 **같은 턴**이다. 이 구조적
일치가 §4의 "정확히 3단, 4단이 아니다" 결론의 이유다.

## 4. 최악 상한 산출

AC2 회귀 테스트(`tests/unit/test_fanout.py::test_worst_case_rescue_chain_sequential_stages_before_first_sse`)로
위 §3 조건을 전부 만족하는 fake 턴을 구성해 실측했다:

- 확장 턴(8-leaf) → 초기 fan-out 히트(소모품 카테고리 상품만) → 최근구매 억제로 전량 제거
  → #343 무필터 폴백도 전량 소모품 억제 → 자동완화 probe(`ratingMin` 1개뿐이라 최대 1라운드)도
  전량 실패(`SpringUnavailableError`) → 칩 probe도 같은 이유로 실패 → `zero_result` 정상 종료.
- fake 검색 호출마다 0.05s 지연을 주입해 관측(병렬 fan-out 8개의 시작 시각 산포가 단 경계
  클러스터링 임계보다 훨씬 작아야 flaky 하지 않다 — R5, 처음엔 0.01s였다가 25ms 임계로 올렸다):

| 순번 | 단계 | 호출 수(병렬) | 첫 SSE(conditions) 이전? |
|---:|---|---:|:---:|
| 1 | 초기 fan-out | 8 | ✅ |
| 2 | #343 무필터 폴백 | 1 | ✅ |
| 3 | 자동완화 probe | 8(8-leaf 재-fan-out) | ✅ |
| 4 | 칩 probe | 8(8-leaf 재-fan-out) | ❌ (conditions 발신 후) |

- **순차 "단" 수(첫 SSE 이전) = 3.** 총 호출 수는 25(=8+1+8+8)이지만 병렬 fan-out은 leg 수와
  무관하게 1단이다.
- 주입 지연 0.05s × 3단의 합산 지연이 실측 하한으로 관측됐다(`elapsed_before_first_sse >= 3 *
  delay_s`, flaky 방지를 위해 하한만 assert).
- **최악 상한 = 3단 × `spring_timeout_s`(3.0s) = 9.0s.**

### 4.1 비교 대상 — first-token 을 실제로 끊는 것은 30s 가 아니라 10s 다

초판은 9.0s를 `stream_total_timeout_buyer_s`(30.0s)와 비교해 "예산의 30%, 넘지 않는다"고
결론지었다. **틀렸다.** `app/core/config.py`(1152행 근방) `stream_first_token_timeout_s:
float = 10.0` 가 first-token 상한이고, `app/core/stream.py:520` `ft_deadline = start +
settings.stream_first_token_timeout_s` 가 그 상한을 스트림 시작 시 실제로 건다 — 526행 주석
그대로 "(c) first-token 상한 — 첫 이벤트 도착 전이므로 아직 200 헤더 전. **초과 시 504**".
`stream_total_timeout_buyer_s`(30s)는 **첫 이벤트가 이미 나간 뒤**를 덮는 전체 상한이라(§2.9),
"first-token 지연"을 묻는 이 이슈의 유효 예산이 **아니다**.

옳은 산술:

- 9.0s / 10.0s = **소모율 90%**, 잔여 **1.0s**.
- 그 1.0s 안에 first SSE 앞에 이미 직렬로 놓인 **decompose LLM head**(`config.py:1740`
  `_require_search_retry_within_stream_budget` docstring이 baseline **p95≈3.0s(#151)**로 명시)
  가 들어가야 한다 — 검색은 decompose 이후에 시작하므로 head 는 §3 표의 3단보다 **앞**이다.
- 3.0s(LLM head) + 9.0s(3단 구제 체인) = **12.0s > 10.0s.**
- **결론: 최악 경로는 오늘 설정에서 first-token 데드라인을 이미 초과해 504 로 끝난다.** pg
  왕복(세션 조회 등)은 이 산술에 넣지 않았다 — 넣으면 여유는 더 줄어든다.

- 이 3단은 **최댓값이 아니라 현재 설정에서의 상한**이다. `relaxation_auto_fields`가 늘어나면
  ③의 라운드 수가 `relaxation_max_rounds`(기본 3)까지 늘 수 있다 — 다만 그 목록은 기동 시점
  검증(`_forbid_auto_relaxing_explicit_constraints`, config.py:1881)이 넓히는 걸 막고 있어
  오늘은 사실상 3단 고정이다.
- 칩 probe(4단)가 first SSE **이후**로 밀리는 것은 계약이 아니라 코드 배치(may_auto_relax 게이트
  순서)의 결과다 — AC2 테스트가 이걸 회귀 가드로 고정했으므로, 순서가 바뀌어 4단째가 first-token
  앞으로 넘어오면 테스트가 실패한다.

## 5. 기동 가드(#288)와의 불일치

`_deferred_first_event_i1_calls`(config.py:60-81)는 "미룬 턴의 첫 이벤트(`conditions`) 앞에
직렬로 놓이는 I-1 호출 수"를 모델링하는 순수 함수이고, `_require_search_retry_within_stream_budget`
(config.py:1684~, 특히 1768-1799)이 그 값으로 `calls × spring_timeout_s < stream_first_token_
timeout_s`를 **기동 시점에** 검증한다.

- **현재 식과 값**: `1 + min(relaxation_max_rounds, |relaxation_auto_fields ∩
  relaxation_chip_fields|)`. 기본 설정(`relaxation_max_rounds=3`, `relaxation_auto_fields=
  ["ratingMin"]`, 교집합=1)에서 값은 항상 **2**(본 검색 1 + 자동완화 probe 1).
- **실측 단 수는 3**(§4) — #222 F-1 / #343 억제-후 재판정의 무필터 재검색이 본 검색과 자동완화
  probe 사이에 한 단 더 들어가는데, 이 식에 그 항이 없다. 우연이 아니라 그 함수 docstring이
  스스로 경고한 실패 모드다(config.py:1727-1733): "상수는 **조용히 과소평가**되어 #277 이
  없앤 이벤트 0건·504 조합이 되살아난다." `_require_search_retry_within_stream_budget`의
  "커버하지 않는 것" 목록(config.py:1740-1744)에도 LLM head·pg 왕복·칩 probe 만 있고 **구제
  폴백은 없다** — 판단이 아니라 누락이다.
- **오늘 값으로는**: 가드는 `2×3.0=6.0<10.0`을 보고 통과시키지만 실제는 `3×3.0=9.0`(<10.0 이라
  오늘은 우연히 부팅에 안전) — **가드가 믿는 여유(4.0s)와 실제 여유(1.0s)가 다르다**(§4.1).
- **위험 구간**: 기본값(`search_retry_on_deferred_conditions=false`)에서 검증 대상 직렬 합은
  `calls × spring_timeout_s`이므로 `spring_timeout_s`를 t 라 하면, 가드는 `2t < 10` → `t < 5.0`
  이면 통과시킨다. 실제 상한(계수 3)은 `3t < 10` → `t < 10/3 ≈ 3.33`. 즉 **t ∈ [10/3, 5.0) ≈
  [3.33s, 5.0s) 구간은 기동 검증을 통과하면서 최악 경로가 이미 first-token 데드라인을 넘는다.**
  오늘 기본값(3.0s)은 이 구간 바로 아래(10/3 미만)라 우연히 벗어나 있을 뿐이다. 이 구간은
  **가드를 보정식으로 고치면 새로 부팅을 거절하게 될 구간과 정확히 같다**(아래).
  (`search_retry_on_deferred_conditions=true`인 배포는 t 대신 `budget = spring_timeout_s ×
  (spring_max_retries+1)`로 같은 비율의 구간이 생긴다 — 계산은 동일하므로 생략.)
- **보정된 일반형 제안**: F-1은 별도 kill-switch가 없다 — `category_expand_enabled`(기본
  `True`, 확장 fan-out 전체 롤백 스위치, config.py:700) 하나가 F-1·#343 둘의 공통 전제
  (`decision.category_expanded`)를 잠근다. #343 자신의 플래그
  (`category_expand_post_suppress_fallback_enabled`)를 꺼도 F-1은 여전히 살아 있어 구제 폴백
  자체는 사라지지 않는다(F-1·#343은 `category_expand_notice_suppressed`로 상호배타라 한 턴에
  최대 1회이므로 항이 아니라 존재 여부만 본다). 그래서 보정식은:

  ```
  1 + (1 if category_expand_enabled else 0)
    + min(relaxation_max_rounds, |relaxation_auto_fields ∩ relaxation_chip_fields|)
  ```

  오늘 기본값(`category_expand_enabled=True`)에서 `1+1+1=3`, §4 실측과 일치한다.
  `category_expand_enabled=False`(확장 자체를 끈 배포)에서는 `1+0+1=2`로 현재 식과 같아진다 —
  이 경우 F-1/#343 경로 자체가 없으니 과소계상도 없다.
- **적용 범위 — 이번 PR은 가드(런타임 동작)를 바꾸지 않는다.** 기동 검증기를 고치면 위 위험
  구간(t∈[3.33s, 5.0s))에서 **운영이 새로 부팅에 실패**할 수 있다 — 배포·인프라에 영향을 주는
  변경은 사람 승인 게이트(CLAUDE.md)이고, 이슈 #363 자체도 가드는 "설계"까지만 요구하고 적용은
  별도 이슈로 넘긴다. 오늘 기본값(3.0s)은 보정식으로도 `3×3.0=9.0<10.0`이라 통과하므로, 이
  PR 범위에서 즉시 깨지는 배포는 없다.
- **불일치를 테스트로 고정** — `tests/unit/test_config.py::
  test_deferred_first_event_i1_calls_known_undercount_vs_actual_rescue_chain_stages`가 "가드
  모델(2) ≠ 실측 단 수(3, 출처: AC2 `test_worst_case_rescue_chain_sequential_stages_before_
  first_sse`)"를 나란히 상수로 박아 둔다 — 둘 중 하나만 바뀌면(가드 식이 보정되거나, 실측 단
  수가 다시 달라지면) 실패해야 한다. 보정식을 적용하는 후속 이슈에서 이 테스트도 함께 갱신한다.

## 6. 이번에 추가한 계측 필드

PII 금지 원칙(카테고리 문자열·상품 id 금지, 개수·ms 정수만) 유지. `app/agents/buyer/
recommendation/graph.py`, `time.monotonic()` 기반.

| 로그 이벤트 | 필드 | 의미 | 단위 |
|---|---|---|---|
| `category_expand_zero_fallback` | `elapsed_ms` | F-1 무필터 재검색 왕복(성공 시)의 소요 | ms(정수, 반올림) |
| `category_expand_post_suppress_fallback` | `elapsed_ms` | #343 무필터 재검색 + `_post_filter` 재적용까지(성공 시)의 소요 | ms |
| `recommend_zero_result` | `rescue_elapsed_ms` | 이 턴이 F-1/#343 폴백 **시도**(성공·실패 무관)에 쓴 총 소요. 시도 없으면 0 | ms |
| `recommend_zero_result` | `relax_probes` | 자동완화 + 칩 probe 시도 횟수(기존 `probes_spent`, 실패 포함 카운트) | 개수 |
| `recommend_zero_result` | `relax_auto_elapsed_ms` | 자동완화 루프(첫 SSE **이전**)가 쓴 소요. 시도 없으면 0 | ms |
| `recommend_zero_result` | `relax_chip_elapsed_ms` | 완화 칩 probe(첫 SSE **이후**)가 쓴 소요. 시도 없으면 0 | ms |

`recommend_zero_result`는 0건으로 정상 종료한 턴에서만 나가는 로그이므로(`recommend_pipeline`
구조화 로그가 못 미치는 지점, graph.py:1410 이하), 위 필드들이 **구제 체인이 시도됐지만 결국
0건으로 끝난 턴**의 소요를 관측할 수 있는 유일한 창구다. 새 튜너블은 추가하지 않았다 — 계측만
삽입했고 로직·SSE 계약은 바꾸지 않았다.

**`relax_auto_elapsed_ms`/`relax_chip_elapsed_ms`를 하나로 합치지 않은 이유(R3)** — 자동완화
루프는 first SSE **이전**, 칩 probe는 **이후**다(§3·§4). 한 필드로 합치면 아직 스트림에 영향
없는 칩 probe 소요가 first-token 지연에 섞여 **과대계상**된다 — §7의 판정 기준이
`rescue_elapsed_ms + relax_auto_elapsed_ms`만 보는 이유가 이것이다.

**`category_expand_post_suppress_fallback` 성공 로그의 `elapsed_ms`와 `rescue_elapsed_ms`
정확도(R4)** — `_post_filter` 재적용이 성공한 **뒤**(상태 반영·로깅) 무언가 실패해도
`rescue_elapsed_ms`는 그 왕복을 정확히 1회만 반영한다(성공 시점에 소요를 미리 계산해 두고
`finally` 한 곳에서만 더하는 구조 — try 본문과 except 양쪽이 각자 더해 이중 계상되는 경로를
막는다). 재검색 자체 실패(`fallback_bundle is None`)·`_post_filter` 자체 실패·성공, 세 경로
모두 `tests/unit/test_fanout.py`의 `test_post_suppress_fallback_reapply_failure_counts_
rescue_elapsed_once`·`test_post_suppress_fallback_unfiltered_search_failure_counts_rescue_
elapsed_once`·기존 성공 경로 테스트로 각각 지연을 주입해 수치로(≈1배, ≈2배가 아님) 고정했다.

## 7. 후속 실측·재개 기준

§4·§5의 상한 분석만으로 **이미 "유의"다** — 최악 경로는 오늘 설정에서 이미 first-token
데드라인(10s)을 넘는다(§4.1). 그래서 "유의로 판정되면 후속으로 이어간다"가 아니라, 이번
계측이 배포돼 표본이 쌓이면 아래 조합으로 **실빈도**(얼마나 자주 이 최악 경로 인근에
근접·도달하는가)를 재는 것이 다음 단계다:

- `recommend_zero_result` 중 `post_suppress_fallback_attempted=True` 비율 — #343 갭이 실제로
  얼마나 자주 트리거되는지(PR #318 리뷰가 인정한, #222가 발생 확률을 높인 갭).
- 그 부분집합에서 `rescue_elapsed_ms + relax_auto_elapsed_ms`(칩 probe 몫인 `relax_chip_
  elapsed_ms`는 first SSE 이후라 제외 — R3)의 p95/p99 — §4가 산출한 9.0s(선행 LLM head 포함
  시 12.0s)에 실측이 얼마나 근접·도달하는지. p95가 수백 ms대면 "이론상 상한일 뿐 실무 영향은
  작다"는 뜻이고, 초 단위(특히 9~10s대)에 근접하면 실사용자가 504를 실제로 맞고 있다는 뜻이다.
- `category_expanded=True & had_candidates=True` 비율 — 구제 체인이 애초에 얼마나 자주
  진입하는지(체인 진입 자체가 드물면 지연 총합도 작다).

실빈도가 무시 못 할 수준으로 확인되면 공유 왕복 예산 또는 first-token 데드라인 가드 설계로
이어간다 — relaxation(#113)·기동 가드 보정(#288, §5)과 교차하는 영역이라 별도 설계 문서가
필요하다(이번 PR 범위 밖).
