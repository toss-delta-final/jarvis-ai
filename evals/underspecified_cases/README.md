# 과소지정 발화 판정 케이스 (#336)

`evals/README.md`(#328 공통 규약)를 따른다 — `caseId` 척추·MFT/INV 표기·결정론 CI.

`cases.json` 은 `is_underspecified_turn`(`app/agents/buyer/recommendation/underspecified.py`)
판정의 앵커다. 각 항목:

- `caseId`: goldenset 규약(`buy-under-NNNN`).
- `utterance`: 참고용 원문 발화 — **판정 입력이 아니다**(아래 참조).
- `decomposeFixture`: `RouteDecision` 생성자 kwargs(부분집합). `filters` 키는
  `ProductSearchFilters` kwargs 로 중첩한다.
- `priorExists`(옵션, 기본 false): true 면 `prior=ProductSearchFilters()`(빈 멀티턴 상태)로
  판정한다 — 첫 턴 게이트(`prior is not None`)를 재현한다.
- `flagEnabled`(옵션, 기본 true): false 면 `underspecified_reask_enabled=False` 로 판정한다
  (buy-under-0008, 롤백 경로 전용).
- `expected.reask`: 기대 판정 (`is_underspecified_turn` 반환값).
- `testType`: `MFT`(라벨 필요) 또는 `INV`(불변, 회귀 방지용 — "이 축은 절대 트리거하면 안
  된다"는 경계).
- `note`: 판정 근거 한 줄.

**`decomposeFixture` 는 decompose 산출 가정값이다** — 발화(`utterance`)가 실제로 그
필드값을 산출하는지(예: "이어폰 추천해줘"가 정말 `category_queries` 를 채우는지)는 이
케이스가 검증하지 않는다. 여기서는 "decompose 가 이 값을 냈다면 판정이 이렇게 나와야 한다"만
고정한다. **그 발화→산출 정합은 `evals/underspecified_probe`(#380)가 실 LLM 반복 분포로
잰다** — #335 는 e2e 커버리지 소관으로 이 정합을 이행하지 않았고, #380 이 이 8건 중 7건을
`cases.json` 승계 앵커로 그대로 흡수해 실측한다(`evals/underspecified_probe/README.md` 의
「cases.json 8건 매핑표」 참조).

앵커 로더는 `tests/unit/test_underspecified.py::test_cases_json_anchor` — 하네스는 이
PR 에 커밋되고, `cases.json`(데이터 파일)만 바뀌면 케이스가 늘어난다.
