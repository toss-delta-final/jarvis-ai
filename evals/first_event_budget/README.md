# 첫 이벤트 예산 실측 (#277)

`measure_first_event.py`는 FE→AI 실 HTTP 경계에서 요청 송신부터 첫 SSE `data:` 프레임
수신까지를 잰다. 이는 api-spec §2.9(c)의 first-token 상한이 실제로 재는 구간이다.

## 조건

- 출고 기본 설정: 검색 3s(`SPRING_SEARCH_TIMEOUT_S`)·재시도 1회, 첫 이벤트 10s, 구매자 전체 30s
  — 실행 조건은 결과 파일의 `payload["config"]`가 스스로 말한다(**[#306]** `spring_search_
  timeout_s`·`rescue_budget_mode`·`rescue_tail_reserve_s` 3키를 추가했다. `narrow` 모드의
  런타임 좁히기가 실제 단 예산을 정하므로 이 키들이 없으면 결과를 해석할 수 없다)
- 실 uvicorn + httpx streaming 사용
- Spring은 `MockTransport`로 지연·타임아웃을 주입하되 실제 I-1 재시도 코드를 통과
- pg-catalog·pg-profile·Google 임베딩 API는 실물
- 빠른 시나리오 n=20, 느린 시나리오 n=8, 대조군 n=5
- p95 CI는 bootstrap 2,000회, confidence 0.95, seed 277

LLM은 `ScriptedLLM`이라 라우팅·decompose·카테고리 매핑 head가 제외된다. Spring 지연도
미기동 로컬 환경에서 주입한 값이며, 결과는 staging 성능 수치가 아니다. 인프로세스
`TestClient`는 SSE 본문을 버퍼링해 첫 이벤트와 전체 종료를 구분하지 못하므로 사용하지 않는다.

결과 파일은 6개다. `results/measure-277-20260804.json`은 미룬 턴의 I-1 재시도 스킵
**전** 불변 원본이고, `results/measure-277-20260804-after-retry-skip.json`은 스킵 **후**
결과이며 `D3_deferred_worst_no_retry` 시나리오가 추가된 실행이다. **[#289 추가]**
`results/measure-289-20260805-flag-off.json`/`…-flag-on.json`은 구매자 `progress` 이벤트
(계약 등재 완료·기본 on, #396 — `app/core/config.py::progress_events_enabled`)를 끈/켠 상태에서
같은 하네스를 재실행한 결과다. `payload["config"].progress_events_enabled`가 그 실행의
조건을 스스로 말한다(하네스 로직·시나리오·seed는 #289에서 변경하지 않았다). off는 #277
이후(재시도 스킵 적용) 상태와 사실상 동일한 기준선이고, on은 모든 시나리오의 첫 이벤트가
`progress`로 바뀌며 p50이 6개 대표 시나리오 전부에서 ~12~15ms로 수렴한다(`D3`는
6869.8ms→11.6ms) — 상세 비교표와 해석은 `scratchpad/draft-progress-contract.md` §8 참고.

**[#306 추가]** `results/measure-306-20260810-before.json`/`…-after.json`은 **미룬 턴 재시도
억제를 제거하기 전/후**를 같은 하네스로 잰 짝이다. 앞선 4개와 달리 이 둘은 `spring_max_retries=1`
(#406이 #394를 원복)·`rescue_budget_mode=narrow`가 기본인 상태에서 측정됐다 — 그래서
`measure-289-*`(2026-08-05, `narrow` 배선 이전)와 직접 비교하면 안 된다. 관찰:

| 시나리오 | before | after |
|---|---|---|
| `D_deferred_worst` | 1회 시도 → `SEARCH_FAILED` @≈3.0s | **4회 시도 → 정상 답변 @≈6.0s** |
| `E`·`H`·`D2` | 1회 시도 → `SEARCH_FAILED` @≈3.0s | 2회 시도 → `SEARCH_FAILED` @≈5.0s |
| `A`·`B`·`C`·`F`·`G`·`D3` | — | 불변 |
| 첫 이벤트 p50 | 3~5.6ms (`progress`) | 3~4.4ms (`progress`) — 불변 |

즉 **재시도가 살려내는 것은 2차가 빠르게 응답하는 경우**(`D`)이고, 2차도 타임아웃 직전까지
쓰는 경우(`D2`·`H`)는 `narrow`가 `(30 − 15 − 경과)/3 ≈ 5.0s`에서 잘라 여전히 실패한다 —
#277이 8/8 재현한 「1차 3.0s 타임아웃 + 2차 2.9s 성공 = 11.8s」 조합이 되살아나지 않는다는
증거다. 대가는 확정 실패 감지가 3.0s→≈5.0s로 늦어지는 것이며, 그동안 `retrying` progress가
나가 화면이 비지 않는다.

⚠️ 이 두 실행은 워크트리에 `.env`가 없어 `GOOGLE_API_KEY`가 빈 상태로 돌았다 — `conditions`
**뒤** rerank 구간이 Spring 순서로 degrade한다. 관심 구간(첫 `conditions` 앞 직렬 I-1)에는
영향이 없고 before/after가 같은 조건이라 차분은 유효하지만, `total_ms` 절대값은 실운영보다
낙관적이다.

## 재실행

pg-catalog·pg-profile과 임베딩 API 설정을 준비한 뒤 저장소 루트에서 실행한다.

```bash
uv run python evals/first_event_budget/measure_first_event.py \
  --out /tmp/measure-first-event.json
```

특정 시나리오만 돌리려면 `--only D2_deferred_worst_slow_ok`처럼 id 접두어를 준다.
