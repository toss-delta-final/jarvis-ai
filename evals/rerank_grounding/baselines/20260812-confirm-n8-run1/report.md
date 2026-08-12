# Buyer rerank grounding experiment

status=supported · commit=90b72e6474913649f8a020d2924b3b4ec57c31e0 · dataset=rerank-grounding-v1:aa86ab23c1d5 · N=8 · dryRun=False

## Primary and guardrails

| arm | unsupported 분자/분모 | rate | out-of-candidate | duplicate | invalid after validation | coverage |
|---|---:|---:|---:|---:|---:|---:|
| `current` | 16/205 | 0.07804878048780488 | 0 | 0 | 0 | 0.9490740740740741 |
| `prompt_only` | 0/207 | 0.0 | 0 | 0 | 0 | 0.9583333333333334 |
| `validated` | 0/212 | 0.0 | 0 | 0 | 0 | 0.9814814814814815 |

## Operational

- failures: 0
- unfilled cells: 0
- model: {'provider': 'openai', 'fastModel': 'gpt-5-nano', 'smartModel': 'gpt-5.6-luna', 'fastReasoningEffort': 'minimal', 'smartReasoningEffort': 'medium', 'timeoutS': 30.0, 'maxRetries': 0}
- budget: {'callCount': 240, 'totalTokens': 215209, 'totalCostUsd': 0.10068279999999992, 'unknownTokenCallCount': 0, 'unknownCostCallCount': 0, 'tokenGateStatus': 'passed', 'costGateStatus': 'passed', 'budgetExceeded': False, 'budgetExceededReason': None, 'limits': {'maxCalls': 720, 'maxTotalTokens': 30000000, 'maxCostUsd': 20.0}}

## Limits

- rating/review/relative-price와 정확한 숫자 주장만 자동 판정한다.
- 검색 관련성이나 전체 자연어 진실성을 증명하지 않는다.
- dry-run은 실행기 검증이며 live 품질 근거가 아니다.

Frozen C1~C4 release claims are unchanged; this report is exploratory appendix evidence.
