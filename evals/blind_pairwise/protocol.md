# 운영 프로토콜: 사람 입력 전 고정사항

이 문서는 평가 시작 전에 coordinator와 evaluator가 읽는 buyer-only 절차다. 평가자에게
`protocol.md` 전체를 보여줄 필요는 없지만, 아래 rubric과 제출 규칙은 동일해야 한다.

## Eligibility

적격 evaluator는 (1) 현지 법률상 동의 가능한 성인 또는 승인된 연구 절차의 참여자이고,
(2) buyer 요청을 읽고 지정 언어로 답할 수 있으며, (3) baseline/recommendation-v2 출력의
생성·튜닝·승인에 관여하지 않았고, (4) 같은 case의 정답이나 모델 버전을 사전에 보지 않았고,
(5) seller 측 품질 평가자가 아닌 사람이다. 개인별 선정 사유나 인구통계는 raw artifact에
기록하지 않고 `eval-01` 같은 alias와 eligibility/consent 확인 여부만 별도 접근 제한 목록에
둔다. 최소 5명의 alias와 pair당 3명의 서로 다른 alias를 assignment 생성 전에 고정한다. 각
alias는 자기에게 routing된 `evaluator-set-*.json`만 받으며, 모든 evaluator 배정을 합친 공개
파일은 만들지 않는다.

## Consent

제출 화면은 다음을 명시한다: 참여는 자발적이며 보상/불이익 조건, 예상 소요시간, 평가 목적,
A/B가 알고리즘 version을 숨긴다는 사실, 언제든 제출 전 중단할 수 있다는 점, 결과가 비식별
탐색 자료로만 쓰인다는 점, 연락처·계정·주문번호를 입력하지 말아야 한다는 점, 보존기간과
삭제 요청 방법. `consent=true`를 확인한 뒤에만 response를 저장한다. 동의 철회 요청이 오면
해당 evaluator alias의 raw rows를 원본과 파생 report에서 삭제하고 삭제 시각을 기록한다.

## PII minimization, retention, deletion

- evaluator에는 임의 alias만 전달한다. 이름, email, 전화, IP, user-agent, 계정/주문 id를
  수집하거나 raw response에 저장하지 않는다.
- pair ID, prompt, output A/B 각각을 coordinator가 입력 전에 비식별화하고 모델명·version명,
  baseline/recommendation-v2/algorithm identity도 제거한다. 수집 gate는 이 네 문자열의
  명백한 PII와 disclosure를 모두 거부한다.
- free-text comment는 받지 않고 controlled `disagreementTags`만 허용한다. 원시 내용에 PII가
  의심되면 그 행을 분석하지 말고 quarantine 후 폐기한다.
- raw responses와 evaluator consent lookup은 암호화된 접근 제한 저장소에 보관한다. raw는
  최종 exploratory report 승인 후 90일까지만 보존하고, 동의 철회/삭제 요청은 확인 후 30일
  이내 raw·파생 export·lookup에서 삭제한다. 삭제된 행은 분석 분모에서 제외하며 0으로 채우지
  않는다.
- assignment artifact는 raw를 복원하는 내부 mapping을 가지므로 evaluator에게 공개하지 않는다.
  공개 presentation에는 mapping/seed/algorithm/evaluator alias가 없다. 보존 만료 때 assignment와
  공개 presentation도 함께 폐기한다.

## Evaluator instructions

1. 각 case의 buyer 요청과 A/B 두 추천을 모두 읽는다.
2. A와 B 어느 쪽이 요청에 더 잘 맞는지 고르고, 의미 있는 차이가 없으면 `tie`를 고른다.
3. 정보가 잘렸거나 안전/정책 문제로 판단할 수 없으면 `abstain`을 고른다. 모르는 것을
   추측해서 승패를 만들지 않는다.
4. relevance/fit, explainability, trustworthiness를 각각 A와 B에 1–5로 독립 채점한다.
5. 이미 본 case를 다시 평가하거나 다른 evaluator와 상의하지 않는다. 한 assignment는 한 번만
   제출한다.

### Rubric and examples

모든 축에서 1은 매우 나쁨, 3은 혼합/중간, 5는 매우 좋음이다. 점수는 선호를 자동으로
결정하지 않는다. 예를 들어 한 출력이 더 관련 있어도 이유가 부정확하면 explainability와
trustworthiness는 낮게 줄 수 있다.

| dimension | 1 | 3 | 5 |
|---|---|---|---|
| relevance/fit | buyer 요청과 무관하거나 핵심 제약을 위반 | 일부 요구에 맞지만 중요한 조건이 빠짐 | 요청·예산·제약에 직접 맞고 불필요한 추천이 없음 |
| explainability | 이유가 없거나 결론만 반복 | 이유가 있으나 일반적이고 어떤 후보에 적용되는지 불명확 | 후보의 관찰 가능한 속성을 buyer 요청과 연결해 짧고 구체적으로 설명 |
| trustworthiness | 근거 없는 사실·과장·모순이 있음 | 대체로 plausible하나 확인되지 않은 표현이 남음 | 출력에 보이는 근거만 사용하고 불확실성을 과장하지 않음 |

예시: “검정 출근용 운동화, 10만원 이하” 요청에 검정·가격을 확인할 수 있는 후보를
제시하고 이유를 연결하면 relevance 5/explainability 5에 가깝다. 후보가 검정인지 보이지
않는데 “검정이라서 최적”이라고 단정하면 trustworthiness를 낮춘다. 두 출력이 같은 수준이면
tie다. 어느 출력도 query를 충족하지 않거나 출력이 손상된 경우에는 tie로 억지 판정하지 말고
abstain한다.

## Fixed analysis plan and reproducibility

수집 전 `preregistration.json`의 pair 수·rating 수·evaluator 수·seed·rubric·분모·CI·alpha
규칙을 hash한다. assign 명령은 exact preregistration과 pair-input artifact를 읽고 두 hash를
coordinator manifest에 고정한다. randomization algorithm은
`sha256-seeded-constrained-balanced-left-right-v2`이며, A/B 방향은 seed-dependent하지만
전체와 evaluator별 좌우 수가 최대 한 건 차이 나도록 constrained balance를 사용한다. ordinal alpha는
`pooled-marginal-cumulative` Krippendorff distance를 사용한다. 수집 후에는 raw JSONL, coordinator
assignment JSON, preregistration hash를 함께 보관한다. 분석은 manifest의 preregistration hash와
고정 값(seed, pair count, 3 ratings, 5 evaluators, algorithm, confidence=0.95)을 재검증하며,
결과에는 각 입력의 SHA-256을 남긴다.

```python
from pathlib import Path
from evals.blind_pairwise.analysis import reproduce_analysis

result = reproduce_analysis(
    Path("raw-responses.jsonl"),
    Path("assignments.json"),
    preregistration_path=Path("evals/blind_pairwise/preregistration.json"),
    # llm_judge_path=Path("llm-judge.jsonl"),  # 실제 judge artifact가 있을 때만
)
```

실제 사람 응답이 없는 동안 `raw-responses.jsonl`을 만들기 위해 LLM, agent, fixture, 임의
응답을 사용하지 않는다. 결과가 비어 있거나 assignment가 덜 채워지면 JSON/report는
`HUMAN_INPUT_REQUIRED` 상태로 남긴다. judge artifact가 없으면 judge confusion matrix section도
생성하지 않는다.
