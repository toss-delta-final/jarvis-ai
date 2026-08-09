## 변경 요약

- A — 색상 동의어 확장이 켜진 채 승인 사전이 비면, TTL 캐시의 실제 로드 경계에서 원인과 조치를
  담은 한국어 WARNING을 한 번 남긴다. 빈 맵은 확장값을 주입하지 않아 기존 단수 `color` 와이어로
  degrade하며, 기동 시 pg-catalog를 강제 조회하지 않는 이유를 코드 주석으로 고정했다.
- B — 정본 I-1 3갈래 판정의 ②(상품에 색상 축이 없으면 통과)와 부분일치의 주체가 Spring BE라는
  사실을 회귀로 고정했다. AI 사후필터는 색상 축을 판정하지 않고, 확장 on/off 모두 `color`를 Spring
  payload 축으로 유지하며 배열 원소를 변형하지 않는다.

### dev 실측 상태 (origin/dev `f1f621e`)

| 이슈 완료 조건 | 상태 | 이번 변경 |
|---|---|---|
| api-spec §4.6 `color: string[]`·3갈래 판정 | v0.28.4에서 완료 | 무변경 |
| `color_synonyms` approved 승격·SQL 반영 | 46행 완료 | 무변경 |
| 두 플래그 기본값 off | DB 없는 CI 연결 누적 회귀 방지 | 무변경 |
| 색상 A/B 하네스 | #474/#501에서 완료 | 실행·기록만 |
| approved 0건 조용한 무동작 가드 | 미구현 | A로 추가 |
| 색상 축 부재 통과의 AI 정합 회귀 | 미구현 | B로 추가 |

### 색상 A/B 실측

`uv run pytest tests/eval/test_goldenset_eval.py -q -m eval -k color`는 `2 passed, 2 deselected`였고,
`evaluate_color_expansion()` 결과는 이 브랜치의 변경 전 #474 기록과 동일하다. A/B는 런타임
관측·회귀만 추가하며 하네스·시드·골든셋을 변경하지 않았으므로 수치 변화가 없다.

| 케이스 | recall@10 off→on | nDCG@10 off→on | 노출(축없음/일치/불일치) off→on |
|---|---:|---:|---|
| buy-colr-0001 | 0→1 | 0→0.445734 | 6/0/0 → 6/3/0 |
| buy-colr-0002 | 1→1 | 0.445734→0.445734 | 6/3/0 → 6/3/0 |
| buy-colr-0003 | 0→1 | 0→0.445734 | 6/0/0 → 6/3/0 |
| buy-colr-0004 | 1→1 | 0.445734→0.445734 | 6/3/0 → 6/3/0 |
| buy-colr-0005 | 0→1 | 0→0.445734 | 6/0/0 → 6/3/0 |
| buy-colr-0006 | 1→1 | 0.445734→0.445734 | 6/3/0 → 6/3/0 |

## 관련

Part of #505

PR #502가 `Closes #461`으로 #461을 조기 종료해 잔여 완료 조건 5건은 #505가 승계했다. 이 브랜치는
#505의 5건 중 승인 0건 무동작 가드와 색상 축 부재 통과의 AI 정합 회귀 두 건만 만족한다.
`1ecd58a` 본문의 `Closes #461` 트레일러는 force-push 금지로 정정하지 않으며 **무효**이고, 이 문서의
`Part of #505`가 정본이다.

| #505 잔여 완료 조건 | 이 브랜치 |
|---|---|
| `color_synonyms` 743행 사람 검수 → approved 승격 | 사람 검수 산출물 |
| 두 플래그 동시 on | 인프라·사람 게이트 |
| 켜기 전/후 운영 실측 대조 | 앞의 두 조건 완료 후 가능 |

## 체크리스트

- [x] `uv run pytest` 통과 — `5277 passed, 156 deselected` (로컬 전체)
- [x] `uv run ruff check` 통과
- [x] CHANGELOG 갱신
- [x] 계약 문서 무변경 (`docs/api-spec.md` 무접촉)
- [x] `docs/lessons.md` 기록
- [x] 신원은 JWT `sub`에서만 도출 · productId는 string — 이 변경은 검색 관측·회귀에 한정

## 리뷰 노트

### 변이 시험

- A — 빈 승인 사전 WARNING 블록을 임시 삭제한 뒤
  `uv run pytest tests/unit/test_color_synonym_wiring.py -q -k 'empty_approved_dictionary'` 실행:
  `2 failed, 1 passed, 9 deselected`. 두 테스트 모두 `assert 0 == 1`로 `"승인 행이 0건"` 로그가
  없음을 검출했다. 원복 뒤 대상 테스트는 통과했다.
- B — `apply_ai_side_filters`에 임시 색상 완전일치 하드필터를 주입한 뒤
  `uv run pytest tests/unit/test_color_synonym_wiring.py -q -k ai_side_filters_leave_all_color_cases` 실행:
  `1 failed, 11 deselected`. 색상 축 없음·불일치 상품이 탈락해 전체 목록 동일성 단언이 실패했다.
  원복 뒤 대상 테스트는 통과했다.

### 범위 밖

- `docs/api-spec.md`, 플래그 기본값, deploy/Variables/인프라, 승인 시드·SQL 재생성, decompose 프롬프트,
  골든셋·baseline, 확장 상한 튜너블은 변경하지 않았다.
