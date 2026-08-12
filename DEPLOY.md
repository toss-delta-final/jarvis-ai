# 배포 가이드 — jarvis-ai

배포 담당이 이 repo만 보고 배포할 수 있도록 정리한 단일 문서다.
아키텍처·계약 원본은 [README.md](README.md) / [docs/api-spec.md](docs/api-spec.md), 환경변수 원본은 [.env.example](.env.example).

## 0. 이 repo는 무엇이고, 어디에 배포하나

- **FastAPI / Python 3.12** AI 에이전트 서버(자비스). 프론트엔드(`toss-delta-final/jarvis-frontend`)·Spring 백엔드(`toss-delta-final/jarvis-backend`)는 별도 repo.
- FE가 Spring 발급 JWT로 **AI 서버를 직접 호출**(SSE)한다. AI는 상품 원본을 복제하지 않고 질의 시점에 Spring 검색을 위임하며, AI 생성물(search_doc·임베딩·프로필)만 자체 PostgreSQL에 둔다.
- **배포 트리거**: 배포팀 CD가 **`main` 머지(=`dev → main` 승격) 기준**으로 동작한다(백엔드 [`deploy.yml`](https://github.com/toss-delta-final/jarvis-backend/blob/main/.github/workflows/deploy.yml)과 동일 패턴). 브랜치 전략은 [README §Git 워크플로](README.md) 참조.
- **런타임 의존**: 이 서버는 **PostgreSQL ×2(pgvector) + Spring API + LLM/임베딩 API**에 붙어야 동작한다(아래 §2·§4).

## 1. 빌드 & 실행 (컨테이너)

```bash
docker build -t jarvis-ai:dev .
docker run -p 8000:8000 --env-file deploy.env jarvis-ai:dev
```

- 멀티스테이지 `uv` 빌드(python:3.12-slim), **non-root**(`jarvis`), embedding 그룹 포함. dev 의존성 제외.
- 컨테이너는 **`0.0.0.0:8000`** 에서 uvicorn 기동(`app.main:app`). 포트는 `EXPOSE 8000`.
- DB ×2가 먼저 떠 있어야 앱이 정상 부팅한다(app 부팅 시 pgvector 확장 + 스토어 스키마 자동 setup). 로컬 전체 스택은 `docker compose up`(앱 + pg-catalog 5433 + pg-profile 5434).

## 2. 환경변수 (`deploy.env`)

전체 목록·용도는 [.env.example](.env.example)(모든 값이 pydantic-settings 필드와 1:1). 아래는 **배포 시 반드시 확인**할 항목.

**필수 (운영 = `AUTH_MODE=jwks` 기준):**

| 키 | 설명 |
|---|---|
| `CATALOG_DB_URL` / `PROFILE_DB_URL` | PostgreSQL ×2 접속(pgvector 필요 — §4). 배포 인프라 값으로. |
| `AUTH_MODE` | 운영은 **`jwks`**(Spring 공개키 RS256 검증). `dev`는 서명 검증 없이 디코드 — **로컬 전용, 운영 금지**. |
| `JWKS_URL` | Spring `GET /.well-known/jwks.json`(배포된 Spring 주소 기준). |
| `JWT_ISSUER` / `JWT_AUDIENCE` | 토큰 iss/aud 검증값(기본 `jarvis-spring-auth` / `jarvis-fastapi-ai`). |
| `JWT_SCOPE` | **exact `chat:stream` 필수**. 빈 값·다른 값·복합 scope는 허용하지 않으며 잘못 주입하면 기동 실패한다. |
| `INTERNAL_API_TOKEN` | AI↔Spring `X-Internal-Token`. **백엔드 `app.internal.token` 과 동일 값**(불일치 시 검색·담기·주문 등 `/internal` 양방향 차단). `jwks` 모드에서 미설정 시 **기동 실패**. |
| `SPRING_BASE_URL` | 역호출 대상 Spring API 주소(검색·장바구니·주문·카탈로그 배치). |
| `OPENAI_API_KEY` | 기본 provider(`LLM_PROVIDER=openai`)일 때 필수. |
| `GOOGLE_API_KEY` | 임베딩(gemini-embedding-001) — 카탈로그 배치·검색 임베딩에 필요. |
| `CORS_ORIGINS` | FE 직접 호출 오리진(JSON 배열 문자열). **FE 운영 오리진 포함 필수**(예: `["https://<FE도메인>"]`). |

**LLM provider 토글**: `LLM_PROVIDER=openai`(기본) → `OPENAI_API_KEY`. `anthropic` 으로 바꾸면 → `ANTHROPIC_API_KEY`. 모델 id(`OPENAI_*_MODEL_ID`·`HAIKU/SONNET_MODEL_ID`)는 각 대시보드 실값으로.

**부하 테스트 전용 scripted 모드**: 실제 사용자 트래픽을 차단한 EC2에서만
`APP_ENVIRONMENT=test`, `LLM_PROVIDER=scripted`를 함께 사용한다. 내부 처리량 상한은
`SCRIPTED_LLM_MODE=instant`, 5초의 추가 비동기 대기를 포함한 SSE 동시 연결 측정은
`SCRIPTED_LLM_MODE=delayed`, `SCRIPTED_LLM_DELAY_S=5.0`을 사용한다. GitHub Variables에서
`RATE_LIMIT_PER_MIN`·`RATE_LIMIT_PER_HOUR`를 양의 정수로 일시 상향할 수 있다. scripted 모드에서는
세션 lifecycle sweep은 유지하되 I-17 카탈로그 enrichment 배치를 등록하지 않아 가짜 생성물이 실
카탈로그와 cursor를 오염시키지 않게 한다. 미등록/빈 값이면 네 설정 모두 env 파일에서 빠져 코드
기본값을 쓴다.

scripted 부하 테스트는 아래 순서를 체크리스트로 사용한다.

1. 변경 전 `APP_ENVIRONMENT`, `LLM_PROVIDER`, `SCRIPTED_LLM_*`, `RATE_LIMIT_*` 값을 기록한다.
2. ALB/보안 그룹 등에서 실제 사용자 트래픽을 차단하고 위 scripted 설정으로 재배포한다.
3. 기동 로그의 `STUB LLM MODE`, 선택한 프로파일·지연값, `I-17 job만 비활성화`를 확인한다.
4. k6/benchmark 실행 후 모든 GitHub Variables를 1번의 운영값으로 원복하고 다시 배포한다.
5. 원복 컨테이너에서 `STUB LLM MODE` 배너가 없음을 확인하고, smoke 요청 1건의
   `chat_request.model_ids`가 `scripted-stub-*`가 아닌 실제 provider 모델인지 확인한 뒤 트래픽을 연다.

provider·환경값은 rate limit과 함께 반드시 원복한다. `APP_ENVIRONMENT=test`와
`LLM_PROVIDER=scripted`가 남은 컨테이너를 실제 사용자에게 열면 가짜 추천이 정상 200으로 응답된다.

**선택 (기본값 있음):** 상태저장 타임아웃/풀(`STATE_STORE_*`), 배치 주기(`CATALOG_BATCH_INTERVAL_S`=300), 검색·추천 튜너블(`TOP_K`·`EXPOSE_*`·`LLM_CALL_LIMIT` 등), 프로필 튜너블(`PROFILE_*`).

**조건부 주입 손잡이** — `deploy.yml` 이 이 키들을 다른 19+개 키와
**똑같이 quoted heredoc(`cat << 'ENVEOF'`) 안에 데이터로만** 써서 `ENV_FILE` 을 만든 뒤,
heredoc 뒤에서 값이 비었거나 공백뿐인 줄만 `sed` 로 지운다(`.github/workflows/deploy.yml`
3b 단계) — **Variable 을 등록하지 않았거나 빈/공백뿐인 값을 등록하면 그 줄 자체가 env 파일에
남지 않고, 앱은 코드 기본값을 그대로 쓴다.** Variable 값은 heredoc 안의 데이터로만 쓰이므로
따옴표·공백이 섞여도 셸이 재해석하지 않는다 — **Variable 값을 실행되는 셸 문장에 직접
스플라이스하지 않는다**는 것이 이 방식의 핵심이다(값에 작은따옴표가 든 Variable 을 셸 문장에
바로 이어붙이면 원격 코드 실행으로 이어질 수 있다는 것이 PR #539 리뷰로 드러났다). 무조건
`KEY=${{ vars.X }}` 로만 쓰면 미등록 시 빈 문자열이 되는데, 아래 `SPRING_MAX_RETRIES`(int)·
`RESCUE_BUDGET_MODE`(Literal)는 빈 문자열 파싱에 기동이 죽으므로 sed 로 그 줄 자체를 지운다.

| 키 | 타입·허용값 | 미등록 시 |
|---|---|---|
| `MODEL_PRICE_IN_PER_1K` / `MODEL_PRICE_OUT_PER_1K` | `{"모델ID": USD/1,000tokens}` JSON 오브젝트 문자열. **표 전체를 치환**한다(기본표와 병합하지 않는다). | `app/core/model_pricing.py` 코드 기본표(`gpt-5-nano`/`gpt-5.6-luna`, 출처·기준일은 `evals/model_eval/pricing_manifest.json` 과 동일). `chat_request` 로그 `costUsd` 가 이 표로 집계된다(기동 로그 `MODEL_PRICE_DEFAULTS_IN_USE`/`MODEL_PRICE_MISSING_AT_STARTUP` 로 상태 확인). |
| `SPRING_MAX_RETRIES` | 정수, `0`~`1`. | 코드 기본값 `1`(PR #532/#406 — #394 원복). |
| `RESCUE_BUDGET_MODE` | `observe` \| `narrow` \| `narrow_skip`. | 코드 기본값 `narrow`. |

**롤백 절차 — `PROGRESS_EVENTS_ENABLED=false` 는 이제 단독으로 기동하지 못한다.** `SPRING_MAX_RETRIES`
기본값이 `0`→`1`로 바뀐 뒤로는, `progress_events_enabled=False`(미룬 턴 직렬 I-1 3회 12.0s)
와 `spring_max_retries=1`(재시도 포함 직렬 합이 더 커짐)을 함께 두면 `STREAM_FIRST_TOKEN_TIMEOUT_S`
가드가 기동 자체를 거부한다 — `tests/unit/test_progress_event.py::
test_progress_events_disabled_rejects_startup_with_retries_enabled` 가 `ValidationError`
(메시지에 `STREAM_FIRST_TOKEN_TIMEOUT_S` 포함)로 이 사실을 고정한다. **`PROGRESS_EVENTS_ENABLED=false`
로 되돌리려면 반드시 `SPRING_MAX_RETRIES=0` 을 함께 지정하라.** 같은 이유로 `RESCUE_BUDGET_MODE=observe`
로 되돌릴 때도 `SPRING_MAX_RETRIES=0` 을 짝으로 지정해야 한다(직렬 합 18.0s 가 `observe` 의
꼬리 예약 비교를 넘기지 못하도록).

## 3. ⚠️ 시크릿 — repo에 실제 값은 없다

repo에는 **키 목록(`.env.example`)만** 있고 실제 시크릿은 없다(커밋 금지). 배포 환경용으로 준비:

- `INTERNAL_API_TOKEN`: **백엔드팀과 동일 값으로 합의**(백엔드 `DEPLOY.md §3`에서 `openssl rand -hex 32`로 생성한 그 값). 양쪽이 달라지면 `/internal` 콜백이 막힌다.
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY`: 각 provider 대시보드 발급값.
- **JWT 서명키는 이 서버에 없다** — AI는 Spring의 JWKS 공개키로 **검증만** 한다(서명은 Spring). 그래서 별도 private key 주입 불필요.
- 시크릿은 repo 밖 안전 채널로만 공유. 배포 환경에선 GitHub Environment/Actions Secrets 등 시크릿 저장소 사용 권장.

## 4. DB 준비 (필수) — PostgreSQL ×2 (pgvector)

두 DB 모두 **pgvector 확장**이 필요하다(`pgvector/pgvector:pg16` 이미지 권장). `catalog`·`profile`로 분리:

- **catalog** — 상품 AI 생성물(`products`: search_doc·embedding vector(1536)·extras + HNSW), `categories`.
- **profile** — 프로필/스레드 상태, `processed_events`(session-end 멱등 lifecycle) 등.

**A. 컨테이너로 띄우는 경우(권장 — compose와 동일):** `pgvector/pgvector:pg16` 두 개를 각각 띄우고 init 스크립트를 `/docker-entrypoint-initdb.d`로 마운트하면 **빈 볼륨 최초 부팅 시 자동 생성**된다.
- catalog init: [`db/catalog/init/`](db/catalog/init/) (`00_products.sql` → `02_categories.sql`)
- profile init: [`db/profile/init/`](db/profile/init/) (`00_processed_events.sql` → `01_conversation_turns.sql` → `02_profile_session_activity.sql` → **`03_chat_session_contexts.sql`** → ... → `05_seller_analysis.sql`, 판매자 분석 저장 계층 5테이블, 이슈 #585)

**B. 관리형 PostgreSQL(RDS 등)인 경우:** pgvector 확장 가용 확인 후 위 init SQL을 순서대로 수동 적용. **기존 볼륨 업그레이드**는 [`db/catalog/migrations/`](db/catalog/migrations/)의 마이그레이션도 적용:
```bash
psql "$CATALOG_DB_URL" -f db/catalog/init/00_products.sql   # 이후 02
psql "$PROFILE_DB_URL" -f db/profile/init/00_processed_events.sql   # 이후 01, 02, 03 ... 05
# 기존 볼륨: db/catalog/migrations/*.sql 을 날짜순 적용
```

> 앱은 부팅 시 pgvector 확장 + LangGraph 스토어 스키마를 idempotent 하게 자체 `setup()` 하고, `processed_events`도 앱 연결 시 idempotent migration 한다. 위 init/migration은 **상품·프로필 도메인 테이블**을 준비하는 것.

### 4.1 #187 lifecycle 기동 순서

AI 프로세스는 외부 요청과 scheduler를 열기 전에 다음 순서를 완료해야 한다.

1. `03_chat_session_contexts.sql`을 포함한 lifecycle schema를 idempotent 적용한다.
2. legacy activity를 설정된 batch 상한 안에서 **bounded backfill**하고 완료/재개 지점을 확정한다.
3. schema와 backfill이 성공한 뒤에만 inactivity/recovery scheduler를 시작한다.

schema 또는 backfill이 실패한 인스턴스는 readiness에 들어가거나 scheduler를 먼저 실행하면 안 된다.

## 5. 헬스체크

`GET /health` → `{"status":"ok"}`. ALB/오케스트레이터 헬스체크 타겟으로 사용.

## 6. 네트워킹 / CORS

- FE가 **AI 서버를 직접 호출(SSE)** 하므로, `CORS_ORIGINS`에 **FE 운영 오리진을 반드시 포함**해야 한다(백엔드의 동일 오리진 프록시 방식과 다름 — AI는 직접 호출 레인).
- AI→Spring 역호출(검색·장바구니·주문·카탈로그 배치)은 `SPRING_BASE_URL` + `X-Internal-Token`으로 나간다. 전 구간 타임아웃 3s.
- `/internal/**` 는 서비스 토큰으로 보호되지만, 가능하면 인그레스에서 외부 노출을 차단 권장.

## 7. 배포 담당 체크리스트

- [ ] `docker build -t jarvis-ai .`
- [ ] `deploy.env` 작성 — §2 필수값, `AUTH_MODE=jwks`, `INTERNAL_API_TOKEN`은 백엔드와 동일, `CORS_ORIGINS`에 FE 운영 오리진
- [ ] `JWT_SCOPE=chat:stream` exact 값 확인(빈 값·다른 값 금지)
- [ ] PostgreSQL ×2(pgvector) 준비 + profile `05_seller_analysis.sql`까지 init/migration 적용(§4)
- [ ] AI startup 로그에서 schema → bounded backfill 완료 → scheduler 시작 순서를 확인
- [ ] 컨테이너 실행(`-p 8000:8000`) 후 `GET /health` = `{"status":"ok"}` 확인
- [ ] Spring(`SPRING_BASE_URL`)·JWKS(`JWKS_URL`) 도달 확인 — 검색·인증 레인 정상
- [ ] FE팀에 **공개 AI API URL(SSE)** 공유
- [ ] (공개 노출 시) `/internal/**` 인그레스 차단

### 7.1 #187 외부 release gate — 순서 변경 금지

이 저장소의 로컬/CI 통과만으로 아래 외부 gate 완료를 주장하지 않는다.

1. **BE #63**: signed `sessionId`, `scope="chat:stream"`, `ticketTtlSeconds=60` 실제 증거를 먼저 남긴다.
2. 마지막 구 계약 티켓 발급 뒤 **90초 drain**(60초 TTL + 30초 여유)을 기다린다.
3. **AI**: lifecycle schema → bounded backfill → scheduler 순서로 배포한다.
4. **FE #52**: 실제 3-tab 로그인/refresh 시나리오를 검증한다.
5. 운영에서 missing-session, claim-conflict, cleanup-retry 등 합의된 **metrics**를 확인한 뒤에만 release gate를 닫는다.

## 8. LangSmith request tracing 운영 정책

LangSmith tracing은 선택 기능이며 기본값은 꺼짐(`LANGSMITH_TRACING=false`)이다. 애플리케이션은
원문 message/tool input·output/예외 메시지/header/고객 PII를 수집하지 않고 명시적 allowlist
metadata만 내보낸다. redaction 검증이 실패하면 trace 전체를 버리고 요청 결과에는 영향을 주지
않는다.

**[#326] 콘텐츠 추적 모드(`LANGSMITH_TRACE_CONTENT`, 기본 `false`)** — 위 비유출 원칙의
**명시적 디버깅 예외**다. 켜면 트레이스에 다음 원문이 실린다: 루트 span의 사용자 발화,
`llm.*` span의 system/user prompt·응답 전문, `spring.*` span의 요청 URL·본문·응답 페이로드
(HTTP 헤더는 모드와 무관하게 싣지 않는다 — `X-Internal-Token` 유출 방지). per-value 절단
상한은 `LANGSMITH_TRACE_CONTENT_MAX_CHARS`(기본 20000자).

- **실사용자 트래픽 오픈 전 디버깅 구간 전용이며, 오픈 시점에 반드시 `false`로 되돌린다.**
  이 전환은 릴리스 체크리스트 항목이다(§7 게이트와 같은 강제 수준으로 취급).
- 모드가 켜지면 metadata allowlist 검증은 유지되지만 `inputs`/`outputs` 서브트리는 redaction
  검증에서 면제된다(면제가 곧 기능). 즉 **켜는 순간 발화에 포함된 어떤 개인정보든 LangSmith에
  도달할 수 있다** — 워크스페이스 접근권한(§8.3)이 유일한 방벽이다.
- 기동 로그에 `LangSmith content tracing ON` WARNING이 찍힌다 — 배포 검증 시 이 로그로 모드
  상태를 확인한다. 미설정(빈 문자열) vars는 앱이 off로 해석한다(기동 실패 없음).
- 콘텐츠 페이로드는 커서 export가 무거워지므로 켤 때 `LANGSMITH_EXPORT_TIMEOUT_S`를 3.0 이상으로
  함께 올린다(0.5 기본값이면 대형 트레이스가 timeout으로 조용히 버려진다).
- 사고 시 kill switch 절차(아래)는 `LANGSMITH_TRACE_CONTENT=false` 전환에도 동일하게 적용된다.

### 8.1 환경별 프로젝트와 배포 기록

| 환경 | `LANGSMITH_PROJECT` |
|---|---|
| local | `jarvis-ai-local` |
| staging | `jarvis-ai-staging` |
| production | `jarvis-ai-production` |

각 배포 기록에는 실제 project/endpoint·region, `LANGSMITH_TRACING`,
`LANGSMITH_TRACING_SAMPLING_RATE`, SDK/application override 유무,
`LANGSMITH_EXPORT_TIMEOUT_S`, 조직 plan/RBAC 상태, service key scope·만료일을 남긴다.
키 값 자체는 기록하지 않는다.

- `LANGSMITH_TRACING_SAMPLING_RATE`는 `0.0`~`1.0`의 volume control이다. LangSmith의
  sampling은 확률적이므로 특정 canary가 반드시 export된다는 보장이 없다. staging privacy
  canary 동안에는 기록된 값을 `1.0`으로 설정하거나, 검증된 deterministic conditional tracing
  규칙으로 해당 요청만 반드시 포함한다. 평상시 배포값도 반드시 변경 이력에 기록한다.
- Jarvis의 명시적 exporter는 `get_trace_factory()`의 애플리케이션 kill switch 뒤에서만
  생성한다. `RunTree.post()`, `tracing_context(enabled=True)` 또는 다른 직접 exporter 경로를
  추가해 이 gate를 우회하면 안 된다.
- 사고 시 먼저 `LANGSMITH_TRACING=false`로 변경하고 현재 배포 방식에 맞게 restart/reload하여
  캐시된 factory까지 교체한다. 설정만 저장하고 프로세스를 그대로 두는 것은 kill switch
  적용 증거가 아니다. 이후 trace가 더 생성되지 않는지 확인한다.

참고: [sampling](https://docs.langchain.com/langsmith/sample-traces),
[conditional tracing](https://docs.langchain.com/langsmith/conditional-tracing),
[custom instrumentation](https://docs.langchain.com/langsmith/annotate-code),
[metadata/tags](https://docs.langchain.com/langsmith/add-metadata-tags).

### 8.2 보존·삭제·삭제 확인

- 공식 trace tier는 **Base 14일 / Extended 400일**이다. staging/production에서 실제 project
  tier를 확인한다. tier 변경은 새 trace에만 적용되고 일부 evaluator/automation/feedback
  설정은 새 trace를 Extended로 올릴 수 있다. Enterprise는 workspace Extended 기간을 별도로
  설정할 수 있으므로 현재 계약/설정을 추측하지 않는다.
- 보존 기간이 끝난 trace는 UI/API에서 사라진 뒤 user data가 내부 시스템에서 삭제되기까지
  추가 시간이 걸릴 수 있고, billing/analytics용 일부 metadata는 남을 수 있다. dataset
  데이터는 trace 보존과 별도다.
- project 전체 삭제는 UI 또는 SDK `delete_tracer_sessions`/project deletion API로 수행한다.
  개별 삭제는 project/session ID와 trace ID(요청당 최대 1,000개) 또는 workspace metadata
  purge를 사용한다.
- trace 삭제는 비동기(non-peak/weekend job)이며 완료 알림이 없다. 요청 직후 완료로 기록하지
  말고, 이후 같은 trace ID/selector를 다시 조회하여 없어졌음을 별도 증거로 남긴다.
- metadata purge의 여러 key/value 조건은 **AND가 아니라 OR**이다. `{environment: "staging",
  requestId: "..."}`처럼 넓은 selector와 고유 selector를 함께 보내지 않는다. canary 삭제는
  고유한 `requestId` 하나만 사용하거나 trace ID를 사용하고, 실행 전 대상 건수를 검토한다.

참고: [usage and retention](https://docs.langchain.com/langsmith/usage-and-billing),
[data purging](https://docs.langchain.com/langsmith/data-purging-compliance),
[manage a trace](https://docs.langchain.com/langsmith/manage-trace).

### 8.3 접근권한·service key 사고 대응

- Workspace Viewer/Editor/Admin RBAC는 **Enterprise 전용**이다. 다른 plan에서는 사용자가
  기본 Admin일 수 있으므로 least privilege가 적용됐다고 가정하지 말고 실제 plan/feature를
  배포 증거에 기록한다.
- Enterprise workspace RBAC가 켜져 있다면 읽기 사용자는 Viewer, 일반 운영자는 Editor,
  workspace 관리는 Admin으로 제한한다. built-in Editor는 run/project를 삭제할 수 없으므로
  purge 담당자에게만 시간 제한된 Admin 권한을 준다. trace 전송 주체에는 `runs:create`만
  포함한 최소 scope의 workspace service key를 우선 사용한다.
- PAT는 개인 script/tool용으로 제한한다. service key 교체는 “원자적 rotation”으로 표현하지
  않는다: 최소 scope·만료가 있는 replacement 생성 → 배포/reload → tracing 확인 → 기존 key
  삭제 순서다. 유출 의심 시 kill switch 적용과 노출 key 삭제를 먼저 하고, 영향 trace는 좁고
  검토된 selector로 purge한 뒤 비동기 완료를 재조회한다.

참고: [RBAC](https://docs.langchain.com/langsmith/rbac),
[organization/workspace operations](https://docs.langchain.com/langsmith/organization-workspace-operations),
[API keys](https://docs.langchain.com/langsmith/create-account-api-key),
[organization API](https://docs.langchain.com/langsmith/manage-organization-by-api).

### 8.4 timeout·shutdown

- `LANGSMITH_EXPORT_TIMEOUT_S`(기본 `0.5`, 허용 `>0`~`5.0`)는
  `asyncio.timeout()`이 **요청 coroutine의 대기 시간만** 제한하는 값이다. exporter가
  `asyncio.to_thread()`로 시작한 worker thread와 그 안의 client/network send는 이 timeout으로
  취소되지 않는다. `TELEMETRY_EXPORT_TIMEOUT` 뒤에도 전송이 완료될 수 있으므로 이를 hard
  batch-send/network timeout 또는 “미전송 보장”으로 표현하지 않는다.
- hard connect/read/write/send 상한이 필요하면 LangSmith client/HTTP transport가 실제로
  제공·적용하는 별도 timeout을 구성하고 staging에서 검증해야 한다. 애플리케이션 wait timeout과
  client/network timeout의 값을 각각 배포 증거에 기록한다.
- 현재 Jarvis에는 timeout 뒤 계속 실행 중인 `to_thread` export를 추적하거나 drain하는
  application queue/handle이 없다. 정상 shutdown/restart 전에는 in-flight export가 있을 수
  있음을 가정하고 worker/client drain 동작을 관찰한다. 사고 kill switch는 새 export 생성을
  막을 뿐 이미 시작한 worker를 회수하지 못하므로, late send를 막아야 하면 노출 key 즉시
  revoke/delete, egress 차단 또는 해당 process 종료를 함께 수행하고 이후 새 trace가 없는지
  확인한다.
- 향후 명시적 in-flight registry/background queue를 추가하면 shutdown에서 새 demand를 막고,
  정해진 drain deadline까지 worker를 추적한 뒤 미완료 건을 기록한다. standalone LangSmith
  client/background tracing도 SDK `flush()`(또는 공식 LangChain equivalent)를 bounded하게
  호출하되 사용자 요청 timeout/cancellation보다 우선시하지 않는다.

### 8.5 staging canary 및 삭제 증거

라이브 실행은 **배포 후** 수행하며 로컬 테스트 통과를 staging 실행 증거로 대신하지 않는다.

1. §8.1의 project/endpoint·region, tracing/sampling/override, timeout, plan/RBAC, service-key
   scope를 기록한다. canary 구간은 sampling `1.0` 또는 검증된 deterministic allow rule로 한다.
2. buyer 1건과 seller 1건에 각각 고유한 비밀 아닌 canary `requestId`를 사용한다. message,
   nested tool arguments/results, provider exception, Authorization/Cookie 형태 값, 고객
   name/email/phone/address, nested metadata에는 서로 구별되는 canary를 심되 실제 secret/PII는
   사용하지 않는다.
3. 각 HTTP 응답의 `X-Request-Id`를 기록한다. LangSmith에서 같은 `requestId` metadata로 root를
   찾아 buyer=`buyer_chat_turn`, seller=`seller_chat_turn` root가 정확히 하나인지 확인한다.
   모든 child가 같은 trace ID이며 parent를 가지는지 확인한다.
4. `server_first_event_ms`, `server_first_text_token_ms`, `provider_ttft_ms`의 의미와 경계를
   확인하고 timeout/cancel/degrade/tool-error fixture를 각각 검증한다.
5. 같은 `requestId`로 structured log를 조회해 HTTP 응답 ↔ log ↔ trace 상관관계를 증명한다.
   captured outgoing payload와 UI/API 표시 데이터를 재귀 탐색하여 모든 canary가 없는지
   확인한다. metadata/tags도 export 데이터이므로 allowlist 외 값이 없어야 한다.
6. secret/PII를 가린 screenshot, trace ID, 설정 기록을 PR #141 증거로 첨부한다.
7. 검증 뒤 trace ID 또는 고유 `requestId` 단일 selector로 삭제를 요청한다. 비동기 작업 후
   다시 조회해 삭제 완료 시각과 결과를 첨부한다. 삭제 전/후 query와 대상 건수도 남긴다.
