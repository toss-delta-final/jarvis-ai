# DESIGN-SHARED-BUDGET-384 — 구제~자동완화를 아우르는 공유 왕복 예산 / first-token 데드라인 가드 설계

작성일: 2026-08-06 · 브랜치: `NyongCho/docs-384-shared-roundtrip-budget` · 이슈 #384(#363 후속)

**결론 세 줄**

1. `MEASURE-FIRST-TOKEN-363.md`(이하 MEASURE-363)의 산술("9.0s vs first-token 10s → 12.0s로 이미
   초과")은 #396(구매자 `progress` 상시화, api-spec v0.26.2)으로 전제가 깨져 **더 이상 성립하지
   않는다** — 오늘은 첫 SSE가 `conditions`가 아니라 `progress`이고, 구제 체인 전체가 first-token
   관문(10s) 안이 아니라 그 관문을 이미 통과한 **뒤**로 옮겨갔다(§1(a)). 다만 그 관문 자체가
   여전히 보장은 아니라는 잔여 위험이 하나 남는다(§1(a) 잔여 ②, 이 설계 범위 밖).
2. 구속 예산은 `stream_first_token_timeout_s`(10s)에서 **`stream_total_timeout_buyer_s`(30s) +
   체감 지연**으로 이동했다. 오늘 기본값(`spring_max_retries=0`)에서 첫 `conditions` 앞 직렬
   Spring 구간(본검색+구제 체인 3단, §2 용어 정의)은 여전히 9.0s(+LLM head 3.0s = 12.0s, 30s
   대비 40%)로 여유가 있다(§2 표 행1). 그러나 **#394 원복(`spring_max_retries` 0→1)만으로도**
   재시도 억제 스코프의 비대칭 갭(§2 각주, 이 설계가 새로 확인한 사실) 때문에 여유가 즉시
   50%(15.0s)로 줄고(§2 표 행2), **#394+#306 을 함께 원복하면 70%(21.0s)까지** 밀어 올린다(§2
   표 행3) — #363이 경고한 위험이 사라진 게 아니라 **다른 트리거(#394/#306 원복)에 결속된
   잠재 위험으로 재배치됐다.**
3. 따라서 공유 왕복 예산/데드라인 가드는 **"지금 당장 필요한 런타임 가드"가 아니라 "#394·#306
   원복의 선행 조건"** 이다(§2 결론). 이 문서는 그 가드의 설계(D1~D8, §3)와 #394/#306 원복
   시점에 사람 판단 없이 등급이 정해지도록 사전 결속된 집행 임계값(§4)을 확정한다 — 구현·적용은
   전부 후속 이슈(§6)이며 이 PR은 코드를 한 줄도 바꾸지 않는다.

---

## 1. 전제 재기준선 — #363 이후 바뀐 것 4건 + 참고 1건

### (a) `progress` 상시화 — 구속 예산 자체가 바뀌었다 ★가장 중요

- `app/core/config.py` `progress_events_enabled` 기본값은 이제 `True`다(#396, api-spec
  v0.26.2 — "**플래그 `progress_events_enabled`가 기본 on으로 전환됐다**"). 운영 기동 가드도
  제거됐다.
- `app/agents/buyer/graph.py::run_buyer_turn`이 decompose **앞**에서
  `yield progress_frame("analyzing", ...)`를 낸다 — intent 라우팅 이전이라 추천·담기·주문조회·
  일반 대화 전 레인 공통이다. 같은 함수 안, 그 emit 바로 위의 `[#289]` 주석이 이 재배치의
  의도를 그대로 적어 두고 있다: "first-token 관문(§2.9 c, 10s)이 LLM head·검색·재시도·자동
  완화를 통째로 안고 있어 미룬 턴 최악에서 이벤트 0건·504가 재현됐다(#277)... 관문에서 빠지는
  건 decompose LLM head **이후**뿐."
- **결과: 첫 SSE 이벤트는 이제 `conditions`가 아니라 `progress`이고, 구제 체인(F-1·#343·자동완화
  probe·칩 probe 전부)은 `conditions`보다도, `progress`보다도 뒤다.** 실측 p50 ≈12ms(첫 4개
  프렐류드 조회 flag-on 실측, `run_buyer_turn`의 `[#289]` 주석 인용, 근거
  `evals/first_event_budget/`).
- **MEASURE-363 §4.1의 산술 "9.0s(첫 `conditions` 앞 직렬 Spring 구간, §2 용어 정의) + 3.0s(LLM
  head) = 12.0s > 10.0s(first-token) → 504"는 그 전제("첫 SSE = conditions, first-token이 그
  구간을 가둔다")가 깨져 성립하지
  않는다.** 오늘 구제 체인은 first-token 관문 **밖**에서 돈다.
