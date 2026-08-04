# [초안] 구매자 SSE `progress` 이벤트 — 계약 제안 (#289)

> **상태: 협의 대상 초안.** 아직 정본(Notion CH-2)·`docs/api-spec.md` §3.1에 등재되지 않았다.
> 이 문서는 플래그 off 구현 + 실측을 먼저 만들기 위한 근거 자료이며, FE/BE 협의 후 정본에
> 반영하는 것이 다음 단계다(#289 Acceptance 2번째 항목). 이 PR 은 `progress_events_enabled`를
> **켜지 않은 채** 끝난다 — 아래 수치·동작은 협의 대상이지 확정 계약이 아니다.

## 1. 이벤트 스키마

envelope 프레이밍은 §2.2(camelCase)·§3.1 기존 이벤트와 동일한 규약을 따른다:

```
data: {"type":"progress","data":{"stage":"analyzing","message":"요청을 확인하고 있어요"}}\n\n
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `stage` | string(enum) | 예 | 진행 단계. 이 PR 이 구현하는 값은 `analyzing` 1종(§2) |
| `message` | string | 아니오 | 사용자 노출 문구. 빈 문자열/미설정이면 `data`에 `message` 키 자체를 싣지 않는다 |

`message`를 선택으로 둔 이유 — FE 가 `stage`만으로 자체 문구(다국어·로컬 카피)를 매핑할 수도
있어야 계약이 특정 문구에 고정되지 않는다. 서버가 문구를 실어도 FE 가 무시하고 자체 매핑을
쓰는 선택지를 막지 않는다.

## 2. `stage` 어휘 — 이 PR 은 `analyzing` 1종만 구현한다

| stage | 상태 | 의미 |
|---|---|---|
| `analyzing` | **구현(플래그 off)** | 요청을 확인하는 중 — intent 확정 전 |
| `searching` | 미구현·협의 대상 | 상품 검색 중 |
| `relaxing` | 미구현·협의 대상 | 조건 자동 완화 재시도 중 |
| `reranking` | 미구현·협의 대상 | 결과 재정렬 중 |

**이 초안의 핵심 발견**: 이슈 본문은 "검색 시작 전 `searching` 1회"를 제안했지만, 첫 프레임을
낼 수 있는 지점(=decompose 앞, §4)에서는 **intent 가 아직 확정되지 않았다.** `run_buyer_turn`은
추천뿐 아니라 담기(`cart_add`)·장바구니 조회(`cart_view`)·주문조회(`order_status`)·일반 대화
(`general`) 턴도 같은 진입점(decompose)으로 라우팅하므로, 그 자리에서 `searching`을 내면
"장바구니 담아줘"·"주문 어디까지 왔어" 같은 비추천 턴에 **검색 중이라고 거짓 표시**한다 —
FE 가 검색 스피너를 보여주는 순간 실제로는 담기 처리 중이라 UX 상 오정보다.

그래서 첫 stage 는 intent 중립적인 `analyzing`("요청을 확인하고 있어요")으로 제안하고,
`searching`은 decompose 뒤 추천 레인 진입이 확정된 다음의 **후속 stage**로 남긴다(미구현).

**FE/BE 협의에서 어휘를 `searching` 하나로 줄이길 원하면, 그 대가(비추천 턴 오라벨링)를
반드시 함께 제시해야 한다** — 그 경우 emit 지점을 intent 확정 이후로 늦춰야 하는데, 그러면
이 PR 이 해결하려는 first-token 관문 문제(§4)가 그대로 남는다. 두 요구(첫 프레임을 최대한
당기기 vs 어휘를 검색 전용으로 좁히기)는 서로 상충한다.

## 3. 발생 규칙 — 순서 계약에 미치는 영향

- **`progress`는 0~1회다.** 플래그 off 이면 항상 0회. 플래그 on 이어도, emit 지점(§4, decompose
  직전) **이전에 턴이 끝나면 0회**다 — LLM 미구성(`llm is None`) 경로는 `progress` 없이
  `error{LLM_UNAVAILABLE}`을 첫 프레임으로 내고 `return`하며, `SessionStateUnavailable`(세션
  상태 저장소 장애)이나 그 앞의 pg 장애로 예외가 나는 턴은 프레임 자체가 0개다(§4가 지키려는
  설계 그 자체 — §7 참고). **FE는 `progress` 도착을 전제하면 안 된다**: 스트림의 첫 프레임이
  `error`이거나 스트림이 아예 열리지 않을 수 있다.
- 플래그 on 인 **정상** 턴(emit 지점까지 도달한 턴)에서는 정확히 1회이며, **그 턴의 스트림
  첫 이벤트**가 된다.
- §3.1 이벤트 순서 계약(현재: `conditions`(0~1) → `token`(0회 이상)+`suggestions`(해당 시) →
  `products.ready`(성공 시 정확히 1회) → `done`(1회))에 `progress`를 추가하면:

  ```
  progress(0~1, 첫 프레임) → conditions(0~1) → token/suggestions → products.ready(0~1) → done(1)
  ```

- **기존 7종의 상대 순서는 불변이다.** 특히 `conditions`는 여전히 검색·자동 완화 뒤에 온다
  (#113·#277 이 고정한 순서) — `progress`는 그 앞에 새 프레임을 하나 추가할 뿐, `conditions`가
  검색보다 먼저 나가도록 당기지 않는다. `tests/unit/test_recommendation.py`의 순서 고정 테스트
  2개(`test_recommendation_delays_conditions_until_search_and_auto_relax_probe_finish`,
  `test_recommendation_emits_conditions_before_search_when_auto_relax_disabled`)는 **#289 후에도
  유효**하며, 이번 PR 에서 지우지 않았다(§7).

## 4. emit 지점과 그 대가

emit 지점: `app/agents/buyer/graph.py::run_buyer_turn`, `prompt_screen` 대입 직후·decompose를
감싸는 `try: with trace_span("buyer.routing", "chain"):` 블록 **바로 앞**.

**왜 스트림 최상단이 아닌가** — 함수 최상단에는 `context_id` 검증(`SessionStateUnavailable`
raise, 세션 상태 저장소 장애 판정)이 있다. 이 예외는 **아직 어떤 프레임도 yield 하지 않은**
async generator 안에서 발생하므로, `open_stream`(`app/core/stream.py`)의 첫 프레임 pull이
그 예외를 그대로 전파받아 §2.5 **스트림 전 오류 봉투**(`503 STATE_UNAVAILABLE`, HTTP 상태 코드
+ JSON 오류 본문, 200 헤더 미전송)로 나간다. 만약 `progress` emit을 이보다 **앞**에 두면, 첫
yield가 먼저 일어나 FastAPI `StreamingResponse`가 200 헤더를 확정 전송한 뒤 저장소 장애가
나므로, 같은 장애가 **in-stream `error`**(§3.1, 200 헤더가 이미 나간 상태)로 바뀐다 — 이것은
계약 위반이다(§2.5 "스트림 전 오류 코드의 통합 목록"이 깨짐).

decompose 직전에 두면 §2.5 봉투가 전부 보존되면서, 관문(first-token 10s, §2.9 c)에서 빠지는
것은 다음 구간이다:

| 구간 | 실측/근거 | decompose 앞에 emit 하면 |
|---|---|---|
| LLM head(decompose 호출) | #151 baseline p95 ≈3.0s(@동시성1) | 관문 밖 |
| I-1 검색(본 검색) | 최대 3s×2=6s(재시도 포함, §2.9 c) | 관문 밖 |
| I-1 자동 완화 probe | may_auto_relax 턴 추가 1회 | 관문 밖 |
| 위 전체 직렬 합(#277 최악) | 6.97s(재시도 스킵 후) / 8/8 504(스킵 전) | 관문 밖 |

대가: `progress` emit 이후 decompose~검색 구간에서 발생하는 오류(LLM 타임아웃·Spring 장애 등)는
이제 **in-stream `error`**로 나간다(§3.1 기존 계약 그대로 — 이건 새로운 동작이 아니라 원래도
그랬다. 다만 지금까지는 `conditions`가 먼저 나가는 정상 턴에서만 그랬고, `may_auto_relax` 턴은
`conditions`도 검색 뒤라 그 구간에서 나는 오류가 스트림 전 오류로 새는 문제가 있었다 — #277이
재시도 스킵으로 임시 봉합한 바로 그 문제. `progress`가 그 구간 앞에 프레임을 하나 깔아 두면
같은 오류가 여전히 in-stream `error`로 나가되, **504·이벤트 0건 없이 최소 한 프레임은 보장**된다는
차이다).

### 남는 위험 — `progress`는 관문 통과를 보장하지 않는다

emit(`progress_frame` yield, `app/agents/buyer/graph.py` L531) **앞**에도 상태 저장소 호출이
남아 있고, 그중 회원 턴 기준 **직렬 4회**가 각각 `run_with_query_timeout`
(`state_store_query_timeout_s` **3.0s**)로 감싸여 있다:

| # | 호출 | 위치 | 상한 |
|---|---|---|---|
| 1 | `thread_store.get` | L450 (`ThreadFilterStore.get`) | 3.0s |
| 1b | `thread_store.put` | L456(conditionActions 있는 턴만) | 3.0s |
| 2 | `read_profile_summary` → `ProfileStore.get_summary` | L462 → `app/agents/profile/store.py` L168 | 3.0s |
| 3 | `cart_store.get_pending` | L471 (`CartStateStore.get_pending`) | 3.0s |
| 4 | `cart_store.get_last_reco_state` | L486 | 3.0s |

**직렬 합 최악 = 4 × 3.0s = 12.0s > first-token 상한 10.0s**(`stream_first_token_timeout_s`).
여기에 콜드스타트 커넥션(`state_store_connect_timeout_s` **5.0s**)이 끼면 더 나빠진다.
즉 **pg-profile 이 다중 초 단위로 느려지면 `progress` 프레임을 내보내기도 전에 관문 예산이
소진돼 이벤트 0건·504가 재현될 수 있다** — **#289는 이 경로를 좁히지만 구조적으로 소멸시키지는
않는다.** 이슈 본문의 "504 경로가 구조적으로 소멸한다"는 표현은 이 문서가 그대로 승계하지
않는다.

소멸시키려면 emit을 이 프렐류드보다 **앞**에 둬야 하는데, 그러면 지금 §2.5 스트림 전 오류
봉투(`503 STATE_UNAVAILABLE`)로 나가는 pg-profile 장애가 in-stream `error`로 바뀐다 — **한
계약 위반을 다른 계약 위반과 맞바꾸는 것**이라 이건 구현 선택이 아니라 **계약 결정 사항**이다
(§2.5 봉투를 좁히는 승인이 필요하고, 그 승인은 이 PR이 재료를 대는 바로 그 FE/BE 협의에서
받는다).

두 실패 모드는 **성격이 다르다.** #277이 잡은 것은 **정상 동작 중**의 지연(검색 재시도·자동
완화는 장애가 아니라 설계된 경로)이 관문을 넘긴 경우였고, 여기 남는 것은 **pg-profile 장애
상태**에서의 지연이다 — 후자는 그 턴이 어차피 실패하는 상황이라 위험도가 같지는 않지만,
그렇다고 "구조적으로 소멸했다"고 말할 수는 없다.

## 5. 구매자·판매자 대칭

판매자 스트림(§3.2)은 이미 `meta`가 매 스트림 첫 프레임이라 같은 관문 문제가 없다. **이 PR 은
판매자 코드를 바꾸지 않는다.** 다만 페이로드 형태를 대조하면:

| | 판매자 `progress`(§3.2, 기구현) | 구매자 `progress`(이 초안) |
|---|---|---|
| 필드 | `{"text": "…"}` | `{"stage": "…", "message"?: "…"}` |
| 발생 시점 | analysis 진행 중 0회 이상, `meta` 뒤 | 0~1회 — 정상 턴은 정확히 1회·스트림 첫 프레임, emit 전 종료 턴은 0회(§3) |
| 목적 | 로딩 표시(자유 텍스트) | 단계 식별(기계 판독 가능한 enum) + 선택적 문구 |

이슈 본문의 "판매자 progress 와 형태를 맞춰 FE 수신부 재사용"은 **지금 그대로는 성립하지
않는다** — 필드 자체가 다르다(`text` vs `stage`+`message`). 이걸 협의 항목으로 올린다. 선택지:

- **(a) 구매자도 `text`만 쓴다** — 판매자와 완전히 같은 셰이프. FE 수신부(파서)를 그대로
  재사용할 수 있으나, `text`는 자유 문자열이라 FE 가 단계별로 다른 UI(스피너 문구 vs 아이콘
  전환 등)를 주려면 **문자열 매칭**에 의존해야 한다(취약 — 서버가 문구를 바꾸면 FE 분기가
  깨진다).
- **(b) 구매자는 `stage`+`message`, 판매자는 후속 개정에서 `stage`를 추가**(기존 `text` 유지,
  하위호환 가법 변경) — **권장안.** 판매자도 이후 필요해지면 `{"text": "…", "stage"?: "…"}`로
  넓히면 되고, 지금 당장 판매자 코드를 바꿀 필요가 없다(이 PR 범위 밖). 구매자는 처음부터
  기계 판독 가능한 `stage`를 갖는다.
- **(c) 두 스트림을 계속 다르게 둔다** — 페이로드 통합을 포기. FE 수신부는 이벤트 타입별로
  이미 분기하므로(§6 참조) 실무 비용은 크지 않을 수 있으나, "형태를 맞춰 재사용"이라는 이슈
  본문의 원 동기를 포기하는 셈이다.

이 초안은 (b)를 권장하지만, **최종 결정은 FE/BE 협의**에 맡긴다.

## 6. FE 하위호환 논거 — 🔴 FE 확인 필요

"미지 `type`은 무시"가 이 계약 신설의 안전 전제다. FE 파서가 `switch(type)` 형태로 이벤트를
디스패치한다면(§1.2, "각 이벤트로 디스패치한다"는 서술이 있음, api-spec.md L1705) 기본 분기가
무시로 떨어진다고 **추정**된다. 그러나 **이 저장소(jarvis-ai)에서는 FE 코드를 확인할 수
없다** — 추정을 사실처럼 쓰지 않는다.

**🔴 FE 확인 필요**: jarvis-frontend 저장소의 SSE 파서(`EventSource`/fetch 스트리밍 소비부,
아마 `applySuggestion` 근처나 이벤트 디스패치 스위치문)에서 `default` 분기가 안전하게
무시하는지(로그만 남기고 렌더 실패 없음) 확인해야 한다. 확인 전에는 플래그를 켤 수 없다 —
FE 가 미지 `type`에서 예외를 던지거나 렌더를 중단하면, `progress` 신설이 오히려 모든 구매자
턴을 깨뜨린다.

## 7. #277 재시도 스킵 원복 조건 — **이 PR 은 원복하지 않는다**

등재(정본 Notion CH-2 + `docs/api-spec.md` §3.1)·배포·**플래그 on**이 모두 된 뒤에만 아래를
되돌린다(목록은 이슈 코멘트 좌표 기준):

| 항목 | 위치 |
|---|---|
| 재시도 억제 컨텍스트 매니저 사용 2곳 | `app/agents/buyer/recommendation/graph.py`(`with spring_client.suppress_search_retry()`, 본 검색 gather · 자동 완화 probe) |
| `suppress_deferred_search_retry` 계산 | `app/agents/buyer/recommendation/graph.py` |
| 복구 가드(기본 false) | `app/core/config.py::search_retry_on_deferred_conditions` |
| 기동 검증 분기(미룬 턴 직렬 합) | `app/core/config.py::_require_search_retry_within_stream_budget` |
| 동작을 고정한 테스트 | `tests/unit/test_recommendation.py` L2350·L2376·L2402·L2429, `tests/unit/test_config.py` L177~L253 |

**지우면 안 되는 것** — 순서 자체를 고정한 테스트 2개는 원복 이후에도 유효하다:
`test_recommendation_delays_conditions_until_search_and_auto_relax_probe_finish`(L3192),
`test_recommendation_emits_conditions_before_search_when_auto_relax_disabled`(L3227). `progress`가
먼저 나가도 `conditions`는 여전히 검색·완화 뒤이기 때문이다(§3 참고).

`conditions` 뒤의 **완화 칩 probe는 억제 대상이 아니다**(첫 이벤트 예산 밖) — 원복 시 이 경계도
함께 지워야 한다.

## 8. 실측 — 전/후 비교

하네스: `evals/first_event_budget/measure_first_event.py`(#277 자산, 그대로 재사용). 산출물
`payload["config"]`에 `progress_events_enabled` 필드를 추가해 아티팩트가 스스로 측정 조건을
말하게 했다(하네스 로직·시나리오·seed는 변경 없음).

- flag-off: `evals/first_event_budget/results/measure-289-20260805-flag-off.json`
- flag-on: `evals/first_event_budget/results/measure-289-20260805-flag-on.json`

| 시나리오 | off p50 | off max | off first_event_type | on p50 | on max | on first_event_type |
|---|---:|---:|---|---:|---:|---|
| `A_nondeferred_fast` | 384.4ms | 408.3ms | `conditions` | 12.8ms | 20.4ms | `progress` |
| `B_deferred_hit` | 718.9ms | 848.3ms | `conditions` | 12.2ms | 71.3ms | `progress` |
| `C_deferred_probe` | 1017.0ms | 1183.3ms | `conditions` | 12.1ms | 15.9ms | `progress` |
| `D2_deferred_worst_slow_ok` | 3370.1ms | 3496.7ms | `conditions` | 12.0ms | 13.8ms | `progress` |
| `D3_deferred_worst_no_retry` | 6869.8ms | 6988.2ms | `conditions` | 11.6ms | 14.2ms | `progress` |
| `G_nondeferred_slow_search` | 371.9ms | 381.2ms | `conditions` | 12.9ms | 14.5ms | `progress` |

원본 산출물: `evals/first_event_budget/results/measure-289-20260805-flag-off.json` /
`…-flag-on.json`.

**기대와 달랐던 지점 — 숫자를 그대로 보고한다.** 이슈 본문·§8 예고는 "미룬 턴이 A/G 수준
(0.4s대)으로 수렴"이었는데, 실측은 **A/G를 포함한 6개 시나리오 전부가 ~12~15ms로 수렴**했다
(D3만 max 14.2ms, B만 max 71.3ms — 워밍업 이후 잔차로 보인다). 원인은 emit 지점이 애초
설계(§4)한 대로 **decompose 자체보다 앞**이기 때문이다 — `may_auto_relax` 턴의 검색·재시도·
완화 구간뿐 아니라, **nondeferred 턴(A/G)이 `conditions` 이전에 거치는 재구매 store pg
왕복(`_load_persisted_repurchase`, §4 하네스 코멘트 "재구매 store pg 왕복이 첫 이벤트 예산에
들어가는지가 이 측정의 관심사")·category mapping·decompose LLM 호출 자체**까지 전부 관문
밖으로 빠진다. 즉 이 하네스에서 관측된 개선은 "미룬 턴만 정상화"가 아니라 "모든 턴의
first-token이 turn 진입 직후로 앞당겨짐"이며, 이슈가 예고한 것보다 **더 크다.**

**여전히 관문 안에 남는 구간 — flag-on이 0ms가 아니라 ~12ms인 이유.** emit(`progress_frame`
yield, `app/agents/buyer/graph.py` L531)보다 **앞**에 있는 세션·스레드·장바구니 프렐류드는
플래그를 켜도 관문 밖으로 빠지지 않는다: `context_id` 검증(L425)·`ensure_thread_adopted`
(L428)·`thread_store.get`(L450)·회원 턴의 `read_profile_summary`(L462 — **profile 읽기는
관문 안이다**, emit보다 앞이라는 게 이번 리뷰로 정정한 부분)·`cart_store.get_pending`(L471)·
`cart_store.get_last_reco_state`(L486)·`screen` 프롬프트 구성. 이 프렐류드 pg 왕복·읽기의
합이 바로 flag-on 실측 p50 ~12~15ms의 정체다.

**두 얼굴을 함께 읽는다 — 평상시 ~12ms, 그러나 상한은 12s.** 측정값이 낮다는 것이 상한이
안전하다는 뜻은 아니다. 이 프렐류드 중 pg 왕복 4회(§4 "남는 위험" 표)는 각각
`state_store_query_timeout_s`(3.0s)로 묶여 있어 **직렬 최악 12.0s**까지 벌어질 수 있고, 이는
first-token 상한(10.0s)을 넘는다. 평상시 관측치(~12ms)와 이론적 상한(12.0s)의 간극이
1,000배 — 정상 경로에서 낮게 재는 실측이 장애 시나리오의 안전을 보증하지 않는다는 것을
이 표가 그대로 보여준다.

## 8-1. 측정되지 않은 것 / 관문 안팎 재정리

이 하네스는 `ScriptedLLM`이라 decompose LLM head가 이미 제외돼 있다. flag-off 대비 개선분에는
LLM head 절감(#151 baseline p95 ≈3.0s)이 **잡히지 않는다** — 실제 운영에서 `progress` 켜기의
이득은 이 표의 수치보다 **크다**(실제로는 emit 이후 decompose 호출에 LLM head가 걸리는데,
여기서는 head 자체가 ScriptedLLM으로 ~0ms라 그 절감분이 표에 드러나지 않는다).

- **관문 안(emit 이전, flag-on 실측 ~12ms의 구성)**: `context_id` 검증·`ensure_thread_adopted`·
  `thread_store.get`·**`read_profile_summary`**(회원 턴)·`cart_store` 2회 읽기·`screen` 프롬프트
  구성.
- **관문 밖(emit 이후, "측정된 것")**: decompose LLM 호출·category mapping·재구매 store
  pg 왕복·I-1 검색·재시도·자동 완화 probe.
- **측정되지 않았지만 구조적으로 관문 밖인 것**: decompose LLM head(#151, ScriptedLLM이라
  하네스 수치에는 ~0ms로 잡힘).

## 9. 미해결 협의 항목 체크리스트

- [ ] **🔴 FE 확인 필요(§6)** — jarvis-frontend SSE 파서의 미지 `type` 무시 여부. 확인 방법:
      jarvis-frontend 저장소에서 이벤트 디스패치 스위치문의 `default` 분기 확인.
- [ ] **stage 어휘 확정(§2)** — `analyzing` 단독 유지 vs `searching`으로 축소(비추천 턴
      오라벨링 대가 수용 여부).
- [ ] **구매자·판매자 페이로드 통합 여부(§5)** — (a)/(b)/(c) 중 선택. 이 초안은 (b) 권장.
- [ ] **정본(Notion CH-2) 개정 합의** — 이 문서를 입력으로 FE/BE 협의.
- [ ] **`docs/api-spec.md` §3.1 사본 동기화** — 정본 등재 후.
- [ ] **플래그 on 전환 + #277 재시도 스킵 원복(§7)** — 등재·배포 후 별도 PR. **운영(jwks 인증
      **또는** staging/production 환경) 기동 가드(`app/core/config.py::_require_pepper_in_prod`의
      #289 분기) 제거가 이 절차의 일부다** — 등재·FE 확인이 끝나기 전에는 이 가드가 두 축
      중 하나만 해당해도 운영에서 플래그를 못 켜게 막는다(R4-1, 판정 축 확장 R5-1).
- [ ] **프렐류드 직렬 예산 vs first-token 상한**(§4) — 상태 저장소 프렐류드 직렬 합(최악 12s)이
      first-token 상한(10s)을 넘는다. 선택지: (a) emit을 프렐류드 앞으로 올리고 pg 장애를
      in-stream error로 내보낸다(§2.5 봉투 축소 — 계약 변경), (b) 프렐류드 호출을 병렬화하거나
      예산을 재배분한다(#288 소관), (c) 현행 유지 + 위험 수용. **이 PR은 (c)로 두고 결정을
      협의에 넘긴다.**
