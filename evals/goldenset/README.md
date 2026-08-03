# 구매자 추천 골든셋 v1

`evals/goldenset`은 구매자 추천의 검색·개인화·재구매·실패 경계를 고정한 내부 전용
결정론 데이터셋이다. 진입점은 `loader.load_cases("dev")`이며, holdout 질의는
`load_cases("holdout")`으로 라벨 없이만 읽힌다. 라벨은 release 평가에서
공개 경로인 `unseal_holdout_labels(reason=..., commit_sha=...)`의 사유·commit 게이트를
통해서 연다. 누출 감사에는 비공개 `_load_labeled_holdout_for_audit()` 접근자가 하나 있으며
`audit.py`만 호출하고 튜닝 코드에서는 사용하지 않는다.

- 스키마·검증: `schema.py`
- split·봉인·#32 어댑터: `loader.py`
- 라이브 I-1 기록: `snapshot.py`
- 누출 감사: `audit.py`
- 작성·검수·변경 규칙: `GUIDE.md`
- 데이터 버전·해시: `manifest.json`

43건(dev 31 / sealed holdout 12)이며, 상품 라벨은 로컬 catalog 7,220건과 라이브 Spring
I-1 응답에서만 가져왔다. 구매 이력 페르소나는 합성이지만 그 안의 상품 ID·이름·카테고리는
동일 스냅샷의 실제 상품이다.

**#32 검색 비교는 이 데이터셋의 `search` slice와
`to_compare_golden_cases()`를 유일한 골든셋 원천으로 쓴다. 별도 골든셋 파일을 만들지 않는다.**

후보가 전부 정답인 케이스는 순위 품질을 측정하지 못하므로 노출·필터·하드제약 검증에만 쓴다.
#143은 `audit/leakage_report.json`의 `nonDiscriminativeRankingCases` 목록을 읽어 해당 케이스를
nDCG·MRR·Precision@k 분모에서 제외해야 한다.
