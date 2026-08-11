# 취향 추출 골든셋 프로브 (#462)

대화 세션(여러 턴의 사용자 발화) → `generate_session_delta`(실 LLM, smart) → `should_promote`
(게이트) → `resolve_triple`(resolver, category kind 만 실 임베딩·pg-catalog)로 이어지는
**배포 파이프라인 그대로**의 취향 추출 정확도를 세션당 N회 실 LLM 반복 분포로 잰다.
`evals/category_probe`(#331)·`evals/intent_probe`(#260) 의 확립된 규약(전역 페이서·실패는
표본이 아님·단일 실행 판정 금지·trivial baseline 의무)을 승계한다. #356(PR #410)이 만든 구조화
트리플 추출 경로가 재는 대상이고, `scripts/probe_delta_prompt_356.py`(라벨 없는 분포 프로브)가
"몇 개 뽑았나"를 쟀다면 이 하네스는 "**맞게** 뽑았나"를 라벨 기준으로 잰다.

## 이건 `evals/goldenset` 이 아니다

| | `evals/goldenset` | 이 프로브 |
|---|---|---|
| 본체 | **추천 품질**(idealOrder·expectedFilters·hardConstraints) | **취향 추출·resolver 정확도** |
| 입력 | 단발 검색 질의 | **다턴 대화**(세션 버퍼) |
| 라벨 | 상품 순위 | 기대 트리플(kind·label·predicate) |
| 평가 | 결정론 1회, nDCG·MRR·P@k | **확률 분포**(세션당 N회) |

두 산출물의 숫자를 섞지 말 것. `evals/goldenset` 의 질의를 이어붙여 유사 세션으로 쓰는 시도는
**이미 실패로 확인됐다** — PR #410 「분포 프로브 실측」이 검색 질의로 만든 세션이 승격률 100%로
포화돼 측정이 성립하지 않았음을 기록했다(이슈 배경 ⚠️).

## 실행

```bash
# 실 LLM (수동, 비용 발생)
uv run python -m evals.taste_probe --out artifacts/taste-probe/run1

# API·pg 없이 배관만 확인 (가짜 LLM·가짜 카탈로그)
uv run python -m evals.taste_probe --out /tmp/tp --dry-run

# N·페이서 조정
uv run python -m evals.taste_probe --out artifacts/run2 --n 8 --rpm 30
```

tier 는 **`smart` 고정**이다 — `generate_session_delta` 가 내부에서 `tier="smart"` 로 부르므로
CLI 에 `--tier` 플래그가 없다(`app/agents/profile/builder.py` 호출부와 어긋나면 다른 걸 재는
것이 된다). `--seed` 는 **manifest 기록 전용**이다 — dry-run 결함 주기·라이브 호출 순서 어느
쪽에도 영향을 주지 않는다(`evals/category_probe` 와 동일 규약).

`--concurrency` 기본값은 **1(순차)** 이다 — 라이브 런의 공유 `RecordingLLM`
(`evals/model_eval/recording.py`)이 호출마다 `unittest.mock.patch("app.core.llm._record_usage",
...)` 로 **모듈 전역**을 패치한다. 동시 호출은 usage·예산 집계를 오염시키고, 먼저 끝난 호출이
패치를 조기 원복해 마지막 호출이 이전 패치(`MagicMock`)를 전역에 영구히 남길 수 있다 — 이 하네스가
만든 결함이 아니라 `evals/model_eval` 공유 인프라의 성질이라 여기서 고치지 않는다(별건 보고).
150콜 규모(30세션×N=5)는 순차로도 충분히 빠르다 — `--concurrency` 를 1보다 크게 올리는 것은 그
위험을 감수하는 선택이다.

**실 LLM 런 전에 `LLM_PROVIDER` 로 설정된 provider 의 크레딧·API 키가 유효한지 먼저 확인하라**
— 무효면 30~150콜을 전부 `transportError` 로 헛되이 태우고서야 원인을 알게 된다(아래 「실 LLM
기준선」절 참조).

## 실 LLM 기준선

