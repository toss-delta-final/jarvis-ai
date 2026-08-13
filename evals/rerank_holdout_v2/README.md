# Rerank prospective holdout v2

Issue #631의 rerank scoring 비교를 기존 19개 ranking holdout에 다시 맞추지 않기 위한 **별도
prospective dataset**이다. 랭킹 세트는 200건이고 safety 세트는 24건이다. 두 숫자는 항상 따로
보고하며, safety 24건은 nDCG나 paired confidence interval의 N에 포함하지 않는다.

## 현재 상태: draft, confirmatory 아님

`dataset/manifest.json`의 현재 상태는 다음과 같다.

- `rankingCount=200`
- `safetyCount=24`
- `labelStatus=draft`
- `confirmatoryEligible=false`
- `datasetHash=4fa52e596f97c60c2b067c0ca6b30345ed574fcb7ad67acb67009b344a49f87b`

200개 query/candidate core는 준비됐지만 라벨은 결정론 heuristic 초안이다. 실제 두 명의 독립
사람 검수와 모든 불일치 adjudication이 끝나기 전에는 이 데이터로 낸 수치를 confirmatory
결과, production 승격 근거, 또는 “N=200 사람 라벨 평가”라고 부르면 안 된다.

## 출처와 한계

repo에는 production query log export가 없다. 새 케이스는 SHA-256
`f98539b2...fec840d`로 고정한 `evals/goldenset/fixtures/catalog_snapshot.json`의 6,585개
상품에서 seed `631200`으로 생성했다. 따라서 provenance는
`synthetic-catalog-derived`이지 `production-derived`가 아니다.

기존 `buyer_dev.jsonl`과 라벨 없는 `buyer_holdout.jsonl` core는 query leakage 검사에만 읽는다.
이미 열린 `buyer_holdout_labels.jsonl`은 생성·검증에서 읽지 않으며 새 라벨로 복제하지 않는다.
감사 테스트가 해당 경로 접근을 직접 실패시킨다.

같은 catalog와 고정 템플릿에서 생성됐기 때문에 200건이라는 행 수가 production traffic의 완전한
대표성을 뜻하지는 않는다. 이 세트는 기존 19건보다 넓은 prospective 판단 재료를 만들지만, 실제
query distribution 검증은 production-derived 표본이 생긴 뒤 별도 단계로 수행해야 한다.

## 구성

| stratum | 랭킹 N | guest | member |
|---|---:|---:|---:|
| `general` | 48 | 40 | 8 |
| `budget_multi` | 40 | 28 | 12 |
| `personalization` | 48 | 0 | 48 |
| `repurchase` | 24 | 0 | 24 |
| `long_tail` | 24 | 24 | 0 |
| `adversarial` | 16 | 8 | 8 |
| **합계** | **200** | **100** | **100** |

모든 ranking core는 중복 없는 catalog 후보 30개를 가진다. heuristic positive는 케이스당 1~6개,
grade는 1~3이고 생략 후보는 암묵 grade 0이다. `personalization`은
`preference_helpful` 24건과 `profile_overreach` 24건으로 나뉜다.

Safety는 다음 세 scenario를 각 8건씩 가진다.

- `catalog_prompt_injection`
- `hard_constraint_integrity`
- `candidate_set_integrity`

## 파일

- `dataset/cases/ranking_core.jsonl`: 라벨 없는 200개 query/profile/candidate core
- `dataset/annotations/draft_labels.jsonl`: heuristic 초안 200개; 공개 평가용이 아님
- `dataset/cases/safety.jsonl`: 별도 safety 24개
- `dataset/audit/report.json`: quota, candidate, constraint, leakage 감사 결과
- `dataset/manifest.json`: 파일 hash, dataset hash, 상태와 release eligibility

Core에는 `relevanceGrades`, `idealOrder`, `hardConstraints`, `mustExcludeProductIds`가 들어갈 수
없다. Loader는 manifest의 개별 파일 hash, 전체 dataset hash, catalog hash를 모두 확인한다.

## 재생성과 감사

기존 destination을 덮어쓰지 않는다. 새 경로에서 생성한 뒤 committed artifact와 byte diff한다.

