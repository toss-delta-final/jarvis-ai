# DESIGN-SHARED-STREAM-REGISTRY-476 — 워커 간 공유 스트림 레지스트리

작성일: 2026-08-09 · 이슈: #476(완료 조건 3) · 상태: 구현 완료, **출하 기본값은 기존 인메모리**

`docs/specs/OPS-SCALEOUT-476.md` §3 이 "sticky 를 보장할 수 없으면 TTL/만료를 갖춘 공유
레지스트리로 전환한다" 로 남겨둔 항목의 구현 설계다. 계약(`docs/api-spec.md`)은 바뀌지 않는다 —
엔드포인트·SSE 이벤트·필드·오류 코드가 그대로이고, 공유 백엔드는 **설정으로 켜는 대안 구현**이다.

## 결론 세 줄

1. 레지스트리 API 를 async 로 올리고 백엔드를 두 개 둔다 — `memory`(기본, 기존 동작 그대로)와
   `shared`(pg-profile 테이블 2개 + lease). 셋 다(활성 슬롯·scope fence·scope idle 대기) 공유로
   올린다. 하나라도 로컬로 남기면 세션 claim 경로가 워커 간에 어긋나 증설이 불가능하다.
2. 기존 fence 원자성의 근거였던 "`acquire_fence()` 에 await 가 없다"는 공유 백엔드에서 성립하지
   않는다. 대신 **스코프 키에 건 `pg_advisory_xact_lock` 이 검사와 설치를 한 트랜잭션 안에
   묶는다**(§3). 이벤트 루프 원자성 → 트랜잭션 원자성으로 근거를 갈아끼웠다.
3. 죽은 워커가 슬롯을 영구히 잠그지 않도록 모든 행에 `lease_expires_at` 을 두고, 만료 행은 없는
   것으로 취급하며 살아있는 스트림만 폴링 tick 에서 lease 를 연장한다(§4).

## 1. 백엔드 선택과 기본값

`app.core.config::Settings.stream_registry_backend` = `"memory"`(기본) | `"shared"`.

- 현재 배포는 워커 1개다. 공유 백엔드는 이득이 없고 DB 왕복과 새 실패 모드만 추가하므로 **출하
  기본 동작은 이 변경 이전과 완전히 동일**하다. `memory` 경로는 DB 를 전혀 타지 않는다.
- 완료 조건은 "워커 다중화 **시** §2.9(a) 가 유효하다" 이므로 "켤 수 있게 만든다"로 충족된다.
  이 변경은 `Dockerfile`·배포 워크플로를 건드리지 않고 `--workers` 를 켜지도 않는다.
- `app.core.stream::registry_is_process_local` 은 하드코딩 상수가 아니라 이 설정에서 파생된다.
  가드 테스트(`tests/unit/test_scaleout_guard_476.py`)의 규칙도 그에 맞춰 정확해졌다 —
  "마커가 True 면 `--workers` 금지" 가 아니라 **"`Dockerfile` 이 워커 다중화를 켜려면 같은
  파일에서 `STREAM_REGISTRY_BACKEND=shared` 를 함께 설정해야 한다"** 다. 백엔드가 무엇이든
  테스트는 스킵되지 않는다.

새 config(이 기능에 필요한 것만, 기존 타임아웃·예산·상한 값은 건드리지 않는다):

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `stream_registry_backend` | `"memory"` | 백엔드 선택 |
| `stream_registry_lease_ttl_s` | 60.0 | 활성 슬롯·fence 행의 lease 수명 |
| `stream_registry_lease_renew_interval_s` | 5.0 | 활성 슬롯 lease 최소 연장 간격 |
| `stream_registry_scope_poll_s` | 0.5 | `wait_for_scope_idle` 폴링 주기 |
| `stream_registry_scope_idle_wait_max_s` | 120.0 | `wait_for_scope_idle` 전체 상한 |

교차 검증(`app.core.config::Settings._validate_stream_registry_leases`):
`renew_interval < ttl / 2` (연장 한 번 놓쳐도 만료되지 않게), `scope_poll < scope_idle_wait_max`,
`scope_idle_wait_max > lease_ttl` (원격 워커가 죽어도 만료로 반드시 풀리게).

