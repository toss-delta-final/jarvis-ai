# 구매자 추천 품질 metric runner

`evals.metrics`는 `evals/goldenset`의 **dev split만** 소비해 구매자 추천 품질을
네트워크·라이브 LLM 없이 결정적으로 계산한다. sealed holdout 라벨은 열거나 import하지 않으며,
holdout 실행은 `NotImplementedError`로 닫혀 있다(#144).

## 실행

```bash
uv run python -m evals.metrics --out /tmp/buyer-eval
uv run pytest -m eval
```

출력 경로는 반드시 `--out`으로 지정한다. `results.json`, `report.md`, `cases.csv`,
`aggregates.csv`, `violations.csv`, `failures.csv`, `run_manifest.json`이 생성된다.
JSON 키와 CSV 헤더는 camelCase이며 JSON은 `sort_keys=True`, UTF-8, LF로 직렬화한다.
`run_manifest.json`의 `run` 객체에만 실행 인스턴스마다 달라질 수 있는 `runId`, `timestamp`,
출력 경로를 포함한 `command`를 격리한다.
OS 환경변수와 `.env`는 평가 설정에 영향을 주지 않으며, 모든 튜너블은 코드 기본값 또는 adapter에
명시적으로 주입한 설정만 사용한다.

## 기본 adapter

기본 `OfflineBuyerAdapter`는 `decompose()`와 `stream_recommendation()`을 연결하고,
`httpx.MockTransport`에서 I-1 검색·I-19 구매 이력·I-21 목록 push만 대역한다.
따라서 URL, `X-Internal-Token`, Spring envelope 파싱, 앱 사후필터, rerank, 노출 보정은
실제 앱 코드를 지난다. scripted decompose는 케이스의 `expectedFilters`를 반환하므로 기본 실행의
Filter Accuracy는 구조상 1.0이다. scripted rerank는 fixture 검색 순서를 유지한다. 이 스탠드인은
#144 실모델 평가와 #145 baseline/ablation에서 교체된다.

## 지표와 분모

시스템 출력의 duplicate id는 **첫 등장만** 순위·제약 지표에 인정하고 중복 건수를 별도 목록 품질
위반으로 보고한다. 카탈로그에 없는 id는 관련도 0으로 계산하고 `unknownProductIds`에 남긴다.

1. **Precision@K** = 정답과 중복 제거 상위 K의 교집합 크기 / **K**. 결과가 K보다 짧아도 분모는
   K라서 부족한 결과를 벌점 처리한다.
2. **Recall@K** = 같은 교집합 크기 / 정답 수.
3. **MRR** = 첫 정답의 1-base 순위 역수이며 정답이 없으면 0이다.
4. **nDCG@K**는 `DCG = Σ rel_i / log2(i+1)`의 **linear gain·log2·1-base** 공식이다.
   IDCG는 `(등급 내림차순, productId 오름차순)`으로 결정론 정렬하며 IDCG=0은 nDCG 분모에서
   제외한다. binary 0/1과 graded 0~3을 같은 공식으로 처리한다.
5. **Filter Accuracy**는 기대 필드와 산출 필드의 합집합을 분모로 삼아 값이 정확히 같은 필드 비율을
   계산한다. 누락과 과잉 산출이 모두 벌점이다.
6. **Hard Constraint Violation**은 `priceMax`, `priceMin`, `forbiddenCategory`,
   `forbiddenProductId`, `mustExclude`를 상품별로 모두 보고한다. 전체 위반율은 한 종류라도 위반한
   케이스 수 / 전체 케이스 수다.
7. **Coverage** 분모인 eligible catalog는 평가 케이스가 참조한 search fixture의 후보 id 합집합이다.
   **Diversity**는 케이스별 고유 `categoryName` 수 / 노출 수의 macro 평균이며 임베딩을 쓰지 않는다.
8. 순위 지표에서는 `audit/leakage_report.json`의 `nonDiscriminativeRankingCases`와 정답이 빈
   케이스를 제외한다. 제외 수와 모든 `caseId`를 JSON·Markdown·CSV에 명시한다.
9. 짧은 결과, duplicate, unknown, empty relevance, 동점, binary/graded 관련도는 단위 테스트로
   고정한다. 모든 유계 지표는 결정론적 seed 생성 루프로 `[0,1]` 범위를 검사한다.
10. 기본 집계는 **macro**(케이스 평균)이며 전체와 복수 소속 slice별로 각각 계산한다.
    Precision/Recall에는 보조 **micro**도 함께 낸다. micro Precision의 분모는
    `순위 평가 케이스 수 × K`, micro Recall의 분모는 순위 평가 케이스들의 정답 수 합계다.

## 축별 필터 지표(#334)

`results.json`의 `filterAxes`(케이스·slice·overall)·`filterAxesSpec`은 `evals/filter_axes`
패키지가 계산한 축별 분해 지표다 — 기존 `filterAccuracy`(합집합 분모 단일값)를 대체하지
않고 병행한다. 정의·정규화 규칙·기존 지표와의 관계는 `evals/filter_axes/README.md`가
정본이다. `report.md`에 축별 표 섹션이, `filter_axes.csv`에 케이스×축 outcome과
slice/overall 집계가 추가된다.

`results.json`의 케이스별 `filterAxes`는 축→outcome 문자열 map 형태를 그대로 유지한다 —
케이스 수준 P/R/F1 수치는 그 outcome과 **동치**다(`match`→precision·recall·F1 전부 1.0,
`valueMismatch`/`spurious`/`missing`→0.0, `bothEmpty`→분모 0이라 전부 None). 케이스 수준의
실제 수치(counts·support·valueStrict/presence P/R/F1)는 `filter_axes.csv`의 `scope=case`
행이 담당한다 — JSON을 다시 계산하지 않고 이 CSV로 필터링·집계하면 된다.

기본 scripted adapter(`OfflineBuyerAdapter`)는 `expectedFilters`를 decompose 출력으로
그대로 내므로 축별 지표도 Filter Accuracy와 마찬가지로 구조상 전부 1.0이 기대된다 — 의미
있는 값은 model_eval/ablation처럼 실제로 필터를 추출하는 adapter에서 나온다.

## PR gate 범위

critical subset은 `hardConstraints` 또는 `mustExcludeProductIds`가 있거나 `failure` slice인 케이스의
합집합이며 입력 순서를 보존한다. 기본 pytest에서 `@pytest.mark.eval` 테스트가 이 부분집합을 실제
오프라인 adapter로 실행한다.

현재 0을 강제하는 종류는 **`priceMax`, `priceMin`**이다. 이 축은 decompose 필터와 앱 결정론 코드가
강제하므로 회귀를 PR에서 막을 수 있다. `mustExclude`, `forbiddenCategory`,
`forbiddenProductId`는 판단 컴포넌트가 필요한데 기본 rerank가 검색순서 passthrough이므로, 라벨을
SUT에 주입해 거짓 0을 만들지 않고 지표·위반 artifact에 전부 공개한다. 전 종류 0 gate는 실모델을
연결하는 #144로 이관한다.

## no-op 기준선(#333)

모든 실행이 no-op 기준선(시스템이 실제로 노출한 상품 집합을 productId 오름차순으로 재정렬했다고
가정 — F-4b 리뷰 반영)의 순위 지표를 시스템 출력과 나란히 `results.json`의 `noopBaseline`
블록(`definition` 필드에 규약 문자열 포함), `report.md`의 비교 표, `aggregates.csv`의
`arm`(`system`/`noop`) 컬럼으로 함께 낸다. 정의는 `evals/goldenset/README.md`의 no-op 절을
본다.

## cutoff·슬라이스 N·confirmatory/exploratory(#333)

`ndcgAtK`는 3·5·10을 항상 함께 계산한다(전 슬라이스). **primary confirmatory metric은
`overall.ndcgAtK.10` 1개뿐이다** — 나머지 cutoff와 슬라이스별 수치는 exploratory다. 슬라이스
집계마다 N(`rankingCaseCount`)을 JSON·`report.md`·`aggregates.csv`에 인쇄하고,
`evals/goldenset/manifest.json`의 `confirmatory.confirmatorySlices`에 있고 N≥30이면
`confirmatory`, 아니면 `exploratory`를 `confirmatoryLabel`로 자동 라벨링한다(#328 다중비교
통제 공통 규약). 슬라이스 집계 함수(`aggregate_by_slice`)는 split과 무관하게 케이스 행
목록만으로 동작해 #144 holdout 러너가 재사용할 수 있다 — holdout 실행 자체의
`NotImplementedError` 게이트는 그대로다.

## behaviorChecks(#333)

`evaluate()`는 순위 지표와 분리된 `behaviorChecks` 섹션도 낸다. `evals/metrics/behavior.py`가
`behaviorGroupId`로 묶인 INV/DIR 케이스(`testType`)를 라벨 없이 검사한다 — 상세는
`evals/goldenset/GUIDE.md`의 INV/DIR 작성법 절을 본다.

**한계(#333 리뷰 F-3)**: 기본 `OfflineBuyerAdapter`는 scripted decompose(케이스의
`expectedFilters`를 그대로 반환)를 쓴다. 같은 `searchFixtureId`를 공유하는 `color_synonym`/
`word_order` INV 쌍은 발화 문구가 달라도 decompose 출력이 항상 같은 필터로 수렴하므로, 노출이
구조적으로 동일해 INV 검사가 **항상 통과**한다 — 이는 배선(파이프라인이 같은 필터를 같은
노출로 잇는지)만 검증하며, "모델이 색상 동의어·어순 변화를 실제로 같은 의미로 이해하는가"는
검증하지 못한다(위 "Filter Accuracy 1.0이 구조적으로 기대됩니다"와 같은 성격의 한계). INV 판정의
정본은 실모델 어댑터 실행(#144 계열, decompose가 발화를 실제로 파싱하는 경로)이다 — 기본
scripted 실행의 INV 통과를 모델 검증 근거로 인용하지 않는다.

## run manifest

manifest는 commit SHA와 dirty flag, `uv.lock`, dataset manifest, 세 fixture, decompose/rerank prompt,
config의 SHA-256, Python·OS, seed, 실행 명령을 기록한다. 컨테이너/CI 이미지 식별자는
`JARVIS_EVAL_IMAGE`를 `image`에 기록하며, 설정되지 않은 로컬 실행은 `null`이다. 서로 다른
`datasetHash` 결과는 직접 비교하지 않는다.