```bash
uv run python -m evals.rerank_holdout_v2 generate \
  --catalog evals/goldenset/fixtures/catalog_snapshot.json \
  --legacy-root evals/goldenset \
  --seed 631200 \
  --out /tmp/rerank-holdout-v2

diff -qr evals/rerank_holdout_v2/dataset /tmp/rerank-holdout-v2

uv run python -m evals.rerank_holdout_v2 audit \
  --root evals/rerank_holdout_v2/dataset
```

정상 audit 요약은 다음과 같다.

```text
ranking=200 guest=100 member=100 safety=24 label_status=draft confirmatory=false
```

현재 legacy core 133개와의 최대 query token-Jaccard는 `0.40`이고 차단 임계치는 `0.85`다.

## 사람 검수와 봉인

두 reviewer packet은 서로 다른 candidate permutation을 사용하고 heuristic grade를 싣지 않는다.
각 CSV는 header 1행 + `200 × 30 = 6,000` judgment 행이다.

```bash
uv run python -m evals.rerank_holdout_v2 packet \
  --root evals/rerank_holdout_v2/dataset \
  --reviewer-slot A --out /secure/review-a.csv

uv run python -m evals.rerank_holdout_v2 packet \
  --root evals/rerank_holdout_v2/dataset \
  --reviewer-slot B --out /secure/review-b.csv
```

각 사람은 모든 행의 `grade`(0~3), `reviewerId`, ISO-8601 `reviewedAt`, `rationale`을 채운다.
두 packet의 reviewer ID는 달라야 한다. 불일치가 있으면 다음 형식의 JSON list로 전부 판정한다.

```json
[
  {
    "caseId": "rh2-general-0001",
    "productId": 1234567890,
    "grade": 2,
    "adjudicatorId": "human-adjudicator-id",
    "adjudicatedAt": "2026-08-20T14:00:00+09:00",
    "rationale": "두 검수 근거와 catalog fact를 비교한 최종 판정"
  }
]
```

불일치가 없으면 `[]`을 쓴다. 실제 검수 결과를 새 release 디렉터리에 봉인한다.

```bash
uv run python -m evals.rerank_holdout_v2 seal \
  --root evals/rerank_holdout_v2/dataset \
  --review-a /secure/review-a.csv \
  --review-b /secure/review-b.csv \
  --adjudications /secure/adjudications.json \
  --sealed-at 2026-08-20T15:00:00+09:00 \
  --out /secure/rerank-holdout-v2-sealed
```

모든 6,000개 후보 judgment가 있고, reviewer가 독립적이며, 모든 불일치가 adjudication됐고,
최종 라벨이 hard constraint/quota/leakage 감사를 통과할 때만 sealed manifest가
`confirmatoryEligible=true`가 된다. Production 코드는 reviewer identity를 만들지 않으며 test의
`human-reviewer-a/b` 값은 release gate만 검증하는 명시적 synthetic fixture다.

## Rerank 실행

Draft는 scripted dry-run으로 loader/runner만 점검할 수 있다.

```bash
uv run python -m evals.rerank_scoring \
  --dataset rerank-holdout-v2 \
  --arms current,structured \
  --case-ids rh2-general-0001 \
  --order-seeds 11 --dry-run \
  --out /tmp/rerank-holdout-v2-dry
```

이 산출물은 `labelStatus=draft`, `confirmatory=false`, `status=not-tested`다. 기본적으로
`--dry-run` 없이 committed draft를 선택하면 live provider를 만들기 전에 거부된다.

Heuristic label이 만드는 방향성만 확인하려면 명시적 opt-in으로 exploratory live 평가를 할 수
있다. 이 경우 raw 수치와 bootstrap CI는 기록하지만 `status`와 비교 `verdict`는 항상
`exploratory`이고 원래 통계 판정은 `statisticalVerdict`에만 남는다.

```bash
uv run python -m evals.rerank_scoring \
  --dataset rerank-holdout-v2 --allow-draft-live \
  --arms current,structured --order-seeds 11,29,47 \
  --max-calls 1500 --max-cost-usd 20 \
  --out artifacts/rerank-scoring/rerank-holdout-v2-draft-live
```

실제 confirmatory 실행은 봉인 release를
`--dataset-root /secure/rerank-holdout-v2-sealed`로 명시한 뒤에만 가능하다.