## 2. 계층

- `app.core.stream_registry::ActiveStreamRegistry` — 인메모리 백엔드. 기존 클래스 그대로이되
  변경 연산이 `async def` 다. **본문에 `await` 가 없으므로 코루틴이 이벤트 루프에 양보하지 않고,
  check-then-set 원자성은 이전과 동일하게 공짜로 성립한다.**
- `app.core.stream_registry::SharedStreamRegistry` — 위를 상속해 교차 워커 조정만 덮어쓴다.
  프로세스 로컬 미러(`_active`/`_fences`)는 그대로 유지한다. 미러는 (a) `active_count()`/
  `is_active()`/`is_fenced()` 의 **이 워커 관점** 응답, (b) release 시 자기 행만 지우기 위한
  토큰 보관, (c) lease 연장 대상 목록에 쓰인다.
- `app.core.stream_registry::SharedStreamStore` — 저장소 프로토콜(6개 연산). 구현 2개:
  - `PostgresSharedStreamStore` — pg-profile. 실제 정본.
  - `InMemorySharedStreamStore` — **테스트 전용**. 레지스트리 인스턴스 2개(=워커 2개)가 같은
    저장소를 보게 해 교차 워커 불변식을 단위 테스트로 고정한다.

`app.core.stream` 은 이 심볼들을 재수출한다 — 기존 import 경로(`from app.core.stream import
ActiveStreamRegistry, get_registry, ...`)가 그대로 동작한다.

### 2.1 sync 로 남기는 것

`active_count()`·`is_active()`·`is_fenced()` 는 **sync·프로세스 로컬**로 남는다.

`active_count()` 는 관측용이고 SSE 프레임마다 호출된다(`app.core.stream::_note_active_streams`).
DB 왕복으로 바꾸면 핫 패스가 죽는다. 의미도 "이 이벤트 루프가 얼마나 붐비는가" 라서 이 지표의
원래 목적(단일 이벤트 루프 포화 판정)에 오히려 더 맞다. 그래서 `chat_request` 의
`activeStreams`/`activeStreamsPeak` 는 **워커별 값**이고, 다중 워커에서 로그를 합산할 수 있도록
같은 레코드에 워커 지문 `workerFp`(`app.core.observability::worker_fingerprint`, 프로세스 시작 시
1회 생성한 인스턴스 id 의 `identifier_fingerprint`)를 함께 남긴다. PII 가 아니다.

## 3. fence 원자성 — 0-1 의 대체 근거

기존 코드의 근거는 `app.core.session_lifecycle::SessionLifecycleCoordinator.claim_owner` 주석이
말하듯 "`acquire_fence()` 에 await 가 없어 활성 스코프 검사와 fence 설치가 하나의 이벤트 루프
원자 연산" 이었다. 공유 백엔드에서는 그 공짜 원자성이 사라진다.

대체 근거: **`(owner_id, session_id)` 스코프 키에 건 `pg_advisory_xact_lock`.**

- `acquire(stream_key, owner, session)` (스코프가 있을 때)와 `acquire_fence(owner, session)` 은
  **같은 스코프 락**을 각자의 트랜잭션 시작 직후에 잡는다. 두 연산이 서로의 테이블을
  교차 검사하므로(전자는 fence 를, 후자는 active 를 본다) 둘 다 같은 락으로 직렬화되어야
  "A 는 fence 없음을 보고 stream 을 넣고, 동시에 B 는 active 없음을 보고 fence 를 넣는" 상호
  검사 레이스가 사라진다.
- 락은 커밋 시 자동 해제되지만 **배제는 락이 아니라 커밋된 행이 유지**한다. 그래서 fence 를
  잡은 트랜잭션이 끝난 뒤에도(호출부는 `finally: release_fence(...)` 를 트랜잭션 **밖**에서
  돈다) fence 가 계속 유효하다.
