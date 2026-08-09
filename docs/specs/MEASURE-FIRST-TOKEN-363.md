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
- **적용 범위 — #383 에서 적용 완료.** `_deferred_first_event_i1_calls`(순수 함수,
  config.py)가 위 보정식으로 바뀌었고 `_require_search_retry_within_stream_budget`
  (config.py, `Settings` 모델 검증기)이 `category_expand_enabled=self.category_expand_
  enabled`를 넘긴다. 위 위험 구간
  (t∈[3.33s, 5.0s))은 이제 **기동 거절 구간**이다 — 배포·인프라 영향 검토(CLAUDE.md 사람 승인
  게이트)는 오케스트레이터가 실측으로 끝냈다: `.github/workflows/deploy.yml`(77-113행)이 운영
  env 파일을 매 배포마다 고정 키 목록으로 전면 재작성하는데, 그 목록에 `SPRING_TIMEOUT_S`·
  `STREAM_FIRST_TOKEN_TIMEOUT_S`·`CATEGORY_EXPAND_ENABLED`·`RELAXATION_*` 는 하나도 없다 —
  운영은 코드 기본값(`spring_timeout_s=3.0`)으로 돈다. 오늘 기본값은 보정식으로 `3×3.0=9.0
  <10.0`이라 통과하므로, 이 적용으로 새로 부팅에 실패하는 배포는 없다.
