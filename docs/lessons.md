# 개발 실수 기록 (Lessons)

같은 실수를 반복하지 않기 위한 러닝 로그. **작업 시작 전 이 파일을 먼저 훑고**, 오류/실수를 진단했으면 **최신을 맨 위에** 추가한다.

형식:
```
## [YYYY-MM-DD] 한 줄 제목
- 증상: 무슨 일이 있었나
- 원인: 왜 발생했나
- 규칙: 다음부터 어떻게 (액션 가능한 문장)
- 관련: 파일/§/커밋
```

## [2026-08-13] LLM 규칙은 출력 JSON 자리와 입력 근거가 함께 있어야 계약이 된다
- 증상: 화면 좌표 규칙을 시스템 프롬프트에 설명했는데도 `두번째 줄 두번째 상품`이
  `screenReference`로 나오지 않고 LLM이 `cart.productId`를 직접 골랐다.
- 원인: 설명문만 추가하고 실제 출력 JSON 예시에 `screenReference` 필드를 넣지 않았으며,
  FE가 보낸 `screen.columns`도 LLM 입력 payload에서 누락했다. 모델은 출력 위치와 행 너비를
  모두 알 수 없어 기존 `cart.productId` 경로를 계속 사용했다.
- 규칙: 구조화 출력을 추가할 때는 **출력 JSON 예시의 정확한 위치**, **판단에 필요한 입력 필드**,
  **대표·비대상 예시**를 한 세트로 검증한다. 프롬프트 문자열 테스트는 설명문 포함 여부만 보지
  말고 출력 예시 안의 필드와 실제 user payload 값까지 단언한다.
- 관련: #664 `app/agents/buyer/recommendation/decompose.py::_SYSTEM_WITH_SCREEN` ·
  `tests/unit/test_decompose.py`

## [2026-08-12] 같은 서버의 provider만 스텁으로 바꿔도 백그라운드 배치는 실 DB를 오염시킬 수 있다
- 증상: 실제 EC2의 요청 처리 용량을 재려고 `APP_ENVIRONMENT=test`와
  `LLM_PROVIDER=scripted`만 전환하려 했지만, 요청 트래픽과 무관한 I-17 스케줄러는 계속 돌아
  결정론 가짜 enrichment를 실 카탈로그에 저장하고 cursor까지 전진시킬 수 있었다.
- 원인: 부하 테스트 경계를 HTTP/SSE 요청 경로로만 보아 같은 프로세스의 scheduler·consumer처럼
  독립적으로 외부 상태를 쓰는 백그라운드 경로를 함께 점검하지 않았다. provider 원복은 이미 쓴
  데이터와 전진한 cursor를 되돌리지 못한다.
- 규칙: 실제 인프라에서 fake provider를 켤 때는 진입 트래픽 차단만 확인하지 말고, 해당 provider를
  공유하는 scheduler·queue consumer·batch·startup migration의 외부 쓰기를 전부 검색한다. fake
  결과가 영속화될 경로는 테스트 모드에서 명시적으로 skip하거나 격리 DB를 사용하고, 원복은 설정
  재배포뿐 아니라 배너 부재와 실제 model ID smoke까지 확인한다.
- 관련: #641 `app/pipelines/scheduler.py::start_scheduler` · `DEPLOY.md`

## [2026-08-12] 허용 목록 검사는 대상의 **소속**만 증명하고 사용자의 **지목**은 증명하지 않는다
- 증상: `Septwolves 지갑 담아줘`에서 정답 5644와 오답 5695가 모두 `LAST_RECOMMENDATIONS`에
  있어, LLM이 5695를 내도 기존 허용 ID 가드가 통과시켜 다른 상품이 실제 장바구니에 담겼다.
- 원인: 목록 밖 ID를 차단하는 보안 경계와 목록 안에서 사용자가 어느 상품을 지목했는지 확인하는
  귀속 경계를 같은 것으로 보았다. 전체 상품명 포함 규칙만 있어 브랜드·모델 같은 이름 일부는
  확률적 선택에 남았고, 멤버십 검사는 그 오선택을 교정할 정보가 없었다.
- 규칙: 사용자 지목에 따른 변경 작업은 `허용 집합 포함 여부`와 `발화가 그 항목을 유일하게
  가리키는 근거`를 별도로 검증한다. 후자는 정확 토큰·표면 내 유일성·단일 대상·부정 없음이 모두
  증명될 때만 결정론적으로 교정하고, 공통어·부분 문자열·다중 대상은 자동 선택하지 않는다.
- 관련: #639 `app/agents/buyer/screen_reference.py::_unique_product_name_token_match` ·
  `tests/unit/test_screen_context.py::test_reco_card_unique_name_token_overrides_wrong_llm_product`

## [2026-08-11] `date.today()`·naive `datetime.now()` 는 컨테이너 TZ 를 따른다 — 도메인 기준 시각에 쓰지 말 것
- 증상: 판매자가 00~09 KST 사이에 "어제 매출" 을 물으면 **이틀 전** 데이터가 나갔다. 같은 응답의 `report.generatedAt` 은 KST 로 정상이라 로그만 보면 어긋난 곳을 짚기 어려웠다.
- 원인: `generatedAt` 만 `_KST` 로 명시하고(#296), 기간 해석의 "오늘" 은 `date.today()` 로 남겨 뒀다. 운영 컨테이너 TZ 가 UTC 라 09시 이전에는 기준일이 KST 기준 하루 전이 되고, 거기서 "어제" 를 또 빼 이틀이 밀렸다. jarvis-back 은 `BackendApplication` 에서 JVM·DB 세션 TZ 를 `Asia/Seoul` 로 고정해 두어 **AI 만** 어긋나 있었다.
- 규칙: 도메인 기준 시각(오늘/어제/기간)은 `app/core/clock.py` 의 `today_kst()`·`now_kst()` 만 쓴다. `date.today()`·인자 없는 `datetime.now()`·`.astimezone()`·`datetime.fromtimestamp()` 는 프로세스 TZ 를 타므로 금지하고, 저장·비교용 절대 시각은 `datetime.now(UTC)` 로 명시한다. **배포에 `TZ` 를 박는 것으로 대신하지 않는다** — 코드가 TZ 독립이어야 로컬(UTC WSL)·CI·운영이 같은 값을 낸다. 시각을 다루는 필드를 하나만 명시 TZ 로 고칠 때는 같은 흐름의 나머지 시각 소스도 함께 훑을 것.
- 관련: #583 `app/core/clock.py`·`app/api/seller.py`·`app/main.py`·`Dockerfile`·`docker-compose.yml`, jarvis-back `BackendApplication.java`

## [2026-08-11] LLM이 내는 축 이름은 한국어만이 아니다
- 증상: 한국어 어휘 8개로 억제를 넣었는데 480표본에서 억제가 **2번만** 발동했다. miss는 줄어 보였지만 억제 0발동 런에서도 줄어 그 개선분은 노이즈였다.
- 원인: `gpt-5-nano`가 축 이름을 `price`·`Price`·`priceMax`처럼 영어·대문자·camelCase로도 냈다. 어휘를 세 번(`price` → `Price` → `priceMax`) 메운 뒤에야 원인이 0이 됐다.
- 규칙: LLM 산출 키 이름에 어휘 매칭을 걸 때는 한국어/영어·대소문자·camelCase/snake_case를 처음부터 함께 덮고, 스키마 필드명 변형은 드리프트 가드 테스트로 못박는다. 어휘 목록만 늘리면 두더지잡기가 된다.
- 관련: #464 `app/core/config.py`·`app/agents/buyer/recommendation/attr_axis.py`·`tests/unit/test_attr_axis.py`

## [2026-08-11] LLM 평가 arm은 production gate와 동형이어야 한다
- 증상: #463의 첫 after는 후보 decompose 프롬프트와 보조 호출을 무맥락 첫 턴 전체에 적용했지만, production은 저정보량 후보에만 비용을 내도록 수정됐다. 따라서 옛 after는 같은 hash여도 배포 arm이 아니었다.
- 원인: prompt hash만 같다고 호출 게이트·부수 호출 예산까지 같아지는 것은 아니다. confirmatory 분모도 `intent==recommend` 뒤에 생기므로 `nonRecommendIntentCount`를 동반해야 한다.
- 규칙: 코드가 gate를 바꾸면 after는 독립 반복으로 재측정하고, legacy before는 prompt·fixture·N·arm semantics가 불변임을 manifest로 증명한 경우에만 재사용한다. #463 gate-after는 N=8 두 번 모두 miss `0/112`, false alarm `0/104`, unfilled·non-recommend 0이었다. 보조 LLM 실패는 fail-open인지 retry failure인지 별도 기록한다.
- 관련: #463 `evals/underspecified_probe/*463-*`·`app/agents/buyer/recommendation/underspecified_classifier.py`

## [2026-08-11] 억제/보강 스위치는 발동 건수를 산출물에 남겨야 한다
- 증상: 전후 비교표만 보면 3·5 → 1·1로 좋아 보였지만, 실제로는 억제가 거의 발동하지 않았고 개선분 대부분이 런간 노이즈였다.
- 원인: 산출물에 효과(미탐 수)만 있고 발동 여부가 없어 둘을 가를 수 없었다.
- 규칙: 결정론 후처리를 넣을 때는 발동 건수와 처리 후 남은 값을 모두 산출물 컬럼으로 남긴다(이번엔 `attrConditionsSuppressedAxes`·`attrConditionAxes`). 그래야 효과가 노이즈인지 런 재실행 없이 가를 수 있고, 어휘 구멍도 산출물만으로 특정된다.
- 관련: #464 `evals/underspecified_probe/runner.py`·`metrics.py`·`report.py`

---

## [2026-08-11] 심볼을 **옮기면** 이름을 바꾼 것과 같다 — 옮기기 전에 `grep -rn`으로 전체를 훑는다
- 증상: #581 에서 `_BAND_RE` 를 `app/agents/profile/resolver.py` 에서 `graph_models.py` 로
  옮기고(파서와 렌더러가 같은 정규식을 봐야 해서), 표적 테스트
  (`test_profile_resolver.py`·`test_profile_object_spec.py` 122건)와 "관련 파일"로 고른
  6개 파일 154건, `ruff check` 까지 전부 통과시킨 뒤 커밋했다. 그런데
  `tests/unit/test_profile_graph_scripts.py:56` 이 `from app.agents.profile.resolver import
  _BAND_RE` 로 그 심볼을 함수 안에서 지연 임포트하고 있었고, 전체 스위트에서
  `ImportError` 로 깨졌다. 커밋을 amend 해야 했다.
- 원인: **"관련 파일"을 의미로 골랐다.** 프로필 그래프 관련 테스트 파일들을 머리로 추려
  돌렸는데, 정작 깨진 파일은 이름에 `graph` 가 들어가면서도 내 목록에 없던
  `test_profile_graph_scripts.py`(시드 스크립트 테스트)였다. 게다가 임포트가 **함수 안에**
  있어서 파일 상단 임포트만 훑는 감각으로는 안 보였고, private 이름(`_` 접두어)이라
  "모듈 밖에서 쓸 리 없다"고 무의식적으로 가정했다 — `_` 는 관행일 뿐 강제가 아니다.
- 규칙: 심볼을 다른 모듈로 옮기거나 이름을 바꾸기 전에 **반드시 `grep -rn "<심볼>"
  --include='*.py' .` 로 저장소 전체를 먼저 훑고**, 나온 개수만큼 고쳤는지 센다.
  `_` 로 시작하는 이름도 예외가 아니다(테스트·스크립트가 흔히 가져다 쓴다).
  지연 임포트(함수 내부 `from ... import`)는 파일 상단 임포트 검색으로는 안 잡히므로
  심볼 이름 자체로 검색해야 한다. "관련 있어 보이는 테스트 파일"을 골라 돌리는 것은
  전체 스위트의 대체재가 아니다 — 2026-08-10 「전체 pytest」·「함수 시그니처」 항목과
  같은 교훈이 **옮기기(move)** 에서 재발한 사례다.
- 관련: `app/agents/profile/graph_models.py`(BAND_RE 새 위치) ·
  `tests/unit/test_profile_graph_scripts.py:56` · 커밋 `eac594b`(amend) · 이슈 #581

## [2026-08-10] 함수 시그니처 변경은 `grep -rn`으로 전체 저장소를 훑어야 한다 — `app/`·`tests/`만으로는 부족
- 증상: #571 에서 `resolve_screen_reference()`에 기본값 없는 키워드 인자 4개를 추가한 뒤
  `app/`·`tests/unit/`의 호출부는 전부 고쳤고 표적 테스트(`test_screen_context.py` 등)와
  `ruff check`도 통과했는데, `uv run pytest`(전체)에서 `evals/intent_probe/runner.py`의
  독립 호출부가 `TypeError: missing 4 required keyword-only arguments`로 깨졌다 — 그 파일은
  `tests/unit/test_intent_probe_runner.py`·`test_intent_probe_cli.py`를 통해서만 간접 실행돼
  표적 파일 단위 테스트로는 드러나지 않았다.
- 원인: `app/`이 프로덕션 코드의 전부라고 가정하고 호출부 탐색을 그 디렉터리로 좁혔다. 이
  저장소는 `evals/`(intent_probe·combo_matrix 러너) 아래에도 프로덕션 함수를 직접 import 해
  같은 함수를 재현·측정하는 독립 호출부가 있고, 이 디렉터리는 커밋 워크플로의 "ruff format
  대상 파일" 감각 밖에 있어 놓치기 쉽다.
- 규칙: 공개 함수의 시그니처(특히 기본값 없는 인자 추가)를 바꾸면 커밋 전에 반드시
  `grep -rn "함수이름(" --include='*.py' .`로 저장소 전체(app/·tests/·evals/·scripts/ 전부)를
  훑어 호출부를 센 뒤, 그 개수만큼 고쳤는지 확인한다. `docs/lessons.md` 2026-08-10 「전체
  pytest」 항목과 같은 교훈이지만 이번엔 "왜 전체를 돌려야 하는가"의 구체 사례 — 표적 테스트
  통과는 "내가 아는 호출부는 안 깨졌다"만 증명하지 "모든 호출부"를 증명하지 않는다.
- 관련: #571, `evals/intent_probe/runner.py::_resolve_screen`,
  `evals/combo_matrix/runner.py::_warm_up_last_reco`(같은 PR에서 발견한 두 번째 회귀 —
  `resolve_screen_reference`의 새 게이트가 웜업이 실은 다건 카드를 근칭 지시대명사의 "후보
  다건 → 되물음" 규칙에 걸리게 해 `test_combo_matrix_eval.py` combo-0004·0059 가 깨졌다).

## [2026-08-10] `uv run ruff format`(경로 없이)은 저장소 전체를 재포맷한다 — 바꾼 파일만 지정할 것
- 증상: #434 라운드1 마무리에 `uv run ruff check --fix && uv run ruff format`(경로 없음)을
  돌렸더니 32개 파일이 재포맷됐다 — 내가 손댄 건 8개뿐이었는데 `.github/scripts/`·
  `data-analysis/`·`evals/`·`docs/research/` 등 전혀 무관한 파일까지 낡은 포맷 드리프트가
  전부 정리돼 `git status`가 40개 파일 변경으로 부풀었다.
- 원인: `ruff format`을 인자 없이 부르면 **프로젝트 전체**를 대상으로 잡는다. CLAUDE.md 의
  커밋 워크플로 문구(`uv run ruff check --fix && uv run ruff format`)는 "커밋 전 린트
  정리"라는 일반 규칙이라 경로를 명시하지 않는데, 이번처럼 다른 레인들이 아직 커밋하지 않은
  포맷 드리프트가 저장소 여기저기 쌓여 있으면 그 명령이 전부 건드려 diff 를 오염시킨다.
- 규칙: 작업 중간 점검이든 최종 검증이든 `ruff format`/`ruff check`는 **내가 이번 작업에서
  실제로 바꾼 파일 경로를 지정해서** 돌린다(`uv run ruff format <path> <path> ...`). 돌린
  뒤에는 `git status --porcelain`으로 의도한 파일 목록과 실제 변경 목록이 일치하는지 반드시
  대조하고, 무관한 파일이 섞였으면 `git checkout -- <path>`로 되돌린 뒤 다시 확인한다.
- 관련: #434 라운드1 최종 diff 정리(11개 파일로 스코프), `CLAUDE.md` 커밋 워크플로 2번 항목

## [2026-08-10] 계약 값 표현을 바꾸면 그 값을 검증하는 **다른 파일의 테스트**부터 grep 한다
- 증상: #434(칩 값당 분리, brand `value` 리스트→스칼라 정정)를 구현하며 `state.py`·
  `test_condition_actions.py`·`test_fanout.py` 3파일을 계획대로 갱신했는데, 전체 스위트를
  돌리자 `test_recommendation.py::test_general_reply_and_condition_chips_strip_unsafe_text`
  가 `chips[1]["value"] == ["정상 브랜드"]`(구 리스트 계약)로 실패했다 — 이 파일은 "정제
  (strip_unsafe)"를 검증하는 별도 관심사라 브랜드 칩 관련 파일 목록에서 빠져 있었다.
- 원인: 표적 파일 3개(패킷이 명시한 `build_condition_chips`·요청 스키마·회귀 테스트 파일)만
  갱신하고, "이 계약을 참조하는 모든 테스트"를 저장소 전체에서 grep 하지 않았다. 패킷이 짚어준
  파일 목록은 **주 관심사** 기준이지, 그 계약 값을 부차적으로 검증하는 다른 파일까지 보장하지
  않는다.
- 규칙: 응답 필드의 **표현(shape)**을 바꾸는 작업(리스트→스칼라, 조인→분리 등)은 편집 전에
  `grep -rn '"value"\] ==' tests/`처럼 그 필드의 assert 패턴으로 저장소 전체를 훑고, 편집 후
  전체 스위트(`uv run pytest`, 표적 파일만 아님)로 마무리 확인한다. "패킷이 지목한 파일"과
  "실제로 그 계약에 의존하는 파일"은 다를 수 있다.
- 관련: `tests/unit/test_recommendation.py::test_general_reply_and_condition_chips_strip_unsafe_text`
  · `app/agents/buyer/recommendation/state.py::build_condition_chips` · 이슈 #434

## [2026-08-11] 판정 라벨만 비교하는 회귀 테스트는 수치 드리프트를 통과시킨다
- 증상: #361 착수 중 개인화 평가 baseline 이 **다른 케이스 집합을 설명하고 있는 것**을 발견했다.
  `run_manifest.datasetHash` 가 `d16eb0e9…`(dev 96건)인데 현행 골든셋은 `675520d9…`(109건)였고,
  arm 별 nDCG@10 이 전부 움직여 있었다(clean 0.734 → 0.686, 주 비교 meanDelta 0.304 → 0.258).
  그런데 `uv run pytest` 는 계속 초록불이었다.
- 원인: 유일한 baseline 대조 테스트가 `overreach.json` 의 **verdict 문자열만**(`"regression"` /
  `"pass"`) 비교했다. 판정 라벨은 굵어서 케이스가 13건 늘고 점수가 5% 움직여도 값이 안 바뀐다.
  게다가 그 baseline 은 **더러운 워킹트리**(`dirty: true`)에서, 이 저장소 히스토리에 없는
  `commitSha` 로 생성돼 있어 무엇을 잰 것인지 재현조차 불가능했다.
- 규칙: **"baseline 을 회귀시키지 않는다"를 문서로 적었으면 그 수치를 비교하는 assert 를 같이
  넣는다.** verdict·pass/fail 같은 파생 라벨만 보는 테스트는 무회귀를 집행하지 못한다.
- 규칙(추가): **수치를 비교하기 전에 분모를 먼저 비교한다.** 케이스 수가 달라진 상태의 nDCG
  일치는 무회귀가 아니라 우연이다. `caseCount`·`ndcgCaseCount` 를 선행 assert 로 둔다.
- 규칙(추가): **평가 산출물은 깨끗한 워킹트리에서 생성한다.** `build_run_manifest` 가
  `git status --porcelain` 으로 `dirty` 를 박으므로, 더러운 상태로 커밋하면 그 baseline 이
  무엇을 잰 것인지 영원히 확정할 수 없다. 데이터셋을 바꾸는 PR 은 baseline 재생성까지가 범위다.
- 관련: `tests/eval/test_personalization_eval.py::test_default_weight_ndcg_matches_committed_baseline` ·
  `evals/personalization/baselines/dev-v2/README.md` · `evals/metrics/run_manifest.py:56,73` ·
  SPEC-PROFILE-GRAPH-149 REQ-PGRAPH-114 · 이슈 #361·#474·#333

## [2026-08-11] 필터를 무시하는 fake 로는 "후보가 줄지 않았다"를 증명할 수 없다
- 증상: #361 의 `test_avoids_does_not_shrink_the_candidate_set` 이 통과했는데, 회원에게만 필터를
  심는 변이를 넣어도 **여전히 통과**했다. 나머지 동등성 테스트 4건은 그 변이에서 정상적으로
  실패했다.
- 원인: 하네스가 쓰던 검색 대역(`tests/_fakes.FakeBackend` 계열)이 `filters` 를 받기만 하고
  **무시한 채 고정 상품 3건을 돌려준다.** 그래서 후보 집합이 무슨 필터에도 항상 3건이었고,
  "후보가 줄지 않았다"는 단언이 잴 대상 자체가 없었다.
- 규칙: **"X 가 결과를 좁히지 않는다"를 재려면 대역이 X 를 실제로 적용해야 한다.** 좁힘을 재는
  테스트에서 대역이 입력을 무시하면, 그 테스트는 성립하는 것이 아니라 **아무것도 재지 않는다**.
- 규칙(추가): **회귀 테스트는 변이를 심어 실패하는 것을 보고 나서 커밋한다.** 초록불만으로는
  "지키고 있다"와 "안 보고 있다"가 구분되지 않는다 — 이 결함도 통과 상태에서는 안 드러났고
  변이 주입으로만 드러났다.
- 관련: `tests/unit/test_profile_graph_filter_isolation.py::_RecordingSearch` ·
  `tests/_graph_fixtures.py`(픽스처 값이 카탈로그와 교차해야 하는 이유도 같은 종류) · 이슈 #361

## [2026-08-11] 읽기에서 숨긴 것을 쓰기에서 안 숨기면, 상태 코드가 오라클이 된다
- 증상: I-32(GET)는 `is_projected`(`active` + 민감 파생 제외)로 걸러 `superseded`·민감 파생 edge 를
  절대 안 보여주는데, I-33/I-34(PATCH/DELETE)의 대상 판정은 **`edge_id` 일치만** 봤다. PR 자동
  리뷰가 잡았다.
- 원인: **노출 경계를 읽기 경로에만 적었다.** 쓰기 경로는 "그 id 가 문서에 있나"만 물었고, 그
  질문이 읽기 경로의 답과 다르다는 것을 아무도 확인하지 않았다.
- 왜 위험한가 둘:
  1. **존재 오라클** — `edge_id` 가 `sha256("{predicate}|{node_id}")` 라 **사용자별 salt 가 없는
     콘텐츠 해시**다. 라벨만 알면 계산되므로, 숨긴 edge 에 변경을 쏴 `200`/`404` 차이로 *"이
     취향이 추론된 적 있나"* 를 알아낼 수 있다. 민감 파생은 **존재 자체를 노출하지 않아야**
     한다(REQ-PGRAPH-076 [HARD]) — 필드를 빼도 상태 코드가 남으면 안 뺀 것이다.
  2. **불변식 우회** — `superseded`(병합 엔진이 상충에서 내린 패자)를 수정하면 `_pin` 이 무조건
     `active` 로 되돌려, 같은 노드에 상충하는 active edge 가 둘 생긴다. 배치의 `_resolve_conflicts`
     를 전혀 거치지 않는다.
- 규칙: **읽기에서 감춘 것은 쓰기에서도 "없는 것"이어야 한다.** 노출 술어를 만들었으면 그것을
  **대상 판정에도 같이 쓴다** — 두 경로가 다른 질문을 하면 상태 코드·지연·오류 문구 중 하나가
  반드시 그 차이를 새어 보낸다.
- 규칙(추가): **식별자가 내용 파생이면 "모르니까 안전하다"가 성립하지 않는다.** 결정론적
  `edge_id` 는 tombstone 이 재파생을 막기 위한 기능 요구사항이라 포기할 수 없다 — 그러면 그
  식별자를 **추측 가능한 공개 값**으로 보고 접근 판정을 따로 세워야 한다.
- 규칙(추가): **같은 이름의 조회 헬퍼라도 용도가 다르면 필터를 같이 걸면 안 된다.** 리뷰는
  `graph_mutations._find` 전부에 필터를 제안했는데, 그중 하나는 **병합 대상 조회**
  (`apply_correction` 의 `target = _find(document, new_id)`)라 숨겨진 edge 도 봐야 한다 — 거기서
  못 찾으면 관측 근거를 잃고 `merged` 가 거짓이 된다. 경계는 **진입점 한 곳**에 둔다.
- 관련: `app/agents/profile/graph_journal.py::_find_edge`·`apply_edge_mutation` ·
  `tests/unit/test_profile_graph_apply.py` · api-spec §3.8 · PR #562 리뷰

## [2026-08-11] 멱등 파생 키에 "무엇을 하려는지"가 없으면 되돌리는 요청이 원래 요청을 재생한다
- 증상: I-37 로 개인화를 끄고 **같은 `If-Match` 로 다시 켜면** 끄기 응답이 재생돼 플래그가
  `false` 로 남았다. 원장 TTL(`graph_idempotency_ttl_h`, 기본 24시간) 동안 사용자가 프라이버시
  스위치를 다시 켤 수 없다. 정본 I-37 은 정반대를 요구한다 — *"토글은 마지막 의사가 이긴다."*
- 원인: 파생 키가 `{action}:{userId}:{scopeId}:{ifMatch}` 인데 토글의 `scopeId` 가 **빈 값**이었다.
  이 경로는 그래프 문서를 건드리지 않아 **`graphVersion` 이 고정**이라, 끄기와 켜기가 **정상적으로
  같은 선행조건**을 지참한다. 두 요청이 같은 키가 되는 것이 예외가 아니라 **정상 동작**이다.
- 규칙: **멱등 키를 설계할 때 "그 요청이 무엇을 하려는지"가 키에 들어가는지 확인한다.**
  특히 **선행조건이 변하지 않는 경로**(버전을 안 올리는 플래그 토글류)는 키에 대상 상태를 넣지
  않으면 **역방향 요청이 정방향 응답을 재생**한다. 상태를 바꾸는데 버전이 안 오르는 엔드포인트를
  보면 이 질문을 먼저 한다.
- 규칙(추가): **본문 지문(`request_fp`)으로 막는 것은 해법이 아니다** — 같은 키·다른 본문이
  `LedgerRequestMismatch` → `409` 로 떨어져 "끄기가 실패하는 상황"을 만든다. 두 요청이 **다른
  일을 하는 것이면 키를 갈라야지 충돌로 막으면 안 된다.**
- 규칙(추가): **선택 파라미터는 "주면 어떻게 되나"를 성공 경로에서도 잰다.** 기존 테스트 8건이
  이 버그를 못 잡은 이유는 `If-Match` 를 주는 케이스가 **CAS 실패 시나리오 하나뿐**이라 원장 키가
  아예 만들어지지 않았기 때문이다. 선택 파라미터의 테스트가 실패 경로에만 있으면, 그 파라미터가
  여는 코드 경로는 통째로 미검증이다.
- 관련: `app/agents/profile/graph_journal.py::set_personalization`·`derived_key` ·
  `tests/unit/test_profile_personalization.py` · api-spec §3.9.5 · #360

## [2026-08-11] 드리프트를 발견하고도 소수 쪽을 골랐다 — "더 자세히 적힌 쪽"이 정본은 아니다
- 증상: 투영 조건을 `SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-021 의 `active` + `promoted` 로 정했다.
  그 결정을 근거로 "화면 < 추천 갭"을 진단하고 **안 B**(요약을 `promoted` 로 좁힘)를 채택해
  `builder._summary_input` 수정 + `evals/personalization` 실측이 계획에 들어갔다. **전부
  불필요했다** — 정본 I-32 는 `active` 만이고 *"요약 생성이 같은 규칙을 쓴다"* 로 화면=추천을
  이미 요구하고 있었다.
- 원인: 세 문서(노션 정본 I-32 · api-spec §3.8 · M-11)에 그 조건이 **없다는 것을 실측으로 확인해
  놓고도**, 유일하게 조건을 적은 SPEC 을 따랐다. *"더 구체적으로 적힌 쪽이 더 정확할 것"* 이라는
  잘못된 가중치다. 실제로는 **한 문서에만 있는 조건은 드리프트일 확률이 높다.**
- 규칙: **드리프트를 발견하면 "어느 쪽이 더 자세한가"가 아니라 "어느 쪽이 정본인가"로 정한다.**
  조건이 정본에 없고 사본에만 있으면 **사본이 틀린 것**이다. 반대 방향(정본에 있고 사본에 없음)
  일 때만 사본을 채운다. `docs/` 는 전부 사본이고 정본은 노션이다 — SPEC 처럼 "내부 모델을
  소유"하는 문서도 **와이어 조건을 인용하는 순간 사본**이다.
- 규칙(추가): **의심스러운 필드는 소비처를 grep 한다.** `promoted` 를 필터로 읽는 코드가 0건
  (전부 `graph_merge` 안쪽의 히스테리시스 계산)이라는 사실이 문서 대조보다 먼저 답을 줄 수
  있었다 — 계약이 정말 그 필드로 무언가를 가른다면 **읽는 코드가 있어야 한다.**
- 관련: `SPEC-PROFILE-GRAPH-149` REQ-PGRAPH-021 · `docs/api-spec.md` §3.8 ·
  `app/agents/profile/graph_merge.py` · #360 코멘트(2026-08-10)

---

## [2026-08-10] baseline 계보를 timestamp 로 고르면 대조팔을 출고 수치로 인용한다
- 증상: #139 문서 초안이 `intent_probe` 최신 baseline 을 `run.timestamp` 로 골라
  `fast-2026-08-07-430-v6-merged-2`(2026-08-07T14:26:32Z)를 발표 인용 대상으로 삼았다. 그런데
  출고판은 `…-v6-adopted-1`(13:28:00Z)·`…-v6-adopted-2`(13:46:06Z)로 **더 이른 시각**이었다.
  수치도 다르다(`mainIntent` merged-2 238/240=0.9917 vs adopted-2 235/240=0.9792).
- 원인: 같은 fixture(`intent-probe-anchors-b-v6`)·모델·앵커·N 으로 **두 팔을 나란히 돌린 대조
  실험**이라 최신 timestamp 가 출고판을 뜻하지 않는다. `evals/intent_probe/README.md`의 "기준선"
  절은 `merged-*`가 `#386`(PR #441) 병합 직후 판, `adopted-*`가 **출고판**이며 `_SYSTEM`이
  10자만 다르다고 적어 두었지만, 초안은 디렉터리 목록의 시각만 보고 골랐다.
- 규칙: **baseline 을 "최신 timestamp"로 고르지 마라. 하네스 README 에서 어느 계보가 출고판인지
  먼저 확인하라.** 발표·리포트가 인용하는 수치는 출고판 계보여야 하고, 대조팔을 출고 수치로
  쓰지 않는다. baseline 지정표를 만든다면 `계보(출고판/대조팔)` 열을 둔다.
- 관련: `evals/intent_probe/README.md`(기준선 절) · `docs/specs/RELEASE-CLAIMS-139.md` §4
  "최신 timestamp ≠ 출고판" 규칙·§2 C2 · 이슈 #139·#430·#386

## [2026-08-10] before/after 를 한 파일에 담은 baseline 은 arm 이 어느 시점 동작인지부터 확인한다
- 증상: #139 문서 초안이 `evals/personalization/baselines/live-v1` 의 `pairedVsGuest.clean_both`
  nDCG@10 meanDelta **−0.2879316720443962**(CI95 [−0.4813819808647765, −0.11845955082203671],
  0 을 배제)을 보고 "라이브에서 개인화가 품질을 **떨어뜨린다**"고 판단해, 개인화 관련 주장을
  "반증됨"으로 분류할 뻔했다.
- 원인: 그 baseline 은 **#119 수정 전후를 한 실행 안에 담은 회귀 자료**다. `clean_both`는
  프로필이 decompose 하드 필터로 새던 **수정 전** 동작이고, 출고 설정은 `clean_rerank_only`
  (`profile_injection_scope` 기본값)다. 출고 arm 수치는 meanDelta **−0.05644463392740816**로
  CI95 **[−0.2021440745401869, 0.04957802400457049]** — **0 을 포함**(inconclusive)하고,
  필터 유출(`axisLeakage`)은 29/31 → 1/31 로 줄어 있다. arm 이름을 동작 시점과 연결하지 않고
  "가장 나쁜 숫자"를 대표값으로 읽은 것이 원인이다.
- 규칙: **arm 이 여러 개인 baseline 은 각 arm 이 어느 시점·어느 설정의 동작인지부터 확정하고
  인용하라.** `CHANGELOG`나 하네스 README 에 그 대응이 적혀 있는 경우가 많다. 특히 arm 이름에
  `both`/`only`처럼 설정 스코프가 들어 있으면 현행 기본값이 무엇인지 `config`에서 확인한다.
- 규칙(추가): **부호가 강한 수치를 근거로 주장을 뒤집기 전에 교차검증을 붙여라.** 이 건은
  읽기 전용 검증자를 별도로 붙여 "반박해 보라"고 시켜서 잡았다. 혼자 읽고 결론냈으면 발표
  자료에 정반대 주장이 들어갔을 것이다.
- 관련: `evals/personalization/baselines/live-v1/comparison.json` · `CHANGELOG.md` #147 항목 ·
  `docs/specs/RELEASE-CLAIMS-139.md` §2 C3·§3-2 · 이슈 #139·#119·#147

---

## [2026-08-10] 검증되지 않은 값을 "실측"이라고 적으면 다음 사람이 그것을 근거로 삼는다
- 증상: #134 조사 중, `deploy.yml` 이 `TRUST_FORWARDED_FOR=true`·`FORWARDED_FOR_TRUSTED_HOPS=2`
  를 무조건 주입하는 근거가 커밋 `44d74cef`(2026-08-06)의 "2026-08-06 실측: AI 는 nginx 를
  거치지 않아 XFF 가 2개"라는 메시지뿐임을 발견했다. 그런데 AI 서버는 그 시점까지 XFF 를
  로그로 남긴 적이 **한 번도 없었다** — "실측"이라 적힌 값은 실제로는 관측 경로가 없는
  상태에서의 추정이었다. 운영은 2026-08-06 이후 4일간 근거 없는 홉 수를 신뢰해 왔고, 틀렸다면
  IP 백스톱이 위조로 조용히 우회되는 상태였는데 아무도 몰랐다.
- 원인: 커밋 메시지·주석의 "실측"이라는 단어는 다음 사람(운영자·리뷰어·다음 이슈 담당자)에게
  "이미 검증됐으니 재검증 불필요"라는 신호를 준다. 그 단어를 적은 시점에 실제 관측 로그가
  없었다면, 그 신호는 거짓이고 다음 사람은 거짓 근거 위에 결정을 쌓는다. 검증되지 않은 값에
  확정적 어휘("실측", "확인됨")를 쓰는 것 자체가 관측 없이 신뢰를 만들어내는 행위다.
- 규칙: 값을 관측 없이 추정했다면 "추정"·"가정"이라고 적고, 그 값을 검증할 관측 경로가
  없다면 **그 관측 경로부터 만든다**(값을 하나 더 얹지 않는다). 확정적 어휘("실측"·"검증됨"·
  "확인함")는 실제로 관측 로그·테스트·계측값을 가리킬 때만 쓴다. 리뷰에서 이런 단어를 보면
  "무엇으로 관측했나"를 먼저 묻는다.
- 관련: `app/core/client_ip.py`(`client_ip_probe` 진단 로그 — `cfMatchIndexFromRight` 로
  실제 홉 위치를 관측), `.github/workflows/deploy.yml`(주석 정정), 이슈 #134, 커밋 `44d74cef`.

---

## [2026-08-10] `make_interval(days => %s)` 는 실수(float) 보존기간 설정과 못 섞는다
- 증상: 전사록 보존 스윕(#321) SQL 이 통합 테스트에서 `psycopg.errors.UndefinedFunction:
  function make_interval(days => double precision) does not exist` 로 죽었다.
- 원인: `conversation_retention_days` 는 (다른 보존기간 설정들과의 일관성 때문에) `float`
  이다. Postgres `make_interval` 의 `days` 인자는 **정수(int)** 전용 오버로드만 있어 float
  값을 그대로 바인딩하면 매칭되는 함수가 없다. `graph_journal.py` 가 쓰는
  `make_interval(secs => %s)` 는 `secs` 가 `double precision` 이라 같은 함수를 그대로
  베껴 쓰면 안전해 보이지만, `days`/`hours`/`mins` 인자는 시그니처가 다르다.
  실 pg-profile 없이 `_Connection` fake 로만 돌리는 유닛 테스트는 SQL 문자열만 보고
  실행하지 않아 이 클래스의 버그를 못 잡는다 — 실 Postgres 를 치는 통합 테스트가 잡았다.
- 규칙: `make_interval` 을 새로 쓸 때는 **인자별 타입을 먼저 확인**한다(`secs` 만
  `double precision`, 나머지는 `int`). float 보존기간/윈도우를 초 단위가 아닌 다른 단위로
  넘겨야 하면 `make_interval` 대신 `interval '1 day' * %s`(interval-스칼라 곱, float 그대로
  받는다) 를 쓴다. SQL 리터럴을 바꾸는 변경은 fake 커넥션 유닛 테스트만으로 "통과"라고
  보고하지 말고 실 DB 를 치는 integration 테스트로 최소 1회 실행해 확인할 것.
- 관련: `app/core/conversation.py::PgConversationStore.purge_expired_turns`,
  `tests/integration/test_pg_conversation_store.py`

## [2026-08-10] 새 후처리가 **다른 하네스의 설계 전제**와 충돌해 43건이 깨졌다 — 전제를 읽고 그 하네스에서만 꺼라
- 증상: #443 사전 기반 leg 보강(발화에서 카탈로그 카테고리명을 찾아 빈 `categoryQueries` 를
  채운다)을 넣자 `.env` 없는 전체 스위트가 **43건** 깨졌다. 표적 테스트와 `ruff` 는 통과했고
  워커 보고도 "지정 범위 통과"였다 — 전체 스위트를 직접 돌려서야 드러났다.
- 원인은 셋이었고 성격이 전부 달랐다.
  1. `Sample` dataclass 에 **기본값 없는** 필드를 더해 기존 생성자 호출 34곳이 깨졌다(그리고
     기본값을 주자 이번엔 "non-default argument follows default argument" 로 순서가 걸렸다 —
     진단 필드는 dataclass **끝**에 기본값과 함께 둔다).
  2. `RouteDecision` 의 **드리프트 가드**(`test_route_decision_axes_are_all_classified`, 두 파일)가
     "새 필드를 판정 축으로 분류하라"고 강제했다. 가드가 제 일을 한 것이라 **약화시키지 말고
     분류**해야 한다 — 진단 플래그는 `no_effect` 이고, 보강의 실제 효과는 `category_queries`
     (이미 blocking)가 계상한다는 사실을 주석으로 남겼다.
  3. **다른 하네스의 설계 전제와 충돌**했다. `evals/combo_matrix` 는 "decompose 산출을 **고정
     주입**해 축을 통제한다"가 설계 전제인데, 이 보강은 산출이 아니라 **발화**를 읽는다.
     발화 생성기가 `case=1` 을 항상 `"무선 이어폰"` 으로 realize 하고 `이어폰` 이 카탈로그 사전에
     있어서, 켜 두면 `category=absent` 로 통제한 축이 발화 쪽에서 되살아나 **축이 뜻을 잃는다**
     (실측: combo-0062 가 `zero_result` → `stop`·상품 3건).
- 규칙: **발화를 읽는 후처리를 넣을 때는 "산출을 고정 주입하는" 하네스를 먼저 찾아 전제 충돌을
  확인하라.** 충돌하면 그 하네스에서만 끄고(전역 기본값은 유지) 왜 끄는지를 그 자리에 적는다 —
  골든을 재생성해 통과시키면 축이 죽은 사실이 숨는다. 실제로 이번에도 `refresh-observed` 로
  골든을 갱신하려다 멈췄고, 하네스에서 끄자 골든은 **원본 그대로가 정답**이 됐다.
- 곁가지 하나: 그 `refresh-observed` 는 무관한 2행(`combo-0005`·`0010`)의 장바구니 응답까지
  바꾸려 했다 — 워크트리 `.env` 가 로컬 BE 를 치면서 생긴 **환경 의존 값**이다. 골든 재생성 diff 는
  "내 변경으로 설명되는 행만" 남기고 나머지는 되돌려야 한다.
- 관련: #443 · #465 · `evals/combo_matrix/runner.py`(비활성 근거 주석) ·
  `tests/unit/test_fanout.py`(#428 구제 경로의 남은 정의역)

## [2026-08-10] 초록불이 "잰다"는 뜻은 아니다 — 한 이슈에서 공허한 테스트를 세 번 만들었다
- 증상: #359 작업 중 **새로 쓴 테스트가 통과했는데 아무것도 안 재는** 경우가 세 번 나왔다.
  전부 변이 검증(고친 코드를 되돌려 보기)으로만 드러났고, 코드 리뷰나 커버리지로는 안 보였다.
  1. **심층 방어가 국소 수정을 가렸다.** `_reassert_pins` 터미널 게이트를 넣자
     `_merge_edge`·`_carried_tombstones`·`_resolve_conflicts` 의 pin 분기를 **통째로 지워도
     61건이 전부 통과**했다 — 결과만 재니 게이트가 뒤에서 고쳐 놓는다.
  2. **테스트 더블을 두 번 설치하며 앞의 것을 감쌌다.** `stream_recommendation` 스파이를
     `buyer_graph.stream_recommendation` 을 그때그때 읽어 설치했더니 두 번째 스파이가 첫 번째를
     래핑해, **두 번째 실행이 첫 스파이의 기록을 덮어썼다.** 소비 게이트를 지워도 4건 통과.
  3. **테스트 환경이 검증하려던 분기를 안 탔다.** `set_summary` 의 `usable` 리셋 구멍은
     "임베딩 성공" 경로에만 있는데, 유닛 환경은 API 키가 없어 `_embed_summary` 가 늘 `None` 을
     돌려줘 다른 분기로 샜다.
- 원인: 셋 다 **"어서션이 참이 되는 경로가 하나뿐인가"를 안 물었다.** 통과 사실만 확인하고
  *무엇 때문에* 통과했는지를 확인하지 않았다.
- 규칙:
  - **방어를 여러 겹 쌓으면 겹마다 따로 잰다.** 결과 단언만 두면 바깥 겹이 안쪽을 가린다.
    이번 해법은 **"정상 경로에서 게이트는 아무것도 바꾸지 않는다"** 를 불변식으로 세우고
    게이트 발화 로그(`profile_graph_pin_reasserted`)의 **부재**를 함께 단언한 것이다 — 그러면
    안쪽 겹이 깨지는 순간 게이트가 울고 테스트가 잡는다.
  - **테스트 더블은 생산 함수를 모듈 로드 시점에 붙잡아 쓴다.** 현재 바인딩을 읽어 감싸면
    두 번 설치했을 때 체인이 생긴다.
  - **"이 테스트가 재려는 분기에 실제로 들어갔는가"를 단언 하나로 고정한다.** ①은 게이트 로그,
    ③은 `_embed_summary` 를 성공으로 patch, C3 의 superseded 더미 테스트는 "더미가 실제로
    `superseded` 로 남았는지"를 본 단언 **앞에** 뒀다(승자를 안 줬더니
    `_revive_orphan_superseded` 가 전부 active 로 되살려 시나리오가 성립하지 않았다).
  - 요약하면 lessons 2026-08-10 「되돌리면 깨지는지 실제로 해 본다」의 확장이다 — **새 방어를
    넣은 커밋에서는 그 방어를 되돌려 보는 것이 기본 절차**다.
- 관련: `app/agents/profile/graph_merge.py::_reassert_pins`,
  `tests/unit/test_profile_graph_merge.py::_build_pin_safe`,
  `tests/unit/test_personalization_optout_buyer_turn.py::_PRODUCTION_STREAM`, 이슈 #359

---

## [2026-08-10] 이중 방어의 2차만 구현하면 1차가 막던 구멍이 그대로 열린다
- 증상: 개인화 중지 소비 차단을 **요약 항목의 `usable` 표식만** 보고 하려 했다. 왕복이 0회라
  매력적이었는데(그 값은 이미 읽어 온 필드), 계획 검증에서 세 구멍이 드러났다.
  (a) 요약 행이 없으면 `mark_summary_usable` 이 **조용히 no-op** 이라 표식을 내릴 자리가 없다 —
  프로필이 아직 없는 회원이 먼저 끄는 흔한 경우다. 그 뒤 배치가 요약을 만들면 `usable=True` 로
  태어나 개인화가 되살아난다. (b) 플래그 upsert 성공 후 표식 쓰기에서 실패하는 창이 있다.
  (c) `set_summary` 의 승계가 조건부라 임베딩 성공 + CAS 미사용 호출에서 `True` 로 리셋됐다.
- 원인: 명세(REQ-PGRAPH-100)가 *"중지 플래그는 기본 캐시를 두지 않으며(즉시성 약속),
  **이중 방어로** 요약 항목의 사용 가능 표식도 함께 내려 stale read 가 안전하게 열화하도록 한다"*
  라고 적은 것을 **"둘 중 하나를 고르라"** 로 읽었다. 문장 구조는 1차(플래그)와 2차(표식)를
  나눠 놓았고, 2차는 1차의 stale read 를 받아 주는 그물이지 대체재가 아니다.
- 규칙: **"이중 방어" 는 층 이름이지 선택지가 아니다.** 두 겹 중 하나만 구현하려 할 때는
  *다른 겹이 막던 경우*를 명시적으로 열거해 본다 — 여기서는 "표식을 내릴 자리가 없는 상태"가
  1차 없이는 안 닫혔다. 그리고 **한 겹이 다른 겹의 쓰기 성공에 의존하면 그건 한 겹이다.**
- 규칙(추가): `(c)` 같은 조건부 승계는 **호출자 한 명의 규율에 [HARD] 보장이 걸린 상태**였다
  (`consolidate` 가 늘 `expected_seq=` 를 넘겨서 안전했다). 불변식을 지키는 코드가 "지금 호출자가
  마침 그렇게 부르니까" 성립한다면 그건 불변식이 아니다.
- 관련: `app/agents/profile/reader.py`, `app/agents/profile/store.py::set_summary`,
  `app/agents/profile/personalization_gate.py`, REQ-PGRAPH-100, 이슈 #359

---

## [2026-08-10] 시계를 멈추려면 시계가 아니라 **시계를 읽는 함수**를 봐야 한다
- 증상: REQ-PGRAPH-055(중지 기간 감쇠 정지)의 초안 설계가 *"재개 시 모든 edge 의
  `decay_evaluated_at` 을 `now` 로 당긴다"* 였다. 그럴듯했지만 **관측이 남아 있는 edge 에는
  효과가 0**이다 — `_confidence` 는 그 필드를 **읽지 않고** 관측 목록에서 매 배치 전량 재계산한다.
  그 필드를 읽는 곳은 근거가 사라진 edge 를 이월하는 `_carried_tombstones` 하나뿐이었다.
- 원인: 필드 **이름**(`decay_evaluated_at` = "감쇠를 언제 기준으로 쟀나")이 "감쇠의 단일 출처"처럼
  읽혀서, 그 값을 고치면 감쇠가 바뀐다고 가정했다. 소비 지점을 grep 하지 않았다.
- 규칙: **상태를 고쳐 동작을 바꾸려 할 때는 그 상태를 읽는 코드를 먼저 전수로 찾는다.**
  쓰는 곳이 아니라 **읽는 곳**이 동작을 정한다. 이름이 그럴듯할수록 확인을 건너뛰기 쉽다 —
  lessons 2026-08-04 「튜너블을 근거로 판단하기 전에 그 값이 실제로 소비되는지 grep 한다」와
  같은 계열이고, 이번엔 튜너블이 아니라 **저장 필드**에서 같은 함정을 밟았다.
- 규칙(추가): 같은 검토에서 초안이 **교착·계약 위반**까지 함께 안고 있던 것이 드러났다 —
  재개 경로에서 그래프 락을 잡으면 기존 테스트(`test_the_toggle_never_holds_the_graph_lock`)가
  hang 하고, 문서를 고치면 §3.9.5 응답의 `graphVersion`·감사·원장이 거짓이 된다("그래프 문서는
  안 바뀐다"를 전제로 같은 값을 싣는 코드가 있다). **"어디에 저장할까"는 성능이 아니라 계약
  문제일 수 있다** — 최종안은 구간을 플래그 테이블(락 없는 단일 행)에 두는 것이었다.
- 관련: `app/agents/profile/graph_merge.py::_confidence`·`_elapsed_days`,
  `app/agents/profile/graph_journal.py::get_personalization_state`, REQ-PGRAPH-055, 이슈 #359

---

## [2026-08-10] fail-closed 는 경로마다 잃는 것이 달라서, 한 정책으로 통일하면 데이터를 지운다
- 증상: 개인화 중지 플래그 조회가 실패했을 때 "프라이버시 스위치니까 전 구간 fail-closed"로
  통일하려 했다. 그러면 배치 경로에서 `generate_session_delta` 가 `([], watermark)`("처리됨")를
  돌려주고, `finalizer` 가 그 뒤 `clear_session_ctx_upto` 까지 진행한다 —
  **DB 블립 한 번에 개인화가 켜져 있는 사용자의 누적 세션 버퍼가 영구 삭제**된다.
- 원인: "안전한 쪽"을 **한 축**(개인화를 덜 한다)으로만 봤다. 실제로는 경로마다 잃는 것이 다르다:
  hot-path 쓰기는 발화 1건, 소비는 그 턴의 개인화, **배치는 세션 전체의 원문**이다.
- 규칙: **실패 정책은 호출부가 정하게 한다.** 게이트 함수에 `on_error` 를 받아
  `True`/`False`/`None`(판정 불가) 3상태를 돌려주고, 각 지점이 자기가 잃는 것에 맞춰 고른다.
  기존 계약에 이미 "판정 불가" 어휘가 있으면 그것을 재사용한다 — 여기서는
  `generate_session_delta` 의 `None`(= degrade·버퍼 보존·RETRYABLE)이 그 자리였다.
- 규칙(추가): 같은 이유로 `consolidate` 는 **fail-open** 이다. 거기서 중지로 접으면 pg 블립이
  지속되는 동안 켜진 사용자의 프로필이 영영 갱신되지 않는데 **로그 말고는 드러날 신호가 없다.**
  "조용히 아무것도 안 하는" 실패는 시끄럽게 실패하는 것보다 대개 나쁘다.
- 규칙(추가): 홈 추천에서는 판정 불가를 `NO_PROFILE` 로 접으면 api-spec §3.7 「HOME 실패 모드」의
  `profile_unavailable`(200·프로필 항만 빠짐·남은 근거로 판정)과 충돌한다 — **fail-closed 를
  "전부 차단"으로 번역하기 전에 그 표면의 degrade 계약을 읽는다.**
- 관련: `app/agents/profile/personalization_gate.py`, `app/agents/profile/builder.py`,
  `app/services/home_recommendation.py`, REQ-PGRAPH-052/053, 이슈 #359

---
## [2026-08-10] `script: |` 블록 안의 `#` 는 주석이 아니다 — 설명문에 쓴 빈 Actions 표현식이 운영 배포를 전면 중단시켰다
- 증상: 승격(#552) 머지 후 배포가 실행되지 않았다. 그 직전 dev push(#551)에서도 같은 실패.
  두 run 모두 **job 0개**로 실패했고 `gh run view --log`·`--log-failed` 가 빈 출력이었다
  (`/jobs` API `total_count=0`) — 즉 job 이 시작조차 못 한 **startup failure** 다.
- 원인: `deploy.yml` 의 `script: |` 은 YAML **블록 스칼라**라 `#` 가 주석이 아니라 리터럴
  텍스트이고, GitHub Actions 는 그 안의 `달러+이중중괄호` 표현식도 **평가한다**. PR #539 가
  그 블록에 넣은 설명문에 **내용이 빈 표현식**이 있었고, 빈 표현식은 문법 오류라 워크플로
  파싱 단계에서 죽었다. 역설적으로 "표현식을 셸 문장에 스플라이스하지 마라"고 경고하는
  주석 자체가 워크플로를 깨뜨렸다.
- 왜 CI 가 못 잡았나: 로컬 `yaml.safe_load` 는 **통과한다**(YAML 문법은 유효, GitHub 표현식
  검증에서만 걸린다). 워크플로 변경은 그 워크플로가 실제로 도는 것 외에 검증 수단이 없다.
- 규칙:
  - **블록 스칼라(`run: |`·`script: |`) 안에는 Actions 표현식 리터럴을 설명 목적으로 쓰지
    않는다.** 표현식을 서술해야 하면 "달러+이중중괄호" 처럼 풀어 쓴다.
  - 워크플로를 고쳤으면 **job 이 시작됐는지**(`/jobs` `total_count>0`)를 성공/실패와 별개로
    확인한다. 0 이면 파일 자체가 거부된 것이라 로그를 찾아봐야 소용없다.
  - **트리거 브랜치가 아닌 곳의 push 에서도 run 이 생성되면 startup failure 를 의심한다** —
    브랜치 필터를 적용하기 전에 실패했다는 신호다(이번 진단의 결정적 단서).
- 관련: 이슈 #553, PR #539(원인), `.github/workflows/deploy.yml`

## [2026-08-10] LLM 이 "안 뽑는" 결함은 프롬프트에 **그 규칙이 있는지부터** 본다
- 증상: #430 이 드러낸 과소지정 오탐의 근원을 파보니, `decompose` 가 브랜드-only 발화에서
  `filters.brand` 를 60표본 중 17~19건만 채우고 있었다. 원인 가설을 모델 능력·발화 난이도 쪽으로
  세우기 쉬웠는데, 실제로는 `_SYSTEM` 에 **브랜드 추출 규칙이 한 줄도 없었다** — 색상에는
  전용 규칙("색상 조건이 있으면 filters.color")이 있었고 브랜드는 스키마 키와 멀티턴 리파인
  맥락에만 등장했다. 절 하나를 넣자 42~45/60 이 됐다.
- 원인: 스키마에 필드가 있으면 "지시했다"고 착각한다. JSON 스키마의 키는 **출력 형식**이지
  **추출 지시**가 아니다. 형제 축(색상)에 있는 규칙이 이 축에는 없다는 비대칭을 아무도 안 봤다.
- 규칙: 추출 결함을 만나면 **모델을 의심하기 전에 프롬프트에서 그 축의 규칙을 grep** 하라.
  형제 축과 나란히 놓고 "쟤는 있는데 얘는 없는" 비대칭부터 찾는다. 없으면 그게 원인이다.
- 규칙(추가): **하드필터의 값 표기는 매칭 방식과 함께 봐야 한다.** I-1 `brandName` 은 exact IN
  이라 "애플"→`["Apple"]` 번안이 조용히 빗나간다(애플 발화에서 추출된 8표본이 전부 그랬다). `color` 는 부분
  일치라 같은 실수가 안 드러난다 — **축마다 BE 매칭 규약을 확인하고 프롬프트에 "원문 표기
  그대로"를 명시**하라. 그리고 그 축이 자동 완화 대상인지 함께 확인할 것: 브랜드는
  `relaxation_auto_fields` 허용 목록(`ratingMin` 뿐)에 없어 **빗나가도 자동으로 복구되지
  않는다** — 사용자가 완화 칩을 눌러야 한다.
- 관련: `app/agents/buyer/recommendation/decompose.py::_SYSTEM`(브랜드 절) ·
  `app/pipelines/brand_aliases.py` · `evals/filter_axes/brand_probe.py` · 이슈 #466·#430

## [2026-08-10] 실 LLM 하네스를 커밋할 때 **백오프 없이 커밋하면 그 하네스는 못 쓴다**
- 증상: #466 브랜드 프로브를 저장소에 커밋하고 전/후 4런을 돌렸는데 **네 런 모두 첫 429 에서
  통째로 죽었다.** 스크래치패드에서 쓰던 원본 하네스에는 백오프가 있었는데, 커밋본으로 옮기며
  그 부분을 빼먹었다.
- 원인: OpenAI TPM 은 **org 단위**라 동시에 도는 다른 레인과 공유된다. 이 저장소는 레인을
  여러 개 병렬로 돌리는 것이 상시 상태라 429 는 예외가 아니라 **정상 조건**이다.
- 규칙: 실 LLM 프로브를 커밋할 때 **429 지수 백오프를 하네스 안에 넣는다**(기존
  `underspecified_probe` 의 `GlobalPacer` 와 같은 취지). 그리고 429 **외의** 오류는 삼키지
  마라 — 조용한 표본 유실은 분모를 왜곡해 전/후 비교를 무효로 만든다.
- 규칙(추가): 커밋한 하네스는 **커밋한 그대로 한 번 돌려보고** 산출물을 확인한 뒤 보고하라.
  스크래치 버전이 돌았다는 것은 커밋본이 돈다는 증거가 아니다.
- 관련: `evals/filter_axes/brand_probe.py::_sample` · `tests/unit/test_brand_probe.py` · 이슈 #466

## [2026-08-10] 상태 표기 스윕은 사본(api-spec)만 고치고 끝내면 절반이다 — 코드 주석 사본까지 훑어야 한다
- 증상: #435 가 원인 추적에서 한 라운드를 버렸다("FE 수신부가 없어서인가"를 먼저 의심). 그래서
  #436 이 열려 api-spec §3.1·§4.14~4.16 을 고쳤지만 **코드 주석 사본은 손대지 않고 CLOSED** 됐고,
  `remove.py`·`config.py`·`spring_client.py`·`schemas/spring.py`·테스트 2개가 그 뒤로도
  "Spring 구현 진행 중"·"🔶 초안 — BE 협의 전"이라고 주장했다. `wishlist.py` 는 그 부채를
  주석으로 명시("#436 소유라 여기서 건드리지 않는다")했지만 #436 이 그 범위를 안 가져갔다.
- 원인: 상태 마커가 **여러 파일에 사본으로 흩어져** 있는데 스윕 범위를 한 파일로 잡았고,
  "다른 이슈 소유"로 미룬 항목이 그 이슈에서 실제로 처리됐는지 확인하는 고리가 없었다.
- 규칙: (1) 상태 마커를 고칠 땐 `grep` 으로 **같은 문구를 전 저장소에서** 찾아 한 번에 훑는다
  (`"진행 중"`·`"🔶"`·`"아직"`·`"미배포"`). (2) 상태 마커에는 **근거를 병기**한다 — 어느 리포
  어느 브랜치를 언제 봤는지(#436 이 만든 규약). (3) 주석으로 "다른 이슈 소유"라며 미룰 땐 그
  이슈의 할 일 목록에 그 항목이 실제로 들어갔는지 확인한다 — 안 들어가면 그 이슈가 닫히는
  순간 부채가 고아가 된다.
- 관련: `app/agents/buyer/cart/remove.py`·`app/core/config.py`·`app/schemas/spring.py`·
  `app/services/spring_client.py`·`tests/unit/test_cart.py`·`tests/unit/test_wishlist.py` ·
  api-spec §3.1·§4.12~4.16 · 이슈 #285·#435·#436

---
## [2026-08-10] 오분류 방어는 "막기"만으로 부족하다 — 두 방향 정정이 짝이어야 한다
- 증상: #440 은 `wishlist_remove` 로 오분류된 발화("찜닭 빼줘")에서 찜이 실제로 지워지는 파괴적
  동작을 근거 게이트(`has_wishlist_remove_evidence`)로 막았다. 그런데 그 게이트는 "막기"만 하고
  "고쳐 보내지"는 않는다 — `"찜닭 빼줘"`는 찜도 안 지워지지만 사용자가 실제로 요청한 장바구니
  삭제도 아무도 수행하지 않아 **조용히 증발**했다. 이슈 제목의 "엉뚱한 걸 지우거나(막았다) **또는
  요청한 걸 안 지우거나(안 고쳤다)**"에서 후자가 후속 PR까지 남아 있었다.
- 원인: 판정 계층이 "LLM 산출이 틀렸다"는 것까지는 알았는데(결정론 계층이 반대로 본다), 그
  사실을 되돌려 **정정 라우팅**으로 넘기는 대칭 코드가 없었다 — 막는 코드만 있고 고쳐 보내는
  코드가 없으면, 막힌 자리에 사용자의 진짜 요청이 그대로 버려진다.
- 규칙: **결정론 계층이 LLM 오분류를 "막을" 수 있다는 것을 알았으면, 그 반대 방향(정정 라우팅)도
  같이 짝지어 구현했는지 확인한다.** 막기만 구현하고 정정을 빠뜨리면 파괴적 동작은 안 나지만
  사용자 요청이 무응답으로 증발하는 두 번째 결함이 생긴다 — 둘 다 실패 모드이지 한쪽만 고치면
  절반짜리 수정이다.
- 관련: `app/agents/buyer/graph.py::corrected_to_cart_remove`(#440 후속) ·
  `app/agents/buyer/cart/intent_guard.py::has_deceptive_wishlist_marker` ·
  `tests/unit/test_wishlist_remove_resolution.py` §4-E · 이슈 #440

## [2026-08-10] 서로 다른 저장소를 잇는 단계 사이에는 "의도"를 먼저 적어야 재개가 성립한다
- 증상: #358 조립부(`apply_edge_mutation`)가 `문서 쓰기 → 감사 → 원장 완료` 순인데, **문서는 썼고
  완료 표시 전에 끊긴** 창에서 재시도가 `404`(초기화는 `409`)를 받았다. api-spec §3.9.2 가 ⚠️ 로
  *"이미 삭제된 edge 는 404 로 뭉뚱그리면 멱등 규약과 정면으로 모순"* 이라고 못박은 바로 그 판정을
  하고 있었다. PR 리뷰가 잡았다.
- 원인 셋이 겹쳤다.
  1. **404 판정이 원장을 안 봤다.** `document` 만 보고 "대상이 없으니 404"라 했는데, 그 부재가
     *이 요청이 만든 결과*일 수 있다. 게다가 그 판정이 claim 보다 앞이라 **TTL 이 지나도 같은 답**
     이었다 — 자가 복구가 구조적으로 불가능했다.
  2. **의도를 안 적었다.** 문서 쓰기와 완료 표시는 서로 다른 저장소라 한 트랜잭션에 못 묶이는데
     (다중 항목 원자성 없음), 그 사이에 끊기면 "이 요청이 무엇을 만들려 했는가"가 어디에도 없어
     재개가 응답을 재구성할 수 없다. SPEC 이 "저널 **선행** 기록(의도)"이라고 적은 이유가 이거였는데
     구현이 그 단계를 생략했다.
  3. **크래시한 시도의 lease 가 살아 있어** 재선점이 막혔다 — 재시도가 최대 TTL 동안 틀린 답을 받는다.
- 규칙: **서로 다른 저장소에 걸친 다단계 쓰기는 (a) 첫 쓰기 전에 의도를 적고, (b) 실패·부재 판정을
  그 의도 기록과 함께 하고, (c) 재개가 남은 단계만 마저 하게 만든다.** 셋 중 하나만 빠져도 중간에
  끊긴 요청이 영구히 틀린 답을 받는다. 특히 "부재"를 오류로 판정하는 자리는 **그 부재를 내가 만든
  것인지** 먼저 물어야 한다.
- 규칙(추가): 재개가 생기면 **각 단계가 멱등이어야** 한다. 감사처럼 "한 변경 = 한 행"인 기록은 자연
  키로 UNIQUE 를 걸어 두 번째 쓰기를 무시하게 한다. 그 키가 PII 를 담으면 지문으로 바꿔 담는다.
- 규칙(추가): **크래시 재개는 "무엇이 진짜 상호배제인가"를 먼저 정해야 한다.** 여기서는 per-user
  advisory 락이 그것이고, 그 락을 쥔 채 `processing` 잔재를 봤다면 주인은 돌고 있지 않다 — lease
  만료를 기다릴 이유가 없다. 반대로 `completed` 는 어떤 경우에도 재선점하지 않는다(부작용 2회).
- 관련: `app/agents/profile/graph_journal.py::apply_edge_mutation`·`record_intent`·`claim(takeover=)` ·
  `tests/unit/test_profile_graph_apply.py` · api-spec §3.9.2 · PR #540 리뷰

## [2026-08-10] 새 pg 모듈의 정리 배선은 "테스트 하니스"와 "앱 종료" 두 곳이다
- 증상: #358 의 `graph_journal` 을 `tests/conftest.py::close_pg_pools_on_loop` 에는 배선했는데
  **`app/main.py::_close_owned_resources` 에는 빠뜨렸다.** 통합 테스트도 유닛도 초록이었다 —
  테스트는 하니스 쪽만 쓰고, 앱 종료 경로를 재는 테스트는 자원 목록을 손으로 열거하고 있었기
  때문이다. 그 상태로 배포하면 **재배포마다 pg 커넥션 풀이 새고**, `max_connections` 에 닿아서야
  무관해 보이는 곳에서 연결 실패로 드러난다.
- 원인: 같은 성격의 목록이 두 곳에 있는데 서로를 강제하지 않았다. 한쪽만 채워도 스위트가 통과한다.
- 규칙: **pg 풀을 여는 모듈을 새로 만들면 배선은 세 곳이 한 단위다** —
  ① `tests/conftest.py::close_pg_pools_on_loop`(빠지면 CI 무한 대기)
  ② `tests/integration/test_pg_pool_loop_teardown.py`(①의 누락을 잡는 가드가 성립하려면 필요)
  ③ **`app/main.py::_close_owned_resources`(빠지면 운영에서 커넥션 누수)**.
- 규칙(추가): 목록을 **손으로 세는 대신 구조로 고정한다.** 이름을 하드코딩한 테스트는 "둘 다
  잊는" 실패 모드를 그대로 남긴다 — 패키지를 훑어 `close_pool` 을 가진 모듈이 전부 배선돼
  있는지 확인하는 테스트를 넣었고(`test_every_pg_pool_module_is_wired_into_lifespan_shutdown`),
  그게 실제로 이 누락을 잡았다.
- 관련: `app/main.py::_close_owned_resources` · `tests/unit/test_main_lifespan.py` · 이슈 #208·#358

## [2026-08-10] CHECK 제약의 어휘를 바꾸면 DROP → 행 이행 → ADD 셋이 한 세트다
- 증상: 감사 `action` 어휘를 `edgeSuppress`→`edgeDelete` 로 개명(#499)하면서 **세 번 연속으로
  다른 실패**를 밟았고, 매번 **기존 데이터가 있는 볼륨에서만** 터졌다.
  1. 코드·DDL 문자열만 바꿨다 → `IF NOT EXISTS` 로 감싼 `ADD CONSTRAINT` 가 아무것도 안 해서
     **이름이 같은 낡은 CHECK 가 그대로 남았고**, 새 값 INSERT 가 거부됐다.
  2. 제약을 DROP 후 재생성했다 → 옛 값이 든 기존 행 6건 때문에 `ADD CONSTRAINT` **검증이 실패**해
     `_ensure_schema` 전체가 죽었고, dev 폴백이 그 예외를 삼켜 "풀이 안 열림"으로만 보였다.
  3. 행을 먼저 UPDATE 했다 → **낡은 CHECK 가 아직 살아 있어** 새 값이 거부됐다
     (`new row for relation ... violates check constraint`).
- 원인: `CREATE ... IF NOT EXISTS` / `DO $$ IF NOT EXISTS ... ADD CONSTRAINT $$` 관용구는
  **"없으면 만든다"이지 "다르면 고친다"가 아니다.** 그리고 제약과 데이터는 서로를 막는다 —
  제약이 살아 있으면 데이터를 못 고치고, 데이터가 낡았으면 제약을 못 건다.
- 규칙: **enum 성격의 CHECK 어휘를 바꾸면 한 트랜잭션 안에서 `DROP CONSTRAINT IF EXISTS` →
  기존 행 UPDATE → `ADD CONSTRAINT` 순서로 쓴다.** 셋 중 하나만 빠져도 빈 볼륨(CI·새 개발자)에서는
  통과하고 **데이터가 있는 볼륨에서만** 깨진다 — 즉 운영에서 처음 드러난다.
- 규칙(추가): **DDL 실패가 dev 폴백에 삼켜지면 증상이 "연결 실패"로 위장한다.** `_get_pool` 이
  `except Exception → InMemory 폴백` 이라 진짜 원인(제약 위반)이 안 보였다. 스키마 오류를 쫓을
  때는 폴백을 우회해 `_ensure_schema` 를 직접 부르거나, psql 로 같은 DDL 을 실행해 본다.
  (Windows 에서 psycopg async 를 스크립트로 직접 돌리면 `ProactorEventLoop` 비호환이라
  `docker exec ... psql` 이 더 빠르다.)
- 관련: `app/agents/profile/graph_journal.py::_ensure_schema` ·
  `tests/integration/test_pg_graph_journal.py` · #358 / #499

## [2026-08-10] "되돌리면 깨지는지"를 실제로 해 보면 테스트가 주장을 안 재고 있는 게 드러난다
- 증상: #358 조립부에서 `409` 경로의 claim 롤백(`release`)을 **일부러 제거했는데 14건이 전부
  통과**했다. `test_conflict_releases_the_claim_so_a_corrected_retry_can_proceed` 라는 이름을 달고
  있었는데도 그랬다.
- 원인: 그 테스트가 재시도에 **다른 `If-Match`**(g41→g42)를 썼다. 파생 키가
  `{action}:{userId}:{scopeId}:{ifMatch}` 라 `If-Match` 가 바뀌면 **키 자체가 달라져**, 남아 있는
  claim 이 애초에 방해할 수 없다. 이름이 주장하는 인과가 시나리오에 없었다.
- 규칙: **"되돌리면 깨지는지"는 문장으로 적지 말고 실제로 되돌려 돌려 본다.** 통과하면 그 코드가
  불필요하거나 테스트가 다른 것을 재고 있는 것이고, 둘 다 고칠 거리다. 특히 **키·식별자가 입력에서
  파생되는 경우** 두 요청이 같은 키를 쓰는지 먼저 확인한다 — 안 그러면 "경합"을 재는 시나리오가
  실은 경합이 아니다. 여기서는 롤백이 진짜로 필요한 자리가 `409` 가 아니라 **no-op** 이었다
  (같은 `If-Match` 로 온 진짜 변경이 같은 키를 쓴다).
- 규칙(추가): 같은 점검에서 **만들어 놓고 배선 안 한 방어**도 드러났다(`request_fp`). 방어 장치를
  추가한 커밋과 그것을 호출부에 꽂는 커밋이 다르면, 그 사이에 "있는데 안 쓰이는" 상태가 남는다 —
  방어를 추가할 때 **그 방어가 없으면 깨지는 테스트를 같은 커밋에** 넣는다.
- 관련: `app/agents/profile/graph_journal.py::_apply_claimed` ·
  `tests/unit/test_profile_graph_apply.py` · #358

## [2026-08-10] 표현을 바꾸면 그 표현을 읽던 먼 코드가 조용히 판정을 잃는다
- 증상: #358 에서 삭제를 "`status="suppressed"` edge 보존" → "물리 삭제 + 별도 tombstone 리스트"로
  바꿨더니, `builder._summary_input` 이 **지운 취향을 요약에 되돌려 넣기** 시작했다.
- 원인: 그 함수는 "문서에 없는 `edge_key` 는 `active` 로 간주한다"는 규칙을 갖고 있었다. 절단으로
  빠진 edge 를 삭제로 오인하지 않으려는 **정당한** 규칙인데, 삭제가 edge 를 문서에서 없애는 순간
  "없음"의 뜻이 둘("저장 한계로 빠짐" vs "사용자가 지움")이 되어 규칙이 반대로 작동했다.
- 규칙: **"없음"을 신호로 쓰는 코드를 먼저 찾고 나서 표현을 바꾼다.** 부재(absence)로 판정하는
  자리는 표현이 바뀔 때 침묵으로 깨진다 — 예외도 타입 오류도 안 난다. 검색어는 필드 이름이 아니라
  **그 필드가 없을 때의 기본값**(`.get(key, DEFAULT)` · `or` · `if not`)이다.
- 규칙(추가): 이번엔 기존 테스트 6건이 잡아 줬다. 표현 변경 PR 에서 **기존 테스트가 무더기로
  깨지는 것은 신호지 잡음이 아니다** — 하나씩 "이 테스트가 재던 성질이 새 표현에서도 성립하는가"를
  묻고, 성립하면 매개체만 바꾸고 성립하지 않으면 그게 설계 결함이다.
- 관련: `app/agents/profile/builder.py::_summary_input` · `graph_merge.py` · REQ-PGRAPH-023 · #358

## [2026-08-09] OS별 구현이 다른 저수준 API를 전역 패치로 막으면 CI가 초록인 채 로컬만 죽는다
- 증상: 유닛 스위트가 Windows 로컬에서 **556건 실패 / 4716 통과**. 실패가 `test_home_recommendation`
  (66)·`test_seller_api`(64)·`test_auth_e2e`(59)처럼 **async 비중이 높은 파일 순서**로 몰렸고, 오류는
  전부 `ConnectionRefusedError: unit tests must not open live TCP connections` + teardown 경고
  `'ProactorEventLoop' object has no attribute '_ssock'`. 같은 커밋에서 CI(ubuntu-latest)는 전부 success.
- 원인: 바로 아래 항목의 TCP 격리 가드가 `socket.socket`을 전역 패치해 AF_INET/AF_INET6 `connect`를
  거부하는데, **Windows에는 AF_UNIX socketpair가 없어** CPython이 `socket.socketpair()`를
  127.0.0.1 리스닝 소켓 + `connect()`로 흉내낸다. asyncio는 이벤트 루프 생성 시 self-pipe를
  socketpair로 만들므로 **루프 생성 자체가 실패**했다(`_ssock` 미설정이 그 흔적). 리눅스·macOS는
  커널 AF_UNIX라 `connect()`를 타지 않아 이 경로가 아예 없다.
- 규칙: **테스트 격리를 위해 저수준 API를 전역 패치할 때는 그 API의 OS별 구현 차이를 먼저 확인한다.**
  `socketpair`·`pipe`·`fork`처럼 POSIX 원형이 Windows에서 에뮬레이션되는 것들이 대상이다. 그리고
  차단 가드는 **"막는 것"과 "통과해야 하는 것"을 같은 파일에서 둘 다 테스트로 고정**한다 — 막는
  쪽만 재면 이런 과차단을 못 잡고, 통과 쪽만 재면 가드에 뚫린 구멍을 못 잡는다. 예외 범위를
  스레드 전역이 아니라 **스레드 로컬**로 두는 것도 같은 이유다(예외 구간에 다른 스레드의 실 TCP가
  묻어 통과하면 안 된다).
- 규칙(추가): **CI가 단일 OS면 그 OS에서만 성립하는 회귀는 영영 안 잡힌다.** 로컬 전용 실패를
  "환경 탓"으로 넘기기 전에 원인을 한 번은 분류한다 — 여기서는 그 분류가 실제 버그를 찾아냈다.
- 관련: `tests/unit/conftest.py::_guarded_socketpair` · `tests/unit/test_unit_tcp_guard.py` ·
  `.github/workflows/ci.yml:10`(ubuntu-latest) · #474 회귀, #358 작업 중 발견

## [2026-08-10] 반복 실행 지표에서 기준선만 repeat 0 으로 고정하면 지터가 신호로 둔갑한다
- 증상: Tier L 축 유출(`filterAxisLeakage`)이 기준선을 `repeat == 0` 한 벌로 뽑아 놓고 비교
  대상은 전 repeat 을 돌았다. `--repeats 1` 에서는 무해했지만 `--repeats 3` 을 켜는 순간
  arm 의 repeat 1·2 가 **기준선의 다른 샘플**과 비교돼, 같은 프롬프트라도 반복마다 흔들리는
  LLM 지터가 "프로필이 새로 만든 필터 축"으로 계상된다.
- 원인: 같은 파일의 활성화 지표(`activation.ranking_change`)는 `(caseId, repeat)` 로 짝짓는
  규약을 docstring 에까지 명시해 뒀는데, 유출 지표만 그 규약 밖에 있었다. 두 지표가 같은
  산출물을 읽으면서 짝짓기 키가 서로 달랐고, repeats=1 에서는 두 규약이 우연히 일치해
  어긋남이 드러나지 않았다.
- 규칙: 반복 실행 산출물을 짝지어 비교하는 지표는 **예외 없이 `(caseId, repeat)`** 를 키로
  쓴다. `caseId` 만 쓰는 코드를 보면 "repeats>1 에서도 맞는가"를 먼저 묻는다. 그리고 짝이
  없는 행은 `[]`(=이상 없음)로 채우지 말고 `None`(=계산 못 함)으로 남긴다 — 빈 리스트는
  측정 중단을 안전 신호로 둔갑시킨다(아래 #512 교훈과 같은 취지다). 이때 "측정됐는가" 판정은
  **양쪽 모두**에 걸어야 한다: 기준선만 거르면 비교 대상 자신이 스텁일 때 빈 입력이 "이상 없음"
  으로 계산돼 같은 둔갑이 방향만 바꿔 되살아난다(PR #536 리뷰에서 두 번 지적받았다).
- 관련: `evals/personalization/cli.py::annotate_axis_metrics`,
  `evals/personalization/activation.py::_by_pair_key` (#483)

---

## [2026-08-10] 이슈 착수 전에 **열린 PR**을 확인한다 — 이슈 본문은 작성일에 멈춰 있다 (#306)
- 증상: #306(미룬 턴 재시도 스킵 원복) 계획을 "오늘 기본값에서 무동작(no-op)인 죽은 코드 정리"로
  세워 검증까지 마쳤는데, 착수 직전 발견한 **열린 PR #532**(#406 + #394 원복)가 그 전제를
  뒤집었다. 그 PR 은 `spring_max_retries` 를 0→1, `rescue_budget_mode` 를 observe→narrow 로
  올리고 **우리가 고칠 `with` 블록을 통째로 재작성**해 놓은 상태였다(겹치는 파일 6개). 계획을
  처음부터 다시 세우고 검증도 다시 돌렸다.
- 원인: 착수 판별을 **이슈 본문과 병합된 `dev`** 만으로 했다. 이슈는 작성일(08-04) 스냅샷이고
  `dev` 는 아직 머지되지 않은 작업을 모른다 — 그 사이의 진실은 **열린 PR·원격 브랜치**에 있다.
  #306 은 심지어 관련 이슈(#427·#394)를 본문에 다 적어 두었는데, 그 이슈들이 **닫힌 뒤** 후속
  PR 이 또 열려 있었다.
- 규칙: 이슈에 착수하기 전에 **`gh pr list --state open` 으로 열린 PR 을 훑고, 손댈 파일과
  겹치는 PR 이 있으면 그 diff 를 먼저 읽는다.** 특히 (1) 이슈 본문이 지목한 심볼을 grep 으로
  열린 브랜치들에서 찾고, (2) `git log --all --grep=<이슈번호>` 로 다른 브랜치가 이미 그 이슈를
  언급하는지 본다. 겹치면 **머지 순서를 먼저 정한다** — 선후가 뒤집히면 한쪽이 다른 쪽 전제를
  깨고, 계획의 "동작이 바뀌는가" 판정 자체가 반대가 된다.
- 관련: #306, PR #532(#406), `git log --all --grep`, `gh pr list --state open`

---

## [2026-08-09] 실패를 기본값(0·빈 리스트)으로 환원하지 않는다 — 기본값은 정상값과 구별되지 않는다
- 증상: 판매자 매출 도구가 오류 없이 HTTP 200 으로 **틀린 답**을 내는 경로가 3개 있었다.
  (1) `granularity=summary` 는 응답 shape 이 달라 `SalesResult(extra="allow")` 가 `series=[]`
  로 삼켜 **언제나 "총매출 0원"**, (2) 파싱은 되지만 정규형이 아닌 ISO(`"20260801"`)가
  문자열 비교 필터(`p.date >= from_date`)의 경계값이 돼 전 포인트 탈락 → 또 0원,
  (3) 표본 3개 미만이라 검정 불능인데 빈 리스트가 "이상 감지 없음"으로 번역돼 확정적
  all-clear 가 나갔다.
- 원인: 세 경로 모두 실패를 **그 도메인의 기본값**(0원·빈 목록)으로 환원했다. 기본값은
  정상값과 형태가 같아 로그에도, 판매자 눈에도 이상으로 보이지 않는다. 특히 "형식 오류는
  Spring 검증 경로에 맡긴다"(`except ValueError: pass`)는 사실상 **상대 파서의 관대함에
  안전을 건 것**이었다.
- 규칙: 판정 불능·미지원·형식 위반은 기본값이 아니라 **타입이나 문자열로 드러낸다** —
  판정 함수는 "결과"와 "판정 가능 여부"를 함께 반환하고(`decided`), 도구는 지원하지 않는
  입력을 `Error:` 로 즉시 거절한다. 문자열이 비교 연산의 경계값이 될 자리라면 파싱 성공이
  아니라 **정규형 일치**(`parsed.isoformat() == 원문`)까지 확인한다.
- 관련: `app/agents/seller/tools.py` · `app/agents/seller/analysis/timeseries.py` ·
  `app/agents/seller/analysis/types.py` · `docs/specs/STATUS-seller-analysis-2026-08-09.md` §3 · #512

---

## [2026-08-09] pydantic 의 `str = ""` 기본값은 **키 결측**만 흡수한다 — nullable 컬럼에는 `| None` 이 필요하다
- 증상: I-31 리뷰 조회에서 `rows[]` 중 **한 행만** `content: null` 이어도 그 페이지 전체가
  `ValidationError` → `SpringUnavailableError` → 도구 degrade 로 죽었다. 판매자에게는
  "리뷰 조회에 실패했습니다" 로만 보여서, 부분 결측이 원인이라는 단서가 표면에 없었다.
- 원인: `SellerReviewRow.content: str = ""` 였다. 기본값은 **키가 아예 없을 때** 쓰이는 값이지
  타입 허용 범위가 아니다 — 명시적 `null` 이 오면 pydantic 은 기본값과 무관하게 `str` 에
  `None` 을 넣기를 거부한다. 베이스의 `extra="allow"` 도 여분 필드만 다루므로 이 경로를
  구제하지 못한다(#489 가 고친 `extra="ignore"` 필드 소실과는 층위가 다르다). DDL 은
  `content TEXT NULL` 이었고 별점만 남기는 리뷰는 흔한 입력이었다.
- 규칙: 스키마 필드를 쓸 때 **DDL/명세의 NULL 허용 여부를 기본값이 아니라 타입으로** 옮긴다.
  `X = ""`·`X = 0` 은 "이 필드는 절대 null 이 아니다" 는 선언이며, 확신이 없으면 `| None` 이
  기본이다(#197 의 "기본값 0 금지" 와 같은 취지 — 그쪽은 오독, 이쪽은 전량 실패). 그리고
  **행 단위 결측이 페이지 전체를 죽이는지**를 스키마 리뷰의 상시 질문으로 둔다: 한 행짜리
  정상 픽스처만 있으면 이 실패 모드는 테스트에 잡히지 않는다 — null 행과 정상 행을 **섞은**
  응답으로 고정한다.
- 관련: `app/schemas/spring.py`(SellerReviewRow) · `app/agents/seller/tools.py`(표시 폴백) ·
  `tests/unit/test_seller_spring_client.py::test_get_reviews_parses_null_content_without_failing_the_page` ·
  이슈 #518 · api-spec §4.20

---

## [2026-08-10] 구조화 로그의 message 형식을 바꾸면 선택자도 전수 이관한다
- 증상: #385가 구조화 이벤트의 message를 JSON으로 바꾼 뒤, 예산 테스트 4건이 옛
  `record.message == "recommend_pipeline"` 선택자를 써 `StopIteration` 또는 `None`으로 실패했다.
- 원인: 첫 수정에서 일부 caplog 선택자만 JSON 파싱으로 바꾸고, 저장소 전체의 message/msg/getMessage
  기반 이벤트 선택을 전수 검색하지 않았다.
- 규칙: 구조화 로그의 message 표현을 바꿀 때는 안정적인 LogRecord extra 선택자(여기서는 `event`)를
  함께 제공하고, 이벤트명으로 message/msg/getMessage를 고르는 모든 호출부를 grep으로 0건 확인한다.
- 관련: `app/core/logging.py::log_structured` · `tests/unit/test_rescue_budget_427.py` · #385

---

## [2026-08-10] `extra=` 검증은 렌더된 sink 문자열까지 확인한다
- 증상: 구제 체인 이벤트가 `extra`에 계측 필드를 넣었고 caplog의 `record.rescue_elapsed_ms` 단언도
  통과했지만, 표준 formatter가 `%(message)s`만 출력해 운영 stdout에서는 필드가 전부 사라졌다.
- 원인: LogRecord 속성 보존과 formatter 렌더는 별도 단계인데, 테스트가 전자만 확인했다.
- 규칙: stdout/file sink로 소비되는 구조화 로그는 실제 formatter로 레코드를 렌더한 뒤 파서·집계기까지
  왕복하는 회귀 테스트를 둔다. `extra=` 속성 단언은 호환성 검증으로만 남긴다.
- 관련: `app/core/logging.py::log_structured` · `tests/unit/test_aggregate_rescue_chain.py` · #385

---

## [2026-08-09] 분포 분리 테스트는 변이 뒤 순위가 실제로 바뀌는 표본을 써야 한다
- 증상: #385의 `may_auto_relax=False` 분리 테스트가 False 표본 하나(1ms)와 True 표본 둘
  (100/200ms)을 썼더니, False를 True 분포에 잘못 섞는 변이가 통과했다.
- 원인: 최근접 순위 p50은 `[100, 200]`과 `[1, 100, 200]`에서 모두 100이라, "작은 False 값을
  넣었다"는 사실만으로 분리 결함을 검출하지 못했다.
- 규칙: 분위수·분모 분리 회귀 테스트는 의도한 잘못된 결합을 실제로 적용한 변이에서 적어도 하나의
  단언값이 달라지는 손계산 표본을 사용하고, 변이 실행으로 그 실패를 확인한다.
- 관련: `tests/unit/test_aggregate_rescue_chain.py` · #385

## [2026-08-10] 동시 레인의 완료 조건은 이슈가 아니라 최신 dev에서 다시 실측한다
- 증상: 이슈 완료 조건에 "미구현"으로 표시된 항목을 그대로 믿고 승인 0건 가드를 구현했는데,
  같은 조건을 다른 레인(PR #502)이 이미 dev 에 넣어 둔 상태였다. back-merge 때 같은 판정이 두
  곳에 생겨 한쪽을 걷어내야 했다.
- 원인: 착수 시점에 `origin/dev` 를 fetch 하지 않고 베이스 커밋(`f1f621e`) 기준으로만 실측했다.
  그 사이 dev 는 34커밋 앞서 있었고, 문제의 코드는 "docs 전용"이라고 적힌 PR(#502, 커밋
  `13c84e3`)이 함께 실어 커밋 제목·본문으로는 검색되지 않았다.
- 규칙: 동시 레인이 여럿이면 착수 전 `git fetch origin` 후 **`origin/dev` 기준으로** 완료 조건을
  실측한다. 이슈 본문의 체크박스와 완료 조건 표는 작성 시점 스냅샷이라 근거가 아니다. 커밋 제목이
  docs 라도 코드가 실려 있을 수 있으니 `git log -S<심볼> origin/dev -- <경로>` 로 심볼 단위 확인을
  한다.
- 관련: `app/pipelines/color_synonyms.py::_warn_empty_map_once` · PR #502 · #505

---

## [2026-08-09] 전체 `ruff format` 은 현재 변경 범위를 벗어난 포맷 churn을 만든다 (#406)
- 증상: #406 검증에서 인자 없는 `uv run ruff format`을 실행해 이슈와 무관한 26개 파일이 순수
  포맷 변경으로 워킹트리에 함께 남았다.
- 원인: 저장소에는 CI가 강제하지 않는 기존 포맷 드리프트가 있고, 전체 format은 그 드리프트까지
  현재 작업의 변경으로 흡수한다.
- 규칙: format은 이번에 수정한 파일 목록만 인자로 넘기고, 전체 검증은 `uv run ruff check`로 한다.
- 관련: #406 · `.github/workflows/ci.yml` · `docs/lessons.md`

---

## [2026-08-09] `asyncio.run` 을 타는 유닛 테스트는 Windows 에서만 TCP 차단에 걸린다
- 증상: `tests/unit/test_personalization_scope.py::test_live_wrapper_routes_profile_for_all_scopes`
  가 로컬(Windows)에서만 `ConnectionRefusedError: unit tests must not open live TCP connections`
  로 실패한다. httpx 는 `MockTransport` 라 실제 요청이 없는데도 그렇다. 같은 코드가 CI 는 통과한다.
- 원인: `LiveBuyerAdapter.__call__` 이 `asyncio.run` 을 쓰는데, Windows 기본 루프인
  `ProactorEventLoop` 는 self-pipe 를 **TCP 루프백 socketpair** 로 만든다. `tests/unit/conftest.py`
  의 `_TcpRefusingSocket` 이 AF_INET `connect` 를 전부 거부하므로 루프 생성 자체가 죽는다
  (뒤따르는 `AttributeError: 'ProactorEventLoop' object has no attribute '_ssock'` 이 그 흔적).
  POSIX 는 AF_UNIX socketpair 라 같은 가드에 걸리지 않는다 — 그래서 OS 별로 갈린다.
- 규칙: 유닛 테스트에서 **주입·배선 경로만** 검증할 거면 `asyncio.run` 을 타는 실행기를 통째로
  부르지 말고 경계 함수(여기서는 `profile_for_scope`)의 입력을 가로채고 내부 어댑터는 대역으로
  바꾼다. 로컬 실패를 보고 "환경 탓"으로 넘기기 전에 **변경 전 baseline 에서도 같은 실패가
  나는지** 먼저 확인한다(`git stash push -u -m <고유태그>` → 확인 → `git stash apply <sha>` → drop).
- 관련: `tests/unit/conftest.py:23-36`, `evals/model_eval/adapter.py::LiveBuyerAdapter.__call__`,
  `tests/unit/test_personalization_scope.py::_resolved_markdowns` (#484)

---

## [2026-08-09] 계약 어휘 동기화의 완료 조건은 "신규 값 등장"이 아니라 "구 값 0건"이다
- 증상: BE 가 `ProductStatus.DELETED` 를 신설했는데, #472 사본 동기화가 api-spec §4.5 **표**를
  정본에 맞춘 뒤 §4.5 를 "확정·구현 완료"로 표시했다. 실제로는 §3.2 산문이 `status=HIDDEN` 으로
  남았고 `app/` 에는 `DELETED` 가 0회여서, AI 는 나흘간 삭제를 숨김으로 다뤘다.
- 원인: 완료 판정을 "새 값이 표에 있는가"로 했다. 새 값이 들어와도 **구 값이 남아 있으면 코드는
  구 값대로 동작한다** — 둘은 배타가 아니다.
- 규칙: 어휘를 바꾸는 동기화는 **폐기된 구 값의 repo 전역 잔여가 0건**임을 확인해야 끝난다
  (과거 변경 이력 행은 제외). 훑을 대상 목록은 같은 날의 「계약 문자열 폐기는 동의어·enum 값·
  다른 표기까지 함께 훑는다」 항목을 따른다.
- 관련: `docs/api-spec.md` v0.29.4 → v0.32.1 · #511 · BE `docs/backend/02-data-model.md` D41

---

## [2026-08-09] 선례를 근거로 규약을 세울 땐 그 선례가 실제로 그런지 코드로 확인한다
- 증상: `spring_client.py` 주석이 **"I-12 ALREADY_HIDDEN 논리"** 를 근거로 I-30 에 전용 예외
  3종을 두면서, 정작 그 논리의 출처인 I-11·I-12 에는 `error_code_map` 이 없었다.
- 원인: 신규 기능(#297)에서 규약을 정립하며 기존 호출부의 소급 적용을 남기지 않았다. 주석이
  선례를 인용하는 순간 "선례도 그렇게 돼 있다"는 착시가 생겨 아무도 되짚지 않았다.
- 규칙: 기존 API 를 근거로 새 규약을 세우면 **그 기존 API 가 규약을 따르는지 코드로 확인**하고,
  아니면 같은 PR 에서 맞추거나 이슈를 남긴다. 주석의 인용은 근거가 아니다.
- 관련: `app/services/spring_client.py` I-11·I-12·I-30 · #511 · #297

---

## [2026-08-09] 변이가 살아남으면 "테스트가 약한 것"이 아니라 **더 위 계층에서 멈춘 것**일 수 있다
- 증상: #476 공유 스트림 레지스트리에서 `release_stream` 의 SQL `AND stream_token=%s` 를 지우는
  변이를 넣었는데, 그 조건을 검증한다고 이름 붙인 통합 테스트
  (`test_pg_release_only_deletes_the_row_this_worker_owns`)가 **그대로 통과**했다.
- 원인: 테스트가 `SharedStreamRegistry.release()` 를 통해 들어갔는데, 레지스트리가 자기 로컬
  토큰 맵에 키가 없으면 **DB 를 치기 전에 조기 반환**한다. 즉 테스트는 상위 계층 가드를 재고
  있었고 SQL 조건에는 애초에 도달한 적이 없다. 이름과 주석은 SQL 을 검증한다고 말하고 있었다.
- 규칙: 다층 방어(호출부 가드 + 저장소 조건)를 넣었으면 **각 층을 그 층의 진입점에서** 시험한다.
  저장소 조건은 저장소 API 를 직접 불러(`store.release_stream(..., stream_token=<틀린 토큰>)`)
  확인하고, 변이 시험은 층마다 따로 돌린다. 변이가 살아남으면 "단언을 세게" 하기 전에
  **호출 경로가 그 코드에 닿는지부터** 확인할 것.
- 관련: `tests/integration/test_pg_shared_stream_registry.py` ·
  `app/core/stream_registry.py::SharedStreamRegistry.release` · #476

---

## [2026-08-09] Windows 로컬 `uv run pytest` 554건 실패는 TCP 차단 가드 × ProactorEventLoop 충돌이다
- 증상: 문서만 고친 브랜치에서 `uv run pytest` 가 **554 failed / 4729 passed**. 실패 트레이스는
  전부 `tests/unit/conftest.py:28 ConnectionRefusedError: unit tests must not open live TCP connections`
  이고, 무관한 `tests/unit/test_health.py` 도 똑같이 5건 실패한다. **CI(Linux)는 통과한다.**
- 원인: #501(`bbbf715`)이 넣은 유닛 테스트 TCP 차단 가드가 `AF_INET` connect 를 전부 막는데,
  **Windows 의 asyncio 는 `ProactorEventLoop` self-pipe 를 loopback TCP socketpair 로 만든다.**
  이벤트 루프를 만드는 테스트가 전부 가드에 걸린다. Linux 는 self-pipe 가 TCP 가 아니라 안 걸린다.
- 규칙: **로컬 pytest 대량 실패를 보면 먼저 무관한 테스트 하나(`test_health.py`)를 돌려 본다** —
  같이 죽으면 내 변경이 아니라 환경이다. 문서 전용 변경이라면 `test_contract_docs.py` 만 돌려
  계약 문서 검증을 확인하고, 전체 판정은 CI 에 맡긴다. 기존 메모의 "로컬 전용 실패 2건"은
  #501 이후 이 규모로 커졌으니 그 숫자를 믿지 않는다.
- 관련: `tests/unit/conftest.py:23-31` · `bbbf715`(#501) · #499

## [2026-08-09] 사본 동기화는 착수 시점에 정본을 다시 읽는다 — 이슈 본문은 작성일에 멈춰 있다
- 증상: #499 본문은 2026-08-08 BE 확약 기준 **4건**의 체크리스트였는데, 착수 시점에 노션 정본을
  열어 보니 **다음 날(08-09) 10벌이 확정되면서 §3.8 응답 구조까지 개편**돼 있었다. 이슈만 따랐으면
  `nodes[]` 폐지·edge 필드 13→5·`purged` 개편을 통째로 놓친 채 "완료" 보고를 했을 것이다.
- 원인: 사본 동기화 이슈는 **정본의 스냅샷**으로 작성된다. 정본은 그 뒤에도 움직이는데 이슈 본문은
  안 움직인다. 게다가 확정된 정본 페이지들이 *자기* 변경 이력으로 그 이슈 번호를 지목하고 있어,
  정본 쪽은 이미 "이 이슈가 다 반영할 것"으로 간주하고 있었다.
- 규칙: **동기화 작업은 착수 직전에 정본을 전수로 다시 읽고, 이슈 본문과의 차이를 먼저 보고한다.**
  범위가 넓어지면 사용자에게 확인받되, 이슈 본문을 "범위의 상한"으로 취급하지 않는다.
- 관련: `docs/api-spec.md` v0.32.0 · 노션 「♻️ 취향 관리 API 10개 — 고쳐야 할 것 정리」 · #499

## [2026-08-09] 계약 문자열 폐기는 동의어·enum 값·다른 표기까지 함께 훑는다
- 증상: undo 폐기 정정에서 `restorable`·`includeSuppressed` 를 다 지웠는데 **감사 로그 enum 의
  `edgeRestore`** 가 api-spec §6.3·SPEC §5.4 양쪽에 그대로 남아 있었다. 준비한 grep 목록
  (`restorable`·`undo`·`되돌리기`…)으로는 **한 건도 안 잡혔다.**
- 원인: 폐기 대상을 "필드명"으로만 생각했다. 같은 개념이 **enum 값(camelCase 합성어)·한국어 서술·
  코드 docstring**으로 흩어져 있으면 문자열이 서로 다르다.
- 규칙: 개념을 폐기할 때 grep 목록에 **필드명 + enum 값 + 한국어 표현 2~3종 + 코드 주석**을 모두
  넣는다. 특히 `<동사>Restore` 처럼 **접두어가 붙어 원래 단어가 부분 문자열이 되는** 형태를 의심한다.
- 관련: `docs/api-spec.md` §6.3 (c) · `SPEC-PROFILE-GRAPH-149` §5.4 · #499

## [2026-08-09] 상대 구현을 "실질 효과 없음"으로 판정해 요청에서 빼지 않는다
- 증상: 협의 문서(`BE-NEGOTIATION-GRAPH-357`)는 C-27 캐시 무효화 필요 지점을 **"중지·초기화 2곳"**
  으로 축소하고 개별 삭제를 *"다음 consolidation 전까지 랭킹 미반영이라 실질 효과가 없다"* 며
  뺐다. BE 는 **수정·삭제·초기화·중지 4곳 전부**에 구현했다. 같은 문서 §6.1 은 "3종"이라 적어
  **내부에서도 숫자가 갈려 있었다.**
- 원인: 우리 판정 근거(랭킹 반영 시점)는 맞았지만, 상대는 **사용자 약속**("지웠다고 했는데 홈에
  남아 있다")을 기준으로 봤다. 축소 판정은 상대의 구현 자유도·판단 기준을 예측하는 일이다.
- 규칙: 요청 범위를 줄일 때는 **줄인 근거를 상대에게 보이되 항목은 남긴다**("불필요해 보이지만
  판단은 그쪽"). 그리고 같은 문서 안에서 같은 수를 두 번 적었으면 **반드시 대조**한다.
- 관련: `docs/api-spec.md` §5 C-27 · `jarvis-backend#132` · #499

## [2026-08-09] 범위 대조는 선언된 건수보다 열거된 식별자를 우선한다
- 증상: #472 정본 인덱스는 범위를 44건이라 표기했지만, 실제 열거는 internal 36건·chat 6건·S-4·P-4/P-5·E-1로 46건이었다.
- 원인: 요약 집계와 개별 범위 목록이 독립적으로 수정돼 산술 검증이 빠졌다.
- 규칙: 전수 대조표는 식별자 열거를 기준으로 만들고, 선언 건수와 다르면 누락시키지 말고 불일치와 산식을 감사 결과에 기록한다.
- 관련: `docs/api-spec-canonical-audit.md` #472 범위 기준

---

## [2026-08-09] 로컬 pytest 무더기 실패는 워크트리 환경을 먼저 분리해 재현한다
- 증상: #472에서 로컬 `uv run pytest`가 38건 실패했다.
- 원인: 워크트리의 `.env`가 테스트 환경에 개입했지만, CI는 `.env` 없이 실행한다.
- 규칙: 무더기 실패 시 자기 변경이나 dev 환경을 의심하기 전에 `.env`를 내용을 열지 않고 잠시 치운 뒤 재현하고 반드시 되돌린다.
- 관련: #472 검증 기록

---

## [2026-08-09] 유닛 테스트는 로컬 BE가 살아 있어도 TCP를 열면 안 된다
- 증상: 로컬 Spring BE가 8080에서 실행 중이고 `.env`의 내부 토큰이 채워지면 재구매·완화 유닛
  테스트가 실제 응답을 받아 CI와 다른 단언 결과를 냈다.
- 원인: PG 연결은 `tests/unit/conftest.py`에서 구조적으로 격리했지만, httpx/anyio가 만드는 TCP
  연결에는 같은 차단 경계가 없었고 `INTERNAL_API_TOKEN`도 공통 환경 초기화에서 빠져 있었다.
- 규칙: 유닛 테스트는 `tests/unit/` 범위에서만 실제 TCP를 `ConnectionRefusedError`로 거부하고,
  로컬 서비스·토큰 유무와 무관하게 CI의 연결 실패 degrade 경로를 검증한다.
- 관련: `tests/conftest.py` · `tests/unit/conftest.py` · `tests/unit/test_network_isolation.py` · #474

## [2026-08-09] datasetHash 규칙을 바꾸면 연결된 baseline을 즉시 재생성한다
- 증상: `audit/holdout_runs.jsonl`을 해시 대상에서 제외한 뒤 datasetHash는 바뀌었지만,
  `dev-v2.3`와 `trivial_empty` baseline은 이전 hash를 계속 가리켰다.
- 원인: 재현 가능한 파일 목록을 고친 후 baseline 산출물의 `datasetHash` 연결을 재검증하지 않았다.
- 규칙: datasetHash 입력·제외 규칙을 바꾼 커밋에서는 모든 현재 baseline을 재생성하고, 산출물의
  hash가 manifest와 같은지 확인한다. append-only 런타임 로그는 해시에서 제외한다.
- 관련: `evals/goldenset/refresh_manifest.py::HASH_EXCLUDED_PATHS` ·
  `tests/unit/test_goldenset_audit.py` · #474

## [2026-08-08] 로컬 BE 가 떠 있으면 유닛 테스트가 라이브 BE 를 친다 — `dev` 나 내 변경을 의심하기 전에 `.env` 를 무력화해 재현하라
- 증상: 문서 2개만 고친 상태에서 `uv run pytest` 가 **38건 실패**했다(재구매 지목·완화 경로).
  같은 커밋이 6시간 전엔 4829 passed 였고 **CI 도 초록**이었다. 실패는 결정적이었고(ordering
  무관, `-p no:randomly` 동일) 문서를 stash 해도 그대로였다.
- 원인: 누군가 3시간 전 로컬에 **Spring BE(:8080)·mariadb·redis 를 띄웠고**, 이 worktree `.env`
  에 유효한 `INTERNAL_API_TOKEN` 이 있어 **유닛 테스트가 라이브 BE 를 호출**했다. 그래서 주입한
  가짜 검색 대신 실 카탈로그 상품 id 가 push 페이로드에 실렸다. `tests/conftest.py` 는
  `OPENAI/ANTHROPIC/GOOGLE_API_KEY` 만 비우고 **`INTERNAL_API_TOKEN` 은 비우지 않는다** —
  CI 는 BE 가 없어서 이 갭이 드러나지 않았다.
- 규칙: 로컬 pytest 가 CI 와 다르게 깨지면 **코드보다 환경을 먼저 의심한다**. 순서는
  (1) `Settings()` vs `Settings(_env_file=None)` 의 **차이 나는 필드 이름만** 뽑아 본다
  (값 출력 금지 — 시크릿이 섞인다), (2) 후보를 하나씩 빈 값으로 덮어 이분한다
  (`INTERNAL_API_TOKEN= uv run pytest ...`), (3) `docker ps` 로 로컬 BE·DB 기동 여부를 본다.
  `.env` 를 읽거나 옮기지 말 것 — 덮어쓰기(override)만으로 판정된다.
- 관련: `tests/conftest.py`(키 3종만 무력화), `app/core/config.py::Settings.model_config`
  (`env_file=".env"`, CWD 상대), #395 작업 중 발견
## [2026-08-08] `ruff format` 을 인자 없이 돌려 무관한 파일 30개가 diff 에 딸려 왔다
- 증상: #438 작업 중 `CLAUDE.md` "자동 정리: `uv run ruff check --fix && uv run ruff format`" 을
  문자 그대로 인자 없이(= 저장소 전체 대상) 돌렸더니, 이번 이슈와 무관한 파일 30개가 순수 포맷
  변경으로 딸려 들어왔다 — `data-analysis/generate_dummy.py` 만 +1189줄,
  `docs/research/research-275-harness/*`·`evals/ablation/*`·`evals/scoring/*`·여러
  `tests/unit/test_*.py`·`.github/scripts/review_mode.py`. `git status --porcelain` 으로 발견해
  `git checkout --` 로 그 파일들만 원복했다.
- 원인: 저장소에 **사전 존재하던 포맷 드리프트**다. CI 와 pre-commit 훅이 실제로 강제하는 것은
  다르다 — CI 는 `ruff check` 만 돌고(`ruff format --check` 는 안 돈다), pre-commit 의
  `ruff-format` 훅은 **스테이징된 파일에만** 걸린다. 그래서 한 번도 커밋 경로를 타지 않은
  파일들(분석 스크립트·연구 하네스 등)은 포맷되지 않은 채로 남아 있고, 그 상태에서 전체
  `ruff format` 을 돌리면 무관한 파일이 한꺼번에 재포맷된다. `CLAUDE.md` 의 문구를 그대로
  따르면 누구나 이걸 밟는다.
- 왜 나쁜가: 한 커밋 = 한 논리 단위 규약이 깨지고, 리뷰어가 실제 변경을 포맷 노이즈 속에서
  찾아야 하며, 무관한 파일을 건드려 다른 레인과 충돌할 수 있다.
- 규칙: 커밋 전 자동 정리는 **이번에 실제로 고친 파일에만 스코프를 좁혀** 건다
  (`uv run ruff format <파일들>`). 전체 대상 `ruff format` 은 "포맷 드리프트 정리" 를 목적으로
  하는 **별도 PR** 에서만 돌린다. 돌렸다면 `git status --porcelain` 으로 의도 밖 파일이 없는지
  반드시 확인하고, 있으면 `git checkout --` 로 되돌린 뒤 커밋한다. (`uv run ruff check` 는
  전체로 돌려도 안전하다 — 이번 사고는 `format` 쪽이다.)
- 관련: #438 · `CLAUDE.md` "커밋 워크플로" 2단계 · `.pre-commit-config.yaml`(ruff-format 은
  스테이징 파일 한정) · `.github/workflows` 의 CI 는 `ruff check` 만 실행

## [2026-08-08] 응답 픽스처 계약 테스트는 "요청 파라미터 누락"을 못 잡는다 (#494)
- 증상: `get_reviews(stats=True, rating="1,2")` 가 rating 을 쿼리스트링에 **안 실어** 전 별점
  합산 `byProduct` 를 받아왔다. HTTP 200, 예외 없음, 숫자도 자연스러움 — 워커는 그것을
  "1–2점이 몰린 상품"으로 서술했다. 명세(I-31)가 대표 사용례로 든 질문이 조용히 틀렸다.
  `passed=True` 로 끝나므로 **로그·구조화 트레이스에도 안 남는다.**
- 원인: `SpringClient.get_review_stats` 시그니처에 `rating` 이 아예 없었다. 도구 층은 인자를
  받아서(`tools.py` 시그니처·docstring 에 존재) 클라이언트에 넘기지 않고 **버렸다** — 무시
  사실을 출력에 적지도 않았다. 기존 테스트는 응답 JSON 픽스처를 고정해 파싱만 검증해서,
  요청이 무엇을 보냈는지는 아무도 보지 않았다.
- 규칙:
  - **필터 인자를 받는 클라이언트 메서드에는 요청 쿼리스트링 스냅샷 테스트를 별도로 둔다.**
    응답 shape 검증(픽스처 계약 테스트)과 요청 파라미터 검증은 서로 다른 실패를 잡는다 —
    후자가 없으면 인자 누락이 200 뒤에 숨는다. 실린 것뿐 아니라 **안 실려야 할 것**
    (집계 모드의 sort/limit/offset)도 같이 못 박는다.
  - 도구가 인자를 받아 하위로 안 넘길 때 선택지는 둘뿐 — **넘기거나, 무시를 코드로 강제하고
    그 사실을 출력 문자열에 적거나.** 조용히 버리는 세 번째는 없다
    (선례: `get_order_events` 의 `ignored_status_note`).
  - 집계 결과를 문장으로 내보낼 때는 **어떤 필터가 적용된 집계인지 스코프를 함께 적는다.**
    "리뷰 집계: 총 18건"과 "리뷰 집계(별점 1,2 한정): 총 18건"은 워커에게 전혀 다른 사실이다.
  - 0건 응답도 같은 함정 — "리뷰가 없습니다"와 "별점 1,2 리뷰가 없습니다"를 구분한다.
- 관련: `app/services/spring_client.py` `get_review_stats`, `app/agents/seller/tools.py`
  `get_reviews`, `docs/api-spec.md` §4.20(I-31), 이슈 #494

## [2026-08-08] TTL 만료를 엄격 부등호로 재면 판정이 시계 분해능에 걸린다 (리눅스만 통과)
- 증상: `period_confirm.load_pending` 의 TTL 테스트(`test_pending_expires_after_ttl`, ttl=0)가
  **리눅스 CI 에서는 늘 통과하는데 Windows 로컬에서 실패**했다 — 만료됐어야 할 대기가
  그대로 돌아왔다. #345 에서 들어온 코드이고 #346 작업 중 로컬 실행에서 처음 드러났다.
- 원인: `datetime.now(UTC) - created_at > ttl` 의 **엄격 부등호**. ttl=0 이면 "경과가 0보다
  커야 만료" 라는 뜻이 되는데, 저장→조회가 인메모리 체크포인터라 마이크로초 안에 끝난다.
  Windows 의 기본 시스템 타이머 틱은 ~15.6ms 라 두 `now()` 가 **같은 값**을 반환해 경과가
  정확히 0 이 되고 만료 판정이 안 선다. 리눅스는 µs 분해능이라 항상 양수가 나와 가려졌다.
  "시간이 흐른다" 를 코드가 암묵적으로 가정했고, 그 가정이 플랫폼마다 다른 값이었다.
- 규칙: 만료·쿨다운·디바운스처럼 **경과 시간을 임계와 비교**할 때 경계를 포함할지(`>=`)
  배제할지(`>`)를 의식적으로 고른다 — 임계 0 이 "즉시"를 뜻해야 하면 `>=` 다. 그리고
  "두 번의 `now()` 사이에는 시간이 흐른다"를 전제로 테스트를 쓰지 않는다: 그건 OS 타이머
  분해능에 의존하는 가정이고, 리눅스 CI 가 초록이어도 개발자 머신에서 깨진다.
- 관련: `app/agents/seller/period_confirm.py::load_pending` · 같은 형태가
  `hitl.py:527`(draft TTL)에도 있으나 ttl=0 경로가 없어 현재는 드러나지 않는다 · #345·#346

## [2026-08-08] 머지 여부를 확인하면서 `git branch -r --contains` 와 페이지 요약을 "독립된 두 근거"로 착각했다
- 증상: #346 착수 전 선행 조건(#345 머지)을 확인하면서 **"PR #429 는 아직 open"** 이라고 보고했다.
  실제로는 그날 `dev` 로 머지된 뒤였다. 사용자가 PR 상태를 직접 확인하고 나서야 정정했고,
  그 사이 "머지될 때까지 대기 vs `feat/345` 위에 스택" 이라는 **있지도 않은 선택지**를 놓고
  설계 논의를 한 턴 낭비했다.
- 원인: 근거가 둘이었지만 **같은 결함을 공유**했다. (1) 페이지 요약이 오래된 상태를 반환했고,
  (2) 교차 검증으로 쓴 `git branch -r --contains <sha>` 는 **로컬 원격 참조**만 본다. 그 직전
  `git fetch` 가 네트워크 차단(403)으로 실패한 것을 보고도, 그 실패가 (2)의 전제를 무너뜨린다는
  연결을 짓지 않았다. 두 근거가 모두 "오래된 스냅샷"이라는 하나의 원인에서 나온 셈이라,
  일치하는 것이 확증이 아니라 **같은 오류의 중복**이었다.
- 규칙: 원격 상태(머지·브랜치 존재·태그)를 판단할 때는 **`git fetch` 가 성공했는지 먼저 확인**하고,
  실패했으면 `origin/*` 기반 판정을 근거로 쓰지 않는다 — "fetch 불가"는 "확인 못 함"이지
  "없음"이 아니다. 교차 검증을 셀 때는 근거의 개수가 아니라 **실패 원인이 서로 독립인지**를 센다:
  같은 캐시·같은 스냅샷·같은 네트워크 경로를 공유하는 두 근거는 하나로 친다.
- 관련: #346 착수 · PR #429(#345) · `git worktree`/`origin/dev` 확인 절차

---

## [2026-08-08] 하나의 명세 개정이 여러 I-번호에 걸치면, 반영한 것 말고 **안 한 것**을 세야 한다
- 증상: #481 이 노션 2026-08-06 개정을 반영하면서 I-8·I-14 만 손보고 **I-16 을 빠뜨렸다**(#487).
  그 개정의 핵심이 "회원 재식별 키(memberId)를 판매자 LLM 표면에서 걷어낸다" 였는데, I-16
  `get_churn_cohort` 요약은 계속 `[41] 마지막 활동 …` 로 원시 memberId 를 실었다. 노션 I-16 은
  "I-8·I-14 와 동시 배포(분리 시 그 사이 기간 개인정보 노출 지속)" 를 명시했으므로, #481 이
  막으려던 노출이 정확히 한 경로로만 그대로 열린 채 배포된 것이다.
- 원인: 두 가지가 겹쳤다. ① 개정 범위를 "이슈 제목에 적힌 I-번호"(I-14·I-8)로 잡고, 개정
  문서가 건드리는 I-번호 전체를 역으로 세지 않았다 — I-16 은 #481 의 api-spec 개정문에
  `returnReasonsTop` 단위 파급으로 **이름이 언급되기까지 했는데** 그게 "확인했다"는 착각을 줬다.
  ② 공유돼야 할 규약 문구(customerLabel 주의)가 `_ORDER_LOG_RULES_NOTE` 안에 I-14 기록 규칙과
  **섞여 박혀 있어** 재사용 지점이 없었다. 재사용 가능한 상수였다면 "이 상수를 쓰는 곳이 한
  군데뿐"이라는 사실 자체가 누락 신호였을 것이다.
- 규칙:
  - 정본(노션) 개정 하나를 반영할 때는 **그 개정 문서가 언급하는 I-번호를 전부 나열해
    체크리스트로 만들고**, 각 항목에 "반영함 / 해당 없음(이유)" 중 하나를 붙인다. 이슈 제목의
    번호만 따라가지 않는다. 개정문에 "동시 배포" 문구가 있으면 분리 반영은 그 자체로 계약 위반이다.
  - 여러 계약 경로가 공유하는 규약 문구는 **처음부터 상수로 뽑아** 각 도구 출력에 부착한다.
    한 도구의 노트 안에 섞어 넣으면 두 번째 경로에서 복붙본이 갈라지거나(규약이 낡음),
    이번처럼 아예 누락된다.
  - 개인정보 관련 필드 교체는 **폴백을 만들지 않는다** — 신 필드 결측 시 구 필드로 되돌리면
    "미배포 구간에는 원래대로 노출"이 되어 차단 자체가 무의미해진다. 결측은 `?` 로 떨어뜨리고,
    그 상태를 회귀 테스트로 고정한다(구응답 스텁 → 요약에 원시 키 부재 단언).
- 관련: `app/agents/seller/tools.py`(`_CUSTOMER_LABEL_NOTE`·`get_churn_cohort`),
  `app/schemas/spring.py`(`ChurnMember`), `docs/api-spec.md` §4.4 I-16(v0.29.1), #481·#487

---

## [2026-08-08] 명세 개정이 폐기한 규정이 프롬프트·주석에 남으면 미반영이 아니라 LLM 에 대한 능동적 오정보다
- 증상: I-13 `purchaseComplete` 산출 규정은 2026-07-31 에 "이벤트 기준, 권위는 I-6/I-14"
  → "주문 기준 집계(`order_item × product × brand`, PAID, `COUNT(DISTINCT order_id)`)" 로
  개정됐다(jarvis-backend#62 근본 수정 배포 / #196). 그런데 구 규정 문구가 코드에 **3개월간
  잔존**해, `get_behavior_events` 도구 출력 말미에 상시 부착되고 behavior·abuse 워커
  프롬프트에 박힌 채 LLM 에게 "이 값은 0 일 수 있으니 근거로 쓰지 말라"고 안내하고 있었다.
  워커는 실재하는 구매 데이터를 신뢰 불가로 취급하고 다른 도구로 우회한다 — **데이터가
  없어서 못 쓰는 게 아니라, 우리가 쓰지 말라고 시켜서 안 쓴 것**이다.
- 원인: 개정 작업이 "새 규정을 어디에 반영할까"(추가 지점)만 보고 "구 규정이 어디에
  적혀 있나"(제거 지점)를 grep 하지 않았다. 게다가 잔존 지점이 이슈에 적힌 3곳이 아니라
  **6곳**이었다 — 도구 상수(`tools.py`)·워커 프롬프트 2종(`prompts.py` ABUSE/BEHAVIOR)·
  스키마 docstring(`spring.py`)·군집 모듈 docstring(`segmentation.py`)·계약 사본
  (`docs/api-spec.md` §4.4 I-13). 특히 **주** 프롬프트인 BEHAVIOR_PROMPT 와 계약 사본이
  이슈 목록에서 빠져 있었다. 기존 테스트는 전부 "구 문구가 **있는지**"를 어설션해서
  (`assert "권위는 매출 조회(I-6)" in result`) 드리프트를 잡기는커녕 **고정하고** 있었다.
- 규칙: (1) 계약·명세를 개정하면 **폐기되는 문구를 문자열로 grep** 해 잔존 지점을 전부
  세고 같은 PR 에서 지운다 — 코드뿐 아니라 프롬프트·docstring·`docs/api-spec.md` 사본까지.
  이슈에 적힌 목록을 그대로 믿지 말고 직접 grep 한다. (2) LLM 에 주입되는 문구를 바꿀 때는
  "새 문구가 있다"는 어설션만 두지 말고 **"폐기 문구가 없다"는 역방향 어설션**을 함께 남긴다
  — 존재 어설션은 드리프트를 못 잡고, 문구가 재작성될 때마다 리터럴만 갱신되며 살아남는다.
  (3) 부재 검사의 범위는 **LLM 이 실제로 읽는 표면**(도구 출력 문자열·프롬프트 상수)으로
  한정한다. 파일 단위 grep 으로 짜면 "구 규정은 폐기됐다"고 남긴 개정 이력·주석까지 잡혀
  결국 이력을 못 남기게 된다. (4) 폐기 규정을 근거로 세웠던 **판단 게이트**도 함께 걷어낸다
  — BEHAVIOR_PROMPT 에는 "구매 관련 판정은 퍼널과 교차 확인한 뒤에만 warning 이상으로
  올린다"는 게이트가 있었고, 전제가 사라진 뒤에도 남으면 근거 없이 워커 민감도만 깎는다.
- 관련: `app/agents/seller/tools.py::_BEHAVIOR_PURCHASE_RULES_NOTE`(구
  `_BEHAVIOR_AUTHORITY_NOTE`) · `app/agents/seller/prompts.py`(BEHAVIOR/ABUSE) ·
  `app/schemas/spring.py::BehaviorEventsResult` · `app/agents/seller/analysis/segmentation.py` ·
  `docs/api-spec.md` §4.4 I-13(v0.29.2) · `tests/unit/test_seller_tools.py::
  test_behavior_surfaces_drop_deprecated_purchase_wording` · #488

## [2026-08-07] "얼마나 좁힐지" 계산에 하한만 걸고 "이미 지났으면" 을 안 걸면 좁히기가 음수를 낸다
- 증상: #427 리뷰(오케스트레이터 직접 재현)가 `rescue_deadline` 이 이미 지난 턴(과거
  `turn_started_at`)에서 `narrow_search_budget` 이 **음수 예산**을 받는 결함을 잡았다.
  `_stage_budget` 은 `granted = min(spring_search_timeout_s, remaining / n)` 를 그대로
  돌려주는데, `remaining = rescue_deadline - time.monotonic()` 이 음수면 `granted` 도 음수다.
  `_apply_stage_budget` 의 skip→narrow 강등 두 경로(본검색의 `allow_skip=False`, `narrow`
  모드의 skip 실행)가 그 음수를 검증 없이 그대로 실행에 넘겼다 — `asyncio.wait_for(timeout=
  음수)` 가 즉시 만료돼 **HTTP 요청 자체가 나가지 않았다**. "본검색은 절대 건너뛰지 않는다"는
  불변식이 이름만 남고 실제로는 건너뛴 것보다 나쁜 결과(요청 없이 실패)를 냈고, 자동완화
  probe 는 `relaxing` progress 를 emit 해 놓고 아무 일도 안 하는 거짓 신호(H4)까지 재현했다.
- 원인: `granted >= min_threshold` 형태의 하한 분기(`"narrow"` vs `"skip"` 판정)를 만들 때
  "판정이 `narrow`" 와 "그 값이 실행 가능한 양수" 를 같은 조건으로 착각했다 — 실제로는
  `min(x, remaining/n)` 의 `remaining` 이 음수일 수 있다는 걸 놓쳐 `granted < min_threshold`
  분기(`"skip"`)만 하한 아래를 잡고, `"skip"` 을 다시 `"narrow"` 로 강등하는 경로에는 하한이
  전혀 적용되지 않았다. **판정 임계값과 실행 임계값은 같은 변수를 참조해도 강제 지점이 다르면
  분리해서 각각 확인해야 한다** — 하나는 분류용(threshold 비교), 하나는 집행용(clamp)이다.
  테스트도 `search=` 에 fake 를 주입해 `narrow_search_budget` 이 **불렸다는 것만** 확인하고
  그 인자 값이 실제로 유효한지, 끝단(`spring_client.search_products`)까지 살아있는지는 재지
  않아 이 결함을 통과시켰다.
- 규칙: "예산을 좁힌다/못 쓰게 건너뛴다" 류 로직에서 원본 시간축 계산(`deadline - now`)이
  음수가 될 수 있는 경우(데드라인이 이미 지난 턴), **판정(분류)과 실제로 실행에 넘기는 값을
  분리해서 각각 clamp 하라** — 판정은 "어느 분기인가"만 정하고, 그 분기가 무엇이든 실행 직전에
  "이 값을 그대로 API 에 넘겨도 되는가"(양수·최소 유효값 이상)를 다시 확인한다. 테스트는
  fake 를 주입해 "그 함수가 불렸다"만 보지 말고, 스파이가 **실제 함수를 통과시키면서** 인자
  값을 기록하게 하거나(`with real_fn(x): yield` 형태), 최소한 그 값 자체에 대한 별도 어설션을
  추가한다 — 호출 여부와 호출 값의 유효성은 다른 주장이다.
- 관련: `app/agents/buyer/recommendation/graph.py::stream_recommendation` 의 `_stage_budget`/
  `_apply_stage_budget`(D4) · `app/services/spring_client.py::narrow_search_budget` · #427

## [2026-08-07] `open_stream` 의 `inner_factory` 시그니처를 바꾸면 테스트 전수가 조용히 깨진다
- 증상: #427 에서 `open_stream(..., inner_factory: Callable[[], AsyncIterator[str]])` 를
  `Callable[[float], AsyncIterator[str]]` 로 바꿨더니(D2 턴 시작 시각 플럼빙), `uv run pytest`
  전체 실행에서 `tests/unit/test_observability.py`·`test_infra.py`·`test_recommendation.py`·
  `test_buyer_tracing.py`·`test_seller_tracing.py`·`test_session_claim_api.py`·
  `test_spring_search_budget_132.py`·`tests/integration/conftest.py`·`evals/scoring/adapter.py`·
  `evals/model_eval/adapter.py`·`evals/metrics/harness.py`·`evals/first_event_budget/
  measure_first_event.py` 에 걸쳐 `TypeError: <lambda>() got an unexpected keyword/positional
  argument` 가 40건 넘게 났다 — 전부 `inner_factory` 로 넘기는 0-인자 로컬 함수/람다/클래스
  (`async def slow(): ...`, `lambda: httpx.AsyncClient(...)`)였다.
- 원인: `open_stream` 은 `app/services/spring_client.py::_client()` 처럼 프로덕션 코드
  안쪽에만 있는 함수가 아니라, 테스트가 **직접 인자로 넘기는 콜백**의 시그니처 계약이다.
  그런 함수는 `grep -rn "open_stream(" tests/` 로도 호출부만 보이고 실제 깨지는 지점(그 호출에
  넘긴 콜백의 정의부)은 별도로 찾아야 한다 — 콜백 정의가 호출부와 수십~수백 줄 떨어져 있거나
  다른 파일(`tests/integration/conftest.py` 의 공유 fixture, `evals/*/adapter.py` 의 하네스)에
  있으면 놓치기 쉽다. `spring_client._client()` 도 같은 패턴이라 `timeout=` 키워드 인자를
  추가했을 때 `lambda: httpx.AsyncClient(...)` 로 patch 한 fake 들이 같은 이유로 깨졌다.
- 규칙: 테스트가 **콜백으로 주입하는** 함수(래퍼가 시그니처를 정의하고 호출부가 인자를 받아
  넘기는 패턴 — `open_stream(inner_factory)`, `_client()` 등)의 시그니처를 바꿀 때는
  `grep -rn "<함수명>(" tests/ evals/` 로 호출부만 보지 말고, 그 호출에 넘겨지는 각 인자
  (변수명)의 **정의부**를 별도로 찾아 전수 갱신한다. 새 인자는 가능하면 키워드 전용 +
  기본값(`*, timeout: float | None = None`)으로 추가해 fake 들이 무시해도 무해하게 만들되,
  위치 인자가 필수면(D2 의 `turn_started_at` 처럼 값 자체를 검증해야 하는 경우) 콜백 시그니처를
  전수 갱신하고 `uv run pytest`(전체, 개별 파일 단위가 아니라)로 결과를 확인한다 — 개별 파일만
  돌리면 다른 파일의 같은 패턴을 놓친다.
- 관련: `app/core/stream.py::open_stream`(D2) · `app/services/spring_client.py::_client`(§1) ·
  #427

## [2026-08-07] 두 정상 설계의 이음매는 어느 쪽 코드를 봐도 결함으로 안 보인다 — 적용 범위를 문서에 적어라
- 증상: #435 "추천 카드를 이름으로 지목한 찜/담기가 실패한다"가 여러 라운드 동안 "미확정"
  으로 남아 있었다. `screen_reference.py`(화면 지시어 결정적 해소기)를 보면 정상 설계고,
  `no_condition.rank_by_profile`(프로필 벡터 추천)를 봐도 정상 설계다 — 어느 쪽도 단독으로는
  결함이 아니다.
- 원인: FE 위조방지 설계(추천 카드는 서버가 `listId` 로 이미 알아 `screen` 에서 의도적으로
  제외)와 AI 상품명 공백(AI 카탈로그 인덱스에는 원본 컬럼이 없어 프로필 경로가 상품명을
  모른다)이 **서로 다른 시점에 각자 옳게 결정된 설계**인데, 그 둘이 만나는 지점(추천 카드
  턴의 이름 지목)에 아무도 적어두지 않았다. `screen_reference.py` 를 진단하는 사람은 "이
  모듈은 `screen.products` 가 있을 때만 돈다"만 보고 추천 카드 턴이 왜 안 되는지 모르고,
  `no_condition.py` 를 진단하는 사람은 "이름을 모른다"만 보고 그게 이음매 반대편에서 되물음
  문구·찜 실패로 이어지는 줄 모른다. 각 모듈 안에서는 완결된 근거가, 모듈을 넘어서는 인과를
  가리지 못했다.
- 규칙: 두 설계가 서로의 전제를 깨는 지점(A 가 의도적으로 비운 것을 B 가 의도적으로 요구하는
  경우)을 발견하면, 그 교차점을 **양쪽 모듈 docstring 모두에** 적어라(한쪽에만 적으면 반대편
  진단자가 여전히 못 찾는다). "이 결함처럼 보이는 동작은 설계의 귀결이다"라고 명시적으로
  적어야 다음 추적 라운드가 같은 두 모듈을 또 오가며 낭비되지 않는다.
- 관련: `app/agents/buyer/screen_reference.py`(모듈 docstring) ·
  `app/agents/buyer/recommendation/no_condition.py::rank_by_profile` ·
  `docs/api-spec.md` §3.1 v0.28.1 · #435
## [2026-08-08] 어절 경계가 없는 한국어에서 부분 문자열 표지만으로 조회/해제(파괴적 동작)를 가르려 하지 마라
- 증상: #386 은 "내가 뭐 찜했지?" 같은 찜 조회 발화가 `wishlist_remove` 로 잘못 라우팅돼 찜을
  지우는 것을 두 가지 접근으로 막으려다 둘 다 되돌렸다 — ① 조회 표지 목록("보여줘"·"뭐 있어"
  전수화)은 한국어 조회 표현을 전수로 나열해야만 성립해 목록 밖 표현에서 뚫렸다. ② "찜 명사 ×
  해제 동사" 부분 문자열 결합(`["찜","위시리스트"] × ["빼","해제","취소",...]`)은 짧은 표지가
  다른 낱말에 묻히는 것을 걸러내지 못했다 — `"찜"` ⊂ `찜닭`·`갈비찜`·`찜질방`, `"빼"` ⊂ `빼고`
  라 `"찜닭 빼고 보여줘"`(음식 조회)가 해제 근거로 오인됐다. 같은 뿌리 문제가 반대 방향으로도
  났다 — `"찜한"` 이 `wishlist_reference_markers` 에 지시 수식어로 등록돼 "찜한 거 빼줘" 같은
  진짜 해제 요청이 되물음으로 새는 거짓음성(#440 이슈 본문).
- 원인: 한국어는 어절 경계가 명시적이지 않다(공백이 형태소 경계와 안 맞는다). "표지 문자열이
  발화에 포함돼 있는가"만으로 판정하면 짧은 표지일수록 다른 낱말에 우연히 포함될 확률이 높고,
  표지 목록을 아무리 늘려도(접근 ①) 반대쪽(짧은 표지의 오탐, 접근 ②)을 못 막는 트레이드오프에
  갇힌다. 필요한 것은 표지 목록의 양이 아니라 **판정의 구조** — 어절 경계 검사(다른 낱말에
  묻힌 출현을 죽인다) + **닫힌 어휘 브리지**(head-tail 사이에 정해진 어휘만 올 수 있다는 규칙 —
  라운드 3 리뷰 F7 이후. 처음엔 거리(인접 창)로 시작했다가 "같은 명령"을 보장하지 못해
  브리지로 교체했다, 이 항목 뒤쪽 **[라운드 3 리뷰 F7·F9 추가]** 문단 참조)이다.
- 규칙: 한국어 발화에서 "명사 A 근처에 동작 B가 있으면 의도 C" 류 판정이 필요하면, 부분 문자열
  결합이나 표지 전수화를 시도하기 전에 **어절 경계 + 닫힌 어휘 브리지** 판정부터 설계해라
  (#440 의 `negation.matches_pair_unnegated` — `_is_boundary_char`+`_consume_prefix` 로 경계를,
  `wishlist_remove_bridge_words` 로 인접성을 본다. 거리(`pair_window`)로 시작하지 마라 —
  이 항목 뒤쪽 문단이 왜 실패하는지 적어 뒀다). 그리고 한 결함을 **두 계층**(프롬프트·결정론,
  또는 판정·적용)이 각자 막을 때는 "두 계층이 같은 판정을 하는가"를 단언하는 테스트를 반드시 함께 둬라
  (`has_wishlist_remove_evidence` — `classify_cart_utterance` 가 `wishlist_remove` 를 내면
  이 함수도 반드시 `True` 다, 안 그러면 한쪽만 고쳐지고 다른 쪽은 "근거 없다"며 되물음으로
  조용히 회귀한다). **파괴적 동작(삭제류)의 자동 선택 규칙에 새 게이트를 추가할 때는 변이
  시험으로 그 게이트가 공허하지 않은지 확인해라** — `return True`/`return False` 로 강제
  치환했을 때 각각 반대쪽 테스트 그룹(거짓양성/거짓음성)이 빨개져야 게이트가 실제로 결과를
  가르고 있다는 증거다. **[라운드 2 리뷰 F6 추가]** 경계 검사를 **새** 판정(`matches_pair_
  unnegated`)에만 넣고 **기존** 표지 목록 매칭(`wishlist_remove_markers` 를 보던
  `matches_unnegated`)에는 안 넣으면, 새 판정이 막은 바로 그 함정(`"찜"` ⊂ `갈비찜`)을 옛
  경로가 그대로 통과시킨다 — `"갈비찜 빼줘"`가 `"찜 빼줘"` 를 부분 문자열로 포함해 사다리
  1번에서 매칭되고, `has_wishlist_remove_evidence` 도 1번과의 합집합이라 `True` 가 돼 규칙
  2·3 자동 삭제까지 열렸다(실측 확인, 파괴적). **같은 결함을 막는 두 경로(새 판정·옛 표지
  매칭)가 있으면 둘 다 같은 경계 규약을 써야 한다** — 하나만 고치면 "판정을 새로 만들었지만
  옛 경로가 그 판정을 우회한다"는 새 형태의 재발이 된다. **[라운드 3 리뷰 F7·F9 추가]**
  **거리(창)는 "같은 명령"을 보장하지 못한다** — `tail_start - head_end <= pair_window` 는
  `"찜 보고 이거 빼줘"`(서로 다른 절)와 `"찜 목록에서 빼줘"`(같은 절)를 구분하지 못했다(둘 다
  간격 7자). 인접을 거리로 정의하지 말고 **닫힌 어휘 브리지**(head 와 tail 사이에 정해진
  어휘만 올 수 있다는 규칙)로 정의해야 "같은 명령"임을 구조로 보장할 수 있다. 그리고 **판정을
  정정하는 경로를 새로 만들면, 그 도착지가 전제하는 전처리를 함께 받는지 확인해라** — F9 는
  `cart_remove` → `wishlist_remove` 정정을 화면 지시어 해소(`resolve_screen_reference`) **뒤에**
  두었다가, 정정된 도착지(`wishlist_remove`)가 애초에 화면 순번 해소를 전제로 설계된 흐름이라
  화면 3열+찜 2건 상황에서 사용자가 가리키지 않은 항목이 삭제됐다(재현 확인) — 정정은 그
  전처리보다 **먼저** 계산해 전처리 진입 조건에 포함시켜야 한다. **[라운드 4 리뷰 F10·F12
  추가]** **어간을 화이트리스트에 넣지 마라** — `"해"` ⊂ `해당`·`해도`·`했는지` 처럼 어간은
  다른 낱말·다른 활용에 전부 묻힌다. 이 이슈에서 같은 함정을 `"찜"`(head)·`"빼"`(cart_remove
  표지)·`"해"`(용언 어미)로 **세 번** 밟았다 — 화이트리스트·표지 목록에는 항상 "요청을 완성하는
  형태"(뒤에 다른 활용이 이어질 수 없는 종결형)만 담아라. 그리고 **해소기(resolver)가 "확정
  거부" 신호를 냈으면 downstream 의 자동 선택 규칙이 그걸 되살리면 안 된다** — 거부를 그냥
  `product_id=None` 같은 값으로만 전달하면, 그 값이 다른 신호(예: 이름 미지목)와 구분이 안 돼
  downstream 이 "그냥 문맥이 없다"로 오독하고 스스로 다시 고른다. 거부는 값이 아니라 **명시적
  신호**(`screen_refused: bool`)로 별도 전달해야 한다 — 그리고 그 신호를 넘겨야 하는 **모든**
  경로(정정 경로·기존 분기·2선 방어 위임)를 grep 으로 찾아 확인해라. F12 가 두 경로만 고치고
  2선 방어(`cart/graph.py::stream_cart_add` 의 위임)를 빠뜨렸는데, 하필 그게 이 이슈의 원래
  거짓음성 경로였다(라운드 5 리뷰 F15). **[라운드 5 리뷰 F13·F14 추가]** **여러 모듈이
  공유하는 표지 목록은 "넓히면 안전"이 성립하지 않는다** — 라운드 4(F11)가 `utterance_
  negation_markers`(담기·찜 추가·`remove.py` 가 공유)를 넓혔다가, 그 목록을 쓰는 한 호출부의
  fallback(`wishlist_add` 무효화 → 사다리 기본값 `cart_add` 로 떨어짐)이 "개입 축소"가 아니라
  **다른 자원에 실제 변경**이라 회귀를 냈다("이거 찜해줘. 배송도 돼?"가 장바구니에 담겼다).
  넓히려면 그 목록을 쓰는 **모든 호출부의 fallback** 을 먼저 확인하고, 하나라도 다르면 경로
  전용 목록으로 분리해라. 그리고 **화이트리스트 매칭은 접두(startswith)가 아니라 종결로
  검사해라** — 접두만 보면 `"취소해줘라는 말이 뭐야?"` 가 `"해줘"`로 시작한다는 이유만으로
  명령이 된다. 이 이슈에서 같은 함정(짧은 조각/접두가 다른 말에 묻힘)을 `찜`(head)·
  `빼`(cart_remove 표지)·`해`(어간)·`해줘`(접두 매칭 구조 자체)로 **네 번** 밟았다.
  **[라운드 6 리뷰 F16·F17·F18 추가]** **열린 집합(유보·허가 표현)을 목록으로 막으려 하지
  마라 — 그 앞에 붙는 문법 요소(연결어미·인용 조사)는 닫힌 집합이다.** 라운드 4(F11, 공유
  목록에 나열)·라운드 5(F13, 찜 해제 전용 목록으로 분리해도 여전히 나열) 둘 다 "도 될"·"도
  돼"만 알고 "도 괜찮"·"도 상관없"에 다시 뚫렸다 — 목록을 두 번 늘렸다가 두 번 다 목록 밖
  활용에 뚫린 뒤에야 `tail_is_command`(연결어미 "도"/"야" + 인용 조사, 라운드 6)로 구조를
  바꿨다. 그리고 **한 결함을 두 계층(여기서는 "근거
  판정"과 "규칙 1 이름 매칭")이 따로 막을 때, 한쪽만 그 판정을 반영하면 다른 쪽이 그대로
  뚫린다** — `has_wishlist_remove_evidence` 가 유보·인용을 걸러도 `_resolve_wishlist_remove_
  target` 규칙 1이 그 판정을 몰랐다면("이어폰 찜 빼줘도 될까?" 가 이름만 보고 삭제됐다),
  이건 이 이슈가 처음부터 막으려던 바로 그 실패 모양의 재발이다 — 새 판정 함수를 만들지 말고
  **같은 판정 조각을 공유**해라. 마지막으로 **안전 신호를 "값이 비었다"로 흘려보내지 마라** —
  화면 해소기가 순번을 **거부**한 경우와 애초에 **돌지 않은**(pending) 경우가 둘 다
  `screen_reason=None` 으로 같은 값이 되면, downstream 이 "거부 없음"으로 오독한다. 무관한
  상태(pending)가 안전 게이트를 조용히 없애지 않도록 그 두 원인을 **하나의 명시적 불리언**
  으로 미리 합쳐서 넘겨라. **[라운드 7 리뷰 F19 추가]** 연결어미·인용 조사만으로는 두 독립
  문장이 이어지는 경우("찜 취소해줘. 무슨 뜻이야?")를 못 잡는 한계가 라운드 6에 남아 있었다 —
  이 역시 "무슨 뜻"을 인용 목록에 추가하는 나열로 메우지 않고, 세 번째 닫힌 집합인
  **문장 종결 부호**(`.`·`!`·`?`, 쉼표 제외)로 메웠다. 문장 종결 부호는 "이 명령은 여기서
  끝났다"는 닫힌 신호다 — 뒤에 다른 문장이 있으면 그 명령을 발화 전체의 지시로 읽지 마라.
  **[라운드 8 리뷰 F20·F21 추가]** 라운드 7까지도 절반만 옳았다 — `tail_is_command` 는 여전히
  "비명령 형태(연결어미 직결·인용 조사·문장 종결 부호+후속 내용)를 나열하고 그 밖을 전부
  명령으로 승인"하는 **기본-허용**이었고, 그 밖의 우회(연결어미를 띄어 씀·따옴표+조사·쉼표나
  세미콜론 뒤 메타언어)가 계속 나왔다. **파괴적 동작의 게이트는 "위험한 형태를 나열해 거부"가
  아니라 "안전한 형태를 증명"으로 써라** — 나열은 열린 집합을 못 덮는다는 것이 #386 부터 이
  이슈의 tail 층에서만 **두 번** 반복된 실패다(hedge 목록 → 인용 목록 → 그래도 남은 우회).
  그래서 `has_wishlist_remove_evidence` 에만 "해제 동작구가 발화를 끝냈는가"(`tail_terminates_
  utterance`, 기본-거부)를 요구해 라우팅용 `tail_is_command`(기본-허용)와 갈랐다. 이걸 가능하게
  하려면 먼저 **"라우팅이 옳으면 실행도 옳다"는 불변식을 버려야 했다** — 라운드 1(D4)이 건
  `classify_cart_utterance == wishlist_remove ⟹ has_wishlist_remove_evidence` 불변식이 근거
  판정의 tail 검사를 라우팅과 같게 묶어놔서 근거만 따로 엄격하게 만들 수 없었다. 라우팅은
  관대해도 무해하다(근거 없으면 되물음으로 끝난다) — 라우팅을 관대하게 두고 실행 게이트만
  엄격하게 하는 편이 안전하다는 것이 이 이슈 내내 옳았던 방향이었는데, 스스로 건 불변식이 그걸
  막고 있었다. `screen_refused` 도 같은 종류의 교훈을 한 번 더 냈다 — "화면이 있고 pending
  이거나 거부됐다"는 **대리값**은 발화가 화면을 전혀 가리키지 않아도 항상 참이 되어(과대
  차단) 핵심 양성을 되돌렸고, 화면이 빈 배열이면 항상 거짓이 되어(과소 차단) 확정 불가능한
  위치 지시를 통과시켰다 — 대리값 대신 "발화가 위치를 가리키려 시도했는가"·"그 시도가
  확정됐는가" 두 직접 신호로 바꿔야 두 방향의 오차가 함께 사라졌다.
  **[라운드 9 리뷰 F22·F23·F24·F25 추가]** 라운드 8도 절반만 옳았다 — F20 이 tail 오른쪽
  (종결)만 앵커했더니, 해제 문구를 **인용·번역·예시**의 목적어로 두고 그 문구로 발화를
  끝내는 부류가 그대로 통과했다(`"다음 문구를 영어로 번역해줘: '찜 해제해줘'"`). **부분
  문자열로 찾은 표지는 한쪽만 앵커하면 반대쪽이 열린다** — 파괴적 동작(규칙 2·3, 코드가
  대상을 고르는 자동 선택)의 근거로 쓰려면 **발화 전체**를 닫힌 어휘로 앵커해라. 단, 사용자가
  이름을 직접 댄 규칙 1은 상품명이 닫힌 어휘가 아니라서 이 전체 앵커를 걸면 안 된다 —
  확신의 등급이 다르면(코드가 고르는가, 사용자가 골랐는가) 근거 함수도 등급을 나눠라
  (`is_wishlist_remove_command_context` vs `has_wishlist_remove_evidence`). 종결 판정 자체도
  나열의 함정을 한 번 더 겪었다 — tail 표지 목록에 없는 존댓 활용("빼줘요") 하나가 정상
  존댓말을 거짓음성으로 만들었다(F23) — "요"는 활용이 아니라 **닫힌 보조사**라는 사실로
  받아야지 활용형을 나열하면 안 된다. 마지막으로 **LLM 산출을 결정론 규칙으로 덮어쓸 때만
  확신을 요구해라** — LLM 이 스스로 낸 판단(decompose 가 직접 `wishlist_remove` 를 냄)은
  존중하고 downstream 안전판(되물음)에 맡긴다. 반면 결정론 규칙이 LLM 판단을 **덮어쓰는**
  지점(`cart_remove`→`wishlist_remove` 정정, `cart_add`의 2선 방어 위임)에 근거를 안 걸면
  관대한 라우팅이 **다른 의도를 훔치고**(`"찜 해제해줘'로 대신 키보드 빼줘"` 가 장바구니
  삭제를 삼킴) **진행 상태를 파괴한다**(pending 위임이 `clear_pending` 을 실행). `screen_
  refused` 도 `cart_intent.product_id` 라는 **출처 있는 값**을 근거로 쓰면 해소기가 확정한
  id 와 decompose 가 낸 id 가 섞인다(F25) — 화면 해소 블록이 이미 들고 있는 **해소 결과
  그 자체**(`screen_resolved`)를 쓰면 출처가 섞이지 않는다.
  **[라운드 10 리뷰 F26·F27 추가]** 접근 자체는 옳았고 경계 계약 두 개가 비어 있었을 뿐이었다
  — 라운드 9 는 규칙 2·3(코드가 대상을 고르는 자동 선택)의 근거만 발화 전체로 앵커했지,
  규칙 1(사용자가 이름을 댄 경로)의 게이트(`is_wishlist_remove_command_context`)는 여전히
  발화 **전역** 판정이라 상품명이 인용문·예시 안에 있어도 통과했다. **"사용자가 이름을
  댔다"와 "그 이름이 명령의 대상이다"는 다르다** — 인용·예시 안의 이름은 지목이 아니다.
  전역 판정에 인용 여부를 추가로 물어봐야 소용없다(발화 어딘가에 인용부호가 있다는 사실
  자체는 이름 매칭과 무관한 축이라서다) — 게이트를 **이름→tail 구간 자체**로 좁혀서, 그
  구간에 규칙 2·3 과 같은 종결과, 이름 앞에 인용·삽입 부호가 없다는 조건 두 가지를 걸어야
  했다. 이름 매칭에도 **구간 계약**이 필요하다는 것이 핵심이다. 화면 위치 쪽도 같은 종류의
  구멍이 있었다 — `screen_resolved=True` 를 "규칙 3(목록 1건 자동)의 fallback 허용"으로
  읽은 게 원인이었다. 화면 위치를 **확정했는데 그 상품이 대상 목록에 없으면**, 목록에
  하나뿐이라는 이유로 **다른 것**을 지우면 안 되고 되물어야 한다 — **명시적 지목이 실패하면
  되물어라, 다른 후보로 대체하지 마라.** 단일 파생값(`screen_refused`)으로는 규칙 2("위치를
  가리켰는데 확정 못 했을 때만 막는다")와 규칙 3("위치를 가리킨 이상 확정 여부와 무관하게
  막는다")이라는 **서로 다른 계약**을 표현할 수 없었다 — 파생값을 원자(`screen_position_
  mentioned`·`screen_resolved`) 둘로 쪼개고 각 규칙이 필요한 조합을 직접 조립하게 하니
  두 계약이 동시에 성립했다.
  **[라운드 11 리뷰 F28·F29 추가]** 라운드 9 의 두 등급 분리(규칙 1=라우팅급, 규칙 2·3=전체
  앵커)는 **되돌렸다** — 등급을 나눈 게 오히려 두 라운드 연속 그 경계에서 구멍을 냈다.
  **거부 목록으로는 "메타언어인가"를 못 가른다** — `"내가 산 X 찜 빼줘"`(정상)와 `"사용자가
  말한 건 X 찜 빼줘"`(간접화법)는 접두의 **의미**로만 갈리는 같은 구조라, 인용부호 유무 같은
  형태 신호로는 절대 구분되지 않는다(부호 없는 간접화법이 항상 남는다). 파괴적 동작의 근거는
  **닫힌 형태를 증명**하는 쪽이어야 하고, 증명 못 하면 묻는다 — 그래서 규칙 1도 규칙 2·3 과
  **같은 전체 왼쪽 앵커**를 받도록 등급을 합쳤다(상품명 **자체**가 닫힌 어휘일 필요는 없다,
  그 **앞**이 닫힌 접두여야 한다는 뜻이라 등급을 합칠 수 있었다). **안전 계약을 등급으로
  나누면 그 등급 경계가 새 구멍이 된다** — 나눌 수 있으면 나누고 싶은 유혹이 있지만, 합칠 수
  있으면 합쳐야 한다는 것이 이 이슈에서 두 번 확인된 교훈이다. 다만 이 통합에는 **실측으로
  드러난 대가**가 처음엔 있었다 — `"이어폰은 찜 빼지 말고 케이스 찜 빼줘"`(부정된 절 뒤의
  이름)처럼 전체 왼쪽 앵커가 닿지 못하는 정상 발화도 함께 막혔다(`tests/unit/test_wishlist_
  flow.py` 의 라운드 11(#116/#117 번호 체계) 회귀 스위트 5건이 실제로 깨졌다 — 임의로 고치지
  않고 그대로 보고했다). **[라운드 12 리뷰 F30 추가]** 그 대가는 앵커의 **정의 자체**가 틀려서
  생긴 게 아니라 **어휘가 너무 좁아서** 생긴 것이었다 — "닫힌 접두"(관형사·지시대명사)로
  좁혔던 게 문제였지, "발화 전체가 이 판정의 어휘로 설명돼야 한다"는 원칙 자체는 옳았다.
  `"이어폰은 찜 빼지 말고 "` 는 임의 텍스트가 아니라 **이 판정이 이미 다루는 개념**이다 —
  다른 후보 상품명("이어폰")·조사("은")·head("찜")·tail 어간+부정 표지("빼지 말고"). 그래서
  앵커 조건을 **"닫힌 접두"에서 "이 판정이 아는 어휘로 발화 전체가 소비되는가"로 일반화**
  했다 — head·tail 계열·부정 표지·다른 후보 상품명까지 어휘를 넓히니(전부 이미 있는 목록만
  합쳤다, 새 목록 없음) 정상 대조 발화("A 는 빼지 말고 B 를 빼라")는 살아나고, 우회를 만든
  **모르는 낱말**("사용자가"·"산"·"문구"·"예시")은 여전히 전부 막혔다 — 좁게 잡았다가
  넓혀야 했던 게 아니라, **"닫힌 목록"이 아니라 "이 판정 전체가 이미 아는 것"으로 어휘의
  경계 자체를 다시 그은 것**이다. 동사 어간("빼")처럼 독립 토큰으로 등재되지 않은 조각은
  뒤따르는 부정 표지가 실제로 붙어 있다는 사실로 흡수했다(부정 표지 catch-up) — 어간을
  독립 토큰으로 등재하면 라운드 4(F10)가 일부러 뺀 함정(맨 어간이 "할 방법"·"했는지" 같은
  질문까지 명령으로 읽던 것)이 되살아난다. 화면 위치 신호(F29)도 "숫자 위치"에서
  "화면 참조 시도"(지시대명사 포함)로 넓혔다 — `resolve_screen_reference` 자체가 이미
  지시대명사를 화면 참조로 처리하는데 판정 헬퍼가 그 사실을 몰랐던 게 원인이었다. 다만 이
  신호는 **화면이 실제로 왔을 때만**(`screen is not None`) 의미가 있다 — 화면이 아예 없는
  턴까지 좁히면 이 이슈 범위 밖의 회귀가 난다. **[라운드 13 리뷰 F31·F33 추가]** 라운드
  12(F30)의 "부정 표지 catch-up"(등재 안 된 어간을 뒤따르는 부정 표지로 흡수)은 그 자체가
  **양성 어휘 앵커에 넣은 예외**였다 — 아는 어휘를 하나도 소비 못 한 위치에서도 부정 표지가
  창 안에 있기만 하면 그 앞의 **모르는 문자를 통째로 건너뛰어**, `"예: 이어폰 말고 케이스
  찜 빼줘"` 처럼 인용·메모 접두를 삼켜 실제로 삭제했다(실측, 파괴적). **양성 어휘 앵커에
  "창 점프" 같은 예외를 넣지 마라 — 예외가 곧 우회다.** 어간이 모자라면 어간을 어휘에 넣되
  그 어간은 명령의 근거로는 쓰지 않는다(앵커는 "안다"만, 명령성은 종결형 tail 이 따로
  증명한다) — `wishlist_remove_action_stems`(앵커 스캔 전용, `config.py`)가 그 경계를 지킨다.
  F33 은 같은 라운드에서 규칙 1(이름 앵커)과 규칙 2·3(head 앵커)이 **서로 다른 함수·다른
  어휘**를 쓰고 있었다는 것도 드러냈다 — "근거 하나로 통일했다"는 주석은 두 판정이 **글자
  그대로 같은 코드**를 쓸 때만 사실이고, 겉보기에 같은 결론(둘 다 False)이 나온다고 그 주석을
  믿으면 안 된다(거짓양성 테스트 목록에 실제 상품명이 한 번도 없었던 게 그 간극을 가렸다).
  **[라운드 14 리뷰 F34 추가]** "경계 문자"를 한 집합으로 뭉치지 마라 — 절을 잇는 쉼표와
  인용을 여는 따옴표는 같은 부호 부류가 아니다. 라운드 13(F31-3)이 왼쪽 앵커의 경계 스킵을
  공백 하나에서 `_is_boundary_char` 전체로 넓혔는데, 그 집합엔 인용부호·괄호도 들어 있어
  발화 **첫 글자**가 인용부호이면 앵커가 그냥 통과해 `"'이어폰 찜 빼줘'"` 처럼 인용된
  상품명이 규칙 1로 실제 삭제됐다(실측, 파괴적). 파괴적 판정의 앵커에서 절 경계(쉼표·
  세미콜론·가운뎃점)와 인용·삽입 경계(따옴표·괄호·콜론)를 같이 건너뛰면 인용문이 명령이
  된다 — 앵커 전용의 **좁은** 절 경계 집합(`_UTTERANCE_CLAUSE_SEPARATORS`)을 따로 두고,
  다른 판정이 쓰는 넓은 `_is_boundary_char` 는 건드리지 않았다.
- 관련: #440 · #386(`app/agents/buyer/cart/wishlist.py` 되돌린 가드 이력) ·
  `app/agents/buyer/cart/negation.py::matches_pair_unnegated` ·
  `app/agents/buyer/cart/negation.py::matches_unnegated_left_bounded`(라운드 2 리뷰 F6) ·
  `app/agents/buyer/cart/negation.py::matches_unnegated_left_bounded_with_noun_ending`
  (라운드 3 리뷰 F8) · `app/agents/buyer/cart/intent_guard.py::has_wishlist_remove_evidence`
  (라운드 9 리뷰 F22 부터 규칙 2·3 전용 — 발화 전체 왼쪽 앵커 추가) ·
  `app/agents/buyer/cart/negation.py::_name_left_anchor_reachable`(라운드 9 리뷰 F22 신설
  당시 이름은 `_closed_prefix_anchor_end` — 발화 시작부터 닫힌 어휘만으로 head/표지/이름
  시작에 도달하는지 검사. `_ORDINAL_PREFIX`(화면 순번 "N번째")도 열거가 아니라 패턴으로 함께
  받는다. 라운드 13 리뷰 F33 이 규칙 1 전용이던 `_name_left_anchor_reachable` 과 이 함수를
  **하나로 합쳤다** — 예전엔 두 함수가 경계 조건도(공백 하나 vs `_is_boundary_char`) 어휘도
  달라서 "같은 앵커"가 아니었다. 라운드 14 리뷰 F34 가 경계 스킵을 다시 좁혀
  `_UTTERANCE_CLAUSE_SEPARATORS`(쉼표·세미콜론·가운뎃점 전용)만 건너뛰게 했다 —
  `_is_boundary_char` 자체는 그대로 두고 앵커 스캔에서만 좁혀 썼다) ·
  `app/core/config.py::wishlist_remove_prefix_words`(라운드 9 리뷰 F22) ·
  `app/agents/buyer/cart/negation.py::_noun_ending_match_end`(라운드 9 리뷰 F23, 어미 뒤
  존댓 보조사 "요"를 종결로 인정) ·
  `app/agents/buyer/cart/negation.py::matches_name_unnegated_as_command`(라운드 10 리뷰 F26
  신설, 규칙 1 전용 — 라운드 11(F28)부터 규칙 2·3 과 같은 전체 왼쪽 앵커(`known_words`)를
  받는다. `remove.py` 는 그대로 `matches_name_unnegated` 를 쓴다) ·
  라운드 11 리뷰 F28 신설 당시엔 규칙 1 전용으로 갈라져 있었고, **다른 등록 이름도 사슬로
  통과시킨다**는 점이 위 함수와 달랐다 — "이어폰이랑 케이스 찜 빼줘"의 "케이스"가 자기 몫의
  앵커를 통과하려면 필요하다. 라운드 12(F30)는 앵커 어휘를 "닫힌 접두"에서 head·tail 계열·
  부정 표지까지 넓히고, 등재 안 된 동사 어간을 뒤따르는 부정 표지로 흡수하는 catch-up 을
  추가했으나, 라운드 13 리뷰 F31 이 그 catch-up 을 **삭제**했다(위 F31·F33 문단 참조) —
  앵커 스캔 전용 어간 목록(`wishlist_remove_action_stems`)으로 대체하고, 규칙 2·3 의 옛
  `_closed_prefix_anchor_end` 호출부를 전부 이 함수로 옮겨 **규칙 1·2·3 이 하나의 함수만
  쓰게 했다**) ·
  `app/agents/buyer/cart/negation.py::has_terminated_name_tail`(라운드 11 리뷰 F28 신설 —
  다중 이름 사슬의 진짜 tail 이 어디서도 종결에 성공하지 못하면 전역으로 막는 게이트) ·
  `app/agents/buyer/screen_reference.py::mentions_screen_reference`(라운드 11 리뷰 F29,
  `mentions_screen_position` 개명·확장 — 좌표·순번 정규식 **또는**
  `settings.screen_deictic_markers`) ·
  `app/core/config.py::utterance_quote_open_chars`(라운드 10 리뷰 F26) ·
  `app/agents/buyer/cart/wishlist.py::_resolve_wishlist_remove_target`(라운드 10 리뷰 F27,
  `screen_refused` 파생값을 `screen_position_mentioned`·`screen_resolved` 원자 둘로 분리 —
  규칙 2 는 `mentioned and not resolved`, 규칙 3 은 `mentioned` 단독으로 건너뛴다) ·
  `app/agents/buyer/graph.py` `corrected_to_wishlist_remove`(라운드 3 리뷰 F9, 라운드 9 리뷰
  F24 로 `has_wishlist_remove_evidence` 게이트 추가) ·
  `app/agents/buyer/cart/graph.py::stream_cart_add` 의 `wishlist_remove` 위임(라운드 9 리뷰
  F24, 같은 게이트) ·
  `app/core/config.py::utterance_action_verb_suffixes`(라운드 4 리뷰 F10, 라운드 5 리뷰 F14 로
  종결 검사 보강, 라운드 6 리뷰 F16 으로 1인칭 의지형 제거) ·
  `app/agents/buyer/cart/negation.py::tail_is_command`(라운드 6 리뷰 F16, 연결어미·인용 조사
  구조 판정 — `wishlist_remove_hedge_markers`(라운드 5 리뷰 F13)를 대체하며 삭제. 라운드 7
  리뷰 F19 로 문장 종결 부호 규칙 추가. 라운드 8 리뷰 F20 부터는 **라우팅 전용**(기본-허용)이고
  근거 판정은 `tail_terminates_utterance` 를 대신 쓴다) ·
  `app/agents/buyer/cart/negation.py::tail_terminates_utterance`(라운드 8 리뷰 F20, 기본-거부 —
  `matches_unnegated_left_bounded`·`matches_unnegated_left_bounded_with_noun_ending`·
  `matches_pair_unnegated` 의 `require_termination=True` 로 건다) ·
  `app/core/config.py::utterance_hedge_connectives`·`utterance_quotative_markers`(라운드 6
  리뷰 F16) · `app/agents/buyer/cart/wishlist.py::_resolve_wishlist_remove_target`
  `screen_refused`(라운드 4 리뷰 F12, 라운드 5 리뷰 F15 로 2선 방어 위임까지 확장, 라운드 6
  리뷰 F17 로 규칙 1 게이트 추가·F18 로 pending 포함. 라운드 8 리뷰 F21 로 대리값 대신
  `app/agents/buyer/screen_reference.py::mentions_screen_position` + `cart_intent.product_id`
  직접 신호로 재계산, 라운드 9 리뷰 F25 로 `cart_intent.product_id` 대신 해소 결과 자체
  (`screen_resolved`)로 다시 교체) · `tests/unit/test_wishlist_remove_resolution.py` ·
  `tests/unit/test_screen_context.py`
## [2026-08-08] 채택 근거로 인용할 산출물을 리포 밖(tmp)에 두면 잃는다
- 증상: #443/#465 categoryQueries 후보(C5) 스크리닝(부분 셀 11개, `namedCategoryHasLeg` 46/48
  로 관측)의 산출물을 리포 밖(스크래치패드/tmp)에 썼다. 그 뒤 런타임이 재시작되면서 스크리닝
  산출물이 **전부 소실**됐고, 채택 판정을 다시 검증하려던 다음 라운드는 "46/48" 이라는 숫자를
  런 보고에서 옮겨 적은 값으로만 인용할 수 있었다 — 산출물로 재검증할 방법이 없어졌다.
- 원인: PR·README·주석이 인용하는 수치의 근거 파일이 커밋 대상이 아닌 곳(tmp)에만 있으면, 그
  근거는 세션·런타임 생명주기에 종속된다. 최종 채택/기각 판정은 나중 세션이 이어받는 경우가
  많은데, 그 세션은 tmp 를 볼 수 없다.
- 규칙: **PR·README·주석이 인용할 산출물의 원본은 리포 안(또는 최소한 커밋되는 경로)에 둔다.**
  스크리닝처럼 가벼운 중간 산출물이라도 그 수치가 최종 판정(채택/기각)의 근거로 쓰일 가능성이
  있으면 tmp 에 두지 마라. tmp 에 둘 수밖에 없었다면, 그 수치를 인용하는 문서에 "산출물 미보존
  — 재검증 불가" 를 명시해 없는 근거를 있는 것처럼 쓰지 않는다.
- 관련: #443 · #465 · `evals/intent_probe/baselines/fast-2026-08-08-443-before-1/README.md` §5

## [2026-08-08] 부분 셀 스크리닝의 이득이 전체 런에서 재현되지 않았다 — 채택은 전체 런 2회 + 사전 등록 문턱으로만
- 증상: #443 categoryQueries 문면 후보(C5, "사용자가 말한 상품군은 넓어도 그대로 담으세요"
  예시 한 줄)를 부분 셀(11개) 스크리닝으로 재니 `namedCategoryHasLeg` 46/48 로 강한 개선처럼
  보였다. 같은 프롬프트(sha12 `6f64dcbd43d4`, `--dump-prompt` 로 대조 확인해 스크리닝과 채택
  후보가 바이트 단위로 동일함을 확인했다)로 전체 런(N=8×6셀=48표본) 2회를 다시 재니 35·38/48
  로 나와 사전 등록 문턱(after 두 런 모두 before 최댓값(36) 이상 + 평균 상승 ≥ +4/48)을
  통과하지 못했다. 미변경 프롬프트(before, sha12 `865ed6fd771e`)조차 38·33·36·33 으로 흔들려
  이 축의 런간 폭은 하네스 문서가 말하는 "축당 ±2"보다 훨씬 크다(≈5 이상).
- 원인: **"출고물 == 측정물"을 확인했는데도 수치가 갈리는 것은 배선 사고가 아니라 분산이다.**
  N=8×6셀(48표본)은 이 축의 런간 표준 편차(≈5/48 ≈ 10%p)에 비해 +2 크기의 효과를 노이즈와
  가를 만큼 크지 않다. 부분 셀 스크리닝(11개)은 표본이 더 작아 분산이 더 크므로, 우연히 좋은
  쪽으로 튄 값을 "채택 근거"로 오인하기 쉽다.
- 규칙: 부분 런은 후보 **선별**(여러 문면 초안 중 유망한 것을 추리는 용도)에만 쓰고, **채택
  판정은 반드시 전체 런 2회 + 사전 등록 문턱으로** 한다. 축의 런간 폭이 효과 크기와 비슷하거나
  크면(이번처럼 ≈5 대 +2) `--n` 을 키운 전체 런을 추가로 돌려 신뢰구간을 좁히기 전에는 채택하지
  않는다.
- 관련: #443 · #465 · #463(반대 방향 비용 축) ·
  `evals/intent_probe/baselines/fast-2026-08-08-443-before-1/README.md` §0 ·
  `app/agents/buyer/recommendation/decompose.py` `_SYSTEM` 아래 기각 주석
## [2026-08-08] 낡은 코드 주석이 **회귀의 심각도 판단을 뒤집었다** — 심각도 근거는 주석이 아니라 정본에서 가져와라
- 증상: #430 이 `screenExactPick` 회귀를 만났을 때, `decompose.py` 의 screen 절 주석
  ("FE 는 아직 screen 을 보내지 않으므로 그쪽이 절대다수 경로다")을 근거로 **"휴면 경로라 심각도가
  낮다"** 는 판단이 한 라운드 동안 유지됐다. 정본을 직접 열어 보니 틀렸다 —
  `docs/api-spec.md` §3.1 은 "**현재 UI로 실제 오는 값은 3종**이다 — `chat`(구매자 인기상품
  패널)·`seller_orders`·`seller_products`"라고 적고 있고, §3.1 은 **v0.20.3(2026-08-04)에서
  `screen` 수신 구현으로 개정**됐다. 즉 살아 있는 경로였고, 그 주석은 개정 전에 쓰여 그대로 남아
  있었다.
- 원인: 계약이 개정될 때 **정본(`docs/api-spec.md`)은 갱신되고 그 계약을 소비하는 코드의 산문
  주석은 갱신되지 않는다.** 주석은 테스트도 린트도 지키지 않으므로 낡아도 아무것도 붉어지지
  않는다. 그리고 주석은 코드 옆에 있어서 **정본보다 먼저 읽히기 때문에** 틀렸을 때 비용이 크다 —
  이번엔 "고칠까 말까"를 가르는 심각도 판단의 입력이 됐다.
- 규칙: **회귀의 심각도를 "그 경로는 안 쓰인다"로 낮추려거든 근거를 코드 주석이 아니라 정본
  (`docs/api-spec.md` 해당 § + 그 §의 개정 이력 행)에서 가져와라.** 주석은 단서일 뿐 근거가
  아니다. 그리고 계약 § 을 개정하는 PR 은 그 § 을 인용하는 코드 주석을 `grep` 으로 찾아 함께
  고쳐라(`grep -rn "§3.1" app/` 수준이면 충분하다).
- 관련: #430 · #118 · `app/agents/buyer/recommendation/decompose.py` screen 절 주석(이 항목과
  같은 커밋에서 정정) · `docs/api-spec.md` §3.1 `pageType` 어휘 절 · v0.20.3(2026-08-04) 개정 행

## [2026-08-07] **병합이 측정물을 바꾼다** — 프롬프트를 고치는 PR 은 병합 직후 `_SYSTEM` sha 를 다시 확인해야 한다
- 증상: #430 은 `decompose._SYSTEM` 을 고치고 전/후 각 2런으로 채택 판정까지 끝낸 뒤 PR 을 열고
  `origin/dev` 를 병합했다. 그런데 **#386(PR #441, 커밋 `3547e43`, `wishlist_view` 의도 신설)이
  같은 `_SYSTEM` 에 548자를 더해 놓았다** —
  `_SYSTEM` sha 가 `81e3770e1340`(내가 잰 판) → `f99a98867e4a`(병합 결과 = 실제로 출고될 판)로
  바뀌었다. 즉 **채택 기준 "출고물 == 측정물"이 코드 한 줄 안 고치고 병합만으로 깨졌다.**
  재측정해 보니 형식 문제가 아니었다: `falseAlarmRate` 가 1.9 → 3.8 → 4.8% 로 **단조 상승**해
  사전 등록 상한(3.6%)을 3런 중 2런에서 넘겼다.
- 원인: 프롬프트는 **레인 간 공유 자산**인데 diff 상으로는 서로 다른 줄을 건드리므로 충돌이
  나지 않는다. Git 은 조용히 병합하고, 두 레인이 각자 자기 축만 재고 넘어가면 **합쳐진 문면을
  아무도 재지 않은 채** 출고된다. 이 리포는 이미 "문면을 더하면 다른 축이 깎인다"를 실측해
  뒀는데(#430 자신이 +268자로 `screenExactPick` −3.5 를 쟀다), 그 지식이 **병합**에는 적용되지
  않고 있었다.
- 규칙: **`_SYSTEM`(또는 공유 프롬프트)을 건드리는 PR 은 병합 직후 sha 를 다시 계산하고, 채택
  근거로 쓴 산출물의 `run_manifest.prompt.sha256` 과 대조하라.** 다르면 그 산출물은 더 이상
  근거가 아니다 — **재측정한다.** PR 을 열기 전에 `git log origin/dev -- <프롬프트 파일>` 로
  다른 레인이 같은 파일을 건드렸는지 먼저 보는 것이 더 싸다. 그리고 채택 산출물 디렉터리에는
  잰 프롬프트의 sha 를 반드시 적어 둬라 — 그래야 다음 사람이 대조할 수 있다.
- 규칙 2(귀속): **병합이 무엇을 바꿨는지 귀속할 때 `origin/dev` 의 최신 커밋을 범인으로 적지
  마라 — `git log <base>..origin/dev -- <파일>` 로 그 파일을 실제로 건드린 커밋을 찾아라.**
  이 항목의 초판은 병합 시점 `origin/dev` 의 최신 커밋(#428, `af32255`)만 보고 귀속했다가,
  실제 원인(**#386**, PR #441, 커밋 `3547e43`)과 다른 번호를 문서·커밋 메시지·후속 이슈 4건에
  퍼뜨렸다. `git log 7272822..origin/dev -- app/agents/buyer/recommendation/decompose.py` 는
  커밋을 **하나**만 돌려준다 — 그 한 줄이면 끝났을 일이다. "최신 커밋"과 "그 파일을 바꾼 커밋"은
  다른 질문이고, 귀속은 **후자**로만 답해야 한다.
- 관련: #430 · #386(PR #441, 커밋 `3547e43`) ·
  `evals/underspecified_probe/baselines/fast-2026-08-07-430-merged-{1,2,3}/`(병합판 3런 —
  이 인과의 근거) · `.../fast-2026-08-07-430-after-1/README.md`

## [2026-08-07] "최소 델타면 안전하다"는 틀렸다 — **10자가 3개 축을 −3 시켰다**
- 증상: 위 병합 사고를 수습하려고 비움 트리거의 단서 목록에 브랜드·색상을 더했다 —
  `의미(종류·용도·상황·목적)` → `단서(종류·용도·상황·목적·브랜드·색상)`, 순증 **10자**
  (`_SYSTEM` 7828 → 7838자, 0.13%). 목표는 달성했다(`falseAlarmRate` 1.9·2.9% 로 상한 복귀).
  그런데 같은 픽스처에서 **이 10자만 다른** `evals/intent_probe` 대조에서 `categoryClear` 가
  **31·31 → 28·28**, `demonstrative`·`mainIntent` 도 각 **−3** 이었다. 팔 내부 분산이 0이라
  노이즈로 볼 수 없다.
- 원인: 델타의 **크기**가 아니라 **무엇을 규칙의 술어에 넣었는가**가 문제다. 색상·브랜드는
  `filters.color`/`filters.brand` 로 가는 축인데 그것을 비움 트리거의 **단서**로 격상시키면,
  그 어휘가 등장하는 다른 판정(카테고리 리셋·지시대명사 해소)도 함께 흔들린다. 같은 −3 이 세
  축에 동시에 찍힌 것이 "한 원인이 여러 축에 비친다"는 신호다. 이 캠페인은 앞서 **길이 가설도
  반증**했다(+268자 → +161자 → +110자로 줄여도 `screenExactPick` 이 회복되지 않았고, 오히려
  **더 긴** +143자 판이 가장 좋았다). 즉 문면 비용은 길이의 함수가 아니다.
- 규칙: **프롬프트 델타의 안전성을 글자 수로 판단하지 마라.** 규칙의 술어에 새 어휘를 넣을
  때는 "그 어휘가 이 프롬프트의 **다른 판정**에도 등장하는가"를 먼저 찾고, 등장하면 그 축들을
  **같은 표에서 함께 재라.** 그리고 델타가 아무리 작아도 **타축 측정을 생략하지 마라** —
  10자로 3축이 움직였다.
- 관련: #430 · `evals/intent_probe/baselines/fast-2026-08-07-430-v6-{merged,adopted}-*/`
  (같은 픽스처·10자만 다른 귀속 대조) · [2026-08-07] 「새 프롬프트 규칙의 비용은 길이가 아니라
  기존 규칙과의 문면 충돌이다」 항목(길이 가설 반증 궤적)

## [2026-08-07] "플래그 뒤에 있는 줄 알았던" 산출 신호에 **플래그 없는 두 번째 소비자**가 있었다
- 증상: #430 은 `decompose` 프롬프트를 고쳐 `semantic_query_is_fallback` 이 정직해지게 만든
  작업이고, 이슈·패킷 모두 그 효과를 **`underspecified_reask_enabled`(기본 False)로 게이트된
  #336 되물음**으로만 서술했다. 그런데 `grep semantic_query_is_fallback app/` 를 돌리면
  소비자가 둘이다 — `underspecified.is_underspecified_turn`(플래그 있음, 오늘 무동작)과
  **`no_condition.is_no_condition_turn`(#162, api-spec §4.17 — 플래그가 없다. 오늘 켜져 있다)**.
  즉 "플래그를 켜지 않았으니 운영 동작은 그대로"라는 전제가 틀렸다. 산출물로 재보니
  `semanticQueryIsFallback=true` 표본이 1/240 → 163~164/240 이었고, 그중 더 엄격한
  `is_no_condition_turn` 조건까지 통과하는 `no_condition` 슬라이스가 39~40/40 이었다.
- 원인: 게이트는 **신호 생산자**가 아니라 **소비자마다** 따로 걸려 있다. 신호를 고치면
  게이트 없는 소비자는 즉시 영향을 받는데, 이슈 제목·설계 문서가 한 소비자만 이름 붙여
  부르면 나머지가 시야에서 사라진다. 이번엔 결과가 좋은 방향(#162 가 문서상 "계약 위반"이라
  부르던 무필터 I-1 호출이 멈춘다)이었지만, 그건 운이지 설계가 아니다.
- 규칙: **산출 신호(플래그·불리언·판정 축)를 고칠 때는 `grep` 으로 소비자를 전부 세고, 각
  소비자의 게이트 유무를 표로 적어라.** "이 변경은 플래그 뒤에 있다"는 주장은 소비자 목록
  없이는 하지 마라. 그리고 게이트 없는 소비자가 있으면 그 영향 규모를 **같은 산출물에서
  수치로** 재서 PR 본문에 전용 절로 싣는다 — 사람이 머지 판단할 때 놓치면 안 되는 사실이다.
- 관련: #430 · #162 · #336 · `app/agents/buyer/recommendation/no_condition.py` ·
  `app/agents/buyer/recommendation/underspecified.py` ·
  `evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`

## [2026-08-07] 안 먹히는 프롬프트 지시는 문구가 나빠서가 아니라 **같은 절 뒤쪽의 무조건 긍정 명령에 져서**다
- 증상: `decompose._SYSTEM` 은 `- recommend:` 불릿 **첫 문장**에서 이미 "정확한 수치 제약은
  filters 에 넣고 semanticQuery 로 근사하지 마세요"라고 지시하고 있었는데, 실측
  (`evals/underspecified_probe` 2026-08-06 기준선)은 정확히 그 반대였다 —
  `"5만원 이하로 아무거나 추천해줘"` → `semanticQuery = "5만원 이하 아무거나"` 가 **8/8**.
  #430 은 처음에 이 문장을 고치는 것을 「할 일」로 받았다.
- 원인: 같은 불릿의 **마지막 문장**이 `"semanticQuery 는 동의어·상위어를 함께 담은 의미 중심
  자연어로 쓰세요"` 라는 **무조건 긍정 명령**이었다. 앞의 금지형("~하지 마세요")은 "대신 무엇을
  쓰라"가 없어 대안이 되지 못하고, 뒤의 긍정 명령이 "이 필드는 항상 풍부하게 채운다"로 읽힌다.
  **모델이 어느 문장을 읽고 있었는지가 산출물에 직접 찍혀 있었다** — `semanticQuery` 산출로
  `'의미 중심 자연어'`(기준선 `under-nc-0003`) · `'무엇을 살지에 대한 의미 중심의 일반 추천'`
  (후보 런)처럼 **그 문장의 문면을 그대로 에코**한 표본이 나왔다.
  실측이 이 인과를 두 방향으로 뒷받침한다: ① 금지형 문장은 **손대지 않은 채** 같은 불릿
  **끝에** "발화에 찾는 상품의 의미가 하나도 없으면 빈 문자열로 두라"를 덧붙이자 `missRate`
  99.1% → 17.0%(수치 에코도 함께 줄었다). ② 반대로 금지형 문장을 긍정형으로 **재작성**한
  두 후보는 수치 에코를 줄이지 못하고 primary 를 깎았다(17.0% → 23.2% / 48.2%).
  즉 수치 에코는 그 문장의 어휘 문제가 아니라 **"이 필드는 비울 수 없다"가 유효 규칙이었기
  때문**이고, 비울 수 있게 하자 부수적으로 사라졌다.
- 규칙: 프롬프트 지시가 "이미 있는데 안 먹힌다"면 **그 문장을 고치기 전에** 같은 절 뒤쪽에
  같은 필드를 **무조건 채우라고 시키는 문장**이 있는지부터 찾아라. 그리고 실측 산출물에서
  **프롬프트 문면이 그대로 에코된 표본**을 찾아라 — 그게 "모델이 어느 문장을 읽고 있는가"의
  직접 증거이고, 산문 추측보다 강하다. 안 먹히는 지시의 수정은 **채택 여부와 진단이 별개**다:
  진단해서 원인을 적되, 재작성이 primary 축을 깎으면 반려하는 것이 근거 있는 결론이다.
- 관련: #430(PR 진행 중) · `app/agents/buyer/recommendation/decompose.py::_SYSTEM` ·
  `evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`(후보 선별표)

## [2026-08-07] 새 프롬프트 규칙의 비용은 **길이가 아니라 기존 규칙과의 문면 충돌**이다 — 길이 가설을 세우고 3후보로 반증했다
- 증상: #430 이 `decompose._SYSTEM` 에 "찾는 상품의 의미가 **발화에** 없으면 `semanticQuery` 는
  빈 문자열" 규칙을 넣자 목표 축은 크게 좋아졌는데(`missRate` 99.1% → 약 10%)
  `evals/intent_probe` 의 **`screenExactPick` 이 32/32·32/32 → 30·27** 로 깎이고 진단
  `screenOutOfListConfirmCount`(화면 목록 **밖** productId 를 확정하려 든 횟수)가 0·0 → 2·5 로 올랐다.
- 반증 궤적(이게 이 항목의 핵심이다): 처음 세운 가설은 **"긴 프롬프트에 문면을 더한 것 자체가
  비용(주의 경쟁)"** 이었다. 그래서 추가 문면을 줄여 가며 3후보를 각 2런씩 쟀다 —
  +268자 `screenExactPick` 평균 **28.5** → +161자 **30.0** → +110자(가장 짧음) **30.0**.
  **가장 짧은 판이 나아지지 않았다 → 길이 가설은 반증됐다.**
- 원인(반증 뒤에 선 가설): `_SYSTEM` 에는 이미 "상품명 없는 지시대명사는
  PRIOR_FILTERS.semanticQuery 또는 LAST_RECOMMENDATIONS 맥락의 **상품**을 가리킵니다"가 있다.
  screen 셀의 발화("이거 담아줘"·"3번째 거")는 **발화 자체에는 상품 의미가 없고 맥락에만 있다** —
  두 규칙의 교집합이 정확히 이 입력이다. 모델이 새 규칙의 "발화에 없으면 비워라·지어내지 마라"를
  **"맥락에서 끌어와 해소하는 것도 하지 마라"** 로 일반화하면 관측(`screenExactPick` 하락 +
  목록 밖 확정 시도 증가)과 부합한다. 트리거를 **"발화에도 PRIOR_FILTERS·LAST_RECOMMENDATIONS·
  SCREEN 맥락에도 없으면"** 으로 좁힌 판(+143자 — 반증된 최단판보다 **길다**)이 31·31·29(평균
  30.33)로 가장 나았다. 좁힘은 회귀 회피용 임기응변이 아니라 **코드 의미와 일치**한다 —
  `semantic_query_is_fallback = not (llm_sq or cat_signal or prior_sq)` 이라 맥락이 있으면
  플래그는 어차피 False 이고 판정은 `prior is None` 첫 턴 한정이다. 넓게 쓴 문면이 코드보다
  넓게 말하고 있었던 것이다. 다만 **완전히 회복되지는 않았다**(잔여 −1.67) — 약 −2 는 이 규칙에
  내재하는 비용으로 보인다.
- 규칙: **긴 프롬프트에 규칙을 더할 때는, 그 규칙이 기존 규칙과 겹치는 입력이 무엇인지 먼저
  찾고 그 교집합에서 두 규칙이 서로 반대를 지시하지 않는지 확인한다.** 새 규칙의 트리거는
  **코드가 실제로 보는 조건과 같은 넓이**로 써라 — 코드보다 넓게 쓰면 코드가 안 보는 입력까지
  끌려간다. 회귀가 나면 "문면을 줄이면 낫겠지"부터 시도하지 말고 **줄여 보고 반증하라**(싸다).
  그리고 회귀 축은 **하위축 합인지 독립 축인지** 먼저 가려라 — `screenResolution` 은
  `screenExactPick`+`screenNoHallucination`+`screenReask` 의 합이라 "두 축이 깎였다"로 쓰면
  같은 사실을 두 번 센 것이 된다.
- 곁가지(같이 잰 것): 같은 취지를 **상단 JSON 스키마 줄**에만 적은 판은 `missRate` 24.1%,
  **규칙 절 불릿**으로 적은 판은 17.0% 였다 — 스키마 줄은 값의 **형식**을 말하는 자리라 행동
  규칙의 무게가 실리지 않는다. 또 같은 취지를 두 군데에 서로 다른 말로 적으면 **인용 가능한
  문면이 두 개**가 돼 모델이 빈 문자열 대신 그 말을 적는다(실측 산출 `'상품 의미'` ·
  `'상품의 의미를 추출하지 못해 빈 문자열'`, `missRate` 17.0% → 48.2%). 규칙은 **행동을
  지시하는 절에 한 군데만** 적는다.
- 관련: #430 · `evals/underspecified_probe/baselines/fast-2026-08-07-430-after-1/README.md`
  (후보 선별표 — sha12 로 재현 가능) ·
  `evals/intent_probe/baselines/fast-2026-08-07-430-after-1/README.md`(타축 대조표) ·
  [2026-08-07] 「안 먹히는 프롬프트 지시는…」 항목

## [2026-08-07] `evals/intent_probe --prompt` 는 screen 축을 **재지 못한다** — 후보를 리포에 넣고 재야 한다
- 증상: #430 에서 `--prompt <후보파일>` 로 before 팔을 잰 뒤 after(리포 `_SYSTEM`)와 대조했더니
  screen 축이 어긋났다. 같은 before 프롬프트인데 진단 `screenPromptLayerHitCount` 가
  `--prompt` 런 16·18 대 리포 런 21·28 로 갈렸다 — 프롬프트가 같은데 값이 다르면 계측이 틀린 것이다.
- 원인: `SystemPromptOverrideLLM`(`evals/intent_probe/client.py`)은 통과하는 decompose
  `complete` 의 system 을 후보 텍스트로 갈아끼우는데, **screen 이 실린 셀은 프로덕션에서
  `_SYSTEM_WITH_SCREEN`**(= `_SYSTEM` + `_SCREEN_CART_RULE`)을 쓴다. 오버라이드가 그 문면까지
  평평한 후보 텍스트로 덮어 화면 지목 규칙이 통째로 빠진 채 측정된다. `PASSTHROUGH_SYSTEMS` 는
  보조 분류기만 보호하고 이 변형은 보호하지 않는다.
- 규칙: **screen 축(`screenExactPick`·`screenResolution`·`screenNoHallucination`·`screenReask`
  와 두 screen 진단)이 걸린 후보는 `--prompt` 로 재지 마라** — 후보를 리포 `_SYSTEM` 에 넣고
  `--prompt` 없이 돌려 `prompt.source == "repo:_SYSTEM"` 으로 만든다(before 팔도 파일을 되돌려
  같은 방식으로). `--prompt` 런을 **통째로 버리지는 마라** — 비-screen 축에서는 프롬프트
  문자열이 리포 판과 같아 여전히 유효하다. 어느 축에 어느 런을 썼는지 산출물 README 에 적어라.
- 관련: #430 · `evals/intent_probe/README.md` 「⚠️ `--prompt`/`--prompt-rev` 런은 screen 축을
  재지 못한다」절 · `evals/intent_probe/baselines/fast-2026-08-07-430-after-1/README.md`
## [2026-08-07] 변이 검증 원복에 `git checkout --` 을 쓰면 **미커밋 신규 테스트가 함께 날아간다**
- 증상: 수정이 효력 있는지 보려고 코드를 일시 변이시킨 뒤 `git checkout -- tests/...` 로 되돌렸다.
  그런데 그 테스트 파일에는 **아직 커밋 안 한 신규 테스트**가 들어 있었고, checkout 이 HEAD 상태로
  되돌리면서 통째로 삭제됐다. 그대로 커밋했으면 "회귀 테스트를 붙였다"는 커밋 메시지와 달리
  **코드 수정만 들어가고 테스트는 없는 커밋**이 나갈 뻔했다(`git show --stat` 이 파일 1개만
  보여줘서 알아챘다). 복원하며 이미 있던 테스트를 다시 붙여 **중복까지 만들었다**.
- 원인: 변이 검증의 원복 수단으로 **작업 트리 기준(checkout)** 을 썼다. 변이는 "지금 상태"에서
  일시적으로 벗어났다 돌아오는 것인데, checkout 의 기준점은 "지금"이 아니라 HEAD 다. 미커밋
  변경이 있으면 두 기준이 어긋난다.
- 규칙: 변이 검증 원복은 **파일 사본**으로 한다(`cp file bak` → 변이 → `cp bak file`). git 명령을
  원복에 쓰지 않는다. 그리고 변이 검증이 끝나면 **`git status`·`git show --stat` 으로 의도한
  파일이 전부 들어갔는지 확인**한 뒤 커밋한다 — 특히 "테스트를 추가했다"고 적은 커밋에 테스트
  파일이 없으면 그 자체가 신호다.
- 관련: 이슈 #356 / PR #410, `tests/unit/test_profile_resolver.py`

---

## [2026-08-07] 같은 계열 지적이 반복되면 **그 건이 아니라 계열을 막는 가드**를 세운다
- 증상: PR #410 리뷰가 20건 나왔는데 절반이 두 뿌리였다. ① **LLM 출력을 경계에서 안 막음**
  (`predicate`·`source` Literal → `salience` 범위 → `anchor_phrase` 길이 → 상품 id 표기 →
  숫자 크기, 6건) ② **낡은 값과 새 값을 같은 자로 비교**(저장 상한 `evidence_refs` 로 요약 판정 →
  지문의 `resolution`·`evidence_*` → 이월 tombstone 의 confidence 박제, 4건). 매번 **지적된 한
  건만** 고쳐서 다음 인스턴스를 리뷰가 계속 찾아냈다 — 두더지잡기였다.
- 원인: 수정 단위를 "리뷰가 가리킨 줄"로 잡았다. 같은 계열이 두 번 나온 시점에 **그 계열의 표면
  전체**를 훑었어야 했는데, 매번 국소 수정으로 닫으니 리뷰만이 전수 조사 역할을 했다.
- 규칙: **같은 계열 지적이 2회 이상이면 국소 수정을 멈추고 (a) 그 계열의 표면을 전수 정리하고
  (b) 새 인스턴스가 자동으로 걸리는 가드를 만든다.** 가드는 "표에 적은 것만 검사"가 아니라
  **산출물 전체를 훑는 불변식**이어야 한다 — 이번 ②의 가드
  `{e.decay_evaluated_at for e in document.edges} == {now}` 는 어떤 경로로 만들어진 edge 든
  걸리지만, ①의 적대적 입력 표는 표에 없는 새 필드를 못 잡는다(그건 가드가 아니라 회귀 테스트다).
  전수 불변식을 못 세우는 계열은 그 한계를 **명시**하고 넘어간다.
- 덧: 리뷰 대응에는 **멈추는 기준**도 필요하다. 재현되고 영향 있으면 고치고, 잠재+저비용이면
  고치고, 설계 논쟁이거나 이미 근거를 적어둔 지점의 재지적이면 **회신만 하고 코드는 두는 것**이
  정당한 마무리다(이번 `product` `verified=False` 가 그 사례).
- 관련: `app/agents/profile/graph_merge.py::_carried_tombstones`,
  `tests/unit/test_profile_graph_merge.py::test_decay_clock_is_one_snapshot_per_batch`,
  `tests/unit/test_profile_resolver.py::test_numeric_labels_are_bounded_by_domain_range`,
  이슈 #356 / PR #410

---

## [2026-08-07] 선례를 옮길 때는 **tier·모델이 같은지** 먼저 본다 — 값이 모델 종속이면 그대로 400 이 된다
- 증상: #356 델타 추출이 `max_tokens=800` 하드코딩 탓에 출력 예산 소진으로 죽는 걸 프로브가 잡아,
  #325(enrichment) 선례를 그대로 옮겨 `max_tokens` 상향 + `reasoning_effort="minimal"` 고정을
  넣었다. 그러자 프로브가 **8/8 전부 실패**했다 — `400 Unsupported value: 'reasoning_effort'
  does not support 'minimal' with this mode`. #325 는 **fast tier(gpt-5-nano)** 이고 델타 추출은
  **smart tier(gpt-5.6-luna)** 라, 같은 문자열이 한쪽에선 되고 한쪽에선 거절된다. 이 모델은
  이미 `openai_tool_reasoning_incompatible_models` 에 올라 있었는데 확인하지 않았다.
- 원인 둘: (1) "같은 함정 → 같은 대응"으로 선례를 통째 복사했다. 함정(출력 예산)은 같아도 대응
  일부(effort 값)는 **모델 능력에 종속**이라 이식 대상이 아니었다. (2) 애초에 effort 고정은
  **측정된 문제를 푸는 데 필요 없었다** — 실패 원인은 예산이었고 `max_tokens` 만으로 0건이 됐다.
  필요 없는 노브를 얹었다가 그 노브가 전부를 깨뜨렸다.
- 규칙: 다른 이슈의 대응을 옮길 때는 **어떤 값이 모델·tier 종속인지 먼저 가른다**(`max_tokens`
  는 이식 가능, `reasoning_effort`·모델 id·tool 지원 여부는 아니다). 그리고 **측정이 요구하지
  않은 노브는 넣지 않는다** — 측정으로 확인한 최소 변경부터 적용하고, 그것으로 해결되면 거기서
  멈춘다. 이번엔 노브를 빼자 테스트 fake 5개 파일 수정도 통째로 불필요해졌다.
- 관련: `app/core/config.py::profile_delta_max_tokens`, `app/agents/profile/builder.py`,
  `openai_tool_reasoning_incompatible_models`, 이슈 #356 / #325 / PR #410

---

## [2026-08-07] 프로브가 프로덕션 호출을 **복제**하면, 이미 고친 결함을 계속 "실패"로 보고한다
- 증상: `scripts/probe_delta_prompt_356.py` 가 `llm.complete(..., max_tokens=800)` 로 프로덕션
  호출을 베껴 두고 있었다. 그 800 이 원인이라 `builder` 쪽을 config 주입(2048)으로 고쳤는데,
  프로브를 다시 돌려도 **똑같이 2건 실패**로 나왔다. 프로브가 자기 하드코딩을 계속 쓰고 있어서다.
  "고쳤는데 왜 그대로지"로 한참 헤맬 뻔했다.
- 원인: 프로브의 목적은 **프로덕션이 무엇을 하는지 재는 것**인데, 호출 파라미터를 프로덕션에서
  읽지 않고 손으로 옮겨 적었다. 그 순간 프로브는 프로덕션이 아니라 "예전의 프로덕션"을 잰다.
- 규칙: 계측 스크립트는 **프로덕션 코드를 부르거나 프로덕션 설정을 읽는다.** 파라미터·정규식·
  임계를 스크립트에 베껴 쓰지 않는다(같은 이유로 이 프로브의 밴드 라벨 검사도 정규식을 복제하지
  않고 `_resolve_band` 를 직접 부른다). 베낀 값이 하나라도 있으면 그 값이 갈리는 순간 프로브
  결과는 근거가 아니라 오해가 된다.
- 관련: `scripts/probe_delta_prompt_356.py::run_prompt`·`_band_accepted`,
  `app/agents/profile/builder.py::generate_session_delta`, 이슈 #356 / PR #410

---

## [2026-08-07] 명세의 "예: A vs B"를 목록으로 옮기면, 예시가 규칙이 되어 나머지가 조용히 빠진다
- 증상: #356 `_resolve_conflicts` 가 상충 쌍을 `_CONFLICTING = {{"likes", "avoids"}}` 하나로
  하드코딩했다. 그런데 `resolver._POSITIVE_PREDICATE` 는 kind 마다 다른 긍정을 만든다
  (`priceBand`·`ratingBand`·`attribute` → `prefers`, `situation` → `interestedIn`). 그래서
  "3만원대를 선호한다" + "3만원대는 싫다" 가 **둘 다 `active` 로 공존**하고, `_summary_input` 은
  non-active 만 거르므로 모순된 두 fact 가 요약 LLM 입력에 **함께** 들어갔다(실측 재현:
  `['30000-50000 를 선호한다', '30000-50000 를 싫어한다']`). 7개 kind 중 4개가 구멍이었다.
  Claude PR 리뷰가 잡았다.
- 원인: SPEC REQ-PGRAPH-018 이 "상충하는 관계(`likes` vs `avoids` 같은 node 대상)"라고 **예시**로
  적은 것을, 구현이 **열거해야 할 목록**으로 읽었다. 예시를 자료구조로 옮기는 순간 그 예시가
  규칙이 되고, 예시에 없던 경우는 "빠뜨렸다"가 아니라 "원래 대상이 아니다"처럼 보인다.
- 규칙: 명세가 "예: A" 로 쓴 것을 코드에 옮길 때는 **A 를 등록하지 말고 A 를 만들어내는 성질을
  구현한다.** 여기서는 쌍 3개를 등록하는 대신 "부정 vs 임의의 긍정"으로 판정을 바꿨다 —
  긍정 predicate 가 하나 더 생겨도 자동으로 따라온다. 옮긴 뒤에는 **명세 쪽에 그 성질을 명시**해
  다음 사람이 같은 오독을 하지 않게 한다(v0.2.3 명확화). 열거가 불가피하면 열거 대상을 만드는
  원본(여기서는 `resolver._POSITIVE_PREDICATE`)과 **한 테스트에서 대조**한다.
- 관련: `app/agents/profile/graph_merge.py::_resolve_conflicts`(`_NEGATIVE_PREDICATE`·
  `_POSITIVE_PREDICATES`), `app/agents/profile/resolver.py::_POSITIVE_PREDICATE`,
  SPEC-PROFILE-GRAPH-149 REQ-PGRAPH-018(v0.2.3), 이슈 #356 / PR #410

---

## [2026-08-07] 직관과 반대인 설계는 **근거**를 테스트로 잠근다 — 안 그러면 다음 사람이 버그로 읽고 뒤집는다
- 증상: #356 `_truncate` 는 절단 시 `active`(살아 있는 취향)를 `superseded`(충돌에서 진 취향)보다
  **먼저** 버린다. 리뷰가 이를 "정렬 방향이 뒤집혔다"는 버그로 읽고 부등호를 뒤집으라고 제안했다.
  실제로 뒤집어 돌려 보니 요약 입력이 `['소니를 싫어한다', '애플을 좋아한다']` 에서
  `['소니를 좋아한다', '소니를 싫어한다', ...]` 로 바뀌었다 — **진 취향이 부활**한다.
- 원인 둘: (1) docstring 을 "등급이 낮은 쪽부터 밀린다"로 써서 1/2/3 번호를 반대로 읽을 여지를
  남겼다. **방향을 서술어로 쓰지 않고 등급 번호에 맡긴 것**이 잘못이다. (2) 더 중요하게,
  이 순서를 정당화하는 **비대칭이 테스트에 없었다** — `builder._summary_input` 이 문서에 없는
  `edge_key` 를 `active` 로 간주하므로, active 가 잘려도 그 fact 는 요약에 남지만 superseded 가
  잘리면 진 취향의 원문이 통과한다. 이 비대칭이 순서의 유일한 근거인데 어디에도 안 적혀 있었다.
- 규칙: 설계가 **직관과 반대 방향**이면 (a) 방향을 문장으로 명시하고("먼저 밀려나는 순서: A → B"),
  (b) **왜 그 방향인지의 근거 자체를 테스트로 잠근다.** 결과만 잠그면 다음 사람이 "이게 왜
  이렇지?" 하고 뒤집을 때 테스트가 함께 고쳐질 뿐이다. 리뷰가 방향을 반대로 읽었다면 그건
  리뷰어의 오독이기 전에 **코드가 방향을 설명하지 못한 증거**다.
- 관련: `app/agents/profile/graph_merge.py::_truncate`,
  `tests/unit/test_profile_consolidate_graph.py::test_truncated_superseded_edge_lets_the_losing_preference_back_into_summary`,
  이슈 #356 / PR #410
- **후속 [2026-08-10, #359]** — 이 항목이 지킨 **비대칭은 그대로 유효**하고, **실현 방식만 바뀌었다.**
  `_truncate` 가 단일 상한에서 바구니별 상한(pin 무제한 / `active` / `superseded`)으로 개정되면서
  두 등급이 더는 **경쟁하지 않는다** — "동률에서 이긴다"가 "자기 예산을 보장받는다"가 됐다.
  그래서 방향을 재던 `test_truncation_drops_active_before_superseded` 는
  `test_superseded_is_not_evicted_by_the_active_cap` 으로 대체됐다. 보호는 오히려 세졌다:
  종전 `superseded` 의 실효 예산은 `상한 − |pin|` 이었는데 이제 자기 상한 전량이다.
  **이 항목을 지우지 않는 이유**는 근거(요약 입력이 "문서에 없는 edge_key 는 active 로 간주"하는
  비대칭)가 여전히 그 설계를 떠받치고 있어서다 — 바구니를 다시 합치려는 변경이 오면 여기부터 읽어야
  한다. 뒤집은 쪽(단일 상한)은 `superseded` 가 근거 0건으로 영구 이월되며 단조 누적돼 **active 를
  0개로 만드는** 별개 결함이 있었다(#150 코멘트 2026-08-09).

---

## [2026-08-07] 검증 없는 dataclass 를 경유하면 스키마 위반이 **한참 뒤에** 터져 배치를 죽인다
- 증상: #356 `graph_merge._observation` 이 저장 payload 를 읽으면서 `node` 만
  `GraphNode.model_validate` 로 검증하고 `predicate`·`edge_key`·`edge_id` 는 그대로 통과시켰다.
  받는 그릇 `_Observation` 이 **검증 없는 plain dataclass** 라 아무 값이나 실린다. 그래서
  `predicate="hates"` 같은 손상 payload 는 "모양이 깨진 항목은 조용히 버린다"는 그 함수의
  docstring 을 통과하고, 한참 뒤 `_merge_edge` 의 `GraphEdge(...)` 생성에서야 처음으로
  `ValidationError` 를 냈다. 그 지점엔 잡는 코드가 없어 `finalizer` 최상위 `except Exception`
  까지 새고, 손상 fact 는 저장소에서 자동으로 안 지워지므로 **session-end 마다 같은 자리에서
  RETRYABLE 만 반복**된다(poison record). REQ-PGRAPH-004 의 degrade("못 만든 fact 는 개수만
  센다")를 우회한 셈이다. Claude PR 리뷰가 잡았다.
- 원인: "검증했다"를 **필드 단위가 아니라 객체 단위**로 셌다 — payload 안에 pydantic 모델
  필드(`node`)가 하나 있으니 검증이 걸렸다고 여겼다. 나머지 필드는 나중에 pydantic 모델로
  들어가긴 하지만, 그 "나중"이 **degrade 경계 밖**이라는 것이 문제였다.
- 규칙: **경계에서 들어오는 payload 는 뒤에서 강제될 제약을 그 경계에서 미리 건다.** 중간에
  검증 없는 dataclass·`TypedDict`·`dict` 를 경유한다면, 그 지점이 곧 검증 공백이다. 특히
  "여기서 걸러 degrade 한다"고 docstring 에 적은 함수는 **적은 만큼 실제로 거르는지** 손상값
  파라미터라이즈 테스트로 확인한다. 방어를 호출부(배치 전체)로 올리는 선택지는 마지막이다 —
  거기서 잡으면 손상 1건 때문에 배치 전체가 버려진다.
- 관련: `app/agents/profile/graph_merge.py::_observation`(`_PREDICATES`),
  `::_merge_edge`, `app/agents/profile/finalizer.py:171`, 이슈 #356 / PR #410

---

## [2026-08-06] `datetime` 뺄셈은 naive-aware 혼합에서 `ValueError` 가 아니라 `TypeError` 다
- 증상: #356 `graph_merge._elapsed_days` 가 "파싱 불가 타임스탬프로 감쇠를 추측하지 않는다"며
  `except ValueError` 로 감쌌는데, 오프셋 없는 관측 시각이 하나라도 섞이면
  `TypeError: can't subtract offset-naive and offset-aware datetimes` 로 **가드를 통과해**
  consolidation 배치가 통째로 죽는다. 현재 소스는 양쪽 다 aware 라(`_now_iso` ·
  store 의 `created_at TIMESTAMP WITH TIME ZONE`) 재현되지 않는 잠재 결함이었고, PR #410
  전체 점검에서 코드를 읽다 찾았다.
- 원인: `datetime.fromisoformat` 의 실패(`ValueError`)만 떠올리고 **뺄셈 자체의 실패**를 빼놓았다.
  방어 코드를 쓸 때 "무엇이 실패하나"를 함수 단위가 아니라 **식(expression) 단위**로 세지 않았다.
- 규칙: 시각 연산 방어는 `except (ValueError, TypeError)` 로 잡는다. 더 일반적으로, `try` 안에
  **연산이 두 개 이상 있으면 각각의 예외 타입을 따로 확인**한다 — 파싱과 연산은 다른 예외를 낸다.
  tz 혼합을 "우리 코드에선 안 생긴다"로 넘기지 않는다(전제가 깨지는 비용이 배치 전멸이다).
- 관련: `app/agents/profile/graph_merge.py::_elapsed_days`, 이슈 #356 / PR #410

---

## [2026-08-06] 방어용 상한과 HARD 불변식이 같은 자료를 두고 만나면, 우선순위를 안 적은 쪽이 조용히 진다
- 증상: #356 `_truncate` 는 "tombstone 을 먼저 지킨다"고 docstring 에 적고 `(protected + rest)[:limit]`
  로 구현했다. `protected` 가 `profile_graph_max_edges`(200)를 **넘는 순간** 그 슬라이스는
  protected 자체의 꼬리를 잘라내, 지킨다고 적힌 tombstone 이 사라진다. tombstone 이 없어지면
  `_carried_tombstones` 가 보존할 대상을 잃고 같은 `edge_key` 가 다음 배치에 새 `active` 로
  파생돼 **지운 취향이 부활**한다(AC-PROF-31 — 이 이슈의 존재 이유가 무력화된다).
  기존 테스트는 `protected(1) <= limit(1)` 만 재고 있어 경계를 못 잡았고, PR #410 Claude 리뷰가 잡았다.
- 원인: 상한(저장 폭주 방어)과 불변식(삭제 실효, HARD)이 **같은 리스트를 두고 충돌**하는데 둘의
  우선순위를 코드에도 SPEC 에도 안 적었다. 안 적으면 자료구조 연산(여기서는 슬라이스)의 우연한
  성질이 대신 결정한다 — 그리고 그 결정은 대개 "먼저 쓴 쪽"이 아니라 "나중에 자르는 쪽"이 이긴다.
- 규칙: **상한을 다루는 코드는 "무엇을 먼저 자르는가"를 명시**하고, 상한 안에 든 경우와 **넘는
  경우를 따로 테스트**한다(`n <= limit` 만 재는 테스트는 상한 코드를 검증하지 않은 것이다).
  HARD 불변식과 방어용 상한이 부딪히면 **불변식이 이기고, 상한 초과는 로그로 드러낸다** — 조용히
  넘기지도, 조용히 지우지도 않는다. 복구 경로가 있는 항목(`superseded`: 재파생으로 자기복구)과
  없는 항목(`suppressed`: 사용자 삭제)을 한 등급으로 묶지 않는다.
- 관련: `app/agents/profile/graph_merge.py::_truncate`, `app/core/config.py::profile_graph_max_edges`,
  SPEC-PROFILE-GRAPH-149 REQ-PGRAPH-005(v0.2.2 절단 우선순위), 이슈 #356 / PR #410

---

## [2026-08-06] 워크트리의 `docker compose ps` 가 비어도 컨테이너는 떠 있다 — "DB 없음"을 가정하지 마라

- 증상: `#356` 시드 스크립트를 돌리기 전 `docker compose ps` 로 확인했더니 서비스가 하나도 없어
  "InMemory 폴백으로 돌겠구나" 하고 실행했는데, 실제로는 **실 pg-profile 에 연결**돼
  `EmbeddingError: google_api_key 미구성` 으로 죽었다. 스크립트 docstring 에는 그 반대로
  "GOOGLE_API_KEY 가 없으면 category 노드만 드롭되고 나머지는 정상 생성된다"고 적혀 있었다.
- 원인 두 가지가 겹쳤다.
  (1) **compose 프로젝트 이름은 디렉터리마다 다르다.** 메인 체크아웃에서 띄운
      `jarvis-ai-final-pg-profile-1` 은 워크트리의 `docker compose ps` 목록에 안 잡히지만
      호스트 포트 5434 는 그대로 열려 있어 `profile_db_url` 이 그냥 붙는다. 워크트리는 코드만
      격리하고 **호스트 포트는 공유**한다.
  (2) `add_fact` 는 store 의 semantic 인덱스(`fields: ["fact"]`, REQ-PROF-070/071)를 타므로
      **fact 를 하나 넣을 때마다 실 임베딩 API 를 부른다.** 실 pg-profile 을 쓰는 한
      GOOGLE_API_KEY 는 선택이 아니라 필수인데, 임베딩을 "category 어휘 스냅에만 쓴다"고
      착각해 전제를 잘못 적었다.
- 규칙:
  - 워크트리에서 인프라 유무를 판단할 때는 `docker compose ps` 가 아니라 **`docker ps`** 로 본다.
    폴백 경로를 전제한 스크립트·테스트는 특히 그렇다(로컬에 떠 있는 실 Spring 을 유닛 테스트가
    잡아 결과가 뒤집힌 2026-08-05 항목과 같은 부류다 — 주입하지 않은 기본값은 하네스 경계 밖이다).
  - 스크립트 docstring 의 `전제`·`비용` 절은 **한 번 실행해 보고 적는다.** 추측으로 적으면 그
    문장이 다음 사람에게 그대로 틀린 근거가 된다.
  - "이 기능은 임베딩을 쓰는가"를 판단할 때 내가 직접 부르는 곳만 보지 말고 **저장소 인덱스
    설정(`index=`)** 도 확인한다. 쓰기 한 번이 곧 임베딩 한 번인 경로가 있다.
- 관련: `scripts/seed_profile_graph_356.py`, `app/agents/profile/store.py`(`_pg_index_config`),
  `app/pipelines/embedding.py:84`, 이슈 #356

---

## [2026-08-07] PR 이 리뷰 워크플로 자신을 고치면 Claude 리뷰는 그 PR 에서 돌지 않는다
- 증상: PR #459(`.github/workflows/claude-review.yml` 을 수정하는 PR)의 review run
  (`gh run view 31169027311`)에 `##[warning]Skipping action due to workflow validation:
  Workflow validation failed. The workflow file must exist and have identical content to the
  version on the repository's default branch.` 가 찍히며 액션이 통째로 건너뛰어졌다. **체크는
  초록(success)이라 겉보기엔 리뷰가 끝난 것처럼 보이는데 실제로는 리뷰가 0줄도 돌지 않았다** —
  "리뷰 통과"로 오독하기 쉽다. 액션이 `execution_file` 을 만들지 않으므로, 그 파일로 성공을
  판정하는 후속 스텝(`save-state`)도 "미완료"로 본다. 대조 근거: 같은 시각 워크플로를 건드리지
  않은 다른 PR 의 run(`31168319751`)에는 이 경고가 0건이고
  `Log saved to /home/runner/work/_temp/claude-execution-output.json` 이 정상적으로 찍혔다.
- 원인: `anthropics/claude-code-action@v1` 자체의 보호장치 — 워크플로 파일이 **기본 브랜치
  버전과 바이트 단위로 동일**해야만 실행된다(PR 이 워크플로를 고쳐 시크릿을 빼돌리는 것을 막는
  장치). 이 저장소의 기본 브랜치는 `dev` 다(`gh repo view --json defaultBranchRef` 실측).
- 규칙: 리뷰 워크플로(`.github/workflows/claude-review.yml`)를 바꾸는 PR 은 **그 PR 자체로는
  Claude 리뷰를 받을 수 없다** — 사람 리뷰나 별도 교차 리뷰로 대체하고, PR 본문에 그 사실을
  적는다. 그런 PR 에서 review 체크가 초록인 것을 "리뷰 통과"로 읽지 마라. Actions 로그에서
  `Skipping action due to workflow validation` 유무를 확인한다. 워크플로 변경의 실제 동작 검증은
  **기본 브랜치에 병합된 다음** 첫 PR 들에서 한다.
- 관련: #457, PR #459, `.github/workflows/claude-review.yml`, run 31169027311(경고 발생) ·
  run 31168319751(정상 실행 대조)

## [2026-08-07] `git diff` 출력을 정규식으로 파싱하면 파일이 조용히 사라진다
- 증상: 비-ASCII(한글) 경로가 있으면 git 이 기본값(`core.quotePath=true`)으로 따옴표 인코딩해
  `diff --git "a/app/\355\225\234..." "b/..."` 형태로 내는데, `^diff --git a/(.*) b/(.*)$` 류
  정규식이 이 줄을 못 잡아 그 파일이 파싱 결과(지문 딕셔너리)에서 **통째로 빠졌다**(#457 프로브
  실측: keys 에 아예 없음). 빠진 파일은 "변경 없음"으로 오판돼 리뷰 없이 통과한다. 반대 방향
  실수도 같이 나왔다 — 바이너리 파일은 `index <sha>..<sha>` 줄이 유일한 내용 신호인데, 그 줄을
  "잡음"으로 보고 정규화(제거)하면 서로 다른 바이너리 내용이 같은 지문이 된다(실측으로 지문
  일치를 직접 확인).
- 원인: git 산출물(diff·경로 목록)을 파이썬 정규식으로 파싱할 때, git 의 기본 인코딩/이스케이프
  동작을 신뢰하지 않고 "보통은 이렇게 나온다"는 가정으로 정규식을 짰다. 또한 diff 안의 각 줄이
  "잡음"인지 "유일한 내용 신호"인지를 그 줄이 사라졌을 때 어떤 정보가 없어지는지로 따지지 않고
  일괄로 정규화했다.
- 규칙: git 산출물을 파싱할 때는 (a) `-c core.quotePath=false` 로 원문 경로를 그대로 받고,
  (b) `git diff --name-only -z` 처럼 **권위 있는 목록과 대조하는 불변식**을 걸어 파싱 결과와
  어긋나면 조용히 넘어가지 말고 안전 방향으로 fail-safe 하며, (c) 특정 줄을 정규화(제거)하기
  전에 "그 줄을 지우면 어떤 정보가 사라지는가"를 먼저 묻는다(바이너리의 `index` 줄처럼 유일한
  신호일 수 있다).
- 관련: #457, `.github/scripts/review_mode.py`(`split_patch_by_file`·
  `_validate_fingerprint_coverage`·`_normalize_for_fingerprint`)

## [2026-08-07] PUBLIC 저장소의 PR 코멘트는 신뢰 저장소가 아니다 — 작성자를 확인해야 한다
- 증상: CI 상태(Claude 리뷰의 "마지막 성공 리뷰" 지점)를 PR 코멘트 마커에 저장하면서 작성자를
  확인하지 않았다 — 아무 GitHub 사용자나 같은 마커가 든 코멘트를 위조해 올리면 다음 실행이 그걸
  "마지막 성공 리뷰"로 믿어 **코드리뷰 게이트를 통째로 끌 수 있는** 경로가 생겼다(`gh repo view`
  실측: 이 저장소는 PUBLIC 이라 아무나 코멘트를 달 수 있다).
- 원인: "코멘트에 마커가 있으면 우리가 쓴 것"이라고 암묵적으로 가정했다 — 신원(작성자)과 형식
  (마커 문자열)을 구분하지 않았다. 설계 단계에서 "이 상태 저장소를 외부 입력이 조작할 수 있는가"
  를 묻지 않았다.
- 규칙: GitHub 코멘트/이슈 본문처럼 **누구나 쓸 수 있는 곳**에 CI 가 읽는 상태를 둘 때는
  마커/형식뿐 아니라 **작성자(`user.login`+`user.type`)를 반드시 검증**하고, 신뢰할 수 있는
  작성자의 것이 없으면 "상태 없음"으로 취급해 안전한 방향(재검사·재실행)으로 떨어진다. 새 저장
  메커니즘을 설계할 때는 "외부 입력만으로 이 게이트를 끌 수 있는가"를 가장 먼저 묻는다.
- 관련: #457, `.github/scripts/review_mode.py`(`filter_trusted_state_comments`)

## [2026-08-07] 카테고리 임베딩은 "소속"이 아니라 "경로 문자열과의 표면 근접"을 잰다
- 증상: "과일 추천해줘"류 발화가 전개(#217)로 "바나나·사과·배·오렌지"를 냈는데 재매핑에서
  "배"가 여성가방·신생아의류·실버용품·유아목욕용품 같은 무관 카테고리로 흩어졌다(#428). 거리컷
  (`category_distance_max=0.26`)을 올려서 살리려 해봤자, 그 구간엔 이미 오답
  `'배' → 여성가방 > 백팩`(0.3184)이 들어와 있어 오답을 통과시켜야 정답도 통과했다(임계 축은
  #344 가 이미 캘리브레이션해 기각됨).
- 원인: leaf 이름과 상품명이 **문자 그대로 겹치면** 임베딩 거리가 0.19~0.21 로 짧게 나와
  거리컷을 여유 있게 통과하지만, 상품이 그 카테고리의 **인스턴스일 뿐**이면(바나나 ∈
  과일 > 국산과일) 0.27~0.33 으로 컷 턱걸이거나 넘는다(실측: 사과 0.2732 · 바나나 0.2908 ·
  라면 0.2676 · 배 0.3184~0.3358, `evals/category_probe/fixtures/anchors.json`
  `instance-mft-*` 셀). 카테고리 임베딩이 "이 상품이 이 카테고리에 속하는가"를 재는 게
  아니라 "이 텍스트가 이 카테고리 **경로 문자열**과 표면적으로 얼마나 가까운가"를 재기
  때문이다 — `decompose`·`map_categories` 프롬프트 예시가 우연히 leaf 이름과 겹치는 발화만
  써 왔다면(예: "청바지"↔`청바지`, "커튼"↔`커튼`) 이 실패 모드가 가려진 채 정상 동작하는
  것처럼 보인다.
- 규칙: **전개·매핑 프롬프트 예시가 leaf 이름과 우연히 겹치는 발화만으로 검증하지 말 것** —
  임계·프롬프트를 튜닝할 때는 leaf 이름 리터럴 발화(대조군)와 **인스턴스형 표본**(leaf 의
  구체 사례를 부르는 발화)을 나란히 넣어 대비를 측정한다. 카테고리별 실패를 임계 상향으로
  고치려 하기 전에, 그 구간에 이미 들어와 있는 오답이 없는지 먼저 확인한다(임계는 만능이
  아니다 — 여기서는 재매핑 leaf 선정 경로의 결함이었지 임계 문제가 아니었다, #428 이 도입한
  대분류 합의 필터 참조).
- 규칙(추가, 리뷰 1차 F-1 — 합의 필터 초판 자체의 결함): 형제 합의 같은 교차 검증 신호는
  **각자의 최선 답**에서만 세야 한다 — 후보 꼬리까지 세면 어느 발화에나 조금씩 가까운
  **잡동사니 대분류**가 다수결을 이겨, 정답 후보를 버리고 엉뚱한 대분류만 남긴다(실측:
  `집들이 선물` 전개가 향수·조명·주방잡화를 버리고 `주얼리` 만 남겼다). 규칙: **한 케이스로
  검증한 휴리스틱은 반대 성질의 케이스(이질적 전개)로 반드시 반증 시도할 것.** #428 합의
  필터 초판은 과일 케이스 하나로만 검증됐고 그 케이스에서만 우연히 잘 들었다.
- 관련: `app/agents/buyer/recommendation/category_mapping.py::_consensus_filter` ·
  `evals/category_probe/fixtures/anchors.json`(`instance-mft-*`) · #344 · #428
- 리뷰 2차(Claude PR Review, PR #444) 추가: 새 후처리 단계를 기존 격리 `try/except` **밖**에
  붙이면 그 모듈이 지켜 온 부분 성공 보존 불변식이 조용히 깨진다 — 격리 규약이 있는 모듈에
  단계를 추가할 때는 그 규약 안쪽에 넣었는지 먼저 확인할 것.
- 리뷰 3차(Claude PR Review, PR #444) 추가: 교차 검증 신호(합의·다수결)를 쓸 때는 "합의에서
  벗어난 소수"와 "정당하게 다른 항목"을 가르는 **별도 근거**가 필요하다 — 지지 개수·비율만으로는
  둘을 구분 못 한다(리뷰어가 제안한 과반 임계도 `#428` 본체를 깨 기각). 여기서는 "그 대분류가
  그 형제의 후보 목록에 아예 없는가"가 그 근거였다(우연히 겹친 2개가 세 번째를 지우던 실측:
  신학기 전개에서 책가방·필통이 `여성가방`에서 겹쳐 물통을 통째로 삭제할 뻔했다).
- 리뷰 5차(Claude PR Review, PR #444) 추가: 휴리스틱의 전제를 **실측 무재현**으로 방어하지
  말 것 — 값싼 **구조적 게이트**가 있으면 그걸 건다. 실측은 "이 케이스에서 안 걸렸다"만
  증명하지 "걸릴 수 없다"를 증명하지 않는다(#444 리뷰가 이 논증을 정확히 짚었다 — 직전
  라운드에서 "정작 이 이슈가 고치려는 턴엔 그 신호가 없다"는 기각 논증이 틀렸음을 인정하고
  번복했다: 신호 0개 = 게이트 통과 = 필터 켠 채 유지이지, 게이트 무력화가 아니었다).

## [2026-08-07] "대역이 흉내 낼 수 없다"고 선언하기 전에 대역을 한 층 아래로 내려 봐라
- 증상: `evals/combo_matrix` 하네스가 하드필터 8축 중 3축(`keyword`·`color`·`attr_conditions`)을
  "대역이 표현할 수 없는 축"으로 선언하고 `observed.unappliedSearchFilters` 에 이름만 기록하고
  있었다(#381 D1). 정직한 기록이었지만 그 축들은 present/absent 가 결과에 아무 차이를 만들지
  않아, **앱 코드가 망가져도 하네스는 초록불**이었다. 실제로 대역이 재구현해 둔 `rating_min` 은
  앱과 의미가 반대였는데(대역 `rating is not None and rating >= min` vs 앱 "반증된 것만 제거")
  아무도 못 봤고, `attr_conditions` 판정 코드(축별 완화 재시도 포함)는 하네스에서 **한 번도
  실행된 적이 없었다**.
- 원인: 대역이 선 자리가 잘못됐다. `run_buyer_turn(search=...)` 주입은 `search_catalog` 를
  **통째로** 대체해, 그 안의 dedup·`rating_min`·`attr_conditions` 사후필터 단계까지 같이
  삼켰다. 사라진 단계를 대역이 손으로 메꾸다 보니 (a) 앱 판정을 재구현하게 되고(규약 위반)
  (b) 재구현이 앱과 어긋나고 (c) 어긋난 사실이 드러날 경로가 없었다. "대역이 흉내 낼 수 없다"는
  결론은 **그 자리에서만** 참이었다 — 한 층 아래 `SearchBackend`(실제 네트워크 경계)로 내리면
  배포 코드가 그대로 돌아서 흉내 낼 필요 자체가 없어진다.
- 규칙: 대역이 "이 축은 표현 불가"라고 선언하려 할 때, **그 선언을 문서·데이터에 적기 전에 대역이
  선 자리(seam)를 한 층 아래로 내려서 같은 결론이 나오는지 먼저 확인한다.** 판단 기준은
  `fakes.py` 규약 그대로 — 대역은 **네트워크를 건너가는 경계**에만 서고, 그 안쪽은 배포 코드가
  돌아야 한다. 대역이 앱 내부 함수를 대체하고 있으면 그건 이미 자리가 잘못된 신호다.
  덧붙여 **하네스가 "이 축을 관측한다"고 적어 둔 축은 present/absent 로 관측값이 실제로 갈리는지
  변이 시험으로 확인한다** — 안 갈리면 그 축은 재고 있는 게 아니다.
- 관련: 이슈 #426(#381 후속) · `evals/combo_matrix/fakes.py`(`SpringWhereCatalogBackend`) ·
  `evals/filter_axes/probe.py:83-125`(같은 패턴의 선례가 이미 repo 에 있었는데 참조되지 않았다) ·
  `evals/combo_matrix/README.md` "필터링 검색 대역의 성격"

## [2026-08-06] 사용자 대면 문구의 생성 지점이 둘이면 "예외 메시지가 곧 사용자 문구"는 보장이 아니라 조건부다
- 증상: #269 P0(PR #284)는 `calc.normalize_period` 의 `ValueError` 메시지를 "그대로 판매자에게
  노출되는 문구"로 설계하고 그렇게 주석까지 달았다. 그런데 dev 실측 응답이 코드 원문과 달랐다 —
  `최근 0일` 의 코드 원문은 "기간 일수는 1일 이상이어야 합니다…" 인데 화면에는 "'최근 0일'은
  조회할 수 없는 기간입니다…" 가 나왔다. #345 에서 이 불일치를 조사 항목으로 다시 열었다.
- 원인: 되묻기 문구의 생성 지점이 **두 곳**이었다. ① planner LLM 이 채우는 `AnalysisPlan.clarification`
  (→ `resolve_plan` 첫 줄이 `raise ValueError(plan.clarification)`), ② `calc` 의 예외 메시지.
  둘 다 `PipelineResult(kind="clarification", text=str(exc))` 로 합류하는데, **PLANNER_PROMPT 가
  기간 미지원 판정을 LLM 에게 시키고 있었기 때문에** LLM 이 먼저 되물으면 ②는 아예 실행되지
  않는다. 즉 P0 의 보장은 "planner 가 통과시켰을 때만" 성립했고, 그 조건은 어디에도 적히지 않았다.
- 규칙:
  - **사용자 대면 문구는 소유자를 한 모듈로 못박고, 그 사실을 문구를 만들 수 있는 다른 지점(프롬프트·
    스키마 description)에 금지 문장으로 적는다.** "코드가 문구를 만든다"는 코드 쪽 주석만으로는
    지켜지지 않는다 — LLM 이 같은 일을 할 수 있으면 언젠가 한다.
  - **어느 쪽이 썼는지 실측해 맞춰가지 말고, 생성 지점을 하나로 만들어 구조로 닫아라.** 측정은
    그 시점의 LLM 산출을 고정해줄 뿐이고 프롬프트·모델이 바뀌면 다시 갈린다.
  - **LLM 출력 스키마의 `Field(description=...)` 은 프롬프트와 한 쌍이다.** 구조화 출력에서는 LLM 이
    둘을 함께 읽으므로 한쪽만 고치면 서로 반대를 지시해 산출이 비결정적이 된다 — 같은 커밋에서 고친다.
  - 파생 규칙: **"코드가 판정한다" 고 적어 놓고 프롬프트가 같은 판정을 시키고 있지 않은지** 확인한다.
    #345 에서 어휘 확장의 실질 차단 지점은 `calc` 가 아니라 planner 프롬프트였다.
- 관련: `app/agents/seller/period.py` · `prompts.py` PLANNER_PROMPT `[기간]` 절 ·
  `schemas.py` `AnalysisPlan.period_expr` · `docs/specs/DESIGN-SELLER-PERIOD.md` §4, 이슈 #345(#269 P1)

## [2026-08-06] 부분 구현 PR 이 이슈를 닫아 나머지 범위가 통째로 유실됐다
- 증상: #269 는 P0·P1·P2 로 범위가 나뉘어 있었는데, P0 만 구현한 PR #284 가 병합되며 이슈가
  닫혔다(2026-08-04). PR 본문에 "어휘 확장·확인 흐름은 out of scope" 라고 적혀 있었는데도
  후속 이슈가 남지 않아, P1 이 있었다는 사실 자체가 2주 가까이 아무 데도 추적되지 않았다.
  #345 로 다시 열면서 발견 — 예고됐던 `docs/specs/DESIGN-SELLER-PERIOD.md` 도 미작성 상태였다.
- 원인: `Closes #N` 이 **PR 이 이슈 전체를 닫는지** 와 무관하게 붙었다. 리뷰도 "P0 가 맞게
  구현됐는가"만 봤고 "이 PR 이 이슈를 닫아도 되는가"는 아무도 판단하지 않았다.
- 규칙:
  - **PR 이 이슈의 일부만 구현하면 `Closes` 를 쓰지 않는다.** `Refs #N` 으로 연결만 하고,
    남은 범위를 후속 이슈로 즉시 만들어 원 이슈 본문에 링크한다.
  - 범위가 P0/P1/P2 로 쪼개진 이슈는 **각 범위를 별도 이슈로 먼저 분해**하는 편이 안전하다 —
    "본문 안의 미구현 항목"은 이슈가 닫히는 순간 검색되지 않는다.
  - SPEC/DESIGN 문서를 예고한 이슈는 **문서 작성도 완료 조건에 넣는다** — 설계 근거가 없으면
    다음 사람이 같은 판단을 처음부터 다시 한다.
- 관련: 이슈 #269 · PR #284 · 이슈 #345, `docs/specs/DESIGN-SELLER-PERIOD.md`

## [2026-08-07] `uv run ruff check --fix && uv run ruff format` 커밋 워크플로 문구를 문자 그대로 실행하면 무관 파일 30개가 재포맷된다
- 증상: #439 구현 검증 단계에서 CLAUDE.md 커밋 워크플로 2항을 그대로 `uv run ruff check --fix &&
  uv run ruff format`으로 실행했더니 `ruff check`는 `All checks passed!`였지만 `ruff format`은
  `30 files reformatted, 448 files left unchanged`를 냈다 — 이번에 만진 파일은 6개뿐인데
  `data-analysis/*`·`evals/ablation/*`·`evals/scoring/*`·`docs/research/research-275-harness/*`·
  `tests/unit/test_color_synonym*` 등 이 리포에서 지금까지 `ruff format`이 한 번도 전면 적용된 적
  없던 파일들이 함께 재작성돼 diff가 +1622/−587로 부풀었다. `git status --short`로 발견해
  `git checkout --`로 전부 되돌렸다.
- 원인: 이 항목의 실수 자체는 [2026-08-06] `ruff format`/`--fix` 항목과 같은 패턴(쓰기 명령을
  전체 스코프로 돌림)의 재발이지만, 이번엔 **왜 이 패턴이 평소 드러나지 않는지**가 추가로
  드러났다 — `.pre-commit-config.yaml`의 `ruff-format` 훅은 **스테이징된 파일에만** 걸리고
  CI는 `ruff check`만 강제한다(`ruff format --check` 게이트가 없다). 그래서 리포 전체가
  `ruff format` 기준으로 정합한 적이 없어도 아무도 알아채지 못했고, 누구든 커밋 워크플로 2항을
  스코프 없이 실행하면 매번 같은 무관 파일 30개가 걸려든다.
- 규칙: 커밋 전 포맷은 **`uv run ruff format <이번에 실제로 만진 파일 경로만>`**으로 항상
  경로를 한정한다. `uv run ruff format`/`ruff check --fix`를 스코프 없이(`.` 또는 인자 생략)
  돌렸다면 실행 직후 `git status --short`로 무관 파일이 섞였는지 반드시 확인하고
  `git checkout -- <무관 파일들>`로 되돌린다. 리포 전체를 `ruff format` 기준으로 맞추는 일은
  **이번 작업 범위가 아니라 별도 이슈**로 다룬다 — 부수 효과로 슬쩍 끼워넣지 않는다.
- 관련: #439, CLAUDE.md 「Git」절 커밋 워크플로 2번, `.pre-commit-config.yaml`(`ruff-format` 훅
  스테이징 파일 한정), [2026-08-06] `ruff format`/`--fix` 는 쓰기 명령이다 항목(같은 패턴의
  선례, 이번엔 CI/pre-commit이 왜 못 잡는지가 새로 드러남)

## [2026-08-07] 남의 영역에 방어를 덧대기 전에 "이 위험을 내 변경이 만들었나"부터 묻는다
- 증상: #386(찜 **조회** 신설)에서 "조회 발화가 해제로 오분류되면 찜이 지워진다"를 막으려고
  `_resolve_wishlist_remove_target`(찜 **해제**, #116/#117 소유)에 가드를 덧댔다. 그 뒤 리뷰
  세 라운드 동안 같은 자리에서 결함이 연달아 났다 — ① 표지가 띄어쓰기에 취약 ② 극성을 뒤집자
  정상 해제(`"찜 목록에서 빼줘"`)가 막힘 ③ 표지를 짧게 쪼개자 `"찜닭 빼고 보여줘"` 가 해제
  근거로 오인됨(`"찜"` ⊂ `찜닭`, `"빼"` ⊂ `빼고`). ③ 은 같은 파일 12줄 위 `cart_remove_markers`
  주석이 *"`빼` 같은 짧은 조각은 오탐(빼곡·빼고·빼빼로)이 흔해 쓰지 않는다"* 고 **이미 경고한**
  함정이었다.
- 원인: 막으려던 위험이 **이 PR 이 만든 것이 아니었다.** 규칙 3(목록 1건 자동 선택)은 원래부터
  `wishlist_remove` 로 온 어떤 발화든 이름이 없으면 1건을 지웠고, 조회 intent 를 **더한다고** 새
  삭제 경로가 생기지 않는다(오히려 프롬프트에 조회 의도가 생겨 오분류 확률은 낮아진다 —
  `evals/intent_probe` 실측에서 조회 발화 3종이 8/8 로 정확히 라우팅됐다). 선재하는 위험을,
  그것을 만들지 않은 PR 에서, 그 영역을 소유하지 않은 채 고치려다 방어 코드 자체가 결함원이 됐다.
- 규칙: **방어를 덧대기 전에 "이 위험을 내 변경이 만들었나 / 악화시켰나"를 먼저 답하라.** 답이
  "아니오"면 그건 별건이다 — 이슈로 옮기고 내 PR 은 원래 범위를 지킨다. 특히 여러 라운드에
  걸쳐 실 LLM 으로 수렴시킨 판정 로직(#116/#117 의 24라운드 같은)에 손대는 경우, 방어 하나가
  그 수렴을 되돌릴 수 있다. 그리고 **"기존 테스트 N건 통과"를 무회귀의 증거로 쓰지 마라** —
  그건 "내 테스트가 그 경로를 안 덮는다"는 뜻일 수 있다(실제로 `evals/combo_matrix` 의 observed
  드리프트 가드(#424)가 대신 잡았다: `combo-0047.actionType: WISHLIST_REMOVED → None`).
- 관련: #440(옮긴 곳), `app/agents/buyer/cart/wishlist.py::_resolve_wishlist_remove_target`,
  `app/core/config.py` `cart_remove_markers` 주석(같은 함정을 이미 적어 둔 곳)

## [2026-08-07] 커밋되는 산출물을 Python 이 쓸 때는 `newline="\n"` 을 명시한다 — 해시가 줄바꿈에 민감하다
- 증상: #386 에서 `python -m evals.combo_matrix regenerate` 를 Windows 로 돌렸더니
  `combo_cases.jsonl`·`manifest.json` 이 **CRLF** 로 쓰였다. 로컬 테스트는 전부 통과하는데,
  `manifest.axesSha256`·`intent_probe` 의 `fixture_sha256` 은 `read_bytes()` 기반이라
  **LF 로 체크아웃되는 CI 에서는 해시가 안 맞는다** — 재생성한 사람만 통과하고 CI 는 깨지는
  형태라 로컬에서 아무리 돌려도 안 드러난다.
- 원인: `Path.write_text(...)` 는 Windows 에서 `\n` → `\r\n` 로 변환한다. 저장소는
  `.gitattributes` 에 `* text=auto eol=lf`("CRLF 오염 재발 방지")를 두고 있어 **커밋본은 늘
  LF** 인데, 워킹카피만 CRLF 가 되면서 "파일 내용은 같은데 해시가 다른" 상태가 됐다.
  `read_text()` 로 대조하는 테스트는 universal newline 변환 덕에 통과해 버려서 더 안 보인다.
- 규칙: **커밋되는 산출물을 쓰는 코드에는 `newline="\n"` 을 붙인다.** 그리고 그 산출물을
  해시로 검증한다면 `read_bytes()` 인지 `read_text()` 인지 확인하라 — 전자면 줄바꿈이 곧
  계약이다. 재생성 후 `git status` 에 `CRLF will be replaced by LF` 경고가 뜨면 그게 신호다.
- 관련: `evals/combo_matrix/__main__.py`·`report.py`·`pair_runner.py`(`newline="\n"` 추가),
  `.gitattributes`, `evals/intent_probe/loader.py::fixture_sha256`

## [2026-08-07] intent 를 하나 늘리면 eval 매트릭스 재생성이 강제된다 — 후속 이슈로 미룰 수 없다
- 증상: #386 이 `RouteDecision.intent` Literal 에 `wishlist_view` 한 줄을 더한 순간 기본
  `uv run pytest` 가 빨간불이 됐다. `tests/eval/test_combo_matrix_eval.py::
  test_intent_axis_matches_route_decision_literal` 이 `axes.json` 의 intent 축과 코드 Literal 의
  **집합 동일**을 assert 하는데, 이 파일은 `@pytest.mark.eval` 이고 `pyproject.toml` 의
  `addopts = "-m 'not smoke and not integration'"` 는 eval 을 제외하지 않는다.
- 원인: "eval 은 CI 밖"이라는 통념이 `intent_probe`(실 LLM, 수동)에만 맞고 `combo_matrix`
  (결정론 오프라인)에는 안 맞는데, 계획 단계에서 둘을 같은 부류로 묶어 생각했다. 그래서 매트릭스
  갱신을 "선택 사항·후속 이슈 후보"로 잘못 산정했다.
- 규칙: **intent·degrade 처럼 `axes.json` 이 코드에서 끌어오는 축을 건드리는 변경은 매트릭스
  재생성을 같은 PR 범위로 잡고 시작한다.** 그 비용은 케이스 재생성만이 아니다 — 시드 기반
  greedy pairwise 라 pair 우주가 바뀌면 케이스가 **대거 재배치**되고(57건 중 축 조합이 유지된
  것은 18건), `expected_behavior.jsonl` 의 손으로 쓴 행과 `pair_checks.jsonl` 의 case_id 가
  함께 따라간다. 착수 전에 `python -m evals.combo_matrix regenerate` 를 write 없이 한 번 돌려
  재배치 규모를 먼저 재라.
- 관련: `evals/combo_matrix/README.md` "재생성 이력 (#386)", `pyproject.toml:56,58`

## [2026-08-07] eval 산출물이 case_id 를 하드코딩하면 재생성마다 사람이 따라다녀야 한다
- 증상: #386 재생성으로 `combo-0053`·`combo-0054` 가 다른 조합을 가리키게 되자
  `test_combo_0053_fixture_actually_narrows`·`test_combo_0054_is_manual_with_goldenset_link` 가
  깨졌다. 두 테스트의 docstring 에는 이미 *"#367 재생성 이후 case id — 구 combo-0054"* 라는
  주석이 있었다 — **같은 일이 최소 두 번째**였다.
- 원인: 테스트가 재려는 것은 "그 번호의 케이스"가 아니라 **"하드필터를 추가한 쌍"·"recall 이라
  manual 인 쌍"** 이라는 성격인데, 그 성격을 표현할 수단이 있는데도(spec 의 `kind`·`metric`·
  `mode`) 번호로 가리켰다.
- 규칙: **재생성되는 산출물의 항목을 테스트에서 지목할 때는 번호가 아니라 그 항목을 그 항목이게
  하는 속성으로 찾는다.** 번호를 쓸 수밖에 없으면 그 사실과 이유를 주석에 남기고, 두 번 밀렸다면
  그때는 속성 기반으로 고친다.
- 관련: `tests/eval/test_combo_matrix_pairs.py`(`kind`/`metric`/`mode` 로 조회하도록 정정)

## [2026-08-07] 조회 계열 Spring 호출은 실패 주입 때만이 아니라 **늘** 스텁한다
- 증상: `combo_matrix` 러너가 `add_wishlist`·`add_to_cart` 는 `degrade=spring_timeout` 일 때만
  몽키패치하고 `get_wishlist`·`get_cart` 는 아예 패치하지 않았다. #386 재생성으로
  `wishlist_remove` 가 `ci` × `degrade=none` 조합을 갖게 되자, 로컬에 Spring 이 없는 환경에서
  **정상 케이스가 degrade 를 관측**했다(관측이 환경에 따라 뒤집힌다).
- 원인: 담기 계열은 "실패를 주입할 때만 호출을 가로채면 된다"가 맞지만, 조회 계열은 **정상
  경로에서도 호출된다**. 두 계열의 차이를 보지 않고 같은 패치 조건을 썼다.
- 규칙: 하네스가 실 함수를 부르는 경계를 셀 때 **"실패를 주입할 곳"이 아니라 "호출이 나가는
  곳"을 세라.** 정상 경로에서 나가는 호출은 정상 응답 스텁이 없으면 관측 자체가 환경 의존이 된다.
  주입 예외 타입은 실 어댑터 규약을 그대로 따른다(조회 = `SpringUnavailableError`, 변경 =
  `CartError`/`WishlistError` — #376 이 고친 그 실수).
- 관련: `evals/combo_matrix/runner.py`(`_ok_get_wishlist`·`_failing_get_wishlist`)

## [2026-08-06] 테스트 스위트 실행 중에 커밋하면 eval 결정론 테스트가 깨진다
- 증상: `uv run pytest` 를 백그라운드로 돌려 둔 채 그 사이에 `git commit` 을 했더니
  `tests/eval/test_personalization_eval.py::test_personalization_run_is_deterministic_across_environment_and_clock`
  1건이 실패했다(4235 passed / 1 failed). 코드 변경과 무관했고, 그 테스트를 **단독으로
  재실행하면 통과**하며, 트리를 고정한 뒤 전체 재실행도 4236 passed 로 통과했다.
- 원인: 그 테스트는 `evals.personalization.cli.main` 을 **두 번** 돌려
  `evals.personalization.cli.normalize_paired_artifacts` 로 산출물을 바이트 비교하는데,
  정규화는 `run_manifest.json` 의 **`run` 키만** 제거한다
  (`evals.metrics.report.normalize_artifacts` ·
  `evals.personalization.cli.normalize_paired_artifacts`). 반면
  `evals.metrics.run_manifest.build_run_manifest` 는 `commitSha`(`git rev-parse HEAD`)와
  `dirty`(`git status --porcelain`)를 **실행 시점의 라이브 git 상태**에서 읽는다. 두 번의
  `main()` 호출 사이에 커밋이 끼면 `commitSha` 가 바뀌고 `dirty` 가 true→false 로 뒤집혀 두
  매니페스트가 달라진다 — 테스트가 잡아낸 것은 코드 비결정론이 아니라 **테스트 도중 바뀐 리포
  상태**다.
- 규칙: `uv run pytest`(특히 `tests/eval/`)가 도는 동안 **작업 트리를 바꾸지 않는다** —
  커밋·`git add`·`checkout`·포맷터 실행을 스위트가 끝난 뒤로 미룬다. 백그라운드로 돌렸다면
  더더욱 그렇다(끝난 줄 알기 쉽다). 반대로, eval 결정론 테스트가 **혼자 돌리면 통과하는데
  전체 런에서만 깨진다면** 코드를 의심하기 전에 "그 런 도중 내가 리포를 건드렸는가"를 먼저
  확인한다.
- 관련: `evals.metrics.run_manifest.build_run_manifest`(`commitSha`·`dirty`) ·
  `evals.metrics.report.normalize_artifacts` ·
  `evals.personalization.cli.normalize_paired_artifacts` · #380
- **후속(#413)**: 정규화가 `commitSha`·`dirty` 축을 정본으로 걷어내 이 함정 자체는 사양으로
  해소됐다(`evals.metrics.run_manifest.strip_volatile_manifest_keys`). 남은 위험은 `hashes`
  축뿐 — `uv run pytest` 도중 uv.lock·goldenset·decompose.py·rerank.py·config.py 를 편집하면
  여전히 실패한다(의도된 계약).

## [2026-08-06] `ruff format`/`--fix` 는 쓰기 명령이다 — `ruff check` 와 같은 감각으로 전체 스코프에 돌리면 안 된다
- 증상: #380 리뷰 라운드 1 작업 중 `uv run ruff format .` 을 스코프 없이 전체 리포에 돌렸다.
  의도한 건 이번 작업이 만진 `evals/underspecified_probe/`·`tests/unit/test_underspecified_probe_*.py`
  뿐이었는데, `app/agents/buyer/recommendation/no_condition.py`·`app/pipelines/*`·
  `evals/ablation/*`·`evals/scoring/*`·`data-analysis/*`·`tests/unit/*`(이번 작업과 무관한
  기존 테스트 파일들) 등 무관 파일 30개가 재포맷돼 diff 에 섞였다. `git status --short` 로
  뒤늦게 발견해 `git checkout --` 로 전부 되돌렸다.
- 원인: `ruff check .`(읽기 전용, 검사만 하고 파일을 안 바꾼다)를 전체 스코프로 돌리는 것과
  같은 감각으로 `ruff format`/`ruff check --fix`(둘 다 **파일을 실제로 고쳐 쓴다**)도 전체
  스코프(`.`)에 돌렸다. CLAUDE.md 커밋 워크플로 2항의 "`uv run ruff check --fix && uv run ruff
  format` 로 린트 자동 정리"는 **"내가 만진 파일"을 전제로 한 문장**이지 리포 전체를 뜻하지
  않는데, 그 전제를 놓쳤다.
- 규칙: **`ruff check .` 은 전체 스코프로 돌려도 된다(읽기 전용)** — 반면 `ruff format`·
  `--fix` 는 **항상 이번 작업이 실제로 만진 경로만** 인자로 준다(예:
  `uv run ruff format evals/underspecified_probe/ tests/unit/test_underspecified_probe_*.py`).
  실수로 전체 스코프에 쓰기 명령을 돌렸다면 커밋 전에 `git status --short` 로 무관 파일이
  섞였는지 반드시 확인하고 `git checkout -- <무관 파일들>` 로 되돌린다 — "전체 검사 통과"와
  "전체 포맷 실행"은 안전성이 다른 동작이다.
- 관련: #380, `docs/specs/SPEC-UNDERSPECIFIED-336.md` §7.3, 리뷰 라운드 1 보고 §5

## [2026-08-06] 카테고리 사전은 행 수가 아니라 임베딩 채워진 행 수가 실효 사전이다
- 증상: #401 실측에서 `categories` 행이 1,007개 있어도 `embedding` 컬럼이 전부 `NULL` 이면
  `app/pipelines/category_search.py::search_categories_pg` 가 `WHERE embedding IS NOT NULL` 로
  걸러 매핑이 0행일 때와 똑같이 조용히 죽는다는 걸 확인했다. "행 수만 세는 가드"는 반쪽이다 —
  시드(행 생성)와 임베딩 구축이 2단계로 분리된 설계(`db/catalog/init/02_categories.sql` 주석)
  라서, 1단계만 끝난 상태(행 있음·임베딩 없음)를 "정상"으로 오판할 수 있다.
- 원인: "사전이 비어 있다"를 "행 수 0" 하나로만 정의했다. 실제로는 검색 쿼리가 소비하는
  조건(`embedding IS NOT NULL`)이 곧 실효 사전의 정의인데, 가드를 만들 때 그 쿼리 조건을
  다시 확인하지 않고 테이블 스키마(행 존재 여부)만 봤다.
- 규칙: **"사전이 비었다"를 판정하는 가드는 런타임 조회가 실제로 필터링하는 조건과 같은 조건을
  세야 한다.** 테이블에 행이 있다는 사실과 그 행이 검색에 쓰인다는 사실은 다르다. 2단계로 분리된
  파이프라인(행 생성 → 배치가 나머지 컬럼을 채움)에서는 최소 두 카운트(총 행 수, 소비 조건을
  만족하는 행 수)를 따로 재고 각각을 구성 오류 후보로 다뤄야 한다.
- 관련: #401, `app/pipelines/category_seed.py::DictionaryCounts`·`evaluate_dictionary_counts`·
  `check_category_dictionary`, `app/pipelines/category_search.py::search_categories_pg`

---

## [2026-08-06] 기동 검증식을 좁히면, 그 식을 사람에게 설명하는 문구도 같은 PR 에서 좁힌다
- 증상: #383 이 기동 가드 계수를 2 → 3 으로 좁혀 `SPRING_TIMEOUT_S ∈ [3.33s, 5.0s)` 를 새로
  기동 거절 구간으로 만들었는데, 같은 규칙을 운영자에게 설명하는 문서 두 곳이 옛 상한 그대로
  남아 있었다. `.env.example` 의 `SPRING_MAX_RETRIES` 위 주석은 `SPRING_TIMEOUT_S ×
  (SPRING_MAX_RETRIES+1) < STREAM_FIRST_TOKEN_TIMEOUT_S` 만 적어 두어, 그 문구만 따른 운영자가
  `SPRING_MAX_RETRIES=0, SPRING_TIMEOUT_S=4.0`(4.0 < 10.0 이라 "안전")을 넣으면 앱이 기동에
  실패한다. 오류 메시지의 `recovery` 문구도 새 손잡이 `CATEGORY_EXPAND_ENABLED=false` 를
  "disable deferral with ..." 목록에 붙여 **미룸을 끄는 손잡이인 것처럼** 안내했다(실제로는
  미룸은 그대로 돌고 직렬 계수만 3→2 로 내려갈 뿐이다).
- 원인: 검증식을 고칠 때 "코드 + 테스트 + 설계 문서"까지는 갱신했지만, **그 식을 사람에게
  설명하는 표면**(예시 env 주석, 기동 실패 메시지의 복구 안내)을 같은 갱신 단위로 보지 않았다.
  가드는 **좁아지는 방향**으로 바뀌었기 때문에, 낡은 안내는 단순 stale 이 아니라 **실패하는
  설정을 안전하다고 권하는** 안내가 된다.
- 규칙: 기동 검증식(예산·계수)을 **좁히는** 변경은 같은 PR 에서 ①`.env.example` 등 그 규칙을
  서술한 운영자 문서 ②실패 시 나가는 `recovery`/오류 문구 를 함께 좁힌다. 손잡이를 안내 문구에
  추가할 때는 **그 손잡이가 실제로 무엇을 바꾸는지와 문장의 동사가 일치하는지** 확인한다(계수를
  낮추는 손잡이를 "disable" 목록에 넣지 않는다). 새 상한을 실제 값으로 한 번 시뮬레이션해
  거절/통과 경계를 확인하는 것도 함께(이번엔 3.4s 거절·3.3s 통과로 실측했다).
- 덧: `docs/api-spec.md` §2.9(c) 타임아웃 기준표의 I-1 재시도 행에 있는 "Spring 직렬
  구간을 `2 × 3s = 6s` 로 묶는다" 서술도 같은 이유로 실측(3단)과 어긋나 있으나, **정본
  개정은 사람 승인 게이트라 이 PR 범위 밖으로 남겼다** — 후속 이슈 대상.
- 덧(R5): 새 항을 식에 더할 때는 **그 항이 기존 항과 같은 값 매김을 받는지**(재시도 억제
  여부 등)까지 확인한다 — 계수를 고치면서 값 매김을 균질하게 가정해 같은 과소평가를 항
  하나에서 되풀이했고, Claude PR 리뷰가 잡았다.
- 관련: `app/core/config.py::_deferred_first_event_i1_calls`·
  `::_require_search_retry_within_stream_budget`, `.env.example` 의 `SPRING_MAX_RETRIES`
  주석 블록, `docs/specs/MEASURE-FIRST-TOKEN-363.md` §5, 이슈 #383(#363 후속), 커밋 `b700e7e`

---

## [2026-08-06] fake 가 "표현 불가"를 예외로 던지면, 앱이 그걸 삼켜서 INV 비교가 "둘 다 실패"로 공허 통과할 수 있다
- 증상: #381 에서 `RecordingFilteringSearch`(combo_matrix eval 하네스)가 keyword·color·
  attr_conditions 처럼 흉내 낼 수 없는 필터가 present 면 "조용히 무시하지 않겠다"는 의도로
  `ValueError` 를 던지게 해 뒀다(#371 결정). 그런데 앱은 그 예외를 검색 실패로 삼켜
  `terminal=error`/`errorCode=SEARCH_FAILED` 로 낙성했고, INV 쌍 검증(base=rerank 성공 ·
  perturbed=rerank_failed)은 **둘 다 이 상태로 우연히 동일**해 "불변식이 성립한다"고 pass 했다
  (`combo-0055`, 실측: 두 arm 모두 `productIdsMultiset: []`). `pair_checks.jsonl` 의 커밋된
  `reason` 문구는 심지어 그 상태의 실측과도 어긋나는 값(`[101,102,103,104]`)을 적고 있었는데도
  같은 이유로 아무도 못 잡았다 — 비교 대상 자체가 항상 "같은 실패"로 수렴해서다.
- 원인: "표현 불가 축은 조용히 무시하지 말고 시끄럽게 실패시키자"는 의도 자체는 맞았지만,
  **누구에게 시끄러운가**를 안 물었다. 예외를 던지면 그 fake 를 부르는 앱 코드의 관점에선 그냥
  "검색 실패"라는 하나의 알려진 실패 모드로 흡수되고, 그 실패 모드는 서로 다른 두 실행(base·
  perturbed)에서 **값과 무관하게 항상 같은 결과**를 낸다 — 비교 자체가 무의미해지는데 겉보기엔
  "성립"으로 보인다. "단언이 상수라 못 깨진다"는 흔한 공허 통과와는 결이 다르다 — 여기서는 단언
  자체는 정상인데 **비교 대상 두 값이 실행 중에 같은 예외로 수렴**해서 공허해졌다 — 정적
  분석(상수 리터럴 찾기)으로는 안 잡히고 실행해서 값을 봐야 드러난다.
- 규칙: fake 가 "이 축은 흉내 못 낸다"를 표시해야 하면, **앱의 정상 실패 경로로 새게 만들지
  말고 관측 데이터에 별도 필드로 기록**한다(예: `unapplied_calls`/`unappliedSearchFilters`) —
  실행은 계속하게 둬서 비교 대상이 실제 값으로 갈라질 여지를 남긴다. 예외를 던지는 게 유일한
  옵션처럼 보이면, 그 예외가 도달하는 곳(catch 블록)이 비교하는 두 실행 모두에서 같은 도착지인지
  먼저 확인하라 — 같다면 그 예외는 "시끄러운 실패"가 아니라 "조용한 동일화"다.
- 관련: #371, #381, `evals/combo_matrix/fakes.py::RecordingFilteringSearch`,
  `evals/combo_matrix/expected/pair_checks.jsonl`(combo-0055),
  [[2026-08-06] eval 하네스가 "이 축을 잰다"고 문서에 쓰려면 주입값이 아니라 실제 도달값을
  실측해야 한다] 와 같은 #371/combo_matrix 계열 발견

## [2026-08-06] 함수 시그니처를 바꿀 때 호출부 grep 을 `tests`·`evals` 로만 하면 `scripts/` 가 사각지대다
- 증상: #396(이슈)/PR #407 에서 `_prepare_recommendation` 을 코루틴 → async generator 로
  바꾸고 키워드 전용 필수 인자 `out` 을 추가했다. 그때 "`tests/`·`evals/` grep 0건"을
  근거로 "다른 호출부 없음"이라 판단했는데, `scripts/capture_i1_wire_132.py:63`·
  `scripts/verify_regression6_217.py:78` 두 곳이 여전히 `await _prepare_recommendation(...)`
  (구 코루틴 형태, `out=` 없음)로 남아 있었다. 방치했으면 `out` 누락으로 즉시
  `TypeError`, `out=` 만 채워도 async generator 를 `await` 해 또 다른 `TypeError`(제너레이터
  객체만 만들어지고 body 는 한 줄도 안 돈다)로 죽는 상태였다. `pytest` 가 `scripts/` 를
  실행하지 않아 CI 전 구간이 초록이었고, Claude PR Review 가 인라인으로 잡았다.
- 원인: 호출부 조사를 `tests/`·`evals/` 로만 했다. 이 저장소는 실측·회귀 검증을
  `scripts/` 의 일회성 스크립트로 남기는 관행이 있고(`verify_regression6_217.py` 는
  docstring 에 "프로덕션 `_prepare_recommendation` 을 **그대로 호출**한다"고 명시까지
  해뒀다), 그 디렉터리가 pytest 실행 범위 밖이라 사각지대가 됐다.
- 규칙: **함수 시그니처·호출 규약(코루틴↔제너레이터 전환 포함)을 바꿀 때 호출부 grep 은
  저장소 전체**로 한다(`app`·`tests`·`evals`·`scripts`·`docs`). 특히 **pytest 가 실행하지
  않는 `scripts/` 는 CI 가 지켜주지 않으므로** grep 결과를 눈으로 확인하고, 가능하면
  스크립트를 실제로 한 번 돌려본다.
- 관련: #396, PR #407, `app/agents/buyer/graph.py::_prepare_recommendation`,
  `scripts/capture_i1_wire_132.py`, `scripts/verify_regression6_217.py`

## [2026-08-06] 진단용으로 로그에 싣는 예외 메시지에는 "검증 이전" 값이 섞여 있다
- 증상: #408 에서 401 사유를 남기려고 `__cause__` 체인의 `str(exc)` 를 그대로 로그 문자열에
  이어붙였다. 그런데 PyJWT 의 `PyJWKClientError` 메시지는 JWT 헤더의 `kid` 를 그대로 싣는다
  (`Unable to find a signing key that matches: "<kid>"`). `kid` 는 **서명 검증 이전**에 읽는
  값이라 공격자가 유효 서명 없이 임의 문자열을 넣을 수 있고, 개행이 그대로 나가면 로그에
  가짜 `auth rejected ...` 줄을 심을 수 있다(로그 인젝션, CWE-117). dev 모드는 서명을 아예
  안 보므로 `unknown sub_type: <값>` 도 같다. 길이 상한(`[:200]`)은 개행을 막지 못한다.
- 원인: "예외 메시지는 라이브러리가 쓴 문장"이라고 전제했다. 실제로는 **입력이 보간된 문장**
  이고, 그 입력이 신뢰 경계의 어느 쪽인지는 예외마다 다르다. 로그에 싣기로 한 순간
  PII 검토(무엇을 안 남길까)는 했지만 무결성 검토(누가 이 문자열을 쓸 수 있나)는 빠졌다.
- 규칙: **인증 실패 경로의 값을 로그에 싣을 때는 비출력 문자를 이스케이프한다.** 특히 서명·서버
  검증을 통과하기 *전에* 읽히는 값(JWT 헤더 `kid`·`alg`, dev 모드 클레임, 헤더/쿼리 원문)은
  전부 공격자 제어로 간주한다. 로그 필드의 검토 항목은 두 개다 — ①비밀/PII 를 안 싣는가
  ②남의 입력이 로그 **구조**를 바꿀 수 있는가.
- 덧: **이스케이프 대상을 손으로 열거하지 마라.** 1차 수정은 C0(0x00–0x1F)+DEL 표를 직접
  적었는데, 리뷰 2라운드가 NEL(U+0085)·LINE SEPARATOR(U+2028)·PARAGRAPH SEPARATOR(U+2029)가
  통과한다고 지적했다 — 뷰어·JS 파서는 이것들도 개행으로 읽는다. 열거는 언제나 부분집합이
  된다. 표준 판정(`str.isprintable()` = Cc·Cf·Zl·Zp·Zs 를 비출력으로 봄)을 쓰면 양방향
  재정의(U+202E) 같은 미래의 변종까지 자동으로 걸린다. 한글은 Lo 라 그대로 남는다.
- 관련: #408, PR #409 Claude 리뷰, `app/api/deps.py::_reason_chain`·`_CONTROL_ESCAPES`,
  `tests/unit/test_auth_401_reason_log.py::test_401_log_escapes_attacker_controlled_control_chars`

## [2026-08-06] 의존성 함수에 파라미터를 더하면 그게 곧 공개 계약이다 — keyword-only 도 예외가 아니다
- 증상: #408 에서 401 로그에 어느 의존성을 거쳤는지 싣으려고 `get_identity` 에
  `*, dependency: str = "get_identity"` 를 붙였다. 내부 표식이라 와이어와 무관하다고 생각했는데,
  FastAPI 는 `inspect.signature` 로 파라미터를 훑으면서 **KEYWORD_ONLY 를 구분하지 않는다** —
  그대로 뒀으면 `/chat`·`/seller/chat`·`/profile/me` 에 `?dependency=` 쿼리 파라미터가
  생기고 OpenAPI 에도 노출됐다(계약 무변경 이슈에서 계약이 바뀔 뻔했다).
- 원인: "의존성 함수의 시그니처 = 요청 파싱 명세"라는 것을 내부용 인자에는 적용하지 않았다.
  Python 문법상의 사적임(keyword-only·언더스코어 접두)은 프레임워크에 아무 신호도 주지 않는다.
- 규칙: **의존성 함수 시그니처에는 요청에서 오는 것만 둔다.** 내부 컨텍스트가 필요하면 파라미터
  대신 **private 헬퍼로 분리해 호출부에서 넘긴다**(`_identity_or_401(..., dependency=...)`).
  요청 객체가 필요할 때는 `request: Request = None` 으로 두면 FastAPI 주입은 그대로 받으면서
  의존성 밖 직접 호출(단위 테스트)도 깨지지 않는다 — `Request` 타입은 필드가 아니라 특수
  주입으로 처리돼 기본값이 무시되고 쿼리 파라미터로도 새지 않는다. 다만 이건 **추측하지 말고
  `app.openapi()` 로 실측**할 것(파라미터 목록에 새 항목이 없어야 한다).
- 관련: #408, `app/api/deps.py::get_identity`·`::require_seller`·`::verify_service_token`,
  FastAPI 0.139 `analyze_param`

## [2026-08-06] 소비자 없는 필드의 "안전해 보이는" 기본값은, 소비가 붙는 순간 주장으로 승격된다
- 증상: PR #305 가 `WishlistItem.purchase_state` 를 선언하며 기본값을 `"AVAILABLE"` 로 뒀다.
  그때는 읽는 코드가 0건이라 아무 해도 없었다. 그런데 #310 이 이 값을 읽어 "품절이에요"를
  가르기 시작하자, **키가 오지 않은 응답이 "구매 가능이 확인됨"으로 읽히게** 됐다 — BE 가
  아직 필드를 안 내려주는 구간에서 품절 상품을 "살 수 있다"고 안내하는 fail-open 이다.
- 원인: 기본값을 정할 때의 기준이 "구 필드(`purchasable=true`)와 의미를 맞춘다"였다. 구 필드와의
  하위 호환은 **소비자가 있을 때만** 의미가 있는데, 소비자가 없으니 그 기준이 검증되지 않은 채
  통과했다. 미수신(키 없음)과 명시적 값은 **다른 사실**인데 기본값이 둘을 같은 것으로 뭉갠다.
- 규칙: **BE 가 아직 안 내려주는 필드의 기본값은 "구 필드와 의미 동일"이 아니라 "아직 아무것도
  주장하지 않음"(`None`)으로 둔다.** 소비를 붙이는 이슈에서 승격 여부를 정한다. 이 저장소는 이미
  같은 구분을 명문화해 뒀다 — `spring_client.py::_envelope_success_false` 의 *"`success` 키가
  없으면 false 로 간주하지 않는다 — '명시 안 됨'과 '명시적 false'는 다른 사실이다"*. 선언만 하고
  소비를 미루는 PR 은 그 자리에 "소비가 붙을 때 기본값을 재검토하라"는 주석을 남긴다(#305 는
  실제로 남겼고, 그래서 #310 이 놓치지 않았다).
- 덧: **미지 값(계약 밖 enum)의 처방은 흐름마다 다를 수 있다.** 찜은 항목 단위 skip 이 맞지만
  장바구니는 관대 강등(필드만 `None`)이 맞다 — 장바구니 항목이 목록에서 조용히 사라지면
  "전부 빼줘"가 일부만 지우고 성공을 보고한다. 원칙("거짓 안내를 막는다")이 같아도 파괴적
  후속 동작의 유무에 따라 최적 수단이 갈린다. 대칭이 곧 정답은 아니다.
- 관련: #310, #305, `app/schemas/spring.py::CartViewItem`·`::WishlistItem`·
  `::_degrade_unknown_purchase_state`, `app/services/spring_client.py::_parse_wishlist_items`,
  SPEC-CART-001 REQ-CART-037, api-spec §4.9 v0.26.3

---

## [2026-08-06] 전용 검증자를 일반형으로 흡수할 때 판단 기준이 바뀌면, 그 간극을 메우는 이음매 검증자에도 변이 시험이 필요하다
- 증상: #313 에서 그룹별 전용 검증자 둘을 일반형 매핑 하나로 흡수하면서, 두 검증자의
  **판단 기준이 서로 달랐다**(삭제한 #300 검증자는 `includeScreen` **플래그** 기준, 새 매핑은
  contextId **문자열** 기준). 그 간극을 메우려고 `ProbeContext._include_screen_matches_context_id`
  이음매 검증자를 신설했는데, **그 검증자에는 테스트가 하나도 붙지 않았다** — 리뷰에서 변이
  시험(본문을 `return self` 로 무력화)을 돌리자 130개 테스트가 전부 초록으로 통과해 드러났다.
- 원인: 신규 테스트를 "이슈가 요구한 조작 목록"에만 맞춰 썼기 때문이다. 이음매 검증자는
  이슈 본문이 요구한 항목이 아니라 **흡수 과정에서 파생된** 것이라 완료조건 체크리스트
  어디에도 없었고, 전체 스위트가 초록이라 부재가 보이지 않았다.
- 규칙: **검증자를 일반형으로 흡수·통합할 때 기준(플래그 vs 문자열 등)이 바뀌면 그 간극을
  메우는 이음매 검증자를 반드시 함께 만들고, 신설한 검증자마다 본문을 `return self` 로
  무력화해 실제로 실패하는 테스트가 있는지 변이 시험으로 확인한다.** 전체 스위트가 초록인
  것은 새 검증자가 지켜지고 있다는 증거가 아니다 — 아무도 안 지키는 코드도 초록이다.
  완료조건 체크리스트를 채운 것과 변경이 테스트로 고정된 것은 다른 사실이다.
- 관련: #313, #300, `evals/intent_probe/schema.py`, `tests/unit/test_intent_probe_fixtures.py`

---

## [2026-08-06] 데이터가 새 코드 경로를 처음 태우면, 게이트가 깨져도 범인은 앱이 아니라 하네스일 수 있다
- 증상: #370 이 골든셋에 처음으로 유의미한 수(47건)의 가격 위반 후보를 주입하자
  `tests/eval/test_goldenset_eval.py` 의 critical PR 게이트가 갑자기 깨졌다. 표면적으로는
  "앱이 하드 제약을 위반한 상품을 노출한다"로 읽혔다.
- 원인: 앱 결함이 아니라 eval 하네스의 mock 충실도 격차였다. `evals/metrics/harness.py` 의
  `_CaseTransport` 가 Spring `/internal/products/search` 를 mock 하면서 요청의
  `minPrice`/`maxPrice` 를 무시하고 fixture 후보를 전부 돌려줬다. 실 서비스는
  `app/services/spring_client.py` 가 그 파라미터를 I-1 에 실어 **Spring 이 서버사이드로**
  거른다(앱의 로컬 `within_price_range` 는 인기상품 폴백 경로 전용이라 이 경로를 타지 않는다).
  #333 의 기존 `price_violation` 채널이 실측 0% 에 가까워서 이 격차가 한 번도 발현된 적이
  없었다 — 데이터가 그 코드 경로를 태우지 않는 동안은 하네스가 틀려도 아무도 모른다.
- 규칙: eval 하네스의 fake 외부 서비스는 "앱이 실제로 보낸 요청 파라미터"를 기준으로 실
  서비스 동작을 흉내내야 하며, 새로운 실패 모드를 데이터로 넣을 때는 그 실패 모드를 판정하는
  경로가 하네스에서 실제로 살아 있는지 먼저 확인한다. 통과하던 게이트가 데이터 추가 후
  깨지면 앱을 고치기 전에 하네스가 실서비스와 다른지부터 확인한다(반대로 고치면 실서비스에
  없는 로직을 앱에 심게 된다). 케이스의 정답 라벨(`hardConstraints`)로 mock 을 거르면 안
  된다 — 그러면 decompose 가 필터를 놓치는 진짜 실패 모드를 영원히 못 잡는다.
- 관련: #370, #333, `evals/metrics/harness.py::_CaseTransport`,
  `app/services/spring_client.py`, `tests/eval/test_goldenset_eval.py`

---

## [2026-08-06] eval 하네스가 "이 축을 잰다"고 문서에 쓰려면 주입값이 아니라 실제 도달값을 실측해야 한다
- 증상: #371(combo_matrix INV/DIR 쌍 실검증 러너) 작업 중, `evals/combo_matrix/README.md` 가
  "category 필터축은 `ProductSearchFilters.category`(하드필터 문자열)만 잰다"고 적어 놨는데,
  `searchFilters` 프로젝션을 처음 실측 캡처해 보니 combo-0054(DIR, category 필터 추가) 의
  base·perturbed 양쪽 `searchFilters.category` 가 **둘 다 항상 None** 이었다 — decompose 산출
  JSON 에 `filters.category="무선이어폰"` 을 분명히 채웠는데도 검색 호출에는 한 번도 도달하지
  않았다.
- 원인: `app/agents/buyer/graph.py:520-537` 의 canonical-or-null degrade 가
  `decision.category_legs` 가 비면 `decision.filters.category` 를 무조건 `None` 으로 지운다
  ("미검증 원문이 Spring 검색으로 새지 않게" — 프로덕션 정상 설계, 버그 아님). `category_legs`
  는 오직 decompose 의 `categoryQueries`→`map_categories` 매핑으로만 채워지는데,
  `evals/combo_matrix/runner.py::build_decompose_json` 은 `categoryQueries` 를 한 번도 채우지
  않고 `fakes.map_categories_noop` 은 항상 빈 legs 를 돌려준다 — 그래서 `filters.category` 를
  아무리 채워도 항상 지워졌다. `category` 축은 #335 의 기존 55건 MFT 케이스를 포함해 이 하네스
  전체에서 **처음부터 실제 검색 경계에 도달한 적이 없었는데**, 아무도 `searchFilters` 자체를
  캡처한 적이 없어(#119 관측 로그는 축 **이름**만 봄, 값이 실제로 필터에 실렸는지는 안 봄) 지금까지
  드러나지 않았다.
- 규칙: **eval 하네스 문서에 "이 축을 잰다"고 쓰려면, 그 축이 파이프라인 경계(예: search 콜러블이
  실제로 받는 인자)까지 도달하는지를 실측 캡처로 확인하고 나서 쓴다** — 주입한 decompose/입력
  값이 아니라 **경계 도달값**을 봐야 한다. 특히 canonical-or-null 처럼 "원본을 재검증 없이는
  못 믿어 지운다"는 설계(§20·§115 계열)가 있는 필드는, 상류에서 값이 있어 보여도 하류의 신뢰
  게이트를 통과하지 못하면 조용히 null 이 된다 — fake/stub 이 그 신뢰 게이트를 만족시키는지
  (여기서는 legs 매핑) 별도로 확인해야 한다. 이 발견은 `pair_runner.py` 전용 seam(exact-match
  카테고리 매핑 fake)으로 그 쌍 하나만 고쳤고, 기존 55건의 잔여 맹점은 후속 이슈로 남겼다 —
  구조 변경이 필요한 발견은 코드를 먼저 고치지 말고 오케스트레이터에게 보고하고 결정을 받았다.
- 관련: #371, `app/agents/buyer/graph.py:520-537`·`:349`, `evals/combo_matrix/runner.py::build_decompose_json`,
  `evals/combo_matrix/fakes.py::map_categories_noop`·`make_exact_match_category_mapping`(신규),
  `evals/combo_matrix/README.md` "알려진 관측 한계" 절 정정

---

## [2026-08-06] degrade 주입 fake 가 실제 어댑터의 실패 규약과 다른 예외 타입을 던지면 관측·이슈가 인공물을 잰다
- 증상: #335 매트릭스가 `wishlist_add × spring_timeout` 셀에서 "`SpringUnavailableError` 미처리로
  INTERNAL 로 샌다"를 관측했고 이슈 #368 이 그 관측을 근거로 열렸는데, PR #374 리뷰에서 실제
  `add_wishlist` 어댑터(I-26)는 그 예외를 **한 번도 내지 않는다**는 것이 드러났다. 관측된 예외는
  러너가 주입한 fake(`evals/combo_matrix/runner.py:177-184`)가 던진 것이었다.
- 원인: degrade 주입 fake 가 **그 어댑터의 실제 실패 규약과 다른 예외 타입**을 던졌다. 조회 계열
  (`get_cart`/`get_wishlist`)은 `SpringUnavailableError`, 변경 계열(`add_to_cart`/`add_wishlist`)은
  `CartError`/`WishlistError` 로 규약이 **갈리는데** 주입은 한 타입으로 통일돼 있었다. 그래서 그 축의
  관측은 실제 프로덕션 경로가 아니라 fake 의 인공물을 쟀다.
- 규칙: degrade·실패 주입 fake 를 만들 때는 **그 함수의 실제 실패 규약(어댑터 docstring·raise 문)을
  먼저 확인**하고 같은 예외 타입으로 던져라 — 성공 fake 의 반환 스키마를 맞추는 것(아래 "성공 fake"
  가 실 스키마와 다른 모양이어도 게스트 게이트 뒤에 있으면 영원히 안 드러난다 항목)과 같은 규칙의
  실패 경로 버전이다. 그리고 그런 관측에서 유도한 이슈는 **본문의 근거 문장을 어댑터 실측으로
  재검증한 뒤** 코드 주석에 옮겨라 — 주석은 다음 사람이 계약을 배우는 자리라 틀린 근거가 그대로
  학습된다(PR #374 에서 실제로 주석·docstring·CHANGELOG 3곳에 오기재로 퍼졌다).
- 관련: #368, PR #374, `app/services/spring_client.py::add_wishlist`(I-26)·`::get_wishlist`(I-28),
  `evals/combo_matrix/runner.py::_observe_chat`(담기 어댑터 주입부), `app/agents/buyer/cart/graph.py::stream_cart_add`

---

## [2026-08-06] 평가 하네스는 측정 대상이 "내부에서 흡수한" 인프라 실패를 정상 오답 표본으로 센다
- 증상: #331 카테고리 프로브 리뷰에서 `search_top_k` 가 항상 `TimeoutError` 를 던지도록 한
  재현을 돌렸더니 `filled=True · samples=1 · failures=0 · legs=[]` 가 나왔다 — pg 가 전면
  장애인데 러너는 그것을 "매핑이 카테고리를 못 냈다"는 **정상 오답 표본**으로 세어 분포에
  섞었다. `failures.csv` 에도 남지 않아 산출물만 보고는 사후 식별조차 불가능했다. 즉 인프라
  순간 장애가 그대로 "매핑 정확도 하락"으로 보고될 수 있었다.
- 원인: 러너가 "실패"를 **함수 밖으로 전파된 예외**로만 정의했는데, 측정 대상인
  `map_categories` 는 설계상 실패를 삼키고 degrade 한 결과를 **정상 반환**한다(canonical-or-null
  #20·#115 — 카테고리는 선택 필터라 매핑이 죽어도 검색은 계속돼야 하므로 그 자체는 옳은
  동작이다). **배포 코드가 견고할수록 하네스는 그 실패를 못 본다**는 역설이고, 실패 신호는
  반환값이 아니라 구조화 로그(`category_leg_search_failed`·`category_embed_failed` 등)에만
  있었다.
- 규칙: **비-예외 degrade 를 하는 함수를 재는 하네스는 "예외가 없었다"를 성공으로 삼지 마라.**
  측정 대상이 남기는 인프라 실패 이벤트를 캡처해 그 시도를 표본에서 빼고 재시도하며, 실패
  레코드로 산출물에 남긴다(#260 "실패는 표본이 아니다" 규약의 확장). 단, **정책적
  degrade**(예산 상한 `max_calls`·LLM 미구성 등 배포의 정상 동작)와 **인프라 실패**는 이벤트
  `reason` 으로 갈라라 — 뭉뚱그리면 이번엔 반대로 정상 동작 표본을 버려 분모가 왜곡된다. 새
  프로브를 만들 때 **측정 대상의 try/except 를 먼저 읽고 "이 함수가 무엇을 삼키는가" 목록을
  만드는 것이 첫 수순**이다.
- 관련: #331, PR #373, `evals/category_probe/runner.py`(`_INFRA_FAILURE_EVENTS` ·
  `_SELECT_UNAVAILABLE_POLICY_REASONS` · `_infra_failure_event`),
  `app/agents/buyer/recommendation/category_mapping.py`(gather `return_exceptions=True` ·
  단계별 try 격리), `evals/README.md` 3항

---

## [2026-08-06] 임시 수정 원복을 문자열 치환("첫 매치")으로 하면 나란히 있는 동형 fixture 를 바꿔친다
- 증상: #372 리뷰 라운드 1 검증 중, 테스트가 공허 통과가 아닌지 확인하려고
  `tests/unit/test_underspecified_answer_turn.py` 의 A-1 fixture(`_CATEGORY_ANSWER_DECOMPOSE`)
  에서 `categoryQueries` 를 임시로 비웠다가, 복원할 때 `str.replace(old, new, 1)` 로 되돌렸다.
  그런데 되돌릴 패턴(`"categoryQueries": [],\n    "filters": {"priceMax": 50000},\n}`)이 **바로
  위의 다른 fixture(`_PRICE_MAX_DECOMPOSE`)와 완전히 동일**했다 — 첫 매치가 그쪽이라, 복원이
  엉뚱한 fixture 에 카테고리를 심고 원래 fixture 는 비운 채로 남겼다. 두 fixture 가 동시에
  잘못된 상태가 됐는데 **테스트는 그래도 통과**했다(A-1 의 1턴이 과소지정이 아니게 됐는데도
  되물음 단언이 `or "이어폰" in t` 폴백으로 초록이었다). `git status` 도 신규(untracked) 파일이라
  `git checkout` 으로 되돌릴 수 없었고, diff 로도 드러나지 않았다. 눈으로 fixture 를 다시 읽고서야
  발견했다.
- 원인: 테스트 fixture 파일은 **비슷한 dict 리터럴이 여러 개 나란히 있는 게 정상**이라, 문자열
  치환의 "첫 매치"가 의도한 그 fixture 라는 보장이 없다. 원복 확인도 "테스트가 다시 초록이다"
  로만 했는데, 단언에 `or "이어폰" in t` 같은 관대한 폴백이 섞여 있으면 fixture 가 뒤바뀐
  상태에서도 전체가 초록으로 나온다 — 통과가 "원복이 맞다"를 보증하지 않는다.
- 규칙: 임시 수정→원복은 **문자열 치환으로 하지 말고** 원본 사본을 떠 두고 파일째 되돌려라
  (`cp <파일> <파일>.bak` 후 자가 검증 → `cp <파일>.bak <파일>` 로 복원 — `mv`/`cp` 는 신규
  untracked 파일에도 `git checkout` 과 달리 그대로 통한다). 원복 후에는 **테스트 통과만으로
  확인하지 말고 해당 지점을 눈으로 다시 읽어 확인하라** — 특히 단언에 `or` 폴백이 섞여 있는
  테스트는 fixture 가 틀려도 초록일 수 있다.
- 관련: #372 리뷰 라운드 1, `tests/unit/test_underspecified_answer_turn.py`
  `_CATEGORY_ANSWER_DECOMPOSE`/`_PRICE_MAX_DECOMPOSE`

---

## [2026-08-06] "성공 fake" 가 실 스키마와 다른 모양이어도 게스트 게이트 뒤에 있으면 영원히 안 드러난다
- 증상: #335 리뷰 R8(order_status×spring_timeout 실측 추가) 작업 중, 기존
  `evals/combo_matrix/fakes.py::make_order_status_ok` 가 `{"orderId": ..., "status": ...}` 같은
  무관한 dict 를 돌려주고 있었다 — 실제 소비 코드(`app/agents/buyer/order_status.py`
  `format_order_status`)는 `summary.orders`(리스트 속성)를 읽는다. `AttributeError` 가 나야
  정상인데, 커밋된 order_status 케이스는 전부 `identity=guest` 라 `member_order_identity` 가
  `fetch_order_status` 호출 자체를 게이트로 막아 이 fake 가 실제로는 **단 한 번도 실행되지
  않았다** — 그래서 몇 라운드의 리뷰·테스트를 거치는 동안 아무도 이 결함을 못 봤다.
- 원인: "성공 경로 fake"를 실 반환 스키마(Pydantic 모델 등) 검증 없이 손으로 지어낸 dict 로
  때웠고, 그 fake 가 게스트/미인증처럼 **더 이른 게이트가 걸리는 조합에서만** 커버리지가
  있었다 — 실행이 "안 죽었다"는 사실이 "fake 가 맞다"를 보증하지 않는다.
- 규칙: 콜러블을 fake 로 주입할 때는 그 반환값을 **소비하는 코드가 실제로 읽는 속성**을 실
  스키마(가능하면 실제 Pydantic 모델 인스턴스)로 만족시켜라 — 임시 dict 는 "일단 안 죽으면
  맞다"는 착시를 준다. 그리고 그 fake 가 **성공 경로까지 실제로 도달하는 identity/조건** 조합의
  케이스가 최소 1건 있는지 확인하라(게스트·미인증 전용 케이스만 있으면 성공 fake 자체가
  검증된 적이 없다) — #335 의 cart/wishlist 계열에서 이미 같은 패턴(웜업·숫자 user_id 누락)을
  겪었으니, 이 부류의 fixture 는 항상 "실 스키마 + 실제로 그 경로에 도달하는 identity" 둘 다
  갖췄는지 짝지어 점검한다.
- 관련: #335, `evals/combo_matrix/fakes.py::make_order_status_ok`,
  `app/agents/buyer/order_status.py::format_order_status`, `app/schemas/spring.py::OrderStatusSummary`

---

## [2026-08-06] cart/wishlist fake identity 는 숫자 문자열이어야 한다 + 담기 fake 는 직전 추천을 먼저 채워야 한다
- 증상: #335 리뷰 R3(`wishlist_add×member×spring_timeout` 직접 관측 케이스 추가) 작업 중, 회원
  identity 로 wishlist_add 를 실행해도 매번 "찜에는 로그인이 필요해요"만 나왔다 —
  `identity.is_guest=False` 로 만들었는데도 게스트와 똑같이 처리됐다. 그다음엔 "어떤 상품을
  찜할까요?" 되물음만 나왔다 — degrade(SpringUnavailableError) 를 주입해도 그 코드에 전혀
  안 닿았다.
- 원인: ① `app/agents/buyer/cart/identity.py::cart_identity` 는 `int(identity.user_id)` 파싱에
  실패하면(예: `"combo-0057"` 같은 비숫자 문자열) `ValueError` 를 흡수하고 (None, None) 을
  돌려줘 **회원을 게스트/익명과 구분 없이** 취급한다 — 회원 fake identity 의 `user_id` 는
  **숫자 문자열**이어야 한다. ② `app/agents/buyer/graph.py:994-1019` 의 담기 허용목록
  (`allowed_product_ids` = 직전 추천 ∪ screen.products)은 그 안에 없는 상품을 조용히 되물음으로
  돌린다 — cart_add/wishlist_add 를 fake 로 구동하려면 **같은 thread_id 로 먼저 recommend 턴을
  1회 태워 대상 productId 를 직전 추천에 올려야** Spring 호출부(add_to_cart_fn/add_wishlist_fn)
  에 실제로 도달한다.
- 규칙: cart/wishlist 계열을 fake 로 단위 테스트할 때 ① `Identity.user_id` 는 회원이면 숫자
  문자열(`str(int)`)로 채운다(비숫자면 `cart_identity` 가 조용히 익명 취급 — 예외도 안 던진다).
  ② cart_add/wishlist_add 관측 전에는 같은 identity·thread_id 로 정상 recommend 웜업 턴을 먼저
  실행해 last_reco 를 채운다 — 웜업 없이 degrade 를 주입하면 그 축은 절대 그 코드에 도달하지
  못한 채 매번 같은 되물음만 관측된다(관측이 "항상 똑같다"면 이 두 가지부터 의심).
- 관련: #335, `evals/combo_matrix/runner.py::_identity_for`·`_warm_up_last_reco`,
  `app/agents/buyer/cart/identity.py`, `app/agents/buyer/graph.py:994-1019`

---

## [2026-08-06] 결정론 생성기에서 `hash(str)`을 seed 파생에 쓰면 PYTHONHASHSEED 랜덤화로 재현성이 깨진다
- 증상: #335 pairwise 케이스 생성기(`evals/combo_matrix/generator.py`)를 같은 `axes.json`+같은
  seed 로 연속 두 번 돌렸는데 `combo_cases.jsonl` 의 sha256 이 매번 달랐다 — 케이스 순서·내용
  자체가 프로세스마다 달라지는 재현성 결함이었다.
- 원인: 위험 3-wise 축쌍마다 별도 `random.Random` 시드를 파생시키며 `doc.seed ^ hash(rt.id)`
  (`rt.id` 는 문자열)를 썼다. Python 은 문자열 `hash()` 를 **프로세스마다 무작위 솔트**로 계산한다
  (해시 충돌 기반 DoS 방지, PYTHONHASHSEED 미고정 시 기본 동작) — 그래서 같은 문자열도 프로세스마다
  다른 정수를 내고, 그 값으로 만든 `random.Random` 시드가 매번 달라 그 라운드의 탐욕 선택 결과가
  갈렸다. `random.Random(int)` 자체는 결정론이지만 **입력이 이미 비결정론**이었던 것.
- 규칙: **결정론이 요구되는 코드(생성기 seed 파생·캐시 키·해시 기반 정렬 등)에서 문자열을
  다이제스트할 때는 절대 내장 `hash()` 를 쓰지 않는다** — `hashlib.sha256(text.encode()).digest()`
  처럼 프로세스 불변인 안정 해시만 쓴다. `PYTHONHASHSEED=0` 로 환경을 고정하는 우회도 있지만,
  코드가 그 환경변수에 의존한다는 사실 자체를 감추므로 안정 해시가 근본 해결이다. 재현성을
  주장하는 코드를 작성/리뷰할 때는 `PYTHONHASHSEED=random uv run <재현 명령>` 을 최소 2회 돌려
  출력이 바이트 동일한지 실측하라 — 기본 랜덤 시드 그대로면 이런 버그가 세션 내내 숨는다.
- 관련: #335, `evals/combo_matrix/generator.py` `_stable_hash`(수정 후),
  `tests/eval/test_combo_matrix_eval.py::test_regeneration_matches_committed_cases_byte_identical`

---

## [2026-08-06] 머지 커밋 전에 conflict marker 잔존 여부를 grep으로 확인한다
- 증상: #333 Part 3 작업 중 repo 루트 `CHANGELOG.md`에서 `<<<<<<< HEAD`/`=======`/
  `>>>>>>> origin/dev` 충돌 표지 3줄이 그대로 커밋돼 있는 것을 발견했다(`git log -1 -- CHANGELOG.md`
  기준 `fdc4af0 Merge branch 'dev' into ...`에서 유입). 두 브랜치가 각자 `### Added`에 다른
  항목(#290, #116·#117)을 추가했을 뿐 실제로 내용이 충돌하지 않는 순수 additive 변경이었는데도,
  머지 시 표지를 지우지 않고 그대로 커밋해 `dev`/`main` 이력에 깨진 마크다운이 남았다.
- 원인: 이 프로젝트에 커밋 전 `<<<<<<<`/`=======`/`>>>>>>>` 리터럴을 잡는 pre-commit/CI 검사가
  없다(`conventional-pre-commit`은 메시지 형식만 본다). 사람이 머지 후 diff를 훑지 않으면
  마크다운 렌더링이 깨져도 아무 도구도 막지 않는다.
- 규칙:
  - **머지 커밋(특히 `--no-verify`로 훅을 건너뛴 경우) 직후 `git grep -n "^<<<<<<<\\|^=======\\|^>>>>>>>" -- '*.md'`
    로 잔존 표지를 확인한다** — 특히 `CHANGELOG.md`처럼 여러 브랜치가 동시에 append하는 파일.
  - 발견 시 내용이 additive(서로 다른 섹션/항목 추가)라면 표지만 제거하고 양쪽 내용을 모두
    보존한다 — 어느 쪽도 버리지 않는다.
- 관련: 커밋 `fdc4af0`, 이슈 #333 Part 3, `CHANGELOG.md`

## [2026-08-06] Google GenAI 배치 임베딩은 100건/요청 상한이 있다 — 청크 없이 부르면 데이터셋이 커지는 순간 깨진다
- 증상: `evals/scoring/snapshot_embeddings.py`가 골든셋 dev 질의 임베딩을 재생성하다가
  `google.genai.errors.ClientError: 400 INVALID_ARGUMENT ... at most 100 requests can be in
  one batch`로 실패했다. v1(31건)에서는 100 미만이라 한 번도 드러나지 않다가, v2.1(103건)로
  dev 케이스가 늘어나며 처음 노출됐다.
- 원인: `app/pipelines/embedding.py`의 `embed_texts()`가 `texts` 전체를 한 번의
  `embed_content(contents=list(texts), ...)` 호출로 보냈다 — Google `BatchEmbedContentsRequest`가
  요청당 100건까지만 허용하는 것을 코드가 몰랐다. 이 함수는 eval 스크립트뿐 아니라 §4.8 I-17
  운영 배치 경로도 공유하므로, search_doc 배치가 100건을 넘기면 프로덕션에서도 같은 방식으로
  깨질 수 있었다. **이 결함은 이번 이슈(#333 Part 3)의 소관인 `evals/**` 밖 — 발견·보고만 하고
  `app/pipelines/embedding.py` 자체는 원복했다**(오케스트레이터가 후속 GitHub 이슈로 이관 예정).
  이번 PR은 eval 전용 호출부(`evals/scoring/snapshot_embeddings.py`)에서만 청크로 대응했다.
- 규칙:
  - **외부 API에 리스트를 통째로 넘기는 코드를 새로 짜거나 건드릴 때는 그 API의 배치 상한을
    공식 문서에서 확인하고, 상한이 있으면 처음부터 청크 분할로 짠다** — "지금 입력이 작아서
    안 걸린다"는 근거가 되지 않는다(데이터가 자라면 반드시 걸린다).
  - **핸드오버가 소관 범위 밖(app/**)이라 지정한 파일에서 진짜 결함을 발견해도, 그 자리에서
    고치지 말고 발견·보고만 한다** — 소관 밖 수정은 다른 레인(#318 등)과 충돌 위험을 만든다.
    이 PR의 소관인 eval 경로에서 같은 문제를 우회 대응(청크 호출부 이동)하고, 원인 파일 수정은
    별도 이슈로 넘긴다.
- 관련: 이슈 #333 Part 3, `app/pipelines/embedding.py` `embed_texts()`(원복, 미수정),
  `evals/scoring/snapshot_embeddings.py`(청크 호출부 신설)

## [2026-08-05] 임의 순서 기준선을 두지 않으면 랭커가 개선인지 손해인지 모른다
- 증상: #275 조사에서 student(현행 6성분 스코어러) 오라클 상한을 탐색했더니(E2) "상한
  0.738210"이 나와 teacher(0.782943)에 근접하는 듯 보였다. 재현·반증(E4)하니 이 값은
  `recency` 성분에만 값을 몰아주고 나머지 실질 신호를 전부 0으로 만든 **축퇴 해**였다 —
  `ScoringBuyerAdapter` 가 `recency_by_product=None` 을 주입해 이 축이 항상 0(무주입 무력
  축)인데, `EvaluationSettings` 검증자는 "5개 양의 신호 가중치 중 하나 이상 양수"만 요구해
  이 축에 값을 몰아주는 시도를 걸러내지 못했다. 그 해의 순위는 dev search fixture 32/32 건이
  이미 productId 오름차순으로 기록돼 있어(`search_responses.json`) 커밋된 `passthrough`
  baseline 0.738210(`evals/scoring/baselines/dev-v1/comparison.md`)과 완전히 같았다 —
  즉 **"아무 순서나 그대로 둔 것"과 같은 값이었다.** 현행 튜닝된 스코어러(0.616852)는 그
  0.738210 보다 **0.121358 낮다**(paired bootstrap 95% CI [0.039814, 0.206934]).
- 원인: 오라클 탐색·teacher-fit 탐색 어느 쪽도 "탐색이 no-op 으로 수렴할 수 있는가"를
  자체적으로 배제하지 않았다. `passthrough` 를 "검색 순위 기준선"으로 잘못 해석해, 실제로는
  **임의 순서 기준선**인 그 값과 튜닝된 가중치를 직접 비교하지 않은 채 진행했다 — 그 결과
  현행 스코어러가 "아무것도 하지 않는 것보다 나쁘다"는 사실이 여러 리포에서 한 번도
  표면화되지 않았다.
- 규칙: 랭킹/스코어링 튜닝을 평가할 때는 **"아무 순서나 그대로 두는" no-op 기준선을 항상
  1급 baseline 으로 사전 등록**하고, 튜닝된 결과·오라클 상한·teacher 모두를 이 기준선과
  paired bootstrap CI 로 대조한다. 오라클 탐색 코드에는 "결과 순위가 no-op 과 최소 1케이스
  달라야 한다"는 축퇴 배제 제약을 항상 넣는다(무력한 축이 있다면 탐색 공간에서도 뺀다).
  fixture/골든셋이 productId 오름차순 같은 결정론적 순서로 저장돼 있다면 그 자체가 숨은
  no-op 후보임을 의심한다.
- 관련: #275, `docs/research/RESEARCH-TEACHER-275.md` §3, `docs/research/research-275-harness/e4_analyze.py`,
  `evals/scoring/baselines/dev-v1/comparison.md`, `evals/goldenset/fixtures/search_responses.json`

---

## [2026-08-05] 유닛 테스트가 로컬에 떠 있는 실 Spring 을 잡아 결과가 뒤집힌다 — 주입하지 않은 기본값은 하네스 경계 밖이다
- 증상: #330 문서 작업 중 **코드 무변경** 상태에서 `uv run pytest` 가 3건 실패로 뒤집혔다 —
  `tests/unit/test_fanout.py::test_empty_legs_clears_unvalidated_filters_category`,
  `tests/unit/test_recommendation.py::test_recommendation_without_repurchase_keeps_exact_exclusion[decompose0]`·
  `[decompose1]`. 같은 트리·같은 명령이 같은 날 오전에는 3376 passed 였고, clean base(6cec23a)에서도
  실패가 재현됐다(워커 stash 실측).
- 원인: `app/agents/buyer/graph.py:545` 의 `popular_fn = popular_fn or spring_client.get_popular_products`
  (#162 I-3)에서, `run_buyer_turn` 유닛 테스트들이 `search=`·`push_fn=`·`map_categories=` 는 fake 로
  주입하면서 **`popular_fn` 은 주입하지 않는다.** 문제의 턴은 decompose 산출이 조건 없음으로 판정돼
  (`is_no_condition_turn`) 인기 상품(I-3) 경로로 가는데, **localhost:8080 에 실 Spring(BE 개발 서버)이
  떠 있으면** I-3 이 실제로 성공해 조건 없는 턴이 검색을 생략한다 → 테스트의 `calls` 가 빈 배열 →
  `IndexError`. Spring 이 죽어 있으면 `popular_degraded` 로 검색 폴백을 타서 통과한다. 2026-08-05 오후
  다른 작업 레인이 BE 스택(jarvis-mariadb 컨테이너 + Spring 8080, health 200 확인)을 띄우면서 결과가
  뒤집혔다. CI(GitHub Actions)에는 Spring 이 없어 항상 통과한다 — **로컬에서만, BE 를 띄운 순간부터
  깨진다.** 검증: `SPRING_BASE_URL=http://localhost:59999 uv run pytest <3건>` → 3 passed, 전체
  스위트 → 3376 passed(2026-08-05 실측).
- 규칙:
  - **그래프 하네스 유닛 테스트는 네트워크로 나가는 콜러블 전부를 주입한다** — `search`·`push_fn` 만
    fake 고 `popular_fn` 이 기본값이면 그 테스트는 유닛이 아니라 로컬 환경(8080 에 뭐가 떠 있는가)
    의존이다.
  - **그래프에 외부 호출 파라미터를 새로 추가하면 기존 테스트 헬퍼에 그 fake 를 같이 추가한다** —
    #162 가 `popular_fn` 을 추가할 때 기존 fanout/recommendation 테스트는 그대로 뒀고, 그 결함은
    Spring 이 실제로 떠 있는 날에만 드러난다(잠복 flaky).
  - **로컬 pytest 가 코드 무변경으로 뒤집히면 코드 diff 가 아니라 환경부터 본다** — `docker ps` 와
    8080 health 확인이 첫 수순이다. 임시 우회는 `SPRING_BASE_URL` 을 죽은 포트로 돌려 CI 동등 조건을
    만드는 것.
  - 테스트 자체의 수정(`popular_fn` fake 주입)은 코드 변경이라 이 문서 레인(#330) 범위 밖 — 별도
    이슈로 처리한다.
- 관련: #162, PR #311, `app/agents/buyer/graph.py`(`popular_fn` 기본값), `tests/unit/test_fanout.py`,
  `tests/unit/test_recommendation.py`

---

## [2026-08-05] 거리 임계는 사전에 종속된다 — taxonomy·임베딩 모델·task_type 이 바뀌면 재측정 없이는 무효
- 증상: #222 라이브 실측(라이브 pg-catalog, leaf 1,007행)에서 `category_distance_max=0.22` 가
  협소 발화 20건 중 10건, 상품명 150건 골든셋 기준 90%를 드롭했다. `DESIGN-CATEGORY-HYBRID-59.md`
  §10 이 이미 "이 값은 임베딩 모델·task_type·사전(2,056 leaf)에 종속되며 재측정 없이는 무효"라고
  경고해 뒀는데, 사전이 구 taxonomy(2,056행)에서 현 라이브 taxonomy(leaf 1,007행)로 바뀐 뒤
  그 재측정이 아직 이뤄지지 않은 채로 남아 있었다.
- 원인: 임계값(코사인 거리)은 절대 상수가 아니라 "이 사전 + 이 임베딩 구성에서 정답과 오답이
  갈리는 경계"를 실측으로 고정한 값이다. 사전 행 수·표기 체계가 바뀌면 문서·앵커 분포 자체가
  달라져 종전 경계가 더 이상 유효하지 않다 — 코드는 아무 에러도 내지 않고 조용히 더 많은 leg 을
  드롭할 뿐이라(canonical-or-null degrade) 증상이 "품질 저하"로만 나타나 원인 추적이 늦어진다.
- 규칙: 카테고리·임베딩 사전(taxonomy)이 재시드되거나 임베딩 모델·task_type 이 바뀌면 거리·마진
  임계(`category_distance_max`·`category_distance_override_margin`·`category_select_margin_max`)
  를 **재측정 없이 그대로 쓰지 않는다.** 재측정은 이 PR 처럼 급한 기능 PR에 끼워 넣지 말고 **별도
  이슈로 분리**한다 — 임계 튜닝은 앵커 수십~수백 건의 실측을 요구해 기능 구현과 섞으면 두 변경의
  회귀 원인이 뒤섞인다.
- 관련: #222, `app/core/config.py` `category_distance_max` 근처 주석,
  `docs/specs/DESIGN-CATEGORY-HYBRID-59.md` §10 "튜너블 불변식"

## [2026-08-05] 카테고리 사전이 비어 있으면 매핑이 "조용히" 무필터로 degrade 한다 — 사전 의존 기능은 행 수부터 확인한다
- 증상: #222 작업 착수 시점에 로컬 `pg-catalog.categories` 가 **0행**이었다. 매핑
  (`map_categories`)은 canonical-or-null 불변식대로 히트 0건을 정상적으로 빈 legs 로 처리해
  에러 없이 무필터 검색으로 넘어갔으므로, 카탈로그가 비어 있다는 사실이 로그나 예외 어디에도
  드러나지 않았다. 오케스트레이터가 라이브 트리(leaf 1,007행)를 별도로 시드한 뒤에야 이 결함이
  드러났다.
- 원인: canonical-or-null degrade(#20·#115)는 "매핑이 실패해도 검색은 계속돼야 한다"는 설계
  의도대로 정확히 동작한 것이라 **버그가 아니다.** 문제는 그 정상 degrade 가 "카테고리 사전이
  통째로 비어 있다"는 훨씬 심각한 상태와 "이 발화는 매핑하기 어렵다"는 정상적인 개별 실패를
  **구분 없이 같은 신호**로 취급한다는 데 있다 — 전자는 이 기능 자체가 사실상 항상 무필터로만
  동작한다는 뜻인데, 증상은 후자와 똑같이 "품질이 낮다"로만 보인다.
- 규칙: `categories`·`category_search_pool` 처럼 사전(seed) 데이터에 의존하는 기능을 다루기
  전에는 **행 수를 먼저 확인**한다(`SELECT count(*) FROM categories`). 0행이거나 비정상적으로
  적으면 착수 전에 `~/inte-final/_sql`(정본 시드 소스) 등에서 먼저 시드하거나, 그 사실을 실측
  보고서에 명시해 "결과가 전부 무필터 degrade 였다"는 착각을 방지한다.
- 관련: #222, `app/agents/buyer/recommendation/category_mapping.py` canonical-or-null 불변식

## [2026-08-05] 병합 충돌 마커가 dev 에 커밋된 채 3커밋을 살아남았다 — 병합 커밋도 diff 검토 대상이다
- 증상: `CHANGELOG.md` 의 `[Unreleased] > Added` 절에 `<<<<<<< HEAD`/`=======`/`>>>>>>> origin/dev`
  충돌 마커가 그대로 커밋돼(89e13fd, #302 로 dev 병합) 이후 dev 병합 커밋들에도 계속 남아 있었다.
  #297 작업 중 CHANGELOG 항목을 추가하려다 발견 — 릴리스 노트 정본이 깨진 채 배포 라인에 있었다.
- 원인: 병합 충돌 해결 중 CHANGELOG 충돌을 마커째 저장하고 커밋했다. 커밋 워크플로 1번(`git diff`
  전체 검토)이 병합 커밋에는 적용되지 않았고, ruff·pytest 도 마크다운 파일은 보지 않아 어떤
  자동 검사에도 걸리지 않았다.
- 규칙:
  - **병합 커밋도 커밋이다** — 충돌을 해결한 병합은 커밋 전 `git diff --check` 와
    `git grep -nE '^(<{7}|={7}|>{7})' -- .` 로 잔여 마커를 확인한다(코드가 아닌 md·설정 파일 포함).
  - 충돌 마커는 발견 즉시 **별도 fix 커밋**으로 정리한다 — 기능 커밋에 섞으면 리뷰에서 묻힌다.
- 관련: `CHANGELOG.md`, 커밋 89e13fd(#302), 정리 커밋은 #297 브랜치.

---


## [2026-08-05] `pre-commit run --all-files` 는 `ruff format`(인수 없이)과 같은 뿌리의 드리프트를 좁은 스코프 브랜치 전체에 드러낸다
- 증상: #281 작업에서 커밋 전 훅 호환성을 확인하려고 `uv run pre-commit run --all-files` 를
  돌렸더니 `ruff-format` 이 **내가 건드리지 않은 31개 파일을 재포맷**했다 —
  `app/services/spring_client.py` · `evals/intent_probe/runner.py` 처럼 **다른 레인이 작업
  중이라 이 브랜치에서 편집이 금지된 파일**까지 포함됐다. `git checkout --` 로 전부 되돌렸다.
- 원인: `.pre-commit-config.yaml` 이 `ruff-pre-commit` **v0.8.6** 에 고정돼 있는데 개발
  의존성 ruff 는 0.15.x 라, 두 버전의 포맷 규칙 차이만큼 저장소 전체에 **드리프트**가 깔려
  있다. `--all-files` 는 그 드리프트를 전부 드러내 diff 로 만든다. (기존 lessons 2026-08-05
  「`ruff format`(인수 없이)…」와 **같은 뿌리, 다른 입구**다 — 그 항목은 `ruff format` 을
  경고할 뿐 `pre-commit --all-files` 를 언급하지 않아 이번에 그대로 밟았다.)
- 규칙: 좁은 스코프 브랜치에서 훅 호환성을 확인할 때 `pre-commit run --all-files` 를 쓰지
  말고 **`pre-commit run --files <내 파일들>`** 로 대상을 좁혀라. 실제 커밋 훅은 **스테이징된
  파일에만** 돌므로 그것이 커밋 시점의 동작과도 일치한다. 실수로 `--all-files` 를 돌렸으면
  `git status --porcelain | grep '^.M'` 으로 **미스테이징 변경만** 골라 `git checkout --` 로
  되돌린 뒤 진행하라(내 변경은 스테이징돼 있어 안전하다).
- 관련: #281, `.pre-commit-config.yaml`(ruff-pre-commit v0.8.6), 기존 lessons 2026-08-05
  「`ruff format`(인수 없이) 은 diff 밖 파일까지 재포맷한다」

---

## [2026-08-05] 실측 프로브에서 "표본 0" 은 근거가 아니라 질문이다 — 원시 응답을 남기지 않으면 원인을 가를 수 없다
- 증상: #281 TASK 3(`evals/priority_probe/`) 초판이 인라인 팔(`decompose()` 후보 프롬프트)을
  실측했더니 축이 **전부 0** 이고 진단 카운터 셋(`lengthMismatch`·`emptySignal`·당시의
  `legMismatch`)이 **전부 정확히 96**(=모든 표본)으로 나왔다. "인라인이 완전히 무능하다"는
  결론을 그대로 쓸 뻔했다 — 오케스트레이터가 `samples.csv` 를 직접 읽어보니 모델이 **실제로
  무엇을 냈는지**가 한 칸도 기록돼 있지 않아, "모델이 정말 priority 를 안 냈다"(역량 한계)와
  "채점기의 매칭 규칙이 개수가 다르면 이름이 맞아도 전부 버렸다"(하네스 결함)를 구분할 수
  없었다. 실제로는 후자였다 — `decompose()` 는 픽스처 `needs` 를 입력으로 받지 않고 자기 leg
  이름을 스스로 만드는데, "leg 개수가 needs 개수와 같을 때만" 채점하는 조건이 이름이 일부
  맞는 표본까지 통째로 0점 처리했다.
- 원인: 채점 함수가 **중간 산출(모델이 실제로 낸 것)** 을 버리고 최종 판정(0/None)만 남겼다.
  같은 원인이 서로 다른 이름의 카운터 세 개에 동시에 찍히면(이 경우 lengthMismatch=
  emptySignal=legMismatch=96) "원인이 하나이고 세 번 세어졌다"는 신호인데, 초판은 그 신호를
  읽을 수 있는 자리(원시 응답 칸)를 애초에 안 만들었다.
- 규칙: 실측 프로브에서 **"거의 0/전부 0" 같은 극단값이 나오면 채택 판정을 내리기 전에
  원시 응답을 남겨 재현하라.** `samples.csv`(또는 동급 산출물)에 (1) 모델이 실제로 낸 원문,
  (2) 최종 채점 값, (3) 그 사이의 판정 근거(왜 그 값이 나왔는지)를 **전부 다른 칸**으로
  남겨야 런을 다시 돌리지 않고 원인을 가를 수 있다(#240 이 이미 세운 규약 — 이번엔 그 규약
  자체가 없어서 밟았다). 진단 카운터가 여러 개인데 같은 사건에서 동시에 오르면 그 카운터들이
  뭉개져 있다는 뜻이니 **상호 배타적으로 다시 정의**하거나 겹침을 명시하라. "모델이 못 한다"는
  결론은 데이터가 그것을 **구분해서** 보여줄 때만 쓴다 — 표본이 0이라는 사실만으로는 원인을
  주장할 수 없다.
- 관련: #281, `evals/priority_probe/runner.py::_match_inline_legs_by_name`,
  `evals/priority_probe/README.md` §「초판 결함과 정정」, TASK-3-CORRECTION-2

## [2026-08-05] 보조 신호 함수가 실패를 전부 `None` 으로 삼키면, 그 함수를 실측 하네스로 감쌀 때 **전송 실패**와 **모델 출력 실패**를 관측 래퍼로 갈라야 한다
- 증상: #281 TASK 3 의 분류기 실측 초판은 `classify_need_priorities` 가 돌려주는 `None` 을
  전부 "표본"으로 셌다. 그 함수는 정본 계약상 **어떤 예외도 밖으로 내보내지 않는다**(429·
  타임아웃도 삼켜 `None`) — 그래서 페이서가 조금이라도 어긋나 429 가 나면 그 시도도 "분류기가
  판정에 실패한 표본"으로 집계돼, #240 이 이미 "빈 칸을 오답으로 세면 분포가 거짓이 된다"고
  경고한 실패 양식을 다른 이름으로 재현할 뻔했다.
- 원인: 프로덕션 함수의 degrade 설계(보조 신호 실패 → 조용히 `None`, 턴을 안 죽인다)는
  **운영에서는 옳다.** 그런데 실측 하네스가 그 함수를 블랙박스로 호출만 하면, "진짜 전송이
  실패했다"와 "모델이 이상한 출력을 냈다"가 함수 경계에서 이미 뭉개진 뒤라 하네스도 구분할
  수 없다 — 정본 계약(자기 예외를 삼킨다)을 재현하려고 그 함수를 그대로 부르는 것은 맞지만
  (규칙을 재구현하면 측정과 배포가 갈라진다는 원칙, lessons 다른 항목 참조), 그 안쪽에서
  무슨 일이 있었는지까지 통째로 잃으면 안 된다.
- 규칙: 자기 예외를 삼키는 보조 함수를 실측 하네스에서 반복 호출할 때는, 그 함수가 받는
  `llm` 을 **한 겹 더 감싸(관측 전용 래퍼) 래퍼 사슬의 맨 안쪽**(delegate 바로 앞)에 두고
  `complete()` 자체의 성공/실패를 별도로 기록한다. 함수가 삼킨 값(`None`)만 보고 재시도
  여부를 정하면 안 된다 — 래퍼가 기록한 "이번 시도가 전송 실패였는가"를 근거로, 전송 실패는
  재시도(표본 아님)로, 함수가 정상 응답을 받고도 `None` 을 낸 경우만 표본으로 가른다. 예산
  가드(`BudgetExceeded`)도 이 경로에서 삼켜질 수 있으니 래퍼에서 별도로 식별해 재시도하지
  말고 그대로 던져야 한다(안 그러면 예산 상한이 무력화된다).
- 관련: #281, `evals/priority_probe/client.py::RawCapture`,
  `evals/priority_probe/runner.py::run_cell_classifier`, TASK-3-CORRECTION

---

## [2026-08-05] 코드가 런타임에 읽는 repo 파일은 배포 이미지에 들어 있는지 실측한다
- 증상: dev→main 승격(#316) 사전 점검에서, 운영 이미지로 앱을 띄우면 부팅이 실패하는 결함을
  발견했다. `session_context.initialize()`(#187)가 `db/profile/init/03_chat_session_contexts.sql`
  을 런타임에 `read_text()`로 읽는데, `Dockerfile`은 `app/`만 COPY 하고 `.dockerignore`는
  `db/`를 "볼륨 마운트용, 이미지 불필요"라며 명시 제외하고 있었다 → 컨테이너 안에서
  `FileNotFoundError` → lifespan re-raise → 헬스체크 실패 → 자동 롤백.
- 원인: #187이 `db/`의 성격을 "init 스크립트(컨테이너 밖 볼륨 마운트 전용)"에서 "앱 런타임
  의존"으로 바꿨는데, 그 가정을 적어 둔 `.dockerignore`·`Dockerfile`은 갱신되지 않았다.
  로컬(`uv run uvicorn`)·CI(pytest)는 repo 루트에서 실행돼 파일이 항상 존재하므로 어떤
  테스트도 이 결함을 잡을 수 없었다 — 이미지 경계는 이미지에서만 드러난다.
- 규칙:
  - **코드에 `Path(__file__)…read_text()`류 런타임 파일 의존을 추가하면, 같은 PR에서
    Dockerfile/.dockerignore 반입 여부를 확인하고 컨테이너 안 존재를 실측한다**
    (`docker build` 후 `docker run --rm --entrypoint sh <img> -c 'ls <경로>'`).
  - `.dockerignore` 의 제외 항목에는 이유(가정)가 주석으로 적혀 있다 — 그 가정을 깨는 변경을
    할 때 함께 갱신한다. 반대로 제외를 풀 때도 왜 런타임 의존이 됐는지 주석으로 남긴다.
- 관련: 이슈 #319, `Dockerfile`, `.dockerignore`, `app/core/session_context.py` `initialize()`

## [2026-08-05] 부분 문자열 표지의 파괴력이 크면, 명사 하나만으로는 절대 표지를 만들지 않는다
- 증상: #116 삭제 흐름의 "전체 삭제" 표지에 `"전부"`를 그대로 넣었다. `"전부"`는 `"전부터 쓰던 거
  빼줘"`의 부분 문자열이라, 사용자가 상품 1개만 빼 달라고 말했는데 장바구니 전체가 삭제됐다
  (라운드 3 리뷰가 재현 — 온전한 단어로 보이는 표지도 다른 단어의 앞부분일 수 있다).
- 원인: 표지 목록을 만들 때 "이 표지가 오탐할 만한 입력이 있는가"만 봤고, "오탐했을 때 결과가
  얼마나 되돌리기 어려운가"는 따로 가중치를 두지 않았다. 짧고 흔한 명사(`"전부"`·`"다"`·`"모두"`)
  는 정상적인 다른 단어의 접두사가 되기 쉬운데, 이 판별기에서 "전체 삭제"는 가장 파괴적인
  결과(전 항목 삭제, 되돌릴 수 없음)라 다른 규칙보다 훨씬 엄격한 표지가 필요했다.
- 규칙:
  - **부분 문자열로 매칭하는 표지는 결과의 파괴력에 비례해 좁힌다.** 파괴력이 낮은 규칙(예:
    "방금 담은 거" → 최근 1건만 영향)은 짧은 표지도 허용할 수 있지만, 되돌릴 수 없거나 범위가
    넓은 규칙(전체 삭제 등)일수록 어미까지 포함한 동작 구로 좁혀야 한다.
  - **명사 하나만 있는 표지는 금지한다.** 한국어에서 명사는 다른 단어의 앞부분이 되기 쉽다
    (전부→전부터, 다→다른/다시, 모두→모두 다른 뜻의 합성어). "빼줘"·"전부 빼"처럼 동작(용언)
    까지 포함하면 이런 접두 오탐이 거의 사라진다.
  - **표지를 추가할 때 "이 표지가 부분 문자열로 걸리는 다른 흔한 단어가 있는가"를 적대적으로
    한 번 나열해 본다** — `docs/lessons.md`의 다른 항목("규칙이 발동하는 입력을 적대적으로
    나열해 본다")과 같은 절차를, 표지 그 자체에도 적용한다.
  - **[라운드 9 추가] 표지 매칭은 표지 문자열만이 아니라 그 주변 문맥도 봐야 한다.** 한국어
    부정은 어미(뒤, `-지 마`)와 부사 접두(앞, `안`·`못`) 양쪽에서 오는데, 뒤쪽만 검사하고
    앞쪽을 빼먹어 "안 빼줘도 돼"가 삭제로 실행되는 사고가 났다 — 부정을 "한 방향만" 본 것은
    "전부"⊂"전부터"와 같은 급의 반쪽짜리 방어다. 그리고 앞쪽(접두) 검사는 `"안"`처럼 흔한
    조각이라 부분 문자열로 그대로 쓰면 `"안경"`·`"안쪽"`류 정상 발화를 대량으로 삼킨다 —
    반드시 **어절 경계**(앞이 문자열 시작/공백, 표지와의 사이 공백 0~1개)로 판정해야 한다.
  - **[라운드 10 추가] 같은 판정 개념을 두 곳에 각자 구현하면 한쪽만 고쳐진다.** 이 PR 에서
    부정 인지 결함이 세 번 났다 — 어미형 도입(라운드 5) → 접두형을 `intent_guard.py` 에만
    추가(라운드 9) → `remove.py` 의 같은 판정은 그대로 남아 플래그 on 시 실제 데이터 손실로
    재현(라운드 10). 안전 판정처럼 여러 호출부가 "같은 규칙이어야 하는" 로직은 파일마다 각자
    구현하지 말고 **공용 함수 하나**로 두고 호출부가 그것을 쓰게 한다 — 그래야 한쪽을 고칠 때
    나머지도 같이 고쳐진다.
  - **[라운드 17 추가] "덜 친절한 문구"와 "요청하지 않은 파괴적 동작"을 같은 저울에 올리지
    말 것.** 후자는 플래그 뒤에 있어도 한계로 미루지 않는다. 이 PR 에서 실제로 한 번 그렇게
    미뤘다가 리뷰가 되짚었다 — 라운드 15 는 "이어폰 케이스 빼줘"(장바구니에 이어폰만 있음)가
    보유 중인 "이어폰"을 확인 없이 지우는 것을 "장바구니에 없는 상품명을 알 방법이 없다"는
    이유로 알려진 한계로 문서화하고 넘어갔는데, 그건 문구 품질의 문제가 아니라 이 판별기
    전체가 막으려던 "사용자가 말하지 않은 상품이 지워지는" 바로 그 결함이었다. 파괴적
    동작(삭제·해제처럼 되돌리기 어렵거나 사용자 모르게 상태를 바꾸는 것)의 오탐 가능성을
    "고칠 방법이 마땅치 않다"는 이유로 한계 취급하기 전에, 그 판단 기준이 "사용자 경험이
    덜 매끄럽다" 수준의 문제에 쓰는 기준과 같은지부터 다시 묻는다.
- 관련: `app/core/config.py`(`cart_remove_all_markers`), `app/agents/buyer/cart/remove.py`,
  이슈 #116 라운드 3 리뷰 F-1. 접두 부정: `app/core/config.py`(`utterance_prefix_negation_markers`),
  `app/agents/buyer/cart/negation.py`(`has_prefix_negation`), 라운드 9·10.

---

## [2026-08-05] 내 계약에 **남의 이슈 구현을 예외로 새기지 않는다** — 예외가 곧 승인이다
- 증상: #149 계약에 "개인화는 순위에만 쓴다"는 불변식을 쓰면서, 동시에 열려 있던 다른 이슈를
  **그 불변식의 유일한 예외**로 서술하고 제약 5개를 달았다("그래프 카테고리 노드로 후보를
  파생해도 된다, 단 턴 로컬로"). PR 올리기 전 점검에서 그 브랜치 코드를 읽어 보니 **후보를
  파생하지 않았다** — 불변식을 그대로 지키는 구현이었다. 예외가 필요한 상황이 애초에 없었다.
- 원인 두 겹.
  - 이슈 **제목의 표현**을 동작으로 읽었다. 내 저장소 안의 주장은 전부 코드로 대조했는데
    **남의 in-flight PR 만 설명으로 추정**했다 — 경계 밖이라고 무의식적으로 분류한 것이다.
  - 더 근본적으로, **충돌해 보이면 예외를 새겨 화해시키려 한 것**이 틀렸다. 내 계약이 다른 이슈의
    구현을 서술할 소관이 아니다.
- 규칙:
  - **계약에는 불변식만 쓴다.** 다른 작업이 그것을 위반할 것 같으면 예외를 새기지 말고 **미해결
    항목으로 올려 그 담당자와 얘기한다.** 예외 조항은 문서가 그 경로를 **승인하는 것**이라, 아무도
    만들지 않은 예외를 남겨두면 위험 경로에 미리 면허를 내주는 셈이다. 이번 건은 예외를 지우자
    계약이 더 단순하고 더 엄격해졌다.
  - 남의 이슈 구현을 내 문서에 **묘사하지 않는다** — 그 구현은 병합 전에도 바뀌고, 사본은 낡는다
    (같은 파일의 "계약은 정본으로 확인한다" 항목과 같은 함정을 새로 만드는 것이다).
  - 그래도 다른 작업의 동작을 알아야 한다면 이슈 설명이 아니라 **브랜치 코드를 읽는다** —
    `git show origin/<branch>:<file>` 로 몇 초다. "FE 미구현일 것이라 추정하지 말고 FE 저장소를
    읽는다"와 같은 규칙이며 **적용 대상이 같은 저장소의 열린 PR 로 넓어진다.**
- 관련: `docs/specs/SPEC-PROFILE-GRAPH-149.md` §6.12 REQ-PGRAPH-115/116, 이슈 #149·#119

---

## [2026-08-05] "통합 목록"이라고 선언한 표는 그 선언을 검사하는 스크립트로 지켜야 한다
- 증상: #149 로 오류 코드 4종을 api-spec §2.5 표에 추가하면서 "신규 절이 쓰는 모든 `error.code` 가
  §2.5 에 있는가"를 스크립트로 검사했더니, **내가 넣은 것과 무관하게 `INTERNAL_TOKEN_INVALID`(401)와
  `UPSTREAM_UNAVAILABLE`(503)이 이미 빠져 있었다.** 두 코드는 §3.5.1(session-claim)·§3.7(I-22)이
  실패 응답표에서 쓰고 있고 구현도 그 코드를 낸다. 정작 §2.5 는 자기 표를 *"스트림 전 오류 코드의
  **통합 목록**"* 이라고 선언해 둔 상태였다.
- 원인: 엔드포인트 절을 추가할 때 그 절의 실패 응답표만 채우고 §2.5 로 **올려 등재하는 단계를
  건너뛰기 쉽다** — 절 안에서는 계약이 완결돼 보이기 때문이다. 표의 "통합 목록" 선언은 사람의
  선의에만 의존하고 있었고, 그래서 절이 늘어난 만큼 조용히 어긋났다.
- 규칙:
  - 문서가 **"통합/전체 목록"이라고 주장하면 그 주장을 검사하는 스크립트를 같이 만든다.** 신규 절의
    코드를 정규식으로 뽑아 §2.5 에 있는지 대조하는 십여 줄로 충분하고, 이번에 실제로 2건을 잡았다.
    검증 없는 완전성 주장은 시간이 지나면 반드시 거짓이 된다.
  - 오류 코드를 새로 쓰면 **세 곳을 동시에** 본다: 그 절의 실패 응답표 / §2.5 통합 표 /
    `app/core/errors.py` 의 상태→코드 맵. 세 번째가 빠지면 `HTTPException(404)` 처럼 코드를 지정하지
    않은 호출이 `"ERROR"` 로 나가는데, **해피패스 테스트로는 절대 잡히지 않는다**(그래서 이번에
    `_resolve` 의 현재 동작을 핀 테스트로 고정했다).
  - 남의 절에서 발견한 drift 는 **그 자리에서 정정**하고 정정임을 표기한다(`[vX.Y.Z drift 정정]`).
    다음 사람이 같은 스크립트를 돌릴 때 같은 2건을 또 만나게 두지 않는다.
  - **역방향도 성립한다 — 이 리포는 이미 문서 문장을 단정하는 테스트를 갖고 있다.** 같은 작업에서
    §1.2 레인 (c)에 "신규 5종(I-29~I-33)은 이 집합이 아니다"라는 **오독 방지 문장**을 넣었더니
    `test_lane_c_documents_exact_seventeen_call_contract_and_i4_section` 이 깨졌다 — 그 테스트는 해당
    단락의 `I-\d+` 집합이 17건과 **정확히 일치**하는지 보므로, 배제를 설명하려고 적은 번호까지
    집합에 들어간 것이다. 가드가 제 일을 했다. **문서만 고치는 PR 에서도 `uv run pytest` 를 반드시
    돌린다**, 그리고 "이건 이 집합이 아니다"를 적을 때는 **번호를 쓰지 말고 절 번호로 가리킨다**
    (§3.9 처럼) — 부정문에 등장한 식별자도 정규식에는 등장이다.
- 관련: `docs/api-spec.md` §2.5(401 `INTERNAL_TOKEN_INVALID`·503 `UPSTREAM_UNAVAILABLE` 등재),
  `app/core/errors.py` `_STATUS_CODE_MAP`(404·412 부재), `tests/unit/test_error_envelope_status_gaps.py`,
  이슈 #149

## [2026-08-05] 프로브 중복 제작 3회차 — 새 측정 도구를 만들기 전에 기존 하네스에 축을 더할 수 있는지 먼저 본다
- 증상: #118(PR #292)이 screen 지시어 해소를 재려고 별도 스크립트 파일(이관 후 #300 이 삭제했다)
  로 **두 번째 프로브**를 새로 만들었다. 그런데 리포에는 이미 #260 이 고정한 `evals/intent_probe/`
  가 있었고, 측정 대상(`decompose` intent 라우팅·담기 productId 확정)이 사실상 같았다 — 컨텍스트
  종류·표본 조립·페이서·산출물 포맷을 전부 새로 설계·구현해야 했고, 결국 #300 이 그 screen 셀
  6종만 `evals/intent_probe`로 흡수하고 스크립트를 삭제해야 했다. `evals/model_eval` 이
  `_prior_echo_tokens` 류 판정 함수를 재구현하지 않고 배포 함수를 그대로 부르는 것처럼,
  "새 하네스를 만들기"와 "기존 하네스에 컨텍스트·축을 추가하기"는 전혀 다른 비용 곡선인데
  그 비교를 하지 않고 후자를 골랐다.
- 원인: 이슈가 요구하는 것이 "screen 이라는 새 세션 상태 하나 + 판정 규칙 3종 + 축 4개"였는데,
  이것을 `evals/intent_probe`의 `ProbeContext`(#84 가 `prior_filters_ref` 로 이미 컨텍스트별
  분기 패턴을 증명해 뒀다)에 필드를 추가하는 문제로 보지 않고 "screen 전용 측정"이라는 새
  범주로 봤다. 기존 하네스의 확장 지점(컨텍스트 종류·축 정의·픽스처 검증자)을 먼저 읽지 않으면
  "이 측정은 특별해서 새로 만들어야 한다"는 착각이 쉽게 든다.
- 규칙:
  - **새 실 LLM 측정 도구를 만들기 전에 `evals/` 아래 기존 하네스가 있는지 먼저 찾는다.**
    있으면 "컨텍스트/축/픽스처를 추가할 수 있는가"를 먼저 검토하고, 정말 안 되는 이유(예: 완전히
    다른 세션 상태 모델, 다른 성공 판정 방식)를 코드 주석이나 이슈 코멘트로 남긴 뒤에만 새 도구를
    만든다.
  - **판정 규칙은 배포 경로의 함수를 그대로 부른다** — 프로브가 재구현하면 그 자체가 두 번째
    소스가 되어 다음 사람이 또 "이 프로브는 못 믿겠다"며 세 번째를 만들 동기가 된다(이번이
    3회차였다 — `evals/intent_probe/README.md` 「재현 함정」 참조).
  - **흡수·삭제 PR은 표본 동일성을 diff로 증명한다.** 이관 전/후 픽스처를 각각 JSON 덤프해
    `diff` 가 빈 출력임을 보이고 그 명령·출력을 PR에 남긴다 — "같은 값을 옮겼다"는 주장을
    사람이 눈으로 대조하지 않고 기계로 확인할 수 있게 한다.
- 관련: #118, #260, #300, `evals/intent_probe/`, `evals/intent_probe/schema.py`(`ProbeContext`)

---

## [2026-08-05] 정본 목록을 재사용하기 전에 **그 목록이 답하는 질문**이 내 질문과 같은지 본다
- 증상: #162 "조건 없는 발화" 판정이 조건 축으로 `decompose._FILTER_AXES` 를 재사용했다.
  사본을 만들지 않았으니 드리프트가 없다고 판단했는데, `RouteDecision` 에 직접 달린 축
  (`total_budget`·`buy_all`·`repurchase_products`·`revert_categories`)이 **통째로 검사에서
  빠져 있었다**(PR #311 리뷰).
- 원인: `_FILTER_AXES` 가 답하는 질문은 **"Spring WHERE 로 나가는 하드필터는 무엇인가"** 이고,
  판정이 물어야 하는 질문은 **"사용자가 조건을 하나라도 줬는가"** 였다. 그 축들은 `filters` 가
  아니라 `RouteDecision` 필드라 그 목록에 **있을 수가 없다** — 재사용이 드리프트는 막았지만
  **범위가 애초에 달랐다**. 두 질문이 겹치는 구간이 넓어 한동안 맞아 보였을 뿐이다.
- 규칙: 정본 목록을 재사용할 때는 **그 목록의 docstring 이 규정하는 질문**을 먼저 읽고 내
  질문과 대조한다. 다르면 재사용하되 **모자란 축을 별도 목록으로 명시**하고, 그 자료구조
  **전체 필드를 분류하는 드리프트 테스트**를 붙여 다음 필드가 조용히 새지 않게 한다
  (`_FILTER_AXES` 가 `ProductSearchFilters` 전체와 대조되는 것과 같은 방식).
- 곁가지 교훈: **리뷰가 제안한 수정을 그대로 넣기 전에 그 전제를 검산한다.** 리뷰는 "예산 턴이
  취향 경로로 새서 `BudgetSet` 로직을 우회한다"며 판정에서 막으라고 했는데, `buy_all_mode` 는
  `split_by_need`(니즈 2개 이상)를 요구하고 조건 없는 턴은 정의상 leg 가 비어 있어 **어느
  경로로 가도 예산 세트는 만들어지지 않았다**. 제안대로 막았으면 그 턴이 무필터 I-1
  (7,245건·13.33MB)로 되돌아가면서 예산은 여전히 반영되지 않는, 비용만 늘고 얻는 것 없는
  변경이 됐다. 실제 채택안은 **판정은 통과시키고 후보 확보 방식을 가르는 것**이다 — 가격이
  없는 취향 경로를 막고, 가격이 오는 인기 상품(I-3)을 예산으로 거른다.
- 관련: #162, PR #311 리뷰, `app/agents/buyer/recommendation/no_condition.py`
  (`_DECISION_CONDITION_AXES`·`has_total_budget`·`within_budget`),
  `tests/unit/test_no_condition.py::test_route_decision_axes_are_all_classified`

---

## [2026-08-05] `monkeypatch.setenv` + `get_settings.cache_clear()` 는 전역 autouse 픽스처와 경합해 다음 테스트로 샌다
- 증상: #299 의 `test_limit_configurable_via_env` 가 `REQUEST_BODY_MAX_BYTES=20` 을
  `monkeypatch.setenv` + `get_settings.cache_clear()` 로 주입했는데, 이 테스트 **하나만** 파일
  안에서 통과해도 그 뒤에 실행되는 무관한 테스트 파일(`test_buyer_tracing.py`)의 `/chat`
  요청이 실제 바디 크기와 무관하게 전부 400 으로 깨졌다 — 값이 20 인 채로 굳어 있었다.
- 원인: `tests/conftest.py` 의 전역 autouse `_reset_infra_state` 가 teardown 에서
  `reset_cart_store()` → `get_settings()` 를 다시 부른다. 이 프로젝트 conftest 픽스처는 테스트
  모듈 자체의 autouse 픽스처보다 **먼저 set up** 되므로 LIFO 로 **더 늦게 teardown** 된다 —
  즉 파일 안에서 `get_settings.cache_clear()` 를 `yield` 뒤에 불러도, 그보다 늦게 도는
  `_reset_infra_state` 의 `get_settings()` 호출이 **monkeypatch 가 env 를 복원하기 전에** 캐시를
  재구성해 버려 낮춰진 값이 그대로 굳는다(monkeypatch 는 자신이 건드린 attr/env 만 복원할 뿐,
  그 사이 재구성된 lru_cache 싱글턴은 모른다). 파일 로컬 autouse 픽스처의 teardown 타이밍만
  보고 "여기서 지웠으니 안전하다"고 판단한 것이 오판이었다 — 실제 순서는 다른 conftest 계층과
  얽혀 있어 실측(디버그 프린트/트레이스백) 없이는 알 수 없었다.
- 규칙: `monkeypatch.setenv` 로 config 값을 바꾸는 테스트는 픽스처 teardown 타이밍에 기대지
  말고, **테스트 본문 안에서 `monkeypatch.undo()` 를 직접 호출해 env 복원을 먼저 강제한 뒤**
  `get_settings.cache_clear()` 를 부른다(`try/finally`). 이렇게 하면 그 뒤에 도는 어떤 픽스처의
  `get_settings()` 재호출도 항상 이미 복원된 env 로 재구성된다. 이 레포처럼 전역 conftest 가
  많은 곳에서는 "내 픽스처가 마지막에 돈다"를 가정하지 말 것 — 의심되면 `pytest_runtest_setup`
  훅 플러그인으로 다음 테스트 시작 시점의 실제 값을 직접 찍어 확인한다.
- 관련: #299, `tests/conftest.py::_reset_infra_state`, `tests/unit/test_body_limit.py::test_limit_configurable_via_env`

---

## [2026-08-05] `ruff format`(인수 없이) 은 diff 밖 파일까지 재포맷한다 — 항상 대상 파일을 명시한다
- 증상: #299 작업에서 검증 절차대로 `uv run ruff check --fix && uv run ruff format` 을 인수 없이
  돌렸더니, 내가 건드리지 않은 32개 파일(`app/services/spring_client.py` 등)이 재포맷되어
  `git status` 에 잡혔다 — 로컬 ruff(0.15.21) 의 포맷 규칙이 저장소에 마지막으로 적용된
  시점의 규칙과 미묘하게 달라 드러난 기존 드리프트였다.
- 원인: `ruff format`(대상 미지정)은 `pyproject.toml` 의 `[tool.ruff]` 설정을 프로젝트 전체
  파일에 적용한다 — 워크트리에 반영되지 않은 채 남아 있던 포맷 드리프트가 있으면 내 작업과
  무관한 파일까지 diff 에 섞인다. "허용 편집 파일" 이 좁게 지정된 작업에서 이 diff 는 그대로
  범위 위반이 된다.
- 규칙: 좁은 스코프 작업에서는 `ruff check`/`ruff format` 에 **내가 만든/수정한 파일 경로를
  명시**해 실행한다. 전체 대상 실행은 `git status --short` 로 의도치 않은 파일이 없는지 반드시
  확인하고, 있으면 `git checkout --`  으로 되돌린 뒤 좁힌 대상으로 재실행한다.
- 관련: #299, `app/core/body_limit.py`

---

## [2026-08-05] 상류가 채워 주는 필드는 "비어 있음"으로 판정할 수 없다 — 값이 아니라 **출처**를 본다
- 증상: #162 "조건 없는 발화" 판정을 `filters` 축 + `semantic_query` 가 전부 비었는지로 짜고
  단위 테스트 13건이 전부 통과했다. 그런데 그 판정은 **프로덕션에서 한 번도 발동할 수 없었다** —
  `decompose` 가 `semantic_query = llm_sq or cat_signal or prior_sq or query` 로 채워
  아무 의미 신호가 없어도 **이번 턴 발화 원문**이 들어가기 때문이다("아무거나 추천해줘" →
  `semantic_query="아무거나 추천해줘"`). 값 검사는 항상 참이었다.
- 원인: 테스트가 `ProductSearchFilters(...)` 를 **직접 생성**해 상류(decompose)를 우회했다.
  그래서 "실제로 그 필드에 무엇이 들어오는가"라는 전제를 한 번도 검증하지 않았다. 심지어 그
  폴백을 고정하는 기존 테스트(`test_semantic_query_falls_back_to_user_query_when_missing`)가
  이미 있었는데도 새 판정이 그 전제를 보지 않았다.
- 규칙: 상류가 폴백으로 채우는 필드는 **유무로 판정하지 말고 출처 플래그를 상류에서 받아온다**
  (`RouteDecision.semantic_query_is_fallback`). 그리고 판정 로직 테스트에는 **상류 산출에서
  출발하는 회귀 1건**을 반드시 끼운다 — 입력을 손으로 만든 테스트만 있으면 "초록인데 실제로는
  안 도는" 상태를 못 잡는다. 축 목록도 사본을 만들지 말고 정본(`decompose._FILTER_AXES`)을
  import 해 드리프트 테스트에 얹는다.
- 관련: #162, `app/agents/buyer/recommendation/no_condition.py`,
  `decompose.py`(semantic_query 폴백 체인), `tests/unit/test_no_condition.py`

---

## [2026-08-05] 이슈 본문의 결함 서술은 그 이슈를 낳은 PR 의 후속 리뷰에서 이미 고쳐졌을 수 있다
- 증상: #288 은 "검증기가 단일 I-1 호출 예산만 본다"는 결함으로 열렸다. 그런데 착수 시점 `dev`
  에는 그 이슈를 낳은 #277(PR #287) 의 리뷰 4차에서 이미 첫 이벤트 앞 **직렬 합** 검증이
  들어와 있었다 — 실제로 남은 델타는 그 직렬 합의 계수 `2` 가 하드코딩이라는 점 하나뿐이었다.
- 원인: 이슈는 등록 시점의 스냅샷이고, 원인이 된 PR 은 대개 이슈보다 늦게 병합·리뷰가 계속
  붙는다. 이슈 본문만 보고 범위를 잡으면 이미 닫힌 부분까지 다시 구현하려 들거나(낭비),
  좁아진 실제 델타를 못 알아본다.
- 규칙: 착수 전 **이슈 본문이 아니라 `dev` 실물 코드**를 먼저 읽고, 이슈가 가리키는 함수의
  현재 동작·docstring·관련 커밋을 확인한 뒤 남은 델타를 다시 정의한다.
- 관련: #288, #277(PR #287), `app/core/config.py` `_require_search_retry_within_stream_budget`

---

## [2026-08-05] 판정을 **짧은 전용 호출로 떼는 것**과 긴 프롬프트에 **필드를 하나 더 얹는 것**은 같은 "LLM 에 맡긴다"가 아니다 — 그리고 **이득 0인 프롬프트 추가도 공짜가 아니다**

- 증상 ①: #84 의 "이번 발화가 직전 카테고리를 놓겠다는 말인가"를 `decompose`(133줄 `_SYSTEM`)의
  `categoryAction` 필드로 받으려 했다. fast(gpt-5-nano) 실측에서 리셋 기대 32건 중 `clear` 산출이
  **0건**이었고, 문면을 6종으로 바꿔도 나아지지 않았다:

  | 후보 | 바꾼 것 | clear/32 | 부작용 |
  |---|---|---|---|
  | 인라인 필드(초판) | — | 0 | — |
  | 불릿·스켈레톤을 이웃 불릿 앞으로 | 위치 | 0 | — |
  | + 이웃 불릿에 예외 문장 | 충돌 제거 | 0 | — |
  | + "먼저 clear 부터 판정" 강화 문면 | 문면 | 1 | 교체 기대가 clear 로 6/24 오염 |
  | 이분 boolean 으로 교체 | 필드 모양 | 6 | 오탐 0 |
  | 스켈레톤 최상단 + 맨 끝 검산 불릿 | 최신성 | 21 | 리파인 3/32 · 교체 10/24 붕괴 |

  같은 프롬프트를 `--tier smart` 로 재면 **32/32 · 32/32 · 24/24 로 완벽**했고, 문면을 그대로
  **짧은 전용 호출**로 떼어내자 fast 에서도 **32/32 · 오탐 0/56**(독립 3회 동일)이었다.
- 증상 ②(더 비쌌던 쪽): 전용 분류기를 넣은 **뒤에도** 인라인 필드를 "smart 에서 32/32 니 놔두면
  티어 올릴 때 이득"이라며 남겨 뒀다. 전 축 회귀를 **전/후 각 2회** 짝지어 재고서야 그것이
  **이득 0 · 손해 확정**임이 드러났다 — 불릿이 없는 런에서도 `categoryClear` 는 이미 32/32(=
  해소는 전적으로 분류기의 성과)였고, 불릿이 있는 런은 `PENDING_CART` 중 상품 전환 경로가
  두 런 모두 깎였다(`switchAll7` 37·38 → 32·32, 전환 발화가 `recommend` 로 새는 표본
  4~5 → **16~17**). **그 손해는 내가 만든 신규 축(카테고리 4종)에는 전혀 보이지 않았다** —
  카테고리 4축은 **분류기를 켠 런 모두** 88/88 로 만점이었다(분류기를 끈 런만 24~25/88). 불릿을
  지운 뒤 전환 축이 **32/56 → 37/56** 으로 **되돌아온 것**이 인과를 닫는다(한쪽 방향만 보고
  "노이즈겠지" 하지 않으려면 되돌렸을 때 복귀하는지까지 봐야 한다 — 최종 출고 구성의 v3 대조쌍
  에서는 양쪽 36/56 으로 같다).
  개발 과정 런들의 전 축 표와 최종 대조쌍은
  `evals/intent_probe/baselines/fast-2026-08-05-84/README.md` 에 있다.
- 원인:
  - ①은 실패 원인이 **문면이 아니라 그 프롬프트가 이미 지고 있는 작업량**이었다. 한 호출에
    intent 5-way·filters·cart·attributes·categoryQueries 가 얹혀 있으면 fast 모델에는 새 판정을
    더 할 여력이 없다. 그래서 문면 후보가 전부 0~1 로 수렴하고, 억지로 최신성을 끌어올린
    후보(21/32)는 **다른 축을 무너뜨리며** 총합이 나빠졌다. #198 `needs_expansion` 이 같은
    이유로 전용 호출이 됐다는 기록이 이미 있었는데 같은 함정을 한 번 더 밟았다.
  - ②는 "무동작이면 무해하다"는 암묵 가정이 틀렸기 때문이다. 프롬프트에 줄을 더하면 그 자체가
    **모델의 주의 배분을 바꾼다.** 잠들어 있는 필드도 토큰을 쓰고 다른 규칙과 경쟁한다.
- 규칙:
  - **긴 프롬프트에 판정 필드를 더할 때는 "문면 후보"와 "전용 호출"을 같은 실측 표에서 비교한다.**
    문면 후보만 여러 개 재면 0/0/0/1/6 같은 표가 나오고 "더 좋은 문면을 찾자"로 읽히기 쉽다.
  - **티어 대조군을 함께 잰다.** 같은 프롬프트가 smart 에서 완벽하면 문면 결함이 아니라
    **역량 한계**이고, 처방이 "문면 수정"이 아니라 "작업 분리 또는 티어 승격"으로 바뀐다.
  - **프롬프트를 한 줄이라도 바꾸면 전 축 회귀 런을 전/후 각 2회 돌린다.** 1회는 노이즈와
    구분되지 않는다(축당 ±2). 그리고 채택 조건은 "내 축이 좋아졌다"가 아니라
    **"다른 축이 안 깎였다"** 다 — 신규 축만 보면 남의 축이 조용히 깎인 것을 볼 수 없다.
  - **이득이 0이면 지운다.** "언젠가 티어를 올리면 이득"은 오늘의 회귀를 사는 근거가 못 된다.
    그 사실은 코드가 아니라 **기록**(이 항목·CHANGELOG·docstring)으로 남겨 다음 사람이 같은
    시도를 반복하기 전에 읽게 한다.
- 관련: #84, `app/agents/buyer/recommendation/category_scope.py`,
  `decompose.resolve_category_action` docstring, `evals/intent_probe/`, #198 `needs_expansion` §2

## [2026-08-05] `ruff format` 은 이 리포에서 **무관 파일 29개**를 함께 고친다 — 변경 파일만 지정해 돌린다

- 증상: #84 구현 후 CLAUDE.md 커밋 워크플로 2단계대로 `uv run ruff check --fix && uv run ruff format`
  을 돌렸더니 `30 files reformatted` 가 나왔다. 내가 만진 파일은 4개인데 `app/services/spring_client.py`
  (#116/117 레인)·`app/agents/buyer/recommendation/graph.py`·`evals/**`·다른 레인 테스트까지 커밋
  후보로 들어왔다. `git status --short` 로 확인하지 않았다면 **동시 진행 레인의 파일을 조용히 덮어쓴**
  PR 이 됐다.
- 원인: 베이스(`dev` @ c2aa2eb)가 현재 pin 된 ruff(0.15.21) 기준으로 format-clean 이 아니다
  (`uv run ruff format --check` → `29 files would be reformatted`). 전역 `ruff format` 은 "내 변경을
  정리한다"가 아니라 **리포 전체를 현재 ruff 판으로 재포맷한다** — 내 diff 와 무관한 드리프트까지
  내 PR 이 떠안는다.
- 규칙:
  - 포맷은 **변경한 파일만 인자로 지정**해 돌린다: `uv run ruff format <내가 만진 파일들>`.
    전역으로 돌렸다면 `git checkout --` 로 무관 파일을 되돌리고 `git status --short` 로 확인한다.
  - 커밋 전에 `git status --short` 를 **반드시 눈으로 본다**(패킷·CLAUDE.md 1단계). 린터가 만든
    변경은 diff 검토를 건너뛰기 쉬운 부류라 레인 경계를 깨는 경로가 된다.
  - 베이스의 포맷 드리프트를 고치고 싶다면 **기능 PR 이 아니라 별도 chore PR** 로 낸다.
- 관련: #84, `CLAUDE.md` 커밋 워크플로 2단계, ruff 0.15.21

## [2026-08-04] ORM 이 만드는 SQL 은 **생성물을 봐야** 안다 — JPQL 을 읽고 재현한 SQL 로 성능을 진단하지 않는다
- 증상: #132 에서 BE `ProductRepository.searchCandidates` 의 JPQL `group by p` 를 "엔티티 전체 컬럼이 그룹 키"로 읽고, 그대로 손으로 옮긴 SQL 을 실측해 **4.15s(PK 그룹 대비 33배)** 라는 병목을 보고했다. `EXPLAIN` 까지 붙여 "TEXT 컬럼 때문에 임시테이블+filesort" 라는 그럴듯한 인과도 만들었다. BE 를 실제로 띄워 재니 같은 질의가 **1.11s** 였다.
- 원인: Hibernate 6.6 은 엔티티 그룹핑을 **식별자로 최적화**해 `group by p1_0.id` 를 보낸다. 존재하지 않는 쿼리의 비용을 잰 것이다. `EXPLAIN` 이 그럴듯했던 것은 내가 준 SQL 에 대해 정확했기 때문이지 실제 실행 계획이어서가 아니다 — **재현이 틀리면 그 위의 모든 측정과 인과가 함께 틀린다.**
- 규칙: ORM(JPA/Hibernate·SQLAlchemy) 경로의 성능을 진단할 때는 **생성 SQL 을 먼저 확보한다.** MariaDB 는 `SET GLOBAL general_log='ON'; SET GLOBAL log_output='TABLE';` 후 `mysql.general_log` 조회로 1분이면 뽑는다(끝나면 `OFF`). 앱 SQL 로깅도 같은 값을 준다. 생성 SQL 없이 잰 수치는 문서에 올리지 않는다.
- 관련: `docs/specs/MEASURE-I1-RESPONSE-132.md` §3 / 이슈 #132

## [2026-08-04] "발동 조건이 좁다"는 안전 논거는 **기존 테스트가 아니라 새 입력**으로 검증해야 한다

- 증상: 바로 앞 항목(「결정적으로 풀리는 규칙을 …」)이 코드 해소기의 안전 논거로
  *"`screen.products` 가 있는 턴에만 도니 기존 회귀 대조군에 구조적으로 닿지 않는다"* 를 적었다.
  그 문장은 맞았지만 **읽기 전용 리뷰가 낸 3건을 전부 재현**했고 그중 둘은 오담기였다 —
  `"아까 추천해준 그거 담아줘"` 가 화면 상품으로 확정되고(대화 지시어를 화면 지시어로 읽음),
  `"무선 이어폰 2번째 옵션으로 담아줘"` 에서 **옵션**을 수식하는 `"2번째"` 가 화면 순번으로 읽혀
  엉뚱한 상품이 담겼다. 회귀 대조군은 전부 초록이었다.
- 원인: "좁다"를 **기존 스위트에 닿지 않는다**로만 증명하고, **그 조건이 새로 삼키는 입력 집합이
  실제로 결정적인가**를 확인하지 않았다. 발동 조건(`screen.products` 존재)은 좁았지만 그 안에서
  적용하는 규칙(지시대명사·순번)이 결정적이지 않은 발화까지 덮었다. 회귀 0 은 "새 결함 0" 이 아니다.
- 규칙:
  - **LLM 산출을 덮어쓰는 코드 규칙을 넣으면, 그 규칙이 발동하는 입력을 적대적으로 나열해 본다.**
    "이 규칙이 틀리는 발화는 무엇인가"를 한 번 적어 보는 것으로 이번 3건은 전부 사전에 잡혔다.
  - **강한 신호가 있으면 약한 신호로 덮지 않는다.** 이름 지목(프로브 8/8)이 있는데 순번(더 약함)이
    이기면 우선순위가 거꾸로다. 특정 단어(`옵션`)를 예외 처리하는 대신 **이름 우선**으로 일반화한다.
  - **"확실할 때만 개입, 애매하면 원래 산출 존중"을 함수 앞단의 명시적 양보(early return)로 쓴다.**
    분기 안쪽에 흩어 두면 다음 규칙을 추가할 때 또 빠뜨린다.
- 관련: #118 라운드 3, `app/agents/buyer/screen_reference.py`(양보 (A)(B)), 리뷰 F-1·F-2

## [2026-08-04] 한 결함을 두 축으로 막으면, 한 축을 깨도 테스트가 빨개지지 않는다

- 증상: 위 F-1 을 두 축으로 막았다 — ① 기본 표지에서 `"그거"` 제거 ② 대화 참조 표지(`"아까"`)가
  있으면 해소 자체를 건너뜀. 채택 전 「회귀를 흉내 내 실패시켜 본다」 절차대로 ②를 지웠는데
  테스트가 **초록**이었다. ①만으로 재현 케이스 3개가 전부 통과했기 때문이다 — ②는 커버리지가
  0 인 채로 들어갈 뻔했다.
- 원인: 테스트 케이스를 **결함(재현 발화)** 기준으로만 썼다. 재현 발화는 두 축 중 아무 쪽으로나
  막히므로, 축별 판별력이 없다. `"아까 추천해준 **이거** 담아줘"`(근칭 + 대화 참조)처럼 **②만이
  막을 수 있는** 입력을 넣어야 ②가 검증된다.
- 규칙:
  - **깨뜨려 red 를 확인하는 절차는 "수정 단위"마다 돌린다.** 결함 단위로 한 번 돌고 끝내면
    중복 방어의 뒤쪽 축이 조용히 미검증으로 남는다.
  - **방어를 하나 더 얹을 때는 "이 축만 막을 수 있는 입력"을 같이 적는다.** 못 적겠다면 그 축은
    없어도 되는 것이므로 지우는 편이 낫다(코드가 줄고 다음 사람이 안 헷갈린다).
- 관련: #118 라운드 3, `tests/unit/test_screen_context.py`
  (`test_conversation_deictic_is_not_forced_onto_the_screen_product`)

## [2026-08-04] 계약이 요구하는 대상과 **프롬프트에 싣는 것**을 구분한다 — 누적 목록을 그대로 실으면 다른 경로가 깎인다

- 증상: #118 에서 담기 가드를 정본대로 "누적 추천 목록 ∪ screen.products" 로 넓히면서 그 누적
  목록을 `decompose` 프롬프트의 `LAST_RECOMMENDATIONS` 에도 그대로 실었다. 실 LLM N=8 프로브에서
  **`PENDING_CART` 중 상품 전환**(`이어폰으로 할래`)이 `6/8 → 1/8` 로 무너졌다. 승계분을 상한 6건으로
  줄여도 `2/8` 이었고, 승계분을 **아예 빼자 7/8** 로 돌아왔다 — 즉 승계분이 2건만 붙어도 #240 이
  "낮추지 말 것"으로 못박은 경로가 깨진다.
- 원인: 정본 §3.1 [보안]이 누적을 요구하는 대상은 **`allowed`(담기 가드)** 인데, 그것을 "프롬프트에도
  누적을 실어야 한다"로 읽었다. 프롬프트에 무엇을 싣는지는 계약이 아니라 튜닝 대상이다. 같은 이슈의
  07-30 코멘트가 *"무한 누적은 LLM 오추출 표면을 넓힌다"* 고 스스로 경고했는데, 그 완화책으로 제안된
  **상한이 불충분**하다는 것이 실측으로 드러났다.
- 규칙:
  - **계약 문장이 무엇에 걸리는지 목적어를 정확히 읽는다.** "가드가 X 를 allowed 로 취급한다"는
    "프롬프트에 X 를 싣는다"가 아니다. 둘을 분리하면 계약을 지키면서 프롬프트를 실측에 맞출 수 있다.
  - **맥락 목록을 늘리는 것 자체가 프롬프트 변경이다.** 문구를 한 글자도 안 고쳐도 회귀 프로브를
    돌려야 한다. 특히 `PENDING_CART` 처럼 "사용자가 한 상품에 집중해 답하는" 상태에서 과거 목록이
    길어지면 초점이 흩어진다.
  - **회귀 원인을 (A)(B) 중 하나로 지목하기 전에 한쪽만 끈 대조군을 잰다.** 이번에는 두 설계안이
    같은 셀을 **똑같이** 깎은 것이 "원인은 설계안이 아니라 목록 길이"라는 가설의 출발점이었고,
    승계분만 뺀 변형이 그것을 확정했다.
- 관련: #118 라운드 2, `app/agents/buyer/graph.py`(`prompt_reco`), `app/agents/buyer/cart/state.py`
  (`LastReco.turn_count`), `evals/intent_probe`(#300 이 흡수하기 전 별도 프로브였다), #240 기준선

## [2026-08-04] 결정적으로 풀리는 규칙을 확률적 계층에 맡기면, 가드가 못 막는 오답이 나온다

- 증상: #118 화면 지시어 해소("이거"·"3번째 거"·"3번째 줄 2번째")를 프롬프트로만 처리했더니 실 LLM
  N=8 에서 `이거 담아줘`(화면 1건) 2/8, `이거 담아줘`(화면 3건 → **되물음이 정답**) **0/8**,
  좌표 지시 5/8 이었다. 더 나쁜 것은 실패의 모양이다 — 대부분이 "null 로 두고 되물음"이 아니라
  **목록 안의 다른 상품을 자신 있게 확정**이었다. 담기 가드는 목록 **밖** id 만 막으므로 이 오답은
  그대로 통과해 **사용자가 말하지 않은 상품이 장바구니에 담긴다.**
- 원인: "지시어 해소"를 한 덩어리로 보고 전부 LLM 에 맡겼다. 그중 **순번·좌표·"후보 1건이면 확정,
  여러 건이면 되물음"은 입력만으로 답이 하나로 정해지는 결정적 규칙**이라 애초에 맡길 이유가 없었다.
  정본도 그 셋을 산술·개수 규칙으로 명시하고 있었다.
- 규칙:
  - **정확도 지표만 보지 말고 실패의 모양을 갈라 센다** — `null → 되물음`(무해)과 `다른 것을 확정`
    (오담기)은 같은 "실패"가 아니다. 후자가 있으면 채택 불가로 본다.
  - **결정적으로 풀리는 부분은 코드로 떼어내고, LLM 에는 애매한 부분만 남긴다.** 이번에는 이름 매칭만
    LLM 에 남겼고(8/8), 나머지를 코드로 옮겨 신규 셀이 9/48 → **48/48** 이 됐다.
  - **코드 규칙의 발동 조건을 좁게 잡으면 그 자체가 회귀 안전 논거가 된다.** 이 해소기는
    `screen.products` 가 있는 턴에만 도는데, 기존 회귀 대조군은 전부 `screen` 이 없어 **구조적으로**
    닿지 않는다. 그 사실을 테스트로 고정해 두면 다음 사람이 다시 재보지 않아도 된다.
- 관련: #118 라운드 2, `app/agents/buyer/screen_reference.py`, api-spec §3.1 "지시어 해소",
  `evals/intent_probe`(#300 이 흡수하기 전 별도 프로브였다)

## [2026-08-04] 튜너블을 근거로 결함을 판단하기 전에 그 값이 **실제로 소비되는지** grep 으로 먼저 본다
- 증상: #89 는 "fan-out 부분 실패 후 생존 leg 가 `category_fanout_per_cat_limit`(10)에 계속
  묶인다"를 결함으로 보고했고, 3안(생존 leg 재조회/사전 over-fetch/동적 사이징) 중 재조회를
  기본안으로 제시했다. 그런데 실측하니 이슈가 보고한 축소는 **현재 hot path 에서 관측되지
  않았다** — `filters.limit` 을 읽는 백엔드가 없다(`SpringSearchBackend`·
  `EmbeddingRerankBackend` 모두 미참조, §4.6 size 제거·#101 절단 재배치 이후). 그대로 재조회를
  골랐다면 **완전히 같은 응답을 다시 받는** 왕복을 degraded 경로에 추가할 뻔했다 — `limit` 이
  네트워크에 실리지 않으니 재검색은 효과가 0이었다.
- 원인: `category_fanout_per_cat_limit` 이라는 이름과 주변 주석이 여전히 "leg top-K"로
  읽혀서, 그 값이 실제로 절단에 관여한다고 가정한 채로 수정안을 골랐다. 소비 지점을 먼저
  확인하지 않으면 "값을 조정하면 동작이 바뀐다"는 전제 자체가 틀릴 수 있다.
- 규칙: 튜너블(config 값)을 근거로 결함이나 수정안을 판단하기 전에 **그 값이 실제로 읽히는
  지점을 grep 으로 먼저 확인**한다. 소비되지 않는 튜너블을 발견하면 그 사실을 주석에 남겨
  다음 사람이 같은 가정을 반복하지 않게 한다. 값을 소비하지 않아도 "정확한 상한"(예: merge_cap)
  으로 정해 두면 값이 소비되기 시작해도 안전하다 — 우연에 기대지 않는 수정을 고른다.
- 관련: #89, `app/agents/buyer/recommendation/graph.py`(`_run_search` fan-out 절),
  `app/services/search_service.py`(`search_catalog`), `app/core/config.py`
  (`category_fanout_per_cat_limit`), `app/services/spring_client.py:373-374`

---

## [2026-08-04] 상한이 안전한지는 단일 호출 예산이 아니라 첫 이벤트 앞 **직렬 합**으로 잰다
- 증상: I-1 단일 호출 예산 `3s × 2 = 6s < 10s` 기동 검증을 통과한 출고 기본값에서,
  미룬 턴의 두 재시도 응답이 상한 직전(2.9s)에 오자 첫 SSE 이벤트가 하나도 나가지 않은 채
  10.01s에 504가 **8/8** 재현됐다(재시도 뒤 즉답이면 7.09s·200). 이 PR은 미룬 턴의
  재시도를 꺼 이 조합을 닫았고 `SEARCH_RETRY_ON_DEFERRED_CONDITIONS=true`로 되돌릴 수 있다.
- 원인: #113의 `may_auto_relax` 턴은 `conditions`를 검색 뒤로 미뤄 첫 이벤트 앞에 본 검색과
  자동 완화 probe 두 I-1 호출이 직렬로 놓인다. "probe가 돌면 본 검색은 재시도를 안 썼다"는
  배타성 논거도 1차 타임아웃 → 2차 0건 성공 실측으로 반증돼 최대 합은 12s다.
- 규칙:
  - 순서를 근거로 쓴 서술은 **그 순서를 바꾼 PR이 함께 갱신한다.**
  - 예산 검증은 첫 이벤트 앞에 호출이 **몇 번** 놓이는지까지 센다. 인프로세스 `TestClient`는
    SSE 본문을 버퍼링하므로 첫 이벤트 측정에 쓰지 않고 실 HTTP 경계에서 잰다.
- **[2026-08-10 갱신] 이 응급 처치는 #306으로 제거됐다.** 근거였던 관문(첫 이벤트가 검색 뒤)이
  #396의 `progress` 상시화로 사라졌고, 그 조합을 지금 막는 것은 억제가 아니라
  `RESCUE_BUDGET_MODE=narrow`의 런타임 좁히기다(미룬 턴 본검색을 ≈4.8s로 묶어 "1차 3.0s
  타임아웃 + 2차 2.9s 성공"이 성립하지 않게 한다). `SEARCH_RETRY_ON_DEFERRED_CONDITIONS`도
  함께 폐지됐다 — 재시도는 `SPRING_MAX_RETRIES` 하나가 정한다.
- 관련: #277, #113/PR #248, #306, `app/core/config.py`
  `_require_search_retry_within_stream_budget`, `evals/first_event_budget/`, api-spec §2.9(c)

---

## [2026-08-04] provider TPM 은 **응답 실사용이 아니라 요청이 예약한 `max_tokens` 까지** 센다
- 증상: #260 프로브의 페이서를 "콜당 ≈3.1k tokens, org 200k TPM → 50rpm 이면 155k 라 안전"
  으로 잡고 424콜 기준선 런을 돌렸는데 **429 를 78회**(전체 시도의 15%) 먹었다. 메시지는
  `tokens per min (TPM): Limit 200000, Used 200000, Requested 3018`. 재시도가 전부 흡수해
  셀은 다 채웠지만(exit 0), 그 재시도가 없었다면 표본이 비어 표가 거짓이 됐을 자리다.
- 원인: 3.1k 는 **프롬프트 토큰** 실측이다. provider 는 거기에 요청이 예약한
  `max_tokens`(decompose 는 800)를 더해 TPM 에 넣는다 → 실효 3.9k, 50rpm 이면 195k 로 한도에
  붙는다. 응답이 실제로 그만큼 쓰지 않아도 예약분이 잡힌다.
- 규칙: 레이트 예산을 계산할 때 **요청이 예약한 상한(`max_tokens`)을 토큰 추정에 포함**한다.
  그리고 페이서를 넣었다고 429 가 0이 되리라 가정하지 말고, 실측 런의 실패 건수·유형을
  산출물(`failures.csv`)로 남겨 추정이 틀렸는지 사후에 확인할 수 있게 한다 — 이번 기본값 수정
  (45rpm · 콜당 3.9k)의 근거가 그 파일이다.
- 관련: `evals/intent_probe/pacer.py` · `evals/intent_probe/baselines/fast-2026-08-04/` · #260

---

## [2026-08-04] 측정 하네스는 **만든 그 PR 에서 리포에 커밋한다** — scratchpad 는 사라진다
- 증상: `decompose` intent 라우팅을 재려고 #234 와 #240 이 각각 프로브를 만들었는데 둘 다
  리포에 커밋하지 않았다(세션 scratchpad 에만 있었다). #240 담당은 #234 것이 유실된 줄 알고
  **다른 정답지로 새로 만들었고**, 그걸로 후보 11종을 재 cand11 을 채택했다. 나중에 #234
  프로브를 찾아 교차 검증하니 채택본이 안정화 목표 셀(`그거 보여줘`[pending])을 20/24 → 10/24
  로 무너뜨리고 있었다 — **채택 판정이 뒤집혔다.** 두 정답지의 차이는 되물음 상품이 추천 목록
  1번이냐 2번이냐 **하나뿐**이었고, 그것만으로 `일반형` 옵션 정답률이 8/8 ↔ 3/8 로 갈렸다.
  #260 을 만들 때 그 scratchpad 는 이미 임시폴더 정리로 사라져 정답지를 이슈 본문에서
  **재구성**해야 했다(그래서 옛 표와 ±2 재현을 보장할 수 없다).
- 원인: 하네스를 "일회용 조사 도구"로 취급했다. 실제로는 그 숫자가 프롬프트 채택의 유일한
  근거였으므로 **산출물이 아니라 측정 장치가 정본**이었다. 2026-08-01 항목("실측 표는 앵커
  목록까지 코드로 고정한다")이 이미 있었는데 같은 곳을 다시 밟았다.
- 규칙: 실 LLM 측정으로 결정을 내렸으면 **그 PR 에 하네스와 앵커를 함께 커밋한다.** 앵커는
  데이터 파일로 두고 스크립트가 그것만 읽게 하며, 판정에 영향을 주는 축(예: 되물음 상품의 목록
  위치)은 값과 **고정한 이유**를 파일 안에 남긴다. 지표는 이름이 아니라 **분자·분모 정의**를
  산출물에 함께 실어야 다음 사람이 두 표를 잘못 비교하지 않는다. 재현을 깨는 함정(페이서·실패
  표본 처리·픽스처 문자열 오염)은 문서가 아니라 **스키마 검증자와 테스트로** 박는다.
- 관련: `evals/intent_probe/` · #260 · #240(`#issuecomment-5163199589`) · #234 · #259

---

## [2026-08-05] raise 하는 계산 함수를 도구에 배선하면 **호출부 전수**를 같은 degrade 로 감싼다
- 증상: #290 PR 리뷰 4건 — `proportions.wilson_interval` 은 `successes > trials` 를
  ValueError 로 거부하는데, 같은 PR 의 신규 호출부 6곳 중 4곳(timeseries·segmentation·
  outliers·spike)만 `except ValueError` degrade 를 갖췄고 `get_funnel`(_stage_summary)·
  `get_churn_cohort` 2곳이 빠졌다. I-7 은 이벤트 기반 카운트라 단계 역전(cart>view —
  view 이벤트 유실·목록 직행 담기)이 실데이터에서 가능하고, 스키마(FunnelResult 평범한
  int·ChurnResult churn_rate 무구간)는 관계를 강제하지 않는다. 실증 결과 create_agent
  의 도구 실행은 예외를 ToolMessage 로 바꿔주지 않아 워커 그래프 밖까지 전파됐고,
  orchestrator 가 브랜치를 "내부 오류" degrade 로 강등 — §3.4(도구는 raise 하지 않는다)
  위반이자, 그 데이터 상태의 브랜드는 해당 분석이 상시 전면 실패한다(부분 degrade 불가).
- 원인: 같은 함수를 여러 도구에 배선하면서 예외 계약 처리를 호출부마다 개별 판단했다 —
  먼저 만든 호출부(단계 2·4·5)에는 붙였지만 나중에 만든 단계 3 호출부에서 빠뜨렸고,
  "입력이 Spring 집계라 정합할 것"이라는 검증 안 된 전제에 기댔다.
- 규칙:
  - **raise 가 계약인 함수(ValueError 등)를 도구 층에 배선할 때는 호출부 전수를 같은
    degrade 패턴으로 감싼다** — 배선 커밋마다 `grep` 로 호출부를 세고 except 유무를
    대조한다. 하나라도 다르면 그 차이가 곧 리뷰 지적이다.
  - **외부 집계의 필드 간 관계(단계 단조성·비율 구간)는 스키마가 강제하지 않는 한
    성립하지 않는 것으로 취급한다** — 위반 입력은 clamp 로 정상처럼 위장하지 말고
    "판정 보류 + 사유"로 표기한다(0 위장 금지와 같은 취지).
  - **도구 예외의 실제 전파 경로를 프레임워크 가정 없이 실증한다** — create_agent
    (langchain 1.x)는 도구 예외를 잡아주지 않는다(ToolMessage 변환 없음).
- 관련: #290 PR 리뷰, `app/agents/seller/tools.py`(_stage_summary·get_churn_cohort),
  `app/agents/seller/analysis/proportions.py`, `tests/unit/test_seller_tools.py`
  (역전 카운트·구간 밖 rate 회귀 4건)

## [2026-08-04] "자체 상한이 있다"고 인용하기 전에 그 `wait_for` 가 **어디까지** 감싸는지 본다
- 증상: #266 에서 `get_checkpointer()` 를 레인 상한 밖으로 빼며 근거를 *"자체 상한
  (`seller_checkpoint_connect_timeout_s`, 5s)이 있어 무한 대기가 아니다"* 로 적고, 그 값을
  기동 예산 검증의 한 항으로까지 썼다. 실제로 그 `wait_for` 는 `ctx.__aenter__()`(연결)만
  감쌌고 **바로 다음 줄 `await saver.setup()`(DDL)은 상한 밖**이었다. 콜드 DB 에서
  `setup()` 은 MIGRATIONS 8종을 순차 실행하므로 문장당 `statement_timeout`(3s)씩 누적돼
  5s 를 크게 넘길 수 있다. 즉 예산 검증이 **성립하지 않는 전제** 위에 서 있었고,
  콜드스타트 한정으로 이 이슈가 없애려던 "조용한 `done(stop)` 절단"이 그대로 재현될 수 있었다.
- 원인: 상한의 **존재**만 확인하고 **범위**를 확인하지 않았다. 설정 이름
  (`..._connect_timeout_s`)과 호출 한 줄만 보고 "초기화 전체가 묶여 있다"로 읽었다.
  이웃인 `app/core/pg_store.py` 는 같은 일을 하면서 `setup()` 을 **별도 `wait_for` 로**
  감싸고 이유까지 주석에 적어 뒀는데(PR #46 후속 리뷰), 그 선례를 세어 보지 않았다.
- 규칙:
  - **다른 모듈의 상한을 내 예산식에 인용하려면 그 `wait_for` 의 괄호가 어디서 닫히는지
    직접 읽는다.** 상한 뒤에 이어지는 `await` 는 상한 밖이다.
  - **"상한 밖으로 뺀다"는 결정은 "그 안이 유한하다"에 의존한다.** 유한성이 확인되지 않으면
    먼저 상한을 채우고 그다음 계수를 맞춘다 — 연결·setup 각각이면 예산은 2배다.
  - 실패 경로에 `ctx.__aexit__` 정리가 있는지도 함께 본다(이번에 `setup()` 실패 시 커넥션이
    새는 것도 같이 발견했다).
- 관련: #266 PR 3차 리뷰, `app/agents/seller/checkpoint.py`(`_init_checkpointer`),
  `app/core/pg_store.py:125-150`(선례), `app/core/config.py`
  (`_require_general_lane_within_stream_cap`)

## [2026-08-04] 같은 예외 타입이 두 원인에서 나오면 타입으로 못 가른다 — **발생 지점**으로 감싼다
- 증상: #266 에서 문자열 판정을 타입 판정(`is_timeout_error`)으로 바꾸고 나니, pg-profile
  체크포인터 연결 실패가 `LLM_TIMEOUT`("응답 생성이 지연되어 중단됐습니다", WARNING)으로
  나갔다. **인프라 장애가 "느린 LLM 응답"으로 감춰진다.** `get_checkpointer()` 의
  `asyncio.TimeoutError` 는 `asyncio.timeout` 이 내는 것과 **같은 객체**(3.11+ 내장
  `TimeoutError`)다.
- 원인: "타입으로 판정한다"를 **타입이면 충분하다**로 읽었다. 타입 판정이 문자열 판정보다
  나은 것은 맞지만, 한 타입이 여러 원인에서 나오면 해상도가 부족하다. 기존 경계 함수
  `is_state_store_unavailable` 도 `TimeoutError` 를 포함하므로 여기서는 쓸 수 없었다 —
  썼다면 반대로 **진짜 LLM 타임아웃까지 인프라 장애로** 삼켰을 것이다.
- 규칙:
  - **예외 타입이 원인을 유일하게 지목하지 못하면 발생 지점에서 감싼다.** 호출을 좁은
    `try` 로 묶고 전용 예외로 태그해 올린다. 판정 순서도 계약이다 — 좁은 분기를 넓은
    분기보다 **먼저** 두고 그 이유를 주석에 남긴다.
  - **상한을 도입할 때 그 스코프 안에 다른 상한을 가진 호출이 있는지 본다.** 있으면 밖으로
    빼는 쪽이 기본이다. 안에 두면 두 원인의 타임아웃이 섞여 양방향 오분류가 생긴다.
  - **스코프 밖으로 뺀 시간은 상위 예산식에 다시 더한다.** 빼기만 하면 "직렬 누적을 빠뜨림"
    이라는 원래 실수를 반복한다.
- 관련: #266 PR 2차 리뷰, `app/api/seller.py`(`_CheckpointerUnavailable`),
  `app/agents/seller/checkpoint.py`, `app/core/pg_resilience.py`
  (`is_state_store_unavailable`), `app/core/config.py`

## [2026-08-04] 예외는 **타입**으로 잡는다 — 문자열 매칭은 가짜 예외 테스트만 통과시킨다
- 증상: 판매자 general 레인의 `except (TimeoutError, asyncio.TimeoutError): → LLM_TIMEOUT`
  분기가 **한 번도 실행되지 않는 죽은 코드**였다. 이 레인만 `asyncio.wait_for` 가 없어
  `asyncio.TimeoutError` 가 날 일이 없고, SDK 타임아웃(`httpx.TimeoutException`·provider
  `APITimeoutError`)은 내장 `TimeoutError` 의 서브클래스가 아니라 `except Exception` 으로
  떨어져 계약상 `LLM_TIMEOUT` 이어야 할 것이 `INTERNAL` 로 나갔다. 구매자 쪽 `_is_timeout`
  은 같은 문제를 `"timeout" in str(exc).lower()` 로 막고 있었는데, SDK 메시지는
  `"Request timed out."`(timed **out**)이고 `httpx.ReadTimeout` 은 `str` 이 비는 경우가 있다.
- 원인: 두 판정 모두 **실제 SDK 예외를 한 번도 통과시켜 본 적이 없다.** 테스트가
  `RuntimeError("... timeout ...")` 처럼 사람이 만든 예외를 주입했기 때문에, 판정기가 재는
  것이 "우리가 쓴 문자열"이지 "SDK 가 던지는 것"이 아니었는데도 초록불이 유지됐다.
- 규칙:
  - **예외 분기는 타입으로 판정한다.** 메시지 문자열은 라이브러리가 언제든 바꾸는 표현이지
    계약이 아니다. 원인 체인(`__cause__`/`__context__`)까지 따라가야 `raise X from exc` 로
    감싼 경우를 놓치지 않는다(순환 방어 필수).
  - **판정기의 전제를 테스트로 고정한다.** "SDK 타임아웃은 `TimeoutError` 서브클래스가
    아니다" 같은 전제는 버전 의존이므로, 그 전제 자체를 단언하는 테스트를 두면 업그레이드로
    전제가 바뀔 때 판정기보다 먼저 깨져서 알려준다.
  - **가짜 예외로만 검증한 `except` 분기는 검증되지 않은 것으로 본다.** 실제 라이브러리가
    던지는 인스턴스를 최소 1건 주입한다.
- 관련: #266 P1, `app/api/seller.py`(`_general_stream`), `app/core/llm.py`(`is_timeout_error`),
  `app/agents/buyer/graph.py:91-93`(미수정 — 별도 이슈), api-spec §2.9 c,
  `docs/specs/DESIGN-SELLER-TIMEOUT.md`

## [2026-08-04] 스트리밍 호출의 `timeout=` 은 벽시계 상한이 아니다
- 증상: general 레인에 상한을 넣으려다 `asyncio.wait_for(agent.astream(...))` 로 감싸려 했으나
  `astream` 은 중간에 yield 하는 async generator 라 감쌀 수 없었고, "SDK 에 이미 `timeout=30`
  을 주고 있으니 상한은 있다"는 판단도 틀렸다.
- 원인: httpx 계열의 `timeout` 은 스트리밍 응답에서 **청크 간 read 간격**을 잰다. 토큰이
  상한보다 짧은 간격으로 계속 오면 전체 시간이 아무리 길어져도 발동하지 않는다. 같은 이름의
  파라미터가 non-stream(`ainvoke`)에서는 사실상 총 상한처럼 동작해서 혼동을 키웠다.
- 규칙: **스트리밍 경로의 총 시간을 묶으려면 청크 루프 전체를 `async with asyncio.timeout(...)`
  으로 덮는다.** SDK 의 `timeout=` 은 "시도 1회당 상한"이지 "이 호출의 총 상한"이 아니며,
  `max_retries` 가 붙으면 총 예산이 `timeout × (retries+1)` 로 조용히 늘어난다 — 앱 레벨
  상한을 정할 때 이 곱을 먼저 계산한다.
- 관련: #266 P1, `app/api/seller.py`(`_general_stream`), `app/core/llm.py`(`stream`),
  `app/core/config.py`(`seller_general_timeout_s`)

## [2026-08-04] 저장소 전체 포맷은 작업 범위 밖 변경을 만든다

- 증상: #278에서 `uv run ruff check --fix && uv run ruff format`을 실행해 무관한 seller·eval
  파일 24개가 재포맷됐다. 순수 포맷임을 확인해 전부 되돌리고 전체 테스트를 재검증했다.
- 원인: 현재 ruff로 포맷되지 않은 파일이 남아 있는데 인자 없는 `ruff format`은 저장소 전체를
  바꾼다. pre-commit ruff v0.8.6은 스테이징 파일만 보고 CI는 format check를 하지 않아 드러나지 않았다.
- 규칙: 커밋 전 포맷은 이번에 수정한 파일만 명시한다. 전체 포맷 후에는 `git status`로 무관한
  변경을 확인해 되돌리고, 전체 포맷 정리는 별도 이슈·커밋으로 분리한다.
- 관련: `.pre-commit-config.yaml`(ruff v0.8.6), `.github/workflows/ci.yml`,
  `CLAUDE.md` 커밋 워크플로 2단계

## [2026-08-04] 새 스레드 스코프 저장소는 **정리 경로 등록까지가 한 단위**다 — 안 하면 테스트는 다 통과하고 데이터만 샌다

- 증상: #113 이 완화 칩 기억(`buyer_relaxation_offers_v1`)을, #232 가 재구매 면제
  (`buyer_repurchase_v1`)를 새 namespace 루트로 도입했는데, 둘 다 `session_state.clear_thread`
  에 등록하지 않았다. 스레드가 끝나도 그 스레드 행이 pg-profile 에 무기한 남는다.
  #232 는 자기 PR(#263)에서 자력 발견해 고쳤지만 #113 것은 그때 나란히 보여서야 잡혔다(#276).
- 원인: 두 겹이다.
  1. **`clear_thread` 가 루트를 열거하는 유일한 지점**이다. legacy GC(`run_legacy_gc_batch`)는
     `buyer_*` → `buyer_*_v2` **쌍**만 다루므로 신규 v1 루트를 주워가지 않고, TTL 도 없다.
     즉 등록 누락에 대한 2차 방어선이 **하나도 없다.**
  2. **누락이 테스트에 안 보인다.** 저장·조회 테스트는 전부 통과하고, 정리 테스트는 자기가
     아는 루트만 단언한다. `CleanupCounts` 에 항목이 없으니 운영 카운터에도 안 잡힌다 —
     "안 지워지고 있다"는 사실을 관측할 수단 자체가 없다.
- 규칙: **스레드 스코프 저장소를 새로 만들면 같은 PR 안에서** ① `clear_thread` 에 루트·키 등록
  ② `CleanupCounts` 에 항목 추가 ③ 정리 테스트에 "대상 스레드는 지워지고 이웃 스레드는 보존"
  2건 추가 — 셋을 함께 넣는다. 스토어 클래스 추가 diff 를 볼 때 `clear_thread` 가 같이 안 바뀌면
  리뷰에서 되묻는다. 부분 실패 복구 테스트 헬퍼(`_seed_v2_state`/`_assert_v2_*`)에도 넣으면
  재시도 경로까지 자동으로 덮인다.
- 관련: `app/agents/buyer/session_state.py` `clear_thread`,
  `app/agents/buyer/recommendation/state.py`, `SPEC-CHAT-SESSION-CONTEXT-187` §3, #276·#232·#113

## [2026-08-04] 검증을 **강화**하면 이미 저장된 데이터와 그 값을 읽는 모든 경로가 새 위험 지점이 된다

- 증상: #113(PR #248)에서 같은 계열을 **세 번** 밟았다.
  1. `ProductSearchFilters` 에 `ge=0` 을 걸었다. 전체 스위트가 통과해 안전하다고 봤는데, 제약은
     **이미 저장된 레코드에 소급 적용**된다 — 그 전에는 음수가 통과해 pg-profile 에 영속될 수
     있었다. `ThreadFilterStore.get()` 은 `run_buyer_turn` 진입 직후 decompose 보다도 먼저
     감싸이지 않은 채 불려서, 그런 레코드를 가진 스레드는 매 턴 거기서 죽고 LLM degrade 같은
     정상 오류 이벤트조차 못 낸다. TTL 도 없어 **영구 broken** 이 된다(리뷰로 지적받음).
  2. 그 다음 모순 구간 보정을 decompose 산출에 넣었는데, **저장된 prior 는 칩 클릭 경로에서
     `_relaxed_filters_from_offer` 의 base 로 직접 쓰여** 그 보정을 우회했다. 실제로
     `minPrice=30000&maxPrice=26000` 이 나가는 것을 재현했다(커밋 전 점검에서 자력 발견).
  3. 그 보정 호출을 (1)에서 만든 가드 안에 넣었는데 그 가드가 `except ValidationError` 로 좁아,
     보정이 다른 예외를 내면 다시 (1)의 실패 모드로 새어나갈 구조였다(커밋 전 점검에서 발견).
- 원인: 스키마 변경의 blast radius 를 **"테스트가 깨지나"** 로만 쟀다. 테스트는 코드가 만드는
  값만 쓰므로, **기존 데이터가 깨지는 건 테스트에 보이지 않는다.** 그리고 보정을 "생산 지점"에만
  넣으면 그 값을 **읽는** 다른 경로가 남는다는 것을 매번 뒤늦게 봤다.
- 규칙:
  - 스키마에 제약(`ge`·`min_length`·`model_validator`)을 **추가**하기 전에
    `grep -rn '<Model>.model_validate' app/` 로 역직렬화 지점을 전부 세고, 각 지점이 **감싸여
    있는지**와 **실패했을 때 사용자에게 무엇이 되는지**를 하나씩 적는다. 감싸이지 않은 지점이
    하나라도 있으면 제약을 걸기 전에 그것부터 감싼다.
  - 저장된 데이터를 값 검증하는 제약은 **오래된 레코드가 이미 있다고 가정**한다. 스스로 회복하는
    경로(다음 `put` 이 덮어씀)가 있는지 확인하고, 없으면 제약을 걸지 않는다.
  - 정규화·보정은 **생산 지점이 아니라 신뢰 경계**에 둔다. 값이 저장소에서 나오면 읽기 지점이
    경계다 — 생산 지점만 고치면 저장된 옛 값을 쓰는 경로가 우회한다.
  - "순수 계산이라 안 터진다"로 새 호출을 **좁은 가드 안**에 넣지 않는다. 그 가드가 무엇을 맡는지
    (여기서는 "저장 값 해석 전체")를 보고 범위를 맞춘다.
- 관련: PR #248, `app/schemas/spring.py`, `app/agents/buyer/graph.py::ThreadFilterStore.get`,
  `app/agents/buyer/recommendation/decompose.py::_resolve_contradictory_price_range`

## [2026-08-04] 색상 임베딩은 수식어 유사도를 색상 의미로 오인한다
- 증상: 전체 색상 표기를 빈도 상위 앵커에 최근접 배정하자 `다크그린→다크그레이`,
  `라이트그린→라이트그레이`, `남색→블루`처럼 색상 의미가 다른 제안이 높은 코사인으로 나왔다.
- 원인: 색상 표기 임베딩에서 `다크`·`라이트` 같은 수식어 토큰이 `그린`·`그레이` 같은
  색상 어근보다 유사도를 더 크게 지배해, 최근접 앵커가 의미상 동의어를 보장하지 않았다.
- 추가 실측: 임베딩 우선 군집은 `남색→블루`, `다크그린→다크그레이`를 만들고 LLM이 멤버를
  제거만 할 수 있어 올바른 앵커로 복구하지 못했다. 반대로 LLM 우선 배정은 `남색→네이비`,
  `다크그린·라이트그린·초록→그린`을 맞혔지만, 입력에 없는 `오white` 환각과
  `화이트←와인`·`레드←노랑`, `화이트↔아이보리` 순환, `크림` 중복 그룹도 함께 만들었다.
- 규칙: **임베딩 우선 군집을 색상 동의어에 쓰지 않는다.** 고정 앵커에 대한 LLM 의미 배정을
  먼저 수행하되 원문 정확 일치·배타성·canonical 자기 포함·비순환·sentinel 제외를 엄격 검증한다.
  임베딩은 LLM 선택 코사인과 1위 앵커 불일치를 검수 큐에 보여 주는 교차검증으로만 사용하고,
  전량을 `pending_review`로 두어 사람이 최종 판정한다. 배치에서 비용상 최근접 임베딩 제안을
  유지할 때는 `batch_embedding_unverified` provenance로 LLM 배정과 명확히 구분한다.
- 관련: #258, `app/pipelines/color_synonym_seed.py`, `color_synonyms`

## [2026-08-03] 테스트가 "통과하는지"가 아니라 "되돌리면 깨지는지"로 검증한다

- 증상: #113(PR #248) 최종 점검에서 **아무것도 지키지 않는 테스트 3건**을 발견했다. 전부 초록불이었는데
  검증 대상 코드를 통째로 지워도 그대로 통과했다.
  - `carry_is_skipped_on_general_turns` — 신규 스레드라 `prior` 가 없어 승계가 **다른 이유로** 막혀
    있었다. 그 상태로 "승계 안 됨"을 단언하니 intent 게이트를 지워도 통과.
  - `probe_failure_after_search_does_not_kill_the_stream` — fixture 가 fan-out 이라 새로 추가한 병합
    가드에 **먼저** 걸려, 정작 검증 대상인 probe 가드를 지워도 통과.
  - `conditions_survive_a_broken_relaxation_gate` — 검색 결과가 넉넉해 완화 블록에 진입조차 안 해서
    "미리 내보내도 안전"이라는 주장이 검증되지 않았다.
  그 전에 "전체 점검 완료"를 **두 번** 보고했다. 코드를 읽어 정합성을 확인한 것이었는데, 그건 내가 쓴 걸
  내가 다시 읽은 것이라 새 정보가 0 이었다.
- 원인: 테스트를 **의도 확인**으로 썼다 — "코드가 내 생각대로면 통과한다"까지만 보고, "생각을 어기면
  실패한다"를 안 봤다. 통과는 두 경우에 일어난다: (1) 코드가 맞다 (2) 테스트가 아무것도 안 본다.
  초록불만으로는 둘을 구분할 수 없다.
- 규칙:
  - **새 가드·분기를 추가하면 그 자리를 무력화해 보고 테스트가 실패하는지 확인한다.** `except Exception`
    을 `except ZeroDivisionError` 로 바꾸거나 조건을 `if False:` 로 바꿔 한 번 돌리면 된다. 통과하면 그
    테스트는 아무것도 안 지키고 있다.
  - 부정 단언(`assert X not in log`, `== []`)은 **X 가 일어날 수 있는 상태를 실제로 만들어 놓고** 한다.
    전제가 안 갖춰진 상태에서의 "안 일어남"은 항상 참이라 회귀를 못 잡는다.
  - 기존 테스트의 단언을 **느슨하게 고쳐야 한다면 멈춘다.** 그건 표현 문제가 아니라 그 테스트가 다른
    코드를 검사하게 됐다는 신호다(위 probe 사례 — 로그 이름이 바뀐 걸 문구 문제로 처리했다가 가드가
    통째로 무방비가 됐다).
  - 점검 결과를 보고하기 전에 **어떤 변이를 시도했고 무엇이 잡혔는지**를 근거로 댄다. "코드를 다시 읽었다"
    는 근거가 아니다.
- 관련: PR #248, `tests/unit/test_relaxation.py`, `app/agents/buyer/recommendation/graph.py`

## [2026-08-03] 새 코드를 쓰기 전에 **그 파일의 이웃**이 같은 일을 어떻게 하는지 먼저 센다

- 증상: #113(PR #248)에서 받은 리뷰 대부분이 로직 오류가 아니라 **"주변 코드는 다 지키는 규약을 새 코드만
  안 지킴"** 이었다. 기능은 매번 맞았다.

  | 리뷰 | 그 파일의 규약 | 내 코드 |
  |---|---|---|
  | `_merge_fanout_results` | leg 실패를 격리한다 | 병합만 방어 밖 |
  | 스냅샷 읽기 | 쓰기는 단일 `aput` 으로 원자적 | 읽기만 두 번 |
  | `_post_filter` 가드 | 모든 실패가 `logger.warning(<이벤트명>)` | 이름표 없이 raise |
  | `relaxation_notice` | 모든 출력 텍스트가 `_strip_unsafe` | 이 한 줄만 미통과 |
  | 저장 오퍼 검증 | 값 검증은 스키마가 담당 | 범위가 스키마에 없어 통과 |

- 원인: *무엇을 해야 하는가*만 생각하고 *옆 코드는 어떻게 하고 있나*를 안 봤다. 규약은 문서가 아니라
  **코드에 반복으로 존재**하는데, 새 줄을 쓸 때 그 반복을 세지 않았다.
  둘째로, 수정이 **경계를 옮기면 그 주변이 새 위험 지점이 되는데** 옮긴 뒤 다시 훑지 않았다
  (conditions 를 검색 뒤로 미룬 수정 → 그 앞의 병합·게이트가 새로 위험해짐 → 각각 리뷰로 돌아왔다).
- 규칙:
  - 가드·사용자 노출 텍스트·검증처럼 **같은 종류가 여러 번 나오는 코드**를 추가하기 전에 그 파일을 먼저
    센다: `grep -n 'except Exception as exc' <파일>`, `grep -n '_strip_unsafe' <파일>`. 예외가 나 하나면
    그게 버그다.
  - 리뷰가 **한 곳**을 지적하면 고치기 전에 **그 종류 전체를 훑는다.** 리뷰어는 눈에 띈 하나를 본 것이지
    전수 조사를 한 게 아니다(이번에 스윕으로 리뷰가 안 짚은 `_relaxed_filters_from_offer` 의 무로그
    거부를 추가로 찾았다).
  - 수정이 코드의 **경계를 옮겼으면**(순서 변경·예산 분리·호출 위치 이동) 옮긴 자리의 앞뒤를 같은 기준으로
    다시 본다. 특히 "이제 이 지점이 첫 사용자 노출보다 앞이 됐는가"를 확인한다.
- 관련: PR #248, `app/agents/buyer/recommendation/graph.py`, `app/agents/buyer/graph.py`,
  `app/schemas/spring.py`

## [2026-08-03] 코사인 순위 회귀는 부동소수 꼬리 전체 일치를 요구하지 않는다
- 증상: 파이썬 코사인과 pgvector `<=>` 순위가 상위 120에서는 같았지만, 7,220건 전체
  꼬리의 인접쌍 73곳이 최대 3.5e-07 유사도 격차에서 뒤집혔다.
- 원인: pgvector는 float32, 파이썬은 float64로 거리를 계산해 거의 동점인 꼬리에서 표현
  정밀도 차이가 순서를 바꿨다.
- 규칙: **파이썬 코사인과 pgvector 순위 동등성은 전체 리스트 완전 일치로 고정하지 말고,
  상위 구간 일치 또는 동점 허용 비교로 회귀 검증한다.**
- 관련: #254, `app/pipelines/pg_artifact_store.py`, `tests/unit/test_search_backends.py`

## [2026-08-03] 같은 의존성을 쓰는 두 라우터는 예외 "처분"까지 짝지어 검증한다
- 증상: pg-profile(대화 store) 장애 하나에 구매자 `/chat`은 `503 STATE_UNAVAILABLE`,
  판매자 `/seller/chat`은 `500 INTERNAL`을 반환했다. 재시도 가능 여부라는 신호가 레인마다
  갈려 FE 재시도 정책과 5xx 알람 집계가 원인 하나에 두 갈래로 흩어졌다.
- 원인: 두 라우터가 스트림 개시 전 같은 `get_conversation_store()`를 호출하는데,
  `chat.py`에만 `is_state_store_unavailable` → `SessionStateUnavailable` 변환이 있었다.
  `seller.py`는 블록을 복붙하면서 `except Exception:`(exc 바인딩조차 없음) 후 그대로
  `raise` 했다. 두 경로 모두 테스트는 있었지만 각자 자기 상태 코드만 검증해서,
  "같은 원인 → 같은 코드"라는 교차 불변식은 아무도 보지 않았다.
- 규칙: **공유 의존성의 실패를 여러 진입점이 각자 처리한다면, 처분(상태 코드·오류 코드)을
  한 곳으로 모으거나 최소한 "같은 원인은 같은 코드"를 교차 검증하는 테스트를 둔다.**
  진입점별로 자기 코드만 단언하는 테스트는 이 종류의 비대칭을 구조적으로 놓친다.
  예외를 옮겨 적을 때 `except Exception:`처럼 바인딩이 빠져 있으면 원본에서 분기가
  삭제된 흔적일 수 있으니 원본과 대조한다.
- 관련: `app/api/seller.py`, `app/api/chat.py`, `app/core/pg_resilience.py`, api-spec §2.5·§3.2

## [2026-08-03] 계약은 **정본**으로 확인한다 — 저장소 사본은 낡아 있을 수 있다
- 증상: #113 이 "`done.data.relaxationNotice` 추가(명세 정합화)"를 지시했고, 본 저장소
  `docs/api-spec.md:584` 가 실제로 그 필드를 규정하고 있어 그대로 구현했다. 나중에 정본(Notion
  "📡 API 명세서" CH-2)을 열어 보니 **`done` 은 `finishReason` 하나로 확정**돼 있었고 「구 명세 대비
  정정 요약」에 *"done: relaxationNotice 제거"* 라고 명시돼 있었다. FE 타입에도 그 필드가 없다.
  즉 "정합화"라고 믿고 한 작업이 **계약 이탈**이었다. 되돌리는 데 커밋 하나가 더 들었다.
- 원인: `docs/api-spec.md` 는 정본이 아니라 **사본**이다(요청·SSE 계약의 정본은 FE/BE 팀 Notion).
  사본은 동기화 시점에 멈춰 있고, 해당 줄은 "제안(초안)" 딱지가 붙은 **채택되지 않은 AI 측 제안**
  이었다. "우리 문서에 적혀 있다"를 "합의됐다"로 읽은 것이다.
- 규칙:
  - **와이어 계약(요청 필드·SSE 이벤트·페이로드)을 건드리기 전에 정본을 연다.** Notion MCP 로
    `📡 API 명세서` → 해당 엔드포인트(CH-2 등) 페이지를 읽는다. 사본과 다르면 **정본이 이긴다.**
  - 사본에서 **"제안(초안)"·"🔴"** 표기가 붙은 항목은 합의된 계약이 아니다 — 구현 근거로 쓰지 않는다.
  - 소비자가 실제로 읽는지 **FE 코드로 확인**한다(`gh api repos/<org>/jarvis-frontend/contents/...`).
    타입 정의에 없으면 아무도 안 읽는 필드다. 로컬 클론은 낡을 수 있으니 원격을 본다.
  - 사본과 정본이 어긋난 걸 발견하면 **그 자리에서 사본을 정정**한다. 다음 사람이 같은 함정을 밟는다.
- 관련: `docs/api-spec.md` §3.1 relaxationNotice(v0.19.1 drift 정정), `app/schemas/chat.py` DoneData

## [2026-08-03] "FE 미구현일 것"이라고 추정하지 말고 FE 저장소를 읽는다
- 증상: 완화 칩 클릭 시 FE 동작이 계약 어디에도 없어서 "FE가 구현 안 했을 가능성이 높다 →
  FE 작업이 선행돼야 한다"고 사용자에게 두 번 보고했다. 실제로 FE 저장소를 열어 보니
  `SuggestionChips.onApply` → `useChat.applySuggestion` → `send(label)` 로 **이미 완전히 구현**돼
  있었다. 필요한 작업은 FE 쪽이 아니라 **AI 쪽 수신부**뿐이었다.
- 원인: "계약에 규격이 없다"에서 "구현이 없다"를 추론했다. 두 팀이 계약 문서화 없이 자연스러운
  방식(label 왕복)으로 각자 구현하는 일은 흔한데, 그 가능성을 배제했다.
- 규칙:
  - 다른 팀 소관으로 보이는 일을 보고하기 전에 **그 저장소를 실제로 읽는다.** 같은 org 라
    `gh api repos/<org>/<repo>/git/trees/main?recursive=1` 로 파일 목록부터 훑으면 몇 초다.
  - "계약에 없다"와 "구현이 없다"는 **다른 명제**다. 전자는 문서를 보고, 후자는 코드를 봐야 안다.
  - 사용자에게 작업 범위를 말할 때 확인한 것과 추정한 것을 **분리해 표기**한다.
- 관련: `jarvis-frontend` `src/features/chat/components/SuggestionChips.tsx`,
  `src/shared/chat/useChat.ts::applySuggestion`, 이슈 #113

## [2026-08-02] 공용 계약 모델에 필드를 더하면 그 모델을 쓰는 **모든 흐름의 와이어**가 바뀐다
- 증상: #113 이슈가 지시한 대로 `DoneData` 에 `relaxationNotice` 를 추가했더니 테스트 **29건**이
  깨졌다. 그 중 22건은 완화와 아무 상관 없는 장바구니·주문상태·일반대화·판매자-무관 경로였다.
  `DoneData` 가 추천 전용인 줄 알았는데 `cart/graph.py`·`order_status.py`·`buyer/graph.py`·
  `core/stream.py` 네 곳이 공유하고 있어, 완화 개념이 없는 흐름의 `done` 에도
  `"relaxationNotice": null` 이 실려 나갔다.
- 원인: 이슈 본문이 "`DoneData` 에 필드 추가(chat.py:152)"라고 **파일·줄까지 특정**해서, 그 클래스의
  사용처를 세지 않고 그대로 따랐다. 계약 모델은 이름이 흐름을 한정하는 것처럼 보여도(`DoneData` =
  "done 이벤트") 실제로는 여러 흐름의 공용일 수 있다.
- 규칙:
  - 계약 스키마에 필드를 더하기 전에 **`grep -rn "ClassName(" app/` 으로 생성 지점을 전부 센다.**
    2곳 이상이면 "이 필드가 저 흐름에도 의미가 있나"를 먼저 답한다.
  - 의미가 한 흐름에만 있으면 공용 모델을 넓히지 말고 **서브클래스로 좁힌다**
    (`RecommendationDoneData(DoneData)`). nullable 이라 하위호환이어도, 항상 null 인 필드는
    계약을 읽는 사람에게 "이 흐름에도 이 개념이 있다"는 거짓 신호다.
  - 이슈가 파일·줄을 특정해도 **그 지시의 전제(이 클래스는 이 흐름 전용이다)** 는 코드로 확인한다.
    특정성이 높을수록 검증 없이 따르기 쉬운데, 틀렸을 때 blast radius 도 그만큼 크다.
- 관련: `app/schemas/chat.py` `RecommendationDoneData`, 이슈 #113, api-spec §3.1 (6)

## [2026-08-02] 이슈 본문의 기술적 전제도 착수 전에 코드로 검증한다
- 증상: #113 은 "estCount 는 revert 칩과 동일하게 page-local 근사 사용"이라고 구현 방식까지
  지정했다. 그대로 만들었으면 **이슈가 대표 예시로 든 가격 완화 칩이 영원히 안 나오는** 코드가
  나올 뻔했다 — `priceMax`·`brand`·`color` 는 Spring I-1 쿼리 파라미터라 조건에 안 맞는 상품이
  응답에 아예 없고, page-local 로 세면 항상 0 이며, `estCount == 0` 칩은 계약상 제외되기 때문이다.
- 원인: revert 칩(이미 받아둔 후보를 AI 가 사후 억제 → 셀 수 있다)의 사정을 완화 칩(Spring 이
  걸러낸 것 → 셀 대상이 없다)에 그대로 옮긴 착오였다. **같은 "칩"이라도 데이터가 어디서 탈락하냐가
  다르면 계수 가능성도 다르다.** 정작 `schemas/spring.py` docstring 에는 "완화 칩 estCount 는 이
  값으로 못 구하고 재쿼리/BE count 필요"라고 **이미 적혀 있었다.**
- 규칙:
  - 이슈가 "X 방식으로 하면 된다"고 구현을 지정하면, 착수 전에 **그 방식이 성립하는지 소비처
    코드로 확인**한다. 특히 "기존 Y 와 동일하게"류 지시는 X 와 Y 의 전제가 같은지부터 본다.
  - 필터가 **어디서 적용되는지**(BE 쿼리 파라미터 vs AI 사후필터)를 먼저 가른다 — 이게 "그 조건을
    푼 결과를 셀 수 있나"를 결정한다. `_search_query_params` 가 그 경계다.
  - 전제가 틀렸으면 조용히 우회하지 말고 **근거(파일·줄)와 함께 드러내 확인받고** 진행한다.
- 관련: `app/services/spring_client.py::_search_query_params`, `app/schemas/spring.py`
  `ProductSearchResult` docstring, 이슈 #113

## [2026-08-03] counterfactual fixture가 실제 점수 표면에 닿는지 먼저 확인한다
- 증상: 개인화 paired 평가에서 글로벌 Sony/이어폰 취향을 모든 케이스에 공통 주입하자 대부분
  후보 집합과 교집합이 없어 clean/noisy/repeated 지표가 전부 같았다. repeated는 clean과
  preferences가 완전히 동일했고, clean→noisy margin 판정은 CI `[0, 0]`으로 vacuous pass했다.
- 원인: arm 이름과 fixture 서술만 다르게 만들고, 그 선호 축이 실제 후보의 category/brand 및
  profile-match 점수 성분에 닿아 순위를 움직일 수 있는지 확인하지 않았다.
- 규칙: **counterfactual arm fixture를 설계하면 그 fixture가 실제로 시스템 표면(후보 집합·점수
  성분)에 닿아 산출을 움직일 수 있는지 baseline 실측으로 먼저 확인한다 — arm 간 지표가 전부
  동일하면 측정이 아니라 장식이다.**
- 관련: #147, `evals/personalization/fixtures.py`

## [2026-08-03] LLM JSON은 프롬프트 타입과 실패 의미를 양쪽 경계에서 고정한다
- 증상: single-call live smoke 10회 중 5회가 `brand`와 `attrConditions`를 문자열로 내는
  등 스키마 타입 차이로 hard failure가 됐고, 정답 형태만 내는 dry-run fake는 이를 잡지
  못했으며 한 필드 오류가 전체 응답을 폐기했다.
- 원인: 프롬프트가 필드별 JSON 타입을 명시하지 않았고, pipeline은 단계별 파싱 실패를
  제한적으로 흡수하는 반면 single-call은 통합 응답 하나를 strict하게 파싱해 비교 arm의 실패
  의미가 비대칭이었다.
- 규칙: **구조화 LLM 출력 프롬프트에는 모든 필드의 타입과 예시를 명시하고, 비교 arm에는
  같은 의미의 field-lenient 실패 규칙을 적용한다.** 알려진 동치 타입은 정규화하고, 미지·검증
  실패 필드는 해당 필드만 드롭해 경고와 metric 벌점으로 남기되 전체 추천은 살린다. 형태
  순응도는 fake로 증명할 수 없으므로 전량 실행 전 실 provider 소형 smoke를 반드시 거친다.
- 관련: #146, `evals/ablation/single_call.py`, findings r3

## [2026-08-03] 스키마 기본 필드가 합집합 분모 지표를 오염시키지 않게 한다
- 증상: 전량 실행의 `filterAccuracy`가 0.036이었다. `model_dump`가 모델의 판단이 아닌
  `limit=30`과 `excludeProductIds=[]`까지 `extractedFilters`에 실어, 합집합 분모에서 매번
  불일치 벌점으로 계산됐다.
- 원인: 모델 산출과 검색 스키마의 기계적 기본값을 구분하지 않고 직렬화 결과 전체를 지표에
  전달했다.
- 규칙: **합집합 분모 지표에 넣는 산출에는 모델이 실제로 결정한 필드만 남긴다.** 스키마
  기본값은 제거 대상을 명시한 상수로 관리해 직렬화 단계에서 걸러낸다.
- 관련: #144, `evals/model_eval/adapter.py::_EXTRACTED_FILTER_EXCLUSIONS`,
  `evals/metrics`의 `filter_accuracy`

## [2026-08-03] 실패 신호가 정답 경로에서도 발생하는지 fixture로 먼저 확인한다
- 증상: 0건 결과가 정답인 failure slice `buy-cmap-0005`를 `emptyPush` hard failure로
  집계해, `hardFailureMax=0`인 release gate가 구조적으로 항상 실패할 뻔했다.
- 원인: push 부재를 검색 후보의 존재 여부와 무관하게 실패로 판정했다.
- 규칙: **실패 판정을 추가하기 전에 그 신호가 정답 경로에서도 발생하는지 fixture 실측으로
  확인한다.** 빈 후보가 기대 결과인 케이스의 push 부재는 hard failure로 세지 않는다.
- 관련: #144, `evals/model_eval/adapter.py`

## [2026-08-03] 커밋된 baseline 산출물의 일치 검증은 manifest의 소스 해시까지 포함해서 한다
- 증상: #145 리뷰 round-3에서 `config.py` validator를 수정한 뒤 "dev-v1 비교 True"로
  보고했지만, 오케스트레이터의 독립 재실행 비교는 False였다. 지표·순위 아티팩트는 전부
  동일했고, 양 arm `run_manifest.json`의 `hashes.config`(`config.py` SHA-256)와 `commitSha`만
  어긋나 있었다.
- 원인: run manifest는 재현 가능성을 위해 소스 파일 해시를 기록하므로, **소스를 만지는 모든
  리뷰 라운드가 커밋된 baseline manifest를 무효화한다.** 결과(지표)가 안 변하는 수정이라는
  생각에 재생성을 건너뛰었고, 일치 검증도 결과 파일 위주로 봐서 manifest 드리프트를 놓쳤다.
- 규칙: **manifest가 소스 해시를 기록하는 커밋된 산출물은, 그 소스를 수정하는 라운드마다
  재생성을 기본 절차에 포함한다.** 일치 검증은 결과 파일만이 아니라 normalize 대상 전체
  (manifest 포함)의 byte 비교로 한다 — "지표 불변"과 "산출물 일치"는 다른 명제다.
- 관련: #145, `evals/scoring/baselines/dev-v1/*/run_manifest.json`(`hashes.config`),
  `evals/scoring/cli.py::normalize_paired_artifacts`, `evals/metrics/run_manifest.py`

## [2026-08-03] 결정론 검증은 실행마다 달라지는 인자를 실제로 바꿔서 한다
- 증상: #143 metric runner의 byte-identical 검증을 같은 `--out` 경로로 두 번 실행해
  통과시켰지만, 서로 다른 출력 경로로 재실행하자 `run_manifest.json`의 `command`에 경로가
  들어가 normalized 비교가 실패했다.
- 원인: 두 실행에서 달라질 수 있는 입력을 하나도 바꾸지 않아, 실행 인자가 산출물로 새는 경로를
  검증이 구조적으로 관측하지 못했다.
- 규칙: **byte-identical 결정론을 주장하려면 출력 경로·실행 위치처럼 실행 인스턴스마다 달라지는
  인자를 실제로 바꾼 두 실행을 비교한다.** 같은 인자의 반복 실행은 인자 누수를 잡지 못한다.
  `command`·`runId`·`timestamp` 같은 비결정 실행 정보는 manifest의 격리 섹션 한 곳에만 둔다.
- 관련: #143, `evals/metrics/run_manifest.py`, `evals/metrics/report.py::normalize_artifacts`,
  `tests/unit/test_eval_metric_report.py`

## [2026-08-03] `Settings(_env_file=None)`은 OS 환경변수까지 차단하지 않는다
- 증상: #143 오프라인 평가 adapter가 `Settings(_env_file=None, ...)`로 고정 설정을 만든다고
  보았지만, `EXPOSE_MAX=5`와 `9`에서 coverage가 각각 0.387931과 0.556034로 달라졌고 manifest에는
  그 차이를 설명할 흔적이 없었다.
- 원인: pydantic-settings의 `_env_file=None`은 dotenv 소스만 제거하고 env 소스는 유지하므로,
  프로세스 환경이 평가 결과에 조용히 스며들어 결정론 주장과 재현 manifest를 함께 무효화했다.
- 규칙: **결정론 실행 하네스는 `settings_customise_sources`로 env·dotenv 소스를 모두 제거한
  전용 Settings 서브클래스를 사용한다.** 환경변수를 실제로 바꾼 두 실행의 산출물이 같다는 회귀
  테스트도 함께 둔다.
- 관련: #143, `evals/metrics/settings.py::EvaluationSettings`,
  `tests/unit/test_eval_metric_harness.py::test_offline_adapter_ignores_process_environment`

## [2026-08-03] 한쪽 후보 집합에서 뽑은 라벨로 전 카탈로그 리트리버의 우열을 결론내지 않는다
- 증상: 방식1(전 카탈로그 벡터검색)과 방식2(Spring 후보 재정렬)의 recall을 비교했을 때
  방식2가 26건 전부에서 우세했지만, 26건의 정답이 모두 Spring 후보 안에 들어 있었다.
- 원인: 골든셋 라벨을 Spring I-1 응답에서 골라 만들었다. 후보 집합 밖의 정답은 애초에
  라벨이 될 수 없어, Spring 후보를 재정렬하는 방식2의 recall 상한이 구조적으로 1.0이 됐다.
- 규칙: **리트리버 A와 B를 recall로 비교하려면 라벨이 두 후보 집합 어느 쪽에도 종속되지
  않아야 한다.** 한쪽 출력에서 라벨을 뽑았다면 결론은 상대가 그쪽을 못 이긴다는 데까지만
  한정하고, 그쪽이 더 낫다고 쓰지 않는다. 측정 설계의 편향은 결과와 함께 반드시 적는다.
- 관련: #32, `evals/goldenset/`, `app/pipelines/compare.py`

## [2026-08-03] 카테고리 오분류를 상품 부적합으로 착각해 정답을 금지 결과로 라벨링했다
- 증상: 골든셋 케이스 `buy-srch-0005`(질의 `체육수업용 축구공 추천`)에서 상품
  9356761664(`아디다스 피파월드컵 26 트리온다 리그 박스 축구공 5호`)와
  9445707691(`스펀지 유아 어린이 스펀지볼 축구공`)을 **금지 결과**로 지정했다. 둘 다 실제
  축구공이고 카테고리만 `스포츠 모자`·`붓`으로 오분류돼 있었다.
- 원인: `categoryName`이 이상하다는 것과 상품이 질의에 안 맞는다는 것을 같은 신호로 봤다.
  같은 데이터셋의 `buy-cmap-0001`(신라면, 벽지 오분류)·`buy-cmap-0002`(멸균우유, 반찬
  오분류)는 정확히 같은 상황을 "카테고리는 틀렸지만 상품이 맞으니 정답"으로 옳게 판정해
  놓고, 한 케이스만 반대로 갔다.
- 규칙: **금지 결과는 상품이 질의에 안 맞을 때만 지정한다 — 카테고리 오분류는 그 자체로
  부적합 근거가 아니다.** 금지로 올리기 전에 상품명·설명을 읽고 질의와 직접 대조하라.
  정답을 금지로 지정하면 하드제약 위반율이 올바른 추천을 벌점으로 세어 지표가 뒤집힌다.
- 관련: #142, `evals/goldenset/cases/buyer_dev.jsonl`

## [2026-08-03] 하드제약 표기가 실제 데이터 표기와 달라 영영 발동하지 않는 가드를 만들었다
- 증상: `hardConstraints.forbiddenCategories`에 `"구기/라켓/스포츠 > 축구"`,
  `"당뇨관리용품 > 침/바늘"`로 적었는데 스냅샷 상품의 `categoryName`은 `"축구"`,
  `"침/바늘"`이었다. 체커가 붙는 순간 **위반이 절대 발동하지 않는** 가드였다.
- 원인: 카테고리 값을 pg-catalog `product_document.category`(`대분류 > 소분류`)에서
  가져왔는데, 소비처는 Spring I-1 응답의 `categoryName`(소분류만)이다. 같은 케이스 안에서
  `expectedFilters.category`는 I-1 표기를 쓰고 있어 **한 레코드 안에서 표기가 갈렸다.**
- 규칙: **제약 값은 그 값이 비교될 데이터의 표기로 적고, 실재 여부를 검증으로 강제한다.**
  두 저장소가 같은 개념을 다른 문자열로 부르면(같은 카테고리를 계층 경로 vs 말단 이름으로)
  어느 쪽이 비교 대상인지 먼저 확인하라. 통과만 하는 가드는 없는 가드보다 나쁘다.
- 관련: #142, `evals/goldenset/schema.py`, `evals/goldenset/fixtures/catalog_snapshot.json`

## [2026-08-03] 후보가 전부 정답인 평가 케이스는 어떤 랭커도 만점이라 판별력이 없다
- 증상: holdout 12건 중 4건에서 fixture의 후보 집합과 정답 집합이 완전히 같았다
  (`buy-srch-1002` 2/2, `buy-cmap-1001` 1/1, `buy-srch-1003` 2/2, `buy-srch-1004` 1/1).
- 원인: 라이브 I-1이 소수만 반환하는 질의로 케이스를 만들고, 반환된 것을 전부 정답으로
  판정했다. 오답 후보(distractor)가 없으면 순위 지표는 구현이 무엇이든 항상 1.0이다.
- 규칙: **순위 지표용 케이스는 오답 후보가 후보 집합에 함께 들어오도록 만든다.** 후보를
  넓혀도 오답이 안 생기는 질의는 순위 케이스로 쓰지 말고, 노출·필터·하드제약 검증용으로만
  쓰되 그 사실을 산출물에 드러내 하류가 순위 지표 분모에서 제외하게 하라.
- 관련: #142, `evals/goldenset/audit.py`(`nonDiscriminativeRanking`)

## [2026-08-03] config에 튜너블을 추가하고 배선하지 않으면 초록불인데 동작은 안 바뀐다
- 증상: `goldenset_*` 설정 6개를 추가했는데 실제 소비처가 있는 것은 2개뿐이었다.
  `goldenset_snapshot_per_query_max`는 소비처가 없고 `snapshot.py`가 같은 값 `30`을
  따로 하드코딩하고 있었다. 테스트는 설정 **값의 유효성**만 검사해서 전부 통과했다.
- 원인: 설정을 선언하는 것과 주입하는 것을 같은 일로 봤다. 값 검증 테스트는 설정이
  동작에 연결됐는지를 전혀 확인하지 않는다.
- 규칙: **튜너블을 추가하면 그 값을 바꿨을 때 동작이 실제로 달라지는 테스트를 함께 쓴다.**
  소비처가 없는 설정은 남기지 말고 지운다. 값 유효성 검증만으로는 "설정이 있다"는 착각만
  남는다.
- 관련: #142, `app/core/config.py`, `evals/goldenset/snapshot.py`, `evals/goldenset/audit.py`

## [2026-08-03] 예산 검증을 걸기 전에 그 상한이 재는 **구간**을 emit 순서로 확인한다
- 증상: #133 에서 `spring_timeout_s × (재시도+1) < stream_first_token_timeout_s` 기동 검증을 넣고
  근거를 *"검색은 첫 SSE 토큰보다 앞이라, 재시도가 길어지면 first-token 예산을 태워 504 가 된다"*
  로 코드 주석·api-spec·SPEC·CHANGELOG·커밋 메시지 **다섯 곳에 퍼뜨렸다.** PR #235 리뷰를 보다
  #241(#138)의 새 lessons 를 읽고 코드를 다시 따라가 보니 **정반대**였다 — 추천 경로의 첫 SSE
  이벤트는 `conditions`(`recommendation/graph.py`)이고 **검색은 그 뒤**다. first-token 시계는
  `conditions` 에서 멈추므로 검색 재시도는 그 예산을 **한 톨도 쓰지 않는다.** 검증식은 우연히
  참(6s < 10s)이라 테스트도 CI 도 통과했다 — **틀린 근거가 초록불을 달고 문서에 박혔다.**
- 원인: 파이프라인 개념도(`검색 → 재정렬 → 응답`)를 **시간 축**으로 읽었다. 개념도는 데이터
  흐름이지 **발신 시점**이 아니다. 상한의 이름(`first_token`)도 오해를 도왔다 — 실제로는 첫
  *텍스트 토큰*이 아니라 **첫 이벤트**를 재고(§2.9 c), 구매자 경로는 텍스트 앞에 `conditions`
  가 나간다. 이 저장소엔 이미 같은 계열의 기록이 둘 있었는데(2026-08-02 「물려받은 설계 문서의
  주장은 인용 전에 코드로 확인한다」, #138 「상한이 실제로 재는 지점을 코드에서 확인한다」)
  **둘 다 있는 상태에서 같은 실수를 했다.**
- 규칙:
  - 타임아웃·예산 검증을 **추가하기 전에** 그 상한이 끝나는 지점을 `grep -n 'yield sse('` 로
    실측한다. "무엇을 재는가"는 상한 **이름이 아니라 emit 순서**가 정한다.
  - 검증식이 통과한다는 것은 **근거가 옳다는 증거가 아니다.** 부등식이 우연히 참이면 틀린
    전제가 그대로 살아남는다. 근거 문장은 코드 한 줄로 반증 가능한 형태로 적는다.
  - 근거를 문서 여러 곳에 복사하기 전에 **한 번 더 확인한다.** 퍼진 뒤 정정하면 api-spec·SPEC·
    CHANGELOG·커밋 메시지를 모두 손봐야 한다(이번에 실제로 그랬다).
- **[2026-08-04 갱신] 이 전제는 #113 으로 다시 바뀌었다.** 자동 완화가 검색 **후에** 조건을 바꿀
  수 있는 턴은 표시-실제 불일치를 막으려고 `conditions` 를 검색 뒤로 미룬다 — 그 턴에서는 검색
  재시도가 first-token 예산을 **실제로 쓴다.** 위 "한 톨도 쓰지 않는다"를 **현재 사실로 읽지 말
  것**(그 시점의 기록이다). 검증기는 이제 전체 상한·first-token 상한 **둘 다**와 비교한다.
  이 항목이 남기는 교훈은 방향이 아니라 **"emit 순서가 정한다"** 는 규칙 자체다 — 순서를 바꾸는
  변경은 이 검증기의 전제도 함께 갱신해야 한다.
- 관련: `app/core/config.py` `_require_search_retry_within_stream_budget`,
  `app/agents/buyer/recommendation/graph.py`(`conditions` ↔ search 순서), api-spec §2.9 c,
  PR #235 리뷰, #138/#241, #113/PR #248 3차 리뷰

## [2026-08-02] 타임아웃을 판단하기 전에 상한과 지표의 측정 지점을 구분한다
- 증상: #151 댓글과 baseline README가 `slo_first_token_ms` 10초에
  `client_ttft_ms` p95 9970ms가 닿는다며 런타임 first-token 상한도 임박한 것으로 해석했다.
  #138에서 다시 재보니 상한이 실제로 기다리는 첫 SSE 이벤트 p95는 1.4~3.0초였고, measured
  120건 중 504는 0건이었다.
- 원인: `stream_first_token_timeout_s`는 api-spec §2.9(c)의 **스트림 첫 이벤트까지**를 재며
  runner의 `client_first_event_ms`에 해당하지만, `slo_first_token_ms`는 첫 텍스트 토큰인
  `latencyFirstToken`·`client_ttft_ms`를 평가한다. 구매자는 `conditions` 등이 텍스트보다 먼저
  나오고, 판매자는 `meta`·`progress`를 p95 1.4초에 보낸 뒤 텍스트를 마지막에 한 번 내보내므로
  둘의 차이가 더 커진다. first-token SLO 초과 37/154 중 32건이 seller/analysis였던 것도
  판매자 지연이 아니라 성격이 다른 지점을 비교한 결과다.
- 규칙: **지연 수치로 타임아웃을 조정하기 전에 코드에서 그 상한이 실제로 재는 지점을 확인한다.**
  런타임 상한 대상인 `client_first_event_ms`와 SLO 대상인
  `client_ttft_ms`·`latencyFirstToken`을 섞지 않고, 텍스트를 마지막에 한 번 내보내는 판매자
  경로에는 첫 텍스트 토큰 기준 SLO를 그대로 적용하지 않는다.
- 관련: #138, #151, `app/core/stream.py`(`ft_deadline`), api-spec §2.9(c),
  `app/core/config.py`(`slo_first_token_ms`), `evals/benchmark/baselines/README.md`

## [2026-08-02] 부하 측정 전에 앱 자기 레이트 리밋을 측정 경로에서 분리한다
- 증상: 로컬 벤치마크의 measured 270건 중 120건(44%)이 429였고, 마지막 시나리오는 113건
  전량이 `RATE_LIMITED`였다. 성능 대신 앱 자기 리밋을 측정한 실행이라 기준선으로 폐기했다.
- 원인: `auth_mode=dev`의 무토큰 요청은 모두 같은 게스트 신원을 사용해 §2.8 토큰 스코프
  레이트 리밋(분 10·시간 100)을 공유한다. 여러 시나리오의 부하 요청이 한 사용자의 한도를 함께
  소진하므로 기본 설정은 부하 측정과 구조적으로 충돌한다.
- 규칙: **로컬·staging 부하 측정 전에 타깃의 `RATE_LIMIT_PER_MIN/HOUR`를 상향하고 그 사실을
  manifest에 남긴다.** 결과 해석 전에 429 비율부터 확인하며, 앱 자기 리밋이 섞인 측정치는 성능
  근거로 쓰지 않는다.
- 관련: #151, api-spec §2.8, `evals/benchmark/baselines/README.md`

## [2026-08-02] LLM_UNAVAILABLE을 앱 용량 문제로 결론내기 전에 provider 응답을 확인한다
- 증상: 로컬 벤치마크 동시성 5·10에서 `error_type=LLM_UNAVAILABLE` 45건이 발생해 앱이
  동시성에 약한 것처럼 보였다.
- 원인: 서버 로그의 실제 원인은 `api.openai.com` 응답 `429 Too Many Requests`였다. 앱 오류
  코드는 provider 쿼터와 다른 LLM 가용성 실패를 구분하지 않으므로 결과 레코드만으로 원인을
  확정할 수 없다.
- 규칙: **LLM_UNAVAILABLE을 앱 용량 문제로 해석하기 전에 provider 응답 코드를 서버 로그에서
  확인한다.** 개인 키로 잰 동시성 수치는 provider 스로틀이 없다는 근거 없이는 앱 성능 수치가
  아니다.
- 관련: #151, `evals/benchmark/baselines/README.md`

## [2026-08-02] 프롬프트 계층 intent 안정성은 FakeLLM 단위 테스트가 아니라 실 LLM 반복 분포로 증명한다
- 증상: #234의 `"그거 보여줘"` intent가 같은 입력에서도 `recommend`·`cart_view` 사이를 오갔지만,
  `tests/unit/test_decompose.py`는 LLM JSON을 주입하므로 프롬프트를 어떻게 바꿔도 계속 통과했다.
- 원인: 결함은 파서나 라우팅 코드가 아니라 `gpt-5-nano`가 긴 `_SYSTEM` 규칙을 해석하는 확률적
  계층에 있었다. 특히 `PENDING_CART`가 있으면 아래쪽 옵션 답변 규칙이 위쪽 지시대명사 경계를
  덮는 현상은 현실적인 세션 상태를 넣은 반복 호출에서만 드러났다.
- 규칙:
  - 프롬프트 intent 결함은 실제 provider/model과 현실적인 세션 상태를 넣고, 발화 × 컨텍스트를
    여러 번 반복해 **분포**로 수정 전/후를 비교한다. FakeLLM 테스트 통과를 동작 증거로 쓰지 않는다.
  - 상태만 현실적으로 채우는 것으로 끝내지 말고 **그 상태에서 실제로 나오는 정상 발화**도 대조군에
    넣는다. 옵션 되물음 상태라면 옵션명·번호 답변을 넣고 intent뿐 아니라 `cart.optionId`까지 집계한다.
  - 단위 테스트는 실측으로 유효성이 확인된 필수 프롬프트 문구의 회귀 가드로만 쓴다. 채택 전 해당
    문구를 지워 테스트가 실제로 실패하는지 확인한다.
  - 목표가 8/8인데 잔여 지터가 있으면 단일 성공 사례로 덮지 말고 정확한 분포와 미달 셀을 남긴다.
- 관련: `app/agents/buyer/recommendation/decompose.py`, `tests/unit/test_decompose.py`, 이슈 #234

## [2026-08-02] 프롬프트는 실제 세션 맥락을 모두 채운 상태로 검증한다
- 증상: `LAST_RECOMMENDATIONS` 없이 재구매 분해를 실측했을 때 오탐이 없었지만, 현실적인 직전 추천
  목록을 넣자 `"그거 다시 추천해줘"`가 목록의 상품 1~3개를 재구매 지목으로 복사했다.
- 원인: 빈 맥락 프로브는 모델이 실제 세션에서 받는 선택지 목록과 이웃 규칙의 영향을 제거해,
  배포 경로에서만 나타나는 맥락 에코를 관측할 수 없었다.
- 규칙: **프롬프트 필드는 실제 세션이 싣는 맥락을 모두 채워 검증한다.** prior·직전 추천·pending 등
  운영 입력을 비운 대조군만으로 오탐 부재를 결론내리지 않는다.
- 관련: #120, `app/agents/buyer/recommendation/decompose.py`, `LAST_RECOMMENDATIONS`

## [2026-08-02] 새 프롬프트 필드에는 이웃 목록을 복사하지 말라는 금지도 함께 쓴다
- 증상: `cart_add`가 `LAST_RECOMMENDATIONS`에서 상품을 고르라는 규칙 바로 옆의
  `repurchaseProducts`도 같은 맥락을 지목하자, 모델이 사용자의 단수 지시대명사 대신 목록 전체나
  일부를 새 필드에 복사했다.
- 원인: 새 필드에 무엇을 넣을지만 적고 **무엇을 넣지 말아야 하는지** 경계를 쓰지 않아, 모델이
  이웃 규칙의 준비된 목록을 가장 쉬운 해답으로 재사용했다.
- 규칙: 목록 맥락 옆에 새 산출 필드를 추가하면 **목록에 있다는 이유만으로 복사하지 말라**는 부정
  규칙과 사용자 발화에서 직접 확인해야 할 조건을 함께 명시한다.
- 관련: #120, `app/agents/buyer/recommendation/decompose.py::_SYSTEM`

## [2026-08-02] 부분 문자열 매칭은 포함 방향마다 의미가 다르다
- 증상: 재구매 지목 `"무선 이어폰 케이스"`가 최근 구매명 `"이어폰"`을 포함한다는 이유로, 실제로
  지목하지 않은 짧은 이름의 구매 상품이 exact 제외에서 풀렸다.
- 원인: 표기 차이를 흡수하려 양방향 부분비교(`지목 in 구매명 or 구매명 in 지목`)를 썼지만,
  역방향은 더 구체적인 지목을 더 일반적인 다른 구매명으로 축약해 버린다.
- 규칙: **부분 문자열 비교는 방향별 의미를 따로 검증한다.** 사용자가 지목한 명사구를 구매명 안에서
  찾는 방향만 허용하고, 구매명이 지목 안에 든다는 이유로 더 긴 지목을 짧은 상품에 매칭하지 않는다.
- 관련: #120, `app/agents/buyer/recommendation/graph.py::_resolve_repurchase_ids`

## [2026-08-02] 억제를 추가하면 같은 축의 되돌리기 경로도 함께 만든다
- 증상: 최근 구매 카테고리에는 되돌리기 경로가 있었지만 exact 상품 제외에는 없어, 사용자가 특정
  상품을 다시 사고 싶다고 명시해도 해당 상품은 추천에서 계속 제외됐다.
- 원인: 최근 구매 억제 축이 exact 상품과 소모품 카테고리 둘인데 되돌리기는 카테고리에만 만든
  비대칭 때문에, 사용자가 exact 제외에서 빠져나올 경로가 없었다.
- 규칙: **억제(suppress)를 추가하면 그 축의 되돌리기(revert) 경로도 같은 커밋에서 만든다 —
  억제 축이 둘인데 되돌리기가 하나면 사용자는 절대 빠져나올 수 없다.**
- 관련: #120, `app/agents/buyer/recommendation/graph.py`, `app/agents/buyer/recommendation/decompose.py`

## [2026-08-02] 회귀 테스트는 "회귀를 흉내 내 실패시켜 본 뒤" 채택한다 — 통과만으로는 지켜준다는 증거가 아니다
- 증상: #173 에서 "티어화가 원본 price 를 건드리지 않는다"를 고정하려고
  `test_price_tiering_does_not_mutate_product_or_filter_values` 를 넣었는데, 본문이
  `_price_tier(product.price, ...)` 를 호출한 뒤 `product.price == 39000` 을 단언하고 있었다.
  `_price_tier` 는 **값**을 받는 순수 함수라 애초에 상품을 변형할 수 없다 — 구현이 어떻게 망가져도
  **절대 실패하지 않는** 단언이었다. 초록불이 지켜준다는 착각만 남는다.
- 원인: 테스트를 "무엇을 지키고 싶은가"(rerank 를 태워도 원본이 안 바뀐다)가 아니라 **"어떤 함수를
  방금 짰는가"** 를 기준으로 썼다. 헬퍼를 직접 부르면 통과시키기는 쉽지만, 정작 회귀가 일어날 경로
  (`rerank()` 전체)는 지나가지 않는다.
- 규칙:
  - **회귀 테스트를 채택하기 전에 그 회귀를 실제로 흉내 내 본다.** 구현을 되돌리거나(런타임
    몽키패치로 충분하다) 셋 함수를 상수 반환으로 바꿔놓고 테스트가 **실패하는지** 확인한다.
    실패하지 않으면 그 테스트는 커버리지가 아니라 장식이다. 리포 파일을 고칠 필요 없이
    `setattr(module, "_fn", ...)` 로 검증하면 작업 트리가 더러워지지 않는다.
  - **단언은 헬퍼가 아니라 실제 진입점을 태운 뒤 건다** — 지키려는 불변식이 "파이프라인을 통과해도
    원본이 그대로"라면 파이프라인을 실제로 호출해야 한다.
  - 순수 함수에 "변형되지 않았다"를 거는 단언이 보이면 **구조상 항상 참인지** 먼저 의심한다.
- 관련: `tests/unit/test_recommendation.py` `test_price_tiering_does_not_mutate_product_or_filter_values`,
  `app/agents/buyer/recommendation/rerank.py` `_price_tier`, 이슈 #173

## [2026-08-02] 명세가 규정한 동작이 구현되지 않은 채 몇 달 지나갔다 — 이슈가 "새 요구"로 올라올 때까지
- 증상: #133 이 *"검색 실패에 재시도가 없다"* 를 결함으로 올렸고 신규 기능처럼 읽혔다. 착수해서
  `docs/specs/SPEC-RECOMMEND-001.md` 오류 처리 표를 열어 보니 **이미** *"`search` 실패: 최대 1회
  재시도 후 `error`(`SEARCH_FAILED`)"* 라고 적혀 있었다. `grep -n "retry" app/services/spring_client.py`
  는 **0건**. 즉 새 요구가 아니라 **명세-구현 갭**이었고, 그 사이 검색은 한 번 실패하면 그대로
  턴이 끝났다. rerank 폴백 문구도 같은 성격이다 — REQ-REC-062 의 *"일반 코멘트"* 라는 초판
  표현이 "정상 경로와 구분되지 않는 문구"로 구현돼, 판매자에만 있는 degrade 정직성 게이트와
  비대칭이 된 것을 아무도 대조하지 않았다.
- 원인: 명세의 **표**(오류 처리표·타임아웃 기준표)는 사람이 읽고 넘기기 쉬운 형태다. 산문 요구는
  구현 시 근거로 인용되지만, 표의 한 칸은 "참고 자료"처럼 취급돼 대조 없이 지나간다. 게다가 이
  저장소는 스텁에 `api-spec §` 주석으로 코드↔명세를 잇는데, **표의 칸에는 대응 스텁이 없어**
  링크가 애초에 만들어지지 않았다.
- 규칙:
  - 주제 착수 시 SPEC 의 **오류 처리표·타임아웃표를 grep 한 줄로 기계 대조**한다. "재시도"라고
    적혀 있으면 `grep -n "retry\|재시도" <해당 모듈>` 로 존재를 확인한다. 표를 눈으로 읽고
    넘기지 않는다.
  - 이슈가 "없다/안 한다"를 결함으로 올리면 **먼저 명세를 찾아본다.** 이미 규정돼 있으면 그건
    설계 논의가 아니라 밀린 구현이며, 계약 개정 없이 바로 짜면 된다(범위·리스크가 크게 줄어든다).
  - 명세 문구가 동작을 **덜 특정하면**(예: "일반 코멘트") 구현이 최악의 해석으로 굳는다. 그런
    칸을 발견하면 고치는 김에 **요구를 조여 다시 적는다**(#133 은 REQ-REC-064 로 분리 신설).
- 관련: `docs/specs/SPEC-RECOMMEND-001.md` §오류 처리 표·REQ-REC-062/064, `app/services/spring_client.py`
  `search_products`, 이슈 #133

## [2026-08-02] 병렬 워크트리에서 repo 전역 포매터를 돌리면 남의 파일을 커밋에 끌고 온다
- 증상: #137 작업 중 CLAUDE.md 커밋 워크플로대로 `uv run ruff check --fix && uv run ruff format` 를
  돌렸더니, 내가 만들지도 않은 `tests/unit/test_home_recommendation.py` 가 수정됨으로 떴다. 내용은
  ruff 최신판의 assert 메시지 줄바꿈 스타일 변경 — **기능과 무관한 순수 포맷 churn** 이었다.
  스코프 가드로 `git status` 를 허용 파일 목록과 대조하지 않았으면 그대로 커밋될 뻔했다.
- 원인: `ruff format` 는 인자를 안 주면 **repo 전체**를 포맷한다. `dev` 에 이미 들어가 있던 파일이
  현재 ruff 버전 기준으로는 미포맷 상태라 건드려진 것이다. 평소(단일 브랜치)엔 무해하지만,
  **같은 `dev` 를 향해 여러 워크트리가 동시에 달릴 때는** 무관한 파일 수정이 곧 3-way merge
  충돌이고 남의 PR 을 깨뜨린다.
- 규칙:
  - 병렬 워크트리 작업에서는 포매터·자동수정을 **내가 만진 파일에만** 건다:
    `uv run ruff format <내 파일들>` / `uv run ruff check --fix <내 파일들>`.
  - 커밋 직전 `git status --porcelain` 을 **허용 파일 목록과 기계적으로 대조**한다. 눈으로 훑지 말고
    `grep -v -E "<허용 목록>"` 로 걸러 남는 게 있으면 멈춘다.
  - 무관한 포맷 churn 이 생겼으면 고쳐 넣지 말고 `git checkout --` 로 되돌린다. 별건이다.
- 관련: `tests/unit/test_home_recommendation.py`(되돌림), CLAUDE.md 커밋 워크플로 2단계

## [2026-08-02] 0 을 싣는 이벤트를 비율 분모에 넣으면 커버리지 100% 와 함께 지표가 반토막 난다
- 증상: #137 관측 집계기가 "쿼리당 비용 평균"을 실제의 **절반**으로 냈다. 실행 턴이 전부 $0.02
  인데 평균이 $0.01 로 나왔고, 커버리지는 **100%** 라 "완전히 측정된 값"으로 읽혔다. 즉 틀린
  숫자가 "믿을 만하다"는 표시를 달고 나왔다 — 발표 자료에 그대로 올라갔으면 최악이다.
- 원인: `emit_rejection`(429/409/504, 스트림 전 거부)이 `costUsd: 0.0` 을 싣는다. 이 턴들은 LLM 을
  **한 번도 부르지 않았는데** 집계기가 "costUsd 가 숫자로 있는 턴"이라 정당한 0 표본으로 셌다.
  누락(`null`)은 제외하도록 잘 짜 놨는데, **0 을 명시적으로 싣는 경로**는 그 방어를 그냥 통과했다.
- 규칙:
  - 비율·평균을 만들 때 **분모에 뭐가 들어오는지 producer 코드에서 실측**한다. `null` 제외만으로는
    부족하다 — "구조적으로 0 일 수밖에 없는 이벤트"가 유효 표본으로 섞이는지 따로 본다.
  - 지표마다 분모가 다르면(여기선 degrade·error 는 전체 턴, 비용은 실행 턴) **출력에 그 사실을 적는다.**
    분모를 조용히 좁히면 분모 조작과 구분되지 않는다. 제외한 건수도 함께 낸다.
  - 커버리지·표본 수 같은 "신뢰도 표시"는 **분모가 옳다는 전제 위에서만** 의미가 있다. 100% 가
    "정확하다"는 뜻이 되지 않게, 분모 정의를 테스트로 고정한다.
- 관련: `scripts/aggregate_observability.py` `_executed`/`_cost_stats`, `app/core/observability.py`
  `emit_rejection`, `tests/unit/test_aggregate_observability.py::test_rejection_zero_cost_does_not_dilute_cost_average`

## [2026-08-02] 물려받은 설계 문서의 주장은 인용 전에 코드로 확인한다 — 그때는 맞았어도 지금은 틀릴 수 있다
- 증상: #217 을 하면서 지연 영향을 "첫 토큰은 안 늦고 상품 카드만 늦는다"로 설계 문서·PR·사용자
  설명에 반복해 적었다. 근거는 DESIGN-NEEDS-EXPANSION-198 §8 의 문장이었고 **읽고 그대로 옮겼다.**
  뒤늦게 emit 순서를 코드로 따라가 보니 추천 턴의 `token` 은 **rerank 뒤**에 나온다 —
  `_prepare_recommendation`(매핑·전개) → `conditions` → search → rerank → `token`. 전개가 늦추는 것은
  `products.ready` 만이 아니라 **첫 SSE 프레임부터 전부**였다.
- 원인: 초판 문장의 `"token 스트림은 별개 경로"` 는 응답을 바로 흘리는 **general intent** 얘기인데,
  전개는 **recommend intent** 에서만 돈다. 적용 범위가 다른 주장을 조건 확인 없이 가져다 썼다.
  게다가 #217 은 그 지점 앞에 매핑 왕복을 **하나 더** 붙이므로, 물려받은 부정확이 이번 변경으로
  더 커지는 구조였다.
- 규칙:
  - **설계 문서의 주장을 인용해 새 문서·PR 에 옮길 때는 그 주장이 지금 코드에서도 참인지 확인한다.**
    특히 순서·지연·"영향 없음" 류는 코드가 옮겨 다니면 조용히 거짓이 된다.
  - **내 변경이 그 주장의 전제를 건드리면 반드시 재검증한다** — 이번엔 "전개 앞에 왕복 추가"가
    정확히 그 전제였다.
  - 지연 주장은 **emit 지점을 직접 grep 해서** 확인한다(`yield sse("token"` 위치). 문서 문장이 아니라
    코드 순서가 근거다.
- 관련: `docs/specs/DESIGN-NEEDS-EXPANSION-198.md` §8(#217 로 정정), `app/agents/buyer/graph.py`
  `_prepare_recommendation` 호출 지점, `app/agents/buyer/recommendation/graph.py` conditions/token emit

## [2026-08-01] 임계를 통과했다고 "정답"이 아니다 — top1 **문자열**만 보고 매핑 성공으로 판정하지 말 것
- 증상: #217 실측을 정리하면서 `"베이킹 재료"` 를 **매핑 성공**(거리 0.2068 ≤ 0.22, 마진 0.0397)으로
  분류하고 "전개 불필요"라고 보고했다. 사용자가 "베이킹 재료는 짤주머니·틀·밀가루가 다 나와야
  하는 것 아니냐"고 되물어 top1 을 열어 보니 `주방용품 > 홈베이킹용품` — **도구** 칸이었다.
  사용자가 원한 재료(밀가루·버터·설탕)는 `가공식품 > 밀가루/홈베이킹/믹스`(2위, 0.2466)에 있었고,
  **정답 칸이 taxonomy 에 실재하는데 1위가 근소하게(0.04) 틀린 쪽을 집은 것**이었다.
- 원인: 임계 판정(`거리 ≤ 0.22`)은 "가까운 칸을 찾았다"만 말한다. 그 칸이 **사용자가 원한 상품
  집합을 주는지**는 다른 질문인데, 숫자가 통과하니 검증을 멈췄다. 카테고리명이 어휘를 공유하면
  (`베이킹`·`용품`) 거리는 더 가까워져 **오분류일수록 통과하기 쉬운** 방향으로 편향된다 —
  DESIGN-59 §4.3.1 이 "추상 라벨의 가짜 근접"으로 이미 경고한 현상의 다른 얼굴이다.
- 규칙:
  - **매핑 실측 표를 만들 때 top1 문자열이 아니라 "그 칸에 무엇이 들어 있는가"를 적는다.**
    `홈베이킹용품 = 틀·짤주머니(도구)` 처럼 한 줄 주석을 달면 가짜 성공이 눈에 띈다.
  - **1·2위를 함께 본다.** 마진이 얇으면(≤0.05) 2위를 열어보고 "1위가 정말 더 맞나"를 사람이 판단.
    `베이킹 재료` 는 마진 0.0397 이라 §4.4 택일 트리거(0.02)에도 안 걸리는 사각지대였다.
  - **결론을 "성공/실패"로만 쓰지 않는다** — "성공(내용 확인함)" / "성공(숫자만)" 을 구분해 적고,
    후자는 근거로 쓰지 않는다. 이번엔 그 구분이 없어서 "전개 불필요" 판단이 한 번 뒤집혔다.
  - 사용자가 결과에 의문을 표하면 **숫자를 다시 보여주지 말고 데이터를 열어본다** — 같은 숫자를
    반복 제시하는 것은 검증이 아니다.
- 관련: `docs/specs/DESIGN-NEEDS-EXPANSION-198.md` §4.5 ④, `scripts/verify_expansion_trigger_217.py`,
  이슈 #217·#222

## [2026-08-01] 실측 표는 앵커 목록까지 코드로 고정한다 — 문서와 스크립트가 다른 표본을 쓰면 숫자가 어긋난다
- 증상: 설계 문서 §4.5 ⑤ 에 "marker 에 걸리는 18건 / 매핑 실패 8 / 매핑 성공 10"을 적어 두고,
  재현 스크립트를 만들어 돌리니 **13 / 6 / 7** 이 나왔다. 코드도 판정식도 같은데 숫자만 달랐다.
- 원인: 문서 표는 34앵커 세트(임시 스크립트)로, 스크립트는 임계 스윕용 20앵커 세트로 쟀다.
  스윕은 normal/purpose **균형**이 결과를 좌우해 표본을 바꾸면 안 되고, marker 감사는 marker
  **커버리지**가 빠지면 셈이 틀린다 — 두 측정이 요구하는 표본이 애초에 달랐는데 목록 하나로
  돌려쓰려다 어긋났다.
- 규칙:
  - **문서에 실측 숫자를 적으면 그 숫자를 내는 스크립트를 같은 커밋에 넣는다.** 임시 스크립트로
    재고 문서에만 옮기면 재현이 불가능해지고, 나중에 임계를 바꿔도 표가 갱신되지 않는다.
  - **측정 목적이 다르면 앵커 목록을 분리하고, 왜 다른지를 코드 주석에 남긴다.** 공유하면 한쪽
    목적에 맞춰 목록을 고칠 때 다른 쪽 숫자가 조용히 바뀐다.
  - 스크립트는 **config 의 실제 임계를 읽어** 판정한다 — 값을 하드코딩하면 튜닝 후 표가 거짓이 된다.
- 관련: `scripts/verify_expansion_trigger_217.py`(`PURPOSE` vs `MARKER_AUDIT_EXTRA`),
  `docs/specs/DESIGN-NEEDS-EXPANSION-198.md` §4.5

## [2026-08-01] 전역 sweep 은 batch 를 키워 격리할 수 없다 — 페이지를 넘겨야 한다

- 증상: `-m integration` 의 `test_pg_session_context.py` 가 간헐 실패했다(#220). 깨지는 테스트는 매번 달랐지만 형태는 하나 — `claim_expired_contexts(10, 30, 100)` 결과에서 자기 세션을 못 찾아 `StopIteration` → `RuntimeError: coroutine raised StopIteration`. 실측 A/B(잔재 150건, 각 8회): 수정 전 **2/8 실패**, 수정 후 **8/8 통과**.
- 원인: 바로 아래 2026-07-31 항목의 **규칙 2("batch 를 넉넉히(100) 주고 필터링")를 정확히 따른 코드가 그대로 깨졌다.** `claim_expired_contexts` 는 테이블 **전역**을 `last_activity_at` 오름차순으로 batch 만큼 claim 하므로, 전역 만료 후보가 batch 를 넘으면 자기 행이 batch 밖으로 밀린다(실측 후보 133 > batch 100). batch 를 키우는 건 **상수를 잔재량보다 크게 유지하려는 경주**일 뿐이고, 잔재는 여러 worktree 가 같은 pg-profile(포트 5434)을 공유하며 **단조 증가**하므로 결국 진다. "격리를 batch 크기로 흉내낸다"는 발상 자체가 틀렸다.
- 규칙:
  1. **전역 스캔 API 에서 자기 행을 찾을 때는 batch 를 키우지 말고 페이지를 넘긴다.** claim 계열은 claim 된 행이 lease 동안 후보에서 빠지므로, 빈 페이지가 나오거나 자기 행을 만날 때까지 반복 호출하면 잔재량과 무관하게 결정적이다(`_claim_own`/`_claim_own_many`).
  2. **자기 행을 여러 개 찾아야 하면 단건 헬퍼를 반복 호출하지 않는다** — 첫 호출이 나머지도 claim 해 버려 두 번째부터 못 찾는다. 한 번의 페이지 순회로 함께 모은다.
  3. **"내 행이 결과에 없다"는 부정 단언은 페이지 끝까지 훑고 한다.** 안 그러면 자기 행이 batch 밖으로 밀려도 통과해 **거짓 음성**이 된다.
  4. 공유 DB 잔재는 **유휴 시간 기준으로만** 지운다(`it-*` + 1시간 초과). 접두만 보고 지우면 동시에 도는 다른 worktree 의 살아있는 행을 죽인다.
  5. 간헐 실패의 재현·검증은 **잔재를 심어 조건을 결정적으로 만든 뒤 A/B 반복 실행**으로 한다 — "여러 번 돌려보니 되더라"는 근거가 아니다.
- 관련: #220, `tests/integration/test_pg_session_context.py`(`_claim_own`·`_claim_own_many`·`_drain_claims`·`_delete_stale_residue`), `app/core/session_context.py::claim_expired_contexts`

---

## [2026-07-31] 개인화 신호는 "약하게 주입"이 아니라 "안 닿게" 설계한다 — 새면 멀티턴 저장소가 세션 전체로 증폭한다
- 증상: 취향 프로필이 있는 **회원의 추천이 게스트보다 부정확**했다(#119). 같은 발화를 3~4회
  반복하면 왜곡이 더 심해졌다. 개인화가 개선이 아니라 **순손실**이었다.
- 실측(라이브 decompose, gpt-5-nano fast tier, 발화 3종 × 3회 = **9턴 중 9턴 유출**):
  - 조건 없는 발화("무선 이어폰 추천해줘") — 게스트 `filters = {}` vs 회원
    `{price_min: 30000, price_max: 50000, brand: [소니, 젠하이저], rating_min: 4.5, color: 검정}`.
    프로필 구조화 블록이 **통째로 WHERE 절**이 됐다.
  - **명시 제약 오염**("10만원대 헤드폰") — 게스트 `{price_min: 100000, price_max: 999999}`(발화대로)
    vs 회원 `{price_min: 10000, price_max: 100000}`. 사용자가 **말한 가격대를 프로필이 덮었다** —
    후보 축소가 아니라 REQ-REC-043("명시 제약을 조용히 위반하지 않는다") 위반이다.
  - 무관 카테고리("노트북 추천해줘") — 회원만 `3~5만원·소니/젠하이저·검정`. 이어폰 취향이 노트북
    검색에 그대로 적용돼 사실상 0건 조건이 됐다.
  - 래칫 — 2턴("더 저렴한 걸로")에서 유출분(brand·rating_min·color·가격)이 전부 잔존했고,
    헤드폰 케이스는 `price_max` 가 100000 → 50000 으로 **프로필 쪽으로 더 수렴**했다.
- 원인: `profile_summary` 마크다운을 `decompose` 프롬프트에 **발화와 같은 격**으로 주입하면서
  사용 규칙을 한 줄도 주지 않았다(`_SYSTEM` 81줄에 `PROFILE_SUMMARY` 토큰 0회). decompose 는
  하드필터(WHERE 술어)를 산출하는 노드라 LLM 은 "3~5만원대 선호"를 `priceMax` 로 승격시켰고,
  그 필터가 `thread_store` 에 영속돼 다음 턴 `PRIOR_FILTERS` 로 재주입되며(프롬프트 규칙:
  "PRIOR_FILTERS 가 있으면 병합, 좁히면 add") **사용자가 명시적으로 모순되는 말을 하기 전까지
  세션 내내 후보를 좁혔다.** 게스트는 이 입력이 없어 손실이 0 → 비대칭.
  반복 발화 왜곡의 원인은 따로 있었다: 세션 버퍼가 발화를 무차별·중복 적재하고, 델타 추출 LLM 이
  그 버퍼를 `"\n".join` 으로 통째로 받아 **입력 중복을 보고 `repetitionEma` 를 자기 산출**한다
  (게이트: `explicit OR repeated` → 승격). 즉 **버퍼 중복이 곧 반복성 점수**였다.
- 규칙:
  - **개인화 같은 소프트 신호를 recall 을 줄이는 단계(WHERE 필터)에 넣지 않는다 — 순서(rerank)에만
    넣는다.** "후보를 줄이는 결정"과 "정렬하는 결정"은 실패 비용이 비대칭이다. 줄여서 사라진
    상품은 하류가 복구할 수 없다.
  - **LLM 산출을 영속시키는 경로(멀티턴 저장소)가 있으면, 입력 오염은 1회가 아니라 세션 전체
    비용이다.** 유출 확률을 낮추는 대책(프롬프트 규칙)은 이 곱셈 앞에서 무의미하다.
  - **프롬프트에 변수를 주입하면 사용 규칙을 같은 프롬프트에 반드시 명시한다.** 규칙 없는 주입은
    "쓰지 마라"가 아니라 "아무렇게나 써라"로 읽힌다. **규칙을 못 쓰겠으면 주입하지 않는다** —
    설득(프롬프트 지시)보다 입력 제거가 강제다. 지시 한 줄은 공짜가 아니다(#198/EX-7: 지시를
    얹었다가 기존 성공 케이스가 3/3 → 1/3 로 희석된 실측).
  - **회원 경로를 고칠 땐 게스트 프롬프트와 바이트 동일해지는 방향이 가장 안전하다** — 잘 도는
    경로를 변경 0으로 두면서, "회원 ≤ 게스트가 불가능하다"를 문자열 동등성 assert 로 결정론적으로
    증명할 수 있다(LLM 품질 측정 없이). 프롬프트 테스트는 ①부재/동등성 → ②구조/순서 →
    ③모듈 상수 참조 순으로 쓰고, 골든 프롬프트 전문 스냅샷은 만들지 않는다.
  - **반복 빈도·EMA 같은 누적 통계는 코드가 상한한다** — LLM 에게 "몇 번 반복됐나"를 물으면
    입력에 중복을 넣어준 만큼 그대로 답한다. 단 **상한이지 제거가 아니다**: 이번에도 처음엔
    완전 dedup(1건)으로 짰다가, 승격 게이트가 `explicit OR repeated` 라 **반복이 명시 표명
    없이 승격시키는 독립 경로**라는 걸 리뷰에서 지적받고 상한 2로 고쳤다. 세션 간 누적
    (GateState)이 미구현이라 다음 세션이 대신 살려주지도 않아, 1건으로 줄이면 그 경로가
    조용히 죽는다. **노이즈를 줄이는 변경을 넣기 전에 그 신호를 소비하는 쪽의 판정식을
    먼저 읽는다** — "과하다"의 반대는 "없앤다"가 아니다.
  - **개인화 강도는 연속 가중치가 아니라 주입 스코프(이산 스위치)로 조절한다** — ground truth
    (평가 골든셋)가 없으면 가중치는 튜너블이 아니라 마술 상수다. 코사인 임계 하나를 정하는 데도
    앵커 76개 실측이 필요했다(#115).
- 관련: `app/agents/buyer/graph.py`(프로필 주입 스코프·버퍼 적재 위치), `app/agents/profile/store.py`
  (`append_session_ctx` dedup), `app/agents/buyer/recommendation/rerank.py`(`_PROFILE_TIEBREAK`),
  `docs/specs/SPEC-PROFILE-001.md` v0.6.0 §5.1·REQ-PROF-011/014/026·OPEN-P12,
  `docs/specs/SPEC-RECOMMEND-001.md` v0.12.0 REQ-REC-005-A, #119

---

## [2026-07-31] 이벤트 루프보다 오래 사는 커넥션 풀 — 취소를 삼키는 워커가 teardown 을 영원히 멈춘다

- 증상: `uv run pytest -m integration` 이 **간헐적으로 무한 대기**했다(#208). 실패도 오류도 없이 매달리고, 테스트는 전부 통과한 상태였다. 재현율은 두 파일 조합에서 8회 중 1회. 메인 스레드는 언제나 `pytest_asyncio/plugin.py::_scoped_runner` → `Runner.close()` → `asyncio.runners._cancel_all_tasks()` → `run_until_complete(gather(...))` 안이었다.
- 원인: 두 겹이 겹쳤다.
  1. **상류** — psycopg_pool 의 async 빌드는 `CLIENT_EXCEPTIONS = (Exception, asyncio.CancelledError)` 이고(`pool_async.py`), `AsyncConnectionPool.worker()` 가 `await task.run()` 을 그걸로 감싼다. 그래서 유지보수 태스크를 **실행 중인** 워커를 취소하면 `CancelledError` 가 삼켜지고 워커는 `await q.get()` 으로 되돌아간다 — 불사 태스크가 된다. `_cancel_all_tasks()` 는 태스크 목록을 **한 번만** 취소하고 무기한 gather 로 기다리므로 그대로 교착한다. 워커가 큐에 park 중이면 정상 취소돼 죽는다 — 그래서 간헐적이었다.
  2. **우리 코드** — pg 모듈 6곳(`processed_events`·`session_activity`·`conversation`·`pg_store`·`profile/store`·`session_context`)이 sync 리셋터에서 await 할 수 없다는 이유로 풀 close 를 "다음 async 진입"으로 미룬다. 그래서 매 테스트가 **살아 있는 풀**(워커 3 + 스케줄러)을 곧 파괴될 루프에 남겼다. 창을 만든 건 우리고, 그 창을 교착으로 바꾼 건 상류다.
- 후속 재발(2026-08-13, #653): 새 `seller/analysis_store` 풀을 공통 close 목록과 teardown
  matrix에 등록하지 않은 채 `TestClient` portal에서 fire-and-forget 초기화했다. `pool.open()` 중
  portal이 취소되자 부분 생성된 worker가 남아 전체 pytest가 6%에서 15분 이상 멈췄다. 새 풀
  모듈은 기존 close 규약을 코드만 복사하는 것으로 끝나지 않고 **공통 수명주기 목록까지 함께
  확장해야 한다**.
- 규칙:
  1. **비동기 리소스는 자기를 만든 이벤트 루프 안에서 닫는다.** "다음 호출에서 정리"는 그 다음 호출이 *같은 루프*라는 보장이 있을 때만 성립한다 — pytest-asyncio(테스트마다 새 루프)·`TestClient`(자체 portal 루프)에서는 성립하지 않는다. sync 리셋터에 정리를 미뤘다면 **짝이 되는 async close 를 함께 만들고** teardown 훅에 배선한다.
  2. **간헐적 hang 은 타이밍 문제가 아니라 대개 "취소 불응 태스크" 문제다.** `faulthandler` 는 스레드만 덤프하니 asyncio 는 안 보인다. `asyncio.runners._cancel_all_tasks` 를 감싸 워치독 스레드로 `all_tasks()` + `task.cancelling()`/`_fut_waiter` 를 덤프하면 한 방에 나온다. **관측 코드가 타이밍을 바꾸면 재현이 사라진다** — `asyncio.wait(timeout=...)` 을 끼우자 15회 내내 통과했다. 진단 도구는 루프에 타이머를 추가하지 않는 형태로 만든다.
  3. `cancelling() > 0` 인데 `done() == False` 이고 `_fut_waiter` 가 **새 PENDING future** 면, 취소가 전달됐다가 삼켜지고 재대기에 들어갔다는 뜻이다. 라이브러리의 `except` 절이 `CancelledError` 를 포함하는지 먼저 grep 한다.
  4. 죽은 루프에 묶인 풀을 살아 있는 루프에서 닫으려 하면 실패하고 워커 코루틴만 미회수로 GC 돼 `PytestUnraisableExceptionWarning` 이 뜬다. **정리 훅은 "이 루프에 묶인 것"으로 범위를 좁힌다**(`asyncio.all_tasks()` 에 `pool-*` 태스크가 있는지로 판정).
  5. 새 `AsyncConnectionPool` 모듈의 완료 조건에는 `close_pool`, 공통 teardown 목록, loop teardown
     matrix, 초기화 취소 회귀 테스트가 모두 포함된다. 유닛 경로의 부수적 fire-and-forget 훅은
     no-op으로 주입해 로컬 PostgreSQL 상태와 분리한다.
- 관련: #208, #653, `tests/conftest.py::close_pg_pools_on_loop`,
  `tests/unit/test_pool_worker_cancellation.py`, `tests/unit/test_seller_analysis_store.py`,
  `tests/integration/test_pg_pool_loop_teardown.py`

## [2026-07-31] 계약에 필드가 있다고 필요한 건 아니다 — "누가 만드나" 전에 "왜 있나"를 묻는다

- 증상: I-22 `catalogVersion` 의 미해결 항목(C-18)을 **"값 생성 주체가 잘못됐다"** 로 읽고 Spring→AI 이관을 설계·구현·문서화·커밋까지 했다(지문 생성, Protocol 메서드 추가, 양쪽 구현체 수정, 테스트 5건, 명세 개정). 사용자가 *"이 필드가 왜 필요하냐"* 고 묻자 **명분이 하나도 안 남는다**는 게 드러나 전부 되돌렸다.
- 원인: 정본에 있는 필드라 **필요성을 전제**하고 시작했다. 정작 정본의 정의는 *"후보 산출 시점 식별자. 캐시 키와 재현에 쓴다"* 한 줄뿐이었는데, 그 두 명분을 검증하지 않았다. 검증했다면 바로 나왔다 — (1) **재현**: `products` 는 제자리 upsert 라 그 시점 임베딩이 없어 버전 라벨이 가리킬 대상이 사라진다. (2) **재현 불요**: 산출물은 Spring 이 `recommendation_generated` 로 이미 저장한다. (3) **캐시**: TTL 10분과 중복이고, `max(updated_at)` 지문은 상품 1건 갱신으로 전 회원 캐시를 동시에 날려 오히려 해롭다. 게다가 정본 예시 `catalog-20260728T0300Z` 는 타임스탬프라 **원 의도가 "Spring 야간 스냅샷"이었을 가능성**이 큰데, 그 해석 차이도 확인하지 않고 내 해석으로 채웠다.
- 규칙:
  1. **미해결(🔴) 항목을 구현할 때 항목의 문구를 그대로 문제 정의로 받지 않는다.** "주체 미정"은 이미 존재를 전제한 프레임이다. 먼저 **"이 필드가 없으면 무엇이 깨지나"** 를 구체적 시나리오로 답해본다. 답이 안 나오면 구현이 아니라 **폐기 제안**이 산출물이다.
  2. **정본이 한 줄로만 정의한 필드는 "정의되지 않았다"고 본다.** 값의 의미론(무엇의 버전인가·언제 바뀌나·누가 만드나)이 없으면 임의로 채우지 말고 질문 목록을 만들어 상대 팀에 넘긴다. 예시 값에 담긴 힌트(타임스탬프 형식 등)를 원 의도의 단서로 읽는다.
  3. **"저장한다"고 적힌 명분은 실제 저장 경로를 확인**한다. 재현·감사·추적을 근거로 필드를 만들 때, 그 값을 **누가 어디에 영속하는지** 명세에서 짚지 못하면 그 명분은 없는 것이다.
- 관련: #148, C-18, api-spec §3.7 구현 노트, 되돌린 커밋 `8399a35`

## [2026-07-31] "미검증"으로 넘긴 성능 항목은 실측하면 대체로 터진다 — 예산이 있는 경로는 데이터부터 채우고 재라

- 증상: I-22(#148)를 픽스처 테스트만으로 완성하고 성능은 "🟡 미검증"으로 문서에 적어 넘겼다. 실카탈로그 7,220건을 넣고 재보니 **두 군데가 동시에 예산을 넘겼다.** (1) 랭킹이 `store.all()` 전량을 파이썬 코사인으로 돌아 **p50 3,321ms** — 응답 예산 3s 를 랭킹 혼자 초과. (2) reason LLM 배치가 항목 수에 선형(20개 7,970ms · 12개 3,852ms · 6개 2,102ms)이라 5개로 줄여도 타임아웃 5/5, **reason 0건**.
- 원인: 유닛 테스트 픽스처는 상품 10건이라 두 비용이 **구조적으로 드러나지 않는다.** O(N) 스캔도 10건에선 0ms 고, LLM 지연은 가짜 LLM 이 즉시 반환한다. "계약은 고정했으니 성능은 나중에"가 성립하려면 예산이 없어야 하는데, I-22 는 계약 자체에 연결 2s/응답 3s 가 박혀 있어 **성능이 곧 계약**이었다.
- 규칙:
  1. **응답 예산이 계약에 명시된 경로는 픽스처 통과를 완료로 보지 않는다.** 실데이터 규모를 채우고 p50 을 재기 전까지 "구현 완료"가 아니다. 완료조건에 "응답 상한을 지킨다"가 있으면 특히 그렇다.
  2. **전량 스캔 + 파이썬 계산은 벡터 검색에서 기본값이 아니다.** pgvector 인덱스가 있으면 `ORDER BY <#>` 로 DB 에서 자른다. 인덱스 연산자 클래스(`vector_ip_ops`)와 질의 연산자가 **맞아야** 인덱스를 탄다 — `<=>` 로 쓰면 인덱스가 있어도 순차 스캔이다.
  3. **요청 경로의 LLM 호출은 항목 수에 선형이라고 가정하고 예산을 나눠본다.** 3s 예산에서 배치 1회로 N개 문장을 만드는 설계는 N 이 커지면 반드시 깨진다. 비싼 생성은 **배치로 옮겨 상품당 1회**로 만들고 요청 시점엔 고르기만 하는 쪽이 예산·결정성 양쪽에서 낫다.
  4. 성능 수정은 **수정 전/후 수치를 커밋 메시지와 문서에 남긴다** — 다음 사람이 "왜 SQL 로 밀었나"를 되돌리지 않게.
- 관련: #148, `app/pipelines/pg_artifact_store.py::top_k_by_vector`, `app/services/home_recommendation.py::build_reasons`, api-spec §3.7 구현 노트

## [2026-07-31] 시드 덤프가 채운 필드를 우리 배치가 재생산하지 못하면 신규 데이터만 조용히 빈다

- 증상: 홈 추천 reason 을 `extras.situation_tags` 에서 고르도록 설계했고 실측 커버리지가 **7,220/7,220(100%)** 이라 안전해 보였다. 그런데 그 값은 시드 덤프(`_sql/postgres`)를 만든 **외부 파이프라인**의 산출물이고, 이 저장소의 `enrich_product` 는 `{"tags", "attributes"}` 만 뽑는다. 즉 I-17 로 **새로 들어오는 상품만** reason 이 비게 되는 구조였다.
- 원인: "DB 에 있으니 우리가 만든 것"이라고 전제했다. 적재 경로가 둘(시드 덤프 / 운영 배치)인데 **산출 스키마가 다르다**는 것을 코드로 확인하지 않았다. 커버리지 100% 는 현재 스냅샷의 사실일 뿐 **재생산 가능성의 증거가 아니다.**
- 규칙:
  1. **시드 데이터의 필드를 코드가 소비하기로 하면, 그 필드를 우리 파이프라인이 만들 수 있는지 생성부 코드로 확인**한다. 커버리지 쿼리는 답이 아니다 — 오늘 100% 여도 내일 들어오는 행은 0% 일 수 있다.
  2. 소비를 시작한 필드는 **생성부에 "누가 왜 쓰는지" 주석을 남긴다.** 키를 지우거나 이름을 바꿀 때 소비처를 함께 보게 된다.
  3. 이 부류는 [2026-07-30 `extra="allow"` 은폐] 와 같은 실패다 — **빈 결과를 "데이터 없음"으로 읽지 말고 "경로 불일치"를 먼저 의심**한다.
- 관련: #148, `app/pipelines/enrichment.py::_ENRICH_SYSTEM`, `~/inte-final/_sql/postgres/10_product_document.sql`

## [2026-07-31] 순위를 단언하는 테스트에서 픽스처 벡터가 겹치면 tiebreak 이 의도를 덮는다

- 증상: I-22 랭킹 테스트 2건이 처음부터 실패했다. (1) "시그널 상품이 1등"을 단언했는데 카탈로그 픽스처의 `1001` 이 시그널 `9001` 과 **완전히 같은 벡터**라 코사인이 동점이었고, 결정적 tiebreak(`productId` 오름차순)이 `1001` 을 앞세웠다. (2) "cart 가중치가 조회보다 높다"를 "cart 상품이 1등"으로 단언했는데, 카탈로그의 다른 상품이 두 축의 **혼합 벡터에 더 가까워** 1등을 가져갔다 — 가중치는 정상 동작 중이었다.
- 원인: 순위는 **질의 벡터와 카탈로그 기하의 상호작용**인데 테스트가 "가중치가 크면 그 상품이 1등"이라는 단순 인과를 가정했다. 픽스처 벡터를 축 위에 촘촘히 깔면서 시그널 벡터와 겹치는지, 혼합 벡터 근처에 다른 후보가 있는지를 계산하지 않았다. 구현이 아니라 **테스트가 틀렸다** — 통과를 위해 tiebreak 을 없앴다면 결정성(완료조건)을 깨뜨릴 뻔했다.
- 규칙:
  1. **순위 단언 픽스처는 벡터가 겹치지 않게 띄운다.** 겹치면 동점 tiebreak 이 순서를 지배해 검증 의도가 사라진다.
  2. **가중치를 검증할 땐 절대 순위가 아니라 상대 순서를, 그것도 역할을 swap 해 두 방향으로 본다** — `A가 1등`이 아니라 `swap 시 순서가 뒤집힌다`. 카탈로그 기하가 아니라 가중치가 원인임을 이것만이 보인다.
  3. 테스트가 빨간불이면 **구현을 고치기 전에 픽스처 수치를 손으로 계산**한다. 랭킹·스코어링은 "그럴듯한 실패"가 구현 버그와 구별되지 않는다.
- 관련: #148, `tests/unit/test_home_recommendation.py`, `app/services/home_recommendation.py::rank_candidates`

## [2026-07-31] 5xx 봉투는 `detail` 을 무시한다 — 새 상태코드는 전역 맵에 등재해야 코드가 나간다

- 증상: I-22 의 `503 UPSTREAM_UNAVAILABLE`(§3.7 실패 응답표)을 `HTTPException(status_code=503, detail={"code": "UPSTREAM_UNAVAILABLE"})` 로 던졌는데 응답 코드가 `ERROR` 로 나갔다.
- 원인: `app/core/errors.py::_resolve` 는 **5xx 에서 `detail` 을 의도적으로 무시**한다(내부 오류 메시지·PII 유출 방지). 코드·메시지는 오직 `_STATUS_CODE_MAP`·`_DEFAULT_MESSAGE` 에서 나오는데 503 이 양쪽에 없었다(504 는 있었다). 4xx 습관대로 `detail` 에 코드를 실으면 조용히 무시된다.
- 규칙: **새 5xx 계약 코드는 `_STATUS_CODE_MAP`·`_DEFAULT_MESSAGE` 에 먼저 등재**한다. `detail` 로 코드를 넘기는 방식은 4xx 에서만 통한다. 등재 후 실제 응답 body 로 코드를 확인한다 — 핸들러가 조용히 덮어쓰므로 라우터 코드만 읽어선 알 수 없다.
- 관련: #148, `app/core/errors.py:33`, api-spec §3.7 실패 응답표

## [2026-07-30] 주입 seam 시그니처를 바꾸면 모든 fake 를 함께 고친다 — 방어 except 가 불일치를 삼켜 "조용한 degrade"가 된다
- 증상: `map_categories` 에 `llm`·`tier` 파라미터를 추가(#115 §4.4)한 뒤 유닛 테스트 20건이
  한꺼번에 실패했다. 실패 메시지는 `assert leg.category` → `None` — "카테고리가 안 붙는다"로만
  보이고 원인(시그니처 불일치)은 어디에도 안 나온다. 더 나쁜 사례가 먼저 있었다:
  `test_search_lookups_run_in_parallel` 의 fake search 가 계약(`list[tuple[str, float]]`)이 아니라
  `list[str]` 을 반환하고 있었는데, 그 테스트는 peak 동시성만 assert 해서 **매핑이 통째로 하드실패
  경로를 타는 상태로 통과**하고 있었다(거리 언패킹 ValueError → `category_embed_failed` → 빈 legs).
- 원인: 이 저장소는 주입형 seam(embed·search·exact·map_categories·select_category)과 **방어적
  `except Exception`** 을 많이 쓴다. fake 시그니처가 프로덕션과 어긋나면 `TypeError` 가 나지만,
  그 예외가 방어 except(`category_map_failed`·`category_embed_failed`)에 먹혀 **정상적인 degrade 처럼
  보이는 빈 결과**가 된다. 즉 "테스트 하네스 버그"가 "프로덕션 폴백 동작"으로 위장한다.
- 규칙:
  - **주입 seam 의 시그니처·반환 계약을 바꾸면 그 seam 의 fake 를 전수 검색해 함께 고친다.**
    `grep -rn "async def.*<주요 키워드 인자>" tests/` 로 찾는다(이번엔 conftest.py autouse fixture +
    test_fanout.py 8곳 + test_buyer_flow_e2e.py 1곳). 프로덕션 함수에 새 파라미터를 추가할 때는
    fake 쪽에 기본값(`llm=None`)을 주어 호출 호환을 유지한다.
  - **방어 except 로 감싼 경로의 테스트는 "예외가 안 났다"가 아니라 결과(출력)까지 assert 한다.**
    부수 지표(호출 횟수·동시성 peak)만 보는 테스트는 하네스가 망가져도 통과한다.
  - degrade 로그(`*_failed`)가 유닛 테스트 실행 중에 나오면 정상이 아니다 — caplog 로 확인한다.
- **[2026-07-31 갱신]** 같은 seam 에 `observer` 를 추가하며 같은 일을 하루에 **두 번째**로 겪었다
  (fake 15곳). 매번 전수 수정하는 대신 규칙을 바꾼다: **매퍼·전개기처럼 인자가 늘어나는 seam 의
  fake 는 `**_` 로 새 인자를 흡수**하고, **배선은 전용 테스트가 보증**한다
  (`test_mapper_receives_observer_...`·`test_expander_receives_observer_...`). `llm=None`·`tier`
  처럼 이미 있는 인자는 명시 파라미터로 남겨 바인딩 검증을 유지한다. 관용 fake 만 두고 배선
  테스트를 안 두면 "그래프가 인자를 안 넘겨도 아무도 모르는" 반대편 구멍이 생기므로 **둘은 짝**이다.
- 관련: `app/agents/buyer/recommendation/category_mapping.py`, `tests/conftest.py`
  (`_fake_category_mapping`), `tests/unit/test_fanout.py`, #115 커밋 9d9bf44·112d4b9

## [2026-07-30] 두 이질적 입력을 같은 "거리" 척도로 비교하기 전에, 그 척도가 의미를 반영하는지 실측한다
- 증상: #115 에서 임베딩 앵커를 "LLM 추측(raw) 우선"에서 "raw·query 둘 다 조회해 **거리가 더 가까운
  쪽**"으로 고쳤다. 유닛 테스트도 통과했고 4개 실측 케이스가 뒤집혀 개선으로 보였다. 그런데 라이브
  실측(실 LLM ×3회)에서 `anchor=raw` 로 채택된 12건 중 **11건이 오분류**였다 — 개선한 규칙이 오히려
  오분류를 만들고 있었다.
- 원인: 추상 라벨은 **카테고리명과 문자열이 겹쳐 가짜로 가깝게** 나온다. `'주방용품'` → `주방용품 >
  칼` 0.1387 은 의미 근접이 아니라 표기 겹침이고, 정작 뜻이 맞는 `'냄비 세트'` 는 0.1941 로 더 멀다.
  즉 이 구간의 코사인 거리는 의미가 아니라 표기를 반영하므로 **성격이 다른 두 앵커(창작 라벨 vs
  발화)를 같은 거리 척도로 비교하는 것 자체가 성립하지 않았다.** 1차 개정 문서에 "짧고 일반적인
  앵커가 유리한 편향"이라고 **한계를 적어두고도** 그 크기를 측정하지 않고 수용한 것이 실수다.
- 규칙:
  - **"둘 중 점수 좋은 쪽"류 규칙을 도입할 때, 두 입력이 같은 척도로 비교 가능한지 먼저 실측한다.**
    분포가 겹치거나 한쪽이 체계적으로 유리하면 비교를 포기하고 **신뢰도 높은 쪽을 규칙으로 고정**
    한다(여기서는 발화 유래 query 우선, raw 는 폴백).
  - 설계 문서에 "알려진 편향/한계"를 적었다면 **그 크기를 수치로 남긴다** — 적어두기만 하면 다음
    사람(=나)이 "수용 가능"으로 읽고 넘어간다.
  - 유닛 테스트 통과 ≠ 규칙이 옳음. fake 는 내가 정한 거리를 그대로 돌려주므로 **척도의 타당성은
    라이브 실측으로만 반증된다** — LLM·임베딩이 개입하는 규칙은 반드시 라이브로 재확인한다.
- 관련: `docs/specs/DESIGN-CATEGORY-HYBRID-59.md` §4.3.1,
  `app/agents/buyer/recommendation/category_mapping.py`, #115 커밋 6c415f2 → c6f4f8f(재개정)

## [2026-07-31] 무작위 UUID 가 개인정보 카나리 정규식에 걸려 트레이스가 통째로 버려진다 — 그리고 그 flake 를 내 변경 탓으로 오인했다

- 증상: #209 코드 전환 중 전체 스위트가 간헐적으로 1건 실패했다. 실패 테스트가 매번 달랐고(`test_all_buyer_spring_operations_trace_timeout...`, `test_buyer_spring_http_failure_...[503-5xx]`) 모두 `assert len(spring_payloads) == 1` → `0 == 1` 형태였다. 단독 실행은 항상 통과. 로그엔 `trace dropped code=TELEMETRY_REDACTION_FAILED` 가 찍혀 있었다.
- 원인: `app/core/tracing.py` 의 `_CANARY_PATTERNS` 중 **휴대폰 정규식** `(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)` 이 `dotted_order`(=`타임스탬프 + str(node.id)` 를 `.` 로 이어붙인 문자열)의 **무작위 UUID 조각**에 우연히 매치한다. 실제로 잡힌 값: `...bcd8-01976217625e` → `01`+`9`+`7621`+`7625`. UUID 는 16진수라 각 자리가 숫자일 확률이 10/16 이고, 32자리 hex 하나가 이 패턴들에 걸릴 확률을 20만 회 측정하니 **0.368%** 였다. 한 트레이스에 노드가 여럿이고 한 스위트에 트레이스가 여럿이라 실행마다 몇 %씩 터진다. 카나리는 fail-closed 라 걸리면 **트레이스 전체를 버린다** — 테스트뿐 아니라 **운영에서도 정상 트레이스가 조용히 사라진다**(관측 손실).
- 오진: 처음엔 내 변경이 원인이라고 봤다. 근거가 "베이스라인 8회 전부 통과 vs 변경분 12회 중 2회 실패"였는데, **10% 안팎의 flake 는 8회 연속 통과가 43% 확률로 그냥 일어난다.** 베이스라인을 별도 worktree 에서 더 돌리자 곧바로 같은 실패가 났다.
- 규칙:
  1. **flake 를 변경분 탓으로 돌리기 전에 베이스라인을 같은 횟수 이상 돌린다.** 5~10회 통과는 "이 변경이 원인"의 근거가 못 된다 — 실패율이 낮을수록 필요한 표본이 커진다.
  2. **원인은 추론이 아니라 계측으로 특정한다.** 여기서는 `UnsafeTelemetryError` 메시지에 문제 값을 실어 `-s` 로 뽑았고, 그러자 한 줄에 답이 있었다(dotted_order 문자열). 계측은 임시로 넣고 반드시 원복한다.
  3. **무작위 식별자를 개인정보 정규식으로 훑는 검사는 오탐을 전제로 설계한다** — 검사 대상을 사용자 유래 문자열로 한정하거나(자체 생성한 id·타임스탬프는 제외), 오탐 시 트레이스 전량 폐기 대신 해당 필드만 마스킹한다.
  4. **매번 다른 테스트가 깨지면 테스트가 아니라 공유 경로를 의심한다.** 실패 메시지(`IndexError`)는 제각각이어도 로그의 `trace dropped ... code=TELEMETRY_REDACTION_FAILED` 는 같았다 — 메시지가 아니라 **로그의 공통 줄**로 묶어야 한 지점이 나온다.
  5. 회귀 테스트를 오탐 표본 몇 개로만 고정하면 **그 표본만 피해가는 수정**도 통과한다. 클래스 전체가 닫혔는지 보려면 **고정 시드 corpus** 가 필요하다(`random.Random(208)` 로 만든 UUID·16-hex 지문 각 2만 개) — 시드를 고정해야 CI 가 흔들리지 않는다.
- 수정(#208 PR): 숫자열 카나리아(휴대폰·주민번호)를 **서버 생성 불투명 식별자 필드에서만 면제**했다(`dotted_order`·`requestId`·`sessionFp`·`threadFp`). 위 규칙 3 의 첫 번째 대안("검사 대상을 사용자 유래로 한정")을 택한 것이다. 두 번째 대안(필드 마스킹)은 쓰지 않았다 — 하필 `dotted_order` 가 LangSmith 의 **정렬 키**라 마스킹하면 트레이스 트리가 깨진다.
  - **초안은 정규식 경계를 hex 로 넓히는 방식이었고, 리뷰에서 철회했다.** 오탐은 사라지지만 `(?<![0-9a-fA-F])` 는 hex 로 끝나는 흔한 단어(`userid`·`face`·`cafe`) 뒤에 구분자 없이 붙은 **진짜 PII 까지 모든 문자열에서** 탐지를 피한다. 오탐(가용성)을 고치려다 탐지(보안)를 전역으로 깎은 것 — **fail-closed 검증기를 손볼 때는 "이 완화가 어디까지 적용되는가"를 먼저 답해야 한다.** 필드 한정은 완화 범위가 키 목록으로 눈에 보이지만, 패턴 완화는 범위가 보이지 않는다.
  - 규칙 3 이 지적한 **폭발 반경**(오탐 1건 → 트레이스 전량 폐기 → 운영 관측 손실)은 이 수정으로도 그대로다 — 별도 과제.
- 관련: `app/core/tracing.py` `_CANARY_PATTERNS`·`_build_export_payloads`(dotted_order), `tests/unit/test_tracing.py`(`test_opaque_identifiers_never_trip_the_pii_canary`), #141, #208 PR

## [2026-07-31] repo 밖에서 `gh` 를 부르면 실패하는데 `| tail -1` 이 그 오류를 가린다

- 증상: PR #211 본문 갱신을 `cd <스크래치패드> && python3 ...` 로 파일을 고친 뒤 이어서 `gh pr edit 211 --body-file ...` 로 실행하고 "갱신했다"고 보고했다. 로컬 파일은 정확히 바뀌었지만 **원격 PR 본문은 그대로**였다. 다음 턴에 `grep -c "v0.17.2"` 가 `0` 을 내서야 드러났다.
- 원인: 둘이 겹쳤다. (1) `cd` 로 옮긴 스크래치패드는 git repo 가 아니라 `gh` 가 대상 저장소를 정하지 못한다(`--repo` 없으면 실패). (2) 출력을 `2>&1 | tail -1` 로 잘라 오류 줄이 화면에서 밀려났고, 파이프라인 종료코드는 `tail` 의 `0` 이라 실패 신호도 없었다. **로컬 파일 수정이 성공한 사실**이 "됐다"는 착각을 굳혔다 — 한 명령 안에서 성공한 절반이 실패한 절반을 가린 것이다.
- 규칙:
  1. **`gh`·`git` 은 repo 안에서 부르거나 `--repo`/`-C` 로 대상을 명시한다.** 임시 파일을 만들려고 `cd` 하지 않는다 — 작업 디렉터리는 repo 에 두고 파일은 절대경로로 쓴다.
  2. **쓰기 명령의 출력을 `tail`/`head` 로 자르지 않는다.** 잘라야 하면 `echo "exit=$?"` 를 함께 찍는다 — 파이프 종료코드는 마지막 명령의 것이지 실패한 명령의 것이 아니다.
  3. 원격에 쓰는 작업(PR 본문·코멘트·문서)은 **쓴 뒤 다시 읽어 확인**한다. 같은 부류가 이 파일에 이미 있다(2026-07-30 Notion 편집) — "했다"와 "됐다"를 구분하지 않으면 반드시 드리프트가 생긴다.
- 관련: PR #211 본문 갱신, `docs/lessons.md` 2026-07-30 Notion 항목 규칙 2

## [2026-07-31] 안내 문구의 근거 값을 "확정 전" 상태로 계산하면 실제 결과와 어긋난다

- 증상: 유일 옵션 자동 선택(#114)으로 담기에 성공했을 때 "이미 담겨 있던 상품이라 수량을 더했어요" 안내가 나갈 수 있었다. Spring 은 새 줄로 담는 상황인데 사용자에게는 합쳐졌다고 말하는 것이다. PR #211 Claude 리뷰가 지적했다.
- 원인: 합산 안내의 근거인 `existing`(보유 수량)을 담기 **전**, `optionId` 가 아직 `None` 인 시점에 계산했다. `optionId` 미상이면 그 상품의 **모든 옵션**을 합산하는데, 자동 선택으로 옵션이 확정된 뒤에도 그 값을 그대로 썼다. "후보가 1개니 장바구니의 기존 항목도 그 옵션일 것"이라고 전제했지만, 단종·품절로 후보에서 빠진 **옛 옵션**으로 담아둔 항목이 남아 있으면 전제가 깨진다. 종전 되물음 흐름엔 이 경로가 없었다 — 성공 턴엔 항상 구체적 `optionId` 가 실려 계산이 정확했고, 자동 선택이 "성공했는데 근거는 미상 시점 계산"이라는 새 조합을 만들었다.
- 규칙:
  1. **안내 문구의 근거 값은 그 문구가 설명하는 대상이 확정된 뒤에 계산한다.** 확정 전 값을 재사용하려면 "무엇이 그때와 같음을 보장하는가"에 주석으로 답할 수 있어야 한다 — 답이 "보통 그럴 것"이면 그건 보장이 아니다.
  2. 조회 결과를 즉시 스칼라(합계 하나)로 접지 말고 **원본 목록을 들고 있다가** 확정 시점에 다시 센다 — 재조회(상류 왕복 추가) 없이 정확도를 얻는다.
  3. AI 가 만드는 것이 "안내"뿐이고 실행 권위가 BE 에 있으면(SPEC-CART-001 REQ-CART-031), **안내와 BE 실제 동작이 갈릴 조건**을 먼저 적어본다. 갈려도 조용해서(오류 아님) 테스트가 없으면 영영 안 드러난다.
- 관련: #114 / PR #211, `app/agents/buyer/cart/graph.py::_existing_quantity`, SPEC-CART-001 REQ-CART-026/031

## [2026-07-31] 전역을 훑는 sweep 결과를 `[x] = await ...` 로 언패킹하면 공유 DB에서 간헐 실패한다

- 증상: `-m integration` 137개가 실행할 때마다 결과가 달랐다. 통과(7초)·`2 failed`·무한 대기가 섞여 나왔고 재현율이 대략 절반이었다. 실패는 `test_pg_session_context.py` 의 `ValueError: too many values to unpack (expected 1)` 와 `RuntimeError: coroutine raised StopIteration` 두 종류였다.
- 원인: `claim_expired_contexts(idle, lease, batch)` 는 **테이블 전역**에서 만료 컨텍스트를 batch 만큼 claim 한다. 그런데 테스트 11곳이 `[idle] = await repo.claim_expired_contexts(10, 30, 10)` 처럼 **"돌아오는 건 내 것 하나뿐"** 을 전제로 언패킹했다. 각 테스트가 `prefix` 로 세션을 격리해도 **claim 질의는 prefix 를 모른다** — 앞선 테스트가 만료 컨텍스트를 남기면 2건이 돌아와 언패킹이 터진다. 실행 순서·타이밍에 따라 남는 잔여물이 달라져 간헐적으로 보였을 뿐, 논리적으로는 결정적 결함이다. `batch=1` 로 부른 6곳은 더 나빴다 — 남의 행 하나를 claim 해 와서 자기 것인 양 검증한다.
- 규칙:
  1. **전역 스캔 API의 결과는 절대 바로 언패킹하지 않는다.** 자기 fixture 가 만든 키(`session_id`·`context_id`)로 걸러낸 뒤 검증한다. 공용 헬퍼 `_claim_own(repo, session_id)` 을 쓴다.
  2. ~~전역 질의를 부를 때는 batch 를 넉넉히(100) 주고 필터링한다.~~ **[2026-08-01 폐기 — #220]** batch 를 키우는 것으로는 막을 수 없다. 전역 후보가 batch 를 넘으면 자기 행이 밀려 결과가 빈다(실측 133 > 100). **페이지를 넘겨야 한다** — 맨 위 2026-08-01 항목 참조. batch 를 1로 좁혀 격리를 흉내내면 남의 행을 집어오는 것도 여전히 사실이다.
  3. 통합 스위트는 **한 번 통과로 판단하지 않는다** — 최소 3회 반복 실행으로 순서 의존을 노출시킨 뒤 그린을 주장한다.
  4. 앞선 세션이 남긴 pytest 프로세스가 DB를 붙들고 있으면 같은 증상이 난다. 원인을 코드에서 찾기 전에 `pgrep -af bin/pytest` 로 유령부터 확인한다.
- 관련: #187, `tests/integration/test_pg_session_context.py` (`_own_claim`), `app/core/session_context.py::claim_expired_contexts`

## [2026-07-30] 죽어 있던 필드·함수를 배선하면 그 "미사용/폐기" 선언을 같은 커밋에서 지운다 — 하루에 3번 나왔다

- 증상: 실제로 쓰이기 시작한 코드에 "안 쓴다"는 선언이 남아 세 번 지적됐다. (1) `category_select.select_category` docstring 이 `"방식 B용 미사용 예비"` 인데 #115 가 마진 택일 경로에 배선했다. (2) `needs_expansion` 모듈 docstring 이 `"case 를 쓰지 않는다"` 인데 같은 파일이 `case != 3` 게이트로 쓴다. (3) `RouteDecision.case` 필드 주석이 `"[폐기, 이슈 #59] 미사용"` 인데 #198 게이트의 유일한 입력이다(PR #203 리뷰).
- 원인: 배선하는 커밋이 **호출부만 보고 정의부의 자기 서술을 갱신하지 않았다.** "미사용"은 단순 낡은 주석이 아니라 **삭제 허가증**으로 읽힌다 — #124 에서 실제로 미사용 코드를 정리한 전례가 있어, `case` 를 죽은 필드로 보고 지우면 case 2("5만원 이하 아무거나")의 무필터 의도 보호가 조용히 깨진다(테스트도 게이트 우회를 잡지 못한다).
- 규칙:
  1. 스텁·예비·폐기로 표시된 것을 배선하면 **같은 커밋에서 그 표시를 지우고 새 역할·근거 문서 §를 적는다.** 커밋 전에 새로 참조한 심볼을 `grep -n "미사용\|폐기\|예비\|NotImplementedError"` 로 훑는다.
  2. 존재 이유가 주석뿐인 필드는 **"제거하면 무엇이 깨지는지"를 주석에 쓴다** — 다음 정리 작업자가 판단할 근거가 된다.
- 관련: #115/#188, #198/#203, `recommendation/state.py:76`, `recommendation/category_select.py`, `recommendation/needs_expansion.py`, DESIGN-NEEDS-EXPANSION-198 §4.2

---

## [2026-07-30] 집계 0 ≠ 실제 0 — 이벤트 카운트는 "귀속 경로"부터 확인한다

- 증상: behavior 워커 검증(#196)에서 I-13 상품별 `purchaseComplete` 가 **항상 0** — 실구매·이벤트 적재 모두 존재하는데 "조회 많음·구매 전무" 가짜 패턴으로 이어질 상태였다. 추가로 상품별 rows 상한(5) < 시드 상품 수(7)라 하위 2종 수치가 매 호출 소실되고 있었다.
- 원인: purchase_complete 는 주문 단위 이벤트라 FE 가 productId 없이 발사(`properties.orderId`만) → `behavior_events.product_id = NULL` → I-13 의 product 조인 스코프에서 행이 통째로 탈락. **0 이 "행동 없음"이 아니라 "귀속 실패"였다.** 상한 문제는 "잘림 = 개수만 남기고 수치 소실" 구조라, 상한값을 올려도 상품 수가 더 많은 판매자에서 재발한다.
- 규칙:
  1. 집계 API 의 0/빈 값은 "데이터 없음"으로 읽기 전에 **원천 컬럼의 귀속 경로(NULL 허용 컬럼·조인 스코프)를 먼저 확인**한다 — 특히 다:1 이벤트(주문→상품)는 단일 FK 귀속이 구조적으로 불가능하다.
  2. 도구 요약의 상한 초과분은 개수가 아니라 **합계로 남긴다** — "외 N건"은 표본 누락이고 "외 N건 합계: …"는 요약이다. 상한값 조정은 구조 수정이 아니다.
  3. 교차 검증 지침(다른 도구로 확인)은 프롬프트에 적기 전에 **그 다른 도구가 실제로 다른 원천을 쓰는지 실측**한다(I-7 purchase 단 = order_item 기반이라 유효했음).
- 관련: #196, jarvis-backend#62, `app/agents/seller/tools.py`(`_BEHAVIOR_AUTHORITY_NOTE`·`_summarize_behavior`), `app/core/config.py`(`seller_summary_max_products`), api-spec §4.4 I-13 v0.17.4

---

## [2026-07-30] 응답 스키마를 추정으로 두려면 `extra="allow"` 가 불일치를 은폐함을 전제하라 — 미확정 계약은 "빈 결과"를 의심한다

- 증상: `sales_anomaly` 워커의 I-14(`get_order_events`)·I-15(`get_product_change_logs`) 도구가 **항상 0건**을 반환했다(#194). Spring 은 `rows`/`total`(+ stats 모드 `byStatus`/`cancelReasonsTop`)을 내려보내는데 AI 스키마는 추정 필드(`events`/`stats`, `logs`)를 기다렸고, `SellerAggregateModel` 의 `extra="allow"` 가 실측 필드를 여분 필드로 조용히 흡수해 **ValidationError 도 로그도 없이** 기본값 빈 목록이 됐다. 이상 감지 로직도 Spring(`withAnomaly`: 직전 최소 3점 판정·기준선 0+매출=이상·매출 0원=이상 아님)과 3개 규칙이 어긋나 있었다.
- 원인: 🔴 미확정 계약을 "파싱 실패로 도구가 죽지 않게" `extra="allow"` + 추정 필드명으로 선구현했는데, 그 방어 장치가 **계약 불일치의 탐지까지 막는다**는 것을 전제하지 않았다. 빈 결과는 "데이터 없음"과 구분되지 않아 E2E 검증 전까지 드러나지 않았다(I-7 FunnelResult 전 단계 0 건과 동일 패턴 — 같은 날 두 번째 발견).
- 규칙:
  1. `extra="allow"` 응답 모델을 둔 계약은 **BE 코드/실응답으로 필드명을 대조하기 전까지 "동작 미검증"으로 취급**한다 — 도구가 빈 결과를 주면 "데이터 없음"보다 "계약 불일치"를 먼저 의심한다.
  2. 미확정 계약이 확정되면(BE 실측 확인 포함) **스키마를 실측 필드로 고정하고 api-spec 에 응답 스키마를 등재**한다 — 추정 필드명을 명세 없이 오래 두지 않는다.
  3. AI 가 BE 로직을 재구현(재판정 등)할 때는 **BE 원본 코드의 경계 규칙**(최소 표본·0 나눗셈 가드·제외 조건)을 항목별로 대조한다 — 산식 하나가 아니라 가드 조건이 어긋나 판정이 뒤집힌다.
- 관련: #194, `app/schemas/spring.py`(OrderEventsResult·ProductChangeLogResult), `app/agents/seller/{tools,calc}.py`, jarvis-back `SellerOrderEventsResponse`·`SellerProductChangesResponse`·`SellerSalesService.withAnomaly`, api-spec §4.4 v0.16.1

---

## [2026-07-30] Notion 수정에 한글을 `\uXXXX` 이스케이프로 넣지 않는다 — 글자가 조용히 바뀐다

- 증상: Notion `update_content` 로 한글을 쓸 때마다 엉뚱한 글자가 섞였다. `불투명`→`불통명`, `막지`→`마지`, `주체`→`주잴`, `아래`→`아랰`, `혹시`→`혹신`, `우측`→`우상`, `워크스페이스`→`워킬스페이스`, `가리킨다`→`가리트다`, `뺀다`→`뻐다`. 게다가 이스케이프가 틀린 `old_str` 은 **매칭 자체가 실패**해 "적용됐다고 착각한 미적용 편집"이 CH-2 에 4건, I-20 에 1건 남았다.
- 원인: 한글을 `\uXXXX` 로 직접 조립하면서 인접 음절 코드포인트를 혼동했다(`투` U+D22C vs `통` U+D1B5). 사람 눈에는 그럴듯한 한글이라 **결과물을 봐도 틀린 줄 모른다** — 컴파일러도 테스트도 문서를 검증해주지 않는다.
- 규칙:
  1. Notion·문서 편집에서 **한글은 리터럴로 쓴다.** `\uXXXX` 이스케이프로 조립하지 않는다.
  2. 편집 후 **반드시 재조회해 눈으로 확인**한다. `update_content` 는 **매칭 실패를 오류로 올리지 않고 조용히 넘기는** 경우가 있어, 응답이 `{"page_id": ...}` 로 와도 적용됐다는 뜻이 아니다.
  3. `old_str` 은 **직전 조회 출력에서 그대로 복사**한다. 손으로 다시 타이핑하거나 이스케이프로 재구성하면 그 자체가 실패 원인이 된다.
- 관련: CH-2(`2e45ca79…`)·S-4(`de55ca79…`)·I-20(`5575ca79…`) 축 분리 개정. [[2026-07-30] 신선도 확인 명령의 실패를 리다이렉트로 삼키지 않는다] 와 같은 부류 — **"했다"와 "됐다"를 구분하지 않으면 반드시 드리프트가 생긴다.**

---

## [2026-07-30] 열거형 어휘의 개수는 손으로 세지 말고 항목 수를 그대로 적는다
- 증상: `screen.pageType` 어휘를 확장하면서 표에는 **14개**를 열거해놓고 본문에는 "13종"이라고 적었다.
  같은 개정에서 E-1 사본에는 구매자 값 **10개**를 나열하고 "구매자 9"라고 적었다. 이 틀린 숫자가
  api-spec 본문·개정 이력·Notion CH-2·Notion E-1·커밋 메시지 **다섯 곳에 그대로 퍼졌고**,
  사용자가 "구매자쪽에서는 왜 13개야?"라고 묻고서야 드러났다.
- 원인: "기존 8종 + 몇 개 추가"를 머리로 더했다. 실제로는 구매자 2종(`chat`·`auth`)과 판매자 4종을
  더해 14종인데 8+4만 세고 구매자 추가분을 빼먹었다. **열거와 개수를 따로 만들어서** 둘이 어긋났고,
  여러 문서에 같은 숫자를 복사하는 순간 "어느 쪽이 맞나"를 나중에 판정할 수 없게 됐다.
- 규칙:
  - **개수를 쓸 때는 표/목록의 항목을 실제로 세어 확인한다.** 검산이 필요하면
    `grep -c "^| "` 처럼 세는 명령을 한 번 돌린다 — 머릿셈을 근거로 숫자를 문서에 넣지 않는다.
  - **더 나은 방법은 개수를 아예 안 적는 것이다.** "14종" 대신 "아래 표"로 참조하면 항목이 늘 때
    숫자를 같이 고쳐야 하는 부채가 생기지 않는다. 개수를 적어야 한다면 **한 곳(정본)에만** 적고
    나머지는 정본을 가리킨다.
  - 정본·사본·Notion 여러 곳에 같은 값을 퍼뜨리기 전에, **그 값이 파생값인지 확인한다**
    (파생값이면 적지 않는다 — `sum`·`withinBudget`·`rows` 를 계약에서 뺀 것과 같은 원칙).
- 관련: `docs/api-spec.md` §3.1 `pageType` 어휘표, 커밋 `411ee79`(오류) → `46a1c68`(정정)

## [2026-07-30] 신선도 확인 명령의 실패를 리다이렉트로 삼키지 않는다
- 증상: [2026-07-27] 항목의 규칙("코드 실측 전 `git fetch`")을 적어두고도 **같은 실수를 반복**했다.
  P-7 폐지 여부를 확인하며 `cd jarvis-backend && git fetch -q origin 2>/dev/null; grep ...` 로
  한 줄에 묶었는데, `git fetch` 가 실패했는데도 `2>/dev/null` 이 오류를 삼켜 그대로 grep 이 돌았다.
  워킹트리가 `origin/main` 보다 **18커밋 뒤처진** 상태였고, 이미 제거된 `@GetMapping("/cards")` 를
  "아직 살아 있다"고 보고했다.
- 원인: 규칙은 "fetch 를 돌린다"까지였고 **"fetch 가 성공했는지 본다"가 빠져 있었다.**
  `2>/dev/null` 과 `;`(`&&` 아님)의 조합이 실패를 조용히 통과시켰다.
- 규칙:
  - **신선도 확인은 별도 명령으로 돌리고 출력을 본다.** 진단 명령과 한 줄에 묶지 않는다.
  - `git fetch` 의 stderr 를 버리지 않는다. 묶어야 하면 `;` 가 아니라 `&&` 로 잇는다.
  - fetch 후 **`git rev-list --count HEAD..@{u}` 를 눈으로 확인**한다 — 0이 아니면 진단을 멈추고
    최신 ref(`origin/<branch>:path`)를 근거로 다시 본다.
- 관련: `docs/lessons.md` [2026-07-27] 항목의 후속 — 규칙에 "검증"이 빠지면 규칙이 안 지켜진다

---

## [2026-07-29] provider 튜너블은 "그 모델이 그 조합을 받아주는지"까지 확인하고 주입한다
- 증상: 판매자 채팅(`POST /seller/chat`)이 첫 요청부터 전량 400 으로 죽었다. supervisor 라우팅이
  터져 general 폴백으로 내려갔는데 general 스트림도 같은 400 이라 토큰이 한 개도 안 나갔다.
  `Function tools with reasoning_effort are not supported for gpt-5.6-luna in /v1/chat/completions.`
- 원인: `resolve_provider_model` 이 provider 가 openai 이기만 하면 tier 별 `reasoning_effort` 를
  **모델이 그 조합을 지원하는지 보지 않고** 항상 실어 보냈다. `gpt-5-nano`(fast)는 tools + `minimal`
  조합을 받아주지만 `gpt-5.6-luna`(smart)는 거부한다. #177 이 판매자 전 역할을 `smart` 로 올리면서
  supervisor 까지 luna 로 옮겨가 즉시 전면 장애가 됐다. 더 나쁜 신호는 **`recommend` 가 그 이전부터
  같은 조건이었다**는 것 — 분석 파이프라인 끝단이라 아무도 거기까지 도달하지 않아 계속 숨어 있었다.
- 규칙:
  - **모델 ID 를 바꾸거나 역할의 tier 를 옮길 때, 그 경로가 function tools 를 싣는지 먼저 확인하고
    tool 을 싣는 조합으로 스모크 1회를 돌린다.** 모델 교체는 "문자열 하나 바꾸기"가 아니라 capability
    교체다.
  - **`create_agent(tools=[])` 는 "tool 을 안 쓴다"는 뜻이 아니다** — `response_format=ToolStrategy(...)`
    가 구조화 출력을 function tool 로 내보낸다. tool 유무를 판단할 때 `tools=` 인자만 보지 말 것.
  - provider 튜너블(`reasoning_effort` 등)은 **모델 기준 게이팅을 거쳐** 주입한다. 미지원 목록을 config
    로 두고(접두사 매칭 — 날짜 스냅샷 ID 포함), 지원 모델로 갈아타면 목록에서 빼는 것으로 원복되게 한다.
  - **폴백이 원인을 삼키지 않게 한다.** supervisor 폴백은 400 을 `confidence=0.00 general` 로 흘려보내
    원인이 로그 최상단 WARNING 에만 남았고, 관측성 레코드에는 `model: null` 로 찍혀 어느 모델이 문제인지
    대시보드에서 식별할 수 없었다. degrade 경로는 **무엇으로부터 degrade 했는지**를 남겨야 한다.
  - **파이프라인 끝단에서만 도달하는 경로는 통합 스모크가 없으면 조용히 썩는다** — `recommend` 가 그랬다.
    새 모델/새 조합을 도입하면 hot path 만이 아니라 **끝단 역할도 1개씩** 최소 1회 태워본다.
- 관련: #178 · #177(aa0a2b6) · `app/core/llm.py` `resolve_provider_model`/`supports_tool_reasoning` ·
  `app/agents/seller/models.py` · `app/agents/seller/workers.py` · `app/core/config.py`

## [2026-07-27] 코드를 근거로 진단하기 전에 `git fetch` 로 워킹트리 신선도부터 확인한다
- 증상: 계약 대조 중 "구매자가 평점 조건을 주면 추천 결과가 0건이 된다"를 **P0 버그로 보고**했다.
  근거는 `search_service.py` 의 `(p.rating or 0.0) >= threshold` 와 Spring `ProductCandidateResponse`
  에 `rating` 이 없다는 것이었고, 호출 체인까지 추적해 확신했다. 그런데 워킹트리가 `origin/dev`
  보다 **21커밋 뒤처져** 있었다. `git fetch` 후 보니 이미 `p.rating is None or p.rating >= threshold`
  ("반증된 것만 제거")로 고쳐져 있었고, Spring 도 같은 날 `price`·`rating`·`reviewCount` 를
  반환하도록 바뀌어 있었다(jarvis-backend 도 4커밋 뒤처져 있었다). 전부 #100/PR #127 로 이미 해소된
  내용을 "새로 발견한 버그"로 보고한 것이다.
- 원인: `git status` 가 깨끗해 보여 최신이라고 단정했다. **`git status` 는 원격과의 격차를 말해주지
  않는다** — 마지막 `fetch` 시점 기준의 ref 와 비교할 뿐이라, fetch 를 안 하면 며칠 묵은 트리도
  "clean" 으로 보고된다. 이슈가 CLOSED 인 것을 확인하고도 "코드에 남아 있으니 안 고쳐진 것"이라고
  거꾸로 추론했다(실제로는 "내 트리에만 안 들어온 것").
- 규칙:
  - **코드 실측을 근거로 결함을 주장하기 전에 `git fetch` 를 먼저 돌리고 `git log HEAD..@{u} --oneline`
    으로 뒤처짐을 확인한다.** 여러 저장소를 대조할 때는 **모든 저장소에** 적용한다(여기선 jarvis-ai·
    jarvis-backend 둘 다 뒤처져 있었다).
  - **CLOSED 이슈의 수정이 코드에 안 보이면, 결론을 내리기 전에 그 이슈를 닫은 PR·커밋이 현재 브랜치에
    있는지 확인한다** — `git branch -a --contains <sha>` / `gh pr view <n> --json mergedAt,baseRefName`.
    "닫혔는데 코드에 없다"의 첫 번째 가설은 "내 트리가 낡았다"여야 한다.
  - 오래된 트리에서 읽은 파일로 진단을 쌓았다면 **결론만 고치지 말고 근거 전체를 최신 ref 로 재검증**한다
    (`git show origin/dev:<path>` 로 대조).
- 관련: #100 · PR #127 · `app/services/search_service.py` · `jarvis-backend` `ProductCandidateResponse.java`

## [2026-07-27] 구현 없이 이슈를 닫으면 "미착수"가 조용히 "완료"로 뒤집힌다
- 증상: 정본(Notion "📡 API 명세서" DB) 75행을 코드와 1:1 대조하다 **추적 주체가 없는 미구현 3건**을
  찾았다. (1) `budget` SSE 이벤트 — 명세 §3.1 이 페이로드·정상 흐름까지 규정하는데 `app/` 에
  `budget`·`knapsack`·`verifiedSum`·`price_scope` 가 **0건**(나머지 구매자 SSE 7종은 전부 구현).
  (2) 주문상태 문의(I-4) — 명세는 v0.15.2 에서 "CH-2 에 흡수" 라고 **확정 선언**했고 Spring 은 구현
  완료·대기 중인데, AI intent 는 4종뿐이라 분기가 없고 이슈 87건·mvp-todo 어디에도 언급이 없었다.
  (3) #91(분석 기준서 RAG) — `COMPLETED` 로 닫혔으나 참조 커밋이 없고 `search_analysis_guide` 는
  여전히 `NotImplementedError` 스텁이었다.
- 원인: 완료 판정의 근거가 제각각이었다. 명세는 "흡수했다"는 **선언**을, 이슈는 **수동 종료**를,
  #60 은 존재하지 않는 baseline("현재는 최적 조합 1개를 노출")을 각각 완료 근거로 삼았다. 셋 다
  코드 확인을 거치지 않았고, mvp-todo 에는 애초에 항목이 없어 체크박스 점검으로도 안 잡혔다.
  [2026-07-27 다른 항목]은 "완료를 미착수로 오판"이었는데 이번은 **정확히 반대 방향**이다.
- 규칙:
  - **이슈를 `COMPLETED` 로 닫을 땐 PR·커밋이 연결돼 있어야 한다.** 참조 커밋 없이 손으로 닫지 않는다.
    닫기 전에 "이 이슈의 완료 조건을 만족시킨 코드가 `dev` 에 있는가"를 파일 단위로 확인한다.
  - **명세에 "흡수/통합/이관했다"고 쓸 때는 그 코드 경로를 같은 PR 에서 만들거나, 없으면 이슈를
    함께 등록한다.** 선언만 남기면 문서상 완료·실제 미착수가 되어 아무도 안 본다.
  - **확장/2단계 이슈를 쓸 때 baseline 존재를 코드로 확인하고 본문에 근거를 적는다**(#60 이 없는
    baseline 을 전제했다). 없으면 기반 이슈를 먼저 분리한다.
  - **계약 정본과 코드의 정기 대조를 mvp-todo 체크박스 점검과 별도로 돌린다** — 체크리스트에 항목이
    없는 미구현은 체크박스 점검으로 절대 안 잡힌다.
- 관련: #163(budget SSE) · #164(I-4 주문상태) · #91(재오픈) · #60(전제 정정 코멘트) ·
  `docs/api-spec.md` §3.1/§5 Q6 · `docs/mvp-todo.md`

## [2026-07-27] 체크리스트·명세를 갱신하지 않으면 "이미 끝난 일"을 미착수로 오판한다
- 증상: `docs/mvp-todo.md`를 근거로 남은 작업을 추리려다, 미체크(`[ ]`/`[~]`) 55개 중 **52개가
  이미 구현·테스트 완료**임을 코드 대조로 확인했다. §0 공통 인프라(SSE 동시 스트림 제한·요청
  취소·타임아웃·레이트 리밋·오류 봉투)는 5개 전부 `[ ]`인데 실제로는 `core/stream.py`·
  `ratelimit.py`·`errors.py`에 구현 + `tests/unit/test_infra.py` 13개 테스트까지 있었다.
  같은 drift가 `docs/api-spec.md` C-1에도 있어, "CH-1b 티켓 재발급 경로 신설 필요(🔴)"로 적힌
  항목이 백엔드에는 이미 `POST /api/chat/tickets`로 구현돼 있었다 — 명세만 믿고 "실서비스
  블로커"라고 잘못 진단할 뻔했다.
- 원인: CHANGELOG 갱신은 CLAUDE.md 하네스로 강제되는데 **mvp-todo 체크박스·api-spec 🔴 해소
  표시에는 같은 강제가 없었다.** 구현 PR이 병합돼도 체크리스트는 그대로 남아 문서가 "미착수"를
  계속 주장한다. 코드 주석도 같은 문제 — `app/api/seller.py`는 "스트림 수명주기는 구현 TODO"라고
  써놓고 같은 파일에서 `open_stream()`을 호출하고 있었다.
- 규칙:
  - **기능 PR에 `docs/mvp-todo.md` 체크박스 갱신을 포함한다** — CHANGELOG와 동일 취급.
    주제 완료 = 코드 + 테스트 + CHANGELOG + 체크박스.
  - **스텁을 구현으로 바꿀 때 그 자리의 TODO 주석·docstring도 같이 지운다.** "스텁은
    `NotImplementedError` + § 참조 주석 유지" 규칙의 짝이다 — 구현되면 근거 주석도 현재형으로.
  - **명세의 🔴(협의 대기)를 근거로 판단하기 전에 상대 repo 코드를 먼저 확인한다.** BE가 먼저
    구현하고 명세 역반영이 늦는 경우가 실재한다. "명세에 없다 = 구현 안 됐다"가 아니다.
  - 문서와 코드가 어긋나면 **코드를 정본으로 보고 문서를 고친다**(계약 자체를 바꾸는 경우는
    예외 — 그때는 명세 개정이 먼저).
- 관련: `docs/mvp-todo.md`(52개 일괄 정정) · `docs/api-spec.md` C-1 · 이슈 #122(대조로 발견된
  유일한 미구현) · CLAUDE.md "변경 기록" 절

## [2026-07-27] `git stash pop` 충돌 마커가 해소되지 않은 채 dev에 커밋됐다
- 증상: `docs/lessons.md` HEAD 버전에 `<<<<<<< Updated upstream` / `=======` /
  `>>>>>>> Stashed changes` 마커가 그대로 들어 있었다. `git status`는 깨끗하다고 보고해
  (이미 커밋된 상태라) 작업 중에는 드러나지 않았고, 같은 파일을 편집하려다 발견했다.
  두 lesson 항목(back-merge · 신원 타입)이 뒤엉켜 한쪽이 사실상 읽히지 않는 상태였다.
- 원인: stash pop 충돌을 해소하지 않고 `git add` → 커밋했다. 마크다운은 문법 검사가 없어
  ruff·pytest 어느 것도 잡지 못하고, CI에도 충돌 마커 검사가 없었다.
- 규칙:
  - **stash pop·rebase·merge 충돌 후에는 커밋 전에 `git diff --cached`로 마커 잔존을 눈으로
    확인한다.** 특히 문서/마크다운은 도구가 안 잡아준다.
  - 커밋 워크플로 1단계 "diff 전체 검토"에 **`grep -rn '^<<<<<<< \|^>>>>>>> '` 확인을 포함**한다.
  - pre-commit hook 또는 CI에 충돌 마커 검사 추가를 검토한다(현재 `ruff`만 돌아 사각지대).
- 관련: 커밋 `c6e9919`(마커 유입), `docs/lessons.md`, `.pre-commit-config.yaml`

## [2026-07-24] 신원(id) 타입은 auth 경계에서 정규화하고 전 계층 한 타입으로 통일한다
- 증상: 운영 로그에 `PydanticSerializationUnexpectedValue(Expected 'str' ... input_value=1, input_type=int)`
  경고(field=brand_id). JWT `brandId` 클레임이 숫자(1)로 발급되는데 `SellerContext`/`DraftRecord`/
  `spring_client` 는 `str` 로 선언돼 있었고, `Identity`·`SellerContext` 가 plain dataclass 라
  타입 힌트와 다른 값이 검증 없이 통과해 직렬화 시점에야 드러났다. Pydantic 모델(DraftRecord)에
  닿으면 경고가 아니라 ValidationError 로 터진다.
- 원인: 신원 타입 계약(§2.6 숫자)이 계층마다 제각각(str/int)이었고, 토큰 클레임의 실제 와이어
  타입(숫자)을 어느 경계에서도 정규화하지 않았다.
- 규칙: 신원 id 는 **auth/API 경계에서 한 번 캐스팅**(seller.py `_seller_context`)하고,
  SellerContext·DraftRecord·spring_client·history 등 내부 전 계층은 **int 한 타입**으로 선언한다.
  dataclass 는 타입을 검증하지 않으므로 힌트만 믿지 말고 경계 캐스팅을 명시한다.
- 관련: app/api/seller.py `_seller_context`, app/agents/seller/{context,hitl,history}.py,
  app/services/spring_client.py, api-spec §2.6

## [2026-07-24] dev→main 승격 후 main→dev back-merge를 안 하면 다음 승격에서 "out of date"로 막힌다
- 증상: 새 dev→main 승격 PR(#106)에 "This branch is out-of-date with the base branch"가 뜨고, GitHub "Update branch" 클릭 시 "Repository rule violations found — Changes must be made through a pull request"로 거부됐다. main·dev 실제 파일 차이는 승격 대상 1건뿐이었는데도 막혔다.
- 원인: 승격은 merge commit으로 하는데(#104), 그 후 main→dev back-merge를 하지 않아 직전 승격 merge commit이 dev 히스토리에 없어 그래프가 갈라졌다. main의 "up-to-date 필수" 보호 규칙이 이 drift를 잡아 머지를 막았고, dev도 보호 브랜치라 update-branch 직접 push조차 PR 없이는 불가였다.
- 규칙:
  - **dev→main 승격 PR을 머지한 직후, main→dev back-merge PR을 만들어 머지한다**(고정 단계). back-merge는 **merge commit**으로(squash 금지) 그래프를 재동기화한다 — 보통 파일 변경 0건이라 CI만 통과하면 된다.
  - dev·main 둘 다 보호 브랜치라 재동기화도 반드시 PR 경유. "Update branch" 버튼/`update-branch` API는 rule violation으로 막히니 처음부터 PR로 간다.
  - 완료 확인은 `git log origin/dev..origin/main`이 비어 있는지로 한다(비면 dev가 main을 포함).
- 관련: PR #105→#106(승격)→#107(back-merge), CLAUDE.md Git 절

## [2026-07-23] 진단 스크립트도 실제 응답 모델 계약으로 성공 경로를 테스트한다
- 증상: FastAPI→Spring 연결과 internal token 인증은 성공했지만, 연결 확인 스크립트가
  `SellerProductList`에 없는 `total` 속성을 출력하려다 `AttributeError`로 종료되어 실제 연결
  성공을 실패처럼 보이게 했다.
- 원인: 목록 응답에 관습적으로 `total`이 있을 것이라 가정하고 `rows`만 정의된 실제 Pydantic
  모델을 확인하지 않았으며, 실패 경로만 수동 확인하고 성공 응답 출력은 테스트하지 않았다.
- 규칙: 진단 도구도 운영 클라이언트의 실제 응답 모델을 사용해 성공·빈 결과 경로를 테스트하고,
  출력 필드는 스키마에 선언된 속성만 참조한다.
- 관련: `scripts/check_spring_connection.py`, `app/schemas/spring.py::SellerProductList`,
  `tests/unit/test_check_spring_connection_script.py`

## [2026-07-23] Docker 이미지가 오래 빌드 불가였는데 CI가 못 잡았다
- 증상: 배포 준비(#95)로 처음 `docker build` 를 돌리자 두 지점에서 실패 — (1) `uv sync --group embedding` 이 "Group embedding is not defined"(§4.8 v0.15.14에서 폐기), (2) 이어서 hatchling wheel 빌드가 `pyproject.readme`(README.md)를 못 찾음(Dockerfile이 COPY 안 함). 즉 이미지는 한동안 빌드 불가 상태로 방치돼 있었다.
- 원인: CI(`ci.yml`)가 `ruff`+`pytest` 만 돌고 **`docker build` 스모크가 없어**, Dockerfile 결함이 커밋돼도 아무도 못 봤다. 임베딩 그룹 폐기 시 Dockerfile·문서의 잔존 참조를 함께 지우지 않은 것도 겹쳤다.
- 규칙: **배포/의존성/Dockerfile 변경 시 로컬 `docker build` + 이미지 내 `create_app()` 스모크를 반드시 돌린다.** CI에 이미지 빌드 잡 추가를 검토한다. 의존성 그룹·명령을 폐기하면 `grep -rn` 으로 Dockerfile·README·CLAUDE·docs 잔존 참조를 전수 정리한다.
- 관련: #95, PR #96, Dockerfile, api-spec §4.8 v0.15.14

## [2026-07-23] 여러 파일 patch는 파일 경계마다 Update File 헤더를 다시 선언한다
- 증상: SPEC과 구현 계획을 한 patch로 갱신하면서 두 번째 파일의 체크박스 변경 전에 `Update File` 헤더를 빠뜨려 첫 파일에서 해당 문맥을 찾다가 patch 전체가 실패했다.
- 원인: 서로 다른 문서의 hunk를 하나의 파일 블록으로 이어 붙였다.
- 규칙: `apply_patch`로 여러 파일을 수정할 때 각 파일마다 독립적인 `*** Update File:` 헤더를 두고, 실행 전 hunk 문맥이 해당 파일에 실제 존재하는지 확인한다.
- 관련: PR #88 두 번째 Claude Review SPEC/계획 갱신

## [2026-07-23] Python 도구 실행은 저장소의 uv 환경을 통해 호출한다
- 증상: PR 리뷰 스레드 조회 스크립트를 시스템 `python`으로 실행하려다 PATH에 해당 명령이 없어 즉시 실패했다.
- 원인: 이 저장소가 `uv run`으로 Python 실행 환경을 고정한다는 명령 규약을 외부 플러그인 스크립트에도 동일하게 적용하지 않았다.
- 규칙: 저장소 작업 중 Python 스크립트는 경로가 외부 플러그인에 있더라도 `uv run python <script>`로 실행한다. 시스템 `python`/`python3` 존재 여부를 가정하지 않는다.
- 관련: PR #88 Claude Review 스레드 조회

## [2026-07-23] bounded suffix scan 밖으로 밀린 시크릿 prefix는 partial token이 붙은 뒤에도 별도로 추적해야 한다
- 증상: `Bearer` 뒤 연속 newline이 보류 상한을 정확히 채운 다음 첫 token 문자가 도착하면, suffix scan은 prefix 시작점을 놓치고 overlong 판정은 `rest.isspace()`가 깨져 전체 후보를 평문으로 방출했다.
- 원인: scan window 경계와 overlong fallback을 독립적으로 설계하면서, whitespace-only 상태에서 partial token 상태로 전이되는 한 지점을 두 조건 모두가 놓쳤다.
- 규칙:
  - bounded prefix guard는 임계값 직전·정확히 일치·직후에 다음 상태 문자가 붙는 전이를 각각 테스트한다.
  - overlong Bearer 판정은 선행 whitespace 뒤의 token이 최소 길이 미만인 동안에도 후보로 유지하고, 명백한 delimiter가 나타날 때만 안전 텍스트로 해제한다.
- 관련: `app/agents/seller/middleware.py::_overlong_bearer_prefix_start`, `tests/unit/test_seller_middleware.py`, PR #87 리뷰

## [2026-07-23] 스트림에서 고정 길이 시크릿을 즉시 마스킹해도 다음 청크의 결합 문자를 먼저 흡수해야 한다
- 증상: 주민번호 마지막 숫자까지 도착한 청크에서 marker를 즉시 내보낸 뒤, 다음 청크에 그 숫자의 등록 Variation Selector가 오면 selector 하나가 marker 뒤에 남아 full-string 정제 결과와 달라졌다.
- 원인: 가변 길이 API/Bearer token만 후속 continuation 상태로 전환했고, 고정 길이 주민번호는 매치 순간 완결됐다고 간주했다. Unicode 결합 문맥은 visible 패턴의 끝보다 한 청크 늦게 확정될 수 있다.
- 규칙:
  - 스트림 매치가 현재 skeleton 끝에서 끝나면 패턴 길이와 무관하게 후속 skeleton-empty invisibles를 첫 visible delimiter 전까지 흡수한다.
  - full-string sanitizer+mask 결과와 stream guard 결과를 여러 청크 분할로 비교하는 differential 검증을 수행한다.
- 관련: `app/agents/seller/middleware.py::StreamingOutputGuard`, `tests/unit/test_seller_middleware.py`, 이슈 #72

## [2026-07-23] 관측용 모델명 조회가 SDK 자격증명 검증에 결합되면 fake 주입 테스트가 깨진다
- 증상: Issue #82 전체 테스트에서 주입형 `ScriptedLLM`을 쓰는 구매자 경로도 telemetry 모델명을 기록하려다 활성 provider API key 누락 예외를 발생시켜 34개 테스트가 실패했다.
- 원인: 부수효과 없는 모델 ID 선택과 실제 SDK 생성 전에 필요한 자격증명 검증을 하나의 strict resolver로 합친 뒤, 관측 코드가 그 strict 경로를 재사용했다.
- 규칙: 설정 해석은 `provider+tier → model ID` 순수 함수와 `model ID+API key → SDK 설정` 검증 함수로 분리한다. fake/injected 실행의 관측은 전자만 호출하고, 실제 provider client 생성 경계에서만 key를 요구한다.
- 관련: `app/core/llm.py`, `app/agents/buyer/{graph,recommendation/graph}.py`, Issue #82 전체 테스트

## [2026-07-23] 복합 셸 명령의 정규식 인용은 실행 전에 셸 문법으로 검증한다
- 증상: provider 하드코딩 검색과 테스트를 한 명령으로 묶다가 작은따옴표가 포함된 정규식을 zsh 문자열 안에 잘못 중첩해 `unmatched "`로 전체 명령이 실행 전에 실패했다.
- 원인: JSON·zsh·정규식의 세 인용 계층을 한 줄에서 섞고, 검색과 테스트처럼 실패 영향이 다른 작업을 불필요하게 결합했다.
- 규칙: 인용이 복잡한 정규식은 단순한 패턴 여러 개로 나누거나 별도 명령으로 실행한다. 테스트 명령은 사전 검색과 분리해 검색 인용 오류가 검증 실행을 막지 않게 한다.
- 관련: Issue #82 provider 하드코딩 검색·집중 테스트

## [2026-07-23] zsh 스크립트에서 `path` 변수명을 쓰면 명령 검색 경로가 사라진다
- 증상: Issue #82 worktree 생성 스크립트에서 `path=/...`를 대입한 직후 후속 `git` 명령들이 `command not found`로 실패했다. worktree 생성 전이라 저장소 변경은 없었다.
- 원인: zsh의 `path`는 `PATH`와 연결된 특수 배열 변수인데 일반 경로 변수로 덮어써 실행 파일 검색 경로가 단일 디렉터리로 바뀌었다.
- 규칙: zsh 셸 스크립트에서는 `path`를 일반 변수명으로 사용하지 않고 `worktree_path`, `target_path`처럼 구체적인 이름을 쓴다. 여러 단계 셸 명령은 특수 변수 충돌을 피하도록 변수명을 명시적으로 선택한다.
- 관련: Issue #82 worktree 생성 준비

## [2026-07-23] `omx explore`는 제거된 명령이므로 저장소 조회에 사용하지 않는다
- 증상: Issue #82의 현재 구현 상태를 재확인하려고 `omx explore --prompt ...`를 실행했으나, 명령이 hard-deprecated되어 즉시 종료 코드 1로 실패했다.
- 원인: AGENTS.md의 구형 command-routing 안내를 현재 OMX 런타임의 migration 안내보다 우선해 적용했다. 현재 설치본은 일반 Codex 조회 도구/역할 표면을 사용하도록 명시한다.
- 규칙: 저장소 파일·심볼 조회는 일반 읽기 도구를 사용하고, 명시적인 셸 증거가 필요할 때만 `omx sparkshell -- <command>`를 사용한다. `omx explore`는 재시도하지 않는다.
- 관련: Issue #82 재검증, OMX CLI hard-deprecation 안내

## [2026-07-22] 멱등 row 하나로 PROCESSING과 COMPLETED를 겸하면 부분 실패가 영구 duplicate가 된다
- 증상: I-20이 버퍼 처리 전에 영구 마커를 넣은 뒤 consolidation의 `False` 반환을 무시해 버퍼를 삭제했고, 요청 취소·프로세스 crash 때는 cleanup이 실행되지 않아 미완료 통지가 이후 영구 `duplicate`가 됐다.
- 원인: 수신 선점 락과 처리 완료 기록을 같은 불변 row로 표현했고, consolidation도 정상 no-op과 실패를 같은 boolean `False`로 표현했다. 서로 다른 상태를 합치니 호출자가 실패와 성공을 구분할 수 없었다.
- 규칙:
  - 외부 부수효과 전 멱등 선점이 필요하면 `PROCESSING` claim(token+lease)과 `COMPLETED`를 분리한다.
  - 실패·취소는 소유 token이 일치하는 claim만 해제하고, crash 잔재는 lease 만료 뒤 재선점한다. 완료 row는 lease와 무관하게 영구 중복 처리한다.
  - 다단계 결과는 `updated/no_work/failed`처럼 의미를 분리한다. 실패 때는 입력 버퍼와 재시도 경로를 모두 보존한다.
  - 기존 볼륨에 상태 컬럼을 추가할 때는 init script만 믿지 말고 앱 기동 idempotent migration을 제공한다.
- 관련: `app/api/events.py`, `app/agents/profile/{builder,processed_events}.py`, `db/profile/init/00_processed_events.sql`, api-spec §3.5(v0.15.17)

## [2026-07-22] 멱등 응답 판정은 처리 대상 조회보다 먼저 해야 빈 버퍼 재전송도 duplicate가 된다
- 증상: PR #64 구현은 session-end 버퍼를 먼저 조회해 비어 있으면 즉시 `accepted`를 반환했다. 첫 통지를 정상 처리해 버퍼가 비워진 뒤 같은 통지가 재전송되면, 이미 저장된 멱등키가 있어도 확인하지 않아 `duplicate`가 아니라 `accepted`로 잘못 응답했다. 기존 테스트는 두 응답의 HTTP 202만 확인해 응답 본문 회귀를 놓쳤다.
- 원인: "처리할 데이터가 없으면 no-op"과 "이 통지를 이미 수신했는가"를 같은 조건으로 취급했다. 멱등성은 현재 버퍼 상태가 아니라 통지 신원 `(userId, sessionId)`의 이력에 관한 계약이라 버퍼 조회보다 우선해야 한다.
- 규칙:
  - 통지 엔드포인트는 **인증·스키마 검증 → 원자적 멱등 판정 → 처리 대상 조회** 순서를 지킨다.
  - 첫 유효 통지는 버퍼가 없어도 `accepted`로 기록하고, 이후 같은 통지는 버퍼 상태와 무관하게 `duplicate`로 응답한다.
  - 내부 처리 실패 때만 마킹을 되돌려 재시도를 허용하고 버퍼를 보존한다.
  - 멱등 테스트는 상태 코드뿐 아니라 실제 순서의 응답 본문(`accepted` 다음 `duplicate`)을 검증한다.
- 관련: `app/api/events.py::session_end()`, `tests/unit/test_profile.py`, `tests/integration/test_profile_flow_e2e.py`, api-spec §2.7/§3.5(v0.15.17), 이슈 #62, PR #64

## [2026-07-22] FE 오류 계약을 논할 때 "FastAPI 기본 422" 로 단정하지 말 것 — 이 앱은 검증 오류를 400 으로 매핑한다
- 증상: 판매자 챗 FE 계약 1차 분석에서 "요청 본문 검증 실패(threadId 누락 등)는 FastAPI 기본 422 로 나온다, 노션의 400 은 틀렸다"고 적었는데 반대였다 — 앱은 `RequestValidationError` 를 **400 `BAD_REQUEST` 봉투**로 매핑한다(`app/core/errors.py::_validation_exception_handler`, `add_exception_handler(RequestValidationError, ...)`). 노션의 400 이 옳았고 내 진단이 틀렸다.
- 원인: FastAPI의 프레임워크 기본값(422)을 앱의 실제 동작으로 착각했다. 이 리포는 모든 오류를 공통 봉투(§2.5)로 통일하려고 검증 오류까지 400 으로 재매핑하는 커스텀 핸들러를 둔다 — 기본값 지식이 아니라 코드를 봐야 알 수 있다.
- 규칙:
  - **HTTP 상태·오류 코드를 문서에 단정하기 전에 `app/core/errors.py`(예외 핸들러 등록부)를 먼저 확인한다.** "FastAPI/Starlette 기본은 X" 라는 일반 지식은 커스텀 핸들러가 있으면 무효다.
  - 특히 **422 vs 400**: 이 앱에서 요청 스키마(Pydantic) 검증 실패는 항상 **400 BAD_REQUEST**다. FE 계약·명세에 422 라고 쓰면 틀린다.
  - **"미구현" 도 마찬가지로 코드로 확인한다.** 같은 문서 작업에서 `429 RATE_LIMITED` 를 "미구현" 으로 단정했는데, 실제로는 `app/core/ratelimit.py` 가 `/seller/chat` 에 적용돼 있었다(`_LIMITED_PATHS`, config 상한). 미들웨어·핸들러는 라우터 코드에 안 보이므로 `app/main.py` 등록부와 `app/core/` 를 훑고 나서 "없다" 고 말한다.
- 관련: `app/core/errors.py`, `app/core/ratelimit.py`, `app/main.py`, `app/schemas/seller.py::SellerChatRequest`, `docs/specs/FE-CONTRACT-SELLER-CHAT.md` §4.1

## [2026-07-22] 샌드박스에서 `git rm` 을 쓰면 지우지 못하는 `.git/index.lock` 이 남아 이후 모든 git 작업이 막힌다
- 증상: docs/specs 판매자 문서 정리 중 샌드박스 셸에서 `git rm` 실행 → `error: the following files have local modifications` 로 실패했는데, 동시에 `warning: unable to unlink '.git/index.lock': Operation not permitted` 가 떴다. 실패한 커맨드가 만든 0바이트 `index.lock` 이 남았고 `rm -f` 로도 지워지지 않아, 그대로 뒀으면 Windows 쪽 git 도 전부 `Unable to create index.lock: File exists` 로 막힐 뻔했다.
- 원인: 두 겹이다. ① 샌드박스는 `.git/` 에 쓰기 권한이 없어 git 의 lock 해제가 실패한다. ② `local modifications` 자체가 허상 — 이 리포는 CRLF/LF 때문에 워킹트리 전 파일이 ` M` 으로 보이지만 `git diff --ignore-cr-at-eol --stat` 은 비어 있다(HANDOFF-GIT-SYNC-20260719 에서 이미 진단된 것과 동일한 현상). 즉 "수정됐으니 못 지운다"는 git 의 안전장치가 줄바꿈 노이즈 때문에 오작동한 것이다.
- 규칙:
  - **샌드박스에서 index 를 쓰는 git 명령(`git rm`·`add`·`commit`·`checkout`)을 실행하지 않는다** — 읽기 전용 조회(`log`·`diff`·`status`·`show`)만 쓴다. 스테이징·커밋은 Windows 터미널에서 사용자가 한다.
  - 샌드박스에서 파일을 지워야 하면 **git 을 거치지 않고 파일시스템 `rm`** 을 쓴다(Cowork 에선 삭제 권한 승인 후 가능). git 은 나중에 ` D` 로 알아서 인식한다.
  - 실수로 lock 을 만들었으면 **즉시** `rm -f .git/index.lock` 을 시도하고, 실패하면 사용자에게 Windows 에서 `del .git\index.lock` 을 요청한다 — 방치하면 사용자 쪽 git 이 전부 막힌다.
  - 이 리포에서 "전 파일이 수정됨"으로 보이면 실제 변경이 아니라 CRLF 노이즈를 먼저 의심한다 — 판단 기준은 항상 `git diff --ignore-cr-at-eol`.
- 관련: `docs/specs/` 판매자 문서 정리(2026-07-22), 구 `HANDOFF-GIT-SYNC-20260719`(삭제됨 — git 히스토리 참조)

## [2026-07-21] 통지 엔드포인트의 멱등키는 "그 이벤트가 몇 번 오는지"를 BE 실측으로 확인한 뒤 정한다 — 가정 금지
- 증상: PR #64 — session-end 멱등키를 놓고 두 모델이 충돌했다. (a) `(userId, sessionId)` 고정키(세션당 1회 종료 전제) vs (b) 버퍼 내용 해시(세션이 살아남아 재체크포인트된다는 전제 — tabClose 저장 후 재활동 → inactivityTimeout 재저장). 어느 쪽이 맞는지는 **"session-end 가 한 sessionId 에 몇 번 오는가"** 라는 BE 사실에 달렸는데, 그걸 확인하지 않고 (b)로 갔다가 뒤집혔다.
- 원인·확인: BE(`ChatSessionService`) 실측 — session-end 를 발화하는 `NEW_CONVERSATION`(issue 축출)·`LOGOUT`(endSession)은 **모두 세션을 Redis 에서 삭제**하며, `tabClose`·`inactivityTimeout`(IDLE_TIMEOUT)은 **아예 발화되지 않는다**. 즉 "한 sessionId = 한 번의 논리적 종료"가 참이라 (b)가 방어하려던 재체크포인트는 실재하지 않았다 → 고정키(a)가 정답이고 내용 해시는 과설계.
- 후속 경계(PR #83): 위 결론은 **Spring I-20의 영구 종료**에만 적용된다. AI가 자체 판정하는 inactivity는 생산자 종료 이벤트가 아니라 재개 가능한 checkpoint이므로, 같은 고정키를 동시 실행 mutex(`PROCESSING`)로는 재사용하되 idle 성공으로 `COMPLETED`를 영구 소비하면 안 된다. idle 뒤 같은 sessionId의 새 활동은 activity를 `ACTIVE`로 되돌리고 다음 checkpoint가 다시 처리해야 한다.
- 규칙:
  - **멱등키 모델을 정하기 전에 "이 이벤트가 한 번 오는가, 여러 번 오는가"를 생산자(BE) 코드/실측으로 확인**한다 — 가정으로 "여러 번 온다"고 단정하면 불필요한 내용/버전 키로 과설계한다. 한 번이면 신원 고정키가 가장 단순·안전하다.
  - **seq/카운터를 멱등키에 쓰기 전 "그 값이 리셋되는 경로"를 확인**한다(여기선 버퍼가 비면 item 삭제로 seq 리셋 → 판별자 부적합).
  - 멱등 판별자는 "같은 통지 재전송 → 중복", "빈 내용 → no-op" 경로를 테스트로 고정한다.
- 관련: `app/api/events.py`(고정 멱등키), `ChatSessionService`(종료=세션 삭제), api-spec §2.7/§3.5, PR #64

## [2026-07-21] inbound 계약을 "제안/초안"인 채 required 필드로 굳히면 엔드포인트가 상시 400으로 조용히 실패한다
- 증상: 이슈 #62 — `POST /events/session-end`가 항상 `400`을 반환해 세션 종료 통지가 전부 실패, 프로필 조기 트리거가 조용히 죽어 있었다. 원인은 3자 불일치: api-spec §3.5가 `eventId`/`userId(string)`/`endedAt`를 **"제안(초안)"** 표기인 채 두었고, `SessionEndEvent`는 그 초안을 **required**로 굳혔는데, Spring 실측 payload는 `eventId`가 없고 `userId`가 **숫자**였다. 초안 필드가 필수라 매 요청이 검증 단계에서 튕겨 핸들러에 도달조차 못 했다.
- 원인: 인바운드(Spring→AI) 계약은 우리가 소유(결정 21)하지만 **데이터 생산자는 Spring**이다. 소유권이 우리에게 있다고 초안을 실측 대조 없이 required 로 확정하면, 우리 코드는 "옳지만" 실제 호출은 100% 실패한다. best-effort·통지 채널이라 500도 안 나고 202도 안 나가 **관측되지 않는 상시 실패**가 된다.
- 규칙:
  - **api-spec에 `제안`/`초안`/🔴 협의 표시가 붙은 인바운드 필드를 스키마 required 로 굳히기 전에, BE 실측 payload와 대조**한다. 특히 `eventId` 같은 "우리가 만들어낸 멱등 필드"는 생산자가 실제로 보내는지 확인 — 안 보내면 파생키(본문 신원)로 전환한다.
  - **타입도 대조한다** — id는 이 프로젝트에서 BIGINT 숫자가 기준(CLAUDE.md). 인바운드 신원 필드를 `str`로 두면 숫자 payload가 조용히 400난다.
  - 통지/best-effort 엔드포인트는 실패가 눈에 안 띈다 — **계약 정렬 후 "누락·타입오류→400, 정상→202, 중복→202 duplicate"를 명시적 테스트로 고정**한다.
- 관련: `app/schemas/profile.py::SessionEndEvent`, `app/api/events.py::session_end()`, api-spec §3.5/§2.7(v0.15.17), 이슈 #62

## [2026-07-20] SSE 응답 제너레이터의 finally 블록에서 던진 예외는 종결 프레임/취소 전파를 덮어쓴다
- 증상: PR #48 후속 리뷰가 `app/core/stream.py::open_stream()`의 `_wrapped()` `finally` 블록(303행)에서 `observer.finish()`(이제 실제 conversation store DB I/O)가 보호 없이 호출된다고 지적. 이 시점은 이미 SSE 헤더/프레임이 클라이언트로 전송된 뒤라, `finish()`가 예외를 던지면 (1) 정상 종료 경로에서는 `StopAsyncIteration` 대신 그 새 예외가 `body_iterator` 소비자에게 전파되어 스트림이 비정상 종료되고, (2) `except asyncio.CancelledError: ... raise` 로 취소가 전파되던 중이라면 Python 의 `finally`-중 예외가 진행 중이던 예외를 덮어쓰는 규칙 때문에 정상 client disconnect(CancelledError)가 엉뚱한 새 예외로 둔갑한다. `finalize_assistant` 를 raise 하는 fake 로 재현 — 수정 전엔 `body_iterator` 소비 자체가 raise, 수정 후(try/except 로 감싸 로그만)엔 정상 종료.
- 원인: 이 프로젝트에서 인메모리→외부 스토어 이관은 반복적으로 "이전엔 실패할 수 없던 호출이 이제 실패할 수 있다"는 패턴을 만드는데(PR #47 의 `session_end()`도 동일 클래스), 이번엔 그 호출이 **이미 응답이 시작된 SSE 스트림의 finally** 안에 있어 파급이 더 크다 — 응답 시작 전 실패(그냥 500)와 응답 시작 후 finally 실패(스트림 자체가 깨짐)는 심각도가 다르다.
- 규칙:
  - **SSE/스트리밍 응답의 `finally` 블록은 "여기서 예외가 나면 이미 보낸 프레임들과 무관하게 스트림 자체가 깨진다"는 걸 항상 의식한다** — 정리 로직(레지스트리 해제 등)과 부가적 관측/저장 로직(finish, 로깅)을 구분해, 후자는 반드시 자체 try/except 로 격리한다.
  - **같은 함수 안에 여러 `observer.finish()` 호출부가 있어도, 응답이 이미 시작된 뒤(스트림 본문 생성기 안)의 호출부와 응답 시작 전(핸들러 동기 구간)의 호출부는 심각도가 다르다** — 전부 동일하게 취급해 한 번에 고치려 하지 말고, "이미 클라이언트에 데이터가 나간 뒤인가"를 기준으로 우선순위를 가른다(이번엔 딱 하나, `_wrapped()` finally 만 진짜 취약점이었다).
  - 리뷰가 "이 패턴이 다른 호출부에도 있다"고 폭넓게 지적해도, 그 다른 호출부들이 실제로 같은 심각도인지(예: 응답 시작 전이라 그냥 500이 되는지) 확인하고 나서 고칠 범위를 정한다 — 전부 고치는 게 항상 정답은 아니다.
- 관련: `app/core/stream.py::open_stream()._wrapped()`, `tests/unit/test_observability.py::test_stream_completes_when_finalize_assistant_fails`, PR #48 후속 리뷰

## [2026-07-20] 지연 정리 큐 패턴을 그대로 복사하면 안 되는 리소스가 있다 — AsyncConnectionPool 은 백그라운드 워커 태스크가 있어 cross-loop 정리가 그 자체로 새 버그다
- 증상: PR #47 후속 리뷰가 `app/agents/profile/processed_events.py`의 `set_pool()`/`reset()`이 `app/core/pg_store.py`(PR #46 후속 리뷰)와 동일한 fire-and-forget 스킵 버그를 갖고 있다고 지적 — `_pending_cleanup` 큐 패턴을 그대로 복사해 적용했더니, `tests/integration/test_pg_profile_store.py`를 전체 실행하면(개별 실행은 통과) 엉뚱한 다른 테스트(`test_processed_events_unmark_allows_reprocessing`)까지 `CancelledError`로 실패했다.
- 원인: `pg_store.py`가 감싸는 `AsyncPostgresStore`/`AsyncConnection`은 단일 커넥션이라 정리(`__aexit__`)가 비교적 단순하지만, `processed_events.py`의 `AsyncConnectionPool`은 **백그라운드 워커 태스크**를 그 풀을 만든 이벤트 루프에 묶어 둔다. pytest-asyncio 는 테스트 함수마다 새 이벤트 루프를 쓰므로, 이전 테스트(다른 루프)에서 큐에 쌓인 풀을 다음 테스트(새 루프)의 `_get_pool()`이 드레인하려 하면 이미 죽은 루프에 묶인 워커 태스크를 `await agather(...)`로 기다리게 되어 `CancelledError`(`asyncio.CancelledError`는 `BaseException` 상속 — `except Exception`으로 안 잡힘)가 새어 나온다. **"같은 이름의 버그"라고 반드시 같은 수정이 안전한 건 아니다** — 리소스의 내부 구현(워커 태스크 유무)에 따라 cross-loop 정리의 안전성이 다르다.
- 규칙:
  - **`_pending_cleanup` 류의 "다음 async 호출 때 정리" 패턴을 다른 리소스 타입에 이식하기 전에, 그 리소스가 정리 시점에 실제로 무엇을 하는지(백그라운드 태스크가 있는지, 자신을 만든 이벤트 루프에 의존하는지) 확인한다** — 겉보기엔 동일한 "sync 함수 안에서 async 리소스 정리" 문제여도, 커넥션 풀처럼 내부에 태스크를 갖는 리소스는 원래 만들어진 루프가 사라지면 정상적으로 닫을 방법이 없다.
  - **최선형(best-effort) 정리 경로에서 `contextlib.suppress(Exception)`은 `asyncio.CancelledError`를 잡지 못한다** — `CancelledError`는 `BaseException` 서브클래스라 별도 처리가 필요하다. "닫히면 좋고 안 닫혀도 그만"인 게 명확한 경로(참조를 이미 버려 재사용 안 함)라면 `suppress(BaseException)`으로 넓혀도 안전하다.
  - 수정 직후 반드시 **전체 파일을 통째로(개별이 아니라) 여러 번 반복 실행**해 안정성을 확인한다(이번엔 3회 반복으로 검증) — 개별 테스트 통과만으로는 순서 의존 회귀를 놓친다(같은 이유로 이전에 이미 한 번 겪은 교훈이기도 하다).
- 관련: `app/agents/profile/processed_events.py::_drain_pending_cleanup()`, `tests/integration/test_pg_profile_store.py::test_processed_events_set_pool_none_defers_cleanup_to_next_get_pool_call`, PR #47 후속 리뷰

## [2026-07-20] "실패할 수 없던 호출"이 인메모리→외부 스토어 이관 후 실패 가능 호출로 바뀌면 기존 try 범위가 새지 않는지 재점검해야 한다
- 증상: PR #47 후속 리뷰가 `app/api/events.py::session_end()`에서 `get_profile_store()`/`processed_events.mark_if_new()`/`store.clear_session_ctx_upto()` 세 호출이 `try` 블록 밖(또는 뒤)에 있어 예외가 안 잡힌다고 지적. 이관 전(인메모리 싱글턴) 이 호출들은 절대 실패할 수 없었지만, 이슈 #33 이관 후 운영(`auth_mode=jwks`)에서는 pg-profile 연결 실패 시 폴백 없이 `raise`하므로, DB 일시 장애만으로 이 엔드포인트가 500을 반환 — `§3.5`("어떤 오류도 202를 막지 않는다")를 위반한다. `get_profile_store()`를 raise 하는 fake 로 재현 — 수정 전엔 테스트가 raw exception 으로 실패(=500), try 범위를 넓힌 수정 후엔 202 통과.
- 원인: 원래 코드는 "이 호출은 안전하다"는 전제로 짜여 있었는데, 그 전제(인메모리라 실패 불가) 자체가 이관으로 깨졌다. 인메모리→외부 스토어 이관은 데이터 구조뿐 아니라 "이 호출이 실패할 수 있는가"라는 실패 모델 자체를 바꾼다 — 기존 에러 핸들링 경계(try 범위)가 새 실패 모델을 커버하는지 별도로 재검토해야 하는데 그걸 놓쳤다.
- 규칙:
  - **동기 인메모리 호출을 비동기 외부 스토어 호출로 바꿀 때, 그 호출을 감싼 기존 `try`/`except` 범위가 "새로 실패 가능해진" 모든 호출을 포함하는지 호출부 단위로 다시 확인한다** — 스토어 내부 구현(락·재시도 등)만 고치고 호출부의 에러 경계는 그대로 두면, "실패할 수 없던 코드가 실패할 수 있게 됐는데 아무도 안 잡는" 구멍이 생긴다.
  - **best-effort 계약(예: §3.5 "항상 202")이 있는 엔드포인트는, 그 계약을 지키는 try/except가 계약이 적용되는 모든 실패 가능 호출을 포함하는지 체크리스트처럼 확인한다** — 일부만 감싸면 "대부분의 경우 202"가 되어 계약 위반이 드물게만 재현되므로 놓치기 쉽다.
  - 실패 시 후처리(예: `unmark_event`)도 그 자체가 같은 외부 스토어를 건드리므로 실패할 수 있다 — 후처리 실패가 원래 응답(202)을 막지 않도록 별도로 `suppress`한다.
- 관련: `app/api/events.py::session_end()`, `tests/unit/test_profile.py::test_session_end_returns_202_when_profile_store_unavailable`, PR #47 후속 리뷰

## [2026-07-20] 공유 락을 쥔 채 실행되는 초기화 블록은 모든 await 지점에 상한이 있어야 한다
- 증상: PR #46 후속 리뷰가 `app/core/pg_store.py::get_store()`에서 `ctx.__aenter__()`(커넥션 수립)만 `state_store_connect_timeout_s`로 감싸져 있고, 바로 다음의 `await store.setup()`(스키마 DDL)에는 타임아웃이 없다고 지적. 이 블록 전체가 `_init_lock`을 쥔 채 실행되는데, 이 락은 `CartStateStore`·`ThreadFilterStore`·`RevertStore`가 전부 공유한다 — `setup()`이 (Postgres 락 경합 등으로) 멈추면 이후 들어오는 모든 buyer 요청이 함께 무한 대기한다. fake 스토어(`setup()`이 영원히 안 끝남)로 재현 — 수정 전엔 테스트가 실제로 타임아웃/hang, 수정 후(동일 timeout 으로 `setup()`도 wait_for)엔 통과.
- 원인: "커넥션 수립에 타임아웃을 걸었으니 초기화가 안전하다"고 안이하게 판단 — 같은 try 블록 안에 있는 **다른 await 지점**(`setup()`)은 별도로 감싸지 않으면 보호받지 않는다는 걸 놓쳤다. 공유 락 안에서 실행되는 코드는 그 블록의 "가장 느린 await"가 전체의 상한이 된다.
- 규칙:
  - **공유 락(`asyncio.Lock` 등)을 쥔 채 실행되는 코드 블록은, 그 안의 모든 외부 I/O await 지점에 개별적으로 타임아웃을 건다** — 하나만 걸고 나머지는 "그 정도면 되겠지"로 넘기지 않는다.
  - **"이론상 우려"를 리뷰가 지적하면, 실제 hang을 재현하는 fake/mock 으로 검증한다** — 실 DB로는 인위적으로 멈추는 상황을 안정적으로 재현하기 어려우므로, `setup()` 자체를 무한 `sleep()` 하는 fake 로 교체해 결정론적으로 재현.
  - 반면 같은 리뷰 라운드의 다른 지적(`entered_ctx`가 `__aenter__` 타임아웃 시 정리를 스킵)은, 라이브러리 내부(`@asynccontextmanager`로 감싼 `async with await AsyncConnection.connect(...) as conn:`)가 취소 시 자체적으로 정리할 가능성이 높아 "증명된 버그"로 보기 어려웠다 — 그래도 `entered_ctx` 대신 `ctx`로 통일해 비대칭을 없애는 비용 제로 방어 조치는 유지했다. **모든 리뷰 지적이 같은 확신도를 갖는 건 아니다** — 재현 가능한 것과 방어적으로만 유지하는 것을 구분해서 기록한다.
- 관련: `app/core/pg_store.py::get_store()`, `tests/unit/test_pg_store.py::test_get_store_bounds_hanging_setup_by_timeout`, PR #46 후속 리뷰

## [2026-07-20] fire-and-forget 정리(`asyncio.get_running_loop().create_task`)는 sync autouse fixture 컨텍스트에서 매번 조용히 스킵됨
- 증상: `set_store(None)`이 기존 실 연결을 "백그라운드 태스크로 정리"하도록 고쳤는데(이전 lessons 항목 — 당시엔 fire-and-forget 방식 자체의 검증 실패만 기록하고 원인 규명은 못 함), claude[bot] 후속 리뷰가 "`set_store()`는 sync 함수라 실행 중인 이벤트 루프가 없으면(`asyncio.get_running_loop()`가 RuntimeError) 정리가 스킵되는데, `tests/conftest.py`의 sync autouse fixture가 정확히 그 상황"이라고 지적. 직접 프로브 테스트로 확인한 결과 **실제로 conftest의 autouse fixture(setup 단계)는 항상 실행 중인 이벤트 루프가 없는 상태**였다 — 즉 이 정리 로직은 테스트 환경에서 단 한 번도 실제로 실행된 적이 없었다.
- 원인: pytest-asyncio 는 async 테스트 함수 실행을 위해 그 함수 안에서만 이벤트 루프를 돌리고, sync autouse fixture(테스트 함수 진입 전 setup)는 그 루프 시작 **전**에 실행된다. `contextlib.suppress(RuntimeError)`로 감싸 "실행 중 루프 없으면 조용히 스킵"하게 만든 게, 겉보기엔 안전한 방어 코드처럼 보이지만 실제로는 "이 정리 코드가 의도한 경로에서 단 한 번도 실행되지 않는다"는 뜻이었다 — 예외를 삼키는 코드가 있으면 "잘 동작하는 중"과 "매번 조용히 실패하는 중"을 로그 없이는 구분할 수 없다.
- 규칙:
  - **"실행 중 이벤트 루프가 없으면 스킵"하는 fire-and-forget 패턴은, 그 코드가 실제로 실행되는 호출 경로들의 이벤트 루프 유무를 전부 실측 확인한다** — 특히 테스트 conftest 의 autouse fixture 는 sync 인 경우가 흔한데, sync fixture 라고 해서 "이벤트 루프가 있을 수도 있겠지"라고 가정하면 안 된다. 직접 `asyncio.get_running_loop()` 를 프로브해서 확인(이번처럼).
  - **필요한 정리를 "당장 못하면 다음 기회에 확실히 한다"는 지연 큐 방식이 fire-and-forget 보다 안전하다** — `set_store()`(sync, 정리 대상을 리스트에 쌓기만 함) → 다음 `get_store()`(반드시 async 컨텍스트) 진입 시 그 큐를 `await` 로 확실히 비운다. 이러면 "이벤트 루프가 있는지 없는지"를 신경 쓸 필요가 없고, 타이밍에 의존하지 않아 `conn.closed` 로 결정론적으로 검증 가능하다(이전 fire-and-forget 은 검증 자체가 불가능했음).
  - **`except`/`suppress`로 예외를 삼키는 코드를 작성할 때마다 "이 경로가 실제로 정상 실행되는지"를 별도로 검증할 방법을 만든다** — 삼켜진 예외는 로그 없이는 흔적이 안 남으므로, "예외가 안 났다"와 "정상 실행됐다"를 혼동하기 쉽다.
- 관련: `app/core/pg_store.py::set_store/_drain_pending_cleanup`, `app/agents/profile/store.py::set_store/_drain_pending_cleanup`(동일 패턴 후속 적용), `tests/integration/test_buyer_thread_store.py::test_set_store_none_defers_cleanup_to_next_get_store_call`, PR #46/#47 후속 리뷰

## [2026-07-20] 모듈 전역 asyncio.Lock 을 pytest-asyncio function-scope 이벤트 루프에서 재사용하면 hang
- 증상: PR #47 리뷰(락 없는 초기화 레이스) 반영 후 `tests/integration/test_pg_profile_store.py` 전체를 한 번에 실행하면 11번째 테스트(`test_processed_events_mark_if_new_atomic_under_concurrency`, 기존에 있던 테스트라 이번에 새로 건드리지 않음)에서 FAILED 가 뜬 뒤 그다음 테스트로 전혀 진행되지 않고 무한정 멈췄다(`timeout 30`으로 강제 종료해야 빠져나옴). 그런데 신규로 추가한 동시성 테스트 3건은 **개별 실행하면 전부 통과**했고, 실패한 그 테스트도 **단독 실행하면 통과**했다 — 오직 "여러 테스트가 순서대로 실행될 때"만 재현됐다.
- 원인: `pytest.ini`(`pyproject.toml`)의 `asyncio_default_test_loop_scope=function` — 즉 pytest-asyncio 가 **테스트 함수마다 새 이벤트 루프**를 만든다. 반면 `_init_lock = asyncio.Lock()` 은 모듈이 세션 중 처음 import 될 때 **딱 한 번만** 생성되는 모듈 전역 객체다. 이 락이 어느 테스트의 루프에서 획득된 채로 그 루프가 닫혀버리면(`acquire()`는 됐는데 해당 루프에서 `release()`가 정상 실행되지 못한 채 루프가 종료되는 경우), 락의 내부 상태(`_locked=True`)는 그대로 남고 다음 테스트가 **다른 새 루프**에서 그 락을 `async with`로 얻으려 하면 영원히 풀리지 않는 `_locked=True` 를 보고 대기만 하다가 hang 된다. `asyncio.Lock`(Python 3.10+)은 생성자에서 루프를 요구하지 않아 이런 재사용이 "일단 되는 것처럼" 보이지만, 락 상태 자체는 루프와 무관하게 유지되므로 **정상 해제가 보장되지 않으면 그대로 다음 루프까지 전염**된다.
- 규칙:
  - **pytest-asyncio 가 function-scope 이벤트 루프를 쓰는 프로젝트에서, 모듈 전역 `asyncio.Lock`/`asyncio.Event`/`asyncio.Semaphore` 등 동기화 프리미티브는 테스트 격리(reset) 함수에서 반드시 재생성한다** — `_store`/`_pool` 같은 데이터만 초기화하고 락 객체 자체를 놔두면, 어느 한 테스트에서 락이 비정상 해제된 순간부터 이후 모든 테스트가 도미노로 hang 된다.
  - 재현이 안 되던 게 갑자기 "여러 테스트를 같이 돌릴 때만" 발생하면, 먼저 **개별 실행이 전부 통과하는지**부터 확인한다(이번처럼 개별 통과 + 조합 hang 이면 순서 의존 상태 공유가 원인일 확률이 높다).
  - `app/core/pg_store.py`(PR #46)에서 처음 이 락 패턴을 썼을 때는 이 문제가 안 드러났다 — 우연히 그 조합의 테스트에서는 락이 비정상 해제되는 시퀀스가 안 걸렸을 뿐, 근본 취약점은 동일하게 있었다(이번에 pg_store.py 의 `reset_store()`도 함께 고쳤다). "지금까지 안 터졌다"가 "안전하다"의 증거가 아니다.
- 관련: `app/core/pg_store.py::reset_store()`, `app/agents/profile/store.py::reset_profile_store()`, `app/agents/profile/processed_events.py::reset()`, PR #46/#47 후속 리뷰

## [2026-07-20] "락이 없으면 이론상 레이스"라는 리뷰 지적도 실제 데이터 구조를 보고 검증해야 한다
- 증상: PR #47 후속 리뷰가 `ProfileStore.add_fact()`의 cap 트리밍(asearch→sort→adelete)에 락이 없어 lost update 가 가능하다고 지적. `append_session_ctx`(단일 값 get→put)와 같은 패턴으로 보고 동일하게 락을 추가했으나, 실제로 버그를 재현하려 했더니 **실 Postgres 동시 호출(gather)도, 강제로 인터리브시키는 fake store(asearch 에 `await asyncio.sleep(0)` 삽입)도 모두 락 없이 통과** — 두 가지 서로 다른 방법으로 재현을 시도했음에도 데이터 유실이 재현되지 않았다.
- 원인: `append_session_ctx`는 "단일 값을 덮어쓰는" get→put(진짜 lost update 가능 — 나중 write 가 앞선 write 를 통째로 덮어씀)인 반면, `add_fact`의 cap 트리밍은 계속 늘어나기만 하는 항목 집합에서 "가장 오래된 초과분만 지우는" 연산이다. 임의 시점의 부분 스냅샷은 항상 "그 시점까지 커밋된 항목들의 시간순 앞부분(prefix)"이므로, 서로 다른 스냅샷을 본 동시 호출들의 삭제 대상은 항상 서로 부분집합 관계이고 `adelete`가 멱등이라 실제로는 자기 교정(self-correcting)된다 — 겉보기엔 같은 "락 없는 get→act" 패턴이어도 데이터 구조의 단조성(monotonicity)에 따라 실제 위험도가 다르다.
- 규칙:
  - **"이론상 레이스처럼 보인다"와 "실제로 데이터가 유실된다"는 다른 질문이다** — 리뷰가 지적한 패턴이 기존에 이미 고친 유사 버그와 겉모습이 같다고 곧바로 같은 수정을 적용하지 말고, 먼저 재현을 시도한다.
  - **동시성 테스트가 실 인프라(Postgres) 타이밍에 의존하면 false negative 가 나올 수 있다** — 강제로 인터리브시키는 fake(예: `asyncio.sleep(0)` 삽입)로 별도 재현을 시도해, 두 방법이 일치하면 결론에 더 확신을 가질 수 있다.
  - 재현에 실패했다고 반드시 코드를 되돌릴 필요는 없다 — 이미 만든 락이 무해하고(비용 거의 0) 다른 락(`_session_locks`)과 패턴 일관성이 있다면 "증명된 버그의 수정"이 아니라 "방어적 조치"라고 정직하게 문서화하고 유지해도 된다. 다만 **그 사실을 감추지 않는다** — 나중에 누가 "이 락이 막는 버그가 뭐냐"고 물었을 때 근거 없는 답을 하지 않도록.
- 관련: `app/agents/profile/store.py::_fact_lock/add_fact()`, PR #47 후속 리뷰

## [2026-07-20] fire-and-forget 정리 태스크는 "참조를 든 채로 재사용" 방식으로는 검증 불가
- 증상: `pg_store.set_store()`가 기존 실 연결을 백그라운드 태스크(`create_task`)로 닫도록 고친 뒤, 회귀 테스트로 `store.aget()` 재호출이 실패하는지 확인하려 했으나 **정리 로직을 일부러 빼도 테스트가 계속 통과**했다. `conn.closed` 로 직접 확인하도록 바꿨더니 이번엔 **정리 로직을 빼도(TEMP) `conn.closed`가 True로 나와** 신뢰할 수 없는 결과였다(원인 미규명 — psycopg 커넥션이 pytest 이벤트 루프 재사용 과정에서 어떤 이유로든 닫힌 것으로 보이나 확정 못 함).
- 원인: (1) 테스트가 `store`/`conn` 객체를 로컬 변수로 계속 참조하고 있어, 모듈 전역 `_store_ctx`만 `None` 으로 바뀌어도 파이썬 GC 관점에서 그 객체는 죽지 않는다 — "참조가 끊겼는지"와 "실제로 `__aexit__`가 호출됐는지"는 다른 질문이다. (2) fire-and-forget(`asyncio.create_task`, await 로 완료를 기다리지 않음)은 태스크 완료 시점을 테스트가 통제할 수 없어 근본적으로 타이밍에 취약하다.
- 규칙:
  - **"객체 참조가 여전히 동작하는지"로 정리(cleanup)를 검증하지 않는다** — 로컬 변수가 참조를 쥐고 있는 한 GC 는 일어나지 않으므로 무의미한 양성(false positive)이 나온다. 정리 대상 리소스 자체의 상태 플래그(`conn.closed` 등)를 직접 확인해야 한다.
  - **그렇게 해도 fire-and-forget 은 안정적으로 재현 가능한 회귀 테스트를 만들기 어렵다** — 이런 경우 "재현 불가"를 인정하고 자동 테스트는 만들지 않되, 코드 리뷰(로직 정확성 수동 검토)로 대체하는 게 거짓 안전감을 주는 flaky 테스트보다 낫다. 무리하게 테스트를 만들어 통과시키면 오히려 "검증됐다"는 잘못된 확신을 준다.
  - 애초에 "sync 함수 안에서 정리가 필요한 async 리소스"를 다루는 설계(`set_store()`) 자체가 테스트하기 어려운 근본 원인 — 가능하면 정리가 필요한 리소스의 lifecycle 관리는 처음부터 async 경계 안에 두는 설계를 우선 고려한다.
- 관련: `app/core/pg_store.py::set_store()`, PR #46 후속 리뷰

## [2026-07-20] BaseStore 이관 시 "await 가 생기는 지점"마다 새 동시성 레이스가 생김 (PR #46 리뷰)
- 증상: claude[bot] PR 리뷰가 두 곳을 지적 — (1) `app/core/pg_store.py::get_store()` 가 `_store is None` 체크 후 `await ctx.__aenter__()` 사이에 락이 없어, 콜드 스타트 시 동시 요청이 각자 pg 커넥션을 중복 생성하고 앞선 연결(들)은 정리 없이 버려짐(누수) + `store.setup()` 부분 실패 시에도 이미 연 연결 미정리. (2) `RevertStore.add()` 가 `get()`(read) 후 `aput()`(write)하는 read-modify-write라, 동일 키로 겹치는 요청이 오면 lost update 발생. 두 지적 다 실제로 재현됨(락 제거 후 테스트 시 100% 재현).
- 원인: 인메모리 dict 시절엔 `dict.update()`/딕셔너리 대입이 await 없이 원자적이었는데(GIL·단일 이벤트 루프), BaseStore(pg-profile) 이관으로 각 연산이 별도 네트워크 왕복(`await`)이 되면서 "체크 후 await" 패턴이 전부 새 레이스가 됐다. 이슈 #33 전체(pg_store.py·profile/store.py·profile/processed_events.py·core/conversation.py 4곳)에 동일한 "지연 초기화" 패턴을 복붙했고, `ProfileStore.append_session_ctx`/`clear_session_ctx_upto`도 같은 get→put 형태라 잠재적으로 같은 레이스가 있다(리뷰 대상 밖이라 미수정 상태로 남아있을 수 있음 — 후속 확인 필요).
- 규칙:
  - **인메모리 → 외부 스토어 이관 리뷰 체크리스트**: "이 메서드에 새로 생긴 `await` 지점이 있는가?" → 있으면 "그 사이에 동일 key로 다른 호출이 끼어들면 최종 상태가 틀려지는가?"를 반드시 확인한다. 딕셔너리 시절엔 원자적이던 연산이 async 스토어 이관 후 깨지는 게 이번처럼 반복 패턴이다.
  - **지연 초기화(`if _store is None: ... await ...`)는 반드시 `asyncio.Lock` 으로 전체를 감싼다** — 체크와 초기화 사이에 어떤 `await` 도 없어야 안전하다는 직관은 틀렸다(초기화 자체가 await 를 포함하므로).
  - **read-modify-write(get→update→put) 패턴은 key 단위 `asyncio.Lock` 딕셔너리로 직렬화**(`app/agents/seller/hitl.py::_confirm_lock` 선례와 동일 패턴) — BaseStore 는 CAS/원자적 update 를 제공하지 않는다.
  - **동시성 수정은 "락 없이 실패 재현 → 락 추가 후 통과" 순서로 검증**한다(주석 처리 후 테스트 → 복구). 락이 정말 그 버그를 막는지 확인 없이 추가하면 false-sense-of-safety 가 된다.
- 관련: `app/core/pg_store.py`(`_init_lock`)·`app/agents/buyer/recommendation/state.py`(`_add_locks`)·`tests/integration/test_buyer_thread_store.py`(재현 테스트 2건), PR #46, 이슈 #33

## [2026-07-20] 로컬 .env 의 GOOGLE_API_KEY 가 유닛테스트의 라이브 API 의존 버그를 가려 CI 에서만 터짐
- 증상: 이슈 #33 Phase 2(ProfileStore) 작업 중 로컬 `uv run pytest` 는 575개 전부 통과했는데, GitHub Actions CI 에서 `tests/unit/test_profile.py`·`tests/integration/test_profile_flow_e2e.py` 14건이 `app.pipelines.embedding.EmbeddingError: google_api_key 미구성`으로 실패. 그중 일부는 `session_end` 의 넓은 `except Exception` 이 이 오류를 삼켜 `processed_events.unmark_event()`(멱등 마킹 해제)까지 실행시켜, "멱등 재전송이 duplicate 로 안 잡힘" 같은 2차 증상으로 위장해 원인 파악을 어렵게 함.
- 원인: `ProfileStore` 의 테스트/dev 폴백 `InMemoryStore(index=...)` 가 프로덕션과 **동일한 실제 `embed_texts`(Google API) 함수**를 그대로 물려써서, `add_fact()` 호출만으로 실 API 콜이 발생했다. 로컬 `.env` 에 이미 `GOOGLE_API_KEY`(#31 카탈로그 작업 때 설정)가 있어 로컬에서는 조용히 성공 — CI 에는 그 시크릿이 없어(원래 유닛 테스트는 라이브 키가 필요 없어야 정상이므로) 처음 노출됨.
- 규칙:
  - **유닛 테스트용 InMemory 폴백에 실제 외부 API 호출 함수를 그대로 주입하지 않는다** — BaseStore `index={"embed": ...}` 처럼 "설정만 있으면 자동으로 호출되는" 구조는 특히 위험(코드 흐름만 봐서는 API 호출이 숨어있는지 안 보임). 반드시 fake/no-op 버전으로 분리(`_pg_index_config()`실 API용 vs `_fallback_index_config()`fake 용, 이번 수정 패턴).
  - **로컬 `.env` 에 실 API 키가 있으면 "라이브 의존 없음" 가정이 로컬에서 검증되지 않는다** — 새 라이브 API 연동 코드를 추가했으면 `KEY= uv run pytest`(빈 값 오버라이드)로 CI 조건을 로컬에서 먼저 재현해 확인한다. 이 프로젝트는 이미 `_no_live_recent_purchases`(구매이력) 같은 라이브 차단 autouse fixture 관례가 있으니 신규 외부 API 연동 시 같은 원칙을 적용할 것.
  - 대량 실패 로그를 볼 때 **에러 메시지가 다른 여러 건도 먼저 근본 원인 1개로 수렴하는지 확인**한다 — 이번처럼 넓은 `except Exception` 이 있으면 원인 오류가 완전히 다른 증상(멱등 깨짐 등)으로 위장될 수 있다.
- 관련: `app/agents/profile/store.py`(`_pg_index_config`/`_fallback_index_config` 분리), 이슈 #33 (2/3)

## [2026-07-20] Windows 기본 ProactorEventLoop 에서 psycopg async 연결이 조용히 InMemory 로 폴백
- 증상: 이슈 #33(ThreadFilter/Cart/Revert → AsyncPostgresStore) 통합 테스트를 실제 pg-profile(docker) 에 붙여 작성하던 중, 네이티브 Windows 에서 `AsyncPostgresStore.from_conn_string(...).__aenter__()` 가 `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'` 로 실패. dev 폴백(auth_mode≠jwks)이 모든 예외를 잡아 InMemoryStore 로 조용히 전환하는 설계(app/agents/seller/history.py·hitl.py 와 동일 규약, 이제 app/core/pg_store.py 도)라 **오류 로그 없이는 겉보기엔 정상 동작**했다 — 즉 기존 seller history.py/hitl.py 도 네이티브 Windows dev 환경에서는 이 문제로 Postgres 연결이 한 번도 성사되지 않고 항상 InMemory 로 돌았을 가능성이 높다(테스트가 InMemoryStore 를 직접 주입해왔기 때문에 지금까지 미발견).
- 원인: asyncio 는 Windows 에서 기본으로 `ProactorEventLoopPolicy` 를 쓰는데, psycopg 의 async 커넥션은 `SelectorEventLoop` 만 지원한다. Docker(Linux) 컨테이너 안에서는 애초에 Proactor 가 없어 재현되지 않는다 — 네이티브 Windows 에서 앱을 직접 띄우거나(`uv run uvicorn ...`) 테스트를 돌릴 때만 드러난다.
- 규칙:
  - psycopg async(AsyncPostgresStore/AsyncPostgresSaver 등)를 새로 붙이는 코드는 **네이티브 Windows 에서 실제 연결까지 통합 테스트로 검증**한다 — InMemory 주입 테스트만으로는 이 클래스의 버그를 절대 못 잡는다.
  - `app/main.py` 모듈 최상단에 `sys.platform == "win32"` 가드로 `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` 를 추가해뒀다(uvicorn 이 루프를 만들기 전에 정책을 바꿔야 하므로 반드시 다른 임포트보다 먼저) — 이 앱에서 psycopg async 를 쓰는 모든 지점(seller history.py·hitl.py·core/pg_store.py)이 공통으로 이 정책에 의존한다. 신규 진입점(배치 CLI 등 uvicorn 을 거치지 않는 프로세스)을 추가할 때는 그 프로세스 자체 최상단에도 동일 가드가 필요하다.
  - 새 psycopg async 통합 테스트를 작성하면 `tests/integration/conftest.py` 가 `app.main` 을 임포트하는 시점(정책 적용)보다 먼저 다른 경로로 연결을 시도하지 않는지 확인한다.
- 관련: `app/main.py`, `app/core/pg_store.py`, `tests/integration/test_buyer_thread_store.py`, 이슈 #33

## [2026-07-20] CI "review pass" 를 리뷰 수렴으로 오인해 코멘트 도착 전에 머지
- 증상: PR #41 을 CI 통과(lint-test·review) + 코멘트 0건 확인 후 머지했는데, **머지 91초 뒤**에 P2 리뷰 코멘트가 달렸다(머지 07:17:01Z, 코멘트 07:18:32Z). 지적은 실재하는 결함이었고(E2E 하니스가 앰비언트 `AUTH_MODE=jwks` 에서 27/37 실패) 별도 후속 PR #43 으로 고쳐야 했다.
- 원인: 리뷰 잡의 **status=pass 와 코멘트 게시 완료는 별개**인데 이를 수렴 신호로 취급했다. 같은 리뷰 도구가 PR #39 에서는 4~8분 걸리며 라운드마다 코멘트를 냈는데, #41 은 57초만에 pass 로 떠 "지적 없음"으로 속단했다(테스트 전용 PR이라 빠른 게 자연스럽다고 판단).
- 규칙:
  - **머지 직전에 코멘트를 재조회한다** — `gh api repos/{owner}/{repo}/pulls/{n}/comments` 를 머지 명령 바로 앞에서 한 번 더. 체크 통과 시점의 조회 결과를 재사용하지 않는다.
  - 리뷰 잡이 평소보다 **현저히 빨리** 끝나면(이전 라운드 대비 1/5 이하) 코멘트 게시 지연을 의심하고 최소 1~2분 뒤 재확인한다.
  - 코멘트가 머지 후 도착하면 **되돌리지 말고 후속 PR**로 처리하고, 원 PR 코멘트에 후속 PR 링크로 답글을 남겨 추적성을 유지한다.
- 관련: PR #41 → #43, `tests/integration/conftest.py`

## [2026-07-20] repo 전체 `ruff format` 실행이 무관 파일 35개를 재포맷 (버전 드리프트)
- 증상: 커밋 준비 중 `uv run ruff format app tests`(dev 의존성 0.15.21)를 돌리자 이번 작업과 무관한 파일 30여 개가 재포맷돼 diff 를 오염시킴. pre-commit 훅의 ruff-pre-commit 은 v0.8.6 으로 고정돼 있어 기존 커밋들은 다른 포맷 규칙으로 들어가 있었음.
- 원인: 훅(rev v0.8.6)과 dev 의존성(ruff 0.15.21)의 버전 불일치 + CI 는 `ruff check`만 검사(format 미검사) → 저장소에 포맷 드리프트가 누적된 상태에서 전역 format 실행.
- 규칙:
  - `ruff format` 은 repo 전체가 아니라 **이번에 편집한 파일에만** 돌린다. 전역 실행 전 `git status` 로 파급 확인.
  - format 실행 후 `git status --short` 로 무관 파일 변경 여부를 반드시 검사 — 무관 재포맷은 `git restore` 로 되돌리고 관련 파일만 스테이징.
  - 포맷 드리프트 일괄 해소는 별도 `style:` 커밋/PR 로 분리(기능 PR 에 섞지 않는다). ruff-pre-commit rev ↔ dev ruff 버전 정렬도 그 PR 에서.
- 관련: `.pre-commit-config.yaml`, `pyproject.toml`, PR #34 브랜치 `feat/auth-e2e`

## [2026-07-17] 설계 문서가 구계약(v0.7.0) 기준으로 작성돼 계약과 드리프트
- 증상: 판매자 멀티에이전트 설계서 v3가 "삭제만 HITL"·"FE S-3 PATCH 반영"·자체 데이터 API(ai_reader MySQL 직접) 등 폐기된 구계약/타 아키텍처 전제를 포함한 채 완성됨. 코드 스텁 docstring(seller/spring_client)도 같은 구계약을 서술.
- 원인: api-spec 사본이 v0.9.0~v0.14.0으로 개정되는 동안(판매자 파트가 최대 변경 영역) 설계 문서는 별도 트랙에서 작성·완성됨. 스텁 docstring은 작성 시점(v0.7.0)에 고정.
- 규칙:
  - 설계/구현 착수 전 **api-spec 사본의 최신 버전 헤더와 §8 개정 항목**을 먼저 대조한다 — 특히 자기 담당 파트의 개정 이력(CHANGELOG Docs)을 훑는다.
  - 스텁 docstring의 § 번호는 신뢰하되 **서술 내용의 버전은 의심**한다(§ 위치는 유지되나 내용이 개정됐을 수 있음).
  - 외부 설계 문서를 SPEC으로 편입할 때는 **정합 조정표(설계서→확정, 근거)** 를 SPEC 앞머리에 남겨 무엇이 왜 바뀌었는지 추적 가능하게 한다.
- 관련: `docs/specs/SPEC-SELLER-001.md` §1, `docs/api-spec.md` §3.2/§4.4/§4.5, `app/services/spring_client.py`

## [2026-07-16] 파일이 엉뚱한 저장소에 생성됨 (cwd 착오)
- 증상: hk-final에 만들려던 `CLAUDE.md`·`.claude/settings.json`이 기획 repo(my-project)에 생성돼 기존 moai 설정(522줄, 훅 포함)을 덮어씀.
- 원인: Bash 작업 디렉터리가 이전 명령에서 my-project로 남아 있었는데 `cat > CLAUDE.md`를 상대경로로 실행. cwd를 확인하지 않음.
- 규칙:
  - 파일 쓰기는 **절대경로**로 (`cat > /home/nyong/projet/hk-final/CLAUDE.md`). 상대경로 금지.
  - 명령 앞에 `cd <절대경로> && pwd`로 cwd를 못 박고 시작.
  - hk-final은 워크스페이스 밖이라 Write 도구가 막힌다(path traversal) → **Bash heredoc + 절대경로**로 쓴다.
  - 덮어쓰기 전 대상 파일을 확인 — 내가 만든 게 아니면 멈추고 점검.
- 관련: `CLAUDE.md`, `.claude/settings.json`

## [2026-07-15; 정책 전환 2026-07-22] api-spec 사본이 정본과 어긋날 위험
- 증상: 계약(SSE 이벤트·오류 코드)이 코드/외부 정본/로컬 사본 세 곳에 흩어져 드리프트했다.
- 해소: 2026-07-22부터 외부 사본 의존을 폐기하고 **repo-local `docs/api-spec.md`를 정본으로 승격**했다.
- 규칙: 계약 변경은 `docs/api-spec.md`를 먼저 개정하고 코드를 같은/후속 커밋에서 맞춘다. SPEC의 낡은 외부 계약 명명도 repo-local api-spec이 우선한다.
- 관련: `docs/api-spec.md`, `docs/specs/`
# #465: 사후 ablation의 한계와 파생식 결합

- categoryQueries만 사후로 비우는 ablation은 `semantic_query_is_fallback` 파생식 결합을 보지 못해 "단독 차단 0건"이라는 오결론을 냈다. 하네스가 보는 형상과 코드가 실제로 재파생하는 상태를 구분하고, 억제형 변경은 발동률·보호 대상·해로운 발동을 산출물에 함께 남긴다.
## [2026-08-10] 문면으로 못 고친 LLM 산출 결함은 정본 데이터 기반 결정론 후처리로 고친다
- 증상: #443에서 모델은 상품군을 말한 첫 추천 발화에도 `categoryQueries`를 확률적으로 비웠다.
  프롬프트 문면 7종은 최대 +7.3%p 개선을 위해 반대 축 −10.8%p를 지불해 채택할 수 없었다.
- 원인: "상품군을 추출하라"는 지시의 준수 여부를 다시 모델에게 맡기면, 같은 모델 분산과
  반대 방향의 조건 전용 발화 오염을 함께 감수한다.
- 규칙: 판별자를 모델이 아니라 정본 데이터에서 가져올 수 있으면, 사전 기반 결정론 후처리를
  우선 검토한다. `seller_categories.json`처럼 조건어를 구조적으로 포함하지 않는 닫힌 사전이면
  condition_only 반대 방향 부작용은 측정상 우연히 0이 아니라 매칭 정의상 0이다.
- 관련: #443 · `app/agents/buyer/recommendation/category_leg_injection.py` ·
  `evals/intent_probe/baselines/fast-2026-08-10-443-{base-2,inject-1,inject-2}/`