- **잔여 2건(문제가 사라졌다로 끝내면 틀린다):**
  1. `progress`는 계약상 **0회 이상**이다(api-spec §3.1 "(1) `progress`" 절, v0.27.0 개정 —
     #396 2·3단계 PR #407 머지로 `0~1회`에서 상한이 풀렸다). **이 논지는 상한이 아니라
     하한(0회 가능)에 걸려 있다** — 몇 회까지 나가는지와 무관하게, 그 첫 프레임 앞에서 턴이
     끝나면(LLM 미구성 → `error{LLM_UNAVAILABLE}`, 세션 상태 저장소 장애 → §2.5 스트림 전 오류
     봉투) 0회다 — 그 경우들은 애초에 구제 체인에 도달하지 않으므로 이 설계의 대상이 아니다.
  2. `progress` 프레임 **앞**의 상태 저장소 프렐류드(세션·스레드·프로필·장바구니 조회, 각
     `state_store_query_timeout_s` 3.0s, `run_buyer_turn`의 위 `[#289]` 주석)는 여전히
     first-token 관문 **안**이다 — api-spec §3.1 "관문 통과를 보장하지는 않는다... 직렬 최악
     12.0s로 first-token 상한(10.0s)을 넘을 수 있다"가 이를 명시한다. **이 잔여는 구제 체인과
     무관**하므로 이 설계의 집행 대상이 아니다(§6 후속 이슈 (iv)로만 남긴다).
- **결론: 구속 예산이 `stream_first_token_timeout_s`(10s) → `stream_total_timeout_buyer_s`(30s)
  + 체감 지연(첫 `conditions`/`token`까지)으로 이동했다.**

### (b) #394 — `spring_max_retries` 1→0 (CLOSED, 커밋 `2168e9b`, `config.py::Settings.spring_max_retries`)

- 단(stage)당 Spring 예산이 `spring_timeout_s × (spring_max_retries+1)` = `3s×2=6.0s` →
  **3.0s**로 절반이 됐다(`config.py::Settings.spring_max_retries` 현재값 `Field(default=0, ge=0, le=1)`).
- **원복 조건**(이슈 #394 본문): BE #395(I-1 `size` 상한·필드 축소·`rating`/`reviewCount`
  집계 비정규화)가 배포되면 재검토.
- 원복되면 단당 3.0s → 6.0s. 최악 체인(3단, §1(c))이 전부 재시도까지 쓰면 **18.0s**(§2 표
  행3) — 이슈 #384 본문이 예고한 "진짜 표적"이 이 수치다.

### (c) #306 — #277 응급 처치(미룬 턴 I-1 재시도 스킵) 원복 조건

- `spring_client.py::suppress_search_retry` + `config.py::Settings.search_retry_on_deferred_
  conditions`(기본 `False`).
- #306 이슈가 적은 선행 조건 3개(플래그 on·배포·기동 가드 제거)는 **(a)로 이미 충족됐다** —
  #306 본문 자신도 "#289가 그 전제를 없앤다... progress가 검색·재시도와 무관하게 첫 프레임으로
  나가므로 재시도가 아무리 걸려도 first-token 관문에 걸리지 않는다"고 명시한다.
- 그런데 #394로 `spring_max_retries=0`이라 **이 스킵은 지금 사실상 무동작(no-op)** 이다 —
  `spring_client.py::search_products`의 `attempts = 1 if suppressed else spring_max_retries+1`
  계산이 이미 `spring_max_retries+1=1`이라 억제 여부와 무관하게 결과가 같다.
- **원복 순서에 대한 답(이슈가 요구한 것)**: #395 배포 → #394 원복(`retries=1`) → 그 시점에
  비로소 #306 스킵이 "의미를 갖는 결정"(원복하면 재시도가 실제로 켜진다)이 된다. 이 문서 §2의
  수치표가 그 시점의 예산 여유를 낸다 — **결론은 "그때 공유 예산이 선행돼야 한다"**(§2 결론).
- **[2026-08-10 갱신 — 이 절이 예고한 순서대로 종결됐다]** #406(PR #532)이 #394를 원복하며
  `rescue_budget_mode=narrow`를 **함께** 올렸고(이 문서 §4가 요구한 결속), 그 위에서 **#306이
  억제 기구를 제거**했다. 결과는 §2 수치표 3행(`#394+#306 원복 → 18.0s`)과 정확히 일치한다.
  다만 그 18.0s는 **이론 상한이고 실집행값이 아니다** — narrow가 미룬 턴 본검색을
  `(30 − 꼬리 예약 15 − 경과) ÷ 3 ≈ 4.8s`로 좁히므로, #277이 504를 8/8 재현한 「1차 3.0s
  타임아웃 + 2차 2.9s 성공」 조합은 되살아나지 않는다. 즉 **이 설계의 D4(좁히기)가 #306을
  안전하게 만든 기제**이며, 그것이 §4가 "#394 원복은 최소 Lv1을 강제한다"고 결속해 둔 이유다.
  §1(d) 각주①의 억제 스코프 비대칭도 함께 소멸했다(§6 후속 (v) 종결 — 아래 참조).

### (d) #393(PR #411, 머지됨) — `not may_auto_relax` 턴의 추가 I-3 왕복

- `app/agents/buyer/recommendation/graph.py::_run_candidate_source`의 **B 경로**(`[#393 B]`
  주석으로 식별): 카테고리 매핑이 leg를 못 낸 턴에서 검색이 0건이면 I-3 인기 후보를 **추가로**
  부른다. 그 게이트 `not may_auto_relax`의 근거 주석(같은 `[#393 B]` 절 말미)이 명시적으로
  **first-token 10s**를 든다: "미루지 않는 턴은 conditions가 이미 나가 관문을 통과한 뒤라
  안전하다."
- **(a)로 그 근거가 낡았다** — B가 도는 시점 자체가 이미 first-token 관문(`progress`) 밖이므로,
  B를 `may_auto_relax=True` 턴까지 넓혀도 first-token 504는 재현되지 않는다.
- **재평가 결론**: 그럼에도 게이트는 **존치해야 한다** — 근거만 바뀐다. B를 `may_auto_relax`
  턴까지 넓히면 (i) `conditions`가 아직 안 나간 상태에서 검색+I-3 왕복이 한 단 더 늘어 **체감
  지연**(30s 예산 안에서도 사용자가 보는 빈 화면 시간)이 커지고, (ii) 무엇보다 이 문서 §1(d)의
  "첫 `conditions` 앞 직렬 Spring 구간 정확히 3단" 전제(§2 용어 정의, MEASURE-363 §3의 핵심
  관찰)가 무너져 §2 표의 모든 산술이 재계산돼야 한다 — **D1(예산 단위=벽시계+횟수 이중 억제)의
  전제인 "그 구간의 단 수는 유계"가 깨진다.** 근거를 "first-token 10s 보장"에서 "**그 구간의
  총 왕복 수를 유계로 유지**"(D1 원칙)로 교체하는 것이 이 설계의 판정이다. 이 PR은 그 주석을
  고치지 않는다(설계 문서이므로
  `app/` 코드는 건드리지 않는다) — §3 D8 목록·§6 후속 이슈로 남긴다.
- 같은 이유로 낡은 근거가 남은 지점은 §3 D8이 전수 열거한다.

### (e) PR #407(#396 2·3단계) — **머지됨(정정, H1)**

- **초판은 이 PR을 "미머지"로 적었으나 틀렸다.** `c92be6e`로 `dev`에 머지됐고, 이 문서 작업
  브랜치도 이후 `origin/dev`를 병합(머지 커밋 `52a32ac`)하며 함께 들어왔다 — 현재 이 브랜치의
  `app/agents/buyer/recommendation/graph.py`에 이미 반영돼 있다.
- progress 다회 emit + `stage` 7종(`analyzing`·`mapping`·`expanding`·`searching`·`relaxing`·
  `reranking`·`publishing`)이 체인 도중 나간다 — **실제 emit 지점**(코드에서 직접 확인, 전부
  `grep 'progress_frame("stage명"' app/agents/buyer/recommendation/graph.py`로 언제든
  재확인 가능하다 — 줄 번호는 인용하지 않는다):
  `stream_recommendation`이 `searching`(본검색 진입 직전)·`relaxing`(자동완화 루프의 **첫
  probe 직전 1회만**, 지역 플래그 `relaxing_progress_emitted`로 중복 방지)·`reranking`(rerank
  호출 직전)·`publishing`(두 지점 — no_condition 프로필 push 경로와 일반 push 경로 각각)을
  낸다. `analyzing`은 종전대로 `app/agents/buyer/graph.py::run_buyer_turn`(§1(a) 참조)다.
  `retrying`(재시도 진입)은 #406이 별도로 남겼고,
  #406 본문이 "#394가 원복될 때 같이 판단"이라고 명시한다 — 즉 #406은 이 설계와 같은
  트리거(#394 원복)에 결속돼 있으며 아직 구현되지 않았다.
- **체감 비용을 낮추지만 실제 소요는 줄이지 않는다** — 이 설계(§3)는 "PR #407 머지 전/후
  양쪽에서 성립해야 한다"는 요구를 그대로 유지하되, **이제 '후'가 이 브랜치의 현재 상태다**
  (머지 전 상태는 과거형 참고로만 남는다). §3의 D1~D8 어느 결정도 "#407이 들어오면 예산이
  필요 없다"고 결론짓지 않는다 — progress 다회 emit은 사용자에게 "지금 뭐 하는 중"을 보여줄
  뿐, 벽시계 소요 자체는 이 설계가 다루는 대상 그대로 남는다. **다만 #407이 새 상호작용을
  하나 만든다 — D4(§3)가 그 항목을 다룬다(H4).**

---

## 2. 수치표 — "그래서 지금 가드가 필요한가"

**용어 정의(F2, 이 문서 전체에 적용).** 초판은 "구제 체인"을 절마다 다른 범위로 썼다(§2 표·D7은
본검색 포함 3단, D2는 본검색 제외). 이제부터 둘을 이름으로 가른다:

- **"구제 체인"(rescue chain)** = F-1 무필터 재검색 + #343 억제-후 재판정(상호배타, 최대 1단) +
  자동완화 probe(최대 1단) — 최대 2단. 관측 변수 `rescue_elapsed_ms`(F-1/#343 몫) +
  `relax_auto_elapsed_ms`(자동완화 몫)가 재는 대상과 정확히 일치한다. **본검색은 포함하지
  않는다.**
- **"첫 `conditions` 앞 직렬 Spring 구간"** = 본검색 1단 + 구제 체인(위 정의) = 최대 3단. §2
  표·D7 `_rescue_chain_serial_budget_s`가 계산하는 대상은 **이쪽**이다(함수명이 "rescue_chain"을
  포함해 좁은 정의와 헷갈릴 수 있다는 점을 D7이 명시한다).

값은 `app/core/config.py` 실값과 `MEASURE-FIRST-TOKEN-363.md` §4 실측(단 소요 실측 하한)을
근거로 계산한다. 출처는 각 셀 옆에 표기.

| 시나리오 | 단당 Spring 예산 | 첫 `conditions` 앞 직렬 Spring 구간 최악(3단, 본검색 포함) | + LLM head(#151 p95) | 구매자 전체 30s 대비 |
|---|---|---|---|---|
| 오늘 기본값(`spring_max_retries=0`) | 3.0s (`3.0×(0+1)`) | **9.0s**(3×3.0, MEASURE-363 §4 실측) | **12.0s** | **40%** |
| #394 원복(`=1`), #306 미원복 | 3.0s(억제 대상 2단) / **6.0s**(억제 밖 1단, 각주①) | **12.0s**(3.0+6.0+3.0, 각주①) | **15.0s** | **50%** |
| #394 원복 + #306 원복 | 6.0s(`3.0×(1+1)`, 억제 전면 해제) | **18.0s**(3×6.0) | **21.0s** | **70%** |

**각주① — 이 문서가 새로 확인한 재시도 억제 스코프 갭(#383의 보정식도 반영하지 않음).**
`spring_client.py::suppress_search_retry`(컨텍스트 매니저)는
`app/agents/buyer/recommendation/graph.py::stream_recommendation`의 `with
spring_client.suppress_search_retry() ...: search_bundle, purchases = await asyncio.gather(
_run_candidate_source(), _fetch_purchases_once())` 블록 **안에서만** 유효하고 `await` 직후
`finally`(`suppress_search_retry` 안의 `_search_retry_suppressed.reset(token)`)로 즉시
닫힌다(그 `with` 문 바로 위 주석: "이 with는 await 뒤 즉시 닫아... ContextVar가 새지 않게
한다"). 그런데 F-1 무필터 재검색(`stream_recommendation`의 지역 함수 `_run_search_unfiltered`,
호출부는 `[#222 F-1]` 주석 블록)과 #343 억제-후 재판정(호출부는 `[#343]` 주석 블록)은 **둘 다
이 `with` 블록 밖**에서 호출된다(둘 다 `search_bundle`을 이미 언패킹한 뒤의 코드). 자동완화
probe(같은 함수의 `for cand in relax_candidates:` 루프 안, `_probe(cand)` 호출 직전)는 별도로
자기 `with` 블록을 갖고 있어 억제가 적용된다. 즉 **오늘 기본값(`spring_max_retries=0`)에서는
억제 여부가 결과에 영향이 없어(1 attempt로 동일) 드러나지 않지만, #394가 원복되고
(`spring_max_retries=1`) #306은 그대로면(`search_retry_on_deferred_conditions=False` 기본값
유지) F-1/#343 재검색 단만 재시도까지 써서(`attempts=spring_max_retries+1=2`) 6.0s가 되고,
나머지 두 단(본검색·자동완화 probe)은 억제돼 3.0s에 머문다** — 비대칭 12.0s.
`config.py::Settings._require_search_retry_within_stream_budget`의 OFF 분기(`serial_budget =
deferred_calls * spring_timeout_s`)는 이 비대칭을 모델링하지 않고 전 단을 억제된 것으로
가정해 `3×3.0=9.0s`를 검증한다 — **실제(12.0s)와
검증기가 믿는 값(9.0s) 사이에 3.0s 갭이 생긴다.** #394 원복 단독으로도(#306 미원복인 채) 30s
대비 여유가 조용히 줄어든다는 뜻이다. 이 갭은 D7(§3)이 신설 함수 설계에 반드시 반영해야 한다.

> **[2026-08-10 종결] 이 각주가 예고한 비대칭은 #306으로 소멸했다.** #406(PR #532)이 #394를
> 원복(`spring_max_retries=1`)한 직후 #306이 억제 기구(`suppress_search_retry`·
> `search_retry_on_deferred_conditions`)를 제거해, 세 단이 모두 `attempts=2`로 균일해졌다.
> D7의 `_rescue_chain_serial_budget_s`가 이 각주 때문에 갖고 있던 A/B `max`(PR #452 R4)도 함께
> 걷혀 `단 수 × 검색예산 × (재시도+1)` 곱셈 하나가 됐다(기본값 18.0s = §2 표 3행). §6 후속 (v)
> 종결. 아래 본문은 그 시점의 관찰 기록으로 남긴다.

**꼬리(rerank·I-21 push·답변 생성) — 확인 결과는 별도 절로.** 이 셋은 첫 `conditions` 앞 직렬
Spring 구간 **뒤**(products 확정 이후)라 위 표의 해당 열에는 들지 않지만, 같은 30s 예산을
공유하므로 D2(§3)의
"꼬리 예약"에 필요하다:

| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| rerank(LLM `smart` tier, 근거문 생성) | 타임아웃 상한 `llm_timeout_s×(llm_max_retries+1)`=`30.0×2=60.0s` | `app/core/llm.py::get_llm`(타임아웃·재시도 주입부), 호출부 `graph.py::stream_recommendation`의 `rr = await rerank(...)` | 상한만 확인. **실제 p95 관측치는 미확인**(#151 baseline은 decompose 호출 기준이지 rerank 호출 기준이 아니다) |
| I-21 push(`push_recommendations`) | 단일 시도 `spring_timeout_s`=3.0s, 재시도 없음(코드에 attempts 루프 없음) | `spring_client.py::_client`(공용 타임아웃 주입)·`spring_client.py::push_recommendations`(재시도 없는 단일 `try/except`) | 확인 |
| 답변(근거 token) 생성 | rerank 호출 1회의 산출물을 그대로 스트리밍 — **별도 LLM 호출 없음** | `graph.py::stream_recommendation`의 `rr = await rerank(...)` 이후 token emit 경로 | 확인 |

**§2 결론(수치표에서 도출)**: 오늘 기본값에서는 여유가 있다(40%). **#394 원복이 여유를
50%로, #394+#306 원복이 70%로 깎는다** — 어느 쪽도 즉시 30s를 넘기지는 않지만(그러면 애초에
`config.py::Settings._require_search_retry_within_stream_budget`의 전체-상한 비교가 기동을
막는다), rerank(p95 미확인, 상한만 60s)까지 직렬로 겹치면 체감 지연이 30s에 근접할 위험이
정성적으로 커진다. **표는 "오늘 당장 위험"이 아니라 "#394/#306 원복이 여유를 갉아먹는다"는
이슈 원문의 예상 형태와 일치한다** — 공유 왕복 예산은 **"#394/#306 원복의 선행 조건"** 이다.

---

## 3. 설계 결정 D1~D8

### D1 예산의 단위 — **채택: 벽시계 데드라인 단독** (횟수는 보조 관측치로만 유지)

기존 손잡이 `relaxation_max_rounds`(자동완화 라운드 상한)·`relaxation_max_probes`(칩 probe
상한)·F-1/#343 상호배타 가드(`category_expand_notice_suppressed`, 턴당 무필터 재검색 최대
1회)가 **이미 첫 `conditions` 앞 직렬 Spring 구간의 왕복 횟수를 유계(최대 3단, §2 용어 정의·
§1(d) 재확인)로 제한한다.** 왕복 횟수를
새 예산 단위로 또 두면 D1이 다루려는 "체감 지연"과 무관하게 이미 있는 규제를 이름만 바꿔
반복하는 것이다.

**벽시계 채택의 더 강한 근거 — 이미 같은 축의 집행 지점이 있다(F5 정정).** 초판은 "httpx의
`read` 타임아웃이 청크 간격 상한이라 총시간을 보장하지 않는다"(`search_products`의 `[#132]`
주석)는 점을 벽시계 채택 근거로 들며, 마치 총시간을 집행하는 코드가 아예 없는 것처럼 썼다 —
**틀렸다.** `spring_client.py::search_products`에는 이미 총시간 벽시계 가드가 있다(#132·PR
#293): `budget_s = settings.spring_timeout_s * attempts`를 계산해 `await asyncio.wait_for(
_fetch_and_parse(span), timeout=budget_s)`로 감싸고, 초과 시 `SearchBudgetExceeded` +
`spring_search_budget_exceeded` 로그로 degrade한다(같은 함수 안). 이
가드는 `search_catalog`(`search_service.py`)가 위임하는 모든 백엔드(`SpringSearchBackend`·
embedding_rerank 백엔드, 둘 다 내부에서 `spring_client.search_products`를 호출)를 거쳐 F-1·
#343·자동완화 probe·칩 probe **전부**에 이미 적용돼 있다 — read timeout이 총시간을 못 재는
문제는 #132가 이미 메웠다. 그래서 벽시계를 채택하는 진짜 이유는 "총시간 집행 수단이 없어서"가
아니라 **"이미 있는 같은 축(벽시계 총시간) 집행 지점(`budget_s`/`wait_for`)을 재사용할 수
있어서"**다 — D4가 이 지점을 그대로 집행 seam으로 쓴다. 벽시계는 사용자 체감을 직접 재는
유일한 축이라는 결론 자체는 유지된다.

- **기각 — 왕복 횟수 단독**: 이미 세 손잡이가 개별로 횟수를 제한 중이라 새 전역 카운터는
  군더더기다. 게다가 횟수 상한은 "몇 번"만 막지 "얼마나 오래"는 못 막는다(위 read timeout
  사례).
- **기각 — 벽시계+횟수 이중 관리**: 운영자가 두 값을 항상 정합하게 튜닝해야 하는 부담이 생기고,
  #383이 이미 지적한 "계수가 조용히 어긋나는" 실패 모드(§1(d) 각주①과 같은 유형)를 하나 더
  만든다.

### D2 데드라인의 기준점 — **채택: 스트림 시작의 절대 시각부터, 꼬리 예약을 뺀 잔여까지**

(a)로 first-token이 기준점에서 빠졌다. 초판은 "구제 체인 진입 시각"(F-1 판정 시점)을 기준점으로
삼고 거기서부터 새 창(`30 − decompose_head_reserve_s(3.0) − tail_reserve_s`)을 부여했는데,
**이 형태는 예산을 초과 승인한다(F1).** `rescue_entry_time`은 이미 프렐류드(§1(a) 잔여 ②, 최대
12.0s 직렬)·decompose head·본검색이 **끝난 뒤**인데, 그 앞에서 실제로 얼마가 지났는지는 상수
`decompose_head_reserve_s`(3.0s) 하나로만 근사돼 있었다 — 프렐류드 실제 소요와 본검색 실제
소요(최대 3.0~6.0s)가 계산에서 통째로 빠졌다. head가 p95를 넘거나 본검색이 느린 턴에서는
`rescue_deadline`이 스트림의 실제 30s 캡보다 **뒤로** 넘어간다(수치 예시: 프렐류드 0.5s + head
3.0s + 본검색 3.0s = 6.5s가 이미 지났는데, 옛 식은 그 시점에서 `30 − 3 − tail`을 **새로**
준다 — 6.5s만큼 초과 승인).

**옳은 형태 — 기준점을 스트림 시작의 절대 시각으로 잡는다:**

```
rescue_deadline = turn_started_at + (stream_total_timeout_buyer_s − tail_reserve_s)
```

- `turn_started_at`은 **새로 찍는 시각이 아니라, `app/core/stream.py::open_stream`이 이미
  `start = loop.time()`로 잡아 둔 그 값**이어야 한다 — `open_stream`이 실제로 집행하는
  `deadline = start + stream_total_timeout_buyer_s`와 **같은 원점**을 써야, 어떤 턴에서도
  `rescue_deadline`이 그 실제 캡을 넘지 않는다는 것을 증명할 수 있다: `tail_reserve_s ≥ 0`이면
  `rescue_deadline = turn_started_at + 30 − tail_reserve_s ≤ turn_started_at + 30 =
  open_stream의 deadline`이 항상 성립한다(등호는 `tail_reserve_s = 0`일 때만). 프렐류드·
  decompose head·본검색이 실제로 얼마나 걸렸는지는 **자동으로 반영된다** — 별도 근사 상수가
  필요 없다. `decompose_head_reserve_s` 튜너블은 **폐기한다**(D7에서 제거) — 추정 상수를
  하나 줄이는 것은 그 자체로 이득이다(추정이 틀리면 조용히 어긋난다, F1).
- **오늘의 갭 — 이 값이 아직 전달되지 않는다.** `stream.py::open_stream`은 `inner_factory:
  Callable[[], AsyncIterator[str]]`를 인자 없이 호출한다(`agen = inner_factory()`) —
  `start`를 그 팩토리에 넘기지 않는다. 호출 체인은 `app/api/chat.py`의 `open_stream(...)`
  호출부가 `lambda: run_buyer_turn(...)`을 `inner_factory`로 넘기고,
  `app/agents/buyer/graph.py::run_buyer_turn`이
  `app/agents/buyer/recommendation/graph.py::stream_recommendation`을 호출한다 — **이 경로
  어디에도 `start`가 흐르지 않는다.** 이 설계는
  `open_stream`이 `inner_factory`에 `start`(또는 그로부터 계산한 `deadline`)를 넘기도록
  시그니처를 바꾸고, 그 값이 `run_buyer_turn` → `stream_recommendation`까지 새 매개변수로
  전달되는 배선을 요구한다 — **오늘 존재하지 않는 배선이며, §6 후속 이슈 (i)의 필수 범위다.**
  (`loop.time()`은 CPython 기본 이벤트루프 구현상 `time.monotonic()`과 같은 값을 낸다 —
  `asyncio.BaseEventLoop.time()`이 내부적으로 `time.monotonic()`을 쓴다 — 이므로 `stream_
  recommendation` 안에서 이 값을 다른 `time.monotonic()` 호출과 그대로 비교해도 된다.)
- **끝**: `stream_total_timeout_buyer_s`(30s) 자체가 아니라 그 안에서 **꼬리(rerank·I-21 push)를
  위해 예약해 둔 값을 뺀 값**이다. 꼬리 예약이 필요한 이유: §2 표의 꼬리 절이 rerank 실제 p95를
  **미확인**으로 남겼으므로, 그 값을 0으로 취급하면(예약 없이 구제 체인이 잔여를 전부 써버리면)
  rerank가 정상 시간(수 초)만 써도 30s를 넘겨 스트림이 강제 절단될 수 있다. `tail_reserve_s`는
  신설 튜너블(D7)이며 기본값은 **#385 실측(rerank 실제 p95)이 나오기 전까지는 보수적으로 rerank
  상한의 일부**(예: `llm_timeout_s`의 절반 수준)로 두고, 실측 후 좁힌다.

기각안:

- **first-token(10s)을 기준점으로 유지** — (a)로 정상 턴 대부분에서 이미 통과된 관문이라 가드가
  개입할 지점 자체가 없다. 무의미.
- **`progress` SSE 발신 시각 기준** — `progress`는 구제 체인 진입보다 훨씬 앞서 이미 통과했다
  (§1(a) 실측 p50 ≈12ms). 그 시각을 기준으로 "남은 예산"을 재면 이미 여러 초가 지난 것처럼
  보이는 착시가 생긴다(실제로는 decompose head만 지났을 뿐) — 게다가 F1과 같은 이유로,
  `progress` 시각도 스트림 진짜 시작(`stream.py`의 `start`)보다 늦게 찍힌 새 시각이라 그 앞의
  프렐류드가 또 빠진다.
- **구제 체인 진입 시각(초판 채택안)** — F1이 지적한 초과 승인 문제로 기각. 위 옳은 형태로
  대체한다.

### D3 집행 주체 — **채택: 혼합(중앙 데드라인 + 분산 확인)**

중앙에 **단일 절대 시각(`float`, `loop.time()`/`time.monotonic()` 기준) 하나**를 두고, 각 구제
단계가 진입 **직전**에 그 값을 확인(분산 체크)한다.

- **자료구조**: `rescue_deadline: float` —
  `app/agents/buyer/recommendation/graph.py::stream_recommendation`(async generator) 함수
  스코프의 지역 변수. D2가 정의한
  `turn_started_at + (stream_total_timeout_buyer_s − tail_reserve_s)`로 함수 진입 시 **한 번만**
  계산해 대입한다(재계산하지 않는다 — 매 단계에서 다시 계산하면 D2가 고치려던 "새로 창을 부여"
  버그가 되살아난다). `turn_started_at`은 새 매개변수로 받는다(D2의 배선 갭 참조). 기존
  `rescue_elapsed_ms`/`relax_auto_elapsed_ms`(§1(c), MEASURE-363 §6)와 **같은 함수 스코프의
  자매 변수**로 둔다 — 계측(사후 관측, `elapsed_ms` 누적)과 게이트(사전 판단, 데드라인 비교)가
  같은 시계 계열(§1(a) `loop.time()`≡`time.monotonic()`)을 공유해 이중 시계로 인한 드리프트가
  생기지 않는다.
- **수명**: 이번 턴(한 번의 `stream_recommendation` 실행) 안에서만 산다 — 컨텍스트 변수
  (ContextVar)로 승격하지 않는다. `suppress_search_retry`(ContextVar)는 `spring_client.py`
  내부까지 값을 전달해야 해서(호출 스코프가 함수 경계를 넘는다) ContextVar가 필요했지만,
  데드라인 확인은 `graph.py` 로컬에서 끝나는 판단(각 구제 단계 진입 지점이 전부 `stream_
  recommendation` 안)이라 스코프가 더 좁다. 새 ContextVar를 만들면 "이 값이 다음 턴으로 새지
  않는가"라는 같은 종류의 위험(`stream_recommendation`의 `suppress_search_retry` `with` 문
  바로 위 주석이 이미 이 위험을 명시)을 하나 더
  만든다.
- **전달 경로**: 함수 인자로 명시 전달(`_run_search_unfiltered(..., deadline=rescue_deadline)`
  형태) — 지역 함수가 이미 클로저로 상위 스코프 변수를 참조하는 기존 패턴(`_run_candidate_
  source`·`_probe`가 `nonlocal`로 `popular_degraded`·`probes_spent` 등을 공유하는 것과 동일
  방식)을 그대로 따른다.

기각안:

- **완전 분산(config에 단계별 개별 타임아웃 신설)** — 이미 넓은 config 표면(`spring_timeout_s`·
  `relaxation_max_rounds`·`relaxation_max_probes` 등)에 새 손잡이를 더하면, 그 개별 값들의
  합이 우연히 전체 예산과 맞아떨어지도록 사람이 계속 손으로 맞춰야 한다 — #383이 지적한 것과
  **같은 유형의 실패 모드**(계수가 조용히 어긋난다)를 새 표면에서 재현할 뿐이다.
- **완전 중앙(`asyncio.wait_for`로 구제 체인 전체 — 여러 단계에 걸친 코드 블록을 — 한 번에
  감싸기)** — **기각 범위를 좁힌다(F5 정정).** `spring_client.py::search_products`가 이미
  `wait_for(_fetch_and_parse(span), timeout=budget_s)`로 **호출 1건**을 감싸는 벽시계 가드를
  운영 중이므로(D1 정정 참조), "`wait_for` 자체가 위험하다"는 기각은 성립하지 않는다 — 이미
  검증된 패턴이다. 기각은 오히려 **여러 단계(F-1/#343/자동완화 probe)에 걸친 코드 블록 전체를
  하나의 `wait_for`로 묶는 것**에만 적용된다: 타임아웃 발동 시 그 시점에 실행 중이던 임의의
  하위 `await`가 어중간하게 취소돼, `stream_recommendation`의 `[#343]` 재판정 블록 `finally`
  (`[#363 R4]` 주석: "성공·실패·늦은 예외 세 경로 전부 정확히 1회만 더한다")처럼 **취소
  타이밍에 민감한 기존 회계 로직**과 충돌할 위험이 있다(코드 주석이 반복 인용하는 "§7(부가
  기능 실패가 턴을 죽이지 않는다)" 원칙, SPEC-RECOMMEND-001 §7, 예: `stream_recommendation`의
  `_probe` 지역 함수 docstring). D3이 채택한 "각 구제 단계
  진입 직전에 분산 확인"은 이 좁힌 기각과 정합적이다 — 각 단계는 **자신의 `search_products`
  호출 하나**만 `wait_for`로 감싸이고(이미 그렇다), 그 호출을 진입시킬지 자체를 D4가 바깥에서
  판단한다.

### D4 초과 시 동작 — **채택: 혼합(좁히기 우선, 최소 하한 미만이면 건너뛰기)**

남은 예산이 다음 단의 `spring_timeout_s`보다는 작지만 신설 튜너블 `rescue_stage_min_timeout_s`
(D7)보다는 크면, 그 단의 타임아웃을 `min(spring_timeout_s, remaining)`으로 **좁혀서** 1회
시도한다. 남은 예산이 그 최소 하한 미만이면 다음 단 자체를 **건너뛴다.**

**"남은 단 수"를 D7의 공유 함수에서 얻는다.** 이 판단은 진입하려는 단 하나만 보고 되지 않는다
— 남은 예산을 지금 단이 통째로 쓰면, 뒤에 아직 자동완화 probe가 남아 있는 턴에서 그 probe가
곧바로 최소 하한 미만으로 굶어 건너뛰기부터 시작하게 된다. D7의 `_rescue_chain_serial_budget_s`
가 분해하는 계수(F-1/#343 1단·자동완화 최대 `min(relaxation_max_rounds, |auto∩chip|)`단)로
"지금부터 몇 단이 더 있는가"를 구해, 좁힐 때는 `min(spring_timeout_s, remaining / 남은_단_수)`
로 균등 배분한다 — 마지막 단에서만 `remaining` 전체를 준다. 이 정밀 배분의 구체적 알고리즘은
§6 후속 이슈 (i)에 넘긴다(이 문서는 "왜 남은 단 수가 필요한가"까지만 답한다).

**집행 수단 — 이미 있는 seam을 재사용한다(F5).** "좁힌다"는 말만으로는 집행 불가능해 보인다 —
`spring_client.py::_client`는 `spring_timeout_s`를 httpx에 **스칼라로** 주입하고,
D1이 인용하듯 httpx의 `read`는 청크 간격 상한이라 총시간을 보장하지 않는다. 그러나
`search_products`에는 **이미** 그 갭을 메운 총시간 가드가 있다(#132·PR #293,
같은 함수 안): `budget_s = settings.spring_timeout_s * attempts`를
`asyncio.wait_for(_fetch_and_parse(span), timeout=budget_s)`로 감싼다. D4의 좁히기는 **이
`budget_s`를 계산에 쓰는 대신 잔여 예산(`min(spring_timeout_s, remaining) * attempts` 또는
남은 예산이 1회 시도 예산보다 작으면 재시도 자체를 포기하는 형태)으로 주입**하는 것으로
구현한다 — 새 취소 메커니즘을 만들지 않고 기존 `wait_for` 호출의 `timeout` 인자만 잔여값으로
바꾼다.

**적용 범위 — I-1(검색)에만 해당한다.** 이 가드는 `search_products`(I-1) 전용이다 — 확인한
결과 `get_popular_products`(I-3, `_fetch_popular_candidates`가 호출)와 `push_recommendations`
(I-21)는 둘 다 `_client()`의 스칼라 타임아웃만 쓰고 `wait_for`/`budget_s` 가드가 **없다**
(`spring_client.py::get_popular_products`·`spring_client.py::push_recommendations` 확인).
즉 D4의 "좁히기"는 F-1/#343/자동완화 probe/칩
probe(전부 `search_products` 경유)에는 적용되지만, 이 문서 §2 "꼬리" 절의 I-21 push나 A
경로(무필터 우회, I-3)에는 **적용되지 않는다** — 그 경로들은 여전히 `spring_timeout_s` 고정
1회 시도만 하고 예산을 좁힐 수단이 없다(오늘은 `search_max_candidates`급 소규모 응답이라
실효 위험은 작지만, 근거 없이 "전부 좁혀진다"고 쓰면 틀린다).

- **건너뛰기의 트레이드오프(이슈가 명시적으로 요구)** — 건너뛰기는 #222/#343이 고친 "검색
  히트가 있었는데 하류 억제가 전량 지운 걸 '상품 없음'으로 오판"하는 실패를 **부분적으로
  되돌린다.** 이 설계는 그 트레이드오프를 받아들이되, "좁히기 우선"으로 그 빈도를 최소화한다 —
  무조건 건너뛰지 않고, 남은 시간이 조금이라도 유의미하면 시도는 한다.
- **[신설, H4] 예산 판정이 stage emit보다 먼저 와야 한다 — #407이 만든 새 상호작용.** #363
  이후 머지된 PR #407(#396 2·3단계)이 `relaxing` stage를 "자동완화 루프 진입 시점"이 아니라
  **"첫 probe 직전에 1회"**로 배치한 이유가 PR 본문에 명시돼 있다 — "진입 시점에 내면 probe가
  0회인 턴에도 '완화 중'이 뜬다", 규칙은 "서버가 지금 실제로 하는 일이어야 한다 / 안 하는
  일을 하는 중이라고 말하지 않는다". D4의 **건너뛰기**는 정확히 그 거짓 신호를 새로 만들 수
  있는 경로다 — 예산 부족으로 자동완화 probe를 건너뛰는데 `relaxing`이 이미 나갔거나 나가면,
  서버는 사용자에게 "완화 중"이라고 말해 놓고 그 라운드에서 아무 일도 하지 않는다.
  - **집행 지점(대표 사례 `relaxing`)** — `stream_recommendation`의 자동완화 루프
    (`for cand in relax_candidates:`)는 현재 순서가 (1) `if rounds >= settings.
    relaxation_max_rounds: break` → (2) `rounds += 1` → (3) `if settings.progress_events_
    enabled and not relaxing_progress_emitted: ... yield progress_frame("relaxing", ...)`
    → (4) `with (suppress...): outcome = await _probe(cand)`다. D4의 예산 확인(`remaining =
    rescue_deadline - loop.time()`가 `rescue_stage_min_timeout_s` 미만인가)을 **(2)와 (3)
    사이에 삽입**한다 — 예산이 부족하면 `relaxing`을 emit하지 않고 그 라운드를 건너뛴다
    (루프의 다음 후보로 가거나, 남은 예산이 이미 0에 가까우면 루프 자체를 `break`). 예산이
    남아 있으면(좁혀서라도
    시도 가능하면) 종전대로 emit 후 `_probe`를 호출한다 — **좁히기는 실제로 probe를 시도하므로
    stage emit이 정당하다**(서버가 지금 그 일을 하고 있다는 게 참이다). 거짓 신호가 되는 것은
    "건너뛰기" 쪽뿐이다.
  - **F-1/#343(구제 체인의 나머지 절반)은 이 위험이 없다** — 코드에 이 둘 전용 stage가 없다
    (§1(e) 참조, 5종 emit 지점 중 하나도 F-1/#343 전용이 아니다). "searching"은 F-1/#343보다
    훨씬 앞, 본검색 진입 직전에 이미 나갔고(§1(e)), "본검색 중"이라는 넓은 의미로만 읽혀서
    F-1/#343의 건너뛰기가 그 문구를 거짓으로 만들지 않는다.
  - **`reranking`·`publishing`은 영향받지 않는다(코드로 확인)** — 이 둘은 rescue 체인이 끝난
    **뒤**, 각자의 실제 호출(`rr = await rerank(...)`, `push_fn(...)`) 바로 앞에서 조건 없이
    emit된다(§1(e) 좌표). D4의 예산 확인은 F-1/#343/자동완화 probe에만 적용되므로 두 stage의
    진실성에는 관여하지 않는다.
  - **후속 이슈 (i)의 완료 조건에 추가**: "예산으로 건너뛴 단의 `relaxing` progress stage가
    나가지 않는지" 회귀 테스트(§6 (i)에 반영, 아래).
- **좁히기와 api-spec §2.9(c) "AI→Spring 전 구간 3s" 규약의 관계 — §5에서 판정.** 결론만 먼저
  적으면: 좁히기(3s **미만**으로 주는 것)는 "3s 통일" 문구를 어기지 않는다(상한을 넘지 않는
  방향의 변경이라서다) — 다만 문구가 짧게 주는 것을 **허용**한다고 명시적으로 말하지는 않아
  §5가 명확화 개정을 제안한다(적용은 사람 승인 게이트).
- **관측 가능성 — 조용한 포기 금지.** 건너뛴 사실은 새 로그 필드로 남긴다: `recommend_zero_
  result`/`recommend_pipeline`에 `rescue_stage_skipped_budget: bool`(D7 신설 필드, 이 턴에서
  예산 부족으로 건너뛴 단이 있었는가)을 추가한다. 좁혀서 시도한 경우도 `rescue_stage_narrowed_
  timeout_ms: int | null`로 남긴다 — 좁히기가 실제로 얼마나 자주·얼마나 세게 발동하는지가
  §4 등급 판단의 입력이 된다.

### D5 칩 probe 취급 — **채택: 같은 30s 예산 안, 그러나 D3 데드라인 감시 대상에서는 제외**

이슈는 칩 probe가 첫 SSE 이후라 축이 다르지만 30s 전체 예산에는 든다고 지적했다. (a) 이후
재평가: `relax_auto_elapsed_ms`/`relax_chip_elapsed_ms` 분리(#363 R3, MEASURE-363 §7 "R3")의
원 근거는 "first SSE(당시 `conditions`) 이전/이후"였는데, (a)로 `conditions` 자체가 더 이상
first-token 관문의 경계가 아니게 됐으므로 그 근거 문장은 낡았다. 그러나 분리를 없앨 이유는
아니다 — **`conditions`/`token` 발신 전/후**는 여전히 사용자 체감이 다른 두 구간이다(전자는
빈 화면, 후자는 이미 화면에 무언가 뜬 상태의 추가 대기). 그래서:

- 칩 probe는 D2/D3의 데드라인 감시 대상에서 제외한다 — 자기 예산(`stream_recommendation`의
  `probe_budget = settings.relaxation_max_probes` 대입, 이미 횟수로 유계)만으로 충분히
  제한돼 있고, `outcomes = await asyncio.gather(*(_probe(c) for c in pending))`로 병렬
  실행되어 벽시계 기여가 **후보 수와 무관하게 왕복 1회분**으로 유계인 것은 맞다. 다만 그
  1회분의 상한이 `spring_timeout_s`(3.0s)라는 것은 **틀렸다** — 칩 probe의 `_probe(cand)`
  호출은 자동완화 루프의 `_probe(cand)` 호출과 달리 `suppress_search_
  retry()`로 감싸여 있지 **않다**(자동완화 루프 주석이 "아래 완화 칩 probe는 감싸지 않는다"고
  명시). 즉 `_search_retry_suppressed.get()`이 이 지점에서 항상 `False`라 실제 상한은
  `spring_timeout_s × (spring_max_retries+1)`이고, **오늘(`spring_max_retries=0`)은 3.0s로
  같지만 #394 원복 후에는 6.0s**다(30s 예산의 20%).
- 이 정정이 "제외해도 된다"는 결론을 바꾸는가 — **바꾸지 않는다.** 6.0s(20%)는 §4의 1급 지표
  ceiling(§4, #394 원복 시 최대 9.0~12.0s)에 비해서도, 30s 전체 예산에 비해서도 단독으로
  스트림을 위협하는 크기가 아니고, 무엇보다 칩 probe는 이미 `conditions`/`token`이 나간 **뒤**라
  D2가 다루는 "빈 화면" 체감과 성격이 다르다(D5 서두). 다만 §4의 관측 로그에는 이 6.0s 상한을
  반영해 `relax_chip_elapsed_ms`를 **여전히 별도로** 관측한다 — "구제 체인이 총 체감 지연에
  얼마나 기여했는가"를 볼 때, `conditions` 이전 몫과 이후 몫을 구분해야 어느 쪽을 조여야 할지
  판단할 수 있다.

기각안 — **자동완화와 같은 예산 풀에 합치기**: 합치면 자동완화가 먼저 돌아 예산을 다 쓴 턴에서
칩이 통째로 굶는 문제(PR #248 리뷰가 이미 지적하고 고친 바로 그 문제, `probe_budget` 대입
바로 위 `[PR #248 리뷰]` 주석)가 되살아난다.

### D6 `may_auto_relax=False` 턴 — **채택: 예외는 유효하다(다만 이유가 이슈 원문과 다르다)**

이슈 원문은 "conditions가 검색 이전에 나가므로 집행 대상에서 빼야 한다"고 적었다. 코드를 다시
읽으면 이 예외는 **(a)와 무관하게 원래부터 성립하는 사실**이었다:

- `stream_recommendation` 앞부분의 `if not may_auto_relax: yield sse("conditions", ...)`는
  검색을 시작하는 `with spring_client.suppress_search_retry() ...: search_bundle, purchases =
  await asyncio.gather(...)`보다 **앞**에 있다. 즉 `may_auto_relax=False` 턴은 `conditions`가
  F-1/#343/자동완화 probe 전부보다 먼저 나간다.
- F-1(`[#222 F-1]` 태그, 게이트 `decision.category_expanded and search_result.total_count ==
  0`)과 #343(`[#343]` 태그, 게이트 `category_expand_post_suppress_fallback_enabled and ...`)의
  게이트 조건 어디에도 `may_auto_relax`가 없다 — **F-1/#343은 `may_auto_relax`와 무관하게
  돈다.** 이 두 구제 단계 자체는 애초에 "`conditions` 발신 여부"가 아니라 "확장 턴인가·억제로
  비었는가"만 본다.
- 따라서 `may_auto_relax=False` 턴에서 F-1/#343/자동완화 probe가 도는 시점은 **`conditions`
  발신 뒤**다 — 이슈 원문이 말한 그대로다. 다만 이건 (a) 이후에 생긴 예외가 아니라 **(a)와
  독립적으로 원래부터 성립했던 사실**이다. "`conditions`보다 먼저/나중"과 "first-token 관문
  보다 먼저/나중"은 원래도 서로 다른 축이었다 — (a)는 후자(관문)의 기준점만 옮겼지 전자
  (`conditions` 순서)는 건드리지 않았다(api-spec §3.1 "기존 6종의 상대 순서는 불변").
- **결론**: D6이 이슈가 예상한 "예외가 통합될 수도 있다"는 방향으로 흐르지 않는다 — 예외는
  그대로 유지한다. 대신 **§4(집행 강도)의 관측 판정을 수정해야 한다**: MEASURE-363 R7
  ("`may_auto_relax=True`인 턴만 본다")의 원래 근거는 "false인 턴은 first-token을 안 늦춘다"
  였는데, (a) 이후 first-token 자체가 이 체인과 무관해졌으므로 그 근거로는 더 이상
  `may_auto_relax=False` 턴을 제외할 이유가 없다 — false인 턴도 `conditions`/`token`까지의
  **체감 지연**에는 F-1/#343 소요가 그대로 실린다. §4가 이 재정의를 반영한다.

### D7 튜너블 + #383과의 계수 공유

**신설 튜너블**(전부 `app/core/config.py` 주입 제안 — 적용은 후속 이슈):

| 이름 | 타입 | 잠정 기본값 | 의미 |
|---|---|---|---|
| `rescue_budget_mode` | `Literal["observe","narrow","narrow_skip"]` | `"observe"` | §4 등급(Lv0/Lv1/Lv2)의 런타임 스위치 |
| `rescue_stage_min_timeout_s` | `float` | 예: `0.5`(§6에서 재산정) | D4 "좁히기"의 최소 하한 — 미만이면 건너뛴다 |
| `rescue_tail_reserve_s` | `float` | 미정 — #385 실측 대기(§6) | D2 "꼬리 예약" — rerank·I-21 push를 위해 남겨 둘 시간 |

**공유 지점 — 순수 함수 시그니처.** `config.py::_deferred_first_event_i1_calls`의 자리에
새 함수를 둔다:

```python
def _rescue_chain_serial_budget_s(
    *,
    deferred_calls: int,          # _deferred_first_event_i1_calls() 반환값(아래 참조)
    spring_timeout_s: float,
    spring_max_retries: int,
    search_retry_on_deferred_conditions: bool,
) -> float:
    """첫 conditions 앞 직렬 Spring 구간(본검색 + F-1/#343 재검색 + 자동완화 probe) 직렬 최악
    벽시계.

    이름은 "rescue_chain"이지만 계산 대상은 §2가 정의한 좁은 "구제 체인"(F-1/#343+자동완화,
    본검색 제외)이 아니라 본검색을 포함한 넓은 "첫 conditions 앞 직렬 Spring 구간"이다(F2) —
    함수명·계산 대상 불일치는 후속 이슈(§6 (i))에서 `_pre_conditions_serial_budget_s` 등으로
    리네임하는 것을 고려한다.

    §1(d) 각주①의 억제 스코프 비대칭을 반영한다 — search_retry_on_deferred_conditions=False
    (기본값)일 때 F-1/#343 재검색 1단은 suppress_search_retry() 컨텍스트 밖이라 재시도가
    억제되지 않는다. 나머지(본검색·자동완화 probe)만 억제된다.
    """
```

- `_deferred_first_event_i1_calls` 자체는 **#383의 보정식**을 전제로 쓴다 — 검증 가능한 사실은
  "이 브랜치 베이스(`798f0a9`)의 `config.py`에는 아직 옛 식 `1 + min(relaxation_max_rounds,
  |auto∩chip|)`이 있고 #383 보정식은 반영되지 않았다"까지다. #383은 별도 워크트리에서
  **동시 진행 중인 레인**이라(오케스트레이터 확인) 이 문서가 그 착수·완료 여부를 단정하지
  않는다 — 이 문서가 쓰는 "3단" 전제는 #383 보정식이 코드에 반영된 시점 이후에만 정확해진다:
  `1 + (1 if category_expand_enabled else 0) + min(relaxation_max_rounds, |auto_fields ∩
  chip_fields|)`.
- 이 함수는 위 갭(각주①) 때문에 `deferred_calls × 단가` 같은 균일 곱셈이 아니라, **F-1/#343에
  해당하는 1단만 별도 계수**(재시도 억제가 적용 안 됨 → 항상 `spring_timeout_s ×
  (spring_max_retries+1)`)로 계산하고 나머지 단은 `search_retry_on_deferred_conditions`에 따라
  분기해야 정확하다 — #383의 제안식은 이 비대칭을 반영하지 않으므로(§1(d) 각주①), **#383과
  함께 갈 때 이 함수도 같이 고쳐야 한다.**
- **공유 요구(이슈 코멘트 인용 — "서로 다른 계수를 쓰면 안 된다") — D2가 아니라 D4가 런타임
  소비처다.** F1로 D2의 `rescue_deadline`은 `turn_started_at`·`stream_total_timeout_buyer_s`·
  `tail_reserve_s`만으로 계산되도록 고쳤다 — 이 함수를 호출하지 **않는다**(실제 경과 시간을
  쓰므로 이론적 단 수 모델이 필요 없다). 이 함수의 실제 런타임 소비처는 **D4**다: 좁히기 판단이
  "남은 예산을 이번 단이 전부 가져가도 되는가, 뒤에 남은 단(들)을 위해 일부 남겨야 하는가"를
  가르려면 "지금부터 몇 단이 더 남았는가"가 필요하고, 그 값을 이 함수의 계수 분해(F-1/#343
  1단 + 자동완화 최대 `min(relaxation_max_rounds, |auto∩chip|)`단)에서 얻는다. 그래서 (i)
  런타임 집행(`app/agents/buyer/recommendation/graph.py`의 D4 좁히기 로직이 이 함수를 import해
  "남은 단 수"를 구한다)과 (ii) 기동 시점 검증(`config.py::Settings.
  _require_search_retry_within_stream_budget`가 인라인 계산을 이 함수 호출로 교체한다)
  **둘 다 이 함수 하나만 호출한다.**
  한쪽만 고치는 드리프트(#383이 고치려는 바로 그 실패 모드)를 구조적으로 막는다. §4의 1급
  지표 ceiling 계산(D3 채택 형태)도 같은 계수 분해를 재사용하되, 그쪽은 "본검색 제외"라는
  다른 부분집합을 보므로 셋이 완전히 같은 호출은 아니다 — 계수의 **원천**(어느 단이 억제되고
  몇 단인지)만 공유하고, 합산 범위(본검색 포함/제외)는 용도별로 다르다는 점을 §6 후속 이슈
  (i)가 구현 시 분명히 해야 한다.

### D8 낡아버린 전제의 잔재 목록 — 전수 열거 (이 PR에서 고치지 않는다 — 설계 문서이므로 `app/`·`docs/api-spec.md`는 건드리지 않는다)

| # | 파일::심볼 | 현재 서술 | 왜 낡았는지 |
|---|---|---|---|
| 1 | `graph.py::_run_candidate_source`의 `[#393 B]` 절 | "미루지 않는 턴은 conditions가 이미 나가 관문을 통과한 뒤라 안전하다"(first-token 10s 근거) | (a)로 B가 도는 시점 자체가 이미 first-token 관문 밖. 게이트는 **존치**하되 근거를 "첫 `conditions` 앞 직렬 Spring 구간 총 왕복 유계"(D1, §2 용어 정의)로 교체해야 한다(§1(d)) |
| 2 | `graph.py::stream_recommendation`의 `suppress_search_retry` `with` 문 바로 위 주석 | ~~"progress 이벤트가 계약에 생기면 이 스킵은 원복 가능하다"~~ | **해소됨(#306, 2026-08-10)** — #406(PR #532)이 #394를 원복해 스킵이 다시 유효해진 직후 #306이 억제 기구를 통째로 제거했고, 그 주석·`with` 문·`suppress_search_retry` 자체가 함께 사라졌다. 6행과 같은 형식으로 종결 표시만 남긴다 |
| 3 | `config.py::Settings._require_search_retry_within_stream_budget` docstring | "구매자 progress 이벤트(#289)가 계약에 등재되면 미룸 자체가 사라져 이 검증기는 보험 계층이 된다" | **틀렸다** — `conditions` 지연(미룸)은 사라지지 않는다(api-spec §3.1 정상 흐름 서술, "conditions는 여전히 검색·자동 완화 뒤다"). 사라지는 것은 이 미룸이 **first-token 관문에 걸리는 것**뿐이다. "보험 계층"이 되는 결론은 맞되 이유가 다르다 — 이제는 first-token 초과·504 방지가 아니라 첫 `conditions` 앞 직렬 Spring 구간의 벽시계 초과·체감 지연 폭주 방지 보험(본 설계 D1~D5가 그 새 역할을 규정) |
| 4 | `MEASURE-FIRST-TOKEN-363.md` §4.1·§7 | "12.0s > 10.0s이므로 최악 경로는 오늘 설정에서 이미 first-token 데드라인을 넘어 504가 된다" | (a)로 무효. 그 파일은 이 PR 범위 밖이라 고치지 않는다(#383 레인이 같은 파일 §5를 동시 작업 중) — 이 설계 §1(a)가 정정 근거를 대신 남긴다 |
| 5 | `docs/api-spec.md` §2.9(c) I-1 재시도 행 | "미룬 턴은 첫 이벤트 앞 본 검색 1회 + probe 1회를 각각 재시도 없이... 직렬 `2×3s=6s`" | (a)와 무관하게 #383이 지적한 낡음 — 실측 단 수는 3인데 서술은 2를 센다. §5 개정안에서 diff 제시(적용 안 함) |
| 6 | `docs/api-spec.md` §3.1 progress 절 | ~~"확정 값은 `analyzing` 1종"~~ | **해소됨(#396 v0.27.0)** — PR #407 머지로 어휘가 7종+개방형으로 확정됐다(§3.1 "(1) `progress`" 절의 v0.27.0 태그, 개정 이력 표의 "v0.27.0" 행). (a)와 직접 관련 없는 항목이었고(참고), 이제 낡은 서술 자체가 없다 — §6 후속 이슈 (ii)의 대상에서 제외한다 |

**부수 발견(D8 목록 밖, (a)와 무관 — §1(d) 각주①)**: 재시도 억제 스코프 비대칭
(`suppress_search_retry`가 F-1/#343 재검색을 감싸지 않음)은 #383의 보정식으로도 못 잡는
별개의 계상 오류다. 오늘(`spring_max_retries=0`)은 결과에 영향이 없어 드러나지 않았을 뿐이다 —
D7의 신설 함수 설계에 반드시 반영해야 한다.

---

## 4. 집행 강도 — #385에 사전 결속

이슈 완료 조건 ②는 "#385 실측 p95를 보고 집행 강도를 정하라"이나 **#385는 아직 미완**이다(§2
표는 config 실값·MEASURE-363 실측만으로 계산했지, 운영 표본을 쓰지 않았다). 측정 없이도 확정
가능한 것(등급 트리거 조건 자체)과 측정에 결속해야 하는 것(등급 사이 이동)을 가른다.

**판정 지표 재정의(D6 결론 반영)**: MEASURE-363 §7의 원 지표("`recommend_zero_result` ∪
`recommend_pipeline`에서 `may_auto_relax=True`인 턴의 `rescue_elapsed_ms + relax_auto_
elapsed_ms` p95/p99")는 first-token 축이 전제였다. (a) 이후에는:

- **1급 지표(개정)**: 위 지표를 `may_auto_relax` 값과 **무관하게** 전체 턴에 대해 낸다 — F-1/
  #343은 `may_auto_relax`와 무관하게 도므로(D6), false인 턴을 제외하면 그 몫의 체감 지연이
  관측에서 빠진다. `rescue_elapsed_ms`(모든 턴에 유효)는 그대로 두고, `relax_auto_elapsed_ms`는
  `may_auto_relax=True`인 턴에서만 0이 아니므로 자연히 반영된다 — **필터 없이 합산 p95/p99를
  낸다.**
- **2급 지표(기존 유지)**: `may_auto_relax=True` 한정 지표도 함께 관측한다 — "`conditions`가
  구제 체인 뒤로 미뤄지는 턴"만의 몫을 따로 보고 싶을 때 필요하다(D5가 `conditions` 전/후
  구간을 여전히 구분하기로 했으므로).

**§4 초판의 결함(F3)과 수정 방향**: 초판은 등급 임계값을 §2 표의 **총합**(본검색+head+구제
체인) 대비 비율(40%/66%)로 잡았는데, 1급 지표(`rescue_elapsed_ms + relax_auto_elapsed_ms`)는
본검색도 LLM head도 재지 않는다(MEASURE-363 §6 계측 필드 표 확인 — 둘 다 별도 변수이거나
계측 자체가 없다). 그 지표의 이론적 최댓값(ceiling)을 §1(d) 각주①의 비대칭까지 반영해 계산하면
아래처럼 30s의 40%(12.0s)에 전혀 못 미친다 — **Lv0·Lv1 상태에서 40% 조건은 물리적으로 발화할
수 없었다.** 척도(총합 대비 비율)를 지표의 정의역과 맞지 않는 채로 붙인 것이 원인이다(F3).

**1급 지표 ceiling(이론 최댓값) — 총합이 아니라 지표 자신의 정의역으로 재정의**:

| 시나리오 | `rescue_elapsed_ms` 최대 | `relax_auto_elapsed_ms` 최대 | 1급 지표 ceiling |
|---|---|---|---|
| 오늘(`spring_max_retries=0`) | 3.0s | 3.0s | **6.0s** |
| #394 원복, #306 미원복 | 6.0s(각주① 비대칭) | 3.0s(억제 유지) | **9.0s** |
| #394+#306 원복 | 6.0s | 6.0s(억제 전면 해제) | **12.0s** |

등급 임계값은 **30s의 고정 비율이 아니라 이 ceiling의 비율**로 잡는다 — 그러면 임계값은
정의상 `threshold = ratio × ceiling ≤ ceiling`이라 **항상 발화 가능**하다(비율이 100% 미만인 한
p95/p99가 그 값에 도달하는 것을 막는 물리 법칙이 없다). ceiling 자체는 D7의 공유 함수가
현재 설정(`spring_max_retries`·`search_retry_on_deferred_conditions`)으로 실시간 계산한다 —
등급 판정 코드가 하드코딩된 초 단위 상수를 갖지 않는다.

**3단 등급**(§6 후속 이슈 (i)가 구현):

| 등급 | `rescue_budget_mode` | 진입 조건 | 발화 가능한가 |
|---|---|---|---|
| Lv0 관측만 | `observe` | 기본값. #394가 원복되지 않는 한 계속 | 해당 없음(임계값 비교 자체를 안 함) |
| Lv1 좁히기 | `narrow` | **필요조건**: #394 원복(`spring_max_retries≥1`)이 배포됨. **충분조건**: 1급 지표 p95 ≥ 0.7 × ceiling(현재 설정) | 오늘 설정 그대로면 ceiling 9.0s(#394만 원복)의 70%=**6.3s** — p95가 이론 최댓값 9.0s 이하인 한 도달 가능. 필요조건이 없으면(오늘 retries=0) ceiling 6.0s의 70%=4.2s도 마찬가지로 도달 가능하지만, 필요조건이 이를 막는다(설계상 의도) |
| Lv2 좁히기+건너뛰기 | `narrow_skip` | Lv1 상태에서 1급 지표 p99 ≥ 0.9 × ceiling(현재 설정), **또는** 스트림 전체 30s 강제 절단(`done{finishReason:"stop"}`, api-spec §2.9(c) 행2)이 이 체인 관여 턴에서 관측됨 | #394+#306 원복 시 ceiling 12.0s의 90%=**10.8s**, #394만 원복 시 9.0s의 90%=**8.1s** — 둘 다 각 시나리오의 ceiling 이하라 도달 가능. 두 번째 선택지(30s 강제 절단)는 지표와 무관한 독립 트리거라 항상 유효 |

**등급과 무관하게 성립하는 조건(이슈가 요구)**: **#394 원복은 그 자체로 최소 Lv1을 강제한다** —
이는 1급 지표의 실측과 무관한 **별도 근거**다: §2 표(본검색+head 포함 총합)가 보이듯 원복 즉시
첫 `conditions` 앞 직렬 Spring 구간의 상한이 18.0~21.0s로 뛰어(30s의 60~70%) 실측 표본이
쌓이기 전에도 "관측만"으로는 불충분하다는 판단이 이미 §2 산술로 선다(이 조건은 1급 지표
ceiling 표와는 다른 지표를 근거로 쓴다는 점에 주의 — 총합 vs 좁은 지표를 다시 섞지 않는다,
F2). 즉 **#394를 원복하는 PR은 `rescue_budget_mode=narrow`로의 전환을 같은 PR 또는 선행 PR에
포함해야 한다** — 이 결속이 이슈 완료 조건 ②("측정값이 없어 못 정한다"로 끝내지 말 것)에
대한 답이다.

---

## 5. 계약(api-spec §2.9) — 개정안 초안 (적용하지 않는다, 사람 승인 게이트)

### 5.1 D4 "좁히기" vs "AI→Spring 전 구간 3s" 규약

원문(§2.9(c) 표, "AI→Spring 콜백" 행): `**3s**(BE I-2 문서 기준으로 통일)`. "통일"이라는 단어와
정확한 초 단위 지정은 **상한이 아니라 고정 기준값**으로 읽힌다 — 바로 아래 I-1 재시도 행이
"재시도 총량(기본 6s)"처럼 예외를 명시적 문장으로 서술하는 것과 대조하면, "통일" 행 자체에는
"up to"류 표현이 없다.

**판정**: D4의 좁히기(개별 호출 타임아웃을 남은 예산만큼 3s **미만**으로 줄이는 것)는 이 문구를
**어기지 않는다** — 3s를 넘지 않는 방향의 변경이라서다. 다만 문구가 짧게 주는 것을 명시적으로
허용한다고 말하지 않아 **문면이 침묵**한다. 명확화 개정을 제안한다(적용 안 함).

**원문은 한 줄이다**(`docs/api-spec.md` §2.9(c) 표의 "AI→Spring 콜백" 행) — 아래 before/after는
그 한 줄(마크다운 표 한 행)을 그대로 인용한다. 줄바꿈은 표시하지 않는다(원문에 없다):

```diff
- | AI→Spring 콜백(§4.1/§4.4~4.7/§4.9/§4.12~4.17) | **3s**(BE I-2 문서 기준으로 통일) | 각 계약의 degrade 규칙(조회 생략·담기 `CART_ERROR`·dedup 생략 등) |
+ | AI→Spring 콜백(§4.1/§4.4~4.7/§4.9/§4.12~4.17) | **3s**(BE I-2 문서 기준으로 통일 — 구제 체인 공유 예산(§384)이 걸린 호출은 잔여 예산에 따라 이 값 이하로 좁혀질 수 있다) | 각 계약의 degrade 규칙(조회 생략·담기 `CART_ERROR`·dedup 생략 등) |
```

### 5.2 I-1 재시도 행의 "직렬 2×3s=6s" 서술

(a)와 무관하게 #383이 지적한 낡음 — 실측 단 수는 3(F-1/#343 + 본검색 + 자동완화 probe)인데
서술은 2(본검색 + probe)만 센다.

**원문도 한 줄이다**(`docs/api-spec.md` §2.9(c) 표의 `↳ **I-1 검색만 재시도 1회**` 행). 그 한 줄은
BE 관측 포인트 외에도 재시도 대상 4xx 분류·`Retry-After` 미존중·PR #287 실측치·v0.21.0/v0.26.2
갱신 이력 등 이 개정과 무관한 서술을 다수 포함한다 — 아래는 **바뀌는 절만 발췌**했고, 나머지
(`…`로 표시한 구간)는 원문 그대로 유지한다. 실제 적용 시에는 한 줄 전체에서 이 절만 치환한다:

```diff
- …**BE 관측 포인트** — `conditions`를 미루지 않는 턴은 같은 검색 요청이 최대 2번 온다. 기본값에서 `may_auto_relax` 턴(#113)은 첫 이벤트 앞의 본 검색 1회와, 0건이면 자동 완화 probe 1회를 **각각 재시도 없이** 호출해 Spring 직렬 구간을 `2 × 3s = 6s`로 묶는다.…
+ …**BE 관측 포인트** — `conditions`를 미루지 않는 턴은 같은 검색 요청이 최대 2번 온다. 기본값에서 `may_auto_relax` 턴(#113)은 첫 이벤트 앞의 본 검색 1회·확장 턴의 구제 재검색(F-1/#343, 상호배타 최대 1회) 1회·0건이면 자동 완화 probe 1회를 **각각 재시도 없이** 호출해 Spring 직렬 구간을 최대 `3 × 3s = 9s`로 묶는다(#383·#384, `docs/specs/MEASURE-FIRST-TOKEN-363.md` §4).…
```

이 개정이 필요한지 여부와 개정안 요지는 오케스트레이터가 판단해 에스컬레이션한다 —
**`docs/api-spec.md` 파일 자체는 이 PR에서 수정하지 않았다.**

---

## 6. 후속 실행 이슈 제안 (등록하지 않음 — GitHub 이슈 생성 금지)

**(i) 구제 체인 공유 왕복 예산 런타임 구현 + 튜너블 + 테스트**
- 목적: D1~D7의 설계를 코드로 집행한다.
- 범위: (1) **`turn_started_at` 플럼빙**(D2) — `app/core/stream.py::open_stream`이 `start`(또는
  파생 `deadline`)를 `inner_factory`에 전달하도록 시그니처를 바꾸고, `app/api/chat.py`의 람다·
  `app/agents/buyer/graph.py::run_buyer_turn`·
  `app/agents/buyer/recommendation/graph.py::stream_recommendation`까지 새 매개변수로
  관통시킨다 — **오늘 존재하지 않는 배선**이라 D2~D4 전체의
  전제다. (2) `app/core/config.py`(D7 튜너블 3종 + `_rescue_chain_serial_budget_s` 함수 — D4의
  "남은 단 수" 계산과 기동 검증이 공유). (3) `app/agents/buyer/recommendation/graph.py`(D3
  `rescue_deadline` 변수·D4 좁히기/건너뛰기 로직·D7 로그 필드). (4) §4 등급(Lv0~Lv2)의 1급
  지표 ceiling 계산.
- 선행 조건: 이 설계 문서 승인. **#394가 원복되기 전에는 `rescue_budget_mode=observe`만
  구현해도 충분**(§4) — Lv1/Lv2는 #394 원복과 함께 배선. 다만 (1)의 플럼빙은 `observe`
  모드에서도 필요하다(관측 로그가 실제 경과를 정확히 재려면 `turn_started_at`이 있어야 한다).
- 완료 조건 초안: `uv run pytest` 통과, `rescue_stage_skipped_budget`/`rescue_stage_narrowed_
  timeout_ms` 로그 필드 추가, `rescue_deadline`이 어떤 턴에서도 `stream.py`의 실제 스트림
  데드라인을 넘지 않는지(D2의 `≤` 부등식) 회귀 테스트로 고정, D4의 "남은 단 수" 계산과 기동
  검증기(`_require_search_retry_within_stream_budget`)가 `_rescue_chain_serial_budget_s` 하나만
  호출하는지 확인. **[신설, H4]** 예산 부족으로 자동완화 probe를 건너뛴 턴에서 `relaxing`
  progress stage가 SSE에 나가지 않는지(=D4의 예산 확인이 `relaxing` progress_frame emit보다 먼저
  평가되는지) 회귀 테스트로 고정 — `test_progress_event.py`류에 "narrow_skip 모드 + 예산
  소진" 시나리오를 추가한다.
- 근거: §3(D1~D8), §4(집행 강도).

**(ii) D8 낡은 전제 잔재 정리**
- 목적: §3 D8 표의 남은 5개 지점(6번 항목은 #396 v0.27.0으로 이미 해소됨, H3) + 부수 발견
  1건을 실제로 고친다.
- 범위: 주석·docstring 정정(코드 로직 변경 없음), `MEASURE-FIRST-TOKEN-363.md` §4.1·§7에 (a)
  이후 무효 표시 추가(#383 레인과 조율 필요 — 같은 파일 §5를 #383이 동시에 고치는 중).
- 선행 조건: 없음(독립적으로 진행 가능, 단 #383과 파일 충돌 조율).
- 완료 조건 초안: `grep`으로 "first-token 10s"·"미룸 자체가 사라져" 같은 낡은 근거 문구가 D8
  표의 5개 위치에서 사라졌는지 확인.
- 근거: §3 D8.

**(iii) api-spec §2.9(c) 개정 (사람 승인)**
- 목적: §5.1·§5.2의 두 개정안을 정본(Notion CH-2) 협의 후 `docs/api-spec.md`에 반영한다.
- 범위: §2.9(c) 표 두 행.
- 선행 조건: 이 문서 §5의 판정에 대한 사람 승인. (ii)가 §3 D8 항목 5를 먼저 고치면 이 개정과
  중복될 수 있어 순서 조율 필요.
- 완료 조건 초안: 정본 합의 → 사본 동기화 → 버전 이력 추가.
- 근거: §5.

**(iv) progress 프레임 앞 상태 저장소 프렐류드 관문 잔여**
- 목적: §1(a) 잔여 ②(세션·스레드·프로필·장바구니 조회, 각 3.0s, 직렬 최악 12.0s가 여전히
  first-token 10s를 넘을 수 있음)를 해소한다.
- 범위: 이 설계의 구제 체인과는 **무관** — `app/agents/buyer/graph.py`의 `progress` 이전 프렐류드
  조회 순서/병렬화.
- 선행 조건: 없음(구제 체인 이슈와 독립).
- 완료 조건 초안: 프렐류드 조회를 직렬에서 병렬로 바꾸거나, 개별 타임아웃을 좁혀 직렬 최악을
  first-token 아래로 낮춘다.
- 근거: §1(a) 잔여 ②.

**(v) 재시도 억제 스코프 비대칭 수정(§1(d) 각주①, 이 설계가 새로 발견) — ✅ 종결(#306, 2026-08-10)**
- **결말**: 두 대안(감싸기 / 계상만 정확히) 중 어느 쪽도 아닌 **세 번째 길로 해소됐다** —
  #306이 억제 자체를 제거해 "감쌀 대상"이 사라졌다. `_rescue_chain_serial_budget_s`의 A/B
  `max`(PR #452 R4)도 함께 걷혀 `단 수 × 단가` 곱셈 하나가 됐다. 아래 원문은 이력으로 남긴다.
- 목적: `suppress_search_retry()`가 F-1/#343 무필터 재검색을 감싸지 않아 #394 원복 시 그 단만
  비대칭으로 재시도되는 갭을 해소한다.
- 범위: `app/agents/buyer/recommendation/graph.py::stream_recommendation`의 F-1(`[#222 F-1]`
  절)·#343(`[#343]` 절)의 `_run_search_unfiltered()` 호출을
  기존 `with spring_client.suppress_search_retry() if suppress_deferred_search_retry else
  nullcontext():` 패턴으로 감싸거나, D7의 `_rescue_chain_serial_budget_s`가 이 비대칭을 정확히
  모델링하도록 한다(코드를 고치는 대신 계상만 정확히 하는 대안도 가능 — 어느 쪽이 나은지는
  후속 이슈가 판단).
- 선행 조건: (i)와 함께 진행 권장(같은 파일·같은 함수를 건드린다).
- 완료 조건 초안: `_require_search_retry_within_stream_budget`의 OFF 분기 검증식이 이 비대칭을
  반영해도 기동이 막히지 않는지 확인(오늘 기본값에서는 영향 없음, §2 표).
- 근거: §1(d) 각주①, §3 D7.

---

## 근거표 — 이 문서가 인용한 수치의 출처

**인용 방식(R1/R2, 4라운드 구조 정정)**: `config.py`를 포함해 모든 코드 좌표는 **줄 번호가 아니라
심볼**로 쓴다 — 동시 진행 레인이 여럿이라 이 문서가 살아 있는 동안 줄 번호는 계속 드리프트한다
(3라운드에서 `recommendation/graph.py` 좌표를, 4라운드에서 `config.py` 좌표 14개 전부를 갱신해야
했다 — grep 한 번이면 찾히는 필드명·함수명으로 바꾸면 이 갱신 자체가 필요 없어진다). 심볼로
특정 안 되는 지점(특정 주석 한 줄, 코드 블록 범위)은 가장 가까운 심볼 + 식별 태그/변수명으로
쓴다. 값 자체는 이 라운드에서 `2ad08a7`(#325) 병합 후 현재 파일로 전부 재확인했다.

| 값 | 출처 |
|---|---|
| `stream_first_token_timeout_s = 10.0` | `config.py::Settings.stream_first_token_timeout_s` |
| `stream_total_timeout_buyer_s = 30.0` | `config.py::Settings.stream_total_timeout_buyer_s` |
| `spring_timeout_s = 3.0` | `config.py::Settings.spring_timeout_s` |
| `spring_max_retries = 0`(기본, #394) | `config.py::Settings.spring_max_retries` |
| `state_store_query_timeout_s = 3.0` | `config.py::Settings.state_store_query_timeout_s` |
| `relaxation_max_rounds = 3` | `config.py::Settings.relaxation_max_rounds` |
| `relaxation_max_probes = 4` | `config.py::Settings.relaxation_max_probes` |
| `relaxation_auto_fields = ["ratingMin"]` | `config.py::Settings.relaxation_auto_fields` |
| `relaxation_chip_fields = ["priceMax","ratingMin","brand","color"]` | `config.py::Settings.relaxation_chip_fields` |
| `category_expand_enabled = True` | `config.py::Settings.category_expand_enabled` |
| `category_expand_post_suppress_fallback_enabled = True` | `config.py::Settings.category_expand_post_suppress_fallback_enabled` |
| ~~`search_retry_on_deferred_conditions = False`(기본)~~ → **필드 폐지(#306, 2026-08-10)** | 억제 기구 제거로 config 에서 삭제됐다 — 재시도는 `spring_max_retries` 하나가 정한다 |
| `progress_events_enabled = True`(기본, #396) | `config.py::Settings.progress_events_enabled` |
| `llm_timeout_s = 30.0` / `llm_max_retries = 1` | `config.py::Settings.llm_timeout_s` / `config.py::Settings.llm_max_retries` |
| 첫 `conditions` 앞 직렬 Spring 구간 단 수 = 3(본검색+구제 체인, first SSE 이전 — (a) 이전 기준, §2 용어 정의) | `docs/specs/MEASURE-FIRST-TOKEN-363.md` §4, 회귀 `tests/unit/test_fanout.py::test_worst_case_rescue_chain_sequential_stages_before_first_sse` |
| decompose LLM head p95 ≈ 3.0s(#151 baseline) | `config.py::Settings._require_search_retry_within_stream_budget` docstring의 "커버하지 않는 것" 절 인용 |
| `progress` 첫 프레임 실측 p50 ≈ 12ms | `app/agents/buyer/graph.py::run_buyer_turn`의 `[#289]` 주석, `evals/first_event_budget/` |
| ~~재시도 억제 스코프(`suppress_search_retry`가 F-1/#343을 감싸지 않음)~~ → **소멸(#306, 2026-08-10)** | 억제 기구가 제거돼 세 단 모두 `spring_client.py::search_products`의 `attempts = spring_max_retries + 1` 을 균일하게 쓴다(§1(d) 각주① 종결 표시 참조) |
| I-21 push 재시도 없음, 단일 시도 3.0s | `spring_client.py::_client`(공용 클라이언트 타임아웃)·`spring_client.py::push_recommendations`(재시도 루프 없음) |
| `search_products`(I-1)의 기존 총시간 벽시계 가드(`budget_s`/`wait_for`, F5) — F-1/#343/자동완화 probe/칩 probe 전부 이 경유 | `spring_client.py::search_products`(`budget_s` 계산·`wait_for`/`SearchBudgetExceeded`) · 경유 경로 `search_service.py::SpringSearchBackend.search`·`search_service.py::EmbeddingRerankBackend.search`(둘 다 내부에서 `search_products` 호출) |
| `get_popular_products`(I-3)·`push_recommendations`(I-21)는 위 가드가 **없음**(F5) | `spring_client.py::get_popular_products`(`_client()`만 사용)·`spring_client.py::push_recommendations`(동일) |
| ~~칩 probe(`_probe`)는 `suppress_search_retry()` 밖 — 자동완화 루프의 `_probe(cand)` 호출과 달리 억제 없음(F4)~~ → **구분 소멸(#306, 2026-08-10)** | 억제가 사라져 자동완화 probe·칩 probe가 같은 재시도 규칙을 쓴다. 둘의 차이는 이제 `conditions` 전/후라는 위치와 예산 배분(D5)뿐이다 |
| rerank LLM 호출 상한 60.0s(`llm_timeout_s×(retries+1)`), 실 p95 미확인 | `app/core/llm.py::get_llm`(`timeout`/`max_retries` 주입부), 정의부 `app/agents/buyer/recommendation/rerank.py::rerank`·호출부 `graph.py::stream_recommendation`의 `rr = await rerank(...)` |
| api-spec §2.9(c) 타임아웃 기준표 | `docs/api-spec.md` §2.9 "(c) 타임아웃 기준표" 절 |
| api-spec §3.1 `progress` 이벤트 절 (v0.27.0) | `docs/api-spec.md` §3.1 "(1) `progress`" 절 |
| api-spec §3.1 이벤트 순서(정상 흐름, v0.27.0) | `docs/api-spec.md` §3.1 "정상 흐름(추천)" 서술 |
| `docs/specs/README.md`가 `MEASURE-*`/`DESIGN-*`를 색인 표에 안 싣는 관례 | `docs/specs/README.md` 전문 재확인(표에 두 문서 계열 부재) |

---

## 확인하지 못한 것 (미확인)

- **rerank(LLM `smart` tier) 호출의 실제 p95/p99 지연** — 상한(`llm_timeout_s×(retries+1)=
  60.0s`)만 확인했고 baseline 관측치를 찾지 못했다. §2 꼬리 절·§3 D2의 `rescue_tail_reserve_s`
  기본값 산정에 필요하다 — #385 후속 실측 또는 별도 baseline 측정이 선행돼야 한다.
- **#383의 실제 병합 여부와 정확한 코드 반영 시점** — 이 세션 시점(`git log` HEAD `798f0a9`)에서
  `config.py`의 `_deferred_first_event_i1_calls`는 여전히 옛 식(2)이고 #383 이슈는 OPEN으로
  확인했다. 이 문서 §3 D7이 "#383 보정식을 전제로 쓴다"고 명시했지만, #383이 이 설계와 다른
  순서로 진행되거나 다른 형태로 변형될 가능성은 배제하지 못했다.
- **§4 등급 임계값의 최종 확정치** — F3 정정 이후 임계값은 1급 지표 ceiling 대비 비율(Lv1
  p95≥70%, Lv2 p99≥90%)로 정의했지만, **왜 70%/90%인지의 근거는 "항상 발화 가능하다"는
  구조적 요건뿐**이고 그 값이 실제로 적절한 민감도(너무 자주/드물게 발동하지 않는가)인지는
  실측이 없어 판단하지 못했다. §2 표의 총합(본검색+head+구제 체인) 산술(오늘 40%, #394 단독
  원복 50%, #394+#306 원복 70%)은 별도 근거(§4 "등급과 무관하게 성립하는 조건")로만 쓰고 이
  임계값 산정에는 섞지 않았다(F2·F3). #385 실제 운영 표본이 나오면 70%/90%을 재보정해야 한다.
- **`rescue_stage_min_timeout_s`의 구체적 기본값** — D4·D7에서 "예: 0.5"로만 잠정 제시했다.
  이 값이 실제로 F-1/#343/자동완화 probe 각각에 유의미한지(예: 0.5s로 Spring 왕복이 성공할
  확률)는 실측이 없어 판단하지 못했다.