- **구제 폴백 항은 재시도 억제 대상이 아니다(#383 R5, PR #414 Claude 리뷰 실측 확인)** —
  `graph.py::stream_recommendation` 에서 `spring_client.suppress_search_retry()` 로
  재시도를 끄는 `with` 블록은 저장소 전체에 본 검색(`asyncio.gather` 호출)과 자동완화
  probe(`_probe(cand)`) 를 감싼 두 곳뿐이고, F-1/#343 구제 재검색(같은 함수의
  `_run_search_unfiltered()` 호출 두 곳 — F-1 폴백·억제-후 재판정)은 그 블록 밖이라
  `spring_client.py::search` 의 `settings.spring_max_retries + 1` 을 그대로 받아 항상
  재시도한다. 그래서 세 항을 균질하게 `spring_timeout_s` 로 값 매기면(`deferred_calls ×
  spring_timeout_s`) 구제 항 하나를 과소평가한다 — `_require_search_retry_within_stream_
  budget` 의 가드 OFF 분기는 이제 `suppressed_calls × spring_timeout_s + rescue_calls ×
  budget`(`budget = spring_timeout_s × (spring_max_retries+1)`)로 항별로 나눠 잰다. **위
  위험 구간 산술도 `spring_max_retries` 에 따라 달라진다** — `spring_max_retries=0`(오늘
  기본값, #394)이면 `budget == spring_timeout_s` 라 위 §5 산술이 그대로 성립하지만,
  `spring_max_retries=1`(허용 상한)이면 실제 직렬 합은 `2t + 1×(t×2) = 4t` 로 늘어나
  위험 구간이 `t ∈ [2.5, 5.0)`로 넓어진다(`.env.example` 이 한때 `SPRING_MAX_RETRIES=1`
  을 예시로 실었던 것과 기본 `spring_timeout_s=3.0`이 만나면 실제로 걸렸다 — 예시 값을
  코드 기본값 0으로 정정했다).
- **불일치를 일치로 고정** — `tests/unit/test_config.py::
  test_deferred_first_event_i1_calls_matches_actual_rescue_chain_stages`(#383, 종전
  `..._known_undercount_vs_actual_rescue_chain_stages`를 개명·정정)가 이제 "가드 모델(3) ==
  실측 단 수(3, 출처: AC2 `test_worst_case_rescue_chain_sequential_stages_before_first_sse`)"를
  일치로 고정한다 — 둘 중 하나만 어긋나면(가드 식이 다시 과소계상하거나, 실측 단 수가 달라지면)
  실패해야 한다.
- **#384(`docs/specs/DESIGN-SHARED-BUDGET-384.md`, PR #417) 정합 — 두 사실을 갱신한다**(그
  문서 자체는 다른 레인 소유라 여기서 고치지 않는다):
  1. §1(d) 각주①이 지적한 **재시도 억제 스코프 비대칭**(구제 1단은 `suppress_search_retry()`
     밖이라 재시도가 살아 있다)을 **이 PR(R5)이 이미 반영했다** — 위 "구제 폴백 항은 재시도
     억제 대상이 아니다" 항목이 그 대응이다. #384 D7 이 인용한 "#383의 제안식은 이 비대칭을
     반영하지 않는다"는 서술은 **이 PR 이후로는 해소된 상태**다.
  2. #384 §1(a)가 재기준선한 사실(#396 progress 상시화로 첫 SSE 가 `conditions` 아닌
     `progress` 라 first-token 관문을 그 프레임이 먼저 충족한다)로, **위 §4.1의 "최악 경로는
     오늘 이미 first-token 을 넘어 504" 결론은 더 이상 유효하지 않다** — `_require_search_
     retry_within_stream_budget` docstring의 "구매자 progress 이벤트는 #396 이 이미 구현했다"
     단락(#383 R3 정정)이 같은 사실을 이미 반영해 뒀다.

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
| `recommend_zero_result` | `may_auto_relax` | 아래 참조 — 두 로그 공통 | bool |
| `recommend_pipeline` | `rescue_elapsed_ms`/`relax_auto_elapsed_ms`/`relax_chip_elapsed_ms` | **구제·완화가 성공해 0건이 아닌 채 종결된 턴**의 같은 값(정의는 `recommend_zero_result`와 동일 변수) | ms |
| `recommend_pipeline` | `may_auto_relax` | `False`면 `conditions`가 검색 **이전**에 이미 나가(graph.py:545) 위 소요가 first-token을 전혀 늦추지 않는다 — 판정 시 `True`인 턴만 봐야 한다(§7) | bool |

`recommend_zero_result`는 0건으로 정상 종료한 턴에서만 나가는 로그이고(`if not candidates:`
분기, graph.py:1410, 1462행에서 `return`), `recommend_pipeline`은 그 분기를 타지 않은(=구제나
완화가 성공해 후보가 채워진) 턴에서만 나간다(graph.py:1781~) — **한 턴은 둘 중 정확히 하나만
남긴다(상호 배타, 이중 계상 없음). 두 로그의 합집합이 "구제 체인이 관여한 턴 전수"다.** R7 이전
초판은 계측 필드를 `recommend_zero_result`에만 넣어 "구제를 시도했지만 결국 0건"인 턴만
관측되고, **구제가 실제로 통해 지연된 첫 토큰이라도 결과를 받은 턴**(이 이슈가 재려는 가장
중요한 표본)은 값이 계산만 되고 로그로 남지 않는 결함이 있었다 — Claude PR Review(#379)가
지적해 `recommend_pipeline`에도 같은 세 필드 + `may_auto_relax`를 추가했다. 새 튜너블은
추가하지 않았다 — 계측만 삽입했고 로직·SSE 계약은 바꾸지 않았다.

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
- **1급 관측 대상(R7)** — `recommend_zero_result`·`recommend_pipeline` 두 로그를 **합쳐서**,
  `may_auto_relax=True`인 턴만 골라 `rescue_elapsed_ms + relax_auto_elapsed_ms`(칩 probe 몫인
  `relax_chip_elapsed_ms`는 first SSE 이후라 제외 — R3)의 p95/p99를 본다. `may_auto_relax=
  False`인 턴은 conditions가 검색 이전에 이미 나가 이 소요가 first-token을 전혀 늦추지 않으므로
  섞으면 분포가 실제보다 완화돼 보인다. **0건으로 끝난 턴만 보면 이슈가 재려는 절반(구제가
  실제로 통한 턴)이 빠진다** — `recommend_pipeline`도 반드시 포함한다. §4가 산출한 9.0s(선행
  LLM head 포함 시 12.0s)에 실측이 얼마나 근접·도달하는지: p95가 수백 ms대면 "이론상 상한일 뿐
  실무 영향은 작다"는 뜻이고, 초 단위(특히 9~10s대)에 근접하면 실사용자가 504를 실제로 맞거나
  맞을 뻔했다는 뜻이다.
- `recommend_zero_result` 중 `category_expanded=True & had_candidates=True` 비율 — 구제 체인이
  애초에 얼마나 자주 진입하는지(체인 진입 자체가 드물면 지연 총합도 작다). **한계**:
  `category_expanded`/`had_candidates`는 `recommend_pipeline`에는 없다(R7 범위 밖) — 구제가
  성공한 턴 쪽 진입 빈도는 이 비율에 잡히지 않으므로, 이 지표는 "0건으로 끝난 진입"의 하한으로
  읽는다.

실빈도가 무시 못 할 수준으로 확인되면 공유 왕복 예산 또는 first-token 데드라인 가드 설계로
이어간다 — relaxation(#113)·기동 가드 보정(#288, §5)과 교차하는 영역이라 별도 설계 문서가
필요하다(이번 PR 범위 밖).

## 8. 실측 결과 — 싱크는 수정됐지만 운영 표본은 아직 없다 (2026-08-10)

**판정: 오늘 시점 운영 실측값은 없다.** `app/core/logging.py::configure_logging`은 `logging.basicConfig(level=..., format="%(asctime)s
%(levelname)s %(name)s %(message)s")`만 설정한다. 표준 `logging.Formatter`는 format 문자열에 없는
`LogRecord` 속성을 렌더링하지 않으므로, 수정 전 `logger.info("recommend_zero_result", extra={...})`의
`rescue_elapsed_ms`·`may_auto_relax` 등은 LogRecord에는 붙어도 컨테이너 stdout 문자열에서는 폐기된다.
수정 전 재현은 이 설정으로 해당 로그 호출을 실행한 뒤 렌더 결과가
`INFO app.agents.buyer.recommendation.graph recommend_zero_result`까지만 남고 extra 키가 없는지를
확인하면 된다. 이 문제는 #385 후속에서 **구제 체인 4개 이벤트만** `log_structured()`로 JSON message와
기존 `extra` 양쪽에 싣도록 수정했다. 따라서 평문 formatter의 렌더 줄도 파서가 받는 `{"event": ...}`
JSON을 포함하고, 기존 `record.rescue_elapsed_ms`류 테스트의 LogRecord 속성도 유지한다. `app/`의 나머지
88개 `extra={` 호출은 여전히 미렌더 상태다. docker json-file 드라이버는 `max-size=10m`, `max-file=3`으로
총 30MB만 보관하므로 JSON 줄 증가로 회전이 빨라져 표본 축적 창이 짧아질 수 있다.

`chat_request`만은 예외다. `app/core/observability.py`가 `logger.info(json.dumps(record))`로 JSON을
message 자체에 실으므로 오늘도 `latencyFirstToken`·`latencyTotal`·`errorType`·`lane`·`role`을
정상 출력한다. 기존 테스트가 `caplog.records`의 `zero_log.rescue_elapsed_ms`처럼 LogRecord 속성만
읽어 포맷 단계의 폐기를 잡지 못했던 것이 원인이다. 이제 sink는 고쳐졌지만, 아직 배포·축적·승인된
운영 로그 조회가 없으므로 실측값은 없다.

### 8.1 싱크 수정 범위와 남은 전제

채택한 방식은 `recommend_zero_result`, `recommend_pipeline`, `category_expand_zero_fallback`,
`category_expand_post_suppress_fallback`만 JSON message로 만드는 국소 수정이다. 전역 JSON formatter는
기각했다. `chat_request`가 이미 JSON message를 쓰므로 바깥 JSON 레코드 안의 문자열로 이중 인코딩되고,
`aggregate_observability.py::parse_log_line`이 최상위 `event`를 찾지 못해 기존 관측 파이프라인을 버린다.
전역 extras 렌더도 기각했다. `raw`·`query`·`canonical` 같은 카테고리 문자열을 포함한 나머지 88개 호출을
새로 stdout에 노출해 PII 규약을 회귀시킨다.

이 수정만으로는 실측값이 생기지 않는다. 이 브랜치가 `dev`에 병합된 뒤 `main`으로 승격되어 **배포**돼야
하고, 그 뒤 표본 축적 기간이 지나야 한다. 운영 로그 접근은 사람 승인 게이트이므로 이 작업에서는 조회하지
않았다. `.github/workflows/deploy.yml`은 수정하지 않았다.

### 8.2 운영 표본이 생긴 뒤의 집계 규약

선행 싱크 수정 뒤 JSON lines를 받으면 다음 명령으로 재실행한다.

```bash
uv run python scripts/aggregate_rescue_chain.py LOGFILE ... --markdown rescue-chain.md --csv rescue-chain.csv
```

결과는 이 문서의 후속 실측 절에 운영 날짜·로그 범위·표본 수와 함께 기록한다. 1급 지표의 모집단은
`recommend_zero_result ∪ recommend_pipeline`이고, 분모는 그중 `may_auto_relax=True`인 턴이다.
분자는 `rescue_elapsed_ms + relax_auto_elapsed_ms`이며 first SSE 뒤의
`relax_chip_elapsed_ms`는 제외한다. 빈도 가중 p50/p95/p99와 n, zero_result/pipeline 이벤트별 n을
함께 보고하고, `may_auto_relax=False` 턴은 first-token 비기여 별도 그룹으로 남긴다. 다만
`recommend_pipeline`은 구제 미진입 성공 턴도 남기므로 0ms가 빈도 가중 분위수를 희석할 수 있다.
그래서 `구제 기여 > 0`인 턴만의 조건부 p50/p95/p99·n, 그 n/전체 분모의 노출률, 9.0s 기준선 이상
턴 수, `stream_first_token_timeout_s` 상한 이상 턴 수, 최댓값을 반드시 함께 읽는다. 근접도는
희석된 전체 p95 하나가 아니라 이 조건부 분포와 임계 초과 건수로 판정하고, p95/상한의 소모율도
기록한다. 최소 표본 기본값은 기존 `degrade_alert_min_samples`를 **빌린 값**이며(새 설정 추가 없음),
운영자는 `--min-samples`로 이 보고서 실행마다 덮어쓸 수 있다. 최소 표본에 못 미치면 수치 결론 대신
"표본 부족 — 판정 보류"를 낸다.

체인 진입 빈도는 `recommend_zero_result`만을 분모로
`post_suppress_fallback_attempted=True`, `category_expanded=True and had_candidates=True` 비율을 낸다.
`recommend_pipeline`에는 마지막 네 필드가 없으므로 이는 0건으로 끝난 진입의 하한이며, 성공 종결
턴까지 대표하는 실빈도로 확대 해석하지 않는다. `chat_request`의 `errorType=UPSTREAM_TIMEOUT`은
오늘도 즉시 대조 가능하지만, home recommendation·랭킹 등 다른 경로도 같은 오류를 낼 수 있어
로그만으로 first-token 초과를 분리하지 못한다. 따라서 이 값은 first-token 초과의 **상한**으로
라벨링한다. 운영 로그 접근은 사람 승인 게이트라 이 작업에서는 조회하지 않았다.

### 8.3 후속 판단 경계

§5 마지막 항목처럼 #396 progress 상시화 이후 §4.1의 "최악 경로는 오늘 이미 first-token을 넘어
504" 결론은 유효하지 않으므로 여기서 되살리지 않는다. 실측값이 없으니 후속 ②(#384)의 집행 강도도
정할 수 없으며, 합성 표본이나 상한 추정치를 운영 측정값으로 대체하지 않는다. 로컬 합성 표본은
집계 도구 검증용일 뿐 운영 실측값이 아니다.
