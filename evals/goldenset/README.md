# 구매자 추천 골든셋 v2

`evals/goldenset`은 구매자 추천의 검색·개인화·재구매·실패 경계를 고정한 내부 전용
결정론 데이터셋이다. 진입점은 `loader.load_cases("dev")`이며, holdout 질의는
`load_cases("holdout")`으로 라벨 없이만 읽힌다. 라벨은 release 평가에서
공개 경로인 `unseal_holdout_labels(reason=..., commit_sha=...)`의 사유·commit 게이트를
통해서 연다. 누출 감사에는 비공개 `_load_labeled_holdout_for_audit()` 접근자가 하나 있으며
`audit.py`만 호출하고 튜닝 코드에서는 사용하지 않는다.

- 스키마·검증: `schema.py`
- split·봉인·#32 어댑터: `loader.py`
- 라이브 I-1 기록(골든+완화 검색): `snapshot.py`
- 하드 네거티브 주입(pg-catalog 조회 전용): `inject.py`
- 누출 감사: `audit.py`
- v1→v2 일회성 이관 스크립트: `migrate_v1_to_v2.py`
- 작성·검수·변경 규칙(v2 절 포함): `GUIDE.md`
- 데이터 버전·해시·슬라이스 쿼터·confirmatory 규약: `manifest.json`

120건(dev 96 / sealed holdout 24)이며, 상품 라벨은 로컬 catalog(pg-catalog 6,559건 —
v1 시드 7,220건에서 축소)와 라이브 Spring I-1 응답에서만 가져왔다. 구매 이력 페르소나는
합성이지만 그 안의 상품 ID·이름·카테고리는 동일 스냅샷의 실제 상품이다. Part 1(#333)은
v1을 구조·규약만 v2로 이관했고, Part 2가 `evals/goldenset/campaign_v2.py` 라이브 캠페인으로
후보를 30까지 채우고 하드 네거티브를 라벨링하며 슬라이스 쿼터·INV/DIR 그룹을 채웠다
(datasetVersion 2.1.0).

**#32 검색 비교는 이 데이터셋의 `search` slice와
`to_compare_golden_cases()`를 유일한 골든셋 원천으로 쓴다. 별도 골든셋 파일을 만들지 않는다.**

후보가 전부 정답인 케이스는 순위 품질을 측정하지 못하므로 노출·필터·하드제약 검증에만 쓴다.
#143은 `audit/leakage_report.json`의 `nonDiscriminativeRankingCases` 목록을 읽어 해당 케이스를
nDCG·MRR·Precision@k 분모에서 제외해야 한다.

## no-op 기준선

**no-op = 시스템이 실제로 노출한 상품 집합(중복 제거 후)을 productId 오름차순으로 재정렬한
것을 노출했다고 가정한 기준선이다(#333 리뷰 F-4b).** fixture candidates를 별도로 참조하지
않는다 — 시스템이 이미 hard_filter·dedup을 포함한 실제 앱 경로를 거쳐 낸 노출 집합 그 자체가
"같은 후보"의 정의이고, 거기에 임의 순서(오름차순)를 적용한 것이 no-op이다. fixture 후보
자체가 이미 productId 오름차순으로 기록되므로(`schema.SearchFixture` 불변식), 이는 "시스템
노출 집합에 fixture 순서를 적용한 것"과 동치다. `evals/metrics`는 모든 실행에서 이 기준선의
순위 지표를 시스템 출력과 나란히 `noopBaseline` 블록(`definition` 필드에 이 규약을 그대로
실음, `evals/metrics/runner.py` `NOOP_BASELINE_DEFINITION`)으로 함께 낸다.

**정의를 두 번 바꾼 이유**: no-op 기준선의 목적은 "같은 후보·같은 노출에서 순서만 임의"를
재는 것이다(#275의 no-op도 동일 eligible 집합 위 순서 비교였다). 최초 구현(F-2 이전)은
fixture 후보 전체를 그대로 no-op으로 썼는데, 시스템이 후보보다 짧게 노출하면 그 차이가
"노출 길이 효과"로 nDCG 델타에 섞였다(F-4). 이를 fixture 후보를 시스템 노출 길이로 자르는
방식으로 고쳤더니(F-4), 실측 스모크에서 4/31 케이스가 여전히 달랐다 — 앱의 실제 dedup/필터가
후보 앞쪽(낮은 productId) 상품을 제외하고 뒤쪽 상품으로 대체하는데, "앞에서 N개 자르기"는 이
대체를 반영하지 못해 no-op 노출 집합이 시스템의 실제 노출 집합과 달라졌다(F-4b). 최종 정의는
시스템의 실제 노출 집합 자체를 재정렬하므로 이 문제가 구조적으로 발생하지 않는다.

**`passthrough`는 검색 순위 기준선이 아니라 임의 순서 기준선이다.** v1 dev search fixture
32/32건이 이미 productId 오름차순으로 기록돼 있었기 때문에 `evals/scoring`의 `passthrough`
baseline은 우연히 no-op과 같은 순서였다 — "검색엔진이 매긴 순위"가 아니다. #145 baseline
결과 수치 자체는 바뀌지 않지만 해석은 이렇게 정정한다(`evals/scoring/README.md`도 동일).

## 버전·해시

**서로 다른 `datasetHash`에서 나온 점수는 절대 직접 비교하지 않는다.** v1(`1.0.0`,
hash `764bc148858cb9c04b9da7a210a5479f7f0daa04bec61563c7f94233e9646b04`)과 v2(`2.0.0`)는
스키마·슬라이스·candidates provenance가 달라 같은 지표 이름이라도 분자·분모 정의가 다를 수
있다. `evals/scoring/baselines/dev-v1/`·`evals/ablation/baselines/20260803-dev-full-n5/`는
v1 hash 기준 참고값으로만 남기고 전 baseline 재실행은 Part 3에서 한다.
