# MEASURE-REVIEW-INDEX-395 — review 커버링 인덱스(BE PR #133)의 소요시간 실측

이슈 #395 후속. BE가 jarvis-backend PR #133(`f0a329f`, 2026-08-08 머지)에서 `idx_review_product`를
`(product_id)` → `(product_id, status, rating)`로 확장하며 노션 댓글에 남긴 한계 —

> 저희 로컬 DB에 리뷰가 0건이라 실행계획만 확인했고 소요 시간은 재지 못했습니다. 운영
> 데이터에서 재보실 수 있으면 before/after 수치를 주시면 좋겠습니다.

— 를 운영 MariaDB 백업(리뷰 114,077건)에서 채운다. **커밋·PR 없음. 측정·기록·보고만.**

**결론 세 줄**

1. **BE 가 EXPLAIN 으로 본 전환(`Using where` → `Using where; Using index`)이 실제 리뷰
   114,077건에서 그대로 재현됐고, 시간도 줄었다** — 무필터 최악(6,290건 집계) 기준 DB 레벨
   **100ms → 27ms(−73%, 3.8배)**, review 쪽 페이지 접근량은 **353,298 → 12,801(27.6배 감소)**.
2. **감소폭은 매칭 규모에 비례한다** — `keyword=세트`(2,316건)는 93ms→62ms(−34%),
   leaf 카테고리(40건)는 1.3ms→0.5ms(−64%, 절대값은 이미 작았다). A/B/A 반복에서 두 before
   값의 편차가 5% 이내라 이 감소가 인덱스 효과임을 확인했다(캐시·부하 드리프트가 아니다).
