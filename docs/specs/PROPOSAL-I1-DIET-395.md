# PROPOSAL-I1-DIET-395 — I-1 응답 비대화, BE 협의 3건 제안서

이슈 #395. 운영에서 구매자 추천이 `SEARCH_FAILED`로 떨어졌다 — 필터 없는 I-1 검색이
**7.74초·12.3MB**(AI→Spring 3초 예산 초과)였다. AI 쪽 방어(#132·#393 등)는 이미 끝났고, 이 문서는
**BE와 협의할 3건을 "BE가 바로 결정할 수 있는 제안서"로 만든 것**이다. 산출물은 문서 1(본 문서) +
재현 스크립트(`scripts/measure_i1_field_bytes_395.py`) + 회귀 테스트(`tests/unit/
test_i1_field_diet_395.py`) + CHANGELOG다. `app/` 프로덕션 코드는 건드리지 않았다.

## §0 요약

1. I-1 응답의 **81.9%가 `attributes`**이고, 그중 **53.2%(전체의 43.5%)가 AI가 전혀 읽지 않는
   `_extra`**(리뷰 문장·시각 묘사문)다. `_extra`·`_source_pid`·`_domain`·`_category`를 빼고
   `summary`·`options`·`optionCount`(모두 AI 미소비)까지 빼면 항목당 바이트가 **53.8~59.3% 준다**
   (실측 두 데이터소스 교차 확인, §3).
2. I-1은 결과 수 상한이 없다 — 매칭이 7,245건이면 7,245건을 전량 반환한다(`docs/api-spec.md`
   §4.6의 「[2026-07-23, BE 합의] `size` 제거 — 라운드1 전량 반환」 노트로 AI가 합의한 사양,
   `MEASURE-I1-RESPONSE-132.md` §8 처방이 그 사양을 유지하기로 재확인). 상한을 다시 두려면
   **정렬 기준**과 **`totalCount`**가 반드시 같이 와야 한다.
   그렇지 않으면 임의 표본 절단이거나(품질 붕괴), AI가 "그런 상품이 없다"고 조용히 오판한다(§4).
3. `rating`·`reviewCount`는 검색마다 `Review` 조인+집계로 계산된다(BE 주석 자인). BE에 **이미 있는**
   id-IN 배치 집계 경로(`ReviewService#getStats(ids)`, I-3/인기상품이 쓴다)가 있어, size 상한이
   생기면 그 경로로 갈아탈 여지가 열린다(§6).

| 협의 항목 | 제안 | 근거 한 줄 | BE 작업 크기(AI 추정) |
|---|---|---|---|
| 1. `size` 상한 | **협의 2 반영 시** 기본 1,000·하드 3,000 / **미반영 시** 하드 1,500 + 결정적 정렬 + `totalCount` 동반 | 처리율 역산(12.3MB/7.74s≈1.6MB/s, 남는 창 1.9s) — 다이어트 적용 N=3,000은 1.4s(예산 안), 미적용 N=3,000은 3.4s(예산 초과)(§4) | 중(쿼리 `LIMIT`+정렬 컬럼 선정+응답 필드 1개 추가) |
| 2. 필드 축소 | `attributes._extra`·`_source_pid`·`_domain`·`_category` 제외(1층) + `summary`·`options`·`optionCount` 제외(2층) | 항목당 바이트 **53.8~59.3%** 감소(§3·§5) + `keyword` LIKE 오탐 감소(§5) | 소(직렬화 코드에서 필드 몇 개 제외) |
| 3. rating/reviewCount 비정규화 | 근본: 리뷰 쓰기 시 집계 컬럼 갱신 / 중간: size 상한 도입 후 id-IN 배치 집계로 전환 | 검색마다 `LEFT JOIN Review ... GROUP BY` 도는 구조(§6) | 중(근본)~소(중간, 기존 코드 재사용) |

## §1 방법과 데이터 출처

무엇을 무엇으로 쟀는지:

- **§3 바이트 표**: repo 내 픽스처 `evals/goldenset/fixtures/catalog_snapshot.json`(6,585건)과
  BE 시드 SQL 덤프 `~/inte-final/_sql/mariadb/30_product.sql`(6,559건, repo 밖 자산) 둘 다로
  `scripts/measure_i1_field_bytes_395.py`를 돌려 교차 확인했다.
- **§2 소비 표**: 이 저장소(`app/`)의 정적 코드 인용 — 실행하지 않고 grep + 코드 읽기로 확인했다.
- **§4~§6 BE 근거**: BE 로컬 체크아웃 `~/inte-final/jarvis-backend`를 읽기 전용으로 열어
  `git show origin/main:<경로>`로 확인했다(작업 트리는 `742ba77`, 2026-07-31로 낡아 그대로 열면
  일주일 전 코드를 인용하게 된다).

재현 명령 한 줄:

```bash
uv run python scripts/measure_i1_field_bytes_395.py
```

측정 시점 **2026-08-07**. 근거 커밋 AI `4bba354`(`NyongCho/docs-395-i1-response-diet`, base `dev`),
BE `origin/main 4c6b287`(2026-08-07).

> ⚠️ 이 문서의 수치는 **측정 시점 기록**이다. 카탈로그·코드가 바뀌면 값이 달라진다 — 재측정 없이
> 현재 동작의 근거로 인용하지 말 것(`MEASURE-I1-RESPONSE-132.md` 말미와 같은 경고).

## §2 AI가 실제로 소비하는 필드

이슈 본문은 "AI 스키마가 파싱하는 건 11개 필드"라고 적었다. **그건 스키마 필드 수이지 소비 필드
수가 아니다** — 파싱은 하지만 아무도 읽지 않는 필드가 있다. 아래 표는 그 둘을 가른다. 소비 =
값을 실제로 읽어 판단·계산·프롬프트에 쓰는 것. 스키마 선언만으로는 소비가 아니다.

| 와이어 필드 | 파싱 | 소비 | 소비처(심볼) |
|---|:---:|:---:|---|
| `productId` | ○ | ○ | dedup 집합 구성(`app/agents/buyer/recommendation/graph.py` 다수), rerank 후보 키, I-21 push 대상 id |
| `name` | ○ | ○ | `app/agents/buyer/recommendation/rerank.py::rerank`(프롬프트 후보 dict 조립부, rerank 프롬프트 후보 항목) |
| `price` | ○ | ○ | `app/agents/buyer/recommendation/no_condition.py::within_budget`(예산 검증) · `rerank.py`의 `_price_tier`/`_price_medians`(가격 티어) |
| `rating` | ○ | ○ | `rerank.py::_rating_tier` · `search_service.apply_ai_side_filters`(평점 사후필터) · `app/agents/buyer/recommendation/graph.py::_unrated_product_ids`(무평점 판정) |
| `reviewCount` | ○ | ○ | `rerank.py::_review_tier` · `apply_ai_side_filters`의 rating=0 판별(리뷰 부재 vs 저평점 구분, #171) |
| `categoryName` | ○ | ○ | `rerank.py::rerank`(후보 dict 조립부, rerank 후보 `category`) · `graph.py`의 소모품 억제·그룹 키 |
| `brandName` | ○ | ○ | `rerank.py::rerank`(후보 dict 조립부, rerank 후보 `brand`) |
| `attributes` | ○ | **△** | `search_service._matches_attr_conditions`(명시 속성 하드매칭) **한 곳뿐**. rerank LLM 프롬프트에는 안 실린다(`rerank.py::rerank`의 후보 dict 조립부가 attributes를 프롬프트 dict에 넣지 않음) |
| `summary` | ○ | ✗ | `.summary`를 읽는 코드를 `app/`에서 찾지 못했다(grep 0건, seller 쪽 `finding.summary`는 무관한 다른 도메인). 스키마 주석은 "소비는 #101" — #101은 이미 병합됐고(방식2 채택) 그 PR이 실제로 구현한 것은 `attributes` 매칭(PR②)이었다. `summary` 소비 계획이 살아 있는지 별도 이슈로 추적되는 흔적은 못 찾았다 |
| `options`/`optionCount` | ○ | ✗ | 스키마 주석도 "이 PR은 rerank·문구·옵션 되물음에서 소비하지 않는다"(#278)고 명시. 읽는 코드 0건 |

- **`attributes`의 △가 이 이슈의 핵심**이다 — 소비되는 필드 안에 미소비 하위 키가 들어 있다.
  그래서 "필드 제외"는 최상위 필드 단위가 아니라 **`attributes` 내부 키 단위**여야 한다(§5).
- `summary`·`options`·`optionCount`는 소비 없음이 확인됐다. 다만 "소비 계획이 아예 없다"를
  단정할 근거(이슈·PR)는 못 찾았다 — **없다고 확인한 것이 아니라 못 찾은 것**이다. §5에서 이
  셋은 "당장 소비 없음이 확인됐으니 빼 달라"로 제안하되, 계획이 있다면 알려 달라는 문장을 붙였다.
- **사전 조사와 달랐던 점**: 없다. 이슈 작성자의 사전 조사(패킷 §2 표)를 항목별로 재확인한 결과
  전부 일치했다.

## §3 미사용 필드의 바이트 기여

표본: `evals/goldenset/fixtures/catalog_snapshot.json`(6,585건). 이 픽스처는 BE 카탈로그와 **같은
원천 덤프**에서 나왔고 `attributes` 모양이 BE가 그대로 내려보내는 JSON과 일치한다(항목 키 자체가
이미 I-1 와이어 필드 이름이다).

**BE가 실제로 그 키들을 내려보낸다는 교차 확인** — 두 근거:

- (a) BE 시드 덤프 `~/inte-final/_sql/mariadb/30_product.sql`의 `product.attributes`에 `_extra`가
  **전 행(6,559/6,559)** 들어 있다 — `grep -c "_extra" ~/inte-final/_sql/mariadb/30_product.sql`
  → `6559`, 헤더 주석 `-- product: 6,559 rows`와 일치(재현 가능).
- (b) BE 응답 DTO가 `attributes`를 가공 없이 통째로(Jackson `JsonNode`) 싣는다 —
  `com.jarvis.product.dto.ProductCandidateResponse`(`record ProductCandidateResponse(..., JsonNode
  attributes, ...)`, BE `origin/main`).

### §3.1 최상위 필드별 바이트 기여 (픽스처 6,585건, 원본 10.700 MiB)

| 필드 | bytes | % |
|---|---:|---:|
| `attributes` | 9,187,313 | 81.9% |
| `name` | 562,283 | 5.0% |
| `summary` | 443,155 | 3.9% |
| `categoryName` | 336,698 | 3.0% |
| `brandName` | 187,106 | 1.7% |
| `productId` | 164,536 | 1.5% |
| `reviewCount` | 134,442 | 1.2% |
| `rating` | 103,913 | 0.9% |
| `price` | 100,322 | 0.9% |

BE 덤프(6,559건, `options`/`optionCount` 없음 — 이 테이블엔 없는 컬럼)로도 교차 확인했다:
`attributes` 76.0%(9,149,015B) · `summary` 13.0%(1,569,523B, 덤프는 픽스처와 달리 대부분 채워져
있어 비중이 크다) — **`attributes`가 지배적이라는 결론은 두 소스에서 같다.**

### §3.2 `attributes` 내부 키별 바이트 기여 (attributes 전체 9,187,313B 대비 %)

| 키 | bytes | % of attributes |
|---|---:|---:|
| `_extra` | 4,885,357 | **53.2%** |
| `_category` | 317,379 | 3.5% |
| `_domain` | 198,550 | 2.2% |
| `_source_pid` | 190,876 | 2.1% |
| (그 외 소재·색상·사이즈 등 카테고리별 속성 축 — 각 2.0% 이하, 길게 흩어짐) | | |

`_extra`(review_pros·review_cons·situation_tags·visual_features) 하나가 attributes 바이트의
과반이고, `_source_pid`·`_domain`·`_category`(AI 사전 매핑 부산물, §2 확인)를 더하면 **attributes의
61.0%(전체의 49.9%)**가 §2에서 소비 미확인인 4개 키에 몰려 있다.

### §3.3 다이어트 적용 시 절감

`_extra`·`_source_pid`·`_domain`·`_category`(attributes 내부) + `summary`·`options`·`optionCount`
(최상위)를 뺀 결과:

| | 픽스처(6,585건) | BE 덤프(6,559건) |
|---|---:|---:|
| 원본 총 바이트 | 10.700 MiB | 11.476 MiB |
| 다이어트 총 바이트 | 4.944 MiB | 4.670 MiB |
| 절감 | **53.8%** | **59.3%** |
| 1건당 평균(원본) | 1,704 B | 1,835 B |
| 1건당 평균(다이어트) | 787 B | 747 B |

두 소스가 독립적으로 50%대 후반의 절감을 보인다(덤프는 실제 `product.price`가 채워져 있어 원본
평균이 더 크고, 그만큼 절감률도 더 크다 — 아래 편향 참조).

### §3.4 후보 N건일 때 예상 바디 크기 (픽스처 평균 기준, 추정)

| N | 원본(추정) | 다이어트(추정) |
|---:|---:|---:|
| 30 | 49.9 KiB | 23.1 KiB |
| 300 | 499.2 KiB | 230.7 KiB |
| 1,000 | 1.625 MiB | 768.9 KiB |
| 6,585(전량) | 10.700 MiB | 4.944 MiB |

⚠️ N=30/300/1,000 행은 1건당 평균 × N의 **추정치**다 — 실 분포는 상품마다 `attributes` 크기가
달라 균일하지 않다. 실측(§3.1~§3.3)과 구분해 읽을 것.

### §3.5 파싱 시간

`scripts/measure_i1_parse_132.py`(합성 payload, `attributes`+`options`+`summary` 전부 포함하는
무거운 항목 모양)를 로컬에서 재실행한 결과(2026-08-07):

| N | body | elapsed |
|---:|---:|---:|
| 30 | 0.02M | 12.1ms* |
| 1,000 | 0.70M | 11.0ms |
| 3,000 | 2.09M | 47.9ms |
| 7,245 | 5.06M | 92.6ms |
| 20,000 | 13.99M | 398.8ms |

\* N=30 행은 이 스윕의 첫 호출이라 인터프리터·pydantic 워밍업 비용이 섞여 있다(33배 큰
N=1,000보다 느리게 나오는 역전이 그 증거) — N 간 비교에는 쓰지 말 것. N≥1,000 구간의 단조
증가만 추세로 읽는다.

이 스크립트의 합성 항목(698B/건)은 §3.3 실측 평균(1,835B/건, BE 덤프)보다 가볍다 — 절대 바이트는
정확한 대조가 아니다. 다만 **추세**는 분명하다: N=20,000(오늘 카탈로그의 3배 규모)에서도 파싱은
400ms로, 3초 예산에 여유가 크다. **§4의 `size` 상한 필요성은 파싱 시간이 아니라 다른 이유(§4)에서
나온다** — 실측이 그 인과를 정직하게 좁힌다.

`--body-file`로 실 BE 응답을 넣는 대조(원본 vs 다이어트 바디)는 하지 않았다 — 로컬에 BE·DB를
띄우는 실험은 이번 이슈의 비범위(§10)이고, 여기서 시간을 태우지 않기로 했다(패킷 §3 지침).
바이트 실측(§3.1~§3.4)만으로 결론에 필요한 근거는 충분하다.

### §3.6 픽스처 편향 — 우리 주장에 불리한 쪽으로 읽을 것

픽스처의 `summary`·`price`·`rating`·`reviewCount`는 **78.0%(5,138/6,585건)가 null**이다(실측,
네 필드가 정확히 같은 비율인 것으로 보아 같은 배치에서 함께 비어 있다). 이는:

- `price`·`rating`·`reviewCount`의 §3.1 바이트 기여가 **실제보다 작게** 잡혔다 — 실 운영 응답은
  이 세 필드가 대부분 값을 가지므로 절감률 계산에서 분모(원본 총 바이트)가 실제보다 작다. **절감률
  53.8%는 그래서 보수적으로 읽어야 한다**(우리 주장에 유리한 쪽이 아니다).
- BE 덤프 기반 §3.1·§3.3 수치는 이 편향이 없다(`product.price`가 실제 값) — 두 소스를 나란히
  적은 이유다.

## §4 협의 1: `size` 상한

이슈 본문은 "AI 소비 상한은 `search_max_candidates`(기본 30) 수준이면 충분하다"고 적었다.
**그 이름의 설정은 이 저장소에 없다**(`app/core/config.py` grep 0건 — 한 문서에서 개념적으로
언급될 뿐 실제 설정이 아니다). 그리고 그 수치는 위험하다.

1. **AI는 30건을 "받아서" 쓰는 게 아니라 "전량에서 골라" 30건으로 줄인다.** 흐름: Spring 전량 →
   (fan-out leg 병합·dedup) → 임베딩 유사도 재정렬 → `embedding_rerank_limit`(config 기본 **30**)
   압축 → Sonnet rerank. 이 30은 **재정렬 뒤** 상한이다 — Spring 요청 파라미터가 아니다.
2. **BE 쿼리에 `ORDER BY`가 없다.** BE가 스스로 이렇게 적어 뒀다(`ProductRepository#searchCandidates`
   Javadoc, BE `origin/main`): *"정렬을 걸지 않는 것도 같은 이유(응답 순서 무보장) — 순위는
   FastAPI 리랭킹 소관이라 매칭 전체에 filesort를 거는 비용만 남는다."* 정렬 없는 결과를 앞에서
   자르면 **임의 표본**이다. `MEASURE-I1-RESPONSE-132.md` §3의 `keyword=세트` 실측(매칭 2,657건)을
   기준으로, `size=30`으로 앞을 자르면 사용자가 원한 상품이 후보에 남을 확률은 약 30/2,657 ≈
   **1.1%**다. `size=30`은 12MB 문제를 **품질 붕괴로 바꾸는 것**이다 — #132가 AI 쪽 개수 상한을
   기각한 이유와 같다(`MEASURE-I1-RESPONSE-132.md` §8: *"개수 상한은 두지 않는다 — 전량 반환은
   BE 합의이고, 응답에 ORDER BY가 없어 앞에서 자르면 뒤쪽 관련 상품이 임베딩 재정렬 기회를
   잃는다"*).
3. 그래서 제안은 **(a) 상한값은 크게 + (b) 절단 순서를 결정적으로 + (c) `totalCount` 동반** 3종
   세트다. 하나만 받아들이면 나머지 둘의 위험이 그대로 남는다.

   **(a) 권고값은 3초 예산에서 역산한다** — 바이트 헤드룸이 아니라 **시간 예산**이 근거여야
   한다. 앞선 초안은 "12.3MB의 44%라 안전하다"고 적었는데, **12.3MB는 예산을 초과해 실패한
   값**이다. 실패한 값의 몇 %라는 이유로는 안전을 논증할 수 없다 — 아래처럼 처리율로 역산한다.

   - **처리율**: 이슈 실측 하나뿐이다 — 12.3MB / 7.74s ≈ **1.6 MB/s**(BE 직렬화 + 네트워크
     전송 + AI 파싱을 뭉뚱그린 운영 환경 종단 값). **단일 관측에서 나온 거친 추정**이라는 것을
     명시한다 — 로컬 컨테이너 내부 실측(`MEASURE-I1-RESPONSE-132.md` §3: 13.33MB/1.112s ≈
     12 MB/s)은 loopback이라 이 예산 산정에는 쓰지 않는다(네트워크 구간이 없어 훨씬 빠르다).
   - **남는 창**: 1회 호출 예산은 `spring_timeout_s = 3.0s`(기본, 재시도 0)다. 그중 BE 쿼리가
     이미 최악 1.11초를 쓰므로(`MEASURE-I1-RESPONSE-132.md` §3), **전송·파싱에 남는 창은 약
     3.0 − 1.11 ≈ 1.9초**다.
   - **N=1,000/3,000을 이 창에 넣어 본다**(§3.3 BE 덤프 실측 평균 × N ÷ 1.6MB/s, MB는 처리율과
     같은 10⁶B 단위):

     | | 1건당 | N=1,000 | N=3,000 |
     |---|---:|---:|---:|
     | 다이어트 적용 | 747 B | 0.75MB ≈ 0.5s | 2.24MB ≈ 1.4s |
     | 다이어트 미적용 | 1,835 B | 1.8MB ≈ 1.1s | **5.5MB ≈ 3.4s — 1.9초 창 초과, 총예산(3.0s)도 초과** |

   **결론이 협의 2(필드 축소)의 수용 여부에 갈린다** — 이게 이 절에서 가장 중요한 대목이다.
   협의 2가 함께 반영되면 N=3,000도 1.4s로 1.9초 창 안에 여유 있게 들어온다(역산 상한은
   1.9×1.6MB/747B ≈ 4,070건 — 3,000은 그 아래 안전 마진). 협의 2가 미반영이면 N=3,000(3.4s)은
   창을 넘고 총예산도 넘긴다 — 미반영 시 안전한 상한은 1.9×1.6MB/1,835B ≈ 1,657건이다.

   그래서 제안은 **조건부**다:
   - **협의 2가 함께 반영되면**: 권고 기본 **1,000** / 하드 상한 **3,000**.
   - **협의 2가 미반영이면**: 하드 상한을 **1,500 수준**으로 낮춰 주시기 바란다(역산 상한
     1,657건에 여유를 둔 값). 권고 기본은 1,000을 유지해도 무방하다(1.1s로 창 안).

   1,000은 오늘 이미 존재하는 무필터 최악(매칭 7,245건)을 **의도적으로 절단한다** — 그래서
   (b)·(c)가 필수다. 파싱 자체는 §3.5에서 본 대로 이 규모 전부에서 여유가 있어 **제약이
   되지 않는다** — 상한값을 정하는 근거는 파싱 시간이 아니라 **전송을 포함한 종단 시간
   예산**이다(정직하게 밝힌다 — 처음 초안의 "바이트 헤드룸" 논증은 폐기한다).

   **fan-out 유의사항**: 위 표는 **호출 1건** 기준이다. 한 턴은 leg를 최대 5개(`category_fanout_
   max`, config 기본값) 동시에 던지고 완화 probe까지 겹치면 실측 동시 15다(`MEASURE-I1-
   RESPONSE-132.md` §5). 상한은 **호출당**이라 턴 전체가 주고받는 바이트는 그 배수가 될 수
   있다. 오늘은 큰 응답(무필터·단일 호출)과 높은 동시성(리프 카테고리 fan-out·작은 응답)이
   같은 턴에서 겹치지 않지만, `MEASURE-I1-RESPONSE-132.md` §6이 명시하듯 **그 분리는 보장이
   아니라 우연**이다 — 상한값을 (b)·(c) 없이 무한정 키우면 안 되는 이유이기도 하다.

   **(b) 절단 순서** — 무엇이 적절한 정렬 기준인지는 BE가 안다(가격? id? 재고?). AI가 요청하는
   것은 "무엇이든 **결정적**이어야 하고, 그 기준을 알려 달라"이다. 이 요청이 BE에 filesort 비용을
   지운다는 것도 정직하게 밝힌다 — 다만 상한을 두면 그 비용은 **전량 정렬이 아니라 상한 크기의
   top-N 선택**이다(오늘도 이미 `ORDER BY` 없는 그룹핑 쿼리가 최악 1.11초를 쓰고 있다 —
   `MEASURE-I1-RESPONSE-132.md` §3).

   **(c) `totalCount` 필수** — 없으면 AI가 **조용히 틀린다**. 코드로 입증한다:
   `ProductSearchResult.total_count`는 파싱된 항목 수다(`app/services/spring_client.py::
   _parse_search_response`, `total_count=len(products)`) — "매칭 전체 수"가 아니라 **"이번에
   받은 수"**다. 추천 그래프가 이 값으로 다음을 가른다(`app/agents/buyer/recommendation/graph.py`):
   카테고리 확장 후 0건 판정(`decision.category_expanded and search_result.total_count == 0`),
   자동 완화 진입/재판정, 완화칩 `estCount`, 억제-후 재판정(F-1). BE가 `size` 상한으로 절단하는
   순간 이 값은 "받은 수"로 변질되고, AI는 실제로 더 있는 매칭을 "조건에 맞는 상품이 N개뿐"이라고
   오판·안내하게 된다. **이 절이 §7 회귀 테스트 `test_truncated_response_parses_and_total_count_
   follows_received_count`와 짝이다** — 그 테스트가 정확히 이 동작(총량이 아니라 받은 수를
   따라간다는 것)을 코드로 고정한다.

## §5 협의 2: 필드 축소

3층으로 나눠 제시한다(BE가 부분 수용하기 쉽도록):

- **1층(가장 큰 효과·가장 안전)**: `attributes` 안의 AI 미사용 키 — `_extra`(review_pros·
  review_cons·situation_tags·visual_features)·`_source_pid`·`_domain`·`_category`. §3.2 실측으로
  attributes 바이트의 61.0%(전체의 49.9%)를 차지한다. §2에서 소비처가 하나도 없음을 확인했다.
- **2층**: 최상위 미소비 필드 — `summary`·`options`·`optionCount`(§2에서 소비 없음 확인). 다만
  §2에서 밝혔듯 "소비 계획이 없다"를 단정할 근거는 못 찾았다 — 계획이 살아 있다면 알려 주시기
  바란다. 없다면 이 3개 필드를 빼 주시기 바란다.
- **3층(대안)**: 위 제외가 어려우면 `fields=` 선택 파라미터나 경량 응답 모드를 검토해 주시기
  바란다.

**바이트 말고도 이유가 하나 더 있다.** BE의 `keyword` 필터가 `attributes` **JSON 전문**을 LIKE로
훑는다 — `ProductRepository#searchCandidates`(BE `origin/main`) 쿼리 조건: `lower(p.attributes)
like lower(concat('%', :keyword, '%'))`. `_extra`의 리뷰 문장·시각 묘사문이 그 LIKE 대상에 들어가므로
**키워드 오탐의 출처**다. 이건 가설이 아니라 BE가 이미 **같은 현상을 `color` 축에서 실측하고
고쳤다** — 같은 쿼리의 Javadoc이 이렇게 적었다(BE `origin/main`, 확정 코멘트 "2026-08-03 LLM팀
실측 합의"): *"구 구현은 attributes JSON 전문을 LIKE로 훑어 정밀도가 화이트 37.9%였다 —
`_extra.visual_features` 설명문이 오탐 출처였다."* `json_extract`로 색상 키만 좁혀 그 오탐을
없앴다(color 축은 이미 해결). **`keyword`는 아직 전문 LIKE다** — 같은 쿼리 안에서 `p.name`·
`p.summary`·`p.attributes` 세 컬럼을 함께 LIKE로 훑는다. 즉 필드 축소는 전송량뿐 아니라 **검색
정밀도와 스캔 비용**에도 듣는다.

이 키들을 빼 주시기 바란다. 다만 다른 소비자(FE 직접 노출, 관리자 도구 등)가 있어 뺄 수 없는
사정이 있다면 알려 주시기 바란다.

## §6 협의 3: rating/reviewCount 비정규화

**조회 시 집계가 맞는지부터 확인**했다. BE의 I-1 쿼리는 `left join Review r ... group by p.id`로
`count(r), avg(r.rating)`을 계산한다(`ProductRepository#searchCandidates`, BE `origin/main`). BE
주석도 이렇게 적는다: *"평점은 같은 쿼리에서 집계(02 D9) — 후보 수가 무제한이라 id IN 배치 집계를
쓸 수 없다."* BE 저장소(`jarvis-backend origin/main`)의 `docs/backend/schema.sql`의 `review` 테이블 주석도 같은 설계를 확인한다:
*"평점 평균·리뷰 수 컬럼 없음 — 조회 시 집계, 반정규화 안 함(D9)."*

제안은 두 갈래이고 **둘은 배타가 아니다**:

- **근본**: 집계 컬럼 비정규화(리뷰 쓰기 시 갱신). 리뷰 11만여 건(#132 실측, 126,313건)에서
  검색마다 조인+집계가 도는 구조다.
- **중간**: `size` 상한이 생기면(§4) BE 주석이 말한 제약("후보 수가 무제한이라")이 풀려 **id IN
  배치 집계 경로로 돌아갈 수 있다** — 그 경로는 BE에 **이미 있다**. `ReviewService#getStats(
  Collection<Long>)`(`reviewRepository.aggregateVisibleByProductIds(productIds)`, id IN 배치)가
  I-3/인기 상품 경로(`ProductService#getPopularCandidates`)에서 이미 쓰인다 — 그 메서드 Javadoc이
  직접 이렇게 적는다(BE `origin/main`): *"I-3 — 인기 상품을 후보 형식으로. 상위 N건 고정이라
  평점은 배치 집계로 충분하다 — I-1과 달리 후보 수가 유한하다."* **§4가 I-1의 후보 수를 유한하게
  만들면, 이 문장의 전제("I-1과 달리")가 I-1에도 성립하게 된다** — 즉 **협의 1이 협의 3의 중간
  해법을 공짜로 연다.** 확인된 사실이라 적는다.

**인덱스 점검도 요청한다.** BE 저장소(`jarvis-backend origin/main`)의 `docs/backend/schema.sql`의 `review` 테이블에는
`idx_review_product`(`product_id` 단일 컬럼) 인덱스만 보인다 — `(product_id, status)` 복합
인덱스는 이 스키마 사본에서는 보이지 않는다. **우리가 단정하지는 않는다** — 이 사본이 라이브 DB의
전체 인덱스(Hibernate 자동 생성분 등)를 다 담고 있다는 보장이 없어, BE가 직접 확인해 주시기
바란다.

## §7 AI 측 수용 준비 상태

아래 6개 테스트(`tests/unit/test_i1_field_diet_395.py`)가 "어느 필드가 빠져도 파서가 견딘다"를
실증한다. 각 테스트가 보장하는 명제를 1:1로 적는다.

| 테스트 | 보장하는 명제 |
|---|---|
| `test_attributes_survives_without_ai_unconsumed_keys` | `attributes`에서 `_extra`·`_source_pid`·`_domain`·`_category`가 빠져도 정상 파싱되고, 소비되는 축(명시 속성 하드매칭)은 살아 있다 |
| `test_summary_missing_still_parses_with_other_fields_intact` | `summary`가 응답에 아예 없어도 파싱되고 나머지 필드(가격·평점·카테고리·브랜드)는 온전하다 |
| `test_options_and_option_count_missing_still_parses` | `options`·`optionCount`가 응답에 없어도 파싱된다 |
| `test_attributes_entirely_absent_still_parses_and_search_unaffected` | `attributes` 자체가 통째로 없어도 파싱되고, 속성 조건 없는 검색 경로가 영향받지 않는다 |
| `test_diet_response_yields_same_post_filter_result_as_full_response` | 미사용 필드가 전부 빠진 "다이어트 응답"도 사후필터(가격·평점·명시 속성 매칭)가 기존 응답과 **같은 결과 집합**을 낸다 |
| `test_truncated_response_parses_and_total_count_follows_received_count` | `size` 절단된 응답도 정상 파싱되며, `total_count`는 매칭 전체가 아니라 **받은 개수**를 따라간다(§4-(c)의 근거를 코드로 고정) |

**공허성 검증(변이 시험)**: 6개 테스트 각각에 대해 관련 필드/로직을 일시적으로 엄격하게 바꿔
테스트가 실제로 깨지는지 확인하고 원복했다(diff에 흔적 없음). 결과:

- `SpringProduct.summary`를 `str`(필수)로 변이 → **6개 전부 실패**(모든 테스트 항목이 summary를
  전송하지 않으므로).
- `SpringProduct.options`를 `list[str]`(필수)로 변이 → **6개 전부 실패**(같은 이유).
- `SpringProduct.attributes`를 `dict[str, object]`(필수)로 변이 → attributes를 아예 안 보내는
  3개 테스트(`test_options_and_option_count_missing_still_parses`·
  `test_attributes_entirely_absent_still_parses_and_search_unaffected`·
  `test_truncated_response_parses_and_total_count_follows_received_count`)만 실패, 나머지 3개는
  attributes를 명시 공급하므로 통과 — 기대한 대로 갈렸다.
- `search_service._matches_attr_conditions`를 항상 `True`를 반환하도록 변이(관대 매칭이 전부
  통과로 퇴화하는 버그를 흉내) → `test_attributes_survives_without_ai_unconsumed_keys`(불일치
  조건이 탈락하는지 보는 음성 단언)와 `test_diet_response_yields_same_post_filter_result_as_full_
  response`(소재 불일치 항목 4가 걸러지는지)가 **둘 다 실패**.
- `spring_client._parse_search_response`의 `total_count=len(products)`를 임의 상수(999)로 변이 →
  `test_truncated_response_parses_and_total_count_follows_received_count`가 **실패**.

## §8 BE 송부용 요청문 3건

그대로 복사해 노션/슬랙에 붙일 수 있는 형태다.

---

**요청 1 — I-1 `size` 상한 + 결정적 정렬 + `totalCount`**

(배경) 2026-08-07 운영에서 구매자 추천이 필터 없는 I-1 검색으로 7.74초·12.3MB 응답을 받아
`SEARCH_FAILED`로 떨어졌습니다. AI 쪽에서는 이미 총시간 가드(#132)와 파싱 스레드 격리를 적용해
둔 상태입니다만, 응답 자체의 크기는 AI 쪽에서 줄일 수 없습니다.

(요청) I-1에 `size` 상한을 다시 도입해 주시기 바랍니다. 다만 조건이 있습니다 — (1) 상한은
**저희 쪽 필드 축소 요청(요청 2)이 함께 반영되면 기본 1,000건·하드 상한 3,000건**, **필드
축소가 반영되지 않으면 하드 상한을 1,500건 수준**으로 잡아 주시기 바랍니다(오늘 운영 실측
처리율 12.3MB/7.74초≈1.6MB/초로 역산한 결과이며, 30건 등 소규모는 정렬 없는 현재 쿼리
구조상 사용자가 원한 상품이 잘려나갈 확률이 매우 높습니다), (2) **결정적인 정렬 기준**
(id·가격·최신순 등, 기준은 BE에서 판단해 주시면 됩니다)을 적용해 주시고, (3) 응답에
**`totalCount`**(실제 매칭 전체 건수) 필드를 추가해 주시기 바랍니다.

(우리가 이미 확인한 것) AI는 오늘도 정렬 없는 응답을 그대로 받아 재정렬·필터링만 하고 있어
BE 쪽 정렬 도입에 맞춰 조정할 부분이 없습니다. `totalCount`가 없으면 AI가 "상한에 걸려 잘렸다"와
"정말 그만큼밖에 없다"를 구분하지 못해 사용자에게 잘못된 안내(조건에 맞는 상품이 이것뿐이다)를
할 위험이 있습니다.

(회신에 필요한 답 항목) ① 상한값·정렬 기준으로 어떤 값이 적절할지, ② `totalCount` 추가 가능
여부와 예상 작업 일정 — **그리고 `totalCount`를 어느 위치에 실을지**(§9 초안의 두 형태 (i)
envelope 형제 필드 vs (ii) `data`를 `{items, totalCount}` 객체로 감싸는 형태 중 어느 쪽이
BE에 편한지), ③ 정렬 추가가 쿼리 성능에 미치는 영향(상한 크기의 top-N이라 크지 않을 것으로
예상하지만 BE가 실측해 주시면 감사하겠습니다).

---

**요청 2 — I-1 응답 필드 축소**

(배경) I-1 응답의 81.9%가 `attributes` 필드이고, 그중 절반 이상(전체의 약 44%)이 AI가 전혀
사용하지 않는 `_extra`(리뷰 문장·시각 묘사문 원문)입니다. `summary`·`options`·`optionCount`도
현재 AI 파이프라인에서 읽는 코드가 없습니다.

(요청) 아래 필드를 응답에서 빼 주시기 바랍니다: `attributes._extra`·`attributes._source_pid`·
`attributes._domain`·`attributes._category`(attributes 내부 키), `summary`·`options`·
`optionCount`(최상위 필드). 어려우시면 `fields=` 선택 파라미터 등 대안도 검토 부탁드립니다.

(우리가 이미 확인한 것) 이 필드들이 AI 쪽 어디에서도 소비되지 않는다는 것을 코드 정적 분석과
회귀 테스트로 확인했습니다(`docs/specs/PROPOSAL-I1-DIET-395.md` §2·§7). 만약 다른 소비자(FE
직접 노출, 관리자 도구 등)가 있어 못 빼는 필드가 있다면 알려 주시면 그 필드는 요청에서
제외하겠습니다.

(회신에 필요한 답 항목) ① 위 7개 필드 각각 제외 가능 여부, ② 제외가 어려운 필드가 있다면 그
이유(다른 소비자 등), ③ `keyword` 검색이 현재 `attributes` JSON 전문을 LIKE로 훑고 있어(색상
축은 이미 `json_extract`로 좁혀 오탐을 없애신 것으로 확인했습니다) `keyword`도 같은 방식 적용이
검토 대상인지.

---

**요청 3 — rating/reviewCount 집계 방식 확인**

(배경) I-1이 매 검색마다 `Review` 테이블과 조인해 평점·리뷰수를 집계하고 있는 것으로 코드에서
확인했습니다(리뷰 12만여 건).

(요청) (1) 평점·리뷰수를 상품에 반정규화(리뷰 작성/수정 시 집계 컬럼 갱신)하는 것이 검토
대상인지, 만약 지금 당장은 어렵다면 (2) 요청 1의 `size` 상한이 도입되면 이미 BE에 있는 인기
상품 경로의 id-IN 배치 집계(`ReviewService#getStats`)로 I-1도 전환할 수 있는지 확인 부탁드립니다.
(3) 추가로 `review` 테이블에 `(product_id, status)` 복합 인덱스가 있는지도 확인 부탁드립니다.

(우리가 이미 확인한 것) BE 코드 주석에서 "후보 수가 무제한이라 id IN 배치 집계를 쓸 수 없다"는
설계 근거를 확인했고, 그 배치 집계 경로 자체는 인기 상품 조회(I-3)에 이미 구현돼 있다는 것도
확인했습니다. 요청 1이 받아들여지면 그 제약이 풀릴 것으로 보여 함께 문의드립니다.

(회신에 필요한 답 항목) ① 반정규화 검토 여부, ② 요청 1 수용 시 id-IN 배치 집계 전환 가능 여부,
③ `review(product_id, status)` 인덱스 존재 여부.

---

## §9 api-spec §4.6 개정 초안

**⚠️ 초안일 뿐 적용하지 않는다.** BE 합의 후 별도 PR에서 `docs/api-spec.md`에 반영한다. 이번
PR은 이 문서만 추가하고 정본은 건드리지 않았다.

`docs/api-spec.md` §4.6 "AI → Spring 요청" 표에 아래 행을 되살리는 형태를 제안한다(diff 형태):

```diff
 | `color` | string \| null | 아니오 | 색상 조건. ... |
-| ~~`size`~~ | — | — | **[2026-07-23 개정, BE 합의] 제거됨** — 아래 노트 참조 |
+| `size` | int \| null | 아니오 | **[2026-08-?? 재도입, #395]** 응답 항목 상한(§4 협의 결과에
+따라 기본 1,000/하드 3,000 또는 하드 1,500 — BE 협의로 확정). 매칭이 상한을 넘으면 **결정적
+정렬 기준**(BE 확정 예정)으로 상위 N건만 반환하고 `totalCount`에 실제 매칭 전체 건수를 싣는다. |
```

`totalCount`는 **`data[]` 항목 내부가 아니라 envelope 레벨**이어야 한다 — 상품마다 반복해서
실으면 안 된다. BE가 고를 수 있게 두 형태를 제시한다.

**형태 (i) — envelope 형제 필드** (오늘 envelope `{success, data:[...]}`에 필드 하나 추가):

```diff
 {
   "success": true,
   "data": [
     { "productId": 1, "name": "린넨 셔츠", ... }
-  ]
+  ],
+  "totalCount": 2657
 }
```

**형태 (ii) — `data`를 객체로 감싸 `items`+`totalCount`를 함께 싣는다**(구 wrapped 계약과 같은 모양):

```diff
 {
   "success": true,
-  "data": [
-    { "productId": 1, "name": "린넨 셔츠", ... }
-  ]
+  "data": {
+    "items": [
+      { "productId": 1, "name": "린넨 셔츠", ... }
+    ],
+    "totalCount": 2657
+  }
 }
```

**(ii)는 AI 파서가 이미 호환 수용한다** — `app/services/spring_client.py::_parse_search_response`가
`data:{items:[...]}` 형태를 지금도 받는다(구 계약 호환 분기, `payload.get("items")`만 읽고 형제
키는 무시하므로 `totalCount`가 같이 와도 파싱이 깨지지 않는다). 즉 **(ii)로 가면 envelope 형태
변경만으로는 AI가 깨지지 않는다** — BE의 선택지를 넓히는 사실이라 알려 둔다. (i)로 가면 새 최상위
키 하나가 늘 뿐이라 이쪽도 파서 구조상 문제는 없다(현재는 `data` 외 다른 키를 보지 않을 뿐,
막지도 않는다).

**정직하게 밝혀 둔다**: 어느 형태든 `totalCount` **값을 실제로 읽어 쓰려면 AI 쪽 후속 작업이
1건 필요**하다 — 지금 `ProductSearchResult.total_count`는 `spring_client.py::
_parse_search_response`가 `len(products)`(파싱된 항목 수)로 채우고 있어(§4-(c)), BE가 필드를
추가해도 AI가 그 값을 읽어 대입하도록 바꾸기 전까지는 여전히 "받은 개수"를 쓴다. 그 변경은 이
PR 범위가 아니다 — **BE 합의 후 별도 이슈**로 진행한다.

응답 표(§4.6 "AI가 받는 응답")에 아래 필드를 추가(형태 (i) 기준 — (ii)를 택하면 `data` 자체의
타입 서술을 배열에서 객체로 바꾸고 `items`/`totalCount` 두 줄로 나눈다):

```diff
 | `[].optionCount` | int \| null | ... |
+| `totalCount` | number | **[신설, #395]** 이번 요청의 `size` 절단 여부와 무관한 **실제 매칭 전체
+건수**. `data[]`의 길이(= 이번에 받은 건수)와 다를 수 있다. envelope 레벨 필드이며 `data[]`
+항목 안에는 싣지 않는다. |
```

그리고 아래 필드를 응답 표에서 **삭제**(협의 2 수용 시):

```diff
-| `[].summary` | string \| null | 요약(#100 P0 — rerank/세부조건용, 소비는 #101 2차 압축) |
...
-| `[].options` | string[] \| null | 선택. Spring 송신 계약은 옵션 이름 최대 20개다. ... |
-| `[].optionCount` | int \| null | 선택. 절단 전 전체 옵션 개수(0 이상). ... |
```

`[].attributes`의 서술에 아래 문장을 추가(협의 2 1층 수용 시, 타입 변경 없음 — 값 내용만 축소):

```diff
 | `[].attributes` | object \| null | ... 2차 압축 속성 매칭 대상 |
+  **[#395]** `_extra`(리뷰 원문·시각 묘사문)·`_source_pid`·`_domain`·`_category`는 AI가 소비하지
+  않는 내부 키라 BE 응답에서 제외한다(값 형태·타입 계약은 불변).
```

## §10 비범위·후속

- 골든셋 재실행·실 LLM 호출·DB 기동이 필요한 실험은 하지 않았다(비용·시간, 패킷 §8). 로컬
  픽스처와 정적 근거로 결론을 냈다.
- `--body-file`로 실 BE 응답을 넣는 파싱 시간 원본 vs 다이어트 대조(§3.5에서 언급)는 하지
  않았다 — 로컬에 BE·DB를 띄워야 하는 실험이라 이번 PR 범위 밖이다.
- `docs/api-spec.md` 정식 개정, `app/schemas/spring.py`·`spring_client.py`의 실제 필드 축소
  구현, `app/core/config.py`에 `size` 상한 관련 튜너블 추가는 **BE 합의 후** 별도 이슈·PR로
  진행한다.
- `summary`가 향후 소비될 계획(#101 2차 압축 등)이 실제로 살아 있는지는 이 이슈에서 확정하지
  못했다 — §5 요청문 2에서 BE·기획 측에 직접 확인을 요청했다.
- **BE 반영 후 재측정 절차**(이슈 #395 완료 조건 "무필터 요청의 응답 시간이 3초 예산 안에
  들어온다(재측정)"을 닫는 데 쓴다) — 새 스크립트를 만들지 않고 기존 것으로 한다:
  (a) `X-Internal-Token`으로 `/internal/products/search`를 무필터 최악 질의로 직접 호출해
  바디 크기·소요를 잰다(`MEASURE-I1-RESPONSE-132.md` §3이 쓴 방식 그대로 — BE가 로컬/스테이징에
  기동돼 있어야 한다). (b) 그 응답 바디를 파일로 저장해 `scripts/measure_i1_field_bytes_395.py
  --dump`가 아니라 **저장한 실 응답 자체**를 육안·`jq`로 확인해 §5가 요청한 필드(`_extra` 등)가
  실제로 빠졌는지 대조하고, 동시에 `scripts/measure_i1_parse_132.py --body-file <저장한 응답>`
  으로 파싱 소요를 잰다(§3.5가 이미 이 스크립트를 쓴다 — 다이어트 반영 전/후 바디를 각각 넣어
  대조). (c) 같은 무필터 질의를 2회 연속 호출해 반환된 `productId` 순서(상한 절단 시 앞 N건)가
  두 응답에서 동일한지 확인한다 — 다르면 §4-(b)가 요청한 "결정적 정렬"이 지켜지지 않은 것이다.
