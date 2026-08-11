# AI persona review 참고자료 (#153)

이 문서는 #153 평가 패키지의 수집 전 준비도를 점검한 **AI persona 기반 정성 참고자료**다.
이는 사람 evaluator response가 아니며, 실제 A/B pair 또는 assignment를 평가해 산출한
추천 승패·선호 count·LLM judge 결과가 아니다. 따라서 raw human JSONL이나 유효한
`llm-judge.jsonl`을 생성하지 않는다.

현재 수집 상태는 계속 `HUMAN_INPUT_REQUIRED`다. 사람 입력 acceptance에는 실제 적격 human
evaluator 최소 5명, buyer pair 최소 20개, 각 pair당 서로 다른 evaluator 3명, 총 60개
assignment에 대한 60개 독립 human response가 필요하다. 이 문서는 그 조건을 충족하거나
대체하지 않는다.

## Persona별 준비도

| persona | readiness | 핵심 판단 |
|---|---:|---|
| Product | 3/5 | buyer-only 범위와 exploratory caveat는 명확하지만, 실제 추천 품질의 제품 의사결정에는 사람 응답과 사전 등록된 pair가 더 필요하다. |
| ML evaluation | 4/5 | seed 고정, constrained A/B balance, rubric, 분모, agreement, provenance 설계가 구체적이다. 실제 crossed human data가 없어 통계 결과는 아직 산출할 수 없다. |
| Trust/Safety | 3/5 | PII 최소화, 동의, abstain, retention/deletion, algorithm identity 비공개 원칙은 준비되어 있으나 실제 collection gate 운영 증거는 아직 없다. |
| UX Research | 3/5 | relevance/fit·explainability·trustworthiness rubric과 tie/abstain 안내는 준비되어 있으나 evaluator comprehension과 실제 인터뷰/파일럿 검증이 없다. |
| Data Ops/QA | 3/5 | assignment routing, hash provenance, schema validation, completion gate는 점검 가능하지만 실제 response ingestion과 삭제 운영은 미실행이다. |

종합 준비도는 **3.2/5**다. 이는 설계·운영 준비도에 대한 AI 참고 의견이며, baseline과
recommendation-v2 중 어느 쪽이 우월한지에 대한 판단이 아니다.

## 공통 강점

- buyer-only 범위, #152/#154와의 분리, exploratory/non-generalizable caveat가 명시되어 있다.
- 수집 전 seed·algorithm·preregistration 및 pair-input provenance를 고정하고 재현할 수 있다.
- evaluator별 blind artifact, PII/algorithm disclosure guard, consent·retention·deletion 규칙이 있다.
- relevance/fit, explainability, trustworthiness의 1–5 rubric과 tie/abstain/disagreement 보존이 있다.
- 실제 human input이 없을 때 성공을 가장하지 않고 `HUMAN_INPUT_REQUIRED`로 남긴다.

## 공통 보완사항

- 실제 eligible human evaluator를 모집하고 consent/eligibility 및 독립 평가 절차를 운영해야 한다.
- 20개 이상의 실제 buyer pair와 pair당 3개의 독립 human response를 수집해야 한다.
- 실제 수집 전에 evaluator가 A/B 비식별 presentation과 rubric을 이해하는지 파일럿으로 확인해야 한다.
- raw response 삭제·철회·quarantine 절차를 실제 저장소와 담당자 흐름에서 검증해야 한다.
- 실제 A/B pair와 assignment가 생긴 뒤에만 judge artifact schema를 적용하고, 그때에만 선택적
  LLM judge 비교를 수행할 수 있다.

## 산출물 경계

이 검토는 AI가 작성한 준비도 의견일 뿐이다. 사람 응답으로 저장하거나 `responseOrigin=human`
으로 변환하지 않으며, pairwise preference/승패/신뢰도 구간/합의도/혼동행렬을 만들지 않는다.
실제 A/B pair·assignment가 없으므로 이 문서만으로 유효한 `llm-judge.jsonl`을 만들 수 없다.
