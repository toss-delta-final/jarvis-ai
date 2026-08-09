# 변경 요약

I-1 Spring 검색이 재시도 가능한 실패 뒤 실제 다음 시도에 들어갈 때, 구매자 스트림이 `retrying`
progress를 즉시 한 번만 내보내도록 ContextVar 관측 seam과 본검색 queue/task 드레인을 추가했다.

기본 `spring_max_retries=0`(#394 한시 조치)에서는 신호가 물리적으로 발화하지 않아 기존 인라인
`await gather(...)` 경로를 그대로 유지한다. 발화 0회인 신호 때문에 모든 추천 턴의 취소·ContextVar·trace
의미를 자식 task로 바꾸지 않기 위한 D1 게이트다.

## 관련

Closes #406

- api-spec §3.1, v0.29.5
- `retrying`은 본검색만 배선한다. 구제 체인·`_run_search_unfiltered`·자동 완화 probe의 재시도는
  이번 범위 밖이라 신호를 내지 않는다.
- v0.29.4는 열려 있는 PR #502(#472 정본 전수 대조)가 선점했다. #502가 이 PR보다 늦게 병합되면
  api-spec 버전 번호에 구멍이 남을 수 있다.

## 체크리스트

- [x] `uv run pytest` 통과 — `5282 passed, 156 deselected, 1 warning in 327.08s`
- [x] `uv run ruff check` 통과 — `All checks passed!`
- [x] CHANGELOG 갱신
- [x] 계약 `progress.stage` 추가에 맞춰 `docs/api-spec.md` §3.1 동기화
- [x] `docs/lessons.md` 기록 — 전체 `ruff format`은 변경 파일로 범위를 제한한다(#406)
- [x] 신원은 JWT `sub`에서만 도출 · productId는 string

## 리뷰 노트

- `observe_search_retry`는 `create_task` 생성 시점에 복사된 ContextVar에서만 동작하고, with scope는
  즉시 닫는다. 따라서 retrying frame을 yield해도 observer·budget·suppression이 다음 턴으로 새지 않는다.
- 조기 스트림 종료에서는 검색 task를 동기 `cancel()`만 하며 await하지 않는다(#84 취소 규율).
- 기본값 `spring_max_retries=0`은 변경하지 않았다. 이 상태에서는 `retrying`이 실제 와이어에 나오지
  않으며 #394 원복 시에만 D1의 retry-progress 경로가 열린다.
- 변이 시험: M1(드레인 제거)은 T1을 `TimeoutError`로, non-deferred/deferred 실제 httpx 재시도
  회귀를 각각 progress frame 누락 단언으로 실패시켰다. M2(재시도 게이트 제거)는 T2의
  `retrying` 부재 단언을, M3(1회 플래그 제거)는 T3의 `2 == 1` 단언을 실패시켰다.
- 변이 시험: M4(notify 제거)는 T6 성공 재시도 case의 `0 == 1` 단언을, M5(비재시도 종료 notify)는
  T6 400 case의 `1 == 0` 단언을 실패시켰다. 모두 즉시 원복 후 회귀·전체 스위트를 재실행했다.