- 이 때문에 fence 를 "호출부(`claim_owner`)의 `uow` 트랜잭션 안 xact lock" 으로 만드는 안은
  채택하지 않았다. `app.core.session_lifecycle::SessionLifecycleCoordinator._prepare_transient`
  와 `_delete_transient` 는 fence 를 `repository.lock_session(...)` **밖에서** 잡고 그 안팎에
  걸쳐 들고 있다. xact lock 은 커밋 시 풀리므로 이 구간을 덮지 못한다. 레지스트리는 자기
  커넥션·자기 트랜잭션을 쓰고 fence 는 lease 를 가진 **행**이다.
- `stream_key` 만 있고 스코프가 없는 acquire(판매자 스트림, 신원 없는 dev 요청)는 fence 의미가
  없어 스코프 락을 잡지 않는다. PK 충돌만으로 배제가 성립한다.

`release_fence(token)` 는 여전히 **발급한 토큰 객체만** 해제할 수 있다(프로세스 로컬 미러의
객체 동일성 검사 — 위조 토큰은 `ValueError`). 공유 백엔드는 추가로
`DELETE ... WHERE (owner_id, session_id, fence_token) = (...)` 로 자기 행만 지운다. 부기가 아니라
실제 해제다.

## 4. lease 와 heartbeat

- `active_streams`·`stream_scope_fences` 모든 행이 `lease_expires_at` 을 갖는다. **읽기는 항상
  `lease_expires_at > now()` 로 필터**하고, 쓰기는 만료 행을 조건부 UPSERT 로 탈취한다:

  ```sql
  INSERT INTO active_streams (...) VALUES (...)
  ON CONFLICT (stream_key) DO UPDATE SET ... WHERE active_streams.lease_expires_at <= now()
  RETURNING stream_key
  ```

  행이 돌아오면 획득 성공(신규 삽입 또는 만료 행 탈취), 안 돌아오면 살아있는 소유자가 있다는
  뜻이다. 한 문장이므로 PK 상에서 원자적이고, 만료 행 청소와 획득이 분리되지 않는다
  (`DELETE` 후 `INSERT` 로 쪼개면 두 워커가 같은 만료 슬롯을 동시에 가져갈 수 있다).

- 연장은 **이미 있는 폴링 tick 을 재사용**한다. `app.core.stream::open_stream` 의 first-token
  대기 루프와 `_wrapped` 루프는 `settings.stream_disconnect_poll_s` 마다 `asyncio.wait` 에서
  돌아온다. 그 지점에서 `registry.lease_renewal_due(stream_key)`(**sync**, 인메모리 백엔드는
  항상 `False`)를 먼저 보고, 참일 때만 `await registry.renew_lease(stream_key)` 로 DB 를 친다.
  프레임마다 DB 를 치지 않으며, 기본 백엔드에서는 코루틴조차 만들지 않는다.
