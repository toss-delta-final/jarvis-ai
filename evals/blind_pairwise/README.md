# Blind pairwise buyer evaluation (#153)

이 디렉터리는 **구매자 추천 품질**의 baseline과 recommendation-v2를 비교하기 위한
사람 평가 자산이다. 선택적 evidence이며 #152·#154와 병행한다. seller 평가, production
A/B test, 합성 evaluator response, 모집단 우월성 주장은 이 패키지의 범위가 아니다.

## 현재 상태

이 PR은 사람 입력 전 준비 단계만 제공한다. 커밋된 raw human response는 없으며,
`preregistration.json`에는 응답 데이터가 들어 있지 않다. 실제 최소 완료 조건은 다음을
모두 사람 응답으로 충족하는 것이다.

- 서로 다른 buyer case **20개 이상**
- pair마다 서로 다른 적격 human evaluator **3명**의 독립 응답
- 적격 evaluator **5명 이상**

따라서 이 패키지를 설치하거나 테스트한 것만으로 #153의 사람 응답 acceptance를 충족했다고
말하지 않는다. 결과 산출물은 `minimumPlanSatisfied`, `humanInputComplete`, `status`를 별도로
표시한다. 실제 행이 없거나 필수 배정이 덜 채워지면 `status`는
`HUMAN_INPUT_REQUIRED`이고, 이 패키지는 fixture·합성 응답을 만들지 않는다.

수집 전 AI persona 준비도 참고자료는 [`synthetic_persona_review.md`](synthetic_persona_review.md)에
있다. 이 문서는 사람 응답이나 pairwise 승패가 아니며, 실제 A/B pair/assignment가 없으므로
유효한 `llm-judge.jsonl`도 만들지 않는다.

## 수집 전 고정 항목

`preregistration.json`을 수집 전에 검토하고 hash를 보관한다. 변경은 응답 수집 후 분석 계획을
바꾸는 방식으로 하지 않는다.

- buyer-only scope, 20 pair, pair당 독립 3 rating, 최소 5 eligible evaluator
- seed `20260811`
- `sha256-seeded-constrained-balanced-left-right-v2` 결정론 배정/A-B 방향 알고리즘
  (전체 및 evaluator별 좌우 노출 차이 최대 1을 seed로 고정)
- 평가자에게는 알고리즘 이름·버전과 내부 variant 매핑을 보여주지 않음
- rubric 세 축: `relevance_fit`, `explainability`, `trustworthiness` (각 1–5 ordinal)
- raw preference는 `A`, `B`, `tie`, `abstain` 중 하나로 보존
- preference 비율의 Wilson 95% interval, 차원별 ordinal distribution, Krippendorff alpha
- Wilson interval은 crossed pair/evaluator dependence를 반영하지 않는 **descriptive
  conditional response-level interval**이며 모집단 coverage나 superiority의 근거가 아님
- preference alpha에서는 `abstain`을 missing/cannot-judge로 취급하지만 abstain count는 보존
- LLM judge 결과는 실제 judge artifact가 제공된 경우에만 계산

## 입력과 artifact 경계

운영자는 실제 baseline/recommendation-v2 실행 결과를 별도 보안 위치에서 `pairs.jsonl`로
준비한다. 한 줄의 입력 모양은 다음과 같으며 이것은 **human response가 아니다**.

```json
{"pairId":"pair-01","prompt":"비식별화한 buyer 요청","baselineText":"출력 A","recommendationV2Text":"출력 B"}
```

`pairId`, prompt, 두 출력 **모두** 평가자에게 보내기 전에 이름·연락처·주문번호·계정 id·모델명·
버전명 등 PII와 algorithm disclosure를 제거해야 한다. `PairInput`은 세 문자열과 opaque pair ID
각각에서 명백한 이메일/전화번호·식별자·variant/algorithm/version disclosure를 발견하면 생성을
거부한다. 평가자-facing 문자열에는 `baseline`, `recommendation-v2`, 모델/알고리즘/version
identity를 쓰지 않는다.

coordinator artifact는 다음을 포함한다.

- seed와 randomization algorithm
- evaluator alias와 pair별 내부 A/B 매핑
- 분석에 필요한 실제 두 출력

이 파일은 평가자에게 배포하지 않는다. 공개 presentation은 evaluator마다 별도
`evaluator-set-*.json` 하나씩 생성하며, 각 파일에는 그 evaluator에게 배정된 case만 있다.
공개 presentation은 `A`/`B` 텍스트와 opaque assignment/pair ID만 포함하고 seed, algorithm,
evaluator id, `leftVariant`/`rightVariant`를 포함하지 않는다. coordinator artifact의
`routing`만 evaluator alias와 파일/assignment를 연결한다. A/B 방향은 seed로 결정하되 전체와
evaluator별 좌우 노출 수가 각각 최대 한 건만 차이 나도록 균형화한다.

## 명령

```bash
uv run python -m evals.blind_pairwise assign \
  --pairs /secure/pairs.jsonl \
  --evaluators eval-01,eval-02,eval-03,eval-04,eval-05 \
  --seed 20260811 \
  --preregistration evals/blind_pairwise/preregistration.json \
  --coordinator-out /secure/assignments.json \
  --public-dir /secure/presentations/
```

실제 assign 명령은 committed preregistration의 seed·pair count·ratings per pair·최소
evaluator 수·algorithm·confidence=0.95와 입력 파일 hash를 검증한다. `--public-out`처럼 모든
evaluator 배정을 한 파일로 쓰는 경로는 허용하지 않는다. coordinator JSON에는 exact pair-input
SHA-256, preregistration SHA-256과 routing만 보관한다.

