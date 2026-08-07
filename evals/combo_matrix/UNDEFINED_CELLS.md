# 미정의 셀 목록 (이슈 #335)

> 생성: `evals/combo_matrix/report.py::render_undefined_cells` — `expected_behavior.jsonl` 에서 자동 생성된다. 손으로 고치지 말 것(드리프트 방지).

이 표의 각 행은 **후속 스펙 이슈로 바로 쓸 수 있는 형식**이다 — 셀 좌표(축값 조합) ·
대표 발화 예시 · 현행 동작 관측(있으면) · 근거 부재 지점 · tracking.

총 1개 셀(케이스 2건에서 발견).

## `buy_all=true`, `constraint_strength=unspecified`, `total_budget=present`

- **케이스**: combo-0037, combo-0039
- **대표 발화**: 아무거나 추천해줘 (총 5만원 안에서) (다 사줘)
- **상태**: partial
- **세부(aspect, 좌표 아님)**: zero_result_relaxation_and_clarify
- **현행 동작(정의된 부분)**: 무지정 판정(prior is None 등 5조건)은 total_budget/buy_all 을 보지 않아 그대로 성립한다. total_budget 이 있으면 취향 벡터 경로가 막히고 인기 상품(I-3)이 예산 이하로 걸러진다. buy_all 단독은 leg 이 없어 무동작(BUY_ALL 세트 미생성) — push 는 PICK_ONE 로 고정된다.
- **관측**(combo-0037): `{'eventTypes': ['progress', 'conditions', 'progress', 'progress', 'token', 'token', 'progress', 'products.ready', 'done'], 'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'actionType': None, 'actionReason': None, 'lastTokenText': '50,000원 안에서 인기 있는 상품으로 골라봤어요. "무선 이어폰"처럼 어떤 상품을 찾으시는지 알려주시면 더 잘 추천해드릴 수 있어요.', 'pushCount': 1, 'listType': 'PICK_ONE', 'pushProductCount': 2, 'searchCallCount': 0, 'searchFilters': None, 'unappliedSearchFilters': []}`
- **관측**(combo-0039): `{'eventTypes': ['progress', 'conditions', 'progress', 'progress', 'token', 'token', 'progress', 'products.ready', 'done'], 'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'actionType': None, 'actionReason': None, 'lastTokenText': '50,000원 안에서 인기 있는 상품으로 골라봤어요. "무선 이어폰"처럼 어떤 상품을 찾으시는지 알려주시면 더 잘 추천해드릴 수 있어요.', 'pushCount': 1, 'listType': 'PICK_ONE', 'pushProductCount': 2, 'searchCallCount': 0, 'searchFilters': None, 'unappliedSearchFilters': [], 'notes': ['조건 없는 턴은 후보 소스가 popular_fn(I-3) 이라(graph.py:797-830) 이 하네스의 popular_fn fake 가 항상 성공해 search/rerank degrade 축은 관측되지 않는다 — popular_fn 실패 시에만 _run_search() 로 폴백한다(코드 정의 동작, 갭 아님).']}`
- **근거(있는 부분)**: code:app/agents/buyer/recommendation/no_condition.py:128-131; code:app/agents/buyer/recommendation/no_condition.py:141-149; code:app/agents/buyer/recommendation/graph.py:722-731; code:app/agents/buyer/recommendation/graph.py:812-823; code:app/agents/buyer/recommendation/graph.py:1888-1894
- **tracking**: in_progress(#336)
