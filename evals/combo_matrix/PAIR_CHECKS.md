# INV/DIR 쌍 실검증 결과 (이슈 #371)

> 생성: `evals/combo_matrix/pair_runner.py::render_pair_checks_md` — `expected/pair_checks.jsonl` 을 실행해 자동 생성된다. 손으로 고치지 말 것(드리프트 방지).

- INV 통과: 1/1
- DIR(ci) 통과: 1/1
- manual 분리: 1건
- 분모(전체 쌍 행 수): 3

## combo-0053 (DIR, mode=ci)

- **원본**: combo-0022
- **축 diff**: `category: absent→present`
- **검증 정의**: 하드필터 추가는 후보 집합을 좁히거나 유지한다(WHERE 조건 추가)
- **metric**: push_product_count · **direction**: non_increase
- **metric(base)**: 3
- **metric(perturbed)**: 2
- **guards**: {'perturbed_filters_strict_superset': True, 'base_count_positive': True}
- **base 프로젝션**: `{'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'searchFilters': {'category': None, 'priceMin': None, 'priceMax': None, 'brand': None, 'ratingMin': 4.0, 'keyword': None, 'color': None, 'attrConditions': None}, 'unappliedSearchFilters': [], 'legs': [], 'listType': 'PICK_ONE', 'listsCount': 1, 'perListProductCount': [3], 'listEntryFieldKeys': [['listId', 'productIds']], 'productIdsMultiset': [101, 102, 104], 'pushProductCount': 3}`
- **perturbed 프로젝션**: `{'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'searchFilters': {'category': '무선이어폰', 'priceMin': None, 'priceMax': None, 'brand': None, 'ratingMin': 4.0, 'keyword': None, 'color': None, 'attrConditions': None}, 'unappliedSearchFilters': [], 'legs': [('무선이어폰', None)], 'listType': 'PICK_ONE', 'listsCount': 1, 'perListProductCount': [2], 'listEntryFieldKeys': [['listId', 'productIds']], 'productIdsMultiset': [101, 102], 'pushProductCount': 2}`
- **verdict**: pass

## combo-0054 (DIR, mode=manual)

- **상태**: manual — 실행 안 함
- **사유**: recall 은 정답(relevance) 라벨이 있어야 계산된다 — 고정 fixture 검색 대역은 회원/게스트 산출이 항상 동일해 방향 관측이 원리적으로 불가. 절대 기준·표본은 goldenset 소관(expected_behavior combo-0054 aspect 와 일치)
- **소관(link)**: evals/goldenset(#333)

## combo-0055 (INV, mode=ci)

- **원본**: combo-0024
- **축 diff**: `degrade: none→rerank_failed`
- **검증 정의**: rerank 실패 degrade 에서도 push 계약 형태는 불변 — expected_behavior 의 정의 그대로. 실측 결과 base(rerank 성공)·perturbed(rerank_failed 폴백)의 push productIds 멀티셋이 동일해(둘 다 [101]) productIdsMultiset 도 invariant_fields 로 잠근다(이슈 #371 §3-a 각주). [이슈 #381 D1] keyword·color·attr_conditions 를 표현 불가로 던지던 구 RecordingFilteringSearch(ValueError)에서는 두 arm 이 모두 SEARCH_FAILED 로 삼켜져 terminal=error 인 채 우연히 pass 했다(공허 통과) — D1 로 미적용 필터를 기록만 하고 계속하게 고친 뒤에는 두 arm 다 terminal=done 으로 정상 종료해 불변식이 실제 의미로 성립한다. reasons 값(문구)·token 텍스트는 의도적으로 제외(rerank 성공/폴백이 값을 바꾸는 것은 정의된 동작) [F3, 리뷰 R1] `unappliedSearchFilters` 도 invariant_fields 에 추가했다 — `searchFilters` 에 color·attrConditions 값이 실려 있어도 그건 대역이 실제로 적용한 축이 아니라(D1) 미적용으로 기록된 값이라, 두 arm 에서 "실제로 측정된 축"이 갈리면 이 INV 는 성립하면 안 된다 — 공허하지 않음을 실측으로 확인했다(양쪽 다 ['color', 'attrConditions'], 빈 리스트끼리 비교하는 공허한 불변식이 아니다).
- **invariant_fields**: ['terminal', 'finishReason', 'errorCode', 'searchFilters', 'legs', 'listType', 'listsCount', 'listEntryFieldKeys', 'perListProductCount', 'productIdsMultiset', 'unappliedSearchFilters']
- **불일치 필드**: []
- **base 프로젝션**: `{'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'searchFilters': {'category': '무선이어폰', 'priceMin': 20000, 'priceMax': 50000, 'brand': ['나이키'], 'ratingMin': 4.0, 'keyword': None, 'color': '블랙', 'attrConditions': {'방수': 'true'}}, 'unappliedSearchFilters': ['color', 'attrConditions'], 'legs': [('무선이어폰', None)], 'listType': 'PICK_ONE', 'listsCount': 1, 'perListProductCount': [1], 'listEntryFieldKeys': [['listId', 'productIds']], 'productIdsMultiset': [101], 'pushProductCount': 1}`
- **perturbed 프로젝션**: `{'terminal': 'done', 'finishReason': 'stop', 'errorCode': None, 'searchFilters': {'category': '무선이어폰', 'priceMin': 20000, 'priceMax': 50000, 'brand': ['나이키'], 'ratingMin': 4.0, 'keyword': None, 'color': '블랙', 'attrConditions': {'방수': 'true'}}, 'unappliedSearchFilters': ['color', 'attrConditions'], 'legs': [('무선이어폰', None)], 'listType': 'PICK_ONE', 'listsCount': 1, 'perListProductCount': [1], 'listEntryFieldKeys': [['listId', 'productIds']], 'productIdsMultiset': [101], 'pushProductCount': 1}`
- **verdict**: pass
