# 미정의 셀 목록 (이슈 #335)

> 생성: `evals/combo_matrix/report.py::render_undefined_cells` — `expected_behavior.jsonl` 에서 자동 생성된다. 손으로 고치지 말 것(드리프트 방지).

이 표의 각 행은 **후속 스펙 이슈로 바로 쓸 수 있는 형식**이다 — 셀 좌표(축값 조합) ·
대표 발화 예시 · 현행 동작 관측(있으면) · 근거 부재 지점 · tracking.

총 5개 셀(케이스 7건에서 발견).

## `buy_all=true`, `constraint_strength=unspecified`, `total_budget=present`

- **케이스**: combo-0039
- **대표 발화**: 아무거나 추천해줘 (총 5만원 안에서) (다 사줘)
- **상태**: partial
- **세부(aspect, 좌표 아님)**: zero_result_relaxation_and_clarify
- **현행 동작(정의된 부분)**: 무지정 판정(prior is None 등 5조건)은 total_budget/buy_all 을 보지 않아 그대로 성립한다. total_budget 이 있으면 취향 벡터 경로가 막히고 인기 상품(I-3)이 예산 이하로 걸러진다. buy_all 단독은 leg 이 없어 무동작(BUY_ALL 세트 미생성) — push 는 PICK_ONE 로 고정된다.
- **관측**(combo-0039): `{'actionReason': None, 'actionType': None, 'errorCode': None, 'eventTypes': ['conditions', 'token', 'token', 'products.ready', 'done'], 'finishReason': 'stop', 'lastTokenText': '50,000원 안에서 인기 있는 상품으로 골라봤어요. "무선 이어폰"처럼 어떤 상품을 찾으시는지 알려주시면 더 잘 추천해드릴 수 있어요.', 'listType': 'PICK_ONE', 'pushCount': 1, 'terminal': 'done'}`
- **근거(있는 부분)**: code:app/agents/buyer/recommendation/no_condition.py:128-131; code:app/agents/buyer/recommendation/no_condition.py:141-149; code:app/agents/buyer/recommendation/graph.py:722-731; code:app/agents/buyer/recommendation/graph.py:812-823; code:app/agents/buyer/recommendation/graph.py:1888-1894
- **tracking**: in_progress(#336)

## `degrade=embedding_missing`, `surface=HOME`

- **케이스**: combo-0050
- **대표 발화**: (HOME 지면 — 발화 없음, I-22 콜백)
- **상태**: undefined
- **관측**(combo-0050): `{'note': 'HOME(I-22) 엔 대응 코드 경로 없음 — 실행 생략(expected_behavior.status=undefined)'}`
- **tracking**: 미배정 — 후속 스펙 이슈 필요

## `degrade=rerank_failed`, `surface=HOME`

- **케이스**: combo-0052
- **대표 발화**: (HOME 지면 — 발화 없음, I-22 콜백)
- **상태**: undefined
- **관측**(combo-0052): `{'note': 'HOME(I-22) 엔 대응 코드 경로 없음 — 실행 생략(expected_behavior.status=undefined)'}`
- **tracking**: 미배정 — 후속 스펙 이슈 필요

## `degrade=spring_timeout`, `intent=wishlist_add`

- **케이스**: combo-0044, combo-0045, combo-0057
- **대표 발화**: 이거 찜해줘
- **상태**: partial
- **현행 동작(정의된 부분)**: SpringUnavailableError 가 stream_wishlist_add 안에서 개별 처리되지 않고 상위 스트림 pump 의 범용 catch-all 로 새어나가 error(INTERNAL) 로 종료된다 — 계약은 지켜지지만(스트림이 죽지 않는다) cart_add/cart_remove/cart_view/wishlist_remove 처럼 SpringUnavailableError 전용 처리·문구는 없다.
- **관측**(combo-0044): `{'actionReason': None, 'actionType': None, 'errorCode': None, 'eventTypes': ['token', 'done'], 'finishReason': 'stop', 'lastTokenText': '찜에는 로그인이 필요해요.', 'listType': None, 'notes': ['identity=guest 는 로그인 필요 게이트가 Spring 호출보다 먼저 걸려 이 케이스는 SpringUnavailableError 미처리 갭(expected_behavior.status=partial 근거)을 실제로는 밟지 않는다 — 그 갭은 identity=member 조합에서만 실측된다(README 리스크 참조).', '웜업으로 last_reco 채움(productId 101) — context 축은 decompose 입력 관점(none)이고 세션 스토어 상태와는 별개다(runner.py::_warm_up_last_reco).'], 'pushCount': 0, 'terminal': 'done'}`
- **관측**(combo-0057): `{'note': 'stream_wishlist_add 가 이 예외를 개별 처리하지 않아 run_buyer_turn 을 직접 호출한 이 경계(unit)에서 그대로 전파됐다 — 프로덕션에서는 core/stream.py 의 open_stream 범용 catch-all이 error(INTERNAL) SSE 로 감싼다(통합 레벨, 이 하네스 범위 밖). 이 unhandledException 자체가 발견 3번의 직접 증거다.', 'unhandledException': 'SpringUnavailableError'}`
- **근거(있는 부분)**: code:app/agents/buyer/graph.py:1095-1107; code:app/core/stream.py:688-705
- **tracking**: 미배정 — 후속 스펙 이슈 필요

## `degrade=spring_timeout`, `surface=HOME`

- **케이스**: combo-0053
- **대표 발화**: (HOME 지면 — 발화 없음, I-22 콜백)
- **상태**: undefined
- **관측**(combo-0053): `{'exception': 'UpstreamTimeout', 'statusCode': 504}`
- **tracking**: 미배정 — 후속 스펙 이슈 필요
