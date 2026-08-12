# Adversarial Grounding Arms Design

## Goal

PR #638의 210 family/450 case 구매자 adversarial 데이터셋을 기존 rerank grounding
실험군 A(`current`), B(`prompt_only`), C(`validated`)에 연결한다. Production 구매자 그래프의
기본값은 `current`로 유지하고, arm 선택과 결과 결합은 평가 패키지 안에서만 수행한다.

## Registered behavior

- 기존 명령은 `--arms current`와 동일하며 한 case당 결과 한 건을 만든다.
- `--arms all`은 `current,prompt_only,validated`를 canonical 순서로 실행한다.
- A는 현행 자유문장 prompt를 호출한다.
- B는 구조화 prompt를 한 번 호출하고 model rationale을 표시한다.
- 요청된 첫 실제 arm의 decompose 결정을 뒤 arm이 재사용해 A↔B 검색 후보 차이가 fast-tier
  샘플링에 의해 흔들리지 않게 한다.
- C는 B의 같은 `RerankResult.grounding_decisions`를 재사용해 표시 rationale만
  `rendered_rationale`로 바꾼다. C를 위한 두 번째 provider 호출은 하지 않는다.
- B와 C는 후보 ID, 순위, 검색 결과, decompose 결과가 완전히 같아야 한다.
- C 파생 결과에는 `derivedFromArm="prompt_only"`를 기록하고 `providerCalls`를 비워 비용을
  중복 계산하지 않는다.

## Components and data flow

1. CLI가 `--arms current|prompt_only|validated|all`을 파싱한다.
2. `AdversarialBuyerRunner`가 선택 arm을 보관한다.
3. 첫 실제 arm이 만든 `RouteDecision`을 case별로 보관하고 뒤 arm에 깊은 복사로 전달한다.
4. runner의 기존 평가 전용 patch scope에서 구매자 그래프의 `rerank` 호출을 감싸
   `grounding_arm`을 주입한다. Production 호출부는 수정하지 않는다.
5. wrapper가 `RerankResult.grounding_decisions`를 execution artifact에 직렬화한다.
6. B와 C가 함께 요청되면 B execution을 깊은 복사하고 `reasons`, `pushBody`, 기록된 I-21
   request body의 reason을 검증 템플릿으로 치환해 C를 만든다.
7. 각 arm을 기존 scorer로 독립 채점한 뒤 하나의 `results.jsonl`에 arm을 포함해 기록한다.
8. summary/report/manifest에는 요청 arms와 arm별 verdict/hard-failure 집계를 추가한다.

## Error handling and compatibility

- 알 수 없는 arm, 빈 arm 목록, 중복 arm은 CLI 입력 오류(exit 2)다.
- C 단독 실행은 `validated`를 직접 호출한다. B와 C가 함께 있을 때만 동일 응답 파생 규약을 쓴다.
- B에 grounding decision이 없는 항목은 C에서도 원래 reason을 보존한다. rerank 밖에서 보충된
  상품을 새 근거로 꾸미지 않는다.
- 기존 artifact 필드는 삭제하지 않는다. 단일 기본 실행의 `caseCount`, case ID 목록과 파일
  구성은 그대로다.
- scripted 구조화 arm은 `NO_VERIFIABLE_EVIDENCE`를 내어 배선만 검증하며 실제 품질 근거로
  해석하지 않는다.

## Verification

- arm 파싱 RED/GREEN 단위 테스트;
- runner가 A/B/C를 실제 `rerank`에 주입하고 decision을 포착하는 테스트;
- B execution에서 C를 만들 때 ID/순위 동일, reason만 치환, provider call 0을 검증하는 테스트;
- `--arms all` 한 case smoke에서 결과 3건과 arm별 manifest/summary를 검증;
- #638 데이터 validator/generator, 관련 eval tests, grounding tests, Ruff를 실행한다.

## Limits

이 연결은 #638의 자동 oracle과 기존 grounding detector를 한 artifact에 모을 기반이다. 검색
관련성이나 임의 자연어 진실성을 새로 자동 판정하지 않으며, production 기본 arm도 바꾸지 않는다.