사람이 제출한 raw JSONL을 분석할 때만 다음 명령을 실행한다. raw 파일이 없으면 성공을
가장하는 fixture를 만들지 않는다.

```bash
uv run python -m evals.blind_pairwise analyze \
  --raw /secure/raw-responses.jsonl \
  --assignments /secure/assignments.json \
  --preregistration evals/blind_pairwise/preregistration.json \
  --out /secure/analysis.json
```

실제 judge artifact가 있을 때만 위 명령에 `--judge /secure/llm-judge.jsonl`을 추가한다. 파일이
없으면 해당 옵션을 생략한다.

`analysis.json`은 raw/assignment/pair-input/preregistration SHA-256 provenance, preference counts,
각 분모, Wilson 95% interval, ordinal counts와 분모, agreement, disagreement examples,
관측 evaluator alias 수와 pair별 response completeness, caveat를 보존한다. preregistration
hash가 coordinator artifact와 일치하지 않으면 분석을 거부한다. `report.md`는 같은 위치에
생성된다. judge를 전달한 경우에만 report에 human × judge confusion matrix와 agreement/CI가
추가되며, judge가 없으면 그 section을 만들지 않는다. `reproduce_analysis()`는 raw artifact와
coordinator manifest를 다시 검증해 같은 분석을 재생성한다.

## Raw response schema

raw 행은 `schema.py`의 `RawResponse`로 검증한다. evaluator가 보는 정보에 맞춰 preference와
dimension score는 A/B로만 기록한다.

```json
{
  "schemaVersion":"blind-pairwise-response-v1",
  "responseId":"resp-opaque-01",
  "assignmentId":"asgn-pair-01-01",
  "pairId":"pair-01",
  "evaluatorId":"eval-01",
  "responseOrigin":"human",
  "consent":true,
  "preference":"A",
  "dimensionScores":{
    "relevance_fit":{"A":4,"B":3},
    "explainability":{"A":4,"B":3},
    "trustworthiness":{"A":5,"B":3}
  },
  "disagreementTags":[],
  "submittedAt":"2026-08-11T00:00:00+00:00"
}
```

`responseOrigin`은 `human`만 허용하고 `consent`가 true가 아니면 거부한다. evaluator id는
`eval-*` pseudonym만 허용한다. raw schema에는 이름, email, 전화번호, 자유서술 feedback,
IP, user-agent, 계정 id 필드를 넣지 않는다. tie와 abstain은 값 그대로 보존하며 대체 승패로
변환하지 않는다. abstain은 dimension score를 모두 `null`로 제출할 수 있다.

## 분석 규칙

선호 결과는 다음 분모를 항상 함께 출력한다.

- `responses`: 모든 검증된 human response 수
- `nonAbstain`: abstain을 제외한 응답 수; tie 포함
- `decisive`: baseline 또는 recommendation-v2를 고른 응답 수; tie/abstain 제외

baseline/recommendation-v2 preference interval은 decisive 분모, tie interval은 non-abstain
분모, abstain interval은 전체 responses 분모의 Wilson 95% interval이다. 이 interval은
descriptive conditional response-level 추정량이며 crossed pair/evaluator dependence를
보정하지 않고 모집단 coverage나 population superiority를 주장하지 않는다. dimension score는
각 variant의 1–5 count와 유효 score 분모를 별도로 출력한다. 결측 score를 0으로 채우거나
평균으로 대체하지 않는다.

agreement는 preference에 nominal Krippendorff alpha, rubric score에 ordinal Krippendorff
alpha를 적용한다. ordinal alpha는 pooled category marginal의 cumulative ordinal distance
(`pooled-marginal-cumulative`)를 사용하며 numeric squared interval alpha를 ordinal이라고
부르지 않는다. preference alpha는
`abstain-as-missing` 정책이고 count/분모에는 abstain을 그대로 보존한다. pair 안에서 1개
관측만 있는 경우 alpha는 `null`이며, 이를 0 또는 1로 가장하지 않는다. 불일치 예시는 pair
단위 preference 분포와 variant로 역매핑한 dimension score 범위만 포함하고 evaluator id나
자유 텍스트를 포함하지 않는다.

coverage의 `humanInputComplete`는 planned pair마다 정확히 서로 다른 3명의 응답이 assignment와
일대일로 모두 들어오고, 실제 관측 evaluator alias가 5명 이상일 때만 true다. 그 전에는
`status=HUMAN_INPUT_REQUIRED`로 남는다. report에는 전체 관측 evaluator 수와 모든 pair별
expected/observed/distinct evaluator/complete 표가 함께 나온다.

LLM judge 비교는 judge artifact가 실제로 전달된 경우에만 `llmJudge` 섹션을 추가한다.
그때 human preference × judge preference confusion matrix, agreement 분자/분모, Wilson
95% interval을 계산한다. judge artifact가 없으면 해당 섹션을 만들지 않는다.

## 한계

이 설계는 buyer 추천 출력의 작은 탐색적 표본을 비교한다. evaluator 모집 방식과 20 pair의
case 구성 때문에 결과는 exploratory이고 일반화 가능하지 않다. 결과로 모집단 전체의 우월성,
온라인 CTR/전환, production A/B 승자를 주장하지 않는다. 실제 human rows가 생기기 전에는
분석 결과를 만들기 위해 합성 응답이나 LLM 응답을 사람 응답으로 세지 않는다.