> 🔴 **[#581] 아래 정본 판은 무효다 — 재측정 대기.** 열린 밴드 도입으로 `_DELTA_SYSTEM` 과
> 골든셋이 함께 바뀌어, 이 판의 `promptSha12`·`datasetVersion` 이 가리키는 상태는 저장소에
> 더는 없다(현재 데이터셋은 `2026-08-11.1`). **새 런을 이 수치와 비교하지 말 것** — 아래
> 「다른 해시끼리 비교 금지」가 그대로 적용된다.

정본은 `evals/taste_probe/baselines/openai-20260809-n5/`(provider=openai · model=gpt-5.6-luna ·
N=5 · promptSha12=`f1087ac09a78` · datasetVersion=`2026-08-08.2`) — 정본 선언 표·해석은
`evals/taste_probe/baselines/README.md` 를 보라. 2026-08-08 확인 시점에는 `LLM_PROVIDER=openai`
크레딧 소진(429)·대안 `anthropic` 키 인증 실패(401)로 막혀 있었으나, 2026-08-09 키 교체로 해소돼
첫 판을 생성했다(재현 오류 문면은 baselines/README.md 「과거 상태」절에 보존).

## CI 에서 돌리지 않는다

실 LLM·실 임베딩·라이브 pg 호출이라 비용·비결정론이 붙는다. **수동 실행 도구**다.
`tests/unit/test_taste_probe_*.py`(schema·runner·metrics·cli 4파일)는 전부 가짜
(`ScriptedDeltaLLM`·`NoiseFalsePositiveLLM`·`FakeCatalog`)라 CI 에서 API/pg 콜이 0이다.
`test_taste_probe_cli.py` 는 `cli.main` 자체를 `--dry-run` 으로 실행해 배선(시임·산출물 쓰기·
종료 코드·결정론)을 검증한다.

## 데이터셋 — 슬라이스와 쿼터

`fixtures/sessions.json`(30세션, v`2026-08-11.1`) — `fixtures/manifest.json` 의 sha256 과
대조해 읽는다(불일치 → 종료 코드 2). 외부 경로(`--fixture <path>`)는 대조를 건너뛰되 해시를
산출물에 기록한다.

| slice | 세션 | 내용 |
|---|---|---|
| `kindCoverage` | 10 | kind 7종(brand·category·attribute·priceBand·ratingBand·product·situation) 전부 최소 1세션 + category·priceBand·attribute 가중 3세션 |
| `polarity` | 4 | 2쌍 — 브랜드 선호/회피, 속성 선호/회피 |
| `repetition` | 3 | 다른 표현으로 반복(2) + 축자 반복이 `profile_buffer_repeat_cap`(2)에 먹히는 것 자체를 재는 세션(1) |
| `conflict` | 3 | 선호 → 회피 전환. 기대는 **전환 후 상태**(avoids)만 |
| `noise` | 10 | 취향 신호 0(잡담·배송·주문·환불·계정 문의). `expectedTriples: []` |

비노이즈 20세션에 **기대 트리플 23개**(라운드 2 재검수 후 — 아래 함정 7 참조, 세션당 1~2개).

`category` kind 의 `accept`/`canonicalLabel` 은 **라이브 pg-catalog `categories` 사전의
canonical 잎 표기 그대로**(`대분류 > 잎`, 예: `음향가전 > 이어폰`, `커피/생수/음료 > 커피`,
`여성의류 > 청바지`, `여성의류 > 원피스`) — `loader.preflight_check_catalog` 가 라이브 런에서
강제한다(`--dry-run` 은 pg 콜 0 이라 부르지 않는다).

`brand`·`attribute`·`situation` 은 통제 어휘가 없어 `verified: false` 로 통과한다(#357 소관,
이 이슈 비범위). 그래서 이 kind 들의 `accept` 는 표기 흔들림을 담아 넉넉히 열거한다(예: 브랜드
`["소니", "SONY"]`) — `normalize_label` 은 대소문자·공백만 접고 한글↔라틴은 못 합친다. 그 사실
자체가 `nodeIdAgreement` 축이 재는 값이다.

## 재는 대상 — 프로덕션 경로

```
세션 발화 목록
  → store.append_session_ctx(...)              # 프로덕션 캡을 그대로 먹인다
  → generate_session_delta(user_id, thread_key, profile_watermark=..., llm=..., settings=...)
       ├ llm.complete(system=_DELTA_SYSTEM, ...)   # 하네스가 채록하는 클라이언트
       ├ should_promote(...)                       # 게이트 — 프로덕션 함수 그대로
       └ _resolve_delta → resolve_triple(...)      # resolver — 프로덕션 함수 그대로
  → store.get_fact_records(user_id)          # 산출 트리플을 여기서 읽는다(FactRecord.graph_triples)
```

판정(게이트·resolver·밴드 파서·정규화·식별자 산출)은 **전부 프로덕션 함수를 import 해서
부른다** — 하네스에 규칙을 베껴 쓰지 않는다(#380 규약, `probe_delta_prompt_356` 이 세운 전례).
`generate_session_delta` 는 **승격된 fact 문자열 목록**(`promoted`)을 돌려준다 — 트리플은
반환값에 없고 `store.get_fact_records(user_id)` 의 `FactRecord.graph_triples` 에서 읽는다
(`ResolvedTriple.as_payload()` 의 저장 모양, **snake_case** — `node_id`·`type`·`label`·
`verified`·`predicate`·`edge_key` 등. 실제 `model_dump(mode="json")` 을 찍어 확인했다,
`graph_models.py` 에 CamelModel 별칭이 없다). **`promotedCount` 는 이 반환값의 길이를 그대로
쓴다** — `emittedDeltas - gateRejected` 로 되계산하지 않는다: `add_fact` 가 동일 fact 문자열을
store 레코드 1건으로 dedup 해도, 프로덕션 `promoted` 리스트 자체는 개별 게이트 통과 건수를
그대로 세므로 그 갭을 `factDedupCollapsed` 로 따로 드러낸다(아래 러너 절).

## 스토어 격리 — 표본마다 유일한 키, 리셋은 런당 1회

`reset_profile_store()` 는 프로세스 전역 싱글턴을 교체한다. **표본마다 부르지 않는다** —
`--concurrency` > 1 로 여러 세션이 동시에 진행 중일 때 표본마다 리셋하면 다른 세션이 마침
쓰고 있는 전역 스토어를 통째로 갈아치우는 레이스가 된다(store.py 자신의 advisory lock 설계와
정면으로 어긋난다). 대신 **런 시작 시 1회**만 부르고(pg-profile 접속 0 보장), 격리는 표본마다
유일한 `user_id`/`thread_key`(`taste-probe-{sessionId}-{attempt}`)로 확보한다 — InMemoryStore
는 `(namespaceRoot, userId)` 로 네임스페이스를 가르므로 키가 다르면 애초에 겹치지 않는다.
`tests/unit/test_taste_probe_runner.py::test_samples_do_not_leak_facts_across_attempts` 가 이
격리를 실측한다(시도마다 고유한 조작 fact 를 섞어, 격리가 깨지면 산출 트리플 수가 누적돼야
정상인데 매 표본 동일하게 유지되는지 확인).

## dry-run 시임 — 최후 수단

`resolver._resolve_category` 는 임베딩·pg 콜백을 인자로 받지만, `builder._resolve_delta` 가
`resolve_triple` 을 부를 때 그 인자를 넘기지 않는다(`builder.py` 수정은 이 이슈 범위 밖). 그래서
`category` kind 는 `--dry-run`·유닛 테스트에서도 실 임베딩·실 pg 를 타려 한다 — `seams.py` 의
`fake_catalog_seam` 이 `app.pipelines.category_search.exact_lookup`/`search_categories_pg`·
`app.pipelines.embedding.embed_texts` **모듈 속성**을 임시 패치해 막는다(resolver 가 함수 안에서
지연 import 하므로 호출 시점 모듈 속성 읽기가 패치를 그대로 탄다). **라이브 런은 이 시임을
전혀 쓰지 않는다.**

## 러너 — 단계 귀속

세션당 N 표본을 채운다(#260 규약: 실패는 표본이 아니다 — 성공할 때까지 재시도, 상한은
`attempt_multiplier`, 못 채운 세션은 `unfilledSessions`+종료 코드 4). `evals.model_eval.budget.
BudgetExceeded` 는 **표본 실패가 아니라 런 중단 신호**라 재시도하지 않고 즉시 전파한다(그래야
`cli.py` 의 예산 초과 종료 코드 3 이 실제로 도달한다 — 종전엔 표본 실패로 삼켜져 도달 불가능한
죽은 코드였다).

표본 하나마다 다음 진단을 기록한다 — 미탐이 어느 단계에서 났는지 가른다:

- `emittedDeltas` — LLM 원문 응답을 채록해 `extract_json`(프로덕션 함수)으로 다시 파싱해 관측
  (판정 아님).
- `promotedCount` — `generate_session_delta` 의 반환값 길이(프로덕션 값, 위 절 참조).
- `gateRejected` — 채록한 델타 중 `should_promote`(프로덕션 게이트)가 거절한 수.
- `resolverDroppedByKind`(kind→건수)/`legacySchemaNoKind` — 승격됐는데 그래프 트리플이 안 붙은
  fact 를 `FactRecord.fact` 문자열로 델타에 역매핑해 **kind·label 을 함께** 얻는다(추정 아님).
  `kind` 가 있는데 트리플이 안 붙은 건은 `resolverDroppedByKind`, `kind` 자체가 없는 구스키마
  조기 반환(`_resolve_delta` 가 "정상 경로"로 문서화한 분기)은 별도로 `legacySchemaNoKind` 에
  센다 — 하나로 섞으면 "resolver 가 실패했다"와 "구 프롬프트 호환 경로를 탔다"를 못 가른다.
  같은 fact 문자열을 내는 델타가 여럿이면 store dedup 으로 한 레코드에 합쳐져 첫 델타로만
  귀속하는 근사가 섞인다 — 그 근사가 발동했는지는 `factDedupCollapsed`
  (`promotedCount - fact 레코드 수`)로 드러난다. 0 이 아니면 그 표본의 kind 귀속은 **하한**이다.
- `bandLabelRejected` — 밴드 kind 인데 `_resolve_band`(프로덕션 파서)가 드롭한 `{kind, label}`
  쌍. kind 를 함께 실어야 "정상 라벨이 거부된 것"과 "kind 오분류로 밴드 형식이 안 맞은 것"이
  갈린다(예: `ratingBand` 로 잘못 분류된 `"30000-50000"` 은 라벨 자체는 정상이다).
- `schemaViolation`/`transportError`(타입별) — 표본 자체가 실패한 시도. 분류는
  `is_output_length_error`/`is_timeout_error`(프로덕션 판별기)를 그대로 부른다
  (`probe_delta_prompt_356._error_signature` 와 같은 방식). `_RecordingLLM` 이 `llm.complete()`
  가 던진 예외 객체와 `generate_session_delta` 가 최종적으로 던진 예외의 **identity** 를 비교해
  전송 실패(llm.complete 자체가 던짐)와 스키마 위반(성공한 응답을 `extract_json` 이 파싱 실패)
  을 메시지 매칭 없이 가른다.

## 축과 정의

정의는 `metrics.py` 의 함수 데이터로 있고 **산출물(`results.json`·`report.md`)에 그대로
실린다.** 다중 비교 통제(#328 규약 5): **primary confirmatory 는 `recall` 하나**, 사전 등록
2차는 `noiseFalsePositiveRate`·`nodeIdAgreement`(`exploratory: false`). 나머지 축(`missRate`·
`falsePositiveRate`·`sessionExactSet`·baseline·슬라이스 분해)은 전부 `exploratory: true` 를
스스로 단다.

| axisId | 분자 | 분모(N=5, 실제 골든셋 v2026-08-08.2 기준) |
|---|---|---|
| `recall`(primary) | 매칭된 기대 트리플 수(최대 이분 매칭, 아래 매칭 규칙) | 노이즈 제외 20세션의 기대 트리플 23개 × 5 = 115 |
| `missRate` | 1 - recall 의 분자 | 같은 115 |
| `noiseFalsePositiveRate`(2차) | noise 세션 표본에서 산출된 트리플 총수 | noise 10세션 × 5 = 50 |
| `falsePositiveRate` | 어떤 기대 트리플과도 매칭 안 된 산출 트리플 수 | 산출 트리플 총수(전체) |
| `nodeIdAgreement`(2차) | 매칭 트리플 중 nodeId 일치 수 | 매칭 트리플 수 |
| `sessionExactSet` | 산출 집합이 기대 집합과 정확히 일치(여분 0·누락 0) | 세션 30 × 5 = 150 |
| `baselineRecall`/`baselineFalsePositiveRate`/`baselineNoiseFalsePositiveRate`/`baselineSessionExactSet` | §trivial baseline | |

**매칭 규칙**: `produced.node.type == expected.kind` 그리고 `produced.predicate ==
expected.predicate` 그리고 `normalize_label(produced.node.label) ∈
{normalize_label(a) for a in expected.accept}`. **최대 이분 매칭**(Kuhn 알고리즘, 증강 경로)
이다 — 그리디 1:1 은 기대/산출 배열 순서에 매칭 크기가 의존할 수 있다(예: `E1.accept={A,B}`,
`E2.accept={A}`, `produced=[A,B]` 면 순서만 바꿔 recall 50%↔100%). 최대 매칭의 크기는 그래프
이론상 순서 불변이다 — `test_match_sample_score_is_order_invariant` 가 이를 고정한다.

진단 카운터(합불 아님): `emittedDeltas`·`promotedCount`·`gateRejected`·`resolverDroppedByKind`·
`resolverDroppedCount`·`legacySchemaNoKind`·`unprojectedFacts`(= 둘의 합)·`factDedupCollapsed`·
`verifiedFalseCount`·`bandLabelRejected`·`schemaViolation`·`transportError`(타입별).

**kind 오분류 행렬**: 미매칭 기대 트리플 × 미매칭 산출 트리플에서 **라벨이 실제로 일치하는
쌍**을 먼저 짝지어 `(expectedKind → producedKind)` 로 센다(`metrics.build_confusion`) — 이게
진짜 kind 오분류다. 남은 미매칭 기대는 `(expectedKind → ∅)`, 남은 미매칭 산출은
`(∅ → producedKind)`. 종전(위치 짝짓기, `zip_longest`)은 "라벨은 맞는데 kind 만 틀린" 진짜
오분류와 "아무 관계 없는 오탐"을 같은 칸에 넣었다. `report.md` 표 + `confusion.csv`.

**predicate 오분류 행렬**(`metrics.build_predicate_confusion`): kind·라벨은 맞는데 predicate
만 다른 쌍의 `(expectedPredicate → producedPredicate)` 빈도 — `polarity`·`conflict` 슬라이스가
실제로 재려는 실패("소니는 별로예요"가 `likes 소니` 로 저장되는 극성 반전,
`resolver._decide_predicate` 주석이 기록한 실측 결함 유형)를 직접 드러낸다.
`report.md` 표 + `predicate_confusion.csv`.

`dropped.csv`(sessionId, sampleIndex, kind, label) — resolver 가 드롭한 승격 델타 전량, kind
귀속 포함.

## trivial baseline

정의(`baseline.py`): **"아무것도 안 뽑는다"**(`profile_graph_delta_enabled=False` 근처). LLM 콜
0, 결정론 — 골든셋만으로 계산되고 러너 실행이 필요 없다(baseline 산출은 항상 빈 산출이므로).
`baselineRecall = 0/전체기대`, `baselineFalsePositiveRate = 0/0`(baseline 산출 트리플 수가
분모라 정의상 0 — 파이프라인 축과 분모가 달라 직접 비교가 안 된다), `baselineNoiseFalsePositiveRate
= 0/(noise 세션 × N)`(사전 등록 2차 축 `noiseFalsePositiveRate` 와 **같은 분모**로 대조하려고
`score_baseline(golden_set, n=n)` 이 `n` 을 받는다), `baselineSessionExactSet` = noise 세션만
우연히 일치 / 전체 세션. 같은 런의 같은 `report.md` 에 파이프라인 수치와 나란히 싣는다.

baseline 은 오탐 축에서 정의상 완벽하고 미탐 축에서 정의상 최악이다 — 따라서 **추출이
개선인지는 "recall 이 0보다 얼마나 큰가"를 "오탐을 얼마나 치렀나"와 함께 봐야** 판정된다. 한
축만 보면 baseline 을 이기는 것이 자명하거나 불가능해 보인다.

## 표본 사전 산정 (규약 4)

primary 축 `recall` 의 기본 분모(노이즈 제외 20세션 · 기대 트리플 23개 · N=5 = 115)에서
p̂=0.5(최대 분산 가정) 기준 95% CI 반폭(**이항 근사** — 아래 군집 표본 경고 참조):

```
SE = sqrt(0.5 × 0.5 / 115) ≈ 0.0466
95% CI 반폭 = 1.96 × 0.0466 ≈ 0.091 ≈ 9%p
```

이 표본 크기는 **방향 판정용**(프롬프트·resolver 변경이 명백히 개선/퇴행했는지)이지 **미세
차이 판정용**이 아니다. 슬라이스 분해(`axesBySlice`)는 세션 수가 더 적어(3~10) 반폭이 더 커
전부 `exploratory` 다.

**⚠ 이 분모는 독립 표본이 아니라 군집 표본이다.** `recall` 의 분모(115)는 독립 관측 115개가
아니라 **같은 20세션을 각 N=5회 반복**한 것이다 — 같은 세션의 N개 표본은 같은 발화·같은 게이트
임계·같은 resolver 임계를 공유해 서로 상관돼 있다(세션 내 오차가 세션 간 오차보다 작다).
독립의 단위는 **세션**(20개)이지 표본(115개)이 아니므로, 위 이항 근사 반폭(9%p)은 **실제
신뢰구간보다 좁게 잡힌 값**이다 — "방향 판정용" 이라는 위 문장이 그래서 나온다. 세션 내
상관까지 반영한 정확한 반폭(예: 군집-강건 표준오차)은 이 이슈 범위 밖이며, 미세 튜닝을 재려면
`--n` 을 올리는 것보다 **세션 수(20)를 늘리는 쪽**이 통계적으로 더 유효하다.

미세 튜닝을 재려면(방향 판정을 벗어나려면) `--n` 을 올린다: ±5%p 를 원하면 SE≈0.0255,
분모 D ≈ (1.96/0.0255)² × 0.25 ≈ 384 → 20세션 기준 세션당 N ≈ 19~20(위 계산은 이항 근사이며,
군집 상관을 감안하면 실제 필요 N 은 이보다 더 크다).

## ⚠ 단일 실행으로 채택 판정 금지

같은 프롬프트·같은 임계의 독립 실행에서도 위 CI 반폭만큼 축이 흔들린다. 채택 판정은 **독립
2~3회** 분포로 한다(`evals/intent_probe`·`evals/category_probe` 와 동일 규약).

## MFT 만 쓰는 근거

이 하네스는 `testType: "MFT"` 만 쓴다. **INV**(같은 의미 다른 표기 → 같은 결과)는 `category_probe`
가 카테고리 표기 변형(색상 동의어 등)에 이미 적용 중이고, 이 하네스에서 표기 변형 민감도는
`brand`/`attribute`/`situation` 의 `accept` 목록 자체(여러 표기를 모두 정답으로 인정)가 이미
흡수한다 — 별도 INV 그룹으로 다수결 합의를 재는 것이 이 취향 추출 축에서는 이중 계측이다.
**DIR**(방향 술어, 라벨 불필요)은 이 축에서 성립하는 라벨-불필요 방향 관계가 없다 — "취향이
더 강해져야/약해져야 한다"류의 판정 잣대가 취향 추출에는 없고, `conflict` 슬라이스(선호→회피
전환)가 그 방향성 있는 케이스를 라벨 기반(MFT, 최종 predicate=avoids)으로 이미 담당한다.

## 함정

1. **정답 누출 금지** — priceBand/ratingBand `canonicalLabel` 정규형(`"30000-50000"`)이나 `" > "`
   포함 category `canonicalLabel` 이 발화 원문에 그대로 있으면 안 된다(`schema.py` 가 강제,
   category_probe 함정 3 승계) — 정규형을 발화에 심으면 LLM 이 베껴 쓰는 것을 추출 능력으로
   오독한다.
2. **`profile_buffer_repeat_cap`(기본 2)가 축자 반복을 먹는다** — 동일 문자열 3번째부터는 세션
   버퍼에 안 들어간다(`rep-verbatim-repeat-cap` 세션이 이 동작 자체를 재려는 의도로 존재).
3. **brand/attribute/situation 은 `verified: false` 가 정상**(#357 비범위) — 미구현 표식이
   아니라 정확한 답이다(통제 어휘가 아직 없다).
4. **프롬프트 해시가 바뀌면 과거 기준선과 비교 금지** — `run_manifest.json.hashes.
   deltaSystemPrompt`(sha12 는 `report.md` 첫 줄에도 실린다)로 확인한다(#198 전례 — 지시 한
   줄이 성공률 3/3 → 1/3 로 회귀시킨 적 있다).
5. **`datasetHash` 가 바뀌면 baseline 전량 재실행**(규약 8) — 세션 하나만 고쳐도 표 전체가
   달라진다.
6. **검색 질의로 세션을 만들면 게이트가 포화된다**(실측, PR #410 「분포 프로브 실측」) — 이
   골든셋의 turns 는 전부 취향 표현이 실제로 든 자연스러운 한국어 발화다.
7. **라벨 안 된 취향 신호가 오탐을 부풀릴 수 있다** — 발화에 여러 개의 장기 취향 신호가 섞여
   있는데 기대 트리플이 하나만 라벨돼 있으면, 모델이 나머지를 정확히 뽑아도
   `falsePositiveRate`/`sessionExactSet` 이 그 성공을 실패로 채점한다(라운드 2 리뷰 실측:
   `kc-attribute-02` 의 "향이 강하지 않은 것도 중요해요", `pol-brand-prefer` 의 "쿠션감이 특히
   마음에 들어요"). 이 골든셋은 v2026-08-08.2 에서 비노이즈 20세션 전량을 다시 훑어 그런 신호를
   라벨하거나 중립화했다 — 새 세션을 추가할 때도 발화당 "장기 보관할 만한 취향 표명"을 전부
   세어 라벨했는지 확인해야 한다(`conflict` 슬라이스의 과거형 선호는 의도적 예외 — 전환 후
   상태만 라벨한다).

## 비범위

- resolver 거리 임계 재측정(OPEN-G1, `graph_node_distance_max`·`graph_node_override_margin`) —
  이 골든셋이 재측정 근거로 쓰일 수 있으나 별건이다(이슈 #462 비범위, #344 소관).
- 브랜드 통제 어휘 확보(C-28, #357).
- `scripts/probe_delta_prompt_356.py` 수정(읽기만 했다).
- `evals/model_eval/recording.py` 의 동시성 하의 전역 patch 결함(위 `--concurrency` 절) — 공유
  인프라 소관, 이 라운드는 기본값을 안전 쪽(1)으로 내려 노출만 차단했다.

## run manifest

`evals/metrics/run_manifest.py::build_run_manifest` 를 확장한다(`manifest.py`).
`hashes.deltaSystemPrompt`(`builder._DELTA_SYSTEM` sha256, `manifest.
delta_system_prompt_sha256()` 를 `report.md` 헤더와 공유) · `hashes.datasetFixture` ·
`hashes.tasteProbeModules`(하네스 `*.py` 파일별 sha256) · `thresholds`(게이트·resolver 임계
7종) · `dictionary`(라이브 런에서만 `evals.category_probe.manifest.dictionary_fingerprint`
재사용 — category 정확도가 같은 pg-catalog 사전에 종속되므로) · `axisDefinitions` ·
`singleRunNotAVerdict`/`notGoldenset` 문구를 남긴다.
