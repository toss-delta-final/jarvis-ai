# 카테고리 매핑·선택 프로브 (#331)

`decompose`(실 LLM, fast) → `map_categories`(실 임베딩 + 라이브 pg-catalog + 조건부 §4.4 택일
LLM)로 이어지는 **배포 파이프라인 그대로**의 발화→카테고리 정확도를 셀당 N회 실 LLM 반복 분포로
잰다. `evals/intent_probe` 의 확립된 규약(전역 페이서·실패는 표본이 아님·단일 실행 판정 금지)을
승계한다. 에픽 [#328](https://github.com/toss-delta-final/jarvis-ai/issues/328) 공통 규약 8항을
전부 지킨다(`evals/README.md`).

## 이건 골든셋이 아니다

| | `evals/goldenset` | 이 프로브 |
|---|---|---|
| 본체 | **추천 품질**(idealOrder·expectedFilters·hardConstraints) | **카테고리 매핑·선택 정확도** |
| 평가 | 결정론 1회, nDCG·MRR·P@k | **확률 분포**(발화당 N회) |
| 카테고리 라벨 표기 | Spring I-1 `categoryName`(leaf 단독) | pg-catalog canonical(`대분류 > 잎`, 아래 §표기 참조) |
| 봉인 | dev/holdout 봉인·누출 감사 | 불필요(정답이 자명해 봉인할 라벨이 아님) |

두 산출물의 숫자를 섞지 말 것. goldenset `category_mapping_failure` 슬라이스는 이 프로브가 재
측정하는 실패 가설의 **출처**일 뿐 채점 정본이 아니다(아래 앵커 절 참조).

## 실행

```bash
# 오늘의 기준선
uv run python -m evals.category_probe --out artifacts/category-probe/run1 --tier fast

# API 없이 배관만 확인 (가짜 LLM·가짜 pg)
uv run python -m evals.category_probe --out /tmp/probe --dry-run

# N·페이서 조정
uv run python -m evals.category_probe --out artifacts/run2 --n 16 --rpm 30 --tpm 150000
```

## CI 에서 돌리지 않는다

실 LLM·실 임베딩·라이브 pg 호출이라 비용·비결정론이 붙는다. **수동 실행 도구**다.
`tests/unit/test_category_probe_*.py` 는 전부 가짜(ScriptedCategoryLLM·FakeCatalog)라 CI 에서
API/pg 콜이 0이다.

## 표기 규약 (이미 확정 — 재론 금지)

정답 라벨은 **현행 pg-catalog `categories` 사전(leaf 1,007행)의 canonical 문자열 그대로** 쓴다.
예: `음향가전 > 이어폰`, `PC부품 > CPU`. 근거는 api-spec §4.6(2026-08-03 정본 개정) —
`categoryName` 은 **잎 이름 정확 일치**이고, 잎 이름 자체가 `대분류 > 중분류` 문자열이다
(`categoryName=거실가구 > 소파` ✅, `categoryName=거실가구` ❌ 0건). `map_categories` 의 legs
canonical 이 그대로 `graph.py:636` 에서 Spring `category` 필터로 나간다.

`evals/goldenset/GUIDE.md` 의 "leaf 단독 표기" 규칙은 **구 taxonomy 기준**이라 이 하네스에는
적용하지 않는다. leaf 단독 이름(예: `티셔츠`)은 도메인 간 중복이 커(실측 81건, 예: `청바지`가
4개 서로 다른 canonical 에 걸침) 라벨로 성립하지 않는다.

**검증 2단**: (a) 스키마(pydantic, CI) — accept 문자열은 `" > "` 를 정확히 1회 포함, 발화
원문은 `" > "` 를 포함 금지(정답 신호 직접 누출 방지). (b) 런타임 pre-flight(수동 도구,
`loader.preflight_check_catalog`) — 모든 accept 가 라이브 `categories` 에 실재해야 하며
불일치 시 종료 코드 2. notInCatalog 셀의 `absentKeyword` 가 사전에 실재하면(ILIKE) 역시 거부.

## 앵커(정답지)

`fixtures/anchors.json`(46셀, v2) — `fixtures/manifest.json` 의 sha256 과 대조해 읽는다(불일치 →
종료 코드 2). 외부 경로(`--fixture <path>`)는 대조를 건너뛰되 해시를 산출물에 기록한다.

| slice | testType | 셀 수 | 내용 |
|---|---|---|---|
| single | MFT | 22 | 협소 단일 카테고리 발화. `#344` 실측에서 드롭됐던 협소 발화 유형(노트북·기저귀·
  전기면도기 등)과 goldenset `category_mapping_failure` 9건 중 **8건**을 caseId 로 이어 포함
  (`buy-cmap-0001` "신라면 봉지라면 찾아줘"만 미승계 — 대응하는 leaf 가 카탈로그에 없어
  `가공식품 > 컵라면`/`수입라면` 뿐인 taxonomy 라 단일 정답 셀로 부적합하다고 판단해 뺐다).
  이 중 8셀(`instance-mft-*`)은 v2(#428)에서 추가한 **인스턴스형 앵커** — 아래 절 참조 |
| single | INV | 8 (4그룹×2) | 색상 동의어(`#258`, "빨간"/"레드")·표기 변형(decompose 프롬프트가
  직접 예시하는 "청바지"/"데님 팬츠", "무선 이어폰"/"무선 블루투스 이어폰") — 같은 의미 다른
  표기는 같은 카테고리로 수렴해야 한다 |
| multi | MFT | 6 | fan-out 발화(expectedLegs 2~3개) |
| none | MFT | 5 | 카테고리 무지정 — 기대: legs 빈 배열(#22 카테고리 강제 금지) |
| notInCatalog | MFT | 5 | 카탈로그에 없는 카테고리 지목(사전 ILIKE 조회로 부재 확인 후 선정) —
  기대: legs 빈 배열. `expansionLeaves` 는 진단으로만 기록 |

합계 46셀 × N=8 = 368 decompose 콜(+§4.4 택일 소량) — `evals/intent_probe` 기준선 런
(424콜, $0.086)보다 작다.

### v2(#428) — 인스턴스형 앵커

v1 의 38셀은 전부 **카테고리 층위**(taxonomy leaf 이름과 발화가 겹치는) 앵커였다 — "바나나"
같은 **인스턴스형**(leaf 의 한 사례를 부르는 표현) 앵커가 셋에 없어 `category_distance_max`
가 0.19~0.21(leaf 이름 표면 근접)로 캘리브레이션됐고, 실제 인스턴스형 발화(0.27~0.33)가
그 컷 밖에 있다는 사실이 드러나지 않았다(`docs/lessons.md` 참조). `instance-mft-001`~`005`
(사과·바나나·오렌지·배·라면)는 그 실패 모드를 계측하기 위한 셀이고, **현재
`category_distance_max=0.26` 초과로 드롭되는 것이 기대 동작이다** — `single` 슬라이스
점수(`top1Single`·`topKInclusion`·baseline)가 `baselines/fast-2026-08-06`(38셀 기준) 대비
떨어지는 것은 회귀가 아니라 **계측 범위 확대**다. `instance-mft-006`~`008`(계란·커튼·이불)은
leaf 이름과 상품명이 문자 그대로 겹치는 대조군 — 인스턴스형 셀과 나란히 두어 두 축의 대비를
드러낸다. `baselines/fast-2026-08-06/` 산출물 자체는 v1 38셀 기준으로 남겨 두고 수정하지
않는다(재실행 없는 사후 비교의 기준선).

**taxonomy 구조가 정당한 모호성을 만드는 셀**은 `expectedLegs[].accept` 에 해당 leaf 전부를
열거한다 — 예: "노트북 사고 싶어"는 이 taxonomy 가 노트북을 브랜드별 잎(`노트북 > 삼성전자`·
`ASUS`·`LG전자`)으로만 나눠 브랜드 미지정 발화가 구조적으로 모호하다. 이는 매핑 실패가 아니라
taxonomy 자체의 구조이므로 accept 를 복수로 열어 정당한 모호성과 오분류를 가른다.

## 축과 정의

정의는 `metrics.py` 의 함수 데이터로 있고 **산출물(`results.json`·`report.md`)에 그대로
실린다.**

| axisId | 분자 | 분모(N=8) |
|---|---|---|
| `top1Single` | 최종 legs 에 기대 leg 의 accept 중 하나가 존재 | single 30셀×8 = 240 |
| `topKInclusion` | 기대 accept 중 하나가 이긴 앵커의 top-`category_top_k`(5) 후보(계측 hits
  기준) 안에 존재. decompose 가 leg 자체를 안 냈으면 분자 불충족 | 같은 240 |
| `multiCoverage` | 기대 leg 별로 accept ∈ legs (leg 단위 집계) | multi 6셀 × 기대 leg 수 × 8 |
| `multiExactSet` | legs 집합이 기대 leg 집합과 정확히 일치(여분 leg 없음) | 6×8 = 48 |
| `noneNoForce` | legs == [] | 5×8 = 40 |
| `notInCatalogNoForce` | legs == [] (오답 canonical 강제 없음) | 5×8 = 40 |
| `invAgreement` | 그룹 내 전 변형의 다수결 top-1 canonical 이 서로 동일하고 null 아님 | 4그룹 |
| `baselineTop1Single` | trivial baseline top-1 ∈ accept | single 30셀(결정론 1회) |
| `baselineTopK` | accept ∈ baseline top-5 | 30셀 |

진단 카운터(합불 아님): `intentSlipCount`(decompose 가 recommend 가 아닌 intent 를 내 버려진
시도) · `noLegCount`(decompose 가 categoryQueries 를 못 낸 single/multi 표본 — **매핑 실패와
추출 실패를 가르는 핵심 카운터**) · `distanceRejectedCount` · `selectNullCount` ·
`selectChangedCount` · `exactHitCount` · `expansionTakenCount`.

**혼동 표**: single/multi 오답 표본에서 `기대 accept[0] → 실제 legs[0]`(leg 없으면 `∅`) 쌍
빈도. `report.md` 표 + `confusion.csv`.

## 표본 사전 등록·산정 근거

슬라이스별 목표 N 은 §328 공통 규약 4항(관측 분산에서 역산해 사전 등록)을 따른다. 주 지표
`top1Single` 의 분모(single 30셀×8=240)에서 p̂=0.5(최대 분산 가정) 기준 95% CI 반폭은:

```
SE = sqrt(0.5 × 0.5 / 240) ≈ 0.0323
95% CI 반폭 = 1.96 × SE ≈ 0.063
```

즉 이 표본 크기는 **방향 판정용**(파이프라인이 명백히 개선/퇴행했는지)이지 **미세 차이
판정용**(±수 퍼센트포인트 단위 프롬프트 튜닝 비교)이 아니다. 미세 튜닝을 재려면 §328 4항의
표(±0.05 ≈ 249건, ±0.10 ≈ 63건)를 참고해 `--n` 을 올린다.

## ⚠ 단일 실행으로 채택 판정 금지

같은 프롬프트·같은 임계의 독립 실행에서도 위 CI 반폭만큼 축이 흔들린다. 채택 판정은 **독립
2~3회** 분포로 한다(`evals/intent_probe` 와 동일 규약).

## trivial baseline

정의(`baseline.py`): 발화 원문을 `embedding_task_query` 로 임베딩 → `search_categories_pg`
top-5 → top-1. **LLM 0콜, 거리컷·택일 없음.** 셀당 결정론 1회. 같은 런에서 계산해 같은
`report.md` 에 파이프라인 수치와 나란히 싣는다. none/notInCatalog 슬라이스에서 baseline 은
항상 top-1 을 강제하므로 정의상 0% 다 — 그 사실 자체가 "baseline 은 기권할 수 없다"는 정보다.

## #344 와의 연결

`hits.csv` 는 (cellId, sampleIndex, legIndex, anchorKind, rank, canonical, distance) 행 전체를
남긴다 — 원시 top-k 거리 전량이라 임계(`category_distance_max` 등) 스윕을 **런 재실행 없이
오프라인**으로 할 수 있다. `samples.csv` 에는 채택 leg·unresolved·expansionLeaves·select 호출
수를 함께 남긴다. `report.md` 의 top-1 distance 분포(정답/오답 중앙값·사분위) 절도 이 계측을
요약한다.

`sweep.py` 가 이 오프라인 스윕 실행기다(API·pg·LLM 콜 0, `category_mapping.py` §4 의 채택
규칙을 그대로 재현):

```bash
uv run python -m evals.category_probe.sweep --run evals/category_probe/baselines/fast-2026-08-06
# 여러 런 합산 + 런별 분리
uv run python -m evals.category_probe.sweep --run <dir1> --run <dir2> --dmax-grid 0.24,0.26,0.28
```

이 기준선(`baselines/fast-2026-08-06`, 사전 leaf 1,007행) 스윕으로 `category_distance_max` 를
0.22 → **0.26** 으로 올렸다(#344) — single 정답 med 거리 0.2416·q3 0.2579 vs notInCatalog 최소
d1 0.2621 사이에서 nic 무강제(0/40)를 지키는 최대 컷이다. `category_distance_override_margin`
(0.035)·`category_select_margin_max`(0.02) 는 재검증만 하고 값은 유지했다. 근거는
`app/core/config.py` `category_distance_max` 주석 참조.

## run manifest

`evals/metrics/run_manifest.py::build_run_manifest` 를 확장한다(`manifest.py`). 앵커 sha256 ·
`decompose`/`category_select` `_SYSTEM` 해시 · 임베딩 모델·task_type · **사전 상태**
(`categories` 행 수 + 정렬된 category 문자열 전체의 sha256 = `dictionaryHash`) · 임계 3종
(`category_distance_max`·`category_distance_override_margin`·`category_select_margin_max`)를
남긴다. 거리 임계는 사전에 종속된다(`docs/lessons.md` 2026-08-05) — 사전이 재시드되면
`dictionaryHash` 가 바뀌어 과거 런과 비교할 수 없다는 사실이 드러난다.

**[이슈 #401] 무엇이어야 하는지는 이제 정본이 말해준다.** 카테고리 사전의 정본은
`db/catalog/seed/categories.json`(leaf 1,007 문자열, repo 편입)이고, `dictionaryHash`(`rowCount`/
`sha256`)는 여전히 **DB collation**(`ORDER BY category`, 예: en_US.utf8) 순서를 잰다 — 과거 런과의
비교 가능성 때문에 이 두 필드는 건드리지 않는다. 대신 `dictionary_fingerprint()` 가 추가로 남기는
필드로 정본과 대조한다:

- `canonicalSha256` — 같은 DB 행을 **codepoint 정렬**(파이썬 `sorted()`, `fingerprint_rows()`)해
  낸 sha256. DB collation 에 종속되지 않아 정본 파일의 지문과 직접 비교 가능하다.
- `seed` — `{"path", "rowCount", "sha256"}`. 정본 파일(`db/catalog/seed/categories.json`) 자체의
  codepoint 지문(`canonical_seed_fingerprint()`).
- `matchesSeed` — `canonicalSha256 == seed.sha256`. **`false` 면 라이브 DB 의 categories 가 repo
  정본과 어긋나 있다는 뜻** — 재시드 누락, 수동 INSERT, 또는 오래된 볼륨을 의심하라. `dmax` 등
  임계 재사용 전에 먼저 이 필드를 확인한다.

`rowCount`/`sha256`(DB collation 순서)와 `canonicalSha256`(codepoint 순서)이 다른 것은 버그가
아니다 — DB collation(en_US.utf8)과 파이썬 `sorted()`는 문자열 정렬 기준이 다르다(예:
`db81e849…`(codepoint) vs `fb9ca975…`(en_US.utf8), 같은 1,007행).

## 재현 함정

1. **전역 페이서 필수** — `evals/intent_probe/pacer.py::GlobalPacer`/`PacerLimits` 를 import 해
   재사용한다(intent_probe 디렉터리는 수정하지 않는다). decompose + §4.4 택일 콜은 전부 이
   페이서를 지난다. 임베딩 호출은 LLM 페이서 밖이다 — 429/일시 오류는 샘플 재시도로 흡수하되
   embed 래퍼에 지수 백오프 1~2회를 넣는다(`runner._embed_with_retry`).
2. **실패는 표본이 아니다** — 성공할 때까지 재시도해 N 을 채운다. 못 채운 셀은
   `results.json.unfilledCells` 와 `report.md` 에 드러나며 종료 코드 4가 된다.
3. **앵커-정답 누출 금지** — accept(canonical) 문자열이 발화 원문에 그대로 들어가면 정답이
   자명해진다(`schema.py` 가 발화의 `" > "` 포함을 거부해 강제한다).
4. **단일 실행 판정 금지** — 위 「표본 사전 등록」절의 CI 반폭만큼 흔들린다.

## DIR 미사용 근거

CheckList 테스트 유형 중 MFT(라벨 필요)·INV(불변, 라벨 불필요)만 쓴다. DIR(방향 술어, 라벨
불필요)은 이 축에서 성립하는 라벨-불필요 방향 술어가 없어 쓰지 않는다 — "카테고리가 더
좁아져야 한다/넓어져야 한다"류의 방향 관계를 라벨 없이 판정할 잣대가 카테고리 매핑에는 없고,
INV(같은 의미 다른 표기 → 같은 카테고리)가 라벨 공수 없는 규모 확장 수단을 대신 담당한다.