- TTL(60s) 은 연장 간격(5s)의 12배다. 연장을 여러 번 놓쳐도 슬롯이 죽지 않는다.
- 연장 실패는 스트림을 죽이지 않는다(로그만). `_note_active_streams` 와 같은 격리 원칙이다 —
  관측/부기 실패가 응답 수명주기를 바꾸면 안 된다(#48 슬롯 누수의 성질). 잔여 위험은 §7.

## 5. `wait_for_scope_idle`

`asyncio.Event` 는 프로세스를 못 넘는다. 공유 백엔드는 **폴링 + 상한**으로 재설계한다.

- `stream_registry_scope_poll_s` 마다 `SELECT 1 FROM active_streams WHERE owner_id=… AND
  session_id=… AND lease_expires_at > now() LIMIT 1` 을 보고, 없으면 즉시 반환한다.
- `stream_registry_scope_idle_wait_max_s` 를 넘기면 경고 로그와 함께 반환한다. **무한 대기는
  하지 않는다** — 원격 워커가 죽어도 lease 만료(≤ TTL)로 풀리고, 살아있는 원격 스트림도
  `stream_total_timeout_s` 상한이 있으므로 상한 도달은 이상 상황이다.
- 인메모리 백엔드의 event 기반 구현은 **그대로 둔다**(기본 동작 동등성).

호출부는 `app.core.session_lifecycle::SessionLifecycleCoordinator.begin_terminal` 뿐이다.
상한 도달 시 동작은 기존과 같다 — terminal state 게이트가 이미 새 touch/claim 을 막고 있고,
이어지는 transient 처리는 fence 획득 실패 시 `skipped(active)` 로 다시 대기한다.

## 6. 장애 시 동작 — fail-closed

공유 저장소를 못 쓰면 `acquire`·`acquire_fence`·`wait_for_scope_idle` 은
`app.core.session_context::SessionStateUnavailable` 을 던진다. `app.core.errors` 가 이미 이 예외를
**503 `STATE_UNAVAILABLE`** 로 매핑한다(`docs/api-spec.md` §2.5 표에 있는 기존 코드 — 새 오류
코드를 만들지 않았고 계약은 그대로다).

fail-open(그냥 통과)은 받지 않는다. 다중 워커에서 §2.9(a) 가드가 사라져 동시 스트림이 조용히
겹치고, 그게 이 이슈가 막으려던 바로 그 실패다.

`release`·`release_fence`·`renew_lease` 실패는 던지지 않는다(로그만). 해제 실패는 lease 만료로
자가 치유되고, `finally` 에서 도는 경로라 던지면 원래 예외를 가린다.

`backend="memory"` 에서는 이 경로 자체가 존재하지 않는다(DB 를 타지 않음).

## 7. 기존 호출부 영향

| 심볼 | 변화 |
|---|---|
| `app.core.stream::open_stream` | `acquire`/`release` 를 await. `acquire` 의 `SessionStateUnavailable` 을 관측 마감 후 재전파(503). 폴링 tick 에 lease 연장 훅 추가. |
| `app.core.stream::_note_active_streams` | 변화 없음 — 예외 격리 그대로, `active_count()` 는 여전히 sync·로컬. |
| `app.core.stream::_ResponseLifecycle.finish` | `finally` 에서 `await registry.release(...)`. 순서·격리 불변. |
| `app.core.session_lifecycle::SessionLifecycleCoordinator.claim_owner` | `await registry.acquire_fence(...)`, `finally` 에서 `await registry.release_fence(...)`. 원자성 근거 주석을 §3 으로 교체. |
| `…::_prepare_transient` / `_delete_transient` | 같은 await 화. fence 수명이 `lock_session` 트랜잭션 밖까지 걸쳐 있다는 점이 §3 의 설계 근거다. |
| `…::begin_terminal` | 변화 없음(이미 await). 공유 백엔드에서 폴링·상한이 붙는다. |
| `app.main::_warn_process_local_registry_workers` | 상수 대신 `registry_is_process_local()` 을 본다. |
| `app.main::_close_owned_resources` | `stream_registry` 자원 close 를 목록에 추가(기본 백엔드에서는 no-op). |

### 잔여 위험

- lease 연장이 반복 실패한 채 스트림이 TTL 보다 오래 살면, 다른 워커가 같은 `stream_key` 를
  잡아 §2.9(a) 가 그 방에서 한 번 우회될 수 있다. TTL 60s vs 연장 간격 5s 로 12회 연속 실패가
  필요하고, 그 정도면 DB 자체가 죽어 acquire 도 fail-closed 로 막히는 상태다.
- 인메모리 저장소로 도는 단위 테스트는 레지스트리 **프로토콜 로직**을 고정한다. SQL 의 원자성
  (advisory lock·조건부 UPSERT)은 `tests/integration/test_pg_shared_stream_registry.py` 가
  실제 pg-profile 로 검증한다.
- 이 문서는 스트림 레지스트리만 다룬다. `docs/specs/OPS-SCALEOUT-476.md` §2 인벤토리의 나머지
  프로세스 로컬 상태(카트 RMW·`_confirm_locks`·rate limiter·profile 락 등)는 그대로 남아 있다.
  **공유 레지스트리를 켠다고 워커 증설이 안전해지는 것은 아니다** — 그 조건들은 별도다.