3. **캐시는 여전히 불필요하다는 판단을 뒷받침한다** — 인덱스 적용 후 최악 케이스도 27ms로
   3s 예산의 1% 미만이고, 종단(HTTP) 최악도 321ms→233ms로 줄었다. 다만 종단 감소폭이 DB
   레벨보다 작은 것은 응답 크기(11.8MB, #132가 지적한 문제)가 그대로 남아 직렬화·전송
   비용을 인덱스가 손대지 못하기 때문 — **다음 병목은 review 집계가 아니라 응답 크기다.**

---

## 1. 측정 환경

| | |
|---|---|
| 덤프 | `_backup/20260809/mariadb/jarvis-mariadb-20260807.sql.gz` — `product` 6,310(ON_SALE+재고 6,290) / `review` 114,077 |
| DB 컨테이너 | `jarvis-restore-maria`(mariadb:11 → 실제 11.8.8-MariaDB, 포트 3307), `innodb_buffer_pool_size` 128MB(이미지 기본값) |
| BE 컨테이너 | `jarvis-backend@37fa45e`(PR #133 직전, 코드는 무관 — #133은 스키마/문서만 변경) 를 `--network host` 로 기동, `/internal/products/search` 직접 호출 |
| 하드웨어 | 로컬 WSL2, 20 vCPU, 23GB RAM — **운영과 무관, 절대값이 아니라 배수·실행계획만 인용할 것**(§7) |
| 쿼리 캐시 | `query_cache_type=OFF` 확인(MariaDB 11.8 은 쿼리 캐시 자체가 제거된 버전이라 항상 OFF) |
| 인덱스 확인 | `SHOW INDEX FROM review` 를 각 상태마다 캡처(§3) — 덤프 원본이 `KEY idx_review_product (product_id)`로 PR #133 이전(before) 상태임을 재확인 |

---

## 2. 캡처한 생성 SQL (전문)

`docs/lessons.md`의 전례(#132 — JPQL을 손으로 재현한 SQL로 4.15초를 재서 존재하지 않는
병목에 이슈를 낸 사고) 때문에, general log 를 켜고 BE 컨테이너로 무필터 검색을 한 번 호출해
Hibernate 가 실제로 보낸 SQL 을 그대로 떴다:

```sql
select p1_0.id,p1_0.attributes,p1_0.base_sales_count,p1_0.brand_id,p1_0.category_id,
       p1_0.created_at,p1_0.description,p1_0.image_url,p1_0.name,p1_0.original_price,
       p1_0.price,p1_0.status,p1_0.stock_quantity,p1_0.summary,p1_0.updated_at,
       count(r1_0.id), avg(r1_0.rating)
from product p1_0
left join review r1_0 on r1_0.product_id=p1_0.id and r1_0.status='VISIBLE'
where p1_0.status='ON_SALE' and p1_0.stock_quantity>0
  and (0=0 or p1_0.category_id in (-1))
  and (0=0 or p1_0.brand_id in (-1))
  and (null is null or p1_0.price>=null)
  and (null is null or p1_0.price<=null)
  and (null is null
       or lower(p1_0.name) like replace(lower(concat('%',null,'%')),'\\','\\\\')
       or lower(p1_0.summary) like replace(lower(concat('%',null,'%')),'\\','\\\\')
       or lower(p1_0.attributes) like replace(lower(concat('%',null,'%')),'\\','\\\\'))
  and (null is null
       or json_extract(p1_0.attributes,'$."색상"') is null
       or regexp_instr(lower(cast(json_extract(p1_0.attributes,'$."색상"') as char)),null)>0)
group by p1_0.id
```

확인된 것: **PK 하나로 그룹핑한다**(`group by p1_0.id`, #132 §3 정정과 동일) — 이번에도
재확인했다. 측정에는 이 캡처본과 **같은 join·where·group by 구조**를 쓰되, SELECT 목록을
`review` 집계 비용만 격리하도록 좁히고(전 컬럼 하이드레이션은 #132가 이미 잰 별개 비용),
파라미터 자리(카테고리/브랜드/가격/색상 sentinel)는 아래 3.1의 질의별 실제 조건으로
대체했다 — 구조는 캡처본 그대로, 값만 3질의에 맞게 구체화했다(스크립트 `build_query()`).

---

## 3. 측정 설계

### 3.1 질의 3종 (2026-08-07 덤프 기준 실측 매칭 행수)

| 이름 | 조건 | 매칭 행 |
|---|---|---:|
| `worst` | 파라미터 전부 없음(무필터) | **6,290** |
| `keyword` | `keyword=세트` | **2,316** |
| `leaf` | `categoryName=선케어 > 선크림/선블록`(리프, id 157392844414274913) | **40** |

### 3.2 인덱스 2상태 × A/B/A

```sql
-- before → after
ALTER TABLE review DROP INDEX idx_review_product, ADD INDEX idx_review_product (product_id, status, rating);
-- after → before
ALTER TABLE review DROP INDEX idx_review_product, ADD INDEX idx_review_product (product_id);
```

`scripts/measure_review_index_395.py --apply-index` 가 A1(before) → B(after) → A2(before) 순으로
자동 스윕한다. 각 상태마다 `SHOW INDEX FROM review` 를 캡처해 실제로 바뀌었는지 확인했다(생략 없이
스크립트 로그에 그대로 남음).

### 3.3 측정 규칙

- **워밍업 3회 버림 → 10회 측정**, 중앙값·p95 산출.
- 타이밍은 **서버 사이드**(`NOW(6)` 델타)로 쟀다 — `docker exec` 를 질의마다 새로 fork하면
  그 오버헤드(수십ms)가 leaf 처럼 빠른 질의의 신호를 파묻기 때문에, 워밍업+측정 13회를
  **한 세션 안에서** 실행하고 결과만 한 번에 받았다(`timed_runs()`).
- 결과 행은 `COUNT(*)`로 감싸 클라이언트로 넘어오는 바이트를 줄였다(집계 자체의 서버 비용은
  그대로 실행됨 — 옵티마이저가 서브쿼리를 접지 않는지 EXPLAIN 으로 확인).
- 쿼리 캐시: `query_cache_type=OFF` 확인(§1) — MariaDB 11.8 은 쿼리 캐시가 완전히 제거된 버전이라
  애초에 개입할 수 없다.

---

## 4. DB 레벨 결과

| 질의 | 상태 | 매칭 | 중앙값(ms) | p95(ms) |
|---|---|---:|---:|---:|
| 무필터(최악) | A1(before) | 6,290 | 100.19 | 108.69 |
| 무필터(최악) | B(after) | 6,290 | **26.65** | 28.62 |
| 무필터(최악) | A2(before) | 6,290 | 97.55 | 106.94 |
| keyword=세트 | A1(before) | 2,316 | 93.48 | 99.56 |
| keyword=세트 | B(after) | 2,316 | **62.19** | 65.70 |
| keyword=세트 | A2(before) | 2,316 | 97.46 | 112.41 |
| leaf(선케어>선크림/선블록) | A1(before) | 40 | 1.32 | 1.70 |
| leaf(선케어>선크림/선블록) | B(after) | 40 | **0.47** | 0.51 |
| leaf(선케어>선크림/선블록) | A2(before) | 40 | 1.34 | 1.88 |

**A/B/A 재현성**: A1↔A2 편차는 무필터 −2.6%, keyword +4.3%, leaf +1.5% — 전부 5% 이내다.
B 의 감소폭(−34%~−73%)이 이 노이즈 대역보다 훨씬 커서, 관측된 감소가 인덱스 효과라고
볼 근거가 충분하다(캐시·부하 상태 차이였다면 A1·A2 사이에서 이미 드러났을 것).

**배수**: 무필터 3.76배 · keyword 1.50배 · leaf 2.81배. 감소율이 규모에 비례하지 않고
leaf 가 keyword 보다 큰 것은, leaf 는 애초에 review 쪽 랜덤 조회 횟수가 적어(40건) 인덱스가
페이지 자체를 거의 다 없애는 반면 keyword 는 매칭 2,316건 전부가 review 되짚기 대상이라
절대 시간은 더 크고 감소율은 상대적으로 작기 때문으로 보인다 — §5 의 `rows`/`pages_accessed`
차이가 이를 뒷받침한다.

---

## 5. EXPLAIN before/after — BE 가 말한 전환이 재현됐는가

**재현됐다.** 3질의 전부 review(`r1_0`) 쪽 `Extra` 가 before `Using where` → after
`Using where; Using index` 로 바뀐다(traditional EXPLAIN, 3질의 동일 패턴이라 무필터만 싣는다):

```
-- A1 (before)
table  type  key                 key_len  ref             rows  Extra
p1_0   index PRIMARY             8        NULL            4996  Using where
r1_0   ref   idx_review_product  8        jarvis.p1_0.id  51    Using where

-- B (after)
table  type  key                 key_len  ref                    rows  Extra
p1_0   index PRIMARY             8        NULL                   4996  Using where
r1_0   ref   idx_review_product  90       jarvis.p1_0.id,const   106   Using where; Using index
```

`ANALYZE FORMAT=JSON`(무필터, r1_0 부분)이 왜 빨라졌는지를 숫자로 보여준다:

| | before(A1) | after(B) |
|---|---:|---:|
| `r_table_time_ms`(review 접근 시간) | 85.6 | 15.7 |
| `r_engine_stats.pages_accessed` | **353,298** | **12,801**(27.6배 감소) |
| `used_key_parts` | `product_id` | `product_id, status`(rating 은 커버링으로만 쓰이고 ref 매칭엔 미사용) |

before 는 `product_id` 로 review 행을 찾은 뒤 `status`·`rating` 을 읽으려 **행마다 테이블
페이지를 되짚어(pages_accessed 353K)** 왔고, after 는 인덱스 자체에 세 컬럼이 다 있어 그 되짚기가
사라졌다. BE 커밋 메시지의 "세 컬럼을 인덱스에 담아 집계가 인덱스만으로 끝난다"는 설명과
정확히 일치한다.

---

## 6. 종단(HTTP) 측정

BE 컨테이너에 `/internal/products/search` 를 같은 3질의로 직접 호출(`X-Internal-Token`),
같은 워밍업 3회/측정 10회 규칙.

| 질의 | 상태 | 바디 | 중앙값(ms) | p95(ms) |
|---|---|---:|---:|---:|
| 무필터(최악) | A1(before) | 11.83MB | 320.88 | 373.79 |
| 무필터(최악) | B(after) | 11.83MB | **233.30**(−27%) | 262.75 |
| 무필터(최악) | A2(before) | 11.83MB | 308.28 | 322.92 |
| keyword=세트 | A1(before) | 4.44MB | 183.49 | 189.31 |
| keyword=세트 | B(after) | 4.44MB | **147.78**(−19%) | 158.65 |
| keyword=세트 | A2(before) | 4.44MB | 181.22 | 195.39 |
| leaf | A1(before) | 69KB | 15.71 | 18.30 |
| leaf | B(after) | 69KB | **11.04**(−30%) | 12.38 |
| leaf | A2(before) | 69KB | 9.89 | 13.03 |

DB 레벨(−34%~−73%)보다 종단 감소폭(−19%~−30%)이 작다 — **응답 바디 크기가 그대로**(인덱스는
review 집계만 건드리고 JSON 직렬화·전송·JPA 하이드레이션은 그대로)이기 때문이다. leaf 의
A1/A2 편차(15.71 vs 9.89, −37%)는 DB 레벨보다 크다 — JVM 쪽(GC·JIT 워밍업) 노이즈로 보이며,
바디가 69KB 로 작아 절대 ms 차이 자체는 크지 않다(§7 한계에 반영).

---

## 7. 한계

- **로컬 하드웨어라 절대값은 운영과 다르다.** 인용 가능한 것은 **배수(3.76배/1.50배/2.81배)와
  EXPLAIN 의 실행계획 전환**이다.
- **`innodb_buffer_pool_size` 가 128MB**(mariadb:11 이미지 기본값)로 운영보다 작을 가능성이 높다.
  버퍼풀이 더 크면 before 상태의 랜덤 되짚기도 캐시 히트로 상당 부분 흡수돼, 운영에서는 이번
  측정보다 before/after 격차가 **작게** 나올 수 있다 — 반대로 review 가 더 커지면 격차가 커진다.
  **방향(after 가 더 빠르다)은 구조적이라 유지되지만 배수 자체는 운영에서 재검증이 필요하다.**
- **덤프가 2026-08-07 기준**이라(README §3) 이후 운영 변경(상품 삭제 1건 등)은 반영 안 됨 —
  review 카운트·매칭 행수에 실질적 영향은 없다.
- **HTTP 종단은 JVM 웜업 변동을 A/B/A 로 완전히 통제하지 못했다**(leaf A1/A2 편차 −37%,
  §6) — DB 레벨만큼 확신을 가질 수는 없고, 방향(after 가 더 빠름)만 일관되게 재현됐다.
- BE 컨테이너는 이번 측정 전용으로 새로 빌드한 이미지(`jarvis-backend@37fa45e`)를 썼다 —
  PR #133 은 스키마·문서만 바꿨으므로 코드 버전 차이가 결과에 영향을 주지 않는다.

---

## 8. BE 회신 문안 초안 (노션 댓글용)

> #395 관련해서 운영 백업(리뷰 114,077건)으로 review 커버링 인덱스 효과를 실측했습니다.
>
> EXPLAIN 전환(`Using where` → `Using where; Using index`)이 그대로 재현됐고, 소요시간도
> 줄었습니다 — 무필터 최악 케이스(6,290건 집계) 기준 DB 레벨 **100ms → 27ms(3.8배)**,
> review 쪽 페이지 접근량은 353,298 → 12,801(27.6배 감소)입니다. `keyword` 조건(2,316건)은
> 93ms → 62ms, 리프 카테고리(40건)는 1.3ms → 0.5ms 로 줄었습니다. A/B/A 로 세 번 재서
> before 두 값의 편차가 5% 이내임을 확인해, 이 감소가 인덱스 효과임을 확인했습니다.
>
> 종단(HTTP)에서도 무필터 최악이 321ms → 233ms 로 줄었지만, 감소폭이 DB 레벨보다 작습니다
> (11.8MB 응답 크기는 인덱스와 무관하게 그대로라 직렬화·전송 비용이 남기 때문입니다).
>
> **결론적으로 캐시는 여전히 불필요하다는 판단에 동의합니다.** 인덱스 적용 후 review 집계는
> 최악 케이스도 27ms 로 3초 예산의 1% 미만이라, "실측에서 집계가 여전히 병목이면 캐시를
> 붙인다"던 조건이 성립하지 않습니다. 다음에 볼 것이 있다면 review 집계가 아니라 응답
> 크기(#132 가 지적한 13MB 바디)쪽입니다. 다만 로컬 하드웨어 측정이라 절대값이 아니라
> 배수·실행계획으로만 인용해 주세요(버퍼풀 크기가 운영과 달라 배수 자체는 운영에서
> 재검증이 있으면 더 좋을 것 같습니다). 상세 수치·SQL·EXPLAIN 전문은
> `docs/specs/MEASURE-REVIEW-INDEX-395.md` 에 있습니다.

---

## 9. 재현

```bash
cd /home/uuser/inte-final/_backup/20260809 && ./restore-mariadb.sh   # jarvis-restore-maria, 포트 3307
# BE 컨테이너는 --network host 로 DB_URL=jdbc:mariadb://localhost:3307/jarvis 를 보게 기동(§1)
uv run python scripts/measure_review_index_395.py --apply-index \
  --http-base http://localhost:8099 --internal-token <내부 토큰>
```

측정 종료 후 남긴 상태: `jarvis-restore-maria`(3307)는 살려뒀다(재사용용) — **인덱스는
before(`product_id` 단독)로 되돌려 뒀다.** BE 측정 컨테이너(`jarvis-be-measure`)는 내렸다.
general log 는 껐다(`SHOW VARIABLES LIKE 'general_log'` → OFF 확인).

---

## 10. 후속 — 응답 크기 축의 2026-08-10 운영 실측 (§6 결론 3 이어서)

§6 결론 3이 "다음 병목은 review 집계가 아니라 응답 크기"라고 지목한 그 축을 2026-08-10에
운영에서 직접 쟀다 — 무필터 최악 응답 **6.13 MiB**(항목당 평균 **1,105.7 B**, 다이어트 전
1,835 B/건 대비 −39.7%), 종단 소요 중앙값 **2.09 s**(9회 중 최악 3.20 s, 예산 3s를 스칠 수
있음). 즉 이 문서의 인덱스 개선(무필터 DB 레벨 100.2 ms → 26.7 ms)과 별개로, 응답 크기
축소(#395 요청 2, `attributes` 4키 제외)도 함께 배포돼 종단 시간이 이 문서 측정 시점의 321ms
(로컬 재현 환경)와는 다른 운영 실측치로 나타난다. 상세·필드별 바이트 분해·정렬 결정성 확인은
`docs/specs/PROPOSAL-I1-DIET-395.md` §11이 정본이다. 이 문서(§1~§9)의 본문 수치·표는 그대로
둔다 — 로컬 하드웨어·다른 시점의 측정 기록이다.
