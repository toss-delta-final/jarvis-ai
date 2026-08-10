# OPS-SCALEOUT-476 — 오픈 전 동시성 관측과 워커 다중화 선행조건

작성일: 2026-08-09 · 이슈: #476 · 상태: 관측 배선 완료, 증설은 운영 결정 대기

## 결론 세 줄

1. 단일 이벤트 루프에서 검색 응답 파싱이 겹치면 동시 20에서 3초 예산을 넘는다. 따라서 `chat_request`에 도착 시 활성 수와 턴 중 피크를 남겨 실제 임계를 먼저 측정한다.
2. 오늘 `ActiveStreamRegistry`와 여러 직렬화 락은 프로세스 로컬이므로 `--workers` 증설은 조용한 409 우회와 상태 유실을 만든다. 코드 마커·Dockerfile 가드·기동 경고가 이를 막는다.
3. **권고: 1단계는 JWT `sub`(owner) 축 sticky routing이고, 다중 EC2로 가거나 sticky를 보장할 수 없는 순간에는 TTL/만료를 갖춘 공유 레지스트리로 전환한다.** 어떤 LB·배포 토폴로지를 채택할지는 운영·사람 결정이다.

## 1. 실측과 관측 범위

#427/PR #452 코멘트의 무필터 최악 검색 종단 실측이다. Spring 쿼리 자체가 아니라 6.31 MB 응답을 동시에 파싱하면서 단일 이벤트 루프가 막혔다. 응답 축소는 #395 범위이며 이 문서는 그 원인을 고치지 않고, 부하를 판단할 자료를 남긴다.

| 동시 | p50 | 최악 | 3초 예산 |
|---:|---:|---:|---|
| 1 | 0.726s | 0.726s | OK |
| 5 | 1.162s | 1.348s | OK |
| 10 | 2.022s | 2.313s | OK |
| 20 | 4.229s | 5.238s | 초과 → `SEARCH_FAILED` |

`app.core.stream_registry::ActiveStreamRegistry.active_count`는 활성 슬롯만 O(1)로 읽는다. fence는 스코프 예약이지 열린 스트림이 아니므로 세지 않는다. `app.core.stream::open_stream`은 acquire 성공 직후와 409 거절 직전에 표본을 남긴다. 성공 표본은 자기 자신을 포함하고, 409 표본은 슬롯을 얻지 못했으므로 자기 자신을 제외한다. 그 뒤 매 `asyncio.wait` 반환 직후 `RequestObservation`에 다시 기록해 프레임 도착과 유휴 polling tick 양쪽에서 턴 피크를 갱신한다.

`app.core.observability::RequestObservation`의 `activeStreams`는 최초 표본(요청 도착 시 부하), `activeStreamsPeak`는 그 턴의 최대 표본이다. **두 값 모두 워커별 값이다** — 공유 레지스트리를 켜도 `active_count`는 프레임마다 호출되는 관측 경로라 DB를 치지 않고 이 워커의 슬롯만 센다(의미도 "이 이벤트 루프가 얼마나 붐비는가"라서 단일 이벤트 루프 포화 판정이라는 원래 목적에 더 맞다). 그래서 같은 `chat_request` 행에 `workerFp`(`app.core.observability::worker_fingerprint`, 프로세스 시작 시 1회 생성한 인스턴스 id의 `identifier_fingerprint`)를 함께 남긴다 — 다중 워커 로그를 워커별로 갈라야 이 지표를 해석·합산할 수 있다. 랜덤 uuid라 PII가 아니다. 같은 행의 `searchCalls`·`searchCandidatesMax`·`searchTotalCountMax`·`searchElapsedMsMax`는 검색 호출 수와 후보 수·I-1 전체 건수·종단 지연의 최대를 함께 남겨, 동시성 자체와 응답 크기 가설을 구분하는 근거가 된다. 이 값들은 와이어 계약을 바꾸지 않는다.

## 2. 프로세스 로컬 상태 인벤토리

아래는 모듈 전역 가변 상태를 `git grep`으로 훑고, 다중 워커에서 정합성·상한·직렬화에 직접 영향을 주는 항목을 분류한 결과다. 연결 풀·클라이언트 캐시처럼 수명주기만 프로세스별인 항목은 별도 이관 대상이 아니며, 여기서는 데이터 손실 또는 중복 실행 위험이 있는 상태를 우선한다.

