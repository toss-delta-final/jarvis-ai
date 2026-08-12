# Validated Grounding Production Rollout Design

## Goal

구매자 추천 graph의 production rerank grounding을 C(`validated`)로 기본 전환한다. A/B/C 평가
CLI는 비교 기준 보존을 위해 옵션 생략 시 계속 A(`current`)를 실행한다.

## Evidence

- 450-case live run에서 측정 가능한 unsupported reason은 A 10.87%, B 3.62%, C 0%였다.
- A/B 추천 집합은 비교 가능한 447/447 case에서 같았고 B/C 순위는 450/450 같았다.
- 운영 동등 추정에서 C는 A보다 요청당 API 비용 14.05%, 전체 450-case pipeline proxy 평균
  지연 11.23%가 높았다.
- C validator는 별도 LLM이 아니라 후보 tier와 reason metadata를 대조하는 로컬 결정론 코드다.

## Design

1. `Settings.rerank_grounding_arm`을 `current|prompt_only|validated` Literal로 추가하고 기본값을
   `validated`로 둔다.
2. production `stream_recommendation()`이 해당 설정을 `rerank(..., grounding_arm=...)`에 명시적으로
   전달한다.
3. 환경변수 `RERANK_GROUNDING_ARM=current`로 A에 즉시 롤백할 수 있게 한다.
4. `rerank()` 함수 자체의 기본값은 `current`로 유지한다. 평가·단위 테스트와 명시하지 않은 내부
   직접 호출의 기존 의미를 바꾸지 않고 production graph 경계에서만 rollout을 결정한다.
5. adversarial 평가 runner는 기존 patch scope에서 arm을 명시적으로 덮어쓰므로 A/B/C 비교 계약과
   CLI 기본 A는 유지한다.

## Failure behavior

구조화 metadata가 없거나 틀리면 상품을 제거하거나 순위를 바꾸지 않고 reason만 중립 템플릿으로
강등한다. 설정에 허용되지 않은 arm을 넣으면 Pydantic Settings 검증이 기동 전에 거부한다.

## Verification

- Settings 기본 C, 명시적 A rollback, 잘못된 값 거부;
- production graph가 설정 arm을 rerank에 전달;
- current 직접 호출과 A/B/C evaluation runner 계약 유지;
- grounding/adversarial/recommendation 관련 테스트와 Ruff;
- production source가 C이고 evaluation CLI default가 A임을 정적 확인한다.

## Limits

이번 전환은 추천 이유 표시 grounding만 바꾼다. 검색, eligibility, coverage, injection rank
invariance와 프론트엔드-to-SSE 실제 사용자 E2E 계측은 별도 과제다.
