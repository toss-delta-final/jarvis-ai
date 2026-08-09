# 변경 요약

#474 색상 고유어/정본 표기 MFT 6건과 I-1 색상 배열 mock 충실도, 승인 사전 기반 on/off A/B 측정을 추가한다.

## 후속 범위 확대 (사람 지시)

사람의 명시 지시로 원래 #474 범위 밖이었던 두 후속을 이 브랜치에 함께 포함한다. 로컬 환경에
떠 있는 Spring BE가 유닛 테스트 결과를 바꾸는 문제와 manifest 규칙 변경 뒤 현행 baseline이 낡은
hash를 가리켜도 전수 테스트가 통과하던 문제는 모두 #474 산출물의 신뢰성을 직접 훼손하므로,
별도 이슈로 분리하지 않고 같은 검증 경계에서 닫는다.

- A — `INTERNAL_API_TOKEN`을 테스트 공통 환경에서 비우고 `tests/unit/` TCP만
  `ConnectionRefusedError`로 차단했다. 8080 listener가 살아 있는 상태에서 가드를 제거한 변이는
  `test_network_isolation.py`가 `DID NOT RAISE ConnectionRefusedError`로 실패했고, 복구 뒤
  재구매·완화 323건과 전체 `uv run pytest`로 `.env` 무변경 수용 기준을 확인한다.
- B — `evals/**/baselines/**` JSON의 중첩된 `datasetVersion`/`datasetHash` 쌍 중 현행 manifest
  버전만 비교하고 최소 한 건 이상을 요구한다. `trivial_empty/results.json` hash 첫 글자를 바꾼
  변이는 파일 경로·기록 hash·manifest hash를 출력하며 실패했고, 원복 뒤 통과한다.

| 케이스 | recall@10 off→on | nDCG@10 off→on | 노출(축없음/일치/불일치) |
|---|---:|---:|---|
| buy-colr-0001/3/5 | 0→1 | 0→0.445734 | 6/0/0 → 6/3/0 |
| buy-colr-0002/4/6 | 1→1 | 0.445734→0.445734 | 6/3/0 → 6/3/0 |

| baseline | 처리 | 사유 |
|---|---|---|
| audit leakage report | 재실행 | dataset 2.3.0 감사 |
| filter_axes trivial_empty | 재실행 | 지정된 결정론 baseline |
| scoring dev-v2.3 | 신규 sibling | v2.2와 직접 비교 금지; 신규 케이스 임베딩 결측은 degrade |
| ablation/personalization/model_eval | 기존 hash 고정 | 실 LLM baseline, 이번 범위 밖 |

STEP-1: 기존 color 15건 노출 9→9, 정답 탈락 0건, behaviorChecks 18/18→18/18, overall nDCG@10 0.766540→0.766540.

## 관련

Closes #474

## 체크리스트

- [x] `uv run pytest` 통과 — `5040 passed, 156 deselected, 1 warning in 315.35s` (d3b83d3, 로컬 CI 동등 실행)
- [x] `uv run ruff check` 통과
- [x] CHANGELOG 갱신
- [x] 계약 문서 무변경
- [x] `docs/lessons.md` 기록 — 런타임 로그는 datasetHash에서 제외
- [x] 신원은 JWT `sub`에서만 도출 · productId는 string — eval fixture/앱 계약 무변경

## 리뷰 노트

- labelSource는 `model`, 신규 6건 adjudicator는 독립 검수 부재로 비어 있다.
- dev-v2.2와 2.3.0 점수는 직접 비교하지 않는다. 실 LLM/임베딩 baseline은 외부 의존성 때문에 재실행하지 않았다.
- 신규 쌍은 off 팔에서 의도적으로 달라져 INV 그룹에 등록하지 않았다.
- 변이 시험 실측: MUT1(색상 mock 무력화)·MUT2(on-arm 확장 off)는 각각 A/B 비공허성 테스트를,
  MUT3(manifest `files[]`에서 `__init__.py` 제거)는 완전성 테스트를 정상 실패시켰다.
