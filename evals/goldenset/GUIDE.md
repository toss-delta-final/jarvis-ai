# 구매자 골든셋 작성·검수·변경 가이드

## 케이스 작성

1. 실제 실패 가설과 slice를 먼저 정하고 안정적인 `caseId`를 발급한다.
2. 원문 발화를 그대로 keyword로 쓰지 않고 decompose 정답인 `expectedFilters`를 먼저 확정한다.
   `snapshot.record_snapshots()`에는 이 필터 사전을 그대로 넘겨
   `app.services.spring_client.search_products`를 통해 라이브 I-1을 호출하게 한다. failure
   slice에서 원문 keyword의 0건을 증명할 때만 예외로 하며 notes에 의도를 적는다. 직접 HTTP
   호출이나 인증 토큰 기록은 금지한다.
3. 응답 순서의 상품명·카테고리·가격·설명과 필요 시 pg-catalog `search_doc`을 읽는다.
4. 관련도는 3=질의의 명확한 정답, 2=허용 가능, 1=주변부, 0=오답으로 판정한다. 근거가
   설명만으로 부족하면 정답에서 제외한다.
5. `notes`에 해당 등급과 금지 결과의 이유를 한국어 1~3줄로 남긴다.
6. `schema.validate_cases()`와 `audit.run_audit()`를 실행한다.

`expectedFilters.category`와 `hardConstraints.forbiddenCategories`는 Spring I-1 응답의
`categoryName` 표기(예: `돼지고기`, `축구`)를 쓴다. pg-catalog
`product_document.category`의 `대분류 > 소분류` 표기(예: `축산 > 돼지고기`)를 옮기지 않는다.
금지 카테고리는 `catalog_snapshot.json`의 실제 `categoryName`에 존재해야 한다.

I-19가 없는 현재 구매 이력은 합성 페르소나다. 다만 상품 필드는
`catalog_snapshot.json`에서 그대로 복사한다. `orderedAt`은 실행 시각으로 만들지 않고 기준일
`2026-08-02`에 대한 절대 ISO-8601 문자열로 고정한다. 그래야 90일 윈도우가 재실행마다
달라지지 않는다.

## 사람 검수

- `labeler`는 응답과 설명을 읽어 최초 등급·제약·notes를 작성한다.
- 별도 `adjudicator`는 상품 실재, 3등급 근거, 0/1/2 경계, 하드제약, 금지 결과, split 누출을
  독립 확인한 뒤 자기 가명 ID를 기록한다.
- 체크리스트: 실제 I-1 응답인가, 애매한 상품이 3등급인가, 모든 라벨 ID가 스냅샷에 있는가,
  가격제약을 정답이 스스로 위반하는가, persona/fixture/query가 split 사이에 재사용됐는가.
- v1의 `adjudicator`는 비어 있다. 현재 라벨은 구현자가 붙인 자동 초안이며 사람 검수 완료
  상태가 아니다.

## 변경

케이스 추가·삭제·재라벨은 반드시 다음 순서를 지킨다.

1. `CHANGELOG.md`에 `caseId`와 사유를 `add`/`remove`/`relabel`로 기록한다.
2. 모든 케이스와 manifest의 `datasetVersion`을 올린다.
3. 전체 대상 파일 SHA-256과 `datasetHash`를 다시 계산한다.
4. 누출 감사와 baseline 평가를 다시 실행한다.

**서로 다른 `datasetHash`에서 나온 점수는 직접 비교하지 않는다.**

## sealed holdout

- 튜닝 중 `buyer_holdout_labels.jsonl`을 열거나 import하지 않는다.
- 공개 release 평가 경로는 후보 commit에서만
  `unseal_holdout_labels(reason="<실행 사유>", commit_sha="<40자리 SHA>")`를 호출한다.
  호출은 dataset hash와 시각을 `audit/holdout_runs.jsonl`에 append한다.
- 누출 감사에는 라벨이 필요한 비공개 `_load_labeled_holdout_for_audit()` 접근자가 하나 있다.
  `evals/goldenset/audit.py`만 이를 호출하며 튜닝·평가 코드에서 직접 호출하지 않는다.
- 이후 라벨을 release 실행 결과와 대조하고 감사 로그를 보관한다.
- `.github/workflows/` 배선은 사람 승인 게이트이므로 이 이슈에서 만들지 않았다. 현재 release
  절차는 위 수동 봉인 해제 API와 기록을 코드 수준 강제로 사용한다.

## #32 검색 비교

#32의 query→relevant productId 원천은 이 데이터셋의 `search` slice뿐이다.
`loader.to_compare_golden_cases()`로 기존 `GoldenCase` 하니스에 연결하며 중복 데이터셋 파일을
추가하지 않는다.

후보가 전부 정답인 케이스는 어떤 순위도 만점이므로 순위 품질을 측정하지 못한다. 이 케이스는
노출·필터·하드제약 검증에만 사용하고, #143 평가기는
`audit/leakage_report.json`의 `nonDiscriminativeRankingCases`를 읽어
nDCG·MRR·Precision@k 분모에서 제외해야 한다.

## 향후 production-derived 추가

현재 production-derived 케이스는 0건이다. 향후 추가할 때는 비식별 여부를 사람이 수동
검수하고, raw prompt·raw profile·고객 원문·상품 원문을 저장하지 않는다. 필요한 신호는 유계
범주와 가명 식별자로 환원하며 개인을 재식별할 수 있는 조합도 금지한다.