| 상태 / 심볼 | 현재 범위 | 다중 워커 파손 모드 |
|---|---|---|
| `app.core.stream_registry::ActiveStreamRegistry`의 `_registry` | 프로세스 전역 (**기본값**) | 같은 `owner:threadId` 요청이 서로 다른 워커에 가면 §2.9(a) 409가 우회되어 동시 스트림과 후속 RMW가 겹친다. **해소 가능**: `STREAM_REGISTRY_BACKEND=shared`로 pg-profile 공유 백엔드를 켜면 활성 슬롯·scope fence·scope idle이 워커 간에 공유된다(`docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md`). 아래 나머지 항목은 그대로 남는다. |
| `app.agents.buyer.cart.state::CartStateStore.set_last_reco` | 락 없는 저장소 RMW | 두 워커가 같은 누적 추천을 읽고 쓰면 한 턴의 추천이 통째로 사라져 담기 허용 목록에서 빠진다. |
| `app.agents.seller.hitl::_confirm_locks` | 프로세스 내 `draftId` 락 | 같은 draft confirm이 워커별 락을 각각 얻어 도구 실행·확정이 중복 경합한다. |
| `app.core.ratelimit::SlidingWindowLimiter`의 in-memory 카운터 | 프로세스 전역 | 워커마다 별도 버킷을 가지므로 실효 분당·시간당 상한이 워커 수만큼 늘어난다. `docs.api-spec.md` §2.8은 다중 인스턴스 시 Redis 이관을 이미 단서로 둔다. |
| `app.agents.buyer.session_state::_adoption_locks`와 `_legacy_root_memory_fence` | 프로세스 내 세션 직렬화 | 세션 claim/adoption이 서로 다른 워커에서 동시에 진행되어 세션 전환·레거시 메모리 경계가 교차할 수 있다. |
| `app.agents.buyer.recommendation.state::_add_locks`와 `_repurchase_add_locks` | 프로세스 내 thread별 RMW 락 | 추천·재구매 상태의 get→put 직렬화가 워커 경계를 넘지 못해 마지막 쓰기 유실 위험이 남는다. |
| `app.agents.profile.store::_session_locks`, `_fact_locks`, `_summary_locks`, `_graph_locks` | 프로세스 내 profile RMW 락 | 같은 회원 프로필의 dedup·cap trimming·그래프 병합이 교차해 읽은 상태를 다른 쓰기가 덮을 수 있다. |
| `app.agents.seller.history::_save_locks` | 프로세스 내 seller 저장 락 | 동일 판매자 저장의 순서 보장이 사라져 history 저장 경합이 난다. |

이 표는 워커 증설 전 각 상태를 공유 락·DB 원자 연산으로 바꾸거나, 아래 owner sticky로 같은 소유자의 작업이 같은 워커로 가는지 확인해야 한다는 체크리스트다. 외부 배치의 `app.pipelines.artifacts_batch::_content_retry_queue`와 프로세스별 limiter/cache도 검색에서 발견했으나, 채팅 워커 증설의 직접 경로가 아니므로 배치 토폴로지 변경 때 별도 평가한다.

## 3. 선택지와 권고

| 안 | 장점 | 한계 / 필수 조건 |
|---|---|---|
| owner(`JWT sub`) sticky routing | 기존 프로세스 로컬 락을 당장 공유 저장소로 옮기지 않고도 한 owner의 채팅·세션·판매자 draft를 한 워커에 모은다. | LB가 sticky를 보장해야 하며, 다중 EC2·장애 전환·재분배에서 보장이 약하면 정확성 근거가 사라진다. |
| 공유 레지스트리(Redis 또는 pg) | sticky가 불가능하거나 여러 EC2를 쓸 때 워커 간 §2.9(a) 슬롯을 정확히 공유한다. | 슬롯 lease에 TTL/만료가 필수다. 프로세스가 죽은 뒤 슬롯이 남으면 그 방은 영구 409가 된다(#48의 슬롯 누수와 같은 성질). 위 표의 다른 로컬 RMW/락도 별도로 처리해야 한다. |

sticky key를 `threadId`로 하면 충분하지 않다. `app.core.stream::ActiveStreamRegistry.acquire_fence`, `wait_for_scope_idle`, `_scope_idle`는 `(owner_id, session_id)` 스코프를 쓴다. 같은 세션의 다른 `threadId`가 다른 워커로 가면 fence/idle 보장이 깨진다. 반대로 `app.agents.seller.hitl::_confirm_locks`는 `draftId` 축이다. `app.core.stream::registry_key`가 `owner:threadId`이므로 세 축을 동시에 만족하는 최소 sticky 키는 owner(JWT `sub`)다.

## 4. 증설 전 체크리스트

1. `activeStreams`와 `activeStreamsPeak` 분포, 409 비율, 검색 지연을 같은 기간에 관측한다.
2. 관측값으로 단일 워커 임계와 증설 필요성을 운영에서 판단한다. 관측 없이 임계를 정하지 않는다.
3. owner sticky를 LB에서 보장하거나, shared registry의 TTL/만료·장애 복구를 구현하고 위 로컬 상태의 정합성 대책을 검증한다.
4. 그 뒤에만 `--workers` 또는 컨테이너/EC2 증설을 사람 승인으로 적용한다.

`app.core.stream_registry::registry_is_process_local`은 하드코딩 상수가 아니라 `STREAM_REGISTRY_BACKEND` 설정에서 파생된다(기본 `memory` → `True`). 가드 테스트(`tests/unit/test_scaleout_guard_476.py`)의 규칙은 **"Dockerfile이 `--workers`/`WEB_CONCURRENCY`로 워커 다중화를 켜려면 같은 파일에서 `STREAM_REGISTRY_BACKEND=shared`를 함께 설정해야 한다"**이다 — 백엔드가 무엇이든 스킵되지 않는다. 기동 시 `WEB_CONCURRENCY >= 2`이고 레지스트리가 프로세스 로컬이면 경고만 남기며, 기존 배포를 거부하지 않는다. 공유 백엔드를 켜면 이 경고와 가드는 자동으로 완화되지만, **위 인벤토리의 나머지 조건을 검토했다는 뜻은 아니다.**

## 5. 범위 밖

- #395의 전량 반환 축소와 응답 파싱 최적화
- #427이 observe로 둔 검색 타임아웃 값 조정
- 백프레셔, 429 동시 상한, 모델 티어 강등
- Dockerfile·배포 workflow 수정, 실제 `--workers` 활성화
- Redis/pg 공유 레지스트리 및 다른 프로세스 로컬 락의 구현
