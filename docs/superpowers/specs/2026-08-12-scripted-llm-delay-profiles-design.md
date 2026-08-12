# Scripted LLM Delay Profiles Design

## Goal

부하 테스트용 `LoadTestLLM`을 두 실행 프로파일로 제공한다.

- `instant`: 현재처럼 지연 없이 응답해 FastAPI·DB·Spring의 최대 처리량을 측정한다.
- `delayed`: 사용자 실측 평균에 맞춘 5초의 비동기 대기를 요청당 한 번 적용해 오래 열린 SSE 연결과 동시성을 측정한다.

5초는 최종 종단 시간을 5초로 고정하는 값이 아니라 scripted LLM이 추가하는 I/O 대기다. 실제
벤치마크 종단 시간에는 FastAPI·DB·Spring 처리시간이 함께 포함된다.

## Configuration

환경변수는 다음 두 설정으로 노출한다.

```env
SCRIPTED_LLM_MODE=instant
SCRIPTED_LLM_DELAY_S=5.0
```

`SCRIPTED_LLM_MODE`은 `instant | delayed`만 허용하고 기본값은 기존 동작을 보존하는 `instant`다.
`SCRIPTED_LLM_DELAY_S`는 0 이상 60 이하의 초 단위 실수이며 기본값은 5.0이다. 이 설정은
`LLM_PROVIDER=scripted`일 때만 소비된다.

## Runtime Behavior

`get_llm()`은 scripted provider를 선택했을 때 설정값을 `LoadTestLLM(mode=..., delay_s=...)`로
전달한다. `instant`는 대기하지 않는다. `delayed`는 첫 번째로 인식된 `complete()` 호출 전에
`asyncio.sleep(delay_s)`를 정확히 한 번 실행한다.

한 요청의 buyer graph는 `get_llm()`으로 `LoadTestLLM` 인스턴스를 하나 만들고 decompose, rerank 등
여러 LLM 호출에 재사용한다. 호출마다 5초를 적용하면 총 지연이 호출 수에 비례해 10초 이상으로
부풀기 때문에, 인스턴스당 하나의 sleep task만 만든다. 동시에 진입한 호출도 그 task를 함께
기다리고, 한 waiter의 취소가 공유 sleep을 취소하지 않게 shield한다. 대기는 blocking sleep이
아니라 `asyncio.sleep`을 사용해 실제 외부 I/O 대기처럼 이벤트 루프를 양보한다.

미상 프롬프트는 기존처럼 즉시 `LLMError`를 내며 지연 예산을 소비하지 않는다. 응답 JSON, 호출
분류, 모델 ID와 vendor 미호출 보장은 바꾸지 않는다.

## Verification

- 설정 기본값과 환경변수 파싱을 검증한다.
- `instant`가 sleep을 호출하지 않는지 검증한다.
- `delayed`가 여러 `complete()` 호출에도 5초 sleep을 한 번만 요청하는지 검증한다.
- 동시 `complete()` 호출도 하나의 sleep을 함께 기다리는지 검증한다.
- `get_llm()`이 설정을 `LoadTestLLM`에 배선하는지 검증한다.
- scripted 모드에서는 I-17 카탈로그 enrichment job이 등록되지 않는지 검증한다.
- 배포에 노출한 rate-limit 값이 양수인지 검증한다.
- 기존 scripted 회귀 테스트와 lint를 통과시킨다.

## Operational Use

```env
# 최대 처리량 측정
APP_ENVIRONMENT=test
LLM_PROVIDER=scripted
SCRIPTED_LLM_MODE=instant

# 실제 평균 대기시간을 반영한 동시 연결 측정
APP_ENVIRONMENT=test
LLM_PROVIDER=scripted
SCRIPTED_LLM_MODE=delayed
SCRIPTED_LLM_DELAY_S=5.0
```

두 결과 모두 실 LLM 네트워크·429·토큰 비용을 포함하지 않는다. `delayed`는 지연과 SSE 연결
수명만 근사한다. scripted 모드에서는 session lifecycle job은 유지하지만 I-17 카탈로그
enrichment job은 등록하지 않아 가짜 생성물과 cursor가 실 DB를 오염시키지 않게 한다.

동일 EC2에서 테스트할 때는 실제 사용자 트래픽을 먼저 차단하고 기존 GitHub Variables를 기록한다.
테스트 후 `APP_ENVIRONMENT`, `LLM_PROVIDER`, `SCRIPTED_LLM_*`, `RATE_LIMIT_*`를 모두 원복해
재배포한 뒤 scripted 배너가 사라지고 smoke 요청이 실제 model ID를 기록하는지 확인한다.
